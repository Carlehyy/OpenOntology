"""部分映射本体上的本体助手工具契约（fail-closed，不拿 0/空冒充完整）。

model-first 的本体里常见「只建了结构、还没接数据」的对象类型与关系。在此
之前 search_objects 对这类类型返回 total=0/exact/complete，traverse_links 与
带 joins 的 query_objects 对无数据关系返回 0 邻居——与「有数据但过滤为零」
「确实没有关联」无法区分。本文件固定：

  1. 无映射且无本地事实的对象类型：search_objects 报 object_data_unmapped；
  2. 有已发布本地映射的对象类型过滤确实为零：仍是如实的 0 结果；
  3. 无映射但有本地事实/边（手工建模）的对象与关系照常可查；
  4. 无数据的纯结构关系：traverse_links / query_objects 报
     relation_data_unmapped，且不触达查询执行；
  5. 远程混合关系：get_object 不再把残留旧边列入 links，改用
     dataCoverage.gaps 说明；traverse_links 维持既有围栏码；
  6. describe_objects 在摘要与详情中标明对象/关系的可查询能力。

对象与关系判定同时覆盖钉定 release（快照 mappings/linkMappings）与无
release 的工作区实时视图（live 映射行）——后者是默认对话入口，A14 曾在
这里漏判。远程绑定本身的围栏见 test_agent_remote_fence.py，本文件不重复。
"""
import json

import pytest

from app.data_channel.datasets.models import Dataset
from app.ontologies.agent_query import remote_query
from app.ontologies.agent_query.service import QueryError
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.toolkit import ToolRunner
from app.ontologies.formal_modeling.models import LinkInstance, LinkType, ObjectInstance, ObjectType
from app.ontologies.mappings.models import OntologyMapping
from tests.ontologies.test_agent_remote_aggregation import (
    _SequencePort, _patch_execute_unified, _unified_result,
)
from tests.ontologies.test_agent_remote_fence import (
    _REMOTE_DESCRIPTOR, _freeze_with_remote_supplier,
)
from tests.ontologies.test_agent_runtime import modeled_ontology  # noqa: F401
from tests.ontologies.test_agent_unified_query import _freeze

_OBJECT_UNMAPPED = "object_data_unmapped"
_RELATION_UNMAPPED = "relation_data_unmapped"
_REMOTE_FENCE = "remote_ontology_semantic_query_required"
_VIEWS = ["pinned", "live"]

# fixture 之外的虚构结构：无映射无事实的优惠券，以及无映射无边的
# order→supplier 关系。后者与 lt-1 端点相同（lt-1 同样没有 link mapping，
# 但有一条本地边），只差「有没有数据」，便于并排对比两类关系的工具行为。
_COUPON = "ot-coupon"
_BACKUP_SUPPLIER = "order_backup_supplier"
_LOCAL_ORDER_DATASET = "ds-local-orders"


def _runner(db, ontology_id: str, view: str, release_id: str, *,
            user=None) -> ToolRunner:
    """pinned=钉定 release 的快照视图；live=默认对话入口的工作区实时视图。"""
    _, _, scope = build_scope(
        db, ontology_id, release_id=release_id if view == "pinned" else None)
    return ToolRunner(db, scope, user=user)


def _freeze_with_dataless_structure(db, ontology_id: str) -> str:
    """增补虚构类型与关系后冻结当前 release，返回 release id。"""
    db.add(ObjectType(
        id=_COUPON, ontology_id=ontology_id, name="Coupon", display_name="优惠券",
        primary_key="coupon_no",
        properties=[{"id": "cp1", "name": "coupon_no", "displayName": "券号",
                     "type": "string", "required": True}]))
    db.add(LinkType(
        id="lt-backup-supplier", ontology_id=ontology_id, name=_BACKUP_SUPPLIER,
        display_name="备选供应商", source_object_type_id="ot-order",
        target_object_type_id="ot-supplier", cardinality="many-to-one",
        properties=[]))
    db.commit()
    return _freeze(db, ontology_id)["releaseId"]


