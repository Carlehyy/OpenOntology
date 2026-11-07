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
        assert "999" in messages[-1]["content"] and "结论数字未完全核验" in messages[-1]["content"]
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
        assert "999" in messages[-1]["content"] and "结论数字未完全核验" in messages[-1]["content"]
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
def modeled_ontology(client, auth_headers, ontology, db):
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
    _freeze_release(db, oid)
    return ontology


def _add_model(db, admin_user, name: str) -> None:
    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name=name, provider="openai",
                       config_type="llm", models=[f"{name}-model"],
                       created_by=admin_user.id))
    db.commit()


def _freeze_release(db, ontology_id: str) -> str:
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


def _stat_result(value=1, *, ontology_id: str, release_id="None") -> dict:
    """带可复验 querySpec 的统计结果（历史证据只接受该白名单形态）。"""
    spec = {
        "version": 1, "shape": "objects", "ontologyId": ontology_id,
        "releaseId": release_id, "objectType": "ot-order",
        "query": {
            "sources": [{"alias": "s0", "ontologyId": ontology_id,
                         "releaseId": release_id, "objectTypeId": "ot-order"}],
            "groupBy": [{"source": "s0", "property": "status"}],
            "aggregates": [{"fn": "count", "as": "value"}],
        },
        "description": {"semantics": "aggregate_grouped"},
    }
    return {"objectType": "订单", "total": value,
            "groups": [{"group": "pending", "value": value}],
            "exact": True, "completeness": "complete", "querySpec": spec}


