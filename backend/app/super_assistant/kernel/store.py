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
    TERMINAL_RUN_STATUSES,
    request_cancel,
)
from .events import EventEnvelope, validate_payload
from .models import (
    InboxItem,
    ExecutionCall,
    ExecutionCommand,
    ExecutionDispatchOutbox,
    ExecutionEvent,
    ExecutionRun,
)
from ..models import SuperAssistantConversation


UTC = timezone.utc
RUN_DEADLINE = timedelta(hours=24)
CANCEL_GRACE = timedelta(seconds=30)
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


class QuestionStateError(ContractError):
    """回答关联的问题不存在、已过期或已被消费（映射为 404/409）。"""

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


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
    max_steps: int = 8,
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
    if type(max_steps) is not int or not 1 <= max_steps <= 128:
        raise ContractError("max_steps must be between 1 and 128")
    conv = db.scalar(
        select(SuperAssistantConversation)
        .where(SuperAssistantConversation.id == conversation_id, SuperAssistantConversation.owner_id == owner_id)
        .with_for_update()
    )
    if conv is None:
        raise KeyError(conversation_id)
    # ``assistant_child`` Runs are executed by the Assistant Hub in a worker
    # session. Persist an internal marker inside the frozen context so the
    # Hub can distinguish this durable path from the legacy direct-UI tool
    # (whose historical "resume latest" behaviour remains supported). Copy
    # nested data first: callers may reuse their argument dict for another
    # invocation and must not observe our marker.
    binding = dict(binding or {})
    mode = binding.get("binding_mode", "direct_ui")
    if mode == "assistant_child":
        child_context = binding.get("context")
        child_context = dict(child_context) if isinstance(child_context, dict) else {}
        # This is a trusted execution-origin marker, not model/user context.
        # Always overwrite a supplied value so false/null cannot restore the
        # legacy "resume latest" path and bypass the frozen domain binding.
        child_context["_kernel_child"] = True
        binding["context"] = child_context

    payload = {
        "goal": goal,
        "deadline": deadline.isoformat() if deadline else None,
        "parent_run_id": parent_run_id,
        "join_policy": join_policy,
        "max_steps": max_steps,
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
        if parent.status in {
            RunStatus.CANCELLED.value, RunStatus.EXPIRED.value,
            RunStatus.COMPLETED.value, RunStatus.FAILED.value,
        }:
            raise ContractError("cannot bind child to terminal parent")
    if mode == "delegated":
        required = {"ontology_id", "draft_version_id", "lifecycle", "write_permission_hash"}
        if required - binding.keys() or binding.get("lifecycle") != "editing" or not binding.get("write_permission_hash"):
            raise ContractError("delegated binding requires editing draft and write permission")
        # Domain ownership/lifecycle and the permission fingerprint are owned
        # by the exploration service.  Do not accept an opaque caller-supplied
        # hash: a stale or fabricated hash would turn a public delegated Run
        # into an authorization snapshot that cannot be revalidated later.
        from app.auth.models import User
        from app.assistant_hub.adapters.exploration import validate_delegated_binding
        user = db.get(User, owner_id)
        if user is None:
            raise ContractError("delegated binding owner does not exist")
        try:
            normalized = validate_delegated_binding(db, user, binding)
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
        binding.update(normalized)
    elif mode not in {"direct_ui", "legacy"}:
        if mode != "assistant_child" or not binding.get("assistant_key"):
            raise ContractError("unknown binding_mode")
    run = ExecutionRun(
        id=_new_id(), owner_id=owner_id, conversation_id=conversation_id,
        parent_run_id=parent_run_id, execution_version="kernel.v1", status="queued",
        goal=goal, deadline=deadline or (_now() + RUN_DEADLINE),
        idempotency_key=idempotency_key, payload_hash=payload_hash,
        binding_mode=mode, binding_snapshot_ref=json.dumps(binding, sort_keys=True),
        ontology_id=binding.get("ontology_id"), draft_version_id=binding.get("draft_version_id"),
        join_policy=join_policy,
        budget_snapshot_ref=json.dumps({"max_steps": max_steps}, sort_keys=True),
    )
    db.add(run)
    db.flush()
    command_id = _new_id()
    append_event(
        db, run, event_type="run.created",
        payload={"execution_version": "kernel.v1", "conversation_id": conversation_id},
        actor={"kind": "user"}, command_id=command_id, idempotency_key=idempotency_key,
    )
    if parent_run_id:
        child_ids = list(parent.required_child_ids or [])
        if run.id in child_ids:
            raise IdempotencyConflict("child Run is already bound to parent")
        child_ids.append(run.id)
        parent.required_child_ids = child_ids
        append_event(
            db, parent, event_type="run.child_bound",
            payload={"parent_run_id": parent.id, "child_run_id": run.id, "join_policy": parent.join_policy, "required": True},
            actor={"kind": "user"}, command_id=command_id, idempotency_key=f"{idempotency_key}:parent-bind",
        )
        append_event(
            db, run, event_type="run.child_bound",
            payload={"parent_run_id": parent.id, "child_run_id": run.id, "join_policy": parent.join_policy, "required": True},
            actor={"kind": "user"}, command_id=command_id, idempotency_key=f"{idempotency_key}:child-bind",
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
    connector_id: str | None = None,
    provider_event_id: str | None = None,
) -> ExecutionEvent:
    """追加一个事件；调用方必须已锁定 Run。seq 回滚不消耗可见序号。"""
    if lease is not None:
        assert_lease(run, lease)
    validate_payload(event_type, payload)
    payload_hash = _hash_payload(payload)
    if connector_id and provider_event_id:
        prior = db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.connector_id == connector_id,
            ExecutionEvent.provider_event_id == provider_event_id,
        ))
        if prior is not None:
            if prior.run_id != run.id:
                # Provider event ids are scoped to a connector, not to an
                # arbitrary Run. Returning a row from another Run would make
                # this append silently disappear from the current event log
                # and could let a cross-owner/reused callback be treated as a
                # successful duplicate.
                raise IdempotencyConflict("provider_event_id is already bound to another Run")
            if prior.payload_hash != payload_hash:
                raise IdempotencyConflict("provider_event_id payload hash conflict")
            return prior
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
        redaction=envelope.redaction, connector_id=connector_id,
        provider_event_id=provider_event_id, payload_hash=payload_hash,
    )
    db.add(event)
    run.next_event_seq += 1
    return event


