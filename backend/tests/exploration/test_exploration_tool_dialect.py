"""工具参数方言归一化回归测试 — LLM 的 XML 风格参数方言在工具边界被还原。

病灶（实测 MiniMax-M3）：嵌套数组被包成 {"item": [...]}、数组里混入
{"$text": "..."} 伪节点、布尔写成 "true"/"false" 字符串，导致 canvas 校验
以「必须是数组」整批拒收元素。归一化只发生在 toolkit 工具边界的白名单写
工具上；canvas.upsert_elements 保持严格，不做方言兼容。LLM 一律 fake（本
文件直接构造方言参数，不发任何模型调用）。
"""
from __future__ import annotations

import uuid

from app.exploration import canvas as C
from app.exploration.models import ExplorationSession
from app.exploration.toolkit import ExplorationToolRunner, _normalize_dialect_args


def _tool_session(db, *, canvas=None):
    row = ExplorationSession(
        id=str(uuid.uuid4()), title="dialect",
        canvas=canvas if canvas is not None else C.empty_canvas(),
        canvas_version=1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_item_wrapped_nested_arrays_land_on_canvas(db):
    """{"item": [...]} 包裹的元素/属性/枚举/关系归一化后成功落画布，属性真的在。"""
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row)
    result = runner.run("upsert_elements", {
        "kind": "object",
        "elements": {"item": [  # elements 数组本身被包裹
            {
                "name": "store", "displayName": "门店",
                "description": "业务最小经营单元",
                "attributes": {"item": [
                    {"name": "store_code", "displayName": "门店编码",
                     "typeHint": "文本", "required": "true"},  # 字符串布尔
                    {"name": "status", "displayName": "营业状态", "typeHint": "枚举",
                     "enum": {"item": ["营业中", "闭店"]}},  # 枚举数组被包裹
                ]},
                "relations": {"item": [
                    {"target": "region", "displayName": "所属区域",
                     "cardinality": "many-to-one"},
                ]},
            },
        ]},
    })
    assert result.get("errors") is None
    assert result["applied"] == 1
    obj = row.canvas["objects"][0]
    attrs = {item["name"]: item for item in obj["attributes"]}
    assert set(attrs) == {"store_code", "status"}
    assert attrs["store_code"]["required"] is True      # "true" → True
    assert attrs["status"]["enum"] == ["营业中", "闭店"]
    assert [rel["target"] for rel in obj["relations"]] == ["region"]


def test_text_pseudo_nodes_dropped_and_string_booleans_normalized(db):
    """elements/嵌套数组里的 {"$text": ...} 伪节点被剔除，其余元素正常落画布。"""
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row)
    result = runner.run("upsert_elements", {
        "kind": "behavior",
        "elements": [
            {"$text": "行为一：审批订单"},  # 伪节点混入 elements 数组
            {
                "name": "approve_order", "displayName": "审批订单",
                "actor": "Approver", "object": "Order",
                "needs_approval": "false",  # 字符串布尔 → False
                "inputs": [
                    {"name": "comment", "typeHint": "文本"},
                    {"$text": "审批意见"},  # 伪节点混入嵌套数组
                ],
            },
            {"$text": "行为二"},
        ],
    })
    assert result.get("errors") is None
    assert result["applied"] == 1
    behaviors = row.canvas["behaviors"]
    assert [item["name"] for item in behaviors] == ["approve_order"]
    behavior = behaviors[0]
    assert behavior["needs_approval"] is False
    assert [item["name"] for item in behavior["inputs"]] == ["comment"]

    # 子项删除标记的字符串布尔："true" → True 后 _delete 生效
    obj_canvas, applied, errors = C.upsert_elements(
        C.empty_canvas(), "object", [{"name": "store", "attributes": [
            {"name": "store_code", "typeHint": "文本"}]}])
    assert applied and not errors
    row.canvas = obj_canvas
    db.commit()
    attr_id = row.canvas["objects"][0]["attributes"][0]["id"]
    deleted = runner.run("upsert_elements", {
        "kind": "object",
        "elements": [{"name": "store",
                      "attributes": [{"id": attr_id, "_delete": "true"}]}],
    })
    assert deleted.get("errors") is None
    assert row.canvas["objects"][0]["attributes"] == []


