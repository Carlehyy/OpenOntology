"""
消息通知 — 渠道转发（apprise）service

职责：渠道 CRUD（URL Fernet 加密、界面只回脱敏掩码）、消息到达即生成投递单、
APScheduler 扫描器异步执行投递（重试上限 3 次）、渠道测试直发（不落消息表）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.shared.encryption import decrypt as fernet_decrypt
from app.shared.encryption import encrypt as fernet_encrypt

from app.notifications.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationMessage,
)

logger = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 3
BODY_TRUNCATE_CHARS = 4000
DELIVERY_SCAN_LIMIT = 50


def _now() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds") + "Z"


def _send_via_apprise(apprise_url: str, *, title: str, body: str) -> None:
    """同步发送；由测试 monkeypatch 此函数做离线验证。

    apprise 的 URL 语法即渠道配置（mailto://、dingtalk://、json:// 等），
    MARKDOWN 正文由 apprise 按渠道能力自行降级转换。
    """
    import apprise

    apprise_obj = apprise.Apprise()
    if not apprise_obj.add(apprise_url):
        raise ValueError("apprise URL 无法解析")
    try:
        ok = apprise_obj.notify(
            title=title,
            body=body,
            body_format=apprise.NotifyFormat.MARKDOWN,
            timeout=15,  # 单渠道默认上限 60s：显式收紧，防慢渠道占住扫描线程
        )
    finally:
        apprise_obj.clear()
    if not ok:
        raise RuntimeError("渠道投递失败（notify 返回失败）")


def _safe_error(value: str) -> str:
    return " ".join((value or "投递失败").split())[:500] or "投递失败"


# ── 渠道 CRUD ─────────────────────────────────────────────────


def _validate_apprise_url(url: str) -> str:
    trimmed = (url or "").strip()
    if not trimmed:
        raise HTTPException(422, "apprise URL 不能为空")
    if "://" not in trimmed:
        raise HTTPException(422, "aprise URL 需形如 scheme://…（如 json://、mailto://、dingtalk://）")
    if len(trimmed) > 2000:
        raise HTTPException(422, "apprise URL 过长（上限 2000 字符）")
    return trimmed


def mask_apprise_url(url: str) -> str:
    """scheme://…abcd：只暴露协议与末 4 字符（URL 本身即凭据）。"""
    trimmed = (url or "").strip()
    if "://" not in trimmed:
        return "…"
    scheme, _, rest = trimmed.partition("://")
    tail = rest[-4:] if len(rest) > 8 else ""
    return f"{scheme}://…{tail}" if tail else f"{scheme}://…"


