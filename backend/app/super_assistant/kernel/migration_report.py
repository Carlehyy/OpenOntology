"""Report-only inventory for legacy Super Assistant data.

The first kernel release deliberately does not mutate legacy rows.  This module
turns the migration rules into a deterministic report that can be attached to a
deployment rehearsal and reviewed before any backfill job is enabled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.super_assistant.models import (
    SuperAssistantDelegation,
    SuperAssistantMemory,
    SuperAssistantMcpServer,
    SuperAssistantPalaceFile,
    SuperAssistantSkill,
)


@dataclass(frozen=True, slots=True)
class LegacyDisposition:
    source: str
    total: int
    mappable: int
    readonly: int
    rule: str


def _count(db: Session, model: type[Any], owner_id: str | None) -> int:
    stmt = select(func.count()).select_from(model)
    if owner_id is not None and hasattr(model, "owner_id"):
        stmt = stmt.where(model.owner_id == owner_id)
    return int(db.scalar(stmt) or 0)


def build_legacy_migration_report(db: Session, *, owner_id: str | None = None) -> dict[str, Any]:
    """Return a report without changing a single legacy row.

    ``readonly`` is intentionally explicit: rows that cannot be represented by
    kernel.v1 remain available to the legacy read paths and are never silently
    rebound to another Run or capability revision.
    """
    delegation_total = _count(db, SuperAssistantDelegation, owner_id)
    delegation_mappable = int(db.scalar(
        select(func.count()).select_from(SuperAssistantDelegation).where(
            *( [SuperAssistantDelegation.owner_id == owner_id] if owner_id is not None else [] ),
            SuperAssistantDelegation.super_conversation_id.is_not(None),
            SuperAssistantDelegation.assistant_key.is_not(None),
        )
    ) or 0)
    memory_total = _count(db, SuperAssistantMemory, owner_id)
    palace_total = _count(db, SuperAssistantPalaceFile, owner_id)
    mcp_total = _count(db, SuperAssistantMcpServer, owner_id)
    skill_total = _count(db, SuperAssistantSkill, owner_id)

    rows = [
        LegacyDisposition("delegation", delegation_total, delegation_mappable, delegation_total - delegation_mappable,
                          "回填 legacy Run/Call；不改变 assistant_key 或 conversation_ref；缺少会话引用的行只读"),
        LegacyDisposition("memory", memory_total, 0, memory_total,
                          "保留 legacy/source 与原文；标记 risk=unknown，不伪造 source_ref"),
        LegacyDisposition("palace_file", palace_total, palace_total, 0,
                          "content_hash 作为 legacy source_version；缺 provenance 的历史事实不升级为 current"),
        LegacyDisposition("mcp_server", mcp_total, mcp_total, 0,
                          "通过 manifest 生成 immutable CapabilityRevision；不把旧配置提升为 trusted"),
        LegacyDisposition("skill", skill_total, skill_total, 0,
                          "通过 manifest/文件 revision 生成 immutable CapabilityRevision；保留原 owner scope"),
    ]
    return {
        "schema_version": "kernel.v1.legacy-disposition.v1",
        "owner_id": owner_id,
        "mutated": False,
        "rows": [asdict(row) for row in rows],
        "totals": {
            "legacy_rows": sum(row.total for row in rows),
            "mappable_rows": sum(row.mappable for row in rows),
            "readonly_rows": sum(row.readonly for row in rows),
        },
        "review_required": [row.source for row in rows if row.readonly],
    }
