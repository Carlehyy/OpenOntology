"""Transactional effect execution and persistence for formal Actions.

The canonical action_engine owns the global lock and phase orchestration. This
module interprets ordered effect rules and commits their facts/log atomically.
"""
from __future__ import annotations

import time
import uuid
from functools import partial
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ontology_formal import (
    ActionExecutionLog,
    LinkInstance,
    LinkType,
    ObjectInstance,
    ObjectType,
)
from app.ontologies.formal_modeling.action_execution_context import (
    ActionDefinitionResolution,
    ExecutedActionEffects,
    PreparedActionExecution,
)
from app.ontologies.formal_modeling.action_effect_persistence import (
    _preview_derived_projection,
    _record_and_recompute,
)
from app.ontologies.formal_modeling.action_notification_effect import (
    _execute_internal_notification,
)
from app.ontologies.formal_modeling.action_runtime_support import (
    RuleExecutionError,
    _current_release_error,
    _fail_log,
    _idempotent_replay,
    _log_to_dict,
    _match_state_id,
    _now,
    _preview_find,
    _preview_instance_values,
    _preview_iter,
    _preview_link_values,
    _resolve_value,
    _rule_identity,
    _sentinel_id_from_execution_lineage,
    _validate_link_candidate,
    _validate_link_write,
    _validate_object_candidate,
    _validate_object_write,
    logger,
)
from app.ontologies.formal_modeling.action_validation import (
    _definition_properties,
    _validate_expression_property_references,
)
from app.ontologies.formal_modeling.derived import DerivedComputationError
from app.ontologies.formal_modeling.facts import (
    record_link_fact,
    record_object_presence,
)
from app.ontologies.formal_modeling.safe_eval import (
    SafeEvalError,
    safe_eval,
)
from app.ontologies.formal_modeling.webhook_dispatcher import (
    WebhookDispatchError,
    dispatch_webhook,
    preview_webhook,
)


