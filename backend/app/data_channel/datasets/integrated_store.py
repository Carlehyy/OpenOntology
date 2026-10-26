"""Bounded immutable relational versions for registered integrated datasets.

Curated lake tables/changesets are deliberately separate.  A private relation is
never readable through these ports until EOF proof, physical count, schema and
an ordered full-stream checksum agree.  Version, manifest, latest pointer,
write-reject triggers and publication outbox are committed in one transaction.
There is no offset resume: a failed run requires a new consistent source scan.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterator

import sqlalchemy as sa

from app.data_channel.datasets.lake_gate import IntegratedSnapshotGate, LakeGateError
from app.data_channel.datasets.lake_store import sanitize_lake_column
from app.data_channel.datasets.snapshot_text import MAX_STREAM_BATCH_BYTES, canonical_row_bytes
from app.data_channel.datasets.models import (
    Dataset, DatasetVersion, DatasetVersionEvent, IntegratedSyncRun,
)
from app.data_channel.integrated import fields

MAX_BATCH_SIZE = 1000
MAX_BATCH_BYTES = MAX_STREAM_BATCH_BYTES
ROW_ORDINAL = "__row_seq"
TABLE_PREFIX = "lake_iv_"
_TABLE_RE = re.compile(r"^lake_iv_[0-9a-f]{32}$")
_CHECKSUM_FORMAT = "sha256:integrated-v1:length-prefixed-schema-and-ordered-rows"
# An owner that cannot renew within this window may be reclaimed, but only by
# proving that the lease expired.  The executor renews every 20s while a task
# runs, so a healthy owner never leaks its single-owner claim.
INTEGRATED_LEASE_SECONDS = 90
_INTEGRATED_LEASE_FORMAT = "integrated-owner-lease-v1"
_ABANDONED_ERROR = (
    "INTEGRATED_SNAPSHOT_ABANDONED: the owner lease expired; restart from a "
    "fresh consistent source snapshot (no cursor resume)"
)
_RECOVERY_STATUSES = ("starting", "loading")


class IntegratedVersionError(RuntimeError):
    """Invalid, incomplete, unavailable or concurrently superseded version."""


@dataclass(frozen=True)
class TypedVersionRelation:
    table: sa.Table
    column_map: dict[str, str]
    row_ordinal: sa.Column
    metadata: dict


def _batch_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_BATCH_SIZE:
        raise IntegratedVersionError("Batch size must be between 1 and 1000")
    return value


def _as_utc(value) -> datetime | None:
    """Normalize stored lease timestamps (SQLite may return naive datetimes)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise IntegratedVersionError("Integrated owner lease timestamp is invalid") from None
    else:
        raise IntegratedVersionError("Integrated owner lease timestamp is invalid")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def new_owner_lease(*, actor_id: str, token: str | None = None,
                    now: datetime | None = None,
                    seconds: int = INTEGRATED_LEASE_SECONDS,
                    pid: int | None = None) -> dict:
    """Mint the single-owner lease stored inside the run's snapshot manifest.

    No model/migration change is required: the lease is additive JSON evidence.
    It is ownership evidence only; row counts and ``__row_seq`` are never a
    source cursor that may be resumed.
    """
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise IntegratedVersionError("Integrated owner lease requires its initiating actor")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        raise IntegratedVersionError("Integrated owner lease duration must be positive")
    token = str(uuid.uuid4()) if token is None else token
    if not isinstance(token, str) or not token.strip():
        raise IntegratedVersionError("Integrated owner lease token is invalid")
    acquired = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    return {
        "format": _INTEGRATED_LEASE_FORMAT,
        "token": token,
        "actor_id": actor_id,
        "acquired_at": acquired.isoformat(),
        "expires_at": (acquired + timedelta(seconds=seconds)).isoformat(),
        "pid": os.getpid() if pid is None else pid,
    }


def lease_of(run_or_snapshot) -> dict | None:
    """Return the stored lease mapping, or None when the run is unverifiable."""
    snapshot = (run_or_snapshot.snapshot_json
                if hasattr(run_or_snapshot, "snapshot_json") else run_or_snapshot)
    if not isinstance(snapshot, dict):
        return None
    lease = snapshot.get("lease")
    return lease if isinstance(lease, dict) else None


def lease_expires_at(run_or_snapshot) -> datetime | None:
    lease = lease_of(run_or_snapshot)
    if lease is None:
        return None
    return _as_utc(lease.get("expires_at"))


def lease_retry_delay(run_or_snapshot, *, now: datetime | None = None) -> float | None:
    """Seconds until the live owner lease expires; None when unverifiable."""
    expires = lease_expires_at(run_or_snapshot)
    if expires is None:
        return None
    checked = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    return max(0.0, (expires - checked).total_seconds())


