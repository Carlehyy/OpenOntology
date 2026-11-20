import asyncio
import json
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
    from app.super_assistant.kernel.models import InboxItem as KernelInboxItem
    db.add(KernelInboxItem(
        run_id=run.id, kind="question_answer", priority=30, status="pending",
        question_id="q1", target_ref="q1", payload={"question": "需要什么？"},
        source="system", accepted_at=run.created_at, idempotency_key="question-q1",
    ))
    db.commit()
    from app.super_assistant.kernel.store import append_input
    item = append_input(db, run_id=run.id, owner_id=owner.id, kind="question_answer", question_id="q1", payload={"content": "answer"}, idempotency_key="answer-1")
    db.commit(); db.refresh(run)
    first = runtime._rebuild_messages(db, run)
    db.commit(); db.expire_all(); db.refresh(run)
    second = runtime._rebuild_messages(db, run)
    assert [item["content"] for item in first if item["role"] == "user"].count("answer") == 1
    assert [item["content"] for item in second if item["role"] == "user"].count("answer") == 1
    consumed = db.query(ExecutionEvent).filter_by(run_id=run.id, event_type="inbox.consumed").all()
    # 回答行的消费必须恰好一次（两次 rebuild 不重复消费）；问题行的关闭
    # 会额外产生一条 consumed 事件，属新契约的预期行为。
    assert len([e for e in consumed if (e.payload or {}).get("inbox_id") == item.id]) == 1


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


def test_kernel_external_call_requests_approval_and_defers_dispatch(db, monkeypatch):
    from app.super_assistant.kernel.models import Approval as KernelApproval, ExecutionDispatchOutbox

    run, _, _ = _runtime_fixture(db, monkeypatch, goal="调用外部智能体")
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {"content": "已发起", "tool_calls": [], "external_call": {"target_ref": "agent:research", "reason": "等待外部结果"}})
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "external-wait"}))
    db.expire_all()
    persisted = db.get(ExecutionRun, run.id)
    # Baseline §10：非 read_only 副作用先过审批——Run 停在 waiting_approval，
    # Call 已物化为可对账的 waiting_external，但派发 Outbox 必须不存在。
    assert persisted.status == "waiting_approval"
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()
    # provider 从未被接触：Call 只能落在 offered/not_sent，不能伪造
    # remote_running 外部观测（baseline §2/§3.2）。
    assert external.status == "offered"
    assert external.outcome == "not_sent"
    approval = db.query(KernelApproval).filter_by(run_id=run.id, call_id=external.id, status="pending").one()
    assert approval.capability_revision == external.capability_revision
    assert approval.parameter_hash
    assert db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id, command_id=f"external-dispatch:{external.id}").count() == 0


def _gated_external_fixture(db, monkeypatch, *, goal):
    from app.super_assistant.kernel.models import Approval as KernelApproval

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal=goal)
    target = f"fake.gated_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交待审", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "执行写入"},
    })
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": f"gated-{target}"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, target_ref=target).one()
    approval = db.query(KernelApproval).filter_by(call_id=external.id).one()
    db.refresh(run)
    assert run.status == "waiting_approval"
    return run, owner, external, approval


def test_kernel_approval_denied_closes_gated_call_and_replans(db, monkeypatch):
    from app.super_assistant.kernel.models import ExecutionDispatchOutbox
    from app.super_assistant.kernel.router import decide_kernel_approval
    from app.super_assistant.kernel.schemas import ApprovalDecisionRequest

    run, owner, external, approval = _gated_external_fixture(db, monkeypatch, goal="需要审批的写入")
    decide_kernel_approval(
        run.id, approval.id, ApprovalDecisionRequest(decision="denied", idempotency_key=f"deny-{approval.id}"),
        db=db, user=owner, if_match=f'"{run.version}"', idempotency_header=f"deny-{approval.id}",
    )
    db.expire_all()
    external = db.get(runtime.ExecutionCall, external.id)
    # provider 从未被接触：closed/not_sent 是可证明的诚实终态。
    assert (external.status, external.outcome) == ("closed", "not_sent")
    assert db.get(ExecutionRun, run.id).status == "active"
    assert db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id, command_id=f"external-dispatch:{external.id}").count() == 0


def test_kernel_approval_approved_defers_then_dispatches_gated_call(db, monkeypatch):
    from app.super_assistant.kernel.models import ExecutionDispatchOutbox
    from app.super_assistant.kernel.router import decide_kernel_approval
    from app.super_assistant.kernel.schemas import ApprovalDecisionRequest

    run, owner, external, approval = _gated_external_fixture(db, monkeypatch, goal="批准后执行")
    version = run.version
    decide_kernel_approval(
        run.id, approval.id, ApprovalDecisionRequest(decision="approved", idempotency_key=f"ok-{approval.id}"),
        db=db, user=owner, if_match=f'"{version}"', idempotency_header=f"ok-{approval.id}",
    )
    db.expire_all()
    # 批准只解锁派发：Run 回 waiting_external 等结果自然唤醒；Call 保持
    # offered/not_sent，由 dispatch 消费端翻到 running/accepted。
    assert db.get(ExecutionRun, run.id).status == "waiting_external"
    external = db.get(runtime.ExecutionCall, external.id)
    assert external.status == "offered"
    assert external.outcome == "not_sent"
    assert db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id, command_id=f"external-dispatch:{external.id}").count() == 1
    # 同键同版本的幂等重放不得产生第二条派发指令（异版本重放按迟到
    # 决定契约返回 409 并保留原决定）。
    decide_kernel_approval(
        run.id, approval.id, ApprovalDecisionRequest(decision="approved", idempotency_key=f"ok-{approval.id}"),
        db=db, user=owner, if_match=f'"{version}"', idempotency_header=f"ok-{approval.id}",
    )
    assert db.query(ExecutionDispatchOutbox).filter_by(run_id=run.id, command_id=f"external-dispatch:{external.id}").count() == 1


def test_expiring_gated_approval_closes_undispatched_call(db, monkeypatch):
    from datetime import timedelta

    from app.super_assistant.kernel.models import Approval as KernelApproval
    from app.super_assistant.kernel.recovery import expire_inbox_once

    run, _, external, approval = _gated_external_fixture(db, monkeypatch, goal="审批过期")
    inbox_row = db.query(runtime.InboxItem).filter_by(approval_id=approval.id).one()
    past = run.created_at - timedelta(seconds=1)
    approval.expires_at = past
    inbox_row.expires_at = past
    db.commit()
    assert expire_inbox_once(db) == 1
    db.expire_all()
    external = db.get(runtime.ExecutionCall, external.id)
    # 审批 TTL 到期：未派发的 Call 收敛为 closed/not_sent，Run 按 fail_run 失败。
    assert (external.status, external.outcome) == ("closed", "not_sent")
    assert db.get(KernelApproval, approval.id).status == "expired"
    assert db.get(ExecutionRun, run.id).status == "failed"


