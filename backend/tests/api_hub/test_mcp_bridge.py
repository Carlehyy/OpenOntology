"""mcp-bridge:// 接口的执行器分发单测。

不真实拉起 MCP 进程：call_tool 与 MCP Server 加载均被替换为确定性假件，
只验证分发、请求体解析、错误映射与调用审计（runs 落库）契约。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api_hub import config, db, executor, mcp_bridge
from app.api_hub.interface_contracts import InterfaceIn, KV
from app.api_hub.interface_service import create_interface
from app.super_assistant.mcp_client import McpClientError


SERVER_ID = "bridge-server-1"


@pytest.fixture
def bridge_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api_hub.db")
    db.init_db()
    return db


def _fake_server(**overrides):
    attrs = {
        "id": SERVER_ID,
        "transport": "stdio",
        "url": "",
        "headers_encrypted": None,
        "command": "npx",
        "args": ["-y", "worldbank-mcp"],
        "env_encrypted": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _tool_call_body(tool_name="search_dmp", arguments=None, request_id=7):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {"keyword": ""}},
        }
    )


def _create_bridge_interface(bridge_db, tool_name="search_dmp", body=None):
    return create_interface(
        InterfaceIn(
            name=f"桥接测试 · {tool_name}",
            description="bridge test",
            group_name="MCP 插件",
            method="POST",
            url=mcp_bridge.bridge_url(SERVER_ID, tool_name),
            headers=[KV(key="Content-Type", value="application/json")],
            body_type="json",
            body_content=body if body is not None else _tool_call_body(tool_name),
        )
    )


def _patch_backend(monkeypatch, server=None, calls=None, result_json='{"content": []}'):
    async def fake_call_tool(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if isinstance(result_json, Exception):
            raise result_json
        return result_json

    # executor.mcp_bridge 与 mcp_bridge 是同一模块对象，补丁一处即全覆盖
    monkeypatch.setattr(
        mcp_bridge, "_load_server",
        lambda server_id: server if server else _fake_server(),
    )
    monkeypatch.setattr(mcp_bridge.mcp_client, "call_tool", fake_call_tool)


def test_run_interface_dispatches_bridge_and_records_run(bridge_db, monkeypatch):
    calls: list[dict] = []
    _patch_backend(
        monkeypatch,
        server=_fake_server(),
        calls=calls,
        result_json='{"content": [{"type": "text", "text": "ok"}]}',
    )
    iface = _create_bridge_interface(bridge_db)

    result = executor.run_interface(iface)

    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["content_type"] == "application/json"
    envelope = json.loads(result["response_body"])
    assert envelope["id"] == 7
    assert envelope["result"] == {"content": [{"type": "text", "text": "ok"}]}
    # 服务端以原生传输调用 MCP，凭据在桥接内解密，不进入接口配置
    assert calls[0]["transport"] == "stdio"
    assert calls[0]["tool_name"] == "search_dmp"
    assert calls[0]["arguments"] == {"keyword": ""}
    assert calls[0]["command"] == "npx"
    with db.get_conn() as conn:
        run = conn.execute(
            "SELECT ok, status_code, request_snapshot, response_body FROM runs "
            "WHERE interface_id = ?",
            (iface["id"],),
        ).fetchone()
    assert run["ok"] == 1
    assert run["status_code"] == 200
    snapshot = json.loads(run["request_snapshot"])
    assert snapshot["url"] == f"mcp-bridge://{SERVER_ID}/search_dmp"
    assert "tools/call" in snapshot["body_content"]
    assert json.loads(run["response_body"])["result"]["content"][0]["text"] == "ok"


def test_bridge_body_override_replaces_template_arguments(bridge_db, monkeypatch):
    calls: list[dict] = []
    _patch_backend(monkeypatch, calls=calls)
    iface = _create_bridge_interface(bridge_db)
    overrides = executor.RequestOverrides(
        body=_tool_call_body(arguments={"keyword": "gdp"}, request_id=9),
        source="ui",
    )

    # preview-run/raw 与 n8n 代理按原始字节回传：include_response_content 时必须填充
    raw = executor.run_interface(dict(iface), overrides, include_response_content=True)

    assert raw["ok"] is True
    assert raw["response_content"] == raw["response_body"].encode()
    assert calls[0]["arguments"] == {"keyword": "gdp"}
    assert json.loads(raw["response_body"])["id"] == 9


def test_bridge_rejects_tool_name_mismatch(bridge_db, monkeypatch):
    _patch_backend(monkeypatch)
    iface = _create_bridge_interface(
        bridge_db,
        tool_name="search_dmp",
        body=_tool_call_body(tool_name="other_tool"),
    )

    result = executor.run_interface(iface)

    assert result["ok"] is False
    assert result["status_code"] == 400
    assert result["error_type"] == "configuration"
    assert "other_tool" in (result["error"] or "")


def test_bridge_rejects_invalid_json_body(bridge_db, monkeypatch):
    _patch_backend(monkeypatch)
    iface = _create_bridge_interface(bridge_db, body="not-json")

    result = executor.run_interface(iface)

    assert result["ok"] is False
    assert result["status_code"] == 400
    assert result["error_type"] == "configuration"


def test_bridge_reports_missing_server_as_404(bridge_db, monkeypatch):
    def missing(server_id):
        raise mcp_bridge.McpBridgeNotFoundError(f"MCP Server {server_id} 不存在或已删除")

    async def unreachable(**kwargs):  # pragma: no cover - 分流在前
        raise AssertionError("should not call MCP")

    monkeypatch.setattr(mcp_bridge, "_load_server", missing)
    monkeypatch.setattr(mcp_bridge.mcp_client, "call_tool", unreachable)
    iface = _create_bridge_interface(bridge_db)

    result = executor.run_interface(iface)

    assert result["ok"] is False
    assert result["status_code"] == 404
    assert result["error_type"] == "configuration"


def test_bridge_maps_mcp_failure_to_502(bridge_db, monkeypatch):
    _patch_backend(
        monkeypatch,
        result_json=McpClientError("无法连接 MCP Server: spawn failed"),
    )
    iface = _create_bridge_interface(bridge_db)

    result = executor.run_interface(iface)

    assert result["ok"] is False
    assert result["status_code"] == 502
    assert result["error_type"] == "upstream_mcp"
    assert "spawn failed" in (result["error"] or "")


def test_bridge_urls_are_rejected_by_outbound_http_validator():
    from app.api_hub.outbound_security import OutboundTargetError, validate_outbound_url

    # 失效安全：保留方案不是合法出站 HTTP 地址，绕过分发也无法发出请求
    with pytest.raises(OutboundTargetError):
        validate_outbound_url(f"mcp-bridge://{SERVER_ID}/search_dmp")


def test_parse_bridge_url_roundtrip_and_rejects_extra_segments():
    url = mcp_bridge.bridge_url("srv-1", "weird tool/name")
    assert mcp_bridge.parse_bridge_url(url) == ("srv-1", "weird tool/name")
    with pytest.raises(mcp_bridge.McpBridgeError):
        mcp_bridge.parse_bridge_url("mcp-bridge://only-server")
    with pytest.raises(mcp_bridge.McpBridgeError):
        mcp_bridge.parse_bridge_url("mcp-bridge:///no-server/tool")
    with pytest.raises(mcp_bridge.McpBridgeError):
        mcp_bridge.parse_bridge_url(f"mcp-bridge://srv-1/a/b")


@pytest.mark.parametrize("builtin_key", ["weknora", "minio", "api_hub"])
def test_bridge_rejects_platform_builtin_mcp_servers(bridge_db, monkeypatch, builtin_key):
    calls: list[dict] = []
    _patch_backend(
        monkeypatch,
        server=_fake_server(builtin_key=builtin_key),
        calls=calls,
    )
    iface = _create_bridge_interface(bridge_db)

    result = executor.run_interface(iface)

    assert result["ok"] is False
    assert result["status_code"] == 400
    assert result["error_type"] == "configuration"
    assert "平台内置 MCP" in (result["error"] or "")
    assert calls == []
