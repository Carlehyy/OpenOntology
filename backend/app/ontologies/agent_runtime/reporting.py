"""智能助手的数据分析报告能力。

第一性原理：报告模板不是自由 HTML，而是可编辑、可验证、可版本化的查询与
呈现契约。AI 负责起草结构和叙述；真实数据只通过授权范围内的只读工具取得；
HTML 由服务端确定性渲染。模板修改后必须重新试运行，质量门通过才允许发布。
"""
from __future__ import annotations

import html
import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.model_configs.selector import llm_call_kwargs, select_llm_model_config
from app.ontologies.agent_runtime import llm_bridge
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.models import AnalysisReportRun, AnalysisReportTemplate
from app.ontologies.agent_runtime.toolkit import ToolRunner

logger = logging.getLogger(__name__)

REPORT_READ_TOOLS = {"aggregate_objects", "search_objects"}
REPORT_VISUALIZATIONS = {"auto", "kpi", "bar", "line", "pie", "table", "none"}
MAX_SECTIONS = 8
MAX_QUERIES_PER_SECTION = 4
MAX_QUERY_ARGUMENT_BYTES = 12_000
FILTER_OPERATORS = {"eq", "neq", "contains", "in", "gt", "gte", "lt", "lte"}

DEFAULT_STYLE = {
    "theme": "editorial-light",
    "accent": "teal",
    "density": "comfortable",
    "showSources": True,
}


def normalize_style(style: Optional[dict[str, Any]]) -> dict[str, Any]:
    """只保留渲染器支持的外观令牌，避免无界 JSON 进入持久化模板。"""
    raw = style if isinstance(style, dict) else {}
    show_sources = raw.get("showSources", DEFAULT_STYLE["showSources"])
    if not isinstance(show_sources, bool):
        show_sources = DEFAULT_STYLE["showSources"]
    return {
        "theme": raw.get("theme") if raw.get("theme") in {"editorial-light"} else DEFAULT_STYLE["theme"],
        "accent": raw.get("accent") if raw.get("accent") in {"teal"} else DEFAULT_STYLE["accent"],
        "density": raw.get("density") if raw.get("density") in {"comfortable"} else DEFAULT_STYLE["density"],
        "showSources": show_sources,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: Any, cap: int) -> str:
    return str(value or "").strip()[:cap]


def _slug(value: str, index: int) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_-]+", "-", value or "").strip("-").lower()
    return raw[:48] or f"section-{index + 1}"


def _normalize_filters(value: Any, title: str) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError(f"章节「{title}」的 filters 必须是最多 10 项的数组")
    filters = []
    for item in value:
        if not isinstance(item, dict) or not str(item.get("property") or "").strip():
            raise ValueError(f"章节「{title}」的过滤条件必须包含 property")
        op = str(item.get("op") or "eq")
        if op not in FILTER_OPERATORS:
            raise ValueError(f"章节「{title}」包含不支持的过滤操作符：{op}")
        filters.append({"property": str(item["property"]).strip(), "op": op,
                        "value": deepcopy(item.get("value"))})
    return filters


def _normalize_query_arguments(tool: str, arguments: dict[str, Any], title: str) -> dict[str, Any]:
    if tool == "aggregate_objects":
        allowed = {"object_type", "metric", "metric_property", "group_by", "filters"}
    else:
        allowed = {"object_type", "filters", "keyword", "limit"}
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"章节「{title}」的 {tool} 含未知参数：{', '.join(unknown)}")

    object_type = str(arguments.get("object_type") or "").strip()
    if not object_type:
        raise ValueError(f"章节「{title}」的 {tool} 缺少 object_type")
    normalized: dict[str, Any] = {"object_type": object_type}
    filters = _normalize_filters(arguments.get("filters"), title)
    if filters:
        normalized["filters"] = filters

    if tool == "aggregate_objects":
        metric = str(arguments.get("metric") or "count")
        if metric not in {"count", "sum", "avg", "min", "max"}:
            raise ValueError(f"章节「{title}」使用了不支持的聚合指标：{metric}")
        normalized["metric"] = metric
        metric_property = str(arguments.get("metric_property") or "").strip()
        if metric != "count" and not metric_property:
            raise ValueError(f"章节「{title}」的 {metric} 聚合缺少 metric_property")
        if metric_property:
            normalized["metric_property"] = metric_property
        group_by = arguments.get("group_by")
        if group_by is not None:
            if not isinstance(group_by, str) or not group_by.strip():
                raise ValueError(f"章节「{title}」的 group_by 必须是单个属性名")
            normalized["group_by"] = group_by.strip()
    else:
        keyword = arguments.get("keyword")
        if keyword not in (None, ""):
            normalized["keyword"] = _clean_text(keyword, 300)
        try:
            requested_limit = int(arguments.get("limit", 12))
        except (TypeError, ValueError):
            requested_limit = 12
        normalized["limit"] = max(1, min(requested_limit, 20))
    return normalized


