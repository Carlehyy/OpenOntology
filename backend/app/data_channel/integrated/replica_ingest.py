"""Durable batch entry point for current replicas; the receiver owns commit.

Snapshot COPY and its batch receipt share the target transaction. A connector
restart during an unfinished snapshot creates a new private run; snapshot rows
are never overlaid onto a previously published relation.
"""
from __future__ import annotations

import copy
import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import sqlalchemy as sa

from app.data_channel.connections.models import Connection
from app.data_channel.integrated import cdc_control, replica_control as control
from app.data_channel.integrated import replica_journal as journal
from app.data_channel.integrated import replica_coverage as coverage
from app.data_channel.integrated import replica_store as store
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import IntegratedReplicaCapture
from app.data_channel.integrated.replica_codec import ReplicaCodecError, decode_source_image
from app.data_channel.integrated.replica_models import IntegratedReplica, IntegratedReplicaRun
from app.data_channel.integrated.replica_snapshot_models import IntegratedReplicaSnapshotBatch


class ReplicaIngestError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _locked_capture(db, capture_id):
    initial = db.get(IntegratedReplicaCapture, str(capture_id))
    if initial is None:
        raise ReplicaIngestError("CAPTURE_NOT_ACCEPTING")
    members = copy.deepcopy(initial.tables_json)
    if not isinstance(members, list) or not members:
        raise ReplicaIngestError("CAPTURE_TABLES_INVALID")
    # Same lock order as apply/publication: replica -> run -> capture.
    asset_ids = sorted({item["asset_id"] for item in members})
    replicas = {row.asset_id: row for row in db.scalars(sa.select(IntegratedReplica).where(
        IntegratedReplica.asset_id.in_(asset_ids)).order_by(IntegratedReplica.asset_id)
        .with_for_update().execution_options(populate_existing=True))}
    run_ids = sorted({item["run_id"] for item in members})
    runs = {row.id: row for row in db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.id.in_(run_ids)).order_by(IntegratedReplicaRun.id)
        .with_for_update().execution_options(populate_existing=True))}
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == str(capture_id)).with_for_update()
        .execution_options(populate_existing=True))
    if capture is None or capture.status not in {"starting", "streaming", "stale"}:
        raise ReplicaIngestError("CAPTURE_NOT_ACCEPTING")
    if capture.tables_json != members:
        # Detachment may race the initial member read. Retry with the new
        # frozen-member flags instead of poisoning a healthy shared stream.
        raise ReplicaIngestError("CAPTURE_MEMBERS_CHANGED", retryable=True)
    connection = db.get(Connection, capture.connection_id)
    if connection is None:
        raise ReplicaIngestError("SOURCE_CONNECTION_MISSING")
    try:
        identity = cdc_control.source_identity(connection)
    except ValueError:
        raise ReplicaIngestError("SOURCE_CONFIG_UNAVAILABLE") from None
    if identity != capture.identity_fingerprint:
        raise ReplicaIngestError("SOURCE_IDENTITY_CHANGED")
    targets = {}
    for member in members:
        if member.get("detached") is True:
            continue
        run = runs.get(member["run_id"])
        replica = replicas.get(member["asset_id"])
        asset = db.get(IntegratedAsset, member["asset_id"])
        if run is not None and run.status in {"retired", "failed", "cancelled"}:
            continue
        if (run is None or replica is None or asset is None
                or run.asset_id != asset.id or run.capture_id != capture.id
                or asset.connection_id != capture.connection_id
                or asset.schema_fingerprint != run.schema_fingerprint
                or run.status not in {"loading", "catching_up", "active"}
                or (run.status == "active" and replica.active_run_id != run.id)
                or (run.status != "active" and (
                    replica.pending_run_id != run.id or replica.generation != run.generation
                    or not run.owner_token or run.lease_expires_at is None
                    or _utc(run.lease_expires_at) <= _now()))):
            raise ReplicaIngestError("REPLICA_TARGET_CHANGED")
        key = (asset.schema_name, asset.table_name)
        if key in targets:
            raise ReplicaIngestError("CAPTURE_TABLES_INVALID")
        targets[key] = (replica, run, asset)
    return capture, connection, targets


