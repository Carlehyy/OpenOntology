"""画布写入的 LLM 序列化形态矫正测试（商业化审查 D-010 回归）。

生产事故背景：MiniMax-M3 等模型把工具参数中的嵌套数组序列化为
{"item": …} 包装对象 / 索引对象 / 单对象 / XML 串，Pydantic 严格 list
校验全拒且模型无法自纠，画布写入死循环。矫正层的契约：

  1. 可识别的坏形态全部矫正为 list，元素正常落库；
  2. 矫正出的空表 ≠ 模型显式 [] 清空（防误清空既有子表）；
  3. 无法识别的非空形态保持响亮失败，错误信息指给模型；
  4. 网关 {"_raw": …} 回退形态在 toolkit 层抢救（围栏剥离 + 配平提取）。
"""
from __future__ import annotations

import uuid

from app.exploration import canvas as C
from app.exploration.models import ExplorationSession


def _object_with_two_attrs() -> dict:
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        "displayName": "客户",
        "attributes": [
            {"name": "code", "displayName": "客户编码", "typeHint": "文本"},
            {"name": "level", "displayName": "客户等级", "typeHint": "枚举"},
        ],
    }])
    assert not errors
    return cv


def test_attributes_item_wrap_dict_is_coerced():
    cv = C.empty_canvas()
    cv, applied, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        "attributes": {"item": {"name": "code", "typeHint": "文本"}},
    }])
    assert not errors and applied
    assert [a["name"] for a in cv["objects"][0]["attributes"]] == ["code"]


def test_attributes_items_wrap_list_is_coerced():
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        "attributes": {"items": [
            {"name": "code", "typeHint": "文本"},
            {"name": "level", "typeHint": "枚举"},
        ]},
    }])
    assert not errors
    assert len(cv["objects"][0]["attributes"]) == 2


def test_attributes_index_dict_is_coerced_in_numeric_order():
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        "attributes": {
            "0": {"name": "code", "typeHint": "文本"},
            "2": {"name": "level", "typeHint": "枚举"},
            "1": {"name": "name", "typeHint": "文本"},
        },
    }])
    assert not errors
    assert [a["name"] for a in cv["objects"][0]["attributes"]] == [
        "code", "name", "level"]


def test_attributes_single_dict_is_wrapped_into_list():
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer", "attributes": {"name": "code", "typeHint": "文本"},
    }])
    assert not errors
    assert len(cv["objects"][0]["attributes"]) == 1


def test_attributes_json_string_is_coerced():
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        "attributes": '[{"name": "code", "typeHint": "文本"}, '
                      '{"name": "level", "typeHint": "枚举"}]',
    }])
    assert not errors
    assert len(cv["objects"][0]["attributes"]) == 2


def test_attributes_xml_item_string_is_coerced():
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        "attributes": (
            "<item><name>code</name><typeHint>文本</typeHint></item>"
            "<item><name>level</name><typeHint>枚举</typeHint>"
            "<enum>V1</enum><enum>V2</enum></item>"
        ),
    }])
    assert not errors
    attributes = cv["objects"][0]["attributes"]
    assert [a["name"] for a in attributes] == ["code", "level"]
    # 同名兄弟标签（enum）在 XML 解析时收进列表
    assert attributes[1]["enum"] == ["V1", "V2"]


def test_attributes_xml_container_string_is_coerced():
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        "attributes": (
            "<attributes>"
            "<item><name>code</name><typeHint>文本</typeHint></item>"
            "<item><name>level</name><typeHint>枚举</typeHint></item>"
            "</attributes>"
        ),
    }])
    assert not errors
    assert len(cv["objects"][0]["attributes"]) == 2


def test_coerced_empty_wrap_does_not_clear_existing_children():
    cv = _object_with_two_attrs()
    # 空包装对象 / 空串 = 「未提供」，不得误读成显式 [] 清空
    cv, _, errors = C.upsert_elements(cv, "object", [
        {"name": "Customer", "attributes": {}}])
    assert not errors
    assert len(cv["objects"][0]["attributes"]) == 2
    cv, _, errors = C.upsert_elements(cv, "object", [
        {"name": "Customer", "attributes": ""}])
    assert not errors
    assert len(cv["objects"][0]["attributes"]) == 2
    # 模型显式 [] 仍然是清空整表（原语义不变）
    cv, _, errors = C.upsert_elements(cv, "object", [
        {"name": "Customer", "attributes": []}])
    assert not errors
    assert cv["objects"][0]["attributes"] == []


def test_coerced_patch_merges_children_incrementally():
    cv = _object_with_two_attrs()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        # 坏形态矫正后按自然键增量合并：既有 2 项 + 新增 1 项
        "attributes": {"item": [{"name": "status", "typeHint": "是否"}]},
    }])
    assert not errors
    assert [a["name"] for a in cv["objects"][0]["attributes"]] == [
        "code", "level", "status"]


