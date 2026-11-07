"""Bounded ontology discovery, conversation context and structured tool previews."""
from __future__ import annotations

import json
import re
from typing import Any

from app.ontologies.agent_runtime.boundary import remote_object_type_ids
from app.ontologies.agent_runtime.limits import limit

# 模型不需要的传输/展示字段：只在结果顶层剥离。业务 rows/items 由本体定义，
# 其中同名属性必须原样保留。
_TOP_LEVEL_HIDDEN_KEYS = frozenset({
    "querySpec", "querySpecHash", "moduleQuerySpec", "moduleQuerySpecHash",
    "chart", "chartRendered", "drilldown", "html", "htmlContent",
    "templateSnapshot",
})
_PROTECTED_LIST_KEYS = frozenset({"columns", "groupBy", "groupBuckets"})
_TOP_ROW_KEYS = frozenset({
    "groups", "rows", "groupValues", "items", "results", "data", "ranking",
    "top", "modules", "sampleWindows",
})
_TOP_N = 10
_MONTH_N = 12
_MONTH_VALUE_RE = re.compile(
    r"(?<!\d)(?:(?:19|20)\d{2}\s*[-/年]\s*\d{1,2}\s*月?|\d{1,2}\s*月|Q[1-4])",
    re.IGNORECASE,
)


