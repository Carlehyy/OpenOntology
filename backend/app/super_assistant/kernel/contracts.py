"""kernel.v1 的规范化状态、状态转换和问题/取消策略。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class ContractError(ValueError):
    """输入或状态转换违反 kernel.v1 合同。"""


class RunStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_RETRY = "waiting_retry"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    COMPLETED = "completed"
    FAILED = "failed"


class CancelReason(StrEnum):
    USER = "user"
    PARENT = "parent"
    DEADLINE = "deadline"


class ExpiryPolicy(StrEnum):
    REASK_ONCE = "reask_once"
    FAIL_BRANCH = "fail_branch"
    FAIL_RUN = "fail_run"


class ExpiryAction(StrEnum):
    REASK = "reask"
    FAIL_BRANCH = "fail_branch"
    FAIL_RUN = "fail_run"


class CallStatus(StrEnum):
    OFFERED = "offered"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_EXTERNAL = "waiting_external"
    CANCEL_REQUESTED = "cancel_requested"
    RECONCILING = "reconciling"
    CLOSED = "closed"


class CallOutcome(StrEnum):
    NOT_SENT = "not_sent"
    ACCEPTED = "accepted"
    REMOTE_RUNNING = "remote_running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED_CONFIRMED = "cancelled_confirmed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class TurnCloseReason(StrEnum):
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_RETRY = "waiting_retry"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    STUCK_RECOVERY = "stuck_recovery"
    INTERRUPTED = "interrupted"
    YIELDED = "yielded"


class StepCloseReason(StrEnum):
    DECISION_COMPLETE = "decision_complete"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_RETRY = "waiting_retry"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.CANCELLED,
    RunStatus.EXPIRED,
    RunStatus.COMPLETED,
    RunStatus.FAILED,
})
WAITING_RUN_STATUSES = frozenset({
    RunStatus.WAITING_INPUT,
    RunStatus.WAITING_APPROVAL,
    RunStatus.WAITING_EXTERNAL,
    RunStatus.WAITING_RETRY,
})

_ALLOWED_CALL_OUTCOMES: dict[CallStatus, frozenset[CallOutcome]] = {
    CallStatus.OFFERED: frozenset({CallOutcome.NOT_SENT}),
    CallStatus.DISPATCHED: frozenset({CallOutcome.NOT_SENT, CallOutcome.OUTCOME_UNKNOWN}),
    CallStatus.RUNNING: frozenset({CallOutcome.ACCEPTED, CallOutcome.REMOTE_RUNNING, CallOutcome.OUTCOME_UNKNOWN}),
    CallStatus.WAITING_EXTERNAL: frozenset({CallOutcome.ACCEPTED, CallOutcome.REMOTE_RUNNING, CallOutcome.OUTCOME_UNKNOWN}),
    CallStatus.CANCEL_REQUESTED: frozenset({CallOutcome.ACCEPTED, CallOutcome.REMOTE_RUNNING, CallOutcome.OUTCOME_UNKNOWN}),
    CallStatus.RECONCILING: frozenset({CallOutcome.ACCEPTED, CallOutcome.REMOTE_RUNNING, CallOutcome.OUTCOME_UNKNOWN}),
    CallStatus.CLOSED: frozenset({CallOutcome.NOT_SENT, CallOutcome.COMPLETED, CallOutcome.FAILED, CallOutcome.CANCELLED_CONFIRMED}),
}


@dataclass(frozen=True, slots=True)
class RunState:
    status: RunStatus = RunStatus.QUEUED
    cancel_reason: CancelReason | None = None
    unresolved_call_count: int = 0
    question_attempts: int = 0

    def __post_init__(self) -> None:
        if self.unresolved_call_count < 0:
            raise ContractError("unresolved_call_count cannot be negative")
        if self.status in TERMINAL_RUN_STATUSES and self.cancel_reason not in {
            None, CancelReason.USER, CancelReason.PARENT, CancelReason.DEADLINE,
        }:
            raise ContractError("terminal Run has invalid cancel_reason")
        if self.status not in {
            RunStatus.CANCEL_REQUESTED,
            RunStatus.CANCELLING,
            *TERMINAL_RUN_STATUSES,
        } and self.cancel_reason is not None:
            raise ContractError("open Run cannot retain a cancel_reason")


def _ensure_open(state: RunState) -> None:
    if state.status in TERMINAL_RUN_STATUSES:
        raise ContractError(f"terminal Run cannot transition: {state.status}")


def request_cancel(state: RunState, reason: CancelReason) -> RunState:
    """登记取消意图；首个控制命令赢得终态原因。"""
    _ensure_open(state)
    if state.status in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLING}:
        if state.cancel_reason != reason:
            raise ContractError("conflicting cancel reason")
        return state
    return replace(state, status=RunStatus.CANCEL_REQUESTED, cancel_reason=reason)


def begin_cancelling(state: RunState) -> RunState:
    _ensure_open(state)
    if state.status != RunStatus.CANCEL_REQUESTED:
        raise ContractError("only cancel_requested can begin cancelling")
    return replace(state, status=RunStatus.CANCELLING)


def finish_cancelling(state: RunState) -> RunState:
    _ensure_open(state)
    if state.status != RunStatus.CANCELLING or state.cancel_reason is None:
        raise ContractError("cancelling requires a cancel reason")
    terminal = RunStatus.EXPIRED if state.cancel_reason == CancelReason.DEADLINE else RunStatus.CANCELLED
    return replace(state, status=terminal)


def mark_deadline(state: RunState) -> RunState:
    """Run deadline：无未决 Call 可直接过期，否则先登记 deadline cancel。"""
    _ensure_open(state)
    if state.cancel_reason is not None and state.cancel_reason != CancelReason.DEADLINE:
        raise ContractError("conflicting cancel reason")
    if state.unresolved_call_count:
        return request_cancel(state, CancelReason.DEADLINE)
    return replace(state, status=RunStatus.EXPIRED, cancel_reason=CancelReason.DEADLINE)


def mark_active(state: RunState) -> RunState:
    _ensure_open(state)
    if state.status not in {RunStatus.QUEUED, *WAITING_RUN_STATUSES, RunStatus.PAUSED}:
        raise ContractError(f"cannot activate from {state.status}")
    return replace(state, status=RunStatus.ACTIVE)


def mark_waiting(state: RunState, status: RunStatus) -> RunState:
    _ensure_open(state)
    if status not in WAITING_RUN_STATUSES:
        raise ContractError("invalid waiting status")
    if state.status != RunStatus.ACTIVE:
        raise ContractError("only active Run can wait")
    return replace(state, status=status)


def mark_paused(state: RunState) -> RunState:
    _ensure_open(state)
    if state.status not in {RunStatus.ACTIVE, *WAITING_RUN_STATUSES}:
        raise ContractError(f"cannot pause from {state.status}")
    return replace(state, status=RunStatus.PAUSED)


def mark_completed(state: RunState) -> RunState:
    _ensure_open(state)
    if state.status != RunStatus.ACTIVE or state.unresolved_call_count:
        raise ContractError("Run must be active with no unresolved Call to complete")
    return replace(state, status=RunStatus.COMPLETED)


def mark_failed(state: RunState) -> RunState:
    _ensure_open(state)
    return replace(state, status=RunStatus.FAILED)


def validate_call(status: CallStatus, outcome: CallOutcome) -> None:
    if outcome not in _ALLOWED_CALL_OUTCOMES[status]:
        raise ContractError(f"invalid Call status/outcome pair: {status}/{outcome}")


def transition_call(status: CallStatus, outcome: CallOutcome, *, new_status: CallStatus, new_outcome: CallOutcome | None = None) -> tuple[CallStatus, CallOutcome]:
    """校验 Call 双轴转换；cancel_requested 不写入 outcome。"""
    validate_call(status, outcome)
    resolved_outcome = outcome if new_outcome is None else new_outcome
    validate_call(new_status, resolved_outcome)
    if status == CallStatus.CLOSED:
        raise ContractError("closed Call cannot transition")
    return new_status, resolved_outcome


def expire_question(state: RunState, policy: ExpiryPolicy) -> tuple[RunState, ExpiryAction]:
    """返回新状态和确定的分支动作；分支失败不伪造为整个 Run 失败。"""
    _ensure_open(state)
    if policy == ExpiryPolicy.REASK_ONCE and state.question_attempts == 0:
        return replace(state, status=RunStatus.WAITING_INPUT, question_attempts=1), ExpiryAction.REASK
    if policy == ExpiryPolicy.REASK_ONCE:
        return replace(state, status=RunStatus.ACTIVE), ExpiryAction.FAIL_BRANCH
    if policy == ExpiryPolicy.FAIL_BRANCH:
        return replace(state, status=RunStatus.ACTIVE), ExpiryAction.FAIL_BRANCH
    if policy == ExpiryPolicy.FAIL_RUN:
        return replace(state, status=RunStatus.FAILED), ExpiryAction.FAIL_RUN
    raise ContractError(f"unknown expiry policy: {policy}")
