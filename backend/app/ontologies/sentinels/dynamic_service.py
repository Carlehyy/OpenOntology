"""Governed assistant-created Sentinel overlay.

Dynamic Sentinels are intentionally outside immutable ontology snapshots.  The
service owns their origin, validates every complete definition against the
current release and AgentProfile boundary, and requires a current full-data
trial before enablement.  LLM output is only an untrusted candidate.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.ontology import OntologyProject
from app.models.sentinel import Sentinel, SentinelMatchState
from app.ontologies.agent_runtime import schemas as AgentSchemas
from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.release_context import CurrentReleaseContext, current_release_context
from app.ontologies.sentinels.validation import validate_sentinels
from app.ontologies.versions.snapshot_contract import (
    snapshot_models,
)


ORIGIN_BUILTIN = "release_builtin"
ORIGIN_DYNAMIC = "assistant_dynamic"

_DEFINITION_KEYS = {
    "name", "displayName", "display_name", "description", "bindings", "links",
    "condition", "conditionRows", "condition_rows", "conditionLogic",
    "condition_logic", "primaryAlias", "primary_alias", "actionIds",
    "action_ids", "actions", "actionParameters", "action_parameters",
    "onChange", "on_change", "onSchedule", "on_schedule",
    "scanIntervalSeconds", "scan_interval_seconds", "triggerMode",
    "trigger_mode", "muted", "pattern",
}
_BINDING_KEYS = {
    "alias", "objectTypeId", "object_type_id", "objectType", "object_type", "filter",
}
_LINK_KEYS = {
    "from", "fromAlias", "linkTypeId", "link_type_id", "linkType", "link_type", "to",
}
_PARAM_TEMPLATE = re.compile(
    r"\{\{\s*(?P<alias>[^.\s{}]+)\.(?P<property>[^{}\s]+)\s*\}\}"
)
_EVENT_PARAMETER_PROPERTIES = {
    "edge", "matchKey", "occurredAt", "sentinelId", "sentinelName",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error(code: str, message: str, field: str = "") -> dict:
    return {"code": code, "message": message, "field": field}


def _raise_validation(report: dict, message: str = "动态哨兵校验未通过") -> None:
    if report.get("passed"):
        return
    raise HTTPException(422, detail={
        "code": "dynamic_sentinel_validation_failed",
        "message": message,
        "errors": report.get("errors") or [],
    })


def require_current_release(db: Session, ontology_id: str,
                            release_id: str) -> CurrentReleaseContext:
    """Bind an overlay to the exact immutable current release.

    ``OntologyProject.status`` is a legacy editing-compatibility field.  A
    project can have drafts while v0/v1/... remains its released runtime, so
    release validity is decided exclusively by ``current_release_context``.
    """
    return current_release_context(
        db, ontology_id, expected_release_id=release_id)


def _lock_current_release(db: Session, context: CurrentReleaseContext) -> None:
    """Serialize overlay writes with promotion/rollback of the release pointer."""
    project = (db.query(OntologyProject).filter(
        OntologyProject.id == context.project.id,
        # PostgreSQL CDC holds a FOR KEY SHARE release lease on a dedicated
        # connection. FOR NO KEY UPDATE is compatible with that lease but still
        # conflicts with promotion/rollback's FOR UPDATE pointer switch.
    ).with_for_update(key_share=True).populate_existing().first())
    if project is None:
        raise HTTPException(404, "Ontology not found")
    if project.current_release_id != context.id:
        raise HTTPException(409, detail={
            "code": "release_context_changed",
            "message": "当前发布版本已变化，请刷新智能助手后重试",
            "expectedReleaseId": context.id,
            "currentReleaseId": project.current_release_id,
        })


@contextmanager
def _sentinel_write_fence(db: Session, sentinel_id: str):
    """Share the evaluator's cross-process fence with one management write."""
    # Lazy import avoids the evaluator -> action engine -> router import graph at
    # module initialization while keeping one authoritative lock implementation.
    from app.ontologies.sentinels.evaluator import _sentinel_execution_lock

    with _sentinel_execution_lock(db, sentinel_id):
        try:
            yield
        except Exception:
            # Release project/row locks before releasing the advisory lock.  This
            # preserves the global sentinel -> project -> row lock order even on
            # validation failures.
            db.rollback()
            raise


