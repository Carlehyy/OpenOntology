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


def test_deadline_after_user_cancel_is_rejected():
    state = request_cancel(RunState(RunStatus.ACTIVE), CancelReason.USER)
    with pytest.raises(ContractError, match="conflicting"):
        mark_deadline(state)


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


@pytest.mark.parametrize("schema_version", [0, 2, 999, True, 1.0, "1"])
def test_event_registry_rejects_unsupported_schema_version(schema_version):
    with pytest.raises(ContractError, match="schema_version"):
        validate_payload(
            "run.created",
            {"execution_version": "kernel.v1", "conversation_id": "c1"},
            schema_version=schema_version,
        )


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "run.status_changed",
            {"from": "unknown", "to": "active", "reason": "test", "actor": "system", "version": 1},
        ),
        (
            "run.cancel_requested",
            {"reason": "test", "cancel_reason": "operator", "actor": "user"},
        ),
        (
            "run.cancel_timeout",
            {"reason": "test", "cancel_deadline": "t", "unresolved_call_ids": [], "run_terminal_status": "active"},
        ),
        (
            "call.outcome_changed",
                {
                    "call_id": "call-1",
                    "status": "closed",
                "outcome": "outcome_unknown",
                "evidence_ref": "e1",
                "connector_id": "connector",
                "provider_event_id": "event",
            },
        ),
        ("turn.closed", {"turn_id": "t1", "reason": "unknown"}),
        ("step.closed", {"step_id": "s1", "reason": "completed"}),
        (
            "approval.decided",
            {"approval_id": "a1", "decision": "expired", "actor": "user", "decided_at": "t", "authorization_hash": "h"},
        ),
    ],
)
def test_event_registry_rejects_invalid_frozen_values(event_type, payload):
    with pytest.raises(ContractError, match="invalid|must"):
        validate_payload(event_type, payload)


def test_event_envelope_uses_schema_and_value_validation():
    event = EventEnvelope(
        event_id="e1",
        run_id="r1",
        seq=0,
        event_type="call.outcome_changed",
        schema_version=2,
        occurred_at=datetime.now(timezone.utc),
        actor={"kind": "worker"},
        causation_id="c1",
        correlation_id="r1",
        command_id="cmd1",
        idempotency_key="idem1",
        payload={
            "status": "closed",
            "outcome": "outcome_unknown",
            "evidence_ref": "e1",
            "connector_id": "connector",
            "provider_event_id": "event",
        },
        redaction={"mode": "none"},
    )
    with pytest.raises(ContractError, match="schema_version"):
        event.validate()
