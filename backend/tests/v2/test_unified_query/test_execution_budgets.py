"""Whole-logical-query execution budgets: per-layer + cumulative ledger.

Covers the A-07 segment that per-side/per-stage limits cannot express: stages
of one logical query that each stay under their own budget while the
cumulative total (rows/bytes/source statements) exceeds the whole-query limit
and rejects explicitly naming layer and dimension; count-check overhead
disclosure without fabricated wire bytes; one shared ledger across every
MemoryExecutor of the same logical query (derived-constraint executors
included, never reset per executor); and observable source-stream closure on
mid-budget refusal. The executor under test is the production path.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
import sqlalchemy as sa

from app.data_channel.query.contracts import (ColumnSpec, JoinConstraint, QueryError,
                                              QueryLimits, Relation)
from app.data_channel.query.federated import MemoryExecutor
from app.data_channel.query.memory_budget import QueryExecutionBudget
from app.data_channel.query.parser import parse


class FakeService:
    def __init__(self, limits=None):
        self.limits = limits or QueryLimits()
        self.db = None

    def _deadline(self):
        pass

    def _platform_budget(self):
        pass


def sqlite_relation(alias, table, specs, conn, connection_key, batches=None):
    def default_batches(statement, params, batch_size):
        result = conn.execute(statement, params).mappings()
        rows = [dict(row) for row in result]
        result.close()
        yield rows

    base_state = {"completeness": "complete", "consistency": "repeatable_read",
                  "dialect": "sqlite"}
    return Relation(alias, table, {s.name: s for s in specs}, base_state,
                    "auth:" + connection_key, "schema:" + connection_key + ":" + alias,
                    connection_key, batches=batches or default_batches)


def _tracked_batches(source, state, key):
    def batches(statement, params, batch_size):
        try:
            yield from source.batches(statement, params, batch_size)
        finally:
            state[key] = True
    return batches


@pytest.fixture
def sources():
    pg_meta = sa.MetaData()
    customers = sa.Table("customers", pg_meta, sa.Column("id", sa.BigInteger),
                         sa.Column("region", sa.Text), sa.Column("name", sa.Text))
    my_meta = sa.MetaData()
    orders = sa.Table("orders", my_meta, sa.Column("id", sa.BigInteger),
                      sa.Column("customer_id", sa.BigInteger),
                      sa.Column("total", sa.Numeric(30, 4)))
    pg_engine = sa.create_engine("sqlite://")
    my_engine = sa.create_engine("sqlite://")
    pg_meta.create_all(pg_engine)
    my_meta.create_all(my_engine)
    with pg_engine.begin() as conn:
        conn.execute(customers.insert(), [
            {"id": 1, "region": "north", "name": "acme"},
            {"id": 2, "region": "south", "name": "globex"},
            {"id": 3, "region": "north", "name": "initech"},
        ])
    with my_engine.begin() as conn:
        conn.execute(orders.insert(), [
            {"id": 100, "customer_id": 1, "total": 10},
            {"id": 101, "customer_id": 2, "total": 20},
            {"id": 102, "customer_id": 1, "total": 30},
        ])
    with pg_engine.connect() as pg_conn, my_engine.connect() as my_conn:
        yield {
            "pg_conn": pg_conn, "my_conn": my_conn,
            "customers": customers, "orders": orders,
            "pg": {
                "customers": sqlite_relation("customers", customers, [
                    ColumnSpec("id", "integer"), ColumnSpec("region", "string"),
                    ColumnSpec("name", "string")], pg_conn, "pg"),
            },
            "my": {
                "orders": sqlite_relation("orders", orders, [
                    ColumnSpec("id", "integer"), ColumnSpec("customer_id", "integer"),
                    ColumnSpec("total", "decimal", precision=30, scale=4)], my_conn, "my"),
            },
        }
    pg_engine.dispose()
    my_engine.dispose()


JOIN_QUERY = ("from customers as customers | join orders as orders "
              "on customers.id = orders.customer_id | stats count() as n")


# -- each layer under, cumulative rows over the whole-query total ----------------

def test_layers_under_but_cumulative_rows_over_total(sources):
    limits = QueryLimits(execution_total_rows_limit=6)
    relations = {"customers": sources["pg"]["customers"],
                 "orders": sources["my"]["orders"]}
    executor = MemoryExecutor(FakeService(limits), parse(JOIN_QUERY), relations, {})
    with pytest.raises(QueryError) as caught:
        executor.run()
    error = caught.value
    assert (error.status, error.code) == (422, "query_budget_exceeded")
    # Dimension and layer are explicit: the whole logical query, tripped while
    # reading the orders layer.
    assert error.extra["budget"] == "execution_total_rows_limit"
    assert error.extra["layer"] == "logical_query"
    assert error.extra["stage"] == "orders"
    assert error.extra["limit"] == 6 and error.extra["actual"] == 7
    by_layer = error.extra["byLayer"]
    # Every layer stayed far under its own per-side budget (default 1,000,000):
    # customers = 1 count row + 3 streamed rows, orders = 3 key-filtered rows.
    assert by_layer["customers"]["rows"] == 4
    assert by_layer["orders"]["rows"] == 3
    assert all(usage["rows"] < limits.federated_side_rows_limit
               for usage in by_layer.values())
    # Refusal states incompleteness; it never claims "not executed" or release.
    assert "incomplete" in error.message
    assert "released" not in error.extra


def test_layers_under_but_cumulative_bytes_over_total(sources):
    relations = {"customers": sources["pg"]["customers"],
                 "orders": sources["my"]["orders"]}
    # First pass with default (huge) totals measures the deterministic encoded
    # bytes of each layer; both layers stay under the per-side byte budget.
    baseline = MemoryExecutor(FakeService(), parse(JOIN_QUERY), relations, {}).run()
    budget = baseline.plan["executionBudget"]
    by_layer = budget["byLayer"]
    assert by_layer["customers"]["bytes"] > 0 and by_layer["orders"]["bytes"] > 0
    default_limits = QueryLimits()
    assert all(usage["bytes"] < default_limits.federated_side_bytes_limit
               for usage in by_layer.values())
    total = budget["totals"]["bytes"]
    # Second pass: each layer again under its own budget, cumulative one byte
    # over the whole-query total.
    tight = QueryLimits(execution_total_bytes_limit=total - 1)
    executor = MemoryExecutor(FakeService(tight), parse(JOIN_QUERY), relations, {})
    with pytest.raises(QueryError) as caught:
        executor.run()
    assert caught.value.extra["budget"] == "execution_total_bytes_limit"
    assert caught.value.extra["layer"] == "logical_query"
    assert caught.value.extra["limit"] == total - 1
    assert caught.value.extra["actual"] == total


# -- repeated source scans bounded by the statements dimension -------------------

def test_source_statements_limit_bounds_repeated_scans(sources):
    # customers: count probe + materialization = 2 statements; the keyed pull
    # of orders would be the third and is refused first.
    limits = QueryLimits(execution_source_statements_limit=2)
    relations = {"customers": sources["pg"]["customers"],
                 "orders": sources["my"]["orders"]}
    executor = MemoryExecutor(FakeService(limits), parse(JOIN_QUERY), relations, {})
    with pytest.raises(QueryError) as caught:
        executor.run()
    assert caught.value.extra["budget"] == "execution_source_statements_limit"
    assert caught.value.extra["stage"] == "orders"
    assert caught.value.extra["actual"] == 3
    # With room for a single statement, the count probe passes and the
    # materialization stream itself is refused on the customers layer.
    tighter = QueryLimits(execution_source_statements_limit=1)
    executor = MemoryExecutor(FakeService(tighter), parse(JOIN_QUERY), relations, {})
    with pytest.raises(QueryError) as caught:
        executor.run()
    assert caught.value.extra["budget"] == "execution_source_statements_limit"
    assert caught.value.extra["stage"] == "customers"


# -- count-check overhead disclosed, never fabricated as wire bytes --------------

def test_count_checks_disclosed_without_fabricated_bytes(sources):
    # Unit contract: the marker alone discloses the probe — no rows, no bytes,
    # no statement double charge (the statement is charged once at the read
    # boundary).
    ledger = QueryExecutionBudget(QueryLimits())
    ledger.charge_count_check("probe")
    assert ledger.layer_usage("probe") == {"rows": 0, "bytes": 0,
                                           "statements": 0, "countChecks": 1}
    # Integration: the materialized side runs one COUNT probe; its single
    # returned aggregate row is counted, and totals stay consistent (the count
    # row adds no measured bytes because nothing beyond it was transferred).
    relations = {"customers": sources["pg"]["customers"],
                 "orders": sources["my"]["orders"]}
    result = MemoryExecutor(FakeService(), parse(JOIN_QUERY), relations, {}).run()
    budget = result.plan["executionBudget"]
    by_layer = budget["byLayer"]
    assert by_layer["customers"]["countChecks"] == 1
    assert by_layer["customers"]["statements"] == 2  # count probe + stream
    assert by_layer["orders"]["statements"] == 1 and by_layer["orders"]["countChecks"] == 0
    assert budget["totals"] == {"rows": 7, "bytes": by_layer["customers"]["bytes"]
                                + by_layer["orders"]["bytes"],
                                "statements": 3, "countChecks": 1}


# -- one shared ledger across the executors of one logical query ------------------

def test_shared_ledger_across_derived_constraint_and_main_executors(sources):
    service = FakeService(QueryLimits(execution_total_rows_limit=4))
    service.execution_budget = QueryExecutionBudget(service.limits)
    relations = {"customers": sources["pg"]["customers"],
                 "orders": sources["my"]["orders"]}
    join_pipeline = parse("from customers as customers | join orders as orders "
                          "on customers.id = orders.customer_id")
    # Stage 1: the derived-constraint executor (as _validate_derived_constraints
    # builds it) — a uniqueness COUNT probe over customers.
    constraint_executor = MemoryExecutor(service, join_pipeline, relations, {})
    constraint_executor.check_join_constraints({"orders": JoinConstraint("1:N")})
    assert service.execution_budget.layer_usage("customers") == {
        "rows": 1, "bytes": 0, "statements": 1, "countChecks": 1}
    # Stage 2: the main executor continues the same account (never a reset),
    # and the cumulative rows dimension trips mid-stream.
    main_executor = MemoryExecutor(service, parse(JOIN_QUERY), relations, {})
    assert main_executor.execution_budget is service.execution_budget
    assert constraint_executor.execution_budget is service.execution_budget
    with pytest.raises(QueryError) as caught:
        main_executor.run()
    error = caught.value
    assert error.extra["budget"] == "execution_total_rows_limit"
    assert error.extra["actual"] == 5  # probe row + count row + 3 customer rows
    totals = service.execution_budget.layer_totals()
    assert totals["customers"]["rows"] == 5
    # Truthful schema: a layer with no executed statements is absent, not a
    # zero row — the orders pull never started after the refusal.
    assert "orders" not in totals


def test_standalone_fallback_ledgers_are_not_service_wiring(sources):
    # Without a service-owned ledger, direct construction gets self-contained
    # fallbacks; they must not pretend the stages shared one account.
    service = FakeService()
    assert not hasattr(service, "execution_budget")
    relations = {"customers": sources["pg"]["customers"]}
    pipeline = parse("from customers as customers | fields customers.id")
    first = MemoryExecutor(service, pipeline, relations, {})
    second = MemoryExecutor(service, pipeline, relations, {})
    assert first.execution_budget is not second.execution_budget
    # The explicit argument wins over both fallbacks.
    shared = QueryExecutionBudget(service.limits)
    third = MemoryExecutor(service, pipeline, relations, {}, execution_budget=shared)
    assert third.execution_budget is shared


# -- observable release on mid-budget refusal -------------------------------------

def test_budget_refusal_closes_source_stream(sources):
    closed = {"customers": False}
    tracked = replace(sources["pg"]["customers"],
                      batches=_tracked_batches(sources["pg"]["customers"], closed, "customers"))
    relations = {"customers": tracked, "orders": sources["my"]["orders"]}
    # count row + 2 streamed rows fit; the third customer row refuses
    # mid-stream and the stream must close observably, deterministically.
    limits = QueryLimits(execution_total_rows_limit=3)
    executor = MemoryExecutor(FakeService(limits), parse(JOIN_QUERY), relations, {})
    with pytest.raises(QueryError) as caught:
        executor.run()
    assert caught.value.extra["budget"] == "execution_total_rows_limit"
    assert closed["customers"] is True


def test_mark_released_records_only_confirmed_events():
    ledger = QueryExecutionBudget(QueryLimits())
    ledger.charge_rows("a")
    try:
        ledger.charge_bytes("a", ledger.total_bytes_limit + 1)
    except QueryError as error:
        # The rejection itself never claims release.
        assert "released" not in error.extra
    else:  # pragma: no cover - the charge must reject
        raise AssertionError("charge_bytes must reject over the total")
    # Only after the real close completed does the caller record the event.
    ledger.mark_released("ticket")
    ledger.mark_released("source_scopes")
    released = ledger.snapshot()["released"]
    assert [item["what"] for item in released] == ["ticket", "source_scopes"]
    assert all(isinstance(item["elapsedMs"], int) for item in released)


# -- plan observability ------------------------------------------------------------

def test_plan_carries_execution_budget(sources):
    single = parse("from customers as customers | fields customers.id")
    result = MemoryExecutor(FakeService(), single,
                            {"customers": sources["pg"]["customers"]}, {}).run()
    budget = result.plan["executionBudget"]
    assert result.plan["strategy"] == "memory_source_pushdown"
    assert budget["totals"]["rows"] == 3 and budget["totals"]["statements"] == 1
    assert budget["totals"]["countChecks"] == 0
    assert budget["byLayer"]["customers"]["rows"] == 3
    assert budget["byLayer"]["customers"]["bytes"] > 0
    assert budget["limits"] == {"totalRows": QueryLimits().execution_total_rows_limit,
                                "totalBytes": QueryLimits().execution_total_bytes_limit,
                                "sourceStatements": QueryLimits().execution_source_statements_limit}
    assert isinstance(budget["elapsedMs"], int) and budget["timeoutMs"] > 0
    # The memory dimension reports the shared reservation (present here via the
    # executor's own MemoryBudget fallback).
    assert budget["memory"]["limitBytes"] == result.plan["memoryBudget"]["limitBytes"]


def test_snapshot_is_counts_only(sources):
    relations = {"customers": sources["pg"]["customers"],
                 "orders": sources["my"]["orders"]}
    result = MemoryExecutor(FakeService(), parse(JOIN_QUERY), relations, {}).run()
    budget = result.plan["executionBudget"]
    assert set(budget["byLayer"]) == {"customers", "orders"}
    for usage in budget["byLayer"].values():
        assert set(usage) == {"rows", "bytes", "statements", "countChecks"}
        assert all(type(value) is int for value in usage.values())
    # Serializable, and no source row value ever entered the ledger: every
    # layer entry is int-typed, so the string cell values below cannot occur.
    serialized = json.dumps(budget)
    for row_value in ("acme", "globex", "initech", "north", "south"):
        assert row_value not in serialized


def test_existing_side_limit_errors_name_layer(sources):
    # Pre-existing per-stage limits keep their semantics and gain the layer.
    limits = QueryLimits(memory_result_bytes_limit=8)
    pipeline = parse("from customers as customers | fields customers.name, customers.region")
    executor = MemoryExecutor(FakeService(limits), pipeline,
                              {"customers": sources["pg"]["customers"]}, {})
    with pytest.raises(QueryError) as caught:
        executor.run()
    assert caught.value.extra["budget"] == "memory_result_bytes_limit"
    assert caught.value.extra["layer"] == "result"


def test_ledger_counts_batch_transfer_not_consumption(sources):
    """A provider batch is the transfer unit: the ledger must never report
    fewer rows than the source already sent when a rejection fires mid-batch.

    native-style provider: materializes the whole batch before yielding once.
    With a total-row limit below the batch size, the refusal must charge (and
    reject on) the full arrived batch, not only the rows consumed so far.
    """
    from types import SimpleNamespace

    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    detail = sa.Table("budget_batch_probe", meta, sa.Column("id", sa.BigInteger))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(detail.insert(), [{"id": i} for i in range(1, 22)])
    transferred = {"rows": 0}

    def native_style_batches(statement, params, batch_size):
        rows = [dict(row) for row in engine.connect().execute(
            statement, params).mappings()]
        transferred["rows"] += len(rows)
        yield rows

    relation = Relation("d", detail, {"id": ColumnSpec("id", "integer")},
                        {"completeness": "complete", "dialect": "sqlite"},
                        "auth:p", "schema:p", "conn:p", batches=native_style_batches)
    limits = QueryLimits(execution_total_rows_limit=5)
    service = SimpleNamespace(limits=limits, db=None,
                              _deadline=lambda: None, _platform_budget=lambda: None)
    executor = MemoryExecutor(service, parse("from d as d | fields d.id"),
                              {"d": relation}, {})
    with pytest.raises(QueryError) as caught:
        executor.run()
    assert caught.value.extra["budget"] == "execution_total_rows_limit"
    ledger_rows = executor.execution_budget.snapshot()["totals"]["rows"]
    assert ledger_rows == transferred["rows"] == 21
    assert caught.value.extra["actual"] == ledger_rows
    engine.dispose()


def test_liveness_probe_refuses_before_sql_when_statement_budget_full(db, admin_user, monkeypatch):
    """Cursor liveness probe: budget refusal BEFORE the SELECT 1, not after.

    With the whole-query statement budget exhausted, the recheck closure must
    reject without issuing the probe (no extra source work past the limit) and
    must not charge anything for the refused probe. With room available the
    probe runs exactly once per unique source and is disclosed as one control
    statement (statements only — its single returned row is control overhead,
    never counted as data rows or bytes).
    """
    from contextlib import ExitStack

    from app.data_channel.integrated import native
    from app.data_channel.connections.models import Connection
    from app.data_channel.query import providers
    from app.data_channel.query.contracts import Binding

    db.add(Connection(id="conn-probe-budget", name="probe source", kind="mysql",
                      config={"host": "127.0.0.1", "port": 56306, "user": "u",
                              "password": "p", "database": "integrated_source"},
                      status="active", created_by=admin_user.id))
    db.commit()

    meta = {"asset-probe": {
        "asset_id": "asset-probe", "dataset_id": "ds", "connection_id": "conn-probe-budget",
        "connection_status": "active", "dialect": "mysql",
        "columns": [{"name": "id", "type": "integer", "nullable": False}],
        "primary_key": ["id"], "schema_fingerprint": "fp-1",
        "auth_fingerprint": "fp-auth", "projection": None}}
    probes = []

    def fake_metadata(_db, _user, asset_ids):
        return {asset_id: meta[asset_id] for asset_id in asset_ids}

    def fake_probe(connection):
        probes.append(connection.id)

    monkeypatch.setattr(native, "resolve_metadata", fake_metadata)
    monkeypatch.setattr(native, "probe_source", fake_probe)
    binding = Binding(kind="integrated", asset_id="asset-probe")
    limits = QueryLimits(execution_source_statements_limit=1)

    # Budget already exhausted: the probe must not run and nothing is charged.
    full = QueryExecutionBudget(limits)
    full.charge_statement("native:conn-probe-budget:control")
    with ExitStack() as stack:
        relation = providers.resolve_integrated(db, admin_user, [("d", binding)],
                                                limits, stack, recheck_only=True,
                                                execution_budget=full)["d"]
    with pytest.raises(QueryError) as caught:
        relation.recheck()
    assert caught.value.extra["budget"] == "execution_source_statements_limit"
    assert probes == []  # no SELECT 1 left the budget
    assert full.layer_totals()["native:conn-probe-budget:control"]["statements"] == 1

    # Room available: exactly one probe per unique source, charged as one
    # control statement with zero rows and zero bytes.
    room = QueryExecutionBudget(QueryLimits(execution_source_statements_limit=4))
    with ExitStack() as stack:
        relation = providers.resolve_integrated(db, admin_user, [("d", binding)],
                                                limits, stack, recheck_only=True,
                                                execution_budget=room)["d"]
    relation.recheck()
    relation.recheck()  # probe memoized per fetch cell: still one round trip
    assert probes == ["conn-probe-budget"]
    assert room.layer_usage("native:conn-probe-budget:control") == {
        "rows": 0, "bytes": 0, "statements": 1, "countChecks": 0}


def test_configured_limits_pick_up_new_dimensions():
    from app.data_channel.query.limits import configured_limits
    limits = configured_limits()
    defaults = QueryLimits()
    assert limits.execution_total_rows_limit == defaults.execution_total_rows_limit
    assert limits.execution_total_bytes_limit == defaults.execution_total_bytes_limit
    assert (limits.execution_source_statements_limit
            == defaults.execution_source_statements_limit)


# -- native source scope: internal COUNT and control overhead disclosed -----------

def test_native_scope_discloses_count_probe_and_control_overhead():
    """The native scope's own statements join the same logical ledger.

    ``iter_batches`` internally runs an EOF-proof COUNT before every stream
    (unless the identical statement was pre-counted); with a ledger attached
    that probe is disclosed as one statement + one returned aggregate row +
    that datum's actual text bytes on the connection layer. SET/reflect
    control round trips are itemized on a separate control layer with zero
    rows/bytes — measured, never guessed, and the source-side scan cost of the
    probe stays on the deadline clock instead of being invented as rows.
    """
    from types import SimpleNamespace

    from app.data_channel.integrated import native

    engine = sa.create_engine("sqlite://")
    table_meta = sa.MetaData()
    customers = sa.Table("customers", table_meta, sa.Column("id", sa.BigInteger),
                         sa.Column("name", sa.Text))
    table_meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(customers.insert(), [{"id": 1, "name": "a"},
                                          {"id": 2, "name": "b"},
                                          {"id": 3, "name": "c"}])
    ledger = QueryExecutionBudget(QueryLimits())
    with engine.connect() as conn:
        asset = SimpleNamespace(id="asset-x", table_name="customers", schema_name=None,
                                schema_fingerprint=None)
        # Pin the fingerprint the scope's own reflection computes so the drift
        # check passes; sqlite issues no scope-open SETs, so control charges
        # here come only from measured reflection round trips.
        _table, _schema, fingerprint = native.reflect(conn, asset)
        asset.schema_fingerprint = fingerprint
        scope = native.NativeSourceSession(
            conn, [asset], {"asset-x": {"connection_id": "conn-x"}},
            {"id": "snap-1", "started_at": "2026-11-01T00:00:00+00:00"},
            execution_budget=ledger)
        setup_control = ledger.layer_usage("native:conn-x:control")
        assert setup_control["statements"] >= 1  # real reflection round trips
        assert setup_control["rows"] == 0 and setup_control["bytes"] == 0
        assert ledger.layer_usage("native:conn-x") == {"rows": 0, "bytes": 0,
                                                       "statements": 0, "countChecks": 0}

        statement = sa.select(scope.tables["asset-x"].c.id)
        assert scope.expected_rows(statement) == 3
        probe = ledger.layer_usage("native:conn-x")
        assert probe == {"rows": 1, "bytes": 1, "statements": 1, "countChecks": 1}

        rows = [row for batch in scope.iter_batches(statement) for row in batch]
        assert [row["id"] for row in rows] == [1, 2, 3]
        # The stream reused the pre-counted statement object: exactly one COUNT
        # disclosed in total, and the three data rows are charged by the outer
        # federated read boundary (alias layer), never double-counted here.
        assert ledger.layer_usage("native:conn-x") == probe
        # Post-stream drift reflection adds further measured control round
        # trips — statements only, still no rows and no fabricated bytes.
        after = ledger.layer_usage("native:conn-x:control")
        assert after["statements"] > setup_control["statements"]
        assert after["rows"] == 0 and after["bytes"] == 0
    engine.dispose()


def test_native_scope_without_ledger_behaves_unchanged():
    from types import SimpleNamespace

    from app.data_channel.integrated import native

    engine = sa.create_engine("sqlite://")
    table_meta = sa.MetaData()
    customers = sa.Table("customers", table_meta, sa.Column("id", sa.BigInteger))
    table_meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(customers.insert(), [{"id": 7}])
    with engine.connect() as conn:
        asset = SimpleNamespace(id="asset-y", table_name="customers", schema_name=None,
                                schema_fingerprint=None)
        _table, _schema, fingerprint = native.reflect(conn, asset)
        asset.schema_fingerprint = fingerprint
        scope = native.NativeSourceSession(
            conn, [asset], {"asset-y": {"connection_id": "conn-y"}},
            {"id": "snap-2", "started_at": "2026-11-01T00:00:00+00:00"})
        assert scope.execution_budget is None and scope.ledger_layer is None
        statement = sa.select(scope.tables["asset-y"].c.id)
        assert scope.expected_rows(statement) == 1
        assert [row["id"] for batch in scope.iter_batches(statement) for row in batch] == [7]
        assert scope.completion["complete"] is True
    engine.dispose()
