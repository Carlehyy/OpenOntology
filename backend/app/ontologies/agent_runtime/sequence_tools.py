"""有界、确定性的“窗口内主体/序列”分析 — 不重造查询引擎。

默认模式（distinct）回答“窗口内有多少个/是否 ≥N 个不同主体”，走统一查询
``count_distinct``，不拉明细。只有用户明确说“连续 / 间隔 / 顺序”时才进入
序列模式：

- consecutive：相邻事件间隔 ≤ max_gap_seconds；
- interval：相邻事件间隔落在 [min_gap_seconds, max_gap_seconds]；
- ordered：事件按时间推进依次命中 sequence 中的阶段过滤器。

序列评估必须先做一次有界 count 探测：候选事件超过统一查询物化预算
（1000 行）时直接拒绝，绝不“截前 1000 行再给确定结论”。窗口、过滤、
排序、授权和远程源下推全部复用统一查询正门；本模块只做确定性内存判定。
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.query_spec import (
    build_columns, build_query_spec, drilldown_conditions, make_drilldown,
    map_rows, spec_hash,
)
from app.ontologies.agent_runtime.query_window import (
    resolve_window, window_filters, window_for_result,
)
from app.ontologies.agent_runtime.read_queries import decimal_number, encode_scalar
from app.ontologies.agent_runtime.sequence_analysis import (
    SequenceEvent, group_sequence_result, ordered_subjects,
)
from app.ontologies.agent_runtime.sequence_window_distinct import (
    analyze_window_distinct,
)

SEQUENCE_ROW_BUDGET = 1000
SUBJECT_PREVIEW = 50
_FILTER_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "contains", "in",
               "is_null", "is_not_null"}
_MODES = {"distinct", "window_distinct", "consecutive", "interval", "ordered"}
_MAX_STAGES = 8


def _definitions(ot) -> dict[str, dict]:
    return {p.get("name"): p for p in (ot.properties or [])
            if isinstance(p, dict) and p.get("name")}


def _visible_properties(scope, ot) -> list[str]:
    allowed = scope.allowed_property_names(ot.id)
    return [name for name in _definitions(ot)
            if allowed is None or name in allowed]


def _filter_leaf(scope, ot, item: dict, source: str = "s0") -> dict:
    if not isinstance(item, dict) or not item.get("property"):
        raise ToolError("sequence_invalid_filter: 过滤条件必须包含 property")
    prop = scope.resolve_visible_property(ot, item["property"])
    op = item.get("op", "eq")
    if op not in _FILTER_OPS:
        raise ToolError(f"sequence_invalid_filter: 不支持的过滤操作符 {op}")
    value = item.get("value")
    if op in {"is_null", "is_not_null"}:
        return {"source": source, "property": prop, "op": op}
    if op == "in" and not isinstance(value, list):
        value = [value]
    return {"source": source, "property": prop, "op": op, "value": value}


def _parse_time(value, zone=None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(text), time.min)
            except ValueError as exc:
                raise ToolError(
                    f"sequence_time_unparseable: 事件时间「{value}」无法解析为"
                    "确定性时间，拒绝给出序列结论") from exc
    else:
        raise ToolError("sequence_time_unparseable: 事件时间不是可解析的日期/时间")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone or timezone.utc)
    return parsed


def _compare(left, op: str, right) -> bool:
    if op == "eq":
        return left == right or (left is not None and str(left) == str(right))
    if op == "neq":
        return not _compare(left, "eq", right)
    if op == "contains":
        return (left is not None and right is not None
                and str(right).lower() in str(left).lower())
    if op == "in":
        options = right if isinstance(right, list) else [right]
        return any(_compare(left, "eq", option) for option in options)
    if op == "is_null":
        return left is None
    if op == "is_not_null":
        return left is not None
    a, b = decimal_number(left), decimal_number(right)
    if a is None or b is None:
        if left is None or right is None:
            return False
        a, b = str(left), str(right)
    return {
        "gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b,
    }[op]


def _matches(row_values: dict, item: dict) -> bool:
    return _compare(row_values.get(item.get("property")), item.get("op", "eq"),
                    item.get("value"))


def _aggregate_spec(*, scope, ot, where, resolved_window, subject_prop,
                    threshold) -> dict:
    from app.ontologies.agent_query import schemas as QS
    request = QS.QueryRequest(
        sources=[QS.QuerySource(alias="s0", ontology_id=scope.ontology.id,
                                release_id=scope.release_id,
                                object_type_id=ot.id)],
        where=[QS.FilterLeaf.model_validate(item) for item in where],
        aggregates=[
            QS.AggregateItem(fn="count_distinct", source="s0",
                             property=subject_prop, as_="value"),
            QS.AggregateItem(fn="count", as_="rows"),
        ],
        page_size=1,
    )
    spec = build_query_spec(
        shape="objects", ontology_id=scope.ontology.id,
        release_id=str(scope.release_id), request=request,
        labels={"value": "去重主体数", "rows": "事件行数"},
        resolved_window=window_for_result(resolved_window),
        description={
            "semantics": "distinct_subject_count",
            "objectType": ot.display_name,
            "subjectProperty": subject_prop,
            "threshold": threshold,
            "filters": [
                {"property": item.get("property"), "op": item.get("op"),
                 "value": item.get("value")}
                for item in where
            ],
            "timeWindow": window_for_result(resolved_window),
        },
        object_type=ot.id,
    )
    return {"request": request, "spec": spec}



def analyze_sequences(db, scope, user, session_id: str, args: dict,
                      spec_cache: dict | None = None) -> dict:
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import (
        QueryError, execute_unified_query, fetch_unified_query_page,
        release_query_snapshot,
    )
    from app.ontologies.agent_query.remote_query import cancel_remote_query
    from app.ontologies.agent_runtime.query_handles import _release_terminal_page

    if user is None:
        raise ToolError("remote_query_user_required: 序列分析需要已认证用户")
    if scope.release_id is None:
        raise ToolError("sequence_release_required: 序列分析需要固定的发布版本")
    cap = min(200, int(scope.profile.max_rows_per_query or 0))
    if cap <= 0:
        raise ToolError("profile_query_disabled: 当前本体助手未启用查询额度")

    ot = scope.require_object_type(args.get("object_type", ""))
    mode = str(args.get("mode") or "distinct").strip().lower()
    if mode not in _MODES:
        raise ToolError(
            f"sequence_mode_unsupported: mode 需为 {'/'.join(sorted(_MODES))}；"
            "普通数量问题请用 distinct，不能把连续/间隔/顺序问题冒充为普通 count")
    subject_prop = scope.resolve_visible_property(
        ot, args.get("subject_property") or args.get("subject") or "")
    time_prop = scope.resolve_visible_property(
        ot, args.get("time_property") or "")
    try:
        threshold = int(args.get("threshold")
                        or args.get("minimum_count") or 1)
    except (TypeError, ValueError) as exc:
        raise ToolError("sequence_invalid_threshold: threshold 必须是整数") from exc
    if threshold < 1:
        raise ToolError("sequence_invalid_threshold: threshold 必须 ≥ 1")

    where = [_filter_leaf(scope, ot, item) for item in (args.get("filters") or [])]
    resolved_window = None
    raw_window = args.get("window")
    if raw_window is not None:
        if not isinstance(raw_window, dict):
            raise ToolError("sequence_invalid_window: window 必须是对象")
        window_input = {key: value for key, value in raw_window.items()
                        if key != "object_type"}
        window_input["property"] = time_prop
        definition = _definitions(ot).get(time_prop) or {}
        resolved_window = resolve_window(
            window_input, property_name=time_prop,
            property_type=str(definition.get("type") or "string").lower())
        where.extend(window_filters(resolved_window, source="s0"))

    if mode == "window_distinct":
        time_type = str((_definitions(ot).get(time_prop) or {})
                        .get("type") or "").lower()
        return analyze_window_distinct(
            db=db, scope=scope, user=user, session_id=session_id,
            ot=ot, args=args, subject_prop=subject_prop,
            time_prop=time_prop, time_property_type=time_type,
            threshold=threshold, where=where,
            resolved_window=resolved_window, cap=cap,
            cancel_handle=_cancel_sequence_handle)

    source = QS.QuerySource(alias="s0", ontology_id=scope.ontology.id,
                            release_id=scope.release_id, object_type_id=ot.id)

    def execute(request):
        try:
            return execute_unified_query(
                db, user, request, session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
        except ValidationError as exc:
            raise ToolError(f"sequence_invalid_query: {exc}") from exc
        except QueryError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    # ---- 默认：窗口内去重主体数（>=N），不走明细下载 ----
    if mode == "distinct":
        raw_groups = args.get("group_by") or []
        if raw_groups and not isinstance(raw_groups, list):
            raise ToolError("sequence_invalid_group: group_by 必须是数组")
        group_items = []
        for item in raw_groups:
            if isinstance(item, str):
                item = {"property": item}
            if not isinstance(item, dict) or not item.get("property"):
                raise ToolError("sequence_invalid_group: group_by 项需要 property")
            bucket = item.get("bucket")
            if bucket is not None and bucket not in {
                    "half_hour", "day", "week", "month",
                    "quarter", "year"}:
                raise ToolError(f"sequence_invalid_group: 不支持的时间分桶 {bucket}")
            group_items.append({
                "source": "s0",
                "property": scope.resolve_visible_property(ot, item["property"]),
                "bucket": bucket,
                "readable": str(item["property"]),
            })
        if not group_items:
            built = _aggregate_spec(
                scope=scope, ot=ot, where=where,
                resolved_window=resolved_window, subject_prop=subject_prop,
                threshold=threshold)
            data = execute(built["request"])
            _release_terminal_page(db, user, session_id, data)
            if (data.get("partial") or data.get("exact") is False
                    or data.get("completeness", "complete") != "complete"
                    or len(data.get("rows") or []) != 1):
                raise ToolError("sequence_incomplete: 去重主体统计未返回完整结果")
            row = data["rows"][0]
            value = int(Decimal(str(row.get("value") or 0)))
            rows_count = int(Decimal(str(row.get("rows") or 0)))
            return {
                "kind": "sequence_analysis",
                "mode": "distinct",
                "objectType": ot.display_name,
                "subjectProperty": subject_prop,
                "timeProperty": time_prop,
                "window": window_for_result(resolved_window),
                "threshold": threshold,
                "distinctSubjects": encode_scalar(value),
                "candidateRows": encode_scalar(rows_count),
                "matchingSubjects": None,
                "meetsThreshold": value >= threshold,
                "sampleSubjects": [],
                "exact": True,
                "partial": False,
                "completeness": "complete",
                "completenessScope": "query_execution",
                "dataMode": ("remote" if isinstance(data.get("persistence"), dict)
                             else "materialized"),
                "querySpec": built["spec"],
                "querySpecHash": spec_hash(built["spec"]),
                "queryId": data.get("queryId"),
                "observedAt": data.get("observedAt"),
                "consistency": {
                    "sameSnapshot": False,
                    "queryFresh": True,
                    "mode": "fresh",
                    "observedAt": data.get("observedAt"),
                    "note": "去重主体数是一次新的聚合观测",
                },
                "note": ("没有连续/间隔/顺序限定：按窗口内去重主体数（>=N）回答；"
                         "重复事件只计一个主体"),
            }

        select = [QS.SelectItem(source="s0", property=item["property"],
                                as_=f"g{index}")
                  for index, item in enumerate(group_items)]
        group_by_payload = [
            QS.GroupByItem(source="s0", property=item["property"],
                           **({"bucket": item["bucket"]}
                              if item["bucket"] else {}))
            for item in group_items
        ]
        request = QS.QueryRequest(
            sources=[source],
            where=[QS.FilterLeaf.model_validate(item) for item in where],
            select=select, group_by=group_by_payload,
            aggregates=[
                QS.AggregateItem(fn="count_distinct", source="s0",
                                 property=subject_prop, as_="value"),
                QS.AggregateItem(fn="count", as_="rows"),
            ],
            order_by=[QS.OrderByItem(output="value", direction="desc"),
                      QS.OrderByItem(output="g0", direction="asc")],
            page_size=min(200, cap),
        )
        detail_request = QS.QueryRequest(
            sources=[source],
            where=[QS.FilterLeaf.model_validate(item) for item in where],
            select=[QS.SelectItem(source="s0", property=subject_prop, as_="c0"),
                    QS.SelectItem(source="s0", property=time_prop, as_="c1")],
        )
        labels = {f"g{index}": item["readable"]
                  for index, item in enumerate(group_items)}
        labels.update({"value": "去重主体数", "rows": "事件行数"})
        spec = build_query_spec(
            shape="objects", ontology_id=scope.ontology.id,
            release_id=str(scope.release_id), request=request, labels=labels,
            resolved_window=window_for_result(resolved_window),
            description={
                "semantics": "grouped_aggregate",
                "objectType": ot.display_name,
                "subjectProperty": subject_prop,
                "threshold": threshold,
                "groupBy": [{"property": item["readable"],
                             **({"bucket": item["bucket"]}
                                if item["bucket"] else {})}
                            for item in group_items],
                "timeWindow": window_for_result(resolved_window),
            },
            object_type=ot.id,
        )
        spec["drilldownBase"] = detail_request.model_dump(by_alias=True)
        spec["drilldownLabels"] = {"c0": subject_prop, "c1": time_prop}
        data = execute(request)
        _release_terminal_page(db, user, session_id, data)
        raw_rows = list(data.get("rows") or [])
        if session_id and user is not None:
            from app.ontologies.agent_runtime.query_response import (
                _attach_drilldowns,
            )
            _attach_drilldowns(
                rows=raw_rows, group_items=group_items, spec=spec,
                resolved_window=resolved_window, user=user,
                session_id=session_id)
        rows = map_rows(raw_rows, labels)
        total_groups = data.get("totalRowCount")
        truncated = bool(data.get("hasNext") or data.get("truncated")
                         or (total_groups is not None
                             and total_groups > len(rows)))
        groups = []
        for raw in raw_rows:
            distinct = int(Decimal(str(raw.get("value") or 0)))
            candidate = int(Decimal(str(raw.get("rows") or 0)))
            entry = {
                "groupValues": [raw.get(f"g{index}")
                                for index in range(len(group_items))],
                "distinctSubjects": encode_scalar(distinct),
                "candidateRows": encode_scalar(candidate),
                "meetsThreshold": distinct >= threshold,
            }
            if raw.get("drilldown"):
                entry["drilldown"] = raw["drilldown"]
            if raw.get("drilldownUnavailable"):
                entry["drilldownUnavailable"] = raw["drilldownUnavailable"]
            groups.append(entry)
        matching = sum(1 for entry in groups if entry["meetsThreshold"])
        return {
            "kind": "sequence_analysis",
            "mode": "distinct",
            "objectType": ot.display_name,
            "subjectProperty": subject_prop,
            "timeProperty": time_prop,
            "window": window_for_result(resolved_window),
            "threshold": threshold,
            "distinctSubjects": None,
            "candidateRows": None,
            "matchingSubjects": None,
            "meetsThreshold": None,
            "matchingGroups": encode_scalar(matching),
            "matchingGroupsIsLowerBound": truncated,
            "groupBy": [item["readable"] for item in group_items],
            "groupBuckets": [item["bucket"] for item in group_items],
            "groups": groups,
            "totalGroups": total_groups,
            "truncated": truncated,
            "exact": True,
            "partial": False,
            "completeness": "complete",
            "completenessScope": "query_execution",
            "dataMode": ("remote" if isinstance(data.get("persistence"), dict)
                         else "materialized"),
            "querySpec": spec,
            "querySpecHash": spec_hash(spec),
            "queryId": data.get("queryId"),
            "observedAt": data.get("observedAt"),
            "consistency": {
                "sameSnapshot": False,
                "queryFresh": True,
                "mode": "fresh",
                "observedAt": data.get("observedAt"),
                "note": "分组去重主体数是一次新的聚合观测",
            },
            "note": ("按分组返回窗口内去重主体数和是否达到阈值；"
                     "分组被截断时 matchingGroups 是下界"),
        }

    # ---- 序列模式：模块/分组内连续片段，主体去重阈值 ----
    raw_groups = args.get("group_by") or []
    if raw_groups and not isinstance(raw_groups, list):
        raise ToolError("sequence_invalid_group: group_by 必须是数组")
    group_items = []
    for item in raw_groups:
        if isinstance(item, str):
            item = {"property": item}
        if not isinstance(item, dict) or not item.get("property"):
            raise ToolError("sequence_invalid_group: group_by 项需要 property")
        if item.get("bucket") is not None:
            raise ToolError("sequence_invalid_group: 序列分组暂不支持时间分桶；"
                            "请用原始属性分组")
        group_items.append({
            "source": "s0",
            "property": scope.resolve_visible_property(ot, item["property"]),
            "readable": str(item["property"]),
        })

    stage_filters: list[list[dict]] = []
    if mode == "ordered":
        stages = args.get("sequence") or args.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ToolError("sequence_invalid_stages: ordered 模式需要 sequence 阶段列表")
        if len(stages) > _MAX_STAGES:
            raise ToolError(f"sequence_invalid_stages: 阶段最多 {_MAX_STAGES} 个")
        for stage in stages:
            if not isinstance(stage, dict):
                raise ToolError("sequence_invalid_stages: 每个阶段必须是对象")
            filters = stage.get("filters")
            if not isinstance(filters, list) or not filters:
                raise ToolError("sequence_invalid_stages: 每个阶段至少一个 filters")
            stage_filters.append([
                _filter_leaf(scope, ot, item) for item in filters
            ])
    max_gap = args.get("max_gap_seconds")
    min_gap = args.get("min_gap_seconds", 0)
    try:
        max_gap_seconds = int(max_gap) if max_gap is not None else None
        min_gap_seconds = int(min_gap or 0)
    except (TypeError, ValueError) as exc:
        raise ToolError("sequence_invalid_gap: 间隔必须是整数秒") from exc
    if mode in {"consecutive", "interval"} and max_gap_seconds is None:
        raise ToolError("sequence_invalid_gap: 连续/间隔模式需要 max_gap_seconds")
    if max_gap_seconds is not None and max_gap_seconds < 0:
        raise ToolError("sequence_invalid_gap: max_gap_seconds 不能为负")
    if min_gap_seconds < 0 or (max_gap_seconds is not None
                               and min_gap_seconds > max_gap_seconds):
        raise ToolError("sequence_invalid_gap: 间隔区间非法")
    if mode in {"consecutive", "interval"} and threshold < 1:
        threshold = 1

    probe = QS.QueryRequest(
        sources=[source], where=[QS.FilterLeaf.model_validate(item) for item in where],
        aggregates=[QS.AggregateItem(fn="count", as_="value"),
                    QS.AggregateItem(fn="count_distinct", source="s0",
                                     property=subject_prop, as_="subjects")],
        page_size=1)
    probe_data = execute(probe)
    _release_terminal_page(db, user, session_id, probe_data)
    if (probe_data.get("partial") or probe_data.get("exact") is False
            or probe_data.get("completeness", "complete") != "complete"
            or len(probe_data.get("rows") or []) != 1):
        raise ToolError("sequence_incomplete: 序列候选计数未返回完整结果")
    probe_row = probe_data["rows"][0]
    candidate_rows = int(Decimal(str(probe_row.get("value") or 0)))
    distinct_subjects = int(Decimal(str(probe_row.get("subjects") or 0)))
    if candidate_rows > SEQUENCE_ROW_BUDGET:
        raise ToolError(
            "sequence_budget_exceeded: 窗口/过滤内有 "
            f"{candidate_rows} 条候选事件，超过确定性序列分析上限 "
            f"{SEQUENCE_ROW_BUDGET}；不接受截断样本后给确定结论。"
            "请缩小绝对时间窗口或增加过滤条件后重试")

    selected_props: list[str] = [subject_prop, time_prop]
    for item in group_items:
        if item["property"] not in selected_props:
            selected_props.append(item["property"])
    for filters in stage_filters:
        for item in filters:
            if item.get("property") not in selected_props:
                selected_props.append(item.get("property"))
    select = [QS.SelectItem(source="s0", property=prop, as_=f"c{index}")
              for index, prop in enumerate(selected_props)]
    detail_request = QS.QueryRequest(
        sources=[source], where=[QS.FilterLeaf.model_validate(item) for item in where],
        select=select,
        order_by=[QS.OrderByItem(output="c1", direction="asc"),
                  QS.OrderByItem(output="c0", direction="asc")],
        page_size=min(cap, max(1, candidate_rows or 1)))
    detail_data = execute(detail_request)
    collected = list(detail_data.get("rows") or [])
    pages = 1
    detail_page_size = max(1, int(detail_request.page_size or cap))
    max_pages = ((SEQUENCE_ROW_BUDGET + detail_page_size - 1)
                 // detail_page_size) + 2
    seen_cursors: set[str] = set()
    while detail_data.get("hasNext"):
        pages += 1
        if pages > max_pages or len(collected) > SEQUENCE_ROW_BUDGET:
            _cancel_sequence_handle(db, user, session_id, detail_data)
            raise ToolError(
                "sequence_budget_exceeded: 序列明细分页超过有界预算，"
                "拒绝在未完整读取候选事件时给出确定结论")
        cursor = detail_data.get("cursor")
        if not cursor:
            _cancel_sequence_handle(db, user, session_id, detail_data)
            raise ToolError("sequence_incomplete: 序列明细缺少续页游标")
        if cursor in seen_cursors:
            _cancel_sequence_handle(db, user, session_id, detail_data)
            raise ToolError(
                "sequence_incomplete: 续页游标重复，拒绝无限读取")
        seen_cursors.add(cursor)
        try:
            next_page = fetch_unified_query_page(
                db, user, str(cursor), session_kind="ontology_agent",
                session_id=session_id or "", include_page_size=True)
        except Exception:
            # 续页失败：当前页/句柄必须显式回收，不能把配额留给下一回合。
            _cancel_sequence_handle(db, user, session_id, detail_data)
            raise
        detail_data = next_page
        collected.extend(detail_data.get("rows") or [])
    _release_terminal_page(db, user, session_id, detail_data)
    if (detail_data.get("partial") or detail_data.get("exact") is False
            or detail_data.get("completeness", "complete") != "complete"):
        raise ToolError("sequence_incomplete: 序列明细未返回完整结果")
    if len(collected) != candidate_rows:
        raise ToolError(
            "sequence_incomplete: 序列明细行数与候选计数不一致"
            f"（{len(collected)}/{candidate_rows}），拒绝推断")

    values_of = {prop: f"c{index}" for index, prop in enumerate(selected_props)}
    zone = None
    if resolved_window:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(str(resolved_window.get("timezone")))
        except Exception:  # noqa: BLE001 - 窗口解析已校验
            zone = None

    groups: dict[tuple, list] = {}
    seen = set()
    duplicate_rows = 0
    missing_subject = 0
    for row in collected:
        subject = row.get("c0")
        if subject is None or str(subject) == "":
            missing_subject += 1
            continue
        signature = tuple(row.get(f"c{index}")
                          for index in range(len(selected_props)))
        if signature in seen:
            duplicate_rows += 1
            continue
        seen.add(signature)
        values = {prop: row.get(column) for prop, column in values_of.items()}
        group_key = tuple(values.get(item["property"])
                          for item in group_items)
        groups.setdefault(group_key, []).append(
            SequenceEvent(subject=str(subject),
                          at=_parse_time(row.get("c1"), zone),
                          values=values))

    group_results = []
    for group_key, events in groups.items():
        if mode == "ordered":
            matchers = [
                (lambda values, filters=filters:
                 all(_matches(values, item) for item in filters))
                for filters in stage_filters
            ]
            within_raw = args.get("within_seconds")
            within_seconds = None
            if within_raw is not None:
                try:
                    within_seconds = int(within_raw)
                except (TypeError, ValueError) as exc:
                    raise ToolError(
                        "sequence_invalid_gap: within_seconds 必须是整数") from exc
            matched = ordered_subjects(
                events, matchers, within_seconds=within_seconds)
            result = {
                "distinctSubjects": len(matched),
                "matchingSubjects": len(matched),
                "sampleSubjects": sorted(matched)[:SUBJECT_PREVIEW],
                "qualifies": len(matched) >= threshold,
                "segmentStart": None,
                "segmentEnd": None,
                "segmentEventCount": None,
            }
        else:
            result = group_sequence_result(
                events, max_gap_seconds=max_gap_seconds or 0,
                min_gap_seconds=min_gap_seconds, threshold=threshold)
        group_results.append({
            "groupValues": list(group_key),
            "groupDisplay": " | ".join(
                f"{item['readable']}={('（空）' if value is None else value)}"
                for item, value in zip(group_items, group_key)) or "全部",
            **result,
        })
    matching_groups = sum(1 for item in group_results if item["qualifies"])
    best_subjects = max((item["distinctSubjects"] for item in group_results),
                        default=0)
    sample_subjects: list[str] = []
    for item in group_results:
        for subject in item.get("sampleSubjects") or []:
            if subject not in sample_subjects:
                sample_subjects.append(subject)
            if len(sample_subjects) >= SUBJECT_PREVIEW:
                break
        if len(sample_subjects) >= SUBJECT_PREVIEW:
            break

    labels = {f"c{index}": prop for index, prop in enumerate(selected_props)}
    spec = build_query_spec(
        shape="objects", ontology_id=scope.ontology.id,
        release_id=str(scope.release_id), request=detail_request,
        labels=labels, resolved_window=window_for_result(resolved_window),
        description={
            "semantics": f"sequence_{mode}",
            "objectType": ot.display_name,
            "subjectProperty": subject_prop,
            "timeProperty": time_prop,
            "threshold": threshold,
            "thresholdScope": ("distinct_subjects_in_qualifying_segment"
                               if mode != "ordered"
                               else "distinct_subjects_with_ordered_path"),
            "maxGapSeconds": max_gap_seconds,
            "minGapSeconds": min_gap_seconds,
            "groupBy": [{"property": item["readable"]} for item in group_items],
            "stageCount": len(stage_filters) or None,
            "timeWindow": window_for_result(resolved_window),
        },
        object_type=ot.id,
    )
    spec["drilldownBase"] = detail_request.model_dump(by_alias=True)
    spec["drilldownLabels"] = {
        item.as_: item.property for item in detail_request.select
    }
    drilldown = None
    if session_id and user is not None:
        drilldown = make_drilldown(
            owner=str(getattr(user, "id", "")),
            ontology_id=scope.ontology.id,
            conversation_id=session_id,
            base_spec_hash=spec_hash(spec),
            conditions=[], group_values=[], group_by=[],
            resolved_window=window_for_result(resolved_window),
        )
    is_remote = isinstance(detail_data.get("persistence"), dict)
    consistency = {
        "sameSnapshot": not is_remote,
        "queryFresh": True,
        "mode": "lazy_keyset" if is_remote else "snapshot",
        "observedAt": detail_data.get("observedAt"),
        "note": ("本地物化快照内完成有界序列评估" if not is_remote else
                 "远程 keyset 分页不保证同一快照；序列结论仅基于已读取页，"
                 "不能声称全量一致"),
    }
    return {
        "kind": "sequence_analysis",
        "mode": mode,
        "objectType": ot.display_name,
        "subjectProperty": subject_prop,
        "timeProperty": time_prop,
        "window": window_for_result(resolved_window),
        "threshold": threshold,
        "thresholdScope": ("distinct_subjects_in_qualifying_segment"
                           if mode != "ordered"
                           else "distinct_subjects_with_ordered_path"),
        "distinctSubjects": encode_scalar(distinct_subjects),
        "candidateRows": encode_scalar(candidate_rows),
        "evaluatedRows": encode_scalar(len(collected) - duplicate_rows),
        "duplicateRows": encode_scalar(duplicate_rows),
        "missingSubjectRows": encode_scalar(missing_subject),
        "matchingSubjects": encode_scalar(best_subjects),
        "matchingGroups": encode_scalar(matching_groups),
        "meetsThreshold": matching_groups >= 1,
        "groupBy": [item["readable"] for item in group_items],
        "groups": group_results,
        "sampleSubjects": sorted(sample_subjects),
        "subjectsTruncated": len(sample_subjects) >= SUBJECT_PREVIEW,
        "exact": not is_remote,
        "partial": is_remote,
        "completeness": "partial" if is_remote else "complete",
        "completenessScope": "query_execution",
        "limitations": (["remote_keyset_not_snapshot"] if is_remote else []),
        "dataMode": ("remote" if is_remote else "materialized"),
        "querySpec": spec,
        "querySpecHash": spec_hash(spec),
        "drilldown": drilldown,
        "observedAt": detail_data.get("observedAt"),
        "consistency": consistency,
        "note": ("连续/间隔：按分组事件时间排序，任一连续片段满足相邻间隔政策后，"
                 "统计该片段内去重主体数是否 >= threshold；gap 打断后继续寻找下一片段。"
                 "重复主体只计一次；远程结果不声称同一快照。"),
    }

def _cancel_sequence_handle(db, user, session_id: str, data: dict) -> None:
    from app.ontologies.agent_query.remote_query import cancel_remote_query
    from app.ontologies.agent_query.service import release_query_snapshot
    query_id = data.get("queryId")
    if not query_id:
        return
    try:
        if isinstance(data.get("persistence"), dict):
            cancel_remote_query(db, user, str(query_id))
        else:
            if not release_query_snapshot(
                    db, user, str(query_id), session_kind="ontology_agent",
                    session_id=session_id or ""):
                cancel_remote_query(db, user, str(query_id))
    except Exception:  # noqa: BLE001
        pass
