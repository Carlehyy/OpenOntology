from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.shared.database import SessionLocal

from app.deps import get_current_user, get_db
from app.super_assistant.kernel.contracts import CancelReason, ContractError
from app.super_assistant.kernel.models import Approval, Artifact, ExecutionCall, ExecutionCommand, ExecutionDispatchOutbox, ExecutionEvent, ExecutionRun, InboxItem
from app.super_assistant.kernel.schemas import ApprovalDecisionRequest, CancelRunRequest, ControlRunRequest, CreateRunRequest, InputRequest, RetryRunRequest, RunAccepted, RunSummary, RunView
from app.super_assistant.kernel.store import IdempotencyConflict, VersionConflict, append_event, append_input, cancel_run, control_run, create_run, record_command
from app.super_assistant.kernel.artifacts import artifact_is_expired, validate_object_storage_ref, verify_artifact
from app.shared.storage import get_storage_service
from app.super_assistant.kernel.outbox import replay_dead_once
from app.super_assistant.models import SuperAssistantConversation


router = APIRouter()


def _request_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
            parent_run_id=body.parent_run_id, join_policy=body.join_policy, max_steps=body.max_steps,
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
    inbox = db.scalars(select(InboxItem).where(InboxItem.run_id == run.id, InboxItem.status.in_(("pending", "claimed"))).order_by(InboxItem.priority, InboxItem.id)).all()
    artifacts = db.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.owner_id == user.id).order_by(Artifact.id)).all()
    try:
        binding = json.loads(run.binding_snapshot_ref) if run.binding_snapshot_ref else {}
    except json.JSONDecodeError:
        binding = {}
    return RunView(
        run_id=run.id, conversation_id=run.conversation_id, status=run.status,
        wait_reason=run.wait_reason, version=run.version, execution_version=run.execution_version,
        goal=run.goal, deadline=run.deadline, binding_snapshot=binding,
        current_inbox=[{"inbox_id": item.id, "kind": item.kind, "question_id": item.question_id, "approval_id": item.approval_id, "expires_at": item.expires_at} for item in inbox],
        calls=[{"call_id": c.id, "status": c.status, "outcome": c.outcome, "capability_key": c.capability_key} for c in calls],
        artifacts=[{"artifact_id": a.id, "kind": a.kind, "mime_type": a.mime_type, "size": a.size, "checksum": a.checksum, "status": a.status, "business_status": a.business_status} for a in artifacts],
    )