def _add_outbox(
    db: Session,
    run: ExecutionRun,
    *,
    command_id: str,
    message_ref: str,
    subject: str | None = None,
    payload: dict | None = None,
) -> ExecutionDispatchOutbox:
    row = ExecutionDispatchOutbox(
        id=_new_id(), command_id=command_id, run_id=run.id,
        subject=subject or f"{OUTBOX_SUBJECT_PREFIX}{run.owner_id}", message_ref=message_ref,
        payload=dict(payload or {}),
        status="pending", next_attempt_at=_now(),
    )
    db.add(row)
    return row


def enqueue_reconcile_observation(
    db: Session,
    *,
    run: ExecutionRun,
    call: ExecutionCall,
    observation: dict,
    claim_owner: str | None = None,
) -> ExecutionDispatchOutbox:
    """Persist one external observation for the NATS reconciler consumer.

    Callback and scheduler ingress are producers only.  They never mutate the
    Call state directly; the durable ``sa.execution.reconcile`` consumer is
    the single fenced state transition path.  A provider event id (or a
    deterministic payload hash when a provider omits one) is the command
    identity, so retries are idempotent and conflicting observations are
    rejected before another outbox row can be created.
    """
    if run.id != call.run_id:
        raise ContractError("reconcile observation call does not belong to run")
    if claim_owner is not None and call.lease_owner != claim_owner:
        raise VersionConflict("reconcile claim is no longer held")
    payload = dict(observation)
    payload["run_id"] = run.id
    payload["call_id"] = call.id
    provider_event_id = str(payload.get("provider_event_id") or "").strip()
    identity = provider_event_id or _hash_payload(payload)
    command_id = f"reconcile:{call.id}:{identity}"
    message_ref = f"reconcile://{call.id}/{identity}"
    existing = db.scalar(select(ExecutionDispatchOutbox).where(
        ExecutionDispatchOutbox.command_id == command_id,
    ).with_for_update())
    if existing is not None:
        if _hash_payload(existing.payload or {}) != _hash_payload(payload):
            raise IdempotencyConflict("reconcile observation payload conflict")
        if existing.status == "published":
            # 观察型消息没有消费回执：一旦消费端丢失投递（nak 耗尽/流重建），
            # 已发布行会以同 command_id 永远挡住该 provider_event_id 的重发，
            # Call 随即无限等待。命中已发布行即重置回 pending 让 publisher
            # 重投；重复消费由消费端 provider_event_id 幂等去重兜底。
            existing.status = "pending"
            existing.claim_token = None
            existing.claim_expires_at = None
            db.flush()
        return existing
    from app.data_channel.pipeline_tasks.dispatch import EXECUTION_RECONCILE_SUBJECT
    return _add_outbox(
        db, run, command_id=command_id, message_ref=message_ref,
        subject=EXECUTION_RECONCILE_SUBJECT, payload=payload,
    )


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
    # Fence any in-flight worker immediately. It may still submit facts with a
    # row lock, but it cannot continue tool/model side effects under the old
    # epoch; cancel grace and recovery own the remaining closure.
    run.lease_epoch += 1
    run.lease_owner, run.lease_expires_at = None, None
    # Cancellation is a two-phase protocol.  The worker gets a bounded grace
    # period to confirm remote cancellation; the scheduler closes the Run
    # after this deadline even if the connector is unavailable.
    run.cancel_deadline = _now() + CANCEL_GRACE
    run.version += 1
    # Mark in-flight Calls before publishing the cancellation wake-up. The
    # external reconciler can then issue connector.cancel when a remote handle
    # exists; a delayed invoke message will be rejected by the runtime gate.
    _mark_calls_cancel_requested(db, run, command_id=command.command_id, actor={"kind": "user"})
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
    _cascade_cancel_children(db, run, parent_command_id=command.command_id)
    _add_outbox(db, run, command_id=command.command_id, message_ref=f"command://{command.command_id}")
    command.result = {"status": run.status, "version": run.version}
    return run


