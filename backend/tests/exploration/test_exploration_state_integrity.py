"""业务探索权威状态与安全增量修改回归测试。"""
from __future__ import annotations

import json
import uuid
from sqlalchemy.orm import sessionmaker

from app.exploration import canvas as C
from app.exploration import orchestrator as OR
from app.exploration import readiness as R
from app.exploration import workspace as W
from app.config import settings
from app.exploration.models import ExplorationSession
from app.exploration.toolkit import ExplorationToolRunner


def _rich_canvas() -> dict:
    canvas = C.empty_canvas()
    canvas, _, errors = C.upsert_elements(canvas, "object", [{
        "name": "Order",
        "displayName": "订单",
        "description": "交易订单",
        "keyAttribute": "order_no",
        "attributes": [
            {"name": "order_no", "displayName": "订单号",
             "typeHint": "文本", "required": True},
            {"name": "amount", "displayName": "金额",
             "typeHint": "金额", "required": True, "notes": "含税"},
        ],
        "relations": [
            {"name": "customer", "target": "Customer",
             "displayName": "下单客户", "cardinality": "many-to-one"},
            {"name": "supplier", "target": "Supplier",
             "displayName": "供货方", "cardinality": "many-to-one"},
        ],
    }])
    assert not errors
    canvas, _, errors = C.upsert_elements(canvas, "behavior", [{
        "name": "confirm_order",
        "actor": "Sales",
        "object": "Order",
        "inputs": [
            {"name": "comment", "typeHint": "文本", "required": False},
            {"name": "channel", "typeHint": "枚举", "enum": ["web", "store"]},
        ],
    }])
    assert not errors
    canvas, _, errors = C.upsert_elements(canvas, "scenario", [{
        "name": "order_flow",
        "goal": "完成下单",
        "steps": ["提交订单", "判断金额", "人工审批", "结束"],
        "branches": [
            {"fromStep": 2, "toStep": 3, "condition": "金额≥50000元"},
            {"fromStep": 2, "toStep": 4, "condition": "金额<50000元"},
        ],
    }])
    assert not errors
    return canvas


def test_structured_lists_merge_sparse_children_without_data_loss():
    canvas = _rich_canvas()
    order = canvas["objects"][0]
    amount = next(item for item in order["attributes"] if item["name"] == "amount")
    customer = next(item for item in order["relations"] if item["name"] == "customer")

    # 父元素和子项都只带 id；未提及的属性/关系及被修改子项的旧字段必须保留。
    canvas, applied, errors = C.upsert_elements(canvas, "object", [{
        "id": order["id"],
        "attributes": [
            {"id": amount["id"], "notes": "含税、保留两位小数"},
            {"name": "currency", "displayName": "币种",
             "typeHint": "枚举", "enum": ["CNY", "USD"]},
        ],
        "relations": [{"id": customer["id"], "cardinality": "one-to-many"}],
    }])
    assert applied and not errors
    updated = canvas["objects"][0]
    attrs = {item["name"]: item for item in updated["attributes"]}
    relations = {item["name"]: item for item in updated["relations"]}
    assert set(attrs) == {"order_no", "amount", "currency"}
    assert attrs["amount"]["type_hint"] == "金额"
    assert attrs["amount"]["required"] is True
    assert attrs["amount"]["notes"] == "含税、保留两位小数"
    assert set(relations) == {"customer", "supplier"}
    assert relations["customer"]["target"] == "Customer"
    assert relations["customer"]["cardinality"] == "one-to-many"

    behavior = canvas["behaviors"][0]
    comment = next(item for item in behavior["inputs"] if item["name"] == "comment")
    canvas, _, errors = C.upsert_elements(canvas, "behavior", [{
        "id": behavior["id"],
        "inputs": [{"id": comment["id"], "required": True}],
    }])
    assert not errors
    inputs = {item["name"]: item for item in canvas["behaviors"][0]["inputs"]}
    assert set(inputs) == {"comment", "channel"}
    assert inputs["comment"]["type_hint"] == "文本" and inputs["comment"]["required"] is True

    scenario = canvas["scenarios"][0]
    terminal = next(item for item in scenario["branches"]
                    if item["condition"] == "金额<50000元")
    canvas, _, errors = C.upsert_elements(canvas, "scenario", [{
        "id": scenario["id"],
        "branches": [{"id": terminal["id"], "toStep": None}],
    }])
    assert not errors
    branches = canvas["scenarios"][0]["branches"]
    assert len(branches) == 2
    terminal_after = next(item for item in branches if item["id"] == terminal["id"])
    assert terminal_after["from_step"] == 2
    assert terminal_after["condition"] == "金额<50000元"
    assert "to_step" not in terminal_after


