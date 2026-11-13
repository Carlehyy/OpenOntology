#!/usr/bin/env python3
"""Integrated replica acceptance E2E — real sources, production modules only.

Owned artifacts: this script + ``.artifacts/replica-acceptance/``. The driver
phase runs INSIDE the existing ``openontology-cdc-executor:canary`` image on
the shared spike network ``oo-replica-spike-20261112`` and exercises the
production stack end to end — ``ReplicaSupervisor`` tick loop, production
``replica_receiver``, Debezium Server child (java spawn automatic), replica
ingest/journal, apply/store and the local read lane. No mock receiver and no
mock ingest anywhere; source disconnection is simulated only by killing our
own child process.

Coverage is the requested real-data matrix: exact full-column typing (int2/
int8, bool, float4/float8, numeric(30,10), text/varchar/char, date/time/
timestamp/timestamptz, json(b), bytes, uuid, SQL NULL), an empty table, long
single transactions (3000 rows, update 1000, delete 1000), cross-table and
PK-changing transactions, updates/deletes, TOASTed text/jsonb (unchanged
placeholder fill), controlled source CRUD with row-token CAS/idempotency, a
later-added table that must not restart the existing stream, interrupted
initial candidates (abandon + rebuild), and drain/leak checks. Undrained gates
are reported as honest SKIPPED reasons, never relaxed.

Two source connections keep cohorts independent: cohort A (accounts, events,
empty metrics, wide_types, toast_rows, long_tx, later orders) must never
restart; cohort B (rebuild_target) exercises the interrupted-candidate
rebuild. The target PostgreSQL database ``replica_acceptance`` is created/
dropped by this script only; ``replica_store_probe`` and the shared source
containers are never stopped.

Host usage::

    python3 scripts/e2e/integrated_replica_acceptance.py --source pg
    python3 scripts/e2e/integrated_replica_acceptance.py --source mysql
    python3 scripts/e2e/integrated_replica_acceptance.py --source pg --keep-fixtures

Driver mode is invoked by the host inside the container; do not call directly.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import threading
import time
import traceback
from datetime import date, datetime, time as dt_time, timezone
from decimal import Decimal
from pathlib import Path

# Host mode only (the driver copy lives at /tmp/acceptance.py in-container).
_SCRIPT = Path(__file__).resolve()
ROOT = _SCRIPT.parents[2] if len(_SCRIPT.parents) > 2 else Path("/work")
DEFAULT_PROJECT = "oo-replica-spike-20261112"
ARTIFACTS = ROOT / ".artifacts" / "replica-acceptance"
JAR = ROOT / ".artifacts" / "replica-java" / "replica-smt.jar"

NETWORK = DEFAULT_PROJECT
IMAGE = "openontology-cdc-executor:canary"
DRIVER_MEMORY = "3g"
DRIVER_CPUS = "2"
CONTAINER = "oo-replica-acceptance-driver"
SRC_PG_CONTAINER = f"{DEFAULT_PROJECT}-source-pg-1"
SRC_MYSQL_CONTAINER = f"{DEFAULT_PROJECT}-source-mysql-1"
TARGET_PG_CONTAINER = f"{DEFAULT_PROJECT}-target-pg-1"
TARGET_DB = "replica_acceptance"
FIXTURE_SECRET = "replica_fixture"  # compose fixture credential (user==pw)
SRC_SCHEMA = "acceptance"

DRIVER_TIMEOUT_SECONDS = 40 * 60
MARKER = "@@RESULT@@"


# ── Host helpers ─────────────────────────────────────────────────────────────

def sh(args, *, timeout=120, check=True, stdin=None):
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, input=stdin)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args[:5])}\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}")
    return result


def psql(container: str, sql: str, *, database: str, timeout=180) -> str:
    return sh(["docker", "exec", container, "psql", "-U", FIXTURE_SECRET,
               "-d", database, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql],
              timeout=timeout).stdout


def preflight(source: str) -> None:
    if JAR is not None and not JAR.is_file():
        raise RuntimeError(f"replica SMT jar missing: {JAR}")
    sh(["docker", "inspect", IMAGE])
    sh(["docker", "network", "inspect", NETWORK])
    if source == "pg":
        sh(["docker", "exec", SRC_PG_CONTAINER, "pg_isready",
            "-U", FIXTURE_SECRET, "-d", "replica_source"])
    else:
        sh(["docker", "exec", SRC_MYSQL_CONTAINER, "mysqladmin", "ping",
            "-h", "127.0.0.1", "-uroot", f"-p{FIXTURE_SECRET}", "--silent"])
    sh(["docker", "exec", TARGET_PG_CONTAINER, "pg_isready",
        "-U", FIXTURE_SECRET, "-d", "replica_target"])


def reset_target_database() -> None:
    psql(TARGET_PG_CONTAINER, f"DROP DATABASE IF EXISTS {TARGET_DB} WITH (FORCE);",
         database="replica_target")
    psql(TARGET_PG_CONTAINER, f"CREATE DATABASE {TARGET_DB} OWNER {FIXTURE_SECRET};",
         database="replica_target")


# Deterministic fixture DDL: exact decimals, json (incl. json null), binary,
# extreme bigints, full logical-type coverage, SQL NULLs, an empty table,
# TOAST-sized text/jsonb and a long-transaction table; multitable cohort A.
PG_DDL = """
DROP SCHEMA IF EXISTS acceptance CASCADE;
CREATE SCHEMA acceptance;
CREATE TABLE acceptance.accounts (
  id integer PRIMARY KEY, name text NOT NULL,
  price numeric(12,6) NOT NULL, total numeric(20,4) NOT NULL,
  meta jsonb NOT NULL, payload bytea NOT NULL, ref bigint NOT NULL);
CREATE TABLE acceptance.events (id integer PRIMARY KEY, note text NOT NULL);
CREATE TABLE acceptance.metrics_empty (id integer PRIMARY KEY, val text NOT NULL);
CREATE TABLE acceptance.rebuild_target (id integer PRIMARY KEY, val text NOT NULL);
CREATE TABLE acceptance.wide_types (
  id integer PRIMARY KEY,
  i2 smallint NOT NULL, i8 bigint NOT NULL, flag boolean NOT NULL,
  amount numeric(30,10) NOT NULL,
  txt text, vc varchar(80), ch char(8) NOT NULL,
  d date NOT NULL, tm time(6) NOT NULL, dt timestamp(6) NOT NULL,
  instant timestamptz(6) NOT NULL, js jsonb, bin_value bytea, tag uuid NOT NULL,
  opt text);
CREATE TABLE acceptance.toast_rows (
  id integer PRIMARY KEY, big_text text NOT NULL, big_json jsonb NOT NULL,
  note text NOT NULL);
CREATE TABLE acceptance.long_tx (
  id integer PRIMARY KEY, n integer NOT NULL, pad text NOT NULL);
CREATE TABLE acceptance.float_types (
  id integer PRIMARY KEY, f4 real NOT NULL, f8 double precision NOT NULL);
INSERT INTO acceptance.accounts VALUES
 (1,'alpha',3.141593,9999999999999999.9999,'{"k":[1,2],"s":"x"}',decode('0001ff5a','hex'),9223372036854775807),
 (2,'beta',0.000001,-0.0001,'{"nested":{"a":true}}',decode('ff00','hex'),-9223372036854775808),
 (3,'gamma',12345.678900,0.0000,'{"z":null}',decode('00','hex'),0);
INSERT INTO acceptance.events VALUES (1,'e-1'),(2,'e-2'),(3,'e-3');
INSERT INTO acceptance.rebuild_target VALUES (1,'rt-1'),(2,'rt-2');
INSERT INTO acceptance.wide_types VALUES
 (1,-32768,9223372036854775807,true,12345678901234567890.0123456789,
  'plain text','varchar-value','char0001','2024-02-29','23:59:59.123456',
  '2024-02-29 12:34:56.123456','2024-02-29 12:34:56.123456+05:30',
  '{"k":[1,2],"s":"x"}',decode('0001ff','hex'),
  '0f9a4c3b-8d2e-4f6a-9b1c-2d3e4f5a6b7c',NULL),
 (2,32767,-9223372036854775808,false,-0.0000000001,
  '中文文本','v2','char0002','1000-01-01','00:00:00','1000-01-01 00:00:00',
  '1000-01-01 00:00:00+00','{"n":1234567890123456789}',decode('deadbeef','hex'),
  '9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a','present'),
 (3,0,0,true,99999999999999999999.9999999999,
  NULL,NULL,'char0003','2038-01-19','12:00:00.000001',
  '2038-01-19 03:14:07.999999','2038-01-19 03:14:07.999999+05:30',NULL,NULL,
  '00000000-0000-4000-8000-000000000000',NULL);
INSERT INTO acceptance.float_types VALUES
 (1,1.5,-2.25),(2,-0.5,0.125),
 (4,1.234567,1.2345678901234567);
INSERT INTO acceptance.toast_rows VALUES
 (1,repeat('A',199990)||'-seed-tail-1',
  jsonb_build_object('blob',repeat('B',200000),'n',1),'seed'),
 (2,repeat('C',100000)||'-seed-tail-2','{"small":true}'::jsonb,'seed2');
ALTER TABLE acceptance.accounts REPLICA IDENTITY FULL;
ALTER TABLE acceptance.events REPLICA IDENTITY FULL;
ALTER TABLE acceptance.metrics_empty REPLICA IDENTITY FULL;
ALTER TABLE acceptance.rebuild_target REPLICA IDENTITY FULL;
ALTER TABLE acceptance.wide_types REPLICA IDENTITY FULL;
ALTER TABLE acceptance.toast_rows REPLICA IDENTITY FULL;
ALTER TABLE acceptance.long_tx REPLICA IDENTITY FULL;
ALTER TABLE acceptance.float_types REPLICA IDENTITY FULL;
"""

MYSQL_DDL = """
DROP DATABASE IF EXISTS acceptance;
CREATE DATABASE acceptance CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
USE acceptance;
CREATE TABLE accounts (
  id INT PRIMARY KEY, name VARCHAR(100) NOT NULL,
  price DECIMAL(12,6) NOT NULL, total DECIMAL(20,4) NOT NULL,
  meta JSON NOT NULL, payload BLOB NOT NULL, ref BIGINT NOT NULL);
