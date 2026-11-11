"""One-click WeKnora SSO relay: ticket lifecycle and consume-route contract.

与 Kaneo 中继同构（test_kaneo_sso.py），差异只在会话载体：WeKnora v0.9.0
登录响应是 JSON 里的 Bearer token（无 Set-Cookie），consume 把响应整体
base64url 编码进 302 的 ``/#oidc_result=`` fragment，由 WeKnora 前端自带
的会话落盘管道持久化。relay 在平台层有意不设守卫（浏览器经 weknora-
gateway 在 WeKnora origin 访问，带不上平台 Bearer 头），一次性票据就是
凭证。这些测试钉：

- ticket：HMAC 防伪、TTL、Redis ``SET NX`` 单次消费、Redis 不可用 fail-closed；
- sign-in：管理员凭据只进请求体、与部署 bootstrap 同口径、失败/异常响应
  不产出交接载荷；
- consume 路由：302 回 WeKnora 根路径、成功才携带 ``#oidc_result=`` 载荷
  （base64url + UTF-8 往返）、失败即手动登录兜底、日志不落凭据与票据。
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.shared.config import settings
from app.super_assistant.weknora import sso


CONSUME_PATH = "/api/v2/super-assistant/weknora/sso/consume"
ADMIN_EMAIL = "admin@weknora.example.com"
ADMIN_PASSWORD = "Relay-Only-WeKnora-2026"
# WeKnora v0.9.0 AuthLoginResponse 的真实形态：平铺 token/refresh_token/
# user/active_tenant/memberships，无 cookie；username 可能是非 ASCII。
LOGIN_PAYLOAD = {
    "success": True,
    "token": "jwt-session-token",
    "refresh_token": "jwt-refresh-token",
    "user": {"id": 1, "username": "知识管家", "email": ADMIN_EMAIL},
    "active_tenant": {"id": 1, "name": "Default"},
    "memberships": [],
}


def _decode_fragment(fragment: str) -> dict:
    payload = base64.urlsafe_b64decode(fragment + "=" * (-len(fragment) % 4))
    return json.loads(payload.decode("utf-8"))


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
    monkeypatch.setattr(settings, "weknora_base_url", "http://weknora-app:8080")
    monkeypatch.setattr(settings, "weknora_web_url", "https://kb.example.com/")
    monkeypatch.setattr(settings, "weknora_admin_email", ADMIN_EMAIL)
    monkeypatch.setattr(settings, "weknora_admin_password", ADMIN_PASSWORD)


# ── 票据：签发 / 防伪 / 单次消费 ───────────────────────────────────────────


def test_ticket_round_trip_returns_bound_user_and_burns_on_first_use(redis):
    ticket = sso.issue_ticket("user-1")

    assert sso.consume_ticket(ticket) == "user-1"
    assert sso.consume_ticket(ticket) is None

    claim = redis.calls[0]
    assert claim["nx"] is True
    assert claim["ex"] == sso.TICKET_TTL_SECONDS
    assert claim["key"].startswith("weknora:sso:ticket:")


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


def test_sign_in_encodes_login_payload_for_the_native_handoff_pipe(
    monkeypatch, relay_settings,
):
    calls: list[dict] = []
    response = httpx.Response(200, json=LOGIN_PAYLOAD)
    monkeypatch.setattr(sso.httpx, "post", _fake_post(calls, response))

    fragment = sso.sign_in_handoff_fragment()

    # 载荷经 WeKnora 前端 decodeOIDCResult 的同构规则可无损还原（含非 ASCII）
    assert _decode_fragment(fragment) == LOGIN_PAYLOAD
    # 与部署 bootstrap 同口径：POST /api/v1/auth/login，body 仅邮箱/密码，
    # 不钉 Origin（WeKnora 登录不做 origin 校验）
    (call,) = calls
    assert call["url"] == "http://weknora-app:8080/api/v1/auth/login"
    assert call["json"] == {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    assert "headers" not in call
    assert call["follow_redirects"] is False
    assert call["trust_env"] is False
    assert call["timeout"] == settings.weknora_timeout_seconds


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"success": False, "message": "invalid"}),
        httpx.Response(500),
        httpx.Response(200, json={"success": False, "message": "locked"}),
        httpx.Response(200, json={"success": True, "user": {}}),
        httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"}),
    ],
    ids=["credentials-rejected", "server-error", "login-failed",
         "missing-token", "not-json"],
)
def test_sign_in_fails_closed_on_unusable_responses(
    monkeypatch, relay_settings, response,
):
    monkeypatch.setattr(sso.httpx, "post", _fake_post([], response))

    assert sso.sign_in_handoff_fragment() is None


def test_sign_in_fails_closed_when_weknora_is_unreachable(
    monkeypatch, relay_settings,
):
    calls: list[dict] = []
    monkeypatch.setattr(
        sso.httpx, "post", _fake_post(calls, error=httpx.ConnectError("down")),
    )

    assert sso.sign_in_handoff_fragment() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("weknora_admin_password", ""),
        ("weknora_admin_email", " "),
    ],
    ids=["no-password", "blank-email"],
)
def test_sign_in_skips_the_network_when_misconfigured(
    monkeypatch, relay_settings, field, value,
):
    calls: list[dict] = []
    monkeypatch.setattr(sso.httpx, "post", _fake_post(calls, httpx.Response(200)))
    monkeypatch.setattr(settings, field, value)

    assert sso.sign_in_handoff_fragment() is None
    assert calls == []


# ── consume 路由：302 契约与降级路径 ────────────────────────────────────────


def test_consume_redirects_to_native_handoff_fragment(
    client, redis, monkeypatch,
):
    monkeypatch.setattr(
        sso, "sign_in_handoff_fragment", lambda: "ZnJhZ21lbnQ",
    )
    ticket = sso.issue_ticket("user-1")

    response = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/#oidc_result=ZnJhZ21lbnQ"
    # 令牌只经 URL fragment 下发且不可被中间层缓存
    assert response.headers["cache-control"] == "no-store"
    assert ADMIN_PASSWORD not in response.text


def test_consume_is_single_use_and_does_not_sign_in_twice(
    client, redis, monkeypatch,
):
    calls: list[int] = []

    def fake_sign_in():
        calls.append(1)
        return "ZnJhZ21lbnQ"

    monkeypatch.setattr(sso, "sign_in_handoff_fragment", fake_sign_in)
    ticket = sso.issue_ticket("user-1")

    first = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )
    replay = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert first.headers["location"].startswith("/#oidc_result=")
    assert replay.status_code == 302
    assert replay.headers["location"] == "/"
    assert replay.headers["cache-control"] == "no-store"
    assert calls == [1]


@pytest.mark.parametrize(
    "ticket", [None, "", "garbage", "a.b"], ids=["missing", "empty", "garbage", "empty-parts"],
)
def test_consume_rejects_invalid_tickets_without_signing_in(
    client, redis, monkeypatch, ticket,
):
    calls: list[int] = []
    monkeypatch.setattr(sso, "sign_in_handoff_fragment", lambda: calls.append(1))

    params = {} if ticket is None else {"ticket": ticket}
    response = client.get(CONSUME_PATH, params=params, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert calls == []


def test_consume_falls_back_to_weknora_login_when_sign_in_fails(
    client, redis, monkeypatch,
):
    monkeypatch.setattr(sso, "sign_in_handoff_fragment", lambda: None)
    ticket = sso.issue_ticket("user-1")

    response = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_relay_never_logs_credentials_or_tickets(
    client, redis, monkeypatch, relay_settings, caplog,
):
    monkeypatch.setattr(sso.httpx, "post", _fake_post([], httpx.Response(401)))
    ticket = sso.issue_ticket("user-1")

    with caplog.at_level("DEBUG", logger="app.super_assistant.weknora.sso"):
        sso.consume_ticket(ticket)
        sso.consume_ticket(ticket)
        sso.sign_in_handoff_fragment()
        client.get(CONSUME_PATH, params={"ticket": "bad"}, follow_redirects=False)

    assert ADMIN_PASSWORD not in caplog.text
    assert ADMIN_EMAIL not in caplog.text
    assert ticket not in caplog.text
