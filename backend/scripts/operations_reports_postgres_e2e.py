#!/usr/bin/env python3
"""Operations-report controlled SQL acceptance against real PostgreSQL.

Exercises ``app.data_channel.query.report_sql`` end to end on a real
PostgreSQL, plus the operations report/article HTTP boundary through a real
PG-backed session (FastAPI TestClient with the production lifespan suppressed):

- manual structured blob, curated approved lake table (direct + approved
  row-edit overlay) and integrated begin/append/seal frozen relations, with
  aggregate values checked against independently computed expectations;
- latest-version switch, pending/rejected approval refusals, tampered sealed
  manifest refusal;
- AST guard against physical-table / schema-qualified / dataset-shadowing CTE
  escalation;
- literal ``'%(foo)s :bar'`` survives verbatim, injection-shaped parameter
  strings stay bound values (never SQL);
- CASE / date_trunc execution, empty versions, invalid typed values,
  duplicate output columns;
- explicit staging/result row & byte budgets and statement timeout errors;
- one batch pinned to one snapshot while another connection moves the dataset
  pointer between its queries (SQLAlchemy event hook, no execute mocking);
- pool_size=2 real connection rotation with zero ``rep_ds_*`` temp-table
  residue;
- report/article CRUD, reorder and revision-409 through the HTTP router.

Safety: requires ``OPENONTOLOGY_OPERATIONS_REPORTS_E2E=1`` and
``ENVIRONMENT=test``.  The script creates one throwaway UUID-named database
inside the explicitly provided test PostgreSQL and always drops it (WITH
FORCE) in ``finally``; the given database itself is never modified except for
that CREATE/DROP.  ``UNIFIED_QUERY_*`` env overrides shrink production-scale
budgets so over-budget scenarios are reachable with small fixtures; execution
itself is never mocked.  The JSON report is metrics-only (fixed fields,
counts, booleans, durations); no connection strings, credentials, database
names, dataset ids or SQL text reach it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

SENTINEL = "OPENONTOLOGY_OPERATIONS_REPORTS_E2E"
DEFAULT_REPORT = BACKEND_DIR.parent / ".artifacts" / "operations-reports" / \
    "reports-postgres.json"

REPORT_STAGES = {
    "starting",
    "database_guard",
    "database_create",
    "schema_setup",
    "fixtures",
    "sql_core",
    "integrated",
    "curated",
    "guards",
    "budgets",
    "timeout",
    "snapshot",
    "pool",
    "api",
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
    "budget_overrides",
    "cases",
    "durations_ms",
    "counts",
    "pool_distinct_pids",
    "temp_residue_count",
    "timeout_elapsed_ms",
}
REPORT_CASES = {
    "manual_structured_blob",
    "typed_case_date_trunc",
    "literal_placeholders",
    "mod_and_percent_literals",
    "e_string_literal_executes",
    "param_injection_inert",
    "single_query_wrapper",
    "integrated_sealed_relation",
    "integrated_broken_sealed_rejected",
    "integrated_latest_version_switch",
    "curated_lake_approved",
    "curated_edit_overlay",
    "curated_pending_rejected_then_approved",
    "empty_version",
    "invalid_typed_value",
    "duplicate_output_columns",
    "malicious_physical_table_rejected",
    "malicious_qualified_name_rejected",
    "malicious_cte_shadow_rejected",
    "staging_rows_budget",
    "staging_bytes_budget",
    "result_rows_budget",
    "result_bytes_budget",
    "statement_timeout",
    "same_snapshot_mid_batch",
    "pool_rotation_no_residue",
    "api_crud_reorder_revision",
    "api_execute_queries",
}

# Shrunk for this acceptance run only (set before any app import so pydantic
# settings pick them up): they keep every execution real while making the
# over-budget scenarios reachable with tiny fixtures.
BUDGET_OVERRIDES = {
    "UNIFIED_QUERY_STATEMENT_TIMEOUT_MS": "2000",
    "UNIFIED_QUERY_FEDERATED_SIDE_ROWS_LIMIT": "50",
    "UNIFIED_QUERY_FEDERATED_SIDE_BYTES_LIMIT": "4000",
    "UNIFIED_QUERY_MEMORY_RESULT_BYTES_LIMIT": "4000",
}
# Statement budget used only by the timeout scenario.
TIMEOUT_CASE_MS = 250


def _report_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "fail",
        "stage": "starting",
        "server_version_major": 0,
        "role_superuser": None,
        "database_created": False,
        "database_dropped": None,
        "budget_overrides": {name: int(value) for name, value
                             in BUDGET_OVERRIDES.items()},
        "cases": {name: False for name in sorted(REPORT_CASES)},
        "durations_ms": {},
        "counts": {},
        "pool_distinct_pids": 0,
        "temp_residue_count": None,
        "timeout_elapsed_ms": None,
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


def _assert_sanitized_report(report: dict[str, Any],
                             sensitive_values: set[str]) -> bytes:
    if set(report) != REPORT_FIELDS:
        raise ValueError("report schema mismatch")
    if report["schema_version"] != 1:
        raise ValueError("report schema version mismatch")
    if report["result"] not in {"pass", "fail"}:
        raise ValueError("invalid report result")
    if report["stage"] not in REPORT_STAGES:
        raise ValueError("invalid report stage")
    if set(report["cases"]) != REPORT_CASES:
        raise ValueError("report case set mismatch")
    payload = json.dumps(_sanitize_value(report), sort_keys=True,
                         indent=2) + "\n"
    for value in sensitive_values:
        if value and value in payload:
            raise ValueError("sensitive value reached report payload")
    return payload.encode("utf-8")


def _write_report(path: Path, report: dict[str, Any],
                  sensitive_values: set[str]) -> None:
    payload = _assert_sanitized_report(report, sensitive_values)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if tmp.exists():
            tmp.unlink()
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("report mode must be 0600")


def _guard_database_url() -> str:
    if os.environ.get(SENTINEL) != "1":
        raise ValueError(f"{SENTINEL} must be 1")
    if os.environ.get("ENVIRONMENT") != "test":
        raise ValueError("ENVIRONMENT must be test")
    database_url = os.environ.get("DATABASE_URL", "")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "DATABASE_URL must not contain query or fragment overrides")
    if not parsed.username or not parsed.password:
        raise ValueError("DATABASE_URL must contain credentials")
    return database_url


def _expect_query_error(callable_, *, status: int, code: str) -> None:
    from app.data_channel.query.contracts import QueryError

    try:
        callable_()
    except QueryError as exc:
        assert exc.status == status, (exc.status, exc.code, status, code)
        assert exc.code == code, (exc.status, exc.code, status, code)
        return
    raise AssertionError(f"expected QueryError {status}/{code}, call succeeded")


def _typed(name: str, kind: str, source_type: str, *, nullable: bool = True,
           **metadata) -> dict:
    return {"name": name, "type": kind, "source_type": source_type,
            "nullable": nullable, "precision": None, "scale": None,
            "timezone": None, "collation": None, "length": None,
            "unsigned": False, **metadata}


def _seed_user(session, *, username: str, role: str):
    from app.auth.models import User
    from app.services.auth_service import hash_password

    user = User(
        id=f"or-e2e-{role}-{uuid.uuid4().hex[:10]}",
        username=f"{username}_{uuid.uuid4().hex[:8]}",
        email=f"{username}-{uuid.uuid4().hex[:6]}@example.invalid",
        password_hash=hash_password("report-e2e-only"),
        role=role,
    )
    session.add(user)
    session.commit()
    return user


def _seed_structured(session, *, name: str, columns: list[str],
                     columns_typed: list[dict], rows: list[dict]):
    from app.data_channel.datasets.models import Dataset, DatasetVersion

    blob = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    dataset_id = f"or-e2e-ds-{uuid.uuid4().hex[:12]}"
    version = DatasetVersion(
        id=f"or-e2e-ver-{uuid.uuid4().hex[:12]}",
        dataset_id=dataset_id, version_no=1, rowcount=len(rows),
        data_blob=blob, data_size=len(blob),
        checksum=hashlib.sha256(blob).hexdigest())
    dataset = Dataset(
        id=dataset_id, name=name, kind="structured",
        schema_json={"columns": list(columns),
                     "columns_typed": list(columns_typed)})
    session.add(dataset)
    session.flush()
    version.dataset_id = dataset_id
    session.add(version)
    session.flush()
    dataset.latest_version_id = version.id
    session.commit()
    return dataset


def _seed_integrated(session, user, *, name: str, schema: list[dict],
                     pk: list[str], batches: list[list[dict]]):
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets import integrated_store as store
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.integrated.contracts import digest
    from app.data_channel.integrated.models import IntegratedAsset

    fingerprint = digest({"columns": schema, "primary_key": pk})
    connection = Connection(id=f"or-e2e-conn-{uuid.uuid4().hex[:10]}",
                            name=f"conn-{uuid.uuid4().hex[:6]}",
                            kind="postgres", config={})
    session.add(connection)
    session.flush()
    dataset = Dataset(
        id=f"or-e2e-ds-{uuid.uuid4().hex[:12]}", name=name,
        kind="integrated", source_connection_id=connection.id,
        source_resource="public.fixture")
    session.add(dataset)
    session.flush()
    asset = IntegratedAsset(
        id=f"or-e2e-asset-{uuid.uuid4().hex[:10]}", dataset_id=dataset.id,
        connection_id=connection.id, name="fixture", schema_name="public",
        table_name="fixture",
        schema_json={"columns": schema, "primary_key": pk},
        schema_fingerprint=fingerprint, writable=False, created_by=user.id)
    session.add(asset)
    session.commit()
    descriptor = {
        "schema": schema, "primary_key": pk, "schema_fingerprint": fingerprint,
        "asset_id": asset.id, "dataset_id": dataset.id,
        "connection_id": connection.id, "resource": "public.fixture",
        "expected_rows": sum(len(batch) for batch in batches),
        "snapshot": {"id": str(uuid.uuid4()), "consistent": True,
                     "kind": "repeatable_read",
                     "started_at": "2026-11-10T08:00:00+00:00"},
    }
    run, gate = store.begin_snapshot(session, descriptor)
    hasher = store.snapshot_hasher(run)
    for batch in batches:
        store.write_snapshot_batch(session, run, gate, batch, hasher)
    completion = {
        "complete": True, "row_count": run.expected_rows,
        "schema_fingerprint": run.schema_json["schema_fingerprint"],
        "snapshot_id": run.snapshot_json["id"],
    }
    version = store.seal_snapshot(session, run, completion,
                                  hasher.hexdigest())
    session.expire_all()
    return dataset, run, version


def _seed_curated(session, *, name: str, rows: list[dict],
                  columns_typed: list[dict]):
    from app.data_channel.datasets import lake_store
    from app.data_channel.datasets.models import Dataset

    dataset = Dataset(
        id=f"or-e2e-ds-{uuid.uuid4().hex[:12]}", name=name, kind="curated",
        schema_json={
            "primary_key": "id",
            "columns": [item["name"] for item in columns_typed],
            "columns_typed": list(columns_typed)})
    session.add(dataset)
    session.commit()
    version, _changeset = lake_store.upsert_run(
        session, dataset, rows, "overwrite", ["id"])
    session.expire_all()
    return dataset, version


def _add_review(session, dataset, version, *, status: str):
    from datetime import datetime, timezone

    from app.data_channel.curated.models import CuratedReview

    review = CuratedReview(
        id=f"or-e2e-rv-{uuid.uuid4().hex[:12]}",
        curated_dataset_id=dataset.id, dataset_version_id=version.id,
        status=status,
        decided_at=(datetime.now(timezone.utc)
                    if status in {"approved", "rejected"} else None))
    session.add(review)
    session.commit()
    return review


def _set_review_status(session, review, status: str):
    from datetime import datetime, timezone

    review.status = status
    review.decided_at = (datetime.now(timezone.utc)
                         if status in {"approved", "rejected"} else None)
    session.commit()


def _add_row_edit(session, review, row: dict, pk_col: str, field: str,
                  new_value: str):
    from app.data_channel.curated.models import CuratedRowEdit
    from app.data_channel.curated.review_row_identity import encode_row_pk

    session.add(CuratedRowEdit(
        id=f"or-e2e-ed-{uuid.uuid4().hex[:12]}", review_id=review.id,
        row_pk=encode_row_pk(row, [pk_col]), field_name=field,
        old_value=str(row.get(field)), new_value=new_value))
    session.commit()


def _temp_table_count(engine) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text(
            "SELECT count(*) FROM pg_class WHERE relname LIKE 'rep_ds_%'"
        )).scalar_one())


class _IdRef:
    """Detached-safe row handle for seeding helpers (id only)."""

    def __init__(self, row_id: str):
        self.id = row_id


class _MidBatchPointerFlip:
    """Move the dataset pointer from another connection between two queries.

    Uses a SQLAlchemy ``before_cursor_execute`` hook on the report engine; the
    update itself runs on a dedicated second engine.  Nothing about statement
    execution is mocked.
    """

    def __init__(self, report_engine, engine_url: str, dataset_id: str,
                 new_version_id: str, marker: str):
        from sqlalchemy import create_engine, event

        self._engine_url = engine_url
        self._dataset_id = dataset_id
        self._new_version_id = new_version_id
        self._marker = marker
        self.fired = False
        self.update_backend_pid = None
        self._report_engine = report_engine
        self._event = event
        self._create_engine = create_engine

    def _flip(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import sessionmaker

        flip_engine = self._create_engine(self._engine_url, pool_size=1)
        try:
            session = sessionmaker(bind=flip_engine)()
            try:
                raw = session.connection().connection.driver_connection
                cursor = raw.cursor()
                cursor.execute("SELECT pg_backend_pid()")
                self.update_backend_pid = int(cursor.fetchone()[0])
                cursor.close()
                session.execute(text(
                    "UPDATE v2_datasets SET latest_version_id = :v "
                    "WHERE id = :d"),
                    {"v": self._new_version_id, "d": self._dataset_id})
                session.commit()
            finally:
                session.close()
        finally:
            flip_engine.dispose()

    def _before_cursor_execute(self, conn, cursor, statement, parameters,
                               context, executemany):
        if self.fired or self._marker not in statement:
            return
        self.fired = True
        self._flip()

    def __enter__(self):
        self._event.listen(self._report_engine, "before_cursor_execute",
                           self._before_cursor_execute)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event

        event.remove(self._report_engine, "before_cursor_execute",
                     self._before_cursor_execute)
        return False


def _run(report_path: Path) -> None:
    report = _report_template()
    sensitive_values: set[str] = set()
    stage = "starting"
    engine = None
    monitor_engine = None
    admin_engine = None
    database_dropped = False
    run_database: str | None = None
    try:
        _write_report(report_path, report, sensitive_values)
        stage = "database_guard"
        report["stage"] = stage
        base_url = _guard_database_url()
        sensitive_values.add(base_url)

        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        base = make_url(base_url)
        run_database = f"oo_reports_e2e_{uuid.uuid4().hex[:12]}"
        run_url = base.set(database=run_database).render_as_string(
            hide_password=False)
        sensitive_values.add(run_url)
        sensitive_values.add(run_database)

        stage = "database_create"
        report["stage"] = stage
        admin_engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{run_database}"')
        report["database_created"] = True

        # Must be set before the first app import: settings reads them once.
        os.environ["DATABASE_URL"] = run_url
        for name, value in BUDGET_OVERRIDES.items():
            os.environ[name] = value

        stage = "schema_setup"
        report["stage"] = stage
        engine = create_engine(run_url, pool_size=2, max_overflow=0)
        monitor_engine = create_engine(run_url, pool_size=1)
        from app.database import Base
        from app.model_registry import import_all_models

        import_all_models()  # same registration surface as alembic env
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            report["server_version_major"] = int(conn.execute(text(
                "SELECT current_setting('server_version_num')")).scalar()) \
                // 10000
            report["role_superuser"] = str(conn.execute(text(
                "SELECT current_setting('is_superuser')")).scalar()
            ).strip().lower() == "on"

        from sqlalchemy import update as sa_update
        from sqlalchemy.orm import sessionmaker

        from app.data_channel.datasets.models import IntegratedSyncRun

        sessions = sessionmaker(bind=engine)

        from app.data_channel.query.contracts import QueryError  # noqa: F401
        from app.data_channel.query.report_sql import (
            check_report_sql, execute_report_queries, execute_report_query,
        )

        stage = "fixtures"
        report["stage"] = stage
        setup = sessions()
        try:
            editor = _seed_user(setup, username="or_editor", role="editor")

            main_rows = [
                {"region": "north", "amount": 120,
                 "ts": "2026-11-01T10:30:00+00:00", "note": "alpha"},
                {"region": "north", "amount": 80,
                 "ts": "2026-11-01T15:45:00+00:00", "note": "beta"},
                {"region": "south", "amount": 200,
                 "ts": "2026-11-02T08:00:00+00:00", "note": "gamma"},
                {"region": "south", "amount": 60,
                 "ts": "2026-11-02T19:20:00+00:00", "note": "delta"},
                {"region": "east", "amount": 40,
                 "ts": "2026-11-03T07:10:00+00:00", "note": "epsilon"},
            ]
            main_ds = _seed_structured(
                setup, name=f"主数据-{uuid.uuid4().hex[:6]}",
                columns=["region", "amount", "ts", "note"],
                columns_typed=[
                    {"name": "region", "type": "string"},
                    {"name": "amount", "type": "number"},
                    {"name": "ts", "type": "timestamp"},
                    {"name": "note", "type": "string"}],
                rows=main_rows)

            empty_ds = _seed_structured(
                setup, name=f"空版本-{uuid.uuid4().hex[:6]}",
                columns=["region", "amount"],
                columns_typed=[{"name": "region", "type": "string"},
                               {"name": "amount", "type": "number"}],
                rows=[])

            invalid_ds = _seed_structured(
                setup, name=f"坏类型-{uuid.uuid4().hex[:6]}",
                columns=["sku", "qty"],
                columns_typed=[{"name": "sku", "type": "string"},
                               {"name": "qty", "type": "integer"}],
                rows=[{"sku": "a", "qty": "12"},
                      {"sku": "b", "qty": "not-a-number"}])

            wide_note = "w" * 900
            wide_rows = [{"seq": str(i), "note": wide_note}
                         for i in range(1, 7)]
            wide_ds = _seed_structured(
                setup, name=f"宽行-{uuid.uuid4().hex[:6]}",
                columns=["seq", "note"],
                columns_typed=[{"name": "seq", "type": "string"},
                               {"name": "note", "type": "string"}],
                rows=wide_rows)

            # Result-bytes fixture: staging stays inside the shrunk staging
            # byte budget, but the JSON-encoded result (escaped quotes)
            # exceeds the shrunk result byte budget.
            pct_rows = [{"seq": str(i), "note": '"' * 80}
                        for i in range(1, 41)]
            pct_ds = _seed_structured(
                setup, name=f"引号行-{uuid.uuid4().hex[:6]}",
                columns=["seq", "note"],
                columns_typed=[{"name": "seq", "type": "string"},
                               {"name": "note", "type": "string"}],
                rows=pct_rows)

            rows_budget_ds = _seed_structured(
                setup, name=f"行超限-{uuid.uuid4().hex[:6]}",
                columns=["seq"],
                columns_typed=[{"name": "seq", "type": "string"}],
                rows=[{"seq": str(i)} for i in range(60)])

            integ_schema = [
                _typed("id", "integer", "BIGINT", nullable=False),
                _typed("region", "string", "TEXT"),
                _typed("note", "string", "TEXT"),
                _typed("amount", "integer", "BIGINT"),
            ]
            v1_batches = [
                [{"id": 1, "region": "north", "note": "v1-only",
                  "amount": 10},
                 {"id": 2, "region": "north", "note": "n2", "amount": 20},
                 {"id": 3, "region": "south", "note": "n3", "amount": 30}],
                [{"id": 4, "region": "south", "note": "n4", "amount": 40},
                 {"id": 5, "region": "east", "note": "n5", "amount": 50},
                 {"id": 6, "region": "east", "note": "n6", "amount": 60}],
            ]
            snap_ds, snap_run_v1, snap_ver_v1 = _seed_integrated(
                setup, editor, name=f"快照-{uuid.uuid4().hex[:6]}",
                schema=integ_schema, pk=["id"], batches=v1_batches)

            broken_ds, broken_run, broken_ver = _seed_integrated(
                setup, editor, name=f"破损-{uuid.uuid4().hex[:6]}",
                schema=integ_schema, pk=["id"],
                batches=[[{"id": 1, "region": "north", "note": "x",
                           "amount": 1},
                          {"id": 2, "region": "south", "note": "y",
                           "amount": 2}]])

            curated_rows = [
                {"id": "1", "region": "north", "amount": "10"},
                {"id": "2", "region": "north", "amount": "20"},
                {"id": "3", "region": "south", "amount": "30"},
                {"id": "4", "region": "south", "amount": "40"},
            ]
            curated_typed = [
                {"name": "id", "type": "string"},
                {"name": "region", "type": "string"},
                {"name": "amount", "type": "integer"},
            ]
            curated_ds, curated_v1 = _seed_curated(
                setup, name=f"成品-{uuid.uuid4().hex[:6]}",
                rows=curated_rows, columns_typed=curated_typed)
            curated_review = _add_review(
                setup, curated_ds, curated_v1, status="approved")

            timeout_batches = [
                [{"id": i, "val": f"value-{i:05d}"} for i in
                 range(start, start + 1000)]
                for start in range(1, 6001, 1000)
            ]
            timeout_schema = [
                _typed("id", "integer", "BIGINT", nullable=False),
                _typed("val", "string", "TEXT"),
            ]
            timeout_ds, _tr, _tv = _seed_integrated(
                setup, editor, name=f"超时-{uuid.uuid4().hex[:6]}",
                schema=timeout_schema, pk=["id"], batches=timeout_batches)

            world = SimpleNamespace(
                editor_id=editor.id, main_id=main_ds.id,
                empty_id=empty_ds.id, invalid_id=invalid_ds.id,
                wide_id=wide_ds.id, pct_id=pct_ds.id,
                rows_budget_id=rows_budget_ds.id,
                snap_id=snap_ds.id, snap_v1_id=snap_ver_v1.id,
                snap_asset_id=snap_run_v1.asset_id,
                snap_conn_id=snap_ds.source_connection_id,
                broken_id=broken_ds.id, broken_run_id=broken_run.id,
                curated_id=curated_ds.id,
                timeout_id=timeout_ds.id, curated_v1_id=curated_v1.id,
                curated_review_id=curated_review.id,
            )
            report["counts"] = {
                "structured_main_rows": len(main_rows),
                "structured_wide_bytes": sum(
                    len(json.dumps(row).encode()) for row in wide_rows),
                "structured_rows_budget_rows": 60,
                "structured_pct_rows": len(pct_rows),
                "integrated_snapshot_v1_rows": 6,
                "integrated_timeout_rows": 6000,
                "curated_rows": len(curated_rows),
            }
        finally:
            setup.close()

        def _execute(dataset_id, queries, **kwargs):
            session = sessions()
            try:
                return execute_report_queries(
                    session, dataset_id, queries, **kwargs)
            finally:
                session.close()

        # ----------------------------------------------------- sql_core
        stage = "sql_core"
        report["stage"] = stage
        started = time.perf_counter()
        grouped = _execute(world.main_id, {
            "by_region": "SELECT region, COUNT(*) AS n, SUM(amount) AS total "
                         "FROM dataset GROUP BY region ORDER BY region",
        })["by_region"]
        expected = {}
        for row in main_rows:
            slot = expected.setdefault(row["region"], [0, 0])
            slot[0] += 1
            slot[1] += row["amount"]
        assert grouped["source"] == "materialized_temp", grouped["source"]
        got = {row["region"]: (row["n"], row["total"])
               for row in grouped["rows"]}
        assert got == {k: tuple(v) for k, v in expected.items()}, got
        assert grouped["dataset"]["version_no"] == 1
        report["cases"]["manual_structured_blob"] = True

        buckets = _execute(world.main_id, {
            "bucket": "SELECT CASE WHEN amount >= 100 THEN 'high' "
                      "ELSE 'low' END AS bucket, COUNT(*) AS n "
                      "FROM dataset GROUP BY 1 ORDER BY 1",
            "daily": "SELECT date_trunc('day', ts) AS day, COUNT(*) AS n "
                     "FROM dataset GROUP BY 1 ORDER BY 1",
        })
        expected_buckets = {"high": 0, "low": 0}
        for row in main_rows:
            expected_buckets["high" if row["amount"] >= 100 else "low"] += 1
        assert {row["bucket"]: row["n"]
                for row in buckets["bucket"]["rows"]} == expected_buckets
        expected_days: dict[str, int] = {}
        for row in main_rows:
            day = row["ts"][:10]
            expected_days[day] = expected_days.get(day, 0) + 1
        got_days = {str(row["day"])[:10]: row["n"]
                    for row in buckets["daily"]["rows"]}
        assert got_days == expected_days, got_days
        report["cases"]["typed_case_date_trunc"] = True

        literal = _execute(world.main_id, {
            "literal": "SELECT '%(foo)s :bar' AS marker, COUNT(*) AS n "
                       "FROM dataset",
        })["literal"]
        assert literal["rows"][0]["marker"] == "%(foo)s :bar", literal["rows"]
        filtered = _execute(world.main_id, {
            "filtered": "SELECT COUNT(*) AS n FROM dataset "
                        "WHERE (:region IS NULL OR region = :region)",
        }, params={"region": None})["filtered"]
        assert filtered["rows"][0]["n"] == len(main_rows)
        north = _execute(world.main_id, {
            "north": "SELECT COUNT(*) AS n FROM dataset "
                     "WHERE (:region IS NULL OR region = :region)",
        }, params={"region": "north"})["north"]
        assert north["rows"][0]["n"] == 2
        report["cases"]["literal_placeholders"] = True

        injection = _execute(world.main_id, {
            "inject": "SELECT COUNT(*) AS n FROM dataset WHERE region = "
                      ":region",
        }, params={"region": "north' OR '1'='1"})["inject"]
        assert injection["rows"][0]["n"] == 0, injection["rows"]
        still_fine = _execute(world.main_id, {
            "still": "SELECT COUNT(*) AS n FROM dataset",
        })["still"]
        assert still_fine["rows"][0]["n"] == len(main_rows)
        report["cases"]["param_injection_inert"] = True

        single_session = sessions()
        try:
            one = execute_report_query(
                single_session, world.main_id,
                "SELECT COUNT(*) AS n FROM dataset")
            assert one["rows"][0]["n"] == len(main_rows)
            assert one["source"] == "materialized_temp"
        finally:
            single_session.close()
        report["cases"]["single_query_wrapper"] = True
        report["durations_ms"]["sql_core"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # --------------------------------------------------- integrated
        stage = "integrated"
        report["stage"] = stage
        started = time.perf_counter()
        sealed = _execute(world.snap_id, {
            "agg": "SELECT region, COUNT(*) AS n, SUM(amount) AS total "
                   "FROM dataset GROUP BY region ORDER BY region",
        })["agg"]
        assert sealed["source"] == "sealed_relation", sealed["source"]
        assert sealed["dataset"]["version_no"] == 1
        # SUM(bigint) is NUMERIC in PostgreSQL: the result cell is an exact
        # decimal string, so compare numerically rather than by JSON type.
        got = {row["region"]: (row["n"], int(row["total"]))
               for row in sealed["rows"]}
        v1_expected = {}
        for batch in v1_batches:
            for row in batch:
                slot = v1_expected.setdefault(row["region"], [0, 0])
                slot[0] += 1
                slot[1] += row["amount"]
        assert got == {k: tuple(v) for k, v in v1_expected.items()}, got
        report["cases"]["integrated_sealed_relation"] = True

        # Tamper the sealed manifest checksum (real DB row update): the
        # sealed relation must fail its completeness proof and refuse reads.
        tamper_session = sessions()
        try:
            tamper_session.execute(sa_update(IntegratedSyncRun).where(
                IntegratedSyncRun.id == world.broken_run_id).values(
                checksum="00" * 32))
            tamper_session.commit()
        finally:
            tamper_session.close()
        _expect_query_error(
            lambda: _execute(world.broken_id, {"q": "SELECT COUNT(*) AS n "
                                             "FROM dataset"}),
            status=409, code="report_dataset_version_unavailable")
        report["cases"]["integrated_broken_sealed_rejected"] = True
        report["durations_ms"]["integrated"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ------------------------------------------------------ curated
        stage = "curated"
        report["stage"] = stage
        started = time.perf_counter()
        approved_lake = _execute(world.curated_id, {
            "region_sum": "SELECT region, COUNT(*) AS n, SUM(amount) AS total "
                          "FROM dataset GROUP BY region ORDER BY region",
        })["region_sum"]
        assert approved_lake["source"] == "lake_table", approved_lake["source"]
        assert approved_lake["dataset"]["version_no"] == 1
        assert approved_lake["dataset"]["review_status"] == "approved"
        lake_expected = {}
        for row in curated_rows:
            slot = lake_expected.setdefault(row["region"], [0, 0])
            slot[0] += 1
            slot[1] += int(row["amount"])
        assert {row["region"]: (row["n"], int(row["total"]))
                for row in approved_lake["rows"]} == \
            {k: tuple(v) for k, v in lake_expected.items()}, approved_lake
        report["cases"]["curated_lake_approved"] = True

        edit_session = sessions()
        try:
            _add_row_edit(edit_session, _IdRef(world.curated_review_id),
                          row=curated_rows[2], pk_col="id",
                          field="amount", new_value="999")
        finally:
            edit_session.close()
        overlaid = _execute(world.curated_id, {
            "region_sum": "SELECT region, SUM(amount) AS total FROM dataset "
                          "GROUP BY region ORDER BY region",
        })["region_sum"]
        assert overlaid["source"] == "materialized_temp", overlaid["source"]
        overlay_expected = {"north": 30, "south": 999 + 40}
        assert {row["region"]: int(row["total"])
                for row in overlaid["rows"]} == overlay_expected, \
            overlaid["rows"]
        report["cases"]["curated_edit_overlay"] = True

        curated_v2_rows = [
            {"id": "1", "region": "north", "amount": "100"},
            {"id": "2", "region": "west", "amount": "200"},
            {"id": "3", "region": "west", "amount": "300"},
        ]
        curated_session = sessions()
        try:
            version2, _c = None, None
            from app.data_channel.datasets import lake_store
            dataset_ref = curated_session.get(
                type(curated_ds), world.curated_id)
            version2, _changeset = lake_store.upsert_run(
                curated_session, dataset_ref, curated_v2_rows, "overwrite",
                ["id"])
            world.curated_v2_id = version2.id
            curated_session.expire_all()
        finally:
            curated_session.close()

        _expect_query_error(
            lambda: _execute(world.curated_id,
                             {"q": "SELECT COUNT(*) AS n FROM dataset"}),
            status=409, code="report_dataset_version_not_approved")
        pending_session = sessions()
        try:
            dataset_ref = pending_session.get(type(curated_ds), world.curated_id)
            v2_review = _add_review(pending_session, dataset_ref,
                                    _IdRef(world.curated_v2_id),
                                    status="pending")
            v2_review_id = v2_review.id
        finally:
            pending_session.close()
        _expect_query_error(
            lambda: _execute(world.curated_id,
                             {"q": "SELECT COUNT(*) AS n FROM dataset"}),
            status=409, code="report_dataset_version_not_approved")
        rejected_session = sessions()
        try:
            rejected_session.expire_all()
            from app.data_channel.curated.models import CuratedReview
            review_ref = rejected_session.get(CuratedReview, v2_review_id)
            _set_review_status(rejected_session, review_ref, "rejected")
        finally:
            rejected_session.close()
        _expect_query_error(
            lambda: _execute(world.curated_id,
                             {"q": "SELECT COUNT(*) AS n FROM dataset"}),
            status=409, code="report_dataset_version_rejected")
        approved_session = sessions()
        try:
            approved_session.expire_all()
            from app.data_channel.curated.models import CuratedReview
            review_ref = approved_session.get(CuratedReview, v2_review_id)
            _set_review_status(approved_session, review_ref, "approved")
        finally:
            approved_session.close()
        switched = _execute(world.curated_id, {
            "west": "SELECT region, SUM(amount) AS total FROM dataset "
                    "GROUP BY region ORDER BY region",
        })["west"]
        assert switched["source"] == "lake_table", switched["source"]
        assert switched["dataset"]["version_no"] == 2
        assert {row["region"]: int(row["total"])
                for row in switched["rows"]} == {"north": 100,
                                                 "west": 500}, switched
        report["cases"]["curated_pending_rejected_then_approved"] = True

        mods = _execute(world.curated_id, {
            "mod_fn": "SELECT MOD(amount, 2) AS r, COUNT(*) AS n "
                      "FROM dataset GROUP BY 1 ORDER BY 1",
            "mod_op": "SELECT amount % 2 AS r, COUNT(*) AS n "
                      "FROM dataset GROUP BY 1 ORDER BY 1",
        })
        assert [(row["r"], row["n"]) for row in mods["mod_fn"]["rows"]] ==             [(0, 3)], mods["mod_fn"]["rows"]
        assert [(row["r"], row["n"]) for row in mods["mod_op"]["rows"]] ==             [(0, 3)], mods["mod_op"]["rows"]

        escaped = _execute(world.main_id, {
            "plain_pct": "SELECT '100%' AS pct, COUNT(*) AS n FROM dataset",
        })
        assert escaped["plain_pct"]["rows"][0]["pct"] == "100%"
        report["cases"]["mod_and_percent_literals"] = True

        # E-prefixed (byte-string AST node) literal with a percent sign must
        # survive driver-side generation verbatim.
        e_string = _execute(world.main_id, {
            "e_string": "SELECT E'50% x' AS esc, COUNT(*) AS n FROM dataset",
        })["e_string"]
        assert e_string["rows"][0]["esc"] == "50% x", e_string["rows"]
        assert e_string["rows"][0]["n"] == len(main_rows)
        report["cases"]["e_string_literal_executes"] = True
        report["durations_ms"]["curated"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ------------------------------------------------------- guards
        stage = "guards"
        report["stage"] = stage
        started = time.perf_counter()
        empty = _execute(world.empty_id, {
            "count": "SELECT COUNT(*) AS n FROM dataset",
            "select": "SELECT region FROM dataset",
        })
        assert empty["count"]["rows"][0]["n"] == 0
        assert empty["select"]["rows"] == []
        assert empty["select"]["columns"] == ["region"]
        report["cases"]["empty_version"] = True

        _expect_query_error(
            lambda: _execute(world.invalid_id,
                             {"q": "SELECT SUM(qty) AS total FROM dataset"}),
            status=422, code="invalid_typed_value")
        report["cases"]["invalid_typed_value"] = True

        _expect_query_error(
            lambda: _execute(world.main_id, {
                "q": "SELECT region AS dup_name, note AS dup_name "
                     "FROM dataset"}),
            status=422, code="report_duplicate_columns")
        report["cases"]["duplicate_output_columns"] = True

        _expect_query_error(
            lambda: _execute(world.main_id, {"q": "SELECT COUNT(*) AS n "
                                             "FROM v2_users"}),
            status=422, code="report_sql_invalid")
        assert not check_report_sql(
            "SELECT COUNT(*) AS n FROM v2_users").ok
        report["cases"]["malicious_physical_table_rejected"] = True

        _expect_query_error(
            lambda: _execute(world.main_id, {"q": "SELECT COUNT(*) AS n "
                                             "FROM public.v2_users"}),
            status=422, code="report_sql_invalid")
        report["cases"]["malicious_qualified_name_rejected"] = True

        _expect_query_error(
            lambda: _execute(world.main_id, {
                "q": "WITH dataset AS (SELECT 1 AS x) SELECT x FROM dataset"}),
            status=422, code="report_sql_invalid")
        report["cases"]["malicious_cte_shadow_rejected"] = True
        report["durations_ms"]["guards"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ------------------------------------------------------ budgets
        stage = "budgets"
        report["stage"] = stage
        started = time.perf_counter()
        try:
            _execute(world.rows_budget_id,
                     {"q": "SELECT COUNT(*) AS n FROM dataset"})
            raise AssertionError("staging rows budget not enforced")
        except QueryError as exc:
            assert exc.status == 422, (exc.status, exc.code)
            assert exc.code == "report_materialization_budget_exceeded", \
                exc.code
            assert exc.extra.get("budget") == "materialized_rows"
        report["cases"]["staging_rows_budget"] = True

        try:
            _execute(world.wide_id, {"q": "SELECT COUNT(*) AS n FROM dataset"})
            raise AssertionError("staging bytes budget not enforced")
        except QueryError as exc:
            assert exc.status == 422 and \
                exc.code == "report_materialization_budget_exceeded"
            assert exc.extra.get("budget") == "materialized_bytes", exc.extra
        report["cases"]["staging_bytes_budget"] = True

        try:
            _execute(world.main_id, {"q": "SELECT note FROM dataset"}, limit=2)
            raise AssertionError("result rows budget not enforced")
        except QueryError as exc:
            assert exc.status == 422 and \
                exc.code == "report_result_budget_exceeded"
            assert exc.extra.get("budget") == "result_rows"
        report["cases"]["result_rows_budget"] = True

        try:
            _execute(world.pct_id, {"q": "SELECT note FROM dataset"})
            raise AssertionError("result bytes budget not enforced")
        except QueryError as exc:
            assert exc.status == 422 and \
                exc.code == "report_result_budget_exceeded"
            assert exc.extra.get("budget") == "result_bytes", exc.extra
        report["cases"]["result_bytes_budget"] = True
        report["durations_ms"]["budgets"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ------------------------------------------------------ timeout
        stage = "timeout"
        report["stage"] = stage
        started = time.perf_counter()
        # Lower the statement budget for this scenario only (settings are read
        # on every execution); resolving the sealed relation stays well inside
        # it while the 6000 x 6000 concatenation join cannot finish.
        from app.shared.config import settings as app_settings

        normal_timeout_ms = app_settings.unified_query_statement_timeout_ms
        app_settings.unified_query_statement_timeout_ms = TIMEOUT_CASE_MS
        timeout_started = time.perf_counter()
        try:
            try:
                _execute(world.timeout_id, {
                    "slow": "SELECT COUNT(a.val || b.val) AS n "
                            "FROM dataset a JOIN dataset b ON TRUE"})
                raise AssertionError(
                    "statement timeout not enforced after "
                    f"{round((time.perf_counter() - timeout_started) * 1000)}"
                    " ms")
            except QueryError as exc:
                assert exc.status == 422 and \
                    exc.code == "report_budget_exceeded", (exc.status,
                                                           exc.code)
        finally:
            app_settings.unified_query_statement_timeout_ms = \
                normal_timeout_ms
        timeout_elapsed_ms = (time.perf_counter() - timeout_started) * 1000
        # Cancelled near the budget instead of running to completion.
        assert timeout_elapsed_ms < 5_000, timeout_elapsed_ms
        report["timeout_elapsed_ms"] = round(timeout_elapsed_ms, 1)
        # The lowered budget must not leak into later executions.
        after_timeout = _execute(world.timeout_id, {
            "count": "SELECT COUNT(*) AS n FROM dataset"})["count"]
        assert after_timeout["rows"][0]["n"] == 6000, after_timeout["rows"]
        report["cases"]["statement_timeout"] = True
        report["durations_ms"]["timeout"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ----------------------------------------------------- snapshot
        stage = "snapshot"
        report["stage"] = stage
        started = time.perf_counter()
        v2_batches = [
            [{"id": 1, "region": "north", "note": "n1", "amount": 11},
             {"id": 2, "region": "north", "note": "n2", "amount": 21},
             {"id": 3, "region": "south", "note": "n3", "amount": 31},
             {"id": 4, "region": "south", "note": "n4", "amount": 41}],
            [{"id": 5, "region": "east", "note": "n5", "amount": 51}],
        ]
        # Re-seal a second version through the real begin/write/seal path.
        from app.data_channel.datasets import integrated_store as store_mod
        from app.data_channel.integrated.contracts import digest as digest2

        snap_session = sessions()
        try:
            dataset_ref = snap_session.get(type(snap_ds), world.snap_id)
            fingerprint = digest2({"columns": integ_schema,
                                   "primary_key": ["id"]})
            descriptor = {
                "schema": integ_schema, "primary_key": ["id"],
                "schema_fingerprint": fingerprint,
                "asset_id": world.snap_asset_id, "dataset_id": world.snap_id,
                "connection_id": world.snap_conn_id,
                "resource": "public.fixture",
                "expected_rows": sum(len(b) for b in v2_batches),
                "snapshot": {"id": str(uuid.uuid4()), "consistent": True,
                             "kind": "repeatable_read",
                             "started_at": "2026-11-10T09:00:00+00:00"},
            }
            run2, gate2 = store_mod.begin_snapshot(snap_session, descriptor)
            hasher2 = store_mod.snapshot_hasher(run2)
            for batch in v2_batches:
                store_mod.write_snapshot_batch(snap_session, run2, gate2,
                                               batch, hasher2)
            completion2 = {"complete": True, "row_count": run2.expected_rows,
                           "schema_fingerprint":
                               run2.schema_json["schema_fingerprint"],
                           "snapshot_id": run2.snapshot_json["id"]}
            snap_ver_v2 = store_mod.seal_snapshot(
                snap_session, run2, completion2, hasher2.hexdigest())
            world.snap_v2_id = snap_ver_v2.id
            snap_session.expire_all()
        finally:
            snap_session.close()

        # Rewind the pointer to v1: the batch must start on v1 and survive a
        # concurrent pointer move to the already-sealed v2 mid-batch.
        from app.data_channel.datasets.models import Dataset as _DatasetTable

        rewind = sessions()
        try:
            rewind.execute(sa_update(_DatasetTable).where(
                _DatasetTable.id == world.snap_id).values(
                latest_version_id=world.snap_v1_id))
            rewind.commit()
        finally:
            rewind.close()

        with _MidBatchPointerFlip(engine, run_url, world.snap_id,
                                  world.snap_v2_id,
                                  marker='AS snapmark2') as flip:
            pinned = _execute(world.snap_id, {
                "first": "SELECT COUNT(*) AS n FROM dataset",
                "second": "SELECT COUNT(*) AS snapmark2 FROM dataset "
                          "WHERE note = 'v1-only'",
            })
        assert flip.fired and flip.update_backend_pid is not None
        assert pinned["first"]["dataset"]["version_no"] == 1
        assert pinned["second"]["dataset"]["version_no"] == 1
        assert pinned["first"]["rows"][0]["n"] == 6, pinned["first"]["rows"]
        assert pinned["second"]["rows"][0]["snapmark2"] == 1, \
            pinned["second"]["rows"]
        report["cases"]["same_snapshot_mid_batch"] = True

        after = _execute(world.snap_id, {
            "fresh": "SELECT COUNT(*) AS n FROM dataset "
                     "WHERE note = 'v1-only'",
        })["fresh"]
        assert after["dataset"]["version_no"] == 2, after["dataset"]
        assert after["rows"][0]["n"] == 0, after["rows"]
        report["cases"]["integrated_latest_version_switch"] = True
        report["durations_ms"]["snapshot"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # --------------------------------------------------------- pool
        stage = "pool"
        report["stage"] = stage
        started = time.perf_counter()
        from sqlalchemy import event

        executed_pids: list[int] = []

        def _record_pid(conn, cursor, statement, parameters, context,
                        executemany):
            if 'AS poolmark' in statement:
                raw = conn.connection.driver_connection
                probe = raw.cursor()
                probe.execute("SELECT pg_backend_pid()")
                executed_pids.append(int(probe.fetchone()[0]))
                probe.close()

        event.listen(engine, "before_cursor_execute", _record_pid)
        try:
            outer_a = engine.connect()
            try:
                _execute(world.main_id, {"q": "SELECT COUNT(*) AS poolmark "
                                          "FROM dataset"})
                assert _temp_table_count(monitor_engine) == 0
            finally:
                outer_a.close()
            outer_b = engine.connect()
            try:
                _execute(world.main_id, {"q": "SELECT COUNT(*) AS poolmark "
                                          "FROM dataset"})
                assert _temp_table_count(monitor_engine) == 0
            finally:
                outer_b.close()
            for _ in range(3):
                _execute(world.main_id, {"q": "SELECT COUNT(*) AS poolmark "
                                          "FROM dataset"})
                assert _temp_table_count(monitor_engine) == 0
        finally:
            event.remove(engine, "before_cursor_execute", _record_pid)
        report["pool_distinct_pids"] = len(set(executed_pids))
        report["temp_residue_count"] = _temp_table_count(monitor_engine)
        assert len(set(executed_pids)) >= 2, executed_pids
        assert report["temp_residue_count"] == 0
        report["cases"]["pool_rotation_no_residue"] = True
        report["durations_ms"]["pool"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ---------------------------------------------------------- api
        stage = "api"
        report["stage"] = stage
        started = time.perf_counter()
        import app.deps as app_deps
        from app.auth.service import create_access_token
        from app.main import app as fastapi_app
        from fastapi.testclient import TestClient

        def _override_get_db():
            session = sessions()
            try:
                yield session
            finally:
                session.close()

        fastapi_app.dependency_overrides[app_deps.get_db] = _override_get_db
        client = None
        try:
            client = TestClient(fastapi_app)  # no `with`: keep lifespan off
            token = create_access_token({"sub": world.editor_id})
            headers = {"Authorization": f"Bearer {token}"}
            base = "/api/v2/operations/reports"

            created = client.post(base, json={"name": "E2E 运营周报",
                                              "description": "PG 验收"},
                                  headers=headers)
            assert created.status_code == 201, created.text
            report_id = created.json()["data"]["id"]
            assert created.json()["data"]["revision"] == 1

            article_markdown = (
                "# 周报\n\n"
                "```filter\nname: region\ntype: text\n```\n\n"
                "```sql name=by_region\nSELECT region, COUNT(*) AS n "
                "FROM dataset "
                "WHERE (:region IS NULL OR region = :region) "
                "GROUP BY region ORDER BY region\n```\n"
            )
            first = client.post(f"{base}/{report_id}/articles", json={
                "title": "第一篇", "dataset_id": world.main_id,
                "markdown": article_markdown}, headers=headers)
            assert first.status_code == 201, first.text
            first_id = first.json()["data"]["id"]
            assert first.json()["data"]["position"] == 0
            second = client.post(f"{base}/{report_id}/articles", json={
                "title": "第二篇", "dataset_id": world.main_id,
                "markdown": article_markdown}, headers=headers)
            assert second.status_code == 201, second.text
            second_id = second.json()["data"]["id"]
            assert second.json()["data"]["position"] == 1

            detail = client.get(f"{base}/{report_id}", headers=headers)
            assert detail.status_code == 200, detail.text
            assert [a["id"] for a in
                    detail.json()["data"]["articles"]] == [first_id,
                                                           second_id]

            conflict = client.patch(
                f"{base}/{report_id}/articles/{second_id}",
                json={"title": "过期修改", "expected_revision": 99},
                headers=headers)
            assert conflict.status_code == 409 and conflict.json()[
                "detail"]["code"] == "article_revision_conflict"

            report_conflict = client.patch(
                f"{base}/{report_id}",
                json={"description": "旧修改", "expected_revision": 1},
                headers=headers)
            assert report_conflict.status_code == 409 and \
                report_conflict.json()["detail"]["code"] == \
                "report_revision_conflict"

            reordered = client.post(
                f"{base}/{report_id}/articles/reorder",
                json={"article_ids": [second_id, first_id]},
                headers=headers)
            assert reordered.status_code == 200, reordered.text
            assert [item["id"] for item in
                    reordered.json()["data"]["items"]] == [second_id,
                                                           first_id]
            assert [item["position"] for item in
                    reordered.json()["data"]["items"]] == [0, 1]
            report["cases"]["api_crud_reorder_revision"] = True

            executed = client.post(
                f"{base}/{report_id}/articles/{first_id}/execute",
                json={"queries": {
                    "by_region": "SELECT region, COUNT(*) AS n FROM dataset "
                                 "WHERE (:region IS NULL OR region = :region)"
                                 " GROUP BY region ORDER BY region"},
                    "params": {"region": None}, "limit": 100},
                headers=headers)
            assert executed.status_code == 200, executed.text
            payload = executed.json()["data"]["results"]["by_region"]
            assert {row["region"]: row["n"]
                    for row in payload["rows"]} == {"east": 1, "north": 2,
                                                   "south": 2}, payload
            assert payload["source"] == "materialized_temp"
            report["cases"]["api_execute_queries"] = True
        finally:
            if client is not None:
                client.close()
            fastapi_app.dependency_overrides.clear()
        report["durations_ms"]["api"] = round(
            (time.perf_counter() - started) * 1000, 1)

        stage = "complete"
        report["stage"] = stage
        missing_cases = sorted(name for name, ok in report["cases"].items()
                               if not ok)
        assert not missing_cases, f"cases not executed: {missing_cases}"
        report["result"] = "pass"
    except Exception:
        import traceback

        traceback.print_exc()
        report["result"] = "fail"
        report["stage"] = stage
        _write_report(report_path, report, sensitive_values)
        raise SystemExit(
            f"operations reports PostgreSQL E2E failed at stage: {stage}"
        ) from None
    finally:
        for possible_engine in (engine, monitor_engine):
            if possible_engine is not None:
                try:
                    possible_engine.dispose()
                except Exception:  # noqa: BLE001 - cleanup best effort
                    pass
        drop_error: BaseException | None = None
        if admin_engine is not None and run_database is not None:
            try:
                with admin_engine.connect() as conn:
                    conn.exec_driver_sql(
                        f'DROP DATABASE IF EXISTS "{run_database}" '
                        "WITH (FORCE)")
                database_dropped = True
            except Exception as exc:  # noqa: BLE001 - recorded, never hidden
                drop_error = exc
            finally:
                admin_engine.dispose()
        report["database_dropped"] = database_dropped
        if drop_error is not None:
            # A created-but-undropped throwaway database is a failed run even
            # when every case passed; the original exception (if any) still
            # propagates unchanged.
            report["result"] = "fail"
        try:
            _write_report(report_path, report, sensitive_values)
        except Exception:  # noqa: BLE001 - never mask the primary result
            pass
        if drop_error is not None:
            if sys.exc_info()[0] is None:
                raise SystemExit(
                    "operations reports PostgreSQL E2E cleanup failed: "
                    "throwaway database was not dropped") from drop_error
        elif report["result"] == "pass":
            print(f"operations reports PostgreSQL E2E: pass "
                  f"(cases={len(REPORT_CASES)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=DEFAULT_REPORT,
        help=f"write a strictly sanitized 0600 JSON report "
             f"(default: {DEFAULT_REPORT})")
    args = parser.parse_args()
    _run(args.report.resolve())


if __name__ == "__main__":
    main()
