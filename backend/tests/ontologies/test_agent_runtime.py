"""本体智能体（agent_runtime）的边界与回合测试：

  1. 授权边界：白名单外的对象类型/动作不可见、不可用；链接两端有隐藏类型时链接也隐藏
  2. 工具行为：search 过滤 + 配额截断；aggregate 统计；history 溯源
  3. 动作治理：propose_action 永远 dry-run 不落数据；execute-proposal 才真实执行，
     requires_approval 的动作进 HITL pending 队列
  4. 回合编排：假 LLM 驱动 orchestrator 走「查询→回答」全链路，轨迹/引用被持久化
"""
import json
import uuid

import pytest


def test_large_visual_tool_result_keeps_structured_graph():
    from app.ontologies.agent_runtime.orchestrator import _audit_result, _display_result

    nodes = [{
        "id": f"instance:i-{index}",
        "entityId": f"i-{index}",
        "kind": "instance",
        "label": f"设备-{index}",
        "objectTypeId": "device",
        "objectTypeLabel": "设备",
        "preview": [{"name": "blob", "label": "大字段", "value": "x" * 300}],
    } for index in range(120)]
    result = {
        "kind": "impact",
        "mode": "association_only",
        "change": {"instanceId": "i-0", "property": "status"},
        "summary": {"related": 119, "direct": 10, "indirect": 109},
        "nodes": nodes,
        "edges": [{
            "id": f"link:l-{index}", "entityId": f"l-{index}", "kind": "relation",
            "source": f"instance:i-{index}", "target": f"instance:i-{index + 1}",
            "label": "关联", "properties": {"blob": "y" * 300},
        } for index in range(119)],
        "impacts": [{
            "instanceId": f"i-{index}", "label": f"设备-{index}", "objectType": "设备",
            "depth": 1 if index < 10 else 2,
            "classification": "direct" if index < 10 else "indirect",
            "certainty": "related",
            "path": {"nodeIds": ["i-0", f"i-{index}"], "edgeIds": [f"l-{index}"],
                     "steps": [{"blob": "z" * 300}], "hops": 1},
        } for index in range(1, 120)],
        "disclaimer": "只读关联范围",
    }

    displayed = _display_result(result)
    assert displayed["kind"] == "impact"
    assert isinstance(displayed["nodes"], list)
    assert isinstance(displayed["edges"], list)
    assert displayed["visualizationTruncated"] is True
    assert "preview" not in displayed["nodes"][0]
    assert "properties" not in displayed["edges"][0]
    assert "steps" not in displayed["impacts"][0]["path"]
    assert len(json.dumps(displayed, ensure_ascii=False)) <= 18000

    audited = _audit_result(result)
    assert len(audited["nodes"]) == 120
    assert len(audited["edges"]) == 119
    assert audited["nodes"][0]["preview"][0]["value"] == "x" * 300
    assert audited["impacts"][0]["path"]["steps"][0]["blob"] == "z" * 300


def test_agent_stream_can_close_without_yielding_after_generator_exit(monkeypatch):
    from app.ontologies.agent_runtime import orchestrator

    def fake_run(*_args, **_kwargs):
        yield {"type": "answer", "content": "ok"}
        yield {"type": "answer", "content": "unused"}

    monkeypatch.setattr(orchestrator, "_run", fake_run)
    stream = orchestrator.run_agent_turn(None, "ontology", None, "question")
    assert next(stream)["content"] == "ok"
    stream.close()  # 回归：关闭中的生成器不得再 yield done 或抛 RuntimeError


def _fo(ontology_id: str) -> str:
    return f"/api/v2/formal/ontologies/{ontology_id}"


