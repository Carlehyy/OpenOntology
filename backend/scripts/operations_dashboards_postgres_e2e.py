#!/usr/bin/env python3
"""Operations-dashboards concurrency acceptance against real PostgreSQL.

Exercises the dashboard write path (create/patch/reorder/delete) on a real
PostgreSQL with the real domain lock (``pg_advisory_xact_lock(20303, 147)``),
bounded ``lock_timeout`` and revision/conflict semantics (ACCEPTANCE P01-P07):

- P01 single alembic head + fresh upgrade + exact schema shape;
- P02 upgrade over synthetic existing 0149 data (reports rows preserved),
  model-created pre-existing table accepted idempotently, drifted table
  rejected (not silently stamped over);
- P03 downgrade 0150 -> 0149 drops the dashboard table; upgrade restores;
- P04 concurrent empty-table creates (barrier-synchronized threads), then
  appending after an existing sort_order=0;
- P05 revision conflicts: expected_revision CAS (one winner, one 409 with the
  current revision) and no-expected_revision ordered updates under row lock;
- P06 full reorder racing create/delete; every outcome linearizable under the
  domain lock (exact set wins, stale set gets 409), final state == SQL truth;
- P07 a third transaction holding the domain lock >5s: create/reorder/delete
  fail fast with 409 dashboard_write_busy (the create compensates the remote
  dashboard it already created); after release, retries succeed and no
  advisory lock leaks.

The Grafana HTTP boundary is served by a local loopback stub implementing the
pinned 13.3.1 surface the client actually uses (PoC-measured endpoints);
PostgreSQL, advisory locks, transactions and the service code paths are all
real. The 501-dashboard bulk case against real pinned Grafana belongs to t6
(P08).

Safety: requires ``OPENONTOLOGY_OPERATIONS_DASHBOARDS_E2E=1`` and
``ENVIRONMENT=test``. Creates one throwaway UUID-named database inside the
explicitly provided test PostgreSQL and always drops it (WITH FORCE) in
``finally``; the given database itself is never modified beyond CREATE/DROP.
The JSON report is metrics-only; no connection strings, credentials, database
names or UIDs reach it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import stat
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

SENTINEL = "OPENONTOLOGY_OPERATIONS_DASHBOARDS_E2E"
DEFAULT_REPORT = (
    BACKEND_DIR.parent / ".artifacts" / "operations-dashboards" / "dashboards-postgres.json"
)

REPORT_STAGES = {
    "starting",
    "database_guard",
    "database_create",
    "stub_grafana",
    "alembic_heads",
    "schema_setup",
    "migrations",
    "concurrency_p04",
    "concurrency_p05",
    "concurrency_p06",
    "concurrency_p07",
    # 矩阵全绿但一次性库删除失败：如实失败，不得发布误导的 pass 结果。
    "cleanup",
    "complete",
}

REPORT_FIELDS = {
    "schema_version",
    "result",
    "stage",
    "server_version_major",
    "role_superuser",
    "database_created",
    "database_dropped",
    "stub_authorized",
    "stub_dashboards_created",
    "stub_dashboards_deleted",
    "cases",
    "durations_ms",
    "counts",
    "lock_wait_ms",
}

REPORT_CASES = {
    "single_head",
    "fresh_upgrade_shape",
    "existing_data_preserved",
    "model_created_table_idempotent",
    "drifted_table_rejected",
    "downgrade_upgrade_cycle",
    "concurrent_creates",
    "append_after_zero",
    "revision_cas_conflict",
    "revision_retry_after_refresh",
    "row_lock_ordered_updates",
    "reorder_vs_create_delete",
    "reorder_linearizable_outcomes",
    "domain_lock_busy",
    "busy_create_compensates",
    "retry_after_release",
    "no_lock_leak",
}

STUB_USER = "admin"
STUB_PASSWORD = secrets.token_urlsafe(18)
STUB_BASIC = "Basic " + base64.b64encode(
    f"{STUB_USER}:{STUB_PASSWORD}".encode()
).decode()

ADVISORY_LOCK_KEYS = (20303, 147)


# ---------------------------------------------------------------------------
# Loopback Grafana stub: the pinned 13.3.1 surface the client actually uses
# ---------------------------------------------------------------------------


class StubGrafanaState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.uids: set[str] = set()
        self.authorized = 0
        self.created = 0
        self.deleted = 0
        self.folder_created = 0


class _StubHandler(BaseHTTPRequestHandler):
    state: StubGrafanaState
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # silence the default request logging
        return

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") != STUB_BASIC:
            self._json(401, {"message": "unauthorized"})
            return False
        with self.state.lock:
            self.state.authorized += 1
        return True

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/grafana/api/health":
            self._json(200, {"database": "ok", "version": "13.3.1"})
            return
        if not self._authorized():
            return
        if path == "/grafana/api/user":
            self._json(200, {"login": STUB_USER, "isGrafanaAdmin": True})
            return
        if path == "/grafana/api/folders/openontology":
            with self.state.lock:
                exists = self.state.folder_created > 0
            self._json(200, {"uid": "openontology"}) if exists else self._json(
                404, {"message": "not found"}
            )
            return
        if path == "/grafana/api/search":
            query = parse_qs(parsed.query)
            uids = query.get("dashboardUIDs", [])
            limit = int(query.get("limit", ["100"])[0])
            with self.state.lock:
                found = [
                    {"uid": uid, "title": uid, "url": f"/grafana/d/{uid}"}
                    for uid in uids
                    if uid in self.state.uids
                ]
            self._json(200, found[:limit])
            return
        self._json(404, {"message": "not found"})

    def do_POST(self):  # noqa: N802
        parsed = urlsplit(self.path)
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if parsed.path == "/grafana/api/folders":
            with self.state.lock:
                self.state.folder_created += 1
            self._json(200, {"uid": body.get("uid")})
            return
        if parsed.path == "/grafana/api/dashboards/db":
            uid = body["dashboard"]["uid"]
            with self.state.lock:
                self.state.uids.add(uid)
                self.state.created += 1
            self._json(200, {"id": 1, "uid": uid})
            return
        self._json(404, {"message": "not found"})

    def do_DELETE(self):  # noqa: N802
        parsed = urlsplit(self.path)
        if not self._authorized():
            return
        if parsed.path.startswith("/grafana/api/dashboards/uid/"):
            uid = parsed.path.rsplit("/", 1)[1]
            with self.state.lock:
                if uid in self.state.uids:
                    self.state.uids.discard(uid)
                    self.state.deleted += 1
                    self._json(200, {"message": "deleted"})
                    return
            self._json(404, {"message": "not found"})
            return
        self._json(404, {"message": "not found"})


class StubGrafanaServer:
    def __init__(self) -> None:
        self.state = StubGrafanaState()

    def __enter__(self) -> "StubGrafanaServer":
        handler = type("BoundStubHandler", (_StubHandler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Report helpers (metrics only; sensitive values are asserted absent)
# ---------------------------------------------------------------------------


def _report_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "fail",
        "stage": "starting",
        "server_version_major": 0,
        "role_superuser": None,
        "database_created": False,
        "database_dropped": None,
        "stub_authorized": 0,
        "stub_dashboards_created": 0,
        "stub_dashboards_deleted": 0,
        "cases": {name: False for name in sorted(REPORT_CASES)},
        "durations_ms": {},
        "counts": {},
        "lock_wait_ms": None,
    }


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, (bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    raise ValueError(f"unsupported report value type: {type(value)!r}")


def _assert_sanitized_report(report: dict[str, Any], sensitive: set[str]) -> bytes:
    if set(report) != REPORT_FIELDS:
        raise ValueError("report schema mismatch")
    if report["result"] not in {"pass", "fail"}:
        raise ValueError("invalid report result")
    if report["stage"] not in REPORT_STAGES:
        raise ValueError("invalid report stage")
    if set(report["cases"]) != REPORT_CASES:
        raise ValueError("report case set mismatch")
    payload = json.dumps(_sanitize_value(report), sort_keys=True, indent=2) + "\n"
    for value in sensitive:
        if value and value in payload:
            raise ValueError("sensitive value reached report payload")
    return payload.encode("utf-8")


def _write_report(path: Path, report: dict[str, Any], sensitive: set[str]) -> None:
    """Write the sanitized report atomically with platform-correct permissions.

    POSIX: the temp file (and the replaced report) is forced to 0600 and the
    final mode is asserted; real chmod/IO errors propagate untouched.
    Windows: ``os.fchmod``/POSIX mode bits do not exist, so the permission
    steps and the 0600 assertion are skipped (mkstemp's restrictive creation
    stands) while sanitization and the atomic replace are unchanged.

    Every failure path releases the created file descriptor *before*
    removing the temp file: the raw ``mkstemp`` fd is owned by this function
    until ``os.fdopen`` takes it over, so a failure before takeover must
    ``os.close(fd)`` (otherwise the open handle blocks unlink with
    WinError 32), and a failure after takeover closes the file object. The
    original exception is re-raised — cleanup never swallows the real
    error, and a failed write never leaves a report file behind.
    """
    payload = _assert_sanitized_report(report, sensitive)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    handle = None
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "wb")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp, path)
    except BaseException:
        # 先释放句柄再清理临时文件：fdopen 已接管则关文件对象（重复 close
        # 为幂等），未接管则本函数负责关闭 mkstemp 的裸 fd。清理自身的
        # OSError 不掩盖原异常；失败不产出误导的报告文件。
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        else:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if hasattr(os, "fchmod") and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("report mode must be 0600")


def _guard_database_url() -> str:
    """Only an explicit, credential-bearing PostgreSQL DSN is accepted."""
    if os.environ.get(SENTINEL) != "1":
        raise ValueError(f"{SENTINEL} must be 1")
    if os.environ.get("ENVIRONMENT") != "test":
        raise ValueError("ENVIRONMENT must be test")
    database_url = os.environ.get("DATABASE_URL", "")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if parsed.query or parsed.fragment:
        raise ValueError("DATABASE_URL must not contain query or fragment overrides")
    if not parsed.username or not parsed.password:
        raise ValueError("DATABASE_URL must contain credentials")
    return database_url


def _http_outcome(exc: Exception):
    """(status, code, detail) of a service-raised HTTPException."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return getattr(exc, "status_code", None), detail.get("code"), detail
    return getattr(exc, "status_code", None), None, detail


