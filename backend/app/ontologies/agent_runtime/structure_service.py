"""按需结构发现 — 只读当前问题所需片段，不加载完整 schema。

返回准确的 releaseId/version 与字段/动作来源引用；结构片段受 profile 与
属性 allowlist 约束；超限返回明确 422，不偷偷截断。
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.ontologies.agent_runtime.boundary import build_scope

MAX_OBJECT_TYPES = 20
MAX_LINK_TYPES = 30
MAX_ACTIONS = 10
MAX_JOIN_ASSERTIONS = 20
MAX_PAYLOAD_BYTES = 64 * 1024


class StructureError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        payload.update(self.extra)
        return payload


def build_structure(db: Session, user, ontology_id: str, *,
                    expected_release_id: Optional[str] = None,
                    object_type_ids: Optional[list[str]] = None,
                    link_type_ids: Optional[list[str]] = None,
                    action_ids: Optional[list[str]] = None) -> dict:
    from app.ontologies.release_context import current_release_context

    context = current_release_context(db, ontology_id,
                                      expected_release_id=expected_release_id)
    project, profile, scope = build_scope(db, ontology_id,
                                           release_id=context.release.id)
    if not profile.enabled:
        raise StructureError(403, "profile_disabled",
                             "该本体的智能体已停用")

    requested_types = object_type_ids or []
    requested_links = link_type_ids or []
    requested_actions = action_ids or []
    if len(requested_types) > MAX_OBJECT_TYPES:
        raise StructureError(422, "structure_budget_exceeded",
                             f"objectTypeIds 超过 {MAX_OBJECT_TYPES} 上限")
    if len(requested_links) > MAX_LINK_TYPES:
        raise StructureError(422, "structure_budget_exceeded",
                             f"linkTypeIds 超过 {MAX_LINK_TYPES} 上限")
    if len(requested_actions) > MAX_ACTIONS:
        raise StructureError(422, "structure_budget_exceeded",
                             f"actionIds 超过 {MAX_ACTIONS} 上限")

    def _resolve(pool, requested, kind, label):
        if not requested:
            return []
        resolved = []
        for ref in requested:
            item = pool.get(ref)
            if item is None:
                for candidate in pool.values():
                    if ref in (candidate.name, candidate.display_name):
                        item = candidate
                        break
            if item is None:
                raise StructureError(
                    403, "object_type_forbidden" if kind == "type"
                    else "link_type_forbidden" if kind == "link"
                    else "action_forbidden",
                    f"{label}「{ref}」不存在或不在授权范围内")
            resolved.append(item)
        return resolved

    types = _resolve(scope.object_types, requested_types, "type", "对象类型")
    links = _resolve(scope.link_types, requested_links, "link", "链接类型")
    actions = _resolve(scope.actions, requested_actions, "action", "动作")

    # 未显式请求类型时，链接/动作依赖的类型自动补入片段
    needed_types = {t.id for t in types}
    for lt in links:
        needed_types.add(lt.source_object_type_id)
        needed_types.add(lt.target_object_type_id)
    for a in actions:
        if a.object_type_id:
            needed_types.add(a.object_type_id)
    if needed_types - {t.id for t in types}:
        for tid in sorted(needed_types - {t.id for t in types}):
            extra = scope.object_types.get(tid)
            if extra is not None:
                types.append(extra)
    if len(types) > MAX_OBJECT_TYPES:
        raise StructureError(422, "structure_budget_exceeded",
                             f"结构片段对象类型超过 {MAX_OBJECT_TYPES} 上限")

    def _props(ot):
        allowed = scope.allowed_property_names(ot.id)
        result = []
        for p in (ot.properties or []):
            if not isinstance(p, dict) or not p.get("name"):
                continue
            if allowed is not None and p["name"] not in allowed:
                continue
            result.append({
                "id": p.get("id"),
                "name": p.get("name"),
                "displayName": p.get("displayName") or p.get("name"),
                "type": p.get("type", "string"),
                "required": bool(p.get("required")),
                "description": p.get("description"),
                "source": "computed" if p.get("source") == "computed"
                else "stored",
            })
        return result

    def _join_assertions() -> list[dict]:
        """与片段相关的本人已登记关联声明（有界，供模型发现 evidenceRef）。

        只是使用侧发现，不是治理认证；explicit_key join 的充分证据仍是
        这些 assertion 本身（query 核会按归属/两端/键型重验）。
        """
        from app.ontologies.agent_query.models import AgentJoinAssertion

        fragment_types = {t.id for t in types}
        rows = (
            db.query(AgentJoinAssertion)
            .filter(
                AgentJoinAssertion.created_by == str(user.id),
                or_(
                    (AgentJoinAssertion.left_ontology_id == ontology_id)
                    & AgentJoinAssertion.left_object_type_id.in_(
                        fragment_types),
                    (AgentJoinAssertion.right_ontology_id == ontology_id)
                    & AgentJoinAssertion.right_object_type_id.in_(
                        fragment_types),
                ))
            .order_by(AgentJoinAssertion.created_at.desc())
            .limit(MAX_JOIN_ASSERTIONS).all())
        return [
            {
                "evidenceRef": f"assertion:{row.id}",
                "category": "user_asserted",
                "leftOntologyId": row.left_ontology_id,
                "leftObjectTypeId": row.left_object_type_id,
                "leftProperty": row.left_property,
                "rightOntologyId": row.right_ontology_id,
                "rightObjectTypeId": row.right_object_type_id,
                "rightProperty": row.right_property,
                "keyType": row.key_type,
                "businessMeaning": row.business_meaning,
            }
            for row in rows
        ]

    import json as _json

    payload = {
        "ontology": {"id": project.id, "name": project.name},
        "release": {"id": context.release.id,
                    "version": context.release.version_number},
        "objectTypes": [
            {
                "id": t.id, "name": t.name, "displayName": t.display_name,
                "primaryKey": t.primary_key,
                "properties": _props(t),
                "sourceRefs": {
                    "ontologyId": project.id,
                    "releaseId": context.release.id,
                    "objectTypeId": t.id,
                },
            }
            for t in types
        ],
        "joinAssertions": _join_assertions(),
        "linkTypes": [
            {
                "id": lt.id, "name": lt.name, "displayName": lt.display_name,
                "cardinality": lt.cardinality,
                "sourceObjectTypeId": lt.source_object_type_id,
                "targetObjectTypeId": lt.target_object_type_id,
                "sourceRefs": {
                    "ontologyId": project.id,
                    "releaseId": context.release.id,
                    "linkTypeId": lt.id,
                },
            }
            for lt in links
        ],
        "actions": [
            {
                "id": a.id, "name": a.name, "displayName": a.display_name,
                "objectTypeId": a.object_type_id,
                "requiresApproval": bool(a.requires_approval),
                "parameters": [
                    {
                        "name": p.get("name"),
                        "displayName": p.get("displayName") or p.get("name"),
                        "type": p.get("type", "string"),
                        "required": bool(p.get("required")),
                        "defaultValue": p.get(
                            "defaultValue", p.get("default_value")),
                    }
                    for p in (a.parameters or [])
                    if isinstance(p, dict) and p.get("name")
                ],
                "sourceRefs": {
                    "ontologyId": project.id,
                    "releaseId": context.release.id,
                    "actionId": a.id,
                },
            }
            for a in actions
        ],
    }
    if len(_json.dumps(payload, ensure_ascii=False).encode("utf-8")) \
            > MAX_PAYLOAD_BYTES:
        raise StructureError(422, "structure_budget_exceeded",
                             f"结构片段超过 {MAX_PAYLOAD_BYTES} 字节上限")
    return payload