def test_structured_child_delete_is_explicit_and_empty_list_remains_compatible():
    canvas = _rich_canvas()
    order = canvas["objects"][0]
    amount_id = next(item["id"] for item in order["attributes"]
                     if item["name"] == "amount")
    canvas, _, errors = C.upsert_elements(canvas, "object", [{
        "id": order["id"],
        "attributes": [{"id": amount_id, "_delete": True}],
    }])
    assert not errors
    assert [item["name"] for item in canvas["objects"][0]["attributes"]] == ["order_no"]
    assert len(canvas["objects"][0]["relations"]) == 2

    # 历史工具调用使用 [] 表示整表清空，继续兼容。
    canvas, _, errors = C.upsert_elements(canvas, "object", [{
        "id": order["id"], "relations": [],
    }])
    assert not errors and canvas["objects"][0]["relations"] == []


def test_invalid_nested_patch_is_atomic_for_its_parent_element():
    canvas = _rich_canvas()
    before = json.loads(json.dumps(canvas["objects"][0], ensure_ascii=False))
    amount = next(item for item in before["attributes"] if item["name"] == "amount")
    canvas, applied, errors = C.upsert_elements(canvas, "object", [{
        "id": before["id"],
        "attributes": [
            {"id": amount["id"], "notes": "这项本来合法"},
            {"name": "bad_enum", "typeHint": "枚举", "enum": ["same", " same "]},
        ],
    }])
    assert not applied and errors
    assert canvas["objects"][0] == before


def test_stable_name_collision_is_rejected_and_legacy_dirty_canvas_cannot_crash_readiness():
    canvas = C.empty_canvas()
    canvas, _, errors = C.upsert_elements(canvas, "object", [
        {
            "name": "Alpha",
            "attributes": [{"name": "id", "typeHint": "文本"}],
            "keyAttribute": "id",
        },
        {
            "name": "Beta",
            "attributes": [{
                "name": "status", "displayName": "状态",
                "typeHint": "枚举", "enum": ["新建", "完成"],
            }],
            "keyAttribute": "status",
        },
    ])
    assert not errors
    beta_id = canvas["objects"][1]["id"]

    canvas, applied, errors = C.upsert_elements(canvas, "object", [{
        "id": beta_id, "name": "Alpha",
    }])
    assert not applied
    assert errors and "稳定 name 冲突" in errors[0]
    assert [item["name"] for item in canvas["objects"]] == ["Alpha", "Beta"]

    # 历史版本可能已经含脏数据：readiness 必须返回堵门项，而不是抛 DiagramError/500。
    dirty = json.loads(json.dumps(canvas, ensure_ascii=False))
    dirty["objects"][1]["name"] = "Alpha"
    report = R.evaluate(dirty)
    assert report["ready"] is False
    assert any(
        "重复稳定 name" in item
        for gate in report["gates"]
        for item in gate["blockingItems"]
    )


