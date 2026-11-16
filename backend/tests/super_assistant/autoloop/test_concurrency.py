"""Two real PostgreSQL transactions verify lock contention and final invariants."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Queue
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.super_assistant.autoloop import store
from app.super_assistant.autoloop.models import (
    UNFINISHED, AutoLoopAction, AutoLoopActionStep, AutoLoopConsumedNeed,
    AutoLoopDecision, AutoLoopDecisionRequest, AutoLoopDispatch, AutoLoopEvent,
    AutoLoopReceipt,
)


@pytest.fixture
def owner(pg_engine):
    identity = str(uuid4())
    with Session(pg_engine) as db:
        db.add(User(id=identity, username="race-owner", email="race@test.invalid",
                    password_hash="fixture-no-login"))
        db.commit()
    return identity


def _card(db, owner, **overrides):
    values = dict(owner_id=owner, workspace_id="workspace-a", issue_key="TASK-1",
                  source="watchdog", need_key="need-a", title="Choose", question="Continue?",
                  options=[{"id": "continue", "label": "Continue"}], evidence={"comment": ["c1"]})
    return store.create_or_update_decision(db, **{**values, **overrides})


def _decide(db, owner, identity, key="decide-a"):
    return store.submit_decision(db, owner_id=owner, decision_id=identity,
        actor_id=owner, expected_content_version=1, idempotency_key=key, option_id="continue")


def _handled(db, owner, identity, expected_content_version=1):
    return store.mark_handled(db, owner_id=owner, decision_id=identity,
        actor_id=owner, expected_content_version=expected_content_version, idempotency_key="handled-a")


@contextmanager
def _contender(holder, operation):
    """Yield only after PostgreSQL confirms the second connection waits on holder.

    This does not infer concurrency from a sleep or a sequential result. A bounded
    server statement timeout and holder rollback also make assertion failures safe.
    """
    ready = Queue(maxsize=1)
    engine = holder.get_bind()
    holder_pid = holder.scalar(select(func.pg_backend_pid()))

    def run():
        with Session(engine) as db:
            db.execute(text("SET LOCAL statement_timeout = '8s'"))
            ready.put(db.scalar(select(func.pg_backend_pid())))
            try:
                result = operation(db)
                db.commit()
                return result
            except (IntegrityError, store.StoreConflict) as exc:
                db.rollback()
                return exc

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            contender_pid = ready.get(timeout=5)
            assert contender_pid != holder_pid
            deadline = monotonic() + 5
            while not holder.scalar(text("SELECT :holder = ANY(pg_blocking_pids(:contender))"),
                                    {"holder": holder_pid, "contender": contender_pid}):
                if future.done():
                    pytest.fail(f"contender completed without a conflicting lock: {future.result()!r}")
                assert monotonic() < deadline, "second PostgreSQL connection never waited on holder"
                Event().wait(0.01)
            yield future
        finally:
            # Release locks before waiting for a thread if an assertion failed.
            holder.rollback()


@pytest.mark.parametrize("issue", ["TASK-1", None], ids=["linked", "unlinked"])
@pytest.mark.parametrize("state", UNFINISHED)
def test_database_unfinished_uniqueness_between_connections(pg_engine, owner, issue, state):
    def duplicate(db):
        db.add(AutoLoopDecision(owner_id=owner, workspace_id="workspace-a", issue_key=issue,
            source="assistant", status="open", need_key="need-a", title="duplicate", question="?"))
        db.flush()

    with Session(pg_engine) as left:
        row, _ = _card(left, owner, issue_key=issue, source="assistant")
        row.status = state
        left.flush()
        with _contender(left, duplicate) as future:
            left.commit()
            assert isinstance(future.result(timeout=10), IntegrityError)
        assert left.scalar(select(func.count()).select_from(AutoLoopDecision)) == 1


def test_concurrent_receipt_replay_has_one_fact_and_dispatch(pg_engine, owner):
    values = dict(source_kind="notification", source_system="fixture", source_event_id="event-a",
                  owner_id=owner, payload={"text": "hello"})

    def replay(db):
        receipt, event, created = store.accept_receipt(db, **values)
        return receipt.id, event.id, created

    with Session(pg_engine) as left:
        receipt, event, created = store.accept_receipt(left, **values)
        identities = receipt.id, event.id
        assert created
        with _contender(left, replay) as future:
            left.commit()
            assert future.result(timeout=10) == (*identities, False)
        for model in (AutoLoopReceipt, AutoLoopEvent, AutoLoopDispatch):
            assert left.scalar(select(func.count()).select_from(model)) == 1


def test_concurrent_need_creation_returns_one_open_decision(pg_engine, owner):
    def duplicate(db):
        row, created = _card(db, owner)
        return row.id, created

    with Session(pg_engine) as left:
        row, created = _card(left, owner)
        identity = row.id
        assert created
        with _contender(left, duplicate) as future:
            left.commit()
            assert future.result(timeout=10) == (identity, False)
        assert left.scalar(select(func.count()).select_from(AutoLoopDecision)) == 1


def test_concurrent_state_cas_keeps_first_update(pg_engine, owner):
    with Session(pg_engine) as left:
        row, _ = _card(left, owner)
        identity = row.id
        left.commit()
        first = left.execute(update(AutoLoopDecision).where(
            AutoLoopDecision.id == identity, AutoLoopDecision.content_version == 1,
        ).values(content_version=2, question="first")).rowcount
        assert first == 1

        def stale(db):
            return db.execute(update(AutoLoopDecision).where(
                AutoLoopDecision.id == identity, AutoLoopDecision.content_version == 1,
            ).values(content_version=2, question="second")).rowcount

        with _contender(left, stale) as future:
            left.commit()
            assert future.result(timeout=10) == 0
        left.expire_all()
        assert left.get(AutoLoopDecision, identity).question == "first"


def test_concurrent_decide_replay_keeps_action_identity(pg_engine, owner):
    with Session(pg_engine) as left:
        row, _ = _card(left, owner)
        identity = row.id
        left.commit()
        _decide(left, owner, identity)
        action_id = left.scalar(select(AutoLoopAction.id))
        remote_id = left.scalar(select(AutoLoopActionStep.remote_request_id))

        def replay(db):
            _, request, repeated = _decide(db, owner, identity)
            return request.result["actionId"], repeated

        with _contender(left, replay) as future:
            left.commit()
            assert future.result(timeout=10) == (action_id, True)
        assert left.scalar(select(AutoLoopActionStep.remote_request_id)) == remote_id
        assert left.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 1


def test_handled_wins_against_concurrent_decide(pg_engine, owner):
    with Session(pg_engine) as left:
        row, _ = _card(left, owner)
        identity = row.id
        left.commit()
        _handled(left, owner, identity)
        with _contender(left, lambda db: _decide(db, owner, identity)) as future:
            left.commit()
            conflict = future.result(timeout=10)
            assert isinstance(conflict, store.StoreConflict) and conflict.http_status == 409
        assert left.scalar(select(func.count()).select_from(AutoLoopAction)) == 0
        assert left.scalar(select(func.count()).select_from(AutoLoopConsumedNeed)) == 1


def test_decide_wins_against_stale_open_handled(pg_engine, owner):
    with Session(pg_engine) as left:
        row, _ = _card(left, owner)
        identity = row.id
        left.commit()
        _decide(left, owner, identity)
        step = left.scalar(select(AutoLoopActionStep))
        stable = step.id, step.remote_request_id
        fact_count = left.scalar(select(func.count()).select_from(AutoLoopEvent))
        with _contender(left, lambda db: _handled(db, owner, identity)) as future:
            left.commit()
            conflict = future.result(timeout=10)
            assert isinstance(conflict, store.StoreConflict) and conflict.http_status == 412
            assert conflict.code == "content_version_conflict"
            assert conflict.current["status"] == "pending_dispatch"
            assert conflict.current["contentVersion"] == 2
        left.expire_all()
        current = left.get(AutoLoopDecision, identity)
        assert current.status == "pending_dispatch" and current.content_version == 2
        assert left.scalar(select(AutoLoopAction.state)) == "pending"
        step = left.scalar(select(AutoLoopActionStep))
        assert (step.id, step.remote_request_id) == stable
        assert step.state == "queued" and step.cancelled_at is None and step.ever_sent_at is None
        assert left.scalar(select(func.count()).select_from(AutoLoopAction)) == 1
        assert left.scalar(select(func.count()).select_from(AutoLoopActionStep)) == 1
        assert left.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 1
        assert left.scalar(select(func.count()).select_from(AutoLoopConsumedNeed)) == 0
        assert left.scalar(select(func.count()).select_from(AutoLoopEvent)) == fact_count
        # The rejected key was not bound; explicit confirmation with the freshly
        # read token can safely cancel the still-unattempted action using that key.
        _handled(left, owner, identity, expected_content_version=current.content_version)
        left.commit()
        assert current.status == "handled" and current.content_version == 3
        left.refresh(step)
        assert step.cancelled_at is not None and step.ever_sent_at is None
        assert left.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 2
        assert left.scalar(select(func.count()).select_from(AutoLoopConsumedNeed)) == 1


@pytest.mark.parametrize("winner", ["handled", "worker"])
def test_handled_and_worker_claim_are_mutually_exclusive(pg_engine, owner, winner):
    with Session(pg_engine) as left:
        row, _ = _card(left, owner)
        identity = row.id
        _decide(left, owner, identity)
        step_id = left.scalar(select(AutoLoopActionStep.id))
        left.commit()

        if winner == "handled":
            _handled(left, owner, identity, expected_content_version=row.content_version)
            operation = lambda db: store.claim_step(db, owner_id=owner, step_id=step_id)
        else:
            assert store.claim_step(left, owner_id=owner, step_id=step_id)
            operation = lambda db: _handled(db, owner, identity)

        with _contender(left, operation) as future:
            left.commit()
            result = future.result(timeout=10)
            if winner == "handled":
                assert result is None
            else:
                assert isinstance(result, store.StoreConflict) and result.http_status == 409
        left.expire_all()
        decision, step = left.get(AutoLoopDecision, identity), left.get(AutoLoopActionStep, step_id)
        if winner == "handled":
            assert decision.status == "handled" and step.cancelled_at and not step.ever_sent_at
        else:
            assert decision.status == step.state == "sending" and step.ever_sent_at and not step.cancelled_at


@pytest.mark.parametrize("terminal", ["handled", "withdrawn", "completed"])
@pytest.mark.parametrize("winner", ["update", "close"])
def test_accepted_need_update_and_terminal_close_serialize(pg_engine, owner, terminal, winner):
    with Session(pg_engine) as left:
        row, _ = _card(left, owner)
        identity = row.id
        left.commit()

        def update_question(db):
            updated, created = _card(db, owner, need_key="need-b", question="Question B?",
                evidence={"comment": ["b1"]})
            return updated.id if updated else None, created

        def close(db, expected_version):
            if terminal == "handled":
                result, _, _ = _handled(db, owner, identity, expected_content_version=expected_version)
            elif terminal == "withdrawn":
                result = store.withdraw_decision(db, owner_id=owner, decision_id=identity,
                    expected_content_version=expected_version, reason="external_followup",
                    evidence={"comment": ["b1", "member-comment"]})
            else:
                result, _, _ = store.submit_decision(db, owner_id=owner, decision_id=identity,
                    actor_id=owner, expected_content_version=expected_version,
                    idempotency_key="decide-a", option_id="continue")
                for step in list(db.scalars(select(AutoLoopActionStep))):
                    claimed = store.claim_step(db, owner_id=owner, step_id=step.id)
                    assert store.finish_step(db, owner_id=owner, step_id=step.id,
                        lease_token=claimed.lease_token, outcome="confirmed", remote_object_id="saved-" + step.id)
            return result.status

        if winner == "update":
            assert update_question(left) == (identity, False)
            count = left.scalar(select(func.count()).select_from(AutoLoopEvent))
            with _contender(left, lambda db: close(db, 1)) as future:
                left.commit()
                conflict = future.result(timeout=10)
                assert isinstance(conflict, store.StoreConflict)
                assert conflict.http_status == 412 and conflict.code == "content_version_conflict"
                assert conflict.current["status"] == "open" and conflict.current["contentVersion"] == 2
            left.expire_all()
            current = left.get(AutoLoopDecision, identity)
            assert (current.need_key, current.question, current.content_version) == ("need-b", "Question B?", 2)
            assert left.scalar(select(func.count()).select_from(AutoLoopEvent)) == count
            assert left.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == 0
            assert left.scalar(select(func.count()).select_from(AutoLoopConsumedNeed)) == 0
            assert left.scalar(select(func.count()).select_from(AutoLoopAction)) == 0
            assert left.scalar(select(func.count()).select_from(AutoLoopActionStep)) == 0
            assert close(left, 2) == terminal
            left.commit()
        else:
            # Both identities are already accepted. Once close wins, a concurrent
            # replay of B cannot append a snapshot or open a replacement card.
            assert update_question(left) == (identity, False)
            left.commit()
            assert close(left, 2) == terminal
            count = left.scalar(select(func.count()).select_from(AutoLoopEvent))
            with _contender(left, update_question) as future:
                left.commit()
                assert future.result(timeout=10) == (None, False)
            left.expire_all()
            assert left.scalar(select(func.count()).select_from(AutoLoopEvent)) == count
            assert left.scalar(select(func.count()).select_from(AutoLoopDecision)) == 1

        current = left.get(AutoLoopDecision, identity, populate_existing=True)
        assert current.status == terminal
        consumed = {item.need_key: item for item in left.scalars(select(AutoLoopConsumedNeed))}
        assert set(consumed) == {"need-a", "need-b"}
        assert consumed["need-a"].evidence_watermark == {"comment": ["c1"]}
        assert consumed["need-b"].evidence_watermark == {
            "comment": ["b1", "member-comment"] if terminal == "withdrawn" else ["b1"]}
        assert all(item.decision_item_id == identity and item.reason == terminal for item in consumed.values())
        assert left.scalar(select(func.count()).select_from(AutoLoopDecisionRequest)) == (terminal != "withdrawn")
        assert left.scalar(select(func.count()).select_from(AutoLoopAction)) == (terminal == "completed")
        assert left.scalar(select(func.count()).select_from(AutoLoopActionStep)) == (terminal == "completed")
        count = left.scalar(select(func.count()).select_from(AutoLoopEvent))
        assert _card(left, owner) == (None, False)
        assert update_question(left) == (None, False)
        assert left.scalar(select(func.count()).select_from(AutoLoopEvent)) == count
        assert _card(left, owner, need_key="need-c", evidence={"comment": ["b1"]})[1]
        assert left.scalar(select(func.count()).select_from(AutoLoopDecision).where(
            AutoLoopDecision.status.in_(UNFINISHED))) == 1


def test_two_workers_claim_one_step_once(pg_engine, owner):
    with Session(pg_engine) as left:
        row, _ = _card(left, owner)
        _decide(left, owner, row.id)
        step_id = left.scalar(select(AutoLoopActionStep.id))
        left.commit()
        claimed = store.claim_step(left, owner_id=owner, step_id=step_id)
        lease_token = claimed.lease_token
        with _contender(left, lambda db: store.claim_step(db, owner_id=owner, step_id=step_id)) as future:
            left.commit()
            assert future.result(timeout=10) is None
        left.expire_all()
        step = left.get(AutoLoopActionStep, step_id)
        assert step.attempts == 1 and step.lease_token == lease_token