def create_channel(
    db: Session, *, name: str, apprise_url: str, note: str | None, user=None
) -> NotificationChannel:
    name = (name or "").strip()
    if not name:
        raise HTTPException(422, "name 不能为空")
    if len(name) > 200:
        raise HTTPException(422, "name 过长（上限 200 字符）")
    url = _validate_apprise_url(apprise_url)
    if db.query(NotificationChannel).filter(NotificationChannel.name == name).first():
        raise HTTPException(409, f"同名渠道已存在：{name}")
    row = NotificationChannel(
        name=name,
        apprise_url_encrypted=fernet_encrypt(url),
        note=((note or "").strip()[:500] or None),
        created_by=getattr(user, "id", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_channel(
    db: Session,
    channel: NotificationChannel,
    *,
    name: str | None = None,
    apprise_url: str | None = None,
    note: str | None = None,
    enabled: bool | None = None,
) -> NotificationChannel:
    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(422, "name 不能为空")
        clash = (
            db.query(NotificationChannel)
            .filter(NotificationChannel.name == name, NotificationChannel.id != channel.id)
            .first()
        )
        if clash:
            raise HTTPException(409, f"同名渠道已存在：{name}")
        channel.name = name
    if apprise_url is not None and apprise_url.strip():
        channel.apprise_url_encrypted = fernet_encrypt(_validate_apprise_url(apprise_url))
    if note is not None:
        channel.note = (note.strip()[:500] or None)
    if enabled is not None:
        channel.enabled = bool(enabled)
    db.commit()
    db.refresh(channel)
    return channel


def delete_channel(db: Session, channel: NotificationChannel) -> None:
    # SQLite（测试/开发）默认不强制外键：显式删投递单，不依赖 DB 级 CASCADE
    (
        db.query(NotificationDelivery)
        .filter(NotificationDelivery.channel_id == channel.id)
        .delete(synchronize_session=False)
    )
    db.delete(channel)
    db.commit()


def require_channel(db: Session, channel_id: str) -> NotificationChannel:
    row = db.query(NotificationChannel).filter(NotificationChannel.id == channel_id).first()
    if row is None:
        raise HTTPException(404, "渠道不存在")
    return row


def channel_url(channel: NotificationChannel) -> str:
    return fernet_decrypt(channel.apprise_url_encrypted)


def channel_out(channel: NotificationChannel) -> dict[str, Any]:
    try:
        url_masked = mask_apprise_url(channel_url(channel))
    except Exception:  # noqa: BLE001 — 密文损坏时掩码退化为占位，不阻断列表
        url_masked = "…"
    return {
        "id": channel.id,
        "urlMasked": url_masked,
        "name": channel.name,
        "enabled": channel.enabled,
        "note": channel.note,
        "lastStatus": channel.last_status,
        "lastError": channel.last_error or "",
        "lastSentAt": _iso(channel.last_sent_at),
        "createdAt": _iso(channel.created_at),
        "updatedAt": _iso(channel.updated_at),
    }


# ── 到达即生成投递单 + 异步执行 ───────────────────────────────


def fan_out_deliveries(db: Session, message: NotificationMessage) -> int:
    """消息新建即为所有启用渠道生成 pending 投递单（幂等：唯一约束跳过既有对）。"""
    channels = (
        db.query(NotificationChannel).filter(NotificationChannel.enabled.is_(True)).all()
    )
    existing = {
        row.channel_id
        for row in db.query(NotificationDelivery.channel_id).filter(
            NotificationDelivery.message_id == message.id
        ).all()
    }
    created = 0
    for channel in channels:
        if channel.id in existing:
            continue
        db.add(
            NotificationDelivery(
                message_id=message.id,
                channel_id=channel.id,
            )
        )
        created += 1
    if created:
        db.flush()
    return created


def dispatch_pending_deliveries(db: Session, *, limit: int = DELIVERY_SCAN_LIMIT) -> dict[str, int]:
    """扫描 pending 投递单并同步执行（调用方持有会话；失败重试至上限转 failed）。

    单渠道单次一条同步网络调用：apprise 自带请求超时；扫描器 max_instances=1
    防重叠。每条独立提交，单条失败不影响其余。
    """
    rows = (
        db.query(NotificationDelivery, NotificationChannel, NotificationMessage)
        .join(
            NotificationChannel,
            NotificationChannel.id == NotificationDelivery.channel_id,
        )
        .join(
            NotificationMessage,
            NotificationMessage.id == NotificationDelivery.message_id,
        )
        .filter(
            NotificationDelivery.status == "pending",
            # 停用渠道即止损：积压单冻结（不外发）；重新启用后按剩余次数恢复重试
            NotificationChannel.enabled.is_(True),
        )
        .order_by(NotificationDelivery.created_at.asc())
        .limit(limit)
        .all()
    )
    sent = failed = 0
    for delivery, channel, message in rows:
        delivery.attempts = int(delivery.attempts or 0) + 1
        body = message.body_md or ""
        try:
            _send_via_apprise(
                channel_url(channel),
                title=message.title[:500],
                body=(body or "")[:BODY_TRUNCATE_CHARS],
            )
            delivery.status = "sent"
            delivery.sent_at = _now()
            delivery.last_error = ""
            channel.last_status = "sent"
            channel.last_error = ""
            channel.last_sent_at = _now()
            sent += 1
        except Exception as exc:  # noqa: BLE001 — 单渠道失败不阻断批次
            delivery.last_error = _safe_error(str(exc))
            if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
                delivery.status = "failed"
                failed += 1
            channel.last_status = "failed"
            channel.last_error = delivery.last_error
        db.commit()
    return {"sent": sent, "failed": failed, "scanned": len(rows)}


def test_channel(db: Session, channel: NotificationChannel) -> dict[str, Any]:
    """渠道测试直发：固定文案、不落消息表、结果即时返回并更新渠道最近状态。"""
    try:
        _send_via_apprise(
            channel_url(channel),
            title="【消息通知】渠道测试",
            body="这是一条渠道测试消息。收到即说明渠道配置可用。",
        )
        channel.last_status = "sent"
        channel.last_error = ""
        channel.last_sent_at = _now()
        db.commit()
        return {"ok": True, "message": "测试消息已发送"}
    except Exception as exc:  # noqa: BLE001 — 测试失败要回传给界面
        error = _safe_error(str(exc))
        channel.last_status = "failed"
        channel.last_error = error
        db.commit()
        return {"ok": False, "message": error}


# ── 模板化渠道（L2）+ 平台 SMTP ──────────────────────────────

import json as _json

from app.notifications.channel_templates import (
    TEMPLATES,
    mask_field,
    template_display as _template_display,
)


def list_template_definitions() -> list[dict[str, Any]]:
    """前端「新建渠道」的类型选择与动态表单定义。"""
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "hint": f.hint,
                    "required": f.required,
                    "secret": f.secret,
                    "placeholder": f.placeholder,
                }
                for f in t.fields
            ],
        }
        for t in TEMPLATES.values()
    ]


