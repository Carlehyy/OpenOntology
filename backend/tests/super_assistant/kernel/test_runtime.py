import asyncio
import uuid
import pytest
from datetime import timezone
from types import SimpleNamespace

from tests.conftest import TestSession

from app.super_assistant.models import SuperAssistantConversation, SuperAssistantMcpServer, SuperAssistantRemoteAgent, SuperAssistantRemoteAgentTask
from app.super_assistant.kernel.models import Artifact, CapabilityRevision, ExecutionEvent, ExecutionRun
from app.super_assistant.kernel.store import create_run
from app.super_assistant.kernel import runtime
from app.super_assistant.kernel import scheduler as kernel_scheduler
from app.super_assistant.kernel.connectors import AgentDescriptor, SessionPolicy
from app.super_assistant.kernel.artifacts import MAX_ARTIFACT_BYTES
from app.models.user import User


def test_rebuild_messages_records_consumed_input_for_crash_recovery(db):
    owner = User(id=str(uuid.uuid4()), username=f"input-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="input")
    db.add(conversation); db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="goal", idempotency_key="input-run")
    db.commit()
    run.status = "waiting_input"; db.commit()
    from app.super_assistant.kernel.store import append_input
    append_input(db, run_id=run.id, owner_id=owner.id, kind="question_answer", question_id="q1", payload={"content": "answer"}, idempotency_key="answer-1")
    db.commit(); db.refresh(run)
    first = runtime._rebuild_messages(db, run)
    db.commit(); db.expire_all(); db.refresh(run)
    second = runtime._rebuild_messages(db, run)
    assert [item["content"] for item in first if item["role"] == "user"].count("answer") == 1
    assert [item["content"] for item in second if item["role"] == "user"].count("answer") == 1
    assert db.query(ExecutionEvent).filter_by(run_id=run.id, event_type="inbox.consumed").count() == 1


def test_user_input_wakes_waiting_run_without_leaving_stranded_pending_item(db):
    owner = User(id=str(uuid.uuid4()), username=f"wake-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="input wake")
    db.add(conversation); db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="goal", idempotency_key="wake-run")
    db.commit()
    run.status = "waiting_input"; db.commit()
    from app.super_assistant.kernel.models import ExecutionDispatchOutbox
    from app.super_assistant.kernel.store import append_input
    item = append_input(db, run_id=run.id, owner_id=owner.id, kind="user_input", payload={"content": "follow up"}, idempotency_key="wake-input")
    db.commit(); db.refresh(run)
    assert item.status == "pending"
    assert run.status == "active"
    wake = db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id, message_ref="command://" + db.query(ExecutionEvent).filter_by(run_id=run.id, event_type="inbox.appended").order_by(ExecutionEvent.seq.desc()).first().command_id).one()
    assert wake.subject.startswith("sa.execution.run.")


def test_request_budget_keeps_required_history_and_records_compaction():
    long_goal = "GOAL-START " + ("g" * 48_000) + " GOAL-END"
    long_child = "CHILD-START " + ("c" * 48_000) + " CHILD-END"
    candidates = [
        runtime._MessageCandidate("system", "system", "system"),
        runtime._MessageCandidate("user", long_goal, "goal"),
        runtime._MessageCandidate("assistant", "old model turn", "assistant"),
        runtime._MessageCandidate("assistant", long_child, "child_result"),
        runtime._MessageCandidate("user", "old user turn", "user_input"),
        runtime._MessageCandidate("user", "LATEST-USER", "user_input"),
    ]

    bounded, trace = runtime._bound_request_candidates(
        candidates, context_content="CTX-START " + ("k" * 48_000) + " CTX-END",
    )
    estimate = sum(runtime._message_token_estimate(item) for item in bounded)
    contents = [item.content for item in bounded]

    assert estimate <= runtime.REQUEST_TOKEN_HARD_CAP
    assert any("GOAL-START" in content for content in contents)
    assert any("GOAL-END" in content for content in contents)
    assert "LATEST-USER" in contents
    assert any("CHILD-START" in content and "CHILD-END" in content for content in contents)
    assert trace["compacted"] is True
    assert trace["hard_cap"] == runtime.REQUEST_TOKEN_HARD_CAP
    assert trace["estimated_tokens"] == estimate
    assert trace["truncated_message_count"] >= 1
    assert "assistant" in trace["dropped_kinds"]