def _mark_calls_cancel_requested(db: Session, run: ExecutionRun, *, command_id: str, actor: dict[str, str]) -> None:
    """Move in-flight Calls onto the cancellation/reconciliation path."""
    active_calls = db.scalars(select(ExecutionCall).where(
        ExecutionCall.run_id == run.id,
        ExecutionCall.status.in_(("offered", "dispatched", "running", "waiting_external", "reconciling")),
    ).with_for_update()).all()
    for call in active_calls:
        if call.status in {"offered", "dispatched"} and call.outcome == "not_sent":
            call.status, call.outcome = "closed", "not_sent"
        else:
            # 取消意图只写 status（baseline §3.2）：outcome 保留已观测事实，
            # 由 reconciliation 决定远端是否真正停止。
            call.status = "cancel_requested"
        call.next_reconcile_at = _now() if call.remote_task_ref else None
        append_event(
            db, run, event_type="call.outcome_changed",
            payload={"call_id": call.id, "status": call.status, "outcome": call.outcome, "evidence_ref": call.evidence_ref, "connector_id": call.target_ref, "provider_event_id": call.provider_event_id},
            actor=actor, command_id=f"cancel:{command_id}:{call.id}", idempotency_key=f"cancel-call:{command_id}:{call.id}", connector_id=call.target_ref,
        )


