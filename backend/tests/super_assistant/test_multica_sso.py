"""Multica 一键直达中继：票据生命周期、服务端登录与 consume 路由契约。

中继在平台层有意不挂守卫（浏览器经 multica-gateway 在 Multica origin 访问，
带不上平台 Bearer 头），一次性票据本身就是凭证。这些用例钉住：

- ticket：HMAC 防伪、TTL、Redis ``SET NX`` 单次消费、Redis 不可用 fail-closed；
- sign-in：先 ``send-code`` 再 ``verify-code``，中继账号与固定验证码只进请求体，
  任一步失败/异常响应都不透传；
- consume 路由：302 回 Multica 根路径、成功才透传 Set-Cookie、失败即落到
  Multica 自带登录页、日志不落凭据与票据。
"""
from __future__ import annotations

import httpx
import pytest

from app.shared.config import settings
from app.super_assistant import multica_sso as sso


CONSUME_PATH = "/api/v2/super-assistant/multica/sso/consume"
SSO_EMAIL = "admin@multica.example.com"
FIXED_CODE = "246810"
# 上游 v0.11.0 实测的登录响应形态：HttpOnly 会话 cookie + 两个可读的 CSRF
# cookie；中继对 cookie 名无假设，全部透传。
AUTH_COOKIE = (
    "multica_auth=token-value.signature; Path=/; Max-Age=2592000; "
    "HttpOnly; SameSite=Strict"
)
CSRF_COOKIE = "multica_csrf=nonce.signature; Path=/; Max-Age=2592000; SameSite=Strict"
CSRF_SESSION_COOKIE = (
    "multica_csrf_session=nonce.signature; Path=/; Max-Age=2592000; SameSite=Strict"
)
ALL_COOKIES = [AUTH_COOKIE, CSRF_COOKIE, CSRF_SESSION_COOKIE]


class FakeRedis:
    """仅实现票据单次消费用到的 ``set(nx=..., ex=...)``。"""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.calls: list[dict] = []

    def set(self, key, value, nx=False, ex=None):
        self.calls.append({"key": key, "nx": nx, "ex": ex})
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(sso, "redis_client", lambda: fake)
    return fake


@pytest.fixture
def relay_settings(monkeypatch):
    monkeypatch.setattr(settings, "multica_default_base_url", "http://multica:8080")
    monkeypatch.setattr(settings, "multica_default_web_url", "https://multica.example.com")
    monkeypatch.setattr(settings, "multica_sso_email", SSO_EMAIL)
    monkeypatch.setattr(settings, "multica_dev_verification_code", FIXED_CODE)


def _ok_responses() -> list[httpx.Response]:
    return [
        httpx.Response(200, json={"message": "Verification code sent"}),
        httpx.Response(
            200,
            headers=[("set-cookie", cookie) for cookie in ALL_COOKIES],
            json={"token": "session-jwt", "user": {"email": SSO_EMAIL}},
        ),
    ]


def _fake_post(recorder, responses, error=None):
    """按调用顺序返回 ``responses``（最后一个用尽后重复）；``error`` 优先。"""
    def post(url, **kwargs):
        recorder.append({"url": url, **kwargs})
        if error is not None:
            raise error
        index = min(len(recorder) - 1, len(responses) - 1)
        return responses[index]

    return post


# ── 票据：签发 / 防伪 / 单次消费 ───────────────────────────────────────────


def test_ticket_round_trip_returns_bound_user_and_burns_on_first_use(redis):
    ticket = sso.issue_ticket("user-1")

    assert sso.consume_ticket(ticket) == "user-1"
    assert sso.consume_ticket(ticket) is None

    claim = redis.calls[0]
    assert claim["nx"] is True
    assert claim["ex"] == sso.TICKET_TTL_SECONDS
    assert claim["key"].startswith("multica:sso:ticket:")


