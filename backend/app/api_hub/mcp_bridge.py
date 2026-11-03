"""MCP 桥接接口的执行适配：把 `mcp-bridge://` 接口分发为服务端 MCP 调用。

插件社区「转接口」对 streamable_http 之外的传输（stdio 本地进程、SSE 双通道
流式）无法生成直连单发 HTTP 接口；这类工具导出为保留方案
``mcp-bridge://<server_id>/<tool_name>`` 的接口，由执行器在本模块内进程内
分发：解析 JSON-RPC（tools/call）请求体，复用超级助手的 MCP 客户端以原生
传输调用目标 Server，再把结果包回 JSON-RPC 响应。凭据（请求头/环境变量）
全程留在服务端解密使用，不进入接口明文配置。

失效安全：保留方案不是合法的出站 HTTP(S) 地址，即便分发被绕过，
``outbound_security.validate_outbound_url`` 也会拒绝该 URL。
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import quote, unquote, urlsplit

from app.super_assistant import mcp_client
from app.super_assistant.models import SuperAssistantMcpServer

BRIDGE_SCHEME = "mcp-bridge"


class McpBridgeError(ValueError):
    """桥接目标或请求体不合法（映射为 4xx 配置错误）。"""


class McpBridgeNotFoundError(McpBridgeError):
    """桥接目标 MCP Server 不存在（映射为 404）。"""


def bridge_url(server_id: str, tool_name: str) -> str:
    return f"{BRIDGE_SCHEME}://{server_id}/{quote(tool_name, safe='')}"


def is_bridge_url(url: str) -> bool:
    return urlsplit(url or "").scheme.lower() == BRIDGE_SCHEME


def parse_bridge_url(url: str) -> tuple[str, str]:
    parts = urlsplit(url or "")
    server_id = (parts.netloc or "").strip()
    # 工具名经 quote(safe="") 编码后不会含裸斜杠；路径中出现斜杠即视为多段，拒绝
    raw_tool = (parts.path or "").lstrip("/")
    if not server_id or not raw_tool or "/" in raw_tool:
        raise McpBridgeError("MCP 桥接地址格式无效，应为 mcp-bridge://<server_id>/<tool_name>")
    return server_id, unquote(raw_tool)


def _request_body(iface: dict, overrides) -> str:
    body = getattr(overrides, "body", None)
    if isinstance(body, str) and body.strip():
        return body
    return str(iface.get("body_content") or "")


def _parse_tool_call(body_text: str, tool_name: str) -> tuple[dict, object]:
    try:
        payload = json.loads(body_text or "{}")
    except json.JSONDecodeError as exc:
        raise McpBridgeError("请求体不是合法 JSON，无法解析 tools/call 参数") from exc
    if not isinstance(payload, dict):
        raise McpBridgeError("请求体必须是 JSON-RPC 对象")
    request_id = payload.get("id", 1)
    params = payload.get("params")
    if not isinstance(params, dict):
        raise McpBridgeError("请求体缺少 JSON-RPC params（应为 tools/call 调用）")
    name = params.get("name")
    if name is not None and str(name) != tool_name:
        raise McpBridgeError(
            f"请求体工具名 {name!r} 与接口绑定的 {tool_name!r} 不一致"
        )
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise McpBridgeError("tools/call 的 arguments 必须是 JSON 对象")
    return arguments, request_id


def _reject_builtin(server: SuperAssistantMcpServer) -> None:
    if getattr(server, "builtin_key", None):
        # 平台内置通道（minio/api_hub/weknora）只允许超级助手按固定清单、
        # 内部端点与平台凭据执行，桥接导出会绕过只读白名单与凭据边界。
        raise McpBridgeError("平台内置 MCP 不支持 mcp-bridge 分发")


def _load_server(server_id: str) -> SuperAssistantMcpServer:
    from app.shared.database import SessionLocal

    with SessionLocal() as db:
        item = db.query(SuperAssistantMcpServer).filter(
            SuperAssistantMcpServer.id == server_id,
        ).first()
        if item is None:
            raise McpBridgeNotFoundError(f"MCP Server {server_id} 不存在或已删除")
        _reject_builtin(item)
        return item


def _error_result(status_code: int, message: str, error_type: str, elapsed_ms: int) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": message}},
        ensure_ascii=False,
    )
    return {
        "ok": False,
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
        "response_headers": {"content-type": "application/json"},
        "response_body": body,
        "response_content": None,
        "content_type": "application/json",
        "error": message,
        "error_type": error_type,
        "relogin": False,
    }


def run_bridge_interface(
    iface: dict,
    overrides,
    *,
    include_response_content: bool = False,
) -> tuple[dict, dict]:
    """执行一个 mcp-bridge:// 接口，返回 (result, snapshot)。"""
    start = time.perf_counter()
    url = (iface.get("url") or "").strip()
    snapshot = {
        "method": iface.get("method"),
        "url": url,
        "headers": [],
        "body_type": iface.get("body_type"),
        "body_content": _request_body(iface, overrides)[:4000],
        "source": getattr(overrides, "source", None),
        "proxy_key_name": getattr(overrides, "proxy_key_name", None),
        "source_ip": getattr(overrides, "source_ip", None),
    }
    try:
        server_id, tool_name = parse_bridge_url(url)
        arguments, request_id = _parse_tool_call(_request_body(iface, overrides), tool_name)
        server = _load_server(server_id)
        _reject_builtin(server)
    except McpBridgeNotFoundError as exc:
        result = _error_result(404, str(exc), "configuration", int((time.perf_counter() - start) * 1000))
        return result, snapshot
    except McpBridgeError as exc:
        result = _error_result(400, str(exc), "configuration", int((time.perf_counter() - start) * 1000))
        return result, snapshot

    try:
        raw = asyncio.run(
            mcp_client.call_tool(
                transport=server.transport,
                url=server.url,
                headers=mcp_client.decrypt_headers(server.headers_encrypted),
                tool_name=tool_name,
                arguments=arguments,
                command=server.command,
                args=server.args or [],
                env=mcp_client.decrypt_env(server.env_encrypted),
            )
        )
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            payload = {"content": [{"type": "text", "text": str(raw)}]}
    except Exception as exc:  # McpClientError 及进程/网络失败统一映射为上游错误
        result = _error_result(
            502,
            f"MCP 工具 {tool_name} 调用失败：{exc}",
            "upstream_mcp",
            int((time.perf_counter() - start) * 1000),
        )
        return result, snapshot

    body = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "result": payload},
        ensure_ascii=False,
    )
    result = {
        "ok": True,
        "status_code": 200,
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
        "response_headers": {"content-type": "application/json"},
        "response_body": body,
        # preview-run/raw 与 n8n 代理按原始字节回传上游响应
        "response_content": body.encode() if include_response_content else None,
        "content_type": "application/json",
        "error": None,
        "error_type": None,
        "relogin": False,
    }
    return result, snapshot


__all__ = [
    "BRIDGE_SCHEME",
    "McpBridgeError",
    "McpBridgeNotFoundError",
    "bridge_url",
    "is_bridge_url",
    "parse_bridge_url",
    "run_bridge_interface",
]
