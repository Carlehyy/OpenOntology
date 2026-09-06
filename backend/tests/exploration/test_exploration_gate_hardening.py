"""业务探索质量门的 false-green 对抗回归测试。"""
from __future__ import annotations

import copy

import pytest

from app.exploration import canvas as C
from app.exploration import diagram as D
from app.exploration import questions as Q
from app.exploration import readiness as R


def _ready_canvas() -> dict:
    canvas = C.empty_canvas()
    canvas, _, errors = C.upsert_elements(canvas, "object", [{
        "name": "Order",
        "displayName": "订单",
        "keyAttribute": "order_no",
        "attributes": [
            {"name": "order_no", "displayName": "订单号", "typeHint": "文本", "required": True},
            {"name": "amount", "displayName": "金额", "typeHint": "金额"},
        ],
    }])
    assert not errors
    canvas, _, errors = C.upsert_elements(canvas, "actor", [{
        "name": "Approver", "displayName": "审批人", "kind": "role",
    }])
    assert not errors
    canvas, _, errors = C.upsert_elements(canvas, "behavior", [{
        "name": "approve_order",
        "displayName": "审批订单",
        "actor": "Approver",
        "object": "Order",
        "trigger": "订单提交审批",
        "outcome": "记录审批结论",
    }])
    assert not errors
    canvas, _, errors = C.upsert_elements(canvas, "scenario", [{
        "name": "approval_flow",
        "displayName": "订单审批流程",
        "goal": "完成订单审批",
        "actors": ["Approver"],
        "steps": ["审批人审批订单并记录结论"],
        "objects": ["Order"],
        "behaviors": ["approve_order"],
        "expectedOutcome": "订单获得明确审批结论",
    }])
    assert not errors
    assert R.evaluate(canvas)["ready"] is True
    return canvas


def _coverage_items(canvas: dict) -> list[str]:
    report = R.evaluate(canvas)
    return next(g for g in report["gates"] if g["id"] == "coverage")["blockingItems"]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("goal", "goal"),
        ("actors", "actors"),
        ("steps", "steps"),
        ("objects", "objects"),
        ("behaviors", "behaviors"),
        ("expected_outcome", "expected_outcome"),
    ],
)
def test_scenario_shell_cannot_pass_readiness(field: str, expected: str):
    canvas = _ready_canvas()
    canvas["scenarios"][0][field] = [] if field in {"actors", "steps", "objects", "behaviors"} else ""

    report = R.evaluate(canvas)
    assert report["ready"] is False
    assert any(expected in item for item in _coverage_items(canvas))
    with pytest.raises(D.DiagramError, match="质量校验未通过"):
        D.flow_mermaid(canvas, "approval_flow")


def test_scenario_references_and_behavior_participants_must_be_coherent():
    canvas = _ready_canvas()
    canvas, _, errors = C.upsert_elements(canvas, "actor", [{
        "name": "Observer", "displayName": "观察员", "kind": "role",
    }])
    assert not errors
    canvas["scenarios"][0]["actors"] = ["Observer"]
    items = _coverage_items(canvas)
    assert any("执行主体未列入场景 actors" in item for item in items)

    canvas = _ready_canvas()
    canvas["scenarios"][0]["objects"] = ["Ghost"]
    items = _coverage_items(canvas)
    assert any("引用未定义对象" in item for item in items)
    assert R.evaluate(canvas)["ready"] is False


def test_conditional_and_declared_branch_sources_need_two_explicit_valid_branches():
    canvas = _ready_canvas()
    scenario = canvas["scenarios"][0]
    scenario["steps"] = ["审批是否通过？", "订单生效", "订单驳回"]
    assert any("至少需要 2 条显式 branches" in item for item in _coverage_items(canvas))

    scenario["branches"] = [
        {"from_step": 1, "to_step": 2, "condition": "通过"},
    ]
    assert any("当前 1 条" in item for item in _coverage_items(canvas))

    scenario["branches"].append(
        {"from_step": 1, "to_step": 99, "condition": "驳回"},
    )
    assert any("不在步骤范围" in item for item in _coverage_items(canvas))

    scenario["branches"] = [
        {"from_step": 1, "to_step": 2, "condition": "通过"},
        {"from_step": 1, "to_step": 3, "condition": "驳回"},
    ]
    assert R.evaluate(canvas)["ready"] is True

    # 即便步骤文本没写“如果/是否”，主动声明了分支的源节点也必须是完整决策。
    scenario["steps"][0] = "审批决策"
    scenario["branches"] = [{"from_step": 1, "to_step": 2, "condition": "通过"}]
    assert any("至少需要 2 条显式 branches" in item for item in _coverage_items(canvas))