def _execute_action_effects(
        db: Session,
        ontology_id: str,
        body,
        *,
        actor_id: Optional[str],
        caused_by_fact: Optional[str],
        preview_context: Optional[dict],
        resolution: ActionDefinitionResolution,
        prepared: PreparedActionExecution,
) -> ExecutedActionEffects | dict[str, Any]:
    start = resolution.start
    preview_only = resolution.preview_only
    definition_context = resolution.definition_context
    action = resolution.action
    ontology_version = resolution.ontology_version
    ontology_release_id = resolution.ontology_release_id
    target_snapshot = prepared.target_snapshot
    instance_release_id = prepared.instance_release_id
    params = prepared.params
    rules = prepared.rules
    target_props = prepared.target_props
    target_instance = prepared.target_instance


    # Pre-allocate the durable success-log identity before mutations.  Facts
    # have no foreign-key dependency on it, so every ordered mutation can use
    # one stable causal pointer while the log itself is inserted only after all
    # local rules have succeeded.  Any failure rolls all of them back.
    execution_log_id = str(uuid.uuid4())
    notification_sentinel_id = (
        _sentinel_id_from_execution_lineage(db, ontology_id, body)
        if not body.dry_run and any(
            rule.get("type") == "notification" for rule in rules)
        else None
    )
    causal = caused_by_fact or execution_log_id
    src = f"action://{action.name or action.id}"

    record_and_recompute = partial(
        _record_and_recompute,
        db,
        ontology_id,
        source=src,
        actor_id=actor_id,
        caused_by=causal,
        ontology_version=ontology_version,
        ontology_release_id=ontology_release_id,
        definition_context=definition_context,
        instance_release_id=instance_release_id,
    )

    # 执行规则（原子：任一规则失败 → 全部回滚 → 落 failed 日志）
    effects: list[dict] = []
    created_by_type: dict[str, ObjectInstance] = {}   # objectTypeId → 本次创建的实例（供 created_object 引用）
    declared_created_types: set[str] = set()          # dry-run 也要验证 created_object 链路
    pending_links: list[dict] = []                    # 链接事实缓冲 {link_id, link_type_id, exists}
    deferred_webhooks: list[tuple[int, dict, str, dict]] = []
    dry_run_created_objects: list = []
    dry_run_links: list = []
    dry_run_deleted_link_ids: set[str] = set()
    dry_run_target_properties = (
        dict(target_instance.properties or {})
        if body.dry_run and target_instance is not None
        else None
    )

    preview_derived = partial(
        _preview_derived_projection,
        db,
        ontology_id,
        preview_context=preview_context,
        definition_context=definition_context,
        instance_release_id=instance_release_id,
        dry_run_created_objects=dry_run_created_objects,
    )
    dry_run_target_computed = (
        dict(target_instance.computed or {})
        if body.dry_run and target_instance is not None
        else None
    )

    try:
        for rule_ordinal, rule in enumerate(
                sorted(rules, key=lambda r: r.get("order", 0))):
            if not rule.get("enabled", True):
                continue
            rtype = rule.get("type")
            rname = rule.get("name") or rtype
            cfg = rule.get("config", {})

            if rtype == "validation":
                # Already evaluated fail-closed above; it has no mutation phase.
                continue

            if rtype == "create_object":
                ot_id = cfg.get("targetObjectTypeId")
                ot = (
                    _preview_find(
                        definition_context, "object_types", ot_id)
                    if definition_context is not None else
                    db.query(ObjectType).filter(
                        ObjectType.id == ot_id,
                        ObjectType.ontology_id == ontology_id).first()
                )
                if not ot:
                    raise RuleExecutionError(rname, f"目标对象类型不存在: {ot_id}")
                declared_created_types.add(ot_id)
                mappings = cfg.get("propertyMappings", [])
                if not isinstance(mappings, list):
                    raise RuleExecutionError(rname, "propertyMappings 必须是数组")
                target_definitions = _definition_properties(ot)
                props: dict = {}
                for m in mappings:
                    if not isinstance(m, dict) or not m.get("targetProperty"):
                        raise RuleExecutionError(
                            rname, "属性映射缺少 targetProperty")
                    target_property = str(m["targetProperty"])
                    definition = target_definitions.get(target_property)
                    if definition and (
                        definition.get("source") == "computed"
                        or bool(definition.get("computed"))
                    ):
                        raise RuleExecutionError(
                            rname,
                            f"派生属性不能由 create_object 映射写入: "
                            f"{target_property}",
                        )
                    try:
                        props[target_property] = _resolve_value(
                            m, params, target_props, db, ontology_id,
                            ontology_release_id=instance_release_id,
                            preview_context=definition_context)
                    except SafeEvalError as e:
                        raise RuleExecutionError(rname, f"属性映射「{m.get('targetProperty')}」取值失败: {e}")
                instance_id = str(uuid.uuid4())
                pk_prop = next((p for p in (ot.properties or [])
                                if isinstance(p, dict)
                                and (p.get("id") == ot.primary_key
                                     or p.get("name") == ot.primary_key)),
                               None)
                if pk_prop and not props.get(pk_prop["name"]):
                    props = dict(props)
                    props[pk_prop["name"]] = instance_id
                if not body.dry_run:
                    inst = ObjectInstance(id=instance_id,
                                          ontology_id=ontology_id,
                                          ontology_release_id=instance_release_id,
                                          object_type_id=ot_id,
                                          properties=props, source="action")
                    db.add(inst)
                    record_object_presence(
                        db,
                        ontology_id=ontology_id,
                        instance_id=instance_id,
                        object_type_id=ot_id,
                        source=src,
                        actor_id=actor_id,
                        caused_by=causal,
                        ontology_version=ontology_version,
                        ontology_release_id=ontology_release_id,
                    )
                    _validate_object_write(
                        db, ontology_id, instance_release_id, inst.id, rname,
                        definition_context=definition_context)
                    try:
                        record_and_recompute(
                            inst,
                            old_props=None,
                            new_props=dict(props),
                        )
                    except DerivedComputationError as exc:
                        raise RuleExecutionError(
                            rname, f"派生属性重算失败: {exc}") from exc
                    created_by_type[ot_id] = inst
                else:
                    candidate = SimpleNamespace(
                        id=instance_id,
                        object_type_id=ot_id,
                        properties=props,
                        computed={},
                        ontology_release_id=instance_release_id,
                    )
                    try:
                        candidate.computed = (
                            preview_derived(candidate, ot)
                        )
                    except DerivedComputationError as exc:
                        raise RuleExecutionError(
                            rname, f"派生属性重算失败: {exc}") from exc
                    _validate_object_candidate(
                        db, ontology_id, instance_release_id,
                        candidate,
                        rname,
                        extra_candidates=dry_run_created_objects,
                        preview_context=definition_context,
                    )
                    dry_run_created_objects.append(candidate)
                    created_by_type[ot_id] = candidate
                effects.append({"type": "create_object", "description": f"创建对象 {ot.display_name}",
                                "targetObjectTypeId": ot_id,
                                "targetInstanceId": instance_id,
                                "newValue": props})

            elif rtype == "update_property":
                prop = cfg.get("targetProperty")
                if not isinstance(prop, str) or not prop:
                    raise RuleExecutionError(rname, "update_property 缺少 targetProperty")
                if not body.dry_run and not target_instance:
                    raise RuleExecutionError(rname, "update_property 需要目标实例（执行时未选择实例）")
                if target_instance:
                    target_type = (
                        _preview_find(
                            definition_context, "object_types",
                            target_instance.object_type_id)
                        if definition_context is not None else
                        db.query(ObjectType).filter(
                            ObjectType.id == target_instance.object_type_id,
                            ObjectType.ontology_id == ontology_id,
                        ).first()
                    )
                    definition = (
                        _definition_properties(target_type).get(prop)
                        if target_type is not None else None
                    )
                    if definition and (
                        definition.get("source") == "computed"
                        or bool(definition.get("computed"))
                    ):
                        raise RuleExecutionError(
                            rname,
                            f"update_property 不能写入派生属性: {prop}",
                        )
                try:
                    val = _resolve_value({"sourceType": cfg.get("valueSource", "constant"),
                                          "sourceValue": cfg.get("value", ""),
                                         "functionId": cfg.get("functionId")},
                                         params, target_props, db, ontology_id,
                                         ontology_release_id=instance_release_id,
                                         preview_context=definition_context)
                except SafeEvalError as e:
                    raise RuleExecutionError(rname, f"取值失败: {e}")
                stored_before = (
                    dict(dry_run_target_properties or {})
                    if body.dry_run and target_instance is not None
                    else (
                        dict(target_instance.properties or {})
                        if target_instance else dict(target_props or {})
                    )
                )
                old = stored_before.get(prop)
                changed = prop not in stored_before or old != val
                np = dict(stored_before)
                np[prop] = val
                if not body.dry_run and target_instance:
                    input_facts: list = []
                    derived_count = 0
                    if changed:
                        target_instance.properties = np
                        _validate_object_write(
                            db, ontology_id, instance_release_id,
                            target_instance.id, rname,
                            definition_context=definition_context)
                        try:
                            input_facts, derived_count = (
                                record_and_recompute(
                                    target_instance,
                                    old_props=(
                                        {prop: old}
                                        if prop in stored_before else {}),
                                    new_props={prop: val},
                                )
                            )
                        except DerivedComputationError as exc:
                            raise RuleExecutionError(
                                rname,
                                f"派生属性重算失败: {exc}") from exc
                    # A semantic no-op must not invalidate an unbound/manual
                    # computed projection or emit CDC without fact lineage.
                    target_props = {
                        **dict(target_instance.properties or {}),
                        **dict(target_instance.computed or {}),
                    }
                elif target_instance:
                    if changed:
                        candidate = SimpleNamespace(
                            id=target_instance.id,
                            object_type_id=target_instance.object_type_id,
                            properties=np,
                            computed=dict(dry_run_target_computed or {}),
                            ontology_release_id=(
                                target_instance.ontology_release_id),
                        )
                        try:
                            candidate.computed = (
                                preview_derived(candidate, target_type)
                            )
                        except DerivedComputationError as exc:
                            raise RuleExecutionError(
                                rname,
                                f"派生属性重算失败: {exc}") from exc
                        _validate_object_candidate(
                            db, ontology_id, instance_release_id,
                            candidate,
                            rname,
                            preview_context=definition_context,
                        )
                        dry_run_target_properties = dict(
                            candidate.properties or {})
                        dry_run_target_computed = dict(
                            candidate.computed or {})
                    target_props = {
                        **dict(dry_run_target_properties or {}),
                        **dict(dry_run_target_computed or {}),
                    }
                effects.append({"type": "update_property", "description": f"更新属性 {prop}",
                                "property": prop, "oldValue": old, "newValue": val,
                                "changed": changed,
                                **({
                                    "inputFactIds": [
                                        fact.id for fact in input_facts],
                                    "derivedFactCount": derived_count,
                                } if not body.dry_run and target_instance
                                   else {})})

            elif rtype == "create_link":
                lt_id = cfg.get("linkTypeId")
                lt = (
                    _preview_find(
                        definition_context, "link_types", lt_id)
                    if definition_context is not None else
                    db.query(LinkType).filter(
                        LinkType.id == lt_id,
                        LinkType.ontology_id == ontology_id).first()
                )
                if not lt:
                    raise RuleExecutionError(rname, f"链接类型不存在: {lt_id}")
                if target_instance and target_instance.object_type_id != lt.source_object_type_id:
                    raise RuleExecutionError(
                        rname,
                        f"源实例类型不符合链接定义: {target_instance.object_type_id} != "
                        f"{lt.source_object_type_id}")
                if not target_instance:
                    raise RuleExecutionError(rname, "create_link 需要源实例")
                tsrc = cfg.get("targetSource", "parameter")
                tval = cfg.get("targetValue", "")
                tgt: Optional[str] = None
                if tsrc == "created_object":
                    # targetValue = objectTypeId，取本次执行创建的该类型实例（与前端引擎一致）
                    inst = created_by_type.get(tval)
                    tgt = inst.id if inst else None
                    if tval not in declared_created_types:
                        raise RuleExecutionError(rname, f"created_object 引用失败：本次执行未创建类型 {tval} 的对象")
                    if tval != lt.target_object_type_id:
                        raise RuleExecutionError(
                            rname,
                            f"created_object 类型不符合链接目标定义: {tval} != "
                            f"{lt.target_object_type_id}")
                elif tsrc == "source":
                    tgt = target_instance.id if target_instance else None
                else:
                    try:
                        v = _resolve_value({"sourceType": tsrc,
                                            "sourceValue": tval,
                                            "functionId": cfg.get("functionId")},
                                           params, target_props, db, ontology_id,
                                           ontology_release_id=instance_release_id,
                                           preview_context=definition_context)
                    except SafeEvalError as e:
                        raise RuleExecutionError(rname, f"目标解析失败: {e}")
                    tgt = str(v) if v not in (None, "") else None
                if not tgt:
                    raise RuleExecutionError(rname, f"create_link 无法解析目标对象: {tval}")
                target_object = (
                    created_by_type.get(tval)
                    if tsrc == "created_object" and body.dry_run
                    else None
                )
                if target_object is None:
                    if (
                        preview_context is not None
                        and preview_context.get("isolated", False)
                    ):
                        target_object = _preview_find(
                            preview_context, "objects", tgt)
                    else:
                        target_object_query = db.query(ObjectInstance).filter(
                            ObjectInstance.id == str(tgt),
                            ObjectInstance.ontology_id == ontology_id)
                        if instance_release_id is not None:
                            target_object_query = target_object_query.filter(
                                ObjectInstance.ontology_release_id
                                == instance_release_id)
                        target_object = target_object_query.first()
                    if not target_object:
                        raise RuleExecutionError(rname, f"链接目标实例不存在: {tgt}")
                    if target_object.object_type_id != lt.target_object_type_id:
                        raise RuleExecutionError(
                            rname,
                            f"目标实例类型不符合链接定义: {target_object.object_type_id} != "
                            f"{lt.target_object_type_id}")
                if not body.dry_run:
                    li = LinkInstance(id=str(uuid.uuid4()),
                                      ontology_id=ontology_id,
                                      ontology_release_id=instance_release_id,
                                      link_type_id=lt_id,
                                      source_object_id=target_instance.id, target_object_id=str(tgt))
                    db.add(li)
                    _validate_link_write(
                        db, ontology_id, instance_release_id, li.id, rname,
                        definition_context=definition_context)
                    pending_links.append({"link_id": li.id, "link_type_id": lt_id, "exists": True})
                else:
                    candidate_link = SimpleNamespace(
                        id=f"dry-run:{uuid.uuid4()}",
                        link_type_id=lt_id,
                        source_object_id=target_instance.id,
                        target_object_id=str(tgt),
                        properties={},
                    )
                    _validate_link_candidate(
                        db, ontology_id, instance_release_id,
                        candidate_link,
                        rname,
                        extra_instances=dry_run_created_objects,
                        existing_candidates=dry_run_links,
                        excluded_link_ids=dry_run_deleted_link_ids,
                        preview_context=definition_context,
                    )
                    dry_run_links.append(candidate_link)
                effects.append({"type": "create_link", "description": f"创建链接 {lt_id}",
                                "linkTypeId": lt_id, "newValue": tgt})

            elif rtype == "delete_link":
                lt_id = cfg.get("linkTypeId")
                lt = (
                    _preview_find(
                        definition_context, "link_types", lt_id)
                    if definition_context is not None else
                    db.query(LinkType).filter(
                        LinkType.id == lt_id,
                        LinkType.ontology_id == ontology_id).first()
                )
                if not lt:
                    raise RuleExecutionError(rname, f"链接类型不存在: {lt_id}")
                if target_instance and target_instance.object_type_id != lt.source_object_type_id:
                    raise RuleExecutionError(
                        rname,
                        f"源实例类型不符合链接定义: {target_instance.object_type_id} != "
                        f"{lt.source_object_type_id}")
                if not target_instance:
                    raise RuleExecutionError(rname, "delete_link 需要源实例")
                if (
                    preview_context is not None
                    and preview_context.get("isolated", False)
                ):
                    candidates = sorted(
                        _preview_iter(
                            preview_context, "links", link_type_id=lt_id,
                            source_object_id=target_instance.id),
                        key=lambda item: str(item.id))
                else:
                    q = db.query(LinkInstance).filter(
                        LinkInstance.ontology_id == ontology_id,
                        LinkInstance.link_type_id == lt_id,
                        LinkInstance.source_object_id == target_instance.id)
                    if instance_release_id is not None:
                        q = q.filter(
                            LinkInstance.ontology_release_id
                            == instance_release_id)
                    candidates = q.order_by(LinkInstance.id.asc()).all()
                if body.dry_run:
                    candidates = [
                        item for item in candidates
                        if str(item.id) not in dry_run_deleted_link_ids
                    ]
                    candidates.extend([
                        item for item in dry_run_links
                        if item.link_type_id == lt_id
                        and item.source_object_id == target_instance.id
                    ])
                    candidates = sorted(
                        candidates, key=lambda item: str(item.id))
                condition = str(cfg.get("condition") or "").strip()
                rows: list = []
                for li in candidates:
                    if not condition:
                        rows.append(li)
                        continue
                    related_target = next((
                        item for item in dry_run_created_objects
                        if item.id == li.target_object_id
                    ), None)
                    if related_target is None:
                        if (
                            preview_context is not None
                            and preview_context.get("isolated", False)
                        ):
                            related_target = _preview_find(
                                preview_context, "objects", li.target_object_id)
                        else:
                            target_query = db.query(ObjectInstance).filter(
                                ObjectInstance.id == li.target_object_id,
                                ObjectInstance.ontology_id == ontology_id,
                            )
                            if instance_release_id is not None:
                                target_query = target_query.filter(
                                    ObjectInstance.ontology_release_id
                                    == instance_release_id)
                            related_target = target_query.first()
                    if related_target is None:
                        raise RuleExecutionError(
                            rname,
                            f"删除条件无法读取链接目标实例: {li.target_object_id}")
                    condition_scopes = {
                        "object": target_props or {},
                        "source": target_props or {},
                        "target": related_target.properties or {},
                        "link": li.properties or {},
                        "params": params,
                    }
                    try:
                        _validate_expression_property_references(
                            condition, condition_scopes)
                        matched = bool(safe_eval(
                            condition, condition_scopes))
                    except SafeEvalError as exc:
                        raise RuleExecutionError(
                            rname, f"删除条件求值失败: {exc}") from exc
                    if matched:
                        rows.append(li)
                if not body.dry_run:
                    for li in rows:
                        pending_links.append({"link_id": li.id, "link_type_id": lt_id, "exists": False})
                        db.delete(li)
                    effects.append({"type": "delete_link",
                                    "description": f"删除链接 {lt_id} × {len(rows)}",
                                    "linkTypeId": lt_id, "oldValue": len(rows),
                                    "matchedLinkIds": [item.id for item in rows],
                                    "conditionApplied": bool(condition)})
                else:
                    matched_ids = {str(item.id) for item in rows}
                    virtual_ids = {
                        str(item.id) for item in dry_run_links}
                    dry_run_links[:] = [
                        item for item in dry_run_links
                        if str(item.id) not in matched_ids
                    ]
                    dry_run_deleted_link_ids.update(
                        matched_ids - virtual_ids)
                    effects.append({"type": "delete_link",
                                    "description": (
                                        f"删除链接 {lt_id} × {len(rows)}（模拟）"),
                                    "linkTypeId": lt_id,
                                    "oldValue": len(rows),
                                    "matchedLinkIds": [item.id for item in rows],
                                    "conditionApplied": bool(condition)})

            elif rtype == "notification":
                effects.append(_execute_internal_notification(
                    db,
                    ontology_id,
                    body,
                    action=action,
                    config=cfg,
                    rule_name=rname,
                    parameters=params,
                    target_properties=target_props,
                    target_instance=target_instance,
                    preview_context=preview_context,
                    dry_run_links=dry_run_links,
                    dry_run_created_objects=dry_run_created_objects,
                    dry_run_deleted_link_ids=dry_run_deleted_link_ids,
                    ontology_release_id=ontology_release_id,
                    sentinel_id=notification_sentinel_id,
                    execution_log_id=execution_log_id,
                ))

            elif rtype == "webhook":
                # External effects run only after local writes, facts and
                # derived recomputation have flushed successfully.  They still
                # run before commit so a webhook failure rolls local work back.
                deferred_webhooks.append(
                    (rule_ordinal, rule, rname, cfg))

            else:
                raise RuleExecutionError(rname or "unknown", f"不支持的动作规则类型: {rtype}")

    except RuleExecutionError as e:
        if not preview_only:
            db.rollback()
        return _fail_log(db, ontology_id, action, body, start, str(e),
                        effects=effects, actor_id=actor_id,
                        ontology_version=ontology_version,
                        ontology_release_id=ontology_release_id,
                        target_snapshot=target_snapshot,
                        suppress_log=preview_only)
    except Exception as e:  # noqa: BLE001 — 任何意外都必须留下失败日志而非 500
        if not preview_only:
            db.rollback()
        logger.exception(
            "动作规则执行出现未封装异常: ontology=%s action=%s",
            ontology_id, body.action_id)
        return _fail_log(db, ontology_id, action, body, start,
                         "动作执行出现内部错误，请检查服务端日志",
                         effects=effects, actor_id=actor_id,
                         validation_errors=["action_execution_internal_error"],
                         ontology_version=ontology_version,
                         ontology_release_id=ontology_release_id,
                         target_snapshot=target_snapshot,
                         suppress_log=preview_only)
    return ExecutedActionEffects(
        execution_log_id=execution_log_id,
        effects=effects,
        pending_links=pending_links,
        deferred_webhooks=deferred_webhooks,
        target_props=target_props,
        source=src,
        causal_fact_id=causal,
    )


