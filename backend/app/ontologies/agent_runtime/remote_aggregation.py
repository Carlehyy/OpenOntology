"""Execute the bounded aggregate tool through the governed remote query service."""
from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.read_queries import encode_scalar

_RANGE_OPS = {"gt", "gte", "lt", "lte"}


def _filter(item):
    leaf = {"source": "objects", "property": item["property"],
            "op": item.get("op", "eq"), "value": item.get("value")}
    value, op = leaf["value"], leaf["op"]
    null = {"source": "objects", "property": item["property"], "op": "is_null"}
    if op in {"eq", "neq"} and value is None:
        return {**null, "op": "is_null" if op == "eq" else "is_not_null"}
    if op == "neq":
        return {"or": [leaf, null]}
    if op == "in":
        values = value if isinstance(value, list) else [value]
        if None in values:
            nonnull = [v for v in values if v is not None]
            return {"or": [{**leaf, "value": nonnull}, null]} if nonnull else null
        return {**leaf, "value": values}
    # contains 由调用方先经源端枚举解析为 in；范围配 null 无远程语义。
    if op == "contains" or (op in _RANGE_OPS and value is None):
        raise ToolError("remote_aggregate_filter_unsupported: 远程统计不支持此过滤语义；"
                        "请使用明确的等值、范围或 in 条件")
    return leaf


def _execute(db, user, session_id, payload):
    """校验、执行并释放一页有界的统一查询；不完整结果 fail closed。"""
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import QueryError, execute_unified_query
    from app.ontologies.agent_query.remote_query import cancel_remote_query

    try:
        request = QS.QueryRequest.model_validate(payload)
        result = execute_unified_query(db, user, request, session_kind="ontology_agent",
                                       session_id=session_id or "")
        # The tool consumes one bounded page and exposes no continuation cursor.
        cancel_remote_query(db, user, result["queryId"])
    except ValidationError as exc:
        raise ToolError(f"remote_aggregate_invalid_query: {exc}") from exc
    except QueryError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    if (result.get("partial") or result.get("exact") is False
            or result.get("completeness", "complete") != "complete"):
        raise ToolError("remote_aggregate_incomplete: 远程查询未返回完整统计")
    return result


def _scalar_count(db, user, session_id, count_payload, where) -> int:
    probe = _execute(db, user, session_id, count_payload(where))
    rows = probe["rows"]
    if len(rows) != 1 or probe.get("hasNext") or probe.get("truncated"):
        raise ToolError("remote_aggregate_incomplete: 远程查询未返回完整标量统计")
    return int(Decimal(str(rows[0]["value"])))


def _zero_coverage(db, user, session_id, resolved, count_payload) -> dict:
    """区分 0 的成因：范围内无数据 / 等值取值无匹配 / 授权范围内源无数据。"""
    if not resolved:
        return {"zeroResult": True, "reason": "source_no_data", "unfilteredCount": 0,
                "note": "授权范围内远程源没有数据"}
    identity = [item for item in resolved if item.get("op", "eq") not in _RANGE_OPS]
    if len(identity) < len(resolved):
        where = [_filter(item) for item in identity]
        outside = _scalar_count(db, user, session_id, count_payload, where)
        if outside:
            return {"zeroResult": True, "reason": "no_data_in_range",
                    "matchedOutsideRange": outside,
                    "note": f"等值条件在范围条件之外匹配 {outside} 行：名称/取值存在，"
                            "但指定范围（如该时段）内无数据"}
        if not identity:
            return {"zeroResult": True, "reason": "source_no_data", "unfilteredCount": 0,
                    "note": "授权范围内远程源没有数据"}
    total = _scalar_count(db, user, session_id, count_payload, [])
    if total:
        return {"zeroResult": True, "reason": "filter_no_match", "unfilteredCount": total,
                "note": f"远程源共 {total} 行，但过滤取值无任何匹配：名称或取值不存在"}
    return {"zeroResult": True, "reason": "source_no_data", "unfilteredCount": 0,
            "note": "授权范围内远程源没有数据"}


