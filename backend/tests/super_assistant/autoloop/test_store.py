"""Persistence invariants run on SQLite and opt-in isolated real PostgreSQL."""
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.super_assistant.autoloop import store
from app.super_assistant.autoloop.models import (
    FACT_TABLES, UNFINISHED, AutoLoopAction, AutoLoopActionStep, AutoLoopConsumedNeed,
    AutoLoopDecision, AutoLoopDecisionRequest, AutoLoopDispatch, AutoLoopEvent,
    AutoLoopEvidence, AutoLoopReceipt, utcnow,
)


@pytest.fixture(params=["sqlite", "postgresql"])
def durable_db(request, db):
    if request.param == "sqlite":
        yield db
    else:
        with Session(request.getfixturevalue("pg_engine")) as session:
            yield session


@pytest.fixture
def owners(durable_db):
    ids = [str(uuid4()), str(uuid4())]
    for index, identity in enumerate(ids):
        durable_db.add(User(id=identity, username=f"owner-{index}", email=f"owner-{index}@test.invalid",
                           password_hash="fixture-no-login"))
    durable_db.commit()
    return ids


def card(db, owner, **overrides):
    values = dict(owner_id=owner, workspace_id="workspace-a", issue_key="TASK-1",
                  source="watchdog", need_key="need-a", title="Choose", question="Continue?",
                  options=[{"id": "continue", "label": "Continue"}], evidence={"comment": ["c1"]})
    values.update(overrides)
    return store.create_or_update_decision(db, **values)


def decide(db, owner, row, key="decide-a", **overrides):
    values = dict(owner_id=owner, decision_id=row.id, actor_id=owner,
                  expected_content_version=row.content_version, idempotency_key=key, option_id="continue")
    values.update(overrides)
    return store.submit_decision(db, **values)


def handled(db, owner, row, key="handled-a", **overrides):
    values = dict(owner_id=owner, decision_id=row.id, actor_id=owner,
                  expected_content_version=row.content_version, idempotency_key=key)
    values.update(overrides)
    return store.mark_handled(db, **values)


def steps(db, row):
    action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.decision_item_id == row.id))
    return list(db.scalars(select(AutoLoopActionStep).where(
        AutoLoopActionStep.action_id == action.id).order_by(AutoLoopActionStep.ordinal)))


def test_receipt_fanout_atomic_replay_and_ownerless_quarantine(durable_db, owners):
    db, owner = durable_db, owners[0]
    values = dict(source_kind="notification", source_system="fixture", source_event_id="event-a",
                  owner_id=owner, payload={"text": "hello"}, ingest_key_id=str(uuid4()))
    receipt, event, created = store.accept_receipt(db, **values)
    assert created and event.receipt_id == receipt.id
    db.rollback()
    assert db.scalar(select(func.count()).select_from(AutoLoopReceipt)) == 0
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == 0
    assert db.scalar(select(func.count()).select_from(AutoLoopDispatch)) == 0
    receipt, event, _ = store.accept_receipt(db, **values)
    identity = receipt.id, event.id
    db.commit()
    replay, original, created = store.accept_receipt(db, **values)
    assert not created and (replay.id, original.id) == identity
    assert db.scalar(select(func.count()).select_from(AutoLoopDispatch)) == 1
    _, unowned, _ = store.accept_receipt(db, **{**values, "source_event_id": "unowned", "owner_id": None})
    dispatch = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.event_id == unowned.id))
    assert unowned.action == dispatch.state == "awaiting_owner"
    assert store.claim_dispatch(db, dispatch.id) is None


def test_dispatch_lease_recovery_rejects_stale_completion(durable_db, owners):
    db = durable_db
    _, event, _ = store.accept_receipt(db, source_kind="schedule", source_system="fixture",
        source_event_id="run-a", owner_id=owners[0], payload={})
    dispatch = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.event_id == event.id))
    claimed = store.claim_dispatch(db, dispatch.id, lease_seconds=-1)
    old_token = claimed.lease_token
    assert old_token and store.claim_dispatch(db, dispatch.id) is None
    assert store.recover_dispatches(db) == 1
    reclaimed = store.claim_dispatch(db, dispatch.id)
    assert reclaimed.lease_token != old_token and reclaimed.attempts == 2
    assert not store.finish_dispatch(db, dispatch.id, old_token)
    assert store.finish_dispatch(db, dispatch.id, reclaimed.lease_token)
    db.commit()
    assert db.get(AutoLoopDispatch, dispatch.id).state == "done"