def _finalize_action_execution(
        db: Session,
        ontology_id: str,
        body,
        *,
        actor_id: Optional[str],
        preview_context: Optional[dict],
        resolution: ActionDefinitionResolution,
        prepared: PreparedActionExecution,
        executed: ExecutedActionEffects,
) -> dict[str, Any]:
    start = resolution.start
    preview_only = resolution.preview_only
    action = resolution.action
    ontology_version = resolution.ontology_version
    ontology_release_id = resolution.ontology_release_id
    target_snapshot = prepared.target_snapshot
    params = prepared.params
    idem_key = prepared.idempotency_key
    target_instance = prepared.target_instance
    execution_log_id = executed.execution_log_id
    effects = executed.effects
    pending_links = executed.pending_links
    deferred_webhooks = executed.deferred_webhooks
    target_props = executed.target_props
    src = executed.source
    causal = executed.causal_fact_id

    if preview_only:
        try:
            for _rule_ordinal, _rule, rname, cfg in deferred_webhooks:
                try:
                    preview = preview_webhook(
                        cfg, params=params,
                        object_props=target_props or {})
                except WebhookDispatchError as exc:
                    raise RuleExecutionError(
                        rname, str(exc)) from exc
                effects.append({
                    "type": "webhook",
                    "description": "调用 Webhook（模拟，未发出网络请求）",
                    **preview,
                })
            release_error = _current_release_error(db, resolution, preview_context)
            if release_error:
                raise RuleExecutionError(
                    "release_fence", release_error)
        except RuleExecutionError as exc:
            return _fail_log(
                db, ontology_id, action, body, start, str(exc),
                effects=effects, actor_id=actor_id,
                validation_errors=(
                    ["release_context_changed"]
                    if "发布节点" in str(exc) else []),
                ontology_version=ontology_version,
                ontology_release_id=ontology_release_id,
                target_snapshot=target_snapshot,
                suppress_log=True,
            )
        preview_effects = [
            {
                **dict(effect),
                "status": effect.get("status") or "preview",
                "committed": False,
            }
            for effect in effects
        ]
        return {
            "id": None,
            "actionId": action.id,
            "actionName": action.display_name,
            "objectTypeId": action.object_type_id,
            "objectInstanceId": body.target_instance_id,
            "parameters": params,
            "status": "success",
            "validationErrors": [],
            "effects": preview_effects,
            "errorMessage": None,
            "durationMs": int((time.time() - start) * 1000),
            "dryRun": True,
            "executedAt": _now().isoformat(),
            "actorId": actor_id,
            "targetSnapshot": target_snapshot,
            "idempotencyKey": None,
            "sentinelMatchStateId": _match_state_id(body),
            "ontologyVersion": ontology_version,
            "ontologyReleaseId": ontology_release_id,
            "previewOnly": True,
            "sideEffects": "none",
        }

    release_error = _current_release_error(db, resolution, preview_context)
    if release_error:
        return _fail_log(
            db, ontology_id, action, body, start, release_error,
            effects=effects,
            validation_errors=["release_context_changed"],
            actor_id=actor_id, parameters=params,
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            target_snapshot=target_snapshot,
        )

    try:
        log = ActionExecutionLog(
            id=execution_log_id,
            ontology_id=ontology_id, action_id=action.id,
            action_name=action.display_name,
            object_type_id=action.object_type_id,
            object_instance_id=body.target_instance_id,
            parameters=params, status="success", validation_errors=[],
            effects=list(effects),
            duration_ms=int((time.time() - start) * 1000),
            dry_run=body.dry_run, actor_id=actor_id,
            idempotency_key=idem_key,
            sentinel_match_state_id=_match_state_id(body),
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            target_snapshot=target_snapshot,
        )
        db.add(log)
        db.flush()

        # Property and derived facts were written at each ordered mutation so
        # intermediate consumers and history cannot diverge.  Link facts do
        # not affect computed-property scope and can be appended together here
        # after the success-log identity is materialized.
        if pending_links:
            for pl in pending_links:
                record_link_fact(
                    db, ontology_id=ontology_id,
                    link_instance_id=pl["link_id"],
                    link_type_id=pl["link_type_id"],
                    exists=pl["exists"], source=src,
                    actor_id=actor_id, caused_by=causal,
                    ontology_version=ontology_version,
                    ontology_release_id=ontology_release_id,
                )
        db.flush()

        # ``target_props`` is a detached execution snapshot.  Stored values may
        # have changed during the rule loop.  Reload the exact target row after
        # all local effects so every deferred external effect resolves
        # templates from the committed-to-be stored+computed view.
        if not body.dry_run and target_instance is not None:
            db.refresh(
                target_instance,
                attribute_names=["properties", "computed"],
            )
            target_props = {
                **dict(target_instance.properties or {}),
                **dict(target_instance.computed or {}),
            }

        for rule_ordinal, rule, rname, cfg in deferred_webhooks:
            if body.dry_run:
                try:
                    preview = preview_webhook(
                        cfg, params=params,
                        object_props=target_props or {})
                except WebhookDispatchError as exc:
                    raise RuleExecutionError(
                        rname, str(exc)) from exc
                effects.append({
                    "type": "webhook",
                    "description": "调用 Webhook（模拟，未发出网络请求）",
                    **preview,
                })
                continue
            try:
                delivery = dispatch_webhook(
                    cfg,
                    params=params,
                    object_props=target_props or {},
                    idempotency_key=(
                        f"formal-action:{ontology_id}:{action.id}:"
                        f"{idem_key}:{_rule_identity(rule, rule_ordinal)}"
                        if idem_key else None
                    ),
                )
            except WebhookDispatchError as exc:
                if exc.idempotency_key:
                    effects.append({
                        "type": "webhook",
                        "url": exc.safe_url,
                        "method": exc.method,
                        "attempts": exc.attempts,
                        "idempotencyKey": exc.idempotency_key,
                        "status": (
                            "delivery_uncertain"
                            if exc.delivery_uncertain else "failed"),
                        "externalDeliveryMayHaveOccurred": bool(
                            exc.delivery_uncertain),
                        "description": (
                            "Webhook 投递结果不确定，需按幂等键对账"
                            if exc.delivery_uncertain
                            else "Webhook 在发出请求前失败"),
                    })
                raise RuleExecutionError(rname, str(exc)) from exc
            effects.append({
                "type": "webhook",
                "description": (
                    f"Webhook 已调用 → {delivery['method']} "
                    f"{delivery['url']} (HTTP {delivery['statusCode']}，"
                    f"{delivery['attempts']} 次尝试)"),
                **delivery,
            })

        log.effects = list(effects)
        db.commit()
        db.refresh(log)
    except IntegrityError:
        # Another worker won the same key.  This transaction (including object,
        # link, fact and notification effects) is rolled back before replaying
        # the durable winner, so no duplicate side effect survives.
        db.rollback()
        replay = _idempotent_replay(db, ontology_id, idem_key)
        if replay is not None:
            return replay
        return _fail_log(
            db, ontology_id, action, body, start,
            "动作提交违反数据库完整性约束",
            effects=effects, actor_id=actor_id,
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            target_snapshot=target_snapshot,
        )
    except RuleExecutionError as exc:
        db.rollback()
        return _fail_log(
            db, ontology_id, action, body, start, str(exc),
            effects=effects, actor_id=actor_id,
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            target_snapshot=target_snapshot,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "动作事实/派生/提交出现未封装异常: ontology=%s action=%s",
            ontology_id, body.action_id)
        return _fail_log(
            db, ontology_id, action, body, start,
            "动作提交出现内部错误，请检查服务端日志",
            effects=effects, actor_id=actor_id,
            validation_errors=["action_commit_internal_error"],
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            target_snapshot=target_snapshot,
        )
    return _log_to_dict(log)
