"""Semantic plans must share typed execution and retain safe operator ordering."""
import pytest

from app.data_channel.query.contracts import QueryError
from app.data_channel.query.ontology_adapter import (
    OntologyQueryAdapter, _fingerprint, _response, build_typed_request,
)


def _plan():
    return {"sources": [{"alias": "customers", "binding": {"kind": "integrated", "assetId": "crm"},
                         "propertyMap": {"customerId": "customer_id", "tier": "customer_tier"},
                         "schemaFingerprint": "f" * 64,
                         "declaredTypes": {"customerId": "integer", "tier": "string"}}],
            "sourceFilters": [], "where": [], "joins": [], "select": [],
            "groupBy": [], "aggregates": [{"fn": "count", "source": None,
                                              "property": None, "as": "count"}],
            "distinct": False, "orderBy": [], "outputs": ["count"], "pageSize": 10}


def test_semantic_request_forces_memory_and_records_frozen_schema_expectation():
    plan = _plan()
    typed = build_typed_request(plan, request={"sources": []},
                                session_kind="assistant", session_id="conversation")
    assert typed.result_storage == "memory"
    assert typed.session_id == "conversation"
    assert typed.session_kind == "assistant"
    assert next(iter(typed.origin["expectations"].values()))["schemaFingerprint"] == "f" * 64
    assert len(typed.bindings) == 1
    assert typed.pipeline.commands[-1].op == "stats"


def test_source_filters_remain_inside_relation_before_outer_join():
    plan = _plan()
    plan["sourceFilters"] = [{"source": "customers", "filter": {
        "source": "customers", "property": "tier", "op": "eq", "value": "VIP"}}]
    typed = build_typed_request(plan, request={}, session_kind="direct", session_id="user")
    filtered = typed.derived[typed.pipeline.source]
    assert [command.op for command in filtered.pipeline.commands] == ["where", "fields"]
    predicate = filtered.pipeline.commands[0].args[0]
    assert predicate.args[0].name == "tier"
    assert predicate.args[1].value == "VIP"
    assert [command.op for command in typed.pipeline.commands] == ["stats"]


def test_named_grouping_columns_are_preserved_without_string_query_generation():
    plan = _plan()
    plan["select"] = [{"source": "customers", "property": "tier", "as": "customerTier"}]
    plan["groupBy"] = [{"source": "customers", "property": "tier"}]
    plan["outputs"] = ["customerTier", "count"]
    typed = build_typed_request(plan, request={}, session_kind="direct", session_id="user")
    group = typed.pipeline.commands[0].args[-1]
    assert group[0] == "group"
    assert group[1][0][0].name == "tier"
    assert group[1][0][1] == "customerTier"
    assert not hasattr(typed, "query")


def _result():
    return {"queryId": "handle", "columns": [{"name": "count", "type": "integer"}],
            "rows": [{"count": "123"}], "totalRows": 1,
            "page": {"hasMore": False, "cursor": None},
            "sourceStates": [{"asOf": "2026-10-26T00:00:00Z"}],
            "expiresAt": "2026-10-26T00:05:00Z", "consistency": "repeatable_read",
            "countsScope": "complete_snapshot", "plan": {}}


class _MemoryService:
    def __init__(self, origin):
        self.origin = origin
        self.cancelled = []
        self.fetches = []

    def memory_origin(self, token):
        return {"queryId": "handle", "origin": self.origin,
                "session": {"kind": "assistant", "id": "conversation"}}

    def fetch(self, request, **session):
        self.fetches.append((request.cursor, session))
        return _result()

    def cancel(self, query_id):
        self.cancelled.append(query_id)


def _page_adapter(monkeypatch, plan):
    service = _MemoryService({"request": {"semantic": "request"},
                              "planFingerprint": _fingerprint(plan)})
    adapter = OntologyQueryAdapter()
    monkeypatch.setattr(adapter, "_service", lambda db, user: service)
    return adapter, service


