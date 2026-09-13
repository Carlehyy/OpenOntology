from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.super_assistant.kernel.contracts import CancelReason, ContractError
from app.super_assistant.kernel.models import ExecutionCall, ExecutionCommand, ExecutionRun
from app.super_assistant.kernel.schemas import CancelRunRequest, CreateRunRequest, RunAccepted, RunView
from app.super_assistant.kernel.store import IdempotencyConflict, VersionConflict, cancel_run, create_run


router = APIRouter()


def _request_id() -> str:
    return str(uuid.uuid4())


@router.post(
    "/conversations/{conversation_id}/runs",
    response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_kernel_run(
    conversation_id: str,
    body: CreateRunRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if idempotency_header is None or idempotency_header != body.idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must match body.idempotency_key")
    try:
        run, _ = create_run(
            db, owner_id=user.id, conversation_id=conversation_id, goal=body.goal,
            idempotency_key=body.idempotency_key, deadline=body.deadline,
            parent_run_id=body.parent_run_id, join_policy=body.join_policy,
            binding=body.binding.model_dump(exclude_none=True) if body.binding else None,
        )
        db.commit()
        return RunAccepted(
            run_id=run.id, execution_version="kernel.v1",
            stream_url=f"/api/v2/super-assistant/runs/{run.id}/events",
            request_id=_request_id(),
        )
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="conversation or parent run not found") from exc
    except IdempotencyConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="idempotency_conflict") from exc
    except ContractError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/runs/{run_id}", response_model=RunView)
def get_kernel_run(run_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id, ExecutionRun.owner_id == user.id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    calls = db.scalars(select(ExecutionCall).where(ExecutionCall.run_id == run.id).order_by(ExecutionCall.call_index)).all()
    try:
        binding = json.loads(run.binding_snapshot_ref) if run.binding_snapshot_ref else {}
    except json.JSONDecodeError:
        binding = {}
    return RunView(
        run_id=run.id, conversation_id=run.conversation_id, status=run.status,
        wait_reason=run.wait_reason, version=run.version, execution_version=run.execution_version,
        goal=run.goal, deadline=run.deadline, binding_snapshot=binding,
        calls=[{"call_id": c.id, "status": c.status, "outcome": c.outcome, "capability_key": c.capability_key} for c in calls],
    )


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_kernel_run(
    run_id: str,
    body: CancelRunRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    try:
        expected_version = int(if_match.strip('"'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="If-Match must be a Run version") from exc
    try:
        run = cancel_run(
            db, run_id=run_id, owner_id=user.id, reason=CancelReason(body.reason),
            idempotency_key=body.idempotency_key, expected_version=expected_version,
        )
        command = db.scalar(select(ExecutionCommand).where(
            ExecutionCommand.run_id == run.id,
            ExecutionCommand.scope == f"run:{run.id}",
            ExecutionCommand.idempotency_key == body.idempotency_key,
        ))
        db.commit()
        return {"command_id": command.command_id if command else body.idempotency_key, "status": run.status, "version": run.version}
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="run not found") from exc
    except VersionConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="version_conflict") from exc
    except IdempotencyConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="idempotency_conflict") from exc
    except ContractError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
