"""kernel.v1 dispatch outbox publisher。

发布失败只推进 durable retry 状态，不在 API 或 executor 进程内联执行 Run。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_channel.pipeline_tasks.dispatch import EXECUTION_DLQ_SUBJECT, dispatch_execution
from .models import ExecutionDispatchOutbox, ExecutionRun
from .store import _now

MAX_ATTEMPTS = 10
CLAIM_TTL = timedelta(seconds=60)


def publish_due_once(db: Session, *, batch_size: int = 20, publisher_id: str | None = None) -> int:
    """Claim immediately before each bounded publish, never a waiting batch.

    Broker dispatch has a 15-second deadline, below CLAIM_TTL. Claiming all
    twenty rows up front allowed later rows to expire while earlier broker
    calls were still running. A unique token also fences stale acknowledgments
    when the same publisher identity is reused after a crash.
    """
    publisher_id = publisher_id or f"outbox:{uuid.uuid4().hex[:12]}"
    published = 0
    for _ in range(batch_size):
        now = _now()
        row = db.scalar(
            select(ExecutionDispatchOutbox)
            .where(
                ExecutionDispatchOutbox.status == "pending",
                ExecutionDispatchOutbox.next_attempt_at <= now,
            )
            .order_by(ExecutionDispatchOutbox.next_attempt_at, ExecutionDispatchOutbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if row is None:
            db.rollback()
            break
        token = f"{publisher_id}:{uuid.uuid4().hex}"
        row.status = "claimed"
        row.claim_token = token
        row.claim_expires_at = now + CLAIM_TTL
        row_id = row.id
        subject, command_id, message_ref, run_id = row.subject, row.command_id, row.message_ref, row.run_id
        payload = dict(row.payload or {})
        db.commit()
        try:
            # Rows written before the payload column was introduced carry an
            # empty JSON object. Preserve their call dispatch contract while
            # allowing newer reconcile rows to carry an immutable body.
            if message_ref.startswith("call://") and "call_id" not in payload:
                payload["call_id"] = message_ref.removeprefix("call://")
            payload.update({
                "run_id": run_id,
                "command_id": command_id,
                "message_ref": message_ref,
            })
            dispatch_execution(
                subject,
                payload,
                command_id=command_id,
            )
        except Exception as exc:  # durable row remains for a later scheduler tick
            _record_failure(db, row_id, token, str(exc))
        else:
            current = _lock_claim(db, row_id, token)
            if current is None:
                db.rollback()
                continue
            current.status = "published"
            current.published_at = _now()
            current.claim_token = None
            current.claim_expires_at = None
            db.commit()
            published += 1
    return published


def recover_expired_claims(db: Session) -> int:
    """将发布器崩溃留下的 claimed 行恢复为 pending。"""
    now = _now()
    rows = db.scalars(select(ExecutionDispatchOutbox).where(
        ExecutionDispatchOutbox.status == "claimed",
        ExecutionDispatchOutbox.claim_expires_at < now,
    ).with_for_update(skip_locked=True).execution_options(populate_existing=True)).all()
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


def _lock_claim(db: Session, row_id: str, claim_token: str, *, require_claimed: bool = True):
    statement = select(ExecutionDispatchOutbox).where(
        ExecutionDispatchOutbox.id == row_id,
        ExecutionDispatchOutbox.claim_token == claim_token,
    )
    if require_claimed:
        statement = statement.where(ExecutionDispatchOutbox.status == "claimed")
    return db.scalar(statement.with_for_update().execution_options(populate_existing=True))


def _record_failure(db: Session, row_id: str, claim_token: str, error: str) -> None:
    row = _lock_claim(db, row_id, claim_token, require_claimed=False)
    if row is None:
        db.rollback()
        return
    row.attempt_count += 1
    row.error_ref = error[:1000]
    row.claim_token = None
    row.claim_expires_at = None
    exhausted = row.attempt_count >= MAX_ATTEMPTS
    if exhausted:
        row.status = "dead"
    else:
        row.status = "pending"
        row.next_attempt_at = _now() + timedelta(seconds=min(300, 5 * (2 ** (row.attempt_count - 1))))
    dlq_payload = {
        "schema": "sa.execution.dlq.v1",
        "run_id": row.run_id,
        "outbox_id": row.id,
        "command_id": row.command_id,
        "message_ref": row.message_ref,
        "subject": row.subject,
        "attempt_count": row.attempt_count,
        "error_ref": row.error_ref,
        "replay_requires_operator": True,
    }
    db.commit()
    if exhausted:
        try:
            dispatch_execution(EXECUTION_DLQ_SUBJECT, dlq_payload, command_id=f"dlq:{row.command_id}:{row.attempt_count}")
        except Exception:
            pass
