"""D8 回归：远程绑定对象类型在本地工具面必须显式拒绝。

旧 search/traverse/graph/path/history/报告 fallback 对 remote 绑定类型一律报
remote_ontology_semantic_query_required，不得返回 0/空/exact complete 冒充
完整；本地物化类型不受影响；混类型图谱诚实标注远程部分而非冒充全量。
钉定 release 的视图只认发布快照；默认对话的工作区实时视图按当前 live
映射判定远程绑定（A14：否则 release-less 会话以 0 行冒充）。

远程聚合是唯一例外（细化契约见 test_agent_remote_aggregation.py）：钉定
release 的 aggregate_objects 对远程绑定类型自动转统一查询执行，无认证
用户的 runner 报 remote_query_user_required；无 release 的会话没有发布
绑定语义，聚合维持本文件的旧围栏。search/遍历/历史围栏不变。
"""
import json

import pytest

from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.toolkit import ToolRunner
from tests.ontologies.test_agent_runtime import modeled_ontology

_ERROR_CODE = "remote_ontology_semantic_query_required"

_REMOTE_DESCRIPTOR = {
    "assetId": "asset-remote-1",
    "datasetId": "ds-remote-1",
    "connectionId": "conn-remote-1",
    "schemaName": "public",
    "tableName": "suppliers",
    "schemaFingerprint": "f" * 64,
    "columns": [{"name": "supplier_name", "type": "string"}],
    "primaryKey": ["supplier_name"],
}


def _freeze_with_remote_supplier(db, ontology_id: str) -> str:
    """冻结当前 release 并在其发布快照中登记 ot-supplier 的远程绑定。"""
    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import ObjectInstance
    from app.models.ontology_version import OntologyVersion
    from app.ontologies.versions.evolution_service import snapshot_hash
    from app.ontologies.versions.workspace_service import _snapshot_formal

    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    snapshot = _snapshot_formal(db, ontology_id)
    snapshot.setdefault("mappings", []).append({
        "id": "map-remote-supplier",
        "targetObjectTypeId": "ot-supplier",
        "entityClass": "Supplier",
        "curatedDatasetId": "ds-remote-1",
        "bindingMode": "remote",
        "remote": _REMOTE_DESCRIPTOR,
        "fieldMapping": {"supplier_name": "sname"},
        "status": "published",
    })
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    for instance in db.query(ObjectInstance).filter_by(
            ontology_id=ontology_id).all():
        instance.ontology_release_id = release.id
    db.commit()
    return str(release.id)


def _pinned_runner(db, ontology_id: str, release_id: str) -> ToolRunner:
    _, _, scope = build_scope(db, ontology_id, release_id=release_id)
    return ToolRunner(db, scope)


def _fo(ontology_id: str) -> str:
    return f"/api/v2/formal/ontologies/{ontology_id}"


def test_search_refuses_remote_type_and_pinned_aggregate_needs_user(
        modeled_ontology, db):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)
    runner = _pinned_runner(db, oid, release_id)

    with pytest.raises(ToolError, match=_ERROR_CODE):
        runner.run("search_objects", {"object_type": "Supplier"})
    # 钉定 release 的远程聚合自动转统一查询（不再走旧围栏转介）；
    # 本地直连 runner 没有认证用户，必须显式报缺用户而不是统计 0。
    with pytest.raises(ToolError, match="remote_query_user_required"):
        runner.run("aggregate_objects",
                   {"object_type": "Supplier", "metric": "count"})

    # 同一 scope 内本地物化类型不受影响：不能一刀切禁掉全部本地查询。
    agg = runner.run("aggregate_objects",
                     {"object_type": "Order", "metric": "count"})
    assert agg["value"] == 2
    found = runner.run("search_objects",
                       {"object_type": "Order", "limit": 5})
    assert found["total"] == 2 and found["exact"] is True


