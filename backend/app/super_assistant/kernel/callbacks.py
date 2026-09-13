"""外部 Agent callback 的归属校验与 canonical 事件映射。"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db
from app.shared.encryption import decrypt
from app.super_assistant.kernel.schemas import AgentCallbackRequest
from app.super_assistant.models import SuperAssistantRemoteAgent
from .contracts import ContractError
from .models import ExecutionAttempt, ExecutionCall, ExecutionRun
from .store import append_event


callback_router = APIRouter()


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
        remote = db.scalar(select(SuperAssistantRemoteAgent).where(
            SuperAssistantRemoteAgent.id == connector_id,
            SuperAssistantRemoteAgent.owner_id == owner_id,
        ))
        if remote is None or call.target_ref != remote.key:
            raise ContractError("callback connector does not match call target")
    if payload.get("connector_id") != connector_id or payload.get("provider_event_id") != provider_event_id:
        raise ContractError("callback payload identity does not match envelope")
    if event_type not in {"call.progress", "call.outcome_changed", "attempt.result"}:
        raise ContractError("unsupported external callback event")
    payload_call_id = payload.get("call_id")
    if payload_call_id is not None and str(payload_call_id) != call_id:
        raise ContractError("callback payload call does not match envelope")
    if event_type == "attempt.result":
        attempt_id = payload.get("attempt_id")
        attempt = db.scalar(select(ExecutionAttempt).where(
            ExecutionAttempt.id == str(attempt_id or ""),
            ExecutionAttempt.call_id == call_id,
        ))
        if attempt is None:
            raise ContractError("callback attempt does not belong to call")
    append_event(
        db, run, event_type=event_type, payload=payload,
        actor={"kind": "connector"}, command_id=f"callback:{connector_id}:{provider_event_id}",
        idempotency_key=f"provider:{connector_id}:{provider_event_id}",
        connector_id=connector_id, provider_event_id=provider_event_id,
    )


def _canonical_callback(value: AgentCallbackRequest, *, run_id: str, call_id: str) -> bytes:
    payload_bytes = json.dumps(value.payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(payload_bytes).hexdigest() != value.payload_hash:
        raise HTTPException(status_code=422, detail="callback payload hash mismatch")
    return f"{run_id}.{value.request_id}.{call_id}.{value.provider_event_id}.{value.payload_hash}".encode("utf-8")


@callback_router.post("/runs/{run_id}/calls/{call_id}/callback", status_code=202)
def receive_agent_callback(
    run_id: str,
    call_id: str,
    body: AgentCallbackRequest,
    db: Session = Depends(get_db),
    x_callback_timestamp: str | None = Header(default=None, alias="X-Callback-Timestamp"),
    x_callback_signature: str | None = Header(default=None, alias="X-Callback-Signature"),
):
    """Authenticate and reconcile one remote Agent callback."""
    try:
        timestamp = int(x_callback_timestamp or "")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid callback timestamp") from exc
    if abs(int(time.time()) - timestamp) > 300:
        raise HTTPException(status_code=401, detail="callback timestamp expired")
    if not x_callback_signature or not x_callback_signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="callback signature required")
    remote = db.scalar(select(SuperAssistantRemoteAgent).where(
        (SuperAssistantRemoteAgent.id == body.connector_id) | (SuperAssistantRemoteAgent.key == body.connector_id),
        SuperAssistantRemoteAgent.enabled.is_(True),
    ))
    if remote is None or not remote.token_encrypted:
        raise HTTPException(status_code=401, detail="callback connector not authorized")
    try:
        secret = decrypt(remote.token_encrypted)
    except Exception as exc:  # noqa: BLE001 - invalid credentials are auth failures
        raise HTTPException(status_code=401, detail="callback connector not authorized") from exc
    if not secret:
        raise HTTPException(status_code=401, detail="callback connector not authorized")
    signed = f"{timestamp}.".encode("ascii") + _canonical_callback(body, run_id=run_id, call_id=call_id)
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_callback_signature):
        raise HTTPException(status_code=401, detail="invalid callback signature")
    payload = dict(body.payload or {})
    if payload.get("connector_id", body.connector_id) != body.connector_id or payload.get("provider_event_id", body.provider_event_id) != body.provider_event_id:
        raise HTTPException(status_code=422, detail="callback payload identity mismatch")
    payload.update({"connector_id": body.connector_id, "provider_event_id": body.provider_event_id})
    try:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
        call = db.scalar(select(ExecutionCall).where(ExecutionCall.id == call_id, ExecutionCall.run_id == run_id).with_for_update())
        if run is None or call is None or run.owner_id != remote.owner_id:
            raise KeyError("callback scope not found")
        if call.target_ref not in {remote.id, remote.key}:
            raise ContractError("callback connector does not match call target")
        if body.event_type != "call.outcome_changed":
            # Outcome callbacks are appended by the reconciler atomically
            # with their state change. Progress/attempt facts have no state
            # transition and can be recorded directly.
            append_agent_callback(
                db, owner_id=remote.owner_id, run_id=run_id, call_id=call_id,
                connector_id=body.connector_id, provider_event_id=body.provider_event_id,
                event_type=body.event_type, payload=payload,
            )
            db.commit()
        else:
            db.rollback()
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="callback scope not found") from exc
    except ContractError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if body.event_type == "call.outcome_changed":
        from .runtime import reconcile_execution_message
        import asyncio
        reported_state = payload.get("remote_state")
        # Some providers use ``status=closed`` as a transport envelope and
        # carry the semantic terminal state in ``outcome``.
        if not reported_state or str(reported_state).strip().lower() in {"closed", "done"}:
            reported_state = payload.get("outcome") or payload.get("status")
        accepted = asyncio.run(reconcile_execution_message({
            "run_id": run_id, "call_id": call_id, "connector_id": body.connector_id,
            "provider_event_id": body.provider_event_id,
            "remote_state": reported_state,
            "status": payload.get("status"), "content": payload.get("content"),
            "evidence_ref": payload.get("evidence_ref"), "artifacts": payload.get("artifacts"),
        }))
        if not accepted:
            raise HTTPException(status_code=503, detail="callback reconciliation not persisted; retry with the same event id")
    return {"accepted": True, "provider_event_id": body.provider_event_id}
