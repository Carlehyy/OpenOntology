"""Supervise native initial snapshots and CDC for current local replicas.

Each immutable capture cohort owns its child, slot and persisted offsets.
Adding a table creates a separate cohort; it never restarts an existing stream.
A target PostgreSQL session lock fences capture ownership. Offset storage must
be shared by executors that can take over the same capture.

Stop/detach/cleanup contract:

- a member whose run is terminal and unreferenced is detached from the frozen
  cohort member list at a safe point (child stopped or after local apply), but
  its schema identity is retained so peers keep the same child configuration;
- a draining cohort only becomes ``stopped`` after the connector's source
  cleanup *returns True*. ``False`` (slot still active) and source failures
  keep it ``draining`` with a bounded retry, never claiming ``cleaned_at``;
- all source IO (preflight/resume, health, cleanup, offset read) runs on
  bounded pools so one unreachable source never blocks another cohort's local
  apply;
- an already published cohort may only restart after the durable Debezium
  offset file is read through the shared Java ``ReadOffsets`` reader and its
  content/partition is proven; empty, truncated, malformed or foreign
  partitions become an explicit ``gap`` instead of a silent re-snapshot.

Publication of ``catching_up`` runs is deliberately not decided here. The
verified-coverage barrier/proof is owned by the main capture/apply layer; the
single :meth:`_apply_capture` call is the seam where that asynchronous proof is
threaded in later.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa

from app.data_channel.connections.models import Connection
from app.data_channel.integrated import cdc_control
from app.data_channel.integrated import cdc_runtime as cdc_runtime
from app.data_channel.integrated import replica_cleanup
from app.data_channel.integrated import replica_connector as connector
from app.data_channel.integrated import replica_control as control
from app.data_channel.integrated import replica_coverage as coverage
from app.data_channel.integrated import replica_runtime as runtime
from app.data_channel.integrated.cdc_executor import (
    CHILD_JAVA_OPTS, DEBEZIUM_CLASSPATH, DEBEZIUM_MAIN, JAVA_BINARY, JAVA_HOME,
    ChildProcess, SourceLease, probe_health,
)
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import IntegratedReplicaCapture
from app.data_channel.integrated.replica_ingest import mark_capture_error
from app.data_channel.integrated.replica_models import IntegratedReplica, IntegratedReplicaRun

logger = logging.getLogger(__name__)
STATE_ROOT = Path(os.environ.get("REPLICA_STATE_ROOT", "/var/lib/openontology-replicas"))
RUN_ROOT = Path(os.environ.get("REPLICA_RUN_ROOT", "/tmp/integrated-replicas"))
HEARTBEAT_FILE = Path(os.environ.get("REPLICA_HEARTBEAT_FILE", "/tmp/integrated_replica_executor.heartbeat"))
MAX_CAPTURES = int(os.environ.get("REPLICA_MAX_CAPTURES", "2"))
INTERVAL = float(os.environ.get("REPLICA_RECONCILE_INTERVAL_SECONDS", "0.5"))
QUARKUS_PORT_BASE = int(os.environ.get("REPLICA_QUARKUS_PORT_BASE", "24024"))
MIN_INTERVAL = 0.05
RETRY_SECONDS = 10.0
SCHEMA_CHECK_SECONDS = 30.0
EVIDENCE_SECONDS = 15.0
# A source coverage probe that has not returned within this bound is retried
# with the same durable token; PG retries may insert more than one WAL
# message, and the first observed cut wins.
PROBE_TIMEOUT_SECONDS = 10.0
# Locally received but unapplied transactions make a published replica stale
# only after this grace window. Source idle time is never used for this.
APPLY_LAG_SECONDS = 3.0
# A confirmed ordered cut older than this no longer proves the source is still
# being observed (probe interval 10s + bounded source IO).
COVERAGE_STALE_SECONDS = 30.0

# Closed error codes for the local durability gate. Content problems are
# terminal gaps (a re-snapshot would silently skip the unproven interval);
# transient read failures stay retryable and never claim a gap.
OFFSETS_MISSING = "REPLICA_OFFSETS_MISSING"
OFFSETS_INVALID = "REPLICA_OFFSETS_INVALID"
OFFSETS_READ_FAILED = "REPLICA_OFFSETS_READ_FAILED"
OFFSET_PARTITION_CHANGED = "SOURCE_PARTITION_CHANGED"
CLEANUP_PENDING = "REPLICA_CLEANUP_PENDING"
CLEANUP_FAILED = "REPLICA_CLEANUP_FAILED"
CONNECTION_MISSING = "SOURCE_CONNECTION_MISSING"
# A queued cohort that cannot get a capture slot is reported explicitly.
# Capacity is a per-executor memory budget (one JVM child per capture), not a
# claim that hundreds of sources are being initialized. Scale by running more
# executors over the shared state volume or by raising REPLICA_MAX_CAPTURES
# only with the matching memory budget.
CAPACITY = "REPLICA_EXECUTOR_CAPACITY"


class OffsetReadError(RuntimeError):
    """The shared Java reader rejected the file content (never a value echo)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class CaptureProcess:
    capture_id: str
    index: int
    state_dir: Path
    run_dir: Path
    child: object | None = None
    config_digest: str | None = None
    next_schema_check: float = 0.0
    started_at: float = 0.0
    next_retry_at: float = 0.0
    source_check: Future | None = None
    checked_digest: str | None = None
    check_digest: str | None = None
    offset_check: Future | None = None
    offsets_verified: bool = False
    offset_read: Future | None = None
    next_offset_read: float = 0.0
    # Ordered source coverage cut (separate from transport liveness). The
    # durable token is prepared and committed before the source IO starts.
    probe_future: Future | None = None
    probe_token: str | None = None
    probe_generation: int | None = None
    probe_started_at: float = 0.0
    next_probe_at: float = 0.0
    # Timed-out probes may still be inside bounded source IO; never queue
    # unbounded retries behind them.
    probe_abandoned: set[Future] = field(default_factory=set)
    # Per-run start of locally received but not yet applied transactions.
    lag_since: dict[str, float] = field(default_factory=dict)
    health_up: bool = False
    health_read: Future | None = None
    next_health_read: float = 0.0


