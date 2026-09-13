"""持久来源到 kernel.v1 ContextCandidate 的适配层。

该模块只负责把已有的记忆服务和记忆宫殿检索结果转换成可审计的
``SourceRef``。来源的 owner/status 在构建时再次校验，因此删除或取代的
legacy 行不会因为 Neo4j 延迟清理而重新进入 Context Pack。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.super_assistant import memory_service, palace_graph
from app.super_assistant.models import SuperAssistantMemory, SuperAssistantPalaceBuild, SuperAssistantPalaceFile

from .context import ContextCandidate, ContextTier, SourceRef

logger = logging.getLogger(__name__)


def _revision(value: datetime | None, fallback: str = "1") -> str:
    if value is None:
        return fallback
    stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return stamp.isoformat()


def _terms(query: str) -> list[str]:
    # palace_graph.search 是词法接口；保留 CJK 字符串整体，同时按空白拆分
    # 拉丁/数字术语，避免引入另一套分词依赖。
    return [term.strip().casefold() for term in str(query or "").split() if term.strip()]


def _memory_candidates(db: Session, owner_id: str, query_text: str) -> list[ContextCandidate]:
    """取固定数量 pinned + 本轮相关记忆，并在同一事务中计数引用。"""
    # relevant_memories 负责效果因子、时间衰减和 match_count；即使查询为空，
    # pinned 仍是用户明确要求常驻的上下文来源。
    relevant = memory_service.relevant_memories(db, owner_id, query_text) if query_text else []
    relevant_ids = {row.id for row in relevant}
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
        if row.id in seen or row.owner_id != owner_id or row.superseded:
            continue
        seen.add(row.id)
        rows.append(row)
    if rows:
        memory_service.mark_referenced(db, [row.id for row in rows])
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


def _active_palace_files(db: Session, owner_id: str) -> dict[str, SuperAssistantPalaceFile]:
    rows = db.scalars(
        select(SuperAssistantPalaceFile).where(
            SuperAssistantPalaceFile.owner_id == owner_id,
            # failed/pending/building rows have no authoritative graph snapshot.
            SuperAssistantPalaceFile.status == "built",
        )
    ).all()
    return {row.id: row for row in rows}


def _latest_builds(db: Session, file_ids: set[str]) -> dict[str, SuperAssistantPalaceBuild]:
    if not file_ids:
        return {}
    rows = db.scalars(
        select(SuperAssistantPalaceBuild)
        .where(
            SuperAssistantPalaceBuild.file_id.in_(file_ids),
            SuperAssistantPalaceBuild.status == "success",
        )
        .order_by(SuperAssistantPalaceBuild.created_at.desc(), SuperAssistantPalaceBuild.id.desc())
    ).all()
    result: dict[str, SuperAssistantPalaceBuild] = {}
    for row in rows:
        result.setdefault(row.file_id, row)
    return result


def _palace_candidates(db: Session, owner_id: str, query_text: str) -> list[ContextCandidate]:
    terms = _terms(query_text)
    if not terms:
        return []
    try:
        graph = palace_graph.search(owner_id, terms)
    except Exception:
        # Neo4j is an optional knowledge source; failure must not block model
        # execution or invalidate memory candidates.
        logger.info("记忆宫殿检索不可用，跳过图谱上下文（owner=%s）", owner_id, exc_info=True)
        return []
    active = _active_palace_files(db, owner_id)
    if not active:
        return []
    builds = _latest_builds(db, set(active))
    by_file: dict[str, dict[str, Any]] = defaultdict(lambda: {"entities": [], "relations": []})
    for entity in graph.get("entities") or []:
        file_ids = set(entity.get("file_ids") or []) & set(active)
        for file_id in file_ids:
            by_file[file_id]["entities"].append(entity)
    for relation in graph.get("relations") or []:
        file_ids = set(relation.get("file_ids") or []) & set(active)
        for file_id in file_ids:
            by_file[file_id]["relations"].append(relation)
    result: list[ContextCandidate] = []
    for file_id, payload in by_file.items():
        row = active[file_id]
        build = builds.get(file_id)
        revision = row.sha256 or _revision(row.updated_at)
        extraction_id = build.id if build is not None else f"palace:{file_id}:{revision}"
        lines = [f"来源文件：{row.filename}"]
        for entity in payload["entities"][:40]:
            aliases = ", ".join(str(item) for item in entity.get("aliases") or [])
            suffix = f"（别名：{aliases}）" if aliases else ""
            lines.append(f"实体：{entity.get('name', '')} [{entity.get('type', '其他')}] {suffix}".rstrip())
        for relation in payload["relations"][:40]:
            lines.append(
                f"关系：{relation.get('source_name', relation.get('source', ''))}"
                f" -{relation.get('name', '')}-> "
                f"{relation.get('target_name', relation.get('target', ''))}"
            )
        result.append(ContextCandidate(
            source=SourceRef(
                "palace_file", file_id, str(revision), f"palace://{file_id}",
                "palace.graph.v1", str(extraction_id),
            ),
            content="\n".join(lines), tier=ContextTier.RELEVANT,
            relevance=0.8, authority=0.9, freshness=1.0,
            cost=1.0, section="knowledge",
        ))
    return result


def collect_context_candidates(db: Session, owner_id: str, query_text: str) -> list[ContextCandidate]:
    """构建本次 Run 可用的长期上下文来源。

    这是一个 best-effort source adapter：数据库中的 owner/status 过滤是硬闸，
    Neo4j 不可用只会缺少图谱来源。调用方仍需交给 ``ContextPackPlanner``，
    以统一预算、排序、tombstone 过滤和 pack hash。
    """
    candidates = _memory_candidates(db, owner_id, query_text)
    candidates.extend(_palace_candidates(db, owner_id, query_text))
    return candidates
