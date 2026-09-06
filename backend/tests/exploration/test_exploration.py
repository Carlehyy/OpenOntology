"""业务探索（exploration）模块测试：

  1. 会话 CRUD 与归属隔离
  2. 回合编排：假 LLM 驱动工具调用沉淀画布，轨迹/画布版本/标题持久化；
     非法元素被 pydantic 拒绝并把错误回填给 LLM（对话期修复回路）
  3. 需求文档：确定性章节永远生成，LLM 缺席时叙述节降级占位；版本递增
  4. 转化管线（纯确定性，不依赖 LLM）：类型映射 / 主键回退 / 悬空关系剔除 /
     规则挂载(disabled) / 审批传播 / 主体合并 / 场景覆盖检查 /
     derivation→激活函数草稿(enabled=false) / alert+事件→哨兵草稿(muted 影子) /
     未命中行为的 approval 规则必须产生警告（不得静默丢失）
  5. 落地：新建本体后 fo_* 表可查；函数/哨兵带三重闸门落地（enabled=false/muted/draft）；
     五类元素写 source 血缘（session/document/draft/draftKey/sourceRefs）；
     草稿可重复应用（同名跳过幂等，部分勾选后剩余元素可二次落地）；
     废弃后不可应用；保守合并——同名对象跳过、链接端点可绑定到目标本体既有类型
"""
import json
import uuid
from contextlib import contextmanager

import pytest

from app.exploration import canvas as C
from app.exploration import converter as CV
from app.exploration.models import ExplorationMessage, ExplorationSession

BASE = "/api/v2/exploration"


@pytest.fixture
def session(client, auth_headers):
    r = client.post(f"{BASE}/sessions", headers=auth_headers, json={})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _seed_canvas(db, session_id: str, canvas: dict) -> None:
    s = db.query(ExplorationSession).filter(ExplorationSession.id == session_id).first()
    s.canvas = canvas
    s.canvas_version = (s.canvas_version or 0) + 1
    db.commit()


def _demo_canvas() -> dict:
    cv = C.empty_canvas()
    cv, _, errs = C.upsert_elements(cv, "object", [
        {"name": "Order", "displayName": "订单", "keyAttribute": "order_no",
         "attributes": [
             {"name": "order_no", "displayName": "订单号", "typeHint": "文本", "required": True},
             {"name": "amount", "displayName": "金额", "typeHint": "金额"},
             {"name": "paid", "displayName": "是否已支付", "typeHint": "是否"},
         ],
         "relations": [{"target": "Supplier", "displayName": "归属供应商",
                        "cardinality": "many-to-one"},
                       {"target": "Ghost", "displayName": "悬空关系"}]},
        {"name": "Supplier", "displayName": "供应商",
         "attributes": [{"name": "sname", "displayName": "名称", "required": True}]},
    ])
    assert not errs
    cv, _, _ = C.upsert_elements(cv, "actor", [
        {"name": "Finance", "displayName": "财务", "kind": "role",
         "responsibilities": ["审核付款"]},
        {"name": "Supplier", "displayName": "供应商（主体）", "kind": "org"},   # 与对象同名 → 合并
        {"name": "ERP", "kind": "system"},                                    # system 不建对象
    ])
    cv, _, _ = C.upsert_elements(cv, "behavior", [
        {"name": "mark_paid", "displayName": "标记支付", "actor": "Finance",
         "object": "Order", "trigger": "收到银行回单",
         "inputs": [{"name": "note", "displayName": "备注", "typeHint": "文本"}],
         "constraints": ["金额必须大于 0"], "needsApproval": False},
    ])
    cv, _, _ = C.upsert_elements(cv, "rule", [
        {"name": "big_amount_approval", "displayName": "大额审批", "kind": "approval",
         "appliesTo": "mark_paid", "statement": "金额超过 1 万需要审批"},
        {"name": "amount_positive", "kind": "validation", "appliesTo": "mark_paid",
         "statement": "金额必须为正数", "errorMessage": "金额必须大于 0"},
        {"name": "orphan_rule", "kind": "alert", "appliesTo": "Warehouse",
         "statement": "库存低于安全线告警"},          # 目标未定义 → 哨兵草稿不绑对象
        {"name": "total_calc", "displayName": "订单总额计算", "kind": "derivation",
         "appliesTo": "Order", "statement": "总额 = 明细金额之和"},
    ])
    cv, _, _ = C.upsert_elements(cv, "event", [
        {"name": "order_paid", "displayName": "订单已支付", "source": "mark_paid",
         "payload": ["order_no", "amount"], "consequences": ["通知供应商发货"]},
        {"name": "daily_check", "displayName": "每日对账", "source": "time",
         "consequences": ["生成对账单"]},              # time 来源 → 定期扫描哨兵
    ])
    cv, _, _ = C.upsert_elements(cv, "scenario", [
        {"name": "pay_flow", "displayName": "支付流程", "goal": "完成订单支付",
         "objects": ["Order", "Invoice"], "behaviors": ["mark_paid", "refund"]},
    ])
    return cv


def test_canvas_upsert_merges_fields_and_rejects_bad_enums():
    cv = C.empty_canvas()
    cv, ids, errors = C.upsert_elements(cv, "object", [{
        "name": "Order", "displayName": "订单", "keyAttribute": "order_no",
        "attributes": [{"name": "order_no", "typeHint": "文本"}],
        "relations": [{"target": "Customer", "cardinality": "many-to-one"}],
    }])
    assert ids and not errors

    # 修描述不能把未随补丁重发的已确认属性/关系清空。
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Order", "description": "订单主数据",
    }])
    order = cv["objects"][0]
    assert not errors and order["description"] == "订单主数据"
    assert order["attributes"][0]["name"] == "order_no" and order["relations"]

    # 显式空数组仍表示用户/AI 确认清空。
    cv, _, errors = C.upsert_elements(cv, "object", [{"name": "Order", "relations": []}])
    assert not errors and cv["objects"][0]["relations"] == []

    _, applied, errors = C.upsert_elements(cv, "object", [{
        "name": "Broken", "attributes": [{"name": "status", "enum": ["open", " open "]}],
    }])
    assert not applied and any("重复" in error for error in errors)


def test_completeness_uses_same_entity_reference_contract_as_readiness():
    """person/org/role 主体是合法实体端点，完整度不能再误报、质量门却放行。"""
    cv = C.empty_canvas()
    cv, _, _ = C.upsert_elements(cv, "object", [{
        "name": "Order", "attributes": [{"name": "id", "typeHint": "文本"}],
        "keyAttribute": "id",
        "relations": [{"target": "Customer", "cardinality": "many-to-one"}],
    }])
    cv, _, _ = C.upsert_elements(cv, "actor", [
        {"name": "Customer", "kind": "person", "keyAttribute": "customer_id",
         "attributes": [{"name": "customer_id", "typeHint": "文本"}]},
        {"name": "CSR", "kind": "role"},
    ])
    cv, _, _ = C.upsert_elements(cv, "behavior", [{
        "name": "serve_customer", "actor": "CSR", "object": "Customer",
    }])
    gaps = C.completeness(cv)["gaps"]
    assert not any("Customer" in gap and ("未定义" in gap or "尚未" in gap) for gap in gaps)


# ---------------------------------------------------------------- 会话


def test_session_crud(client, auth_headers, session):
    r = client.get(f"{BASE}/sessions", headers=auth_headers)
    assert any(s["id"] == session["id"] for s in r.json()["data"])

    r = client.get(f"{BASE}/sessions/{session['id']}", headers=auth_headers)
    data = r.json()["data"]
    assert data["messages"] == []
    assert set(data["canvas"].keys()) >= {"objects", "actors", "behaviors",
                                          "events", "rules", "scenarios"}
    assert data["completeness"]["counts"]["objects"] == 0

    r = client.delete(f"{BASE}/sessions/{session['id']}", headers=auth_headers)
    assert r.status_code == 204
    assert client.get(f"{BASE}/sessions/{session['id']}",
                      headers=auth_headers).status_code == 404


# ---------------------------------------------------------------- 回合编排


def _fake_model_config(db, admin_user):
    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name="fake", provider="openai",
                       config_type="llm", models=["fake-model"],
                       enabled=True, created_by=admin_user.id))
    db.commit()


def test_chat_turn_persists_canvas(client, auth_headers, session, db, admin_user, monkeypatch):
    _fake_model_config(db, admin_user)
    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            assert any(t["name"] == "upsert_elements" for t in tools)
            assert "业务探索" in messages[0]["content"]
            return {"content": None, "usage": {"inputTokens": 5, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "upsert_elements",
                                    "arguments": {"kind": "object", "elements": [
                                        {"name": "Order", "displayName": "订单",
                                         "attributes": [{"name": "order_no", "required": True}]}]}}]}
        assert messages[-1]["role"] == "tool" and '"applied": 1' in messages[-1]["content"]
        return {"content": "已记录订单对象。它的业务主键是哪个属性？", "tool_calls": [],
                "usage": {"inputTokens": 5, "outputTokens": 5}}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "我们要做订单管理", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert data["steps"][0]["tool"] == "upsert_elements"
    assert data["canvas"]["objects"][0]["name"] == "Order"
    assert data["completeness"]["counts"]["objects"] == 1

    r = client.get(f"{BASE}/sessions/{session['id']}", headers=auth_headers)
    detail = r.json()["data"]
    assert detail["title"] == "我们要做订单管理"          # 首条消息成为标题
    assert detail["canvasVersion"] == 1
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["steps"][0]["tool"] == "upsert_elements"


def test_chat_retries_one_empty_model_response(client, auth_headers, session,
                                               db, admin_user, monkeypatch):
    """Provider 偶发返回无文本/无工具的空包时，编排器应自愈一次。"""
    _fake_model_config(db, admin_user)
    calls = {"n": 0}

    def fake_chat(_call_kwargs, messages, _tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None, "tool_calls": [], "usage": None}
        assert "上一响应为空" in messages[-1]["content"]
        return {"content": "已恢复并继续处理。", "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    response = client.post(
        f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
        json={"message": "继续建模", "stream": False},
    )
    assert response.status_code == 200, response.text
    assert calls["n"] == 2
    assert response.json()["data"]["content"] == "已恢复并继续处理。"


