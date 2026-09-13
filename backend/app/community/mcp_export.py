"""把插件社区 MCP Server 的工具导出为接口代理（API Hub）的 HTTP 接口。

两种导出形态：

- streamable_http：生成直连 MCP Server 的单发 JSON-RPC POST 接口
  （``{"jsonrpc":"2.0","method":"tools/call",...}``）。MCP 配置的请求头由
  后端解密后原样带入（接口管理本身即明文存储请求头，与平台现状一致）。
- stdio / sse：本地进程与流式双通道无法以直连单发 HTTP 表达，改为生成
  ``mcp-bridge://<server_id>/<tool>`` 保留方案接口，由接口代理执行器在
  服务端进程内代为调用 MCP 并回传 JSON-RPC 响应；凭据不出服务端。

边界说明：直连形态仅适用于无状态 MCP Server——有状态 MCP Server（要求
initialize 握手 / Mcp-Session-Id 会话头）无法用单发 POST 调用，该限制会写进
生成接口的描述中，无状态网关类（如 mcpgateway、本平台 api-hub 自带 MCP）
可直接调用；桥接形态由平台持有完整 MCP 会话，不受该限制约束。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.api_hub import db as api_hub_db
from app.api_hub.interface_contracts import InterfaceIn, KV
from app.api_hub.interface_service import create_interface
from app.api_hub.mcp_bridge import bridge_url
from app.super_assistant import mcp_server_service
from app.super_assistant.mcp_client import decrypt_headers
from app.super_assistant.models import SuperAssistantMcpServer


EXPORT_GROUP_NAME = "MCP 插件"
_STATELESS_HINT = (
    "注意：该接口为单发 JSON-RPC（tools/call），仅适用于无状态 MCP Server；"
    "要求 initialize 握手或会话头的有状态服务可能无法直接调用。"
)
_BRIDGED_HINT = (
    "注意：该接口由平台桥接调用 MCP（服务端原生传输），凭据保留在服务端，"
    "调用由平台进程代为执行。"
)


class McpExportIn(BaseModel):
    tool_names: list[str] = Field(min_length=1, max_length=100)


class McpExportOut(BaseModel):
    created: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)


def _placeholder(schema: dict[str, Any]) -> Any:
    if "default" in schema:
        return schema["default"]
    value_type = schema.get("type", "string")
    if value_type == "object":
        return {}
    if value_type == "array":
        return []
    if value_type in {"integer", "number"}:
        return 0
    if value_type == "boolean":
        return False
    return ""


def json_rpc_body(tool_name: str, input_schema: dict[str, Any]) -> str:
    """按工具 input_schema 生成可编辑的 JSON-RPC tools/call 请求模板。"""
    properties = input_schema.get("properties") or {} if isinstance(input_schema, dict) else {}
    arguments = {
        str(key): _placeholder(schema if isinstance(schema, dict) else {})
        for key, schema in properties.items()
    }
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        ensure_ascii=False,
        indent=2,
    )


def interface_name(server: SuperAssistantMcpServer, tool_name: str) -> str:
    return f"{server.display_name or server.name} · {tool_name}"[:200]


def build_interface(
    server: SuperAssistantMcpServer,
    tool: dict[str, Any],
    headers: dict[str, str],
) -> InterfaceIn:
    tool_name = str(tool.get("name") or "")
    description = str(tool.get("description") or "").strip()
    summary = f"MCP 工具 {tool_name}（来自 {server.display_name or server.name}）。"
    if server.transport == "streamable_http":
        target_url = server.url
        hint = _STATELESS_HINT
        tool_headers = [
            KV(key="Content-Type", value="application/json"),
            KV(key="Accept", value="application/json, text/event-stream"),
            *(KV(key=key, value=value) for key, value in headers.items()),
        ]
    else:
        target_url = bridge_url(server.id, tool_name)
        hint = _BRIDGED_HINT
        tool_headers = [
            KV(key="Content-Type", value="application/json"),
            KV(key="Accept", value="application/json"),
        ]
    return InterfaceIn(
        name=interface_name(server, tool_name),
        description=f"{summary}{description} {hint}"[:20_000],
        group_name=EXPORT_GROUP_NAME,
        method="POST",
        url=target_url,
        headers=tool_headers,
        body_type="json",
        body_content=json_rpc_body(
            str(tool.get("name") or ""),
            tool.get("input_schema") or {},
        ),
    )


def export_server_tools(
    db,
    owner_id: str,
    server_id: str,
    tool_names: list[str],
) -> McpExportOut:
    server = mcp_server_service.get_mcp_server(
        db,
        owner_id,
        server_id,
        include_builtins=False,
    )
    if server.transport == "developed":
        # 自研 MCP 的桥接执行分支尚未落地：现在放行只会生成运行时必然
        # 失败的 mcp-bridge:// 接口，必须在创建前拒绝而不是留死接口
        raise mcp_server_service.McpServerValidationError(
            "自研 MCP 暂不支持转接口：请经超级助手对话调用，或等待后续版本支持"
        )
    manifest = {str(tool.get("name") or ""): tool for tool in server.tool_manifest}
    if not manifest:
        raise mcp_server_service.McpServerValidationError(
            "该 MCP Server 尚未发现工具，请先执行连接测试"
        )
    unknown = [name for name in tool_names if name not in manifest]
    if unknown:
        raise mcp_server_service.McpServerValidationError(
            f"未在工具清单中找到：{', '.join(unknown)}"
        )

    headers = decrypt_headers(server.headers_encrypted)
    result = McpExportOut()
    with api_hub_db.get_conn() as conn:
        existing = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM interfaces WHERE name IN "
                f"({','.join('?' for _ in tool_names)})",
                tuple(interface_name(server, name) for name in tool_names),
            ).fetchall()
        }
    for name in tool_names:
        target_name = interface_name(server, name)
        if target_name in existing:
            result.skipped.append(
                {"tool": name, "reason": f"同名接口「{target_name}」已存在，已跳过"}
            )
            continue
        try:
            created = create_interface(
                build_interface(server, manifest[name], headers)
            )
        except ValueError as exc:
            raise mcp_server_service.McpServerValidationError(
                f"生成接口失败（工具 {name}）：{exc}"
            ) from exc
        result.created.append(
            {"id": created["id"], "name": created["name"], "tool": name}
        )
    return result


__all__ = [
    "EXPORT_GROUP_NAME",
    "McpExportIn",
    "McpExportOut",
    "build_interface",
    "export_server_tools",
    "interface_name",
    "json_rpc_body",
]
