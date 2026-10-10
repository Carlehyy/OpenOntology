"""任务实例 — HTTP 层业务编排（模板/版本、激活、节点交互、快照投影）。

事务边界：引擎函数在请求会话内完成「事件 + 物化」，本层负责
commit 之后的传输动作（NATS 派发）与通知（inbox）——执行事实与
传输分离（设计方案 §5.2）。

派发 seam：_dispatch_node 是模块级单点，测试以 monkeypatch 替换为
内联假执行器或 no-op；NATS 未配置时记录错误并依赖对账重投
（reconcile.redispatch_stale_once），不回滚已持久化的事实。
"""
from __future__ import annotations

import base64
import hashlib
import logging

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.task_instances import engine
from app.task_instances import schemas
from app.task_instances.models import (
    INSTANCE_ACTIVE,
    INSTANCE_STATUSES,
    NODE_RUNNING,
    NODE_DISPATCHED,
    NODE_WAITING_APPROVAL,
    NODE_WAITING_HUMAN,
    APPROVAL_PENDING,
    TaskApproval,
    TaskArtifact,
    TaskInstance,
    TaskNodeRun,
    TaskSteeringMessage,
    TaskTemplate,
    TaskTemplateRevision,
)
from app.task_instances.spec import (
    SpecError,
    WorkflowSpec,
    compile_workflow_yaml,
    parse_workflow_yaml,
)
from app.task_instances.validator import validate_workflow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 派发 seam（测试 monkeypatch 点）
# ---------------------------------------------------------------------------


def _dispatch_node(node_run_id: str) -> None:
    try:
        from app.data_channel.pipeline_tasks.dispatch import dispatch_task

        dispatch_task("task_instances.control",
                      {"kind": "dispatch", "node_run_id": node_run_id})
    except Exception:  # noqa: BLE001 — 传输失败不回滚事实，靠对账重投
        logger.exception("节点派发失败（等待对账重投）: %s", node_run_id)


def _dispatch_steering(node_run_id: str, message_id: str,
                       content: str = "") -> None:
    try:
        from app.data_channel.pipeline_tasks.dispatch import dispatch_task

        dispatch_task("task_instances.control",
                      {"kind": "steer", "node_run_id": node_run_id,
                       "message_id": message_id, "content": content[:60000]})
    except Exception:  # noqa: BLE001
        logger.exception("插话派发失败: %s", node_run_id)


def _after_commit_dispatch(result: engine.EngineResult) -> None:
    """提交后派发；内联执行（测试）返回级联时循环推进。

    生产 NATS 路径 _dispatch_node 返回 None，级联由 executor 侧
    handler 完成后再发布（run_control_message）。
    """
    pending = list(result.dispatches)
    guard = 0
    while pending:
        guard += 1
        if guard > 50:  # 防御：policies 已限环路上限
            logger.error("级联派发超过安全上限，停止推进")
            break
        cascades: list[str] = []
        for node_run_id in pending:
            inner = _dispatch_node(node_run_id)
            if inner is not None:
                cascades.extend(inner.dispatches)
        pending = cascades


# ---------------------------------------------------------------------------
# 模板 / 版本
# ---------------------------------------------------------------------------


def compile_and_validate(spec_yaml: str) -> tuple[WorkflowSpec, str, dict]:
    try:
        compiled = compile_workflow_yaml(spec_yaml)
    except SpecError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "TEMPLATE_INVALID", "message": str(exc),
                    **({"line": exc.line} if exc.line else {})},
        ) from exc
    errors = validate_workflow(compiled.spec)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"code": "TEMPLATE_INVALID",
                    "message": "语义校验未通过", "errors": errors},
        )
    return compiled.spec, compiled.canonical_hash, compiled.canonical


def create_template(db: Session, body: schemas.TemplateCreate, user) -> dict:
    spec, canonical_hash, canonical = compile_and_validate(body.spec_yaml)
    template = TaskTemplate(
        name=spec.metadata.name,
        description=spec.metadata.description or "",
        created_by=getattr(user, "id", None),
    )
    db.add(template)
    db.flush()
    revision = TaskTemplateRevision(
        template_id=template.id, revision_no=1,
        spec_yaml=body.spec_yaml, spec_compiled=canonical,
        canonical_hash=canonical_hash, note=body.note,
        created_by=getattr(user, "id", None))
    db.add(revision)
    db.flush()
    template.latest_revision_id = revision.id
    db.commit()
    return _template_out(db, template)


