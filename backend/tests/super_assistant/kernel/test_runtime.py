import asyncio
import uuid
from types import SimpleNamespace

from tests.conftest import TestSession

from app.super_assistant.models import SuperAssistantConversation
from app.super_assistant.kernel.models import Artifact, ExecutionEvent, ExecutionRun
from app.super_assistant.kernel.store import create_run
from app.super_assistant.kernel import runtime
from app.models.user import User


def test_kernel_activation_persists_attempt_artifact_and_completion(db, monkeypatch):
    owner = User(
        id=str(uuid.uuid4()), username=f"runtime-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin",
    )
    db.add(owner)
    db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="runtime")
    db.add(conversation)
    db.flush()
    run, _ = create_run(
        db, owner_id=owner.id, conversation_id=conversation.id,
        goal="write a concise answer", idempotency_key="run-1",
    )
    db.commit()

    fake_config = SimpleNamespace(id="fake-config")
    monkeypatch.setattr(runtime, "SessionLocal", TestSession)
    monkeypatch.setattr(runtime, "select_llm_model_config", lambda **_: fake_config)
    monkeypatch.setattr(runtime, "llm_call_kwargs", lambda _: {"provider": "openai", "model": "fake-model", "api_key": "x"})
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {"content": "done"})

    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "run-command"}))

    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "completed"
    assert db.query(Artifact).filter_by(run_id=run.id, business_status="success").count() == 1
    event_types = [e.event_type for e in db.query(ExecutionEvent).filter_by(run_id=run.id).order_by(ExecutionEvent.seq)]
    assert event_types[:2] == ["run.created", "run.status_changed"]
    assert "assistant.message" in event_types
    assert event_types[-1] == "run.status_changed"