def test_duplicate_activation_with_live_lease_is_a_noop(db, monkeypatch):
    from datetime import timedelta
    from app.super_assistant.kernel.store import acquire_lease
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="duplicate activation")
    run.status = "active"
    acquire_lease(db, run_id=run.id, worker_id="worker-a", ttl=timedelta(minutes=5))
    db.commit()
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "duplicate"}))
    db.expire_all()
    assert db.get(ExecutionRun, run.id).status == "active"


def test_cancel_during_model_call_closes_call_as_unknown_and_never_completes(db, monkeypatch):
    from app.super_assistant.kernel.store import cancel_run
    from app.super_assistant.kernel.contracts import CancelReason
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="cancel race")
    cancelled = {"done": False}

    def late_cancel(*_args, **_kwargs):
        if not cancelled["done"]:
            session = TestSession()
            current = session.get(ExecutionRun, run.id)
            cancel_run(session, run_id=run.id, owner_id=owner.id, reason=CancelReason.USER, idempotency_key="race-cancel", expected_version=current.version)
            session.commit(); session.close()
            cancelled["done"] = True
        return {"content": "late result", "tool_calls": []}

    monkeypatch.setattr(runtime.provider, "chat", late_cancel)
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "cancel-race"}))
    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "cancel_requested"
    call = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, capability_key="model.chat").one()
    attempt = db.query(runtime.ExecutionAttempt).filter_by(call_id=call.id).one()
    assert call.status == "reconciling" and call.outcome == "outcome_unknown"
    assert attempt.finished_at is not None
    assert db.query(Artifact).filter_by(run_id=run.id).count() == 0
    assert db.query(runtime.ExecutionEvent).filter_by(run_id=run.id, event_type="call.outcome_changed").count() == 2


def test_pause_during_model_call_fences_old_activation(db, monkeypatch):
    from app.super_assistant.kernel.store import control_run
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="pause race")
    paused = {"done": False}

    def late_pause(*_args, **_kwargs):
        if not paused["done"]:
            session = TestSession()
            current = session.get(ExecutionRun, run.id)
            control_run(session, run_id=run.id, owner_id=owner.id, action="pause", idempotency_key="race-pause", expected_version=current.version)
            session.commit(); session.close()
            paused["done"] = True
        return {"content": "late result", "tool_calls": []}

    monkeypatch.setattr(runtime.provider, "chat", late_pause)
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "pause-race"}))
    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "paused" and persisted.lease_owner is None
    call = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, capability_key="model.chat").one()
    assert call.status == "reconciling" and call.outcome == "outcome_unknown"


def test_stale_worker_failure_reconciles_only_its_fenced_call(db, monkeypatch):
    from datetime import timedelta
    from app.super_assistant.kernel.store import acquire_lease
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="stale worker")
    run.status = "active"
    token = acquire_lease(db, run_id=run.id, worker_id="worker-a", ttl=timedelta(minutes=5))
    turn = runtime.ExecutionTurn(run_id=run.id, turn_no=0, status="open")
    db.add(turn); db.flush()
    step = runtime.ExecutionStep(turn_id=turn.id, step_no=0, status="open")
    db.add(step); db.flush()
    call = runtime.ExecutionCall(run_id=run.id, turn_id=turn.id, step_id=step.id, call_index=0, capability_key="model.chat", capability_revision=1, idempotency_key="stale-call", status="running", outcome="accepted", lease_epoch=token.epoch)
    db.add(call); db.flush()
    attempt = runtime.ExecutionAttempt(call_id=call.id, attempt_no=1, provider_status="started")
    db.add(attempt)
    run.lease_epoch += 1; run.lease_owner = "worker-b"
    db.commit()
    runtime._mark_run_failed(run.id, RuntimeError("stale"), expected_token=token)
    db.expire_all()
    assert db.get(ExecutionRun, run.id).status == "active"
    db.refresh(call); db.refresh(attempt); db.refresh(step)
    assert call.status == "reconciling" and call.outcome == "outcome_unknown"
    assert attempt.finished_at is not None and step.status == "closed"


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