def renew_run_lease(db, run_id: str, token: str, *, now: datetime | None = None,
                    seconds: int = INTEGRATED_LEASE_SECONDS) -> bool:
    """Renew one live owner's lease. Must be called with a dedicated session.

    The row lock serializes renewal against recovery/failure transitions; a
    reclaimed (failed/sealed) run can never regain a lease.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise IntegratedVersionError("Integrated lease renewal requires its run")
    if not isinstance(token, str) or not token.strip():
        raise IntegratedVersionError("Integrated lease renewal requires its owner token")
    checked = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    run = (db.query(IntegratedSyncRun).filter(IntegratedSyncRun.id == run_id)
           .with_for_update().populate_existing().one_or_none())
    if run is None or run.status not in _RECOVERY_STATUSES:
        return False
    lease = lease_of(run)
    if lease is None or lease.get("token") != token:
        return False
    snapshot = copy.deepcopy(run.snapshot_json)
    snapshot["lease"] = {
        **lease,
        "expires_at": (checked + timedelta(seconds=seconds)).isoformat(),
        "renewed_at": checked.isoformat(),
    }
    run.snapshot_json = snapshot
    db.commit()
    return True


def _owned_run(db, run, owner_token: str) -> IntegratedSyncRun:
    """Fence this worker against a run reclaimed by a recovery winner."""
    current = (db.query(IntegratedSyncRun).filter(IntegratedSyncRun.id == run.id)
               .with_for_update().populate_existing().one_or_none())
    if current is None:
        raise IntegratedVersionError("Integrated run no longer exists")
    if current.status != "loading" or current.version_id:
        raise IntegratedVersionError(
            "Integrated run is no longer owned by this worker; stop without publishing")
    lease = lease_of(current)
    if lease is None or lease.get("token") != owner_token:
        raise IntegratedVersionError(
            "Integrated run ownership/lease changed; refusing to continue")
    return current


def _audit_receipt(audit) -> dict:
    """Public recovery receipt reconstructed from the immutable audit row."""
    recovered_at = _as_utc(audit.recovered_at)
    return {
        "run_id": audit.run_id,
        "asset_id": audit.asset_id,
        "dataset_id": audit.dataset_id,
        "status": "failed",
        "complete": False,
        "recovered": True,
        "restart_required": True,
        "original_status": audit.original_status,
        "operator": audit.recovered_by,
        "reason": audit.reason,
        "recovered_at": recovered_at.isoformat() if recovered_at else None,
        "ownership": audit.ownership_proof or {"method": audit.ownership_method},
        "physical_table": audit.physical_table,
        "table_existed": bool(audit.table_existed),
        "table_dropped": bool(audit.table_dropped),
        "error": _ABANDONED_ERROR,
    }


def recovery_receipt(db, run_id: str) -> dict | None:
    """Return the durable recovery receipt for this exact run, if any."""
    from app.data_channel.datasets.models import IntegratedSyncRecovery
    audit = db.query(IntegratedSyncRecovery).filter(
        IntegratedSyncRecovery.run_id == run_id).one_or_none()
    return _audit_receipt(audit) if audit is not None else None


def _ownership_proof(lease: dict | None, checked_at: datetime,
                     proof: dict | None) -> dict | None:
    """Only an expired lease or explicit operator evidence can prove owner loss."""
    if lease is not None:
        expires = _as_utc(lease.get("expires_at"))
        if expires is not None and expires <= checked_at:
            return {
                "method": "owner_lease_expired",
                "owner_token": lease.get("token"),
                "lease_expires_at": expires.isoformat(),
                "checked_at": checked_at.isoformat(),
            }
    if proof is not None:
        if not isinstance(proof, dict):
            raise IntegratedVersionError("Operator ownership proof must be a mapping")
        kind = proof.get("kind")
        reference = proof.get("reference")
        observed_at = _as_utc(proof.get("observed_at"))
        if (not isinstance(kind, str) or not kind.strip()
                or not isinstance(reference, str) or not reference.strip()
                or observed_at is None):
            raise IntegratedVersionError(
                "Operator ownership proof requires kind/reference/observed_at")
        if lease is not None and proof.get("owner_token") != lease.get("token"):
            raise IntegratedVersionError(
                "Operator ownership proof does not identify the current owner lease")
        return {
            "method": "operator_evidence",
            "kind": kind.strip()[:200],
            "reference": reference.strip()[:500],
            "observed_at": observed_at.isoformat(),
            "checked_at": checked_at.isoformat(),
        }
    return None


def recover_abandoned_snapshot(db, run_id: str, *, operator: str, reason: str,
                               now: datetime | None = None,
                               proof: dict | None = None) -> dict:
    """CAS a provably abandoned run to failed and drop only its private relation.

    A live lease, a sealed/failed run, or an unverifiable owner refuses the
    takeover.  The CAS, the immutable recovery audit and the private relation
    drop commit together; a repeated call returns the same durable receipt.
    """
    from app.data_channel.datasets.models import IntegratedSyncRecovery
    if not isinstance(run_id, str) or not run_id.strip():
        raise IntegratedVersionError("Integrated recovery requires an exact run id")
    if not isinstance(operator, str) or not operator.strip():
        raise IntegratedVersionError("Integrated recovery requires its operator")
    if not isinstance(reason, str) or not reason.strip():
        raise IntegratedVersionError("Integrated recovery requires a reason")
    checked_at = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    db.rollback()
    prior = recovery_receipt(db, run_id)
    if prior is not None:
        db.rollback()
        return {**prior, "idempotent": True}
    run = (db.query(IntegratedSyncRun).filter(IntegratedSyncRun.id == run_id)
           .with_for_update().populate_existing().one_or_none())
    # Re-read the audit under the row lock: a concurrent winner may have
    # committed between our first lookup and this serialization point.
    prior = recovery_receipt(db, run_id)
    if prior is not None:
        db.rollback()
        return {**prior, "idempotent": True}
    if run is None:
        db.rollback()
        return {
            "run_id": run_id, "status": "missing", "complete": False,
            "recovered": False, "idempotent": False,
            "reason": "run not found; nothing to dispose",
        }
    if run.status == "sealed" or run.version_id:
        db.rollback()
        raise IntegratedVersionError(
            "A sealed integrated run can never be recovered or cleaned")
    if run.status == "failed":
        db.rollback()
        return {
            "run_id": run.id, "asset_id": run.asset_id, "dataset_id": run.dataset_id,
            "status": "failed", "complete": False, "recovered": False,
            "idempotent": False,
            "reason": "run already failed by its owner; no recovery receipt exists",
        }
    if run.status not in _RECOVERY_STATUSES:
        db.rollback()
        raise IntegratedVersionError(
            f"Integrated run status {run.status!r} is not recoverable")
    lease = lease_of(run)
    ownership = _ownership_proof(lease, checked_at, proof)
    if ownership is None:
        db.rollback()
        raise IntegratedVersionError(
            "Integrated run owner lease is still live or cannot be proven dead; "
            "refusing to take over")
    physical = run.physical_table
    if (not _TABLE_RE.fullmatch(physical)
            or physical != TABLE_PREFIX + uuid.UUID(run.id).hex):
        db.rollback()
        raise IntegratedVersionError("Invalid abandoned integrated relation identity")
    existed = sa.inspect(db.connection()).has_table(physical)
    db.add(IntegratedSyncRecovery(
        id=str(uuid.uuid4()), run_id=run.id, asset_id=run.asset_id,
        dataset_id=run.dataset_id, original_status=run.status,
        owner_token=(lease or {}).get("token"),
        lease_expires_at=_as_utc((lease or {}).get("expires_at")),
        ownership_method=ownership["method"], ownership_proof=ownership,
        recovered_by=operator.strip()[:200], reason=reason.strip()[:500],
        physical_table=physical, table_existed=existed, table_dropped=existed,
        recovered_at=checked_at,
    ))
    changed = db.execute(
        sa.update(IntegratedSyncRun).where(
            IntegratedSyncRun.id == run.id,
            IntegratedSyncRun.status == run.status,
            IntegratedSyncRun.version_id.is_(None),
        ).values(
            status="failed", error=_ABANDONED_ERROR, completed_at=checked_at,
        ).execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        db.rollback()
        raise IntegratedVersionError(
            "Integrated run changed concurrently; refusing to recover it")
    try:
        if existed:
            sa.Table(physical, sa.MetaData()).drop(db.connection())
    except Exception:
        # The CAS and its audit are rolled back together with the failed drop:
        # never report a recovery whose private relation still exists.
        db.rollback()
        raise
    receipt = {
        "run_id": run.id,
        "asset_id": run.asset_id,
        "dataset_id": run.dataset_id,
        "status": "failed",
        "complete": False,
        "recovered": True,
        "restart_required": True,
        "original_status": run.status,
        "row_count": run.row_count,
        "expected_rows": run.expected_rows,
        "operator": operator.strip()[:200],
        "reason": reason.strip()[:500],
        "recovered_at": checked_at.isoformat(),
        "ownership": ownership,
        "physical_table": physical,
        "table_existed": existed,
        "table_dropped": existed,
        "error": _ABANDONED_ERROR,
        "idempotent": False,
    }
    db.commit()
    return receipt


def recover_expired_snapshots(db, *, operator: str, now: datetime | None = None,
                              limit: int = 100) -> list[dict]:
    """Bounded reconciliation sweep for runs whose owner lease expired.

    This is the operator/management entry point; it never touches a live lease
    and reports unverifiable rows instead of guessing.  Each recovered row is
    idempotent through ``recover_abandoned_snapshot``.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise IntegratedVersionError("Recovery sweep limit must be between 1 and 1000")
    checked_at = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    run_ids = [row.id for row in db.query(IntegratedSyncRun).filter(
        IntegratedSyncRun.status.in_(_RECOVERY_STATUSES),
    ).order_by(IntegratedSyncRun.updated_at.asc()).limit(limit).all()]
    receipts: list[dict] = []
    for run_id in run_ids:
        run = db.query(IntegratedSyncRun).filter(
            IntegratedSyncRun.id == run_id).populate_existing().one_or_none()
        expires = lease_expires_at(run) if run is not None else None
        if expires is None or expires > checked_at:
            continue
        try:
            receipts.append(recover_abandoned_snapshot(
                db, run_id, operator=operator,
                reason="owner lease expired (bounded reconciliation sweep)",
                now=checked_at,
            ))
        except IntegratedVersionError as exc:
            db.rollback()
            receipts.append({
                "run_id": run_id, "status": "blocked", "complete": False,
                "recovered": False, "idempotent": False, "error": str(exc),
            })
    return receipts


