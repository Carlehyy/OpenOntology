"""Value resolution and pure rule evaluation for formal Actions."""
from __future__ import annotations

import json
import re
from heapq import nsmallest
from itertools import chain
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.ontology_formal import (
    ActionType,
    LinkInstance,
    ObjectInstance,
    OntologyFunction,
)
from app.ontologies.formal_modeling.action_validation import (
    _TPL_RE,
    _definition_id,
    _validate_expression_property_references,
)
from app.ontologies.formal_modeling.derived import DerivedComputationError
from app.ontologies.formal_modeling.safe_eval import SafeEvalError, safe_eval
from app.ontologies.formal_modeling.validation import (
    LEGACY_SYSTEM_PROPERTIES,
    property_value_type_issue,
)
from app.services.formal.function_engine import (
    derived_function_contract_issues,
    evaluate_function_contract,
    execute_function,
)


def _preview_reader(context: Optional[dict]):
    """Return the optional isolated projection reader.

    A reader is deliberately duck-typed so the action runtime does not import
    trial staging models (and therefore cannot accidentally read live Formal
    rows).  Legacy list-backed contexts remain supported for unit tests and
    small previews.
    """
    if not context or not context.get("isolated", False):
        return None
    return context.get("reader") or context.get("projection_reader")


def _preview_iter(context: Optional[dict], key: str, **filters):
    reader = _preview_reader(context)
    if reader is not None:
        iterator = getattr(reader, "iter", None)
        if iterator is not None:
            return iterator(key, **filters)
    values = (context or {}).get(key) or []
    if not isinstance(values, (list, tuple)):
        return iter(())
    def matches(item):
        return all(
            str(getattr(item, field, "")) == str(value)
            for field, value in filters.items() if value is not None
        )
    return (item for item in values if matches(item))


def _preview_values(context: Optional[dict], key: str) -> list:
    """Legacy materializing view; contract validators need complete scope.

    New action lookup/effect paths must use ``_preview_find``/``_preview_iter``
    so an isolated SQL reader is not converted into a giant Python list.
    """
    return list(_preview_iter(context, key))


def _preview_find(context: Optional[dict], key: str, item_id: Any):
    wanted = str(item_id or "")
    reader = _preview_reader(context)
    if reader is not None:
        finder = getattr(reader, "find", None)
        if finder is not None:
            return finder(key, wanted)
    return next((
        item for item in _preview_iter(context, key)
        if _definition_id(item) == wanted
    ), None)


def _preview_instance_values(context: Optional[dict]) -> list:
    return _preview_values(context, "objects")


def _preview_link_values(context: Optional[dict]) -> list:
    return _preview_values(context, "links")


def _execute_action_function(
        fn, db: Session | None, ontology_id: str, *,
        obj_props: Optional[dict], params: dict,
        ontology_release_id: str | None,
        preview_context: Optional[dict] = None,
) -> dict[str, Any]:
    """Execute one function against the same data view as the Action preview.

    Snapshot trials cannot query the mutable Formal projection: doing that
    would combine candidate definitions/objects with the current release.  The
    regular runtime continues through ``execute_function`` while isolated
    previews provide their frozen object set to the shared function contract.
    """
    if (
        preview_context is None
        or not preview_context.get("isolated", False)
    ):
        return execute_function(
            fn, db, ontology_id, obj_props=obj_props, params=params,
            ontology_release_id=ontology_release_id)

    target_type_id = str(
        getattr(fn, "target_object_type_id", None) or "")

    def load_scope_objects() -> list[dict]:
        return [
            dict(getattr(item, "properties", None) or {})
            for item in _preview_instance_values(preview_context)
            if not target_type_id
            or str(getattr(item, "object_type_id", None) or "")
            == target_type_id
        ]

    return evaluate_function_contract(
        fn,
        obj_props=obj_props,
        params=params,
        object_loader=load_scope_objects,
    )


