"""外部 Agent callback 的归属校验与 canonical 事件映射。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import ContractError
from .models import ExecutionCall, ExecutionRun
from .store import append_event


def append_agent_callback(
    db: Session,
    *,
    owner_id: str,
    run_id: str,
    call_id: str,
    connector_id: str,
    provider_event_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
    call = db.scalar(select(ExecutionCall).where(ExecutionCall.id == call_id, ExecutionCall.run_id == run_id).with_for_update())
    if run is None or call is None or run.owner_id != owner_id:
        raise KeyError("callback scope not found")
    # The connector identity is part of the call's authorization snapshot. A
    # callback must never be able to impersonate another connector merely by
    # knowing a run/call id. Legacy calls without a target remain accepted for
    # compatibility and are fenced by the provider event uniqueness key.
    if call.target_ref and call.target_ref != connector_id:
        raise ContractError("callback connector does not match call target")
    if payload.get("connector_id") != connector_id or payload.get("provider_event_id") != provider_event_id:
        raise ContractError("callback payload identity does not match envelope")
    if event_type not in {"call.progress", "call.outcome_changed", "attempt.result"}:
        raise ContractError("unsupported external callback event")
    append_event(
        db, run, event_type=event_type, payload=payload,
        actor={"kind": "connector"}, command_id=f"callback:{connector_id}:{provider_event_id}",
        idempotency_key=f"provider:{connector_id}:{provider_event_id}",
        connector_id=connector_id, provider_event_id=provider_event_id,
    )