@pytest.mark.parametrize("state", UNFINISHED)
def test_every_unfinished_state_has_database_uniqueness(durable_db, owners, state):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner)
    row.status = state
    unlinked, _ = card(db, owner, source="assistant", issue_key=None)
    unlinked.status = state
    db.commit()
    for issue in ("TASK-1", None):
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(AutoLoopDecision(owner_id=owner, workspace_id="workspace-a", issue_key=issue,
                source="assistant", status="open", need_key="need-a", title="duplicate", question="?"))
            db.flush()
    # Other owner/workspace and distinct unlinked needs remain independent.
    assert card(db, owners[1])[1]
    assert card(db, owner, workspace_id="workspace-b")[1]
    assert card(db, owner, issue_key=None, source="assistant", need_key="need-b")[1]


def test_open_content_cas_and_terminal_need_consumption(durable_db, owners):
    db, owner = durable_db, owners[0]
    row, created = card(db, owner)
    assert created and row.content_version == row.version == 1
    same, created = card(db, owner, evidence={"comment": ["c1", "c2"]}, question="New evidence?")
    assert same.id == row.id and not created and same.content_version == 2
    with pytest.raises(store.StoreConflict) as conflict:
        decide(db, owner, row, expected_content_version=1)
    assert conflict.value.http_status == 412
    with pytest.raises(store.StoreConflict) as conflict:
        handled(db, owners[1], row)
    assert conflict.value.http_status == 404
    row, request, replay = handled(db, owner, row)
    assert not replay and row.status == "handled" and request.result["label"] == "已人工处理"
    assert row.content_version == 3
    assert handled(db, owner, row, expected_content_version=request.content_version)[2]
    assert row.content_version == 3
    with pytest.raises(store.StoreConflict) as conflict:
        decide(db, owner, row)
    assert conflict.value.http_status == 409
    db.commit()
    assert card(db, owner, evidence={"comment": ["c1", "c2", "late"]}) == (None, False)
    consumed = db.scalar(select(AutoLoopConsumedNeed))
    assert consumed.evidence_watermark == {"comment": ["c1", "c2"]}
    assert card(db, owner, need_key="new-need")[1]


@pytest.mark.parametrize("source,linked,kinds", [
    ("watchdog", True, ["issue.comment"]),
    ("assistant", True, ["decision.answered", "issue.comment"]),
    ("kernel_fallback", False, ["decision.answered"]),
])
def test_answer_source_split_stable_identity_replay_and_consumption(durable_db, owners, source, linked, kinds):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner, source=source, issue_key="TASK-1" if linked else None, workspace_id=None)
    assert row.workspace_id == "@owner"
    row, request, replay = decide(db, owner, row)
    assert not replay and row.status == "pending_dispatch" and row.content_version == 2
    plan = steps(db, row)
    identities = [(step.id, step.remote_request_id) for step in plan]
    assert [step.kind for step in plan] == kinds
    assert all(UUID(identity).version == 4 for pair in identities for identity in pair)
    db.commit()
    _, repeated, replay = decide(db, owner, row, expected_content_version=request.content_version)
    assert replay and repeated.id == request.id and row.content_version == 2
    assert [(step.id, step.remote_request_id) for step in steps(db, row)] == identities
    if len(plan) > 1:
        assert store.claim_step(db, owner_id=owner, step_id=plan[1].id) is None
    for step in plan:
        claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
        assert claimed.ever_sent_at is not None
        assert store.finish_step(db, owner_id=owner, step_id=step.id, lease_token=claimed.lease_token,
                                 outcome="confirmed", remote_object_id="saved-" + step.id)
    db.refresh(row)
    assert row.status == "completed"
    terminal_version = row.content_version
    facts_count = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    _, repeated, replay = decide(db, owner, row, expected_content_version=request.content_version)
    assert replay and repeated.id == request.id and row.content_version == terminal_version
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == facts_count
    assert db.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 1
    assert db.scalar(select(AutoLoopConsumedNeed.reason)) == "completed"
    assert card(db, owner, source=source, issue_key="TASK-1" if linked else None, workspace_id=None) == (None, False)


