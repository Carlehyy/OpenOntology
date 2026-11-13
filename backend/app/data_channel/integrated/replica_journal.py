"""Durable complete-transaction receipts for replica capture, never Sentinel.

The HTTP caller commits before acknowledging a batch. Row WAL positions are
identities only: a PostgreSQL transaction which commits later can contain lower
row LSNs. Only a validated END advances the ordered transaction cursor.

Identity rules per dialect:

- PostgreSQL xids wrap and are reused. Rows attach to the single unfinished
  transaction sharing their ``<xid>:`` prefix; a reused xid only earns a second
  journal row when its row LSN proves a new epoch (a transaction's row LSNs
  always precede its own commit record, so row LSN orders epochs). Duplicate
  ENDs are identified by the stored ``commit_lsn``, never by xid equality
  alone, and wrap must never make a new run reject.
- MySQL GTIDs are unique; grouping uses the full string. Complete-END ordering
  compares only the SourceRecord offset coordinates ``(file, pos[, event])``
  of consecutive ENDs — serial binlog delivery makes those monotonic — and
  never compares GTID sequence suffixes across UUIDs. Row-versus-END ordering
  is deliberately NOT invented: probe evidence only proves flushed offsets
  cover completed transactions, not that an END offset is the full commit
  position. Mixed old/new delivery is rejected by END-completeness plus stable
  record identity plus the END-coordinate frontier.
- Replay comparison ignores the envelope processing timestamps
  ``ts_ms/ts_us/ts_ns`` (they legitimately drift across connector restarts)
  and compares source identity + op + before/after + schema + transaction
  total order. ``transaction.source_position["source_event_at"]`` is an ISO
  timestamp derived from row ``source.ts_ms``/``source.ts_us`` — never from
  the END processing ts. It is the source EVENT time, not the commit time, so
  coverage treats it as a conservative lower bound of applied source event
  time; it stays empty when no real source timestamp exists.
- A replay inside the connector restart offset keeps the first receipt and
  never rewrites the newer recorded ``source_offset`` (liveness still moves).
  TRUNCATE events (``op=t``) and DDL/schema-change records are explicit
  rejections: a rebuild is the only safe response.

Pruning (interface for the runtime executor):

- ``prune_before(db, capture_id, *, commit_seq)`` deletes receipts/transactions
  only below a caller-proven safe replay floor and only when every bound run
  that may still apply has ``applied_position.commit_seq`` past them. Apply
  completion alone never justifies deletion.
- ``prune_applied(db, capture_id, *, confirmed_offset)`` derives that floor
  from the formal persisted Debezium offset materialized by the executor:
  a transaction is pruned only when its END position is STRICTLY before the
  recorded replayable start — a flushed offset can legally stop inside the
  last delivered complete transaction, so an equal coordinate keeps the whole
  transaction. postgresql requires ``lsn`` plus at least one of
  ``lsn_proc``/``lsn_commit`` as commit/process proof (a bare ``lsn`` may be
  a row LSN inside a transaction); without proof nothing is pruned. mysql
  takes ``{"file", "pos"[, "event"]}`` and only an event id present on BOTH
  sides breaks a same-(file,pos) tie.
- The floor persists as ``capture.committed_position["replay_floor"]`` (the
  position keys are never overwritten). Post-prune redelivery never backflows:
  MySQL rows at/below the floor are skipped directly (the binlog is serial, so
  a row below the floor provably belongs to a pruned transaction); PostgreSQL
  rows are always buffered first — WAL interleaving means a long transaction's
  row LSNs can precede the commit LSN of an already-pruned earlier transaction
  — and only the END proves staleness (``commit_lsn <= floor``), dropping the
  buffered replay atomically with its pending bytes. A buffered transaction
  belongs to such an END only when every buffered row precedes its commit
  record; a newer transaction above the floor is never dropped or skipped.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa

from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture, IntegratedReplicaReceipt, IntegratedReplicaTransaction,
)
from app.data_channel.integrated.replica_models import IntegratedReplicaRun
from app.shared.exact_json import dumps_exact

MAX_BATCH_RECORDS = 2048
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_PENDING_BYTES = 1024 * 1024 * 1024
TRANSPORT_SCHEMA = "io.openontology.replica.TransportRecord"
# Envelope processing timestamps drift across connector restarts; they are
# excluded from replay comparison and never prove a record changed.
_PROCESSING_TS_KEYS = ("ts_ms", "ts_us", "ts_ns")
# Runs in these statuses may still apply receipts and gate pruning. Retired,
# failed and cancelled runs never apply again and never block the floor.
_PRUNE_BLOCKING_RUN_STATUSES = ("queued", "capturing", "loading", "catching_up", "active")


class ReplicaJournalError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def _reject_constant(value):
    raise ReplicaJournalError("INVALID_TRANSPORT_JSON")


def loads(raw):
    return json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)


def _canonical(value) -> str:
    """Deterministic exact-JSON form: wire key order cannot change identity.

    Duplicate detection must compare semantics, not first-seen byte order: the
    same source event redelivered with reordered JSON keys is a replay, never
    a conflict. Decimal keeps its exact literal token (``1.10`` != ``1.1``).
    """
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key) + ":" + _canonical(item)
            for key, item in sorted(value.items())) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value)


def _replay_form(envelope) -> str:
    """Canonical comparison form: schema + payload minus processing timestamps.

    ``envelope`` is the journaled ``{"schema", "payload"}`` event (stored text
    is parsed with ``loads`` first). Only source identity, op, before/after,
    schema and transaction total order decide replay equality.
    """
    payload = envelope.get("payload")
    scrubbed = payload
    if isinstance(payload, dict):
        scrubbed = {key: item for key, item in payload.items()
                    if key not in _PROCESSING_TS_KEYS}
    return _canonical({"schema": envelope.get("schema"), "payload": scrubbed})


def _is_initial_snapshot_notification(event) -> bool:
    """Debezium signal-framework notification (``aggregate_type`` identifies
    the snapshot kind; STARTED/DATA_COLLECTIONS_RESOLVED legitimately carry an
    empty ``offset_json='{}'``). Identified by payload shape — the event
    schema name is not stable across connector versions."""
    return isinstance(event, dict) and event.get("aggregate_type") == "Initial Snapshot"


def _is_schema_change(envelope, payload) -> bool:
    """DDL/schema-change records must be blocked explicitly.

    Debezium MySQL emits a ``SchemaChange`` value (``ddl``/``tableChanges``,
    no ``op``) when schema changes are included; applying it as a no-op would
    leave a healthy-looking replica on a frozen schema. TRUNCATE is detected by
    ``op == "t"`` at the operation gate; DDL is detected here by schema name or
    its unambiguous top-level payload keys (a source column named ``ddl`` stays
    nested under ``after``/``before`` and never reaches this check).
    """
    schema = envelope.get("schema") if isinstance(envelope, dict) else None
    name = schema.get("name") if isinstance(schema, dict) else None
    if isinstance(name, str) and name.endswith(".SchemaChange"):
        return True
    if not isinstance(payload, dict) or payload.get("op") or payload.get("status"):
        return False
    return "ddl" in payload or "tableChanges" in payload


def unpack(record: dict) -> tuple[dict, dict, dict | None]:
    """Retain the Connect value schema and exact sourceOffset from our SMT.

    Only initial-snapshot notifications may carry an empty offset: every row,
    transaction or heartbeat record still requires a non-empty sourceOffset.
    """
    if not isinstance(record, dict):
        raise ReplicaJournalError("INVALID_TRANSPORT_RECORD")
    schema, payload = record.get("schema"), record.get("payload")
    if not isinstance(schema, dict) or schema.get("name") != TRANSPORT_SCHEMA or not isinstance(payload, dict):
        raise ReplicaJournalError("TRANSPORT_METADATA_REQUIRED")
    try:
        offset, partition = loads(payload["offset_json"]), loads(payload["partition_json"])
        fields = {field["field"]: field for field in schema["fields"]}
        event = payload.get("event")
        envelope = {"schema": fields["event"], "payload": event} if event is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplicaJournalError("INVALID_TRANSPORT_RECORD") from exc
    if not isinstance(offset, dict) or not isinstance(partition, dict):
        raise ReplicaJournalError("SOURCE_OFFSET_REQUIRED")
    if not offset and not _is_initial_snapshot_notification(event):
        raise ReplicaJournalError("SOURCE_OFFSET_REQUIRED")
    return partition, offset, envelope


def _pg_tx_parts(value, code: str) -> tuple[str, int]:
    """``<xid>:<lsn>`` from PG transaction metadata; both parts numeric."""
    if not isinstance(value, str) or len(value) > 200:
        raise ReplicaJournalError(code)
    xid, _, lsn_text = value.partition(":")
    if not xid.isdigit() or not lsn_text.isdigit():
        raise ReplicaJournalError(code)
    return xid, int(lsn_text)


def _mysql_tx_id(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ReplicaJournalError("TRANSACTION_METADATA_REQUIRED")
    return value


def _pg_candidates(db, capture_id: str, xid: str) -> list:
    """Transactions sharing an xid: the bare first-epoch key plus suffixed wrap
    epochs (``<xid>:<first-row-lsn>``). xid is digits-only, so the trailing
    colon keeps the LIKE prefix collision-free (``100:%`` never matches
    ``1000:...``)."""
    return db.scalars(sa.select(IntegratedReplicaTransaction).where(
        IntegratedReplicaTransaction.capture_id == capture_id,
        sa.or_(IntegratedReplicaTransaction.source_transaction_id == xid,
               IntegratedReplicaTransaction.source_transaction_id.like(f"{xid}:%")))
        .execution_options(populate_existing=True)).all()


def _tx_commit_lsn(transaction) -> int | None:
    position = transaction.source_position
    if isinstance(position, dict):
        value = position.get("commit_lsn")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _offset_coords(offset) -> tuple[str, int, int] | None:
    """MySQL binlog coordinates ``(file, pos, event)``; ``event`` is 0 when the
    record carries no event id. None when the offset is not usable."""
    if not isinstance(offset, dict):
        return None
    file, pos = offset.get("file"), offset.get("pos")
    if not isinstance(file, str) or not file or isinstance(pos, bool) or not isinstance(pos, int):
        return None
    event = offset.get("event")
    if isinstance(event, bool) or not isinstance(event, int):
        event = 0
    return (file, pos, event)


def _position_coords(position) -> tuple[str, int, int] | None:
    if not isinstance(position, dict):
        return None
    coords = _offset_coords(position.get("offset"))
    if coords is None:
        coords = _offset_coords(
            {"file": position.get("file"), "pos": position.get("pos")})
    return coords


def _not_ahead(coords, previous) -> bool:
    """``coords`` fails to prove strictly ahead of ``previous`` coordinates.

    The trailing event id only participates when both sides carry one: a
    restart must not be rejected merely because the connector stopped (or
    started) recording the ``event`` key.
    """
    if coords is None or previous is None:
        return False
    if coords[0] == previous[0] and coords[1] == previous[1] and (
            coords[2] == 0 or previous[2] == 0):
        return True
    return coords <= previous


def _pg_position(offset: dict, commit_lsn: int) -> dict:
    return {"dialect": "postgresql", "commit_lsn": commit_lsn, "offset": offset}


def _mysql_position(offset: dict) -> dict:
    if not isinstance(offset.get("file"), str) or type(offset.get("pos")) is not int:
        raise ReplicaJournalError("COMMIT_POSITION_REQUIRED")
    return {"dialect": "mysql", "file": offset["file"], "pos": offset["pos"],
            "gtids": offset.get("gtids"), "offset": offset}


def _replay_floor(capture) -> dict | None:
    position = capture.committed_position
    if not isinstance(position, dict):
        return None
    floor = position.get("replay_floor")
    if (not isinstance(floor, dict)
            or isinstance(floor.get("commit_seq"), bool)
            or not isinstance(floor.get("commit_seq"), int)
            or not isinstance(floor.get("position"), dict)):
        return None
    return floor


def _floor_lsn(floor) -> int | None:
    if floor is None:
        return None
    value = floor.get("position", {}).get("commit_lsn")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _capture(db, capture_id: str) -> IntegratedReplicaCapture:
    row = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == capture_id).with_for_update()
        .execution_options(populate_existing=True))
    if row is None or row.status not in {"starting", "streaming", "stale"}:
        raise ReplicaJournalError("CAPTURE_NOT_ACCEPTING")
    return row


def _record_identity(partition: dict, payload: dict, txid_value: str, ordinal: int) -> dict:
    source = payload.get("source") or {}
    return {"partition": partition, "source": {
        key: source.get(key) for key in ("lsn", "txId", "file", "pos", "row", "gtid", "schema", "db", "table")},
        "transaction": txid_value, "ordinal": ordinal, "operation": payload["op"]}


def _existing_receipt(db, capture, envelope, partition, payload, txid_value, ordinal):
    """Receipt lookup by stable record identity; a byte-identical identity
    with drifted processing timestamps (or reordered wire keys) is a replay,
    changed content is ``REPLAY_RECORD_CHANGED``."""
    identity = _record_identity(partition, payload, txid_value, ordinal)
    record_id = hashlib.sha256(_canonical(identity).encode()).hexdigest()
    existing = db.scalar(sa.select(IntegratedReplicaReceipt).where(
        IntegratedReplicaReceipt.capture_id == capture.id,
        IntegratedReplicaReceipt.record_id == record_id))
    if existing is not None:
        if _replay_form(loads(existing.envelope_json)) != _replay_form(envelope):
            raise ReplicaJournalError("REPLAY_RECORD_CHANGED")
    return existing, record_id


def _note_row_progress(transaction, payload, row_lsn: int | None) -> None:
    """Track per-transaction row progress on the unfinished transaction:
    newest ``source.ts_us``/``ts_ms`` (epoch microseconds, promoted by the END
    to ``source_event_at``) and the min/max PG row LSN. The max row LSN decides
    END membership: a transaction's own commit record always follows every one
    of its rows, so a buffered transaction belongs to an END only when all its
    rows precede that commit LSN. The min row LSN proves a lower-LSN replay is
    not a row of this transaction (WAL order), catching stale rows from an
    already-pruned older xid epoch. Stashed under ``source_position`` only while
    uncommitted — the END replaces it with the final position dict."""
    source = payload.get("source") or {}
    total_us = None
    for key, factor in (("ts_us", 1), ("ts_ms", 1000)):
        raw = source.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            total_us = raw * factor
            break
    current = transaction.source_position if isinstance(transaction.source_position, dict) else {}
    updated = dict(current)
    changed = False
    if total_us is not None:
        best = updated.get("row_ts_us")
        if not (isinstance(best, int) and not isinstance(best, bool) and best >= total_us):
            updated["row_ts_us"] = total_us
            changed = True
    if row_lsn is not None:
        best = updated.get("row_lsn_max")
        if not (isinstance(best, int) and not isinstance(best, bool) and best >= row_lsn):
            updated["row_lsn_max"] = row_lsn
            changed = True
        # Rows of a transaction are delivered in increasing WAL order; the
        # lowest observed row LSN proves a lower-LSN replay is not a row of
        # this unfinished transaction (it is an older, already-pruned epoch).
        low = updated.get("row_lsn_min")
        if not (isinstance(low, int) and not isinstance(low, bool) and low <= row_lsn):
            updated["row_lsn_min"] = row_lsn
            changed = True
    if changed:
        transaction.source_position = updated


def _row_lsn_max(transaction) -> int | None:
    stash = transaction.source_position if isinstance(transaction.source_position, dict) else {}
    value = stash.get("row_lsn_max")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _row_lsn_min(transaction) -> int | None:
    stash = transaction.source_position if isinstance(transaction.source_position, dict) else {}
    value = stash.get("row_lsn_min")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _source_event_at(transaction) -> datetime | None:
    """Newest row source-event time; the source EVENT time is a conservative
    lower bound for coverage, never a claimed commit time."""
    stash = transaction.source_position if isinstance(transaction.source_position, dict) else {}
    raw = stash.get("row_ts_us")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        return None
    seconds, micros = divmod(raw, 1_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=micros)


def _store_receipt(db, capture, transaction, envelope, payload, ordinal: int,
                   record_id: str, raw: str, size: int, *,
                   row_lsn: int | None = None) -> None:
    if capture.pending_bytes + size > MAX_PENDING_BYTES:
        raise ReplicaJournalError("REPLICA_BACKLOG_CAPACITY", retryable=True)
    capture.received_seq += 1
    capture.pending_bytes += size
    db.add(IntegratedReplicaReceipt(
        capture_id=capture.id, sequence=capture.received_seq,
        transaction_id=transaction.id, record_id=record_id,
        ordinal=ordinal, envelope_json=raw, size_bytes=size))
    transaction.event_count += 1
    _note_row_progress(transaction, payload, row_lsn)
    db.flush()


def _drop_buffered(db, capture, transaction) -> int:
    """Atomically discard a fully buffered replay whose END proved it stale
    (commit at/below the replay floor): receipts, transaction row and
    pending_bytes move together, so a stale replay never backflows values."""
    total = db.execute(sa.select(
        sa.func.coalesce(sa.func.sum(IntegratedReplicaReceipt.size_bytes), 0))
        .where(IntegratedReplicaReceipt.transaction_id == transaction.id)).scalar()
    db.execute(sa.delete(IntegratedReplicaReceipt).where(
        IntegratedReplicaReceipt.transaction_id == transaction.id)
        .execution_options(synchronize_session=False))
    db.delete(transaction)
    if total > capture.pending_bytes:
        raise ReplicaJournalError("PRUNE_STATE_INVALID")
    capture.pending_bytes -= total
    db.flush()
    return total


def _commit_end(db, capture, transaction, position: dict) -> None:
    """Advance the ordered cursor; shared by row-backed and empty END commits."""
    previous = capture.committed_position if isinstance(capture.committed_position, dict) else {}
    event_at = _source_event_at(transaction)
    if event_at is not None:
        position = {**position, "source_event_at": event_at.isoformat()}
    floor = previous.get("replay_floor")
    capture.received_seq += 1
    capture.committed_seq = capture.received_seq
    capture.committed_position = {**position, **({"replay_floor": floor} if floor is not None else {})}
    transaction.commit_seq = capture.committed_seq
    transaction.source_position = position
    db.flush()


def _pg_row(db, capture, partition, payload, envelope, ordinal: int) -> bool:
    """Accept one c/u/d row; True when journaled, False when a safe skip."""
    xid, row_lsn = _pg_tx_parts((payload.get("transaction") or {}).get("id"),
                                "TRANSACTION_METADATA_REQUIRED")
    raw = dumps_exact(envelope)
    size = len(raw.encode("utf-8"))
    if size > MAX_RECORD_BYTES:
        raise ReplicaJournalError("SOURCE_RECORD_TOO_LARGE")
    txid_value = (payload.get("transaction") or {}).get("id")
    existing, record_id = _existing_receipt(
        db, capture, envelope, partition, payload, txid_value, ordinal)
    if existing is not None:
        return False
    candidates = _pg_candidates(db, capture.id, xid)
    unfinished = next((t for t in candidates if t.commit_seq is None), None)
    # PG WAL interleaves: a long transaction's row LSNs can precede the commit
    # LSN of an already-pruned earlier transaction (different xid), so rows are
    # never filtered against the global replay floor — they are always buffered.
    # The comparison below is scoped to this xid's own committed epochs: a new
    # epoch's rows always follow the previous epoch's END, while a replayed
    # pre-END row of a pruned epoch falls at/below it.
    committed_lsn = max((_tx_commit_lsn(t) or 0 for t in candidates if t.commit_seq is not None),
                        default=0)
    if unfinished is not None:
        if row_lsn <= committed_lsn:
            # A row at/below a committed epoch's own END belongs to that older
            # xid epoch. When its receipt is gone (pruned), a restart replay
            # re-delivers it: it must never be attached to the new epoch's
            # unfinished transaction, which would corrupt its event_count /
            # ordinal set and make the real END fail as incomplete.
            return False
        buffered_min = _row_lsn_min(unfinished)
        if buffered_min is not None and row_lsn < buffered_min:
            # Rows of one transaction arrive in increasing WAL order, so a
            # lower-LSN replay cannot belong to the buffered transaction. This
            # is the same stale-epoch guard after the committed boundary itself
            # was pruned (no committed candidate row remains to compare with).
            return False
    else:
        if row_lsn <= committed_lsn:
            # A row below a kept committed transaction's own commit LSN is a
            # late row of that transaction, never a new epoch.
            raise ReplicaJournalError("TRANSACTION_REPLAY_INCOMPLETE")
        key = xid if not candidates else f"{xid}:{row_lsn}"
        unfinished = IntegratedReplicaTransaction(
            id=str(uuid.uuid4()), capture_id=capture.id,
            source_transaction_id=key, event_count=0)
        db.add(unfinished)
        db.flush()
    _store_receipt(db, capture, unfinished, envelope, payload, ordinal,
                   record_id, raw, size, row_lsn=row_lsn)
    return True


def _mysql_row(db, capture, partition, offset, payload, envelope, ordinal: int, floor) -> bool:
    txkey = _mysql_tx_id((payload.get("transaction") or {}).get("id"))
    raw = dumps_exact(envelope)
    size = len(raw.encode("utf-8"))
    if size > MAX_RECORD_BYTES:
        raise ReplicaJournalError("SOURCE_RECORD_TOO_LARGE")
    existing, record_id = _existing_receipt(
        db, capture, envelope, partition, payload, txkey, ordinal)
    if existing is not None:
        return False
    transaction = db.scalar(sa.select(IntegratedReplicaTransaction).where(
        IntegratedReplicaTransaction.capture_id == capture.id,
        IntegratedReplicaTransaction.source_transaction_id == txkey))
    if transaction is not None and transaction.commit_seq is not None:
        raise ReplicaJournalError("TRANSACTION_REPLAY_INCOMPLETE")
    if transaction is None and _not_ahead(
            _offset_coords(offset),
            _position_coords(floor.get("position") if floor else None)):
        # MySQL binlog is serial: a row at/below the replay floor with no
        # journal row belongs to a pruned transaction.
        return False
    if transaction is None:
        transaction = IntegratedReplicaTransaction(
            id=str(uuid.uuid4()), capture_id=capture.id,
            source_transaction_id=txkey, event_count=0)
        db.add(transaction)
        db.flush()
    _store_receipt(db, capture, transaction, envelope, payload, ordinal,
                   record_id, raw, size)
    return True


def _pg_end(db, capture, offset, payload, floor) -> bool:
    xid, commit_lsn = _pg_tx_parts(payload.get("id"), "COMMIT_POSITION_REQUIRED")
    expected = payload.get("event_count")
    if type(expected) is not int or expected < 0:
        raise ReplicaJournalError("TRANSACTION_COUNT_REQUIRED")
    previous = capture.committed_position if isinstance(capture.committed_position, dict) else {}
    prev_lsn = previous.get("commit_lsn")
    prev_lsn = prev_lsn if isinstance(prev_lsn, int) and not isinstance(prev_lsn, bool) else None
    candidates = _pg_candidates(db, capture.id, xid)
    # END duplicates are identified by the stored commit LSN: an xid match
    # alone would misfire across wrap epochs.
    for transaction in candidates:
        if transaction.commit_seq is not None and _tx_commit_lsn(transaction) == commit_lsn:
            if transaction.event_count != expected:
                raise ReplicaJournalError("REPLAY_RECORD_CHANGED")
            return False
    unfinished = next((t for t in candidates if t.commit_seq is None), None)
    floor_lsn = _floor_lsn(floor)
    if floor_lsn is not None and commit_lsn <= floor_lsn:
        # END of an already-pruned transaction. Its redelivered rows were
        # buffered unfiltered (long transactions legitimately carry row LSNs
        # below the floor); the END now proves staleness. The buffered rows
        # belong to this END only when every one precedes its commit record —
        # otherwise they are a newer transaction's and stay untouched.
        if unfinished is not None:
            lsn_max = _row_lsn_max(unfinished)
            if lsn_max is not None and lsn_max < commit_lsn:
                _drop_buffered(db, capture, unfinished)
        return False
    if unfinished is not None:
        if unfinished.event_count != expected:
            raise ReplicaJournalError("TRANSACTION_INCOMPLETE")
        ordinals = list(db.scalars(sa.select(IntegratedReplicaReceipt.ordinal).where(
            IntegratedReplicaReceipt.transaction_id == unfinished.id)
            .order_by(IntegratedReplicaReceipt.ordinal)))
        if ordinals != list(range(1, expected + 1)):
            raise ReplicaJournalError("TRANSACTION_INCOMPLETE")
        if prev_lsn is not None and commit_lsn <= prev_lsn:
            raise ReplicaJournalError("COMMIT_ORDER_CHANGED")
        _commit_end(db, capture, unfinished, _pg_position(offset, commit_lsn))
        return True
    if expected == 0 and (prev_lsn is None or commit_lsn > prev_lsn):
        key = xid if not any(t.source_transaction_id == xid for t in candidates) \
            else f"{xid}:{commit_lsn}"
        transaction = IntegratedReplicaTransaction(
            id=str(uuid.uuid4()), capture_id=capture.id,
            source_transaction_id=key, event_count=0)
        db.add(transaction)
        db.flush()
        _commit_end(db, capture, transaction, _pg_position(offset, commit_lsn))
        return True
    if prev_lsn is not None and commit_lsn <= prev_lsn:
        raise ReplicaJournalError("COMMIT_ORDER_CHANGED")
    raise ReplicaJournalError("TRANSACTION_INCOMPLETE")


def _mysql_end(db, capture, offset, payload, floor) -> bool:
    txkey = _mysql_tx_id(payload.get("id"))
    expected = payload.get("event_count")
    if type(expected) is not int or expected < 0:
        raise ReplicaJournalError("TRANSACTION_COUNT_REQUIRED")
    position = _mysql_position(offset)
    coords = _offset_coords(offset)
    order = _position_coords(capture.committed_position)
    transaction = db.scalar(sa.select(IntegratedReplicaTransaction).where(
        IntegratedReplicaTransaction.capture_id == capture.id,
        IntegratedReplicaTransaction.source_transaction_id == txkey))
    if transaction is not None and transaction.commit_seq is not None:
        if transaction.event_count != expected:
            raise ReplicaJournalError("REPLAY_RECORD_CHANGED")
        return False
    if transaction is not None:
        if transaction.event_count != expected:
            raise ReplicaJournalError("TRANSACTION_INCOMPLETE")
        ordinals = list(db.scalars(sa.select(IntegratedReplicaReceipt.ordinal).where(
            IntegratedReplicaReceipt.transaction_id == transaction.id)
            .order_by(IntegratedReplicaReceipt.ordinal)))
        if ordinals != list(range(1, expected + 1)):
            raise ReplicaJournalError("TRANSACTION_INCOMPLETE")
        # Only END-vs-END offset coordinates order transactions (serial binlog
        # delivery); GTID suffixes are never compared, least of all across
        # UUIDs. The END offset is not assumed to be the full commit position.
        if _not_ahead(coords, order):
            raise ReplicaJournalError("COMMIT_ORDER_CHANGED")
        _commit_end(db, capture, transaction, position)
        return True
    if _not_ahead(coords, _position_coords(floor.get("position") if floor else None)):
        return False  # END of a pruned transaction: safe skip, no backflow
    if expected == 0 and not _not_ahead(coords, order):
        transaction = IntegratedReplicaTransaction(
            id=str(uuid.uuid4()), capture_id=capture.id,
            source_transaction_id=txkey, event_count=0)
        db.add(transaction)
        db.flush()
        _commit_end(db, capture, transaction, position)
        return True
    if _not_ahead(coords, order):
        raise ReplicaJournalError("COMMIT_ORDER_CHANGED")
    raise ReplicaJournalError("TRANSACTION_INCOMPLETE")


def append_batch(db, capture_id: str, records: list[dict], *, dialect: str) -> dict:
    """Persist exact receipts and END proofs in caller's transaction, no commit."""
    if dialect not in {"postgresql", "mysql"}:
        raise ReplicaJournalError("UNSUPPORTED_SOURCE")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH_RECORDS:
        raise ReplicaJournalError("INVALID_TRANSPORT_BATCH")
    capture = _capture(db, capture_id)
    accepted = duplicates = 0
    floor = _replay_floor(capture)
    for wrapped in records:
        partition, offset, envelope = unpack(wrapped)
        handled = None
        if partition.get("server") is None:
            raise ReplicaJournalError("SOURCE_PARTITION_REQUIRED")
        expected_server = "oo_replica_" + uuid.UUID(capture.id).hex
        if partition["server"] != expected_server:
            raise ReplicaJournalError("SOURCE_PARTITION_CHANGED")
        payload = envelope["payload"] if envelope is not None else None
        if payload is not None and not isinstance(payload, dict):
            raise ReplicaJournalError("INVALID_TRANSPORT_RECORD")
        if payload is not None and _is_schema_change(envelope, payload):
            # DDL/schema change is never a data no-op: rebuild is the only
            # safe path. TRUNCATE is rejected below via op == "t".
            raise ReplicaJournalError("SOURCE_SCHEMA_CHANGED")
        if payload and payload.get("op") in {"r", "t"}:
            # Initial values use the native snapshot lane. Truncate/DDL must
            # explicitly rebuild, never leave a seemingly healthy old table.
            raise ReplicaJournalError("UNEXPECTED_SNAPSHOT" if payload["op"] == "r" else "SOURCE_TRUNCATED")
        if payload and payload.get("op") in {"c", "u", "d"}:
            # Transaction metadata gates shape before ordering: a row without
            # a usable transaction id is a metadata violation, not an order one.
            txid_value = (payload.get("transaction") or {}).get("id")
            if dialect == "postgresql":
                _pg_tx_parts(txid_value, "TRANSACTION_METADATA_REQUIRED")
            else:
                _mysql_tx_id(txid_value)
            ordinal = (payload.get("transaction") or {}).get("total_order")
            if type(ordinal) is not int or ordinal < 1:
                raise ReplicaJournalError("TRANSACTION_ORDER_REQUIRED")
            if dialect == "postgresql":
                handled = _pg_row(db, capture, partition, payload, envelope, ordinal)
            else:
                handled = _mysql_row(db, capture, partition, offset, payload, envelope, ordinal, floor)
            accepted, duplicates = (accepted + 1, duplicates) if handled \
                else (accepted, duplicates + 1)
        elif payload and payload.get("status") == "END":
            if dialect == "postgresql":
                handled = _pg_end(db, capture, offset, payload, floor)
            else:
                handled = _mysql_end(db, capture, offset, payload, floor)
            accepted, duplicates = (accepted + 1, duplicates) if handled \
                else (accepted, duplicates + 1)
        elif payload and payload.get("op"):
            raise ReplicaJournalError("SOURCE_OPERATION_UNSUPPORTED")
        # Replays still prove the pipeline is alive: liveness bookkeeping must
        # advance for every delivered record, duplicates included. A duplicate
        # receipt/END, however, is an older restart-offset replay and must not
        # overwrite the already journaled (newer) source offset. A leaked
        # notification carries no position and must not clobber the offset.
        if offset and handled is not False:
            capture.source_offset = offset
        capture.heartbeat_at = datetime.now(timezone.utc)
    db.flush()
    return {"accepted": accepted, "duplicates": duplicates,
            "received_seq": capture.received_seq, "committed_seq": capture.committed_seq}