CREATE TABLE events (id INT PRIMARY KEY, note VARCHAR(100) NOT NULL);
CREATE TABLE metrics_empty (id INT PRIMARY KEY, val VARCHAR(100) NOT NULL);
CREATE TABLE rebuild_target (id INT PRIMARY KEY, val VARCHAR(100) NOT NULL);
CREATE TABLE wide_types (
  id INT PRIMARY KEY, i2 SMALLINT NOT NULL, i8 BIGINT NOT NULL,
  flag TINYINT(1) NOT NULL, amount DECIMAL(30,10) NOT NULL,
  txt TEXT, vc VARCHAR(80), ch CHAR(8) NOT NULL, d DATE NOT NULL,
  tm TIME(6) NOT NULL, dt DATETIME(6) NOT NULL, js JSON, bin_value BLOB,
  tag CHAR(36) NOT NULL, opt VARCHAR(80));
CREATE TABLE toast_rows (
  id INT PRIMARY KEY, big_text LONGTEXT NOT NULL, big_json JSON NOT NULL,
  note VARCHAR(80) NOT NULL);
CREATE TABLE long_tx (
  id INT PRIMARY KEY, n INT NOT NULL, pad TEXT NOT NULL);
CREATE TABLE float_types (
  id INT PRIMARY KEY, f4 FLOAT NOT NULL, f8 DOUBLE NOT NULL,
  u8 BIGINT UNSIGNED NOT NULL);
INSERT INTO accounts VALUES
 (1,'alpha',3.141593,9999999999999999.9999,'{"k":[1,2],"s":"x"}',UNHEX('0001ff5a'),9223372036854775807),
 (2,'beta',0.000001,-0.0001,'{"nested":{"a":true}}',UNHEX('ff00'),-9223372036854775808),
 (3,'gamma',12345.678900,0.0000,'{"z":null}',UNHEX('00'),0);
INSERT INTO events VALUES (1,'e-1'),(2,'e-2'),(3,'e-3');
INSERT INTO rebuild_target VALUES (1,'rt-1'),(2,'rt-2');
INSERT INTO wide_types VALUES
 (1,-32768,9223372036854775807,1,12345678901234567890.0123456789,
  'plain text','varchar-value','char0001','2024-02-29','23:59:59.123456',
  '2024-02-29 12:34:56.123456','{"k": [1, 2], "s": "x"}',UNHEX('0001ff'),
  '0f9a4c3b-8d2e-4f6a-9b1c-2d3e4f5a6b7c',NULL),
 (2,32767,-9223372036854775808,0,-0.0000000001,
  '中文文本','v2','char0002','1000-01-01','00:00:00','1000-01-01 00:00:00',
  '{"n": 1234567890123456789}',UNHEX('deadbeef'),
  '9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a','present'),
 (3,0,0,1,99999999999999999999.9999999999,
  NULL,NULL,'char0003','2038-01-19','12:00:00.000001',
  '2038-01-19 03:14:07.999999',NULL,NULL,
  '00000000-0000-4000-8000-000000000000',NULL);
INSERT INTO float_types VALUES
 (1,1.5,-2.25,18446744073709551615),
 (2,-0.5,0.125,9223372036854775808),
 (3,0,0,0),
 (4,1.234567,1.2345678901234567,1);
INSERT INTO toast_rows VALUES
 (1,CONCAT(REPEAT('A',199990),'-seed-tail-1'),
  JSON_OBJECT('blob',REPEAT('B',200000),'n',1),'seed'),
 (2,CONCAT(REPEAT('C',100000),'-seed-tail-2'),'{"small": true}','seed2');
