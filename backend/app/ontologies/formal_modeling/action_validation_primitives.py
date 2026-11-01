"""Shared static primitives for formal Action validation.

This leaf module is intentionally independent of the definition validator and
the runtime so compatibility facades can re-export helpers without cycles.
"""
from __future__ import annotations

import ast
import json
import re
from copy import deepcopy
from datetime import date, datetime
from typing import Any

from app.ontologies.formal_modeling.safe_eval import SafeEvalError


_TPL_RE = re.compile(r"\{\{?\s*(params?|object)\.(\w+)\s*\}?\}")
_MISSING = object()

_SUPPORTED_RULE_TYPES = {
    "validation", "create_object", "update_property", "create_link",
    "delete_link", "notification", "webhook", "delete_row",
}


def _validate_expression_property_references(
        expression: str, scopes: dict[str, dict]) -> None:
    """Reject direct ``scope.property`` typos before safe_eval returns None."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        # ``safe_eval`` below owns the canonical syntax error message.
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        scope_name = node.value.id
        if scope_name not in scopes:
            continue
        if node.attr not in scopes[scope_name]:
            raise SafeEvalError(
                f"{scope_name} 引用了不存在的属性: {node.attr}")


def _parameter_default(parameter: dict) -> Any:
    """Return a declared parameter default without conflating it with ``None``."""
    for key in ("defaultValue", "default_value", "default"):
        if key in parameter:
            return deepcopy(parameter[key])
    return _MISSING


def _parameter_options(parameter: dict) -> list[Any] | None:
    raw = parameter.get("enum")
    if raw is None:
        raw = parameter.get("options")
    if raw is None:
        raw = parameter.get("allowedValues", parameter.get("allowed_values"))
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [item.get("value") if isinstance(item, dict) and "value" in item else item
            for item in raw]


def _type_error(name: str, expected: str) -> str:
    return f"参数「{name}」类型错误，期望 {expected}"


def _parameter_type_error(name: str, declared_type: str, value: Any) -> str | None:
    """Validate the action parameter vocabulary used by the graph editor.

    Coercion is deliberately avoided at the execution boundary: a string ``"1"``
    must not silently become the numeric value ``1`` just because a downstream
    expression happens to accept it.
    """
    ptype = (declared_type or "string").strip().lower()
    if value is None:
        return None
    if ptype in ("string", "reference"):
        return None if isinstance(value, str) else _type_error(name, ptype)
    if ptype in ("number", "float", "double"):
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) \
            else _type_error(name, "number")
    if ptype in ("integer", "int"):
        return None if isinstance(value, int) and not isinstance(value, bool) \
            else _type_error(name, "integer")
    if ptype in ("boolean", "bool"):
        return None if isinstance(value, bool) else _type_error(name, "boolean")
    if ptype in ("array", "list", "object_set"):
        return None if isinstance(value, list) else _type_error(name, "array")
    if ptype in ("object", "dict"):
        return None if isinstance(value, dict) else _type_error(name, "object")
    if ptype == "date":
        if not isinstance(value, str):
            return _type_error(name, "ISO date")
        try:
            date.fromisoformat(value)
            return None
        except ValueError:
            return _type_error(name, "ISO date")
    if ptype in ("datetime", "timestamp"):
        if not isinstance(value, str):
            return _type_error(name, "ISO datetime")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return None
        except ValueError:
            return _type_error(name, "ISO datetime")
    if ptype in ("json", "any"):
        try:
            json.dumps(value)
            return None
        except (TypeError, ValueError):
            return _type_error(name, "JSON-compatible value")
    return f"参数「{name}」声明了不支持的类型「{declared_type}」"


def _definition_field(item, *names: str, default=None):
    if isinstance(item, dict):
        for name in names:
            if name in item:
                return item[name]
        return default
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _definition_id(item) -> str:
    return str(_definition_field(item, "id", default="") or "")


def _definition_properties(item) -> dict[str, dict]:
    return {
        str(prop.get("name")): prop
        for prop in (
            _definition_field(item, "properties", default=[]) or [])
        if isinstance(prop, dict) and prop.get("name")
    }


def _static_sample(definition: dict) -> Any:
    default = _parameter_default(definition)
    if default is not _MISSING:
        return default
    value_type = str(definition.get("type") or "string").lower()
    if value_type in ("number", "float", "double", "integer", "int"):
        return 1
    if value_type in ("boolean", "bool"):
        return True
    if value_type in ("array", "list", "object_set"):
        return []
    if value_type in ("object", "dict", "json"):
        return {}
    if value_type == "date":
        return "2026-01-01"
    if value_type in ("datetime", "timestamp"):
        return "2026-01-01T00:00:00+00:00"
    return "sample"
