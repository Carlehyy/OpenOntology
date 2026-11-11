#!/usr/bin/env python3
"""Real integrated CDC acceptance runner (worktree cdc-analysis-20261110).

Executes the SCENARIOS registry of ``integrated_cdc.py`` against the real
isolated compose project ``cdc-canary-20261110``: real MySQL/PostgreSQL
sources, the real standalone CDC executor (child Debezium servers), the real
receiver -> ingest -> Sentinel fact/outbox pipeline and the real backend
notification worker. Nothing on the CDC path is mocked, faked or synthetically
injected: every asserted fact, notification and state transition was produced
by the live stack.

Honest setup contract (reported per-step in the setup artifact):

- Platform schema: this runner owns ``alembic upgrade head`` on the canary
  platform database (loopback 127.0.0.1:45532 only, sanitized fixture env,
  in-process Alembic exactly like the migration probe). No dotenv is read;
  the runner refuses to start if any backend env file exists in the worktree.
- Source fixture: seeded through the QA driver's ``seed-sources`` (public
  deterministic compose credentials). This runner additionally enforces
  ``REPLICA IDENTITY FULL`` on the PG inventory table (the connector
  preflight requires it) and appends two extra deterministic rows before the
  coverage anchor is taken, so they can never leak in as history.
- Connections and assets: registered and tested through the real backend API
  (login -> POST /api/v2/connections -> POST .../test -> POST
  /api/v2/integrated-datasets), exercising the pinned dbx metadata lane.
- Ontology release with one remote mapping + one on_pattern Sentinel and an
  internal notification action: seeded deterministically through the platform
  ORM (explicit setup, mirroring the reviewed flow fixture), because the full
  publish frontdoor is owned by other in-flight work. The frozen snapshot is
  built from the REAL registered asset reflection (ids, fingerprint,
  columns), so the CDC preflight/contract checks run against genuine physical
  metadata. Actual receiver/ingest/Sentinel/Notification behavior is always
  live.

Run with the backend venv (``backend/.venv/bin/python``); uses only the
standard library plus project-existing requests/sqlalchemy/psycopg2.

    migrate    alembic upgrade head on cdc_platform (idempotent, db-only)
    setup      fixture + API connections/assets + ORM release + wait healthy
    run        execute acceptance scenarios (see --list)
    all        setup + run

Evidence lands under ``.artifacts/cdc-validation/acceptance/`` and is never
committed to Git.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:  # normal script execution: scripts/e2e is on sys.path
    import integrated_stack as base
    import integrated_cdc as driver
except ImportError:  # importlib loading from tests without package context
    def _load(name: str):
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().with_name(f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    base = _load("integrated_stack")
    driver = _load("integrated_cdc")

ROOT = base.ROOT
PROJECT = driver.PROJECT
ART = ROOT / ".artifacts" / "cdc-validation" / "acceptance"

BACKEND = ROOT / "backend"
PLATFORM_DSN = driver.PLATFORM_DSN
FIXTURE_SECRET = driver.FIXTURE_SECRET

API = "http://127.0.0.1:48180"


def fixture_admin() -> tuple[str, str]:
    """Synthetic admin credentials, read from the compose fixture itself.

    The compose file is the single public source of truth for the canary's
    deterministic fixture credentials; nothing is guessed or defaulted and no
    dotenv/production value is ever consulted. Fails closed when absent.
    """
    import re

    text = driver.COMPOSE.read_text(encoding="utf-8")
    block = re.search(r"^  backend:\n(?P<body>(?:^(?:    .*)?\n)+)", text,
                      re.MULTILINE)
    if block is None:
        raise RuntimeError("compose backend service block not found")
    values = {}
    for key in ("FIRST_ADMIN_USER", "FIRST_ADMIN_PASSWORD"):
        match = re.search(rf"^\s+{key}:\s*(\S+)\s*$", block.group("body"),
                          re.MULTILINE)
        if match is None:
            raise RuntimeError(
                f"compose backend fixture does not declare {key}; refusing "
                "to guess admin credentials")
        values[key] = match.group(1).strip("'\"")
    return values["FIRST_ADMIN_USER"], values["FIRST_ADMIN_PASSWORD"]

# Extra deterministic rows added BEFORE the coverage anchor (commit-order and
# restart scenarios). Never part of the driver's aggregate oracle.
EXTRA_ROWS = (
    ("item-09", 100, "ready", "2026-01-01 00:00:00"),
    ("item-10", 100, "ready", "2026-01-01 00:00:00"),
)

# Explicit SQLAlchemy URLs over the compose-internal service names (public
# deterministic fixture credentials from the compose file). The split
# host/port/user form is not used: SQLConnector defaults it to mysql+pymysql
# regardless of kind, while connection_string is honored identically by the
# connection test, native reflection, dbx discovery and the CDC identity.
SOURCE_URLS = {
    "pg": "postgresql+psycopg2://cdc_e2e:cdc_e2e_fixture@source-pg:5432/cdc_source",
    "mysql": "mysql+pymysql://cdc_e2e:cdc_e2e_fixture@source-mysql:3306/cdc_source",
}
SOURCE_KINDS = {"pg": "postgres", "mysql": "mysql"}

PG_ONTOLOGY = "cdc-acc-pg-ontology"
MYSQL_ONTOLOGY = "cdc-acc-mysql-ontology"

# Time budget defaults (seconds): Debezium JVM cold starts dominate.
ACTIVATION_TIMEOUT = 300.0
EVENT_TIMEOUT = 60.0
RESTART_TIMEOUT = 240.0
CLEANUP_TIMEOUT = 180.0
POLL_INTERVAL = 0.2

SCENARIO_ORDER = (
    "no_history",
    "separate_commits_same_timestamp",
    "multi_field_one_row_one_notification",
    "rollback_no_event",
    "one_transaction_intermediate_honest",
    "mysql_same_contract",
    # Provenance inspects every notice produced so far: it must follow all
    # notifying foundational scenarios of BOTH sources.
    "notification_provenance",
    "latency_p95_commit_to_notification",
    "pg_commit_order_no_loss",
    "credential_rotation_resumes_offset",
    "restart_resume_offsets",
    "source_gap_explicit_unhealthy",
    "ddl_drift_explicit_unhealthy",
    "repoint_and_decommission_structured_409",
    "last_mapping_removal_stops_and_cleans",
    "republish_after_dormant_new_generation",
    "zero_object_instance_no_lake_mirror",
)


# ── environment sanitization ───────────────────────────────────────────────
def sanitize_environment() -> None:
    for relative in ("backend/.env", ".env", "config/generated/local/.env"):
        if (ROOT / relative).exists():
            raise RuntimeError(
                f"sanitized worktree expected: {relative} must not exist")
    sys.path.insert(0, str(BACKEND))
    # Fixture env for every app import (models, alembic env, hash helpers).
    # SENTINEL_AUTO_DISPATCH=0 keeps this host process from ever spawning a
    # dispatch worker; the backend container owns notification dispatch.
    import os

    os.environ.update({
        "ENVIRONMENT": "test",
        "DATABASE_URL": "postgresql://cdc_e2e:cdc_e2e_fixture"
                        "@127.0.0.1:45532/cdc_platform",
        "REDIS_URL": "redis://:cdc_e2e_fixture@127.0.0.1:46489/0",
        "SECRET_KEY": FIXTURE_SECRET,
        "SENTINEL_AUTO_DISPATCH": "0",
    })


# ── small shared helpers ───────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def wait_until(predicate: Callable[[], Any], timeout: float,
               interval: float = POLL_INTERVAL, *,
               describe: str = "condition") -> tuple[bool, float, Any]:
    """Poll until truthy; returns (ok, elapsed_seconds, last_value)."""
    started = time.monotonic()
    last: Any = None
    while time.monotonic() - started <= timeout:
        last = predicate()
        if last:
            return True, time.monotonic() - started, last
        time.sleep(interval)
    return False, time.monotonic() - started, last


def write_artifact(name: str, payload: Any) -> Path:
    ART.mkdir(parents=True, exist_ok=True)
    path = ART / name
    if path.resolve().parent != ART.resolve():
        raise ValueError("artifact must stay inside the owned directory")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    return path


class PlatformDb:
    """Read-mostly helper over the canary platform database (loopback only)."""

    def __init__(self):
        import sqlalchemy as sa
        from sqlalchemy.pool import NullPool

        self.sa = sa
        self.engine = sa.create_engine(
            PLATFORM_DSN, poolclass=NullPool, hide_parameters=True)

    def rows(self, statement: str, **params) -> list[dict]:
        with self.engine.connect() as conn:
            result = conn.execute(self.sa.text(statement), params)
            columns = result.keys()
            return [dict(zip(columns, row)) for row in result]

    def scalar(self, statement: str, **params):
        with self.engine.connect() as conn:
            return conn.execute(self.sa.text(statement), params).scalar()

    def close(self):
        self.engine.dispose()


class Api:
    """Minimal real-API client (login once, bearer afterwards)."""

    def __init__(self):
        import requests

        self.requests = requests
        self.session = requests.Session()
        self.session.trust_env = False
        self.token: str | None = None
        self.admin_id: str | None = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, method: str, path: str, *, json_body=None,
                timeout: float = 60.0):
        response = self.session.request(
            method, API + path, json=json_body, headers=self._headers(),
            timeout=timeout)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:2000]}
        return response.status_code, payload

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, json_body=body)

    def patch(self, path, body=None):
        return self.request("PATCH", path, json_body=body)

    def delete(self, path):
        return self.request("DELETE", path)

    def wait_ready(self, timeout: float = 180.0) -> dict:
        ok, elapsed, last = wait_until(
            lambda: self.get("/openapi.json")[0] == 200,
            timeout, 1.0, describe="backend http")
        return {"ready": ok, "waited_seconds": round(elapsed, 1),
                "last_status": last}

    def login(self) -> dict:
        username, password = fixture_admin()
        status, payload = self.post("/api/v1/auth/login", {
            "username": username, "password": password})
        if status != 200:
            raise RuntimeError(f"login failed: {status} {payload}")
        self.token = payload["data"]["access_token"]
        status, profile = self.get("/api/v1/auth/profile")
        if status != 200:
            raise RuntimeError(f"profile failed: {status} {profile}")
        self.admin_id = profile["data"]["id"]
        return {"admin_id": self.admin_id, "username": username}


# ── source helpers (docker exec into OWN containers, label-verified) ───────
def source_sql(service: str, statement: str) -> str:
    return driver._source_sql(service, statement)


def container_id(service: str) -> str:
    return base.require_owned(service, PROJECT)


def owned_container_any_state(service: str) -> str:
    """Label-verified own container id, running or stopped (exactly one)."""
    matches = [item for item in base.owned_containers(PROJECT)
               if item["service"] == service]
    if len(matches) != 1:
        raise RuntimeError(
            f"exactly one owned {service} container is required, "
            f"found {len(matches)}")
    return matches[0]["id"]


class ContainerOpsNotAllowed(RuntimeError):
    """Container lifecycle is owned by the stack operator, not this runner."""


_ALLOW_CONTAINER_OPS = False


def _require_container_ops(action: str) -> None:
    if not _ALLOW_CONTAINER_OPS:
        raise ContainerOpsNotAllowed(
            f"{action} needs --allow-container-ops (container lifecycle is "
            "owned by the stack operator)")


def docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    process = subprocess.run(
        ["docker", *args], capture_output=True, text=True, env=base.SAFE_ENV,
        timeout=timeout)
    return process


def stop_own_container(service: str, grace: int = 25) -> dict:
    _require_container_ops(f"stopping {service}")
    process = docker("stop", "-t", str(grace), container_id(service), timeout=60)
    if process.returncode:
        raise RuntimeError(f"docker stop {service} failed: {process.stderr[-500:]}")
    return {"service": service, "stopped": True}


def start_own_container(service: str) -> dict:
    _require_container_ops(f"starting {service}")
    process = docker("start", owned_container_any_state(service), timeout=60)
    if process.returncode:
        raise RuntimeError(f"docker start {service} failed: {process.stderr[-500:]}")
    return {"service": service, "started": True}


def pg_source_session(script: str) -> subprocess.Popen:
    """Open a streaming psql session over docker exec (concurrency scenarios)."""
    command = ["docker", "exec", "-i", container_id("source-pg"),
               "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "cdc_e2e",
               "-d", "cdc_source"]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env=base.SAFE_ENV)
    process.stdin.write(script)
    process.stdin.flush()
    return process


# ── evidence queries over the platform DB ──────────────────────────────────
FACT_COLUMNS = ("id, ontology_id, instance_id, source_event_id, event_group_id, "
                "external_asset_id, change_kind, key, old_value, new_value, "
                "occurred_at, created_at")

_TABLES: dict[str, str] = {}


def table(name: str) -> str:
    """Canonical physical table name resolved from the ORM model (never guessed)."""
    if not _TABLES:
        from app.model_registry import import_all_models

        import_all_models()
        from app.data_channel.integrated.models import (
            IntegratedAsset, IntegratedCdcSource, IntegratedCdcSubscription,
            IntegratedMutation)
        from app.ontologies.formal_modeling.models import ObjectInstance
        from app.ontologies.projects.models import OntologyProject
        from app.ontologies.sentinels.models import (
            Notification, SentinelCdcOutbox, SentinelEventLog)

        _TABLES.update({
            "event_log": SentinelEventLog.__tablename__,
            "notifications": Notification.__tablename__,
            "outbox": SentinelCdcOutbox.__tablename__,
            "object_instances": ObjectInstance.__tablename__,
            "projects": OntologyProject.__tablename__,
            "cdc_sources": IntegratedCdcSource.__tablename__,
            "cdc_subscriptions": IntegratedCdcSubscription.__tablename__,
            "mutations": IntegratedMutation.__tablename__,
            "assets": IntegratedAsset.__tablename__,
        })
    return _TABLES[name]


def external_facts(db: PlatformDb, ontology_id: str,
                   instance_id: str | None = None) -> list[dict]:
    statement = (f"SELECT {FACT_COLUMNS} FROM {table('event_log')} "
                 "WHERE ontology_id = :o AND source_event_id IS NOT NULL")
    params: dict[str, Any] = {"o": ontology_id}
    if instance_id is not None:
        statement += " AND instance_id = :i"
        params["i"] = instance_id
    statement += " ORDER BY id"
    return db.rows(statement, **params)


def notifications(db: PlatformDb, ontology_id: str) -> list[dict]:
    return db.rows(
        "SELECT id, ontology_id, ontology_release_id, sentinel_id, "
        "action_log_id, recipient, channel, status, related_object_id, "
        f"created_at FROM {table('notifications')} "
        "WHERE ontology_id = :o ORDER BY created_at, id",
        o=ontology_id)


def notification_count(db: PlatformDb, ontology_id: str) -> int:
    return int(db.scalar(
        f"SELECT count(*) FROM {table('notifications')} WHERE ontology_id = :o",
        o=ontology_id) or 0)


def source_state(db: PlatformDb, connection_id: str) -> dict | None:
    rows = db.rows(
        "SELECT connection_id, generation, status, last_error_code, "
        "last_position, heartbeat_at, cleanup_completed_at, last_event_at "
        f"FROM {table('cdc_sources')} WHERE connection_id = :c", c=connection_id)
    return rows[0] if rows else None


def subscriptions(db: PlatformDb, ontology_id: str) -> list[dict]:
    return db.rows(
        "SELECT id, connection_id, asset_id, ontology_id, release_id, "
        "mapping_id, status, source_generation, activation_position, "
        f"activated_at FROM {table('cdc_subscriptions')} "
        "WHERE ontology_id = :o ORDER BY created_at", o=ontology_id)


def compute_instance_id(ontology_id: str, object_type_id: str, asset_id: str,
                        connection_id: str, generation: int,
                        pk: dict) -> str:
    from app.data_channel.integrated.cdc_ingest import _hash_pairs

    scope = ":".join((ontology_id, object_type_id, asset_id,
                      connection_id, str(generation))).encode("utf-8")
    return "remote:" + _hash_pairs(
        scope, [(key, pk[key]) for key in sorted(pk)])


def pg_slot_names() -> list[str]:
    raw = source_sql(
        "source-pg", "SELECT slot_name FROM pg_replication_slots ORDER BY 1;")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def pg_publication_names() -> list[str]:
    raw = source_sql(
        "source-pg", "SELECT pubname FROM pg_publication ORDER BY 1;")
    return [line.strip() for line in raw.splitlines() if line.strip()]


# ── release ORM seed (explicit setup; live runtime does the rest) ──────────
OBJECT_ID_PROP, QUANTITY_PROP, STATE_PROP = "item_id", "quantity", "state"


def fixture_ids(ontology_id: str) -> dict:
    """Per-ontology namespaced fixture ids: global primary keys never collide."""
    return {
        "object_type": f"{ontology_id}-inventory-type",
        "action": f"{ontology_id}-notify",
        "sentinel": f"{ontology_id}-zero-sentinel",
        "mapping": f"{ontology_id}-mapping",
        "rule": f"{ontology_id}-notification-rule",
        "release_v1": f"{ontology_id}-release-v1",
        "release_v2": f"{ontology_id}-release-v2",
    }


def _object_properties() -> list[dict]:
    return [
        {"id": "item-id", "name": OBJECT_ID_PROP, "type": "string",
         "required": True},
        {"id": "quantity", "name": QUANTITY_PROP, "type": "number"},
        {"id": "state", "name": STATE_PROP, "type": "string"},
    ]


def _action_rules(ontology_id: str, admin_id: str) -> list[dict]:
    return [{
        "id": fixture_ids(ontology_id)["rule"], "name": "Notify operations",
        "type": "notification", "enabled": True,
        "config": {"channel": "internal", "recipientSource": "constant",
                   "recipient": admin_id,
                   "messageTemplate": "Inventory requires review"},
    }]


def _pattern(ontology_id: str) -> dict:
    return {"stages": [{
        "alias": "item", "objectTypeId": fixture_ids(ontology_id)["object_type"],
        "filter": f"item.{QUANTITY_PROP} == 0 and item.{STATE_PROP} == 'ready'",
    }], "absence": {"enabled": False}, "within": 3600}


def _release_snapshot(ontology_id: str, descriptor: dict,
                      admin_id: str) -> tuple[dict, str]:
    from app.ontologies.versions.snapshot_contract import (
        complete_snapshot, snapshot_hash)

    ids = fixture_ids(ontology_id)
    rules = _action_rules(ontology_id, admin_id)
    pattern = _pattern(ontology_id)
    snapshot = complete_snapshot({
        "objectTypes": [{
            "id": ids["object_type"], "name": "Inventory",
            "displayName": "Inventory", "primaryKey": "item-id",
            "properties": _object_properties(),
        }],
        "actions": [{
            "id": ids["action"], "name": "notify_inventory",
            "displayName": "Inventory notification",
            "objectTypeId": ids["object_type"], "parameters": [],
            "rules": rules,
        }],
        "sentinels": [{
            "id": ids["sentinel"], "name": "inventory_zero",
            "displayName": "Inventory zero",
            "bindings": [{"alias": "item",
                          "objectTypeId": ids["object_type"]}],
            "links": [], "pattern": pattern, "condition": None,
            "primaryAlias": "item", "actionIds": [ids["action"]],
            "actionParameters": {}, "triggerMode": "on_pattern",
            "onChange": True, "onSchedule": True, "scanIntervalSeconds": 60,
            "enabled": True, "muted": False, "status": "published",
        }],
        "mappings": [{
            "id": ids["mapping"], "entityClass": "Inventory",
            "targetObjectTypeId": ids["object_type"],
            "curatedDatasetId": descriptor["datasetId"],
            "status": "active", "bindingMode": "remote",
            "remote": descriptor,
            "fieldMapping": {
                "source_id": OBJECT_ID_PROP, "balance": QUANTITY_PROP,
                "source_status": STATE_PROP, "__primary_key__": "source_id",
                "__binding_mode__": "remote", "__remote__": descriptor,
            },
        }],
    })
    return snapshot, snapshot_hash(snapshot)


def seed_release(db: PlatformDb, *, ontology_id: str, descriptor: dict,
                 admin_id: str) -> dict:
    """Create-if-absent ORM seed of one released ontology (fixture-scoped).

    Never deletes: an existing fixture project is reused as-is (deleting a
    project would cascade live CDC subscription rows and bypass the
    controlled stop + slot cleanup). Only this runner's namespaced ids are
    ever written.
    """
    from sqlalchemy.orm import sessionmaker

    from app.model_registry import import_all_models

    import_all_models()
    from app.ontologies.formal_modeling.models import ActionType, ObjectType
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.sentinels.models import Sentinel
    from app.ontologies.versions.models import OntologyVersion

    ids = fixture_ids(ontology_id)
    snapshot, digest = _release_snapshot(ontology_id, descriptor, admin_id)
    session = sessionmaker(bind=db.engine)()
    created: list[str] = []
    try:
        project = session.get(OntologyProject, ontology_id)
        if project is None:
            project = OntologyProject(
                id=ontology_id, name=f"CDC acceptance {ontology_id}",
                domain="synthetic", created_by=admin_id, status="published",
                version="v1")
            session.add(project)
            session.flush()
            created.append("project")
        if session.get(ObjectType, ids["object_type"]) is None:
            session.add(ObjectType(
                id=ids["object_type"], ontology_id=ontology_id,
                name="Inventory", display_name="Inventory",
                primary_key="item-id", properties=_object_properties()))
            created.append("object_type")
        if session.get(ActionType, ids["action"]) is None:
            session.add(ActionType(
                id=ids["action"], ontology_id=ontology_id,
                name="notify_inventory",
                display_name="Inventory notification",
                object_type_id=ids["object_type"], parameters=[],
                rules=_action_rules(ontology_id, admin_id)))
            created.append("action")
        if session.get(Sentinel, ids["sentinel"]) is None:
            session.add(Sentinel(
                id=ids["sentinel"], ontology_id=ontology_id,
                name="inventory_zero", display_name="Inventory zero",
                bindings=[{"alias": "item",
                           "objectTypeId": ids["object_type"]}],
                links=[], pattern=_pattern(ontology_id), condition=None,
                primary_alias="item", action_ids=[ids["action"]],
                action_parameters={}, trigger_mode="on_pattern",
                on_change=True, on_schedule=True, scan_interval_seconds=60,
                enabled=True, muted=False, status="published"))
            created.append("sentinel")
        release = session.get(OntologyVersion, ids["release_v1"])
        if release is None:
            release = OntologyVersion(
                id=ids["release_v1"], ontology_id=ontology_id,
                version_number="v1", node_kind="release",
                lifecycle_status="released", revision=0,
                snapshot_formal=snapshot, snapshot_hash=digest,
                published_at=datetime.now(timezone.utc), created_by=admin_id)
            session.add(release)
            created.append("release_v1")
        session.flush()
        pointer_before = project.current_release_id
        if project.current_release_id is None:
            project.current_release_id = release.id
        session.commit()
        return {"ontology_id": ontology_id, "release_id": release.id,
                "snapshot_hash": release.snapshot_hash,
                "created": created or ["reused-existing-fixture"],
                "pointer_before": pointer_before,
                "current_release_id": project.current_release_id,
                "fixture_ids": ids,
                "mapping_remote_descriptor": descriptor}
    finally:
        session.close()


def clear_release_pointer(db: PlatformDb, ontology_id: str) -> None:
    with db.engine.begin() as conn:
        conn.execute(db.sa.text(
            f"UPDATE {table('projects')} SET current_release_id = NULL "
            "WHERE id = :o"), {"o": ontology_id})


def set_release_pointer(db: PlatformDb, ontology_id: str, release_id: str) -> None:
    with db.engine.begin() as conn:
        conn.execute(db.sa.text(
            f"UPDATE {table('projects')} SET current_release_id = :r "
            "WHERE id = :o"), {"o": ontology_id, "r": release_id})


def seed_release_v2(db: PlatformDb, ontology_id: str, descriptor: dict,
                    admin_id: str) -> str:
    """Publish a second immutable release (v2) for the existing fixture project."""
    from sqlalchemy.orm import sessionmaker

    from app.ontologies.versions.models import OntologyVersion

    ids = fixture_ids(ontology_id)
    snapshot, digest = _release_snapshot(ontology_id, descriptor, admin_id)
    session = sessionmaker(bind=db.engine)()
    try:
        if session.get(OntologyVersion, ids["release_v2"]) is None:
            session.add(OntologyVersion(
                id=ids["release_v2"], ontology_id=ontology_id,
                version_number="v2", node_kind="release",
                lifecycle_status="released", revision=0,
                snapshot_formal=snapshot, snapshot_hash=digest,
                published_at=datetime.now(timezone.utc), created_by=admin_id))
            session.commit()
    finally:
        session.close()
    set_release_pointer(db, ontology_id, ids["release_v2"])
    return ids["release_v2"]


def verify_platform_head(db: PlatformDb) -> dict:
    """Read-only check: the platform DB is at the single Alembic head.

    The stack operator owns the platform migration; this runner only verifies.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    current = db.scalar("SELECT version_num FROM alembic_version")
    return {"heads": list(heads), "current": current,
            "at_head": len(heads) == 1 and current == heads[0]}


