"""Ontology-owned read port, assembled into the text query engine by main.

Factories are injected by the composition root: the ontology domain never
imports the data-channel query package, even through a function-local import.
This resolver uses the caller's pinned query transaction and never commits it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.models import User

from app.auth.permissions import user_has_menu_access
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_runtime.boundary import AgentScope, ToolError
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.agent_runtime.read_queries import (
    BOOLEAN_TYPES, INTEGER_TYPES, NUMBER_TYPES, invalid_typed_property,
    property_definitions, typed_property,
)
from app.ontologies.formal_modeling.models import ObjectInstance
from app.ontologies.projects.models import OntologyProject
from app.ontologies.release_context import current_release_context


def _fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_ontology_relation(db, user, alias, binding, limits, *,
                              column_spec_factory, relation_factory):
    """Return only an authorized, exact-release, typed SELECT relation.

    The query engine must establish at least REPEATABLE READ before calling the port.
    Profile creation is intentionally forbidden here because its legacy helper
    commits; a missing profile is a clear configuration rejection instead.
    """
    if not getattr(user, "id", None):
        raise HTTPException(401, detail={"code": "authentication_required"})
    if not user_has_menu_access(db, user, "agent"):
        raise HTTPException(403, detail={"code": "MENU_ACCESS_DENIED",
            "message": "当前角色无权访问此功能", "menu_key": "agent"})
    ontology_id = binding.ontology_id
    release_id = binding.release_id
    object_type_id = binding.object_type_id
    if not ontology_id or not release_id or not object_type_id:
        raise HTTPException(422, detail={"code": "invalid_ontology_binding",
                                       "message": "ontologyId/releaseId/objectTypeId均为必填"})
    require_ontology_access(db, ontology_id, user, write=False)
    # Shared row locks fence release activation and profile edits until the
    # surrounding query transaction completes. SQLite is unit-test only.
    db.execute(select(OntologyProject).where(OntologyProject.id == ontology_id)
               .with_for_update(read=True)).scalar_one()
    context = current_release_context(db, ontology_id, expected_release_id=release_id)
    profile = db.execute(select(AgentProfile).where(
        AgentProfile.ontology_id == ontology_id).with_for_update(read=True)
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if profile is None or not profile.enabled:
        raise HTTPException(403, detail={"code": "profile_disabled",
                                       "message": "本体助手未启用或尚未配置"})
    if int(profile.max_rows_per_query or 0) <= 0:
        raise HTTPException(403, detail={"code": "profile_query_disabled"})
    scope = AgentScope(db, context.project, profile, context)
    try:
        object_type = scope.require_object_type(object_type_id)
    except ToolError as exc:
        raise HTTPException(403, detail={"code": "object_type_forbidden",
                                       "message": str(exc)}) from exc
    from app.ontologies.mappings.remote_binding import get_remote_binding
    if get_remote_binding(context.snapshot, object_type.id) is not None:
        raise HTTPException(422, detail={
            "code": "remote_ontology_semantic_query_required",
            "message": "该对象使用远程绑定，请使用本体统一查询入口；不读取空的本地事实或隐式物化",
        })
    definitions = property_definitions(object_type)
    allowed = scope.allowed_property_names(object_type.id)
    postgres = db.get_bind().dialect.name == "postgresql"
    columns, expressions, invalid_values = {}, [], []
    for name, definition in definitions.items():
        if allowed is not None and name not in allowed:
            continue
        declared = str(definition.get("type") or "string").lower()
        kind = ("integer" if declared in INTEGER_TYPES else
                "decimal" if declared in NUMBER_TYPES else
                "boolean" if declared in BOOLEAN_TYPES else
                "json" if declared in {"json", "object", "array"} else "string")
        columns[name] = column_spec_factory(name=name, type=kind,
                                           nullable=not definition.get("required"),
                                           source_type=declared)
        expressions.append(typed_property(definition, name, postgres=postgres).label(name))
        if postgres:
            invalid = invalid_typed_property(definition, name)
            if invalid is not None:
                invalid_values.append(invalid)
    # A relation with no business fields is still countable without exposing
    # arbitrary JSON. The technical id is explicit and cannot overwrite a field.
    if "_instance_id" in definitions:
        raise HTTPException(422, detail={"code": "reserved_query_field",
                                       "message": "_instance_id为查询技术字段保留名"})
    columns["_instance_id"] = column_spec_factory(name="_instance_id", type="string", nullable=False)
    expressions.insert(0, ObjectInstance.id.label("_instance_id"))
    statement = select(*expressions).where(
        ObjectInstance.ontology_id == ontology_id,
        ObjectInstance.ontology_release_id == release_id,
        ObjectInstance.object_type_id == object_type.id)
    if invalid_values:
        # EXISTS scans the full exact release until a counterexample appears;
        # it is not a first-page/row-budget sample. Only authorized fields enter
        # this check, and the caller already installed its SQL resource budget.
        invalid_query = select(ObjectInstance.id).where(
            ObjectInstance.ontology_id == ontology_id,
            ObjectInstance.ontology_release_id == release_id,
            ObjectInstance.object_type_id == object_type.id,
            or_(*invalid_values))
        if db.scalar(select(invalid_query.exists())):
            raise HTTPException(422, detail={"code": "invalid_ontology_typed_value",
                "message": "发布范围含非空非法类型值，未返回部分或类型降级统计"})
    auth_material = {
        "userId": str(user.id), "role": str(getattr(user, "role", "")),
        "enabled": profile.enabled, "objects": profile.allowed_object_type_ids,
        "properties": profile.allowed_properties, "maxRows": profile.max_rows_per_query,
    }
    auth_hash = _fingerprint(auth_material)
    schema_hash = _fingerprint({name: definition for name, definition in definitions.items()
                                if allowed is None or name in allowed})

    def recheck():
        # The fact rows remain pinned; revocation must use a new metadata view,
        # not the fact transaction's older REPEATABLE READ snapshot.
        with Session(bind=db.get_bind(), autoflush=False) as fresh_db:
            fresh_user = fresh_db.get(User, user.id)
            if fresh_user is None or not fresh_user.is_active:
                raise HTTPException(403, detail={"code": "query_authorization_changed"})
            if not user_has_menu_access(fresh_db, fresh_user, "agent"):
                raise HTTPException(403, detail={"code": "MENU_ACCESS_DENIED",
                    "message": "当前角色无权访问此功能", "menu_key": "agent"})
            require_ontology_access(fresh_db, ontology_id, fresh_user, write=False)
            current_release_context(fresh_db, ontology_id, expected_release_id=release_id)
            fresh = fresh_db.execute(select(AgentProfile).where(
                AgentProfile.ontology_id == ontology_id)).scalar_one_or_none()
            if fresh is None:
                raise HTTPException(403, detail={"code": "query_authorization_changed"})
            current = {"userId": str(fresh_user.id), "role": fresh_user.role,
                       "enabled": fresh.enabled, "objects": fresh.allowed_object_type_ids,
                       "properties": fresh.allowed_properties, "maxRows": fresh.max_rows_per_query}
            if _fingerprint(current) != auth_hash:
                raise HTTPException(403, detail={"code": "query_authorization_changed"})

    return relation_factory(
        alias=alias, table=statement.subquery(), columns=columns,
        state={"alias": alias, "kind": "ontology", "id": ontology_id,
               "versionId": release_id, "ontologyId": ontology_id,
               "releaseId": release_id, "objectTypeId": object_type.id,
               "asOf": datetime.now(timezone.utc).isoformat(),
               "maxPageSize": min(int(profile.max_rows_per_query), 200),
               "consistency": "repeatable_read", "completeness": "complete"},
        auth_fingerprint=auth_hash, schema_fingerprint=schema_hash,
        connection_key="platform", platform=True, recheck=recheck)
