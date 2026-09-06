"""Shared static validation for release and assistant-created Sentinels."""

from __future__ import annotations

import ast
import re
from types import SimpleNamespace
from typing import Any

from app.ontologies.formal_modeling.models import (
    ActionType as FoActionType,
    LinkType as FoLinkType,
    ObjectType as FoObjectType,
)
from app.ontologies.sentinels.cep import contract as cep_contract
from app.ontologies.sentinels.cep import temporal_ops
from app.ontologies.sentinels.evaluator import RESERVED_SENTINEL_ALIASES
from app.ontologies.sentinels.models import Sentinel


_SENTINEL_PARAMETER_TEMPLATE = re.compile(
    r"\{\{\s*(?P<alias>[^.\s{}]+)\.(?P<property>[^{}\s]+)\s*\}\}"
)
_SENTINEL_EVENT_PROPERTIES = frozenset({
    "edge", "matchKey", "occurredAt", "sentinelId", "sentinelName",
})


def _gate_error(
    code: str,
    kind: str,
    message: str,
    *,
    item_id: str = "",
    name: str = "",
    field: str = "",
) -> dict:
    error = {
        "code": code,
        "kind": kind,
        "id": item_id,
        "name": name,
        "message": message,
    }
    if field:
        error["field"] = field
    return error


def _action_has_usable_default(parameter: dict) -> bool:
    for key in ("defaultValue", "default_value", "default"):
        if key in parameter:
            return parameter[key] not in (None, "")
    return False


def _normal_sentinel_source_type(raw: Any) -> str:
    value = str(raw or "string").strip().lower()
    return {
        "float": "number", "double": "number",
        "integer": "number", "int": "number",
        "bool": "boolean",
        "list": "array", "object_set": "array",
        "dict": "object",
        "timestamp": "datetime",
    }.get(value, value)


def _normal_action_parameter_type(raw: Any) -> str:
    value = str(raw or "string").strip().lower()
    return {
        "float": "number", "double": "number",
        "int": "integer",
        "bool": "boolean",
        "list": "array", "object_set": "array",
        "dict": "object",
        "timestamp": "datetime",
    }.get(value, value)


def _sentinel_parameter_types_compatible(
    source_type: str,
    target_type: str,
) -> bool:
    source = _normal_sentinel_source_type(source_type)
    target = _normal_action_parameter_type(target_type)
    if target in {"any", "json"}:
        return True
    if source == target:
        return True
    # Both parameter kinds are represented by immutable string identifiers at
    # the Sentinel boundary.
    if source in {"string", "reference"} and target in {"string", "reference"}:
        return True
    return False


def _sentinel_expression_property_errors(
    expression: Any,
    alias_properties: dict[str, set[str]],
    *,
    sentinel_id: str,
    sentinel_name: str,
    field: str,
) -> list[dict]:
    """Validate direct property references against the immutable release schema."""
    raw = str(expression or "").strip().rstrip(";").strip()
    if not raw:
        return []
    try:
        tree = ast.parse(raw, mode="eval")
    except SyntaxError:
        # validate_safe_expression owns the canonical syntax error.
        return []

    missing: set[str] = set()
    dynamic: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            alias = node.value.id
            if (
                alias in alias_properties
                and node.attr not in alias_properties[alias]
            ):
                missing.add(f"{alias}.{node.attr}")
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            alias = node.value.id
            if alias not in alias_properties:
                continue
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value not in alias_properties[alias]:
                    missing.add(f"{alias}[{key.value!r}]")
            else:
                dynamic.add(alias)

    errors = [
        _gate_error(
            "sentinel_expression_property_not_found",
            "sentinel",
            (
                f"哨兵「{sentinel_name}」表达式引用了发布版本中不存在的属性: "
                f"{reference}"
            ),
            item_id=sentinel_id,
            name=sentinel_name,
            field=field,
        )
        for reference in sorted(missing)
    ]
    errors.extend(
        _gate_error(
            "sentinel_dynamic_property_forbidden",
            "sentinel",
            f"哨兵「{sentinel_name}」表达式不允许通过动态下标访问 {alias} 的属性",
            item_id=sentinel_id,
            name=sentinel_name,
            field=field,
        )
        for alias in sorted(dynamic)
    )
    return errors


