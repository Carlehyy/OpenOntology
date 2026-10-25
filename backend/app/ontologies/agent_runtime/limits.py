"""
统一限额注册表 (Agent Runtime Limits) — 借鉴 DataFoundry 的
``agent-runtime-limits.ts``：所有数值限额集中在单一注册表，
每项带 default/min/max/env/description，运行时可经环境变量覆盖。
profile（fo_agent_profiles）只暴露治理项；本模块是全部魔法数字的单一事实源。

新增限额必须在这里登记，禁止在工作流代码里引入未解释的数字阈值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRuntimeLimit:
    key: str
    default: int
    min: int
    max: int
    env: str
    description: str


# 默认值与原实现常量保持完全一致：本次重构零行为变化。
_LIMITS: tuple[AgentRuntimeLimit, ...] = (
    AgentRuntimeLimit(
        "tool_result_display_cap", 6000, 1000, 200_000,
        "AGENT_TOOL_RESULT_DISPLAY_CAP",
        "推送/持久化给前端展示的单个工具结果长度上限（字符）"),
    AgentRuntimeLimit(
        "tool_result_llm_cap", 8000, 1000, 200_000,
        "AGENT_TOOL_RESULT_LLM_CAP",
        "回填给 LLM 的单个工具结果长度上限（字符）"),
    AgentRuntimeLimit(
        "graph_display_cap", 18000, 2000, 500_000,
        "AGENT_GRAPH_DISPLAY_CAP",
        "图谱类步骤保留可渲染结构的单步持久化体积上限（字符）"),
    AgentRuntimeLimit(
        "history_verbatim", 10, 1, 100,
        "AGENT_HISTORY_VERBATIM",
        "逐字带入 LLM 的最近消息条数"),
    AgentRuntimeLimit(
        "history_digest_scan", 40, 5, 1000,
        "AGENT_HISTORY_DIGEST_SCAN",
        "「对话回顾」摘要回溯的最近消息条数（须 ≥ history_verbatim）"),
    AgentRuntimeLimit(
        "search_scan_cap", 5000, 100, 1_000_000,
        "AGENT_SEARCH_SCAN_CAP",
        "非PostgreSQL检索输入预算；超出显式拒绝，PG由SQL资源预算控制"),
    AgentRuntimeLimit(
        "aggregate_scan_cap", 100_000, 1000, 10_000_000,
        "AGENT_AGGREGATE_SCAN_CAP",
        "非PostgreSQL聚合输入预算；超出显式拒绝，PG全量精确SQL聚合不受此行数帽限制"),
    AgentRuntimeLimit(
        "citation_cap", 20, 1, 200,
        "AGENT_CITATION_CAP",
        "一次回答最多引用的实例数"),
    AgentRuntimeLimit(
        "snippet_fields", 3, 1, 10,
        "AGENT_SNIPPET_FIELDS",
        "引用卡片附带的属性摘要字段数"),
    AgentRuntimeLimit(
        "answer_verify_retries", 1, 0, 3,
        "AGENT_ANSWER_VERIFY_RETRIES",
        "确定性结论校验失败时允许的修正回环次数"),
    AgentRuntimeLimit(
        "answer_verify_unverified_floor", 0, 0, 100,
        "AGENT_ANSWER_VERIFY_UNVERIFIED_FLOOR",
        "未验证数字超过此阈值才触发修正/标注（0=发现即处理）"),
    AgentRuntimeLimit(
        "value_trunc", 200, 32, 100_000,
        "AGENT_VALUE_TRUNC",
        "单个属性值的输出截断长度（字符）"),
    AgentRuntimeLimit(
        "chart_point_cap", 20, 1, 200,
        "AGENT_CHART_POINT_CAP",
        "聚合图表最多展示的数据点（按值降序取前 N）"),
    AgentRuntimeLimit(
        "max_hops", 5, 1, 20,
        "AGENT_MAX_HOPS",
        "单次多跳遍历最多跳数"),
    AgentRuntimeLimit(
        "hop_fanout_cap", 200, 1, 10_000,
        "AGENT_HOP_FANOUT_CAP",
        "每个节点每一跳最多考察的链接数"),
    AgentRuntimeLimit(
        "frontier_cap", 500, 1, 100_000,
        "AGENT_FRONTIER_CAP",
        "每一跳最多展开的前沿节点数"),
    AgentRuntimeLimit(
        "path_node_budget", 2000, 10, 1_000_000,
        "AGENT_PATH_NODE_BUDGET",
        "一次多跳最多访问的节点总数"),
    AgentRuntimeLimit(
        "world_model_list_cap", 20, 1, 200,
        "AGENT_WORLD_MODEL_LIST_CAP",
        "list_world_model_services 单次返回的可用/不可用服务数上限"),
    AgentRuntimeLimit(
        "world_model_example_chars", 2000, 200, 100_000,
        "AGENT_WORLD_MODEL_EXAMPLE_CHARS",
        "推演服务示例入参（exampleInput）展示的字符上限"),
    AgentRuntimeLimit(
        "world_model_invoke_per_turn", 2, 1, 20,
        "AGENT_WORLD_MODEL_INVOKE_PER_TURN",
        "单回合最多调用的世界模型推演次数（内核执行成本护栏）"),
    AgentRuntimeLimit(
        "world_model_context_chars", 50_000, 1000, 200_000,
        "AGENT_WORLD_MODEL_CONTEXT_CHARS",
        "推演入参 context+actions 序列化后的字符上限（世界模型侧另有 100K 硬顶）"),
)

_LIMIT_BY_KEY: dict[str, AgentRuntimeLimit] = {item.key: item for item in _LIMITS}


def limit(key: str) -> int:
    """读取限额：环境变量可覆盖（超界自动夹取到 min/max），否则用默认值。"""
    item = _LIMIT_BY_KEY[key]
    raw = os.getenv(item.env)
    if raw is None or not raw.strip():
        return item.default
    try:
        value = int(raw)
    except ValueError:
        return item.default
    return max(item.min, min(item.max, value))


def limit_summary() -> dict[str, dict]:
    """注册表快照，供运维/监控展示（key → 默认值/边界/环境变量/说明）。"""
    return {
        item.key: {
            "default": item.default,
            "min": item.min,
            "max": item.max,
            "env": item.env,
            "description": item.description,
            "effective": limit(item.key),
        }
        for item in _LIMITS
    }
