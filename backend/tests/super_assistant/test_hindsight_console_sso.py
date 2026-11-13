"""记忆控制台一键免密中继：票据生命周期与 consume 路由契约。

控制面没有独立网关，中继因此挂在平台 origin；consume 路由有意不挂平台
守卫（浏览器顶层导航带不上 Bearer 头），票据即凭证。这些测试钉住：

- ticket：HMAC 防伪、TTL、Redis ``SET NX`` 单次消费、Redis 不可用 fail-closed；
- 服务端登录：密钥只进请求体、不经环境代理、失败/异常响应不透传；
- entry：仅管理员、未配置或不同源时 fail-closed 并保留手动登录兜底；
- consume：302 回控制面根地址、成功才透传 Set-Cookie、日志不落密钥与票据。
"""
from __future__ import annotations

import httpx
import pytest

from app.shared.config import settings
from app.super_assistant import hindsight_sso

BASE = "/api/v2/super-assistant/memory"
ENTRY_PATH = f"{BASE}/console/entry"
CONSUME_PATH = f"{BASE}/console/sso"
CONSOLE_ORIGIN = "http://memory.example.invalid:19999"
CP_ACCESS_KEY = "Relay-Only-Hindsight-Access-2026"
# 控制面 1.2.0 实测的登录响应形态：host-only、无 Secure 前缀，Path=/。
SESSION_COOKIE = (
    "hindsight_cp_access=1794412198.signature; Path=/; Max-Age=86400; "
    "HttpOnly; SameSite=lax"
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
    monkeypatch.setattr(hindsight_sso, "redis_client", lambda: fake)
    return fake


@pytest.fixture
def relay_settings(monkeypatch):
    monkeypatch.setattr(settings, "hindsight_web_url", CONSOLE_ORIGIN, raising=False)
    monkeypatch.setattr(
        settings, "hindsight_cp_base_url", "http://hindsight:9999", raising=False,
    )
    monkeypatch.setattr(
        settings, "hindsight_cp_access_key", CP_ACCESS_KEY, raising=False,
    )


@pytest.fixture
def editor_headers(client, editor_user):
    from app.auth.service import create_access_token

    token = create_access_token({"sub": editor_user.id})
    return {"Authorization": f"Bearer {token}"}


# ── 票据：签发 / 防伪 / 单次消费 ───────────────────────────────────────────


def test_ticket_round_trip_returns_bound_user_and_burns_on_first_use(redis):
    ticket = hindsight_sso.issue_ticket("user-1")

    assert hindsight_sso.consume_ticket(ticket) == "user-1"
    assert hindsight_sso.consume_ticket(ticket) is None

    claim = redis.calls[0]
    assert claim["nx"] is True
    assert claim["ex"] == hindsight_sso.TICKET_TTL_SECONDS
    assert claim["key"].startswith("hindsight:console:ticket:")


def test_ticket_rejects_tampering_and_foreign_signatures(redis, monkeypatch):
    ticket = hindsight_sso.issue_ticket("user-1")
    body, _, signature = ticket.partition(".")
    forged_body = hindsight_sso.issue_ticket("user-2").partition(".")[0]

    for bad in (
        "",
        "no-separator",
        f"{body}.",
        f".{signature}",
        f"{forged_body}.{signature}",
        f"{body}.{'0' * 64}",
    ):
        assert hindsight_sso.consume_ticket(bad) is None

    # 签名密钥轮换后旧票据全部失效
    monkeypatch.setattr(settings, "secret_key", "rotated-secret-key-0123456789")
    assert hindsight_sso.consume_ticket(ticket) is None
    assert redis.keys == {}


def test_ticket_expires_after_ttl(redis, monkeypatch):
    ticket = hindsight_sso.issue_ticket("user-1")
    real_time = hindsight_sso.time.time
    monkeypatch.setattr(
        hindsight_sso.time, "time",
        lambda: real_time() + hindsight_sso.TICKET_TTL_SECONDS + 1,
    )

    assert hindsight_sso.consume_ticket(ticket) is None
    assert redis.keys == {}


def test_ticket_fails_closed_when_redis_is_unavailable(monkeypatch):
    monkeypatch.setattr(hindsight_sso, "redis_client", lambda: None)

    assert hindsight_sso.consume_ticket(hindsight_sso.issue_ticket("user-1")) is None


def test_ticket_fails_closed_when_redis_errors(monkeypatch):
    class BrokenRedis:
        def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(hindsight_sso, "redis_client", lambda: BrokenRedis())

    assert hindsight_sso.consume_ticket(hindsight_sso.issue_ticket("user-1")) is None


# ── 服务端登录：请求形状与失败面 ────────────────────────────────────────────


def _fake_post(recorder, response=None, error=None):
    def post(url, **kwargs):
        recorder.append({"url": url, **kwargs})
        if error is not None:
            raise error
        return response

    return post


def test_sign_in_posts_the_access_key_without_env_proxy(monkeypatch, relay_settings):
    calls: list[dict] = []
    response = httpx.Response(200, headers=[("set-cookie", SESSION_COOKIE)], json={})
    monkeypatch.setattr(hindsight_sso.httpx, "post", _fake_post(calls, response))

    assert hindsight_sso.sign_in_session_cookies() == [SESSION_COOKIE]

    (call,) = calls
    assert call["url"] == "http://hindsight:9999/api/auth/login"
    assert call["json"] == {"key": CP_ACCESS_KEY}
    assert call["follow_redirects"] is False
    assert call["trust_env"] is False
    assert call["timeout"] >= 5.0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": "unauthorized"}),
        httpx.Response(500),
        httpx.Response(200, json={"success": True}),
    ],
    ids=["key-rejected", "server-error", "no-set-cookie"],
)
def test_sign_in_fails_closed_on_unusable_responses(
    monkeypatch, relay_settings, response,
):
    monkeypatch.setattr(hindsight_sso.httpx, "post", _fake_post([], response))

    assert hindsight_sso.sign_in_session_cookies() is None


