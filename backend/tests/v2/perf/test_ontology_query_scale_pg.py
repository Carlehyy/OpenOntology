"""Opt-in small real-PG assistant SQL contracts, NOT full-chain acceptance.

These tests seed a v0 release and 6007 synthetic FO rows directly. They validate
real PostgreSQL numeric/SQL/resource behavior but do not exercise dbx registration,
mapping, Trial/Promote, sentinel/LLM/UI, or the 2.3M/10M-row acceptance workloads.
Run only in an immutable sanitized runtime copy against the owned fixture stack.
"""
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.database import Base
from app.ontologies.agent_query.read_port import resolve_ontology_relation
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.agent_runtime.toolkit import ToolRunner
from app.ontologies.formal_modeling.models import ObjectInstance, ObjectType
from app.ontologies.projects.models import OntologyProject
from app.ontologies.release_context import create_initial_release

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRATED_ONTOLOGY_PG_E2E") != "1",
                                reason="explicit owned real PostgreSQL opt-in required")


def _owned_url():
    raw = os.environ.get("INTEGRATED_QUERY_PLATFORM_DSN")
    if not raw:
        pytest.fail("INTEGRATED_QUERY_PLATFORM_DSN required; no default/production fallback")
    url = sa.engine.make_url(raw)
    if (url.get_backend_name() != "postgresql" or url.host != "127.0.0.1"
            or url.port != 45432 or url.database != "integrated_metadata"):
        pytest.fail("DSN must point to the explicitly owned loopback E2E metadata PostgreSQL")
    return raw


@pytest.fixture
def pg_ontology():
    token = uuid.uuid4().hex
    schema = "assistant_query_" + token
    root = sa.create_engine(_owned_url(), hide_parameters=True)
    private = sa.create_engine(_owned_url(), hide_parameters=True,
                               connect_args={"options": "-csearch_path=" + schema})
    created = False
    try:
        with root.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            created = True
        # Private schema DDL is not migration/registration acceptance evidence.
        Base.metadata.create_all(private)
        with Session(private, expire_on_commit=False) as db:
            user = User(id=str(uuid.uuid4()), username="assistant_" + token,
                        email=token + "@example.test", role="admin", is_active=True,
                        password_hash="not-a-login-fixture")
            db.add(user)
            db.flush()
            project = OntologyProject(id=str(uuid.uuid4()), name="PG assistant fixture",
                                      domain="synthetic", created_by=user.id)
            db.add(project)
            db.flush()
            properties = [{"id": "amount-p", "name": "amount", "type": "decimal"},
                          {"id": "bucket-p", "name": "bucket", "type": "string"},
                          {"id": "derived-p", "name": "derived", "type": "number", "source": "computed"}]
            object_type = ObjectType(id=str(uuid.uuid4()), ontology_id=project.id,
                                     name="Order", display_name="合成订单", properties=properties)
            db.add(object_type)
            db.flush()
            profile = AgentProfile(ontology_id=project.id, enabled=True, max_rows_per_query=2)
            db.add(profile)
            release = create_initial_release(db, project, snapshot={"objectTypes": [
                {"id": object_type.id, "name": "Order", "displayName": "合成订单", "properties": properties}],
                "linkTypes": [], "actions": [], "functions": []}, created_by=user.id)
            db.flush()
            db.execute(sa.insert(ObjectInstance), [
                {"id": "fixture-row-" + str(index), "ontology_id": project.id,
                 "ontology_release_id": release.id, "object_type_id": object_type.id,
                 "properties": {"amount": "9007199254740993.01" if index == 0 else "0.01",
                                "bucket": str(index % 2)}, "computed": {"derived": index}}
                for index in range(6007)])
            db.commit()
            _, _, scope = build_scope(db, project.id, release_id=release.id)
            yield SimpleNamespace(db=db, engine=private, user=user, project=project,
                                  release=release, profile=profile, object_type=object_type,
                                  runner=ToolRunner(db, scope))
    finally:
        private.dispose()
        # Exact UUID-owned target only; never drop public/shared tables or data.
        assert schema == "assistant_query_" + token and uuid.UUID(token).hex == token
        try:
            if created:
                with root.begin() as conn:
                    conn.execute(sa.schema.DropSchema(schema, cascade=True))
        finally:
            root.dispose()