def update_template(db: Session, template_id: str,
                    body: schemas.TemplateUpdate, user) -> dict:
    template = _get_template(db, template_id)
    spec, canonical_hash, canonical = compile_and_validate(body.spec_yaml)
    existing = db.query(TaskTemplateRevision).filter(
        TaskTemplateRevision.template_id == template.id,
        TaskTemplateRevision.canonical_hash == canonical_hash,
    ).first()
    if existing is None:
        latest_no = db.query(TaskTemplateRevision).filter(
            TaskTemplateRevision.template_id == template.id,
        ).order_by(TaskTemplateRevision.revision_no.desc()).first()
        revision = TaskTemplateRevision(
            template_id=template.id,
            revision_no=(latest_no.revision_no + 1) if latest_no else 1,
            spec_yaml=body.spec_yaml, spec_compiled=canonical,
            canonical_hash=canonical_hash, note=body.note,
            created_by=getattr(user, "id", None))
        db.add(revision)
        db.flush()
        template.latest_revision_id = revision.id
    else:
        revision = existing
    template.name = spec.metadata.name
    template.description = spec.metadata.description or ""
    db.commit()
    return _template_out(db, template)


def validate_only(spec_yaml: str) -> dict:
    compile_and_validate(spec_yaml)
    return {"valid": True}


def list_templates(db: Session, *, keyword: str = "") -> list[dict]:
    query = db.query(TaskTemplate).filter(TaskTemplate.deleted_at.is_(None))
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(TaskTemplate.name.like(like),
                                 TaskTemplate.description.like(like)))
    templates = query.order_by(TaskTemplate.updated_at.desc()).all()
    return [_template_out(db, t, with_yaml=False) for t in templates]


def get_template(db: Session, template_id: str) -> dict:
    return _template_out(db, _get_template(db, template_id), with_yaml=True)


def delete_template(db: Session, template_id: str) -> dict:
    template = _get_template(db, template_id)
    from datetime import datetime, timezone
    template.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "deleted", "id": template_id}


def list_revisions(db: Session, template_id: str) -> list[dict]:
    _get_template(db, template_id)
    revisions = db.query(TaskTemplateRevision).filter(
        TaskTemplateRevision.template_id == template_id,
    ).order_by(TaskTemplateRevision.revision_no.desc()).all()
    return [_revision_out(r) for r in revisions]


def _get_template(db: Session, template_id: str) -> TaskTemplate:
    template = db.query(TaskTemplate).filter(
        TaskTemplate.id == template_id,
        TaskTemplate.deleted_at.is_(None),
    ).first()
    if template is None:
        raise HTTPException(status_code=404, detail={
            "code": "TEMPLATE_NOT_FOUND", "message": "模板不存在"})
    return template


def _template_out(db: Session, template: TaskTemplate, *,
                  with_yaml: bool = False) -> dict:
    revision = db.query(TaskTemplateRevision).filter(
        TaskTemplateRevision.id == template.latest_revision_id).first()
    instance_count = db.query(TaskInstance).filter(
        TaskInstance.template_revision_id.in_(
            [r.id for r in db.query(TaskTemplateRevision.id).filter(
                TaskTemplateRevision.template_id == template.id)])).count()
    data = {
        "id": template.id,
        "name": template.name,
        "description": template.description or "",
        "latest_revision_no": revision.revision_no if revision else None,
        "latest_revision_id": template.latest_revision_id,
        "canonical_hash": revision.canonical_hash if revision else None,
        "instance_count": instance_count,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }
    if with_yaml and revision is not None:
        data["spec_yaml"] = revision.spec_yaml
    return data


def _revision_out(revision: TaskTemplateRevision) -> dict:
    return {
        "id": revision.id,
        "revision_no": revision.revision_no,
        "canonical_hash": revision.canonical_hash,
        "note": revision.note,
        "created_by": revision.created_by,
        "created_at": revision.created_at,
    }


# ---------------------------------------------------------------------------
# 实例
# ---------------------------------------------------------------------------


def activate_instance(
    db: Session, template_id: str, body: schemas.InstanceActivate, user,
    *, idempotency_key: str | None,
) -> dict:
    template = _get_template(db, template_id)
    revision = None
    if body.revision_no is not None:
        revision = db.query(TaskTemplateRevision).filter(
            TaskTemplateRevision.template_id == template.id,
            TaskTemplateRevision.revision_no == body.revision_no,
        ).first()
    else:
        revision = db.query(TaskTemplateRevision).filter(
            TaskTemplateRevision.id == template.latest_revision_id).first()
    if revision is None:
        raise HTTPException(status_code=404, detail={
            "code": "REVISION_NOT_FOUND", "message": "模板版本不存在"})
    if idempotency_key:
        existing = db.query(TaskInstance).filter(
            TaskInstance.idempotency_key == idempotency_key).first()
        if existing is not None:
            return _instance_out(db, existing)
    instance, result = engine.activate(
        db, revision=revision, name=body.name, goal=body.goal,
        inputs=body.inputs, created_by=user, idempotency_key=idempotency_key)
    db.commit()
    _after_commit_dispatch(result)
    _notify_waiting_parties(db, instance)
    return _instance_out(db, instance)


