"""Lazy detail continuation: source-side keyset paging for single-source
detail queries.

Covers the supported boundary (declared structural unique key, exact-typed
ordering, no JOIN/STATS/DISTINCT), transport honesty (page+probe only, only
requested columns, no whole-source COUNT), cumulative caps with observable
release, sequential-cursor semantics under concurrent fetches, NULL-aware
strictly-after predicates, and the bounded materialized fallback when the
boundary is not met. All physical sources are deterministic SQLite-backed
relations; the production execution path is under test.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.data_channel.query.contracts import (Binding, ColumnSpec, QueryError,
                                              QueryLimits, Relation,
                                              TextQueryCursorRequest,
                                              TextQueryRequest)
from app.data_channel.query.derived import resolve_derived
from app.data_channel.query.handles import REGISTRY
from app.data_channel.query.ontology_adapter import build_typed_request
from app.data_channel.query.service import (_keyset_predicate, _lazy_keyset_shape,
                                            QueryService)
from app.data_channel.query.parser import parse


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


def counting_relation(relation, counter):
    original = relation.batches

    def batches(statement, params, batch_size):
        counter["sql"].append(str(statement))
        counter["limits"].append((getattr(statement, "_limit", None),
                                  getattr(statement, "_offset", None)))
        counter["queries"] += 1
        rows = []
        for batch in original(statement, params, batch_size):
            rows.extend(batch)
            counter["rows"] += len(batch)
        yield rows
    return replace(relation, batches=batches)


def make_service(relations, limits=None):
    service = QueryService(
        SimpleNamespace(in_transaction=lambda: False, rollback=lambda: None,
                        get_bind=lambda: SimpleNamespace(
                            dialect=SimpleNamespace(name="postgresql"))),
        SimpleNamespace(id="owner-1"), secret="lazy-test", limits=limits)
    service.relations = dict(relations)
    service._resolve = lambda plan, **_: setattr(service, "relations", dict(relations))
    return service


@pytest.fixture(autouse=True)
def clean_registry():
    REGISTRY.handles.clear()
    REGISTRY._tickets.clear()
    yield
    REGISTRY.handles.clear()
    REGISTRY._tickets.clear()


@pytest.fixture
def events():
    meta = sa.MetaData()
    table = sa.Table("events", meta,
                     sa.Column("id", sa.BigInteger, primary_key=True),
                     sa.Column("region", sa.Text),
                     sa.Column("amount", sa.Numeric(30, 4)),
                     sa.Column("payload", sa.Text))
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    meta.create_all(engine)
    with engine.begin() as conn:
        for index in range(1, 501):
            region = None if index % 7 == 0 else ("north" if index % 2 else "south")
            conn.execute(table.insert(), {
                "id": index,
                "region": region,
                "amount": Decimal(index) + Decimal("0.2500"),
                "payload": f"row-{index}"})
    with engine.connect() as conn:
        specs = [ColumnSpec("id", "integer", False), ColumnSpec("region", "string"),
                 ColumnSpec("amount", "decimal", True, precision=30, scale=4),
                 ColumnSpec("payload", "string")]
        relation = sqlite_relation("events", table, specs, conn, "ev",
                                   state={"uniqueKey": ["id"]})
        yield {"conn": conn, "table": table, "relation": relation}
    engine.dispose()


def request(query, page_size=20, storage="memory"):
    return TextQueryRequest(language="spl", query=query, page_size=page_size,
                            bindings={"events": Binding(kind="integrated", asset_id="a1")},
                            result_storage=storage)


def page_all(service, first):
    pages = [first]
    while pages[-1]["page"]["hasMore"]:
        pages.append(service.fetch(TextQueryCursorRequest(cursor=pages[-1]["page"]["cursor"])))
    return pages


# -- eligibility ---------------------------------------------------------------

def test_lazy_shape_boundary(events):
    pipeline = parse("from events as events | fields events.id, events.region")
    shape = _lazy_keyset_shape(pipeline, {"events": events["relation"]})
    assert shape is not None
    assert [item.column for item in shape.order] == ["id"]
    joined = parse("from events as events | join events as other "
                   "on events.id = other.id | fields events.id")
    assert _lazy_keyset_shape(
        joined, {"events": events["relation"], "other": events["relation"]}) is None
    aggregated = parse("from events as events | stats count() as n")
    assert _lazy_keyset_shape(aggregated, {"events": events["relation"]}) is None
    no_key = replace(events["relation"],
                     state={**events["relation"].state, "uniqueKey": []})
    assert _lazy_keyset_shape(pipeline, {"events": no_key}) is None
    float_type = replace(events["relation"], columns={
        **events["relation"].columns, "id": ColumnSpec("id", "float", False)})
    assert _lazy_keyset_shape(pipeline, {"events": float_type}) is None


def test_without_unique_key_pages_from_bounded_materialization(events):
    relation = replace(events["relation"],
                       state={**events["relation"].state, "uniqueKey": []})
    service = make_service({"events": relation})
    response = service.execute(request(
        "from events as events | fields events.id | sort events.id asc"))
    assert response["plan"]["strategy"] == "memory_source_pushdown"
    assert response["totalRows"] == 500
    assert "cursor" in response["page"] and "mode" not in response["page"]


# -- transport honesty: page + probe only, necessary columns, no COUNT --------

def test_single_source_detail_pages_read_only_page_plus_probe(events):
    counter = {"sql": [], "limits": [], "queries": 0, "rows": 0}
    relation = counting_relation(events["relation"], counter)
    service = make_service({"events": relation})
    query = ("from events as events | fields events.id, events.amount "
             "| sort events.amount desc")
    first = service.execute(request(query, page_size=20))
    assert first["plan"]["strategy"] == "memory_lazy_keyset"
    assert first["page"]["mode"] == "keyset_live"
    assert first["page"]["counting"] == "page_probe"
    assert first["totalRows"] is None
    assert len(first["rows"]) == 20
    # Only requested columns (plus the unique-key tiebreaker for ORDER BY).
    assert [column["name"] for column in first["columns"]] == ["id", "amount"]
    first_sql = counter["sql"][-1].upper()
    assert "PAYLOAD" not in first_sql and "REGION" not in first_sql
    assert "COUNT(" not in first_sql
    assert counter["limits"][-1] == (21, 0)

    pages = page_all(service, first)
    assert len(pages) == 25
    every = [row for page in pages for row in page["rows"]]
    assert len(every) == 500
    amounts = [Decimal(row["amount"]) for row in every]
    assert amounts == sorted(amounts, reverse=True)
    last = pages[-1]
    assert last["page"]["counting"] == "complete"
    assert last["totalRows"] == 500
    assert last["page"]["cursor"] is None
    # 24 full pages fetch 21 rows, the final page fetches its 20: 524 total.
    assert counter["rows"] == 24 * 21 + 20
    assert all("COUNT(" not in sql.upper() for sql in counter["sql"])
    # Every page: source-side LIMIT page+1, OFFSET only on page 0, and a
    # strictly-after keyset predicate on every continuation page.
    assert counter["limits"][0] == (21, 0)
    for sql, (limit, offset) in list(zip(counter["sql"], counter["limits"]))[1:]:
        assert limit == 21 and offset in (None, 0)
        assert "<" in sql or ">" in sql


def test_live_source_growth_seen_by_new_query_and_continuation(events):
    service = make_service({"events": events["relation"]})
    query = "from events as events | fields events.id | sort events.id asc"
    first = service.execute(request(query, page_size=250))
    assert len(first["rows"]) == 250 and first["totalRows"] is None
    # Source rows grow 500 -> 510 after the first page (same connection
    # transaction view; no committed-snapshot promise is made or claimed).
    for index in range(501, 511):
        events["conn"].execute(
            events["table"].insert(), {"id": index, "region": "north",
                                       "amount": Decimal("1.0000"),
                                       "payload": f"row-{index}"})
    fresh = make_service({"events": events["relation"]})
    restarted = fresh.execute(request(query, page_size=600))
    assert restarted["page"]["counting"] == "complete"
    assert restarted["totalRows"] == 510
    second = service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    ids = [row["id"] for row in second["rows"]]
    assert ids == [str(index) for index in range(251, 501)]
    third = service.fetch(TextQueryCursorRequest(cursor=second["page"]["cursor"]))
    assert [row["id"] for row in third["rows"]] == [str(index) for index in range(501, 511)]


# -- NULL-aware ordering and composite keys -------------------------------------

def test_nulls_last_secondary_sort_with_unique_tiebreaker(events):
    service = make_service({"events": events["relation"]})
    query = ("from events as events | fields events.id, events.region "
             "| sort events.region asc, events.id desc")
    pages = page_all(service, service.execute(request(query, page_size=17)))
    every = [row for page in pages for row in page["rows"]]
    assert len(every) == 500
    regions = [(row["region"], int(row["id"])) for row in every]
    non_null = [item for item in regions if item[0] is not None]
    nulls = [item for item in regions if item[0] is None]
    # All NULL rows sort after non-NULL (asc, nulls last); ids descend within
    # every equal-region group, including the NULL group.
    assert regions[:len(non_null)] == non_null
    assert regions[len(non_null):] == nulls
    assert non_null == sorted(non_null, key=lambda item: (item[0], -item[1]))
    assert nulls == sorted(nulls, key=lambda item: -item[1])
    assert len({row["id"] for row in every}) == 500  # no skip, no duplicate


def test_composite_unique_key_and_decimal_keyset(events):
    relation = replace(events["relation"],
                       state={**events["relation"].state, "uniqueKey": ["region", "id"]})
    service = make_service({"events": relation})
    query = ("from events as events | fields events.id, events.region, events.amount "
             "| sort events.amount asc")
    pages = page_all(service, service.execute(request(query, page_size=13)))
    every = [row for page in pages for row in page["rows"]]
    assert len(every) == 500
    amounts = [Decimal(row["amount"]) for row in every]
    assert amounts == sorted(amounts)
    assert len({row["id"] for row in every}) == 500


def test_keyset_predicate_null_semantics_unit():
    from app.data_channel.query.service import _LazyOrder
    order = (_LazyOrder("region", "region", "asc", "last"),
             _LazyOrder("id", "id", "asc", "last"))
    expr, params, exhausted = _keyset_predicate("t", order, [None, "7"], {})
    assert not exhausted and params == {"__ks0": "7"}
    # NULL on the leading key binds no parameter; only the strictly-after
    # comparison on the tiebreaker carries a typed value.
    _expr, params, exhausted = _keyset_predicate("t", order, ["north", None], {})
    assert params == {"__ks0": "north"} and not exhausted
    # A cursor at trailing NULLs on every key is exhausted.
    _expr, _params, exhausted = _keyset_predicate("t", order, [None, None], {})
    assert exhausted


# -- limit window semantics ------------------------------------------------------

def test_limit_window_reports_result_limited_without_source_count(events):
    service = make_service({"events": events["relation"]})
    query = ("from events as events | fields events.id "
             "| sort events.id asc | limit 35 offset 5")
    pages = page_all(service, service.execute(request(query, page_size=20)))
    every = [row for page in pages for row in page["rows"]]
    assert [row["id"] for row in every] == [str(index) for index in range(6, 41)]
    assert pages[-1]["totalRows"] == 35
    assert pages[-1]["resultLimited"] is True
    for page in pages[:-1]:
        assert page["resultLimited"] is False and page["totalRows"] is None


# -- page byte boundary ----------------------------------------------------------

def test_page_byte_boundary_never_skips_rows(events):
    from app.data_channel.query.encoding import _json_parts

    def envelope_size(response):
        return sum(len(part.encode("utf-8")) for part in _json_parts({"data": response}))

    query = ("from events as events | fields events.id, events.payload "
             "| sort events.id asc")
    # Derive the limit from the REAL page-0 envelope: metadata plus exactly
    # three rows. Real packing must then serve ~3-row pages (never page_size,
    # never a skip), and the keyset cursor advances only across served rows.
    probe_service = make_service({"events": events["relation"]})
    probe = probe_service.execute(request(query, page_size=50))
    assert probe["plan"]["strategy"] == "memory_lazy_keyset"
    # Page past release-log saturation, then derive the limit from that real
    # continuation envelope: later pages differ only by counter digits.
    for _ in range(5):
        probe = probe_service.fetch(TextQueryCursorRequest(cursor=probe["page"]["cursor"]))
    row_size = envelope_size({"r": probe["rows"][0]})
    padded = dict(probe)
    padded["page"] = {**probe["page"], "cursor": "0" * 640}  # packer cursor bound
    metadata_only = envelope_size({**padded, "rows": []})
    limit = metadata_only + 3 * row_size + 128  # counter-digit growth headroom
    service = make_service({"events": events["relation"]},
                           limits=QueryLimits(result_bytes_limit=limit))
    first = service.execute(request(query, page_size=50))
    pages = page_all(service, first)
    every = [row["id"] for page in pages for row in page["rows"]]
    assert every == [str(index) for index in range(1, 501)]
    assert any(len(page["rows"]) < 50 for page in pages)  # byte-bound pages, never page_size
    # Below the real metadata floor the refusal is explicit, never a partial
    # page dressed as success.
    starving = make_service({"events": events["relation"]},
                            limits=QueryLimits(result_bytes_limit=64))
    with pytest.raises(QueryError) as exc:
        starving.execute(request(query, page_size=50))
    assert exc.value.code == "query_budget_exceeded"
    assert exc.value.extra["budget"] == "page_bytes"


# -- caps and observable release --------------------------------------------------

def test_continuation_rows_cap_rejects_and_releases(events):
    limits = QueryLimits(continuation_rows_limit=30)
    service = make_service({"events": events["relation"]}, limits=limits)
    query = "from events as events | fields events.id | sort events.id asc"
    first = service.execute(request(query, page_size=20))
    handle = REGISTRY.handles[first["queryId"]]
    assert handle.continuation.rows_read == 21
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert exc.value.status == 422
    assert exc.value.code == "query_budget_exceeded"
    assert exc.value.extra["layer"] == "continuation"
    assert exc.value.extra["budget"] == "continuation_rows_limit"
    assert exc.value.extra["released"] == ["result_handle"]
    assert REGISTRY.get(first["queryId"]) is None
    with pytest.raises(QueryError) as gone:
        service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert (gone.value.status, gone.value.code) == (410, "result_handle_expired")


def test_continuation_max_pages_cap(events):
    limits = QueryLimits(continuation_max_pages=1)
    service = make_service({"events": events["relation"]}, limits=limits)
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert exc.value.extra["budget"] == "continuation_max_pages"
    assert REGISTRY.get(first["queryId"]) is None


def test_continuation_bytes_cap(events):
    limits = QueryLimits(continuation_bytes_limit=200)
    service = make_service({"events": events["relation"]}, limits=limits)
    # Page 0 alone exceeds the cumulative byte cap: rejected at execute with
    # the ticket and page-0 scope released (nothing was registered).
    with pytest.raises(QueryError) as exc:
        service.execute(request(
            "from events as events | fields events.id, events.payload", page_size=20))
    assert exc.value.extra["budget"] == "continuation_bytes_limit"
    assert exc.value.extra["layer"] == "continuation"
    assert not REGISTRY.handles


def test_continuation_total_wallclock_never_resets(events):
    service = make_service({"events": events["relation"]})
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    handle = REGISTRY.handles[first["queryId"]]
    # Age the logical query beyond the total wallclock budget.
    handle.continuation.started_monotonic -= service.limits.continuation_total_timeout_ms / 1000 + 1
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert exc.value.extra["budget"] == "continuation_total_timeout_ms"
    assert REGISTRY.get(first["queryId"]) is None


def test_stored_quota_refusal_cancels_handle_without_partial(events, monkeypatch):
    service = make_service({"events": events["relation"]})
    first = service.execute(request("from events as events | fields events.id, events.payload",
                                    page_size=20))
    query_id = first["queryId"]
    assert REGISTRY.get(query_id) is not None
    # The stored-quota refusal fires at refresh time (after the page ran and
    # the handle state advanced): the handle must be cancelled, never left
    # half-served.
    from app.data_channel.query.contracts import QueryError as QE

    def refusing_refresh(handle, limits, **kwargs):
        raise QE(429, "memory_quota_exceeded",
                 "Global in-memory result byte budget exceeded",
                 quota="memory_total_bytes_limit", limitBytes=1)

    monkeypatch.setattr(REGISTRY, "refresh", refusing_refresh)
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert exc.value.status == 429 and exc.value.extra["released"] == ["result_handle"]
    assert exc.value.extra["layer"] == "continuation"
    monkeypatch.undo()
    assert REGISTRY.get(query_id) is None


# -- sequential cursor semantics --------------------------------------------------

def test_only_latest_sequential_cursor_accepted(events):
    service = make_service({"events": events["relation"]})
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    stale = first["page"]["cursor"]
    second = service.fetch(TextQueryCursorRequest(cursor=stale))
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=stale))
    assert (exc.value.status, exc.value.code) == (400, "invalid_cursor")


def test_continuation_cap_constrains_before_reading(events):
    # Page 0 fetches exactly 21 rows; the cap therefore refuses the next page
    # before issuing any new source statement.
    counter = {"sql": [], "limits": [], "queries": 0, "rows": 0}
    relation = counting_relation(events["relation"], counter)
    limits = QueryLimits(continuation_rows_limit=21)
    service = make_service({"events": relation}, limits=limits)
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    queries_before = counter["queries"]
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert exc.value.extra["budget"] == "continuation_rows_limit"
    assert counter["queries"] == queries_before
    assert REGISTRY.get(first["queryId"]) is None


def test_paged_result_never_claims_a_cross_page_snapshot(events):
    service = make_service({"events": events["relation"]})
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    assert first["consistency"] == "live_keyset_paged"
    second = service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert second["consistency"] == "live_keyset_paged"


# -- ontology typed path activation ----------------------------------------------

def _semantic_plan(property_map, page_size=20):
    return {
        "sources": [{"alias": "cust", "binding": {"kind": "integrated", "assetId": "a1"},
                     "propertyMap": dict(property_map),
                     "declaredTypes": {"custId": "integer", "region": "string"}}],
        "sourceFilters": [], "joins": [], "where": [],
        "select": [{"source": "cust", "property": semantic, "as": semantic}
                   for semantic in property_map],
        "groupBy": [], "aggregates": [], "distinct": False,
        "orderBy": [{"output": next(iter(property_map)), "direction": "asc",
                     "nulls": "last"}],
        "pageSize": page_size, "releases": {}, "citations": [],
    }


def _typed_service_over_leaf(events, typed, counter=None):
    leaf = events["relation"]
    if counter is not None:
        leaf = counting_relation(leaf, counter)
    base = {name: leaf for name in typed.bindings}
    relations = resolve_derived(base, typed.derived)
    service = make_service(relations)
    service._resolve = lambda plan, **_: setattr(service, "relations", dict(relations))
    return service


def test_ontology_typed_projection_wrapper_activates_lazy(events):
    counter = {"sql": [], "limits": [], "queries": 0, "rows": 0}
    typed = build_typed_request(
        _semantic_plan({"custId": "id", "region": "region"}),
        request={}, session_kind="ontology", session_id="session-1")
    service = _typed_service_over_leaf(events, typed, counter)
    first = service.execute_typed(typed)
    assert first["plan"]["strategy"] == "memory_lazy_keyset"
    assert first["page"]["mode"] == "keyset_live"
    assert [column["name"] for column in first["columns"]] == ["custId", "region"]
    assert counter["limits"][-1] == (21, 0)
    # The physical PK travelled through the semantic rename (custId) as the
    # ordering tiebreaker; no physical column name leaks into the response.
    assert all(set(row) == {"custId", "region"} for row in first["rows"])
    second = service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]),
                           session_kind="ontology", session_id="session-1")
    assert second["page"]["hasMore"] is True


def test_ontology_typed_wrapper_without_mapped_pk_stays_materialized(events):
    typed = build_typed_request(
        _semantic_plan({"region": "region"}),
        request={}, session_kind="ontology", session_id="session-1")
    service = _typed_service_over_leaf(events, typed)
    response = service.execute_typed(typed)
    assert response["plan"]["strategy"] == "memory_source_pushdown"
    assert response["totalRows"] == 500
    assert response["page"].get("mode") is None


def test_derived_unique_key_not_propagated_through_joins_or_aggregates(events):
    joined = parse("from events as events | join events as other "
                   "on events.id = other.id | fields events.id")
    from app.data_channel.query.contracts import DerivedSourceSpec
    from app.data_channel.query.derived import derived_relation
    spec = DerivedSourceSpec(alias="j", source="events", pipeline=joined, params={})
    relation = derived_relation("j", {"events": events["relation"],
                                      "other": events["relation"]}, spec,
                                bind_prefix="dv0")
    assert relation.state.get("uniqueKey") is None
    aggregated = parse("from events as events | stats count() as n by events.region")
    agg = DerivedSourceSpec(alias="g", source="events", pipeline=aggregated, params={})
    relation = derived_relation("g", {"events": events["relation"]}, agg,
                                bind_prefix="dv1")
    assert relation.state.get("uniqueKey") is None
    # Projection/filter wrappers keep the renamed key.
    wrapper = parse('from events as events | where events.region = "north" '
                    "| fields events.id as custId")
    wrapped = DerivedSourceSpec(alias="w", source="events", pipeline=wrapper, params={})
    relation = derived_relation("w", {"events": events["relation"]}, wrapped,
                                bind_prefix="dv2")
    assert relation.state.get("uniqueKey") == ["custId"]


def test_concurrent_same_token_fetch_serves_exactly_once(events):
    service = make_service({"events": events["relation"]})
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    token = first["page"]["cursor"]
    barrier = threading.Barrier(2)

    def fetch():
        barrier.wait()
        try:
            return ("ok", service.fetch(TextQueryCursorRequest(cursor=token)))
        except QueryError as exc:
            return ("err", exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(fetch) for _ in range(2)]
        outcomes = [item.result() for item in results]
    assert sorted(kind for kind, _ in outcomes) == ["err", "ok"]
    error = next(value for kind, value in outcomes if kind == "err")
    assert (error.status, error.code) == (400, "invalid_cursor")
    handle = REGISTRY.handles[first["queryId"]]
    assert handle.continuation.served == 40
    assert handle.continuation.pages == 2


def test_done_handle_rejects_further_fetches(events):
    service = make_service({"events": events["relation"]})
    pages = page_all(service, service.execute(request(
        "from events as events | fields events.id | limit 30", page_size=20)))
    last_cursor = None
    for page in pages:
        last_cursor = page["page"]["cursor"]
    handle = REGISTRY.handles[pages[-1]["queryId"]]
    assert handle.continuation.done
    # No cursor was issued after completion; replaying any historical cursor
    # is refused.
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=pages[0]["page"]["cursor"]
                                              or last_cursor))
    assert exc.value.code == "invalid_cursor"


# -- cross-request shared logical-query ledger -----------------------------------

def test_execution_ledger_shared_across_services(events):
    # The ledger lives on the handle: a NEW service instance (one HTTP fetch
    # request) continues the same cumulative totals instead of resetting.
    limits = QueryLimits(execution_total_rows_limit=45)
    service_a = make_service({"events": events["relation"]}, limits=limits)
    first = service_a.execute(request("from events as events | fields events.id",
                                      page_size=20))
    handle = REGISTRY.handles[first["queryId"]]
    ledger = handle.execution_budget
    assert ledger.snapshot()["totals"]["rows"] >= 21
    service_b = make_service({"events": events["relation"]}, limits=limits)
    assert service_b.execution_budget is None
    second = service_b.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert len(second["rows"]) == 20
    assert service_b.execution_budget is ledger
    assert ledger.snapshot()["totals"]["rows"] >= 42
    # Cumulative, never reset: the third page crosses the whole-query total.
    with pytest.raises(QueryError) as exc:
        service_b.fetch(TextQueryCursorRequest(cursor=second["page"]["cursor"]))
    assert exc.value.code == "query_budget_exceeded"
    assert exc.value.extra["budget"] == "execution_total_rows_limit"
    assert exc.value.extra["layer"] == "logical_query"


def test_cancel_during_page_never_publishes(events):
    service = make_service({"events": events["relation"]})
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    query_id = first["queryId"]
    raw = events["relation"]
    cancelled = {"done": False}

    def cancelling_batches(statement, params, batch_size):
        for batch in raw.batches(statement, params, batch_size):
            if not cancelled["done"]:
                cancelled["done"] = True
                REGISTRY.cancel(query_id, "owner-1")
            yield batch

    wrapped = {"events": replace(raw, batches=cancelling_batches)}
    service.relations = dict(wrapped)
    service._resolve = lambda plan, **_: setattr(service, "relations", dict(wrapped))
    with pytest.raises(QueryError) as exc:
        service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    # The page-ticket interrupt fires on the cancelled fetch: the page is
    # refused mid-stream and the handle is gone — never a partial success.
    assert (exc.value.status, exc.value.code) == (409, "query_cancelled")
    assert REGISTRY.get(query_id) is None


def test_page_tickets_share_the_user_concurrency_quota(events):
    limits = QueryLimits(memory_max_handles_per_user=1)
    service = make_service({"events": events["relation"]}, limits=limits)
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    # Positive: quota=1 with one stored handle still pages its own logical
    # query — a stored result is never double counted as a concurrency slot.
    second = service.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert len(second["rows"]) == 20
    # A SECOND concurrent execution ticket for the same user is refused.
    ticket = REGISTRY.open_page_ticket(first["queryId"], "owner-1", limits)
    try:
        with pytest.raises(QueryError) as exc:
            REGISTRY.open_page_ticket(first["queryId"], "owner-1", limits)
        assert exc.value.status == 429
        assert exc.value.extra["quota"] == "max_handles_per_user"
    finally:
        REGISTRY.close_ticket(ticket.id)


# -- count(field) semantic execution ----------------------------------------------

def test_semantic_count_field_counts_non_null_only(events):
    from app.data_channel.query.contracts import TypedQueryRequest
    plan = {
        "sources": [{"alias": "ev", "binding": {"kind": "integrated", "assetId": "a1"},
                     "propertyMap": {"id": "id", "region": "region"},
                     "declaredTypes": {"id": "integer", "region": "string"}}],
        "sourceFilters": [], "joins": [], "where": [],
        "select": [],
        "groupBy": [],
        "aggregates": [{"fn": "count", "as": "rows"},
                       {"fn": "count", "source": "ev", "property": "region", "as": "noted"}],
        "distinct": False, "orderBy": [], "pageSize": 20,
        "releases": {}, "citations": [],
    }
    typed = build_typed_request(plan, request={}, session_kind="ontology",
                                session_id="session-1")
    service = _typed_service_over_leaf(events, typed)
    response = service.execute_typed(typed)
    row = response["rows"][0]
    nulls = sum(1 for index in range(1, 501) if index % 7 == 0)
    assert row["rows"] == "500" and row["noted"] == str(500 - nulls)


# -- whole-logical-query time budget ---------------------------------------------

def test_logical_statement_budget_not_widened_across_services(events):
    # statement_timeout_ms is the ORIGINAL logical query's whole-query budget;
    # later pages arriving on fresh HTTP services must not widen it even when
    # continuation_total_timeout_ms is far larger. Time is advanced on the
    # persisted ledger instead of sleeping.
    limits = QueryLimits(statement_timeout_ms=800,
                         continuation_total_timeout_ms=300000,
                         continuation_page_timeout_ms=60000)
    service = make_service({"events": events["relation"]}, limits=limits)
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    handle = REGISTRY.handles[first["queryId"]]
    # Advance logical time past the whole-query statement budget.
    handle.execution_budget.started -= 1.0
    fresh = make_service({"events": events["relation"]}, limits=limits)
    with pytest.raises(QueryError) as exc:
        fresh.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert exc.value.status == 422
    assert exc.value.code == "query_budget_exceeded"
    assert exc.value.extra["layer"] == "logical_query"
    assert exc.value.extra["budget"] == "statement_timeout_ms"
    assert exc.value.extra["released"] == ["result_handle"]
    assert REGISTRY.get(first["queryId"]) is None


def test_logical_budget_still_positive_allows_narrowed_page(events):
    # Within the original logical budget the page still runs; the persisted
    # ledger is shared so the remaining logical wallclock only narrows it.
    limits = QueryLimits(statement_timeout_ms=60000,
                         continuation_total_timeout_ms=300000,
                         continuation_page_timeout_ms=30000)
    service = make_service({"events": events["relation"]}, limits=limits)
    first = service.execute(request("from events as events | fields events.id",
                                    page_size=20))
    handle = REGISTRY.handles[first["queryId"]]
    handle.execution_budget.started -= 10.0  # ~50s of the 60s budget consumed
    fresh = make_service({"events": events["relation"]}, limits=limits)
    second = fresh.fetch(TextQueryCursorRequest(cursor=first["page"]["cursor"]))
    assert len(second["rows"]) == 20
    assert fresh.execution_budget is handle.execution_budget
