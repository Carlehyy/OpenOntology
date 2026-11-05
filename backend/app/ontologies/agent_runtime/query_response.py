"""查询响应装配 — 可读行、一致性口径、结构化分组与签名下钻。

只消费引擎返回的页面/分组行，不执行查询；图表与下钻均为确定性附加信息。
"""
from __future__ import annotations

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.query_spec import (
    drilldown_conditions, make_drilldown, spec_hash,
)
from app.ontologies.agent_runtime.query_window import window_for_result

_BUCKET_UNITS = {"half_hour": "半小时", "day": "日", "week": "周",
                 "month": "月", "quarter": "季", "year": "年"}


def _data_mode(data: dict) -> str:
    modes = sorted({entry.get("dataMode")
                    for entry in (data.get("releases") or {}).values()
                    if isinstance(entry, dict) and entry.get("dataMode")})
    return "/".join(modes) if modes else "materialized"


def _persistence_mode(data: dict) -> str:
    persistence = data.get("persistence")
    if isinstance(persistence, dict):
        if persistence.get("classification") == "aggregate":
            return "remote_aggregate"
        return "remote_detail"
    if _data_mode(data) == "remote":
        return "remote_detail"
    return "snapshot"


def _consistency(data: dict, *, page_fetch: bool) -> dict:
    mode = _persistence_mode(data)
    observed = data.get("observedAt")
    if page_fetch:
        if mode == "remote_detail":
            return {
                "sameSnapshot": False, "queryFresh": False,
                "mode": "lazy_keyset", "observedAt": observed,
                "note": "远程源 keyset 续读，非固定快照；页间观测时点可能变化",
            }
        if mode == "remote_aggregate":
            return {
                "sameSnapshot": True, "queryFresh": False,
                "mode": "remote_aggregate", "observedAt": observed,
                "note": "远程聚合结果由同一句柄返回；不是明细 keyset 续读",
            }
        return {
            "sameSnapshot": True, "queryFresh": False, "mode": "snapshot",
            "observedAt": observed, "note": "页间共享同一次物化查询快照",
        }
    if mode == "remote_aggregate":
        return {
            "sameSnapshot": True, "queryFresh": True,
            "mode": "remote_aggregate", "observedAt": observed,
            "note": "远程聚合结果是一次物化聚合观测",
        }
    if mode == "remote_detail":
        return {
            "sameSnapshot": False, "queryFresh": True,
            "mode": "fresh_remote_keyset", "observedAt": observed,
            "note": "本次为新查询；远程结果为源端 keyset 观测，不是固定历史快照",
        }
    return {
        "sameSnapshot": True, "queryFresh": True, "mode": "snapshot",
        "observedAt": observed, "note": "本次结果来自同一次物化查询快照",
    }



def _group_items_from_spec(spec: dict | None) -> list[dict]:
    if not spec or spec.get("shape") != "objects":
        return []
    query = spec.get("query") or {}
    labels = spec.get("labels") or {}
    items = []
    for index, group in enumerate(query.get("groupBy") or []):
        if not isinstance(group, dict):
            continue
        prop = str(group.get("property") or "")
        items.append({
            "source": group.get("source"),
            "property": prop,
            "bucket": group.get("bucket"),
            "readable": labels.get(f"g{index}") or prop,
        })
    return items


def _group_payload(row: dict, group_items: list[dict]) -> tuple[list, str]:
    values = [row.get(f"g{index}") for index in range(len(group_items))]
    parts = []
    for value, item in zip(values, group_items):
        unit = _BUCKET_UNITS.get(item.get("bucket") or "", "")
        label = f"{item['readable']}({unit})" if unit else item["readable"]
        parts.append(f"{label}={('（空）' if value is None else value)}")
    return values, " | ".join(parts)


