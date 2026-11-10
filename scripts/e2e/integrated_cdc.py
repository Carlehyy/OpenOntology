#!/usr/bin/env python3
"""Integrated CDC canary: real MySQL/PostgreSQL -> Debezium -> platform DB
facts -> Sentinel -> internal notification, in one isolated compose stack.

Ownership and honesty rules (AGENTS.md §4/§5/§6):

- Only this driver's own compose project ``cdc-canary-20261110`` is touched,
  label-verified through the shared safety envelope of integrated_stack.py
  (no dotenv, no pulls, loopback-only published ports, ownership ledger).
  Other projects' containers - running or exited - are never inspected
  beyond ``docker image inspect`` of read-only image ids, never stopped.
- Subcommands that cannot run honestly (executor image not built, peer
  runtime not landed) exit non-zero with an explicit ``blocked`` reason;
  they never simulate Debezium, the receiver, the executor or notifications.
- Evidence (JSON reports) is written under ``.artifacts/cdc-validation/``
  and never committed to Git.

Subcommands:

    plan            print services/ports/resources for coordination (no Docker)
    preflight       verify ports/images/disk for a phase (migration|acceptance)
    up/down/status  start/stop/inspect the phase's own services
    seed-sources    deterministic synthetic inventory tables in both sources
    migrate-probe   real PG migration checks (fresh head, prior 0143 -> head,
                    0144 down/up with seeded facts, CDC-owned model drift)
    build-executor  build the peer CDC executor image with the pinned inputs
    scenarios       list the acceptance scenario registry with honest status
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:  # normal script execution: scripts/e2e is on sys.path
    import integrated_stack as base
except ImportError:  # importlib loading from tests without package context
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "integrated_stack", Path(__file__).resolve().with_name("integrated_stack.py"))
    base = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(base)

ROOT = base.ROOT

# ── isolated stack identity ────────────────────────────────────────────────
PROJECT = "cdc-canary-20261110"
COMPOSE = ROOT / "docker-compose.cdc-e2e.yml"
ART = ROOT / ".artifacts" / "cdc-validation"
STATE = ART / "resources.json"

# Loopback-only published ports. Every other service stays compose-internal:
# the backend, dbx and the CDC executor reach sources by service name, and
# the Debezium HTTP receiver is loopback inside the executor container only.
PORTS = {
    "db": [45532],           # platform metadata DB (host-side migration probe)
    "backend": [48180],      # backend API (front-door acceptance)
    "source-pg": [], "source-mysql": [], "redis": [], "nats": [],
    "neo4j": [], "minio": [], "dbx": [], "cdc-executor": [],
}
IMAGES = {
    "db": "postgres:16-alpine",
    "source-pg": "postgres:16-alpine",
    "source-mysql": "mysql:8.4",
    "redis": "redis:7-alpine",
    "nats": "nats:alpine",
    "neo4j": "neo4j:5.20-community",
    "minio": "minio/minio:latest",
    "dbx": "docker.cnb.cool/dbxio.com/dbx@sha256:b661f7e35deed06d9611dcda79c44ffcc8e4fd0f44308028ea77f7f18b36638c",
    "backend": "oo-ontology-mcp-backend:acceptance",
    "cdc-executor": "openontology-cdc-executor:canary",
}
# The pinned Debezium server distribution is embedded in the executor image
# (peer Dockerfile.cdc); the upstream image is never run directly here.
DEBEZIUM_SERVER_IMAGE = "quay.io/debezium/server:3.7.1.Final"
EXECUTOR_DOCKERFILE = ROOT / "backend" / "Dockerfile.cdc"
EXECUTOR_BUILD_COMMAND = [
    "docker", "build", "-f", "backend/Dockerfile.cdc",
    "--build-arg", "PYTHON_BASE_IMAGE=oo-ontology-mcp-backend:acceptance",
    "--build-arg", "CDC_PREBUILT_BACKEND=1",
    "-t", "openontology-cdc-executor:canary", "backend",
]

PHASES = {
    # migration probe needs only the metadata database (bounded resources:
    # never reserve the full stack for a DB-only check).
    "migration": ["db"],
    # acceptance runs the whole chain; dbx is required by the front-door
    # asset registration (registration fails closed without pinned dbx).
    "acceptance": ["db", "source-pg", "source-mysql", "redis", "nats",
                   "neo4j", "minio", "dbx", "backend", "cdc-executor"],
}

# ── frozen runtime contracts this canary asserts (owner: runtime peers) ────
INGEST_STATUSES = ("captured", "duplicate", "stale", "heartbeat", "control")
CAPTURE_STATUSES = ("captured", "duplicate", "stale", "capacity_exceeded")
SOURCE_HEALTHY_STATUSES = ("active", "starting")   # healthy only per status GET
SOURCE_UNHEALTHY_EXPLICIT = ("error", "gap", "paused", "stopped")
CHANGE_KINDS = ("created", "updated", "deleted")
SNAPSHOT_MODE = "no_data"
REPLAY_IDENTITY_RULE = (
    "source_event_id derives from connector + immutable source coordinates "
    "(mysql file/pos/row; postgres lsn + transaction.id + "
    "data_collection_order) + op + primary key - never timestamp or content")
EVENT_GROUP_RULE = (
    "event_group_id is unique per source row event + mapping; txn/gtid never "
    "group distinct row events")

MIGRATION_PRIOR_REVISION = "0143_operations_menu_keys"
CDC_REVISION = "0144_integrated_cdc"
PLATFORM_DSN = ("postgresql+psycopg2://cdc_e2e:cdc_e2e_fixture"
                "@127.0.0.1:45532/cdc_platform")
FIXTURE_SECRET = "cdc_canary_public_fixture_secret_not_production"

# ── deterministic synthetic inventory fixture (no real data) ───────────────
INVENTORY_TABLE = "inventory"
INVENTORY_COLUMNS = ("source_id", "balance", "source_status", "observed_at")
INVENTORY_ROWS = (
    # item-01 separate committed 100->0->100 (same fixed business timestamp)
    ("item-01", 100, "ready", "2026-01-01 00:00:00"),
    # item-02 one-row multi-field update -> exactly one notification
    ("item-02", 100, "pending", "2026-01-01 00:00:00"),
    # item-03 rolled-back transaction must produce no event
    ("item-03", 100, "ready", "2026-01-01 00:00:00"),
    # item-04 intermediate updates inside one transaction (record honestly)
    ("item-04", 100, "ready", "2026-01-01 00:00:00"),
    # item-05 committed while the executor is stopped (offset resume)
    ("item-05", 100, "ready", "2026-01-01 00:00:00"),
    # item-06 latency series commits
    ("item-06", 100, "ready", "2026-01-01 00:00:00"),
    # item-07 MySQL mirror of the separate-commits case
    ("item-07", 100, "ready", "2026-01-01 00:00:00"),
    # item-08 concurrent-transaction commit-order case (row LSN vs commit)
    ("item-08", 100, "ready", "2026-01-01 00:00:00"),
)

SCENARIOS = (
    # (id, description, needs)
    ("migrate_fresh_head", "alembic upgrade head on an empty physical PG", "migration phase"),
    ("migrate_prior_0143_to_head", "prior 0143 database upgrades through 0144 (and any follow-ups) with seeded local facts preserved", "migration phase"),
    ("migrate_0144_down_up", "0144 downgrade removes exactly external rows, organic facts survive, upgrade re-applies", "migration phase"),
    ("migrate_no_model_drift", "CDC-owned tables/columns/FKs/partial indexes match the current models exactly", "migration phase"),
    ("separate_commits_same_timestamp", "two committed 100->0->100 changes with an identical business timestamp stay two events; the transient zero still notifies", "acceptance phase"),
    ("multi_field_one_row_one_notification", "one row update touching several mapped fields yields exactly one notification", "acceptance phase"),
    ("rollback_no_event", "BEGIN/UPDATE/ROLLBACK produces no event, no notification", "acceptance phase"),
    ("one_transaction_intermediate_honest", "intermediate updates inside one transaction: connector output recorded exactly as observed", "acceptance phase"),
    ("duplicate_delivery_exactly_one_notification", "at-least-once redelivery after an offset flush miss yields duplicate acks and still exactly one notification", "acceptance phase"),
    ("restart_resume_offsets", "executor restart with an ACTIVE subscription keeps slot/offsets; a downtime commit is delivered exactly once", "acceptance phase"),
    ("backend_restart_resume", "backend API restart does not duplicate or lose notifications", "acceptance phase"),
    ("source_gap_explicit_unhealthy", "dropped slot/purged binlog surfaces status gap (explicit unhealthy), never silent snapshot", "acceptance phase"),
    ("ddl_drift_explicit_unhealthy", "dropping a mapped column surfaces SCHEMA_DRIFT explicitly", "acceptance phase"),
    ("no_object_instance_materialized", "external events create zero ObjectInstance/fo_* rows for the ontology", "acceptance phase"),
    ("latency_p95_commit_to_notification", "commit->notification P95 <= 5s on the active path", "acceptance phase"),
    ("pg_commit_order_no_loss", "concurrent PG transactions committing in reverse row-LSN order lose no event (no unsafe row-LSN high-water dedupe)", "acceptance phase"),
    ("cross_source_late_commit_no_skip", "a late-committing ingest transaction cannot be skipped by the pattern pipeline", "acceptance phase"),
    ("last_mapping_removal_stops_and_cleans", "removing the last released remote mapping stops the child and drops the owned slot/publication (cleanup confirmed)", "acceptance phase"),
    ("decommission_active_structured_409", "asset decommission while subscriptions are active is a structured 409, not an FK 500", "acceptance phase"),
    ("republish_after_dormant_new_generation", "new publish after a stopped+cleaned source starts an explicit new generation with no_data coverage", "acceptance phase"),
    ("credential_rotation_resumes_offset", "credential-only rotation keeps the offset and resumes the same coverage", "acceptance phase"),
    ("repoint_rejected_until_cleanup", "physical source repoint is rejected (structured 409) until stop+slot cleanup is confirmed", "acceptance phase"),
    ("mysql_same_contract", "the MySQL source passes the same event/notification/idempotency contract", "acceptance phase"),
)


# ── plan / preflight / up / down / status ──────────────────────────────────
def resources() -> dict[str, Any]:
    limits = {
        "db": "512m/1.0", "source-pg": "384m/1.0", "source-mysql": "512m/1.0",
        "redis": "96m/0.25", "nats": "96m/0.25", "neo4j": "1024m/1.0",
        "minio": "256m/0.5", "dbx": "768m/1.0", "backend": "1280m/2.0",
        "cdc-executor": "1408m/2.0",
    }
    return {
        "project": PROJECT,
        "compose": str(COMPOSE.relative_to(ROOT)),
        "published_loopback_ports": {k: v for k, v in PORTS.items() if v},
        "internal_only_services": sorted(k for k, v in PORTS.items() if not v),
        "memory_cpu_limits": limits,
        "total_memory_limit_bytes": 6208 * 1024 * 1024,
        "docker": {"memory_bytes": 8214757376, "cpus": 18},
        "phases": PHASES,
        "pull_policy": "never", "env_file": None,
        "credentials": "public deterministic fixtures only (compose file)",
        "pinned": {"debezium_server": DEBEZIUM_SERVER_IMAGE,
                   "dbx_digest": IMAGES["dbx"].split("@")[1]},
    }


def phase_services(phase: str) -> list[str]:
    if phase not in PHASES:
        raise SystemExit(f"unknown phase {phase!r}; choose one of {sorted(PHASES)}")
    return list(PHASES[phase])


def phase_profiles(phase: str) -> tuple[str, ...]:
    return ("cdc",) if "cdc-executor" in PHASES[phase] else ()


def pending_services(phase: str) -> list[str]:
    """Phase services not already running in this project (own ports are
    legitimately bound by our own running containers, never 'occupied')."""
    running = {item["service"] for item in base.owned_containers(PROJECT)
               if item["running"]}
    return [service for service in phase_services(phase) if service not in running]


def cmd_preflight(phase: str) -> dict[str, Any]:
    services = pending_services(phase)
    report = base.preflight(
        services, project=PROJECT, ports=PORTS, images=IMAGES, directory=ART)
    report["phase"] = phase
    report["already_running"] = sorted(set(phase_services(phase)) - set(services))
    report["debezium_runtime"] = {
        "note": "Debezium server runs inside the executor image only",
        "upstream_image_required_directly": False,
    }
    if "cdc-executor" in phase_services(phase):
        report["executor_dockerfile_present"] = EXECUTOR_DOCKERFILE.is_file()
        if not report["executor_dockerfile_present"]:
            report["result"] = "blocked"
            report["blocked_reason"] = (
                f"peer runtime file missing: {EXECUTOR_DOCKERFILE.relative_to(ROOT)}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def cmd_up(phase: str) -> dict[str, Any]:
    services = pending_services(phase)
    if not services:
        return cmd_status()
    result = base.stack_up(
        services, project=PROJECT, ports=PORTS, images=IMAGES,
        compose_file=COMPOSE, profiles=phase_profiles(phase),
        state_path=STATE, directory=ART)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def cmd_down(destroy_data: bool) -> dict[str, Any]:
    result = base.stack_down(
        destroy_data, project=PROJECT, state_path=STATE, directory=ART,
        profiles=("cdc",), compose_file=COMPOSE)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def cmd_status() -> dict[str, Any]:
    result = base.update_state(PROJECT, ART)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


# ── deterministic synthetic source seeding ─────────────────────────────────
def _source_sql(service: str, statement: str) -> str:
    cid = base.require_owned(service, PROJECT)
    if service == "source-pg":
        return base.run(["docker", "exec", "-i", cid, "psql", "-X", "-q", "-A", "-t",
                         "-v", "ON_ERROR_STOP=1", "-U", "cdc_e2e", "-d", "cdc_source"],
                        text=statement)
    return base.run(["docker", "exec", "-i", "-e", "MYSQL_PWD=cdc_e2e_fixture", cid,
                     "mysql", "--default-character-set=utf8mb4", "--batch", "--raw",
                     "--skip-column-names", "-u", "cdc_e2e", "cdc_source"],
                    text=statement)


def cmd_seed_sources() -> dict[str, Any]:
    """Create the deterministic inventory fixture in both real sources.

    PG sets REPLICA IDENTITY FULL so Debezium before-images carry every
    mapped column (old/new facts stay honest); MySQL runs ROW/FULL binlog
    from the compose command. Seeding happens BEFORE activation so no
    snapshot/no_data coverage ever includes these rows as history.
    """
    report: dict[str, Any] = {"rows": len(INVENTORY_ROWS), "sources": {}}
    expected_aggregate = "|".join(str(value) for value in (
        len(INVENTORY_ROWS), len({row[0] for row in INVENTORY_ROWS}),
        sum(row[1] for row in INVENTORY_ROWS)))

    pg_ddl = f"""
