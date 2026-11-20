"""AutoLoop contracts and real-PostgreSQL routing/writeback/watchdog regressions."""
from __future__ import annotations

import json
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.model_configs.models import ModelConfig
from app.super_assistant import multica_client, multica_service, provider
from app.super_assistant.autoloop import dispatching, planner, routing, schemas, service, sourcing, store, templates, watchdog, worker
from app.super_assistant.autoloop.models import (
    AutoLoopAction, AutoLoopActionStep, AutoLoopDecision, AutoLoopDispatch,
    AutoLoopEvent, AutoLoopEvidence, AutoLoopRule, utcnow,
)
from app.super_assistant.autoloop.router import router
from app.super_assistant.models import (
    SuperAssistantMulticaConfig,
    SuperAssistantMulticaProfile,
    SuperAssistantMulticaProfileRevision,
)
from app.super_assistant.schemas import MulticaConfigUpdate

BASE_A = "http://127.0.0.1:18002"
TOKEN_A = "isolated-fixture-token"


@pytest.fixture
def feature_sessions(pg_engine):
    # The fixture owns a disposable schema on the reserved real PostgreSQL.
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
        db.commit()
        multica_service.save_config(db, identity, MulticaConfigUpdate(
            base_url=BASE_A, workspace_id="workspace-a", token=TOKEN_A, enabled=True))
    return identity


def _resolve(owner_id):
    return lambda session, row: sourcing.resolve_decision_source(session, owner_id, row)


def _rule(**changes):
    return schemas.RuleCreate.model_validate({"name": "route", "enabled": True, "position": 0,
        "match": {}, "titleTemplate": "[rule] {{ title }}", "descriptionTemplate": "{{ body }}", **changes})


def _admit(sessions, owner_id, payload=None):
    with sessions() as db:
        _, event, _ = store.accept_receipt(db, source_kind="schedule", source_system="fixture",
            source_event_id=str(uuid4()), owner_id=owner_id,
            payload=payload or {"title": "event", "body": "original"})
        work = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.event_id == event.id))
        db.commit()
        return event.id, work.id


def _card(db, owner_id, source="watchdog", issue="TASK-1"):
    return store.create_or_update_decision(db, owner_id=owner_id, workspace_id="workspace-a",
        issue_key=issue, source=source, need_key=str(uuid4()), title="Choose", question="Continue?",
        options=[{"id": "continue", "label": "Continue"}], evidence={"factsSummary": "Observed"})[0]


@pytest.mark.parametrize("source", [
    "{{ ''.__class__ }}", "{{ event.__class__ }}", "{{ cycler() }}",
    "{% include 'secret' %}", "{% macro f() %}x{% endmacro %}",
    "{{ title | attr('__class__') }}", "{{ 10 ** 100000 }}", "{{ 'x' * 1000000000 }}",
])
def test_template_unsafe_or_unbounded_expressions_fail(source):
    with pytest.raises(templates.RenderError):
        templates.render(source, "", {"title": "event"})


def test_template_determinism_autoescape_and_json_only():
    sample = {"title": "<script>x</script>", "body": "原文", "items": ["a", "b"]}
    first = templates.render("{{ title }}", "{% for item in items %}{{ item }}{% endfor %}", sample)
    assert first == templates.render("{{ title }}", "{% for item in items %}{{ item }}{% endfor %}", sample)
    assert first == ("&lt;script&gt;x&lt;/script&gt;", "ab")
    with pytest.raises(templates.RenderError):
        templates.render("{{ value }}", "", {"value": object()})
    with pytest.raises(templates.RenderError, match="资源"):
        templates.render("{{ title }}", "", {"title": "x" * 300000})


def test_template_nested_iteration_and_intermediate_limits():
    assert templates.render("loop", "{% for a in items %}{% for b in items %}{{ a }}{{ b }}{% endfor %}{% endfor %}",
        {"items": ["a", "b"]}) == ("loop", "aaabbabb")
    with pytest.raises(templates.RenderError) as error:
        templates.render("loop", "{% for a in items %}{% for b in items %}x{% endfor %}{% endfor %}",
            {"items": [1] * 100})
    assert error.value.code == "template_render_limit"
    for source in ("{{ text | replace('a', replacement) }}", "{{ items | join(separator) }}",
                   "{{ text | replace(old='a', new=replacement) }}", "{{ items | join(d=separator) }}",
                   "{{ items | tojson(indent=1000000000) }}", "{{ ([replacement] * 1000) | length }}"):
        with pytest.raises(templates.RenderError) as error:
            templates.render(source, "", {"text": "a" * 1000, "replacement": "b" * 1000,
                "items": ["a"] * 1000, "separator": "b" * 1000})
        assert error.value.code == "template_render_limit"
    assert templates.render("{{ missing | default('fallback') }}", "{{ items | join(',') }}",
        {"items": ["a", "b"], "_autoloop_iter": "untrusted"}) == ("fallback", "a,b")
    assert templates.render("{{ text | replace(old='a', new='b', count=1) | upper | trim }}",
        "{{ items | join(d=',') | lower }}", {"text": " aa ", "items": ["A", "B"]}) == ("BA", "a,b")
    assert templates.render("{{ items | tojson }}", "{{ items | length }}", {"items": [1, 2]}) == ("[1, 2]", "2")


