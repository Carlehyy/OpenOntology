"""事实流与资产湖版本的端到端血缘 + 关系孪生（链接属性事实）。

第 1 批（MYW-21）：
- PropertyFact.source_dataset_version_id：投影事实携带来源湖版本；
- DatasetVersion.producer_run_id：湖版本记录产出它的流水线运行；
- 链接属性事实（kind='link' 属性级）：创建/更新边属性进入事实流，
  as-of 时态回放对链接同样可用。
"""
from __future__ import annotations

import pytest

from app.ontologies.formal_modeling.facts import record_property_facts
from app.ontologies.formal_modeling.models import PropertyFact


def _base(ontology_id: str) -> str:
    return f"/api/v2/formal/ontologies/{ontology_id}"


def _create_type(client, headers, oid: str, name: str, *, properties=None,
                 primary_key: str | None = None) -> str:
    r = client.post(f"{_base(oid)}/object-types", headers=headers, json={
        "name": name,
        "displayName": name,
        "primaryKey": primary_key,
        "properties": properties or [],
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _create_instance(client, headers, oid: str, otid: str, properties: dict):
    r = client.post(f"{_base(oid)}/instances", headers=headers, json={
        "objectTypeId": otid,
        "properties": properties,
        "source": "manual",
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _create_link_type(client, headers, oid: str, src_otid: str, tgt_otid: str):
    r = client.post(f"{_base(oid)}/link-types", headers=headers, json={
        "name": "owned_by", "displayName": "归属",
        "sourceObjectTypeId": src_otid, "targetObjectTypeId": tgt_otid,
        "cardinality": "one-to-one",
        "properties": [
            {"id": "link-weight", "name": "weight", "displayName": "权重",
             "type": "number", "required": False},
        ],
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_record_property_facts_stores_source_dataset_version(db, ontology):
    """事实写入器落盘来源湖版本，供血缘查询。"""
    from app.models.ontology_formal import ObjectInstance

    inst = ObjectInstance(
        ontology_id=ontology["id"], object_type_id="ot-x",
        properties={"a": 1}, source="pipeline", external_id="e-1",
    )
    db.add(inst)
    db.flush()
    created = record_property_facts(
        db, ontology_id=ontology["id"], instance_id=inst.id,
        object_type_id="ot-x", old_props=None, new_props={"a": 1},
        source="pipeline", source_dataset_version_id="dv-42")
    db.commit()
    assert len(created) == 1
    row = db.query(PropertyFact).filter(
        PropertyFact.instance_id == inst.id,
        PropertyFact.property_name == "a",
    ).one()
    assert row.source_dataset_version_id == "dv-42"
    assert row.kind == "property"


def test_fact_dto_exposes_source_dataset_version(
        client, auth_headers, ontology, db):
    """事实列表 API 返回 sourceDatasetVersionId，前端可直接展示血缘。"""
    oid = ontology["id"]
    otid = _create_type(client, auth_headers, oid, "Order", primary_key="code",
                        properties=[{"id": "p-code", "name": "code",
                                     "displayName": "编号", "type": "string",
                                     "required": True}])
    iid = _create_instance(client, auth_headers, oid, otid, {"code": "O-1"})

    from app.models.ontology_formal import ObjectInstance
    inst = db.query(ObjectInstance).filter(ObjectInstance.id == iid).one()
    record_property_facts(
        db, ontology_id=oid, instance_id=iid,
        object_type_id=inst.object_type_id,
        old_props=dict(inst.properties or {}),
        new_props={**dict(inst.properties or {}), "code": "O-1-v2"},
        source="pipeline", source_dataset_version_id="dv-7")
    db.commit()

    r = client.get(f"{_base(oid)}/instances/{iid}/facts",
                   headers=auth_headers)
    assert r.status_code == 200, r.text
    pipeline_fact = next(
        item for item in r.json()["data"]
        if item.get("source") == "pipeline")
    assert pipeline_fact["sourceDatasetVersionId"] == "dv-7"


def test_link_property_facts_and_as_of_replay(
        client, auth_headers, ontology):
    """边属性创建/更新落事实（kind='link'），链接也可时态回放。"""
    oid = ontology["id"]
    otid = _create_type(client, auth_headers, oid, "Order", primary_key="code",
                        properties=[{"id": "p-code", "name": "code",
                                     "displayName": "编号", "type": "string",
                                     "required": True}])
    a = _create_instance(client, auth_headers, oid, otid, {"code": "O-1"})
    b = _create_instance(client, auth_headers, oid, otid, {"code": "O-2"})
    ltid = _create_link_type(client, auth_headers, oid, otid, otid)

    r = client.post(f"{_base(oid)}/link-instances", headers=auth_headers, json={
        "linkTypeId": ltid, "sourceObjectId": a, "targetObjectId": b,
        "properties": {"weight": 1.5},
    })
    assert r.status_code == 201, r.text
    link_id = r.json()["data"]["id"]

    facts = client.get(
        f"{_base(oid)}/instances/{link_id}/facts",
        headers=auth_headers).json()["data"]
    weight_facts = [f for f in facts if f["propertyName"] == "weight"]
    assert len(weight_facts) == 1, facts
    assert weight_facts[0]["kind"] == "link"
    assert weight_facts[0]["value"] == 1.5
    exists_facts = [f for f in facts if f["propertyName"] == "exists"]
    assert exists_facts and exists_facts[0]["value"] is True

    # 时态回放：创建时刻的边投影 = weight 1.5 且存在
    created_at = weight_facts[0]["recordedAt"]
    replay = client.get(
        f"{_base(oid)}/instances/{link_id}/as-of",
        params={"t": created_at}, headers=auth_headers)
    assert replay.status_code == 200, replay.text
    body = replay.json()["data"]
    assert body["exists"] is True
    assert body["properties"].get("weight") == 1.5

    # 更新边属性 → 新事实 supersede 旧事实，回放新时刻看到新值
    r = client.patch(f"{_base(oid)}/link-instances/{link_id}",
                     headers=auth_headers, json={"properties": {"weight": 2.5}})
    assert r.status_code == 200, r.text

    facts = client.get(
        f"{_base(oid)}/instances/{link_id}/facts",
        headers=auth_headers).json()["data"]
    weight_facts = sorted(
        [f for f in facts if f["propertyName"] == "weight"],
        key=lambda f: f["recordedAt"])
    assert len(weight_facts) == 2, facts
    assert weight_facts[1]["value"] == 2.5
    assert weight_facts[1]["supersedesId"] == weight_facts[0]["id"]

    replay = client.get(
        f"{_base(oid)}/instances/{link_id}/as-of",
        params={"t": weight_facts[1]["recordedAt"]}, headers=auth_headers)
    assert replay.json()["data"]["properties"].get("weight") == 2.5

    # 删除链接 → 墓碑存在性事实，回放删除后时刻 exists=False
    assert client.delete(f"{_base(oid)}/link-instances/{link_id}",
                         headers=auth_headers).status_code == 204
    facts = client.get(
        f"{_base(oid)}/instances/{link_id}/facts",
        headers=auth_headers).json()["data"]
    tombstone = next(
        f for f in facts
        if f["propertyName"] == "exists" and f["value"] is False)
    replay = client.get(
        f"{_base(oid)}/instances/{link_id}/as-of",
        params={"t": tombstone["recordedAt"]}, headers=auth_headers)
    assert replay.json()["data"]["exists"] is False


def test_link_update_rejects_unknown_property(
        client, auth_headers, ontology):
    """链接属性更新同样受契约校验（未知属性 422）。"""
    oid = ontology["id"]
    otid = _create_type(client, auth_headers, oid, "Order", primary_key="code",
                        properties=[{"id": "p-code", "name": "code",
                                     "displayName": "编号", "type": "string",
                                     "required": True}])
    a = _create_instance(client, auth_headers, oid, otid, {"code": "O-1"})
    b = _create_instance(client, auth_headers, oid, otid, {"code": "O-2"})
    ltid = _create_link_type(client, auth_headers, oid, otid, otid)
    r = client.post(f"{_base(oid)}/link-instances", headers=auth_headers, json={
        "linkTypeId": ltid, "sourceObjectId": a, "targetObjectId": b,
        "properties": {"weight": 1.5},
    })
    link_id = r.json()["data"]["id"]

    r = client.patch(f"{_base(oid)}/link-instances/{link_id}",
                     headers=auth_headers, json={"properties": {"ghost": 1}})
    assert r.status_code == 422, r.text
    codes = {e["code"] for e in r.json()["detail"]["errors"]}
    assert "unknown_property" in codes

    # 404：不存在的链接
    r = client.patch(f"{_base(oid)}/link-instances/missing-link",
                     headers=auth_headers, json={"properties": {"weight": 9}})
    assert r.status_code == 404


@pytest.mark.parametrize("source", ["pipeline", "pipeline-reconcile"])
def test_projection_link_facts_carry_source_version(
        db, admin_user, source):
    """投影建链/对账事实落盘来源湖版本（与 formal_projection 写路径对齐）。"""
    from app.models.ontology import OntologyProject
    from app.ontologies.formal_modeling.facts import record_link_fact

    onto = OntologyProject(name="血缘事实", domain="测试",
                           build_mode="pipeline_mapping",
                           created_by=admin_user.id)
    db.add(onto)
    db.commit()

    fact = record_link_fact(
        db, ontology_id=onto.id, link_instance_id="li-1",
        link_type_id="lt-1", exists=True, source=source,
        source_dataset_version_id="dv-9")
    db.commit()
    assert fact is not None
    assert fact.source_dataset_version_id == "dv-9"
    assert fact.kind == "link"


def test_promoted_link_lineage_uses_only_unambiguous_frozen_mapping_pins():
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.ontologies.versions.promotion_service import _link_fact_version

    link = SimpleNamespace(link_id="link", link_type_id="owns", source_relation_id="relation",
                           source_object_id="source", target_object_id="target")
    endpoints = {"source": "source-dataset", "target": "target-dataset"}
    direct = {"srcDatasetId": "source-dataset", "tgtDatasetId": "target-dataset"}
    edge = {**direct, "edgeDatasetId": "edge-dataset"}
    pins = {"source-dataset": {"versionId": "source-pin"},
            "target-dataset": {"versionId": "target-pin"},
            "edge-dataset": {"versionId": "edge-pin"}}
    assert _link_fact_version(link, endpoints, {"owns": [direct]}, pins) == "source-pin"
    assert _link_fact_version(link, endpoints, {"owns": [edge]}, pins) == "edge-pin"
    assert _link_fact_version(link, endpoints, {"owns": [edge, dict(edge)]}, pins) == "edge-pin"
    assert _link_fact_version(link, {}, {"owns": [edge]}, pins) is None
    assert _link_fact_version(link, endpoints, {}, pins) is None
    assert _link_fact_version(link, endpoints, {"owns": [edge]}, {}) is None
    import copy
    pins["other-edge-dataset"] = {"versionId": "other-edge-pin"}
    before = copy.deepcopy((vars(link), endpoints, pins))
    for compatible in ([direct, edge], [edge, {**direct, "edgeDatasetId": "other-edge-dataset"}]):
        with pytest.raises(HTTPException) as ambiguous:
            _link_fact_version(link, endpoints, {"owns": compatible}, pins)
        assert ambiguous.value.status_code == 409
        assert ambiguous.value.detail["code"] == "link_lineage_ambiguous"
        assert ambiguous.value.detail["linkId"] == "link"
        assert "血缘不唯一" in ambiguous.value.detail["message"]
        assert (vars(link), endpoints, pins) == before
    link.source_relation_id = None
    assert _link_fact_version(link, endpoints, {"owns": [direct, edge]}, pins) is None
