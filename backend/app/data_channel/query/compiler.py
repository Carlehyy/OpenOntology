"""Metadata-bound, parameterized SQLAlchemy compilation (never raw SQL input)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from .contracts import ColumnSpec, QueryError, Relation
from .parser import Command, Expr, Pipeline, Ref, Value

_NUMERIC = {"integer", "decimal", "float"}
_ORDERABLE = _NUMERIC | {"string", "date", "timestamp", "time"}


@dataclass
class Compiled:
    statement: sa.Select
    columns: list[ColumnSpec]
    before_limit: sa.Select | None
    params: dict[str, Any]


def refs_in(value):
    if isinstance(value, Ref):
        yield value
    elif isinstance(value, (Expr, Command)):
        yield from refs_in(value.args)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from refs_in(item)


def _typed_value(spec: ColumnSpec, raw: Any) -> Any:
    if raw is None:
        raise QueryError(422, "invalid_parameter_type", "Use IS NULL, not a NULL comparison")
    try:
        if spec.type == "string" and isinstance(raw, str):
            if "\x00" in raw: raise ValueError()
            raw.encode("utf-8", errors="strict")
            return raw
        if spec.type == "boolean" and type(raw) is bool: return raw
        if spec.type == "integer":
            if type(raw) is int:
                value = raw
            elif isinstance(raw, str) and re.fullmatch(r"-?(0|[1-9][0-9]*)", raw):
                value = int(raw)
            else:
                raise ValueError()
            lower, upper = (0, 2**64 - 1) if spec.unsigned else (-2**63, 2**63 - 1)
            if lower <= value <= upper: return value
        if spec.type == "decimal" and not isinstance(raw, (float, bool)):
            value = Decimal(raw)
            if value.is_finite():
                _, digits, exponent = value.as_tuple()
                # Extra trailing zeros are representational, not rounding.
                while exponent < 0 and digits and digits[-1] == 0:
                    digits, exponent = digits[:-1], exponent + 1
                scale = max(-exponent, 0)
                integer_digits = max(len(digits) + exponent, 0) if value else 0
                if spec.scale is not None and scale > spec.scale:
                    raise ValueError()
                if spec.precision is not None and integer_digits > spec.precision - (spec.scale or 0):
                    raise ValueError()
                return value
        if spec.type == "float" and type(raw) in {int, float, Decimal}:
            value = float(raw)
            if math.isfinite(value): return value
        if spec.type == "date" and isinstance(raw, str): return date.fromisoformat(raw)
        if spec.type in {"timestamp", "time"} and isinstance(raw, str):
            # fromisoformat truncates sub-microseconds; only discarded zeroes are exact.
            if any(any(digit != "0" for digit in fraction[6:])
                   for fraction in re.findall(r"[.,]([0-9]+)", raw)):
                raise ValueError()
            value = (datetime if spec.type == "timestamp" else time).fromisoformat(raw.replace("Z", "+00:00"))
            # Fractional offsets are not portable; Python even loses +00:00:00.1.
            offset = re.search(r"[+-][0-9:]+[.,]([0-9]+)$", raw) if value.tzinfo else None
            if offset and any(digit != "0" for digit in offset.group(1)):
                raise ValueError()
            if spec.timezone != (value.tzinfo is not None):
                raise ValueError("timestamp timezone mismatch")
            return value
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        pass
    raise QueryError(422, "invalid_parameter_type",
                     f"Value for {spec.name!r} must have declared type {spec.type}")


def compile_query(pipeline: Pipeline, relations: dict[str, Relation],
                  params: dict[str, Any], *, dialect: str = "postgresql") -> Compiled:
    expressions: dict[Ref, Any] = {}
    specs: dict[Ref, ColumnSpec] = {}
    for alias, relation in relations.items():
        for name, spec in relation.columns.items():
            ref = Ref(alias, name)
            specs[ref] = spec
            expressions[ref] = (relation.expressions[name] if name in relation.expressions else relation.table.c[name])

    required = set(refs_in(pipeline.commands))
    # Without projection/aggregation the language explicitly means all columns.
    if not any(c.op in {"fields", "stats"} for c in pipeline.commands):
        required.update(specs)
    for ref in required:
        if ref not in specs:
            raise QueryError(422, "unknown_field", f"Unknown field {ref.alias}.{ref.name}")
    # Stable labels use indexes, not user identifiers or concatenations.
    labels = {ref: f"qf_{i}" for i, ref in enumerate(specs) if ref in required}
    used_params: set[str] = set()
    bound_params: dict[str, Any] = {}
    binds = 0

    def comparable(expr, spec):
        if spec.type == "string":
            if dialect == "mysql": return sa.cast(expr, mysql.BINARY())
            return sa.cast(expr, sa.Text()).collate("C" if dialect == "postgresql" else "BINARY")
        return expr

    def lookup(ref: Ref):
        if ref not in expressions:
            raise QueryError(422, "unknown_field", f"Field {ref.alias}.{ref.name} is not visible here")
        return expressions[ref], specs[ref]

    def value_for(value: Value, spec: ColumnSpec):
        nonlocal binds
        raw = value.value
        if value.parameter:
            if raw not in params:
                raise QueryError(422, "missing_parameter", f"Missing parameter :{raw}")
            used_params.add(raw)
            raw = params[raw]
        parsed = _typed_value(spec, raw)
        name = f"qv_{binds}"
        binds += 1
        bound_params[name] = parsed
        return sa.bindparam(name, value=parsed, type_=spec.sql_type())

    def predicate(expr: Expr):
        if expr.op in {"and", "or"}:
            children = [predicate(child) for child in expr.args]
            return (sa.and_ if expr.op == "and" else sa.or_)(*children)
        if expr.op == "not": return sa.not_(predicate(expr.args[0]))
        ref = expr.args[0]
        col, spec = lookup(ref)
        if expr.op == "is_null": return col.is_(None)
        if expr.op == "is_not_null": return col.is_not(None)
        if spec.type in {"json", "binary"}:
            raise QueryError(422, "unsupported_comparison", "JSON/binary support only NULL predicates")
        col = comparable(col, spec)
        if expr.op == "in":
            if len(expr.args) > 1001:
                raise QueryError(422, "query_too_complex", "IN supports at most 1000 values")
            return col.in_([value_for(v, spec) for v in expr.args[1:]])
        val = value_for(expr.args[1], spec)
        if expr.op in {">", ">=", "<", "<="} and spec.type not in _ORDERABLE:
            raise QueryError(422, "unsupported_comparison", f"Cannot order {spec.type}")
        return {"=": lambda: col == val, "!=": lambda: col != val,
                "<>": lambda: col != val, ">": lambda: col > val,
                ">=": lambda: col >= val, "<": lambda: col < val,
                "<=": lambda: col <= val}[expr.op]()

    visible_aliases = {pipeline.alias}
    initial = relations[pipeline.alias]
    active = [ref for ref in labels if ref.alias == pipeline.alias]
    if not active:
        # COUNT(*) still needs a FROM, but not business columns.
        stmt = sa.select(sa.literal(1).label("q_dummy")).select_from(initial.table)
    else:
        stmt = sa.select(*(expressions[ref].label(labels[ref]) for ref in active)).select_from(initial.table)
    expressions = {ref: expressions[ref] for ref in active}
    finalized = False
    sorted_ = False
    limited = False
    output_columns: list[ColumnSpec] = []
    outputs: dict[str, Any] = {}
    projected_refs: dict[Ref, str] = {}
    before_limit = None

    def wrap():
        nonlocal stmt, expressions
        sub = stmt.subquery()
        expressions = {ref: sub.c[labels[ref]] for ref in expressions}
        stmt = sa.select(*(col.label(labels[ref]) for ref, col in expressions.items())).select_from(sub)
        if not expressions:
            stmt = sa.select(sa.literal(1).label("q_dummy")).select_from(sub)
        return sub

    def finalize_default():
        nonlocal stmt, finalized, output_columns, outputs
        sub = wrap()
        names = []
        chosen = []
        for ref, col in expressions.items():
            name = f"{ref.alias}.{ref.name}"
            names.append(name)
            if len(name.encode("utf-8")) > 63:
                raise QueryError(422, "invalid_output_name", "Alias long source columns with FIELDS")
            chosen.append(col.label(name))
            projected_refs[ref] = name
            output_columns.append(replace(specs[ref], name=name))
        if not chosen:
            raise QueryError(422, "empty_projection", "An output projection is required")
        stmt = sa.select(*chosen).select_from(sub)
        outputs = dict(zip(names, chosen))
        finalized = True

    for command in pipeline.commands:
        op, args = command.op, command.args
        if limited:
            raise QueryError(422, "invalid_operator_order", "LIMIT must be the final operator")
        if op in {"where", "join"} and (finalized or sorted_):
            raise QueryError(422, "invalid_operator_order", "WHERE/JOIN must precede FIELDS/STATS/SORT")
        if op == "where":
            # Pin current left-side rows before a later LEFT JOIN; predicates
            # after the JOIN remain post-join predicates (SQL three-valued logic).
            wrap()
            stmt = stmt.where(predicate(args[0]))
        elif op == "join":
            _, alias, left, right, kind = args
            if left.alias == alias and right.alias != alias: left, right = right, left
            if left.alias not in visible_aliases or right.alias != alias:
                raise QueryError(422, "invalid_join", "JOIN must connect a visible alias to the new alias")
            if left not in specs or right not in specs:
                raise QueryError(422, "unknown_field", "Unknown JOIN key")
            ls, rs = specs[left], specs[right]
            if ls.type != rs.type or ls.type not in _ORDERABLE | {"boolean"} or ls.timezone != rs.timezone:
                raise QueryError(422, "join_type_mismatch", "JOIN keys must have the same scalar declared type")
            sub = wrap()
            relation = relations[alias]
            right_expr = {ref: (relation.expressions[ref.name] if ref.name in relation.expressions else relation.table.c[ref.name])
                          for ref in labels if ref.alias == alias}
            condition = comparable(expressions[left], ls) == comparable(right_expr[right], rs)
            joined = sub.join(relation.table, condition, isouter=kind == "left")
            expressions.update(right_expr)
            if kind == "left":
                for ref in right_expr: specs[ref] = replace(specs[ref], nullable=True)
            stmt = sa.select(*(col.label(labels[ref]) for ref, col in expressions.items())).select_from(joined)
            visible_aliases.add(alias)
        elif op in {"fields", "stats"}:
            if finalized or sorted_:
                raise QueryError(422, "invalid_operator_order", "Choose one FIELDS or STATS before SORT")
            sub = wrap()
            chosen = []
            if op == "fields":
                for ref, name in args:
                    col, spec = lookup(ref)
                    chosen.append(col.label(name))
                    projected_refs[ref] = name
                    output_columns.append(replace(spec, name=name))
            else:
                groups = next((arg[1] for arg in args if arg[0] == "group"), ())
                for ref in groups:
                    col, spec = lookup(ref)
                    if spec.type in {"json", "binary"}:
                        raise QueryError(422, "unsupported_group_type", "Cannot group JSON/binary")
                    chosen.append(comparable(col, spec).label(ref.name))
                    projected_refs[ref] = ref.name
                    output_columns.append(replace(spec, name=ref.name))
                for item in args:
                    if item[0] == "group": continue
                    fn, ref, name = item
                    if fn == "count" and ref is None:
                        aggregate = sa.func.count()
                        spec = ColumnSpec(name, "integer", False)
                    else:
                        if ref is None:
                            raise QueryError(422, "invalid_aggregate", f"{fn} requires a field")
                        col, source_spec = lookup(ref)
                        if fn in {"sum", "avg"} and source_spec.type not in _NUMERIC:
                            raise QueryError(422, "invalid_aggregate_type", f"{fn} requires a numeric field")
                        if fn in {"min", "max"} and source_spec.type not in _ORDERABLE:
                            raise QueryError(422, "invalid_aggregate_type", f"{fn} requires an orderable field")
                        if fn == "count_distinct":
                            if source_spec.type in {"json", "binary"}:
                                raise QueryError(422, "invalid_aggregate_type", "COUNT DISTINCT requires a scalar field")
                            aggregate = sa.func.count(sa.distinct(comparable(col, source_spec)))
                        else:
                            aggregate = getattr(sa.func, fn)(comparable(col, source_spec) if fn in {"min", "max"} else col)
                        if fn.startswith("count"):
                            spec = ColumnSpec(name, "integer", False)
                        elif fn in {"sum", "avg"} and source_spec.type in {"integer", "decimal"}:
                            spec = ColumnSpec(name, "decimal", True)
                            # DBAPI PostgreSQL/MySQL preserve native NUMERIC.
                            # type_coerce changes only result decoding: CAST AS
                            # DECIMAL without precision would truncate on MySQL.
                            aggregate = sa.type_coerce(aggregate, sa.Numeric())
                        else:
                            spec = replace(source_spec, name=name, nullable=True)
                    chosen.append(aggregate.label(name))
                    output_columns.append(spec)
            names = [spec.name for spec in output_columns]
            if len(names) != len(set(names)):
                raise QueryError(422, "duplicate_output", "Output names must be unique")
            if any(not name or len(name.encode("utf-8")) > 63 or "\x00" in name for name in names):
                raise QueryError(422, "invalid_output_name", "Output names must occupy 1..63 UTF-8 bytes; alias long physical names")
            stmt = sa.select(*chosen).select_from(sub)
            if op == "stats" and groups:
                stmt = stmt.group_by(*(comparable(lookup(ref)[0], specs[ref]) for ref in groups))
            outputs = dict(zip(names, chosen))
            finalized = True
        elif op == "sort":
            if sorted_:
                raise QueryError(422, "invalid_operator_order", "Only one SORT is supported")
            if not finalized: finalize_default()
            for ref, direction, nulls in args:
                if isinstance(ref, Ref):
                    if ref not in projected_refs:
                        raise QueryError(422, "unknown_output", f"Unknown sort field {ref.alias}.{ref.name}")
                    name = projected_refs[ref]
                else:
                    name = ref
                if name not in outputs:
                    raise QueryError(422, "unknown_output", f"Unknown sort output {name!r}")
                col = outputs[name]
                spec = next(c for c in output_columns if c.name == name)
                if spec.type not in _ORDERABLE | {"boolean"}:
                    raise QueryError(422, "unsupported_sort_type", "Cannot sort this output type")
                col = comparable(col, spec)
                # CASE is portable to MySQL 8, which lacks NULLS FIRST/LAST.
                stmt = stmt.order_by(sa.case((col.is_(None), 0 if nulls == "first" else 1),
                                             else_=1 if nulls == "first" else 0),
                                     col.desc() if direction == "desc" else col.asc())
            sorted_ = True
        elif op == "limit":
            if not finalized: finalize_default()
            before_limit = stmt
            if args[0] > 9223372036854775807 or (len(args) > 1 and args[1] > 9223372036854775807):
                raise QueryError(422, "invalid_limit", "LIMIT/OFFSET exceeds signed 64-bit range")
            stmt = stmt.limit(args[0]).offset(args[1] if len(args) > 1 else 0)
            limited = True
    if not finalized: finalize_default()
    unused = set(params) - used_params
    if unused:
        raise QueryError(422, "unused_parameter", f"Unused parameter(s): {', '.join(sorted(unused))}")
    # DBAPI JSON decoders commonly collapse SQL NULL and JSON literal null.
    # Cast only the final projection to text, after relational predicates and
    # aggregates have operated on the native JSON values. Do not change the
    # platform-wide engine's JSON codec or JSON-valued SQL expressions.
    if any(spec.type == "json" for spec in output_columns):
        stmt = stmt.with_only_columns(*(
            sa.cast(column, sa.Text()).label(spec.name) if spec.type == "json" else column
            for column, spec in zip(stmt.selected_columns, output_columns)
        ), maintain_column_froms=True)
    return Compiled(stmt, output_columns, before_limit, bound_params)
