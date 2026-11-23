"""multica REST 客户端：_request seam 打桩的传输层单测。"""
from __future__ import annotations

import pytest

from app.shared.config import settings
from app.super_assistant import multica_client
from app.super_assistant.multica_client import MulticaClientError


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload: dict | list | None = None,
                 has_json: bool = True):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self._has_json = has_json

    def json(self):
        if not self._has_json or self._payload is None:
            raise ValueError("no json")
        return self._payload


def _patch_request(monkeypatch, response: _FakeResponse, calls: list | None = None):
    def fake_request(method, url, **kwargs):
        if calls is not None:
            calls.append({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(multica_client, "_request", fake_request)


def test_clean_web_url_is_browser_origin_only(monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("workbench URL validation must not query the network")

    monkeypatch.setattr(multica_client, "validate_mcp_url", no_network)
    monkeypatch.setattr(multica_client, "_request", no_network)
    for value, expected in (
        (None, None),
        (" ", None),
        (" HTTPS://Web.Example.test:443/ ", "https://web.example.test"),
        ("http://127.0.0.1:18100", "http://127.0.0.1:18100"),
        ("https://[2001:db8::10]:8446/", "https://[2001:db8::10]:8446"),
    ):
        assert multica_client.clean_web_url(value) == expected
    for value in (
        "web.example.test", "ftp://web.example.test", "https:////web.example.test",
        "https://pat@web.example.test", "https://web.example.test/v1",
        "https://web.example.test//", "https://web.example.test?", "https://web.example.test/#",
        "https://web.example.test\\evil.example.test", "https://we\x00b.example.test",
        "https://we\tb.example.test", "https://web.example.test:0",
        "https://web.example.test:65536", "https://[2001:db8::10]x",
    ):
        with pytest.raises(MulticaClientError, match="工作台地址"):
            multica_client.clean_web_url(value)


def test_fetch_me_sends_bearer_and_normalizes_base_url(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(payload={"id": "u1", "name": "admin"}), calls)
    me = multica_client.fetch_me("http://127.0.0.1:8080/", "mul-token")
    assert me["name"] == "admin"
    assert calls[0]["url"] == "http://127.0.0.1:8080/api/me"
    assert calls[0]["headers"]["Authorization"] == "Bearer mul-token"
    assert calls[0]["follow_redirects"] is False
    assert "X-Workspace-ID" not in calls[0]["headers"]


def test_scoped_calls_include_workspace_header(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(payload=[]), calls)
    multica_client.list_agents("http://127.0.0.1:8080", "mul-token", "ws-1")
    assert calls[0]["headers"]["X-Workspace-ID"] == "ws-1"
    assert calls[0]["url"].endswith("/api/agents")


def test_list_issues_drops_empty_params_and_passes_filters(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(payload={"issues": []}), calls)
    multica_client.list_issues(
        "http://127.0.0.1:8080", "mul-token", "ws-1",
        status="in_progress", assignee=None, limit=10,
    )
    assert calls[0]["params"] == {"status": "in_progress", "limit": "10"}


def test_wrapped_and_plain_list_shapes_normalize(monkeypatch):
    _patch_request(monkeypatch, _FakeResponse(payload={"agents": [{"id": "a1"}]}))
    assert multica_client.list_agents("http://127.0.0.1:8080", "t", "ws") == [{"id": "a1"}]
    _patch_request(monkeypatch, _FakeResponse(payload=[{"identifier": "MYW-1"}]))
    assert multica_client.list_issues("http://127.0.0.1:8080", "t", "ws") == [{"identifier": "MYW-1"}]
    _patch_request(monkeypatch, _FakeResponse(payload={"unexpected": True}))
    assert multica_client.list_agents("http://127.0.0.1:8080", "t", "ws") == []


def test_http_error_surfaces_status_and_server_message(monkeypatch):
    _patch_request(monkeypatch, _FakeResponse(401, payload={"error": "missing authorization"}))
    with pytest.raises(MulticaClientError, match="401"):
        multica_client.fetch_me("http://127.0.0.1:8080", "bad")


def test_non_json_response_raises(monkeypatch):
    _patch_request(monkeypatch, _FakeResponse(200, text="<html>", has_json=False))
    with pytest.raises(MulticaClientError, match="有效 JSON"):
        multica_client.fetch_me("http://127.0.0.1:8080", "t")


def test_create_issue_assignee_contract_is_type_plus_id(monkeypatch):
    """服务端 create 只认 assignee_type=agent + assignee_id（名称字段会被静默忽略）。"""
    calls: list = []
    _patch_request(
        monkeypatch,
        _FakeResponse(payload={"identifier": "MYW-9", "title": "修复登录"}),
        calls,
    )
    multica_client.create_issue(
        "http://127.0.0.1:8080", "t", "ws",
        title="修复登录", description=None, assignee_id="258652f6-44fa-498b-83b7-ec5016552931",
    )
    assert calls[0]["json"] == {
        "title": "修复登录",
        "assignee_type": "agent",
        "assignee_id": "258652f6-44fa-498b-83b7-ec5016552931",
    }

    multica_client.create_issue(
        "http://127.0.0.1:8080", "t", "ws",
        title="修复登录", description="详细描述", assignee_id=None,
    )
    assert calls[1]["json"] == {"title": "修复登录", "description": "详细描述"}

    multica_client.create_issue(
        "http://127.0.0.1:8080", "t", "ws",
        title="修复登录", allow_duplicate=True,
    )
    assert calls[2]["json"] == {"title": "修复登录", "allow_duplicate": True}


def test_external_task_observation_and_cancel_use_issue_task_routes(monkeypatch):
    calls: list = []
    responses = iter([
        _FakeResponse(payload={"identifier": "MYW-9", "status": "in_progress"}),
        _FakeResponse(payload={"tasks": [{"id": "task-1", "status": "running"}]}),
        _FakeResponse(payload=[{"id": "task-1", "status": "completed"}]),
        _FakeResponse(payload={"status": "cancelled"}),
    ])

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(multica_client, "_request", fake_request)
    issue = multica_client.get_issue("http://127.0.0.1:8080", "t", "ws", "MYW-9")
    active = multica_client.get_active_task("http://127.0.0.1:8080", "t", "ws", "MYW-9")
    runs = multica_client.list_task_runs("http://127.0.0.1:8080", "t", "ws", "MYW-9")
    result = multica_client.cancel_task("http://127.0.0.1:8080", "t", "ws", "MYW-9", "task-1")
    assert issue["identifier"] == "MYW-9"
    assert active["tasks"] == [{"id": "task-1", "status": "running"}]
    assert runs == [{"id": "task-1", "status": "completed"}]
    assert result["status"] == "cancelled"
    assert calls[0]["url"].endswith("/api/issues/MYW-9")
    assert calls[1]["url"].endswith("/api/issues/MYW-9/active-task")
    assert calls[2]["url"].endswith("/api/issues/MYW-9/task-runs")
    assert calls[2]["params"] == {"scope": "issue"}
    assert calls[3]["method"] == "POST"
    assert calls[3]["url"].endswith("/api/issues/MYW-9/tasks/task-1/cancel")


def test_match_agent_exact_then_unique_substring():
    agents = [
        {"id": "agent-1", "name": "全栈工程师（KiMi）"},
        {"id": "agent-2", "name": "测试工程师（KiMi）"},
        {"id": "agent-3", "name": "软件架构师"},
    ]
    assert multica_client.match_agent(agents, "全栈工程师（KiMi）") == ("agent-1", "全栈工程师（KiMi）")
    assert multica_client.match_agent(agents, "软件") == ("agent-3", "软件架构师")
    with pytest.raises(MulticaClientError, match="多个智能体"):
        multica_client.match_agent(agents, "KiMi")  # 子串命中两个：歧义报错
    with pytest.raises(MulticaClientError, match="未在工作区找到"):
        multica_client.match_agent(agents, "不存在的名字")
    with pytest.raises(MulticaClientError, match="不能为空"):
        multica_client.match_agent(agents, "  ")


def test_normalize_base_url_trims_trailing_slash_and_rejects_bad_scheme():
    assert multica_client.normalize_base_url("http://127.0.0.1:8080///") == "http://127.0.0.1:8080"
    with pytest.raises(MulticaClientError):
        multica_client.normalize_base_url("ftp://multica.local")


def test_base_url_rejects_private_targets_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(payload={}), calls)
    with pytest.raises(MulticaClientError, match="multica 服务地址无效"):
        multica_client.fetch_me("http://127.0.0.1:8080", "t")
    assert calls == []  # SSRF 拒绝发生在发请求之前


def test_multica_internal_allowlist_is_independent_of_mcp_trust(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "super_assistant_mcp_internal_base_urls", "http://127.0.0.1:8080")
    monkeypatch.setattr(settings, "super_assistant_multica_internal_base_urls", "")
    calls = []
    _patch_request(monkeypatch, _FakeResponse(payload={"id": "me"}), calls)
    with pytest.raises(MulticaClientError):
        multica_client.fetch_me("http://127.0.0.1:8080", "synthetic-pat")
    assert calls == []
    monkeypatch.setattr(settings, "super_assistant_multica_internal_base_urls", "http://127.0.0.1:8080")
    assert multica_client.fetch_me("http://127.0.0.1:8080", "synthetic-pat") == {"id": "me"}
    assert len(calls) == 1
    assert calls[0]["follow_redirects"] is False


@pytest.mark.parametrize("url", [
    "http://169.254.169.254", "http://169.254.1.2", "http://0.0.0.0", "http://224.0.0.1",
])
def test_multica_allowlist_cannot_authorize_metadata_or_special_targets(monkeypatch, url):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "super_assistant_multica_internal_base_urls", url)
    calls = []
    _patch_request(monkeypatch, _FakeResponse(payload={}), calls)
    with pytest.raises(MulticaClientError):
        multica_client.fetch_me(url, "synthetic-pat")
    assert calls == []


@pytest.mark.parametrize("suffix", [
    "/../admin", "/%2e%2e/admin", "/%252e%252e/admin", "/%2fadmin", "/%255cadmin",
    "?access_token=synthetic-pat", "#synthetic-pat",
])
def test_multica_internal_url_rejects_ambiguous_paths_and_embedded_secrets(monkeypatch, suffix):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "super_assistant_multica_internal_base_urls", "http://127.0.0.1:8080")
    calls = []
    _patch_request(monkeypatch, _FakeResponse(payload={}), calls)
    with pytest.raises(MulticaClientError) as raised:
        multica_client.fetch_me("http://127.0.0.1:8080" + suffix, "synthetic-pat")
    assert "synthetic-pat" not in str(raised.value)
    assert calls == []