def _publish_local_order_mapping(db, ontology_id: str) -> None:
    """给 Order 挂一条可物化的本地映射：真实 curated 数据集 + applied 状态。

    形状对齐 release_gate_service / promotion_service 认可的已物化映射
    （curatedDatasetId 指向存在的数据集、声明 __primary_key__），冻结时由
    真实快照采集器收进 release 的 mappings。
    """
    db.add(Dataset(id=_LOCAL_ORDER_DATASET, name="订单源-本地映射", kind="curated",
                   schema_json={"primary_key": "order_no"}))
    db.add(OntologyMapping(
        id="map-local-order", ontology_id=ontology_id, entity_class="Order",
        target_object_type_id="ot-order", status="applied",
        curated_dataset_id=_LOCAL_ORDER_DATASET,
        field_mapping={"order_no": "order_no", "status": "status",
                       "amount": "amount",
                       "__primary_key__": "order_no", "__pk_source__": "lake"}))
    db.commit()


def _pin_stale_edge(db, ontology_id: str, release_id: str) -> None:
    """把 fixture 的旧边标成同 release 物化行（切换远程绑定前的残留）。"""
    for link in db.query(LinkInstance).filter_by(ontology_id=ontology_id).all():
        link.ontology_release_id = release_id
    db.commit()


def _bind_remote_supplier_live(db, ontology_id: str) -> None:
    """工作区实时视图下的供应商远程绑定：live 映射行携带冻结描述符。"""
    db.add(OntologyMapping(
        id="map-live-remote-supplier", ontology_id=ontology_id,
        entity_class="Supplier", target_object_type_id="ot-supplier",
        status="applied",
        field_mapping={"__binding_mode__": "remote",
                       "__remote__": _REMOTE_DESCRIPTOR,
                       "supplier_name": "sname",
                       "__primary_key__": "supplier_name",
                       "__pk_source__": "remote"}))
    db.commit()


def _assert_code(exc: ToolError, code: str) -> None:
    """ToolError 契约：既有 ``code`` 属性，又保留 ``<code>: <说明>`` 消息前缀。"""
    assert getattr(exc, "code", None) == code, str(exc)
    assert str(exc).startswith(f"{code}:"), str(exc)


def _assert_capability(entry: dict, queryable: bool, data_mode: str,
                       reason: str | None = None) -> None:
    assert entry["queryable"] is queryable, entry
    assert entry["dataMode"] == data_mode, entry
    assert entry.get("unqueryableReason") == reason, entry


def _supplier_names(result: dict) -> list:
    return [item["properties"]["sname"] for item in result["items"]]


def _joined_query(link_type: str) -> dict:
    return {
        "object_type": "Order",
        "joins": [{"object_type": "Supplier", "link_type": link_type}],
        "select": ["order_no", {"object_type": "Supplier", "property": "sname"}],
        "order_by": [{"output": "order_no", "direction": "asc"}],
    }


# ------------------------------------------------ 1-3 对象：未映射 / 真零 / 本地事实


@pytest.mark.parametrize("view", _VIEWS)
def test_search_objects_rejects_object_type_without_mapping_or_facts(
        modeled_ontology, db, view):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_dataless_structure(db, oid)
    runner = _runner(db, oid, view, release_id)

    # 优惠券既无映射也无本地事实：0 行不是业务事实，不得报 complete。
    with pytest.raises(ToolError) as rejected:
        runner.run("search_objects", {"object_type": "Coupon"})

    _assert_code(rejected.value, _OBJECT_UNMAPPED)