def test_ticket_rejects_tampering_and_foreign_signatures(redis, monkeypatch):
    ticket = sso.issue_ticket("user-1")
    body, _, signature = ticket.partition(".")
    forged_body = sso.issue_ticket("user-2").partition(".")[0]

    for bad in (
        "",
        "no-separator",
        f"{body}.",
        f".{signature}",
        f"{forged_body}.{signature}",
        f"{body}.{'0' * 64}",
    ):
        assert sso.consume_ticket(bad) is None

    # 签名密钥轮换后旧票据全部失效
    monkeypatch.setattr(settings, "secret_key", "rotated-secret-key-0123456789")
    assert sso.consume_ticket(ticket) is None
    assert redis.keys == {}


def test_ticket_expires_after_ttl(redis, monkeypatch):
    ticket = sso.issue_ticket("user-1")
    real_time = sso.time.time
    monkeypatch.setattr(
        sso.time, "time",
        lambda: real_time() + sso.TICKET_TTL_SECONDS + 1,
    )

    assert sso.consume_ticket(ticket) is None
    assert redis.keys == {}


def test_ticket_fails_closed_when_redis_is_unavailable(monkeypatch):
    monkeypatch.setattr(sso, "redis_client", lambda: None)

    assert sso.consume_ticket(sso.issue_ticket("user-1")) is None


def test_ticket_fails_closed_when_redis_errors(monkeypatch):
    class BrokenRedis:
        def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(sso, "redis_client", lambda: BrokenRedis())

    assert sso.consume_ticket(sso.issue_ticket("user-1")) is None


# ── 服务端登录：请求形状与失败面 ────────────────────────────────────────────


def test_sign_in_sends_email_then_fixed_code_and_relays_cookies(
    monkeypatch, relay_settings,
):
    calls: list[dict] = []
    monkeypatch.setattr(sso.httpx, "post", _fake_post(calls, _ok_responses()))

    assert sso.sign_in_session_cookies() == ALL_COOKIES

    send, verify = calls
    assert send["url"] == "http://multica:8080/auth/send-code"
    assert send["json"] == {"email": SSO_EMAIL}
    assert verify["url"] == "http://multica:8080/auth/verify-code"
    assert verify["json"] == {"email": SSO_EMAIL, "code": FIXED_CODE}
    for call in calls:
        assert call["follow_redirects"] is False
        assert call["trust_env"] is False
        assert call["timeout"] == sso._SIGNIN_TIMEOUT_SECONDS


def test_sign_in_stops_when_send_code_is_rejected(monkeypatch, relay_settings):
    calls: list[dict] = []
    monkeypatch.setattr(
        sso.httpx, "post",
        _fake_post(calls, [httpx.Response(429, json={"message": "too many requests"})]),
    )

    assert sso.sign_in_session_cookies() is None
    assert [call["url"].rsplit("/", 1)[-1] for call in calls] == ["send-code"]


@pytest.mark.parametrize(
    "verify_response",
    [
        httpx.Response(401, json={"message": "invalid code"}),
        httpx.Response(500),
        httpx.Response(200, json={"token": "session-jwt"}),
    ],
    ids=["code-rejected", "server-error", "no-set-cookie"],
)
def test_sign_in_fails_closed_on_unusable_verify_responses(
    monkeypatch, relay_settings, verify_response,
):
    monkeypatch.setattr(
        sso.httpx, "post",
        _fake_post([], [httpx.Response(200, json={}), verify_response]),
    )

    assert sso.sign_in_session_cookies() is None


def test_sign_in_fails_closed_when_multica_is_unreachable(
    monkeypatch, relay_settings,
):
    calls: list[dict] = []
    monkeypatch.setattr(
        sso.httpx, "post", _fake_post(calls, [], error=httpx.ConnectError("down")),
    )

    assert sso.sign_in_session_cookies() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("multica_dev_verification_code", ""),
        ("multica_sso_email", " "),
        ("multica_default_base_url", "http://multica:8080/api"),
    ],
    ids=["no-fixed-code", "blank-email", "invalid-base-origin"],
)
def test_sign_in_skips_the_network_when_misconfigured(
    monkeypatch, relay_settings, field, value,
):
    calls: list[dict] = []
    monkeypatch.setattr(sso.httpx, "post", _fake_post(calls, _ok_responses()))
    monkeypatch.setattr(settings, field, value)

    assert sso.sign_in_session_cookies() is None
    assert calls == []


