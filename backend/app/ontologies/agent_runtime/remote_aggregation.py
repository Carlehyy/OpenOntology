"""Execute the bounded aggregate tool through the governed remote query service."""
from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.read_queries import encode_scalar


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
    # Local contains is case-insensitive; the remote operator is case-sensitive.
    if op == "contains" or (op in {"gt", "gte", "lt", "lte"} and value is None):
        raise ToolError("remote_aggregate_filter_unsupported: 远程统计不支持此过滤语义；"
                        "请使用明确的等值、范围或 in 条件")
    return leaf


def aggregate_remote(db, scope, user, session_id, ot, metric, metric_prop,
                     group_by, filters):
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import QueryError, execute_unified_query
    from app.ontologies.agent_query.remote_query import cancel_remote_query

    if user is None:
        raise ToolError("remote_query_user_required: 远程统计需要已认证用户")
    cap = int(scope.profile.max_rows_per_query or 0)
    if cap <= 0:
        raise ToolError("profile_query_disabled: 当前本体助手未启用查询额度")
    aggregates = [{"fn": metric, "as": "value"}]
    if metric != "count":
        aggregates[0].update(source="objects", property=metric_prop)
        aggregates.append({"fn": "count", "as": "matched"})
    payload = {
        "sources": [{"alias": "objects", "ontologyId": scope.ontology.id,
                     "releaseId": scope.release_id, "objectTypeId": ot.id}],
        "where": [_filter(item) for item in (filters or [])],
        "aggregates": aggregates, "pageSize": min(50, cap) if group_by else 1,
    }
    if group_by:
        payload.update(
            select=[{"source": "objects", "property": group_by, "as": "group"}],
            groupBy=[{"source": "objects", "property": group_by}],
            orderBy=[{"output": "value", "direction": "desc"},
                     {"output": "group", "direction": "asc"}],
        )
    # Capture display metadata before the query service rolls back its read session.
    object_name, release_id = ot.display_name, scope.release_id
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

    def number(value):
        return encode_scalar(Decimal(str(value))) if value is not None else None

    rows = result["rows"]
    out = {"objectType": object_name, "metric": metric,
           "metricProperty": metric_prop, "scanned": None,
           "partial": False, "exact": True, "completeness": "complete",
           "valueType": "integer" if metric == "count" else "decimal",
           "valueEncoding": "number_or_lossless_string", "dataMode": "remote",
           "countSemantics": "bound_rows", "releaseId": release_id,
           "queryId": result["queryId"], "observedAt": result["observedAt"],
           "countsScope": result["countsScope"], "citations": result["citations"]}
    if not group_by:
        if len(rows) != 1 or result.get("hasNext") or result.get("truncated"):
            raise ToolError("remote_aggregate_incomplete: 远程查询未返回完整标量统计")
        return {**out, "value": number(rows[0]["value"]),
                "matched": number(rows[0]["value" if metric == "count" else "matched"])}
    groups = [{"group": str(row["group"]), "value": number(row["value"]),
               "count": number(row["value" if metric == "count" else "matched"])}
              for row in rows]
    total = result.get("totalRowCount")
    truncated = bool(result.get("hasNext") or result.get("truncated")
                     or (total is not None and total > len(groups)))
    return {**out, "groupBy": group_by, "groups": groups, "totalGroups": total,
            "truncated": truncated,
            "matched": None if truncated else encode_scalar(sum(
                (Decimal(str(group["count"])) for group in groups), Decimal(0)))}
