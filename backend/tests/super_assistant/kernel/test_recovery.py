import uuid
from datetime import timedelta

from app.models.user import User
from app.super_assistant.models import SuperAssistantConversation
from app.super_assistant.kernel.models import ExecutionCall, ExecutionEvent, InboxItem
from app.super_assistant.kernel.policies import ExecutionPolicy
from app.super_assistant.kernel.recovery import expire_due_runs_once, expire_inbox_once, join_ready_parents_once, recover_stuck_runs_once
from app.super_assistant.kernel.contracts import CancelReason
from app.super_assistant.kernel.store import acquire_lease, cancel_run, create_run, renew_lease


def _owner_and_conversation(db):
    owner = User(id=str(uuid.uuid4()), username=f"recover-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner)
    db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="recovery")
    db.add(conversation)
    db.flush()
    return owner, conversation


def test_expiry_requests_cancel_then_closes_after_grace(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="deadline", idempotency_key="deadline", deadline=None)
    run.deadline = run.created_at - timedelta(seconds=1)
    db.commit()
    assert expire_due_runs_once(db, policy=ExecutionPolicy(cancel_grace=timedelta(seconds=2))) == 1
    db.refresh(run)
    assert run.status == "expired"  # no unresolved call: no grace required


def test_stuck_run_is_fenced_and_woken(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="stuck", idempotency_key="stuck")
    run.updated_at = run.created_at - timedelta(minutes=10)
    run.lease_expires_at = run.created_at - timedelta(minutes=10)
    db.commit()
    assert recover_stuck_runs_once(db, policy=ExecutionPolicy(stuck_detector=timedelta(minutes=5))) == 1
    db.refresh(run)
    assert run.lease_owner == "kernel:recovery"
    assert run.lease_epoch == 1


def test_parent_join_wakes_waiting_parent_after_child_completion(db):
    owner, conversation = _owner_and_conversation(db)
    parent, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="parent", idempotency_key="parent")
    db.flush()
    child, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="child", idempotency_key="child", parent_run_id=parent.id)
    db.flush()
    parent.status = "waiting_external"
    child.status = "completed"
    db.commit()
    assert join_ready_parents_once(db) == 1
    db.refresh(parent)
    assert parent.status == "active"


def test_parent_join_maps_queued_child_without_blocking_scheduler(db):
    owner, conversation = _owner_and_conversation(db)
    parent, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="parent", idempotency_key="parent-queued")
    db.flush()
    child, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="child", idempotency_key="child-queued", parent_run_id=parent.id)
    parent.status = "waiting_external"
    db.commit()
    assert join_ready_parents_once(db) == 0
    db.refresh(parent)
    assert parent.status == "waiting_external"


def test_parent_cancel_is_not_overwritten_by_child_join(db):
    owner, conversation = _owner_and_conversation(db)
    parent, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="parent", idempotency_key="parent-cancel-join")
    db.flush()
    child, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="child", idempotency_key="child-cancel-join", parent_run_id=parent.id)
    parent.status = "cancel_requested"
    child.status = "cancelled"
    db.commit()
    assert join_ready_parents_once(db) == 0
    db.refresh(parent)
    assert parent.status == "cancel_requested"


def test_expiry_with_unresolved_call_sets_cancel_grace(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="cancel grace", idempotency_key="grace")
    db.flush()
    db.add(ExecutionCall(run_id=run.id, call_index=0, capability_key="remote", capability_revision=1, idempotency_key="call-1", status="running", outcome="accepted"))
    run.deadline = run.created_at - timedelta(seconds=1)
    db.commit()
    assert expire_due_runs_once(db, policy=ExecutionPolicy(cancel_grace=timedelta(seconds=30))) == 1
    db.refresh(run)
    assert run.status == "cancel_requested"
    assert run.cancel_reason == "deadline"
    assert run.cancel_deadline is not None


def test_heartbeat_renews_same_fencing_epoch(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="heartbeat", idempotency_key="heartbeat")
    db.commit()
    token = acquire_lease(db, run_id=run.id, worker_id="worker-a")
    renewed = renew_lease(db, token=token, ttl=timedelta(seconds=45))
    assert renewed.epoch == token.epoch
    assert renewed.expires_at > token.expires_at


def test_user_cancel_has_grace_deadline_and_scheduler_closes_it(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="cancel", idempotency_key="cancel")
    db.commit()
    cancel_run(db, run_id=run.id, owner_id=owner.id, reason=CancelReason.USER, idempotency_key="cancel-cmd", expected_version=1)
    run.cancel_deadline = run.created_at - timedelta(seconds=1)
    db.commit()
    assert expire_due_runs_once(db) == 1
    db.refresh(run)
    assert run.status == "cancelled"


def test_expired_question_is_reasked_once_then_fails_run(db):
    owner, conversation = _owner_and_conversation(db)
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="question", idempotency_key="question")
    run.status = "waiting_input"
    run.wait_reason = "question"
    db.add(InboxItem(
        run_id=run.id, kind="question_answer", priority=30, status="pending",
        question_id="q1", target_ref="q1", payload={"question": "需要什么？"}, source="system",
        expires_at=run.created_at - timedelta(seconds=1), expiry_policy="reask_once",
        idempotency_key="question-wait",
    ))
    db.commit()
    assert expire_inbox_once(db) == 1
    db.refresh(run)
    assert run.status == "waiting_input"
    retry = db.query(InboxItem).filter(InboxItem.run_id == run.id, InboxItem.status == "pending").one()
    assert retry.expiry_policy == "fail_run"
    retry.expires_at = run.created_at - timedelta(seconds=1)
    db.commit()
    assert expire_inbox_once(db) == 1
    db.refresh(run)
    assert run.status == "failed"
    assert db.query(ExecutionEvent).filter_by(run_id=run.id, event_type="inbox.expired").count() == 2
