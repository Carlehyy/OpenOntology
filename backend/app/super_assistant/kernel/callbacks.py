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
    if event_type not in {"call.progress", "call.outcome_changed", "attempt.result"}:
        raise ContractError("unsupported external callback event")
    append_event(
        db, run, event_type=event_type, payload=payload,
        actor={"kind": "connector"}, command_id=f"callback:{connector_id}:{provider_event_id}",
        idempotency_key=f"provider:{connector_id}:{provider_event_id}",
        connector_id=connector_id, provider_event_id=provider_event_id,
    )

