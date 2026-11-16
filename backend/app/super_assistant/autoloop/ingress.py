"""Transactional admission of notification and scheduled-run facts.

The caller owns commit/rollback. Ownership comes from persistent platform rows;
external text, reported source names and credentials never choose an owner.
"""
from __future__ import annotations

from datetime import timezone

from sqlalchemy.orm import Session

from app.auth.models import User
from app.notifications.models import NotificationIngestKey, NotificationMessage
from app.super_assistant.autoloop.store import accept_receipt
from app.super_assistant.models import SuperAssistantScheduledRun, SuperAssistantScheduledTask


def _active_owner(db: Session, identity: str | None) -> str | None:
    owner = db.get(User, identity, populate_existing=True) if identity else None
    return owner.id if owner is not None and owner.is_active else None


def accept_notification(db: Session, message: NotificationMessage, *, thread_id: str | None = None):
    """Admit only externally ingested messages, using their original key facts.

    A notification replay refers to the existing message and its original key,
    even when another valid key delivered the replay. Legacy messages without a
    receipt can be repaired in the same transaction without re-fanning deliveries.
    """
    if message.source_type != "ingest":
        return None
    key = (db.get(NotificationIngestKey, message.ingest_key_id, populate_existing=True)
           if message.ingest_key_id else None)
    owner_id = _active_owner(db, key.created_by if key else None)
    return accept_receipt(
        db, source_kind="notification", source_system=message.source_system,
        source_event_id=(f"event:{message.event_id}" if message.event_id else f"message:{message.id}"),
        owner_id=owner_id, ingest_key_id=message.ingest_key_id,
        payload={
            "notificationId": message.id, "eventId": message.event_id,
            "title": message.title, "body": message.body_md, "priority": message.priority,
            "sourceSystem": message.source_system, "ingestKeyId": message.ingest_key_id,
            "allowedSourceSystem": key.allowed_source_system if key else None,
            "createdBy": key.created_by if key else None,
            **({"threadId": thread_id} if thread_id else {}),
        },
    )


def accept_scheduled_run(db: Session, task: SuperAssistantScheduledTask, run: SuperAssistantScheduledRun):
    instant = run.scheduled_for
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return accept_receipt(
        db, source_kind="schedule", source_system="openontology",
        source_event_id=run.id, owner_id=_active_owner(db, run.owner_id),
        scheduled_task_id=task.id, scheduled_run_id=run.id,
        payload={
            "scheduledTaskId": task.id, "scheduledRunId": run.id,
            "title": task.title, "instruction": task.instruction,
            "body": task.instruction, "scheduledFor": instant.isoformat(),
            "timezone": task.timezone, "targetKind": task.target_kind,
            "targetTemplateId": task.target_template_id,
        },
    )