@pytest.mark.parametrize("category,changed", [
    ("decide", {"expected_content_version": 2}),
    ("decide", {"option_id": "different"}),
    ("decide", {"free_text": "different"}),
    ("mark_handled", {"expected_content_version": 2}),
    ("mark_handled", {"reason": "different"}),
])
def test_accepted_idempotency_key_is_bound_to_exact_original_payload(durable_db, owners, category, changed):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner)
    submit = decide if category == "decide" else handled
    row, request, _ = submit(db, owner, row)
    db.commit()
    version = row.content_version
    counts = {model: db.scalar(select(func.count()).select_from(model)) for model in (
        AutoLoopDecisionRequest, AutoLoopAction, AutoLoopActionStep, AutoLoopConsumedNeed, AutoLoopEvent)}
    with pytest.raises(store.StoreConflict) as conflict:
        submit(db, owner, row, **{"expected_content_version": request.content_version, **changed})
    assert conflict.value.http_status == 409 and conflict.value.code == "idempotency_key_conflict"
    assert conflict.value.current["status"] == row.status
    assert row.content_version == version
    assert all(db.scalar(select(func.count()).select_from(model)) == count for model, count in counts.items())
    with pytest.raises(store.StoreConflict) as denied:
        submit(db, owners[1], row, expected_content_version=request.content_version)
    assert denied.value.http_status == 404
    assert row.content_version == version


@pytest.mark.parametrize("category", ["decide", "mark_handled"])
def test_rejected_cas_does_not_bind_key_and_normalized_replay_is_read_only(durable_db, owners, category):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner)
    card(db, owner, question="Updated wording without new evidence?")
    assert row.content_version == 2
    submit = decide if category == "decide" else handled
    content = {"free_text": "  confirmed choice  "} if category == "decide" else {"reason": "  done externally  "}
    original_facts = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    with pytest.raises(store.StoreConflict) as conflict:
        submit(db, owner, row, expected_content_version=1, **content)
    assert conflict.value.http_status == 412 and row.content_version == 2
    for model in (AutoLoopDecisionRequest, AutoLoopAction, AutoLoopActionStep, AutoLoopConsumedNeed):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == original_facts
    row, request, replay = submit(db, owner, row, expected_content_version=2, **content)
    assert not replay and row.content_version == 3 and request.content_version == 2
    db.commit()
    facts = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    normalized = {key: value.strip() for key, value in content.items()}
    _, repeated, replay = submit(db, owner, row, expected_content_version=2, **normalized)
    assert replay and repeated.id == request.id and row.content_version == 3
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == facts


def test_only_never_attempted_pending_steps_can_be_cancelled(durable_db, owners):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner)
    decide(db, owner, row)
    plan = steps(db, row)
    handled(db, owner, row)
    db.commit()
    assert row.status == "handled"
    assert store.claim_step(db, owner_id=owner, step_id=plan[0].id) is None
    db.refresh(plan[0])
    assert plan[0].cancelled_at is not None and plan[0].ever_sent_at is None


def test_expired_sending_is_unknown_and_retries_keep_remote_identity(durable_db, owners):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner)
    decide(db, owner, row)
    step = steps(db, row)[0]
    stable = step.id, step.remote_request_id
    claim = store.claim_step(db, owner_id=owner, step_id=step.id, lease_seconds=-1)
    old_token = claim.lease_token
    assert store.recover_step(db, owner_id=owner, step_id=step.id)
    db.refresh(row)
    assert row.status == "writeback_unknown"
    assert not store.finish_step(db, owner_id=owner, step_id=step.id, lease_token=old_token, outcome="confirmed")
    assert store.claim_step(db, owner_id=owner, step_id=step.id) is None
    assert not store.retry_failed_step(db, owner_id=owner, step_id=step.id)
    with pytest.raises(store.StoreConflict) as conflict:
        handled(db, owner, row)
    assert conflict.value.http_status == 409
    with pytest.raises(ValueError):
        store.reconcile_step(db, owner_id=owner, step_id=step.id, outcome="failed", proof={})
    assert store.reconcile_step(db, owner_id=owner, step_id=step.id, outcome="failed",
                                proof={"kind": "authoritative_nonexecution", "requestId": step.remote_request_id})
    assert store.retry_failed_step(db, owner_id=owner, step_id=step.id)
    with pytest.raises(store.StoreConflict):
        handled(db, owner, row)  # Requeued attempted writes are never safe to cancel.
    again = store.claim_step(db, owner_id=owner, step_id=step.id)
    assert (again.id, again.remote_request_id) == stable and again.attempts == 2
    assert store.finish_step(db, owner_id=owner, step_id=step.id, lease_token=again.lease_token,
                             outcome="unknown", error="response_lost")
    assert store.reconcile_step(db, owner_id=owner, step_id=step.id, outcome="confirmed",
                                proof={"kind": "remote_id_lookup"}, remote_object_id="comment-a")
    db.refresh(row)
    assert row.status == "completed"