def test_real_pg_assistant_sql_complete_counts_search_and_decimal(pg_ontology):
    f = pg_ontology
    count = f.runner.run("aggregate_objects", {"object_type": "Order", "metric": "count"})
    assert count["value"] == count["matched"] == count["scanned"] == 6007
    assert count["exact"] is True and count["completeness"] == "complete"
    total = f.runner.run("aggregate_objects", {"object_type": "Order", "metric": "sum",
                                              "metric_property": "amount"})
    assert total["value"] == "9007199254741053.07"
    page = f.runner.run("search_objects", {"object_type": "Order", "limit": 200})
    assert page["total"] == 6007 and page["returned"] == 2
    assert page["truncated"] is True and page["partial"] is False and page["exact"] is True
    grouped = f.runner.run("aggregate_objects", {"object_type": "Order", "metric": "count",
                                                "group_by": "bucket"})
    assert grouped["groups"] == [{"group": "0", "value": 3004, "count": 3004},
                                  {"group": "1", "value": 3003, "count": 3003}]
    assert grouped["scanned"] == grouped["matched"] == 6007
    empty = f.runner.run("aggregate_objects", {"object_type": "Order", "metric": "count",
        "group_by": "bucket", "filters": [{"property": "bucket", "op": "eq", "value": "absent"}]})
    assert empty["scanned"] == 6007 and empty["matched"] == 0 and empty["groups"] == []
    derived = f.runner.run("aggregate_objects", {"object_type": "Order", "metric": "sum",
                                                "metric_property": "derived"})
    assert derived["value"] == 6006 * 6007 // 2
    oracle = f.db.execute(sa.select(sa.func.count(), sa.func.sum(sa.cast(
        ObjectInstance.properties["amount"].as_string(), sa.Numeric())))).one()
    assert oracle == (6007, Decimal("9007199254741053.07"))


