"""Deterministic unit/contracts; not evidence of million-row/real UI acceptance."""
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.auth.models import RoleMenuPermission
from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query.read_port import resolve_ontology_relation
from app.ontologies.agent_query.service import UnifiedQueryService
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.agent_runtime.read_queries import (
    encode_scalar, invalid_typed_property, typed_property,
)
from app.ontologies.agent_runtime.toolkit import ToolRunner
from app.ontologies.formal_modeling.models import ObjectInstance, ObjectType
from app.ontologies.projects.models import OntologyProject
from app.ontologies.release_context import create_initial_release


@pytest.fixture
def query_scope(db, admin_user):
    project = OntologyProject(id="scale-ontology", name="Scale", domain="test",
                              created_by=admin_user.id)
    db.add(project)
    db.flush()
    properties = [{"id": "p1", "name": "amount", "type": "decimal"},
                  {"id": "p2", "name": "bucket", "type": "string"},
                  {"id": "p3", "name": "derived", "type": "number", "source": "computed"}]
    object_type = ObjectType(id="scale-order", ontology_id=project.id, name="Order",
                             display_name="订单", properties=properties)
    db.add(object_type)
    profile = AgentProfile(ontology_id=project.id, enabled=True, max_rows_per_query=2)
    db.add(profile)
    release = create_initial_release(db, project, snapshot={"objectTypes": [
        {"id": object_type.id, "name": "Order", "displayName": "订单", "properties": properties}],
        "linkTypes": [], "actions": [], "functions": []}, created_by=admin_user.id)
    db.commit()
    _, _, scope = build_scope(db, project.id, release_id=release.id)
    return project, release, profile, scope


def _rows(db, project, release, values):
    for index, value in enumerate(values):
        db.add(ObjectInstance(id=f"scale-row-{index}", ontology_id=project.id,
                              ontology_release_id=release.id, object_type_id="scale-order",
                              properties={"amount": value, "bucket": str(index % 2)},
                              computed={"derived": index}))
    db.commit()


def test_lossless_numeric_encoding():
    assert encode_scalar(Decimal("9007199254740993.0123456789")) == "9007199254740993.0123456789"
    assert encode_scalar(9007199254740993) == "9007199254740993"
    assert encode_scalar(Decimal("350")) == 350
    assert encode_scalar(Decimal("0.1")) == "0.1"
    assert encode_scalar({"nested": [9007199254740993, Decimal("0.1")]}) == {
        "nested": ["9007199254740993", "0.1"]}
    from app.ontologies.agent_runtime.read_queries import decimal_add
    assert str(decimal_add(Decimal("90071992547409930123456789.01"), Decimal("0.02"))) == "90071992547409930123456789.03"


def test_pg_numeric_expression_is_numeric_not_float():
    expression = typed_property({"type": "decimal"}, "amount")
    sql = str(select(expression).compile(dialect=postgresql.dialect()))
    assert "NUMERIC" in sql and "FLOAT" not in sql
    assert "CASE" in sql  # malformed legacy numeric values cannot break CAST
    computed = str(select(typed_property({"type": "number", "source": "computed"},
                                         "derived")).compile(dialect=postgresql.dialect()))
    assert "computed" in computed


@pytest.mark.parametrize("declared", ["decimal", "integer", "boolean"])
def test_pg_invalid_type_gate_is_full_scope_predicate(declared):
    condition = invalid_typed_property({"type": declared, "source": "computed"}, "test_value")
    sql = str(select(ObjectInstance.id).where(condition).exists().compile(
        dialect=postgresql.dialect()))
    assert "EXISTS" in sql and "computed" in sql and "IS NOT NULL" in sql
    assert "LIMIT" not in sql
    if declared == "integer":
        assert "trunc" in sql and "CASE" in sql
    elif declared == "boolean":
        assert "json_typeof" in sql
    else:
        assert " ~ " in sql


def test_search_never_truncates_declared_numeric_strings(db, query_scope, monkeypatch):
    from app.ontologies.agent_runtime import toolkit
    project, release, _, scope = query_scope
    exact = "9" * 100
    _rows(db, project, release, [exact])
    original = toolkit.runtime_limit
    monkeypatch.setattr(toolkit, "runtime_limit", lambda key: 5 if key == "value_trunc" else original(key))
    result = ToolRunner(db, scope).run("search_objects", {"object_type": "Order"})
    assert result["items"][0]["properties"]["amount"] == exact


def test_non_pg_aggregate_is_lossless_and_complete(db, query_scope):
    project, release, _, scope = query_scope
    _rows(db, project, release, ["0.1", "0.2", "9007199254740993"])
    result = ToolRunner(db, scope).run("aggregate_objects", {
        "object_type": "Order", "metric": "sum", "metric_property": "amount"})
    assert result["value"] == "9007199254740993.3"
    assert result["matched"] == 3 and result["partial"] is False
    assert result["exact"] is True and result["completeness"] == "complete"


