from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.auth.models import User
from app.auth.permissions import user_has_menu_access
from app.inbox.models import (
    InboxDelivery,
    InboxEventReceipt,
    InboxItem,
    InboxOutboxEvent,
)
from app.inbox.schemas import InboxEventIn


logger = logging.getLogger(__name__)

_URL_CREDENTIALS = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^\s/:@]+):([^\s/@]+)@"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def _now() -> datetime:
    return datetime.utcnow()


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="milliseconds") + "Z"


def _safe_error_summary(value: str) -> str:
    """Bound and redact an exception string before it enters a user surface."""
    summary = " ".join((value or "执行失败").split())
    summary = _URL_CREDENTIALS.sub(r"\1***:***@", summary)
    summary = _BEARER_TOKEN.sub("Bearer ***", summary)
    summary = _SECRET_ASSIGNMENT.sub(r"\1\2***", summary)
    return (summary or "执行失败")[:500]


def _open_key(event: InboxEventIn) -> str:
    identity = event.event_id if event.operation == "append" else event.source.correlation_key
    raw = f"{event.source.system}:{event.source.type}:{identity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload_hash(event: InboxEventIn) -> str:
    payload = event.model_dump(mode="json", by_alias=True, exclude_none=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _active_recipients(db: Session, event: InboxEventIn) -> list[str]:
    audience = event.audience
    if audience is None:
        return []
    query = db.query(User).filter(User.is_active.is_(True))
    if audience.type == "user":
        query = query.filter(User.id == audience.user_id)
    elif audience.type == "users":
        query = query.filter(User.id.in_(set(audience.user_ids)))
    else:
        query = query.filter(User.role == audience.role)
    return [row.id for row in query.all()]


def publish_event(db: Session, event: InboxEventIn) -> InboxItem | None:
    """Project one versioned event into item/delivery rows without committing."""
    payload_hash = _payload_hash(event)
    receipt = db.query(InboxEventReceipt).filter(
        InboxEventReceipt.event_id == event.event_id,
    ).first()
    if receipt is not None:
        if receipt.payload_hash != payload_hash:
            raise ValueError(f"inbox eventId {event.event_id!r} was reused with another payload")
        if receipt.item_id:
            return db.query(InboxItem).filter(InboxItem.id == receipt.item_id).first()
        return None

    key = _open_key(event)
    item = db.query(InboxItem).filter(InboxItem.open_key == key).with_for_update().first()
    occurred_at = _naive_utc(event.occurred_at)

    if event.operation == "close":
        if item is not None:
            item.business_state = event.resolution.state
            item.open_key = None
            item.resolved_at = occurred_at
            item.resolution_reason = event.resolution.reason or None
            item.latest_occurrence_id = event.source.occurrence_id
            item.last_occurred_at = occurred_at
            item.updated_at = _now()
            db.flush()
        db.add(InboxEventReceipt(
            event_id=event.event_id,
            payload_hash=payload_hash,
            operation=event.operation,
            source_system=event.source.system,
            source_type=event.source.type,
            item_id=item.id if item else None,
        ))
        db.flush()
        return item

    recipients = _active_recipients(db, event)
    if item is None:
        item = InboxItem(
            schema_version=event.schema_version,
            source_system=event.source.system,
            source_type=event.source.type,
            source_id=event.source.id,
            correlation_key=event.source.correlation_key,
            open_key=key,
            kind=event.item.kind,
            priority=event.item.priority,
            business_state="open",
            title=event.item.title,
            summary=event.item.summary,
            safe_context=event.item.safe_context,
            resource=event.resource.model_dump(by_alias=True),
            actions=[action.model_dump(by_alias=True) for action in event.actions],
            occurrence_count=1,
            latest_occurrence_id=event.source.occurrence_id,
            first_occurred_at=occurred_at,
            last_occurred_at=occurred_at,
            expires_at=(
                _naive_utc(event.item.expires_at) if event.item.expires_at else None
            ),
        )
        db.add(item)
        db.flush()
    else:
        item.source_id = event.source.id
        item.kind = event.item.kind
        item.priority = event.item.priority
        item.title = event.item.title
        item.summary = event.item.summary
        item.safe_context = event.item.safe_context
        item.resource = event.resource.model_dump(by_alias=True)
        item.actions = [action.model_dump(by_alias=True) for action in event.actions]
        item.occurrence_count = int(item.occurrence_count or 0) + 1
        item.latest_occurrence_id = event.source.occurrence_id
        item.last_occurred_at = occurred_at
        item.expires_at = (
            _naive_utc(event.item.expires_at) if event.item.expires_at else None
        )
        item.updated_at = _now()
        db.flush()

    existing = {
        delivery.recipient_user_id: delivery
        for delivery in db.query(InboxDelivery).filter(InboxDelivery.item_id == item.id).all()
    }
    for user_id in recipients:
        delivery = existing.get(user_id)
        if delivery is None:
            db.add(InboxDelivery(item_id=item.id, recipient_user_id=user_id))
        else:
            # A fresh occurrence must become visible again even if the recipient
            # had already read the previous attempt in the same incident.
            delivery.delivery_state = "unread"
            delivery.read_at = None
            delivery.archived_at = None
            delivery.updated_at = _now()

    db.add(InboxEventReceipt(
        event_id=event.event_id,
        payload_hash=payload_hash,
        operation=event.operation,
        source_system=event.source.system,
        source_type=event.source.type,
        item_id=item.id,
    ))
    db.flush()
    return item


def enqueue_pipeline_task_result(
    db: Session,
    *,
    task_id: str,
    status: str,
    error: str,
    occurrence_id: str,
    run_id: str | None,
    trigger_type: str | None,
    occurred_at: datetime | None = None,
) -> str | None:
    if status not in {"failed", "success"}:
        return None
    event_id = f"pipeline-task:{task_id}:{occurrence_id}:{status}"
    if db.query(InboxOutboxEvent.id).filter(InboxOutboxEvent.id == event_id).first():
        return event_id
    db.add(InboxOutboxEvent(
        id=event_id,
        event_type="pipeline_task_result",
        payload={
            "taskId": task_id,
            "status": status,
            "error": (error or "")[:4000],
            "occurrenceId": occurrence_id,
            "runId": run_id,
            "triggerType": trigger_type or "manual",
            "occurredAt": _iso(occurred_at or _now()),
        },
    ))
    return event_id


def enqueue_ontology_approval_request(
    db: Session,
    *,
    log_id: str,
    ontology_id: str,
    action_name: str,
    ontology_version: str | None,
    ontology_release_id: str | None,
    occurred_at: datetime | None = None,
) -> str:
    """Durably notify governance users about a pending ontology action.

    The outbox contains only bounded, safe display metadata.  Action
    parameters and execution details remain in the authoritative ontology
    tables and are fetched after the user follows the internal link.
    """
    event_id = f"ontology-approval:{log_id}:requested"
    if db.query(InboxOutboxEvent.id).filter(InboxOutboxEvent.id == event_id).first():
        return event_id
    db.add(InboxOutboxEvent(
        id=event_id,
        event_type="ontology_approval",
        payload={
            "phase": "requested",
            "logId": log_id,
            "ontologyId": ontology_id,
            "actionName": (action_name or "待审批动作")[:200],
            "ontologyVersion": (ontology_version or "")[:20],
            "ontologyReleaseId": (ontology_release_id or "")[:200],
            "occurredAt": _iso(occurred_at or _now()),
        },
    ))
    return event_id


def enqueue_ontology_approval_decision(
    db: Session,
    *,
    log_id: str,
    ontology_id: str,
    decision: str,
    action_name: str,
    ontology_version: str | None,
    ontology_release_id: str | None,
    occurred_at: datetime | None = None,
) -> str:
    """Durably close the corresponding approval inbox item."""
    if decision not in {"approved", "rejected", "failed"}:
        raise ValueError("unsupported ontology approval decision")
    event_id = f"ontology-approval:{log_id}:{decision}"
    if db.query(InboxOutboxEvent.id).filter(InboxOutboxEvent.id == event_id).first():
        return event_id
    db.add(InboxOutboxEvent(
        id=event_id,
        event_type="ontology_approval",
        payload={
            "phase": "decision",
            "decision": decision,
            "logId": log_id,
            "ontologyId": ontology_id,
            "actionName": (action_name or "待审批动作")[:200],
            "ontologyVersion": (ontology_version or "")[:20],
            "ontologyReleaseId": (ontology_release_id or "")[:200],
            "occurredAt": _iso(occurred_at or _now()),
        },
    ))
    return event_id


def _pipeline_task_recipient_ids(db: Session, task, pipeline) -> list[str]:
    candidates = [getattr(task, "created_by", None), getattr(pipeline, "created_by", None)]
    for candidate in candidates:
        if not candidate:
            continue
        user = db.query(User).filter(User.id == candidate, User.is_active.is_(True)).first()
        if user and user_has_menu_access(db, user, "data.sync_tasks"):
            return [user.id]
    admins = db.query(User).filter(User.role == "admin", User.is_active.is_(True)).all()
    return [user.id for user in admins]


def _dispatch_pipeline_task_result(db: Session, row: InboxOutboxEvent) -> None:
    from app.data_channel.pipeline_tasks.models import PipelineTask
    from app.data_channel.pipelines.models import Pipeline

    payload = row.payload or {}
    task = db.query(PipelineTask).filter(PipelineTask.id == payload.get("taskId")).first()
    if task is None:
        return
    pipeline = db.query(Pipeline).filter(Pipeline.id == task.pipeline_id).first()
    recipients = _pipeline_task_recipient_ids(db, task, pipeline)
    occurred = payload.get("occurredAt") or task.last_run_at or row.created_at or _now()
    run_id = payload.get("runId")
    query = {"task_id": task.id}
    if run_id:
        query["run_id"] = run_id
    href = "/data/pipelines/sync-tasks?" + urlencode(query)
    source = {
        "system": "data_channel",
        "type": "pipeline_task_failure",
        "id": task.id,
        "occurrenceId": payload.get("occurrenceId"),
        "correlationKey": f"pipeline-task:{task.id}:failure",
    }

    if payload.get("status") == "success":
        event = InboxEventIn.model_validate({
            "schemaVersion": "v1",
            "eventId": row.id,
            "occurredAt": occurred,
            "operation": "close",
            "source": source,
            "resolution": {"state": "resolved", "reason": "next_run_succeeded"},
        })
    else:
        # A deleted/deactivated owner and an installation without an active
        # administrator must not poison the durable queue forever. There is no
        # valid personal delivery target, so acknowledge this result without
        # creating an orphan inbox item.
        if not recipients:
            logger.warning("PipelineTask %s 无可用收件箱接收人，跳过失败告警", task.id)
            return
        safe_error = _safe_error_summary(str(payload.get("error") or "执行失败"))
        event = InboxEventIn.model_validate({
            "schemaVersion": "v1",
            "eventId": row.id,
            "occurredAt": occurred,
            "operation": "upsert",
            "source": source,
            "item": {
                "kind": "alert",
                "priority": "high",
                "title": f"数据任务执行失败：{task.name}",
                "summary": safe_error,
                "safeContext": {
                    "taskName": task.name,
                    "pipelineName": pipeline.name if pipeline else "关联流水线不可用",
                    "triggerType": payload.get("triggerType") or "manual",
                    "latestRunId": run_id,
                    "errorSummary": safe_error,
                },
            },
            "resource": {
                "type": "pipeline_task_run",
                "id": run_id or task.id,
                "label": task.name,
                "href": href,
            },
            "audience": {"type": "users", "userIds": recipients},
            "actions": [{"key": "open", "label": "查看执行记录", "href": href}],
        })
    publish_event(db, event)


def _approval_recipient_ids(db: Session) -> list[str]:
    # The formal governance decision endpoint is admin-only. Keep the inbox
    # audience aligned with that authorization boundary.
    return [
        user.id
        for user in db.query(User).filter(
            User.role == "admin",
            User.is_active.is_(True),
        ).all()
    ]


def _dispatch_ontology_approval(db: Session, row: InboxOutboxEvent) -> None:
    payload = row.payload or {}
    log_id = str(payload.get("logId") or "")
    ontology_id = str(payload.get("ontologyId") or "")
    if not log_id or not ontology_id:
        raise ValueError("ontology approval outbox payload missing identity")
    correlation_key = f"ontology-approval:{log_id}"
    source = {
        "system": "ontology",
        "type": "action_approval",
        "id": log_id,
        "occurrenceId": row.id,
        "correlationKey": correlation_key,
    }
    occurred = payload.get("occurredAt") or row.created_at or _now()
    if payload.get("phase") == "decision":
        # Preserve ordering if a terminal event is picked up before the
        # request projection (for example after a partial worker crash).
        item = db.query(InboxItem).filter(
            InboxItem.source_system == "ontology",
            InboxItem.source_type == "action_approval",
            InboxItem.correlation_key == correlation_key,
            InboxItem.business_state == "open",
        ).first()
        if item is None and _approval_recipient_ids(db):
            raise RuntimeError("approval request projection has not completed")
        event = InboxEventIn.model_validate({
            "schemaVersion": "v1",
            "eventId": row.id,
            "occurredAt": occurred,
            "operation": "close",
            "source": source,
            "resolution": {
                "state": "resolved" if payload.get("decision") == "approved" else "cancelled",
                "reason": f"approval_{payload.get('decision')}",
            },
        })
    else:
        recipients = _approval_recipient_ids(db)
        if not recipients:
            # No governance recipient is a valid installation state; ack the
            # event without creating an orphan notification.
            return
        href = (
            f"/ontologies/{ontology_id}?tab=governance&approval_id={log_id}"
        )
        action_name = str(payload.get("actionName") or "待审批动作")[:200]
        version = str(payload.get("ontologyVersion") or "")[:20]
        event = InboxEventIn.model_validate({
            "schemaVersion": "v1",
            "eventId": row.id,
            "occurredAt": occurred,
            "operation": "upsert",
            "source": source,
            "item": {
                "kind": "task",
                "priority": "high",
                "title": f"待审批：{action_name}",
                "summary": f"本体动作等待治理审批（版本 {version or '未标注'}）",
                "safeContext": {
                    "ontologyId": ontology_id,
                    "approvalLogId": log_id,
                    "ontologyVersion": version,
                },
            },
            "resource": {
                "type": "ontology_action_approval",
                "id": log_id,
                "label": action_name,
                "href": href,
            },
            "audience": {"type": "users", "userIds": recipients},
            "actions": [{"key": "open", "label": "查看并审批", "href": href}],
        })
    publish_event(db, event)


def drain_outbox(db: Session, *, event_id: str | None = None, limit: int = 50) -> dict[str, int]:
    query = db.query(InboxOutboxEvent).filter(InboxOutboxEvent.status == "pending")
    if event_id:
        query = query.filter(InboxOutboxEvent.id == event_id)
    rows = query.order_by(InboxOutboxEvent.created_at.asc()).limit(limit).all()
    processed = failed = 0
    for candidate in rows:
        row_id = candidate.id
        try:
            row = db.query(InboxOutboxEvent).filter(
                InboxOutboxEvent.id == row_id,
                InboxOutboxEvent.status == "pending",
            ).with_for_update().first()
            if row is None:
                continue
            row.status = "processing"
            row.attempts = int(row.attempts or 0) + 1
            db.flush()
            if row.event_type == "pipeline_task_result":
                _dispatch_pipeline_task_result(db, row)
            elif row.event_type == "ontology_approval":
                _dispatch_ontology_approval(db, row)
            else:
                raise ValueError(f"unknown inbox outbox event type: {row.event_type}")
            row.status = "completed"
            row.processed_at = _now()
            row.last_error = ""
            db.commit()
            processed += 1
        except Exception as exc:  # noqa: BLE001 - durable event remains retryable
            db.rollback()
            retry = db.query(InboxOutboxEvent).filter(InboxOutboxEvent.id == row_id).first()
            if retry is not None:
                retry.status = "pending"
                retry.attempts = int(retry.attempts or 0) + 1
                retry.last_error = str(exc)[:2000]
                db.commit()
            failed += 1
            logger.exception("收件箱 outbox 投递失败: %s", row_id)
    return {"processed": processed, "failed": failed}


def _encode_cursor(item: InboxItem, delivery: InboxDelivery) -> str:
    payload = json.dumps({
        "at": _iso(item.last_occurred_at),
        "id": delivery.id,
    }, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        at = datetime.fromisoformat(str(payload["at"]).replace("Z", "+00:00"))
        return _naive_utc(at), str(payload["id"])
    except Exception as exc:
        raise HTTPException(400, "无效的收件箱游标") from exc


def _delivery_dict(delivery: InboxDelivery, item: InboxItem) -> dict[str, Any]:
    context = dict(item.safe_context or {})
    context["failureCount"] = item.occurrence_count
    return {
        "id": delivery.id,
        "itemId": item.id,
        "kind": item.kind,
        "priority": item.priority,
        "businessState": item.business_state,
        "deliveryState": delivery.delivery_state,
        "title": item.title,
        "summary": item.summary,
        "safeContext": context,
        "source": {
            "system": item.source_system,
            "type": item.source_type,
            "id": item.source_id,
            "occurrenceId": item.latest_occurrence_id,
        },
        "resource": item.resource or {},
        "actions": item.actions or [],
        "occurrenceCount": item.occurrence_count,
        "firstOccurredAt": _iso(item.first_occurred_at),
        "lastOccurredAt": _iso(item.last_occurred_at),
        "resolvedAt": _iso(item.resolved_at),
        "expiresAt": _iso(item.expires_at),
        "resolutionReason": item.resolution_reason,
        "readAt": _iso(delivery.read_at),
        "createdAt": _iso(delivery.created_at),
        "canArchive": item.business_state != "open" or item.kind == "notice",
    }


def list_deliveries(
    db: Session,
    *,
    user_id: str,
    tab: str = "all",
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict:
    query = db.query(InboxDelivery, InboxItem).join(
        InboxItem, InboxItem.id == InboxDelivery.item_id,
    ).filter(InboxDelivery.recipient_user_id == user_id)
    if tab == "actionable":
        query = query.filter(
            InboxItem.business_state == "open",
            InboxItem.kind.in_(("task", "alert")),
            InboxDelivery.delivery_state != "archived",
        )
    elif tab == "unread":
        query = query.filter(InboxDelivery.delivery_state == "unread")
    elif tab == "resolved":
        query = query.filter(
            InboxItem.business_state == "resolved",
            InboxDelivery.delivery_state != "archived",
        )
    elif tab == "archived":
        query = query.filter(InboxDelivery.delivery_state == "archived")
    elif tab == "all":
        query = query.filter(InboxDelivery.delivery_state != "archived")
    else:
        raise HTTPException(400, "tab 仅支持 actionable、unread、resolved、all、archived")
    if kind:
        if kind not in {"task", "alert", "notice"}:
            raise HTTPException(400, "kind 仅支持 task、alert、notice")
        query = query.filter(InboxItem.kind == kind)
    if cursor:
        occurred_at, delivery_id = _decode_cursor(cursor)
        query = query.filter(or_(
            InboxItem.last_occurred_at < occurred_at,
            and_(
                InboxItem.last_occurred_at == occurred_at,
                InboxDelivery.id < delivery_id,
            ),
        ))
    rows = query.order_by(
        InboxItem.last_occurred_at.desc(), InboxDelivery.id.desc(),
    ).limit(limit + 1).all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    if has_more and visible:
        last_delivery, last_item = visible[-1]
        next_cursor = _encode_cursor(last_item, last_delivery)
    else:
        next_cursor = None
    return {
        "items": [_delivery_dict(delivery, item) for delivery, item in visible],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


def inbox_summary(db: Session, *, user_id: str) -> dict:
    base = db.query(InboxDelivery, InboxItem).join(
        InboxItem, InboxItem.id == InboxDelivery.item_id,
    ).filter(InboxDelivery.recipient_user_id == user_id)
    open_alerts = base.filter(
        InboxItem.business_state == "open",
        InboxItem.kind == "alert",
        InboxDelivery.delivery_state != "archived",
    ).count()
    actionable = base.filter(
        InboxItem.business_state == "open",
        InboxItem.kind.in_(("task", "alert")),
        InboxDelivery.delivery_state != "archived",
    ).count()
    unread = base.filter(InboxDelivery.delivery_state == "unread").count()
    resolved = base.filter(
        InboxItem.business_state == "resolved",
        InboxDelivery.delivery_state != "archived",
    ).count()
    return {
        "openAlertCount": open_alerts,
        "actionableCount": actionable,
        "unreadCount": unread,
        "resolvedCount": resolved,
    }


def get_delivery(db: Session, *, user_id: str, delivery_id: str) -> tuple[InboxDelivery, InboxItem]:
    row = db.query(InboxDelivery, InboxItem).join(
        InboxItem, InboxItem.id == InboxDelivery.item_id,
    ).filter(
        InboxDelivery.id == delivery_id,
        InboxDelivery.recipient_user_id == user_id,
    ).first()
    if row is None:
        raise HTTPException(404, "收件箱消息不存在")
    return row


def update_delivery_state(
    db: Session, *, user_id: str, delivery_id: str, state: str,
) -> dict:
    delivery, item = get_delivery(db, user_id=user_id, delivery_id=delivery_id)
    now = _now()
    if state == "archived" and item.business_state == "open" and item.kind in {"task", "alert"}:
        raise HTTPException(409, "进行中的待办或告警不能归档")
    delivery.delivery_state = state
    delivery.updated_at = now
    if state == "read":
        delivery.read_at = now
        delivery.archived_at = None
    elif state == "unread":
        delivery.read_at = None
        delivery.archived_at = None
    else:
        delivery.archived_at = now
        if delivery.read_at is None:
            delivery.read_at = now
    db.commit()
    db.refresh(delivery)
    return _delivery_dict(delivery, item)


def mark_all_read(db: Session, *, user_id: str) -> int:
    rows = db.query(InboxDelivery).filter(
        InboxDelivery.recipient_user_id == user_id,
        InboxDelivery.delivery_state == "unread",
    ).all()
    now = _now()
    for delivery in rows:
        delivery.delivery_state = "read"
        delivery.read_at = now
        delivery.updated_at = now
    db.commit()
    return len(rows)


def serialize_delivery(delivery: InboxDelivery, item: InboxItem) -> dict:
    return _delivery_dict(delivery, item)
