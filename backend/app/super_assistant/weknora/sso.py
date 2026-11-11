"""One-click SSO entry into the native WeKnora Web (shared admin session).

与 Kaneo 中继（``super_assistant/kaneo/sso.py``，2026-11-11 方案一）同构，
差异只在会话载体：WeKnora v0.9.0 是纯 Bearer token 模型（登录响应 JSON
携带 ``token``/``refresh_token``，无 ``Set-Cookie``），因此不能透传 cookie，
改为把登录响应经 WeKnora 前端自带的 ``#oidc_result=`` 会话落盘管道交给
其自身代码持久化：

- 受守卫的 ``POST /api/v2/super-assistant/weknora/entry`` 用平台 SECRET_KEY
  签发一次性票据（HMAC-SHA256，TTL 90s，绑定平台用户 id）；
- 浏览器新标签打开 ``{WEKNORA_WEB_URL}/sso/entry?ticket=...``（WeKnora
  origin），weknora-gateway 把该路径反代到无平台守卫的 consume 路由（浏览器
  在 WeKnora origin 带不上平台 Bearer 头，票据本身就是凭证，守卫在签发时
  已完成）；
- consume 校验票据（签名 + 有效期 + Redis ``SET NX`` 单次消费）后，用与部署
  bootstrap 相同的服务端登录（``POST /api/v1/auth/login``，body 仅邮箱/密码，
  与 bootstrap._login 同口径）换取响应，整体 base64url 编码进 302 ``/#oidc_result=``。
  WeKnora 前端 App.vue 的 ``decodeOIDCResult`` 接受 base64url 并按 UTF-8
  解码，``persistOIDCLoginResponse`` 自行写入 ``weknora_token`` 等存储并经
  ``/auth/me`` 重建用户上下文——平台不复刻其 localStorage 细节。

fail-closed：任何失败（票据无效/重放、Redis 不可用、WeKnora 401/5xx、缺
token）都 302 回 ``/`` 且不带载荷，WeKnora 路由守卫把未登录访问送到自带
登录页（即引入 SSO 前的手动登录兜底）。凭据与票据不写日志、不进接口响应；
302 响应钉 ``Cache-Control: no-store``，令牌只存在于 URL fragment（不发给
服务器、不进访问日志），WeKnora 落盘后立即 ``history.replaceState`` 清除。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

import httpx

from app.shared.config import settings
from app.shared.redis_cache import get_client as redis_client

logger = logging.getLogger(__name__)

# 票据只在「点击 → 网关跳转」之间存活，90s 足够宽裕又限制重放窗口。
TICKET_TTL_SECONDS = 90

_SEPARATOR = "."
_SINGLE_USE_KEY_PREFIX = "weknora:sso:ticket:"


def _sign(payload: bytes) -> str:
    return hmac.new(
        settings.secret_key.encode(), payload, hashlib.sha256,
    ).hexdigest()


def issue_ticket(user_id: str) -> str:
    """签发一次性跳转票据：签名内容为 uid/jti/exp，防篡改防伪造。"""
    payload = json.dumps(
        {
            "exp": int(time.time()) + TICKET_TTL_SECONDS,
            "jti": secrets.token_hex(16),
            "uid": user_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{encoded}{_SEPARATOR}{_sign(payload)}"


def _decode_ticket(ticket: str) -> dict | None:
    body, separator, signature = ticket.partition(_SEPARATOR)
    if not separator or not body or not signature:
        return None
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        claims = json.loads(payload)
    except ValueError:
        return None
    if (
        not isinstance(claims, dict)
        or not isinstance(claims.get("uid"), str)
        or not claims["uid"]
        or not isinstance(claims.get("jti"), str)
        or not claims["jti"]
        or not isinstance(claims.get("exp"), int)
    ):
        return None
    if claims["exp"] < time.time():
        return None
    return claims


def _claim_single_use(jti: str) -> bool:
    """Redis ``SET NX`` 单次消费；Redis 不可用时拒绝（fail-closed）。

    SSO 依赖 Redis 只做票据防重放；Redis 故障时免登录入口整体降级为
    WeKnora 手动登录，不影响平台其余能力。
    """
    try:
        client = redis_client()
        if client is None:
            return False
        return bool(client.set(
            f"{_SINGLE_USE_KEY_PREFIX}{jti}", "1",
            nx=True, ex=TICKET_TTL_SECONDS,
        ))
    except Exception:  # noqa: BLE001 - 无法保证单次消费即拒绝
        return False


def consume_ticket(ticket: str) -> str | None:
    """校验并烧毁票据；返回签发时绑定的平台用户 id，失败返回 None。"""
    claims = _decode_ticket(ticket)
    if claims is None:
        logger.warning("WEKNORA_SSO_TICKET_INVALID")
        return None
    if not _claim_single_use(claims["jti"]):
        logger.warning("WEKNORA_SSO_TICKET_REJECTED")
        return None
    return claims["uid"]


def sign_in_handoff_fragment() -> str | None:
    """服务端以管理员凭据登录 WeKnora，返回 ``#oidc_result=`` 载荷。

    登录请求与部署 bootstrap 同口径（邮箱 + 密码，无私网 Origin 钉扎——
    WeKnora 登录不做 origin 校验）；响应做与 bootstrap 相同的结构校验后
    整体 base64url 编码。载荷经 302 的 URL fragment 下发，由 WeKnora
    前端自带的会话落盘管道消费。
    """
    if not settings.weknora_admin_email.strip() or not settings.weknora_admin_password:
        logger.warning("WEKNORA_SSO_NOT_CONFIGURED")
        return None
    try:
        response = httpx.post(
            f"{settings.weknora_base_url.rstrip('/')}/api/v1/auth/login",
            json={
                "email": settings.weknora_admin_email,
                "password": settings.weknora_admin_password,
            },
            timeout=settings.weknora_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        logger.warning("WEKNORA_SSO_SIGNIN_UNAVAILABLE")
        return None
    if response.status_code != 200:
        # 凭据漂移（WeKnora 侧改密未同步 .env）或服务端拒绝：只记状态码。
        logger.warning("WEKNORA_SSO_SIGNIN_REJECTED status=%s", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("WEKNORA_SSO_SIGNIN_INVALID_RESPONSE")
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("success") is not True
        or not isinstance(payload.get("token"), str)
        or not payload["token"]
    ):
        logger.warning("WEKNORA_SSO_SIGNIN_INVALID_RESPONSE")
        return None
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")
