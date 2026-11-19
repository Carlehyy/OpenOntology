"""Managed WeKnora endpoint execution boundary.

``execution.call_tool`` is the last line of defence for knowledge scope: it
must re-check the connection shape, the frozen tool manifest and the live
knowledge-base scope (via the endpoint's own ``list_knowledge_bases`` tool)
both before and after the actual read, and revoke the connection on any
mismatch. Workers only ever hold the endpoint token — the admin credentials
never reach this path, and the token must not leak beyond the Bearer header.

The native admin API is faked over ``httpx.MockTransport``; the MCP endpoint
itself is faked at the ``mcp_client._client_session`` seam with a single
in-memory session, so the real discovery/serialization code paths run.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import itertools
import json
import re
import secrets
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.config import settings
from app.super_assistant import mcp_client
from app.super_assistant.models import SuperAssistantMcpServer
from app.super_assistant.weknora import connections, execution
from app.super_assistant.weknora.client import WeKnoraClient


FAKE_ORIGIN = "http://127.0.0.1:8787"
ADMIN_EMAIL = "admin@weknora.example.com"
ADMIN_PASSWORD = "Native-Relay-2026"

READ_TOOLS = [
    "list_knowledge_bases", "search_knowledge", "grep_chunks", "list_documents",
    "read_document", "wiki_search", "wiki_read_page", "wiki_index",
]


# ───────────── 原生管理 API 替身（与 directory 测试同一镜像契约） ─────────────


class FakeWeKnoraAPI:
    def __init__(self):
        self.knowledge_bases: list[dict] = []
        self.endpoints: list[dict] = []
        self.tokens: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []

    def add_knowledge_base(self, identifier, name) -> dict:
        item = {"id": identifier, "name": name, "type": "document",
                "embedding_model_id": "model-embedding-1",
                "summary_model_id": "model-qa-1"}
        self.knowledge_bases.append(item)
        return item

    def add_endpoint(self, identifier, name, knowledge_base_ids) -> dict:
        token = "mcp_" + secrets.token_hex(16)
        item = {"id": identifier, "name": name, "enabled": True,
                "knowledge_base_ids": list(knowledge_base_ids),
                "tools": list(READ_TOOLS), "default_agent_id": "",
                "token_hint": f"mcp_…{token[-4:]}"}
        self.endpoints.append(item)
        self.tokens[identifier] = token
        return item

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content.decode()) if request.content else {}
        self.requests.append((method, path))
        if path == "/api/v1/auth/login":
            if (body.get("email") != ADMIN_EMAIL
                    or body.get("password") != ADMIN_PASSWORD):
                return httpx.Response(401, json={"success": False})
            return httpx.Response(200, json={
                "success": True, "token": "admin-session-token",
                "user": {"is_system_admin": True}})
        if request.headers.get("Authorization") != "Bearer admin-session-token":
            return httpx.Response(401, json={"success": False})
        if method == "GET" and path == "/api/v1/knowledge-bases":
            return httpx.Response(200, json={"success": True, "data": [dict(k) for k in self.knowledge_bases]})
        if method == "GET" and path == "/api/v1/mcp-endpoints":
            return httpx.Response(200, json={"success": True, "data": [dict(e) for e in self.endpoints]})
        match = re.fullmatch(r"/api/v1/mcp-endpoints/([^/]+)", path)
        if method == "GET" and match:
            for endpoint in self.endpoints:
                if endpoint["id"] == match.group(1):
                    return httpx.Response(200, json={"success": True, "data": dict(endpoint)})
        return httpx.Response(404, json={"success": False})


# ───────────────── MCP 端点替身（_client_session 单会话） ─────────────────


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = f"{name} 只读知识工具"
        self.inputSchema = {"type": "object"}


class FakeListResult:
    def __init__(self, tools):
        self.tools = tools


class FakeCallResult:
    def __init__(self, payload: str):
        self._payload = payload

    def model_dump_json(self, **kwargs) -> str:
        return self._payload


class FakeEndpointSession:
    def __init__(self, backend, connection):
        self.backend = backend
        self.connection = connection

    async def list_tools(self):
        if self.backend.list_error is not None:
            raise mcp_client.McpClientError(self.backend.list_error)
        return FakeListResult([FakeTool(name) for name in self.backend.tool_names])

    async def call_tool(self, tool_name, arguments):
        self.backend.calls.append({
            "tool_name": tool_name, "arguments": arguments,
            "headers": dict(self.connection.get("headers") or {}),
            "url": self.connection.get("url"),
        })
        return FakeCallResult(self.backend.respond(tool_name))


class FakeManagedEndpoint:
    """In-memory read-only knowledge endpoint behind ``_client_session``."""

    def __init__(self, knowledge_base_ids):
        self.knowledge_base_ids = list(knowledge_base_ids)
        self.tool_names = list(READ_TOOLS)
        self.calls: list[dict] = []
        self.connections: list[dict] = []
        self.result = json.dumps(
            {"content": [{"type": "text", "text": "42 chunks"}]}, ensure_ascii=False)
        self.list_error: str | None = None
        self.scope_payload: str | None = None
        self.on_actual_call = None

    def respond(self, tool_name: str) -> str:
        if tool_name == "list_knowledge_bases":
            if self.scope_payload is not None:
                return self.scope_payload
            payload = {"knowledge_bases": [
                {"id": identifier} for identifier in self.knowledge_base_ids]}
            return json.dumps({
                "structuredContent": payload,
                "content": [{"type": "text", "text": json.dumps(payload)}],
            }, ensure_ascii=False)
        if self.on_actual_call is not None:
            self.on_actual_call()
        return self.result


def install_managed_endpoint(monkeypatch, backend: FakeManagedEndpoint):
    @asynccontextmanager
    async def fake_client_session(**connection):
        backend.connections.append(connection)
        yield FakeEndpointSession(backend, connection)

    monkeypatch.setattr(mcp_client, "_client_session", fake_client_session)
    return backend


@pytest.fixture
def env(db, monkeypatch):
    api = FakeWeKnoraAPI()
    monkeypatch.setattr(settings, "weknora_base_url", FAKE_ORIGIN)
    monkeypatch.setattr(settings, "weknora_admin_email", ADMIN_EMAIL)
    monkeypatch.setattr(settings, "weknora_admin_password", ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "weknora_timeout_seconds", 5)
    monkeypatch.setattr(settings, "super_assistant_mcp_internal_base_urls", "")
    transport = httpx.MockTransport(api.handler)

    def factory(**kwargs):
        return WeKnoraClient(transport=transport, **kwargs)

    monkeypatch.setattr(connections, "WeKnoraClient", factory)

    backend = install_managed_endpoint(monkeypatch, FakeManagedEndpoint(["kb-1"]))
    # 失败撤销诊断走独立会话：指回测试引擎以便断言落库结果
    monkeypatch.setattr(execution, "SessionLocal", sessionmaker(bind=db.get_bind()))

    suffix = uuid.uuid4().hex[:8]
    owner = User(id=f"owner-{suffix}", username=f"owner-{suffix}",
                 email=f"owner-{suffix}@example.com",
                 password_hash="unused", role="admin")
    db.add(owner)
    db.commit()
    api.add_knowledge_base("kb-1", "Team KB")
    api.add_endpoint("ep-exec", "Team Endpoint", ["kb-1"])
    server = connections.connect_endpoint(
        db, owner.id, "ep-exec", token=api.tokens["ep-exec"])
    db.expire_all()
    return SimpleNamespace(api=api, backend=backend, owner=owner,
                           endpoint_id="ep-exec", server_id=server.id)


async def managed_call(db, env, *, tool_name="search_knowledge",
                       arguments=None, **overrides):
    server = db.query(SuperAssistantMcpServer).filter_by(id=env.server_id).one()
    payload = dict(
        server_id=server.id, owner_id=server.owner_id,
        endpoint_id=server.weknora_endpoint_id, scope=server.weknora_scope,
        transport=server.transport, url=server.url,
        headers=mcp_client.decrypt_headers(server.headers_encrypted),
        tool_name=tool_name, arguments=arguments or {"query": "本体治理"},
    )
    payload.update(overrides)
    return await execution.call_tool(**payload)


def refreshed_server(db, env) -> SuperAssistantMcpServer:
    db.expire_all()
    return db.query(SuperAssistantMcpServer).filter_by(id=env.server_id).one()


# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_managed_call_validates_scope_around_the_read(db, env):
    token = env.api.tokens["ep-exec"]

    result = await managed_call(db, env)

    assert result == env.backend.result
    names = [call["tool_name"] for call in env.backend.calls]
    assert names == ["list_knowledge_bases", "search_knowledge",
                     "list_knowledge_bases"]
    assert env.backend.calls[1]["arguments"] == {"query": "本体治理"}
    # 连接形状与 Bearer 头贯穿每一次会话
    for connection in env.backend.connections:
        assert connection["headers"] == {"Authorization": f"Bearer {token}"}
        assert connection["url"] == f"{FAKE_ORIGIN}/mcp/ep-exec"
        assert connection.get("command") is None and connection.get("args") == []
    server = refreshed_server(db, env)
    assert server.constrained is True
    assert server.last_test_status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"transport": "sse"},
    {"url": "https://attacker.example.com/mcp/ep-exec"},
    {"command": "npx", "args": ["-y", "evil-server"]},
    {"scope": None},
    {"tool_name": "delete_document"},
], ids=["foreign-transport", "foreign-url", "stdio-command",
        "scope-dropped", "unknown-tool"])
async def test_connection_or_request_drift_is_rejected_before_any_read(
    db, env, overrides,
):
    with pytest.raises(mcp_client.McpClientError):
        await managed_call(db, env, **overrides)

    assert env.backend.calls == []  # 任何知识读取都未发生
    server = refreshed_server(db, env)
    assert server.constrained is False
    assert server.last_test_status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [
    ["search_knowledge"],                # 缺范围校验工具
    READ_TOOLS + ["delete_document"],    # 混入非只读工具
], ids=["missing-scope-tool", "write-tool"])
async def test_unsafe_or_incomplete_manifest_rejects_hosting(db, env, tools):
    env.backend.tool_names = tools

    with pytest.raises(mcp_client.McpClientError) as excinfo:
        await managed_call(db, env)

    assert "重新验证" in str(excinfo.value)
    assert env.backend.calls == []
    server = refreshed_server(db, env)
    assert server.constrained is False
    assert server.last_test_status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["before", "during"])
async def test_scope_drift_before_or_during_read_rejects_and_revokes(
    db, env, mode,
):
    if mode == "before":
        env.backend.knowledge_base_ids = ["kb-1", "kb-9"]
    else:
        env.backend.on_actual_call = lambda: setattr(
            env.backend, "knowledge_base_ids", ["kb-9"])

    with pytest.raises(mcp_client.McpClientError) as excinfo:
        await managed_call(db, env)

    assert "知识库范围已变化" in str(excinfo.value)
    names = [call["tool_name"] for call in env.backend.calls]
    if mode == "before":
        assert names == ["list_knowledge_bases"]  # 实际读取未发生
    else:
        # 读取期间漂移：结果被丢弃，绝不返回给会话
        assert names == ["list_knowledge_bases", "search_knowledge",
                         "list_knowledge_bases"]
    server = refreshed_server(db, env)
    assert server.constrained is False
    assert server.last_test_status == "error"


@pytest.mark.asyncio
async def test_invalid_scope_payload_fails_closed(db, env):
    env.backend.scope_payload = json.dumps({"isError": True})

    with pytest.raises(mcp_client.McpClientError) as excinfo:
        await managed_call(db, env)

    assert "重新验证" in str(excinfo.value)
    server = refreshed_server(db, env)
    assert server.constrained is False


@pytest.mark.asyncio
async def test_failures_never_leak_the_endpoint_token(db, env):
    token = env.api.tokens["ep-exec"]
    env.backend.knowledge_base_ids = ["kb-2"]  # 制造一次范围漂移失败

    with pytest.raises(mcp_client.McpClientError) as excinfo:
        await managed_call(db, env)

    assert token not in str(excinfo.value)
    server = refreshed_server(db, env)
    assert token not in (server.last_test_message or "")
    assert token not in json.dumps(server.weknora_scope or {})
    for call in env.backend.calls:
        assert token not in json.dumps(call["arguments"])
    # Bearer 头是 token 唯一出现的位置
    for connection in env.backend.connections:
        assert connection["headers"] == {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_diagnostics_failure_still_denies_access(db, env, monkeypatch):
    class BrokenSessionFactory:
        def __call__(self):
            return self

        def __enter__(self):
            raise RuntimeError("diagnostic store down")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(execution, "SessionLocal", BrokenSessionFactory())
    env.backend.knowledge_base_ids = ["kb-2"]

    # 撤销诊断落库失败时同样拒绝，绝不因诊断故障放行
    with pytest.raises(mcp_client.McpClientError):
        await managed_call(db, env)