def _drop_throwaway_database(admin_engine, run_database: str) -> None:
    """Drop the throwaway database this run created, with legal PG syntax.

    Only the ``run_database`` name generated by this run
    (``oo_dashboards_e2e_<random>``) is ever dropped — never a wider target.
    ``WITH (FORCE)`` is the legal PostgreSQL 13+ spelling (``WITH FORCE``
    is a syntax error and previously left throwaway databases behind).
    Failures raise the original exception; the caller records only a fixed,
    non-sensitive identifier (never SQL/DSN/database name/credentials).
    """
    with admin_engine.connect() as conn:
        conn.exec_driver_sql(f'DROP DATABASE "{run_database}" WITH (FORCE)')


def _finalize_report(
    report: dict[str, Any], *, matrix_succeeded: bool, database_dropped: bool
) -> None:
    """Form the final result only after the cleanup outcome is known.

    Matrix green + database dropped -> pass/complete. Matrix green but
    cleanup failed -> fail/cleanup (no misleading pass artifact). Matrix
    not green -> keep the existing failure result and failure-point stage.
    """
    if not matrix_succeeded:
        return
    if database_dropped:
        report["result"] = "pass"
        report["stage"] = "complete"
    else:
        report["result"] = "fail"
        report["stage"] = "cleanup"


