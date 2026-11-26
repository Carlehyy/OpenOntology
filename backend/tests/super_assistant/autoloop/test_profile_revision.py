"""AutoLoop immutable source binding: freeze, resolve, replay and recheck.

Real profile tables and the real Multica profile service with real encryption
are used throughout; only the Multica network client is patched. Committed
steps and decision evidence freeze the ``{baseUrl, workspaceId,
profileRevisionId}`` source they were planned under; answers, execution,
reconciliation and watchdog rechecks bind that exact source across an A→B
profile switch, and idempotent replays stay successful even after the source
became unusable.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.model_configs.models import ModelConfig
from app.super_assistant import multica_client, multica_service
from app.super_assistant.autoloop import dispatching, planner, schemas, service, sourcing, store, watchdog, worker
from app.super_assistant.autoloop.models import (
    AutoLoopAction, AutoLoopActionStep, AutoLoopBinding, AutoLoopDecision,
    AutoLoopEvent, AutoLoopEvidence, AutoLoopIssueProgress, utcnow,
)
from app.super_assistant.autoloop.router import router
from app.super_assistant.models import (
    SuperAssistantMulticaConfig,
    SuperAssistantMulticaProfile,
    SuperAssistantMulticaProfileRevision,
)
from app.super_assistant.schemas import MulticaConfigUpdate, MulticaProfileCreate, MulticaProfileUpdate
from tests.conftest import TestSession

BASE_A, WS_A, TOKEN_A = "http://127.0.0.1:19001", "workspace-a", "token-a"
BASE_B, WS_B, TOKEN_B = "http://127.0.0.1:19002", "workspace-b", "token-b"


@pytest.fixture(params=["sqlite", "postgresql"])
def sessions(request):
    if request.param == "sqlite":
        yield TestSession
        return
    engine = request.getfixturevalue("pg_engine")
    for table in (SuperAssistantMulticaConfig, SuperAssistantMulticaProfile,
                  SuperAssistantMulticaProfileRevision, ModelConfig):
        table.__table__.create(engine, checkfirst=True)
    yield sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def owner(sessions):
    identity = str(uuid4())
    with sessions() as db:
        db.add(User(id=identity, username=identity, email=f"{identity}@test.invalid",
                    password_hash="fixture-no-login", is_active=True))
        db.commit()
    return identity


def _reference(base_url, workspace_id, revision_id):
    return {"baseUrl": base_url, "workspaceId": workspace_id, "profileRevisionId": revision_id}


def _save(sessions, owner_id, base_url, workspace_id, token):
    """Enroll a source through the real service (real tables, real encryption)."""
    with sessions() as db:
        config = multica_service.save_config(db, owner_id, MulticaConfigUpdate(
            base_url=base_url, workspace_id=workspace_id, token=token, enabled=True))
        return str(config.profile_revision_id)


def _card(sessions_or_db, owner_id, workspace_id, *, reference=None, issue="TASK-1", source="watchdog"):
    def create(db):
        evidence = {"factsSummary": "Observed", "commentIds": [], "timelineIds": [], "messageIds": []}
        if reference:
            evidence["sourceRef"] = reference
        row, _ = store.create_or_update_decision(db, owner_id=owner_id,
            workspace_id=workspace_id, issue_key=issue, source=source,
            need_key=str(uuid4()), title="Choose", question="Continue?",
            options=[{"id": "continue", "label": "Continue"}], evidence=evidence)
        db.commit()
        return SimpleNamespace(id=row.id, content_version=row.content_version)
    if hasattr(sessions_or_db, "__call__"):
        with sessions_or_db() as db:
            db.expire_all()
            return create(db)
    return create(sessions_or_db)


def _submit(sessions, owner_id, row, key="answer", resolver_owner=None):
    resolver_owner = resolver_owner or owner_id
    with sessions() as db:
        result, _, _ = store.submit_decision(db, owner_id=owner_id, decision_id=row.id,
            actor_id=owner_id, expected_content_version=row.content_version,
            idempotency_key=key, option_id="continue",
            source_resolver=lambda session, card: sourcing.resolve_decision_source(
                session, resolver_owner, card))
        db.commit()
        return result


def _patch_comment_network(monkeypatch, writes, *, fail=None):
    monkeypatch.setattr(multica_client, "get_issue",
        lambda base, secret, workspace, reference: {
            "id": "remote-1", "identifier": reference, "workspace_id": workspace})

    def create_comment(base, secret, workspace, reference, *, content=None, **kwargs):
        writes.append((base, secret, workspace, reference, content))
        if fail is not None:
            raise fail
        return multica_client.MulticaCommentResult("comment-1", {"id": "comment-1"}, "unknown", {})

    monkeypatch.setattr(multica_client, "create_comment", create_comment)


def test_routing_freezes_source_reference_into_steps_and_decisions(sessions, owner, monkeypatch):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    with sessions() as db:
        rule = service.create_rule(db, owner, schemas.RuleCreate.model_validate({
            "name": "route", "enabled": True, "position": 0, "match": {},
            "titleTemplate": "[rule] {{ title }}", "descriptionTemplate": "{{ body }}"}),
            confirm_all_match=True)
        db.commit()
        rule_id = rule["id"]
    with sessions() as db:
        _, event, _ = store.accept_receipt(db, source_kind="schedule", source_system="fixture",
            source_event_id=str(uuid4()), owner_id=owner, scheduled_task_id=str(uuid4()),
            payload={"title": "event", "body": "original"})
        work = db.scalar(select(dispatching.AutoLoopDispatch).where(
            dispatching.AutoLoopDispatch.event_id == event.id))
        event_id, work_id = event.id, work.id
        db.commit()
    dispatching.process_dispatch(work_id, session_factory=sessions)
    with sessions() as db:
        action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == event_id))
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
        assert step.request_payload["baseUrl"] == BASE_A
        assert step.request_payload["workspaceId"] == WS_A
        assert step.request_payload["profileRevisionId"] == revision_a

    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="create_decision", reason="choice required", title="Pick", question="Which?",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]))
    with sessions() as db:
        service.delete_rule(db, owner, rule_id)  # unmatched events must reach the planner
        db.commit()
    with sessions() as db:
        _, event, _ = store.accept_receipt(db, source_kind="schedule", source_system="fixture",
            source_event_id=str(uuid4()), owner_id=owner, scheduled_task_id=str(uuid4()),
            payload={"title": "event", "body": "original"})
        work = db.scalar(select(dispatching.AutoLoopDispatch).where(
            dispatching.AutoLoopDispatch.event_id == event.id))
        event_id, work_id = event.id, work.id
        db.commit()
    dispatching.process_dispatch(work_id, session_factory=sessions)
    with sessions() as db:
        decision = db.scalar(select(AutoLoopDecision).where(AutoLoopDecision.origin_event_id == event_id))
        assert decision is not None
        assert decision.evidence_baseline["sourceRef"] == _reference(BASE_A, WS_A, revision_a)
        assert decision.current_evidence["sourceRef"] == _reference(BASE_A, WS_A, revision_a)


def test_answer_binds_frozen_source_and_executes_against_it_after_switch(sessions, owner, monkeypatch):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    row = _card(sessions, owner, WS_A, reference=_reference(BASE_A, WS_A, revision_a))
    _submit(sessions, owner, row)
    with sessions() as db:
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.kind == "issue.comment"))
        assert step.request_payload["baseUrl"] == BASE_A
        assert step.request_payload["workspaceId"] == WS_A
        assert step.request_payload["profileRevisionId"] == revision_a
        identity = step.request_payload["decisionItemId"]
    writes = []
    _patch_comment_network(monkeypatch, writes)
    assert worker.scan_actions(session_factory=sessions) == 1
    assert writes[0][:3] == (BASE_A, TOKEN_A, WS_A)
    with sessions() as db:
        assert db.get(AutoLoopDecision, identity).status == "completed"


@pytest.mark.parametrize("revoked", [False, True], ids=["source-available", "source-revoked"])
def test_internal_answer_routes_on_its_frozen_source_after_switch(sessions, owner, monkeypatch, revoked):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    row = _card(sessions, owner, WS_A, issue=None, source="assistant",
                reference=_reference(BASE_A, WS_A, revision_a))
    _submit(sessions, owner, row)
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    assert worker.scan_actions(session_factory=sessions) == 1
    with sessions() as db:
        event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.type == "decision.answered"))
        event_id = event.id
        work_id = db.scalar(select(dispatching.AutoLoopDispatch.id).where(
            dispatching.AutoLoopDispatch.event_id == event_id))
        if revoked:
            db.execute(update(SuperAssistantMulticaProfileRevision).where(
                SuperAssistantMulticaProfileRevision.id == revision_a).values(revoked_at=utcnow()))
        db.commit()

    planned = []

    def plan(db, *, event, payload, workspace_id, trusted_issue):
        planned.append(workspace_id)
        return planner.Plan(action="create_task", reason="continue after answer", title="Follow-up")

    monkeypatch.setattr(planner, "plan_event", plan)
    dispatching.process_dispatch(work_id, session_factory=sessions)
    with sessions() as db:
        action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == event_id))
        if revoked:
            assert action is None and planned == []
            fallback = db.scalar(select(AutoLoopEvent).where(
                AutoLoopEvent.parent_event_id == event_id, AutoLoopEvent.type == "routing.fallback"))
            assert fallback.error == "decision_source_unavailable"
            card = db.get(AutoLoopDecision, fallback.decision_item_id)
            assert card.current_evidence["sourceRef"] == _reference(BASE_A, WS_A, revision_a)
            assert card.workspace_id == WS_A
        else:
            assert planned == [WS_A]
            step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
            assert step.request_payload["baseUrl"] == BASE_A
            assert step.request_payload["workspaceId"] == WS_A
            assert step.request_payload["profileRevisionId"] == revision_a


def test_reconcile_unknown_binds_frozen_source_after_switch(sessions, owner, monkeypatch):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    row = _card(sessions, owner, WS_A, reference=_reference(BASE_A, WS_A, revision_a))
    _submit(sessions, owner, row)
    writes = []
    _patch_comment_network(monkeypatch, writes, fail=multica_client.MulticaRequestError(
        "lost response", outcome="unknown"))
    worker.scan_actions(session_factory=sessions)
    # A→B switch happens after the unknown outcome; reconciliation must still
    # query revision A, not the newly active profile.
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    reads = []

    def list_comments(base, secret, workspace, reference):
        reads.append((base, secret, workspace))
        return multica_client.MulticaRead(
            [{"id": "comment-1", "request_id": request_id[0]}], True)

    with sessions() as db:
        request_id = [db.scalar(select(AutoLoopActionStep.remote_request_id).where(
            AutoLoopActionStep.kind == "issue.comment"))]
    monkeypatch.setattr(multica_client, "list_comments", list_comments)
    assert worker.scan_actions(session_factory=sessions) == 1
    assert reads == [(BASE_A, TOKEN_A, WS_A)]
    with sessions() as db:
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.kind == "issue.comment"))
        assert step.state == "confirmed" and step.remote_object_id == "comment-1"


def test_legacy_rows_resolve_their_workspace_source_not_the_active_profile(sessions, owner):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    card = _card(sessions, owner, WS_A)  # legacy card: no frozen reference
    with sessions() as db:
        row = db.get(AutoLoopDecision, card.id)
        source, revision_id = sourcing.resolve_decision_source(db, owner, row)
        assert (source.base_url, source.workspace_id, revision_id) == (BASE_A, WS_A, revision_a)
        # A legacy step that records its endpoint/workspace resolves that unique
        # source instead of blindly borrowing the active profile.
        config, secret = worker._connection(db, owner, {
            "workspaceId": WS_A, "baseUrl": BASE_A})
        assert (config.base_url, secret) == (BASE_A, TOKEN_A)
        # Unlinked cards never recorded a workspace and keep active semantics.
        unlinked, _ = store.create_or_update_decision(db, owner_id=owner,
            workspace_id=None, issue_key=None, source="assistant", need_key=str(uuid4()),
            title="Unlinked", question="Continue?",
            options=[{"id": "continue", "label": "Continue"}], evidence={"factsSummary": "Observed"})
        active, active_revision = sourcing.resolve_decision_source(db, owner, unlinked)
        assert (active.base_url, active.workspace_id) == (BASE_B, WS_B)
        assert active_revision is not None
        # A workspace whose source is gone fails closed instead of retargeting.
        orphan, _ = store.create_or_update_decision(db, owner_id=owner,
            workspace_id="workspace-unknown", issue_key="TASK-9", source="watchdog",
            need_key=str(uuid4()), title="Orphan", question="Continue?",
            options=[{"id": "continue", "label": "Continue"}], evidence={"factsSummary": "Observed"})
        with pytest.raises(multica_service.MulticaServiceError):
            sourcing.resolve_decision_source(db, owner, orphan)


def test_unusable_bound_source_fails_closed_without_sending(sessions, owner, monkeypatch):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    row = _card(sessions, owner, WS_A, reference=_reference(BASE_A, WS_A, revision_a))
    _submit(sessions, owner, row)
    with sessions() as db:
        db.execute(update(SuperAssistantMulticaProfileRevision).where(
            SuperAssistantMulticaProfileRevision.id == revision_a,
        ).values(revoked_at=utcnow()))
        db.commit()
    writes = []
    _patch_comment_network(monkeypatch, writes)
    for _ in range(3):
        worker.scan_actions(session_factory=sessions)
    assert writes == []
    with sessions() as db:
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.kind == "issue.comment"))
        assert step.state == "failed" and step.reconciliation["kind"] == "not_sent"
        assert db.get(AutoLoopDecision, row.id).status == "writeback_failed"


def test_token_rotation_preserves_old_revision_until_explicitly_revoked(sessions, owner):
    revision_one = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    revision_two = _save(sessions, owner, BASE_A, WS_A, "token-a-rotated")
    assert revision_one != revision_two
    with sessions() as db:
        retired = db.get(SuperAssistantMulticaProfileRevision, revision_one)
        assert retired.retired_at is not None and retired.revoked_at is None
        # Steps bound to the old revision keep executing with its credentials.
        config, secret = worker._connection(db, owner, {
            "workspaceId": WS_A, "baseUrl": BASE_A, "profileRevisionId": revision_one})
        assert (config.id, secret) == (revision_one, TOKEN_A)
        db.execute(update(SuperAssistantMulticaProfileRevision).where(
            SuperAssistantMulticaProfileRevision.id == revision_one,
        ).values(revoked_at=utcnow()))
        db.commit()
        with pytest.raises(multica_service.MulticaServiceError):
            worker._connection(db, owner, {
                "workspaceId": WS_A, "baseUrl": BASE_A, "profileRevisionId": revision_one})


def test_replay_returns_before_any_source_resolution(sessions, owner):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    row = _card(sessions, owner, WS_A, reference=_reference(BASE_A, WS_A, revision_a))
    resolutions = []

    def resolver(db, card):
        resolutions.append(card.id)
        return sourcing.resolve_decision_source(db, owner, card)

    with sessions() as db:
        store.submit_decision(db, owner_id=owner, decision_id=row.id, actor_id=owner,
            expected_content_version=row.content_version, idempotency_key="answer",
            option_id="continue", source_resolver=resolver)
        db.commit()
    assert resolutions == [row.id]

    def forbidden(db, card):
        raise AssertionError("replay must not resolve the source")

    with sessions() as db:
        _, request, replay = store.submit_decision(db, owner_id=owner, decision_id=row.id,
            actor_id=owner, expected_content_version=row.content_version,
            idempotency_key="answer", option_id="continue", source_resolver=forbidden)
        assert replay and request.idempotency_key == "answer"
        db.commit()


def _client(sessions, owner_id):
    api = FastAPI()
    api.include_router(router, prefix="/api/v2/super-assistant")

    def database():
        with sessions() as db:
            yield db

    api.dependency_overrides[get_db] = database
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=owner_id)
    return TestClient(api), "/api/v2/super-assistant/autoloop"


def test_decide_with_unusable_source_returns_409_and_keeps_card_open(sessions, owner):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    row = _card(sessions, owner, WS_A, reference=_reference(BASE_A, WS_A, revision_a))
    with sessions() as db:
        db.execute(update(SuperAssistantMulticaProfileRevision).where(
            SuperAssistantMulticaProfileRevision.id == revision_a,
        ).values(revoked_at=utcnow()))
        db.commit()
    client, base = _client(sessions, owner)
    response = client.post(base + f"/decision-items/{row.id}/decide", json={
        "expectedContentVersion": row.content_version, "idempotencyKey": "answer",
        "optionId": "continue"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "decision_source_unavailable"
    with sessions() as db:
        assert db.get(AutoLoopDecision, row.id).status == "open"
        assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0


def test_decide_replay_still_succeeds_after_source_revoked(sessions, owner):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    row = _card(sessions, owner, WS_A, reference=_reference(BASE_A, WS_A, revision_a))
    client, base = _client(sessions, owner)
    body = {"expectedContentVersion": row.content_version, "idempotencyKey": "answer",
            "optionId": "continue"}
    accepted = client.post(base + f"/decision-items/{row.id}/decide", json=body)
    assert accepted.status_code == 202 and accepted.json()["status"] == "pending_dispatch"
    with sessions() as db:
        db.execute(update(SuperAssistantMulticaProfileRevision).where(
            SuperAssistantMulticaProfileRevision.id == revision_a,
        ).values(revoked_at=utcnow()))
        db.commit()
    replay = client.post(base + f"/decision-items/{row.id}/decide", json=body)
    assert replay.status_code == 200
    assert replay.json()["alreadyProcessed"] is True
    assert replay.json()["contentVersion"] == accepted.json()["contentVersion"]


@pytest.mark.parametrize("discovery_unavailable", [False, True], ids=["available", "unavailable"])
def test_watchdog_freezes_source_and_withdraws_previous_source_card_after_switch(
        sessions, owner, monkeypatch, discovery_unavailable):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    issue = {"id": "remote-1", "identifier": "TASK-1", "status": "blocked", "workspace_id": WS_A}
    reads = []

    def record(name, function):
        def wrapped(base, secret, *args):
            reads.append((name, base, secret))
            return function(base, secret, *args)
        return wrapped

    def list_issues(base, secret, workspace):
        if workspace == WS_A:
            return multica_client.MulticaRead([issue], True)
        if discovery_unavailable:
            raise multica_client.MulticaRequestError("fixture discovery failure", outcome="failed")
        return multica_client.MulticaRead([], True)

    monkeypatch.setattr(multica_client, "list_all_issues", list_issues)
    monkeypatch.setattr(multica_client, "list_timeline", record("list_timeline",
        lambda base, secret, workspace, reference: multica_client.MulticaRead([], True)))
    comments = {"items": []}
    monkeypatch.setattr(multica_client, "list_comments", record("list_comments",
        lambda base, secret, workspace, reference: multica_client.MulticaRead(comments["items"], True)))
    monkeypatch.setattr(multica_client, "get_active_task", record("get_active_task",
        lambda base, secret, workspace, reference: {"tasks": []}))
    monkeypatch.setattr(multica_client, "get_issue", record("get_issue",
        lambda base, secret, workspace, reference: {"id": "remote-1", "identifier": reference,
                                                    "status": "blocked", "workspace_id": workspace}))
    monkeypatch.setattr(multica_client, "list_task_messages", record("list_task_messages",
        lambda base, secret, workspace, task: multica_client.MulticaRead([], True)))
    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="create_decision", reason="choice required", title="Recover", question="Retry?",
        options=[{"id": "retry", "label": "Retry"}, {"id": "wait", "label": "Wait"}]))

    # Discovery under A creates the card with A frozen into its evidence.
    assert watchdog.scan_owner(owner, session_factory=sessions) == 1
    with sessions() as db:
        row = db.scalar(select(AutoLoopDecision).where(
            AutoLoopDecision.owner_id == owner, AutoLoopDecision.issue_key == "TASK-1"))
        assert row.status == "open" and row.source == "watchdog"
        assert row.evidence_baseline["sourceRef"] == _reference(BASE_A, WS_A, revision_a)
        assert row.current_evidence["sourceRef"] == _reference(BASE_A, WS_A, revision_a)
        identity, created = row.id, row.created_at

    # A→B switch: discovery moves to B while the A card must be rechecked via
    # exactly revision A (same revision, same endpoint, same credentials).
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    reads.clear()
    comments["items"] = [{"id": "web-comment", "author_type": "member", "author_id": "member-1",
        "created_at": (utcnow() + timedelta(seconds=1)).isoformat(), "content": "handled"}]
    assert watchdog.scan_owner(owner, session_factory=sessions) == 1
    assert all(base == BASE_A and secret == TOKEN_A for _, base, secret in reads)
    assert "get_issue" in {name for name, _, _ in reads}
    with sessions() as db:
        row = db.get(AutoLoopDecision, identity)
        assert row.status == "withdrawn" and row.close_reason == "检测到外部跟进"
    # The withdrawal stays consumed; later scans do not resurrect the need.
    assert watchdog.scan_owner(owner, session_factory=sessions) == 0
    assert created is not None


@pytest.mark.parametrize("revocation", ["none", "before_scan", "during_read"])
def test_watchdog_does_not_rebind_old_card_after_pat_rotation(sessions, owner, monkeypatch, revocation):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    issue = {"id": "remote-1", "identifier": "TASK-1", "status": "blocked", "workspace_id": WS_A}
    discovery, historical, streams = [], [], []

    def revoke_original():
        with sessions() as db:
            db.execute(update(SuperAssistantMulticaProfileRevision).where(
                SuperAssistantMulticaProfileRevision.id == revision_a,
            ).values(revoked_at=utcnow()))
            db.commit()

    def list_issues(base, secret, workspace):
        discovery.append((base, secret, workspace))
        same_issue = {**issue, "description": "Seen with the rotated PAT"} if secret == TOKEN_B else issue
        return multica_client.MulticaRead([same_issue], True)

    def get_issue(base, secret, workspace, reference):
        historical.append((base, secret, workspace, reference))
        return issue

    def list_comments(base, secret, workspace, reference):
        streams.append((base, secret, workspace, reference))
        # The new PAT can see a human follow-up unknown to the original source.
        # It cannot withdraw the old card even though the workspace and issue match.
        items = [{"id": "new-source-comment", "author_type": "member", "author_id": "member-1",
                  "created_at": (utcnow() + timedelta(seconds=1)).isoformat()}] if secret == TOKEN_B else []
        return multica_client.MulticaRead(items, True)

    def active_task(base, secret, workspace, reference):
        if revocation == "during_read" and historical and secret == TOKEN_A:
            revoke_original()
        return {"tasks": []}

    monkeypatch.setattr(multica_client, "list_all_issues", list_issues)
    monkeypatch.setattr(multica_client, "get_issue", get_issue)
    monkeypatch.setattr(multica_client, "list_timeline", lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "list_comments", list_comments)
    monkeypatch.setattr(multica_client, "get_active_task", active_task)
    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="create_decision", reason="choice required", title="Recover", question="Retry?",
        options=[{"id": "retry", "label": "Retry"}, {"id": "wait", "label": "Wait"}]))

    assert watchdog.scan_owner(owner, session_factory=sessions) == 1
    with sessions() as db:
        row = db.scalar(select(AutoLoopDecision).where(AutoLoopDecision.owner_id == owner))
        identity, version, evidence = row.id, row.content_version, row.current_evidence
        assert evidence["sourceRef"] == _reference(BASE_A, WS_A, revision_a)

    assert _save(sessions, owner, BASE_A, WS_A, TOKEN_B) != revision_a
    if revocation == "before_scan":
        revoke_original()
    streams.clear()
    assert watchdog.scan_owner(owner, session_factory=sessions) == (1 if revocation == "none" else 0)
    assert discovery == [(BASE_A, TOKEN_A, WS_A), (BASE_A, TOKEN_B, WS_A)]
    original_read = [] if revocation == "before_scan" else [(BASE_A, TOKEN_A, WS_A, "TASK-1")]
    assert historical == streams == original_read
    with sessions() as db:
        row = db.get(AutoLoopDecision, identity)
        assert (row.status, row.content_version, row.current_evidence) == ("open", version, evidence)
        assert db.scalar(select(func.count()).select_from(AutoLoopEvidence)) == 0

    client, base = _client(sessions, owner)
    response = client.post(base + f"/decision-items/{identity}/decide", json={
        "expectedContentVersion": version, "idempotencyKey": "answer", "optionId": "retry"})
    if revocation == "none":
        assert response.status_code == 202
        with sessions() as db:
            step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.kind == "issue.comment"))
            source, secret = worker._connection(db, owner, step.request_payload)
            assert (source.id, source.workspace_id, secret) == (revision_a, WS_A, TOKEN_A)
    else:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "decision_source_unavailable"
        with sessions() as db:
            assert db.get(AutoLoopDecision, identity).status == "open"
            assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0


def test_reconcile_checks_source_again_when_discovery_missed_an_open_card(sessions, owner):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    row = _card(sessions, owner, WS_A, reference=_reference(BASE_A, WS_A, revision_a))
    revision_b = _save(sessions, owner, BASE_A, WS_A, TOKEN_B)
    # Simulate an issue already selected for discovery before the old-source
    # card became visible. Reconciliation under the issue lock is the last gate.
    issue = {"id": "remote-1", "identifier": "TASK-1", "status": "blocked", "workspace_id": WS_A}
    followup = {"id": "new-source-comment", "author_type": "member", "author_id": "member-1",
                "created_at": (utcnow() + timedelta(seconds=1)).isoformat()}
    with sessions() as db:
        watchdog.reconcile_issue(db, owner_id=owner, workspace_id=WS_A, issue=issue,
            comments=[followup], timeline=[], messages=[], tasks=set(), complete=True,
            source_ref=_reference(BASE_A, WS_A, revision_b))
        db.commit()
    with sessions() as db:
        saved = db.get(AutoLoopDecision, row.id)
        assert (saved.status, saved.content_version) == ("open", row.content_version)
        assert saved.current_evidence["sourceRef"] == _reference(BASE_A, WS_A, revision_a)
        assert db.scalar(select(func.count()).select_from(AutoLoopEvidence)) == 0


def test_scan_does_not_withdraw_on_incomplete_streams(sessions, owner, monkeypatch):
    _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    issue = {"id": "remote-1", "identifier": "TASK-1", "status": "blocked", "workspace_id": WS_A}
    monkeypatch.setattr(multica_client, "list_all_issues",
        lambda *args: multica_client.MulticaRead([issue], True))
    complete = {"timeline": True}
    monkeypatch.setattr(multica_client, "list_timeline",
        lambda *args: multica_client.MulticaRead([], complete["timeline"]))
    monkeypatch.setattr(multica_client, "list_comments",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "get_active_task", lambda *args: {"tasks": []})
    monkeypatch.setattr(multica_client, "list_task_messages",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="create_decision", reason="choice required", title="Recover", question="Retry?",
        options=[{"id": "retry", "label": "Retry"}, {"id": "wait", "label": "Wait"}]))
    assert watchdog.scan_owner(owner, session_factory=sessions) == 1
    with sessions() as db:
        identity = db.scalar(select(AutoLoopDecision.id).where(
            AutoLoopDecision.owner_id == owner, AutoLoopDecision.issue_key == "TASK-1"))
    # A skipped (truncated) timeline stream must not claim completeness, so a
    # qualifying external follow-up cannot withdraw from an incomplete read.
    complete["timeline"] = False
    monkeypatch.setattr(multica_client, "list_comments",
        lambda *args: multica_client.MulticaRead([{"id": "web-comment", "author_type": "member",
            "author_id": "member-1", "created_at": (utcnow() + timedelta(seconds=1)).isoformat()}], True))
    watchdog.scan_owner(owner, session_factory=sessions)
    with sessions() as db:
        assert db.get(AutoLoopDecision, identity).status == "open"
    complete["timeline"] = True
    watchdog.scan_owner(owner, session_factory=sessions)
    with sessions() as db:
        assert db.get(AutoLoopDecision, identity).status == "withdrawn"


def test_scan_isolates_foreign_workspace_evidence_before_persisting(sessions, owner, monkeypatch):
    _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    foreign = {"id": "remote-x", "identifier": "TASK-X", "status": "blocked",
               "workspace_id": "workspace-foreign"}

    def forbidden(*args, **kwargs):
        raise AssertionError("foreign-endpoint evidence must not be read")

    monkeypatch.setattr(multica_client, "list_all_issues",
        lambda *args: multica_client.MulticaRead([foreign], True))
    for name in ("list_timeline", "list_comments", "get_active_task", "list_task_messages"):
        monkeypatch.setattr(multica_client, name, forbidden)
    assert watchdog.scan_owner(owner, session_factory=sessions) == 0
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(AutoLoopEvidence)) == 0
        assert db.scalar(select(func.count()).select_from(AutoLoopIssueProgress)) == 0
        assert db.scalar(select(func.count()).select_from(AutoLoopDecision)) == 0


def test_historical_recheck_rotation_is_bounded_and_fair(sessions, owner, monkeypatch):
    total, limit = 7, watchdog._HISTORICAL_SOURCES_PER_SCAN
    assert total > limit
    revisions = {}
    for index in range(total):
        workspace = f"workspace-{index}"
        with sessions() as db:
            multica_service.create_profile(db, owner, MulticaProfileCreate(
                name=f"source-{index}", base_url=BASE_A, workspace_id=workspace,
                token=f"token-{index}", enabled=True))
            revisions[workspace] = db.scalar(select(SuperAssistantMulticaProfileRevision.id).where(
                SuperAssistantMulticaProfileRevision.workspace_id == workspace))
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)  # current source B, discovery finds nothing
    for index in range(total):
        workspace = f"workspace-{index}"
        _card(sessions, owner, workspace, issue=f"TASK-{index}",
              reference=_reference(BASE_A, workspace, revisions[workspace]))

    scanned = []
    monkeypatch.setattr(multica_client, "list_all_issues",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "get_issue",
        lambda base, secret, workspace, reference: scanned.append(workspace) or {
            "id": reference, "identifier": reference, "status": "blocked", "workspace_id": workspace})
    monkeypatch.setattr(multica_client, "list_timeline",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "list_comments",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "get_active_task", lambda *args: {"tasks": []})
    monkeypatch.setattr(multica_client, "list_task_messages",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="ignore", reason="waiting for external conditions"))

    assert watchdog.scan_owner(owner, session_factory=sessions) == limit
    first = set(scanned)
    assert len(first) == limit
    # The next bounded scan reaches the sources the first one skipped before
    # re-scanning already-visited ones: rotation cannot starve a source.
    assert watchdog.scan_owner(owner, session_factory=sessions) == limit
    second = set(scanned)
    assert len(second - first) == total - limit
    assert first | second == {f"workspace-{index}" for index in range(total)}


@pytest.mark.parametrize("legacy", [
    {}, {"workspaceId": WS_A}, {"baseUrl": BASE_A}],
    ids=["unrecorded", "workspace-only", "base-only"])
def test_unprovable_legacy_step_fails_closed_instead_of_guessing_active(
        sessions, owner, monkeypatch, legacy):
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)  # the active profile is now B
    row = _card(sessions, owner, WS_A, reference=_reference(BASE_A, WS_A, revision_a))
    _submit(sessions, owner, row)
    with sessions() as db:
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.kind == "issue.comment"))
        identity = step.id
        # A pre-freeze payload that kept no provable source identity.
        step.request_payload = {**{key: value for key, value in step.request_payload.items()
            if key not in ("profileRevisionId", "baseUrl", "workspaceId")}, **legacy}
        db.commit()
    writes = []
    _patch_comment_network(monkeypatch, writes)
    for _ in range(3):
        worker.scan_actions(session_factory=sessions)
    # Never sent anywhere: the active profile B is not borrowed for a step
    # that cannot prove the source it was planned under.
    assert writes == []
    with sessions() as db:
        step = db.get(AutoLoopActionStep, identity)
        assert step.state == "failed" and step.reconciliation["kind"] == "not_sent"
        assert db.get(AutoLoopDecision, row.id).status == "writeback_failed"


def test_pair_only_reference_historical_recheck_uses_unique_source_not_active(
        sessions, owner, monkeypatch):
    _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    # Legacy card whose evidence froze only the endpoint/workspace pair.
    row = _card(sessions, owner, WS_A, reference={"baseUrl": BASE_A, "workspaceId": WS_A})
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    reads, transcripts = [], []

    def record(name, function):
        def wrapped(base, secret, *args):
            reads.append((name, base, secret))
            return function(base, secret, *args)
        return wrapped

    monkeypatch.setattr(multica_client, "list_all_issues",
        lambda base, secret, workspace: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "get_issue", record("get_issue",
        lambda base, secret, workspace, reference: {"id": "remote-1", "identifier": reference,
                                                    "status": "blocked", "workspace_id": workspace}))
    monkeypatch.setattr(multica_client, "list_timeline", record("list_timeline",
        lambda base, secret, workspace, reference: multica_client.MulticaRead([], True)))
    monkeypatch.setattr(multica_client, "list_comments", record("list_comments",
        lambda base, secret, workspace, reference: multica_client.MulticaRead([{
            "id": "web-comment", "author_type": "member", "author_id": "member-1",
            "created_at": (utcnow() + timedelta(seconds=1)).isoformat()}], True)))
    monkeypatch.setattr(multica_client, "get_active_task", record("get_active_task",
        lambda base, secret, workspace, reference: {"tasks": [{"id": "run-1", "status": "running"}]}))

    def messages(base, secret, workspace, task):
        transcripts.append(task)
        return multica_client.MulticaRead([], True)

    monkeypatch.setattr(multica_client, "list_task_messages", record("list_task_messages", messages))
    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="create_decision", reason="choice required", title="Recover", question="Retry?",
        options=[{"id": "retry", "label": "Retry"}, {"id": "wait", "label": "Wait"}]))

    assert watchdog.scan_owner(owner, session_factory=sessions) == 1
    assert reads and all(base == BASE_A and secret == TOKEN_A for _, base, secret in reads)
    # The official {tasks: [...]} identities drive the transcript reads.
    assert transcripts == ["run-1"]
    with sessions() as db:
        assert db.get(AutoLoopDecision, row.id).status == "withdrawn"
        assert row.content_version is not None
    assert watchdog.scan_owner(owner, session_factory=sessions) == 0


def test_unresolvable_source_cards_stay_manual_without_starving_rechecks(
        sessions, owner, monkeypatch):
    live = watchdog._HISTORICAL_SOURCES_PER_SCAN  # live groups exactly fill the budget
    revisions = {}
    for index in range(live):
        workspace = f"live-{index}"
        with sessions() as db:
            multica_service.create_profile(db, owner, MulticaProfileCreate(
                name=f"live-{index}", base_url=BASE_A, workspace_id=workspace,
                token=f"token-{index}", enabled=True))
            revisions[workspace] = db.scalar(select(SuperAssistantMulticaProfileRevision.id).where(
                SuperAssistantMulticaProfileRevision.workspace_id == workspace))
    with sessions() as db:
        multica_service.create_profile(db, owner, MulticaProfileCreate(
            name="dead", base_url=BASE_A, workspace_id="workspace-dead",
            token="token-dead", enabled=True))
        dead_revision = db.scalar(select(SuperAssistantMulticaProfileRevision.id).where(
            SuperAssistantMulticaProfileRevision.workspace_id == "workspace-dead"))
        db.commit()
    dead = _card(sessions, owner, "workspace-dead", issue="TASK-DEAD",
                 reference=_reference(BASE_A, "workspace-dead", dead_revision))
    for index in range(live):
        workspace = f"live-{index}"
        _card(sessions, owner, workspace, issue=f"TASK-{index}",
              reference=_reference(BASE_A, workspace, revisions[workspace]))
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)  # current source; discovery finds nothing

    scanned = []
    monkeypatch.setattr(multica_client, "list_all_issues",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "get_issue",
        lambda base, secret, workspace, reference: scanned.append(workspace) or {
            "id": reference, "identifier": reference, "status": "blocked", "workspace_id": workspace})
    monkeypatch.setattr(multica_client, "list_timeline",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "list_comments",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "get_active_task",
        lambda *args: {"tasks": []})
    monkeypatch.setattr(multica_client, "list_task_messages",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="ignore", reason="waiting for external conditions"))
    with sessions() as db:
        db.execute(update(SuperAssistantMulticaProfileRevision).where(
            SuperAssistantMulticaProfileRevision.id == dead_revision,
        ).values(revoked_at=utcnow()))
        db.commit()

    # The revoked source never occupies one of the bounded historical slots,
    # so every live source is rechecked in the same scan.
    assert watchdog.scan_owner(owner, session_factory=sessions) == live
    assert set(scanned) == {f"live-{index}" for index in range(live)}
    for _ in range(2):
        watchdog.scan_owner(owner, session_factory=sessions)
    assert "workspace-dead" not in scanned
    with sessions() as db:
        # Its card stays open as explicit manual work; reconciliation of an
        # unusable frozen source is never silently claimed.
        assert db.get(AutoLoopDecision, dead.id).status == "open"


def test_scan_owner_commits_first_use_platform_default_before_cross_session_recheck(
        sessions, owner, monkeypatch):
    from app.shared.config import settings

    base, workspace = "http://127.0.0.1:19003", "workspace-default"
    monkeypatch.setattr(settings, "multica_default_base_url", base)
    monkeypatch.setattr(settings, "multica_default_web_url", base)
    monkeypatch.setattr(settings, "multica_default_workspace_id", workspace)
    monkeypatch.setattr(settings, "multica_default_workspace_name", "平台默认")
    monkeypatch.setattr(settings, "multica_default_pat", "token-default")
    issue = {"id": "remote-d", "identifier": "TASK-D", "status": "blocked", "workspace_id": workspace}
    monkeypatch.setattr(multica_client, "list_all_issues",
        lambda *args: multica_client.MulticaRead([issue], True))
    monkeypatch.setattr(multica_client, "list_timeline",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "list_comments",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "get_active_task",
        lambda *args: {"tasks": []})
    monkeypatch.setattr(multica_client, "list_task_messages",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="create_decision", reason="choice required", title="Recover", question="Retry?",
        options=[{"id": "retry", "label": "Retry"}, {"id": "wait", "label": "Wait"}]))
    assert watchdog.scan_owner(owner, session_factory=sessions) == 1
    with sessions() as db:
        # The seeded default profile/revision survived the scan's own session,
        # so the separate recheck session could resolve the frozen revision.
        revision = db.scalar(select(SuperAssistantMulticaProfileRevision.id).join(
            SuperAssistantMulticaProfile,
            SuperAssistantMulticaProfile.id == SuperAssistantMulticaProfileRevision.profile_id,
        ).where(SuperAssistantMulticaProfile.owner_id == owner))
        assert revision is not None
        assert db.scalar(select(AutoLoopDecision.issue_key).where(
            AutoLoopDecision.owner_id == owner)) == "TASK-D"
    # A later scan reuses the committed seeding instead of re-materializing it.
    assert watchdog.scan_owner(owner, session_factory=sessions) == 1
    with sessions() as db:
        assert db.scalar(select(SuperAssistantMulticaProfileRevision.id).join(
            SuperAssistantMulticaProfile,
            SuperAssistantMulticaProfile.id == SuperAssistantMulticaProfileRevision.profile_id,
        ).where(SuperAssistantMulticaProfile.owner_id == owner)) == revision


def test_scan_watchdog_enumerates_first_use_default_owners_only_when_complete(
        sessions, owner, monkeypatch):
    from app.shared.config import settings

    base, workspace = "http://127.0.0.1:19003", "workspace-default"

    def configure(complete):
        monkeypatch.setattr(settings, "multica_default_base_url", base if complete else "")
        monkeypatch.setattr(settings, "multica_default_web_url", base if complete else "")
        monkeypatch.setattr(settings, "multica_default_workspace_id", workspace if complete else "")
        monkeypatch.setattr(settings, "multica_default_workspace_name", "平台默认" if complete else "")
        monkeypatch.setattr(settings, "multica_default_pat", "token-default" if complete else "")

    configured = str(uuid4())
    with sessions() as db:
        db.add(User(id=configured, username=configured, email=f"{configured}@test.invalid",
                    password_hash="fixture-no-login", is_active=True))
        db.commit()
    _save(sessions, configured, BASE_A, WS_A, TOKEN_A)
    # The first-use owner has no config/profile/decision rows at all.
    configure(False)
    with sessions() as db:
        assert watchdog._scan_owners(db) == {configured}
    configure(True)
    with sessions() as db:
        assert watchdog._scan_owners(db) == {owner, configured}
    issue = {"id": "remote-d", "identifier": "TASK-D", "status": "blocked", "workspace_id": workspace}
    monkeypatch.setattr(multica_client, "list_all_issues",
        lambda base_url, secret, ws: multica_client.MulticaRead(
            [issue] if ws == workspace else [], True))
    monkeypatch.setattr(multica_client, "list_timeline",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "list_comments",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(multica_client, "get_active_task",
        lambda *args: {"tasks": []})
    monkeypatch.setattr(multica_client, "list_task_messages",
        lambda *args: multica_client.MulticaRead([], True))
    monkeypatch.setattr(planner, "plan_event", lambda *args, **kwargs: planner.Plan(
        action="create_decision", reason="choice required", title="Recover", question="Retry?",
        options=[{"id": "retry", "label": "Retry"}, {"id": "wait", "label": "Wait"}]))
    # The automatic scan reaches the first-use owner end to end: seeding is
    # committed, the frozen revision resolves cross-session, a card appears.
    assert watchdog.scan_watchdog(session_factory=sessions) == 1
    with sessions() as db:
        assert db.scalar(select(AutoLoopDecision.issue_key).where(
            AutoLoopDecision.owner_id == owner)) == "TASK-D"


def test_scan_owners_include_default_and_unresolved_even_when_disabled(sessions):
    identities = {name: str(uuid4()) for name in ("enabled", "default", "unresolved", "plain", "disabled")}
    with sessions() as db:
        for identity in identities.values():
            db.add(User(id=identity, username=identity, email=f"{identity}@test.invalid",
                        password_hash="fixture-no-login", is_active=True))
        db.commit()
    _save(sessions, identities["enabled"], BASE_A, WS_A, TOKEN_A)
    _save(sessions, identities["disabled"], BASE_A, "workspace-disabled", TOKEN_A)
    with sessions() as db:
        db.get(SuperAssistantMulticaConfig, identities["disabled"]).enabled = False
        db.add(SuperAssistantMulticaProfile(owner_id=identities["default"], name="平台默认 multica",
            kind=multica_service.PROFILE_KIND_PLATFORM_DEFAULT,
            builtin_key=multica_service.PLATFORM_DEFAULT_BUILTIN_KEY, is_active=False))
        db.commit()
    _card(sessions, identities["unresolved"], WS_A, issue="TASK-U")
    _card(sessions, identities["disabled"], "workspace-disabled", issue="TASK-D")
    with sessions() as db:
        owners = watchdog._scan_owners(db)
    assert identities["enabled"] in owners
    assert identities["default"] in owners
    assert identities["unresolved"] in owners
    assert identities["disabled"] in owners  # unresolved history with disabled config
    assert identities["plain"] not in owners


# ---------------------------------------------------------------------------
# Bound threads across an A→B switch, and explicit deployment-default sync at
# the dispatch and decide entry points (profile reads are pure).
# ---------------------------------------------------------------------------

def _configure_default(monkeypatch, base, workspace, name="平台默认"):
    from app.shared.config import settings

    monkeypatch.setattr(settings, "multica_default_base_url", base)
    monkeypatch.setattr(settings, "multica_default_web_url", base)
    monkeypatch.setattr(settings, "multica_default_workspace_id", workspace)
    monkeypatch.setattr(settings, "multica_default_workspace_name", name)
    monkeypatch.setattr(settings, "multica_default_pat", "token-default")


def _plain_rule(sessions, owner_id):
    with sessions() as db:
        service.create_rule(db, owner_id, schemas.RuleCreate.model_validate({
            "name": "route", "enabled": True, "position": 0, "match": {},
            "titleTemplate": "[rule] {{ title }}", "descriptionTemplate": "{{ body }}"}),
            confirm_all_match=True)
        db.commit()


def _admit_schedule(sessions, owner_id, task_id, **extra_payload):
    with sessions() as db:
        _, event, _ = store.accept_receipt(db, source_kind="schedule", source_system="fixture",
            source_event_id=str(uuid4()), owner_id=owner_id, scheduled_task_id=task_id,
            payload={"title": "event", "body": "original", **extra_payload})
        work = db.scalar(select(dispatching.AutoLoopDispatch).where(
            dispatching.AutoLoopDispatch.event_id == event.id))
        event_id, work_id = event.id, work.id
        db.commit()
    return event_id, work_id


def _bind_thread_under_a(sessions, owner, monkeypatch, *, reads=None):
    """Run one scheduled event through routing+worker so the thread binds to A."""
    revision_a = _save(sessions, owner, BASE_A, WS_A, TOKEN_A)
    monkeypatch.setattr(multica_client, "create_issue",
        lambda base, secret, workspace, **kwargs: {"id": "remote-1", "identifier": "TASK-1",
                                                   "workspace_id": workspace})

    def get_issue(base, secret, workspace, reference):
        if reads is not None:
            reads.append((base, secret, workspace, reference))
        return {"id": "remote-1", "identifier": reference, "workspace_id": workspace}

    monkeypatch.setattr(multica_client, "get_issue", get_issue)
    _plain_rule(sessions, owner)
    task = str(uuid4())
    first_event, first_work = _admit_schedule(sessions, owner, task)
    dispatching.process_dispatch(first_work, session_factory=sessions)
    assert worker.scan_actions(session_factory=sessions) == 1
    with sessions() as db:
        binding = db.scalar(select(AutoLoopBinding))
        assert binding.workspace_id == WS_A
        assert binding.provenance["kind"] == "created_issue"
        assert binding.provenance["baseUrl"] == BASE_A
        assert binding.provenance["profileRevisionId"] == revision_a
    return revision_a, task, first_event


@pytest.mark.parametrize("change", ["switch", "disable", "delete"])
def test_bound_thread_keeps_its_frozen_source(sessions, owner, monkeypatch, change):
    reads = []
    revision_a, task, _ = _bind_thread_under_a(sessions, owner, monkeypatch, reads=reads)
    if change == "switch":
        _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    else:
        with sessions() as db:
            profile_id = multica_service.active_config(db, owner).profile_id
            if change == "disable":
                multica_service.update_profile(db, owner, profile_id, MulticaProfileUpdate(enabled=False))
            else:
                multica_service.delete_profile(db, owner, profile_id)
            assert multica_service.active_config(db, owner) is None

    second_event, second_work = _admit_schedule(sessions, owner, task)
    dispatching.process_dispatch(second_work, session_factory=sessions)
    with sessions() as db:
        action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == second_event))
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
        assert step.kind == "issue.comment"
        assert step.request_payload["baseUrl"] == BASE_A
        assert step.request_payload["workspaceId"] == WS_A
        assert step.request_payload["profileRevisionId"] == revision_a
        assert step.request_payload["issueRef"] == "TASK-1"
        # No fork: the established thread was not re-bound into workspace B.
        assert db.scalar(select(func.count()).select_from(AutoLoopBinding)) == 1
    assert reads == [(BASE_A, TOKEN_A, WS_A, "TASK-1")]


def test_new_thread_after_switch_targets_the_active_profile(sessions, owner, monkeypatch):
    revision_a, _, _ = _bind_thread_under_a(sessions, owner, monkeypatch)
    revision_b = _save(sessions, owner, BASE_B, WS_B, TOKEN_B)

    created = []
    monkeypatch.setattr(multica_client, "create_issue",
        lambda base, secret, workspace, **kwargs: created.append((base, secret, workspace)) or {
            "id": "remote-2", "identifier": "TASK-2", "workspace_id": workspace})
    # An external notification or schedule can report these fields as raw data;
    # only a durable internal answer can use them as connector authority.
    other_event, other_work = _admit_schedule(sessions, owner, str(uuid4()),
        **_reference(BASE_A, WS_A, revision_a), type="decision.answered", source_kind="decision")
    dispatching.process_dispatch(other_work, session_factory=sessions)
    assert worker.scan_actions(session_factory=sessions) == 1
    with sessions() as db:
        action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == other_event))
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
        assert step.kind == "issue.create"
        assert step.request_payload["baseUrl"] == BASE_B
        assert step.request_payload["workspaceId"] == WS_B
        assert step.request_payload["profileRevisionId"] == revision_b
    assert created == [(BASE_B, TOKEN_B, WS_B)]


def test_stale_bound_source_routes_to_manual_decision_not_the_new_profile(
        sessions, owner, monkeypatch):
    revision_a, task, _ = _bind_thread_under_a(sessions, owner, monkeypatch)
    _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    with sessions() as db:
        db.execute(update(SuperAssistantMulticaProfileRevision).where(
            SuperAssistantMulticaProfileRevision.id == revision_a,
        ).values(revoked_at=utcnow()))
        db.commit()

    event, work = _admit_schedule(sessions, owner, task)
    dispatching.process_dispatch(work, session_factory=sessions)
    with sessions() as db:
        fallback = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.parent_event_id == event,
            AutoLoopEvent.type == "routing.fallback"))
        assert fallback is not None and fallback.error == "binding_source_unavailable"
        # Bounded manual path: no new task is created in the new profile and no
        # same-name guess is attempted against workspace B.
        assert db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == event)) is None
        decision = db.scalar(select(AutoLoopDecision).where(AutoLoopDecision.origin_event_id == event))
        assert decision is not None and decision.source == "kernel_fallback"


def test_forked_thread_bindings_stay_manual(sessions, owner, monkeypatch):
    revision_a, task, first_event = _bind_thread_under_a(sessions, owner, monkeypatch)
    revision_b = _save(sessions, owner, BASE_B, WS_B, TOKEN_B)
    with sessions() as db:
        # Historical fork: the same thread also bound into workspace B.
        db.add(AutoLoopBinding(owner_id=owner, workspace_id=WS_B,
            source_scope="schedule:" + task, source_thread=task, issue_key="TASK-B",
            provenance={"kind": "created_issue", "baseUrl": BASE_B,
                        "profileRevisionId": revision_b},
            established_event_id=first_event))
        db.commit()

    event, work = _admit_schedule(sessions, owner, task)
    dispatching.process_dispatch(work, session_factory=sessions)
    with sessions() as db:
        fallback = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.parent_event_id == event,
            AutoLoopEvent.type == "routing.fallback"))
        assert fallback is not None and fallback.error == "binding_ambiguous"
        assert db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == event)) is None


def test_dispatch_entry_materializes_deployment_default_for_first_use_owner(
        sessions, owner, monkeypatch):
    base, workspace = "http://127.0.0.1:19004", "workspace-default"
    _configure_default(monkeypatch, base, workspace)
    _plain_rule(sessions, owner)

    event, work = _admit_schedule(sessions, owner, str(uuid4()))
    dispatching.process_dispatch(work, session_factory=sessions)
    with sessions() as db:
        revision = db.scalar(select(SuperAssistantMulticaProfileRevision.id).join(
            SuperAssistantMulticaProfile,
            SuperAssistantMulticaProfile.id == SuperAssistantMulticaProfileRevision.profile_id,
        ).where(SuperAssistantMulticaProfile.owner_id == owner))
        assert revision is not None
        action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.event_id == event))
        assert action is not None  # routed, not a multica_unavailable fallback
        step = db.scalar(select(AutoLoopActionStep).where(AutoLoopActionStep.action_id == action.id))
        assert step.request_payload["baseUrl"] == base
        assert step.request_payload["workspaceId"] == workspace
        assert step.request_payload["profileRevisionId"] == revision


def test_decide_materializes_deployment_default_after_idempotency_check(
        sessions, owner, monkeypatch):
    base, workspace = "http://127.0.0.1:19005", "workspace-default"
    _configure_default(monkeypatch, base, workspace)
    # Unlinked assistant card: no workspace or frozen source ever recorded, so
    # resolution must fall back to the deployment default after the sync.
    row = _card(sessions, owner, None, issue=None, source="assistant")

    client, api_base = _client(sessions, owner)
    response = client.post(api_base + f"/decision-items/{row.id}/decide", json={
        "expectedContentVersion": row.content_version, "idempotencyKey": "answer",
        "optionId": "continue"})
    assert response.status_code == 202
    with sessions() as db:
        revision = db.scalar(select(SuperAssistantMulticaProfileRevision.id).join(
            SuperAssistantMulticaProfile,
            SuperAssistantMulticaProfile.id == SuperAssistantMulticaProfileRevision.profile_id,
        ).where(SuperAssistantMulticaProfile.owner_id == owner))
        assert revision is not None
        step = db.scalar(select(AutoLoopActionStep).where(
            AutoLoopActionStep.kind == "decision.answered"))
        assert step.request_payload["baseUrl"] == base
        assert step.request_payload["profileRevisionId"] == revision