def test_kernel_hub_delegation_creates_bound_child_run(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="委派任务")
    result = runtime._invoke_hub_delegation(db, run, {"assistant": "ontology_agent", "task": "分析本体", "context": {"ontology_id": "ont-1"}})
    payload = __import__("json").loads(result)
    assert payload["status"] == "queued"
    child = db.get(ExecutionRun, payload["child_run_id"])
    assert child is not None
    assert child.parent_run_id == run.id
    snapshot = __import__("json").loads(child.binding_snapshot_ref)
    assert snapshot["binding_mode"] == "assistant_child"
    assert snapshot["context"]["_kernel_child"] is True
    assert payload["child_run_id"] in (db.get(ExecutionRun, run.id).required_child_ids or [])


def test_kernel_hub_delegation_idempotency_is_per_tool_invocation(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="重复委派")
    first = __import__("json").loads(runtime._invoke_hub_delegation(
        db, run, {"assistant": "ontology_agent", "task": "发送相同通知", "context": {"ontology_id": "ont-1"}}, invocation_ref="step-1:tool-0",
    ))
    second = __import__("json").loads(runtime._invoke_hub_delegation(
        db, run, {"assistant": "ontology_agent", "task": "发送相同通知", "context": {"ontology_id": "ont-1"}}, invocation_ref="step-2:tool-0",
    ))
    replay = __import__("json").loads(runtime._invoke_hub_delegation(
        db, run, {"assistant": "ontology_agent", "task": "发送相同通知", "context": {"ontology_id": "ont-1"}}, invocation_ref="step-1:tool-0",
    ))
    assert first["child_run_id"] != second["child_run_id"]
    assert replay["child_run_id"] == first["child_run_id"]
    assert replay["replayed"] is True


def test_kernel_exploration_delegation_requires_binding_before_child_creation(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="业务澄清")
    result = runtime._invoke_hub_delegation(
        db, run, {"assistant": "exploration", "task": "梳理订单流程", "context": {}},
    )
    payload = __import__("json").loads(result)
    assert payload["status"] == "needs_input"
    assert payload["reason"] == "delegation_binding_required"
    assert db.query(ExecutionRun).filter(ExecutionRun.parent_run_id == run.id).count() == 0


def test_kernel_ontology_delegation_requires_binding_before_child_creation(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="本体委派")
    result = runtime._invoke_hub_delegation(
        db, run, {"assistant": "ontology_agent", "task": "分析本体", "context": {}},
    )
    payload = __import__("json").loads(result)
    assert payload["status"] == "needs_input"
    assert payload["reason"] == "delegation_binding_required"
    assert db.query(ExecutionRun).filter(ExecutionRun.parent_run_id == run.id).count() == 0


def test_kernel_hub_child_result_is_merged_into_parent(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="委派任务")
    result = runtime._invoke_hub_delegation(db, run, {"assistant": "ontology_agent", "task": "分析本体", "context": {"ontology_id": "ont-1"}})
    child_id = __import__("json").loads(result)["child_run_id"]
    db.commit()
    def fake_hub(*_args, **_kwargs):
        if False:
            yield None
        return __import__("json").dumps({"status": "answered", "content": "子助手完成"}, ensure_ascii=False)
    monkeypatch.setattr(runtime.delegation, "run_delegation_tool", fake_hub)
    asyncio.run(runtime.process_execution_message({"run_id": child_id, "command_id": "child-run"}))
    db.expire_all()
    assert db.get(ExecutionRun, child_id).status == "completed"
    parent = db.get(ExecutionRun, run.id)
    parent.status = "waiting_external"
    db.commit()
    from app.super_assistant.kernel.recovery import join_ready_parents_once
    join_ready_parents_once(db)
    db.expire_all()
    assert db.get(ExecutionRun, run.id).status == "active"
    merged = db.query(Artifact).filter_by(run_id=run.id, kind="child.result").one()
    assert "子助手完成" in merged.inline_content


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