def test_template_budget_and_iteration_limit(monkeypatch):
    ticks = iter([0, 0, 2, 2, 2, 2])
    monkeypatch.setattr(templates, "monotonic", lambda: next(ticks, 2))
    with pytest.raises(templates.RenderError) as error:
        templates.render("{{ title }}", "", {"title": "a"}, budget_ms=1)
    assert error.value.code == "template_render_limit"


def test_binding_only_bounded_extraction():
    binding = {"mode": "extract", "pattern": r"(TASK-[0-9]{1,8})"}
    assert templates.binding_target(binding, {"body": "See TASK-123"}) == "TASK-123"
    for pattern in (r"(a+)+$", r"(a|aa)+$", r"(?=a)a", r"(a)\1"):
        with pytest.raises(templates.RenderError):
            templates.validate_binding({"pattern": pattern})


def test_rule_snapshot_survives_edit_delete_and_replay(feature_sessions, owner, monkeypatch):
    with feature_sessions() as db:
        saved = service.create_rule(db, owner, _rule())
        service.create_rule(db, owner, _rule(name="second", position=1, titleTemplate="second"))
        db.commit()
    event_id, work_id = _admit(feature_sessions, owner)
    with feature_sessions() as db:
        event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == event_id))
        frozen = routing.prepare(db, event)
        frozen_id = frozen.id
        service.patch_rule(db, owner, saved["id"], schemas.RulePatch(titleTemplate="changed"))
        service.delete_rule(db, owner, saved["id"])
        db.commit()
    monkeypatch.setattr(planner, "plan_event", lambda *a, **kw: pytest.fail("matched rules must not call model"))
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        assert db.get(AutoLoopDispatch, work_id).state == "done"
        frozen = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == frozen_id))
        assert frozen.rule_snapshot["titleTemplate"] == "[rule] {{ title }}" and frozen.rule_version == 1
        action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == event_id))
        steps = list(db.scalars(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id)))
        assert len(steps) == 1 and steps[0].state == "queued"
        assert steps[0].request_payload["title"] == "[rule] event"
        assert templates.UNTRUSTED_LABEL in steps[0].request_payload["description"]
        assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 1


@pytest.mark.parametrize("source,expected", [
    ("watchdog", ["issue.comment"]),
    ("assistant", ["decision.answered", "issue.comment"]),
    ("kernel_fallback", ["decision.answered", "issue.comment"]),
])
def test_decision_worker_completes_only_required_steps_once(feature_sessions, owner, monkeypatch, source, expected):
    writes = []
    monkeypatch.setattr(multica_client, "get_issue", lambda *a: {
        "id": "remote-1", "identifier": "TASK-1", "workspace_id": "workspace-a"})
    def comment(*args, **kw):
        writes.append(kw)
        return multica_client.MulticaCommentResult("comment-1", {"id": "comment-1"}, "unknown", {})
    monkeypatch.setattr(multica_client, "create_comment", comment)
    with feature_sessions() as db:
        row = _card(db, owner, source)
        identity, version = row.id, row.content_version
        store.submit_decision(db, owner_id=owner, decision_id=identity, actor_id=owner,
            expected_content_version=version, idempotency_key="answer", option_id="continue",
            source_resolver=_resolve(owner))
        db.commit()
    assert worker.scan_actions(session_factory=feature_sessions) == len(expected)
    assert worker.scan_actions(session_factory=feature_sessions) == 0
    with feature_sessions() as db:
        row = db.get(AutoLoopDecision, identity)
        steps = list(db.scalars(select(AutoLoopActionStep).order_by(AutoLoopActionStep.ordinal)))
        assert [step.kind for step in steps] == expected
        assert all(step.state == "confirmed" and step.ever_sent_at for step in steps)
        assert row.status == "completed" and row.content_version > version
        assert db.scalar(select(func.count()).select_from(AutoLoopEvent).where(
            AutoLoopEvent.type == "decision.answered")) == (source != "watchdog")
        assert store.submit_decision(db, owner_id=owner, decision_id=identity, actor_id=owner,
            expected_content_version=version, idempotency_key="answer", option_id="continue")[2]
        db.commit()
    assert writes == [{"content": "Continue"}]


