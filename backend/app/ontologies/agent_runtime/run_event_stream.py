"""SSE subscription over the durable run event ledger."""
from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from sqlalchemy.orm import Session

from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.run_common import require_run, snapshot
from app.ontologies.agent_runtime.run_models import (
    TERMINAL_RUN_STATUSES,
    AgentRun,
    AgentRunEvent,
)


def stream_run_events(
    db: Session,
    ontology_id: str,
    current_user: Any,
    run_id: str,
    *,
    after_seq: int = 0,
    poll_interval: float | None = None,
    heartbeat_interval: float | None = None,
) -> Iterator[dict]:
    """Yield a snapshot followed by replayable events until terminal + flushed.

    The transport layer turns each dict into one SSE frame.  ``after_seq`` is
    an explicit replay cursor: every persisted event with ``seq > after_seq``
    is returned even when it is already reflected in the snapshot's
    ``lastSeq``.  Clients that want only new events should pass the snapshot
    ``lastSeq`` as the next cursor.
    """
    run = require_run(db, ontology_id, current_user, run_id)
    yield {"type": "snapshot", "snapshot": snapshot(run)}

    poll_seconds = (
        limit("run_event_poll_ms") / 1000
        if poll_interval is None
        else max(0.01, poll_interval)
    )
    heartbeat_seconds = (
        limit("run_heartbeat_seconds")
        if heartbeat_interval is None
        else max(0.1, heartbeat_interval)
    )
    last_seq = max(0, int(after_seq))
    last_heartbeat = time.monotonic()

    while True:
        events = (
            db.query(AgentRunEvent)
            .filter(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.seq > last_seq,
            )
            .order_by(AgentRunEvent.seq.asc())
            .all()
        )
        for event in events:
            payload = dict(event.payload or {})
            yield {
                "id": f"{run_id}:{event.seq}",
                "seq": event.seq,
                "type": event.event_type,
                **payload,
            }
            last_seq = event.seq

        state = (
            db.query(AgentRun.status, AgentRun.last_seq)
            .filter(
                AgentRun.id == run_id,
                AgentRun.ontology_id == ontology_id,
            )
            .first()
        )
        if state is None:
            db.rollback()
            return
        status, run_last_seq = state
        if status in TERMINAL_RUN_STATUSES and last_seq >= int(run_last_seq):
            db.rollback()
            return

        current = time.monotonic()
        if current - last_heartbeat >= heartbeat_seconds:
            yield {"type": "heartbeat"}
            last_heartbeat = current
        # End the read transaction so the next poll observes committed events
        # from the worker (SQLite in particular can otherwise pin a snapshot).
        db.rollback()
        time.sleep(poll_seconds)