def test_kernel_model_transient_failures_create_retry_attempts(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="retry model")
    calls = {"count": 0}
    def flaky(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("temporary gateway failure")
        return {"content": "最终结果"}
    monkeypatch.setattr(runtime.provider, "chat", flaky)
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "model-retry"}))
    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "completed"
    attempts = db.query(runtime.ExecutionAttempt).join(runtime.ExecutionCall).filter(runtime.ExecutionCall.run_id == run.id).order_by(runtime.ExecutionAttempt.attempt_no).all()
    assert len(attempts) == 3
    assert [item.safe_to_retry for item in attempts] == [True, True, False]


def test_model_retry_rechecks_cancel_before_next_provider_call(db, monkeypatch):
    from app.super_assistant.kernel.contracts import CancelReason
    from app.super_assistant.kernel.store import cancel_run
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="retry cancel")
    calls = {"count": 0}

    def flaky_then_cancel(*_args, **_kwargs):
        calls["count"] += 1
        session = TestSession()
        current = session.get(ExecutionRun, run.id)
        if calls["count"] == 1:
            cancel_run(session, run_id=run.id, owner_id=owner.id, reason=CancelReason.USER, idempotency_key="retry-cancel", expected_version=current.version)
            session.commit(); session.close()
            raise RuntimeError("temporary gateway failure")
        session.close()
        return {"content": "错误的第二次调用"}

    monkeypatch.setattr(runtime.provider, "chat", flaky_then_cancel)
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "retry-cancel"}))
    db.expire_all()
    assert calls["count"] == 1
    assert db.get(ExecutionRun, run.id).status == "cancel_requested"


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


def test_kernel_runtime_materializes_external_wait_as_reconcilable_call(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="调用外部智能体")
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {"content": "已发起", "tool_calls": [], "external_call": {"target_ref": "agent:research", "reason": "等待外部结果"}})
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "external-wait"}))
    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "waiting_external"
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()
    assert external.status == "waiting_external"
    assert external.outcome == "remote_running"
    inbox = db.query(runtime.InboxItem).filter_by(run_id=run.id, kind="external_event").one()
    assert inbox.call_id == external.id


def test_kernel_external_call_is_dispatched_through_registry_and_wakes_run(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="委派远程研究")
    target = f"fake.external_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交远程任务", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "研究项目"},
    })
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "external-dispatch"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()

    class FakeConnector:
        def descriptor(self):
            return AgentDescriptor(
                agent_id="fake-agent", key=target, revision=1, transport="rap.v1",
                session_policy=SessionPolicy.RESUMABLE,
            )

        async def invoke(self, **kwargs):
            assert kwargs["run_id"] == run.id
            assert '"message": "研究项目"' in kwargs["input_ref"]
            return {"status": "answered", "content": "远程研究完成"}

        async def cancel(self, **kwargs):
            return {"status": "unsupported"}

        async def query_status(self, **kwargs):
            return {"status": "unsupported"}

    runtime.connector_registry.register(FakeConnector())
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))

    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    assert persisted.status == "active"
    db.refresh(external)
    assert external.status == "closed"
    assert external.outcome == "completed"
    artifact = db.query(Artifact).filter_by(run_id=run.id, kind="external.result").one()
    assert artifact.inline_content == "远程研究完成"