def _snapshot_batch(db, capture, run, asset, envelopes):
    identity = [{"schema": envelope["schema"], "source": envelope["payload"].get("source"),
                 "after": envelope["payload"].get("after")}
                for envelope in envelopes]
    batch_id = hashlib.sha256(journal._canonical(identity).encode("utf-8")).hexdigest()
    existing = db.get(IntegratedReplicaSnapshotBatch, (run.id, batch_id))
    if existing is not None:
        return 0, len(envelopes)
    if run.status != "loading":
        raise ReplicaIngestError("UNEXPECTED_SNAPSHOT")
    schema = store.build_replica_schema(run.id, run.schema_json,
                                        schema_fingerprint=run.schema_fingerprint)
    try:
        rows = [decode_source_image(envelope, run.schema_json) for envelope in envelopes]
        if any(row is None for row in rows):
            raise ReplicaIngestError("SNAPSHOT_ROW_INVALID")
        # The receiver bounds both encoded bytes and row count. COPY and the
        # receipt are rolled back together on any failure, including ACK loss.
        store.write_batch(db.connection(), schema, rows)
    except (ReplicaCodecError, store.ReplicaStoreError) as exc:
        raise ReplicaIngestError(exc.code) from exc
    db.add(IntegratedReplicaSnapshotBatch(run_id=run.id, batch_id=batch_id,
                                         row_count=len(rows)))
    run.row_count += len(rows)
    proof = dict(run.snapshot_proof or {})
    proof.update(snapshot_id=capture.id, row_count=run.row_count,
                 initial_rows=run.row_count, schema_fingerprint=run.schema_fingerprint)
    run.snapshot_proof = proof
    db.flush()
    return len(rows), 0


def _notification(db, capture, targets, envelope, offset):
    payload = envelope["payload"]
    if (envelope["schema"].get("name") != "io.debezium.connector.common.Notification"
            or payload.get("aggregate_type") != "Initial Snapshot"):
        raise ReplicaIngestError("SNAPSHOT_NOTIFICATION_INVALID")
    event_id, kind = payload.get("id"), payload.get("type")
    try:
        uuid.UUID(event_id)
    except (ValueError, TypeError, AttributeError):
        raise ReplicaIngestError("SNAPSHOT_NOTIFICATION_INVALID") from None
    data = payload.get("additional_data") or {}
    if not isinstance(data, dict) or any(not isinstance(value, str) for value in data.values()):
        raise ReplicaIngestError("SNAPSHOT_NOTIFICATION_INVALID")
    expected_collections = {f"{member['schema_name']}.{member['table_name']}"
                            for member in capture.tables_json}
    if kind in {"ABORTED", "FAILED"}:
        raise ReplicaIngestError("INITIAL_SNAPSHOT_FAILED")
    if kind == "SKIPPED":
        # An established stream resumes from its verified persistent offset;
        # Debezium announces that no new initial snapshot is necessary.
        positioned = ((type(offset.get("lsn")) is int and offset["lsn"] > 0)
                      or (isinstance(offset.get("file"), str) and bool(offset["file"])
                          and type(offset.get("pos")) is int and offset["pos"] >= 4))
        if not targets or not positioned or any(run.status != "active" or
                (run.snapshot_proof or {}).get("complete") is not True
                for _, run, _ in targets.values()):
            raise ReplicaIngestError("INITIAL_SNAPSHOT_INTERRUPTED")
        return
    if kind not in {"STARTED", "DATA_COLLECTIONS_RESOLVED", "IN_PROGRESS",
                    "TABLE_SCAN_COMPLETED", "COMPLETED"}:
        raise ReplicaIngestError("SNAPSHOT_NOTIFICATION_INVALID")
    if data.get("connector_name") not in {None, "oo_replica_" + uuid.UUID(capture.id).hex}:
        raise ReplicaIngestError("SOURCE_PARTITION_CHANGED")
    if kind == "DATA_COLLECTIONS_RESOLVED":
        resolved = data.get("data_collections", "").split(",")
        if len(resolved) != len(expected_collections) or set(resolved) != expected_collections:
            raise ReplicaIngestError("SNAPSHOT_TABLES_CHANGED")
    for key, (_, run, _) in targets.items():
        proof = copy.deepcopy(run.snapshot_proof or {})
        if kind == "STARTED":
            previous = proof.get("started_id")
            if previous is not None and previous != event_id:
                raise ReplicaIngestError("INITIAL_SNAPSHOT_RESTARTED")
            timestamp = payload.get("timestamp")
            if type(timestamp) is not int:
                raise ReplicaIngestError("SNAPSHOT_NOTIFICATION_INVALID")
            proof.update(started_id=event_id, started_at=datetime.fromtimestamp(
                timestamp / 1000, timezone.utc).isoformat())
        elif kind == "DATA_COLLECTIONS_RESOLVED":
            proof["collections_resolved"] = True
        elif kind == "TABLE_SCAN_COMPLETED":
            if data.get("scanned_collection") != ".".join(key):
                continue
            count = data.get("total_rows_scanned", "")
            if data.get("status") != "SUCCEEDED" or not count.isdecimal():
                raise ReplicaIngestError("INITIAL_SNAPSHOT_FAILED")
            expected = int(count)
            if "expected_rows" in proof and proof["expected_rows"] != expected:
                raise ReplicaIngestError("SNAPSHOT_COUNT_CHANGED")
            proof.update(expected_rows=expected, scan_id=event_id)
        elif kind == "COMPLETED":
            if proof.get("complete") is True:
                if proof.get("completed_id") != event_id:
                    raise ReplicaIngestError("INITIAL_SNAPSHOT_RESTARTED")
                continue
            if (run.status != "loading" or not proof.get("started_id")
                    or not proof.get("collections_resolved") or not proof.get("scan_id")
                    or proof.get("expected_rows") != run.row_count or not offset):
                raise ReplicaIngestError("SNAPSHOT_PROOF_INCOMPLETE")
            # This global notification is emitted after the final snapshot row.
            # Per-table SCAN_COMPLETED can precede its final row, so it only
            # records a count; publication waits for this global barrier.
            proof.update(complete=True, completed_id=event_id, snapshot_id=capture.id,
                row_count=run.row_count, initial_rows=run.row_count,
                schema_fingerprint=run.schema_fingerprint, offset=copy.deepcopy(offset),
                source_confirmed_at=proof["started_at"])
            control.finish_load(db, run.id, run.owner_token, completion=proof)
            continue
        run.snapshot_proof = proof
    db.flush()


