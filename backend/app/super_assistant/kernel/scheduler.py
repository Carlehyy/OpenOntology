"""kernel.v1 outbox/recovery scheduler (APScheduler producer side)."""
from __future__ import annotations

import asyncio
import logging
from sqlalchemy import select

from app.shared.database import SessionLocal

logger = logging.getLogger(__name__)
_scheduler = None


def _poll_external_calls_once() -> None:
    """Poll provider-backed Calls whose reconciliation deadline has arrived.

    The scheduler only emits an observation; ``reconcile_execution_message``
    remains the single fenced state transition path.  Connector capabilities
    decide whether polling/cancellation is possible, so unsupported legacy
    integrations retain the existing outcome-unknown behavior.
    """
    from .contracts import CallStatus, RunStatus
    from .models import ExecutionCall, ExecutionRun
    from .runtime import _resolve_external_connector, reconcile_execution_message
    from .store import _now

    terminal = {
        RunStatus.COMPLETED.value, RunStatus.FAILED.value,
        RunStatus.CANCELLED.value, RunStatus.EXPIRED.value,
    }
    db = SessionLocal()
    try:
        now = _now()
        calls = db.scalars(select(ExecutionCall).where(
            ExecutionCall.status.in_((CallStatus.WAITING_EXTERNAL.value, CallStatus.RECONCILING.value)),
            (ExecutionCall.next_reconcile_at.is_(None)) | (ExecutionCall.next_reconcile_at <= now),
        ).order_by(ExecutionCall.next_reconcile_at, ExecutionCall.id).limit(50)).all()
        for call in calls:
            run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == call.run_id))
            if run is None or run.status in terminal or not call.remote_task_ref:
                continue
            connector = _resolve_external_connector(db, run, call)
            if connector is None:
                db.rollback()
                continue
            descriptor = connector.descriptor()
            cancelling = run.status in {RunStatus.CANCEL_REQUESTED.value, RunStatus.CANCELLING.value}
            if cancelling and not descriptor.supports_cancel:
                db.rollback()
                continue
            if not cancelling and not descriptor.supports_query_status:
                db.rollback()
                continue
            operation = connector.cancel if cancelling else connector.query_status
            remote_ref = call.remote_task_ref
            db.rollback()
            try:
                observed = asyncio.run(operation(remote_task_ref=remote_ref))
            except Exception:
                logger.exception("external call observation failed for run=%s call=%s", run.id, call.id)
                continue
            if not isinstance(observed, dict):
                continue
            payload = {
                "run_id": run.id,
                "call_id": call.id,
                "connector_id": descriptor.agent_id,
                "remote_state": observed.get("status") or observed.get("provider_status"),
                "provider_event_id": observed.get("provider_event_id"),
                "evidence_ref": observed.get("evidence_ref"),
                "content": observed.get("content"),
            }
            asyncio.run(reconcile_execution_message(payload))
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
