"""
受限工具集 (Bounded Toolkit) — agent 与本体交互的全部动词

每个工具都以 AgentScope 为唯一世界观：越界引用 → ToolError（返回给 LLM 自我
修正）。工具永远不接触数据集 / SQL / Neo4j —— 只读 fo_* 投影表与事实流，
"写"只能以 propose_action(dry_run) 的形式提出，真实执行在 router 层由用户
确认后经 action_engine 完成（requires_approval 的动作仍会进 HITL 审批队列）。

工具清单：
  search_objects     按类型 + 属性过滤 + 关键词检索实例
  get_object         单实例详情（含各链接类型的邻居概览）
  traverse_links     沿链接类型遍历邻居
  find_paths         两个具体实例之间的最短关系路径
  analyze_change_impact 字段拟议变更的关系可达范围（只读预演）
  aggregate_objects  分组计数 / 求和 / 均值 / 最值
  get_object_history 实例的事实流（谁、何时、为何改的 — 溯源问答）
  trace_causal_chain 跨对象因果链追溯（决策 → 动作 → 事实 → 后续覆盖）
  explain_sentinel_firing 哨兵触发解释（条件求值 + 命中证据 + 状态语义）
  list_actions       授权范围内的动作及参数说明
  run_decision_simulation 隔离快照上的多视角决策推演（只写推演运行记录）
  list_world_model_services 当前本体可用的世界模型推演服务（按语义注册过滤 + 漂移/前置条件检查）
  run_world_model_simulation 调用世界模型推演服务（确定性脚本执行，调用经 world_model 调用记录审计）
  propose_action     动作预演（引擎 dry-run：校验 + 模拟效果，不落实际变更）
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.ontology_formal import (
    ObjectInstance, LinkInstance, PropertyFact,
)
from app.ontologies.agent_runtime.boundary import AgentScope, ToolError
from app.ontologies.agent_runtime.limits import limit as runtime_limit
from app.ontologies.formal_modeling.facts import fact_order_clause
from app.ontologies.versions.models import OntologyVersion
from app.shared.time_utils import utc_iso

# 限额统一注册表（默认值/边界/环境变量见 limits.py）


def _history_release_lineage(
        db: Session, ontology_id: str, current_release_id: str) -> dict:
    """Resolve the release authority segment for Fact-history reads.

    A normal trial promotion is a new provenance baseline, so only its own
    release id is authoritative.  A rollback activation does not rebuild Fact
    history; it inherits the direct parent chain through rollback activations
    until (and including) the nearest normal promotion baseline.

    The traversal is deliberately parent-only.  Missing/cyclic/non-release
    parents make the lineage incomplete; a rollback then fails closed to the
    current release instead of treating a partial ancestor set as authority.
    """
    rows = db.query(
        OntologyVersion.id,
        OntologyVersion.parent_version_id,
        OntologyVersion.promoted_from_id,
        OntologyVersion.node_kind,
        OntologyVersion.lifecycle_status,
    ).filter(
        OntologyVersion.ontology_id == ontology_id,
    ).all()
    releases = {
        str(row.id): row
        for row in rows
        if (
            (row.node_kind or "release") == "release"
            and (row.lifecycle_status or "released") == "released"
        )
    }
    current_id = str(current_release_id)
    current = releases.get(current_id)
    if current is None:
        return {
            "current": None,
            "authorityReleaseIds": [current_id],
            "baselineReleaseId": None,
            "lineageComplete": False,
            "preBaselineReleaseIds": [],
        }

    # A release with promoted_from_id is an ordinary promotion and therefore
    # defines its own authority reset.  Its ancestors remain readable only for
    # the explicitly non-authoritative preBaselineOrigin explanation.
    ordinary = bool(current.promoted_from_id)
    if ordinary:
        pre_baseline: list[str] = []
        seen = {current_id}
        cursor = str(current.parent_version_id or "")
        complete = True
        while cursor:
            if cursor in seen:
                complete = False
                break
            seen.add(cursor)
            row = releases.get(cursor)
            if row is None:
                complete = False
                break
            pre_baseline.append(cursor)
            cursor = str(row.parent_version_id or "")
        return {
            "current": current,
            "authorityReleaseIds": [current_id],
            "baselineReleaseId": current_id,
            "lineageComplete": complete,
            "preBaselineReleaseIds": pre_baseline if complete else [],
        }

    authority = [current_id]
    seen = {current_id}
    cursor = str(current.parent_version_id or "")
    baseline_id: str | None = None
    complete = True
    while cursor:
        if cursor in seen:
            complete = False
            break
        seen.add(cursor)
        row = releases.get(cursor)
        if row is None:
            complete = False
            break
        authority.append(cursor)
        if row.promoted_from_id:
            baseline_id = cursor
            break
        cursor = str(row.parent_version_id or "")

    if not complete:
        # Partial ancestry is evidence, not authority.  Keep only facts whose
        # immutable release id is the current activation.
        authority = [current_id]
        baseline_id = None
    return {
        "current": current,
        "authorityReleaseIds": authority,
        "baselineReleaseId": baseline_id,
        "lineageComplete": complete,
        "preBaselineReleaseIds": [],
    }


# ---------------------------------------------------------------- 工具定义

TOOL_DEFS: list[dict] = [
    {
        "name": "search_objects",
        "description": "在指定对象类型下检索实例。支持属性条件过滤（filters）与全文关键词（keyword）。回答任何关于'有哪些/多少/是什么'的问题前都应先用本工具查证，不要凭空作答。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string", "description": "对象类型的 name 或显示名"},
                "filters": {
                    "type": "array",
                    "description": "属性过滤条件，AND 关系",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property": {"type": "string", "description": "属性 name"},
                            "op": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "contains", "in"]},
                            "value": {"description": "比较值；op=in 时为数组"},
                        },
                        "required": ["property", "op", "value"],
                    },
                },
                "keyword": {"type": "string", "description": "在所有属性值中做包含匹配的关键词"},
                "limit": {"type": "integer", "description": "返回条数上限（受授权配额约束）"},
            },
            "required": ["object_type"],
        },
    },
    {
        "name": "get_object",
        "description": "查看单个对象实例的全部属性、派生属性，以及它在各链接类型上的邻居概览。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "对象实例 id"},
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "traverse_links",
        "description": "从一个对象实例出发，沿指定链接类型遍历，返回相邻对象。用于回答'X 关联了哪些 Y'类问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "起点对象实例 id"},
                "link_type": {"type": "string", "description": "链接类型的 name 或显示名"},
                "direction": {"type": "string", "enum": ["out", "in"], "description": "out=作为源对象沿链接向外, in=作为目标对象反向"},
                "limit": {"type": "integer"},
            },
            "required": ["instance_id", "link_type"],
        },
    },
    {
        "name": "traverse_path",
        "description": "从一个起点对象出发，沿指定的链接类型序列做多跳遍历，返回终点对象。用于回答'X 经由 A 再经由 B 关联到哪些 Z'这类多跳 / 影响分析 / 血缘追溯问题。单跳用 traverse_links 即可；本工具用于 2 跳及以上。已内置扇出上限与节点预算，超限会截断并标记。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "起点对象实例 id"},
                "path": {
                    "type": "array",
                    "description": f"有序的跳序列，每一跳指定一个链接类型和方向；最多 {runtime_limit("max_hops")} 跳",
                    "items": {
                        "type": "object",
                        "properties": {
                            "link_type": {"type": "string", "description": "链接类型的 name 或显示名"},
                            "direction": {"type": "string", "enum": ["out", "in"], "description": "out=沿链接向外, in=反向；默认 out"},
                        },
                        "required": ["link_type"],
                    },
                },
                "limit": {"type": "integer", "description": "返回终点对象的条数上限（受授权配额约束）"},
            },
            "required": ["instance_id", "path"],
        },
    },
    {
        "name": "find_paths",
        "description": "查找两个具体对象实例之间的最短关系路径，并返回可供图谱高亮的节点与边。用户明确问‘A 到 B 有哪些路径/如何关联’时使用；如果只有名称，先用 search_objects 找到两端 instance_id。",
        "parameters": {
            "type": "object",
            "properties": {
                "source_instance_id": {"type": "string", "description": "起点实例 id"},
                "target_instance_id": {"type": "string", "description": "终点实例 id"},
                "direction": {"type": "string", "enum": ["both", "outgoing", "incoming"], "description": "both=忽略遍历方向寻找连接，outgoing=只沿关系正向，incoming=只沿关系反向"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 6},
                "max_paths": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["source_instance_id", "target_instance_id"],
        },
    },
    {
        "name": "analyze_change_impact",
        "description": "只读预演某实例字段的拟议变化，沿真实关系计算直接/间接可达范围，并返回可供图谱分层标亮的节点与边。它不修改数据；结果只代表关联范围而非确定因果，回答时必须保留该边界。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "拟议变更的实例 id"},
                "property": {"type": "string", "description": "拟议变更的属性 name 或显示名"},
                "proposed_value": {"description": "拟议的新值，可为字符串、数字、布尔或空值"},
                "direction": {"type": "string", "enum": ["both", "outgoing", "incoming"]},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            "required": ["instance_id", "property", "proposed_value"],
        },
    },
    {
        "name": "aggregate_objects",
        "description": "对某对象类型的实例做聚合统计：count/sum/avg/min/max，可按属性分组。回答'总共/平均/最多'类问题用这个，不要逐条列举。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "metric": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
                "metric_property": {"type": "string", "description": "metric 非 count 时必填：参与计算的数值属性 name"},
                "group_by": {"type": "string", "description": "可选：按此属性分组"},
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "contains", "in"]},
                            "value": {},
                        },
                        "required": ["property", "op", "value"],
                    },
                },
            },
            "required": ["object_type", "metric"],
        },
    },
    {
        "name": "get_object_history",
        "description": "查看对象实例的事实流（属性级变更历史）：每次变更的值、来源（人工/采集/某动作）、操作者、因果与置信度。回答'为什么是这个值/谁改的/什么时候变的'用这个。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string"},
                "property_name": {"type": "string", "description": "可选：只看某个属性"},
                "limit": {"type": "integer"},
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "trace_causal_chain",
        "description": "沿事实流追溯实例属性变化的因果链：哪次决策/哪个动作导致了变更、此前值是什么、之后又被谁覆盖、派生值由哪些输入算出。返回带出处的节点与因果边（决策→动作→事实→后续覆盖）；事实节点自带 causedBy/supersedesId/derivedFrom 指针，决策事实（factKind=decision）会拆出 decision/reason 字段。回答'为什么变了/谁批准的/这个值从哪来/后来变成了什么'这类跨对象因果问题用这个；单实例的单点历史用 get_object_history。链受深度与数量上限约束，返回 truncated=true 时说明这是部分链。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "要追溯的实例 id"},
                "property_name": {"type": "string", "description": "可选：只追溯某个属性"},
                "direction": {"type": "string", "enum": ["upstream", "downstream", "both"], "description": "upstream=什么导致了这些变化；downstream=这些变化后来导致了什么；默认 both"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "list_actions",
        "description": "列出授权范围内可用的业务动作及其参数说明。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "explain_sentinel_firing",
        "description": "解释一条哨兵为什么触发（或为什么没触发）：给出条件表达式、每条命中元组的对象与属性值证据、条件与绑定过滤的求值结果、状态语义与动作结果。材料来自触发记录与命中时刻快照，是确定性还原而非猜测。用户问「这条哨兵为什么触发/为什么没触发/为什么执行了动作」时使用；只读，不产生任何提案。",
        "parameters": {
            "type": "object",
            "properties": {
                "sentinel_id": {"type": "string", "description": "哨兵的 id、name 或显示名"},
                "firing_id": {"type": "string", "description": "可选：指定某次触发记录；缺省解释该哨兵最近一次"},
                "match_limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "最多展开几个命中元组，默认 3"},
            },
            "required": ["sentinel_id"],
        },
    },
    {
        "name": "list_dynamic_sentinels",
        "description": "列出当前发布版本之上的助手动态哨兵。只返回 assistant_dynamic；发布版内置哨兵不在可管理集合中。用户询问已有动态哨兵或准备修改时先调用。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_dynamic_sentinel_change",
        "description": "生成动态哨兵创建/更新/启用/停用/删除提案。只生成提案，不直接写库；内置哨兵永远不可操作。创建或更新 definition 会接受对象/关系/动作的 id、name 或显示名并由服务端规范化、强校验。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["create", "update", "enable", "disable", "delete"]},
                "sentinel_id": {"type": "string", "description": "update/enable/disable/delete 必填，只能来自 list_dynamic_sentinels"},
                "definition": {
                    "type": "object",
                    "description": "create 必填；update 可只给要修改的字段，服务端与当前完整定义合并后校验",
                    "properties": {
                        "name": {"type": "string", "description": "稳定英文技术名，如 overdue_order_alert"},
                        "displayName": {"type": "string"},
                        "description": {"type": "string"},
                        "bindings": {"type": "array", "items": {"type": "object", "properties": {
                            "alias": {"type": "string"}, "objectType": {"type": "string"}, "filter": {"type": "string"}
                        }, "required": ["alias", "objectType"]}},
                        "links": {"type": "array", "items": {"type": "object", "properties": {
                            "from": {"type": "string"}, "linkType": {"type": "string"}, "to": {"type": "string"}
                        }, "required": ["from", "linkType", "to"]}},
                        "condition": {"type": "string"},
                        "primaryAlias": {"type": "string"},
                        "actions": {"type": "array", "items": {"type": "string"}},
                        "actionParameters": {"type": "object", "additionalProperties": True},
                        "onChange": {"type": "boolean"},
                        "onSchedule": {"type": "boolean"},
                        "scanIntervalSeconds": {"type": "integer"},
                        "triggerMode": {"type": "string", "enum": ["on_enter", "on_enter_leave", "run_on_all", "on_pattern"]},
                        "pattern": {
                            "type": "object",
                            "description": "triggerMode=on_pattern 时必填的 CEP 事件模式；stages 的 alias/objectTypeId 必须与 bindings 镜像一致，onChange/onSchedule 必须同时为 true，窗口不得小于 scanIntervalSeconds",
                            "properties": {
                                "stages": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "object", "properties": {
                                    "alias": {"type": "string"},
                                    "objectTypeId": {"type": "string"},
                                    "filter": {"type": "string", "description": "如 \"a.status == 'submitted'\"；在事件时刻求值，应引用被监听的变更属性"},
                                    "within": {"type": "integer", "description": "完成本 stage 允许的秒数窗口（60~604800），缺省用 pattern.within"}
                                }, "required": ["alias", "objectTypeId"]}},
                                "within": {"type": "integer", "description": "缺省窗口秒数，默认 3600"},
                                "absence": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"], "description": "缺失分支：stage 超时未完成时以 edge=absence 触发"},
                                "aggregate": {"type": "object", "description": "单 stage 窗口聚合（count/avg/sum/min/max），带滞回防抖", "properties": {
                                    "property": {"type": "string"},
                                    "function": {"type": "string", "enum": ["count", "avg", "sum", "min", "max"]},
                                    "window": {"type": "integer"},
                                    "threshold": {"type": "number"},
                                    "comparison": {"type": "string", "enum": ["gte", "lte", "gt", "lt"]}
                                }, "required": ["property", "function", "window", "threshold"]},
                                "condition": {"type": "string", "description": "模式级最终条件，可引用各 stage 别名；支持 changed_within('a.prop',秒)/prev('a.prop') 时间算子"}
                            },
                            "required": ["stages"]
                        },
                        "muted": {"type": "boolean"}
                    },
                    "additionalProperties": False
                }
            },
            "required": ["operation"]
        }
    },
    {
        "name": "run_decision_simulation",
        "description": "在当前发布版本和授权范围的冻结快照上运行独立决策推演。用户说‘推演未来/比较方案/辅助决策/如果…怎么办’时使用。引擎会让多个独立角色提出可能性，再由确定性评分比较方案；结果不是概率或因果证明，也不会执行动作或写回真实对象。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要辅助决定的完整问题，需包含目标或待比较的选择"},
                "alternatives": {
                    "type": "array",
                    "description": "可选，用户已明确的 2-6 个互斥方案；未明确时由场景编译器生成包含维持现状的候选方案",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 6,
                },
                "horizon": {"type": "string", "description": "可选，推演时间范围或决策窗口"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_world_model_services",
        "description": "列出当前本体可用的世界模型推演服务（已发布且语义注册到本本体的服务）。可用服务附带示例入参 exampleInput（context/actions/horizon 的期望形状）；因本体版本演进（依赖对象类型缺失）或数据不足（前置条件不满足）暂不可用的服务会给出原因。做未来态势/what-if 数据推演前先调用本工具。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_world_model_simulation",
        "description": "调用一个世界模型推演服务做确定性推演：context 按服务示例入参的形状、用查询工具收集的真实数据组装，actions 为干预动作，horizon 为推演步长。返回结构化结果与调用记录编号；结果中的数字必须原样引用，不得自行改算或外推。",
        "parameters": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string", "description": "服务 id（来自 list_world_model_services）"},
                "context": {"type": "object", "description": "推演入参，形状参照服务 exampleInput 的 context", "additionalProperties": True},
                "actions": {"type": "array", "description": "干预动作列表，形状参照 exampleInput 的 actions", "items": {}},
                "horizon": {"type": "integer", "description": "推演步长/时间窗口", "minimum": 0},
            },
            "required": ["service_id", "context"],
        },
    },
    {
        "name": "propose_action",
        "description": "对某动作做预演（dry-run）：跑完整校验并模拟全部效果，但不落任何实际变更。这是 agent 唯一的'写'入口——预演结果会作为提案展示给用户，由用户决定是否真实执行。需要修改数据时必须用本工具，禁止假装已经执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "动作的 name 或显示名"},
                "parameters": {"type": "object", "description": "动作参数", "additionalProperties": True},
                "target_instance_id": {"type": "string", "description": "动作作用的目标实例 id（动作绑定对象类型时必填）"},
            },
            "required": ["action"],
        },
    },
]


# ---------------------------------------------------------------- 过滤与格式化

def _num(v: Any) -> Optional[float]:
    try:
        if isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _match(props: dict, f: dict) -> bool:
    prop, op, want = f.get("property"), f.get("op", "eq"), f.get("value")
    got = (props or {}).get(prop)
    if op == "eq":
        return got == want or (str(got) == str(want) and got is not None)
    if op == "neq":
        return not (got == want or (str(got) == str(want) and got is not None))
    if op == "contains":
        return want is not None and got is not None and str(want).lower() in str(got).lower()
    if op == "in":
        options = want if isinstance(want, list) else [want]
        return got in options or str(got) in [str(x) for x in options]
    # 数值比较：双方都可转数值才比较，否则退化为字符串比较
    a, b = _num(got), _num(want)
    if a is None or b is None:
        if got is None or want is None:
            return False
        a, b = str(got), str(want)  # type: ignore[assignment]
    if op == "gt":
        return a > b
    if op == "gte":
        return a >= b
    if op == "lt":
        return a < b
    if op == "lte":
        return a <= b
    raise ToolError(f"不支持的过滤操作符: {op}")


def _trunc(v: Any) -> Any:
    if isinstance(v, str) and len(v) > runtime_limit("value_trunc"):
        return v[:runtime_limit("value_trunc")] + f"…(截断,共{len(v)}字符)"
    return v


def _row(inst: ObjectInstance) -> dict:
    return {
        "id": inst.id,
        "properties": {k: _trunc(v) for k, v in (inst.properties or {}).items()},
        "computed": {k: _trunc(v) for k, v in (inst.computed or {}).items()},
    }


def _label(scope: AgentScope, inst: ObjectInstance) -> str:
    """实例的人类可读标签：主键值 > name/title 属性 > id 前缀。"""
    ot = scope.object_types.get(inst.object_type_id)
    props = inst.properties or {}
    if ot and ot.primary_key:
        pk_prop = next((p for p in (ot.properties or []) if isinstance(p, dict)
                        and (p.get("id") == ot.primary_key or p.get("name") == ot.primary_key)), None)
        if pk_prop and props.get(pk_prop.get("name")) not in (None, ""):
            return str(props[pk_prop["name"]])
    for k in ("name", "title", "name_cn", "display_name"):
        if props.get(k) not in (None, ""):
            return str(props[k])
    # 兜底：第一个非空的标量属性值（比 id 前缀对人友好）
    for v in props.values():
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v)[:40]
    return inst.id[:8]


_TEMPORAL_HINTS = ("date", "time", "day", "week", "month", "year", "quarter", "ts",
                   "时间", "日期", "年", "月", "日", "季", "周")
_METRIC_LABELS = {"count": "数量", "sum": "合计", "avg": "平均值", "min": "最小值", "max": "最大值"}


def _is_temporal(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _TEMPORAL_HINTS)


def _build_aggregate_chart(ot_display: str, metric: str, metric_prop: Optional[str],
                           group_by: str, groups: list[dict]) -> Optional[dict]:
    """从聚合分组结果确定性地生成图表 spec —— 数字全部来自真实结果，零幻觉。

    仅在「分组 + ≥2 个有效数据点」时出图；单值聚合只是一个统计量，不出图。
    类型推断：时间维→折线(line)；计数且类别很少→饼图(pie，看占比)；否则柱状(bar，做对比)。
    图表 spec 与前端 ```chart 块同构，直接交给 AgentChart 渲染。
    """
    points = [{"label": str(g.get("group")), "value": g.get("value")}
              for g in groups if g.get("value") is not None][:runtime_limit("chart_point_cap")]
    if len(points) < 2:
        return None
    metric_label = _METRIC_LABELS.get(metric, metric)
    if _is_temporal(group_by):
        ctype = "line"
    elif metric == "count" and len(points) <= 6:
        ctype = "pie"
    else:
        ctype = "bar"
    y_label = metric_label if metric == "count" else f"{metric_label}({metric_prop})"
    return {
        "type": ctype,
        "title": f"{ot_display}：按{group_by}{metric_label}",
        "xLabel": group_by,
        "yLabel": y_label,
        "data": points,
    }


def _snippet(scope: AgentScope, inst: ObjectInstance, max_fields: int = runtime_limit("snippet_fields")) -> str:
    """引用实例的一句话属性摘要（跳过与标签重复的值）——让引用卡片不只有一个名字。"""
    ot = scope.object_types.get(inst.object_type_id)
    prop_defs = {p.get("name"): p for p in (ot.properties or [])
                 if isinstance(p, dict)} if ot else {}
    label = _label(scope, inst)
    parts: list[str] = []
    for k, v in (inst.properties or {}).items():
        if len(parts) >= max_fields:
            break
        if v in (None, "") or isinstance(v, (list, dict)):
            continue
        sval = str(v).strip()
        if not sval or sval == label:
            continue
        if len(sval) > 24:
            sval = sval[:24] + "…"
        disp = (prop_defs.get(k) or {}).get("displayName") or k
        parts.append(f"{disp}: {sval}")
    return " · ".join(parts)


class ToolRunner:
    """把 scope 装进闭包，给 orchestrator 一个统一的 run(name, args) 入口。
    顺带收集引用（citations）——回答里出现过的实例都可追溯。"""

    def __init__(self, db: Session, scope: AgentScope, *,
                 decision_context: Optional[dict] = None,
                 world_model_context: Optional[dict] = None):
        self.db = db
        self.scope = scope
        self.decision_context = decision_context or {}
        # 世界模型推演上下文：{"user": <User>} —— 菜单权限门控 + 调用记录审计人
        self.world_model_context = world_model_context or {}
        self.citations: list[dict] = []
        self.proposals: list[dict] = []
        self._cited: set[str] = set()
        self._world_model_invocations = 0

    def _cite(self, inst: ObjectInstance):
        if inst.id in self._cited or len(self.citations) >= runtime_limit("citation_cap"):
            return
        ot = self.scope.object_types.get(inst.object_type_id)
        ot_name = ot.display_name if ot else inst.object_type_id
        label = _label(self.scope, inst)
        self._cited.add(inst.id)
        self.citations.append({
            "instanceId": inst.id,
            "objectType": ot_name,
            "label": label,
            # 统一引用契约：预拼展示串 + 属性摘要，前端直接用
            "sourceLabel": f"{ot_name} · {label}",
            "snippet": _snippet(self.scope, inst),
        })

    def run(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ToolError(f"未知工具: {name}")
        return handler(args or {})

    # ------------------------------------------------------------ 各工具实现

    def _instance_query(self):
        query = self.db.query(ObjectInstance).filter(
            ObjectInstance.ontology_id == self.scope.ontology.id)
        if self.scope.release_id is not None:
            query = query.filter(
                ObjectInstance.ontology_release_id == self.scope.release_id)
        return query

    def _link_query(self, *entities):
        query = self.db.query(*(entities or (LinkInstance,))).filter(
            LinkInstance.ontology_id == self.scope.ontology.id)
        if self.scope.release_id is not None:
            query = query.filter(
                LinkInstance.ontology_release_id == self.scope.release_id)
        return query

    def _instances_of(self, type_id: str) -> list[ObjectInstance]:
        return (self._instance_query()
                .filter(ObjectInstance.object_type_id == type_id)
                .order_by(ObjectInstance.created_at.desc())
                .limit(runtime_limit("search_scan_cap")).all())

    def _stream_instances(self, type_id: str):
        """流式产出某类型的全部实例（yield_per，不materialize全表）。聚合走此口，
        以修正原先 .limit(5000) 盲截断导致的『大数据量下统计静默算错』。"""
        return (self._instance_query()
                .filter(ObjectInstance.object_type_id == type_id)
                .yield_per(2000))

    def _validated_filters(self, ot, filters):
        """校验并规范化过滤条件的属性名（越界硬失败）：把 displayName 归一到 name，
        属性不存在则 ToolError 让 LLM 自我修正——而非静默匹配为空。"""
        if not filters:
            return filters
        out = []
        for f in filters:
            if not isinstance(f, dict):
                continue
            out.append({**f, "property": self.scope.resolve_property(ot, f.get("property", ""))})
        return out

    def _apply_filters(self, rows: list[ObjectInstance], filters, keyword) -> list[ObjectInstance]:
        out = []
        kw = (keyword or "").strip().lower()
        for inst in rows:
            props = {**(inst.properties or {}), **(inst.computed or {})}
            if filters and not all(_match(props, f) for f in filters):
                continue
            if kw:
                hay = " ".join(str(v) for v in props.values()) + " " + inst.id + " " + (inst.external_id or "")
                if kw not in hay.lower():
                    continue
            out.append(inst)
        return out

    def _limit(self, requested) -> int:
        cap = self.scope.profile.max_rows_per_query or 50
        try:
            n = int(requested) if requested else cap
        except (TypeError, ValueError):
            n = cap
        return max(1, min(n, cap))

    def _tool_search_objects(self, args: dict) -> dict:
        ot = self.scope.require_object_type(args.get("object_type", ""))
        filters = self._validated_filters(ot, args.get("filters"))
        rows = self._instances_of(ot.id)
        matched = self._apply_filters(rows, filters, args.get("keyword"))
        limit = self._limit(args.get("limit"))
        page = matched[:limit]
        for inst in page:
            self._cite(inst)
        return {
            "objectType": ot.display_name,
            "total": len(matched),
            "returned": len(page),
            "truncated": len(matched) > len(page),
            "scannedCap": len(rows) >= runtime_limit("search_scan_cap"),
            "items": [_row(i) for i in page],
        }

    def _tool_get_object(self, args: dict) -> dict:
        inst = self.scope.visible_instance(
            self.db.query(ObjectInstance).filter(
                ObjectInstance.id == args.get("instance_id", "")).first())
        self._cite(inst)
        ot = self.scope.object_types[inst.object_type_id]

        neighbors = []
        for lt in self.scope.link_types.values():
            for direction, self_col, other_col in (
                    ("out", LinkInstance.source_object_id, LinkInstance.target_object_id),
                    ("in", LinkInstance.target_object_id, LinkInstance.source_object_id)):
                links = (self._link_query()
                         .filter(LinkInstance.link_type_id == lt.id,
                                 self_col == inst.id)
                         .limit(50).all())
                if not links:
                    continue
                sample = []
                for li in links[:5]:
                    other_id = li.target_object_id if direction == "out" else li.source_object_id
                    other = self._instance_query().filter(
                        ObjectInstance.id == other_id).first()
                    if other and other.object_type_id in self.scope.object_types:
                        sample.append({"instanceId": other.id, "label": _label(self.scope, other)})
                neighbors.append({
                    "linkType": lt.display_name, "linkTypeName": lt.name,
                    "direction": direction, "count": len(links), "sample": sample,
                })
        return {"objectType": ot.display_name, **_row(inst), "links": neighbors}

    def _tool_traverse_links(self, args: dict) -> dict:
        inst = self.scope.visible_instance(
            self.db.query(ObjectInstance).filter(
                ObjectInstance.id == args.get("instance_id", "")).first())
        lt = self.scope.require_link_type(args.get("link_type", ""))
        direction = args.get("direction") or "out"
        self_col = LinkInstance.source_object_id if direction == "out" else LinkInstance.target_object_id
        limit = self._limit(args.get("limit"))

        links = (self._link_query()
                 .filter(LinkInstance.link_type_id == lt.id,
                         self_col == inst.id)
                 .limit(limit + 1).all())
        items = []
        for li in links[:limit]:
            other_id = li.target_object_id if direction == "out" else li.source_object_id
            other = self._instance_query().filter(ObjectInstance.id == other_id).first()
            if not other or other.object_type_id not in self.scope.object_types:
                continue
            self._cite(other)
            ot = self.scope.object_types[other.object_type_id]
            items.append({"objectType": ot.display_name, **_row(other),
                          "linkProperties": li.properties or {}})
        return {
            "from": _label(self.scope, inst), "linkType": lt.display_name,
            "direction": direction, "returned": len(items),
            "truncated": len(links) > limit, "items": items,
        }

    def _tool_traverse_path(self, args: dict) -> dict:
        start = self.scope.visible_instance(
            self.db.query(ObjectInstance).filter(
                ObjectInstance.id == args.get("instance_id", "")).first())
        path = args.get("path")
        if not isinstance(path, list) or not path:
            raise ToolError('path 不能为空：给出有序跳序列，如 [{"link_type":"下单","direction":"out"}]')
        if len(path) > runtime_limit("max_hops"):
            raise ToolError(f"最多 {runtime_limit("max_hops")} 跳，当前 {len(path)} 跳。请缩短路径或分多次遍历")

        # 逐跳预解析链接类型（越界即失败：每一跳都受作用域约束，防经由链接泄露隐藏类型）
        hops = []
        for h in path:
            if not isinstance(h, dict):
                raise ToolError('path 每一跳必须是对象，如 {"link_type":"下单","direction":"out"}')
            lt = self.scope.require_link_type(h.get("link_type", ""))
            direction = h.get("direction") or "out"
            if direction not in ("out", "in"):
                raise ToolError("direction 只能是 out 或 in")
            hops.append((lt, direction))

        visited = {start.id}
        frontier = [start.id]
        budget_hit = frontier_capped = False
        hop_trace: list[dict] = []
        for depth, (lt, direction) in enumerate(hops):
            self_col = (LinkInstance.source_object_id if direction == "out"
                        else LinkInstance.target_object_id)
            cur = frontier[:runtime_limit("frontier_cap")]
            if len(frontier) > runtime_limit("frontier_cap"):
                frontier_capped = True
            next_ids: list[str] = []
            next_seen: set[str] = set()
            for node_id in cur:
                links = (self._link_query(
                            LinkInstance.source_object_id, LinkInstance.target_object_id)
                         .filter(LinkInstance.link_type_id == lt.id, self_col == node_id)
                         .limit(runtime_limit("hop_fanout_cap")).all())
                for src_id, tgt_id in links:
                    other = tgt_id if direction == "out" else src_id
                    if other in visited or other in next_seen:
                        continue
                    next_seen.add(other)
                    next_ids.append(other)
                    if len(visited) + len(next_ids) > runtime_limit("path_node_budget"):
                        budget_hit = True
                        break
                if budget_hit:
                    break
            visited.update(next_ids)
            frontier = next_ids
            hop_trace.append({"hop": depth + 1, "linkType": lt.display_name,
                              "direction": direction, "reached": len(next_ids)})
            if not frontier or budget_hit:
                break

        limit = self._limit(args.get("limit"))
        items = []
        for oid in frontier[:limit]:
            inst = self._instance_query().filter(ObjectInstance.id == oid).first()
            if not inst or inst.object_type_id not in self.scope.object_types:
                continue  # 终点仍受作用域约束
            self._cite(inst)
            otx = self.scope.object_types[inst.object_type_id]
            items.append({"objectType": otx.display_name, **_row(inst)})
        return {
            "from": _label(self.scope, start),
            "path": [{"linkType": lt.display_name, "direction": d} for lt, d in hops],
            "hops": hop_trace,
            "endpoints": len(frontier),
            "returned": len(items),
            "truncated": len(frontier) > limit,
            "budgetHit": budget_hit,
            "frontierCapped": frontier_capped,
            "items": items,
        }

    def _tool_find_paths(self, args: dict) -> dict:
        from app.ontologies.agent_runtime.graph_service import find_paths

        result = find_paths(
            self.scope,
            args.get("source_instance_id", ""),
            args.get("target_instance_id", ""),
            direction=args.get("direction") or "both",
            max_depth=args.get("max_depth") or 5,
            max_paths=args.get("max_paths") or 3,
        )
        for node in result.get("nodes") or []:
            instance = self.db.query(ObjectInstance).filter(
                ObjectInstance.id == node.get("entityId")).first()
            if instance:
                self._cite(instance)
        return result

    def _tool_analyze_change_impact(self, args: dict) -> dict:
        from app.ontologies.agent_runtime.graph_service import analyze_change_impact

        result = analyze_change_impact(
            self.scope,
            args.get("instance_id", ""),
            args.get("property", ""),
            args.get("proposed_value"),
            direction=args.get("direction") or "both",
            max_depth=args.get("max_depth") or 3,
        )
        for node in result.get("nodes") or []:
            instance = self.db.query(ObjectInstance).filter(
                ObjectInstance.id == node.get("entityId")).first()
            if instance:
                self._cite(instance)
        return result

    def _tool_aggregate_objects(self, args: dict) -> dict:
        ot = self.scope.require_object_type(args.get("object_type", ""))
        metric = args.get("metric") or "count"
        if metric not in ("count", "sum", "avg", "min", "max"):
            raise ToolError(f"不支持的 metric: {metric}")
        metric_prop = args.get("metric_property")
        if metric != "count" and not metric_prop:
            raise ToolError(f"metric={metric} 需要提供 metric_property（数值属性名）")
        # 属性护栏（越界硬失败）：拼错/臆造的属性名当场报错并给可用清单，不静默算错
        if metric_prop:
            metric_prop = self.scope.resolve_property(ot, metric_prop)
        group_by = args.get("group_by")
        if group_by:
            group_by = self.scope.resolve_property(ot, group_by)
        filters = self._validated_filters(ot, args.get("filters"))

        # 流式累加（不materialize全表，内存安全）：修正原先 .limit(5000) 盲截断
        # 导致的『大数据量下统计静默算错』；到安全阀才停并诚实标 partial。
        acc: dict[str, dict] = {}
        def bump(key: str, v):
            a = acc.get(key)
            if a is None:
                a = acc[key] = {"count": 0, "sum": 0.0, "nnum": 0, "min": None, "max": None}
            a["count"] += 1
            if v is not None:
                a["sum"] += v
                a["nnum"] += 1
                a["min"] = v if a["min"] is None else min(a["min"], v)
                a["max"] = v if a["max"] is None else max(a["max"], v)

        scanned = matched = 0
        partial = False
        for inst in self._stream_instances(ot.id):
            if scanned >= runtime_limit("aggregate_scan_cap"):
                partial = True
                break
            scanned += 1
            merged = {**(inst.properties or {}), **(inst.computed or {})}
            if filters and not all(_match(merged, f) for f in filters):
                continue
            matched += 1
            v = _num(merged.get(metric_prop)) if metric != "count" else None
            bump(str(merged.get(group_by)) if group_by else "__all__", v)

        def finalize(a: dict):
            if metric == "count":
                return a["count"]
            if a["nnum"] == 0:
                return None
            if metric == "sum":
                return round(a["sum"], 6)
            if metric == "avg":
                return round(a["sum"] / a["nnum"], 6)
            return a["min"] if metric == "min" else a["max"]

        base = {"objectType": ot.display_name, "metric": metric,
                "metricProperty": metric_prop, "scanned": scanned,
                "matched": matched, "partial": partial}
        if not group_by:
            empty = {"count": 0, "sum": 0.0, "nnum": 0, "min": None, "max": None}
            return {**base, "value": finalize(acc.get("__all__") or empty)}

        result = [{"group": k, "value": finalize(a), "count": a["count"]} for k, a in acc.items()]
        result.sort(key=lambda x: (x["value"] is None, -(x["value"] or 0)))
        groups_out = result[:50]
        out = {**base, "groupBy": group_by, "groups": groups_out,
               "truncated": len(result) > 50}
        # 确定性图表：直接从真实分组值生成，前端渲染，无需 LLM 再吐 chart
        chart = _build_aggregate_chart(ot.display_name, metric, metric_prop, group_by, groups_out)
        if chart:
            out["chart"] = chart
            out["chartRendered"] = True
        return out

    def _tool_get_object_history(self, args: dict) -> dict:
        inst = self.scope.visible_instance(
            self.db.query(ObjectInstance).filter(
                ObjectInstance.id == args.get("instance_id", "")).first())
        self._cite(inst)
        limit = min(self._limit(args.get("limit")), 50)
        base_query = self.db.query(PropertyFact).filter(
            PropertyFact.ontology_id == self.scope.ontology.id,
            PropertyFact.instance_id == inst.id)
        if args.get("property_name"):
            base_query = base_query.filter(
                PropertyFact.property_name == args["property_name"])

        # Legacy/unscoped callers retain the original all-history behavior.
        if self.scope.release_id is None:
            facts = (
                base_query.order_by(*fact_order_clause()).limit(limit).all()
            )
            return {
                "instance": _label(self.scope, inst),
                "facts": [{
                    "property": f.property_name,
                    "value": _trunc((f.value or {}).get("v")),
                    "present": (f.value or {}).get("present", True),
                    "kind": f.kind or "property",
                    "source": f.source,
                    "actorId": f.actor_id,
                    "causedBy": f.caused_by,
                    "confidence": f.confidence,
                    "recordedAt": utc_iso(f.recorded_at),
                } for f in facts],
            }

        current_release_id = str(self.scope.release_id)
        lineage = _history_release_lineage(
            self.db, self.scope.ontology.id, current_release_id)
        authority_ids = list(lineage["authorityReleaseIds"])
        facts = (
            base_query.filter(
                PropertyFact.ontology_release_id.in_(authority_ids))
            .order_by(*fact_order_clause())
            .limit(limit)
            .all()
        )

        def fact_payload(
                fact: PropertyFact, *, authoritative: bool,
                adopted_by_release_id: str | None = None) -> dict:
            release_id = (
                str(fact.ontology_release_id)
                if fact.ontology_release_id is not None else None
            )
            item = {
                "property": fact.property_name,
                "value": _trunc((fact.value or {}).get("v")),
                "present": (fact.value or {}).get("present", True),
                "kind": fact.kind or "property",
                "source": fact.source,
                "actorId": fact.actor_id,
                "causedBy": fact.caused_by,
                "confidence": fact.confidence,
                "recordedAt": utc_iso(fact.recorded_at),
                "ontologyReleaseId": release_id,
                "inherited": (
                    authoritative and release_id != current_release_id
                ),
                "authoritative": authoritative,
            }
            if adopted_by_release_id is not None:
                item["adoptedByReleaseId"] = adopted_by_release_id
            return item

        result = {
            "instance": _label(self.scope, inst),
            "releaseContext": {
                "currentReleaseId": current_release_id,
                "authorityReleaseIds": authority_ids,
                "baselineReleaseId": lineage["baselineReleaseId"],
                "lineageComplete": lineage["lineageComplete"],
            },
            "facts": [
                fact_payload(fact, authoritative=True)
                for fact in facts
            ],
        }

        current = lineage["current"]
        # Normal promotion is a provenance reset even when materializing the
        # reviewed trial produces no new Fact (a no-op value).  Make that
        # baseline adoption explicit rather than relabelling an older Fact as
        # current authority.
        if (
            current is not None
            and current.promoted_from_id
            and not facts
        ):
            merged = {
                **dict(inst.properties or {}),
                **dict(inst.computed or {}),
            }
            requested_property = args.get("property_name")
            if requested_property:
                adopted_properties = [{
                    "property": requested_property,
                    "value": _trunc(merged.get(requested_property)),
                    "present": requested_property in merged,
                }]
            else:
                adopted_properties = [{
                    "property": key,
                    "value": _trunc(value),
                    "present": True,
                } for key, value in sorted(merged.items())]

            audit_trial_id: str | None = None
            metadata_complete = False
            # The release row itself does not store the selected trial id.  A
            # publish audit plus the matching passed trial is the only complete
            # evidence; old releases without both stay explicit about the gap.
            from app.ontologies.inference.models import AuditLog
            from app.ontologies.versions.models import (
                OntologyTrialObject,
                OntologyTrialRun,
            )

            audits = self.db.query(AuditLog).filter(
                AuditLog.ontology_id == self.scope.ontology.id,
                AuditLog.event_type == "publish",
                AuditLog.event_subtype == "draft_promoted",
                AuditLog.object_id == current_release_id,
            ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).all()
            matching_trial_ids = {
                str((audit.meta or {}).get("trial_run_id"))
                for audit in audits
                if (
                    str((audit.meta or {}).get("draft_version_id") or "")
                    == str(current.promoted_from_id)
                    and (audit.meta or {}).get("trial_run_id")
                )
            }
            if len(matching_trial_ids) == 1:
                candidate_id = next(iter(matching_trial_ids))
                trial = self.db.query(OntologyTrialRun).filter(
                    OntologyTrialRun.id == candidate_id,
                    OntologyTrialRun.ontology_id
                    == self.scope.ontology.id,
                    OntologyTrialRun.version_id
                    == current.promoted_from_id,
                    OntologyTrialRun.status == "passed",
                ).first()
                if trial is not None:
                    audit_trial_id = candidate_id
                    trial_object = self.db.query(
                        OntologyTrialObject).filter(
                            OntologyTrialObject.trial_run_id == candidate_id,
                            OntologyTrialObject.object_id == inst.id,
                        ).first()
                    if trial_object is not None:
                        trial_values = {
                            **dict(trial_object.properties or {}),
                            **dict(trial_object.computed or {}),
                        }
                        metadata_complete = all(
                            (
                                item["property"] in trial_values
                                and trial_values.get(item["property"])
                                == merged.get(item["property"])
                            )
                            if item["present"] else (
                                item["property"] not in trial_values
                            )
                            for item in adopted_properties
                        )

            adoption = {
                "releaseId": current_release_id,
                "promotedFromId": str(current.promoted_from_id),
                "objectPresent": True,
                "properties": adopted_properties,
                "authoritative": True,
                "metadataComplete": metadata_complete,
            }
            if audit_trial_id is not None:
                adoption["trialRunId"] = audit_trial_id
            result["baselineAdoption"] = adoption

            # Older matching values can explain where a value was first seen,
            # but they are not part of the current release's authority
            # segment.  Search only the direct parent chain and never siblings.
            pre_release_ids = list(lineage["preBaselineReleaseIds"])
            if lineage["lineageComplete"] and pre_release_ids:
                property_names = [
                    item["property"] for item in adopted_properties
                ]
                old_facts = (
                    self.db.query(PropertyFact)
                    .filter(
                        PropertyFact.ontology_id
                        == self.scope.ontology.id,
                        PropertyFact.instance_id == inst.id,
                        PropertyFact.ontology_release_id.in_(
                            pre_release_ids),
                        PropertyFact.property_name.in_(property_names),
                        PropertyFact.kind.in_(["property", "derived"]),
                    )
                    .order_by(*fact_order_clause())
                    .all()
                )
                release_rank = {
                    release_id: rank
                    for rank, release_id in enumerate(pre_release_ids)
                }
                adopted_by_property = {
                    item["property"]: item
                    for item in adopted_properties
                }
                origins: dict[str, tuple[int, PropertyFact]] = {}
                for fact in old_facts:
                    adopted = adopted_by_property.get(fact.property_name)
                    if adopted is None:
                        continue
                    fact_value = (fact.value or {}).get("v")
                    fact_present = (fact.value or {}).get("present", True)
                    if (
                        fact_value != merged.get(fact.property_name)
                        or fact_present != adopted["present"]
                    ):
                        continue
                    rank = release_rank.get(
                        str(fact.ontology_release_id), len(release_rank))
                    previous = origins.get(fact.property_name)
                    # Query ordering already selects newest Fact within a
                    # release; only a nearer release may replace it.
                    if previous is None or rank < previous[0]:
                        origins[fact.property_name] = (rank, fact)
                if origins:
                    result["preBaselineOrigin"] = [
                        fact_payload(
                            origin[1],
                            authoritative=False,
                            adopted_by_release_id=current_release_id,
                        )
                        for _, origin in sorted(
                            origins.items(),
                            key=lambda item: (
                                item[1][0], item[0],
                            ),
                        )
                    ]
        return result

    def _tool_trace_causal_chain(self, args: dict) -> dict:
        inst = self.scope.visible_instance(
            self.db.query(ObjectInstance).filter(
                ObjectInstance.id == args.get("instance_id", "")).first())
        self._cite(inst)
        property_name = args.get("property_name")
        if property_name:
            object_type = self.scope.object_types.get(inst.object_type_id)
            if object_type is None:
                raise ToolError("实例的对象类型不在授权范围内")
            property_name = self.scope.resolve_property(
                object_type, property_name)
        authority_release_ids = None
        if self.scope.release_id is not None:
            lineage = _history_release_lineage(
                self.db, self.scope.ontology.id, str(self.scope.release_id))
            authority_release_ids = list(lineage["authorityReleaseIds"])
        from app.ontologies.decision_intelligence.service import (
            trace_causal_chain,
        )
        result = trace_causal_chain(
            self.db,
            ontology_id=self.scope.ontology.id,
            instance_id=inst.id,
            property_name=property_name,
            direction=args.get("direction") or "both",
            max_depth=args.get("max_depth") or 3,
            authority_release_ids=authority_release_ids,
        )
        result["instance"] = _label(self.scope, inst)
        # 链上涉及的其他实例也给引用（仅授权范围内的类型可引用）
        other_ids = sorted({
            node["instanceId"]
            for node in result["nodes"]
            if node["kind"] == "fact"
            and node.get("instanceId") and node["instanceId"] != inst.id
        })
        for other in self.db.query(ObjectInstance).filter(
                ObjectInstance.id.in_(other_ids[:runtime_limit("citation_cap") * 2])).all():
            if other.object_type_id in self.scope.object_types:
                self._cite(other)
        return result

    def _tool_explain_sentinel_firing(self, args: dict) -> dict:
        from app.models.sentinel import Sentinel, SentinelFiring
        from app.ontologies.sentinels.explanation_service import (
            explain_sentinel_firing,
            resolve_sentinel_definition,
        )

        sentinel_ref = str(
            args.get("sentinel_id")
            or args.get("sentinel_name")
            or "").strip()
        if not sentinel_ref:
            raise ToolError("需要提供 sentinel_id（哨兵的 id、name 或显示名）")
        firing_query = self.db.query(SentinelFiring).filter(
            SentinelFiring.ontology_id == self.scope.ontology.id)
        firing_id = args.get("firing_id")
        if firing_id:
            firing = firing_query.filter(
                SentinelFiring.id == firing_id).first()
            if firing is None:
                raise ToolError("触发记录不存在")
        else:
            live = self.db.query(Sentinel).filter(
                Sentinel.ontology_id == self.scope.ontology.id,
                Sentinel.name == sentinel_ref,
            ).first()
            if live is None:
                live = self.db.query(Sentinel).filter(
                    Sentinel.ontology_id == self.scope.ontology.id,
                    Sentinel.display_name == sentinel_ref,
                ).first()
            query = firing_query
            if live is not None:
                query = query.filter(
                    SentinelFiring.sentinel_id == live.id)
            else:
                # 内置哨兵可能无 live 行：按 id 或触发名匹配
                query = query.filter(
                    (SentinelFiring.sentinel_id == sentinel_ref)
                    | (SentinelFiring.sentinel_name == sentinel_ref))
            firing = query.order_by(
                SentinelFiring.created_at.desc()).first()
            if firing is None:
                raise ToolError(
                    f"没有找到哨兵「{sentinel_ref}」的触发记录")
        definition = resolve_sentinel_definition(
            self.db, ontology_id=self.scope.ontology.id,
            sentinel_id=firing.sentinel_id, firing=firing)
        if definition is None:
            raise ToolError(
                "无法解析该哨兵的定义（既无 live 行，也不在触发所属"
                " release 快照中）")
        result = explain_sentinel_firing(
            self.db, ontology_id=self.scope.ontology.id,
            definition=definition, firing=firing,
            match_limit=args.get("match_limit") or 3)
        # 链上涉及的对象实例给引用（仅授权范围内的类型）
        instance_ids = sorted({
            alias.get("instanceId")
            for item in result["matchedTuples"]
            for alias in item.get("aliases", [])
            if alias.get("instanceId")
        })
        for inst in self.db.query(ObjectInstance).filter(
                ObjectInstance.id.in_(
                    instance_ids[:int(runtime_limit("citation_cap")) * 2])
        ).all():
            if inst.object_type_id in self.scope.object_types:
                self._cite(inst)
        return result

    def _tool_list_actions(self, args: dict) -> dict:
        items = []
        for a in self.scope.actions.values():
            ot = self.scope.object_types.get(a.object_type_id) if a.object_type_id else None
            items.append({
                "name": a.name, "displayName": a.display_name,
                "description": a.description,
                "targetObjectType": ot.display_name if ot else None,
                "requiresApproval": bool(a.requires_approval),
                "parameters": [
                    {"name": p.get("name"), "displayName": p.get("displayName"),
                     "type": p.get("type", "string"), "required": bool(p.get("required")),
                     "description": p.get("description")}
                    for p in (a.parameters or []) if isinstance(p, dict)
                ],
            })
        return {"actions": items}

    def _tool_run_decision_simulation(self, args: dict) -> dict:
        context = self.decision_context
        if not context.get("call_kwargs"):
            raise ToolError("当前调用缺少决策推演模型上下文，请重新发起对话")
        if not context.get("created_by"):
            raise ToolError("当前用户不可审计，无法创建决策推演记录")
        from app.ontologies.decision_simulation.service import compact_tool_result, execute
        run = execute(
            self.db,
            self.scope,
            question=args.get("question") or "",
            alternatives=args.get("alternatives") if isinstance(args.get("alternatives"), list) else [],
            horizon=args.get("horizon"),
            conversation_id=context.get("conversation_id"),
            created_by=context["created_by"],
            call_kwargs=context["call_kwargs"],
            model_config_id=context.get("model_config_id"),
        )
        return compact_tool_result(run)

    # ------------------------------------------------------------ 世界模型推演

    def _require_world_model_access(self) -> None:
        """世界模型推演是跨域能力：进程内调用绕过了 world_model 路由的
        menu_guard，必须在此复刻同一道菜单权限门（Q2 决策：要求 world_model key）。"""
        from app.auth.permissions import user_has_menu_access
        user = self.world_model_context.get("user")
        if user is None:
            raise ToolError("当前调用缺少用户上下文，无法使用世界模型推演")
        if not user_has_menu_access(self.db, user, "world_model"):
            raise ToolError(
                "当前用户没有「世界模型」菜单权限，无法使用世界模型推演；"
                "请联系管理员在用户管理的角色菜单中勾选。")

    def _world_model_eligibility(self, service) -> tuple[bool, list[str]]:
        """服务对当前会话是否可用：本体绑定 → 类型存在（含授权白名单交集，
        兼作版本漂移检测）→ 前置条件实例数。任一不满足给出人可读原因。"""
        binding = service.applicable_object_types
        if not isinstance(binding, dict):
            return False, ["服务未登记本体语义注册信息"]
        if binding.get("ontology_id") != self.scope.ontology.id:
            return False, ["服务未注册到当前本体"]
        reasons: list[str] = []
        known = self.scope.object_types
        missing = [t for t in (binding.get("object_type_ids") or []) if t not in known]
        if missing:
            reasons.append(
                f"依赖的 {len(missing)} 个对象类型已不在当前版本或授权范围内"
                "（本体版本演进或权限变更），需要服务维护者重新发布")
        counts = self.scope.instance_counts()
        for pre in (service.preconditions or []):
            if not isinstance(pre, dict):
                continue
            tid = str(pre.get("object_type_id") or "")
            need = int(pre.get("min_count") or 1)
            have = counts.get(tid, 0)
            if have < need:
                ot = known.get(tid)
                label = ot.display_name if ot else tid
                reasons.append(f"前置条件不满足：{label} 实例数 {have} < 要求 {need}")
        return (not reasons), reasons

    def _world_model_example(self, service) -> Optional[dict]:
        """取发布时冻结版本的 test_input 作为 context 组装样板（截断保护）。"""
        from app.world_model.models import WorldModelScriptVersion
        if not service.version_id:
            return None
        version = self.db.get(WorldModelScriptVersion, service.version_id)
        if version is None or not isinstance(version.test_input, dict):
            return None
        payload = json.dumps(version.test_input, ensure_ascii=False, default=str)
        cap = runtime_limit("world_model_example_chars")
        if len(payload) > cap:
            return {"_truncated": True, "preview": payload[:cap]}
        return version.test_input

    def _tool_list_world_model_services(self, args: dict) -> dict:
        self._require_world_model_access()
        from app.world_model.models import SERVICE_STATUS_ONLINE, WorldModelService
        rows = (self.db.query(WorldModelService)
                .filter(WorldModelService.status == SERVICE_STATUS_ONLINE)
                .order_by(WorldModelService.updated_at.desc()).all())
        available: list[dict] = []
        blocked: list[dict] = []
        known = self.scope.object_types
        for svc in rows:
            binding = svc.applicable_object_types
            if not isinstance(binding, dict) or binding.get("ontology_id") != self.scope.ontology.id:
                continue  # 注册到其他本体的服务不进入本会话视野
            item = {
                "serviceId": svc.id,
                "name": svc.name,
                "description": svc.description or "",
                "applicableObjectTypes": [
                    known[t].display_name if t in known else t
                    for t in (binding.get("object_type_ids") or [])],
            }
            ok, reasons = self._world_model_eligibility(svc)
            if ok:
                example = self._world_model_example(svc)
                if example is not None:
                    item["exampleInput"] = example
                available.append(item)
            else:
                item["reasons"] = reasons
                blocked.append(item)
        cap = runtime_limit("world_model_list_cap")
        return {
            "kind": "world_model_services",
            "available": available[:cap],
            "blocked": blocked[:cap],
            "note": ("调用 run_world_model_simulation 时 context/actions 按 exampleInput 的形状、"
                     "用查询工具收集的真实数据组装；blocked 为版本演进或数据不足暂不可用的服务。"),
        }

    def _tool_run_world_model_simulation(self, args: dict) -> dict:
        self._require_world_model_access()
        cap_invokes = runtime_limit("world_model_invoke_per_turn")
        if self._world_model_invocations >= cap_invokes:
            raise ToolError(
                f"本回合世界模型推演调用已达上限（{cap_invokes} 次）；"
                "请基于已有推演结果作答，或把问题拆到下一回合。")
        from fastapi import HTTPException
        from app.world_model import service as world_model_service
        from app.world_model.models import SERVICE_STATUS_ONLINE, WorldModelService
        from app.world_model.schemas import InvokeRequest
        service_id = str(args.get("service_id") or "").strip()
        if not service_id:
            raise ToolError("缺少 service_id；请先用 list_world_model_services 获取可用服务。")
        svc = self.db.get(WorldModelService, service_id)
        if svc is None:
            raise ToolError("推演服务不存在或已被删除。")
        if svc.status != SERVICE_STATUS_ONLINE:
            raise ToolError(f"推演服务「{svc.name}」当前状态为 {svc.status}，未在线。")
        ok, reasons = self._world_model_eligibility(svc)
        if not ok:
            raise ToolError(f"推演服务「{svc.name}」当前不可用：" + "；".join(reasons))
        context = args.get("context") if isinstance(args.get("context"), dict) else {}
        actions = args.get("actions") if isinstance(args.get("actions"), list) else []
        try:
            horizon = int(args.get("horizon", 1))
        except (TypeError, ValueError):
            horizon = 1
        horizon = max(0, horizon)
        try:
            size = len(json.dumps({"context": context, "actions": actions},
                                  ensure_ascii=False, default=str))
        except (TypeError, ValueError) as exc:
            raise ToolError("context/actions 必须是可 JSON 序列化的数据") from exc
        cap_chars = runtime_limit("world_model_context_chars")
        if size > cap_chars:
            raise ToolError(
                f"context+actions 序列化后 {size} 字符，超过上限 {cap_chars}；"
                "请缩小数据范围后重试。")
        user = self.world_model_context.get("user")
        try:
            self._world_model_invocations += 1
            result = world_model_service.invoke_service(
                self.db, service_id,
                InvokeRequest(context=context, actions=actions, horizon=horizon),
                user)
        except HTTPException as exc:
            detail = exc.detail
            message = str(detail.get("message") or detail) if isinstance(detail, dict) else str(detail)
            raise ToolError(f"推演服务调用失败：{message}") from exc
        out = {
            "kind": "world_model_simulation",
            "serviceId": service_id,
            "serviceName": svc.name,
            "ok": bool(result.ok),
            "payload": result.payload,
            "durationMs": result.duration_ms,
            "callId": result.call_id,
        }
        # 失败才带 error 键：orchestrator._summarize / step.error 都按"键存在"判定错误，
        # 成功时带 error=None 会把步骤摘要变成字符串 'None'（线上验收实测发现）。
        if result.error:
            out["error"] = result.error
        return out

    def _tool_list_dynamic_sentinels(self, args: dict) -> dict:
        if self.scope.release is None:
            raise ToolError("动态哨兵管理需要锁定当前正式发布版本")
        from app.ontologies.sentinels import dynamic_service
        rows = dynamic_service.list_dynamic(
            self.db, self.scope.release, self.scope)
        return {"releaseId": self.scope.release_id, "sentinels": rows}

    def _tool_propose_dynamic_sentinel_change(self, args: dict) -> dict:
        if self.scope.release is None:
            raise ToolError("动态哨兵管理需要锁定当前正式发布版本")
        operation = str(args.get("operation") or "").strip()
        if operation not in {"create", "update", "enable", "disable", "delete"}:
            raise ToolError("operation 必须是 create/update/enable/disable/delete")
        from fastapi import HTTPException
        from app.ontologies.sentinels import dynamic_service
        sentinel_id = str(args.get("sentinel_id") or "").strip() or None
        definition = args.get("definition")
        expected_revision = None
        try:
            if operation != "create":
                row = dynamic_service.dynamic_row(
                    self.db, self.scope.ontology.id, sentinel_id or "")
                expected_revision = row.definition_revision
                if operation == "update":
                    patch = definition if isinstance(definition, dict) else {}
                    definition = {
                        **dynamic_service.definition_from_row(row),
                        **patch,
                    }
            proposal = dynamic_service.proposal(
                self.db, self.scope.release, self.scope, operation,
                sentinel_id=sentinel_id,
                definition=definition if isinstance(definition, dict) else None,
                expected_revision=expected_revision,
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict):
                raise ToolError(str(detail.get("message") or detail)) from exc
            raise ToolError(str(detail)) from exc
        self.proposals.append(proposal)
        return {
            "proposal": proposal,
            "note": "已生成动态哨兵变更提案，等待用户在界面确认；尚未修改任何哨兵。",
        }

    def _tool_propose_action(self, args: dict) -> dict:
        if not self.scope.profile.allow_action_proposals:
            raise ToolError("当前授权边界禁止提出动作提案（allow_action_proposals=false）")
        if self.scope.release_id is None:
            raise ToolError("动作提案必须锁定当前发布版本；请刷新本体助手后重试")
        action = self.scope.require_action(args.get("action", ""))
        target_id = args.get("target_instance_id")
        if target_id:
            # 目标实例必须可见 —— 不允许对隐藏类型的实例发起动作
            self.scope.visible_instance(
                self.db.query(ObjectInstance).filter(ObjectInstance.id == target_id).first())

        from app.ontologies.formal_modeling.schemas import RunActionRequest
        from app.services.formal.action_engine import execute_action
        body = RunActionRequest(action_id=action.id,
                                parameters=args.get("parameters") or {},
                                target_instance_id=target_id, dry_run=True,
                                release_id=self.scope.release_id)
        log = execute_action(
            self.db,
            self.scope.ontology.id,
            body,
            expected_release_id=self.scope.release_id,
        )

        proposal = {
            "kind": "action",
            "proposalId": f"prop-{int(time.time() * 1000)}-{len(self.proposals)}",
            "releaseId": self.scope.release_id,
            "actionId": action.id,
            "actionName": action.display_name,
            "parameters": args.get("parameters") or {},
            "targetInstanceId": target_id,
            "requiresApproval": bool(action.requires_approval),
            "status": log.get("status"),
            "validationErrors": log.get("validationErrors") or [],
            "effects": log.get("effects") or [],
        }
        self.proposals.append(proposal)
        return {
            "proposal": proposal,
            "note": ("预演通过。已生成提案卡片，等待用户确认后才会真实执行"
                     + ("（该动作还需人工审批）。" if action.requires_approval else "。"))
            if log.get("status") == "success"
            else "预演未通过，请根据 validationErrors 修正参数后重试。",
        }
