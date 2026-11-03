"""Durable agent-run endpoints (separate router from the legacy chat router).

Mounted by the application under ``/api/v2/formal/ontologies``.  The legacy
``/agent/chat`` status/cancel endpoints are adapted by the caller to the same
service functions; this router owns the new nested run contract:

  POST /{ontology_id}/agent/chat/runs
  GET  /{ontology_id}/agent/chat/runs/{run_id}/events
  POST /{ontology_id}/agent/chat/runs/{run_id}/cancel
  POST /{ontology_id}/agent/chat/runs/{run_id}/retry
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_runtime import run_service
from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime.application_errors import (
    AgentRuntimeApplicationError,
)

router = APIRouter()

_APPLICATION_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "conflict": 409,
    "invalid": 422,
}


def _ok(data: Any) -> dict:
    return {"data": data}


def _application_call(function, /, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except AgentRuntimeApplicationError as error:
        raise HTTPException(
            _APPLICATION_STATUS[error.kind], detail=error.detail,
        ) from error


def _sse_frame(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False, default=str)
    if event.get("type") == "heartbeat":
        return f"event: heartbeat\ndata: {data}\n\n"
    event_id = event.get("id")
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}data: {data}\n\n"


@router.post("/{ontology_id}/agent/chat/runs")
def submit_run(
    ontology_id: str,
    body: S.ChatRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=False)
    return _ok(_application_call(
        run_service.submit_run, db, ontology_id, current_user, body,
    ))


@router.get("/{ontology_id}/agent/chat/runs/{run_id}/events")
def stream_run_events(
    ontology_id: str,
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=False)
    # Validate ownership before response headers start; errors raised from the
    # generator body could no longer be mapped to an HTTP status.
    _application_call(
        run_service.get_run, db, ontology_id, current_user, run_id,
    )
    events = run_service.stream_run_events(
        db, ontology_id, current_user, run_id,
        after_seq=after_seq,
    )
    return StreamingResponse(
        (_sse_frame(event) for event in events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{ontology_id}/agent/chat/runs/{run_id}/cancel")
def cancel_run(
    ontology_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=False)
    return _ok(_application_call(
        run_service.cancel_run, db, ontology_id, current_user, run_id,
    ))


@router.post("/{ontology_id}/agent/chat/runs/{run_id}/retry")
def retry_run(
    ontology_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_ontology_access(db, ontology_id, current_user, write=False)
    return _ok(_application_call(
        run_service.retry_run, db, ontology_id, current_user, run_id,
    ))