def normalize_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规范化并收紧模板契约；禁止把写工具混入自动报告。"""
    if not isinstance(sections, list) or not sections:
        raise ValueError("报告模板至少需要一个分析章节")
    if len(sections) > MAX_SECTIONS:
        raise ValueError(f"报告模板最多支持 {MAX_SECTIONS} 个章节")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(sections):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 个章节格式不正确")
        title = _clean_text(raw.get("title"), 160)
        goal = _clean_text(raw.get("goal"), 1200)
        if not title or not goal:
            raise ValueError(f"第 {index + 1} 个章节缺少标题或分析目标")
        section_id = _slug(_clean_text(raw.get("id"), 80) or title, index)
        if section_id in seen:
            section_id = f"{section_id}-{index + 1}"
        seen.add(section_id)

        visualization = str(raw.get("visualization") or "auto").lower()
        if visualization not in REPORT_VISUALIZATIONS:
            raise ValueError(f"章节「{title}」使用了不支持的图表类型：{visualization}")

        plan = raw.get("queryPlan") or raw.get("query_plan") or []
        if not isinstance(plan, list) or not plan:
            raise ValueError(f"章节「{title}」还没有数据查询计划")
        if len(plan) > MAX_QUERIES_PER_SECTION:
            raise ValueError(f"章节「{title}」最多允许 {MAX_QUERIES_PER_SECTION} 个查询")
        normalized_plan = []
        for item in plan:
            if not isinstance(item, dict):
                raise ValueError(f"章节「{title}」的数据查询格式不正确")
            tool = str(item.get("tool") or "").strip()
            if tool not in REPORT_READ_TOOLS:
                raise ValueError(
                    f"报告模板只允许可重复的只读统计/检索工具，章节「{title}」包含：{tool or '未知工具'}")
            arguments = item.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError(f"章节「{title}」的查询参数必须是对象")
            try:
                argument_size = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"章节「{title}」的查询参数必须是可序列化 JSON") from exc
            if argument_size > MAX_QUERY_ARGUMENT_BYTES:
                raise ValueError(f"章节「{title}」的单次查询参数过大")
            normalized_plan.append({
                "tool": tool,
                "arguments": _normalize_query_arguments(tool, deepcopy(arguments), title),
            })

        normalized.append({
            "id": section_id,
            "title": title,
            "goal": goal,
            "visualization": visualization,
            "queryPlan": normalized_plan,
        })
    return normalized


def _first_groupable_property(object_type) -> Optional[str]:
    for prop in object_type.properties or []:
        if not isinstance(prop, dict):
            continue
        if str(prop.get("type") or "string").lower() in {"string", "enum", "boolean"}:
            name = prop.get("name")
            if name:
                return str(name)
    return None


def _fallback_spec(scope, brief: str) -> dict[str, Any]:
    # 远程绑定类型不进 fallback 自动选题：本地报告查询面没有它的数据，
    # 自动选上只会产出整章 remote_ontology_semantic_query_required 错误。
    from app.ontologies.agent_runtime.boundary import remote_object_type_ids
    remote_ids = remote_object_type_ids(scope)
    object_types = [object_type for object_type in scope.object_types.values()
                    if object_type.id not in remote_ids]
    if not object_types and scope.object_types:
        raise ValueError(
            "当前授权范围内的对象类型均为远程绑定，本地无物化事实；"
            "报告查询请经统一查询入口（ontology_query）获取远程数据")
    sections: list[dict[str, Any]] = []
    for index, object_type in enumerate(object_types[:3]):
        sections.append({
            "id": f"overview-{index + 1}",
            "title": f"{object_type.display_name}规模概览",
            "goal": f"统计{object_type.display_name}的当前规模，并说明这一规模对业务判断意味着什么。",
            "visualization": "kpi",
            "queryPlan": [{
                "tool": "aggregate_objects",
                "arguments": {"object_type": object_type.name, "metric": "count"},
            }],
        })
        group_by = _first_groupable_property(object_type)
        if group_by and len(sections) < 4:
            sections.append({
                "id": f"distribution-{index + 1}",
                "title": f"{object_type.display_name}结构分布",
                "goal": f"按 {group_by} 分析{object_type.display_name}的分布，识别集中项和需要关注的长尾。",
                "visualization": "bar",
                "queryPlan": [{
                    "tool": "aggregate_objects",
                    "arguments": {"object_type": object_type.name, "metric": "count", "group_by": group_by},
                }],
            })
        if len(sections) >= 3:
            break
    if not sections:
        raise ValueError("当前授权范围内没有可用于报告的对象类型")
    if len(sections) == 1:
        object_type = object_types[0]
        sections.append({
            "id": "sample-records",
            "title": f"{object_type.display_name}数据样本",
            "goal": f"抽取少量{object_type.display_name}记录，帮助用户核对字段和值是否符合预期。",
            "visualization": "table",
            "queryPlan": [{
                "tool": "search_objects",
                "arguments": {"object_type": object_type.name, "limit": 8},
            }],
        })
    topic = _clean_text(brief, 80) or f"{scope.ontology.name}经营分析"
    return {
        "name": topic if len(topic) <= 48 else f"{scope.ontology.name}数据分析报告",
        "description": f"围绕“{topic}”形成的汇报级数据分析模板；发布前必须用真实数据完成试运行确认。",
        "sections": sections[:4],
        "style": deepcopy(DEFAULT_STYLE),
        "generationMode": "fallback",
    }


def _extract_json(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if "```" in text:
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
        if fenced:
            text = fenced[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的模板不是对象")
    return parsed


def generate_template_spec(db: Session, ontology_id: str, brief: str,
                           model_id: Optional[str] = None,
                           conversation_context: str = "") -> dict[str, Any]:
    """AI 起草模板；失败时退到确定性模板，保证能力可用但显式标记来源。"""
    _, profile, scope = build_scope(db, ontology_id)
    if not profile.enabled:
        raise ToolError("该本体的智能体已停用")
    fallback = _fallback_spec(scope, brief)
    config = select_llm_model_config(db, model_id=model_id or profile.default_model_id)
    call_kwargs = llm_call_kwargs(config)
    if not call_kwargs:
        return fallback

    prompt = f"""为本体「{scope.ontology.name}」生成一份可编辑、可重复运行的数据分析报告模板。

# 用户的报告目标
{brief[:4000]}

# 可选对话背景
{conversation_context[:5000] or '（无）'}

# 授权范围内的本体技能卡
{scope.skill_card()}

# 输出约束
只输出一个 JSON 对象，不要 markdown 代码块。结构必须是：
{{
  "name": "不超过40字的报告名",
  "description": "报告适用场景与核心目的",
  "sections": [
    {{
      "id": "英文短标识",
      "title": "章节标题",
      "goal": "本章节要回答的业务问题和应提炼的结论",
      "visualization": "auto|kpi|bar|line|pie|table|none",
      "queryPlan": [{{"tool": "aggregate_objects|search_objects", "arguments": {{}}}}]
    }}
  ]
}}

