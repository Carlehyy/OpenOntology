"""业务流程图示（flow/sequence 的流程 target）测试：

  1. 流程 target 出 flow 图：结构化步骤文本用 step.name、显式 actor/behavior 标注、
     异常分支通向结束；sequence 按 step.seq 抽取非空 behavior
  2. 共享图校验对流程生效：不可达 / 不可结束 / 条件步骤分支数 / 分支越界，
     且与场景共用同一份口径文案
  3. 场景与流程跨类同名 → 歧义报错要求用 id 消歧；id 精确指定可出图
  4. 挂接流程且 steps 为空的场景作为 target → 拒图文案指向流程图
  5. 场景原有出图与错误文案不变（回归）
"""
from __future__ import annotations

import pytest

from app.exploration import canvas as C
from app.exploration import diagram as D


def _process_canvas() -> dict:
    """对象/主体/行为齐备 + 一个可出图的流程（步骤故意乱序提交，验证按 seq 排序）。"""
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "Order", "displayName": "订单", "keyAttribute": "order_no",
        "attributes": [{"name": "order_no", "typeHint": "文本"}],
    }])
    assert not errors
    cv, _, errors = C.upsert_elements(cv, "actor", [
        {"name": "Sales", "displayName": "销售", "kind": "role"},
        {"name": "Buyer", "displayName": "采购员", "kind": "role"},
    ])
    assert not errors
    cv, _, errors = C.upsert_elements(cv, "behavior", [
        {"name": "confirm_order", "displayName": "确认订单", "actor": "Sales",
         "object": "Order", "outcome": "订单进入履约"},
        {"name": "ship_order", "displayName": "订单发货", "actor": "Sales",
         "object": "Order", "outcome": "订单发出"},
    ])
    assert not errors
    cv, _, errors = C.upsert_elements(cv, "process", [{
        "name": "order_fulfillment", "displayName": "订单履约流程",
        "goal": "完成订单履约",
        "steps": [
            {"seq": 3, "name": "订单发货", "actor": "Sales", "behavior": "ship_order"},
            {"seq": 1, "name": "确认订单", "actor": "Sales", "behavior": "confirm_order"},
            {"seq": 2, "name": "如果库存不足则采购", "actor": "Buyer"},
        ],
        "branches": [
            {"fromStep": 2, "toStep": 3, "condition": "库存充足"},
            {"fromStep": 2, "toStep": None, "condition": "缺货取消", "kind": "exception"},
        ],
        "objects": ["Order"],
        "metrics": [{"name": "履约时长", "formula": "≤ 48 小时", "sourceObjects": ["Order"]}],
        "expectedOutcome": "订单交付客户",
    }])
    assert not errors
    return cv


def test_process_flow_diagram_renders_structured_steps():
    cv = _process_canvas()
    flow = D.build_diagram(cv, "flow", "订单履约流程")
    mermaid = flow["mermaid"]
    assert mermaid.startswith("flowchart")
    assert flow["target"] == "订单履约流程"
    # 步骤文本 = step.name（按 seq 排序）；显式 actor 标注，不用文本启发式
    assert 'S1["确认订单｜Sales"]' in mermaid
    assert 'S2{"如果库存不足则采购｜Buyer"}' in mermaid
    assert 'S3["订单发货｜Sales"]' in mermaid
    assert 'S0 --> S1' in mermaid
    assert 'S2 -->|"库存充足"| S3' in mermaid
    assert 'S2 -->|"缺货取消"| SE' in mermaid       # 异常分支通向结束
    assert "订单交付客户" in mermaid
    assert flow["warnings"] == []
    assert flow["layout"]["density"] == 3

    # target=None 且无场景 → 取第一个流程；按 id 精确定位同效
    assert D.build_diagram(cv, "flow")["mermaid"] == mermaid
    process = cv["processes"][0]
    assert D.build_diagram(cv, "flow", process["id"])["mermaid"] == mermaid