def test_external_result_after_cancel_preserves_remote_ref_for_cancellation(db, monkeypatch):
    from app.super_assistant.kernel.store import cancel_run
    from app.super_assistant.kernel.contracts import CancelReason
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="取消远程任务")
    target = f"fake.cancel_race_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {"content": "已提交", "tool_calls": [], "external_call": {"target_ref": target, "message": "执行"}})
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "cancel-race-external"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()

    class CancelRaceConnector:
        def descriptor(self):
            return AgentDescriptor(agent_id="cancel-race", key=target, revision=1, transport="rap.v1", session_policy=SessionPolicy.RESUMABLE, supports_cancel=True, supports_query_status=True)
        async def invoke(self, **kwargs):
            session = TestSession()
            current = session.get(ExecutionRun, run.id)
            cancel_run(session, run_id=run.id, owner_id=owner.id, reason=CancelReason.USER, idempotency_key="external-cancel-race", expected_version=current.version)
            session.commit(); session.close()
            return {"status": "running", "remote_task_ref": "remote-42"}
        async def cancel(self, **kwargs):
            return {"status": "cancelled", "remote_task_ref": kwargs["remote_task_ref"]}
        async def query_status(self, **kwargs):
            return {"status": "running", "remote_task_ref": kwargs["remote_task_ref"]}

    runtime.connector_registry.register(CancelRaceConnector())
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))
    db.expire_all(); db.refresh(external)
    assert db.get(ExecutionRun, run.id).status == "cancel_requested"
    assert external.status == "waiting_external" and external.remote_task_ref == "remote-42"
    observed_reconcile_at = external.next_reconcile_at
    if observed_reconcile_at is not None and observed_reconcile_at.tzinfo is None:
        observed_reconcile_at = observed_reconcile_at.replace(tzinfo=timezone.utc)
    assert observed_reconcile_at is not None and observed_reconcile_at <= runtime._now()
    assert db.query(runtime.ExecutionAttempt).filter_by(call_id=external.id).one().finished_at is not None


def test_kernel_external_structured_artifact_is_persisted_and_verified(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="返回结构化结果")
    target = f"fake.artifact_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {"content": "", "tool_calls": [], "external_call": {"target_ref": target, "message": "生成 JSON"}})
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "artifact-dispatch"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()
    class ArtifactConnector:
        def descriptor(self):
            return AgentDescriptor(agent_id="artifact-agent", key=target, revision=1, transport="rap.v1")
        async def invoke(self, **kwargs):
            return {"status": "answered", "artifacts": [{"kind": "report", "mime_type": "application/json", "content": {"ok": True}}]}
        async def cancel(self, **kwargs):
            return {"status": "unsupported"}
        async def query_status(self, **kwargs):
            return {"status": "unsupported"}
    runtime.connector_registry.register(ArtifactConnector())
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))
    db.expire_all()
    artifact = db.query(Artifact).filter_by(run_id=run.id, kind="report").one()
    assert artifact.integrity_status == "verified"
    assert artifact.inline_content == '{"ok": true}'


def test_external_declared_artifact_rejects_oversized_metadata_before_db_write():
    run = SimpleNamespace(owner_id="owner-1", id="run-1")
    call = SimpleNamespace(id="call-1", target_ref="remote-1")
    with pytest.raises(ValueError, match="size exceeds"):
        runtime._persist_external_artifacts(
            object(), run, call,
            [{"storage_ref": "s3://bucket/owner-1/run-1/artifact", "size": MAX_ARTIFACT_BYTES + 1, "checksum": "sha256:x"}],
        )


def test_external_declared_artifact_rejects_storage_ref_over_database_limit():
    run = SimpleNamespace(owner_id="owner-1", id="run-1")
    call = SimpleNamespace(id="call-1", target_ref="remote-1")
    with pytest.raises(ValueError, match="storage_ref is too long"):
        runtime._persist_external_artifacts(
            object(), run, call,
            [{"storage_ref": "s3://bucket/" + "x" * 990, "size": 1, "checksum": "sha256:x"}],
        )