def test_unrecognizable_form_fails_loudly_with_guidance():
    cv = C.empty_canvas()
    cv, applied, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer", "attributes": 42}])
    assert not applied and len(errors) == 1
    assert "valid list" in errors[0] or "数组" in errors[0]
    # elements 本身无法解析 → 明确指引错误
    cv, applied, errors = C.upsert_elements(cv, "object", 42)
    assert not applied and len(errors) == 1
    assert "elements 必须是元素对象的 JSON 数组" in errors[0]


def test_elements_itself_accepts_dict_and_xml_forms():
    cv = C.empty_canvas()
    cv, applied, errors = C.upsert_elements(cv, "object",
                                            {"name": "Customer"})
    assert not errors and len(applied) == 1
    cv, applied, errors = C.upsert_elements(cv, "object",
                                            "<item><name>VIP</name></item>")
    assert not errors and len(applied) == 1
    assert cv["objects"][-1]["name"] == "VIP"


def test_nested_child_list_fields_are_coerced_one_level_deeper():
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Customer",
        # attribute 子项的 enum（list[str]）以索引对象形态到达
        "attributes": [{"name": "level", "typeHint": "枚举",
                        "enum": {"0": "V1", "1": "V2"}}],
    }])
    assert not errors
    assert cv["objects"][0]["attributes"][0]["enum"] == ["V1", "V2"]

    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "process", [{
        "name": "onboarding",
        "steps": [
            {"seq": 1, "name": "提交资料",
             # ProcessStep.inputs（list[str]）以包装对象形态到达
             "inputs": {"item": ["身份证", "营业执照"]}},
        ],
        "metrics": [
            {"name": "时长", "formula": "≤ 2 天",
             # MetricSpec.source_objects（list[str]）以索引对象形态到达
             "source_objects": {"0": "Customer"}},
        ],
    }])
    assert not errors, errors
    assert cv["processes"][0]["steps"][0]["inputs"] == ["身份证", "营业执照"]
    assert cv["processes"][0]["metrics"][0]["source_objects"] == ["Customer"]


def test_actor_responsibilities_index_dict_is_coerced():
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "actor", [{
        "name": "Admin", "responsibilities": {"0": "审核", "1": "封禁"},
    }])
    assert not errors
    assert cv["actors"][0]["responsibilities"] == ["审核", "封禁"]


# ---------------------------------------------------------------------------
# toolkit 层：网关 _raw 回退的抢救
# ---------------------------------------------------------------------------

def _tool_session(db):
    row = ExplorationSession(
        id=str(uuid.uuid4()), title="coercion-tools",
        canvas=C.empty_canvas(), canvas_version=1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_raw_args_salvaged_through_runner(db, admin_user):
    from app.exploration.toolkit import ExplorationToolRunner
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row, user=admin_user)
    result = runner.run("upsert_elements", {"_raw": (
        '```json\n{"kind": "object", "elements": [{"name": "Customer",'
        ' "attributes": {"item": {"name": "code"}}}]}\n```')} )
    assert result["applied"] == 1, result
    names = [a["name"] for a in row.canvas["objects"][0]["attributes"]]
    assert names == ["code"]


def test_raw_args_unsalvageable_keeps_error_path(db, admin_user):
    from app.exploration.toolkit import ExplorationToolRunner
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row, user=admin_user)
    result = runner.run("upsert_elements", {"_raw": "完全不是 JSON 也没有花括号"})
    assert result["applied"] == 0
    assert row.canvas["objects"] == []


def test_coercion_does_not_mutate_caller_arguments():
    # orchestrator 把同一段 arguments dict 同时用于工具执行、step 审计持久化
    # 与下一轮对话历史；矫正必须发生在拷贝上，不能改写模型原始发送内容
    element = {"name": "Customer", "attributes": {"item": [{"name": "code", "typeHint": "文本"}]}}
    elements = [element]
    cv = C.empty_canvas()
    cv, applied, errors = C.upsert_elements(cv, "object", elements)
    assert not errors and applied
    assert element == {"name": "Customer",
                       "attributes": {"item": [{"name": "code", "typeHint": "文本"}]}}


def test_runner_keeps_raw_tool_arguments_intact(db, admin_user):
    import copy as copy_module

    from app.exploration.toolkit import ExplorationToolRunner
    row = _tool_session(db)
    runner = ExplorationToolRunner(db, row, user=admin_user)
    args = {"kind": "object",
            "elements": [{"name": "Customer",
                          "attributes": {"item": {"name": "code"}}}]}
    snapshot = copy_module.deepcopy(args)
    result = runner.run("upsert_elements", args)
    assert result["applied"] == 1, result
    assert args == snapshot