@pytest.mark.parametrize("view", _VIEWS)
def test_search_objects_keeps_published_mapping_and_manual_facts_queryable(
        modeled_ontology, db, view):
    oid = modeled_ontology["id"]
    _publish_local_order_mapping(db, oid)
    release_id = _freeze(db, oid)["releaseId"]
    runner = _runner(db, oid, view, release_id)

    # 已发布本地映射 + 过滤确实为零：如实返回 0，不得误判为「未映射」。
    empty = runner.run("search_objects", {
        "object_type": "Order",
        "filters": [{"property": "order_no", "op": "eq", "value": "SO-NONE"}]})
    assert (empty["total"], empty["returned"], empty["items"]) == (0, 0, [])
    assert empty["exact"] is True and empty["completeness"] == "complete"
    assert runner.run("search_objects", {"object_type": "Order"})["total"] == 2

    # 无映射但有本地事实（手工建模的 Supplier）：照常可查，不因「无映射」被拒。
    manual = runner.run("search_objects", {"object_type": "Supplier"})
    assert _supplier_names(manual) == ["华南电子"]


# ---------------------------------------------------- 4 关系：无数据的纯结构关系


@pytest.mark.parametrize("view", _VIEWS)
def test_traverse_links_rejects_relation_without_data(modeled_ontology, db, view):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_dataless_structure(db, oid)
    runner = _runner(db, oid, view, release_id)

    # 备选供应商关系没有任何边：0 个邻居不是「没有关联」。
    with pytest.raises(ToolError) as rejected:
        runner.run("traverse_links", {"instance_id": "inst-o1",
                                      "link_type": _BACKUP_SUPPLIER})
    _assert_code(rejected.value, _RELATION_UNMAPPED)

    # 同端点、同样无 link mapping 但有一条本地边的 lt-1 照常可查。
    related = runner.run("traverse_links", {"instance_id": "inst-o1",
                                            "link_type": "order_supplier"})
    assert _supplier_names(related) == ["华南电子"]
    # 全本地对象的详情照常列出有数据的本地关系。
    detail = runner.run("get_object", {"instance_id": "inst-o1"})
    assert [(link["linkTypeName"], link["count"])
            for link in detail["links"]] == [("order_supplier", 1)]


def test_query_objects_rejects_relation_without_data_before_any_query(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_dataless_structure(db, oid)
    runner = _runner(db, oid, "pinned", release_id, user=admin_user)

    # 对照：沿有本地边的关系关联查询会真正执行——证明下面的「零调用」
    # 不是查询走了旁路造成的假象。
    related = runner.run("query_objects", _joined_query("order_supplier"))
    assert [(row["order_no"], row["sname"]) for row in related["rows"]] == [
        ("SO-001", "华南电子")]

    calls = _patch_execute_unified(monkeypatch, error=QueryError(
        422, "unexpected_source_query", "无数据关系不得触达查询执行"))
    with pytest.raises(ToolError) as rejected:
        runner.run("query_objects", _joined_query(_BACKUP_SUPPLIER))
    _assert_code(rejected.value, _RELATION_UNMAPPED)
    assert calls == []


# ---------------------------------------- 5 远程混合关系：详情以 gaps 说明，不列旧边


@pytest.mark.parametrize("view", _VIEWS)
def test_get_object_reports_remote_relation_as_gap_not_stale_links(
        modeled_ontology, db, view):
    oid = modeled_ontology["id"]
    if view == "pinned":
        release_id = _freeze_with_remote_supplier(db, oid)
        _pin_stale_edge(db, oid, release_id)
    else:
        release_id = None
        _bind_remote_supplier_live(db, oid)
    runner = _runner(db, oid, view, release_id)

    result = runner.run("get_object", {"instance_id": "inst-o1"})

    # 残留旧边指向的 inst-s1 是切换远程绑定前的物化行，不得再当邻居展示。
    assert result["links"] == []
    gaps = result["dataCoverage"]["gaps"]
    assert [gap["linkTypeId"] for gap in gaps] == ["lt-1"]
    assert gaps[0]["code"] == _REMOTE_FENCE
    assert gaps[0]["kind"] == "linkType"
    assert gaps[0]["label"] == "订单-供应商"
    assert "inst-s1" not in json.dumps(result, ensure_ascii=False)

    # 沿同一关系的本地遍历维持既有围栏码。
    with pytest.raises(ToolError, match=f"^{_REMOTE_FENCE}:"):
        runner.run("traverse_links", {"instance_id": "inst-o1",
                                      "link_type": "order_supplier"})


# ------------------------------------------------- 6 describe_objects：能力可见


@pytest.mark.parametrize("view", _VIEWS)
def test_describe_objects_reports_capability_of_types_and_relations(
        modeled_ontology, db, view):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_dataless_structure(db, oid)
    runner = _runner(db, oid, view, release_id)

    summary = {item["id"]: item
               for item in runner.run("describe_objects", {})["objectTypes"]}
    _assert_capability(summary["ot-order"], True, "materialized")
    _assert_capability(summary[_COUPON], False, "unmapped", _OBJECT_UNMAPPED)

    order = runner.run(
        "describe_objects", {"object_types": ["Order"]})["objectTypes"][0]
    _assert_capability(order, True, "materialized")
    links = {link["name"]: link for link in order["links"]}
    _assert_capability(links["order_supplier"], True, "materialized")
    _assert_capability(links[_BACKUP_SUPPLIER], False, "unmapped",
                       _RELATION_UNMAPPED)


def test_describe_objects_marks_remote_binding_and_mixed_relation(
        modeled_ontology, db):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)
    runner = _runner(db, oid, "pinned", release_id)

    summary = {item["id"]: item
               for item in runner.run("describe_objects", {})["objectTypes"]}
    details = {item["id"]: item for item in runner.run(
        "describe_objects", {"object_types": ["Supplier", "Order"]})["objectTypes"]}

    # 钉定发布态且远程绑定唯一：可走治理入口（query_objects）。
    _assert_capability(summary["ot-supplier"], True, "remote")
    _assert_capability(details["ot-supplier"], True, "remote")
    _assert_capability(details["ot-order"], True, "materialized")
    # 混合端点的关系：沿它做治理查询本身不被支持（dataMode 取值此处不约定）。
    [mixed] = [link for link in details["ot-order"]["links"]
               if link["name"] == "order_supplier"]
    assert mixed["queryable"] is False, mixed
    assert mixed["unqueryableReason"] == "remote_relation_unsupported", mixed


