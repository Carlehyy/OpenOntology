"""Pure reconciliation and recovery decisions for kernel.v1.

Remote connector callbacks are intentionally treated as observations.  This
module normalizes provider states and computes the next action without touching
SQLAlchemy objects; the runtime applies the decision under the run lease.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from .contracts import CallOutcome, CallStatus, RunStatus
from .policies import ExecutionPolicy, RetryDecision, SideEffectClass, decide_retry


class RemoteState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ReconcileAction(StrEnum):
    WAIT = "wait"
    CLOSE = "close"
    RETRY = "retry"
    MANUAL_ATTENTION = "manual_attention"
    IGNORE_LATE = "ignore_late"


@dataclass(frozen=True, slots=True)
class RemoteObservation:
    state: RemoteState
    provider_event_id: str | None = None
    evidence_ref: str | None = None
    raw_state: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    action: ReconcileAction
    call_status: CallStatus
    call_outcome: CallOutcome
    next_reconcile_at: datetime | None = None
    reason: str = ""


def normalize_remote_state(value: object) -> RemoteState:
    """Map common connector statuses to the stable kernel vocabulary."""
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"running", "pending", "queued", "accepted", "in_progress", "processing"}:
        return RemoteState.RUNNING
    if normalized in {"completed", "complete", "done", "success", "succeeded", "finished"}:
        return RemoteState.COMPLETED
    if normalized in {"failed", "failure", "error", "errored"}:
        return RemoteState.FAILED
    if normalized in {"cancelled", "canceled", "aborted", "stopped"}:
        return RemoteState.CANCELLED
    return RemoteState.UNKNOWN


def reconcile_delay(*, now: datetime, attempt_count: int, policy: ExecutionPolicy) -> datetime:
    """Return bounded exponential backoff for a subsequent reconciliation."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if attempt_count < 0:
        raise ValueError("attempt_count cannot be negative")
    exponent = max(0, attempt_count)
    delay_seconds = policy.reconciliation_initial.total_seconds() * (policy.reconciliation_backoff ** exponent)
    delay = timedelta(seconds=min(delay_seconds, policy.reconciliation_max_delay.total_seconds()))
    return now.astimezone(timezone.utc) + delay


def decide_reconciliation(
    *,
    observation: RemoteObservation,
    run_status: RunStatus,
    call_status: CallStatus,
    call_outcome: CallOutcome,
    side_effect: SideEffectClass,
    safe_to_retry: bool,
    reconcile_attempt_count: int,
    policy: ExecutionPolicy,
    now: datetime,
) -> ReconcileDecision:
    """Compute a deterministic action for one remote observation.

    A terminal Run is immutable from the user's perspective.  A late provider
    callback is retained as evidence but cannot reopen that Run.
    """
    state = observation.state
    # Call terminality is stronger than any later provider observation. A
    # delayed or duplicated RUNNING callback must never reopen a closed Call.
    if call_status is CallStatus.CLOSED:
        return ReconcileDecision(ReconcileAction.IGNORE_LATE, call_status, call_outcome, reason="call already terminal")
    if state is RemoteState.RUNNING and (
        call_status is CallStatus.CANCEL_REQUESTED
        or run_status in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLING}
    ):
        # Cancellation is monotonic. Keep the cancellation fact while the
        # provider is still running so the scheduler can issue cancel/query;
        # never turn a late running observation back into an ordinary wait.
        return ReconcileDecision(
            ReconcileAction.WAIT, CallStatus.CANCEL_REQUESTED,
            CallOutcome.OUTCOME_UNKNOWN,
            next_reconcile_at=reconcile_delay(now=now, attempt_count=reconcile_attempt_count, policy=policy),
            reason="remote still running after cancellation requested",
        )
    if run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.EXPIRED, RunStatus.FAILED}:
        # A terminal Run is immutable, but an unresolved remote Call still
        # needs to converge.  Cancellation/timeout may have happened before
        # the provider accepted the work; retain the Call fact and keep
        # polling/cancelling it without reopening the Run.
        if state is RemoteState.COMPLETED:
            return ReconcileDecision(ReconcileAction.CLOSE, CallStatus.CLOSED, CallOutcome.COMPLETED, reason="remote completed after run terminal")
        if state is RemoteState.FAILED:
            return ReconcileDecision(ReconcileAction.CLOSE, CallStatus.CLOSED, CallOutcome.FAILED, reason="remote failed after run terminal")
        if state is RemoteState.CANCELLED:
            return ReconcileDecision(ReconcileAction.CLOSE, CallStatus.CLOSED, CallOutcome.CANCELLED_CONFIRMED, reason="remote cancellation confirmed after run terminal")
        if state is RemoteState.RUNNING:
            return ReconcileDecision(
                ReconcileAction.WAIT, CallStatus.WAITING_EXTERNAL,
                CallOutcome.REMOTE_RUNNING,
                next_reconcile_at=reconcile_delay(now=now, attempt_count=reconcile_attempt_count, policy=policy),
                reason="remote still running after run terminal",
            )
        return ReconcileDecision(ReconcileAction.MANUAL_ATTENTION, CallStatus.RECONCILING, CallOutcome.OUTCOME_UNKNOWN, reason="remote outcome unknown after run terminal")
    if state is RemoteState.COMPLETED:
        return ReconcileDecision(ReconcileAction.CLOSE, CallStatus.CLOSED, CallOutcome.COMPLETED, reason="remote completed")
    if state is RemoteState.FAILED:
        return ReconcileDecision(ReconcileAction.CLOSE, CallStatus.CLOSED, CallOutcome.FAILED, reason="remote failed")
    if state is RemoteState.CANCELLED:
        return ReconcileDecision(ReconcileAction.CLOSE, CallStatus.CLOSED, CallOutcome.CANCELLED_CONFIRMED, reason="remote cancellation confirmed")
    if state is RemoteState.RUNNING:
        return ReconcileDecision(
            ReconcileAction.WAIT,
            CallStatus.WAITING_EXTERNAL,
            CallOutcome.REMOTE_RUNNING,
            next_reconcile_at=reconcile_delay(now=now, attempt_count=reconcile_attempt_count, policy=policy),
            reason="remote still running",
        )
    retry = decide_retry(
        side_effect=side_effect,
        safe_to_retry=safe_to_retry,
        outcome_unknown=True,
        attempt_no=max(1, reconcile_attempt_count),
        max_attempts=policy.reconciliation_max_attempts,
    )
    if retry.decision is RetryDecision.RETRY:
        return ReconcileDecision(
            ReconcileAction.RETRY,
            CallStatus.RECONCILING,
            CallOutcome.OUTCOME_UNKNOWN,
            next_reconcile_at=reconcile_delay(now=now, attempt_count=reconcile_attempt_count, policy=policy),
            reason=retry.reason,
        )
    return ReconcileDecision(
        ReconcileAction.MANUAL_ATTENTION,
        CallStatus.RECONCILING,
        CallOutcome.OUTCOME_UNKNOWN,
        reason=retry.reason,
    )