def test_timeout_keeps_identity_unknown_and_does_not_resend(feature_sessions, owner, monkeypatch):
    writes = []
    monkeypatch.setattr(multica_client, "get_issue", lambda *a: {
        "id": "remote-1", "identifier": "TASK-1", "workspace_id": "workspace-a"})
    def lost(*a, **kw):
        writes.append(kw)
        raise multica_client.MulticaRequestError("lost response", outcome="unknown")
    monkeypatch.setattr(multica_client, "create_comment", lost)
    monkeypatch.setattr(multica_client, "list_comments", lambda *a: multica_client.MulticaRead([], True))
    with feature_sessions() as db:
        row = _card(db, owner)
        identity = row.id
        store.submit_decision(db, owner_id=owner, decision_id=identity, actor_id=owner,
            expected_content_version=row.content_version, idempotency_key="answer", option_id="continue",
            source_resolver=_resolve(owner))
        db.commit()
    worker.scan_actions(session_factory=feature_sessions)
    worker.scan_actions(session_factory=feature_sessions)
    with feature_sessions() as db:
        row = db.get(AutoLoopDecision, identity)
        step = db.scalar(select(AutoLoopActionStep))
        assert row.status == "writeback_unknown" and step.attempts == 1
        assert step.state == "unknown" and step.remote_request_id
        with pytest.raises(store.StoreConflict) as conflict:
            store.mark_handled(db, owner_id=owner, decision_id=identity, actor_id=owner,
                expected_content_version=1, idempotency_key="handled")
        assert conflict.value.http_status == 409
    assert len(writes) == 1


@pytest.mark.parametrize("recent,recovery,expected", [(True, True, True), (True, False, False), (False, True, False), (False, False, False)])
def test_todo_attention_requires_recent_failure_and_recovery(recent, recovery, expected):
    at = utcnow() - timedelta(hours=1 if recent else 200)
    timeline = [{"id": "failure", "action": "task_failed", "created_at": at.isoformat(), "details": {"task_id": "run-1"}}]
    if recovery:
        timeline.append({"id": "recovery", "created_at": at.isoformat(), "details": {
            "task_id": "run-1", "from": "in_progress", "to": "todo", "cause": "failure_recovery"}})
    assert watchdog.attention({"status": "todo"}, timeline) is expected
    assert watchdog.attention({"status": "blocked"}, [])
    assert watchdog.attention({"status": "in_review"}, [])


def test_watchdog_late_evidence_followup_consumption_and_new_run(feature_sessions, owner):
    issue = {"id": "remote-1", "identifier": "TASK-1", "status": "blocked", "workspace_id": "workspace-a"}
    before = utcnow() - timedelta(hours=1)
    timeline = [{"id": "start-1", "action": "task_started", "created_at": before.isoformat(), "details": {"task_id": "run-1"}}]
    def scan(db, comments=None, messages=None, events=None, tasks=None, complete=True):
        return watchdog.reconcile_issue(db, owner_id=owner, workspace_id="workspace-a", issue=issue,
            comments=comments or [], timeline=events or timeline, messages=messages or [], tasks=tasks or {"run-1"}, complete=complete)
    with feature_sessions() as db:
        row = scan(db)
        identity, version = row.id, row.content_version
        db.commit()
        assert scan(db).content_version == version
        late = {"id": "run-1:0", "task_id": "run-1", "seq": 0, "created_at": before.isoformat(), "content": "late"}
        assert scan(db, messages=[late]).id == identity
        assert row.content_version == version + 1
        db.commit()
        followup = {"id": "web-comment", "author_type": "member", "author_id": "same-pat-owner",
                    "created_at": (utcnow() + timedelta(seconds=1)).isoformat(), "content": "Followed up"}
        assert scan(db, comments=[followup], complete=False).status == "open"
        assert scan(db, comments=[followup]).status == "withdrawn"
        assert row.close_reason == "检测到外部跟进"
        db.commit()
        assert scan(db, comments=[followup], messages=[late]) is None
        issue["status"] = "in_review"
        assert scan(db, comments=[followup]) is None
        new_event = {"id": "start-2", "action": "task_started", "created_at": (utcnow() + timedelta(seconds=2)).isoformat(), "details": {"task_id": "run-2"}}
        fresh = scan(db, comments=[followup], events=[*timeline, new_event], tasks={"run-1", "run-2"})
        assert fresh.id != identity and fresh.status == "open"
        assert db.scalar(select(func.count()).select_from(AutoLoopEvidence).where(AutoLoopEvidence.remote_id == "run-1:0")) == 1