规则：
1. 生成 3 到 5 个章节，形成“核心规模→结构/趋势→风险/明细→结论”的汇报逻辑。
2. 数据查询必须引用技能卡里真实存在的对象类型和属性；禁止臆造字段。
3. 自动报告只允许 aggregate_objects 和 search_objects，禁止动作、自由 SQL 和具体实例 ID。
4. aggregate_objects 参数严格为：object_type、metric(count|sum|avg|min|max)、可选 metric_property、可选 group_by（单个字符串）、可选 filters。
   示例：{{"tool":"aggregate_objects","arguments":{{"object_type":"PurchaseOrder","metric":"sum","metric_property":"amount","group_by":"month"}}}}。
5. search_objects 参数严格为：object_type、可选 filters、可选 keyword、limit；filters 每项严格为 property/op/value。
   示例：{{"tool":"search_objects","arguments":{{"object_type":"PurchaseOrder","filters":[{{"property":"status","op":"eq","value":"延期"}}],"limit":12}}}}。
6. 一个 aggregate_objects 只能有一个 metric；需要总量和总金额时拆成两个查询。禁止使用 object、aggregations、field 或数组形式 group_by。
7. 统计优先 aggregate_objects；明细核对才用 search_objects，limit 不超过 12。
8. line 只用于明确的时间属性且预计至少 4 个数据点；类别超过 6 项不要用 pie。
9. 每个章节至少一个查询，最多两个查询。"""
    try:
        response = llm_bridge.chat(call_kwargs, [
            {"role": "system", "content": "你是严谨的企业数据分析报告设计师，只使用授权本体中的真实字段。"},
            {"role": "user", "content": prompt},
        ], tools=[])
        parsed = _extract_json(response.get("content") or "")
        sections = normalize_sections(parsed.get("sections") or [])
        return {
            "name": _clean_text(parsed.get("name"), 240) or fallback["name"],
            "description": _clean_text(parsed.get("description"), 2000) or fallback["description"],
            "sections": sections,
            "style": deepcopy(DEFAULT_STYLE),
            "generationMode": "ai",
        }
    except Exception as exc:  # noqa: BLE001 — AI 起草失败不得摧毁确定性能力
        logger.warning("报告模板 AI 起草失败，使用确定性模板: %s", exc)
        return fallback


def template_snapshot(template: AnalysisReportTemplate, ontology_name: str = "") -> dict[str, Any]:
    return {
        "id": template.id,
        "ontologyId": template.ontology_id,
        "ontologyName": ontology_name,
        "name": template.name,
        "description": template.description,
        "revision": template.revision,
        "sections": deepcopy(template.sections or []),
        "style": normalize_style(template.style),
        "status": template.status,
    }


def _result_summary(result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if "groups" in result:
        groups = result.get("groups") or []
        if not groups:
            return "真实数据查询未返回可分组的数据。"
        top = groups[:3]
        values = [item.get("value") for item in groups if isinstance(item.get("value"), (int, float))]
        total = sum(values)
        dimension_names = {"risk_level": "风险等级", "order_month": "订单月份", "status": "状态"}
        dimension = dimension_names.get(result.get("groupBy"), result.get("groupBy") or "分组维度")
        parts = []
        for item in top:
            share = ""
            if total > 0 and isinstance(item.get("value"), (int, float)):
                share = f"（占 {item.get('value') / total * 100:.1f}%）"
            parts.append(f"{item.get('group')}为 {_number(item.get('value'))}{share}")
        return (f"{result.get('objectType') or '对象'}按{dimension}形成 {len(groups)} 个分组，"
                f"其中{'、'.join(parts)}。以上结论仅反映本次查询的分组结果，不代表不同对象之间存在关联或因果关系。")
    if "value" in result:
        metric_names = {"count": "数量", "sum": "合计", "avg": "平均值", "min": "最小值", "max": "最大值"}
        metric = metric_names.get(result.get("metric"), result.get("metric") or "指标")
        prop = f"（{result.get('metricProperty')}）" if result.get("metricProperty") else ""
        return f"{result.get('objectType') or '对象'}的{metric}{prop}为 {_number(result.get('value'))}。"
    if "items" in result:
        return f"本次真实数据检索命中 {result.get('total', len(result.get('items') or []))} 条，展示 {len(result.get('items') or [])} 条样本。"
    return "本次真实数据查询已完成。"


def _unsupported_object_reference(scope, section: dict[str, Any], narrative: str) -> Optional[str]:
    """拦截没有查询证据的跨对象归因；命中后保留确定性摘要，不发布该段 AI 文案。"""
    common_terms = ("订单", "供应商", "客户", "患者", "合同", "物料", "产品", "仓库",
                    "员工", "资产", "发票", "项目")
    evidence_labels = {
        str((query.get("result") or {}).get("objectType") or "")
        for query in section.get("queries") or []
    }
    allowed_terms: set[str] = set()
    forbidden_terms: set[str] = set()
    for object_type in scope.object_types.values():
        label = str(object_type.display_name or "")
        terms = {label, str(object_type.name or "")}
        terms.update(term for term in common_terms if term in label)
        target = allowed_terms if label in evidence_labels else forbidden_terms
        target.update(term for term in terms if len(term) >= 2)
    conflicts = sorted(term for term in forbidden_terms - allowed_terms if term in narrative)
    if conflicts:
        return f"AI 叙述提及未在本章节查询结果中出现的对象：{'、'.join(conflicts)}"
    return None


def _narrate_sections(db: Session, profile, scope, section_results: list[dict[str, Any]],
                      model_id: Optional[str]) -> str:
    config = select_llm_model_config(db, model_id=model_id or profile.default_model_id)
    call_kwargs = llm_call_kwargs(config)
    if not call_kwargs:
        return "fallback"
    compact = []
    for section in section_results:
        compact.append({
            "id": section["id"], "title": section["title"], "goal": section["goal"],
            "results": [item.get("result") for item in section.get("queries") or []],
        })
    prompt = f"""根据下面由平台只读工具返回的真实数据，为每个报告章节撰写汇报级中文分析。
