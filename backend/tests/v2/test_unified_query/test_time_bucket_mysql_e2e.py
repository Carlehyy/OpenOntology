"""Opt-in real MySQL 8.4 acceptance for time-bucket stats through the query core.

Found in operation: a bucketed ``stats ... by <time field>`` plan with an
``orderBy`` returned 503 against MySQL 8.4 although the compiled SELECT/GROUP BY
executed when pasted directly. Root cause: the NULL-ordering ORDER BY CASE
re-expanded the bucket expression over the derived table's ungrouped base
column, which ONLY_FULL_GROUP_BY rejects with ERROR 1055; the driver error was
then mapped to 503. This module drives the production ``build_typed_request``
and ``QueryService.execute_typed`` memory path against a real MySQL engine.

Safety: explicit ``INTEGRATED_QUERY_E2E=1`` plus an owned loopback MySQL DSN.
The fixture creates one UUID-named table in the isolated fixture database and
always drops it; no other table, schema or database is touched.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.data_channel.integrated import native
from app.data_channel.integrated.native import NativeSourceSession
from app.data_channel.query.compiler import compile_query
from app.data_channel.query.contracts import ColumnSpec, QueryLimits, Relation
from app.data_channel.query.derived import resolve_derived
from app.data_channel.query.handles import REGISTRY
from app.data_channel.query.ontology_adapter import OntologyQueryAdapter, build_typed_request
from app.data_channel.query.parser import Command, Pipeline, Ref, validate_pipeline
from app.data_channel.query.service import QueryService

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATED_QUERY_E2E") != "1",
    reason="explicit isolated real-MySQL opt-in required")

BUCKETS = ("day", "week", "month", "quarter", "year")
ROWS = [
    {"id": 1, "occurred_at": datetime(2026, 1, 3, 10, 0), "name": "a"},   # 周六
    {"id": 2, "occurred_at": datetime(2026, 1, 6, 10, 0), "name": "b"},   # 周二
    {"id": 3, "occurred_at": datetime(2026, 1, 13, 10, 0), "name": "a"},  # 周二
    {"id": 4, "occurred_at": datetime(2026, 2, 20, 10, 0), "name": "b"},  # 周五
    {"id": 5, "occurred_at": datetime(2026, 3, 8, 10, 0), "name": "a"},   # 周日
    {"id": 6, "occurred_at": datetime(2026, 10, 4, 10, 0), "name": "c"},  # 周日
    {"id": 7, "occurred_at": None, "name": "d"},
]
EXPECTED = {
    "day": [("2026-01-03", 1), ("2026-01-06", 1), ("2026-01-13", 1),
            ("2026-02-20", 1), ("2026-03-08", 1), ("2026-10-04", 1), (None, 1)],
    "week": [("2025-12-29", 1), ("2026-01-05", 1), ("2026-01-12", 1),
             ("2026-02-16", 1), ("2026-03-02", 1), ("2026-09-28", 1), (None, 1)],
    "month": [("2026-01-01", 3), ("2026-02-01", 1), ("2026-03-01", 1),
              ("2026-10-01", 1), (None, 1)],
    "quarter": [("2026-01-01", 5), ("2026-10-01", 1), (None, 1)],
    "year": [("2026-01-01", 6), (None, 1)],
}


def _mysql_url() -> str:
    raw = os.environ.get("INTEGRATED_QUERY_MYSQL_DSN")
    if not raw:
        pytest.fail("INTEGRATED_QUERY_MYSQL_DSN is required; no default/production fallback")
    url = sa.engine.make_url(raw)
    if (url.get_backend_name() != "mysql" or url.host != "127.0.0.1"
            or url.database != "integrated_source" or url.query or "#" in raw):
        pytest.fail("DSN must be the owned loopback MySQL integrated_source fixture database")
    return raw


class _MySQLService(QueryService):
    """Resolve every authorized leaf to the owned MySQL table.

    Mirrors the production resolve shape (leaf relation keyed by binding name,
    derived FIELDS relations compiled over it) without requiring platform
    metadata rows; the compiler/executor/budget code under test is production.
    """

    def __init__(self, engine, table, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._engine = engine
        self._table = table

    def _resolve(self, plan, *, recheck_only: bool = False):
        leaf_aliases: dict[str, str] = {}
        for name, alias in plan.pipeline.sources:
            if name in plan.bindings:
                leaf_aliases.setdefault(name, alias)
        for spec in plan.derived.values():
            for name, alias in validate_pipeline(spec.pipeline).sources:
                if name in plan.bindings:
                    leaf_aliases.setdefault(name, alias)
        columns = {
            "id": ColumnSpec("id", "integer", False),
            "occurred_at": ColumnSpec("occurred_at", "timestamp"),
            "name": ColumnSpec("name", "string"),
        }
        for name, binding in plan.bindings.items():
            alias = leaf_aliases[name]
            connection = self.stack.enter_context(
                self._engine.connect().execution_options(isolation_level="REPEATABLE READ"))
            self.stack.enter_context(connection.begin())
            bound = self._table.alias(alias)

            def batches(statement, params, size, connection=connection):
                result = connection.execution_options(
                    stream_results=True).execute(statement, params or {}).mappings()
                try:
                    while batch := result.fetchmany(size):
                        yield [dict(row) for row in batch]
                finally:
                    result.close()

            self.relations[name] = Relation(
                alias, bound, columns,
                {"alias": alias, "kind": "integrated", "id": binding.asset_id,
                 "versionId": None, "asOf": "2026-11-05T00:00:00+00:00",
                 "consistency": "repeatable_read", "completeness": "complete",
                 "dialect": "mysql", "uniqueKey": ["id"]},
                "auth", "schema", "source:mysql-time-bucket", batches=batches)
        if plan.derived:
            self.relations = resolve_derived(self.relations, plan.derived)


@pytest.fixture
def mysql_fixture():
    engine = sa.create_engine(_mysql_url())
    name = "uqe_bucket_" + uuid.uuid4().hex
    table = sa.Table(
        name, sa.MetaData(),
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.Column("name", sa.Text()))
    table.create(engine)
    with engine.begin() as connection:
        connection.execute(table.insert(), ROWS)
    try:
        yield engine, table
    finally:
        table.drop(engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_registry():
    REGISTRY.handles.clear()
    REGISTRY._tickets.clear()
    yield
    REGISTRY.handles.clear()
    REGISTRY._tickets.clear()


def _plan(bucket: str, *, page_size: int = 100, stats_limit: int | None = None) -> dict:
    plan = {
        "pageSize": page_size,
        "releases": [], "citations": [],
        "sources": [{
            "alias": "orders",
            "binding": {"kind": "integrated", "assetId": "asset-mysql-bucket"},
            "declaredTypes": {"id": "number", "occurred_at": "datetime", "name": "string"},
            "propertyMap": {"id": "id", "occurred_at": "occurred_at", "name": "name"},
        }],
        "sourceFilters": [], "joins": [], "where": [],
        "groupBy": [{"source": "orders", "property": "occurred_at", "bucket": bucket}],
        "aggregates": [{"fn": "count", "as": "n"}],
        "select": [{"source": "orders", "property": "occurred_at", "as": "bucket"}],
        "distinct": False,
        "orderBy": [{"output": "bucket", "direction": "asc", "nulls": "last"}],
    }
    if stats_limit is not None:
        plan["statsLimit"] = stats_limit
    return plan


def _execute(engine, table, plan: dict):
    typed = build_typed_request(plan, request={"plan": plan["groupBy"]},
                                session_kind="ontology", session_id="time-bucket-e2e")
    service = _MySQLService(
        engine, table,
        SimpleNamespace(in_transaction=lambda: False, rollback=lambda: None),
        SimpleNamespace(id="uqe-bucket-" + uuid.uuid4().hex),
        secret="isolated-mysql-bucket-test-secret",
        limits=QueryLimits())
    return service.execute_typed(typed)


@pytest.mark.parametrize("bucket", BUCKETS)
def test_mysql_time_bucket_stats_sorted_by_bucket(mysql_fixture, bucket):
    engine, table = mysql_fixture
    data = _execute(engine, table, _plan(bucket))
    assert [(row["bucket"], int(row["n"])) for row in data["rows"]] == EXPECTED[bucket]
    assert data["totalRows"] == len(EXPECTED[bucket])
    assert data["resultLimited"] is False
    assert [column["name"] for column in data["columns"]] == ["bucket", "n"]
    budget = data["plan"]["executionBudget"]
    assert budget["timeoutMs"] == QueryLimits().statement_timeout_ms == 300_000
    assert budget["totals"]["statements"] == 1  # one bucket statement, no count probe
    assert budget["totals"]["rows"] == len(EXPECTED[bucket])


def test_mysql_half_hour_bucket_floors_to_half_hours(mysql_fixture):
    """half_hour keeps a timestamp and floors to :00/:30 on MySQL 8.4.

    Real-run path: module/window distinct analysis aggregates per half hour;
    the value must stay a timestamp (not a date) and be rounded downward.
    """
    engine, _fixture_table = mysql_fixture
    name = "uqe_half_" + uuid.uuid4().hex
    table = sa.Table(
        name, sa.MetaData(),
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.Column("name", sa.Text()))
    table.create(engine)
    try:
        with engine.begin() as connection:
            connection.execute(table.insert(), [
                {"id": 1, "occurred_at": datetime(2026, 1, 1, 10, 0, 0), "name": "a"},
                {"id": 2, "occurred_at": datetime(2026, 1, 1, 10, 29, 59), "name": "b"},
                {"id": 3, "occurred_at": datetime(2026, 1, 1, 10, 30, 0), "name": "a"},
                {"id": 4, "occurred_at": datetime(2026, 1, 1, 10, 59, 59), "name": "b"},
                {"id": 5, "occurred_at": datetime(2026, 1, 1, 23, 59, 59), "name": "c"},
                {"id": 6, "occurred_at": None, "name": "d"},
            ])
        data = _execute(engine, table, _plan("half_hour"))
        assert [(row["bucket"], int(row["n"])) for row in data["rows"]] == [
            ("2026-01-01T10:00:00", 2), ("2026-01-01T10:30:00", 2),
            ("2026-01-01T23:30:00", 1), (None, 1)]
        assert data["columns"][0]["type"] == "timestamp"
        assert data["columns"][0]["encoding"] == "iso8601"
        assert data["plan"]["executionBudget"]["timeoutMs"] == 300_000
    finally:
        table.drop(engine)


def test_native_read_port_executes_half_hour_bucket_and_keeps_raw_sql_denied(mysql_fixture):
    """The real native read port accepts the compiled half-hour statement.

    Regression: the half-hour expression must be a typed SQLAlchemy expression;
    TIMESTAMPADD unit keywords or interval literal columns would be rejected by
    NativeSourceSession._validate_select (RAW_SQL_DENIED) before execution.
    """
    engine, _fixture_table = mysql_fixture
    name = "uqe_native_half_" + uuid.uuid4().hex
    table = sa.Table(
        name, sa.MetaData(),
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.Column("name", sa.Text()))
    table.create(engine)
    connection = engine.connect()
    try:
        with connection.begin():
            connection.execute(table.insert(), [
                {"id": 1, "occurred_at": datetime(2026, 1, 1, 10, 0, 0), "name": "a"},
                {"id": 2, "occurred_at": datetime(2026, 1, 1, 10, 29, 59), "name": "b"},
                {"id": 3, "occurred_at": datetime(2026, 1, 1, 10, 30, 0), "name": "a"},
                {"id": 4, "occurred_at": datetime(2026, 1, 1, 10, 59, 59), "name": "b"},
                {"id": 5, "occurred_at": datetime(2026, 1, 1, 23, 59, 59), "name": "c"},
                {"id": 6, "occurred_at": None, "name": "d"},
            ])
        asset = SimpleNamespace(
            id="asset-native-half", table_name=name,
            schema_name=engine.url.database, dataset_id="dataset-native-half",
            connection_id="connection-native-half", schema_fingerprint=None)
        _, _schema, fingerprint = native.reflect(connection, asset)
        asset.schema_fingerprint = fingerprint
        scope = NativeSourceSession(
            connection, [asset],
            {asset.id: {"connection_id": asset.connection_id}},
            {"id": str(uuid.uuid4()), "started_at": "2026-11-05T00:00:00+00:00"})
        relation = Relation(
            "t", scope.tables[asset.id].alias("t"),
            {"id": ColumnSpec("id", "integer", False),
             "occurred_at": ColumnSpec("occurred_at", "timestamp"),
             "name": ColumnSpec("name", "string")},
            {"dialect": "mysql", "completeness": "complete"},
            "auth", "schema", "source:mysql-native-half")
        pipeline = Pipeline("t", "t", (
            Command("stats", (("count", None, "n"),
                              ("group", ((Ref("t", "occurred_at"), "bucket", "half_hour"),)))),
            Command("sort", (("bucket", "asc", "last"),)),
        ))
        compiled = compile_query(pipeline, {"t": relation}, {}, dialect="mysql")
        assert scope.expected_rows(compiled.statement, compiled.params) == 4
        rows = [row for batch in
                scope.iter_batches(compiled.statement, compiled.params, batch_size=2)
                for row in batch]
        assert [(row["bucket"].isoformat() if row["bucket"] else None, int(row["n"]))
                for row in rows] == [
            ("2026-01-01T10:00:00", 2), ("2026-01-01T10:30:00", 2),
            ("2026-01-01T23:30:00", 1), (None, 1)]
        assert scope.completion["complete"] is True
        assert scope.completion["row_count"] == 4
        # The guard is not loosened: raw fragments stay denied.
        for raw in (sa.text("MINUTE"), sa.literal_column("interval '30 minutes'")):
            with pytest.raises(HTTPException) as exc:
                scope.expected_rows(sa.select(raw))
            assert exc.value.status_code == 422
            assert exc.value.detail["code"] == "RAW_SQL_DENIED"
    finally:
        connection.close()
        table.drop(engine)


def test_mysql_bounded_bucket_stats_count_probe_wraps_grouped_statement(mysql_fixture):
    """statsLimit's EOF count wraps the compiled bucket statement on MySQL.

    The pre-aggregation COUNT must execute without the ONLY_FULL_GROUP_BY 1055
    failure (ORDER BY is stripped before wrapping) and must report the full
    group count, not the limited page.
    """
    engine, table = mysql_fixture
    data = _execute(engine, table, _plan("month", page_size=2, stats_limit=2))
    assert [(row["bucket"], int(row["n"])) for row in data["rows"]] == [
        ("2026-01-01", 3), ("2026-02-01", 1)]
    assert data["resultLimited"] is True
    # The materialized rows are the LIMIT prefix, but the measured pre-LIMIT
    # group count remains exact and paging still ends at the materialized rows.
    assert data["totalRows"] == 2
    assert data["plan"]["totalRowsBeforeLimit"] == 5
    assert data["page"]["hasMore"] is False and data["page"]["cursor"] is None
    budget = data["plan"]["executionBudget"]
    assert budget["timeoutMs"] == QueryLimits().statement_timeout_ms == 300_000
    # One EOF-proof COUNT over the grouped statement plus the limited page
    # statement; both are disclosed on the ledger.
    assert budget["totals"]["statements"] == 2
    assert budget["totals"]["countChecks"] == 1


def test_ontology_adapter_response_for_mysql_bucket_plan(mysql_fixture):
    """The public adapter response keeps its contract for bucket plans."""
    engine, table = mysql_fixture
    user = SimpleNamespace(id="uqe-bucket-" + uuid.uuid4().hex)
    adapter = OntologyQueryAdapter()
    adapter._service = lambda db, _user: _MySQLService(
        engine, table, db, user, secret="isolated-mysql-bucket-test-secret",
        limits=QueryLimits())
    db = SimpleNamespace(in_transaction=lambda: False, rollback=lambda: None)

    plan = _plan("month")
    data = adapter.execute(db, user, plan, request={"plan": plan["groupBy"]},
                           session_kind="ontology", session_id="time-bucket-e2e")
    assert [(row["bucket"], int(row["n"])) for row in data["rows"]] == EXPECTED["month"]
    assert data["totalRowCount"] == len(EXPECTED["month"])
    assert data["exact"] is True and data["truncated"] is False
    assert data["resultBounded"] is False and data["partial"] is False
    assert data["persistence"]["mode"] == "ephemeral"
    assert data["hasNext"] is False and data["cursor"] is None

    bounded = _plan("month", page_size=2, stats_limit=2)
    data = adapter.execute(db, user, bounded, request={"plan": bounded["groupBy"]},
                           session_kind="ontology", session_id="time-bucket-e2e-bounded")
    assert [(row["bucket"], int(row["n"])) for row in data["rows"]] == [
        ("2026-01-01", 3), ("2026-02-01", 1)]
    assert data["truncated"] is True and data["resultBounded"] is True
    assert data["exact"] is True and data["completeness"] == "complete"
    # Exact pre-LIMIT group count with no cursor: the remainder is unreachable.
    assert data["totalRowCount"] == 5 and data["hasNext"] is False
    assert data["cursor"] is None
