"""Complete bounded source snapshots into immutable integrated lake versions.

Crash recovery never resumes a source snapshot.  A run owns its private
relation through a lease stored in ``snapshot_json``; a duplicate delivery or a
bounded reconciliation sweep may reclaim an *expired* lease only, after a
compare-and-set to ``failed`` and an audit receipt.  Every write/seal is fenced
on the same lease so a reclaimed worker can never publish.
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.data_channel.datasets.models import IntegratedSyncRun

from app.data_channel.datasets.integrated_store import (
    INTEGRATED_LEASE_SECONDS, MAX_BATCH_SIZE, IntegratedVersionError,
    begin_snapshot, fail_snapshot, new_owner_lease, recover_abandoned_snapshot,
    recover_expired_snapshots, renew_run_lease, seal_snapshot, snapshot_hasher,
    write_snapshot_batch,
)
from app.data_channel.integrated.native import authorize, open_snapshot

logger = logging.getLogger(__name__)


class IntegratedRunClaimed(IntegratedVersionError):
    """The queued-state compare-and-set definitively lost to another owner."""


class _LeaseHeartbeat:
    """Renew a live owner's lease from a dedicated session while it works."""

    def __init__(self, db, token: str, *, interval: int, lease_seconds: int):
        bind = db.get_bind()
        self._factory = (
            sessionmaker(bind=bind, expire_on_commit=False)
            if isinstance(bind, Engine) else None
        )
        self._token = token
        self._interval = max(int(interval), 1)
        self._lease_seconds = int(lease_seconds)
        self._run_id: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, run_id: str) -> None:
        self._run_id = run_id
        if self._factory is None or self._thread is not None:
            # An in-memory/connection-bound session has no thread-safe session
            # factory; batch commits still refresh the lease before each write.
            return
        self._thread = threading.Thread(
            target=self._renew_until_stopped,
            name=f"integrated-lease-{run_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def _renew_until_stopped(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._factory() as heartbeat_db:
                    renewed = renew_run_lease(
                        heartbeat_db, self._run_id, self._token,
                        seconds=self._lease_seconds)
            except Exception:
                # A heartbeat gap is not an ownership proof for anybody else:
                # recovery additionally requires the stored expiry, and the
                # worker is fenced if it lost the CAS.
                logger.warning(
                    "Integrated run lease heartbeat failed run_id=%s", self._run_id)
                continue
            if not renewed:
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def sync_integrated_asset(db, asset_id: str, *, user, batch_size: int = MAX_BATCH_SIZE,
                          run_id: str | None = None, actor_id: str | None = None,
                          lease_seconds: int = INTEGRATED_LEASE_SECONDS,
                          heartbeat_seconds: int | None = None) -> dict:
    """Sync a complete repeatable-read snapshot; never a sampled/truncated one.

    Authorize using the initiating user, not a worker/service administrator. Each
    call owns its new run; failed/crashed runs are private and a retry starts a
    fresh consistent snapshot. No guessed cursor is used across source sessions.
    The source context must also exit successfully before a version is published.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise IntegratedVersionError("Batch size must be between 1 and 1000")
    if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int)
            or lease_seconds < 1):
        raise IntegratedVersionError("Integrated owner lease duration must be positive")
    if heartbeat_seconds is None:
        heartbeat_seconds = max(lease_seconds // 3, 1)
    if (isinstance(heartbeat_seconds, bool) or not isinstance(heartbeat_seconds, int)
            or heartbeat_seconds < 1):
        raise IntegratedVersionError("Integrated owner heartbeat interval must be positive")
    # Claim before entering the failure-cleanup scope. A duplicate delivery may
    # observe an in-progress run, but must never mark that other worker failed.
    owner_token = str(uuid.uuid4())
    initiating_actor = actor_id if actor_id is not None else getattr(user, "id", None)
    lease = None
    heartbeat = None
    if run_id is not None:
        queued = db.get(IntegratedSyncRun, run_id)
        if (queued is None or queued.asset_id != asset_id
                or queued.snapshot_json.get("actor_id") != initiating_actor
                or (user is not None and user.id != initiating_actor)
                or queued.snapshot_json.get("batch_size") != batch_size):
            raise IntegratedVersionError("Integrated queued run does not match its initiating actor/request")
        if user is None:
            # The worker validated the wakeup actor against this exact durable
            # run, but that actor was deleted. CAS only queued->failed: a stale
            # observer must never drop or fail a relation another owner claimed.
            error = "The initiating actor no longer exists; this run cannot be authorized"
            failed = db.execute(sa.update(IntegratedSyncRun).where(
                IntegratedSyncRun.id == run_id, IntegratedSyncRun.status == "queued"
            ).values(status="failed", error=error, completed_at=datetime.now(timezone.utc))
              .execution_options(synchronize_session=False)).rowcount
            db.commit()
            db.expire(queued)
            if failed != 1:
                raise IntegratedRunClaimed("Integrated run is already claimed; loading runs cannot be resumed")
            raise IntegratedVersionError(error)
        if queued.status == "sealed" and queued.version_id:
            return {"run_id": queued.id, "asset_id": asset_id, "dataset_id": queued.dataset_id,
                    "version_id": queued.version_id, "dataset_version_id": queued.version_id,
                    "status": "sealed", "complete": True, "row_count": queued.row_count,
                    "checksum": queued.checksum}
        lease = new_owner_lease(
            actor_id=initiating_actor, token=owner_token, seconds=lease_seconds)
        heartbeat = _LeaseHeartbeat(
            db, owner_token, interval=heartbeat_seconds, lease_seconds=lease_seconds)
        claimed_snapshot = {**queued.snapshot_json, "lease": lease}
        claimed = db.execute(sa.update(IntegratedSyncRun).where(
            IntegratedSyncRun.id == run_id, IntegratedSyncRun.status == "queued"
        ).values(status="starting", snapshot_json=claimed_snapshot)
          .execution_options(synchronize_session=False)).rowcount
        db.commit()
        if claimed != 1:
            raise IntegratedRunClaimed("Integrated run is already claimed; loading runs cannot be resumed")
        db.expire(queued)
        # Cover the source-open window before the first batch can refresh it.
        heartbeat.start(run_id)
    else:
        lease = new_owner_lease(
            actor_id=initiating_actor, token=owner_token, seconds=lease_seconds)
        heartbeat = _LeaseHeartbeat(
            db, owner_token, interval=heartbeat_seconds, lease_seconds=lease_seconds)
    try:
        # The queued message already proved its actor_id against the durable run.
        # A deleted user is a terminal business outcome for this exact run, not an
        # infrastructure retry: fail the receipt before source access and never
        # create a replacement snapshot.
        if user is None:
            raise IntegratedVersionError(
                "The initiating actor no longer exists; this run cannot be authorized")
        authorize(db, user, write=True)
        with open_snapshot(db, asset_id, user=user, batch_size=batch_size) as source:
            run, gate = begin_snapshot(db, source.descriptor, run_id=run_id, lease=lease)
            run_id = run.id
            heartbeat.start(run.id)
            digest = snapshot_hasher(run)
            for batch in source.iter_batches():
                write_snapshot_batch(db, run, gate, batch, digest, owner_token=owner_token)
            completion = dict(source.completion)
        # A failing source transaction close is still a failure, not EOF proof.
        version = seal_snapshot(db, run, completion, digest.hexdigest(),
                                owner_token=owner_token)
        return {
            "run_id": run.id, "asset_id": asset_id, "dataset_id": run.dataset_id,
            "version_id": version.id, "dataset_version_id": version.id,
            "version_no": version.version_no, "status": "sealed", "complete": True,
            "row_count": run.row_count, "checksum": run.checksum,
            "schema_fingerprint": run.schema_json["schema_fingerprint"],
            "snapshot": run.snapshot_json,
        }
    except Exception:
        db.rollback()
        if run_id is not None:
            try:
                fail_snapshot(db, run_id)
            except Exception:
                # Preserve the original failure, and leave loading private if
                # the platform DB itself is unavailable. Never expose a version.
                db.rollback()
                logger.warning("Integrated failed-run cleanup could not finish run_id=%s", run_id)
        raise
    finally:
        heartbeat.stop()


def _json_receipt(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def main(argv: list[str] | None = None) -> int:
    """Operator entry point: prove owner loss, dispose only that private run.

    Example::

        python -m app.data_channel.sync_tasks.integrated_sync recover \\
            --run-id <uuid> --operator ops:alice --reason "worker SIGKILL proven"
    """
    parser = argparse.ArgumentParser(
        prog="integrated-sync-recovery",
        description="Recover abandoned integrated sync runs without source resume",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    recover = commands.add_parser("recover", help="recover one exact run")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--operator", required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument(
        "--worker-proof", type=json.loads, default=None,
        help='operator evidence JSON: {"kind","reference","observed_at","owner_token"}',
    )
    sweep = commands.add_parser(
        "sweep", help="recover every run whose owner lease already expired")
    sweep.add_argument("--operator", required=True)
    sweep.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)

    from app.model_registry import import_all_models
    import_all_models()
    from app.database import SessionLocal

    with SessionLocal() as db:
        if args.command == "recover":
            receipt = recover_abandoned_snapshot(
                db, args.run_id, operator=args.operator, reason=args.reason,
                proof=args.worker_proof,
            )
            print(_json_receipt(receipt))
            return 0 if receipt.get("recovered") or receipt.get("idempotent") else 2
        receipts = recover_expired_snapshots(
            db, operator=args.operator, limit=args.limit)
        print(_json_receipt(receipts))
        return 0 if all(item.get("recovered") or item.get("idempotent")
                        for item in receipts) else 2


if __name__ == "__main__":
    raise SystemExit(main())