只输出 JSON 对象：{{"章节id": "120-260字的分析结论"}}。
要求：先给结论，再解释数据；指出异常、集中度或局限；禁止引入输入中没有的数字和事实；
每章只能使用该章自己的 results，严禁引用同一 JSON 中其他章节的数据；不同对象类型之间默认没有关联关系，
除非该章结果明确返回了关系，否则严禁做跨对象归因或因果推断（例如把供应商风险归因到订单延期）；
partial/truncated/scannedCap=true 时必须说明覆盖局限；无数据时明确写“当前数据不足”，不要粉饰。

{json.dumps(compact, ensure_ascii=False, default=str)[:30000]}"""
    try:
        response = llm_bridge.chat(call_kwargs, [
            {"role": "system", "content": "你是为管理层撰写数据分析报告的高级分析师，所有结论必须可由给定数据核对。"},
            {"role": "user", "content": prompt},
        ], tools=[])
        parsed = _extract_json(response.get("content") or "")
        for section in section_results:
            narrative = _clean_text(parsed.get(section["id"]), 3000)
            if narrative:
                issue = _unsupported_object_reference(scope, section, narrative)
                if issue:
                    section["narrativeAudit"] = {"passed": False, "detail": issue,
                                                   "action": "replaced_with_deterministic_summary"}
                    section["narrativeMode"] = "fallback_guarded"
                else:
                    section["narrative"] = narrative
                    section["narrativeAudit"] = {"passed": True, "detail": "未发现越出本章证据边界的对象引用"}
                    section["narrativeMode"] = "ai"
        return "ai"
    except Exception as exc:  # noqa: BLE001
        logger.warning("报告叙述生成失败，使用确定性摘要: %s", exc)
        return "fallback"


def _chart_for(section: dict[str, Any]) -> Optional[dict[str, Any]]:
    visualization = section.get("visualization") or "auto"
    if visualization in {"none", "table"}:
        return None
    if visualization == "kpi":
        data = []
        metric_names = {"count": "数量", "sum": "合计", "avg": "平均值", "min": "最小值", "max": "最大值"}
        for query in section.get("queries") or []:
            result = query.get("result") or {}
            if "value" not in result:
                continue
            metric = metric_names.get(result.get("metric"), result.get("metric") or "指标")
            prop = f" · {result.get('metricProperty')}" if result.get("metricProperty") else ""
            data.append({"label": f"{result.get('objectType') or '指标'} · {metric}{prop}",
                         "value": result.get("value")})
        if data:
            return {"type": "kpi", "title": section["title"], "data": data[:4]}
    for query in section.get("queries") or []:
        result = query.get("result") or {}
        chart = result.get("chart")
        if chart:
            chart = deepcopy(chart)
            if visualization != "auto":
                chart["type"] = visualization
            if chart.get("type") == "pie" and len(chart.get("data") or []) > 6:
                chart["type"] = "bar"
            if chart.get("type") == "line":
                chart["data"] = sorted(chart.get("data") or [], key=lambda item: str(item.get("label") or ""))
            return chart
        groups = result.get("groups")
        if groups and visualization != "auto":
            data = [{"label": str(item.get("group")), "value": item.get("value")}
                    for item in groups if item.get("value") is not None][:20]
            if data:
                ctype = "bar" if visualization == "kpi" else visualization
                if ctype == "pie" and len(data) > 6:
                    ctype = "bar"
                return {"type": ctype, "title": section["title"], "data": data}
    return None


def evaluate_quality(snapshot: dict[str, Any], sections: list[dict[str, Any]]) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    if len(sections) < 2:
        blockers.append("汇报级报告至少需要两个相互补充的分析章节")
    checks.append({"key": "structure", "label": "报告结构", "passed": len(sections) >= 2,
                   "detail": f"共 {len(sections)} 个分析章节"})

    successful = 0
    substantive = 0
    for section in sections:
        title = section.get("title") or "未命名章节"
        queries = section.get("queries") or []
        errors = [q.get("error") for q in queries if q.get("error")]
        real_results = [q.get("result") for q in queries if q.get("result") and not q.get("error")]
        meaningful_results = [result for result in real_results if (
            ("value" in result and result.get("value") is not None)
            or bool(result.get("groups"))
            or bool(result.get("items"))
        )]
        if errors or not real_results:
            blockers.append(f"章节「{title}」没有通过真实数据查询：{errors[0] if errors else '无有效结果'}")
        else:
            successful += 1
        if meaningful_results:
            substantive += 1
        else:
            blockers.append(f"章节「{title}」没有取得可用于分析的真实数据")
        if len((section.get("narrative") or "").strip()) < 20:
            blockers.append(f"章节「{title}」缺少可用于汇报的分析结论")
        for result in real_results:
            if result.get("partial"):
                warnings.append(f"章节「{title}」只基于部分数据计算，报告中必须保留范围说明")
            if result.get("truncated") or result.get("scannedCap"):
                warnings.append(f"章节「{title}」的数据被截断，结论仅代表当前返回范围")
        chart = section.get("chart")
        if (section.get("visualization") not in {"none", "table"}) and not chart:
            warnings.append(f"章节「{title}」当前数据不适合生成有效图表，已保留表格/文字表达")
        if chart and chart.get("type") == "line" and len(chart.get("data") or []) < 4:
            warnings.append(f"章节「{title}」折线图数据点少于 4 个，建议改用指标卡或柱状图")

    checks.append({"key": "live_data", "label": "真实数据试运行", "passed": successful == len(sections),
                   "detail": f"{successful}/{len(sections)} 个章节查询成功"})
    checks.append({"key": "substance", "label": "数据有效性", "passed": substantive == len(sections),
                   "detail": f"{substantive}/{len(sections)} 个章节取得可分析数据"})
    guarded = [s for s in sections if (s.get("narrativeAudit") or {}).get("passed") is False]
    for section in guarded:
        warnings.append(f"章节「{section.get('title')}」的 AI 叙述越出证据边界，已自动改用确定性摘要")
    checks.append({"key": "grounding", "label": "叙述证据边界", "passed": True,
                   "detail": (f"{len(guarded)} 个越界叙述已安全回退" if guarded else "未发现未取数对象的跨域归因")})
    narrative_ok = all(len((s.get("narrative") or "").strip()) >= 20 for s in sections)
    checks.append({"key": "narrative", "label": "结论完整度", "passed": narrative_ok,
                   "detail": "每章均有可核对的分析结论" if narrative_ok else "存在结论过短或缺失的章节"})
    sources_ok = all(bool(s.get("queries")) for s in sections)
    checks.append({"key": "traceability", "label": "数据可追溯", "passed": sources_ok,
                   "detail": "每章保留查询计划与结果快照" if sources_ok else "存在无来源章节"})

    score = max(0, 100 - len(blockers) * 25 - len(warnings) * 5)
    passed = not blockers and score >= 80
    return {
        "passed": passed,
        "score": score,
        "threshold": 80,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "summary": ("已达到汇报级发布标准" if passed
                    else f"未达到发布标准：{len(blockers)} 个阻断项，{len(warnings)} 个提醒"),
        "templateRevision": snapshot.get("revision"),
    }


def execute_report(db: Session, template: AnalysisReportTemplate, current_user,
                   trigger_type: str = "preview", model_id: Optional[str] = None) -> AnalysisReportRun:
    ontology, profile, scope = build_scope(db, template.ontology_id)
    snapshot = template_snapshot(template, ontology.name)
    run = AnalysisReportRun(
        template_id=template.id,
        ontology_id=template.ontology_id,
        created_by=getattr(current_user, "id", None) or template.created_by,
        trigger_type=trigger_type,
        status="running",
        template_revision=template.revision,
        template_snapshot=snapshot,
        started_at=_now(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        sections = normalize_sections(template.sections or [])
        rendered_sections: list[dict[str, Any]] = []
        for section in sections:
            runner = ToolRunner(db, scope)
            queries: list[dict[str, Any]] = []
            for query in section["queryPlan"]:
                tool, arguments = query["tool"], query["arguments"]
                try:
                    result = runner.run(tool, arguments)
                    queries.append({"tool": tool, "arguments": deepcopy(arguments), "result": result})
                except (ToolError, ValueError) as exc:
                    queries.append({"tool": tool, "arguments": deepcopy(arguments), "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    logger.exception("报告查询执行失败: %s", tool)
                    queries.append({"tool": tool, "arguments": deepcopy(arguments),
                                    "error": "查询执行失败，请检查数据服务状态"})
            fallback_narrative = " ".join(
                _result_summary(item.get("result") or {"error": item.get("error")}) for item in queries)
            rendered_sections.append({
                "id": section["id"], "title": section["title"], "goal": section["goal"],
                "visualization": section["visualization"], "queries": queries,
                "citations": deepcopy(runner.citations), "narrative": fallback_narrative,
            })

        narrative_mode = _narrate_sections(db, profile, scope, rendered_sections,
                                            model_id or template.default_model_id)
        for section in rendered_sections:
            section["chart"] = _chart_for(section)
            section.setdefault("narrativeMode", narrative_mode)

        quality = evaluate_quality(snapshot, rendered_sections)
        run.section_results = rendered_sections
        run.quality_report = quality
        run.html_content = render_report_html(snapshot, rendered_sections, quality, run.started_at)
        run.status = "succeeded"
        run.completed_at = _now()
        if trigger_type == "preview":
            template.last_preview_run_id = run.id
            template.last_preview_revision = template.revision
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:  # noqa: BLE001
        logger.exception("报告运行失败")
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.quality_report = {
            "passed": False, "score": 0, "threshold": 80,
            "blockers": [f"报告运行失败：{str(exc)[:500]}"], "warnings": [], "checks": [],
            "summary": "报告运行失败，未生成可发布产物",
            "templateRevision": template.revision,
        }
        run.completed_at = _now()
        db.commit()
        db.refresh(run)
        return run


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _number(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return _e(value)


def _narrative_html(text: str) -> str:
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", text or "") if item.strip()]
    if not paragraphs:
        return '<p class="empty-copy">当前数据不足，尚不能形成可靠结论。</p>'
    return "".join(f"<p>{_e(item).replace(chr(10), '<br>')}</p>" for item in paragraphs)


def _chart_data(chart: dict[str, Any]) -> list[tuple[str, float]]:
    points = []
    for item in chart.get("data") or []:
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        points.append((str(item.get("label") or "未命名"), value))
    return points[:15]


def _bar_chart(chart: dict[str, Any]) -> str:
    points = _chart_data(chart)
    if not points:
        return ""
    max_value = max(abs(value) for _, value in points) or 1
    row_h, left, usable = 48, 178, 470
    height = 42 + len(points) * row_h
    rows = []
    for index, (label, value) in enumerate(points):
        y = 28 + index * row_h
        width = max(3, abs(value) / max_value * usable)
        rows.append(
            f'<text x="0" y="{y + 15}" class="chart-label">{_e(label[:24])}</text>'
            f'<rect x="{left}" y="{y}" width="{usable}" height="20" rx="10" class="bar-track" />'
            f'<rect x="{left}" y="{y}" width="{width:.1f}" height="20" rx="10" class="bar-fill" />'
            f'<text x="{min(left + width + 10, 690):.1f}" y="{y + 15}" class="chart-value">{_number(value)}</text>')
    return (f'<figure class="chart-wrap" aria-label="{_e(chart.get("title") or "柱状图")}">'
            f'<svg viewBox="0 0 720 {height}" role="img">{"".join(rows)}</svg></figure>')


def _line_chart(chart: dict[str, Any]) -> str:
    points = _chart_data(chart)
    if len(points) < 2:
        return _bar_chart({**chart, "type": "bar"})
    width, height, left, top, plot_w, plot_h = 720, 300, 55, 24, 625, 210
    values = [value for _, value in points]
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    coords = []
    for index, (_, value) in enumerate(points):
        x = left + (index / max(1, len(points) - 1)) * plot_w
        y = top + (hi - value) / span * plot_h
        coords.append((x, y))
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    labels = []
    for index, ((label, value), (x, y)) in enumerate(zip(points, coords)):
        if index in {0, len(points) - 1} or len(points) <= 8 or index % 2 == 0:
            labels.append(f'<text x="{x:.1f}" y="{height - 22}" text-anchor="middle" class="axis-label">{_e(label[:10])}</text>')
        labels.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" class="line-point"><title>{_e(label)}: {_number(value)}</title></circle>')
    grids = "".join(
        f'<line x1="{left}" y1="{top + i * plot_h / 4:.1f}" x2="{left + plot_w}" y2="{top + i * plot_h / 4:.1f}" class="grid-line" />'
        for i in range(5))
    return (f'<figure class="chart-wrap" aria-label="{_e(chart.get("title") or "趋势图")}">'
            f'<svg viewBox="0 0 {width} {height}" role="img">{grids}'
            f'<polyline points="{polyline}" class="line-path" />{"".join(labels)}</svg></figure>')


def _pie_chart(chart: dict[str, Any]) -> str:
    points = [(label, value) for label, value in _chart_data(chart) if value >= 0]
    total = sum(value for _, value in points)
    if not points or total <= 0 or len(points) > 6:
        return _bar_chart({**chart, "type": "bar"})
    colors = ["#0f766e", "#2f8f83", "#75b4a9", "#d3a64a", "#bb6b4a", "#6f7f77"]
    stops, cursor = [], 0.0
    legend = []
    for index, (label, value) in enumerate(points):
        end = cursor + value / total * 100
        stops.append(f"{colors[index]} {cursor:.2f}% {end:.2f}%")
        legend.append(
            f'<li><span class="legend-dot" style="background:{colors[index]}"></span>'
            f'<span>{_e(label)}</span><strong>{_number(value)}</strong></li>')
        cursor = end
    return (f'<figure class="chart-wrap pie-layout" aria-label="{_e(chart.get("title") or "占比图")}">'
            f'<div class="donut" style="background:conic-gradient({", ".join(stops)})">'
            f'<div><strong>{_number(total)}</strong><span>总计</span></div></div>'
            f'<ul class="chart-legend">{"".join(legend)}</ul></figure>')


def _chart_html(chart: Optional[dict[str, Any]]) -> str:
    if not chart:
        return ""
    ctype = chart.get("type") or "bar"
    if ctype == "kpi":
        data = chart.get("data") or []
        if not data:
            return ""
        cards = "".join(
            f'<div class="kpi"><span>{_e(item.get("label"))}</span>'
            f'<strong>{_number(item.get("value"))}</strong><small>实时统计结果</small></div>'
            for item in data[:4])
        return f'<div class="kpi-grid">{cards}</div>'
    if ctype in {"line", "area"}:
        return _line_chart(chart)
    if ctype == "pie":
        return _pie_chart(chart)
    return _bar_chart(chart)


def _result_table(section: dict[str, Any]) -> str:
    blocks: list[str] = []
    scalar_rows: list[str] = []
    metric_names = {"count": "数量", "sum": "合计", "avg": "平均值", "min": "最小值", "max": "最大值"}
    for query_index, query in enumerate(section.get("queries") or [], 1):
        result = query.get("result") or {}
        groups = result.get("groups")
        if groups:
            display_groups = list(groups[:15])
            if (result.get("chart") or {}).get("type") == "line":
                display_groups.sort(key=lambda item: str(item.get("group") or ""))
            rows = "".join(
                f'<tr><td>{_e(item.get("group"))}</td><td class="num">{_number(item.get("value"))}</td>'
                f'<td class="num">{_number(item.get("count"))}</td></tr>' for item in display_groups)
            blocks.append('<div class="data-table"><table><caption>图表对应数据表'
                          f' · 查询 {query_index}</caption><thead><tr><th>分组</th><th>指标值</th>'
                          f'<th>样本数</th></tr></thead><tbody>{rows}</tbody></table></div>')
        if "value" in result:
            metric = metric_names.get(result.get("metric"), result.get("metric") or "指标")
            prop = f" · {result.get('metricProperty')}" if result.get("metricProperty") else ""
            scalar_rows.append(
                f'<tr><td>{_e(result.get("objectType"))}</td><td>{_e(metric)}{_e(prop)}</td>'
                f'<td class="num">{_number(result.get("value"))}</td></tr>')
        items = result.get("items")
        if items:
            keys: list[str] = []
            for item in items[:12]:
                for key in (item.get("properties") or {}).keys():
                    if key not in keys and len(keys) < 6:
                        keys.append(key)
            head = "".join(f"<th>{_e(key)}</th>" for key in keys)
            rows = "".join(
                "<tr>" + "".join(f'<td>{_e((item.get("properties") or {}).get(key))}</td>' for key in keys) + "</tr>"
                for item in items[:12])
            blocks.append(f'<div class="data-table"><table><caption>真实数据样本 · 查询 {query_index}'
                          f'（最多展示 12 条）</caption><thead><tr>{head}</tr></thead>'
                          f'<tbody>{rows}</tbody></table></div>')
    if scalar_rows:
        blocks.insert(0, '<div class="data-table"><table><caption>核心指标数据</caption>'
                      '<thead><tr><th>对象</th><th>指标</th><th>结果</th></tr></thead>'
                      f'<tbody>{"".join(scalar_rows)}</tbody></table></div>')
    return "".join(blocks) or '<div class="empty-data">当前没有可展示的数据表。</div>'


def render_report_html(snapshot: dict[str, Any], sections: list[dict[str, Any]],
                       quality: dict[str, Any], generated_at: datetime) -> str:
    generated = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    section_html = []
    for index, section in enumerate(sections, 1):
        source_items = []
        for query in section.get("queries") or []:
            if query.get("error"):
                source_items.append(f'<li class="source-error">{_e(query.get("tool"))}：{_e(query.get("error"))}</li>')
            else:
                source_items.append(
                    f'<li><span>{_e(query.get("tool"))}</span><code>{_e(json.dumps(query.get("arguments") or {}, ensure_ascii=False))}</code></li>')
        limitations = []
        for query in section.get("queries") or []:
            result = query.get("result") or {}
            if result.get("partial"):
                limitations.append("统计触及扫描安全上限，本节按部分数据计算。")
            if result.get("truncated") or result.get("scannedCap"):
                limitations.append("明细返回范围受限，本节结论不代表未返回记录。")
        limitation_html = "" if not limitations else (
            '<aside class="limitation"><strong>数据范围说明</strong>'
            + "".join(f"<p>{_e(item)}</p>" for item in dict.fromkeys(limitations)) + "</aside>")
        section_html.append(f"""
        <section class="report-section">
          <div class="section-index">{index:02d}</div>
          <header><div><span class="eyebrow">ANALYSIS CHAPTER</span><h2>{_e(section['title'])}</h2></div>
            <p class="section-goal">{_e(section['goal'])}</p></header>
          <div class="insight"><span class="insight-label">核心分析</span>{_narrative_html(section.get('narrative') or '')}</div>
          {_chart_html(section.get('chart'))}
          {_result_table(section)}
          {limitation_html}
          <details class="sources"><summary>数据来源与查询口径</summary><ul>{''.join(source_items)}</ul></details>
        </section>""")

    checks = "".join(
        f'<li class="{"ok" if item.get("passed") else "bad"}"><span>{"通过" if item.get("passed") else "未通过"}</span>'
        f'<strong>{_e(item.get("label"))}</strong><small>{_e(item.get("detail"))}</small></li>'
        for item in quality.get("checks") or [])
    warning_items = "".join(f"<li>{_e(item)}</li>" for item in quality.get("warnings") or [])
    first_insights = [s.get("narrative") or "" for s in sections[:3]]
    executive = " ".join(text.split("。", 1)[0] + "。" for text in first_insights if text).strip()
    if not executive:
        executive = "当前真实数据不足，尚不能形成可靠的管理层摘要。"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(snapshot.get('name'))}</title>
<style>
:root{{--paper:#f6f3ec;--surface:#fffefa;--ink:#17211d;--muted:#66736d;--teal:#0f766e;--teal-soft:#dcece7;--gold:#c2933f;--line:#dcd8ce;--danger:#a33d32}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:"Avenir Next","Source Han Sans SC","Noto Sans CJK SC",system-ui,sans-serif;line-height:1.65;-webkit-font-smoothing:antialiased}}
.report-shell{{width:min(1120px,calc(100% - 48px));margin:32px auto 72px}}.cover{{min-height:720px;padding:72px 68px 52px;border-radius:34px;background:var(--surface);box-shadow:0 28px 90px rgba(48,57,52,.10),inset 0 0 0 1px rgba(53,70,63,.08);display:grid;grid-template-columns:minmax(0,1.55fr) minmax(250px,.45fr);gap:70px;position:relative;overflow:hidden;break-after:page}}
.cover:before{{content:"";position:absolute;inset:0;background:radial-gradient(circle at 88% 10%,rgba(15,118,110,.14),transparent 34%),linear-gradient(135deg,transparent 0 62%,rgba(194,147,63,.07));pointer-events:none}}.cover-main,.cover-meta{{position:relative;z-index:1}}.brandline{{display:flex;align-items:center;gap:12px;font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--teal);font-weight:700}}.brandmark{{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:var(--teal);color:white;font-size:14px;letter-spacing:0}}
.cover h1{{font-family:"Iowan Old Style","Songti SC","Noto Serif CJK SC",serif;font-size:clamp(46px,6vw,78px);line-height:1.08;letter-spacing:-.035em;margin:100px 0 28px;max-width:760px;font-weight:650}}.dek{{font-size:18px;color:var(--muted);max-width:660px;margin:0}}.executive{{margin-top:70px;padding-top:24px;border-top:1px solid var(--line)}}.executive span,.eyebrow,.insight-label{{display:block;font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--teal);font-weight:750;margin-bottom:10px}}.executive p{{font-family:"Iowan Old Style","Songti SC",serif;font-size:22px;line-height:1.55;margin:0}}
.cover-meta{{align-self:end;padding:24px;border-radius:24px;background:rgba(246,243,236,.72);box-shadow:inset 0 0 0 1px rgba(23,33,29,.07)}}.meta-row{{padding:14px 0;border-bottom:1px solid rgba(23,33,29,.09)}}.meta-row:last-child{{border:0}}.meta-row span{{display:block;font-size:10px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}}.meta-row strong{{display:block;margin-top:4px;font-size:14px}}.score{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:32px!important;color:var(--teal)}}
.report-section{{position:relative;margin-top:32px;padding:56px 64px 48px;border-radius:30px;background:var(--surface);box-shadow:0 18px 60px rgba(48,57,52,.075),inset 0 0 0 1px rgba(53,70,63,.075);break-inside:avoid}}.section-index{{position:absolute;right:42px;top:34px;font:600 54px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:rgba(15,118,110,.10)}}.report-section header{{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,.7fr);gap:48px;align-items:end;padding-bottom:28px;border-bottom:1px solid var(--line)}}.report-section h2{{font-family:"Iowan Old Style","Songti SC",serif;font-size:36px;line-height:1.2;margin:0;letter-spacing:-.02em}}.section-goal{{margin:0;color:var(--muted);font-size:13px}}
.insight{{margin:34px 0 24px;padding:28px 30px;border-radius:22px;background:#f0f5f2;box-shadow:inset 0 0 0 1px rgba(15,118,110,.09)}}.insight p{{font-size:16px;margin:0 0 12px}}.insight p:last-child{{margin-bottom:0}}.empty-copy{{color:var(--muted)}}
.kpi-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:24px 0}}.kpi{{min-height:150px;padding:28px 30px;border-radius:22px;background:linear-gradient(135deg,#0d625c,#0f766e);color:white;display:flex;flex-direction:column;justify-content:space-between}}.kpi span{{font-size:12px;opacity:.78}}.kpi strong{{margin:18px 0 8px;font:650 clamp(34px,4vw,50px)/1 ui-monospace,SFMono-Regular,Menlo,monospace}}.kpi small{{font-size:10px;opacity:.66}}
.chart-wrap{{margin:26px 0 20px;padding:24px;border-radius:22px;background:#faf9f5;box-shadow:inset 0 0 0 1px rgba(23,33,29,.075);overflow:hidden}}.chart-wrap svg{{width:100%;height:auto;display:block}}.chart-label{{font-size:12px;fill:#4d5b55}}.chart-value{{font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace;fill:#17211d}}.bar-track{{fill:#e6e9e4}}.bar-fill{{fill:#168378}}.grid-line{{stroke:#e2e3dd;stroke-width:1}}.line-path{{fill:none;stroke:#0f766e;stroke-width:4;stroke-linecap:round;stroke-linejoin:round}}.line-point{{fill:#fff;stroke:#0f766e;stroke-width:3}}.axis-label{{font-size:10px;fill:#66736d}}.pie-layout{{display:grid;grid-template-columns:220px 1fr;gap:34px;align-items:center}}.donut{{width:210px;height:210px;border-radius:50%;display:grid;place-items:center;position:relative}}.donut:after{{content:"";position:absolute;width:116px;height:116px;border-radius:50%;background:#faf9f5}}.donut div{{position:relative;z-index:1;text-align:center}}.donut strong{{display:block;font:650 24px ui-monospace,SFMono-Regular,Menlo,monospace}}.donut span{{font-size:10px;color:var(--muted)}}.chart-legend{{list-style:none;margin:0;padding:0}}.chart-legend li{{display:grid;grid-template-columns:12px 1fr auto;gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid var(--line);font-size:12px}}.legend-dot{{width:9px;height:9px;border-radius:50%}}
.data-table{{margin-top:20px;overflow:auto;border-radius:18px;box-shadow:inset 0 0 0 1px rgba(23,33,29,.09)}}table{{width:100%;border-collapse:collapse;font-size:12px}}caption{{text-align:left;padding:14px 16px 8px;color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase}}th,td{{padding:12px 16px;text-align:left;border-bottom:1px solid #ebe8df;vertical-align:top}}th{{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);background:#f8f6f0}}tr:last-child td{{border-bottom:0}}.num{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}}.empty-data{{padding:22px;border-radius:18px;background:#f8f6f0;color:var(--muted);font-size:12px}}
.limitation{{margin-top:20px;padding:16px 18px;border-radius:16px;background:#fff6df;color:#745715;font-size:12px}}.limitation p{{margin:4px 0 0}}.sources{{margin-top:22px;padding-top:18px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}}.sources summary{{cursor:pointer;font-weight:650;color:#46534d}}.sources ul{{padding-left:18px}}.sources li{{margin:7px 0}}.sources code{{margin-left:8px;white-space:pre-wrap;word-break:break-word;color:#596760}}.source-error{{color:var(--danger)}}
.quality{{margin-top:32px;padding:44px 56px;border-radius:30px;background:#15221e;color:#f5f4ef;break-inside:avoid}}.quality header{{display:flex;align-items:end;justify-content:space-between;gap:24px}}.quality h2{{font-family:"Iowan Old Style","Songti SC",serif;font-size:34px;margin:0}}.quality header strong{{font:650 46px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:#88c9bd}}.quality-checks{{list-style:none;margin:28px 0 0;padding:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}.quality-checks li{{padding:18px;border-radius:16px;background:rgba(255,255,255,.055);display:grid;grid-template-columns:auto 1fr;gap:4px 10px}}.quality-checks li>span{{grid-row:1/3;font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#8bd0c4}}.quality-checks li.bad>span{{color:#f2a497}}.quality-checks small{{color:#aeb8b3}}.warnings{{margin-top:22px;color:#e7c987;font-size:12px}}.footer{{padding:24px 8px;text-align:center;color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase}}
@media(max-width:760px){{.report-shell{{width:calc(100% - 24px);margin:12px auto 36px}}.cover{{min-height:auto;padding:40px 28px;grid-template-columns:1fr;gap:40px;border-radius:24px}}.cover h1{{margin-top:62px;font-size:44px}}.cover-meta{{align-self:auto}}.report-section{{padding:38px 24px;border-radius:24px}}.report-section header{{grid-template-columns:1fr;gap:14px}}.report-section h2{{font-size:30px}}.section-index{{right:22px;font-size:40px}}.kpi-grid{{grid-template-columns:1fr}}.pie-layout{{grid-template-columns:1fr;justify-items:center}}.quality{{padding:36px 24px}}.quality-checks{{grid-template-columns:1fr}}}}
@media print{{@page{{size:A4;margin:13mm}}body{{background:white}}.report-shell{{width:auto;margin:0}}.cover,.report-section,.quality{{box-shadow:none;border:1px solid #dedbd2;border-radius:0;margin:0 0 10mm}}.cover{{min-height:255mm;padding:20mm 15mm;break-after:page}}.report-section{{padding:13mm 12mm;break-inside:avoid}}.quality{{background:white;color:var(--ink);padding:12mm}}.quality-checks li{{border:1px solid #dedbd2}}.quality-checks small{{color:var(--muted)}}details{{display:block}}details>summary{{display:none}}}}
</style></head><body><main class="report-shell">
<section class="cover"><div class="cover-main"><div class="brandline"><span class="brandmark">O</span>OpenOntology · Intelligence Report</div>
<h1>{_e(snapshot.get('name'))}</h1><p class="dek">{_e(snapshot.get('description'))}</p>
<div class="executive"><span>Executive readout</span><p>{_e(executive[:650])}</p></div></div>
<aside class="cover-meta"><div class="meta-row"><span>Ontology</span><strong>{_e(snapshot.get('ontologyName'))}</strong></div>
<div class="meta-row"><span>Generated at</span><strong>{_e(generated)}</strong></div><div class="meta-row"><span>Template revision</span><strong>R{_number(snapshot.get('revision'))}</strong></div>
<div class="meta-row"><span>Quality score</span><strong class="score">{_number(quality.get('score'))}/100</strong></div></aside></section>
{''.join(section_html)}
<section class="quality"><header><div><span class="eyebrow">QUALITY CONTROL</span><h2>发布质量检查</h2></div><strong>{_number(quality.get('score'))}</strong></header>
<ul class="quality-checks">{checks}</ul>{f'<ul class="warnings">{warning_items}</ul>' if warning_items else ''}</section>
<footer class="footer">Generated by OpenOntology · Data is bound to the recorded query snapshot</footer>
</main></body></html>"""
