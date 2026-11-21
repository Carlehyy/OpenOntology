"""Resume coordinates for the fixed Debezium replica connectors.

A received offset can describe a partial transaction: the retained journal
and connector transaction context allow replay. This is not an applied cursor
or a proof that a replica is ready to be published.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.data_channel.integrated import replica_coverage as coverage

_PG_COUNTED_OPERATIONS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


@dataclass(frozen=True)
class _PostgresProgress:
    boundary_lsn: int
    slot_lsn: int
    event: tuple[int, int] | None = None
    xid: int | None = None
    end: bool = False


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _pg_progress(offset):
    lsn, processed, committed = (offset.get(key) for key in ("lsn", "lsn_proc", "lsn_commit"))
    if ((lsn is None and processed is None)
            or any(value is not None and not _integer(value, 1)
                   for value in (lsn, processed, committed))):
        return None
    events = offset.get("lsn_events_processed", 0)
    xid = offset.get("txId")
    if not _integer(events) or (xid is not None and not _integer(xid)):
        return None
    message = offset.get("messageType")
    resume = processed or lsn
    if message == "COMMIT":
        # pgoutput ENDs set all three markers to the commit boundary.
        return (_PostgresProgress(committed, committed)
                if lsn == processed == committed else None)
    counted = message in _PG_COUNTED_OPERATIONS
    if counted:
        # The locator applies the count at the requested LSN. Reject a count
        # that describes another LSN or cannot pinpoint a row at all.
        if lsn != resume or not _integer(events, 1):
            return None
    elif message not in (None, "BEGIN", "MESSAGE", "NOOP", "ORIGIN"):
        return None
    transactional_message = message == "MESSAGE" and (
        xid is not None or lsn != committed or resume != committed)
    if counted or message == "BEGIN" or transactional_message:
        # pgoutput sends transactions in commit order. An open transaction's
        # rows can have lower WAL addresses than the prior transaction's END.
        if lsn is None or resume > max(committed or 0, lsn):
            return None
        return _PostgresProgress(committed or 0, committed or resume,
                                 (lsn, events if counted else 0), xid)
    frontier = max(value for value in (lsn, processed, committed) if value is not None)
    return _PostgresProgress(frontier, committed or resume)


def _pg_end(position):
    if not isinstance(position, dict) or position.get("dialect") != "postgresql":
        return None
    lsn = position.get("commit_lsn")
    if not _integer(lsn, 1):
        return None
    offset = position.get("offset")
    xid = offset.get("txId") if isinstance(offset, dict) else None
    return _PostgresProgress(lsn, lsn, xid=xid if _integer(xid) else None, end=True)


def _pg_received(offset, end):
    progress = _progress(offset, "postgresql")
    if progress is None or offset.get("messageType") != "COMMIT":
        return progress
    if end is not None and end.boundary_lsn == progress.boundary_lsn:
        return end
    if offset.get("transaction_id") is not None:
        # A COMMIT heartbeat can precede its transaction metadata END. The
        # journal must receive END before the connector may skip it on restart.
        return None
    return progress


def _mysql_progress(offset):
    if not _integer(offset.get("pos"), 4):
        return None
    event, row = offset.get("event", 0), offset.get("row", 0)
    if not _integer(event) or not _integer(row):
        return None
    try:
        family, file, pos = coverage._binlog_position(offset)
        gtids = offset.get("gtids")
        gtids = "" if gtids is None else gtids
        if gtids != "":
            coverage.gtid_covers(gtids, "")  # reject malformed GTID sets
    except (coverage.ReplicaCoverageError, ValueError):
        return None
    return family, (file, pos, event, row), gtids


def _progress(offset, dialect):
    if not isinstance(offset, dict):
        return None
    if dialect == "postgresql":
        return _pg_progress(offset)
    if dialect == "mysql":
        return _mysql_progress(offset)
    return None


def resumable(offset, dialect):
    if _progress(offset, dialect) is None:
        return False
    snapshot = offset.get("snapshot")
    if dialect == "mysql":
        # The binlog connector stores no completion flag with its snapshot
        # marker. Wait for a streaming offset with the marker cleared.
        return snapshot is None
    final_record = offset.get("last_snapshot_record", False)
    if type(final_record) is not bool:
        return False
    if snapshot is None:
        return True
    # PostgreSQL clears the snapshot on loading a completed final marker.
    completed = offset.get("snapshot_completed")
    return ((snapshot is True or snapshot in ("true", "INITIAL"))
            and (final_record or completed is True or completed == "true"))


def _covers(received, required, dialect):
    if dialect == "postgresql":
        if received.boundary_lsn < required.boundary_lsn:
            return False
        if required.event is None:
            return True
        # Only the journal's complete END can prove a partial transaction
        # was fully received. A probe or the preceding END proves no rows.
        if (received.end and received.boundary_lsn > max(required.boundary_lsn, required.event[0])
                and (required.boundary_lsn or (received.xid is not None and received.xid == required.xid))):
            return True
        return (received.event is not None and received.xid == required.xid
                and received.boundary_lsn == required.boundary_lsn
                and received.event >= required.event)
    family, coords, gtids = received
    required_family, required_coords, required_gtids = required
    return (family == required_family and coords >= required_coords
            and coverage.gtid_covers(gtids, required_gtids))


def record_received(capture, offset, dialect):
    """Don't let an acknowledged replay regress the durable received frontier."""
    if dialect == "postgresql":
        end = _pg_end(capture.committed_position)
        received = _pg_received(offset, end)
        previous = _pg_received(capture.source_offset, end)
        # The first streaming transaction can lack lsn_commit. Its row
        # receipt supplements the initial boundary kept in the snapshot proof.
        if (received is not None and received.event is not None and end is None
                and previous is not None and capture.source_offset.get("messageType") is None):
            capture.source_offset = offset
            return
    else:
        received = _progress(offset, dialect)
        previous = _progress(capture.source_offset, dialect)
    if received is not None and (previous is None or _covers(received, previous, dialect)):
        capture.source_offset = offset