def test_sso_entry_requires_both_account_and_fixed_code(monkeypatch, relay_settings):
    assert sso.sso_entry_enabled() is True

    monkeypatch.setattr(settings, "multica_dev_verification_code", "")
    assert sso.sso_entry_enabled() is False

    monkeypatch.setattr(settings, "multica_dev_verification_code", FIXED_CODE)
    monkeypatch.setattr(settings, "multica_sso_email", "")
    assert sso.sso_entry_enabled() is False


# ── consume 路由：302 契约与降级路径 ────────────────────────────────────────


def test_consume_forwards_session_cookies_and_redirects_to_multica_root(
    client, redis, monkeypatch,
):
    monkeypatch.setattr(sso, "sign_in_session_cookies", lambda: list(ALL_COOKIES))
    ticket = sso.issue_ticket("user-1")

    response = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert response.headers.get_list("set-cookie") == ALL_COOKIES
    assert FIXED_CODE not in response.text
    assert SSO_EMAIL not in response.text


def test_consume_is_single_use_and_does_not_sign_in_twice(
    client, redis, monkeypatch,
):
    calls: list[int] = []

    def fake_sign_in():
        calls.append(1)
        return [AUTH_COOKIE]

    monkeypatch.setattr(sso, "sign_in_session_cookies", fake_sign_in)
    ticket = sso.issue_ticket("user-1")

    first = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )
    replay = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert first.headers.get_list("set-cookie") == [AUTH_COOKIE]
    assert replay.status_code == 302
    assert replay.headers["location"] == "/"
    assert replay.headers.get_list("set-cookie") == []
    assert calls == [1]


@pytest.mark.parametrize(
    "ticket", [None, "", "garbage", "a.b"], ids=["missing", "empty", "garbage", "empty-parts"],
)
def test_consume_rejects_invalid_tickets_without_signing_in(
    client, redis, monkeypatch, ticket,
):
    calls: list[int] = []
    monkeypatch.setattr(sso, "sign_in_session_cookies", lambda: calls.append(1))

    params = {} if ticket is None else {"ticket": ticket}
    response = client.get(CONSUME_PATH, params=params, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert response.headers.get_list("set-cookie") == []
    assert calls == []


def test_consume_falls_back_to_multica_login_when_sign_in_fails(
    client, redis, monkeypatch,
):
    monkeypatch.setattr(sso, "sign_in_session_cookies", lambda: None)
    ticket = sso.issue_ticket("user-1")

    response = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert response.headers.get_list("set-cookie") == []


def test_relay_never_logs_credentials_or_tickets(
    client, redis, monkeypatch, relay_settings, caplog,
):
    monkeypatch.setattr(
        sso.httpx, "post",
        _fake_post([], [httpx.Response(200, json={}), httpx.Response(401, json={})]),
    )
    ticket = sso.issue_ticket("user-1")

    # 只收中继模块自己的日志：HTTP 客户端的访问日志天然带请求 URL，
    # 与凭据是否泄漏无关（真实票据不进 HTTP 调用）。
    with caplog.at_level("DEBUG", logger="app.super_assistant.multica_sso"):
        sso.consume_ticket(ticket)
        sso.consume_ticket(ticket)
        sso.sign_in_session_cookies()
        response = client.get(
            CONSUME_PATH, params={"ticket": "bad"}, follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers.get_list("set-cookie") == []
    assert FIXED_CODE not in caplog.text
    assert SSO_EMAIL not in caplog.text
    assert ticket not in caplog.text
