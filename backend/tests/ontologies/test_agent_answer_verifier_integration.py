"""确定性结论校验在编排器中的集成测试：编造数字 → 一次修正回环 → 通过。"""
import uuid

from app.ontologies.agent_runtime import llm_bridge


def _fo(oid: str) -> str:
    return f"/api/v2/formal/ontologies/{oid}"


def test_answer_verification_corrects_fabricated_number(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """假 LLM 终答编造 999 → 校验失败 → 修正回环后给出与工具结果一致的数字。"""
    oid = modeled_ontology["id"]

    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name="verify-fake", provider="openai",
                       config_type="llm", models=["verify-fake-model"],
                       created_by=admin_user.id))
    db.commit()

    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            assert any(t["name"] == "search_objects" for t in tools)
            return {"content": None,
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "search_objects",
                                    "arguments": {"object_type": "Order",
                                                  "filters": [{"property": "status",
                                                               "op": "eq",
                                                               "value": "pending"}]}}]}
        if calls["n"] == 2:
            # 终答编造了一个工具结果里没有的数字
            return {"content": "待支付订单共 999 个。", "tool_calls": [],
                    "usage": {"inputTokens": 20, "outputTokens": 8}}
        # 修正回环（tools=[]）：给出可对应工具结果的数字
        assert tools == []
        assert "999" in messages[-1]["content"] and "结论校验失败" in messages[-1]["content"]
        return {"content": "待支付订单共 1 个，即 SO-001。", "tool_calls": [],
                "usage": {"inputTokens": 15, "outputTokens": 6}}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "待支付订单有几个？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert "999" not in data["content"]
    assert "SO-001" in data["content"]
    verification = data["verification"]
    assert verification["passed"] is True
    assert verification["retried"] is True
    assert calls["n"] == 3


def test_answer_verification_warns_when_model_wont_fix(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """修正回环后模型仍编造 → 不摧毁回答，但显式标注未验证数字。"""
    oid = modeled_ontology["id"]

    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name="verify-stubborn", provider="openai",
                       config_type="llm", models=["verify-stubborn-model"],
                       created_by=admin_user.id))
    db.commit()

    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None,
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "search_objects",
                                    "arguments": {"object_type": "Order"}}]}
        # 终答与修正回环都坚持编造 999
        return {"content": "待支付订单共 999 个。", "tool_calls": [],
                "usage": {"inputTokens": 20, "outputTokens": 8}}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "待支付订单有几个？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert "999" in data["content"]
    assert "结论校验" in data["content"]
    verification = data["verification"]
    assert verification["passed"] is False
    assert 999.0 in verification["unverified"]
    assert verification["retried"] is True
    assert calls["n"] == 3  # 首轮工具调用 + 终答 + 一次修正回环


def test_answer_verification_skips_date_restatement_when_tool_fails(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """工具失败 + 终答只重述日期（2026年5月）→ 日期已屏蔽：不重试、不告警。"""
    oid = modeled_ontology["id"]

    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name="verify-date", provider="openai",
                       config_type="llm", models=["verify-date-model"],
                       created_by=admin_user.id))
    db.commit()

    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            assert any(t["name"] == "search_objects" for t in tools)
            # 不存在的对象类型 → 工具失败，step 带 error，数值宇宙为空
            return {"content": None,
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "search_objects",
                                    "arguments": {"object_type": "NoSuchType"}}]}
        # 工具失败后终答只重述查询日期，不编造任何数量
        return {"content": "2026年5月的待支付订单数据暂时无法获取。",
                "tool_calls": [], "usage": {"inputTokens": 20, "outputTokens": 8}}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "2026年5月有多少待支付订单？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert "结论校验" not in data["content"]
    verification = data["verification"]
    assert verification["passed"] is True
    assert verification["retried"] is False
    assert calls["n"] == 2  # 首轮工具调用 + 终答，无修正回环


def test_answer_verification_still_retries_when_tool_fails_and_date_hides_count(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """工具失败 + 终答编造 999 → 仍重试并警告；修正回环拿日期里的 5 冒充计数也无证据。"""
    oid = modeled_ontology["id"]

    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name="verify-date-count", provider="openai",
                       config_type="llm", models=["verify-date-count-model"],
                       created_by=admin_user.id))
    db.commit()

    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None,
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "search_objects",
                                    "arguments": {"object_type": "NoSuchType"}}]}
        if calls["n"] == 2:
            # 工具已失败，终答仍编造数量（日期本身被屏蔽，999 无任何证据）
            return {"content": "2026年5月待支付订单共 999 个。",
                    "tool_calls": [], "usage": {"inputTokens": 20, "outputTokens": 8}}
        assert tools == []
        assert "999" in messages[-1]["content"] and "结论校验失败" in messages[-1]["content"]
        # 修正回环偷换概念：拿日期标签里的 5 冒充计数，依旧没有工具证据
        return {"content": "2026年5月待支付订单共 5 个。",
                "tool_calls": [], "usage": {"inputTokens": 15, "outputTokens": 6}}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "2026年5月有多少待支付订单？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert "999" not in data["content"]
    assert "结论校验" in data["content"]
    verification = data["verification"]
    assert verification["passed"] is False
    assert verification["unverified"] == [5]
    assert verification["retried"] is True
    assert calls["n"] == 3  # 首轮工具调用 + 终答 + 一次修正回环


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
