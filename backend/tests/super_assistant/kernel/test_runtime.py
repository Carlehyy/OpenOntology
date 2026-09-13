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


def _runtime_fixture(db, monkeypatch, *, goal="multi step", max_steps=8):
    owner = User(
        id=str(uuid.uuid4()), username=f"runtime-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin",
    )
    db.add(owner)
    db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="runtime")
    db.add(conversation)
    db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal=goal, idempotency_key=uuid.uuid4().hex, max_steps=max_steps)
    db.commit()
    monkeypatch.setattr(runtime, "SessionLocal", TestSession)
    monkeypatch.setattr(runtime, "select_llm_model_config", lambda **_: SimpleNamespace(id="fake-config"))
    monkeypatch.setattr(runtime, "llm_call_kwargs", lambda _: {"provider": "openai", "model": "fake-model", "api_key": "x"})
    monkeypatch.setattr(runtime.delegation, "delegation_tools", lambda *_args, **_kwargs: [])
    return run, owner, conversation


def test_kernel_runtime_runs_multiple_model_steps_and_persists_each_step(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch)
    results = iter([
        {"content": "先查资料", "tool_calls": [{"id": "tool-1", "name": "delegate_to_assistant", "arguments": {"assistant_key": "researcher", "task": "查资料"}}]},
        {"content": "最终答案", "tool_calls": []},
    ])
    monkeypatch.setattr(runtime, "_invoke_hub_delegation", lambda *_args, **_kwargs: "资料结果")
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: next(results))

    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "multi-step"}))

    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "completed"
    turns = db.query(runtime.ExecutionTurn).filter_by(run_id=run.id).all()
    assert len(turns) == 1
    assert db.query(runtime.ExecutionStep).filter_by(turn_id=turns[0].id).count() == 2
    assert db.query(runtime.ExecutionCall).filter_by(run_id=run.id).count() == 2
    assert db.query(runtime.ExecutionAttempt).join(runtime.ExecutionCall).filter(runtime.ExecutionCall.run_id == run.id).count() == 2


def test_kernel_runtime_yields_for_input_then_resumes_in_a_new_turn(db, monkeypatch):
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="需要用户确认")
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {"content": "请补充信息", "tool_calls": [], "wait_for_input": {"question_id": "q-1", "question": "项目名称？"}})
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "wait-input"}))
    db.expire_all()
    waiting = db.get(ExecutionRun, run.id)
    assert waiting.status == "waiting_input"
    assert waiting.wait_reason == "question"

    from app.super_assistant.kernel.store import append_input
    append_input(db, run_id=run.id, owner_id=owner.id, kind="question_answer", question_id="q-1", payload={"content": "OpenOntology"}, idempotency_key="answer-1")
    db.commit()
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {"content": "已确认 OpenOntology", "tool_calls": []})
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "resume-input"}))

    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "completed"
    turns = db.query(runtime.ExecutionTurn).filter_by(run_id=run.id).order_by(runtime.ExecutionTurn.turn_no).all()
    assert [turn.turn_no for turn in turns] == [0, 1]
    assert all(turn.status == "closed" for turn in turns)


def test_kernel_runtime_honors_max_steps_and_yields_retry(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, max_steps=1)
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {"content": "继续", "tool_calls": [{"id": "tool-1", "name": "delegate_to_assistant", "arguments": {}}]})
    monkeypatch.setattr(runtime, "_invoke_hub_delegation", lambda *_args, **_kwargs: "结果")
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "max-step"}))
    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "waiting_retry"
    assert persisted.wait_reason == "max_steps"
    turn = db.query(runtime.ExecutionTurn).filter_by(run_id=run.id).one()
    assert turn.close_reason == "waiting_retry"
    step = db.query(runtime.ExecutionStep).filter_by(turn_id=turn.id).one()
    assert step.close_reason == "waiting_retry"
