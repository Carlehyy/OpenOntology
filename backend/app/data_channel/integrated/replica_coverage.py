"""Ordered source coverage cuts, separate from transport liveness.

A PostgreSQL cut is a nontransactional logical WAL message with a persisted
random token. It changes no business table. Seeing it through the same ordered,
durable HTTP receiver proves preceding committed changes were delivered; every
live cohort member must still apply their complete transactions before
publication.

MySQL uses the connector's completed streaming boundary: a GTID set that covers
the SQL frontier, or the same binlog family's numeric (file, pos) at/after the
frontier. Row/END/snapshot offsets are never inferred here; ingest must call
``observe_mysql_offset`` only for completed streaming boundaries.

State is intentionally bounded: at most one ``pending``, one ``confirmed`` and
one ``mysql_completed_offset``. A pending probe has a 60 second deadline; after
it expires and no message was observed, a new probe gets a new token and late
messages for the old token are ignored. An already observed cut is never
replaced by a late SQL response or a duplicate emit (first observed wins).
"""
from __future__ import annotations

import base64
import copy
import re
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.data_channel.integrated.replica_capture_models import IntegratedReplicaCapture
from app.data_channel.integrated.replica_models import IntegratedReplicaRun

PROBE_INTERVAL_SECONDS = 10
PROBE_DEADLINE_SECONDS = 60
_LIVE_RUN_STATUSES = frozenset({"loading", "catching_up", "active"})
_READABLE_CAPTURE_STATUSES = frozenset({"starting", "streaming", "stale"})
_DIALECTS = frozenset({"postgresql", "mysql"})
_MESSAGE_TOPIC = "oo_replica_{}.message"


class ReplicaCoverageError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _invalid():
    raise ReplicaCoverageError("REPLICA_COVERAGE_INVALID")


def _strict_int(value, *, minimum=None):
    if type(value) is not int:
        return False
    return minimum is None or value >= minimum


def _strict_positive_int(value):
    return _strict_int(value, minimum=1)


def _parse_time(value):
    if not isinstance(value, str):
        _invalid()
    try:
        return _utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        _invalid()


def prefix(capture_id):
    try:
        return "oo_replica_cut_" + uuid.UUID(str(capture_id)).hex
    except (AttributeError, TypeError, ValueError):
        _invalid()


def _state(capture):
    value = capture.coverage_probe
    if value is None:
        return {}
    if not isinstance(value, dict):
        _invalid()
    return copy.deepcopy(value)


def _membership(capture):
    """Frozen cohort membership: required run ids and explicitly detached ids.

    ``run.generation`` is the asset rebuild counter and ``capture.generation``
    is the connection cohort counter; they are deliberately independent and
    must never be compared for equality.
    """
    tables = capture.tables_json
    if not isinstance(tables, list):
        _invalid()
    required, detached = {}, set()
    for item in tables:
        if not isinstance(item, dict):
            _invalid()
        asset_id, run_id = item.get("asset_id"), item.get("run_id")
        if not isinstance(asset_id, str) or not asset_id:
            _invalid()
        if not isinstance(run_id, str) or not run_id:
            _invalid()
        flag = item.get("detached")
        if flag not in (None, False, True):
            _invalid()
        if run_id in required or run_id in detached:
            _invalid()
        if flag is True:
            detached.add(run_id)
        else:
            required[run_id] = asset_id
    return required, detached


def _pending_expired(pending, current):
    expires_at = pending.get("expires_at")
    if expires_at is None:
        # Legacy state: prepare_probe stamps a deadline on the next call; a
        # record/observe arriving before that keeps the old token usable.
        return False
    return _parse_time(expires_at) <= current


def _validate_pending(pending):
    if not isinstance(pending, dict):
        _invalid()
    if not isinstance(pending.get("id"), str) or not pending["id"]:
        _invalid()
    if not _strict_int(pending.get("generation"), minimum=1):
        _invalid()
    _parse_time(pending.get("requested_at"))
    if pending.get("expires_at") is not None:
        _parse_time(pending["expires_at"])
    if pending.get("frontier") is not None and not isinstance(pending["frontier"], dict):
        _invalid()
    if "observed_position" in pending:
        if not isinstance(pending["observed_position"], dict):
            _invalid()
        if not _strict_int(pending.get("observed_seq"), minimum=0):
            _invalid()
        _parse_time(pending.get("observed_at"))
    elif "observed_seq" in pending:
        _invalid()


