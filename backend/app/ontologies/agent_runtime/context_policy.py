"""Bounded ontology discovery, conversation context and structured tool previews."""
from __future__ import annotations

import json
import re
from itertools import chain, zip_longest
from typing import Any

from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.query_coverage import coverage_metadata
from app.ontologies.agent_runtime.query_window import TEMPORAL_PROPERTY_TYPES
from app.ontologies.formal_modeling.action_validation import action_parameter_hints

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


def _mentioned(question: str, *labels: Any) -> bool:
    for label in labels:
        if not isinstance(label, str) or not (term := label.strip().casefold()):
            continue
        if not term.isascii():
            if term in question:
                return True
        elif re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", question):
            return True
    return False


def _released_catalog(scope, question: str, budget: int) -> str:
    """Rank only released, visible metadata; mentions are hints, not a query plan."""
    question = question[:limit("context_chars_cap")].casefold()
    types = scope.object_types
    coverage = scope.query_coverage
    remote_ids = coverage.remote_ids
    remote_states = coverage.property_states
    direct = {ot.id for ot in types.values()
              if _mentioned(question, ot.id, ot.name, ot.display_name)}
    related = set()
    named_links, inferred_links = [], []
    for link in scope.link_types.values():
        source, target = link.source_object_type_id, link.target_object_type_id
        if _mentioned(question, link.id, link.name, link.display_name):
            named_links.append(link)
            related.update((source, target))
        elif source != target and source in direct and target in direct:
            inferred_links.append(link)
    actions = []
    if scope.profile.allow_action_proposals:
        for action in scope.actions.values():
            if _mentioned(question, action.id, action.name, action.display_name):
                actions.append(action)
                if action.object_type_id is not None:
                    related.add(action.object_type_id)

    # 自由描述可能提到其他字段；只在不受结构和远程列限制时加入短摘录。
    descriptions = not remote_ids and all(getattr(scope.profile, key) is None for key in (
        "allowed_object_type_ids", "allowed_link_type_ids", "allowed_action_ids",
        "allowed_properties",
    ))
    ranked = []
    for ot in types.values():
        allowed = scope.allowed_property_names(ot.id)
        queryable = scope.queryable_property_names(ot, remote_states=remote_states)
        fields = []
        primary = None
        for prop in ot.properties or []:
            if not isinstance(prop, dict) or not (name := prop.get("name")):
                continue
            if ((allowed is not None and name not in allowed)
                    or (queryable is not None and name not in queryable)):
                continue
            label = prop.get("displayName") or prop.get("display_name") or name
            ptype = str(prop.get("type") or "string")
            mentioned = _mentioned(question, name, label)
            is_primary = bool(ot.primary_key and ot.primary_key in (name, prop.get("id")))
            if is_primary:
                primary = name
            field = {"name": name, "displayName": label, "type": ptype}
            if descriptions and mentioned and isinstance(prop.get("description"), str):
                field["description"] = prop["description"][:120]
            fields.append(((mentioned, is_primary, ptype.lower() in TEMPORAL_PROPERTY_TYPES), field))
        priority = (ot.id in related, ot.id in direct, any(score[0] for score, _ in fields))
        ranked.append((priority, ot, fields, primary))
    ranked.sort(key=lambda entry: entry[0], reverse=True)

    lines = ["# 本体授权目录", _json({"releaseId": scope.release_id})]
    footer = "目录为节选，缺少的授权属性、关系和动作可用 describe_objects 定向查看；不声明实例数量。"
    used = sum(len(line) + 1 for line in lines) + len(footer)

    def append(record: dict) -> None:
        nonlocal used
        line = _json(record)
        if used + len(line) + 1 <= budget:
            lines.append(line)
            used += len(line) + 1

    def type_record(entry) -> dict:
        priority, ot, fields, primary = entry
        fields.sort(key=lambda pair: pair[0], reverse=True)
        record = {
            "objectTypeId": ot.id, "name": ot.name, "displayName": ot.display_name,
            **coverage.object(ot).payload(),
            "properties": [field for _, field in fields[:6]],
        }
        if primary is not None:
            record["primaryKey"] = primary
        if len(fields) > 6:
            record["omittedProperties"] = len(fields) - 6
        if descriptions and any(priority) and ot.description:
            record["description"] = ot.description[:120]
        return record

    def link_record(link) -> dict:
        record = {
            "linkType": link.name, "id": link.id, "displayName": link.display_name,
            "sourceObjectTypeId": link.source_object_type_id,
            "targetObjectTypeId": link.target_object_type_id,
            "cardinality": link.cardinality,
            **coverage.link(link).payload(),
        }
        if descriptions and link.description:
            record["description"] = link.description[:120]
        return record

    def action_record(action) -> dict:
        return {
            "action": action.name, "id": action.id, "displayName": action.display_name,
            "objectTypeId": action.object_type_id,
            "requiresApproval": bool(action.requires_approval),
            "parameters": action_parameter_hints(action),
        }

    # Share the focus budget across types, relationships and actions. A long
    # list of matching types must not take all the space before a named action.
    for records in zip_longest(
        (type_record(entry) for entry in ranked if any(entry[0])),
        map(link_record, chain(named_links, inferred_links)),
        map(action_record, actions),
    ):
        for record in records:
            if record is not None:
                append(record)
    for entry in ranked:
        if not any(entry[0]):
            append(type_record(entry))
    return "\n".join([*lines, footer])


def ontology_catalog(scope, *, question: str | None = None) -> str:
    budget = min(16_000, limit("context_chars_cap") // 3)
    if question is not None and getattr(scope, "release", None) is not None:
        return _released_catalog(scope, question, budget)
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
        access = scope.query_coverage.object(item)
        mode = {"remote": "远程绑定", "materialized": "本地物化",
                "unmapped": "仅定义·无查询覆盖"}[access.mode]
        if access.reason:
            mode += f"，暂不可查询：{access.reason}"
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
    stripped = {**_strip_transport(result), **coverage_metadata(result)}
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
    if "error" in minimal:
        minimal["error"] = str(minimal["error"])[:240]
    for keep in (3, 1):
        if len(payload := _json({**minimal, **coverage_metadata(stripped, keep)})) <= cap:
            return payload
    if minimal.pop("cursor", None) is not None:
        minimal["cursorOmitted"] = True
        minimal["notice"] = "结果超出上下文预算；分页游标无法放入上下文，请缩小查询范围。"
    for keep in (3, 1, 0):
        if len(payload := _json({**minimal, **coverage_metadata(stripped, keep)})) <= cap:
            return payload
    smallest = {"llmPreviewTruncated": True, "llmPreviewOmitted": True,
                "notice": "结果超出上下文预算；不能据此推断业务事实。"}
    if len(payload := _json({**smallest, **coverage_metadata(stripped, 1)})) <= cap:
        return payload
    return _json(smallest)


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