def _attach_drilldowns(*, rows: list[dict], group_items: list[dict],
                        spec: dict, resolved_window: dict | None,
                        user, session_id: str) -> None:
    if not group_items:
        return
    if user is None or not session_id:
        for row in rows:
            row["drilldownUnavailable"] = "missing_conversation"
        return
    try:
        base_hash = spec_hash(spec)
    except Exception:  # noqa: BLE001
        base_hash = ""
    owner = str(getattr(user, "id", ""))
    for row in rows:
        values, display = _group_payload(row, group_items)
        row["groupValues"] = values
        row["groupDisplay"] = display
        try:
            conditions = drilldown_conditions(values, group_items)
            drilldown = make_drilldown(
                owner=owner,
                ontology_id=str(spec.get("ontologyId")),
                conversation_id=str(session_id),
                base_spec_hash=base_hash,
                conditions=conditions,
                group_values=values,
                group_by=[item["readable"] for item in group_items],
                resolved_window=window_for_result(resolved_window),
            )
            row["drilldown"] = drilldown
        except ToolError as exc:
            row["drilldownUnavailable"] = str(exc)




def cursor_page_result(*, data: dict, spec: dict, user,
                       session_id: str, page_fetch: bool = True) -> dict:
    """Restore readable labels/statistics from a cached querySpec on cursor pages."""
    from app.ontologies.agent_runtime.query_spec import (
        build_columns, map_rows, spec_hash,
    )
    from app.ontologies.agent_runtime.read_queries import encode_scalar

    labels = spec.get("labels") or {}
    description = spec.get("description") or {}
    group_items = _group_items_from_spec(spec)
    raw_rows = list(data.get("rows") or [])
    if group_items:
        _attach_drilldowns(
            rows=raw_rows, group_items=group_items, spec=spec,
            resolved_window=spec.get("resolvedWindow"), user=user,
            session_id=session_id)
    rows = map_rows(raw_rows, labels)
    has_next = bool(data.get("hasNext"))
    out = {
        "shape": "objects",
        "objectType": description.get("objectType"),
        "joinedObjectTypes": description.get("joinedObjectTypes") or [],
        "dataMode": _data_mode(data),
        "columns": build_columns(data.get("columns"), labels),
        "rows": rows,
        "returned": len(rows),
        "totalRowCount": encode_scalar(data.get("totalRowCount"))
        if data.get("totalRowCount") is not None else None,
        "rowCounting": data.get("rowCounting"),
        "hasNext": has_next,
        "cursor": data.get("cursor") if has_next else None,
        "truncated": has_next,
        "exact": data.get("exact") is not False,
        "partial": bool(data.get("partial")),
        "completeness": data.get("completeness", "complete"),
        "completenessScope": "query_execution",
        "pageSize": data.get("pageSize"),
        "queryId": data.get("queryId"),
        "observedAt": data.get("observedAt"),
        "releaseId": spec.get("releaseId"),
        "resolvedWindow": spec.get("resolvedWindow"),
        "consistency": _consistency(data, page_fetch=page_fetch),
        "querySpec": spec,
        "querySpecHash": spec_hash(spec),
    }
    # 续页同样携带投影省略元数据：不能因翻页丢掉“哪些默认字段未观测”。
    if "omittedFields" in description:
        out["projection"] = {
            "cap": description.get("selectCap"),
            "selectedFields": description.get("select") or [],
            "omittedFields": description.get("omittedFields") or [],
            "note": description.get("projectionNote") or (
                "默认投影受单一查询投影上限约束；omittedFields 未返回，"
                "不代表字段不存在或值为空"),
        }
    if description.get("drilldownOmittedFields"):
        out["drilldownProjection"] = {
            "cap": description.get("drilldownSelectCap"),
            "omittedFields": description.get("drilldownOmittedFields") or [],
            "note": description.get("drilldownProjectionNote") or (
                "下钻明细投影受单一查询投影上限约束；省略字段未被观测"),
        }
    if group_items:
        out["groupBy"] = [
            (f"{item['readable']}({_BUCKET_UNITS[item['bucket']]})"
             if item.get("bucket") else item["readable"])
            for item in group_items
        ]
        out["groupBuckets"] = [item.get("bucket") for item in group_items]
        out["groupValues"] = [
            [row.get(f"g{index}") for index in range(len(group_items))]
            for row in raw_rows
        ]
    return out
