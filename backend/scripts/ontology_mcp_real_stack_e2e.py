#!/usr/bin/env python3
"""Ontology MCP endpoint real-stack acceptance (isolated, re-runnable).

Role: exercise /api/v2/formal/ontologies/{oid}/mcp on a live loopback-only
stack through pi's own native MCP client (pi mcp list --json + bundled
pi-mcp McpClient, no LLM): tool discovery, schema/pagination, query rows/
aggregates/multipage, hidden-attribute non-leak, cross-ontology & cursor
rejections, wrong-password/rotation 401, v1->v2 rotation recovery.
Guards: OPENONTOLOGY_ONTOLOGY_MCP_E2E=1 + ENVIRONMENT=test, loopback-only
API/PG, per-run temp PI_CODING_AGENT_DIR (real ~/.pi untouched), run-scoped
synthetic fixtures removed on exit, secret-free JSON report.
Usage: OPENONTOLOGY_ONTOLOGY_MCP_E2E=1 ENVIRONMENT=test \
  .venv/bin/python scripts/ontology_mcp_real_stack_e2e.py \
  [--api-base URL] [--pg-dsn DSN] [--report-dir DIR] [--keep-temp]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg2
from passlib.context import CryptContext

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

SENTINEL_ENV = "OPENONTOLOGY_ONTOLOGY_MCP_E2E"
DEFAULT_API_BASE = "http://127.0.0.1:18036"
DEFAULT_PG_DSN = "postgresql://ontoprompt:ontoprompt@127.0.0.1:25432/ontoprompt"
DEFAULT_REPORT_DIR = REPO_DIR / ".artifacts" / "ontology-mcp"

# Same scheme stack as app.auth.service.pwd_context so HTTP login verifies the
# hashes this script inserts.
PWD_CONTEXT = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")

PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent"
PI_MCP_PACKAGE_NAME = "@earendil-works/pi-mcp"
PI_MIN_VERSION = "1.3"
PI_CLI_SERVER_NAME = "oo-ontology-a"
HEADER_USER = "X-OpenOntology-Username"
HEADER_PASS = "X-OpenOntology-Password"
MCP_PATH_TEMPLATE = "/api/v2/formal/ontologies/{oid}/mcp"
TEXT_LIMIT_BYTES = 20 * 1024  # pi truncates model-facing tool text above this
SUBPROCESS_TIMEOUT_S = 180

EXPECTED_TOOLS = ("ontology_schema", "ontology_query", "ontology_query_page")

# Rejection error codes — every value observed on the live stack and traced
# to its implementation (boundary allowlist, jsonschema forbid, snapshot
# session/release re-validation, release pointer guard).
EXPECTED_ERROR_CODES: dict[str, str | None] = {
    # observed live + traced to the boundary/validator implementation:
    "hidden_property_select": "property_forbidden",
    "cross_ontology_object_type": "object_type_forbidden",
    "source_field_injection": "invalid_arguments",  # jsonschema extra-forbid
    # agent_query/service.fetch_page: snapshot filtered by mcp:user:ontology
    # session -> foreign endpoint misses the snapshot (404 snapshot_not_found)
    "cross_endpoint_cursor": "snapshot_not_found",
    # fetch_page re-validates every snapshot release against the current
    # pointer -> 409 release_context_changed after v2 promote
    "stale_cursor_after_promote": "release_context_changed",
    "old_release_query": "release_context_changed",  # 409 + re-read guidance
}
# Hidden-attribute boundary on this script's own fo_agent_profiles row.
BOUNDARY_V1 = {"ot-order": ["order_no", "status", "amount"], "ot-supplier": ["sname"]}
BOUNDARY_V2 = {
    "ot-order": ["order_no", "status", "amount", "region"],
    "ot-supplier": ["sname"],
}

HIDDEN_PROP = "secret_token"
HIDDEN_VALUE_PREFIX = "SECRET-"
NEW_PROP_V2 = "region"
CROSS_OBJECT_TYPE = "ot-party"  # object type owned by ontology B

REPORT_STAGES = {
    "starting", "guards", "pi_discovery", "db_fixture", "http_fixtures",
    "publish_v1", "boundary_fixture", "pi_cli", "sdk_phase1", "password_rotation",
    "publish_v2", "sdk_phase3", "cleanup", "complete",
}
REPORT_CASES = {
    "guard_env_sentinels",
    "guard_loopback_only",
    "pi_binary_discovered",
    "pi_version_supported",
    "pi_mcp_module_resolved",
    "node_runtime_resolved",
    "db_users_created",
    "http_admin_login",
    "http_viewer_login",
    "ontology_a_created",
    "ontology_b_created",
    "structure_a_saved",
    "structure_b_saved",
    "publish_v1_a",
    "publish_v1_b",
    "boundary_applied_v1",
    "pi_cli_config_escaping",
    "pi_cli_list_connected",
    "sdk_initialize",
    "sdk_tool_discovery",
    "schema_bootstrap_v1",
    "schema_pagination_terminated",
    "schema_text_under_limit",
    "schema_filtered_by_type",
    "query_rows_correct",
    "query_aggregates_correct",
    "query_multipage_complete",
    "hidden_absent_from_outputs",
    "hidden_select_rejected",
    "cross_ontology_objecttype_rejected",
    "source_field_injection_rejected",
    "cross_endpoint_cursor_rejected",
    "wrong_password_http_401",
    "password_changed_http",
    "old_password_invalid_after_change",
    "new_password_accepted",
    "publish_v2",
    "boundary_applied_v2",
    "stale_cursor_rejected",
    "old_release_query_rejected",
    "schema_bootstrap_v2",
    "query_v2_recovered",
    "cleanup_users_deleted",
    "cleanup_ontologies_deleted",
    "cleanup_profiles_deleted",
    "cleanup_temp_dir_removed",
    "cleanup_processes_reaped",
    "sdk_query_release_required",
    "error_code_enum_match",
}
REPORT_FIELDS = {
    "schema_version", "run_id", "timestamp", "result", "stage", "versions",
    "counts", "cases", "observed_error_codes", "text_bytes", "cleanup",
    "failure_detail",
}

# ---------------------------------------------------------------- fixtures ---

ORDER_STATUSES = [
    ("paid", 100), ("pending", 200), ("paid", 100), ("pending", 200),
    ("paid", 100), ("pending", 200), ("paid", 100), ("pending", 200),
    ("paid", 100), ("pending", 200), ("cancelled", 300), ("cancelled", 300),
]
EXTRA_V2_STATUSES = [("shipped", 400), ("shipped", 400), ("shipped", 400)]


def _orders(v2: bool) -> list[dict[str, Any]]:
    rows = []
    total = len(ORDER_STATUSES) + (len(EXTRA_V2_STATUSES) if v2 else 0)
    statuses = list(ORDER_STATUSES) + (EXTRA_V2_STATUSES if v2 else [])
    for idx, (status, base) in enumerate(statuses, start=1):
        props: dict[str, Any] = {
            "order_no": f"SO-{idx:03d}",
            "status": status,
            "amount": base + idx,
            HIDDEN_PROP: f"{HIDDEN_VALUE_PREFIX}{idx:03d}",
        }
        if v2:
            props[NEW_PROP_V2] = "north" if idx % 2 else "south"
        rows.append(props)
    assert len(rows) == total
    return rows


def _expected_aggregates(v2: bool) -> list[dict[str, Any]]:
    agg: dict[str, list[int]] = {}
    for order in _orders(v2):
        agg.setdefault(order["status"], []).append(order["amount"])
    return [
        {"status": status, "cnt": len(vals), "total": sum(vals)}
        for status, vals in sorted(agg.items(), key=lambda kv: -sum(kv[1]))
    ]


def _expected_paid_order_nos(v2: bool) -> list[str]:
    return [o["order_no"] for o in _orders(v2) if o["status"] == "paid"]


# ------------------------------------------------------------- report/state --

class Run:
    def __init__(self, report_dir: Path) -> None:
        self.run_id = uuid.uuid4().hex[:8]
        self.report_dir = report_dir
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = report_dir / f"run-{self.run_id}.log"
        self.pid_path = report_dir / "pids.log"
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "result": "fail",
            "stage": "starting",
            "versions": {"pi": None, "pi_mcp": None, "node": None, "api": None},
            "counts": {
                "users_created": 0,
                "ontologies_created": 0,
                "instances_published_v1": len(ORDER_STATUSES),
                "instances_published_v2": len(ORDER_STATUSES) + len(EXTRA_V2_STATUSES),
                "tools_discovered": 0,
                "schema_pages": 0,
                "query_pages": 0,
                "rows_fetched": 0,
            },
            "cases": {name: False for name in sorted(REPORT_CASES)},
            "observed_error_codes": {name: None for name in EXPECTED_ERROR_CODES},
            "text_bytes": {"schema_max_page": 0, "query_max_page": 0},
            "cleanup": {
                "users_deleted": False,
                "ontologies_deleted": False,
                "profiles_deleted": False,
                "temp_dir_removed": False,
                "processes_reaped": False,
            },
            "failure_detail": None,
        }
        self.secrets: set[str] = set()
        self._log_file = self.log_path.open("w", encoding="utf-8")
        self.spawned: list[tuple[int, str]] = []

    def log(self, message: str) -> None:
        line = f"[oomcp-e2e {self.run_id}] {message}"
        print(line, flush=True)
        self._log_file.write(line + "\n")
        self._log_file.flush()

    def stage(self, name: str) -> None:
        self.report["stage"] = name
        self.log(f"--- stage: {name}")

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        if name not in REPORT_CASES:
            raise KeyError(f"unregistered case {name!r}")
        self.report["cases"][name] = bool(passed)
        tag = "PASS" if passed else "FAIL"
        suffix = f" :: {detail}" if detail else ""
        self.log(f"{tag} {name}{suffix}")
        return bool(passed)

    def register_secret(self, value: str | None) -> None:
        if value:
            self.secrets.add(value)

    def record_pid(self, pid: int, description: str) -> None:
        self.spawned.append((pid, description))
        with self.pid_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{pid}\t{description}\t{self.run_id}\n")

    def fail(self, detail: str) -> None:
        self.report["failure_detail"] = detail[:500]
        self.log(f"FATAL {detail}")

    def finish(self, status: str) -> dict[str, Any]:
        self.report["result"] = status
        self.report["stage"] = "complete"
        payload = json.dumps(self._sanitize(self.report), sort_keys=True, indent=2)
        for secret in self.secrets:
            if secret and secret in payload:
                # Must never happen: strip nothing silently — fail the run.
                self.report["result"] = "fail"
                self.report["failure_detail"] = "sanitizer: secret reached report"
                payload = json.dumps(
                    self._sanitize(self.report), sort_keys=True, indent=2)
                break
        out = self.report_dir / "mcp-real-stack-report.json"
        out.write_text(payload + "\n", encoding="utf-8")
        self.log(f"report written to {out} (result={self.report['result']})")
        self._log_file.close()
        return self.report

    @staticmethod
    def _sanitize(value: Any) -> Any:
        if isinstance(value, bool) or isinstance(value, int) or value is None:
            return value
        if isinstance(value, float):
            return round(value, 3)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return {str(k): Run._sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [Run._sanitize(v) for v in value]
        raise ValueError(f"unsupported report value {type(value)!r}")


def _assert_report_shape(report: dict[str, Any]) -> None:
    if set(report) != REPORT_FIELDS:
        raise ValueError("report field drift")
    if set(report["cases"]) != REPORT_CASES:
        raise ValueError("report case drift")


# ------------------------------------------------------------------- guards --

def probe_api(run: Run, api_base: str) -> bool:
    """Reachability probe that works direct AND behind the production Nginx
    path router: root-level /health/live and /openapi.json resolve to the SPA
    fallback when proxied, so probe a shared /api/ route instead (anonymous
    auth/profile is JSON 401 on both fronts)."""
    base = api_base.rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{base}/api/v1/auth/profile")
            json_ct = "json" in (resp.headers.get("content-type") or "")
            ok = resp.status_code in (401, 403) and json_ct
            openapi = client.get(f"{base}/openapi.json")
            if openapi.status_code == 200 and "json" in (
                    openapi.headers.get("content-type") or ""):
                run.report["versions"]["api"] = str(
                    (openapi.json() or {}).get("info", {}).get("version")
                    or "")
        run.log(f"api probe /api/v1/auth/profile={resp.status_code} "
                f"(json={json_ct}) openapi={openapi.status_code}")
        return ok
    except httpx.HTTPError as exc:
        run.log(f"api probe failed: {exc}")
        return False


def _host_is_loopback(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_loopback:
            return False
    return True


def check_guards(run: Run, api_base: str, pg_dsn: str) -> bool:
    run.stage("guards")
    ok_env = (
        os.environ.get(SENTINEL_ENV) == "1"
        and os.environ.get("ENVIRONMENT") == "test"
    )
    run.check("guard_env_sentinels", ok_env,
              "need OPENONTOLOGY_ONTOLOGY_MCP_E2E=1 and ENVIRONMENT=test")
    api_host = urlsplit(api_base).hostname or ""
    dsn_host = urlsplit(pg_dsn).hostname or ""
    ok_loop = (
        _host_is_loopback(api_host) and _host_is_loopback(dsn_host)
        and urlsplit(api_base).scheme in {"http", "https"}
    )
    run.check("guard_loopback_only", ok_loop,
              f"api host={api_host} pg host={dsn_host} must be loopback")
    return ok_env and ok_loop


# -------------------------------------------------------------- pi discovery --

def _read_package_name(pkg_dir: Path) -> tuple[str, str] | None:
    try:
        data = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("name")
    version = data.get("version")
    if isinstance(name, str) and isinstance(version, str):
        return name, version
    return None


def discover_pi(run: Run) -> dict[str, Any] | None:
    """Locate pi + its bundled pi-mcp client module + a node runtime.

    Resolution is fully dynamic (no personal paths): ``pi`` on PATH -> realpath
    -> walk up to the pi package root -> pi-mcp under adjacent node_modules.
    """
    run.stage("pi_discovery")
    pi_bin = shutil.which("pi")
    if not pi_bin:
        run.check("pi_binary_discovered", False, "pi not found on PATH")
        return None
    run.check("pi_binary_discovered", True)

    pi_real = Path(pi_bin).resolve()
    probe = pi_real.parent
    pi_pkg_dir: Path | None = None
    for _ in range(8):
        if (probe / "package.json").exists():
            meta = _read_package_name(probe)
            if meta and meta[0] == PI_PACKAGE_NAME:
                pi_pkg_dir = probe
                break
        if probe.parent == probe:
            break
        probe = probe.parent
    if pi_pkg_dir is None:
        run.check("pi_version_supported", False, "pi package root not resolved")
        return None

    version_cmd = [pi_bin, "--version"]
    try:
        proc = subprocess.run(version_cmd, capture_output=True, text=True,
                              timeout=30, cwd=str(REPO_DIR))
        pi_version = (proc.stdout or "").strip().splitlines()[0].strip()
    except (OSError, IndexError, subprocess.TimeoutExpired):
        pi_version = ""
    run.report["versions"]["pi"] = pi_version or None
    ok_version = pi_version.startswith(PI_MIN_VERSION)
    run.check("pi_version_supported", ok_version,
              f"pi --version -> {pi_version!r} (need {PI_MIN_VERSION}.x)")

    pi_mcp_leaf = PI_MCP_PACKAGE_NAME.split("/", 1)[1]
    candidates: list[Path] = []
    for base in (pi_pkg_dir, pi_pkg_dir.parent, pi_pkg_dir.parent.parent):
        candidates.append(base / "node_modules" / "@earendil-works" / pi_mcp_leaf)
    candidates.append(pi_pkg_dir.parent.parent / "@earendil-works" / pi_mcp_leaf)
    pi_mcp_dir: Path | None = None
    pi_mcp_version: str | None = None
    for cand in candidates:
        meta = _read_package_name(cand) if cand.exists() else None
        if meta and meta[0] == PI_MCP_PACKAGE_NAME:
            pi_mcp_dir = cand
            pi_mcp_version = meta[1]
            break
    run.report["versions"]["pi_mcp"] = pi_mcp_version
    ok_mcp = pi_mcp_dir is not None and (pi_mcp_version or "").startswith(PI_MIN_VERSION)
    run.check("pi_mcp_module_resolved", ok_mcp,
              f"pi-mcp at {pi_mcp_dir} version={pi_mcp_version}")
    if not ok_mcp or pi_mcp_dir is None:
        return None

    node_bin = Path(pi_bin).parent / "node"
    if not node_bin.exists():
        node_bin = Path(shutil.which("node") or "")
    node_version = ""
    if str(node_bin) and node_bin.exists():
        try:
            proc = subprocess.run([str(node_bin), "--version"], capture_output=True,
                                  text=True, timeout=30)
            node_version = (proc.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            node_version = ""
    run.report["versions"]["node"] = node_version or None
    ok_node = bool(node_version)
    run.check("node_runtime_resolved", ok_node, f"node -> {node_version}")
    if not ok_node:
        return None

    return {
        "pi_bin": pi_bin,
        "pi_pkg_dir": pi_pkg_dir,
        "pi_mcp_entry": str(pi_mcp_dir / "dist" / "index.js"),
        "node_bin": str(node_bin),
        "pi_version": pi_version,
    }


# -------------------------------------------------------------- DB fixtures --

def _pg_connect(pg_dsn: str):
    return psycopg2.connect(pg_dsn)


def create_users(run: Run, pg_dsn: str) -> dict[str, Any] | None:
    run.stage("db_fixture")
    suffix = run.run_id
    admin_name = f"oomcpe2e_admin_{suffix}"
    viewer_name = f"oomcpe2e_user_{suffix}"
    # Leading '!', '$', '!' and a literal '%20' prove byte-exact transport:
    # the endpoint uses header values as-is (never percent-decoded), and pi
    # config escaping ($$/$!) leaves '%' untouched through Nginx and backend.
    admin_pass = "!E2e$" + secrets.token_hex(8) + "%20$!w0rd"
    viewer_pass = "!E2e$" + secrets.token_hex(8) + "%20$!w0rd"
    viewer_pass_new = "E2e-" + secrets.token_hex(8) + "$$rot"
    for secret in (admin_pass, viewer_pass, viewer_pass_new):
        run.register_secret(secret)
    admin_id, viewer_id = str(uuid.uuid4()), str(uuid.uuid4())
    conn = _pg_connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, username, email, password_hash, role,"
                " is_active, token_version, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s,'admin',true,0,now(),now()),"
                " (%s,%s,%s,%s,'viewer',true,0,now(),now())",
                (admin_id, admin_name, f"{admin_name}@oomcp-e2e.invalid",
                 PWD_CONTEXT.hash(admin_pass),
                 viewer_id, viewer_name, f"{viewer_name}@oomcp-e2e.invalid",
                 PWD_CONTEXT.hash(viewer_pass)),
            )
        conn.commit()
    finally:
        conn.close()
    run.report["counts"]["users_created"] = 2
    run.check("db_users_created", True)
    for name in (admin_name, viewer_name):
        run.register_secret(name)
    return {
        "admin": {"id": admin_id, "username": admin_name, "password": admin_pass},
        "viewer": {"id": viewer_id, "username": viewer_name,
                   "password": viewer_pass, "password_new": viewer_pass_new},
    }


def apply_boundary(run: Run, pg_dsn: str, ontology_id: str,
                   allowed: dict[str, list[str]], case: str) -> None:
    """IP-02: restrict MCP-visible properties on this script's own ontology."""
    conn = _pg_connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fo_agent_profiles (id, ontology_id, enabled,"
                " allowed_object_type_ids, allowed_link_type_ids,"
                " allowed_action_ids, allowed_properties,"
                " allow_action_proposals, max_rows_per_query, max_steps,"
                " system_prompt_extra, created_at, updated_at)"
                " VALUES (%s,%s,true,NULL,NULL,'[]'::json,%s::json,"
                " false,200,8,'',now(),now())"
                " ON CONFLICT (ontology_id) DO UPDATE SET"
                " allowed_properties=EXCLUDED.allowed_properties,"
                " enabled=true, updated_at=now()",
                (str(uuid.uuid4()), ontology_id, json.dumps(allowed)),
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT allowed_properties FROM fo_agent_profiles"
                " WHERE ontology_id=%s", (ontology_id,))
            row = cur.fetchone()
        ok = row is not None and row[0] == allowed
        run.check(case, ok)
    finally:
        conn.close()


