"""用户变量公开只读查询端点（/api/public/env-vars、/api/public/privacy-vars）。

供 n8n 等外部流水线无人值守调用：以「变量查询密钥」替代平台 JWT——密钥
跟用户不跟变量，按类别（env/privacy）隔离，一把有效密钥即可读取该用户
该类别下全部变量。鉴权与 event ingest key 同一模式：Authorization:
Bearer <key> → sha256 查表，失败统一 401 "Invalid API key"（刻意不用
"Not authenticated"，避免前端 axios 拦截器误判跳登录）。

先例：/api/public/manual-datasets（datasets/sharing_router.py）。
响应为无信封的 {"items": [...]}，便于流水线直接消费；隐私变量未上报过
的 key 返回 value=null（变量存在性对持钥方本就可见）。解密失败的坏行
（Fernet 密钥轮换/数据损坏）同样降级为 value=null 并告警日志留痕，不让
单条坏数据打断整类查询（500 会引发 n8n 重试风暴）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session

from app.auth.crypto import decrypt_value, hash_query_key
from app.auth.models import User, UserEnvVar, UserPrivacyVar, UserQueryKey
from app.deps import get_db

logger = logging.getLogger(__name__)

public_router = APIRouter()

_bearer = HTTPBearer(auto_error=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def query_key_is_usable(row: UserQueryKey, now: datetime) -> bool:
    """密钥统一有效性判定：未吊销且未过期（含已吊销但未过期的行）。

    配额检查（auth/router.py）与公开端点鉴权共用此函数，避免两处判定
    逻辑漂移出"配额认为有效、鉴权认为无效"的分叉。SQLite/PG 读回的
    naive datetime 统一按 UTC 解释（写入侧恒为 UTC）。
    """
    return (
        row.revoked_at is None
        and (row.expires_at is None or _as_aware_utc(row.expires_at) > now)
    )


def _safe_decrypt(ciphertext: str, *, key_id: str, var_key: str) -> str | None:
    """解密单个变量值；坏行（Fernet 密钥轮换/数据损坏）降级为 None 并告警。

    公开查询端点面向无人值守流水线：单条坏数据不应让整类变量不可用
    （500 会引发 n8n 重试风暴），坏行以 value=null 返回、日志留痕排查。
    """
    try:
        return decrypt_value(ciphertext)
    except Exception:
        logger.warning(
            "查询密钥读取变量解密失败，降级为 null（key_id=%s var=%s）",
            key_id,
            var_key,
        )
        return None


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
    if not row or not query_key_is_usable(row, _now()):
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
    return {
        "items": [
            {
                "key": r.key,
                "value": _safe_decrypt(r.value_encrypted, key_id=key.id, var_key=r.key)
                if r.value_encrypted
                else "",
            }
            for r in rows
        ]
    }


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
                "value": _safe_decrypt(r.value_encrypted, key_id=key.id, var_key=r.key)
                if r.value_encrypted
                else None,
                "last_reported_at": r.last_reported_at,
            }
            for r in rows
        ]
    }