def test_partial_writeback_blocks_handled_and_later_steps(durable_db, owners):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner, source="assistant")
    decide(db, owner, row)
    first, second = steps(db, row)
    claim = store.claim_step(db, owner_id=owner, step_id=first.id)
    store.finish_step(db, owner_id=owner, step_id=first.id, lease_token=claim.lease_token, outcome="confirmed")
    claim = store.claim_step(db, owner_id=owner, step_id=second.id)
    with pytest.raises(ValueError):
        store.finish_step(db, owner_id=owner, step_id=second.id, lease_token=claim.lease_token, outcome="failed")
    store.finish_step(db, owner_id=owner, step_id=second.id, lease_token=claim.lease_token,
                      outcome="failed", proof={"kind": "HTTP_rejection", "status": 422})
    db.refresh(row)
    assert row.status == "writeback_failed"
    assert db.scalar(select(AutoLoopAction.state)) == "partial"
    version = row.content_version
    facts = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    for expected in (1, version):
        with pytest.raises(store.StoreConflict) as conflict:
            handled(db, owner, row, expected_content_version=expected)
        assert conflict.value.http_status == 409
        assert conflict.value.code == "side_effect_in_progress"
        assert conflict.value.current["status"] == "writeback_failed"
        assert [item["state"] for item in conflict.value.current["writebackSteps"]] == ["confirmed", "failed"]
        assert all(item["everSentAt"] for item in conflict.value.current["writebackSteps"])
        assert row.content_version == version
        assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == facts
        assert db.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 1
        assert db.scalar(select(func.count()).select_from(AutoLoopConsumedNeed)) == 0


def test_invalid_value_does_not_bind_idempotency_key(durable_db, owners):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner)
    facts = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    with pytest.raises(store.StoreConflict) as conflict:
        decide(db, owner, row, option_id="not-an-option")
    assert conflict.value.http_status == 422
    assert row.content_version == 1
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == facts
    for model in (AutoLoopDecisionRequest, AutoLoopAction, AutoLoopActionStep):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    row, request, replay = decide(db, owner, row)
    assert not replay and request.content_version == 1 and row.content_version == 2


def test_late_evidence_and_stream_cursors_are_independent(durable_db, owners):
    db, owner = durable_db, owners[0]
    values = dict(owner_id=owner, workspace_id="workspace-a", issue_key="TASK-1")
    store.record_evidence(db, **values, stream="comment", items=[{"id": "new", "created_at": "2026-11-14T08:00:00Z"}], cursor={"before": "nano-1"})
    store.record_evidence(db, **values, stream="message", items=[{"id": "task:3"}], cursor={"seq": 3})
    progress = store.record_evidence(db, **values, stream="comment", items=[{"id": "old", "created_at": "2026-11-13T08:00:00Z"}, {"id": "new"}], cursor={"before": "nano-2"})
    db.commit()
    assert progress.cursors == {"comment": {"before": "nano-2"}, "message": {"seq": 3}}
    evidence = list(db.scalars(select(AutoLoopEvidence)))
    assert {item.remote_id for item in evidence} == {"old", "new", "task:3"}
    assert next(item for item in evidence if item.remote_id == "new").payload["created_at"].endswith("08:00:00Z")
    assert {item.remote_id: item.happened_at.date().isoformat() for item in evidence if item.happened_at} == {
        "new": "2026-11-14", "old": "2026-11-13"}
    assert next(item for item in evidence if item.remote_id == "task:3").happened_at is None
    facts = list(db.scalars(select(AutoLoopEvent).where(
        AutoLoopEvent.type == "evidence.recorded").order_by(AutoLoopEvent.seq)))
    assert [fact.data["newRemoteIds"] for fact in facts] == [["new"], ["task:3"], ["old"]]
    assert [fact.data["version"] for fact in facts] == [2, 3, 4]
    assert facts[0].data["cursors"] == {"comment": {"before": "nano-1"}}
    assert facts[-1].data["cursors"] == progress.cursors
    assert {fact.correlation_id for fact in facts} == {progress.id}
    assert all(fact.owner_id == owner and fact.issue_ref == "TASK-1" for fact in facts)
    assert db.scalar(select(func.count()).select_from(AutoLoopDispatch)) == 0