def list_instances(db: Session, *, status: str = "", template_id: str = "",
                   mine: bool = False, user=None, page: int = 1,
                   size: int = 20) -> dict:
    query = db.query(TaskInstance)
    if status:
        if status not in INSTANCE_STATUSES:
            raise HTTPException(status_code=400, detail={
                "code": "BAD_STATUS", "message": f"非法状态 {status}"})
        query = query.filter(TaskInstance.status == status)
    if template_id:
        revision_ids = [r.id for r in db.query(TaskTemplateRevision.id).filter(
            TaskTemplateRevision.template_id == template_id)]
        query = query.filter(TaskInstance.template_revision_id.in_(revision_ids))
    if mine and user is not None:
        query = query.filter(TaskInstance.created_by == user.id)
    total = query.count()
    rows = (query.order_by(TaskInstance.created_at.desc())
            .offset((page - 1) * size).limit(size).all())
    return {"total": total, "page": page, "size": size,
            "items": [_instance_out(db, r, brief=True) for r in rows]}


def get_instance(db: Session, instance_id: str) -> dict:
    instance = _get_instance(db, instance_id)
    return _instance_out(db, instance)


def cancel_instance_api(db: Session, instance_id: str,
                         body: schemas.InstanceCancel, user) -> dict:
    instance = _get_instance(db, instance_id)
    engine.cancel_instance(db, instance_id, reason=body.reason, actor=user)
    db.commit()
    return _instance_out(db, instance)


def _get_instance(db: Session, instance_id: str) -> TaskInstance:
    instance = db.query(TaskInstance).filter(
        TaskInstance.id == instance_id).first()
    if instance is None:
        raise HTTPException(status_code=404, detail={
            "code": "INSTANCE_NOT_FOUND", "message": "实例不存在"})
    return instance


def _instance_out(db: Session, instance: TaskInstance, *,
                  brief: bool = False) -> dict:
    runs = db.query(TaskNodeRun).filter(
        TaskNodeRun.instance_id == instance.id,
    ).order_by(TaskNodeRun.created_at, TaskNodeRun.attempt_no).all()
    needs_attention = any(
        r.status in (NODE_WAITING_HUMAN, NODE_WAITING_APPROVAL) for r in runs)
    data = {
        "id": instance.id,
        "name": instance.name,
        "goal": instance.goal,
        "status": instance.status,
        "needs_attention": needs_attention,
        "template_revision_id": instance.template_revision_id,
        "created_by": instance.created_by,
        "created_at": instance.created_at,
        "finished_at": instance.finished_at,
        "fail_reason": instance.fail_reason,
        "cancel_reason": instance.cancel_reason,
        "nodes": [_node_run_out(r) for r in runs],
    }
    if not brief:
        approvals = db.query(TaskApproval).filter(
            TaskApproval.instance_id == instance.id).all()
        artifacts = db.query(TaskArtifact).filter(
            TaskArtifact.instance_id == instance.id).all()
        data["approvals"] = [{
            "id": a.id, "node_run_id": a.node_run_id,
            "status": a.status, "reason": a.reason,
            "proposal": a.proposal, "expires_at": a.expires_at,
            "decided_by": a.decided_by, "decided_at": a.decided_at,
        } for a in approvals]
        data["artifacts"] = [{
            "id": a.id, "node_run_id": a.node_run_id, "name": a.name,
            "mime_type": a.mime_type, "size_bytes": a.size_bytes,
            "sha256": a.sha256, "created_at": a.created_at,
        } for a in artifacts]
        data["spec_snapshot"] = instance.spec_snapshot
    return data


def _node_run_out(run: TaskNodeRun) -> dict:
    node_id = run.node_id
    waiting = run.status in (NODE_WAITING_HUMAN, NODE_WAITING_APPROVAL)
    return {
        "node_run_id": run.id,
        "node_id": node_id,
        "attempt_no": run.attempt_no,
        "status": run.status,
        "waiting": waiting,
        "error": run.error,
        "correction_count": run.correction_count,
        "rework_count": run.rework_count,
        "output": run.output,
        "dispatched_at": run.dispatched_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
    }


