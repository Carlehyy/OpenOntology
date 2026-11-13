"""Database lifecycle for immutable replica capture cohorts.

A newly registered or rebuilt table joins a new cohort. Existing cohorts never
restart to expand their table list. All functions run in the caller's target
transaction; source IO and child processes belong to the supervisor.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from app.data_channel.connections.models import Connection
from app.data_channel.integrated import cdc_control, replica_control as control
from app.data_channel.integrated import replica_store as store
from app.data_channel.integrated import replica_coverage as coverage
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import IntegratedReplicaCapture
from app.data_channel.integrated.replica_models import IntegratedReplica, IntegratedReplicaRun


def now():
    return datetime.now(timezone.utc)


def queue_registered(db) -> None:
    """Enrol existing assets without doing source IO in a schema migration."""
    assets = list(db.scalars(sa.select(IntegratedAsset).outerjoin(
        IntegratedReplica, IntegratedReplica.asset_id == IntegratedAsset.id)
        .where(IntegratedReplica.asset_id.is_(None)).order_by(IntegratedAsset.id)))
    for asset in assets:
        control.request_rebuild(db, asset)


def prepare_capture(db, connection_id: str) -> IntegratedReplicaCapture | None:
    connection = db.scalar(sa.select(Connection).where(Connection.id == connection_id)
                           .with_for_update().execution_options(populate_existing=True))
    if connection is None:
        return None
    runs = list(db.scalars(sa.select(IntegratedReplicaRun).join(
        IntegratedAsset, IntegratedAsset.id == IntegratedReplicaRun.asset_id).where(
            IntegratedAsset.connection_id == connection_id,
            IntegratedReplicaRun.status == "queued").order_by(IntegratedReplicaRun.asset_id)))
    if not runs:
        return None
    generation = (db.scalar(sa.select(sa.func.max(IntegratedReplicaCapture.generation)).where(
        IntegratedReplicaCapture.connection_id == connection_id)) or 0) + 1
    capture = IntegratedReplicaCapture(id=str(uuid.uuid4()), connection_id=connection_id,
        generation=generation, identity_fingerprint=cdc_control.source_identity(connection),
        status="starting", tables_json=[])
    db.add(capture)
    db.flush()
    members = []
    for run in runs:
        asset = db.get(IntegratedAsset, run.asset_id)
        replica = db.get(IntegratedReplica, run.asset_id)
        if replica is None or replica.pending_run_id != run.id:
            continue
        try:
            schema = store.build_replica_schema(run.id, run.schema_json,
                                                schema_fingerprint=run.schema_fingerprint)
        except store.ReplicaStoreError as exc:
            # One unsupported registered table must not repeatedly roll back
            # preparation of every other queued table from the same source.
            run.status, run.last_error_code = "failed", exc.code
            run.completed_at = now()
            replica.pending_run_id = None
            replica.last_error_code = exc.code
            replica.status = "stale" if replica.active_run_id else "blocked"
            continue
        token = control.claim_run(db, run.id)
        store.create_table(db.connection(), schema)
        control.begin_load(db, run.id, token, capture_id=capture.id,
                           capture_start={"mode": "debezium_initial", "capture_id": capture.id})
        members.append({"asset_id": asset.id, "run_id": run.id,
                        "schema_name": asset.schema_name, "table_name": asset.table_name,
                        "schema_json": copy.deepcopy(run.schema_json),
                        "schema_fingerprint": run.schema_fingerprint})
    if not members:
        db.delete(capture)
        db.flush()
        return None
    capture.tables_json = members
    db.flush()
    return capture


def live_runs(db, capture_id: str) -> list[IntegratedReplicaRun]:
    return list(db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.capture_id == capture_id,
        IntegratedReplicaRun.status.in_(["loading", "catching_up", "active"]))
        .order_by(IntegratedReplicaRun.asset_id)))


def renew_initializers(db, capture_id: str) -> None:
    for run in live_runs(db, capture_id):
        if run.status in {"loading", "catching_up"}:
            control.renew_run(db, run.id, run.owner_token)


def abandon_initialization(db, capture_id: str, *, code: str,
                           retry: bool) -> None:
    """A lost native snapshot is discarded, never resumed over partial rows."""
    capture = db.get(IntegratedReplicaCapture, capture_id)
    if capture is None:
        return
    for initial in live_runs(db, capture_id):
        if initial.status == "active":
            continue
        replica = control._replica(db, initial.asset_id)
        run = db.scalar(sa.select(IntegratedReplicaRun).where(
            IntegratedReplicaRun.id == initial.id).with_for_update()
            .execution_options(populate_existing=True))
        if replica.pending_run_id != run.id:
            continue
        run.status = "failed"
        run.last_error_code = code
        run.owner_token = None
        run.lease_expires_at = None
        run.completed_at = now()
        replica.pending_run_id = None
        replica.last_error_code = code
        if replica.status not in {"rebuild_required", "blocked"}:
            replica.status = "stale" if replica.active_run_id else "blocked"
        if retry:
            control.request_rebuild(db, db.get(IntegratedAsset, run.asset_id))
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == capture_id).with_for_update()
        .execution_options(populate_existing=True))
    capture.status = "draining"
    capture.last_error_code = code
    db.flush()


def apply_capture(db, capture_id: str, *, max_transactions: int = 100) -> int:
    """Commit every cohort member and its cursor together, including cross-table TXs."""
    from app.data_channel.integrated.replica_apply import apply_run

    runs = live_runs(db, capture_id)
    # Pre-lock every replica before any run/capture lock. Journal follows this
    # same order; concurrent HTTP ingestion cannot form a lock cycle.
    asset_ids = sorted({run.asset_id for run in runs})
    list(db.scalars(sa.select(IntegratedReplica).where(IntegratedReplica.asset_id.in_(asset_ids))
                    .order_by(IntegratedReplica.asset_id).with_for_update()
                    .execution_options(populate_existing=True)))
    list(db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.id.in_([run.id for run in runs])).order_by(IntegratedReplicaRun.id)
        .with_for_update().execution_options(populate_existing=True)))
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == capture_id).with_for_update()
        .execution_options(populate_existing=True))
    if capture is None or capture.status not in {"starting", "streaming", "stale"}:
        return 0
    applied = 0
    for run in runs:
        if run.status in {"catching_up", "active"}:
            applied += apply_run(db, run.id, owner_token=run.owner_token,
                                 max_transactions=max_transactions)
    confirmed = coverage.applied_probe(capture, runs)
    if confirmed is None:
        confirmed = (capture.coverage_probe or {}).get("confirmed")
    valid_cut = (isinstance(confirmed, dict) and confirmed.get("capture_id") == capture.id
                 and confirmed.get("generation") == capture.generation)
    for run in runs:
        cursor = run.applied_position or {}
        if (run.status == "catching_up" and run.snapshot_proof
                and run.snapshot_proof.get("complete") is True and valid_cut
                and int(cursor.get("commit_seq", 0)) >= confirmed["commit_seq"]
                and int(cursor.get("commit_seq", 0)) >= capture.committed_seq):
            position = copy.deepcopy(cursor) or {"capture_id": capture.id,
                "commit_seq": 0, "snapshot": copy.deepcopy(run.snapshot_proof.get("offset"))}
            control.publish_run(db, run.id, run.owner_token, applied_position=position,
                coverage_confirmed_at=datetime.fromisoformat(confirmed["confirmed_at"]),
                row_count=run.row_count)
        if run.status == "active":
            replica = db.get(IntegratedReplica, run.asset_id)
            replica.heartbeat_at = capture.heartbeat_at
            if valid_cut and int(cursor.get("commit_seq", 0)) >= confirmed["commit_seq"]:
                timestamp = datetime.fromisoformat(confirmed["confirmed_at"])
                previous = replica.coverage_confirmed_at
                if previous is None or timestamp.timestamp() > previous.replace(
                        tzinfo=previous.tzinfo or timezone.utc).timestamp():
                    replica.coverage_confirmed_at = timestamp
    db.flush()
    return applied


def cleanup_relations(db, *, limit: int = 20) -> int:
    """Drop only terminal, unreferenced run tables and their snapshot receipts."""
    from app.data_channel.integrated.replica_snapshot_models import IntegratedReplicaSnapshotBatch

    runs = list(db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.status.in_(["retired", "failed", "cancelled"]),
        IntegratedReplicaRun.cleaned_at.is_(None)).order_by(IntegratedReplicaRun.created_at)
        .limit(limit)))
    cleaned = 0
    for initial in runs:
        replica = control._replica(db, initial.asset_id)
        run = db.scalar(sa.select(IntegratedReplicaRun).where(
            IntegratedReplicaRun.id == initial.id).with_for_update()
            .execution_options(populate_existing=True))
        if run.id in {replica.active_run_id, replica.pending_run_id}:
            continue
        store.drop_owned(db.connection(), run.id)
        db.execute(sa.delete(IntegratedReplicaSnapshotBatch).where(
            IntegratedReplicaSnapshotBatch.run_id == run.id))
        run.cleaned_at = now()
        cleaned += 1
    db.flush()
    return cleaned