def received_positions(capture, runs, dialect):
    """Read evidence in O(cohort size), without scanning replay receipts."""
    committed = capture.committed_position
    end = _pg_end(committed) if dialect == "postgresql" else None
    positions = []
    if dialect == "postgresql":
        received = _pg_received(capture.source_offset, end)
        proofs = [position for position in (received, end) if position is not None]
    else:
        proofs = []
        positions.append(capture.source_offset)
        if isinstance(committed, dict) and committed.get("dialect") == dialect:
            positions.append(committed.get("offset"))
    positions.extend(run.snapshot_proof.get("offset") for run in runs
                     if isinstance(run.snapshot_proof, dict)
                     and run.snapshot_proof.get("complete") is True
                     and run.snapshot_proof.get("snapshot_id") == capture.id)
    probes = capture.coverage_probe
    if isinstance(probes, dict):
        confirmed = probes.get("confirmed")
        if (isinstance(confirmed, dict) and confirmed.get("generation") == capture.generation
                and confirmed.get("capture_id") == capture.id):
            positions.append(confirmed.get("position"))
        pending = probes.get("pending")
        if isinstance(pending, dict) and pending.get("generation") == capture.generation:
            # Pending tokens live on the capture row and carry no capture ID.
            # The SQL frontier alone proves nothing about what was received.
            positions.append(pending.get("observed_position"))
        if dialect == "mysql":
            positions.append(probes.get("mysql_completed_offset"))
    proofs.extend(progress for position in positions
                  if (progress := _progress(position, dialect)) is not None)
    return proofs


def received_offset_covers(received, offset, dialect):
    required = _progress(offset, dialect)
    return required is not None and any(_covers(position, required, dialect) for position in received)