def _validate_confirmed(confirmed):
    if not isinstance(confirmed, dict):
        _invalid()
    if not isinstance(confirmed.get("id"), str) or not confirmed["id"]:
        _invalid()
    if not _strict_int(confirmed.get("generation"), minimum=1):
        _invalid()
    if not _strict_int(confirmed.get("commit_seq"), minimum=0):
        _invalid()
    if not isinstance(confirmed.get("position"), dict):
        _invalid()
    _parse_time(confirmed.get("confirmed_at"))


def prepare_probe(db, capture_id):
    """Persist a token before dispatching source IO; the caller commits first."""
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == capture_id).with_for_update()
        .execution_options(populate_existing=True))
    if capture is None or capture.status not in _READABLE_CAPTURE_STATUSES:
        return None
    required, _detached = _membership(capture)
    if not required:
        return None
    runs = list(db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.capture_id == capture_id,
        IntegratedReplicaRun.status.in_(sorted(_LIVE_RUN_STATUSES)))
        .execution_options(populate_existing=True)))
    by_run = {run.id: run for run in runs if run.id in required}
    if set(by_run) != set(required):
        return None
    if any(run.status == "loading" for run in by_run.values()):
        return None
    if any(run.capture_id != capture.id for run in by_run.values()):
        return None

    state = _state(capture)
    pending = state.get("pending")
    current = now()
    if pending is not None:
        _validate_pending(pending)
        if pending["generation"] != capture.generation:
            # New capture generation: the old token can no longer be observed.
            state.pop("pending", None)
            pending = None
        elif pending.get("observed_position") is not None:
            # A cut is already observed; wait for all members to apply it.
            return None
        elif pending.get("expires_at") is None:
            pending["expires_at"] = (
                current + timedelta(seconds=PROBE_DEADLINE_SECONDS)).isoformat()
            state["pending"] = pending
            capture.coverage_probe = state
            db.flush()
            return copy.deepcopy(pending) if pending.get("frontier") is None else None
        elif not _pending_expired(pending, current):
            return copy.deepcopy(pending) if pending.get("frontier") is None else None
        else:
            # Deadline without an observed cut: the next probe uses a new
            # token; a late message for the old token is safely ignored.
            state.pop("pending", None)
            pending = None

    confirmed = state.get("confirmed")
    if confirmed is not None:
        _validate_confirmed(confirmed)
        if confirmed.get("generation") == capture.generation:
            age = (current - _parse_time(confirmed["confirmed_at"])).total_seconds()
            if age < PROBE_INTERVAL_SECONDS:
                return None
        else:
            state.pop("confirmed", None)

    pending = {
        "id": uuid.uuid4().hex,
        "generation": capture.generation,
        "requested_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=PROBE_DEADLINE_SECONDS)).isoformat(),
    }
    state["pending"] = pending
    capture.coverage_probe = state
    db.flush()
    return copy.deepcopy(pending)


def _valid_frontier(frontier):
    if not isinstance(frontier, dict) or frontier.get("dialect") not in _DIALECTS:
        return False
    if frontier["dialect"] == "postgresql":
        return _strict_positive_int(frontier.get("lsn"))
    if frontier.get("gtids") is not None:
        if not isinstance(frontier["gtids"], str):
            return False
        _gtid_set(frontier["gtids"])
        return True
    return _valid_binlog_position(frontier)


def record_probe(db, capture_id, token, frontier):
    """Record an advisory SQL frontier. First observed position always wins."""
    capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == capture_id).with_for_update()
        .execution_options(populate_existing=True))
    if capture is None:
        return
    state = _state(capture)
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return
    _validate_pending(pending)
    if pending.get("id") != token or pending.get("generation") != capture.generation:
        return
    if pending.get("observed_position") is not None:
        # A SQL response that raced the message (or a retry) must not replace
        # the first observed cut, nor make the batch fail.
        return
    if _pending_expired(pending, now()):
        return
    if not _valid_frontier(frontier):
        _invalid()
    pending["frontier"] = copy.deepcopy(frontier)
    if frontier["dialect"] == "mysql":
        observed = state.get("mysql_completed_offset")
        if observed is not None:
            _validate_offset(observed)
            _observe_mysql(capture, pending, frontier, observed)
    state["pending"] = pending
    capture.coverage_probe = state
    db.flush()


def _valid_message_shape(envelope, offset, topic):
    if not isinstance(envelope, dict):
        _invalid()
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        _invalid()
    message = payload.get("message")
    if not isinstance(message, dict):
        _invalid()
    if not isinstance(offset, dict) or not isinstance(topic, str):
        _invalid()
    return payload, message