def test_process_sequence_diagram_orders_behaviors_by_step_seq():
    cv = _process_canvas()
    seq = D.build_diagram(cv, "sequence", "order_fulfillment")
    mermaid = seq["mermaid"]
    assert mermaid.startswith("sequenceDiagram")
    # 步骤 2 未绑定 behavior → 跳过；顺序按 seq：确认订单(seq1) 在 订单发货(seq3) 前
    confirm_at = mermaid.index("确认订单")
    ship_at = mermaid.index("订单发货")
    assert -1 < confirm_at < ship_at
    assert "Sales" in mermaid and "Order" in mermaid

    # 全部步骤都未绑定 behavior → 明确报错而不是空图
    cv["processes"][0]["steps"] = [
        {**step, "behavior": None} for step in cv["processes"][0]["steps"]
    ]
    with pytest.raises(D.DiagramError, match="还没有绑定行为"):
        D.build_diagram(cv, "sequence", "order_fulfillment")


def test_process_flow_shared_graph_validation():
    cv = _process_canvas()

    # 条件步骤自环 → 第 3 步从起点不可达（与场景同一份文案口径）
    broken = _process_canvas()
    broken["processes"][0]["branches"] = [
        {"from_step": 2, "to_step": 2, "condition": "继续等货"},
        {"from_step": 2, "to_step": 2, "condition": "再次催单"},
    ]
    with pytest.raises(D.DiagramError, match="流程图质量校验未通过：以下步骤从流程起点不可达：3"):
        D.build_diagram(broken, "flow", "order_fulfillment")
    assert any("从流程起点不可达" in issue
               for issue in D.process_model_analysis(broken, "order_fulfillment")["issues"])

    # 最后一步是条件步骤且分支全部回环 → 没有通往结束节点的路径（步骤 2 去条件化，隔离变量；
    # 注意本工厂 steps 乱序存储：raw[0]=seq3、raw[1]=seq1、raw[2]=seq2）
    broken = _process_canvas()
    broken["processes"][0]["steps"][2]["name"] = "采购补货"
    broken["processes"][0]["steps"][0]["name"] = "是否确认发货？"
    broken["processes"][0]["branches"] = [
        {"from_step": 3, "to_step": 2, "condition": "再核一遍"},
        {"from_step": 3, "to_step": 2, "condition": "等客户回复"},
    ]
    with pytest.raises(D.DiagramError, match="没有通往结束节点的路径"):
        D.build_diagram(broken, "flow", "order_fulfillment")

    # 条件步骤只剩 1 条显式分支 → 与场景同一拦截文案
    broken = _process_canvas()
    broken["processes"][0]["branches"] = [
        {"from_step": 2, "to_step": 3, "condition": "库存充足"},
    ]
    with pytest.raises(D.DiagramError, match="至少需要 2 条显式 branches，当前 1 条"):
        D.build_diagram(broken, "flow", "order_fulfillment")

    # 分支条件重复 / 目标越界
    broken = _process_canvas()
    broken["processes"][0]["branches"] = [
        {"from_step": 2, "to_step": 3, "condition": "库存充足"},
        {"from_step": 2, "to_step": None, "condition": "库存充足"},
    ]
    with pytest.raises(D.DiagramError, match="存在重复分支条件"):
        D.build_diagram(broken, "flow", "order_fulfillment")

    broken = _process_canvas()
    broken["processes"][0]["branches"][0]["to_step"] = 99
    with pytest.raises(D.DiagramError, match="不在步骤范围"):
        D.build_diagram(broken, "flow", "order_fulfillment")

    # 步骤主体/行为引用不可解析 → 出图前拦截
    broken = _process_canvas()
    broken["processes"][0]["steps"][0]["actor"] = "Ghost"
    broken["processes"][0]["steps"][0]["behavior"] = "ghost_behavior"
    with pytest.raises(D.DiagramError, match="步骤引用未定义主体"):
        D.build_diagram(broken, "flow", "order_fulfillment")
    with pytest.raises(D.DiagramError, match="步骤引用未定义行为"):
        D.build_diagram(broken, "sequence", "order_fulfillment")

    # 基线画布自身可过分析（对照组，防误伤）
    assert D.process_model_analysis(cv, "order_fulfillment")["issues"] == []


