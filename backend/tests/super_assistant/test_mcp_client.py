from contextlib import asynccontextmanager
import subprocess
import sys
from pathlib import Path

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

_BACKEND_DIR = Path(__file__).resolve().parents[2]


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


def test_internal_base_url_allows_fixed_docker_http_endpoint_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    endpoint = "http://weknora:8080/mcp/endpoint-1"
    assert validate_mcp_url(
        endpoint, require_https=True, internal_base_url="http://weknora:8080",
    ) == endpoint
    # base 带路径前缀时，端点必须留在该前缀内。
    assert validate_mcp_url(
        "https://gateway.example.com/weknora/mcp/endpoint-1",
        require_https=True,
        internal_base_url="https://gateway.example.com/weknora",
    ).endswith("/weknora/mcp/endpoint-1")


@pytest.mark.parametrize(("url", "base", "match"), [
    ("http://weknora:9090/mcp/endpoint-1", "http://weknora:8080", "不同源"),
    ("https://weknora:8080/mcp/endpoint-1", "http://weknora:8080", "不同源"),
    ("http://weknora:8080/admin", "http://weknora:8080/weknora", "路径越出"),
    ("http://user:secret@weknora:8080/mcp/x", "http://weknora:8080", "不能内嵌"),
    ("http://weknora:8080/mcp/x?token=1", "http://weknora:8080", "查询串"),
    ("http://weknora:8080/mcp/x#frag", "http://weknora:8080", "查询串"),
    ("http://169.254.169.254/mcp/x", "http://169.254.169.254", "链路本地"),
    ("http://metadata.google.internal/mcp/x", "http://metadata.google.internal", "元数据"),
])
def test_internal_base_url_rejects_untrusted_endpoints(monkeypatch, url, base, match):
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(McpClientError, match=match):
        validate_mcp_url(url, require_https=True, internal_base_url=base)


def test_internal_base_url_does_not_weaken_default_external_rules(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(McpClientError, match="必须使用 HTTPS"):
        validate_mcp_url("http://weknora:8080/mcp/endpoint-1", require_https=True)
    with pytest.raises(McpClientError, match="非公网地址"):
        validate_mcp_url("http://127.0.0.1:8080/mcp/endpoint-1")


@pytest.mark.asyncio
async def test_internal_base_url_is_forwarded_to_the_client_session(monkeypatch):
    seen: dict = {}

    class FakeSession:
        async def list_tools(self):
            class Result:
                tools = []

            return Result()

    @asynccontextmanager
    async def fake_session(**kwargs):
        seen.update(kwargs)
        yield FakeSession()

    monkeypatch.setattr(mcp_client, "_client_session", fake_session)
    await mcp_client.discover_tools(
        transport="streamable_http",
        url="http://weknora:8080/mcp/endpoint-1",
        headers={},
        internal_base_url="http://weknora:8080",
    )
    assert seen["internal_base_url"] == "http://weknora:8080"


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


def test_bootstrap_mcp_client_imports_without_platform_configuration():
    """The WeKnora init container has only bootstrap variables.

    Importing ``bootstrap._default_mcp`` must not trigger the platform
    settings/encryption modules; the internal endpoint validator must work
    under ``env -i`` while external validation still requires platform config.
    """
    script = """
import sys

from app.super_assistant.weknora.bootstrap import _default_mcp

client = _default_mcp()
assert "app.shared.config" not in sys.modules
assert "app.shared.encryption" not in sys.modules
assert client.validate_mcp_url(
    "http://weknora-app:8080/mcp/endpoint-1",
    internal_base_url="http://weknora-app:8080",
) == "http://weknora-app:8080/mcp/endpoint-1"

try:
    client.validate_mcp_url("https://example.com/mcp", require_https=True)
except Exception:
    pass
else:
    raise SystemExit("external validation unexpectedly succeeded without config")

print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_BACKEND_DIR,
        env={"PYTHONPATH": str(_BACKEND_DIR)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
