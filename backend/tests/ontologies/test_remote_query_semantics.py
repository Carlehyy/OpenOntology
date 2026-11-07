"""Published semantic planning only; real source execution is a separate gate."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ontologies.agent_query.models import AgentJoinAssertion
from app.ontologies.agent_query.schemas import QueryRequest
from app.ontologies.agent_query.service import QueryError
from app.ontologies.agent_query import remote_query
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion
from app.ontologies.versions.snapshot_contract import complete_snapshot, snapshot_hash


def _release(db, user, name="CRM", *, asset="customers"):
    ontology_id, release_id = str(uuid4()), str(uuid4())
    snapshot = complete_snapshot({
        "objectTypes": [{"id": f"{asset}-type", "name": asset, "displayName": name,
                         "primaryKey": "p-key", "properties": [
                             {"id": "p-key", "name": "key", "type": "number", "required": True},
                             {"id": "p-amount", "name": "amount", "type": "number"},
                             {"id": "p-note", "name": "note", "type": "string"}]}],
        "mappings": [{"id": f"{asset}-mapping", "curatedDatasetId": f"{asset}-dataset",
                      "entityClass": asset, "targetObjectTypeId": f"{asset}-type",
                      "bindingMode": "remote", "remote": {
                          "assetId": asset, "datasetId": f"{asset}-dataset",
                          "connectionId": f"{asset}-connection", "schemaName": "sales",
                          "tableName": asset, "columns": [
                              {"name": "physical_key", "type": "integer", "nullable": False},
                              {"name": "physical_amount", "type": "decimal", "precision": 30, "scale": 4},
                              {"name": "physical_note", "type": "string"}],
                          "primaryKey": ["physical_key"], "schemaFingerprint": "f" * 64},
                      "fieldMapping": {"physical_key": "key", "physical_amount": "amount",
                                       "physical_note": "note", "__primary_key__": "physical_key"}}]})
    project = OntologyProject(id=ontology_id, name=name, domain="test", created_by=user.id)
    db.add(project)
    db.flush()
    release = OntologyVersion(id=release_id, ontology_id=ontology_id, version_number="v0",
                              node_kind="release", lifecycle_status="released", revision=0,
                              snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
                              created_by=user.id)
    profile = AgentProfile(ontology_id=ontology_id, enabled=True, max_rows_per_query=10)
    db.add_all([release, profile])
    db.flush()
    project.current_release_id = release_id
    db.commit()
    return {"ontologyId": ontology_id, "releaseId": release_id,
            "objectTypeId": f"{asset}-type"}, profile


def _request(identity, **changes):
    return QueryRequest.model_validate({"sources": [{"alias": "a", **identity}],
                                       "select": [{"source": "a", "property": "key", "as": "key"}],
                                       **changes})


def test_remote_plan_uses_frozen_binding_and_only_authorized_properties(db, admin_user):
    identity, profile = _release(db, admin_user)
    profile.allowed_properties = {identity["objectTypeId"]: ["key", "amount"]}
    db.commit()
    request = _request(identity)
    plan = remote_query.prepare_remote_plan(db, admin_user, request)
    source = plan["sources"][0]
    assert source["binding"] == {"kind": "integrated", "assetId": "customers"}
    assert source["propertyMap"] == {"key": "physical_key", "amount": "physical_amount"}
    assert source["schemaFingerprint"] == "f" * 64
    assert plan["pageSize"] == 10
    assert plan["releases"]["a"]["dataMode"] == "remote"
    assert not db.in_transaction()
    assert "physical_note" not in str(plan)


def test_remote_plan_rejects_profile_and_unmapped_field_without_opening_source(db, admin_user):
    identity, profile = _release(db, admin_user)
    profile.allowed_properties = {identity["objectTypeId"]: ["key"]}
    db.commit()
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, _request(identity, select=[{
            "source": "a", "property": "amount", "as": "amount"}]))
    assert error.value.code == "property_forbidden"
    assert not db.in_transaction()
    profile = db.query(AgentProfile).filter_by(ontology_id=identity["ontologyId"]).one()
    profile.enabled = False
    db.commit()
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, _request(identity))
    assert error.value.code == "profile_disabled"
    assert not db.in_transaction()


def _cross_query(db, user):
    left, _ = _release(db, user)
    right, _ = _release(db, user, "ERP", asset="orders")
    assertion = AgentJoinAssertion(
        created_by=user.id, left_ontology_id=left["ontologyId"],
        left_object_type_id=left["objectTypeId"], left_property="key",
        right_ontology_id=right["ontologyId"], right_object_type_id=right["objectTypeId"],
        right_property="key", key_type="number", business_meaning="Published customer business key")
    db.add(assertion)
    db.commit()
    nested = {"sources": [{"alias": "o", **right}],
              "select": [{"source": "o", "property": "key", "as": "customerKey"}],
              "groupBy": [{"source": "o", "property": "key"}],
              "aggregates": [{"fn": "sum", "source": "o", "property": "amount", "as": "total"}]}
    payload = {"sources": [{"alias": "c", **left}, {"alias": "totals", "subquery": nested}],
               "joins": [{"left": "c", "right": "totals", "type": "left", "on": {
                   "kind": "explicit_key", "leftProperty": "key", "rightProperty": "customerKey",
                   "keyType": "number", "cardinality": "1:1", "businessMeaning": "Customer totals",
                   "evidenceRefs": [f"assertion:{assertion.id}"]}}],
               "select": [{"source": "c", "property": "key", "as": "customerKey"},
                          {"source": "totals", "property": "total", "as": "total"}]}
    return payload, assertion


def test_derived_group_key_retains_published_lineage_for_join_evidence(db, admin_user):
    payload, _ = _cross_query(db, admin_user)
    plan = remote_query.prepare_remote_plan(db, admin_user, QueryRequest.model_validate(payload))
    assert plan["joins"][0]["rightProperty"] == "customerKey"
    assert plan["joins"][0]["evidence"]["categories"] == ["user_asserted"]
    assert plan["sources"][1]["subquery"]["outputs"] == ["customerKey", "total"]
    assert not db.in_transaction()


def test_aggregate_value_cannot_masquerade_as_published_join_key(db, admin_user):
    payload, _ = _cross_query(db, admin_user)
    payload["joins"][0]["on"]["rightProperty"] = "total"
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, QueryRequest.model_validate(payload))
    assert error.value.code == "join_semantics_unverified"


def test_derived_join_rechecks_assertion_ownership(db, admin_user, editor_user):
    payload, assertion = _cross_query(db, admin_user)
    assertion.created_by = editor_user.id
    db.commit()
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, QueryRequest.model_validate(payload))
    assert error.value.code == "join_semantics_unverified"


def test_remote_source_filter_cannot_reference_another_source(db, admin_user):
    payload, _ = _cross_query(db, admin_user)
    payload["sourceFilters"] = [{"source": "c", "filter": {
        "source": "totals", "property": "total", "op": "gt", "value": 0}}]
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, QueryRequest.model_validate(payload))
    assert error.value.code == "invalid_parameter"
    assert not db.in_transaction()


def test_remote_execute_discards_handle_when_final_scope_changed(db, admin_user, monkeypatch):
    identity, _ = _release(db, admin_user)
    request = _request(identity)
    plans = iter([{"authorized": "before"}, {"authorized": "after"}])
    cancelled = []
    port = SimpleNamespace(execute=lambda *args, **kwargs: {"queryId": "memory-result"},
                           cancel=lambda db, user, query_id: cancelled.append(query_id))
    monkeypatch.setattr(remote_query, "prepare_remote_plan", lambda *args, **kwargs: next(plans))
    monkeypatch.setattr(remote_query, "_port", port)
    with pytest.raises(QueryError) as error:
        remote_query.execute_remote_query(db, admin_user, request,
                                          session_kind="direct", session_id=admin_user.id)
    assert error.value.code == "query_context_changed"
    # 漂移差异范围随错误附注返回（配置变更日志可直接查证成因）
    assert error.value.extra.get("changedScopes")
    assert error.value.extra.get("reprepareRequired") is True
    assert cancelled == ["memory-result"]
    assert not db.in_transaction()


def test_entity_class_only_remote_mapping_uses_same_binding_resolution(db, admin_user):
    identity, _ = _release(db, admin_user)
    release = db.get(OntologyVersion, identity["releaseId"])
    snapshot = dict(release.snapshot_formal)
    snapshot["mappings"] = [{key: value for key, value in snapshot["mappings"][0].items()
                             if key != "targetObjectTypeId"}]
    release.snapshot_formal = snapshot
    db.commit()
    request = _request(identity)
    assert remote_query.uses_remote_sources(db, request)
    assert remote_query.prepare_remote_plan(db, admin_user, request)["sources"][0]["binding"] == {
        "kind": "integrated", "assetId": "customers"}


def test_current_release_change_is_a_query_error_at_http_entry(client, auth_headers, ontology):
    response = client.post("/api/v2/formal/agent/query", headers=auth_headers, json={
        "sources": [{"alias": "a", "ontologyId": ontology["id"],
                     "releaseId": "not-current", "objectTypeId": "unknown"}],
        "select": [{"source": "a", "property": "key", "as": "key"}]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "release_context_changed"


def test_remote_page_size_over_cap_reports_controlled_fields(db, admin_user):
    """远程有效 cap=min(叶子 profile, 200)；越界错误带受控上限字段。"""
    identity, profile = _release(db, admin_user)
    profile.max_rows_per_query = 50
    db.commit()
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(
            db, admin_user, _request(identity, pageSize=51))
    assert (error.value.status, error.value.code) == (
        422, "invalid_parameter")
    assert error.value.extra["field"] == "pageSize"
    assert error.value.extra["requestedPageSize"] == 51
    assert error.value.extra["maxPageSize"] == 50
    assert error.value.extra["defaultPageSize"] == 50
    # 公共文案保留上限，并给出可执行建议（不夹取、不抬帽）。
    assert "50" in error.value.message
    assert "cursor" in error.value.message or "降低" in error.value.message
    assert not db.in_transaction()


def test_remote_http_page_size_over_cap_keeps_422_with_actionable_message(
        client, auth_headers, db, admin_user):
    """直连 HTTP：越帽仍 422，公共 detail 仅 code/message，文案含上限与建议。"""
    identity, profile = _release(db, admin_user)
    profile.max_rows_per_query = 100
    db.commit()
    response = client.post("/api/v2/formal/agent/query", headers=auth_headers,
                           json={"sources": [{"alias": "a", **identity}],
                                 "select": [{"source": "a", "property": "key",
                                             "as": "key"}],
                                 "pageSize": 200})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert set(detail) == {"code", "message"}
    assert detail["code"] == "invalid_parameter"
    assert "100" in detail["message"]
    assert "cursor" in detail["message"] or "降低" in detail["message"]
    assert "200" not in detail["message"]  # 不回显请求原值


def test_remote_cap_takes_minimum_of_all_nested_leaf_profiles(db, admin_user):
    payload, _ = _cross_query(db, admin_user)
    left_ontology = payload["sources"][0]["ontologyId"]
    # 右侧是派生源：其叶子 profile 也必须计入有效 cap。
    right_ontology = payload["sources"][1]["subquery"]["sources"][0][
        "ontologyId"]
    for ontology_id, cap in ((left_ontology, 200), (right_ontology, 30)):
        profile = db.query(AgentProfile).filter_by(
            ontology_id=ontology_id).one()
        profile.max_rows_per_query = cap
    db.commit()

    # 默认页长 = min(50, 所有叶子 min cap) = min(50, 30) = 30。
    default_plan = remote_query.prepare_remote_plan(
        db, admin_user, QueryRequest.model_validate(payload))
    assert default_plan["pageSize"] == 30

    payload["pageSize"] = 31
    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(
            db, admin_user, QueryRequest.model_validate(payload))
    assert (error.value.status, error.value.code) == (
        422, "invalid_parameter")
    assert error.value.extra["maxPageSize"] == 30
    assert error.value.extra["defaultPageSize"] == 30
    assert error.value.extra["requestedPageSize"] == 31

    payload["pageSize"] = 30
    plan = remote_query.prepare_remote_plan(
        db, admin_user, QueryRequest.model_validate(payload))
    assert plan["pageSize"] == 30


def test_remote_plan_accepts_count_field_semantics(db, admin_user):
    identity, profile = _release(db, admin_user, asset="notes")
    profile.allowed_properties = {identity["objectTypeId"]: ["key", "note"]}
    db.commit()
    request = _request(identity, select=[], aggregates=[
        {"fn": "count", "as": "rows"},
        {"fn": "count", "source": "a", "property": "note", "as": "noted"}])
    plan = remote_query.prepare_remote_plan(db, admin_user, request)
    assert plan["aggregates"] == [
        {"fn": "count", "source": None, "property": None, "as": "rows"},
        {"fn": "count", "source": "a", "property": "note", "as": "noted"}]
    # Half-specified count stays rejected.
    with pytest.raises(Exception) as exc:
        remote_query.prepare_remote_plan(db, admin_user, _request(
            identity, aggregates=[{"fn": "count", "source": "a", "as": "x"}]))
    assert "count" in str(exc.value)


# ------------------------------------------------------------- 时间分桶/有界统计

def _add_date_property(db, identity):
    import copy

    project = db.query(OntologyProject).filter_by(id=identity["ontologyId"]).one()
    release = db.query(OntologyVersion).filter_by(id=project.current_release_id).one()
    snapshot = copy.deepcopy(release.snapshot_formal)
    snapshot["objectTypes"][0]["properties"].append(
        {"id": "p-day", "name": "day", "type": "date"})
    mapping = snapshot["mappings"][0]
    mapping["remote"]["columns"].append({"name": "physical_day", "type": "date"})
    mapping["fieldMapping"]["physical_day"] = "day"
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    db.commit()


def test_remote_plan_bucket_group_carries_bucket_and_validates_type(db, admin_user):
    identity, _profile = _release(db, admin_user)
    _add_date_property(db, identity)
    plan = remote_query.prepare_remote_plan(db, admin_user, _request(
        identity,
        select=[{"source": "a", "property": "day", "as": "day"}],
        group_by=[{"source": "a", "property": "day", "bucket": "month"}],
        aggregates=[{"fn": "count", "as": "count"}],
        order_by=[{"output": "day", "direction": "asc"}]))
    assert plan["groupBy"][0]["bucket"] == "month"

    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, _request(
            identity,
            select=[{"source": "a", "property": "note", "as": "note"}],
            group_by=[{"source": "a", "property": "note", "bucket": "month"}],
            aggregates=[{"fn": "count", "as": "count"}],
            order_by=[{"output": "note", "direction": "asc"}]))
    assert error.value.code == "invalid_parameter"
    assert "date/datetime/timestamp" in error.value.message


def test_remote_plan_bounded_stats_requires_group_order_and_sets_limit(db, admin_user):
    identity, _profile = _release(db, admin_user)
    grouped = dict(
        select=[{"source": "a", "property": "note", "as": "note"}],
        group_by=[{"source": "a", "property": "note"}],
        aggregates=[{"fn": "count", "as": "count"}],
        order_by=[{"output": "count", "direction": "desc"}])
    plan = remote_query.prepare_remote_plan(
        db, admin_user, _request(identity, bounded_stats=True, **grouped))
    assert plan["statsLimit"] == plan["pageSize"] == 10

    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(db, admin_user, _request(
            identity, bounded_stats=True,
            select=[{"source": "a", "property": "note", "as": "note"}],
            group_by=[{"source": "a", "property": "note"}],
            aggregates=[{"fn": "count", "as": "count"}]))
    assert "orderBy" in error.value.message