def _table(run: IntegratedSyncRun) -> sa.Table:
    # Names never come from caller-supplied SQL or an unvalidated manifest.
    if not _TABLE_RE.fullmatch(run.physical_table) or run.physical_table != TABLE_PREFIX + uuid.UUID(run.id).hex:
        raise IntegratedVersionError("Invalid integrated version relation identity")
    schema = run.schema_json
    columns = schema["source_schema"]
    mapping = schema["column_map"]
    primary_key = schema["primary_key_columns"]
    if (list(mapping) != [c["name"] for c in columns]
            or len(set(mapping.values())) != len(columns)
            or ROW_ORDINAL in mapping.values()):
        raise IntegratedVersionError("Invalid integrated version column mapping")
    fields = [sa.Column(ROW_ORDINAL, sa.BigInteger, nullable=False)]
    fields += [sa.Column(mapping[c["name"]], sa.Text,
                         nullable=c["nullable"] and c["name"] not in primary_key)
               for c in columns]
    constraints = [sa.UniqueConstraint(ROW_ORDINAL, name=f"uq_{run.physical_table}_seq")]
    if primary_key:
        constraints.append(sa.PrimaryKeyConstraint(
            *(mapping[k] for k in primary_key), name=f"pk_{run.physical_table}"))
    else:
        fields[0].primary_key = True
    return sa.Table(run.physical_table, sa.MetaData(), *fields, *constraints)