def test_watchdog_excludes_exact_own_id_but_not_member_identity(feature_sessions, owner):
    issue = {"id": "remote-1", "identifier": "TASK-1", "status": "blocked"}
    with feature_sessions() as db:
        row = watchdog.reconcile_issue(db, owner_id=owner, workspace_id="workspace-a", issue=issue,
            comments=[], timeline=[], messages=[], tasks=set(), complete=True)
        event = store.append_event(db, owner_id=owner, type="fixture", source_kind="schedule", source_system="fixture", action="accepted")
        action, _ = store.persist_action(db, owner_id=owner, event_id=event.id,
            steps=[("issue.comment", {"workspaceId": "workspace-a", "issueRef": "TASK-1"})])
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
        claim = store.claim_step(db, owner_id=owner, step_id=step.id)
        store.finish_step(db, owner_id=owner, step_id=step.id, lease_token=claim.lease_token,
            outcome="confirmed", remote_object_id="own-comment")
        db.commit()
        own = {"id": "own-comment", "author_type": "member", "author_id": "same-member",
               "created_at": (utcnow() + timedelta(seconds=1)).isoformat()}
        def scan(comments):
            return watchdog.reconcile_issue(db, owner_id=owner, workspace_id="workspace-a", issue=issue,
                comments=comments, timeline=[], messages=[], tasks=set(), complete=True)
        assert scan([own]).status == "open"
        assert scan([own, {**own, "id": "web-comment"}]).status == "withdrawn"


def test_exact_disabled_model_does_not_use_default(feature_sessions, owner, monkeypatch):
    with feature_sessions() as db:
        db.add(ModelConfig(id="disabled", name="disabled", provider="openai", models=["fixture"],
            created_by=owner, enabled=False, config_type="llm"))
        db.commit()
        monkeypatch.setattr(planner, "select_llm_model_config", lambda **kw: pytest.fail("must not silently fall back"))
        with pytest.raises(planner.PlanningError, match="explicit_model_unavailable"):
            planner.plan_event(db, event=SimpleNamespace(type="fixture"), payload={},
                workspace_id="workspace-a", model_id="disabled")


def test_provider_daily_limit_stops_without_retry(monkeypatch):
    limit = Event()
    monkeypatch.setattr(provider, "_daily_limit", limit)
    calls = []
    class LimitError(Exception):
        status_code = 403
        body = {"code": "DAILY_LIMIT_EXCEEDED"}
    def call():
        calls.append(1)
        raise LimitError()
    with pytest.raises(provider.DailyLimitExceeded):
        provider._with_retry(call)
    assert calls == [1] and provider.daily_limit_reached()


def _submit(sessions, owner_id, source="watchdog", issue="TASK-1"):
    with sessions() as db:
        row = _card(db, owner_id, source, issue=issue)
        identity, version = row.id, row.content_version
        _, request, _ = store.submit_decision(db, owner_id=owner_id, decision_id=identity,
            actor_id=owner_id, expected_content_version=version, idempotency_key="answer",
            option_id="continue", source_resolver=_resolve(owner_id))
        accepted = service.decision_result(db, row, request, replay=False)
        db.commit()
    return identity, version, accepted


def _remote_issue(*args):
    return {"id": "remote-1", "identifier": "TASK-1", "workspace_id": "workspace-a"}


def test_replay_preserves_accepted_version_after_worker_completion(feature_sessions, owner, monkeypatch):
    monkeypatch.setattr(multica_client, "get_issue", _remote_issue)
    monkeypatch.setattr(multica_client, "create_comment", lambda *a, **kw:
        multica_client.MulticaCommentResult("comment-1", {"id": "comment-1"}, "unknown", {}))
    identity, version, accepted = _submit(feature_sessions, owner)
    assert worker.scan_actions(session_factory=feature_sessions) == 1
    with feature_sessions() as db:
        row, request, replay = store.submit_decision(db, owner_id=owner, decision_id=identity,
            actor_id=owner, expected_content_version=version, idempotency_key="answer", option_id="continue")
        completed_version = row.content_version
        result = service.decision_result(db, row, request, replay=replay)
        assert result["alreadyProcessed"] and result["status"] == "completed"
        assert result["contentVersion"] == accepted["contentVersion"] < completed_version
        assert all(step["state"] == "confirmed" for step in result["writebackSteps"])
        for changed in ({"expected_content_version": version + 1}, {"option_id": None, "free_text": "changed"}):
            kwargs = dict(owner_id=owner, decision_id=identity, actor_id=owner,
                expected_content_version=version, idempotency_key="answer", option_id="continue")
            with pytest.raises(store.StoreConflict, match="idempotency_key_conflict"):
                store.submit_decision(db, **{**kwargs, **changed})
        assert row.content_version == completed_version


