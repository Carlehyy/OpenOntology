from datetime import datetime, timedelta, timezone

from app.super_assistant.kernel.contracts import CallOutcome, CallStatus, RunStatus
from app.super_assistant.kernel.policies import ExecutionPolicy, SideEffectClass
from app.super_assistant.kernel.reconciler import (
    ReconcileAction,
    RemoteObservation,
    RemoteState,
    decide_reconciliation,
    decide_run_timeout,
    normalize_remote_state,
    reconcile_delay,
    should_recover_run,
)


def test_normalize_remote_state_and_bounded_backoff():
    assert normalize_remote_state("in-progress") is RemoteState.RUNNING
    assert normalize_remote_state("succeeded") is RemoteState.COMPLETED
    assert normalize_remote_state("wat") is RemoteState.UNKNOWN
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    policy = ExecutionPolicy(reconciliation_initial=timedelta(seconds=5), reconciliation_max_delay=timedelta(seconds=12))
    assert reconcile_delay(now=now, attempt_count=0, policy=policy) == now + timedelta(seconds=5)
    assert reconcile_delay(now=now, attempt_count=4, policy=policy) == now + timedelta(seconds=12)


def test_reconcile_completion_and_late_result_are_terminally_safe():
    now = datetime.now(timezone.utc)
    policy = ExecutionPolicy()
    completed = decide_reconciliation(
        observation=RemoteObservation(RemoteState.COMPLETED), run_status=RunStatus.WAITING_EXTERNAL,
        call_status=CallStatus.RECONCILING, call_outcome=CallOutcome.OUTCOME_UNKNOWN,
        side_effect=SideEffectClass.EXTERNAL_ASYNC, safe_to_retry=False,
        reconcile_attempt_count=1, policy=policy, now=now,
    )
    assert (completed.action, completed.call_status, completed.call_outcome) == (ReconcileAction.CLOSE, CallStatus.CLOSED, CallOutcome.COMPLETED)
    late = decide_reconciliation(
        observation=RemoteObservation(RemoteState.COMPLETED), run_status=RunStatus.CANCELLED,
        call_status=CallStatus.CLOSED, call_outcome=CallOutcome.CANCELLED_CONFIRMED,
        side_effect=SideEffectClass.EXTERNAL_ASYNC, safe_to_retry=False,
        reconcile_attempt_count=1, policy=policy, now=now,
    )
    assert late.action is ReconcileAction.IGNORE_LATE


def test_unknown_external_result_requires_manual_attention_after_budget():
    decision = decide_reconciliation(
        observation=RemoteObservation(RemoteState.UNKNOWN), run_status=RunStatus.WAITING_EXTERNAL,
        call_status=CallStatus.RECONCILING, call_outcome=CallOutcome.OUTCOME_UNKNOWN,
        side_effect=SideEffectClass.EXTERNAL_ASYNC, safe_to_retry=True,
        reconcile_attempt_count=12, policy=ExecutionPolicy(), now=datetime.now(timezone.utc),
    )
    assert decision.action is ReconcileAction.MANUAL_ATTENTION


def test_stuck_detector_ignores_terminal_runs():
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=10)
    policy = ExecutionPolicy(stuck_detector=timedelta(minutes=5))
    assert should_recover_run(status=RunStatus.ACTIVE, updated_at=old, now=now, policy=policy)
    assert not should_recover_run(status=RunStatus.COMPLETED, updated_at=old, now=now, policy=policy)


def test_run_timeout_requests_cancel_before_expiry_and_closes_after_grace():
    now = datetime.now(timezone.utc)
    assert decide_run_timeout(status=RunStatus.ACTIVE, unresolved_call_count=1, deadline=now - timedelta(seconds=1), now=now).action == "expiry_requested"
    closed = decide_run_timeout(status=RunStatus.CANCELLING, unresolved_call_count=1, deadline=now - timedelta(seconds=1), cancel_deadline=now - timedelta(seconds=1), now=now)
    assert (closed.status, closed.action) == (RunStatus.EXPIRED, "cancel_timeout")
