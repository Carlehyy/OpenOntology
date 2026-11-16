"""Notification admission is atomic and owner-bound on SQLite and real PostgreSQL."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.notifications import service
from app.notifications.models import (
    NotificationAttachment, NotificationChannel, NotificationDelivery,
    NotificationIngestKey, NotificationMessage, NotificationMessageState,
)
from app.shared.database import Base
from app.super_assistant.autoloop import ingress
from app.super_assistant.autoloop.models import AutoLoopDispatch, AutoLoopEvent, AutoLoopReceipt


_NOTIFICATION_TABLES = [model.__table__ for model in (
    NotificationIngestKey, NotificationMessage, NotificationMessageState,
    NotificationAttachment, NotificationChannel, NotificationDelivery,
)]


@pytest.fixture(params=["sqlite", "postgresql"])
def ingress_sessions(request, db):
    engine = db.get_bind() if request.param == "sqlite" else request.getfixturevalue("pg_engine")
    Base.metadata.create_all(engine, tables=_NOTIFICATION_TABLES)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _owner(db, *, active=True):
    identity = str(uuid4())
    db.add(User(id=identity, username=identity, email=f"{identity}@test.invalid",
                password_hash="fixture-no-login", is_active=active))
    db.flush()
    return identity


def _key(db, owner, *, source="trusted-source"):
    identity = str(uuid4())
    plaintext = f"fixture-ingest-{identity}"
    row = NotificationIngestKey(id=identity, name="fixture-source", key_prefix="fixture",
        key_hash=service.hash_key(plaintext), allowed_source_system=source, created_by=owner)
    db.add(row)
    db.commit()
    verified = service.verify_ingest_key(db, plaintext)
    assert verified is not None and verified.id == identity
    return verified


def _channel(db):
    channel = NotificationChannel(name="fixture-channel", apprise_url_encrypted="fixture-no-send")
    db.add(channel)
    db.commit()
    return channel.id


def _counts(db):
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in (
        NotificationMessage, NotificationDelivery, AutoLoopReceipt, AutoLoopEvent, AutoLoopDispatch,
    ))


def test_ingest_commit_contains_fanout_receipt_event_and_dispatch(ingress_sessions):
    with ingress_sessions() as db:
        owner = _owner(db)
        key = _key(db, owner)
        _channel(db)
        response = service.ingest_message(db, {"title": "notice", "eventId": "event-1"}, key)
        assert response["idempotent"] is False
    # A new session models restart after HTTP commit, before any NATS publication.
    with ingress_sessions() as restarted:
        assert _counts(restarted) == (1, 1, 1, 1, 1)
        message = restarted.scalar(select(NotificationMessage))
        receipt = restarted.scalar(select(AutoLoopReceipt))
        fact = restarted.scalar(select(AutoLoopEvent))
        dispatch = restarted.scalar(select(AutoLoopDispatch))
        assert receipt.owner_id == fact.owner_id == dispatch.owner_id == owner
        assert receipt.ingest_key_id == key.id
        assert receipt.payload["notificationId"] == message.id == response["message"]["id"]
        assert receipt.payload["allowedSourceSystem"] == "trusted-source"
        assert receipt.source_event_id == "event:event-1"
        assert dispatch.state == "pending" and dispatch.event_id == fact.id


def test_alternate_key_replay_preserves_original_key_owner_and_content(ingress_sessions):
    with ingress_sessions() as db:
        original_owner, replay_owner = _owner(db), _owner(db)
        original_key = _key(db, original_owner)
        replay_key = _key(db, replay_owner)
        _channel(db)
        first = service.ingest_message(db, {"title": "original", "eventId": "same"}, original_key)
        replay = service.ingest_message(db, {"title": "changed", "eventId": "same",
            "ownerId": replay_owner, "createdBy": replay_owner, "ingestKeyId": replay_key.id}, replay_key)
        assert replay["idempotent"] is True
        assert replay["message"]["id"] == first["message"]["id"]
        assert _counts(db) == (1, 1, 1, 1, 1)
        receipt = db.scalar(select(AutoLoopReceipt))
        assert receipt.owner_id == original_owner and receipt.ingest_key_id == original_key.id
        assert receipt.payload["title"] == "original"
        assert receipt.payload["createdBy"] == original_owner


@pytest.mark.parametrize("owner_state", ["missing", "inactive", "unbound", "missing_key"])
def test_invalid_owner_is_visible_without_borrowing_another_identity(ingress_sessions, owner_state):
    with ingress_sessions() as db:
        fallback_owner = _owner(db)
        owner = (_owner(db, active=False) if owner_state == "inactive"
                 else str(uuid4()) if owner_state == "missing" else None)
        key = _key(db, owner)
        if owner_state == "missing_key":
            # An orphaned historical message is repaired without choosing the replay key.
            original_key_id = str(uuid4())
            message = NotificationMessage(title="legacy", body_md="", source_type="ingest",
                source_system="trusted-source", event_id="orphan", ingest_key_id=original_key_id)
            db.add(message)
            db.commit()
            replay_key = _key(db, fallback_owner)
            service.ingest_message(db, {"title": "replay", "eventId": "orphan"}, replay_key)
        else:
            service.ingest_message(db, {"title": "notice", "ownerId": fallback_owner}, key)
        receipt = db.scalar(select(AutoLoopReceipt))
        fact = db.scalar(select(AutoLoopEvent))
        dispatch = db.scalar(select(AutoLoopDispatch))
        assert receipt.owner_id is None and fact.owner_id is None and dispatch.owner_id is None
        assert fact.action == dispatch.state == "awaiting_owner"
        assert db.scalar(select(func.count()).select_from(AutoLoopReceipt)) == 1


def test_persisted_owner_activity_is_reloaded_before_admission(ingress_sessions):
    with ingress_sessions() as db:
        owner_id = _owner(db)
        key = _key(db, owner_id)
        cached_owner = db.get(User, owner_id)
        assert cached_owner.is_active
        with ingress_sessions() as other:
            other.execute(update(User).where(User.id == owner_id).values(is_active=False))
            other.commit()
        service.ingest_message(db, {"title": "notice"}, key)
        assert db.scalar(select(AutoLoopDispatch)).state == "awaiting_owner"


def test_unrestricted_claimed_source_and_issue_fields_are_untrusted(ingress_sessions):
    with ingress_sessions() as db:
        owner, forged_owner = _owner(db), _owner(db)
        key = _key(db, owner, source=None)
        service.ingest_message(db, {"title": "notice", "sourceSystem": "multica",
            "ownerId": forged_owner, "issueId": "forged-issue", "issueKey": "FAKE-1",
            "workspaceId": "forged-workspace", "pat": "fixture-not-a-credential"}, key)
        receipt = db.scalar(select(AutoLoopReceipt))
        assert receipt.owner_id == owner
        assert receipt.source_system == "multica"  # A weak textual filter, never identity proof.
        assert receipt.payload["allowedSourceSystem"] is None
        assert not {"ownerId", "issueId", "issueKey", "workspaceId", "pat"} & receipt.payload.keys()


@pytest.mark.parametrize("failure_at", ["fanout", "admission", "commit"])
def test_ingest_failure_rolls_back_message_fanout_and_all_autoloop_facts(
    ingress_sessions, monkeypatch, failure_at,
):
    with ingress_sessions() as db:
        key = _key(db, _owner(db))
        _channel(db)
        key_id = key.id
        original = ingress.accept_notification if failure_at == "admission" else service.fan_out_deliveries

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected admission transaction interruption")

        if failure_at == "admission":
            monkeypatch.setattr(ingress, "accept_notification", interrupted)
        elif failure_at == "fanout":
            monkeypatch.setattr(service, "fan_out_deliveries", interrupted)
        else:
            def fail_commit(_session):
                raise RuntimeError("injected admission transaction interruption")
            event.listen(db, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="transaction interruption"):
            service.ingest_message(db, {"title": "notice", "eventId": "retry"}, key)
        assert not db.in_transaction()
    with ingress_sessions() as restarted:
        assert _counts(restarted) == (0, 0, 0, 0, 0)
    monkeypatch.undo()
    with ingress_sessions() as restarted:
        key = restarted.get(NotificationIngestKey, key_id)
        retry = service.ingest_message(restarted, {"title": "notice", "eventId": "retry"}, key)
        assert retry["idempotent"] is False
        assert _counts(restarted) == (1, 1, 1, 1, 1)


def test_legacy_replay_repairs_admission_atomically_and_retains_original_fanout(
    ingress_sessions, monkeypatch,
):
    with ingress_sessions() as db:
        key = _key(db, _owner(db))
        _channel(db)
        message = NotificationMessage(title="historical", body_md="", source_type="ingest",
            source_system="trusted-source", event_id="legacy", ingest_key_id=key.id)
        db.add(message)
        db.flush()
        service.fan_out_deliveries(db, message)
        db.commit()
        key_id = key.id
        original = ingress.accept_notification

        def interrupted(*args):
            original(*args)
            raise RuntimeError("replay interrupted")

        monkeypatch.setattr(ingress, "accept_notification", interrupted)
        with pytest.raises(RuntimeError, match="replay interrupted"):
            service.ingest_message(db, {"title": "replay", "eventId": "legacy"}, key)
        assert not db.in_transaction()
    with ingress_sessions() as restarted:
        assert _counts(restarted) == (1, 1, 0, 0, 0)
        monkeypatch.setattr(ingress, "accept_notification", original)
        key = restarted.get(NotificationIngestKey, key_id)
        assert service.ingest_message(restarted, {"title": "replay", "eventId": "legacy"}, key)["idempotent"]
        assert _counts(restarted) == (1, 1, 1, 1, 1)


def test_rejected_source_and_other_key_attachment_authorization_are_preserved(ingress_sessions):
    with ingress_sessions() as db:
        owner = _owner(db)
        key, other = _key(db, owner), _key(db, owner)
        with pytest.raises(HTTPException) as rejection:
            service.ingest_message(db, {"title": "notice", "sourceSystem": "forged"}, key)
        assert rejection.value.status_code == 403
        assert _counts(db) == (0, 0, 0, 0, 0)
        response = service.ingest_message(db, {"title": "notice"}, key)
        message_id = response["message"]["id"]
        assert service.require_ingest_message(db, message_id, key).id == message_id
        with pytest.raises(HTTPException) as rejection:
            service.require_ingest_message(db, message_id, other)
        assert rejection.value.status_code == 403


def test_manual_and_internal_messages_do_not_admit_external_work(ingress_sessions):
    with ingress_sessions() as db:
        service.create_message(db, title="manual")
        service.publish_notification(db, title="internal", event_id="internal")
        assert _counts(db) == (2, 0, 0, 0, 0)


def test_postgresql_concurrent_ingest_replay_has_one_atomic_identity(pg_engine):
    Base.metadata.create_all(pg_engine, tables=_NOTIFICATION_TABLES)
    sessions = sessionmaker(bind=pg_engine, expire_on_commit=False)
    with sessions() as db:
        key = _key(db, _owner(db))
        _channel(db)
        key_id = key.id
    barrier = Barrier(2)

    def ingest():
        with sessions() as db:
            key = db.get(NotificationIngestKey, key_id)
            barrier.wait(timeout=10)
            return service.ingest_message(db, {"title": "concurrent", "eventId": "race"}, key)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(ingest) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert len({result["message"]["id"] for result in results}) == 1
    with sessions() as db:
        assert _counts(db) == (1, 1, 1, 1, 1)


def test_thread_metadata_is_atomic_and_replay_keeps_original_thread(ingress_sessions):
    from app.super_assistant.autoloop import routing

    with ingress_sessions() as db:
        owner = _owner(db)
        key = _key(db, owner)
        first = service.ingest_message(db, {"title": "first", "eventId": "thread-event",
            "threadId": "thread-a"}, key)
        replay = service.ingest_message(db, {"title": "replay", "eventId": "thread-event",
            "threadId": "thread-b"}, key)
        assert replay["idempotent"] and replay["message"]["id"] == first["message"]["id"]
        receipt = db.scalar(select(AutoLoopReceipt))
        event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.receipt_id == receipt.id))
        assert receipt.payload["threadId"] == "thread-a"
        assert routing.source_context(db, event, receipt) == ("key:" + key.id, "thread-a")
        for invalid in (123, "x" * 513):
            with pytest.raises(HTTPException) as rejection:
                service.ingest_message(db, {"title": "invalid", "threadId": invalid}, key)
            assert rejection.value.status_code == 422
        assert db.scalar(select(func.count()).select_from(AutoLoopReceipt)) == 1