def test_non_pg_budget_refuses_instead_of_partial_count(db, query_scope, monkeypatch):
    from app.ontologies.agent_runtime import toolkit
    project, release, _, scope = query_scope
    _rows(db, project, release, [1, 2, 3])
    original = toolkit.runtime_limit
    monkeypatch.setattr(toolkit, "runtime_limit",
                        lambda key: 2 if key in {"search_scan_cap", "aggregate_scan_cap"}
                        else original(key))
    runner = ToolRunner(db, scope)
    with pytest.raises(ToolError, match="aggregate_budget_exceeded"):
        runner.run("aggregate_objects", {"object_type": "Order", "metric": "count"})
    with pytest.raises(ToolError, match="search_budget_exceeded"):
        runner.run("search_objects", {"object_type": "Order", "filters": [
            {"property": "amount", "op": "eq", "value": 3}]})


def test_structured_sql_aggregate_input_not_limited_to_5000(db, admin_user, query_scope, monkeypatch):
    from app.ontologies.agent_query import service
    project, release, _, _ = query_scope
    _rows(db, project, release, [1, 2, 3])
    monkeypatch.setattr(service, "SOURCE_ROW_BUDGET", 2)
    request = QS.QueryRequest(sources=[QS.QuerySource(alias="a", ontology_id=project.id,
        release_id=release.id, object_type_id="scale-order")],
        aggregates=[QS.AggregateItem(fn="count", as_="total")])
    result = UnifiedQueryService(db, admin_user, request, session_kind="direct",
                                 session_id=admin_user.id).execute()
    assert result["rows"] == [{"total": 3}]
    assert result["inputCounts"] == {"a": 3}
    assert result["exact"] is True and result["completeness"] == "complete"


@pytest.mark.parametrize("empty", [False, True])
def test_pg_grouped_sql_retains_full_counts_and_empty_input_evidence(
        db, query_scope, monkeypatch, empty):
    _, _, _, scope = query_scope
    runner = ToolRunner(db, scope)
    statements = []
    rows = ([(None, None, None, None, None, 2300000, False)] if empty else
            [(str(index), 100, 100, 2300000, 100000, 2300000, False) for index in range(51)])

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: rows)

    # Compile-only contract: real PostgreSQL execution is separate acceptance.
    monkeypatch.setattr(runner, "_sql_read_budget", nullcontext)
    monkeypatch.setattr(db, "execute", execute)
    result = runner._sql_aggregate_objects(scope.require_object_type("Order"),
                                           "count", None, "bucket", [])
    assert result["scanned"] == 2300000
    assert result["exact"] is True and result["completeness"] == "complete"
    assert result["matched"] == (0 if empty else 2300000)
    assert result["totalGroups"] == (0 if empty else 100000)
    assert result["truncated"] is (not empty)
    assert len(result["groups"]) == (0 if empty else 50)
    assert len(statements) == 1
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "LEFT OUTER JOIN" in sql and "OVER ()" in sql
    assert "GROUP BY" in sql and 51 in compiled.params.values()
    assert "ontology_release_id" in sql and "ORDER BY" in sql
    assert "EXISTS" in sql and " AS invalid" in sql
    statements.clear()
    rows[:] = [(*rows[0][:-1], True)]
    with pytest.raises(ToolError, match="invalid_typed_value"):
        runner._sql_aggregate_objects(scope.require_object_type("Order"),
                                      "count", None, "bucket", [])
    assert len(statements) == 1


def test_pg_search_empty_page_keeps_same_statement_full_scope_type_gate(db, query_scope, monkeypatch):
    _, _, profile, scope = query_scope
    profile.allowed_properties = {"scale-order": ["amount", "bucket"]}
    runner = ToolRunner(db, scope)
    statements = []
    invalid = False

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(all=lambda: [(None, None, invalid)])

    monkeypatch.setattr(runner, "_sql_read_budget", nullcontext)
    # Compile-only PG routing without changing the SQLite fixture's compiler.
    monkeypatch.setattr(db, "get_bind", lambda: SimpleNamespace(dialect=postgresql.dialect()))
    monkeypatch.setattr(db, "execute", execute)
    result = runner.run("search_objects", {"object_type": "Order", "keyword": "missing"})
    assert result["total"] == result["returned"] == 0 and result["items"] == []
    assert result["exact"] is True and result["completeness"] == "complete"
    assert len(statements) == 1
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "LEFT OUTER JOIN" in sql and "OVER ()" in sql and "EXISTS" in sql
    gate = runner._sql_invalid_typed_value(scope.require_object_type("Order"))
    compiled_gate = select(gate).compile(dialect=postgresql.dialect())
    assert "LIMIT" not in str(compiled_gate) and "WHERE fo_object_instances.ontology_id" in str(compiled_gate)
    assert "amount" in compiled_gate.params.values() and "derived" not in compiled_gate.params.values()
    invalid = True
    statements.clear()
    with pytest.raises(ToolError, match="invalid_typed_value"):
        runner.run("search_objects", {"object_type": "Order", "keyword": "missing"})
    assert len(statements) == 1


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    source_type: str | None = None