@pytest.fixture
def modeled_ontology(client, auth_headers, ontology):
    """两个对象类型（订单/供应商）+ 链接 + 一个改状态动作 + 若干实例。"""
    oid = ontology["id"]
    body = {
        "objectTypes": [
            {"id": "ot-order", "name": "Order", "displayName": "订单", "primaryKey": "order_no",
             "properties": [
                 {"id": "p1", "name": "order_no", "displayName": "订单号", "type": "string", "required": True},
                 {"id": "p2", "name": "status", "displayName": "状态", "type": "string"},
                 {"id": "p3", "name": "amount", "displayName": "金额", "type": "number"},
             ], "positionX": 0, "positionY": 0},
            {"id": "ot-supplier", "name": "Supplier", "displayName": "供应商", "primaryKey": "sname",
             "properties": [
                 {"id": "p4", "name": "sname", "displayName": "名称", "type": "string", "required": True},
             ], "positionX": 0, "positionY": 0},
        ],
        "linkTypes": [
            {"id": "lt-1", "name": "order_supplier", "displayName": "订单-供应商",
             "sourceObjectTypeId": "ot-order", "targetObjectTypeId": "ot-supplier",
             "cardinality": "many-to-one"},
        ],
        "actions": [
            {"id": "act-1", "name": "mark_paid", "displayName": "标记已支付",
             "objectTypeId": "ot-order", "requiresApproval": False,
             "parameters": [{"name": "note", "displayName": "备注", "type": "string"}],
             "rules": [{"type": "update_property", "name": "set-status", "enabled": True,
                        "order": 0, "config": {"targetProperty": "status",
                                               "valueSource": "constant", "value": "\"paid\""}}]},
            {"id": "act-2", "name": "cancel_order", "displayName": "取消订单",
             "objectTypeId": "ot-order", "requiresApproval": True,
             "parameters": [],
             "rules": [{"type": "update_property", "name": "set-status", "enabled": True,
                        "order": 0, "config": {"targetProperty": "status",
                                               "valueSource": "constant", "value": "\"cancelled\""}}]},
        ],
        "functions": [],
        "instances": [
            {"id": "inst-o1", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-001", "status": "pending", "amount": 100}, "computed": {}},
            {"id": "inst-o2", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-002", "status": "paid", "amount": 250}, "computed": {}},
            {"id": "inst-s1", "objectTypeId": "ot-supplier",
             "properties": {"sname": "华南电子"}, "computed": {}},
        ],
        "linkInstances": [
            {"id": "li-1", "linkTypeId": "lt-1",
             "sourceObjectId": "inst-o1", "targetObjectId": "inst-s1"},
        ],
    }
    r = client.put(f"{_fo(oid)}/full", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    # 查询可使用草稿投影；真实动作测试会先把该结构冻结到当前 release。
    return ontology


# ---------------------------------------------------------------- 边界


def test_profile_defaults_deny_actions(client, auth_headers, modeled_ontology):
    oid = modeled_ontology["id"]
    r = client.get(f"{_fo(oid)}/agent/profile", headers=auth_headers)
    assert r.status_code == 200
    p = r.json()["data"]
    assert p["allowedActionIds"] == []            # 动作默认拒绝
    assert p["allowedObjectTypeIds"] is None      # 读默认全开

    caps = client.get(f"{_fo(oid)}/agent/capabilities", headers=auth_headers).json()["data"]
    assert len(caps["objectTypes"]) == 2
    assert caps["actions"] == []                  # 未授权 → 不可见
    assert "订单" in caps["skillCard"]


def test_scope_hides_unauthorized_types_and_links(client, auth_headers, modeled_ontology, db):
    oid = modeled_ontology["id"]
    # 只授权订单，不授权供应商 → 供应商隐藏，且链接因一端隐藏而隐藏
    r = client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
                   json={"allowedObjectTypeIds": ["ot-order"]})
    assert r.status_code == 200

    caps = client.get(f"{_fo(oid)}/agent/capabilities", headers=auth_headers).json()["data"]
    assert [t["id"] for t in caps["objectTypes"]] == ["ot-order"]
    assert caps["linkTypes"] == []
    assert "供应商" not in caps["skillCard"]

    from app.ontologies.agent_runtime.boundary import build_scope, ToolError
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    _, _, scope = build_scope(db, oid)
    runner = ToolRunner(db, scope)
    with pytest.raises(ToolError):
        runner.run("search_objects", {"object_type": "Supplier"})
    # 隐藏类型的实例连 get_object 也不行
    with pytest.raises(ToolError):
        runner.run("get_object", {"instance_id": "inst-s1"})


def test_reset_to_all(client, auth_headers, modeled_ontology):
    oid = modeled_ontology["id"]
    client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
               json={"allowedObjectTypeIds": ["ot-order"]})
    r = client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
                   json={"resetToAll": ["allowed_object_type_ids"]})
    assert r.json()["data"]["allowedObjectTypeIds"] is None


# ---------------------------------------------------------------- 工具


def test_search_filter_and_cap(client, auth_headers, modeled_ontology, db):
    oid = modeled_ontology["id"]
    from app.ontologies.agent_runtime.boundary import build_scope
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    _, profile, scope = build_scope(db, oid)

    runner = ToolRunner(db, scope)
    r = runner.run("search_objects", {
        "object_type": "订单",
        "filters": [{"property": "amount", "op": "gt", "value": 150}],
    })
    assert r["total"] == 1
    assert r["items"][0]["properties"]["order_no"] == "SO-002"
    assert runner.citations and runner.citations[0]["label"] == "SO-002"

    # 配额：limit 请求 999 也被 max_rows_per_query 压回
    profile.max_rows_per_query = 1
    db.commit()
    _, _, scope2 = build_scope(db, oid)
    r = ToolRunner(db, scope2).run("search_objects", {"object_type": "Order", "limit": 999})
    assert r["returned"] == 1 and r["truncated"] is True


def test_aggregate_and_history_and_traverse(client, auth_headers, modeled_ontology, db):
    oid = modeled_ontology["id"]
    from app.ontologies.agent_runtime.boundary import build_scope
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    _, _, scope = build_scope(db, oid)
    runner = ToolRunner(db, scope)

    agg = runner.run("aggregate_objects", {"object_type": "Order", "metric": "sum",
                                           "metric_property": "amount"})
    assert agg["value"] == 350

    grouped = runner.run("aggregate_objects", {"object_type": "Order", "metric": "count",
                                               "group_by": "status"})
    assert {g["group"]: g["value"] for g in grouped["groups"]} == {"pending": 1, "paid": 1}

    trav = runner.run("traverse_links", {"instance_id": "inst-o1",
                                         "link_type": "order_supplier"})
    assert trav["returned"] == 1
    assert trav["items"][0]["properties"]["sname"] == "华南电子"

    hist = runner.run("get_object_history", {"instance_id": "inst-o1"})
    assert any(f["property"] == "status" for f in hist["facts"])   # 保存时追加过事实
    assert all("present" in fact for fact in hist["facts"])