def insert_release_instances(run: Run, pg_dsn: str, ontology_id: str,
                             release_id: str,
                             orders: list[dict[str, Any]] | None) -> bool:
    """Synthetic release data, direct-DB (sanctioned isolated fixture).

    Promote rebuilds instances only from mapping datasets; an unmapped
    ontology's release legitimately has zero rows, so the structure ships
    through the formal front door and the synthetic business data is inserted
    here, pinned to that release.  ``orders=None`` removes this run's rows
    only (used before v2 promote so no unattributed runtime state exists for
    the release gate to protect).  Only rows with this run's id prefix are
    ever touched.
    """
    prefix = f"oomcp{run.run_id}"
    conn = _pg_connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM fo_object_instances WHERE id LIKE %s",
                (prefix + "%",))
            if orders:
                rows = [
                    (f"{prefix}o{idx:02d}", ontology_id, release_id,
                     "ot-order", json.dumps(props), "{}", "manual")
                    for idx, props in enumerate(orders, start=1)
                ]
                cur.executemany(
                    "INSERT INTO fo_object_instances (id, ontology_id,"
                    " ontology_release_id, object_type_id, properties,"
                    " computed, source, created_at, updated_at)"
                    " VALUES (%s,%s,%s,%s,%s::json,%s::json,%s,now(),now())",
                    rows)
        conn.commit()
        return True
    except psycopg2.Error as exc:
        run.log(f"instance fixture failed: {exc.pgcode}")
        conn.rollback()
        return False
    finally:
        conn.close()


