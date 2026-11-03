"""Public application facade for durable ontology-assistant runs.

The implementation is split by responsibility:

- ``run_submission``: submit / retry / status / cancel commands;
- ``run_execution``: claim, lease heartbeat, event consumption, terminal state;
- ``run_reconciliation``: queued re-dispatch, expired-lease interruption and
  delta retention cleanup;
- ``run_event_stream``: snapshot + replayable SSE subscription;
- ``run_common``: shared snapshot/idempotency/capacity/dispatch primitives;
- ``run_events``: ordered event ledger and bounded step payloads.

Existing callers (chat service, NATS task entry, run router, tests) import this
module; it composes the real modules above rather than duplicating them.
"""
from __future__ import annotations

from app.ontologies.agent_runtime.run_common import (
    DispatchUnavailable,
    set_dispatch_run_port,
)
from app.ontologies.agent_runtime.run_event_stream import stream_run_events
from app.ontologies.agent_runtime.run_execution import execute_run
from app.ontologies.agent_runtime.run_reconciliation import (
    cleanup_expired_delta_events,
    reconcile_runs,
)
from app.ontologies.agent_runtime.run_submission import (
    cancel_run,
    get_run,
    retry_run,
    submit_run,
)

__all__ = [
    "DispatchUnavailable",
    "cancel_run",
    "cleanup_expired_delta_events",
    "execute_run",
    "get_run",
    "reconcile_runs",
    "retry_run",
    "set_dispatch_run_port",
    "stream_run_events",
    "submit_run",
]