def should_recover_run(*, status: RunStatus, updated_at: datetime, now: datetime, policy: ExecutionPolicy) -> bool:
    """Whether a non-terminal Run has exceeded the stuck detector window."""
    if status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.EXPIRED, RunStatus.FAILED}:
        return False
    if updated_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("updated_at and now must be timezone-aware")
    return now.astimezone(timezone.utc) - updated_at.astimezone(timezone.utc) >= policy.stuck_detector

@dataclass(frozen=True, slots=True)
class RunTimeoutDecision:
    status: RunStatus
    cancel_reason: str | None
    action: str


def decide_run_timeout(*, status: RunStatus, unresolved_call_count: int, deadline: datetime | None, now: datetime, cancel_deadline: datetime | None = None, cancel_reason: str | None = None) -> RunTimeoutDecision:
    """Apply deadline/cancel-grace policy without mutating persistence.

    Deadline with unresolved Calls first requests cancellation; a subsequent
    expired cancel grace closes the Run as ``expired`` (deadline) or
    ``cancelled`` (user/parent cancellation).  Terminal Runs are unchanged.
    """
    if unresolved_call_count < 0:
        raise ValueError("unresolved_call_count cannot be negative")
    if now.tzinfo is None or (deadline is not None and deadline.tzinfo is None) or (cancel_deadline is not None and cancel_deadline.tzinfo is None):
        raise ValueError("timestamps must be timezone-aware")
    if status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.EXPIRED, RunStatus.FAILED}:
        return RunTimeoutDecision(status, None, "noop")
    now_utc = now.astimezone(timezone.utc)
    if status in {RunStatus.CANCELLING, RunStatus.CANCEL_REQUESTED} and cancel_deadline is not None and now_utc >= cancel_deadline.astimezone(timezone.utc):
        reason = cancel_reason or ("deadline" if status is RunStatus.CANCELLING else "user")
        if reason not in {"user", "parent", "deadline"}:
            raise ValueError("invalid cancel_reason")
        return RunTimeoutDecision(RunStatus.EXPIRED if reason == "deadline" else RunStatus.CANCELLED, reason, "cancel_timeout")
    if deadline is None or now_utc < deadline.astimezone(timezone.utc):
        return RunTimeoutDecision(status, None, "noop")
    if unresolved_call_count:
        return RunTimeoutDecision(RunStatus.CANCEL_REQUESTED, "deadline", "expiry_requested")
    return RunTimeoutDecision(RunStatus.EXPIRED, "deadline", "expired")