def cleanup_db(run: Run, pg_dsn: str, users: dict[str, Any],
               ontology_ids: list[str]) -> None:
    conn = _pg_connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM fo_object_instances WHERE id LIKE %s",
                (f"oomcp{run.run_id}%",))
            cur.execute(
                "DELETE FROM fo_agent_profiles WHERE ontology_id = ANY(%s)",
                (ontology_ids,))
            profiles = cur.rowcount
            ok_profiles = profiles >= (1 if ontology_ids else 0)
            run.check("cleanup_profiles_deleted", ok_profiles,
                      f"rows={profiles}")
            run.report["cleanup"]["profiles_deleted"] = ok_profiles
        conn.commit()
    finally:
        conn.close()
    ok_users = False
    try:
        conn = _pg_connect(pg_dsn)
        with conn.cursor() as cur:
            # First use of a legacy domain on a fresh stack creates the
            # domains row with created_by = this run's admin; hand it back to
            # the fixture admin so the synthetic user can be deleted.
            cur.execute(
                "UPDATE domains SET created_by ="
                " (SELECT id FROM users WHERE username='admin' LIMIT 1)"
                " WHERE created_by IN (%s,%s)",
                (users["admin"]["id"], users["viewer"]["id"]))
            # Strictly this run's two synthetic users (by exact id) — a
            # concurrent run of the same script must never be affected.
            cur.execute(
                "DELETE FROM users WHERE id IN (%s,%s)",
                (users["admin"]["id"], users["viewer"]["id"]))
            ok_users = cur.rowcount == 2
        conn.commit()
    except psycopg2.Error as exc:
        run.log(f"user cleanup failed: {exc.pgcode}")
        conn.rollback()
        ok_users = False
    finally:
        conn.close()
    run.check("cleanup_users_deleted", ok_users)
    run.report["cleanup"]["users_deleted"] = ok_users


# ------------------------------------------------------------- HTTP fixtures --

