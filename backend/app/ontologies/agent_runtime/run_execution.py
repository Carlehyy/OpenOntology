"""Claim, lease, execute and finish durable agent runs."""
from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.user import User
from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.models import AgentConversation, AgentMessage
from app.ontologies.agent_runtime.run_common import now as utc_now
from app.ontologies.agent_runtime.run_events import (
    DeltaAccumulator,
    append_event,
    bounded_step_event,
)
from app.ontologies.agent_runtime.run_models import (
    TERMINAL_RUN_STATUSES,
    AgentRun,
)
from app.ontologies.agent_runtime.turn_contracts import TurnExecution

logger = logging.getLogger(__name__)


def _claim_run(session: Session, run_id: str) -> str | None:
    state = (
        session.query(AgentRun.status)
        .filter(AgentRun.id == run_id)
        .first()
    )
    if state is None or state[0] != "queued":
        session.rollback()
        return None
    owner = uuid.uuid4().hex
    current = utc_now()
    lease_expires = current + timedelta(seconds=limit("run_lease_seconds"))
    updated = (
        session.query(AgentRun)
        .filter(AgentRun.id == run_id, AgentRun.status == "queued")
        .update(
            {
                "status": "running",
                "lease_owner": owner,
                "lease_expires_at": lease_expires,
                "started_at": current,
                "cancel_requested_at": None,
                "error": None,
                "updated_at": current,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        session.rollback()
        return None
    session.commit()
    return owner


def _is_cancelled(
    factory: Callable[[], Session],
    run_id: str,
    owner: str,
) -> bool:
    """Cancellation/fencing check; fail closed when the DB cannot answer.

    A worker stops when the run is no longer running, when another worker owns
    the lease, or when a cancel was requested.  A failed query is treated as
    a stop signal: continuing would risk writing after the run left this
    worker's ownership.
    """
    session = factory()
    try:
        row = (
            session.query(
                AgentRun.status,
                AgentRun.cancel_requested_at,
                AgentRun.lease_owner,
                AgentRun.lease_expires_at,
            )
            .filter(AgentRun.id == run_id)
            .first()
        )
        if row is None:
            return True
        status, cancel_requested_at, lease_owner, lease_expires_at = row
        if status != "running":
            return True
        if lease_owner != owner:
            return True
        if lease_expires_at is None:
            return True
        if lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
        if lease_expires_at <= utc_now():
            return True
        return cancel_requested_at is not None
    except Exception:  # noqa: BLE001 — fail closed, never continue unowned
        logger.exception(
            "本体助手回合取消/租约状态查询失败，按取消处理 run=%s", run_id)
        return True
    finally:
        session.close()


class _LeaseHeartbeat(threading.Thread):
    def __init__(
        self,
        factory: Callable[[], Session],
        run_id: str,
        owner: str,
        lease_seconds: int,
        interval_seconds: int,
    ) -> None:
        super().__init__(
            name=f"agent-run-lease-{run_id[:8]}",
            daemon=True,
        )
        self._factory = factory
        self._run_id = run_id
        self._owner = owner
        self._lease_seconds = max(1, int(lease_seconds))
        self._interval_seconds = max(1, int(interval_seconds))
        self._stop_event = threading.Event()

    def run(self) -> None:
        interval = max(1, min(self._interval_seconds, self._lease_seconds // 3))
        while not self._stop_event.wait(interval):
            session = self._factory()
            try:
                current = utc_now()
                updated = (
                    session.query(AgentRun)
                    .filter(
                        AgentRun.id == self._run_id,
                        AgentRun.status == "running",
                        AgentRun.lease_owner == self._owner,
                        AgentRun.lease_expires_at.is_not(None),
                        AgentRun.lease_expires_at > current,
                    )
                    .update(
                        {
                            "lease_expires_at": current + timedelta(
                                seconds=self._lease_seconds),
                            "updated_at": current,
                        },
                        synchronize_session=False,
                    )
                )
                session.commit()
                if updated != 1:
                    return
            except Exception:  # noqa: BLE001 — retry on the next interval
                logger.exception(
                    "本体助手回合租约续约失败，等待下一次续约 run=%s",
                    self._run_id,
                )
            finally:
                session.close()

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2)


def _owned_running_clause(
    run_id: str,
    owner: str,
    current: datetime,
) -> tuple:
    """CAS guard for every worker write: running + same owner + unexpired lease."""
    return (
        AgentRun.id == run_id,
        AgentRun.status == "running",
        AgentRun.lease_owner == owner,
        AgentRun.lease_expires_at.is_not(None),
        AgentRun.lease_expires_at > current,
    )


def _flush_delta(
    session: Session,
    run_id: str,
    owner: str,
    delta: DeltaAccumulator,
) -> bool:
    taken = delta.take()
    if taken is None:
        return True
    content, reset = taken
    current = utc_now()
    row = (
        session.query(AgentRun.content)
        .filter(*_owned_running_clause(run_id, owner, current))
        .first()
    )
    if row is None:
        session.rollback()
        return False
    new_content = content if reset else (row[0] or "") + content
    updated = (
        session.query(AgentRun)
        .filter(*_owned_running_clause(run_id, owner, current))
        .update(
            {"content": new_content, "updated_at": current},
            synchronize_session=False,
        )
    )
    if updated != 1:
        session.rollback()
        return False
    payload = {"content": content}
    if reset:
        payload["reset"] = True
    if append_event(
        session, run_id, "delta", payload, require_status="running",
    ) is None:
        session.rollback()
        return False
    session.commit()
    return True


def _record_step(
    session: Session,
    run_id: str,
    owner: str,
    steps: list[dict],
) -> bool:
    current = utc_now()
    updated = (
        session.query(AgentRun)
        .filter(*_owned_running_clause(run_id, owner, current))
        .update(
            {"steps": steps, "updated_at": current},
            synchronize_session=False,
        )
    )
    if updated != 1:
        session.rollback()
        return False
    if append_event(
        session, run_id, "step", steps[-1], require_status="running",
    ) is None:
        session.rollback()
        return False
    session.commit()
    return True


def _record_meta(session: Session, run_id: str, owner: str, event: dict) -> bool:
    payload = {
        key: event.get(key)
        for key in ("conversationId", "model", "releaseId")
        if event.get(key) is not None
    }
    current = utc_now()
    values: dict[str, Any] = {"updated_at": current}
    if event.get("model") is not None:
        values["model"] = str(event["model"])
    updated = (
        session.query(AgentRun)
        .filter(*_owned_running_clause(run_id, owner, current))
        .update(values, synchronize_session=False)
    )
    if updated != 1:
        session.rollback()
        return False
    if append_event(
        session, run_id, "meta", payload, require_status="running",
    ) is None:
        session.rollback()
        return False
    session.commit()
    return True


def _drain_delta(
    session: Session,
    run_id: str,
    owner: str,
    delta: DeltaAccumulator,
) -> tuple[str, bool, str] | None:
    """Take the unflushed delta and compute its post-merge projection content.

    Used only on terminal transitions: the merged content and the delta event
    are committed together with the assistant message and run terminal state.
    The read is owner/lease fenced; an expired or stolen lease yields nothing
    and the following terminal CAS will fail.
    """
    taken = delta.take()
    if taken is None:
        return None
    content, reset = taken
    current = utc_now()
    row = (
        session.query(AgentRun.content)
        .filter(*_owned_running_clause(run_id, owner, current))
        .first()
    )
    if row is None:
        return None
    merged = content if reset else (row[0] or "") + content
    return content, reset, merged


def _finish_run(
    session: Session,
    run_id: str,
    owner: str,
    *,
    status: str,
    error: str | None,
    event_type: str,
    event_payload: dict,
    content: str | None = None,
    citations: list | None = None,
    proposals: list | None = None,
    usage: dict | None = None,
    steps: list[dict] | None = None,
    delta_event: tuple[str, bool] | None = None,
) -> bool:
    """Transition running -> terminal exactly once, with terminal + done events.

    The core orchestrator adds the assistant ``AgentMessage`` to this same
    session without committing when it runs under ``TurnExecution``.  The
    explicit flush below puts that message in the terminal transaction; if the
    run CAS loses (cancelled/interrupted by another writer), the rollback also
    discards the late assistant message.

    For success/error terminals the CAS additionally requires
    ``cancel_requested_at IS NULL``.  If a cancel request landed between the
    core's final cancellation check and its answer/error yield, the CAS fails,
    this transaction rolls back the pending assistant, and
    ``_commit_cancel_after_race`` commits the cancelled terminal if the worker
    still owns a live lease.
    """
    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"invalid terminal status: {status}")
    try:
        session.flush()
    except Exception:  # noqa: BLE001 — pending assistant must not outlive the run
        logger.exception("本体助手终态前刷新待提交消息失败 run=%s", run_id)
        session.rollback()
        return False

    current = utc_now()
    values: dict[str, Any] = {
        "status": status,
        "finished_at": current,
        "updated_at": current,
        "lease_owner": None,
        "lease_expires_at": None,
        "error": error,
    }
    if content is not None:
        values["content"] = content
    if citations is not None:
        values["citations"] = citations
    if proposals is not None:
        values["proposals"] = proposals
    if usage is not None:
        values["usage"] = usage
    if steps is not None:
        values["steps"] = steps

    cancel_race = status in ("succeeded", "error")
    query = session.query(AgentRun).filter(
        AgentRun.id == run_id,
        AgentRun.status == "running",
        AgentRun.lease_owner == owner,
        AgentRun.lease_expires_at.is_not(None),
        AgentRun.lease_expires_at > current,
    )
    if cancel_race:
        query = query.filter(AgentRun.cancel_requested_at.is_(None))
    updated = query.update(values, synchronize_session=False)
    if updated != 1:
        session.rollback()
        if cancel_race:
            return _commit_cancel_after_race(
                session,
                run_id,
                owner,
                steps=steps,
                content=content if status == "error" else None,
                delta_event=delta_event if status == "error" else None,
            )
        return False
    if delta_event is not None:
        chunk, reset = delta_event
        payload = {"content": chunk}
        if reset:
            payload["reset"] = True
        if append_event(
            session, run_id, "delta", payload, require_status=status,
        ) is None:
            session.rollback()
            return False
    if append_event(
        session, run_id, event_type, event_payload, require_status=status,
    ) is None:
        session.rollback()
        return False
    if append_event(
        session, run_id, "done", {"status": status}, require_status=status,
    ) is None:
        session.rollback()
        return False
    session.commit()
    return True


def _commit_cancel_after_race(
    session: Session,
    run_id: str,
    owner: str,
    *,
    steps: list[dict] | None,
    content: str | None,
    delta_event: tuple[str, bool] | None,
) -> bool:
    """Cancel branch after a success/error CAS lost to a cancel request.

    The failed terminal transaction was rolled back (including the core's
    pending final-answer assistant message).  If this worker still owns a live
    lease and the run is still running, commit a cancelled terminal with a
    fresh ``[已取消]`` assistant message.  Otherwise leave the run to its new
    owner/reconcile.
    """
    row = (
        session.query(
            AgentRun.status,
            AgentRun.lease_owner,
            AgentRun.lease_expires_at,
            AgentRun.cancel_requested_at,
            AgentRun.conversation_id,
            AgentRun.model,
        )
        .filter(AgentRun.id == run_id)
        .first()
    )
    if row is None:
        return False
    (
        status,
        lease_owner,
        lease_expires_at,
        cancel_requested_at,
        conversation_id,
        model,
    ) = row
    current = utc_now()
    if status != "running" or lease_owner != owner or lease_expires_at is None:
        return False
    if lease_expires_at.tzinfo is None:
        lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
    if lease_expires_at <= current or cancel_requested_at is None:
        return False

    session.add(AgentMessage(
        conversation_id=conversation_id,
        role="assistant",
        content="[已取消]",
        steps=steps or [],
        model=model,
    ))
    values: dict[str, Any] = {
        "status": "cancelled",
        "finished_at": current,
        "updated_at": current,
        "lease_owner": None,
        "lease_expires_at": None,
        "error": None,
        "steps": steps or [],
    }
    if content is not None:
        values["content"] = content
    updated = (
        session.query(AgentRun)
        .filter(*_owned_running_clause(run_id, owner, current))
        .update(values, synchronize_session=False)
    )
    if updated != 1:
        session.rollback()
        return False
    if delta_event is not None:
        chunk, reset = delta_event
        payload = {"content": chunk}
        if reset:
            payload["reset"] = True
        if append_event(
            session, run_id, "delta", payload, require_status="cancelled",
        ) is None:
            session.rollback()
            return False
    if append_event(
        session, run_id, "cancelled", {"reason": "user"},
        require_status="cancelled",
    ) is None:
        session.rollback()
        return False
    if append_event(
        session, run_id, "done", {"status": "cancelled"},
        require_status="cancelled",
    ) is None:
        session.rollback()
        return False
    session.commit()
    return True


def _consume_run_events(
    session: Session,
    run: AgentRun,
    owner: str,
    events: Iterator[dict],
    *,
    is_cancelled: Callable[[], bool],
) -> None:
    run_id = run.id
    delta = DeltaAccumulator()
    steps = list(run.steps or [])

    try:
        for event in events:
            event_type = event.get("type")

            # Terminal events are consumed even after a cancel request: the
            # core may have queued its assistant message, and _finish_run must
            # commit (or roll back) that message together with the run state.
            if event_type == "answer":
                content = event.get("content")
                if not isinstance(content, str) or content == "":
                    pending = _drain_delta(session, run_id, owner, delta)
                    if pending is not None:
                        content = pending[2]
                    else:
                        current = (
                            session.query(AgentRun.content)
                            .filter(AgentRun.id == run_id)
                            .scalar()
                        )
                        content = current or ""
                _finish_run(
                    session, run_id, owner,
                    status="succeeded",
                    error=None,
                    event_type="answer",
                    event_payload={
                        "content": content,
                        "citations": event.get("citations") or [],
                        "proposals": event.get("proposals") or [],
                        "usage": event.get("usage"),
                        "verification": event.get("verification"),
                    },
                    content=content,
                    citations=event.get("citations") or [],
                    proposals=event.get("proposals") or [],
                    usage=event.get("usage"),
                    steps=steps,
                )
                return
            if event_type == "error":
                message = str(event.get("message") or "智能体执行失败")
                pending = _drain_delta(session, run_id, owner, delta)
                _finish_run(
                    session, run_id, owner,
                    status="error",
                    error=message,
                    event_type="error",
                    event_payload={"message": message},
                    steps=steps,
                    content=pending[2] if pending is not None else None,
                    delta_event=(
                        (pending[0], pending[1])
                        if pending is not None else None
                    ),
                )
                return
            if event_type == "cancelled":
                pending = _drain_delta(session, run_id, owner, delta)
                _finish_run(
                    session, run_id, owner,
                    status="cancelled",
                    error=None,
                    event_type="cancelled",
                    event_payload={"reason": event.get("reason") or "agent"},
                    steps=steps,
                    content=pending[2] if pending is not None else None,
                    delta_event=(
                        (pending[0], pending[1])
                        if pending is not None else None
                    ),
                )
                return

            if is_cancelled():
                pending = _drain_delta(session, run_id, owner, delta)
                _finish_run(
                    session, run_id, owner,
                    status="cancelled",
                    error=None,
                    event_type="cancelled",
                    event_payload={"reason": "user"},
                    steps=steps,
                    content=pending[2] if pending is not None else None,
                    delta_event=(
                        (pending[0], pending[1])
                        if pending is not None else None
                    ),
                )
                return

            if event_type == "delta":
                due = delta.add(
                    str(event.get("content") or ""),
                    reset=bool(event.get("reset")),
                )
                if due and not _flush_delta(session, run_id, owner, delta):
                    return
            elif event_type == "step":
                if not _flush_delta(session, run_id, owner, delta):
                    return
                step = bounded_step_event(event)
                steps.append(step)
                if not _record_step(session, run_id, owner, steps):
                    return
            elif event_type == "meta":
                if not _flush_delta(session, run_id, owner, delta):
                    return
                if not _record_meta(session, run_id, owner, event):
                    return
            elif event_type == "done":
                # The worker owns the terminal transition; a bare core ``done``
                # without answer/error/cancelled is incomplete.
                continue
            else:
                logger.warning("忽略未知 run 事件类型: %s", event_type)

        # Generator ended without a terminal event.  Discard any unexpected
        # pending assistant first; only the run/error ledger may commit.
        pending = _drain_delta(session, run_id, owner, delta)
        session.rollback()
        message = "智能体未返回有效回答"
        _finish_run(
            session, run_id, owner,
            status="error",
            error=message,
            event_type="error",
            event_payload={"message": message},
            steps=steps,
            content=pending[2] if pending is not None else None,
            delta_event=(
                (pending[0], pending[1]) if pending is not None else None
            ),
        )
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 — cleanup only
                logger.exception("关闭回合事件生成器失败 run=%s", run_id)


def _revalidate_execution_access(
    session: Session,
    user: User,
    ontology_id: str,
) -> str | None:
    """Re-check authorization after submit; a revoked user must not keep reading.

    The HTTP layer checked access when the run was submitted, but dispatch may
    execute seconds or minutes later.  Inactive users, menu revocation, or an
    ontology that disappeared must terminate the run instead of executing it.
    """
    if not getattr(user, "is_active", True):
        return "用户已停用，回合终止执行。"
    from app.auth.permissions import user_has_menu_access

    if not user_has_menu_access(session, user, "agent"):
        return "用户已失去智能体访问权限，回合终止执行。"
    from app.ontologies.access import require_ontology_access

    try:
        require_ontology_access(session, ontology_id, user, write=False)
    except Exception:  # noqa: BLE001 — includes HTTP 403/404 from access layer
        logger.warning(
            "本体助手执行前访问复核失败 user=%s ontology=%s",
            getattr(user, "id", None), ontology_id,
            exc_info=True,
        )
        return "用户已失去本体访问权限，回合终止执行。"
    return None


def execute_run(
    run_id: str,
    *,
    db: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
    run_turn_fn: Callable[..., Iterator[dict]] | None = None,
    stream_model: bool = True,
) -> None:
    """NATS/executor entrypoint: claim the queued run and execute one turn.

    ``stream_model`` defaults to the worker's streaming mode; the legacy
    synchronous request path passes ``False`` and also captures
    ``stream=false`` on the run itself, so both the submit flag and the caller
    hint must allow streaming before the orchestrator opens a model stream.
    """
    factory = session_factory or SessionLocal
    owns_session = db is None
    session = db if db is not None else factory()
    heartbeat: _LeaseHeartbeat | None = None
    try:
        owner = _claim_run(session, run_id)
        if owner is None:
            return
        session.expire_all()
        run = session.get(AgentRun, run_id)
        if run is None:
            return
        user = session.get(User, run.user_id)
        conversation = session.get(AgentConversation, run.conversation_id)
        user_message = session.get(AgentMessage, run.user_message_id)
        missing = None
        if user is None:
            missing = "提问用户不存在"
        elif conversation is None:
            missing = "回合所属会话不存在"
        elif user_message is None:
            missing = "回合对应的用户消息不存在"
        if missing is not None:
            _finish_run(
                session, run_id, owner,
                status="error",
                error=missing,
                event_type="error",
                event_payload={"message": missing},
            )
            return

        access_error = _revalidate_execution_access(
            session, user, run.ontology_id,
        )
        if access_error is not None:
            _finish_run(
                session, run_id, owner,
                status="error",
                error=access_error,
                event_type="error",
                event_payload={"message": access_error},
            )
            return

        heartbeat = _LeaseHeartbeat(
            factory,
            run.id,
            owner,
            limit("run_lease_seconds"),
            limit("run_heartbeat_seconds"),
        )
        heartbeat.start()

        execution = TurnExecution(
            user_message_id=run.user_message_id,
            is_cancelled=lambda: _is_cancelled(factory, run_id, owner),
            stream_model=bool(stream_model) and bool(run.stream_model),
        )
        turn_fn = run_turn_fn
        if turn_fn is None:
            from app.ontologies.agent_runtime.orchestrator import run_agent_turn

            turn_fn = run_agent_turn

        try:
            events = turn_fn(
                session,
                run.ontology_id,
                user,
                run.question,
                conversation_id=run.conversation_id,
                model_id=run.model_id,
                release_id=run.release_id,
                run_id=run.id,
                execution=execution,
            )
            _consume_run_events(
                session, run, owner, events,
                is_cancelled=execution.is_cancelled,
            )
        except Exception as error:  # noqa: BLE001 — persist terminal error event
            logger.exception("本体助手回合执行失败 run=%s", run_id)
            session.rollback()
            message = f"智能体执行失败: {error}"
            _finish_run(
                session, run_id, owner,
                status="error",
                error=message,
                event_type="error",
                event_payload={"message": message},
            )
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        if owns_session:
            session.close()