"""


def reset_source(source: str) -> None:
    if source == "pg":
        sh(["docker", "exec", "-i", SRC_PG_CONTAINER, "psql", "-U",
            FIXTURE_SECRET, "-d", "replica_source", "-v", "ON_ERROR_STOP=1",
            "-q"], stdin=PG_DDL)
    else:
        # The fixture image's client defaults to latin1 in a C locale; force
        # utf8mb4 so non-ASCII fixture values are not double-encoded.
        sh(["docker", "exec", "-i", SRC_MYSQL_CONTAINER, "mysql", "-uroot",
            f"-p{FIXTURE_SECRET}", "--binary-mode",
            "--default-character-set=utf8mb4"], stdin=MYSQL_DDL)


def owned_capture_ids() -> list[str]:
    exists = sh(["docker", "exec", TARGET_PG_CONTAINER, "psql", "-U",
                 FIXTURE_SECRET, "-d", "replica_target", "-tAc",
                 f"SELECT 1 FROM pg_database WHERE datname='{TARGET_DB}'"],
                check=False)
    if exists.returncode != 0 or "1" not in exists.stdout:
        return []
    out = sh(["docker", "exec", TARGET_PG_CONTAINER, "psql", "-U",
              FIXTURE_SECRET, "-d", TARGET_DB, "-tAc",
              "SELECT id FROM v2_integrated_replica_captures"], check=False)
    if out.returncode != 0:
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def cleanup_owned(source: str) -> None:
    """Stop only what this run created: ir_* slots/publications of my own
    captures, the fixture schema/database and the driver container. The
    shared source/target containers themselves are never stopped."""
    sh(["docker", "rm", "-f", CONTAINER], check=False, timeout=60)
    if source == "pg":
        for capture_id in owned_capture_ids():
            hex_id = capture_id.replace("-", "")
            sh(["docker", "exec", SRC_PG_CONTAINER, "psql", "-U",
                FIXTURE_SECRET, "-d", "replica_source", "-q", "-c",
                f"SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots"
                f" WHERE slot_name='ir_{hex_id}' AND active;"
                f"SELECT pg_drop_replication_slot('ir_{hex_id}') WHERE EXISTS"
                f" (SELECT 1 FROM pg_replication_slots WHERE slot_name='ir_{hex_id}');"],
               check=False, timeout=60)
            sh(["docker", "exec", SRC_PG_CONTAINER, "psql", "-U",
                FIXTURE_SECRET, "-d", "replica_source", "-q", "-c",
                f'DROP PUBLICATION IF EXISTS "irpub_{hex_id}"'],
               check=False, timeout=60)
        sh(["docker", "exec", SRC_PG_CONTAINER, "psql", "-U", FIXTURE_SECRET,
            "-d", "replica_source", "-q", "-c",
            "DROP SCHEMA IF EXISTS acceptance CASCADE"], check=False)
    else:
        sh(["docker", "exec", SRC_MYSQL_CONTAINER, "mysql", "-uroot",
            f"-p{FIXTURE_SECRET}", "-e", "DROP DATABASE IF EXISTS acceptance"],
           check=False)


def source_url(source: str) -> str:
    if source == "pg":
        return ("postgresql+psycopg2://replica_fixture:replica_fixture@"
                "source-pg:5432/replica_source")
    return "mysql+pymysql://root:replica_fixture@source-mysql:3306/acceptance"


SOURCE_DISCONNECT = "@@SOURCE_DISCONNECT@@"
SOURCE_RECONNECT = "@@SOURCE_RECONNECT@@"


def fixture_source(source: str) -> tuple[str, str]:
    if source == "pg":
        return SRC_PG_CONTAINER, "source-pg"
    return SRC_MYSQL_CONTAINER, "source-mysql"


def disconnect_fixture_source(source: str) -> None:
    container, _ = fixture_source(source)
    result = subprocess.run(
        ["docker", "network", "disconnect", NETWORK, container],
        capture_output=True, text=True)
    if result.returncode and "is not connected" not in result.stderr:
        raise RuntimeError(f"source disconnect failed: {result.stderr[-500:]}")


def reconnect_fixture_source(source: str) -> None:
    container, alias = fixture_source(source)
    result = subprocess.run(
        ["docker", "network", "connect", "--alias", alias, NETWORK, container],
        capture_output=True, text=True)
    if result.returncode and "already exists" not in result.stderr:
        raise RuntimeError(f"source reconnect failed: {result.stderr[-500:]}")


def run_driver(source: str, log_path: Path, args) -> dict:
    jar_mount = (["-v", f"{JAR}:/opt/debezium/lib/replica-smt.jar:ro"]
                 if JAR is not None else [])
    command = [
        "docker", "run", "--rm", "--name", CONTAINER,
        "--network", NETWORK, "--no-healthcheck",
        "--memory", DRIVER_MEMORY, "--cpus", DRIVER_CPUS,
        "-v", f"{ROOT / 'backend'}:/work:ro",
        "-v", f"{Path(__file__).resolve()}:/tmp/acceptance.py:ro",
        *jar_mount,
        "-e", "PYTHONPATH=/work",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", "ENVIRONMENT=test",
        "-e", "DATABASE_URL=postgresql+psycopg2://replica_fixture:"
              f"replica_fixture@target-pg:5432/{TARGET_DB}",
        "-e", "REPLICA_STATE_ROOT=/tmp/ra/state",
        "-e", "REPLICA_RUN_ROOT=/tmp/ra/run",
        "-e", "REPLICA_HEARTBEAT_FILE=/tmp/ra/heartbeat",
        "-e", "CDC_CHILD_JAVA_OPTS=-Xms64m -Xmx256m "
              "-XX:MaxDirectMemorySize=64m -XX:ActiveProcessorCount=2 "
              "-Djava.awt.headless=true",
        "-w", "/tmp",
        "--entrypoint", "python3", IMAGE,
        "/tmp/acceptance.py", "--mode", "driver", "--source", source,
        "--row-count", str(args.row_count),
        "--latency-samples", str(args.latency_samples),
        "--source-count", str(args.source_count),
    ]
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        watchdog = threading.Timer(
            args.timeout,
            lambda: (log.write("[host] watchdog: killing driver\n"),
                     process.kill()))
        watchdog.daemon = True
        watchdog.start()
        result = None
        disconnected = False
        try:
            for line in process.stdout:
                log.write(line)
                log.flush()
                line = line.strip()
                if line.startswith(MARKER):
                    result = json.loads(line[len(MARKER):])
                elif line == SOURCE_DISCONNECT:
                    disconnect_fixture_source(source)
                    disconnected = True
                    log.write("[host] source network disconnected\n")
                elif line == SOURCE_RECONNECT:
                    reconnect_fixture_source(source)
                    disconnected = False
                    log.write("[host] source network reconnected\n")
            process.wait()
        finally:
            watchdog.cancel()
            if disconnected:
                # Never leave a shared fixture source offline, even if the
                # driver crashed between the two markers.
                try:
                    reconnect_fixture_source(source)
                except Exception as exc:  # noqa: BLE001
                    log.write(f"[host] reconnect trap failed: {exc}\n")
            log.write(f"\n[host] driver exit code: {process.returncode}\n")
    if result is None:
        raise RuntimeError(f"driver produced no {MARKER} result; see {log_path}")
    result["driver_exit_code"] = process.returncode
    return result


def host_main(args) -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    log_path = ARTIFACTS / f"{args.source}-driver.log"
    result_path = ARTIFACTS / f"{args.source}-result.json"
    print(f"[host] source={args.source} artifacts={ARTIFACTS}")
    preflight(args.source)
    cleanup_owned(args.source)
    reset_target_database()
    reset_source(args.source)
    print(f"[host] fixtures reset; launching driver container {CONTAINER}")
    result = {"ok": False, "error": "driver did not run"}
    try:
        result = run_driver(args.source, log_path, args)
        print(f"[host] gates: {json.dumps(result.get('gates', {}), indent=2)}")
        if result.get("observed"):
            print(f"[host] observed: {json.dumps(result['observed'], indent=2)}")
        if result.get("failures"):
            print(f"[host] failures: {json.dumps(result['failures'], indent=2)}")
    except Exception as exc:
        result = {"ok": False, "error": f"{exc}", "traceback":
                  traceback.format_exc()}
    finally:
        if not args.keep_fixtures:
            cleanup_owned(args.source)
    result_path.write_text(json.dumps(result, indent=2, default=str),
                           encoding="utf-8")
    ok = bool(result.get("ok")) and result.get("driver_exit_code") == 0
    print(f"[host] log={log_path} result={result_path} ok={ok}")
    return 0 if ok else 1


# ── Driver (inside the executor container; production modules only) ─────────

def driver_main(args) -> int:
    import logging
    from types import SimpleNamespace

    import sqlalchemy as sa
    from fastapi import HTTPException

    logging.basicConfig(level="INFO",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.model_registry import import_all_models
    from app.shared.database import Base, SessionLocal, engine

    import_all_models()
    Base.metadata.create_all(bind=engine)

    from app.auth.models import User
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.integrated import native, service
    from app.data_channel.integrated.contracts import RowMutation
    from app.data_channel.integrated import replica_control as control
    from app.data_channel.integrated import replica_store as store
    from app.data_channel.integrated.models import IntegratedAsset
    from app.data_channel.integrated.replica_capture_models import (
        IntegratedReplicaCapture,
    )
    from app.data_channel.integrated.replica_executor import ReplicaSupervisor
    from app.data_channel.integrated.replica_models import (
        IntegratedReplica, IntegratedReplicaRun,
    )
    from app.data_channel.integrated.replica_receiver import (
        create_replica_receiver,
    )
    from app.data_channel.query import providers
    from app.data_channel.query.contracts import Binding, QueryLimits

    source, is_pg = args.source, args.source == "pg"
    src = sa.create_engine(
        source_url(source), poolclass=sa.pool.NullPool,
        connect_args={"connect_timeout": 2} if is_pg
        else {"connect_timeout": 2, "read_timeout": 5})
    gates: dict[str, bool] = {}
    evidence: dict = {}
    observed: dict = {}
    failures: list[str] = []

    def record(gate, ok, detail=None):
        gates[gate] = bool(ok)
        if detail is not None:
            evidence.setdefault(gate, []).append(detail)
        # Live gate marker: the host log shows the actual matrix as it runs
        # instead of waiting for the final @@RESULT@@ line.
        print(f"@@GATE@@ {gate}={bool(ok)}", flush=True)

    def fail(gate, exc):
        gates[gate] = False
        failures.append(f"{gate}: {exc}")
        evidence[gate] = traceback.format_exc()
        print(f"@@GATE@@ {gate}=False error={type(exc).__name__}: "
              f"{str(exc).splitlines()[0][:300]}", flush=True)

    def source_exec(statements, *, transaction=False):
        if transaction:
            with src.begin() as conn:
                for statement in statements:
                    conn.execute(sa.text(statement))
        else:
            with src.connect() as conn:
                for statement in statements:
                    conn.execute(sa.text(statement))
                conn.commit()

    def fresh_supervisor():
        return ReplicaSupervisor(
            session_factory=SessionLocal,
            receiver=create_replica_receiver(session_factory=SessionLocal),
            state_root="/tmp/ra/state", run_root="/tmp/ra/run",
            heartbeat_file="/tmp/ra/heartbeat", max_captures=4)

    supervisor = fresh_supervisor()

    def pump(probe, *, deadline, what, interval=1.0):
        """Drive the production supervisor tick until probe(db) is truthy."""
        import logging as _logging

        end = time.monotonic() + deadline
        last = None
        while time.monotonic() < end:
            try:
                supervisor.tick()
            except Exception as exc:  # noqa: BLE001 - tick 自身故障要可见但可重试
                _logging.getLogger("acceptance").warning(
                    "tick raised type=%s", type(exc).__name__)
            with SessionLocal() as db:
                try:
                    last = probe(db)
                except Exception as exc:  # noqa: BLE001
                    last = None
                    _logging.getLogger("acceptance").warning(
                        "probe raised type=%s", type(exc).__name__)
            if last:
                return last
            time.sleep(interval)
        raise AssertionError(f"timeout waiting for {what}; last={last!r}")

    # ── deterministic fixtures: user / connections / assets via native.reflect

    def register_asset(db, connection_id, table):
        with src.connect() as conn:
            _, schema, fingerprint = native.reflect(
                conn, SimpleNamespace(schema_name=SRC_SCHEMA, table_name=table))
        dataset = Dataset(
            id=f"ds-{table}", name=f"acceptance {table}", kind="integrated",
            source_connection_id=connection_id,
            source_resource=f"{SRC_SCHEMA}.{table}", schema_json=schema)
        db.add(dataset)
        db.flush()
        asset = IntegratedAsset(
            id=f"asset-{table}", dataset_id=dataset.id,
            connection_id=connection_id, name=f"acceptance {table}",
            schema_name=SRC_SCHEMA, table_name=table, schema_json=schema,
            schema_fingerprint=fingerprint, created_by=user_id,
            writable=True)
        db.add(asset)
        db.flush()
        return asset

    from app.services.auth_service import hash_password

    with SessionLocal() as db:
        db.add(User(id="acceptance-admin", username="acceptance_admin",
                    email="acceptance@example.test",
                    password_hash=hash_password("acceptance-only"),
                    role="admin", is_active=True))
        db.flush()
        user_id = "acceptance-admin"
        kind = "postgres" if is_pg else "mysql"
        common = {"user": FIXTURE_SECRET if is_pg else "root",
                  "password": FIXTURE_SECRET,
                  "host": "source-pg" if is_pg else "source-mysql",
                  "port": 5432 if is_pg else 3306,
                  "database": "replica_source" if is_pg else SRC_SCHEMA}
        for connection_id in ("acc-src-a", "acc-src-b", "acc-src-c"):
            db.add(Connection(id=connection_id,
                              name=f"acceptance cohort {connection_id[-1]}",
                              kind=kind, config=dict(common), status="active",
                              created_by=user_id))
        db.flush()
        for table in ("accounts", "events", "metrics_empty", "wide_types",
                      "toast_rows", "long_tx"):
            register_asset(db, "acc-src-a", table)
        register_asset(db, "acc-src-b", "rebuild_target")
        register_asset(db, "acc-src-c", "float_types")
        db.commit()
    record("fixtures_registered", True, {"tables": [
        "accounts", "events", "metrics_empty", "wide_types", "toast_rows",
        "long_tx", "rebuild_target", "float_types"]})

    assets = {name: f"asset-{name}" for name in
              ("accounts", "events", "metrics_empty", "wide_types",
               "toast_rows", "long_tx", "rebuild_target", "float_types")}
    cohort_a_assets = [assets["accounts"], assets["events"],
                       assets["metrics_empty"], assets["wide_types"],
                       assets["toast_rows"], assets["long_tx"]]

    def replica_states(db):
        rows = db.execute(sa.select(
            IntegratedReplica.asset_id, IntegratedReplica.status,
            IntegratedReplica.pending_run_id, IntegratedReplica.active_run_id)
        ).all()
        return {row[0]: {"status": row[1], "pending": row[2], "active": row[3]}
                for row in rows}

    def all_ready(db, asset_ids):
        states = replica_states(db)
        ready = [states.get(a, {}).get("status") == "ready"
                 and states.get(a, {}).get("pending") is None
                 for a in asset_ids]
        return all(ready) or None

    def relation_rows(db, asset_id, columns):
        from contextlib import ExitStack

        with ExitStack() as stack:
            relation = providers.resolve_integrated(
                db, db.get(User, user_id),
                [("src", Binding(kind="integrated", asset_id=asset_id))],
                QueryLimits(), stack)["src"]
            selected = [relation.expressions[name] for name in columns]
            order = relation.expressions["id"]
            return [dict(row) for row in db.execute(
                sa.select(*selected).order_by(order)).mappings()]

    def candidate_rows(db, asset_id):
        """Typed rows of the newest non-terminal run's local candidate table.

        Reads through the production store helpers (build_replica_schema +
        read_batches) so verification works while the runtime correctly holds
        publication at catching_up behind the verified-catchup barrier.
        """
        run = db.scalar(sa.select(IntegratedReplicaRun).where(
            IntegratedReplicaRun.asset_id == asset_id,
            IntegratedReplicaRun.status.in_(["catching_up", "active"]))
            .order_by(IntegratedReplicaRun.created_at.desc()).limit(1))
        if run is None:
            return None, None
        schema = store.build_replica_schema(
            run.id, run.schema_json, schema_fingerprint=run.schema_fingerprint)
        rows = []
        with engine.connect() as conn:
            for batch in store.read_batches(conn, schema, batch_size=100):
                rows.extend(batch)
        return run.status, rows

    def capture_rows(db, connection_id=None):
        statement = sa.select(IntegratedReplicaCapture)
        if connection_id:
            statement = statement.where(
                IntegratedReplicaCapture.connection_id == connection_id)
        return [{"id": row.id, "connection": row.connection_id,
                 "status": row.status, "generation": row.generation,
                 "committed_seq": row.committed_seq}
                for row in db.scalars(statement)]

    # ── Gate 1: initial snapshot progress + baseline exactness ───────────
    # Per current runtime guidance: verify stream/run progress (stop at
    # catching_up) and typed baseline values; a first "ready" is recorded as
    # an observation, never treated as final production pass.
    try:
        started = time.monotonic()

        def stream_progress(db):
            # Heartbeat liveness is not coverage. Wait for the production
            # publication state (active run behind the verified cut), not for
            # capture.status == "streaming", which is a health-liveness label.
            captures = [row for row in capture_rows(db)
                        if row["connection"] in {"acc-src-a", "acc-src-b"}]
            if len(captures) < 2:
                return None
            if any(row["status"] in {"blocked", "gap"} for row in captures):
                raise AssertionError(f"capture terminal state: {captures}")
            runs = db.execute(sa.select(
                IntegratedReplicaRun.asset_id,
                IntegratedReplicaRun.status)).all()
            past_loading = {row[0] for row in runs
                            if row[1] in {"catching_up", "active"}}
            if not past_loading:
                return None
            states = replica_states(db)
            published = sorted(
                asset_id for asset_id, state in states.items()
                if state["active"] and state["status"] in {"ready",
                                                             "catching_up"})
            if not published:
                return None
            return {"captures": captures,
                    "runs_past_loading": sorted(past_loading),
                    "published": published}

        progress = pump(stream_progress, deadline=300,
                        what="initial stream progress")
        record("initial_stream_progress", True,
               {"seconds": round(time.monotonic() - started, 1), **progress})

        with SessionLocal() as db:
            observed["replica_states"] = replica_states(db)
            # Publish-without-proof red flag: active runs whose capture has
            # not committed a single transaction.
            runs = db.execute(sa.select(
                IntegratedReplicaRun.asset_id,
                IntegratedReplicaRun.capture_id,
                IntegratedReplicaRun.status).where(
                IntegratedReplicaRun.status == "active")).all()
            committed = {row["id"]: row["committed_seq"]
                         for row in capture_rows(db)}
            observed["active_runs_without_committed_tx"] = [
                {"asset": row[0], "capture_committed_seq":
                 committed.get(row[1])} for row in runs
                if not committed.get(row[1])]

        def wide_expected():
            """Exact full-column expectation for the wide_types fixture."""
            rows = [
                {"id": 1, "i2": -32768, "i8": 9223372036854775807,
                 "flag": True if is_pg else 1,
                 "amount": Decimal("12345678901234567890.0123456789"),
                 "txt": "plain text", "vc": "varchar-value",
                 "ch": "char0001", "d": date(2024, 2, 29),
                 "tm": dt_time(23, 59, 59, 123456),
                 "dt": datetime(2024, 2, 29, 12, 34, 56, 123456),
                 "js": {"k": [1, 2], "s": "x"},
                 "bin_value": b"\x00\x01\xff",
                 "tag": "0f9a4c3b-8d2e-4f6a-9b1c-2d3e4f5a6b7c", "opt": None},
                {"id": 2, "i2": 32767, "i8": -9223372036854775808,
                 "flag": False if is_pg else 0,
                 "amount": Decimal("-0.0000000001"),
                 "txt": "中文文本", "vc": "v2", "ch": "char0002",
                 "d": date(1000, 1, 1), "tm": dt_time(0, 0, 0),
                 "dt": datetime(1000, 1, 1, 0, 0, 0),
                 "js": {"n": 1234567890123456789},
                 "bin_value": b"\xde\xad\xbe\xef",
                 "tag": "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a",
                 "opt": "present"},
                {"id": 3, "i2": 0, "i8": 0, "flag": True if is_pg else 1,
                 "amount": Decimal("99999999999999999999.9999999999"),
                 "txt": None, "vc": None, "ch": "char0003",
                 "d": date(2038, 1, 19), "tm": dt_time(12, 0, 0, 1),
                 "dt": datetime(2038, 1, 19, 3, 14, 7, 999999),
                 "js": None, "bin_value": None,
                 "tag": "00000000-0000-4000-8000-000000000000", "opt": None},
            ]
            if is_pg:
                rows[0]["instant"] = datetime(
                    2024, 2, 29, 7, 4, 56, 123456, tzinfo=timezone.utc)
                rows[1]["instant"] = datetime(
                    1000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
                rows[2]["instant"] = datetime(
                    2038, 1, 18, 21, 44, 7, 999999, tzinfo=timezone.utc)
            return {row["id"]: row for row in rows}

        def parsed_json(value):
            if value is None:
                return None
            raw = value if isinstance(value, str) else json.dumps(value)
            return json.loads(str(raw), parse_float=Decimal)

        def wide_matches(rows, expected=None):
            expected = expected or wide_expected()
            if {row.get("id") for row in rows} != set(expected):
                return False
            for row in rows:
                want = expected[row["id"]]
                for name, value in want.items():
                    got = row.get(name)
                    if name == "js":
                        if parsed_json(got) != value:
                            return False
                    elif name == "flag":
                        if is_pg:
                            if not isinstance(got, bool) or got != value:
                                return False
                        elif (not isinstance(got, int)
                              or isinstance(got, bool) or got != value):
                            return False
                    elif name == "bin_value":
                        if value is None:
                            if got is not None:
                                return False
                        elif bytes(got or b"") != value:
                            return False
                    elif name == "amount":
                        if got is None or Decimal(got) != value:
                            return False
                    elif got != value:
                        return False
            return True

        def float_expected():
            rows = [
                {"id": 1, "f4": 1.5, "f8": -2.25},
                {"id": 2, "f4": -0.5, "f8": 0.125},
                {"id": 4, "f4": 1.234567,
                 "f8": 1.2345678901234567},
            ]
            if not is_pg:
                rows[0]["u8"] = 18446744073709551615
                rows[1]["u8"] = 9223372036854775808
                rows.append({"id": 3, "f4": 0.0, "f8": 0.0, "u8": 0})
                rows.append({"id": 4, "f4": 1.234567,
                             "f8": 1.2345678901234567, "u8": 1})
            return {row["id"]: row for row in rows}

        def float_matches(rows):
            expected = float_expected()
            if {row.get("id") for row in rows} != set(expected):
                return False
            return all(all(row.get(name) == value
                           for name, value in expected[row["id"]].items())
                       for row in rows)

        def toast_expected():
            return {
                1: {"big_text": "A" * 199990 + "-seed-tail-1",
                    "big_json": {"blob": "B" * 200000, "n": 1},
                    "note": "seed"},
                2: {"big_text": "C" * 100000 + "-seed-tail-2",
                    "big_json": {"small": True}, "note": "seed2"},
            }

        def toast_matches(rows, expected=None):
            expected = expected or toast_expected()
            if {row.get("id") for row in rows} != set(expected):
                return False
            for row in rows:
                want = expected[row["id"]]
                if row.get("big_text") != want["big_text"]:
                    return False
                if parsed_json(row.get("big_json")) != want["big_json"]:
                    return False
                if row.get("note") != want["note"]:
                    return False
            return True

        def candidate_exact(name, status, rows):
            """Typed baseline comparison on the candidate table."""
            if name == "accounts":
                by_id = {row["id"]: row for row in rows}
                if set(by_id) != {1, 2, 3}:
                    return False
                for row_id, price, total, meta, payload, ref in (
                        (1, "3.141593", "9999999999999999.9999",
                         {"k": [1, 2], "s": "x"}, b"\x00\x01\xffZ",
                         9223372036854775807),
                        (2, "0.000001", "-0.0001", {"nested": {"a": True}},
                         b"\xff\x00", -9223372036854775808),
                        (3, "12345.678900", "0.0000", {"z": None}, b"\x00",
                         0)):
                    row = by_id[row_id]
                    if str(row.get("price")) != price:
                        return False
                    if str(row.get("total")) != total:
                        return False
                    raw_meta = row.get("meta")
                    parsed = json.loads(str(raw_meta)) if raw_meta is not None else None
                    if parsed != meta:
                        return False
                    if bytes(row.get("payload") or b"") != payload:
                        return False
                    if row.get("ref") != ref:
                        return False
                return True
            if name == "events":
                return [row["note"] for row in rows] == ["e-1", "e-2", "e-3"]
            if name == "metrics_empty":
                return rows == []
            if name == "wide_types":
                return wide_matches(rows)
            if name == "toast_rows":
                return toast_matches(rows)
            if name == "float_types":
                return float_matches(rows)
            if name == "long_tx":
                return rows == []
            if name == "rebuild_target":
                return [row["val"] for row in rows] == ["rt-1", "rt-2"]
            if name == "orders":
                return [row["item"] for row in rows] == ["widget", "gizmo"]
            return None

        def wait_candidates(expected, *, deadline, what):
            def candidates(db):
                detail = {}
                for name in expected:
                    status, rows = candidate_rows(db, assets[name])
                    detail[name] = {"status": status, "rows": len(rows or [])}
                    if status is None or not candidate_exact(
                            name, status, rows or []):
                        return None
                return detail
            return pump(candidates, deadline=deadline, what=what)

        verified = wait_candidates(
            ["accounts", "events", "metrics_empty", "wide_types",
             "toast_rows", "long_tx", "rebuild_target"],
            deadline=300, what="candidate baseline values")
        record("baseline_candidate_typed", True, verified)
    except BaseException as exc:  # noqa: BLE001
        fail("initial_stream_progress", exc)
        return _finish(gates, evidence, observed, failures, supervisor, src,
                       source, 1)
    except BaseException as exc:  # noqa: BLE001
        fail("initial_stream_progress", exc)
        return _finish(gates, evidence, observed, failures, supervisor, src,
                       source, 1)

    # ── Gate 1b: edge types: float4/float8 and MySQL unsigned bigint ─────
    # Isolated cohort C. A terminal capture error is a real product failure
    # (never silently dropped); exact rows on an active candidate are a pass.
    try:
        def edge_types_ready(db):
            capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
                IntegratedReplicaCapture.connection_id == "acc-src-c"))
            if capture is None:
                return None
            if capture.status in {"blocked", "gap"}:
                raise AssertionError(
                    f"edge capture {capture.status} "
                    f"code={capture.last_error_code}")
            status, rows = candidate_rows(db, assets["float_types"])
            if status is None or rows is None or not float_matches(rows):
                return None
            return {"rows": len(rows), "status": status,
                    "capture_status": capture.status}

        edge = pump(edge_types_ready, deadline=180, what="edge type exactness")
        record("float_and_unsigned_bigint_exact", True, edge)
        with SessionLocal() as db:
            observed["coverage_probes"] = {
                capture.id: capture.coverage_probe
                for capture in db.scalars(sa.select(IntegratedReplicaCapture))}
    except BaseException as exc:  # noqa: BLE001
        with SessionLocal() as db:
            capture = db.scalar(sa.select(IntegratedReplicaCapture).where(
                IntegratedReplicaCapture.connection_id == "acc-src-c"))
            observed["edge_type_failure"] = {
                "error": f"{type(exc).__name__}: {exc}",
                "capture_status": getattr(capture, "status", None),
                "capture_error": getattr(capture, "last_error_code", None)}
        failures.append(f"float/unsigned bigint exactness: {exc}")

    # ── Gate 2: streaming CRUD / PK change / cross-table transaction ─────
    try:
        source_exec([
            f"INSERT INTO {SRC_SCHEMA}.events VALUES (4,'e-4')",
            f"UPDATE {SRC_SCHEMA}.accounts SET price=2.718282 WHERE id=1",
            f"DELETE FROM {SRC_SCHEMA}.events WHERE id=1",
            f"UPDATE {SRC_SCHEMA}.accounts SET id=901 WHERE id=2",
        ])
        source_exec([
            f"UPDATE {SRC_SCHEMA}.accounts SET total=1.0000 WHERE id=3",
            f"INSERT INTO {SRC_SCHEMA}.events VALUES (5,'e-5')",
        ], transaction=True)

        def crud_followed(db):
            status_events, events = candidate_rows(db, assets["events"])
            status_accounts, accounts = candidate_rows(
                db, assets["accounts"])
            if status_events is None or status_accounts is None:
                return None
            notes = [row["note"] for row in events]
            by_id = {row["id"]: row for row in accounts}
            if (sorted(by_id) != [1, 3, 901]
                    or notes != ["e-2", "e-3", "e-4", "e-5"]):
                return None
            return {"ids": sorted(by_id), "notes": notes,
                    "price_1": str(by_id[1]["price"]),
                    "total_3": str(by_id[3]["total"]),
                    "ref_901": by_id[901]["ref"]}

        detail = pump(crud_followed, deadline=180, what="streaming CRUD")
        ok = (detail["price_1"] == "2.718282"
              and detail["total_3"] == "1.0000"
              and detail["ref_901"] == -9223372036854775808)
        record("streaming_crud_pk_crosstx_candidate", bool(ok), detail)
        if not ok:
            raise AssertionError(f"CRUD mismatch: {detail!r}")
    except BaseException as exc:  # noqa: BLE001
        fail("streaming_crud_pk_crosstx_candidate", exc)
        return _finish(gates, evidence, observed, failures, supervisor, src,
                       source, 1)

    # ── Gate 2b: every logical type survives a CDC update exactly ────────
    try:
        if is_pg:
            source_exec([
                f"UPDATE {SRC_SCHEMA}.wide_types SET amount=-1.0000000000,"
                " txt='updated', js='{\"changed\": true}',"
                " bin_value=decode('c0ffee','hex'), opt='now-set', flag=false,"
                f" tag='11111111-2222-4333-8444-555555555555' WHERE id=2"])
        else:
            source_exec([
                f"UPDATE {SRC_SCHEMA}.wide_types SET amount=-1.0000000000,"
                " txt='updated', js='{\"changed\": true}',"
                " bin_value=UNHEX('c0ffee'), opt='now-set', flag=0,"
                f" tag='11111111-2222-4333-8444-555555555555' WHERE id=2"])
        modified = wide_expected()
        modified[2].update({
            "amount": Decimal("-1.0000000000"), "txt": "updated",
            "js": {"changed": True}, "bin_value": b"\xc0\xff\xee",
            "opt": "now-set", "flag": False if is_pg else 0,
            "tag": "11111111-2222-4333-8444-555555555555"})

        def wide_updated(db):
            status, rows = candidate_rows(db, assets["wide_types"])
            if status is None or rows is None:
                return None
            return {"rows": len(rows)} if wide_matches(rows, modified) else None

        detail = pump(wide_updated, deadline=180,
                      what="wide type update exactness")
        record("wide_types_cdc_exact_after_update", True, detail)
    except BaseException as exc:  # noqa: BLE001
        fail("wide_types_cdc_exact_after_update", exc)

    # ── Gate 2c: row-count-parameterised long transaction ────────────────
    # Default 3000 rows proves a single transaction spanning batch/journal
    # limits; the same gate accepts a larger --row-count without changing
    # semantics. 30M-scale seeding is a separate tier and is not claimed here.
    row_count = args.row_count
    update_count = max(1, row_count // 3)
    delete_count = max(1, row_count // 3)
    remaining = row_count - delete_count
    try:
        def long_matches(rows, phase):
            expected_ids = (range(1, remaining + 1) if phase == "delete"
                            else range(1, row_count + 1))
            if [row.get("id") for row in rows] != list(expected_ids):
                return False
            for row in rows:
                if row.get("n") != row["id"] * 7:
                    return False
                want = "P" * 120
                if phase in {"update", "delete"} and row["id"] <= update_count:
                    want = "U" + want
                if row.get("pad") != want:
                    return False
            return True

        if is_pg:
            insert_sql = (f"INSERT INTO {SRC_SCHEMA}.long_tx "
                          "SELECT g, g*7, repeat('P',120) FROM "
                          f"generate_series(1,{row_count}) g")
        else:
            values = ",".join(f"({i},{i * 7},'{'P' * 120}')"
                              for i in range(1, row_count + 1))
            insert_sql = f"INSERT INTO {SRC_SCHEMA}.long_tx VALUES {values}"
        source_exec([insert_sql], transaction=True)

        def long_state(phase, count):
            def probe(db):
                status, rows = candidate_rows(db, assets["long_tx"])
                if status is None or rows is None or len(rows) != count:
                    return None
                return ({"phase": phase, "rows": len(rows)}
                        if long_matches(rows, phase) else None)
            return pump(probe, deadline=300, what=f"long transaction {phase}")

        inserted = long_state("insert", row_count)
        source_exec([f"UPDATE {SRC_SCHEMA}.long_tx SET pad=CONCAT('U',pad)"
                     f" WHERE id BETWEEN 1 AND {update_count}"],
                    transaction=True)
        updated = long_state("update", row_count)
        source_exec([f"DELETE FROM {SRC_SCHEMA}.long_tx WHERE id > {remaining}"],
                    transaction=True)
        deleted = long_state("delete", remaining)
        record("long_transaction_insert_update_delete", True,
               {"row_count": row_count, "update_count": update_count,
                "delete_count": delete_count, "insert": inserted,
                "update": updated, "delete": deleted})
    except BaseException as exc:  # noqa: BLE001
        fail("long_transaction_insert_update_delete", exc)

    # ── Gate 2d: TOAST placeholder fill and exact text/jsonb updates ─────
    try:
        source_exec([f"UPDATE {SRC_SCHEMA}.toast_rows SET note='note-only'"
                     " WHERE id=1"])
        expected = toast_expected()
        expected[1]["note"] = "note-only"

        def toast_phase(want, what):
            def probe(db):
                status, rows = candidate_rows(db, assets["toast_rows"])
                if status is None or rows is None:
                    return None
                return {"rows": len(rows)} if toast_matches(rows, want) else None
            return pump(probe, deadline=180, what=what)

        note_only = toast_phase(expected, "TOAST unchanged-column fill")
        if is_pg:
            source_exec([
                f"UPDATE {SRC_SCHEMA}.toast_rows SET"
                " big_text=repeat('D',149990)||'-v2',"
                " big_json=jsonb_build_object('deep',repeat('E',120000))"
                " WHERE id=2"])
        else:
            source_exec([
                f"UPDATE {SRC_SCHEMA}.toast_rows SET"
                " big_text=CONCAT(REPEAT('D',149990),'-v2'),"
                " big_json=JSON_OBJECT('deep',REPEAT('E',120000))"
                " WHERE id=2"])
        expected[2].update({"big_text": "D" * 149990 + "-v2",
                            "big_json": {"deep": "E" * 120000}})
        replaced = toast_phase(expected, "TOAST replacement exactness")
        source_exec([f"DELETE FROM {SRC_SCHEMA}.toast_rows WHERE id=1"])
        expected.pop(1)

        def toast_deleted(db):
            status, rows = candidate_rows(db, assets["toast_rows"])
            if status is None or rows is None:
                return None
            return {"rows": len(rows)} if toast_matches(rows, expected) else None

        deleted = pump(toast_deleted, deadline=180, what="TOAST delete")
        record("toast_placeholder_update_delete_exact", True,
               {"note_only": note_only, "replaced": replaced,
                "deleted": deleted})
    except BaseException as exc:  # noqa: BLE001
        fail("toast_placeholder_update_delete_exact", exc)

    # ── Gate 2e: controlled source CRUD with row-token CAS ──────────────
    try:
        def mutate_account(operation, key, values, *, token=None, idem):
            body = RowMutation(key=key, values=values,
                               expected_row_token=token,
                               idempotency_key=idem)
            with SessionLocal() as db:
                return service.mutate_row(
                    db, db.get(User, user_id), assets["accounts"],
                    operation, body)

        def expect_mutation_error(code, **kwargs):
            try:
                mutate_account(**kwargs)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                return exc.status_code == 409 and detail.get("code") == code
            return False

        def candidate_account(wanted_id):
            with SessionLocal() as db:
                status, rows = candidate_rows(db, assets["accounts"])
            if status is None or rows is None:
                return None
            return next((row for row in rows if row.get("id") == wanted_id),
                        None)

        def wait_account(wanted_id, name=None, absent=False):
            def probe(_db):
                row = candidate_account(wanted_id)
                if absent:
                    return {"absent": True} if row is None else None
                return ({"name": row.get("name")} if row is not None
                        and row.get("name") == name else None)
            return pump(probe, deadline=180, what=f"account {wanted_id}")

        insert_values = {
            "name": "delta", "price": "1.500000", "total": "2.0000",
            "meta": '{"added": true}',
            "payload": {"$binary": base64.b64encode(b"\x01\x02\x03").decode()},
            "ref": 0,
        }
        def preview_row(wanted_id):
            with SessionLocal() as db:
                try:
                    preview = service.preview_rows(
                        db, db.get(User, user_id), assets["accounts"], 1000)
                except Exception:  # noqa: BLE001 - transient read state
                    return None
            return next((row for row in preview["rows"]
                         if row["values"].get("id") == wanted_id), None)

        def wait_preview(wanted_id, *, name=None, meta=None,
                         token_not=None):
            def probe(_db):
                row = preview_row(wanted_id)
                if row is None:
                    return None
                values = row["values"]
                if name is not None and values.get("name") != name:
                    return None
                if meta is not None:
                    try:
                        parsed = json.loads(str(values.get("meta")))
                    except (TypeError, ValueError):
                        return None
                    if parsed != meta:
                        return None
                if token_not is not None and row.get("row_token") == token_not:
                    return None
                return row
            return pump(probe, deadline=180,
                        what=f"preview account {wanted_id}")

        first = mutate_account("insert", {"id": 910}, insert_values,
                               idem="cas-insert-910")
        wait_account(910, "delta")
        preview1 = wait_preview(910, name="delta")
        # The preview token is the source-read-compatible CAS token; using
        # only the mutation receipt token would miss this compatibility bug.
        mutate_account("update", {"id": 910}, {"name": "delta2"},
                       token=preview1["row_token"], idem="cas-update-910-1")
        wait_account(910, "delta2")
        stale_receipt_rejected = expect_mutation_error(
            "ROW_CONFLICT", operation="update", key={"id": 910},
            values={"name": "delta3"}, token=first["row_token"],
            idem="cas-stale-910")
        preview2 = wait_preview(910, name="delta2",
                                token_not=preview1["row_token"])
        mutate_account("update", {"id": 910}, {"name": "delta4"},
                       token=preview2["row_token"], idem="cas-update-910-2")
        wait_account(910, "delta4")
        # A real CDC JSON update must yield a new source-compatible token.
        source_exec([f"UPDATE {SRC_SCHEMA}.accounts SET "
                     "meta='{\"cdc\": true}' WHERE id=910"])
        preview3 = wait_preview(910, name="delta4",
                                meta={"cdc": True},
                                token_not=preview2["row_token"])
        mutate_account("update", {"id": 910}, {"name": "delta5"},
                       token=preview3["row_token"], idem="cas-update-910-3")
        wait_account(910, "delta5")
        # A real source-side concurrent update must make the old preview
        # token fail CAS (no receipt shortcut).
        preview4 = wait_preview(910, name="delta5",
                                token_not=preview3["row_token"])
        source_exec([f"UPDATE {SRC_SCHEMA}.accounts SET name='delta-race'"
                     " WHERE id=910"])
        race_rejected = expect_mutation_error(
            "ROW_CONFLICT", operation="update", key={"id": 910},
            values={"name": "delta6"}, token=preview4["row_token"],
            idem="cas-race-910")
        with src.connect() as conn:
            source_name = conn.execute(sa.text(
                f"SELECT name FROM {SRC_SCHEMA}.accounts WHERE id=910")).scalar()
        duplicate_rejected = expect_mutation_error(
            "ROW_ALREADY_EXISTS", operation="insert", key={"id": 910},
            values=insert_values, idem="cas-insert-910-dup")
        preview5 = wait_preview(910, name="delta-race")
        mutate_account("delete", {"id": 910}, {},
                       token=preview5["row_token"], idem="cas-delete-910")
        wait_account(910, absent=True)
        replay = mutate_account("insert", {"id": 910}, insert_values,
                                idem="cas-insert-910")
        with src.connect() as conn:
            replay_absent = conn.execute(sa.text(
                f"SELECT count(*) FROM {SRC_SCHEMA}.accounts WHERE id=910"
            )).scalar_one() == 0
        ok = (stale_receipt_rejected and race_rejected
              and duplicate_rejected and source_name == "delta-race"
              and replay.get("replayed") is True and replay_absent)
        record("controlled_crud_row_token_cas", bool(ok), {
            "preview_token_used": True,
            "preview_token_update_ok": True,
            "stale_receipt_rejected": stale_receipt_rejected,
            "json_cdc_new_preview_token": preview3["row_token"]
                != preview2["row_token"],
            "source_race_rejected": race_rejected,
            "source_after_race": source_name,
            "duplicate_rejected": duplicate_rejected,
            "replay_replayed": replay.get("replayed"),
            "replay_source_absent": replay_absent})
        preview_ok = (race_rejected
                      and preview3["row_token"] != preview2["row_token"])
        record("preview_token_source_compatible_cas", bool(preview_ok), {
            "preview_token_update_used": True,
            "json_cdc_token_changed": preview3["row_token"]
                != preview2["row_token"],
            "source_race_rejected": race_rejected})
        if not ok:
            raise AssertionError("controlled CRUD CAS mismatch")
    except BaseException as exc:  # noqa: BLE001
        fail("controlled_crud_row_token_cas", exc)

    # ── Gate 2f: commit→local-query P95 via production replica_read ──────
    # This measures service.preview_rows (replica_read local SQL), never a
    # direct candidate-table SELECT. Two capture connections on ONE source
    # cluster; not two (and never 200) independent source instances.
    try:
        def local_visible(asset_name, row_id, column, value):
            with SessionLocal() as db:
                preview = service.preview_rows(
                    db, db.get(User, user_id), assets[asset_name], 1000)
            return any(entry["values"].get("id") == row_id
                       and entry["values"].get(column) == value
                       for entry in preview["rows"])

        def sample_latency(asset_name, table, column, row_id, value_of,
                           restore):
            samples = []
            for index in range(args.latency_samples):
                value = value_of(index)
                started_lat = time.monotonic()
                source_exec([f"UPDATE {SRC_SCHEMA}.{table} SET {column}="
                             f"'{value}' WHERE id={row_id}"])
                seen = None
                deadline = started_lat + 15
                while time.monotonic() < deadline:
                    supervisor.tick()
                    try:
                        visible = local_visible(asset_name, row_id, column,
                                                value)
                    except Exception:  # noqa: BLE001 - transient read state
                        visible = False
                    if visible:
                        seen = (time.monotonic() - started_lat) * 1000
                        break
                    time.sleep(0.05)
                if seen is None:
                    raise AssertionError(
                        f"latency sample {index} never visible via local query")
                samples.append(seen)
            source_exec([f"UPDATE {SRC_SCHEMA}.{table} SET {column}="
                         f"'{restore}' WHERE id={row_id}"])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                supervisor.tick()
                try:
                    restored = local_visible(asset_name, row_id, column,
                                             restore)
                except Exception:  # noqa: BLE001
                    restored = False
                if restored:
                    break
                time.sleep(0.05)
            ordered = sorted(samples)
            return {
                "samples": [round(value, 1) for value in samples],
                "p50": round(ordered[len(ordered) // 2], 1),
                "p95": round(ordered[min(len(ordered) - 1,
                                         int(0.95 * (len(ordered) - 1)))], 1),
            }

        latency = {}
        if args.source_count >= 1:
            latency["acc-src-a"] = sample_latency(
                "events", "events", "note", 2,
                lambda index: f"latA-{index}", "e-2")
        if args.source_count >= 2:
            latency["acc-src-b"] = sample_latency(
                "rebuild_target", "rebuild_target", "val", 1,
                lambda index: f"latB-{index}", "rt-1")
        for detail in latency.values():
            detail["lane"] = "replica_read.preview_rows_local_sql"
            # Conservative upper bound: the clock starts before the source
            # UPDATE/COMMIT, so it includes source SQL time; it is not an
            # exact commit-instant measurement.
            detail["measurement_start"] = "before_source_update_commit"
        observed["local_read_latency_ms"] = latency
        observed["latency_scope"] = (
            "capture connections on one source cluster; not independent DB "
            "instances")
        worst = max(detail["p95"] for detail in latency.values())
        record("local_read_p95_le_3000ms", worst <= 3000.0, latency)
    except BaseException as exc:  # noqa: BLE001
        fail("local_read_p95_le_3000ms", exc)

    def readable_now():
        with SessionLocal() as db:
            states = replica_states(db)
            return [name for name, asset_id in assets.items()
                    if states.get(asset_id, {}).get("active")
                    and states.get(asset_id, {}).get("status") in {
                        "ready", "stale"}]

    def _gate_local_read():
        from contextlib import ExitStack

        def forbidden(*_args, **_kwargs):
            raise AssertionError("read lane must not touch the source")

        saved = (native.source_engine, native.open_read_scope,
                 native.probe_source)
        native.source_engine = forbidden
        native.open_read_scope = forbidden
        native.probe_source = forbidden
        try:
            with SessionLocal() as db:
                user_row = db.get(User, user_id)
                preview = service.preview_rows(db, user_row,
                                               assets["accounts"], 10)
                with ExitStack() as stack:
                    relation = providers.resolve_integrated(
                        db, user_row,
                        [("src", Binding(kind="integrated",
                                         asset_id=assets["accounts"]))],
                        QueryLimits(), stack)["src"]
                    names = sorted(row[0] for row in db.execute(
                        sa.select(relation.expressions["name"])))
                    relation.recheck()
            # "stale" is a documented readable state (last confirmed coverage,
            # honestly labeled); this gate proves no source fallback, not
            # freshness. Publication readiness is covered by Gate 1.
            ok = (preview["source_state"]["replica_status"] in {"ready",
                                                                 "stale"}
                  and names == ["alpha", "beta", "gamma"])
            record("local_read_no_source_fallback", bool(ok),
                   {"preview_rows": len(preview["rows"]), "query_names": names,
                    "replica_status":
                        preview["source_state"]["replica_status"]})
        finally:
            (native.source_engine, native.open_read_scope,
             native.probe_source) = saved
        if not ok:
            raise AssertionError("local read mismatch")

    # ── Gate 3: local preview/query never fall back to the source ────────
    try:
        if not readable_now():
            # Honest no-pass: publication is correctly held at catching_up by
            # the verified-catchup barrier; local read lane needs an active
            # run. Never faked.
            gates["local_read_no_source_fallback"] = False
            failures.append("local_read_no_source_fallback: "
                            "SKIPPED_requires_ready (catching_up barrier)")
            evidence["local_read_no_source_fallback"] = {
                "reason": "no published replica; runtime holds catching_up"}
        else:
            _gate_local_read()
    except BaseException as exc:  # noqa: BLE001
        fail("local_read_no_source_fallback", exc)

    # ── Gate 3b: real source network outage; local reads stay local ──────
    # Host disconnects the owned fixture source container from the network on
    # @@SOURCE_DISCONNECT@@ and reconnects with the service alias on
    # @@SOURCE_RECONNECT@@. This is a real outage, distinct from killing only
    # our own child (Gate 4). The host finally-trap reconnects on crash.
    try:
        if not readable_now():
            raise AssertionError("no published replica for outage gate")
        print(SOURCE_DISCONNECT, flush=True)
        down = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with src.connect() as conn:
                    conn.execute(sa.text("SELECT 1"))
            except Exception:  # noqa: BLE001
                down = True
                break
            time.sleep(0.5)
        if not down:
            raise AssertionError("source still reachable after disconnect")
        stale = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            supervisor.tick()
            with SessionLocal() as db:
                states = replica_states(db)
                captures = [row for row in capture_rows(db)
                            if row["connection"] == "acc-src-a"]
                if (states.get("asset-accounts", {}).get("status") == "stale"
                        or (captures and captures[0]["status"] == "stale")):
                    stale = {"replica": states.get("asset-accounts"),
                             "capture": captures[0] if captures else None}
                    break
            time.sleep(0.5)
        if stale is None:
            raise AssertionError("replica did not become stale on outage")

        def forbidden(*_args, **_kwargs):
            raise AssertionError("outage read lane touched the source")

        saved = (native.source_engine, native.open_read_scope,
                 native.probe_source)
        native.source_engine = forbidden
        native.open_read_scope = forbidden
        native.probe_source = forbidden
        try:
            with SessionLocal() as db:
                user_row = db.get(User, user_id)
                preview = service.preview_rows(db, user_row,
                                               assets["accounts"], 10)
                with __import__("contextlib").ExitStack() as stack:
                    relation = providers.resolve_integrated(
                        db, user_row,
                        [("src", Binding(kind="integrated",
                                         asset_id=assets["accounts"]))],
                        QueryLimits(), stack)["src"]
                    names = sorted(row[0] for row in db.execute(
                        sa.select(relation.expressions["name"])))
                    relation.recheck()
        finally:
            (native.source_engine, native.open_read_scope,
             native.probe_source) = saved
        local_ok = (preview["source_state"]["replica_status"] in {"ready",
                                                                  "stale"}
                    and names == ["alpha", "beta", "gamma"])
        outage_detail = {"stale": stale, "local_read": local_ok,
                         "names": names,
                         "replica_status":
                             preview["source_state"]["replica_status"]}
        print(SOURCE_RECONNECT, flush=True)
        src.dispose()
        reachable = False
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with src.connect() as conn:
                    conn.execute(sa.text("SELECT 1"))
                reachable = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        recovery = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and reachable:
            supervisor.tick()
            with SessionLocal() as db:
                states = replica_states(db)
                captures = [row for row in capture_rows(db)
                            if row["connection"] == "acc-src-a"]
                status = states.get("asset-accounts", {}).get("status")
                if status in {"ready", "stale"} and captures and \
                        captures[0]["status"] in {"starting", "streaming",
                                                  "stale"}:
                    recovery = {"replica": status,
                                "capture": captures[0]["status"]}
                    break
            time.sleep(0.5)
        ok = bool(stale) and local_ok and reachable and recovery is not None
        outage_detail.update({"reachable_after_reconnect": reachable,
                              "recovery": recovery})
        record("source_disconnect_local_read", bool(ok), outage_detail)
        if not ok:
            failures.append(
                "source_disconnect_local_read: "
                + json.dumps(outage_detail, default=str)[:2000])
    except BaseException as exc:  # noqa: BLE001
        # Reconnect immediately if the failure happened while the source was
        # disconnected; the host also has a reconnect trap.
        try:
            print(SOURCE_RECONNECT, flush=True)
        except Exception:  # noqa: BLE001
            pass
        fail("source_disconnect_local_read", exc)

    # ── Gate 4: shutdown/restart resumes the stream (no re-snapshot) ─────
    with SessionLocal() as _db:
        _established = [name for name, asset_id in assets.items()
                        if _db.execute(
                            sa.select(IntegratedReplicaRun.status)
                            .where(IntegratedReplicaRun.asset_id == asset_id)
                            .order_by(IntegratedReplicaRun.created_at.desc())
                            .limit(1)).scalar() == "active"]
    if not _established:
        # Honest no-pass: restart of a non-active (catching_up) cohort is by
        # design an abandon+rebuild, not a resume; this gate needs published
        # active runs behind the verified-catchup barrier.
        gates["restart_resume_no_resnapshot"] = False
        failures.append("restart_resume_no_resnapshot: "
                        "SKIPPED_requires_active_run (catching_up barrier)")
        evidence["restart_resume_no_resnapshot"] = {
            "reason": "no published active runs to resume"}
    else:
        try:
            with SessionLocal() as db:
                before_captures = capture_rows(db)
                before_active = {asset_id: state["active"]
                                 for asset_id, state
                                 in replica_states(db).items()}
            supervisor.shutdown()
            # A real executor restart closes the process and its advisory-lock
            # connection. In this single-process driver the old lease object
            # would otherwise keep the lock, so release it explicitly to model
            # process exit faithfully (never to bypass ownership checks).
            if getattr(supervisor, "lease", None) is not None:
                supervisor.lease.reset()
            # Session-level advisory locks survive a pooled connection close;
            # dispose every pooled socket exactly as process exit would.
            engine.dispose()
            supervisor = fresh_supervisor()
            # A source change committed while the whole executor was down.
            source_exec([f"INSERT INTO {SRC_SCHEMA}.events VALUES (6,'e-6')"])

            def resumed(db):
                captures = [row for row in capture_rows(db)
                            if row["connection"] in {"acc-src-a", "acc-src-b"}]
                if len(captures) < 2:
                    return None
                terminal = [row for row in captures
                            if row["status"] in {"blocked", "gap", "stopped"}]
                if terminal:
                    raise AssertionError(
                        f"capture did not resume: {terminal}")
                status_events, events = candidate_rows(db, assets["events"])
                notes = [row["note"] for row in events or []]
                return ({"notes": notes} if "e-6" in notes
                        and status_events == "active" else None)

            detail = pump(resumed, deadline=240, what="restart resume")
            with SessionLocal() as db:
                after_captures = capture_rows(db)
                after_active = {asset_id: state["active"] for asset_id, state
                                in replica_states(db).items()}
            ok = (len(before_captures) == len(after_captures)
                  and {(row["connection"], row["generation"]) for row in
                       before_captures}
                  == {(row["connection"], row["generation"]) for row in
                      after_captures}
                  and before_active == after_active)
            observed["captures"] = after_captures
            record("restart_resume_no_resnapshot", bool(ok), {
                "captures_before": len(before_captures),
                "captures_after": len(after_captures),
                "active_runs_stable": before_active == after_active,
                "detail": detail})
            if not ok:
                raise AssertionError("restart changed capture/run topology")
        except BaseException as exc:  # noqa: BLE001
            fail("restart_resume_no_resnapshot", exc)

    # ── Gate 4b: two consecutive IDLE restarts must keep offsets non-empty ─
    # No business write is performed: the restart itself may deliver a SKIPPED
    # snapshot notification. If that notification is treated as an offset
    # commit with an empty value, the persisted offsets.dat is poisoned and a
    # subsequent restart cannot resume. Read the real production offset files.
    try:
        idle_rounds = []
        for round_index in (1, 2):
            supervisor.shutdown()
            if getattr(supervisor, "lease", None) is not None:
                supervisor.lease.reset()
            engine.dispose()
            supervisor = fresh_supervisor()
            with SessionLocal() as db:
                ab_ids = {row["id"] for row in capture_rows(db)
                          if row["connection"] in {"acc-src-a", "acc-src-b"}}
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                supervisor.tick()
                if ab_ids <= set(supervisor.processes) and all(
                        supervisor.processes[capture_id].child is not None
                        for capture_id in ab_ids):
                    break
                time.sleep(0.2)
            # Allow the restarted child to process SKIPPED and flush offsets.
            time.sleep(4)
            round_detail = {}
            for capture_id in sorted(ab_ids):
                path = Path("/tmp/ra/state") / capture_id / "offsets.dat"
                entry = {"path": str(path), "exists": path.is_file()}
                if path.is_file():
                    try:
                        offsets = supervisor._read_offsets(path)
                    except Exception as exc:  # noqa: BLE001
                        entry["read_error"] = f"{type(exc).__name__}: {exc}"
                    else:
                        expected = "oo_replica_" + capture_id.replace("-", "")
                        target = [
                            item for item in offsets
                            if isinstance(item, dict)
                            and (item.get("partition") or {}).get("server")
                            == expected]
                        entry["offsets"] = target
                        entry["source_kind"] = ("postgres" if is_pg
                                                else "mysql")
                        # Authoritative production validator: exactly one
                        # matching partition with a dialect position. PG needs
                        # lsn/lsn_proc/lsn_commit; MySQL needs file+pos.
                        problem = None
                        validator = getattr(supervisor, "_offset_problem",
                                            None)
                        if validator is not None:
                            problem = validator(offsets, capture_id,
                                                entry["source_kind"])
                        entry["offset_problem"] = problem
                        if validator is not None:
                            entry["non_empty"] = problem is None
                        else:
                            entry["non_empty"] = bool(target) and all(
                                isinstance(item.get("offset"), dict)
                                and bool(item["offset"])
                                and (item["offset"].get("lsn") is not None
                                     or (item["offset"].get("file")
                                         and item["offset"].get("pos")
                                         is not None)
                                     or item["offset"].get("gtids")
                                     is not None)
                                for item in target)
                round_detail[capture_id] = entry
            idle_rounds.append({"round": round_index,
                                "captures": round_detail})
        ok = all(all(item.get("non_empty") for item in round_item["captures"]
                     .values()) for round_item in idle_rounds)
        record("idle_restart_offsets_not_empty", bool(ok),
               {"rounds": idle_rounds})
        if not ok:
            failures.append(
                "idle_restart_offsets_not_empty: SKIPPED notification left "
                "empty/poisoned offsets.dat after idle restart")
    except BaseException as exc:  # noqa: BLE001
        fail("idle_restart_offsets_not_empty", exc)

    # ── Gate 5: interrupted candidate fenced and rebuilt in a new capture
    try:
        with SessionLocal() as db:
            cohort_a = [row for row in capture_rows(db)
                        if row["connection"] == "acc-src-a"]
            assert len(cohort_a) == 1, f"cohort A capture count {cohort_a}"
            cohort_a_id = cohort_a[0]["id"]
            captures_before = {row["id"] for row in capture_rows(db)}
            control.request_rebuild(
                db, db.get(IntegratedAsset, assets["rebuild_target"]))
            db.commit()
        # The idle-restart gate may have only just spawned cohort A's child;
        # wait for the real child before taking the stability baseline.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            item = supervisor.processes.get(cohort_a_id)
            if item is not None and item.child is not None:
                break
            supervisor.tick()
            time.sleep(0.2)
        item = supervisor.processes.get(cohort_a_id)
        assert item is not None and item.child is not None, \
            "cohort A process/child missing"
        started_at_before = item.started_at

        def child_spawned(_db):
            # Only a NEW capture id (the candidate) may be the victim —
            # cohort B's established capture also has a live child.
            for capture in supervisor.processes.values():
                if (capture.capture_id not in captures_before
                        and capture.child is not None):
                    return capture.capture_id
            return None

        victim = pump(child_spawned, deadline=180,
                      what="candidate capture child spawn")
        supervisor.processes[victim].child.kill_group()
        observed["interrupted_capture"] = victim

        def rebuilt(db):
            status, rows = candidate_rows(db, assets["rebuild_target"])
            if status is None or not candidate_exact("rebuild_target",
                                                     status, rows):
                return None
            captures_b = [row for row in capture_rows(db)
                          if row["connection"] == "acc-src-b"]
            terminal = [row for row in captures_b
                        if row["status"] in {"blocked", "gap"}
                        and row["id"] != victim]
            if terminal:
                raise AssertionError(f"cohort B terminal: {terminal}")
            if len(captures_b) < 3 or not any(
                    row["status"] in {"starting", "streaming"}
                    for row in captures_b):
                return None
            victim_row = next((row for row in captures_b
                               if row["id"] == victim), None)
            if victim_row is None or victim_row["status"] != "stopped":
                return None
            return {"captures_b": captures_b, "candidate_status": status}

        detail = pump(rebuilt, deadline=300, what="candidate rebuild")
        with SessionLocal() as db:
            failed = [{"status": row[0], "code": row[1]} for row in
                      db.execute(sa.select(IntegratedReplicaRun.status,
                                           IntegratedReplicaRun.last_error_code)
                                 .where(IntegratedReplicaRun.asset_id
                                        == assets["rebuild_target"])
                                 .where(IntegratedReplicaRun.status == "failed"))]
            status, target_rows = candidate_rows(db,
                                                 assets["rebuild_target"])
            states = replica_states(db)
        item = supervisor.processes.get(cohort_a_id)
        cohort_a_intact = (
            item is not None and item.started_at == started_at_before
            and next((row["status"] for row in capture_rows(SessionLocal())
                      if row["id"] == cohort_a_id), None)
            in {"starting", "streaming", "stale"})
        ok = (len(failed) >= 1
              and candidate_exact("rebuild_target", status,
                                  target_rows or []) is True
              and cohort_a_intact)
        detail_out = {"failed_runs": failed,
                      "cohort_a_intact": cohort_a_intact,
                      "candidate_status": status, "pump": detail,
                      "captures_b": capture_rows(SessionLocal())}
        record("interrupted_candidate_rebuilt", bool(ok), detail_out)
        if not ok:
            failures.append(
                "interrupted_candidate_rebuilt: "
                + json.dumps(detail_out, default=str)[:3000])
    except BaseException as exc:  # noqa: BLE001
        fail("interrupted_candidate_rebuilt", exc)

    # ── Gate 6: adding a table never restarts the existing capture ───────
    try:
        with SessionLocal() as db:
            cohort_a_id = next(row["id"] for row in capture_rows(db)
                               if row["connection"] == "acc-src-a")
        started_at_before = (supervisor.processes[cohort_a_id].started_at
                             if cohort_a_id in supervisor.processes else None)
        source_exec([
            (f"CREATE TABLE {SRC_SCHEMA}.orders (id integer PRIMARY KEY,"
             " item text NOT NULL, qty integer NOT NULL);"
             f"ALTER TABLE {SRC_SCHEMA}.orders REPLICA IDENTITY FULL"
             if is_pg else
             f"CREATE TABLE {SRC_SCHEMA}.orders (id INT PRIMARY KEY,"
             " item VARCHAR(100) NOT NULL, qty INT NOT NULL)"),
            f"INSERT INTO {SRC_SCHEMA}.orders VALUES (1,'widget',7),"
            "(2,'gizmo',3)",
        ])
        with SessionLocal() as db:
            register_asset(db, "acc-src-a", "orders")
            db.commit()
        assets["orders"] = "asset-orders"

        def orders_ready(db):
            status, rows = candidate_rows(db, assets["orders"])
            if status is None:
                return None
            exact = candidate_exact("orders", status, rows)
            return {"status": status, "rows": rows} if exact else None

        detail = pump(orders_ready, deadline=300, what="orders replica")
        item = supervisor.processes.get(cohort_a_id)
        with SessionLocal() as db:
            captures_a = [row for row in capture_rows(db)
                          if row["connection"] == "acc-src-a"]
        started_stable = (item is not None and started_at_before is not None
                          and item.started_at == started_at_before)
        ok = (started_stable
              and any(row["status"] in {"starting", "streaming", "stale"}
                      for row in captures_a)
              and [row["item"] for row in detail["rows"]] == ["widget",
                                                              "gizmo"])
        record("add_table_keeps_existing_stream", bool(ok), {
            "cohort_a": cohort_a_id, "started_at_stable": started_stable,
            "captures_a": captures_a, "orders": detail["rows"]})
        observed["captures_final"] = capture_rows(SessionLocal())
    except BaseException as exc:  # noqa: BLE001
        fail("add_table_keeps_existing_stream", exc)

    # ── Gate 7: drain every owned capture and prove no source resources leak
    try:
        with SessionLocal() as db:
            db.execute(sa.update(IntegratedReplicaCapture).values(
                status="draining"))
            db.commit()

        def drained(db):
            rows = capture_rows(db)
            return {"captures": rows} if all(
                row["status"] == "stopped" for row in rows) else None

        drain = pump(drained, deadline=120, what="capture drain")
        supervisor.shutdown()
        slots = []
        if is_pg:
            from app.data_channel.connections.models import Connection
            from app.data_channel.integrated import replica_connector

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with src.connect() as conn:
                    slots = [row[0] for row in conn.execute(sa.text(
                        "SELECT slot_name FROM pg_replication_slots "
                        "WHERE slot_name LIKE 'ir_%'"))]
                if not slots:
                    break
                # The draining reconcile may race the just-stopped walsender;
                # retry the production cleanup until the slot is releasable.
                with SessionLocal() as db:
                    for capture in drain["captures"]:
                        row = db.get(IntegratedReplicaCapture, capture["id"])
                        if row is None:
                            continue
                        try:
                            replica_connector.cleanup(
                                db.get(Connection, row.connection_id),
                                capture["id"])
                        except Exception:  # noqa: BLE001
                            pass
                    db.commit()
                time.sleep(1.0)
        record("cleanup_captures_stopped_no_slot_leak",
               all(row["status"] == "stopped" for row in drain["captures"])
               and not slots,
               {"captures": drain["captures"], "leftover_slots": slots})
    except BaseException as exc:  # noqa: BLE001
        fail("cleanup_captures_stopped_no_slot_leak", exc)

    ok = bool(gates) and all(gates.values()) and not failures
    return _finish(gates, evidence, observed, failures, supervisor, src,
                   source, 0 if ok else 1)


def _finish(gates, evidence, observed, failures, supervisor, src, source,
            code) -> int:
    try:
        supervisor.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        src.dispose()
    except Exception:  # noqa: BLE001
        pass
    payload = {"source": source, "ok": code == 0, "gates": gates,
               "evidence": evidence, "observed": observed,
               "failures": failures}
    print(MARKER + json.dumps(payload, default=str))
    return code


def main() -> int:
    global NETWORK, IMAGE, CONTAINER, SRC_PG_CONTAINER, \
        SRC_MYSQL_CONTAINER, TARGET_PG_CONTAINER, TARGET_DB, ARTIFACTS, JAR, \
        DRIVER_MEMORY, DRIVER_CPUS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["host", "driver"], default="host")
    parser.add_argument("--source", choices=["pg", "mysql"], default="pg")
    parser.add_argument("--keep-fixtures", action="store_true")
    # Bounded scale knobs. 200 independent source instances and multi-JVM
    # fan-out are explicitly out of this 8 GB environment's verifiable scope;
    # two connections on one cluster are reported as such, never as 200 DBs.
    parser.add_argument("--row-count", type=int, default=3000)
    parser.add_argument("--latency-samples", type=int, default=20)
    parser.add_argument("--source-count", type=int, choices=[1, 2],
                        default=2)
    # CI/9337 parameterization: unique compose project, network, image and
    # artifact locations. Defaults remain the local development fixture.
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help="compose project/container prefix")
    parser.add_argument("--network", default=None,
                        help="docker network (default: --project)")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--jar", default=str(JAR),
                        help="SMT jar path, or 'builtin' to use the image jar")
    parser.add_argument("--target-db", default=TARGET_DB)
    parser.add_argument("--artifacts", default=".artifacts/replica-acceptance")
    parser.add_argument("--driver-memory", default=DRIVER_MEMORY)
    parser.add_argument("--driver-cpus", default=DRIVER_CPUS)
    parser.add_argument("--timeout", type=int, default=DRIVER_TIMEOUT_SECONDS,
                        help="driver watchdog timeout in seconds")
    args = parser.parse_args()
    if not 1 <= args.row_count <= 50000:
        parser.error("--row-count must be within 1..50000 for this 8 GB host")
    if not 5 <= args.latency_samples <= 200:
        parser.error("--latency-samples must be within 5..200")
    if not 60 <= args.timeout <= 3600:
        parser.error("--timeout must be within 60..3600 seconds")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.project):
        parser.error("--project must match [a-z0-9][a-z0-9_-]{0,62}")
    if args.network is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", args.network):
        parser.error("--network is not a safe docker network name")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", args.target_db):
        parser.error("--target-db must be a safe lowercase SQL identifier")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}", args.image):
        parser.error("--image is not a safe image reference")
    NETWORK = args.network or args.project
    IMAGE = args.image
    DRIVER_MEMORY = args.driver_memory
    DRIVER_CPUS = args.driver_cpus
    CONTAINER = (f"{args.project}-acceptance-driver"
                 if args.project != DEFAULT_PROJECT
                 else "oo-replica-acceptance-driver")
    SRC_PG_CONTAINER = f"{args.project}-source-pg-1"
    SRC_MYSQL_CONTAINER = f"{args.project}-source-mysql-1"
    TARGET_PG_CONTAINER = f"{args.project}-target-pg-1"
    TARGET_DB = args.target_db
    artifacts = Path(args.artifacts)
    ARTIFACTS = artifacts if artifacts.is_absolute() else ROOT / artifacts
    JAR = None if str(args.jar).lower() in {"builtin", "none", ""} else (
        Path(args.jar) if Path(args.jar).is_absolute() else ROOT / args.jar)
    if args.mode == "driver":
        return driver_main(args)
    return host_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
