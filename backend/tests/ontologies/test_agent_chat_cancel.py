"""运行取消（chat_cancel 注册表 + 编排器协作式取消 + 取消端点）测试。"""
import uuid

from app.ontologies.agent_runtime.chat_cancel import chat_cancel_registry


def _fo(oid: str) -> str:
    return f"/api/v2/formal/ontologies/{oid}"


class TestCancelRegistry:
    def test_lifecycle(self):
        run_id = "run-lifecycle"
        chat_cancel_registry.register(run_id)
        assert chat_cancel_registry.is_cancelled(run_id) is False
        assert run_id in chat_cancel_registry.active_runs()
        assert chat_cancel_registry.request_cancel(run_id) is True
        assert chat_cancel_registry.is_cancelled(run_id) is True
        chat_cancel_registry.unregister(run_id)
        assert run_id not in chat_cancel_registry.active_runs()

    def test_unknown_run_cancel_reports_false(self):
        assert chat_cancel_registry.request_cancel("run-absent") is False

    def test_re_register_resets_cancel_flag(self):
        run_id = "run-reregister"
        chat_cancel_registry.register(run_id)
        chat_cancel_registry.request_cancel(run_id)
        chat_cancel_registry.register(run_id)
        assert chat_cancel_registry.is_cancelled(run_id) is False
        chat_cancel_registry.unregister(run_id)


def test_cancel_endpoint_for_unknown_run(client, auth_headers, modeled_ontology):
    oid = modeled_ontology["id"]
    r = client.post(
        f"{_fo(oid)}/agent/chat/cancel", headers=auth_headers,
        json={"runId": "run-that-does-not-exist"})
    assert r.status_code == 404, r.text


def test_cancel_endpoint_rejects_empty_run_id(client, auth_headers, modeled_ontology):
    oid = modeled_ontology["id"]
    r = client.post(
        f"{_fo(oid)}/agent/chat/cancel", headers=auth_headers,
        json={"runId": ""})
    assert r.status_code == 422


def test_cancel_mid_turn_stops_and_persists(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """假 LLM 第二轮返回前用户发起取消 → 回合以 cancelled 终止，落 [已取消] 消息。"""
    oid = modeled_ontology["id"]

    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name="cancel-fake", provider="openai",
                       config_type="llm", models=["cancel-fake-model"],
                       created_by=admin_user.id))
    db.commit()

    run_id = "run-cancel-integration"
    calls = {"n": 0}

    from app.ontologies.agent_runtime import llm_bridge

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None,
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "search_objects",
                                    "arguments": {"object_type": "Order"}}]}
        # 第二次模型调用返回前：模拟用户在界面点击停止
        assert chat_cancel_registry.request_cancel(run_id) is True
        return {"content": None,
                "usage": {"inputTokens": 10, "outputTokens": 5},
                "tool_calls": [{"id": "tc2", "name": "search_objects",
                                "arguments": {"object_type": "Order"}}]}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "这个回合会被取消", "stream": False, "runId": run_id})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert data["content"] is None  # 取消回合没有终答
    assert calls["n"] == 2           # 第二轮模型调用后立刻检查取消标志
    assert chat_cancel_registry.is_cancelled(run_id) is False  # 回合结束即注销
    assert run_id not in chat_cancel_registry.active_runs()

    # 审计：user + [已取消] assistant 消息，第一轮已执行的工具轨迹被保留
    conv_id = data["conversationId"]
    r = client.get(f"{_fo(oid)}/agent/conversations/{conv_id}", headers=auth_headers)
    messages = r.json()["data"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "[已取消]"
    assert len(messages[1]["steps"]) == 1
    assert messages[1]["steps"][0]["tool"] == "search_objects"


import pytest


@pytest.fixture
def modeled_ontology(client, auth_headers, ontology):
    """两个对象类型（订单/供应商）+ 链接 + 动作 + 若干实例（与 test_agent_runtime 同构）。"""
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
        "actions": [],
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
    return ontology