def observe_postgres_message(db, capture, envelope, offset, topic):
    """Called after flushing all preceding data records in this HTTP batch."""
    payload, message = _valid_message_shape(envelope, offset, topic)
    expected_prefix = prefix(capture.id)
    if message.get("prefix") != expected_prefix:
        return
    state = _state(capture)
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return
    _validate_pending(pending)
    if pending.get("generation") != capture.generation:
        return
    content = message.get("content")
    if not isinstance(content, str):
        _invalid()
    try:
        decoded = base64.b64decode(content, validate=True).decode("ascii")
    except (ValueError, TypeError, UnicodeError):
        _invalid()
    if decoded != pending.get("id"):
        return
    if pending.get("observed_position") is not None:
        # First matching message is authoritative; duplicate emits (including
        # one with a different LSN) are ignored without failing the batch.
        return
    if _pending_expired(pending, now()):
        return

    lsn = offset.get("lsn")
    if not _strict_positive_int(lsn):
        _invalid()
    if payload.get("op") != "m":
        _invalid()
    source = payload.get("source")
    if not isinstance(source, dict):
        _invalid()
    if (topic != _MESSAGE_TOPIC.format(uuid.UUID(capture.id).hex)
            or offset.get("lsn_proc") != lsn
            or not _strict_positive_int(offset.get("lsn_proc"))
            or offset.get("lsn_commit") != lsn
            or not _strict_positive_int(offset.get("lsn_commit"))
            or offset.get("messageType") != "MESSAGE"
            or source.get("lsn") != lsn
            or not _strict_positive_int(source.get("lsn"))
            or payload.get("transaction") is not None):
        _invalid()
    if not _strict_int(capture.committed_seq, minimum=0):
        _invalid()
    pending["frontier"] = {"dialect": "postgresql", "lsn": lsn}
    pending.update(observed_seq=capture.committed_seq,
                   observed_at=now().isoformat(),
                   observed_position={"lsn": lsn})
    state["pending"] = pending
    capture.coverage_probe = state
    db.flush()


def _gtid_set(value):
    if not isinstance(value, str) or len(value) > 1024 * 1024:
        _invalid()
    result = {}
    groups = value.replace("\n", "").strip()
    if not groups:
        return result
    for group in groups.split(","):
        if not group.strip():
            _invalid()
        server, *intervals = group.strip().split(":")
        try:
            server = str(uuid.UUID(server))
        except ValueError:
            _invalid()
        if not intervals:
            _invalid()
        ranges = result.setdefault(server, [])
        for interval in intervals:
            values = interval.split("-")
            if (len(values) not in {1, 2}
                    or any(not part.isdecimal() for part in values)):
                _invalid()
            start, end = int(values[0]), int(values[-1])
            if start <= 0 or end < start:
                _invalid()
            ranges.append((start, end))
    for server, ranges in result.items():
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        result[server] = merged
    return result


def gtid_covers(completed, required):
    received, target = _gtid_set(completed), _gtid_set(required)
    return all(any(low <= start and high >= end for low, high in received.get(server, []))
               for server, ranges in target.items() for start, end in ranges)


def _valid_binlog_position(value):
    if not isinstance(value, dict) or not _strict_positive_int(value.get("pos")):
        return False
    match = re.fullmatch(r"(.+)\.(\d+)", str(value.get("file", "")))
    return match is not None


def _binlog_position(value):
    if not _valid_binlog_position(value):
        _invalid()
    match = re.fullmatch(r"(.+)\.(\d+)", value["file"])
    return match.group(1), int(match.group(2)), value["pos"]


def _validate_offset(offset):
    if not isinstance(offset, dict):
        _invalid()
    _binlog_position(offset)
    gtids = offset.get("gtids")
    if gtids is not None:
        _gtid_set(gtids)


def _observe_mysql(capture, pending, frontier, offset):
    gtids = frontier.get("gtids")
    if gtids is not None:
        covered = gtid_covers(offset.get("gtids", ""), gtids)
    else:
        received, required = _binlog_position(offset), _binlog_position(frontier)
        covered = received[0] == required[0] and received[1:] >= required[1:]
    if covered and "observed_position" not in pending:
        if not _strict_int(capture.committed_seq, minimum=0):
            _invalid()
        pending.update(observed_seq=capture.committed_seq,
                       observed_at=now().isoformat(),
                       observed_position=copy.deepcopy(offset))


def _offset_covers(new, old):
    """True when new position/GTID set is at least old; incomparable is False."""
    new_pos, old_pos = _binlog_position(new), _binlog_position(old)
    if new_pos[0] != old_pos[0]:
        _invalid()
    if new_pos[1:] > old_pos[1:]:
        return True
    if new_pos[1:] < old_pos[1:]:
        return False
    old_gtids, new_gtids = old.get("gtids"), new.get("gtids")
    if old_gtids is None or new_gtids is None:
        return old_gtids == new_gtids
    return gtid_covers(new_gtids, old_gtids)


