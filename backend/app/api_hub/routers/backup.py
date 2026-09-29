"""数据备份与还原：导出 / 导入接口清单（JSON 格式）。

备份内容只含接口清单（interfaces），不含调用历史（runs）、设置（settings）、
登录态等本地敏感数据 —— 符合「备份接口清单，其它的都不用」。

还原时以「接口名 + 请求方法 + URL」三元组作为自然键去重：已存在的接口跳过，
不重复导入，也不覆盖已有数据。接口自带的 group_name 直接写入，分组自然出现。
"""
import json
import re
from datetime import datetime, timezone
from typing import List
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, ValidationError

from app.auth.models import User
from app.deps import get_current_user

from .. import db
from ..interface_contracts import InterfaceIn
from ..interface_service import (
    _check_group_name,
    _dump_kv,
    _is_admin,
    _validate_proxy_publish,
)
from ..publication import is_sensitive_name

router = APIRouter(prefix="/backup", tags=["api-hub-backup"])

# 备份文件的标识，还原时用于校验来源
BACKUP_APP = "API-Hub"
BACKUP_VERSION = 7
# 展示/导出脱敏口径：与前端 interfaceUxHelpers 的 SENSITIVE_FIELD_RE 同源（publication.is_sensitive_name）；
# 数据管家的写入拒绝名单是另一职责，不并入

# 可移植的接口字段（不含 id、created_at、updated_at、sort_order 等本地/派生字段）
_IFACE_FIELDS = (
    "name", "description", "group_name", "method", "url", "query_params", "headers",
    "body_type", "body_content", "file_fields", "mcp_enabled", "open_enabled",
    "http_enabled", "proxy_slug", "proxy_query_keys", "proxy_header_keys",
    "proxy_body_enabled", "proxy_body_keys", "parameter_schema",
)


class ExportIn(BaseModel):
    name: str = ""
    mode: str = "full"            # full | partial
    ids: List[int] = Field(default_factory=list)
    include_sensitive: bool = False


class ImportIn(BaseModel):
    app: str = ""
    version: int = 0
    name: str = ""
    exported_at: str = ""
    mode: str = ""
    interfaces: List[dict] = Field(default_factory=list)

def _safe_url(url: str) -> str:
    parsed = urlsplit(url or "")
    query = [
        (key, "" if is_sensitive_name(key) else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment)
    )


def _natural_key(name: str, method: str, url: str) -> tuple[str, str, str]:
    """用脱敏后的 URL 比较，避免安全备份因清空令牌值而重复导入。"""
    return ((name or "").strip(), (method or "").upper(), _safe_url(url or ""))


def _row_to_portable(row, *, include_sensitive: bool) -> dict:
    """把一行接口记录转为可移植的纯数据。"""
    out = {}
    for f in _IFACE_FIELDS:
        v = row[f]
        if f in (
            "query_params", "headers", "proxy_query_keys", "proxy_header_keys",
            "proxy_body_keys", "file_fields", "parameter_schema",
        ):
            try:
                v = json.loads(v) if v else []
            except (json.JSONDecodeError, TypeError):
                v = []
        elif f in (
            "mcp_enabled", "open_enabled", "http_enabled",
            "proxy_body_enabled",
        ):
            v = bool(v)
        out[f] = v
    if not include_sensitive:
        query_params = out.get("query_params") or []
        out["query_params"] = [
            {
                "key": item.get("key", ""),
                "value": "" if is_sensitive_name(item.get("key", "")) else item.get("value", ""),
            }
            for item in query_params
            if isinstance(item, dict)
        ]
        out["url"] = _safe_url(out.get("url") or "")
        omitted = bool(out.get("headers") or out.get("body_content"))
        out["headers"] = []
        out["body_content"] = ""
        out["sensitive_values_omitted"] = omitted
    return out


def _safe_filename(name: str) -> str:
    """把备份名变成 ASCII 安全的文件名（HTTP 头不能直接含非 ASCII 字符）。"""
    name = name.strip() or "Backup"
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    return name.strip("-") or "Backup"


