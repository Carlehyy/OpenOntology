"""Execute the bounded aggregate tool through the governed remote query service."""
from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError

from app.ontologies.agent_query.schemas import MAX_SELECT_ITEMS
from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.limits import limit as runtime_limit
from app.ontologies.agent_runtime.query_planning import (
    _bounded_projection, _visible_properties,
)
from app.ontologies.agent_runtime.query_spec import (
    build_query_spec, drilldown_conditions, make_drilldown, spec_hash,
)
from app.ontologies.agent_runtime.read_queries import encode_scalar

_RANGE_OPS = {"gt", "gte", "lt", "lte"}
_BUCKETS = {"half_hour", "day", "week", "month", "quarter", "year"}
_BUCKET_UNITS = {"half_hour": "半小时", "day": "日", "week": "周",
                  "month": "月", "quarter": "季", "year": "年"}


def _group_page(limit: int | None = None) -> int:
    """分组/枚举页上限：聚合摘要行由注册表统一约束（默认 200，≤ 引擎 200）。

    Top-N 的 ``limit`` 直接作为源端 LIMIT（须与显式 orderBy 一起下推），
    不再先取 200 组再让调用方/模型挑前 N 组。
    """
    cap = min(200, runtime_limit("max_group_page"))
    return cap if limit is None else min(cap, max(1, int(limit)))


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



def _assert_remote_properties_queryable(scope, ot, properties) -> None:
    """源执行前完成属性校验：远程绑定对象只允许查询已映射列。

    聚合工具的原属性护栏只验证声明/授权；未映射属性若拖到计划编制阶段
    才报错，会先触发 contains 枚举等额外源端查询。这里提前 fail closed，
    并给出可查询属性清单。
    """
    visible = _visible_properties(scope, ot)
    queryable = set(visible)
    missing = [name for name in properties if name and name not in queryable]
    if not missing:
        return
    available = "、".join(visible) or "（无）"
    raise ToolError(
        "remote_aggregate_property_unqueryable: 属性"
        f"{'、'.join(f'「{name}」' for name in missing)}"
        f"在对象类型「{ot.display_name}」上没有远程列映射或不可查询；"
        f"可查询属性：{available}")


def _enumeration_truncated(result: dict, rows: list[dict]) -> bool:
    """分组值枚举是否被截断：截断时不能把已命中取值冒充完整 contains 解析。"""
    total = result.get("totalRowCount")
    return bool(result.get("hasNext") or result.get("truncated")
                or result.get("resultBounded")
                or (total is not None and total > len(rows)))


def _contains_unresolved(want) -> ToolError:
    return ToolError(
        "remote_aggregate_contains_unresolved: 远程源该属性取值超过一页，"
        f"无法完整解析 contains「{want}」；请改用 in/eq 给出明确取值后重试")


def _payload_spec(*, scope, ot, payload, labels, description,
                  resolved_window=None, detail_payload=None) -> dict:
    from app.ontologies.agent_query import schemas as QS
    request = QS.QueryRequest.model_validate(payload)
    spec = build_query_spec(
        shape="objects", ontology_id=scope.ontology.id,
        release_id=str(scope.release_id), request=request, labels=labels,
        resolved_window=resolved_window, description=description,
        object_type=ot.id,
    )
    if detail_payload is not None:
        detail_request = QS.QueryRequest.model_validate(detail_payload)
        spec["drilldownBase"] = detail_request.model_dump(by_alias=True)
        spec["drilldownLabels"] = {
            item.as_: item.property for item in detail_request.select
        }
    return spec