def test_same_name_ambiguity_requires_id_disambiguation():
    cv = _process_canvas()
    cv, _, errors = C.upsert_elements(cv, "scenario", [{
        "name": "order_fulfillment_case", "displayName": "订单履约流程",
        "goal": "重名场景", "actors": ["Sales"], "steps": ["销售确认订单"],
        "objects": ["Order"], "behaviors": ["confirm_order"],
        "expectedOutcome": "完成",
    }])
    assert not errors
    # 跨类同名 → 歧义报错要求 id；name/display_name 命中两集合都算
    with pytest.raises(D.DiagramError, match="匹配到多个场景/流程候选，请使用画布元素 id 精确指定"):
        D.build_diagram(cv, "flow", "订单履约流程")
    process = cv["processes"][0]
    assert D.build_diagram(cv, "flow", process["id"])["mermaid"].startswith("flowchart")
    with pytest.raises(D.DiagramError, match="场景或流程「不存在的目标」不存在"):
        D.build_diagram(cv, "flow", "不存在的目标")


def test_linked_scenario_without_steps_rejects_diagram():
    cv = _process_canvas()
    cv, _, errors = C.upsert_elements(cv, "scenario", [{
        "name": "vip_flow", "displayName": "VIP 履约", "goal": "大客户履约变体",
        "processRef": "order_fulfillment",
    }])
    assert not errors
    with pytest.raises(D.DiagramError, match="走所属流程主路径，请直接查看流程图"):
        D.build_diagram(cv, "flow", "VIP 履约")
    with pytest.raises(D.DiagramError, match="走所属流程主路径，请直接查看流程图"):
        D.build_diagram(cv, "sequence", "VIP 履约")


def test_scenario_diagrams_and_error_messages_unchanged():
    cv = _process_canvas()
    cv, _, errors = C.upsert_elements(cv, "scenario", [{
        "name": "pay_flow", "displayName": "支付流程", "goal": "完成订单支付",
        "actors": ["Sales"],
        "steps": ["销售确认回单", "如果金额 ≥ 50000 元则走审批", "订单变为已支付"],
        "objects": ["Order"], "behaviors": ["confirm_order"],
        "branches": [
            {"fromStep": 2, "toStep": 3, "condition": "金额 ≥ 50000 元，审批通过"},
            {"fromStep": 2, "toStep": 3, "condition": "金额 < 50000 元"},
        ],
        "expectedOutcome": "订单进入已支付状态",
    }])
    assert not errors

    flow = D.build_diagram(cv, "flow", "支付流程")
    assert flow["mermaid"].startswith("flowchart")
    assert 'S2{' in flow["mermaid"] and "金额 ≥ 50000 元，审批通过" in flow["mermaid"]
    seq = D.build_diagram(cv, "sequence", "支付流程")
    assert seq["mermaid"].startswith("sequenceDiagram")

    # 场景错误文案逐字保持（测试精确匹配的禁区）
    cv["scenarios"][0]["branches"] = []
    with pytest.raises(D.DiagramError, match="至少需要 2 条显式 branches"):
        D.build_diagram(cv, "flow", "支付流程")
    with pytest.raises(D.DiagramError, match="流程图质量校验未通过"):
        D.flow_mermaid(cv, "支付流程")
    cv["scenarios"][0]["behaviors"] = ["missing_behavior"]
    with pytest.raises(D.DiagramError, match="未定义行为"):
        D.build_diagram(cv, "sequence", "支付流程")


# ---------------------------------------------------------------- 状态迁移解析