def _temporal_expression_errors(
        expression: Any,
        alias_properties: dict[str, set[str]],
        *,
        sentinel_id: str,
        sentinel_name: str,
        field: str,
        allow_temporal: bool,
) -> list[dict]:
    """Validate CEP temporal operators (changed_within/prev) statically.

    时间算子只允许出现在哨兵 condition：filter 在元组解析阶段逐绑定求值，
    没有整批候选元组可供批量预取，运行期必然 fail-closed——因此发布
    门禁必须直接拒绝，而不是留到运行期静默不命中。
    """
    raw = str(expression or "").strip().rstrip(";").strip()
    if not raw or not any(
            f"{name}(" in raw for name in cep_contract.TEMPORAL_FUNCTIONS):
        return []
    refs, shape_errors = temporal_ops.extract_temporal_refs(raw)
    errors: list[dict] = []

    def _gate(code: str, message: str) -> None:
        errors.append(_gate_error(
            code, "sentinel", message,
            item_id=sentinel_id, name=sentinel_name, field=field))

    if not allow_temporal:
        _gate(
            "sentinel_temporal_in_filter_forbidden",
            f"哨兵「{sentinel_name}」的 {field} 不允许使用时间算子"
            "（changed_within/prev 仅支持 condition）")
        return errors
    for message in shape_errors:
        _gate("sentinel_temporal_expression_invalid", message)
    for ref in refs:
        if ref.alias not in alias_properties:
            _gate(
                "sentinel_temporal_alias_not_found",
                f"哨兵「{sentinel_name}」时间算子引用了未声明的 alias: "
                f"{ref.alias}")
        elif ref.key not in alias_properties[ref.alias]:
            _gate(
                "sentinel_temporal_property_not_found",
                f"哨兵「{sentinel_name}」时间算子引用了发布版本中不存在的"
                f"属性: {ref.alias}.{ref.key}")
        if ref.seconds is not None and (
                not temporal_ops.window_seconds_in_range(ref.seconds)):
            _gate(
                "sentinel_temporal_window_out_of_range",
                f"哨兵「{sentinel_name}」changed_within 窗口必须在 "
                f"{cep_contract.TEMPORAL_WINDOW_MIN_SECONDS}~"
                f"{cep_contract.TEMPORAL_WINDOW_MAX_SECONDS} 秒之间: "
                f"{ref.seconds}")
    return errors


