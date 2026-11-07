"""按授权作用域的结构发现工具 — 只暴露 AgentScope 里的类型/属性/链接/动作。

为什么需要独立工具：每轮注入全量 skill card 会把大量类型和属性塞进上下文，
模型仍可能猜字段。``describe_objects`` 默认只列授权类型摘要（分页），
显式点名 1-8 个类型时返回该类型的完整授权属性、已发布链接与动作摘要。
本工具不统计实例数（实例计数可能昂贵且不是结构发现），也不读取 profile
之外的任何表。
"""
from __future__ import annotations

from app.ontologies.agent_runtime.boundary import ToolError, remote_object_type_ids
from app.ontologies.agent_runtime.property_visibility import (
    remote_binding_states as _remote_binding_states,
)

_SUMMARY_PAGE = 12
_MAX_DETAIL_TYPES = 8


def _property_payload(ot, allowed, remote_state=None) -> list[dict]:
    primary = str(getattr(ot, "primary_key", "") or "")
    remote = remote_state is not None
    queryable = remote_state.get("queryable") if remote else None
    reason = remote_state.get("reason") if remote else None
    properties = []
    for item in ot.properties or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        if allowed is not None and name not in allowed:
            continue
        entry = {
            "name": name,
            "displayName": item.get("displayName") or item.get("display_name") or name,
            "type": item.get("type") or "string",
            "required": bool(item.get("required")),
            "primaryKey": bool(primary and (item.get("id") == primary
                                            or name == primary)),
            "computed": bool(item.get("source") == "computed"
                             or item.get("computed")),
            "description": item.get("description"),
        }
        if remote:
            entry["queryable"] = bool(name in (queryable or set()))
            if not entry["queryable"]:
                entry["unqueryableReason"] = reason or "remote_property_unmapped"
        properties.append(entry)
    return properties


def _link_payload(scope, ot) -> list[dict]:
    links = []
    for link in scope.link_types.values():
        direction = None
        other_id = None
        if link.source_object_type_id == ot.id:
            direction = "out"
            other_id = link.target_object_type_id
        elif link.target_object_type_id == ot.id:
            direction = "in"
            other_id = link.source_object_type_id
        if direction is None:
            continue
        other = scope.object_types.get(other_id)
        links.append({
            "name": link.name,
            "displayName": link.display_name,
            "direction": direction,
            "otherObjectTypeId": other_id,
            "otherObjectType": (other.display_name if other is not None
                                else other_id),
            "cardinality": getattr(link, "cardinality", None),
            "description": getattr(link, "description", None),
        })
    links.sort(key=lambda item: (item["otherObjectType"] or "",
                                 item["displayName"] or ""))
    return links


def _action_payload(scope, ot) -> list[dict]:
    actions = []
    for action in scope.actions.values():
        target_id = getattr(action, "object_type_id", None)
        if target_id is not None and target_id != ot.id:
            continue
        actions.append({
            "name": action.name,
            "displayName": action.display_name,
            "description": getattr(action, "description", None),
            "requiresApproval": bool(action.requires_approval),
            "targetsThisType": target_id == ot.id,
            "parameters": [
                {
                    "name": parameter.get("name"),
                    "displayName": parameter.get("displayName")
                    or parameter.get("display_name") or parameter.get("name"),
                    "type": parameter.get("type", "string"),
                    "required": bool(parameter.get("required")),
                    "description": parameter.get("description"),
                }
                for parameter in (action.parameters or [])
                if isinstance(parameter, dict)
            ],
        })
    actions.sort(key=lambda item: item["displayName"] or item["name"] or "")
    return actions


def _summary(scope, ot, remote_ids) -> dict:
    allowed = scope.allowed_property_names(ot.id)
    return {
        "id": ot.id,
        "name": ot.name,
        "displayName": ot.display_name,
        "description": getattr(ot, "description", None),
        "primaryKey": getattr(ot, "primary_key", None),
        "propertyCount": len(_property_payload(ot, allowed)),
        "linkCount": len(_link_payload(scope, ot)),
        "remote": ot.id in remote_ids,
    }


def _detail(scope, ot, remote_ids, remote_states) -> dict:
    allowed = scope.allowed_property_names(ot.id)
    links = _link_payload(scope, ot)
    actions = _action_payload(scope, ot)
    state = remote_states.get(ot.id) if ot.id in remote_ids else None
    properties = _property_payload(ot, allowed, remote_state=state)
    payload = {
        "id": ot.id,
        "name": ot.name,
        "displayName": ot.display_name,
        "description": getattr(ot, "description", None),
        "primaryKey": getattr(ot, "primary_key", None),
        "remote": ot.id in remote_ids,
        "properties": properties,
        "links": links,
        "actions": actions,
        "propertyCount": len(properties),
        "linkCount": len(links),
        "actionCount": len(actions),
    }
    if state is not None:
        payload["queryablePropertyCount"] = sum(
            1 for item in properties if item.get("queryable"))
    return payload


def _offset(next_token) -> int:
    if next_token in (None, ""):
        return 0
    try:
        offset = int(str(next_token))
    except (TypeError, ValueError) as exc:
        raise ToolError("describe_objects_invalid_next: next 分页令牌非法") from exc
    if offset < 0:
        raise ToolError("describe_objects_invalid_next: next 分页令牌非法")
    return offset


def describe_objects(scope, args: dict) -> dict:
    remote_ids = remote_object_type_ids(scope)
    remote_states = _remote_binding_states(scope, remote_ids)
    raw_types = args.get("object_types")
    if raw_types is not None and args.get("next") not in (None, ""):
        raise ToolError("describe_objects_invalid_query: object_types 与 next 不能同时使用")
    if raw_types is not None:
        if not isinstance(raw_types, list) or not raw_types:
            raise ToolError("describe_objects_invalid_query: object_types 必须是非空数组")
        if len(raw_types) > _MAX_DETAIL_TYPES:
            raise ToolError(
                f"describe_objects_invalid_query: 一次最多 {_MAX_DETAIL_TYPES} 个对象类型；"
                "请分批调用")
        resolved = []
        seen = set()
        for ref in raw_types:
            ot = scope.require_object_type(str(ref))
            if ot.id not in seen:
                seen.add(ot.id)
                resolved.append(ot)
        return {
            "mode": "details",
            "objectTypes": [_detail(scope, ot, remote_ids, remote_states)
                            for ot in resolved],
            "returned": len(resolved),
            "total": len(scope.object_types),
            "hasMore": False,
            "next": None,
        }

    pool = sorted(scope.object_types.values(),
                  key=lambda ot: (ot.display_name or "", ot.name or ""))
    offset = _offset(args.get("next"))
    page = pool[offset:offset + _SUMMARY_PAGE]
    end = offset + len(page)
    has_more = end < len(pool)
    return {
        "mode": "summary",
        "objectTypes": [_summary(scope, ot, remote_ids) for ot in page],
        "returned": len(page),
        "total": len(pool),
        "offset": offset,
        "hasMore": has_more,
        "next": str(end) if has_more else None,
        "note": ("摘要不含实例数；需要完整授权属性/链接/动作时用 object_types "
                 "点名 1-8 个类型再调用"),
    }