def test_get_canvas_elements_returns_full_canonical_state_and_version(db):
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="state", canvas=_rich_canvas(), canvas_version=7)
    db.add(session)
    db.commit()
    runner = ExplorationToolRunner(db, session)
    order = session.canvas["objects"][0]

    result = runner.run("get_canvas_elements", {
        "kind": "object", "ids": [order["id"]],
    })
    assert result["canvasVersion"] == 7
    assert result["truncated"] is False
    assert result["elements"] == [order]
    assert result["page"]["returned"] == result["page"]["total"] == 1
    assert isinstance(result["readiness"]["ready"], bool)

    # 旧版本写入被拒绝，画布和版本都不变。
    conflict = runner.run("upsert_elements", {
        "kind": "object",
        "expected_canvas_version": 6,
        "elements": [{"id": order["id"], "description": "不应写入"}],
    })
    assert conflict["conflict"] is True
    assert conflict["canvasVersion"] == 7
    assert session.canvas["objects"][0]["description"] == "交易订单"

    # 正确版本写入后，工具结果立即带回新版本、readiness 和完整 canonical 元素。
    written = runner.run("upsert_elements", {
        "kind": "object",
        "expected_canvas_version": 7,
        "elements": [{"id": order["id"], "description": "已安全更新"}],
    })
    assert written["canvasVersion"] == 8
    assert written["elements"][0]["description"] == "已安全更新"
    assert "readiness" in written and "gates" in written["readiness"]


def test_canvas_commit_uses_atomic_compare_and_swap(db):
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="cas", canvas=C.empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    db.refresh(session)
    runner = ExplorationToolRunner(db, session)

    assert runner._version_conflict({"expected_canvas_version": 0}) is None
    candidate, applied, errors = C.upsert_elements(
        session.canvas, "object", [{"name": "StaleWriter"}])
    assert applied and not errors

    OtherSession = sessionmaker(bind=db.get_bind())
    other = OtherSession()
    try:
        current = other.query(ExplorationSession).filter_by(id=session.id).one()
        external, external_applied, external_errors = C.upsert_elements(
            current.canvas, "object", [{"name": "ConcurrentWinner"}])
        assert external_applied and not external_errors
        current.canvas = external
        current.canvas_version = 1
        other.commit()
    finally:
        other.close()

    conflict = runner._commit_canvas(candidate)
    assert conflict is not None and conflict["conflict"] is True
    assert conflict["canvasVersion"] == 1
    assert [item["name"] for item in session.canvas["objects"]] == ["ConcurrentWinner"]


def test_in_turn_stale_version_rebases_without_conflict(db):
    """LLM 批量携带同一旧版本：自身写入造成的推进对齐继续，不烧工具预算。"""
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="rebase", canvas=C.empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    db.refresh(session)
    runner = ExplorationToolRunner(db, session)

    first = runner.run("upsert_elements", {
        "kind": "actor", "expected_canvas_version": 0,
        "elements": [{"name": "Employee", "displayName": "员工", "kind": "role"}],
    })
    assert "error" not in first and first["canvasVersion"] == 1

    # 模型在同一条消息里并行发出的第二个写调用仍带 expected v0 —— 服务端
    # 对齐到本回合已写出的 v1，继续落库而不是报画布版本冲突。
    second = runner.run("upsert_elements", {
        "kind": "actor", "expected_canvas_version": 0,
        "elements": [{"name": "Admin", "displayName": "管理员", "kind": "role"}],
    })
    assert "error" not in second and second["canvasVersion"] == 2
    assert {a["name"] for a in session.canvas["actors"]} == {"Employee", "Admin"}
    assert session.context_stats.get("canvasRebases") == 1