@router.post("/runs/{run_id}/retry", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
def retry_kernel_run(
    run_id: str,
    body: RetryRunRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Retry a failed Run as a new immutable execution lineage.

    A terminal Run is never reopened. The new Run reuses the original goal and
    binding snapshot, while receiving a fresh idempotency key and execution
    facts so the failed attempt remains auditable.
    """
    if idempotency_header is None or idempotency_header != body.idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must match body.idempotency_key")
    source = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id, ExecutionRun.owner_id == user.id))
    if source is None:
        raise HTTPException(status_code=404, detail="run not found")
    if source.status != "failed":
        raise HTTPException(status_code=409, detail="only failed Run can retry")
    try:
        binding = json.loads(source.binding_snapshot_ref) if source.binding_snapshot_ref else {}
        run, _ = create_run(
            db,
            owner_id=user.id,
            conversation_id=source.conversation_id,
            goal=source.goal,
            idempotency_key=body.idempotency_key,
            deadline=None,
            max_steps=body.max_steps,
            binding=binding,
        )
        db.commit()
        return RunAccepted(
            run_id=run.id,
            execution_version="kernel.v1",
            stream_url=f"/api/v2/super-assistant/runs/{run.id}/events",
            request_id=_request_id(),
        )
    except IdempotencyConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="idempotency_conflict") from exc
    except ContractError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/conversations/{conversation_id}/runs", response_model=list[RunSummary])
def list_kernel_runs(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """List independent long-running Runs for one conversation."""
    conversation = db.scalar(select(SuperAssistantConversation).where(
        SuperAssistantConversation.id == conversation_id,
        SuperAssistantConversation.owner_id == user.id,
    ))
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = db.scalars(
        select(ExecutionRun)
        .where(ExecutionRun.conversation_id == conversation_id, ExecutionRun.owner_id == user.id)
        .order_by(ExecutionRun.created_at.desc())
        .limit(limit)
    ).all()
    return [RunSummary(
        run_id=row.id,
        conversation_id=row.conversation_id,
        status=row.status,
        wait_reason=row.wait_reason,
        version=row.version,
        goal=row.goal,
        deadline=row.deadline,
        created_at=row.created_at,
        updated_at=row.updated_at,
    ) for row in rows]


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


def _parse_if_match(if_match: str | None) -> int:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    try:
        return int(if_match.strip('"'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="If-Match must be a Run version") from exc


@router.post("/runs/{run_id}/pause", status_code=status.HTTP_202_ACCEPTED)
def pause_kernel_run(run_id: str, body: ControlRunRequest, db: Session = Depends(get_db), user=Depends(get_current_user), if_match: str | None = Header(default=None, alias="If-Match")):
    return _control_kernel_run(run_id, body, "pause", _parse_if_match(if_match), db, user)


@router.post("/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
def resume_kernel_run(run_id: str, body: ControlRunRequest, db: Session = Depends(get_db), user=Depends(get_current_user), if_match: str | None = Header(default=None, alias="If-Match")):
    return _control_kernel_run(run_id, body, "resume", _parse_if_match(if_match), db, user)


def _control_kernel_run(run_id: str, body: ControlRunRequest, action: str, expected_version: int, db: Session, user):
    try:
        run = control_run(db, run_id=run_id, owner_id=user.id, action=action, idempotency_key=body.idempotency_key, expected_version=expected_version)
        command = db.scalar(select(ExecutionCommand).where(ExecutionCommand.run_id == run.id, ExecutionCommand.idempotency_key == body.idempotency_key))
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


@router.post("/runs/{run_id}/inputs", status_code=status.HTTP_202_ACCEPTED)
def submit_kernel_input(run_id: str, body: InputRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    try:
        item = append_input(
            db, run_id=run_id, owner_id=user.id, kind=body.kind,
            payload={"content": body.content} if body.content is not None else {"content_ref": body.content_ref},
            idempotency_key=body.idempotency_key, question_id=body.question_id,
            target_ref=body.content_ref,
        )
        db.commit()
        return {"inbox_id": item.id, "status": item.status, "run_id": run_id}
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="run not found") from exc
    except ContractError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runs/{run_id}/approvals/{approval_id}/decision", status_code=status.HTTP_202_ACCEPTED)
def decide_kernel_approval(run_id: str, approval_id: str, body: ApprovalDecisionRequest, db: Session = Depends(get_db), user=Depends(get_current_user), if_match: str | None = Header(default=None, alias="If-Match")):
    expected_version = _parse_if_match(if_match)
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id, ExecutionRun.owner_id == user.id).with_for_update())
    approval = db.scalar(select(Approval).where(Approval.id == approval_id, Approval.run_id == run_id, Approval.owner_id == user.id).with_for_update())
    if run is None or approval is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="run or approval not found")
    try:
        command, replayed = record_command(db, run, kind="approval_decision", idempotency_key=body.idempotency_key, payload={"approval_id": approval_id, "decision": body.decision, "expected_version": expected_version})
        if replayed:
            db.commit()
            return {"command_id": command.command_id, "status": approval.status, "version": run.version}
        if run.version != expected_version:
            raise VersionConflict("version_conflict")
        if approval.status != "pending":
            raise ContractError("approval is no longer pending")
        approval.status, approval.decided_at, approval.decided_by = body.decision, _utcnow(), user.id
        append_event(db, run, event_type="approval.decided", payload={"approval_id": approval.id, "decision": body.decision, "actor": "user", "decided_at": approval.decided_at.isoformat(), "authorization_hash": approval.parameter_hash}, actor={"kind": "user"}, command_id=command.command_id, idempotency_key=body.idempotency_key)
        if run.status == "waiting_approval":
            before = run.status
            run.status, run.wait_reason, run.version = "active", None, run.version + 1
            append_event(db, run, event_type="run.status_changed", payload={"from": before, "to": run.status, "reason": "approval_decided", "actor": "user", "version": run.version}, actor={"kind": "user"}, command_id=command.command_id, idempotency_key=body.idempotency_key)
        command.result = {"status": approval.status, "version": run.version}
        db.commit()
        return {"command_id": command.command_id, "status": approval.status, "version": run.version}
    except VersionConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="version_conflict") from exc
    except (ContractError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs/{run_id}/events")
def stream_kernel_events(
    run_id: str,
    after_seq: int = Query(default=-1, ge=-1),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id, ExecutionRun.owner_id == user.id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    cursor = after_seq
    if last_event_id:
        parts = last_event_id.split(":")
        if len(parts) != 2 or parts[0] != run_id:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be run_id:seq")
        try:
            cursor = int(parts[1])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be run_id:seq") from exc
    oldest = db.scalar(select(ExecutionEvent.seq).where(ExecutionEvent.run_id == run_id).order_by(ExecutionEvent.seq).limit(1))
    if oldest is not None and cursor < oldest - 1:
        raise HTTPException(status_code=410, detail="event cursor is outside replay window")

    # The request dependency session must not remain pinned for the lifetime
    # of an SSE subscription.  Each short poll uses a fresh session and the
    # blocking SQL work is moved off the event loop.
    db.rollback()

    def read_batch():
        session = SessionLocal()
        try:
            current_run = session.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id))
            events = session.scalars(
                select(ExecutionEvent)
                .where(ExecutionEvent.run_id == run_id, ExecutionEvent.seq > cursor)
                .order_by(ExecutionEvent.seq).limit(100)
            ).all()
            snapshot = None if current_run is None else {
                "run_id": current_run.id, "status": current_run.status,
                "version": current_run.version, "wait_reason": current_run.wait_reason,
            }
            return snapshot, events
        finally:
            session.close()

    async def generate():
        nonlocal cursor
        last_ping = time.monotonic()
        sent_snapshot = False
        while True:
            snapshot, events = await asyncio.to_thread(read_batch)
            if not sent_snapshot and snapshot is not None:
                yield f"event: run.snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\nretry: 5000\n\n"
                sent_snapshot = True
            for event in events:
                cursor = event.seq
                yield f"id: {run_id}:{event.seq}\nevent: {event.event_type}\ndata: {json.dumps(event.payload, ensure_ascii=False, default=str)}\nretry: 5000\n\n"
            if snapshot is None or (snapshot["status"] in {"cancelled", "expired", "completed", "failed"} and not events):
                break
            if time.monotonic() - last_ping >= 15:
                yield ": ping\n\n"
                last_ping = time.monotonic()
            await asyncio.sleep(0.25)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _artifact_bytes(artifact: Artifact, *, run_id: str) -> bytes:
    if artifact.inline_content is not None:
        data = artifact.inline_content.encode("utf-8")
    else:
        try:
            validate_object_storage_ref(
                artifact.storage_ref,
                owner_id=artifact.owner_id,
                run_id=run_id,
                artifact_id=artifact.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="artifact_storage_ref_invalid") from exc
        try:
            data = get_storage_service().get_object(artifact.storage_ref)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="artifact object not found") from exc
        except Exception as exc:
            # Do not turn a storage outage into a false 404.  The object store
            # is an operational dependency and callers should retry later.
            if getattr(exc, "code", None) in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise HTTPException(status_code=404, detail="artifact object not found") from exc
            raise HTTPException(status_code=503, detail="artifact storage unavailable") from exc
    integrity = verify_artifact(data, expected_checksum=artifact.checksum, expected_size=artifact.size)
    if integrity.integrity_status != "verified":
        raise HTTPException(status_code=409, detail="artifact_integrity_failed")
    return data


def _load_kernel_artifact(run_id: str, artifact_id: str, db: Session, user):
    artifact = db.scalar(select(Artifact).where(Artifact.id == artifact_id, Artifact.run_id == run_id, Artifact.owner_id == user.id))
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact_is_expired(status=artifact.status, retention_until=artifact.retention_until):
        raise HTTPException(status_code=410, detail="artifact expired or deleted")
    if artifact.status != "complete" or artifact.integrity_status != "verified":
        raise HTTPException(status_code=409, detail="artifact_not_ready")
    return artifact


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
def get_kernel_artifact(run_id: str, artifact_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    artifact = _load_kernel_artifact(run_id, artifact_id, db, user)
    data = _artifact_bytes(artifact, run_id=run_id)
    # Preserve the existing JSON shape.  Binary/object-backed assets remain
    # represented by null content and use the explicit download endpoint.
    content = artifact.inline_content
    if content is None and (artifact.mime_type.startswith("text/") or artifact.mime_type == "application/json") and len(data) <= 1 * 1024 * 1024:
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            content = None
    return {"artifact_id": artifact.id, "mime_type": artifact.mime_type, "size": artifact.size, "checksum": artifact.checksum, "status": artifact.status, "business_status": artifact.business_status, "content": content, "download_url": f"/api/v2/super-assistant/runs/{run_id}/artifacts/{artifact.id}/download"}


@router.get("/runs/{run_id}/artifacts/{artifact_id}/download")
def download_kernel_artifact(run_id: str, artifact_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    artifact = _load_kernel_artifact(run_id, artifact_id, db, user)
    data = _artifact_bytes(artifact, run_id=run_id)
    return Response(
        content=data,
        media_type=artifact.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.id}"',
            "X-Artifact-Checksum": artifact.checksum,
            "X-Artifact-Size": str(artifact.size),
        },
    )


@router.post("/runs/{run_id}/dispatch/{outbox_id}/replay", status_code=status.HTTP_202_ACCEPTED)
def replay_kernel_dispatch(run_id: str, outbox_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    row = db.scalar(select(ExecutionDispatchOutbox).where(ExecutionDispatchOutbox.id == outbox_id, ExecutionDispatchOutbox.run_id == run_id))
    if row is None:
        raise HTTPException(status_code=404, detail="dispatch not found")
    try:
        replayed = replay_dead_once(db, outbox_id=outbox_id, owner_id=user.id)
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="dispatch not found") from exc
    return {"outbox_id": outbox_id, "status": "pending" if replayed else row.status, "replayed": replayed}
