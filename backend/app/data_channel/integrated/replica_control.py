"""Fenced lifecycle of a current replica; callers own database transactions.

Capture/apply workers supply durable position proofs. Heartbeats alone never
publish a replica or advance its coverage. This module performs no source IO
and never commits, so publication can share a transaction with the final apply.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import IntegratedReplicaCapture
from app.data_channel.integrated.replica_models import IntegratedReplica, IntegratedReplicaRun

LEASE_SECONDS = 90
READABLE_STATUSES = frozenset({"ready", "stale", "initializing", "catching_up"})
TERMINAL_RUN_STATUSES = frozenset({"failed", "cancelled", "retired"})
SUPERSEDED_RUN_CODE = "REPLICA_RUN_SUPERSEDED"


class ReplicaControlError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def _replica(db, asset_id):
    row = db.scalar(sa.select(IntegratedReplica).where(
        IntegratedReplica.asset_id == str(asset_id)).with_for_update()
        .execution_options(populate_existing=True))
    if row is None:
        raise ReplicaControlError("REPLICA_NOT_REGISTERED")
    return row


def ensure_replica(db, asset: IntegratedAsset) -> IntegratedReplica:
    # Registration/reconciliation serializes on the durable asset, including
    # the first insert for which a replica row lock does not yet exist.
    locked = db.scalar(sa.select(IntegratedAsset).where(
        IntegratedAsset.id == asset.id).with_for_update())
    if locked is None:
        raise ReplicaControlError("ASSET_NOT_FOUND")
    row = db.get(IntegratedReplica, asset.id)
    if row is None:
        row = IntegratedReplica(asset_id=asset.id, status="pending", generation=0)
        db.add(row)
        db.flush()
    return row


def _pending_lease_valid(run) -> bool:
    if not run.owner_token or run.lease_expires_at is None:
        return False
    return _utc(run.lease_expires_at) > _now()


def _pending_run_resumable(db, run) -> bool:
    """A candidate can only stay pending while it can still progress.

    A claimed initializer whose owner lease expired has no production recovery
    caller; an explicit rebuild must supersede it. ``blocked``/``gap``/
    ``draining``/``stopped`` (and a missing capture row) cannot apply or accept
    ingest either. A queued (unbound) run or one in a starting/streaming/stale
    capture with a valid owner lease remains idempotent, including a long
    healthy loading/catching_up run.
    """
    if (run.status in {"capturing", "loading", "catching_up"}
            and not _pending_lease_valid(run)):
        return False
    if not run.capture_id:
        return True
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == run.capture_id).with_for_update()
        .execution_options(populate_existing=True))
    if capture is None:
        return False
    return capture.status in {"starting", "streaming", "stale"}


def request_rebuild(db, asset: IntegratedAsset) -> IntegratedReplicaRun:
    # Production sessions disable autoflush. Callers such as
    # abandon_initialization set the old run failed / pending_run_id None just
    # before this call; without flushing, _replica/populate_existing reload the
    # old loading candidate and return it, so no replacement generation is
    # queued. Flush the caller's fenced transition first (still one transaction).
    db.flush()
    ensure_replica(db, asset)
    replica = _replica(db, asset.id)
    manifest = {"columns": copy.deepcopy(asset.schema_json["columns"]),
                "primary_key": copy.deepcopy(asset.schema_json["primary_key"])}
    if replica.pending_run_id:
        existing = db.scalar(sa.select(IntegratedReplicaRun).where(
            IntegratedReplicaRun.id == replica.pending_run_id).with_for_update()
            .execution_options(populate_existing=True))
        if existing is None:
            raise ReplicaControlError("REPLICA_RUN_MISSING")
        if existing.status not in TERMINAL_RUN_STATUSES:
            if existing.status == "active" or existing.id == replica.active_run_id:
                # Corrupt pointer: never cancel published data to satisfy a
                # rebuild request.
                raise ReplicaControlError("REPLICA_ACTIVE_PENDING_CONFLICT")
            matches = (existing.schema_fingerprint == asset.schema_fingerprint
                       and existing.schema_json == manifest)
            if matches and _pending_run_resumable(db, existing):
                # Same fenced candidate in a healthy cohort: repeated dispatch
                # is idempotent (a long loading/catching_up run is not reset).
                return existing
            # Superseded schema, or a capture that can no longer resume:
            # fenced terminalize the obsolete candidate and queue a new
            # generation. The active run and capture member lifecycle are
            # left to their own reconciliation.
            existing.status = "cancelled"
            existing.last_error_code = SUPERSEDED_RUN_CODE
            existing.owner_token = None
            existing.lease_expires_at = None
            existing.completed_at = _now()
            db.flush()
    replica.generation += 1
    run = IntegratedReplicaRun(
        id=str(uuid.uuid4()), asset_id=asset.id, generation=replica.generation,
        status="queued", schema_json=manifest,
        schema_fingerprint=asset.schema_fingerprint, row_count=0)
    db.add(run)
    db.flush()
    replica.pending_run_id = run.id
    # A known gap/schema violation must remain unreadable during rebuilding.
    if replica.status not in {"rebuild_required", "blocked"}:
        replica.status = "initializing"
    replica.last_error_code = None
    db.flush()
    return run


def _owned(db, run_id, token, *, statuses):
    initial = db.get(IntegratedReplicaRun, str(run_id))
    if initial is None:
        raise ReplicaControlError("REPLICA_RUN_MISSING")
    replica = _replica(db, initial.asset_id)
    run = db.scalar(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.id == str(run_id)).with_for_update()
        .execution_options(populate_existing=True))
    if (replica.pending_run_id != run.id or replica.generation != run.generation
            or run.owner_token != token or not token
            or run.lease_expires_at is None or _utc(run.lease_expires_at) <= _now()
            or run.status not in statuses):
        raise ReplicaControlError("REPLICA_OWNER_LOST")
    return replica, run


def claim_run(db, run_id: str) -> str:
    initial = db.get(IntegratedReplicaRun, str(run_id))
    if initial is None:
        raise ReplicaControlError("REPLICA_RUN_MISSING")
    replica = _replica(db, initial.asset_id)
    run = db.scalar(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.id == str(run_id)).with_for_update()
        .execution_options(populate_existing=True))
    if replica.pending_run_id != run.id or replica.generation != run.generation:
        raise ReplicaControlError("REPLICA_RUN_SUPERSEDED")
    if run.status != "queued":
        # A lost snapshot transaction cannot be resumed by a replacement owner.
        raise ReplicaControlError("REPLICA_RUN_ALREADY_CLAIMED")
    token = str(uuid.uuid4())
    run.owner_token = token
    run.lease_expires_at = _now() + timedelta(seconds=LEASE_SECONDS)
    run.started_at = _now()
    run.status = "capturing"
    db.flush()
    return token


def renew_run(db, run_id: str, token: str) -> None:
    _, run = _owned(db, run_id, token, statuses={"capturing", "loading", "catching_up"})
    run.lease_expires_at = _now() + timedelta(seconds=LEASE_SECONDS)
    db.flush()


def begin_load(db, run_id: str, token: str, *, capture_id: str, capture_start: dict) -> None:
    _, run = _owned(db, run_id, token, statuses={"capturing"})
    if not capture_id or not isinstance(capture_start, dict) or not capture_start:
        raise ReplicaControlError("CAPTURE_PROOF_MISSING")
    run.capture_id = str(uuid.UUID(capture_id))
    run.capture_start = copy.deepcopy(capture_start)
    run.status = "loading"
    db.flush()


def finish_load(db, run_id: str, token: str, *, completion: dict) -> None:
    replica, run = _owned(db, run_id, token, statuses={"loading"})
    if (completion.get("complete") is not True
            or type(completion.get("row_count")) is not int
            or completion["row_count"] < 0
            or completion.get("row_count") != completion.get("expected_rows")
            or completion.get("schema_fingerprint") != run.schema_fingerprint
            or not completion.get("snapshot_id")):
        raise ReplicaControlError("SNAPSHOT_PROOF_INCOMPLETE")
    run.snapshot_proof = copy.deepcopy(completion)
    run.row_count = completion["row_count"]
    run.status = "catching_up"
    if replica.status not in {"rebuild_required", "blocked"}:
        replica.status = "catching_up"
    db.flush()


def publish_run(db, run_id: str, token: str, *, applied_position: dict,
                coverage_confirmed_at: datetime, row_count: int) -> str | None:
    """Publish only inside the transaction that applied the verified barrier.

    The capture/apply layer must validate source-specific ordering and durable
    coverage before calling this function; a timestamp is not that validation.
    Returns the retired run identity for separately fenced relation cleanup.
    """
    replica, run = _owned(db, run_id, token, statuses={"catching_up"})
    if (not run.capture_id or not run.capture_start or not run.snapshot_proof
            or not isinstance(applied_position, dict) or not applied_position
            or not isinstance(coverage_confirmed_at, datetime)
            or type(row_count) is not int or row_count < 0):
        raise ReplicaControlError("APPLY_PROOF_INCOMPLETE")
    asset = db.get(IntegratedAsset, run.asset_id)
    if asset is None or asset.schema_fingerprint != run.schema_fingerprint:
        raise ReplicaControlError("REPLICA_SCHEMA_CHANGED")
    previous = replica.active_run_id
    if previous:
        old = db.get(IntegratedReplicaRun, previous)
        if old is None or old.status != "active":
            raise ReplicaControlError("ACTIVE_REPLICA_MISSING")
        old.status = "retired"
    run.status = "active"
    run.applied_position = copy.deepcopy(applied_position)
    run.row_count = row_count
    run.completed_at = _now()
    run.owner_token = None
    run.lease_expires_at = None
    replica.active_run_id = run.id
    replica.pending_run_id = None
    replica.schema_fingerprint = run.schema_fingerprint
    replica.applied_position = copy.deepcopy(applied_position)
    replica.applied_at = _now()
    replica.coverage_confirmed_at = _utc(coverage_confirmed_at)
    replica.status = "ready"
    replica.last_error_code = None
    db.flush()
    return previous


def fail_run(db, run_id: str, token: str, *, error_code: str) -> None:
    replica, run = _owned(db, run_id, token, statuses={"capturing", "loading", "catching_up"})
    run.status = "failed"
    run.last_error_code = error_code
    run.completed_at = _now()
    run.owner_token = None
    run.lease_expires_at = None
    replica.pending_run_id = None
    replica.last_error_code = error_code
    if replica.status not in {"rebuild_required", "blocked"}:
        replica.status = "stale" if replica.active_run_id else "blocked"
    db.flush()


def recover_expired_run(db, run_id: str) -> bool:
    """Fence an abandoned initializer without resuming its lost source snapshot.

    Physical cleanup uses the returned run's identity after this transaction
    commits. A late worker cannot renew, load or publish with the expired token.
    The active replica is never replaced by an incomplete candidate.
    """
    initial = db.get(IntegratedReplicaRun, str(run_id))
    if initial is None:
        return False
    replica = _replica(db, initial.asset_id)
    run = db.scalar(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.id == str(run_id)).with_for_update()
        .execution_options(populate_existing=True))
    if (replica.pending_run_id != run.id or replica.generation != run.generation
            or run.status not in {"capturing", "loading", "catching_up"}
            or run.lease_expires_at is None or _utc(run.lease_expires_at) > _now()):
        return False
    run.status = "failed"
    run.last_error_code = "INITIALIZATION_OWNER_EXPIRED"
    run.completed_at = _now()
    run.owner_token = None
    run.lease_expires_at = None
    replica.pending_run_id = None
    replica.last_error_code = run.last_error_code
    if replica.status not in {"rebuild_required", "blocked"}:
        replica.status = "stale" if replica.active_run_id else "blocked"
    db.flush()
    return True


def request_stop(db, asset: IntegratedAsset) -> IntegratedReplica:
    """Fence application immediately; the executor confirms capture detachment."""
    ensure_replica(db, asset)
    replica = _replica(db, asset.id)
    replica.status = "stopped"
    replica.last_error_code = "REPLICA_STOP_REQUESTED"
    runs = list(db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.asset_id == asset.id).order_by(IntegratedReplicaRun.id)
        .with_for_update().execution_options(populate_existing=True)))
    for run in runs:
        if run.status in {"queued", "capturing", "loading", "catching_up", "active"}:
            run.status = "cancelled"
            run.owner_token = None
            run.lease_expires_at = None
            run.completed_at = _now()
            run.last_error_code = "REPLICA_STOP_REQUESTED"
    replica.active_run_id = replica.pending_run_id = None
    db.flush()
    return replica


def mark_unavailable(db, asset_id: str, *, error_code: str, rebuild_required: bool) -> None:
    replica = _replica(db, asset_id)
    replica.last_error_code = error_code
    replica.status = ("rebuild_required" if rebuild_required
                      else "stale" if replica.active_run_id else "blocked")
    db.flush()


def readable_run(db, asset: IntegratedAsset) -> tuple[IntegratedReplica, IntegratedReplicaRun]:
    replica = db.get(IntegratedReplica, asset.id)
    if (replica is None or not replica.active_run_id
            or replica.status not in READABLE_STATUSES):
        raise ReplicaControlError("REPLICA_NOT_READY")
    run = db.get(IntegratedReplicaRun, replica.active_run_id)
    if run is None or run.status != "active" or run.asset_id != asset.id:
        raise ReplicaControlError("ACTIVE_REPLICA_MISSING")
    if (replica.schema_fingerprint != asset.schema_fingerprint
            or run.schema_fingerprint != asset.schema_fingerprint):
        raise ReplicaControlError("REPLICA_SCHEMA_CHANGED")
    return replica, run