def test_partial_failure_retries_original_step_and_never_repeats_confirmed_step(feature_sessions, owner, monkeypatch):
    writes = []
    monkeypatch.setattr(multica_client, "get_issue", _remote_issue)
    def comment(*args, **kwargs):
        writes.append(kwargs)
        if len(writes) == 1:
            raise multica_client.MulticaRequestError("rate limited", outcome="failed", status_code=429)
        return multica_client.MulticaCommentResult("comment-1", {"id": "comment-1"}, "unknown", {})
    monkeypatch.setattr(multica_client, "create_comment", comment)
    identity, _, _ = _submit(feature_sessions, owner, "assistant")
    assert worker.scan_actions(session_factory=feature_sessions) == 2
    with feature_sessions() as db:
        steps = list(db.scalars(select(AutoLoopActionStep).order_by(AutoLoopActionStep.ordinal)))
        original = [(step.id, step.remote_request_id) for step in steps]
        assert [step.state for step in steps] == ["confirmed", "failed"]
        assert db.scalar(select(AutoLoopAction)).state == "partial"
    assert worker.scan_actions(session_factory=feature_sessions) == 1
    assert worker.scan_actions(session_factory=feature_sessions) == 0
    with feature_sessions() as db:
        steps = list(db.scalars(select(AutoLoopActionStep).order_by(AutoLoopActionStep.ordinal)))
        assert [(step.id, step.remote_request_id) for step in steps] == original
        assert [step.attempts for step in steps] == [1, 2]
        assert db.get(AutoLoopDecision, identity).status == "completed"
        assert db.scalar(select(func.count()).select_from(AutoLoopEvent).where(
            AutoLoopEvent.type == "decision.answered")) == 1
    assert len(writes) == 2


def test_failed_retry_is_bounded_and_revoked_source_never_sends(feature_sessions, owner, monkeypatch):
    writes = []
    monkeypatch.setattr(multica_client, "get_issue", _remote_issue)
    def rejected(*args, **kwargs):
        writes.append(kwargs)
        raise multica_client.MulticaRequestError("rate limited", outcome="failed", status_code=429)
    monkeypatch.setattr(multica_client, "create_comment", rejected)
    identity, _, _ = _submit(feature_sessions, owner)
    for _ in range(5):
        worker.scan_actions(session_factory=feature_sessions)
    assert len(writes) == 3
    with feature_sessions() as db:
        step = db.scalar(select(AutoLoopActionStep))
        assert step.state == "failed" and step.attempts == 3
        # Revoking the frozen source makes every later attempt fail closed
        # before any write; the active profile is never borrowed instead.
        db.execute(update(SuperAssistantMulticaProfileRevision).values(revoked_at=utcnow()))
        db.commit()
    for _ in range(2):
        worker.scan_actions(session_factory=feature_sessions)
    with feature_sessions() as db:
        assert db.get(AutoLoopDecision, identity).status == "writeback_failed"
    assert len(writes) == 3


def test_remote_success_local_commit_failure_recovers_unknown_without_resend(feature_sessions, owner, monkeypatch):
    writes = []
    monkeypatch.setattr(multica_client, "get_issue", _remote_issue)
    monkeypatch.setattr(multica_client, "list_comments", lambda *a: multica_client.MulticaRead([], True))
    def comment(*args, **kwargs):
        writes.append(kwargs)
        return multica_client.MulticaCommentResult("remote-success", {"id": "remote-success"}, "unknown", {})
    monkeypatch.setattr(multica_client, "create_comment", comment)
    identity, _, _ = _submit(feature_sessions, owner)
    with feature_sessions() as db:
        step_id = db.scalar(select(AutoLoopActionStep.id))
    class CommitFailure(Session):
        def commit(self):
            if self.scalar(select(AutoLoopActionStep.state).where(AutoLoopActionStep.id == step_id)) == "confirmed":
                raise RuntimeError("injected local commit failure after remote success")
            return super().commit()
    faulty_sessions = sessionmaker(bind=feature_sessions.kw["bind"], class_=CommitFailure, expire_on_commit=False)
    with pytest.raises(RuntimeError, match="local commit failure"):
        worker.execute_step(owner, step_id, session_factory=faulty_sessions)
    with feature_sessions() as db:
        step = db.get(AutoLoopActionStep, step_id)
        assert step.state == "sending" and step.attempts == 1
        step.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
    worker.scan_actions(session_factory=feature_sessions)
    worker.scan_actions(session_factory=feature_sessions)
    with feature_sessions() as db:
        assert db.get(AutoLoopActionStep, step_id).state == "unknown"
        assert db.get(AutoLoopDecision, identity).status == "writeback_unknown"
    assert len(writes) == 1


def test_inactive_owner_is_visible_without_routing_or_connector_reads(feature_sessions, owner, monkeypatch):
    _, work_id = _admit(feature_sessions, owner)
    with feature_sessions() as db:
        db.get(User, owner).is_active = False
        db.commit()
    monkeypatch.setattr(dispatching, "business_router", lambda *a: pytest.fail("inactive owner must not route"))
    monkeypatch.setattr(multica_client, "get_issue", lambda *a: pytest.fail("inactive owner must not read connector"))
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        assert db.get(AutoLoopDispatch, work_id).state == "awaiting_owner"
        assert db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.type == "routing.awaiting_owner"))
        assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0