def test_canonical_payload_is_identity_under_normalization():
    """规范 JSON 经过归一化必须恒等（含单键 item 非数组、自由文本里的 true）。"""
    payload = {
        "kind": "object",
        "expected_canvas_version": 7,
        "elements": [
            {
                "id": "el-12345678", "name": "store", "displayName": "门店",
                "description": "true",  # 自由文本恰好是 "true"：非布尔字段不动
                "key_attribute": "store_code",
                "attributes": [
                    {"id": "sub-abcdef0123", "name": "store_code",
                     "type_hint": "文本", "required": True,
                     "enum": ["营业中", "闭店"], "notes": None},
                ],
                "relations": [
                    {"target": "region", "name": "region",
                     "cardinality": "many-to-one", "description": None},
                ],
                "item": "非方言的多键 dict 原样保留",
            },
        ],
    }
    assert _normalize_dialect_args(payload) == payload
    # 单键 item 但值不是数组：不是方言包装，原样保留
    assert _normalize_dialect_args({"item": "not-a-list"}) == {"item": "not-a-list"}
    # 标量位置的 {"$text": ...} 伪节点取其文本值
    assert _normalize_dialect_args(
        {"statement": {"$text": "金额 ≥ 50000 元需审批"}}
    ) == {"statement": "金额 ≥ 50000 元需审批"}


def test_whitelisted_sibling_tools_args_are_normalized(db):
    """todo_write / raise_questions / remove_elements 同属白名单，同样解包。"""
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row)

    planned = runner.run("todo_write", {
        "items": {"item": [{"content": "梳理对象模型", "status": "in_progress"}]},
    })
    assert planned.get("error") is None
    assert planned["total"] == 1

    raised = runner.run("raise_questions", {
        "questions": {"item": [{
            "question": "门店营业状态的枚举口径是什么？",
            "kind": "blocking", "target": "store.status",
            "options": {"item": ["营业中/闭店", "营业中/休整/闭店"]},
        }]},
    })
    assert raised.get("errors") is None
    assert raised["raised"] == 1

    canvas, applied, errors = C.upsert_elements(
        C.empty_canvas(), "object", [{"name": "store"}])
    assert applied and not errors
    row.canvas = canvas
    db.commit()
    removed = runner.run("remove_elements", {
        "kind": "object", "ids": {"item": ["store"]}})
    assert removed["removed"] == 1
    assert row.canvas["objects"] == []


def test_workspace_file_content_is_not_touched(db):
    """白名单外工具参数不受影响：manage_workspace_file content 原样落盘回读。"""
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row)
    content = ('字面文本：{"item": [1, 2, 3]} 与 {"$text": "保留"} 都不是方言；'
               'required 取值 "true" 同样原样保留')
    created = runner.run("manage_workspace_file", {
        "action": "create", "path": "notes.md", "content": content})
    assert created.get("created") is True
    read = runner.run("manage_workspace_file", {
        "action": "read", "file_id": created["id"]})
    assert read["content"] == content


def test_non_list_nested_field_error_carries_minimal_example():
    """拒收错误附正确形状最小示例；canvas 层不做方言兼容（仍拒收）。"""
    # 更新路径：已有元素收到非数组嵌套字段 → 「必须是数组」+ 示例
    canvas, applied, errors = C.upsert_elements(
        C.empty_canvas(), "object", [{"name": "store"}])
    assert applied and not errors
    obj_id = canvas["objects"][0]["id"]
    _, applied, errors = C.upsert_elements(canvas, "object", [
        {"id": obj_id, "attributes": {"item": [{"name": "store_code"}]}}])
    assert not applied
    assert errors and "必须是数组" in errors[0]
    assert "正确形状示例" in errors[0]
    assert 'attributes: [{"name"' in errors[0]

    # 新建路径：pydantic list_type 错误同样附示例
    _, applied, errors = C.upsert_elements(C.empty_canvas(), "object", [
        {"name": "store", "attributes": {"item": [{"name": "store_code"}]}}])
    assert not applied
    assert errors and "正确形状示例" in errors[0]
    assert 'attributes: [{"name"' in errors[0]

    # 子项内数组字段（enum 被包裹）也附示例
    _, applied, errors = C.upsert_elements(C.empty_canvas(), "object", [
        {"name": "store", "attributes": [
            {"name": "status", "enum": {"item": ["营业中", "闭店"]}}]}])
    assert not applied
    assert errors and "正确形状示例" in errors[0]
    assert 'enum: ["值A","值B"]' in errors[0]