def _pattern_definition_errors(
        sentinel, aliases: dict[str, str],
        alias_properties: dict[str, set[str]], links: list[dict],
        *, sentinel_id: str, sentinel_name: str) -> list[dict]:
    """CEP 模式哨兵（trigger_mode='on_pattern'）的深度结构校验。

    形状/边界权威在 ``cep.pattern.normalize_pattern``；此处叠加需要发布
    图谱上下文的校验：stages 与 bindings 镜像一致、stage filter 引用发布
    属性且禁用时间算子（事件时刻单别名求值没有批量预取上下文）、跨对象
    相邻 stage 必须有 links 关联、窗口 ≥ 扫描间隔（否则超时判定形同虚设）、
    聚合属性存在、模式级 condition 可编译。
    """
    from app.ontologies.sentinels.cep import pattern as cep_pattern

    errors: list[dict] = []
    sid, label = sentinel_id, sentinel_name
    mode = str(getattr(sentinel, "trigger_mode", "") or "")
    pattern = getattr(sentinel, "pattern", None)
    is_pattern_mode = mode == cep_contract.PATTERN_TRIGGER_MODE

    def _gate(code: str, message: str, field: str = "pattern") -> None:
        errors.append(_gate_error(
            code, "sentinel", message, item_id=sid, name=label,
            field=field))

    if not is_pattern_mode:
        if pattern is not None:
            _gate(
                "invalid_sentinel_pattern_mode",
                f"哨兵「{label}」pattern 仅在 triggerMode=on_pattern 时允许出现")
        return errors
    definition = cep_pattern.normalize_pattern(pattern)
    if definition is None:
        _gate(
            "invalid_sentinel_pattern",
            f"哨兵「{label}」triggerMode=on_pattern 时必须携带结构合法的 "
            "pattern（stages 1~4、窗口 60s~7d、聚合字段白名单）")
        return errors
    if not (getattr(sentinel, "on_change", False)
            and getattr(sentinel, "on_schedule", False)):
        _gate(
            "invalid_sentinel_pattern_trigger_flags",
            f"哨兵「{label}」模式哨兵必须同时开启 onChange 与 onSchedule",
            field="onChange")
    scan_interval = int(getattr(sentinel, "scan_interval_seconds", 300) or 300)
    stages = definition["stages"]
    stage_by_alias = {stage["alias"]: stage for stage in stages}

    # stages 必须与 bindings 镜像（同别名、同对象类型），保证引擎按对象
    # 类型筛选哨兵的既有路径对模式哨兵同样成立。
    if set(stage_by_alias) != set(aliases):
        _gate(
            "sentinel_pattern_bindings_mismatch",
            f"哨兵「{label}」pattern.stages 的 alias 集合必须与 bindings "
            "完全一致（镜像约束）")
        return errors
    for alias, stage in stage_by_alias.items():
        if alias in aliases and aliases[alias] != stage["objectTypeId"]:
            _gate(
                "sentinel_pattern_bindings_mismatch",
                f"哨兵「{label}」stage「{alias}」的对象类型必须与 bindings "
                "中同名别名一致")
    primary = str(getattr(sentinel, "primary_alias", "") or "")
    if primary and primary not in stage_by_alias:
        _gate(
            "invalid_sentinel_primary_alias",
            f"哨兵「{label}」模式哨兵的 primaryAlias 必须指向某个 stage 别名",
            field="primaryAlias")

    for index, stage in enumerate(stages):
        props = alias_properties.get(stage["alias"], set())
        filter_expr = stage.get("filter")
        if filter_expr:
            try:
                from app.ontologies.formal_modeling.safe_eval import (
                    validate_safe_expression,
                )
                validate_safe_expression(
                    filter_expr, {stage["alias"], "obj"})
            except Exception as exc:
                _gate(
                    "sentinel_pattern_filter_invalid",
                    f"哨兵「{label}」stage「{stage['alias']}」的 filter 无法"
                    f"编译: {exc}", field=f"pattern.stages[{index}].filter")
            errors.extend(_sentinel_expression_property_errors(
                filter_expr,
                {stage["alias"]: props, "obj": props},
                sentinel_id=sid, sentinel_name=label,
                field=f"pattern.stages[{index}].filter"))
            errors.extend(_temporal_expression_errors(
                filter_expr,
                {stage["alias"]: props, "obj": props},
                sentinel_id=sid, sentinel_name=label,
                field=f"pattern.stages[{index}].filter",
                allow_temporal=False))
        if index > 0:
            within = cep_pattern.stage_window(definition, index)
            if within < scan_interval:
                _gate(
                    "sentinel_pattern_window_below_scan",
                    f"哨兵「{label}」stage「{stage['alias']}」的窗口 "
                    f"({within}s) 不得小于扫描间隔 ({scan_interval}s)，"
                    "否则超时判定形同虚设",
                    field=f"pattern.stages[{index}].within")

    aggregate = definition.get("aggregate")
    if aggregate:
        props = alias_properties.get(stages[0]["alias"], set())
        if aggregate["property"] not in props:
            _gate(
                "sentinel_pattern_aggregate_property_not_found",
                f"哨兵「{label}」聚合属性 {aggregate['property']} 在发布"
                "版本中不存在",
                field="pattern.aggregate.property")
        if aggregate["window"] < scan_interval:
            _gate(
                "sentinel_pattern_window_below_scan",
                f"哨兵「{label}」聚合窗口 ({aggregate['window']}s) 不得小于"
                f"扫描间隔 ({scan_interval}s)",
                field="pattern.aggregate.window")

    # 跨对象模式：相邻 stage 必须有 links 关联（同对象类型时走同实例关联）。
    if not definition.get("same_instance", True):
        for previous, stage in zip(stages, stages[1:]):
            connected = any(
                {link.get("from"), link.get("to")}
                == {previous["alias"], stage["alias"]}
                for link in links if isinstance(link, dict)
            )
            if not connected:
                _gate(
                    "sentinel_pattern_link_missing",
                    f"哨兵「{label}」跨对象相邻 stage "
                    f"「{previous['alias']}」→「{stage['alias']}」"
                    "必须声明 links 关联")

    condition = definition.get("condition")
    if condition:
        try:
            from app.ontologies.formal_modeling.safe_eval import (
                validate_safe_expression,
            )
            validate_safe_expression(
                condition,
                set(stage_by_alias) | set(cep_contract.TEMPORAL_FUNCTIONS))
        except Exception as exc:
            _gate(
                "sentinel_pattern_condition_invalid",
                f"哨兵「{label}」模式级 condition 无法编译: {exc}",
                field="pattern.condition")
        errors.extend(_sentinel_expression_property_errors(
            condition, alias_properties,
            sentinel_id=sid, sentinel_name=label,
            field="pattern.condition"))
        errors.extend(_temporal_expression_errors(
            condition, alias_properties,
            sentinel_id=sid, sentinel_name=label,
            field="pattern.condition", allow_temporal=True))
    return errors


