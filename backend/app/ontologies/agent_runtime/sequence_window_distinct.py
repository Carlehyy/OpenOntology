"""固定 30 分钟窗去重主体阈值统计（源端多阶段派生聚合）。

从 sequence_tools 拆出的独立职责：只负责把“同模块每固定 30 分钟窗内
distinct subject >= N”表达成统一查询的三层派生计划并汇总结果：

- 内层 object source：group by 模块 + half_hour 桶，
  count_distinct(subject)/count；
- 中层 derived source：where users>=threshold 后按模块 group，
  count(*) qualifyingWindows、max(users)；
- 总量 derived source：sum/count 得到 qualifyingWindows/qualifyingModules；
- 独立样本查询只取前 20 个达标窗口用于展示/下钻，不回传全部窗口组。

窗口粒度只承诺固定 30 分钟；其它粒度明确拒绝。half_hour 桶按存储
timestamp 的 UTC 半点取整，展示时转换到 resolvedWindow.timezone。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.query_spec import (
    build_query_spec, make_drilldown, spec_hash,
)
from app.ontologies.agent_runtime.query_window import (
    bucket_bounds, window_for_result,
)
from app.ontologies.agent_runtime.read_queries import encode_scalar

_WINDOW_BUCKET = "half_hour"
_WINDOW_MINUTES = 30
_WINDOW_MODULE_LIMIT = 200
_WINDOW_SAMPLE_LIMIT = 20


def _remote_time_column_timezone(snapshot, object_type_id: str,
                                 time_property: str):
    """远程绑定里 time_property 对应列的权威 timezone 契约。

    返回 True（TIMESTAMP/timestamptz 等带时区列）、False（DATETIME/
    timestamp without time zone 等无时区列）或 None（无远程绑定/列映射
    不唯一/旧快照缺少 timezone 元数据）。None 时调用方必须保留引擎返回值
    原样，绝不猜测时区。
    """
    from app.ontologies.mappings.remote_binding import (
        RemoteBindingError, get_remote_binding,
    )
    try:
        binding = get_remote_binding(snapshot, object_type_id)
    except RemoteBindingError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    if binding is None:
        return None
    columns = {
        str(item.get("name")): item
        for item in binding.get("columns") or []
        if isinstance(item, dict) and item.get("name")
    }
    mapped = [str(column) for column, prop in
              (binding.get("fieldMap") or {}).items()
              if str(prop) == time_property]
    if len(mapped) != 1:
        return None
    column = columns.get(mapped[0]) or {}
    timezone_flag = column.get("timezone")
    return bool(timezone_flag) if timezone_flag is not None else None


def _time_column_timezone(scope, ot, time_property: str):
    release = getattr(scope, "release", None)
    if release is None:
        return None
    return _remote_time_column_timezone(
        release.snapshot, ot.id, time_property)


def _window_condition_value(value, column_has_timezone):
    """把桶边界编码为与时间列 timezone 契约一致的过滤值。

    - True：带时区列 -> 使用 UTC aware ISO（``+00:00``）；
    - False：无时区列 -> 去偏移的 naive ISO；
    - None：保留引擎返回值的原始 tzinfo，不猜测。
    """
    if value is None:
        return None
    if column_has_timezone is True:
        if not isinstance(value, datetime):
            return str(value)
        normalized = (value.replace(tzinfo=timezone.utc)
                      if value.tzinfo is None
                      else value.astimezone(timezone.utc))
        return normalized.isoformat()
    if column_has_timezone is False:
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return _window_value_text(value)


def _window_value_text(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _window_display_bounds(value, timezone_name):
    """源端 half_hour 桶值 → 展示用窗口边界。

    引擎按存储 timestamp 的 UTC 半点取整（MySQL 会话时区固定 UTC）；这里
    把边界转换到 resolvedWindow.timezone 展示，同时保留 UTC 边界供核对。
    """
    bounds = bucket_bounds(value, _WINDOW_BUCKET)
    if bounds is None:
        return None
    start, end = bounds
    start_utc = (start.replace(tzinfo=timezone.utc)
                 if start.tzinfo is None else start.astimezone(timezone.utc))
    end_utc = (end.replace(tzinfo=timezone.utc)
               if end.tzinfo is None else end.astimezone(timezone.utc))
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(str(timezone_name)) if timezone_name else timezone.utc
    except Exception:  # noqa: BLE001 - resolvedWindow 已校验时区
        zone = timezone.utc
    return {
        "start": start_utc.astimezone(zone),
        "end": end_utc.astimezone(zone),
        "startUtc": start_utc,
        "endUtc": end_utc,
    }


def analyze_window_distinct(*, db, scope, user, session_id, ot, args,
                            subject_prop, time_prop, time_property_type,
                            threshold, where, resolved_window, cap,
                            cancel_handle) -> dict:
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import (
        QueryError, execute_unified_query,
    )
    from app.ontologies.agent_runtime.query_handles import (
        _release_terminal_page,
    )

    try:
        window_minutes = int(args.get("window_minutes") or _WINDOW_MINUTES)
    except (TypeError, ValueError) as exc:
        raise ToolError(
            "sequence_invalid_window: window_minutes 必须是整数") from exc
    if window_minutes != _WINDOW_MINUTES:
        raise ToolError(
            "sequence_window_minutes_unsupported: 当前仅支持固定 30 分钟窗口"
            "（window_minutes=30）；其它窗口粒度不提供近似或拆分结果")
    if resolved_window is None:
        raise ToolError(
            "sequence_window_required: window_distinct 需要明确的时间窗口"
            "（start/end 或 last）；没有绝对窗口边界不能给固定窗口统计")
    if time_property_type not in {"datetime", "timestamp"}:
        raise ToolError(
            "sequence_window_time_property: 固定 30 分钟窗要求 time_property "
            "为 datetime/timestamp 类型")

    raw_groups = args.get("group_by") or []
    if raw_groups and not isinstance(raw_groups, list):
        raise ToolError("sequence_invalid_group: group_by 必须是数组")
    group_items: list[dict] = []
    seen: set[str] = set()
    for item in raw_groups:
        if isinstance(item, str):
            item = {"property": item}
        if not isinstance(item, dict) or not item.get("property"):
            raise ToolError("sequence_invalid_group: group_by 项需要 property")
        if item.get("bucket") is not None:
            raise ToolError(
                "sequence_invalid_group: window_distinct 的 group_by 只接受原始"
                "属性（模块维度）；时间窗由 window_minutes 固定")
        prop = scope.resolve_visible_property(ot, item["property"])
        if prop in seen:
            continue
        seen.add(prop)
        group_items.append({
            "source": "s0", "property": prop,
            "readable": str(item["property"]),
        })

    # QueryRequest 协议上限在构造前显式归一为工具错误：groupBy 最多 8
    # （内层 half_hour 时间桶占用 1 个槽，模块维度最多 7）；where 最多 16
    # （窗口边界已占 2 个槽，filters 最多 14）。不做预算扩张。
    if len(group_items) > 7:
        raise ToolError(
            "sequence_window_group_budget_exceeded: window_distinct 的模块分组"
            f"维度最多 7 个（内层 half_hour 时间桶占用 1 个分组槽，单请求上限 8；"
            f"当前 {len(group_items)} 个）；请减少 group_by 后重试")
    if len(where) > 16:
        raise ToolError(
            "sequence_window_filter_budget_exceeded: window_distinct 的过滤条件"
            f"超过单请求上限 16（当前 {len(where)} 个；窗口边界占用 2 个槽，"
            "filters 最多 14 个）；请减少 filters 后重试")

    source = QS.QuerySource(alias="s0", ontology_id=scope.ontology.id,
                            release_id=scope.release_id, object_type_id=ot.id)
    inner_where = [QS.FilterLeaf.model_validate(item) for item in where]
    window_output = f"g{len(group_items)}"
    inner_select = [
        QS.SelectItem(source="s0", property=item["property"], as_=f"g{index}")
        for index, item in enumerate(group_items)
    ]
    inner_select.append(QS.SelectItem(
        source="s0", property=time_prop, as_=window_output))
    inner_group_by = [
        QS.GroupByItem(source="s0", property=item["property"])
        for item in group_items
    ]
    inner_group_by.append(QS.GroupByItem(
        source="s0", property=time_prop, bucket=_WINDOW_BUCKET))
    inner_request = QS.QueryRequest(
        sources=[source], where=inner_where,
        select=inner_select, group_by=inner_group_by,
        aggregates=[
            QS.AggregateItem(fn="count_distinct", source="s0",
                             property=subject_prop, as_="users"),
            QS.AggregateItem(fn="count", as_="rows"),
        ],
        page_size=max(1, min(200, cap)),
    )

    def run(request, label: str) -> dict:
        try:
            return execute_unified_query(
                db, user, request, session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
        except Exception as exc:  # noqa: BLE001 - 统一错误码透传
            if isinstance(exc, QueryError):
                raise ToolError(f"{exc.code}: {exc.message}") from exc
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                raise ToolError(f"sequence_invalid_query: {exc}") from exc
            raise

    def ensure_complete(data: dict, label: str) -> None:
        if (data.get("partial") or data.get("exact") is False
                or data.get("completeness", "complete") != "complete"):
            raise ToolError(
                f"window_distinct_incomplete: {label} 未返回完整聚合结果；"
                "拒绝用部分结果给出窗口结论")

    agg_alias = "agg"
    module_limit = max(1, min(_WINDOW_MODULE_LIMIT, cap))
    sample_limit = max(1, min(_WINDOW_SAMPLE_LIMIT, cap))
    module_outputs = [f"g{index}" for index in range(len(group_items))]
    qualifying_where = [QS.FilterLeaf(
        source=agg_alias, property="users", op="gte", value=threshold)]

    if group_items:
        middle_request = QS.QueryRequest(
            sources=[QS.QuerySource(alias=agg_alias, subquery=inner_request)],
            where=qualifying_where,
            select=[QS.SelectItem(source=agg_alias, property=output,
                                  as_=output)
                    for output in module_outputs],
            group_by=[QS.GroupByItem(source=agg_alias, property=output)
                      for output in module_outputs],
            aggregates=[
                QS.AggregateItem(fn="count", as_="qualifyingWindows"),
                QS.AggregateItem(fn="max", source=agg_alias,
                                 property="users",
                                 as_="maxDistinctSubjects"),
            ],
            order_by=[
                QS.OrderByItem(output="qualifyingWindows", direction="desc"),
                QS.OrderByItem(output="maxDistinctSubjects", direction="desc"),
                *[QS.OrderByItem(output=output, direction="asc")
                  for output in module_outputs],
            ],
            page_size=module_limit,
            bounded_stats=True,
        )
        module_data = run(middle_request, "模块窗口汇总")
        ensure_complete(module_data, "模块窗口汇总")
        _release_terminal_page(db, user, session_id, module_data)
        module_rows = list(module_data.get("rows") or [])

        totals_request = QS.QueryRequest(
            sources=[QS.QuerySource(alias="per_module",
                                    subquery=middle_request)],
            aggregates=[
                QS.AggregateItem(fn="sum", source="per_module",
                                 property="qualifyingWindows",
                                 as_="qualifyingWindows"),
                QS.AggregateItem(fn="count", as_="qualifyingModules"),
            ],
            page_size=1,
        )
        totals_data = run(totals_request, "窗口总数汇总")
        ensure_complete(totals_data, "窗口总数汇总")
        _release_terminal_page(db, user, session_id, totals_data)
        totals_rows = list(totals_data.get("rows") or [])
        if len(totals_rows) != 1:
            raise ToolError(
                "window_distinct_incomplete: 窗口总数汇总未返回单行标量")
        total_windows = int(Decimal(str(
            totals_rows[0].get("qualifyingWindows") or 0)))
        total_modules = int(Decimal(str(
            totals_rows[0].get("qualifyingModules") or 0)))
        reported_modules = module_data.get("totalRowCount")
        if reported_modules is not None and int(reported_modules) != total_modules:
            raise ToolError(
                "window_distinct_incomplete: 模块汇总与模块总数不一致"
                f"（{reported_modules}/{total_modules}）")
        module_spec_request = middle_request
        module_spec_labels = {
            **{output: item["readable"]
               for output, item in zip(module_outputs, group_items)},
            "qualifyingWindows": "达标窗口数",
            "maxDistinctSubjects": "最大去重主体数",
        }
    else:
        totals_request = QS.QueryRequest(
            sources=[QS.QuerySource(alias=agg_alias, subquery=inner_request)],
            where=qualifying_where,
            aggregates=[
                QS.AggregateItem(fn="count", as_="qualifyingWindows"),
                QS.AggregateItem(fn="max", source=agg_alias,
                                 property="users",
                                 as_="maxDistinctSubjects"),
            ],
            page_size=1,
        )
        totals_data = run(totals_request, "窗口总数汇总")
        ensure_complete(totals_data, "窗口总数汇总")
        _release_terminal_page(db, user, session_id, totals_data)
        totals_rows = list(totals_data.get("rows") or [])
        if len(totals_rows) != 1:
            raise ToolError(
                "window_distinct_incomplete: 窗口总数汇总未返回单行标量")
        total_windows = int(Decimal(str(
            totals_rows[0].get("qualifyingWindows") or 0)))
        total_modules = 1 if total_windows else 0
        module_rows = []
        if total_windows:
            max_value = totals_rows[0].get("maxDistinctSubjects")
            module_rows = [{
                "g0": None,
                "qualifyingWindows": total_windows,
                "maxDistinctSubjects": max_value,
            }]
        module_spec_request = None
        module_spec_labels = {}

    # 主 querySpec 必须能重放 headline 精确总量（totals_request），而不是
    # 只重放模块分组视图；模块视图作为独立 moduleQuerySpec 提供给 UI。
    spec_request = totals_request
    if group_items:
        spec_labels = {
            "qualifyingWindows": "达标窗口数",
            "qualifyingModules": "达标模块数",
        }
        qualifying_modules_scope = "source_aggregate"
    else:
        # 无模块维度时 totals_request 只能源端算出 qualifyingWindows 与
        # maxDistinctSubjects；qualifyingModules=1/0 由本地根据前者推导，
        # 不在 spec 的聚合输出中，必须显式标注口径。
        spec_labels = {
            "qualifyingWindows": "达标窗口数",
            "maxDistinctSubjects": "最大去重主体数",
        }
        qualifying_modules_scope = (
            "derived_from_qualifying_windows_no_module_dimension")

    modules = []
    for row in module_rows:
        values = [row.get(output) for output in module_outputs]
        max_value = row.get("maxDistinctSubjects")
        modules.append({
            "module": (" | ".join("（空）" if value is None else str(value)
                                  for value in values)
                       if values else "全部"),
            "moduleValues": values,
            "qualifyingWindows": encode_scalar(int(Decimal(str(
                row.get("qualifyingWindows") or 0)))),
            "maxDistinctSubjects": (
                encode_scalar(int(Decimal(str(max_value))))
                if max_value is not None else None),
        })
    # 源端 orderBy 已按 qualifyingWindows/maxDistinct 排序；这里再做一次
    # 确定性排序，确保任何执行端/分页顺序都不会把模块顺序反转。
    modules.sort(key=lambda item: (
        -int(item["qualifyingWindows"] or 0),
        -int(item["maxDistinctSubjects"] or 0),
        tuple("" if value is None else str(value)
              for value in item["moduleValues"]),
    ))

    sample_request = QS.QueryRequest(
        sources=[QS.QuerySource(alias=agg_alias, subquery=inner_request)],
        where=qualifying_where,
        select=[
            *[QS.SelectItem(source=agg_alias, property=output, as_=output)
              for output in module_outputs],
            QS.SelectItem(source=agg_alias, property=window_output,
                          as_="window"),
            QS.SelectItem(source=agg_alias, property="users", as_="users"),
            QS.SelectItem(source=agg_alias, property="rows", as_="rows"),
        ],
        order_by=[
            QS.OrderByItem(output="users", direction="desc"),
            QS.OrderByItem(output="window", direction="asc"),
            *[QS.OrderByItem(output=output, direction="asc")
              for output in module_outputs],
        ],
        page_size=sample_limit,
    )
    sample_data = run(sample_request, "窗口示例")
    ensure_complete(sample_data, "窗口示例")
    sample_rows = list(sample_data.get("rows") or [])
    sample_total = sample_data.get("totalRowCount")
    if sample_total is not None and int(sample_total) != total_windows:
        raise ToolError(
            "window_distinct_incomplete: 窗口示例总数与总量汇总不一致"
            f"（{sample_total}/{total_windows}）")
    if sample_data.get("hasNext"):
        cancel_handle(db, user, session_id, sample_data)
    else:
        _release_terminal_page(db, user, session_id, sample_data)

    detail_select = [
        QS.SelectItem(source="s0", property=subject_prop, as_="c0"),
        QS.SelectItem(source="s0", property=time_prop, as_="c1"),
    ]
    detail_select.extend(
        QS.SelectItem(source="s0", property=item["property"], as_=f"c{index + 2}")
        for index, item in enumerate(group_items))
    detail_request = QS.QueryRequest(
        sources=[source], where=inner_where, select=detail_select,
        page_size=max(1, min(50, cap)))
    description = {
        "semantics": "window_distinct",
        "objectType": ot.display_name,
        "subjectProperty": subject_prop,
        "timeProperty": time_prop,
        "threshold": threshold,
        "thresholdScope": "distinct_subjects_per_fixed_window",
        "windowMinutes": _WINDOW_MINUTES,
        "windowBucket": _WINDOW_BUCKET,
        "groupBy": [{"property": item["readable"]}
                    for item in group_items],
        "timeWindow": window_for_result(resolved_window),
        "qualifyingModulesScope": qualifying_modules_scope,
    }
    if not group_items:
        description["qualifyingModulesSemantics"] = (
            "没有模块维度：qualifyingModules=1（qualifyingWindows>0）或 0，"
            "由本地根据 qualifyingWindows 推导；主 querySpec 的聚合输出只含 "
            "qualifyingWindows/maxDistinctSubjects，不包含 qualifyingModules")
    spec = build_query_spec(
        shape="objects", ontology_id=scope.ontology.id,
        release_id=str(scope.release_id), request=spec_request,
        labels=spec_labels,
        resolved_window=window_for_result(resolved_window),
        description={**description, "view": "totals"},
        object_type=ot.id,
    )
    spec["drilldownBase"] = detail_request.model_dump(by_alias=True)
    spec["drilldownLabels"] = {
        "c0": subject_prop, "c1": time_prop,
        **{f"c{index + 2}": item["property"]
           for index, item in enumerate(group_items)},
    }
    base_hash = spec_hash(spec)
    module_spec = None
    if module_spec_request is not None:
        module_spec = build_query_spec(
            shape="objects", ontology_id=scope.ontology.id,
            release_id=str(scope.release_id), request=module_spec_request,
            labels=module_spec_labels,
            resolved_window=window_for_result(resolved_window),
            description={**description, "semantics": "window_distinct_modules",
                         "view": "modules"},
            object_type=ot.id,
        )
    samples = []
    timezone_name = (resolved_window or {}).get("timezone")
    time_column_timezone = _time_column_timezone(scope, ot, time_prop)
    for row in sample_rows:
        values = [row.get(output) for output in module_outputs]
        window_value = row.get("window")
        bounds = bucket_bounds(window_value, _WINDOW_BUCKET)
        display = _window_display_bounds(window_value, timezone_name)
        entry = {
            "module": (" | ".join("（空）" if value is None else str(value)
                                  for value in values)
                       if values else "全部"),
            "moduleValues": values,
            "windowStart": (_window_value_text(display["start"])
                            if display is not None else None),
            "windowEnd": (_window_value_text(display["end"])
                          if display is not None else None),
            "windowStartUtc": (_window_value_text(display["startUtc"])
                               if display is not None else None),
            "windowEndUtc": (_window_value_text(display["endUtc"])
                             if display is not None else None),
            "distinctSubjects": encode_scalar(int(Decimal(str(
                row.get("users") or 0)))),
            "candidateRows": encode_scalar(int(Decimal(str(
                row.get("rows") or 0)))),
        }
        if bounds is None:
            entry["drilldownUnavailable"] = (
                "window_bucket_boundary_unresolved")
        elif user is not None and session_id:
            conditions = []
            for item, value in zip(group_items, values):
                conditions.append(
                    {"source": "s0", "property": item["property"],
                     "op": "is_null"} if value is None else
                    {"source": "s0", "property": item["property"],
                     "op": "eq", "value": value})
            start, end = bounds
            start_value = _window_condition_value(start, time_column_timezone)
            end_value = _window_condition_value(end, time_column_timezone)
            conditions.extend([
                {"source": "s0", "property": time_prop, "op": "gte",
                 "value": start_value},
                {"source": "s0", "property": time_prop, "op": "lt",
                 "value": end_value},
            ])
            try:
                entry["drilldown"] = make_drilldown(
                    owner=str(getattr(user, "id", "")),
                    ontology_id=scope.ontology.id,
                    conversation_id=session_id,
                    base_spec_hash=base_hash,
                    conditions=conditions,
                    group_values=[*values, window_value],
                    group_by=[item["readable"] for item in group_items]
                    + [time_prop],
                    resolved_window=window_for_result(resolved_window),
                )
            except ToolError as exc:
                entry["drilldownUnavailable"] = str(exc)
        else:
            entry["drilldownUnavailable"] = "missing_conversation"
        samples.append(entry)

    return {
        "kind": "sequence_analysis",
        "mode": "window_distinct",
        "objectType": ot.display_name,
        "subjectProperty": subject_prop,
        "timeProperty": time_prop,
        "window": window_for_result(resolved_window),
        "windowMinutes": _WINDOW_MINUTES,
        "windowBucket": _WINDOW_BUCKET,
        "windowTimezone": timezone_name,
        "windowAlignment": (
            "half-hour tumbling windows on UTC/source timestamps; "
            "window timezone is used for range filtering and display "
            "(for offsets that are multiples of 30 minutes, local "
            "wall-clock half-hours coincide)"),
        "threshold": threshold,
        "thresholdScope": "distinct_subjects_per_fixed_window",
        "qualifyingWindows": encode_scalar(total_windows),
        "qualifyingModules": encode_scalar(total_modules),
        "qualifyingModulesScope": qualifying_modules_scope,
        "moduleCount": encode_scalar(total_modules),
        "modules": modules,
        "modulesTruncated": total_modules > len(modules),
        "moduleLimit": module_limit,
        "sampleWindows": samples,
        "sampleWindowsTruncated": total_windows > len(samples),
        "evaluatedWindows": None,
        "evaluatedWindowsAvailable": False,
        "exact": True,
        "partial": False,
        "completeness": "complete",
        "completenessScope": "query_execution",
        "dataMode": "remote",
        "querySpec": spec,
        "querySpecHash": base_hash,
        "moduleQuerySpec": module_spec,
        "moduleQuerySpecHash": (spec_hash(module_spec)
                                if module_spec is not None else None),
        "queryId": totals_data.get("queryId"),
        "observedAt": totals_data.get("observedAt"),
        "consistency": {
            "sameSnapshot": False,
            "queryFresh": True,
            "mode": "window_distinct_multi_query",
            "observedAt": totals_data.get("observedAt"),
            "note": ("模块汇总、总数与样本窗口是同一回合的多次源端聚合观测；"
                     "不是单次固定快照，也不是全量 module-window 组回传"),
        },
        "limitations": ["multi_query_observation_not_single_snapshot",
                        "evaluated_windows_not_counted"],
        "note": ("固定 30 分钟窗按模块源端聚合：内层 count_distinct(subject)，"
                 "外层仅汇总达到 threshold 的窗口；未回传全部窗口明细，"
                 "qualifyingWindows/qualifyingModules 为源端精确聚合结果"),
    }