def test_evidence_times_preserve_unknown_and_normalize_utc(durable_db, owners):
    db, owner = durable_db, owners[0]
    items = [
        {"id": "offset", "created_at": "2026-11-14T16:00:00+08:00"},
        {"id": "nano", "created_at": "2026-11-14T08:00:00.123456789Z"},
        {"id": "invalid", "created_at": "unknown"},
        {"id": "naive", "created_at": "2026-11-14T08:00:00"},
        {"id": "missing"},
    ]
    store.record_evidence(db, owner_id=owner, workspace_id="workspace-a", issue_key="TASK-1",
        stream="timeline", items=items, cursor={"complete": True})
    db.commit()
    evidence = {item.remote_id: item for item in db.scalars(select(AutoLoopEvidence))}
    assert evidence["offset"].happened_at.hour == 8
    assert evidence["nano"].happened_at.microsecond == 123456
    assert all(evidence[key].happened_at is None for key in ("invalid", "naive", "missing"))
    assert evidence["invalid"].payload == items[2]


def test_facts_reject_raw_updates_and_deletes(durable_db, owners):
    db, owner = durable_db, owners[0]
    store.accept_receipt(db, source_kind="notification", source_system="fixture", source_event_id="fact", owner_id=owner, payload={})
    row, _ = card(db, owner)
    store.record_evidence(db, owner_id=owner, workspace_id="workspace-a", issue_key="TASK-1", stream="comment", items=[{"id": "c1"}], cursor={})
    handled(db, owner, row)
    db.commit()
    for table in FACT_TABLES:
        assert db.scalar(select(func.count()).select_from(table)) > 0
        for statement in (f"UPDATE {table.name} SET id=id", f"DELETE FROM {table.name}"):
            with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
                db.execute(text(statement))
    assert db.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 1


def test_projection_history_preserves_atomic_states_and_stable_remote_identity(durable_db, owners):
    db, owner = durable_db, owners[0]
    _, origin, _ = store.accept_receipt(db, source_kind="notification", source_system="fixture",
        source_event_id="history", owner_id=owner, payload={})
    row, _ = card(db, owner, origin_event_id=origin.id)
    decision_id = row.id
    card(db, owner, origin_event_id=origin.id, evidence={"comment": ["c1", "c2"]}, title="Updated")
    decide(db, owner, row)
    step = steps(db, row)[0]
    stable = step.id, step.remote_request_id
    action_id = step.action_id
    claimed = store.claim_step(db, owner_id=owner, step_id=step.id, lease_seconds=-1)
    assert store.recover_step(db, owner_id=owner, step_id=step.id)
    count = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    # A stale worker must not invent a successful transition fact.
    assert not store.finish_step(db, owner_id=owner, step_id=step.id,
        lease_token=claimed.lease_token, outcome="confirmed")
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == count
    assert store.reconcile_step(db, owner_id=owner, step_id=step.id, outcome="failed",
        proof={"kind": "authoritative_nonexecution", "requestId": stable[1]})
    assert store.retry_failed_step(db, owner_id=owner, step_id=step.id)
    claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
    assert store.finish_step(db, owner_id=owner, step_id=step.id, lease_token=claimed.lease_token,
        outcome="confirmed", remote_object_id="comment-saved", remote_result={"id": "comment-saved"})
    db.commit()

    facts = list(db.scalars(select(AutoLoopEvent).where(
        AutoLoopEvent.decision_item_id == decision_id).order_by(AutoLoopEvent.seq)))
    decision_facts = [fact for fact in facts if fact.type.startswith("decision.")]
    assert [fact.action for fact in decision_facts] == [
        "open", "open", "pending_dispatch", "sending", "writeback_unknown", "writeback_failed",
        "pending_dispatch", "sending", "completed"]
    assert [fact.data["version"] for fact in decision_facts] == list(range(1, 10))
    assert [fact.data["contentVersion"] for fact in decision_facts] == list(range(1, 10))
    assert decision_facts[0].data["title"] == "Choose"
    assert decision_facts[1].data["title"] == "Updated"
    assert decision_facts[0].data["evidence"] == {"comment": ["c1"]}
    assert decision_facts[1].data["contentVersion"] == 2
    action_facts = [fact for fact in facts if fact.action_id == action_id]
    assert [fact.type for fact in action_facts] == [
        "action.created", "action.step_claimed", "action.step_recovered", "action.step_reconciled",
        "action.step_requeued", "action.step_claimed", "action.step_finished"]
    assert [fact.data["steps"][0]["state"] for fact in action_facts] == [
        "queued", "sending", "unknown", "failed", "queued", "sending", "confirmed"]
    assert {tuple((fact.data["steps"][0]["id"], fact.data["steps"][0]["remoteRequestId"]))
        for fact in action_facts} == {stable}
    assert action_facts[-1].data["steps"][0]["remoteObjectId"] == "comment-saved"
    assert action_facts[-1].data["steps"][0]["attempts"] == 2
    assert action_facts[-1].data["steps"][0]["result"] == {"id": "comment-saved"}
    assert all(fact.parent_event_id == origin.id and fact.source_system == "fixture" for fact in facts)
    assert db.scalar(select(func.count()).select_from(AutoLoopDispatch)) == 1

    count = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    new, _ = card(db, owner, issue_key="TASK-2", need_key="rolled-back")
    decide(db, owner, new)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == count
    assert db.get(AutoLoopDecision, new.id) is None


