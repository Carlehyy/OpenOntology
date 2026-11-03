"""聚合图表 spec 的叶子构造器 — 无业务重依赖，toolkit/查询工具共用。

图表数字全部来自真实分组结果；本模块只做确定性的类型推断和标签拼装。
"""
from __future__ import annotations

from typing import Optional

from app.ontologies.agent_runtime.limits import limit as runtime_limit


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