def test_kernel_resolves_rap_pull_agent_as_idempotent_external_task(db, monkeypatch):
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="委派回连助手")
    agent = SuperAssistantRemoteAgent(
        owner_id=owner.id, key=f"remote.pull_{uuid.uuid4().hex[:8]}", label="回连助手",
        description="pull", endpoint=None, mode="pull", enabled=True,
        timeout_seconds=60, agent_key_hash=f"hash-{uuid.uuid4().hex}",
    )
    db.add(agent); db.commit()
    call = SimpleNamespace(target_ref=agent.id, capability_revision=1)
    connector = runtime._resolve_external_connector(db, run, call)
    assert connector is not None
    assert connector.descriptor().supports_query_status is True
    db.commit()  # the pull callback uses a separate worker session
    result = asyncio.run(connector.invoke(
        run_id=run.id, call_id="call-pull-1",
        input_ref='{"message":"执行回连任务","session_ref":null}', deadline=None,
    ))
    assert result["status"] == "running"
    assert result["remote_task_ref"].startswith("rap-pull:")
    assert db.query(SuperAssistantRemoteAgentTask).count() == 1
    duplicate = asyncio.run(connector.invoke(
        run_id=run.id, call_id="call-pull-1",
        input_ref='{"message":"执行回连任务","session_ref":null}', deadline=None,
    ))
    assert duplicate["remote_task_ref"] == result["remote_task_ref"]
    assert db.query(SuperAssistantRemoteAgentTask).count() == 1


def test_kernel_external_async_result_is_reconciled_after_remote_acceptance(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="委派 multica 长任务")
    target = f"fake.external_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "执行长任务"},
    })
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "async-external"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()

    class AsyncConnector:
        def descriptor(self):
            return AgentDescriptor(
                agent_id="fake-async", key=target, revision=1, transport="multica",
                session_policy=SessionPolicy.RESUMABLE, supports_cancel=True,
                supports_query_status=True,
            )

        async def invoke(self, **kwargs):
            return {"status": "running", "remote_task_ref": '{"issue_ref":"MYW-1"}', "provider_status": "queued"}

        async def query_status(self, **kwargs):
            return {"status": "completed", "content": "长任务完成", "remote_task_ref": kwargs["remote_task_ref"], "provider_event_id": "evt-1"}

        async def cancel(self, **kwargs):
            return {"status": "cancelled", "remote_task_ref": kwargs["remote_task_ref"]}

    runtime.connector_registry.register(AsyncConnector())
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))
    db.expire_all()
    assert db.get(ExecutionRun, run.id).status == "waiting_external"
    db.refresh(external)
    assert external.status == "waiting_external"
    assert external.remote_task_ref == '{"issue_ref":"MYW-1"}'

    asyncio.run(runtime.reconcile_execution_message({
        "run_id": run.id, "call_id": external.id, "connector_id": "fake-async",
        "remote_state": "completed", "provider_event_id": "evt-1", "content": "长任务完成",
    }))
    db.expire_all()
    assert db.get(ExecutionRun, run.id).status == "active"
    artifact = db.query(Artifact).filter_by(run_id=run.id, kind="external.result").one()
    assert artifact.inline_content == "长任务完成"