def _evaluate_context_derived_projection(
        db: Session | None, ontology_id: str, instance, object_type,
        definition_context: dict,
        ontology_release_id: str | None) -> dict:
    """Evaluate computed properties from one immutable definition context."""
    computed_props = [
        prop for prop in (getattr(object_type, "properties", None) or [])
        if isinstance(prop, dict)
        and (
            prop.get("source") == "computed"
            or bool(prop.get("computed"))
        )
    ]
    computed_names = {
        str(prop.get("name"))
        for prop in computed_props if prop.get("name")
    }
    stored_names = {
        str(prop.get("name"))
        for prop in (getattr(object_type, "properties", None) or [])
        if isinstance(prop, dict)
        and prop.get("name")
        and not (
            prop.get("source") == "computed"
            or bool(prop.get("computed"))
        )
    } | set(LEGACY_SYSTEM_PROPERTIES)
    projected = dict(getattr(instance, "computed", None) or {})
    type_label = str(
        getattr(object_type, "display_name", None)
        or getattr(object_type, "name", None)
        or getattr(object_type, "id", None)
        or "(未命名对象类型)"
    )
    for prop in computed_props:
        name = str(prop.get("name") or "")
        label = str(
            prop.get("displayName")
            or prop.get("display_name")
            or name
            or "(未命名派生属性)"
        )
        if not name:
            raise DerivedComputationError(
                f"对象类型「{type_label}」存在未命名的派生属性，无法安全重算")
        function_id = str(prop.get("functionId") or "").strip()
        if not function_id:
            projected.pop(name, None)
            continue
        fn = _preview_find(
            definition_context, "functions", function_id)
        if fn is None:
            raise DerivedComputationError(
                f"派生属性「{label}」引用的函数不存在: {function_id}")
        if not bool(getattr(fn, "enabled", True)):
            raise DerivedComputationError(
                f"派生属性「{label}」引用的函数已禁用")
        issues = derived_function_contract_issues(
            fn,
            object_type_id=str(getattr(object_type, "id", "") or ""),
            computed_property_names=computed_names,
            stored_property_names=stored_names,
            expected_return_type=str(prop.get("type") or ""),
        )
        if issues:
            raise DerivedComputationError(
                f"派生属性「{label}」引用的函数契约无效: "
                + "；".join(message for _, message in issues))
        if str(getattr(fn, "language", "") or "").strip().lower() \
                != "expression":
            projected.pop(name, None)
            continue
        result = _execute_action_function(
            fn, db, ontology_id,
            obj_props=dict(getattr(instance, "properties", None) or {}),
            params={},
            ontology_release_id=ontology_release_id,
            preview_context=definition_context,
        )
        if not result.get("success"):
            raise DerivedComputationError(
                f"派生属性「{label}」重算失败: "
                f"{result.get('error') or '未知错误'}")
        value = result.get("result")
        type_issue = property_value_type_issue(prop, value)
        if type_issue is not None:
            raise DerivedComputationError(
                f"派生属性「{label}」重算结果类型不匹配："
                f"期望 {type_issue.get('expected') or prop.get('type')}，"
                f"实际为 {type_issue.get('actual') or type(value).__name__}")
        projected[name] = value
    return projected


def _resolve_value(mapping: dict, params: dict, source_props: Optional[dict],
                   db: Session, ontology_id: str,
                   ontology_release_id: str | None = None,
                   preview_context: Optional[dict] = None) -> Any:
    """解析取值来源。expression 无 functionId 时直接对表达式求值
    （作用域 object=目标属性, params=参数），求值失败抛 SafeEvalError 让上层可见。"""
    st = mapping.get("sourceType")
    sv = mapping.get("sourceValue", "")
    if st == "parameter":
        if sv not in params:
            raise SafeEvalError(f"参数不存在或未提供: {sv}")
        return params[sv]
    if st in ("source_property", "property"):
        if sv not in (source_props or {}):
            raise SafeEvalError(f"目标属性不存在: {sv}")
        return (source_props or {})[sv]
    if st == "constant":
        try:
            return json.loads(sv)
        except (json.JSONDecodeError, TypeError):
            return sv
    if st in ("expression", "function"):
        fid = mapping.get("functionId")
        if fid:
            fn = (
                _preview_find(preview_context, "functions", fid)
                if preview_context is not None else
                db.query(OntologyFunction).filter(
                    OntologyFunction.id == fid,
                    OntologyFunction.ontology_id == ontology_id,
                ).first()
            )
            if fn:
                r = _execute_action_function(
                    fn, db, ontology_id, obj_props=source_props,
                    params=params,
                    ontology_release_id=ontology_release_id,
                    preview_context=preview_context)
                if not r.get("success"):
                    raise SafeEvalError(r.get("error") or "函数执行失败")
                return r.get("result")
            raise SafeEvalError(f"函数不存在: {fid}")
        if st == "expression" and sv:
            scopes = {"params": params, "object": source_props or {}}
            _validate_expression_property_references(sv, scopes)
            return safe_eval(sv, scopes)
        raise SafeEvalError("expression/function 取值来源缺少表达式或 functionId")
    raise SafeEvalError(f"不支持的取值来源: {st}")