# ---------------------------------------------------------------------------
# Main acceptance
# ---------------------------------------------------------------------------


def _run(report_path: Path) -> dict[str, Any]:
    report = _report_template()
    sensitive: set[str] = set()
    engine = None
    admin_engine = None
    holder_engine = None
    database_dropped = False
    matrix_succeeded = False
    run_database: str | None = None
    stub: StubGrafanaServer | None = None
    outcome_lock = threading.Lock()

    try:
        report["stage"] = "database_guard"
        base_url = _guard_database_url()
        sensitive.add(base_url)

        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.engine import make_url

        base = make_url(base_url)
        run_database = f"oo_dashboards_e2e_{uuid.uuid4().hex[:12]}"
        run_url = base.set(database=run_database).render_as_string(hide_password=False)
        sensitive.add(run_url)
        sensitive.add(run_database)
        sensitive.add(STUB_PASSWORD)

        report["stage"] = "database_create"
        admin_engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{run_database}"')
        report["database_created"] = True

        report["stage"] = "stub_grafana"
        stub = StubGrafanaServer()
        stub.__enter__()
        # Must be set before the first app import: settings reads them once.
        os.environ["DATABASE_URL"] = run_url
        os.environ["GRAFANA_BASE_URL"] = stub.base_url
        os.environ["GRAFANA_ADMIN_USER"] = STUB_USER
        os.environ["GRAFANA_ADMIN_PASSWORD"] = STUB_PASSWORD

        report["stage"] = "alembic_heads"
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        alembic_cfg.set_main_option(
            "sqlalchemy.url", run_url.replace("%", "%%")
        )
        heads = ScriptDirectory(str(BACKEND_DIR / "alembic")).get_heads()
        assert heads == ["0150_operations_dashboards"], heads
        report["cases"]["single_head"] = True

        report["stage"] = "schema_setup"
        engine = create_engine(run_url, pool_pre_ping=True)
        command.upgrade(alembic_cfg, "head")
        with engine.connect() as conn:
            report["server_version_major"] = int(
                conn.execute(
                    text("SELECT current_setting('server_version_num')")
                ).scalar()
            ) // 10000
            report["role_superuser"] = (
                str(
                    conn.execute(text("SELECT current_setting('is_superuser')")).scalar()
                ).strip().lower()
                == "on"
            )

        def _assert_shape() -> None:
            inspector = inspect(engine)
            columns = {
                column["name"]: column
                for column in inspector.get_columns("operations_dashboards")
            }
            assert set(columns) == {
                "id", "name", "description", "icon", "grafana_uid",
                "sort_order", "revision", "created_at", "updated_at",
            }
            assert columns["name"]["nullable"] is False
            assert columns["grafana_uid"]["nullable"] is False
            assert inspector.get_pk_constraint("operations_dashboards")[
                "constrained_columns"
            ] == ["id"]
            uniques = {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints("operations_dashboards")
            }
            assert ("grafana_uid",) in uniques
            indexes = {
                item["name"]: item["column_names"]
                for item in inspector.get_indexes("operations_dashboards")
            }
            assert indexes["ix_operations_dashboards_sort_order"] == ["sort_order"]
            assert indexes["ix_operations_dashboards_updated_at"] == ["updated_at"]

        _assert_shape()
        report["cases"]["fresh_upgrade_shape"] = True

        # Seed the editor user used by every service-level write below.
        from sqlalchemy.orm import sessionmaker

        sessions = sessionmaker(bind=engine)

        from app.auth.models import User
        from app.services.auth_service import hash_password

        setup = sessions()
        try:
            editor = User(
                id=f"dash-e2e-{uuid.uuid4().hex[:10]}",
                username=f"dash_editor_{uuid.uuid4().hex[:8]}",
                email=f"dash-{uuid.uuid4().hex[:6]}@example.invalid",
                password_hash=hash_password("dashboards-e2e-only"),
                role="editor",
            )
            setup.add(editor)
            setup.commit()
            editor_id = editor.id
        finally:
            setup.close()

        class _Editor:
            def __init__(self, user_id: str):
                self.id = user_id
                self.role = "editor"

        def _user():
            return _Editor(editor_id)

        from fastapi import HTTPException

        from app.operations.dashboards import schemas as dashboards_schemas
        from app.operations.dashboards import service as dashboards_service

        def _call(method, *args, **kwargs):
            """Run a service function in its own session; return its outcome.

            ("ok", result) or ("http", (status, code, detail)).
            """
            session = sessions()
            try:
                result = getattr(dashboards_service, method)(session, *args, **kwargs)
                return ("ok", result)
            except HTTPException as exc:
                return ("http", _http_outcome(exc))
            finally:
                session.close()

        def _create(name: str, icon: str = "activity"):
            outcome = _call(
                "create_dashboard",
                dashboards_schemas.DashboardCreate(
                    name=name, description="", icon=icon
                ),
                _user(),
            )
            assert outcome[0] == "ok", outcome
            row = outcome[1]
            return row.id, row.grafana_uid

        # ------------------------------------------------------------ P02/P03
        report["stage"] = "migrations"
        # 合成既有 0149 数据（运营报告行）：downgrade 后再升级 0150 必须保留。
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO operations_reports (id, name, description, sort_order,"
                " revision, created_at, updated_at) VALUES"
                " ('dash-e2e-report-1', '既有报告', '', 0, 1, now(), now())"
            ))
        command.downgrade(alembic_cfg, "0149_integrated_replica_snapshot")
        assert "operations_dashboards" not in inspect(engine).get_table_names()
        command.upgrade(alembic_cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM operations_reports")
            ).scalar() == 1
        _assert_shape()
        report["cases"]["existing_data_preserved"] = True
        report["cases"]["downgrade_upgrade_cycle"] = True

        # 模型 create_all 先存在的表（运行时建表）→ 升级幂等接受，无重复索引。
        command.downgrade(alembic_cfg, "0149_integrated_replica_snapshot")
        from app.database import Base
        from app.model_registry import import_all_models

        import_all_models()
        Base.metadata.create_all(bind=engine)
        command.upgrade(alembic_cfg, "head")
        _assert_shape()
        report["cases"]["model_created_table_idempotent"] = True

        # 漂移表（name 宽度不同）必须被拒绝，不能靠 stamp 跳过。
        command.downgrade(alembic_cfg, "0149_integrated_replica_snapshot")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE operations_dashboards ("
                " id varchar NOT NULL, name varchar(10) NOT NULL,"
                " description text NOT NULL, icon varchar(64) NOT NULL,"
                " grafana_uid varchar(40) NOT NULL, sort_order integer NOT NULL,"
                " revision integer NOT NULL, created_at timestamptz NOT NULL,"
                " updated_at timestamptz NOT NULL, PRIMARY KEY (id),"
                " CONSTRAINT uq_operations_dashboards_grafana_uid"
                " UNIQUE (grafana_uid))"
            ))
        try:
            command.upgrade(alembic_cfg, "head")
            raise AssertionError("drifted table must be rejected")
        except Exception as exc:  # noqa: BLE001 - alembic wraps migration errors
            assert "incompatible" in str(exc), str(exc)[:200]
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE operations_dashboards"))
        command.upgrade(alembic_cfg, "head")
        report["cases"]["drifted_table_rejected"] = True

        # ------------------------------------------------------ P04 creates
        report["stage"] = "concurrency_p04"
        started = time.perf_counter()
        barrier = threading.Barrier(8)
        create_outcomes: list[Any] = []

        def _concurrent_create(index: int) -> None:
            barrier.wait()
            outcome = _create(f"并发看板 {index}")
            with outcome_lock:
                create_outcomes.append(outcome)

        threads = [
            threading.Thread(target=_concurrent_create, args=(index,))
            for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert len(create_outcomes) == 8
        assert len({row_id for row_id, _uid in create_outcomes}) == 8
        assert len({uid for _row_id, uid in create_outcomes}) == 8
        with engine.connect() as conn:
            sort_orders = conn.execute(text(
                "SELECT sort_order FROM operations_dashboards ORDER BY sort_order"
            )).scalars().all()
        assert sort_orders == list(range(8)), sort_orders
        # 已有 sort_order=0 后追加：不得错误重用 0。
        append_id, _append_uid = _create("追加看板")
        with engine.connect() as conn:
            appended = conn.execute(text(
                "SELECT sort_order FROM operations_dashboards WHERE id = :id"
            ), {"id": append_id}).scalar_one()
        assert appended == 8, appended
        report["cases"]["concurrent_creates"] = True
        report["cases"]["append_after_zero"] = True
        report["durations_ms"]["concurrent_creates"] = round(
            (time.perf_counter() - started) * 1000, 1
        )

        # ---------------------------------------------------- P05 revisions
        report["stage"] = "concurrency_p05"
        target_id, _target_uid = create_outcomes[0]
        patch_barrier = threading.Barrier(2)
        patch_outcomes: list[Any] = []

        def _patch_cas(name: str) -> None:
            patch_barrier.wait()
            outcome = _call(
                "update_dashboard",
                target_id,
                dashboards_schemas.DashboardUpdate(
                    name=name, expected_revision=1
                ),
                _user(),
            )
            with outcome_lock:
                patch_outcomes.append(outcome)

        threads = [
            threading.Thread(target=_patch_cas, args=("并发改名A",)),
            threading.Thread(target=_patch_cas, args=("并发改名B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        statuses = sorted(outcome[0] for outcome in patch_outcomes)
        assert statuses == ["http", "ok"], patch_outcomes
        status, code, detail = next(
            outcome for outcome in patch_outcomes if outcome[0] == "http"
        )[1]
        assert (status, code) == (409, "dashboard_revision_conflict"), detail
        assert detail["revision"] == 2, detail
        report["cases"]["revision_cas_conflict"] = True

        retry = _call(
            "update_dashboard",
            target_id,
            dashboards_schemas.DashboardUpdate(name="刷新后改名", expected_revision=2),
            _user(),
        )
        assert retry[0] == "ok", retry
        report["cases"]["revision_retry_after_refresh"] = True

        no_rev_barrier = threading.Barrier(2)
        no_rev_outcomes: list[Any] = []

        def _patch_ordered(name: str) -> None:
            no_rev_barrier.wait()
            outcome = _call(
                "update_dashboard",
                target_id,
                dashboards_schemas.DashboardUpdate(name=name),
                _user(),
            )
            with outcome_lock:
                no_rev_outcomes.append(outcome)

        threads = [
            threading.Thread(target=_patch_ordered, args=("顺序改名A",)),
            threading.Thread(target=_patch_ordered, args=("顺序改名B",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert all(outcome[0] == "ok" for outcome in no_rev_outcomes), no_rev_outcomes
        with engine.connect() as conn:
            revision = conn.execute(text(
                "SELECT revision FROM operations_dashboards WHERE id = :id"
            ), {"id": target_id}).scalar_one()
        assert revision == 5, revision  # 1 + CAS 胜者 + 刷新重试 + 2 次顺序更新
        report["cases"]["row_lock_ordered_updates"] = True

        # ----------------------------------------------- P06 reorder races
        report["stage"] = "concurrency_p06"
        started = time.perf_counter()
        with engine.connect() as conn:
            current_ids = [
                row[0]
                for row in conn.execute(text(
                    "SELECT id FROM operations_dashboards"
                    " ORDER BY sort_order, updated_at DESC, id"
                ))
            ]
        assert len(current_ids) == 9, current_ids
        victim_id = current_ids[0]
        race_barrier = threading.Barrier(3)
        race_outcomes: dict[str, Any] = {}

        def _race_reorder() -> None:
            race_barrier.wait()
            outcome = _call(
                "reorder_dashboards",
                dashboards_schemas.DashboardReorder(
                    dashboard_ids=current_ids[::-1]
                ),
                _user(),
            )
            with outcome_lock:
                race_outcomes["reorder"] = outcome

        def _race_create() -> None:
            race_barrier.wait()
            outcome = _call(
                "create_dashboard",
                dashboards_schemas.DashboardCreate(
                    name="竞态新建看板", description="", icon="gauge"
                ),
                _user(),
            )
            with outcome_lock:
                # 会话已在 _call 关闭；立即取值避免 detached 属性访问。
                race_outcomes["create"] = (
                    outcome[0], (outcome[1].id, outcome[1].grafana_uid)
                    if outcome[0] == "ok" else outcome[1]
                )

        def _race_delete() -> None:
            race_barrier.wait()
            outcome = _call("delete_dashboard", victim_id, _user())
            with outcome_lock:
                race_outcomes["delete"] = outcome

        threads = [
            threading.Thread(target=_race_reorder),
            threading.Thread(target=_race_create),
            threading.Thread(target=_race_delete),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        reorder_outcome = race_outcomes["reorder"]
        create_outcome = race_outcomes["create"]
        delete_outcome = race_outcomes["delete"]
        # 每个结果都按域锁线性化：reorder 要么按其精确集合成功，要么旧集合
        # 409 mismatch；create/delete 在锁序内成功（远端桩健康）。
        assert reorder_outcome[0] in {"ok", "http"}, reorder_outcome
        if reorder_outcome[0] == "http":
            status, code, _detail = reorder_outcome[1]
            assert (status, code) == (409, "dashboard_reorder_mismatch"), reorder_outcome
        assert create_outcome[0] == "ok", create_outcome
        assert delete_outcome[0] == "ok", delete_outcome
        report["cases"]["reorder_linearizable_outcomes"] = True

        new_id, _new_uid = create_outcome[1]
        expected_final = set(current_ids)
        expected_final.discard(victim_id)
        expected_final.add(new_id)
        with engine.connect() as conn:
            final_rows = conn.execute(text(
                "SELECT id, sort_order, revision FROM operations_dashboards"
                " ORDER BY sort_order"
            )).fetchall()
        assert {row[0] for row in final_rows} == expected_final
        assert [row[1] for row in final_rows] == sorted(
            row[1] for row in final_rows
        )
        report["cases"]["reorder_vs_create_delete"] = True
        report["durations_ms"]["reorder_vs_writes"] = round(
            (time.perf_counter() - started) * 1000, 1
        )

        # ------------------------------------------------- P07 domain lock
        report["stage"] = "concurrency_p07"
        started = time.perf_counter()
        holder_engine = create_engine(run_url, pool_size=1)
        holder = holder_engine.connect()
        holder_transaction = holder.begin()
        holder.execute(text(
            f"SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEYS[0]}, {ADVISORY_LOCK_KEYS[1]})"
        ))
        # 持锁时间 > 5s lock_timeout（第三方事务在域锁内休眠）。
        holder.execute(text("SELECT pg_sleep(7)"))

        with stub.state.lock:
            created_before = stub.state.created
            deleted_before = stub.state.deleted

        busy_started = time.perf_counter()
        busy_outcome = _call(
            "create_dashboard",
            dashboards_schemas.DashboardCreate(
                name="锁等待看板", description="", icon="server"
            ),
            _user(),
        )
        lock_wait_ms = (time.perf_counter() - busy_started) * 1000
        report["lock_wait_ms"] = round(lock_wait_ms, 1)
        assert busy_outcome[0] == "http", busy_outcome
        status, code, _detail = busy_outcome[1]
        assert (status, code) == (409, "dashboard_write_busy"), busy_outcome
        assert 4_000 < lock_wait_ms < 60_000, lock_wait_ms
        report["cases"]["domain_lock_busy"] = True

        # create 远端已建：本地失败后必须补偿删除该 UID（404 幂等成功）。
        with stub.state.lock:
            assert stub.state.created == created_before + 1, stub.state.created
            assert stub.state.deleted == deleted_before + 1, stub.state.deleted
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT count(*) FROM operations_dashboards"
                " WHERE name = '锁等待看板'"
            )).scalar() == 0
        report["cases"]["busy_create_compensates"] = True

        holder_transaction.rollback()
        holder.close()
        holder_engine.dispose()
        holder_engine = None

        retry_outcome = _call(
            "create_dashboard",
            dashboards_schemas.DashboardCreate(
                name="释放后重试", description="", icon="activity"
            ),
            _user(),
        )
        assert retry_outcome[0] == "ok", retry_outcome
        report["cases"]["retry_after_release"] = True

        with engine.connect() as conn:
            leaked = conn.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                " AND (classid, objid) = (20303, 147)"
            )).scalar_one()
        assert leaked == 0, leaked
        report["cases"]["no_lock_leak"] = True
        report["durations_ms"]["domain_lock"] = round(
            (time.perf_counter() - started) * 1000, 1
        )

        with stub.state.lock:
            report["stub_authorized"] = stub.state.authorized
            report["stub_dashboards_created"] = stub.state.created
            report["stub_dashboards_deleted"] = stub.state.deleted
            remote_uid_count = len(stub.state.uids)
        with engine.connect() as conn:
            local_count = conn.execute(
                text("SELECT count(*) FROM operations_dashboards")
            ).scalar_one()
        # 远端桩与本地行数一致：每次成功 create 建远端、成功 delete 删远端、
        # 失败 create 的补偿配平。
        assert remote_uid_count == local_count, (remote_uid_count, local_count)
        report["counts"] = {"final_rows": local_count, "remote_uids": remote_uid_count}

        # 矩阵全绿：pass 工件只在 finally 中一次性库删除确认后形成
        # （_finalize_report）；清理失败时最终报告如实 fail，绝不提前
        # 发布误导的 pass 结果。
        matrix_succeeded = True
    finally:
        if stub is not None:
            try:
                stub.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        for disposable in (engine, holder_engine):
            if disposable is not None:
                try:
                    disposable.dispose()
                except Exception:  # noqa: BLE001
                    pass
        if admin_engine is not None and run_database is not None:
            try:
                _drop_throwaway_database(admin_engine, run_database)
                database_dropped = True
            except Exception as exc:  # noqa: BLE001
                # 固定非敏感失败标识：不回显异常串/SQL/DSN/库名/口令。
                database_dropped = False
                print(
                    f"database cleanup failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
            finally:
                try:
                    admin_engine.dispose()
                except Exception:  # noqa: BLE001
                    pass
        if report.get("database_dropped") is None:
            # 只有确有创建过一次性库时才记录清理结果。
            report["database_dropped"] = (
                database_dropped if report.get("database_created") else None
            )
        # 最终结果在清理确认后形成；最终报告写盘失败从此处向上传播到
        # main（非零退出），不吞异常后留下误导的旧 pass 工件。
        _finalize_report(
            report,
            matrix_succeeded=matrix_succeeded,
            database_dropped=database_dropped,
        )
        _write_report(report_path, report, sensitive)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="监控看板真实 PostgreSQL 并发验收（P01-P07）"
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help="脱敏 JSON 报告输出路径（默认 .artifacts/operations-dashboards/）",
    )
    args = parser.parse_args(argv)
    try:
        # _run 返回本轮已写盘的最终报告；最终写盘失败在此传播为非零退出，
        # 绝不回读磁盘上可能残留的旧 pass 工件。
        report = _run(Path(args.report))
    except Exception as exc:  # noqa: BLE001
        # 固定非敏感失败标识：不回显异常串/SQL/DSN/库名/口令。
        print(f"acceptance run failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    failed = [name for name, passed in report["cases"].items() if not passed]
    # 一次性库创建过就必须删除成功：矩阵全绿但清理未确认（false/null）
    # 一律非零退出。
    cleanup_ok = not report["database_created"] or report["database_dropped"] is True
    print(f"result={report['result']} stage={report['stage']}")
    print(
        f"cases={len(REPORT_CASES) - len(failed)}/{len(REPORT_CASES)}"
        + (f" failed={','.join(failed)}" if failed else "")
    )
    return 0 if report["result"] == "pass" and not failed and cleanup_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