def test_redirect_refuses_to_forward_pat_or_echo_upstream_body(monkeypatch):
    calls = []
    response = _FakeResponse(307, payload={"error": "synthetic-pat https://foreign.example"})
    response.headers = {"Location": "https://foreign.example/api/me"}
    _patch_request(monkeypatch, response, calls)
    with pytest.raises(MulticaClientError) as raised:
        multica_client.fetch_me("http://127.0.0.1:8080", "synthetic-pat")
    assert "307" in str(raised.value)
    assert "synthetic-pat" not in str(raised.value)
    assert "foreign.example" not in str(raised.value)
    assert len(calls) == 1 and calls[0]["follow_redirects"] is False


@pytest.mark.parametrize("payload", [{"task": {"id": "task-1"}}, {"tasks": [{}]}, {}, []])
def test_active_task_response_requires_pinned_plural_identity_contract(monkeypatch, payload):
    _patch_request(monkeypatch, _FakeResponse(payload=payload))
    with pytest.raises(MulticaClientError, match="任务身份"):
        multica_client.get_active_task("http://127.0.0.1:8080", "t", "ws", "MYW-9")


@pytest.mark.parametrize("payload", [
    {"tasks": [{"id": "task-1"}]}, [{}], [{"id": "task-1"}, {"id": "task-1"}],
])
def test_task_history_rejects_missing_or_duplicate_identity(monkeypatch, payload):
    _patch_request(monkeypatch, _FakeResponse(payload=payload))
    with pytest.raises(MulticaClientError, match="任务身份"):
        multica_client.list_task_runs("http://127.0.0.1:8080", "t", "ws", "MYW-9")