def test_kernel_external_call_is_dispatched_through_registry_and_wakes_run(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="委派远程研究")
    target = f"fake.external_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交远程任务", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "研究项目"},
    })
    # 本测试聚焦派发链路；审批门行为由专项测试覆盖，按已批准放行。
    monkeypatch.setattr(runtime, "requires_approval", lambda *args, **kwargs: False)
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
    # 本测试聚焦对账链路；审批门行为由专项测试覆盖，按已批准放行。
    monkeypatch.setattr(runtime, "requires_approval", lambda *args, **kwargs: False)
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
    # 本测试聚焦 scheduler 轮询；审批门行为由专项测试覆盖，按已批准放行。
    monkeypatch.setattr(runtime, "requires_approval", lambda *args, **kwargs: False)
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
            return {"status": "completed", "content": "轮询完成", "artifacts": [{"kind": "poll.report", "mime_type": "application/json", "content": {"ok": True}}], "provider_event_id": "poll-1",
                    "remote_task_ref": '{"issue_ref":"PRJ-1","task_id":"run-1","profile_revision_id":"rev-poll"}'}

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
    # The enriched reference returned by the poll rides along so the durable
    # reconciler can persist the learned original task identity.
    assert json.loads(queued.payload["remote_task_ref"])["task_id"] == "run-1"


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


def test_scheduler_fences_multica_call_when_task_identity_unavailable(db, monkeypatch):
    """An ambiguous provider history (two runs, no pinned id) is a durable
    identity verdict: the Call is fenced into manual attention under a static
    reason instead of re-polling every cycle, and the diagnosis issues reads
    only — no cancel/create POST against the provider."""
    from app.super_assistant import multica_client, multica_service
    from app.super_assistant.kernel.models import ExecutionCall, InboxItem

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 来源不唯一")
    _multica_client_fixture(monkeypatch, "ws-a")
    profile = _create_multica_profile(
        db, monkeypatch, owner.id, name="不唯一实例",
        base_url="http://127.0.0.1:19071", workspace_id="ws-a", token="token-a")
    multica_service.activate_profile(db, owner.id, profile.id)
    revision = multica_service._current_revision(db, profile.id).id
    db.commit()

    call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2, binding=revision,
        remote_task_ref=json.dumps({"issue_ref": "MYW-20", "task_id": None,
                                     "issue_id": "20202020-2020-4202-8202-202020202020",
                                     "profile_revision_id": revision}),
        suffix="ambiguous",
    )
    call.status = "waiting_external"; call.outcome = "remote_running"
    run.status = "waiting_external"
    db.add(call)
    db.commit()

    traffic: list[str] = []
    monkeypatch.setattr(
        multica_client, "get_issue",
        lambda base_url, token, workspace_id, issue_ref: (
            traffic.append("get_issue"),
            {"id": "20202020-2020-4202-8202-202020202020", "identifier": issue_ref,
             "workspace_id": workspace_id, "status": "in_progress"})[1])
    monkeypatch.setattr(
        multica_client, "list_task_runs",
        lambda base_url, token, workspace_id, issue_id: (
            traffic.append("task_runs"),
            [{"id": "task-a", "issue_id": issue_id, "workspace_id": workspace_id,
              "status": "completed", "result": "旧结果"},
             {"id": "task-b", "issue_id": issue_id, "workspace_id": workspace_id,
              "status": "in_progress", "result": "重跑中"}])[1])
    monkeypatch.setattr(multica_client, "cancel_task",
                        lambda *_args, **_kwargs: traffic.append("cancel") or {})
    monkeypatch.setattr(multica_client, "create_issue",
                        lambda *_args, **_kwargs: traffic.append("create") or {})

    monkeypatch.setattr(kernel_scheduler, "SessionLocal", TestSession)
    kernel_scheduler._poll_external_calls_once()
    db.expire_all()
    fenced = db.query(ExecutionCall).filter_by(id=call.id).one()
    assert fenced.manual_attention is True
    assert fenced.status == "reconciling" and fenced.outcome == "outcome_unknown"
    # Static operator-facing reason; the raw upstream message is not persisted.
    assert fenced.remote_observed_state_ref == "multica_task_identity_unavailable"
    assert db.query(InboxItem).filter_by(
        run_id=run.id, kind="manual_attention").count() == 1
    # The ambiguity was diagnosed with reads only: no provider POST fired.
    assert sorted(set(traffic)) == ["get_issue", "task_runs"]

    first_round = list(traffic)
    kernel_scheduler._poll_external_calls_once()
    assert traffic == first_round  # fenced Calls are skipped: no polling loop


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
    # 本测试聚焦 MCP 连接器；审批门行为由专项测试覆盖，按已批准放行。
    monkeypatch.setattr(runtime, "requires_approval", lambda *args, **kwargs: False)
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


def test_kernel_mcp_resolve_passes_managed_weknora_fields(db, monkeypatch):
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="受管 WeKnora 检索")
    from app.super_assistant.mcp_client import namespaced_tool_name

    scope = {
        "origin": "https://weknora.example.test", "endpoint_id": "wk-managed-1",
        "knowledge_base_ids": ["kb-1"], "tools": ["search_knowledge"],
    }
    server = SuperAssistantMcpServer(
        owner_id=owner.id, name="platform_weknora_aa11bb22",
        transport="streamable_http",
        url="https://weknora.example.test/mcp/wk-managed-1",
        weknora_endpoint_id="wk-managed-1", weknora_scope=dict(scope),
        constrained=True, last_test_status="success",
        tool_manifest=[{"name": "search_knowledge", "description": "search"}],
        enabled=True, manifest_revision=1,
    )
    db.add(server)
    db.commit()
    target = namespaced_tool_name(server.name, "search_knowledge")
    connector = runtime._resolve_external_connector(
        db, run, SimpleNamespace(target_ref=target, capability_revision=1))
    assert connector is not None
    assert connector.weknora_endpoint_id == "wk-managed-1"
    assert dict(connector.weknora_scope) == scope
    assert connector.owner_id == owner.id
    assert connector.builtin_key is None


def test_kernel_mcp_resolve_denies_unverified_managed_weknora(db, monkeypatch):
    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="未验证 WeKnora")
    from app.super_assistant.mcp_client import namespaced_tool_name

    server = SuperAssistantMcpServer(
        owner_id=owner.id, name="platform_weknora_cc33dd44",
        transport="streamable_http",
        url="https://weknora.example.test/mcp/wk-managed-2",
        weknora_endpoint_id="wk-managed-2",
        weknora_scope={"endpoint_id": "wk-managed-2", "knowledge_base_ids": ["kb-1"]},
        constrained=True, last_test_status="success",
        tool_manifest=[{"name": "search_knowledge", "description": "search"}],
        enabled=True, manifest_revision=1,
    )
    db.add(server)
    db.commit()
    target = namespaced_tool_name(server.name, "search_knowledge")
    call = SimpleNamespace(target_ref=target, capability_revision=1)
    assert runtime._resolve_external_connector(db, run, call) is not None
    assert target in {item["name"] for item in runtime._kernel_connector_tool_schemas(db, owner.id)}
    # constrained 撤销（验证失败复位）→ fail-closed，不得套用存量摘要 fallback
    server.constrained = False
    db.commit()
    assert runtime._resolve_external_connector(db, run, call) is None
    assert target not in {item["name"] for item in runtime._kernel_connector_tool_schemas(db, owner.id)}
    # 最近一次验证非 success（未验证/过期失败）同样拒绝
    server.constrained = True
    server.last_test_status = "error"
    db.commit()
    assert runtime._resolve_external_connector(db, run, call) is None


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


