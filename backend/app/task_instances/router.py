"""任务实例 API — 挂 /api/v2/task-instances（menu key: task_instances）

  模板与版本：
  GET    /templates                          列表（keyword）
  POST   /templates                          创建（YAML 编译 + 语义校验）
  GET    /templates/{id}                     详情（含 spec_yaml）
  PUT    /templates/{id}                     更新（新 revision，canonical_hash 幂等）
  DELETE /templates/{id}                     软删
  POST   /templates/validate                 干校验（不落库）
  GET    /templates/{id}/revisions           版本历史

  实例：
  POST   /templates/{id}/instances           激活（Idempotency-Key 必填）
  GET    /instances                          列表（status/template/mine/分页）
  GET    /instances/{id}                     详情（节点树/审批/产物/spec 快照）
  GET    /instances/{id}/events              SSE：instance.snapshot 首帧 + 事件流
  POST   /instances/{id}/cancel              取消（两阶段冻结语义在单事务内）

  节点交互：
  POST   /instances/{id}/nodes/{node}/approvals/{aid}/decision   审批决定
  POST   /instances/{id}/nodes/{node}/human/submit               人工交活
  POST   /instances/{id}/nodes/{node}/reject                     打回直接上游
  POST   /instances/{id}/steering                                插话（运行中节点）
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user, get_db
from app.task_instances import events as ev
from app.task_instances import schemas, service
from app.task_instances.models import (
    INSTANCE_CANCELLED,
    INSTANCE_COMPLETED,
    INSTANCE_FAILED,
    TaskEvent,
    TaskInstance,
)

router = APIRouter()


def _ok(data):
    return {"data": data}


# ── 模板 ──


@router.get("/templates")
def list_templates(
    keyword: str = Query(default=""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.list_templates(db, keyword=keyword))


@router.post("/templates", status_code=201)
def create_template(
    body: schemas.TemplateCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.create_template(db, body, current_user))


@router.post("/templates/validate")
def validate_template(
    body: schemas.TemplateValidate,
    current_user=Depends(get_current_user),
):
    return _ok(service.validate_only(body.spec_yaml))


@router.get("/templates/{template_id}")
def get_template(
    template_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.get_template(db, template_id))


@router.put("/templates/{template_id}")
def update_template(
    template_id: str,
    body: schemas.TemplateUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.update_template(db, template_id, body, current_user))


@router.delete("/templates/{template_id}")
def delete_template(
    template_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.delete_template(db, template_id))


@router.get("/templates/{template_id}/revisions")
def list_revisions(
    template_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.list_revisions(db, template_id))


# ── 实例 ──


@router.post("/templates/{template_id}/instances", status_code=202)
def activate_instance(
    template_id: str,
    body: schemas.InstanceActivate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail={
            "code": "IDEMPOTENCY_KEY_REQUIRED",
            "message": "激活实例必须携带 1~128 字符的 Idempotency-Key 头"})
    return _ok(service.activate_instance(
        db, template_id, body, current_user,
        idempotency_key=idempotency_key))


@router.get("/instances")
def list_instances(
    status: str = Query(default=""),
    template_id: str = Query(default=""),
    mine: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.list_instances(
        db, status=status, template_id=template_id, mine=mine,
        user=current_user, page=page, size=size))


@router.get("/instances/{instance_id}")
def get_instance(
    instance_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.get_instance(db, instance_id))


@router.post("/instances/{instance_id}/cancel")
def cancel_instance(
    instance_id: str,
    body: schemas.InstanceCancel,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.cancel_instance_api(db, instance_id, body, current_user))


# ── 节点交互 ──


@router.post("/instances/{instance_id}/nodes/{node_id}/approvals/"
             "{approval_id}/decision")
def decide_approval(
    instance_id: str,
    node_id: str,
    approval_id: str,
    body: schemas.ApprovalDecision,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.decide_approval_api(
        db, instance_id, node_id, approval_id, body, current_user))


@router.post("/instances/{instance_id}/nodes/{node_id}/human/submit")
def human_submit(
    instance_id: str,
    node_id: str,
    body: schemas.HumanSubmit,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.human_submit_api(
        db, instance_id, node_id, body, current_user))


@router.post("/instances/{instance_id}/nodes/{node_id}/reject")
def human_reject(
    instance_id: str,
    node_id: str,
    body: schemas.HumanReject,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _ok(service.human_reject_api(
        db, instance_id, node_id, body, current_user))


@router.post("/instances/{instance_id}/steering", status_code=202)
def submit_steering(
    instance_id: str,
    body: schemas.SteeringSubmit,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    return _ok(service.steering_api(
        db, instance_id, body, current_user,
        idempotency_key=idempotency_key or None))


# ── SSE 事件流（kernel 先例：快照首帧 + 游标 + 终态断开） ──


@router.get("/instances/{instance_id}/events",
            response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {}}}})
def stream_instance_events(
    instance_id: str,
    after_seq: int = Query(default=-1, ge=-1),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    instance = db.query(TaskInstance).filter(
        TaskInstance.id == instance_id).first()
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    cursor = after_seq
    if last_event_id:
        parts = last_event_id.split(":")
        if len(parts) != 2 or parts[0] != instance_id:
            raise HTTPException(status_code=400,
                                detail="Last-Event-ID must be instance_id:seq")
        try:
            cursor = int(parts[1])
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail="Last-Event-ID must be instance_id:seq"
                                ) from exc
    # SSE 订阅不得长期钉住请求会话：每批读取用独立短会话
    db.rollback()

    def read_batch():
        session = SessionLocal()
        try:
            current = session.scalar(select(TaskInstance).where(
                TaskInstance.id == instance_id))
            events = session.scalars(
                select(TaskEvent)
                .where(TaskEvent.instance_id == instance_id,
                       TaskEvent.seq > cursor)
                .order_by(TaskEvent.seq).limit(100)
            ).all()
            snapshot = None
            if current is not None:
                snapshot = {
                    "instance": {
                        "id": current.id, "name": current.name,
                        "status": current.status, "goal": current.goal,
                    },
                }
            return snapshot, events
        finally:
            session.close()

    async def generate():
        nonlocal cursor
        last_ping = time.monotonic()
        sent_snapshot = False
        while True:
            snapshot, events = await asyncio.to_thread(read_batch)
            if not sent_snapshot and snapshot is not None:
                yield (f"event: instance.snapshot\n"
                       f"data: {json.dumps(snapshot, ensure_ascii=False, default=str)}"
                       f"\nretry: 5000\n\n")
                sent_snapshot = True
            for event in events:
                cursor = event.seq
                yield (f"id: {instance_id}:{event.seq}\n"
                       f"event: {event.type}\n"
                       f"data: {json.dumps(event.payload, ensure_ascii=False, default=str)}"
                       f"\nretry: 5000\n\n")
            status = (snapshot or {}).get("instance", {}).get("status")
            if status in (INSTANCE_COMPLETED, INSTANCE_FAILED,
                          INSTANCE_CANCELLED) and not events:
                break
            if snapshot is None:
                break
            if time.monotonic() - last_ping >= 15:
                yield ": ping\n\n"
                last_ping = time.monotonic()
            await asyncio.sleep(0.25)

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
