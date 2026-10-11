"""
消息通知 — service 层（创建、处置状态、附件与读侧查询集中于此）

权限契约：整个域仅管理员可用（router 层 require_admin）；消息本身是平台级
事实，各管理员对同一消息维护彼此独立的已读/标记/归档状态。

语义要点：
  - 查看详情即自动标为已读（可再标回未读）；
  - 归档/标记互不影响，全部可逆；
  - 单条删除为物理删除（连带附件文件与状态行），用于误投敏感消息处置。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import aiofiles
from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.notifications.models import (
    NOTIFICATION_PRIORITIES,
    NOTIFICATION_SOURCE_TYPES,
    NotificationAttachment,
    NotificationDelivery,
    NotificationChannel,
    NotificationIngestKey,
    NotificationMessage,
    NotificationMessageState,
    PRIORITY_NORMAL,
    SOURCE_TYPE_MANUAL,
)

MAX_BODY_BYTES = 512 * 1024
NOTIFICATION_TABS = ("all", "unread", "starred", "archived")
PREVIEW_CHARS = 200


def _now() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds") + "Z"


# ── 查找与校验 ─────────────────────────────────────────────────


def require_message(db: Session, message_id: str) -> NotificationMessage:
    message = (
        db.query(NotificationMessage)
        .filter(NotificationMessage.id == message_id)
        .first()
    )
    if message is None:
        raise HTTPException(404, "消息不存在")
    return message


def _validate_payload(*, title: str, body_md: str, priority: str, source_type: str) -> None:
    if not (title or "").strip():
        raise HTTPException(422, "title 不能为空")
    if len((title or "").encode("utf-8")) > 900:
        raise HTTPException(422, "title 过长（上限 300 个字符）")
    if len((body_md or "").encode("utf-8")) > MAX_BODY_BYTES:
        raise HTTPException(413, f"正文超过大小限制 {MAX_BODY_BYTES // 1024} KiB")
    if priority not in NOTIFICATION_PRIORITIES:
        raise HTTPException(422, f"priority 仅支持 {'、'.join(NOTIFICATION_PRIORITIES)}")
    if source_type not in NOTIFICATION_SOURCE_TYPES:
        raise HTTPException(422, f"sourceType 仅支持 {'、'.join(NOTIFICATION_SOURCE_TYPES)}")


# ── 写侧：创建 / 内部发布 ─────────────────────────────────────


def create_message_ex(
    db: Session,
    *,
    title: str,
    body_md: str = "",
    priority: str = PRIORITY_NORMAL,
    source_system: str = "platform",
    source_type: str = SOURCE_TYPE_MANUAL,
    event_id: str | None = None,
    ingest_key_id: str | None = None,
    user=None,
) -> tuple[NotificationMessage, bool]:
    """创建一条通知消息，返回 (消息, 是否新建)。

    (source_system, event_id) 幂等：同键重放返回既有消息且 created=False；
    并发撞唯一约束时回滚重查，保证“重放无副作用、并发不 500”。
    """
    _validate_payload(title=title, body_md=body_md, priority=priority, source_type=source_type)
    normalized_source = (source_system or "platform").strip()[:80] or "platform"
    normalized_event = (event_id or "").strip()[:255] or None

    if normalized_event:
        existing = _find_by_source_event(db, normalized_source, normalized_event)
        if existing is not None:
            return existing, False

    message = NotificationMessage(
        event_id=normalized_event,
        source_system=normalized_source,
        source_type=source_type,
        title=title.strip(),
        body_md=body_md or "",
        priority=priority,
        created_by=getattr(user, "id", None),
        ingest_key_id=ingest_key_id,
    )
    db.add(message)
    try:
        # 消息与“到达即转发”的投递单在同一个事务内提交：
        # 二次提交的窗口里进程被杀会让消息入库而投递单永久丢失，
        # 且幂等重放走 existing 提前返回，无法自愈。
        db.flush()
        fan_out_deliveries(db, message)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_by_source_event(db, normalized_source, normalized_event or "")
        if existing is None:
            raise
        return existing, False
    db.refresh(message)
    return message, True


def create_message(db: Session, **kwargs) -> NotificationMessage:
    message, _created = create_message_ex(db, **kwargs)
    return message


def _find_by_source_event(
    db: Session, source_system: str, event_id: str
) -> NotificationMessage | None:
    return (
        db.query(NotificationMessage)
        .filter(
            NotificationMessage.source_system == source_system,
            NotificationMessage.event_id == event_id,
        )
        .first()
    )


def publish_notification(
    db: Session,
    *,
    title: str,
    body_md: str = "",
    priority: str = PRIORITY_NORMAL,
    source_system: str = "platform",
    event_id: str | None = None,
) -> NotificationMessage:
    """平台内部事件的发布入口（生产者接入点，M1 起可用）。"""
    return create_message(
        db,
        title=title,
        body_md=body_md,
        priority=priority,
        source_system=source_system,
        source_type="internal",
        event_id=event_id,
    )


def delete_message(db: Session, message_id: str) -> None:
    message = require_message(db, message_id)
    attachments = (
        db.query(NotificationAttachment)
        .filter(NotificationAttachment.message_id == message.id)
        .all()
    )
    file_paths = [att.file_path for att in attachments]
    # SQLite（测试/开发）默认不强制外键：显式删子行，不依赖 DB 级 CASCADE
    (
        db.query(NotificationMessageState)
        .filter(NotificationMessageState.message_id == message.id)
        .delete(synchronize_session=False)
    )
    (
        db.query(NotificationAttachment)
        .filter(NotificationAttachment.message_id == message.id)
        .delete(synchronize_session=False)
    )
    db.delete(message)
    db.commit()
    # DB 提交成功后再清理落盘文件与空目录（尽力而为，失败不影响业务事实）
    for path in file_paths:
        _remove_file(path)
    message_dir = os.path.join(settings.uploads_dir, "notifications", message.id)
    try:
        if os.path.isdir(message_dir):
            os.rmdir(message_dir)
    except OSError:
        pass


# ── 处置状态（已读 / 标记 / 归档）──────────────────────────────


def _get_or_create_state(
    db: Session, *, message_id: str, user_id: str
) -> NotificationMessageState:
    state = _find_state(db, message_id=message_id, user_id=user_id)
    if state is None:
        state = NotificationMessageState(message_id=message_id, user_id=user_id)
        db.add(state)
        try:
            db.flush()
        except IntegrityError:
            # 并发首访同一消息：另一请求已建状态行，回滚重查复用
            db.rollback()
            state = _find_state(db, message_id=message_id, user_id=user_id)
            if state is None:
                raise
    return state


def get_message_detail(
    db: Session, *, user_id: str, message_id: str, mark_read: bool = True
) -> dict[str, Any]:
    """详情（含附件）。查看即自动标已读；已读不影响标记/归档。"""
    message = require_message(db, message_id)
    state = _find_state(db, message_id=message.id, user_id=user_id)
    if mark_read and (state is None or not state.is_read):
        state = _get_or_create_state(db, message_id=message.id, user_id=user_id)
        state.is_read = True
        state.read_at = _now()
        db.commit()
        db.refresh(state)
    attachments = _attachments_of(db, message.id)
    return _message_dict(message, state, attachments=attachments, with_body=True)


def update_state(
    db: Session, *, user_id: str, message_id: str, fields: dict[str, bool | None]
) -> dict[str, Any]:
    if not any(value is not None for value in fields.values()):
        raise HTTPException(422, "至少携带 isRead / isStarred / isArchived 中的一个字段")
    message = require_message(db, message_id)
    state = _get_or_create_state(db, message_id=message.id, user_id=user_id)
    now = _now()
    if fields.get("isRead") is not None:
        state.is_read = bool(fields["isRead"])
        state.read_at = now if state.is_read else None
    if fields.get("isStarred") is not None:
        state.is_starred = bool(fields["isStarred"])
        state.starred_at = now if state.is_starred else None
    if fields.get("isArchived") is not None:
        state.is_archived = bool(fields["isArchived"])
        state.archived_at = now if state.is_archived else None
    db.commit()
    db.refresh(state)
    attachments = _attachments_of(db, message.id)
    return _message_dict(message, state, attachments=attachments, with_body=True)


def mark_all_read(db: Session, *, user_id: str) -> int:
    """全部未读 → 已读（含已归档但未读的；不影响标记状态）。批量 upsert。"""
    unread_ids = [
        row[0]
        for row in db.query(NotificationMessage.id)
        .outerjoin(
            NotificationMessageState,
            _state_join_condition(user_id),
        )
        .filter(_read_flag().is_(False))
        .all()
    ]
    if not unread_ids:
        return 0
    existing = {
        state.message_id: state
        for state in db.query(NotificationMessageState).filter(
            NotificationMessageState.message_id.in_(unread_ids),
            NotificationMessageState.user_id == user_id,
        ).all()
    }
    now = _now()
    for message_id in unread_ids:
        state = existing.get(message_id)
        if state is None:
            db.add(
                NotificationMessageState(
                    message_id=message_id, user_id=user_id, is_read=True, read_at=now
                )
            )
        else:
            state.is_read = True
            state.read_at = now
    try:
        db.commit()
    except IntegrityError:
        # 并发 read-all/首访撞状态行唯一约束：退化为逐条 upsert（幂等）
        db.rollback()
        for message_id in unread_ids:
            state = _get_or_create_state(db, message_id=message_id, user_id=user_id)
            state.is_read = True
            state.read_at = _now()
        db.commit()
    return len(unread_ids)


# ── 读侧：列表 / 汇总 ─────────────────────────────────────────


def fan_out_deliveries(db: Session, message: NotificationMessage) -> int:
    """消息新建即为所有启用渠道生成 pending 投递单（幂等：唯一约束跳过既有对）。

    与 create_message_ex 同事务提交：保证“消息入库 ⇄ 投递单生成”原子。
    """
    channels = db.query(NotificationChannel).filter(NotificationChannel.enabled.is_(True)).all()
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
        db.add(NotificationDelivery(message_id=message.id, channel_id=channel.id))
        created += 1
    if created:
        db.flush()
    return created


def list_messages(
    db: Session,
    *,
    user_id: str,
    tab: str = "all",
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    query = (
        db.query(NotificationMessage, NotificationMessageState)
        .outerjoin(NotificationMessageState, _state_join_condition(user_id))
    )
    if tab == "all":
        query = query.filter(_archived_flag().is_(False))
    elif tab == "unread":
        query = query.filter(_archived_flag().is_(False), _read_flag().is_(False))
    elif tab == "starred":
        query = query.filter(_archived_flag().is_(False), _starred_flag().is_(True))
    elif tab == "archived":
        query = query.filter(_archived_flag().is_(True))
    else:
        raise HTTPException(400, "tab 仅支持 all、unread、starred、archived")

    if cursor:
        created_at, message_id = _decode_cursor(cursor)
        query = query.filter(
            or_(
                NotificationMessage.created_at < created_at,
                and_(
                    NotificationMessage.created_at == created_at,
                    NotificationMessage.id < message_id,
                ),
            )
        )
    rows = (
        query.order_by(NotificationMessage.created_at.desc(), NotificationMessage.id.desc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = None
    if has_more and visible:
        last_message, _ = visible[-1]
        next_cursor = _encode_cursor(last_message)

    counts = _attachment_counts(db, [message.id for message, _ in visible])
    return {
        "items": [
            _message_dict(
                message,
                state,
                attachment_count=counts.get(message.id, 0),
                with_body=False,
            )
            for message, state in visible
        ],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


def notification_summary(db: Session, *, user_id: str) -> dict[str, int]:
    base = (
        db.query(func.count(NotificationMessage.id))
        .outerjoin(NotificationMessageState, _state_join_condition(user_id))
    )
    unread = base.filter(_archived_flag().is_(False), _read_flag().is_(False)).scalar() or 0
    starred = base.filter(_archived_flag().is_(False), _starred_flag().is_(True)).scalar() or 0
    archived = base.filter(_archived_flag().is_(True)).scalar() or 0
    total = base.filter(_archived_flag().is_(False)).scalar() or 0
    return {
        "unreadCount": unread,
        "starredCount": starred,
        "archivedCount": archived,
        "totalCount": total,
    }


# ── 附件（安全落盘 + sha256，与工单/事件附件同构）──────────────


async def add_attachment(
    db: Session,
    message: NotificationMessage,
    *,
    upload: UploadFile,
    user=None,
    uploaded_by: str | None = None,
) -> NotificationAttachment:
    filename = upload.filename or ""
    mime = upload.content_type
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    configured_extensions = settings.notification_attachment_extensions
    allowed = {e.strip().lower() for e in configured_extensions.split(",") if e.strip()}
    if ext and allowed and "*" not in allowed and ext not in allowed:
        raise HTTPException(
            400, f"不支持的消息附件类型: .{ext}（允许: {configured_extensions}）"
        )

    att_id = str(uuid.uuid4())
    upload_dir = os.path.join(settings.uploads_dir, "notifications", message.id)
    os.makedirs(upload_dir, exist_ok=True)
    ext_suffix = os.path.splitext(filename or "")[1]
    save_path = os.path.join(upload_dir, f"{att_id}{ext_suffix}")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    file_size = 0
    digest = hashlib.sha256()
    try:
        async with aiofiles.open(save_path, "wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                file_size += len(chunk)
                if file_size > max_bytes:
                    raise HTTPException(413, f"文件超过大小限制 {settings.max_upload_mb}MB")
                digest.update(chunk)
                await destination.write(chunk)
    except Exception:
        _remove_file(save_path)
        raise

    att = NotificationAttachment(
        id=att_id,
        message_id=message.id,
        filename=filename or att_id,
        file_path=save_path,
        file_size=file_size,
        mime_type=mime,
        sha256=digest.hexdigest(),
        uploaded_by=uploaded_by or getattr(user, "id", None),
    )
    try:
        db.add(att)
        db.commit()
        db.refresh(att)
    except Exception:
        db.rollback()
        _remove_file(save_path)
        raise
    return att


def attachment_for_download(
    db: Session, message: NotificationMessage, att_id: str
) -> NotificationAttachment:
    att = (
        db.query(NotificationAttachment)
        .filter(
            NotificationAttachment.id == att_id,
            NotificationAttachment.message_id == message.id,
        )
        .first()
    )
    if att is None:
        raise HTTPException(404, "附件不存在")
    if not att.file_path or not os.path.exists(att.file_path):
        # 与事件附件同语义：行存在但文件丢失属于存储故障，410 而非 500
        raise HTTPException(410, "附件文件已丢失")
    return att


def _remove_file(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ── 内部工具：状态行 / 游标 / 序列化 ───────────────────────────


def _state_join_condition(user_id: str):
    return and_(
        NotificationMessageState.message_id == NotificationMessage.id,
        NotificationMessageState.user_id == user_id,
    )


def _read_flag():
    return func.coalesce(NotificationMessageState.is_read, False)


def _starred_flag():
    return func.coalesce(NotificationMessageState.is_starred, False)


def _archived_flag():
    return func.coalesce(NotificationMessageState.is_archived, False)


def _find_state(db: Session, *, message_id: str, user_id: str) -> NotificationMessageState | None:
    return (
        db.query(NotificationMessageState)
        .filter(
            NotificationMessageState.message_id == message_id,
            NotificationMessageState.user_id == user_id,
        )
        .first()
    )


def _attachments_of(db: Session, message_id: str) -> list[NotificationAttachment]:
    return (
        db.query(NotificationAttachment)
        .filter(NotificationAttachment.message_id == message_id)
        .order_by(NotificationAttachment.created_at.asc())
        .all()
    )


def _attachment_counts(db: Session, message_ids: list[str]) -> dict[str, int]:
    if not message_ids:
        return {}
    rows = (
        db.query(
            NotificationAttachment.message_id, func.count(NotificationAttachment.id)
        )
        .filter(NotificationAttachment.message_id.in_(message_ids))
        .group_by(NotificationAttachment.message_id)
        .all()
    )
    return {message_id: count for message_id, count in rows}


def _encode_cursor(message: NotificationMessage) -> str:
    # 游标必须保留完整微秒精度：截断到毫秒会在同毫秒消息间静默丢行
    payload = json.dumps(
        {"at": message.created_at.isoformat(), "id": message.id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        at = datetime.fromisoformat(str(payload["at"]).replace("Z", "+00:00"))
        if at.tzinfo is not None:
            at = at.astimezone(timezone.utc).replace(tzinfo=None)
        return at, str(payload["id"])
    except Exception as exc:
        raise HTTPException(400, "无效的消息列表游标") from exc


def attachment_out(att: NotificationAttachment) -> dict[str, Any]:
    return {
        "id": att.id,
        "filename": att.filename,
        "fileSize": att.file_size,
        "mimeType": att.mime_type,
        "sha256": att.sha256,
        "createdAt": _iso(att.created_at),
    }


def _message_dict(
    message: NotificationMessage,
    state: NotificationMessageState | None,
    *,
    attachments: list[NotificationAttachment] | None = None,
    attachment_count: int | None = None,
    with_body: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": message.id,
        "eventId": message.event_id,
        "sourceSystem": message.source_system,
        "sourceType": message.source_type,
        "title": message.title,
        "priority": message.priority,
        "isRead": bool(state and state.is_read),
        "isStarred": bool(state and state.is_starred),
        "isArchived": bool(state and state.is_archived),
        "readAt": _iso(state.read_at) if state else None,
        "starredAt": _iso(state.starred_at) if state else None,
        "archivedAt": _iso(state.archived_at) if state else None,
        "createdAt": _iso(message.created_at),
        "updatedAt": _iso(message.updated_at),
    }
    if with_body:
        out["body"] = message.body_md or ""
    else:
        out["bodyPreview"] = (message.body_md or "").strip().replace("\n", " ")[:PREVIEW_CHARS]
    if attachment_count is not None:
        out["attachmentCount"] = attachment_count
    if attachments is not None:
        out["attachments"] = [attachment_out(att) for att in attachments]
    return out


# ── 对外投递：密钥管理 + ingest 入口（M2）─────────────────────


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def mint_ingest_key(
    db: Session, name: str, allowed_source_system: str | None, user=None
) -> tuple[NotificationIngestKey, str]:
    """生成对外投递密钥。返回 (记录, 明文全串)；明文只在此刻可见，之后仅存 sha256。"""
    if not (name or "").strip():
        raise HTTPException(422, "name 不能为空")
    tag = secrets.token_hex(3)
    secret = secrets.token_urlsafe(32)
    key_prefix = f"ob_notif_{tag}"
    plaintext = f"{key_prefix}_{secret}"
    row = NotificationIngestKey(
        name=name.strip()[:200],
        key_prefix=key_prefix,
        key_hash=hash_key(plaintext),
        enabled=True,
        allowed_source_system=(allowed_source_system or "").strip()[:200] or None,
        created_by=getattr(user, "id", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, plaintext


def verify_ingest_key(db: Session, plaintext: str) -> NotificationIngestKey | None:
    if not plaintext:
        return None
    return (
        db.query(NotificationIngestKey)
        .filter(
            NotificationIngestKey.key_hash == hash_key(plaintext),
            NotificationIngestKey.enabled.is_(True),
            NotificationIngestKey.revoked_at.is_(None),
        )
        .first()
    )


def revoke_ingest_key(db: Session, row: NotificationIngestKey) -> None:
    row.enabled = False
    row.revoked_at = _now()
    db.commit()


def ingest_key_out(key: NotificationIngestKey, *, plaintext: str | None = None) -> dict[str, Any]:
    out = {
        "id": key.id,
        "name": key.name,
        "keyPrefix": key.key_prefix,
        "enabled": key.enabled,
        "allowedSourceSystem": key.allowed_source_system,
        "createdAt": _iso(key.created_at),
        "lastUsedAt": _iso(key.last_used_at),
        "revokedAt": _iso(key.revoked_at),
    }
    if plaintext is not None:
        # 明文仅创建响应一次性返回；列表/吊销响应永不携带
        out["plaintextKey"] = plaintext
    return out


def _ingest_str_field(body: dict[str, Any], field: str) -> str | None:
    """取字符串字段；dict/list/数字等非字符串值一律 422（不做隐式 repr）。"""
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, f"{field} 必须是字符串")
    return value.strip()


def ingest_message(db: Session, body: dict[str, Any], key: NotificationIngestKey) -> dict[str, Any]:
    """外部系统经 X-API-Key 投递一条消息。

    sourceSystem 优先取请求体：缺省回退密钥作用域（无作用域则回退密钥名），
    显式声明且与密钥作用域不一致时 403。长度超限直接 422（不静默截断，
    避免不同长 eventId 截断后撞成同一幂等键）。(source_system, eventId) 幂等，
    idempotent = !created 由创建路径统一判定（含并发竞态）。
    """
    source_system = _ingest_str_field(body, "sourceSystem")
    if source_system and key.allowed_source_system and source_system != key.allowed_source_system:
        raise HTTPException(403, f"该密钥不允许以来源系统 {source_system!r} 投递")
    if not source_system:
        source_system = (key.allowed_source_system or key.name).strip()
    if not source_system:
        raise HTTPException(422, "sourceSystem 不能为空")
    if len(source_system) > 80:
        raise HTTPException(422, "sourceSystem 过长（上限 80 个字符）")

    event_id = _ingest_str_field(body, "eventId")
    if event_id:
        if len(event_id) > 255:
            raise HTTPException(422, "eventId 过长（上限 255 个字符）")
    else:
        event_id = None

    title = _ingest_str_field(body, "title")
    if not title:
        raise HTTPException(422, "title 不能为空")
    body_md = _ingest_str_field(body, "body") or ""
    priority = _ingest_str_field(body, "priority") or PRIORITY_NORMAL

    message, created = create_message_ex(
        db,
        title=title,
        body_md=body_md,
        priority=priority,
        source_system=source_system,
        source_type="ingest",
        event_id=event_id,
        ingest_key_id=key.id,
    )
    # 扇出已在 create_message_ex 内与消息同事务完成
    return {
        "idempotent": not created,
        "message": _message_dict(
            message, None, attachments=_attachments_of(db, message.id), with_body=True
        ),
    }


def require_ingest_message(
    db: Session, message_id: str, key: NotificationIngestKey
) -> NotificationMessage:
    """附件投递目标校验：仅 ingest 消息可经密钥追加，且须为该密钥投递的消息
    （历史消息无归属列值时退化为作用域比对）。"""
    message = require_message(db, message_id)
    if message.source_type != "ingest":
        raise HTTPException(403, "仅外部投递的消息允许经投递接口追加附件")
    if message.ingest_key_id is not None:
        if message.ingest_key_id != key.id:
            raise HTTPException(403, "该密钥无权向其它来源投递的消息追加附件")
    elif key.allowed_source_system and message.source_system != key.allowed_source_system:
        raise HTTPException(403, "该密钥无权向此来源系统的消息追加附件")
    return message
