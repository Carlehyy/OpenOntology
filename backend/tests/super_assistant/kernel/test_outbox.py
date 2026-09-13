import uuid
from app.super_assistant.kernel import outbox
from app.super_assistant.kernel.models import ExecutionDispatchOutbox
from app.super_assistant.kernel.store import create_run
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