DROP TABLE IF EXISTS {INVENTORY_TABLE};
CREATE TABLE {INVENTORY_TABLE} (
    source_id varchar(64) PRIMARY KEY,
    balance integer,
    source_status varchar(32),
    observed_at timestamp with time zone NOT NULL
);
ALTER TABLE {INVENTORY_TABLE} REPLICA IDENTITY FULL;
"""
    for row in INVENTORY_ROWS:
        pg_ddl += ("INSERT INTO inventory (source_id, balance, source_status, observed_at) "
                   f"VALUES ('{row[0]}', {row[1]}, '{row[2]}', '{row[3]}+00');\n")
    _source_sql("source-pg", pg_ddl)
    counts = _source_sql(
        "source-pg", "SELECT count(*), count(DISTINCT source_id), "
        "coalesce(sum(balance),0) FROM inventory;").strip()
    ident = _source_sql(
        "source-pg", "SELECT relreplident FROM pg_class c JOIN pg_namespace n "
        "ON n.oid=c.relnamespace WHERE n.nspname='public' AND relname='inventory';").strip()
    wal = _source_sql("source-pg", "SHOW wal_level;").strip()
    report["sources"]["postgres"] = {
        "aggregate": counts, "replica_identity": ident, "wal_level": wal,
        "expected_aggregate": expected_aggregate,
    }
    if wal.lower() != "logical":
        raise RuntimeError("source-pg wal_level must be logical for Debezium")
    if ident != "f":
        raise RuntimeError("inventory must use REPLICA IDENTITY FULL (before images)")

    mysql_ddl = [
        f"DROP TABLE IF EXISTS {INVENTORY_TABLE}",
        f"""CREATE TABLE {INVENTORY_TABLE} (
  source_id varchar(64) NOT NULL PRIMARY KEY,
  balance int NULL,
  source_status varchar(32) NULL,
  observed_at datetime(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ]
    for row in INVENTORY_ROWS:
        mysql_ddl.append(
            "INSERT INTO inventory (source_id, balance, source_status, observed_at) "
            f"VALUES ('{row[0]}', {row[1]}, '{row[2]}', '{row[3]}')")
    _source_sql("source-mysql", ";\n".join(mysql_ddl) + ";")
    counts = _source_sql(
        "source-mysql", "SELECT CONCAT_WS('|', COUNT(*), COUNT(DISTINCT source_id), "
        "COALESCE(SUM(balance),0)) FROM inventory;").strip()
    settings = _source_sql(
        "source-mysql",
        "SELECT CONCAT_WS('|', @@log_bin, @@binlog_format, @@binlog_row_image, "
        "@@gtid_mode);").strip()
    grants = _source_sql("source-mysql", "SHOW GRANTS FOR CURRENT_USER();")
    report["sources"]["mysql"] = {
        "aggregate": counts, "expected_aggregate": expected_aggregate,
        "log_settings": settings, "grants": grants.splitlines(),
    }
    for source, entry in report["sources"].items():
        if entry["aggregate"] != entry["expected_aggregate"]:
            raise RuntimeError(f"{source} inventory seed mismatch: {entry}")
    base.write_report("seed-sources.json", report, ART)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def cmd_build_executor() -> dict[str, Any]:
    if not EXECUTOR_DOCKERFILE.is_file():
        raise RuntimeError(
            f"blocked: peer runtime Dockerfile not landed yet: "
            f"{EXECUTOR_DOCKERFILE.relative_to(ROOT)}")
    started = time.monotonic()
    process = subprocess.run(EXECUTOR_BUILD_COMMAND, cwd=ROOT,
                             env=base.SAFE_ENV | {"COMPOSE_PROJECT_NAME": PROJECT},
                             capture_output=True, text=True)
    report = {
        "command": EXECUTOR_BUILD_COMMAND,
        "exit_code": process.returncode,
        "stdout_tail": process.stdout[-4000:],
        "stderr_tail": process.stderr[-4000:],
        "wall_seconds": round(time.monotonic() - started, 1),
    }
    base.write_report("build-executor.json", report, ART)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if process.returncode:
        raise RuntimeError("executor image build failed; see artifact")
    return report


def cmd_scenarios() -> dict[str, Any]:
    report = {"scenarios": [{"id": i, "description": d, "needs": n,
                             "status": "not_run"} for i, d, n in SCENARIOS]}
    base.write_report("scenarios.json", report, ART)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


# ── migration probe (real physical PG; no app.main import) ─────────────────
def _normalize_default(raw: str | None) -> str:
    if raw is None:
        return ""
    text = str(raw).strip().strip("'\"")
    # '0'::character varying / ('stopped'::character varying) / 0
    for marker in ("::",):
        if marker in text:
            text = text.split(marker)[0].strip("'\"()")
    return text.strip("'\"() ").lower()


def _column_signature(column) -> tuple:
    from sqlalchemy.dialects import postgresql
    compiled = str(column.type.compile(postgresql.dialect())).upper()
    return (column.name, compiled, bool(column.nullable),
            _normalize_default(column.server_default.arg.__str__()
                               if column.server_default is not None else None))


def _db_column_signature(column: dict) -> tuple:
    from sqlalchemy.dialects import postgresql
    default = column.get("default") or column.get("server_default")
    # Reflected generic types only render timezone/length under the PG dialect.
    compiled = str(column["type"].compile(dialect=postgresql.dialect())).upper()
    return (column["name"], compiled,
            bool(column.get("nullable")), _normalize_default(default))


def _cdc_model_tables() -> dict:
    from app.data_channel.integrated.models import (
        IntegratedCdcSource, IntegratedCdcSubscription)
    from app.ontologies.sentinels.models import SentinelEventLog
    return {
        "v2_integrated_cdc_sources": IntegratedCdcSource.__table__,
        "v2_integrated_cdc_subscriptions": IntegratedCdcSubscription.__table__,
        "sentinel_event_log": SentinelEventLog.__table__,
    }


CDC_OWNED_EVENT_LOG_COLUMNS = (
    "source_event_id", "event_group_id", "external_asset_id",
    "external_mapping_id", "event_snapshot",
)


def _minimal_insert(conn, table: str, values: dict) -> None:
    """Type-directed deterministic filler for NOT NULL columns w/o defaults."""
    import sqlalchemy as sa
    inspector = sa.inspect(conn)
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    missing = [name for name, c in columns.items()
               if not c.get("nullable") and not c.get("default")
               and name not in values and name != "id"]
    filler: dict[str, Any] = {}
    for name in missing:
        kind = str(columns[name]["type"]).upper()
        if kind.startswith(("TIMESTAMP", "DATE", "TIME")):
            filler[name] = "now()"
        elif kind.startswith(("INT", "BIGINT", "NUMERIC")):
            filler[name] = "0"
        elif kind.startswith("BOOL"):
            filler[name] = "false"
        elif kind.startswith("JSON"):
            filler[name] = "'{}'::json"
        else:
            limit = columns[name].get("max_length") or getattr(
                columns[name]["type"], "length", None) or 64
            text_value = f"cdc-probe-{name}"[:int(limit)]
            filler[name] = f"'{text_value}'"
    names, placeholders, params = [], [], {}
    for name, value in values.items():
        names.append(name)
        if isinstance(value, sa.TextClause):
            placeholders.append(str(value))  # explicit SQL literal fragment
        else:
            key = f"p{len(params)}"
            params[key] = value
            placeholders.append(f":{key}")
    for name, literal in filler.items():   # filler is always a SQL literal
        names.append(name)
        placeholders.append(literal)
    conn.execute(sa.text(
        f"INSERT INTO {table} ({', '.join(names)}) "
        f"VALUES ({', '.join(placeholders)})"), params)


def migration_drift(engine) -> dict[str, Any]:
    """Compare ONLY CDC-owned schema between physical PG and current models.

    Whole-metadata diffing is intentionally avoided: unrelated in-flight peer
    changes would drown the CDC signal (coordinator instruction).
    """
    import sqlalchemy as sa
    tables = _cdc_model_tables()
    drift: list[dict] = []
    observed: dict[str, Any] = {}
    with engine.connect() as conn:
        inspector = sa.inspect(conn)
        for table, model in tables.items():
            if not inspector.has_table(table):
                drift.append({"table": table, "issue": "table_missing"})
                continue
            actual = {c["name"]: _db_column_signature(c)
                      for c in inspector.get_columns(table)}
            observed[table] = {"columns": sorted(actual)}
            model_columns = {
                c.name: _column_signature(c) for c in model.columns}
            scope = (CDC_OWNED_EVENT_LOG_COLUMNS if table == "sentinel_event_log"
                     else set(model_columns))
            for name in sorted(scope):
                if name not in actual:
                    drift.append({"table": table, "column": name,
                                  "issue": "column_missing_in_db"})
                    continue
                if name not in model_columns:
                    drift.append({"table": table, "column": name,
                                  "issue": "column_missing_in_model"})
                    continue
                expected, seen = model_columns[name], actual[name]
                if expected[1] != seen[1]:
                    drift.append({"table": table, "column": name,
                                  "issue": "type_mismatch",
                                  "model": expected[1], "db": seen[1]})
                if table != "sentinel_event_log":
                    # New CDC tables must match exactly (nullable + default);
                    # sentinel_event_log pre-existing columns are out of scope.
                    if expected[2] != seen[2]:
                        drift.append({"table": table, "column": name,
                                      "issue": "nullable_mismatch",
                                      "model": expected[2], "db": seen[2]})
                    if expected[3] != seen[3]:
                        drift.append({"table": table, "column": name,
                                      "issue": "default_mismatch",
                                      "model": expected[3], "db": seen[3]})
            if table == "sentinel_event_log":
                checks = {c["name"] for c in
                          inspector.get_check_constraints(table)}
                if "ck_sentinel_event_log_external_identity_scope" not in checks:
                    drift.append({"table": table,
                                  "issue": "check_constraint_missing",
                                  "name": "ck_sentinel_event_log_external_identity_scope"})
                indexes = {i["name"]: i for i in inspector.get_indexes(table)}
                for name, unique in (("uq_sentinel_event_log_external", True),
                                     ("ix_sentinel_event_log_external_group", False)):
                    if name not in indexes:
                        drift.append({"table": table, "issue": "index_missing",
                                      "name": name})
                        continue
                    if bool(indexes[name].get("unique")) != unique:
                        drift.append({"table": table, "issue": "index_uniqueness",
                                      "name": name, "expected_unique": unique})
                    predicate = conn.execute(sa.text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = :name"), {"name": name}).scalar_one_or_none()
                    observed[table][name] = predicate
                    if predicate and "WHERE" not in predicate.upper():
                        drift.append({"table": table, "issue": "index_predicate_missing",
                                      "name": name})
                continue
            pk = inspector.get_pk_constraint(table).get("constrained_columns") or []
            expected_pk = [c.name for c in model.primary_key]
            if pk != expected_pk:
                drift.append({"table": table, "issue": "pk_mismatch",
                              "model": expected_pk, "db": pk})
            actual_fks = {
                (tuple(f["constrained_columns"]),
                 f"{f['referred_table']}.{f['referred_columns'][0]}",
                 (f.get("options") or {}).get("ondelete", "NO ACTION").upper())
                for f in inspector.get_foreign_keys(table)}
            expected_fks = {
                (tuple(f.column_keys), f.elements[0].target_fullname,
                 (f.ondelete or "NO ACTION").upper())
                for f in model.constraints if isinstance(f, sa.ForeignKeyConstraint)}
            for difference in expected_fks - actual_fks:
                drift.append({"table": table, "issue": "fk_missing",
                              "expected": list(difference)})
            for difference in actual_fks - expected_fks:
                drift.append({"table": table, "issue": "fk_unexpected",
                              "observed": list(difference)})
            uniques = {tuple(u["column_names"]) for u in
                       inspector.get_unique_constraints(table)}
            expected_uniques = {
                tuple(u.columns.keys()) for u in model.constraints
                if isinstance(u, sa.UniqueConstraint)}
            if expected_uniques != uniques:
                drift.append({"table": table, "issue": "unique_constraint_mismatch",
                              "model": [list(u) for u in expected_uniques],
                              "db": [list(u) for u in uniques]})
            # Named non-unique indexes declared by the model for this table.
            indexes = {i["name"]: i["column_names"] for i in
                       inspector.get_indexes(table) if not i.get("unique")}
            for index in model.indexes:
                if index.unique:
                    continue
                columns = [c.name for c in index.columns]
                if indexes.get(index.name) != columns:
                    drift.append({"table": table, "issue": "index_mismatch",
                                  "name": index.name, "expected": columns,
                                  "db": indexes.get(index.name)})
    return {"drift": drift, "observed": observed,
            "status": "match" if not drift else "model_db_drift"}


def cmd_migrate_probe() -> dict[str, Any]:
    base.require_owned("db", PROJECT)
    if (ROOT / "backend/.env").exists() or (ROOT / ".env").exists():
        raise RuntimeError("sanitized worktree expected: no dotenv files")
    sys.path.insert(0, str(ROOT / "backend"))
    import os
    import re
    import uuid

    import sqlalchemy as sa
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    # Sanitized fixture environment for the whole probe lifetime: revision
    # files import app.shared.config lazily at arbitrary points, so the env
    # must be stable (never restored mid-probe). Public fixture values only.
    os.environ.update({
        "ENVIRONMENT": "test",
        "DATABASE_URL": "postgresql://cdc_e2e:cdc_e2e_fixture@127.0.0.1:45532/cdc_platform",
        "REDIS_URL": "redis://:cdc_e2e_fixture@127.0.0.1:46489/0",
        "SECRET_KEY": FIXTURE_SECRET,
    })

    admin = sa.create_engine(PLATFORM_DSN, isolation_level="AUTOCOMMIT",
                             poolclass=NullPool, hide_parameters=True)
    started = time.monotonic()
    report: dict[str, Any] = {
        "full_e2e_pass": False,
        "kind": "physical_pg_migration_probe_not_full_e2e",
        "cases": {},
        "not_verified": ["Debezium streaming", "receiver/ingest", "Sentinel",
                         "notifications", "executor lifecycle"],
    }
    databases: list[str] = []
    engine = None
    try:
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        def fresh_config(database: str) -> Config:
            url = make_url(PLATFORM_DSN).set(database=database)
            config = Config(str(ROOT / "backend/alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "backend/alembic"))
            config.set_main_option(
                "sqlalchemy.url",
                url.render_as_string(hide_password=False).replace("%", "%%"))
            return config

        heads = ScriptDirectory.from_config(
            fresh_config("cdc_platform")).get_heads()
        assert len(heads) == 1, f"expected one Alembic head, observed {heads}"
        head = heads[0]
        report["cases"]["single_head"] = head

        def create(name: str) -> str:
            full = "cdc_probe_" + uuid.uuid4().hex[:12]
            assert re.fullmatch(r"cdc_probe_[0-9a-f]{12}", full)
            with admin.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{full}"')
                connection.exec_driver_sql(
                    f"COMMENT ON DATABASE \"{full}\" IS 'cdc-canary:{name}'")
            databases.append(full)
            return full

        # Case 1: fresh empty database upgrades to head.
        fresh = create("fresh")
        command.upgrade(fresh_config(fresh), "head")
        report["cases"]["fresh_upgrade_head"] = True

        # Case 2: prior 0143 database with seeded local facts upgrades to head.
        prior = create("prior")
        config = fresh_config(prior)
        command.upgrade(config, MIGRATION_PRIOR_REVISION)
        prior_engine = sa.create_engine(
            make_url(PLATFORM_DSN).set(database=prior), poolclass=NullPool,
            hide_parameters=True)
        with prior_engine.begin() as conn:
            _minimal_insert(conn, "users", {
                "id": "cdc-probe-user", "username": "cdc-probe-user",
                "email": "cdc-probe@example.invalid",
                "password_hash": "not-used", "role": "admin"})
            _minimal_insert(conn, "ontology_projects", {
                "id": "cdc-probe-ontology", "name": "CDC migration probe",
                "domain": "synthetic", "created_by": "cdc-probe-user"})
            for number in range(3):
                _minimal_insert(conn, "sentinel_event_log", {
                    "ontology_id": "cdc-probe-ontology",
                    "object_type_id": "cdc-probe-type",
                    "instance_id": f"cdc-probe-instance-{number}",
                    "change_kind": "updated", "key": "balance",
                    "old_value": sa.text("'100'::json"),
                    "new_value": sa.text("'0'::json"),
                    "source": "organic", "cascade_depth": 0})
        with prior_engine.connect() as conn:
            seeded = conn.execute(sa.text(
                "SELECT count(*) FROM sentinel_event_log")).scalar_one()
        assert seeded == 3, f"expected 3 seeded organic facts, got {seeded}"
        command.upgrade(config, "head")
        with prior_engine.connect() as conn:
            after = conn.execute(sa.text(
                "SELECT count(*), count(source_event_id), "
                "count(DISTINCT change_kind), min(key), max(key) "
                "FROM sentinel_event_log")).one()
            assert list(after) == [3, 0, 1, "balance", "balance"], after
        report["cases"]["prior_0143_upgrade_head"] = True
        report["cases"]["seeded_organic_facts_preserved"] = True

        # Case 3: external facts at head; 0144 downgrade deletes exactly the
        # external rows; upgrade re-applies and the surviving organic rows stay.
        with prior_engine.begin() as conn:
            _minimal_insert(conn, "sentinel_event_log", {
                "ontology_id": "cdc-probe-ontology",
                "ontology_release_id": "cdc-probe-release",
                "object_type_id": "cdc-probe-type",
                "instance_id": "cdc-probe-instance-remote",
                "source_event_id": "cdc-probe-source-event",
                "event_group_id": "cdc-probe-group",
                "external_asset_id": "cdc-probe-asset",
                "external_mapping_id": "cdc-probe-mapping",
                "event_snapshot": sa.text("'{}'::json"),
                "change_kind": "updated", "key": "quantity",
                "old_value": sa.text("'100'::json"),
                "new_value": sa.text("'0'::json"),
                "source": "external", "cascade_depth": 0})
        command.downgrade(config, MIGRATION_PRIOR_REVISION)
        with prior_engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT count(*) FROM sentinel_event_log")).scalar_one()
            assert rows == 3, f"downgrade must leave exactly the 3 organic rows: {rows}"
        report["cases"]["downgrade_0144_removes_exactly_external_rows"] = True
        command.upgrade(config, "head")
        with prior_engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT count(*) FROM sentinel_event_log")).scalar_one()
            assert rows == 3, f"upgrade re-applied; external seed must not resurrect: {rows}"
        report["cases"]["reupgrade_head"] = True
        prior_engine.dispose()

        # Case 4: CDC-owned model drift on the prior->head physical database.
        engine = sa.create_engine(
            make_url(PLATFORM_DSN).set(database=prior), poolclass=NullPool,
            hide_parameters=True)
        drift = migration_drift(engine)
        report["cases"]["model_drift"] = drift["status"]
        report["drift_detail"] = drift["drift"]
        report["drift_observed"] = drift["observed"]
        report["status"] = ("passed_migration_probe_not_full_e2e"
                            if not drift["drift"] else "model_db_drift")
    except Exception as error:  # noqa: BLE001 - report the exact blocker
        import traceback
        traceback.print_exc()
        report["status"] = "failed"
        report["error_class"] = type(error).__name__
        report["error"] = str(error)[:500]
    finally:
        if engine is not None:
            engine.dispose()
        for name in databases:
            with admin.connect() as connection:
                observed = connection.execute(sa.text(
                    "SELECT shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname=:n"), {"n": name}).scalar()
                if observed is None or not str(observed).startswith("cdc-canary:"):
                    raise RuntimeError(f"ownership marker missing for {name}; refuse cleanup")
                connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        report["databases_cleaned"] = databases
        admin.dispose()
        report["wall_seconds"] = round(time.monotonic() - started, 1)
        ART.mkdir(parents=True, exist_ok=True)
        (ART / "migration-probe.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return report


def cmd_platform_migrate() -> dict[str, Any]:
    """Alembic-upgrade the canary platform DB from the host venv (no app.main)."""
    base.require_owned("db", PROJECT)
    sys.path.insert(0, str(ROOT / "backend"))
    import os

    os.environ.update({
        "ENVIRONMENT": "test",
        "DATABASE_URL": "postgresql://cdc_e2e:cdc_e2e_fixture@127.0.0.1:45532/cdc_platform",
        "REDIS_URL": "redis://:cdc_e2e_fixture@127.0.0.1:46489/0",
        "SECRET_KEY": FIXTURE_SECRET,
    })
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/alembic"))
    config.set_main_option(
        "sqlalchemy.url", PLATFORM_DSN.replace("postgresql+psycopg2", "postgresql").replace("%", "%%"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, heads
    command.upgrade(config, "head")
    import sqlalchemy as sa
    engine = sa.create_engine(PLATFORM_DSN, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            version = conn.execute(sa.text(
                "SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()
    report = {"upgrade_to": version, "head": heads[0]}
    base.write_report("platform-migrate.json", report, ART)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("phase", choices=sorted(PHASES))
    up = sub.add_parser("up")
    up.add_argument("phase", choices=sorted(PHASES))
    down = sub.add_parser("down")
    down.add_argument("--destroy-data", action="store_true")
    sub.add_parser("status")
    sub.add_parser("seed-sources")
    sub.add_parser("platform-migrate")
    sub.add_parser("migrate-probe")
    sub.add_parser("build-executor")
    sub.add_parser("scenarios")
    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps(resources(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "preflight":
        return 2 if cmd_preflight(args.phase)["result"] != "pass" else 0
    if args.command == "up":
        cmd_up(args.phase)
        return 0
    if args.command == "down":
        cmd_down(args.destroy_data)
        return 0
    if args.command == "status":
        cmd_status()
        return 0
    if args.command == "seed-sources":
        cmd_seed_sources()
        return 0
    if args.command == "platform-migrate":
        cmd_platform_migrate()
        return 0
    if args.command == "build-executor":
        cmd_build_executor()
        return 0
    if args.command == "scenarios":
        cmd_scenarios()
        return 0
    if args.command == "migrate-probe":
        result = cmd_migrate_probe()
        return 0 if result.get("status") == "passed_migration_probe_not_full_e2e" else 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
