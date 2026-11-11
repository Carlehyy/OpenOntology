"""One-click SSO entry into the native Kaneo Web (shared admin session).

决策（2026-11-11，方案一「一键免登录·共享管理员」）落地链路：

- 受守卫的 ``POST /api/v2/super-assistant/kaneo/entry`` 用平台 SECRET_KEY
  签发一次性票据（HMAC-SHA256，TTL 90s，绑定平台用户 id）；
- 浏览器新标签打开 ``{KANEO_WEB_URL}/sso/entry?ticket=...``（Kaneo
  origin），kaneo-gateway 把该路径反代到无平台守卫的 consume 路由（浏览器
  在 Kaneo origin 带不上平台 Bearer 头，票据本身就是凭证，守卫在签发时
  已完成）；
- consume 校验票据（签名 + 有效期 + Redis ``SET NX`` 单次消费）后，用与
  部署 bootstrap 相同的服务端 better-auth 登录（``POST
  /api/auth/sign-in/email``，``Origin`` 钉在公开 web origin）换取会话，
  把 ``Set-Cookie`` 原样挂在 302 ``/`` 响应上下发——会话 cookie 对 Kaneo
  origin 是第一方，符合上游 ``SameSite=Lax`` host-only 语义。上游实测下发
  ``__Secure-better-auth.session_token``（7 天）等多个 cookie：``__Secure-``
  前缀要求 HTTPS 下发，中继因此只经 HTTPS 网关生效；实现对 cookie 名无
  假设，全部透传。

fail-closed：任何失败（票据无效/重放、Redis 不可用、Kaneo 401/5xx、缺
``Set-Cookie``）都 302 回 ``/`` 且不种 cookie，落到 Kaneo 自带登录页（即
引入 SSO 前的手动登录兜底）。凭据与票据不写日志、不进接口响应。
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
from app.super_assistant.web_entry import origin

logger = logging.getLogger(__name__)

# 票据只在「点击 → 网关跳转」之间存活，90s 足够宽裕又限制重放窗口。
TICKET_TTL_SECONDS = 90

_SEPARATOR = "."
_SINGLE_USE_KEY_PREFIX = "kaneo:sso:ticket:"


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
    Kaneo 手动登录，不影响平台其余能力。
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
        logger.warning("KANEO_SSO_TICKET_INVALID")
        return None
    if not _claim_single_use(claims["jti"]):
        logger.warning("KANEO_SSO_TICKET_REJECTED")
        return None
    return claims["uid"]


def sign_in_session_cookies() -> list[str] | None:
    """服务端以管理员凭据登录 Kaneo，返回待透传的 Set-Cookie 值列表。"""
    if not settings.kaneo_admin_email.strip() or not settings.kaneo_admin_password:
        logger.warning("KANEO_SSO_NOT_CONFIGURED")
        return None
    try:
        web_origin = origin(settings.kaneo_web_url)
    except ValueError:
        logger.warning("KANEO_SSO_WEB_ORIGIN_INVALID")
        return None
    try:
        response = httpx.post(
            f"{settings.kaneo_base_url.rstrip('/')}/api/auth/sign-in/email",
            json={
                "email": settings.kaneo_admin_email,
                "password": settings.kaneo_admin_password,
            },
            # 与部署 bootstrap 同口径：私网 POST 的 Origin 必须等于 Kaneo
            # 的受信 KANEO_CLIENT_URL origin（better-auth 只对带 cookie 的
            # 请求做 origin 校验，这里钉第一方语义保持一致）。
            headers={"Origin": web_origin},
            timeout=settings.kaneo_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        logger.warning("KANEO_SSO_SIGNIN_UNAVAILABLE")
        return None
    if response.status_code != 200:
        # 凭据漂移（Kaneo 侧改密未同步 .env）或服务端拒绝：只记状态码。
        logger.warning("KANEO_SSO_SIGNIN_REJECTED status=%s", response.status_code)
        return None
    cookies = response.headers.get_list("set-cookie")
    if not cookies:
        logger.warning("KANEO_SSO_SIGNIN_NO_COOKIE")
        return None
    return cookies