# ---------------------------------------------------------------------------
# 节点交互：审批 / 人工交活 / 打回 / 插话
# ---------------------------------------------------------------------------


def decide_approval_api(db: Session, instance_id: str, node_id: str,
                        approval_id: str, body: schemas.ApprovalDecision,
                        user) -> dict:
    approval = _get_approval(db, instance_id, node_id, approval_id)
    if body.decision == "rejected" and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail={
            "code": "REJECT_REASON_REQUIRED", "message": "驳回必须携带理由"})
    try:
        result = engine.decide_approval(
            db, approval.id, body.decision, reason=body.reason,
            decided_by=user)
    except engine.ReworkBudgetExhausted as exc:
        raise HTTPException(status_code=409, detail={
            "code": "REWORK_BUDGET_EXHAUSTED", "message": str(exc)}) from exc
    except engine.EngineGuardError as exc:
        raise HTTPException(status_code=409, detail={
            "code": exc.code, "message": str(exc)}) from exc
    db.commit()
    _after_commit_dispatch(result)
    _notify_waiting_parties(db, _get_instance(db, instance_id))
    return _instance_out(db, _get_instance(db, instance_id))


def human_submit_api(db: Session, instance_id: str, node_id: str,
                     body: schemas.HumanSubmit, user) -> dict:
    run = _get_waiting_run(db, instance_id, node_id, NODE_WAITING_HUMAN)
    artifacts = [_store_artifact(instance_id, run, submission)
                 for submission in (body.artifacts or [])]
    result = engine.complete_node(
        db, run.id, body.output, artifacts=artifacts,
        actor=f"user:{getattr(user, 'id', 'unknown')}")
    db.commit()
    _after_commit_dispatch(result)
    _notify_waiting_parties(db, _get_instance(db, instance_id))
    return _instance_out(db, _get_instance(db, instance_id))


def human_reject_api(db: Session, instance_id: str, node_id: str,
                     body: schemas.HumanReject, user) -> dict:
    run = _get_waiting_run(db, instance_id, node_id, NODE_WAITING_HUMAN)
    try:
        result = engine.human_reject(
            db, run.id, reason=body.reason, rejected_by=user,
            target_node_id=body.target_node_id)
    except engine.ReworkBudgetExhausted as exc:
        raise HTTPException(status_code=409, detail={
            "code": "REWORK_BUDGET_EXHAUSTED", "message": str(exc)}) from exc
    except engine.EngineGuardError as exc:
        raise HTTPException(status_code=409, detail={
            "code": exc.code, "message": str(exc)}) from exc
    db.commit()
    _after_commit_dispatch(result)
    _notify_waiting_parties(db, _get_instance(db, instance_id))
    return _instance_out(db, _get_instance(db, instance_id))


def steering_api(db: Session, instance_id: str, body: schemas.SteeringSubmit,
                 user, *, idempotency_key: str | None) -> dict:
    instance = _get_instance(db, instance_id)
    run = db.query(TaskNodeRun).filter(
        TaskNodeRun.instance_id == instance_id,
        TaskNodeRun.node_id == body.node_id,
    ).order_by(TaskNodeRun.attempt_no.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail={
            "code": "NODE_NOT_FOUND", "message": "节点不存在"})
    if run.status not in (NODE_DISPATCHED, NODE_RUNNING):
        raise HTTPException(status_code=409, detail={
            "code": "NODE_NOT_RUNNING",
            "message": f"节点状态为 {run.status}，仅执行中节点接受插话"})
    if idempotency_key:
        existing = db.query(TaskSteeringMessage).filter(
            TaskSteeringMessage.idempotency_key == idempotency_key).first()
        if existing is not None:
            return {"id": existing.id, "status": existing.status,
                    "node_run_id": existing.node_run_id}
    message = TaskSteeringMessage(
        instance_id=instance_id, node_run_id=run.id, content=body.content,
        created_by=f"user:{getattr(user, 'id', 'unknown')}",
        idempotency_key=idempotency_key)
    db.add(message)
    db.commit()
    _dispatch_steering(run.id, message.id, body.content)
    return {"id": message.id, "status": message.status,
            "node_run_id": run.id, "instance_id": instance.id}


def _get_approval(db: Session, instance_id: str, node_id: str,
                  approval_id: str) -> TaskApproval:
    approval = db.query(TaskApproval).filter(
        TaskApproval.id == approval_id,
        TaskApproval.instance_id == instance_id,
    ).first()
    if approval is None:
        raise HTTPException(status_code=404, detail={
            "code": "APPROVAL_NOT_FOUND", "message": "审批不存在"})
    run = db.query(TaskNodeRun).filter(
        TaskNodeRun.id == approval.node_run_id).first()
    if run is None or run.node_id != node_id:
        raise HTTPException(status_code=404, detail={
            "code": "APPROVAL_NOT_FOUND", "message": "审批不属于该节点"})
    return approval


