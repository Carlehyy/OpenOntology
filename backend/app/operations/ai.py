"""运营报告 AI 设计候选：只返回受限的结构化计划，不直接写文章。"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.data_channel.query.contracts import QueryError
from app.model_configs.llm_gateway import LLMError, chat
from app.model_configs.selector import llm_call_kwargs, select_llm_model_config
from app.operations import plan as report_plan
from app.operations.service import _require_dataset, _require_report, _write_required, plan_field_types


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def _extract_json(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    fenced = _JSON_FENCE.findall(text)
    if fenced:
        text = fenced[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型返回的计划不是对象")
    # 允许模型把计划包在 plan 字段中，但不接受其它任意格式。
    if isinstance(value.get("plan"), dict):
        payload = value["plan"]
        explanation = value.get("explanation")
        if explanation is not None:
            payload = {**payload, "_explanation": str(explanation)[:2000]}
        return payload
    return value


def design_plan(
    db: Session,
    report_id: str,
    user,
    *,
    dataset_id: str,
    brief: str,
    model_id: str | None = None,
    current_plan: report_plan.ReportPlan | None = None,
) -> dict[str, Any]:
    """生成候选计划；不创建文章、不更新任何持久化正文。"""
    _write_required(user)
    _require_report(db, report_id)
    dataset = _require_dataset(db, dataset_id)
    clean_brief = str(brief or "").strip()
    if not clean_brief:
        raise QueryError(422, "report_ai_brief_required", "请先描述想通过报告了解什么")

    config = select_llm_model_config(db, model_id=model_id)
    call_kwargs = llm_call_kwargs(config)
    if not call_kwargs:
        raise QueryError(
            503,
            "report_ai_unavailable",
            "尚未配置可用的 LLM，请先到模型配置添加对话模型",
        )

    field_types = plan_field_types(db, dataset)
    schema = [{"name": name, "type": field_type} for name, field_type in field_types.items()]
    previous = current_plan.model_dump(mode="json") if current_plan is not None else None
    prompt = f"""为运营报告创建一个可验证的结构化数据计划。

用户目标：
{clean_brief[:4000]}

数据集字段（只能使用这些字段）：
{json.dumps(schema, ensure_ascii=False)}

当前计划（可选，修改时只提出必要变更）：
{json.dumps(previous, ensure_ascii=False) if previous else 'null'}

只输出一个 JSON 对象，禁止 Markdown 代码围栏。格式必须是：
{{
  "version": 1,
  "filters": [{{"name":"region","type":"text","field":"region"}}],
  "blocks": [
    {{"type":"text","text":"本节要回答的业务问题"}},
    {{"type":"query","name":"by_region","columns":[
      {{"field":"region","alias":"region"}},
      {{"agg":"count","alias":"count"}}
    ],"sort":[{{"field":"count","dir":"desc"}}],"limit":20}},
    {{"type":"chart","chart":"bar","query":"by_region","x":"region","y":["count"]}}
  ]
}}

约束：
1. 只能使用 version=1、text/query/chart 三类块；不能返回 sql、html、javascript、echarts option 或自由字段。
2. query.columns 中普通列必须有 field；聚合只能是 count/count_distinct/sum/avg/min/max，sum/avg 只能用于数值字段。
3. 图表只能是 kpi/bar/line/pie/table，并且引用真实 query 输出列；pie/kpi 恰好一个 y。
4. 筛选器的 name 必须是短标识符，field 必须来自字段清单；不要臆造字段、数字或业务事实。
5. 每个输出列显式给出唯一 alias：只能使用 ASCII 字母、数字、下划线，以字母或下划线开头，最多 {report_plan.ALIAS_MAX_BYTES} 个字符。中文字段保留原 field，alias 使用英文短名。图表 x/y 和排序 field 必须引用这些输出别名，不能引用原字段名。
6. 时间趋势可在 query 内添加 time_bucket: {{"field":"真实时间字段","unit":"day","alias":"period"}}，unit 只能为 minute/hour/day/week/month，且 field 必须是日期或时间类型。时间桶输出别名同样必须满足 ASCII 规则，图表 x 与排序引用该 alias。
7. limit 为 1 到 {report_plan.QUERY_LIMIT_MAX} 的整数；排序 sort 是 {{"field":"输出别名","dir":"asc"}} 数组，dir 只能为 asc/desc，最多 {report_plan.SORT_KEYS_MAX} 项；图表 y 最多 {report_plan.CHART_SERIES_MAX} 项。table 不配置 x/y；kpi 配置一个 y；bar/line/pie 配置 x 和有效 y。
8. 生成 1 到 8 个有互补关系的块，优先给出总量、结构/趋势和明细中的适用部分；没有合适字段时使用 text 说明。text 只写待回答的问题或阅读指引，未试跑前不写具体数值结论。
"""
    try:
        response = chat(
            call_kwargs,
            [
                {
                    "role": "system",
                    "content": "你是严谨的企业运营报告设计师，只生成字段受限、可重复试跑的 JSON 报告计划。",
                },
                {"role": "user", "content": prompt},
            ],
            tools=[],
        )
        payload = _extract_json(response.get("content") or "")
        explanation = str(payload.pop("_explanation", "")).strip() or "AI 已生成候选计划，请先试跑并核对字段、聚合和结果。"
        candidate = report_plan.ReportPlan.model_validate(payload)
        normalized = report_plan.normalize_plan(candidate, field_types=field_types)
    except QueryError:
        raise
    except report_plan.PlanValidationError as exc:
        # 候选未通过字段/结构校验：保留具体问题列表，操作 API 层负责呈现文本。
        raise QueryError(
            422,
            "report_ai_invalid_candidate",
            "AI 返回的报告计划未通过字段和结构校验，请调整描述后重试",
            errors=[str(item)[:200] for item in exc.errors][:20],
        ) from exc
    except ValidationError as exc:
        raise QueryError(
            422,
            "report_ai_invalid_candidate",
            "AI 返回的报告计划未通过字段和结构校验，请调整描述后重试",
            errors=[f"{'.'.join(str(loc) for loc in item.get('loc', ())) or 'plan'}: "
                    f"{str(item.get('msg', ''))[:200]}"
                    for item in exc.errors()][:20],
        ) from exc
    except (ValueError, TypeError) as exc:
        raise QueryError(
            422,
            "report_ai_invalid_candidate",
            "AI 返回的报告计划未通过字段和结构校验，请调整描述后重试",
            errors=[str(exc)[:200]],
        ) from exc
    except LLMError as exc:
        # 提供方故障不是候选问题：对外稳定 503，不回显提供方内部信息。
        raise QueryError(503, "report_ai_failed", "AI 报告设计暂时不可用，请稍后重试") from exc
    except Exception as exc:  # noqa: BLE001 - provider/schema failures have stable public error
        raise QueryError(503, "report_ai_failed", "AI 报告设计暂时不可用，请稍后重试") from exc

    return {
        "plan": normalized,
        "explanation": explanation,
        "generation_mode": "ai",
        "dataset": {
            "id": dataset.id,
            "name": dataset.name,
            "kind": dataset.kind,
            "fields": schema,
        },
    }
