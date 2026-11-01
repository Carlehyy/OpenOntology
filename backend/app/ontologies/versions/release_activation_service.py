"""Release activation support shared by promotion and rollback.

SQL mutations in this module deliberately stop at the caller-owned transaction
boundary. Query-store rebuilds reconcile Neo4j from committed SQL truth and
report readiness without changing HTTP behavior.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.ontologies.sentinels.models import Sentinel, SentinelMatchState
from app.ontologies.versions.snapshot_contract import (
    next_release_number,
)
from app.ontologies.versions.models import OntologyVersion

logger = logging.getLogger(__name__)


def rebuild_required_query_projections(
    db: Session,
    ontology_id: str,
    *,
    mapping_service_factory=None,
    structure_commit: bool = False,
) -> dict:
    """Reconcile non-transactional query stores from committed SQL truth.

    实例投影失败主导 ready；结构投影是可重建探索视图：构建失败耐久置
    failed 如实上报，SQL 权威层拒绝（无当前发布/本体不存在）返回
    ``rejected`` 且 fence 不变。

    事务契约：``structure_commit=False``（precommit 调用方默认）fence
    随调用方事务提交；``structure_commit=True``（commit 之后调用的
    分支）钩子自持事务收口，返回时会话无遗留 fence 写。
    """
    if mapping_service_factory is None:
        from app.ontologies.mappings.mapping_service import MappingService
        mapping_service_factory = MappingService
    service = mapping_service_factory(db)
    neo4j_ok = service._rebuild_neo4j_projection(ontology_id)
    structure = _rebuild_structure_projection(
        db, ontology_id, commit=structure_commit)
    return {
        "ready": bool(neo4j_ok),
        "neo4j": "ok" if neo4j_ok else "error",
        "structure": structure,
    }


def _rebuild_structure_projection(
    db: Session, ontology_id: str, *, commit: bool = False,
) -> dict:
    """结构投影重建：failed=耐久构建失败；rejected=SQL 权威层拒绝。"""
    from app.ontologies.graph.structure_projection import (
        StructureProjectionError,
        StructureProjectionRejected,
        rebuild_structure_projection,
    )

    try:
        return rebuild_structure_projection(db, ontology_id, commit=commit)
    except StructureProjectionRejected as exc:
        logger.warning(
            "structure projection rejected for %s: %s", ontology_id, exc)
        return {"status": "rejected", "error": str(exc)[:500]}
    except StructureProjectionError as exc:
        logger.warning(
            "structure projection rebuild failed for %s: %s", ontology_id, exc)
        return {"status": "failed", "error": str(exc)[:500]}
    except Exception as exc:  # noqa: BLE001 - 结构失败不阻断发布主流程
        logger.warning(
            "structure projection rebuild crashed for %s: %s", ontology_id, exc)
        return {"status": "failed", "error": str(exc)[:500]}


def next_release_activation_number(
    db: Session,
    ontology_id: str,
    *,
    number_allocator: Callable[[str | None], str] = next_release_number,
) -> str:
    """Allocate after every historic release, including legacy pointer reuse."""
    highest = 0
    for (number,) in db.query(OntologyVersion.version_number).filter(
        OntologyVersion.ontology_id == ontology_id,
        OntologyVersion.node_kind == "release",
    ).all():
        raw = str(number or "").removeprefix("v").split(".", 1)[0]
        if raw.isdigit():
            highest = max(highest, int(raw))
    return number_allocator(f"v{highest}")


def invalidate_dynamic_sentinels_for_release(
    db: Session,
    ontology_id: str,
    release_id: str,
) -> int:
    """Disable assistant overlays still bound to an older release."""
    rows = db.query(Sentinel).filter(
        Sentinel.ontology_id == ontology_id,
        Sentinel.origin == "assistant_dynamic",
        Sentinel.retired_at.is_(None),
    ).with_for_update().all()
    stale = [row for row in rows if row.bound_release_id != release_id]
    if not stale:
        return 0
    stale_ids = [row.id for row in stale]
    for row in stale:
        row.enabled = False
        row.last_trial_at = None
        row.last_trial_release_id = None
        row.last_trial_revision = None
        row.last_trial_report = None
    db.query(SentinelMatchState).filter(
        SentinelMatchState.ontology_id == ontology_id,
        SentinelMatchState.sentinel_id.in_(stale_ids),
    ).delete(synchronize_session=False)
    return len(stale)