def _render_template(tpl: str, params: dict, props: Optional[dict]) -> str:
    """模板替换：同时兼容 {{params.x}} / {param.x} / {{object.x}} / {object.x}
    （前端编辑器教用户写 {{params.x}}，历史模板可能是 {param.x}）。

    Missing values fail closed.  Silently replacing a missing recipient/message
    value with an empty string creates a durable ``delivered`` record for
    content that was never actually complete.
    """
    def _sub(m: re.Match) -> str:
        ns, key = m.group(1), m.group(2)
        src = params if ns in ("param", "params") else (props or {})
        if key not in (src or {}) or (src or {}).get(key) is None:
            readable = "参数" if ns in ("param", "params") else "对象属性"
            raise SafeEvalError(f"通知模板引用的{readable}不存在或为空: {key}")
        return str((src or {})[key])
    rendered = _TPL_RE.sub(_sub, tpl or "")
    if re.search(r"\{\{[^{}]*\}\}|\{(?:params?|object)\.[^{}]*\}", rendered):
        raise SafeEvalError("通知模板包含无法解析的占位符")
    if "{{" in rendered or "}}" in rendered:
        raise SafeEvalError("通知模板包含不完整的占位符")
    return rendered


def _resolve_recipient(cfg: dict, params: dict, target_props: Optional[dict],
                       target_instance, db: Session, ontology_id: str,
                       *, virtual_links: Optional[list] = None,
                       virtual_objects: Optional[dict[str, Any]] = None,
                       excluded_link_ids: Optional[set[str]] = None,
                       isolated_links=None,
                       isolated_reader=None,
                       ) -> Optional[str]:
    """解析通知收件人。

    Link lookup must be unambiguous.  Picking an arbitrary ``.first()`` row on
    one-to-many data can report a successful delivery to the wrong person.
    """
    src = cfg.get("recipientSource", "constant")
    val = cfg.get("recipient", "")
    if src == "constant":
        return val
    if src == "parameter":
        if val not in params:
            raise SafeEvalError(f"通知收件人参数不存在: {val}")
        return params.get(val)
    if src == "property":
        if val not in (target_props or {}):
            raise SafeEvalError(f"通知收件人属性不存在: {val}")
        return (target_props or {}).get(val)
    if src == "link":
        if not target_instance:
            raise SafeEvalError("沿链接解析通知收件人需要目标实例")
        excluded = excluded_link_ids or set()
        if isolated_reader is not None:
            base = isolated_reader.iter(
                "links", link_type_id=cfg.get("linkTypeId"),
                source_object_id=target_instance.id)
        elif isolated_links is not None:
            base = (item for item in isolated_links
                    if item.link_type_id == cfg.get("linkTypeId")
                    and item.source_object_id == target_instance.id)
        else:
            link_query = db.query(LinkInstance).filter(
                LinkInstance.ontology_id == ontology_id,
                LinkInstance.link_type_id == cfg.get("linkTypeId"),
                LinkInstance.source_object_id == target_instance.id,
            )
            if target_instance.ontology_release_id is not None:
                link_query = link_query.filter(
                    LinkInstance.ontology_release_id
                    == target_instance.ontology_release_id)
            base = link_query.order_by(LinkInstance.id.asc()).yield_per(200)
        virtual = (item for item in (virtual_links or [])
                   if item.link_type_id == cfg.get("linkTypeId")
                   and item.source_object_id == target_instance.id)
        # Ambiguity requires at most two identities, not a graph-sized list.
        # Keep historical deterministic ID ordering including virtual links.
        links = nsmallest(2, chain(
            (item for item in base if str(item.id) not in excluded), virtual,
        ), key=lambda item: str(item.id))
        if not links:
            raise SafeEvalError("通知收件人链接不存在")
        if len(links) > 1:
            raise SafeEvalError(
                "通知收件人链接不唯一；请通过动作参数明确传入本次匹配对象")
        link = links[0]
        related = (virtual_objects or {}).get(
            str(link.target_object_id))
        if related is None and isolated_reader is not None:
            related = isolated_reader.find("objects", str(link.target_object_id))
        if related is None and isolated_links is None and isolated_reader is None:
            related_query = db.query(ObjectInstance).filter(
                ObjectInstance.id == link.target_object_id,
                ObjectInstance.ontology_id == ontology_id,
            )
            if target_instance.ontology_release_id is not None:
                related_query = related_query.filter(
                    ObjectInstance.ontology_release_id
                    == target_instance.ontology_release_id)
            related = related_query.first()
        if not related:
            raise SafeEvalError("通知收件人关联对象不存在")
        prop = cfg.get("recipientProperty", "email")
        related_values = {
            **dict(getattr(related, "properties", None) or {}),
            **dict(getattr(related, "computed", None) or {}),
        }
        if prop not in related_values:
            raise SafeEvalError(f"通知收件人关联对象属性不存在: {prop}")
        return related_values.get(prop)
    raise SafeEvalError(f"不支持的通知收件人来源: {src}")


