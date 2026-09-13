"""kernel.v1 outbox/recovery scheduler (APScheduler producer side)."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from sqlalchemy import select

from app.shared.database import SessionLocal

logger = logging.getLogger(__name__)
_scheduler = None
_RECONCILE_CLAIM_TTL = timedelta(seconds=45)


def _enqueue_reconcile_observation(
    run_id: str,
    call_id: str,
    observation: dict,
    claim_owner: str,
) -> bool:
    """Persist a poll result; the NATS reconciler owns state transitions."""
    from .models import ExecutionCall, ExecutionRun
    from .store import enqueue_reconcile_observation

    db = SessionLocal()
    try:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
        call = db.scalar(select(ExecutionCall).where(
            ExecutionCall.id == call_id, ExecutionCall.run_id == run_id,
        ).with_for_update())
        if run is None or call is None:
            db.rollback()
            return False
        try:
            enqueue_reconcile_observation(
                db, run=run, call=call, observation=observation,
                claim_owner=claim_owner,
            )
            db.commit()
            return True
        except Exception:
            db.rollback()
            logger.exception("failed to enqueue reconciliation observation for run=%s call=%s", run_id, call_id)
            return False
    finally:
        db.close()


def _claim_external_call(call_id: str, worker_id: str):
    """Atomically claim one due Call before invoking a remote connector.

    Scheduler instances can run in more than one API process.  The Call lease
    fields provide a small cross-process claim so only one instance performs a
    provider query/cancel for a due observation at a time.
    """
    from .contracts import CallStatus
    from .models import ExecutionCall, ExecutionRun
    from .store import _now, _as_utc

    db = SessionLocal()
    try:
        now = _now()
        call = db.scalar(select(ExecutionCall).where(
            ExecutionCall.id == call_id,
            ExecutionCall.status.in_((
                CallStatus.WAITING_EXTERNAL.value,
                CallStatus.RECONCILING.value,
                CallStatus.CANCEL_REQUESTED.value,
            )),
            (ExecutionCall.next_reconcile_at.is_(None)) | (ExecutionCall.next_reconcile_at <= now),
        ).with_for_update(skip_locked=True))
        if call is None:
            return None
        lease_expires = _as_utc(call.lease_expires_at)
        # Call lease fields are also populated by the activation worker while
        # creating an external Call. Only scheduler-prefixed owners represent
        # an active poll claim; a completed activation's lease must not block
        # the first reconciliation forever.
        if (
            lease_expires is not None
            and lease_expires > now
            and str(call.lease_owner or "").startswith("kernel-reconciler:")
            and call.lease_owner != worker_id
        ):
            return None
        run = db.get(ExecutionRun, call.run_id)
        if run is None or not call.remote_task_ref:
            return None
        call.lease_epoch += 1
        call.lease_owner = worker_id
        call.lease_expires_at = now + _RECONCILE_CLAIM_TTL
        snapshot = (run.id, call.id, call.remote_task_ref)
        db.commit()
        return snapshot
    finally:
        db.close()


def _release_external_call(call_id: str, worker_id: str) -> None:
    """Release a scheduler claim without disturbing a newer claimant."""
    from .models import ExecutionCall

    db = SessionLocal()
    try:
        call = db.scalar(select(ExecutionCall).where(
            ExecutionCall.id == call_id,
            ExecutionCall.lease_owner == worker_id,
        ).with_for_update())
        if call is not None:
            call.lease_owner = None
            call.lease_expires_at = None
        db.commit()
    finally:
        db.close()


def _mark_external_call_manual(call_id: str, worker_id: str, reason: str) -> None:
    """Fence a waiting Call whose immutable capability revision was revoked."""
    from .models import ExecutionCall, ExecutionRun
    from .store import _now, append_event
    from .runtime import _append_manual_attention
    from .contracts import CallOutcome, CallStatus

    db = SessionLocal()
    try:
        call = db.scalar(select(ExecutionCall).where(
            ExecutionCall.id == call_id, ExecutionCall.lease_owner == worker_id,
        ).with_for_update())
        if call is None:
            return
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == call.run_id).with_for_update())
        if run is None:
            db.rollback(); return
        call.status = CallStatus.RECONCILING.value
        call.outcome = CallOutcome.OUTCOME_UNKNOWN.value
        call.manual_attention = True
        call.remote_observed_state_ref = reason
        call.next_reconcile_at = None
        _append_manual_attention(db, run, call, reason)
        append_event(
            db, run, event_type="call.outcome_changed",
            payload={"call_id": call.id, "status": call.status, "outcome": call.outcome,
                     "evidence_ref": None, "connector_id": call.target_ref,
                     "provider_event_id": None},
            actor={"kind": "scheduler"}, command_id=f"call:{call.id}:manual:{reason}",
            idempotency_key=f"call-manual:{call.id}:{reason}", connector_id=call.target_ref,
        )
        db.commit()
    finally:
        db.close()


def _poll_external_calls_once() -> None:
    """Poll provider-backed Calls whose reconciliation deadline has arrived.

    The scheduler only emits an observation; ``reconcile_execution_message``
    remains the single fenced state transition path.  Connector capabilities
    decide whether polling/cancellation is possible, so unsupported legacy
    integrations retain the existing outcome-unknown behavior.
    """
    from .contracts import CallStatus, RunStatus
    from .models import ExecutionCall, ExecutionRun
    from .runtime import _resolve_external_connector
    from .store import _now

    db = SessionLocal()
    try:
        now = _now()
        calls = db.scalars(select(ExecutionCall).where(
            ExecutionCall.status.in_((CallStatus.WAITING_EXTERNAL.value, CallStatus.RECONCILING.value, CallStatus.CANCEL_REQUESTED.value)),
            (ExecutionCall.next_reconcile_at.is_(None)) | (ExecutionCall.next_reconcile_at <= now),
        ).order_by(ExecutionCall.next_reconcile_at, ExecutionCall.id).limit(50)).all()
        worker_id = f"kernel-reconciler:{uuid.uuid4().hex}"
        for candidate in calls:
            claimed = _claim_external_call(candidate.id, worker_id)
            if claimed is None:
                continue
            claimed_run_id, claimed_call_id, remote_ref = claimed
            call = db.get(ExecutionCall, claimed_call_id)
            run = db.get(ExecutionRun, claimed_run_id)
            # A cancelled/expired Run can still own an unresolved remote Call.
            # Keep polling those Calls after local termination so a provider
            # cancellation can be confirmed and no remote side effect is
            # abandoned merely because the parent Run reached its terminal
            # projection.
            if run is None or not call.remote_task_ref:
                _release_external_call(claimed_call_id, worker_id)
                continue
            connector = _resolve_external_connector(db, run, call)
            if connector is None:
                db.rollback()
                # A revoked/mismatched immutable revision is terminal for
                # this Call. Other resolution failures (for example a
                # temporarily unavailable connector) remain retryable.
                stale = False
                try:
                    from .runtime import _capability_revision_for_target
                    from .models import CapabilityRevision
                    stale = _capability_revision_for_target(db, run.owner_id, call.target_ref) != int(call.capability_revision)
                    if not stale and call.target_ref:
                        snapshot = db.scalar(select(CapabilityRevision).where(
                            CapabilityRevision.key == str(call.target_ref),
                            CapabilityRevision.revision == int(call.capability_revision),
                        ))
                        stale = snapshot is not None and not snapshot.enabled
                except Exception:
                    stale = False
                if stale:
                    _mark_external_call_manual(claimed_call_id, worker_id, "capability_revision_unavailable")
                else:
                    _release_external_call(claimed_call_id, worker_id)
                continue
            descriptor = connector.descriptor()
            cancelling = (
                call.status == CallStatus.CANCEL_REQUESTED.value
                or run.status in {RunStatus.CANCEL_REQUESTED.value, RunStatus.CANCELLING.value}
            )
            if cancelling and not descriptor.supports_cancel:
                db.rollback()
                _release_external_call(claimed_call_id, worker_id)
                continue
            if not cancelling and not descriptor.supports_query_status:
                db.rollback()
                _release_external_call(claimed_call_id, worker_id)
                continue
            operation = connector.cancel if cancelling else connector.query_status
            db.rollback()
            try:
                observed = asyncio.run(operation(remote_task_ref=remote_ref))
            except Exception:
                logger.exception("external call observation failed for run=%s call=%s", run.id, call.id)
                _release_external_call(claimed_call_id, worker_id)
                continue
            if not isinstance(observed, dict):
                _release_external_call(claimed_call_id, worker_id)
                continue
            payload = {
                "run_id": run.id,
                "call_id": call.id,
                "connector_id": descriptor.agent_id,
                "remote_state": observed.get("status") or observed.get("provider_status"),
                "provider_event_id": observed.get("provider_event_id"),
                "evidence_ref": observed.get("evidence_ref"),
                "content": observed.get("content"),
                "artifacts": observed.get("artifacts") or [],
            }
            _enqueue_reconcile_observation(
                claimed_run_id, claimed_call_id, payload, worker_id,
            )
            _release_external_call(claimed_call_id, worker_id)
    except Exception:
        logger.exception("kernel external reconciliation poll failed")
    finally:
        db.close()


def _drain() -> None:
    from .outbox import publish_due_once, recover_expired_claims
    from .recovery import expire_due_runs_once, expire_inbox_once, join_ready_parents_once, recover_stuck_runs_once

    db = SessionLocal()
    try:
        recover_expired_claims(db)
        expire_due_runs_once(db)
        expire_inbox_once(db)
        recover_stuck_runs_once(db)
        join_ready_parents_once(db)
        publish_due_once(db, batch_size=20)
    except Exception:
        logger.exception("kernel execution outbox drain failed")
    finally:
        db.close()


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(_drain, "interval", seconds=5, id="sa-kernel-outbox", max_instances=1, coalesce=True)
    _scheduler.add_job(_poll_external_calls_once, "interval", seconds=5, id="sa-kernel-external-reconcile", max_instances=1, coalesce=True)
    _scheduler.start()


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
