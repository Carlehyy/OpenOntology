"""本体助手的统一查询工具：明细/排序/分页/按 ID/多维分组/关联统计与数据时间范围。

两个工具都经由 execute_unified_query 的正门：远程绑定对象转远程统一查询
（源端 SQL），本地物化对象走本地快照路径 —— 授权、发布校验、join 证据、
内存预算全部复用既有链路，本模块只做工具参数 → QueryRequest 的装配与
响应的可读化映射。分页句柄遵循终页回收语义（hasNext=false 时释放）。
"""
from __future__ import annotations

from pydantic import ValidationError

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.read_queries import encode_scalar

_TEMPORAL_TYPES = {"date", "datetime", "timestamp", "time"}
_FILTER_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "contains", "in",
               "is_null", "is_not_null"}
_AGGREGATE_FNS = {"count", "count_distinct", "sum", "avg", "min", "max"}


def _leaf(alias: str, prop: str, item: dict) -> dict:
    op = item.get("op", "eq")
    if op not in _FILTER_OPS:
        raise ToolError(f"query_objects_invalid_filter: 不支持的过滤操作符 {op}；"
                        f"可用 {'/'.join(sorted(_FILTER_OPS))}")
    value = item.get("value")
    if op in {"is_null", "is_not_null"}:
        return {"source": alias, "property": prop, "op": op}
    if op == "in" and not isinstance(value, list):
        value = [value]
    if op == "contains" and value is None:
        raise ToolError("query_objects_invalid_filter: contains 需要非空取值")
    return {"source": alias, "property": prop, "op": op, "value": value}


def _remote_bound_properties(scope, ot) -> set[str] | None:
    """远程绑定类型可查询属性 = 授权属性 ∩ 绑定列映射；本地类型返回 None。"""
    if scope.release is None:
        return None
    from app.ontologies.mappings.remote_binding import (
        RemoteBindingError, get_remote_binding,
    )
    try:
        binding = get_remote_binding(scope.release.snapshot, ot.id)
    except RemoteBindingError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    if binding is None:
        return None
    props = {prop for prop in binding.get("fieldMap", {}).values()
             if prop != "__primary_key__"}
    return props


def _resolve_type_reference(scope, ref, default=None):
    if ref in (None, ""):
        return default
    return scope.require_object_type(ref)


def _visible_properties(scope, ot) -> list[str]:
    allowed = scope.allowed_property_names(ot.id)
    definitions = {p.get("name"): p for p in (ot.properties or [])
                   if isinstance(p, dict) and p.get("name")}
    names = [name for name in definitions
             if allowed is None or name in allowed]
    remote_bound = _remote_bound_properties(scope, ot)
    if remote_bound is not None:
        names = [name for name in names if name in remote_bound]
    return names