# The physical checksum covers exactly the physical manifest: the frozen
# logical projection is deliberately excluded so adding, removing or editing
# a field config never changes a version's physical identity.  Manifests
# without those keys hash byte-identically to the pre-projection format.
_CHECKSUM_EXCLUDED_KEYS = ("field_config", "logical_fingerprint")


def _hasher(run: IntegratedSyncRun):
    manifest = {key: value for key, value in run.schema_json.items()
                if key not in _CHECKSUM_EXCLUDED_KEYS}
    digest = hashlib.sha256()
    digest.update(b"integrated-v1\x00")
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode()
    digest.update(len(raw).to_bytes(8, "big"))
    digest.update(raw)
    return digest


def _hash_row(digest, values) -> None:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    digest.update(len(raw).to_bytes(8, "big"))
    digest.update(raw)


def _physical_row_bytes(row) -> int:
    try:
        return canonical_row_bytes(row, limit=MAX_BATCH_BYTES)
    except ValueError:
        raise IntegratedVersionError("Integrated row exceeds the 16 MiB stream byte budget") from None


def _iter_physical(db, table, *, batch_size=MAX_BATCH_SIZE, after=0, columns=None, predicate=None):
    columns = list(columns) if columns is not None else list(table.c)
    stmt = sa.select(*columns).where(table.c[ROW_ORDINAL] > after)
    if predicate is not None:
        stmt = stmt.where(predicate)
    # A row-count LIMIT still lets a buffered driver allocate 1000 wide rows.
    # Ask for a server-side cursor with exactly one-row prefetch, then enforce
    # independent byte and row caps. The only extra allocation is one source row.
    stmt = stmt.order_by(table.c[ROW_ORDINAL]).execution_options(
        stream_results=True, yield_per=1, max_row_buffer=1)
    records = db.execute(stmt).mappings()
    batch, batch_bytes = [], 0
    try:
        while (record := records.fetchone()) is not None:
            row_bytes = _physical_row_bytes(record)
            if batch and batch_bytes + row_bytes > MAX_BATCH_BYTES:
                yield batch
                batch, batch_bytes = [], 0
            batch.append(record)
            batch_bytes += row_bytes
            if len(batch) == batch_size:
                yield batch
                batch, batch_bytes = [], 0
        if batch:
            yield batch
    finally:
        records.close()


def begin_snapshot(db, descriptor: dict, *, run_id: str | None = None,
                   lease: dict | None = None) -> tuple[IntegratedSyncRun, IntegratedSnapshotGate]:
    """Create a private run; commits its metadata/DDL, not a DatasetVersion."""
    gate = IntegratedSnapshotGate(descriptor)
    dataset = db.get(Dataset, descriptor.get("dataset_id"))
    if dataset is None or dataset.kind != "integrated":
        raise IntegratedVersionError("Integrated snapshot requires its registered integrated Dataset")
    expected = descriptor.get("expected_rows")
    snapshot = descriptor.get("snapshot")
    if (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0
            or not isinstance(snapshot, dict) or snapshot.get("consistent") is not True
            or not snapshot.get("id") or not snapshot.get("started_at")):
        raise IntegratedVersionError("Integrated snapshot lacks consistent count/snapshot evidence")
    # The registered asset is the authority, not caller-supplied dataset names.
    from app.data_channel.integrated.models import IntegratedAsset, IntegratedAssetFieldConfig
    asset = db.get(IntegratedAsset, descriptor.get("asset_id"))
    if (asset is None or asset.dataset_id != dataset.id
            or asset.schema_fingerprint != gate.fingerprint):
        raise IntegratedVersionError("Integrated snapshot does not match its registered asset")
    previous = (db.query(IntegratedSyncRun).filter(
        IntegratedSyncRun.version_id == dataset.latest_version_id).first()
        if dataset.latest_version_id else None)
    if previous is not None and previous.schema_json["schema_fingerprint"] != gate.fingerprint:
        raise IntegratedVersionError("Integrated version schema/type changed; explicit schema migration is required")
    # Freeze the asset's logical projection into this run's manifest: sealed
    # versions keep the mapping they were published with, and the frozen
    # config must still match the physical schema being snapshotted.
    frozen_config = None
    config_row = db.get(IntegratedAssetFieldConfig, asset.id)
    if config_row is not None:
        try:
            frozen_config = fields.normalize_field_config(
                config_row.field_config,
                {"columns": gate.schema, "primary_key": gate.primary_key})
        except fields.FieldConfigError:
            raise IntegratedVersionError(
                "Integrated asset field configuration does not match its physical schema") from None
    taken = {ROW_ORDINAL}
    mapping = {name: sanitize_lake_column(name, taken) for name in gate.columns}
    schema = {
        "columns": gate.columns,
        "columns_typed": [{"name": c["name"], "type": c["type"]} for c in gate.schema],
        "source_schema": copy.deepcopy(gate.schema),
        "primary_key": ",".join(gate.primary_key),
        "primary_key_columns": list(gate.primary_key),
        "schema_fingerprint": gate.fingerprint,
        "column_map": mapping,
        "row_ordinal": ROW_ORDINAL,
        "value_encoding": "nullable-text-v1;binary=base64;decimal=exact;temporal=iso8601",
        "checksum_format": _CHECKSUM_FORMAT,
    }
    if frozen_config is not None:
        view = fields.logical_view(
            {"columns": gate.schema, "primary_key": gate.primary_key}, frozen_config)
        schema["field_config"] = frozen_config
        schema["logical_fingerprint"] = fields.logical_fingerprint(view)
    if run_id is None:
        run_id = str(uuid.uuid4())
        stored_snapshot = copy.deepcopy(snapshot)
        if lease is not None:
            stored_snapshot["lease"] = copy.deepcopy(lease)
        run = IntegratedSyncRun(
            id=run_id, dataset_id=dataset.id, asset_id=asset.id,
            base_version_id=dataset.latest_version_id,
            physical_table=TABLE_PREFIX + uuid.UUID(run_id).hex,
            status="loading", expected_rows=expected, row_count=0,
            schema_json=schema, snapshot_json=stored_snapshot,
        )
        db.add(run)
    else:
        run = db.get(IntegratedSyncRun, run_id)
        if (run is None or run.status != "starting" or run.asset_id != asset.id
                or run.dataset_id != dataset.id or run.row_count != 0 or run.version_id
                or run.physical_table != TABLE_PREFIX + uuid.UUID(run_id).hex):
            raise IntegratedVersionError("Integrated queued run is not available for a new snapshot")
        queued_lease = lease_of(run)
        if (queued_lease is not None and lease is not None
                and queued_lease.get("token") != lease.get("token")):
            raise IntegratedVersionError(
                "Integrated queued run belongs to a different owner lease")
        # Keep the enqueue-time base pointer: publishing a concurrently newer
        # version must invalidate this run rather than silently adopt its base.
        run.status = "loading"
        run.expected_rows = expected
        run.schema_json = schema
        run.snapshot_json = {**copy.deepcopy(snapshot),
                             "actor_id": run.snapshot_json.get("actor_id"),
                             "batch_size": run.snapshot_json.get("batch_size"),
                             "lease": copy.deepcopy(queued_lease or lease)}
    db.flush()
    _table(run).create(db.connection())
    db.commit()
    return run, gate