def _smtp_row(db: Session):
    from app.notifications.models import NotificationSmtpSettings

    row = db.query(NotificationSmtpSettings).filter(
        NotificationSmtpSettings.id == "default"
    ).first()
    if row is None:
        row = NotificationSmtpSettings(id="default")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _smtp_dict(db: Session) -> dict[str, Any]:
    row = _smtp_row(db)
    password = ""
    if row.password_encrypted:
        try:
            password = fernet_decrypt(row.password_encrypted)
        except Exception:  # noqa: BLE001 — 密文损坏按未配置处理
            password = ""
    return {
        "host": row.host or "",
        "port": int(row.port or 465),
        "username": row.username or "",
        "password": password,
        "sender": row.sender or "",
        "use_tls": bool(row.use_tls),
    }


def smtp_out(db: Session) -> dict[str, Any]:
    row = _smtp_row(db)
    data = _smtp_dict(db)
    configured = bool(data["host"] and data["username"] and data["password"])
    return {
        "host": data["host"],
        "port": data["port"],
        "username": data["username"],
        "sender": data["sender"],
        "useTls": data["use_tls"],
        "configured": configured,
        "hasPassword": bool(data["password"]),
    }


def update_smtp(
    db: Session,
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    sender: str,
    use_tls: bool,
) -> dict[str, Any]:
    if not (host or "").strip():
        raise HTTPException(422, "SMTP 主机不能为空")
    row = _smtp_row(db)
    row.host = host.strip()[:200]
    row.port = int(port or 465)
    row.username = (username or "").strip()[:200]
    if password is not None and password != "":
        row.password_encrypted = fernet_encrypt(password)
    row.sender = (sender or "").strip()[:200]
    row.use_tls = bool(use_tls)
    db.commit()
    return smtp_out(db)


def test_smtp(db: Session, to: str) -> dict[str, Any]:
    """用平台 SMTP 向指定邮箱发测试邮件（复用 apprise mailtos 链路）。"""
    to = (to or "").strip()
    if not to or "@" not in to:
        raise HTTPException(422, "测试收件邮箱无效")
    data = _smtp_dict(db)
    if not (data["host"] and data["username"] and data["password"]):
        raise HTTPException(422, "平台发件邮箱未配置完整（主机/账号/密码）")
    from app.notifications.channel_templates import _email

    url = _email({"recipients": to}, data)
    try:
        _send_via_apprise(url, title="【消息通知】发件邮箱测试", body="这是一封测试邮件，收到即说明平台发件配置可用。")
        return {"ok": True, "message": f"测试邮件已发送至 {to}"}
    except Exception as exc:  # noqa: BLE001 — 失败原因要回传给界面
        return {"ok": False, "message": _safe_error(str(exc))}


