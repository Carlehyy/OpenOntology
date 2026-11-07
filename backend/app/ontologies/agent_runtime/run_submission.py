"""Submit, retry, query and cancel commands for durable agent runs."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ontology import OntologyProject
from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime.application_errors import (
    conflict,
    forbidden,
    invalid,
    not_found,
)
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.models import AgentMessage
from app.ontologies.agent_runtime.run_common import (
    DispatchRun,
    current_user_id,
    dispatch_after_commit,
    enforce_capacity,
    existing_run_result,
    normalize_run_id,
    now,
    request_hash,
    require_run,
    resolve_conversation,
    snapshot,
    submission_lock,
)
from app.ontologies.agent_runtime.run_events import append_event
from app.ontologies.agent_runtime.run_release import submission_release_id
from app.ontologies.agent_runtime.run_models import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    AgentRun,
    AgentRunEvent,
)


def submit_run(
    db: Session,
    ontology_id: str,
    current_user: Any,
    body: S.ChatRequest,
    *,
    dispatch: bool = True,
    dispatch_run: DispatchRun | None = None,
) -> dict:
    """Persist one run intent atomically and optionally dispatch its id."""
    user_id = current_user_id(current_user)
    question = (body.message or "").strip()
    if not question:
        raise invalid("message 不能为空")
    client_run_id = normalize_run_id(body.run_id)
    stream_model = bool(body.stream)
    request_hash_value = request_hash(
        ontology_id=ontology_id,
        user_id=user_id,
        question=question,
        conversation_id=body.conversation_id,
        model_id=body.model_id,
        release_id=body.release_id,
        stream_model=stream_model,
    )

    if client_run_id:
        existing = (
            db.query(AgentRun).filter(AgentRun.id == client_run_id).first()
        )
        if existing is not None:
            return existing_run_result(
                existing,
                request_hash_value=request_hash_value,
                user_id=user_id,
                ontology_id=ontology_id,
            )

    selected_release_id = submission_release_id(db, ontology_id, user_id, body)
    try:
        _ontology, _profile, scope = build_scope(
            db, ontology_id, release_id=selected_release_id,
        )
    except ToolError as error:
        if db.get(OntologyProject, ontology_id) is None:
            raise not_found("本体不存在") from error
        raise invalid(str(error)) from error

    release_id = scope.release_id
    run_id = client_run_id or str(uuid.uuid4())

    with submission_lock(db):
        if client_run_id:
            existing = (
                db.query(AgentRun).filter(AgentRun.id == client_run_id).first()
            )
            if existing is not None:
                # Release the transaction-scoped advisory lock before returning.
                db.rollback()
                return existing_run_result(
                    existing,
                    request_hash_value=request_hash_value,
                    user_id=user_id,
                    ontology_id=ontology_id,
                )

        enforce_capacity(db, user_id)
        try:
            conversation = resolve_conversation(
                db,
                ontology_id=ontology_id,
                user_id=user_id,
                release_id=release_id,
                conversation_id=body.conversation_id,
                question=question,
            )
            user_message = AgentMessage(
                conversation_id=conversation.id,
                role="user",
                content=question,
            )
            db.add(user_message)
            db.flush()

            run = AgentRun(
                id=run_id,
                ontology_id=ontology_id,
                release_id=release_id,
                conversation_id=conversation.id,
                user_id=user_id,
                user_message_id=user_message.id,
                model_id=body.model_id,
                model=None,
                status="queued",
                question=question,
                request_hash=request_hash_value,
                content="",
                steps=[],
                citations=[],
                proposals=[],
                stream_model=stream_model,
            )
            db.add(run)
            conversation.updated_at = now()
            db.flush()

            if append_event(
                db,
                run.id,
                "meta",
                {
                    "runId": run.id,
                    "ontologyId": ontology_id,
                    "releaseId": release_id,
                    "conversationId": conversation.id,
                    "status": "queued",
                    "model": body.model_id,
                },
                require_status="queued",
            ) is None:
                db.rollback()
                raise conflict("回合事件初始化失败")

            db.commit()
        except IntegrityError as error:
            db.rollback()
            existing = (
                db.query(AgentRun).filter(AgentRun.id == run_id).first()
            )
            if existing is not None:
                return existing_run_result(
                    existing,
                    request_hash_value=request_hash_value,
                    user_id=user_id,
                    ontology_id=ontology_id,
                )
            raise conflict({
                "code": "conversation_busy",
                "message": "该会话已有正在执行或排队中的回合。",
            }) from error
        except Exception:
            db.rollback()
            raise

    if dispatch:
        dispatch_after_commit(run.id, dispatch_run)
        db.expire_all()
        refreshed = db.query(AgentRun).filter(AgentRun.id == run.id).first()
        if refreshed is not None:
            return snapshot(refreshed)
    db.expire_all()
    refreshed = db.query(AgentRun).filter(AgentRun.id == run.id).first()
    return snapshot(refreshed or run)


def get_run(
    db: Session,
    ontology_id: str,
    current_user: Any,
    run_id: str,
) -> dict:
    return snapshot(require_run(db, ontology_id, current_user, run_id))


def _request_running_cancel(
    db: Session,
    run: AgentRun,
    current,
) -> dict:
    """Record a cancel request on a running run (owner already checked)."""
    updated = (
        db.query(AgentRun)
        .filter(AgentRun.id == run.id, AgentRun.status == "running")
        .update(
            {
                "cancel_requested_at": current,
                "updated_at": current,
            },
            synchronize_session=False,
        )
    )
    if updated == 1:
        db.commit()
    else:
        db.rollback()
    db.expire_all()
    current_run = (
        db.query(AgentRun).filter(AgentRun.id == run.id).first() or run
    )
    if updated == 1:
        note = "取消请求已记录，执行将在下一个检查点停止。"
    elif current_run.status in TERMINAL_RUN_STATUSES:
        note = "该回合已结束，无需取消。"
    else:
        note = "取消未生效，请重试。"
    return {
        **snapshot(current_run),
        "cancelled": updated == 1,
        "note": note,
    }


def cancel_run(
    db: Session,
    ontology_id: str,
    current_user: Any,
    run_id: str,
) -> dict:
    """Queued runs cancel immediately; running runs only record the request.

    A running run must stay active until its worker observes the request and
    commits the terminal cancelled state.  This preserves the conversation
    single-flight guard and prevents a retry from overlapping a live worker.

    Cancel-race rule: the worker's lease-fenced terminal CAS decides the final
    state.  Success/error CAS requires ``cancel_requested_at IS NULL``; if a
    cancel landed between the core's last cancellation check and its answer or
    error yield, that terminal transaction fails, rolls back the queued
    assistant answer, and a cancel branch commits ``cancelled`` with a fresh
    ``[已取消]`` assistant message if the worker still owns a live lease.  If
    the success/error CAS wins first, the later cancel reports
    ``cancelled: false``.  Exactly one terminal state can win.
    """
    run = require_run(db, ontology_id, current_user, run_id)
    current = now()
    if run.status == "queued":
        updated = (
            db.query(AgentRun)
            .filter(AgentRun.id == run.id, AgentRun.status == "queued")
            .update(
                {
                    "status": "cancelled",
                    "cancel_requested_at": current,
                    "finished_at": current,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": current,
                },
                synchronize_session=False,
            )
        )
        if updated == 1:
            if append_event(
                db, run.id, "cancelled", {"reason": "user"},
                require_status="cancelled",
            ) is None:
                db.rollback()
                db.expire_all()
                current_run = (
                    db.query(AgentRun).filter(AgentRun.id == run.id).first()
                    or run
                )
                return {
                    **snapshot(current_run),
                    "cancelled": False,
                    "note": "取消写入失败，请重试。",
                }
            append_event(
                db, run.id, "done", {"status": "cancelled"},
                require_status="cancelled",
            )
            db.commit()
            db.expire_all()
            current_run = (
                db.query(AgentRun).filter(AgentRun.id == run.id).first()
                or run
            )
            return {
                **snapshot(current_run),
                "cancelled": True,
                "note": "已取消排队中的回合。",
            }

        # The queued CAS lost a queued->running claim (or another writer won).
        # Re-read under owner isolation and request cancellation on the
        # running run instead of dropping the request or claiming it ended.
        db.rollback()
        refreshed = require_run(db, ontology_id, current_user, run_id)
        if refreshed.status == "running":
            return _request_running_cancel(db, refreshed, current)
        db.expire_all()
        current_run = (
            db.query(AgentRun).filter(AgentRun.id == refreshed.id).first()
            or refreshed
        )
        return {
            **snapshot(current_run),
            "cancelled": False,
            "note": (
                "该回合已结束，无需取消。"
                if current_run.status in TERMINAL_RUN_STATUSES
                else "取消未生效，请重试。"
            ),
        }

    if run.status == "running":
        return _request_running_cancel(db, run, current)

    return {
        **snapshot(run),
        "cancelled": False,
        "note": "该回合已结束，无需取消。",
    }


def retry_run(
    db: Session,
    ontology_id: str,
    current_user: Any,
    run_id: str,
    *,
    dispatch: bool = True,
    dispatch_run: DispatchRun | None = None,
) -> dict:
    """Create a new run for a terminal run's original user turn."""
    source = require_run(db, ontology_id, current_user, run_id)
    caller_id = current_user_id(current_user)
    if source.user_id != caller_id:
        # Admin can observe another user's run, but retrying would create a
        # run whose owner no longer matches the conversation owner.
        raise forbidden("只有回合所有者可以重试该回合")
    if source.status not in TERMINAL_RUN_STATUSES:
        raise conflict({
            "code": "run_still_active",
            "message": "回合尚未结束，不能重试。",
        })
    user_message = db.get(AgentMessage, source.user_message_id)
    if user_message is None:
        raise conflict({
            "code": "run_user_message_missing",
            "message": "原回合的用户消息已不存在，无法重试。",
        })

    user_id = current_user_id(current_user)
    new_run_id = str(uuid.uuid4())
    request_hash_value = request_hash(
        ontology_id=source.ontology_id,
        user_id=user_id,
        question=source.question,
        conversation_id=source.conversation_id,
        model_id=source.model_id,
        release_id=source.release_id,
        stream_model=bool(source.stream_model),
    )

    with submission_lock(db):
        enforce_capacity(db, user_id)
        try:
            run = AgentRun(
                id=new_run_id,
                ontology_id=source.ontology_id,
                release_id=source.release_id,
                conversation_id=source.conversation_id,
                user_id=user_id,
                user_message_id=source.user_message_id,
                model_id=source.model_id,
                model=source.model,
                status="queued",
                question=source.question,
                request_hash=request_hash_value,
                content="",
                steps=[],
                citations=[],
                proposals=[],
                stream_model=bool(source.stream_model),
                retry_of=source.id,
            )
            db.add(run)
            db.flush()
            if append_event(
                db,
                run.id,
                "meta",
                {
                    "runId": run.id,
                    "ontologyId": source.ontology_id,
                    "releaseId": source.release_id,
                    "conversationId": source.conversation_id,
                    "status": "queued",
                    "model": source.model_id,
                    "retryOf": source.id,
                },
                require_status="queued",
            ) is None:
                db.rollback()
                raise conflict("回合事件初始化失败")
            db.commit()
        except IntegrityError as error:
            db.rollback()
            raise conflict({
                "code": "conversation_busy",
                "message": "该会话已有正在执行或排队中的回合。",
            }) from error
        except Exception:
            db.rollback()
            raise

    if dispatch:
        dispatch_after_commit(run.id, dispatch_run)
        db.expire_all()
        refreshed = db.query(AgentRun).filter(AgentRun.id == run.id).first()
        if refreshed is not None:
            return snapshot(refreshed)
    db.expire_all()
    refreshed = db.query(AgentRun).filter(AgentRun.id == run.id).first()
    return snapshot(refreshed or run)


def delete_conversation_runs(db: Session, conversation_id: str) -> None:
    """Delete run-owned records after rejecting an active conversation.

    Called by the established conversation deletion workflow before its own
    records; this function does not commit so the caller keeps one transaction.
    """
    active = (
        db.query(AgentRun.id)
        .filter(
            AgentRun.conversation_id == conversation_id,
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .first()
    )
    if active is not None:
        raise conflict(
            "会话有正在执行或排队中的回合，请先取消或等待结束后再删除"
        )
    run_ids = [
        row[0]
        for row in (
            db.query(AgentRun.id)
            .filter(AgentRun.conversation_id == conversation_id)
            .all()
        )
    ]
    if not run_ids:
        return
    db.query(AgentRunEvent).filter(
        AgentRunEvent.run_id.in_(run_ids),
    ).delete(synchronize_session=False)
    db.query(AgentRun).filter(
        AgentRun.id.in_(run_ids),
    ).delete(synchronize_session=False)
