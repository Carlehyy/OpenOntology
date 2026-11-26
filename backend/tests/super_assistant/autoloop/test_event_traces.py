"""Routing workbench trace read model: fact projection on disposable SQLite.

Facts are seeded exactly as the write side persists them (received anchor,
routing.prepared, routing.completed/routing.fallback, actions, decisions),
so route/outcome stay a pure projection: matched-but-failed still counts as
one matched receipt, degenerate no-rule routing reads assistant, and a
terminal without its effect rows never reads as success.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.shared.database import Base
from app.super_assistant.autoloop import schemas, service, store, trace_service
from app.super_assistant.autoloop.models import (
    AUTOLOOP_TABLES, AutoLoopAction, AutoLoopActionStep, AutoLoopDecision,
    AutoLoopDispatch, AutoLoopEvent, AutoLoopReceipt, AutoLoopRule, utcnow,
)
from app.super_assistant.autoloop.router import router
from app.super_assistant.models import SuperAssistantMulticaConfig

_TABLES = [User.__table__, SuperAssistantMulticaConfig.__table__, *AUTOLOOP_TABLES]
BASE = "/api/v2/super-assistant/autoloop"
_ITEM_KEYS = {"id", "receivedAt", "title", "source", "route", "outcome", "rule",
              "action", "decisionItemId", "issueRef", "scheduledTaskId",
              "scheduledRunId", "reason", "routingPromptVersion", "modelConfigId"}


@pytest.fixture
def harness(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'event-traces.db'}",
        connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=_TABLES)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    current = {"user": "owner-a"}
    api = FastAPI()
    api.include_router(router, prefix="/api/v2/super-assistant")

    def override_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=current["user"])
    with sessions() as db:
        for identity in ("owner-a", "owner-b"):
            db.add(User(id=identity, username=identity, email=f"{identity}@test.invalid",
                        password_hash="fixture-no-login", is_active=True))
        db.commit()
    with TestClient(api) as client:
        yield SimpleNamespace(client=client, sessions=sessions, current=current)


def _admit_at(db, owner, accepted, *, source_event_id, source_kind="schedule",
              source_system="fixture", payload=None,
              scheduled_task_id=None, scheduled_run_id=None):
    """Mirror store.accept_receipt with a pinned accepted anchor instant."""
    payload = payload if payload is not None else {"title": f"event {source_event_id}"}
    identity = [source_kind, source_system, source_event_id]
    receipt = AutoLoopReceipt(
        source_kind=source_kind, source_system=source_system, source_event_id=source_event_id,
        dedup_key=hashlib.sha256(
            json.dumps(identity, ensure_ascii=False).encode()).hexdigest(),
        owner_id=owner, payload=payload, accepted_at=accepted,
        scheduled_task_id=scheduled_task_id, scheduled_run_id=scheduled_run_id)
    db.add(receipt)
    db.flush()
    event = store.append_event(db, receipt_id=receipt.id, owner_id=owner,
        type=f"{source_kind}.received", source_kind=source_kind, source_system=source_system,
        source_ref=source_event_id, happened_at=accepted, action="accepted", data=payload)
    db.add(AutoLoopDispatch(event_id=event.id, owner_id=owner))
    db.flush()
    return receipt, event


def _admit(db, owner, **changes):
    values = dict(source_kind="schedule", source_system="fixture",
                  source_event_id=f"event-{uuid4().hex[:8]}", owner_id=owner,
                  payload={"title": "event"})
    values.update(changes)
    return store.accept_receipt(db, **values)


def _rule(db, owner, name="route"):
    created = service.create_rule(db, owner, schemas.RuleCreate.model_validate(
        {"name": name, "titleTemplate": "[rule] {{ title }}", "descriptionTemplate": "{{ body }}"}))
    return db.scalar(select(AutoLoopRule).where(AutoLoopRule.id == created["id"]))


def _prepared(db, event, rule=None):
    return store.append_event(db, owner_id=event.owner_id, receipt_id=event.receipt_id,
        type="routing.prepared", source_kind=event.source_kind, source_system=event.source_system,
        source_ref=event.source_ref, parent_event_id=event.id, hops=event.hops, action="pending",
        rule_id=rule.id if rule else None, rule_version=rule.version if rule else None,
        rule_snapshot=service.rule_out(rule) if rule else None, data={"originEventId": event.id})


def _completed(db, event, prepared, *, action="create_task", issue_ref=None,
               decision=None, effect=None, reason="first_matching_rule"):
    return store.append_event(db, owner_id=event.owner_id, receipt_id=event.receipt_id,
        type="routing.completed", source_kind=event.source_kind, source_system=event.source_system,
        source_ref=event.source_ref, parent_event_id=event.id, hops=event.hops,
        rule_id=prepared.rule_id, rule_version=prepared.rule_version,
        rule_snapshot=prepared.rule_snapshot, action=action, issue_ref=issue_ref,
        decision_item_id=decision.id if decision else None,
        action_id=effect.id if effect else None,
        data={"plan": {"action": action, "reason": reason}, "untrustedOriginal": event.data})


def _fallback(db, event, prepared, decision, reason="model_unavailable"):
    return store.append_event(db, owner_id=event.owner_id, receipt_id=event.receipt_id,
        type="routing.fallback", source_kind=event.source_kind, source_system=event.source_system,
        source_ref=event.source_ref, parent_event_id=event.id, hops=event.hops,
        rule_id=prepared.rule_id, rule_version=prepared.rule_version,
        rule_snapshot=prepared.rule_snapshot, action="kernel_fallback", error=reason,
        decision_item_id=decision.id, data={"reason": reason})


def _card(db, owner, event, *, issue="TASK-1", source="assistant"):
    row, _ = store.create_or_update_decision(db, owner_id=owner, workspace_id="workspace-a",
        issue_key=issue, source=source, need_key="event:" + event.id, title="Choose",
        question="Continue?", options=[{"id": "continue", "label": "Continue"}],
        evidence={"factsSummary": "observed", "untrustedOriginal": event.data,
                  "eventId": event.id}, origin_event_id=event.id)
    return row


def _effect(db, owner, event, kind="issue.create", payload=None):
    action, _ = store.persist_action(db, owner_id=owner, event_id=event.id,
        steps=[(kind, payload or {"workspaceId": "workspace-a",
                                   "baseUrl": "http://127.0.0.1:18002", "title": "event"})])
    return action


def _finish(db, owner, action, outcome, *, remote_object_id=None, remote_result=None):
    step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
    claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
    return store.finish_step(db, owner_id=owner, step_id=step.id,
        lease_token=claimed.lease_token, outcome=outcome,
        remote_object_id=remote_object_id, remote_result=remote_result,
        proof={"kind": "not_sent"} if outcome == "failed" else None)


def _traces(client, **params):
    response = client.get(BASE + "/event-traces", params=params)
    assert response.status_code == 200
    return response.json()


def _by_ref(page):
    return {item["source"]["ref"]: item for item in page["items"]}


def test_route_outcome_fact_projection_and_payload_stays_out(harness):
    sessions, owner = harness.sessions, "owner-a"
    with sessions() as db:
        rule = _rule(db, owner)
        rule_id = rule.id
        # pending: admitted, not yet prepared.
        _admit(db, owner, source_event_id="pending-1")
        # rule matched, writeback failed (matched+failed stays one matched receipt).
        _, event, _ = _admit(db, owner, source_event_id="failed-1")
        prepared = _prepared(db, event, rule)
        failed_action = _effect(db, owner, event)
        _completed(db, event, prepared, effect=failed_action)
        _finish(db, owner, failed_action, "failed")
        # A confirmed create is traceable even when it has no thread binding or URL.
        _, event, _ = _admit(db, owner, source_event_id="confirmed-1")
        prepared = _prepared(db, event, rule)
        confirmed_action = _effect(db, owner, event)
        _completed(db, event, prepared, effect=confirmed_action)
        _finish(db, owner, confirmed_action, "confirmed", remote_object_id="TASK-9",
                remote_result={"issue": {"id": "9", "identifier": "TASK-9"}})
        # degenerate: no rule matched, planner chose a decision card.
        _, event, _ = _admit(db, owner, source_event_id="assistant-1")
        prepared = _prepared(db, event)
        _completed(db, event, prepared, action="create_decision",
                   decision=_card(db, owner, event, issue=None),
                   reason="assistant needs human choice")
        # rule matched but planning degraded to a fallback review card.
        _, event, _ = _admit(db, owner, source_event_id="fallback-1")
        prepared = _prepared(db, event, rule)
        _fallback(db, event, prepared,
                  _card(db, owner, event, issue="TASK-2", source="kernel_fallback"))
        # assistant ignored the event.
        _, event, _ = _admit(db, owner, source_event_id="ignored-1")
        prepared = _prepared(db, event)
        _completed(db, event, prepared, action="ignore", reason="duplicate noise")
        # terminal claims create_task but no action row exists in facts.
        _, event, _ = _admit(db, owner, source_event_id="ghost-1")
        prepared = _prepared(db, event, rule)
        _completed(db, event, prepared)
        # scheduled identity and notification source naming.
        _admit_at(db, owner, utcnow(), source_event_id="sched-1",
                  scheduled_task_id="task-1", scheduled_run_id="run-1")
        _, event, _ = _admit(db, owner, source_kind="notification", source_system="sys-n",
                          source_event_id="event:n-1",
                          payload={"title": "notify", "notificationId": "n-1", "body": "b"})
        assert event.source_ref == "event:n-1"
        db.commit()
    page = _traces(harness.client)
    items = _by_ref(page)
    assert set(items) == {"pending-1", "failed-1", "confirmed-1", "assistant-1",
                          "fallback-1", "ignored-1", "ghost-1", "sched-1", "n-1"}
    assert all(set(item) == _ITEM_KEYS for item in items.values())
    assert items["pending-1"]["route"] == "pending" and items["pending-1"]["outcome"] == "pending"
    failed = items["failed-1"]
    assert failed["route"] == "rule" and failed["outcome"] == "failed"
    assert failed["rule"] == {"id": rule_id, "name": "route", "version": 1}
    assert failed["action"] == "create_task"
    confirmed = items["confirmed-1"]
    assert confirmed["outcome"] == "confirmed" and confirmed["issueRef"] == "TASK-9"
    detail = harness.client.get(BASE + f"/event-traces/{confirmed['id']}").json()
    assert detail["issueRef"] == "TASK-9" and detail["issueUrl"] is None
    assert detail["actionSteps"][0]["remoteObjectId"] == "TASK-9"
    assistant = items["assistant-1"]
    assert assistant["route"] == "assistant" and assistant["outcome"] == "review"
    assert assistant["reason"] == "assistant needs human choice" and assistant["rule"] is None
    assert assistant["action"] == "create_decision" and assistant["decisionItemId"]
    fallback = items["fallback-1"]
    assert fallback["route"] == "rule" and fallback["outcome"] == "review"
    assert items["ignored-1"]["outcome"] == "ignored" and items["ignored-1"]["route"] == "assistant"
    assert items["ghost-1"]["outcome"] == "failed"  # no action row never reads as success
    assert items["sched-1"]["scheduledTaskId"] == "task-1"
    assert items["sched-1"]["scheduledRunId"] == "run-1"
    notification = items["n-1"]
    assert notification["title"] == "notify" and notification["source"]["ref"] == "n-1"
    assert notification["source"] == {"kind": "notification", "system": "sys-n", "ref": "n-1"}
    stats = harness.client.get(BASE + "/routing-stats", params={"days": 30}).json()
    assert stats["received"] == len(items) == 9
    assert stats["matched"] + stats["unmatched"] + stats["pendingRoute"] == 9
    assert (stats["matched"], stats["unmatched"], stats["pendingRoute"]) == (4, 2, 3)
    assert stats["outcomes"]["failed"] == 2 and stats["outcomes"]["review"] == 2
    assert stats["outcomes"]["confirmed"] == 1 and stats["outcomes"]["ignored"] == 1
    assert stats["outcomes"]["pending"] == 3
    assert stats["rules"] == [{"ruleId": rule_id, "ruleName": "route", "hits": 4}]


def test_assistant_reason_is_truncated_summary_in_list_only(harness):
    sessions, owner = harness.sessions, "owner-a"
    long_reason = "assistant judged " + "detail " * 60
    with sessions() as db:
        _, event, _ = _admit(db, owner, source_event_id="assistant-long")
        prepared = _prepared(db, event)
        _completed(db, event, prepared, action="create_decision",
                   decision=_card(db, owner, event, issue=None), reason=long_reason)
        _, event, _ = _admit(db, owner, source_event_id="rule-long")
        prepared = _prepared(db, event, _rule(db, owner))
        _completed(db, event, prepared, reason=long_reason,
                   effect=_effect(db, owner, event))
        db.commit()
    items = _by_ref(_traces(harness.client))
    summary = items["assistant-long"]["reason"]
    assert summary is not None and len(summary) == 200 and summary.endswith("…")
    assert items["rule-long"]["reason"] is None
    detail = harness.client.get(
        BASE + f"/event-traces/{items['assistant-long']['id']}").json()
    assert detail["reason"] == long_reason


def test_owner_scope_stage_resolution_and_null_without_receipt(harness):
    sessions = harness.sessions
    with sessions() as db:
        rule = _rule(db, "owner-a")
        _, event, _ = _admit(db, "owner-a", source_event_id="stage-1")
        prepared = _prepared(db, event, rule)
        card = _card(db, "owner-a", event, issue=None)
        terminal = _completed(db, event, prepared, action="create_decision", decision=card)
        card_fact = db.scalar(select(AutoLoopEvent).where(
            AutoLoopEvent.decision_item_id == card.id, AutoLoopEvent.type == "decision.created"))
        answered = store.append_event(db, owner_id="owner-a", type="decision.answered",
            source_kind="decision", source_system="openontology", source_ref=card.id,
            parent_event_id=card.origin_event_id, hops=event.hops + 1, action="accepted",
            decision_item_id=card.id, correlation_id="step-1", data={"answer": "Continue"})
        watchdog = store.append_event(db, owner_id="owner-a", type="watchdog.assessed",
            source_kind="multica", source_system="multica", source_ref="TASK-1",
            action="ignore", data={"plan": {"action": "ignore"}})
        # Another owner's trace must stay invisible.
        _, other_event, _ = _admit(db, "owner-b", source_event_id="other-1")
        _prepared(db, other_event)
        db.commit()
    client = harness.client
    assert set(_by_ref(_traces(client))) == {"stage-1"}
    for stage_id in (event.id, prepared.id, terminal.id, card_fact.id, answered.id):
        response = client.get(BASE + f"/event-traces/{stage_id}")
        assert response.status_code == 200 and response.json()["id"] == event.id
    assert client.get(BASE + f"/event-traces/{uuid4()}").status_code == 404
    assert client.get(BASE + f"/event-traces/{other_event.id}").status_code == 404
    null = client.get(BASE + f"/event-traces/{watchdog.id}")
    assert null.status_code == 200 and null.json() is None
    harness.current["user"] = "owner-b"
    assert set(_by_ref(_traces(client))) == {"other-1"}
    assert client.get(BASE + "/routing-stats").json()["received"] == 1
    assert client.get(BASE + f"/event-traces/{event.id}").status_code == 404


def test_filters_time_window_and_cursor_binding(harness):
    sessions, owner = harness.sessions, "owner-a"
    base = utcnow().replace(microsecond=0)
    with sessions() as db:
        rule = _rule(db, owner, name="match-me")
        for index, (delta, kind) in enumerate([(timedelta(hours=2), "notification"),
                                               (timedelta(hours=1), "schedule"),
                                               (timedelta(minutes=30), "schedule")]):
            _, event = _admit_at(db, owner, base - delta,
                source_event_id=f"win-{index}", source_kind=kind, payload={"title": f"win {index}"})
            prepared = _prepared(db, event, rule if index < 2 else None)
            _completed(db, event, prepared, action="create_task" if index < 2 else "ignore",
                       effect=_effect(db, owner, event) if index < 2 else None)
        _admit_at(db, owner, base - timedelta(days=9), source_event_id="too-old")
        _admit_at(db, owner, base, source_event_id="notif-1", source_kind="notification",
                  source_system="sys-x", payload={"title": "n", "notificationId": "n-1"})
        db.commit()
        rule_id = rule.id
    client = harness.client
    since, until = (base - timedelta(hours=3)).isoformat(), base.isoformat()
    window = _traces(client, since=since, until=until)
    assert set(_by_ref(window)) == {"win-0", "win-1", "win-2"}
    assert set(_by_ref(_traces(client, source="notification", since=since, until=until))) == {"win-0"}
    assert _traces(client, sourceSystem="sys-x", since=since, until=until)["items"] == []
    assert set(_by_ref(_traces(client, sourceSystem="sys-x"))) == {"n-1"}
    assert set(_by_ref(_traces(client, source="notification"))) == {"n-1", "win-0"}
    assert set(_by_ref(_traces(client, route="rule"))) == {"win-0", "win-1"}
    assert set(_by_ref(_traces(client, route="assistant"))) == {"win-2"}
    assert set(_by_ref(_traces(client, outcome="ignored"))) == {"win-2"}
    assert set(_by_ref(_traces(client, ruleId=rule_id))) == {"win-0", "win-1"}
    assert set(_by_ref(_traces(client, action="ignore"))) == {"win-2"}
    assert _traces(client, scheduledTaskId="missing")["items"] == []
    # Half-open [since, until): the too-old and until-instant receipts stay out.
    bounded = _traces(client, since=(base - timedelta(days=8)).isoformat(), until=until)
    assert "too-old" not in _by_ref(bounded) and "notif-1" not in _by_ref(bounded)
    # Cursor walks the stable keyset and is bound to owner and filters.
    first = _traces(client, since=since, until=until, limit=1)
    assert first["hasMore"] and len(first["items"]) == 1
    second = _traces(client, since=since, until=until, limit=1, cursor=first["nextCursor"])
    assert len(second["items"]) == 1
    assert second["items"][0]["id"] != first["items"][0]["id"]
    assert client.get(BASE + "/event-traces", params={
        "cursor": first["nextCursor"], "outcome": "confirmed"}).status_code == 400
    assert client.get(BASE + "/event-traces", params={"route": "rules"}).status_code == 422
    assert client.get(BASE + "/event-traces", params={"outcome": "done"}).status_code == 422
    assert client.get(BASE + "/event-traces", params={"since": "not-a-date"}).status_code == 422
    assert client.get(BASE + "/event-traces", params={"until": "2026-11-19 00:00"}).status_code == 422
    harness.current["user"] = "owner-b"
    assert client.get(BASE + "/event-traces", params={
        "cursor": first["nextCursor"], "since": since, "until": until}).status_code == 400


def test_same_instant_keyset_pagination_and_replay_dedup(harness):
    sessions, owner = harness.sessions, "owner-a"
    instant = utcnow().replace(microsecond=123000)
    with sessions() as db:
        events = [_admit_at(db, owner, instant, source_event_id=f"same-{index}")[1]
                  for index in range(5)]
        db.commit()
    expected = [event.id for event in sorted(events, key=lambda row: row.id, reverse=True)]
    collected, cursor = [], None
    for _ in range(5):
        page = _traces(harness.client, limit=2, cursor=cursor)
        collected.extend(item["id"] for item in page["items"])
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            break
    assert collected == expected
    # A repeated delivery of the same source event adjudicates to one receipt.
    with sessions() as db:
        _, original, created = _admit(db, owner, source_event_id="same-0")
        db.commit()
        assert not created
        assert original.id == next(item.id for item in events if item.source_ref == "same-0")
    assert len(_traces(harness.client)["items"]) == 5


@pytest.mark.parametrize("status,outcome", [
    ("open", "review"), ("pending_dispatch", "queued"), ("sending", "sending"),
    ("writeback_unknown", "unknown"), ("writeback_failed", "failed"),
    ("completed", "recorded"), ("handled", "handled"), ("withdrawn", "cancelled"),
])
def test_decision_status_outcome_matrix(harness, status, outcome):
    owner = "owner-a"
    with harness.sessions() as db:
        rule = _rule(db, owner, name="fallback-rule")
        _, event, _ = _admit(db, owner, source_event_id=f"matrix-{status}")
        prepared = _prepared(db, event, rule)
        card = _card(db, owner, event, issue=f"ISSUE-{status}", source="kernel_fallback")
        _fallback(db, event, prepared, card, reason="matrix")
        card.status = status
        db.commit()
    items = _by_ref(_traces(harness.client, outcome=outcome))
    assert set(items) == {f"matrix-{status}"}
    assert items[f"matrix-{status}"]["route"] == "rule"


@pytest.mark.parametrize("issue_ref,outcome", [(None, "recorded"), ("TASK-1", "confirmed")])
def test_submitted_decision_only_confirms_multica_when_comment_is_confirmed(harness, issue_ref, outcome):
    owner = "owner-a"
    with harness.sessions() as db:
        _, event, _ = _admit(db, owner, source_event_id="answer")
        card = _card(db, owner, event, issue=issue_ref)
        _completed(db, event, _prepared(db, event), action="create_decision", decision=card)
        store.submit_decision(db, owner_id=owner, decision_id=card.id, actor_id=owner,
            expected_content_version=card.content_version, idempotency_key="submit",
            option_id="continue")
        effect = db.scalar(select(AutoLoopAction).where(AutoLoopAction.decision_item_id == card.id))
        steps = service._steps(db, effect)
        for step in steps:
            claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
            store.finish_step(db, owner_id=owner, step_id=step.id, lease_token=claimed.lease_token,
                outcome="confirmed", remote_object_id="comment-1" if step.kind == "issue.comment" else "local-event")
        assert card.status == "completed"
        db.commit()
    client = harness.client
    assert len(_traces(client, outcome=outcome)["items"]) == 1
    detail = client.get(BASE + f"/event-traces/{event.id}").json()
    assert detail["outcome"] == outcome
    assert detail["actionSteps"][0]["remoteObjectId"] is None
    if issue_ref:
        assert detail["actionSteps"][1]["remoteObjectId"] == "comment-1"
    stats = client.get(BASE + "/routing-stats").json()
    assert stats["received"] == 1 and stats["outcomes"][outcome] == 1


@pytest.mark.parametrize("state,outcome", [
    ("pending", "queued"), ("sending", "sending"), ("unknown", "unknown"),
    ("failed", "failed"), ("partial", "failed"), ("confirmed", "confirmed"),
    ("cancelled", "cancelled"),
])
def test_action_state_outcome_matrix(harness, state, outcome):
    owner = "owner-a"
    with harness.sessions() as db:
        _, event, _ = _admit(db, owner, source_event_id=f"action-{state}")
        prepared = _prepared(db, event)
        action = _effect(db, owner, event)
        _completed(db, event, prepared, effect=action, reason="assistant planned")
        action.state = state
        db.commit()
    items = _by_ref(_traces(harness.client, outcome=outcome))
    assert set(items) == {f"action-{state}"}


def test_stats_window_rule_names_and_deleted_history(harness, monkeypatch):
    sessions, owner = harness.sessions, "owner-a"
    now = utcnow().replace(microsecond=0)
    monkeypatch.setattr(trace_service, "utcnow", lambda: now)
    with sessions() as db:
        kept = _rule(db, owner, name="original")
        _, event = _admit_at(db, owner, now - timedelta(days=7), source_event_id="at-since")
        _completed(db, event, _prepared(db, event, kept), effect=_effect(db, owner, event))
        service.patch_rule(db, owner, kept.id, schemas.RulePatch(name="renamed"))
        _, event = _admit_at(db, owner, now - timedelta(days=1), source_event_id="after-rename")
        action = _effect(db, owner, event)
        _completed(db, event, _prepared(db, event, kept), effect=action)
        _finish(db, owner, action, "failed")
        doomed = _rule(db, owner, name="doomed")
        _, event = _admit_at(db, owner, now - timedelta(days=2), source_event_id="doomed-1")
        _completed(db, event, _prepared(db, event, doomed), effect=_effect(db, owner, event))
        service.delete_rule(db, owner, doomed.id)
        _, event = _admit_at(db, owner, now - timedelta(hours=1), source_event_id="assistant-1")
        _completed(db, event, _prepared(db, event), action="create_decision",
                   decision=_card(db, owner, event, issue=None), reason="needs a choice")
        _admit_at(db, owner, now - timedelta(days=8), source_event_id="outside")
        _admit_at(db, owner, now, source_event_id="at-until")
        _admit_at(db, owner, now - timedelta(minutes=30), source_event_id="pending-1")
        db.commit()
        kept_id, doomed_id = kept.id, doomed.id
    response = harness.client.get(BASE + "/routing-stats", params={"days": 7})
    assert response.status_code == 200
    stats = response.json()
    assert stats["days"] == 7 and stats["since"] < stats["until"]
    assert stats["received"] == 5 == stats["matched"] + stats["unmatched"] + stats["pendingRoute"]
    assert (stats["matched"], stats["unmatched"], stats["pendingRoute"]) == (3, 1, 1)
    assert stats["outcomes"]["failed"] == 1 and stats["outcomes"]["queued"] == 2
    assert stats["outcomes"]["review"] == 1 and stats["outcomes"]["pending"] == 1
    assert set(stats["outcomes"]) == set(trace_service.OUTCOMES)
    # Renames keep history: hits span both snapshots, name is the latest one.
    assert stats["rules"] == [
        {"ruleId": kept_id, "ruleName": "renamed", "hits": 2},
        {"ruleId": doomed_id, "ruleName": "doomed", "hits": 1},
    ]
    drill = _traces(harness.client, since=stats["since"], until=stats["until"])
    assert set(_by_ref(drill)) == {"at-since", "after-rename", "doomed-1", "assistant-1", "pending-1"}
    assert harness.client.get(BASE + "/routing-stats", params={"days": 8}).status_code == 422
    assert harness.client.get(BASE + "/routing-stats", params={"days": 30}).json()["days"] == 30


def test_detail_steps_audit_tail_and_verified_issue_url(harness):
    sessions, owner = harness.sessions, "owner-a"
    same_origin = "http://127.0.0.1:18002/workspace-a/issues/remote-1"
    with sessions() as db:
        db.add(SuperAssistantMulticaConfig(owner_id=owner, base_url="http://127.0.0.1:18002",
            workspace_id="workspace-a", token_encrypted="fixture", enabled=True))
        rule = _rule(db, owner)
        _, event, _ = _admit(db, owner, source_event_id="detail-1",
                          payload={"title": "detail", "body": "original"})
        prepared = _prepared(db, event, rule)
        action = _effect(db, owner, event)
        terminal = _completed(db, event, prepared, effect=action, issue_ref="TASK-1")
        _finish(db, owner, action, "confirmed", remote_object_id="TASK-1",
                remote_result={"issue": {"id": "remote-1", "identifier": "TASK-1",
                    "url": same_origin}})
        # Unbounded stage churn stays bounded in the audit tail.
        for index in range(trace_service.AUDIT_LIMIT + 20):
            store.append_event(db, owner_id=owner, receipt_id=event.receipt_id,
                type="routing.deferred", source_kind=event.source_kind,
                source_system=event.source_system, parent_event_id=event.id,
                action="pending", error="awaiting_business_router",
                data={"index": index, "untrustedOriginal": "secret-ish"})
        # A card whose only issue URL comes from another origin is never guessed.
        _, cross, _ = _admit(db, owner, source_event_id="cross-1")
        card = _card(db, owner, cross, issue="TASK-X")
        _completed(db, cross, _prepared(db, cross), action="create_decision",
                   decision=card, issue_ref="TASK-X", reason="choice")
        db.get(AutoLoopDecision, card.id).current_evidence = {
            "factsSummary": "s", "issue": {"id": "2", "identifier": "TASK-X",
            "url": "http://evil.example/issue/2"}}
        db.commit()
    client = harness.client
    detail = client.get(BASE + f"/event-traces/{terminal.id}").json()
    assert detail["untrustedOriginal"] == {"title": "detail", "body": "original"}
    assert detail["reason"] == "first_matching_rule" and detail["error"] is None
    assert detail["issueRef"] == "TASK-1" and detail["issueUrl"] == same_origin
    steps = detail["actionSteps"]
    assert len(steps) == 1 and set(steps[0]) == {"id", "kind", "state", "error", "remoteObjectId"}
    assert steps[0]["state"] == "confirmed" and steps[0]["kind"] == "issue.create"
    assert steps[0]["remoteObjectId"] == "TASK-1"
    audit = detail["auditEvents"]
    assert len(audit) == trace_service.AUDIT_LIMIT
    assert {item["type"] for item in audit} == {"routing.deferred"}
    assert all(set(item) == {"id", "type", "happenedAt", "action", "error"} for item in audit)
    with sessions() as db:
        cross_card = db.scalar(select(AutoLoopDecision).where(AutoLoopDecision.issue_key == "TASK-X"))
        result = trace_service.get_trace(db, owner, cross_card.origin_event_id)
    assert result["issueUrl"] is None and result["issueRef"] == "TASK-X"
    assert result["reason"] == "choice" and result["outcome"] == "review"
    # Connector drift refuses to synthesize a URL from an old issue payload.
    with sessions() as db:
        db.get(SuperAssistantMulticaConfig, owner).base_url = "http://127.0.0.1:19999"
        db.commit()
        drifted = trace_service.get_trace(db, owner, terminal.id)
    assert drifted["issueUrl"] is None