def ingest_batch(db, capture_id: str, records: list[dict]) -> dict:
    """Persist one ordered HTTP batch, never acknowledge or commit here."""
    if not isinstance(records, list) or not 1 <= len(records) <= journal.MAX_BATCH_RECORDS:
        raise ReplicaIngestError("INVALID_TRANSPORT_BATCH")
    capture, connection, targets = _locked_capture(db, capture_id)
    dialect = "postgresql" if connection.kind == "postgres" else connection.kind
    expected_server = "oo_replica_" + uuid.UUID(capture.id).hex
    snapshot_rows = defaultdict(list)
    ordered = []
    change_seen = completed_seen = False
    accepted = duplicates = 0
    for record in records:
        try:
            partition, offset, envelope = journal.unpack(record)
        except journal.ReplicaJournalError as exc:
            raise ReplicaIngestError(exc.code, retryable=exc.retryable) from exc
        if partition.get("server") != expected_server:
            raise ReplicaIngestError("SOURCE_PARTITION_CHANGED")
        payload = envelope["payload"] if envelope else None
        if payload is not None and not isinstance(payload, dict):
            raise ReplicaIngestError("INVALID_TRANSPORT_RECORD")
        op = payload.get("op") if payload else None
        if op == "r":
            if change_seen or completed_seen:
                raise ReplicaIngestError("SNAPSHOT_ORDER_CHANGED")
            source = payload.get("source") or {}
            schema_name = source.get("schema") if dialect == "postgresql" else source.get("db")
            key = (schema_name, source.get("table"))
            target = targets.get(key)
            if target is None:
                known = any((member.get("schema_name"), member.get("table_name")) == key
                            for member in capture.tables_json)
                if known:
                    # A stopped member remains in the child's immutable include
                    # set while other tables finish their snapshot and stream.
                    continue
                raise ReplicaIngestError("SOURCE_TABLE_CHANGED")
            snapshot_rows[target[1].id].append(envelope)
        else:
            notification = bool(envelope and envelope["schema"].get("name") ==
                                "io.debezium.connector.common.Notification")
            if notification and payload.get("type") == "COMPLETED":
                completed_seen = True
            if op in {"c", "u", "d", "t"} or (payload and payload.get("status") == "END"):
                change_seen = True
            ordered.append((record, envelope, offset, notification))
    for replica, run, asset in targets.values():
        rows = snapshot_rows.get(run.id)
        if rows:
            count, replayed = _snapshot_batch(db, capture, run, asset, rows)
            accepted += count
            duplicates += replayed
            if count:
                for record in reversed(records):
                    _, offset, event = journal.unpack(record)
                    if (event and event["payload"].get("op") == "r"
                            and event["payload"].get("source", {}).get("table") == asset.table_name):
                        capture.source_offset = offset
                        break
    streaming = []

    def flush_streaming():
        nonlocal accepted, duplicates
        if not streaming:
            return
        try:
            result = journal.append_batch(db, capture.id, streaming, dialect=dialect)
        except journal.ReplicaJournalError as exc:
            raise ReplicaIngestError(exc.code, retryable=exc.retryable) from exc
        accepted += result["accepted"]
        duplicates += result["duplicates"]
        streaming.clear()

    for record, envelope, offset, notification in ordered:
        if notification:
            flush_streaming()
            _notification(db, capture, targets, envelope, offset)
        elif envelope and envelope["payload"].get("op") == "m":
            flush_streaming()
            if (dialect != "postgresql" or envelope["schema"].get("name") !=
                    "io.debezium.connector.postgresql.MessageValue"):
                raise ReplicaIngestError("REPLICA_COVERAGE_INVALID")
            try:
                coverage.observe_postgres_message(db, capture, envelope, offset,
                                                   record["payload"].get("topic"))
                capture.source_offset = offset
            except coverage.ReplicaCoverageError as exc:
                raise ReplicaIngestError(exc.code) from exc
        elif dialect == "mysql" and (envelope is None or
                str(envelope["schema"].get("name", "")).endswith(".Heartbeat")):
            flush_streaming()
            try:
                coverage.observe_mysql_offset(db, capture, offset)
                capture.source_offset = offset
            except coverage.ReplicaCoverageError as exc:
                raise ReplicaIngestError(exc.code) from exc
        else:
            streaming.append(record)
    flush_streaming()
    # Notifications and duplicate snapshot/transaction receipts must not
    # overwrite a newer streaming offset during an ACK-loss replay.
    capture.heartbeat_at = _now()
    for replica, _, _ in targets.values():
        replica.heartbeat_at = capture.heartbeat_at
    db.flush()
    return {"status": "accepted", "accepted": accepted, "duplicates": duplicates}