def test_progressive_graph_path_and_impact_preview(
        client, auth_headers, modeled_ontology, db):
    """数据图谱只读、受边界约束，并能返回可直接高亮的路径/影响结构。"""
    oid = modeled_ontology["id"]
    from app.models.ontology_formal import ObjectType, LinkType, ObjectInstance, LinkInstance

    risk_type = ObjectType(
        id="ot-risk", ontology_id=oid, name="Risk", display_name="风险",
        primary_key="risk_no",
        properties=[
            {"id": "rp1", "name": "risk_no", "displayName": "风险号",
             "type": "string", "required": True},
            {"id": "rp2", "name": "level", "displayName": "等级",
             "type": "string", "required": False},
        ],
    )
    risk_link_type = LinkType(
        id="lt-risk", ontology_id=oid, name="supplier_risk", display_name="供应风险",
        source_object_type_id="ot-supplier", target_object_type_id="ot-risk",
        cardinality="one-to-many", properties=[],
    )
    risk = ObjectInstance(
        id="inst-r1", ontology_id=oid, object_type_id="ot-risk",
        properties={"risk_no": "R-001", "level": "high"}, computed={}, source="manual",
    )
    risk_link = LinkInstance(
        id="li-risk", ontology_id=oid, link_type_id="lt-risk",
        source_object_id="inst-s1", target_object_id="inst-r1", properties={},
    )
    db.add_all([risk_type, risk_link_type, risk, risk_link])
    db.commit()

    level1 = client.get(
        f"{_fo(oid)}/agent/graph?depth=1", headers=auth_headers)
    assert level1.status_code == 200, level1.text
    graph1 = level1.json()["data"]
    assert {node["kind"] for node in graph1["nodes"]} == {"object_type"}
    assert graph1["meta"]["matchedInstances"] == 4

    level2 = client.get(
        f"{_fo(oid)}/agent/graph?depth=2&query=SO-001&limit_per_type=5",
        headers=auth_headers,
    )
    assert level2.status_code == 200, level2.text
    graph2 = level2.json()["data"]
    instance_nodes = [node for node in graph2["nodes"] if node["kind"] == "instance"]
    assert [node["entityId"] for node in instance_nodes] == ["inst-o1"]
    assert graph2["meta"]["matchedInstances"] == 1

    level3 = client.get(
        f"{_fo(oid)}/agent/graph?depth=3&focus_instance_id=inst-o1",
        headers=auth_headers,
    )
    assert level3.status_code == 200, level3.text
    graph3 = level3.json()["data"]
    assert any(node["kind"] == "property" and node["propertyName"] == "status"
               for node in graph3["nodes"])

    detail = client.get(
        f"{_fo(oid)}/agent/graph/instances/inst-o1", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["properties"]["status"] == "pending"

    paths = client.post(
        f"{_fo(oid)}/agent/graph/paths", headers=auth_headers,
        json={"sourceInstanceId": "inst-o1", "targetInstanceId": "inst-r1",
              "direction": "both", "maxDepth": 4, "maxPaths": 3},
    )
    assert paths.status_code == 200, paths.text
    path_data = paths.json()["data"]
    assert path_data["found"] is True
    assert path_data["paths"][0]["nodeIds"] == ["inst-o1", "inst-s1", "inst-r1"]
    assert path_data["paths"][0]["hops"] == 2

    impact = client.post(
        f"{_fo(oid)}/agent/graph/impact", headers=auth_headers,
        json={"instanceId": "inst-o1", "property": "status",
              "proposedValue": "cancelled", "direction": "both", "maxDepth": 3},
    )
    assert impact.status_code == 200, impact.text
    impact_data = impact.json()["data"]
    assert impact_data["mode"] == "association_only"
    assert impact_data["summary"] == {"related": 2, "direct": 1, "indirect": 1}
    assert {item["classification"] for item in impact_data["impacts"]} == {"direct", "indirect"}
    db.refresh(db.query(ObjectInstance).filter(ObjectInstance.id == "inst-o1").one())
    unchanged = db.query(ObjectInstance).filter(ObjectInstance.id == "inst-o1").one()
    assert unchanged.properties["status"] == "pending"  # 预演绝不落真实变更


def test_graph_tools_respect_agent_scope(client, auth_headers, modeled_ontology, db):
    oid = modeled_ontology["id"]
    from app.models.ontology_formal import LinkInstance, ObjectInstance, ObjectType

    # 防御历史脏数据：允许的链接类型也可能被旧数据错误地指向隐藏类型，路径层不得泄露。
    db.add(ObjectType(
        id="ot-hidden", ontology_id=oid, name="HiddenRisk", display_name="隐藏风险",
        properties=[{"name": "name", "displayName": "名称", "type": "string"}],
    ))
    db.add(ObjectInstance(
        id="inst-hidden", ontology_id=oid, object_type_id="ot-hidden",
        properties={"name": "不可见"}, computed={},
    ))
    db.add(LinkInstance(
        id="li-corrupt", ontology_id=oid, link_type_id="lt-1",
        source_object_id="inst-o2", target_object_id="inst-hidden", properties={},
    ))
    db.commit()
    client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
               json={"allowedObjectTypeIds": ["ot-order", "ot-supplier"]})
    dirty_impact = client.post(
        f"{_fo(oid)}/agent/graph/impact", headers=auth_headers,
        json={"instanceId": "inst-o2", "property": "status", "proposedValue": "review"},
    )
    assert dirty_impact.status_code == 200
    dirty_data = dirty_impact.json()["data"]
    assert dirty_data["summary"]["related"] == 0
    assert "inst-hidden" not in json.dumps(dirty_data)

    client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
               json={"allowedObjectTypeIds": ["ot-order"]})

    graph = client.get(f"{_fo(oid)}/agent/graph?depth=2", headers=auth_headers)
    assert graph.status_code == 200
    nodes = graph.json()["data"]["nodes"]
    assert all(node.get("objectTypeId") == "ot-order" for node in nodes)

    path = client.post(
        f"{_fo(oid)}/agent/graph/paths", headers=auth_headers,
        json={"sourceInstanceId": "inst-o1", "targetInstanceId": "inst-s1"},
    )
    assert path.status_code == 422
    assert "不在授权范围" in path.text


# ---------------------------------------------------------------- 动作治理


def _grant_actions(client, auth_headers, oid, ids):
    r = client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
                   json={"allowedActionIds": ids})
    assert r.status_code == 200


def _freeze_modeled_current_release(db, ontology_id: str) -> str:
    """Turn the fixture's live draft projection into its immutable v0 baseline."""
    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import ObjectInstance
    from app.models.ontology_version import OntologyVersion
    from app.ontologies.versions.evolution_service import snapshot_hash
    from app.ontologies.versions.router import _snapshot_formal

    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    snapshot = _snapshot_formal(db, ontology_id)
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    for instance in db.query(ObjectInstance).filter_by(
            ontology_id=ontology_id).all():
        instance.ontology_release_id = release.id
    db.commit()
    return str(release.id)


