"""远程绑定对象的数据时间范围（源端 min/max 一次聚合）。"""
from __future__ import annotations

from pydantic import ValidationError

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.query_planning import (
    _definition_type, _definitions,
)
from app.ontologies.agent_runtime.query_window import (
    resolve_window, window_filters, window_for_result,
)

_TEMPORAL_TYPES = {"date", "datetime", "timestamp", "time"}


def get_data_time_range(db, scope, user, session_id: str, ot,
                        time_property=None, filters=None, window=None) -> dict:
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

    definitions = _definitions(ot)
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
    resolved_window = None
    window_leaves = []
    if window is not None:
        if not isinstance(window, dict):
            raise ToolError("data_time_range_invalid_query: window 必须是对象")
        window_input = {key: value for key, value in window.items()
                        if key != "object_type"}
        window_input["property"] = prop
        resolved_window = resolve_window(
            window_input, property_name=prop,
            property_type=_definition_type(ot, prop))
        window_leaves = window_filters(resolved_window, source="objects")
    payload = {
        "sources": [{"alias": "objects", "ontologyId": scope.ontology.id,
                     "releaseId": scope.release_id, "objectTypeId": ot.id}],
        "where": window_leaves + [_filter(item) for item in resolved_filters],
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
        "resolvedWindow": window_for_result(resolved_window),
        "queryId": result["queryId"], "observedAt": result["observedAt"],
        "countsScope": result["countsScope"], "citations": result["citations"],
    }