def _cascade_cancel_children(db: Session, parent: ExecutionRun, *, parent_command_id: str) -> None:
    """Propagate a parent cancellation through every non-terminal descendant."""
    pending = [parent.id]
    visited: set[str] = set()
    while pending:
        ancestor_id = pending.pop(0)
        if ancestor_id in visited:
            continue
        visited.add(ancestor_id)
        children = db.scalars(
            select(ExecutionRun)
            .where(ExecutionRun.parent_run_id == ancestor_id)
            .order_by(ExecutionRun.id)
            .with_for_update()
        ).all()
        for child in children:
            pending.append(child.id)
            if child.status in {
                RunStatus.CANCELLED.value, RunStatus.EXPIRED.value,
                RunStatus.COMPLETED.value, RunStatus.FAILED.value,
            }:
                continue
            child.lease_epoch += 1
            child.lease_owner, child.lease_expires_at = None, None
            child_key = f"parent-cancel:{parent_command_id}:{child.id}"
            _mark_calls_cancel_requested(db, child, command_id=child_key, actor={"kind": "system"})
            child_command, replayed = record_command(
                db, child, kind="cancel", idempotency_key=child_key,
                payload={"reason": CancelReason.PARENT.value, "parent_command_id": parent_command_id},
            )
            if replayed:
                continue
            before = child.status
            next_state = request_cancel(_state_from_run(child), CancelReason.PARENT)
            child.status = next_state.status.value
            child.cancel_reason = CancelReason.PARENT.value
            child.cancel_deadline = _now() + CANCEL_GRACE
            child.version += 1
            append_event(
                db, child, event_type="run.cancel_requested",
                payload={"reason": CancelReason.PARENT.value, "cancel_reason": CancelReason.PARENT.value, "actor": "system"},
                actor={"kind": "system"}, command_id=child_command.command_id, idempotency_key=child_key,
            )
            append_event(
                db, child, event_type="run.status_changed",
                payload={"from": before, "to": child.status, "reason": "parent_cancel", "actor": "system", "version": child.version},
                actor={"kind": "system"}, command_id=child_command.command_id, idempotency_key=f"{child_key}:status",
            )
            _add_outbox(db, child, command_id=child_command.command_id, message_ref=f"command://{child_command.command_id}")
            child_command.result = {"status": child.status, "version": child.version}


def control_run(
    db: Session,
    *,
    run_id: str,
    owner_id: str,
    action: str,
    idempotency_key: str,
    expected_version: int,
) -> ExecutionRun:
    run = _lock_run(db, run_id)
    _ensure_owner(run, owner_id)
    command, replayed = record_command(
        db, run, kind=action, idempotency_key=idempotency_key,
        payload={"action": action, "expected_version": expected_version},
    )
    if replayed:
        return run
    if run.version != expected_version:
        raise VersionConflict("version_conflict")
    before = run.status
    if action == "pause":
        if run.status != RunStatus.ACTIVE.value:
            raise ContractError("only active Run can pause")
        run.status = RunStatus.PAUSED.value
        # Pausing invalidates the current activation. Resume will mint a new
        # epoch so a late provider result cannot pass fencing and continue a
        # stale tool loop.
        run.lease_epoch += 1
        run.lease_owner, run.lease_expires_at = None, None
        event_type = "run.pause_requested"
        payload = {"reason": "user", "pause_reason": "user", "actor": "user"}
    elif action == "resume":
        if run.status != RunStatus.PAUSED.value:
            raise ContractError("only paused Run can resume")
        run.status = RunStatus.ACTIVE.value
        run.lease_epoch += 1
        run.lease_owner, run.lease_expires_at = None, None
        event_type = "run.recovery_requested"
        payload = {"reason": "user_resume", "lease_epoch": run.lease_epoch, "diagnostic_ref": None}
    else:
        raise ContractError("unknown control action")
    run.version += 1
    append_event(db, run, event_type=event_type, payload=payload, actor={"kind": "user"}, command_id=command.command_id, idempotency_key=idempotency_key)
    append_event(db, run, event_type="run.status_changed", payload={"from": before, "to": run.status, "reason": action, "actor": "user", "version": run.version}, actor={"kind": "user"}, command_id=command.command_id, idempotency_key=idempotency_key)
    _add_outbox(db, run, command_id=command.command_id, message_ref=f"command://{command.command_id}")
    command.result = {"status": run.status, "version": run.version}
    return run