def _insert_batch(db, table, records: list[dict]) -> None:
    if db.get_bind().dialect.name != "postgresql":
        db.execute(table.insert(), records)
        return
    # PG COPY avoids 470M parameter-bind overhead for 10M x 47.  This buffer is
    # only one bounded batch. Text COPY distinguishes SQL NULL from empty text.
    buffer = io.StringIO()
    names = list(table.c.keys())
    for row in records:
        fields = []
        for name in names:
            value = row[name]
            if value is None:
                fields.append("\\N")
            else:
                fields.append(str(value).replace("\\", "\\\\").replace("\t", "\\t")
                              .replace("\n", "\\n").replace("\r", "\\r"))
        buffer.write("\t".join(fields) + "\n")
    buffer.seek(0)
    conn = db.connection()
    quote = conn.dialect.identifier_preparer.quote
    statement = f"COPY {quote(table.name)} ({', '.join(quote(n) for n in names)}) FROM STDIN"
    with conn.connection.driver_connection.cursor() as cursor:
        cursor.copy_expert(statement, buffer)


def write_snapshot_batch(db, run: IntegratedSyncRun, gate: IntegratedSnapshotGate,
                         batch: list[dict], digest, *, owner_token: str | None = None) -> None:
    """Validate/insert one batch and checkpoint count; duplicates fail in DB.

    ``owner_token`` fences a worker whose run was reclaimed by recovery: the
    fresh row is re-read under lock before every batch, so a revoked owner can
    never append to (or publish) a relation another winner disposed.
    """
    if owner_token is not None:
        run = _owned_run(db, run, owner_token)
    if run.status != "loading" or run.version_id:
        raise IntegratedVersionError("Cannot write a published or failed integrated run")
    rows = gate.accept_batch(batch)
    if not rows:
        return
    if run.row_count + len(rows) > run.expected_rows:
        raise IntegratedVersionError("Integrated source exceeded its snapshot row count")
    table = _table(run)
    mapping = run.schema_json["column_map"]
    records = []
    for index, row in enumerate(rows, run.row_count + 1):
        _hash_row(digest, [row[c] for c in mapping])
        records.append({ROW_ORDINAL: index, **{p: row[c] for c, p in mapping.items()}})
    _insert_batch(db, table, records)
    run.row_count += len(rows)
    db.commit()


def snapshot_hasher(run: IntegratedSyncRun):
    """Create the exact deterministic full-stream checksum accumulator."""
    return _hasher(run)


def _physical_evidence(db, run, *, checksum=False):
    table = _table(run)
    inspector = sa.inspect(db.connection())
    if not inspector.has_table(table.name):
        raise IntegratedVersionError("Integrated version relation is missing")
    actual = inspector.get_columns(table.name)
    if [c["name"] for c in actual] != list(table.c.keys()):
        raise IntegratedVersionError("Integrated version physical schema differs from its manifest")
    if any(not isinstance(c["type"], sa.Text) for c in actual[1:]):
        raise IntegratedVersionError("Integrated version column storage type was modified")
    if (not isinstance(actual[0]["type"], sa.BigInteger)
            or any(c["nullable"] != table.c[c["name"]].nullable for c in actual)):
        raise IntegratedVersionError("Integrated version column constraints differ from its manifest")
    expected_pk = [c.name for c in table.primary_key]
    if inspector.get_pk_constraint(table.name).get("constrained_columns") != expected_pk:
        raise IntegratedVersionError("Integrated version primary key differs from its manifest")
    unique_columns = [constraint.get("column_names") for constraint in
                      inspector.get_unique_constraints(table.name)]
    if expected_pk != [ROW_ORDINAL] and [ROW_ORDINAL] not in unique_columns:
        raise IntegratedVersionError("Integrated version ordinal uniqueness constraint is missing")
    count = int(db.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())
    if count != run.row_count or count != run.expected_rows:
        raise IntegratedVersionError("Integrated version physical count differs from sealed evidence")
    if not checksum:
        return table, count, None
    digest = _hasher(run)
    seen = 0
    mapping = run.schema_json["column_map"]
    for records in _iter_physical(db, table):
        for row in records:
            seen += 1
            if row[ROW_ORDINAL] != seen:
                raise IntegratedVersionError("Integrated version ordinal sequence is incomplete")
            _hash_row(digest, [row[p] for p in mapping.values()])
    if seen != count:
        raise IntegratedVersionError("Integrated version checksum scan is incomplete")
    return table, count, digest.hexdigest()


