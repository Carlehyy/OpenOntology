"""Durable recovery scans for kernel.v1.

The scheduler is intentionally a producer: it only records fenced state
transitions and enqueues an outbox wake-up.  Actual model/connector work stays
in the NATS executor.
"""
from __future__ import annotations

from datetime import timedelta, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import RunStatus
from .models import Approval, Artifact, ExecutionAttempt, ExecutionCall, ExecutionEvent, ExecutionRun, ExecutionStep, ExecutionTurn, InboxItem
from .policies import ChildResult, ChildStatus, ExecutionPolicy, JoinDecision, JoinPolicy, decide_child_join
from .reconciler import decide_run_timeout, should_recover_run
from .store import _add_outbox, _now, acquire_lease, append_event


_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value, RunStatus.EXPIRED.value}
_UNRESOLVED = {"offered", "dispatched", "running", "waiting_external", "cancel_requested", "reconciling"}


def _child_status(value: str) -> ChildStatus:
    """Map a Run projection to the smaller fan-in status vocabulary.

    ChildStatus intentionally models join semantics, while RunStatus also
    contains execution-control and waiting states.  Unknown/non-terminal
    values must remain conservative instead of aborting the scheduler tick.
    """
    if value == RunStatus.QUEUED.value:
        return ChildStatus.PENDING
    if value in _TERMINAL:
        return ChildStatus(value)
    return ChildStatus.RUNNING


def _utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def expire_inbox_once(db: Session, *, limit: int = 100) -> int:
    """Expire unanswered questions/approvals and apply their frozen policy.

    A question may be re-asked once with a fresh inbox row.  The second expiry
    and every approval expiry fail the owning Run; no expired item is consumed
    by a later worker.  All transitions are append-only kernel facts.
    """
    now = _now()
    rows = db.scalars(select(InboxItem).where(
        InboxItem.status.in_(("pending", "claimed")),
        InboxItem.expires_at.is_not(None),
        InboxItem.expires_at <= now,
    ).order_by(InboxItem.expires_at, InboxItem.id).limit(limit).with_for_update(skip_locked=True)).all()
    changed = 0
    for item in rows:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == item.run_id).with_for_update())
        if run is None or item.status not in {"pending", "claimed"}:
            continue
        item.status = "expired"
        item.consumed_at = now
        item.claim_token = None
        item.claim_expires_at = None
        append_event(
            db, run, event_type="inbox.expired",
            payload={
                "inbox_id": item.id, "kind": item.kind,
                "target_ref": item.target_ref or item.id,
                "question_id": item.question_id,
                "question_expires_at": item.expires_at.isoformat() if item.expires_at else now.isoformat(),
                "expiry_policy": item.expiry_policy or "fail_run",
                "accepted_at": item.accepted_at.isoformat() if item.accepted_at else now.isoformat(),
            }, actor={"kind": "system"}, command_id=f"inbox-expired:{item.id}",
            idempotency_key=f"inbox-expired:{item.id}",
        )
        if item.approval_id:
            approval = db.scalar(select(Approval).where(Approval.id == item.approval_id).with_for_update())
            if approval is not None and approval.status == "pending":
                approval.status = "expired"
                append_event(
                    db, run, event_type="approval.expired",
                    payload={"approval_id": approval.id, "reason": "ttl", "actor": "system", "occurred_at": now.isoformat()},
                    actor={"kind": "system"}, command_id=f"approval-expired:{approval.id}",
                    idempotency_key=f"approval-expired:{approval.id}",
                )
        if run.status in _TERMINAL:
            changed += 1
            continue
        if item.kind == "question_answer" and item.expiry_policy == "reask_once" and not (item.payload or {}).get("_reasked"):
            # Preserve the original question while marking the retry in the
            # payload, so a second expiration deterministically fails the Run.
            deadline = _utc(run.deadline)
            retry = InboxItem(
                run_id=run.id, kind=item.kind, priority=item.priority, status="pending",
                question_id=item.question_id, target_ref=item.target_ref,
                payload={**(item.payload or {}), "_reasked": True}, source="system",
                expires_at=min(deadline, now + timedelta(minutes=30)) if deadline else now + timedelta(minutes=30),
                expiry_policy="fail_run", accepted_at=now,
                idempotency_key=f"{item.id}:reask",
            )
            db.add(retry); db.flush()
            append_event(
                db, run, event_type="inbox.appended",
                payload={"inbox_id": retry.id, "kind": retry.kind, "target_ref": retry.target_ref or retry.id, "expiry_policy": retry.expiry_policy},
                actor={"kind": "system"}, command_id=f"inbox-reask:{item.id}", idempotency_key=f"inbox-reask:{item.id}",
            )
        else:
            before = run.status
            run.status = RunStatus.FAILED.value
            run.wait_reason = "input_expired"
            run.version += 1
            append_event(
                db, run, event_type="run.status_changed",
                payload={"from": before, "to": run.status, "reason": "inbox_expired", "actor": "system", "version": run.version},
                actor={"kind": "system"}, command_id=f"inbox-fail:{item.id}", idempotency_key=f"inbox-fail:{item.id}",
            )
        changed += 1
    db.commit()
    return changed


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
        _fence_orphaned_calls(db, run, token)
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


