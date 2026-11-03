from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from app.shared.config import settings
from app.super_assistant import mcp_client
from app.super_assistant.weknora.policy import WEKNORA_ALLOWED_TOOLS, WEKNORA_REQUIRED_TOOLS


READ_TOOLS = frozenset(WEKNORA_ALLOWED_TOOLS)
REQUIRED_TOOLS = WEKNORA_REQUIRED_TOOLS
_MAX_STATE_BYTES = 1_048_576


class WeKnoraError(RuntimeError):
    pass


@dataclass(frozen=True)
class Integration:
    base_url: str
    mcp_url: str
    mcp_token: str = field(repr=False)
    knowledge_base_id: str
    knowledge_base_name: str
    tools: list[dict]


def origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise WeKnoraError("WEKNORA_INVALID_URL") from exc
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise WeKnoraError("WEKNORA_INVALID_URL")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{host}{suffix}"


def validate_tools(tools: object) -> list[dict]:
    if not isinstance(tools, list):
        raise WeKnoraError("WEKNORA_INVALID_TOOLS")
    result: list[dict] = []
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise WeKnoraError("WEKNORA_INVALID_TOOLS")
        name = tool.get("name")
        if not isinstance(name, str) or name not in READ_TOOLS or name in names:
            raise WeKnoraError("WEKNORA_UNEXPECTED_TOOL")
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise WeKnoraError("WEKNORA_INVALID_TOOL_SCHEMA")
        names.add(name)
        result.append({
            "name": name,
            "description": str(tool.get("description") or ""),
            "input_schema": schema,
        })
    if not REQUIRED_TOOLS.issubset(names):
        raise WeKnoraError("WEKNORA_REQUIRED_TOOLS_MISSING")
    return result


def get_integration() -> Integration:
    path = Path(settings.weknora_state_path)
    try:
        if not path.is_absolute() or path.is_symlink():
            raise WeKnoraError("WEKNORA_INVALID_STATE_PATH")
        with path.open("rb") as handle:
            raw = handle.read(_MAX_STATE_BYTES + 1)
        if len(raw) > _MAX_STATE_BYTES:
            raise WeKnoraError("WEKNORA_STATE_TOO_LARGE")
        state = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise WeKnoraError("WEKNORA_NOT_INITIALIZED") from exc
    if not isinstance(state, dict) or state.get("version") != 1:
        raise WeKnoraError("WEKNORA_INVALID_STATE")
    if state.get("verified") is not True:
        raise WeKnoraError("WEKNORA_NOT_VERIFIED")
    base_url = origin(settings.weknora_base_url)
    if state.get("base_url") != origin(settings.weknora_state_origin):
        raise WeKnoraError("WEKNORA_STATE_ORIGIN_MISMATCH")
    endpoint = state.get("mcp_endpoint_id")
    token = state.get("mcp_token")
    kb_id = state.get("knowledge_base_id")
    name = state.get("knowledge_base_name")
    if (
        not isinstance(endpoint, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", endpoint)
        or not isinstance(token, str) or not re.fullmatch(r"mcp_[A-Za-z0-9_-]{16,256}", token)
        or not isinstance(kb_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", kb_id)
        or not isinstance(name, str) or not name.strip()
    ):
        raise WeKnoraError("WEKNORA_INVALID_STATE")
    return Integration(
        base_url=base_url,
        mcp_url=f"{base_url}/mcp/{endpoint}",
        mcp_token=token,
        knowledge_base_id=kb_id,
        knowledge_base_name=name,
        tools=validate_tools(state.get("tools")),
    )


async def discover_tools() -> list[dict]:
    integration = get_integration()
    try:
        async with asyncio.timeout(settings.weknora_timeout_seconds):
            tools = await mcp_client.discover_tools(
                transport="streamable_http",
                url=integration.mcp_url,
                headers={"Authorization": f"Bearer {integration.mcp_token}"},
                internal_base_url=integration.base_url,
            )
        return validate_tools(tools)
    except WeKnoraError:
        raise
    except Exception:
        raise WeKnoraError("WEKNORA_MCP_UNAVAILABLE") from None


async def call_tool(tool_name: str, arguments: dict) -> str:
    integration = get_integration()
    if tool_name not in {item["name"] for item in integration.tools}:
        raise WeKnoraError("WEKNORA_TOOL_DENIED")
    try:
        return await mcp_client.call_tool(
            transport="streamable_http",
            url=integration.mcp_url,
            headers={"Authorization": f"Bearer {integration.mcp_token}"},
            tool_name=tool_name,
            arguments=arguments,
            internal_base_url=integration.base_url,
        )
    except Exception:
        raise WeKnoraError("WEKNORA_TOOL_FAILED") from None


async def probe() -> None:
    integration = get_integration()
    try:
        async with asyncio.timeout(settings.weknora_timeout_seconds):
            async with httpx.AsyncClient(
                timeout=min(settings.weknora_timeout_seconds, 5),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(f"{integration.base_url}/health")
                if response.status_code != 200:
                    raise WeKnoraError("WEKNORA_HTTP_UNAVAILABLE")
            actual_tools = await discover_tools()
            expected = sorted(integration.tools, key=lambda item: item["name"])
            if sorted(actual_tools, key=lambda item: item["name"]) != expected:
                raise WeKnoraError("WEKNORA_TOOL_MANIFEST_CHANGED")
            result = await call_tool("list_knowledge_bases", {})
            payload = json.loads(result)
            if not isinstance(payload, dict) or payload.get("isError") is True:
                raise WeKnoraError("WEKNORA_KNOWLEDGE_BASE_UNAVAILABLE")
            if _knowledge_base_ids(payload) != [integration.knowledge_base_id]:
                raise WeKnoraError("WEKNORA_KNOWLEDGE_BASE_SCOPE_CHANGED")
    except WeKnoraError:
        raise
    except Exception:
        raise WeKnoraError("WEKNORA_UNAVAILABLE") from None


def _knowledge_base_ids(payload: dict) -> list[str]:
    structured = payload.get("structuredContent")
    if not isinstance(structured, dict):
        for content in payload.get("content") or []:
            if not isinstance(content, dict) or content.get("type") != "text":
                continue
            try:
                structured = json.loads(content.get("text") or "")
            except (ValueError, TypeError):
                continue
            if isinstance(structured, dict) and "knowledge_bases" in structured:
                break
    if not isinstance(structured, dict):
        raise WeKnoraError("WEKNORA_INVALID_KNOWLEDGE_BASE_RESPONSE")
    knowledge_bases = structured.get("knowledge_bases")
    if not isinstance(knowledge_bases, list):
        raise WeKnoraError("WEKNORA_INVALID_KNOWLEDGE_BASE_RESPONSE")
    identifiers = []
    for item in knowledge_bases:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise WeKnoraError("WEKNORA_INVALID_KNOWLEDGE_BASE_RESPONSE")
        identifiers.append(item["id"])
    return sorted(identifiers)
