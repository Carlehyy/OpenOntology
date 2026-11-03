"""Shared primitives for the durable agent-run application modules.

Submission, execution and event streaming each import these helpers; none of
them import ``run_service`` so the public facade stays acyclic.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.ontologies.agent_runtime.application_errors import (
    conflict,
    forbidden,
    invalid,
    not_found,
)
from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.models import AgentConversation
from app.ontologies.agent_runtime.run_models import (
    ACTIVE_RUN_STATUSES,
    AgentRun,
)

logger = logging.getLogger(__name__)

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PG_ADVISORY_LOCK_KEY = 0x4F4E5450  # "ONTP"; serializes submit capacity checks.
_SQLITE_SUBMIT_LOCK = threading.Lock()

DispatchRun = Callable[[str], Any]
_dispatch_port: DispatchRun | None = None


class DispatchUnavailable(RuntimeError):
    """No dispatch port was installed by the shared runtime composition."""


def set_dispatch_run_port(port: DispatchRun | None) -> None:
    """Install the process-wide dispatch port (shared runtime composition)."""
    global _dispatch_port
    _dispatch_port = port


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def snapshot(run: AgentRun) -> dict:
    return {
        "runId": run.id,
        "ontologyId": run.ontology_id,
        "releaseId": run.release_id,
        "conversationId": run.conversation_id,
        "status": run.status,
        "startedAt": iso(run.started_at),
        "finishedAt": iso(run.finished_at),
        "cancelRequestedAt": iso(run.cancel_requested_at),
        "question": run.question,
        "content": run.content or "",
        "steps": run.steps or [],
        "citations": run.citations or [],
        "proposals": run.proposals or [],
        "usage": run.usage,
        "lastSeq": run.last_seq,
        "error": run.error,
        "retryOf": run.retry_of,
        "model": run.model,
    }


def normalize_run_id(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if not _RUN_ID_PATTERN.fullmatch(value):
        raise invalid(
            "runId 格式非法：只允许 1-128 位字母、数字及 . _ : -"
        )
    return value


def request_hash(
    *,
    ontology_id: str,
    user_id: str,
    question: str,
    conversation_id: str | None,
    model_id: str | None,
    release_id: str | None,
    stream_model: bool,
) -> str:
    payload = {
        "ontologyId": ontology_id,
        "userId": user_id,
        "question": question,
        "conversationId": conversation_id,
        "modelId": model_id,
        "releaseId": release_id,
        "streamModel": bool(stream_model),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def current_user_id(current_user: Any) -> str:
    user_id = str(getattr(current_user, "id", "") or "")
    if not user_id:
        raise forbidden("用户上下文缺失")
    return user_id


@contextmanager
def submission_lock(db: Session):
    """Serialize capacity checks and run insertion across API processes.

    PostgreSQL uses a transaction-scoped advisory lock; SQLite (tests and the
    supported local profile) uses a process lock.  The partial unique index
    remains the final single-flight guard even if the lock is bypassed.
    """
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _PG_ADVISORY_LOCK_KEY},
        )
        yield
    else:
        with _SQLITE_SUBMIT_LOCK:
            yield


def enforce_capacity(db: Session, user_id: str) -> None:
    user_active = (
        db.query(func.count(AgentRun.id))
        .filter(
            AgentRun.user_id == user_id,
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .scalar()
    ) or 0
    user_limit = limit("run_user_cap")
    if user_active >= user_limit:
        raise conflict({
            "code": "run_capacity_exceeded",
            "scope": "user",
            "limit": user_limit,
            "active": int(user_active),
            "message": f"你同时最多有 {user_limit} 个执行中的回合，请等待结束或取消。",
        })

    global_active = (
        db.query(func.count(AgentRun.id))
        .filter(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
        .scalar()
    ) or 0
    global_limit = limit("run_global_cap")
    if global_active >= global_limit:
        raise conflict({
            "code": "run_capacity_exceeded",
            "scope": "global",
            "limit": global_limit,
            "active": int(global_active),
            "message": "平台当前执行中的回合已达上限，请稍后重试。",
        })

    queued = (
        db.query(func.count(AgentRun.id))
        .filter(
            AgentRun.user_id == user_id,
            AgentRun.status == "queued",
        )
        .scalar()
    ) or 0
    queue_limit = limit("run_queue_cap")
    if queued >= queue_limit:
        raise conflict({
            "code": "run_capacity_exceeded",
            "scope": "queue",
            "limit": queue_limit,
            "active": int(queued),
            "message": f"排队中的回合已达上限（{queue_limit}），请稍后重试。",
        })


def require_run(
    db: Session,
    ontology_id: str,
    current_user: Any,
    run_id: str,
) -> AgentRun:
    run = (
        db.query(AgentRun)
        .filter(AgentRun.id == run_id, AgentRun.ontology_id == ontology_id)
        .first()
    )
    if run is None:
        raise not_found("回合不存在")
    if (
        run.user_id != str(getattr(current_user, "id", "") or "")
        and getattr(current_user, "role", "") != "admin"
    ):
        raise forbidden("无权访问他人回合")
    return run


def existing_run_result(
    existing: AgentRun,
    *,
    request_hash_value: str,
    user_id: str,
    ontology_id: str,
) -> dict:
    if existing.user_id != user_id:
        raise conflict({
            "code": "run_id_owner_conflict",
            "message": "runId 已被其他用户占用，请更换 runId。",
        })
    if existing.ontology_id != ontology_id:
        raise conflict({
            "code": "run_id_ontology_conflict",
            "message": "runId 已用于其他本体，请更换 runId。",
        })
    if existing.request_hash != request_hash_value:
        raise conflict({
            "code": "run_id_payload_conflict",
            "message": "相同 runId 的提交内容与首次提交不一致。",
        })
    return snapshot(existing)


def resolve_conversation(
    db: Session,
    *,
    ontology_id: str,
    user_id: str,
    release_id: str | None,
    conversation_id: str | None,
    question: str,
) -> AgentConversation:
    if conversation_id:
        conversation = (
            db.query(AgentConversation)
            .filter(
                AgentConversation.id == conversation_id,
                AgentConversation.ontology_id == ontology_id,
            )
            .first()
        )
        if conversation is None:
            raise not_found("会话不存在")
        if conversation.user_id != user_id:
            raise forbidden("无权访问他人会话")
        if conversation.ontology_release_id != release_id:
            raise conflict({
                "code": "conversation_release_mismatch",
                "message": "会话发布版本与本次回合冻结的版本不一致，请新建会话。",
            })
        return conversation

    conversation = AgentConversation(
        ontology_id=ontology_id,
        ontology_release_id=release_id,
        user_id=user_id,
        title=question.strip()[:60] or "新对话",
    )
    db.add(conversation)
    db.flush()
    return conversation


def resolve_dispatch_port(dispatch_run: DispatchRun | None) -> DispatchRun:
    if dispatch_run is not None:
        return dispatch_run
    if _dispatch_port is not None:
        return _dispatch_port
    raise DispatchUnavailable(
        "dispatch_unavailable: install_runtime_ports() did not register "
        "set_dispatch_run_port"
    )


def dispatch_after_commit(
    run_id: str,
    dispatch_run: DispatchRun | None,
) -> bool:
    try:
        resolve_dispatch_port(dispatch_run)(run_id)
        return True
    except DispatchUnavailable:
        logger.error(
            "本体助手派发端口未安装（dispatch_unavailable），"
            "run 保持 queued 等待对账重派 run=%s",
            run_id,
        )
        return False
    except Exception:  # noqa: BLE001 — persisted run stays queued for reconcile
        logger.exception("本体助手回合派发失败，保持 queued 等待对账重派 run=%s", run_id)
        return False