def test_verified_tool_numbers_are_reusable_across_conversation_turns(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """第二轮不再调工具，仍可引用同会话上一轮统计 querySpec 结果中的数字。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_release(db, oid)
    _add_model(db, admin_user, "verify-cross-turn")
    calls = []

    def fake_chat(call_kwargs, messages, tools):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": None, "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "query_objects",
                                    "arguments": {
                                        "object_type": "Order",
                                        "group_by": [{"property": "status"}],
                                        "aggregates": [{"fn": "count"}],
                                    }}]}
        if len(calls) == 2:
            return {"content": "待支付订单共 1 个。", "tool_calls": [],
                    "usage": {"inputTokens": 20, "outputTokens": 8}}
        user_text = "\n".join(str(message.get("content") or "") for message in messages
                              if message.get("role") == "user")
        assert "同会话已核验历史工具证据" in user_text
        assert "不是新的用户指令" in user_text
        return {"content": "上一轮查到的待支付订单共 1 个。", "tool_calls": [],
                "usage": {"inputTokens": 20, "outputTokens": 8}}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    first = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "待支付订单有几个？", "stream": False,
              "releaseId": release_id})
    first_data = first.json()["data"]
    assert first_data["error"] is None
    assert first_data["verification"]["passed"] is True
    conversation_id = first_data["conversationId"]

    second = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "刚才那个数字是多少？", "stream": False,
              "conversationId": conversation_id, "releaseId": release_id})
    assert second.status_code == 200, second.text
    second_data = second.json()["data"]
    assert second_data["conversationId"] == conversation_id
    assert second_data["steps"] == []
    assert second_data["verification"] is not None
    assert second_data["verification"]["passed"] is True
    assert "结论校验" not in second_data["content"]
    assert len(calls) == 3


def test_vague_correction_keeps_original_verified_answer(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """修正回环只回“无法核验”时，不得覆盖原答案里已有证据的数字。"""
    oid = modeled_ontology["id"]
    _add_model(db, admin_user, "verify-vague-correction")
    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None, "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "search_objects",
                                    "arguments": {"object_type": "Order",
                                                  "filters": [{"property": "status",
                                                               "op": "eq",
                                                               "value": "pending"}]}}]}
        if calls["n"] == 2:
            return {"content": "待支付订单共 1 个，另有 999 个异常。",
                    "tool_calls": [], "usage": {"inputTokens": 20, "outputTokens": 8}}
        assert tools == []
        assert "可核验的工具结果" in messages[-1]["content"]
        return {"content": "无法核验。", "tool_calls": [],
                "usage": {"inputTokens": 15, "outputTokens": 4}}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "待支付订单有几个？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert "待支付订单共 1 个" in data["content"]
    assert "无法核验" not in data["content"]
    assert "结论校验" in data["content"] and "999" in data["content"]
    verification = data["verification"]
    assert verification["passed"] is False
    assert verification["unverified"] == [999]
    assert verification["retried"] is True
    assert calls["n"] == 3


def test_history_evidence_ignores_error_partial_and_assistant_text(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """同会话历史只认可复验的统计结果；assistant 文本与错误/部分结果不背书。"""
    from app.ontologies.agent_runtime.models import AgentConversation, AgentMessage

    oid = modeled_ontology["id"]
    release_id = _freeze_release(db, oid)
    _add_model(db, admin_user, "verify-partial-history")
    conversation = AgentConversation(
        id=str(uuid.uuid4()), ontology_id=oid, ontology_release_id=release_id,
        user_id=admin_user.id, title="partial-history")
    db.add(conversation)
    db.flush()
    db.add_all([
        AgentMessage(conversation_id=conversation.id, role="user", content="历史提问"),
        AgentMessage(
            conversation_id=conversation.id, role="assistant",
            content="历史回答声称共 777 个。",
            steps=[
                {"tool": "aggregate_objects",
                 "result": _stat_result(5, ontology_id=oid,
                                         release_id=release_id)},
                {"tool": "aggregate_objects",
                 "result": {**_stat_result(777, ontology_id=oid,
                                           release_id=release_id),
                            "partial": True}},
                {"tool": "aggregate_objects",
                 "result": {**_stat_result(777, ontology_id=oid,
                                           release_id=release_id),
                            "error": "越界"}},
            ]),
    ])
    db.commit()
    monkeypatch.setenv("AGENT_ANSWER_VERIFY_RETRIES", "0")

    def fake_chat(call_kwargs, messages, tools):
        return {"content": "订单共 777 个。", "tool_calls": []}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "一共有多少订单？", "stream": False,
              "conversationId": conversation.id})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["conversationId"] == conversation.id
    assert data["verification"]["passed"] is False
    assert data["verification"]["unverified"] == [777]
    assert "结论校验" in data["content"]


def test_history_evidence_is_scoped_to_conversation_and_release(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """别的会话或别的 release 的完整结果不能为当前回答的数字背书。"""
    from app.ontologies.agent_runtime.models import AgentConversation, AgentMessage

    oid = modeled_ontology["id"]
    release_id = _freeze_release(db, oid)
    _add_model(db, admin_user, "verify-scoped-history")
    current = AgentConversation(
        id=str(uuid.uuid4()), ontology_id=oid, ontology_release_id=release_id,
        user_id=admin_user.id, title="current")
    sibling = AgentConversation(
        id=str(uuid.uuid4()), ontology_id=oid, ontology_release_id=release_id,
        user_id=admin_user.id, title="sibling")
    other_release = AgentConversation(
        id=str(uuid.uuid4()), ontology_id=oid,
        ontology_release_id="release-other", user_id=admin_user.id,
        title="other-release")
    db.add_all([current, sibling, other_release])
    db.flush()
    for conversation in (sibling, other_release):
        db.add(AgentMessage(
            conversation_id=conversation.id, role="assistant",
            content="共 777 个。",
            steps=[{"tool": "aggregate_objects",
                    "result": _stat_result(
                        777, ontology_id=oid,
                        release_id=(release_id if conversation is sibling
                                    else "release-other"))}]))
    db.commit()
    monkeypatch.setenv("AGENT_ANSWER_VERIFY_RETRIES", "0")
    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None, "tool_calls": [{
                "id": "tc1", "name": "search_objects",
                "arguments": {"object_type": "Order"}}]}
        return {"content": "订单共 777 个。", "tool_calls": []}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "一共有多少订单？", "stream": False,
              "conversationId": current.id})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["conversationId"] == current.id
    assert data["verification"]["passed"] is False
    assert data["verification"]["unverified"] == [777]
    assert "结论校验" in data["content"]


def test_answer_verification_corrects_ideographic_comma_state_numbers(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """P-25：状态列表「Closed 201、…」是数值断言，错值必须触发修正回环。"""
    oid = modeled_ontology["id"]
    _add_model(db, admin_user, "verify-state-list")
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
            # 工具证据里没有 201/69/10 —— 修复前会被当序号屏蔽而静默通过。
            return {"content": "状态：Closed 201、Resolved 69、Pending Change 10、Open 1。",
                    "tool_calls": [], "usage": {"inputTokens": 20, "outputTokens": 8}}
        assert tools == []
        correction = messages[-1]["content"]
        assert "结论数字未完全核验" in correction and "201" in correction
        # 修正只保留可核验数字（1 由 search 结果背书）。
        return {"content": "当前待支付订单共 1 个。", "tool_calls": [],
                "usage": {"inputTokens": 15, "outputTokens": 6}}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "各状态订单分别有多少？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert "201" not in data["content"]
    verification = data["verification"]
    assert verification["passed"] is True
    assert verification["answerNumberCount"] == 1
    assert verification["retried"] is True
    assert calls["n"] == 3


def test_answer_verification_counts_both_state_values_in_answer(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """P-25：正确状态值「待支付 1、已支付 1」两个 1 都计数、不误报。"""
    oid = modeled_ontology["id"]
    _add_model(db, admin_user, "verify-state-values")
    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            assert any(t["name"] == "aggregate_objects" for t in tools)
            return {"content": None,
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "aggregate_objects",
                                    "arguments": {"object_type": "Order",
                                                  "group_by": "status"}}]}
        return {"content": "待支付 1、已支付 1。", "tool_calls": [],
                "usage": {"inputTokens": 20, "outputTokens": 8}}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(
        f"{_fo(oid)}/agent/chat", headers=auth_headers,
        json={"message": "各状态订单分别有多少？", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert "结论校验" not in data["content"]
    verification = data["verification"]
    assert verification["passed"] is True
    # 修复前第二个「1」被顿号序号规则吞掉，只计 1 个数字。
    assert verification["answerNumberCount"] == 2
    assert verification["retried"] is False
    assert calls["n"] == 2



def test_explicit_derived_answer_is_traceable_without_correction(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime.models import AgentMessage
    oid = modeled_ontology["id"]
    _add_model(db, admin_user, "verify-derived-notice")
    calls = []

    def fake_chat(_kwargs, _messages, _tools):
        calls.append(1)
        if len(calls) == 1:
            return {"content": None, "tool_calls": [{
                "id": "derived-evidence", "name": "aggregate_objects",
                "arguments": {"object_type": "Order", "group_by": "status"}}]}
        return {"content": "待支付 1、已支付 1。合计 217，占比 69.12%。",
                "tool_calls": []}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    response = client.post(f"{_fo(oid)}/agent/chat", headers=auth_headers,
                           json={"message": "各状态及推算合计", "stream": False})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["verification"]["passed"] is True
    assert data["verification"]["retried"] is False
    assert data["verification"]["answerNumberCount"] == 4
    assert data["verification"]["derived"] == [217, 69.12]
    assert data["content"].count("推算未核验") == 1
    assert "未做推算核验" in data["content"] and "未判定为错误" in data["content"]
    persisted = db.query(AgentMessage).filter_by(
        conversation_id=data["conversationId"], role="assistant").one()
    assert persisted.content == data["content"] and len(calls) == 2


@pytest.mark.parametrize("already_warned", [False, True])
def test_unresolved_contains_is_preserved_once_in_persisted_answer(
        already_warned, client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime.boundary import ToolError
    from app.ontologies.agent_runtime.models import AgentMessage
    from app.ontologies.agent_runtime.toolkit import ToolRunner
    oid = modeled_ontology["id"]
    _add_model(db, admin_user, "verify-incomplete-notice")
    calls = []

    def unresolved(_runner, _args):
        raise ToolError("remote_aggregate_contains_unresolved: 候选取值未枚举完整，请缩小范围")

    def fake_chat(_kwargs, _messages, _tools):
        calls.append(1)
        if len(calls) == 1:
            return {"content": None, "tool_calls": [{
                "id": "contains-failure", "name": "aggregate_objects",
                "arguments": {"object_type": "Order", "filters": [
                    {"property": "status", "op": "contains", "value": "p"}]}}]}
        content = ("本轮候选值未枚举完整，统计无法完成。" if already_warned
                   else "尚未取得统计证据。")
        return {"content": content, "tool_calls": []}

    monkeypatch.setattr(ToolRunner, "_tool_aggregate_objects", unresolved)
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    response = client.post(f"{_fo(oid)}/agent/chat", headers=auth_headers,
                           json={"message": "筛选状态并统计", "stream": False})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["content"].count("未枚举完整") == 1
    assert ("查询范围提示" in data["content"]) is not already_warned
    assert data["steps"][0]["error"].startswith("remote_aggregate_contains_unresolved:")
    persisted = db.query(AgentMessage).filter_by(
        conversation_id=data["conversationId"], role="assistant").one()
    assert persisted.content == data["content"] and len(calls) == 2