def test_sign_in_fails_closed_when_control_plane_is_unreachable(
    monkeypatch, relay_settings,
):
    calls: list[dict] = []
    monkeypatch.setattr(
        hindsight_sso.httpx, "post", _fake_post(calls, error=httpx.ConnectError("down")),
    )

    assert hindsight_sso.sign_in_session_cookies() is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hindsight_cp_access_key", ""),
        ("hindsight_cp_base_url", " "),
    ],
    ids=["no-access-key", "no-base-url"],
)
def test_sign_in_skips_the_network_when_relay_is_misconfigured(
    monkeypatch, relay_settings, field, value,
):
    calls: list[dict] = []
    monkeypatch.setattr(hindsight_sso.httpx, "post", _fake_post(calls, httpx.Response(200)))
    monkeypatch.setattr(settings, field, value)

    assert hindsight_sso.sign_in_session_cookies() is None
    assert calls == []


# ── entry：管理员边界与 fail-closed ────────────────────────────────────────


def test_entry_issues_a_relative_relay_url_for_admins(
    client, auth_headers, admin_user, relay_settings, redis,
):
    # 控制面与平台同主机（生产同为字面公网 IP，仅端口不同）才会签发
    response = client.post(
        ENTRY_PATH,
        headers={**auth_headers, "host": "memory.example.invalid"},
    )

    assert response.status_code == 200
    redirect_url = response.json()["redirect_url"]
    # 相对地址：平台经 nginx 暴露在哪个 origin，中继就落在哪个 origin
    assert redirect_url.startswith(f"{CONSUME_PATH}?ticket=")
    ticket = redirect_url.split("ticket=", 1)[1]
    # 票据绑定签发时的平台用户，且可消费一次
    assert hindsight_sso.consume_ticket(ticket) == admin_user.id
    assert hindsight_sso.consume_ticket(ticket) is None