def test_external_concurrent_write_still_conflicts_after_rebase(db):
    """回合外部的真实并发写入不被 rebase 吞掉：CAS 硬冲突保持不变。"""
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="ext", canvas=C.empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    db.refresh(session)
    runner = ExplorationToolRunner(db, session)
    own = runner.run("upsert_elements", {
        "kind": "actor", "expected_canvas_version": 0,
        "elements": [{"name": "Employee", "kind": "role"}],
    })
    assert own["canvasVersion"] == 1
    # 生产时序对齐：回合内写入随后即提交（_persist/_prepare_history 都会 commit），
    # 释放写锁后再模拟另一请求的外部写入。
    db.commit()

    OtherSession = sessionmaker(bind=db.get_bind())
    other = OtherSession()
    try:
        current = other.query(ExplorationSession).filter_by(id=session.id).one()
        external, applied, errors = C.upsert_elements(
            current.canvas, "actor", [{"name": "Concurrent", "kind": "role"}])
        assert applied and not errors
        current.canvas = external
        current.canvas_version = 2
        other.commit()
    finally:
        other.close()

    # 旧版本（甚至本回合已知的 v1）写入都必须硬冲突 —— v2 不是本 runner 写出的
    conflict = runner.run("upsert_elements", {
        "kind": "actor", "expected_canvas_version": 0,
        "elements": [{"name": "Late", "kind": "role"}],
    })
    assert conflict.get("conflict") is True
    assert conflict["canvasVersion"] == 2
    late_conflict = runner.run("upsert_elements", {
        "kind": "actor", "expected_canvas_version": 1,
        "elements": [{"name": "Late2", "kind": "role"}],
    })
    assert late_conflict.get("conflict") is True


def test_uploaded_text_remains_user_owned_after_authorized_agent_edit(
        db, tmp_path, monkeypatch):
    """一次获授权编辑不能把用户文件永久降级为后续回合可任意修改的 agent 文件。"""
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="files", canvas=C.empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    row = W.create_text(
        db, session, "contract.md", "原始条款", source="upload")

    authorized = ExplorationToolRunner(
        db, session, user_message="请编辑文件 contract.md，将条款更新为已确认版本")
    edited = authorized.run("manage_workspace_file", {
        "action": "update",
        "file_id": row.id,
        "content": "已确认条款",
        "expected_version": 1,
    })
    assert edited["updated"] is True and edited["version"] == 2
    db.refresh(row)
    assert row.source == "upload"
    assert W.read_text(row) == "已确认条款"

    unauthorized = ExplorationToolRunner(
        db, session, user_message="只总结 contract.md，不要修改或删除任何文件")
    blocked_update = unauthorized.run("manage_workspace_file", {
        "action": "update",
        "file_id": row.id,
        "content": "不应写入",
        "expected_version": 2,
    })
    assert blocked_update["confirmationRequired"] is True
    blocked_delete = unauthorized.run("manage_workspace_file", {
        "action": "delete",
        "file_id": row.id,
    })
    assert blocked_delete["confirmationRequired"] is True
    db.refresh(row)
    assert row.source == "upload"
    assert row.version == 2
    assert W.read_text(row) == "已确认条款"


def test_user_save_promotes_agent_draft_to_protected_user_evidence(
        db, tmp_path, monkeypatch):
    """UI 中的明确用户保存可以接管 AI 草稿，但接管后不可被下一回合任意覆盖。"""
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="files", canvas=C.empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    row = W.create_text(
        db, session, "proposal.md", "AI 初稿", source="agent")

    W.update_text(
        db, row, "用户手工确认后的版本", expected_version=1, source="user")
    db.refresh(row)
    assert row.source == "user" and row.version == 2

    next_turn = ExplorationToolRunner(
        db, session, user_message="请总结 proposal.md，不要修改或删除文件")
    blocked_update = next_turn.run("manage_workspace_file", {
        "action": "update",
        "file_id": row.id,
        "content": "不应写入",
        "expected_version": 2,
    })
    assert blocked_update["confirmationRequired"] is True
    blocked_delete = next_turn.run("manage_workspace_file", {
        "action": "delete",
        "file_id": row.id,
    })
    assert blocked_delete["confirmationRequired"] is True
    db.refresh(row)
    assert row.source == "user"
    assert W.read_text(row) == "用户手工确认后的版本"