def test_traverse_refuses_links_touching_remote_side(
        modeled_ontology, db):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)
    runner = _pinned_runner(db, oid, release_id)

    # lt-1 (order→supplier) 的远端是远程绑定：返回 0 邻居会冒充"无关联"。
    with pytest.raises(ToolError, match=_ERROR_CODE):
        runner.run("traverse_links",
                   {"instance_id": "inst-o1", "link_type": "order_supplier"})
    with pytest.raises(ToolError, match=_ERROR_CODE):
        runner.run("traverse_path", {
            "instance_id": "inst-o1",
            "path": [{"link_type": "order_supplier", "direction": "out"}],
        })


def test_history_refuses_stale_materialized_remote_instance(
        modeled_ontology, db):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)
    runner = _pinned_runner(db, oid, release_id)

    # inst-s1 是切换远程绑定前残留的同 release 物化行：历史事实流只覆盖
    # 本地数据面，不得对远程类型冒充完整溯源。
    with pytest.raises(ToolError, match=_ERROR_CODE):
        runner.run("get_object_history", {"instance_id": "inst-s1"})
    with pytest.raises(ToolError, match=_ERROR_CODE):
        runner.run("trace_causal_chain", {"instance_id": "inst-s1"})


def test_graph_overview_marks_remote_portion_instead_of_faking_full(
        client, auth_headers, modeled_ontology, db):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)

    graph = client.get(
        f"{_fo(oid)}/agent/graph",
        headers=auth_headers,
        params={"depth": 2, "release_id": release_id},
    )
    assert graph.status_code == 200, graph.text
    data = graph.json()["data"]
    meta = data["meta"]
    # 混类型诚实化：远程部分显式列出，本地图谱不冒充全量。
    assert meta["coverage"] == "materialized_only"
    assert [item["id"] for item in meta["remoteObjectTypes"]] == ["ot-supplier"]
    assert [item["id"] for item in meta["remoteLinkTypes"]] == ["lt-1"]
    assert "ot-supplier" not in meta["instanceCounts"]
    node_ids = {node["id"] for node in data["nodes"]}
    # 远程类型节点/实例不进图：0 计数的类型节点会冒充"该类型没有数据"。
    assert "type:ot-supplier" not in node_ids
    assert "inst-s1" not in json.dumps(data["nodes"], ensure_ascii=False)

    # 显式选中远程类型 → 明确 422，而不是空图。
    selected = client.get(
        f"{_fo(oid)}/agent/graph",
        headers=auth_headers,
        params={"depth": 2, "release_id": release_id, "object_type": "Supplier"},
    )
    assert selected.status_code == 422
    assert _ERROR_CODE in selected.text

    # 无 release 的实况/草稿模式没有发布快照语义：围栏按快照判定，不查 live。
    live = client.get(f"{_fo(oid)}/agent/graph", headers=auth_headers,
                      params={"depth": 1})
    assert live.status_code == 200
    assert live.json()["data"]["meta"]["coverage"] == "authorized_scope"


def test_report_fallback_skips_remote_types(modeled_ontology, db):
    from app.ontologies.agent_runtime.reporting import _fallback_spec

    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)
    _, _, scope = build_scope(db, oid, release_id=release_id)

    spec = _fallback_spec(scope, "经营概览")
    referenced = [query["arguments"]["object_type"]
                  for section in spec["sections"]
                  for query in section["queryPlan"]]
    assert referenced, "本地物化类型仍应可自动选题"
    assert "Supplier" not in referenced


def test_report_fallback_all_remote_fails_closed(modeled_ontology, db, client,
                                                 auth_headers):
    from app.ontologies.agent_runtime.reporting import _fallback_spec

    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)
    response = client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
                          json={"allowedObjectTypeIds": ["ot-supplier"]})
    assert response.status_code == 200, response.text
    _, _, scope = build_scope(db, oid, release_id=release_id)

    with pytest.raises(ValueError, match="统一查询入口"):
        _fallback_spec(scope, "经营概览")


def test_system_prompt_guides_remote_types_to_unified_query(
        modeled_ontology, db):
    from app.ontologies.agent_runtime.orchestrator import _system_prompt

    oid = modeled_ontology["id"]
    _, _, scope = build_scope(db, oid)
    prompt = _system_prompt(scope)
    assert _ERROR_CODE in prompt
    assert "ontology_query" in prompt


