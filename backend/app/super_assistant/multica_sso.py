"""Multica 原生 Web 一键直达（平台默认 profile 的免登录中继）。

Multica Web 只认它自己的 Cookie 会话，会话只能由邮箱验证码或 Google 登录
创建；平台持有的 PAT 只能调接口（官方 ``/api/cli-token`` 换的也是接口令牌），
换不出浏览器会话。因此沿用 Kaneo / WeKnora 的中继结构，登录动作改走 Multica
官方的验证码接口：

- 受守卫的 ``POST /api/v2/super-assistant/multica/entry`` 用平台 SECRET_KEY
  签发一次性票据（HMAC-SHA256，TTL 90s，绑定平台用户 id）；
- 浏览器新标签打开 ``{MULTICA_DEFAULT_WEB_URL}/sso/entry?ticket=...``
  （Multica origin），multica-gateway 把该路径反代到无平台守卫的 consume
  路由（浏览器在 Multica origin 带不上平台 Bearer 头，票据本身就是凭证，
  登录与菜单守卫在签发时已完成）；
- consume 校验票据（签名 + 有效期 + Redis ``SET NX`` 单次消费）后，服务端依次
  调用 ``POST /auth/send-code`` 与 ``POST /auth/verify-code``（上游官方固定
  验证码 ``MULTICA_DEV_VERIFICATION_CODE``，只在非 production 环境生效）换取
  会话，把 ``Set-Cookie`` 原样挂在 302 ``/`` 响应上下发——会话 cookie 对
  Multica origin 是第一方。实现对 cookie 名无假设，全部透传。

直达只对部署级默认 profile 生效，所有平台用户以同一个 Multica 账号
（``MULTICA_SSO_EMAIL``）进入——与默认 PAT 的共享语义一致。

fail-closed：任何失败（票据无效/重放、Redis 不可用、Multica 拒绝、缺
``Set-Cookie``）都 302 回 Multica 根路径且不种 cookie，落到 Multica 自带
登录页（即引入直达前的体验）。邮箱、验证码与票据不写日志、不进接口响应。

上游节流（不可配置）：``/auth/send-code`` 对同一邮箱 60 秒内只发一次
验证码，而直达让所有平台用户共用一个账号——因此全平台每分钟最多完成
一次「新的登录」。窗口内的直达拿不到新会话并按 fail-closed 降级；已有
会话的浏览器仍按原会话进入，不受影响。
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
_SINGLE_USE_KEY_PREFIX = "multica:sso:ticket:"

# 与 Kaneo/WeKnora 中继同量级的单次 HTTP 预算：登录只有两个串行请求。
_SIGNIN_TIMEOUT_SECONDS = 15.0


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

    直达依赖 Redis 只做票据防重放；Redis 故障时一键直达整体降级为 Multica
    手动登录，不影响平台其余能力。
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
        logger.warning("MULTICA_SSO_TICKET_INVALID")
        return None
    if not _claim_single_use(claims["jti"]):
        logger.warning("MULTICA_SSO_TICKET_REJECTED")
        return None
    return claims["uid"]


def sso_entry_enabled() -> bool:
    """部署是否提供了一键直达所需的中继账号与固定验证码。"""
    return bool(
        settings.multica_sso_email.strip()
        and settings.multica_dev_verification_code.strip()
    )


def sign_in_session_cookies() -> list[str] | None:
    """服务端以中继账号 + 固定验证码登录 Multica，返回待透传的 Set-Cookie。

    上游的校验接口要求先有未使用的验证码记录（即使提交的是固定验证码），
    因此先 ``send-code`` 再 ``verify-code``；未配邮件服务时这一次 send-code
    只会在 Multica 容器日志里多打一行随机验证码，不影响登录结果。
    """
    if not sso_entry_enabled():
        logger.warning("MULTICA_SSO_NOT_CONFIGURED")
        return None
    try:
        base_url = origin(settings.multica_default_base_url)
    except ValueError:
        logger.warning("MULTICA_SSO_BASE_ORIGIN_INVALID")
        return None
    credentials = {
        "email": settings.multica_sso_email.strip(),
        "code": settings.multica_dev_verification_code.strip(),
    }
    try:
        sent = httpx.post(
            f"{base_url}/auth/send-code",
            json={"email": credentials["email"]},
            timeout=_SIGNIN_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )
        if sent.status_code != 200:
            logger.warning("MULTICA_SSO_SEND_CODE_REJECTED status=%s", sent.status_code)
            return None
        verified = httpx.post(
            f"{base_url}/auth/verify-code",
            json=credentials,
            timeout=_SIGNIN_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        logger.warning("MULTICA_SSO_SIGNIN_UNAVAILABLE")
        return None
    if verified.status_code != 200:
        # 固定验证码未生效（如 Multica 仍跑在 production 环境）或账号被拒：
        # 只记状态码。
        logger.warning("MULTICA_SSO_SIGNIN_REJECTED status=%s", verified.status_code)
        return None
    cookies = verified.headers.get_list("set-cookie")
    if not cookies:
        logger.warning("MULTICA_SSO_SIGNIN_NO_COOKIE")
        return None
    return cookies