class Api:
    def __init__(self, run: Run, base: str) -> None:
        self.run = run
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=60.0)

    def close(self) -> None:
        self.client.close()

    def login(self, username: str, password: str) -> str:
        resp = self.client.post(f"{self.base}/api/v1/auth/login",
                                json={"username": username, "password": password})
        resp.raise_for_status()
        token = resp.json()["data"]["access_token"]
        self.run.register_secret(token)
        return token

    def authed(self, token: str, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        return self.client.request(method, f"{self.base}{path}",
                                   headers=headers, **kw)

    def create_ontology(self, token: str, name: str) -> str:
        resp = self.authed(token, "POST", "/api/v1/ontologies",
                           json={"name": name, "domain": "其他",
                                 "description": "ontology mcp e2e fixture"})
        resp.raise_for_status()
        return resp.json()["data"]["id"]

    def put_full(self, token: str, oid: str, body: dict[str, Any]) -> None:
        resp = self.authed(token, "PUT",
                           f"/api/v2/formal/ontologies/{oid}/full", json=body)
        resp.raise_for_status()

    def publish(self, token: str, oid: str, label: str,
                structure: dict[str, Any] | None = None) -> dict[str, Any]:
        """Full front-door publish: draft -> workspace -> semantic -> trial ->
        impact -> promote.  Published structures reject direct PUT /full, so
        every structure change (v1 and v2) rides the draft workspace.  HTTP
        error bodies (gate codes) surface verbatim in the raised message."""
        from app.exploration.reverse_projection import project_snapshot_to_canvas

        def _ok(resp: httpx.Response, step: str) -> dict[str, Any]:
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"{step} HTTP {resp.status_code}: {resp.text[:280]}")
            return resp.json()["data"]

        prefix = f"/api/v2/ontologies/{oid}"
        source_id = _ok(self.authed(token, "GET",
                                    f"{prefix}/current-release/workspace"),
                        "current-release")["versionId"]
        draft_id = _ok(self.authed(
            token, "POST", f"{prefix}/versions/{source_id}/drafts",
            json={"versionLabel": label}), "drafts")["id"]

        if structure is None:
            structure = _ok(self.authed(
                token, "GET", f"/api/v2/formal/ontologies/{oid}/full"),
                "get-full")
        workspace = _ok(self.authed(
            token, "GET", f"{prefix}/versions/{draft_id}/workspace"),
            "workspace-get")
        saved = _ok(self.authed(
            token, "PUT", f"{prefix}/versions/{draft_id}/workspace",
            json={**structure, "baseRevision": workspace["revision"]}),
            "workspace-put")
        workspace = _ok(self.authed(
            token, "GET", f"{prefix}/versions/{draft_id}/workspace"),
            "workspace-reget")
        _ok(self.authed(
            token, "PUT", f"{prefix}/versions/{draft_id}/semantic",
            json={
                "baseRevision": saved["revision"],
                "canvas": project_snapshot_to_canvas(workspace),
                "documentMd": f"# {label}\n\nontology mcp e2e fixture\n",
                "documentTitle": label,
            }), "semantic")

        trial_data = _ok(self.authed(
            token, "POST", f"{prefix}/versions/{draft_id}/trial-runs",
            json={}), "trial-runs")
        if trial_data.get("status") != "passed":
            raise RuntimeError(f"trial run not passed: {trial_data}")
        impact_data = _ok(self.authed(
            token, "GET", f"{prefix}/versions/{draft_id}/impact"), "impact")
        release = _ok(self.authed(
            token, "POST", f"{prefix}/versions/{draft_id}/promote",
            json={"trialRunId": trial_data["id"],
                  "impactHash": impact_data["impactHash"]}), "promote")
        if release.get("node_kind") != "release":
            raise RuntimeError(f"promote did not produce a release: {release}")
        return release

    def delete_ontology(self, token: str, oid: str) -> bool:
        resp = self.authed(token, "DELETE", f"/api/v1/ontologies/{oid}")
        return resp.status_code == 204

    def change_password(self, token: str, current_pw: str, new_pw: str) -> bool:
        resp = self.client.put(
            f"{self.base}/api/v1/auth/password",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": current_pw, "new_password": new_pw})
        return resp.status_code == 200


def structure_a(v2: bool) -> dict[str, Any]:
    orders = _orders(v2)
    order_props = [
        {"id": "p1", "name": "order_no", "displayName": "订单号",
         "type": "string", "required": True},
        {"id": "p2", "name": "status", "displayName": "状态", "type": "string"},
        {"id": "p3", "name": "amount", "displayName": "金额", "type": "number"},
        {"id": "p4", "name": HIDDEN_PROP, "displayName": "密钥",
         "type": "string"},
    ]
    if v2:
        order_props.append(
            {"id": "p5", "name": NEW_PROP_V2, "displayName": "区域",
             "type": "string"})
    return {
        "objectTypes": [
            {"id": "ot-order", "name": "Order", "displayName": "订单",
             "primaryKey": "order_no", "properties": order_props,
             "positionX": 0, "positionY": 0},
            {"id": "ot-supplier", "name": "Supplier", "displayName": "供应商",
             "primaryKey": "sname",
             "properties": [
                 {"id": "p6", "name": "sname", "displayName": "名称",
                  "type": "string", "required": True}],
             "positionX": 0, "positionY": 0},
        ],
        "linkTypes": [
            {"id": "lt-1", "name": "order_supplier", "displayName": "订单-供应商",
             "sourceObjectTypeId": "ot-order", "targetObjectTypeId": "ot-supplier",
             "cardinality": "many-to-one"},
        ],
        "actions": [],
        "functions": [],
        "instances": [
            {"id": f"inst-o{idx:02d}", "objectTypeId": "ot-order",
             "properties": props, "computed": {}}
            for idx, props in enumerate(orders, start=1)
        ] + [
            {"id": "inst-s1", "objectTypeId": "ot-supplier",
             "properties": {"sname": "华南合成供应商"}, "computed": {}},
        ],
        "linkInstances": [
            {"id": "li-1", "linkTypeId": "lt-1", "sourceObjectId": "inst-o01",
             "targetObjectId": "inst-s1"},
        ],
    }


def structure_b() -> dict[str, Any]:
    return {
        "objectTypes": [
            {"id": CROSS_OBJECT_TYPE, "name": "Party", "displayName": "客户",
             "primaryKey": "party_key",
             "properties": [
                 {"id": "q1", "name": "party_key", "displayName": "客户键",
                  "type": "string", "required": True},
                 {"id": "q2", "name": "label", "displayName": "名称",
                  "type": "string"},
             ],
             "positionX": 0, "positionY": 0},
        ],
        "linkTypes": [],
        "actions": [],
        "functions": [],
        "instances": [
            {"id": "inst-p1", "objectTypeId": CROSS_OBJECT_TYPE,
             "properties": {"party_key": "PK-1", "label": "合成客户一"},
             "computed": {}},
            {"id": "inst-p2", "objectTypeId": CROSS_OBJECT_TYPE,
             "properties": {"party_key": "PK-2", "label": "合成客户二"},
             "computed": {}},
        ],
        "linkInstances": [],
    }


# ------------------------------------------------------------ pi CLI layer ---

def pi_escape_header_value(value: str) -> str:
    """pi mcp.json header value escaping: literal '$' -> '$$', literal '!' -> '$!'
    (verified against pi 1.3.0 dist/core/resolve-config-value.js)."""
    return value.replace("$", "$$").replace("!", "$!")


def _pi_unescape_literal(value: str) -> str:
    """Mirror of pi's template parser for fully-escaped literals: every '$' in
    the escaped text starts a '$$' or '$!' pair; no env/command forms remain."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "$" and index + 1 < len(value) and value[index + 1] in "$!":
            out.append(value[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def run_pi_cli_list(run: Run, pi: dict[str, Any], api: Api, oid_a: str,
                    viewer: dict[str, Any], pi_dir: Path) -> bool:
    run.stage("pi_cli")
    url = api.base + MCP_PATH_TEMPLATE.format(oid=oid_a)
    escaped_user = pi_escape_header_value(viewer["username"])
    escaped_pass = pi_escape_header_value(viewer["password"])
    config = {
        "mcpServers": {
            PI_CLI_SERVER_NAME: {
                "url": url,
                "headers": {
                    HEADER_USER: escaped_user,
                    HEADER_PASS: escaped_pass,
                },
                "timeout": 60,
            }
        }
    }
    (pi_dir / "mcp.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8")
    ok_escape = (
        _pi_unescape_literal(escaped_user) == viewer["username"]
        and _pi_unescape_literal(escaped_pass) == viewer["password"]
    )
    run.check("pi_cli_config_escaping", ok_escape)

    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(pi_dir)
    proc = subprocess.Popen(
        [pi["pi_bin"], "mcp", "list", "--json"],
        cwd=str(pi_dir), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    run.record_pid(proc.pid, "pi mcp list")
    try:
        out, err = proc.communicate(timeout=SUBPROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        run.check("pi_cli_list_connected", False, "pi mcp list timed out")
        return False
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        run.check("pi_cli_list_connected", False,
                  f"pi mcp list output not JSON: {(out or err)[:200]}")
        return False
    servers = payload.get("servers") or []
    entry = next((s for s in servers if s.get("name") == PI_CLI_SERVER_NAME), None)
    if entry is None:
        run.check("pi_cli_list_connected", False, "server entry missing")
        return False
    tools = entry.get("tools") or []
    run.report["counts"]["tools_discovered"] = len(tools)
    ok = (
        entry.get("state") == "connected"
        and proc.returncode == 0
        and set(tools) == set(EXPECTED_TOOLS)
    )
    detail = f"state={entry.get('state')} rc={proc.returncode} tools={tools}"
    if entry.get("error"):
        detail += f" error={str(entry['error'])[:160]}"
    return run.check("pi_cli_list_connected", ok, detail)


# ------------------------------------------------------------ pi-mcp SDK -----

NODE_RUNNER = r"""
/* ontology-mcp e2e runner — drives pi's own MCP client (pi-mcp). No LLM. */
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [,, inputPath, outputPath] = process.argv;
const input = JSON.parse(readFileSync(inputPath, "utf8"));
const { McpClient, StreamableHttpTransport, McpHttpError, McpError } =
  await import(pathToFileURL(process.env.OO_PIMCP_ENTRY).href);

const out = { checks: [], evidence: {}, codes: {} };
const TEXT_LIMIT = input.textLimitBytes ?? 20480;

function check(name, pass, detail) {
  out.checks.push({ name, pass: !!pass, detail: detail === undefined ? "" : String(detail).slice(0, 240) });
}
function classify(e) {
  return {
    name: e?.constructor?.name ?? "Error",
    status: typeof e?.status === "number" ? e.status : null,
    message: String(e?.message ?? e).slice(0, 200),
  };
}
function headers(user, pass) {
  return { [input.headerUser]: user, [input.headerPass]: pass };
}
async function connect(url, user, pass) {
  const client = new McpClient({
    name: "ontology-mcp-e2e", version: "1.0.0", requestTimeoutMs: 30000,
  });
  const transport = new StreamableHttpTransport({
    url, headers: headers(user, pass),
  });
  const init = await client.connect(transport);
  return { client, init };
}
async function call(client, tool, args) {
  const result = await client.callTool(tool, args ?? {});
  const text = (result.content ?? [])
    .filter((c) => c.type === "text").map((c) => c.text).join("\n");
  let parsed = null;
  try { parsed = JSON.parse(text); } catch { /* non-JSON text output */ }
  // Success-path diagnostics: an isError result (or a top-level error code)
  // must surface its code instead of masquerading as empty rows.
  const errCode = (result.isError === true && parsed && typeof parsed.code === "string")
    ? parsed.code : null;
  const errMessage = errCode && parsed && typeof parsed.message === "string"
    ? String(parsed.message).slice(0, 160) : "";
  return { result, text, parsed, bytes: Buffer.byteLength(text, "utf8"),
           errCode, errMessage };
}
function rejectEvidence(callOut) {
  // server._tool_error_result: text JSON with TOP-LEVEL {code, message, ...}
  // (nested error.{code} kept as a defensive fallback only).
  const parsed = callOut?.parsed ?? {};
  const code = typeof parsed.code === "string" ? parsed.code
    : (typeof parsed.error?.code === "string" ? parsed.error.code : null);
  return {
    isError: callOut?.result?.isError === true,
    code,
    status: typeof parsed.status === "number" ? parsed.status : null,
  };
}
function dig(obj, keys) {
  for (const key of keys) {
    const at = Array.isArray(obj) ? null : obj?.[key];
    if (at !== undefined && at !== null) return at;
    const nested = obj?.data?.[key];
    if (nested !== undefined && nested !== null) return nested;
  }
  return undefined;
}
async function expectReject(client, tool, args, caseName, codeKey) {
  // A definite rejection is any of: JSON-RPC error (thrown McpError with a
  // code), HTTP 4xx (thrown McpHttpError with status), or a resolved
  // CallToolResult with isError and a structured error code.  All three carry
  // a machine-readable code that is recorded for the Python-side enum pin.
  let callOut = null;
  try {
    callOut = await call(client, tool, args);
  } catch (e) {
    if (e instanceof McpError && typeof e.code === "number") {
      out.codes[codeKey] = `jsonrpc:${e.code}`;
      check(caseName, true, `jsonrpc error code=${e.code}`);
      return { ok: true };
    }
    if (e instanceof McpHttpError && e.status >= 400 && e.status < 500) {
      out.codes[codeKey] = `http:${e.status}`;
      check(caseName, true, `http ${e.status}`);
      return { ok: true };
    }
    const info = classify(e);
    out.codes[codeKey] = info.name;
    check(caseName, false, `transport-level error: ${info.name} ${info.message}`);
    return { ok: false };
  }
  const ev = rejectEvidence(callOut);
  out.codes[codeKey] = ev.code ?? (ev.status === null ? null : `status:${ev.status}`);
  check(caseName, ev.isError && (ev.code !== null || ev.status !== null),
        `isError=${ev.isError} code=${ev.code} status=${ev.status}`);
  return { ok: ev.isError, ev };
}

async function main() {
  if (input.phase === "1") {
    let A = null;
    try {
      A = await connect(input.urlA, input.user, input.pass);
      check("sdk_initialize", A.client.connectionState === "connected",
            `state=${A.client.connectionState} proto=${A.client.protocolVersion}`);
      out.evidence.protocolVersion = A.client.protocolVersion ?? null;
      out.evidence.serverName = A.client.serverInfo?.name ?? null;

      const tools = await A.client.listTools();
      const names = tools.map((t) => t.name).sort();
      check("sdk_tool_discovery",
            names.length === input.expectedTools.length &&
            names.every((n, i) => n === input.expectedTools[i]),
            `tools=${names.join(",")}`);
      const query = tools.find((t) => t.name === "ontology_query");
      const required = query?.inputSchema?.required ?? [];
      check("sdk_query_release_required", required.includes("releaseId"),
            `required=${JSON.stringify(required)}`);

      // schema bootstrap + pagination (cursor field is `cursor`, null on end)
      const boot = await call(A.client, "ontology_schema", {});
      const releaseId = dig(boot.parsed, ["releaseId", "release_id"]);
      check("schema_bootstrap_v1", releaseId === input.v1ReleaseId,
            `releaseId=${releaseId}`);
      let pages = 1;
      let maxBytes = boot.bytes;
      let allText = boot.text;
      let cursor = dig(boot.parsed, ["cursor", "nextCursor", "next_cursor"]);
      while (cursor && pages < 50) {
        const page = await call(A.client, "ontology_schema", { cursor });
        pages += 1;
        maxBytes = Math.max(maxBytes, page.bytes);
        allText += "\n" + page.text;
        cursor = dig(page.parsed, ["cursor", "nextCursor", "next_cursor"]);
      }
      check("schema_pagination_terminated", cursor === undefined || cursor === null,
            `pages=${pages} stopped=${cursor === undefined || cursor === null}`);
      check("schema_text_under_limit", maxBytes < TEXT_LIMIT,
            `maxPageBytes=${maxBytes}`);
      check("hidden_absent_from_outputs",
            !allText.includes(input.hiddenProp) &&
            !allText.includes(input.hiddenValuePrefix),
            `hiddenPropInSchema=${allText.includes(input.hiddenProp)}`);
      const filtered = await call(A.client, "ontology_schema",
                                  { objectTypeId: "ot-order" });
      const visible = ["order_no", "status", "amount"];
      const filteredVisible = visible.every(
        (p) => filtered.text.includes(p));
      check("schema_filtered_by_type",
            filteredVisible && !filtered.text.includes(input.hiddenProp),
            `visibleProps=${filteredVisible}`);

      // query: rows correctness — contract: {releaseId, query: {sources,...}}
      const rowsQuery = await call(A.client, "ontology_query", {
        releaseId,
        query: {
          sources: [{ alias: "o", objectTypeId: "ot-order" }],
          select: [
            { source: "o", property: "order_no", as: "order_no" },
            { source: "o", property: "amount", as: "amount" },
          ],
          where: [{ source: "o", property: "status", op: "eq", value: "paid" }],
          orderBy: [{ output: "order_no", direction: "asc" }],
        },
      });
      const rows = dig(rowsQuery.parsed, ["rows"]) ?? [];
      const orderNos = rows.map((r) =>
        r.order_no ?? r.orderNo ?? dig(r, ["order_no"])).filter(Boolean);
      const wanted = input.expectedPaidOrderNos;
      check("query_rows_correct",
            rowsQuery.errCode === null &&
            orderNos.length === wanted.length &&
            orderNos.every((n, i) => n === wanted[i]),
            rowsQuery.errCode
              ? `error code=${rowsQuery.errCode} ${rowsQuery.errMessage}`
              : `rows=${orderNos.length}/${wanted.length} sample=${orderNos.slice(0, 3).join(",")}`);

      // aggregation — group key rides select (proven unified-query shape)
      const aggQuery = await call(A.client, "ontology_query", {
        releaseId,
        query: {
          sources: [{ alias: "o", objectTypeId: "ot-order" }],
          select: [{ source: "o", property: "status", as: "status" }],
          groupBy: [{ source: "o", property: "status" }],
          aggregates: [
            { fn: "count", as: "cnt" },
            { fn: "sum", source: "o", property: "amount", as: "total" },
          ],
          orderBy: [{ output: "total", direction: "desc" }],
        },
      });
      const aggRows = dig(aggQuery.parsed, ["rows"]) ?? [];
      const aggOk = input.expectedAggregates.every((exp) => {
        const hit = aggRows.find((r) => JSON.stringify(r).includes(exp.status));
        if (!hit) return false;
        const cnt = Number(hit.cnt ?? dig(hit, ["cnt"]));
        const total = Number(hit.total ?? dig(hit, ["total"]));
        return cnt === exp.cnt && Math.abs(total - exp.total) < 1e-6;
      }) && aggRows.length === input.expectedAggregates.length;
      check("query_aggregates_correct",
            aggQuery.errCode === null && aggOk,
            aggQuery.errCode
              ? `error code=${aggQuery.errCode} ${aggQuery.errMessage}`
              : `aggRows=${JSON.stringify(aggRows).slice(0, 160)}`);

      // multipage: full keyset walk to the terminal page (snapshot released)
      let page = await call(A.client, "ontology_query", {
        releaseId,
        query: {
          sources: [{ alias: "o", objectTypeId: "ot-order" }],
          select: [
            { source: "o", property: "order_no", as: "order_no" },
            { source: "o", property: "status", as: "status" },
          ],
          orderBy: [{ output: "order_no", direction: "asc" }],
          pageSize: 3,
        },
      });
      let queryPages = 1;
      let maxQueryBytes = page.bytes;
      let collected = dig(page.parsed, ["rows"]) ?? [];
      let queryCursor = dig(page.parsed, ["cursor"]);
      const textAll = [page.text];
      if (page.errCode) {
        check("query_multipage_complete", false,
              `first page error code=${page.errCode} ${page.errMessage}`);
      }
      while (queryCursor && queryPages < 20) {
        const next = await call(A.client, "ontology_query_page",
                                { cursor: queryCursor });
        queryPages += 1;
        maxQueryBytes = Math.max(maxQueryBytes, next.bytes);
        collected = collected.concat(dig(next.parsed, ["rows"]) ?? []);
        textAll.push(next.text);
        queryCursor = dig(next.parsed, ["cursor"]);
      }
      const allOrderNos = collected.map((r) => r.order_no).filter(Boolean);
      const uniq = new Set(allOrderNos);
      const expectedAll = input.allOrderNos;
      check("query_multipage_complete",
            uniq.size === allOrderNos.length &&
            allOrderNos.length === expectedAll.length &&
            expectedAll.every((n) => uniq.has(n)) &&
            queryCursor == null,
            `pages=${queryPages} rows=${allOrderNos.length}/${expectedAll.length}`);
      // keep a LIVE mid-walk cursor (walk stopped before terminal page) for
      // the post-v2 stale-cursor phase
      let kept = await call(A.client, "ontology_query", {
        releaseId,
        query: {
          sources: [{ alias: "o", objectTypeId: "ot-order" }],
          select: [{ source: "o", property: "order_no", as: "order_no" }],
          orderBy: [{ output: "order_no", direction: "asc" }],
          pageSize: 5,
        },
      });
      let keptCursor = dig(kept.parsed, ["cursor"]);
      if (keptCursor) {
        const kept2 = await call(A.client, "ontology_query_page",
                                 { cursor: keptCursor });
        keptCursor = dig(kept2.parsed, ["cursor"]);
      }
      out.evidence.midCursor = keptCursor ?? null;
      const joined = textAll.join("\n") + rowsQuery.text + aggQuery.text
        + filtered.text + kept.text;
      check("hidden_absent_from_outputs",
            !joined.includes(input.hiddenProp) &&
            !joined.includes(input.hiddenValuePrefix), "");

      // hidden property explicitly selected -> rejected
      await expectReject(A.client, "ontology_query", {
        releaseId,
        query: {
          sources: [{ alias: "o", objectTypeId: "ot-order" }],
          select: [
            { source: "o", property: "order_no", as: "order_no" },
            { source: "o", property: input.hiddenProp, as: "leak" },
          ],
        },
      }, "hidden_select_rejected", "hidden_property_select");

      // cross-ontology: foreign object type id on this endpoint
      await expectReject(A.client, "ontology_query", {
        releaseId,
        query: {
          sources: [{ alias: "p", objectTypeId: input.crossObjectType }],
          select: [{ source: "p", property: "party_key", as: "party_key" }],
        },
      }, "cross_ontology_objecttype_rejected", "cross_ontology_object_type");

      // cross-ontology: injected ontologyId on a source leaf (extra forbid)
      await expectReject(A.client, "ontology_query", {
        releaseId,
        query: {
          sources: [{ alias: "o", objectTypeId: "ot-order",
                      ontologyId: input.otherOntologyId }],
          select: [{ source: "o", property: "order_no", as: "order_no" }],
        },
      }, "source_field_injection_rejected", "source_field_injection");

      // cross-endpoint cursor: A cursor against B endpoint
      const midCursor = out.evidence.midCursor;
      if (midCursor) {
        let B = null;
        try {
          B = await connect(input.urlB, input.user, input.pass);
          await expectReject(B.client, "ontology_query_page",
                             { cursor: midCursor }, "cross_endpoint_cursor_rejected", "cross_endpoint_cursor");
        } finally {
          if (B) await B.client.close().catch(() => {});
        }
      } else {
        check("cross_endpoint_cursor", false, "no cursor from first page");
      }

      // wrong password -> 401 at initialize
      let wrongInfo = null;
      try {
        await connect(input.urlA, input.user, input.wrongPass);
      } catch (e) {
        wrongInfo = classify(e);
      }
      check("wrong_password_http_401",
            wrongInfo !== null && wrongInfo.status === 401,
            `name=${wrongInfo?.name} status=${wrongInfo?.status}`);

      out.evidence.schemaPages = pages;
      out.evidence.queryPages = queryPages;
      out.evidence.schemaMaxBytes = maxBytes;
      out.evidence.queryMaxBytes = maxQueryBytes;
      out.evidence.rowsFetched = allOrderNos.length;
      await A.client.close().catch(() => {});
    } catch (e) {
      const info = classify(e);
      check("sdk_phase1_fatal", false, `${info.name}: ${info.message}`);
      if (A) await A.client.close().catch(() => {});
    }
  }

  if (input.phase === "2") {
    let oldInfo = null;
    try {
      await connect(input.urlA, input.user, input.oldPass);
    } catch (e) {
      oldInfo = classify(e);
    }
    check("old_password_invalid_after_change",
          oldInfo !== null && oldInfo.status === 401,
          `name=${oldInfo?.name} status=${oldInfo?.status}`);
    let A = null;
    try {
      A = await connect(input.urlA, input.user, input.newPass);
      const tools = await A.client.listTools();
      check("new_password_accepted",
            A.client.connectionState === "connected" && tools.length > 0,
            `tools=${tools.length}`);
      await A.client.close().catch(() => {});
    } catch (e) {
      const info = classify(e);
      check("new_password_accepted", false, `${info.name}: ${info.message}`);
      if (A) await A.client.close().catch(() => {});
    }
  }

  if (input.phase === "3") {
    let A = null;
    try {
      A = await connect(input.urlA, input.user, input.pass);
      // stale cursor from v1 after v2 promote
      if (input.midCursor) {
        await expectReject(A.client, "ontology_query_page",
                           { cursor: input.midCursor }, "stale_cursor_rejected", "stale_cursor_after_promote");
      } else {
        check("stale_cursor_after_promote", false, "no stored cursor");
      }
      // explicit old release query -> rejected
      await expectReject(A.client, "ontology_query", {
        releaseId: input.v1ReleaseId,
        query: {
          sources: [{ alias: "o", objectTypeId: "ot-order" }],
          select: [{ source: "o", property: "order_no", as: "order_no" }],
        },
      }, "old_release_query_rejected", "old_release_query");
      // bootstrap now pins v2; the type page exposes the new property while
      // the hidden property stays hidden (overview pages carry no properties)
      const boot = await call(A.client, "ontology_schema", {});
      const releaseId = dig(boot.parsed, ["releaseId", "release_id"]);
      const typePage = await call(A.client, "ontology_schema",
                                  { releaseId, objectTypeId: "ot-order" });
      check("schema_bootstrap_v2",
            releaseId === input.v2ReleaseId &&
            typePage.text.includes(input.newProp) &&
            !typePage.text.includes(input.hiddenProp),
            `releaseId=${releaseId} newProp=${typePage.text.includes(input.newProp)}`);
      const q = await call(A.client, "ontology_query", {
        releaseId,
        query: {
          sources: [{ alias: "o", objectTypeId: "ot-order" }],
          select: [
            { source: "o", property: "order_no", as: "order_no" },
            { source: "o", property: input.newProp, as: "region" },
          ],
          orderBy: [{ output: "order_no", direction: "asc" }],
          pageSize: 200,
        },
      });
      const rows = dig(q.parsed, ["rows"]) ?? [];
      const regions = new Set(rows.map((r) => r.region).filter(Boolean));
      check("query_v2_recovered",
            q.result.isError !== true &&
            rows.length === input.expectedRows &&
            regions.size === 2,
            `rows=${rows.length}/${input.expectedRows} regions=${[...regions].join(",")}`);
      await A.client.close().catch(() => {});
    } catch (e) {
      const info = classify(e);
      check("sdk_phase3_fatal", false, `${info.name}: ${info.message}`);
      if (A) await A.client.close().catch(() => {});
    }
  }

  writeFileSync(outputPath, JSON.stringify(out), "utf8");
}

main().catch((e) => {
  out.checks.push({ name: "node_runner_fatal", pass: false,
                    detail: String(e?.message ?? e).slice(0, 240) });
  writeFileSync(outputPath, JSON.stringify(out), "utf8");
});
"""


def run_node_phase(run: Run, pi: dict[str, Any], work_dir: Path,
                   phase_input: dict[str, Any]) -> dict[str, Any]:
    runner_path = work_dir / "runner.mjs"
    if not runner_path.exists():
        runner_path.write_text(NODE_RUNNER, encoding="utf-8")
    input_path = work_dir / f"phase{phase_input['phase']}-in.json"
    output_path = work_dir / f"phase{phase_input['phase']}-out.json"
    payload = json.dumps(phase_input)
    for key in ("pass", "oldPass", "newPass", "wrongPass"):
        if key in phase_input:
            run.register_secret(phase_input[key])
    input_path.write_text(payload, encoding="utf-8")

    env = dict(os.environ)
    env["OO_PIMCP_ENTRY"] = pi["pi_mcp_entry"]
    proc = subprocess.Popen(
        [pi["node_bin"], str(runner_path), str(input_path), str(output_path)],
        cwd=str(work_dir), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    run.record_pid(proc.pid, f"node phase {phase_input['phase']}")
    try:
        _, err = proc.communicate(timeout=SUBPROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
        run.log(f"node phase {phase_input['phase']} timed out")
        return {"checks": [], "codes": {}, "evidence": {}}
    if err and err.strip():
        run.log(f"node stderr (tail): {err.strip()[-400:]}")
    if proc.returncode != 0 or not output_path.exists():
        run.log(f"node phase {phase_input['phase']} rc={proc.returncode}")
        return {"checks": [], "codes": {}, "evidence": {}}
    return json.loads(output_path.read_text(encoding="utf-8"))


def apply_node_results(run: Run, node_out: dict[str, Any]) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for check in node_out.get("checks", []):
        name = check["name"]
        if name in REPORT_CASES:
            results[name] = run.check(name, check["pass"], check.get("detail", ""))
        elif not check["pass"]:
            run.log(f"NODE-FAIL {name}: {check.get('detail', '')}")
    codes = node_out.get("codes", {})
    for key, code in codes.items():
        if key in run.report["observed_error_codes"]:
            run.report["observed_error_codes"][key] = code
    evidence = node_out.get("evidence", {})
    if "schemaPages" in evidence:
        run.report["counts"]["schema_pages"] = int(evidence.get("schemaPages") or 0)
        run.report["counts"]["query_pages"] = int(evidence.get("queryPages") or 0)
        run.report["counts"]["rows_fetched"] = int(evidence.get("rowsFetched") or 0)
        run.report["text_bytes"]["schema_max_page"] = int(
            evidence.get("schemaMaxBytes") or 0)
        run.report["text_bytes"]["query_max_page"] = int(
            evidence.get("queryMaxBytes") or 0)
    return results


def verify_error_codes(run: Run) -> None:
    """Pinned codes (verified against server/service source) must match
    exactly; unpinned rejections were already asserted for the rejection
    itself plus a non-empty recorded code."""
    pinned = {key: expected for key, expected in EXPECTED_ERROR_CODES.items()
              if expected is not None}
    mismatches: list[str] = []
    for key, expected in pinned.items():
        observed = run.report["observed_error_codes"].get(key)
        if observed != expected:
            mismatches.append(f"{key}: expected={expected} observed={observed}")
    run.check("error_code_enum_match", not mismatches,
              "; ".join(mismatches)[:220])


# ------------------------------------------------------------------- main ----

def reap_processes(run: Run) -> None:
    ok = True
    for pid, desc in run.spawned:
        try:
            os.kill(pid, 0)
            # Still alive: short-lived helpers must not linger; terminate.
            try:
                os.kill(pid, 15)
                time.sleep(0.2)
                os.kill(pid, 0)
                os.kill(pid, 9)
            except OSError:
                pass
            run.log(f"terminated leftover pid {pid} ({desc})")
        except OSError:
            pass  # already exited — expected for run-to-completion children
        except PermissionError:
            ok = False
    run.check("cleanup_processes_reaped", ok)
    run.report["cleanup"]["processes_reaped"] = ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--pg-dsn", default=DEFAULT_PG_DSN)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the temp pi-config/work dir for debugging")
    args = parser.parse_args()

    run = Run(args.report_dir)
    _assert_report_shape(run.report)
    work_dir: Path | None = None
    api: Api | None = None
    users: dict[str, Any] | None = None
    ontology_ids: list[str] = []
    try:
        if not check_guards(run, args.api_base, args.pg_dsn):
            run.fail("guards failed")
            return 1

        if not probe_api(run, args.api_base):
            run.fail("API not reachable (stack up? health/live or openapi)")
            return 1

        pi = discover_pi(run)
        if pi is None:
            run.fail("pi discovery failed")
            return 1

        users = create_users(run, args.pg_dsn)
        if users is None:
            run.fail("db fixture failed")
            return 1

        api = Api(run, args.api_base)
        try:
            admin_token = api.login(users["admin"]["username"],
                                    users["admin"]["password"])
            run.check("http_admin_login", True)
        except (httpx.HTTPError, KeyError) as exc:
            run.check("http_admin_login", False, str(exc)[:200])
            run.fail("admin login failed")
            return 1

        run.stage("http_fixtures")
        suffix = run.run_id
        oid_a = api.create_ontology(admin_token, f"OO-MCP-E2E-A-{suffix}")
        oid_b = api.create_ontology(admin_token, f"OO-MCP-E2E-B-{suffix}")
        run.report["counts"]["ontologies_created"] = 2
        run.check("ontology_a_created", bool(oid_a))
        run.check("ontology_b_created", bool(oid_b))
        ontology_ids = [oid_a, oid_b]

        try:
            api.put_full(admin_token, oid_a, structure_a(v2=False))
            run.check("structure_a_saved", True)
            api.put_full(admin_token, oid_b, structure_b())
            run.check("structure_b_saved", True)
        except httpx.HTTPError as exc:
            detail = getattr(exc, "response", None)
            body = detail.text[:200] if detail is not None else str(exc)[:200]
            run.check("structure_a_saved", False, body)
            run.check("structure_b_saved", False, body)
            run.fail("structure save failed")
            return 1

        run.stage("publish_v1")
        try:
            release_a1 = api.publish(admin_token, oid_a,
                                     f"oomcp-e2e v1 {suffix}",
                                     structure=structure_a(v2=False))
            run.check("publish_v1_a", release_a1.get("id") is not None)
            release_b1 = api.publish(admin_token, oid_b,
                                     f"oomcp-e2e-b v1 {suffix}",
                                     structure=structure_b())
            run.check("publish_v1_b", release_b1.get("id") is not None)
        except (httpx.HTTPError, RuntimeError, KeyError) as exc:
            run.check("publish_v1_a", False, str(exc)[:200])
            run.check("publish_v1_b", False, str(exc)[:200])
            run.fail("publish v1 failed")
            return 1
        v1_release_id = release_a1["id"]
        # release data (synthetic, isolated direct-DB fixture pinned to v1)
        if not insert_release_instances(run, args.pg_dsn, oid_a, v1_release_id,
                                        _orders(v2=False)):
            run.fail("v1 data fixture failed")
            return 1

        run.stage("boundary_fixture")
        apply_boundary(run, args.pg_dsn, oid_a, BOUNDARY_V1, "boundary_applied_v1")

        work_dir = Path(tempfile.mkdtemp(prefix="oomcp-e2e-"))
        pi_dir = work_dir / "pi-agent"
        pi_dir.mkdir()

        ok_cli = run_pi_cli_list(run, pi, api, oid_a, users["viewer"], pi_dir)

        run.stage("sdk_phase1")
        phase1 = {
            "phase": "1",
            "urlA": api.base + MCP_PATH_TEMPLATE.format(oid=oid_a),
            "urlB": api.base + MCP_PATH_TEMPLATE.format(oid=oid_b),
            "user": users["viewer"]["username"],
            "pass": users["viewer"]["password"],
            "wrongPass": users["viewer"]["password"] + "x",
            "headerUser": HEADER_USER,
            "headerPass": HEADER_PASS,
            "v1ReleaseId": v1_release_id,
            "expectedTools": sorted(EXPECTED_TOOLS),
            "expectedPaidOrderNos": _expected_paid_order_nos(v2=False),
            "expectedAggregates": _expected_aggregates(v2=False),
            "allOrderNos": [o["order_no"] for o in _orders(v2=False)],
            "hiddenProp": HIDDEN_PROP,
            "hiddenValuePrefix": HIDDEN_VALUE_PREFIX,
            "crossObjectType": CROSS_OBJECT_TYPE,
            "otherOntologyId": oid_b,
            "textLimitBytes": TEXT_LIMIT_BYTES,
        }
        node_out = run_node_phase(run, pi, work_dir, phase1)
        results = apply_node_results(run, node_out)
        mid_cursor = (node_out.get("evidence", {}) or {}).get("midCursor")

        run.stage("password_rotation")
        try:
            viewer_token = api.login(users["viewer"]["username"],
                                     users["viewer"]["password"])
            run.check("http_viewer_login", True)
        except (httpx.HTTPError, KeyError) as exc:
            run.check("http_viewer_login", False, str(exc)[:200])
            viewer_token = None
        if viewer_token:
            changed = api.change_password(
                viewer_token, users["viewer"]["password"],
                users["viewer"]["password_new"])
            run.check("password_changed_http", changed)
        if ok_cli and not results.get("sdk_initialize"):
            run.fail("SDK phase1 could not connect although CLI did")

        run.stage("password_rotation")
        phase2 = {
            "phase": "2",
            "urlA": phase1["urlA"],
            "user": users["viewer"]["username"],
            "oldPass": users["viewer"]["password"],
            "newPass": users["viewer"]["password_new"],
            "headerUser": HEADER_USER,
            "headerPass": HEADER_PASS,
        }
        node_out2 = run_node_phase(run, pi, work_dir, phase2)
        apply_node_results(run, node_out2)

        run.stage("publish_v2")
        # Remove this run's synthetic v1 rows first: promote's runtime-state
        # gate fail-closes when a release would erase unattributed runtime
        # data, and an unmapped ontology's trial legitimately carries none.
        if not insert_release_instances(run, args.pg_dsn, oid_a,
                                        v1_release_id, orders=None):
            run.fail("v1 data teardown failed")
            return 1
        try:
            release_a2 = api.publish(admin_token, oid_a,
                                     f"oomcp-e2e v2 {suffix}",
                                     structure=structure_a(v2=True))
            run.check("publish_v2", release_a2.get("id") is not None
                      and release_a2["id"] != v1_release_id)
        except (httpx.HTTPError, RuntimeError, KeyError) as exc:
            run.check("publish_v2", False, str(exc)[:200])
            run.fail("publish v2 failed")
            return 1
        if not insert_release_instances(run, args.pg_dsn, oid_a,
                                        release_a2["id"], _orders(v2=True)):
            run.fail("v2 data fixture failed")
            return 1
        apply_boundary(run, args.pg_dsn, oid_a, BOUNDARY_V2, "boundary_applied_v2")

        run.stage("sdk_phase3")
        phase3 = {
            "phase": "3",
            "urlA": phase1["urlA"],
            "user": users["viewer"]["username"],
            "pass": users["viewer"]["password_new"],
            "headerUser": HEADER_USER,
            "headerPass": HEADER_PASS,
            "v1ReleaseId": v1_release_id,
            "v2ReleaseId": release_a2["id"],
            "midCursor": mid_cursor,
            "newProp": NEW_PROP_V2,
            "expectedRows": len(ORDER_STATUSES) + len(EXTRA_V2_STATUSES),
        }
        node_out3 = run_node_phase(run, pi, work_dir, phase3)
        apply_node_results(run, node_out3)
        verify_error_codes(run)

    except Exception as exc:  # noqa: BLE001 — report any failure honestly
        run.fail(f"unexpected: {type(exc).__name__}: {exc}"[:480])
    finally:
        run.stage("cleanup")
        if api is not None and users is not None:
            try:
                admin_token = api.login(users["admin"]["username"],
                                        users["admin"]["password"])
                deleted = bool(ontology_ids) and all(
                    api.delete_ontology(admin_token, oid)
                    for oid in ontology_ids)
                run.check("cleanup_ontologies_deleted", deleted)
                run.report["cleanup"]["ontologies_deleted"] = deleted
            except (httpx.HTTPError, KeyError):
                run.check("cleanup_ontologies_deleted", False)
                run.report["cleanup"]["ontologies_deleted"] = False
        if users is not None:
            try:
                cleanup_db(run, args.pg_dsn, users, ontology_ids)
            except psycopg2.Error:
                run.check("cleanup_users_deleted", False, "psycopg2 error")
        if work_dir is not None:
            if args.keep_temp:
                run.log(f"keeping temp dir {work_dir} (--keep-temp)")
                run.check("cleanup_temp_dir_removed", True,
                          "kept intentionally via --keep-temp")
            else:
                shutil.rmtree(work_dir, ignore_errors=True)
                removed = not work_dir.exists()
                run.check("cleanup_temp_dir_removed", removed)
                run.report["cleanup"]["temp_dir_removed"] = removed
        reap_processes(run)
        if api is not None:
            api.close()

    _assert_report_shape(run.report)
    failed = [name for name, ok in run.report["cases"].items() if not ok]
    status = "pass" if not failed else "fail"
    if failed:
        run.report["failure_detail"] = (
            run.report["failure_detail"]
            or f"failed cases: {', '.join(sorted(failed))}")[:500]
    report = run.finish(status)
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