def test_large_nested_canonical_state_is_losslessly_pageable():
    canvas = C.empty_canvas()
    canvas, _, errors = C.upsert_elements(canvas, "object", [{
        "name": "WideRecord",
        "attributes": [
            {"name": f"field_{index}", "typeHint": "文本"}
            for index in range(235)
        ],
    }])
    assert not errors
    element_id = canvas["objects"][0]["id"]

    seen: list[str] = []
    offset = 0
    while True:
        page = C.canvas_elements_page(
            canvas, "object", ids=[element_id],
            nested_field="attributes", nested_offset=offset, nested_limit=50)
        seen.extend(item["name"] for item in page["elements"][0]["attributes"])
        nested = page["nestedPages"][0]
        if not nested["hasMore"]:
            break
        offset = nested["nextOffset"]

    assert seen == [f"field_{index}" for index in range(235)]
    assert len(set(seen)) == 235


def test_system_prompt_includes_full_authoritative_snapshot_and_canvas_version():
    session = ExplorationSession(
        id="s", title="state", canvas=_rich_canvas(), canvas_version=12)
    prompt = OR._system_prompt(session, skills={})
    assert "canvasVersion=12" in prompt
    assert '"complete": true' in prompt
    assert '"type_hint": "金额"' in prompt
    assert '"cardinality": "many-to-one"' in prompt
    assert '"enum": [' in prompt and '"web"' in prompt and '"store"' in prompt
    assert "get_canvas_elements" in prompt


def test_oversized_inline_snapshot_degrades_to_bounded_valid_index():
    canvas = C.empty_canvas()
    canvas, _, errors = C.upsert_elements(canvas, "object", [
        {"name": f"Object_{index}", "description": "说明" * 2_000}
        for index in range(80)
    ])
    assert not errors
    encoded = C.canonical_snapshot_json(canvas, max_chars=1_000)
    decoded = json.loads(encoded)
    assert decoded["complete"] is False
    assert decoded["counts"]["objects"] == 80
    assert len(encoded) <= 1_000


def test_bounded_tool_result_is_always_valid_json_with_version_and_truncation():
    result = {
        "kind": "object",
        "canvasVersion": 41,
        "elements": [{"id": "el-1", "description": "很长" * 10_000}],
        "readiness": {"ready": False, "stage": "阶段1", "blockingCount": 2},
    }
    encoded = OR._serialize_tool_result(result, cap=400)
    decoded = json.loads(encoded)
    assert decoded["transportTruncated"] is True
    assert decoded["canvasVersion"] == 41
    assert decoded["originalChars"] > 400
    assert len(encoded) <= 400


def test_next_llm_step_sees_latest_version_readiness_and_canonical_element(db, monkeypatch):
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="state", canvas=C.empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    monkeypatch.setattr(OR, "select_llm_model_config", lambda db, model_id=None: object())
    monkeypatch.setattr(OR, "llm_call_kwargs", lambda cfg: {"model": "fake"})
    calls = {"count": 0}

    def fake_chat(call_kwargs, messages, tools):
        calls["count"] += 1
        if calls["count"] == 1:
            assert any(tool["name"] == "get_canvas_elements" for tool in tools)
            return {
                "content": None,
                "usage": None,
                "tool_calls": [{
                    "id": "write-1",
                    "name": "upsert_elements",
                    "arguments": {
                        "kind": "object",
                        "expected_canvas_version": 0,
                        "elements": [{
                            "name": "Order",
                            "attributes": [{"name": "order_no", "typeHint": "文本"}],
                            "keyAttribute": "order_no",
                        }],
                    },
                }],
            }
        tool_message = messages[-1]
        assert tool_message["role"] == "tool"
        payload = json.loads(tool_message["content"])
        assert payload["canvasVersion"] == 1
        assert payload["readiness"]["blockingCount"] >= 1
        assert payload["elements"][0]["attributes"][0]["name"] == "order_no"
        return {"content": "已记录。", "tool_calls": [], "usage": None}

    monkeypatch.setattr(OR.llm_bridge, "chat", fake_chat)
    events = list(OR.run_exploration_turn(
        db, session.id, user=object(), message="建立订单对象"))
    assert calls["count"] == 2
    assert any(event["type"] == "canvas" and event["version"] == 1 for event in events)
    assert next(event for event in events if event["type"] == "answer")["content"] == "已记录。"