def _vuln_alert_canvas() -> dict:
    """生产实况画布：漏洞告警单生命周期 + 真实事故中的 outcome 文本形态。"""
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "vuln_alert", "displayName": "漏洞告警单",
        "keyAttribute": "alert_id",
        "attributes": [
            {"name": "alert_id", "displayName": "告警单号", "typeHint": "文本",
             "required": True},
            {"name": "status", "displayName": "状态", "typeHint": "枚举",
             "required": True,
             "enum": ["已通报", "已确认", "规避中", "修复中", "已闭环",
                      "已超期未修复"]},
        ],
    }])
    assert not errors
    cv, _, _ = C.upsert_elements(cv, "behavior", [
        # 事故 1：目标状态后紧跟括号补充说明 —— 旧解析把「已闭环」（验证…）
        # 整段吞成目的端，边丢失 → 「已闭环」被误判孤立、lifecycles 门永久堵死
        {"name": "verify_repair", "displayName": "验证漏洞修复结果",
         "actor": "vuln_repair_owner", "object": "vuln_alert",
         "outcome": ("漏洞告警单状态从「修复中」变为「已闭环」"
                     "（验证结果为「通过（已闭环）」）；"
                     "若验证不通过则触发返工重新走修复流程")},
        # 事故 2：叙事式「从无到有」把后续整句吞成伪源状态（40 字符垃圾）
        {"name": "improve", "displayName": "立项改进",
         "actor": "vuln_repair_owner", "object": "vuln_alert",
         "outcome": ("改进措施落地情况状态从无到有，初始创建后标记为「已立项」；"
                     "当开始实施时状态从「已立项」变为「修复中」")},
        {"name": "confirm", "displayName": "分析确认告警",
         "actor": "vuln_repair_owner", "object": "vuln_alert",
         "outcome": "漏洞告警单状态从「已通报」变为「已确认」"},
    ])
    return cv


def test_state_transition_survives_parenthetical_suffix():
    """「从X变为Y（补充说明）」必须解析出真实迁移，不得误报孤立状态。"""
    analysis = D.state_model_analysis(_vuln_alert_canvas(), "vuln_alert")
    edges = {(src, dst) for src, dst, _ in analysis["edges"]}
    assert ("修复中", "已闭环") in edges
    assert ("已通报", "已确认") in edges
    # 终态「已闭环」已由真实迁入边接通，不再是孤立状态
    assert "已闭环" not in set(analysis["isolated"])


def test_state_transition_narrative_not_swallowed_as_state():
    """「从无到有，…」叙事不得吞并后续句子成为伪状态；真实未知引用仍要报。"""
    analysis = D.state_model_analysis(_vuln_alert_canvas(), "vuln_alert")
    issues_text = "；".join(analysis["issues"])
    # 「已立项」不在枚举内 —— 这是真实的未知引用，必须继续暴露给模型修复
    assert "已立项" in issues_text
    # 旧解析吞出的 40 字符垃圾状态不允许再出现
    assert "无到有" not in issues_text
    assert "初始创建后标记" not in issues_text


def test_state_diagram_error_teaches_canonical_phrasing():
    """校验失败的报错要教会模型规范写法，让对话期修复回路可收敛。"""
    cv = C.empty_canvas()
    cv, _, errors = C.upsert_elements(cv, "object", [{
        "name": "doc", "displayName": "单据",
        "attributes": [{"name": "status", "displayName": "状态", "typeHint": "枚举",
                        "enum": ["草稿", "生效"]}],
    }])
    assert not errors
    cv, _, _ = C.upsert_elements(cv, "behavior", [{
        "name": "approve", "actor": "clerk", "object": "doc",
        "outcome": "单据被审批通过",
    }])
    with pytest.raises(D.DiagramError, match="规范写法") as exc_info:
        D.build_diagram(cv, "state", "doc")
    assert "单独成句" in str(exc_info.value)
    assert "分号" in str(exc_info.value)
