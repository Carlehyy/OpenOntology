"""Public compatibility facade for formal Action validation.

Parameter preparation remains here; definition validation is implemented by
rule-specific static handlers in ``action_definition_validation``.
``action_parameter_hints`` projects the parameter contract for model context.
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.api_hub.publication import is_sensitive_name
from app.models.ontology_formal import ActionType
from app.ontologies.formal_modeling.action_definition_validation import (
    _ActionDefinitionValidator,
    _snapshot_rule_safe,
    action_supports_snapshot_execution,
    validate_action_definition,
)
from app.ontologies.formal_modeling.action_validation_primitives import (
    _MISSING,
    _SUPPORTED_RULE_TYPES,
    _TPL_RE,
    _definition_field,
    _definition_id,
    _definition_properties,
    _length_bounds,
    _number_bounds,
    _parameter_default,
    _parameter_options,
    _parameter_type_error,
    _static_sample,
    _type_error,
    _validate_expression_property_references,
)


def prepare_action_parameters(action: ActionType, supplied: dict | None) -> tuple[dict, list[str]]:
    """Apply defaults and enforce the declared action parameter contract.

    This helper is intentionally public to the ontology runtime: sentinels can
    rely on the exact same contract rather than maintaining a weaker validator.
    """
    if supplied is not None and not isinstance(supplied, dict):
        return {}, ["动作 parameters 必须是对象"]
    raw = dict(supplied or {})
    errors: list[str] = []
    definitions: dict[str, dict] = {}
    parameter_definitions = action.parameters or []
    if not isinstance(parameter_definitions, list):
        return {}, ["动作参数定义损坏：parameters 必须是数组"]
    for item in parameter_definitions:
        if not isinstance(item, dict):
            errors.append("动作参数定义损坏：参数项必须是对象")
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            errors.append("动作参数定义损坏：参数缺少 name")
            continue
        if name in definitions:
            errors.append(f"动作参数定义损坏：参数「{name}」重复")
            continue
        definitions[name] = item

    unknown = sorted(str(k) for k in raw.keys() if k not in definitions)
    if unknown:
        errors.append(f"动作收到未声明参数：{', '.join(unknown)}")

    params: dict[str, Any] = {}
    for name, definition in definitions.items():
        value = raw.get(name, _MISSING)
        if value is _MISSING:
            value = _parameter_default(definition)
        if value is _MISSING:
            if definition.get("required"):
                errors.append(f"参数「{definition.get('displayName') or name}」为必填")
            continue
        if value in (None, "") and definition.get("required"):
            errors.append(f"参数「{definition.get('displayName') or name}」为必填")
            continue

        type_error = _parameter_type_error(name, str(definition.get("type") or "string"), value)
        if type_error:
            errors.append(type_error)
            continue

        options = _parameter_options(definition)
        if options is not None:
            if not options:
                errors.append(f"参数「{name}」的 options/enum 定义无效")
                continue
            if value not in options:
                errors.append(f"参数「{name}」不在允许选项中: {options}")
                continue

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum, maximum = _number_bounds(definition)
            try:
                if minimum is not None and value < float(minimum):
                    errors.append(f"参数「{name}」不得小于 {minimum}")
                if maximum is not None and value > float(maximum):
                    errors.append(f"参数「{name}」不得大于 {maximum}")
            except (TypeError, ValueError):
                errors.append(f"参数「{name}」的 min/max 定义无效")

        if isinstance(value, (str, list, dict)):
            min_length, max_length = _length_bounds(definition)
            try:
                if min_length is not None and len(value) < int(min_length):
                    errors.append(f"参数「{name}」长度不得小于 {min_length}")
                if max_length is not None and len(value) > int(max_length):
                    errors.append(f"参数「{name}」长度不得大于 {max_length}")
            except (TypeError, ValueError):
                errors.append(f"参数「{name}」的 minLength/maxLength 定义无效")

        params[name] = value
    return params, errors


# Caps for ``action_parameter_hints``. Values reaching a model must stay
# enumerations and identifiers, never free text or data dumps.
_HINT_MAX_OPTIONS = 20
_HINT_MAX_TEXT_CHARS = 80
# Every number emitted must survive an IEEE-754 double round trip: an integer
# stays within exact range, a float only has to be finite. Beyond that it is
# data, not an enumeration value.
_HINT_MAX_INT = 2 ** 53 - 1
# ``is_sensitive_name`` is the repository's display-redaction rule. These are
# credential names it does not cover, yet a default under one is a credential.
_HINT_CREDENTIAL_NAME = re.compile(r"pass(?:phrase|code)|access[-_]?key", re.IGNORECASE)


def _hint_scalar(value: Any) -> bool:
    """Return whether ``value`` is a JSON scalar small enough for model input."""
    if isinstance(value, str):
        return len(value) <= _HINT_MAX_TEXT_CHARS
    if isinstance(value, int):  # includes bool, which is always small
        return isinstance(value, bool) or abs(value) <= _HINT_MAX_INT
    if value is None:
        return True
    return isinstance(value, float) and math.isfinite(value)


def _hint_number(bound: Any) -> int | float | None:
    """Return the bound as the number ``prepare_action_parameters`` compares with."""
    if bound is None or isinstance(bound, bool):
        return None
    if isinstance(bound, int):
        return bound if abs(bound) <= _HINT_MAX_INT else None
    try:
        number = float(bound)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _hint_length(bound: Any) -> int | None:
    """Return the bound as the length ``prepare_action_parameters`` compares with."""
    if bound is None or isinstance(bound, bool):
        return None
    try:
        length = int(bound)
    except (TypeError, ValueError, OverflowError):
        return None
    return length if abs(length) <= _HINT_MAX_INT else None


def action_parameter_hints(action) -> list[dict]:
    """Project an action's parameter contract into compact hints for model context.

    This is a read-only projection of the contract ``prepare_action_parameters``
    enforces at execution; it grants nothing and decides nothing. It accepts the
    ORM row and the released snapshot namespace alike. Every hint carries
    ``name`` (canonical parameter name), ``type`` (declared type, lowercased)
    and ``required``; every other key appears only when declared:

    ``default``
        A safe scalar default; text must be one of the disclosed options.
        An explicit ``null`` stays ``None`` when it can be disclosed.
    ``defaultOmitted``
        A declared default is withheld: a sensitive name, ``reference`` type,
        free text, structured data, or a scalar outside the caps.
    ``options``
        The complete allowed-value list, scalars only.
    ``optionsOmitted``
        The declared list cannot be disclosed in full, including options for
        ``reference`` parameters. A partial list is never emitted.
    ``min`` / ``max`` / ``minLength`` / ``maxLength``
        Declared numeric and length bounds. A bound that is a boolean,
        non-finite, or an integer beyond exact double range is dropped.
    ``sensitive``
        The name matches the repository's display-redaction rule (plus the
        credential names that rule misses); its default and options are always
        withheld.

    Rules, descriptions, display names and every other configuration key are
    never projected.
    """
    parameter_definitions = _definition_field(action, "parameters", default=[]) or []
    if not isinstance(parameter_definitions, list):
        return []
    hints: list[dict] = []
    seen: set[str] = set()
    for definition in parameter_definitions:
        if not isinstance(definition, dict):
            continue
        name = str(definition.get("name") or "").strip()
        ptype = str(definition.get("type") or "string").strip().lower()
        # Execution keeps the first definition of a duplicated name.
        if not name or name in seen or not (_hint_scalar(name) and _hint_scalar(ptype)):
            continue
        seen.add(name)
        sensitive = bool(
            is_sensitive_name(name) or _HINT_CREDENTIAL_NAME.search(name))
        hint: dict[str, Any] = {
            "name": name, "type": ptype,
            "required": bool(definition.get("required")),
        }
        if sensitive:
            hint["sensitive"] = True

        values_withheld = sensitive or ptype == "reference"
        options = _parameter_options(definition)
        disclose_options = (
            not values_withheld and options is not None
            and 0 < len(options) <= _HINT_MAX_OPTIONS
            and all(_hint_scalar(option) for option in options)
        )
        default = _parameter_default(definition)
        if default is not _MISSING:
            if (values_withheld or not _hint_scalar(default)
                    or (isinstance(default, str) and (not disclose_options or default not in options))):
                hint["defaultOmitted"] = True
            else:
                hint["default"] = default

        if disclose_options:
            hint["options"] = options
        elif options is not None:
            hint["optionsOmitted"] = True

        lower, upper = _number_bounds(definition)
        for key, number in (("min", _hint_number(lower)), ("max", _hint_number(upper))):
            if number is not None:
                hint[key] = number
        lower, upper = _length_bounds(definition)
        for key, limit in (("minLength", _hint_length(lower)),
                           ("maxLength", _hint_length(upper))):
            if limit is not None:
                hint[key] = limit
        hints.append(hint)
    return hints


def _prepare_action_rules(action: ActionType) -> tuple[list[dict], list[str]]:
    raw = action.rules or []
    if not isinstance(raw, list):
        return [], ["动作规则定义损坏：rules 必须是数组"]
    rules: list[dict] = []
    errors: list[str] = []
    identifiers: set[str] = set()
    for index, rule in enumerate(raw):
        if not isinstance(rule, dict):
            errors.append(f"动作规则定义损坏：第 {index + 1} 项必须是对象")
            continue
        if not rule.get("enabled", True):
            continue
        rtype = rule.get("type")
        if rtype not in _SUPPORTED_RULE_TYPES:
            errors.append(f"不支持的动作规则类型: {rtype}")
            continue
        if not isinstance(rule.get("config", {}), dict):
            errors.append(f"规则「{rule.get('name') or rtype}」config 必须是对象")
            continue
        order = rule.get("order", 0)
        if not isinstance(order, (int, float)) or isinstance(order, bool):
            errors.append(f"规则「{rule.get('name') or rtype}」order 必须是数字")
            continue
        identifier = str(rule.get("id") or "").strip()
        if identifier:
            if identifier in identifiers:
                errors.append(f"动作规则定义损坏：规则 id 重复: {identifier}")
                continue
            identifiers.add(identifier)
        rules.append(rule)
    ordered = sorted(rules, key=lambda item: item.get("order", 0))
    first_webhook = next(
        (index for index, item in enumerate(ordered)
         if item.get("type") == "webhook"),
        None,
    )
    if first_webhook is not None and any(
        item.get("type") not in ("validation", "webhook")
        for item in ordered[first_webhook + 1:]
    ):
        errors.append(
            "Webhook 必须排在所有本地副作用规则之后；否则外部成功后本地失败无法回滚")
    first_effect = next(
        (index for index, item in enumerate(ordered)
         if item.get("type") != "validation"),
        None,
    )
    if first_effect is not None and any(
        item.get("type") == "validation"
        for item in ordered[first_effect + 1:]
    ):
        errors.append(
            "validation 必须排在所有副作用规则之前；"
            "运行时校验是动作前置条件，不读取中间变更")
    return rules, errors
