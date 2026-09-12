"""Synchronous and SSE chat orchestration for Exploration."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.exploration.context_builder import _CARD_PROGRESS_TOOL
from app.exploration.orchestrator import run_exploration_turn
from app.exploration.session_service import _ok, _require_session


def chat(
    session_id: str,
    body,
    db: Session,
    current_user,
    *,
    require_session_fn=_require_session,
    run_turn_fn=run_exploration_turn,
    ok_fn: Callable[[Any], dict] = _ok,
    json_module=json,
    streaming_response_cls=StreamingResponse,
):
    require_session_fn(db, session_id, current_user)
    if not (body.message or "").strip():
        raise HTTPException(422, "message 不能为空")

    if not body.stream:
        events = list(run_turn_fn(
            db,
            session_id,
            current_user,
            body.message,
            model_id=body.model_id,
            web_search=body.web_search,
        ))
        answer = next(
            (event for event in events if event["type"] == "answer"),
            None,
        )
        error = next(
            (event for event in events if event["type"] == "error"),
            None,
        )
        meta = next(
            (event for event in events if event["type"] == "meta"),
            {},
        )
        canvas_event = next(
            (
                event
                for event in reversed(events)
                if event["type"] == "canvas"
            ),
            None,
        )
        steps = [
            event
            for event in events
            # heartbeat（llm_round 活性心跳）按类型天然不进此列表；
            # attachment_card 是索引卡懒生成的进度 step，同样不是真实工具
            # 步骤 —— 都不进非流式响应的 steps（保持与持久化 steps 同义）。
            if event["type"] == "step" and event.get("tool") != _CARD_PROGRESS_TOOL
        ]
        return ok_fn({
            "sessionId": meta.get("sessionId") or session_id,
            "model": meta.get("model"),
            "steps": [
                {
                    key: value
                    for key, value in step.items()
                    if key != "type"
                }
                for step in steps
            ],
            "content": (answer or {}).get("content"),
            "usage": (answer or {}).get("usage"),
            "canvas": (canvas_event or {}).get("canvas"),
            "completeness": (canvas_event or {}).get("completeness"),
            "error": (error or {}).get("message"),
        })

    user = current_user

    def event_stream():
        # SSE outlives request dependencies, so it owns a fresh DB session.
        from app.database import SessionLocal

        session = SessionLocal()
        try:
            for event in run_turn_fn(
                session,
                session_id,
                user,
                body.message,
                model_id=body.model_id,
                web_search=body.web_search,
            ):
                payload = json_module.dumps(
                    event,
                    ensure_ascii=False,
                    default=str,
                )
                yield f"data: {payload}\n\n"
        finally:
            session.close()

    return streaming_response_cls(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