class ReplicaSupervisor:
    def __init__(self, *, session_factory=None, receiver=None, lease=None,
                 connector_module=connector, spawn=None, health_probe=probe_health,
                 state_root=STATE_ROOT, run_root=RUN_ROOT,
                 heartbeat_file=HEARTBEAT_FILE, max_captures=MAX_CAPTURES,
                 interval=INTERVAL, clock=time.time, offset_reader=None,
                 cleanup_workers=None):
        if session_factory is None:
            from app.shared.database import SessionLocal
            session_factory = SessionLocal
        self.sessions = session_factory
        self.receiver = receiver
        self.lease = lease
        self.connector = connector_module
        self.spawn = spawn or (lambda args, cwd, env: ChildProcess(args, cwd=cwd, env=env))
        self.health_probe = health_probe
        self.state_root, self.run_root = Path(state_root), Path(run_root)
        self.heartbeat_file = Path(heartbeat_file)
        self.max_captures = max(1, max_captures)
        # Never busy-loop when a misconfigured interval is zero/negative.
        self.interval = max(MIN_INTERVAL, interval)
        self.clock = clock
        self.read_offsets = offset_reader or self._read_offsets
        self.processes: dict[str, CaptureProcess] = {}
        self.retry_after: dict[str, float] = {}
        # Cleanup is source IO: it runs outside the reconcile transaction and
        # never occupies a capture slot. A completed future is consumed by the
        # next tick that sees the still-draining cohort.
        self.cleanups: dict[str, Future] = {}
        self.cleanup_after: dict[str, float] = {}
        self.stop_requested = threading.Event()
        self._source_checks = ThreadPoolExecutor(
            max_workers=self.max_captures, thread_name_prefix="replica-source-check")
        self._offset_reads = ThreadPoolExecutor(
            max_workers=min(self.max_captures, 4), thread_name_prefix="replica-offset-read")
        self._cleanup_pool = ThreadPoolExecutor(
            max_workers=cleanup_workers or max(1, min(self.max_captures, 4)),
            thread_name_prefix="replica-cleanup")
        # Dedicated bounded pool: a wedged source probe must never consume a
        # preflight slot or block another cohort's local apply.
        self._coverage_probes = ThreadPoolExecutor(
            max_workers=max(1, min(self.max_captures, 2)),
            thread_name_prefix="replica-coverage-probe")

    def run(self) -> int:
        if self.receiver is None:
            from app.data_channel.integrated.replica_receiver import create_replica_receiver
            self.receiver = create_replica_receiver(session_factory=self.sessions)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_requested.set())
        logger.info("replica executor starting max_captures=%s interval=%s",
                    self.max_captures, self.interval)
        try:
            while not self.stop_requested.is_set():
                started = time.monotonic()
                try:
                    self.tick()
                except Exception as exc:
                    logger.error("replica tick failed type=%s", type(exc).__name__)
                self.stop_requested.wait(max(0.0, self.interval - (time.monotonic() - started)))
        finally:
            self.shutdown()
        return 0

    def tick(self) -> None:
        active: set[str] = set()
        with self.sessions() as db:
            if self.lease is None:
                self.lease = SourceLease(db.get_bind())
            if not self.lease.healthy():
                self._stop_all()
                self.lease.reset()
            runtime.queue_registered(db)
            db.commit()
            # Established streams are always serviced before new initializers.
            captures = list(db.scalars(sa.select(IntegratedReplicaCapture).where(
                IntegratedReplicaCapture.status != "stopped")
                .order_by(IntegratedReplicaCapture.created_at)))
            for capture in captures:
                capture_id = capture.id
                active.add(capture_id)
                try:
                    self._reconcile(db, capture_id)
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    code = getattr(exc, "code", "REPLICA_EXECUTOR_INTERNAL")
                    retryable = code in {
                        "REPLICA_EXECUTOR_INTERNAL", "SOURCE_UNREACHABLE",
                        "SOURCE_AUTH_FAILED", OFFSETS_READ_FAILED}
                    logger.error("replica capture failed capture=%s code=%s type=%s",
                                 capture_id, code, type(exc).__name__)
                    item = self.processes.get(capture_id)
                    if item is not None:
                        self._stop(item)
                        if retryable:
                            item.next_retry_at = self.clock() + RETRY_SECONDS
                    # We are no longer servicing this cohort: another executor
                    # may take the fence instead of waiting for a backoff.
                    if self.lease is not None:
                        self.lease.release("replica:" + capture_id)
                    if retryable:
                        self.retry_after[capture_id] = self.clock() + RETRY_SECONDS
                    self._error(capture_id, code, retryable=retryable)
            # Fair FIFO: the connection with the oldest queued run gets the
            # next free capture slot, regardless of its identifier. A static
            # connection ordering could starve newer sources forever.
            queued = db.execute(sa.select(
                IntegratedAsset.connection_id,
                sa.func.min(IntegratedReplicaRun.created_at).label("queued_at"))
                .join(IntegratedReplicaRun, IntegratedReplicaRun.asset_id == IntegratedAsset.id)
                .where(IntegratedReplicaRun.status == "queued")
                .group_by(IntegratedAsset.connection_id)
                .order_by(sa.func.min(IntegratedReplicaRun.created_at),
                          IntegratedAsset.connection_id)).all()
            db.rollback()
            for connection_id, _ in queued:
                if len(self.processes) >= self.max_captures:
                    break
                try:
                    capture = runtime.prepare_capture(db, connection_id)
                    if capture is not None:
                        self._clear_capacity_error(db, capture)
                    capture_id = capture.id if capture is not None else None
                    db.commit()
                    if capture_id:
                        active.add(capture_id)
                        self._reconcile(db, capture_id)
                        db.commit()
                except Exception as exc:
                    db.rollback()
                    logger.error("replica prepare failed type=%s", type(exc).__name__)
            if len(self.processes) >= self.max_captures and queued:
                # Never leave capacity waiting silent: the queued replica is
                # explicitly not progressing. An active replica of the same
                # asset stays readable (stale), never blocked.
                self._report_capacity(db)
                db.commit()
            runtime.cleanup_relations(db)
            db.commit()
        self._sweep_bookkeeping(active)
        self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_file.touch()

    @staticmethod
    def _live_runs(db, capture_id: str) -> list[IntegratedReplicaRun]:
        """Refresh live members from the row, never a stale identity-map copy.

        Production sessions disable autoflush; a run loaded before
        prepare_capture/begin_load can still say "queued" in memory. Reading
        the live set without populate_existing would then skip lease renewal
        and abandonment of a genuinely loading candidate.
        """
        return list(db.scalars(sa.select(IntegratedReplicaRun).where(
            IntegratedReplicaRun.capture_id == capture_id,
            IntegratedReplicaRun.status.in_(["loading", "catching_up", "active"]))
            .order_by(IntegratedReplicaRun.asset_id)
            .execution_options(populate_existing=True)))

    def _reconcile(self, db, capture_id: str) -> None:
        capture = db.get(IntegratedReplicaCapture, capture_id, populate_existing=True)
        if capture is None:
            return
        key = "replica:" + capture.id
        if not self.lease.acquire(key):
            return
        runs = self._live_runs(db, capture_id)
        if capture.status == "draining" or not runs:
            item = self.processes.pop(capture_id, None)
            if item:
                self._stop(item)
            # Stop accepting ingest before proving members are detached.
            if capture.status != "draining":
                capture.status = "draining"
            detached = self._detach(db, capture)
            connection = db.get(Connection, capture.connection_id)
            if connection is None:
                # No source identity remains to prove resource cleanup.
                capture.status, capture.cleaned_at = "stopped", None
                capture.last_error_code = CONNECTION_MISSING
                self._forget(capture_id)
                self.lease.release(key)
                return
            state = self._cleanup_state(db, capture, connection)
            if state != "cleaned":
                # False, failures and in-flight cleanups keep the cohort
                # draining; cleaned_at is only written for a confirmed True.
                self.lease.release(key)
                return
            capture.status, capture.cleaned_at = "stopped", runtime.now()
            if detached:
                capture.last_error_code = None
            self._forget(capture_id)
            self.lease.release(key)
            return
        if capture.status in {"blocked", "gap"}:
            item = self.processes.pop(capture_id, None)
            if item:
                self._stop(item)
            # Source resources stay for diagnosis, but terminal members can no
            # longer be applied or published and may safely detach.
            self._detach(db, capture)
            self.lease.release(key)
            return
        item = self.processes.get(capture_id)
        if item is None:
            if len(self.processes) >= self.max_captures:
                self.lease.release(key)
                return
            if self.retry_after.get(capture_id, 0) > self.clock():
                self.lease.release(key)
                return
            # A process restart cannot recover the source transaction used by
            # an unfinished initial snapshot. With an active peer present we
            # must not drain the whole cohort (or bypass offsets): fail only
            # the unfinished member and keep the active stream fenced.
            unfinished = [run for run in runs if run.status == "loading"]
            state_dir = self.state_root / capture.id
            if unfinished and (state_dir.exists() or capture.heartbeat_at is not None):
                if any(run.status == "active" for run in runs):
                    self._fail_unfinished_members(
                        db, capture_id, unfinished,
                        code="INITIAL_SNAPSHOT_INTERRUPTED")
                    self._detach(db, capture)
                    runs = [run for run in runs if run.status == "active"]
                else:
                    runtime.abandon_initialization(db, capture_id,
                        code="INITIAL_SNAPSHOT_INTERRUPTED", retry=True)
                    self.lease.release(key)
                    return
            index = next((index for index in range(self.max_captures)
                          if index not in {process.index
                                           for process in self.processes.values()}), None)
            if index is None:
                self.lease.release(key)
                return
            item = CaptureProcess(capture.id, index, state_dir, self.run_root / capture.id)
            self.processes[capture_id] = item
        connection = db.get(Connection, capture.connection_id)
        if connection is None or cdc_control.source_identity(connection) != capture.identity_fingerprint:
            mark_capture_error(db, capture_id, "SOURCE_IDENTITY_CHANGED", retryable=False)
            self._stop(item)
            self.lease.release(key)
            return
        # Two descriptor sets, deliberately separate:
        # - property_assets: the frozen cohort manifest (including detached
        #   members) so a restart keeps the exact include list/columns the
        #   offsets were produced with; filtering by live runs would change
        #   the replayed transaction set.
        # - live_assets: only members that still apply, used for source
        #   preflight/schema-drift. A purged asset row is not required because
        #   the frozen member still carries the physical descriptor.
        members_by_run = {member.get("run_id"): member
                          for member in (capture.tables_json or [])
                          if isinstance(member, dict)}
        property_assets = []
        for member in capture.tables_json or []:
            if not isinstance(member, dict):
                mark_capture_error(db, capture_id, "CAPTURE_TABLES_INVALID",
                                   retryable=False)
                self._stop(item)
                self.lease.release(key)
                return
            schema_json = member.get("schema_json")
            fingerprint = member.get("schema_fingerprint")
            if not isinstance(schema_json, dict) or not isinstance(fingerprint, str):
                run = db.get(IntegratedReplicaRun, member.get("run_id"))
                if run is None:
                    mark_capture_error(db, capture_id, "CAPTURE_TABLES_INVALID",
                                       retryable=False)
                    self._stop(item)
                    self.lease.release(key)
                    return
                schema_json, fingerprint = run.schema_json, run.schema_fingerprint
            if (not isinstance(member.get("asset_id"), str)
                    or not isinstance(member.get("schema_name"), str)
                    or not isinstance(member.get("table_name"), str)):
                mark_capture_error(db, capture_id, "CAPTURE_TABLES_INVALID",
                                   retryable=False)
                self._stop(item)
                self.lease.release(key)
                return
            property_assets.append(SimpleNamespace(
                id=member["asset_id"], schema_name=member["schema_name"],
                table_name=member["table_name"], schema_fingerprint=fingerprint,
                schema_json=copy.deepcopy(schema_json)))

        live_assets = []
        for run in runs:
            member = members_by_run.get(run.id) or {}
            asset = db.get(IntegratedAsset, run.asset_id)
            if asset is not None and asset.schema_fingerprint != run.schema_fingerprint:
                # A restart or live check must not validate a new schema while
                # the child still carries the frozen include descriptors.
                mark_capture_error(db, capture_id, "REPLICA_SCHEMA_CHANGED",
                                   retryable=False)
                self._stop(item)
                self.lease.release(key)
                return
            schema_name = getattr(asset, "schema_name", None) or member.get("schema_name")
            table_name = getattr(asset, "table_name", None) or member.get("table_name")
            if not isinstance(schema_name, str) or not isinstance(table_name, str):
                mark_capture_error(db, capture_id, "SOURCE_TABLE_CHANGED",
                                   retryable=False)
                self._stop(item)
                self.lease.release(key)
                return
            live_assets.append(SimpleNamespace(
                id=run.asset_id, schema_name=schema_name, table_name=table_name,
                schema_fingerprint=run.schema_fingerprint,
                schema_json=copy.deepcopy(run.schema_json)))
        config_digest = hashlib.sha256(repr(connection.config).encode()).hexdigest()
        if item.child is not None and item.child.poll() is not None:
            code = getattr(item.child, "error_code", None) or "REPLICA_CHILD_EXITED"
            self._stop(item)
            unfinished = [run for run in runs if run.status == "loading"]
            if unfinished:
                if any(run.status == "active" for run in runs):
                    self._fail_unfinished_members(db, capture_id, unfinished,
                                                  code=code)
                    self._detach(db, capture)
                    runs = [run for run in runs if run.status == "active"]
                else:
                    runtime.abandon_initialization(db, capture_id, code=code,
                                                   retry=True)
                    self.lease.release(key)
                    return
            mark_capture_error(db, capture_id, code, retryable=code != "SOURCE_LOG_GAP")
            if code == cdc_runtime.CODE_SOURCE_LOG_GAP:
                # An unusable source log position is a proven gap: never let
                # the bounded restart masquerade as a fresh snapshot.
                capture.status = "gap"
            item.next_retry_at = self.clock() + RETRY_SECONDS
        if item.child is not None and config_digest != item.config_digest:
            self._stop(item)
            unfinished = [run for run in runs if run.status == "loading"]
            if unfinished:
                if any(run.status == "active" for run in runs):
                    self._fail_unfinished_members(
                        db, capture_id, unfinished,
                        code="INITIAL_SNAPSHOT_CONFIG_CHANGED")
                    self._detach(db, capture)
                    runs = [run for run in runs if run.status == "active"]
                else:
                    runtime.abandon_initialization(db, capture_id,
                        code="INITIAL_SNAPSHOT_CONFIG_CHANGED", retry=True)
                    self.lease.release(key)
                    return
        runtime.renew_initializers(db, capture_id)
        if item.child is None:
            if item.next_retry_at > self.clock():
                # Rotation: a backoff with no child must not pin the source
                # fence. Another executor may take the next turn.
                self.lease.release(key)
                return
            # Any published member means durable offsets are mandatory; a
            # non-active sibling must never turn the restart into a silent
            # re-snapshot or bypass the offset proof.
            established = any(run.status == "active" for run in runs)
            if established:
                verdict = self._durable_offsets(db, item, capture, connection)
                if verdict is None:
                    return  # bounded local read in flight
                if verdict is False:
                    # Retryable read failures and terminal gaps both stop the
                    # restart; the fence is released for a fair retry.
                    self.lease.release(key)
                    return
            if not self._source_ready(item, connection, live_assets, config_digest,
                                      resume=established):
                return
            self._start(item, capture, connection, property_assets, config_digest)
        elif not self._source_ready(item, connection, live_assets, config_digest, resume=False):
            # The last successful check remains valid while its bounded
            # successor runs. Source IO never blocks target transaction apply.
            pass
        # ``applied_probe`` compares the frozen membership with the live runs.
        # A terminal member that is still listed would reject the cut, so
        # detach it before apply; the second pass catches runs retired by this
        # apply. Both lock replica -> run -> capture, the same order as apply,
        # so this cannot form an ORM lock cycle.
        self._detach(db, capture)
        self._apply_capture(db, capture_id)
        self._detach(db, capture)
        # A completed coverage cut is persisted on the next tick; its token is
        # fenced by capture generation, so a late old result cannot overwrite
        # a newer pending probe.
        self._consume_probe(db, capture, item)
        self._prune_offsets(db, item)
        self._check_health(item)
        evidence = self.receiver.last_accepted_at(capture.id, capture.generation)
        healthy = (evidence is not None and evidence >= item.started_at
                   and self.clock() - evidence <= EVIDENCE_SECONDS
                   and item.checked_digest == config_digest and item.health_up
                   and getattr(item.child, "error_code", None) is None)
        if healthy:
            if capture.status != "streaming":
                logger.info("replica capture streaming capture=%s", capture.id)
            capture.status, capture.last_error_code = "streaming", None
            confirmed_seq = self._confirmed_probe_seq(capture)
            confirmed_age = self._confirmed_probe_age(capture)
            coverage_stale = (confirmed_age is not None
                              and confirmed_age > COVERAGE_STALE_SECONDS)
            active_runs = [run for run in self._live_runs(db, capture_id)
                           if run.status == "active"]
            active_ids = {run.id for run in active_runs}
            for run in active_runs:
                replica = db.get(IntegratedReplica, run.asset_id)
                if replica is None:
                    continue
                cursor = self._run_cursor(run)
                lagging = cursor < capture.committed_seq
                if lagging:
                    item.lag_since.setdefault(run.id, self.clock())
                else:
                    item.lag_since.pop(run.id, None)
                lagged = (lagging and run.id in item.lag_since
                          and self.clock() - item.lag_since[run.id] >= APPLY_LAG_SECONDS)
                if (replica.active_run_id == run.id and not replica.pending_run_id
                        and replica.coverage_confirmed_at is not None
                        and confirmed_seq is not None and cursor >= confirmed_seq
                        and not lagging and not coverage_stale):
                    replica.status, replica.last_error_code = "ready", None
                elif (lagged and replica.active_run_id == run.id
                      and not replica.pending_run_id):
                    # Received locally but not applied: readable stale data,
                    # never a false "ready". Source idle time is irrelevant.
                    replica.status, replica.last_error_code = \
                        "stale", "REPLICA_APPLY_LAG"
                elif (coverage_stale and replica.active_run_id == run.id
                      and not replica.pending_run_id):
                    # The newest confirmed cut is too old to prove the source
                    # is still being observed, even if heartbeats arrived.
                    replica.status, replica.last_error_code = \
                        "stale", "REPLICA_COVERAGE_STALE"
                elif (confirmed_seq is None and replica.active_run_id == run.id
                      and not replica.pending_run_id
                      and replica.status == "ready"):
                    # A published snapshot without an ordered, applied cut is
                    # readable history, not proven current coverage.
                    replica.status, replica.last_error_code = \
                        "stale", "REPLICA_COVERAGE_UNCONFIRMED"
            item.lag_since = {run_id: since for run_id, since
                              in item.lag_since.items() if run_id in active_ids}
        elif self.clock() - item.started_at > EVIDENCE_SECONDS:
            mark_capture_error(db, capture_id, "REPLICA_NO_FRESH_EVIDENCE", retryable=True)
        # Prepare and commit the next ordered cut after local apply; source IO
        # starts only after that commit and never blocks another cohort.
        self._prepare_probe(db, capture, item, connection)

    @staticmethod
    def _run_cursor(run) -> int:
        position = run.applied_position
        sequence = position.get("commit_seq") if isinstance(position, dict) else None
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            return 0
        return sequence

    @staticmethod
    def _confirmed_probe(capture):
        state = capture.coverage_probe
        if not isinstance(state, dict):
            return None
        confirmed = state.get("confirmed")
        if (not isinstance(confirmed, dict)
                or confirmed.get("capture_id") != capture.id
                or confirmed.get("generation") != capture.generation):
            return None
        return confirmed

    @classmethod
    def _confirmed_probe_seq(cls, capture) -> int | None:
        confirmed = cls._confirmed_probe(capture)
        if confirmed is None:
            return None
        sequence = confirmed.get("commit_seq")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            return None
        return sequence

    @classmethod
    def _confirmed_probe_age(cls, capture) -> float | None:
        confirmed = cls._confirmed_probe(capture)
        if confirmed is None:
            return None
        value = confirmed.get("confirmed_at")
        if not isinstance(value, str):
            return None
        try:
            confirmed_at = datetime.fromisoformat(value)
        except ValueError:
            return None
        if confirmed_at.tzinfo is None:
            confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - confirmed_at).total_seconds()

    def _consume_probe(self, db, capture, item) -> None:
        """Persist a completed cut on a later tick; never block apply for it."""
        future = item.probe_future
        if future is None:
            return
        if not future.done():
            if self.clock() - item.probe_started_at >= PROBE_TIMEOUT_SECONDS:
                # The durable token stays pending. A retry emits the same
                # token (PG may insert several WAL messages; first wins).
                item.probe_abandoned.add(future)
                item.probe_future = None
                item.probe_token = None
                item.probe_generation = None
                item.next_probe_at = self.clock() + RETRY_SECONDS
            return
        item.probe_future = None
        token, item.probe_token = item.probe_token, None
        item.probe_generation = None
        if not token:
            return
        try:
            frontier = future.result()
        except Exception as exc:
            code = getattr(exc, "code", None) or "REPLICA_COVERAGE_UNAVAILABLE"
            item.next_probe_at = self.clock() + RETRY_SECONDS
            logger.error("replica coverage probe failed capture=%s code=%s type=%s",
                         capture.id, code, type(exc).__name__)
            return
        if not isinstance(frontier, dict) or not frontier:
            item.next_probe_at = self.clock() + RETRY_SECONDS
            return
        try:
            coverage.record_probe(db, capture.id, token, frontier)
        except coverage.ReplicaCoverageError as exc:
            item.next_probe_at = self.clock() + RETRY_SECONDS
            logger.error("replica coverage record rejected capture=%s code=%s",
                         capture.id, exc.code)

    def _prepare_probe(self, db, capture, item, connection) -> None:
        """Durably prepare a cut, commit the token, then start bounded source IO."""
        if item.probe_future is not None:
            return
        if self.clock() < item.next_probe_at:
            return
        emit = getattr(self.connector, "emit_coverage_probe", None)
        if emit is None:
            # Coverage connector not wired yet; never persist a token that no
            # thread can emit.
            return
        item.probe_abandoned = {future for future in item.probe_abandoned
                                if not future.done()}
        if item.probe_abandoned:
            # One stuck source IO attempt is enough; retry once it returns.
            item.next_probe_at = self.clock() + RETRY_SECONDS
            return
        # Production sessions disable autoflush. Flush the streaming/ready
        # transition first or prepare_probe's populate_existing reload would
        # restore the old "starting" row and commit it again.
        db.flush()
        pending = coverage.prepare_probe(db, capture.id)
        if pending is None:
            return
        token = pending.get("id")
        if not isinstance(token, str) or not token:
            return
        source = SimpleNamespace(id=connection.id, kind=connection.kind,
                                 config=copy.deepcopy(connection.config))
        # The pending token and all local apply results must be durable before
        # the source IO starts, and no target row lock may be held while emit
        # runs outside this transaction.
        db.commit()
        item.probe_token = token
        item.probe_generation = pending.get("generation")
        item.probe_started_at = self.clock()
        item.probe_future = self._coverage_probes.submit(
            emit, source, capture.id, token)

    def _apply_capture(self, db, capture_id: str) -> int:
        """Single local-apply seam.

        Publication of a ``catching_up`` run requires the main layer's
        verified-coverage barrier and is never decided by this supervisor.
        Keeping the call in one place leaves that asynchronous proof a clear
        integration point without changing the lifecycle state machine.
        """
        return runtime.apply_capture(db, capture_id)

    def _fail_unfinished_members(self, db, capture_id: str, runs, *,
                                 code: str) -> int:
        """Fail only unrecoverable initializers; never drain an active peer.

        The candidate cannot be resumed after the child/offsets were lost, but
        an already published member of the same cohort must keep streaming.
        The failed member stays explicit (``rebuild_required`` without an
        active run) and is detached so ordered cuts see the live set again.
        """
        failed = 0
        for initial in runs:
            if initial.status != "loading":
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
            run.completed_at = runtime.now()
            replica.pending_run_id = None
            replica.last_error_code = code
            replica.status = "stale" if replica.active_run_id else "rebuild_required"
            failed += 1
        if failed:
            db.flush()
        return failed

    def _report_capacity(self, db) -> int:
        """Make a queued cohort's non-progress explicit, without blocking reads.

        An asset that still has an active replica stays readable (``stale``);
        only an asset with no published data becomes ``blocked``. A stronger
        existing error (``rebuild_required`` or a non-capacity code) is never
        overwritten.
        """
        rows = db.execute(sa.select(IntegratedReplicaRun, IntegratedReplica)
            .join(IntegratedReplica,
                  IntegratedReplica.asset_id == IntegratedReplicaRun.asset_id)
            .where(IntegratedReplicaRun.status == "queued")
            .order_by(IntegratedReplicaRun.created_at)).all()
        marked = 0
        for run, replica in rows:
            if replica.pending_run_id != run.id:
                continue
            if replica.status in {"rebuild_required", "stopped"}:
                continue
            if replica.last_error_code not in {None, CAPACITY}:
                continue
            target = "stale" if replica.active_run_id else "blocked"
            if replica.status != target or replica.last_error_code != CAPACITY:
                replica.status, replica.last_error_code = target, CAPACITY
                marked += 1
        if marked:
            db.flush()
        return marked

    def _clear_capacity_error(self, db, capture) -> None:
        """Admission clears only the capacity marker; real errors stay."""
        for member in capture.tables_json or []:
            if not isinstance(member, dict):
                continue
            run = db.get(IntegratedReplicaRun, member.get("run_id"))
            replica = db.get(IntegratedReplica, member.get("asset_id"))
            if run is None or replica is None:
                continue
            if (replica.pending_run_id != run.id
                    or replica.last_error_code != CAPACITY):
                continue
            replica.last_error_code = None
            if replica.status in {"blocked", "stale"}:
                replica.status = "initializing"

    def _detach(self, db, capture) -> bool:
        """Mark terminal, unreferenced members detached at a safe point.

        Never rewrites schema/table/asset/run identities and never touches a
        live peer. The caller guarantees the cohort cannot still write (child
        stopped, or the member's run is terminal and pointers are broken).
        """
        # Production sessions disable autoflush; flush pending status changes
        # before confirm_detached's populate_existing reload can overwrite
        # them with the old row (e.g. draining -> starting).
        db.flush()
        try:
            replica_cleanup.confirm_detached(db, capture.id)
            return True
        except replica_cleanup.ReplicaCleanupError as exc:
            capture.last_error_code = exc.code
            logger.error("replica detach rejected capture=%s code=%s",
                         capture.id, exc.code)
            return False

    def _cleanup_state(self, db, capture, connection) -> str:
        """Advance one async source-cleanup attempt; never claims success."""
        capture_id = capture.id
        future = self.cleanups.get(capture_id)
        if future is not None:
            if not future.done():
                return "pending"
            self.cleanups.pop(capture_id, None)
            try:
                cleaned = future.result()
            except Exception as exc:
                code = getattr(exc, "code", None) or CLEANUP_FAILED
                capture.last_error_code = code
                self.cleanup_after[capture_id] = self.clock() + RETRY_SECONDS
                logger.error("replica source cleanup failed capture=%s code=%s type=%s",
                             capture_id, code, type(exc).__name__)
                return "failed"
            if cleaned is True:
                self.cleanup_after.pop(capture_id, None)
                return "cleaned"
            # A still-active slot (or any non-True result) is not proof.
            capture.last_error_code = CLEANUP_PENDING
            self.cleanup_after[capture_id] = self.clock() + RETRY_SECONDS
            logger.info("replica source cleanup deferred capture=%s", capture_id)
            return "pending"
        if self.clock() < self.cleanup_after.get(capture_id, 0.0):
            return "backoff"
        # ORM objects and sessions never cross threads. Copy only the fields
        # consumed by the connector's existing control operation.
        source = SimpleNamespace(id=connection.id, kind=connection.kind,
                                 config=copy.deepcopy(connection.config))
        self.cleanups[capture_id] = self._cleanup_pool.submit(
            self.connector.cleanup, source, capture_id)
        return "scheduled"

    def _forget(self, capture_id: str) -> None:
        self.retry_after.pop(capture_id, None)
        self.cleanup_after.pop(capture_id, None)

    def _sweep_bookkeeping(self, active: set[str]) -> None:
        """Drop retry/cleanup state for cohorts that no longer exist."""
        for capture_id, future in list(self.cleanups.items()):
            if capture_id not in active and future.done():
                self.cleanups.pop(capture_id, None)
        for capture_id in list(self.cleanup_after):
            if capture_id not in active:
                self.cleanup_after.pop(capture_id, None)
        for capture_id in list(self.retry_after):
            if capture_id not in active:
                self.retry_after.pop(capture_id, None)

    def _durable_offsets(self, db, item, capture, connection):
        """Gate an established restart on the real Debezium offset content.

        Returns ``True`` only after a single partition for this capture has a
        non-empty, dialect-resumable offset. ``None`` means the bounded Java
        read is still in flight; ``False`` records either an explicit gap
        (content/partition cannot be trusted) or a retryable read failure.
        """
        if item.offsets_verified:
            return True
        if item.offset_check is not None:
            if not item.offset_check.done():
                return None
            future, item.offset_check = item.offset_check, None
            try:
                entries = future.result()
            except OffsetReadError as exc:
                self._offset_gap(db, capture, exc.code)
                return False
            except Exception as exc:
                item.next_retry_at = self.clock() + RETRY_SECONDS
                self.retry_after[capture.id] = item.next_retry_at
                mark_capture_error(db, capture.id, OFFSETS_READ_FAILED, retryable=True)
                logger.error("replica offset read failed capture=%s type=%s",
                             capture.id, type(exc).__name__)
                return False
            code = self._offset_problem(entries, capture.id, connection.kind)
            if code is not None:
                self._offset_gap(db, capture, code)
                return False
            item.offsets_verified = True
            return True
        path = item.state_dir / "offsets.dat"
        if not path.is_file():
            mark_capture_error(db, capture.id, OFFSETS_MISSING, retryable=False)
            return False
        item.offset_check = self._offset_reads.submit(self.read_offsets, path)
        return None

    @staticmethod
    def _offset_problem(entries, capture_id: str, kind: str | None) -> str | None:
        """Validate the ReadOffsets array; never treats an empty map as resume."""
        try:
            expected = "oo_replica_" + uuid.UUID(str(capture_id)).hex
        except (ValueError, AttributeError, TypeError):
            return OFFSETS_INVALID
        if not isinstance(entries, list) or not entries:
            return OFFSETS_INVALID
        matched = []
        for entry in entries:
            if not isinstance(entry, dict):
                return OFFSETS_INVALID
            partition = entry.get("partition")
            if not isinstance(partition, dict):
                return OFFSET_PARTITION_CHANGED
            if partition.get("server") != expected:
                return OFFSET_PARTITION_CHANGED
            offset = entry.get("offset")
            if not isinstance(offset, dict) or not offset:
                # Registered partition without a committed offset: resuming
                # here would silently start a new snapshot.
                return OFFSETS_INVALID
            matched.append(offset)
        if len(matched) != 1:
            return OFFSET_PARTITION_CHANGED

        def integer(key):
            value = matched[0].get(key)
            return isinstance(value, int) and not isinstance(value, bool)

        if kind == connector.KIND_POSTGRES:
            if not any(integer(key) for key in ("lsn", "lsn_proc", "lsn_commit")):
                return OFFSETS_INVALID
        elif kind == connector.KIND_MYSQL:
            file = matched[0].get("file")
            if not isinstance(file, str) or not file or not integer("pos"):
                return OFFSETS_INVALID
        return None

    def _offset_gap(self, db, capture, code: str) -> None:
        """Persist an explicit, terminal gap instead of a silent snapshot."""
        mark_capture_error(db, capture.id, code, retryable=False)
        row = db.get(IntegratedReplicaCapture, capture.id, populate_existing=True)
        if row is not None and row.status not in {"stopped", "draining"}:
            row.status = "gap"
        db.flush()

    def _source_ready(self, item, connection, assets, digest, *, resume):
        if item.source_check is not None:
            if not item.source_check.done():
                return item.checked_digest == digest
            checked = item.check_digest
            try:
                item.source_check.result()
            finally:
                item.source_check = None
            if checked != digest:
                item.checked_digest = None
                return False
            item.checked_digest = checked
            item.next_schema_check = self.clock() + SCHEMA_CHECK_SECONDS
        if item.checked_digest != digest or self.clock() >= item.next_schema_check:
            # ORM objects and sessions never cross threads. Copy only the
            # fields consumed by the existing connector control operations.
            source = SimpleNamespace(id=connection.id, kind=connection.kind,
                                     config=copy.deepcopy(connection.config))
            tables = [SimpleNamespace(id=asset.id, schema_name=asset.schema_name,
                table_name=asset.table_name, schema_fingerprint=asset.schema_fingerprint,
                schema_json=copy.deepcopy(asset.schema_json)) for asset in assets]

            def check():
                if resume:
                    self.connector.validate_resume(source, item.capture_id)
                self.connector.preflight(source, tables)

            item.check_digest = digest
            item.source_check = self._source_checks.submit(check)
        return item.checked_digest == digest

    def _check_health(self, item):
        if item.health_read is not None and item.health_read.done():
            try:
                item.health_up = item.health_read.result() is True
            except Exception:
                item.health_up = False
            finally:
                item.health_read = None
        if item.health_read is None and self.clock() >= item.next_health_read:
            item.health_read = self._source_checks.submit(
                self.health_probe, QUARKUS_PORT_BASE + item.index)
            item.next_health_read = self.clock() + 2

    def _read_offsets(self, path):
        """Read persisted Debezium offsets through the shared Java reader."""
        completed = subprocess.run([JAVA_BINARY, "-cp", DEBEZIUM_CLASSPATH,
            "io.openontology.replica.ReadOffsets", str(path)],
            capture_output=True, timeout=10, check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "JAVA_HOME": JAVA_HOME, "LANG": "C.UTF-8", "TZ": "UTC"})
        if completed.returncode in {3, 4, 5}:
            raise OffsetReadError(OFFSETS_INVALID)
        if completed.returncode != 0:
            # Usage/internal invocation problems are retryable, never a gap.
            raise RuntimeError("offset reader failed")
        if len(completed.stdout) > 1024 * 1024:
            raise OffsetReadError(OFFSETS_INVALID)
        try:
            return json.loads(completed.stdout)
        except ValueError:
            raise OffsetReadError(OFFSETS_INVALID) from None

    def _prune_offsets(self, db, item):
        from app.data_channel.integrated import replica_journal

        if item.offset_read is not None and item.offset_read.done():
            try:
                offsets = item.offset_read.result()
                expected = "oo_replica_" + item.capture_id.replace("-", "")
                if isinstance(offsets, list):
                    for entry in offsets:
                        if (isinstance(entry, dict) and entry.get("partition", {}).get("server") == expected
                                and isinstance(entry.get("offset"), dict)):
                            replica_journal.prune_applied(db, item.capture_id,
                                confirmed_offset=entry["offset"])
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError,
                    replica_journal.ReplicaJournalError):
                # Concurrent file replacement or an idle in-transaction offset
                # is not a log gap. Keep replay state until a proven boundary.
                pass
            finally:
                item.offset_read = None
        path = item.state_dir / "offsets.dat"
        if (item.offset_read is None and self.clock() >= item.next_offset_read and path.is_file()):
            item.offset_read = self._offset_reads.submit(self.read_offsets, path)
            item.next_offset_read = self.clock() + 10

    def _start(self, item, capture, connection, property_assets, digest):
        secret = os.urandom(32)
        registration = self.receiver.register(capture.id, capture.generation, secret)
        item.state_dir.mkdir(parents=True, exist_ok=True)
        config_dir = item.run_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        for directory in (item.state_dir, item.run_dir, config_dir):
            os.chmod(directory, 0o700)
        properties = self.connector.build_properties(connection=connection,
            assets=property_assets,
            capture_id=capture.id, secret_b64=base64.b64encode(secret).decode("ascii"),
            sink_url=f"http://127.0.0.1:{self.receiver.port}{registration.path}",
            quarkus_port=QUARKUS_PORT_BASE + item.index, state_dir=str(item.state_dir))
        config_file = config_dir / "application.properties"
        descriptor = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(properties)
        args = [JAVA_BINARY, *CHILD_JAVA_OPTS, "-cp", DEBEZIUM_CLASSPATH, DEBEZIUM_MAIN]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "JAVA_HOME": JAVA_HOME,
               "LANG": "C.UTF-8", "TZ": "UTC"}
        item.started_at = self.clock()
        item.child = self.spawn(args, cwd=str(item.run_dir), env=env)
        item.config_digest = digest
        item.health_up = False
        capture.status, capture.last_error_code = "starting", None
        logger.info("replica child started capture=%s", item.capture_id)

    def _error(self, capture_id, code, *, retryable):
        try:
            with self.sessions() as db:
                mark_capture_error(db, capture_id, code, retryable=retryable)
                if code == cdc_runtime.CODE_SOURCE_LOG_GAP:
                    capture = db.get(IntegratedReplicaCapture, capture_id,
                                     populate_existing=True)
                    if (capture is not None
                            and capture.status not in {"stopped", "draining"}):
                        capture.status = "gap"
                db.commit()
        except Exception:
            logger.error("replica error report unavailable capture=%s", capture_id)

    def _stop(self, item):
        if item.child is not None:
            child, item.child = item.child, None
            child.stop()
        for future in (item.source_check, item.health_read, item.offset_check,
                       item.offset_read, item.probe_future):
            if future is not None:
                future.cancel()
        item.source_check = item.health_read = item.offset_check = item.offset_read = None
        item.probe_future = None
        item.probe_token = None
        item.probe_generation = None
        item.next_probe_at = 0.0
        item.probe_abandoned.clear()
        item.lag_since.clear()
        item.checked_digest = item.check_digest = None
        item.offsets_verified = False
        item.health_up = False
        item.next_schema_check = item.next_health_read = 0.0
        if self.receiver is not None:
            self.receiver.unregister(item.capture_id)
        shutil.rmtree(item.run_dir, ignore_errors=True)

    def _stop_all(self):
        items, self.processes = list(self.processes.values()), {}
        for item in items:
            self._stop(item)

    def shutdown(self):
        # Stop concurrently before joining, to bound shutdown for many sources.
        items, self.processes = list(self.processes.values()), {}
        for item in items:
            if item.child is not None:
                item.child.terminate()
        deadline = time.monotonic() + 25
        for item in items:
            if item.child is not None:
                if not item.child.wait(max(0.0, deadline - time.monotonic())):
                    item.child.kill_group()
                item.child.reap()
                item.child.kill_group()
                item.child = None
            shutil.rmtree(item.run_dir, ignore_errors=True)
        if self.receiver is not None:
            self.receiver.stop()
        self._source_checks.shutdown(wait=True, cancel_futures=True)
        self._offset_reads.shutdown(wait=True, cancel_futures=True)
        self._cleanup_pool.shutdown(wait=True, cancel_futures=True)
        self._coverage_probes.shutdown(wait=True, cancel_futures=True)
        self.cleanups.clear()
        self.cleanup_after.clear()
        if self.lease is not None:
            self.lease.reset()


def main():
    logging.basicConfig(level=os.environ.get("APP_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.model_registry import import_all_models
    import_all_models()
    raise SystemExit(ReplicaSupervisor().run())


if __name__ == "__main__":
    main()
