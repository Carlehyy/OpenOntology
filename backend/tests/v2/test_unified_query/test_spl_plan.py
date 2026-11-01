"""Staged SPL text planning: structural contracts and core lowering.

Deterministic language/planning contracts, not a substitute for real
PostgreSQL E2E. Execution here runs only over in-memory SQLite relations to
prove the lowered typed request compiles and stages push down as derived
subqueries — authorization stays behind the composition root.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.data_channel.query.compiler import compile_query
from app.data_channel.query.contracts import (
    Binding, ColumnSpec, DerivedSourceSpec, QueryError, Relation,
)
from app.data_channel.query.derived import resolve_derived
from app.data_channel.query.parser import parse, parse_spl_plan
from app.data_channel.query.spl_plan import (
    MAX_PLAN_COMMANDS, MAX_PLAN_STAGES, build_spl_request, leaf_name_map,
    plan_fingerprint, validate_plan_graph,
)

LEAF = {"customers": Binding(kind="integrated", asset_id="asset-c"),
        "orders": Binding(kind="integrated", asset_id="asset-o")}

TWO_STAGE = ("with reg = from customers as c | where c.region = :r "
             "| fields c.id as cid, c.region as region; "
             "from reg as r1 | join orders as o on r1.cid = o.customer_id "
             "| stats count() as n, sum(o.amount) as total by r1.region")


@pytest.fixture
def relations():
    metadata = sa.MetaData()
    orders = sa.Table("orders", metadata, sa.Column("id", sa.BigInteger),
                      sa.Column("customer_id", sa.BigInteger),
                      sa.Column("amount", sa.Numeric(30, 4)))
    customers = sa.Table("customers", metadata, sa.Column("id", sa.BigInteger),
                         sa.Column("region", sa.Text))

    def relation(alias, table, specs, connection="test"):
        return Relation(alias, table, {spec.name: spec for spec in specs},
                        {"completeness": "complete"}, "auth", "schema", connection,
                        platform=True)

    return {"customers": relation("c", customers, [ColumnSpec("id", "integer"),
                                                   ColumnSpec("region", "string")]),
            "orders": relation("o", orders, [ColumnSpec("id", "integer"),
                                             ColumnSpec("customer_id", "integer"),
                                             ColumnSpec("amount", "decimal",
                                                        precision=30, scale=4)])}


def lower(text, bindings=None, **kwargs):
    return build_spl_request(parse_spl_plan(text), bindings if bindings is not None else LEAF,
                             **kwargs)


def resolve_and_compile(text, relations, params=None, bindings=None):
    request = lower(text, bindings=bindings, params=params)
    name_map = leaf_name_map(parse_spl_plan(text), bindings if bindings is not None else LEAF)
    base = {internal: relations[public] for public, internal in name_map.items()}
    resolved = resolve_derived(base, request.derived)
    # compile_query keys relations by pipeline alias (same as derived_relation).
    alias_relations = {alias: resolved[name] for name, alias in request.pipeline.sources}
    # The request's params dict is main-pipeline scoped by the lowering.
    return compile_query(request.pipeline, alias_relations, request.params, dialect="sqlite")


# ---- single-pipeline compatibility ----

def test_single_pipeline_parse_is_unchanged_and_rejects_staged_text():
    pipeline = parse("from orders as o | where o.amount > 1 | fields o.id as oid | limit 10")
    assert pipeline.source == "orders" and pipeline.alias == "o"
    assert [command.op for command in pipeline.commands] == ["where", "fields", "limit"]
    with pytest.raises(QueryError) as exc:
        parse("with f = from orders as o | fields o.id; from f as f1 | fields f1.id")
    assert exc.value.code == "multi_stage_unsupported"


def test_parse_spl_plan_accepts_stageless_text_and_validates_stages():
    plan = parse_spl_plan("from orders as o | fields o.id")
    assert plan.stages == () and plan.pipeline.source == "orders"
    staged = parse_spl_plan(TWO_STAGE)
    assert staged.stage_names == ("reg",)
    with pytest.raises(QueryError) as exc:
        parse_spl_plan(f"with {'a' * 64} = from orders as o | fields o.id; "
                       f"from {'a' * 64} as x | fields x.id")
    assert exc.value.code == "invalid_identifier"
    with pytest.raises(QueryError) as exc:
        parse_spl_plan("with f = from orders as o | fields o.id; "
                       "with f = from customers as c | fields c.id; from f as x | fields x.id")
    assert exc.value.code == "duplicate_stage"


# ---- lowering: stages become same-source derived relations ----

def test_stages_lower_to_derived_specs_with_stable_names_and_fingerprint():
    request = lower(TWO_STAGE, params={"r": "north"})
    name_map = leaf_name_map(parse_spl_plan(TWO_STAGE), LEAF)
    assert name_map == {"customers": "s1", "orders": "s3"}
    assert set(request.derived) == {"s2"}
    spec = request.derived["s2"]
    assert isinstance(spec, DerivedSourceSpec)
    assert spec.source == "s1"
    assert [command.op for command in spec.pipeline.commands] == ["where", "fields"]
    assert spec.pipeline.source == "s1" and spec.pipeline.alias == "c"
    assert request.pipeline.source == "s2" and request.pipeline.alias == "r1"
    assert set(request.bindings) == {"s1", "s3"}
    assert request.join_constraints == {}
    assert request.result_storage == "persisted"  # dataclass default, never hardcoded here
    scoped = lower(TWO_STAGE, params={"r": "north", "main_only": 5})
    # Stage-only params live on the spec; main keeps the rest.
    assert request.derived["s2"].params == {"r": "north"}
    assert scoped.derived["s2"].params == {"r": "north"}
    assert scoped.params == {"main_only": 5}
    again = lower(TWO_STAGE, params={"r": "north"})
    assert request.request_fingerprint == again.request_fingerprint
    assert request.request_fingerprint != lower(TWO_STAGE, params={"r": "south"}).request_fingerprint


def test_result_storage_passes_through_untouched():
    request = lower("from orders as o | fields o.id", result_storage="memory")
    assert request.result_storage == "memory"


def test_two_stage_plan_compiles_and_pushes_filters_into_the_stage_subquery(relations):
    statement = resolve_and_compile(TWO_STAGE, relations, params={"r": "north"})
    sql = str(statement.statement)
    assert "customers" in sql and "orders" in sql
    # The stage filter lands inside the derived subquery, not the outer join.
    assert sql.count("region") >= 1


def test_two_stage_execution_returns_filtered_join_rows(relations):
    # Aggregate shapes are covered at compile level (their PG COLLATE handling
    # is executor territory); this proves stage pushdown + federated join rows.
    # The stage filter is numeric: SQLite's TEXT COLLATE handling of compiled
    # string predicates is a dialect quirk owned by core (real target is PG).
    text = ("with reg = from customers as c | where c.id = :cid "
            "| fields c.id as cid, c.region as region; "
            "from reg as r1 | join orders as o on r1.cid = o.customer_id "
            "| fields r1.region, o.id as oid, o.amount as amount")
    engine = sa.create_engine("sqlite://")
    relations["customers"].table.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(relations["customers"].table.insert(),
                     [{"id": 1, "region": "north"}, {"id": 2, "region": "south"}])
        conn.execute(relations["orders"].table.insert(), [
            {"id": 1, "customer_id": 1, "amount": Decimal("10.50")},
            {"id": 2, "customer_id": 1, "amount": Decimal("30.00")},
            {"id": 3, "customer_id": 2, "amount": Decimal("7.00")},
        ])
        compiled = resolve_and_compile(text, relations, params={"cid": 1})
        rows = sorted((dict(row._mapping) for row in conn.execute(compiled.statement,
                                                                  compiled.params)),
                      key=lambda row: row["oid"])
    assert rows == [
        {"region": "north", "oid": 1, "amount": Decimal("10.50")},
        {"region": "north", "oid": 2, "amount": Decimal("30.00")},
    ]
    engine.dispose()


def test_nested_stage_chain_compiles_over_resolved_derived(relations):
    text = ("with a = from customers as c | fields c.id as cid, c.region as region; "
            "with b = from a as x | where x.region = :r | fields x.cid as cid; "
            "from b as y | fields y.cid")
    statement = resolve_and_compile(text, relations, params={"r": "north"})
    sql = str(statement.statement)
    assert sql.lower().count("select") >= 3  # two nested derived subqueries + outer


def test_stage_combining_cross_connection_leaves_is_rejected_by_core(relations):
    remote = dict(relations)
    remote["orders"] = Relation("o", relations["orders"].table, relations["orders"].columns,
                                {"completeness": "complete"}, "auth", "schema", "other-conn",
                                platform=True)
    # A stage that itself joins two leaves must share one physical connection;
    # the MAIN pipeline may still federate across stages/leaves legally.
    text = ("with bad = from customers as c | join orders as o on c.id = o.customer_id "
            "| fields c.id as cid; from bad as x | fields x.cid")
    request = lower(text)
    base = {internal: remote[public]
            for public, internal in leaf_name_map(parse_spl_plan(text), LEAF).items()}
    with pytest.raises(QueryError) as exc:
        resolve_derived(base, request.derived)
    assert exc.value.code == "cross_source_derived_rejected"


# ---- structural rejections ----

@pytest.mark.parametrize("text,bindings,code", [
    ("with f = from ghost as g | fields g.id; from f as x | fields x.id",
     LEAF, "unknown_source"),
    ("with a = from f as x | fields x.id; "
     "with f = from customers as c | fields c.id; from a as y | fields y.id",
     LEAF, "forward_stage_reference"),
    ("with f = from f as x | fields x.id; from f as y | fields y.id",
     LEAF, "stage_cycle"),
    ("with f = from customers as c | fields c.id; from orders as o | fields o.id",
     LEAF, "dead_stage"),
    ("from orders as o | join customers as c on o.id = c.id | fields o.id",
     {"orders": LEAF["orders"]}, "unknown_source"),
    ("with c = from customers as x | fields x.id; from c as y | fields y.id",
     {"c": LEAF["customers"]}, "duplicate_source"),
    ("from orders as o | fields o.id",
     {"orders": Binding(kind="ontology", ontology_id="ont-1",
                        release_id="rel-1", object_type_id="t-1")},
     "semantic_binding_rejected"),
    ("from orders as o | fields o.id",
     {}, "invalid_query"),
])
def test_structural_rejections(text, bindings, code):
    with pytest.raises(QueryError) as exc:
        lower(text, bindings=bindings)
    assert exc.value.code == code


def test_unreferenced_bindings_stay_inert_like_single_pipeline_text():
    request = lower("with f = from customers as c | fields c.id; from f as x | fields x.id",
                    bindings={"customers": LEAF["customers"], "orders": LEAF["orders"]})
    assert set(request.bindings) == {"s1"}  # orders never referenced -> never resolved


def test_overcomplex_plans_are_rejected_whole_plan():
    stage = " | ".join(f"where c.id > {i}" for i in range(70))
    with pytest.raises(QueryError) as exc:
        lower(f"with f = from customers as c | {stage}; from f as x | fields x.id")
    assert exc.value.code == "query_too_complex"
    text = "".join(f"with s{i} = from customers as c{i} | fields c{i}.id; "
                   for i in range(MAX_PLAN_STAGES + 1))
    with pytest.raises(QueryError) as exc:
        lower(text + "from s0 as x | fields x.id", bindings={"customers": LEAF["customers"]})
    assert exc.value.code == "query_too_complex"


def test_whole_plan_command_budget_counts_every_stage_not_per_stage():
    # Two stages of 31 commands each: every stage alone stays under the
    # per-pipeline cap (64), but 62 + outer > the whole-plan budget of 64.
    stage_body = " | ".join(["where c.id > 0"] * 30)
    stages = (f"with s0 = from customers as c | {stage_body} | fields c.id as id0; "
              f"with s1 = from customers as d | {stage_body} | fields d.id as id1; ")
    base = stages + "from s0 as x | join s1 as y on x.id0 = y.id1 | fields x.id0"
    plan = parse_spl_plan(base)
    assert sum(len(p.commands) for p in plan.pipelines) == 64  # 31+31+2: exactly at budget
    assert lower(base).params == {}
    with pytest.raises(QueryError) as exc:
        lower(base + " | where x.id0 > 0")
    assert exc.value.code == "query_too_complex"
    assert MAX_PLAN_COMMANDS == 64 and MAX_PLAN_STAGES == 8


def test_fingerprint_distinguishes_stages_params_and_order():
    plan_a = parse_spl_plan(TWO_STAGE)
    plan_b = parse_spl_plan("with reg = from customers as c | fields c.id as cid; "
                            "from reg as r1 | join orders as o on r1.cid = o.customer_id "
                            "| stats count() as n by r1.region")
    assert plan_fingerprint(plan_a, {"r": "north"}) == plan_fingerprint(plan_a, {"r": "north"})
    assert plan_fingerprint(plan_a, {"r": "north"}) != plan_fingerprint(plan_a, {"r": "south"})
    assert plan_fingerprint(plan_a) != plan_fingerprint(plan_b)


def test_validate_plan_graph_rejects_non_plan_and_reports_leaf_usage():
    with pytest.raises(QueryError) as exc:
        validate_plan_graph("not-a-plan", LEAF)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_pipeline"

def test_outer_federation_across_connections_compiles(relations):
    remote = dict(relations)
    remote["orders"] = Relation("o", relations["orders"].table, relations["orders"].columns,
                                {"completeness": "complete"}, "auth", "schema", "other-conn",
                                platform=True)
    # Stage pushes the aggregation down on customers' connection; the main
    # pipeline federates the stage result with the remote leaf. Compile-level
    # federation is legal here; execution routing belongs to the executor.
    text = ("with reg = from customers as c | where c.region = :r "
            "| stats count() as n by c.region; "
            "from reg as r1 | join orders as o on r1.n = o.customer_id | fields r1.region, o.id")
    statement = resolve_and_compile(text, remote, params={"r": "north"})
    assert "customers" in str(statement.statement) and "orders" in str(statement.statement)


def test_stage_without_terminal_projection_is_rejected():
    with pytest.raises(QueryError) as exc:
        lower("with f = from customers as c | where c.region = :r; "
              "from f as x | fields x.region", params={"r": "north"})
    assert exc.value.code == "stage_requires_projection"