def test_blocking_dismissed_remains_a_liability_and_visible_in_summary():
    canvas = _ready_canvas()
    canvas, ids, errors = Q.raise_questions(canvas, [{
        "question": "大额订单的金额门槛是多少？",
        "kind": "blocking",
        "target": "Order.amount",
    }])
    assert not errors
    canvas, done, errors = Q.resolve_questions(canvas, [{
        "id": ids[0],
        "status": "dismissed",
        "resolution": "当前暂时不知道，后续再确认",
    }])
    assert done and not errors
    assert not Q.open_questions(canvas, Q.KIND_BLOCKING)  # 兼容旧 open API 语义
    assert len(Q.blocking_liabilities(canvas)) == 1

    report = R.evaluate(canvas)
    assert report["ready"] is False
    assert report["openQuestions"]["dismissedBlocking"] == 1
    question_gate = next(g for g in report["gates"] if g["id"] == "questions")
    assert any("搁置但尚未解决" in item for item in question_gate["blockingItems"])
    assert "已搁置，仍未解决" in Q.ledger_summary(canvas)


def test_quantification_rejects_placeholder_and_unrelated_numbers():
    assert Q.is_quantified("大额指 ≥ 50000 元")
    assert Q.is_quantified("尽快指 24 小时内完成")
    assert not Q.is_quantified("金额超过阈值时审批")
    assert not Q.is_quantified("大额订单由 2 人审批")
    assert not Q.is_quantified("通常金额 ≥ 50000 元时审批")

    canvas = _ready_canvas()
    canvas, ids, errors = Q.raise_questions(canvas, [{
        "question": "大额订单的金额门槛是多少？",
        "kind": "blocking",
        "target": "Order.amount",
        "options": ["≥50000元", "≥100000元"],
    }])
    assert not errors
    _, done, errors = Q.resolve_questions(canvas, [{
        "id": ids[0],
        "resolution": "2 人审批",
    }])
    assert not done
    assert any("货币单位" in error for error in errors)

    # 只在问题确实询问金额口径时启用单位约束；普通业务问题不会因出现“金额”误杀。
    ordinary = _ready_canvas()
    ordinary, ids, _ = Q.raise_questions(ordinary, [{
        "question": "订单金额由谁复核？",
        "kind": "blocking",
    }])
    _, done, errors = Q.resolve_questions(ordinary, [{
        "id": ids[0],
        "resolution": "财务总监",
    }])
    assert done and not errors


def test_resolved_target_field_must_match_canvas_and_threshold_must_be_materialized():
    canvas = _ready_canvas()
    canvas, ids, errors = Q.raise_questions(canvas, [{
        "question": "订单业务主键是什么？",
        "kind": "blocking",
        "target": "Order.key_attribute",
    }])
    assert not errors
    canvas, done, errors = Q.resolve_questions(canvas, [{
        "id": ids[0],
        "resolution": "customer_no",
    }])
    assert done and not errors
    issues, _ = Q.resolved_question_issues(canvas)
    assert any("尚未写入" in issue and "key_attribute" in issue for issue in issues)
    assert R.evaluate(canvas)["ready"] is False

    matching = copy.deepcopy(canvas)
    matching["questions"][0]["resolution"] = "订单号"
    assert not any(Q.resolved_question_issues(matching))
    assert R.evaluate(matching)["ready"] is True

    threshold = _ready_canvas()
    threshold, ids, _ = Q.raise_questions(threshold, [{
        "question": "大额订单的金额门槛是多少？",
        "kind": "blocking",
        "target": "Order.amount",
    }])
    threshold, done, errors = Q.resolve_questions(threshold, [{
        "id": ids[0],
        "resolution": "≥50000元",
    }])
    assert done and not errors
    threshold_issues, _ = Q.resolved_question_issues(threshold)
    assert any("尚未写入" in issue for issue in threshold_issues)
    assert R.evaluate(threshold)["ready"] is False

    threshold, _, errors = C.upsert_elements(threshold, "rule", [{
        "name": "large_order_approval",
        "displayName": "大额订单审批",
        "kind": "approval",
        "appliesTo": "approve_order",
        "statement": "订单金额 ≥ 50000 元时必须审批",
    }])
    assert not errors
    assert not any(Q.resolved_question_issues(threshold))
    assert R.evaluate(threshold)["ready"] is True