def _multica_client_fixture(monkeypatch, *workspace_ids):
    """Patch the actual multica client: profile creation/activation performs a
    live /api/me plus workspace-visibility check against these workspaces."""
    from app.super_assistant import multica_client

    monkeypatch.setattr(
        multica_client, "fetch_me",
        lambda *_args, **_kwargs: {"name": "fixture", "email": "fixture@test.invalid"},
    )
    monkeypatch.setattr(
        multica_client, "list_workspaces",
        lambda *_args, **_kwargs: [
            {"id": workspace, "name": workspace, "slug": workspace}
            for workspace in workspace_ids
        ],
    )


def _create_multica_profile(db, monkeypatch, owner_id, *, name, base_url, workspace_id, token):
    from app.super_assistant import multica_service
    from app.super_assistant.schemas import MulticaProfileCreate

    return multica_service.create_profile(db, owner_id, MulticaProfileCreate(
        name=name, base_url=base_url, workspace_id=workspace_id,
        workspace_name=workspace_id, token=token, enabled=True,
    ))


def _multica_call_row(run, *, target, capability_revision, binding=None, remote_task_ref=None, suffix=""):
    from app.super_assistant.kernel.models import ExecutionCall

    return ExecutionCall(
        run_id=run.id, call_index=0,
        capability_key=f"external:{target}", capability_revision=capability_revision,
        target_ref=target, input_snapshot_ref='{"arguments":{}}',
        side_effect_class="external_async", idempotency_key=f"multica-{target}-{suffix}",
        status="offered", outcome="not_sent",
        multica_profile_revision_id=binding, remote_task_ref=remote_task_ref,
    )


def test_multica_bound_call_executes_queries_and_cancels_source_after_profile_switch(db, monkeypatch):
    """Integration through the real service: after A→B the bound Call's
    execute/query/cancel keep hitting source A's endpoint and credentials."""
    from app.super_assistant import multica_client, multica_service
    from app.super_assistant.kernel.contracts import ContractError

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 来源绑定")
    _multica_client_fixture(monkeypatch, "ws-a", "ws-b")
    profile_a = _create_multica_profile(
        db, monkeypatch, owner.id, name="实例 A",
        base_url="http://127.0.0.1:19001", workspace_id="ws-a", token="token-a")
    profile_b = _create_multica_profile(
        db, monkeypatch, owner.id, name="实例 B",
        base_url="http://127.0.0.1:19002", workspace_id="ws-b", token="token-b")
    multica_service.activate_profile(db, owner.id, profile_a.id)
    revision_a = multica_service._current_revision(db, profile_a.id).id
    db.commit()

    call = _multica_call_row(
        run, target="multica_create_task",
        capability_revision=runtime._capability_revision_for_target(db, owner.id, "multica_create_task"),
        binding=revision_a, suffix="integration",
    )
    db.add(call)
    db.commit()  # closures resolve the bound source in separate worker sessions

    seen: list[tuple] = []
    issue_uuid = "11111111-1111-4111-8111-111111111111"

    def fake_create_issue(base_url, token, workspace_id, **kwargs):
        seen.append(("create", base_url, token, workspace_id))
        return {"id": issue_uuid, "identifier": "MYW-1", "title": "t", "status": "open"}

    def fake_get_issue(base_url, token, workspace_id, issue_ref):
        seen.append(("get_issue", base_url, token, workspace_id))
        return {"id": issue_uuid, "identifier": "MYW-1",
                "workspace_id": workspace_id, "status": "in_progress"}

    def fake_list_task_runs(base_url, token, workspace_id, issue_id):
        seen.append(("task_runs", base_url, token, workspace_id))
        return [{"id": "task-1", "issue_id": issue_id, "workspace_id": workspace_id,
                 "status": "in_progress", "result": "部分结果"}]

    def fake_cancel_task(base_url, token, workspace_id, issue_id, task_id):
        seen.append(("cancel", base_url, token, workspace_id))
        return {"id": task_id, "issue_id": issue_id, "workspace_id": workspace_id,
                "status": "cancelled"}

    monkeypatch.setattr(multica_client, "create_issue", fake_create_issue)
    monkeypatch.setattr(multica_client, "get_issue", fake_get_issue)
    monkeypatch.setattr(multica_client, "list_task_runs", fake_list_task_runs)
    monkeypatch.setattr(multica_client, "cancel_task", fake_cancel_task)

    connector = runtime._resolve_external_connector(db, run, call)
    assert connector is not None
    assert connector.profile_revision_id == revision_a
    assert connector.profile_workspace_id == "ws-a"
    # The frozen capability snapshot keeps the historical shape: the workspace
    # is covered by the digest and per-Call source isolation is enforced by
    # the immutable-revision proof, not by generic extra scope fields.
    snapshot = db.query(CapabilityRevision).filter_by(
        key="multica_create_task", revision=call.capability_revision).one()
    assert snapshot.workspace_scope == []
    assert snapshot.manifest_hash == runtime.multica_config_digest_value(
        multica_service.config_for_revision(db, owner.id, revision_a))

    created = asyncio.run(connector.invoke(
        run_id=run.id, call_id=call.id,
        input_ref='{"arguments":{"title":"t"}}', deadline=None))
    assert created["status"] == "running"
    ref = json.loads(created["remote_task_ref"])
    assert ref["profile_revision_id"] == revision_a
    assert ref["base_url"] == "http://127.0.0.1:19001" and ref["workspace_id"] == "ws-a"

    # A → B switch must not retarget the bound Call's query or cancel.
    multica_service.activate_profile(db, owner.id, profile_b.id)
    db.commit()

    observed = asyncio.run(connector.query_status(remote_task_ref=created["remote_task_ref"]))
    assert observed["status"] == "running"
    # The query learns the original run id through the bare task-run history
    # and returns the enriched reference for the reconciler to persist.
    observed_ref = json.loads(observed["remote_task_ref"])
    assert observed_ref["task_id"] == "task-1" and observed_ref["issue_id"] == issue_uuid
    assert observed_ref["profile_revision_id"] == revision_a
    cancelled = asyncio.run(connector.cancel(remote_task_ref=created["remote_task_ref"]))
    assert cancelled["status"] == "cancelled"
    # Every provider interaction stayed on source A's endpoint/credentials.
    assert seen and all(
        (base_url, token, workspace) == ("http://127.0.0.1:19001", "token-a", "ws-a")
        for _kind, base_url, token, workspace in seen
    )

    # Cross-workspace identity: a reference claiming another workspace must
    # fail closed even though the revision itself still resolves.
    foreign_ref = json.dumps({
        "issue_ref": "MYW-1", "task_id": None,
        "profile_revision_id": revision_a,
        "base_url": "http://127.0.0.1:19001", "workspace_id": "ws-b",
    })
    with pytest.raises(ContractError, match="different workspace"):
        asyncio.run(connector.query_status(remote_task_ref=foreign_ref))