def _add_order_fields(db):
    order = db.get(ObjectType, "ot-order")
    order.properties = [*order.properties, *[
        {"id": name, "name": name, "type": "string"}
        for name in ("stored_null", "definition_only")
    ]]
    for row in db.query(ObjectInstance).filter_by(object_type_id=order.id):
        row.properties = {**row.properties, "stored_null": None}
    db.commit()


@pytest.mark.parametrize("view", _VIEWS)
def test_explicit_property_requires_a_fact_or_applied_mapping(db, modeled_ontology, admin_user, view):
    oid = modeled_ontology["id"]
    _add_order_fields(db)
    _publish_local_order_mapping(db, oid)
    # A draft field mapping, even with a real dataset, establishes no coverage.
    db.add(OntologyMapping(id="map-unapplied", ontology_id=oid, entity_class="Order",
                           target_object_type_id="ot-order", status="draft",
                           curated_dataset_id=_LOCAL_ORDER_DATASET,
                           field_mapping={"draft_column": "definition_only"}))
    db.commit()
    release_id = _freeze(db, oid)["releaseId"]
    if view == "pinned":
        db.add(ObjectInstance(id="outside-release", ontology_id=oid, object_type_id="ot-order",
                              properties={"order_no": "OTHER", "definition_only": "outside"}))
        db.commit()
    runner = _runner(db, oid, view, release_id, user=admin_user)
    detail = runner.run("describe_objects", {"object_types": ["Order"]})["objectTypes"][0]
    properties = {item["name"]: item for item in detail["properties"]}
    assert properties["stored_null"]["queryable"] is True
    assert properties["definition_only"]["unqueryableReason"] == "property_data_unmapped"

    count = runner.run("aggregate_objects", {
        "object_type": "Order", "group_by": "stored_null"})
    assert count["groups"] == [{"group": "", "value": 2, "count": 2}]
    for tool, args in [
        ("search_objects", {"filters": [{"property": "definition_only", "op": "eq", "value": None}]}),
        ("aggregate_objects", {"group_by": "definition_only"}),
        *([("query_objects", {"select": ["definition_only"]}),
           ("query_objects", {"order_by": [{"output": "definition_only"}]})] if view == "pinned" else []),
    ]:
        with pytest.raises(ToolError) as rejected:
            runner.run(tool, {"object_type": "Order", **args})
        assert rejected.value.code == "property_data_unmapped"
        assert rejected.value.gap["property"] == "definition_only"
    if view == "pinned":
        rows = runner.run("query_objects", {"object_type": "Order", "select": ["stored_null"]})
        assert rows["rows"] == [{"stored_null": None}, {"stored_null": None}]
        assert rows["completeness"] == "complete"


