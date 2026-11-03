"""Lease reconciliation and event-ledger maintenance for agent runs."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.run_common import (
    DispatchRun,
    dispatch_after_commit,
    now as utc_now,
)
from app.ontologies.agent_runtime.run_events import append_event
from app.ontologies.agent_runtime.run_models import AgentRun, AgentRunEvent

logger = logging.getLogger(__name__)


def _interrupt_expired(
    session: Session,
    run_id: str,
    current: datetime,
) -> bool:
    message = "回合执行租约已过期，已标记为中断（不会自动重跑）。"
    updated = (
        session.query(AgentRun)
        .filter(
            AgentRun.id == run_id,
            AgentRun.status == "running",
            AgentRun.lease_expires_at.is_not(None),
            AgentRun.lease_expires_at < current,
        )
        .update(
            {
                "status": "interrupted",
                "finished_at": current,
                "updated_at": current,
                "lease_owner": None,
                "lease_expires_at": None,
                "error": message,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        session.rollback()
        return False
    if append_event(
        session, run_id, "error",
        {"message": message, "code": "interrupted"},
        require_status="interrupted",
    ) is None:
        session.rollback()
        return False
    if append_event(
        session, run_id, "done", {"status": "interrupted"},
        require_status="interrupted",
    ) is None:
        session.rollback()
        return False
    session.commit()
    return True


def cleanup_expired_delta_events(
    session: Session,
    *,
    now: datetime | None = None,
) -> int:
    """Delete token deltas past the retention window.

    The run row keeps the accumulated content/steps, so reconnecting clients
    restore from the snapshot; answer/step/meta terminal events are retained
    for audit and replay.
    """
    current = now or utc_now()
    cutoff = current - timedelta(hours=limit("run_event_retention_hours"))
    deleted = (
        session.query(AgentRunEvent)
        .filter(
            AgentRunEvent.event_type == "delta",
            AgentRunEvent.created_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
    session.commit()
    return int(deleted or 0)


def reconcile_runs(
    db: Session | None = None,
    *,
    session_factory: Callable[[], Session] | None = None,
    dispatch_run: DispatchRun | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Re-dispatch queued runs and interrupt expired running leases.

    Running runs with a live lease are never touched, so a restarted API or a
    second executor cannot abort work owned by another live worker.
    """
    owns_session = db is None
    session = db if db is not None else (session_factory or SessionLocal)()
    interrupted = 0
    redispatched = 0
    current = now or utc_now()
    try:
        queued_ids = [
            row[0]
            for row in (
                session.query(AgentRun.id)
                .filter(AgentRun.status == "queued")
                .order_by(AgentRun.created_at.asc())
                .all()
            )
        ]
        session.rollback()
        for run_id in queued_ids:
            if dispatch_after_commit(run_id, dispatch_run):
                redispatched += 1

        expired_ids = [
            row[0]
            for row in (
                session.query(AgentRun.id)
                .filter(
                    AgentRun.status == "running",
                    AgentRun.lease_expires_at.is_not(None),
                    AgentRun.lease_expires_at < current,
                )
                .all()
            )
        ]
        session.rollback()
        for run_id in expired_ids:
            if _interrupt_expired(session, run_id, current):
                interrupted += 1

        try:
            deleted = cleanup_expired_delta_events(session, now=current)
            if deleted:
                logger.info("清理过期 run delta 事件 %s 条", deleted)
        except Exception:  # noqa: BLE001 — maintenance must not break reconcile
            session.rollback()
            logger.exception("清理过期 run delta 事件失败")
        return {"interrupted": interrupted, "redispatched": redispatched}
    finally:
        if owns_session:
            session.close()
