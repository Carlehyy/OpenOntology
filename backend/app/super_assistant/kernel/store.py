"""Kernel 事实写入边界。

所有改变 Run 执行事实的入口都经过这里：先锁 Run，再做幂等/版本检查，随后
在同一事务追加事件和 dispatch outbox。该模块不启动 Worker，也不包含模型调用。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .contracts import (
    CancelReason,
    ContractError,
    RunStatus,
    request_cancel,
)
from .events import EventEnvelope, validate_payload
from .models import (
    ExecutionCommand,
    ExecutionDispatchOutbox,
    ExecutionEvent,
    ExecutionRun,
)
from ..models import SuperAssistantConversation


UTC = timezone.utc
RUN_DEADLINE = timedelta(hours=24)
OUTBOX_SUBJECT_PREFIX = "sa.execution.run."


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _new_id() -> str:
    return str(uuid.uuid4())


class IdempotencyConflict(ContractError):
    """同一幂等键提交了不同 payload。"""


class VersionConflict(ContractError):
    """写入方持有过期的 Run version 或 lease。"""


@dataclass(frozen=True, slots=True)
class LeaseToken:
    run_id: str
    owner: str
    epoch: int
    expires_at: datetime


def _lock_run(db: Session, run_id: str) -> ExecutionRun:
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
    if run is None:
        raise KeyError(run_id)
    return run


def _ensure_owner(run: ExecutionRun, owner_id: str) -> None:
    if run.owner_id != owner_id:
        raise KeyError(run.id)


def create_run(
    db: Session,
    *,
    owner_id: str,
    conversation_id: str,
    goal: str,
    idempotency_key: str,
    deadline: datetime | None = None,
    parent_run_id: str | None = None,
    join_policy: str = "all",
    binding: dict | None = None,
) -> tuple[ExecutionRun, bool]:
    """创建 queued Run 并在同一事务写入 created 事件和 outbox。

    返回 ``(run, replayed)``；调用方负责 commit/rollback。Conversation 行锁
    保证同一用户/会话的创建幂等竞态在 PostgreSQL 上只有一个胜者。
    """
    if not goal.strip():
        raise ContractError("goal cannot be empty")
    if join_policy not in {"all", "any"}:
        raise ContractError("join_policy must be all or any")
    conv = db.scalar(
        select(SuperAssistantConversation)
        .where(SuperAssistantConversation.id == conversation_id, SuperAssistantConversation.owner_id == owner_id)
        .with_for_update()
    )
    if conv is None:
        raise KeyError(conversation_id)
    payload = {
        "goal": goal,
        "deadline": deadline.isoformat() if deadline else None,
        "parent_run_id": parent_run_id,
        "join_policy": join_policy,
        "binding": binding or {},
    }
    payload_hash = _hash_payload(payload)
    existing = db.scalar(select(ExecutionRun).where(
        ExecutionRun.owner_id == owner_id,
        ExecutionRun.conversation_id == conversation_id,
        ExecutionRun.idempotency_key == idempotency_key,
    ).with_for_update())
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise IdempotencyConflict("idempotency_conflict")
        return existing, True
    if parent_run_id:
        parent = _lock_run(db, parent_run_id)
        if parent.owner_id != owner_id or parent.conversation_id != conversation_id:
            raise ContractError("parent Run must share owner and conversation")
        if parent.id == parent_run_id and parent.status in {RunStatus.CANCELLED.value, RunStatus.EXPIRED.value}:
            raise ContractError("cannot bind child to cancelled or expired parent")
    binding = binding or {}
    mode = binding.get("binding_mode", "direct_ui")
    if mode == "delegated":
        required = {"ontology_id", "draft_version_id", "lifecycle", "write_permission"}
        if required - binding.keys() or binding.get("lifecycle") != "editing" or binding.get("write_permission") is not True:
            raise ContractError("delegated binding requires editing draft and write permission")
    elif mode not in {"direct_ui", "legacy"}:
        raise ContractError("unknown binding_mode")
    run = ExecutionRun(
        id=_new_id(), owner_id=owner_id, conversation_id=conversation_id,
        parent_run_id=parent_run_id, execution_version="kernel.v1", status="queued",
        goal=goal, deadline=deadline or (_now() + RUN_DEADLINE),
        idempotency_key=idempotency_key, payload_hash=payload_hash,
        binding_mode=mode, binding_snapshot_ref=json.dumps(binding, sort_keys=True),
        ontology_id=binding.get("ontology_id"), draft_version_id=binding.get("draft_version_id"),
        join_policy=join_policy,
    )
    db.add(run)
    db.flush()
    command_id = _new_id()
    append_event(
        db, run, event_type="run.created",
        payload={"execution_version": "kernel.v1", "conversation_id": conversation_id},
        actor={"kind": "user"}, command_id=command_id, idempotency_key=idempotency_key,
    )
    _add_outbox(db, run, command_id=command_id, message_ref=f"run://{run.id}")
    return run, False


def append_event(
    db: Session,
    run: ExecutionRun,
    *,
    event_type: str,
    payload: dict,
    actor: dict[str, str],
    command_id: str,
    idempotency_key: str,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    redaction: dict | None = None,
    lease: LeaseToken | None = None,
) -> ExecutionEvent:
    """追加一个事件；调用方必须已锁定 Run。seq 回滚不消耗可见序号。"""
    if lease is not None:
        assert_lease(run, lease)
    validate_payload(event_type, payload)
    seq = run.next_event_seq
    envelope = EventEnvelope(
        event_id=_new_id(), run_id=run.id, seq=seq, event_type=event_type,
        schema_version=1, occurred_at=_now(), actor=actor,
        causation_id=causation_id or command_id, correlation_id=correlation_id or run.id,
        command_id=command_id, idempotency_key=idempotency_key, payload=payload,
        redaction=redaction or {"mode": "none"},
    )
    envelope.validate()
    event = ExecutionEvent(
        event_id=envelope.event_id, run_id=run.id, seq=seq, event_type=event_type,
        schema_version=1, occurred_at=envelope.occurred_at, actor=actor,
        causation_id=envelope.causation_id, correlation_id=envelope.correlation_id,
        command_id=command_id, idempotency_key=idempotency_key, payload=payload,
        redaction=envelope.redaction,
    )
    db.add(event)
    run.next_event_seq += 1
    return event


def _add_outbox(db: Session, run: ExecutionRun, *, command_id: str, message_ref: str) -> ExecutionDispatchOutbox:
    row = ExecutionDispatchOutbox(
        id=_new_id(), command_id=command_id, run_id=run.id,
        subject=f"{OUTBOX_SUBJECT_PREFIX}{run.owner_id}", message_ref=message_ref,
        status="pending", next_attempt_at=_now(),
    )
    db.add(row)
    return row


def record_command(
    db: Session,
    run: ExecutionRun,
    *,
    kind: str,
    idempotency_key: str,
    payload: dict,
) -> tuple[ExecutionCommand, bool]:
    """在已锁 Run 内登记控制命令，并返回是否为重放。"""
    digest = _hash_payload(payload)
    scope = f"run:{run.id}"
    existing = db.scalar(select(ExecutionCommand).where(
        ExecutionCommand.scope == scope,
        ExecutionCommand.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        if existing.payload_hash != digest:
            raise IdempotencyConflict("idempotency_conflict")
        return existing, True
    command = ExecutionCommand(
        command_id=_new_id(), run_id=run.id, scope=scope, kind=kind,
        idempotency_key=idempotency_key, payload_hash=digest, result={},
    )
    db.add(command)
    db.flush()
    return command, False


def cancel_run(
    db: Session,
    *,
    run_id: str,
    owner_id: str,
    reason: CancelReason,
    idempotency_key: str,
    expected_version: int | None = None,
) -> ExecutionRun:
    run = _lock_run(db, run_id)
    _ensure_owner(run, owner_id)
    command, replayed = record_command(
        db, run, kind="cancel", idempotency_key=idempotency_key,
        payload={"reason": reason.value, "expected_version": expected_version},
    )
    if replayed:
        return run
    if expected_version is not None and run.version != expected_version:
        raise VersionConflict("version_conflict")
    before = run.status
    next_state = request_cancel(_state_from_run(run), reason)
    run.status, run.cancel_reason = next_state.status.value, next_state.cancel_reason.value if next_state.cancel_reason else None
    run.version += 1
    append_event(
        db, run, event_type="run.cancel_requested",
        payload={"reason": reason.value, "cancel_reason": reason.value, "actor": "user"},
        actor={"kind": "user"}, command_id=command.command_id, idempotency_key=idempotency_key,
    )
    append_event(
        db, run, event_type="run.status_changed",
        payload={"from": before, "to": run.status, "reason": reason.value, "actor": "user", "version": run.version},
        actor={"kind": "user"}, command_id=command.command_id, idempotency_key=idempotency_key,
    )
    _add_outbox(db, run, command_id=command.command_id, message_ref=f"command://{command.command_id}")
    command.result = {"status": run.status, "version": run.version}
    return run


def _state_from_run(run: ExecutionRun):
    from .contracts import RunState
    return RunState(
        status=RunStatus(run.status),
        cancel_reason=CancelReason(run.cancel_reason) if run.cancel_reason else None,
        unresolved_call_count=0,
    )


def acquire_lease(db: Session, *, run_id: str, worker_id: str, ttl: timedelta = timedelta(seconds=30)) -> LeaseToken:
    run = _lock_run(db, run_id)
    if run.status in {s.value for s in {RunStatus.CANCELLED, RunStatus.EXPIRED, RunStatus.COMPLETED, RunStatus.FAILED}}:
        raise ContractError("terminal Run cannot acquire lease")
    now = _now()
    lease_expires = _as_utc(run.lease_expires_at)
    if lease_expires and lease_expires > now and run.lease_owner != worker_id:
        raise ContractError("lease is held by another worker")
    run.lease_epoch += 1
    run.lease_owner = worker_id
    run.lease_expires_at = now + ttl
    return LeaseToken(run.id, worker_id, run.lease_epoch, run.lease_expires_at)


def assert_lease(run: ExecutionRun, token: LeaseToken) -> None:
    if run.id != token.run_id or run.lease_owner != token.owner or run.lease_epoch != token.epoch:
        raise ContractError("stale lease fencing token")
    if not run.lease_expires_at or _as_utc(run.lease_expires_at) <= _now():
        raise ContractError("lease expired")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