def _freeze(db, run: IntegratedSyncRun, table: sa.Table) -> None:
    conn = db.connection()
    quote = conn.dialect.identifier_preparer.quote
    name = quote(table.name)
    if conn.dialect.name == "sqlite":
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger = quote(f"{table.name}_{operation.lower()}")
            conn.exec_driver_sql(f"CREATE TRIGGER {trigger} BEFORE {operation} ON {name} "
                                 "BEGIN SELECT RAISE(ABORT, 'integrated version is immutable'); END")
    elif conn.dialect.name == "postgresql":
        function = quote("lake_iv_freeze_" + uuid.UUID(run.id).hex)
        conn.exec_driver_sql(f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                             "BEGIN RAISE EXCEPTION 'integrated version is immutable'; END; $$")
        conn.exec_driver_sql(f"CREATE TRIGGER {quote(table.name + '_immutable')} "
                             f"BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {name} "
                             f"FOR EACH STATEMENT EXECUTE FUNCTION {function}()")
    else:
        raise IntegratedVersionError("Integrated lake supports PostgreSQL, or SQLite tests only")


def seal_snapshot(db, run: IntegratedSyncRun, completion: dict, expected_checksum: str,
                  *, owner_token: str | None = None) -> DatasetVersion:
    if owner_token is not None:
        # Lock + fence before evaluating any completeness proof: a worker whose
        # run was reclaimed by recovery must not publish, even if its local
        # buffered evidence looked complete.
        run = _owned_run(db, run, owner_token)
    if (run.status != "loading" or run.version_id
            or completion.get("complete") is not True
            or completion.get("row_count") != run.row_count
            or completion.get("row_count") != run.expected_rows
            or completion.get("schema_fingerprint") != run.schema_json["schema_fingerprint"]
            or completion.get("snapshot_id") != run.snapshot_json["id"]):
        raise IntegratedVersionError("Integrated snapshot EOF/count/schema/snapshot proof is incomplete")
    dataset = (db.query(Dataset).filter(Dataset.id == run.dataset_id)
               .with_for_update(of=Dataset).populate_existing().one())
    if dataset.latest_version_id != run.base_version_id:
        raise IntegratedVersionError("A newer dataset version was published while this snapshot loaded; restart")
    table, _, checksum = _physical_evidence(db, run, checksum=True)
    if checksum != expected_checksum:
        raise IntegratedVersionError("Integrated version checksum differs from the complete source stream")
    _freeze(db, run, table)
    version_no = int(db.query(sa.func.max(DatasetVersion.version_no)).filter(
        DatasetVersion.dataset_id == dataset.id).scalar() or 0) + 1
    version = DatasetVersion(
        id=str(uuid.uuid4()), dataset_id=dataset.id, version_no=version_no,
        rowcount=run.row_count, checksum=checksum,
        data_blob=None, data_size=None, storage_uri=None,
    )
    db.add(version)
    db.flush()
    run.status = "sealed"
    run.version_id = version.id
    run.checksum = checksum
    run.completed_at = datetime.now(timezone.utc)
    run.snapshot_json = {**run.snapshot_json, "completion": copy.deepcopy(completion)}
    dataset.latest_version_id = version.id
    dataset.schema_json = {**(dataset.schema_json or {}), **copy.deepcopy(run.schema_json),
                           "integrated_asset_id": run.asset_id, "storage_kind": "integrated_relation"}
    db.add(DatasetVersionEvent(dataset_id=dataset.id, dataset_version_id=version.id,
                               event_type="version_published"))
    db.commit()
    from app.data_channel.datasets import cache
    cache.invalidate_overview()
    return version


def fail_snapshot(db, run_id: str) -> None:
    """Remove only an unsealed private relation; keep the durable failure receipt."""
    db.rollback()
    run = (db.query(IntegratedSyncRun).filter(IntegratedSyncRun.id == run_id)
           .with_for_update().populate_existing().one_or_none())
    if run is None or run.status == "sealed" or run.version_id:
        db.rollback()
        return
    if run.status == "failed":
        # The first durable failure or recovery receipt is immutable. A late
        # worker must not overwrite the audit written by a recovery winner.
        db.rollback()
        return
    if (not _TABLE_RE.fullmatch(run.physical_table)
            or run.physical_table != TABLE_PREFIX + uuid.UUID(run.id).hex):
        raise IntegratedVersionError("Invalid failed integrated relation identity")
    # A queued/starting run has no schema or physical table yet. Cleanup never
    # fabricates a completeness contract just to remove that private relation.
    sa.Table(run.physical_table, sa.MetaData()).drop(db.connection(), checkfirst=True)
    run.status = "failed"
    run.error = "INTEGRATED_SNAPSHOT_FAILED: restart from a fresh consistent source snapshot"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()