def ontology_catalog(scope) -> str:
    remote_ids = remote_object_type_ids(scope)
    budget = min(16_000, limit("context_chars_cap") // 3)
    lines = [f"# 本体「{scope.ontology.name}」授权目录", f"releaseId={scope.release_id}"]
    used = sum(map(len, lines))
    types = list(scope.object_types.values())
    shown = 0
    for item in types:
        allowed = scope.allowed_property_names(item.id)
        queryable = scope.queryable_property_names(item)
        props = [p for p in (item.properties or []) if isinstance(p, dict)
                 and p.get("name") and (allowed is None or p["name"] in allowed)
                 and (queryable is None or p["name"] in queryable)]
        fields = ", ".join(f"{p['name']}:{p.get('type', 'string')}" for p in props[:6])
        mode = "远程绑定" if item.id in remote_ids else "本地物化"
        line = f"- {item.display_name} ({item.name}, id={item.id}, {mode}): {fields}"
        if len(props) > 6:
            line += f"；另有 {len(props) - 6} 个授权属性，先 describe_objects"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
        shown += 1
    if shown < len(types):
        lines.append(f"目录仅展示 {shown}/{len(types)} 类；用 describe_objects 分页发现其余类型。")
    lines.append("关系、完整属性、可用动作通过 describe_objects 按需发现；目录不声明实例数量。")
    return "\n".join(lines)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _without_drilldown(item: Any) -> Any:
    """只移除统计行里已知的签名下钻对象；业务同名字段保持原样。"""
    value = item.get("drilldown") if isinstance(item, dict) else None
    if isinstance(value, dict) and "token" in value:
        item = dict(item)
        item.pop("drilldown", None)
    return item


def _strip_transport(result: dict) -> dict:
    """剥离顶层传输元数据；rows/items 业务内容不递归改写。"""
    stripped = {key: value for key, value in result.items()
                if key not in _TOP_LEVEL_HIDDEN_KEYS}
    if result.get("groupBy") is not None or result.get("groupValues") is not None:
        for key in ("rows", "groups"):
            value = stripped.get(key)
            if isinstance(value, list):
                stripped[key] = [_without_drilldown(item) for item in value]
    samples = stripped.get("sampleWindows")
    if isinstance(samples, list):
        # window_distinct 的样本行带签名下钻；只清理已知 token 对象。
        stripped["sampleWindows"] = [_without_drilldown(item) for item in samples]
    return stripped


def _monthly_result(result: dict) -> bool:
    buckets = result.get("groupBuckets")
    if isinstance(buckets, list) and any(str(item).lower() == "month" for item in buckets):
        return True
    group_by = result.get("groupBy")
    names = group_by if isinstance(group_by, list) else [group_by]
    for name in names:
        if isinstance(name, str) and ("(月)" in name or "month" in name.lower()):
            return True
    sample = result.get("groups") or result.get("rows") or []
    for item in sample[:3]:
        label = item.get("group") if isinstance(item, dict) else item
        if isinstance(label, str) and _MONTH_VALUE_RE.search(label):
            return True
    return False


def _list_limit(key: str, result: dict) -> int | None:
    if key in _PROTECTED_LIST_KEYS:
        return None
    lowered = key.lower()
    temporal_key = any(token in lowered for token in ("month", "trend", "series", "bucket"))
    row_key = (key in _TOP_ROW_KEYS
               or any(token in lowered for token in
                      ("group", "row", "item", "result", "data", "rank", "top")))
    if temporal_key or (row_key and _monthly_result(result)):
        return _MONTH_N
    if row_key:
        return _TOP_N
    return 5


def _trim_lists(preview: dict, result: dict, *, compact: bool,
                primary: str | None = None) -> dict:
    """按层级收紧顶层列表；压缩层优先保住 Top10/月度 12 行。"""
    omitted: dict[str, int] = dict(preview.get("omittedItems") or {})
    for key, value in list(preview.items()):
        if not isinstance(value, list):
            if (compact and isinstance(value, str) and len(value) > 512
                    and not key.lower().endswith(("cursor", "token", "hash"))):
                preview[key] = value[:512]
            continue
        if key in _PROTECTED_LIST_KEYS:
            continue
        if compact:
            keep = _list_limit(key, result) if key == primary else 1
        else:
            keep = _list_limit(key, result)
        if keep is not None and len(value) > keep:
            omitted[key] = omitted.get(key, 0) + len(value) - keep
            preview[key] = value[:keep]
    if omitted:
        preview["llmPreviewTruncated"] = True
        preview["llmPreviewOmitted"] = True
        preview["omittedItems"] = omitted
    return preview


def _metadata(result: dict) -> dict:
    keys = (
        "kind", "objectType", "metric", "metricProperty", "mode",
        "returned", "total", "totalRowCount", "rowCounting", "totalGroups",
        "truncated", "hasNext", "cursor", "exact", "partial", "completeness",
        "completenessScope", "dataCoverage", "resolvedWindow", "observedAt",
        "queryId", "error", "value", "groupBy", "window",
        "windowMinutes", "windowBucket", "windowTimezone", "timezone",
        "windowAlignment", "threshold", "thresholdScope",
        "subjectProperty", "timeProperty", "qualifyingWindows",
        "qualifyingModules", "moduleCount", "moduleLimit", "modulesTruncated",
        "sampleWindowsTruncated", "evaluatedWindows",
        "evaluatedWindowsAvailable", "limitations", "note", "consistency",
        "llmPreviewTruncated", "llmPreviewOmitted", "omittedItems",
    )
    return {key: result[key] for key in keys if key in result}


def tool_payload(result: dict, *, cap: int | None = None) -> str:
    """Bounded model preview; truncation never changes query completeness.

    先剥离模型不需要的查询计划/下钻签名/图表副本；超出预算时优先保留
    Top10 或月度 12 行，并通过 ``omittedItems``/``llmPreviewOmitted`` 明确
    标注截断。分页 cursor 不作为可遗弃字段，始终保留到最后的元数据兜底。
    """
    cap = cap or limit("tool_result_llm_cap")
    stripped = _strip_transport(result)
    if len(payload := _json(stripped)) <= cap:
        return payload
    preview = _trim_lists(dict(stripped), result, compact=False)
    if len(payload := _json(preview)) <= cap:
        return payload
    primary = next((key for key in ("groups", "rows", "modules", "sampleWindows")
                    if isinstance(stripped.get(key), list) and stripped[key]), None)
    compact = _trim_lists(preview, result, compact=True, primary=primary)
    compact["llmPreviewTruncated"] = True
    compact["llmPreviewOmitted"] = True
    if len(payload := _json(compact)) <= cap:
        return payload
    compact = _trim_lists(
        compact, result, compact=True,
        primary="modules" if primary == "modules" else None)
    if len(payload := _json(compact)) <= cap:
        return payload
    metadata = _metadata(compact)
    modules = preview.get("modules")
    modules = modules[:_TOP_N] if isinstance(modules, list) else []
    metadata["notice"] = ("结果超出上下文预算；请缩小投影或查询范围，"
                          "完整明细在查询视图查看。")
    while modules:
        trial = {**metadata, "modules": modules}
        if len(payload := _json(trial)) <= cap:
            return payload
        modules = modules[:len(modules) // 2]
    if len(payload := _json(metadata)) <= cap:
        return payload
    minimal = {key: value for key, value in metadata.items()
               if key in ("hasNext", "cursor", "error", "llmPreviewTruncated",
                          "llmPreviewOmitted", "notice")}
    if len(payload := _json(minimal)) <= cap:
        return payload
    minimal.pop("cursor", None)
    minimal["cursorOmitted"] = True
    minimal["notice"] = "结果超出上下文预算；分页游标无法放入上下文，请缩小查询范围。"
    if len(payload := _json(minimal)) <= cap:
        return payload
    return _json({"llmPreviewTruncated": True, "llmPreviewOmitted": True,
                  "notice": "结果超出上下文预算；不能据此推断业务事实。"})


def budget_messages(messages: list[dict], *, cap: int | None = None) -> list[dict]:
    """Keep user turns and assistant/tool call pairs intact under a character budget."""
    cap = cap or limit("context_chars_cap")
    bounded = [dict(message) for message in messages]
    while len(_json(bounded)) > cap:
        user_positions = [i for i, message in enumerate(bounded) if message.get("role") == "user"]
        if len(user_positions) > 1:
            del bounded[user_positions[0]:user_positions[1]]
            continue
        tool_positions = [i for i, message in enumerate(bounded) if message.get("role") == "tool"
                          and len(str(message.get("content") or "")) > 1_000]
        if not tool_positions:
            break
        index = tool_positions[0]
        try:
            data = json.loads(bounded[index]["content"])
        except (TypeError, ValueError):
            data = {"notice": "较早的工具输出过长，请按需要重新读取。"}
        bounded[index]["content"] = tool_payload(data, cap=1_000)
    return bounded