# ---------------------------------------------------------------------------
# Pruning: replay-floor-proven receipt recycling
# ---------------------------------------------------------------------------

def _run_cursor(run) -> int:
    position = run.applied_position
    seq = position.get("commit_seq") if isinstance(position, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return 0
    return seq


def prune_before(db, capture_id: str, *, commit_seq: int) -> dict:
    """Delete receipts/transactions below a caller-proven safe replay floor.

    ``commit_seq`` must come from the executor reading the connector's formal
    persisted Debezium offset: a restart resumes after that position, so
    transactions with ``commit_seq <= floor`` can no longer be redelivered.
    Deletion additionally requires every bound run that may still apply to
    have ``applied_position["commit_seq"]`` at/below-covering the transaction;
    apply completion alone never proves receipts deletable. The floor persists
    monotonically in ``capture.committed_position["replay_floor"]`` (existing
    position keys untouched) so any post-prune redelivery at/below it is
    acknowledged as a stale duplicate instead of re-journaled.
    """
    if isinstance(commit_seq, bool) or not isinstance(commit_seq, int) or commit_seq < 1:
        raise ReplicaJournalError("PRUNE_FLOOR_INVALID")
    capture = _capture(db, capture_id)
    existing = _replay_floor(capture)
    if existing is not None and existing["commit_seq"] > commit_seq:
        return {"pruned_transactions": 0, "pruned_receipts": 0,
                "released_bytes": 0, "replay_floor": existing}
    boundary = db.scalar(sa.select(IntegratedReplicaTransaction).where(
        IntegratedReplicaTransaction.capture_id == capture.id,
        IntegratedReplicaTransaction.commit_seq.is_not(None),
        IntegratedReplicaTransaction.commit_seq <= commit_seq)
        .order_by(IntegratedReplicaTransaction.commit_seq.desc()).limit(1))
    floor = existing
    if boundary is not None and isinstance(boundary.source_position, dict) and (
            floor is None or boundary.commit_seq >= floor["commit_seq"]):
        floor = {"commit_seq": boundary.commit_seq,
                 "position": dict(boundary.source_position)}
    if floor is None:
        return {"pruned_transactions": 0, "pruned_receipts": 0,
                "released_bytes": 0, "replay_floor": None}
    runs = db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.capture_id == capture.id,
        IntegratedReplicaRun.status.in_(_PRUNE_BLOCKING_RUN_STATUSES))).all()
    cursor = min((_run_cursor(run) for run in runs), default=None)
    prunable = db.scalars(sa.select(IntegratedReplicaTransaction).where(
        IntegratedReplicaTransaction.capture_id == capture.id,
        IntegratedReplicaTransaction.commit_seq.is_not(None),
        IntegratedReplicaTransaction.commit_seq <= floor["commit_seq"])).all()
    released = pruned_receipts = pruned_transactions = 0
    for transaction in prunable:
        if cursor is not None and transaction.commit_seq > cursor:
            continue
        totals = db.execute(sa.select(
            sa.func.coalesce(sa.func.sum(IntegratedReplicaReceipt.size_bytes), 0),
            sa.func.count()).where(
            IntegratedReplicaReceipt.transaction_id == transaction.id)).one()
        released += totals[0]
        pruned_receipts += totals[1]
        db.execute(sa.delete(IntegratedReplicaReceipt).where(
            IntegratedReplicaReceipt.transaction_id == transaction.id)
            .execution_options(synchronize_session=False))
        db.delete(transaction)
        pruned_transactions += 1
    if released > capture.pending_bytes:
        raise ReplicaJournalError("PRUNE_STATE_INVALID")
    capture.pending_bytes -= released
    position = capture.committed_position if isinstance(capture.committed_position, dict) else {}
    capture.committed_position = {**position, "replay_floor": floor}
    db.flush()
    return {"pruned_transactions": pruned_transactions, "pruned_receipts": pruned_receipts,
            "released_bytes": released, "replay_floor": floor}