def test_multica_bound_call_fails_closed_when_revision_unresolvable(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="multica 失联")
    call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding="rev-gone", suffix="gone",
    )
    db.add(call)
    db.commit()
    assert runtime._resolve_external_connector(db, run, call) is None
    # The scheduler must fence this Call into manual attention instead of
    # letting it silently borrow the active profile.
    assert runtime._external_call_capability_stale(db, run, call) is True


def test_multica_bound_call_fails_closed_when_revision_revoked(db, monkeypatch):
    from datetime import datetime

    from app.super_assistant import multica_service

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 撤销")
    _multica_client_fixture(monkeypatch, "ws-a")
    profile = _create_multica_profile(
        db, monkeypatch, owner.id, name="撤销实例",
        base_url="http://127.0.0.1:19011", workspace_id="ws-a", token="token-a")
    multica_service.activate_profile(db, owner.id, profile.id)
    revision = multica_service._current_revision(db, profile.id)
    revision.revoked_at = datetime.now(timezone.utc)  # explicit security revocation
    call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding=revision.id, suffix="revoked",
    )
    db.add(call)
    db.commit()
    assert runtime._resolve_external_connector(db, run, call) is None
    assert runtime._external_call_capability_stale(db, run, call) is True


def test_multica_bound_call_survives_token_edit_and_profile_delete(db, monkeypatch):
    """Token edits retire a revision but never revoke it, and a soft-deleted
    profile keeps its immutable history: the bound Call keeps executing and
    querying its original source and credentials through both."""
    from app.super_assistant import multica_client, multica_service
    from app.super_assistant.schemas import MulticaProfileUpdate

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 不可变来源")
    _multica_client_fixture(monkeypatch, "ws-a")
    profile = _create_multica_profile(
        db, monkeypatch, owner.id, name="不可变实例",
        base_url="http://127.0.0.1:19061", workspace_id="ws-a", token="token-old")
    multica_service.activate_profile(db, owner.id, profile.id)
    revision_old = multica_service._current_revision(db, profile.id).id
    db.commit()

    ref = json.dumps({"issue_ref": "MYW-9", "task_id": None,
                      "issue_id": "99999999-9999-4999-8999-999999999999",
                      "profile_revision_id": revision_old,
                      "base_url": "http://127.0.0.1:19061", "workspace_id": "ws-a"})
    call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding=revision_old, remote_task_ref=ref, suffix="immutable",
    )
    db.add(call)
    db.commit()

    # A token edit appends a new revision and retires (not revokes) the old.
    multica_service.update_profile(
        db, owner.id, profile.id, MulticaProfileUpdate(token="token-new"))
    db.commit()
    assert multica_service._current_revision(db, profile.id).id != revision_old
    connector = runtime._resolve_external_connector(db, run, call)
    assert connector is not None and connector.profile_revision_id == revision_old
    assert runtime._external_call_capability_stale(db, run, call) is False

    # Soft-deleting the profile keeps the immutable revision resolvable.
    multica_service.delete_profile(db, owner.id, profile.id)
    db.commit()
    connector = runtime._resolve_external_connector(db, run, call)
    assert connector is not None
    assert runtime._external_call_capability_stale(db, run, call) is False

    # Queries keep using the retired revision's original credentials.
    seen: list[tuple] = []
    monkeypatch.setattr(
        multica_client, "get_issue",
        lambda base_url, token, workspace_id, issue_ref: (
            seen.append((base_url, token, workspace_id)),
            {"id": "99999999-9999-4999-8999-999999999999", "identifier": issue_ref,
             "workspace_id": workspace_id, "status": "in_progress"},
        )[1])
    monkeypatch.setattr(
        multica_client, "list_task_runs",
        lambda base_url, token, workspace_id, issue_id: (
            seen.append((base_url, token, workspace_id)),
            [{"id": "task-9", "issue_id": issue_id, "workspace_id": workspace_id,
              "status": "in_progress", "result": "进行中"}],
        )[1])
    observed = asyncio.run(connector.query_status(remote_task_ref=ref))
    assert observed["status"] == "running"
    assert seen and all(
        (base_url, token, workspace_id) == ("http://127.0.0.1:19061", "token-old", "ws-a")
        for base_url, token, workspace_id in seen
    )


def test_multica_unbound_call_keeps_legacy_active_semantics(db, monkeypatch):
    """A pure-legacy unbound Call resolves its source from the authoritative
    persisted capability digest: while the matching revision stays usable it
    keeps running on that exact source even across a profile switch (the
    owner's revisions are read across the switch, never the new active
    profile), and once the source revision is revoked the Call fails closed."""
    from app.super_assistant import multica_client, multica_service

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 兼容")
    _multica_client_fixture(monkeypatch, "ws-a", "ws-b")
    profile_a = _create_multica_profile(
        db, monkeypatch, owner.id, name="兼容 A",
        base_url="http://127.0.0.1:19021", workspace_id="ws-a", token="token-a")
    profile_b = _create_multica_profile(
        db, monkeypatch, owner.id, name="兼容 B",
        base_url="http://127.0.0.1:19022", workspace_id="ws-b", token="token-b")
    multica_service.activate_profile(db, owner.id, profile_a.id)
    revision_a = multica_service._current_revision(db, profile_a.id).id
    db.commit()

    # Persist the frozen capability snapshot first (the upgrade scenario:
    # the deployment already dispatched Multica work under source A), then
    # the legacy Call created before profile binding may prove A by digest.
    bound = _multica_call_row(
        run, target="multica_list_agents",
        capability_revision=runtime._capability_revision_for_target(db, owner.id, "multica_list_agents"),
        binding=revision_a, suffix="legacy-snapshot",
    )
    db.add(bound)
    db.commit()
    assert runtime._resolve_external_connector(db, run, bound) is not None

    call = _multica_call_row(
        run, target="multica_list_agents",
        capability_revision=runtime._capability_revision_for_target(db, owner.id, "multica_list_agents"),
        binding=None, remote_task_ref='{"issue_ref":"MYW-3","task_id":null}',
        suffix="legacy",
    )
    db.add(call)
    db.commit()

    seen: list[tuple] = []
    monkeypatch.setattr(
        multica_client, "list_agents",
        lambda base_url, token, workspace_id: seen.append((base_url, token, workspace_id)) or [],
    )
    resolved_revisions: list[str] = []
    real_config_for_revision = multica_service.config_for_revision

    def spy_config_for_revision(session, owner_id, revision_id):
        resolved_revisions.append(str(revision_id))
        return real_config_for_revision(session, owner_id, revision_id)

    monkeypatch.setattr(multica_service, "config_for_revision", spy_config_for_revision)
    connector = runtime._resolve_external_connector(db, run, call)
    assert connector is not None and connector.profile_revision_id == revision_a
    asyncio.run(connector.invoke(run_id=run.id, call_id=call.id, input_ref='{"arguments":{}}', deadline=None))
    assert seen == [("http://127.0.0.1:19021", "token-a", "ws-a")]
    # Even for a valid legacy capability-digest proof the closure must hand
    # the service the resolved source revision (query/cancel require source).
    assert revision_a in resolved_revisions

    # Switching to B changes the active source, but the frozen capability
    # digest still proves this old Call was created under A: the owner's
    # revisions are read across the switch and the pending Call keeps
    # resolving — and executing against — exactly source A.
    multica_service.activate_profile(db, owner.id, profile_b.id)
    db.commit()
    connector = runtime._resolve_external_connector(db, run, call)
    assert connector is not None and connector.profile_revision_id == revision_a
    assert runtime._external_call_capability_stale(db, run, call) is False
    seen.clear()
    asyncio.run(connector.invoke(run_id=run.id, call_id=call.id, input_ref='{"arguments":{}}', deadline=None))
    assert seen == [("http://127.0.0.1:19021", "token-a", "ws-a")]

    # An explicit revocation of A's revision removes the only proven source:
    # the Call fails closed instead of borrowing the now-active profile B.
    from datetime import datetime

    from app.super_assistant.models import SuperAssistantMulticaProfileRevision

    db.get(SuperAssistantMulticaProfileRevision, revision_a).revoked_at = datetime.now(timezone.utc)
    db.commit()
    assert runtime._resolve_external_connector(db, run, call) is None
    assert runtime._external_call_capability_stale(db, run, call) is True