@pytest.mark.parametrize("terminal", ["handled", "withdrawn", "completed"])
def test_terminal_consumes_all_accepted_needs_atomically(durable_db, owners, terminal):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner)
    identity = row.id
    evidence_a = {"comment": ["c1", "c2"]}
    evidence_b = {"comment": ["b1"]}
    card(db, owner, evidence=evidence_a)
    same, created = card(db, owner, need_key="need-b", question="Question B?", evidence=evidence_b)
    assert not created and same.id == identity and same.need_key == "need-b"
    assert same.content_version == 3 and same.evidence_baseline == {"comment": ["c1"]}
    db.commit()
    before = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    closing_evidence = {**evidence_b, "externalFollowup": ["member-comment"]} if terminal == "withdrawn" else evidence_b

    def close():
        current = db.get(AutoLoopDecision, identity, populate_existing=True)
        if terminal == "handled":
            handled(db, owner, current)
        elif terminal == "withdrawn":
            store.withdraw_decision(db, owner_id=owner, decision_id=identity,
                expected_content_version=current.content_version,
                reason="external_followup", evidence=closing_evidence)
        else:
            decide(db, owner, current)
            for step in steps(db, current):
                claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
                assert store.finish_step(db, owner_id=owner, step_id=step.id,
                    lease_token=claimed.lease_token, outcome="confirmed", remote_object_id="saved-" + step.id)
        assert current.status == terminal
        db.flush()
        return current

    close()
    assert db.scalar(select(func.count()).select_from(AutoLoopConsumedNeed)) == 2
    db.rollback()
    assert db.get(AutoLoopDecision, identity).status == "open"
    assert db.scalar(select(func.count()).select_from(AutoLoopConsumedNeed)) == 0
    assert db.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 0
    assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == before
    close()
    db.commit()

    consumed = {item.need_key: item for item in db.scalars(select(AutoLoopConsumedNeed))}
    assert set(consumed) == {"need-a", "need-b"}
    assert consumed["need-a"].evidence_watermark == evidence_a
    assert consumed["need-b"].evidence_watermark == closing_evidence
    assert all((item.owner_id, item.workspace_id, item.issue_key, item.decision_item_id, item.reason)
        == (owner, "workspace-a", "TASK-1", identity, terminal) for item in consumed.values())
    facts = list(db.scalars(select(AutoLoopEvent).where(
        AutoLoopEvent.decision_item_id == identity,
        AutoLoopEvent.type.in_(("decision.created", "decision.updated"))).order_by(AutoLoopEvent.seq)))
    assert [(fact.data["needKey"], fact.data["evidence"]) for fact in facts[:3]] == [
        ("need-a", {"comment": ["c1"]}), ("need-a", evidence_a), ("need-b", evidence_b)]
    assert facts[-1].action == terminal and facts[-1].data["evidence"] == closing_evidence
    count = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    assert card(db, owner, evidence=evidence_a) == (None, False)
    assert card(db, owner, need_key="need-b", question="Question B?", evidence=evidence_b) == (None, False)
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == count
    # Consumption stays within its owner/workspace/Issue; equal evidence alone
    # never suppresses a genuinely distinct actionable need.
    assert card(db, owners[1], evidence=evidence_a)[1]
    assert card(db, owner, workspace_id="workspace-b", evidence=evidence_a)[1]
    assert card(db, owner, issue_key="TASK-2", evidence=evidence_a)[1]
    fresh, created = card(db, owner, need_key="need-c", evidence=evidence_b)
    assert created and fresh.id != identity and fresh.status == "open"
    assert db.scalar(select(func.count()).select_from(AutoLoopDecision).where(
        AutoLoopDecision.owner_id == owner, AutoLoopDecision.workspace_id == "workspace-a",
        AutoLoopDecision.issue_key == "TASK-1", AutoLoopDecision.status.in_(UNFINISHED))) == 1