def test_propose_is_dry_run_only(client, auth_headers, modeled_ontology, db):
    oid = modeled_ontology["id"]
    _grant_actions(client, auth_headers, oid, ["act-1"])
    release_id = _freeze_modeled_current_release(db, oid)

    from app.ontologies.agent_runtime.boundary import build_scope, ToolError
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    _, _, scope = build_scope(db, oid, release_id=release_id)
    runner = ToolRunner(db, scope)

    r = runner.run("propose_action", {"action": "mark_paid",
                                      "target_instance_id": "inst-o1",
                                      "parameters": {"note": "ok"}})
    assert r["proposal"]["status"] == "success"
    assert runner.proposals and runner.proposals[0]["actionId"] == "act-1"

    # dry-run 不落变更：状态仍是 pending
    from app.models.ontology_formal import ObjectInstance
    inst = db.query(ObjectInstance).filter(ObjectInstance.id == "inst-o1").first()
    db.refresh(inst)
    assert inst.properties["status"] == "pending"

    # 白名单外的动作提案被拒
    with pytest.raises(ToolError):
        runner.run("propose_action", {"action": "cancel_order",
                                      "target_instance_id": "inst-o1"})


def test_execute_proposal_respects_boundary_and_hitl(client, auth_headers, modeled_ontology, db):
    oid = modeled_ontology["id"]
    _grant_actions(client, auth_headers, oid, ["act-1", "act-2"])
    release_id = _freeze_modeled_current_release(db, oid)

    from app.models.ontology import OntologyProject
    project = db.query(OntologyProject).filter_by(id=oid).one()
    assert project.status == "draft"

    # 普通动作：真实执行 → 属性变更 + 事实追加
    r = client.post(f"{_fo(oid)}/agent/execute-proposal", headers=auth_headers,
                    json={"actionId": "act-1", "targetInstanceId": "inst-o1",
                          "parameters": {"note": "confirmed"},
                          "releaseId": release_id})
    assert r.status_code == 200
    log = r.json()["data"]
    assert log["status"] == "success"
    assert log["actorId"]                      # 执行者 = 确认的用户

    from app.models.ontology_formal import ObjectInstance
    inst = db.query(ObjectInstance).filter(ObjectInstance.id == "inst-o1").first()
    db.refresh(inst)
    assert inst.properties["status"] == "paid"

    # 需审批动作 → pending，进 HITL 队列，属性不变
    r = client.post(f"{_fo(oid)}/agent/execute-proposal", headers=auth_headers,
                    json={"actionId": "act-2", "targetInstanceId": "inst-o1",
                          "releaseId": release_id})
    pending = r.json()["data"]
    assert pending["status"] == "pending"
    db.refresh(inst)
    assert inst.properties["status"] == "paid"     # 没被取消

    # 审批通过后仍按提案记录的同一发布节点继续执行。
    approved = client.post(
        f"{_fo(oid)}/action-logs/{pending['id']}/decide",
        headers=auth_headers,
        json={"decision": "approved", "reason": "版本一致，确认执行",
              "releaseId": release_id},
    )
    assert approved.status_code == 200, approved.text
    decision = approved.json()["data"]
    assert decision["pendingLog"]["status"] == "approved"
    assert decision["executionLog"]["status"] == "success"

    # 边界外动作 → 403
    _grant_actions(client, auth_headers, oid, ["act-1"])
    r = client.post(f"{_fo(oid)}/agent/execute-proposal", headers=auth_headers,
                    json={"actionId": "act-2", "targetInstanceId": "inst-o1",
                          "releaseId": release_id})
    assert r.status_code == 403


