"""NATS executor task body for a user-authorized integrated snapshot sync.

Dispatch wiring remains in the integrated router/task registry. This module
starts no scheduler/consumer and does not revive the retired sync engine.
"""
from __future__ import annotations

from datetime import datetime, timezone


class IntegratedLeaseActive(RuntimeError):
    """A live owner still holds the run; the delivery must be retried later.

    Raising this makes the executor NAK with ``retry_after`` instead of ACKing
    (which would strand a crashed owner) or taking over a live worker.
    """

    def __init__(self, run_id: str, retry_after: float):
        super().__init__(
            f"Integrated run {run_id} is still owned by a live worker lease")
        self.run_id = run_id
        self.retry_after = retry_after


def _receipt(run, *, error: str | None = None, recovery: dict | None = None) -> dict:
    """Return a durable run receipt suitable for an ACKed duplicate delivery."""
    # Keep this task-body contract independent of a worker's in-memory exception.
    # The router's public DTO adds the same fields, but a task must be useful when
    # called from the executor without importing the HTTP service module.
    receipt = {
        "run_id": run.id,
        "asset_id": run.asset_id,
        "dataset_id": run.dataset_id,
        "status": run.status,
        "complete": run.status == "sealed" and run.version_id is not None,
        "row_count": run.row_count,
        "expected_rows": run.expected_rows,
        "version_id": run.version_id,
        "error": error if error is not None else run.error,
        "checksum": run.checksum,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "started_at": run.snapshot_json.get("started_at"),
        "completed_at": run.completed_at,
    }
    if recovery is not None:
        receipt["recovery"] = recovery
        receipt["retry_required"] = bool(recovery.get("restart_required"))
    return receipt


def _duplicate_delivery(db, run) -> dict:
    """Resolve a redelivery for an in-progress run without blind takeover.

    Only an expired owner lease is provable death.  A live lease is a NAK (the
    owner may still be writing); a missing lease is unverifiable and is left
    untouched with an explicit receipt.  A recovered run becomes a durable
    ``failed`` receipt that requires a fresh full snapshot.
    """
    from app.data_channel.datasets import integrated_store as store
    from app.data_channel.datasets.models import IntegratedSyncRun

    lease = store.lease_of(run)
    if lease is None:
        return _receipt(
            run,
            error=(
                "Integrated run is in progress but has no verifiable owner lease; "
                "left untouched for explicit operator recovery"),
        )
    expires = store.lease_expires_at(run)
    now = datetime.now(timezone.utc)
    if expires is not None and expires > now:
        # Keep the wakeup alive without touching the run: once the owner either
        # finishes or its lease really expires, this delivery resolves.
        raise IntegratedLeaseActive(
            run.id, max(1.0, (expires - now).total_seconds() + 1.0))
    try:
        receipt = store.recover_abandoned_snapshot(
            db, run.id, operator="integrated.sync.redelivery",
            reason="owner lease expired before the message could be acknowledged",
            now=now,
        )
    except store.IntegratedVersionError:
        # Another owner may have sealed/failed/recovered concurrently. Re-read
        # the durable outcome; anything still in progress must be redelivered.
        db.rollback()
        current = db.get(IntegratedSyncRun, run.id)
        if current is not None and current.status in {"sealed", "failed"}:
            return _receipt(current, recovery=store.recovery_receipt(db, current.id))
        raise
    current = db.get(type(run), run.id)
    if current is None:
        return receipt
    return _receipt(current, recovery=receipt)


def integrated_sync_task(asset_id: str, actor_id: str, batch_size: int = 1000,
                         run_id: str | None = None) -> dict:
    """Execute one exact persisted run and ACK durable duplicate outcomes.

    A JetStream redelivery is not permission to resume a private snapshot.  Only
    ``queued`` is eligible for the atomic claim in ``sync_integrated_asset``;
    ``sealed``/``failed`` return their durable receipt, ``starting``/``loading``
    either re-arm a live owner's delivery or recover an expired one.  A business
    exception is ACK-safe only after the same run is durably ``failed``;
    otherwise it is re-raised so the executor can redeliver it.
    """
    from app.database import SessionLocal
    from app.auth.models import User
    from app.data_channel.datasets.models import IntegratedSyncRun
    from app.data_channel.sync_tasks.integrated_sync import IntegratedRunClaimed, sync_integrated_asset

    if run_id is None:
        # Integrated sync is intentionally run-id based.  Do not manufacture a
        # run from a wakeup that omitted identity; malformed messages are ACKed by
        # the NATS handler and this direct entry point reports the same rejection.
        return {"run_id": None, "asset_id": asset_id, "status": "rejected",
                "complete": False, "error": "Integrated sync requires run_id"}

    with SessionLocal() as db:
        run = db.get(IntegratedSyncRun, run_id)
        if run is None:
            # There is no durable outcome to retry against; this is a poison or
            # stale wakeup, not a source failure. ACK it without creating work.
            return {"run_id": run_id, "asset_id": asset_id, "status": "rejected",
                    "complete": False, "error": "Integrated sync run not found"}
        expected = run.snapshot_json or {}
        if (run.asset_id != asset_id
                or expected.get("actor_id") != actor_id
                or expected.get("batch_size") != batch_size):
            # Never mutate the actual run when a message carries another run's
            # identity; returning its current receipt makes the mismatch ACKable.
            return _receipt(run, error="Integrated sync wakeup does not match persisted run")

        if run.status in {"starting", "loading"}:
            return _duplicate_delivery(db, run)

        if run.status in {"sealed", "failed"}:
            # A duplicate delivery must not claim or clean another worker's run.
            recovery = None
            if run.status == "failed":
                from app.data_channel.datasets import integrated_store as store
                prior = store.recovery_receipt(db, run.id)
                if prior is not None:
                    # A repeat delivery must return the identical durable
                    # receipt without repeating the cleanup.
                    recovery = {**prior, "idempotent": True}
            return _receipt(run, recovery=recovery)

        if run.status != "queued":
            return _receipt(run, error="Integrated sync run has an unrecognized status")

        user = db.get(User, actor_id)
        try:
            result = sync_integrated_asset(
                db, asset_id, user=user, batch_size=batch_size, run_id=run_id,
                **({"actor_id": actor_id} if user is None else {}))
        except IntegratedRunClaimed:
            # This exception is raised only after a successful CAS commit proved
            # another delivery owns the run, not for unconfirmed DB failures.
            db.rollback()
            current = db.get(IntegratedSyncRun, run_id)
            if current is not None and current.status in {"starting", "loading", "sealed", "failed"}:
                return _receipt(current)
            raise
        except Exception:
            # sync_integrated_asset rolls back and best-effort marks this exact
            # run failed. Re-read after its transaction boundary: only a durable
            # failed receipt is safe to ACK; loading/starting means DB cleanup or
            # infrastructure confirmation was unavailable, so NAK via re-raise.
            db.rollback()
            current = db.get(IntegratedSyncRun, run_id)
            if current is not None and current.status == "failed":
                from app.data_channel.datasets import integrated_store as store
                return _receipt(current, recovery=store.recovery_receipt(db, current.id))
            raise
        return result