def test_real_pg_read_port_numeric_lock_and_authorization(pg_ontology):
    f = pg_ontology
    binding = SimpleNamespace(ontology_id=f.project.id, release_id=f.release.id,
                              object_type_id=f.object_type.id)
    f.profile.allowed_properties = {f.object_type.id: ["amount"]}
    f.db.commit()
    f.db.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    relation = resolve_ontology_relation(f.db, f.user, "orders", binding, None,
        column_spec_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        relation_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    assert set(relation.columns) == {"_instance_id", "amount"}
    assert relation.state["maxPageSize"] == 2
    exact = f.db.scalar(sa.select(sa.func.sum(relation.table.c.amount)))
    assert exact == Decimal("9007199254741053.07")
    relation.recheck()
    # A concurrent profile edit cannot race a pinned authorized result.
    with f.engine.begin() as concurrent:
        concurrent.execute(sa.text("SET LOCAL lock_timeout = '20ms'"))
        with pytest.raises(DBAPIError):
            concurrent.execute(sa.update(AgentProfile).where(
                AgentProfile.id == f.profile.id).values(enabled=False))
        concurrent.rollback()
    f.db.rollback()
    f.profile.enabled = False
    f.db.commit()
    with pytest.raises(HTTPException) as excinfo:
        resolve_ontology_relation(f.db, f.user, "orders", binding, None,
            column_spec_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            relation_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "profile_disabled"


@pytest.mark.parametrize("declared,invalid", [
    ("integer", "garbage"), ("integer", "NaN"), ("integer", "Infinity"),
    ("integer", "1.2"), ("integer", True), ("decimal", "invalid"),
    ("boolean", "truthy"), ("boolean", 1), ("boolean", {}),
])
def test_real_pg_read_port_rejects_invalid_types_beyond_first_page(pg_ontology, declared, invalid):
    f = pg_ontology
    properties = [*f.release.snapshot_formal["objectTypes"][0]["properties"],
                  {"id": "integer-p", "name": "integer_value", "type": declared}]
    snapshot = dict(f.release.snapshot_formal)
    snapshot["objectTypes"] = [{**snapshot["objectTypes"][0], "properties": properties}]
    f.release.snapshot_formal = snapshot
    # Last-id value is not on the two-row output page. The full-scope SQL gate
    # must reject even a COUNT that would not project integer_value at all.
    f.db.add(ObjectInstance(id="zz-invalid-row", ontology_id=f.project.id,
        ontology_release_id=f.release.id, object_type_id=f.object_type.id,
        created_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        properties={"amount": None, "integer_value": invalid}))
    f.db.commit()
    binding = SimpleNamespace(ontology_id=f.project.id, release_id=f.release.id,
                              object_type_id=f.object_type.id)
    with pytest.raises(HTTPException) as excinfo:
        resolve_ontology_relation(f.db, f.user, "orders", binding, None,
            column_spec_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            relation_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "invalid_ontology_typed_value"
    _, _, scope = build_scope(f.db, f.project.id, release_id=f.release.id)
    runner = ToolRunner(f.db, scope)
    empty_filter = [{"property": "bucket", "op": "eq", "value": "absent"}]
    # Direct assistant tools share the same full authorized input rule, including
    # zero matches and COUNT that does not project the malformed property.
    for name, args in [
        ("search_objects", {}),
        ("search_objects", {"filters": empty_filter}),
        ("aggregate_objects", {"metric": "count"}),
        ("aggregate_objects", {"metric": "sum", "metric_property": "amount"}),
        ("aggregate_objects", {"metric": "count", "group_by": "bucket"}),
        ("aggregate_objects", {"metric": "count", "group_by": "bucket", "filters": empty_filter}),
        ("search_objects", {"filters": [{"property": "integer_value", "op": "eq",
                                         "value": True if declared == "boolean" else 1}]}),
    ]:
        with pytest.raises(ToolError, match="invalid_typed_value"):
            runner.run(name, {"object_type": "Order", **args})
    # Invalid fields that are unauthorized do not leak via a type-error oracle.
    f.profile.allowed_properties = {f.object_type.id: ["amount"]}
    f.db.commit()
    relation = resolve_ontology_relation(f.db, f.user, "orders", binding, None,
        column_spec_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        relation_factory=lambda **kwargs: SimpleNamespace(**kwargs))
    assert f.db.scalar(sa.select(sa.func.count()).select_from(relation.table)) == 6008
    _, _, scope = build_scope(f.db, f.project.id, release_id=f.release.id)
    runner = ToolRunner(f.db, scope)
    assert runner.run("aggregate_objects", {"object_type": "Order", "metric": "count"})["value"] == 6008
    empty = runner.run("search_objects", {"object_type": "Order", "filters": [
        {"property": "amount", "op": "eq", "value": "-1"}]})
    assert empty["total"] == empty["returned"] == 0 and empty["items"] == []
    assert empty["exact"] is True and empty["completeness"] == "complete"


def test_real_pg_timeout_is_contained_and_does_not_commit(pg_ontology):
    f = pg_ontology
    # Keep an uncommitted caller change; the toolkit must not commit it.
    f.profile.max_steps = 17
    f.db.flush()
    with pytest.raises(ToolError, match="query_execution_rejected"):
        with f.runner._sql_read_budget():
            f.db.execute(sa.text("SET LOCAL statement_timeout = '1ms'"))
            f.db.execute(sa.text("SELECT pg_sleep(0.05)"))
    assert f.db.scalar(sa.select(sa.literal(1))) == 1  # caller transaction still usable
    f.db.rollback()
    assert f.db.get(AgentProfile, f.profile.id).max_steps != 17


def test_real_pg_temp_limit_zero_is_preserved_and_spill_rejected(pg_ontology):
    f = pg_ontology
    if f.db.scalar(sa.select(sa.func.current_setting("is_superuser"))) != "on":
        pytest.skip("fixture role cannot set SUSET temp_file_limit for the zero-spill probe")
    f.db.execute(sa.text("SET LOCAL temp_file_limit = '0'"))
    with pytest.raises(ToolError, match="query_execution_rejected"):
        with f.runner._sql_read_budget():
            assert f.db.scalar(sa.select(sa.func.current_setting("temp_file_limit"))) == "0"
            f.db.execute(sa.text("SET LOCAL work_mem = '64kB'"))
            f.db.execute(sa.text("SELECT sum(length(payload)) FROM ("
                "SELECT repeat(md5(i::text), 64) AS payload "
                "FROM generate_series(1, 2000) AS s(i) ORDER BY payload) AS ordered"))
    assert f.db.scalar(sa.select(sa.literal(1))) == 1