def test_multica_legacy_digest_never_substitutes_a_revoked_twin(db, monkeypatch):
    """Duplicate credential triples can produce two revisions with the same
    digest.  The legacy digest scan must fail closed when ANY matching
    revision was explicitly revoked — an unrevoked twin row is never
    substituted for a withdrawn credential — while an explicit binding to a
    still-usable revision keeps resolving (explicit ref wins)."""
    from datetime import datetime

    from sqlalchemy import select

    from app.super_assistant import multica_service
    from app.super_assistant.models import SuperAssistantMulticaProfileRevision

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 撤销孪生")
    _multica_client_fixture(monkeypatch, "ws-a")
    profile_a = _create_multica_profile(
        db, monkeypatch, owner.id, name="孪生 A",
        base_url="http://127.0.0.1:19101", workspace_id="ws-a", token="token-a")
    multica_service.activate_profile(db, owner.id, profile_a.id)
    revision_a = multica_service._current_revision(db, profile_a.id).id
    row_a = db.get(SuperAssistantMulticaProfileRevision, revision_a)
    db.commit()

    # A second profile with the identical endpoint/workspace/credential
    # triple: copy the stored ciphertext so both digests match exactly.
    profile_b = _create_multica_profile(
        db, monkeypatch, owner.id, name="孪生 B",
        base_url=row_a.base_url, workspace_id=row_a.workspace_id, token="token-a")
    revision_b = db.scalar(select(SuperAssistantMulticaProfileRevision.id).where(
        SuperAssistantMulticaProfileRevision.profile_id == profile_b.id))
    db.get(SuperAssistantMulticaProfileRevision, revision_b).token_encrypted = row_a.token_encrypted
    db.commit()
    digest = runtime.multica_config_digest_value(row_a)

    # The genuine bd0971cc snapshot shape for the source the legacy Call
    # was created under.
    db.add(CapabilityRevision(
        key="multica_list_agents", revision=1, source="multica",
        manifest_hash=digest, trust_level="user_untrusted",
        permissions=[], input_schema={}, output_schema={},
        side_effect_class="external_async", supports_stream=False,
        supports_cancel=False, supports_approval=False, supports_artifact=False,
        supports_query_status=False, workspace_scope=[], network_scope=[],
        secret_refs=[], enabled=True,
    ))
    legacy_call = _multica_call_row(
        run, target="multica_list_agents", capability_revision=1,
        binding=None, remote_task_ref='{"issue_ref":"MYW-11","task_id":null}',
        suffix="revoked-twin",
    )
    db.add(legacy_call)
    db.commit()

    # Both twins are usable: the digest proves the shared source and the
    # deterministic latest-first pick resolves it.
    connector = runtime._resolve_external_connector(db, run, legacy_call)
    assert connector is not None
    assert runtime._external_call_capability_stale(db, run, legacy_call) is False

    # Revoking the twin the credential was withdrawn on fails the whole
    # digest closed — the surviving twin row must not be substituted.
    row_a.revoked_at = datetime.now(timezone.utc)
    db.commit()
    assert runtime._resolve_external_connector(db, run, legacy_call) is None
    assert runtime._external_call_capability_stale(db, run, legacy_call) is True

    # An explicit binding to the still-usable twin keeps resolving: the
    # revoked-digest carve-out applies only to the legacy digest scan.
    bound_call = _multica_call_row(
        run, target="multica_list_agents", capability_revision=1,
        binding=revision_b, suffix="revoked-twin-bound",
    )
    db.add(bound_call)
    db.commit()
    assert runtime._resolve_external_connector(db, run, bound_call) is not None
    assert runtime._external_call_capability_stale(db, run, bound_call) is False


def test_multica_legacy_call_without_capability_snapshot_fails_closed(db, monkeypatch):
    """A deterministic revision number is not source proof: a pure-legacy Call
    with no persisted capability snapshot never borrows the active profile,
    even when the computed revision number happens to match."""
    from app.super_assistant import multica_service

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 无快照")
    _multica_client_fixture(monkeypatch, "ws-a")
    profile = _create_multica_profile(
        db, monkeypatch, owner.id, name="无快照实例",
        base_url="http://127.0.0.1:19051", workspace_id="ws-a", token="token-a")
    multica_service.activate_profile(db, owner.id, profile.id)
    db.commit()

    call = _multica_call_row(
        run, target="multica_list_agents",
        capability_revision=runtime._capability_revision_for_target(db, owner.id, "multica_list_agents"),
        binding=None, remote_task_ref='{"issue_ref":"MYW-2","task_id":null}',
        suffix="no-snapshot",
    )
    db.add(call)
    db.commit()
    # No connector was ever resolved for this target: no snapshot exists, and
    # the computed revision number matching is not accepted as proof.
    assert db.query(CapabilityRevision).filter_by(key="multica_list_agents").count() == 0
    assert runtime._resolve_external_connector(db, run, call) is None
    assert runtime._external_call_capability_stale(db, run, call) is True


