"""Hash MCP connection manifests and freeze their capability revisions."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.super_assistant.kernel.capability_service import (
    persist_capability_revision,
    set_capability_revision_enabled,
)
from app.super_assistant.kernel.connectors import (
    AgentDescriptor,
    SessionPolicy,
    TrustLevel,
)
from app.super_assistant.mcp_client import namespaced_tool_name
from app.super_assistant.models import SuperAssistantMcpServer


def manifest_keys(server: SuperAssistantMcpServer) -> list[str]:
    return [
        namespaced_tool_name(server.name, str(tool.get("name") or ""))
        for tool in (server.tool_manifest or [])
        if isinstance(tool, dict) and tool.get("name")
    ]


def manifest_hash(server: SuperAssistantMcpServer, tools: list[dict]) -> str:
    value = {
        "transport": server.transport, "url": server.url,
        "command": server.command, "args": server.args or [],
        "headers": server.headers_encrypted, "env": server.env_encrypted,
        "tools": tools,
    }
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def freeze_manifest(
    db: Session,
    server: SuperAssistantMcpServer,
    tools: list[dict],
    revision: int,
    *,
    enabled: bool = True,
    side_effect_class: str = "external_async",
) -> None:
    digest = manifest_hash(server, tools)
    for tool in tools:
        name = str(tool.get("name") or "") if isinstance(tool, dict) else ""
        if not name:
            continue
        key = namespaced_tool_name(server.name, name)
        descriptor = AgentDescriptor(
            agent_id=f"mcp:{server.id}:{name}", key=key, revision=revision,
            transport="mcp", session_policy=SessionPolicy.STATELESS,
        )
        persist_capability_revision(
            db, descriptor, source="mcp", trust_level=TrustLevel.USER_UNTRUSTED,
            manifest_hash=digest, side_effect_class=side_effect_class,
        )
        set_capability_revision_enabled(db, key, revision, enabled=enabled)
