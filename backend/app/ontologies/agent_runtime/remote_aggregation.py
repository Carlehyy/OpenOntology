"""Execute the bounded aggregate tool through the governed remote query service."""
from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.limits import limit as runtime_limit
from app.ontologies.agent_runtime.read_queries import encode_scalar

_RANGE_OPS = {"gt", "gte", "lt", "lte"}
_BUCKETS = {"day", "week", "month", "quarter", "year"}
_BUCKET_UNITS = {"day": "日", "week": "周", "month": "月", "quarter": "季", "year": "年"}


def _group_page() -> int:
    """分组/枚举页上限：聚合摘要行由注册表统一约束（默认 200，≤ 引擎 200）。"""
    return min(200, runtime_limit("max_group_page"))


def normalize_group_by(group_by) -> list[tuple[str, str | None]]:
    """group_by 参数归一为 (属性名, bucket|None) 列表。

    接受单属性名、属性名数组，或 {"property": ..., "bucket": ...} 对象数组
    （多维分组 + 日/周/月/季/年时间分桶）。
    """
    if group_by is None:
        return []
    items = group_by if isinstance(group_by, list) else [group_by]
    out: list[tuple[str, str | None]] = []
    for item in items:
        if isinstance(item, dict):
            prop = item.get("property") or item.get("name")
            bucket = item.get("bucket")
        else:
            prop, bucket = item, None
        if not prop or not isinstance(prop, str):
            raise ToolError("remote_aggregate_invalid_query: group_by 条目缺少 property")
        if bucket is not None and bucket not in _BUCKETS:
            raise ToolError("remote_aggregate_invalid_query: 不支持的时间分桶 "
                            f"「{bucket}」；可用 day/week/month/quarter/year")
        if (prop, bucket) not in out:
            out.append((prop, bucket))
    return out


def group_by_display(group_items: list[tuple[str, str | None]]) -> str:
    """分组维度的展示名：分桶维度附单位（如「发生时间(月)」），多维用 | 连接。"""
    parts = [f"{prop}({ _BUCKET_UNITS[bucket]})" if bucket else prop
             for prop, bucket in group_items]
    return "|".join(parts)


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
    """校验、执行并释放一页有界的统一查询；不完整结果 fail closed。

    有界分组统计（boundedStats）例外：源端 LIMIT 截断只影响不可达的分组，
    已返回分组值与总组数仍精确（响应 exact/completeness 由适配器保证）。
    """
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
    """区分 0 的成因：范围内无数据 / 等值取值无匹配。

    只保留一次「去掉范围条件后的等值探查」；不再对全表计数 —— 0 结果不应
    比正结果更贵。等值在范围内外均无匹配时，如实报告无法在不额外全表
    计数的前提下区分「取值不存在」与「源完全无数据」。
    """
    if not resolved:
        return {"zeroResult": True, "reason": "source_no_data", "unfilteredCount": None,
                "note": "授权范围内远程源没有数据"}
    identity = [item for item in resolved if item.get("op", "eq") not in _RANGE_OPS]
    if len(identity) < len(resolved):
        outside = _scalar_count(db, user, session_id,
                                count_payload, [_filter(item) for item in identity])
        if outside:
            return {"zeroResult": True, "reason": "no_data_in_range",
                    "matchedOutsideRange": outside,
                    "note": f"等值条件在范围条件之外匹配 {outside} 行：名称/取值存在，"
                            "但指定范围（如该时段）内无数据"}
        if not identity:
            return {"zeroResult": True, "reason": "source_no_data", "unfilteredCount": None,
                    "note": "授权范围内远程源没有数据"}
        return {"zeroResult": True, "reason": "filter_no_match", "unfilteredCount": None,
                "note": "等值条件在范围内外均无匹配；未做全表计数，"
                        "无法区分取值不存在或源为空"}
    return {"zeroResult": True, "reason": "filter_no_match", "unfilteredCount": None,
            "note": "等值条件无任何匹配；未做全表计数，"
                    "无法区分取值不存在或源为空"}