def test_multica_unbound_reference_proves_source_by_endpoint_not_active(db, monkeypatch):
    """An old reference carrying endpoint/workspace evidence resolves exactly
    that source after a switch; it never borrows the new active profile, and
    an unknown source fails closed."""
    from app.super_assistant import multica_client, multica_service

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 旧引用来源")
    _multica_client_fixture(monkeypatch, "ws-a", "ws-b")
    profile_a = _create_multica_profile(
        db, monkeypatch, owner.id, name="来源 A",
        base_url="http://127.0.0.1:19031", workspace_id="ws-a", token="token-a")
    profile_b = _create_multica_profile(
        db, monkeypatch, owner.id, name="来源 B",
        base_url="http://127.0.0.1:19032", workspace_id="ws-b", token="token-b")
    multica_service.activate_profile(db, owner.id, profile_a.id)
    db.commit()

    legacy_ref = json.dumps({
        "issue_ref": "MYW-7", "task_id": None,
        "base_url": "http://127.0.0.1:19031", "workspace_id": "ws-a",
    })
    call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding=None, remote_task_ref=legacy_ref, suffix="source-known",
    )
    db.add(call)
    db.commit()

    # Switch to B first: the old reference must still resolve source A.
    multica_service.activate_profile(db, owner.id, profile_b.id)
    db.commit()

    seen: list[tuple] = []
    source_issue_uuid = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(
        multica_client, "get_issue",
        lambda base_url, token, workspace_id, issue_ref: (
            seen.append((base_url, token, workspace_id)),
            {"id": source_issue_uuid, "identifier": issue_ref,
             "workspace_id": workspace_id, "status": "in_progress"},
        )[1])
    monkeypatch.setattr(
        multica_client, "list_task_runs",
        lambda base_url, token, workspace_id, issue_id: (
            seen.append((base_url, token, workspace_id)),
            [{"id": "task-7", "issue_id": issue_id, "workspace_id": workspace_id,
              "status": "in_progress", "result": "进行中"}],
        )[1])
    connector = runtime._resolve_external_connector(db, run, call)
    assert connector is not None
    assert connector.profile_workspace_id == "ws-a"
    observed = asyncio.run(connector.query_status(remote_task_ref=legacy_ref))
    assert observed["status"] == "running"
    # The query enriches the legacy reference with the immutable source and
    # the unique original run, never with an active rerun.
    observed_ref = json.loads(observed["remote_task_ref"])
    assert observed_ref["task_id"] == "task-7" and observed_ref["issue_id"] == source_issue_uuid
    assert observed_ref["workspace_id"] == "ws-a"
    assert seen and all(
        (base_url, token, workspace_id) == ("http://127.0.0.1:19031", "token-a", "ws-a")
        for base_url, token, workspace_id in seen
    )

    # A reference whose source never existed cannot be proven: fail closed.
    unknown_ref = json.dumps({
        "issue_ref": "MYW-8", "task_id": None,
        "base_url": "http://127.0.0.1:19999", "workspace_id": "ws-none",
    })
    unknown_call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding=None, remote_task_ref=unknown_ref, suffix="source-unknown",
    )
    db.add(unknown_call)
    db.commit()
    assert runtime._resolve_external_connector(db, run, unknown_call) is None
    assert runtime._external_call_capability_stale(db, run, unknown_call) is True


def test_multica_external_call_freezes_profile_revision_at_creation(db, monkeypatch):
    from app.super_assistant import multica_service

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 创建绑定")
    _multica_client_fixture(monkeypatch, "ws-a")
    profile = _create_multica_profile(
        db, monkeypatch, owner.id, name="冻结实例",
        base_url="http://127.0.0.1:19041", workspace_id="ws-a", token="token-a")
    multica_service.activate_profile(db, owner.id, profile.id)
    revision_a = multica_service._current_revision(db, profile.id).id
    db.commit()
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": "multica_create_task", "message": "创建任务"},
    })
    monkeypatch.setattr(runtime, "requires_approval", lambda *args, **kwargs: False)
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "multica-bind-create"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, target_ref="multica_create_task").one()
    assert external.multica_profile_revision_id == revision_a


def test_multica_external_call_without_configuration_stays_unbound(db, monkeypatch):
    run, _, _ = _runtime_fixture(db, monkeypatch, goal="multica 无配置创建")
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": "multica_list_tasks", "message": "查询"},
    })
    monkeypatch.setattr(runtime, "requires_approval", lambda *args, **kwargs: False)
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "multica-legacy-create"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, target_ref="multica_list_tasks").one()
    assert external.multica_profile_revision_id is None


def test_multica_call_freezes_digest_and_revision_from_one_binding(db, monkeypatch):
    """The frozen capability digest (-> revision number) and the profile
    revision binding of a new Call must come from the SAME effective-binding
    read.  A profile switch between tool menu and launch may change the pair
    atomically, but the Call can never mix a digest from one source with the
    revision id of another (which would corrupt the immutable snapshot)."""
    from sqlalchemy import select

    from app.super_assistant import multica_service
    from app.super_assistant.models import SuperAssistantMulticaProfileRevision

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 同源冻结")
    _multica_client_fixture(monkeypatch, "ws-a", "ws-b")
    profile_a = _create_multica_profile(
        db, monkeypatch, owner.id, name="冻结 A",
        base_url="http://127.0.0.1:19091", workspace_id="ws-a", token="token-a")
    profile_b = _create_multica_profile(
        db, monkeypatch, owner.id, name="冻结 B",
        base_url="http://127.0.0.1:19092", workspace_id="ws-b", token="token-b")
    multica_service.activate_profile(db, owner.id, profile_a.id)
    revision_a = multica_service._current_revision(db, profile_a.id).id
    revision_b = db.scalar(select(SuperAssistantMulticaProfileRevision.id).where(
        SuperAssistantMulticaProfileRevision.profile_id == profile_b.id))
    db.commit()
    row_a = db.get(SuperAssistantMulticaProfileRevision, revision_a)
    row_b = db.get(SuperAssistantMulticaProfileRevision, revision_b)
    digest_a = runtime.multica_config_digest_value(row_a)
    digest_b = runtime.multica_config_digest_value(row_b)

    def snapshot_row(revision: int, digest: str) -> CapabilityRevision:
        return CapabilityRevision(
            key="multica_create_task", revision=revision, source="multica",
            manifest_hash=digest, trust_level="user_untrusted",
            permissions=[], input_schema={}, output_schema={},
            side_effect_class="external_async", supports_stream=False,
            supports_cancel=True, supports_approval=False, supports_artifact=False,
            supports_query_status=True, workspace_scope=[], network_scope=[],
            secret_refs=[], enabled=True,
        )

    # Capability history: source A froze at revision 2, a later source B at 3.
    db.add(snapshot_row(2, digest_a))
    db.add(snapshot_row(3, digest_b))
    db.commit()

    # The effective binding changes between two consecutive reads (menu read
    # A, launch would read B).  The launch must freeze BOTH values from the
    # one read it performs — an implementation that reads the digest and the
    # binding separately would freeze revision 2 (A's digest) with B's id.
    projections = [
        SimpleNamespace(base_url=row_a.base_url, workspace_id=row_a.workspace_id,
                        token_encrypted=row_a.token_encrypted, profile_revision_id=revision_a),
        SimpleNamespace(base_url=row_b.base_url, workspace_id=row_b.workspace_id,
                        token_encrypted=row_b.token_encrypted, profile_revision_id=revision_b),
    ]
    reads = {"count": 0}

    def sequencing_config_for(_db, _owner_id, revision_id=None, *, base_url=None, workspace_id=None):
        value = projections[min(reads["count"], len(projections) - 1)]
        reads["count"] += 1
        return value

    monkeypatch.setattr(runtime, "multica_config_for", sequencing_config_for)
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": "multica_create_task", "message": "创建任务"},
    })
    monkeypatch.setattr(runtime, "requires_approval", lambda *args, **kwargs: False)
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "multica-freeze-one-read"}))
    external = db.query(runtime.ExecutionCall).filter_by(
        run_id=run.id, target_ref="multica_create_task").one()
    # Both frozen values describe source A — the single first read.
    assert external.capability_revision == 2
    assert external.multica_profile_revision_id == revision_a


