import uuid
from datetime import timedelta
from app.super_assistant.kernel import outbox
from app.super_assistant.kernel.models import ExecutionDispatchOutbox
from app.super_assistant.kernel.store import create_run
from app.super_assistant.kernel.store import _now
from app.super_assistant.models import SuperAssistantConversation
from app.models.user import User

def test_exhausted_outbox_publishes_structured_dlq(db, monkeypatch):
    owner = User(id=str(uuid.uuid4()), username=f"dlq-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="dlq")
    db.add(conversation); db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="x", idempotency_key=f"dlq-{uuid.uuid4().hex}")
    db.flush(); row = db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id).one()
    row.attempt_count = outbox.MAX_ATTEMPTS - 1; row.claim_token = "publisher"; db.commit()
    seen = []
    monkeypatch.setattr(outbox, "dispatch_execution", lambda subject, payload, *, command_id: seen.append((subject, payload, command_id)))
    outbox._record_failure(db, row.id, "publisher", "nats down")
    db.refresh(row)
    assert row.status == "dead" and seen[0][0] == "sa.execution.dlq"
    assert seen[0][1]["schema"] == "sa.execution.dlq.v1" and seen[0][1]["outbox_id"] == row.id
    assert seen[0][1]["replay_requires_operator"] is True


def test_publish_claims_each_row_immediately_before_dispatch(db, monkeypatch):
    owner = User(id=str(uuid.uuid4()), username=f"publish-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="publish")
    db.add(conversation); db.flush()
    runs = []
    for index in range(2):
        run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal=f"x-{index}", idempotency_key=f"publish-{uuid.uuid4().hex}")
        runs.append(run)
    db.commit()
    seen = []
    monkeypatch.setattr(outbox, "dispatch_execution", lambda subject, payload, *, command_id: seen.append(command_id))
    assert outbox.publish_due_once(db, batch_size=2, publisher_id="publisher") == 2
    rows = [db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id).one() for run in runs]
    assert all(row.status == "published" and row.claim_token is None for row in rows)
    assert len(seen) == 2 and len(set(seen)) == 2


def test_publish_preserves_reconcile_payload_and_legacy_call_id(db, monkeypatch):
    owner = User(id=str(uuid.uuid4()), username=f"payload-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="payload")
    db.add(conversation); db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="x", idempotency_key=f"payload-{uuid.uuid4().hex}")
    initial = db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id).one()
    initial.status = "published"
    db.add(ExecutionDispatchOutbox(
        id=str(uuid.uuid4()), command_id="reconcile:test", run_id=run.id,
        subject="sa.execution.reconcile", message_ref="reconcile://call-1/event-1",
        payload={"call_id": "call-1", "remote_state": "completed"},
        status="pending", next_attempt_at=_now(),
    ))
    db.add(ExecutionDispatchOutbox(
        id=str(uuid.uuid4()), command_id="call:test", run_id=run.id,
        subject="sa.execution.call.owner", message_ref="call://legacy-call",
        payload={}, status="pending", next_attempt_at=_now(),
    ))
    db.commit()
    seen = []
    monkeypatch.setattr(outbox, "dispatch_execution", lambda subject, payload, *, command_id: seen.append((subject, payload, command_id)))
    assert outbox.publish_due_once(db, batch_size=2, publisher_id="publisher") == 2
    reconcile = next(payload for subject, payload, _ in seen if subject == "sa.execution.reconcile")
    legacy = next(payload for subject, payload, _ in seen if subject == "sa.execution.call.owner")
    assert reconcile["remote_state"] == "completed" and reconcile["call_id"] == "call-1"
    assert legacy["call_id"] == "legacy-call"


def test_expired_claim_recovery_is_locked_and_requeues(db):
    owner = User(id=str(uuid.uuid4()), username=f"recover-outbox-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="recover-outbox")
    db.add(conversation); db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="x", idempotency_key=f"recover-outbox-{uuid.uuid4().hex}")
    row = db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id).one()
    row.status, row.claim_token, row.claim_expires_at = "claimed", "stale", _now() - timedelta(seconds=1)
    db.commit()
    assert outbox.recover_expired_claims(db) == 1
    db.refresh(row)
    assert row.status == "pending" and row.claim_token is None