def query_objects(db, scope, user, session_id: str, args: dict) -> dict:
    """明细/排序/分页/多维分组/关联统计的统一查询工具。"""
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import (
        QueryError, execute_unified_query, fetch_unified_query_page,
        release_query_snapshot,
    )
    from app.ontologies.agent_query.remote_query import cancel_remote_query

    if user is None:
        raise ToolError("remote_query_user_required: 统一查询需要已认证用户")
    if scope.release_id is None:
        raise ToolError("query_objects_release_required: 统一查询需要固定的发布版本；"
                        "请从已发布本体进入助手后重试")
    cap = min(200, int(scope.profile.max_rows_per_query or 0))
    if cap <= 0:
        raise ToolError("profile_query_disabled: 当前本体助手未启用查询额度")

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

    # ---- 分组/聚合/投影装配 ----
    group_items = []       # (alias, prop, bucket, readable)
    for item in (args.get("group_by") or []):
        if isinstance(item, str):
            item = {"property": item}
        if not isinstance(item, dict) or not item.get("property"):
            continue
        alias, ot = alias_for(item.get("object_type"))
        prop = resolve_prop(ot, item["property"])
        bucket = item.get("bucket")
        if bucket is not None and bucket not in {"day", "week", "month", "quarter", "year"}:
            raise ToolError(f"query_objects_invalid_query: 不支持的时间分桶 {bucket}")
        group_items.append((alias, prop, bucket, str(item["property"])))
    aggregate_items = []   # (fn, alias, prop, readable)
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
        aggregate_items.append((fn, alias, prop, readable))

    stats_mode = bool(group_items or aggregate_items)
    readable_of: dict[str, str] = {}
    select, group_by, aggregates = [], [], []
    if stats_mode:
        for index, (alias, prop, bucket, readable) in enumerate(group_items):
            out = f"g{index}"
            select.append(QS.SelectItem(source=alias, property=prop, as_=out))
            group_by.append(QS.GroupByItem(
                source=alias, property=prop,
                **({"bucket": bucket} if bucket else {})))
            readable_of[out] = readable
        for index, (fn, alias, prop, readable) in enumerate(aggregate_items):
            out = f"a{index}"
            aggregates.append(QS.AggregateItem(
                fn=fn, source=alias, property=prop, as_=out))
            readable_of[out] = readable
    else:
        select_args = args.get("select") or None
        entries: list[tuple[str, str, object]] = []
        if select_args is None:
            for prop in _visible_properties(scope, base_ot):
                entries.append(("s0", prop, base_ot))
        else:
            for item in select_args:
                if isinstance(item, str):
                    item = {"property": item}
                if not isinstance(item, dict) or not item.get("property"):
                    continue
                alias, ot = alias_for(item.get("object_type"))
                entries.append((alias, resolve_prop(ot, item["property"]), ot))
        if not entries:
            raise ToolError("query_objects_invalid_query: 没有可输出的授权属性")
        names = [prop for _a, prop, _o in entries]
        for index, (alias, prop, ot) in enumerate(entries):
            out = f"c{index}"
            # 多源出现同名属性时用「类型.属性」消歧，主对象保持裸属性名。
            readable = prop if names.count(prop) == 1 or alias == "s0" \
                else f"{label_of[alias]}.{prop}"
            select.append(QS.SelectItem(source=alias, property=prop, as_=out))
            readable_of[out] = readable

    order_by = []
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
        order_by.append(QS.OrderByItem(
            output=out, direction=item.get("direction", "asc")))

    try:
        requested = int(args.get("page_size") or 50)
    except (TypeError, ValueError):
        requested = 50
    page_size = max(1, min(requested, cap))

    cursor = args.get("cursor")
    try:
        if cursor:
            data = fetch_unified_query_page(
                db, user, str(cursor), session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
        else:
            request = QS.QueryRequest(
                sources=sources, joins=joins, where=where, select=select,
                group_by=group_by, aggregates=aggregates, order_by=order_by,
                page_size=page_size)
            data = execute_unified_query(
                db, user, request, session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
    except ValidationError as exc:
        raise ToolError(f"query_objects_invalid_query: {exc}") from exc
    except QueryError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc

    # 终页回收：hasNext=false 时释放快照/句柄；失败不影响成功查询。
    if data.get("hasNext") is False and data.get("queryId"):
        try:
            if isinstance(data.get("persistence"), dict):
                cancel_remote_query(db, user, str(data["queryId"]))
            else:
                released = release_query_snapshot(
                    db, user, str(data["queryId"]),
                    session_kind="ontology_agent", session_id=session_id or "")
                if not released:
                    cancel_remote_query(db, user, str(data["queryId"]))
        except Exception:  # noqa: BLE001 - 回收失败不破坏成功查询
            pass

    rows = []
    for row in data.get("rows") or []:
        rows.append({readable_of.get(key, key): value
                     for key, value in row.items()})
    data_modes = sorted({entry.get("dataMode")
                         for entry in (data.get("releases") or {}).values()
                         if entry.get("dataMode")})
    joined_labels = [label_of[f"s{i}"] for i in range(1, len(sources))]
    return {
        "objectType": base_ot.display_name,
        "joinedObjectTypes": joined_labels,
        "dataMode": "/".join(data_modes) if data_modes else "materialized",
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
    }


def get_data_time_range(db, scope, user, session_id: str, ot,
                        time_property=None, filters=None) -> dict:
    """远程绑定对象的数据时间范围（min/max 一次双聚合下推源端）。"""
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import QueryError, execute_unified_query
    from app.ontologies.agent_query.remote_query import cancel_remote_query
    from app.ontologies.agent_runtime.remote_aggregation import _filter

    if user is None:
        raise ToolError("remote_query_user_required: 远程统计需要已认证用户")
    cap = int(scope.profile.max_rows_per_query or 0)
    if cap <= 0:
        raise ToolError("profile_query_disabled: 当前本体助手未启用查询额度")
    if scope.release is None:
        raise ToolError("data_time_range_release_required: 需要固定的发布版本")
    from app.ontologies.mappings.remote_binding import (
        RemoteBindingError, get_remote_binding,
    )
    try:
        binding = get_remote_binding(scope.release.snapshot, ot.id)
    except RemoteBindingError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    if binding is None:
        raise ToolError("data_time_range_local_unsupported: 数据时间范围工具面向"
                        "远程绑定对象；本地物化对象请用 aggregate_objects 按时间"
                        "属性 min/max 统计")

    definitions = {p.get("name"): p for p in (ot.properties or [])
                   if isinstance(p, dict) and p.get("name")}
    temporal = [name for name, p in definitions.items()
                if str(p.get("type") or "").lower() in _TEMPORAL_TYPES]
    remote_bound = {prop for prop in binding.get("fieldMap", {}).values()
                    if prop != "__primary_key__"}
    temporal = [name for name in temporal if name in remote_bound]
    if not temporal:
        raise ToolError(f"data_time_range_no_temporal: 对象「{ot.display_name}」没有"
                        "可用的 date/datetime/timestamp 映射属性")
    prop = time_property or temporal[0]
    if prop not in definitions:
        # 允许 displayName 引用
        prop = scope.resolve_visible_property(ot, prop)
    if prop not in temporal:
        raise ToolError(f"data_time_range_not_temporal: 属性「{prop}」不是时间类型；"
                        f"可用：{'、'.join(temporal)}")

    resolved_filters = []
    for item in (filters or []):
        if isinstance(item, dict) and item.get("op") == "contains":
            raise ToolError("data_time_range_invalid_filter: 时间范围查询不支持 "
                            "contains；请使用明确的等值、范围或 in 条件")
        resolved_filters.append(item)
    payload = {
        "sources": [{"alias": "objects", "ontologyId": scope.ontology.id,
                     "releaseId": scope.release_id, "objectTypeId": ot.id}],
        "where": [_filter(item) for item in resolved_filters],
        "aggregates": [
            {"fn": "min", "source": "objects", "property": prop, "as": "minValue"},
            {"fn": "max", "source": "objects", "property": prop, "as": "maxValue"}],
        "pageSize": 1,
    }
    try:
        request = QS.QueryRequest.model_validate(payload)
        result = execute_unified_query(db, user, request, session_kind="ontology_agent",
                                       session_id=session_id or "")
        cancel_remote_query(db, user, result["queryId"])
    except ValidationError as exc:
        raise ToolError(f"data_time_range_invalid_query: {exc}") from exc
    except QueryError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    if (result.get("partial") or result.get("exact") is False
            or result.get("completeness", "complete") != "complete"):
        raise ToolError("data_time_range_incomplete: 远程查询未返回完整时间范围")
    rows = result["rows"]
    if len(rows) != 1:
        raise ToolError("data_time_range_incomplete: 远程查询未返回完整时间范围")
    row = rows[0]
    empty = row.get("minValue") is None and row.get("maxValue") is None
    return {
        "objectType": ot.display_name,
        "property": prop,
        "min": row.get("minValue"),
        "max": row.get("maxValue"),
        "dataCoverage": {"zeroResult": empty,
                         "reason": "source_no_data" if empty else "covered",
                         "note": "授权范围内远程源没有数据" if empty else
                         "源数据实际覆盖的最小/最大时间（按当前过滤条件）"},
        "partial": False, "exact": True, "completeness": "complete",
        "completenessScope": "query_execution",
        "dataMode": "remote", "releaseId": scope.release_id,
        "queryId": result["queryId"], "observedAt": result["observedAt"],
        "countsScope": result["countsScope"], "citations": result["citations"],
    }
