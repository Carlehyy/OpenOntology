"""WeKnora startup/dependency-probe boundaries.

The real probe implementation runs against a synthetic integration state; only
the network transport and MCP client are faked.  These tests prove that a bad
credential, an unreachable knowledge base or a changed MCP manifest blocks
startup and never renders secrets, and that the probe is safe to run from the
thread lifecycle offloads it to.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.shared import dependency_probe


BASE_URL = "http://weknora-app:8080"
MCP_TOKEN = "mcp_synthetic_dependency_token_not_a_secret"
MANIFEST = [
    {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }
    for name in ("list_knowledge_bases", "read_document", "search_knowledge")
]


def _install_state(tmp_path, monkeypatch):
    from app.super_assistant.weknora import service

    state = {
        "version": 1,
        "verified": True,
        "base_url": BASE_URL,
        "mcp_endpoint_id": "endpoint-test",
        "mcp_token": MCP_TOKEN,
        "knowledge_base_id": "kb-test",
        "knowledge_base_name": "共享知识库",
        "tools": MANIFEST,
    }
    path = tmp_path / "integration.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(service.settings, "weknora_state_path", str(path))
    monkeypatch.setattr(service.settings, "weknora_base_url", BASE_URL)
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )
    return service


def _apply_failure(failure, service, monkeypatch):
    if failure == "bad-token":
        monkeypatch.setattr(
            service.mcp_client,
            "discover_tools",
            AsyncMock(side_effect=RuntimeError(
                f"HTTP 401 unauthorized Bearer {MCP_TOKEN}")),
        )
    elif failure == "knowledge-base-unreachable":
        monkeypatch.setattr(
            service.mcp_client,
            "discover_tools",
            AsyncMock(return_value=list(MANIFEST)),
        )
        monkeypatch.setattr(
            service.mcp_client,
            "call_tool",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        )
    elif failure == "manifest-fault":
        changed = [
            {
                **MANIFEST[0],
                "input_schema": {
                    "type": "object",
                    "required": ["unexpected_argument"],
                },
            },
            *MANIFEST[1:],
        ]
        monkeypatch.setattr(
            service.mcp_client,
            "discover_tools",
            AsyncMock(return_value=changed),
        )
        monkeypatch.setattr(service.mcp_client, "call_tool", AsyncMock())
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(f"unknown failure: {failure}")


@pytest.mark.asyncio
async def test_probe_weknora_runs_from_the_lifecycle_worker_thread(
    tmp_path, monkeypatch,
):
    """The MCP probe owns its event loop; it must run off the request loop."""
    service = _install_state(tmp_path, monkeypatch)
    probe = AsyncMock()
    monkeypatch.setattr(service, "probe", probe)

    await asyncio.to_thread(dependency_probe.probe_weknora)

    probe.assert_awaited_once_with()


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize(
    "failure",
    ["bad-token", "knowledge-base-unreachable", "manifest-fault"],
)
def test_weknora_failure_blocks_startup_in_every_environment(
    failure, environment, tmp_path, monkeypatch,
):
    service = _install_state(tmp_path, monkeypatch)
    _apply_failure(failure, service, monkeypatch)
    monkeypatch.setattr(dependency_probe.settings, "environment", environment)
    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("WeKnora", dependency_probe.probe_weknora),),
    )

    with pytest.raises(RuntimeError) as exc_info:
        dependency_probe.probe_startup_dependencies()

    assert str(exc_info.value) == (
        "Required startup dependencies unavailable: WeKnora"
    )
    assert MCP_TOKEN not in str(exc_info.value)


def test_weknora_probe_success_does_not_block_startup(tmp_path, monkeypatch):
    service = _install_state(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service.mcp_client,
        "discover_tools",
        AsyncMock(return_value=list(MANIFEST)),
    )
    payload = {
        "structuredContent": {"knowledge_bases": [{"id": "kb-test"}]},
    }
    call_tool = AsyncMock(return_value=json.dumps(payload))
    monkeypatch.setattr(service.mcp_client, "call_tool", call_tool)
    monkeypatch.setattr(dependency_probe.settings, "environment", "production")
    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("WeKnora", dependency_probe.probe_weknora),),
    )

    dependency_probe.probe_startup_dependencies()

    call_tool.assert_awaited_once()
    assert call_tool.await_args.kwargs["tool_name"] == "list_knowledge_bases"


def test_preflight_fails_closed_without_rendering_weknora_secrets(
    tmp_path, monkeypatch, capsys,
):
    service = _install_state(tmp_path, monkeypatch)
    _apply_failure("bad-token", service, monkeypatch)
    monkeypatch.setattr(dependency_probe.settings, "environment", "development")
    monkeypatch.setattr(
        dependency_probe,
        "PROBES",
        (("WeKnora", dependency_probe.probe_weknora),),
    )

    assert dependency_probe.main() == 1

    captured = capsys.readouterr()
    assert "WeKnora: unavailable (WeKnoraError)" in captured.err
    assert "Required production dependency preflight failed: WeKnora" in (
        captured.err
    )
    assert MCP_TOKEN not in captured.out + captured.err
    assert "401" not in captured.out + captured.err