def _validate(action: ActionType, params: dict, target_props: Optional[dict],
              db: Session, ontology_id: str,
              rules: Optional[list[dict]] = None,
              ontology_release_id: str | None = None,
              preview_context: Optional[dict] = None) -> list[str]:
    errors: list[str] = []

    # 1. 绑定的校验函数。引用损坏必须 fail-closed，不能把安全闸门
    # 当成可选增强静默跳过。
    if action.validation_function_id:
        fn = (
            _preview_find(
                preview_context, "functions",
                action.validation_function_id)
            if preview_context is not None else
            db.query(OntologyFunction).filter(
                OntologyFunction.id == action.validation_function_id,
                OntologyFunction.ontology_id == ontology_id).first()
        )
        if not fn:
            errors.append(f"动作绑定的校验函数不存在: {action.validation_function_id}")
        elif fn.function_type != "action_validation":
            errors.append(f"动作绑定的函数「{fn.display_name}」不是 action_validation 类型")
        else:
            r = _execute_action_function(
                fn, db, ontology_id, obj_props=target_props, params=params,
                ontology_release_id=ontology_release_id,
                preview_context=preview_context)
            if r.get("success"):
                vr = r.get("result") or {}
                if isinstance(vr, dict) and not vr.get("valid", True):
                    errors.extend(vr.get("errors") or ["校验失败"])
            else:
                errors.append(f"校验函数执行错误: {r.get('error')}")

    # 2. validation 类型规则 —— 表达式错误/函数引用损坏均 fail-closed。
    for rule in (rules if rules is not None else (action.rules or [])):
        if rule.get("type") == "validation" and rule.get("enabled", True):
            cfg = rule.get("config", {})
            if cfg.get("functionId"):
                fn = (
                    _preview_find(
                        preview_context, "functions", cfg["functionId"])
                    if preview_context is not None else
                    db.query(OntologyFunction).filter(
                        OntologyFunction.id == cfg["functionId"],
                        OntologyFunction.ontology_id == ontology_id).first()
                )
                if fn and fn.function_type == "action_validation":
                    r = _execute_action_function(
                        fn, db, ontology_id, obj_props=target_props,
                        params=params,
                        ontology_release_id=ontology_release_id,
                        preview_context=preview_context)
                    vr = r.get("result") or {}
                    if r.get("success") and isinstance(vr, dict) and not vr.get("valid", True):
                        errors.extend(vr.get("errors") or [cfg.get("errorMessage", "校验失败")])
                    elif not r.get("success"):
                        errors.append(f"校验规则「{rule.get('name')}」函数执行错误: {r.get('error')}")
                elif fn:
                    errors.append(
                        f"校验规则「{rule.get('name')}」引用的函数不是 action_validation: "
                        f"{cfg['functionId']}")
                else:
                    errors.append(
                        f"校验规则「{rule.get('name')}」引用的函数不存在: {cfg['functionId']}")
            elif cfg.get("condition"):
                try:
                    scopes = {
                        "params": params,
                        "object": target_props or {},
                    }
                    _validate_expression_property_references(
                        cfg["condition"], scopes)
                    ok = safe_eval(cfg["condition"], scopes)
                    if not ok:
                        errors.append(cfg.get("errorMessage") or f"{rule.get('name')} 校验失败")
                except SafeEvalError as e:
                    errors.append(f"校验规则「{rule.get('name')}」表达式错误（已拒绝执行）: {e}")
            else:
                errors.append(
                    f"校验规则「{rule.get('name')}」未配置 functionId 或 condition")
    return errors