# ---------------------------- A14 回归：默认对话入口（未显式传 releaseId）


def test_releaseless_chat_scope_still_refers_remote(modeled_ontology, db):
    """A14 正例：默认对话入口（工作区实时视图）远程转介必须仍然生效。

    根因：release-less scope 的 remote_object_type_ids 此前恒返回空集，
    search_objects 会以本地 0 行冒充完整结果（真实 run02 a14_probe：
    reply 声称"实例数=0、search_objects total=0"且无转介标记）。
    工作区视图按当前 live 映射判定远程绑定；钉定 release 的视图仍只
    认快照（本文件既有 pinned 用例不受影响）。
    """
    from app.ontologies.mappings.models import OntologyMapping

    oid = modeled_ontology["id"]
    db.add(OntologyMapping(
        id="map-live-remote", ontology_id=oid, entity_class="Supplier",
        target_object_type_id="ot-supplier", status="applied",
        field_mapping={
            "__binding_mode__": "remote",
            "__remote__": {
                "assetId": "asset-live-1", "datasetId": "ds-live-1",
                "connectionId": "conn-live-1", "schemaName": "public",
                "tableName": "suppliers", "schemaFingerprint": "f" * 64,
                "columns": [{"name": "sname", "type": "string"}],
                "primaryKey": ["sname"],
            },
        }))
    db.commit()

    _, _, scope = build_scope(db, oid)  # 默认对话：不传 releaseId
    assert scope.release is None
    runner = ToolRunner(db, scope)
    with pytest.raises(ToolError, match=_ERROR_CODE):
        runner.run("search_objects", {"object_type": "Supplier"})
    # release-less 会话没有发布快照的远程绑定语义：聚合不自动转统一查询，
    # 维持旧围栏（不得以本地 0 行冒充完整统计）。
    with pytest.raises(ToolError, match=_ERROR_CODE):
        runner.run("aggregate_objects",
                   {"object_type": "Supplier", "metric": "count"})
    # 本地物化类型不受影响。
    found = runner.run("search_objects", {"object_type": "Order", "limit": 5})
    assert found["total"] == 2


def test_skill_card_declares_remote_referral_not_zero(modeled_ontology, db):
    """A14：技能卡不得向模型声明远程类型"实例数=0"（假事实源）。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)
    _, _, scope = build_scope(db, oid, release_id=release_id)
    card = scope.skill_card()
    blocks = card.split("###")
    supplier_block = next(b for b in blocks if "name=Supplier" in b)
    assert "实例数=0" not in supplier_block
    assert "远程绑定" in supplier_block
    # 本地类型仍带真实本地计数（合法 local 契约不变）。
    assert "实例数=2" in card  # Order 本地物化 2 条


def test_summary_marks_remote_types_unknown(modeled_ontology, db):
    """A14：能力概览远程类型 instanceCount=null + remote=true。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_remote_supplier(db, oid)
    _, _, scope = build_scope(db, oid, release_id=release_id)
    summary = scope.summary()
    types = {item["name"]: item for item in summary["objectTypes"]}
    assert types["Supplier"]["remote"] is True
    assert types["Supplier"]["instanceCount"] is None
    assert types["Order"]["remote"] is False
    assert types["Order"]["instanceCount"] == 2


def test_unpublished_scope_stays_workspace_live(modeled_ontology, db):
    """反例契约锁定：无发布指针的本体仍走工作区实时数据（不造 release）。"""
    from app.models.ontology import OntologyProject

    oid = modeled_ontology["id"]
    project = db.query(OntologyProject).filter_by(id=oid).one()
    project.current_release_id = None
    db.commit()
    _, _, scope = build_scope(db, oid)
    assert scope.release is None
    runner = ToolRunner(db, scope)
    found = runner.run("search_objects", {"object_type": "Order", "limit": 5})
    assert found["total"] == 2  # 工作区实时路径不受默认解析影响
