"""Replica supervisor fence tests (SQLite; fake child/receiver, real pools).

The supervisor under test is the real ``ReplicaSupervisor`` over the real
runtime/control/store layer.  Only the process plane is injected: the child
factory, the HTTP receiver and the session lease.  Source IO (preflight /
validate_resume / health probes) runs on the supervisor's real thread pools
against an injected connector, so the tests exercise the asynchronous
contract itself:

- ticks never block on source IO: a wedged source check for one capture
  must not stall another capture's apply in the same tick;
- async startup is asserted through its final state only (source check
  confirmed, then child start), never through intermediate futures;
- source checks are digest-scoped (a restart only proceeds on a check of
  the *current* configuration) and run at most once per schema-check
  interval; a fresh check alone never certifies "streaming".

Remaining fences mirror the module contract:

- losing the session lease stops every child before any other work;
- an executor restart never resumes an unfinished initial snapshot:
  candidates are discarded (failed + rebuilt) and the cohort drained;
- an all-active capture may only resume with durable offsets
  (``offsets.dat``), otherwise it is blocked non-retryably;
- the capture capacity bound releases the per-capture lease and never
  prepares new cohorts while every slot is busy;
- the child environment and generated files never carry platform secrets;
- shutdown terminates, reaps and reclaims every child, the receiver and
  the thread pools, while durable offset state survives;
- a credential change interrupts an initializer (discard + rebuild) and
  restarts an established stream in place under a fresh source check;
- a newly registered table joins a separate capture cohort; an established
  cohort is never restarted for it;
- a draining capture cleans only its own source resources; a blocked
  capture stops its child but preserves them;
- "streaming" requires receiver evidence newer than the current child's
  start, inside the freshness window, and an explicitly UP health probe.

``Harness.tick`` drains the supervisor's immediate fake futures between
ticks (bounded, never a pool-wide wait) so each settle step observes the
asynchronous result on the next tick, exactly like the production loop.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import cdc_control
from app.data_channel.integrated import replica_cleanup as cleanup
from app.data_channel.integrated import replica_connector as connector
from app.data_channel.integrated import replica_control as control
from app.data_channel.integrated import replica_runtime
from app.data_channel.integrated.contracts import digest
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture,
)
from app.data_channel.integrated.replica_executor import (
    CAPACITY,
    OFFSETS_INVALID,
    OFFSETS_READ_FAILED,
    OffsetReadError,
    PROBE_TIMEOUT_SECONDS,
    RETRY_SECONDS,
    SCHEMA_CHECK_SECONDS,
    ReplicaSupervisor,
)
from app.data_channel.integrated.replica_models import (
    IntegratedReplica,
    IntegratedReplicaRun,
)
from tests.conftest import TestSession


# ---------------------------------------------------------------------------
# fakes (process plane only; the database layer and pools are real)
# ---------------------------------------------------------------------------

class FakeChild:
    def __init__(self, *, error_code=None, dead=False):
        self.error_code = error_code
        self.dead = dead
        self.stopped = False
        self.calls: list[str] = []
        self.args = self.cwd = self.env = None

    def poll(self):
        return 3 if self.dead else None

    def stop(self, *, grace=15.0):
        self.calls.append("stop")
        self.stopped = self.dead = True

    def terminate(self):
        self.calls.append("terminate")
        self.stopped = self.dead = True

    def kill_group(self):
        self.calls.append("kill_group")

    def wait(self, timeout):
        self.calls.append("wait")
        return True

    def reap(self):
        self.calls.append("reap")


class FakeReceiver:
    port = 47000

    def __init__(self):
        self.registered: dict[str, tuple] = {}
        self.unregistered: list[str] = []
        self.accepted: dict[tuple, float] = {}
        self.stopped = False

    def register(self, capture_id, generation, secret):
        self.registered[capture_id] = (generation, secret)
        return SimpleNamespace(path=f"/replica/{capture_id}/{generation}")

    def unregister(self, capture_id):
        self.unregistered.append(capture_id)
        self.registered.pop(capture_id, None)

    def last_accepted_at(self, capture_id, generation):
        return self.accepted.get((capture_id, generation))

    def stop(self):
        self.stopped = True


class FakeLease:
    def __init__(self, *, allowed=True, healthy=True):
        self.allowed = allowed
        self.healthy_flag = healthy
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.reset_calls = 0

    def acquire(self, key):
        # Production SourceLease is re-entrant for an already-held key.
        if not self.allowed:
            return False
        if key not in self.acquired:
            self.acquired.append(key)
        return True

    def release(self, key):
        if key in self.acquired:
            self.acquired.remove(key)
        self.released.append(key)

    def healthy(self):
        return self.healthy_flag

    def reset(self):
        self.reset_calls += 1


class FakeConnector:
    """Records the detached source projections the executor must hand over.

    ``cleanup`` returns True (the real connector's contract) unless a test
    supplies a result sequence or exception. Preflight, validate_resume and
    cleanup may be wedged per source/capture id through ``gates`` to prove
    that ticks never wait on source IO.
    """

    def __init__(self, gates=None, cleanup_gates=None):
        self.gates = gates if gates is not None else {}
        self.cleanup_gates = cleanup_gates if cleanup_gates is not None else {}
        self.cleanup_calls: list[tuple[str, str]] = []
        self.cleanup_results: list[bool] = []
        self.cleanup_sources: list[tuple[str, dict]] = []
        self.cleanup_sequence: list = []
        self.cleanup_errors: list = []
        self.validate_resume_calls: list[str] = []
        self.validate_resume_sources: list[tuple[str, dict]] = []
        self.preflight_calls: list[list[str]] = []
        self.preflight_sources: list[tuple[str, dict]] = []
        self.properties: list[dict] = []
        self.probe_calls: list[tuple[str, str, str, dict]] = []
        self.probe_result = {"dialect": "postgresql", "lsn": 100}
        self.probe_error = None
        self.probe_gate: threading.Event | None = None

    def emit_coverage_probe(self, source, capture_id, token):
        self.probe_calls.append(
            (source.id, capture_id, token, copy.deepcopy(source.config)))
        if self.probe_gate is not None:
            self.probe_gate.wait()
        if self.probe_error is not None:
            raise self.probe_error
        return copy.deepcopy(self.probe_result)

    def cleanup(self, source, capture_id):
        self.cleanup_calls.append((source.id, capture_id))
        gate = self.cleanup_gates.get(capture_id)
        if gate is not None:
            gate.wait()
        self.cleanup_sources.append((source.id, copy.deepcopy(source.config)))
        if self.cleanup_errors:
            error = self.cleanup_errors.pop(0)
            if error is not None:
                raise error
        result = self.cleanup_sequence.pop(0) if self.cleanup_sequence else True
        self.cleanup_results.append(result)
        return result

    def validate_resume(self, source, capture_id):
        self.validate_resume_calls.append(capture_id)
        self.validate_resume_sources.append(
            (source.id, copy.deepcopy(source.config)))
        self._wait_gate(source.id)

    def preflight(self, source, tables):
        self.preflight_calls.append([table.id for table in tables])
        self.preflight_sources.append((source.id, copy.deepcopy(source.config)))
        self._wait_gate(source.id)

    def _wait_gate(self, source_id):
        gate = self.gates.get(source_id)
        if gate is not None:
            gate.wait()

    def build_properties(self, **kwargs):
        self.properties.append(kwargs)
        # Close enough to the real file: the rotated secret travels only
        # through this 0600 properties payload.
        return f"webhook.secret={kwargs['secret_b64']}\n"


class OffsetReader:
    """Fake of the shared Java ``ReadOffsets`` output (real reader is covered
    by its own test). The default proves one resumable partition for the
    capture inferred from the state path."""

    def __init__(self):
        self.result = None
        self.error = None
        self.calls: list[Path] = []

    def __call__(self, path):
        self.calls.append(Path(path))
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return copy.deepcopy(self.result)
        capture_id = Path(path).parent.name
        return [{
            "partition": {"server": "oo_replica_" + uuid.UUID(capture_id).hex},
            "offset": {"lsn": 100, "lsn_proc": 100, "lsn_commit": 100},
        }]


class Harness:
    def __init__(self, tmp_path, *, max_captures=2, health=lambda port: None,
                 lease=None, session_factory=TestSession):
        self.receiver = FakeReceiver()
        self.gates: dict[str, threading.Event] = {}
        self.connector = FakeConnector(self.gates)
        self.offsets = OffsetReader()
        self.lease = lease or FakeLease()
        self.children: list[FakeChild] = []
        self.clock = {"now": 1_000_000.0}

        def spawn(args, cwd, env):
            child = FakeChild()
            child.args, child.cwd, child.env = args, cwd, env
            self.children.append(child)
            return child

        self.supervisor = ReplicaSupervisor(
            session_factory=session_factory, receiver=self.receiver,
            lease=self.lease, connector_module=self.connector, spawn=spawn,
            health_probe=health, state_root=Path(tmp_path) / "state",
            run_root=Path(tmp_path) / "run",
            heartbeat_file=Path(tmp_path) / "heartbeat",
            max_captures=max_captures, interval=0.01,
            clock=lambda: self.clock["now"], offset_reader=self.offsets)

    # ── asynchronous driving ────────────────────────────────────────

    def tick(self, *, settle=True):
        """One production tick; optionally drain immediate fake futures.

        Draining is bounded and only tracks futures the supervisor itself
        references — it never waits on the pools as a whole, so a wedged
        source check cannot hang a test tick (use ``settle=False`` there).
        """
        self.supervisor.tick()
        if settle:
            self.drain()

    def drain(self, timeout=2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = [
                future for item in self.supervisor.processes.values()
                for future in (item.source_check, item.health_read,
                               item.offset_check, item.offset_read,
                               item.probe_future, *item.probe_abandoned)
                if future is not None and not future.done()]
            pending += [future for future in self.supervisor.cleanups.values()
                        if not future.done()]
            if not pending:
                return True
            time.sleep(0.005)
        return False

    def settle(self, ticks=3):
        """Drive ticks until async results are consumed (final state only)."""
        for _ in range(ticks):
            self.tick()

    def gate(self, source_id) -> threading.Event:
        event = threading.Event()
        self.gates[source_id] = event
        return event

    def cleanup_gate(self, capture_id) -> threading.Event:
        event = threading.Event()
        self.connector.cleanup_gates[capture_id] = event
        return event

    def probe_gate(self) -> threading.Event:
        self.connector.probe_gate = threading.Event()
        return self.connector.probe_gate

    def open_gates(self):
        for event in self.gates.values():
            event.set()
        for event in self.connector.cleanup_gates.values():
            event.set()
        if self.connector.probe_gate is not None:
            self.connector.probe_gate.set()

    def advance(self, seconds):
        self.clock["now"] += seconds

    def child_for(self, capture_id):
        item = self.supervisor.processes.get(capture_id)
        return item.child if item is not None else None


@pytest.fixture
def new_harness():
    """Harness factory whose supervisors are always shut down afterwards.

    The supervisor owns real thread pools; the finally-guard closes their
    gates and shuts them down so no worker outlives a failed test.
    """
    created: list[Harness] = []
    try:
        def factory(tmp_path, **kwargs):
            harness = Harness(tmp_path, **kwargs)
            created.append(harness)
            return harness
        yield factory
    finally:
        for harness in created:
            harness.open_gates()
            harness.supervisor.shutdown()


# ---------------------------------------------------------------------------
# seeding (real rows, real control-plane transitions)
# ---------------------------------------------------------------------------

MANIFEST = {
    "columns": [
        {"name": "id", "type": "integer", "nullable": False,
         "primary_key": True, "source_type": "INTEGER"},
        {"name": "amount", "type": "decimal", "nullable": True,
         "precision": 12, "scale": 2, "source_type": "NUMERIC"},
    ],
    "primary_key": ["id"],
}


def _manifest():
    return copy.deepcopy(MANIFEST)


@pytest.fixture
def connection(db):
    row = Connection(id="c1", name="replica source", kind="postgres", config={
        "host": "src.example", "port": 5432, "database": "app",
        "user": "cdc_user", "password": "pw-one"})
    db.add(row)
    db.commit()
    return row


def _asset(db, admin_user, connection, *, asset_id, table):
    dataset = Dataset(id=asset_id + "-ds", name=asset_id, kind="integrated",
                      source_connection_id=connection.id,
                      source_resource=f"public.{table}")
    asset = IntegratedAsset(
        id=asset_id, dataset_id=dataset.id, connection_id=connection.id,
        name=asset_id, schema_name="public", table_name=table,
        schema_json=_manifest(), schema_fingerprint=digest(MANIFEST),
        writable=False, created_by=admin_user.id)
    db.add_all([dataset, asset])
    db.commit()
    return asset


def _capture(db, connection, *, status="starting", generation=1,
             created_offset_seconds=0):
    row = IntegratedReplicaCapture(
        id=str(uuid.uuid4()), connection_id=connection.id,
        generation=generation,
        identity_fingerprint=cdc_control.source_identity(connection),
        status=status, tables_json=[],
        created_at=datetime.now(timezone.utc)
        - timedelta(seconds=created_offset_seconds))
    db.add(row)
    db.flush()
    return row


def _loading(db, connection, asset, *, capture=None, generation=1,
             created_offset_seconds=0):
    """A capture cohort whose run is mid-initial-snapshot (loading)."""
    capture = capture or _capture(db, connection, generation=generation,
                                  created_offset_seconds=created_offset_seconds)
    run = control.request_rebuild(db, asset)
    token = control.claim_run(db, run.id)
    control.begin_load(db, run.id, token, capture_id=capture.id,
                       capture_start={"mode": "debezium_initial",
                                      "capture_id": capture.id})
    capture.tables_json = [{"asset_id": asset.id, "run_id": run.id,
                            "schema_name": asset.schema_name,
                            "table_name": asset.table_name}]
    db.commit()
    return capture, run


def _active(db, connection, asset, *, capture=None, generation=1,
            created_offset_seconds=0):
    """A published active replica bound to a capture cohort."""
    capture, run = _loading(db, connection, asset, capture=capture,
                            generation=generation,
                            created_offset_seconds=created_offset_seconds)
    token = run.owner_token
    control.finish_load(db, run.id, token, completion={
        "complete": True, "row_count": 2, "expected_rows": 2,
        "schema_fingerprint": asset.schema_fingerprint,
        "snapshot_id": capture.id})
    control.publish_run(db, run.id, token,
                        applied_position={"capture_id": capture.id,
                                          "commit_seq": 3},
                        coverage_confirmed_at=datetime.now(timezone.utc),
                        row_count=2)
    db.commit()
    return capture, run


def _state_dir(harness, capture_id) -> Path:
    return Path(harness.supervisor.state_root) / capture_id


def _write_offsets(harness, capture_id) -> None:
    state = _state_dir(harness, capture_id)
    state.mkdir(parents=True, exist_ok=True)
    (state / "offsets.dat").write_bytes(b"offsets")


def _capture_row(db, capture_id):
    return db.get(IntegratedReplicaCapture, capture_id,
                  populate_existing=True)


def _run_row(db, run_id):
    return db.get(IntegratedReplicaRun, run_id, populate_existing=True)


def _replica_row(db, asset_id):
    return db.get(IntegratedReplica, asset_id, populate_existing=True)


def _connection2(db):
    row = Connection(id="c2", name="second source", kind="postgres", config={
        "host": "src2.example", "port": 5432, "database": "app2",
        "user": "cdc_user", "password": "pw-one"})
    db.add(row)
    db.commit()
    return row


def _rotate_password(db, connection, password="pw-two"):
    stored = db.get(Connection, connection.id)
    stored.config = dict(stored.config, password=password)
    db.commit()


def _server(capture_id) -> str:
    return "oo_replica_" + uuid.UUID(str(capture_id)).hex


def _no_autoflush_sessions():
    # Production SessionLocal disables autoflush; the shared test session does
    # not, which hid status changes being clobbered by populate_existing.
    return sessionmaker(bind=TestSession.kw["bind"], autoflush=False)


def _multi_active(db, connection, assets):
    """One immutable capture cohort serving several published members."""
    for asset in assets:
        control.request_rebuild(db, asset)
    capture = replica_runtime.prepare_capture(db, connection.id)
    db.commit()
    assert capture is not None and len(capture.tables_json) == len(assets)
    for member in capture.tables_json:
        run = db.get(IntegratedReplicaRun, member["run_id"])
        token = run.owner_token
        control.finish_load(db, run.id, token, completion={
            "complete": True, "row_count": 0, "expected_rows": 0,
            "schema_fingerprint": run.schema_fingerprint,
            "snapshot_id": capture.id})
        control.publish_run(
            db, run.id, token,
            applied_position={"capture_id": capture.id, "commit_seq": 0},
            coverage_confirmed_at=datetime.now(timezone.utc), row_count=0)
    db.commit()
    return capture


def _mixed_capture(db, connection, *, active_asset, loading_asset):
    """One active member plus one still-initializing member in the same cohort."""
    for asset in (active_asset, loading_asset):
        control.request_rebuild(db, asset)
    capture = replica_runtime.prepare_capture(db, connection.id)
    db.commit()
    members = {member["asset_id"]: member for member in capture.tables_json}
    run = db.get(IntegratedReplicaRun, members[active_asset.id]["run_id"])
    token = run.owner_token
    control.finish_load(db, run.id, token, completion={
        "complete": True, "row_count": 0, "expected_rows": 0,
        "schema_fingerprint": run.schema_fingerprint,
        "snapshot_id": capture.id})
    control.publish_run(
        db, run.id, token,
        applied_position={"capture_id": capture.id, "commit_seq": 0},
        coverage_confirmed_at=datetime.now(timezone.utc), row_count=0)
    db.commit()
    return capture


# ---------------------------------------------------------------------------
# async contract: source IO never blocks a tick
# ---------------------------------------------------------------------------

def test_wedged_source_check_does_not_stall_other_captures(
        db, admin_user, tmp_path, connection, new_harness, monkeypatch):
    connection2 = _connection2(db)
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture1, _ = _active(db, connection, asset1, created_offset_seconds=5)
    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture1.id)
    harness.settle(3)  # established: check + health consumed, child running
    assert harness.child_for(capture1.id) is not None

    # The second cohort appears only now; its very first source check will
    # be wedged inside the real source-check pool.
    asset2 = _asset(db, admin_user, connection2, asset_id="a2", table="orders")
    capture2, run2 = _loading(db, connection2, asset2)
    applied: list[str] = []
    monkeypatch.setattr(
        replica_runtime, "apply_capture",
        lambda session, capture_id, **kwargs:
            applied.append(capture_id) or 0)
    gate = harness.gate(connection2.id)

    harness.tick(settle=False)  # capture2's check blocks on the gate
    harness.tick(settle=False)  # and stays blocked across further ticks
    db.expire_all()
    assert applied == [capture1.id, capture1.id]  # apply kept advancing
    assert harness.child_for(capture2.id) is None  # no start without check
    # The worker really reached the gate for capture2's source (recorded
    # before waiting) — this is a wedged check, not an unscheduled one.
    assert harness.connector.preflight_calls.count([asset2.id]) == 1
    assert harness.connector.preflight_sources[-1][0] == connection2.id
    assert not gate.is_set()

    gate.set()  # release the worker before any pool-wide wait
    harness.settle(2)  # the check completes async; the cohort finally starts
    db.expire_all()
    assert harness.child_for(capture2.id) is not None
    assert _run_row(db, run2.id).status == "loading"


def test_source_checks_run_at_most_once_per_schema_interval(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _loading(db, connection, asset)
    harness = new_harness(tmp_path)
    harness.settle(3)
    assert harness.connector.preflight_calls == [[asset.id]]

    harness.tick()
    harness.tick()  # no clock advance: the confirmed check stays valid
    assert harness.connector.preflight_calls == [[asset.id]]

    harness.advance(SCHEMA_CHECK_SECONDS + 1)
    harness.tick()  # interval elapsed: a fresh async check is scheduled
    harness.tick()  # ... and confirmed
    assert len(harness.connector.preflight_calls) == 2


def test_fresh_source_check_alone_never_certifies_streaming(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _loading(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True)
    harness.settle(3)  # source check confirmed, health UP, child running
    db.expire_all()
    item = harness.supervisor.processes[capture.id]
    assert item.checked_digest is not None
    assert item.health_up is True
    # Without receiver evidence none of that certifies "streaming".
    assert _capture_row(db, capture.id).status == "starting"


# ---------------------------------------------------------------------------
# fence: lease loss stops every child first
# ---------------------------------------------------------------------------

def test_lease_loss_stops_every_child_before_any_other_work(
        db, admin_user, tmp_path, connection, new_harness):
    connection2 = _connection2(db)
    capture1, _ = _loading(db, connection, _asset(db, admin_user, connection,
                                                  asset_id="a1", table="orders"),
                           created_offset_seconds=5)
    capture2, _ = _loading(db, connection2,
                           _asset(db, admin_user, connection2,
                                  asset_id="a2", table="orders"))
    harness = new_harness(tmp_path)
    harness.settle(3)
    stopped_before = list(harness.children)
    assert len(stopped_before) == 2
    assert harness.receiver.registered.keys() >= {capture1.id, capture2.id}

    harness.lease.healthy_flag = False
    harness.tick()
    db.expire_all()

    # Every child is stopped and every receiver registration of the lost
    # cohorts revoked before the supervisor does anything else; the lease is
    # rebuilt from scratch.
    assert all(child.stopped for child in stopped_before)
    assert {capture1.id, capture2.id}.isdisjoint(harness.receiver.registered)
    assert harness.lease.reset_calls == 1
    assert {capture1.id, capture2.id}.isdisjoint(harness.supervisor.processes)
    # The stopped cohorts are not silently resumed: with durable state
    # present the same tick discards their candidates and may only rebuild
    # them as fresh cohorts under new capture identities.
    for capture_id in (capture1.id, capture2.id):
        assert _capture_row(db, capture_id).status in {"draining", "stopped"}


# ---------------------------------------------------------------------------
# fence: restart with an unfinished snapshot discards candidates
# ---------------------------------------------------------------------------

def test_restart_discards_unfinished_snapshot_and_rebuilds(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _loading(db, connection, asset)

    first = new_harness(tmp_path)
    first.settle(2)  # a previous executor ran this cohort, left durable state
    assert first.children
    assert _state_dir(first, capture.id).exists()

    restarted = new_harness(tmp_path)  # fresh process table = executor restart
    restarted.settle(3)
    db.expire_all()

    failed = _run_row(db, run.id)
    assert failed.status == "failed"
    assert failed.last_error_code == "INITIAL_SNAPSHOT_INTERRUPTED"
    assert failed.owner_token is None
    assert failed.lease_expires_at is None
    replica = _replica_row(db, asset.id)
    assert replica.status == "blocked"  # never had an active replica
    assert replica.pending_run_id != run.id  # the candidate is discarded
    assert _capture_row(db, capture.id).status == "stopped"
    # No child was spawned for the abandoned cohort by the new executor; the
    # rebuild joined a new cohort (separate capture, new generation) that
    # started asynchronously once its source check confirmed.
    assert restarted.child_for(capture.id) is None
    successors = db.scalars(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id != capture.id)).all()
    assert len(successors) == 1
    assert successors[0].generation == capture.generation + 1
    assert restarted.child_for(successors[0].id) is not None
    rebuilt = _run_row(db, replica.pending_run_id)
    assert rebuilt.status == "loading"
    assert rebuilt.capture_id == successors[0].id
    # The drained cohort was cleaned under its own identity only.
    assert restarted.connector.cleanup_calls == [(connection.id, capture.id)]
    assert restarted.connector.cleanup_results == [True]


def test_first_start_without_durable_state_never_abandons(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _loading(db, connection, asset)
    harness = new_harness(tmp_path)
    harness.settle(3)
    db.expire_all()
    # No prior executor state: the loading run is serviced, not discarded.
    assert _run_row(db, run.id).status == "loading"
    assert _capture_row(db, capture.id).status in {"starting", "streaming"}
    assert harness.child_for(capture.id) is not None


def test_restart_abandons_on_receiver_evidence_even_without_state_dir(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _loading(db, connection, asset)
    # A previous executor received batches (heartbeat) but its state dir was
    # on a lost disk: candidates are still unrecoverable, never resumed.
    _capture_row(db, capture.id).heartbeat_at = datetime.now(timezone.utc)
    db.commit()

    harness = new_harness(tmp_path)
    harness.tick()
    db.expire_all()
    assert _run_row(db, run.id).status == "failed"
    assert _run_row(db, run.id).last_error_code == "INITIAL_SNAPSHOT_INTERRUPTED"
    assert _capture_row(db, capture.id).status == "draining"
    # Any child started in that tick belongs to the rebuild cohort, never
    # to the unrecoverable one.
    assert harness.child_for(capture.id) is None


# ---------------------------------------------------------------------------
# fence: all-active captures require durable offsets
# ---------------------------------------------------------------------------

def test_active_capture_without_offsets_is_blocked(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _active(db, connection, asset)
    harness = new_harness(tmp_path)
    harness.tick()
    db.expire_all()
    row = _capture_row(db, capture.id)
    assert row.status == "blocked"
    assert row.last_error_code == "REPLICA_OFFSETS_MISSING"
    assert _replica_row(db, asset.id).status == "rebuild_required"
    assert _run_row(db, run.id).status == "active"  # the active run survives
    assert harness.children == []
    assert harness.connector.validate_resume_calls == []


def test_active_capture_with_offsets_resumes_after_validation(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _active(db, connection, asset)
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    harness.settle(3)
    db.expire_all()
    assert harness.connector.validate_resume_calls == [capture.id]
    assert harness.child_for(capture.id) is not None
    row = _capture_row(db, capture.id)
    assert row.status in {"starting", "streaming"}
    assert row.last_error_code is None


# ---------------------------------------------------------------------------
# fence: the capacity bound never leaks a lease
# ---------------------------------------------------------------------------

def test_full_capacity_releases_the_lease_and_errors_nothing(
        db, admin_user, tmp_path, connection, new_harness):
    connection2 = _connection2(db)
    # capture1 is the older cohort, so it is always serviced first and wins
    # the only slot deterministically.
    capture1, _ = _loading(db, connection, _asset(
        db, admin_user, connection, asset_id="a1", table="orders"),
        created_offset_seconds=5)
    capture2, _ = _loading(db, connection2, _asset(
        db, admin_user, connection2, asset_id="a2", table="orders"))
    harness = new_harness(tmp_path, max_captures=1)
    harness.settle(3)
    db.expire_all()

    assert len(harness.children) == 1
    assert harness.child_for(capture1.id) is not None
    waiting = _capture_row(db, capture2.id)
    assert waiting.status == "starting"
    assert waiting.last_error_code is None
    # The capacity path released the second capture's lease: nothing held.
    assert "replica:" + capture2.id in harness.lease.released
    assert "replica:" + capture2.id not in harness.lease.acquired


def test_busy_slots_prevent_new_cohorts_from_even_preparing(
        db, admin_user, tmp_path, connection, new_harness):
    connection2 = _connection2(db)
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture1, _ = _active(db, connection, asset1, created_offset_seconds=5)
    asset2 = _asset(db, admin_user, connection2, asset_id="a2", table="orders")
    control.request_rebuild(db, asset2)
    db.commit()

    harness = new_harness(tmp_path, max_captures=1)
    _write_offsets(harness, capture1.id)
    harness.settle(3)
    db.expire_all()
    # Established streams are serviced first; the queued initializer on the
    # other connection is not prepared while every slot is busy.
    assert len(harness.children) == 1
    assert harness.child_for(capture1.id) is not None
    assert db.scalars(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.connection_id == connection2.id)).all() == []
    queued = db.scalar(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.asset_id == asset2.id))
    assert queued.status == "queued"


# ---------------------------------------------------------------------------
# fence: no platform secrets reach the child or loose files
# ---------------------------------------------------------------------------

def test_child_process_and_files_never_see_platform_secrets(
        db, admin_user, tmp_path, connection, new_harness, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://admin:hunter2@db/prod")
    monkeypatch.setenv("PLATFORM_ENCRYPTION_KEY", "slash-slash-rot")
    monkeypatch.setenv("N8N_API_KEY", "n8n-secret")
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _loading(db, connection, asset)
    harness = new_harness(tmp_path)
    harness.settle(3)

    child = harness.children[0]
    assert child is not None
    assert set(child.env) <= {"PATH", "JAVA_HOME", "LANG", "TZ"}
    assert "hunter2" not in str(child.env.values())
    assert "n8n-secret" not in str(child.env.values())

    # The rotated receiver secret travels only inside the 0600 properties
    # file under 0700 directories; the sink URL stays on loopback.
    kwargs = harness.connector.properties[0]
    assert kwargs["sink_url"].startswith(
        f"http://127.0.0.1:{harness.receiver.port}/replica/{capture.id}/")
    run_dir = Path(child.cwd)
    config_file = run_dir / "config" / "application.properties"
    assert config_file.is_file()
    assert (config_file.stat().st_mode & 0o777) == 0o600
    text = config_file.read_text(encoding="ascii")
    assert kwargs["secret_b64"] in text
    for directory in (_state_dir(harness, capture.id), run_dir,
                      run_dir / "config"):
        assert (directory.stat().st_mode & 0o777) == 0o700
    # The child arguments are the pinned Debezium image entrypoint only.
    assert "io.debezium.server.Main" in child.args


# ---------------------------------------------------------------------------
# fence: shutdown reclaims every child, the receiver and the pools
# ---------------------------------------------------------------------------

def test_shutdown_terminates_reaps_and_reclaims_everything(
        db, admin_user, tmp_path, connection, new_harness):
    connection2 = _connection2(db)
    capture1, _ = _loading(db, connection, _asset(
        db, admin_user, connection, asset_id="a1", table="orders"),
        created_offset_seconds=5)
    capture2, _ = _loading(db, connection2, _asset(
        db, admin_user, connection2, asset_id="a2", table="orders"))
    harness = new_harness(tmp_path)
    harness.settle(3)
    assert len(harness.children) == 2

    harness.supervisor.shutdown()
    for child in harness.children:
        assert "terminate" in child.calls
        assert "wait" in child.calls
        assert "reap" in child.calls
        assert not Path(child.cwd).exists()
    assert harness.receiver.stopped is True
    assert harness.lease.reset_calls == 1
    assert harness.supervisor.processes == {}
    # Durable offset state survives the executor exit.
    assert _state_dir(harness, capture1.id).exists()
    assert _state_dir(harness, capture2.id).exists()


# ---------------------------------------------------------------------------
# fence: credential changes interrupt the initializer
# ---------------------------------------------------------------------------

def test_credential_rotation_interrupts_initializer_and_rebuilds(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _loading(db, connection, asset)
    harness = new_harness(tmp_path)
    harness.settle(3)
    first_child = harness.children[0]
    assert first_child is not None and not first_child.stopped

    _rotate_password(db, connection)
    harness.tick()
    db.expire_all()

    assert first_child.stopped
    failed = _run_row(db, run.id)
    assert failed.status == "failed"
    assert failed.last_error_code == "INITIAL_SNAPSHOT_CONFIG_CHANGED"
    replica = _replica_row(db, asset.id)
    assert replica.pending_run_id != run.id  # the candidate is discarded
    assert _capture_row(db, capture.id).status == "draining"
    # The interrupted snapshot is rebuilt, never resumed over partial rows.
    rebuilt = _run_row(db, replica.pending_run_id)
    assert rebuilt.status in {"queued", "loading"}
    assert rebuilt.id != run.id


def test_credential_rotation_on_active_restarts_under_fresh_source_check(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _active(db, connection, asset)
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    harness.settle(3)
    first_child = harness.children[0]
    assert first_child is not None and not first_child.stopped
    assert harness.connector.validate_resume_calls == [capture.id]

    _rotate_password(db, connection)
    harness.settle(3)
    db.expire_all()

    assert first_child.stopped
    assert len(harness.children) == 2  # restarted asynchronously
    assert harness.children[1].cwd == first_child.cwd  # same run location
    # The restart's source check validated the *new* configuration.
    assert harness.connector.validate_resume_calls == [capture.id, capture.id]
    assert harness.connector.validate_resume_sources[-1][1]["password"] == "pw-two"
    row = _capture_row(db, capture.id)
    assert row.status in {"starting", "streaming"}
    assert row.last_error_code is None
    assert _run_row(db, run.id).status == "active"
    assert _state_dir(harness, capture.id).exists()  # offsets untouched


# ---------------------------------------------------------------------------
# fence: a new table gets its own cohort, established streams keep running
# ---------------------------------------------------------------------------

def test_new_table_joins_a_separate_capture_without_restart(
        db, admin_user, tmp_path, connection, new_harness):
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture1, run1 = _active(db, connection, asset1)
    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture1.id)
    harness.settle(3)
    harness.receiver.accepted[(capture1.id, capture1.generation)] = \
        harness.clock["now"]
    harness.tick()
    db.expire_all()
    established = harness.children[0]
    assert _capture_row(db, capture1.id).status == "streaming"

    asset2 = _asset(db, admin_user, connection, asset_id="a2", table="items")
    control.request_rebuild(db, asset2)
    db.commit()
    harness.settle(3)
    db.expire_all()

    # The established cohort was never restarted or drained.
    assert not established.stopped
    assert harness.child_for(capture1.id) is established
    assert _capture_row(db, capture1.id).status == "streaming"
    assert _run_row(db, run1.id).status == "active"
    # The new table joined a new immutable cohort of the same connection.
    capture2 = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.connection_id == connection.id,
        IntegratedReplicaCapture.id != capture1.id))
    assert capture2 is not None
    assert capture2.generation == capture1.generation + 1
    assert [item["asset_id"] for item in capture2.tables_json] == [asset2.id]
    new_child = harness.child_for(capture2.id)
    assert new_child is not None and new_child is not established
    # Distinct cohort: its own run directory and its own sink registration.
    assert Path(new_child.cwd) != Path(established.cwd)
    assert set(harness.receiver.registered) == {capture1.id, capture2.id}


# ---------------------------------------------------------------------------
# fence: blocked/draining slots clean only their own resources
# ---------------------------------------------------------------------------

def test_draining_capture_cleans_only_its_own_resources(
        db, admin_user, tmp_path, connection, new_harness):
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture1, _ = _loading(db, connection, asset1, created_offset_seconds=5)
    asset2 = _asset(db, admin_user, connection, asset_id="a2", table="items")
    capture2, _ = _active(db, connection, asset2, generation=2)
    _capture_row(db, capture1.id).status = "draining"
    db.commit()

    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture2.id)
    harness.settle(3)
    db.expire_all()

    assert harness.connector.cleanup_calls == [(connection.id, capture1.id)]
    assert _capture_row(db, capture1.id).status == "stopped"
    assert "replica:" + capture1.id in harness.lease.released
    # The healthy sibling cohort on the same connection keeps streaming.
    assert _capture_row(db, capture2.id).status in {"starting", "streaming"}
    assert harness.child_for(capture2.id) is not None
    assert not harness.child_for(capture2.id).stopped


def test_blocked_capture_stops_child_but_preserves_source_resources(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _loading(db, connection, asset)
    harness = new_harness(tmp_path)
    harness.settle(3)
    child = harness.children[0]
    assert child is not None and not child.stopped

    _capture_row(db, capture.id).status = "blocked"
    db.commit()
    harness.tick()
    db.expire_all()

    assert child.stopped
    assert capture.id not in harness.supervisor.processes
    assert "replica:" + capture.id in harness.lease.released
    # Blocked (e.g. unrecoverable source state) preserves slot and offsets
    # for diagnosis; it is never cleaned by the supervisor.
    assert harness.connector.cleanup_calls == []
    assert _capture_row(db, capture.id).status == "blocked"
    # No automatic rebuild: a new generation requires an explicit entry.
    assert _run_row(db, run.id).status == "loading"
    assert db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.asset_id == asset.id)).all() == [run]


# ---------------------------------------------------------------------------
# fence: "streaming" needs fresh, current-child evidence and an UP probe
# ---------------------------------------------------------------------------

def _probe_off():
    return lambda port: None


def test_streaming_requires_fresh_evidence_and_health_probe(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _loading(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True)
    harness.settle(3)
    db.expire_all()
    assert _capture_row(db, capture.id).status == "starting"

    key = (capture.id, capture.generation)
    harness.receiver.accepted[key] = harness.clock["now"]
    harness.tick()
    db.expire_all()
    assert _capture_row(db, capture.id).status == "streaming"

    harness.advance(20)  # evidence went stale
    harness.tick()
    db.expire_all()
    row = _capture_row(db, capture.id)
    assert row.status == "stale"
    assert row.last_error_code == "REPLICA_NO_FRESH_EVIDENCE"

    # A health probe that is not explicitly UP never certifies streaming:
    # the next periodic health read is scheduled and consumed asynchronously.
    harness.advance(2)
    harness.supervisor.health_probe = _probe_off()
    harness.receiver.accepted[key] = harness.clock["now"]
    harness.settle(2)
    db.expire_all()
    assert _capture_row(db, capture.id).status != "streaming"

    harness.advance(2)
    harness.supervisor.health_probe = lambda port: True
    harness.receiver.accepted[key] = harness.clock["now"]
    harness.settle(2)
    db.expire_all()
    assert _capture_row(db, capture.id).status == "streaming"


def test_restarted_child_must_earn_streaming_with_its_own_evidence(
        db, admin_user, tmp_path, connection, new_harness):
    """Evidence from a previous child must not certify its replacement.

    The capture has never been certified (no tick after the old evidence),
    so any flip to "streaming" after the restart can only come from the old
    child's evidence — which must never satisfy the health gate because it
    predates the replacement's start.
    """
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _active(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture.id)
    harness.settle(3)
    db.expire_all()
    assert _capture_row(db, capture.id).status == "starting"
    key = (capture.id, capture.generation)
    # The old child delivered evidence; no tick ran, so it was never used.
    harness.receiver.accepted[key] = harness.clock["now"]

    _rotate_password(db, connection)
    harness.advance(1)  # the old evidence stays inside the freshness window
    harness.settle(3)  # digest change restarts the child asynchronously
    db.expire_all()
    replacement = harness.child_for(capture.id)
    assert replacement is not None
    assert replacement is harness.children[1]  # a different child process
    assert harness.children[0].stopped
    item = harness.supervisor.processes[capture.id]
    assert harness.receiver.accepted[key] < item.started_at
    row = _capture_row(db, capture.id)
    assert row.status == "starting", (
        "streaming was certified by the replaced child's evidence")

    # The replacement earns streaming only with evidence of its own.
    harness.receiver.accepted[key] = harness.clock["now"]
    harness.tick()
    db.expire_all()
    assert _capture_row(db, capture.id).status == "streaming"


# ---------------------------------------------------------------------------
# fence: durable offset content/partition, never a silent re-snapshot
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case,code", [
    ("empty_list", "REPLICA_OFFSETS_INVALID"),
    ("wrong_partition", "SOURCE_PARTITION_CHANGED"),
    ("empty_offset", "REPLICA_OFFSETS_INVALID"),
    ("no_resume_coordinate", "REPLICA_OFFSETS_INVALID"),
])
def test_established_capture_with_unproven_offsets_becomes_explicit_gap(
        db, admin_user, tmp_path, connection, new_harness, case, code):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _active(db, connection, asset)
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    expected = _server(capture.id)
    harness.offsets.result = {
        "empty_list": [],
        "wrong_partition": [{"partition": {"server": "oo_replica_" + "0" * 32},
                             "offset": {"lsn": 1}}],
        "empty_offset": [{"partition": {"server": expected}, "offset": {}}],
        # A row LSN alone is not a resumable commit/progress coordinate.
        "no_resume_coordinate": [{"partition": {"server": expected},
                                  "offset": {"ts_usec": 5}}],
    }[case]

    harness.settle(2)
    db.expire_all()

    row = _capture_row(db, capture.id)
    assert row.status == "gap"
    assert row.last_error_code == code
    assert _replica_row(db, asset.id).status == "rebuild_required"
    assert _run_row(db, run.id).status == "active"  # data not silently dropped
    assert harness.child_for(capture.id) is None
    assert harness.connector.validate_resume_calls == []
    assert "replica:" + capture.id in harness.lease.released
    assert "replica:" + capture.id not in harness.lease.acquired


def test_offset_reader_rejection_is_a_gap_not_a_snapshot(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _active(db, connection, asset)
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    harness.offsets.error = OffsetReadError(OFFSETS_INVALID)

    harness.settle(2)
    db.expire_all()

    row = _capture_row(db, capture.id)
    assert row.status == "gap"
    assert row.last_error_code == OFFSETS_INVALID
    assert harness.child_for(capture.id) is None


def test_transient_offset_read_failure_retries_without_gap_or_snapshot(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _active(db, connection, asset)
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    harness.offsets.error = OSError("offset volume unavailable")

    harness.settle(2)
    db.expire_all()
    row = _capture_row(db, capture.id)
    assert row.status == "stale"  # reads stay available, not a proven gap
    assert row.last_error_code == OFFSETS_READ_FAILED
    assert harness.child_for(capture.id) is None
    assert harness.connector.validate_resume_calls == []
    # A retry backoff holds no child and must not pin the source lease.
    assert "replica:" + capture.id in harness.lease.released
    assert "replica:" + capture.id not in harness.lease.acquired

    harness.advance(RETRY_SECONDS + 1)
    harness.offsets.error = None
    harness.settle(3)
    db.expire_all()
    assert harness.child_for(capture.id) is not None
    assert _capture_row(db, capture.id).status in {"starting", "streaming"}


def test_default_offset_reader_reuses_the_shared_java_readoffsets(monkeypatch):
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"], calls["kwargs"] = args, kwargs
        return SimpleNamespace(returncode=0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(
        "app.data_channel.integrated.replica_executor.subprocess.run", fake_run)
    path = Path("/tmp/replica/offsets.dat")
    assert ReplicaSupervisor._read_offsets(None, path) == []
    assert "io.openontology.replica.ReadOffsets" in calls["args"]
    assert calls["args"][-1] == str(path)

    def truncated(args, **kwargs):
        return SimpleNamespace(returncode=4, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        "app.data_channel.integrated.replica_executor.subprocess.run", truncated)
    with pytest.raises(OffsetReadError) as exc:
        ReplicaSupervisor._read_offsets(None, path)
    assert exc.value.code == OFFSETS_INVALID


# ---------------------------------------------------------------------------
# fence: source cleanup only succeeds on a proven True; failures stay draining
# ---------------------------------------------------------------------------

def test_source_cleanup_false_is_never_reported_as_cleaned(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _loading(db, connection, asset)
    harness = new_harness(tmp_path)
    harness.settle(2)
    child = harness.children[0]
    assert child is not None and not child.stopped
    harness.connector.cleanup_sequence = [False, True]

    control.request_stop(db, asset)
    _capture_row(db, capture.id).status = "draining"
    db.commit()

    harness.tick()  # stops the child, then schedules async source cleanup
    harness.tick()  # consumes the False result
    db.expire_all()
    row = _capture_row(db, capture.id)
    assert child.stopped
    assert row.status == "draining"
    assert row.cleaned_at is None
    assert row.last_error_code == "REPLICA_CLEANUP_PENDING"
    assert harness.connector.cleanup_calls == [(connection.id, capture.id)]
    assert harness.connector.cleanup_results == [False]
    # Only the detached projection crosses the thread boundary, never the ORM
    # row or the session.
    assert harness.connector.cleanup_sources == [(connection.id, connection.config)]
    assert "replica:" + capture.id in harness.lease.released
    assert "replica:" + capture.id not in harness.lease.acquired

    harness.advance(RETRY_SECONDS + 1)
    harness.tick()  # retries after the bounded backoff
    harness.tick()  # consumes the True result
    db.expire_all()
    row = _capture_row(db, capture.id)
    assert row.status == "stopped"
    assert row.cleaned_at is not None
    assert row.last_error_code is None
    assert harness.connector.cleanup_results == [False, True]
    assert _run_row(db, run.id).status == "cancelled"


def test_source_cleanup_failure_never_claims_cleaned(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _loading(db, connection, asset)
    harness = new_harness(tmp_path)
    harness.settle(2)
    harness.connector.cleanup_errors = [
        connector.ConnectorError("SOURCE_UNREACHABLE"), None]

    control.request_stop(db, asset)
    _capture_row(db, capture.id).status = "draining"
    db.commit()

    harness.tick()
    harness.tick()
    db.expire_all()
    row = _capture_row(db, capture.id)
    assert row.status == "draining"
    assert row.cleaned_at is None
    assert row.last_error_code == "SOURCE_UNREACHABLE"
    assert harness.connector.cleanup_results == []

    harness.advance(RETRY_SECONDS + 1)
    harness.tick()
    harness.tick()
    db.expire_all()
    row = _capture_row(db, capture.id)
    assert row.status == "stopped"
    assert row.cleaned_at is not None
    assert harness.connector.cleanup_results == [True]


def test_wedged_source_cleanup_does_not_block_another_capture_apply(
        db, admin_user, tmp_path, connection, new_harness, monkeypatch):
    connection2 = _connection2(db)
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture1, _ = _loading(db, connection, asset1, created_offset_seconds=5)
    asset2 = _asset(db, admin_user, connection2, asset_id="a2", table="orders")
    capture2, _ = _active(db, connection2, asset2)
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture2.id)
    harness.settle(3)
    assert harness.child_for(capture2.id) is not None

    control.request_stop(db, asset1)
    _capture_row(db, capture1.id).status = "draining"
    db.commit()
    gate = harness.cleanup_gate(capture1.id)
    applied: list[str] = []
    monkeypatch.setattr(
        replica_runtime, "apply_capture",
        lambda session, capture_id, **kwargs:
            applied.append(capture_id) or 0)

    thread = threading.Thread(target=harness.supervisor.tick)
    thread.start()
    thread.join(1.0)

    # The tick returned while the cleanup worker is still inside source IO;
    # the other cohort's local apply kept advancing.
    assert not thread.is_alive()
    assert not gate.is_set()
    db.expire_all()
    assert applied == [capture2.id]
    assert _capture_row(db, capture1.id).status == "draining"

    gate.set()
    harness.settle(2)
    assert _capture_row(db, capture1.id).status == "stopped"


# ---------------------------------------------------------------------------
# fence: a stopped member keeps the frozen cohort schema; peers keep applying
# ---------------------------------------------------------------------------

def test_member_stop_keeps_frozen_schema_and_peer_streaming(
        db, admin_user, tmp_path, connection, new_harness):
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    asset2 = _asset(db, admin_user, connection, asset_id="a2", table="items")
    capture = _multi_active(db, connection, [asset1, asset2])
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    harness.settle(3)
    child = harness.child_for(capture.id)
    assert child is not None
    before = {member["asset_id"]: copy.deepcopy(member)
              for member in _capture_row(db, capture.id).tables_json}

    control.request_stop(db, asset1)
    db.commit()
    harness.tick()
    db.expire_all()

    row = _capture_row(db, capture.id)
    assert row.status not in {"draining", "stopped"}
    members = {member["asset_id"]: member for member in row.tables_json}
    stopped, live = members[asset1.id], members[asset2.id]
    assert stopped["detached"] is True
    # The frozen identity/schema stays so the existing child configuration is
    # byte-for-byte unchanged; only the detached flag was added.
    for key in ("run_id", "schema_name", "table_name", "schema_json",
                "schema_fingerprint"):
        assert stopped[key] == before[asset1.id][key]
    assert "detached" not in live
    assert live == before[asset2.id]

    # The existing child is never restarted for one stopped member; the peer
    # run keeps its slot and private relation.
    assert harness.child_for(capture.id) is child
    assert not child.stopped
    assert _run_row(db, before[asset2.id]["run_id"]).status == "active"
    assert _run_row(db, before[asset1.id]["run_id"]).status == "cancelled"
    assert _replica_row(db, asset1.id).status == "stopped"
    assert _run_row(db, before[asset1.id]["run_id"]).cleaned_at is not None
    assert _run_row(db, before[asset2.id]["run_id"]).cleaned_at is None

    # A later child restart must keep the frozen include list (including the
    # detached member) or replayed offsets would no longer match the journal;
    # preflight still checks only the live member.
    child.dead = True
    harness.tick()  # observes the exit and schedules its bounded backoff
    harness.advance(RETRY_SECONDS + 1)
    harness.settle(3)
    restarted = harness.child_for(capture.id)
    assert restarted is not None and restarted is not child
    assert {asset.id for asset in harness.connector.properties[-1]["assets"]} == {
        asset1.id, asset2.id}
    assert harness.connector.preflight_calls[-1] == [asset2.id]

    # Detach is what lets the asset DELETE hook remove exactly this member.
    assert cleanup.purge_asset(db, asset1.id) is True
    db.commit()
    assert db.get(IntegratedReplica, asset1.id) is None
    assert db.get(IntegratedReplica, asset2.id) is not None
    assert _capture_row(db, capture.id) is not None
    assert harness.connector.cleanup_calls == []  # cohort still serving


# ---------------------------------------------------------------------------
# fence: capacity is explicit; the oldest queued connection gets the slot
# ---------------------------------------------------------------------------

def test_oldest_queued_connection_gets_the_next_capture_slot(
        db, admin_user, tmp_path, new_harness):
    # Identifiers are inverse to queue age: sorting by id would starve the
    # older source forever.
    newer = Connection(id="a-newer", name="newer", kind="postgres", config={
        "host": "new.example", "port": 5432, "database": "app", "user": "u"})
    older = Connection(id="z-older", name="older", kind="postgres", config={
        "host": "old.example", "port": 5432, "database": "app", "user": "u"})
    db.add_all([newer, older])
    db.commit()
    old_asset = _asset(db, admin_user, older, asset_id="fold", table="orders")
    new_asset = _asset(db, admin_user, newer, asset_id="fnew", table="orders")
    old_run = control.request_rebuild(db, old_asset)
    new_run = control.request_rebuild(db, new_asset)
    db.flush()
    old_run.created_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    new_run.created_at = datetime.now(timezone.utc)
    db.commit()

    harness = new_harness(tmp_path, max_captures=1)
    harness.settle(3)
    db.expire_all()

    captures = db.scalars(sa.select(IntegratedReplicaCapture)).all()
    assert [capture.connection_id for capture in captures] == [older.id]
    assert harness.child_for(captures[0].id) is not None
    assert _run_row(db, new_run.id).status == "queued"


def test_full_capacity_reports_explicit_error_and_clears_on_admission(
        db, admin_user, tmp_path, connection, new_harness):
    connection2 = _connection2(db)
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture1, _ = _active(db, connection, asset1, created_offset_seconds=5)
    asset2 = _asset(db, admin_user, connection2, asset_id="a2", table="orders")
    queued = control.request_rebuild(db, asset2)
    db.commit()

    harness = new_harness(tmp_path, max_captures=1)
    _write_offsets(harness, capture1.id)
    harness.settle(3)
    db.expire_all()

    # The one slot is permanently owned by the established child; the queued
    # cohort is explicitly not progressing instead of silently waiting.
    replica = _replica_row(db, asset2.id)
    assert replica.status == "blocked"
    assert replica.last_error_code == CAPACITY
    assert _run_row(db, queued.id).status == "queued"
    assert db.scalars(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.connection_id == connection2.id)).all() == []

    # Free the slot: admission clears only the capacity marker.
    control.request_stop(db, asset1)
    db.commit()
    harness.settle(3)
    db.expire_all()
    replica = _replica_row(db, asset2.id)
    assert replica.last_error_code is None
    assert replica.status == "initializing"
    admitted = db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.connection_id == connection2.id))
    assert admitted is not None
    assert harness.child_for(admitted.id) is not None


def test_capacity_error_never_blocks_an_active_replica(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, active_run = _active(db, connection, asset)
    queued = control.request_rebuild(db, asset)
    db.commit()

    harness = new_harness(tmp_path, max_captures=1)
    _write_offsets(harness, capture.id)
    harness.settle(3)
    harness.tick()
    db.expire_all()

    replica = _replica_row(db, asset.id)
    assert replica.status == "stale"  # readable, never blocked
    assert replica.last_error_code == CAPACITY
    assert replica.active_run_id == active_run.id
    assert _run_row(db, queued.id).status == "queued"
    assert control.readable_run(db, asset)[1].id == active_run.id


# ---------------------------------------------------------------------------
# fence: ordered coverage cut (durable token, async emit, later-tick record)
# ---------------------------------------------------------------------------

def _start_pending_probe(harness, capture, db):
    """Child running with its first probe blocked inside source IO."""
    gate = harness.probe_gate()
    harness.tick(settle=False)
    harness.drain()  # durable offset read
    harness.tick(settle=False)
    harness.drain()  # source preflight/resume
    harness.tick(settle=False)  # start child + commit token + submit probe
    db.expire_all()
    return gate


def test_coverage_probe_token_is_durable_before_source_io(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _active(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture.id)

    gate = _start_pending_probe(harness, capture, db)
    row = _capture_row(db, capture.id)
    pending = (row.coverage_probe or {}).get("pending")
    call = harness.connector.probe_calls[-1]
    assert pending is not None and pending.get("frontier") is None
    assert call[1] == capture.id and call[2] == pending["id"]
    assert call[3] == connection.config  # detached projection, not ORM
    assert not gate.is_set()

    gate.set()
    harness.drain()
    harness.tick(settle=False)  # completed cut is recorded on the next tick
    db.expire_all()
    recorded = _capture_row(db, capture.id).coverage_probe["pending"]
    assert recorded["id"] == pending["id"]
    assert recorded["frontier"] == harness.connector.probe_result


def test_coverage_probe_timeout_retries_the_same_durable_token(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _active(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture.id)

    gate = _start_pending_probe(harness, capture, db)
    first_token = harness.connector.probe_calls[-1][2]
    item = harness.supervisor.processes[capture.id]
    assert item.probe_future is not None

    harness.advance(PROBE_TIMEOUT_SECONDS + 1)
    harness.tick(settle=False)  # timeout: abandon the stuck attempt
    assert item.probe_future is None
    assert len(item.probe_abandoned) == 1
    assert item.next_probe_at > harness.clock["now"]

    gate.set()
    harness.drain()  # the abandoned worker finishes without blocking apply
    harness.advance(RETRY_SECONDS + 1)
    harness.tick(settle=False)  # retry with the same pending token
    assert harness.connector.probe_calls[-1][2] == first_token
    assert sum(1 for call in harness.connector.probe_calls
               if call[2] == first_token) == 2

    harness.drain()
    harness.tick(settle=False)
    db.expire_all()
    recorded = _capture_row(db, capture.id).coverage_probe["pending"]
    assert recorded["id"] == first_token
    assert recorded["frontier"] == harness.connector.probe_result


def test_stuck_coverage_probe_never_blocks_local_apply(
        db, admin_user, tmp_path, connection, new_harness, monkeypatch):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _active(db, connection, asset)
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    harness.settle(2)  # offset read + source check in flight
    gate = harness.probe_gate()
    applied: list[str] = []
    monkeypatch.setattr(
        replica_runtime, "apply_capture",
        lambda session, capture_id, **kwargs:
            applied.append(capture_id) or 0)

    harness.tick(settle=False)  # child start + blocked probe submission
    harness.tick(settle=False)  # apply keeps advancing while probe is stuck
    db.expire_all()

    assert applied == [capture.id, capture.id]
    assert not gate.is_set()
    assert harness.connector.probe_calls


def test_ready_requires_an_applied_confirmed_cut_and_unapplied_lag_goes_stale(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _active(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture.id)
    harness.settle(4)  # child running, health probe consumed

    key = (capture.id, capture.generation)
    run.applied_position = {"capture_id": capture.id, "commit_seq": 0}
    _capture_row(db, capture.id).committed_seq = 0
    _capture_row(db, capture.id).coverage_probe = {"confirmed": {
        "capture_id": capture.id, "generation": capture.generation,
        "id": "cut-1", "commit_seq": 0, "position": {"lsn": 1},
        "confirmed_at": datetime.now(timezone.utc).isoformat()}}
    replica = _replica_row(db, asset.id)
    replica.status, replica.last_error_code = "stale", "OLD"
    harness.receiver.accepted[key] = harness.clock["now"]
    db.commit()

    harness.tick()
    db.expire_all()
    assert _replica_row(db, asset.id).status == "ready"
    assert _replica_row(db, asset.id).last_error_code is None
    assert _capture_row(db, capture.id).status == "streaming"

    # Received committed_seq 5 is not applied yet: within the grace window the
    # published replica stays ready, after 3s it is explicitly stale.
    _capture_row(db, capture.id).committed_seq = 5
    run.applied_position = {"capture_id": capture.id, "commit_seq": 0}
    harness.receiver.accepted[key] = harness.clock["now"]
    db.commit()
    harness.tick()
    db.expire_all()
    assert _replica_row(db, asset.id).status == "ready"

    harness.advance(4)
    harness.receiver.accepted[key] = harness.clock["now"]
    harness.tick()
    db.expire_all()
    replica = _replica_row(db, asset.id)
    assert replica.status == "stale"
    assert replica.last_error_code == "REPLICA_APPLY_LAG"


def test_ready_without_a_confirmed_cut_is_downgraded(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _active(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture.id)
    harness.settle(4)

    key = (capture.id, capture.generation)
    run.applied_position = {"capture_id": capture.id, "commit_seq": 0}
    _capture_row(db, capture.id).committed_seq = 0
    _capture_row(db, capture.id).coverage_probe = {}
    _replica_row(db, asset.id).status = "ready"
    harness.receiver.accepted[key] = harness.clock["now"]
    db.commit()

    harness.tick()
    db.expire_all()
    replica = _replica_row(db, asset.id)
    assert replica.status == "stale"
    assert replica.last_error_code == "REPLICA_COVERAGE_UNCONFIRMED"


def test_confirmed_cut_older_than_30s_makes_a_ready_replica_stale(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _active(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True)
    _write_offsets(harness, capture.id)
    harness.settle(4)

    key = (capture.id, capture.generation)
    run.applied_position = {"capture_id": capture.id, "commit_seq": 0}
    _capture_row(db, capture.id).committed_seq = 0
    _capture_row(db, capture.id).coverage_probe = {"confirmed": {
        "capture_id": capture.id, "generation": capture.generation,
        "id": "cut-old", "commit_seq": 0, "position": {"lsn": 1},
        "confirmed_at": (datetime.now(timezone.utc)
                         - timedelta(seconds=31)).isoformat()}}
    _replica_row(db, asset.id).status = "ready"
    harness.receiver.accepted[key] = harness.clock["now"]
    db.commit()

    harness.tick()
    db.expire_all()
    replica = _replica_row(db, asset.id)
    assert replica.status == "stale"
    assert replica.last_error_code == "REPLICA_COVERAGE_STALE"
    assert _capture_row(db, capture.id).status == "streaming"


def test_terminal_member_is_detached_before_runtime_apply(
        db, admin_user, tmp_path, connection, new_harness, monkeypatch):
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    asset2 = _asset(db, admin_user, connection, asset_id="a2", table="items")
    capture = _multi_active(db, connection, [asset1, asset2])
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    harness.settle(3)
    run2_id = next(member["run_id"] for member in capture.tables_json
                   if member["asset_id"] == asset2.id)
    control.request_stop(db, asset1)
    db.commit()

    seen: list[dict] = []
    real_apply = replica_runtime.apply_capture

    def recording(session, capture_id, **kwargs):
        row = session.get(IntegratedReplicaCapture, capture_id,
                          populate_existing=True)
        seen.append({member["asset_id"]: member.get("detached")
                     for member in row.tables_json})
        return real_apply(session, capture_id, **kwargs)

    monkeypatch.setattr(replica_runtime, "apply_capture", recording)
    harness.tick()
    db.expire_all()

    assert seen and seen[0][asset1.id] is True
    assert seen[0][asset2.id] is None
    assert _replica_row(db, asset1.id).status == "stopped"
    assert _run_row(db, run2_id).status == "active"


def test_changed_live_schema_blocks_before_any_source_check(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, run = _active(db, connection, asset)
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)
    stored = db.get(IntegratedAsset, asset.id)
    stored.schema_fingerprint = "schema-drifted"
    db.commit()

    harness.settle(2)
    db.expire_all()
    row = _capture_row(db, capture.id)
    assert row.status == "blocked"
    assert row.last_error_code == "REPLICA_SCHEMA_CHANGED"
    assert _replica_row(db, asset.id).status == "rebuild_required"
    assert _run_row(db, run.id).status == "active"
    assert harness.child_for(capture.id) is None
    assert harness.connector.validate_resume_calls == []
    assert harness.connector.preflight_calls == []


def test_restart_with_active_peer_fails_only_the_unfinished_member(
        db, admin_user, tmp_path, connection, new_harness):
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    asset2 = _asset(db, admin_user, connection, asset_id="a2", table="items")
    capture = _mixed_capture(db, connection, active_asset=asset1,
                             loading_asset=asset2)
    members = {member["asset_id"]: member
               for member in _capture_row(db, capture.id).tables_json}
    harness = new_harness(tmp_path)
    _write_offsets(harness, capture.id)  # previous executor left durable state
    harness.settle(3)
    db.expire_all()

    row = _capture_row(db, capture.id)
    assert row.status not in {"draining", "stopped"}
    active_run = _run_row(db, members[asset1.id]["run_id"])
    failed_run = _run_row(db, members[asset2.id]["run_id"])
    assert active_run.status == "active"
    assert failed_run.status == "failed"
    assert failed_run.last_error_code == "INITIAL_SNAPSHOT_INTERRUPTED"
    assert failed_run.owner_token is None and failed_run.lease_expires_at is None
    assert _replica_row(db, asset1.id).active_run_id == active_run.id
    replica2 = _replica_row(db, asset2.id)
    assert replica2.status == "rebuild_required"
    assert replica2.pending_run_id is None
    by_asset = {member["asset_id"]: member for member in row.tables_json}
    assert by_asset[asset2.id]["detached"] is True
    assert by_asset[asset1.id].get("detached") is None
    assert harness.child_for(capture.id) is not None
    # The active peer forced validate_resume; no silent snapshot path.
    assert harness.connector.validate_resume_calls == [capture.id]
    # No hidden automatic rebuild; the explicit rebuild entry stays the path.
    assert db.scalars(sa.select(IntegratedReplicaRun).where(
        IntegratedReplicaRun.asset_id == asset2.id)).all() == [failed_run]


def test_active_capture_with_catching_up_sibling_still_requires_offsets(
        db, admin_user, tmp_path, connection, new_harness):
    asset1 = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    asset2 = _asset(db, admin_user, connection, asset_id="a2", table="items")
    capture = _mixed_capture(db, connection, active_asset=asset1,
                             loading_asset=asset2)
    members = {member["asset_id"]: member for member in capture.tables_json}
    run2 = db.get(IntegratedReplicaRun, members[asset2.id]["run_id"])
    control.finish_load(db, run2.id, run2.owner_token, completion={
        "complete": True, "row_count": 0, "expected_rows": 0,
        "schema_fingerprint": run2.schema_fingerprint,
        "snapshot_id": capture.id})
    db.commit()

    harness = new_harness(tmp_path)
    harness.tick()
    db.expire_all()

    row = _capture_row(db, capture.id)
    assert row.status == "blocked"
    assert row.last_error_code == "REPLICA_OFFSETS_MISSING"
    assert _run_row(db, members[asset1.id]["run_id"]).status == "active"
    assert _run_row(db, run2.id).status == "catching_up"
    assert harness.child_for(capture.id) is None
    assert harness.connector.validate_resume_calls == []


# ---------------------------------------------------------------------------
# regression: production sessions disable autoflush
# ---------------------------------------------------------------------------

def test_streaming_status_survives_no_autoflush_probe_reload(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _active(db, connection, asset)
    harness = new_harness(tmp_path, health=lambda port: True,
                          session_factory=_no_autoflush_sessions())
    _write_offsets(harness, capture.id)
    harness.settle(4)

    key = (capture.id, capture.generation)
    harness.receiver.accepted[key] = harness.clock["now"]
    harness.tick()
    db.expire_all()

    # prepare_probe's populate_existing must not restore the old "starting"
    # row after the supervisor observed fresh evidence and health UP.
    assert _capture_row(db, capture.id).status == "streaming"
    assert _capture_row(db, capture.id).last_error_code is None


def test_draining_status_survives_no_autoflush_detach_reload(
        db, admin_user, tmp_path, connection, new_harness):
    asset = _asset(db, admin_user, connection, asset_id="a1", table="orders")
    capture, _ = _loading(db, connection, asset)
    harness = new_harness(tmp_path,
                          session_factory=_no_autoflush_sessions())
    harness.settle(2)
    harness.connector.cleanup_sequence = [False]

    control.request_stop(db, asset)
    _capture_row(db, capture.id).status = "draining"
    db.commit()
    harness.tick()
    harness.tick()
    db.expire_all()

    # confirm_detached's populate_existing must not resurrect "starting";
    # a failed cleanup stays draining with no cleaned_at claim.
    row = _capture_row(db, capture.id)
    assert row.status == "draining"
    assert row.cleaned_at is None