def aggregate_remote(db, scope, user, session_id, ot, metric, metric_prop,
                     group_by, filters, order_by=None, limit=None):
    if user is None:
        raise ToolError("remote_query_user_required: 远程统计需要已认证用户")
    cap = int(scope.profile.max_rows_per_query or 0)
    if cap <= 0:
        raise ToolError("profile_query_disabled: 当前本体助手未启用查询额度")
    # contains 枚举页不带 boundedStats，必须同时受注册表上限与 profile 授权帽
    # 约束；否则源端计划复核会以 pageSize 超出授权上限拒绝整个聚合。
    enum_page = min(_group_page(), cap)
    filters = [item for item in (filters or []) if isinstance(item, dict)]
    group_items = group_by if isinstance(group_by, list) else []
    if limit is not None and not group_items:
        raise ToolError("remote_aggregate_invalid_limit: limit 仅用于分组统计；"
                        "标量统计请省略 limit")
    if limit is not None:
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ToolError("remote_aggregate_invalid_limit: limit 必须是整数") from exc
        if requested_limit < 1:
            raise ToolError("remote_aggregate_invalid_limit: limit 需 ≥ 1")
        max_groups = _group_page()
        if requested_limit > max_groups:
            raise ToolError(
                f"remote_aggregate_invalid_limit: limit 需在 1..{max_groups}")
    # 在发起任何源端查询（包括 contains 枚举/0 结果探查）之前校验属性映射。
    _assert_remote_properties_queryable(
        scope, ot,
        [metric_prop, *(prop for prop, _bucket in group_items),
         *(item.get("property") for item in filters)])

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
                        "pageSize": enum_page,
                        "select": [{"source": "objects", "property": item["property"],
                                    "as": "group"}],
                        "groupBy": [{"source": "objects", "property": item["property"]}],
                        "orderBy": [{"output": "value", "direction": "desc"},
                                    {"output": "group", "direction": "asc"}]}
        enum = _execute(db, user, session_id, enum_payload)
        meta_result = enum
        rows = enum["rows"]
        matches = [row["group"] for row in rows if row.get("group") is not None]
        if matches and _enumeration_truncated(enum, rows):
            # 枚举只覆盖一页时，已命中的取值只是完整匹配集合的子集；
            # 用它改写 in 会把小样本计数冒充 contains 全量统计。
            raise _contains_unresolved(want)
        if not matches:
            # 源端 LIKE 可能大小写敏感（如 PostgreSQL）：回退为源端全枚举 +
            # 本地不区分大小写子串匹配；全枚举也可能被截断，任何截断都
            # fail closed —— 有少量命中同样不能冒充完整解析。
            fallback = {**count_payload(others), "pageSize": enum_page,
                        "select": [{"source": "objects", "property": item["property"],
                                    "as": "group"}],
                        "groupBy": [{"source": "objects", "property": item["property"]}],
                        "orderBy": [{"output": "value", "direction": "desc"},
                                    {"output": "group", "direction": "asc"}]}
            enum = _execute(db, user, session_id, fallback)
            meta_result = enum
            rows = enum["rows"]
            if _enumeration_truncated(enum, rows):
                raise _contains_unresolved(want)
            matches = [row["group"] for row in rows
                       if row.get("group") is not None
                       and str(want).lower() in str(row["group"]).lower()]
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
        try:
            entry["querySpec"] = _payload_spec(
                scope=scope, ot=ot, payload=payload,
                labels={"value": metric,
                        **({"matched": "matched"} if metric != "count" else {})},
                description={
                    "semantics": "aggregate_scalar",
                    "objectType": ot.display_name,
                    "metric": metric,
                    "metricProperty": metric_prop,
                    "filters": [
                        {"property": item.get("property"), "op": item.get("op"),
                         "value": item.get("value")} for item in resolved
                    ],
                })
            entry["querySpecHash"] = spec_hash(entry["querySpec"])
        except Exception:  # noqa: BLE001 - spec 是可重放附加信息，不破坏已成功统计
            pass
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
    group_limit = _group_page(limit)
    payload = {**count_payload(payload_where), "aggregates": aggregates,
               "select": select, "groupBy": group_by_payload,
               "orderBy": order_items, "pageSize": group_limit,
               "boundedStats": True}
    result = _execute(db, user, session_id, payload)
    out = base_out(result)
    rows = result["rows"]
    labels = {f"group{index}": prop
              for index, (prop, _bucket) in enumerate(group_items)}
    labels["value"] = metric
    if metric != "count":
        labels["matched"] = "matched"
    item_dicts = [{"source": "objects", "property": prop, "bucket": bucket,
                   "readable": prop} for prop, bucket in group_items]
    # 下钻明细投影同样受 32 项协议上限：分组字段与当前过滤字段优先；
    # 超限字段写入 spec 元数据，绝不因越界让整个 querySpec 静默丢失。
    detail_preferred = [prop for prop, _bucket in group_items]
    detail_preferred += [item.get("property") for item in resolved
                         if item.get("property")]
    detail_selected, detail_omitted = _bounded_projection(
        scope, ot, preferred=detail_preferred)
    detail_select = [
        {"source": "objects", "property": prop, "as": f"c{index}"}
        for index, prop in enumerate(detail_selected)
    ]
    if not detail_select:
        detail_select = [{"source": "objects", "property": prop, "as": f"c{index}"}
                         for index, (prop, _bucket) in enumerate(group_items)]
        detail_omitted = []
    detail_payload = None
    if detail_select:
        detail_payload = {
            "sources": [{"alias": "objects", "ontologyId": scope.ontology.id,
                         "releaseId": scope.release_id, "objectTypeId": ot.id}],
            "where": payload_where,
            "select": detail_select,
        }
    description = {
        "semantics": "aggregate_grouped",
        "objectType": ot.display_name,
        "metric": metric,
        "metricProperty": metric_prop,
        "groupBy": [{"property": prop, "bucket": bucket}
                    for prop, bucket in group_items],
        "filters": [
            {"property": item.get("property"), "op": item.get("op"),
             "value": item.get("value")} for item in resolved
        ],
    }
    if detail_omitted:
        description["drilldownSelectCap"] = MAX_SELECT_ITEMS
        description["drilldownOmittedFields"] = list(detail_omitted)
        description["drilldownProjectionNote"] = (
            "下钻明细投影受单一查询投影上限约束；drilldownOmittedFields "
            "未被观测，不代表字段不存在或值为空")
    spec = None
    try:
        spec = _payload_spec(
            scope=scope, ot=ot, payload=payload, labels=labels,
            description=description, detail_payload=detail_payload,
        )
    except Exception:  # noqa: BLE001 - spec 不破坏已成功统计
        spec = None
    groups = []
    for row in rows:
        values = [row.get(f"group{index}") for index in range(len(group_items))]
        parts = []
        for value, (prop, bucket) in zip(values, group_items):
            label = f"{prop}({_BUCKET_UNITS[bucket]})" if bucket else prop
            parts.append(f"{label}={('（空）' if value is None else value)}")
        entry = {"group": "|".join("" if v is None else str(v) for v in values),
                 "value": number(row["value"]),
                 "count": number(row["value" if metric == "count" else "matched"]),
                 "groupValues": values,
                 "groupDisplay": " | ".join(parts)}
        if len(group_items) > 1:
            entry["values"] = values
        if spec is not None and user is not None and session_id:
            try:
                conditions = drilldown_conditions(values, item_dicts)
                entry["drilldown"] = make_drilldown(
                    owner=str(getattr(user, "id", "")),
                    ontology_id=scope.ontology.id,
                    conversation_id=session_id,
                    base_spec_hash=spec_hash(spec),
                    conditions=conditions,
                    group_values=values,
                    group_by=[prop for prop, _bucket in group_items],
                    resolved_window=None,
                )
            except ToolError as exc:
                entry["drilldownUnavailable"] = str(exc)
        groups.append(entry)
    total = result.get("totalRowCount")
    truncated = bool(result.get("hasNext") or result.get("truncated") or result.get("resultBounded")
                     or (total is not None and total > len(groups)))
    entry = {**out, "groupBy": group_display, "groupBuckets":
             [bucket or None for _prop, bucket in group_items],
             "groups": groups, "returned": len(groups),
             # Top-N 语义：返回顺序由 orderBy 决定（默认按值降序）；totalGroups
             # 是源端精确总组数，truncated=true 表示其余分组未返回且不可翻页。
             "groupLimit": group_limit,
             "totalGroups": total,
             "truncated": truncated,
             "matched": None if truncated else encode_scalar(sum(
                 (Decimal(str(group["count"])) for group in groups), Decimal(0)))}
    if truncated:
        if total is not None:
            entry["truncationNote"] = (
                f"按当前排序最多返回 {group_limit} 组；共 {total} 组，"
                f"其余 {max(0, int(total) - len(groups))} 组未返回且不可翻页")
        else:
            entry["truncationNote"] = (
                f"按当前排序最多返回 {group_limit} 组；其余分组未返回且不可翻页")
    if spec is not None:
        entry["querySpec"] = spec
        entry["querySpecHash"] = spec_hash(spec)
    if not groups:
        entry["dataCoverage"] = _zero_coverage(
            db, user, session_id, resolved, count_payload)
    return entry
