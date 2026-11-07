"""HTTP chat adapters backed by the durable run application service."""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.ontologies.agent_runtime import run_service
from app.ontologies.agent_runtime import schemas as S


def run_sync(
    db: Session,
    ontology_id: str,
    current_user: Any,
    body: S.ChatRequest,
    *,
    run_turn_fn: Callable[..., Iterator[dict]],
) -> dict:
    run = run_service.submit_run(db, ontology_id, current_user, body, dispatch=False)
    run_service.execute_run(
        run["runId"], db=db,
        session_factory=sessionmaker(bind=db.get_bind()),
        run_turn_fn=run_turn_fn, stream_model=False,
    )
    db.expire_all()
    run = run_service.get_run(db, ontology_id, current_user, run["runId"])
    return {
        "conversationId": run["conversationId"],
        "releaseId": run["releaseId"],
        "model": run.get("model"),
        "steps": run.get("steps") or [],
        "content": run.get("content") if run["status"] == "succeeded" else None,
        "citations": run.get("citations") or [],
        "proposals": run.get("proposals") or [],
        "usage": run.get("usage"),
        "verification": _verification(db, run["runId"]),
        "error": run.get("error"),
    }


def _verification(db: Session, run_id: str):
    from app.ontologies.agent_runtime.run_models import AgentRunEvent
    event = db.query(AgentRunEvent).filter(
        AgentRunEvent.run_id == run_id, AgentRunEvent.event_type == "answer",
    ).order_by(AgentRunEvent.seq.desc()).first()
    return event.payload.get("verification") if event is not None else None


def stream_events(
    ontology_id: str,
    current_user: Any,
    body: S.ChatRequest,
    *,
    db: Session,
    run_turn_fn: Callable[..., Iterator[dict]] | None = None,
) -> Iterator[str]:
    run = run_service.submit_run(db, ontology_id, current_user, body)
    events = run_service.stream_run_events(db, ontology_id, current_user, run["runId"])
    return (f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            for event in events if event["type"] != "snapshot")