@pytest.mark.parametrize("first_source,last_source", [
    ("watchdog", "assistant"), ("assistant", "watchdog"),
    ("watchdog", "kernel_fallback"), ("kernel_fallback", "watchdog"),
])
def test_cross_source_question_cause_and_stable_submission_agree(durable_db, owners, first_source, last_source):
    db, owner = durable_db, owners[0]
    origins = []
    for index, source in enumerate((first_source, last_source)):
        _, origin, _ = store.accept_receipt(db, source_kind="notification", source_system=source,
            source_event_id=f"cause-{index}", owner_id=owner, payload={"source": source})
        origins.append(origin)
    row, _ = card(db, owner, source=first_source, origin_event_id=origins[0].id)
    identity = row.id
    old_version = row.content_version
    same, created = card(db, owner, source=last_source, need_key="need-b",
        origin_event_id=origins[1].id, title="Latest", question=f"{last_source} question?",
        evidence={"comment": ["b1"]})
    assert not created and same.id == identity
    assert (same.source, same.need_key, same.origin_event_id, same.question) == (
        last_source, "need-b", origins[1].id, f"{last_source} question?")
    assert same.content_version == old_version + 1
    facts = list(db.scalars(select(AutoLoopEvent).where(
        AutoLoopEvent.decision_item_id == identity).order_by(AutoLoopEvent.seq)))
    assert [(fact.data["source"], fact.data["needKey"], fact.parent_event_id, fact.source_system)
        for fact in facts] == [(first_source, "need-a", origins[0].id, first_source),
                              (last_source, "need-b", origins[1].id, last_source)]
    assert [fact.data["originEventId"] for fact in facts] == [item.id for item in origins]
    assert db.scalar(select(func.count()).select_from(AutoLoopDecision)) == 1
    with pytest.raises(store.StoreConflict) as conflict:
        decide(db, owner, row, expected_content_version=old_version)
    assert conflict.value.http_status == 412
    assert db.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 0
    assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 0
    accepted_version = row.content_version
    _, request, replay = decide(db, owner, row)
    assert not replay
    action_id = request.result["actionId"]
    planned = steps(db, row)
    expected_kinds = (["decision.answered"] if last_source != "watchdog" else []) + ["issue.comment"]
    assert [step.kind for step in planned] == expected_kinds
    stable = [(step.id, step.remote_request_id) for step in planned]
    assert len({value for pair in stable for value in pair}) == 2 * len(planned)
    assert all(UUID(value).version == 4 for pair in stable for value in pair)
    assert all(step.request_payload["decisionItemId"] == identity
        and step.request_payload["issueRef"] == "TASK-1" for step in planned)
    db.commit()
    version = row.content_version
    count = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    _, repeated, replay = decide(db, owner, row, expected_content_version=accepted_version)
    assert replay and repeated.id == request.id and repeated.result["actionId"] == action_id
    assert row.content_version == version
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == count
    for step in planned:
        claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
        assert store.finish_step(db, owner_id=owner, step_id=step.id,
            lease_token=claimed.lease_token, outcome="confirmed", remote_object_id="saved-" + step.id)
    db.commit()
    _, repeated, replay = decide(db, owner, row, expected_content_version=accepted_version)
    assert replay and repeated.id == request.id and row.status == "completed"
    assert repeated.result["actionId"] == action_id
    assert [(step.id, step.remote_request_id) for step in steps(db, row)] == stable
    assert db.scalar(select(func.count()).select_from(AutoLoopAction)) == 1
    assert db.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 1
    assert db.scalar(select(func.count()).select_from(AutoLoopActionStep).where(
        AutoLoopActionStep.kind == "decision.answered")) == (last_source != "watchdog")
    assert set(db.scalars(select(AutoLoopConsumedNeed.need_key))) == {"need-a", "need-b"}
    associated = list(db.scalars(select(AutoLoopEvent).where(
        AutoLoopEvent.decision_item_id == identity,
        AutoLoopEvent.type != "decision.created").order_by(AutoLoopEvent.seq)))
    assert all(fact.parent_event_id == origins[1].id and fact.source_system == last_source
        and fact.issue_ref == "TASK-1" for fact in associated)
    assert all(fact.action_id == action_id for fact in associated if fact.type.startswith("action."))


