"""独立看板会话 cookie（``oo_grafana_session``）。

与平台 Bearer 会话完全独立：HMAC-SHA256 域分隔签名（平台 ``SECRET_KEY`` +
独立域前缀 ``oo-grafana-session:v1:``），默认 8 小时有效期。校验只验结构、
签名和 ``exp``；已签发 cookie 不回查用户库或平台 token 撤销状态（平台退出/
改密/Bearer 过期不会即时撤销已签发看板会话，冻结契约 §3）。payload 为
无 padding base64url JSON + ``.`` + base64url MAC；MAC 输入为 ``前缀 +
payload_b64``，恒定时间比较。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.shared.config import settings

COOKIE_NAME = "oo_grafana_session"
COOKIE_PATH = "/grafana/"
# 独立域分隔前缀：与平台其他 HMAC 用途（kaneo/weknora 票据等）互不通用，
# 也不允许无前缀签名互通。
DOMAIN_PREFIX = "oo-grafana-session:v1:"
_SEPARATOR = "."
_B64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(segment: str) -> bytes | None:
    """严格 base64url 解码：拒绝空段、非法字符与 padding。"""
    if not segment or not set(segment) <= _B64_ALPHABET:
        return None
    try:
        return base64.b64decode(
            segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError):
        return None


def _digest(payload_b64: str) -> bytes:
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"{DOMAIN_PREFIX}{payload_b64}".encode("ascii"),
        hashlib.sha256,
    ).digest()


def issue_session(*, user_id: str, role: str, ttl_seconds: int) -> tuple[str, int]:
    """签发看板会话；返回 (token, exp)。"""
    exp = int(time.time()) + int(ttl_seconds)
    payload = json.dumps(
        {"exp": exp, "jti": secrets.token_hex(16), "role": role, "uid": user_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload_b64 = _encode(payload)
    token = f"{payload_b64}{_SEPARATOR}{_encode(_digest(payload_b64))}"
    return token, exp


def verify_session(token: str) -> dict | None:
    """校验看板会话；任何结构/签名/有效期问题都返回 None（fail-closed）。"""
    if not token:
        return None
    body, separator, mac = token.partition(_SEPARATOR)
    if not separator or not body or not mac or _SEPARATOR in mac:
        return None
    payload = _decode(body)
    provided = _decode(mac)
    if payload is None or provided is None:
        return None
    if not hmac.compare_digest(_digest(body), provided):
        return None
    try:
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    for key in ("uid", "role", "jti"):
        value = claims.get(key)
        if not isinstance(value, str) or not value:
            return None
    exp = claims.get("exp")
    # bool 是 int 子类：不得接受布尔值冒充 exp。
    if isinstance(exp, bool) or not isinstance(exp, int):
        return None
    if exp <= int(time.time()):
        return None
    return claims