def aggregate_remote(db, scope, user, session_id, ot, metric, metric_prop,
                     group_by, filters):
    if user is None:
        raise ToolError("remote_query_user_required: 远程统计需要已认证用户")
    cap = int(scope.profile.max_rows_per_query or 0)
    if cap <= 0:
        raise ToolError("profile_query_disabled: 当前本体助手未启用查询额度")
    filters = [item for item in (filters or []) if isinstance(item, dict)]

    def count_payload(where):
        return {"sources": [{"alias": "objects", "ontologyId": scope.ontology.id,
                             "releaseId": scope.release_id, "objectTypeId": ot.id}],
                "where": where,
                "aggregates": [{"fn": "count", "as": "value"}], "pageSize": 1}

    # 远程 contains 是大小写敏感 LIKE，与本地语义不一致：先经源端枚举取值，
    # 按本地不区分大小写子串语义解析为明确 in 条件，再执行主统计。
    resolved: list[dict] = []
    contains_unmatched, meta_result = None, None
    for item in filters:
        if item.get("op") != "contains":
            resolved.append(item)
            continue
        want = item.get("value")
        if want is None:
            raise ToolError("remote_aggregate_filter_unsupported: 远程统计的 contains "
                            "需要非空取值；请使用明确的等值或 in 条件")
        others = [_filter(other) for other in filters
                  if other is not item and other.get("op") != "contains"]
        payload = {**count_payload(others), "pageSize": min(50, cap),
                   "select": [{"source": "objects", "property": item["property"],
                               "as": "group"}],
                   "groupBy": [{"source": "objects", "property": item["property"]}],
                   "orderBy": [{"output": "value", "direction": "desc"},
                               {"output": "group", "direction": "asc"}]}
        enum = _execute(db, user, session_id, payload)
        meta_result = enum
        rows = enum["rows"]
        total = enum.get("totalRowCount")
        truncated = bool(enum.get("hasNext") or enum.get("truncated")
                         or (total is not None and total > len(rows)))
        matches = [row["group"] for row in rows
                   if row.get("group") is not None
                   and str(want).lower() in str(row["group"]).lower()]
        if matches:
            resolved.append({"property": item["property"], "op": "in", "value": matches})
        elif truncated:
            raise ToolError(
                "remote_aggregate_contains_unresolved: 远程源该属性取值超过一页且无一"
                f"匹配「{want}」；请改用 in/eq 给出明确取值后重试")
        else:
            contains_unmatched = str(want)
            break  # 完整枚举无命中：合取已可证明为空集，其余条件无需再解析。

    aggregates = [{"fn": metric, "as": "value"}]
    if metric != "count":
        aggregates[0].update(source="objects", property=metric_prop)
        aggregates.append({"fn": "count", "as": "matched"})
    object_name, release_id = ot.display_name, scope.release_id

    def number(value):
        return encode_scalar(Decimal(str(value))) if value is not None else None

    def base_out(result) -> dict:
        return {"objectType": object_name, "metric": metric,
                "metricProperty": metric_prop, "scanned": None,
                "partial": bool(result.get("partial")),
                "exact": result.get("exact") is not False,
                "completeness": result.get("completeness", "complete"),
                "valueType": "integer" if metric == "count" else "decimal",
                "valueEncoding": "number_or_lossless_string", "dataMode": "remote",
                "countSemantics": "bound_rows", "releaseId": release_id,
                "queryId": result["queryId"], "observedAt": result["observedAt"],
                "countsScope": result["countsScope"], "citations": result["citations"]}

    if contains_unmatched is not None:
        # 完整枚举证明 contains 无任何取值命中：不再执行主查询，直接返回
        # 带覆盖说明的可证明零结果（SQL 空集语义：count=0，sum/avg/min/max=null）。
        coverage = {"zeroResult": True, "reason": "filter_no_match",
                    "unfilteredCount": None,
                    "note": f"模糊条件「{contains_unmatched}」在远程源无任何取值命中"
                            "（完整枚举）"}
        out = base_out(meta_result)
        if not group_by:
            return {**out, "value": number(0 if metric == "count" else None),
                    "matched": number(0), "dataCoverage": coverage}
        return {**out, "groupBy": group_by, "groups": [], "totalGroups": 0,
                "truncated": False, "matched": number(0), "dataCoverage": coverage}

    payload = {**count_payload([_filter(item) for item in resolved]),
               "aggregates": aggregates, "pageSize": min(50, cap) if group_by else 1}
    if group_by:
        payload.update(
            select=[{"source": "objects", "property": group_by, "as": "group"}],
            groupBy=[{"source": "objects", "property": group_by}],
            orderBy=[{"output": "value", "direction": "desc"},
                     {"output": "group", "direction": "asc"}],
        )
    result = _execute(db, user, session_id, payload)
    out = base_out(result)
    rows = result["rows"]

    if not group_by:
        if len(rows) != 1 or result.get("hasNext") or result.get("truncated"):
            raise ToolError("remote_aggregate_incomplete: 远程查询未返回完整标量统计")
        matched_cell = rows[0]["value" if metric == "count" else "matched"]
        entry = {**out, "value": number(rows[0]["value"]),
                 "matched": number(matched_cell)}
        if Decimal(str(matched_cell)) == 0:
            entry["dataCoverage"] = _zero_coverage(
                db, user, session_id, resolved, count_payload)
        return entry
    groups = [{"group": str(row["group"]), "value": number(row["value"]),
               "count": number(row["value" if metric == "count" else "matched"])}
              for row in rows]
    total = result.get("totalRowCount")
    truncated = bool(result.get("hasNext") or result.get("truncated")
                     or (total is not None and total > len(groups)))
    entry = {**out, "groupBy": group_by, "groups": groups, "totalGroups": total,
             "truncated": truncated,
             "matched": None if truncated else encode_scalar(sum(
                 (Decimal(str(group["count"])) for group in groups), Decimal(0)))}
    if not groups:
        entry["dataCoverage"] = _zero_coverage(
            db, user, session_id, resolved, count_payload)
    return entry