def _source_specs(source_schema: list[dict]) -> dict[str, dict]:
    if not isinstance(source_schema, list) or not source_schema:
        raise IntegratedVersionError("Integrated source schema is missing")
    specs: dict[str, dict] = {}
    for spec in source_schema:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            raise IntegratedVersionError("Integrated source schema is invalid")
        name = spec["name"]
        if name in specs:
            raise IntegratedVersionError("Integrated source schema contains duplicate columns")
        specs[name] = spec
    return specs


def _decode_source_value(value, spec: dict):
    """Reverse one canonical integrated TEXT scalar without lossy coercion."""
    if value is None:
        if not spec.get("nullable"):
            raise IntegratedVersionError(
                f"Integrated version contains NULL in non-nullable column {spec['name']}")
        return None
    kind = spec.get("type")
    try:
        if kind == "string":
            if not isinstance(value, str):
                raise ValueError
            return value
        if kind == "integer":
            if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+", value):
                raise ValueError
            result = int(value)
            source = str(spec.get("source_type") or "").lower()
            bits = next((bits for token, bits in (("bigint", 64), ("tinyint", 8),
                         ("mediumint", 24), ("smallint", 16), ("integer", 32),
                         ("int", 32)) if token in source), None)
            if spec.get("unsigned") and result < 0:
                raise ValueError
            if bits is not None and not (0 if spec.get("unsigned") else -(2 ** (bits - 1))) <= result <= (
                    2 ** bits - 1 if spec.get("unsigned") else 2 ** (bits - 1) - 1):
                raise ValueError
            return result
        if kind == "decimal":
            if not isinstance(value, str):
                raise ValueError
            result = Decimal(value)
            if not result.is_finite() or (spec.get("unsigned") and result < 0):
                raise ValueError
            digits = list(result.as_tuple().digits)
            exponent = result.as_tuple().exponent
            while len(digits) > 1 and digits[-1] == 0:
                digits.pop()
                exponent += 1
            if result and spec.get("scale") is not None and exponent < -spec["scale"]:
                raise ValueError
            if result and spec.get("precision") is not None:
                scale = spec.get("scale")
                integer_digits = max(len(digits) + exponent, 0)
                fractional_digits = max(-exponent, 0)
                if scale is not None and integer_digits > spec["precision"] - scale:
                    raise ValueError
                if scale is None and integer_digits + fractional_digits > spec["precision"]:
                    raise ValueError
            return result
        if kind == "float":
            if not isinstance(value, str):
                raise ValueError
            result = float(value)
            if not math.isfinite(result):
                raise ValueError
            return result
        if kind == "boolean":
            if value not in {"true", "false"}:
                raise ValueError
            return value == "true"
        if kind == "timestamp":
            result = datetime.fromisoformat(value)
            if bool(spec.get("timezone")) != (result.utcoffset() is not None):
                raise ValueError
            return result
        if kind == "date":
            result = date.fromisoformat(value)
            if isinstance(result, datetime):
                raise ValueError
            return result
        if kind == "time":
            result = time.fromisoformat(value)
            if bool(spec.get("timezone")) != (result.utcoffset() is not None):
                raise ValueError
            return result
        if kind == "json":
            def reject_constant(_):
                raise ValueError
            from app.data_channel.integrated.contracts import JsonText
            json.loads(value, parse_float=Decimal, parse_constant=reject_constant)
            return JsonText(value)
        if kind == "binary":
            return base64.b64decode(value, validate=True)
        raise ValueError
    except (ValueError, TypeError, OverflowError, InvalidOperation, UnicodeDecodeError):
        raise IntegratedVersionError(
            f"Integrated version contains invalid canonical value in column {spec['name']}") from None


def decode_version_row(row: dict, source_schema: list[dict]) -> dict:
    """Decode an immutable row against its frozen native source schema.

    Physical integrated relations deliberately store nullable TEXT to keep one
    portable representation.  This strict leaf restores native Python values
    only when the row has exactly the frozen column set; no ``None``/empty-string
    coercion, implicit numeric casts, or permissive JSON parsing is allowed.
    """
    specs = _source_specs(source_schema)
    if not isinstance(row, dict) or set(row) != set(specs):
        raise IntegratedVersionError("Integrated version row does not match its frozen schema")
    return {name: _decode_source_value(row[name], spec) for name, spec in specs.items()}


def _manifest(db, dataset_id: str, version_id: str):
    if not dataset_id or not version_id:
        raise IntegratedVersionError("An exact dataset ID and immutable version ID are required")
    version = db.get(DatasetVersion, version_id)
    if version is None or version.dataset_id != dataset_id:
        raise IntegratedVersionError("Integrated dataset version does not exist")
    run = db.query(IntegratedSyncRun).filter(IntegratedSyncRun.version_id == version_id).one_or_none()
    if (run is None or run.status != "sealed" or run.dataset_id != dataset_id
            or not run.checksum or run.checksum != version.checksum
            or run.row_count != version.rowcount or run.row_count != run.expected_rows
            or run.snapshot_json.get("completion", {}).get("complete") is not True):
        raise IntegratedVersionError("Integrated version has no valid sealed completeness manifest")
    # Validate the frozen schema again, not current Dataset.schema_json.
    IntegratedSnapshotGate({"schema": run.schema_json["source_schema"],
                            "primary_key": run.schema_json["primary_key_columns"],
                            "schema_fingerprint": run.schema_json["schema_fingerprint"]})
    # A frozen logical projection must still match the frozen physical schema.
    frozen_config = run.schema_json.get("field_config")
    if frozen_config is not None:
        try:
            fields.normalize_field_config(frozen_config, {
                "columns": run.schema_json["source_schema"],
                "primary_key": run.schema_json["primary_key_columns"]})
        except fields.FieldConfigError:
            raise IntegratedVersionError(
                "Integrated version field configuration is invalid") from None
    completion = run.snapshot_json["completion"]
    if (completion.get("row_count") != run.row_count
            or completion.get("schema_fingerprint") != run.schema_json["schema_fingerprint"]
            or completion.get("snapshot_id") != run.snapshot_json.get("id")):
        raise IntegratedVersionError("Integrated version sealed completion evidence was modified")
    return run, version


