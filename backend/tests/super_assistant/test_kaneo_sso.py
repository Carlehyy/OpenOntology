"""One-click Kaneo SSO relay: ticket lifecycle and consume-route contract.

The relay is intentionally unguarded at the platform layer (the browser hits
it through the kaneo-gateway on Kaneo's origin and cannot carry the platform
Bearer header); the one-time ticket IS the credential. These tests pin:

- ticket: HMAC 防伪、TTL、Redis ``SET NX`` 单次消费、Redis 不可用 fail-closed；
- sign-in：管理员凭据只进请求体、Origin 钉公开 origin、失败/异常响应不透传；
- consume 路由：302 回 Kaneo 根路径、成功才透传 Set-Cookie、失败即手动登录
  兜底、日志不落凭据与票据。
"""
from __future__ import annotations

import httpx
import pytest

from app.shared.config import settings
from app.super_assistant.kaneo import sso


CONSUME_PATH = "/api/v2/super-assistant/kaneo/sso/consume"
ADMIN_EMAIL = "admin@kaneo.example.com"
ADMIN_PASSWORD = "Relay-Only-Kaneo-2026"
# 上游 2.49.0 真实实例实测的登录响应形态：下发两个 cookie，会话 cookie 带
# ``__Secure-`` 前缀（浏览器只接受 HTTPS 下发，中继因此必须经 HTTPS 网关）。
SESSION_COOKIE = (
    "__Secure-better-auth.session_token=token-value.signature; "
    "Max-Age=604800; Path=/; HttpOnly; Secure; SameSite=Lax"
)
SECOND_COOKIE = (
    "better-auth.last_used_login_method=email; Max-Age=2592000; Path=/; "
    "Secure; SameSite=Lax"
)


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
    monkeypatch.setattr(settings, "kaneo_base_url", "http://kaneo-app:5173")
    monkeypatch.setattr(settings, "kaneo_web_url", "https://pm.example.com/")
    monkeypatch.setattr(settings, "kaneo_admin_email", ADMIN_EMAIL)
    monkeypatch.setattr(settings, "kaneo_admin_password", ADMIN_PASSWORD)


# ── 票据：签发 / 防伪 / 单次消费 ───────────────────────────────────────────


def test_ticket_round_trip_returns_bound_user_and_burns_on_first_use(redis):
    ticket = sso.issue_ticket("user-1")

    assert sso.consume_ticket(ticket) == "user-1"
    assert sso.consume_ticket(ticket) is None

    claim = redis.calls[0]
    assert claim["nx"] is True
    assert claim["ex"] == sso.TICKET_TTL_SECONDS
    assert claim["key"].startswith("kaneo:sso:ticket:")


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


def _fake_post(recorder, response=None, error=None):
    def post(url, **kwargs):
        recorder.append({"url": url, **kwargs})
        if error is not None:
            raise error
        return response

    return post


def test_sign_in_posts_admin_credentials_with_pinned_origin(
    monkeypatch, relay_settings,
):
    calls: list[dict] = []
    response = httpx.Response(
        200,
        headers=[("set-cookie", SESSION_COOKIE), ("set-cookie", SECOND_COOKIE)],
        json={"user": {}, "session": {}, "token": "bearer-token"},
    )
    monkeypatch.setattr(sso.httpx, "post", _fake_post(calls, response))

    assert sso.sign_in_session_cookies() == [SESSION_COOKIE, SECOND_COOKIE]

    (call,) = calls
    assert call["url"] == "http://kaneo-app:5173/api/auth/sign-in/email"
    assert call["json"] == {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    assert call["headers"] == {"Origin": "https://pm.example.com"}
    assert call["follow_redirects"] is False
    assert call["trust_env"] is False
    assert call["timeout"] == settings.kaneo_timeout_seconds


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"message": "invalid credentials"}),
        httpx.Response(500),
        httpx.Response(200, json={"token": "bearer-token"}),
    ],
    ids=["credentials-rejected", "server-error", "no-set-cookie"],
)
def test_sign_in_fails_closed_on_unusable_responses(
    monkeypatch, relay_settings, response,
):
    monkeypatch.setattr(sso.httpx, "post", _fake_post([], response))

    assert sso.sign_in_session_cookies() is None


def test_sign_in_fails_closed_when_kaneo_is_unreachable(
    monkeypatch, relay_settings,
):
    calls: list[dict] = []
    monkeypatch.setattr(
        sso.httpx, "post", _fake_post(calls, error=httpx.ConnectError("down")),
    )

    assert sso.sign_in_session_cookies() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kaneo_admin_password", ""),
        ("kaneo_admin_email", " "),
        ("kaneo_web_url", "https://pm.example.com/board"),
    ],
    ids=["no-password", "blank-email", "invalid-web-origin"],
)
def test_sign_in_skips_the_network_when_misconfigured(
    monkeypatch, relay_settings, field, value,
):
    calls: list[dict] = []
    monkeypatch.setattr(sso.httpx, "post", _fake_post(calls, httpx.Response(200)))
    monkeypatch.setattr(settings, field, value)

    assert sso.sign_in_session_cookies() is None
    assert calls == []


# ── consume 路由：302 契约与降级路径 ────────────────────────────────────────


def test_consume_forwards_session_cookies_and_redirects_to_kaneo_root(
    client, redis, monkeypatch,
):
    monkeypatch.setattr(
        sso, "sign_in_session_cookies", lambda: [SESSION_COOKIE, SECOND_COOKIE],
    )
    ticket = sso.issue_ticket("user-1")

    response = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert response.headers.get_list("set-cookie") == [
        SESSION_COOKIE, SECOND_COOKIE,
    ]
    assert ADMIN_PASSWORD not in response.text


def test_consume_is_single_use_and_does_not_sign_in_twice(
    client, redis, monkeypatch,
):
    calls: list[int] = []

    def fake_sign_in():
        calls.append(1)
        return [SESSION_COOKIE]

    monkeypatch.setattr(sso, "sign_in_session_cookies", fake_sign_in)
    ticket = sso.issue_ticket("user-1")

    first = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )
    replay = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert first.headers.get_list("set-cookie") == [SESSION_COOKIE]
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


def test_consume_falls_back_to_kaneo_login_when_sign_in_fails(
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
    monkeypatch.setattr(sso.httpx, "post", _fake_post([], httpx.Response(401)))
    ticket = sso.issue_ticket("user-1")

    with caplog.at_level("DEBUG", logger="app.super_assistant.kaneo.sso"):
        sso.consume_ticket(ticket)
        sso.consume_ticket(ticket)
        sso.sign_in_session_cookies()
        client.get(CONSUME_PATH, params={"ticket": "bad"}, follow_redirects=False)

    assert ADMIN_PASSWORD not in caplog.text
    assert ADMIN_EMAIL not in caplog.text
    assert ticket not in caplog.text