def test_kernel_scheduler_polls_due_external_call_and_wakes_run(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="定时查询 multica")
    target = f"fake.external_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "后台执行"},
    })
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "scheduler-external"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()

    class PollConnector:
        def descriptor(self):
            return AgentDescriptor(
                agent_id="fake-poll", key=target, revision=1, transport="multica",
                session_policy=SessionPolicy.RESUMABLE, supports_query_status=True,
            )

        async def invoke(self, **kwargs):
            return {"status": "running", "remote_task_ref": "poll-ref"}

        async def query_status(self, **kwargs):
            return {"status": "completed", "content": "轮询完成", "artifacts": [{"kind": "poll.report", "mime_type": "application/json", "content": {"ok": True}}], "provider_event_id": "poll-1"}

        async def cancel(self, **kwargs):
            return {"status": "unsupported"}

    runtime.connector_registry.register(PollConnector())
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))
    db.expire_all()
    assert db.get(ExecutionRun, run.id).status == "waiting_external"
    monkeypatch.setattr(kernel_scheduler, "SessionLocal", TestSession)
    asyncio.run(asyncio.sleep(0))
    # Make the due time deterministic for the scheduler's synchronous poll.
    db.refresh(external)
    external.next_reconcile_at = runtime._now()
    db.commit()
    kernel_scheduler._poll_external_calls_once()
    db.expire_all()
    # Polling is producer-only. The durable NATS reconciler consumer is the
    # sole state transition path, so the observation must be queued first.
    from app.super_assistant.kernel.models import ExecutionDispatchOutbox
    queued = db.query(ExecutionDispatchOutbox).filter_by(
        run_id=run.id, subject="sa.execution.reconcile",
    ).one()
    assert queued.payload["call_id"] == external.id
    assert queued.payload["remote_state"] == "completed"


def test_scheduler_continues_cancel_after_run_reaches_terminal_state(db, monkeypatch):
    """Cancel grace must not abandon a still-running remote side effect."""
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="终止后仍需取消远端任务")
    target = f"fake.cancel_terminal_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "执行"},
    })
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "terminal-cancel"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()
    cancelled: list[str] = []

    class TerminalCancelConnector:
        def descriptor(self):
            return AgentDescriptor(
                agent_id="terminal-cancel-agent", key=target, revision=1,
                transport="rap.v1", session_policy=SessionPolicy.RESUMABLE,
                supports_cancel=True, supports_query_status=True,
            )

        async def invoke(self, **kwargs):
            return {"status": "running", "remote_task_ref": "terminal-ref"}

        async def cancel(self, **kwargs):
            cancelled.append(kwargs["remote_task_ref"])
            return {"status": "cancelled", "remote_task_ref": kwargs["remote_task_ref"]}

        async def query_status(self, **kwargs):
            return {"status": "running", "remote_task_ref": kwargs["remote_task_ref"]}

    runtime.connector_registry.register(TerminalCancelConnector())
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))
    db.expire_all(); db.refresh(external)
    persisted_run = db.get(ExecutionRun, run.id)
    persisted_run.status = "cancelled"
    external.next_reconcile_at = runtime._now()
    db.commit()
    monkeypatch.setattr(kernel_scheduler, "SessionLocal", TestSession)
    kernel_scheduler._poll_external_calls_once()
    assert cancelled == ["terminal-ref"]


def test_external_acceptance_without_bounded_remote_ref_requires_manual_attention(db, monkeypatch):
    """An opaque provider id that cannot be persisted must not strand a Call."""
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="远端引用过长")
    target = f"fake.long_ref_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "执行"},
    })
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "long-ref"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()

    class LongRefConnector:
        def descriptor(self):
            return AgentDescriptor(agent_id="long-ref-agent", key=target, revision=1, transport="rap.v1")

        async def invoke(self, **kwargs):
            return {"status": "running", "remote_task_ref": "r" * 2001}

        async def cancel(self, **kwargs):
            return {"status": "unsupported"}

        async def query_status(self, **kwargs):
            return {"status": "unknown"}

    runtime.connector_registry.register(LongRefConnector())
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))
    db.expire_all(); db.refresh(external)
    assert external.status == "reconciling"
    assert external.outcome == "outcome_unknown"
    assert external.manual_attention is True
    assert external.remote_task_ref is None


def test_duplicate_external_delivery_recovers_interrupted_running_call(db, monkeypatch):
    """A call left RUNNING by a crashed worker must enter reconciliation."""
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="恢复中断的远端调用")
    target = f"fake.interrupted_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "执行"},
    })
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "interrupted-call"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()
    external.status = "running"
    external.outcome = "accepted"
    db.commit()

    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))
    db.expire_all(); db.refresh(external)
    assert external.status == "reconciling"
    assert external.outcome == "outcome_unknown"
    assert external.manual_attention is True


