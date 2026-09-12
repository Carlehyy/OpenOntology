"""草稿映射建议 API — 知识库 + 规则 + LLM 概念化裁决。

建议只读不写（知识库回流除外），采纳后的落库仍走草稿工作区整体保存；
全部建议进入人工确认队列，不自动生效。持久建议（Agent 提案）经
/persistent 列表与 confirm/dismiss 端点闭环流转。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user
from app.ontologies.access import ontology_access_guard
from app.ontologies.mappings import suggestion_service


router = APIRouter(dependencies=[Depends(ontology_access_guard)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class MappingSuggestionRequest(BaseModel):
    datasetIds: list[str] = Field(min_length=1, max_length=50)


class DismissSuggestionRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/{ontology_id}/versions/{version_id}/mapping-suggestions")
def suggest_version_mappings(
    ontology_id: str,
    version_id: str,
    body: MappingSuggestionRequest,
    db: Session = Depends(get_db),
):
    return suggestion_service.generate_mapping_suggestions(
        db, ontology_id, version_id, body.datasetIds)


@router.get("/{ontology_id}/versions/{version_id}/mapping-suggestions/persistent")
def list_persistent_mapping_suggestions(
    ontology_id: str,
    version_id: str,
    status: str = "pending",
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """持久建议队列（Agent 提案）只读列表；默认仅 pending。"""
    return suggestion_service.list_persistent_suggestions(
        db, ontology_id, version_id,
        status=status or None, offset=offset, limit=limit)


@router.post(
    "/{ontology_id}/versions/{version_id}"
    "/mapping-suggestions/{suggestion_id}/confirm")
def confirm_persistent_mapping_suggestion(
    ontology_id: str,
    version_id: str,
    suggestion_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """确认建议：写入草稿快照正式映射并回流知识飞轮（幂等）。"""
    return suggestion_service.confirm_persistent_suggestion(
        db, ontology_id, version_id, suggestion_id, current_user)


@router.post(
    "/{ontology_id}/versions/{version_id}"
    "/mapping-suggestions/{suggestion_id}/dismiss")
def dismiss_persistent_mapping_suggestion(
    ontology_id: str,
    version_id: str,
    suggestion_id: str,
    body: DismissSuggestionRequest | None = None,
    db: Session = Depends(get_db),
):
    """驳回建议（队列簿记，不触碰草稿映射）。"""
    return suggestion_service.dismiss_persistent_suggestion(
        db, ontology_id, version_id, suggestion_id,
        reason=(body.reason if body else ""))
