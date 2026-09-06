"""Authoring workflows for built-in Sentinel definitions."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.sentinel import Sentinel, SentinelMatchState
from app.ontologies.sentinels.models import (
    SentinelPatternCursor,
    SentinelPatternState,
)
from app.ontologies.sentinels.dynamic_service import ORIGIN_BUILTIN


WorkflowDependency = Callable[..., Any]


def create_sentinel(
    ontology_id: str,
    body: Any,
    db: Session,
    *,
    require_draft_fn: WorkflowDependency,
    dict_fn: WorkflowDependency,
) -> dict:
    require_draft_fn(db, ontology_id)
    sentinel = Sentinel(
        ontology_id=ontology_id,
        origin=ORIGIN_BUILTIN,
        **body.model_dump(exclude={"status"}),
        status="draft",
    )
    if not sentinel.primary_alias and sentinel.bindings:
        sentinel.primary_alias = sentinel.bindings[0].get("alias")
    db.add(sentinel)
    db.commit()
    db.refresh(sentinel)
    return {"data": dict_fn(sentinel)}


def update_sentinel(
    ontology_id: str,
    sentinel_id: str,
    body: Any,
    db: Session,
    *,
    sentinel_write_fence_fn: WorkflowDependency,
    project_fn: WorkflowDependency,
    dict_fn: WorkflowDependency,
) -> dict:
    with sentinel_write_fence_fn(db, sentinel_id):
        update = body.model_dump(exclude_unset=True)
        project = project_fn(db, ontology_id, for_update=True)
        sentinel = (
            db.query(Sentinel)
            .filter(
                Sentinel.id == sentinel_id,
                Sentinel.ontology_id == ontology_id,
                Sentinel.origin == ORIGIN_BUILTIN,
            )
            .with_for_update()
            .populate_existing()
            .first()
        )
        if not sentinel:
            raise HTTPException(404, "Sentinel not found")
        operational_fields = {"enabled", "muted"}
        if (
            sentinel.status == "published"
            and set(update) & operational_fields
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "sentinel_operational_api_required",
                    "message": (
                        "已发布 Sentinel 的启停/静默必须使用带发布版本与代次校验的 "
                        "operational-state 接口"
                    ),
                },
            )
        if (
            (project.status or "") != "draft"
            and set(update) - operational_fields
        ):
            raise HTTPException(
                409,
                "已发布 Sentinel 仅允许启停/静默；修改条件、绑定或动作前请先撤回本体发布",
            )
        if (
            (project.status or "") == "published"
            and update.get("enabled") is True
            and (sentinel.status or "") != "published"
        ):
            raise HTTPException(
                409,
                "该 Sentinel 不属于当前发布版本；请撤回、重新发布后再启用",
            )
        for key, value in update.items():
            setattr(sentinel, key, value)
        if not sentinel.primary_alias and sentinel.bindings:
            sentinel.primary_alias = sentinel.bindings[0].get("alias")
        if (project.status or "") == "draft":
            sentinel.status = "draft"
        db.commit()
        db.refresh(sentinel)
        return {"data": dict_fn(sentinel)}


def delete_sentinel(
    ontology_id: str,
    sentinel_id: str,
    db: Session,
    *,
    sentinel_write_fence_fn: WorkflowDependency,
    require_draft_fn: WorkflowDependency,
) -> None:
    with sentinel_write_fence_fn(db, sentinel_id):
        require_draft_fn(db, ontology_id)
        sentinel = (
            db.query(Sentinel)
            .filter(
                Sentinel.id == sentinel_id,
                Sentinel.ontology_id == ontology_id,
                Sentinel.origin == ORIGIN_BUILTIN,
            )
            .with_for_update()
            .populate_existing()
            .first()
        )
        if not sentinel:
            raise HTTPException(404, "Sentinel not found")
        # 命中状态一并清理：残留 match_state 会让重建同名哨兵时边沿差分失真
        (
            db.query(SentinelMatchState)
            .filter(SentinelMatchState.sentinel_id == sentinel_id)
            .delete(synchronize_session=False)
        )
        (
            db.query(SentinelPatternState)
            .filter(SentinelPatternState.sentinel_id == sentinel_id)
            .delete(synchronize_session=False)
        )
        db.query(SentinelPatternCursor).filter(
            SentinelPatternCursor.sentinel_id == sentinel_id,
        ).delete(synchronize_session=False)
        db.delete(sentinel)
        db.commit()
