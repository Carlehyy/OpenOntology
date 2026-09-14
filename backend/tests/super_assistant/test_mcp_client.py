from contextlib import asynccontextmanager

import pytest

from app.shared.config import settings
from app.super_assistant import mcp_client
from app.super_assistant.mcp_client import (
    McpClientError,
    _sse_http_client_factory,
    _error_message,
    namespaced_tool_name,
    normalize_connection,
    validate_mcp_url,
)


@pytest.mark.asyncio
async def test_sse_client_factory_disables_redirects():
    client = _sse_http_client_factory(headers={"X-API-Key": "secret"})
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.headers["X-API-Key"] == "secret"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_discovery_never_follows_redirect_to_private_host(monkeypatch):
    import httpx

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    class MockClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    with pytest.raises(McpClientError, match="302"):
        await mcp_client.discover_tools(
            transport="sse", url="https://93.184.216.34/sse",
            headers={"X-API-Key": "test-only-key"},
        )
    assert len(requests) == 1
    assert str(requests[0].url) == "https://93.184.216.34/sse"


def test_mcp_url_allows_public_targets_without_a_host_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(mcp_client.socket, "getaddrinfo", lambda host, port, **_kwargs: [
        (None, None, None, None, ("93.184.216.34", port)),
    ])
    assert validate_mcp_url("https://38.76.215.169:8765/mcp") == "https://38.76.215.169:8765/mcp"
    assert validate_mcp_url("https://tools.example.com/mcp") == "https://tools.example.com/mcp"
    with pytest.raises(McpClientError, match="不能内嵌"):
        validate_mcp_url("https://user:secret@tools.example.com/mcp")


def test_external_integrations_require_https_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "super_assistant_external_https_required", True)
    with pytest.raises(McpClientError, match="必须使用 HTTPS"):
        validate_mcp_url("http://93.184.216.34/mcp", require_https=True)
    assert validate_mcp_url("https://93.184.216.34/mcp", require_https=True).startswith("https://")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:3000/mcp",
    "http://10.2.3.4/mcp",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]:3000/mcp",
])
def test_mcp_url_blocks_nonpublic_targets_in_production(monkeypatch, url):
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(McpClientError, match="非公网地址"):
        validate_mcp_url(url)


def test_mcp_url_permits_local_development_targets(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    assert validate_mcp_url("http://127.0.0.1:3000/mcp") == "http://127.0.0.1:3000/mcp"


def test_tool_namespace_is_stable_and_provider_safe():
    name = namespaced_tool_name("my server", "a.tool/with spaces")
    assert name == "mcp__my_server__a_tool_with_spaces"
    long_name = namespaced_tool_name("server" * 20, "tool" * 20)
    assert len(long_name) == 64
    assert long_name == namespaced_tool_name("server" * 20, "tool" * 20)


def test_anyio_exception_groups_are_unwrapped_for_users():
    error = ExceptionGroup("task group", [ConnectionError("connection refused")])
    assert _error_message(error) == "connection refused"


def test_normalizes_mcp_remote_wrapper_to_direct_streamable_http(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    assert normalize_connection(
        transport="stdio",
        command="npx",
        args=["-y", "mcp-remote", "https://38.76.215.169:8765/mcp"],
    ) == (
        "streamable_http",
        "https://38.76.215.169:8765/mcp",
        None,
        [],
    )


def test_validates_native_stdio_and_legacy_sse(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    assert normalize_connection(
        transport="stdio", command="npx", args=["-y", "@example/mcp-server"],
    ) == ("stdio", "", "npx", ["-y", "@example/mcp-server"])
    assert normalize_connection(
        transport="sse", url="http://127.0.0.1:3000/sse",
    ) == ("sse", "http://127.0.0.1:3000/sse", None, [])


@pytest.mark.asyncio
async def test_call_tool_rejects_oversized_serialized_result(monkeypatch):
    class FakeSession:
        async def call_tool(self, tool_name, arguments):
            return {"content": "x" * (256 * 1024)}

    @asynccontextmanager
    async def fake_session(**kwargs):
        yield FakeSession()

    monkeypatch.setattr(mcp_client, "_client_session", fake_session)
    with pytest.raises(McpClientError, match="256 KiB"):
        await mcp_client.call_tool(
            transport="stdio", url="", headers={}, tool_name="large", arguments={},
            command="python", args=[], env={},
        )