def test_spl_checks_coverage_before_port_and_replays_original_request(
        db, modeled_ontology, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    _add_order_fields(db)
    release_id = _freeze(db, oid)["releaseId"]
    runner = _runner(db, oid, "pinned", release_id, user=admin_user)
    port = _SequencePort([_unified_result([{"stored_null": None}])])
    monkeypatch.setattr(remote_query, "_port", port)
    bindings = {"orders": {"objectType": "Order"}}
    with pytest.raises(ToolError) as rejected:
        runner.run("query_objects", {"semanticBindings": bindings,
                                    "spl": "from orders as o | fields o.definition_only"})
    assert rejected.value.code == "property_data_unmapped"
    assert port.executed == []

    text = "from orders as o | fields o.stored_null"
    out = runner.run("query_objects", {"semanticBindings": bindings, "spl": text})
    assert out["rows"] == [{"stored_null": None}]
    [execution] = port.executed
    assert execution["request"]["spl"] == text
    assert execution["request"]["semanticBindings"]["orders"]["releaseId"] == release_id


def test_graph_gaps_follow_reachable_authorized_types(db, modeled_ontology, client, auth_headers):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_dataless_structure(db, oid)
    # A stale edge on the valid type must not supply a different type as a neighbor.
    db.add(LinkInstance(id="li-wrong-end", ontology_id=oid, ontology_release_id=release_id,
                        link_type_id="lt-1", source_object_id="inst-o1", target_object_id="inst-o2"))
    db.commit()
    runner = _runner(db, oid, "pinned", release_id)
    detail = runner.run("get_object", {"instance_id": "inst-o1"})
    assert [link["count"] for link in detail["links"]] == [1]
    assert [gap["linkTypeId"] for gap in detail["dataCoverage"]["gaps"]] == ["lt-backup-supplier"]
    link_args = {"instance_id": "inst-o1", "link_type": "order_supplier"}
    assert _supplier_names(runner.run("traverse_links", link_args)) == ["华南电子"]
    assert _supplier_names(runner.run("traverse_path", {"instance_id": "inst-o1", "path": [
        {"link_type": "order_supplier"}]})) == ["华南电子"]
    args = {"source_instance_id": "inst-o1", "target_instance_id": "inst-o2", "direction": "outgoing"}
    path = runner.run("find_paths", args)
    assert path["paths"] == [] and path["partial"] is True
    assert [gap["linkTypeId"] for gap in path["dataCoverage"]["gaps"]] == ["lt-backup-supplier"]
    impact = runner.run("analyze_change_impact", {
        "instance_id": "inst-o1", "property": "status", "proposed_value": "paid"})
    assert impact["partial"] is True
    assert [item["instanceId"] for item in impact["impacts"]] == ["inst-s1"]

    profile_path = f"/api/v2/formal/ontologies/{oid}/agent/profile"
    response = client.put(profile_path, headers=auth_headers, json={"allowedLinkTypeIds": ["lt-1"]})
    assert response.status_code == 200
    db.expire_all()
    restricted = _runner(db, oid, "pinned", release_id).run("find_paths", args)
    assert restricted["paths"] == [] and "dataCoverage" not in restricted
