"""
派生属性自动重算 — "能算的不存，输入一变自动重算"的执行器

实例的存储属性发生变化后（编辑器保存 / 实例 CRUD / Action 执行），
对该实例所属类型的全部 computed 属性重算：
  - expression 函数缺失、禁用或执行失败时抛出明确异常，由上游事务回滚；
    绝不让新存储属性与旧派生值组成一个可被 Sentinel 消费的混合快照
  - 后端无法权威执行的非 expression 函数会使对应投影值失效（删除），
    避免把上一次客户端计算结果误当成当前值
  - 值变化时：更新 fo_object_instances.computed 投影 +
    追加 kind=derived 的 PropertyFact（source=fn:<函数名>，
    derived_from=触发本次重算的输入事实 id 列表）——派生链可回溯
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from .models import ObjectType, ObjectInstance, OntologyFunction, PropertyFact
from .function_engine import (
    derived_function_contract_issues,
    execute_function,
)
from .facts import fact_order_clause, record_property_facts
from .validation import LEGACY_SYSTEM_PROPERTIES, property_value_type_issue
from app.shared.numeric_utils import values_equal


class DerivedComputationError(RuntimeError):
    """The authoritative derived projection could not be refreshed safely."""


def _property_label(prop: dict) -> str:
    return str(
        prop.get("displayName")
        or prop.get("display_name")
        or prop.get("name")
        or "(未命名派生属性)"
    )


def evaluate_instance_derived_projection(
    db: Session,
    *,
    ontology_id: str,
    instance: ObjectInstance,
    object_type: Optional[ObjectType] = None,
) -> dict:
    """Return the authoritative computed projection without writing facts.

    Dry-run action rules use this to preview the same projection without
    mutating data.  Real actions call :func:`recompute_instance_derived`
    immediately after each stored-property fact so ordered consumers and the
    fact chain observe the same value.
    """
    if object_type is None:
        object_type = db.query(ObjectType).filter(
            ObjectType.id == instance.object_type_id,
            ObjectType.ontology_id == ontology_id,
        ).first()
    if not object_type:
        return dict(instance.computed or {})

    computed_props = [
        p for p in (object_type.properties or [])
        if isinstance(p, dict)
        and (p.get("source") == "computed" or p.get("computed"))
    ]
    computed_property_names = {
        str(prop.get("name"))
        for prop in computed_props
        if prop.get("name")
    }
    stored_property_names = {
        str(prop.get("name"))
        for prop in (object_type.properties or [])
        if isinstance(prop, dict)
        and prop.get("name")
        and not (
            prop.get("source") == "computed"
            or bool(prop.get("computed"))
        )
    } | set(LEGACY_SYSTEM_PROPERTIES)
    new_computed = dict(instance.computed or {})

    for prop in computed_props:
        name = prop.get("name")
        if not name:
            raise DerivedComputationError(
                f"对象类型「{object_type.display_name or object_type.name}」"
                "存在未命名的派生属性，无法安全重算")
        function_id = str(prop.get("functionId") or "").strip()
        if not function_id:
            # Historical/client-calculated properties have no authoritative
            # server implementation.  Their old materialized value is unsafe
            # after a stored input changes.
            new_computed.pop(name, None)
            continue

        fn = db.query(OntologyFunction).filter(
            OntologyFunction.id == function_id,
            OntologyFunction.ontology_id == ontology_id,
        ).first()
        if fn is None:
            raise DerivedComputationError(
                f"派生属性「{_property_label(prop)}」引用的函数不存在: "
                f"{function_id}")
        if not fn.enabled:
            raise DerivedComputationError(
                f"派生属性「{_property_label(prop)}」引用的函数"
                f"「{fn.display_name or fn.name}」已禁用")
        contract_issues = derived_function_contract_issues(
            fn,
            object_type_id=str(object_type.id),
            computed_property_names=computed_property_names,
            stored_property_names=stored_property_names,
            expected_return_type=str(prop.get("type") or ""),
        )
        if contract_issues:
            raise DerivedComputationError(
                f"派生属性「{_property_label(prop)}」引用的函数"
                f"「{fn.display_name or fn.name}」契约无效: "
                + "；".join(message for _, message in contract_issues)
            )

        language = str(fn.language or "").strip().lower()
        if language != "expression":
            # A browser-computed value is not authoritative on the server.
            new_computed.pop(name, None)
            continue

        result = execute_function(
            fn, db, ontology_id, obj_props=instance.properties or {},
            ontology_release_id=instance.ontology_release_id)
        if not result.get("success"):
            raise DerivedComputationError(
                f"派生属性「{_property_label(prop)}」通过函数"
                f"「{fn.display_name or fn.name}」重算失败: "
                f"{result.get('error') or '未知错误'}")
        new_value = result.get("result")
        type_issue = property_value_type_issue(prop, new_value)
        if (
            type_issue is not None
            and type_issue["code"] == "invalid_property_type_definition"
        ):
            raise DerivedComputationError(
                f"派生属性「{_property_label(prop)}」的类型定义"
                f"「{prop.get('type')}」非法")
        if type_issue is not None:
            raise DerivedComputationError(
                f"派生属性「{_property_label(prop)}」重算结果类型不匹配："
                f"期望 {type_issue['expected']}，"
                f"实际为 {type_issue['actual']}")
        new_computed[name] = new_value

    return new_computed


def recompute_instance_derived(
    db: Session,
    *,
    ontology_id: str,
    instance: ObjectInstance,
    object_type: Optional[ObjectType] = None,
    trigger_facts: Optional[list[PropertyFact]] = None,
    caused_by: Optional[str] = None,
) -> int:
    """重算单个实例的派生属性。

    返回 expression 派生事实新增数。配置或执行错误会抛
    :class:`DerivedComputationError`，调用方必须让当前业务事务回滚。
    """
    if object_type is None:
        object_type = db.query(ObjectType).filter(
            ObjectType.id == instance.object_type_id,
            ObjectType.ontology_id == ontology_id,
        ).first()
    if not object_type:
        return 0

    computed_props = [
        p for p in (object_type.properties or [])
        if isinstance(p, dict)
        and (p.get("source") == "computed" or p.get("computed"))
    ]
    if not computed_props:
        return 0

    trigger_ids = [f.id for f in (trigger_facts or []) if f.id]
    old_computed = dict(instance.computed or {})
    new_computed = evaluate_instance_derived_projection(
        db,
        ontology_id=ontology_id,
        instance=instance,
        object_type=object_type,
    )
    changed = 0
    projection_dirty = not values_equal(old_computed, new_computed)

    for prop in computed_props:
        name = prop.get("name")
        # The projection evaluator above owns all definition and result
        # validation, including unnamed properties.
        function_id = str(prop.get("functionId") or "").strip()
        if not function_id:
            continue
        fn = db.query(OntologyFunction).filter(
            OntologyFunction.id == function_id,
            OntologyFunction.ontology_id == ontology_id,
        ).first()
        # ``evaluate_instance_derived_projection`` has already established
        # that bound functions exist.  Keep the guard for type checkers and
        # defensive resilience if this code is changed independently later.
        if fn is None or str(fn.language or "").strip().lower() != "expression":
            continue

        new_val = new_computed.get(name)

        # 对比基准取"最新事实的值"而非投影——投影的 computed 可能被客户端
        # 整包保存清掉，用它判断会导致重复事实或 supersedes 断链
        last = (db.query(PropertyFact)
                .filter(PropertyFact.ontology_id == ontology_id,
                        PropertyFact.instance_id == instance.id,
                        PropertyFact.kind == "derived",
                        PropertyFact.property_name == name)
                .order_by(*fact_order_clause())
                .first())
        last_val = (last.value or {}).get("v") if last is not None else None

        if last is not None and values_equal(last_val, new_val):
            continue  # 值没变，不追加重复事实

        record_property_facts(
            db,
            ontology_id=ontology_id,
            instance_id=instance.id,
            object_type_id=instance.object_type_id,
            old_props={name: last_val} if last is not None else None,
            new_props={name: new_val},
            source=f"fn:{fn.name}",
            caused_by=caused_by,
            kind="derived",
            derived_from=trigger_ids or None,
        )
        changed += 1

    if projection_dirty:
        instance.computed = new_computed  # 投影同步更新
    return changed
