"""查询规划/装配 — 工具参数到 QueryRequest 与可重放 querySpec 的纯装配。

本模块只做声明解析、别名展开、窗口冻结和可读描述；不执行查询、不读业务行。
"""
from __future__ import annotations

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.query_spec import build_query_spec
from app.ontologies.agent_runtime.query_window import window_for_result
from app.ontologies.agent_query.schemas import MAX_SELECT_ITEMS

_FILTER_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "contains", "in",
               "is_null", "is_not_null"}


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


def _resolve_type_reference(scope, ref, default=None):
    if ref in (None, ""):
        return default
    return scope.require_object_type(ref)


def _definitions(ot) -> dict[str, dict]:
    return {p.get("name"): p for p in (ot.properties or [])
            if isinstance(p, dict) and p.get("name")}


def _visible_properties(scope, ot) -> list[str]:
    allowed = scope.allowed_property_names(ot.id)
    queryable = scope.queryable_property_names(ot)
    return [name for name in _definitions(ot)
            if (allowed is None or name in allowed)
            and (queryable is None or name in queryable)]


def _definition_type(ot, prop: str) -> str:
    definition = _definitions(ot).get(prop) or {}
    return str(definition.get("type") or "string").lower()


def _bounded_projection(scope, ot, *, preferred=(),
                        cap: int = MAX_SELECT_ITEMS) -> tuple[list[str], list[str]]:
    """默认投影的有界选取：可查询主键 + 当前过滤/排序/窗口/分组字段优先。

    返回 ``(selected, omitted)``。``omitted`` 只表示本次因单一查询投影上限
    未返回，不代表字段不存在或观测值为空；调用方必须把它作为结果元数据
    显式告知模型，不得塞进 columns/rows 冒充观测列。
    """
    visible = _visible_properties(scope, ot)
    visible_set = set(visible)
    primary = str(getattr(ot, "primary_key", "") or "")
    ordered: list[str] = []
    for name in [primary, *preferred, *visible]:
        if name and name in visible_set and name not in ordered:
            ordered.append(name)
    return ordered[:cap], ordered[cap:]


def _projection_payload(selected: list[str], omitted: list[str]) -> dict:
    payload = {
        "cap": MAX_SELECT_ITEMS,
        "selectedFields": list(selected),
        "omittedFields": list(omitted),
        "note": ("默认投影按主键与当前过滤/排序/窗口/分组字段优先选取至多 "
                 f"{MAX_SELECT_ITEMS} 个可查询字段；omittedFields 未返回，"
                 "不代表字段不存在或值为空，需要时请显式 select 分批查询"),
    }
    return payload


def _where_description(where: list[dict], label_of: dict[str, str]) -> list[dict]:
    out = []
    for leaf in where or []:
        if not isinstance(leaf, dict) or "source" not in leaf:
            continue
        out.append({
            "objectType": label_of.get(leaf.get("source"), leaf.get("source")),
            "property": leaf.get("property"),
            "op": leaf.get("op"),
            "value": leaf.get("value"),
        })
    return out


def _build_description(*, base_ot, stats_mode: bool, where, label_of,
                       joined_labels, select_entries, group_items,
                       aggregate_items, order_by, resolved_window,
                       page_size, omitted_fields=None) -> dict:
    description = {
        "objectType": base_ot.display_name,
        "semantics": "grouped_aggregate" if stats_mode else "detail_rows",
        "filters": _where_description(where, label_of),
        "joinedObjectTypes": joined_labels,
        # select_entries 为 (alias, property, object_type)；这里公布已解析
        # 属性名而不是 source alias，避免把来源元数据误读成投影字段。
        "select": [entry[1] for entry in select_entries],
        "groupBy": [
            {"property": item["readable"],
             **({"bucket": item["bucket"]} if item.get("bucket") else {})}
            for item in group_items
        ],
        "aggregates": [
            {"fn": item["fn"], "property": item["property"],
             "as": item["readable"]}
            for item in aggregate_items
        ],
        "orderBy": [{"output": item["readable"],
                     "direction": item["direction"]}
                    for item in order_by],
        "pageSize": page_size,
        "timeWindow": window_for_result(resolved_window),
        "countSemantics": ("grouped_aggregate_rows" if stats_mode
                           else "detail_rows"),
    }
    if omitted_fields is not None:
        description["selectCap"] = MAX_SELECT_ITEMS
        description["omittedFields"] = list(omitted_fields)
        if omitted_fields:
            description["projectionNote"] = (
                "默认投影按主键与当前过滤/排序/窗口/分组字段优先选取至多 "
                f"{MAX_SELECT_ITEMS} 个可查询字段；omittedFields 未被观测，"
                "不代表字段不存在或值为空")
    return description


def _spec_from_request(*, request, scope, base_ot, labels, resolved_window,
                       description, stats_mode, detail_request=None,
                       detail_omitted_fields=None) -> dict:
    if stats_mode and detail_omitted_fields:
        description = {
            **description,
            "drilldownSelectCap": MAX_SELECT_ITEMS,
            "drilldownOmittedFields": list(detail_omitted_fields),
            "drilldownProjectionNote": (
                "下钻明细投影同样受单一查询投影上限约束；"
                "drilldownOmittedFields 未被观测，不代表字段不存在或值为空"),
        }
    spec = build_query_spec(
        shape="objects",
        ontology_id=scope.ontology.id,
        release_id=str(scope.release_id),
        request=request,
        labels=labels,
        resolved_window=window_for_result(resolved_window),
        description=description,
        object_type=base_ot.id,
    )
    if stats_mode and detail_request is not None:
        spec["drilldownBase"] = detail_request.model_dump(by_alias=True)
        spec["drilldownLabels"] = {
            item.as_: item.property for item in detail_request.select
        }
    return spec


def _spl_bindings(scope, raw_bindings) -> dict[str, dict]:
    if not isinstance(raw_bindings, dict) or not raw_bindings:
        raise ToolError("query_objects_invalid_query: SPL 查询必须提供 semanticBindings")
    fixed: dict[str, dict] = {}
    for alias, raw in raw_bindings.items():
        if not isinstance(raw, dict):
            raise ToolError(
                f"query_objects_invalid_query: semanticBindings.{alias} 必须是对象")
        ref = (raw.get("objectType") or raw.get("object_type")
               or raw.get("objectTypeId") or raw.get("object_type_id"))
        if not ref:
            raise ToolError(
                f"query_objects_invalid_query: semanticBindings.{alias} 缺少对象类型")
        ot = scope.require_object_type(str(ref))
        fixed[str(alias)] = {
            "ontologyId": scope.ontology.id,
            "releaseId": str(scope.release_id),
            "objectTypeId": ot.id,
        }
    return fixed
