"""Memory-mode federated execution: typed pipelines, derived relations,
in-process handles, budgets, cancellation, and SQL-semantics parity.

Remote-pagination recheck contracts live here too: execute opens remote read
scopes, while cursor paging (persisted snapshots and memory handles) never
reopens a source scope, never reflects schema and never reads source rows —
it revalidates platform metadata (recheck_only), rejects the explicit
failed-test "error" lifecycle state, fences lifecycle-status changes through
the auth fingerprint, and proves physical availability with one bounded
SELECT 1 liveness probe per unique source per page fetch.

Deterministic SQLite-backed relations stand in for physical sources; the
executor under test is the production code path (batches/streaming/budgets).
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.data_channel.query.contracts import (Binding, ColumnSpec, DerivedSourceSpec,
                                              JoinConstraint, QueryError, QueryLimits,
                                              Relation, TextQueryRequest,
                                              TypedQueryRequest)
from app.data_channel.query.derived import derived_relation, resolve_derived
from app.data_channel.query.federated import MemoryExecutor
from app.data_channel.query.handles import INSTANCE_ID, REGISTRY
from app.data_channel.query.parser import (Command, Expr, Pipeline, Ref, Value,
                                           parse, pipeline_from_dict, pipeline_to_dict,
                                           validate_pipeline)
from app.data_channel.query.compiler import compile_predicate, compile_query
from app.data_channel.query.service import QueryService


class FakeService:
    def __init__(self, limits=None):
        self.limits = limits or QueryLimits()
        self.db = None

    def _deadline(self):
        pass

    def _platform_budget(self):
        pass


def sqlite_relation(alias, table, specs, conn, connection_key, state=None):
    def batches(statement, params, batch_size):
        result = conn.execute(statement, params).mappings()
        rows = [dict(row) for row in result]
        result.close()
        yield rows
    base_state = {"completeness": "complete", "consistency": "repeatable_read",
                  "dialect": "sqlite"}
    if state:
        base_state.update(state)
    return Relation(alias, table, {s.name: s for s in specs}, base_state,
                    "auth:" + connection_key, "schema:" + connection_key + ":" + alias,
                    connection_key, batches=batches)


@pytest.fixture
def sources():
    pg_meta = sa.MetaData()
    customers = sa.Table("customers", pg_meta, sa.Column("id", sa.BigInteger),
                         sa.Column("region", sa.Text), sa.Column("name", sa.Text))
    contracts = sa.Table("contracts", pg_meta, sa.Column("id", sa.BigInteger),
                         sa.Column("customer_id", sa.BigInteger),
                         sa.Column("amount", sa.Numeric(30, 4)))
    my_meta = sa.MetaData()
    orders = sa.Table("orders", my_meta, sa.Column("id", sa.BigInteger),
                      sa.Column("customer_id", sa.BigInteger),
                      sa.Column("total", sa.Numeric(30, 4)))
    shipments = sa.Table("shipments", my_meta, sa.Column("id", sa.BigInteger),
                         sa.Column("order_id", sa.BigInteger),
                         sa.Column("qty", sa.BigInteger))
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
        conn.execute(contracts.insert(), [
            {"id": 10, "customer_id": 1, "amount": Decimal("100.5000")},
            {"id": 11, "customer_id": 1, "amount": Decimal("50.2500")},
            {"id": 12, "customer_id": 3, "amount": Decimal("7.0000")},
        ])
    with my_engine.begin() as conn:
        conn.execute(orders.insert(), [
            {"id": 100, "customer_id": 1, "total": Decimal("10.0000")},
            {"id": 101, "customer_id": 2, "total": Decimal("20.0000")},
            {"id": 102, "customer_id": 1, "total": Decimal("30.0000")},
        ])
        conn.execute(shipments.insert(), [
            {"id": 1000, "order_id": 100, "qty": 3},
            {"id": 1001, "order_id": 100, "qty": 4},
            {"id": 1002, "order_id": 101, "qty": 5},
        ])
    with pg_engine.connect() as pg_conn, my_engine.connect() as my_conn:
        yield {
            "pg_conn": pg_conn, "my_conn": my_conn,
            "customers": customers, "contracts": contracts,
            "orders": orders, "shipments": shipments,
            "pg": {
                "customers": sqlite_relation("customers", customers, [
                    ColumnSpec("id", "integer"), ColumnSpec("region", "string"),
                    ColumnSpec("name", "string")], pg_conn, "pg"),
                "contracts": sqlite_relation("contracts", contracts, [
                    ColumnSpec("id", "integer"), ColumnSpec("customer_id", "integer"),
                    ColumnSpec("amount", "decimal", precision=30, scale=4)], pg_conn, "pg"),
            },
            "my": {
                "orders": sqlite_relation("orders", orders, [
                    ColumnSpec("id", "integer"), ColumnSpec("customer_id", "integer"),
                    ColumnSpec("total", "decimal", precision=30, scale=4)], my_conn, "my"),
                "shipments": sqlite_relation("shipments", shipments, [
                    ColumnSpec("id", "integer"), ColumnSpec("order_id", "integer"),
                    ColumnSpec("qty", "integer")], my_conn, "my"),
            },
        }
    pg_engine.dispose()
    my_engine.dispose()


@pytest.fixture(autouse=True)
def clean_registry():
    REGISTRY.handles.clear()
    REGISTRY._tickets.clear()
    yield
    REGISTRY.handles.clear()
    REGISTRY._tickets.clear()


def typed_pipeline(text):
    return parse(text)


# -- derived relations -------------------------------------------------------

def test_derived_relation_same_source_multitable_join(sources):
    base = {"orders": sources["my"]["orders"], "shipments": sources["my"]["shipments"]}
    pipeline = typed_pipeline(
        "from orders as orders | join shipments as shipments on orders.id = shipments.order_id "
        "| stats sum(shipments.qty) as qty, count() as rows by orders.customer_id")
    spec = DerivedSourceSpec(alias="erp_agg", source="orders", pipeline=pipeline, params={})
    relation = derived_relation("erp_agg", base, spec, bind_prefix="dv0")
    assert set(relation.columns) == {"customer_id", "qty", "rows"}
    assert relation.columns["qty"].type == "decimal"
    assert relation.connection_key == "my"
    assert relation.bind_params is None
    service = FakeService()
    executor = MemoryExecutor(service, typed_pipeline("from erp_agg as erp_agg | fields erp_agg.customer_id, erp_agg.qty"),
                              {"erp_agg": relation}, {})
    result = executor.run()
    assert sorted((row["customer_id"], Decimal(row["qty"])) for row in result.rows) == [
        ("1", Decimal("7")), ("2", Decimal("5"))]


def test_derived_relation_with_params_and_where(sources):
    base = {"orders": sources["my"]["orders"]}
    pipeline = typed_pipeline("from orders as orders | where orders.customer_id = :cust "
                              "| fields orders.id, orders.total")
    spec = DerivedSourceSpec(alias="mine", source="orders", pipeline=pipeline, params={"cust": 1})
    relation = derived_relation("mine", base, spec, bind_prefix="dv0")
    assert set(relation.bind_params) == {"dv0_0"}
    executor = MemoryExecutor(FakeService(), typed_pipeline("from mine as mine | fields mine.id"),
                              {"mine": relation}, {})
    result = executor.run()
    assert [row["id"] for row in result.rows] == ["100", "102"]


def test_derived_cross_source_rejected(sources):
    base = {"orders": sources["my"]["orders"], "customers": sources["pg"]["customers"]}
    pipeline = typed_pipeline("from orders as orders | join customers as customers on orders.customer_id = customers.id "
                              "| fields orders.id")
    spec = DerivedSourceSpec(alias="bad", source="orders", pipeline=pipeline, params={})
    with pytest.raises(QueryError) as exc:
        derived_relation("bad", base, spec, bind_prefix="dv0")
    assert exc.value.code == "cross_source_derived_rejected"


def test_resolve_derived_orders_and_caps_depth(sources):
    base = {"orders": sources["my"]["orders"], "shipments": sources["my"]["shipments"]}
    inner = DerivedSourceSpec(
        alias="joined", source="orders",
        pipeline=typed_pipeline("from orders as orders | join shipments as shipments on orders.id = shipments.order_id "
                                "| fields orders.customer_id, shipments.qty"),
        params={})
    resolved = resolve_derived(base, {"joined": inner})
    assert set(resolved) == {"orders", "shipments", "joined"}
    outer = DerivedSourceSpec(
        alias="agg", source="joined",
        pipeline=typed_pipeline("from joined as joined | stats sum(joined.qty) as total by joined.customer_id"),
        params={})
    resolved = resolve_derived(base, {"joined": inner, "agg": outer})
    assert resolved["agg"].columns["total"].type == "decimal"
    # Projection/filter wrappers do not carry user semantics: a wrapper chain
    # is bounded only by the structural path-depth cap (8).
    chain = {"joined": inner, "agg": outer}
    previous = "agg"
    for level in range(3, 9):
        name = f"wrap{level}"
        chain[name] = DerivedSourceSpec(
            alias=name, source=previous,
            pipeline=typed_pipeline(f"from {previous} as {previous} | fields {previous}.customer_id"),
            params={})
        previous = name
    assert resolve_derived(base, chain)[previous].columns["customer_id"].type == "integer"
    deep = DerivedSourceSpec(
        alias="too_deep", source=previous,
        pipeline=typed_pipeline(f"from {previous} as {previous} | stats count() as n"),
        params={})
    with pytest.raises(QueryError) as exc:
        resolve_derived(base, {**chain, "too_deep": deep})
    assert exc.value.code == "query_too_complex"
    cyclic = {"a": DerivedSourceSpec("a", "b", typed_pipeline("from b as b | fields b.id"), {}),
              "b": DerivedSourceSpec("b", "a", typed_pipeline("from a as a | fields a.id"), {})}
    with pytest.raises(QueryError):
        resolve_derived(base, cyclic)


# -- federated execution -------------------------------------------------------

def test_federated_four_table_customer_orders_with_local_aggregation(sources):
    erp_agg = DerivedSourceSpec(
        alias="erp_agg", source="orders",
        pipeline=typed_pipeline(
            "from orders as orders | join shipments as shipments on orders.id = shipments.order_id "
            "| stats sum(shipments.qty) as qty by orders.customer_id"),
        params={})
    crm_agg = DerivedSourceSpec(
        alias="crm_agg", source="customers",
        pipeline=typed_pipeline(
            "from customers as customers | join contracts as contracts on customers.id = contracts.customer_id "
            "| stats sum(contracts.amount) as contracted by customers.id, customers.region"),
        params={})
    relations = resolve_derived(
        {"orders": sources["my"]["orders"], "shipments": sources["my"]["shipments"],
         "customers": sources["pg"]["customers"], "contracts": sources["pg"]["contracts"]},
        {"erp_agg": erp_agg, "crm_agg": crm_agg})
    outer = typed_pipeline(
        "from crm_agg as crm_agg | join erp_agg as erp_agg on crm_agg.id = erp_agg.customer_id "
        "| stats sum(erp_agg.qty) as shipped, sum(crm_agg.contracted) as contracted by crm_agg.region "
        "| sort contracted desc | limit 10")
    executor = MemoryExecutor(FakeService(), outer, relations, {})
    result = executor.run()
    # Local aggregation prevents fanout: north = customer1 (contracted 150.75,
    # shipped 7) + customer3 (7.00, no shipments, INNER join drops it).
    # SQLite renders decimals with padded scale; compare exact numeric values.
    assert len(result.rows) == 1 and result.rows[0]["region"] == "north"
    assert Decimal(result.rows[0]["shipped"]) == Decimal(7)
    assert Decimal(result.rows[0]["contracted"]) == Decimal("150.75")
    assert result.classification == "detail"
    assert result.plan["strategy"] == "memory_federated"
    assert set(result.plan["pushdown"]) == {"crm_agg", "erp_agg"}


def test_federated_left_join_null_extension_and_predicates(sources):
    relations = {"customers": sources["pg"]["customers"], "orders": sources["my"]["orders"]}
    pipeline = typed_pipeline(
        "from customers as customers | join orders as orders on customers.id = orders.customer_id type=left "
        "| where orders.id is null | fields customers.name")
    executor = MemoryExecutor(FakeService(), pipeline, relations, {})
    result = executor.run()
    assert [row["name"] for row in result.rows] == ["initech"]


def test_federated_left_join_right_side_predicate_stays_post_join(sources):
    relations = {"customers": sources["pg"]["customers"], "orders": sources["my"]["orders"]}
    # orders.customer_id is a LEFT-joined alias: its predicate must NOT become a
    # source pre-filter, or the NULL-extended rows would disappear.
    pipeline = typed_pipeline(
        "from customers as customers | join orders as orders on customers.id = orders.customer_id type=left "
        "| where orders.customer_id = 1 or orders.customer_id is null | fields customers.name")
    executor = MemoryExecutor(FakeService(), pipeline, relations, {})
    result = executor.run()
    assert sorted(row["name"] for row in result.rows) == ["acme", "acme", "initech"]


def test_federated_group_alias_and_stats_parity_with_sql(sources):
    # Split connections force the bounded in-memory aggregation path; the
    # persisted compiler over the same data must agree.
    shipments2 = replace(sources["my"]["shipments"], connection_key="my2",
                         schema_fingerprint="schema:my2:shipments")
    relations = {"orders": sources["my"]["orders"], "shipments": shipments2}
    pipeline = Pipeline(
        source="orders", alias="orders",
        commands=(Command("join", ("shipments", "shipments", Ref("orders", "id"), Ref("shipments", "order_id"), "inner")),
                  Command("stats", (("sum", Ref("shipments", "qty"), "total"),
                                    ("count", None, "n"),
                                    ("count", Ref("shipments", "qty"), "n_qty"),
                                    ("count_distinct", Ref("orders", "id"), "orders"),
                                    ("avg", Ref("shipments", "qty"), "mean"),
                                    ("group", ((Ref("orders", "customer_id"), "customerId"),))))))
    executor = MemoryExecutor(FakeService(), pipeline, relations, {})
    result = executor.run()
    by_customer = {row["customerId"]: row for row in result.rows}
    assert Decimal(by_customer["1"]["total"]) == Decimal(7)
    assert by_customer["1"]["n"] == "2" and by_customer["1"]["n_qty"] == "2"
    assert by_customer["1"]["orders"] == "1"
    assert Decimal(by_customer["1"]["mean"]) == Decimal("3.5")
    assert by_customer["2"]["n"] == "1" and Decimal(by_customer["2"]["total"]) == Decimal(5)
    assert result.avg_semantics is not None and "avgSemantics" in result.plan
    # Parity: the persisted compiler produces the same aggregates on the same data.
    compiled = compile_query(Pipeline("orders", "orders", pipeline.commands),
                             {"orders": sources["my"]["orders"],
                              "shipments": sources["my"]["shipments"]}, {}, dialect="sqlite")
    sql_rows = sources["my_conn"].execute(compiled.statement).mappings().all()
    sql_map = {str(r["customerId"]): r for r in sql_rows}
    assert Decimal(sql_map["1"]["total"]) == Decimal(7)
    assert str(sql_map["1"]["orders"]) == "1"


def test_federated_classification_aggregate_only_when_safe(sources):
    shipments2 = replace(sources["my"]["shipments"], connection_key="my2")
    orders2 = replace(sources["my"]["orders"], connection_key="my3")
    agg = typed_pipeline("from orders as orders | join shipments as shipments on orders.id = shipments.order_id "
                         "| stats count() as n, sum(shipments.qty) as q, count_distinct(shipments.id) as d")
    result = MemoryExecutor(FakeService(), agg,
                            {"orders": sources["my"]["orders"], "shipments": shipments2}, {}).run()
    assert result.classification == "aggregate"
    assert result.rows[0] == {"n": "3", "q": "12", "d": "3"}
    with_min = typed_pipeline("from orders as orders | stats min(orders.id) as m")
    assert MemoryExecutor(FakeService(), with_min, {"orders": orders2}, {}).run().classification == "detail"


def test_federated_distinct_and_limit_offset(sources):
    relations = {"shipments": sources["my"]["shipments"]}
    pipeline = Pipeline("shipments", "shipments", (
        Command("fields", ((Ref("shipments", "order_id"), "order_id"),)),
        Command("distinct", ()),
        Command("sort", (("order_id", "desc", "last"),)),
        Command("limit", (1, 1)),
    ))
    result = MemoryExecutor(FakeService(), pipeline, relations, {}).run()
    assert result.rows == [{"order_id": "100"}]
    assert result.result_limited is True


def test_federated_budget_rejects_unbounded_side(sources):
    small = QueryLimits(federated_side_rows_limit=1, federated_pushdown_keys_limit=8192)
    relations = {"customers": sources["pg"]["customers"], "orders": sources["my"]["orders"]}
    pipeline = typed_pipeline("from customers as customers | join orders as orders on customers.id = orders.customer_id "
                              "| stats count() as n")
    executor = MemoryExecutor(FakeService(limits=small), pipeline, relations, {})
    with pytest.raises(QueryError) as exc:
        executor.run()
    assert exc.value.code == "query_budget_exceeded"


def test_federated_result_byte_budget(sources):
    small = QueryLimits(memory_result_bytes_limit=8)
    relations = {"customers": sources["pg"]["customers"]}
    pipeline = typed_pipeline("from customers as customers | fields customers.name, customers.region")
    executor = MemoryExecutor(FakeService(limits=small), pipeline, relations, {})
    with pytest.raises(QueryError) as exc:
        executor.run()
    assert exc.value.extra["budget"] == "memory_result_bytes_limit"


def test_federated_cancellation(sources):
    relations = {"customers": sources["pg"]["customers"], "orders": sources["my"]["orders"]}
    pipeline = typed_pipeline("from customers as customers | join orders as orders on customers.id = orders.customer_id "
                              "| fields customers.name")
    executor = MemoryExecutor(FakeService(), pipeline, relations, {},
                              cancel_check=lambda: True)
    with pytest.raises(QueryError) as exc:
        executor.run()
    assert (exc.value.status, exc.value.code) == (409, "query_cancelled")


def test_federated_pushes_key_filter_to_large_side(sources):
    relations = {"customers": sources["pg"]["customers"], "shipments": sources["my"]["shipments"]}
    pipeline = typed_pipeline("from shipments as shipments | join customers as customers on shipments.order_id = customers.id "
                              "| stats count() as n")
    executor = MemoryExecutor(FakeService(), pipeline, relations, {})
    result = executor.run()
    assert result.rows[0]["n"] == "0"  # no customer has id 100/101
    # Transfer metrics: the streamed side counts materialized rows, the key-
    # filtered side records chunked pulls with zero matched rows and bytes —
    # counts only, never key values or row data.
    assert result.plan["sourceRows"] == {"shipments": 3, "customers": 0}
    assert result.plan["keyedPull"]["customers"] == {"chunks": 1, "rows": 0, "encodedRowBytes": 0}
    # Key-filter path executes IN-list statements against the streamed side.
    executed = []

    def tracking_batches(statement, params, size):
        executed.append(params)
        yield from sources["my"]["shipments"].batches(statement, params, size)
    shipments = replace(sources["my"]["shipments"], batches=tracking_batches)
    pipeline2 = typed_pipeline("from customers as customers | join shipments as shipments on customers.id = shipments.order_id "
                               "| fields customers.name")
    result2 = MemoryExecutor(FakeService(), pipeline2,
                             {"customers": sources["pg"]["customers"], "shipments": shipments}, {}).run()
    assert result2.rows == []
    assert any("push_keys" in params for params in executed)
    assert result2.plan["sourceRows"] == {"customers": 3, "shipments": 0}
    assert result2.plan["keyedPull"]["shipments"] == {"chunks": 1, "rows": 0, "encodedRowBytes": 0}


def test_federated_param_where_pushdown_into_single_component(sources):
    relations = {"orders": sources["my"]["orders"], "shipments": sources["my"]["shipments"]}
    pipeline = typed_pipeline("from orders as orders | where orders.customer_id = :c "
                              "| join shipments as shipments on orders.id = shipments.order_id "
                              "| stats sum(shipments.qty) as q")
    result = MemoryExecutor(FakeService(), pipeline, relations, {"c": 2}).run()
    assert Decimal(result.rows[0]["q"]) == Decimal(5)


# -- proven group-key pushdown --------------------------------------------------
#
# The keyed pull must not force the source to re-aggregate the full derived
# input per chunk: when the pulled column is a group key of the derived GROUP
# BY, the chunk IN list commutes with the aggregation and is compiled below
# it, and a single LEFT join to a group-key aggregation is restricted to the
# join keys the surviving rows can reach. Every test compares against the
# exact pre-pushdown execution (above-group filter, no restriction).

def _tracking(relation, log):
    def batches(statement, params, size):
        log.append((str(statement), params))
        yield from relation.batches(statement, params, size)
    return replace(relation, batches=batches)


def test_group_key_pushdown_compiles_chunk_in_below_derived_group_by(sources):
    totals = DerivedSourceSpec(
        "totals", "orders",
        typed_pipeline("from orders as orders | where orders.total >= :floor "
                       "| stats count() as n, sum(orders.total) as s by orders.customer_id"),
        params={"floor": 0})
    base = {"orders": sources["my"]["orders"]}
    relations = resolve_derived(base, {"totals": totals})
    executed = []
    tracked = _tracking(relations["totals"], executed)
    limits = QueryLimits(federated_key_chunk_size=2)  # 3 keys -> 2 chunks
    service = FakeService(limits=limits)
    service.relations = dict(base)  # parents make the derived shape provable
    pipeline = typed_pipeline(
        "from customers as customers | join totals as totals on customers.id = totals.customer_id type=left "
        "| fields customers.name, totals.n, totals.s")
    pushed = MemoryExecutor(service, pipeline,
                            {"customers": sources["pg"]["customers"], "totals": tracked},
                            {}, derived_specs={"totals": totals}).run()
    # Same rows as the exact pre-pushdown execution (which the same executor
    # produces when the derived metadata is not available).
    reference_log = []
    reference = MemoryExecutor(FakeService(limits=limits), pipeline,
                               {"customers": sources["pg"]["customers"],
                                "totals": _tracking(relations["totals"], reference_log)}, {}).run()
    assert pushed.rows == reference.rows
    # Decimal sums exact and LEFT NULL honesty kept: customer 3 has no group.
    assert [(row["name"], row["n"], row["s"] is None or Decimal(row["s"])) for row in pushed.rows] == [
        ("acme", "2", Decimal("40")), ("globex", "1", Decimal("20")), ("initech", None, True)]
    pull_statements = [text for text, params in executed if "push_keys" in params]
    assert len(pull_statements) == 2  # one statement per chunk
    for text in pull_statements:
        # The chunk IN is compiled into the same SELECT as the derived GROUP BY
        # (below it), not as an outer filter above the aggregation.
        assert "GROUP BY" in text and text.index("IN (") < text.index("GROUP BY")
    chunks = [params["push_keys"] for _, params in executed if "push_keys" in params]
    assert sorted(chunks[0] + chunks[1]) == [1, 2, 3]
    # Metrics stay metadata-only: pushed key counts per alias, never key values.
    assert pushed.plan["groupKeyPushdown"] == {"totals": 3}
    assert pushed.plan["keyedPull"]["totals"]["chunks"] == 2
    assert reference.plan["groupKeyPushdown"] == {}


def test_group_key_pushdown_restricted_projection_keeps_anti_join_exact(sources):
    # Canonical pending shape: orders LEFT (shipments grouped by order_id),
    # unshipped test on the joined key, then GROUP BY the base side's key.
    shipped = DerivedSourceSpec(
        "shipped", "shipments",
        typed_pipeline("from shipments as shipments | stats count() as rows by shipments.order_id"),
        params={})
    pending = DerivedSourceSpec(
        "pending", "orders",
        typed_pipeline("from orders as orders | join shipped as shipped on orders.id = shipped.order_id type=left "
                       "| where shipped.order_id is null "
                       "| stats count() as pending_orders by orders.customer_id"),
        params={})
    base = {"orders": sources["my"]["orders"], "shipments": sources["my"]["shipments"]}
    relations = resolve_derived(base, {"shipped": shipped, "pending": pending})
    executed = []
    tracked = _tracking(relations["pending"], executed)
    service = FakeService()
    service.relations = dict(relations)
    pipeline = typed_pipeline(
        "from customers as customers | join pending as pending on customers.id = pending.customer_id type=left "
        "| fields customers.name, pending.pending_orders")
    pushed = MemoryExecutor(service, pipeline,
                            {"customers": sources["pg"]["customers"], "pending": tracked},
                            {}, derived_specs={"shipped": shipped, "pending": pending}).run()
    reference_log = []
    reference = MemoryExecutor(FakeService(), pipeline,
                               {"customers": sources["pg"]["customers"],
                                "pending": _tracking(relations["pending"], reference_log)}, {}).run()
    # order 100 ships twice (duplicate shipment rows must not fan out the one
    # row per grouped key), order 101 is shipped, order 102 is unshipped:
    # customer 1 counts one pending order; customers 2 and 3 get honest NULLs.
    assert pushed.rows == reference.rows == [
        {"name": "acme", "pending_orders": "1"},
        {"name": "globex", "pending_orders": None},
        {"name": "initech", "pending_orders": None}]
    pulls = [params for _, params in executed if "push_keys" in params]
    assert all(params["semi_keys"] == params["push_keys"] for params in pulls)
    # The grouped projection itself is restricted to reachable order ids: the
    # executed statement carries a second IN inside the grouped subquery.
    assert all(text.count("IN (") >= 2 for text, params in executed if "push_keys" in params)
    assert pushed.plan["groupKeyPushdown"] == {"pending": 3}


def test_group_key_pushdown_declines_unproven_shapes(sources):
    # LIMIT after the derived GROUP BY: filtering above and below the limit are
    # different relations, so the pull must keep the outer filter.
    limited = DerivedSourceSpec(
        "limited", "orders",
        typed_pipeline("from orders as orders | stats count() as n by orders.customer_id | limit 2"),
        params={})
    # The pulled column is a measure output, not a group key.
    measures = DerivedSourceSpec(
        "measures", "orders",
        typed_pipeline("from orders as orders | stats count() as n by orders.customer_id"),
        params={})
    base = {"orders": sources["my"]["orders"]}
    relations = resolve_derived(base, {"limited": limited, "measures": measures})
    service = FakeService()
    service.relations = dict(relations)
    specs = {"limited": limited, "measures": measures}
    for alias, spec, join in (("limited", limited, "customers.id = limited.customer_id"),
                              ("measures", measures, "customers.id = measures.n")):
        pipeline = typed_pipeline(
            f"from customers as customers | join {alias} as {alias} on {join} type=left "
            f"| fields customers.name, {alias}.n")
        pushed = MemoryExecutor(service, pipeline,
                                {"customers": sources["pg"]["customers"], alias: relations[alias]},
                                {}, derived_specs=specs).run()
        reference = MemoryExecutor(FakeService(), pipeline,
                                   {"customers": sources["pg"]["customers"], alias: relations[alias]}, {}).run()
        assert pushed.rows == reference.rows
        assert pushed.plan["groupKeyPushdown"] == {}


def test_group_key_pushdown_keeps_budgets_and_cancellation(sources):
    totals = DerivedSourceSpec(
        "totals", "orders",
        typed_pipeline("from orders as orders | stats count() as n by orders.customer_id"),
        params={})
    base = {"orders": sources["my"]["orders"]}
    relations = resolve_derived(base, {"totals": totals})
    service = FakeService()
    service.relations = dict(base)
    pipeline = typed_pipeline(
        "from customers as customers | join totals as totals on customers.id = totals.customer_id type=left "
        "| fields customers.name, totals.n")
    starved = FakeService(limits=QueryLimits(federated_side_bytes_limit=8))
    starved.relations = dict(base)
    with pytest.raises(QueryError) as exc:
        MemoryExecutor(starved, pipeline,
                       {"customers": sources["pg"]["customers"], "totals": relations["totals"]},
                       {}, derived_specs={"totals": totals}).run()
    assert exc.value.extra["budget"] == "federated_side_bytes_limit"
    with pytest.raises(QueryError) as exc:
        MemoryExecutor(service, pipeline,
                       {"customers": sources["pg"]["customers"], "totals": relations["totals"]},
                       {}, derived_specs={"totals": totals},
                       cancel_check=lambda: True).run()
    assert (exc.value.status, exc.value.code) == (409, "query_cancelled")


def test_group_key_pushdown_bounds_source_scan_per_chunk():
    """Realistic unit-scale cost check (SQLite VM instruction counts).

    The remote fixture's pending shape in miniature: a keyed pull over a
    derived pipeline that LEFT joins a grouped shipment projection and
    aggregates by customer on a second connection, with the fixture's
    customer/order indexes present. Without the pushdown every chunk
    statement re-aggregates the full shipment input and re-joins every order;
    with it, the chunk IN sits below the GROUP BY and the grouped projection
    is restricted to reachable order ids. Both plans must return identical
    rows while the pushed plan does a fraction of the source work.
    """
    customers, per_customer = 10_000, 20
    orders = customers * per_customer
    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    cust = sa.Table("cust", meta, sa.Column("id", sa.BigInteger, primary_key=True))
    ord_t = sa.Table("ord", meta, sa.Column("id", sa.BigInteger, primary_key=True),
                     sa.Column("customer_id", sa.BigInteger))
    ship = sa.Table("ship", meta, sa.Column("sid", sa.BigInteger, primary_key=True),
                    sa.Column("order_id", sa.BigInteger))
    sa.Index("idx_ord_customer", ord_t.c.customer_id)
    sa.Index("idx_ship_order", ship.c.order_id)
    meta.create_all(engine)
    with engine.begin() as conn:
        # Every third order is unshipped (no shipment rows); every ninth has
        # a second shipment row (duplicate multiplicity per order).
        ships, sid = [], 1
        for o in range(1, orders + 1):
            if o % 3:
                ships.append({"sid": sid, "order_id": o})
                sid += 1
                if o % 9 == 0:
                    ships.append({"sid": sid, "order_id": o})
                    sid += 1
        conn.execute(ord_t.insert(), [{"id": o, "customer_id": (o - 1) // per_customer + 1}
                                       for o in range(1, orders + 1)])
        conn.execute(ship.insert(), ships)
        # Planner statistics, like any production source database maintains.
        conn.exec_driver_sql("ANALYZE")

    def batches_for(conn):
        def batches(statement, params, batch_size):
            result = conn.execute(statement, params).mappings()
            found = [dict(row) for row in result]
            result.close()
            yield found
        return batches

    shipped = DerivedSourceSpec(
        "shipped", "ord_ship",
        typed_pipeline("from ord_ship as ord_ship | stats count() as rows by ord_ship.order_id"),
        params={})
    pending = DerivedSourceSpec(
        "pending", "ord_rel",
        typed_pipeline("from ord_rel as ord_rel | join shipped as shipped on ord_rel.id = shipped.order_id type=left "
                       "| where shipped.order_id is null "
                       "| stats count() as pending_orders by ord_rel.customer_id"),
        params={})
    outer = typed_pipeline(
        "from cust_rel as cust_rel | join pending as pending on cust_rel.id = pending.customer_id type=left "
        "| fields cust_rel.id, pending.pending_orders | sort cust_rel.id asc")

    def run(pushed: bool, steps: list, executed: list):
        cust_engine = sa.create_engine("sqlite://")
        cust_table = sa.Table("cust", sa.MetaData(), sa.Column("id", sa.BigInteger))
        cust_table.create(cust_engine)
        with cust_engine.begin() as c:
            c.execute(cust_table.insert(), [{"id": i} for i in range(1, customers + 1)])
        with engine.connect() as source_conn, cust_engine.connect() as cust_conn:
            driver = source_conn.connection.driver_connection
            driver.set_progress_handler(lambda: steps.append(1) or 0, 1)
            base = {
                "ord_rel": Relation("ord_rel", ord_t,
                                    {"id": ColumnSpec("id", "integer"),
                                     "customer_id": ColumnSpec("customer_id", "integer")},
                                    {"completeness": "complete", "consistency": "repeatable_read",
                                     "dialect": "sqlite"}, "auth:x", "schema:x", "x",
                                    batches=batches_for(source_conn)),
                "ord_ship": Relation("ord_ship", ship,
                                     {"sid": ColumnSpec("sid", "integer"),
                                      "order_id": ColumnSpec("order_id", "integer")},
                                     {"completeness": "complete", "consistency": "repeatable_read",
                                      "dialect": "sqlite"}, "auth:x", "schema:x2", "x",
                                     batches=batches_for(source_conn)),
            }
            relations = resolve_derived(base, {"shipped": shipped, "pending": pending})
            cust_rel = Relation("cust_rel", cust_table, {"id": ColumnSpec("id", "integer")},
                                {"completeness": "complete", "consistency": "repeatable_read",
                                 "dialect": "sqlite"}, "auth:y", "schema:y", "y",
                                batches=batches_for(cust_conn))
            tracked = _tracking(relations["pending"], executed)
            service = FakeService()
            if pushed:
                # Production services expose the full resolved map (leaves and
                # every derived alias, name-keyed) to the executor.
                service.relations = dict(relations)
            result = MemoryExecutor(service, outer,
                                    {"cust_rel": cust_rel, "pending": tracked},
                                    {}, derived_specs={"shipped": shipped, "pending": pending}).run()
            driver.set_progress_handler(None, 0)
        cust_engine.dispose()
        return result

    pushed_steps: list = []
    pushed_log = []
    pushed = run(True, pushed_steps, pushed_log)
    reference_steps: list = []
    reference = run(False, reference_steps, [])
    # Identical rows: every customer has pending orders (unshipped orders are
    # those with id % 3 == 0), counts exact under duplicate shipment rows.
    assert pushed.rows == reference.rows and len(pushed.rows) == customers
    for row in pushed.rows:
        first = (int(row["id"]) - 1) * per_customer + 1
        unshipped = len([o for o in range(first, first + per_customer) if o % 3 == 0])
        assert row["pending_orders"] == str(unshipped)
    assert pushed.plan["groupKeyPushdown"] == {"pending": customers}
    assert pushed.plan["keyedPull"]["pending"]["chunks"] == customers // 1000
    pulls = [params for _, params in pushed_log if "push_keys" in params]
    assert all(params["semi_keys"] == params["push_keys"] for params in pulls)
    # The pushed chunks aggregate only their own key range (index-bounded
    # below the GROUP BY); the reference re-aggregates the full input per chunk.
    assert len(reference_steps) > 3 * len(pushed_steps) > 0


# -- join constraints -----------------------------------------------------------

def test_join_constraint_cardinality_enforced(sources):
    relations = {"customers": sources["pg"]["customers"], "orders": sources["my"]["orders"]}
    pipeline = typed_pipeline("from customers as customers | join orders as orders on customers.id = orders.customer_id "
                              "| stats count() as n")
    # 1:N only checks the left key (customers.id is unique): accepted.
    MemoryExecutor(FakeService(), pipeline, relations, {}).run(
        {"orders": JoinConstraint(cardinality="1:N", key_type="number")})
    # N:1 requires the right key unique; orders.customer_id repeats: rejected.
    with pytest.raises(QueryError) as exc:
        MemoryExecutor(FakeService(), pipeline, relations, {}).run(
            {"orders": JoinConstraint(cardinality="N:1", key_type="integer")})
    assert exc.value.code == "join_cardinality_violated"
    # Declared key type is validated before any uniqueness query runs.
    with pytest.raises(QueryError) as exc:
        MemoryExecutor(FakeService(), pipeline, relations, {}).run(
            {"orders": JoinConstraint(cardinality="N:1", key_type="string")})
    assert exc.value.code == "join_key_type_mismatch"
    # 1:1 also checks the right side: rejected; N:M checks nothing.
    with pytest.raises(QueryError):
        MemoryExecutor(FakeService(), pipeline, relations, {}).run(
            {"orders": JoinConstraint(cardinality="1:1", key_type="integer")})
    MemoryExecutor(FakeService(), pipeline, relations, {}).run(
        {"orders": JoinConstraint(cardinality="N:M")})
    with pytest.raises(QueryError) as exc:
        MemoryExecutor(FakeService(), pipeline, relations, {}).run(
            {"nope": JoinConstraint(cardinality="N:M")})
    assert exc.value.code == "invalid_join_constraint"


# -- typed serialization & predicates ----------------------------------------------

def test_pipeline_round_trip_and_validation():
    original = parse("from orders as o | where o.id >= :min and (o.note = \"x\" or o.id in (1, 2.5)) "
                     "| join c as c on o.id = c.id type=left | stats count() as n by o.note "
                     "| sort n desc nulls first | limit 5 offset 2")
    rebuilt = pipeline_from_dict(pipeline_to_dict(original))
    assert rebuilt == original
    typed = Pipeline("orders", "orders", (
        Command("fields", ((Ref("orders", "note"), "note"),)),
        Command("distinct", ()),
        Command("sort", (("note", "asc", "last"),)),
    ))
    assert pipeline_from_dict(pipeline_to_dict(typed)) == typed
    contains = Expr("contains", (Ref("orders", "note"), Value("a%b_")))
    serialized = pipeline_from_dict(pipeline_to_dict(Pipeline("orders", "orders", (
        Command("where", (contains,)),))))
    assert serialized.commands[0].args[0].args[1].value == "a%b_"
    with pytest.raises(QueryError):
        validate_pipeline(Pipeline("orders", "bad alias!", ()))


def test_contains_and_distinct_compile_with_literal_substring_semantics(sources):
    relations = {"customers": sources["pg"]["customers"]}
    pipeline = Pipeline("customers", "customers", (
        Command("where", (Expr("contains", (Ref("customers", "name"), Value("%"))),)),
        Command("fields", ((Ref("customers", "name"), "name"),)),
        Command("distinct", ()),
    ))
    compiled = compile_query(pipeline, relations, {}, dialect="sqlite")
    rows = sources["pg_conn"].execute(compiled.statement, compiled.params).mappings().all()
    # Names have no literal % substring; LIKE metacharacters must not match.
    assert rows == []
    # And a real substring match stays exact.
    pipeline_hit = Pipeline("customers", "customers", (
        Command("where", (Expr("contains", (Ref("customers", "name"), Value("cm"))),)),
        Command("fields", ((Ref("customers", "name"), "name"),)),
    ))
    compiled_hit = compile_query(pipeline_hit, relations, {}, dialect="sqlite")
    rows_hit = sources["pg_conn"].execute(compiled_hit.statement).scalars().all()
    assert rows_hit == ["acme"]
    memory = MemoryExecutor(FakeService(), pipeline, relations, {}).run()
    assert memory.rows == []


def test_compile_predicate_public_helper(sources):
    relations = {"customers": sources["pg"]["customers"]}
    clause, params = compile_predicate(Expr("=", (Ref("customers", "id"), Value("region", True))),
                                       relations, {"region": 2}, dialect="sqlite")
    rows = sources["pg_conn"].execute(sa.select(relations["customers"].table.c.name).where(clause),
                                       params).scalars().all()
    assert rows == ["globex"]


# -- memory handles & service flow ------------------------------------------------

def _fake_service_for_memory(relations, limits=None):
    service = QueryService(SimpleNamespace(in_transaction=lambda: False, rollback=lambda: None,
                                           get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))),
                           SimpleNamespace(id="owner-1"), secret="test", limits=limits)
    service.relations = relations
    service._resolve = lambda plan, **_: None
    return service


def test_execute_typed_memory_end_to_end_pages_origin_and_cancel(sources):
    erp_agg = DerivedSourceSpec(
        alias="erp_agg", source="orders",
        pipeline=typed_pipeline("from orders as orders | join shipments as shipments on orders.id = shipments.order_id "
                                "| stats sum(shipments.qty) as qty by orders.customer_id"),
        params={})
    crm_agg = DerivedSourceSpec(
        alias="crm_agg", source="customers",
        pipeline=typed_pipeline("from customers as customers | join contracts as contracts on customers.id = contracts.customer_id "
                                "| stats sum(contracts.amount) as contracted by customers.id, customers.region"),
        params={})
    relations = resolve_derived(
        {"orders": sources["my"]["orders"], "shipments": sources["my"]["shipments"],
         "customers": sources["pg"]["customers"], "contracts": sources["pg"]["contracts"]},
        {"erp_agg": erp_agg, "crm_agg": crm_agg})
    outer = typed_pipeline(
        "from crm_agg as crm_agg | join erp_agg as erp_agg on crm_agg.id = erp_agg.customer_id "
        "| stats sum(erp_agg.qty) as shipped by crm_agg.region")
    request = TypedQueryRequest(
        pipeline=outer,
        bindings={"orders": Binding(kind="integrated", asset_id="asset-orders"),
                  "shipments": Binding(kind="integrated", asset_id="asset-shipments"),
                  "customers": Binding(kind="integrated", asset_id="asset-customers"),
                  "contracts": Binding(kind="integrated", asset_id="asset-contracts")},
        derived={"erp_agg": erp_agg, "crm_agg": crm_agg},
        join_constraints={"erp_agg": JoinConstraint(cardinality="N:1", key_type="integer")},
        params={}, page_size=1, result_storage="memory",
        origin={"request": {"semantic": True}, "expectations": {}},
        session_kind="ontology", session_id="session-1")
    service = _fake_service_for_memory(relations)
    service._resolve = lambda plan, **_: setattr(service, "relations", relations)
    response = service.execute_typed(request)
    # A group-by key is a source value: the trusted classification is detail.
    assert response["persistence"] == {"mode": "ephemeral", "classification": "detail"}
    # QueryService passes the derived specs through: the erp keyed pull
    # compiled its chunk keys below the derived GROUP BY (counts only).
    assert response["plan"]["groupKeyPushdown"] == {"erp_agg": 2}
    assert [(row["region"], Decimal(row["shipped"])) for row in response["rows"]] == [("north", Decimal("7"))]
    assert response["totalRows"] == 1 and response["resultLimited"] is False
    assert response["page"]["cursor"] is None
    handle = REGISTRY.handles[response["queryId"]]
    cursor = service._sign({"v": 1, "mode": "memory", "inst": INSTANCE_ID,
                            "id": handle.id, "owner": handle.owner_user_id,
                            "q": handle.query_hash, "next": 0, "size": handle.page_size,
                            "exp": handle.expires_at.timestamp()}, memory=True)
    # memory_origin exposes the bridge origin without paging.
    origin_info = service.memory_origin(cursor)
    assert origin_info["origin"]["request"] == {"semantic": True}
    assert origin_info["session"] == {"kind": "ontology", "id": "session-1"}
    # fetch pages from the handle with session binding enforced.
    from app.data_channel.query.contracts import TextQueryCursorRequest
    again = service.fetch(TextQueryCursorRequest(cursor=cursor),
                          session_kind="ontology", session_id="session-1")
    assert again["rows"] == response["rows"]
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=cursor), session_kind="ontology", session_id="other")
    assert exc.value.code == "cursor_session_mismatch"
    # Anonymous router fetch cannot continue a session-bound handle.
    with pytest.raises(QueryError):
        service.fetch(TextQueryCursorRequest(cursor=cursor))
    # Cancellation removes the handle; the cursor then expires with 410.
    assert service.cancel(origin_info["queryId"]) == {"queryId": origin_info["queryId"], "cancelled": True}
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=cursor), session_kind="ontology", session_id="session-1")
    assert (exc.value.status, exc.value.code) == (410, "result_handle_expired")


def test_memory_fetch_foreign_instance_is_410_never_reexecuted(sources):
    relations = {"customers": sources["pg"]["customers"]}
    request = TextQueryRequest(language="spl", query="from customers as customers | fields customers.name",
                               bindings={"customers": {"kind": "integrated", "assetId": "a1"}},
                               result_storage="memory", page_size=10)
    service = _fake_service_for_memory(relations)
    service._resolve = lambda plan, **_: setattr(service, "relations", relations)
    response = service.execute(request)
    assert response["persistence"]["classification"] == "detail"
    foreign = service._sign({"v": 1, "mode": "memory", "inst": "other-process", "id": "missing",
                            "owner": "owner-1", "q": response["plan"] and "x", "next": 0, "size": 10,
                            "exp": datetime.now(timezone.utc).timestamp() + 60}, memory=True)
    from app.data_channel.query.contracts import TextQueryCursorRequest
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=foreign))
    assert (exc.value.status, exc.value.code) == (410, "result_handle_expired")
    # Tampered mode prefix on a persisted-style token is rejected.
    with pytest.raises(QueryError):
        service._decode("mem." + service._sign({"v": 1, "id": "x", "owner": "owner-1", "next": 0})[4:])


def test_memory_handle_ttl_expiry(sources):
    relations = {"customers": sources["pg"]["customers"]}
    service = _fake_service_for_memory(relations, limits=QueryLimits(memory_handle_ttl_seconds=1))
    service._resolve = lambda plan, **_: setattr(service, "relations", relations)
    request = TextQueryRequest(language="spl", query="from customers as customers | fields customers.id",
                               bindings={"customers": {"kind": "integrated", "assetId": "a1"}},
                               result_storage="memory", page_size=2)
    response = service.execute(request)
    assert response["page"]["hasMore"] is True and response["page"]["cursor"]
    handle = REGISTRY.handles[response["queryId"]]
    handle.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    from app.data_channel.query.contracts import TextQueryCursorRequest
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=response["page"]["cursor"]))
    assert exc.value.code == "result_handle_expired"
    assert REGISTRY.get(response["queryId"]) is None


def test_registry_quotas_global_user_and_reservation():
    from app.data_channel.query.handles import MemoryHandle
    limits = QueryLimits(memory_total_bytes_limit=120, memory_per_user_bytes_limit=120,
                         memory_max_handles_per_user=4,
                         federated_side_bytes_limit=10, memory_result_bytes_limit=10)
    reservation = limits.federated_side_bytes_limit + limits.memory_result_bytes_limit

    def handle(id, owner, size):
        return MemoryHandle(id=id, owner_user_id=owner, instance_id=INSTANCE_ID, columns=[],
                            rows=[], total_rows=0, result_limited=False, page_size=10,
                            created_at=datetime.now(timezone.utc),
                            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                            auth_fingerprints={}, schema_fingerprints={}, query_hash=id,
                            request_fingerprint=None, request_kind="typed", stored_request={},
                            source_states=[], consistency="x", plan={}, classification="detail",
                            avg_semantics=None, bytes=size)
    REGISTRY.register(handle("h1", "u1", 50), limits)
    with pytest.raises(QueryError) as exc:
        REGISTRY.register(handle("h2", "u1", 80), limits)
    assert exc.value.extra["quota"] == "memory_per_user_bytes_limit"
    REGISTRY.register(handle("h3", "u2", 50), limits)
    with pytest.raises(QueryError) as exc:
        REGISTRY.register(handle("h4", "u2", 21), limits)
    assert exc.value.extra["quota"] == "memory_total_bytes_limit"
    # Executing queries reserve their worst-case memory globally AND per user;
    # the register window keeps other users' reservations counted.
    REGISTRY.open_ticket("t1", "u3", limits)
    with pytest.raises(QueryError) as exc:
        REGISTRY.open_ticket("t2", "u3", limits)
    assert exc.value.extra["quota"] == "memory_total_bytes_limit"
    with pytest.raises(QueryError) as exc:
        REGISTRY.open_ticket("t2", "u2", limits)
    assert exc.value.extra["quota"] == "memory_total_bytes_limit"
    REGISTRY.close_ticket("t1")
    per_user_fill = QueryLimits(memory_total_bytes_limit=10_000, memory_per_user_bytes_limit=70,
                                federated_side_bytes_limit=10, memory_result_bytes_limit=10)
    REGISTRY.handles.clear()
    REGISTRY.register(handle("hp", "u9", 55), per_user_fill)
    with pytest.raises(QueryError) as exc:
        REGISTRY.open_ticket("tp", "u9", per_user_fill)
    assert exc.value.extra["quota"] == "memory_per_user_bytes_limit"
    REGISTRY.handles.clear()
    REGISTRY.register(handle("h1", "u1", 50), limits)
    REGISTRY.register(handle("h3", "u2", 50), limits)
    ticket = REGISTRY.open_ticket("t1", "u3", limits)
    with pytest.raises(QueryError) as exc:
        REGISTRY.register(handle("hz", "u2", 1), limits)
    assert exc.value.extra["quota"] == "memory_total_bytes_limit"
    assert ticket.cancelled is False
    assert REGISTRY.cancel("t1", "u3") is True
    assert REGISTRY.cancel("t1", "u4") is False
    with pytest.raises(QueryError):
        REGISTRY.register(handle("t1", "u3", 1), limits)
    REGISTRY.close_ticket("t1")
    REGISTRY.handles.pop("h1", None)
    REGISTRY.register(handle("hz", "u2", 50), limits)
    assert REGISTRY.cancel("hz", "u2") is True and REGISTRY.get("hz") is None


def test_execute_typed_rejects_bad_shapes(sources):
    with pytest.raises(QueryError):
        QueryService(SimpleNamespace(in_transaction=lambda: False), SimpleNamespace(id="u"),
                     secret="s").execute_typed("not typed")
    service = QueryService(SimpleNamespace(in_transaction=lambda: False), SimpleNamespace(id="u"), secret="s")
    request = TypedQueryRequest(
        pipeline=typed_pipeline("from orders as orders | fields orders.id"),
        bindings={"orders": Binding(kind="integrated", asset_id="a")},
        params={"x": "unused"}, result_storage="memory")
    with pytest.raises(QueryError) as exc:
        service.execute_typed(request)
    assert exc.value.code == "unused_parameter"
    with pytest.raises(QueryError):
        service.execute_typed(TypedQueryRequest(
            pipeline=typed_pipeline("from orders as orders | fields orders.id"),
            bindings={"orders": Binding(kind="integrated", asset_id="a")},
            result_storage="memory", page_size=True))
    # request_fingerprint is opaque upstream binding: any string is accepted.
    opaque = TypedQueryRequest(
        pipeline=typed_pipeline("from orders as orders | fields orders.id"),
        bindings={"orders": Binding(kind="integrated", asset_id="a")},
        result_storage="memory", request_fingerprint="deadbeef")
    assert service._plan_from_typed(opaque).request_fingerprint == "deadbeef"


def test_exact_decimal_accumulation_beyond_default_precision(sources):
    from decimal import Decimal as D
    exact_rows = [
        (D("1234567890123456789012345678901234567890.1234567890"), 100),
        (D("0.0000000001"), 101),
        (D("9999999999999999999999999999999999999999.9999999999"), 102),
    ]

    def batches(statement, params, batch_size):
        # Stand-in physical source that returns exact decimals (SQLite cannot
        # store this precision); the executor under test is unchanged.
        labels = list(statement.selected_columns.keys())
        if labels == ["n"]:
            yield [{"n": len(exact_rows)}]
            return
        yield [dict(zip(labels, row)) for row in exact_rows]

    wide = sa.table("wide_amounts", sa.column("id", sa.BigInteger), sa.column("amount", sa.Numeric(50, 10)))
    relation = Relation("wide", wide, {"id": ColumnSpec("id", "integer"),
                                       "amount": ColumnSpec("amount", "decimal", precision=50, scale=10)},
                        {"completeness": "complete", "consistency": "repeatable_read", "dialect": "sqlite"},
                        "auth:wide", "schema:wide", "wide-src", batches=batches)
    orders2 = replace(sources["my"]["orders"], connection_key="other")
    # Split connections force in-memory aggregation; sum must stay exact.
    relations = {"wide": relation, "orders": orders2}
    pipeline = Pipeline("wide", "wide", (
        Command("join", ("orders", "orders", Ref("wide", "id"), Ref("orders", "id"), "inner")),
        Command("stats", (("sum", Ref("wide", "amount"), "total"),
                          ("count_distinct", Ref("wide", "amount"), "d"),
                          ("group", ()))),
    ))
    result = MemoryExecutor(FakeService(), pipeline, relations, {}).run()
    # Exact 51-significant-digit sum: no 28-digit-context rounding anywhere.
    assert result.rows[0]["total"] == "11234567890123456789012345678901234567890.1234567890"
    # 40+ digit distinct values never merge through hashing/normalization.
    assert result.rows[0]["d"] == "3"


def test_single_source_stream_is_bounded_before_full_materialization(sources):
    tiny = QueryLimits(memory_result_bytes_limit=16)
    relations = {"customers": sources["pg"]["customers"]}
    pipeline = typed_pipeline("from customers as customers | fields customers.name, customers.region")
    with pytest.raises(QueryError) as exc:
        MemoryExecutor(FakeService(limits=tiny), pipeline, relations, {}).run()
    assert exc.value.extra["budget"] == "memory_result_bytes_limit"
    row_capped = QueryLimits(federated_join_rows_limit=2)
    with pytest.raises(QueryError) as exc:
        MemoryExecutor(FakeService(limits=row_capped), pipeline, relations, {}).run()
    assert exc.value.extra["budget"] == "federated_join_rows_limit"
    # With LIMIT the count pre-check bounds materialization and reports totals.
    limited = typed_pipeline("from customers as customers | fields customers.id | limit 2 offset 1")
    result = MemoryExecutor(FakeService(), limited, relations, {}).run()
    assert [row["id"] for row in result.rows] == ["2", "3"]
    assert result.result_limited is True


def test_nested_derived_join_constraints_enforced(sources):
    # A derived spec joins customers->contracts internally; the declared N:1
    # constraint on that inner join must be validated against the derived scope.
    inner = DerivedSourceSpec(
        alias="crm", source="customers",
        pipeline=typed_pipeline("from customers as customers | join contracts as contracts "
                                "on customers.id = contracts.customer_id | fields customers.id, customers.region"),
        params={})
    relations = resolve_derived({"customers": sources["pg"]["customers"],
                                 "contracts": sources["pg"]["contracts"]}, {"crm": inner})
    outer = typed_pipeline("from crm as crm | fields crm.id")
    service = FakeService()
    executor = MemoryExecutor(service, outer, relations, {})
    executor.run()  # no constraints: fine
    service2 = FakeService()
    executor2 = MemoryExecutor(service2, outer, relations, {})
    # contracts.customer_id repeats (customer 1 has two contracts): N:1 fails.
    from app.data_channel.query.service import _Plan
    plan = SimpleNamespace(pipeline=outer, derived={"crm": inner},
                           join_constraints={"contracts": JoinConstraint(cardinality="N:1", key_type="integer")})
    full = QueryService(SimpleNamespace(in_transaction=lambda: False, rollback=lambda: None,
                                        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))),
                        SimpleNamespace(id="u"), secret="s")
    full.relations = relations
    with pytest.raises(QueryError) as exc:
        full._validate_derived_constraints(plan)
    assert exc.value.code == "join_cardinality_violated"
    with pytest.raises(QueryError) as exc:
        full._validate_derived_constraints(SimpleNamespace(
            pipeline=outer, derived={"crm": inner},
            join_constraints={"nonexistent": JoinConstraint(cardinality="N:M")}))
    assert exc.value.code == "invalid_join_constraint"


def test_adapter_semantic_plan_vertical_through_execute_typed(sources, monkeypatch):
    """Real adapter build_typed_request -> execute_typed -> adapter.fetch."""
    from app.data_channel.query import providers as providers_module
    from app.data_channel.query.ontology_adapter import OntologyQueryAdapter, build_typed_request

    by_asset = {"asset-customers": sources["pg"]["customers"],
                "asset-orders": sources["my"]["orders"]}

    def fake_resolve_integrated(db, user, bindings, limits, stack, **_):
        return {alias: by_asset[binding.asset_id] for alias, binding in bindings}

    monkeypatch.setattr(providers_module, "resolve_integrated", fake_resolve_integrated)
    plan = {
        "pageSize": 10,
        "releases": [],
        "citations": [],
        "sources": [
            {"alias": "cust", "binding": {"kind": "integrated", "assetId": "asset-customers"},
             "schemaFingerprint": by_asset["asset-customers"].schema_fingerprint,
             "declaredTypes": {"id": "number", "region": "string"},
             "propertyMap": {"id": "id", "region": "region"}},
            {"alias": "orders", "binding": {"kind": "integrated", "assetId": "asset-orders"},
             "schemaFingerprint": by_asset["asset-orders"].schema_fingerprint,
             "declaredTypes": {"customerId": "number", "amount": "number"},
             "propertyMap": {"customerId": "customer_id", "amount": "total"}},
        ],
        "sourceFilters": [{"source": "orders", "filter": {
            "source": "orders", "property": "customerId", "op": "gte", "value": 1}}],
        "joins": [{"left": "cust", "right": "orders", "leftProperty": "id",
                   "rightProperty": "customerId", "type": "inner",
                   "cardinality": "1:N", "keyType": "number"}],
        "where": [],
        "groupBy": [{"source": "cust", "property": "region"}],
        "aggregates": [{"fn": "count", "as": "n"},
                       {"fn": "sum", "source": "orders", "property": "amount", "as": "amount"}],
        "select": [{"source": "cust", "property": "region", "as": "region"}],
        "distinct": False,
        "orderBy": [{"output": "amount", "direction": "desc", "nulls": "last"}],
    }
    typed = build_typed_request(plan, request={"semantic": "request"},
                                session_kind="ontology", session_id="sess-1")
    db = SimpleNamespace(in_transaction=lambda: False, rollback=lambda: None,
                         get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    user = SimpleNamespace(id="bridge-user")
    adapter = OntologyQueryAdapter()
    result = adapter.execute(db, user, plan, request={"semantic": "request"},
                             session_kind="ontology", session_id="sess-1")
    assert result["persistence"]["classification"] == "detail"
    assert [(row["region"], row["n"], Decimal(row["amount"])) for row in result["rows"]] == [
        ("north", "2", Decimal("40")), ("south", "1", Decimal("20"))]
    assert result["hasNext"] is False
    # Adapter fetch path: memory_origin + prepare fingerprint + core fetch.
    page = adapter._service(db, user).execute_typed(replace(typed, page_size=1))
    assert page["persistence"]["classification"] == "detail"
    def prepare(db_, user_, request_, serialized=True):
        return None, plan
    token = page["page"]["cursor"]
    refetched = adapter.fetch(db, user, token, session_kind="ontology",
                              session_id="sess-1", prepare=prepare)
    assert refetched["totalRowCount"] == 2
    def failing_prepare(db_, user_, request_, serialized=True):
        return None, {**plan, "pageSize": 999}
    with pytest.raises(QueryError) as exc:
        adapter.fetch(db, user, token, session_kind="ontology",
                      session_id="sess-1", prepare=failing_prepare)
    assert exc.value.code == "query_context_changed"
    # Fingerprint drift cancels the published handle.
    assert REGISTRY.get(refetched["queryId"]) is None


def test_origin_join_constraints_used_when_field_empty(sources):
    relations = {"customers": sources["pg"]["customers"], "orders": sources["my"]["orders"]}
    request = TypedQueryRequest(
        pipeline=typed_pipeline("from customers as customers | join orders as orders on customers.id = orders.customer_id "
                                "| stats count() as n"),
        bindings={"customers": Binding(kind="integrated", asset_id="a"),
                  "orders": Binding(kind="integrated", asset_id="b")},
        result_storage="memory",
        origin={"joinConstraints": {"orders": {"cardinality": "1:1", "keyType": "uuid"}}})
    service = _fake_service_for_memory(relations)
    service._resolve = lambda plan, **_: setattr(service, "relations", relations)
    with pytest.raises(QueryError) as exc:
        service.execute_typed(request)
    assert exc.value.code == "join_key_type_mismatch"


def test_expectations_schema_drift_detected(sources):
    relations = {"customers": sources["pg"]["customers"]}
    service = _fake_service_for_memory(relations)

    def resolve(plan):
        service.relations = relations
    service._resolve = resolve
    request = TypedQueryRequest(
        pipeline=typed_pipeline("from customers as customers | fields customers.id"),
        bindings={"customers": Binding(kind="integrated", asset_id="a")},
        result_storage="memory",
        origin={"expectations": {"customers": {
            "schemaFingerprint": "stale-fingerprint",
            "propertyMap": {"customerId": "id"},
            "declaredTypes": {"customerId": "integer"}}}})
    with pytest.raises(QueryError) as exc:
        service.execute_typed(request)
    assert exc.value.code == "query_context_changed"
    good = TypedQueryRequest(
        pipeline=typed_pipeline("from customers as customers | fields customers.id"),
        bindings={"customers": Binding(kind="integrated", asset_id="a")},
        result_storage="memory",
        origin={"expectations": {"customers": {
            "schemaFingerprint": relations["customers"].schema_fingerprint,
            "propertyMap": {"customerId": "id", "region": "region"},
            "declaredTypes": {"customerId": "number", "region": "string"}}}})
    response = service.execute_typed(good)
    assert response["totalRows"] == 3
    # Semantic number never maps onto a native string column.
    bad_type = TypedQueryRequest(
        pipeline=typed_pipeline("from customers as customers | fields customers.id"),
        bindings={"customers": Binding(kind="integrated", asset_id="a")},
        result_storage="memory",
        origin={"expectations": {"customers": {
            "schemaFingerprint": relations["customers"].schema_fingerprint,
            "propertyMap": {"customerId": "region"},
            "declaredTypes": {"customerId": "number"}}}})
    with pytest.raises(QueryError) as exc:
        service.execute_typed(bad_type)
    assert exc.value.code == "query_context_changed"


@pytest.mark.parametrize(("predicate", "expected"), [
    ("not (orders.id = 100)", ["acme", "globex"]),
    ("not (orders.id = 100 or orders.id is null)", ["acme", "globex"]),
    ("not (orders.id = 100 and customers.id = 99)", ["acme", "acme", "globex", "initech"]),
    ("not (orders.id in (100, 102))", ["globex"]),
])
def test_federated_predicates_preserve_sql_unknown_under_not(sources, predicate, expected):
    pipeline = typed_pipeline(
        "from customers as customers | join orders as orders on customers.id = orders.customer_id type=left "
        f"| where {predicate} | fields customers.name")
    result = MemoryExecutor(FakeService(), pipeline, {
        "customers": sources["pg"]["customers"], "orders": sources["my"]["orders"]}, {}).run()
    assert sorted(row["name"] for row in result.rows) == expected


def test_nested_constraint_uses_shared_budget_and_cancel(sources):
    from app.data_channel.query.memory_budget import MemoryBudget

    inner = DerivedSourceSpec(
        alias="crm", source="customers",
        pipeline=typed_pipeline("from customers as customers | join contracts as contracts "
                                "on customers.id = contracts.customer_id | fields customers.id"))
    relations = resolve_derived({"customers": sources["pg"]["customers"],
                                 "contracts": sources["pg"]["contracts"]}, {"crm": inner})
    plan = SimpleNamespace(pipeline=typed_pipeline("from crm as crm | fields crm.id"),
                           derived={"crm": inner}, join_constraints={
                               "contracts": JoinConstraint(cardinality="1:N", key_type="integer")})
    service = _fake_service_for_memory(relations)
    with pytest.raises(QueryError) as exc:
        service._validate_derived_constraints(plan, memory_budget=MemoryBudget(1))
    assert exc.value.extra["budget"] == "memory_execution_bytes"
    budget = MemoryBudget(1024 * 1024)
    service._validate_derived_constraints(plan, memory_budget=budget)
    assert budget.charged > 0
    with pytest.raises(QueryError) as exc:
        service._validate_derived_constraints(plan, memory_budget=budget, cancel_check=lambda: True)
    assert exc.value.code == "query_cancelled"


def test_derived_rechecks_every_parent_and_fingerprints_nonprimary_changes(sources):
    seen = []
    parents = {name: replace(relation, recheck=lambda name=name: seen.append(name))
               for name, relation in sources["pg"].items()}
    spec = DerivedSourceSpec("crm", "customers", typed_pipeline(
        "from customers as customers | join contracts as contracts on customers.id = contracts.customer_id "
        "| fields customers.id"))
    relation = derived_relation("crm", parents, spec, bind_prefix="dv0")
    relation.recheck()
    assert sorted(seen) == ["contracts", "customers"]
    changed = derived_relation("crm", {**parents, "contracts": replace(parents["contracts"],
        auth_fingerprint="revoked", schema_fingerprint="changed")}, spec, bind_prefix="dv0")
    assert changed.auth_fingerprint != relation.auth_fingerprint
    assert changed.schema_fingerprint != relation.schema_fingerprint


def test_memory_fetch_does_not_reexecute_rows_or_current_cardinality(sources):
    from app.data_channel.query.contracts import TextQueryCursorRequest

    inner = DerivedSourceSpec("crm", "customers", typed_pipeline(
        "from customers as customers | join contracts as contracts on customers.id = contracts.customer_id "
        "| fields customers.id"))
    relations = resolve_derived(sources["pg"], {"crm": inner})
    service = _fake_service_for_memory(relations)
    request = TypedQueryRequest(
        pipeline=typed_pipeline("from crm as crm | fields crm.id"),
        bindings={name: Binding(kind="integrated", asset_id=name) for name in sources["pg"]},
        derived={"crm": inner}, join_constraints={"contracts": JoinConstraint("1:N", "integer")},
        page_size=1, result_storage="memory")
    first = service.execute_typed(request)
    assert first["page"]["hasMore"] is True

    def forbidden(*_):
        raise AssertionError("paging must not execute source row or cardinality statements")

    service.relations = {name: replace(relation, batches=forbidden) for name, relation in relations.items()}
    second = service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert second["queryId"] == first["queryId"] and second["totalRows"] == first["totalRows"]


def test_decimal_sum_bounds_exponent_gap_before_allocation_and_ignores_default_context():
    from decimal import localcontext
    from app.data_channel.query.federated import _final_state, _init_state, _update_state
    from app.data_channel.query.memory_budget import MemoryBudget

    budget = MemoryBudget(4096)
    state = _init_state()
    with localcontext() as context:
        context.prec = 3
        _update_state(state, "sum", Decimal("12345678901234567890.1234"), budget)
        _update_state(state, "sum", Decimal("0.0006"), budget)
    assert _final_state(state, "sum", False) == Decimal("12345678901234567890.1240")
    with pytest.raises(QueryError) as exc:
        _update_state(state, "sum", Decimal("1e1000000"), budget)
    assert exc.value.extra["budget"] == "memory_execution_bytes"


def test_execution_cancellation_holds_reservation_until_exit():
    from app.data_channel.query.handles import MemoryResultRegistry

    registry = MemoryResultRegistry()
    limits = QueryLimits(memory_total_bytes_limit=100, memory_per_user_bytes_limit=100,
                         federated_side_bytes_limit=50, memory_result_bytes_limit=50)
    registry.open_ticket("first", "owner", limits)
    assert registry.cancel("first", "owner") is True
    assert registry.ticket_cancelled("first") is True
    with pytest.raises(QueryError) as exc:
        registry.open_ticket("second", "other", limits)
    assert exc.value.extra["quota"] == "memory_total_bytes_limit"
    registry.close_ticket("first")
    assert not registry.ticket_cancelled("first") and not registry._cancelled_tickets
    registry.open_ticket("second", "other", limits)
    registry.close_ticket("second")


def test_page_failure_removes_published_handle_and_execution_ticket(sources, monkeypatch):
    service = _fake_service_for_memory({"customers": sources["pg"]["customers"]})
    closed = []
    service.stack.callback(lambda: closed.append(True))

    def broken_page(*_):
        raise QueryError(422, "query_budget_exceeded", "page encoding failed")

    monkeypatch.setattr(service, "_page_memory", broken_page)
    with pytest.raises(QueryError, match="page encoding failed"):
        service.execute_typed(TypedQueryRequest(
            pipeline=typed_pipeline("from customers as customers | fields customers.id"),
            bindings={"customers": Binding(kind="integrated", asset_id="customers")},
            result_storage="memory"))
    assert closed == [True]
    assert not REGISTRY.handles and not REGISTRY._tickets and not REGISTRY._cancelled_tickets

    import time
    from sqlalchemy.exc import DBAPIError, PendingRollbackError
    from app.data_channel.query import service as service_module
    from app.shared.query_control import QueryControl, query_control_scope
    source_error = DBAPIError("SELECT remote", {}, Exception(2013, "sensitive driver detail"), False)

    for expired, cancelled in ((False, False), (True, False), (False, True)):
        service = _fake_service_for_memory({"customers": sources["pg"]["customers"]})
        control = QueryControl()
        closed, rolled_back = [], []
        service.stack.callback(lambda: closed.append(True))

        @contextmanager
        def read_transaction():
            try:
                yield
            except QueryError as error:
                assert REGISTRY._tickets
                rolled_back.append(error.code)
                raise
            else:
                raise PendingRollbackError("invalid read transaction must not commit")

        service.stack.enter_context(read_transaction())

        def broken_execution(*_):
            if expired:
                service.started = time.monotonic() - 301
            if cancelled:
                control.cancel()
            raise source_error

        monkeypatch.setattr(service_module.MemoryExecutor, "run", broken_execution)
        with query_control_scope(control), pytest.raises(QueryError) as exc:
            service.execute_typed(TypedQueryRequest(
                pipeline=typed_pipeline("from customers as customers | fields customers.id"),
                bindings={"customers": Binding(kind="integrated", asset_id="customers")},
                result_storage="memory"))
        assert (exc.value.status, exc.value.code) == (
            (409, "query_cancelled") if cancelled else
            (422, "query_budget_exceeded") if expired else (503, "query_execution_failed"))
        if expired:
            assert exc.value.extra == {"budget": "statement_timeout_ms"}
        assert "sensitive" not in str(exc.value.detail())
        assert closed == [True]
        assert rolled_back == [exc.value.code]
        assert not REGISTRY.handles and not REGISTRY._tickets and not REGISTRY._cancelled_tickets


def test_eof_revocation_prevents_handle_publication(sources):
    def revoked():
        raise QueryError(403, "permission_denied", "source access revoked")

    service = _fake_service_for_memory({"customers": replace(sources["pg"]["customers"], recheck=revoked)})
    with pytest.raises(QueryError) as exc:
        service.execute_typed(TypedQueryRequest(
            pipeline=typed_pipeline("from customers as customers | fields customers.id"),
            bindings={"customers": Binding(kind="integrated", asset_id="customers")},
            result_storage="memory"))
    assert exc.value.status == 403
    assert not REGISTRY.handles and not REGISTRY._tickets


# -- remote pagination recheck_only contracts ------------------------------------
#
# Cursor paging over integrated (remote) sources must not reconnect to the
# source for data: persisted snapshots and memory handles already
# materialized their rows inside an execute-time read scope, so revalidation
# resolves platform metadata only (recheck_only=True), never reflects source
# schema and never reads source rows. It fails closed when the pinned
# fingerprints no longer match (credentials, schema, or lifecycle status via
# the auth digest), when the connection's explicit state is failed-test
# "error", and when a bounded SELECT 1 liveness probe (one per unique physical
# source per page fetch) cannot reach the source: physical outages are not
# reinterpreted as success.

@pytest.fixture
def remote_asset(db, admin_user):
    """Registered integrated asset + connection rows on the platform database.

    resolve_metadata stays real: the auth fingerprint includes the connection
    config revision, so credential rotation is observable without any source
    connection being opened.
    """
    from app.data_channel.connections.models import Connection
    from app.data_channel.integrated.models import IntegratedAsset

    connection = Connection(id="conn-remote", name="remote source", kind="postgres",
                            config={"host": "10.0.0.9", "port": 5432, "user": "svc",
                                    "password": "secret-v1", "database": "erp"},
                            status="active", created_by=admin_user.id)
    asset = IntegratedAsset(
        id="asset-remote", dataset_id="dataset-remote", connection_id=connection.id,
        name="customers", schema_name="public", table_name="customers",
        schema_json={"columns": [{"name": "id", "type": "integer", "nullable": False},
                                 {"name": "name", "type": "string", "nullable": True}],
                     "primary_key": ["id"]},
        schema_fingerprint="fp-customers-v1", writable=False, created_by=admin_user.id)
    db.add_all([connection, asset])
    db.commit()
    # Snapshot the original credentials: committing expires the ORM instance,
    # and later reads of connection.config must not silently pick up rotations.
    return SimpleNamespace(user=admin_user, connection=connection, asset=asset,
                           original_config=dict(connection.config))


def _fake_remote_scope(events, table, conn):
    """A stand-in open_read_scope whose stream is served from SQLite.

    Metadata resolution stays production (real resolve_metadata over the
    platform rows); only the physical source connection is replaced.
    """
    from app.data_channel.integrated.native import resolve_metadata

    class FakeScope:
        def __init__(self, db, user, asset_ids):
            self.meta = resolve_metadata(db, user, asset_ids)
            self.tables = {asset_id: table for asset_id in asset_ids}
            self.provenance = {"id": "remote-snapshot-1", "kind": "repeatable_read",
                               "consistent": True, "started_at": "2026-10-26T00:00:00+00:00"}
            self.completion = {"complete": False, "row_count": 0}

        def iter_batches(self, statement, params=None, *, batch_size=1000):
            self.completion = {"complete": False, "row_count": 0,
                               "snapshot_id": self.provenance["id"]}
            result = conn.execute(statement, params or {}).mappings()
            rows = [dict(row) for row in result]
            result.close()
            if rows:
                self.completion["row_count"] = len(rows)
                yield rows
            self.completion["complete"] = True

    @contextmanager
    def fake_open_read_scope(db, user, asset_ids, *, statement_timeout_ms=300000):
        events.append(("open", tuple(asset_ids), statement_timeout_ms))
        try:
            yield FakeScope(db, user, asset_ids)
        finally:
            events.append("close")

    return fake_open_read_scope


def test_resolve_integrated_execute_opens_remote_scope(db, remote_asset, sources, monkeypatch):
    from app.data_channel.connections.models import Connection
    from app.data_channel.integrated import native
    from app.data_channel.integrated.native import resolve_metadata
    from app.data_channel.query import providers

    events = []
    probes = []
    monkeypatch.setattr(native, "probe_source",
                        lambda connection, **kwargs: probes.append(connection.id))
    monkeypatch.setattr(native, "open_read_scope",
                        _fake_remote_scope(events, sources["customers"], sources["pg_conn"]))
    limits = QueryLimits(statement_timeout_ms=1234)
    binding = Binding(kind="integrated", asset_id=remote_asset.asset.id)
    with ExitStack() as stack:
        relations = providers.resolve_integrated(db, remote_asset.user, [("src", binding)],
                                                 limits, stack)
        relation = relations["src"]
        # Execute-time resolution enters the remote scope eagerly with the
        # service statement budget.
        assert events == [("open", ("asset-remote",), 1234)]
        assert relation.source_group is not None
        assert relation.source_group.provenance["id"] == "remote-snapshot-1"
        assert relation.state["asOf"] == "2026-10-26T00:00:00+00:00"
        assert relation.state["snapshotId"] == "remote-snapshot-1"
        meta = resolve_metadata(db, remote_asset.user, [remote_asset.asset.id])[remote_asset.asset.id]
        assert relation.auth_fingerprint == meta["auth_fingerprint"]
        assert relation.schema_fingerprint == remote_asset.asset.schema_fingerprint
        # The execute-time relation carries a working bounded stream.
        statement = sa.select(relation.table.c.id).order_by(relation.table.c.id)
        streamed = [row["id"] for batch in relation.batches(statement, {}, 500) for row in batch]
        assert streamed == [1, 2, 3]
    # Scope teardown is registered on the caller's stack and ran exactly once.
    assert events == [("open", ("asset-remote",), 1234), "close"]
    # Execute-time resolution never probes liveness: the read scope itself is
    # the physical availability proof on this path.
    assert probes == []

    # "inactive" is the untested creation default, not a disable: reads stay
    # allowed. Only the explicit failed-test "error" state rejects up front,
    # before any scope is opened.
    db.execute(sa.update(Connection).where(Connection.id == remote_asset.connection.id)
               .values(status="inactive"))
    db.commit()
    with ExitStack() as stack:
        providers.resolve_integrated(db, remote_asset.user, [("src", binding)], limits, stack)
    db.execute(sa.update(Connection).where(Connection.id == remote_asset.connection.id)
               .values(status="error"))
    db.commit()
    with ExitStack() as stack:
        with pytest.raises(QueryError) as exc:
            providers.resolve_integrated(db, remote_asset.user, [("src", binding)],
                                         limits, stack)
        assert (exc.value.status, exc.value.code) == (409, "source_connection_error")
    assert probes == []
    db.execute(sa.update(Connection).where(Connection.id == remote_asset.connection.id)
               .values(status="active"))
    db.commit()
    with ExitStack() as stack:
        providers.resolve_integrated(db, remote_asset.user, [("src", binding)], limits, stack)
    # Three successful resolutions (initial, inactive, restored) each opened
    # and closed exactly one scope; the error state opened none.
    assert events == [("open", ("asset-remote",), 1234), "close"] * 3
    assert probes == []


def test_resolve_integrated_recheck_only_never_opens_remote_scope(db, remote_asset, monkeypatch):
    from app.data_channel.integrated import native
    from app.data_channel.integrated.native import resolve_metadata
    from app.data_channel.query import providers

    def forbidden(*args, **kwargs):
        raise AssertionError("recheck-only resolution must not open a remote read scope")

    probes = []
    real_probe = native.probe_source
    monkeypatch.setattr(native, "probe_source",
                        lambda connection, **kwargs: probes.append(connection.id))
    monkeypatch.setattr(native, "open_read_scope", forbidden)
    binding = Binding(kind="integrated", asset_id=remote_asset.asset.id)
    with ExitStack() as stack:
        relation = providers.resolve_integrated(db, remote_asset.user, [("src", binding)],
                                                QueryLimits(), stack,
                                                recheck_only=True)["src"]
    meta = resolve_metadata(db, remote_asset.user, [remote_asset.asset.id])[remote_asset.asset.id]
    # No stream, no source group: pages read already-materialized platform rows.
    assert relation.batches is None
    assert relation.source_group is None
    assert relation.table.original.name == "integrated_asset-remote"
    assert set(relation.columns) == {"id", "name"}
    assert relation.state["asOf"] is None and relation.state["snapshotId"] is None
    assert relation.state["completeness"] == "complete"
    assert relation.auth_fingerprint == meta["auth_fingerprint"]
    assert relation.schema_fingerprint == remote_asset.asset.schema_fingerprint
    # With an unchanged context the metadata recheck passes and probes the
    # physical source exactly once (availability, never schema or rows).
    assert probes == []
    relation.recheck()
    assert probes == ["conn-remote"]
    relation.recheck()
    assert probes == ["conn-remote"]

    # Probe transport budget on the real engine-construction path: connection
    # establishment is bounded by connect_timeout on both drivers; psycopg2
    # has no client read deadline, so the PostgreSQL session carries a libpq
    # options statement_timeout of the same budget bounding server-side
    # execution of the single SELECT 1, while MySQL bounds its socket
    # client-side with read/write timeouts. A malformed stored config stays a
    # 422 configuration defect (INVALID_SOURCE_CONFIG) on both the probe and
    # read engines, never a physical-availability verdict.
    from fastapi import HTTPException

    engines = []

    def fake_create_engine(url, **kwargs):
        engine = SimpleNamespace(connect_args=kwargs["connect_args"], disposed=False)
        engines.append(engine)

        @contextmanager
        def connect():
            yield SimpleNamespace(exec_driver_sql=lambda statement: None)

        def dispose():
            engine.disposed = True

        engine.connect, engine.dispose = connect, dispose
        return engine

    monkeypatch.setattr(native.sa, "create_engine", fake_create_engine)
    real_probe(SimpleNamespace(kind="postgres", config=dict(remote_asset.original_config)))
    real_probe(SimpleNamespace(kind="mysql", config={"host": "10.0.0.9", "port": 3306,
                                                     "user": "svc", "password": "secret-v1",
                                                     "database": "erp"}))
    budget = native.PROBE_TIMEOUT_SECONDS
    assert [(engine.connect_args, engine.disposed) for engine in engines] == [
        ({"connect_timeout": budget, "options": f"-c statement_timeout={budget * 1000}"}, True),
        ({"connect_timeout": budget, "read_timeout": budget, "write_timeout": budget}, True),
    ]
    broken = SimpleNamespace(kind="postgres", config={"connection_string": "not a url"})
    with pytest.raises(HTTPException) as exc:
        real_probe(broken)
    assert (exc.value.status_code, exc.value.detail["code"]) == (422, "INVALID_SOURCE_CONFIG")
    with pytest.raises(HTTPException) as exc:
        native.source_engine(broken)
    assert exc.value.detail["code"] == "INVALID_SOURCE_CONFIG"
    # An invalid configuration never constructs a source engine at all.
    assert len(engines) == 2


def test_recheck_only_relation_fails_closed_on_credential_or_schema_change(db, remote_asset, monkeypatch):
    from fastapi import HTTPException
    from app.auth.models import User
    from app.data_channel.connections.models import Connection
    from app.data_channel.integrated import native
    from app.data_channel.integrated.models import IntegratedAsset
    from app.data_channel.query import providers

    monkeypatch.setattr(native, "open_read_scope", lambda *args, **kwargs:
                        pytest.fail("recheck-only resolution must not open a remote read scope"))
    probes = []

    def probe(connection, **kwargs):
        probes.append(connection.id)

    monkeypatch.setattr(native, "probe_source", probe)
    binding = Binding(kind="integrated", asset_id=remote_asset.asset.id)
    with ExitStack() as stack:
        relation = providers.resolve_integrated(db, remote_asset.user, [("src", binding)],
                                                QueryLimits(), stack,
                                                recheck_only=True)["src"]
    relation.recheck()  # baseline: unchanged context revalidates and probes once
    assert probes == ["conn-remote"]

    # Credential rotation changes the connection config revision inside the
    # pinned auth fingerprint: the cursor context is invalidated, fail-closed.
    db.execute(sa.update(Connection).where(Connection.id == remote_asset.connection.id)
               .values(config={**remote_asset.original_config, "password": "secret-v2"}))
    db.commit()
    with pytest.raises(QueryError) as exc:
        relation.recheck()
    assert (exc.value.status, exc.value.code) == (409, "query_context_changed")

    # Restoring the credentials revalidates the same relation again.
    db.execute(sa.update(Connection).where(Connection.id == remote_asset.connection.id)
               .values(config=remote_asset.original_config))
    db.commit()
    relation.recheck()

    # Schema fingerprint drift fails closed the same way.
    db.execute(sa.update(IntegratedAsset).where(IntegratedAsset.id == remote_asset.asset.id)
               .values(schema_fingerprint="fp-customers-v2"))
    db.commit()
    with pytest.raises(QueryError) as exc:
        relation.recheck()
    assert (exc.value.status, exc.value.code) == (409, "query_context_changed")
    db.execute(sa.update(IntegratedAsset).where(IntegratedAsset.id == remote_asset.asset.id)
               .values(schema_fingerprint="fp-customers-v1"))
    db.commit()
    relation.recheck()

    # A deregistered asset must also never silently revalidate.
    db.execute(sa.delete(IntegratedAsset).where(IntegratedAsset.id == remote_asset.asset.id))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        relation.recheck()
    assert exc.value.status_code == 404

    # Missing rows fail the page closed before any probe: with the asset gone
    # the asset linkage rejects first (404); a dangling connection would be
    # rejected by the same metadata resolution a moment later.
    db.execute(sa.delete(Connection).where(Connection.id == remote_asset.connection.id))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        relation.recheck()
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "ASSET_NOT_FOUND"

    # Restore both rows for the availability cases below. The bulk SQL deletes
    # bypassed the session, so fresh instances replace the stale identity-map
    # objects instead of re-adding them.
    user_id = remote_asset.user.id
    db.expunge_all()
    user = db.get(User, user_id)
    db.add(Connection(id="conn-remote", name="remote source", kind="postgres",
                      config=remote_asset.original_config, status="active",
                      created_by=user_id))
    db.add(IntegratedAsset(
        id="asset-remote", dataset_id="dataset-remote", connection_id="conn-remote",
        name="customers", schema_name="public", table_name="customers",
        schema_json={"columns": [{"name": "id", "type": "integer", "nullable": False},
                                 {"name": "name", "type": "string", "nullable": True}],
                     "primary_key": ["id"]},
        schema_fingerprint="fp-customers-v1", writable=False, created_by=user_id))
    db.commit()

    # Physical outage mid-pagination: metadata is unchanged and the connection
    # stays "active", but the bounded SELECT 1 cannot reach the source — the
    # page fails closed (503 SOURCE_UNAVAILABLE), never reinterpreted as
    # metadata-only success. A later successful probe resumes delivery of the
    # still-valid snapshot rows on the next fetch.
    def unreachable(connection, **kwargs):
        probes.append("unreachable:" + connection.id)
        raise HTTPException(503, {"code": "SOURCE_UNAVAILABLE",
                                  "message": "Source is not physically reachable"})

    monkeypatch.setattr(native, "probe_source", unreachable)
    with ExitStack() as stack:
        relation = providers.resolve_integrated(db, user, [("src", binding)],
                                                QueryLimits(), stack,
                                                recheck_only=True)["src"]
    with pytest.raises(HTTPException) as exc:
        relation.recheck()
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "SOURCE_UNAVAILABLE"
    monkeypatch.setattr(native, "probe_source", probe)
    relation.recheck()

    # Lifecycle changes are fenced by the status input inside the auth
    # fingerprint: flipping the pinned "active" state (test pass, rotation
    # reset, failed test) invalidates in-flight pages with config untouched.
    for status in ("inactive", "error"):
        db.execute(sa.update(Connection).where(Connection.id == "conn-remote")
                   .values(status=status))
        db.commit()
        with pytest.raises(QueryError) as exc:
            relation.recheck()
        assert (exc.value.status, exc.value.code) == (409, "query_context_changed")
    db.execute(sa.update(Connection).where(Connection.id == "conn-remote")
               .values(status="active"))
    db.commit()
    relation.recheck()

    # A fresh resolution against an explicit failed-test connection is
    # rejected up front without any scope or probe.
    db.execute(sa.update(Connection).where(Connection.id == "conn-remote")
               .values(status="error"))
    db.commit()
    with ExitStack() as stack:
        with pytest.raises(QueryError) as exc:
            providers.resolve_integrated(db, user, [("src", binding)], QueryLimits(), stack,
                                         recheck_only=True)
        assert (exc.value.status, exc.value.code) == (409, "source_connection_error")
    db.execute(sa.update(Connection).where(Connection.id == "conn-remote")
               .values(status="active"))
    db.commit()

    # One bounded probe per unique physical source per page fetch: two assets
    # sharing one connection pay a single SELECT 1 even when both recheck.
    second = IntegratedAsset(
        id="asset-remote-2", dataset_id="dataset-remote-2", connection_id="conn-remote",
        name="orders", schema_name="public", table_name="orders",
        schema_json={"columns": [{"name": "oid", "type": "integer", "nullable": False}],
                     "primary_key": ["oid"]},
        schema_fingerprint="fp-orders-v1", writable=False, created_by=user_id)
    db.add(second)
    db.commit()
    probes.clear()
    with ExitStack() as stack:
        relations = providers.resolve_integrated(db, user,
                                                 [("src", binding),
                                                  ("ord", Binding(kind="integrated", asset_id=second.id))],
                                                 QueryLimits(), stack, recheck_only=True)
    relations["src"].recheck()
    relations["ord"].recheck()
    assert probes == ["conn-remote"]


def test_memory_execute_opens_scope_once_and_fetch_pages_recheck_only(db, remote_asset, sources, monkeypatch):
    from app.data_channel.integrated import native
    from app.data_channel.query import providers
    from app.data_channel.query.contracts import TextQueryCursorRequest

    events = []
    probes = []
    monkeypatch.setattr(native, "probe_source",
                        lambda connection, **kwargs: probes.append(connection.id))
    monkeypatch.setattr(native, "open_read_scope",
                        _fake_remote_scope(events, sources["customers"], sources["pg_conn"]))
    flags = []
    real_resolve_integrated = providers.resolve_integrated

    def tracking_resolve_integrated(db, user, bindings, limits, stack, *, recheck_only=False):
        flags.append(recheck_only)
        return real_resolve_integrated(db, user, bindings, limits, stack, recheck_only=recheck_only)

    monkeypatch.setattr(providers, "resolve_integrated", tracking_resolve_integrated)
    service = QueryService(db, remote_asset.user, secret="recheck-memory-test")
    request = TypedQueryRequest(
        pipeline=typed_pipeline("from src as src | fields src.id | sort src.id asc"),
        bindings={"src": Binding(kind="integrated", asset_id=remote_asset.asset.id)},
        page_size=2, result_storage="memory")
    first = service.execute_typed(request)
    assert [row["id"] for row in first["rows"]] == ["1", "2"]
    assert first["page"]["hasMore"] is True and first["page"]["cursor"]
    # Execute opened (and tore down) exactly one remote scope; no paging yet.
    assert flags == [False]
    assert events == [("open", ("asset-remote",), QueryLimits().statement_timeout_ms), "close"]
    # The execute path proves availability through the read scope itself: it
    # never runs a liveness probe.
    assert probes == []

    second = service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert [row["id"] for row in second["rows"]] == ["3"]
    assert second["page"]["cursor"] is None
    # Cursor paging resolved metadata only: recheck_only=True, no scope reopen,
    # exactly one bounded SELECT 1 per unique source for the whole fetch.
    assert flags == [False, True]
    assert events == [("open", ("asset-remote",), QueryLimits().statement_timeout_ms), "close"]
    assert probes == ["conn-remote"]


def test_persisted_fetch_resolves_recheck_only_and_pages_existing_snapshot(db, remote_asset, sources, monkeypatch):
    from app.data_channel.query import providers
    from app.data_channel.query.contracts import TextQueryCursorRequest, stable_hash
    from app.data_channel.query.models import UnifiedQueryRow, UnifiedQuerySnapshot
    from app.data_channel.query.service import _utc

    flags = []

    def fake_resolve_integrated(db, user, bindings, limits, stack, *, recheck_only=False):
        flags.append(recheck_only)
        return {alias: sources["pg"]["customers"] for alias, _ in bindings}

    monkeypatch.setattr(providers, "resolve_integrated", fake_resolve_integrated)
    service = QueryService(db, remote_asset.user, secret="recheck-persisted-test")
    service._pin = lambda: None  # the PG-only pin is out of scope for SQLite unit tests
    relation = sources["pg"]["customers"]
    auth_fp = stable_hash({"src": relation.auth_fingerprint})
    schema_fp = stable_hash({"src": relation.schema_fingerprint})
    plan = service._plan_from_text(TextQueryRequest(
        language="spl", query="from src as src | fields src.id | sort src.id asc",
        bindings={"src": {"kind": "integrated", "assetId": remote_asset.asset.id}},
        page_size=2, result_storage="persisted"))
    snapshot = UnifiedQuerySnapshot(
        id="snap-recheck", owner_user_id=str(remote_asset.user.id), query_hash=plan.query_hash,
        auth_fingerprint=auth_fp, schema_fingerprint=schema_fp, request=plan.stored_request,
        columns=[{"name": "id", "type": "integer"}], source_states=[],
        consistency="single_source_snapshot", plan={}, result_limited=False,
        total_rows=3, result_bytes=64, page_size=2,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    db.add(snapshot)
    for ordinal, row in enumerate([{"id": "1"}, {"id": "2"}, {"id": "3"}]):
        db.add(UnifiedQueryRow(snapshot_id=snapshot.id, ordinal=ordinal, row=row))
    db.commit()
    cursor = service._sign({"v": 1, "id": snapshot.id, "owner": snapshot.owner_user_id,
                            "q": snapshot.query_hash, "a": auth_fp, "s": schema_fp,
                            "next": 0, "size": 2, "exp": _utc(snapshot.expires_at).timestamp()})
    first = service.fetch(TextQueryCursorRequest(cursor=cursor))
    assert [row["id"] for row in first["rows"]] == ["1", "2"]
    assert first["page"]["hasMore"] is True
    # Persisted paging resolved the plan with recheck_only=True (never execute).
    assert flags == [True]
    second = service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert [row["id"] for row in second["rows"]] == ["3"]
    assert second["page"]["cursor"] is None
    assert flags == [True, True]
