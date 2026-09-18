"""Canonical persistence service for API Hub interfaces."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import List

from fastapi import HTTPException

from . import config, db, publication
from .interface_contracts import DeleteGroupBody, InterfaceIn, KV
from .personal_ref import interface_has_personal_refs


_RESERVED_GROUP = "默认分组"
_PROXY_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROXY_RESERVED_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


def _check_group_name(name: str) -> None:
    """Reject the display-only reserved group name."""
    if name and name.strip() == _RESERVED_GROUP:
        raise HTTPException(
            status_code=400,
            detail=f"「{_RESERVED_GROUP}」为保留名称，请使用其他名称",
        )


def _load_json_list(value) -> list:
    try:
        data = json.loads(value) if value else []
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _normalize_publish_keys(
    items: List[str],
    *,
    lower: bool = False,
) -> list[str]:
    output = []
    seen = set()
    for item in items:
        key = (item or "").strip()
        marker = key.lower() if lower else key
        if not key or marker in seen:
            continue
        seen.add(marker)
        output.append(key)
    return output


def _validate_proxy_publish(
    conn,
    body: InterfaceIn,
    iid: int | None = None,
) -> tuple[str, list[str], list[str], list[str]]:
    slug = (body.proxy_slug or "").strip().lower()
    query_keys = _normalize_publish_keys(body.proxy_query_keys)
    header_keys = _normalize_publish_keys(
        body.proxy_header_keys,
        lower=True,
    )
    body_keys = _normalize_publish_keys(body.proxy_body_keys)
    if not body.proxy_body_enabled:
        body_keys = []

    if slug and not _PROXY_SLUG_RE.fullmatch(slug):
        raise HTTPException(
            status_code=400,
            detail=(
                "HTTP 公开路径只能包含小写字母、数字、短横线和下划线，"
                "长度 1-64 位"
            ),
        )
    if body.http_enabled:
        if not slug:
            raise HTTPException(
                status_code=400,
                detail="发布 HTTP 接口前必须填写公开路径",
            )
        if not (body.url or "").strip():
            raise HTTPException(
                status_code=400,
                detail="发布 HTTP 接口前必须填写真实 URL",
            )
        if interface_has_personal_refs(body):
            raise HTTPException(
                status_code=400,
                detail=(
                    "接口配置含个人变量占位符（{{privacy:}}/{{env:}}）：公开代理"
                    "链路没有用户身份、不会解析占位符，发布后调用必然失败；"
                    "请先去除占位符再发布。"
                ),
            )
        sql = (
            "SELECT id FROM interfaces "
            "WHERE proxy_slug = ? AND http_enabled = 1"
        )
        params: list = [slug]
        if iid is not None:
            sql += " AND id <> ?"
            params.append(iid)
        if conn.execute(sql, params).fetchone():
            raise HTTPException(
                status_code=409,
                detail=f"HTTP 公开路径「{slug}」已被其它接口使用",
            )

    reserved = _PROXY_RESERVED_HEADERS | {
        config.PROXY_KEY_HEADER.lower(),
    }
    blocked = [
        key
        for key in header_keys
        if key.lower() in reserved
    ]
    if blocked:
        raise HTTPException(
            status_code=400,
            detail=(
                "以下 Header 由平台代理层管理，不能配置为透传项："
                + ", ".join(blocked)
            ),
        )
    if body_keys and body.body_type not in {"json", "form", "multipart"}:
        raise HTTPException(
            status_code=400,
            detail="只有 JSON、Form 或 Multipart Body 支持字段级开放",
        )
    if (
        body.body_type == "json"
        and any(not key.startswith("/") for key in body_keys)
    ):
        raise HTTPException(
            status_code=400,
            detail="JSON Body 字段路径必须以 / 开头",
        )
    return slug, query_keys, header_keys, body_keys


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "group_name": row["group_name"],
        "method": row["method"],
        "url": row["url"],
        "query_params": _load_json_list(row["query_params"]),
        "headers": _load_json_list(row["headers"]),
        "body_type": row["body_type"],
        "body_content": row["body_content"],
        "file_fields": _load_json_list(row["file_fields"]),
        # MCP 开放已退役：两列仅作备份兼容保留（老备份导入仍能恢复行数据），
        # 不再有任何运行时行为挂在它们上面。
        "mcp_enabled": bool(row["open_enabled"]),
        "open_enabled": bool(row["open_enabled"]),
        "http_enabled": bool(row["http_enabled"]),
        "proxy_slug": row["proxy_slug"],
        "proxy_query_keys": _load_json_list(row["proxy_query_keys"]),
        "proxy_header_keys": _load_json_list(row["proxy_header_keys"]),
        "proxy_body_enabled": bool(row["proxy_body_enabled"]),
        "proxy_body_keys": _load_json_list(row["proxy_body_keys"]),
        "parameter_schema": _load_json_list(row["parameter_schema"]),
        "config_revision": int(row["config_revision"]),
        "created_by": row["created_by"],
        "updated_by": row["updated_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _dump_kv(items: List[KV]) -> str:
    return json.dumps(
        [item.model_dump() for item in items],
        ensure_ascii=False,
    )


def _is_admin(user) -> bool:
    """``user`` is a ``User`` object with a ``role`` attribute (see app.deps)."""
    return getattr(user, "role", None) == "admin"


def _get_or_404(conn, iid: int, *, user=None):
    # ``user=None`` preserves the original system-path behavior (lookup by id
    # only) for callers such as agent_service that do not pass a user.
    if user is None or _is_admin(user):
        row = conn.execute(
            "SELECT * FROM interfaces WHERE id = ?",
            (iid,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM interfaces WHERE id = ? AND created_by = ?",
            (iid, user.id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="接口不存在")
    return row


def create_interface(body: InterfaceIn, *, user=None):
    _check_group_name(body.group_name)
    now = datetime.now(timezone.utc).isoformat()
    # System paths (e.g. agent_service) pass no user; they leave created_by
    # empty here and stamp it via _record_actor afterwards. UI requests pass
    # the current user so the row is privately owned from creation.
    actor_id = user.id if user is not None else ""
    with db.get_conn() as conn:
        slug, query_keys, header_keys, body_keys = _validate_proxy_publish(
            conn,
            body,
        )
        cursor = conn.execute(
            "INSERT INTO interfaces(name, description, group_name, method, "
            "url, query_params, headers, body_type, body_content, file_fields, "
            "mcp_enabled, open_enabled, http_enabled, proxy_slug, "
            "proxy_query_keys, proxy_header_keys, proxy_body_enabled, "
            "proxy_body_keys, parameter_schema, created_by, updated_by, "
            "created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            # 23 columns: 10 base + 5 publish + 4 proxy + 3 meta + 2 actor + 2 ts
            (
                body.name,                 # 1
                body.description,          # 2
                body.group_name,           # 3
                body.method.upper(),       # 4
                body.url,                  # 5
                _dump_kv(body.query_params),# 6
                _dump_kv(body.headers),    # 7
                body.body_type,            # 8
                body.body_content,         # 9
                json.dumps(               # 10
                    [item.model_dump() for item in body.file_fields],
                    ensure_ascii=False,
                ),
                1 if body.open_enabled else 0,    # 11 mcp_enabled
                1 if body.open_enabled else 0,    # 12 open_enabled
                1 if body.http_enabled else 0,    # 13
                slug,                             # 14
                json.dumps(query_keys, ensure_ascii=False),  # 15
                json.dumps(header_keys, ensure_ascii=False),  # 16
                1 if body.proxy_body_enabled else 0,          # 17
                json.dumps(body_keys, ensure_ascii=False),    # 18
                json.dumps(                              # 19
                    [item.model_dump(mode="json") for item in body.parameter_schema],
                    ensure_ascii=False,
                ),
                actor_id,                 # 20 created_by
                actor_id,                 # 21 updated_by
                now,                      # 22 created_at
                now,                      # 23 updated_at
            ),
        )
        row = conn.execute(
            "SELECT * FROM interfaces WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return _row_to_dict(row)


def update_interface(iid: int, body: InterfaceIn, *, user=None):
    _check_group_name(body.group_name)
    now = datetime.now(timezone.utc).isoformat()
    actor_id = user.id if user is not None else ""
    with db.get_conn() as conn:
        _get_or_404(conn, iid, user=user)
        slug, query_keys, header_keys, body_keys = _validate_proxy_publish(
            conn,
            body,
            iid,
        )
        conn.execute(
            "UPDATE interfaces SET name=?, description=?, group_name=?, "
            "method=?, url=?, query_params=?, headers=?, body_type=?, "
            "body_content=?, file_fields=?, mcp_enabled=?, "
            "open_enabled=?, http_enabled=?, proxy_slug=?, "
            "proxy_query_keys=?, proxy_header_keys=?, proxy_body_enabled=?, "
            "proxy_body_keys=?, parameter_schema=?, "
            "config_revision=config_revision+1, updated_by=?, updated_at=? "
            "WHERE id=?",
            (
                body.name,
                body.description,
                body.group_name,
                body.method.upper(),
                body.url,
                _dump_kv(body.query_params),
                _dump_kv(body.headers),
                body.body_type,
                body.body_content,
                json.dumps(
                    [item.model_dump() for item in body.file_fields],
                    ensure_ascii=False,
                ),
                1 if body.open_enabled else 0,
                1 if body.open_enabled else 0,
                1 if body.http_enabled else 0,
                slug,
                json.dumps(query_keys, ensure_ascii=False),
                json.dumps(header_keys, ensure_ascii=False),
                1 if body.proxy_body_enabled else 0,
                json.dumps(body_keys, ensure_ascii=False),
                json.dumps(
                    [
                        item.model_dump(mode="json")
                        for item in body.parameter_schema
                    ],
                    ensure_ascii=False,
                ),
                actor_id,
                now,
                iid,
            ),
        )
        row = conn.execute(
            "SELECT * FROM interfaces WHERE id = ?",
            (iid,),
        ).fetchone()
    return _row_to_dict(row)


def delete_interface(iid: int, *, user=None):
    with db.get_conn() as conn:
        _get_or_404(conn, iid, user=user)
        # Explicitly delete history even though the schema also has a cascade.
        conn.execute("DELETE FROM runs WHERE interface_id = ?", (iid,))
        conn.execute("DELETE FROM interfaces WHERE id = ?", (iid,))
    return {"ok": True}


def delete_group(body: DeleteGroupBody, *, user=None):
    """删除指定分组：将该分组下所有接口移入默认分组（group_name 置空）。"""
    name = (body.group_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="分组名不能为空")
    if name == _RESERVED_GROUP:
        raise HTTPException(
            status_code=400,
            detail=f"「{_RESERVED_GROUP}」不可删除",
        )
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        # Non-admin users only clear interfaces they own; admin/system paths
        # (user=None or role=admin) affect the whole group as before.
        if user is None or _is_admin(user):
            cursor = conn.execute(
                "UPDATE interfaces SET group_name = '', updated_at = ? "
                "WHERE group_name = ?",
                (now, name),
            )
        else:
            cursor = conn.execute(
                "UPDATE interfaces SET group_name = '', updated_at = ? "
                "WHERE group_name = ? AND created_by = ?",
                (now, name, user.id),
            )
        count = cursor.rowcount
    return {"ok": True, "count": count}


def rename_group(*, old_name: str, new_name: str, user=None):
    """Rename a group by rewriting group_name on owned interfaces."""
    old = (old_name or "").strip()
    new = (new_name or "").strip()
    if old == _RESERVED_GROUP:
        old = ""
    if new == _RESERVED_GROUP:
        new = ""
    if not old:
        raise HTTPException(status_code=400, detail="原分类名不能为空")
    _check_group_name(old)
    _check_group_name(new)
    if old == new:
        return {"ok": True, "count": 0}
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        if user is None or _is_admin(user):
            cursor = conn.execute(
                "UPDATE interfaces SET group_name = ?, updated_at = ? "
                "WHERE group_name = ?",
                (new, now, old),
            )
        else:
            cursor = conn.execute(
                "UPDATE interfaces SET group_name = ?, updated_at = ? "
                "WHERE group_name = ? AND created_by = ?",
                (new, now, old, user.id),
            )
        count = cursor.rowcount
    return {"ok": True, "count": count, "group_name": new or _RESERVED_GROUP}


def move_interface(
    iid: int,
    *,
    group_name: str = "",
    target_index: int = 0,
    user=None,
):
    """Move an interface into a group slot and resequence sort_order."""
    _check_group_name(group_name)
    now = datetime.now(timezone.utc).isoformat()
    admin = user is None or _is_admin(user)
    with db.get_conn() as conn:
        current = _row_to_dict(_get_or_404(conn, iid, user=user))
        source_group = current["group_name"]
        conn.execute(
            "UPDATE interfaces SET group_name = ?, updated_at = ? WHERE id = ?",
            (group_name, now, iid),
        )
        if admin:
            rows = conn.execute(
                "SELECT id FROM interfaces WHERE group_name = ? "
                "ORDER BY sort_order, id",
                (group_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM interfaces WHERE group_name = ? "
                "AND created_by = ? ORDER BY sort_order, id",
                (group_name, user.id),
            ).fetchall()
        ids = [row["id"] for row in rows]
        if iid in ids:
            ids.remove(iid)
        idx = max(0, min(int(target_index), len(ids)))
        ids.insert(idx, iid)
        for index, row_id in enumerate(ids):
            conn.execute(
                "UPDATE interfaces SET sort_order = ? WHERE id = ?",
                (index, row_id),
            )
        if source_group != group_name:
            if admin:
                source_rows = conn.execute(
                    "SELECT id FROM interfaces WHERE group_name = ? "
                    "ORDER BY sort_order, id",
                    (source_group,),
                ).fetchall()
            else:
                source_rows = conn.execute(
                    "SELECT id FROM interfaces WHERE group_name = ? "
                    "AND created_by = ? ORDER BY sort_order, id",
                    (source_group, user.id),
                ).fetchall()
            for index, row in enumerate(source_rows):
                conn.execute(
                    "UPDATE interfaces SET sort_order = ? WHERE id = ?",
                    (index, row["id"]),
                )
    return {"ok": True}


def _unique_proxy_slug(conn, interface: dict) -> str:
    current = (interface.get("proxy_slug") or "").strip().lower()
    candidate = current if _PROXY_SLUG_RE.fullmatch(current) else publication.slug_suggestion(interface)
    base = candidate[:64]
    suffix = 1
    iid = int(interface["id"])
    while conn.execute(
        "SELECT 1 FROM interfaces WHERE proxy_slug = ? AND http_enabled = 1 AND id <> ?",
        (candidate, iid),
    ).fetchone():
        marker = f"-{iid}" if suffix == 1 else f"-{iid}-{suffix}"
        candidate = base[: 64 - len(marker)] + marker
        suffix += 1
    return candidate


def apply_http_publication(
    iid: int,
    *,
    enabled: bool,
    slug: str = "",
    query_keys: list | None = None,
    header_keys: list | None = None,
    body_enabled: bool = False,
    body_keys: list | None = None,
    user=None,
) -> dict:
    """Update only HTTP publication columns. Does not bump config_revision."""
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        row = _get_or_404(conn, iid, user=user)
        draft = InterfaceIn(
            **{
                **_row_to_dict(row),
                "http_enabled": enabled,
                "proxy_slug": slug,
                "proxy_query_keys": list(query_keys or []),
                "proxy_header_keys": list(header_keys or []),
                "proxy_body_enabled": body_enabled,
                "proxy_body_keys": list(body_keys or []),
            }
        )
        slug, query_keys, header_keys, body_keys = _validate_proxy_publish(conn, draft, iid)
        conn.execute(
            "UPDATE interfaces SET http_enabled=?, proxy_slug=?, proxy_query_keys=?, "
            "proxy_header_keys=?, proxy_body_enabled=?, proxy_body_keys=?, updated_at=? WHERE id=?",
            (
                1 if enabled else 0,
                slug,
                json.dumps(query_keys, ensure_ascii=False),
                json.dumps(header_keys, ensure_ascii=False),
                1 if body_enabled else 0,
                json.dumps(body_keys, ensure_ascii=False),
                now,
                iid,
            ),
        )
        row = conn.execute("SELECT * FROM interfaces WHERE id = ?", (iid,)).fetchone()
    return _row_to_dict(row)


def auto_http_publication(iid: int, *, user=None) -> dict:
    """Infer a safe forwarding contract and publish without bumping revision."""
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        row = _get_or_404(conn, iid, user=user)
        interface = _row_to_dict(row)
        body_keys = publication.infer_body_keys(interface)
        draft = InterfaceIn(
            **{
                **interface,
                "http_enabled": True,
                "proxy_slug": _unique_proxy_slug(conn, interface),
                "proxy_query_keys": publication.infer_query_keys(interface),
                "proxy_header_keys": publication.infer_header_keys(
                    interface, config.PROXY_KEY_HEADER
                ),
                "proxy_body_enabled": bool(body_keys),
                "proxy_body_keys": body_keys,
            }
        )
        slug, query_keys, header_keys, body_keys = _validate_proxy_publish(
            conn, draft, iid
        )
        conn.execute(
            "UPDATE interfaces SET http_enabled=1, proxy_slug=?, proxy_query_keys=?, "
            "proxy_header_keys=?, proxy_body_enabled=?, proxy_body_keys=?, updated_at=? "
            "WHERE id=?",
            (
                slug,
                json.dumps(query_keys, ensure_ascii=False),
                json.dumps(header_keys, ensure_ascii=False),
                1 if body_keys else 0,
                json.dumps(body_keys, ensure_ascii=False),
                now,
                iid,
            ),
        )
        row = conn.execute("SELECT * FROM interfaces WHERE id = ?", (iid,)).fetchone()
    return _row_to_dict(row)