def test_reconciler_persists_learned_task_ref_and_never_switches_to_rerun(db, monkeypatch):
    """A query observation that learns the original task id persists the
    enriched reference onto the Call; observations whose reference contradicts
    the pinned identity (a rerun's completed status, a foreign issue) are
    dropped with no effects, and provider_event_id idempotency holds while
    the status progresses."""
    from app.super_assistant.kernel.models import ExecutionCall

    run, _, _ = _runtime_fixture(db, monkeypatch, goal="multica 学习原任务")
    revision = "rev-learn-1"
    call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding=revision,
        remote_task_ref=json.dumps({"issue_ref": "MYW-5", "task_id": None,
                                    "profile_revision_id": revision}),
        suffix="learn",
    )
    call.status = "waiting_external"; call.outcome = "remote_running"
    run.status = "waiting_external"
    db.add(call)
    db.commit()

    def observe(provider_event_id, state, content, task_id, issue_ref="MYW-5", issue_id="issue-5"):
        return asyncio.run(runtime.reconcile_execution_message({
            "run_id": run.id, "call_id": call.id,
            "connector_id": "multica:multica_create_task",
            "provider_event_id": provider_event_id,
            "remote_state": state, "content": content,
            "remote_task_ref": json.dumps({
                "issue_ref": issue_ref, "task_id": task_id, "issue_id": issue_id,
                "profile_revision_id": revision,
            }),
        }))

    def persisted_call():
        db.expire_all()
        return db.query(ExecutionCall).filter_by(id=call.id).one()

    # The first poll learns the original run id from the bare history list.
    assert observe("multica:obs-learn-1", "running", "执行中", "task-5") is True
    assert json.loads(persisted_call().remote_task_ref)["task_id"] == "task-5"

    # A rerun reporting completion must be dropped whole: the pinned reference
    # survives AND the wrong task's terminal status never closes the Call.
    assert observe("multica:obs-learn-2", "completed", "重跑完成", "task-9") is True
    current = persisted_call()
    assert json.loads(current.remote_task_ref)["task_id"] == "task-5"
    assert current.status == "waiting_external" and current.outcome == "remote_running"
    assert current.evidence_ref is None

    # Redelivery of an already-applied observation is idempotent.
    assert observe("multica:obs-learn-1", "running", "执行中", "task-5") is True
    assert json.loads(persisted_call().remote_task_ref)["task_id"] == "task-5"

    # A foreign issue is dropped the same way, even with a terminal status.
    assert observe("multica:obs-learn-4", "completed", "外来完成", "task-5",
                   issue_ref="MYW-6", issue_id="issue-6") is True
    current = persisted_call()
    assert json.loads(current.remote_task_ref)["issue_ref"] == "MYW-5"
    assert current.status == "waiting_external"

    # Status progress on the pinned original closes the Call exactly once.
    assert observe("multica:obs-learn-3", "completed", "报告完成", "task-5") is True
    closed = persisted_call()
    assert closed.status == "closed" and closed.outcome == "completed"
    assert closed.evidence_ref and closed.evidence_ref.startswith("artifact://")
    assert observe("multica:obs-learn-3", "completed", "报告完成", "task-5") is True
    events = db.query(ExecutionEvent).filter(
        ExecutionEvent.run_id == run.id,
        ExecutionEvent.provider_event_id.like("multica:obs-learn%"),
    ).all()
    # Dropped foreign observations leave no durable events behind.
    assert sorted(event.provider_event_id for event in events) == [
        "multica:obs-learn-1", "multica:obs-learn-3",
    ]
    assert db.query(Artifact).filter_by(call_id=call.id, kind="external.result").count() == 1


def test_multica_old_capability_snapshot_shape_is_reused_after_upgrade(db, monkeypatch):
    """Upgrade compatibility: a CapabilityRevision frozen by pre-upgrade code
    (same config digest, the genuine bd0971cc derived shape) must be reused
    as-is when the upgraded runtime re-resolves the connector under the exact
    baseline immutability comparison.  Existing rows are never rewritten, so
    both the old waiting Call and freshly dispatched work keep flowing."""
    from app.super_assistant import multica_client, multica_service
    from app.super_assistant.kernel.models import ExecutionCall

    run, owner, _ = _runtime_fixture(db, monkeypatch, goal="multica 升级兼容")
    _multica_client_fixture(monkeypatch, "ws-a")
    profile = _create_multica_profile(
        db, monkeypatch, owner.id, name="升级实例",
        base_url="http://127.0.0.1:19081", workspace_id="ws-a", token="token-a")
    multica_service.activate_profile(db, owner.id, profile.id)
    revision_a = multica_service._current_revision(db, profile.id).id
    db.commit()
    config = multica_service.active_config(db, owner.id)
    # The digest is computed exactly as the pre-upgrade code did — the literal
    # _external_config_hash over the historical manifest fields.
    legacy_digest = runtime._external_config_hash({
        "kind": "multica", "base_url": config.base_url,
        "workspace_id": config.workspace_id, "token": config.token_encrypted,
    })
    assert legacy_digest == runtime.multica_config_digest_value(config)

    # A snapshot row in the genuine bd0971cc shape: multica_create_task had
    # its query/cancel executors wired already (revision 2 records the proven
    # async contract), scopes stayed empty.  Old code persisted exactly this,
    # so the exact-field comparison reuses it without any relaxation.
    old_row = CapabilityRevision(
        key="multica_create_task", revision=2, source="multica",
        manifest_hash=legacy_digest, trust_level="user_untrusted",
        permissions=[], input_schema={}, output_schema={},
        side_effect_class="external_async", supports_stream=False,
        supports_cancel=True, supports_approval=False, supports_artifact=False,
        supports_query_status=True, workspace_scope=[], network_scope=[],
        secret_refs=[], enabled=True,
    )
    db.add(old_row)
    db.commit()

    # A waiting Call created before the upgrade carries that revision.
    old_call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding=None, remote_task_ref=json.dumps({"issue_ref": "MYW-40", "task_id": None}),
        suffix="upgrade-old",
    )
    old_call.status = "waiting_external"; old_call.outcome = "remote_running"
    run.status = "waiting_external"
    db.add(old_call)
    db.commit()

    monkeypatch.setattr(
        multica_client, "create_issue",
        lambda *_args, **_kwargs: {
            "id": "40404040-4040-4404-8404-404040404040", "identifier": "MYW-41",
            "title": "t", "status": "open"},
    )
    monkeypatch.setattr(
        multica_client, "get_issue",
        lambda base_url, token, workspace_id, issue_ref: {
            "id": "40404040-4040-4404-8404-404040404040", "identifier": issue_ref,
            "workspace_id": workspace_id, "status": "in_progress"},
    )
    monkeypatch.setattr(
        multica_client, "list_task_runs",
        lambda base_url, token, workspace_id, issue_id: [
            {"id": "task-40", "issue_id": issue_id, "workspace_id": workspace_id,
             "status": "in_progress", "result": "进行中"}],
    )

    # The upgraded runtime re-resolves and re-persists the connector against
    # the same digest: the old snapshot shape is reused, not rejected.
    connector = runtime._resolve_external_connector(db, run, old_call)
    assert connector is not None
    reused = db.query(CapabilityRevision).filter_by(
        key="multica_create_task", revision=2).one()
    # Existing rows are never rewritten: the genuine pre-upgrade shape survives.
    assert reused.supports_query_status is True and reused.supports_cancel is True
    assert reused.workspace_scope == []
    assert db.query(CapabilityRevision).filter_by(
        key="multica_create_task", revision=2).count() == 1
    # New work resolves the same revision from the digest instead of bumping.
    assert runtime._capability_revision_for_target(db, owner.id, "multica_create_task") == 2

    # The old waiting Call can still be queried through the upgraded path…
    observed = asyncio.run(connector.query_status(remote_task_ref=old_call.remote_task_ref))
    assert observed["status"] == "running"

    # …and a fresh Call dispatched by the upgraded code freezes the same
    # digest, shares the reused snapshot, and executes normally.
    new_call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding=revision_a, suffix="upgrade-new",
    )
    db.add(new_call)
    db.commit()
    new_connector = runtime._resolve_external_connector(db, run, new_call)
    assert new_connector is not None
    created = asyncio.run(new_connector.invoke(
        run_id=run.id, call_id=new_call.id,
        input_ref='{"arguments":{"title":"升级后新任务"}}', deadline=None))
    assert created["status"] == "running"
    assert json.loads(created["remote_task_ref"])["profile_revision_id"] == revision_a
    assert db.query(CapabilityRevision).filter_by(
        key="multica_create_task", revision=2).count() == 1
    assert db.query(ExecutionCall).filter_by(run_id=run.id,
                                             target_ref="multica_create_task").count() == 2