def test_entry_rejects_non_admins(client, editor_headers, relay_settings):
    response = client.post(ENTRY_PATH, headers=editor_headers)

    assert response.status_code == 403


def test_entry_fails_closed_without_a_configured_web_origin(
    client, auth_headers, relay_settings, monkeypatch,
):
    monkeypatch.setattr(settings, "hindsight_web_url", "")

    assert client.post(ENTRY_PATH, headers=auth_headers).status_code == 503


def test_entry_fails_closed_without_relay_credentials(
    client, auth_headers, relay_settings, monkeypatch,
):
    monkeypatch.setattr(settings, "hindsight_cp_access_key", "")

    assert client.post(ENTRY_PATH, headers=auth_headers).status_code == 503


def test_entry_fails_closed_when_platform_host_differs_from_console(
    client, auth_headers, relay_settings,
):
    # TestClient 的 Host 是 testserver；与控制面主机不同时免密中继无法种 cookie
    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 503
    assert "不同源" in response.json()["detail"]


# ── consume：302 契约与降级路径 ────────────────────────────────────────────


def test_consume_forwards_session_cookies_and_redirects_to_console(
    client, redis, relay_settings, monkeypatch,
):
    monkeypatch.setattr(
        hindsight_sso, "sign_in_session_cookies", lambda: [SESSION_COOKIE],
    )
    ticket = hindsight_sso.issue_ticket("user-1")

    response = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == CONSOLE_ORIGIN
    assert response.headers.get_list("set-cookie") == [SESSION_COOKIE]
    assert response.headers["cache-control"] == "no-store"
    assert CP_ACCESS_KEY not in response.text


def test_consume_is_single_use_and_does_not_sign_in_twice(
    client, redis, relay_settings, monkeypatch,
):
    calls: list[int] = []

    def fake_sign_in():
        calls.append(1)
        return [SESSION_COOKIE]

    monkeypatch.setattr(hindsight_sso, "sign_in_session_cookies", fake_sign_in)
    ticket = hindsight_sso.issue_ticket("user-1")

    first = client.get(CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False)
    replay = client.get(CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False)

    assert first.headers.get_list("set-cookie") == [SESSION_COOKIE]
    assert replay.status_code == 302
    assert replay.headers["location"] == CONSOLE_ORIGIN
    assert replay.headers.get_list("set-cookie") == []
    assert calls == [1]


@pytest.mark.parametrize(
    "ticket", [None, "", "garbage", "a.b"], ids=["missing", "empty", "garbage", "empty-parts"],
)
def test_consume_rejects_invalid_tickets_without_signing_in(
    client, redis, relay_settings, monkeypatch, ticket,
):
    calls: list[int] = []
    monkeypatch.setattr(hindsight_sso, "sign_in_session_cookies", lambda: calls.append(1))

    params = {} if ticket is None else {"ticket": ticket}
    response = client.get(CONSUME_PATH, params=params, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == CONSOLE_ORIGIN
    assert response.headers.get_list("set-cookie") == []
    assert calls == []


def test_consume_falls_back_to_console_login_when_sign_in_fails(
    client, redis, relay_settings, monkeypatch,
):
    monkeypatch.setattr(hindsight_sso, "sign_in_session_cookies", lambda: None)
    ticket = hindsight_sso.issue_ticket("user-1")

    response = client.get(
        CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == CONSOLE_ORIGIN
    assert response.headers.get_list("set-cookie") == []


def test_relay_never_logs_the_key_or_the_ticket(
    client, redis, relay_settings, monkeypatch, caplog,
):
    monkeypatch.setattr(
        hindsight_sso.httpx, "post", _fake_post([], httpx.Response(401)),
    )
    ticket = hindsight_sso.issue_ticket("user-1")

    with caplog.at_level("WARNING"):
        client.get(CONSUME_PATH, params={"ticket": ticket}, follow_redirects=False)

    assert CP_ACCESS_KEY not in caplog.text
    assert ticket not in caplog.text
    assert "HINDSIGHT_CONSOLE_SIGNIN_REJECTED" in caplog.text