def _resolve_content_ref(db: Session, *, owner_id: str, run_id: str, content_ref: str) -> str:
    """把 ``artifact://`` 引用解析为有界正文；未知引用不落库。

    用户经 ``content_ref`` 提交的内容必须以其真实正文进入模型视图，不能把
    一行 URI 当作消息内容。只支持本 owner 的 Artifact；visibility 为
    run/call 的 Artifact 不能跨 Run 引用。
    """
    from .artifacts import MAX_ARTIFACT_BYTES, verify_artifact
    from .models import Artifact

    ref = str(content_ref or "").strip()
    if not ref.startswith("artifact://"):
        raise ContractError("unsupported content_ref scheme")
    artifact_id = ref.removeprefix("artifact://")
    artifact = db.scalar(select(Artifact).where(Artifact.id == artifact_id))
    if artifact is None or artifact.owner_id != owner_id:
        raise QuestionStateError("content_ref does not resolve to an artifact", not_found=True)
    if artifact.visibility in {"run", "call"} and artifact.run_id != run_id:
        raise QuestionStateError("content_ref artifact is scoped to another run", not_found=True)
    if artifact.status != "complete" or artifact.integrity_status != "verified":
        raise ContractError("content_ref artifact is not ready")
    from .artifacts import artifact_is_expired
    if artifact_is_expired(status=artifact.status, retention_until=getattr(artifact, "retention_until", None)):
        raise ContractError("content_ref artifact retention expired")
    if artifact.inline_content is not None:
        data = artifact.inline_content.encode("utf-8")
    else:
        if int(artifact.size or 0) > 256 * 1024 or int(artifact.size or 0) > MAX_ARTIFACT_BYTES:
            raise ContractError("content_ref artifact exceeds input size limit")
        from app.shared.storage import get_storage_service
        try:
            data = get_storage_service().get_object(artifact.storage_ref)
        except Exception as exc:
            raise ContractError("content_ref artifact is not readable") from exc
    if len(data) > 256 * 1024:
        raise ContractError("content_ref artifact exceeds input size limit")
    integrity = verify_artifact(data, expected_checksum=artifact.checksum, expected_size=artifact.size)
    if integrity.integrity_status != "verified":
        raise ContractError("content_ref artifact failed integrity check")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("content_ref artifact is not UTF-8 text") from exc


