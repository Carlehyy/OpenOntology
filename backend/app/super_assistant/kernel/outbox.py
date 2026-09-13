"""kernel.v1 dispatch outbox publisher。

发布失败只推进 durable retry 状态，不在 API 或 executor 进程内联执行 Run。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_channel.pipeline_tasks.dispatch import dispatch_execution
from .models import ExecutionDispatchOutbox, ExecutionRun
from .store import _now

MAX_ATTEMPTS = 10
CLAIM_TTL = timedelta(seconds=60)


def publish_due_once(db: Session, *, batch_size: int = 20, publisher_id: str | None = None) -> int:
    """发布一批到期 Outbox；返回本次完成 published 的数量。"""
    publisher_id = publisher_id or f"outbox:{uuid.uuid4().hex[:12]}"
    now = _now()
    rows = db.scalars(
        select(ExecutionDispatchOutbox)
        .where(
            ExecutionDispatchOutbox.status == "pending",
            ExecutionDispatchOutbox.next_attempt_at <= now,
        )
        .order_by(ExecutionDispatchOutbox.next_attempt_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        row.status = "claimed"
        row.claim_token = publisher_id
        row.claim_expires_at = now + CLAIM_TTL
    db.commit()
    published = 0
    for row in rows:
        try:
            dispatch_execution(
                row.subject,
                {"run_id": row.run_id, "command_id": row.command_id, "message_ref": row.message_ref},
                command_id=row.command_id,
            )
        except Exception as exc:  # durable row remains for a later scheduler tick
            _record_failure(db, row.id, publisher_id, str(exc))
        else:
            current = db.get(ExecutionDispatchOutbox, row.id)
            if current is None or current.claim_token != publisher_id:
                continue
            current.status = "published"
            current.published_at = _now()
            db.commit()
            published += 1
    return published


def recover_expired_claims(db: Session) -> int:
    """将发布器崩溃留下的 claimed 行恢复为 pending。"""
    now = _now()
    rows = db.scalars(select(ExecutionDispatchOutbox).where(
        ExecutionDispatchOutbox.status == "claimed",
        ExecutionDispatchOutbox.claim_expires_at < now,
    )).all()
    for row in rows:
        row.status = "pending"
        row.claim_token = None
        row.claim_expires_at = None
    db.commit()
    return len(rows)


def replay_dead_once(db: Session, *, outbox_id: str, owner_id: str) -> bool:
    """Explicitly requeue one dead dispatch after an operator decision.

    Replay is never implicit: the row remains dead until a caller with access
    to the owning Run requests it, preserving an auditable boundary around
    potentially duplicated external work.
    """
    row = db.get(ExecutionDispatchOutbox, outbox_id)
    if row is None:
        raise KeyError(outbox_id)
    run = db.get(ExecutionRun, row.run_id)
    if run is None or run.owner_id != owner_id:
        raise KeyError(outbox_id)
    if row.status != "dead":
        return False
    row.status = "pending"
    row.next_attempt_at = _now()
    row.claim_token = None
    row.claim_expires_at = None
    row.error_ref = None
    db.commit()
    return True


def _record_failure(db: Session, row_id: str, claim_token: str, error: str) -> None:
    row = db.get(ExecutionDispatchOutbox, row_id)
    if row is None or row.claim_token != claim_token:
        return
    row.attempt_count += 1
    row.error_ref = error[:1000]
    row.claim_token = None
    row.claim_expires_at = None
    if row.attempt_count >= MAX_ATTEMPTS:
        row.status = "dead"
    else:
        row.status = "pending"
        row.next_attempt_at = _now() + timedelta(seconds=min(300, 5 * (2 ** (row.attempt_count - 1))))
    db.commit()