def test_resolved_question_with_broken_target_path_blocks_readiness():
    canvas = _ready_canvas()
    canvas, ids, _ = Q.raise_questions(canvas, [{
        "question": "审批额度是多少？",
        "kind": "blocking",
        "target": "Order.missing_field",
    }])
    canvas, done, errors = Q.resolve_questions(canvas, [{
        "id": ids[0],
        "resolution": "50000元",
    }])
    assert done and not errors
    issues, _ = Q.resolved_question_issues(canvas)
    assert any("无法解析到画布字段" in issue for issue in issues)
    assert R.evaluate(canvas)["ready"] is False


def test_resolved_question_with_vanished_target_root_no_longer_blocks():
    """销账 target 根元素已被删除/合并 → 降级 advisory，存量卡死会话零迁移自愈。

    生产事故回归：「已销账问题…target 根元素『探索起点』不存在」永久堵住
    questions 质量门，agent 无法自愈。
    """
    canvas = _ready_canvas()
    canvas, ids, _ = Q.raise_questions(canvas, [{
        "question": "探索起点选哪个场景？",
        "kind": "blocking",
        "target": "探索起点",
        "options": ["A. 全流程", "B. 单场景"],
    }])
    canvas, done, errors = Q.resolve_questions(canvas, [{
        "id": ids[0],
        "resolution": "A. 全流程",
    }])
    assert done and not errors

    issues, advisories = Q.resolved_question_issues(canvas)
    assert not issues
    assert any("target 根元素" in item and "不再堵门" in item for item in advisories)
    # readiness 不再被该存量死锁挡住
    gate = next(g for g in R.evaluate(canvas)["gates"] if g["id"] == "questions")
    assert not any("探索起点" in item for item in gate["blockingItems"])


def _question_canvas(options=None, suggestion=None) -> tuple[dict, str]:
    canvas = _ready_canvas()
    canvas, ids, errors = Q.raise_questions(canvas, [{
        "question": "大额订单审批的金额门槛是多少？",
        "kind": "blocking",
        "target": "Order.amount",
        "options": options or [],
        "suggestion": suggestion,
    }])
    assert not errors
    return canvas, ids[0]


def test_delegated_resolution_expands_option_and_default():
    """「按选项B/第1个/按默认」展开为候选字面值后落账；定量铁律不放松。"""
    options = ["A. ≥50000元", "B. ≥100000元（默认）"]
    for phrase, expected in [
        ("按选项B", "B. ≥100000元（默认）"),
        ("选B", "B. ≥100000元（默认）"),
        ("第1个", "A. ≥50000元"),
        ("按默认来", "B. ≥100000元（默认）"),
    ]:
        canvas, qid = _question_canvas(options=options)
        canvas, done, errors = Q.resolve_questions(canvas, [
            {"id": qid, "resolution": phrase}])
        assert not errors, (phrase, errors)
        assert done and done[0]["resolution"] == expected
        # 展开后的字面值必须通过全部复核（定量 + 金额单位 + 证据匹配另行落库）
        assert Q.is_quantified(done[0]["resolution"])


def test_delegated_resolution_refuses_abdication_and_missing_default():
    """开放式放权与无法定位的「默认」不能销账 —— 拍板权不放松。"""
    canvas, qid = _question_canvas(options=["A. ≥50000元", "B. ≥100000元"])
    canvas, done, errors = Q.resolve_questions(canvas, [
        {"id": qid, "resolution": "都可以"}])
    assert not done and any("开放式放权" in e for e in errors)

    # 候选没有标注「默认」也没有定量 suggestion → 按默认销账被拒
    canvas2, qid2 = _question_canvas(options=["A. ≥50000元", "B. ≥100000元"])
    canvas2, done2, errors2 = Q.resolve_questions(canvas2, [
        {"id": qid2, "resolution": "按默认来"}])
    assert not done2 and any("无法按默认销账" in e for e in errors2)