def test_kernel_scheduler_claims_due_call_once_across_workers(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="并发对账")
    call = runtime.ExecutionCall(
        run_id=run.id, call_index=0, capability_key="external.agent", capability_revision=1,
        target_ref="remote-agent", input_snapshot_ref="input", side_effect_class="external_async",
        idempotency_key="poll-claim", status="waiting_external", outcome="remote_running",
        remote_task_ref="remote-task", next_reconcile_at=runtime._now(),
    )
    db.add(call); db.commit()
    monkeypatch.setattr(kernel_scheduler, "SessionLocal", TestSession)
    first = kernel_scheduler._claim_external_call(call.id, "kernel-reconciler:a")
    second = kernel_scheduler._claim_external_call(call.id, "kernel-reconciler:b")
    assert first is not None and first[1] == call.id
    assert second is None
    kernel_scheduler._release_external_call(call.id, "kernel-reconciler:a")
    assert kernel_scheduler._claim_external_call(call.id, "kernel-reconciler:b") is not None
    kernel_scheduler._release_external_call(call.id, "kernel-reconciler:b")


def test_kernel_mcp_tool_uses_owner_scoped_manifest_connector(db, monkeypatch):
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="查询 MCP")
    from app.super_assistant.mcp_client import namespaced_tool_name

    server = SuperAssistantMcpServer(
        owner_id=owner.id, name="research", display_name="Research MCP",
        transport="streamable_http", url="https://mcp.example/tools",
        tool_manifest=[{"name": "search", "description": "search"}], enabled=True,
    )
    db.add(server)
    db.commit()
    target = namespaced_tool_name(server.name, "search")
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "调用 MCP", "tool_calls": [{"id": "mcp-1", "name": target, "arguments": {"q": "OpenOntology"}}],
    })
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "mcp-wait"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()

    async def fake_call_tool(**kwargs):
        assert kwargs["tool_name"] == "search"
        assert kwargs["arguments"] == {"q": "OpenOntology"}
        return "MCP 结果"

    monkeypatch.setattr("app.super_assistant.mcp_client.call_tool", fake_call_tool)
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))

    db.expire_all()
    assert db.get(ExecutionRun, run.id).status == "active"
    artifact = db.query(Artifact).filter_by(run_id=run.id, kind="external.result").one()
    assert artifact.inline_content == "MCP 结果"


def test_waiting_call_cannot_retarget_after_remote_agent_revision_change(db, monkeypatch):
    from app.super_assistant.remote_agent_service import create_agent_row, update_agent
    from app.super_assistant.schemas import RemoteAgentCreate, RemoteAgentUpdate

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="冻结远端配置")
    agent, _ = create_agent_row(db, owner.id, RemoteAgentCreate(
        key=f"remote.freeze_{uuid.uuid4().hex[:8]}", label="freeze", endpoint="https://1.1.1.1/run", token="secret",
    ))
    old_call = SimpleNamespace(target_ref=agent.id, capability_revision=1)
    connector = runtime._resolve_external_connector(db, run, old_call)
    assert connector is not None and connector.endpoint == "https://1.1.1.1/run"
    db.commit()

    update_agent(db, owner.id, agent.id, RemoteAgentUpdate(endpoint="https://8.8.8.8/run"))
    db.expire_all(); db.refresh(agent)
    assert db.query(CapabilityRevision).filter_by(key=agent.key, revision=1).one().enabled is False
    assert runtime._resolve_external_connector(db, run, old_call) is None
    new_revision = runtime._capability_revision_for_target(db, owner.id, agent.key)
    assert new_revision == 2
    new_connector = runtime._resolve_external_connector(db, run, SimpleNamespace(target_ref=agent.id, capability_revision=new_revision))
    assert new_connector is not None and new_connector.endpoint == "https://8.8.8.8/run"
