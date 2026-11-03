"""Structured tool display and audit projections."""
from __future__ import annotations

import json

from app.ontologies.agent_runtime.limits import limit

def _summarize(name: str, result: dict) -> str:
    """步骤的一句话摘要，给前端时间线用。"""
    if "error" in result:
        return str(result["error"])[:120]
    if name == "search_objects":
        return f"{result.get('objectType', '')} 命中 {result.get('total', 0)} 条，返回 {result.get('returned', 0)} 条"
    if name == "get_object":
        return f"{result.get('objectType', '')} · {len(result.get('links', []))} 组关联"
    if name == "traverse_links":
        return f"{result.get('from', '')} —{result.get('linkType', '')}→ {result.get('returned', 0)} 个对象"
    if name == "traverse_path":
        return f"{result.get('from', '')} 经 {len(result.get('path') or [])} 跳 → {result.get('returned', 0)} 个终点对象"
    if name == "find_paths":
        return f"{result.get('sourceLabel', '')} → {result.get('targetLabel', '')}，找到 {len(result.get('paths') or [])} 条路径"
    if name == "analyze_change_impact":
        summary = result.get("summary") or {}
        return f"关联影响预演：直接 {summary.get('direct', 0)}，间接 {summary.get('indirect', 0)}"
    if name == "aggregate_objects":
        if "groups" in result:
            return f"{result.get('objectType', '')} 按 {result.get('groupBy')} 分 {len(result['groups'])} 组"
        return f"{result.get('objectType', '')} {result.get('metric')} = {result.get('value')}"
    if name == "query_objects":
        suffix = "（有下一页）" if result.get("hasNext") else ""
        return (f"{result.get('objectType', '')} 查询返回 "
                f"{result.get('returned', 0)} 行{suffix}")
    if name == "get_data_time_range":
        return (f"{result.get('objectType', '')} 数据范围 "
                f"{result.get('min')} ~ {result.get('max')}")
    if name == "get_object_history":
        return f"{result.get('instance', '')} 的 {len(result.get('facts', []))} 条事实"
    if name == "trace_causal_chain":
        summary = result.get("summary") or {}
        return (f"因果链：{summary.get('factNodes', 0)} 条事实 · "
                f"{summary.get('actionNodes', 0)} 次动作 · "
                f"{summary.get('decisionNodes', 0)} 条决策"
                + ("（部分链）" if result.get("truncated") else ""))
    if name == "explain_sentinel_firing":
        sentinel = result.get("sentinel") or {}
        firing = result.get("firing") or {}
        limits = result.get("explanationLimits") or {}
        return (f"哨兵「{sentinel.get('displayName', '')}」触发解释"
                f"（{firing.get('status')}，展开 {len(result.get('matchedTuples', []))}"
                f"/{limits.get('totalMatches', 0)} 个命中）")
    if name == "list_actions":
        return f"{len(result.get('actions', []))} 个可用动作"
    if name == "run_decision_simulation":
        return (f"决策推演完成：{result.get('perspectiveCount', 0)} 个视角，"
                f"建议「{result.get('recommendedOption') or '待复核'}」")
    if name == "list_world_model_services":
        return (f"世界模型推演服务：{len(result.get('available', []))} 个可用、"
                f"{len(result.get('blocked', []))} 个不可用")
    if name == "run_world_model_simulation":
        return (f"世界模型推演「{result.get('serviceName', '')}」"
                f"{'完成' if result.get('ok') else '失败'}（{result.get('durationMs', 0)}ms）")
    if name == "list_dynamic_sentinels":
        return f"{len(result.get('sentinels', []))} 个助手动态哨兵"
    if name == "propose_dynamic_sentinel_change":
        p = result.get("proposal") or {}
        return f"动态哨兵{p.get('operation', '')}提案：{p.get('sentinelName', '')}"
    if name == "propose_action":
        p = result.get("proposal") or {}
        ok = p.get("status") == "success"
        return f"提案「{p.get('actionName', '')}」预演{'通过' if ok else '未通过'}（{len(p.get('effects') or [])} 项效果）"
    return "完成"


def _compact_graph_node(node: dict) -> dict:
    """移除仅用于详情面板的重字段，保留图谱渲染和节点定位所需字段。"""
    keys = (
        "id", "entityId", "kind", "label", "secondaryLabel", "objectTypeId",
        "objectTypeLabel", "instanceId", "propertyName", "propertyType", "value",
    )
    return {key: node[key] for key in keys if key in node}


