"""Non-overlapping routing execution semantics.

An enabled first-matching rule authorizes Multica writes. Without it, routing is
read-only: misses may only open a local decision card or ignore, answer re-entry
records the completion fact without another model call, and no code path on a
miss decrypts connector credentials or persists write steps.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.model_configs.models import ModelConfig
from app.super_assistant import multica_client, multica_service, provider
from app.super_assistant.models import (
    SuperAssistantMulticaConfig,
    SuperAssistantMulticaProfile,
    SuperAssistantMulticaProfileRevision,
)
from app.super_assistant.schemas import MulticaConfigUpdate
from app.super_assistant.autoloop import dispatching, planner, schemas, service, store, templates, trace_service, worker
from app.super_assistant.autoloop.models import (
    AutoLoopAction, AutoLoopActionStep, AutoLoopBinding, AutoLoopDecision,
    AutoLoopDispatch, AutoLoopEvent,
)


@pytest.fixture
def feature_sessions(pg_engine):
    # Same disposable-schema pattern as test_features; only tables used here.
    # A miss freezes the selected model when it is prepared, so the model
    # registry is read on the miss path too.
    for table in (SuperAssistantMulticaConfig, SuperAssistantMulticaProfile,
                  SuperAssistantMulticaProfileRevision, ModelConfig):
        table.__table__.create(pg_engine, checkfirst=True)
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
def owner(feature_sessions):
    identity = str(uuid4())
    with feature_sessions() as db:
        db.add(User(id=identity, username=identity, email=f"{identity}@test.invalid",
                    password_hash="fixture-no-login", is_active=True))
        db.flush()
        # A miss is frozen with the model it will be assessed by; without an
        # enabled model it is refused before the planner is ever consulted.
        db.add(ModelConfig(id="fixture-model", name="fixture", provider="openai", models=["fixture"],
                           created_by=identity, enabled=True, config_type="llm"))
        multica_service.save_config(db, identity, MulticaConfigUpdate(
            base_url="http://127.0.0.1:18002", workspace_id="workspace-a",
            token="isolated-fixture-token", enabled=True))
    return identity


def _admit(sessions, owner_id, payload=None, scheduled_task_id=None):
    with sessions() as db:
        _, event, _ = store.accept_receipt(db, source_kind="schedule", source_system="fixture",
            source_event_id=str(uuid4()), owner_id=owner_id, scheduled_task_id=scheduled_task_id,
            payload=payload or {"title": "event", "body": "original"})
        work = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.event_id == event.id))
        db.commit()
        return event.id, work.id


def _stub_plan(monkeypatch, plan):
    calls = []

    def stub(db, **kwargs):
        calls.append(kwargs)
        return plan

    monkeypatch.setattr(planner, "plan_event", stub)
    return calls


def _forbid_connector(monkeypatch):
    monkeypatch.setattr(multica_service, "decrypt_token",
        lambda *a, **k: pytest.fail("miss must not decrypt connector credentials"))
    for name in ("get_issue", "list_agents", "create_issue", "create_comment"):
        monkeypatch.setattr(multica_client, name,
            lambda *a, **k: pytest.fail("miss must not touch the Multica connector"))


def _completed(db, event_id):
    return db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.parent_event_id == event_id,
        AutoLoopEvent.type == "routing.completed"))


def test_miss_creates_local_readonly_decision_without_connector_or_writes(feature_sessions, owner, monkeypatch):
    plan = planner.Plan(action="create_decision", reason="需要人工选择处理方式",
        title="未命中规则的事件需要确认", question="如何在 Multica 中处理此事件？",
        options=[{"id": "create", "label": "手动创建任务"}, {"id": "drop", "label": "无需处理"}],
        recommendedOptionId="create")
    calls = _stub_plan(monkeypatch, plan)
    _forbid_connector(monkeypatch)
    event_id, work_id = _admit(feature_sessions, owner)
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        assert calls and calls[0]["decision_only"] is True and calls[0]["decision_purpose"] == "miss"
        assert calls[0].get("trusted_issue") is None
        decision = db.scalar(select(AutoLoopDecision))
        assert decision.source == "assistant" and decision.status == "open"
        assert decision.issue_key is None and decision.need_key.startswith("readonly:")
        assert decision.question == plan.question and len(decision.options) == 2
        assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0
        assert db.scalar(select(func.count()).select_from(AutoLoopActionStep)) == 0
        completed = _completed(db, event_id)
        assert completed.action == "create_decision" and completed.decision_item_id == decision.id
        assert db.get(AutoLoopDispatch, work_id).state == "done"


def test_miss_ignore_records_fact_only(feature_sessions, owner, monkeypatch):
    _stub_plan(monkeypatch, planner.Plan(action="ignore", reason="仅等待外部条件"))
    _forbid_connector(monkeypatch)
    event_id, work_id = _admit(feature_sessions, owner)
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        assert db.scalar(select(func.count()).select_from(AutoLoopDecision)) == 0
        assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0
        completed = _completed(db, event_id)
        assert completed.action == "ignore"
        assert completed.data["plan"]["reason"] == "仅等待外部条件"
        assert db.get(AutoLoopDispatch, work_id).state == "done"


def test_miss_ignores_historical_binding_and_never_appends(feature_sessions, owner, monkeypatch):
    task_id = str(uuid4())
    with feature_sessions() as db:
        anchor = store.append_event(db, owner_id=owner, type="fixture", source_kind="schedule",
            source_system="fixture", action="accepted")
        db.add(AutoLoopBinding(owner_id=owner, workspace_id="workspace-a",
            source_scope="schedule:" + task_id, source_thread=task_id, issue_key="TASK-1",
            provenance={"kind": "created_issue", "baseUrl": "http://127.0.0.1:18002"},
            established_event_id=anchor.id))
        db.commit()
    _stub_plan(monkeypatch, planner.Plan(action="create_decision", reason="需要人工选择",
        title="确认", question="如何处理？", options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]))
    _forbid_connector(monkeypatch)
    event_id, work_id = _admit(feature_sessions, owner, scheduled_task_id=task_id)
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        completed = _completed(db, event_id)
        assert completed.action == "create_decision" and completed.issue_ref is None
        assert db.scalar(select(func.count()).select_from(AutoLoopActionStep)) == 0
        binding = db.scalar(select(AutoLoopBinding))
        assert binding.issue_key == "TASK-1" and binding.provenance["kind"] == "created_issue"


@pytest.mark.parametrize("plan", [
    planner.Plan(action="create_task", reason="model overreach", title="TASK from model"),
    planner.Plan(action="append_task", reason="model overreach", issueRef="TASK-1", description="reply"),
    planner.Plan(action="create_decision", reason="model overreach", title="确认", question="如何处理？",
                 options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], assignee="agent-x"),
    planner.Plan(action="create_decision", reason="model overreach", title="确认", question="如何处理？",
                 options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], issueRef="TASK-9"),
])
def test_miss_model_write_or_target_plans_are_rejected_and_fall_back(feature_sessions, owner, monkeypatch, plan):
    _stub_plan(monkeypatch, plan)
    _forbid_connector(monkeypatch)
    event_id, work_id = _admit(feature_sessions, owner)
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        fallback = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.parent_event_id == event_id,
            AutoLoopEvent.type == "routing.fallback"))
        assert fallback.error == "untrusted_readonly_plan"
        decision = db.scalar(select(AutoLoopDecision))
        assert decision.source == "kernel_fallback" and decision.need_key == "event:" + event_id
        assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0
        assert db.get(AutoLoopDispatch, work_id).state == "done"


def test_miss_model_failure_still_lands_in_existing_fallback(feature_sessions, owner, monkeypatch):
    def unavailable(db, **kwargs):
        raise planner.PlanningError("model_unavailable")

    monkeypatch.setattr(planner, "plan_event", unavailable)
    _forbid_connector(monkeypatch)
    event_id, work_id = _admit(feature_sessions, owner)
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        fallback = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.parent_event_id == event_id,
            AutoLoopEvent.type == "routing.fallback"))
        assert fallback.error == "model_unavailable"
        decision = db.scalar(select(AutoLoopDecision))
        assert decision.source == "kernel_fallback" and decision.status == "open"


def test_planner_rejects_readonly_model_write_output(monkeypatch):
    monkeypatch.setattr(planner, "select_llm_model_config", lambda **kw: object())
    monkeypatch.setattr(planner, "llm_call_kwargs", lambda config: {"model": "fixture"})
    prompts = []

    def chat(kwargs, messages, tools):
        prompts.append(messages[0]["content"])
        return {"content": json.dumps({"action": "create_task", "reason": "overreach", "title": "TASK"})}

    monkeypatch.setattr(provider, "chat", chat)
    with pytest.raises(planner.PlanningError, match="invalid_readonly_action"):
        planner.plan_event(db=None, event=SimpleNamespace(type="notification.received"),
            payload={}, workspace_id="workspace-a", decision_only=True, decision_purpose="miss")
    with pytest.raises(planner.PlanningError, match="invalid_watchdog_action"):
        planner.plan_event(db=None, event=SimpleNamespace(type="watchdog.observed"),
            payload={}, workspace_id="workspace-a", decision_only=True)
    assert "未命中任何已启用规则" in prompts[0] and "禁止 create_task 与 append_task" in prompts[0]
    assert "看门狗只读研判" in prompts[1]


def test_answered_reentry_without_rule_records_completion_without_model(feature_sessions, owner, monkeypatch):
    monkeypatch.setattr(planner, "plan_event",
        lambda *a, **k: pytest.fail("answered re-entry without a rule must not call the model"))
    _forbid_connector(monkeypatch)
    with feature_sessions() as db:
        row, _ = store.create_or_update_decision(db, owner_id=owner, workspace_id="workspace-a",
            issue_key=None, source="assistant", need_key="readonly:fixture", title="确认",
            question="如何处理？", options=[{"id": "a", "label": "A"}], evidence={"factsSummary": "miss"})
        store.submit_decision(db, owner_id=owner, decision_id=row.id, actor_id=owner,
            expected_content_version=row.content_version, idempotency_key="answer", option_id="a")
        db.commit()
        decision_id = row.id
    assert worker.scan_actions(session_factory=feature_sessions) == 1
    with feature_sessions() as db:
        answered = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.type == "decision.answered"))
        assert answered is not None and answered.owner_id == owner
        work = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.event_id == answered.id))
        assert work is not None
    dispatching.process_dispatch(work.id, session_factory=feature_sessions)
    with feature_sessions() as db:
        completed = _completed(db, answered.id)
        assert completed.action == "ignore"
        assert completed.data["plan"]["reason"] == "decision_answered_without_rule"
        assert db.scalar(select(func.count()).select_from(AutoLoopDecision)) == 1
        assert db.get(AutoLoopDecision, decision_id).status == "completed"
        assert db.get(AutoLoopDispatch, work.id).state == "done"


def test_rule_matching_answered_event_keeps_rule_authorization(feature_sessions, owner, monkeypatch):
    monkeypatch.setattr(planner, "plan_event",
        lambda *a, **k: pytest.fail("a matching rule must not call the model"))
    with feature_sessions() as db:
        rule = service.create_rule(db, owner, schemas.RuleCreate.model_validate({"name": "answers",
            "enabled": True, "position": 0, "match": {"sourceKind": "decision"},
            "titleTemplate": "[answered] {{ question }}", "descriptionTemplate": "{{ answer }}"}))
        db.commit()
        event = store.append_event(db, owner_id=owner, type="decision.answered",
            source_kind="decision", source_system="openontology", source_ref=str(uuid4()),
            action="accepted", data={"question": "如何处理？", "answer": "继续"})
        db.add(AutoLoopDispatch(event_id=event.id, owner_id=owner))
        db.commit()
        event_id, work_id = event.id, db.scalar(select(AutoLoopDispatch.id).where(
            AutoLoopDispatch.event_id == event.id))
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        completed = _completed(db, event_id)
        assert completed.action == "create_task" and completed.rule_id == rule["id"]
        action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == event_id))
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
        assert step.kind == "issue.create" and step.state == "queued"
        assert step.request_payload["title"] == "[answered] 如何处理？"
        assert db.scalar(select(func.count()).select_from(AutoLoopDecision)) == 0


def test_bound_rule_with_empty_description_uses_original_in_comment(feature_sessions, owner, monkeypatch):
    with feature_sessions() as db:
        # Binding never narrows a match: this rule matches everything while
        # enabled, and a rule is paused unless enabling is explicit.
        rule = service.create_rule(db, owner, schemas.RuleCreate.model_validate({"name": "append",
            "enabled": True, "titleTemplate": "{{ title }}", "descriptionTemplate": "",
            "binding": {"mode": "extract", "targetIssue": "TASK-1"}}), confirm_all_match=True)
        db.commit()
    monkeypatch.setattr(planner, "plan_event",
        lambda *a, **k: pytest.fail("a matching rule must not call the model"))
    monkeypatch.setattr(multica_client, "get_issue", lambda *a: {
        "id": "issue-1", "identifier": "TASK-1", "workspace_id": "workspace-a"})
    event_id, work_id = _admit(feature_sessions, owner)
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        completed = _completed(db, event_id)
        assert completed.action == "append_task" and completed.rule_id == rule["id"]
        step = db.scalar(select(AutoLoopActionStep))
        assert step.kind == "issue.comment" and step.request_payload["issueRef"] == "TASK-1"
        assert templates.UNTRUSTED_LABEL in step.request_payload["description"]
        assert '"body": "original"' in step.request_payload["description"]
        assert trace_service.get_trace(db, owner, event_id)["outcome"] == "queued"