def _fence_orphaned_calls(db: Session, run: ExecutionRun, token) -> None:
    """Close only calls left by a fenced worker, preserving remote waits."""
    rows = db.scalars(select(ExecutionCall).where(
        ExecutionCall.run_id == run.id,
        ExecutionCall.status.in_(("offered", "dispatched", "running")),
        ExecutionCall.lease_epoch != token.epoch,
    ).with_for_update()).all()
    for call in rows:
        call.status = "reconciling"
        call.outcome = "outcome_unknown"
        call.manual_attention = True
        call.remote_observed_state_ref = "worker_fenced"
        attempt = db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.call_id == call.id).order_by(ExecutionAttempt.attempt_no.desc()).with_for_update())
        if attempt is not None and attempt.finished_at is None:
            attempt.provider_status, attempt.finished_at, attempt.safe_to_retry = "unknown", _now(), False
            append_event(db, run, event_type="attempt.result", payload={"attempt_id": attempt.id, "provider_status": "unknown", "result_ref": None, "error_ref": "worker_fenced", "safe_to_retry": False, "token_usage_ref": None, "cost_ref": None}, actor={"kind": "system"}, command_id=f"recovery:{attempt.id}:fenced", idempotency_key=f"recovery-attempt-fenced:{attempt.id}", lease=token)
        if call.step_id:
            step = db.get(ExecutionStep, call.step_id)
            if step is not None and step.status != "closed":
                step.status, step.close_reason, step.closed_at = "closed", "interrupted", _now()
            if step is not None:
                turn = db.get(ExecutionTurn, step.turn_id)
                if turn is not None and turn.status == "open":
                    turn.status, turn.close_reason, turn.closed_at = "closed", "interrupted", _now()
        append_event(db, run, event_type="call.outcome_changed", payload={"call_id": call.id, "status": call.status, "outcome": call.outcome, "evidence_ref": None, "connector_id": call.target_ref, "provider_event_id": None}, actor={"kind": "system"}, command_id=f"recovery:{call.id}:fenced", idempotency_key=f"recovery-call-fenced:{call.id}", lease=token, connector_id=call.target_ref)