def _default_order_by(group_items, order_by):
    """排序归一：显式 order_by 优先；趋势（全分桶）默认按桶升序，TopN 按值降序。"""
    spec = (order_by if isinstance(order_by, list)
            else [order_by] if isinstance(order_by, dict) else [])
    items = []
    for item in spec:
        if not isinstance(item, dict):
            continue
        output = item.get("output")
        direction = item.get("direction", "desc")
        if output not in {"value", "matched", "group"} or direction not in {"asc", "desc"}:
            raise ToolError("remote_aggregate_invalid_query: order_by 只支持输出 "
                            "value/matched/group 与方向 asc/desc")
        items.append({"output": output if output != "group" else "group0",
                      "direction": direction})
    if items:
        return items
    if group_items and all(bucket for _prop, bucket in group_items):
        return [{"output": "group0", "direction": "asc"},
                {"output": "value", "direction": "desc"}]
    order = [{"output": "value", "direction": "desc"}]
    order += [{"output": f"group{index}", "direction": "asc"}
              for index in range(len(group_items))]
    return order


def aggregate_remote(db, scope, user, session_id, ot, metric, metric_prop,
                     group_by, filters, order_by=None):
    if user is None:
        raise ToolError("remote_query_user_required: 远程统计需要已认证用户")
    cap = int(scope.profile.max_rows_per_query or 0)
    if cap <= 0:
        raise ToolError("profile_query_disabled: 当前本体助手未启用查询额度")
    filters = [item for item in (filters or []) if isinstance(item, dict)]
    group_items = group_by if isinstance(group_by, list) else []

    def count_payload(where):
        return {"sources": [{"alias": "objects", "ontologyId": scope.ontology.id,
                             "releaseId": scope.release_id, "objectTypeId": ot.id}],
                "where": where,
                "aggregates": [{"fn": "count", "as": "value"}], "pageSize": 1}

    # 远程 contains：先把模糊条件下推到源端 LIKE 枚举（大小写不敏感场景由
    # 本地语义兜底），解析为明确 in 条件后执行主统计。
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
        like_leaf = {"source": "objects", "property": item["property"],
                     "op": "contains", "value": want}
        enum_payload = {**count_payload(others + [like_leaf]),
                        "pageSize": _group_page(),
                        "select": [{"source": "objects", "property": item["property"],
                                    "as": "group"}],
                        "groupBy": [{"source": "objects", "property": item["property"]}],
                        "orderBy": [{"output": "value", "direction": "desc"},
                                    {"output": "group", "direction": "asc"}]}
        enum = _execute(db, user, session_id, enum_payload)
        meta_result = enum
        rows = enum["rows"]
        matches = [row["group"] for row in rows if row.get("group") is not None]
        if not matches:
            # 源端 LIKE 可能大小写敏感（如 PostgreSQL）：回退为源端全枚举 +
            # 本地不区分大小写子串匹配，仍无命中才证明空集。
            fallback = {**count_payload(others), "pageSize": _group_page(),
                        "select": [{"source": "objects", "property": item["property"],
                                    "as": "group"}],
                        "groupBy": [{"source": "objects", "property": item["property"]}],
                        "orderBy": [{"output": "value", "direction": "desc"},
                                    {"output": "group", "direction": "asc"}]}
            enum = _execute(db, user, session_id, fallback)
            meta_result = enum
            rows = enum["rows"]
            total = enum.get("totalRowCount")
            truncated = bool(enum.get("hasNext") or enum.get("truncated")
                             or (total is not None and total > len(rows)))
            matches = [row["group"] for row in rows
                       if row.get("group") is not None
                       and str(want).lower() in str(row["group"]).lower()]
            if not matches and truncated:
                raise ToolError(
                    "remote_aggregate_contains_unresolved: 远程源该属性取值超过一页且无一"
                    f"匹配「{want}」；请改用 in/eq 给出明确取值后重试")
        if matches:
            resolved.append({"property": item["property"], "op": "in", "value": matches})
        else:
            contains_unmatched = str(want)
            break  # 完整枚举无命中：合取已可证明为空集，其余条件无需再解析。

    aggregates = [{"fn": metric, "as": "value"}]
    if metric != "count":
        aggregates[0].update(source="objects", property=metric_prop)
        aggregates.append({"fn": "count", "as": "matched"})
    object_name, release_id = ot.display_name, scope.release_id

    def number(value):
        if value is None:
            return None
        try:
            return encode_scalar(Decimal(str(value)))
        except Exception:  # noqa: BLE001 - min/max 作用于 date/datetime 属性时
            # 返回值是 ISO 日期字符串而非数值，按原值无损编码。
            return encode_scalar(value)

    def base_out(result) -> dict:
        return {"objectType": object_name, "metric": metric,
                "metricProperty": metric_prop, "scanned": None,
                "partial": bool(result.get("partial")),
                "exact": result.get("exact") is not False,
                "completeness": result.get("completeness", "complete"),
                # completeness 只代表「查询在授权范围内执行完成」，不代表
                # 数据时间覆盖完整；回答须另行注明数据时间范围与截止日期。
                "completenessScope": "query_execution",
                "valueType": "integer" if metric == "count" else "decimal",
                "valueEncoding": "number_or_lossless_string", "dataMode": "remote",
                "countSemantics": "bound_rows", "releaseId": release_id,
                "queryId": result["queryId"], "observedAt": result["observedAt"],
                "countsScope": result["countsScope"], "citations": result["citations"]}

    group_display = group_by_display(group_items)

    if contains_unmatched is not None:
        # 完整枚举证明 contains 无任何取值命中：不再执行主查询，直接返回
        # 带覆盖说明的可证明零结果（SQL 空集语义：count=0，sum/avg/min/max=null）。
        coverage = {"zeroResult": True, "reason": "filter_no_match",
                    "unfilteredCount": None,
                    "note": f"模糊条件「{contains_unmatched}」在远程源无任何取值命中"
                            "（完整枚举）"}
        out = base_out(meta_result)
        if not group_items:
            return {**out, "value": number(0 if metric == "count" else None),
                    "matched": number(0), "dataCoverage": coverage}
        return {**out, "groupBy": group_display, "groups": [], "totalGroups": 0,
                "truncated": False, "matched": number(0), "dataCoverage": coverage}

    payload_where = [_filter(item) for item in resolved]
    if not group_items:
        payload = {**count_payload(payload_where), "aggregates": aggregates, "pageSize": 1}
        result = _execute(db, user, session_id, payload)
        out = base_out(result)
        rows = result["rows"]
        if len(rows) != 1 or result.get("hasNext") or result.get("truncated"):
            raise ToolError("remote_aggregate_incomplete: 远程查询未返回完整标量统计")
        matched_cell = rows[0]["value" if metric == "count" else "matched"]
        entry = {**out, "value": number(rows[0]["value"]),
                 "matched": number(matched_cell)}
        if Decimal(str(matched_cell)) == 0:
            entry["dataCoverage"] = _zero_coverage(
                db, user, session_id, resolved, count_payload)
        return entry

    # 分组统计：boundedStats 让分组聚合在源端按页截断（top-N + 精确总组数），
    # 高基数分组不再全量回流内存；分桶/趋势见 group_by 的 bucket 参数。
    select = [{"source": "objects", "property": prop, "as": f"group{index}"}
              for index, (prop, _bucket) in enumerate(group_items)]
    group_by_payload = [{"source": "objects", "property": prop,
                         **({"bucket": bucket} if bucket else {})}
                        for prop, bucket in group_items]
    order_items = _default_order_by(group_items, order_by)
    payload = {**count_payload(payload_where), "aggregates": aggregates,
               "select": select, "groupBy": group_by_payload,
               "orderBy": order_items, "pageSize": _group_page(),
               "boundedStats": True}
    result = _execute(db, user, session_id, payload)
    out = base_out(result)
    rows = result["rows"]
    groups = []
    for row in rows:
        values = [row.get(f"group{index}") for index in range(len(group_items))]
        entry = {"group": "|".join("" if v is None else str(v) for v in values),
                 "value": number(row["value"]),
                 "count": number(row["value" if metric == "count" else "matched"])}
        if len(group_items) > 1:
            entry["values"] = values
        groups.append(entry)
    total = result.get("totalRowCount")
    truncated = bool(result.get("hasNext") or result.get("truncated") or result.get("resultBounded")
                     or (total is not None and total > len(groups)))
    entry = {**out, "groupBy": group_display, "groupBuckets":
             [bucket or None for _prop, bucket in group_items],
             "groups": groups, "totalGroups": total,
             "truncated": truncated,
             "matched": None if truncated else encode_scalar(sum(
                 (Decimal(str(group["count"])) for group in groups), Decimal(0)))}
    if not groups:
        entry["dataCoverage"] = _zero_coverage(
            db, user, session_id, resolved, count_payload)
    return entry
