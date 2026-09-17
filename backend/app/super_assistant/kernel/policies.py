"""Small, side-effect-free policy decisions for ``kernel.v1``.

The kernel owns the policy inputs and returns a decision.  Persistence and Run
mutation stay in the coordinator so a policy evaluation cannot accidentally
commit a parent or child state transition.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping


class PolicyError(ValueError):
    """A policy input is invalid or cannot produce a safe decision."""


def _positive(value: timedelta, name: str) -> timedelta:
    if value <= timedelta(0):
        raise PolicyError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Frozen defaults copied into a Run policy snapshot at creation time."""

    run_deadline: timedelta = timedelta(hours=24)
    active_run_concurrency: int = 4
    step_model_timeout: timedelta = timedelta(seconds=120)
    call_max_attempts: int = 3
    lease_ttl: timedelta = timedelta(seconds=30)
    heartbeat_interval: timedelta = timedelta(seconds=10)
    stuck_detector: timedelta = timedelta(minutes=5)
    cancel_grace: timedelta = timedelta(seconds=30)
    reconciliation_initial: timedelta = timedelta(seconds=5)
    reconciliation_backoff: int = 2
    reconciliation_max_delay: timedelta = timedelta(minutes=5)
    reconciliation_max_attempts: int = 12
    outbox_max_attempts: int = 10
    artifact_retention: timedelta = timedelta(days=30)
    max_steps: int = 8

    def __post_init__(self) -> None:
        for name in (
            "run_deadline", "step_model_timeout", "lease_ttl", "heartbeat_interval",
            "stuck_detector", "cancel_grace", "reconciliation_initial",
            "reconciliation_max_delay", "artifact_retention",
        ):
            _positive(getattr(self, name), name)
        for name in (
            "active_run_concurrency", "call_max_attempts", "reconciliation_max_attempts",
            "outbox_max_attempts", "max_steps",
        ):
            if getattr(self, name) < 1:
                raise PolicyError(f"{name} must be at least 1")
        if self.max_steps < 1 or self.max_steps > 128:
            raise PolicyError("max_steps must be between 1 and 128")
        if self.reconciliation_backoff < 1:
            raise PolicyError("reconciliation_backoff must be at least 1")
        if self.heartbeat_interval >= self.lease_ttl:
            raise PolicyError("heartbeat_interval must be shorter than lease_ttl")

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-friendly values for the immutable policy snapshot."""
        values = asdict(self)
        for key, value in values.items():
            if isinstance(value, timedelta):
                values[key] = value.total_seconds()
        return values


def approval_expires_at(*, now: datetime, requested_ttl: timedelta, run_deadline: datetime) -> datetime:
    """Return the earlier of the requested approval TTL and Run deadline."""
    if now.tzinfo is None or run_deadline.tzinfo is None:
        raise PolicyError("now and run_deadline must be timezone-aware")
    if requested_ttl <= timedelta(0):
        raise PolicyError("requested_ttl must be positive")
    now_utc = now.astimezone(timezone.utc)
    deadline_utc = run_deadline.astimezone(timezone.utc)
    return min(now_utc + requested_ttl, deadline_utc)


class JoinPolicy(StrEnum):
    ALL = "all"
    ANY = "any"


class ChildStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class JoinDecision(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChildResult:
    child_id: str
    status: ChildStatus
    required: bool = True

    def __post_init__(self) -> None:
        if not self.child_id:
            raise PolicyError("child_id cannot be empty")


@dataclass(frozen=True, slots=True)
class JoinResult:
    """A recommendation; the caller must explicitly merge it into the parent Run."""

    decision: JoinDecision
    successful_child_ids: tuple[str, ...] = ()
    failed_child_ids: tuple[str, ...] = ()
    waiting_child_ids: tuple[str, ...] = ()
    reason: str = ""


def decide_child_join(children: tuple[ChildResult, ...] | list[ChildResult], policy: JoinPolicy = JoinPolicy.ALL) -> JoinResult:
    """Evaluate parent readiness without mutating a Run.

    A failed required child always fails the parent.  Optional failures are
    reported but never fail it.  ``all`` waits for every required child;
    ``any`` is ready after any successful child (while still honoring required
    failures).
    """
    try:
        policy = JoinPolicy(policy)
    except ValueError as exc:
        raise PolicyError(f"unknown join policy: {policy}") from exc
    if not children:
        return JoinResult(JoinDecision.PENDING, reason="no children bound")
    seen: set[str] = set()
    for child in children:
        if child.child_id in seen:
            raise PolicyError(f"duplicate child_id: {child.child_id}")
        seen.add(child.child_id)
    successful = tuple(c.child_id for c in children if c.status == ChildStatus.COMPLETED)
    failed_required = tuple(c.child_id for c in children if c.required and c.status in {ChildStatus.FAILED, ChildStatus.CANCELLED, ChildStatus.EXPIRED})
    failed_optional = tuple(c.child_id for c in children if not c.required and c.status in {ChildStatus.FAILED, ChildStatus.CANCELLED, ChildStatus.EXPIRED})
    failed = failed_required + failed_optional
    waiting = tuple(c.child_id for c in children if c.status in {ChildStatus.PENDING, ChildStatus.RUNNING})
    if failed_required:
        return JoinResult(JoinDecision.FAILED, successful, failed, waiting, "required child failed")
    if policy == JoinPolicy.ANY and successful:
        return JoinResult(JoinDecision.READY, successful, failed, waiting, "a child completed")
    required_waiting = tuple(c.child_id for c in children if c.required and c.status in {ChildStatus.PENDING, ChildStatus.RUNNING})
    if policy == JoinPolicy.ALL and not required_waiting:
        return JoinResult(JoinDecision.READY, successful, failed, waiting, "all required children completed")
    return JoinResult(JoinDecision.PENDING, successful, failed, waiting, "waiting for child results")


class SideEffectClass(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"
    EXTERNAL_ASYNC = "external_async"


def requires_approval(side_effect_class: str | None, *, require_confirmation: bool = False) -> bool:
    """Baseline §10: MCP require_confirmation 保持既有口径，其余凡非
    read_only 的副作用类别（idempotent_write/non_idempotent_write/
    external_async）必须在派发前取得用户审批；未知分类 fail-closed。"""
    if require_confirmation:
        return True
    value = str(side_effect_class or "")
    if value not in {member.value for member in SideEffectClass}:
        return True
    return value != SideEffectClass.READ_ONLY.value


class RetryDecision(StrEnum):
    RETRY = "retry"
    DO_NOT_RETRY = "do_not_retry"
    MANUAL_ATTENTION = "manual_attention"


@dataclass(frozen=True, slots=True)
class RetryResult:
    decision: RetryDecision
    reason: str


def decide_retry(*, side_effect: SideEffectClass, safe_to_retry: bool, outcome_unknown: bool, attempt_no: int, max_attempts: int) -> RetryResult:
    """Decide retry without ever replaying an unknown non-idempotent write."""
    if attempt_no < 1 or max_attempts < 1:
        raise PolicyError("attempt_no and max_attempts must be positive")
    if outcome_unknown and side_effect == SideEffectClass.NON_IDEMPOTENT_WRITE:
        return RetryResult(RetryDecision.MANUAL_ATTENTION, "unknown non-idempotent write outcome")
    if outcome_unknown and side_effect == SideEffectClass.EXTERNAL_ASYNC:
        return RetryResult(RetryDecision.MANUAL_ATTENTION, "external async outcome requires reconciliation")
    if not safe_to_retry:
        return RetryResult(RetryDecision.DO_NOT_RETRY, "provider marked attempt unsafe to retry")
    if attempt_no >= max_attempts:
        return RetryResult(RetryDecision.DO_NOT_RETRY, "maximum attempts reached")
    return RetryResult(RetryDecision.RETRY, "attempt is safe to retry")


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    """Stable, serializable error contract for API, events and Call results."""

    error_code: str
    message: str
    retryable: bool = False
    safe_to_retry: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    provider_status: str | None = None

    def __post_init__(self) -> None:
        if not self.error_code or not self.message:
            raise PolicyError("error_code and message are required")
        if any(ch.isspace() for ch in self.error_code):
            raise PolicyError("error_code must not contain whitespace")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "safe_to_retry": self.safe_to_retry,
            "details": dict(self.details),
            "provider_status": self.provider_status,
        }