# ── migrate (this runner owns the platform schema) ─────────────────────────
def cmd_migrate() -> dict:
    sanitize_environment()
    base.require_owned("db", PROJECT)
    import sqlalchemy as sa

    started = time.monotonic()
    report: dict[str, Any] = {"dsn_host": "127.0.0.1:45532",
                              "database": "cdc_platform", "steps": []}
    engine = sa.create_engine(PLATFORM_DSN)
    try:
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(BACKEND / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND / "alembic"))
        config.set_main_option(
            "sqlalchemy.url",
            PLATFORM_DSN.replace("postgresql+psycopg2", "postgresql").replace(
                "%", "%%"))
        heads = ScriptDirectory.from_config(config).get_heads()
        assert len(heads) == 1, f"expected one head, got {heads}"
        head = heads[0]
        report["head"] = head
        with engine.connect() as conn:
            exists = conn.execute(sa.text(
                "SELECT to_regclass('public.alembic_version')")).scalar()
            current = None
            if exists is not None:
                current = conn.execute(sa.text(
                    "SELECT version_num FROM alembic_version")).scalar()
        report["before"] = current
        if current != head:
            command.upgrade(config, "head")
            with engine.connect() as conn:
                after = conn.execute(sa.text(
                    "SELECT version_num FROM alembic_version")).scalar()
            report["after"] = after
            report["upgraded"] = True
        else:
            report["after"] = current
            report["upgraded"] = False
        tables = int(engine.connect().execute(sa.text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public'")).scalar())
        report["public_tables"] = tables
        report["status"] = "ok" if report["after"] == head else "failed"
    except Exception as error:  # noqa: BLE001 - exact blocker in artifact
        traceback.print_exc()
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"[:800]
    finally:
        engine.dispose()
        report["wall_seconds"] = round(time.monotonic() - started, 1)
    path = write_artifact("migrate.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"artifact: {path}")
    return report


# ── setup ───────────────────────────────────────────────────────────────────
class Context:
    def __init__(self):
        self.db = PlatformDb()
        self.api = Api()
        self.state: dict[str, Any] = {}
        self.results: dict[str, dict] = {}

    # ── live helpers used by scenarios ─────────────────────────────────────
    def pg_descriptor(self) -> dict:
        return self.state["pg"]["descriptor"]

    def mysql_descriptor(self) -> dict:
        return self.state["mysql"]["descriptor"]

    def ontology(self, source: str) -> dict:
        return self.state[source]

    def instance_id(self, source: str, item: str) -> str:
        entry = self.state[source]
        generation = entry["generation"]
        return compute_instance_id(
            entry["ontology_id"],
            fixture_ids(entry["ontology_id"])["object_type"],
            entry["asset_id"],
            entry["connection_id"], generation, {"source_id": item})

    def commit_source(self, service: str, statement: str) -> float:
        """Execute one autocommit statement; returns monotonic time after."""
        source_sql(service, statement)
        return time.monotonic()

    def wait_facts(self, ontology_id: str, instance_id: str, minimum: int,
                   timeout: float = EVENT_TIMEOUT):
        def facts():
            rows = external_facts(self.db, ontology_id, instance_id)
            return rows if len(rows) >= minimum else None

        return wait_until(facts, timeout, describe="facts")

    def wait_notifications(self, ontology_id: str, minimum: int,
                           timeout: float = EVENT_TIMEOUT):
        def notices():
            count = notification_count(self.db, ontology_id)
            return count if count >= minimum else None

        return wait_until(notices, timeout, describe="notifications")

    def asset_cdc(self, source: str) -> tuple[int, dict]:
        entry = self.state[source]
        return self.api.get(
            f"/api/v2/integrated-datasets/{entry['asset_id']}/cdc")

    def object_instance_count(self) -> int:
        return int(self.db.scalar(
            f"SELECT count(*) FROM {table('object_instances')} "
            "WHERE ontology_id IN (:a, :b)",
            a=PG_ONTOLOGY, b=MYSQL_ONTOLOGY) or 0)

    def record(self, scenario: str, outcome: dict) -> dict:
        outcome.setdefault("finished_at", now_iso())
        self.results[scenario] = outcome
        write_artifact(f"{scenario}.json", outcome)
        write_artifact("summary.json", {
            "started_at": self.state.get("started_at"),
            "updated_at": now_iso(),
            "results": {name: {
                "status": value.get("status"),
                "detail": value.get("detail")} for name, value in
                self.results.items()},
        })
        print(f"[{outcome.get('status')}] {scenario}"
              + (f": {outcome.get('detail')}" if outcome.get("detail") else ""))
        return outcome


def _register_source(ctx: Context, source: str) -> dict:
    """Create (or repair) the fixture connection through the real API."""
    name = f"cdc-acc-source-{source}"
    config = {"connection_string": SOURCE_URLS[source]}
    status, existing = ctx.api.get("/api/v2/connections")
    if status != 200:
        raise RuntimeError(f"list connections failed: {status} {existing}")
    match = next((item for item in existing if item["name"] == name), None)
    if match is None:
        status, payload = ctx.api.post("/api/v2/connections", {
            "name": name, "kind": SOURCE_KINDS[source], "config": config})
        if status != 201:
            raise RuntimeError(f"create connection failed: {status} {payload}")
        connection_id = payload["id"]
        method = "created"
    elif match["status"] == "active":
        connection_id = match["id"]
        method = "reused-active"
    else:
        # A fixture connection left non-active (e.g. an earlier bad config):
        # rotate it to the canonical fixture config instead of reusing it.
        connection_id = match["id"]
        status, payload = ctx.api.patch(
            f"/api/v2/connections/{connection_id}/config", {"config": config})
        if status != 200:
            raise RuntimeError(
                f"repair connection config failed: {status} {payload}")
        method = f"reused-config-repaired (was {match['status']})"
    status, test = ctx.api.request(
        "POST", f"/api/v2/connections/{connection_id}/test", timeout=120.0)
    if status != 200 or not test.get("success"):
        raise RuntimeError(
            f"connection test failed for {name}: {status} {test}")
    return {"connection_id": connection_id, "method": method,
            "config_form": "connection_string", "test": test}


def _register_asset(ctx: Context, source: str, connection_id: str,
                    schema_name: str) -> dict:
    status, existing = ctx.api.get("/api/v2/integrated-datasets")
    if status != 200:
        raise RuntimeError(f"list assets failed: {status} {existing}")
    name = f"cdc-acc-{source}-inventory"
    match = next(
        (item for item in
         (existing.get("items", []) if isinstance(existing, dict) else existing)
         if isinstance(item, dict) and item.get("name") == name), None)
    if match is not None:
        asset_id = match.get("id") or match.get("asset_id")
        method = "reused"
    else:
        status, payload = ctx.api.post("/api/v2/integrated-datasets", {
            "name": name, "connection_id": connection_id,
            "schema_name": schema_name, "table_name": "inventory"})
        if status != 201:
            raise RuntimeError(f"register asset failed: {status} {payload}")
        asset_id = payload["id"]
        method = "created"
    status, schema = ctx.api.get(f"/api/v2/integrated-datasets/{asset_id}/schema")
    if status != 200:
        raise RuntimeError(f"asset schema failed: {status} {schema}")
    status, detail = ctx.api.get(f"/api/v2/integrated-datasets/{asset_id}")
    if status != 200:
        raise RuntimeError(f"asset detail failed: {status} {detail}")
    return {"asset_id": asset_id, "method": method, "schema": schema,
            "detail": detail}


def _descriptor_from_asset(entry: dict, admin_id: str) -> dict:
    schema = entry["schema"]
    detail = entry["detail"]
    columns = [
        {"name": column["name"],
         "type": column.get("type"),
         "nullable": bool(column.get("nullable", True))}
        for column in schema.get("columns", [])
    ]
    return {
        "assetId": entry["asset_id"],
        "datasetId": detail.get("dataset_id") or detail.get("datasetId"),
        "connectionId": detail.get("connection_id") or detail.get("connectionId"),
        "schemaName": detail.get("schema_name") or detail.get("schemaName"),
        "tableName": detail.get("table_name") or detail.get("tableName"),
        "columns": columns,
        "primaryKey": schema.get("primary_key", []),
        "schemaFingerprint": schema.get("schema_fingerprint"),
    }


_SETUP_REPORT: dict[str, Any] = {}


def cmd_setup(*, skip_seed: bool = False) -> Context:
    """Setup with a guaranteed artifact: failures persist the partial report."""
    _SETUP_REPORT.clear()
    try:
        return _setup(skip_seed=skip_seed)
    except BaseException as error:
        _SETUP_REPORT["status"] = "failed"
        _SETUP_REPORT["error"] = f"{type(error).__name__}: {error}"[:1500]
        _SETUP_REPORT["traceback_tail"] = traceback.format_exc()[-3000:]
        _SETUP_REPORT["finished_at"] = now_iso()
        path = write_artifact("setup.json", _SETUP_REPORT)
        print(f"setup failed; partial artifact: {path}", file=sys.stderr)
        raise


def _setup(*, skip_seed: bool = False) -> Context:
    sanitize_environment()
    started = now_iso()
    report = _SETUP_REPORT
    report.update({"started_at": started, "steps": []})

    def step(name: str, payload: Any) -> None:
        report["steps"].append({"step": name, "result": payload})

    # 1) platform schema must already be at head (operator-owned migration).
    probe_db = PlatformDb()
    try:
        head = verify_platform_head(probe_db)
    finally:
        probe_db.close()
    if not head["at_head"]:
        raise RuntimeError(f"platform DB is not at the Alembic head: {head}")
    step("platform_schema", {**head, "method": "verify-only (operator-owned)"})

    # 2) deterministic source fixture through the QA driver.
    def inventory_rows(service: str) -> int | None:
        """Row count, or None when the table is absent (catalog check first)."""
        if service == "source-pg":
            present = source_sql(
                service,
                "SELECT to_regclass('public.inventory') IS NOT NULL;").strip()
            exists = present == "t"
        else:
            exists = source_sql(
                service,
                "SELECT COUNT(*) FROM information_schema.tables WHERE "
                "table_schema = DATABASE() AND table_name = 'inventory';"
            ).strip() == "1"
        if not exists:
            return None
        return int(source_sql(
            service, "SELECT count(*) FROM inventory;").strip())

    seeded_rows = {service: inventory_rows(service)
                   for service in ("source-pg", "source-mysql")}
    seeded_before = all(
        count is not None and count >= len(driver.INVENTORY_ROWS)
        for count in seeded_rows.values())
    if not skip_seed and not seeded_before:
        # The driver seed DROPs and re-creates the table: never allowed once
        # any CDC coverage (subscription) exists for this fixture.
        coverage_db = PlatformDb()
        try:
            covered = int(coverage_db.scalar(
                f"SELECT count(*) FROM {table('cdc_subscriptions')} "
                "WHERE ontology_id IN (:a, :b)",
                a=PG_ONTOLOGY, b=MYSQL_ONTOLOGY) or 0)
        finally:
            coverage_db.close()
        if covered:
            raise RuntimeError(
                "source fixture is incomplete while CDC coverage exists "
                f"({covered} subscription rows, rows={seeded_rows}); refusing "
                "to DROP/re-seed a covered table")
        driver.cmd_seed_sources()
        step("seed_sources", {"method": "driver seed-sources",
                              "rows_before": seeded_rows})
    else:
        step("seed_sources", {"method": "already-present (skipped)",
                              "rows": seeded_rows})
    # PG connector preflight requires full before-images.
    source_sql("source-pg", "ALTER TABLE public.inventory REPLICA IDENTITY FULL;")
    ident = source_sql(
        "source-pg", "SELECT relreplident FROM pg_class c JOIN pg_namespace n "
        "ON n.oid=c.relnamespace WHERE n.nspname='public' "
        "AND relname='inventory';").strip()
    if ident != "f":
        raise RuntimeError(f"PG replica identity must be FULL, observed {ident!r}")
    step("pg_replica_identity_full", {"relreplident": ident})
    for row in EXTRA_ROWS:
        source_sql("source-pg",
                   "INSERT INTO inventory (source_id, balance, source_status, "
                   f"observed_at) VALUES ('{row[0]}', {row[1]}, '{row[2]}', "
                   f"'{row[3]}+00') ON CONFLICT (source_id) DO NOTHING;")
    total_pg = source_sql("source-pg", "SELECT count(*) FROM inventory;").strip()
    step("extra_rows", {"added": [row[0] for row in EXTRA_ROWS],
                        "pg_rows_total": total_pg})

    ctx = Context()
    ctx.state["started_at"] = started
    step("backend_ready", ctx.api.wait_ready())
    login = ctx.api.login()
    step("login", {**login,
                   "method": "POST /api/v1/auth/login (compose fixture admin)"})

    # 3) connections + assets through the real API (dbx lane exercised).
    pg_conn = _register_source(ctx, "pg")
    mysql_conn = _register_source(ctx, "mysql")
    step("connections", {"pg": pg_conn, "mysql": mysql_conn})
    pg_asset = _register_asset(ctx, "pg", pg_conn["connection_id"], "public")
    mysql_asset = _register_asset(
        ctx, "mysql", mysql_conn["connection_id"], "cdc_source")
    step("assets", {
        "pg": {"asset_id": pg_asset["asset_id"], "method": pg_asset["method"]},
        "mysql": {"asset_id": mysql_asset["asset_id"],
                  "method": mysql_asset["method"]}})

    # 4) deterministic ORM release seed (explicit setup; runtime stays live).
    pg_descriptor = _descriptor_from_asset(pg_asset, ctx.api.admin_id)
    mysql_descriptor = _descriptor_from_asset(mysql_asset, ctx.api.admin_id)
    pg_seed = seed_release(
        ctx.db, ontology_id=PG_ONTOLOGY, descriptor=pg_descriptor,
        admin_id=ctx.api.admin_id)
    mysql_seed = seed_release(
        ctx.db, ontology_id=MYSQL_ONTOLOGY, descriptor=mysql_descriptor,
        admin_id=ctx.api.admin_id)
    step("release_seed", {"method": "direct ORM (explicit deterministic setup)",
                          "pg": pg_seed, "mysql": mysql_seed})

    ctx.state["pg"] = {
        "ontology_id": PG_ONTOLOGY, "release_id": pg_seed["release_id"],
        "connection_id": pg_conn["connection_id"], "asset_id": pg_asset["asset_id"],
        "descriptor": pg_descriptor, "generation": None,
    }
    ctx.state["mysql"] = {
        "ontology_id": MYSQL_ONTOLOGY, "release_id": mysql_seed["release_id"],
        "connection_id": mysql_conn["connection_id"],
        "asset_id": mysql_asset["asset_id"],
        "descriptor": mysql_descriptor, "generation": None,
    }

    # 5) wait until both sources are streaming with anchored coverage.
    def healthy(source: str):
        status, payload = ctx.asset_cdc(source)
        if status != 200:
            return None
        if (payload.get("enabled") and payload.get("healthy")
                and payload.get("status") == "active"
                and payload.get("coverage_started_at")):
            return payload
        return None

    waits = {}
    for source in ("pg", "mysql"):
        ok, elapsed, last = wait_until(
            lambda s=source: healthy(s), ACTIVATION_TIMEOUT, 2.0,
            describe=f"{source} cdc healthy")
        waits[source] = {"ok": ok, "waited_seconds": round(elapsed, 1),
                         "last": last}
        if not ok:
            report["activation"] = waits
            report["status"] = "failed"
            write_artifact("setup.json", report)
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            raise RuntimeError(f"{source} CDC did not become healthy: {last}")
        state = source_state(ctx.db, ctx.state[source]["connection_id"])
        ctx.state[source]["generation"] = state["generation"]
        waits[source]["generation"] = state["generation"]
        waits[source]["last_position"] = state["last_position"]
    step("activation", waits)

    pg_slots = pg_slot_names()
    owned = [name for name in pg_slots if name.startswith("cdc_")]
    step("pg_slots_after_activation", {"slots": pg_slots, "owned": owned})
    if len(owned) != 1:
        raise RuntimeError(f"expected exactly one owned PG slot, saw {pg_slots}")

    # 6) no-history baseline: seeded rows must not appear as facts.
    baseline = {}
    for source in ("pg", "mysql"):
        ontology_id = ctx.state[source]["ontology_id"]
        baseline[source] = {
            "facts": len(external_facts(ctx.db, ontology_id)),
            "notifications": notification_count(ctx.db, ontology_id),
        }
    object_instances = ctx.object_instance_count()
    sync_runs = {}
    for source in ("pg", "mysql"):
        status, payload = ctx.api.get(
            f"/api/v2/integrated-datasets/{ctx.state[source]['asset_id']}"
            "/sync-runs")
        sync_runs[source] = {"status": status,
                             "count": len(payload.get("items", payload))
                             if isinstance(payload, dict) else None}
    report["baseline"] = {"per_ontology": baseline,
                          "object_instances": object_instances,
                          "sync_runs": sync_runs}
    if any(value["facts"] or value["notifications"]
           for value in baseline.values()) or object_instances:
        report["status"] = "failed"
        write_artifact("setup.json", report)
        raise RuntimeError(f"no-history baseline violated: {report['baseline']}")
    report["status"] = "ok"
    report["finished_at"] = now_iso()
    write_artifact("setup.json", report)
    print(json.dumps({
        "status": report["status"], "steps": [s["step"] for s in report["steps"]],
        "baseline": report["baseline"], "activation": waits,
        "owned_pg_slot": owned[0],
    }, ensure_ascii=False, indent=2, default=str))
    ctx.state["setup_report"] = report
    ctx.state["owned_pg_slot"] = owned[0]
    return ctx


# ── scenarios ───────────────────────────────────────────────────────────────
def scenario_no_history(ctx: Context) -> dict:
    """Seeded rows (before the anchor) never appear as facts or notices."""
    evidence = {}
    for source in ("pg", "mysql"):
        ontology_id = ctx.state[source]["ontology_id"]
        rows = external_facts(ctx.db, ontology_id)
        evidence[source] = {
            "facts": len(rows),
            "notifications": notification_count(ctx.db, ontology_id),
            "snapshot_mode": "no_data",
        }
    ok = all(value["facts"] == 0 and value["notifications"] == 0
             for value in evidence.values())
    return {"status": "passed" if ok else "failed",
            "detail": "no snapshot history" if ok else "history leaked",
            "evidence": evidence}


def scenario_separate_commits(ctx: Context) -> dict:
    """item-01: two committed 100->0->100 changes -> two events, one notice."""
    item = "item-01"
    instance = ctx.instance_id("pg", item)
    base_facts = len(external_facts(ctx.db, PG_ONTOLOGY, instance))
    base_notices = notification_count(ctx.db, PG_ONTOLOGY)

    t0 = ctx.commit_source("source-pg",
        f"UPDATE inventory SET balance=0 WHERE source_id='{item}';")
    ok1, _, _ = ctx.wait_facts(PG_ONTOLOGY, instance, base_facts + 1)
    t1 = ctx.commit_source("source-pg",
        f"UPDATE inventory SET balance=100 WHERE source_id='{item}';")
    ok2, wait2, _ = ctx.wait_facts(PG_ONTOLOGY, instance, base_facts + 2)
    okn, notice_wait, _ = ctx.wait_notifications(
        PG_ONTOLOGY, base_notices + 1, timeout=EVENT_TIMEOUT * 2)

    rows = external_facts(ctx.db, PG_ONTOLOGY, instance)
    notices = notifications(ctx.db, PG_ONTOLOGY)
    new_notices = notices[base_notices:]
    deltas = rows[base_facts:]
    value_sequence = [
        {"key": row["key"], "old": row["old_value"], "new": row["new_value"],
         "change_kind": row["change_kind"], "source_event_id": row["source_event_id"]}
        for row in deltas]
    ok = (ok1 and ok2 and okn
          and len(deltas) == 2
          and len({row["source_event_id"] for row in deltas}) == 2
          and len(new_notices) == 1)
    return {"status": "passed" if ok else "failed",
            "detail": "two separate commits preserved; transient zero notified once"
                      if ok else "event/notice contract violated",
            "evidence": {
                "item": item, "committed": [
                    {"balance": 0, "t_commit": t0},
                    {"balance": 100, "t_commit": t1}],
                "events": value_sequence,
                "distinct_source_event_ids": len(
                    {row["source_event_id"] for row in deltas}),
                "notification_delta": len(new_notices),
                "first_notice_wait_seconds": round(notice_wait, 2),
                "second_event_wait_seconds": round(wait2, 2),
            }}


def scenario_multi_field(ctx: Context) -> dict:
    """item-02: one row update touching balance+status -> exactly one notice."""
    item = "item-02"
    instance = ctx.instance_id("pg", item)
    base_facts = len(external_facts(ctx.db, PG_ONTOLOGY, instance))
    base_notices = notification_count(ctx.db, PG_ONTOLOGY)

    ctx.commit_source("source-pg",
        f"UPDATE inventory SET balance=0, source_status='ready' "
        f"WHERE source_id='{item}' AND source_status='pending';")
    ok_facts, _, _ = ctx.wait_facts(PG_ONTOLOGY, instance, base_facts + 1)
    ok_notice, notice_wait, _ = ctx.wait_notifications(
        PG_ONTOLOGY, base_notices + 1, timeout=EVENT_TIMEOUT * 2)

    rows = external_facts(ctx.db, PG_ONTOLOGY, instance)[base_facts:]
    notices = notifications(ctx.db, PG_ONTOLOGY)[base_notices:]
    keys = sorted({row["key"] for row in rows})
    group_ids = {row["event_group_id"] for row in rows}
    ok = (ok_facts and ok_notice and len(rows) == 2
          and len(group_ids) == 1 and len(notices) == 1
          and keys == [QUANTITY_PROP, STATE_PROP])
    return {"status": "passed" if ok else "failed",
            "detail": "multi-field update -> one event group -> one notification"
                      if ok else "multi-field contract violated",
            "evidence": {
                "item": item, "fact_keys": keys,
                "event_group_ids": sorted(group_ids),
                "facts": len(rows), "notifications": len(notices),
                "notice_wait_seconds": round(notice_wait, 2),
            }}


def scenario_rollback(ctx: Context) -> dict:
    """item-03: BEGIN/UPDATE/ROLLBACK produces no facts, no notice."""
    item = "item-03"
    instance = ctx.instance_id("pg", item)
    base_facts = len(external_facts(ctx.db, PG_ONTOLOGY, instance))
    base_notices = notification_count(ctx.db, PG_ONTOLOGY)
    source_sql("source-pg", f"""
BEGIN;
UPDATE inventory SET balance=0 WHERE source_id='{item}';
ROLLBACK;
SELECT balance FROM inventory WHERE source_id='{item}';
""")
    balance = source_sql(
        "source-pg",
        f"SELECT balance FROM inventory WHERE source_id='{item}';").strip()
    time.sleep(8)  # bounded quiet window; absence must be observed, not assumed
    facts = external_facts(ctx.db, PG_ONTOLOGY, instance)[base_facts:]
    notices = notification_count(ctx.db, PG_ONTOLOGY)
    ok = (balance == "100" and not facts
          and notices == base_notices)
    return {"status": "passed" if ok else "failed",
            "detail": "rolled-back transaction produced no facts or notices"
                      if ok else "rollback leaked events",
            "evidence": {
                "item": item, "final_balance": balance,
                "facts_delta": len(facts),
                "notifications_delta": notices - base_notices,
                "quiet_window_seconds": 8,
            }}


def scenario_one_transaction(ctx: Context) -> dict:
    """item-04: 100->0->100 inside ONE transaction, recorded exactly as emitted.

    The pinned PostgreSQL connector (pgoutput) emits every row change of a
    committed transaction; on this real stack the fixture transaction was
    observed as two row events. The scenario waits for that complete sequence
    and for the single notification of the transient zero, lets a bounded
    settle window pass, and then requires the persisted record to match
    exactly. Any other emitted shape fails with the observed sequence in the
    evidence; nothing is adapted to whatever happened to arrive.
    """
    item = "item-04"
    instance = ctx.instance_id("pg", item)
    base_facts = len(external_facts(ctx.db, PG_ONTOLOGY, instance))
    base_notices = notification_count(ctx.db, PG_ONTOLOGY)

    def number(value):
        return json.loads(value) if isinstance(value, str) else value

    source_sql("source-pg", f"""
BEGIN;
UPDATE inventory SET balance=0 WHERE source_id='{item}';
UPDATE inventory SET balance=100 WHERE source_id='{item}';
COMMIT;
""")
    ok_facts, facts_wait, _ = ctx.wait_facts(
        PG_ONTOLOGY, instance, base_facts + 2, timeout=EVENT_TIMEOUT * 2)
    ok_notice, notice_wait, _ = ctx.wait_notifications(
        PG_ONTOLOGY, base_notices + 1, timeout=EVENT_TIMEOUT * 2)
    time.sleep(5)  # settle: an extra fact/notice must be observed, not assumed absent
    rows = external_facts(ctx.db, PG_ONTOLOGY, instance)[base_facts:]
    sequence = [
        {"key": row["key"], "old": row["old_value"], "new": row["new_value"],
         "source_event_id": row["source_event_id"]}
        for row in rows]
    transitions = [(number(row["old_value"]), number(row["new_value"]))
                   for row in rows]
    distinct = len({row["source_event_id"] for row in rows})
    notices = notification_count(ctx.db, PG_ONTOLOGY) - base_notices
    ok = (ok_facts and ok_notice
          and transitions == [(100, 0), (0, 100)]
          and distinct == 2
          and all(row["key"] == QUANTITY_PROP for row in rows)
          and notices == 1)
    return {"status": "passed" if ok else "failed",
            "detail": "one transaction 100->0->100 recorded as the two emitted "
                      "row events; transient zero notified exactly once"
                      if ok else "intra-transaction record differs from the "
                                 "connector's emitted sequence",
            "evidence": {
                "item": item, "observed_events": len(rows),
                "distinct_source_event_ids": distinct,
                "transitions": transitions,
                "sequence": sequence, "notifications_delta": notices,
                "facts_wait_seconds": round(facts_wait, 2),
                "notice_wait_seconds": round(notice_wait, 2),
                "settle_window_seconds": 5,
            }}


def scenario_provenance(ctx: Context) -> dict:
    """Every notification carries release/sentinel/action-log provenance."""
    evidence = {}
    ok = True
    for source in ("pg", "mysql"):
        ontology_id = ctx.state[source]["ontology_id"]
        rows = notifications(ctx.db, ontology_id)
        problems = []
        for row in rows:
            if row["ontology_release_id"] != ctx.state[source]["release_id"]:
                problems.append({"id": row["id"], "issue": "release_mismatch"})
            if row["sentinel_id"] != fixture_ids(ontology_id)["sentinel"]:
                problems.append({"id": row["id"], "issue": "sentinel_mismatch"})
            if not row["action_log_id"]:
                problems.append({"id": row["id"], "issue": "action_log_missing"})
            if row["recipient"] != ctx.api.admin_id:
                problems.append({"id": row["id"], "issue": "recipient_mismatch"})
            if row["channel"] != "internal":
                problems.append({"id": row["id"], "issue": "channel_mismatch"})
        evidence[source] = {"notifications": len(rows), "problems": problems}
        ok = ok and not problems and rows
    return {"status": "passed" if ok else "failed",
            "detail": "release+sentinel+actionlog provenance on every notice"
                      if ok else "provenance incomplete",
            "evidence": evidence}


def scenario_latency(ctx: Context, samples: int = 8) -> dict:
    """commit->notification P95 <= 5s on the active path (item-06)."""
    item = "item-06"
    instance = ctx.instance_id("pg", item)
    base_notices = notification_count(ctx.db, PG_ONTOLOGY)
    latencies = []
    errors = []
    for index in range(samples):
        if index > 0:
            # Return to 100 first (a real 0->100 fact, no notification).
            ctx.commit_source("source-pg",
                f"UPDATE inventory SET balance=100 WHERE source_id='{item}';")
            ok, _, _ = ctx.wait_facts(
                PG_ONTOLOGY, instance,
                len(external_facts(ctx.db, PG_ONTOLOGY, instance)) + 1)
            if not ok:
                errors.append(f"reset-{index}")
                continue
        t_commit = ctx.commit_source("source-pg",
            f"UPDATE inventory SET balance=0 WHERE source_id='{item}';")
        okn, waited, _ = ctx.wait_notifications(
            PG_ONTOLOGY, base_notices + index + 1, timeout=30.0)
        if not okn:
            errors.append(f"notice-{index}")
            continue
        # latency = wall time from commit completion to first-seen notice
        latencies.append(round(time.monotonic() - t_commit, 3))
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    else:
        p95 = None
    ok = not errors and p95 is not None and p95 <= 5.0
    return {"status": "passed" if ok else "failed",
            "detail": f"P95 {p95}s over {len(latencies)} samples (<=5s)"
                      if ok else f"latency gate failed ({errors or p95})",
            "evidence": {
                "item": item, "samples_requested": samples,
                "latencies_seconds": latencies, "p95_seconds": p95,
                "errors": errors,
                "method": "host monotonic from COMMIT return to first persisted "
                          "notification row (poll 0.2s; upper bound)",
            }}


def scenario_pg_commit_order(ctx: Context) -> dict:
    """Reverse row-LSN commit order loses no event (no high-water dedupe)."""
    first, second = "item-09", "item-10"
    instance_first = ctx.instance_id("pg", first)
    instance_second = ctx.instance_id("pg", second)
    base_first = len(external_facts(ctx.db, PG_ONTOLOGY, instance_first))
    base_second = len(external_facts(ctx.db, PG_ONTOLOGY, instance_second))

    # Session A updates `first` (lower row LSN) and holds; session B updates
    # `second` (higher LSN) and commits first; A commits last.
    holder = pg_source_session(
        f"BEGIN;\nUPDATE inventory SET balance=0 WHERE source_id='{first}';\n")
    time.sleep(1.0)
    ctx.commit_source("source-pg",
        f"UPDATE inventory SET balance=0 WHERE source_id='{second}';")
    ok_second, _, _ = ctx.wait_facts(
        PG_ONTOLOGY, instance_second, base_second + 1)
    holder.stdin.write("COMMIT;\n")
    holder.stdin.flush()
    holder.stdin.close()
    holder.wait(timeout=30)
    ok_first, _, _ = ctx.wait_facts(
        PG_ONTOLOGY, instance_first, base_first + 1)

    first_rows = external_facts(ctx.db, PG_ONTOLOGY, instance_first)[base_first:]
    second_rows = external_facts(
        ctx.db, PG_ONTOLOGY, instance_second)[base_second:]
    ok = (ok_first and ok_second
          and len(first_rows) == 1 and len(second_rows) == 1)
    return {"status": "passed" if ok else "failed",
            "detail": "lower-LSN row committing last is delivered exactly once"
                      if ok else "commit-order case lost or duplicated events",
            "evidence": {
                "held_row": first, "committed_first_row": second,
                "held_row_events": len(first_rows),
                "committed_first_events": len(second_rows),
                "held_row_event": {row["key"]: (row["old_value"],
                                                row["new_value"])
                                   for row in first_rows},
            }}


def scenario_mysql_contract(ctx: Context) -> dict:
    """item-07: same separate-commit contract through the MySQL connector."""
    item = "item-07"
    instance = ctx.instance_id("mysql", item)
    base_facts = len(external_facts(ctx.db, MYSQL_ONTOLOGY, instance))
    base_notices = notification_count(ctx.db, MYSQL_ONTOLOGY)

    t0 = ctx.commit_source("source-mysql",
        f"UPDATE inventory SET balance=0 WHERE source_id='{item}';")
    ok1, _, _ = ctx.wait_facts(MYSQL_ONTOLOGY, instance, base_facts + 1)
    ctx.commit_source("source-mysql",
        f"UPDATE inventory SET balance=100 WHERE source_id='{item}';")
    ok2, _, _ = ctx.wait_facts(MYSQL_ONTOLOGY, instance, base_facts + 2)
    okn, notice_wait, _ = ctx.wait_notifications(
        MYSQL_ONTOLOGY, base_notices + 1, timeout=EVENT_TIMEOUT * 2)

    rows = external_facts(ctx.db, MYSQL_ONTOLOGY, instance)[base_facts:]
    notices = notifications(ctx.db, MYSQL_ONTOLOGY)[base_notices:]
    ok = (ok1 and ok2 and okn and len(rows) == 2
          and len({row["source_event_id"] for row in rows}) == 2
          and len(notices) == 1)
    return {"status": "passed" if ok else "failed",
            "detail": "MySQL mirror: two commits preserved, one notification"
                      if ok else "MySQL contract violated",
            "evidence": {
                "item": item, "events": len(rows),
                "distinct_source_event_ids": len(
                    {row["source_event_id"] for row in rows}),
                "notifications": len(notices),
                "notice_wait_seconds": round(notice_wait, 2),
                "connector": "mysql binlog file/pos/row",
            }}


ROTATED_PG_PASSWORD = "cdc_e2e_fixture_rotated"  # public deterministic fixture
ORIGINAL_PG_PASSWORD = "cdc_e2e_fixture"         # compose POSTGRES_PASSWORD


def _pg_network_auth(password: str) -> bool:
    """True when the PG fixture role authenticates over the NETWORK path.

    Connects from inside the source container to its own service name, so the
    request matches the password-authenticated host rule. The unix socket and
    loopback are trust in the fixture image and would prove nothing.
    """
    process = docker(
        "exec", "-e", f"PGPASSWORD={password}", container_id("source-pg"),
        "psql", "-X", "-A", "-t", "-h", "source-pg", "-U", "cdc_e2e",
        "-d", "cdc_source", "-c", "SELECT 1", timeout=30)
    return process.returncode == 0 and process.stdout.strip() == "1"


def _owned_pg_slots() -> list[str]:
    return [name for name in pg_slot_names() if name.startswith("cdc_")]


def scenario_credential_rotation(ctx: Context) -> dict:
    """A REAL credential rotation keeps generation/offset and resumes delivery.

    The fixture role's password is changed at the source, the platform
    connection is rotated to the new credential through the API and
    re-tested, and one committed change must then be delivered exactly once
    on the SAME generation and slot. The original fixture credential is
    always restored (source first, then the platform connection).
    """
    entry = ctx.state["pg"]
    connection_id = entry["connection_id"]
    item = "item-10"
    instance = ctx.instance_id("pg", item)
    original_url = SOURCE_URLS["pg"]
    rotated_url = original_url.replace(
        f":{ORIGINAL_PG_PASSWORD}@", f":{ROTATED_PG_PASSWORD}@")
    evidence: dict[str, Any] = {"item": item}

    def healthy():
        _status, payload = ctx.asset_cdc("pg")
        return bool(payload.get("healthy") and payload.get("status") == "active")

    def apply(url: str) -> dict:
        patch_status, _payload = ctx.api.patch(
            f"/api/v2/connections/{connection_id}/config",
            {"config": {"connection_string": url}})
        test_status, test = ctx.api.request(
            "POST", f"/api/v2/connections/{connection_id}/test", timeout=120.0)
        return {"patch_status": patch_status, "test_status": test_status,
                "test_success": bool(isinstance(test, dict)
                                     and test.get("success"))}

    # Preconditions, verified before anything is changed (fail closed): the
    # runner must keep a password-independent path to restore the role, and
    # the network path must really enforce the password.
    local_trust = int(source_sql(
        "source-pg", "SELECT count(*) FROM pg_hba_file_rules WHERE "
        "type = 'local' AND auth_method = 'trust';").strip() or 0)
    evidence["preconditions"] = {
        "local_trust_rules": local_trust,
        "original_password_accepted": _pg_network_auth(ORIGINAL_PG_PASSWORD),
        "rotated_password_accepted": _pg_network_auth(ROTATED_PG_PASSWORD),
    }
    if (not local_trust
            or not evidence["preconditions"]["original_password_accepted"]
            or evidence["preconditions"]["rotated_password_accepted"]):
        return {"status": "failed",
                "detail": "rotation preconditions not met; nothing was changed",
                "evidence": evidence}

    # item-10 may already be 0 (commit-order scenario): return it to 100
    # through a real delivered change before taking the baseline.
    current = source_sql(
        "source-pg",
        f"SELECT balance FROM inventory WHERE source_id='{item}';").strip()
    evidence["balance_before"] = current
    if current != "100":
        reset_base = len(external_facts(ctx.db, PG_ONTOLOGY, instance))
        ctx.commit_source("source-pg",
            f"UPDATE inventory SET balance=100 WHERE source_id='{item}';")
        ok_reset, _, _ = ctx.wait_facts(PG_ONTOLOGY, instance, reset_base + 1)
        if not ok_reset:
            return {"status": "failed",
                    "detail": "reset of the rotation row was not delivered",
                    "evidence": evidence}
    base_facts = len(external_facts(ctx.db, PG_ONTOLOGY, instance))
    base_notices = notification_count(ctx.db, PG_ONTOLOGY)
    before = source_state(ctx.db, connection_id)
    slots_before = _owned_pg_slots()

    failure = None
    outcome: dict[str, Any] = {}
    try:
        source_sql("source-pg",
                   f"ALTER ROLE cdc_e2e PASSWORD '{ROTATED_PG_PASSWORD}';")
        outcome["source_rotated"] = {
            "original_password_accepted": _pg_network_auth(ORIGINAL_PG_PASSWORD),
            "rotated_password_accepted": _pg_network_auth(ROTATED_PG_PASSWORD)}
        outcome["platform_rotated"] = apply(rotated_url)
        ok_health, health_wait, _ = wait_until(
            healthy, RESTART_TIMEOUT, 2.0, describe="healthy after rotation")
        after = source_state(ctx.db, connection_id)
        ctx.commit_source("source-pg",
            f"UPDATE inventory SET balance=0 WHERE source_id='{item}';")
        ok_delivery, _, _ = ctx.wait_facts(
            PG_ONTOLOGY, instance, base_facts + 1, timeout=RESTART_TIMEOUT)
        ok_notice, _, _ = ctx.wait_notifications(
            PG_ONTOLOGY, base_notices + 1, timeout=RESTART_TIMEOUT)
        time.sleep(5)  # settle: a replay would surface as an extra fact/notice
        outcome.update({
            "healthy_after_rotation": ok_health,
            "health_wait_seconds": round(health_wait, 1),
            "generation_before": before["generation"],
            "generation_after": after["generation"],
            "position_before": before["last_position"],
            "position_after": after["last_position"],
            "slots_before": slots_before, "slots_after": _owned_pg_slots(),
            "delivered": ok_delivery, "notified": ok_notice,
            "facts_delta": len(external_facts(
                ctx.db, PG_ONTOLOGY, instance)) - base_facts,
            "notifications_delta": notification_count(
                ctx.db, PG_ONTOLOGY) - base_notices,
        })
    except Exception as error:  # noqa: BLE001 - restore must still run
        failure = f"{type(error).__name__}: {error}"[:500]
    finally:
        # Source first (password-independent unix socket), then the platform.
        source_sql("source-pg",
                   f"ALTER ROLE cdc_e2e PASSWORD '{ORIGINAL_PG_PASSWORD}';")
        restore = apply(original_url)
        ok_restored, restore_wait, _ = wait_until(
            healthy, RESTART_TIMEOUT, 2.0, describe="healthy after restore")
        evidence["restore"] = {
            **restore, "healthy": ok_restored,
            "wait_seconds": round(restore_wait, 1),
            "original_password_accepted": _pg_network_auth(ORIGINAL_PG_PASSWORD),
            "rotated_password_accepted": _pg_network_auth(ROTATED_PG_PASSWORD),
            "generation": (source_state(ctx.db, connection_id) or {}).get(
                "generation"),
        }
    evidence.update(outcome)
    evidence["failure"] = failure
    restored = evidence["restore"]
    ok = (failure is None
          and outcome.get("source_rotated") == {
              "original_password_accepted": False,
              "rotated_password_accepted": True}
          and outcome["platform_rotated"] == {
              "patch_status": 200, "test_status": 200, "test_success": True}
          and outcome["healthy_after_rotation"]
          and outcome["generation_after"] == outcome["generation_before"]
          and outcome["slots_after"] == outcome["slots_before"]
          and len(outcome["slots_before"]) == 1
          and outcome["delivered"] and outcome["notified"]
          and outcome["facts_delta"] == 1
          and outcome["notifications_delta"] == 1
          and restored["patch_status"] == 200 and restored["test_success"]
          and restored["healthy"]
          and restored["original_password_accepted"]
          and not restored["rotated_password_accepted"]
          and restored["generation"] == outcome["generation_before"])
    return {"status": "passed" if ok else "failed",
            "detail": "real credential rotation kept generation, slot and "
                      "offset; delivery resumed exactly once; fixture restored"
                      if ok else "credential rotation contract violated",
            "evidence": evidence}


def scenario_restart_resume(ctx: Context) -> dict:
    """Executor restart keeps slot/offsets; downtime commit delivered once."""
    entry = ctx.state["pg"]
    item = "item-05"
    instance = ctx.instance_id("pg", item)
    base_facts = len(external_facts(ctx.db, PG_ONTOLOGY, instance))
    base_notices = notification_count(ctx.db, PG_ONTOLOGY)
    slots_before = [name for name in pg_slot_names() if name.startswith("cdc_")]

    stop_own_container("cdc-executor")
    slot_alive_during_downtime = [
        name for name in pg_slot_names() if name.startswith("cdc_")]
    ctx.commit_source("source-pg",
        f"UPDATE inventory SET balance=0 WHERE source_id='{item}';")
    start_own_container("cdc-executor")
    ok_health, health_wait, last = wait_until(
        lambda: (lambda sp: (sp[1].get("healthy") and
                             sp[1].get("status") == "active"))(
            ctx.asset_cdc("pg")),
        RESTART_TIMEOUT, 2.0, describe="pg healthy after restart")
    ok_delivery, delivery_wait, _ = ctx.wait_facts(
        PG_ONTOLOGY, instance, base_facts + 1, timeout=RESTART_TIMEOUT)
    time.sleep(6)  # let any offset-flush-miss redelivery arrive before counting
    rows = external_facts(ctx.db, PG_ONTOLOGY, instance)[base_facts:]
    notices = notification_count(ctx.db, PG_ONTOLOGY) - base_notices
    distinct = len({row["source_event_id"] for row in rows})
    slots_after = [name for name in pg_slot_names() if name.startswith("cdc_")]

    # At-least-once redelivery may re-POST the same event (acked duplicate);
    # persisted facts and notifications must stay exactly one.
    ok = (ok_health and ok_delivery and len(rows) == 1 and distinct == 1
          and notices == 1 and slots_before == slot_alive_during_downtime
          and slots_after == slots_before)
    return {"status": "passed" if ok else "failed",
            "detail": "restart kept slot+offset; downtime commit exactly once"
                      if ok else "restart/resume contract violated",
            "evidence": {
                "slots_before": slots_before,
                "slots_during_downtime": slot_alive_during_downtime,
                "slots_after": slots_after,
                "facts_delta": len(rows),
                "distinct_source_event_ids": distinct,
                "notifications_delta": notices,
                "health_wait_seconds": round(health_wait, 1),
                "delivery_wait_seconds": round(delivery_wait, 1),
                "redelivery_window_note":
                    "executor stopped <5s after prior acks; any missed offset "
                    "flush must still yield exactly one fact/notice",
            }}


def _await_source_status(ctx: Context, connection_id: str, status: str,
                         error_code: str | None, timeout: float):
    def check():
        state = source_state(ctx.db, connection_id)
        if state is None or state["status"] != status:
            return None
        if error_code is not None and state["last_error_code"] != error_code:
            return None
        return state

    return wait_until(check, timeout, 2.0, describe=f"source {status}")


def scenario_source_gap(ctx: Context) -> dict:
    """Dropped slot with retained offsets surfaces explicit gap, never resnap."""
    entry = ctx.state["pg"]
    connection_id = entry["connection_id"]
    owned_slot = [name for name in pg_slot_names()
                  if name.startswith("cdc_")][0]

    stop_own_container("cdc-executor")
    source_sql("source-pg", f"SELECT pg_drop_replication_slot('{owned_slot}');")
    dropped = owned_slot not in pg_slot_names()
    start_own_container("cdc-executor")
    ok_gap, gap_wait, state = _await_source_status(
        ctx, connection_id, "gap", "SOURCE_LOG_GAP", RESTART_TIMEOUT)
    status_code, cdc_payload = ctx.asset_cdc("pg")

    # Recover deterministically: remove desired -> dormant cleanup -> restore.
    clear_release_pointer(ctx.db, PG_ONTOLOGY)
    ok_dormant, dormant_wait, drained = wait_until(
        lambda: (lambda s: s is not None and s["status"] == "stopped"
                 and s["cleanup_completed_at"] is not None)(
            source_state(ctx.db, connection_id)),
        CLEANUP_TIMEOUT, 2.0, describe="dormant cleanup")
    slots_after_cleanup = pg_slot_names()
    publications_after = pg_publication_names()
    set_release_pointer(ctx.db, PG_ONTOLOGY, entry["release_id"])
    ok_react, react_wait, last = wait_until(
        lambda: (lambda sp: (sp[1].get("healthy") and
                             sp[1].get("status") == "active"))(
            ctx.asset_cdc("pg")),
        ACTIVATION_TIMEOUT, 2.0, describe="pg healthy after republish")
    new_state = source_state(ctx.db, connection_id)
    ctx.state["pg"]["generation"] = new_state["generation"]
    new_slot = [name for name in pg_slot_names() if name.startswith("cdc_")]

    ok = (dropped and ok_gap and state is not None
          and cdc_payload.get("healthy") is False
          and ok_dormant and ok_react
          and not [name for name in slots_after_cleanup
                   if name.startswith("cdc_")]
          and new_state["generation"] >= 1 and len(new_slot) == 1)
    return {"status": "passed" if ok else "failed",
            "detail": "dropped slot -> explicit gap status, dormant cleanup, "
                      "explicit new generation" if ok else "gap contract violated",
            "evidence": {
                "dropped_slot": owned_slot,
                "gap_status": None if state is None else {
                    "status": state["status"],
                    "last_error_code": state["last_error_code"]},
                "gap_wait_seconds": round(gap_wait, 1),
                "asset_cdc_unhealthy": not cdc_payload.get("healthy"),
                "dormant_cleanup_wait_seconds": round(dormant_wait, 1),
                "slots_after_cleanup": slots_after_cleanup,
                "publications_after_cleanup": publications_after,
                "generation_after_republish": new_state["generation"],
                "new_slot": new_slot,
                "reactivation_wait_seconds": round(react_wait, 1),
            }}


def scenario_ddl_drift(ctx: Context) -> dict:
    """A mapped column disappearing surfaces SCHEMA_DRIFT; restoring resumes.

    The mapped column is removed by an order-preserving RENAME: the physical
    fingerprint is a digest over the ORDERED column list, so DROP + ADD would
    move the column and leave the table permanently drifted from the frozen
    release contract. No row is written while the table is drifted: a change
    committed in that window is decoded with the drifted relation forever and
    could never be delivered under the frozen contract. Drift is therefore
    exercised where the runtime checks it without a row event: the preflight
    of a child (re)start, forced through the API (a same-identity config
    write pauses the source and stops the child; a connection re-test asks
    for the resume).
    """
    entry = ctx.state["pg"]
    connection_id = entry["connection_id"]
    before = source_state(ctx.db, connection_id)
    slots_before = _owned_pg_slots()

    def retest() -> bool:
        status, test = ctx.api.request(
            "POST", f"/api/v2/connections/{connection_id}/test", timeout=120.0)
        return bool(status == 200 and isinstance(test, dict)
                    and test.get("success"))

    def healthy():
        _status, payload = ctx.asset_cdc("pg")
        return bool(payload.get("healthy") and payload.get("status") == "active")

    patch_status, _payload = ctx.api.patch(
        f"/api/v2/connections/{connection_id}/config",
        {"config": {"connection_string": SOURCE_URLS["pg"]}})
    ok_paused, _, _ = wait_until(
        lambda: (lambda s: s is not None and s["status"] == "paused")(
            source_state(ctx.db, connection_id)),
        EVENT_TIMEOUT, 1.0, describe="source paused")

    renamed = False
    ok_drift, drift_wait, drift_state, cdc_payload = False, 0.0, None, {}
    slots_during: list[str] = []
    try:
        source_sql("source-pg", "ALTER TABLE inventory RENAME COLUMN "
                                "source_status TO source_status_drift;")
        renamed = True
        drift_retest = retest()
        ok_drift, drift_wait, drift_state = wait_until(
            lambda: (lambda s: s if s is not None
                     and s["last_error_code"] == "SCHEMA_DRIFT" else None)(
                source_state(ctx.db, connection_id)),
            RESTART_TIMEOUT, 2.0, describe="schema drift")
        _status, cdc_payload = ctx.asset_cdc("pg")
        slots_during = _owned_pg_slots()
    finally:
        if renamed:
            source_sql("source-pg", "ALTER TABLE inventory RENAME COLUMN "
                                    "source_status_drift TO source_status;")

    # Resume is an operator action (re-test). A re-test that lands inside the
    # runtime's start backoff is honestly re-paused, so re-test until healthy.
    retests = 0
    started = time.monotonic()
    ok_recover = False
    while time.monotonic() - started <= RESTART_TIMEOUT:
        retest()
        retests += 1
        ok_recover, _, _ = wait_until(healthy, 20.0, 2.0, describe="recover")
        if ok_recover:
            break
    recover_wait = time.monotonic() - started
    after = source_state(ctx.db, connection_id)

    item = "item-01"
    instance = ctx.instance_id("pg", item)
    base_facts = len(external_facts(ctx.db, PG_ONTOLOGY, instance))
    target = "51" if source_sql(
        "source-pg",
        f"SELECT balance FROM inventory WHERE source_id='{item}';"
    ).strip() == "50" else "50"
    ctx.commit_source("source-pg",
        f"UPDATE inventory SET balance={target} WHERE source_id='{item}';")
    ok_delivery, _, _ = ctx.wait_facts(
        PG_ONTOLOGY, instance, base_facts + 1, timeout=RESTART_TIMEOUT)
    rows = external_facts(ctx.db, PG_ONTOLOGY, instance)[base_facts:]

    ok = (patch_status == 200 and ok_paused and ok_drift
          and cdc_payload.get("healthy") is False
          and slots_during == slots_before
          and ok_recover and ok_delivery and len(rows) == 1
          and after["generation"] == before["generation"]
          and _owned_pg_slots() == slots_before)
    return {"status": "passed" if ok else "failed",
            "detail": "mapped column removed -> explicit SCHEMA_DRIFT, never "
                      "streamed; restored + re-test -> same generation resumes"
                      if ok else "drift contract violated",
            "evidence": {
                "drift_method": "order-preserving RENAME (reversible), "
                                "detected by restart preflight, no row "
                                "written while drifted",
                "paused_before_ddl": ok_paused,
                "drift_observed": ok_drift,
                "drift_status": None if drift_state is None else {
                    "status": drift_state["status"],
                    "last_error_code": drift_state["last_error_code"]},
                "drift_wait_seconds": round(drift_wait, 1),
                "asset_cdc_during_drift": {
                    "healthy": cdc_payload.get("healthy"),
                    "status": cdc_payload.get("status"),
                    "last_error_code": cdc_payload.get("last_error_code")},
                "slot_kept_during_drift": slots_during == slots_before,
                "retests_until_healthy": retests,
                "recover_wait_seconds": round(recover_wait, 1),
                "generation_before": before["generation"],
                "generation_after": after["generation"],
                "post_recovery_delivery": len(rows),
            }}


def scenario_structured_409(ctx: Context) -> dict:
    """Active CDC blocks decommission + physical repoint with structured 409s."""
    pg_entry = ctx.state["pg"]
    asset_id = pg_entry["asset_id"]
    connection_id = pg_entry["connection_id"]

    status_asset, payload_asset = ctx.api.delete(
        f"/api/v2/integrated-datasets/{asset_id}")
    status_repoint, payload_repoint = ctx.api.patch(
        f"/api/v2/connections/{connection_id}/config",
        {"config": {"connection_string":
                    "postgresql+psycopg2://cdc_e2e:cdc_e2e_fixture"
                    "@source-mysql:3306/cdc_source"}})

    def code_of(payload):
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            return detail.get("code")
        return payload.get("code")

    asset_code = code_of(payload_asset)
    repoint_code = code_of(payload_repoint)
    ok = (status_asset == 409 and asset_code == "ASSET_DECOMMISSION_BLOCKED"
          and status_repoint == 409 and repoint_code == "CDC_REPOINT_BLOCKED")
    return {"status": "passed" if ok else "failed",
            "detail": "structured 409 for active decommission and repoint"
                      if ok else "409 contract violated",
            "evidence": {
                "delete_asset": {"status": status_asset, "code": asset_code},
                "repoint": {"status": status_repoint, "code": repoint_code},
            }}


def scenario_last_mapping_removal(ctx: Context) -> dict:
    """Removing the last released mapping stops the child and drops the slot.

    Reconcile retires the stopped subscription rows; the source control row
    stays as the dormant record (stopped + cleanup confirmed) and is only
    deleted by a connection decommission.
    """
    from app.data_channel.integrated.cdc_connector import runtime_names

    entry = ctx.state["pg"]
    connection_id = entry["connection_id"]
    before = source_state(ctx.db, connection_id)
    names = runtime_names(connection_id, before["generation"])
    owned = _owned_pg_slots()
    publications = [name for name in pg_publication_names()
                    if name.startswith("cdcpub_")]

    clear_release_pointer(ctx.db, PG_ONTOLOGY)
    ok_dormant, dormant_wait, state = wait_until(
        lambda: (lambda s: s if s is not None and s["status"] == "stopped"
                 and s["cleanup_completed_at"] is not None else None)(
            source_state(ctx.db, connection_id)),
        CLEANUP_TIMEOUT, 2.0, describe="dormant after removal")
    ok_retired, retired_wait, _ = wait_until(
        lambda: not subscriptions(ctx.db, PG_ONTOLOGY),
        CLEANUP_TIMEOUT, 2.0, describe="subscription rows retired")
    slots_after = _owned_pg_slots()
    pubs_after = [name for name in pg_publication_names()
                  if name.startswith("cdcpub_")]
    remaining_subs = subscriptions(ctx.db, PG_ONTOLOGY)
    _status, cdc_payload = ctx.asset_cdc("pg")
    final = source_state(ctx.db, connection_id)

    ok = (owned == [names["slot"]] and publications == [names["publication"]]
          and ok_dormant and ok_retired
          and not slots_after and not pubs_after and not remaining_subs
          and final is not None and final["status"] == "stopped"
          and final["cleanup_completed_at"] is not None
          and final["generation"] == before["generation"]
          and cdc_payload.get("enabled") is False
          and cdc_payload.get("healthy") is False)
    return {"status": "passed" if ok else "failed",
            "detail": "last mapping removal stopped the child, dropped the "
                      "owned slot+publication and left a dormant source"
                      if ok else "removal/cleanup contract violated",
            "evidence": {
                "expected_owned_names": {"slot": names["slot"],
                                         "publication": names["publication"]},
                "slot_before": owned, "publication_before": publications,
                "slots_after": slots_after, "publications_after": pubs_after,
                "dormant_wait_seconds": round(dormant_wait, 1),
                "subscriptions_retired_wait_seconds": round(retired_wait, 1),
                "remaining_subscriptions": len(remaining_subs),
                "source_after": None if final is None else {
                    "status": final["status"],
                    "generation": final["generation"],
                    "cleanup_confirmed": final["cleanup_completed_at"]
                    is not None},
                "asset_cdc": {"enabled": cdc_payload.get("enabled"),
                              "healthy": cdc_payload.get("healthy"),
                              "status": cdc_payload.get("status")},
            }}


def scenario_republish_after_dormant(ctx: Context) -> dict:
    """A publish after dormant opens exactly generation+1 with no_data coverage."""
    from app.data_channel.integrated.cdc_connector import runtime_names

    entry = ctx.state["pg"]
    connection_id = entry["connection_id"]
    dormant = source_state(ctx.db, connection_id)
    if (dormant is None or dormant["status"] != "stopped"
            or dormant["cleanup_completed_at"] is None):
        return {"status": "failed",
                "detail": "precondition: PG source must be dormant "
                          "(run last_mapping_removal_stops_and_cleans first)",
                "evidence": {"source": dormant}}
    previous_generation = dormant["generation"]
    old_names = runtime_names(connection_id, previous_generation)
    facts_before = len(external_facts(ctx.db, PG_ONTOLOGY))

    release_v2 = seed_release_v2(
        ctx.db, PG_ONTOLOGY, entry["descriptor"], ctx.api.admin_id)
    entry["release_id"] = release_v2

    ok_active, active_wait, _ = wait_until(
        lambda: (lambda sp: (sp[1].get("healthy") and
                             sp[1].get("status") == "active"
                             and sp[1].get("coverage_started_at")))(
            ctx.asset_cdc("pg")),
        ACTIVATION_TIMEOUT, 2.0, describe="v2 active")
    state = source_state(ctx.db, connection_id)
    entry["generation"] = state["generation"]
    new_names = runtime_names(connection_id, state["generation"])
    new_slot = _owned_pg_slots()
    facts_after_activation = len(external_facts(ctx.db, PG_ONTOLOGY))

    item = "item-01"
    instance = ctx.instance_id("pg", item)  # generation-salted: new namespace
    base_notices = notification_count(ctx.db, PG_ONTOLOGY)
    target = "1" if source_sql(
        "source-pg",
        f"SELECT balance FROM inventory WHERE source_id='{item}';"
    ).strip() == "0" else "0"
    ctx.commit_source("source-pg",
        f"UPDATE inventory SET balance={target} WHERE source_id='{item}';")
    ok_delivery, _, _ = wait_until(
        lambda: external_facts(ctx.db, PG_ONTOLOGY, instance) or None,
        RESTART_TIMEOUT, POLL_INTERVAL, describe="v2 delivery")
    expected_notices = 1 if target == "0" else 0
    if expected_notices:
        ctx.wait_notifications(
            PG_ONTOLOGY, base_notices + 1, timeout=RESTART_TIMEOUT)
    time.sleep(5)  # settle: replayed history would surface as extra facts
    delivered = external_facts(ctx.db, PG_ONTOLOGY, instance)
    facts_after = len(external_facts(ctx.db, PG_ONTOLOGY))
    notices = notification_count(ctx.db, PG_ONTOLOGY) - base_notices

    ok = (ok_active
          and state["generation"] == previous_generation + 1
          and new_slot == [new_names["slot"]]
          and new_names["slot"] != old_names["slot"]
          # no_data coverage: activation alone replays nothing
          and facts_after_activation == facts_before
          and ok_delivery and len(delivered) == 1
          and facts_after == facts_before + 1
          and notices == expected_notices)
    return {"status": "passed" if ok else "failed",
            "detail": "republish opened exactly generation+1 on a new owned "
                      "slot with fresh no_data coverage"
                      if ok else "republish contract violated",
            "evidence": {
                "release_v2": release_v2,
                "generation_before": previous_generation,
                "generation_after": state["generation"],
                "old_owned_slot": old_names["slot"],
                "new_owned_slot": new_names["slot"],
                "live_slots": new_slot,
                "facts_total_before": facts_before,
                "facts_total_after_activation": facts_after_activation,
                "facts_total_after": facts_after,
                "delivered_events": len(delivered),
                "committed_balance": target,
                "notifications_delta": notices,
                "expected_notifications": expected_notices,
                "activation_wait_seconds": round(active_wait, 1),
            }}


def scenario_no_object_instance(ctx: Context) -> dict:
    """External events never materialize ObjectInstances or lake mirrors."""
    instances = ctx.object_instance_count()
    mutations = int(ctx.db.scalar(
        f"SELECT count(*) FROM {table('mutations')}") or 0)
    sync_runs = {}
    for source in ("pg", "mysql"):
        status, payload = ctx.api.get(
            f"/api/v2/integrated-datasets/{ctx.state[source]['asset_id']}"
            "/sync-runs")
        items = payload.get("items", []) if isinstance(payload, dict) else []
        sync_runs[source] = len(items)
    pending = int(ctx.db.scalar(
        f"SELECT count(*) FROM {table('outbox')} "
        "WHERE status IN ('cdc_pending','pending') "
        "AND available_at < now() - interval '60 seconds'") or 0)
    ok = instances == 0 and mutations == 0 and not any(
        sync_runs.values()) and pending == 0
    return {"status": "passed" if ok else "failed",
            "detail": "zero ObjectInstance, zero lake mirror, outbox drained"
                      if ok else "materialization/lease contract violated",
            "evidence": {
                "object_instances": instances,
                "integrated_mutations": mutations,
                "sync_runs": sync_runs,
                "stuck_pending_outbox_rows": pending,
            }}


SCENARIOS: dict[str, Callable[[Context], dict]] = {
    "no_history": scenario_no_history,
    "separate_commits_same_timestamp": scenario_separate_commits,
    "multi_field_one_row_one_notification": scenario_multi_field,
    "rollback_no_event": scenario_rollback,
    "one_transaction_intermediate_honest": scenario_one_transaction,
    "notification_provenance": scenario_provenance,
    "latency_p95_commit_to_notification": scenario_latency,
    "pg_commit_order_no_loss": scenario_pg_commit_order,
    "mysql_same_contract": scenario_mysql_contract,
    "credential_rotation_resumes_offset": scenario_credential_rotation,
    "restart_resume_offsets": scenario_restart_resume,
    "source_gap_explicit_unhealthy": scenario_source_gap,
    "ddl_drift_explicit_unhealthy": scenario_ddl_drift,
    "repoint_and_decommission_structured_409": scenario_structured_409,
    "last_mapping_removal_stops_and_cleans": scenario_last_mapping_removal,
    "republish_after_dormant_new_generation": scenario_republish_after_dormant,
    "zero_object_instance_no_lake_mirror": scenario_no_object_instance,
}


def cmd_run(selected: list[str] | None) -> dict:
    sanitize_environment()
    names = selected or list(SCENARIO_ORDER)
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenarios: {unknown}; "
                         f"known: {sorted(SCENARIOS)}")
    ctx = Context()
    # The run subcommand re-uses the setup established by `setup`/`all`; rebind
    # the live state from the platform (release pointers, subscriptions).
    _bind_context(ctx)
    summary: dict[str, Any] = {"started_at": now_iso(), "results": {}}
    write_artifact("summary.json", summary)
    for name in names:
        started = time.monotonic()
        try:
            outcome = SCENARIOS[name](ctx)
        except ContainerOpsNotAllowed as error:
            outcome = {"status": "blocked", "detail": str(error)}
        except Exception as error:  # noqa: BLE001 - evidence, not silence
            traceback.print_exc()
            outcome = {"status": "failed",
                       "detail": f"{type(error).__name__}: {error}"[:500]}
        outcome["wall_seconds"] = round(time.monotonic() - started, 1)
        ctx.record(name, outcome)
        summary["results"][name] = {"status": outcome["status"],
                                    "detail": outcome.get("detail")}
        if outcome["status"] == "failed":
            summary["not_run"] = names[names.index(name) + 1:]
            break
    summary["finished_at"] = now_iso()
    statuses = [value["status"] for value in ctx.results.values()]
    if not statuses:
        summary["overall"] = "empty"
    elif any(s == "failed" for s in statuses):
        summary["overall"] = "failed"
    elif any(s != "passed" for s in statuses):
        summary["overall"] = "incomplete"  # blocked scenarios are never a pass
    else:
        summary["overall"] = "passed"
    summary["counts"] = {s: statuses.count(s) for s in sorted(set(statuses))}
    write_artifact("summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _bind_context(ctx: Context) -> None:
    """Reconstruct live run state from the platform DB and the API."""
    report_path = ART / "setup.json"
    if not report_path.exists():
        raise SystemExit("setup has not run; execute `setup` or `all` first")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "ok":
        raise SystemExit(f"setup artifact is not ok: {report.get('status')}")
    ready = ctx.api.wait_ready()
    if not ready["ready"]:
        raise SystemExit(f"backend not reachable: {ready}")
    ctx.api.login()
    ctx.state["started_at"] = report["started_at"]

    for source, ontology_id, conn_name in (
            ("pg", PG_ONTOLOGY, "cdc-acc-source-pg"),
            ("mysql", MYSQL_ONTOLOGY, "cdc-acc-source-mysql")):
        release = ctx.db.scalar(
            f"SELECT current_release_id FROM {table('projects')} "
            "WHERE id = :o",
            o=ontology_id)
        # Resolve connection + asset from the subscription/control rows.
        sub_rows = ctx.db.rows(
            "SELECT connection_id, asset_id FROM "
            f"{table('cdc_subscriptions')} WHERE ontology_id = :o "
            "ORDER BY created_at LIMIT 1", o=ontology_id)
        if sub_rows:
            connection_id = sub_rows[0]["connection_id"]
            asset_id = sub_rows[0]["asset_id"]
        else:
            # Dormant/removed control rows (late scenarios re-run): fall back
            # to the connection by name and the asset by its dataset link.
            status, connections = ctx.api.get("/api/v2/connections")
            match = next((item for item in connections
                          if item["name"] == conn_name), None)
            if match is None:
                raise SystemExit(f"connection {conn_name} missing")
            connection_id = match["id"]
            asset_id = ctx.db.scalar(
                f"SELECT id FROM {table('assets')} WHERE connection_id = :c "
                "AND table_name = 'inventory'", c=connection_id)
        state = source_state(ctx.db, connection_id)
        descriptor = (
            report["steps"][[s["step"] for s in report["steps"]].index(
                "release_seed")]["result"][source]["mapping_remote_descriptor"])
        ctx.state[source] = {
            "ontology_id": ontology_id,
            "release_id": release,
            "connection_id": connection_id,
            "asset_id": asset_id,
            "descriptor": descriptor,
            "generation": state["generation"] if state else 0,
        }


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    setup = sub.add_parser("setup")
    setup.add_argument("--skip-seed", action="store_true",
                       help="do not re-run driver seed-sources")
    run = sub.add_parser("run")
    run.add_argument("--scenario", action="append", dest="scenarios")
    run.add_argument("--list", action="store_true")
    everything = sub.add_parser("all")
    for entry in (run, everything):
        entry.add_argument(
            "--allow-container-ops", action="store_true",
            help="permit stop/start of this stack's own cdc-executor for the "
                 "restart/gap scenarios (blocked otherwise)")
    args = parser.parse_args()
    global _ALLOW_CONTAINER_OPS
    _ALLOW_CONTAINER_OPS = bool(getattr(args, "allow_container_ops", False))
    if args.command == "migrate":
        return 0 if cmd_migrate()["status"] == "ok" else 1
    if args.command == "setup":
        cmd_setup(skip_seed=args.skip_seed)
        return 0
    if args.command == "run":
        if args.list:
            for name in SCENARIO_ORDER:
                print(name)
            return 0
        summary = cmd_run(args.scenarios)
        return 0 if summary["overall"] == "passed" else 1
    if args.command == "all":
        cmd_setup()
        summary = cmd_run(None)
        return 0 if summary["overall"] == "passed" else 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
