#!/usr/bin/env python3
"""Bounded single-table scale driver for the current replica (real DBZ path).

The source is seeded with ``generate_series`` (never a direct source→target
COPY). The table then goes through the full production path: ReplicaSupervisor,
production receiver, Debezium Server native initial snapshot (SQL COPY on the
source side), replica ingest/journal, typed store COPY batches on the target,
publication, production local query, and a post-snapshot CDC update/delete.

Evidence/progress go to ``.artifacts/replica-scale/``. This script never fakes
200 independent instances; it is one source cluster and one narrow table.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[2] if len(SCRIPT.parents) > 2 else Path("/work")
PROJECT = "oo-replica-spike-20261112"
NETWORK = PROJECT
IMAGE = "openontology-cdc-executor:canary"
CONTAINER = f"{PROJECT}-scale-driver"
SOURCE = f"{PROJECT}-source-pg-1"
TARGET = f"{PROJECT}-target-pg-1"
TARGET_DB = "replica_scale"
JAR = ROOT / ".artifacts/replica-java/replica-smt.jar"
ART = ROOT / ".artifacts/replica-scale"
USER = "replica_fixture"
MARKER = "@@RESULT@@"


def run(args, *, stdin=None, timeout=600, check=True):
    result = subprocess.run(args, input=stdin, text=True,
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args[:6])}\n"
            f"{result.stdout[-1000:]}\n{result.stderr[-2000:]}")
    return result


def psql(statement, *, database="replica_source", timeout=1800, check=True):
    return run(["docker", "exec", "-i", SOURCE, "psql", "-X", "-qAt",
                "-v", "ON_ERROR_STOP=1", "-U", USER, "-d", database],
               stdin=statement, timeout=timeout, check=check).stdout.strip()


def target_psql(statement, *, database="replica_target", timeout=300,
                check=True):
    return run(["docker", "exec", "-i", TARGET, "psql", "-X", "-qAt",
                "-v", "ON_ERROR_STOP=1", "-U", USER, "-d", database],
               stdin=statement, timeout=timeout, check=check).stdout.strip()


def host_seed(rows: int) -> dict:
    psql("""DROP SCHEMA IF EXISTS scale CASCADE; CREATE SCHEMA scale;
            CREATE TABLE scale.scale_rows(id bigint PRIMARY KEY,
                                          val integer NOT NULL);
            ALTER TABLE scale.scale_rows REPLICA IDENTITY FULL;""")
    started = time.monotonic()
    chunk = 100000
    seeded = 0
    checkpoints = []
    while seeded < rows:
        count = min(chunk, rows - seeded)
        psql(f"INSERT INTO scale.scale_rows "
             f"SELECT g, (g % 1000)::int FROM generate_series("
             f"{seeded + 1},{seeded + count}) g;", timeout=1800)
        seeded += count
        if seeded % 100000 == 0 or seeded == rows:
            checkpoints.append({"rows": seeded,
                                "elapsed_sec": round(time.monotonic() - started, 2)})
            print(f"@@SEED@@ rows={seeded} elapsed="
                  f"{checkpoints[-1]['elapsed_sec']}s", flush=True)
    return {"rows": rows, "seconds": round(time.monotonic() - started, 2),
            "checkpoints": checkpoints}


def host_main(args) -> int:
    ART.mkdir(parents=True, exist_ok=True)
    report = {"rows": args.rows, "source": "pg", "target_db": TARGET_DB,
              "scope": "one source cluster, one narrow table; not 200 instances"}
    try:
        run(["docker", "inspect", IMAGE])
        run(["docker", "network", "inspect", NETWORK])
        target_psql(f"DROP DATABASE IF EXISTS {TARGET_DB} WITH (FORCE);"
                    f"CREATE DATABASE {TARGET_DB} OWNER {USER};")
        report["seed"] = host_seed(args.rows)
        command = [
            "docker", "run", "--rm", "--name", CONTAINER,
            "--network", NETWORK, "--no-healthcheck", "--memory", "3g", "--cpus", "2",
            "-v", f"{ROOT / 'backend'}:/work:ro",
            "-v", f"{SCRIPT}:/tmp/scale.py:ro",
            *(["-v", f"{JAR}:/opt/debezium/lib/replica-smt.jar:ro"]
              if JAR is not None else []),
            "-e", "PYTHONPATH=/work", "-e", "PYTHONUNBUFFERED=1",
            "-e", "ENVIRONMENT=test",
            "-e", "DATABASE_URL=postgresql+psycopg2://replica_fixture:"
                  f"replica_fixture@target-pg:5432/{TARGET_DB}",
            "-e", "REPLICA_STATE_ROOT=/tmp/rs/state",
            "-e", "REPLICA_RUN_ROOT=/tmp/rs/run",
            "-e", "REPLICA_HEARTBEAT_FILE=/tmp/rs/heartbeat",
            "-e", "CDC_CHILD_JAVA_OPTS=-Xms64m -Xmx256m "
                  "-XX:MaxDirectMemorySize=64m -XX:ActiveProcessorCount=2 "
                  "-Djava.awt.headless=true",
            "-w", "/tmp", "--entrypoint", "python3", IMAGE,
            "/tmp/scale.py", "--mode", "driver", "--rows", str(args.rows),
            "--timeout", str(args.timeout),
        ]
        log_path = ART / f"scale-{args.rows}-driver.log"
        with open(log_path, "w", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            watchdog = threading.Timer(
                args.timeout,
                lambda: (log.write("[host] watchdog: killing scale driver\n"),
                         process.kill()))
            watchdog.daemon = True
            watchdog.start()
            result = None
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    if line.startswith(MARKER):
                        result = json.loads(line[len(MARKER):])
                process.wait()
            finally:
                watchdog.cancel()
        if result is None:
            raise RuntimeError(f"driver produced no {MARKER}; see {log_path}")
        report.update(result)
        report["driver_exit"] = process.returncode
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        run(["docker", "rm", "-f", CONTAINER], check=False)
        try:
            capture_ids = target_psql(
                "SELECT id FROM v2_integrated_replica_captures;",
                database=TARGET_DB).splitlines()
        except Exception:  # noqa: BLE001
            capture_ids = []
        for capture_id in capture_ids:
            hex_id = capture_id.replace("-", "")
            psql(f"SELECT pg_terminate_backend(active_pid) FROM "
                 f"pg_replication_slots WHERE slot_name='ir_{hex_id}';"
                 f"SELECT pg_drop_replication_slot('ir_{hex_id}') WHERE EXISTS "
                 f"(SELECT 1 FROM pg_replication_slots WHERE slot_name="
                 f"'ir_{hex_id}'); DROP PUBLICATION IF EXISTS \"irpub_{hex_id}\";",
                 check=False)
        psql("DROP SCHEMA IF EXISTS scale CASCADE;")
    ok = bool(report.get("pass"))
    (ART / f"scale-{args.rows}-result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def driver_main(args) -> int:
    import logging
    from types import SimpleNamespace

    import sqlalchemy as sa
    from fastapi import HTTPException

    logging.basicConfig(level="INFO")
    from app.model_registry import import_all_models
    from app.shared.database import Base, SessionLocal, engine

    import_all_models()
    Base.metadata.create_all(bind=engine)
    from app.auth.models import User
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.integrated import native, service
    from app.data_channel.integrated import replica_control as control
    from app.data_channel.integrated import replica_store as store
    from app.data_channel.integrated.models import IntegratedAsset
    from app.data_channel.integrated.replica_executor import ReplicaSupervisor
    from app.data_channel.integrated.replica_models import (
        IntegratedReplica, IntegratedReplicaRun)
    from app.data_channel.integrated.replica_receiver import (
        create_replica_receiver)
    from app.data_channel.query import providers
    from app.data_channel.query.contracts import Binding, QueryLimits

    src = sa.create_engine(
        "postgresql+psycopg2://replica_fixture:replica_fixture@"
        "source-pg:5432/replica_source", poolclass=sa.pool.NullPool)
    supervisor = ReplicaSupervisor(
        session_factory=SessionLocal,
        receiver=create_replica_receiver(session_factory=SessionLocal),
        state_root="/tmp/rs/state", run_root="/tmp/rs/run",
        heartbeat_file="/tmp/rs/heartbeat", max_captures=2)
    started = time.monotonic()
    report = {"rows": args.rows, "progress": [], "pass": False}
    try:
        with SessionLocal() as db:
            db.add(User(id="scale-admin", username="scale_admin",
                        email="scale@example.test", password_hash="fixture",
                        role="admin", is_active=True))
            db.add(Connection(id="scale-src", name="scale source",
                              kind="postgres", status="active",
                              config={"user": USER, "password": USER,
                                      "host": "source-pg", "port": 5432,
                                      "database": "replica_source"},
                              created_by="scale-admin"))
            with src.connect() as conn:
                _, schema, fingerprint = native.reflect(
                    conn, SimpleNamespace(schema_name="scale",
                                          table_name="scale_rows"))
            db.add(Dataset(id="ds-scale", name="scale rows", kind="integrated",
                           source_connection_id="scale-src",
                           source_resource="scale.scale_rows",
                           schema_json=schema))
            db.add(IntegratedAsset(
                id="asset-scale", dataset_id="ds-scale",
                connection_id="scale-src", name="scale rows",
                schema_name="scale", table_name="scale_rows",
                schema_json=schema, schema_fingerprint=fingerprint,
                created_by="scale-admin", writable=False))
            db.commit()

        milestones = [10000]
        next_milestone = milestones[0]
        last_target = 0
        rows_seen = 0
        while time.monotonic() - started < args.timeout:
            supervisor.tick()
            with SessionLocal() as db:
                run_row = db.scalar(sa.select(IntegratedReplicaRun).where(
                    IntegratedReplicaRun.asset_id == "asset-scale"))
                replica = db.get(IntegratedReplica, "asset-scale")
                rows_seen = int(run_row.row_count or 0) if run_row else 0
                if rows_seen >= next_milestone:
                    physical = store.replica_table_name(run_row.id) \
                        if run_row else None
                    stats = {}
                    if physical and (
                            rows_seen <= 100000
                            or rows_seen % 1000000 == 0
                            or rows_seen >= args.rows):
                        values = db.execute(sa.text(
                            f"SELECT count(*), coalesce(sum(val),0), min(id),"
                            f" max(id) FROM {physical}")).one()
                        stats = {"count": values[0], "sum": values[1],
                                 "min": values[2], "max": values[3]}
                    size = db.execute(sa.text(
                        "SELECT pg_total_relation_size(:t)"),
                        {"t": physical}).scalar() if physical else None
                    entry = {"rows": rows_seen,
                             "elapsed_sec": round(time.monotonic() - started, 2),
                             "target_stats": stats, "table_bytes": size}
                    report["progress"].append(entry)
                    print(f"@@PROGRESS@@ rows={rows_seen} "
                          f"elapsed={entry['elapsed_sec']}s "
                          f"target={stats or size}", flush=True)
                    while next_milestone <= rows_seen:
                        next_milestone += 10000 if next_milestone < 100000 \
                            else 100000
                if run_row is not None and run_row.status == "active" \
                        and replica is not None and replica.status in {
                            "ready", "stale"} and rows_seen >= args.rows:
                    break
            time.sleep(0.5)
        else:
            raise AssertionError(f"snapshot timeout rows={rows_seen}")
        report["snapshot_sec"] = round(time.monotonic() - started, 2)

        # Authoritative source-vs-replica streaming row-by-row comparison.
        compare_started = time.monotonic()
        compared = mismatches = missing = extra = 0
        with src.connect().execution_options(stream_results=True) as sconn:
            source_rows = sconn.execution_options(stream_results=True).execute(
                sa.text("SELECT id, val FROM scale.scale_rows ORDER BY id"))
            with engine.connect() as tconn:
                replica_iter = (
                    row for batch in store.read_batches(
                        tconn, schema, batch_size=1000) for row in batch)
                for source_row in source_rows:
                    try:
                        replica_row = next(replica_iter)
                    except StopIteration:
                        missing += 1
                        break
                    compared += 1
                    if (source_row[0] != replica_row["id"]
                            or source_row[1] != replica_row["val"]):
                        mismatches += 1
                for _ in replica_iter:
                    extra += 1
        report["source_vs_replica"] = {
            "compared": compared, "mismatches": mismatches,
            "missing": missing, "extra": extra,
            "seconds": round(time.monotonic() - compare_started, 2)}
        if compared != args.rows or mismatches or missing or extra:
            raise AssertionError(
                f"source-vs-replica mismatch: {report['source_vs_replica']}")

        # Full-column streaming verification through the production store.
        verify_started = time.monotonic()
        with SessionLocal() as db:
            run_row = db.scalar(sa.select(IntegratedReplicaRun).where(
                IntegratedReplicaRun.asset_id == "asset-scale"))
            schema = store.build_replica_schema(
                run_row.id, run_row.schema_json,
                schema_fingerprint=run_row.schema_fingerprint)
        count = total = 0
        with engine.connect() as conn:
            for batch in store.read_batches(conn, schema, batch_size=1000):
                for row in batch:
                    count += 1
                    total += row["val"]
                    if row["val"] != row["id"] % 1000:
                        raise AssertionError(
                            f"row {row['id']} val mismatch: {row['val']}")
        report["verify"] = {"count": count, "sum": total,
                            "seconds": round(time.monotonic() - verify_started, 2)}
        if count != args.rows or total != sum(i % 1000 for i in range(1, args.rows + 1)):
            raise AssertionError("full-column count/sum mismatch")

        # Production local query (providers relation), not candidate SELECT.
        query_started = time.monotonic()
        with SessionLocal() as db:
            with __import__("contextlib").ExitStack() as stack:
                relation = providers.resolve_integrated(
                    db, db.get(User, "scale-admin"),
                    [("src", Binding(kind="integrated", asset_id="asset-scale"))],
                    QueryLimits(), stack)["src"]
                values = db.execute(sa.select(
                    sa.func.count(), sa.func.coalesce(sa.func.sum(
                        relation.expressions["val"]), 0))).one()
        report["local_query"] = {"count": values[0], "sum": values[1],
                                 "seconds": round(time.monotonic() - query_started, 2)}
        preview = service.preview_rows(db, db.get(User, "scale-admin"),
                                       "asset-scale", 1000)
        if preview["source_state"]["replica_status"] not in {"ready", "stale"}:
            raise AssertionError("local preview not readable")

        # Post-snapshot CDC: update + delete in the same stream.
        cdc_started = time.monotonic()
        with src.begin() as conn:
            conn.execute(sa.text("UPDATE scale.scale_rows SET val=7 "
                                 "WHERE id <= 1000"))
            conn.execute(sa.text("DELETE FROM scale.scale_rows "
                                 "WHERE id > :n"), {"n": args.rows - 1000})
        expected_count = args.rows - 1000
        expected_sum = sum(i % 1000 for i in range(1, args.rows + 1)) \
            - sum((i % 1000) for i in range(1, 1001)) + 1000 * 7 \
            - sum((i % 1000) for i in range(args.rows - 999, args.rows + 1))
        while time.monotonic() - cdc_started < 600:
            supervisor.tick()
            with SessionLocal() as db:
                with __import__("contextlib").ExitStack() as stack:
                    relation = providers.resolve_integrated(
                        db, db.get(User, "scale-admin"),
                        [("src", Binding(kind="integrated",
                                         asset_id="asset-scale"))],
                        QueryLimits(), stack)["src"]
                    values = db.execute(sa.select(
                        sa.func.count(), sa.func.coalesce(sa.func.sum(
                            relation.expressions["val"]), 0))).one()
            if values[0] == expected_count and values[1] == expected_sum:
                break
            time.sleep(0.5)
        else:
            raise AssertionError("post-snapshot CDC did not converge")
        report["cdc"] = {"count": values[0], "sum": values[1],
                         "seconds": round(time.monotonic() - cdc_started, 2)}
        report["pass"] = True
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        try:
            supervisor.shutdown()
            if getattr(supervisor, "lease", None) is not None:
                supervisor.lease.reset()
        except Exception:  # noqa: BLE001
            pass
        try:
            src.dispose()
        except Exception:  # noqa: BLE001
            pass
    print(MARKER + json.dumps(report, default=str))
    return 0 if report.get("pass") else 1


def main() -> int:
    global IMAGE, JAR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["host", "driver"], default="host")
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--jar", default=str(JAR),
                        help="SMT jar path, or 'builtin' to use the image jar")
    args = parser.parse_args()
    IMAGE = args.image
    JAR = None if str(args.jar).lower() in {"builtin", "none", ""} else (
        Path(args.jar) if Path(args.jar).is_absolute() else ROOT / args.jar)
    if not 1 <= args.rows <= 50000000:
        parser.error("--rows must be 1..50000000")
    if not 600 <= args.timeout <= 21600:
        parser.error("--timeout must be within 600..21600 seconds")
    if args.mode == "driver":
        return driver_main(args)
    return host_main(args)


if __name__ == "__main__":
    sys.exit(main())
