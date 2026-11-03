"""Query-view response shaping — labels, consistency and drilldown metadata."""
from __future__ import annotations

from app.ontologies.agent_runtime.models import AgentConversation
from app.ontologies.agent_runtime.query_spec import (
    build_columns, make_view_cursor, map_rows,
)
from app.ontologies.agent_runtime.read_queries import encode_scalar


def _persistence(data: dict) -> str:
    persistence = data.get("persistence")
    if isinstance(persistence, dict):
        if persistence.get("classification") == "aggregate":
            return "remote_aggregate"
        return "remote_detail"
    return "snapshot"


def _consistency(data: dict, *, first: bool, persistence: str) -> dict:
    observed = data.get("observedAt")
    if first:
        return {
            "sameSnapshot": False, "queryFresh": True, "mode": "fresh",
            "observedAt": observed,
            "note": "新查询/下钻按 querySpec 重新取数；与历史结果不是同一快照",
        }
    if persistence == "remote_aggregate":
        return {
            "sameSnapshot": True, "queryFresh": False,
            "mode": "remote_aggregate", "observedAt": observed,
            "note": "远程聚合结果由同一句柄物化返回；不是明细 keyset 续读",
        }
    if persistence == "remote_detail":
        return {
            "sameSnapshot": False, "queryFresh": False, "mode": "lazy_keyset",
            "observedAt": observed,
            "note": "远程源 keyset 续读，不是固定快照；页间观测时点可能变化",
        }
    return {
        "sameSnapshot": True, "queryFresh": False, "mode": "snapshot",
        "observedAt": observed, "note": "页间共享同一次物化查询快照",
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
            "source": group.get("source"), "property": prop,
            "bucket": group.get("bucket"),
            "readable": labels.get(f"g{index}") or prop,
        })
    return items


def _attach_view_metadata(rows: list[dict], *, spec: dict | None, user,
                          conversation: AgentConversation,
                          resolved_window: dict | None) -> None:
    group_items = _group_items_from_spec(spec)
    if not group_items:
        return
    if spec is None:
        for row in rows:
            row["drilldownUnavailable"] = "cursor_page_without_query_spec"
        return
    from app.ontologies.agent_runtime.query_response import _attach_drilldowns
    _attach_drilldowns(
        rows=rows, group_items=group_items, spec=spec,
        resolved_window=resolved_window, user=user,
        session_id=conversation.id)


def _data_mode(data: dict) -> str:
    modes = sorted({entry.get("dataMode")
                    for entry in (data.get("releases") or {}).values()
                    if isinstance(entry, dict) and entry.get("dataMode")})
    return "/".join(modes) if modes else "materialized"


def _view_payload(*, data: dict, spec: dict | None, spec_digest: str,
                  user, conversation: AgentConversation, first: bool,
                  resolved_window: dict | None) -> dict:
    labels = (spec or {}).get("labels") or {}
    raw_rows = list(data.get("rows") or [])
    _attach_view_metadata(raw_rows, spec=spec, user=user,
                          conversation=conversation,
                          resolved_window=resolved_window)
    rows = map_rows(raw_rows, labels)
    persistence = _persistence(data)
    query_id = data.get("queryId")
    has_next = bool(data.get("hasNext"))
    cursor = None
    if has_next and data.get("cursor"):
        cursor = make_view_cursor(
            owner=str(getattr(user, "id", "")),
            ontology_id=str(conversation.ontology_id),
            conversation_id=conversation.id,
            spec_hash_value=spec_digest,
            engine_cursor=str(data["cursor"]),
            query_id=str(query_id or ""),
            persistence=persistence,
            resolved_window=resolved_window,
        )
    return {
        "conversationId": conversation.id,
        "columns": build_columns(data.get("columns"), labels),
        "rows": rows,
        "returned": len(rows),
        "pageSize": data.get("pageSize"),
        "hasNext": has_next,
        "cursor": cursor,
        "totalRowCount": encode_scalar(data.get("totalRowCount"))
        if data.get("totalRowCount") is not None else None,
        "rowCounting": data.get("rowCounting"),
        "observedAt": data.get("observedAt"),
        "resolvedWindow": resolved_window,
        "consistency": _consistency(data, first=first,
                                    persistence=persistence),
        "dataMode": ("remote" if persistence.startswith("remote")
                     else _data_mode(data)),
        "releaseId": conversation.ontology_release_id,
        "querySpecHash": spec_digest,
        "released": False,
    }
