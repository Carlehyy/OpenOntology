"""持久来源到 kernel.v1 ContextCandidate 的适配层。

该模块只负责把已有的记忆服务结果转换成可审计的 ``SourceRef``。来源的
owner/status 在构建时再次校验，因此删除或取代的 legacy 行不会重新进入
Context Pack。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.super_assistant import memory_service
from app.super_assistant.models import SuperAssistantMemory

from .context import ContextCandidate, ContextTier, SourceRef
from .source_registry import tombstoned_source_ids


def _revision(value: datetime | None, fallback: str = "1") -> str:
    if value is None:
        return fallback
    stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return stamp.isoformat()




def _memory_candidates(db: Session, owner_id: str, query_text: str) -> list[ContextCandidate]:
    """取固定数量 pinned + 本轮相关记忆，并在同一事务中计数引用。"""
    # relevant_memories 负责效果因子、时间衰减和 match_count；即使查询为空，
    # pinned 仍是用户明确要求常驻的上下文来源。
    relevant = memory_service.relevant_memories(db, owner_id, query_text) if query_text else []
    relevant_ids = {row.id for row in relevant}
    tombstones = tombstoned_source_ids(db, owner_id, "memory")
    pinned = db.scalars(
        select(SuperAssistantMemory)
        .where(
            SuperAssistantMemory.owner_id == owner_id,
            SuperAssistantMemory.superseded.is_(False),
            SuperAssistantMemory.pinned.is_(True),
        )
        .order_by(SuperAssistantMemory.updated_at.desc(), SuperAssistantMemory.id)
    ).all()
    rows: list[SuperAssistantMemory] = []
    seen: set[str] = set()
    for row in [*pinned, *relevant]:
        # owner/status 条件已在查询中，但保留显式检查防止调用方传入被刷新
        # 的对象或未来检索实现绕过条件。
        if row.id in seen or row.id in tombstones or row.owner_id != owner_id or row.superseded:
            continue
        seen.add(row.id)
        rows.append(row)
    result: list[ContextCandidate] = []
    for row in rows:
        result.append(ContextCandidate(
            source=SourceRef(
                "memory", row.id, _revision(row.updated_at), f"memory://{row.id}",
                "memory.v1", f"memory:{row.id}:{_revision(row.updated_at)}",
            ),
            content=row.content.strip(),
            tier=ContextTier.REQUIRED if row.pinned else ContextTier.RELEVANT,
            relevance=1.0 if row.pinned else (1.0 if row.id in relevant_ids else 0.5),
            authority=1.0 if row.source == "explicit" else 0.7,
            freshness=1.0,
            section="knowledge",
        ))
    return result


def mark_selected_context_sources(db: Session, source_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> None:
    """Increment reference counters only for sources admitted to the final pack."""
    memory_ids = [
        str(ref.get("id")) for ref in source_refs
        if ref.get("kind") == "memory" and ref.get("id")
    ]
    if memory_ids:
        memory_service.mark_referenced(db, memory_ids)


def collect_context_candidates(db: Session, owner_id: str, query_text: str) -> list[ContextCandidate]:
    """构建本次 Run 可用的长期上下文来源。

    这是一个 best-effort source adapter：数据库中的 owner/status 过滤是硬闸。
    调用方仍需交给 ``ContextPackPlanner``，以统一预算、排序、tombstone
    过滤和 pack hash。
    """
    return _memory_candidates(db, owner_id, query_text)