def test_multica_daily_limit_aborts_worker_and_all_later_scans(feature_sessions, owner, monkeypatch):
    monkeypatch.setattr(provider, "_daily_limit", Event())
    monkeypatch.setattr(multica_client, "normalize_base_url", lambda value: value)
    calls = []
    def limited(method, url, **kwargs):
        calls.append((method, url))
        return httpx.Response(403, json={"code": "DAILY_LIMIT_EXCEEDED"})
    monkeypatch.setattr(multica_client, "_request", limited)
    _submit(feature_sessions, owner)
    for scan in (worker.scan_actions, worker.scan_actions, watchdog.scan_watchdog, dispatching.scan_pending):
        with pytest.raises(provider.DailyLimitExceeded):
            scan(session_factory=feature_sessions)
    assert len(calls) == 1
    with feature_sessions() as db:
        assert db.scalar(select(AutoLoopActionStep)).state == "sending"
        assert not db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.type == "routing.fallback"))


@pytest.mark.parametrize("action", ["ignore", "create_decision", "unavailable"])
def test_watchdog_assessment_cache_choice_and_conservative_fallback(feature_sessions, owner, monkeypatch, action):
    calls = []
    def assess(db, **kwargs):
        calls.append(kwargs)
        assert kwargs["decision_only"] is True and kwargs["trusted_issue"] == "TASK-1"
        if action == "unavailable":
            raise planner.PlanningError("model_unavailable")
        return planner.Plan(action=action, reason="needs choice" if action == "create_decision" else "waiting",
            **({"title": "Choose recovery", "question": "Retry or wait?", "options": [
                {"id": "retry", "label": "Retry"}, {"id": "wait", "label": "Wait"}]} if action == "create_decision" else {}))
    monkeypatch.setattr(planner, "plan_event", assess)
    issue = {"id": "remote-1", "identifier": "TASK-1", "status": "blocked"}
    with feature_sessions() as db:
        for _ in range(2):
            row = watchdog.reconcile_issue(db, owner_id=owner, workspace_id="workspace-a", issue=issue,
                comments=[], timeline=[], messages=[], tasks=set(), complete=True, assess=watchdog._assess)
            db.commit()
        assert len(calls) == (2 if action == "unavailable" else 1)
        if action == "ignore":
            assert row is None
        elif action == "create_decision":
            assert row.question == "Retry or wait?" and len(row.options) == 2
        else:
            assert row.status == "open" and row.options == []
            assert service.decision_out(db, row)["freeTextAllowed"] is True


def test_routing_local_fault_rolls_back_attempt_before_fallback(feature_sessions, owner, monkeypatch):
    with feature_sessions() as db:
        service.create_rule(db, owner, _rule())
        db.commit()
    _, work_id = _admit(feature_sessions, owner)
    original = store.append_event
    def fail_completed(db, **kwargs):
        if kwargs.get("type") == "routing.completed":
            # Fail inside SQL so the savepoint must restore transaction usability.
            db.execute(select(AutoLoopAction)).scalar_one()
            from sqlalchemy import text
            db.execute(text("SELECT 1 / 0"))
        return original(db, **kwargs)
    monkeypatch.setattr(store, "append_event", fail_completed)
    dispatching.process_dispatch(work_id, session_factory=feature_sessions)
    with feature_sessions() as db:
        assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0
        assert db.scalar(select(func.count()).select_from(AutoLoopActionStep)) == 0
        assert db.scalar(select(AutoLoopDecision)).source == "kernel_fallback"
        assert db.get(AutoLoopDispatch, work_id).state == "done"


