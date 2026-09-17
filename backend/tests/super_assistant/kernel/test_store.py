import uuid

import pytest

from app.models.user import User
from app.super_assistant.kernel.contracts import CancelReason, ContractError
from app.super_assistant.kernel.models import ExecutionCall, ExecutionEvent, ExecutionRun, ExecutionDispatchOutbox
from app.super_assistant.kernel.store import (
    IdempotencyConflict,
    acquire_lease,
    append_event,
    cancel_run,
    create_run,
)
from app.super_assistant.models import SuperAssistantConversation
from app.exploration.session_service import write_permission_fingerprint
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion


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


@pytest.mark.parametrize("supplied_marker", [False, 0, None], ids=["false", "zero", "null"])
def test_child_run_origin_marker_cannot_be_overridden_by_context(db, supplied_marker):
    owner, conversation = _owner_and_conversation(db)
    child, _ = create_run(
        db, owner_id=owner.id, conversation_id=conversation.id,
        goal="child", idempotency_key="trusted-origin",
        binding={
            "binding_mode": "assistant_child", "assistant_key": "ontology_agent",
            "context": {"ontology_id": "ont-1", "_kernel_child": supplied_marker},
        },
    )
    assert __import__("json").loads(child.binding_snapshot_ref)["context"]["_kernel_child"] is True


def test_delegated_binding_rejects_fabricated_permission_hash(db):
    owner, conversation = _owner_and_conversation(db)
    project = OntologyProject(
        id="delegated-ontology", name="Delegated", domain="test", created_by=owner.id,
    )
    version = OntologyVersion(
        id="delegated-draft", ontology_id=project.id, version_number="v0",
        node_kind="draft", lifecycle_status="editing", created_by=owner.id,
    )
    db.add_all([project, version]); db.flush()
    with pytest.raises(ContractError, match="write_permission_hash"):
        create_run(
            db, owner_id=owner.id, conversation_id=conversation.id,
            goal="delegated", idempotency_key="delegated-fake-hash",
            binding={
                "binding_mode": "delegated", "ontology_id": project.id,
                "draft_version_id": version.id, "lifecycle": "editing",
                "write_permission_hash": "sha256:caller-controlled",
            },
        )


def test_delegated_binding_persists_server_verified_permission_hash(db):
    owner, conversation = _owner_and_conversation(db)
    project = OntologyProject(
        id="delegated-ontology-valid", name="Delegated", domain="test", created_by=owner.id,
    )
    version = OntologyVersion(
        id="delegated-draft-valid", ontology_id=project.id, version_number="v0",
        node_kind="draft", lifecycle_status="editing", created_by=owner.id,
    )
    db.add_all([project, version]); db.flush()
    permission_hash = write_permission_fingerprint(owner, project, version)
    run, _ = create_run(
        db, owner_id=owner.id, conversation_id=conversation.id,
        goal="delegated", idempotency_key="delegated-valid-hash",
        binding={
            "binding_mode": "delegated", "ontology_id": project.id,
            "draft_version_id": version.id, "lifecycle": "editing",
            "write_permission_hash": permission_hash,
        },
    )
    assert '"write_permission_hash": "' + permission_hash + '"' in run.binding_snapshot_ref


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
    payload = {"call_id": "call-1", "status": "running", "outcome": "remote_running", "evidence_ref": "e", "connector_id": "c", "provider_event_id": "p"}
    first = append_event(db, run, event_type="call.outcome_changed", payload=payload, actor={"kind": "connector"}, command_id="cmd", idempotency_key="event-1", connector_id="c", provider_event_id="p")
    second = append_event(db, run, event_type="call.outcome_changed", payload=payload, actor={"kind": "connector"}, command_id="cmd", idempotency_key="event-1", connector_id="c", provider_event_id="p")
    assert first.event_id == second.event_id
    with pytest.raises(IdempotencyConflict, match="payload hash"):
        append_event(db, run, event_type="call.outcome_changed", payload={**payload, "evidence_ref": "different"}, actor={"kind": "connector"}, command_id="cmd-2", idempotency_key="event-2", connector_id="c", provider_event_id="p")


def test_external_provider_event_cannot_be_reused_by_another_run(db):
    owner, conversation = _owner_and_conversation(db)
    first, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="first", idempotency_key="first")
    second, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="second", idempotency_key="second")
    payload = {"call_id": "call-1", "status": "running", "outcome": "remote_running", "evidence_ref": "e", "connector_id": "c", "provider_event_id": "provider-reused"}
    append_event(db, first, event_type="call.outcome_changed", payload=payload, actor={"kind": "connector"}, command_id="first-event", idempotency_key="first-event", connector_id="c", provider_event_id="provider-reused")
    with pytest.raises(IdempotencyConflict, match="another Run"):
        append_event(db, second, event_type="call.outcome_changed", payload=payload, actor={"kind": "connector"}, command_id="second-event", idempotency_key="second-event", connector_id="c", provider_event_id="provider-reused")


def test_cancel_parent_propagates_to_non_terminal_descendants(db):
    owner, conversation = _owner_and_conversation(db)
    parent, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="parent", idempotency_key="parent-cancel")
    db.commit()
    child, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="child", idempotency_key="child-cancel", parent_run_id=parent.id)
    child.status = "active"
    db.commit()
    cancel_run(db, run_id=parent.id, owner_id=owner.id, reason=CancelReason.USER, idempotency_key="cancel-parent", expected_version=1)
    db.commit()
    db.refresh(child)
    assert child.status == "cancel_requested"
    assert child.cancel_reason == CancelReason.PARENT.value
    assert db.query(ExecutionEvent).filter_by(run_id=child.id, event_type="run.cancel_requested").count() == 1
    assert db.query(ExecutionDispatchOutbox).filter_by(run_id=child.id).count() >= 1


def test_cancel_closes_unsent_offered_call_without_invalid_outcome_pair(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="offered", idempotency_key="offered-cancel")
    db.add(ExecutionCall(run_id=run.id, call_index=0, capability_key="external", capability_revision=1, idempotency_key="offered-call", status="offered", outcome="not_sent"))
    db.commit()
    cancel_run(db, run_id=run.id, owner_id=owner.id, reason=CancelReason.USER, idempotency_key="offered-cancel-command", expected_version=1)
    db.commit()
    call = db.query(ExecutionCall).filter_by(run_id=run.id).one()
    assert call.status == "closed" and call.outcome == "not_sent"
