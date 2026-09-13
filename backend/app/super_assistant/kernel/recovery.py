"""Durable recovery scans for kernel.v1.

The scheduler is intentionally a producer: it only records fenced state
transitions and enqueues an outbox wake-up.  Actual model/connector work stays
in the NATS executor.
"""
from __future__ import annotations

from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import RunStatus
from .models import ExecutionCall, ExecutionRun
from .policies import ChildResult, ChildStatus, ExecutionPolicy, JoinDecision, JoinPolicy, decide_child_join
from .reconciler import decide_run_timeout, should_recover_run
from .store import _add_outbox, _now, acquire_lease, append_event


_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value, RunStatus.EXPIRED.value}
_UNRESOLVED = {"offered", "dispatched", "running", "waiting_external", "cancel_requested", "reconciling"}


def _utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def expire_due_runs_once(db: Session, *, policy: ExecutionPolicy | None = None, limit: int = 100) -> int:
    """Request deadline cancellation, then close runs after cancel grace."""
    policy = policy or ExecutionPolicy()
    now = _now()
    rows = db.scalars(select(ExecutionRun).where(ExecutionRun.status.not_in(_TERMINAL)).order_by(ExecutionRun.updated_at).limit(limit).with_for_update(skip_locked=True)).all()
    changed = 0
    for run in rows:
        unresolved_ids = list(db.scalars(select(ExecutionCall.id).where(ExecutionCall.run_id == run.id, ExecutionCall.status.in_(_UNRESOLVED))).all())
        unresolved_count = len(unresolved_ids)
        decision = decide_run_timeout(
            status=RunStatus(run.status), unresolved_call_count=unresolved_count,
            deadline=_utc(run.deadline), cancel_deadline=_utc(run.cancel_deadline),
            cancel_reason=run.cancel_reason, now=now,
        )
        if decision.action == "noop":
            continue
        before = run.status
        run.status = decision.status.value
        run.cancel_reason = decision.cancel_reason
        run.version += 1
        if decision.action == "expiry_requested":
            run.cancel_deadline = now + policy.cancel_grace
            append_event(
                db, run, event_type="run.expiry_requested",
                payload={"reason": "deadline", "deadline": run.deadline.isoformat() if run.deadline else now.isoformat(), "unresolved_call_ids": [str(call_id) for call_id in unresolved_ids]},
                actor={"kind": "system"}, command_id=f"expiry:{run.id}:{run.version}", idempotency_key=f"expiry:{run.id}:{run.version}",
            )
        elif decision.action == "cancel_timeout":
            append_event(
                db, run, event_type="run.cancel_timeout",
                payload={"reason": run.cancel_reason or "deadline", "cancel_deadline": run.cancel_deadline.isoformat() if run.cancel_deadline else now.isoformat(), "unresolved_call_ids": [str(call_id) for call_id in unresolved_ids], "run_terminal_status": run.status},
                actor={"kind": "system"}, command_id=f"cancel-timeout:{run.id}:{run.version}", idempotency_key=f"cancel-timeout:{run.id}:{run.version}",
            )
        append_event(
            db, run, event_type="run.status_changed",
            payload={"from": before, "to": run.status, "reason": decision.action, "actor": "system", "version": run.version},
            actor={"kind": "system"}, command_id=f"timeout-status:{run.id}:{run.version}", idempotency_key=f"timeout-status:{run.id}:{run.version}",
        )
        if decision.action == "expiry_requested":
            _add_outbox(db, run, command_id=f"expiry-dispatch:{run.id}:{run.version}", message_ref=f"run://{run.id}")
        changed += 1
    db.commit()
    return changed


def recover_stuck_runs_once(db: Session, *, policy: ExecutionPolicy | None = None, limit: int = 100) -> int:
    """Fence and wake runs whose worker lease and progress both went stale."""
    policy = policy or ExecutionPolicy()
    now = _now()
    rows = db.scalars(select(ExecutionRun).where(ExecutionRun.status.not_in(_TERMINAL)).order_by(ExecutionRun.updated_at).limit(limit).with_for_update(skip_locked=True)).all()
    changed = 0
    for run in rows:
        if _utc(run.lease_expires_at) and _utc(run.lease_expires_at) > now:
            continue
        updated_at = run.updated_at if run.updated_at.tzinfo else run.updated_at.replace(tzinfo=timezone.utc)
        if not should_recover_run(status=RunStatus(run.status), updated_at=updated_at, now=now, policy=policy):
            continue
        token = acquire_lease(db, run_id=run.id, worker_id="kernel:recovery", ttl=policy.lease_ttl)
        run.version += 1
        append_event(
            db, run, event_type="run.recovery_requested",
            payload={"reason": "stuck_detector", "lease_epoch": token.epoch, "diagnostic_ref": f"run://{run.id}/recovery"},
            actor={"kind": "system"}, command_id=f"recovery:{run.id}:{token.epoch}", idempotency_key=f"recovery:{run.id}:{token.epoch}", lease=token,
        )
        _add_outbox(db, run, command_id=f"recovery-dispatch:{run.id}:{token.epoch}", message_ref=f"run://{run.id}")
        changed += 1
    db.commit()
    return changed


def join_ready_parents_once(db: Session, *, limit: int = 100) -> int:
    """Apply child fan-in decisions to parents without executing child work."""
    rows = db.scalars(select(ExecutionRun).where(ExecutionRun.status.not_in(_TERMINAL)).limit(limit).with_for_update(skip_locked=True)).all()
    parents = [r for r in rows if r.required_child_ids]
    changed = 0
    for parent in parents:
        children = db.scalars(select(ExecutionRun).where(ExecutionRun.parent_run_id == parent.id)).all()
        if not children:
            continue
        policy = JoinPolicy(parent.join_policy)
        result = decide_child_join(tuple(ChildResult(c.id, ChildStatus(c.status), c.id in set(parent.required_child_ids or [])) for c in children), policy)
        if result.decision is JoinDecision.PENDING:
            continue
        before = parent.status
        if result.decision is JoinDecision.FAILED:
            parent.status = RunStatus.FAILED.value
            parent.version += 1
        elif parent.status in {RunStatus.WAITING_EXTERNAL.value, RunStatus.WAITING_RETRY.value}:
            parent.status = RunStatus.ACTIVE.value
            parent.version += 1
        else:
            continue
        joined_ids = list(dict.fromkeys(result.failed_child_ids + result.successful_child_ids))
        required_ids = set(parent.required_child_ids or [])
        for child_id in joined_ids:
            required = child_id in required_ids
            append_event(db, parent, event_type="run.child_joined", payload={"parent_run_id": parent.id, "child_run_id": child_id, "join_policy": parent.join_policy, "required": required}, actor={"kind": "system"}, command_id=f"join:{parent.id}:{parent.version}:{child_id}", idempotency_key=f"join:{parent.id}:{parent.version}:{child_id}")
        append_event(db, parent, event_type="run.status_changed", payload={"from": before, "to": parent.status, "reason": "child_join", "actor": "system", "version": parent.version}, actor={"kind": "system"}, command_id=f"join-status:{parent.id}:{parent.version}", idempotency_key=f"join-status:{parent.id}:{parent.version}")
        _add_outbox(db, parent, command_id=f"join-dispatch:{parent.id}:{parent.version}", message_ref=f"run://{parent.id}")
        changed += 1
    db.commit()
    return changed