def test_direct_action_execution_uses_current_release(
        client, auth_headers, modeled_ontology, db):
    """通用动作入口只执行当前不可变发布快照中的动作。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_modeled_current_release(db, oid)

    response = client.post(
        f"{_fo(oid)}/run-action",
        headers=auth_headers,
        json={"actionId": "act-1", "targetInstanceId": "inst-o1",
              "dryRun": False, "releaseId": release_id},
    )

    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "success"
    assert result["ontologyVersion"] == "v0"

    from app.models.ontology_formal import ObjectInstance
    instance = db.query(ObjectInstance).filter_by(id="inst-o1").one()
    db.refresh(instance)
    assert instance.properties["status"] == "paid"


# ---------------------------------------------------------------- 回合编排


def test_chat_turn_with_fake_llm(client, auth_headers, modeled_ontology, db,
                                 admin_user, monkeypatch):
    """假 LLM：第一轮要求查订单，第二轮给出答案 → 验证轨迹、引用、持久化。"""
    oid = modeled_ontology["id"]

    # 造一个可用的模型配置（不会真的被调用）
    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name="fake", provider="openai",
                       config_type="llm", models=["fake-model"],
                       created_by=admin_user.id))
    db.commit()

    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            assert any(t["name"] == "search_objects" for t in tools)
            assert messages[0]["role"] == "system" and "订单" in messages[0]["content"]
            return {"content": None, "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "search_objects",
                                    "arguments": {"object_type": "Order",
                                                  "filters": [{"property": "status", "op": "eq",
                                                               "value": "pending"}]}}]}
        # 第二轮能看到工具结果
        assert messages[-1]["role"] == "tool" and "SO-001" in messages[-1]["content"]
        return {"content": "待支付订单只有 SO-001。", "tool_calls": [],
                "usage": {"inputTokens": 20, "outputTokens": 8}}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{_fo(oid)}/agent/chat", headers=auth_headers,
                    json={"message": "哪些订单还没支付？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert data["content"] == "待支付订单只有 SO-001。"
    assert len(data["steps"]) == 1 and data["steps"][0]["tool"] == "search_objects"
    assert data["citations"][0]["label"] == "SO-001"
    assert data["usage"]["outputTokens"] == 13

    # 会话与消息持久化，轨迹可审计
    conv_id = data["conversationId"]
    r = client.get(f"{_fo(oid)}/agent/conversations/{conv_id}", headers=auth_headers)
    msgs = r.json()["data"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["steps"][0]["tool"] == "search_objects"

    r = client.get(
        f"{_fo(oid)}/agent/conversations/{conv_id}/export",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    exported = r.json()["data"]
    assert exported["schemaVersion"] == "openontology.agent-conversation.v1"
    assert exported["conversation"]["id"] == conv_id
    assert exported["ontology"]["id"] == oid
    assert [message["role"] for message in exported["messages"]] == ["user", "assistant"]
    assert exported["messages"][1]["steps"][0]["tool"] == "search_objects"
    assert exported["messages"][1]["model"] == "fake-model"
    assert exported["messages"][1]["tokenUsage"]["outputTokens"] == 13
    assert exported["summary"]["messageCount"] == 2
    assert exported["summary"]["toolStepCount"] == 1
    assert exported["summary"]["contentCompleteness"]["messageHistory"] == "complete"

    r = client.get(f"{_fo(oid)}/agent/conversations", headers=auth_headers)
    assert any(c["id"] == conv_id for c in r.json()["data"])


def test_chat_turn_empty_llm_response_falls_back_to_notice(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """content 与 tool_calls 双空（网关降级重试后仍无产出）→ 编排器兜底文本，
    不抛错、不产生 step。生产 GLM MaaS tool_calls 缺失缺陷的残余路径。"""
    oid = modeled_ontology["id"]

    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name="fake", provider="openai",
                       config_type="llm", models=["fake-model"],
                       created_by=admin_user.id))
    db.commit()

    def fake_chat(call_kwargs, messages, tools):
        return {"content": None, "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{_fo(oid)}/agent/chat", headers=auth_headers,
                    json={"message": "有哪些实例？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert data["content"] == "（模型未给出回答）"
    assert data["steps"] == []


def test_conversation_export_is_not_limited_to_history_replay_window(
    client, auth_headers, modeled_ontology, db, admin_user,
):
    from app.ontologies.agent_runtime.models import AgentConversation, AgentMessage

    conversation = AgentConversation(
        ontology_id=modeled_ontology["id"],
        user_id=admin_user.id,
        title="超长审计会话",
    )
    db.add(conversation)
    db.flush()
    db.add_all([
        AgentMessage(
            id=f"message-{index:03d}",
            conversation_id=conversation.id,
            role="user" if index % 2 == 0 else "assistant",
            content=f"消息 {index}",
        )
        for index in range(205)
    ])
    db.commit()

    detail = client.get(
        f"{_fo(modeled_ontology['id'])}/agent/conversations/{conversation.id}",
        headers=auth_headers,
    )
    assert detail.status_code == 200
    assert len(detail.json()["data"]["messages"]) == 200

    exported = client.get(
        f"{_fo(modeled_ontology['id'])}/agent/conversations/{conversation.id}/export",
        headers=auth_headers,
    )
    assert exported.status_code == 200, exported.text
    payload = exported.json()["data"]
    assert len(payload["messages"]) == 205
    assert payload["summary"]["messageCount"] == 205
    assert payload["messages"][0]["content"] == "消息 0"
    assert payload["messages"][-1]["content"] == "消息 204"


def test_chat_without_model_config(client, auth_headers, modeled_ontology):
    oid = modeled_ontology["id"]
    r = client.post(f"{_fo(oid)}/agent/chat", headers=auth_headers,
                    json={"message": "你好", "stream": False})
    assert r.status_code == 200
    assert "模型配置" in (r.json()["data"]["error"] or "")


def test_disabled_agent(client, auth_headers, modeled_ontology):
    oid = modeled_ontology["id"]
    client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers, json={"enabled": False})
    r = client.post(f"{_fo(oid)}/agent/chat", headers=auth_headers,
                    json={"message": "你好", "stream": False})
    assert "停用" in (r.json()["data"]["error"] or "")


# ---------------------------------------------------------------- 分析报告能力


def test_analysis_report_template_preview_publish_and_run(
        client, auth_headers, modeled_ontology, db, admin_user, editor_user, monkeypatch):
    """AI 草稿可编辑；真实数据试运行绑定 revision；质量门通过后才能发布并复跑。"""
    oid = modeled_ontology["id"]
    from app.models.model_config import ModelConfig
    model = ModelConfig(id=str(uuid.uuid4()), name="report-fake", provider="openai",
                        config_type="llm", models=["report-fake-model"],
                        created_by=admin_user.id)
    db.add(model)
    db.commit()

    def fake_chat(call_kwargs, messages, tools):
        prompt = messages[-1]["content"]
        if "生成一份可编辑" in prompt:
            return {"content": """{
              "name": "订单经营分析报告",
              "description": "用于管理层核对订单规模与状态结构。",
              "sections": [
                {"id":"order-scale","title":"订单规模","goal":"统计订单总量并解释当前业务规模。","visualization":"kpi","queryPlan":[{"tool":"aggregate_objects","arguments":{"object_type":"Order","metric":"count"}},{"tool":"aggregate_objects","arguments":{"object_type":"Order","metric":"sum","metric_property":"amount"}}]},
                {"id":"order-status","title":"订单状态结构","goal":"按状态分析订单分布与集中情况。","visualization":"bar","queryPlan":[{"tool":"aggregate_objects","arguments":{"object_type":"Order","metric":"count","group_by":"status"}}]}
              ]
            }""", "tool_calls": [], "usage": None}
        assert "为每个报告章节" in prompt
        return {"content": """{
          "order-scale":"当前共有2笔订单。该指标给出了本次真实数据范围内的业务规模基线，可作为后续状态结构分析的分母。",
          "order-status":"待支付与已支付订单各1笔，当前状态分布均衡。样本量仍较小，汇报时应避免将这一结构外推为长期趋势。"
        }""", "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    base = f"{_fo(oid)}/agent/report-templates"
    created = client.post(
        f"{base}/ai-draft", headers=auth_headers,
        json={"brief": "生成订单规模和状态结构的管理层汇报", "modelId": model.id},
    )
    assert created.status_code == 201, created.text
    template = created.json()["data"]
    assert template["status"] == "draft" and template["generationMode"] == "ai"
    assert len(template["sections"]) == 2

    blocked = client.post(f"{base}/{template['id']}/publish", headers=auth_headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "report_preview_required"

    preview = client.post(
        f"{base}/{template['id']}/preview", headers=auth_headers,
        json={"modelId": model.id},
    )
    assert preview.status_code == 200, preview.text
    run = preview.json()["data"]
    assert run["status"] == "succeeded"
    assert run["qualityReport"]["passed"] is True
    assert run["qualityReport"]["score"] >= 80
    assert "订单经营分析报告" in run["htmlContent"]
    assert "订单状态结构" in run["htmlContent"]
    assert "核心指标数据" in run["htmlContent"] and ">350<" in run["htmlContent"]
    assert "<script" not in run["htmlContent"].lower()

    # 试运行后修改模板：旧预览立即失效，发布必须再取一次真实数据。
    template["description"] = "更新后的汇报说明"
    updated = client.put(
        f"{base}/{template['id']}", headers=auth_headers,
        json={
            "expectedRevision": template["revision"],
            "name": template["name"], "description": template["description"],
            "sections": template["sections"], "style": template["style"],
            "defaultModelId": model.id,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["revision"] == template["revision"] + 1
    assert updated.json()["data"]["lastPreviewRunId"] is None
    stale = client.put(
        f"{base}/{template['id']}", headers=auth_headers,
        json={
            "expectedRevision": template["revision"],
            "name": template["name"], "description": "过期页面的覆盖尝试",
            "sections": template["sections"], "style": template["style"],
            "defaultModelId": model.id,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "report_revision_conflict"
    assert client.post(f"{base}/{template['id']}/publish", headers=auth_headers).status_code == 409

    preview2 = client.post(
        f"{base}/{template['id']}/preview", headers=auth_headers,
        json={"modelId": model.id},
    )
    assert preview2.status_code == 200 and preview2.json()["data"]["qualityReport"]["passed"]
    published = client.post(f"{base}/{template['id']}/publish", headers=auth_headers)
    assert published.status_code == 200, published.text
    assert published.json()["data"]["status"] == "published"

    formal_run = client.post(
        f"{base}/{template['id']}/runs", headers=auth_headers,
        json={"modelId": model.id},
    )
    assert formal_run.status_code == 200, formal_run.text
    assert formal_run.json()["data"]["triggerType"] == "manual"
    assert formal_run.json()["data"]["status"] == "succeeded"
    run_id = formal_run.json()["data"]["id"]
    history = client.get(f"{base}/{template['id']}/runs", headers=auth_headers)
    assert history.status_code == 200
    assert history.json()["data"][0]["htmlContent"] == ""
    html_response = client.get(f"{_fo(oid)}/agent/report-runs/{run_id}/html", headers=auth_headers)
    assert html_response.status_code == 200
    assert "default-src 'none'" in html_response.headers["content-security-policy"]

    # 正式模板可复用，但运行历史按发起者隔离，列表与详情权限一致。
    editor_login = client.post(
        "/api/v1/auth/login", json={"username": "editor", "password": "editor123"})
    editor_headers = {
        "Authorization": f"Bearer {editor_login.json()['data']['access_token']}"}
    editor_history = client.get(
        f"{base}/{template['id']}/runs", headers=editor_headers)
    assert editor_history.status_code == 200
    assert editor_history.json()["data"] == []
    assert client.get(
        f"{_fo(oid)}/agent/report-runs/{run_id}", headers=editor_headers).status_code == 403

    editor_run = client.post(
        f"{base}/{template['id']}/runs", headers=editor_headers,
        json={"modelId": model.id},
    )
    assert editor_run.status_code == 200, editor_run.text
    own_editor_history = client.get(
        f"{base}/{template['id']}/runs", headers=editor_headers).json()["data"]
    assert [item["id"] for item in own_editor_history] == [editor_run.json()["data"]["id"]]


def test_analysis_report_quality_gate_and_readonly_query_guard(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """单章节低质量模板不能发布；报告查询计划不能混入写动作。"""
    oid = modeled_ontology["id"]
    from app.models.model_config import ModelConfig
    model = ModelConfig(id=str(uuid.uuid4()), name="quality-fake", provider="openai",
                        config_type="llm", models=["quality-fake-model"],
                        created_by=admin_user.id)
    db.add(model)
    db.commit()

    def fake_chat(call_kwargs, messages, tools):
        prompt = messages[-1]["content"]
        if "生成一份可编辑" in prompt:
            return {"content": """{
              "name":"订单快照","description":"单指标草稿",
              "sections":[{"id":"only","title":"订单总量","goal":"统计订单总量。","visualization":"kpi","queryPlan":[{"tool":"aggregate_objects","arguments":{"object_type":"Order","metric":"count"}}]}]
            }""", "tool_calls": [], "usage": None}
        return {"content": "{\"only\":\"当前共有2笔订单，本节只提供单一规模指标，尚不足以支撑完整的管理层汇报。\"}",
                "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    base = f"{_fo(oid)}/agent/report-templates"
    created = client.post(
        f"{base}/ai-draft", headers=auth_headers,
        json={"brief": "只生成订单总量快照用于验证质量门", "modelId": model.id},
    ).json()["data"]
    preview = client.post(
        f"{base}/{created['id']}/preview", headers=auth_headers,
        json={"modelId": model.id},
    ).json()["data"]
    assert preview["qualityReport"]["passed"] is False
    assert any("至少需要两个" in item for item in preview["qualityReport"]["blockers"])
    publish = client.post(f"{base}/{created['id']}/publish", headers=auth_headers)
    assert publish.status_code == 422
    assert publish.json()["detail"]["code"] == "report_quality_gate_blocked"

    invalid_sections = [{
        "id": "unsafe", "title": "危险章节", "goal": "不应执行写动作。", "visualization": "none",
        "queryPlan": [{"tool": "propose_action", "arguments": {"action": "mark_paid"}}],
    }]
    invalid = client.put(
        f"{base}/{created['id']}", headers=auth_headers,
        json={"expectedRevision": created["revision"], "name": "危险模板", "description": "", "sections": invalid_sections, "style": {}},
    )
    assert invalid.status_code == 422
    assert "只允许" in invalid.text

    wrong_contract = [{
        "id": "wrong-contract", "title": "错误聚合参数", "goal": "验证 AI 参数结构会被提前拦截。",
        "visualization": "bar",
        "queryPlan": [{"tool": "aggregate_objects", "arguments": {
            "object": "Order", "aggregations": [{"field": "amount", "function": "sum"}],
        }}],
    }]
    rejected = client.put(
        f"{base}/{created['id']}", headers=auth_headers,
        json={"expectedRevision": created["revision"], "name": "错误参数模板",
              "description": "", "sections": wrong_contract, "style": {}},
    )
    assert rejected.status_code == 422
    assert "未知参数" in rejected.text


def test_analysis_report_quality_gate_rejects_empty_real_data():
    """查询本身成功但没有任何可分析数据时，不能以“技术成功”冒充汇报质量。"""
    from app.ontologies.agent_runtime.reporting import evaluate_quality

    sections = [{
        "id": f"empty-{index}", "title": f"空数据章节{index}", "goal": "核对真实数据是否存在。",
        "visualization": "table", "narrative": "当前数据不足，无法形成可靠的管理层分析结论。",
        "queries": [{"tool": "search_objects", "arguments": {}, "result": {"items": [], "total": 0}}],
        "chart": None,
    } for index in range(2)]

    quality = evaluate_quality({"revision": 1}, sections)
    assert quality["passed"] is False
    assert quality["score"] == 50
    assert any("没有取得可用于分析" in item for item in quality["blockers"])
    substance = next(item for item in quality["checks"] if item["key"] == "substance")
    assert substance["passed"] is False


def test_analysis_report_style_tokens_are_strictly_normalized():
    """渲染样式不接受字符串伪装的布尔值，也不持久化未知外观令牌。"""
    from app.ontologies.agent_runtime.reporting import normalize_style

    style = normalize_style({
        "theme": "untrusted-theme", "accent": "scripted",
        "density": "compressed", "showSources": "false",
    })
    assert style == {
        "theme": "editorial-light", "accent": "teal",
        "density": "comfortable", "showSources": True,
    }


# ---------------------------------------------------------------- 多跳遍历 & 护栏


def test_traverse_path_multi_hop(client, auth_headers, modeled_ontology, db):
    """2 跳：订单 →(order_supplier)→ 供应商 →(supplier_customer)→ 客户。"""
    oid = modeled_ontology["id"]
    from app.models.ontology_formal import ObjectType, LinkType, ObjectInstance, LinkInstance
    db.add(ObjectType(id="ot-customer", ontology_id=oid, name="Customer", display_name="客户",
                      primary_key="cname",
                      properties=[{"id": "pc", "name": "cname", "displayName": "客户名", "type": "string"}]))
    db.add(LinkType(id="lt-2", ontology_id=oid, name="supplier_customer", display_name="供应商-客户",
                    source_object_type_id="ot-supplier", target_object_type_id="ot-customer",
                    cardinality="one-to-many"))
    db.add(ObjectInstance(id="inst-c1", ontology_id=oid, object_type_id="ot-customer",
                          properties={"cname": "张三"}, computed={}))
    db.add(LinkInstance(id="li-2", ontology_id=oid, link_type_id="lt-2",
                        source_object_id="inst-s1", target_object_id="inst-c1"))
    db.commit()

    from app.ontologies.agent_runtime.boundary import build_scope, ToolError
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    _, _, scope = build_scope(db, oid)
    runner = ToolRunner(db, scope)

    res = runner.run("traverse_path", {"instance_id": "inst-o1", "path": [
        {"link_type": "order_supplier", "direction": "out"},
        {"link_type": "supplier_customer", "direction": "out"},
    ]})
    assert res["returned"] == 1
    assert res["items"][0]["properties"]["cname"] == "张三"
    assert len(res["hops"]) == 2 and res["hops"][-1]["reached"] == 1
    assert runner.citations[-1]["label"] == "张三"

    # 缰绳：超过最大跳数 → 硬失败
    with pytest.raises(ToolError):
        runner.run("traverse_path", {"instance_id": "inst-o1",
                                     "path": [{"link_type": "order_supplier"}] * 6})

    # 边界：隐藏供应商 → 经其的链接不可见，多跳越界即失败（防经链接泄露隐藏类型）
    client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
               json={"allowedObjectTypeIds": ["ot-order"]})
    _, _, scope2 = build_scope(db, oid)
    with pytest.raises(ToolError):
        ToolRunner(db, scope2).run("traverse_path", {"instance_id": "inst-o1",
                                   "path": [{"link_type": "order_supplier"}]})


def test_property_scope_guard(client, auth_headers, modeled_ontology, db):
    """越界属性硬失败：拼错/臆造的属性名当场报错，而非静默算错。"""
    oid = modeled_ontology["id"]
    from app.ontologies.agent_runtime.boundary import build_scope, ToolError
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    _, _, scope = build_scope(db, oid)
    runner = ToolRunner(db, scope)

    with pytest.raises(ToolError):
        runner.run("aggregate_objects", {"object_type": "Order", "metric": "count",
                                         "group_by": "bogus_prop"})
    with pytest.raises(ToolError):
        runner.run("search_objects", {"object_type": "Order",
                                      "filters": [{"property": "nope", "op": "eq", "value": 1}]})

    # displayName 也能解析（状态 → status），并正常分组 + 附确定性图表
    grouped = runner.run("aggregate_objects", {"object_type": "订单", "metric": "count",
                                               "group_by": "状态"})
    assert {g["group"]: g["value"] for g in grouped["groups"]} == {"pending": 1, "paid": 1}
    assert grouped["partial"] is False and grouped["scanned"] == 2


# ---------------------------------------------------------------- 决策推演


def test_decision_simulation_repairs_only_json_syntax():
    from app.ontologies.decision_simulation.service import _json_object

    assert _json_object('```json\n{"title":"方案比较" "items":[1,2]}\n```') == {
        "title": "方案比较", "items": [1, 2],
    }


def test_decision_simulation_tool_is_isolated_auditable_and_queryable(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """多视角推演只写独立运行记录，不改变本体对象，并能由工作台 API 回放。"""
    oid = modeled_ontology["id"]

    def fake_chat(call_kwargs, messages, tools):
        system = messages[0]["content"]
        if "场景编译器" in system:
            content = {
                "title": "订单处理策略推演",
                "horizon": "未来两周",
                "alternatives": [
                    {"label": "维持现状", "description": "不改变当前处理方式"},
                    {"label": "提前分流", "description": "提前处理可能积压的订单"},
                ],
                "objectives": [
                    {"id": "outcome", "label": "处理效果", "weight": 0.5},
                    {"id": "risk", "label": "风险可控", "weight": 0.5},
                ],
                "constraints": ["不能直接修改真实订单"],
                "uncertainties": ["未来订单量未知"],
                "dataQuestions": [],
            }
        elif "决策委员会的记录员" in system:
            content = {
                "summary": "当前快照下可优先小范围验证提前分流。",
                "rationale": ["保守综合分更高，但仍存在数据缺口。"],
                "tradeoffs": ["会增加短期操作成本"],
                "noRegretActions": ["先选择少量订单进行可回滚试行"],
                "earlySignals": ["待处理订单数量连续上升"],
                "stopConditions": ["错误率超过现状基线时停止"],
            }
        else:
            risk_role = "风险挑战视角" in system
            content = {
                "stance": "谨慎支持提前分流" if not risk_role else "需先控制执行风险",
                "keyFindings": ["当前有订单实例可作为基线"],
                "challenges": ["当前快照不能证明未来需求"],
                "optionAssessments": [
                    {
                        "optionId": "option_1",
                        "scores": {"outcome": 55 if not risk_role else 72,
                                   "risk": 70 if not risk_role else 80},
                        "rationale": "不增加执行动作，但可能延续积压。",
                        "evidenceRefs": ["订单/状态"],
                        "assumptions": ["未来结构与当前近似"],
                        "risks": ["响应不足"],
                    },
                    {
                        "optionId": "option_2",
                        "scores": {"outcome": 82 if not risk_role else 68,
                                   "risk": 72 if not risk_role else 60},
                        "rationale": "可能改善处理效果，但需要受控试行。",
                        "evidenceRefs": ["订单/状态", "订单/金额"],
                        "assumptions": ["分流资源可用"],
                        "risks": ["短期操作成本"],
                    },
                ],
                "scenarioOutlooks": [{
                    "name": "需求上行情景",
                    "trigger": "待处理订单持续增加",
                    "impacts": {"option_1": "积压风险扩大", "option_2": "有机会缓冲积压"},
                    "earlySignals": ["待处理订单数量连续上升"],
                }],
            }
        return {"content": json.dumps(content, ensure_ascii=False),
                "tool_calls": [], "usage": {"inputTokens": 10, "outputTokens": 20}}

    from app.ontologies.agent_runtime import llm_bridge
    from app.ontologies.agent_runtime.boundary import build_scope
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    from app.ontologies.decision_simulation.models import DecisionSimulationRun
    from app.models.ontology_formal import ObjectInstance

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    _, _, scope = build_scope(db, oid)
    before = dict(db.query(ObjectInstance).filter(ObjectInstance.id == "inst-o1").one().properties)
    runner = ToolRunner(db, scope, decision_context={
        "created_by": admin_user.id,
        "conversation_id": None,
        "model_config_id": "fake-config",
        "call_kwargs": {"model": "fake-decision-model", "provider": "openai"},
    })

    result = runner.run("run_decision_simulation", {
        "question": "推演未来两周订单处理应该维持现状还是提前分流",
        "alternatives": ["维持现状", "提前分流"],
        "horizon": "未来两周",
    })

    assert result["kind"] == "decision_simulation"
    assert result["status"] == "succeeded"
    assert result["perspectiveCount"] == 4
    assert result["recommendedOption"] == "提前分流"
    run = db.query(DecisionSimulationRun).filter(
        DecisionSimulationRun.id == result["runId"]).one()
    assert run.snapshot["isolation"] == "read_only_release_snapshot"
    assert len(run.snapshot["checksum"]) == 64
    assert run.evaluation["method"]["probability"] is False
    assert run.recommendation["nature"] == "exploratory_decision_support"
    assert run.diagnostics["usage"] == {"inputTokens": 60, "outputTokens": 120}
    assert db.query(ObjectInstance).filter(
        ObjectInstance.id == "inst-o1").one().properties == before

    listed = client.get(
        f"{_fo(oid)}/agent/decision-simulations", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["id"] == run.id
    detail = client.get(
        f"{_fo(oid)}/agent/decision-simulations/{run.id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["recommendation"]["recommendedOption"] == "提前分流"
