"""One-click entry into the native Hindsight control plane (admin console).

与 Kaneo/WeKnora 中继（``super_assistant/kaneo/sso.py``，2026-11-11 方案一）
同构，差异在会话载体与反代位置：

- 受守卫的 ``POST /api/v2/super-assistant/memory/console/entry`` 用平台
  SECRET_KEY 签发一次性票据（HMAC-SHA256，TTL 90s，绑定平台用户 id），返回
  平台自身的相对中继地址（控制面没有独立网关，中继因此挂在平台 origin）；
- 浏览器新标签打开该相对地址（平台 origin），consume 路由无平台守卫（浏览器
  导航带不上平台 Bearer 头，票据本身就是凭证）；
- consume 校验票据（签名 + 有效期 + Redis ``SET NX`` 单次消费）后，用
  ``HINDSIGHT_CP_ACCESS_KEY`` 对控制面做服务端登录（``POST
  {HINDSIGHT_CP_BASE_URL}/api/auth/login``，body ``{"key": ...}``），把
  ``Set-Cookie`` 原样挂在 302 回控制面根地址的响应上下发。

Cookie 的 host-only 语义让中继可以跨端口生效：平台与控制面部署在同一字面
公网 IP（只是端口不同），会话 cookie 不含 ``Domain``/``Secure``，浏览器按
主机存储并忽略端口，因此 8443 下发的 ``hindsight_cp_access`` 会在随后访问
``http://<ip>:19999`` 时一并带上——用户无需再输入访问密钥。若两者主机不同
（如平台改用域名访问），entry 直接 503，前端保留原生地址直开（控制台自带
密钥登录页）作为手动兜底。

fail-closed：任何失败（票据无效/重放、Redis 不可用、控制面 401/5xx、缺
``Set-Cookie``）都 302 回控制面根地址且不种 cookie，落到控制面自带密钥
登录页。密钥与票据不写日志、不进接口响应；302 钉 ``Cache-Control: no-store``。
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

# 票据只在「点击 → 中继跳转」之间存活，90s 足够宽裕又限制重放窗口。
TICKET_TTL_SECONDS = 90

_SEPARATOR = "."
_SINGLE_USE_KEY_PREFIX = "hindsight:console:ticket:"


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

    Redis 故障时免登录入口整体降级为控制面手动密钥登录，不影响记忆链路。
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
        logger.warning("HINDSIGHT_CONSOLE_TICKET_INVALID")
        return None
    if not _claim_single_use(claims["jti"]):
        logger.warning("HINDSIGHT_CONSOLE_TICKET_REJECTED")
        return None
    return claims["uid"]


def relay_ready() -> bool:
    """中继所需的部署配置是否齐全（服务端登录密钥 + 控制面内网地址）。"""
    return bool(
        settings.hindsight_cp_access_key
        and settings.hindsight_cp_base_url.strip()
    )


def sign_in_session_cookies() -> list[str] | None:
    """以控制面访问密钥做服务端登录，返回待透传的 Set-Cookie 值列表。"""
    if not relay_ready():
        logger.warning("HINDSIGHT_CONSOLE_RELAY_NOT_CONFIGURED")
        return None
    try:
        response = httpx.post(
            f"{settings.hindsight_cp_base_url.rstrip('/')}/api/auth/login",
            json={"key": settings.hindsight_cp_access_key},
            # 控制面只在内网；不接受环境代理，避免凭据被转发到意外出口。
            timeout=max(settings.hindsight_timeout_seconds, 5.0),
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        logger.warning("HINDSIGHT_CONSOLE_SIGNIN_UNAVAILABLE")
        return None
    if response.status_code != 200:
        # 密钥漂移（控制面 .env 被手工改过）或控制面未就绪：只记状态码。
        logger.warning(
            "HINDSIGHT_CONSOLE_SIGNIN_REJECTED status=%s", response.status_code,
        )
        return None
    cookies = response.headers.get_list("set-cookie")
    if not cookies:
        logger.warning("HINDSIGHT_CONSOLE_SIGNIN_NO_COOKIE")
        return None
    return cookies
