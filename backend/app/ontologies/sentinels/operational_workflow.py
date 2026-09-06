"""Runtime and operational-state workflows for built-in Sentinels."""
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


def run(
    ontology_id: str,
    db: Session,
    *,
    run_manual_fn: WorkflowDependency,
) -> dict:
    result = run_manual_fn(db, ontology_id)
    # 手动触发产生新的触发记录（总览 7 天统计口径），失效总览缓存（fail-open）。
    from app.ontologies import cache as ontology_cache
    ontology_cache.invalidate_overview()
    return {"data": result}


def get_cdc_status(
    ontology_id: str,
    release_id: str | None,
    include_history: bool,
    db: Session,
    *,
    project_fn: WorkflowDependency,
    sessionmaker_fn: WorkflowDependency,
) -> dict:
    project_fn(db, ontology_id)
    from app.ontologies.sentinels.cdc import cdc_dispatch_status

    factory = sessionmaker_fn(
        bind=db.get_bind(),
        expire_on_commit=False,
    )
    return {
        "data": cdc_dispatch_status(
            ontology_id,
            ontology_release_id=release_id,
            include_history=include_history,
            session_factory=factory,
        ),
    }


def update_operational_state(
    ontology_id: str,
    sentinel_id: str,
    body: Any,
    db: Session,
    *,
    sentinel_write_fence_fn: WorkflowDependency,
    project_fn: WorkflowDependency,
    current_release_context_fn: WorkflowDependency,
    released_dict_fn: WorkflowDependency,
) -> dict:
    with sentinel_write_fence_fn(db, sentinel_id):
        project_fn(db, ontology_id, for_update=True)
        context = current_release_context_fn(
            db,
            ontology_id,
            expected_release_id=body.expected_release_id,
        )
        released = next(
            (
                item
                for item in context.snapshot.get("sentinels") or []
                if isinstance(item, dict)
                and str(item.get("id") or "") == sentinel_id
            ),
            None,
        )
        if released is None:
            raise HTTPException(
                409,
                detail={
                    "code": "builtin_sentinel_not_in_current_release",
                    "message": "该哨兵不属于当前不可变发布版本，请刷新后重试",
                    "currentReleaseId": context.id,
                },
            )

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
        if (
            sentinel is None
            or sentinel.status != "published"
            or sentinel.retired_at is not None
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "builtin_sentinel_not_operational",
                    "message": "当前发布哨兵缺少可用的运行态投影，已拒绝修改",
                    "currentReleaseId": context.id,
                },
            )

        generation = int(sentinel.enable_generation or 0)
        if generation != body.expected_generation:
            raise HTTPException(
                409,
                detail={
                    "code": "builtin_sentinel_generation_conflict",
                    "message": "哨兵运行状态已被其他会话修改，请刷新后重试",
                    "expectedGeneration": body.expected_generation,
                    "currentGeneration": generation,
                    "currentReleaseId": context.id,
                },
            )

        was_enabled = bool(sentinel.enabled)
        was_muted = bool(sentinel.muted)
        target_enabled = (
            was_enabled
            if body.enabled is None
            else bool(body.enabled)
        )
        target_muted = (
            was_muted
            if body.muted is None
            else bool(body.muted)
        )
        activation_transition = (
            (not was_enabled and target_enabled)
            or (
                was_enabled
                and target_enabled
                and was_muted
                and not target_muted
            )
        )

        sentinel.enabled = target_enabled
        sentinel.muted = target_muted
        if was_enabled and not target_enabled:
            # A later re-enable is a new lifecycle. Keeping completed match
            # state here would make activation see every existing match as
            # no_change and silently skip the action.
            (
                db.query(SentinelMatchState)
                .filter(
                    SentinelMatchState.ontology_id == ontology_id,
                    SentinelMatchState.sentinel_id == sentinel.id,
                )
                .delete(synchronize_session=False)
            )
            (
                db.query(SentinelPatternState)
                .filter(
                    SentinelPatternState.ontology_id == ontology_id,
                    SentinelPatternState.sentinel_id == sentinel.id,
                )
                .delete(synchronize_session=False)
            )
            db.query(SentinelPatternCursor).filter(
                SentinelPatternCursor.sentinel_id == sentinel.id,
            ).delete(synchronize_session=False)

        if activation_transition:
            sentinel.enable_generation = generation + 1
            if bool(released.get("onChange", True)):
                from app.ontologies.sentinels.cdc import (
                    capture_builtin_activation,
                )

                activation = capture_builtin_activation(
                    db,
                    ontology_id=ontology_id,
                    ontology_release_id=context.id,
                    sentinel_id=sentinel.id,
                    enable_generation=sentinel.enable_generation,
                )
                if activation is None:
                    raise HTTPException(
                        503,
                        detail={
                            "code": (
                                "builtin_sentinel_activation_unavailable"
                            ),
                            "message": (
                                "哨兵初始化任务无法持久化，运行状态修改已回滚"
                            ),
                        },
                    )

        db.commit()
        db.refresh(sentinel)
        # 启停/静默状态直接影响总览哨兵统计与健康提示，失效总览缓存（fail-open）。
        from app.ontologies import cache as ontology_cache
        ontology_cache.invalidate_overview()
        return {
            "data": released_dict_fn(
                ontology_id,
                context.id,
                released,
                sentinel,
            )
        }


def toggle_sentinel(
    ontology_id: str,
    sentinel_id: str,
    db: Session,
    *,
    sentinel_write_fence_fn: WorkflowDependency,
    project_fn: WorkflowDependency,
) -> dict:
    with sentinel_write_fence_fn(db, sentinel_id):
        project_fn(db, ontology_id, for_update=True)
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
        if sentinel.status == "published":
            raise HTTPException(
                409,
                detail={
                    "code": "sentinel_operational_api_required",
                    "message": (
                        "已发布 Sentinel 的启停必须使用带发布版本与代次校验的 "
                        "operational-state 接口"
                    ),
                },
            )
        sentinel.enabled = not sentinel.enabled
        db.commit()
        # 启停状态直接影响总览哨兵统计，失效总览缓存（fail-open）。
        from app.ontologies import cache as ontology_cache
        ontology_cache.invalidate_overview()
        return {"enabled": sentinel.enabled}
