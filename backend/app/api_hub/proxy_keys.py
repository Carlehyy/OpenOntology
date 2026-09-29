"""HTTP caller-key persistence for API Hub public proxy."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import List

from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import config, db
from .interface_service import _is_admin


class ProxyKeyCreate(BaseModel):
    name: str
    enabled: bool = True
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    scope_all: bool = False
    interface_ids: List[int] = Field(default_factory=list)


class ProxyKeyUpdate(ProxyKeyCreate):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _as_utc(value)
    return value.isoformat() if value else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 归属与范围校验：REST 管理路由与 MCP 工具共用同一口径。
# 密钥表没有 created_by 列，可见性由其绑定的接口归属推导。
# ---------------------------------------------------------------------------

def owned_interface_ids(user) -> set[int]:
    with db.get_conn() as conn:
        if _is_admin(user):
            rows = conn.execute("SELECT id FROM interfaces").fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM interfaces WHERE created_by = ?",
                (user.id,),
            ).fetchall()
    return {int(row["id"]) for row in rows}


def assert_key_scope(user, *, scope_all: bool, interface_ids: list[int]) -> None:
    """非管理员不能建/改 scope_all 密钥，绑定的接口必须归属本人。"""
    if _is_admin(user):
        return
    if scope_all:
        raise HTTPException(
            status_code=403,
            detail="非管理员不能创建或修改「授权全部接口」的调用方密钥",
        )
    owned = owned_interface_ids(user)
    foreign = [item for item in interface_ids if item not in owned]
    if foreign:
        raise HTTPException(
            status_code=403,
            detail="以下接口不属于当前用户，不能写入调用方密钥："
            + ", ".join(str(item) for item in foreign),
        )


def key_visible(user, key: dict, owned: set[int] | None = None) -> bool:
    """scope_all 密钥仅管理员可见；其余密钥当且仅当绑定的接口全归本人。

    ``owned`` 可传入预取的归属集合（owned_interface_ids 的结果），避免
    批量过滤时逐密钥重开连接查询（N+1）。
    """
    if _is_admin(user):
        return True
    if key.get("scope_all"):
        return False
    if owned is None:
        owned = owned_interface_ids(user)
    ids = [int(item) for item in key.get("interface_ids") or []]
    return bool(ids) and all(item in owned for item in ids)


def _validate_key_input(
    conn,
    name: str,
    valid_from: datetime | None,
    expires_at: datetime | None,
    scope_all: bool,
    interface_ids: list[int],
    *,
    allow_expired: bool = False,
) -> tuple[str, str | None, str | None, list[int]]:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="密钥名称不能为空")
    valid_from = _as_utc(valid_from)
    expires_at = _as_utc(expires_at)
    if valid_from and expires_at and expires_at <= valid_from:
        raise HTTPException(status_code=400, detail="过期时间必须晚于生效时间")
    if expires_at and expires_at <= _now() and not allow_expired:
        raise HTTPException(status_code=400, detail="过期时间必须晚于当前时间")

    try:
        ids = sorted({int(item) for item in interface_ids if int(item) > 0})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="接口权限 ID 必须是正整数") from exc
    if not scope_all and not ids:
        raise HTTPException(status_code=400, detail="请选择至少一个可调用接口，或授权全部接口")
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id FROM interfaces WHERE id IN ({placeholders})", ids
        ).fetchall()
        found = {row["id"] for row in rows}
        missing = [str(item) for item in ids if item not in found]
        if missing:
            raise HTTPException(status_code=400, detail="以下接口不存在：" + ", ".join(missing))
    return name, _iso(valid_from), _iso(expires_at), ids


def _replace_key_scope(conn, key_id: int, interface_ids: list[int]) -> None:
    conn.execute("DELETE FROM proxy_key_interfaces WHERE key_id = ?", (key_id,))
    conn.executemany(
        "INSERT INTO proxy_key_interfaces(key_id, interface_id) VALUES(?, ?)",
        [(key_id, interface_id) for interface_id in interface_ids],
    )


def _key_status(row) -> str:
    if not bool(row["enabled"]):
        return "disabled"
    now = _now()
    valid_from = _parse_iso(row["valid_from"])
    expires_at = _parse_iso(row["expires_at"])
    if valid_from and valid_from > now:
        return "scheduled"
    if expires_at and expires_at <= now:
        return "expired"
    return "active"


def _key_view(conn, row) -> dict:
    scopes = conn.execute(
        "SELECT interface_id FROM proxy_key_interfaces WHERE key_id = ? ORDER BY interface_id",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "masked_key": row["key_prefix"] + "••••••••",
        "enabled": bool(row["enabled"]),
        "valid_from": row["valid_from"],
        "expires_at": row["expires_at"],
        "scope_all": bool(row["scope_all"]),
        "interface_ids": [item["interface_id"] for item in scopes],
        "status": _key_status(row),
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _insert_proxy_key(
    conn,
    *,
    name: str,
    enabled: bool,
    valid_from: str | None,
    expires_at: str | None,
    scope_all: bool,
    interface_ids: list[int],
) -> tuple[dict, str]:
    now = _now().isoformat()
    secret = "hub_" + secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO proxy_keys(name, key_prefix, key_hash, enabled, valid_from, "
        "expires_at, scope_all, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            name,
            secret[:12],
            _hash_key(secret),
            1 if enabled else 0,
            valid_from,
            expires_at,
            1 if scope_all else 0,
            now,
            now,
        ),
    )
    _replace_key_scope(conn, int(cur.lastrowid), interface_ids)
    row = conn.execute("SELECT * FROM proxy_keys WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _key_view(conn, row), secret


def list_proxy_keys() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM proxy_keys ORDER BY id DESC").fetchall()
        return [_key_view(conn, row) for row in rows]


def create_proxy_key(body: ProxyKeyCreate) -> dict:
    with db.get_conn() as conn:
        name, valid_from, expires_at, ids = _validate_key_input(
            conn,
            body.name,
            body.valid_from,
            body.expires_at,
            body.scope_all,
            body.interface_ids,
        )
        result, secret = _insert_proxy_key(
            conn,
            name=name,
            enabled=body.enabled,
            valid_from=valid_from,
            expires_at=expires_at,
            scope_all=body.scope_all,
            interface_ids=ids,
        )
    result["secret"] = secret
    return result


def update_proxy_key(key_id: int, body: ProxyKeyUpdate) -> dict:
    now = _now().isoformat()
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM proxy_keys WHERE id = ?", (key_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="密钥不存在")
        name, valid_from, expires_at, ids = _validate_key_input(
            conn,
            body.name,
            body.valid_from,
            body.expires_at,
            body.scope_all,
            body.interface_ids,
            allow_expired=True,
        )
        conn.execute(
            "UPDATE proxy_keys SET name=?, enabled=?, valid_from=?, expires_at=?, "
            "scope_all=?, updated_at=? WHERE id=?",
            (
                name,
                1 if body.enabled else 0,
                valid_from,
                expires_at,
                1 if body.scope_all else 0,
                now,
                key_id,
            ),
        )
        _replace_key_scope(conn, key_id, ids)
        row = conn.execute("SELECT * FROM proxy_keys WHERE id = ?", (key_id,)).fetchone()
        return _key_view(conn, row)


def delete_proxy_key(key_id: int) -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT id FROM proxy_keys WHERE id = ?", (key_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="密钥不存在")
        conn.execute("DELETE FROM proxy_keys WHERE id = ?", (key_id,))
    return {"ok": True}


def authenticate_proxy_key(conn, secret: str, interface_id: int):
    if not secret:
        raise HTTPException(status_code=401, detail=f"缺少请求头 {config.PROXY_KEY_HEADER}")
    row = conn.execute(
        "SELECT * FROM proxy_keys WHERE key_hash = ?", (_hash_key(secret),)
    ).fetchone()
    if not row or _key_status(row) != "active":
        raise HTTPException(status_code=401, detail="代理密钥无效、未生效、已停用或已过期")
    if not bool(row["scope_all"]):
        allowed = conn.execute(
            "SELECT 1 FROM proxy_key_interfaces WHERE key_id = ? AND interface_id = ?",
            (row["id"], interface_id),
        ).fetchone()
        if not allowed:
            raise HTTPException(status_code=403, detail="该密钥无权调用此接口")
    return row