def test_chat_fabricated_write_claims_trigger_corrective_retry(
        client, auth_headers, session, db, admin_user, monkeypatch):
    """零工具调用却声称写入/版本推进 → 纠偏重试一次；改口为计划口吻后放行。

    生产事故回归：MiniMax-M3 在长会话里直接输出「已成功写入（v12 → v22）、
    8 次工具调用成功」而 steps 为空、画布停在 v12。
    """
    _fake_model_config(db, admin_user)
    calls = {"n": 0}

    def fake_chat(_call_kwargs, messages, _tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": "已成功写入 5 个对象（画布 v0 → v3），本回合 6 次工具调用全部完成。",
                    "tool_calls": [], "usage": None}
        assert "服务端核对" in messages[-1]["content"]
        assert "禁止声称未通过工具执行" in messages[-1]["content"]
        return {"content": "修正：改为计划 —— 接下来我将调用 upsert_elements 写入 5 个对象。",
                "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "帮我建模", "stream": False})
    assert r.status_code == 200, r.text
    assert calls["n"] == 2
    content = r.json()["data"]["content"]
    assert "接下来我将" in content and "已成功写入" not in content

    row = (db.query(ExplorationSession)
           .filter(ExplorationSession.id == session["id"]).one())
    assert row.context_stats.get("fabricationRetries") == 1


def test_chat_persistent_fabrication_gets_server_fact_banner(
        client, auth_headers, session, db, admin_user, monkeypatch):
    """纠偏重试后仍虚构：附服务端事实横幅放行，绝不静默吞掉。"""
    _fake_model_config(db, admin_user)
    calls = {"n": 0}

    def fake_chat(_call_kwargs, _messages, _tools):
        calls["n"] += 1
        return {"content": "已成功写入画布并生成 ER 图。",
                "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "帮我建模", "stream": False})
    assert r.status_code == 200, r.text
    assert calls["n"] == 2                       # 纠偏只重试一次
    content = r.json()["data"]["content"]
    assert "已成功写入画布" in content            # 原文保留
    assert "[服务端核对]" in content              # 横幅附上权威事实
    assert "本回合实际工具调用 0 次" in content


def test_chat_truthful_previous_turn_recap_not_flagged(
        client, auth_headers, session, db, admin_user, monkeypatch):
    """「上回合已写入…」的 truthful 回顾不触发守卫（由历史检查点提供事实）。"""
    _fake_model_config(db, admin_user)
    db.add(ExplorationMessage(
        session_id=session["id"], role="assistant", content="已沉淀订单对象。",
        steps=[{"tool": "upsert_elements", "arguments": {}, "summary": "x",
                "durationMs": 3}]))
    db.commit()
    calls = {"n": 0}

    def fake_chat(_call_kwargs, _messages, _tools):
        calls["n"] += 1
        return {"content": "上回合已成功写入订单对象；本回合无新增，请补充金额口径。",
                "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "继续", "stream": False})
    assert r.status_code == 200, r.text
    assert calls["n"] == 1                        # 未触发纠偏
    assert r.json()["data"]["content"] == "上回合已成功写入订单对象；本回合无新增，请补充金额口径。"


def test_chat_planning_language_not_flagged(
        client, auth_headers, session, db, admin_user, monkeypatch):
    """计划口吻（"接下来将…推进到 vN"）不算已完成声明。"""
    _fake_model_config(db, admin_user)
    calls = {"n": 0}

    def fake_chat(_call_kwargs, _messages, _tools):
        calls["n"] += 1
        return {"content": "计划：本回合将把画布从 v0 推进到 v3，接下来将写入 3 个对象。",
                "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "说说计划", "stream": False})
    assert r.status_code == 200, r.text
    assert calls["n"] == 1
    assert "[服务端核对]" not in r.json()["data"]["content"]


def test_chat_history_tool_checkpoint_injected(
        client, auth_headers, session, db, admin_user, monkeypatch):
    """跨回合历史要携带服务端权威的工具执行检查点（反虚构的结构层）。"""
    _fake_model_config(db, admin_user)
    db.add(ExplorationMessage(
        session_id=session["id"], role="user", content="建对象"))
    db.add(ExplorationMessage(
        session_id=session["id"], role="assistant", content="已沉淀订单对象。",
        steps=[
            {"tool": "upsert_elements", "arguments": {}, "summary": "沉淀 1 个",
             "durationMs": 3},
            {"tool": "show_diagram", "arguments": {}, "summary": "x",
             "durationMs": 1, "error": "状态图质量校验未通过"},
        ]))
    db.commit()
    captured: dict = {}

    def fake_chat(_call_kwargs, messages, _tools):
        captured["system"] = messages[0]["content"]
        return {"content": "收到。", "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "继续", "stream": False})
    assert r.status_code == 200, r.text
    system = captured["system"]
    assert "历史回合工具执行记录" in system
    assert "upsert_elements×1" in system
    assert "show_diagram×1（成0/败1）" in system
    assert "叙述不等于执行" in system
    # 检查点只进模型视图，不改写持久化消息
    detail = client.get(f"{BASE}/sessions/{session['id']}",
                        headers=auth_headers).json()["data"]
    assert detail["messages"][1]["content"] == "已沉淀订单对象。"


# ---------------------------------------------------------------- 管线工具（agent 最后一公里）


def _pipeline_ready_canvas() -> dict:
    """十门全过的小画布（供 generate_document/generate_draft 工具测试）。"""
    from app.exploration import readiness as R
    canvas = C.empty_canvas()
    canvas, _, errors = C.upsert_elements(canvas, "object", [{
        "name": "Order", "displayName": "订单", "keyAttribute": "order_no",
        "attributes": [
            {"name": "order_no", "displayName": "订单号", "typeHint": "文本",
             "required": True},
            {"name": "amount", "displayName": "金额", "typeHint": "金额"},
        ],
    }])
    assert not errors
    canvas, _, errors = C.upsert_elements(canvas, "actor", [
        {"name": "Approver", "displayName": "审批人", "kind": "role"}])
    assert not errors
    canvas, _, errors = C.upsert_elements(canvas, "behavior", [{
        "name": "approve_order", "displayName": "审批订单", "actor": "Approver",
        "object": "Order", "trigger": "订单提交审批", "outcome": "记录审批结论",
    }])
    assert not errors
    canvas, _, errors = C.upsert_elements(canvas, "scenario", [{
        "name": "approval_flow", "displayName": "订单审批流程", "goal": "完成订单审批",
        "actors": ["Approver"], "steps": ["审批人审批订单并记录结论"],
        "objects": ["Order"], "behaviors": ["approve_order"],
        "expectedOutcome": "订单获得明确审批结论",
    }])
    assert not errors
    assert R.evaluate(canvas)["ready"] is True
    return canvas


def _tool_session(db, *, ontology_id=None, canvas=None):
    row = ExplorationSession(
        id=str(uuid.uuid4()), title="pipeline-tools",
        canvas=canvas if canvas is not None else C.empty_canvas(),
        canvas_version=1, ontology_id=ontology_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_generate_document_tool_rejects_force_and_empty_canvas(db, admin_user):
    from app.exploration.models import ExplorationDocument
    from app.exploration.toolkit import ExplorationToolRunner
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row, user=admin_user)
    r1 = runner.run("generate_document", {"force": True})
    assert "不支持 force" in r1["error"]
    r2 = runner.run("generate_document", {})
    assert "画布还是空的" in r2["error"]
    assert db.query(ExplorationDocument).filter_by(session_id=row.id).count() == 0


def test_generate_document_tool_reuses_fresh_document(db, admin_user):
    from app.exploration.models import ExplorationDocument
    from app.exploration.toolkit import ExplorationToolRunner
    row = _tool_session(db, canvas=_pipeline_ready_canvas())
    runner = ExplorationToolRunner(db, row, user=admin_user)
    first = runner.run("generate_document", {})
    assert first.get("documentId") and first["reused"] is False
    # 画布未变 → 复用既有文档，不刷 bx_documents 行（防 LLM 重试风暴）
    second = runner.run("generate_document", {})
    assert second["reused"] is True
    assert second["documentId"] == first["documentId"]
    assert db.query(ExplorationDocument).filter_by(session_id=row.id).count() == 1


def test_generate_draft_tool_binding_then_live_gate(db, admin_user):
    from app.exploration.models import ExplorationDocument
    from app.exploration.toolkit import ExplorationToolRunner
    row = _tool_session(db, canvas=_pipeline_ready_canvas())
    runner = ExplorationToolRunner(db, row, user=admin_user)
    assert runner.run("generate_draft", {}).get("bindingRequired") is True

    row.ontology_id = str(uuid.uuid4())
    row.canvas = C.empty_canvas()          # 绑定但活画布未过门
    db.commit()
    result = runner.run("generate_draft", {})
    assert "活画布质量门未通过" in result["error"]
    assert result.get("blockingItems")
    assert "不要重试" in result["error"]
    # 活画布预检挡在服务调用之前 —— 没有任何文档/草稿产生
    assert db.query(ExplorationDocument).filter_by(session_id=row.id).count() == 0


def test_generate_draft_tool_stale_document_guides_regeneration(db, admin_user):
    from app.exploration.toolkit import ExplorationToolRunner
    row = _tool_session(db, ontology_id=str(uuid.uuid4()),
                        canvas=_pipeline_ready_canvas())
    runner = ExplorationToolRunner(db, row, user=admin_user)
    assert runner.run("generate_document", {}).get("documentId")
    new_canvas, _, errors = C.upsert_elements(row.canvas, "scenario", [
        {"name": "approval_flow", "expectedOutcome": "订单获得明确审批结论并归档"}])
    assert not errors
    row.canvas = new_canvas
    row.canvas_version += 1
    db.commit()
    result = runner.run("generate_draft", {})
    assert result.get("staleDocument") is True
    assert "重新调用 generate_document" in result["error"]


def test_generate_draft_tool_happy_path_and_attempt_cap(db, admin_user, ontology):
    from app.exploration.models import ExplorationDraft
    from app.exploration.toolkit import ExplorationToolRunner
    row = _tool_session(db, ontology_id=ontology["id"],
                        canvas=_pipeline_ready_canvas())
    runner = ExplorationToolRunner(db, row, user=admin_user)
    assert runner.run("generate_document", {}).get("documentId")

    first = runner.run("generate_draft", {})
    assert first.get("draftId")
    assert first["counts"].get("objectTypes", 0) >= 1
    assert "人工确认" in first["note"]
    stored = db.query(ExplorationDraft).filter_by(id=first["draftId"]).one()
    assert stored.status == "draft"

    second = runner.run("generate_draft", {})
    assert second.get("draftId")           # 第 2 次允许
    third = runner.run("generate_draft", {})
    assert "上限" in third["error"]         # 第 3 次封顶


def test_chat_invalid_elements_rejected(client, auth_headers, session, db, admin_user, monkeypatch):
    _fake_model_config(db, admin_user)
    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None, "usage": None,
                    "tool_calls": [{"id": "tc1", "name": "upsert_elements",
                                    "arguments": {"kind": "object",
                                                  "elements": [{"displayName": "没有name字段"}]}}]}
        # 校验错误应回填，供 LLM 修正（对话期修复回路）
        assert "不合法" in messages[-1]["content"]
        return {"content": "收到，我会补全 name。", "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "记录一个对象", "stream": False})
    data = r.json()["data"]
    assert data["error"] is None
    assert data["canvas"] is None            # 画布没有被非法元素污染
    r = client.get(f"{BASE}/sessions/{session['id']}/canvas", headers=auth_headers)
    assert r.json()["data"]["completeness"]["counts"]["objects"] == 0


# ---------------------------------------------------------------- 需求文档


def test_document_generation_without_llm(client, auth_headers, session, db):
    r = client.post(f"{BASE}/sessions/{session['id']}/documents",
                    headers=auth_headers, json={})
    assert r.status_code == 422                      # 空画布拒绝生成

    _seed_canvas(db, session["id"], _demo_canvas())
    r = client.post(f"{BASE}/sessions/{session['id']}/documents",
                    headers=auth_headers, json={})
    assert r.status_code == 201, r.text
    doc = r.json()["data"]
    md = doc["contentMd"]
    # 确定性章节忠实于画布；LLM 缺席时叙述节降级为占位而非失败
    assert "## 4. 对象模型" in md and "订单" in md and "order_no" in md
    assert "标记支付" in md and "大额审批" in md and "支付流程" in md
    # §10/§11：澄清账本 + 质量门报告（与草稿闸门同一口径；§8 起插入流程模型章节后顺延）
    assert "## 10. 澄清账本" in md and "## 11. 质量门检查" in md
    assert "⛔ 未就绪" in md          # demo 画布故意含瑕疵（悬空关系/缺主键）

    r = client.post(f"{BASE}/sessions/{session['id']}/documents",
                    headers=auth_headers, json={})
    assert r.json()["data"]["version"] == 2          # 版本递增

    r = client.get(f"{BASE}/sessions/{session['id']}/documents", headers=auth_headers)
    assert [d["version"] for d in r.json()["data"]] == [2, 1]


# ---------------------------------------------------------------- 转化管线（确定性单测）


def test_converter_actor_carries_attributes():
    """person/org 主体是数据实体：attributes 应透传为对象类型属性（根治「主体只有名称」）；
    system 主体不建对象；person/org 空属性触发 completeness 缺口（质量关）。"""
    cv = C.empty_canvas()
    cv, _, errs = C.upsert_elements(cv, "actor", [
        {"name": "Seller", "displayName": "卖家", "kind": "person",
         "responsibilities": ["发布商品", "发货"], "keyAttribute": "shop_name",
         "attributes": [
             {"name": "shop_name", "displayName": "店铺名", "typeHint": "文本", "required": True},
             {"name": "credit_score", "displayName": "信誉分", "typeHint": "数字"},
         ]},
        {"name": "Sys", "displayName": "系统", "kind": "system"},
    ])
    assert not errs
    draft, _ = CV.build_draft(cv)
    ot = {o["name"]: o for o in draft["objectTypes"]}
    assert "Seller" in ot and "Sys" not in ot           # system 主体不建对象
    names = {p["name"] for p in ot["Seller"]["properties"]}
    assert {"shop_name", "credit_score"} <= names        # 属性透传，不再只有 name
    seller_pk = next(p["id"] for p in ot["Seller"]["properties"] if p["name"] == "shop_name")
    assert ot["Seller"]["primaryKey"] == seller_pk        # 主键按编辑器契约引用 Property.id

    # 质量关：person/org 主体空属性 → 缺口，驱动 agent 追问
    cv2, _, _ = C.upsert_elements(C.empty_canvas(), "actor",
                                  [{"name": "Buyer", "displayName": "买家", "kind": "person"}])
    assert any("买家" in g and "属性" in g for g in C.completeness(cv2)["gaps"])


def test_converter_deterministic_mapping():
    draft, report = CV.build_draft(_demo_canvas())

    ot = {o["name"]: o for o in draft["objectTypes"]}
    # 对象 + 非 system 主体（同名 Supplier 合并、ERP 跳过）
    assert set(ot) == {"Order", "Supplier", "Finance"}
    order = ot["Order"]
    assert order["primaryKey"] == next(p["id"] for p in order["properties"]
                                        if p["name"] == "order_no")
    types = {p["name"]: p["type"] for p in order["properties"]}
    assert types["amount"] == "number" and types["paid"] == "boolean" \
        and types["order_no"] == "string"
    # Supplier 未指定主键 → 自动补 id 并回退
    assert ot["Supplier"]["primaryKey"] == next(p["id"] for p in ot["Supplier"]["properties"]
                                                  if p["name"] == "id")
    assert any("Supplier" in w and "主键" in w for w in report["warnings"])

    # 悬空关系剔除、合法关系保留基数
    links = draft["linkTypes"]
    assert len(links) == 1 and links[0]["cardinality"] == "many-to-one"
    assert any("Ghost" in w for w in report["warnings"])

    act = draft["actions"][0]
    assert act["name"] == "mark_paid"
    assert act["requiresApproval"] is True           # approval 规则传播
    # 约束 + validation 规则 → disabled 待形式化规则
    assert len(act["rules"]) == 2
    assert all(r["enabled"] is False and r["config"]["type"] == "validation"
               for r in act["rules"])
    assert any("金额必须大于 0" == r["config"]["errorMessage"] for r in act["rules"])
    assert act["parameters"][0]["name"] == "note"
    assert "订单已支付" in act["description"]         # 事件并入动作描述

    # derivation 规则 → 激活函数草稿（enabled=false，绑定作用对象）
    fns = draft["functions"]
    assert len(fns) == 1
    fn = fns[0]
    assert fn["name"] == "total_calc" and fn["enabled"] is False
    assert fn["displayName"].startswith("待形式化")
    assert fn["functionType"] == "object" and fn["targetObjectTypeKey"] == "obj:order"
    assert fn["language"] == "expression" and fn["body"] == ""
    assert "总额" in fn["description"]

    # alert 规则 + 事件 → 哨兵草稿（muted 影子 + enabled=false + status=draft）
    sens = {s["name"]: s for s in draft["sentinels"]}
    assert set(sens) == {"orphan_rule", "order_paid", "daily_check"}
    assert all(s["muted"] is True and s["enabled"] is False and s["status"] == "draft"
               and s["displayName"].startswith("待形式化") for s in sens.values())
    # 事件来源=行为 → 绑定行为的作用对象，变化驱动
    assert sens["order_paid"]["bindingObjectKey"] == "obj:order"
    assert sens["order_paid"]["onChange"] is True and sens["order_paid"]["onSchedule"] is False
    assert "order_no" in sens["order_paid"]["description"]     # 事件载荷不再丢弃
    # 事件来源=time → 定期扫描
    assert sens["daily_check"]["onSchedule"] is True and sens["daily_check"]["onChange"] is False
    # 告警目标未定义 → 不绑对象 + 警告提示补绑定
    assert sens["orphan_rule"]["bindingObjectKey"] is None
    assert any("orphan_rule" in w and "绑定" in w for w in report["warnings"])

    # 场景覆盖检查：Invoice / refund 缺失
    cov = report["scenarioCoverage"]
    assert cov and cov[0]["missingObjects"] == ["Invoice"] \
        and cov[0]["missingBehaviors"] == ["refund"]
    assert report["llmRefined"] is False


def test_converter_approval_unmatched_not_silent():
    """approval 规则未命中任何行为时必须产生警告（修复此前的静默丢失）。"""
    cv = C.empty_canvas()
    cv, _, _ = C.upsert_elements(cv, "rule", [
        {"name": "ghost_approval", "displayName": "幽灵审批", "kind": "approval",
         "appliesTo": "not_exist_behavior", "statement": "需要审批"},
    ])
    _, report = CV.build_draft(cv)
    assert any("ghost_approval" in w and "审批" in w for w in report["warnings"])


def test_converter_build_draft_is_llm_free(monkeypatch):
    """build_draft 全程不触碰 LLM（补缺已移除，确定性映射自足）——
    即便传了 call_kwargs，llm_bridge 被调用即失败。"""
    from app.ontologies.agent_runtime import llm_bridge

    def _boom(kw, msgs, tools):
        raise AssertionError("build_draft 不应调用 LLM")

    monkeypatch.setattr(llm_bridge, "chat", _boom)
    draft, report = CV.build_draft(_demo_canvas(), call_kwargs={"model": "fake"})
    assert report["llmRefined"] is False
    assert {o["name"] for o in draft["objectTypes"]} == {"Order", "Supplier", "Finance"}


def test_converter_refine_whitelist_direct(monkeypatch):
    """refine_draft（保留的工具函数）只允许白名单字段生效，非法类型/基数被忽略；
    垃圾输出重试后整体丢弃，确定性骨架不受影响。"""
    import json as _json
    from app.ontologies.agent_runtime import llm_bridge

    # 垃圾输出 → 丢弃补丁
    monkeypatch.setattr(llm_bridge, "chat",
                        lambda kw, msgs, tools: {"content": "这不是 JSON", "tool_calls": [],
                                                 "usage": None})
    warnings: list[str] = []
    draft, _ = CV.build_draft(_demo_canvas())
    assert CV.refine_draft(draft, _demo_canvas(), {"model": "fake"}, warnings) is False
    assert any("补缺" in w for w in warnings)

    # 白名单合并
    patch = {"objectTypes": [{"key": "obj:order", "description": "客户订单",
                              "properties": [{"name": "amount", "type": "decimal128"},
                                             {"name": "paid", "type": "boolean",
                                              "displayName": "已支付"}]}],
             "linkTypes": [{"key": "link:order_supplier", "cardinality": "many-to-many-badly"}],
             "actions": [{"key": "act:mark_paid", "description": "财务确认收款后标记"}]}
    monkeypatch.setattr(llm_bridge, "chat",
                        lambda kw, msgs, tools: {"content": _json.dumps(patch),
                                                 "tool_calls": [], "usage": None})
    warnings2: list[str] = []
    assert CV.refine_draft(draft, _demo_canvas(), {"model": "fake"}, warnings2) is True
    order = next(o for o in draft["objectTypes"] if o["key"] == "obj:order")
    types = {p["name"]: p for p in order["properties"]}
    assert order["description"] == "客户订单"
    assert types["amount"]["type"] == "number"        # 非法类型被忽略，保留确定性结果
    assert types["paid"]["displayName"] == "已支付"
    assert any("不合法" in w for w in warnings2)       # 非法基数记警告
    link = draft["linkTypes"][0]
    assert link["cardinality"] == "many-to-one"       # 非法基数被忽略


# ---------------------------------------------------------------- 草稿生成与落地


def _make_draft(client, auth_headers, session_id, db, target_ontology_id=None):
    _seed_canvas(db, session_id, _demo_canvas())
    r = client.post(f"{BASE}/sessions/{session_id}/documents", headers=auth_headers, json={})
    doc_id = r.json()["data"]["id"]
    # demo 画布故意含瑕疵（测转化管线的兜底），质量门会拦 —— 显式越权
    body = {"targetOntologyId": target_ontology_id, "force": True} \
        if target_ontology_id else {"force": True}
    r = client.post(f"{BASE}/documents/{doc_id}/drafts", headers=auth_headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_draft_apply_to_new_ontology(client, auth_headers, session, db):
    draft = _make_draft(client, auth_headers, session["id"], db)
    assert draft["status"] == "draft"

    r = client.post(f"{BASE}/drafts/{draft['id']}/apply", headers=auth_headers,
                    json={"newOntology": {"name": "订单管理本体", "domain": "供应链"}})
    assert r.status_code == 200, r.text
    result = r.json()["data"]
    oid = result["ontologyId"]
    assert result["created"] == {"objectTypes": 3, "linkTypes": 1, "actions": 1,
                                 "functions": 1, "sentinels": 3}

    # 图谱编辑器数据通道可见（与编辑器同一 /full 端点）
    r = client.get(f"/api/v2/formal/ontologies/{oid}/full", headers=auth_headers)
    full = r.json()["data"]
    names = {o["name"] for o in full["objectTypes"]}
    assert names == {"Order", "Supplier", "Finance"}
    order = next(o for o in full["objectTypes"] if o["name"] == "Order")
    assert order["primaryKey"] == next(p["id"] for p in order["properties"]
                                        if p["name"] == "order_no")
    assert full["linkTypes"][0]["cardinality"] == "many-to-one"
    action = full["actions"][0]
    assert action["requiresApproval"] is True
    assert action["objectTypeId"] == order["id"]      # 名称引用被解析成真实 id

    # 激活函数落地：enabled=false 休眠、绑定解析成真实对象 id、待形式化标记
    fns = full["functions"]
    assert len(fns) == 1
    assert fns[0]["enabled"] is False and fns[0]["functionType"] == "object"
    assert fns[0]["targetObjectTypeId"] == order["id"]
    assert fns[0]["displayName"].startswith("待形式化")

    # 哨兵落地：三重闸门（muted + enabled=false + status=draft），不进执行链路
    from app.ontologies.sentinels.models import Sentinel
    sens = {s.name: s for s in db.query(Sentinel).filter(Sentinel.ontology_id == oid)}
    assert set(sens) == {"orphan_rule", "order_paid", "daily_check"}
    assert all(s.muted and not s.enabled and s.status == "draft" for s in sens.values())
    assert sens["order_paid"].bindings[0]["objectTypeId"] == order["id"]
    assert sens["order_paid"].primary_alias == "a" and sens["order_paid"].on_change
    assert sens["daily_check"].on_schedule and not sens["daily_check"].on_change
    assert sens["orphan_rule"].bindings == [] and sens["orphan_rule"].primary_alias is None

    # A newly materialized ontology is born with a complete immutable v0
    # release, regardless of its legacy project-level editing status.
    from app.models.ontology import OntologyProject
    from app.models.ontology_version import OntologyVersion
    project = db.query(OntologyProject).filter_by(id=oid).one()
    assert project.projection_status == "ready"
    release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    assert release.version_number == "v0"
    assert release.node_kind == "release"
    assert release.lifecycle_status == "released"
    assert {item["name"] for item in release.snapshot_formal["objectTypes"]} == {
        "Order", "Supplier", "Finance",
    }

    # v0 业务语义层：画布（剔除 _document_source 元键）+ 文档 + 指纹 + 血缘；
    # 草稿回填落地版本，响应透出 versionId/versionNumber 供前端跳转
    import hashlib

    from app.exploration.document import canvas_fingerprint
    from app.exploration.models import ExplorationDocument, ExplorationDraft
    assert result["versionId"] == release.id
    assert result["versionNumber"] == "v0"
    semantic = release.snapshot_semantic
    assert semantic and semantic["semanticRevision"] == 1
    assert semantic["sourceSessionId"] == session["id"]
    document = db.query(ExplorationDocument).filter_by(
        id=draft["documentId"]).one()
    assert semantic["sourceDocumentId"] == document.id
    assert semantic["documentMd"] == document.content_md
    assert semantic["documentTitle"] == document.title
    assert semantic["documentFingerprint"] == hashlib.sha256(
        document.content_md.encode("utf-8")).hexdigest()
    assert "_document_source" not in semantic["canvas"]
    assert semantic["canvasFingerprint"] == canvas_fingerprint(semantic["canvas"])
    assert semantic["updatedAt"]
    stored_draft = db.query(ExplorationDraft).filter_by(id=draft["id"]).one()
    assert stored_draft.applied_version_id == release.id

    # 血缘：五类元素都带 source（session/document/draft/draftKey/sourceRefs）
    from app.ontologies.formal_modeling.models import (ActionType, LinkType,
                                                       ObjectType, OntologyFunction)
    for model in (ObjectType, LinkType, ActionType, OntologyFunction, Sentinel):
        rows = db.query(model).filter(model.ontology_id == oid).all()
        assert rows and all(
            (x.source or {}).get("kind") == "business_exploration"
            and x.source.get("draftId") == draft["id"]
            and x.source.get("sessionId") == session["id"]
            and x.source.get("draftKey") and x.source.get("sourceRefs")
            for x in rows), f"{model.__name__} 血缘缺失"


def test_draft_apply_keeps_truth_and_failed_fence_when_rebuild_fails(
    client, auth_headers, session, db, monkeypatch,
):
    from app.exploration import application_service
    from app.exploration.models import ExplorationDraft
    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import ObjectType
    from app.ontologies import projection_state

    draft = _make_draft(client, auth_headers, session["id"], db)
    lock_held = {"value": False}

    @contextmanager
    def observed_lock(_db, _ontology_id):
        lock_held["value"] = True
        try:
            yield
        finally:
            lock_held["value"] = False

    def fail_rebuild(session_db, ontology_id):
        assert lock_held["value"] is True
        return projection_state.rebuild_after_commit(
            session_db,
            ontology_id,
            rebuild=lambda _target_ontology_id: False,
            run_in_test=True,
        )

    monkeypatch.setattr(
        application_service,
        "_ontology_build_lock",
        observed_lock,
    )
    monkeypatch.setattr(
        application_service,
        "rebuild_after_commit",
        fail_rebuild,
    )
    response = client.post(
        f"{BASE}/drafts/{draft['id']}/apply",
        headers=auth_headers,
        json={"newOntology": {"name": "投影失败仍保留真相"}},
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "ontology_projection_failed"
    db.expire_all()
    project = db.query(OntologyProject).filter_by(
        name="投影失败仍保留真相",
    ).one()
    assert db.query(ObjectType).filter_by(
        ontology_id=project.id,
    ).count() == 3
    stored_draft = db.query(ExplorationDraft).filter_by(id=draft["id"]).one()
    assert stored_draft.status == "applied"
    assert stored_draft.applied_ontology_id == project.id
    assert project.projection_status == "failed"
    assert "incomplete" in (project.projection_error or "")
    assert lock_held["value"] is False


def test_draft_reapply_partial_then_rest(client, auth_headers, session, db):
    """部分勾选落地后，剩余元素可二次落地到同一本体；重复应用同名跳过（幂等）。"""
    draft = _make_draft(client, auth_headers, session["id"], db)

    r = client.post(f"{BASE}/drafts/{draft['id']}/apply", headers=auth_headers,
                    json={"selectedKeys": ["obj:order"],
                          "newOntology": {"name": "分批落地本体"}})
    assert r.status_code == 200, r.text
    first = r.json()["data"]
    assert first["created"]["objectTypes"] == 1 and first["created"]["sentinels"] == 0

    # 二次应用（全选）：不再 409，固定合并进首次的本体；已落地的 Order 同名跳过
    r = client.post(f"{BASE}/drafts/{draft['id']}/apply", headers=auth_headers, json={})
    assert r.status_code == 200, r.text
    second = r.json()["data"]
    assert second["ontologyId"] == first["ontologyId"]
    assert second["created"]["objectTypes"] == 2          # Supplier + Finance
    assert second["created"]["linkTypes"] == 1            # 端点绑到首批落地的 Order
    assert second["created"]["functions"] == 1 and second["created"]["sentinels"] == 3
    assert any(s["key"] == "obj:order" for s in second["skipped"])

    # 三次应用：全部同名跳过，零新建（幂等收敛）
    r = client.post(f"{BASE}/drafts/{draft['id']}/apply", headers=auth_headers, json={})
    third = r.json()["data"]
    assert third["ontologyId"] == first["ontologyId"]
    assert all(v == 0 for v in third["created"].values())


def test_draft_discard(client, auth_headers, session, db):
    """废弃草稿：幂等；废弃后不可应用。"""
    draft = _make_draft(client, auth_headers, session["id"], db)

    r = client.post(f"{BASE}/drafts/{draft['id']}/discard", headers=auth_headers)
    assert r.status_code == 200 and r.json()["data"]["status"] == "discarded"
    # 幂等
    r = client.post(f"{BASE}/drafts/{draft['id']}/discard", headers=auth_headers)
    assert r.status_code == 200 and r.json()["data"]["status"] == "discarded"

    r = client.post(f"{BASE}/drafts/{draft['id']}/apply", headers=auth_headers,
                    json={"newOntology": {"name": "不该被创建"}})
    assert r.status_code == 409


def test_draft_conservative_merge(client, auth_headers, session, db, ontology):
    """合并进已有本体：走版本正门 —— 合并进草稿版本快照（同名跳过、端点绑定到
    既有类型），不触碰 fo_* live 表、不新建 release、不重建图投影。"""
    from app.exploration.models import ExplorationDraft, ExplorationSession
    from app.models.ontology_version import OntologyVersion
    from app.ontologies.formal_modeling.models import LinkType, ObjectType
    from app.ontologies.versions.snapshot_contract import (
        complete_snapshot,
        snapshot_hash,
    )

    oid = ontology["id"]
    r = client.put(f"/api/v2/formal/ontologies/{oid}/full", headers=auth_headers, json={
        "objectTypes": [{"id": "ot-order", "name": "Order", "displayName": "已有订单",
                         "primaryKey": "order_no",
                         "properties": [{"id": "p1", "name": "order_no", "displayName": "订单号",
                                         "type": "string", "required": True}],
                         "positionX": 0, "positionY": 0}],
        "linkTypes": [], "actions": [], "functions": [], "instances": [], "linkInstances": [],
    })
    assert r.status_code == 200, r.text
    # “目标本体已有 Order”在版本域的含义是发布快照内含 Order：
    # 把 live 表内容同步进 v0 基线（live 投影与发布基线一致的真实状态）。
    release = db.query(OntologyVersion).filter_by(
        ontology_id=oid, version_number="v0").one()
    snap = complete_snapshot(release.snapshot_formal)
    snap["objectTypes"].append({
        "id": "ot-order", "name": "Order", "displayName": "已有订单",
        "primaryKey": "p1",
        "properties": [{"id": "p1", "name": "order_no", "displayName": "订单号",
                        "type": "string", "required": True}],
        "positionX": 0, "positionY": 0,
    })
    release.snapshot_formal = snap
    release.snapshot_hash = snapshot_hash(snap)
    db.commit()

    draft = _make_draft(client, auth_headers, session["id"], db, target_ontology_id=oid)
    conflicted = {o["name"]: o["conflict"] for o in draft["draft"]["objectTypes"]}
    assert conflicted["Order"] is True and conflicted["Supplier"] is False
    assert any("Order" in c for c in draft["report"]["conflicts"])

    live_objects_before = db.query(ObjectType).filter(
        ObjectType.ontology_id == oid).count()
    live_links_before = db.query(LinkType).filter(
        LinkType.ontology_id == oid).count()

    r = client.post(f"{BASE}/drafts/{draft['id']}/apply", headers=auth_headers, json={})
    assert r.status_code == 200, r.text
    result = r.json()["data"]
    assert result["ontologyId"] == oid
    assert result["created"]["objectTypes"] == 2      # Order 被跳过
    assert any("Order" in s["reason"] for s in result["skipped"])
    assert result["created"]["linkTypes"] == 1        # Order→Supplier 绑到既有 ot-order

    # 断点修复回归：合并不触碰 live 表、不新建 release、不重建图投影
    assert db.query(ObjectType).filter(
        ObjectType.ontology_id == oid).count() == live_objects_before
    assert db.query(LinkType).filter(
        LinkType.ontology_id == oid).count() == live_links_before
    assert db.query(OntologyVersion).filter_by(
        ontology_id=oid, node_kind="release").count() == 1

    # 元素落在目标草稿版本快照中，端点解析到既有 ot-order
    target_version = db.query(OntologyVersion).filter_by(
        id=result["versionId"]).one()
    assert target_version.node_kind == "draft"
    assert target_version.lifecycle_status == "editing"
    assert result["versionNumber"] == target_version.version_number
    merged = complete_snapshot(target_version.snapshot_formal)
    assert {o["name"] for o in merged["objectTypes"]} == {
        "Order", "Supplier", "Finance"}
    link = merged["linkTypes"][0]
    assert link["sourceObjectTypeId"] == "ot-order"   # 端点解析到既有对象类型

    # 草稿/会话锚点回填
    stored_draft = db.query(ExplorationDraft).filter_by(id=draft["id"]).one()
    assert stored_draft.applied_ontology_id == oid
    assert stored_draft.applied_version_id == target_version.id
    stored_session = db.query(ExplorationSession).filter_by(
        id=session["id"]).one()
    assert stored_session.ontology_id == oid
    assert stored_session.ontology_version_id == target_version.id


def test_apply_requires_new_ontology_name(client, auth_headers, session, db):
    draft = _make_draft(client, auth_headers, session["id"], db)
    r = client.post(f"{BASE}/drafts/{draft['id']}/apply", headers=auth_headers, json={})
    assert r.status_code == 422
    r = client.post(f"{BASE}/drafts/{draft['id']}/apply", headers=auth_headers,
                    json={"selectedKeys": [], "newOntology": {"name": "x"}})
    assert r.status_code == 422


# ---------------------------------------------------------------- 技能（use_skill 渐进披露）


def test_exploration_skills_are_bundled_and_isolated_from_database():
    from app.exploration.skills import exploration_skills

    first = exploration_skills()
    second = exploration_skills()
    assert set(first) == {"er_diagram", "business_flowchart"}
    assert first is not second
    assert "erDiagram" in first["er_diagram"].instructions
    assert "flowchart TD" in first["business_flowchart"].instructions


def test_chat_with_skill_catalog_and_use_skill(client, auth_headers, session, db,
                                               admin_user, monkeypatch):
    """内置技能目录注入系统提示；use_skill 按需取得完整指令。"""
    _fake_model_config(db, admin_user)
    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            sysmsg = messages[0]["content"]
            assert "可用技能" in sysmsg
            assert "er_diagram" in sysmsg and "business_flowchart" in sysmsg
            # 目录只有一句话描述，不含全文指令（渐进披露）
            assert "输出契约" not in sysmsg
            assert any(t["name"] == "use_skill" for t in tools)
            return {"content": None, "usage": None,
                    "tool_calls": [{"id": "t1", "name": "use_skill",
                                    "arguments": {"name": "er_diagram"}}]}
        # 第二轮：全文指令已回填
        assert "erDiagram" in messages[-1]["content"]
        return {"content": "```mermaid\nerDiagram\n```", "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "画个ER图", "stream": False})
    data = r.json()["data"]
    assert data["error"] is None
    assert data["steps"][0]["tool"] == "use_skill"
    assert "激活技能" in data["steps"][0]["summary"]
    assert "mermaid" in data["content"]


def test_use_skill_unknown_name(client, auth_headers, session, db, admin_user, monkeypatch):
    _fake_model_config(db, admin_user)
    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None, "usage": None,
                    "tool_calls": [{"id": "t1", "name": "use_skill",
                                    "arguments": {"name": "no_such"}}]}
        assert "不存在或未启用" in messages[-1]["content"]
        return {"content": "抱歉。", "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "画图", "stream": False})
    assert r.json()["data"]["steps"][0].get("error")


# ---------------------------------------------------------------- 确定性 ER 图


def test_er_mermaid_deterministic():
    from app.exploration.diagram import er_mermaid
    text = er_mermaid(_quantified_canvas())
    assert text.startswith("erDiagram")
    assert "string order_no PK" in text          # 主键标记
    assert "number amount" in text               # 类型映射
    assert 'Order }o--|| Customer : "下单客户"' in text
    from app.exploration.diagram import DiagramError
    with pytest.raises(DiagramError, match="质量校验未通过"):
        er_mermaid(_demo_canvas())                # 悬空端点/缺基数不再静默丢边或猜默认值
    with pytest.raises(DiagramError):
        er_mermaid({})                           # 空画布 → 指明先补什么


def test_er_diagram_endpoint(client, auth_headers, session, db):
    r = client.get(f"{BASE}/sessions/{session['id']}/diagrams/er", headers=auth_headers)
    assert r.status_code == 422                  # 空画布拒绝

    _seed_canvas(db, session["id"], _demo_canvas())
    r = client.get(f"{BASE}/sessions/{session['id']}/diagrams/er", headers=auth_headers)
    assert r.status_code == 422                  # 质量不合格的 ER 图不渲染半成品
    _seed_canvas(db, session["id"], _quantified_canvas())
    r = client.get(f"{BASE}/sessions/{session['id']}/diagrams/er", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"]["mermaid"].startswith("erDiagram")


# ---------------------------------------------------------------- 会话附件


def _upload(client, headers, sid, filename, content, content_type="text/plain"):
    return client.post(f"{BASE}/sessions/{sid}/attachments", headers=headers,
                       files={"file": (filename, content, content_type)})


def test_attachment_upload_list_inject_delete(client, auth_headers, session, db,
                                              tmp_path, monkeypatch):
    """上传→列表→注入对话上下文→删除 全链路。"""
    from app.config import settings
    from app.exploration.orchestrator import _attachments_block
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    sid = session["id"]

    r = _upload(client, auth_headers, sid, "spec.md",
                "# 采购单\n采购单包含供应商、金额、下单日期。".encode("utf-8"))
    assert r.status_code == 201, r.text
    a = r.json()["data"]
    assert a["filename"] == "spec.md" and a["status"] == "ready" and a["charCount"] > 0
    aid = a["id"]

    r = client.get(f"{BASE}/sessions/{sid}/attachments", headers=auth_headers)
    assert r.status_code == 200 and [x["id"] for x in r.json()["data"]] == [aid]

    # 附件内容注入引导师上下文
    block = _attachments_block(db, sid)
    assert "spec.md" in block and "采购单" in block

    r = client.delete(f"{BASE}/sessions/{sid}/attachments/{aid}", headers=auth_headers)
    assert r.status_code == 204
    assert client.get(f"{BASE}/sessions/{sid}/attachments", headers=auth_headers).json()["data"] == []
    assert _attachments_block(db, sid) == ""


def test_attachment_rejects_unsupported_type(client, auth_headers, session, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    r = _upload(client, auth_headers, session["id"], "malware.exe", b"MZ\x90\x00")
    assert r.status_code == 400


def test_attachment_cross_session_isolation(client, auth_headers, tmp_path, monkeypatch):
    """附件严格绑定会话：B 会话看不到 A 会话的附件。"""
    from app.config import settings
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    a = client.post(f"{BASE}/sessions", headers=auth_headers, json={}).json()["data"]["id"]
    b = client.post(f"{BASE}/sessions", headers=auth_headers, json={}).json()["data"]["id"]

    assert _upload(client, auth_headers, a, "note.txt", b"hello world").status_code == 201
    assert client.get(f"{BASE}/sessions/{b}/attachments", headers=auth_headers).json()["data"] == []
    assert len(client.get(f"{BASE}/sessions/{a}/attachments", headers=auth_headers).json()["data"]) == 1


def test_attachment_cascade_on_session_delete(client, auth_headers, db, tmp_path, monkeypatch):
    from app.config import settings
    from app.exploration.models import ExplorationAttachment
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    sid = client.post(f"{BASE}/sessions", headers=auth_headers, json={}).json()["data"]["id"]

    assert _upload(client, auth_headers, sid, "a.txt", b"data").status_code == 201
    assert db.query(ExplorationAttachment).filter_by(session_id=sid).count() == 1

    assert client.delete(f"{BASE}/sessions/{sid}", headers=auth_headers).status_code == 204
    assert db.query(ExplorationAttachment).filter_by(session_id=sid).count() == 0


def test_workspace_text_crud_download_and_version_conflict(client, auth_headers, session,
                                                           tmp_path, monkeypatch):
    """会话空间：创建/读取/乐观锁保存/下载/路径防穿越。"""
    from app.config import settings
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    sid = session["id"]

    r = client.post(f"{BASE}/sessions/{sid}/workspace/files", headers=auth_headers,
                    json={"path": "notes/domain.md", "content": "# v1\n订单模型"})
    assert r.status_code == 201, r.text
    item = r.json()["data"]
    assert item["relativePath"] == "notes/domain.md" and item["editable"] is True
    assert item["version"] == 1 and len(item["sha256"]) == 64

    aid = item["id"]
    r = client.get(f"{BASE}/sessions/{sid}/attachments/{aid}/content", headers=auth_headers)
    assert r.status_code == 200 and "订单模型" in r.json()["data"]["content"]
    preview = client.get(f"{BASE}/sessions/{sid}/attachments/{aid}/preview", headers=auth_headers)
    assert preview.status_code == 200
    assert preview.json()["data"]["editable"] is True
    assert "订单模型" in preview.json()["data"]["content"]

    r = client.put(f"{BASE}/sessions/{sid}/attachments/{aid}/content", headers=auth_headers,
                   json={"content": "# v2\n订单与客户", "expectedVersion": 1})
    assert r.status_code == 200 and r.json()["data"]["version"] == 2
    r = client.put(f"{BASE}/sessions/{sid}/attachments/{aid}/content", headers=auth_headers,
                   json={"content": "stale", "expectedVersion": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "workspace_version_conflict"

    r = client.get(f"{BASE}/sessions/{sid}/attachments/{aid}/download", headers=auth_headers)
    assert r.status_code == 200 and r.content.decode("utf-8") == "# v2\n订单与客户"

    r = client.post(f"{BASE}/sessions/{sid}/workspace/files", headers=auth_headers,
                    json={"path": "../escape.md", "content": "bad"})
    assert r.status_code == 422


def test_workspace_file_id_isolation(client, auth_headers, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    a = client.post(f"{BASE}/sessions", headers=auth_headers, json={}).json()["data"]["id"]
    b = client.post(f"{BASE}/sessions", headers=auth_headers, json={}).json()["data"]["id"]
    item = client.post(f"{BASE}/sessions/{a}/workspace/files", headers=auth_headers,
                       json={"path": "secret.md", "content": "session-a"}).json()["data"]
    assert client.get(f"{BASE}/sessions/{b}/attachments/{item['id']}/content",
                      headers=auth_headers).status_code == 404
    assert client.get(f"{BASE}/sessions/{b}/attachments/{item['id']}/download",
                      headers=auth_headers).status_code == 404
    assert client.get(f"{BASE}/sessions/{b}/attachments/{item['id']}/preview",
                      headers=auth_headers).status_code == 404


def test_workspace_binary_preview_uses_extracted_text(client, auth_headers, session,
                                                       tmp_path, monkeypatch):
    """Office/PDF 等非可编辑文件在工作区中返回确定性抽取文本。"""
    from types import SimpleNamespace
    from app.config import settings
    from app.exploration import workspace as W
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    monkeypatch.setattr(W, "convert_document", lambda *_: SimpleNamespace(
        content="售后流程规范：接单、派单、维修、回访。", ok=True, error=None))

    uploaded = _upload(client, auth_headers, session["id"], "manual.pdf",
                       b"%PDF-visual-preview", "application/pdf")
    assert uploaded.status_code == 201
    item = uploaded.json()["data"]
    assert item["editable"] is False

    preview = client.get(
        f"{BASE}/sessions/{session['id']}/attachments/{item['id']}/preview",
        headers=auth_headers)
    assert preview.status_code == 200
    assert preview.json()["data"]["editable"] is False
    assert "接单、派单" in preview.json()["data"]["content"]


def test_workspace_agent_tool_round_trip(db, session, tmp_path, monkeypatch):
    from app.config import settings
    from app.exploration.models import ExplorationSession
    from app.exploration.toolkit import ExplorationToolRunner
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    row = db.query(ExplorationSession).filter_by(id=session["id"]).first()
    runner = ExplorationToolRunner(db, row)
    created = runner.run("manage_workspace_file", {
        "action": "create", "path": "agent/summary.md", "content": "v1"})
    assert created["created"] is True
    listed = runner.run("manage_workspace_file", {"action": "list"})
    assert listed["files"][0]["path"] == "agent/summary.md"
    updated = runner.run("manage_workspace_file", {
        "action": "update", "file_id": created["id"], "content": "v2",
        "expected_version": 1})
    assert updated["version"] == 2
    assert runner.run("manage_workspace_file", {
        "action": "read", "file_id": created["id"]})["content"] == "v2"
    # Real tool-calling models may place list.path in file_id; keep this safe
    # compatibility inside the current session rather than failing the turn.
    assert runner.run("manage_workspace_file", {
        "action": "read", "file_id": "agent/summary.md"})["content"] == "v2"


# ---------------------------------------------------------------- 澄清账本（questions）


def test_questions_ledger_quant_discipline():
    """账本三纪律：同题去重；堵门问题模糊结论拒收；定量/点选结论放行。"""
    from app.exploration import questions as Q

    cv = C.empty_canvas()
    cv, ids, errs = Q.raise_questions(cv, [
        {"question": "大额订单的门槛是多少？", "kind": "blocking",
         "target": "Order.amount", "options": ["≥10000元", "≥50000元"]},
        {"question": "订单状态有哪些？", "kind": "blocking"},
        {"question": "订单编号建议用系统流水号", "kind": "advisory",
         "suggestion": "ORD-yyyyMMdd-序号"},
    ])
    assert not errs and len(ids) == 3
    # 同题重复登记 → 复用既有 id，不新增
    cv, ids2, _ = Q.raise_questions(cv, [{"question": "大额订单的门槛是多少？",
                                          "kind": "blocking"}])
    assert ids2 == [ids[0]] and len(Q.get_questions(cv)) == 3

    # 模糊结论 → 拒收并说明命中的模糊词
    cv, done, errs = Q.resolve_questions(cv, [{"id": ids[0], "resolution": "金额较大时算大额"}])
    assert not done and any("较大" in e for e in errs)
    assert Q.open_questions(cv, "blocking") and len(Q.open_questions(cv)) == 3

    # 定量结论 → 销账；枚举清单也算定量
    cv, done, errs = Q.resolve_questions(cv, [
        {"id": ids[0], "resolution": "≥50000元"},
        {"id": ids[1], "resolution": "待支付/已支付/已发货/已完成/已取消"},
    ])
    assert len(done) == 2 and not errs
    # advisory 可用原文销账；dismissed 需写原因
    cv, done, errs = Q.resolve_questions(cv, [
        {"id": "订单编号建议用系统流水号", "resolution": "用户确认采用建议值",
         "status": "dismissed"}])
    assert len(done) == 1 and not Q.open_questions(cv)


# ---------------------------------------------------------------- 质量门（readiness）


def _quantified_canvas() -> dict:
    """一份全绿画布：主键/基数/定量规则/事件来源/场景引用全部齐备。"""
    cv = C.empty_canvas()
    cv, _, _ = C.upsert_elements(cv, "object", [
        {"name": "Order", "displayName": "订单", "keyAttribute": "order_no",
         "attributes": [
             {"name": "order_no", "displayName": "订单号", "typeHint": "文本", "required": True},
             {"name": "amount", "displayName": "金额", "typeHint": "金额"},
             {"name": "status", "displayName": "状态", "typeHint": "枚举",
              "enum": ["待支付", "已支付", "已取消"]},
         ],
         "relations": [{"target": "Customer", "displayName": "下单客户",
                        "cardinality": "many-to-one"}]},
        {"name": "Customer", "displayName": "客户", "keyAttribute": "customer_no",
         "attributes": [{"name": "customer_no", "displayName": "客户编码",
                         "typeHint": "文本", "required": True}]},
    ])
    cv, _, _ = C.upsert_elements(cv, "actor", [
        {"name": "Sales", "displayName": "销售", "kind": "role"},
    ])
    cv, _, _ = C.upsert_elements(cv, "behavior", [
        {"name": "confirm_pay", "displayName": "确认支付", "actor": "Sales",
         "object": "Order", "trigger": "收到银行回单",
         "outcome": "订单从待支付变为已支付"},
        {"name": "cancel_order", "displayName": "取消订单", "actor": "Sales",
         "object": "Order", "trigger": "客户在支付前取消",
         "outcome": "订单从待支付变为已取消"},
    ])
    cv, _, _ = C.upsert_elements(cv, "rule", [
        {"name": "big_amount", "displayName": "大额审批", "kind": "approval",
         "appliesTo": "confirm_pay", "statement": "金额 ≥ 50000 元需要财务总监审批"},
    ])
    cv, _, _ = C.upsert_elements(cv, "event", [
        {"name": "order_paid", "displayName": "订单已支付", "source": "confirm_pay",
         "consequences": ["通知仓库发货"]},
    ])
    cv, _, _ = C.upsert_elements(cv, "scenario", [
        {"name": "pay_flow", "displayName": "支付流程", "goal": "完成订单支付",
         "actors": ["Sales"], "steps": ["销售确认回单", "如果金额 ≥ 50000 元则走审批", "订单变为已支付"],
         "objects": ["Order", "Customer"], "behaviors": ["confirm_pay", "cancel_order"],
         "branches": [
             {"fromStep": 2, "toStep": 3, "condition": "金额 ≥ 50000 元，审批通过"},
             {"fromStep": 2, "toStep": 3, "condition": "金额 < 50000 元"},
         ],
         "expected_outcome": "订单进入已支付状态"}])
    return cv


def test_readiness_gates_block_and_pass():
    from app.exploration import readiness as R

    # demo 画布：悬空关系 / Supplier 缺主键 / 场景引用未定义元素 → 未就绪
    rd = R.evaluate(_demo_canvas())
    assert rd["ready"] is False and rd["blockingCount"] > 0
    by_id = {g["id"]: g for g in rd["gates"]}
    assert not by_id["relations"]["passed"]           # Ghost 悬空关系
    assert any("Ghost" in i for i in by_id["relations"]["blockingItems"])
    assert not by_id["objects"]["passed"]             # Supplier 缺主键
    assert any("Supplier" in i or "供应商" in i for i in by_id["objects"]["blockingItems"])
    assert not by_id["coverage"]["passed"]            # Invoice/refund 未定义
    # 模糊规则拦截：加一条未定量规则
    cv = _demo_canvas()
    cv, _, _ = C.upsert_elements(cv, "rule", [
        {"name": "vague_rule", "kind": "alert", "appliesTo": "Order",
         "statement": "订单长时间未支付要尽快提醒"}])
    rd2 = R.evaluate(cv)
    by_id2 = {g["id"]: g for g in rd2["gates"]}
    assert any("vague_rule" in i and "定量" in i for i in by_id2["rules"]["blockingItems"])

    # 全绿画布 → ready；开放堵门问题会把它拦回来
    from app.exploration import questions as Q
    good = _quantified_canvas()
    rd3 = R.evaluate(good)
    assert rd3["ready"] is True, rd3
    assert "已就绪" in rd3["stage"]
    good2, _, _ = Q.raise_questions(good, [{"question": "退货窗口几天？", "kind": "blocking"}])
    rd4 = R.evaluate(good2)
    assert rd4["ready"] is False and rd4["openQuestions"]["blocking"] == 1


def test_readiness_endpoint(client, auth_headers, session, db):
    _seed_canvas(db, session["id"], _quantified_canvas())
    r = client.get(f"{BASE}/sessions/{session['id']}/readiness", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ready"] is True and data["gatesPassed"] == data["gatesTotal"]
    # 会话详情与画布端点同样带 readiness（前端头部即时展示）
    r = client.get(f"{BASE}/sessions/{session['id']}", headers=auth_headers)
    assert r.json()["data"]["readiness"]["ready"] is True


# ---------------------------------------------------------------- 图表（确定性生成）


def test_diagram_builders():
    from app.exploration import diagram as D

    cv = _quantified_canvas()
    er = D.build_diagram(cv, "er")
    assert er["mermaid"].startswith("erDiagram")
    assert "order_no PK" in er["mermaid"] and "}o--||" in er["mermaid"]

    flow = D.build_diagram(cv, "flow", "支付流程")
    assert flow["mermaid"].startswith("flowchart")
    assert "S1" in flow["mermaid"] and "SE" in flow["mermaid"]
    assert 'S2{' in flow["mermaid"] and "金额 ≥ 50000" in flow["mermaid"]
    assert flow["warnings"] == []

    seq = D.build_diagram(cv, "sequence")
    assert seq["mermaid"].startswith("sequenceDiagram")
    assert "Sales" in seq["mermaid"] and "Order" in seq["mermaid"]

    st = D.build_diagram(cv, "state", "Order")
    assert st["mermaid"].startswith("stateDiagram-v2")
    assert "待支付" in st["mermaid"] and "已支付" in st["mermaid"]
    # 行为 outcome「从待支付变为已支付」→ 识别为状态迁移边
    assert "-->" in st["mermaid"] and "确认支付" in st["mermaid"]

    # 条件不足 → DiagramError 指明先补什么
    with pytest.raises(D.DiagramError):
        D.build_diagram(C.empty_canvas(), "er")
    with pytest.raises(D.DiagramError):
        D.build_diagram(cv, "state", "Customer")       # 无枚举状态属性


def test_state_diagram_rejects_isolated_and_inconsistent_states():
    """状态图是质量投影：枚举/行为口径不一致或孤点时先反馈修复，不渲染半成品。"""
    from app.exploration import diagram as D
    from app.exploration import readiness as R

    cv = _quantified_canvas()
    order = next(o for o in cv["objects"] if o["name"] == "Order")
    status = next(a for a in order["attributes"] if a["name"] == "status")
    status["enum"] = ["pending", "paid", "cancelled", "archived"]
    confirm = next(b for b in cv["behaviors"] if b["name"] == "confirm_pay")
    confirm["outcome"] = "订单状态从待支付变为已支付"

    analysis = D.state_model_analysis(cv, "Order")
    assert any("不在枚举" in issue for issue in analysis["issues"])
    assert any("孤立状态" in issue or "没有可验证" in issue for issue in analysis["issues"])
    with pytest.raises(D.DiagramError, match="质量校验未通过"):
        D.build_diagram(cv, "state", "Order")
    lifecycle_gate = next(g for g in R.evaluate(cv)["gates"] if g["id"] == "lifecycles")
    assert lifecycle_gate["passed"] is False


def test_non_state_enum_cannot_be_rendered_as_lifecycle():
    from app.exploration import diagram as D

    cv = _quantified_canvas()
    customer = next(o for o in cv["objects"] if o["name"] == "Customer")
    customer["attributes"].append({"name": "segment", "type_hint": "枚举",
                                   "enum": ["普通", "重点"]})
    with pytest.raises(D.DiagramError, match="普通枚举不能自动当作生命周期"):
        D.build_diagram(cv, "state", "Customer")


def test_flow_and_sequence_reject_partial_structures():
    from app.exploration import diagram as D

    cv = _quantified_canvas()
    scenario = cv["scenarios"][0]
    scenario["branches"] = []
    with pytest.raises(D.DiagramError, match="至少需要 2 条显式 branches"):
        D.build_diagram(cv, "flow", scenario["name"])

    scenario["branches"] = [
        {"from_step": 2, "to_step": 3, "condition": "通过"},
        {"from_step": 2, "to_step": 99, "condition": "驳回"},
    ]
    with pytest.raises(D.DiagramError, match="不在步骤范围"):
        D.build_diagram(cv, "flow", scenario["name"])

    scenario["behaviors"].append("missing_behavior")
    with pytest.raises(D.DiagramError, match="未定义行为"):
        D.build_diagram(cv, "sequence", scenario["name"])


def test_diagram_endpoint(client, auth_headers, session, db):
    _seed_canvas(db, session["id"], _quantified_canvas())
    r = client.get(f"{BASE}/sessions/{session['id']}/diagrams/er", headers=auth_headers)
    assert r.status_code == 200 and r.json()["data"]["mermaid"].startswith("erDiagram")
    r = client.get(f"{BASE}/sessions/{session['id']}/diagrams/flow",
                   headers=auth_headers, params={"target": "支付流程"})
    assert r.status_code == 200
    r = client.get(f"{BASE}/sessions/{session['id']}/diagrams/state",
                   headers=auth_headers, params={"target": "Customer"})
    assert r.status_code == 422                        # 条件不足给出可操作提示
    r = client.get(f"{BASE}/sessions/{session['id']}/diagrams/nope", headers=auth_headers)
    assert r.status_code == 422


def test_history_revalidates_stored_diagram_against_current_canvas(client, auth_headers, session, db):
    """旧消息中的半成品图不能绕过新质量门继续回放。"""
    cv = _quantified_canvas()
    order = next(o for o in cv["objects"] if o["name"] == "Order")
    next(a for a in order["attributes"] if a["name"] == "status")["enum"].append("已归档")
    _seed_canvas(db, session["id"], cv)
    db.add(ExplorationMessage(
        session_id=session["id"], role="assistant", content="历史状态图",
        steps=[{
            "tool": "show_diagram", "arguments": {"kind": "state", "target": "Order"},
            "summary": "展示状态图", "durationMs": 1,
            "diagram": {"kind": "state", "title": "旧图", "target": "Order",
                        "mermaid": "stateDiagram-v2\n  st0 : 已归档"},
        }],
    ))
    db.commit()

    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=auth_headers).json()["data"]
    step = detail["messages"][-1]["steps"][0]
    assert "diagram" not in step
    assert "历史图表已被质量门拦截" in step["error"]


# ---------------------------------------------------------------- 草稿质量门


def test_draft_gate_blocks_until_ready(client, auth_headers, session, db):
    """未就绪画布：默认拒绝生成草稿（422+报告）；force 越权放行且草稿报告留痕。"""
    _seed_canvas(db, session["id"], _demo_canvas())
    r = client.post(f"{BASE}/sessions/{session['id']}/documents", headers=auth_headers, json={})
    doc_id = r.json()["data"]["id"]

    r = client.post(f"{BASE}/documents/{doc_id}/drafts", headers=auth_headers, json={})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "quality_gate_blocked"
    assert detail["readiness"]["blockingCount"] > 0

    r = client.post(f"{BASE}/documents/{doc_id}/drafts", headers=auth_headers,
                    json={"force": True})
    assert r.status_code == 201
    report = r.json()["data"]["report"]
    assert report["gateOverride"] is True
    assert report["readiness"]["ready"] is False
    assert any("越权" in w for w in report["warnings"])


def test_draft_gate_passes_when_ready(client, auth_headers, session, db):
    """全绿画布：无需 force 直接放行，报告记录就绪状态。"""
    _seed_canvas(db, session["id"], _quantified_canvas())
    r = client.post(f"{BASE}/sessions/{session['id']}/documents", headers=auth_headers, json={})
    doc_id = r.json()["data"]["id"]
    r = client.post(f"{BASE}/documents/{doc_id}/drafts", headers=auth_headers, json={})
    assert r.status_code == 201, r.text
    report = r.json()["data"]["report"]
    assert report["readiness"]["ready"] is True
    assert "gateOverride" not in report
    assert report["validation"]["valid"] is True


def test_draft_selection_validation_enforces_dependency_closure():
    """关系/动作不能脱离所依赖的对象单独落地；完整选择集通过。"""
    draft, _ = CV.build_draft(_quantified_canvas())
    assert CV.validate_draft_selection(draft)["valid"] is True
    link_key = draft["linkTypes"][0]["key"]
    invalid = CV.validate_draft_selection(draft, [link_key])
    assert invalid["valid"] is False
    codes = {item["code"] for item in invalid["errors"]}
    assert {"missing_source_dependency", "missing_target_dependency"} <= codes


def test_context_compaction_keeps_recent_messages_and_canvas_authority(db, session):
    from app.exploration.models import ExplorationMessage, ExplorationSession
    from app.exploration.orchestrator import _prepare_history

    row = db.query(ExplorationSession).filter_by(id=session["id"]).first()
    for i in range(40):
        db.add(ExplorationMessage(session_id=row.id,
                                  role="user" if i % 2 == 0 else "assistant",
                                  content=f"第 {i + 1} 条业务讨论：订单状态与规则"))
    db.commit()

    system, recent = _prepare_history(
        db, row, {"max_context_tokens": 16_384, "max_output_tokens": 2_048},
        "继续讨论", "", {})
    assert len(recent) == 16
    assert row.summary_message_count == 24
    assert "已压缩消息 1-24" in row.context_summary
    assert "当前画布（权威状态" in system
    assert row.context_stats["contextLimit"] == 16_384


def test_small_context_budgets_every_provider_call_and_final_summary(
    client, auth_headers, session, db, admin_user, monkeypatch,
):
    """8K 窗口 + 连续 8 个近 6000 字结果：每次调用（含第 9 次总结）都不得超窗。"""
    from app.models.model_config import ModelConfig
    from app.exploration import orchestrator as OR
    from app.exploration.models import ExplorationSession
    from app.exploration.toolkit import ExplorationToolRunner
    from app.ontologies.agent_runtime import llm_bridge

    _fake_model_config(db, admin_user)
    cfg = db.query(ModelConfig).filter_by(name="fake").one()
    cfg.options = {
        "max_context_tokens": 8_192,
        "max_output_tokens": 4_096,
    }
    db.commit()

    original_run = ExplorationToolRunner.run

    def huge_tool_result(self, name, arguments):
        if name == "manage_workspace_file":
            self.canvas_dirty = False
            self.last_diagram = None
            return {
                "operation": "read",
                "content": "超长工具事实。" * 1_000,
                "canvasVersion": self.session.canvas_version,
                "hasMore": True,
                "nextOffset": 6_000,
            }
        return original_run(self, name, arguments)

    calls: list[int] = []
    saw_visible_page = {"value": False}

    def fake_chat(call_kwargs, messages, tools):
        estimated = OR._estimate_tools(tools) + OR._estimate_messages(messages)
        context_limit = int(call_kwargs["max_context_tokens"])
        output_limit = int(call_kwargs["max_output_tokens"])
        input_budget = (
            context_limit - output_limit - OR._safety_reserve(context_limit))
        assert context_limit == 8_192
        assert output_limit == 1_024
        assert estimated <= input_budget
        calls.append(estimated)
        for item in messages:
            if item.get("role") != "tool":
                continue
            payload = json.loads(item["content"])
            if "content" not in payload:
                continue
            assert payload["content"]
            assert payload["nextOffset"] == (
                payload["offset"] + len(payload["content"]))
            saw_visible_page["value"] = True
        if len(calls) <= 8:
            return {
                "content": None,
                "usage": None,
                "tool_calls": [{
                    "id": f"large-{len(calls)}",
                    "name": "manage_workspace_file",
                    "arguments": {"action": "list"},
                }],
            }
        assert tools == []
        return {
            "content": "已基于预算内的权威检查点完成最终总结。",
            "tool_calls": [],
            "usage": None,
        }

    monkeypatch.setattr(ExplorationToolRunner, "run", huge_tool_result)
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    response = client.post(
        f"{BASE}/sessions/{session['id']}/chat",
        headers=auth_headers,
        json={"message": "连续读取并综合这些资料", "stream": False},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["error"] is None
    assert data["content"] == "已基于预算内的权威检查点完成最终总结。"
    assert len(data["steps"]) == 8
    assert len(calls) == 9
    assert saw_visible_page["value"] is True

    persisted = db.query(ExplorationSession).filter_by(id=session["id"]).one()
    stats = persisted.context_stats
    assert stats["estimatedInputTokens"] <= stats["inputBudget"]
    assert stats["peakProviderEstimatedInputTokens"] <= stats["inputBudget"]


def test_context_smaller_than_supported_minimum_is_rejected_explicitly():
    from app.exploration import orchestrator as OR

    kwargs = {"max_context_tokens": 4_096, "max_output_tokens": 1_024}
    with pytest.raises(OR.ExplorationContextBudgetError, match="至少需要 8192"):
        OR._configure_context_limits(kwargs)


# ---------------------------------------------------------------- 对话内出图与账本工具


def test_chat_show_diagram_and_questions(client, auth_headers, session, db,
                                         admin_user, monkeypatch):
    """假 LLM 依次：登记问题 → 出 ER 图 → 收尾。step 事件应携带 mermaid，
    账本随 canvas 事件对前端可见，消息历史可回放图表。"""
    _fake_model_config(db, admin_user)
    _seed_canvas(db, session["id"], _quantified_canvas())
    calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["n"] += 1
        names = {t["name"] for t in tools}
        assert {"raise_questions", "resolve_questions", "show_diagram"} <= names
        # 系统提示注入质量门与账本
        assert "质量门" in messages[0]["content"] and "澄清账本" in messages[0]["content"]
        if calls["n"] == 1:
            return {"content": None, "usage": None, "tool_calls": [
                {"id": "t1", "name": "raise_questions",
                 "arguments": {"questions": [{"question": "退货窗口是几天？",
                                              "kind": "blocking",
                                              "options": ["7天", "14天"]}]}},
                {"id": "t2", "name": "show_diagram", "arguments": {"kind": "er"}},
            ]}
        return {"content": "请核对 ER 图；另外退货窗口是几天？", "tool_calls": [], "usage": None}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"{BASE}/sessions/{session['id']}/chat", headers=auth_headers,
                    json={"message": "开始吧", "stream": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    steps = data["steps"]
    assert steps[0]["tool"] == "raise_questions" and "登记 1 个" in steps[0]["summary"]
    assert steps[1]["tool"] == "show_diagram"
    assert steps[1]["diagram"]["mermaid"].startswith("erDiagram")   # 图随 step 直达前端
    # 账本进画布 → readiness 拦回未就绪
    assert data["completeness"] is not None
    canvas = data["canvas"]
    assert canvas["questions"][0]["question"] == "退货窗口是几天？"

    # 消息持久化后历史可回放（steps 内含 diagram）
    detail = client.get(f"{BASE}/sessions/{session['id']}", headers=auth_headers).json()["data"]
    last = detail["messages"][-1]
    assert last["steps"][1]["diagram"]["kind"] == "er"
    assert detail["readiness"]["openQuestions"]["blocking"] == 1