def observe_mysql_offset(db, capture, offset):
    """Only complete streaming boundary offsets are eligible, never snapshot rows.

    Fixed Debezium 3.7.1 heartbeats carry restartBinlogPosition/restartGtidSet,
    updated after commit on its ordered consumer thread. The newest completed
    boundary is retained; older/incomparable offsets never regress it. The state
    is flushed only when it actually changes.
    """
    _validate_offset(offset)
    new_position = _binlog_position(offset)
    state = _state(capture)
    changed = False
    stored = state.get("mysql_completed_offset")
    if stored is None:
        state["mysql_completed_offset"] = copy.deepcopy(offset)
        changed = True
    else:
        _validate_offset(stored)
        stored_position = _binlog_position(stored)
        if stored_position[0] != new_position[0]:
            # A different binlog family is a source identity change, not an
            # ordering gap that can be silently compared.
            _invalid()
        if new_position[1:] > stored_position[1:]:
            state["mysql_completed_offset"] = copy.deepcopy(offset)
            changed = True
        elif new_position[1:] == stored_position[1:]:
            if stored.get("gtids") != offset.get("gtids") and _offset_covers(offset, stored):
                state["mysql_completed_offset"] = copy.deepcopy(offset)
                changed = True

    pending = state.get("pending")
    if (isinstance(pending, dict)
            and pending.get("generation") == capture.generation
            and (pending.get("frontier") or {}).get("dialect") == "mysql"
            and "observed_position" not in pending):
        before = copy.deepcopy(pending)
        _observe_mysql(capture, pending, pending["frontier"],
                       state["mysql_completed_offset"])
        if pending != before:
            state["pending"] = pending
            changed = True

    if changed:
        capture.coverage_probe = state
        db.flush()


def _provided_runs(capture, runs, required, detached):
    if not isinstance(runs, (list, tuple)):
        _invalid()
    provided = {}
    for run in runs:
        run_id = getattr(run, "id", None)
        asset_id = getattr(run, "asset_id", None)
        capture_id = getattr(run, "capture_id", None)
        status = getattr(run, "status", None)
        if not isinstance(run_id, str) or not run_id or run_id in provided:
            _invalid()
        if run_id in detached:
            # Explicitly detached: excluded from the all-member set even if
            # the runtime still includes it in live_runs during transition.
            continue
        if run_id not in required or asset_id != required[run_id]:
            # Extra/foreign/detached run: the executor must pass the exact
            # live, non-detached member set, never a convenience subset.
            _invalid()
        if capture_id != capture.id:
            _invalid()
        if status == "loading":
            # Not an invalid caller shape: the member is still initializing.
            # applied_probe must simply not confirm yet.
            return None
        if status not in {"active", "catching_up"}:
            _invalid()
        position = run.applied_position
        if position is None:
            position = {}
        if not isinstance(position, dict):
            _invalid()
        sequence = position.get("commit_seq", 0)
        if not _strict_int(sequence, minimum=0):
            _invalid()
        provided[run_id] = sequence
    return provided


def applied_probe(capture, runs):
    """Consume an observed cut only after every frozen live member applied it.

    Membership comes from ``capture.tables_json`` (asset_id/run_id pairs;
    ``detached: true`` is excluded), never from the caller's list alone.
    ``run.generation`` is intentionally not compared with
    ``capture.generation``: they are different counters.
    """
    if not isinstance(runs, (list, tuple)):
        _invalid()
    state = _state(capture)
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return None
    _validate_pending(pending)
    if pending.get("generation") != capture.generation:
        return None
    if not isinstance(pending.get("frontier"), dict):
        return None
    if "observed_position" not in pending:
        return None
    sequence = pending.get("observed_seq")
    if not _strict_int(sequence, minimum=0):
        return None
    required, detached = _membership(capture)
    if not required:
        return None
    provided = _provided_runs(capture, runs, required, detached)
    if provided is None or set(provided) != set(required):
        return None
    if any(value < sequence for value in provided.values()):
        return None

    confirmed = {
        "capture_id": capture.id,
        "generation": capture.generation,
        "id": pending["id"],
        "commit_seq": sequence,
        "confirmed_at": pending["requested_at"],
        "position": copy.deepcopy(pending["observed_position"]),
    }
    state["confirmed"] = confirmed
    state.pop("pending", None)
    capture.coverage_probe = state
    return confirmed
