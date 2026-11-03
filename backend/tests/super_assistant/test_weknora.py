from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.super_assistant.weknora import service


def _manifest() -> list[dict]:
    return [
        {"name": name, "description": name, "input_schema": {"type": "object", "properties": {}}}
        for name in sorted(service.REQUIRED_TOOLS)
    ]


@pytest.fixture
def integration_state(tmp_path, monkeypatch):
    state = {
        "version": 1,
        "verified": True,
        "base_url": "http://weknora-app:8080",
        "mcp_endpoint_id": "endpoint-test",
        "mcp_token": "mcp_synthetic_token_not_a_real_secret",
        "knowledge_base_id": "kb-test",
        "knowledge_base_name": "共享知识库",
        "tools": _manifest(),
    }
    path = tmp_path / "integration.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(service.settings, "weknora_state_path", str(path))
    monkeypatch.setattr(service.settings, "weknora_base_url", state["base_url"])
    monkeypatch.setattr(service.settings, "weknora_state_origin", state["base_url"])
    return path, state


def test_state_contains_only_configured_origin_and_safe_manifest(integration_state):
    _, state = integration_state
    integration = service.get_integration()
    assert integration.mcp_url == state["base_url"] + "/mcp/endpoint-test"
    assert integration.knowledge_base_id == "kb-test"
    assert state["mcp_token"] not in repr(integration)


@pytest.mark.parametrize("change", [
    {"base_url": "http://other-service:8080"},
    {"mcp_endpoint_id": "../auth/register"},
    {"mcp_token": "invalid"},
    {"tools": [{"name": "delete_document", "input_schema": {"type": "object"}}]},
    {"tools": [{"name": "ask", "input_schema": {"type": "object"}}]},
    {"tools": []},
    {"verified": False},
    {"verified": None},
    {"verified": "true"},
    {"verified": 1},
])
def test_state_rejects_wrong_origin_credentials_and_write_tools(integration_state, change):
    path, state = integration_state
    path.write_text(json.dumps({**state, **change}), encoding="utf-8")
    with pytest.raises(service.WeKnoraError):
        service.get_integration()


def test_host_transport_requires_explicit_matching_state_origin(integration_state, monkeypatch):
    _, state = integration_state
    monkeypatch.setattr(service.settings, "weknora_base_url", "http://127.0.0.1:18080")
    integration = service.get_integration()
    assert integration.base_url == "http://127.0.0.1:18080"
    assert integration.mcp_url == "http://127.0.0.1:18080/mcp/endpoint-test"
    assert integration.mcp_token == state["mcp_token"]
    monkeypatch.setattr(service.settings, "weknora_state_origin", "http://different-instance:8080")
    with pytest.raises(service.WeKnoraError, match="WEKNORA_STATE_ORIGIN_MISMATCH"):
        service.get_integration()


def test_unverified_state_never_installs_integration(integration_state):
    path, state = integration_state
    state.pop("verified")
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(service.WeKnoraError, match="WEKNORA_NOT_VERIFIED"):
        service.get_integration()


def test_missing_state_never_installs_empty_integration(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "weknora_state_path", str(tmp_path / "missing.json"))
    with pytest.raises(service.WeKnoraError, match="WEKNORA_NOT_INITIALIZED"):
        service.get_integration()


def test_state_symlink_is_rejected(integration_state, tmp_path, monkeypatch):
    path, _ = integration_state
    link = tmp_path / "link.json"
    link.symlink_to(path)
    monkeypatch.setattr(service.settings, "weknora_state_path", str(link))
    with pytest.raises(service.WeKnoraError, match="WEKNORA_INVALID_STATE_PATH"):
        service.get_integration()


@pytest.mark.asyncio
async def test_runtime_denies_write_before_network(integration_state, monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(service.mcp_client, "call_tool", call)
    with pytest.raises(service.WeKnoraError, match="WEKNORA_TOOL_DENIED"):
        await service.call_tool("delete_document", {"knowledge_id": "doc-test"})
    call.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_and_call_use_only_server_configured_internal_transport(integration_state, monkeypatch):
    discovery = AsyncMock(return_value=_manifest())
    call = AsyncMock(return_value='{"content": []}')
    monkeypatch.setattr(service.mcp_client, "discover_tools", discovery)
    monkeypatch.setattr(service.mcp_client, "call_tool", call)
    assert await service.discover_tools() == _manifest()
    await service.call_tool("search_knowledge", {"query": "synthetic"})
    assert discovery.call_args.kwargs["internal_base_url"] == "http://weknora-app:8080"
    assert call.call_args.kwargs["url"] == "http://weknora-app:8080/mcp/endpoint-test"
    assert call.call_args.kwargs["arguments"] == {"query": "synthetic"}


@pytest.mark.asyncio
async def test_network_failure_does_not_publish_secrets(integration_state, monkeypatch):
    _, state = integration_state
    monkeypatch.setattr(service.mcp_client, "discover_tools", AsyncMock(side_effect=RuntimeError(state["mcp_token"])))
    with pytest.raises(service.WeKnoraError) as failure:
        await service.discover_tools()
    assert str(failure.value) == "WEKNORA_MCP_UNAVAILABLE"
    assert state["mcp_token"] not in str(failure.value)
    assert failure.value.__suppress_context__ is True


@pytest.mark.asyncio
@pytest.mark.parametrize("knowledge_base_ids,expected", [
    (["kb-test"], None),
    ([], "WEKNORA_KNOWLEDGE_BASE_SCOPE_CHANGED"),
    (["kb-test", "other-kb"], "WEKNORA_KNOWLEDGE_BASE_SCOPE_CHANGED"),
])
async def test_probe_requires_exactly_one_shared_knowledge_base(integration_state, monkeypatch, knowledge_base_ids, expected):
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: async_client(transport=transport, **kwargs))
    monkeypatch.setattr(service, "discover_tools", AsyncMock(return_value=_manifest()))
    payload = {"structuredContent": {"knowledge_bases": [{"id": value} for value in knowledge_base_ids]}}
    call = AsyncMock(return_value=json.dumps(payload))
    monkeypatch.setattr(service, "call_tool", call)
    if expected:
        with pytest.raises(service.WeKnoraError, match=expected):
            await service.probe()
    else:
        await service.probe()
    call.assert_awaited_once_with("list_knowledge_bases", {})


@pytest.mark.asyncio
async def test_probe_rejects_changed_tool_schema_before_tool_execution(integration_state, monkeypatch):
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: async_client(transport=transport, **kwargs))
    tools = _manifest()
    tools[0] = {**tools[0], "input_schema": {"type": "object", "required": ["new_argument"]}}
    monkeypatch.setattr(service, "discover_tools", AsyncMock(return_value=tools))
    call = AsyncMock()
    monkeypatch.setattr(service, "call_tool", call)
    with pytest.raises(service.WeKnoraError, match="WEKNORA_TOOL_MANIFEST_CHANGED"):
        await service.probe()
    call.assert_not_called()


def test_mcp_knowledge_base_response_requires_structured_ids():
    assert service._knowledge_base_ids({"content": [{"type": "text", "text": '{"knowledge_bases":[{"id":"kb-test"}]}'}]}) == ["kb-test"]
    with pytest.raises(service.WeKnoraError):
        service._knowledge_base_ids({"content": [{"type": "text", "text": "knowledge_base_id=kb-test"}]})
