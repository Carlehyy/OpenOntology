"""Revalidate managed knowledge endpoints using only their scoped MCP credential."""
from __future__ import annotations

import asyncio
import json
import logging

from app.shared.database import SessionLocal
from app.super_assistant import mcp_client
from app.super_assistant.weknora.client import WeKnoraError, connection_url
from app.super_assistant.weknora.policy import READ_TOOLS


logger = logging.getLogger(__name__)


def _revoke(server_id: str, owner_id: str, message: str) -> None:
    from app.super_assistant.models import SuperAssistantMcpServer
    from app.super_assistant.weknora.status import invalidate

    try:
        with SessionLocal() as db:
            server = db.query(SuperAssistantMcpServer).filter_by(id=server_id, owner_id=owner_id).first()
            if server is not None:
                invalidate(db, server, message)
    except Exception:
        # Failure to record diagnostics must never grant a rejected call access.
        logger.warning("WEKNORA_CONNECTION_DIAGNOSTIC_FAILED")


def _knowledge_ids(serialized: str) -> list[str]:
    try:
        result = json.loads(serialized)
        if result.get("isError") is True:
            raise ValueError("endpoint scope rejected")
        payload = result.get("structuredContent")
        if payload is None:
            text = next(item["text"] for item in result["content"] if item.get("type") == "text")
            payload = json.loads(text)
        knowledge_bases = payload["knowledge_bases"]
        if not isinstance(knowledge_bases, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in knowledge_bases):
            raise ValueError("invalid knowledge scope")
        return sorted(item["id"] for item in knowledge_bases)
    except (ValueError, TypeError, KeyError, StopIteration):
        raise WeKnoraError("WEKNORA_SCOPE_INVALID", "WeKnora 返回了无效知识库范围，请重新验证", 409) from None


def _serialized(result) -> str:
    serialized = result.model_dump_json(by_alias=True, exclude_none=True)
    if len(serialized.encode("utf-8")) > 256 * 1024:
        raise mcp_client.McpClientError("MCP 工具响应超过 256 KiB 上限")
    return serialized


async def _validate_scope(session, scope: dict) -> None:
    result = await session.call_tool("list_knowledge_bases", arguments={})
    if _knowledge_ids(_serialized(result)) != scope.get("resolved_knowledge_base_ids"):
        raise WeKnoraError("WEKNORA_SCOPE_CHANGED", "WeKnora 知识库范围已变化，请在 MCP 配置中重新验证", 409)


async def call_tool(
    *, server_id: str, owner_id: str, endpoint_id: str, scope: dict,
    transport: str, url: str, headers: dict, tool_name: str, arguments: dict,
    command=None, args=None, env=None,
) -> str:
    try:
        if (not isinstance(scope, dict) or transport != "streamable_http" or command or args or env
                or url != connection_url(endpoint_id)):
            raise WeKnoraError("WEKNORA_CONNECTION_CHANGED", "WeKnora 连接配置已变化，请重新验证", 409)
        async with mcp_client._client_session(
            transport=transport, url=url, headers=headers, command=None, args=[], env={},
        ) as session:
            listed = await session.list_tools()
            tools = [mcp_client._tool_manifest_item(tool) for tool in listed.tools]
            names = {str(item.get("name") or "") for item in tools}
            if (not names or names - set(READ_TOOLS) or "list_knowledge_bases" not in names
                    or tool_name not in names or tools != scope.get("tool_manifest")):
                raise WeKnoraError("WEKNORA_TOOLS_CHANGED", "WeKnora 工具清单已变化，请在 MCP 配置中重新验证", 409)
            await _validate_scope(session, scope)
            result = _serialized(await session.call_tool(tool_name, arguments=arguments))
            # Workers need only the endpoint token, never admin credentials.
            await _validate_scope(session, scope)
            return result
    except Exception as exc:
        message = str(exc) if isinstance(exc, WeKnoraError) else "WeKnora MCP 当前不可用，请检查 token 和服务状态后重新验证"
        await asyncio.to_thread(_revoke, server_id, owner_id, message)
        raise mcp_client.McpClientError(message) from None