@router.post("/export")
def export_backup(body: ExportIn, current_user: User = Depends(get_current_user)):
    """导出接口清单为 JSON 文件（触发浏览器下载）。

    非管理员只能导出自己创建的接口（部分模式下指定他人的 ID 同样被
    过滤掉）；管理员导出全量。与接口列表的行级归属口径一致。
    """
    mode = "partial" if body.mode == "partial" else "full"
    is_admin = _is_admin(current_user)
    with db.get_conn() as conn:
        if mode == "partial":
            if not body.ids:
                raise HTTPException(
                    status_code=400,
                    detail="部分备份模式下必须提供接口 ID 列表",
                )
            placeholders = ",".join("?" * len(body.ids))
            sql = f"SELECT * FROM interfaces WHERE id IN ({placeholders})"
            params: list = list(body.ids)
            if not is_admin:
                sql += " AND created_by = ?"
                params.append(current_user.id)
            rows = conn.execute(
                sql + " ORDER BY group_name, sort_order, id",
                params,
            ).fetchall()
        elif is_admin:
            rows = conn.execute(
                "SELECT * FROM interfaces ORDER BY group_name, sort_order, id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM interfaces WHERE created_by = ? "
                "ORDER BY group_name, sort_order, id",
                (current_user.id,),
            ).fetchall()

    payload = {
        "app": BACKUP_APP,
        "version": BACKUP_VERSION,
        "name": body.name.strip() or "Backup",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "includes_sensitive_values": body.include_sensitive,
        "interface_count": len(rows),
        "interfaces": [
            _row_to_portable(r, include_sensitive=body.include_sensitive)
            for r in rows
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    # RFC 5987：filename 给旧浏览器兜底（ASCII），filename* 给现代浏览器用原名（UTF-8）
    safe_name = _safe_filename(body.name)
    utf8_name = quote(body.name.strip() or "Backup") + ".json"
    return Response(
        content=data,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{safe_name}.json"; filename*=UTF-8\'\'{utf8_name}'
        },
    )


@router.post("/import")
def import_backup(body: ImportIn, current_user: User = Depends(get_current_user)):
    """导入备份包：合并到现有接口清单，重复的（名+方法+URL 一致）跳过。

    导入的接口归属导入者（created_by=导入者），否则非管理员导入后在自己
    的列表里看不到。自然键去重只对比导入者可见的行：非管理员不会因他人
    的同名接口而被计为重复，也不会借此探测他人接口是否存在。
    """
    # ── 来源校验 ──
    if body.app != BACKUP_APP:
        raise HTTPException(
            status_code=400,
            detail=f"备份文件来源不匹配：期望 app={BACKUP_APP}，实际 app={body.app or '(空)'}",
        )
    if body.version > BACKUP_VERSION:
        raise HTTPException(
            status_code=400,
            detail=f"备份文件版本过高（{body.version}），当前平台最高支持 v{BACKUP_VERSION}",
        )

    items = body.interfaces
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="备份文件格式不正确：interfaces 应为数组")

    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    skipped = 0
    with db.get_conn() as conn:
        # 预加载导入者可见接口的自然键，做内存去重（接口数通常很少，无需复杂查询）
        if _is_admin(current_user):
            existing_rows = conn.execute(
                "SELECT name, method, url FROM interfaces"
            ).fetchall()
        else:
            existing_rows = conn.execute(
                "SELECT name, method, url FROM interfaces WHERE created_by = ?",
                (current_user.id,),
            ).fetchall()
        existing = {
            _natural_key(r["name"], r["method"], r["url"]) for r in existing_rows
        }

        for it in items:
            try:
                candidate = InterfaceIn(
                    **{
                        **it,
                        "mcp_enabled": False,
                        "open_enabled": False,
                        "http_enabled": False,
                    }
                )
            except ValidationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"备份中的接口配置无效：{exc.errors()[0]['msg']}",
                ) from exc
            name = candidate.name
            method = candidate.method
            url = candidate.url
            key = _natural_key(name, method, url)
            if key in existing:
                skipped += 1
                continue

            _check_group_name(candidate.group_name)
            (
                proxy_slug,
                proxy_query_keys,
                proxy_header_keys,
                proxy_body_keys,
            ) = _validate_proxy_publish(conn, candidate)

            conn.execute(
                "INSERT INTO interfaces(name, description, group_name, method, url, query_params, headers, "
                "body_type, body_content, file_fields, mcp_enabled, open_enabled, http_enabled, "
                "proxy_slug, proxy_query_keys, proxy_header_keys, proxy_body_enabled, "
                "proxy_body_keys, parameter_schema, created_by, updated_by, "
                "created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    candidate.description,
                    candidate.group_name,
                    method,
                    url,
                    _dump_kv(candidate.query_params),
                    _dump_kv(candidate.headers),
                    candidate.body_type,
                    candidate.body_content,
                    json.dumps([item.model_dump() for item in candidate.file_fields], ensure_ascii=False),
                    0,
                    0,
                    0,
                    proxy_slug,
                    json.dumps(proxy_query_keys, ensure_ascii=False),
                    json.dumps(proxy_header_keys, ensure_ascii=False),
                    1 if candidate.proxy_body_enabled else 0,
                    json.dumps(proxy_body_keys, ensure_ascii=False),
                    json.dumps(
                        [item.model_dump(mode="json") for item in candidate.parameter_schema],
                        ensure_ascii=False,
                    ),
                    current_user.id,
                    current_user.id,
                    now, now,
                ),
            )
            existing.add(key)
            imported += 1

    return {
        "imported": imported,
        "skipped": skipped,
        "total": len(items),
        "name": body.name or "",
    }