def _description(run, version) -> dict:
    base = copy.deepcopy(run.schema_json)
    description = {
        **base, "dataset_id": run.dataset_id,
        "version_id": version.id, "dataset_version_id": version.id,
        "version_no": version.version_no, "row_count": run.row_count,
        "checksum": run.checksum, "complete": True, "status": "sealed",
        "run_id": run.id, "asset_id": run.asset_id,
        "as_of": run.snapshot_json["started_at"], "snapshot": copy.deepcopy(run.snapshot_json),
        "cursor_semantics": "exclusive __row_seq integer in this immutable version, starting at 0",
    }
    # Configured versions present their frozen logical view through the same
    # keys consumers already read; the pristine physical metadata stays
    # available under physical_* keys and the fingerprint stays physical.
    frozen_config = base.get("field_config")
    if frozen_config is not None:
        view = fields.logical_view(
            {"columns": base["source_schema"], "primary_key": base["primary_key_columns"]},
            frozen_config)
        description.update({
            "columns": [c["name"] for c in view["columns"]],
            "columns_typed": [{"name": c["name"], "type": c["type"]} for c in view["columns"]],
            "source_schema": view["columns"],
            "primary_key": ",".join(view["primary_key"]),
            "primary_key_columns": view["primary_key"],
            "column_map": fields.projected_column_map(
                base["source_schema"], base["column_map"], frozen_config),
            "physical_columns": base["columns"],
            "physical_columns_typed": base["columns_typed"],
            "physical_source_schema": base["source_schema"],
            "physical_primary_key": base["primary_key"],
            "physical_primary_key_columns": base["primary_key_columns"],
            "physical_column_map": base["column_map"],
        })
    return description


def describe_version(db, dataset_id: str, version_id: str) -> dict:
    run, version = _manifest(db, dataset_id, version_id)
    _physical_evidence(db, run)
    return _description(run, version)


def get_version_relation(db, dataset_id: str, version_id: str) -> TypedVersionRelation:
    """Public same-platform-DB SQL port. Callers must authorize before entry.

    Scalar columns are nullable TEXT; source_schema is the mandatory exact type
    contract. Query planning must cast before numeric/temporal comparison/join,
    never assume TEXT identity has native source collation/type semantics.
    For configured versions the column_map is the frozen logical mapping, so
    callers address columns by their logical names.
    """
    run, version = _manifest(db, dataset_id, version_id)
    table, _, _ = _physical_evidence(db, run)
    mapping = dict(run.schema_json["column_map"])
    frozen_config = run.schema_json.get("field_config")
    if frozen_config is not None:
        mapping = fields.projected_column_map(
            run.schema_json["source_schema"], run.schema_json["column_map"], frozen_config)
    return TypedVersionRelation(table, mapping, table.c[ROW_ORDINAL], _description(run, version))


def iter_version_rows(db, dataset_id: str, version_id: str, *, batch_size=MAX_BATCH_SIZE,
                      columns: list[str] | None = None, after_row_key: int | None = None) -> Iterator[list[dict]]:
    size = _batch_size(batch_size)
    relation = get_version_relation(db, dataset_id, version_id)
    selected = list(relation.column_map) if columns is None else list(columns)
    if len(set(selected)) != len(selected) or any(c not in relation.column_map for c in selected):
        raise IntegratedVersionError("Version read requested unknown or duplicate columns")
    after = 0 if after_row_key is None else after_row_key
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise IntegratedVersionError("Version cursor must be a nonnegative immutable row ordinal")
    fields = [relation.row_ordinal] + [relation.table.c[relation.column_map[c]] for c in selected]
    for records in _iter_physical(db, relation.table, batch_size=size, after=after, columns=fields):
        yield [{c: row[relation.column_map[c]] for c in selected} for row in records]


def lookup_version_rows(db, dataset_id: str, version_id: str, *, column: str,
                        keys: list, batch_size=MAX_BATCH_SIZE) -> Iterator[list[dict]]:
    """Bounded equality lookup retaining every nonunique match, not a key dict.

    Keys are canonical stored text (or None). Native numeric joins should instead
    use get_version_relation + explicitly typed SQL casts.
    """
    size = _batch_size(batch_size)
    if not isinstance(keys, list) or len(keys) > MAX_BATCH_SIZE:
        raise IntegratedVersionError("Version lookup accepts at most 1000 keys")
    relation = get_version_relation(db, dataset_id, version_id)
    if column not in relation.column_map:
        raise IntegratedVersionError("Version lookup requested an unknown column")
    target = relation.table.c[relation.column_map[column]]
    predicate = target.in_([k for k in keys if k is not None])
    if None in keys:
        predicate = sa.or_(predicate, target.is_(None))
    for rows in _iter_physical(db, relation.table, batch_size=size, predicate=predicate):
        yield [{c: row[p] for c, p in relation.column_map.items()} for row in rows]