def test_http_rule_array_preview_owner_summary_and_decision_replay(feature_sessions, owner):
    api = FastAPI()
    api.include_router(router, prefix="/api/v2/super-assistant")
    current = [owner]
    def database():
        with feature_sessions() as db:
            yield db
    api.dependency_overrides[get_db] = database
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=current[0])
    base = "/api/v2/super-assistant/autoloop"
    with TestClient(api) as client:
        created = client.post(base + "/rules", json=_rule().model_dump(exclude_none=True))
        assert created.status_code == 201
        rule = created.json()
        listed = client.get(base + "/rules")
        assert isinstance(listed.json(), list) and listed.json()[0]["id"] == rule["id"]
        sample = {"title": "sample", "body": "original"}
        preview = client.post(base + f"/rules/{rule['id']}/preview", json={"sampleEvent": sample})
        assert preview.status_code == 200 and preview.json()["ruleVersion"] == 1
        draft = client.post(base + "/rules/preview", json={"rule": _rule().model_dump(exclude_none=True), "sampleEvent": sample})
        assert draft.status_code == 200 and draft.json()["renderedTitle"] == preview.json()["renderedTitle"]
        assert client.patch(base + f"/rules/{rule['id']}", json={"titleTemplate": "{% invalid %}"}).status_code == 422
        assert client.get(base + "/events?cursor=bad").status_code == 400
        assert client.get(base + "/decision-items?status=OPEN").status_code == 422
        assert client.get(base + "/events?limit=101").status_code == 422
        with feature_sessions() as db:
            row = _card(db, owner)
            identity, version = row.id, row.content_version
            db.commit()
        summary = client.get(base + "/decision-items/summary")
        assert summary.status_code == 200 and summary.json() == {
            "openCount": 1, "pendingDispatchCount": 0, "sendingCount": 0,
            "unknownCount": 0, "failedCount": 0, "unfinishedTotal": 1}
        body = {"expectedContentVersion": version, "idempotencyKey": "handled"}
        first = client.post(base + f"/decision-items/{identity}/mark-handled", json=body)
        assert first.status_code == 200 and first.json()["resolution"]["kind"] == "handled"
        replay = client.post(base + f"/decision-items/{identity}/mark-handled", json=body)
        assert replay.status_code == 200 and replay.json()["alreadyProcessed"]
        assert replay.json()["contentVersion"] == first.json()["contentVersion"]
        assert client.post(base + f"/decision-items/{identity}/mark-handled", json={**body, "reason": "changed"}).status_code == 409
        current[0] = str(uuid4())
        assert client.get(base + f"/decision-items/{identity}").status_code == 404
        assert client.get(base + f"/rules/{rule['id']}").status_code == 404
        assert client.get(base + "/rules").json() == []
        current[0] = owner
        assert client.delete(base + f"/rules/{rule['id']}").status_code == 204


def test_nonterminal_replay_returns_acceptance_without_reclaiming(feature_sessions, owner):
    identity, version, accepted = _submit(feature_sessions, owner)
    with feature_sessions() as db:
        step = db.scalar(select(AutoLoopActionStep))
        store.claim_step(db, owner_id=owner, step_id=step.id)
        db.commit()
        current_version = db.get(AutoLoopDecision, identity).content_version
        row, request, replay = store.submit_decision(db, owner_id=owner, decision_id=identity,
            actor_id=owner, expected_content_version=version, idempotency_key="answer", option_id="continue")
        result = service.decision_result(db, row, request, replay=replay)
        assert result["alreadyProcessed"] and result["status"] == "pending_dispatch"
        assert result["contentVersion"] == accepted["contentVersion"] < current_version
        assert result["updatedAt"] == accepted["updatedAt"]
        assert result["writebackSteps"] == accepted["writebackSteps"]
        assert row.content_version == current_version and step.attempts == 1
        assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 1


def test_new_work_runs_when_old_failures_and_unknowns_exceed_batch(feature_sessions, owner, monkeypatch):
    with feature_sessions() as db:
        for state in ("failed", "unknown"):
            for index in range(3):
                event = store.append_event(db, owner_id=owner, type="fixture", source_kind="schedule",
                    source_system="fixture", action="accepted")
                action, _ = store.persist_action(db, owner_id=owner, event_id=event.id,
                    steps=[("issue.comment", {"workspaceId": "workspace-a", "baseUrl": "http://127.0.0.1:18002",
                        "issueRef": f"OLD-{state}-{index}", "answer": "old"})])
                step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
                claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
                store.finish_step(db, owner_id=owner, step_id=step.id, lease_token=claimed.lease_token,
                    outcome=state, proof={"kind": "remote_rejection", "httpStatus": 400} if state == "failed" else None)
        db.commit()
    identity, _, _ = _submit(feature_sessions, owner)
    writes = []
    monkeypatch.setattr(multica_client, "get_issue", _remote_issue)
    monkeypatch.setattr(multica_client, "list_comments", lambda *a: multica_client.MulticaRead([], True))
    def comment(*a, **kw):
        writes.append(kw)
        return multica_client.MulticaCommentResult("new-comment", {"id": "new-comment"}, "unknown", {})
    monkeypatch.setattr(multica_client, "create_comment", comment)
    assert worker.scan_actions(session_factory=feature_sessions, limit=1) == 1
    assert writes == [{"content": "Continue"}]
    with feature_sessions() as db:
        assert db.get(AutoLoopDecision, identity).status == "completed"
        assert db.scalar(select(func.count()).select_from(AutoLoopActionStep).where(
            AutoLoopActionStep.state == "unknown")) == 3


