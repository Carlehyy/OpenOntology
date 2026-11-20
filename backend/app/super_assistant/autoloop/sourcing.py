"""Immutable Multica source references frozen into AutoLoop rows.

A sourceRef is the ``{baseUrl, workspaceId, profileRevisionId}`` provenance
triple captured when a decision card or action step is planned. Answers,
execution, reconciliation and watchdog rechecks resolve that frozen source;
the currently active profile is never borrowed across a recorded reference,
so an A→B profile switch leaves old cards bound to A while new work targets B.

Legacy rows created before references existed resolve through their
endpoint/workspace evidence (``config_for_source``); workspace identity is
unique per owner across endpoints at enrollment time, so that resolution is
unambiguous rather than a guess. Only fully unrecorded rows (no reference and
no issue workspace) keep the historical active-config semantics.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.super_assistant import multica_service
from app.super_assistant.models import (
    SuperAssistantMulticaProfile,
    SuperAssistantMulticaProfileRevision,
)

from .models import NO_WORKSPACE


def source_ref(source: Any, revision_id: str | None) -> dict[str, str]:
    """Freeze the provenance triple of the source a row is planned under."""
    reference = {"baseUrl": str(source.base_url or ""),
                 "workspaceId": str(source.workspace_id or "")}
    if revision_id:
        reference["profileRevisionId"] = str(revision_id)
    return reference


def _reference_of(row) -> dict | None:
    for evidence in (row.current_evidence, row.evidence_baseline):
        reference = (evidence or {}).get("sourceRef")
        if isinstance(reference, dict):
            return reference
    return None


def _verified(source, reference: dict) -> Any:
    if ((reference.get("baseUrl") and str(source.base_url) != reference["baseUrl"])
            or (reference.get("workspaceId") and str(source.workspace_id) != reference["workspaceId"])):
        raise multica_service.MulticaServiceError("multica 来源引用与其 revision 不再一致，需要人工处理")
    return source


def resolve_reference(db: Session, owner_id: str, reference: dict) -> Any:
    """Resolve a frozen reference to its immutable source (never the active profile)."""
    if not isinstance(reference, dict):
        raise multica_service.MulticaServiceError("multica 来源引用无效")
    revision_id = str(reference.get("profileRevisionId") or "")
    if revision_id:
        return _verified(multica_service.config_for_revision(db, owner_id, revision_id), reference)
    base_url = str(reference.get("baseUrl") or "")
    workspace_id = str(reference.get("workspaceId") or "")
    if base_url and workspace_id:
        return multica_service.config_for_source(
            db, owner_id, base_url=base_url, workspace_id=workspace_id)
    raise multica_service.MulticaServiceError("multica 来源引用不完整，需要人工处理")


def source_for_workspace(db: Session, owner_id: str, workspace_id: str) -> Any | None:
    """The unique usable source of a workspace, or None when none was enrolled.

    Enrollment keeps one endpoint per workspace per owner (including retired
    and soft-deleted history), so the endpoint never has to be guessed.
    """
    bases = set(db.scalars(select(SuperAssistantMulticaProfileRevision.base_url).join(
        SuperAssistantMulticaProfile,
        SuperAssistantMulticaProfile.id == SuperAssistantMulticaProfileRevision.profile_id,
    ).where(SuperAssistantMulticaProfile.owner_id == owner_id,
            SuperAssistantMulticaProfileRevision.workspace_id == workspace_id)))
    if not bases:
        return None
    if len(bases) > 1:
        raise multica_service.MulticaProfileConflictError(
            "此工作区 ID 已绑定多个 multica 服务地址，需要人工处理")
    return multica_service.config_for_source(
        db, owner_id, base_url=bases.pop(), workspace_id=workspace_id)


def resolve_decision_source(db: Session, owner_id: str, row) -> tuple[Any, str | None]:
    """``(source, revision_id)`` a decision card is bound to for answer writeback.

    Resolution order: the frozen reference in the card's evidence, then the
    unique source of the card's workspace (legacy cards), then — only for
    unlinked cards that never recorded a workspace — the active profile.
    Unusable sources raise instead of borrowing a different profile.
    """
    reference = _reference_of(row)
    if reference is not None:
        source = resolve_reference(db, owner_id, reference)
        if row.workspace_id not in (None, NO_WORKSPACE, source.workspace_id):
            raise multica_service.MulticaServiceError("multica 来源引用与事项工作区不一致，需要人工处理")
        return source, str(source.id)
    if row.workspace_id and row.workspace_id != NO_WORKSPACE:
        source = source_for_workspace(db, owner_id, row.workspace_id)
        if source is None:
            raise multica_service.MulticaProfileNotFoundError("multica 任务来源不存在或不可用")
        return source, str(source.id)
    if row.issue_key:
        raise multica_service.MulticaServiceError("multica 事项缺少已验证的来源，需要人工处理")
    config = multica_service.active_config(db, owner_id)
    return config, str(config.profile_revision_id) if config and config.profile_revision_id else None
