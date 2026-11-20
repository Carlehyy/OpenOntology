"""Durable routing wakeups with committed claims and frozen rule snapshots."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update

from app.auth.models import User
from app.shared.database import SessionLocal
from app.super_assistant import multica_service
from app.super_assistant.provider import ensure_autoloop_available
from app.super_assistant.autoloop.models import AutoLoopDispatch, AutoLoopEvent, utcnow
from app.super_assistant.autoloop.store import append_event, claim_dispatch, finish_dispatch, recover_dispatches
from app.super_assistant.autoloop.routing import prepare, route_event

# An explicit seam also exercises unavailable-router recovery without disabling
# business routing in normal API/executor processes.
business_router = route_event

logger = logging.getLogger(__name__)
RETRY_SECONDS = 30


class RoutingDeferred(RuntimeError):
    retry_after = RETRY_SECONDS


def scan_pending(*, session_factory=None, publish=None) -> int:
    """Recover leases and publish durable identities without consuming the work.

    A publish timeout leaves the same database work pending. A bounded rescan uses
    its persisted version; duplicate wakeups are harmless under dispatch CAS.
    """
    ensure_autoloop_available()
    if publish is None:
        from app.data_channel.pipeline_tasks.dispatch import dispatch_autoloop

        publish = dispatch_autoloop
    session_factory = session_factory or SessionLocal
    with session_factory() as db:
        recover_dispatches(db)
        db.commit()
        rows = list(db.execute(select(AutoLoopDispatch.id, AutoLoopDispatch.version).where(
            AutoLoopDispatch.state == "pending", AutoLoopDispatch.available_at <= utcnow(),
        ).order_by(AutoLoopDispatch.available_at, AutoLoopDispatch.id).limit(50)))
    sent = 0
    for identity, version in rows:
        try:
            publish(identity, version)
            sent += 1
        except Exception:
            # Broker acceptance may already have happened: never close work or
            # change its identity merely because publisher confirmation failed.
            logger.exception("AutoLoop wakeup not confirmed dispatch=%s", identity)
    return sent


def process_dispatch(dispatch_id: str, *, session_factory=None) -> None:
    ensure_autoloop_available()
    session_factory = session_factory or SessionLocal
    with session_factory() as db:
        row = db.get(AutoLoopDispatch, dispatch_id)
        if row is None:
            raise ValueError("AutoLoop dispatch does not exist")
        if row.state in {"done", "failed", "awaiting_owner"}:
            return
        claimed = claim_dispatch(db, dispatch_id)
        if claimed is None:
            raise RoutingDeferred("AutoLoop work is leased or not due")
        event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == claimed.event_id))
        if event is None:
            raise ValueError("AutoLoop dispatch event does not exist")
        token = claimed.lease_token
        if business_router is None:
            reason = "awaiting_business_router"
            changed = db.execute(update(AutoLoopDispatch).where(
                AutoLoopDispatch.id == dispatch_id, AutoLoopDispatch.state == "claimed",
                AutoLoopDispatch.lease_token == token,
            ).values(state="pending", lease_token=None, lease_until=None,
                available_at=utcnow() + timedelta(seconds=RETRY_SECONDS), error=reason,
                version=AutoLoopDispatch.version + 1, updated_at=utcnow()))
            if changed.rowcount != 1:
                raise RoutingDeferred("AutoLoop dispatch lease changed")
            append_event(db, receipt_id=event.receipt_id, owner_id=event.owner_id,
                type="routing.deferred", source_kind=event.source_kind,
                source_system=event.source_system, source_ref=event.source_ref,
                parent_event_id=event.id, hops=event.hops, action="pending",
                error=reason, data={"dispatchId": dispatch_id, "reason": reason})
            db.commit()
            raise RoutingDeferred(reason)
        prepared = prepare(db, event)
        prepared_id = prepared.id
        db.commit()
    with session_factory() as db:
        # Hold the current lease row while saving the plan; a recovered stale
        # worker cannot append a second plan or change a newly owned dispatch.
        current = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.id == dispatch_id)
                            .with_for_update().execution_options(populate_existing=True))
        if current.state != "claimed" or current.lease_token != token:
            raise RoutingDeferred("AutoLoop dispatch lease changed")
        event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == current.event_id))
        owner = db.get(User, current.owner_id, populate_existing=True) if current.owner_id else None
        if not owner or not owner.is_active:
            current.state, current.error = "awaiting_owner", "owner_inactive"
            current.lease_token = current.lease_until = None
            current.version += 1
            current.updated_at = utcnow()
            append_event(db, receipt_id=event.receipt_id, owner_id=event.owner_id,
                type="routing.awaiting_owner", source_kind=event.source_kind,
                source_system=event.source_system, source_ref=event.source_ref,
                parent_event_id=event.id, hops=event.hops, action="awaiting_owner",
                error="owner_inactive", data={"dispatchId": dispatch_id})
            db.commit()
            return
        # Commit any default-profile write before routing calls a provider.
        # Only drift requires releasing and re-acquiring the dispatch lease row.
        if multica_service.sync_deployment_default(db, owner.id):
            db.commit()
            current = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.id == dispatch_id)
                                .with_for_update().execution_options(populate_existing=True))
            if current is None or current.state != "claimed" or current.lease_token != token:
                raise RoutingDeferred("AutoLoop dispatch lease changed")
            event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == current.event_id))
        prepared = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == prepared_id))
        ensure_autoloop_available()
        business_router(db, event, prepared)
        if not finish_dispatch(db, dispatch_id, token):
            raise RoutingDeferred("AutoLoop dispatch lease changed")
        db.commit()


async def run_dispatch_message(payload: dict) -> None:
    identity = payload.get("dispatch_id")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("AutoLoop message requires dispatch_id")
    await asyncio.to_thread(process_dispatch, identity)
