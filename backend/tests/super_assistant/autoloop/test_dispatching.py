"""Durable routing admission, unknown publication and redelivery recovery."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.super_assistant.autoloop import dispatching, store
from app.super_assistant.autoloop.models import (
    AutoLoopDispatch, AutoLoopEvent, AutoLoopReceipt, utcnow,
)


@pytest.fixture(autouse=True)
def unavailable_router(monkeypatch):
    # These reliability cases deliberately exercise an unavailable business router.
    monkeypatch.setattr(dispatching, "business_router", None)


@pytest.fixture(params=["sqlite", "postgresql"])
def dispatch_sessions(request, db):
    engine = db.get_bind() if request.param == "sqlite" else request.getfixturevalue("pg_engine")
    return sessionmaker(bind=engine, expire_on_commit=False)


def _admit(sessions, *, owner=True):
    with sessions() as db:
        owner_id = str(uuid4()) if owner else None
        if owner_id:
            db.add(User(id=owner_id, username=owner_id, email=f"{owner_id}@test.invalid",
                        password_hash="fixture-no-login", is_active=True))
            db.flush()
        receipt, event, _ = store.accept_receipt(db, source_kind="schedule",
            source_system="super_assistant", source_event_id=str(uuid4()),
            owner_id=owner_id, payload={"instruction": "fixture"})
        dispatch = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.event_id == event.id))
        db.commit()
        return receipt.id, event.id, dispatch.id, dispatch.version


def test_committed_admission_survives_restart_and_unknown_publisher_confirmation(dispatch_sessions):
    receipt_id, event_id, identity, version = _admit(dispatch_sessions)
    publications = []

    def accepted_but_timed_out(dispatch_id, dispatch_version):
        publications.append((dispatch_id, dispatch_version))
        raise TimeoutError("broker acceptance unknown")

    assert dispatching.scan_pending(session_factory=dispatch_sessions, publish=accepted_but_timed_out) == 0
    assert dispatching.scan_pending(session_factory=dispatch_sessions,
        publish=lambda *args: publications.append(args)) == 1
    assert publications == [(identity, version), (identity, version)]
    with dispatch_sessions() as restarted:
        work = restarted.get(AutoLoopDispatch, identity)
        assert work.event_id == event_id and work.state == "pending"
        assert work.version == version and work.attempts == 0
        assert restarted.scalar(select(func.count()).select_from(AutoLoopReceipt)) == 1
        assert restarted.get(AutoLoopReceipt, receipt_id) is not None


def test_missing_router_is_durably_deferred_and_duplicate_delivery_cannot_ack_as_success(dispatch_sessions):
    receipt_id, event_id, identity, _ = _admit(dispatch_sessions)
    with pytest.raises(dispatching.RoutingDeferred, match="awaiting_business_router"):
        dispatching.process_dispatch(identity, session_factory=dispatch_sessions)
    with dispatch_sessions() as restarted:
        work = restarted.get(AutoLoopDispatch, identity)
        assert work.state == "pending" and work.error == "awaiting_business_router"
        assert work.lease_token is None and work.lease_until is None
        available_at = work.available_at
        if available_at.tzinfo is None:  # SQLite DateTime does not retain tzinfo.
            available_at = available_at.replace(tzinfo=timezone.utc)
        assert available_at > utcnow() and work.attempts == 1
        version = work.version
        deferred = restarted.scalar(select(AutoLoopEvent).where(AutoLoopEvent.type == "routing.deferred"))
        assert deferred.receipt_id == receipt_id and deferred.parent_event_id == event_id
        assert deferred.action == "pending" and deferred.data["dispatchId"] == identity
    with pytest.raises(dispatching.RoutingDeferred, match="not due"):
        dispatching.process_dispatch(identity, session_factory=dispatch_sessions)
    with dispatch_sessions() as restarted:
        work = restarted.get(AutoLoopDispatch, identity)
        assert work.version == version and work.attempts == 1
        assert restarted.scalar(select(func.count()).select_from(AutoLoopEvent).where(
            AutoLoopEvent.type == "routing.deferred")) == 1


def test_crash_after_claim_recovers_same_work_and_rejects_stale_lease_completion(dispatch_sessions):
    _, event_id, identity, _ = _admit(dispatch_sessions)
    with dispatch_sessions() as crashed:
        claimed = store.claim_dispatch(crashed, identity, lease_seconds=-1)
        old_token = claimed.lease_token
        crashed.commit()
    sent = []
    assert dispatching.scan_pending(session_factory=dispatch_sessions,
        publish=lambda *args: sent.append(args)) == 1
    assert sent[0][0] == identity
    with dispatch_sessions() as restarted:
        assert restarted.get(AutoLoopDispatch, identity).event_id == event_id
        assert not store.finish_dispatch(restarted, identity, old_token)
        restarted.commit()
    with pytest.raises(dispatching.RoutingDeferred):
        dispatching.process_dispatch(identity, session_factory=dispatch_sessions)
    with dispatch_sessions() as restarted:
        assert restarted.get(AutoLoopDispatch, identity).attempts == 2
        assert restarted.scalar(select(func.count()).select_from(AutoLoopReceipt)) == 1
        assert restarted.scalar(select(func.count()).select_from(AutoLoopDispatch)) == 1
        assert restarted.scalar(select(func.count()).select_from(AutoLoopEvent).where(
            AutoLoopEvent.type == "dispatch.recovered")) == 1


def test_in_flight_lease_is_not_stolen_and_ownerless_work_is_visible(dispatch_sessions):
    _, _, identity, _ = _admit(dispatch_sessions)
    _, _, ownerless, _ = _admit(dispatch_sessions, owner=False)
    with dispatch_sessions() as running:
        claim = store.claim_dispatch(running, identity)
        token, version = claim.lease_token, claim.version
        running.commit()
    with pytest.raises(dispatching.RoutingDeferred, match="leased"):
        dispatching.process_dispatch(identity, session_factory=dispatch_sessions)
    dispatching.process_dispatch(ownerless, session_factory=dispatch_sessions)
    sent = []
    assert dispatching.scan_pending(session_factory=dispatch_sessions,
        publish=lambda *args: sent.append(args)) == 0
    with dispatch_sessions() as restarted:
        assert restarted.get(AutoLoopDispatch, identity).lease_token == token
        assert restarted.get(AutoLoopDispatch, identity).version == version
        assert restarted.get(AutoLoopDispatch, ownerless).state == "awaiting_owner"


@pytest.mark.parametrize("terminal", ["done", "failed"])
def test_duplicate_terminal_delivery_reads_durable_outcome(dispatch_sessions, terminal):
    _, _, identity, _ = _admit(dispatch_sessions)
    with dispatch_sessions() as db:
        claim = store.claim_dispatch(db, identity)
        assert store.finish_dispatch(db, identity, claim.lease_token,
            error="fixture terminal failure" if terminal == "failed" else None)
        version = db.get(AutoLoopDispatch, identity).version
        db.commit()
    dispatching.process_dispatch(identity, session_factory=dispatch_sessions)
    with dispatch_sessions() as restarted:
        work = restarted.get(AutoLoopDispatch, identity)
        assert work.state == terminal and work.version == version and work.attempts == 1


def test_real_postgresql_nats_recovers_worker_crash_after_committed_claim(
    pg_engine, pg_schema, monkeypatch, tmp_path,
):
    """A killed worker leaves an unacked delivery and an expired durable lease."""
    from app.data_channel.pipeline_tasks import dispatch, nats_executor
    from tests.data_channel.test_pipeline_nats_executor import (
        _autoloop_subscription, _consumer_drained, _owned_consumer,
        _owned_pipeline_stream, _record_nats_resource, _reserved_nats_url,
    )
    from tests.super_assistant.autoloop._nats_crash_worker import CRASH_EXIT

    sessions = sessionmaker(bind=pg_engine, expire_on_commit=False)
    receipt_id, event_id, identity, version = _admit(sessions)
    monkeypatch.setattr("app.config.settings.nats_url", _reserved_nats_url())
    monkeypatch.setattr(dispatching, "SessionLocal", sessions)
    monkeypatch.setattr(dispatching.RoutingDeferred, "retry_after", 0.1)
    _, scoped_url, schema = pg_schema
    subject, durable, handler = _autoloop_subscription()
    witness = tmp_path / "committed-claim.json"

    async def recover():
        async with _owned_pipeline_stream() as (nc, js):
            subscription = await _owned_consumer(js, subject, durable)
            await asyncio.to_thread(dispatch.dispatch_autoloop, identity, version)
            env = os.environ.copy()
            env.update({
                "AUTOLOOP_CRASH_DATABASE_URL": scoped_url.render_as_string(hide_password=False),
                "AUTOLOOP_CRASH_SUBJECT": subject, "AUTOLOOP_CRASH_DURABLE": durable,
                "AUTOLOOP_CRASH_WITNESS": str(witness),
            })
            process = subprocess.Popen([sys.executable, "-m",
                "tests.super_assistant.autoloop._nats_crash_worker"],
                cwd=Path(__file__).resolve().parents[3], env=env)
            _record_nats_resource("worker_started", pid=process.pid,
                                  stream=dispatch.PIPELINE_STREAM, consumer=durable,
                                  schema=schema)
            try:
                code = await asyncio.to_thread(process.wait, 45)
            except subprocess.TimeoutExpired:
                process.kill()
                code = await asyncio.to_thread(process.wait, 10)
                _record_nats_resource("worker_timeout_terminated", pid=process.pid,
                                      exitCode=code)
                pytest.fail("fault worker did not reach its committed crash point")
            _record_nats_resource("worker_exited", pid=process.pid, exitCode=code)
            assert code == CRASH_EXIT
            committed = json.loads(witness.read_text(encoding="utf-8"))
            assert committed["dispatchId"] == identity
            with sessions() as fresh:
                work = fresh.get(AutoLoopDispatch, identity)
                assert work.state == "claimed" and work.attempts == 1
                assert work.lease_token == committed["leaseToken"]
                assert fresh.get(AutoLoopReceipt, receipt_id).id == receipt_id
            info = await js.consumer_info(dispatch.PIPELINE_STREAM, durable)
            assert info.num_ack_pending == 1
            await subscription.unsubscribe()
            # Restarted durable re-delivers the original broker sequence.
            subscription = await _owned_consumer(js, subject, durable)
            [redelivered] = await subscription.fetch(1, timeout=5)
            assert redelivered.metadata.sequence.stream == committed["sequence"]
            assert redelivered.metadata.num_delivered >= 2
            assert json.loads(redelivered.data)["dispatch_id"] == identity
            # Restarted scanner recovers only the expired DB lease and republishes
            # the same dispatch (new state version, no replacement work identity).
            assert await asyncio.to_thread(dispatching.scan_pending, session_factory=sessions) == 1
            with sessions() as fresh:
                assert not store.finish_dispatch(fresh, identity, committed["leaseToken"])
                fresh.rollback()
                work = fresh.get(AutoLoopDispatch, identity)
                assert work.state == "pending" and work.event_id == event_id
            await nats_executor.PipelineExecutor()._process_message(redelivered, handler, subject)
            await nc.flush()
            with sessions() as fresh:
                work = fresh.get(AutoLoopDispatch, identity)
                assert work.state == "pending" and work.error == "awaiting_business_router"
                assert work.attempts == 2
                types = list(fresh.scalars(select(AutoLoopEvent.type)))
                assert types.count("dispatch.recovered") == 1
                assert types.count("routing.deferred") == 1
                # Inject a persisted terminal fact to exercise the positive ACK path.
                work.state = "done"
                fresh.commit()
            # Original NAK and recovered scanner wakeup are both safe duplicates.
            messages = []
            for _ in range(2):
                messages.extend(await subscription.fetch(1, timeout=5))
            assert len(messages) == 2
            assert {message.metadata.sequence.stream for message in messages} == {
                committed["sequence"], committed["sequence"] + 1,
            }
            await asyncio.gather(*(nats_executor.PipelineExecutor()._process_message(
                message, handler, subject) for message in messages))
            await nc.flush()
            await _consumer_drained(js, durable)
            with sessions() as fresh:
                assert fresh.get(AutoLoopDispatch, identity).attempts == 2
                assert fresh.scalar(select(func.count()).select_from(AutoLoopReceipt)) == 1

    asyncio.run(recover())


def test_postgresql_concurrent_duplicate_delivery_has_one_deferral(pg_engine):
    sessions = sessionmaker(bind=pg_engine, expire_on_commit=False)
    _, _, identity, _ = _admit(sessions)
    barrier = Barrier(2)

    def consume():
        barrier.wait(timeout=10)
        with pytest.raises(dispatching.RoutingDeferred):
            dispatching.process_dispatch(identity, session_factory=sessions)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(consume) for _ in range(2)]
        for future in futures:
            future.result(timeout=20)
    with sessions() as db:
        assert db.get(AutoLoopDispatch, identity).attempts == 1
        assert db.scalar(select(func.count()).select_from(AutoLoopEvent).where(
            AutoLoopEvent.type == "routing.deferred")) == 1