def test_reconcile_redelivers_pre_upgrade_event_and_acks(db, monkeypatch):
    """The public NATS reconciliation event hash keeps its historical shape:
    a provider event stored by pre-upgrade code must redeliver-and-ack after
    the upgrade, a duplicate that finally carries the enriched reference must
    still persist the enrichment, and a reused event id with different content
    keeps being rejected."""
    from app.super_assistant.kernel.models import ExecutionCall
    from app.super_assistant.kernel.store import append_event

    run, _, _ = _runtime_fixture(db, monkeypatch, goal="multica 事件重投")
    revision = "rev-redeliver-1"
    call = _multica_call_row(
        run, target="multica_create_task", capability_revision=2,
        binding=revision,
        remote_task_ref=json.dumps({"issue_ref": "MYW-30", "task_id": None,
                                    "profile_revision_id": revision}),
        suffix="redeliver",
    )
    call.status = "waiting_external"; call.outcome = "remote_running"
    run.status = "waiting_external"
    db.add(call)
    db.commit()

    # A provider event exactly as pre-upgrade code persisted it: the payload
    # hash covers the historical six fields and nothing else.
    pre_upgrade_payload = {
        "run_id": run.id, "call_id": call.id,
        "remote_state": "running", "content": "执行中",
        "evidence_ref": None, "artifacts": None,
    }
    pre_upgrade_hash = runtime._hash_payload(pre_upgrade_payload)
    append_event(
        db, run, event_type="call.outcome_changed",
        payload={"call_id": call.id, "status": "waiting_external",
                 "outcome": "remote_running", "evidence_ref": None,
                 "connector_id": "multica:multica_create_task",
                 "provider_event_id": "multica:pre-1",
                 "provider_payload_hash": pre_upgrade_hash},
        actor={"kind": "reconciler"}, command_id=f"reconcile:{call.id}:multica:pre-1",
        idempotency_key=f"reconcile:{call.id}:multica:pre-1",
        connector_id="multica:multica_create_task",
        provider_event_id="multica:pre-1",
    )
    db.commit()

    def redeliver(extra=None):
        message = {
            "run_id": run.id, "call_id": call.id,
            "connector_id": "multica:multica_create_task",
            "provider_event_id": "multica:pre-1",
            "remote_state": "running", "content": "执行中",
        }
        message.update(extra or {})
        return asyncio.run(runtime.reconcile_execution_message(message))

    def persisted_call():
        db.expire_all()
        return db.query(ExecutionCall).filter_by(id=call.id).one()

    # Plain redelivery of the pre-upgrade message acks without effects.
    assert redeliver() is True
    current = persisted_call()
    assert current.status == "waiting_external"
    assert json.loads(current.remote_task_ref)["task_id"] is None

    # A duplicate that now carries the learned original task id still acks and
    # persists the enrichment — the reference learning must not regress.
    enriched = json.dumps({"issue_ref": "MYW-30", "task_id": "task-30",
                           "issue_id": "30303030-3030-4303-8303-303030303030",
                           "profile_revision_id": revision})
    assert redeliver({"remote_task_ref": enriched}) is True
    current = persisted_call()
    assert json.loads(current.remote_task_ref)["task_id"] == "task-30"
    assert current.status == "waiting_external"

    # A reused provider event id with different content stays a hard conflict.
    assert redeliver({"content": "内容被替换"}) is False
    current = persisted_call()
    assert current.status == "waiting_external"
    assert json.loads(current.remote_task_ref)["task_id"] == "task-30"


def test_scheduler_poll_payload_keeps_legacy_shape_without_observed_ref(db, monkeypatch):
    """Providers that do not observe a reference keep the historical payload
    shape: no ``remote_task_ref`` key is invented, so the outbox observation
    identity of pre-upgrade integrations is unchanged."""
    from app.super_assistant.kernel.models import ExecutionDispatchOutbox

    run, _, _ = _runtime_fixture(db, monkeypatch, goal="传统 provider 轮询")
    target = f"fake.legacy_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(runtime.provider, "chat", lambda *_args, **_kwargs: {
        "content": "已提交", "tool_calls": [],
        "external_call": {"target_ref": target, "message": "后台执行"},
    })
    monkeypatch.setattr(runtime, "requires_approval", lambda *args, **kwargs: False)
    asyncio.run(runtime.process_execution_message({"run_id": run.id, "command_id": "legacy-poll"}))
    external = db.query(runtime.ExecutionCall).filter_by(run_id=run.id, side_effect_class="external_async").one()

    class LegacyPollConnector:
        def descriptor(self):
            return AgentDescriptor(
                agent_id="fake-legacy-poll", key=target, revision=1,
                transport="mcp", session_policy=SessionPolicy.RESUMABLE,
                supports_query_status=True,
            )

        async def invoke(self, **kwargs):
            return {"status": "running", "remote_task_ref": "legacy-ref"}

        async def query_status(self, **kwargs):
            return {"status": "completed", "content": "完成", "provider_event_id": "legacy-1"}

        async def cancel(self, **kwargs):
            return {"status": "unsupported"}

    runtime.connector_registry.register(LegacyPollConnector())
    asyncio.run(runtime.process_external_call_message({"run_id": run.id, "call_id": external.id}))
    db.expire_all()
    monkeypatch.setattr(kernel_scheduler, "SessionLocal", TestSession)
    db.refresh(external)
    external.next_reconcile_at = runtime._now()
    db.commit()
    kernel_scheduler._poll_external_calls_once()
    db.expire_all()
    queued = db.query(ExecutionDispatchOutbox).filter_by(
        run_id=run.id, subject="sa.execution.reconcile",
    ).one()
    assert queued.payload["remote_state"] == "completed"
    assert "remote_task_ref" not in queued.payload