def mark_capture_error(db, capture_id: str, code: str, *, retryable: bool) -> None:
    """Record a bounded error after the rejected batch has rolled back."""
    initial = db.get(IntegratedReplicaCapture, str(capture_id))
    if initial is None or initial.status in {"stopped", "draining"}:
        return
    members = list(initial.tables_json)
    for asset_id in sorted({item["asset_id"] for item in members}):
        replica = db.scalar(sa.select(IntegratedReplica).where(
            IntegratedReplica.asset_id == asset_id).with_for_update()
            .execution_options(populate_existing=True))
        if replica is None or replica.status == "stopped":
            continue
        # A failed candidate must never invalidate an independent, healthy
        # active capture or restore a terminal member after stop/detachment.
        run_ids = {item["run_id"] for item in members
                   if item["asset_id"] == asset_id and not item.get("detached")}
        if (replica.active_run_id in run_ids or
                (not replica.active_run_id and replica.pending_run_id in run_ids)):
            replica.status = ("stale" if retryable and replica.active_run_id
                              else "blocked" if retryable else "rebuild_required")
            replica.last_error_code = code
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == str(capture_id)).with_for_update()
        .execution_options(populate_existing=True))
    if capture is not None and capture.status not in {"stopped", "draining"}:
        capture.status = "stale" if retryable else "blocked"
        capture.last_error_code = code
    db.flush()