def join_ready_parents_once(db: Session, *, limit: int = 100) -> int:
    """Apply child fan-in decisions to parents without executing child work."""
    rows = db.scalars(select(ExecutionRun).where(ExecutionRun.status.not_in(_TERMINAL)).limit(limit).with_for_update(skip_locked=True)).all()
    parents = [r for r in rows if r.required_child_ids]
    changed = 0
    for parent in parents:
        if parent.status in {RunStatus.CANCEL_REQUESTED.value, RunStatus.CANCELLING.value}:
            # Cancellation is the parent's control decision; child fan-in
            # must never overwrite it with FAILED.
            continue
        children = db.scalars(select(ExecutionRun).where(ExecutionRun.parent_run_id == parent.id)).all()
        if not children:
            continue
        policy = JoinPolicy(parent.join_policy)
        result = decide_child_join(tuple(ChildResult(c.id, _child_status(c.status), c.id in set(parent.required_child_ids or [])) for c in children), policy)
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
            child = next((candidate for candidate in children if candidate.id == child_id), None)
            if child is not None:
                _merge_child_result(db, parent, child)
            append_event(db, parent, event_type="run.child_joined", payload={"parent_run_id": parent.id, "child_run_id": child_id, "join_policy": parent.join_policy, "required": required}, actor={"kind": "system"}, command_id=f"join:{parent.id}:{parent.version}:{child_id}", idempotency_key=f"join:{parent.id}:{parent.version}:{child_id}")
        append_event(db, parent, event_type="run.status_changed", payload={"from": before, "to": parent.status, "reason": "child_join", "actor": "system", "version": parent.version}, actor={"kind": "system"}, command_id=f"join-status:{parent.id}:{parent.version}", idempotency_key=f"join-status:{parent.id}:{parent.version}")
        _add_outbox(db, parent, command_id=f"join-dispatch:{parent.id}:{parent.version}", message_ref=f"run://{parent.id}")
        changed += 1
    db.commit()
    return changed


def _merge_child_result(db: Session, parent: ExecutionRun, child: ExecutionRun) -> None:
    """Materialize a bounded child-result manifest in the parent context.

    Parent activation must be able to reason over a completed child without
    opening the child Run's private event stream.  The manifest references all
    child Artifacts and embeds only small inline payloads; object-backed
    content remains addressable through its artifact URI and ownership checks.
    The provenance marker makes repeated recovery scans idempotent.
    """
    provenance = f"child-run:{child.id}:v{child.version}"
    if db.scalar(select(Artifact).where(Artifact.run_id == parent.id, Artifact.provenance_ref == provenance)) is not None:
        return
    artifacts = db.scalars(select(Artifact).where(Artifact.run_id == child.id).order_by(Artifact.id)).all()
    entries = []
    for artifact in artifacts:
        entry = {
            "artifact_id": artifact.id,
            "kind": artifact.kind,
            "mime_type": artifact.mime_type,
            "size": artifact.size,
            "checksum": artifact.checksum,
            "storage_ref": artifact.storage_ref,
            "business_status": artifact.business_status,
        }
        if artifact.inline_content is not None and len(artifact.inline_content.encode("utf-8")) <= 64 * 1024:
            entry["inline_content"] = artifact.inline_content
        entries.append(entry)
    content = json.dumps({"child_run_id": child.id, "status": child.status, "artifacts": entries}, ensure_ascii=False, sort_keys=True)
    artifact = Artifact(
        owner_id=parent.owner_id, run_id=parent.id, kind="child.result",
        mime_type="application/json", size=len(content.encode("utf-8")),
        checksum="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        storage_ref=f"inline://{parent.id}/{provenance}", inline_content=content,
        status="complete", integrity_status="verified", business_status="success" if child.status == RunStatus.COMPLETED.value else "failed",
        visibility="owner", provenance_ref=provenance,
    )
    db.add(artifact)
    db.flush()
    append_event(
        db, parent, event_type="artifact.declared",
        payload={"artifact_id": artifact.id, "kind": artifact.kind, "mime_type": artifact.mime_type,
                 "size": artifact.size, "checksum": artifact.checksum, "storage_ref": artifact.storage_ref,
                 "visibility": artifact.visibility},
        actor={"kind": "system"}, command_id=f"child-artifact:{artifact.id}:declare",
        idempotency_key=f"child-artifact:{provenance}:declare",
    )
    append_event(
        db, parent, event_type="artifact.completed",
        payload={"artifact_id": artifact.id, "checksum": artifact.checksum,
                 "integrity_status": artifact.integrity_status, "business_status": artifact.business_status},
        actor={"kind": "system"}, command_id=f"child-artifact:{artifact.id}:complete",
        idempotency_key=f"child-artifact:{provenance}:complete",
    )
    append_event(
        db, parent, event_type="assistant.message",
        payload={"attempt_id": artifact.id, "message_ref": f"artifact://{artifact.id}"},
        actor={"kind": "system"}, command_id=f"child-artifact:{artifact.id}:message",
        idempotency_key=f"child-artifact:{provenance}:message",
    )