def _strictly_before(coords, boundary) -> bool:
    """``coords`` is provably strictly before ``boundary`` binlog coordinates.

    Same-(file,pos) ties are only broken by an event id carried on BOTH
    sides; any other tie keeps the transaction (the flushed offset can stop
    inside the last delivered complete transaction, so equality is never
    proof that replay cannot resume there).
    """
    if coords is None or boundary is None:
        return False
    if coords[0] != boundary[0]:
        return coords < boundary
    if coords[1] != boundary[1]:
        return coords[1] < boundary[1]
    return coords[2] != 0 and boundary[2] != 0 and coords[2] < boundary[2]


def prune_applied(db, capture_id: str, *, confirmed_offset: dict) -> dict:
    """Prune receipts the connector's formal persisted offset proves replay-free.

    ``confirmed_offset`` is the persisted Debezium source offset materialized
    by the executor from Java ReadOffsets. A transaction is pruned only when
    its END position is STRICTLY before the recorded replayable start — an
    equal coordinate keeps the whole transaction:

    - postgresql: the map must carry ``lsn`` plus at least one of
      ``lsn_proc``/``lsn_commit`` as process/commit proof (a bare ``lsn`` can
      be a row LSN inside a transaction); the replayable start is the minimum
      of the confirmed markers. Without proof nothing is pruned.
    - mysql: ``{"file": str, "pos": int[, "event": int], ...}``; a flushed
      offset can legally stop inside the last delivered complete transaction
      (END offsets may still carry transaction resume info), so only
      strictly-before prunes.

    Delegates the run gating and floor persistence to :func:`prune_before`.
    """
    if not isinstance(confirmed_offset, dict) or not confirmed_offset:
        raise ReplicaJournalError("PRUNE_OFFSET_INVALID")
    capture = _capture(db, capture_id)
    empty = {"pruned_transactions": 0, "pruned_receipts": 0,
             "released_bytes": 0, "replay_floor": _replay_floor(capture)}
    position = capture.committed_position
    dialect = position.get("dialect") if isinstance(position, dict) else None
    if dialect not in {"postgresql", "mysql"}:
        return empty
    if dialect == "postgresql":
        def marker(key):
            value = confirmed_offset.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None
        if all(marker(key) is None for key in ("lsn", "lsn_proc", "lsn_commit")):
            raise ReplicaJournalError("PRUNE_OFFSET_INVALID")
        proof = [value for value in (marker("lsn_proc"), marker("lsn_commit"))
                 if value is not None]
        if not proof:
            return empty  # 无commit/process证据：宁可回收量为零，不做假安全
        lsn = marker("lsn")
        safe_start = min(proof + ([lsn] if lsn is not None else []))
        def proven(stored) -> bool:
            value = stored.get("commit_lsn") if isinstance(stored, dict) else None
            return isinstance(value, int) and not isinstance(value, bool) and value < safe_start
    else:
        boundary = _offset_coords(confirmed_offset)
        if boundary is None:
            raise ReplicaJournalError("PRUNE_OFFSET_INVALID")
        def proven(stored) -> bool:
            return _strictly_before(_position_coords(stored), boundary)
    committed = db.execute(sa.select(
        IntegratedReplicaTransaction.commit_seq,
        IntegratedReplicaTransaction.source_position).where(
        IntegratedReplicaTransaction.capture_id == capture.id,
        IntegratedReplicaTransaction.commit_seq.is_not(None))
        .order_by(IntegratedReplicaTransaction.commit_seq.desc())).all()
    floor_seq = next((seq for seq, stored in committed if proven(stored)), None)
    if floor_seq is None:
        return empty
    return prune_before(db, capture_id, commit_seq=floor_seq)