def test_value_wrapped_options_registered(db):
    """{"value": ...} 包装的澄清问题选项解包后成功登记（实测 MiniMax-M3 的选项方言）。"""
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row)
    result = runner.run("raise_questions", {
        "questions": [{
            "question": "盘点执行主体:复用店长还是新增盘点员?",
            "kind": "advisory",
            "target": "manual_stocktake_task",
            "options": [{"value": "复用店长"}, {"value": "新增盘点员 stocktaker"}],
            "suggestion": "复用店长",
        }],
    })
    assert result.get("errors") is None
    assert result["raised"] == 1
    questions = row.canvas["questions"]
    assert questions[0]["options"] == ["复用店长", "新增盘点员 stocktaker"]


def test_apply_draft_selected_keys_dialect_unwrapped(db, monkeypatch):
    """apply_draft 同属方言白名单：selected_keys 的 {"item": [...]} 包裹、
    {"$text": ...} 伪节点、{"value": ...} 包装在工具边界还原为字符串数组。"""
    from app.exploration import application_service
    from app.exploration.models import ExplorationDraft

    row = _tool_session(db)
    row.ontology_id = str(uuid.uuid4())
    db.commit()
    draft = ExplorationDraft(session_id=row.id, document_id="doc-1",
                             target_ontology_id=row.ontology_id,
                             draft={}, report={}, status="draft")
    db.add(draft)
    db.commit()
    captured: dict = {}

    def fake_apply(draft_id, body, db_, user):
        captured["selected_keys"] = body.selected_keys
        return {"data": {"created": {}, "skipped": [], "warnings": [],
                         "ontologyId": row.ontology_id}}

    monkeypatch.setattr(application_service, "apply_draft", fake_apply)
    runner = ExplorationToolRunner(
        db, row, user=object(), user_message="确认沉淀")
    result = runner.run("apply_draft", {
        "draft_id": draft.id,
        "selected_keys": {"item": [
            "obj:order",
            {"$text": "伪节点剔除"},
            {"value": "obj:customer"},
        ]},
    })
    assert result.get("error") is None, result
    assert captured["selected_keys"] == ["obj:order", "obj:customer"]


def test_propose_mapping_field_mapping_value_wrappers_unwrapped(db, monkeypatch):
    """propose_mapping 同属方言白名单：field_mapping 的 {"value": ...} 包装值
    在工具边界还原；服务层收到的是纯净 {数据集列名: 本体属性名}。"""
    from app.ontologies.mappings import suggestion_service

    row = _tool_session(db)
    row.ontology_id = str(uuid.uuid4())
    row.ontology_version_id = str(uuid.uuid4())
    db.commit()
    captured: dict = {}

    def fake_propose(db_, ontology_id, version_id, *, dataset_id, object_ref,
                     field_mapping, primary_key_column, note):
        captured["field_mapping"] = field_mapping
        return {"datasetName": "客户表", "objectName": "Customer",
                "fieldCount": len(field_mapping), "reused": False}

    monkeypatch.setattr(
        suggestion_service, "propose_agent_mapping", fake_propose)
    runner = ExplorationToolRunner(db, row, user=object())
    result = runner.run("propose_mapping", {
        "dataset_id": "ds-1",
        "target_object": "Customer",
        "field_mapping": {
            "cust_id": {"value": "customer_id"},
            "cust_name": "customer_name",
        },
    })
    assert result.get("error") is None, result
    assert captured["field_mapping"] == {
        "cust_id": "customer_id", "cust_name": "customer_name"}
