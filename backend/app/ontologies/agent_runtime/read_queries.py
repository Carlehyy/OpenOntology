"""Read-only SQL expressions and lossless scalar encoding for ontology tools.

Only declared property names enter expressions. Values remain bound parameters;
there is no SQL text supplied by a caller. PostgreSQL owns complete-set work.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Any

from sqlalchemy import Numeric, Text, and_, case, cast, func, not_, or_

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.formal_modeling.models import ObjectInstance

NUMBER_TYPES = {"number", "integer", "int", "bigint", "float", "double", "decimal", "numeric"}
INTEGER_TYPES = {"integer", "int", "bigint"}
BOOLEAN_TYPES = {"boolean", "bool"}
# Finite decimal notation; excludes NaN/Infinity, arrays, objects and booleans.
_NUMBER_PATTERN = r"^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$"
JS_SAFE_INTEGER = 2**53 - 1


def decimal_number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def decimal_add(left: Decimal, right: Decimal) -> Decimal:
    """Exact bounded Python accumulation, independent of default precision=28."""
    exponent = min(left.as_tuple().exponent, right.as_tuple().exponent)
    precision = max(len(number.as_tuple().digits) + number.as_tuple().exponent - exponent
                    for number in (left, right)) + 1
    with localcontext() as context:
        context.prec = max(28, precision)
        return left + right


def temp_limit_kb(raw: str) -> int | None:
    """Parse PostgreSQL temp_file_limit display units; unlimited is not bounded."""
    match = re.fullmatch(r"(\d+)\s*(B|kB|MB|GB|TB)?", str(raw).strip(), re.IGNORECASE)
    if match is None:
        return None
    units = {"b": Decimal(1) / 1024, "kb": 1, "mb": 1024,
             "gb": 1024**2, "tb": 1024**3}
    value = Decimal(match[1]) * units[(match[2] or "kb").lower()]
    return int(value.to_integral_value(rounding="ROUND_CEILING"))


def encode_scalar(value: Any) -> Any:
    """JSON-safe without silently turning NUMERIC or large integers into float."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ToolError("invalid_numeric_result: 查询返回非有限数值")
        if value == value.to_integral_value() and abs(value) <= JS_SAFE_INTEGER:
            return int(value)
        return str(value)
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > JS_SAFE_INTEGER:
        return str(value)
    if isinstance(value, dict):
        return {key: encode_scalar(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_scalar(child) for child in value]
    return value


def raw_property(definition: dict, name: str):
    column = (ObjectInstance.computed if definition.get("source") == "computed"
              or definition.get("computed") else ObjectInstance.properties)
    return column[name]


def typed_property(definition: dict, name: str, *, postgres: bool = True):
    raw = raw_property(definition, name)
    declared = str(definition.get("type") or "string").lower()
    if declared in NUMBER_TYPES:
        text_value = raw.as_string()
        if postgres:
            # A CASE protects the cast even when a projection contains malformed
            # legacy numeric values. They are NULL, never float or a sampled row.
            text_value = cast(text_value, Text)
            return case((text_value.op("~")(_NUMBER_PATTERN),
                         cast(text_value, Numeric())), else_=None)
        return cast(text_value, Numeric())
    if declared in BOOLEAN_TYPES:
        return raw.as_boolean()
    if declared in {"json", "object", "array"}:
        return raw.as_json()
    return raw.as_string()


def invalid_typed_property(definition: dict, name: str):
    """PostgreSQL predicate for non-null values outside the declared scalar type.

    The read port validates the complete authorized release before exposing its
    typed relation. A CASE used to protect CAST is not permission to silently
    turn malformed values into NULL in an exact query.
    """
    declared = str(definition.get("type") or "string").lower()
    text_value = cast(raw_property(definition, name).as_string(), Text)
    if declared in NUMBER_TYPES:
        invalid = not_(text_value.op("~")(_NUMBER_PATTERN))
        if declared in INTEGER_TYPES:
            numeric = typed_property(definition, name)
            invalid = or_(invalid, numeric != func.trunc(numeric))
    elif declared in BOOLEAN_TYPES:
        invalid = func.json_typeof(raw_property(definition, name)) != "boolean"
    else:
        return None
    return and_(text_value.is_not(None), invalid)


def property_definitions(object_type) -> dict[str, dict]:
    return {str(p["name"]): p for p in (object_type.properties or [])
            if isinstance(p, dict) and p.get("name")}


def filter_expression(definitions: dict[str, dict], item: dict):
    """Compile the structured tool's scalar filters against declared fields."""
    name, op, want = item["property"], item.get("op", "eq"), item.get("value")
    definition = definitions[name]
    expr = typed_property(definition, name)
    declared = str(definition.get("type") or "string").lower()

    def value(v):
        if declared in NUMBER_TYPES and v is not None:
            number = decimal_number(v)
            if number is None:
                raise ToolError(f"invalid_typed_value: 属性「{name}」需要有限数值")
            return number
        if declared in BOOLEAN_TYPES and v is not None and not isinstance(v, bool):
            raise ToolError(f"invalid_typed_value: 属性「{name}」需要布尔值")
        return v

    if op in {"eq", "neq"}:
        equal = expr.is_(None) if want is None else expr == value(want)
        # Python's existing neq treats missing/null as unequal to non-null.
        return equal if op == "eq" else (not_(equal) if want is None
                                               else or_(expr.is_(None), not_(equal)))
    if op == "in":
        options = want if isinstance(want, list) else [want]
        nonnull = [value(v) for v in options if v is not None]
        parts = [expr.in_(nonnull)] if nonnull else []
        if None in options:
            parts.append(expr.is_(None))
        return or_(*parts) if parts else expr.in_([])
    if op == "contains":
        if want is None:
            return expr.is_(None) & expr.is_not(None)
        return func.lower(cast(raw_property(definition, name).as_string(), Text)).contains(
            str(want).lower(), autoescape=True)
    if op in {"gt", "gte", "lt", "lte"}:
        if want is None:
            return expr.is_(None) & expr.is_not(None)
        method = {"gt": "__gt__", "gte": "__ge__", "lt": "__lt__", "lte": "__le__"}[op]
        return getattr(expr, method)(value(want))
    raise ToolError(f"不支持的过滤操作符: {op}")
