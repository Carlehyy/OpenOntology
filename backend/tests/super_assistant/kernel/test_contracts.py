from datetime import datetime, timezone

import pytest

from app.super_assistant.kernel.contracts import (
    CallOutcome,
    ExpiryAction,
    CallStatus,
    CancelReason,
    ContractError,
    ExpiryPolicy,
    RunState,
    RunStatus,
    begin_cancelling,
    expire_question,
    finish_cancelling,
    mark_active,
    mark_deadline,
    request_cancel,
    transition_call,
    validate_call,
)
from app.super_assistant.kernel.events import EventEnvelope, validate_payload


def test_user_cancel_wins_and_is_terminal():
    state = RunState(RunStatus.ACTIVE, unresolved_call_count=1)
    state = request_cancel(state, CancelReason.USER)
    state = begin_cancelling(state)
    assert finish_cancelling(state).status == RunStatus.CANCELLED


def test_deadline_with_unresolved_call_expires_after_cancelling():
    state = RunState(RunStatus.ACTIVE, unresolved_call_count=1)
    state = mark_deadline(state)
    assert state.status == RunStatus.CANCEL_REQUESTED
    state = finish_cancelling(begin_cancelling(state))
    assert state.status == RunStatus.EXPIRED


def test_deadline_without_unresolved_call_expires_immediately():
    state = mark_deadline(RunState(RunStatus.WAITING_INPUT))
    assert state.status == RunStatus.EXPIRED


def test_conflicting_cancel_is_rejected():
    state = request_cancel(RunState(RunStatus.ACTIVE), CancelReason.USER)
    with pytest.raises(ContractError, match="conflicting"):
        request_cancel(state, CancelReason.DEADLINE)


def test_question_reask_only_once_then_fails_branch():
    state, action = expire_question(RunState(RunStatus.WAITING_INPUT), ExpiryPolicy.REASK_ONCE)
    assert action == ExpiryAction.REASK
    assert state.status == RunStatus.WAITING_INPUT
    state = RunState(RunStatus.WAITING_INPUT, question_attempts=1)
    state, action = expire_question(state, ExpiryPolicy.REASK_ONCE)
    assert action == ExpiryAction.FAIL_BRANCH
    assert state.status == RunStatus.ACTIVE


def test_call_status_outcome_axes_are_validated():
    validate_call(CallStatus.RUNNING, CallOutcome.REMOTE_RUNNING)
    assert transition_call(
        CallStatus.RUNNING,
        CallOutcome.REMOTE_RUNNING,
        new_status=CallStatus.RECONCILING,
    ) == (CallStatus.RECONCILING, CallOutcome.REMOTE_RUNNING)
    with pytest.raises(ContractError):
        validate_call(CallStatus.CLOSED, CallOutcome.OUTCOME_UNKNOWN)


def test_event_registry_requires_payload_and_redaction_reference():
    with pytest.raises(ContractError, match="missing payload"):
        validate_payload("run.status_changed", {"to": "active"})
    event = EventEnvelope(
        event_id="e1", run_id="r1", seq=0, event_type="assistant.delta",
        schema_version=1, occurred_at=datetime.now(timezone.utc),
        actor={"kind": "worker"}, causation_id="c1", correlation_id="r1",
        command_id="cmd1", idempotency_key="idem1",
        payload={"attempt_id": "a1", "delta_seq": 0, "content_ref": "obj://x"},
        redaction={"mode": "reference", "content_ref": "obj://x", "checksum": "sha256:x"},
    )
    event.validate()


def test_event_registry_rejects_reference_without_checksum():
    event = EventEnvelope(
        event_id="e1", run_id="r1", seq=0, event_type="run.created",
        schema_version=1, occurred_at=datetime.now(timezone.utc),
        actor={"kind": "system"}, causation_id="c1", correlation_id="r1",
        command_id="cmd1", idempotency_key="idem1",
        payload={"execution_version": "kernel.v1", "conversation_id": "c1"},
        redaction={"mode": "reference", "content_ref": "obj://x"},
    )
    with pytest.raises(ContractError, match="checksum"):
        event.validate()


def test_event_registry_rejects_oversized_inline_payload():
    with pytest.raises(ContractError, match="64 KiB"):
        validate_payload("assistant.message", {"attempt_id": "a1", "message_ref": "x" * (64 * 1024)})