def _resolve_ref(scope, pool_name: str, ref: Any, label: str) -> str:
    value = str(ref or "").strip()
    if not value:
        raise ValueError(f"{label}不能为空")
    resolver = getattr(scope, f"require_{pool_name}")
    return str(resolver(value).id)


def _reject_unknown_fields(raw: dict, allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(f"{label} 包含未声明字段: {', '.join(unknown)}")


def _strict_bool(raw: dict, camel: str, snake: str, default: bool) -> bool:
    if camel in raw:
        value = raw[camel]
    elif snake in raw:
        value = raw[snake]
    else:
        return default
    if type(value) is not bool:
        raise ValueError(f"{camel} 必须是 JSON 布尔值 true/false")
    return value


def normalize_candidate(raw: dict, scope) -> dict:
    """Convert model-friendly name references to canonical release ids."""
    if not isinstance(raw, dict):
        raise ValueError("definition 必须是对象")
    _reject_unknown_fields(raw, _DEFINITION_KEYS, "definition")
    bindings = []
    for item in raw.get("bindings") or []:
        if not isinstance(item, dict):
            raise ValueError("bindings 中的每一项必须是对象")
        _reject_unknown_fields(item, _BINDING_KEYS, "binding")
        ref = (item.get("objectTypeId") or item.get("object_type_id")
               or item.get("objectType") or item.get("object_type"))
        bindings.append({
            "alias": str(item.get("alias") or "").strip(),
            "objectTypeId": _resolve_ref(scope, "object_type", ref, "对象类型"),
            "filter": item.get("filter"),
        })

    links = []
    for item in raw.get("links") or []:
        if not isinstance(item, dict):
            raise ValueError("links 中的每一项必须是对象")
        _reject_unknown_fields(item, _LINK_KEYS, "link")
        ref = (item.get("linkTypeId") or item.get("link_type_id")
               or item.get("linkType") or item.get("link_type"))
        links.append({
            "from": str(item.get("from") or item.get("fromAlias") or "").strip(),
            "linkTypeId": _resolve_ref(scope, "link_type", ref, "关系类型"),
            "to": str(item.get("to") or "").strip(),
        })

    raw_actions = raw.get("actionIds", raw.get("action_ids", raw.get("actions", []))) or []
    action_ids = [_resolve_ref(scope, "action", ref, "动作") for ref in raw_actions]
    raw_parameters = raw.get("actionParameters", raw.get("action_parameters", {})) or {}
    action_parameters: dict[str, Any] = {}
    if not isinstance(raw_parameters, dict):
        raise ValueError("actionParameters 必须是对象")
    for action_ref, parameters in raw_parameters.items():
        action_id = _resolve_ref(scope, "action", action_ref, "动作")
        action_parameters[action_id] = parameters

    name = str(raw.get("name") or "").strip()
    display_name = str(raw.get("displayName") or raw.get("display_name") or "").strip()
    primary_alias = str(
        raw.get("primaryAlias") or raw.get("primary_alias")
        or (bindings[0]["alias"] if bindings else "")
    ).strip()
    return {
        "name": name,
        "displayName": display_name,
        "description": raw.get("description"),
        "bindings": bindings,
        "links": links,
        "condition": raw.get("condition"),
        "conditionRows": raw.get("conditionRows", raw.get("condition_rows", [])) or [],
        "conditionLogic": raw.get("conditionLogic", raw.get("condition_logic", "and")),
        "primaryAlias": primary_alias,
        "actionIds": action_ids,
        "actionParameters": action_parameters,
        "onChange": _strict_bool(raw, "onChange", "on_change", True),
        "onSchedule": _strict_bool(raw, "onSchedule", "on_schedule", False),
        "scanIntervalSeconds": raw.get(
            "scanIntervalSeconds", raw.get("scan_interval_seconds", 300)),
        "triggerMode": raw.get("triggerMode", raw.get("trigger_mode", "on_enter")),
        "pattern": (
            raw.get("pattern")
            if isinstance(raw.get("pattern"), dict) else None),
        "muted": _strict_bool(raw, "muted", "muted", False),
    }


def _property_names(object_type: Any | None) -> set[str]:
    return {
        str(item.get("name"))
        for item in ((object_type.properties or []) if object_type else [])
        if isinstance(item, dict) and item.get("name")
    }


def _expression_property_errors(
    expression: str | None,
    alias_properties: dict[str, set[str]],
    field: str,
) -> list[dict]:
    """Validate release-schema property references without executing data."""
    raw = str(expression or "").strip().rstrip(";").strip()
    if not raw:
        return []
    try:
        tree = ast.parse(raw, mode="eval")
    except SyntaxError:
        return []  # The shared safe-expression validator reports syntax errors.
    missing: set[str] = set()
    dynamic: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            alias = node.value.id
            if alias in alias_properties and node.attr not in alias_properties[alias]:
                missing.add(f"{alias}.{node.attr}")
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            alias = node.value.id
            if alias not in alias_properties:
                continue
            key_node = node.slice
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                if key_node.value not in alias_properties[alias]:
                    missing.add(f"{alias}[{key_node.value!r}]")
            else:
                dynamic.add(alias)
    errors = [
        _error(
            "sentinel_expression_property_not_found",
            f"表达式引用了发布版本中不存在的属性: {reference}",
            field,
        )
        for reference in sorted(missing)
    ]
    errors.extend(
        _error(
            "sentinel_dynamic_property_forbidden",
            f"表达式不允许通过动态下标访问 {alias} 的属性",
            field,
        )
        for alias in sorted(dynamic)
    )
    return errors


def _dynamic_contract_errors(definition: dict, models: dict) -> list[dict]:
    """Assistant-only checks beyond the shared graph-editor release gate."""
    errors: list[dict] = []
    object_by_id = {item.id: item for item in models["objectTypes"]}
    alias_properties = {
        item["alias"]: _property_names(object_by_id.get(item["objectTypeId"]))
        for item in definition["bindings"]
    }
    for index, binding in enumerate(definition["bindings"]):
        props = alias_properties.get(binding["alias"], set())
        errors.extend(_expression_property_errors(
            binding.get("filter"),
            {binding["alias"]: props, "obj": props},
            f"bindings[{index}].filter",
        ))
    errors.extend(_expression_property_errors(
        definition.get("condition"), alias_properties, "condition"))

    primary = definition["primaryAlias"]
    for action_id, parameters in definition["actionParameters"].items():
        if not isinstance(parameters, dict):
            continue  # The shared release validator reports this shape error.
        for parameter_name, spec in parameters.items():
            if not isinstance(spec, str) or ("{{" not in spec and "}}" not in spec):
                continue
            field = f"actionParameters.{action_id}.{parameter_name}"
            matches = list(_PARAM_TEMPLATE.finditer(spec))
            remainder = _PARAM_TEMPLATE.sub("", spec)
            if not matches or "{{" in remainder or "}}" in remainder:
                errors.append(_error(
                    "invalid_sentinel_parameter_template",
                    f"参数模板格式非法: {spec}", field))
                continue
            for match in matches:
                alias = match.group("alias")
                prop = match.group("property")
                if alias in {"event", "edge"}:
                    if prop not in _EVENT_PARAMETER_PROPERTIES:
                        errors.append(_error(
                            "sentinel_event_property_not_found",
                            f"参数模板引用的事件字段不存在: {alias}.{prop}",
                            field))
                    continue
                alias = primary if alias in {"primary", "target"} else alias
                if alias not in alias_properties:
                    errors.append(_error(
                        "sentinel_parameter_alias_not_found",
                        f"参数模板引用的 alias 不存在: {alias}", field))
                elif prop != "id" and prop not in alias_properties[alias]:
                    errors.append(_error(
                        "sentinel_parameter_property_not_found",
                        f"参数模板引用的发布属性不存在: {alias}.{prop}", field))
    encoded = json.dumps(definition, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 65536:
        errors.append(_error(
            "dynamic_sentinel_definition_too_large",
            "动态哨兵完整定义不能超过 64 KiB",
        ))
    return errors


def _contract_hash(snapshot: dict, definition: dict) -> str:
    ids = {
        "objectTypes": {str(item.get("objectTypeId")) for item in definition["bindings"]},
        "linkTypes": {str(item.get("linkTypeId")) for item in definition["links"]},
        "actions": {str(item) for item in definition["actionIds"]},
    }
    contract = {
        key: sorted(
            [item for item in snapshot.get(key, []) if str(item.get("id")) in ids[key]],
            key=lambda item: str(item.get("id") or ""),
        )
        for key in ids
    }
    payload = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_definition(db: Session, context: CurrentReleaseContext, scope,
                        raw: dict, *, sentinel_id: str | None = None) -> tuple[dict, dict]:
    errors: list[dict] = []
    try:
        normalized = normalize_candidate(raw, scope)
        parsed = AgentSchemas.DynamicSentinelDefinition.model_validate(normalized)
        definition = parsed.model_dump(mode="json", by_alias=True)
    except (ValidationError, ValueError, TypeError, ToolError) as exc:
        report = {
            "passed": False, "releaseId": context.id,
            "validatedAt": _now().isoformat(),
            "contractHash": None,
            "errors": [_error("invalid_definition", str(exc))],
        }
        return {}, report

    duplicate = db.query(Sentinel).filter(
        Sentinel.ontology_id == context.project.id,
        Sentinel.retired_at.is_(None),
        Sentinel.name == definition["name"],
    )
    if sentinel_id:
        duplicate = duplicate.filter(Sentinel.id != sentinel_id)
    if duplicate.first() is not None:
        errors.append(_error(
            "duplicate_sentinel_name",
            f"哨兵技术名称 {definition['name']} 已存在",
            "name",
        ))

    # Reuse the same deep release gate used by graph-editor Sentinels.
    try:
        models = snapshot_models(context.snapshot)
        candidate = SimpleNamespace(
            id=sentinel_id or "candidate",
            name=definition["name"],
            display_name=definition["displayName"],
            bindings=definition["bindings"],
            links=definition["links"],
            condition=definition.get("condition"),
            pattern=definition.get("pattern"),
            primary_alias=definition["primaryAlias"],
            action_ids=definition["actionIds"],
            action_parameters=definition["actionParameters"],
            on_change=bool(definition.get("onChange")),
            on_schedule=bool(definition.get("onSchedule")),
            scan_interval_seconds=int(
                definition.get("scanIntervalSeconds") or 300),
            trigger_mode=definition.get("triggerMode") or "on_enter",
        )
        errors.extend(validate_sentinels(
            [candidate], models["objectTypes"], models["linkTypes"], models["actions"]))
        errors.extend(_dynamic_contract_errors(definition, models))
    except Exception as exc:  # fail closed if the shared governance gate changes
        errors.append(_error(
            "shared_validation_failed",
            f"发布级校验器执行失败: {exc}",
        ))

    report = {
        "passed": not errors,
        "releaseId": context.id,
        "validatedAt": _now().isoformat(),
        "contractHash": _contract_hash(context.snapshot, definition),
        "errors": errors,
    }
    return definition, report


def definition_from_row(row: Sentinel) -> dict:
    return {
        "name": row.name,
        "displayName": row.display_name,
        "description": row.description,
        "bindings": row.bindings or [],
        "links": row.links or [],
        "condition": row.condition,
        "conditionRows": row.condition_rows or [],
        "conditionLogic": row.condition_logic or "and",
        "primaryAlias": row.primary_alias,
        "actionIds": row.action_ids or [],
        "actionParameters": row.action_parameters or {},
        "onChange": bool(row.on_change),
        "onSchedule": bool(row.on_schedule),
        "scanIntervalSeconds": int(row.scan_interval_seconds or 300),
        "triggerMode": row.trigger_mode or "on_enter",
        "pattern": row.pattern if isinstance(row.pattern, dict) else None,
        "muted": bool(row.muted),
    }


def _apply_definition(row: Sentinel, definition: dict) -> None:
    row.name = definition["name"]
    row.display_name = definition["displayName"]
    row.description = definition.get("description")
    row.bindings = definition["bindings"]
    row.links = definition["links"]
    row.condition = definition.get("condition")
    row.condition_rows = definition.get("conditionRows") or []
    row.condition_logic = definition.get("conditionLogic") or "and"
    row.primary_alias = definition["primaryAlias"]
    row.action_ids = definition["actionIds"]
    row.action_parameters = definition["actionParameters"]
    row.on_change = definition["onChange"]
    row.on_schedule = definition["onSchedule"]
    row.scan_interval_seconds = definition["scanIntervalSeconds"]
    row.trigger_mode = definition["triggerMode"]
    row.pattern = (
        definition.get("pattern")
        if isinstance(definition.get("pattern"), dict) else None)
    row.muted = definition["muted"]


def _clear_runtime_states(db: Session, sentinel_id: str) -> None:
    """清理哨兵运行态：命中集、CEP 在途状态与事件水位一起作废。"""
    from app.ontologies.sentinels.models import (
        SentinelPatternCursor, SentinelPatternState,
    )
    db.query(SentinelPatternState).filter(
        SentinelPatternState.sentinel_id == sentinel_id).delete(
            synchronize_session=False)
    db.query(SentinelPatternCursor).filter(
        SentinelPatternCursor.sentinel_id == sentinel_id).delete(
            synchronize_session=False)
    db.query(SentinelMatchState).filter(
        SentinelMatchState.sentinel_id == sentinel_id).delete(
            synchronize_session=False)


def dynamic_row(db: Session, ontology_id: str, sentinel_id: str,
                *, for_update: bool = False) -> Sentinel:
    query = db.query(Sentinel).filter(
        Sentinel.id == sentinel_id,
        Sentinel.ontology_id == ontology_id,
        Sentinel.origin == ORIGIN_DYNAMIC,
        Sentinel.retired_at.is_(None),
    )
    if for_update:
        query = query.with_for_update().populate_existing()
    row = query.first()
    if row is None:
        # Built-in ids intentionally look nonexistent through assistant APIs.
        raise HTTPException(404, "动态哨兵不存在")
    return row


def serialize_dynamic(row: Sentinel) -> dict:
    trial = row.last_trial_report if isinstance(row.last_trial_report, dict) else None
    trial_current = bool(
        trial and trial.get("passed")
        and row.last_trial_release_id == row.bound_release_id
        and row.last_trial_revision == row.definition_revision
    )
    return {
        "id": row.id,
        "ontologyId": row.ontology_id,
        "origin": row.origin,
        "boundReleaseId": row.bound_release_id,
        "createdBy": row.created_by,
        "definitionRevision": row.definition_revision,
        "enableGeneration": int(row.enable_generation or 0),
        **definition_from_row(row),
        "enabled": bool(row.enabled),
        "status": row.status,
        "validationReport": row.validation_report,
        "lastTrialAt": row.last_trial_at.isoformat() if row.last_trial_at else None,
        "lastTrialReport": trial,
        "trialCurrent": trial_current,
        "canEnable": bool(
            trial_current
            and isinstance(row.validation_report, dict)
            and row.validation_report.get("passed")
        ),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def reconcile_release(db: Session, context: CurrentReleaseContext, scope) -> None:
    sentinel_ids = [
        str(item[0]) for item in db.query(Sentinel.id).filter(
            Sentinel.ontology_id == context.project.id,
            Sentinel.origin == ORIGIN_DYNAMIC,
            Sentinel.retired_at.is_(None),
        ).all()
    ]
    with ExitStack() as fences:
        for sentinel_id in sorted(set(sentinel_ids)):
            fences.enter_context(_sentinel_write_fence(db, sentinel_id))
        _lock_current_release(db, context)
        rows = db.query(Sentinel).filter(
            Sentinel.ontology_id == context.project.id,
            Sentinel.origin == ORIGIN_DYNAMIC,
            Sentinel.retired_at.is_(None),
        ).with_for_update().populate_existing().all()
        for row in rows:
            previous = (
                row.validation_report
                if isinstance(row.validation_report, dict) else {})
            release_changed = row.bound_release_id != context.id
            # Disabled, already-valid definitions do not need repeated writes.
            # An enabled row is deliberately revalidated at every real
            # execution boundary so a later AgentProfile restriction fails
            # closed.
            if not release_changed and not row.enabled and previous.get("passed"):
                continue
            definition, report = validate_definition(
                db, context, scope, definition_from_row(row), sentinel_id=row.id)
            trial = (
                row.last_trial_report
                if isinstance(row.last_trial_report, dict) else {})
            trial_current = bool(
                trial.get("passed")
                and row.last_trial_release_id == context.id
                and row.last_trial_revision == row.definition_revision
            )
            must_disable = (
                not report.get("passed")
                or release_changed
                or (row.enabled and not trial_current)
            )
            if must_disable:
                row.enabled = False
                row.last_trial_at = None
                row.last_trial_release_id = None
                row.last_trial_revision = None
                row.last_trial_report = None
                _clear_runtime_states(db, row.id)
            elif previous.get("passed"):
                # Same release, current trial, deep validation still passes:
                # there is no state transition to persist.
                continue
            if definition:
                _apply_definition(row, definition)
            row.bound_release_id = context.id
            row.validation_report = {
                **report,
                "compatibility": (
                    "invalid" if not report.get("passed")
                    else "review_required" if release_changed
                    else "compatible"
                ),
            }
        # Always close the project/row transaction before the advisory fences.
        # Leaving a no-op reconciliation transaction open would invert the
        # order against a concurrent single-Sentinel management write.
        db.commit()


def list_dynamic(db: Session, context: CurrentReleaseContext, scope) -> list[dict]:
    reconcile_release(db, context, scope)
    rows = db.query(Sentinel).filter(
        Sentinel.ontology_id == context.project.id,
        Sentinel.origin == ORIGIN_DYNAMIC,
        Sentinel.retired_at.is_(None),
    ).order_by(Sentinel.created_at.desc()).all()
    return [serialize_dynamic(row) for row in rows]


def create_dynamic(db: Session, context: CurrentReleaseContext, scope,
                   definition: dict, user_id: str | None) -> Sentinel:
    _lock_current_release(db, context)
    canonical, report = validate_definition(db, context, scope, definition)
    _raise_validation(report)
    row = Sentinel(
        ontology_id=context.project.id,
        origin=ORIGIN_DYNAMIC,
        bound_release_id=context.id,
        created_by=user_id,
        definition_revision=1,
        validation_report=report,
        enabled=False,
        status="published",
        source={"kind": ORIGIN_DYNAMIC, "createdBy": user_id},
    )
    _apply_definition(row, canonical)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_dynamic(db: Session, context: CurrentReleaseContext, scope,
                   sentinel_id: str, expected_revision: int,
                   definition: dict) -> Sentinel:
    with _sentinel_write_fence(db, sentinel_id):
        _lock_current_release(db, context)
        row = dynamic_row(db, context.project.id, sentinel_id, for_update=True)
        if row.definition_revision != expected_revision:
            raise HTTPException(409, detail={
                "code": "dynamic_sentinel_revision_conflict",
                "message": "动态哨兵已被其他会话修改，请刷新后重试",
                "currentRevision": row.definition_revision,
            })
        canonical, report = validate_definition(
            db, context, scope, definition, sentinel_id=row.id)
        _raise_validation(report)
        _apply_definition(row, canonical)
        row.definition_revision += 1
        row.bound_release_id = context.id
        row.validation_report = report
        row.enabled = False
        row.last_trial_at = None
        row.last_trial_release_id = None
        row.last_trial_revision = None
        row.last_trial_report = None
        _clear_runtime_states(db, row.id)
        db.commit()
        db.refresh(row)
        return row


def run_trial(db: Session, context: CurrentReleaseContext, scope,
              sentinel_id: str) -> Sentinel:
    with _sentinel_write_fence(db, sentinel_id):
        _lock_current_release(db, context)
        row = dynamic_row(db, context.project.id, sentinel_id, for_update=True)
        canonical, validation = validate_definition(
            db, context, scope, definition_from_row(row), sentinel_id=row.id)
        _raise_validation(validation, "动态哨兵不再符合当前发布版本，试跑已拒绝")
        _apply_definition(row, canonical)
        from app.ontologies.sentinels.evaluator import preview_sentinel
        report = preview_sentinel(db, context.project.id, row, context.id)
        row.bound_release_id = context.id
        row.validation_report = validation
        row.last_trial_at = _now()
        row.last_trial_release_id = context.id
        row.last_trial_revision = row.definition_revision
        row.last_trial_report = report
        if not report.get("passed"):
            row.enabled = False
        db.commit()
        db.refresh(row)
        return row


def set_enabled(db: Session, context: CurrentReleaseContext, scope,
                sentinel_id: str, expected_revision: int,
                enabled: bool) -> Sentinel:
    with _sentinel_write_fence(db, sentinel_id):
        _lock_current_release(db, context)
        row = dynamic_row(db, context.project.id, sentinel_id, for_update=True)
        if row.definition_revision != expected_revision:
            raise HTTPException(409, detail={
                "code": "dynamic_sentinel_revision_conflict",
                "message": "动态哨兵已被修改，请刷新后重试",
                "currentRevision": row.definition_revision,
            })
        was_enabled = bool(row.enabled)
        if enabled:
            canonical, validation = validate_definition(
                db, context, scope, definition_from_row(row), sentinel_id=row.id)
            _raise_validation(validation, "启用前强校验未通过")
            _apply_definition(row, canonical)
            trial = (
                row.last_trial_report
                if isinstance(row.last_trial_report, dict) else {})
            if (row.last_trial_release_id != context.id
                    or row.last_trial_revision != row.definition_revision
                    or not trial.get("passed")):
                raise HTTPException(409, detail={
                    "code": "dynamic_sentinel_trial_required",
                    "message": "启用前必须在当前发布版本上完成一次通过的全量试跑",
                })
            row.validation_report = validation
            row.bound_release_id = context.id
        row.enabled = enabled
        if enabled and not was_enabled:
            row.enable_generation = int(row.enable_generation or 0) + 1
            from app.ontologies.sentinels.cdc import (
                CAPTURE_SUPPRESSED_KEY,
                SUPPRESS_KEY,
                capture_dynamic_activation,
            )
            # Dynamic enablement is a management transaction, not an editor
            # save.  A reused Session can still carry the editor's dispatch
            # suppression marker after its synchronous evaluation; letting
            # that stale marker leak here would silently drop the required
            # activation event.  Mapping projection sets the explicit capture
            # marker and keeps its own chain-scoped barrier semantics.
            if (
                db.info.get(SUPPRESS_KEY)
                and not db.info.get(CAPTURE_SUPPRESSED_KEY)
            ):
                db.info.pop(SUPPRESS_KEY, None)
            activation = capture_dynamic_activation(
                db,
                ontology_id=context.project.id,
                ontology_release_id=context.id,
                sentinel_id=row.id,
                definition_revision=row.definition_revision,
                enable_generation=row.enable_generation,
            )
            if activation is None:
                raise HTTPException(503, detail={
                    "code": "dynamic_sentinel_activation_unavailable",
                    "message": "动态哨兵初始化任务无法持久化，启用已回滚",
                })
        elif not enabled:
            _clear_runtime_states(db, row.id)
        db.commit()
        db.refresh(row)
        return row


def retire_dynamic(db: Session, context: CurrentReleaseContext, sentinel_id: str,
                   expected_revision: int | None = None) -> None:
    with _sentinel_write_fence(db, sentinel_id):
        _lock_current_release(db, context)
        row = dynamic_row(db, context.project.id, sentinel_id, for_update=True)
        if (expected_revision is not None
                and row.definition_revision != expected_revision):
            raise HTTPException(409, detail={
                "code": "dynamic_sentinel_revision_conflict",
                "message": "动态哨兵已被修改，请刷新后重试",
                "currentRevision": row.definition_revision,
            })
        row.enabled = False
        row.retired_at = _now()
        row.definition_revision += 1
        _clear_runtime_states(db, row.id)
        db.commit()


def proposal(db: Session, context: CurrentReleaseContext, scope,
             operation: str, *, sentinel_id: str | None = None,
             definition: dict | None = None,
             expected_revision: int | None = None) -> dict:
    row = None
    canonical = None
    if operation == "create":
        canonical, report = validate_definition(db, context, scope, definition or {})
    else:
        row = dynamic_row(db, context.project.id, sentinel_id or "")
        if expected_revision is not None and row.definition_revision != expected_revision:
            raise HTTPException(409, detail={
                "code": "dynamic_sentinel_revision_conflict",
                "message": "动态哨兵已被其他会话修改",
                "currentRevision": row.definition_revision,
            })
        if operation == "update":
            canonical, report = validate_definition(
                db, context, scope, definition or {}, sentinel_id=row.id)
        else:
            canonical = definition_from_row(row)
            _, report = validate_definition(
                db, context, scope, canonical, sentinel_id=row.id)
    status = "success" if report.get("passed") else "failed"
    validation_errors = [
        item.get("message") or str(item) for item in report.get("errors") or []
    ]
    if operation == "enable" and row is not None:
        serialized = serialize_dynamic(row)
        if not serialized["canEnable"]:
            status = "failed"
            validation_errors.append("启用前必须在当前发布版本上完成一次通过的全量试跑")
    return {
        "kind": "sentinel",
        "proposalId": f"sentinel-{operation}-{int(_now().timestamp() * 1000)}",
        "operation": operation,
        "sentinelId": row.id if row is not None else None,
        "sentinelName": (
            (canonical or {}).get("displayName")
            or (row.display_name if row is not None else "动态哨兵")
        ),
        "releaseId": context.id,
        "expectedRevision": row.definition_revision if row is not None else None,
        "definition": canonical,
        "status": status,
        "validationErrors": validation_errors,
        "validationReport": report,
    }
