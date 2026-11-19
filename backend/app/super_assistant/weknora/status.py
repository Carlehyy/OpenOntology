"""Persist a managed connection failure without depending on connector construction."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.mcp_client import namespaced_tool_name
from app.super_assistant.models import SuperAssistantMcpServer


def invalidate(db: Session, server: SuperAssistantMcpServer, message: str) -> None:
    db.refresh(server, with_for_update=True)
    keys = [namespaced_tool_name(server.name, tool["name"])
            for tool in (server.tool_manifest or []) if isinstance(tool, dict) and tool.get("name")]
    if keys:
        for revision in db.query(CapabilityRevision).filter(
            CapabilityRevision.key.in_(keys),
            CapabilityRevision.revision == int(server.manifest_revision or 1),
        ).all():
            revision.enabled = False
    server.constrained = False
    server.last_test_status = "error"
    server.last_test_message = message[:500]
    server.last_tested_at = datetime.now(timezone.utc)
    db.commit()