def test_watchdog_retries_unavailable_assessment_and_invalidates_model_revision(feature_sessions, owner, monkeypatch):
    calls = []
    def assess(db, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise planner.PlanningError("model_unavailable")
        return planner.Plan(action="create_decision", reason="choice required", title="Recovered assessment",
            question="Retry or wait?", options=[{"id": "retry", "label": "Retry"}, {"id": "wait", "label": "Wait"}])
    monkeypatch.setattr(planner, "plan_event", assess)
    issue = {"id": "remote-1", "identifier": "TASK-1", "status": "blocked"}
    with feature_sessions() as db:
        def scan():
            row = watchdog.reconcile_issue(db, owner_id=owner, workspace_id="workspace-a", issue=issue,
                comments=[], timeline=[], messages=[], tasks=set(), complete=True, assess=watchdog._assess)
            db.commit()
            return row
        row = scan()
        identity, version = row.id, row.content_version
        assert row.options == []
        recovered = scan()
        assert recovered.id == identity and recovered.question == "Retry or wait?"
        assert recovered.content_version > version and len(calls) == 2
        scan()
        assert len(calls) == 2
        db.add(ModelConfig(name="new config", provider="openai", models=["fixture"],
            created_by=owner, enabled=True, config_type="llm"))
        db.commit()
        assert scan().id == identity and len(calls) == 3


def test_connector_success_echoes_cannot_persist_pat_or_extra_response_fields(feature_sessions, owner, monkeypatch):
    secret = "isolated-fixture-token"
    monkeypatch.setattr(multica_client, "get_issue", lambda *a: {
        **_remote_issue(), "token": secret, "description": secret, "nested": {"Authorization": secret}})
    monkeypatch.setattr(multica_client, "create_comment", lambda *a, **kw:
        multica_client.MulticaCommentResult("comment-safe", {"id": "comment-safe", "content": secret,
            "authorization": secret}, "unknown", {"supplements": [{"task_id": "run-safe", "status": "pending",
                "failure_reason": secret, "token": secret}], "supplement_failure_reason": secret}))
    _submit(feature_sessions, owner)
    assert worker.scan_actions(session_factory=feature_sessions) == 1
    with feature_sessions() as db:
        step = db.scalar(select(AutoLoopActionStep))
        assert step.remote_result == {"comment": {"id": "comment-safe"}, "issue": _remote_issue()}
        assert step.execution_evidence == {"state": "unknown", "supplements": [{"task_id": "run-safe", "status": "pending"}]}
        assert secret not in json.dumps([event.data for event in db.scalars(select(AutoLoopEvent))])


def test_schedule_thread_binding_reuses_created_issue_and_rejects_connector_drift(feature_sessions, owner, monkeypatch):
    from app.super_assistant.autoloop.models import AutoLoopBinding

    monkeypatch.setattr(multica_client, "create_issue", lambda *a, **kw: _remote_issue())
    reads = []
    def get_issue(*args):
        reads.append(args)
        return _remote_issue()
    monkeypatch.setattr(multica_client, "get_issue", get_issue)
    with feature_sessions() as db:
        service.create_rule(db, owner, _rule())
        db.commit()
    task_id = str(uuid4())
    def admit():
        with feature_sessions() as db:
            _, event, _ = store.accept_receipt(db, source_kind="schedule", source_system="fixture",
                source_event_id=str(uuid4()), owner_id=owner, scheduled_task_id=task_id,
                payload={"title": "event", "body": "original"})
            work = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.event_id == event.id))
            db.commit()
            return event.id, work.id
    _, first = admit()
    dispatching.process_dispatch(first, session_factory=feature_sessions)
    assert worker.scan_actions(session_factory=feature_sessions) == 1
    with feature_sessions() as db:
        binding = db.scalar(select(AutoLoopBinding))
        assert binding.source_scope == "schedule:" + task_id and binding.source_thread == task_id
        assert binding.provenance["kind"] == "created_issue"
        assert binding.provenance["baseUrl"] == BASE_A
        assert binding.provenance["profileRevisionId"]
    second_event, second = admit()
    dispatching.process_dispatch(second, session_factory=feature_sessions)
    with feature_sessions() as db:
        action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == second_event))
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
        assert step.kind == "issue.comment" and step.request_payload["issueRef"] == "TASK-1"
        # Enrollment keeps one endpoint per workspace, so a live connector
        # change cannot occur; simulate persisted provenance drift instead.
        binding = db.scalar(select(AutoLoopBinding))
        binding.provenance = {**binding.provenance, "baseUrl": "http://127.0.0.1:18003"}
        db.commit()
    third_event, third = admit()
    dispatching.process_dispatch(third, session_factory=feature_sessions)
    with feature_sessions() as db:
        fallback = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.parent_event_id == third_event,
            AutoLoopEvent.type == "routing.fallback"))
        assert fallback.error == "binding_connector_changed"
        assert db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == third_event)) is None
    assert len(reads) == 1