@pytest.mark.parametrize("state", ["pending_dispatch", "sending", "writeback_unknown", "writeback_failed"])
def test_submitted_question_stays_frozen_without_consuming_unaccepted_need(durable_db, owners, state):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner, source="assistant")
    decide(db, owner, row)
    planned = steps(db, row)
    stable = [(step.id, step.remote_request_id, step.kind, step.request_payload) for step in planned]
    claimed = None
    if state != "pending_dispatch":
        claimed = store.claim_step(db, owner_id=owner, step_id=planned[0].id)
    if state in ("writeback_unknown", "writeback_failed"):
        outcome = "unknown" if state == "writeback_unknown" else "failed"
        assert store.finish_step(db, owner_id=owner, step_id=claimed.id, lease_token=claimed.lease_token,
            outcome=outcome, proof={"kind": "authoritative_nonexecution"} if outcome == "failed" else None)
    assert row.status == state
    content_version = row.content_version
    count = db.scalar(select(func.count()).select_from(AutoLoopEvent))
    same, created = card(db, owner, source="watchdog", need_key="need-c", question="Later scan?",
        evidence={"comment": ["later-comment"]})
    assert not created and same.id == row.id and same.content_version == content_version
    assert (same.source, same.need_key, same.question) == ("assistant", "need-a", "Continue?")
    assert same.current_evidence == {"comment": ["c1"]}
    assert db.scalar(select(func.count()).select_from(AutoLoopEvent)) == count
    assert [(step.id, step.remote_request_id, step.kind, step.request_payload) for step in steps(db, row)] == stable
    store.record_evidence(db, owner_id=owner, workspace_id="workspace-a", issue_key="TASK-1",
        stream="comment", items=[{"id": "later-comment"}], cursor={"after": "later-comment"})
    assert db.scalar(select(AutoLoopEvidence.remote_id)) == "later-comment"
    assert db.scalar(select(func.count()).select_from(AutoLoopConsumedNeed)) == 0
    if state == "sending":
        assert store.finish_step(db, owner_id=owner, step_id=claimed.id, lease_token=claimed.lease_token,
            outcome="confirmed", remote_object_id="event-saved")
    elif state == "writeback_unknown":
        assert store.reconcile_step(db, owner_id=owner, step_id=planned[0].id, outcome="confirmed",
            proof={"kind": "authoritative_result"}, remote_object_id="event-saved")
    elif state == "writeback_failed":
        assert store.retry_failed_step(db, owner_id=owner, step_id=planned[0].id)
    for step in steps(db, row):
        if step.state == "confirmed":
            continue
        claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
        assert store.finish_step(db, owner_id=owner, step_id=step.id, lease_token=claimed.lease_token,
            outcome="confirmed", remote_object_id="saved-" + step.id)
    db.commit()
    assert row.status == "completed"
    assert set(db.scalars(select(AutoLoopConsumedNeed.need_key))) == {"need-a"}
    assert card(db, owner, source="watchdog", need_key="need-c", question="Later scan?",
        evidence={"comment": ["later-comment"]})[1]


def test_need_identity_change_alone_is_a_content_update(durable_db, owners):
    db, owner = durable_db, owners[0]
    row, _ = card(db, owner)
    identity = row.id
    same, created = card(db, owner, need_key="need-b")
    assert not created and same.id == identity and same.need_key == "need-b" and same.content_version == 2
    handled(db, owner, same)
    db.commit()
    assert set(db.scalars(select(AutoLoopConsumedNeed.need_key))) == {"need-a", "need-b"}
    assert card(db, owner) == card(db, owner, need_key="need-b") == (None, False)


def test_action_event_replay_keeps_committed_request_ids(durable_db, owners):
    db, owner = durable_db, owners[0]
    _, event, _ = store.accept_receipt(db, source_kind="notification", source_system="fixture", source_event_id="action", owner_id=owner, payload={})
    action, created = store.persist_action(db, owner_id=owner, event_id=event.id, steps=[("issue.create", {"title": "hello"})])
    identity = action.id
    step_id = db.scalar(select(AutoLoopActionStep.remote_request_id))
    db.commit()
    replay, created = store.persist_action(db, owner_id=owner, event_id=event.id, steps=[("issue.create", {"title": "different"})])
    assert not created and replay.id == identity
    assert db.scalar(select(AutoLoopActionStep.remote_request_id)) == step_id
    with pytest.raises(store.StoreConflict) as conflict:
        store.persist_action(db, owner_id=owners[1], event_id=event.id, steps=[])
    assert conflict.value.http_status == 404
