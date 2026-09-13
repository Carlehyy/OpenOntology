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


def test_fetch_me_sends_bearer_and_normalizes_base_url(monkeypatch):
    calls: list = []
    _patch_request(monkeypatch, _FakeResponse(payload={"id": "u1", "name": "admin"}), calls)
    me = multica_client.fetch_me("http://127.0.0.1:8080/", "mul-token")
    assert me["name"] == "admin"
    assert calls[0]["url"] == "http://127.0.0.1:8080/api/me"
    assert calls[0]["headers"]["Authorization"] == "Bearer mul-token"
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
        _FakeResponse(payload={"id": "task-1", "status": "running"}),
        _FakeResponse(payload={"status": "cancelled"}),
    ])

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(multica_client, "_request", fake_request)
    issue = multica_client.get_issue("http://127.0.0.1:8080", "t", "ws", "MYW-9")
    task = multica_client.get_active_task("http://127.0.0.1:8080", "t", "ws", "MYW-9")
    result = multica_client.cancel_task("http://127.0.0.1:8080", "t", "ws", "MYW-9", "task-1")
    assert issue["identifier"] == "MYW-9"
    assert task["id"] == "task-1"
    assert result["status"] == "cancelled"
    assert calls[0]["url"].endswith("/api/issues/MYW-9")
    assert calls[1]["url"].endswith("/api/issues/MYW-9/active-task")
    assert calls[2]["method"] == "POST"
    assert calls[2]["url"].endswith("/api/issues/MYW-9/tasks/task-1/cancel")


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
