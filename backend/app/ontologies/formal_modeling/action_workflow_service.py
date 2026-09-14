"""Action execution and HITL approval workflows for formal ontologies."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker

from app.models.ontology_formal import (
    ActionExecutionLog,
    ObjectInstance,
    ObjectType,
    PropertyFact,
)
from app.ontologies.formal_modeling.facts import record_decision_fact
from app.ontologies.formal_modeling.runtime_support import (
    _approval_instance_label,
    _current_release_view,
    _ok,
    _require_ontology,
)
from app.ontologies.release_context import current_release_context
from app.schemas import ontology_formal as S


def run_action_locked(
    ontology_id: str,
    body: S.RunActionRequest,
    db: Session,
    current_user,
    *,
    execute_action_fn: Callable,
):
    """Run an action after the route adapter acquires the projection lock."""
    _require_ontology(db, ontology_id, for_update=True)
    release = current_release_context(
        db,
        ontology_id,
        expected_release_id=body.release_id,
    )
    log = execute_action_fn(
        db,
        ontology_id,
        body,
        actor_id=getattr(current_user, "id", None),
        expected_release_id=release.id,
    )
    # 执行会产生新日志（可能进入待审批），立即失效详情页两条读缓存（fail-open）。
    from app.ontologies import cache as ontology_cache
    ontology_cache.invalidate_pending()
    ontology_cache.invalidate_overview()
    return _ok(log)


def list_pending_actions(
    ontology_id: str,
    release_id: Optional[str],
    current_release_only: bool,
    db: Session,
):
    """Return pending or resumable action approvals."""
    # 详情页 Tab 角标轮询热点：短 TTL 缓存（fail-open），决策/执行时 bump
    # 版本立即失效。jsonable_encoder 保证缓存命中与直查路径返回同一 JSON。
    from fastapi.encoders import jsonable_encoder

    from app.config import settings
    from app.ontologies import cache as ontology_cache
    return ontology_cache.cached_call(
        ontology_cache.pending_cache_key(ontology_id, release_id),
        settings.ontology_pending_cache_ttl_seconds,
        lambda: jsonable_encoder(_list_pending_actions_uncached(
            ontology_id,
            release_id,
            current_release_only,
            db,
        )),
    )


def _list_pending_actions_uncached(
    ontology_id: str,
    release_id: Optional[str],
    current_release_only: bool,
    db: Session,
):
    project = _require_ontology(db, ontology_id)
    release_snapshot = None
    release_identifier = None
    if release_id:
        release = current_release_context(
            db,
            ontology_id,
            expected_release_id=release_id,
        )
        release_snapshot = release.snapshot
        release_identifier = release.id
    elif current_release_only:
        release_row, release_snapshot = _current_release_view(db, project)
        release_identifier = release_row.id
    query = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == ontology_id,
        ActionExecutionLog.status.in_(("pending", "executing")),
        ActionExecutionLog.dry_run == False,  # noqa: E712
    )
    released_actions_by_id = {}
    if release_snapshot is not None:
        released_actions_by_id = {
            str(item["id"]): item
            for item in release_snapshot["actions"]
            if item.get("id")
        }
        action_ids = set(released_actions_by_id)
        if not action_ids:
            return _ok([])
        query = query.filter(
            ActionExecutionLog.ontology_release_id == release_identifier,
            ActionExecutionLog.action_id.in_(action_ids),
        )
    items = (
        query.order_by(ActionExecutionLog.executed_at.desc())
        .limit(100)
        .all()
    )
    instance_ids = {
        item.object_instance_id
        for item in items
        if item.object_instance_id
    }
    instances = (
        db.query(ObjectInstance)
        .filter(
            ObjectInstance.ontology_id == ontology_id,
            ObjectInstance.id.in_(instance_ids),
        )
        .all()
        if instance_ids
        else []
    )
    instances_by_id = {item.id: item for item in instances}
    object_type_ids = {
        item.object_type_id
        for item in items
        if item.object_type_id
    }
    object_type_ids.update(
        item.object_type_id
        for item in instances
        if item.object_type_id
    )
    if release_snapshot is not None:
        object_types = [
            SimpleNamespace(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                display_name=(
                    item.get("displayName")
                    or item.get("display_name")
                ),
                primary_key=(
                    item.get("primaryKey")
                    or item.get("primary_key")
                ),
                properties=item.get("properties") or [],
            )
            for item in release_snapshot["objectTypes"]
            if str(item.get("id") or "") in object_type_ids
        ]
    else:
        object_types = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.id.in_(object_type_ids),
            )
            .all()
            if object_type_ids
            else []
        )
    object_types_by_id = {item.id: item for item in object_types}

    result = []
    for item in items:
        instance = instances_by_id.get(item.object_instance_id)
        if instance is None and isinstance(item.target_snapshot, dict):
            snapshot = item.target_snapshot
            snapshot_properties = snapshot.get("properties")
            if (
                snapshot.get("id")
                and snapshot.get("objectTypeId")
                and isinstance(snapshot_properties, dict)
            ):
                instance = SimpleNamespace(
                    id=str(snapshot["id"]),
                    object_type_id=str(snapshot["objectTypeId"]),
                    properties=snapshot_properties,
                    external_id=None,
                )
        object_type = object_types_by_id.get(
            (
                instance.object_type_id
                if instance is not None
                else item.object_type_id
            ),
        )
        payload = S.ActionLogOut.model_validate(item).model_dump(
            by_alias=True,
        )
        payload.update(
            {
                "actionName": (
                    released_actions_by_id.get(
                        item.action_id,
                        {},
                    ).get("displayName")
                    or released_actions_by_id.get(
                        item.action_id,
                        {},
                    ).get("name")
                    or payload.get("actionName")
                ),
                "objectTypeName": (
                    (object_type.display_name or object_type.name)
                    if object_type is not None
                    else None
                ),
                "objectInstanceLabel": (
                    _approval_instance_label(instance, object_type)
                    if instance is not None
                    else None
                ),
                "triggerSource": (
                    "sentinel"
                    if item.sentinel_match_state_id
                    else "manual"
                    if item.actor_id
                    else "system"
                ),
            },
        )
        result.append(payload)
    return _ok(result)


def decide_pending_action_locked(
    ontology_id: str,
    log_id: str,
    body: S.DecisionRequest,
    db: Session,
    current_user,
    *,
    execute_action_fn: Callable,
):
    """Apply one HITL decision while preserving its transaction boundaries."""
    project = _require_ontology(db, ontology_id, for_update=True)
    log = (
        db.query(ActionExecutionLog)
        .filter(
            ActionExecutionLog.id == log_id,
            ActionExecutionLog.ontology_id == ontology_id,
        )
        .first()
    )
    if not log:
        raise HTTPException(404, "执行记录不存在")
    decision = (body.decision or "").lower()
    if decision not in ("approved", "rejected"):
        raise HTTPException(422, "decision 必须是 approved 或 rejected")
    # ``executing`` is a durable approval checkpoint. A process can stop after
    # the human decision commits but before (or just after) the separately
    # committed action finishes. Repeating the same approval resumes through
    # the stable execution idempotency key without creating a second fact.
    resuming_approved = (
        log.status == "executing"
        and decision == "approved"
        and log.decided_at is not None
    )
    if log.status != "pending" and not resuming_approved:
        raise HTTPException(
            409,
            f"该记录已处理（当前状态: {log.status}），不能重复决策",
        )
    # Approval always resolves the exact current release, even when an older
    # client omitted releaseId. Version labels can repeat across rollback
    # activations, so comparing project.version alone is not a safety fence.
    # Rejection is side-effect free and may still process historical proposals
    # without requiring their release to remain current.
    release = (
        current_release_context(
            db,
            ontology_id,
            expected_release_id=body.release_id,
        )
        if decision == "approved" or body.release_id
        else None
    )
    current_version = (
        release.version
        if release is not None
        else project.version
    )
    if release is not None:
        if (
            not log.ontology_release_id
            or log.ontology_release_id != release.id
            or not log.ontology_version
            or log.ontology_version != current_version
        ):
            raise HTTPException(
                409,
                f"该动作不属于当前发布本体 {current_version}，"
                "跨版本审批已拒绝（发布节点不一致）",
            )
    elif decision == "approved":
        if not log.ontology_version:
            raise HTTPException(
                409,
                "该待办动作缺少本体版本血缘，不能安全批准；可选择拒绝",
            )
        if log.ontology_version != current_version:
            raise HTTPException(
                409,
                f"该动作属于本体 {log.ontology_version}，"
                f"当前为 {current_version}；跨版本审批已拒绝",
            )

    uid = getattr(current_user, "id", None)
    uname = (
        getattr(current_user, "username", None)
        or getattr(current_user, "email", None)
        or uid
    )
    if resuming_approved:
        fact = (
            db.query(PropertyFact)
            .filter(
                PropertyFact.ontology_id == ontology_id,
                PropertyFact.instance_id == log.id,
                PropertyFact.property_name == "decision",
                PropertyFact.kind == "decision",
            )
            .order_by(
                PropertyFact.recorded_at.desc(),
                PropertyFact.id.desc(),
            )
            .first()
        )
        if fact is None:
            raise HTTPException(
                409,
                "审批日志处于执行恢复态，但缺少已持久化的决策事实",
            )
    else:
        # Human governance evidence is committed before any business action is
        # attempted. The action runs in a separate Session below, so its
        # rollback can never erase the approval/rejection audit.
        fact = record_decision_fact(
            db,
            ontology_id=ontology_id,
            action_log_id=log.id,
            decision=decision.upper(),
            source=f"user://{uname}",
            actor_id=uid,
            reason=body.reason,
            ontology_version=log.ontology_version,
            ontology_release_id=log.ontology_release_id,
        )
        log.decided_by = uid
        log.decided_at = datetime.now(timezone.utc)
        log.decision_reason = body.reason

    if decision == "rejected":
        log.status = "rejected"
        # Rejection is terminal for this approval proposal but not for the
        # business edge. Release the unique key/claim so a later evaluation
        # can create one new, auditable attempt rather than replaying rejection.
        state_id = log.sentinel_match_state_id
        log.idempotency_key = None
        from app.inbox.service import enqueue_ontology_approval_decision
        enqueue_ontology_approval_decision(
            db,
            log_id=log.id,
            ontology_id=ontology_id,
            decision="rejected",
            action_name=log.action_name or "待审批动作",
            ontology_version=log.ontology_version,
            ontology_release_id=log.ontology_release_id,
            occurred_at=log.decided_at,
        )
        db.commit()
        db.refresh(log)
        if state_id:
            from app.ontologies.sentinels.evaluator import (
                reject_sentinel_match_claim,
            )

            reject_sentinel_match_claim(db, ontology_id, state_id)
        # 拒绝同样改变待审批队列，立即失效两条读缓存（fail-open）。
        from app.ontologies import cache as ontology_cache
        ontology_cache.invalidate_pending()
        ontology_cache.invalidate_overview()
        return _ok(
            S.ActionLogOut.model_validate(log).model_dump(by_alias=True),
        )

    # Approval is now durable but is not yet proof of technical success.
    # ``executing`` prevents dashboards from presenting it as completed and
    # provides a resumable checkpoint for a process crash.
    log.status = "executing"
    fact_id = fact.id
    state_id = log.sentinel_match_state_id
    decision_actor_id = log.decided_by or uid
    action_id = log.action_id
    execution_release_id = log.ontology_release_id
    parameters = dict(log.parameters or {})
    object_instance_id = log.object_instance_id
    target_snapshot = (
        dict(log.target_snapshot)
        if isinstance(log.target_snapshot, dict)
        else None
    )
    db.commit()

    # 批准：以原参数真正执行；执行事实的因果指针指向已耐久化的决策事实。
    # A separate Session is a hard transaction boundary: execute_action may
    # rollback freely without touching the decision checkpoint above.
    # The approved execution owns a stable key independent from the proposal
    # key. If the process exits after execute_action() commits but before this
    # pending row is linked, retrying the decision replays the durable execution
    # instead of applying the business side effect a second time.
    approved_execution_key = f"approval-execution:{log_id}"
    exec_body = SimpleNamespace(
        action_id=action_id,
        parameters=parameters,
        target_instance_id=object_instance_id,
        dry_run=False,
        sentinel_match_state_id=state_id,
        idempotency_key=approved_execution_key,
        target_snapshot=target_snapshot,
        expected_release_id=execution_release_id,
    )
    sentinel_token = None
    if state_id:
        from app.ontologies.sentinels.evaluator import in_sentinel_run

        sentinel_token = in_sentinel_run.set(True)
    execution_session_factory = sessionmaker(
        bind=db.get_bind(),
        expire_on_commit=False,
    )
    execution_db = execution_session_factory()
    try:
        result = execute_action_fn(
            execution_db,
            ontology_id,
            exec_body,
            actor_id=decision_actor_id,
            caused_by_fact=fact_id,
            skip_approval=True,
            expected_release_id=execution_release_id,
        )
    finally:
        execution_db.close()
        if sentinel_token is not None:
            in_sentinel_run.reset(sentinel_token)

    # Finalization is a third transaction. Both successful and failed
    # technical attempts are linked to the already durable human decision.
    db.expire_all()
    log = (
        db.query(ActionExecutionLog)
        .filter(
            ActionExecutionLog.id == log_id,
            ActionExecutionLog.ontology_id == ontology_id,
        )
        .with_for_update()
        .first()
    )
    if log is None:
        raise HTTPException(
            409,
            "审批日志在动作执行后不存在，无法完成关联",
        )
    log.related_log_id = result.get("id")
    execution_succeeded = result.get("status") == "success"
    if execution_succeeded:
        log.status = "approved"
        log.error_message = None
    else:
        # Human approval and technical execution are separate facts. A failed
        # execution must neither appear as approved/executed nor permanently
        # own the proposal key. The decision fact still records that a human
        # approved the proposal; this row records that execution failed.
        log.status = "failed"
        log.error_message = (
            result.get("errorMessage")
            or "审批已通过，但动作技术执行失败"
        )
        log.idempotency_key = None
    from app.inbox.service import enqueue_ontology_approval_decision
    enqueue_ontology_approval_decision(
        db,
        log_id=log.id,
        ontology_id=ontology_id,
        decision="approved" if execution_succeeded else "failed",
        action_name=log.action_name or "待审批动作",
        ontology_version=log.ontology_version,
        ontology_release_id=log.ontology_release_id,
        occurred_at=log.decided_at,
    )
    db.commit()
    db.refresh(log)
    sentinel_resume = None
    if state_id:
        if execution_succeeded:
            from app.ontologies.sentinels.evaluator import (
                resume_sentinel_match_claim,
            )

            sentinel_resume = resume_sentinel_match_claim(
                db,
                ontology_id,
                state_id,
            )
        else:
            from app.ontologies.sentinels.evaluator import (
                fail_sentinel_match_claim,
            )

            sentinel_resume = fail_sentinel_match_claim(
                db,
                ontology_id,
                state_id,
            )
    # 决策事实改变待审批队列与运行统计，立即失效两条读缓存（fail-open）。
    from app.ontologies import cache as ontology_cache
    ontology_cache.invalidate_pending()
    ontology_cache.invalidate_overview()
    return _ok(
        {
            "pendingLog": S.ActionLogOut.model_validate(log).model_dump(
                by_alias=True,
            ),
            "executionLog": result,
            "decisionFactId": fact_id,
            "sentinelResume": sentinel_resume,
        },
    )