def _validate_template_params(template_id: str, params: dict[str, Any]) -> dict[str, str]:
    template = TEMPLATES.get(template_id)
    if template is None:
        raise HTTPException(422, f"未知渠道模板：{template_id}")
    if not isinstance(params, dict):
        raise HTTPException(422, "params 必须是对象")
    clean: dict[str, str] = {}
    for f in template.fields:
        value = params.get(f.key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if f.required:
                raise HTTPException(422, f"{f.label} 不能为空")
            continue
        if not isinstance(value, str):
            raise HTTPException(422, f"{f.label} 必须是字符串")
        clean[f.key] = value.strip()
    return clean


def build_template_url(db: Session, template_id: str, params: dict[str, str]) -> str:
    """模板字段 + 平台设置 → apprise URL。"""
    if template_id == "email":
        smtp = _smtp_dict(db)
        if not (smtp["host"] and smtp["username"] and smtp["password"]):
            raise HTTPException(422, "邮件渠道需要先在「渠道配置 → 平台发件设置」配置 SMTP")
        from app.notifications.channel_templates import _email

        return _validate_apprise_url(_email(params, smtp))
    if template_id == "custom":
        return _validate_apprise_url(params.get("url", ""))
    template = TEMPLATES[template_id]
    return _validate_apprise_url(template.build(params))


def create_templated_channel(
    db: Session,
    *,
    name: str,
    note: str | None,
    template_id: str,
    params: dict[str, Any],
    user=None,
) -> NotificationChannel:
    clean = _validate_template_params(template_id, params)
    url = build_template_url(db, template_id, clean)
    name = (name or "").strip()
    if not name:
        raise HTTPException(422, "name 不能为空")
    if len(name) > 200:
        raise HTTPException(422, "name 过长（上限 200 字符）")
    if db.query(NotificationChannel).filter(NotificationChannel.name == name).first():
        raise HTTPException(409, f"同名渠道已存在：{name}")
    row = NotificationChannel(
        name=name,
        apprise_url_encrypted=fernet_encrypt(url),
        template=template_id,
        params_encrypted=fernet_encrypt(_json.dumps(clean, ensure_ascii=False)),
        note=((note or "").strip()[:500] or None),
        created_by=getattr(user, "id", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_channel_params(
    db: Session, channel: NotificationChannel, *, params: dict[str, Any]
) -> NotificationChannel:
    """参数更新为合并语义：提交空/缺省的字段保留原值（敏感字段“留空保持不变”）。"""
    template_id = channel.template or "custom"
    template = TEMPLATES[template_id]
    merged = channel_params(channel)
    submitted = {
        key: value
        for key, value in (params or {}).items()
        if isinstance(value, str) and value.strip()
    }
    merged.update(submitted)
    clean = _validate_template_params(template_id, merged)
    channel.apprise_url_encrypted = fernet_encrypt(build_template_url(db, template_id, clean))
    channel.params_encrypted = fernet_encrypt(_json.dumps(clean, ensure_ascii=False))
    db.commit()
    db.refresh(channel)
    return channel


def channel_params(channel: NotificationChannel) -> dict[str, str]:
    if not channel.params_encrypted:
        return {}
    try:
        data = _json.loads(fernet_decrypt(channel.params_encrypted))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — 密文损坏按空参数处理（编辑=重填）
        return {}


def templated_channel_out(db: Session, channel: NotificationChannel) -> dict[str, Any]:
    """模板化渠道序列化：类型名 + 脱敏字段展示 + 编辑用字段表。"""
    base = channel_out(channel)
    template = TEMPLATES.get(channel.template or "")
    if template is None:
        base.update({"template": "custom", "templateName": TEMPLATES["custom"].name, "display": base.get("urlMasked", ""), "fields": []})
        return base
    params = channel_params(channel)
    base.update(
        {
            "template": template.id,
            "templateName": template.name,
            "display": _template_display(template.id, params),
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "hint": f.hint,
                    "required": f.required,
                    "secret": f.secret,
                    "placeholder": f.placeholder,
                    # 编辑预填：非敏感原值；敏感脱敏（留空提交=保持不变由前端控制）
                    "value": mask_field(params.get(f.key, "")) if f.secret else params.get(f.key, ""),
                }
                for f in template.fields
            ],
        }
    )
    return base
