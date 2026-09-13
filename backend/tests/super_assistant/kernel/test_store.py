import uuid

import pytest

from app.models.user import User
from app.super_assistant.kernel.contracts import CancelReason, ContractError
from app.super_assistant.kernel.models import ExecutionEvent, ExecutionRun, ExecutionDispatchOutbox
from app.super_assistant.kernel.store import (
    IdempotencyConflict,
    acquire_lease,
    append_event,
    cancel_run,
    create_run,
)
from app.super_assistant.models import SuperAssistantConversation


def _owner_and_conversation(db):
    owner = User(
        id=str(uuid.uuid4()), username=f"kernel-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin",
    )
    db.add(owner)
    db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="kernel")
    db.add(conversation)
    db.flush()
    return owner, conversation


def test_create_run_is_idempotent_and_writes_event_and_outbox(db):
    owner, conversation = _owner_and_conversation(db)
    run, replayed = create_run(
        db, owner_id=owner.id, conversation_id=conversation.id,
        goal="prepare a report", idempotency_key="create-1",
    )
    db.commit()
    same, replayed = create_run(
        db, owner_id=owner.id, conversation_id=conversation.id,
        goal="prepare a report", idempotency_key="create-1",
    )
    assert replayed is True
    assert same.id == run.id
    assert db.query(ExecutionEvent).filter_by(run_id=run.id).count() == 1
    assert db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id).count() == 1


def test_create_run_rejects_same_key_with_different_payload(db):
    owner, conversation = _owner_and_conversation(db)
    create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="one", idempotency_key="same")
    db.commit()
    with pytest.raises(IdempotencyConflict):
        create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="two", idempotency_key="same")


def test_child_run_binding_is_persisted_and_evented(db):
    owner, conversation = _owner_and_conversation(db)
    parent, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="parent", idempotency_key="parent")
    db.commit()
    child, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="child", idempotency_key="child", parent_run_id=parent.id)
    db.commit()
    db.refresh(parent)
    assert child.parent_run_id == parent.id
    assert child.id in parent.required_child_ids
    assert db.query(ExecutionEvent).filter_by(run_id=parent.id, event_type="run.child_bound").count() == 1
    assert db.query(ExecutionEvent).filter_by(run_id=child.id, event_type="run.child_bound").count() == 1


def test_cancel_is_versioned_idempotent_and_conflicting_reason_rejected(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="one", idempotency_key="create")
    db.commit()
    run = cancel_run(db, run_id=run.id, owner_id=owner.id, reason=CancelReason.USER, idempotency_key="cancel", expected_version=1)
    db.commit()
    assert run.status == "cancel_requested"
    replay = cancel_run(db, run_id=run.id, owner_id=owner.id, reason=CancelReason.USER, idempotency_key="cancel", expected_version=1)
    assert replay.id == run.id
    with pytest.raises(ContractError, match="conflicting"):
        cancel_run(db, run_id=run.id, owner_id=owner.id, reason=CancelReason.DEADLINE, idempotency_key="cancel-2", expected_version=2)


def test_lease_epoch_fences_old_worker(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="one", idempotency_key="create")
    db.commit()
    first = acquire_lease(db, run_id=run.id, worker_id="worker-a")
    db.commit()
    db.refresh(run)
    # Expire the first lease in the test fixture, then acquire a new epoch.
    run.lease_expires_at = run.lease_expires_at.replace(year=2000)
    db.commit()
    second = acquire_lease(db, run_id=run.id, worker_id="worker-b")
    assert second.epoch > first.epoch
    with pytest.raises(ContractError, match="stale lease"):
        append_event(
            db, run, event_type="run.pause_requested",
            payload={"reason": "pause", "pause_reason": "user", "actor": "user"},
            actor={"kind": "worker"}, command_id="c", idempotency_key="i", lease=first,
        )


def test_external_provider_event_is_idempotent_but_hash_conflicts_are_rejected(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="one", idempotency_key="create")
    db.flush()
    payload = {"status": "running", "outcome": "remote_running", "evidence_ref": "e", "connector_id": "c", "provider_event_id": "p"}
    first = append_event(db, run, event_type="call.outcome_changed", payload=payload, actor={"kind": "connector"}, command_id="cmd", idempotency_key="event-1", connector_id="c", provider_event_id="p")
    second = append_event(db, run, event_type="call.outcome_changed", payload=payload, actor={"kind": "connector"}, command_id="cmd", idempotency_key="event-1", connector_id="c", provider_event_id="p")
    assert first.event_id == second.event_id
    with pytest.raises(IdempotencyConflict, match="payload hash"):
        append_event(db, run, event_type="call.outcome_changed", payload={**payload, "evidence_ref": "different"}, actor={"kind": "connector"}, command_id="cmd-2", idempotency_key="event-2", connector_id="c", provider_event_id="p")
