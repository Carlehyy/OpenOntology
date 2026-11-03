"""本体助手的统一查询工具：明细/排序/分页/按 ID/多维分组/关联统计与数据时间范围。

所有查询都经由 execute_unified_query 的正门：远程绑定对象转远程统一查询
（源端 SQL），本地物化对象走本地快照路径 —— 授权、发布校验、join 证据、
内存预算全部复用既有链路。本模块只做工具参数 → QueryRequest 的装配、
响应可读化、可重放 querySpec、以及分组下钻令牌。

关键语义：
- 服务端相对时间只在本次解析：``window={property, timezone, last}`` 被冻结为
  绝对 [start, end) 写入 querySpec；后续 replay/下钻不再解释“最近 N 天”。
- ``querySpec`` 是可交给 Web 的语义计划（固定 ontology/release + 绝对窗口 +
  规范化 QueryRequest 或 SPL 绑定），不含底层连接信息或引擎 cursor。
- 聚合分组返回结构化 ``groupValues`` 与签名 ``drilldown``；时间桶下钻用
  日历边界 [bucketStart, bucketEnd)，绝不按展示串拆字符。
- 分页句柄遵循终页回收语义（hasNext=false 时释放）；中间页保持引擎一致性。
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.chart_specs import _build_aggregate_chart
from app.ontologies.agent_runtime.query_handles import (
    _release_terminal_page,
)
from app.ontologies.agent_runtime.query_planning import (
    _build_description, _definition_type, _definitions, _leaf,
    _resolve_type_reference, _spec_from_request, _spl_bindings,
    _visible_properties,
)
from app.ontologies.agent_runtime.query_response import (
    _attach_drilldowns, _consistency, _data_mode, _group_items_from_spec,
    cursor_page_result,
)
from app.ontologies.agent_runtime.query_spec import (
    build_columns, build_query_spec, drilldown_conditions, make_drilldown,
    map_rows, spec_hash,
)
from app.ontologies.agent_runtime.query_window import (
    resolve_window, window_filters, window_for_result,
)
from app.ontologies.agent_runtime.read_queries import (
    decimal_number, encode_scalar,
)

_FILTER_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "contains", "in",
               "is_null", "is_not_null"}
_AGGREGATE_FNS = {"count", "count_distinct", "sum", "avg", "min", "max"}
_BUCKETS = {"day", "week", "month", "quarter", "year"}
_BUCKET_UNITS = {"day": "日", "week": "周", "month": "月",
                 "quarter": "季", "year": "年"}
_SPL_KEYS = {"spl", "semanticBindings", "semantic_bindings", "params",
             "cursor", "page_size"}


def _query_objects_spl(db, scope, user, session_id, args, cap,
                       spec_cache=None) -> dict:
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import (
        QueryError, execute_unified_query, fetch_unified_query_page,
    )

    text = str(args.get("spl") or "").strip()
    if not text:
        raise ToolError("query_objects_invalid_query: spl 不能为空")
    cursor = args.get("cursor")
    page_size = min(cap, max(1, int(args.get("page_size") or 50)))
    spec = spec_cache.get(str(cursor)) if (cursor and spec_cache) else None
    if cursor and spec is None:
        raise ToolError(
            "query_objects_cursor_context_missing: 无法从当前回合恢复该 cursor "
            "对应的 SPL 查询计划；请在发起查询的同一回合内续页，或重新发起查询")
    try:
        if cursor:
            data = fetch_unified_query_page(
                db, user, str(cursor), session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
        else:
            bindings = _spl_bindings(
                scope, args.get("semanticBindings")
                or args.get("semantic_bindings") or {})
            params = args.get("params") or {}
            if not isinstance(params, dict):
                raise ToolError("query_objects_invalid_query: params 必须是对象")
            request = QS.QueryRequest(
                spl=text, semantic_bindings=bindings, params=params,
                page_size=page_size)
            spec = build_query_spec(
                shape="spl",
                ontology_id=scope.ontology.id,
                release_id=str(scope.release_id),
                spl=text,
                semantic_bindings=bindings,
                params=params,
                resolved_window=None,
                description={
                    "semantics": "spl_multi_stage",
                    "semanticBindings": bindings,
                    "params": params,
                    "note": "SPL 阶段通过既有语义翻译/授权/证据链执行",
                },
            )
            data = execute_unified_query(
                db, user, request, session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
    except ValidationError as exc:
        raise ToolError(f"query_objects_invalid_query: {exc}") from exc
    except QueryError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc

    _release_terminal_page(db, user, session_id, data)
    labels = (spec or {}).get("labels") or {}
    columns = build_columns(data.get("columns"), labels)
    rows = map_rows(data.get("rows") or [], labels)
    out = {
        "shape": "spl",
        "dataMode": _data_mode(data),
        "columns": columns,
        "rows": rows,
        "returned": len(rows),
        "totalRowCount": encode_scalar(data.get("totalRowCount"))
        if data.get("totalRowCount") is not None else None,
        "rowCounting": data.get("rowCounting"),
        "hasNext": bool(data.get("hasNext")),
        "cursor": data.get("cursor") if data.get("hasNext") else None,
        "truncated": bool(data.get("hasNext")),
        "exact": data.get("exact") is not False,
        "partial": bool(data.get("partial")),
        "completeness": data.get("completeness", "complete"),
        "completenessScope": "query_execution",
        "pageSize": data.get("pageSize", page_size),
        "queryId": data.get("queryId"),
        "observedAt": data.get("observedAt"),
        "releaseId": scope.release_id,
        "resolvedWindow": None,
        "consistency": _consistency(data, page_fetch=False),
    }
    if spec is not None:
        out["querySpec"] = spec
        out["querySpecHash"] = spec_hash(spec)
        if out["cursor"] and spec_cache is not None:
            spec_cache[out["cursor"]] = spec
    else:
        out["querySpecUnavailable"] = "cursor_page_without_previous_spec"
    return out


def query_objects(db, scope, user, session_id: str, args: dict,
                  spec_cache: dict | None = None) -> dict:
    """明细/排序/分页/多维分组/关联统计/SPL 的统一查询工具。"""
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import (
        QueryError, execute_unified_query, fetch_unified_query_page,
    )

    if user is None:
        raise ToolError("remote_query_user_required: 统一查询需要已认证用户")
    if scope.release_id is None:
        raise ToolError("query_objects_release_required: 统一查询需要固定的发布版本；"
                        "请从已发布本体进入助手后重试")
    cap = min(200, int(scope.profile.max_rows_per_query or 0))
    if cap <= 0:
        raise ToolError("profile_query_disabled: 当前本体助手未启用查询额度")
    if args.get("spl"):
        mixed = sorted(set(args) - _SPL_KEYS)
        if mixed:
            raise ToolError("query_objects_invalid_query: spl 形态不能同时携带结构树参数："
                            + "、".join(mixed))
        return _query_objects_spl(db, scope, user, session_id, args, cap,
                                  spec_cache=spec_cache)

    # cursor-first：续页只带 cursor 时从回合内 querySpec 缓存恢复源/标签/
    # 分组/窗口，不再要求 object_type；缓存缺失则显式要求上下文，绝不把
    # 空类型当错误对象或返回 c0/g0 技术列冒充正常结果。
    cursor = args.get("cursor")
    cached_spec = spec_cache.get(str(cursor)) if (
        cursor and spec_cache) else None
    if cursor and cached_spec is not None:
        try:
            data = fetch_unified_query_page(
                db, user, str(cursor), session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
        except QueryError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        _release_terminal_page(db, user, session_id, data)
        result = cursor_page_result(
            data=data, spec=cached_spec, user=user,
            session_id=session_id or "", page_fetch=True)
        if result.get("cursor") and spec_cache is not None:
            spec_cache[str(result["cursor"])] = cached_spec
        return result
    if cursor:
        raise ToolError(
            "query_objects_cursor_context_missing: 无法从当前回合恢复该 cursor "
            "对应的查询计划；请在发起查询的同一回合内续页，或重新发起查询")

    base_ot = scope.require_object_type(args.get("object_type", ""))

    # ---- 源与关联展开：s0 为主对象，joins 依序链式引入 s1..sN ----
    aliases: dict[str, str] = {base_ot.id: "s0"}
    label_of: dict[str, str] = {"s0": base_ot.display_name}
    sources = [QS.QuerySource(alias="s0", ontology_id=scope.ontology.id,
                              release_id=scope.release_id,
                              object_type_id=base_ot.id)]
    joins = []
    join_args = args.get("joins") or []
    if not isinstance(join_args, list):
        join_args = []
    if len(join_args) > 6:
        raise ToolError("query_objects_invalid_query: joins 最多 6 跳")
    prev_alias, prev_ot = "s0", base_ot
    for index, join in enumerate(join_args):
        if not isinstance(join, dict):
            raise ToolError("query_objects_invalid_query: joins 每项需为 "
                            "{object_type, link_type, type?}")
        ot = _resolve_type_reference(scope, join.get("object_type"))
        if ot is None:
            raise ToolError("query_objects_invalid_query: joins 缺少 object_type")
        if ot.id in aliases:
            raise ToolError(f"query_objects_invalid_query: 对象类型「{ot.display_name}」"
                            "重复出现；同一类型只允许参与一次关联")
        lt = scope.require_link_type(join.get("link_type", ""))
        alias = f"s{index + 1}"
        direction = ("source_to_target"
                     if lt.source_object_type_id == prev_ot.id
                     else "target_to_s")
        join_type = join.get("type") if join.get("type") in {"inner", "left"} else "inner"
        sources.append(QS.QuerySource(
            alias=alias, ontology_id=scope.ontology.id,
            release_id=scope.release_id, object_type_id=ot.id))
        joins.append(QS.QueryJoin(
            left=prev_alias, right=alias, type=join_type,
            on=QS.DeclaredRelationOn(kind="declared_relation",
                                     link_type_id=lt.id, direction=direction)))
        aliases[ot.id] = alias
        label_of[alias] = ot.display_name
        prev_alias, prev_ot = alias, ot

    def alias_for(ref) -> tuple[str, object]:
        ot = _resolve_type_reference(scope, ref, default=base_ot)
        if ot is None or ot.id not in aliases:
            raise ToolError(f"query_objects_invalid_query: 对象类型「{ref}」未在"
                            "主对象或 joins 中出现")
        return aliases[ot.id], ot

    def resolve_prop(ot, prop):
        return scope.resolve_visible_property(ot, prop)

    # ---- 过滤：object_type 缺省为主对象 ----
    where = []
    for item in args.get("filters") or []:
        if not isinstance(item, dict) or not item.get("property"):
            continue
        alias, ot = alias_for(item.get("object_type"))
        prop = resolve_prop(ot, item["property"])
        where.append(_leaf(alias, prop, item))

    # ---- 服务端相对时间窗口：一次解析为绝对 [start, end) ----
    resolved_window = None
    raw_window = args.get("window")
    if raw_window is not None:
        if not isinstance(raw_window, dict):
            raise ToolError("query_objects_invalid_query: window 必须是对象")
        wanted_type = raw_window.get("object_type")
        alias, ot = alias_for(wanted_type)
        prop = resolve_prop(ot, raw_window.get("property", ""))
        window_input = {key: value for key, value in raw_window.items()
                        if key != "object_type"}
        window_input["property"] = prop
        resolved_window = resolve_window(
            window_input, property_name=prop,
            property_type=_definition_type(ot, prop))
        where.extend(window_filters(resolved_window, source=alias))

    # ---- 分组/聚合/投影装配 ----
    group_items = []       # dict(alias, property, bucket, readable)
    for item in (args.get("group_by") or []):
        if isinstance(item, str):
            item = {"property": item}
        if not isinstance(item, dict) or not item.get("property"):
            continue
        alias, ot = alias_for(item.get("object_type"))
        prop = resolve_prop(ot, item["property"])
        bucket = item.get("bucket")
        if bucket is not None and bucket not in _BUCKETS:
            raise ToolError(f"query_objects_invalid_query: 不支持的时间分桶 {bucket}")
        group_items.append({"source": alias, "property": prop,
                            "bucket": bucket, "readable": str(item["property"])})
    aggregate_items = []   # dict(fn, source, property, readable)
    for item in (args.get("aggregates") or []):
        if not isinstance(item, dict) or item.get("fn") not in _AGGREGATE_FNS:
            raise ToolError(f"query_objects_invalid_query: aggregates.fn 需为 "
                            f"{'/'.join(sorted(_AGGREGATE_FNS))}")
        fn = item["fn"]
        prop_ref = item.get("property")
        alias, ot = (None, None) if fn == "count" and not prop_ref \
            else alias_for(item.get("object_type"))
        prop = None
        if prop_ref:
            prop = resolve_prop(ot, prop_ref)
        readable = item.get("as") or (fn if prop is None else f"{fn}_{prop}")
        aggregate_items.append({"fn": fn, "source": alias, "property": prop,
                                "readable": str(readable)})

    stats_mode = bool(group_items or aggregate_items)
    readable_of: dict[str, str] = {}
    select, group_by, aggregates = [], [], []
    select_entries: list[tuple[str, str, object]] = []
    if stats_mode:
        for index, item in enumerate(group_items):
            out = f"g{index}"
            select.append(QS.SelectItem(source=item["source"],
                                        property=item["property"], as_=out))
            group_by.append(QS.GroupByItem(
                source=item["source"], property=item["property"],
                **({"bucket": item["bucket"]} if item["bucket"] else {})))
            readable_of[out] = item["readable"]
        for index, item in enumerate(aggregate_items):
            out = f"a{index}"
            aggregates.append(QS.AggregateItem(
                fn=item["fn"], source=item["source"],
                property=item["property"], as_=out))
            readable_of[out] = item["readable"]
    else:
        select_args = args.get("select") or None
        if select_args is None:
            for prop in _visible_properties(scope, base_ot):
                select_entries.append(("s0", prop, base_ot))
        else:
            for item in select_args:
                if isinstance(item, str):
                    item = {"property": item}
                if not isinstance(item, dict) or not item.get("property"):
                    continue
                alias, ot = alias_for(item.get("object_type"))
                select_entries.append(
                    (alias, resolve_prop(ot, item["property"]), ot))
        if not select_entries:
            raise ToolError("query_objects_invalid_query: 没有可输出的授权属性")
        names = [prop for _a, prop, _o in select_entries]
        for index, (alias, prop, ot) in enumerate(select_entries):
            out = f"c{index}"
            # 多源出现同名属性时用「类型.属性」消歧，主对象保持裸属性名。
            readable = prop if names.count(prop) == 1 or alias == "s0" \
                else f"{label_of[alias]}.{prop}"
            select.append(QS.SelectItem(source=alias, property=prop, as_=out))
            readable_of[out] = readable

    order_items: list[dict] = []
    readable_outputs = dict(readable_of)
    for item in args.get("order_by") or []:
        if not isinstance(item, dict) or not item.get("output"):
            continue
        name = str(item["output"])
        out = next((key for key, label in readable_outputs.items()
                    if label == name or key == name), None)
        if out is None:
            raise ToolError(f"query_objects_invalid_query: 排序引用未知输出「{name}」；"
                            f"可用：{'、'.join(sorted(set(readable_outputs.values())))}")
        direction = item.get("direction", "asc")
        if direction not in {"asc", "desc"}:
            raise ToolError("query_objects_invalid_query: 排序方向需为 asc/desc")
        order_items.append({"output": out, "readable": readable_of[out],
                            "direction": direction})

    try:
        requested = int(args.get("page_size") or 50)
    except (TypeError, ValueError):
        requested = 50
    page_size = max(1, min(requested, cap))

    page_fetch = False
    spec = None
    detail_request = None
    try:
        if True:
            request = QS.QueryRequest(
                sources=sources, joins=joins, where=where, select=select,
                group_by=group_by, aggregates=aggregates,
                order_by=[QS.OrderByItem(output=item["output"],
                                         direction=item["direction"])
                          for item in order_items],
                page_size=page_size)
            if stats_mode:
                detail_select = [
                    QS.SelectItem(source="s0", property=prop, as_=f"c{index}")
                    for index, prop in enumerate(_visible_properties(scope, base_ot))
                ]
                if not detail_select:
                    detail_select = [QS.SelectItem(
                        source=group_items[0]["source"],
                        property=group_items[0]["property"], as_="c0")]
                detail_request = QS.QueryRequest(
                    sources=sources, joins=joins, where=where,
                    select=detail_select)
            description = _build_description(
                base_ot=base_ot, stats_mode=stats_mode, where=where,
                label_of=label_of,
                joined_labels=[label_of[f"s{i}"] for i in range(1, len(sources))],
                select_entries=select_entries, group_items=group_items,
                aggregate_items=aggregate_items, order_by=order_items,
                resolved_window=resolved_window, page_size=page_size)
            spec = _spec_from_request(
                request=request, scope=scope, base_ot=base_ot,
                labels=readable_of, resolved_window=resolved_window,
                description=description, stats_mode=stats_mode,
                detail_request=detail_request)
            data = execute_unified_query(
                db, user, request, session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
    except ValidationError as exc:
        raise ToolError(f"query_objects_invalid_query: {exc}") from exc
    except QueryError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc

    _release_terminal_page(db, user, session_id, data)
    labels = (spec or {}).get("labels") or {}
    columns = build_columns(data.get("columns"), labels)
    raw_rows = list(data.get("rows") or [])
    if stats_mode:
        if spec is not None:
            _attach_drilldowns(
                rows=raw_rows, group_items=group_items, spec=spec,
                resolved_window=resolved_window, user=user,
                session_id=session_id)
        else:
            for row in raw_rows:
                row["drilldownUnavailable"] = (
                    "cursor_page_without_previous_spec")
    rows = map_rows(raw_rows, labels)
    joined_labels = [label_of[f"s{i}"] for i in range(1, len(sources))]
    out = {
        "shape": "objects",
        "objectType": base_ot.display_name,
        "joinedObjectTypes": joined_labels,
        "dataMode": _data_mode(data),
        "columns": columns,
        "rows": rows,
        "returned": len(rows),
        "totalRowCount": encode_scalar(data.get("totalRowCount"))
        if data.get("totalRowCount") is not None else None,
        "rowCounting": data.get("rowCounting"),
        "hasNext": bool(data.get("hasNext")),
        "cursor": data.get("cursor") if data.get("hasNext") else None,
        "truncated": bool(data.get("hasNext")),
        "exact": data.get("exact") is not False,
        "partial": bool(data.get("partial")),
        "completeness": data.get("completeness", "complete"),
        "completenessScope": "query_execution",
        "pageSize": data.get("pageSize", page_size),
        "queryId": data.get("queryId"),
        "observedAt": data.get("observedAt"),
        "releaseId": scope.release_id,
        "resolvedWindow": window_for_result(resolved_window),
        "consistency": _consistency(data, page_fetch=page_fetch),
    }
    if stats_mode:
        out["groupBy"] = [
            (f"{item['readable']}({_BUCKET_UNITS[item['bucket']]})"
             if item.get("bucket") else item["readable"])
            for item in group_items
        ]
        out["groupBuckets"] = [item.get("bucket") for item in group_items]
        out["groupValues"] = [
            [row.get(f"g{index}") for index in range(len(group_items))]
            for row in (data.get("rows") or [])
        ]
    if stats_mode and len(group_items) == 1 and aggregate_items:
        metric_item = aggregate_items[0]
        metric_value = metric_item["fn"]
        group_prop = group_items[0]["readable"]
        group_label = (f"{group_prop}({_BUCKET_UNITS[group_items[0]['bucket']]})"
                       if group_items[0].get("bucket") else group_prop)
        value_label = metric_item["readable"]
        points = [
            {"group": row.get(group_prop), "value": row.get(value_label)}
            for row in rows
        ]
        if all(isinstance(point["value"], (int, float))
               and not isinstance(point["value"], bool)
               for point in points):
            try:
                chart = _build_aggregate_chart(
                    base_ot.display_name, metric_value,
                    metric_item.get("property"), group_label, points)
                if chart:
                    out["chart"] = chart
                    out["chartRendered"] = True
            except Exception:  # noqa: BLE001 - 图表是可选的呈现层
                pass
    if spec is not None:
        out["querySpec"] = spec
        out["querySpecHash"] = spec_hash(spec)
        if out["cursor"] and spec_cache is not None:
            spec_cache[out["cursor"]] = spec
    return out
