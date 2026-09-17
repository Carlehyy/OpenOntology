from datetime import datetime, timedelta, timezone

import pytest

from app.super_assistant.kernel.policies import (
    ChildResult,
    ChildStatus,
    ErrorEnvelope,
    ExecutionPolicy,
    JoinDecision,
    JoinPolicy,
    PolicyError,
    RetryDecision,
    SideEffectClass,
    approval_expires_at,
    decide_child_join,
    decide_retry,
)


def test_defaults_are_typed_and_json_friendly():
    policy = ExecutionPolicy()
    assert policy.run_deadline == timedelta(hours=24)
    assert policy.call_max_attempts == 3
    assert policy.as_dict()["lease_ttl"] == 30.0


def test_policy_rejects_invalid_or_unsafe_defaults():
    with pytest.raises(PolicyError):
        ExecutionPolicy(heartbeat_interval=timedelta(seconds=30))
    with pytest.raises(PolicyError):
        ExecutionPolicy(call_max_attempts=0)


def test_approval_ttl_is_capped_by_run_deadline_and_normalized_to_utc():
    now = datetime(2026, 9, 13, 8, tzinfo=timezone.utc)
    expiry = approval_expires_at(now=now, requested_ttl=timedelta(hours=2), run_deadline=now + timedelta(hours=1))
    assert expiry == now + timedelta(hours=1)
    assert approval_expires_at(now=now, requested_ttl=timedelta(hours=2), run_deadline=now - timedelta(seconds=1)) == now - timedelta(seconds=1)


def test_join_all_waits_for_required_and_optional_failure_does_not_fail_parent():
    result = decide_child_join([
        ChildResult("required", ChildStatus.COMPLETED),
        ChildResult("optional", ChildStatus.FAILED, required=False),
    ], JoinPolicy.ALL)
    assert result.decision is JoinDecision.READY
    assert result.failed_child_ids == ("optional",)


def test_join_any_is_ready_on_first_success_but_required_failure_wins():
    ready = decide_child_join([
        ChildResult("slow", ChildStatus.RUNNING),
        ChildResult("fast", ChildStatus.COMPLETED),
    ], JoinPolicy.ANY)
    assert ready.decision is JoinDecision.READY
    failed = decide_child_join([
        ChildResult("required", ChildStatus.FAILED),
        ChildResult("fast", ChildStatus.COMPLETED, required=False),
    ], JoinPolicy.ANY)
    assert failed.decision is JoinDecision.FAILED


def test_join_does_not_mutate_parent_and_rejects_duplicate_ids():
    children = [ChildResult("one", ChildStatus.RUNNING)]
    result = decide_child_join(children)
    assert result.decision is JoinDecision.PENDING
    assert children[0].status is ChildStatus.RUNNING
    with pytest.raises(PolicyError):
        decide_child_join([ChildResult("one", ChildStatus.RUNNING), ChildResult("one", ChildStatus.COMPLETED)])


def test_unknown_non_idempotent_result_requires_manual_attention():
    result = decide_retry(side_effect=SideEffectClass.NON_IDEMPOTENT_WRITE, safe_to_retry=True, outcome_unknown=True, attempt_no=1, max_attempts=3)
    assert result.decision is RetryDecision.MANUAL_ATTENTION


def test_retry_honors_safety_and_attempt_limit():
    assert decide_retry(side_effect=SideEffectClass.READ_ONLY, safe_to_retry=True, outcome_unknown=False, attempt_no=1, max_attempts=3).decision is RetryDecision.RETRY
    assert decide_retry(side_effect=SideEffectClass.READ_ONLY, safe_to_retry=False, outcome_unknown=False, attempt_no=1, max_attempts=3).decision is RetryDecision.DO_NOT_RETRY
    assert decide_retry(side_effect=SideEffectClass.READ_ONLY, safe_to_retry=True, outcome_unknown=False, attempt_no=3, max_attempts=3).decision is RetryDecision.DO_NOT_RETRY


def test_error_envelope_is_structured_and_serializable():
    error = ErrorEnvelope("provider_timeout", "provider timed out", retryable=True, safe_to_retry=True, details={"attempt": 1})
    assert error.to_dict() == {
        "error_code": "provider_timeout",
        "message": "provider timed out",
        "retryable": True,
        "safe_to_retry": True,
        "details": {"attempt": 1},
        "provider_status": None,
    }
    with pytest.raises(PolicyError):
        ErrorEnvelope("", "missing code")