def append_input(
    db: Session,
    *,
    run_id: str,
    owner_id: str,
    kind: str,
    payload: dict,
    idempotency_key: str,
    question_id: str | None = None,
    target_ref: str | None = None,
    expires_at: datetime | None = None,
    expiry_policy: str | None = None,
    expected_version: int | None = None,
) -> InboxItem:
    run = _lock_run(db, run_id)
    _ensure_owner(run, owner_id)
    if kind == "question_answer" and not question_id:
        raise ContractError("question_id is required for question_answer")
    existing = db.scalar(select(InboxItem).where(
        InboxItem.run_id == run.id, InboxItem.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        same = existing.kind == kind and existing.question_id == question_id and existing.target_ref == target_ref
        if same:
            if payload.get("content_ref") and not payload.get("content"):
                # 首次请求的 content_ref 已在落库前解析为正文；重放只携带
                # ref。必须按 ref 判等——拿 {content_ref} 与已解析的
                # {content_ref, content} 逐字节比较会误报幂等冲突。
                same = (existing.payload or {}).get("content_ref") == payload.get("content_ref")
            else:
                same = (existing.payload or {}) == payload
        if not same:
            raise IdempotencyConflict("inbox idempotency key reused with different payload")
        return existing
    if expected_version is not None and run.version != expected_version:
        raise VersionConflict("version_conflict")
    if run.status in {status.value for status in TERMINAL_RUN_STATUSES}:
        raise ContractError("terminal Run cannot accept input")
    # ``content_ref`` 必须在这里解析成正文：下游模型视图只消费 content。
    if payload.get("content") is None and payload.get("content_ref"):
        resolved = _resolve_content_ref(db, owner_id=run.owner_id, run_id=run.id, content_ref=str(payload["content_ref"]))
        payload = {**payload, "content": resolved}
    if kind in {"user_input", "question_answer"} and payload.get("content") is None:
        # 空回答会在下方把问题行永久置为 consumed：答案静默丢失且 TTL 不再
        # 补救，必须在落库前拒绝（HTTP 层已有 one-of 校验，此处覆盖内部调用方）。
        raise ContractError("input requires content or a resolvable content_ref")
    question_row: InboxItem | None = None
    if kind == "question_answer":
        if run.status not in {
            RunStatus.WAITING_INPUT.value,
            RunStatus.WAITING_RETRY.value,
            RunStatus.PAUSED.value,
        }:
            raise ContractError("run is not waiting for input")
        question_row = db.scalar(select(InboxItem).where(
            InboxItem.run_id == run.id,
            InboxItem.kind == "question_answer",
            InboxItem.question_id == question_id,
            InboxItem.source == "system",
        ).order_by(InboxItem.accepted_at.desc(), InboxItem.id.desc()).with_for_update())
        if question_row is None:
            raise QuestionStateError("question not found for this run", not_found=True)
        question_expires = _as_utc(question_row.expires_at)
        if question_row.status == "expired" or (question_expires is not None and question_expires <= _now()):
            raise QuestionStateError("question has expired")
        if question_row.status != "pending":
            raise QuestionStateError("question is no longer answerable")
    item = InboxItem(
        run_id=run.id, kind=kind, priority={"control": 0, "approval_decision": 10, "external_event": 20, "user_input": 30, "question_answer": 30, "resume": 0}.get(kind, 30),
        status="pending", question_id=question_id, target_ref=target_ref,
        payload=payload, payload_ref=target_ref, source="user", expires_at=expires_at,
        expiry_policy=expiry_policy, accepted_at=_now(), idempotency_key=idempotency_key,
    )
    db.add(item)
    db.flush()
    command_id = _new_id()
    append_event(db, run, event_type="inbox.appended", payload={"inbox_id": item.id, "kind": kind, "target_ref": target_ref or item.id, "expiry_policy": expiry_policy or "none", "question_id": question_id}, actor={"kind": "user"}, command_id=command_id, idempotency_key=idempotency_key)
    if question_row is not None:
        # 回答被接受即关闭问题行：否则 TTL 扫描器会在 30 分钟后把已回答的
        # 问题重新提问或按 fail 策略误伤 Run。
        question_row.status = "consumed"
        question_row.consumed_at = _now()
        append_event(
            db, run, event_type="inbox.consumed",
            payload={"inbox_id": question_row.id, "kind": question_row.kind, "question_id": question_row.question_id},
            actor={"kind": "user"}, command_id=command_id,
            idempotency_key=f"inbox-consumed:{question_row.id}",
        )
    if kind in {"user_input", "question_answer", "resume"} and run.status in {RunStatus.WAITING_INPUT.value, RunStatus.WAITING_RETRY.value}:
        before = run.status
        run.status, run.wait_reason, run.version = RunStatus.ACTIVE.value, None, run.version + 1
        reason = "resume_received" if kind == "resume" else "input_received"
        append_event(db, run, event_type="run.status_changed", payload={"from": before, "to": run.status, "reason": reason, "actor": "user", "version": run.version}, actor={"kind": "user"}, command_id=command_id, idempotency_key=idempotency_key)
        # Waking a Run is a durable command. Without an outbox record the
        # state would become ACTIVE while no NATS activation is published.
        _add_outbox(db, run, command_id=command_id, message_ref=f"command://{command_id}")
    elif run.status == RunStatus.ACTIVE.value and kind == "user_input":
        # 激活中途到达的输入不能改变当前 Step，但必须保证后续激活能捡到它：
        # 登记一条续行 Outbox，让当前 worker 收尾后立刻再激活一次。
        _add_outbox(db, run, command_id=f"{command_id}:wake", message_ref=f"run://{run.id}")
    return item


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


def renew_lease(db: Session, *, token: LeaseToken, ttl: timedelta = timedelta(seconds=30)) -> LeaseToken:
    """Renew a worker lease while retaining the same fencing epoch.

    Renewal is deliberately fenced and transactional: a worker that lost its
    lease cannot extend it after another worker has taken ownership.
    """
    if ttl <= timedelta(0):
        raise ContractError("lease ttl must be positive")
    run = _lock_run(db, token.run_id)
    if run.status not in {RunStatus.ACTIVE.value, RunStatus.QUEUED.value}:
        raise ContractError("non-active Run cannot renew lease")
    assert_lease(run, token)
    expires_at = _now() + ttl
    run.lease_expires_at = expires_at
    db.flush()
    return LeaseToken(run.id, token.owner, token.epoch, expires_at)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