def validate_sentinels(
    sentinels: list[Sentinel],
    object_types: list[FoObjectType],
    link_types: list[FoLinkType],
    actions: list[FoActionType],
) -> list[dict]:
    """发布前验证 Sentinel 的所有静态引用和动作参数可供给性。"""
    errors: list[dict] = []
    object_by_id = {item.id: item for item in object_types}
    link_by_id = {item.id: item for item in link_types}
    action_by_id = {item.id: item for item in actions}

    for sentinel in sentinels:
        sid = sentinel.id or ""
        label = sentinel.display_name or sentinel.name or sid
        bindings = sentinel.bindings or []
        if not isinstance(bindings, list) or not bindings:
            errors.append(_gate_error(
                "sentinel_bindings_missing", "sentinel",
                f"哨兵「{label}」至少需要一个对象绑定",
                item_id=sid, name=label, field="bindings"))
            bindings = []

        aliases: dict[str, str] = {}
        for index, binding in enumerate(bindings):
            if not isinstance(binding, dict):
                errors.append(_gate_error(
                    "invalid_sentinel_binding", "sentinel",
                    f"哨兵「{label}」第 {index + 1} 个 binding 必须是对象",
                    item_id=sid, name=label, field=f"bindings[{index}]"))
                continue
            alias = str(binding.get("alias") or "").strip()
            object_type_id = str(binding.get("objectTypeId") or "").strip()
            if not alias:
                errors.append(_gate_error(
                    "sentinel_alias_missing", "sentinel",
                    f"哨兵「{label}」第 {index + 1} 个 binding 缺少 alias",
                    item_id=sid, name=label, field=f"bindings[{index}].alias"))
            elif alias in aliases:
                errors.append(_gate_error(
                    "duplicate_sentinel_alias", "sentinel",
                    f"哨兵「{label}」的 alias \"{alias}\" 重复",
                    item_id=sid, name=label, field=f"bindings[{index}].alias"))
            elif alias in RESERVED_SENTINEL_ALIASES:
                errors.append(_gate_error(
                    "reserved_sentinel_alias", "sentinel",
                    f"哨兵「{label}」的 alias \"{alias}\" 是运行时保留名称",
                    item_id=sid, name=label, field=f"bindings[{index}].alias"))
            else:
                aliases[alias] = object_type_id
            if object_type_id not in object_by_id:
                errors.append(_gate_error(
                    "sentinel_object_type_not_found", "sentinel",
                    f"哨兵「{label}」binding \"{alias or index}\" 引用的对象类型不存在",
                    item_id=sid, name=label,
                    field=f"bindings[{index}].objectTypeId"))
            binding_filter = binding.get("filter")
            if binding_filter:
                try:
                    from app.ontologies.formal_modeling.safe_eval import (
                        validate_safe_expression,
                    )

                    validate_safe_expression(
                        str(binding_filter),
                        {alias, "obj"} if alias else {"obj"},
                    )
                except Exception as exc:
                    errors.append(_gate_error(
                        "invalid_sentinel_binding_filter", "sentinel",
                        f"哨兵「{label}」binding "
                        f"\"{alias or index}\" 的 filter 无法编译: {exc}",
                        item_id=sid, name=label,
                        field=f"bindings[{index}].filter"))
                if alias and alias not in RESERVED_SENTINEL_ALIASES:
                    object_type = object_by_id.get(object_type_id)
                    property_names = {
                        str(item.get("name"))
                        for item in (
                            (object_type.properties or [])
                            if object_type is not None else []
                        )
                        if isinstance(item, dict) and item.get("name")
                    }
                    errors.extend(_sentinel_expression_property_errors(
                        binding_filter,
                        {alias: property_names, "obj": property_names},
                        sentinel_id=sid,
                        sentinel_name=label,
                        field=f"bindings[{index}].filter",
                    ))
                    errors.extend(_temporal_expression_errors(
                        binding_filter,
                        {alias: property_names, "obj": property_names},
                        sentinel_id=sid,
                        sentinel_name=label,
                        field=f"bindings[{index}].filter",
                        allow_temporal=False,
                    ))

        primary_alias = str(sentinel.primary_alias or "").strip()
        if not primary_alias or primary_alias not in aliases:
            errors.append(_gate_error(
                "invalid_sentinel_primary_alias", "sentinel",
                f"哨兵「{label}」的 primaryAlias 必须指向已声明且唯一的 alias",
                item_id=sid, name=label, field="primaryAlias"))
        # 别名 → 发布属性集合：condition 校验与 pattern 深度校验共用，
        # 无条件构建（pattern 哨兵可以没有顶层 condition）。
        alias_properties = {}
        for alias, object_type_id in aliases.items():
            object_type = object_by_id.get(object_type_id)
            alias_properties[alias] = {
                str(item.get("name"))
                for item in (
                    (object_type.properties or [])
                    if object_type is not None else []
                )
                if isinstance(item, dict) and item.get("name")
            }
        if sentinel.condition:
            try:
                from app.ontologies.formal_modeling.safe_eval import (
                    validate_safe_expression,
                )

                validate_safe_expression(
                    str(sentinel.condition),
                    # CEP 时间算子按 per-evaluation scope 注入 condition；
                    # filter 校验（上方）不含它们，发布门禁直接拒绝 filter
                    # 内的时间算子使用。
                    set(aliases) | set(cep_contract.TEMPORAL_FUNCTIONS),
                )
            except Exception as exc:
                errors.append(_gate_error(
                    "invalid_sentinel_condition", "sentinel",
                    f"哨兵「{label}」的 condition 无法编译: {exc}",
                    item_id=sid, name=label, field="condition"))
            errors.extend(_sentinel_expression_property_errors(
                sentinel.condition,
                alias_properties,
                sentinel_id=sid,
                sentinel_name=label,
                field="condition",
            ))
            errors.extend(_temporal_expression_errors(
                sentinel.condition,
                alias_properties,
                sentinel_id=sid,
                sentinel_name=label,
                field="condition",
                allow_temporal=True,
            ))

        links = sentinel.links or []
        if not isinstance(links, list):
            errors.append(_gate_error(
                "invalid_sentinel_links", "sentinel",
                f"哨兵「{label}」的 links 必须是数组",
                item_id=sid, name=label, field="links"))
            links = []
        for index, link in enumerate(links):
            if not isinstance(link, dict):
                errors.append(_gate_error(
                    "invalid_sentinel_link", "sentinel",
                    f"哨兵「{label}」第 {index + 1} 个 link 必须是对象",
                    item_id=sid, name=label, field=f"links[{index}]"))
                continue
            from_alias = str(link.get("from") or "").strip()
            to_alias = str(link.get("to") or "").strip()
            link_type_id = str(link.get("linkTypeId") or "").strip()
            if from_alias not in aliases or to_alias not in aliases:
                errors.append(_gate_error(
                    "sentinel_link_alias_not_found", "sentinel",
                    f"哨兵「{label}」的 link 端点必须引用已声明 alias",
                    item_id=sid, name=label, field=f"links[{index}]"))
            link_type = link_by_id.get(link_type_id)
            if link_type is None:
                errors.append(_gate_error(
                    "sentinel_link_type_not_found", "sentinel",
                    f"哨兵「{label}」引用的关系类型不存在: {link_type_id}",
                    item_id=sid, name=label,
                    field=f"links[{index}].linkTypeId"))
            elif from_alias in aliases and to_alias in aliases and (
                aliases[from_alias] != link_type.source_object_type_id
                or aliases[to_alias] != link_type.target_object_type_id
            ):
                errors.append(_gate_error(
                    "sentinel_link_endpoint_mismatch", "sentinel",
                    f"哨兵「{label}」的 link 端点类型与关系类型方向不匹配",
                    item_id=sid, name=label, field=f"links[{index}]"))

        errors.extend(_pattern_definition_errors(
            sentinel, aliases, alias_properties, links,
            sentinel_id=sid, sentinel_name=label))

        action_ids = sentinel.action_ids or []
        if not isinstance(action_ids, list):
            errors.append(_gate_error(
                "invalid_sentinel_actions", "sentinel",
                f"哨兵「{label}」的 actionIds 必须是数组",
                item_id=sid, name=label, field="actionIds"))
            action_ids = []
        if len(action_ids) != len(set(str(aid) for aid in action_ids)):
            errors.append(_gate_error(
                "duplicate_sentinel_action", "sentinel",
                f"哨兵「{label}」的 actionIds 存在重复",
                item_id=sid, name=label, field="actionIds"))

        all_parameters = sentinel.action_parameters or {}
        if not isinstance(all_parameters, dict):
            errors.append(_gate_error(
                "invalid_sentinel_action_parameters", "sentinel",
                f"哨兵「{label}」的 actionParameters 必须是对象",
                item_id=sid, name=label, field="actionParameters"))
            all_parameters = {}
        declared_action_ids = {str(aid) for aid in action_ids}
        for configured_id in all_parameters:
            if str(configured_id) not in declared_action_ids:
                errors.append(_gate_error(
                    "orphan_sentinel_action_parameters", "sentinel",
                    f"哨兵「{label}」为未声明动作 {configured_id} 配置了参数",
                    item_id=sid, name=label,
                    field=f"actionParameters.{configured_id}"))

        for index, raw_action_id in enumerate(action_ids):
            action_id = str(raw_action_id or "")
            action = action_by_id.get(action_id)
            if action is None:
                errors.append(_gate_error(
                    "sentinel_action_not_found", "sentinel",
                    f"哨兵「{label}」引用的动作不存在: {action_id}",
                    item_id=sid, name=label, field=f"actionIds[{index}]"))
                continue
            if (
                primary_alias in aliases
                and action.object_type_id
                and action.object_type_id != aliases[primary_alias]
            ):
                errors.append(_gate_error(
                    "sentinel_action_target_mismatch", "sentinel",
                    f"哨兵「{label}」的动作目标类型与 primaryAlias 类型不匹配",
                    item_id=sid, name=label, field=f"actionIds[{index}]"))
            if getattr(sentinel, "trigger_mode", None) == "on_enter_leave":
                from app.ontologies.formal_modeling.action_engine import (
                    action_supports_snapshot_execution,
                )

                if not action_supports_snapshot_execution(action):
                    errors.append(_gate_error(
                        "sentinel_leave_action_not_snapshot_safe", "sentinel",
                        f"哨兵「{label}」启用了离开触发，但动作"
                        f"「{action.display_name or action.name}」依赖实时对象或关系，"
                        "目标删除后无法仅凭命中快照执行",
                        item_id=sid, name=label,
                        field=f"actionIds[{index}]"))
            configured = all_parameters.get(action_id, {})
            if not isinstance(configured, dict):
                errors.append(_gate_error(
                    "invalid_sentinel_action_parameters", "sentinel",
                    f"哨兵「{label}」为动作"
                    f"「{action.display_name or action.name}」配置的参数必须是对象",
                    item_id=sid, name=label,
                    field=f"actionParameters.{action_id}"))
                configured = {}
            declared_parameters = {
                str(parameter.get("name") or ""): parameter
                for parameter in (action.parameters or [])
                if isinstance(parameter, dict) and parameter.get("name")
            }
            for parameter_name, spec in configured.items():
                if parameter_name not in declared_parameters:
                    errors.append(_gate_error(
                        "sentinel_action_parameter_unknown", "sentinel",
                        f"哨兵「{label}」为动作"
                        f"「{action.display_name or action.name}」提供了未声明参数 "
                        f"\"{parameter_name}\"",
                        item_id=sid, name=label,
                        field=f"actionParameters.{action_id}.{parameter_name}"))
                    continue
                field = f"actionParameters.{action_id}.{parameter_name}"
                target_parameter = declared_parameters[parameter_name]

                def validate_required_property_supply(
                    property_definition: dict,
                    source_label: str,
                ) -> None:
                    if (
                        not target_parameter.get("required")
                        or _action_has_usable_default(target_parameter)
                        or property_definition.get("required") is True
                    ):
                        return
                    errors.append(_gate_error(
                        "sentinel_required_parameter_optional_property",
                        "sentinel",
                        f"哨兵「{label}」将动作必填参数"
                        f"「{parameter_name}」仅绑定到可选属性"
                        f" {source_label}；真实对象缺字段时动作必然失败",
                        item_id=sid, name=label, field=field))

                def validate_binding_type(
                    source_type: str | None,
                    source_label: str,
                ) -> None:
                    if (
                        not source_type
                        or _sentinel_parameter_types_compatible(
                            source_type,
                            str(target_parameter.get("type") or "string"),
                        )
                    ):
                        return
                    errors.append(_gate_error(
                        "sentinel_parameter_type_mismatch",
                        "sentinel",
                        f"哨兵「{label}」参数「{parameter_name}」绑定的"
                        f"{source_label}类型为 {source_type}，与动作参数类型 "
                        f"{target_parameter.get('type') or 'string'} 不兼容",
                        item_id=sid, name=label, field=field))

                if isinstance(spec, str):
                    if "{{" not in spec and "}}" not in spec:
                        validate_binding_type("string", "字符串常量")
                        continue
                    matches = list(_SENTINEL_PARAMETER_TEMPLATE.finditer(spec))
                    remainder = _SENTINEL_PARAMETER_TEMPLATE.sub("", spec)
                    if not matches or "{{" in remainder or "}}" in remainder:
                        errors.append(_gate_error(
                            "invalid_sentinel_parameter_template", "sentinel",
                            f"哨兵「{label}」参数「{parameter_name}」模板格式非法: "
                            f"{spec}",
                            item_id=sid, name=label, field=field))
                        continue
                    full_match = _SENTINEL_PARAMETER_TEMPLATE.fullmatch(spec)
                    template_source_type = (
                        "string" if full_match is None else None)
                    template_source_label = (
                        "插值模板" if full_match is None else "模板来源")
                    for match in matches:
                        template_alias = match.group("alias")
                        prop = match.group("property")
                        if template_alias in {"event", "edge"}:
                            if full_match is not None:
                                template_source_type = "string"
                                template_source_label = f"事件属性 {prop}"
                            if prop not in _SENTINEL_EVENT_PROPERTIES:
                                errors.append(_gate_error(
                                    "sentinel_event_property_not_found",
                                    "sentinel",
                                    f"哨兵「{label}」参数「{parameter_name}」"
                                    f"引用了不受支持的事件属性: {prop}",
                                    item_id=sid, name=label, field=field))
                            continue
                        resolved_alias = (
                            primary_alias
                            if template_alias in {"primary", "target"}
                            else template_alias
                        )
                        if resolved_alias not in aliases:
                            errors.append(_gate_error(
                                "sentinel_parameter_alias_not_found",
                                "sentinel",
                                f"哨兵「{label}」参数「{parameter_name}」"
                                f"模板引用的 alias 不存在: {template_alias}",
                                item_id=sid, name=label, field=field))
                            continue
                        if prop == "id":
                            if full_match is not None:
                                template_source_type = "string"
                                template_source_label = (
                                    f"实例标识 {resolved_alias}.id")
                            continue
                        object_type = object_by_id.get(aliases[resolved_alias])
                        property_definitions = {
                            str(item.get("name")): item
                            for item in (
                                (object_type.properties or [])
                                if object_type is not None else []
                            )
                            if isinstance(item, dict) and item.get("name")
                        }
                        if prop not in property_definitions:
                            errors.append(_gate_error(
                                "sentinel_parameter_property_not_found",
                                "sentinel",
                                f"哨兵「{label}」参数「{parameter_name}」"
                                "模板引用的发布属性不存在: "
                                f"{resolved_alias}.{prop}",
                                item_id=sid, name=label, field=field))
                        else:
                            property_definition = property_definitions[prop]
                            validate_required_property_supply(
                                property_definition,
                                f"{resolved_alias}.{prop}",
                            )
                            if full_match is not None:
                                template_source_type = str(
                                    property_definition.get("type")
                                    or "string")
                                template_source_label = (
                                    f"属性 {resolved_alias}.{prop}")
                    validate_binding_type(
                        template_source_type,
                        template_source_label,
                    )
                    continue
                if not isinstance(spec, dict):
                    # Scalar/list/object literal; runtime contract validates type.
                    continue
                raw_source = spec.get("sourceType", spec.get("source"))
                if raw_source is None:
                    continue  # plain object literal
                source = str(raw_source).strip().lower().replace("-", "_")
                allowed_sources = {
                    "constant", "literal", "property", "match",
                    "match_property", "target_id", "primary_id",
                    "event", "event_property", "edge",
                }
                if source not in allowed_sources:
                    errors.append(_gate_error(
                        "invalid_sentinel_parameter_source", "sentinel",
                        f"哨兵「{label}」参数「{parameter_name}」"
                        f"的绑定来源 {raw_source!r} 不受支持",
                        item_id=sid, name=label, field=field))
                    continue
                if source in {"constant", "literal"}:
                    if "value" not in spec and "sourceValue" not in spec:
                        errors.append(_gate_error(
                            "sentinel_constant_value_missing", "sentinel",
                            f"哨兵「{label}」参数「{parameter_name}」"
                            "的常量绑定缺少 value",
                            item_id=sid, name=label, field=field))
                    else:
                        from app.ontologies.formal_modeling.action_engine import (
                            prepare_action_parameters,
                        )

                        value = (
                            spec.get("value")
                            if "value" in spec
                            else spec.get("sourceValue")
                        )
                        _, value_errors = prepare_action_parameters(
                            SimpleNamespace(parameters=[
                                declared_parameters[parameter_name]
                            ]),
                            {parameter_name: value},
                        )
                        for value_error in value_errors:
                            errors.append(_gate_error(
                                "sentinel_constant_parameter_invalid",
                                "sentinel",
                                f"哨兵「{label}」常量参数"
                                f"「{parameter_name}」无效: {value_error}",
                                item_id=sid, name=label, field=field))
                    continue
                if source in {"event", "event_property", "edge"}:
                    prop = str(
                        spec.get("property", spec.get("sourceValue"))
                        or ("edge" if source == "edge" else "")
                    ).strip()
                    if not prop:
                        errors.append(_gate_error(
                            "sentinel_event_property_missing", "sentinel",
                            f"哨兵「{label}」参数「{parameter_name}」"
                            "的事件绑定缺少 property",
                            item_id=sid, name=label, field=field))
                    elif prop not in _SENTINEL_EVENT_PROPERTIES:
                        errors.append(_gate_error(
                            "sentinel_event_property_not_found", "sentinel",
                            f"哨兵「{label}」参数「{parameter_name}」"
                            f"引用了不受支持的事件属性: {prop}",
                            item_id=sid, name=label, field=field))
                    else:
                        validate_binding_type("string", f"事件属性 {prop}")
                    continue
                raw_alias = str(
                    spec.get("alias") or primary_alias or "").strip()
                alias = (
                    primary_alias
                    if raw_alias in {"primary", "target"}
                    else raw_alias
                )
                if alias not in aliases:
                    errors.append(_gate_error(
                        "sentinel_parameter_alias_not_found", "sentinel",
                        f"哨兵「{label}」参数「{parameter_name}」"
                        f"引用的 alias 不存在: {raw_alias}",
                        item_id=sid, name=label, field=field))
                    continue
                if source in {"target_id", "primary_id"}:
                    validate_binding_type("string", f"实例标识 {alias}.id")
                    continue
                if source in {"property", "match", "match_property"}:
                    prop = str(
                        spec.get("property", spec.get("sourceValue")) or ""
                    ).strip()
                    if not prop:
                        errors.append(_gate_error(
                            "sentinel_parameter_property_missing", "sentinel",
                            f"哨兵「{label}」参数「{parameter_name}」"
                            "的属性绑定缺少 property",
                            item_id=sid, name=label, field=field))
                    elif prop == "id":
                        validate_binding_type(
                            "string",
                            f"实例标识 {alias}.id",
                        )
                    else:
                        object_type = object_by_id.get(aliases[alias])
                        property_definitions = {
                            str(item.get("name")): item
                            for item in (
                                (object_type.properties or [])
                                if object_type else []
                            )
                            if isinstance(item, dict) and item.get("name")
                        }
                        if prop not in property_definitions:
                            errors.append(_gate_error(
                                "sentinel_parameter_property_not_found",
                                "sentinel",
                                f"哨兵「{label}」参数「{parameter_name}」"
                                f"绑定的属性不存在: {alias}.{prop}",
                                item_id=sid, name=label, field=field))
                        else:
                            property_definition = property_definitions[prop]
                            validate_required_property_supply(
                                property_definition,
                                f"{alias}.{prop}",
                            )
                            validate_binding_type(
                                str(
                                    property_definition.get("type")
                                    or "string"
                                ),
                                f"属性 {alias}.{prop}",
                            )
            for parameter in (action.parameters or []):
                if (
                    not isinstance(parameter, dict)
                    or not parameter.get("required")
                ):
                    continue
                parameter_name = str(parameter.get("name") or "").strip()
                if not parameter_name:
                    continue
                configured_value = configured.get(parameter_name)
                if (
                    _action_has_usable_default(parameter)
                    or (
                        parameter_name in configured
                        and configured_value not in (None, "")
                    )
                ):
                    continue
                errors.append(_gate_error(
                    "sentinel_required_action_parameter_missing",
                    "sentinel",
                    f"哨兵「{label}」未为动作"
                    f"「{action.display_name or action.name}」提供必填参数 "
                    f"\"{parameter_name}\"，且动作未声明默认值",
                    item_id=sid,
                    name=label,
                    field=f"actionParameters.{action_id}.{parameter_name}",
                ))
    return errors