def test_read_port_release_permission_and_no_transaction_commit(db, admin_user, query_scope, monkeypatch):
    project, release, profile, _ = query_scope
    _rows(db, project, release, [10])
    profile.allowed_properties = {"scale-order": ["bucket", "derived"]}
    db.commit()
    binding = SimpleNamespace(ontology_id=project.id, release_id=release.id,
                              object_type_id="scale-order")
    def no_commit(*args, **kwargs):
        raise AssertionError("read port must not commit")
    monkeypatch.setattr(db, "commit", no_commit)
    relation = resolve_ontology_relation(db, admin_user, "orders", binding, None,
        column_spec_factory=Column, relation_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    assert set(relation.columns) == {"_instance_id", "bucket", "derived"}
    assert relation.platform is True and relation.state["versionId"] == release.id
    assert relation.state["maxPageSize"] == 2
    assert db.execute(select(relation.table)).one()._mapping["bucket"] == "0"
    relation.recheck()
    profile.allowed_properties = {"scale-order": []}
    db.flush()
    with pytest.raises(Exception) as excinfo:
        relation.recheck()
    assert excinfo.value.detail["code"] == "query_authorization_changed"


def test_read_port_requires_agent_menu_before_scope_and_on_recheck(db, admin_user, query_scope, monkeypatch):
    from app.ontologies.agent_query import read_port
    project, release, profile, _ = query_scope
    permissions = RoleMenuPermission(role="custom", menu_keys=["data.structured"])
    db.add(permissions)
    profile.enabled = False
    db.flush()
    user = SimpleNamespace(id=admin_user.id, role="custom")
    binding = SimpleNamespace(ontology_id=project.id, release_id=release.id,
                              object_type_id="scale-order")

    def forbidden_scope_lookup(*args, **kwargs):
        raise AssertionError("menu denial must precede ontology/profile lookup")

    with monkeypatch.context() as denied:
        denied.setattr(read_port, "require_ontology_access", forbidden_scope_lookup)
        with pytest.raises(HTTPException) as excinfo:
            resolve_ontology_relation(db, user, "a", binding, None,
                column_spec_factory=Column, relation_factory=lambda **kwargs: kwargs)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "MENU_ACCESS_DENIED"
    assert excinfo.value.detail["menu_key"] == "agent"
    profile.enabled = True
    for role in ("viewer", "custom"):
        permissions = db.get(RoleMenuPermission, role)
        if permissions is None:
            permissions = RoleMenuPermission(role=role)
            db.add(permissions)
        permissions.menu_keys = ["data.structured", "agent"]
        db.flush()
        user = SimpleNamespace(id=admin_user.id, role=role)
        relation = resolve_ontology_relation(db, user, "a", binding, None,
            column_spec_factory=Column, relation_factory=lambda **kwargs: SimpleNamespace(**kwargs))
        relation.recheck()
        permissions.menu_keys = ["data.structured"]
        db.flush()
        with pytest.raises(HTTPException) as excinfo:
            relation.recheck()
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail["code"] == "MENU_ACCESS_DENIED"
        with pytest.raises(HTTPException) as excinfo:
            resolve_ontology_relation(db, user, "a", binding, None,
                column_spec_factory=Column, relation_factory=lambda **kwargs: kwargs)
        assert excinfo.value.detail["menu_key"] == "agent"


def test_tool_field_auth_covers_filter_aggregate_keyword_and_citation(db, query_scope):
    project, release, profile, scope = query_scope
    _rows(db, project, release, [987654321])
    profile.allowed_properties = {"scale-order": ["bucket"]}
    db.commit()
    _, _, scope = build_scope(db, project.id, release_id=release.id)
    runner = ToolRunner(db, scope)
    with pytest.raises(ToolError):
        runner.run("aggregate_objects", {"object_type": "Order", "metric": "sum",
                                          "metric_property": "amount"})
    with pytest.raises(ToolError):
        runner.run("search_objects", {"object_type": "Order", "filters": [
            {"property": "amount", "op": "eq", "value": 987654321}]})
    assert runner.run("search_objects", {"object_type": "Order", "keyword": "987654321"})["total"] == 0
    result = runner.run("search_objects", {"object_type": "Order"})
    assert result["items"][0]["properties"] == {"bucket": "0"}
    assert "987654321" not in str(runner.citations)


def test_read_port_keeps_canonical_200_row_transport_ceiling(db, admin_user, query_scope):
    project, release, profile, _ = query_scope
    profile.max_rows_per_query = 500
    db.commit()
    relation = resolve_ontology_relation(db, admin_user, "a", SimpleNamespace(
        ontology_id=project.id, release_id=release.id, object_type_id="scale-order"), None,
        column_spec_factory=Column, relation_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    assert relation.state["maxPageSize"] == 200


def test_read_port_rejects_stale_release(db, admin_user, query_scope):
    project, _, _, _ = query_scope
    binding = SimpleNamespace(ontology_id=project.id, release_id="stale",
                              object_type_id="scale-order")
    with pytest.raises(Exception) as excinfo:
        resolve_ontology_relation(db, admin_user, "a", binding, None,
            column_spec_factory=Column, relation_factory=lambda **kwargs: kwargs)
    assert excinfo.value.detail["code"] == "release_context_changed"