def test_turn_streams_text_deltas_and_plan_events(db, monkeypatch):
    """流式回合契约：text_delta 实时外发；todo_write 成功后推送 plan 事件。"""
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="stream", canvas=C.empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    monkeypatch.setattr(OR, "select_llm_model_config", lambda db, model_id=None: object())
    monkeypatch.setattr(OR, "llm_call_kwargs", lambda cfg: {"model": "fake"})
    calls = {"n": 0}

    def fake_chat(_ck, _messages, _tools):
        return {"content": "兜底。", "tool_calls": [], "usage": None}

    def fake_chat_stream(_ck, _messages, _tools):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"delta": "先定计划，"}
            yield {"delta": "再写画布。"}
            yield {"final": {
                "content": "先定计划，再写画布。",
                "tool_calls": [{
                    "id": "t1", "name": "todo_write",
                    "arguments": {"items": [
                        {"content": "建对象", "status": "in_progress"},
                        {"content": "出图", "status": "pending"},
                    ]},
                }],
                "usage": None}}
        else:
            yield {"delta": "完成"}
            yield {"final": {"content": "完成", "tool_calls": [], "usage": None}}

    monkeypatch.setattr(OR.llm_bridge, "chat", fake_chat)
    monkeypatch.setattr(OR.llm_bridge, "chat_stream", fake_chat_stream)

    events = list(OR.run_exploration_turn(
        db, session.id, user=object(), message="建模"))
    deltas = [e["delta"] for e in events if e.get("type") == "text_delta"]
    assert "".join(deltas) == "先定计划，再写画布。完成"
    plan = next(e for e in events if e.get("type") == "plan")
    assert plan["items"] == [
        {"content": "建对象", "status": "in_progress"},
        {"content": "出图", "status": "pending"},
    ]
    assert next(e for e in events if e["type"] == "answer")["content"] == "完成"
    # 持久化消息含 todo_write 步骤（历史回放据此重建计划）
    row = db.query(ExplorationSession).filter_by(id=session.id).one()
    assert row.context_stats.get("recentMessages") is not None


def test_stream_unsupported_falls_back_to_single_chat(db, monkeypatch):
    """provider 不支持流式（无 api_base）→ unsupported 信号 → 一次性 chat 兜底。"""
    session = ExplorationSession(
        id=str(uuid.uuid4()), title="fallback", canvas=C.empty_canvas(), canvas_version=0)
    db.add(session)
    db.commit()
    monkeypatch.setattr(OR, "select_llm_model_config", lambda db, model_id=None: object())
    monkeypatch.setattr(OR, "llm_call_kwargs", lambda cfg: {"model": "fake"})

    def fake_chat(_ck, _messages, _tools):
        return {"content": "非流式结果。", "tool_calls": [], "usage": None}

    def fake_chat_stream(_ck, _messages, _tools):
        yield {"unsupported_stream": True}
        raise AssertionError("unsupported 后不应继续消费流")

    monkeypatch.setattr(OR.llm_bridge, "chat", fake_chat)
    monkeypatch.setattr(OR.llm_bridge, "chat_stream", fake_chat_stream)

    events = list(OR.run_exploration_turn(
        db, session.id, user=object(), message="你好"))
    assert not any(e.get("type") == "text_delta" for e in events)
    assert next(e for e in events if e["type"] == "answer")["content"] == "非流式结果。"


def test_system_prompt_encodes_autonomous_agent_contract():
    """E2 自主建模代理契约与回合预算：禁求确认、批量检查点、PLAN 工具、24 步。"""
    assert OR._MAX_STEPS == 24
    session = ExplorationSession(
        id="prompt-contract", title="t", canvas=C.empty_canvas(), canvas_version=0)
    prompt = OR._system_prompt(session)
    assert "自主建模代理" in prompt
    assert "不要提出「是否同意继续" in prompt
    assert "一次性批量" in prompt
    assert "todo_write" in prompt
    assert "generate_document" in prompt and "generate_draft" in prompt
    # 反虚构纪律与定量铁律保留
    assert "严禁声称已完成" in prompt
    assert "定量铁律" in prompt