def _get_waiting_run(db: Session, instance_id: str, node_id: str,
                     expected: str) -> TaskNodeRun:
    run = db.query(TaskNodeRun).filter(
        TaskNodeRun.instance_id == instance_id,
        TaskNodeRun.node_id == node_id,
    ).order_by(TaskNodeRun.attempt_no.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail={
            "code": "NODE_NOT_FOUND", "message": "节点不存在"})
    if run.status != expected:
        raise HTTPException(status_code=409, detail={
            "code": "NODE_NOT_WAITING",
            "message": f"节点状态为 {run.status}，期望 {expected}"})
    return run


def _store_artifact(instance_id: str, run: TaskNodeRun,
                    submission: schemas.ArtifactSubmission) -> dict:
    """产物先落存储再参与交活（sha256 防篡改；存储 seam 见 shared/storage）。"""
    content = base64.b64decode(submission.content_base64, validate=True)
    digest = hashlib.sha256(content).hexdigest()
    storage_uri = _write_artifact_object(
        f"{instance_id}/{run.node_id}/{run.attempt_no}/{submission.name}",
        content, submission.mime_type)
    return {"name": submission.name, "mime_type": submission.mime_type,
            "size_bytes": len(content), "sha256": digest,
            "storage_uri": storage_uri}


def _write_artifact_object(key: str, content: bytes, mime_type: str) -> str:
    """对象存储 seam：测试可 monkeypatch；默认走平台 MinIO 封装。单点失败
    即整体 400（产物未落盘不得交活——防容器销毁丢证据，设计 §6.2）。"""
    from app.shared.storage import get_storage_service

    return get_storage_service().put_bytes(
        f"task-instances/{key}", content, content_type=mime_type)


def _notify_waiting_parties(db: Session, instance: TaskInstance) -> None:
    """waiting_human / waiting_approval 投递站内通知（旁路，失败仅告警）。

    直发 publish_event（与定时任务结果通知同款路径）；open_key 聚合使
    同一等待项重复指派时重置未读而不是堆积条目。
    """
    try:
        from datetime import datetime, timezone
        import uuid as _uuid

        from app.inbox.schemas import (
            InboxAction,
            InboxAudience,
            InboxContent,
            InboxEventIn,
            InboxResource,
            InboxSource,
        )
        from app.inbox.service import publish_event

        runs = db.query(TaskNodeRun).filter(
            TaskNodeRun.instance_id == instance.id,
            TaskNodeRun.status.in_((NODE_WAITING_HUMAN, NODE_WAITING_APPROVAL)),
        ).all()
        spec = instance.spec_snapshot or {}
        for run in runs:
            node = (spec.get("nodes") or {}).get(run.node_id, {})
            kind = node.get("kind")
            if kind == "approval" and run.status == NODE_WAITING_APPROVAL:
                title = f"任务实例待审批：{instance.name}"
                summary = f"节点 {run.node_id} 等待审批决定。"
            elif kind == "human" and run.status == NODE_WAITING_HUMAN:
                title = f"任务实例待办：{instance.name}"
                summary = (f"节点 {run.node_id}"
                           f"（{node.get('role', '')}）等待交活。")
            else:
                continue
            href = f"/task-instances?instance={instance.id}"
            publish_event(db, InboxEventIn(
                event_id=f"task-instance:{instance.id}:{run.node_id}:"
                         f"{run.attempt_no}:{_uuid.uuid4().hex[:8]}",
                occurred_at=datetime.now(timezone.utc),
                operation="append",
                source=InboxSource(
                    system="task_instances",
                    type="platform",
                    id=f"instance:{instance.id}:node:{run.node_id}",
                    correlation_key=instance.id,
                ),
                item=InboxContent(
                    kind="task", priority="normal",
                    title=title, summary=summary,
                    safe_context={"instanceId": instance.id,
                                  "nodeId": run.node_id},
                ),
                resource=InboxResource(
                    type="task_instance", id=instance.id,
                    label=instance.name, href=href,
                ),
                audience=InboxAudience(type="role", role="admin"),
                actions=[InboxAction(
                    key="open", label="查看任务实例", href=href)],
            ))
        db.commit()
    except Exception:  # noqa: BLE001 — 通知是旁路能力
        db.rollback()
        logger.exception("任务实例站内通知投递失败: %s", instance.id)