def test_page_reads_nested_origin_envelope_and_preserves_session(monkeypatch, db, admin_user):
    plan = dict(_plan(), releases={}, citations=[])
    adapter, service = _page_adapter(monkeypatch, plan)
    calls = []

    def prepare(db, user, request, *, serialized):
        calls.append((request, serialized))
        return object(), plan

    result = adapter.fetch(db, admin_user, "mem.test-cursor-token", session_kind="assistant",
                           session_id="conversation", prepare=prepare)
    assert result["rows"] == [{"count": "123"}]
    assert result["persistence"]["mode"] == "ephemeral"
    assert calls == [({"semantic": "request"}, True)]
    assert service.fetches == [("mem.test-cursor-token", {"session_kind": "assistant",
                                              "session_id": "conversation"})]
    assert service.cancelled == []
    assert not db.in_transaction()


def test_page_rejects_other_session_without_destroying_owner_handle(monkeypatch, db, admin_user):
    adapter, service = _page_adapter(monkeypatch, _plan())
    with pytest.raises(QueryError) as error:
        adapter.fetch(db, admin_user, "mem.test-cursor-token", session_kind="assistant",
                      session_id="other", prepare=lambda *args, **kwargs: pytest.fail("not authorized"))
    assert error.value.status == 404
    assert service.fetches == []
    assert service.cancelled == []


@pytest.mark.parametrize("classification", ["aggregate", "detail", "untrusted"])
def test_response_preserves_only_trusted_core_aggregate_classification(classification):
    result = dict(_result(), persistence={"mode": "ephemeral", "classification": classification})
    payload = _response(result, dict(_plan(), releases={}, citations=[]))
    assert payload["persistence"]["classification"] == (
        "aggregate" if classification == "aggregate" else "detail")


def test_page_keeps_handle_for_transient_metadata_failure(monkeypatch, db, admin_user):
    plan = dict(_plan(), releases={}, citations=[])
    adapter, service = _page_adapter(monkeypatch, plan)
    calls = 0

    def prepare(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise QueryError(503, "query_backend_unavailable", "Temporary metadata outage")
        return object(), plan

    with pytest.raises(QueryError):
        adapter.fetch(db, admin_user, "mem.test-cursor-token", session_kind="assistant",
                      session_id="conversation", prepare=prepare)
    assert service.cancelled == []
    assert adapter.fetch(db, admin_user, "mem.test-cursor-token", session_kind="assistant",
                         session_id="conversation", prepare=prepare)["rows"] == [{"count": "123"}]


@pytest.mark.parametrize("failure", ["changed", "forbidden", "source_unavailable"])
def test_page_invalidates_handle_after_context_check_fails(monkeypatch, db, admin_user, failure):
    plan = dict(_plan(), releases={}, citations=[])
    adapter, service = _page_adapter(monkeypatch, plan)

    def prepare(*args, **kwargs):
        if failure == "forbidden":
            raise QueryError(403, "property_forbidden", "Property revoked")
        if failure == "changed":
            return object(), dict(plan, pageSize=1)
        return object(), plan

    if failure == "source_unavailable":
        # Physical outage surfaced by the page liveness probe: native raises
        # HTTPException(503, SOURCE_UNAVAILABLE); the handle must die so a
        # later restore cannot resurrect the invalid delivery context.
        from fastapi import HTTPException

        def failing_fetch(request, **session):
            raise HTTPException(503, {"code": "SOURCE_UNAVAILABLE",
                                      "message": "Source is not physically reachable"})

        service.fetch = failing_fetch

    with pytest.raises(Exception) as error:
        adapter.fetch(db, admin_user, "mem.test-cursor-token", session_kind="assistant",
                      session_id="conversation", prepare=prepare)
    expected = 403 if failure == "forbidden" else 409 if failure == "changed" else 503
    assert getattr(error.value, "status_code", getattr(error.value, "status", None)) == expected
    assert service.cancelled == ["handle"]
    assert not db.in_transaction()
