"""用户变量公开只读查询端点（/api/public/env-vars、/api/public/privacy-vars）。

供 n8n 等外部流水线无人值守调用：以「变量查询密钥」替代平台 JWT——密钥
跟用户不跟变量，按类别（env/privacy）隔离，一把有效密钥即可读取该用户
该类别下全部变量。鉴权与 event ingest key 同一模式：Authorization:
Bearer <key> → sha256 查表，失败统一 401 "Invalid API key"（刻意不用
"Not authenticated"，避免前端 axios 拦截器误判跳登录）。

先例：/api/public/manual-datasets（datasets/sharing_router.py）。
响应为无信封的 {"items": [...]}，便于流水线直接消费；隐私变量未上报过
的 key 返回 value=null（变量存在性对持钥方本就可见）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session

from app.auth.crypto import decrypt_value, hash_query_key
from app.auth.models import User, UserEnvVar, UserPrivacyVar, UserQueryKey
from app.deps import get_db

public_router = APIRouter()

_bearer = HTTPBearer(auto_error=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def verify_query_key(db: Session, token: str, category: str) -> UserQueryKey:
    """校验查询密钥：sha256 查表 + 未吊销 + 未过期 + 类别匹配 + 用户可用。

    未知/吊销/过期/用户停用统一 401（与分享 token 同一防枚举口径）；
    密钥本身有效但类别不匹配返回 403，便于调用方自查用错接口。
    命中后更新 last_used_at，给用户侧提供最小的使用痕迹可见性。
    """
    token = (token or "").strip()
    row = (
        db.query(UserQueryKey)
        .filter(UserQueryKey.key_hash == hash_query_key(token))
        .first()
        if token
        else None
    )
    if (
        not row
        or row.revoked_at is not None
        or (row.expires_at is not None and _as_aware_utc(row.expires_at) <= _now())
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")
    if row.category != category:
        raise HTTPException(status_code=403, detail="Key category mismatch")
    user = db.query(User).filter(User.id == row.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid API key")
    row.last_used_at = _now()
    db.commit()
    return row


@public_router.get("/env-vars")
def public_env_vars(
    credentials=Depends(_bearer),
    db: Session = Depends(get_db),
):
    key = verify_query_key(db, credentials.credentials if credentials else "", "env")
    rows = (
        db.query(UserEnvVar)
        .filter(UserEnvVar.user_id == key.user_id)
        .order_by(UserEnvVar.key)
        .all()
    )
    return {"items": [{"key": r.key, "value": decrypt_value(r.value_encrypted)} for r in rows]}


@public_router.get("/privacy-vars")
def public_privacy_vars(
    credentials=Depends(_bearer),
    db: Session = Depends(get_db),
):
    key = verify_query_key(db, credentials.credentials if credentials else "", "privacy")
    rows = (
        db.query(UserPrivacyVar)
        .filter(UserPrivacyVar.user_id == key.user_id)
        .order_by(UserPrivacyVar.key)
        .all()
    )
    return {
        "items": [
            {
                "key": r.key,
                "value": decrypt_value(r.value_encrypted) if r.value_encrypted else None,
                "last_reported_at": r.last_reported_at,
            }
            for r in rows
        ]
    }