def _compact_graph_edge(edge: dict) -> dict:
    keys = ("id", "entityId", "kind", "source", "target", "label", "linkTypeId")
    return {key: edge[key] for key in keys if key in edge}


def _compact_path(path: dict) -> dict:
    keys = ("nodeIds", "edgeIds", "hops")
    return {key: path[key] for key in keys if key in path}


def _graph_display_result(result: dict) -> dict:
    """为助手联动保留可解析图结构，同时按相关性限制消息与数据库体积。"""
    node_limit, edge_limit, impact_limit = 80, 120, 60
    compact = {key: value for key, value in result.items()
               if key not in {"nodes", "edges", "paths", "impacts"}}
    compact["nodes"] = [_compact_graph_node(node)
                        for node in (result.get("nodes") or [])[:node_limit]]
    compact["edges"] = [_compact_graph_edge(edge)
                        for edge in (result.get("edges") or [])[:edge_limit]]
    if "paths" in result:
        compact["paths"] = [_compact_path(path) for path in (result.get("paths") or [])[:5]]
    if "impacts" in result:
        impacts = []
        for item in (result.get("impacts") or [])[:impact_limit]:
            retained = {key: item[key] for key in (
                "instanceId", "label", "objectType", "depth", "classification", "certainty",
            ) if key in item}
            retained["path"] = _compact_path(item.get("path") or {})
            impacts.append(retained)
        compact["impacts"] = impacts

    original_counts = {
        "nodes": len(result.get("nodes") or []),
        "edges": len(result.get("edges") or []),
        "impacts": len(result.get("impacts") or []),
    }
    displayed_counts = {
        "nodes": len(compact["nodes"]),
        "edges": len(compact["edges"]),
        "impacts": len(compact.get("impacts") or []),
    }
    compact["visualizationTruncated"] = original_counts != displayed_counts
    compact["visualizationCounts"] = {
        "available": original_counts,
        "displayed": displayed_counts,
    }

    # 极密图谱继续按层级收紧，但始终保留合法、可直接渲染的 JSON，而不是字符串预览。
    payload = json.dumps(compact, ensure_ascii=False, default=str)
    if len(payload) > limit("graph_display_cap"):
        compact["nodes"] = compact["nodes"][:40]
        compact["edges"] = compact["edges"][:60]
        if "impacts" in compact:
            compact["impacts"] = compact["impacts"][:30]
        compact["visualizationTruncated"] = True
        compact["visualizationCounts"]["displayed"] = {
            "nodes": len(compact["nodes"]),
            "edges": len(compact["edges"]),
            "impacts": len(compact.get("impacts") or []),
        }
    if len(json.dumps(compact, ensure_ascii=False, default=str)) > limit("graph_display_cap"):
        compact["nodes"] = compact["nodes"][:32]
        compact["edges"] = compact["edges"][:48]
        if "impacts" in compact:
            compact["impacts"] = compact["impacts"][:24]
        compact["visualizationCounts"]["displayed"] = {
            "nodes": len(compact["nodes"]),
            "edges": len(compact["edges"]),
            "impacts": len(compact.get("impacts") or []),
        }
    return compact


def _display_result(result: dict):
    """给前端展示查询描述与有界结果，远程明细由查询视图实时读取。"""
    if result.get("querySpec"):
        from app.ontologies.agent_runtime.query_spec import public_query_result
        return public_query_result(result)
    cap = limit("tool_result_display_cap")
    try:
        payload = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return {"_note": "结果无法序列化展示"}
    if len(payload) <= cap:
        return result
    if result.get("kind") in {"path", "impact"}:
        return _graph_display_result(result)
    return {
        "_truncated": True,
        "_note": f"结果较大（约 {len(payload)} 字符），此处仅展示前 {cap} 字符预览。",
        "preview": payload[:cap],
    }


def _audit_result(result: dict):
    """规范化审计输出；远程明细仅保留可重新授权的查询描述。"""
    if result.get("querySpec") and "remote" in str(result.get("dataMode", "")):
        from app.ontologies.agent_runtime.query_spec import public_query_result
        result = public_query_result(result)
    try:
        return json.loads(json.dumps(result, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return {"_serializationError": "工具结果无法序列化"}
