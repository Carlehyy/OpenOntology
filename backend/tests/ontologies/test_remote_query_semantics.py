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
