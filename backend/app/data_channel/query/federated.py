"""Bounded in-memory federated execution.

Memory mode never creates PostgreSQL TEMP tables, never COPYs, and never
stores rows in the database. Same-connection sources (including their joins,
filters and local aggregation inside derived subqueries) are compiled into one
statement per connection component and streamed from the source. Cross-source
equi-joins run in bounded memory: the materialized side is capped by rows and
bytes, the streamed side is filtered by the materialized side's distinct join
keys pushed down as chunked IN lists, and joins/aggregations/sorts preserve
SQL semantics (JOIN multiplicity, NULL/LEFT extension, DISTINCT, exact
decimals). When the streamed side is a derived GROUP BY and the pulled column
is one of its group keys, the chunked IN list is proven-equivalent to a row
predicate and compiled below that GROUP BY (and, for a single LEFT join to a
pure group-key projection, the projection is restricted to the join keys the
surviving rows can reach), so each chunk aggregates only its own key range on
the source instead of re-aggregating the full input per chunk. Where no
bounded plan exists the query fails with an explicit budget error instead of
silently truncating or pulling whole tables.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from typing import Any, Callable, Iterator

import sqlalchemy as sa

from app.data_channel.integrated.contracts import JsonText

from .compiler import _comparable, _typed_value, compile_query
from .contracts import (ColumnSpec, DerivedSourceSpec, JoinConstraint, QueryError,
                        Relation)
from .encoding import _copy_line, encode_value, encoded_size
from .memory_budget import MemoryBudget, resident_size
from .parser import Command, Expr, Pipeline, Ref, params_in, validate_pipeline

_ORDERABLE = {"integer", "decimal", "float", "string", "date", "timestamp", "time"}
_AGGREGATE_ONLY = {"count", "count_distinct", "sum", "avg"}
AVG_SEMANTICS = "decimal_28_significant_digits_half_even"


@dataclass
class MemoryResult:
    columns: list[ColumnSpec]
    rows: list[dict]
    row_bytes: int
    result_limited: bool
    classification: str
    avg_semantics: str | None
    plan: dict
    source_states: list[dict]
    consistency: str


def output_classification(pipeline: Pipeline) -> str:
    """Trusted, structure-derived: any non-aggregated source value is detail."""
    for cmd in pipeline.commands:
        if cmd.op == "fields":
            return "detail"
        if cmd.op == "stats":
            groups = next((arg[1] for arg in cmd.args if arg[0] == "group"), ())
            if groups:
                return "detail"  # group keys are original source values
            if any(item[0] not in _AGGREGATE_ONLY for item in cmd.args if item[0] != "group"):
                return "detail"  # min/max expose source values
            return "aggregate"
    return "detail"  # default projection streams raw source rows


def _iter_refs(value) -> Iterator[Ref]:
    if isinstance(value, Ref):
        yield value
    elif isinstance(value, Expr):
        yield from _iter_refs(value.args)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _iter_refs(item)


def _hashable(value):
    return value


def _less_than(a, b) -> bool:
    try:
        return a < b
    except TypeError:
        return str(a) < str(b)


def _init_state():
    return {"count": 0, "count_value": 0, "distinct": set(), "sum": None, "min": None, "max": None}


def _update_state(state, fn, value, memory_budget):
    state["count"] += 1
    if value is None:
        return
    state["count_value"] += 1
    if fn == "count_distinct":
        state["distinct"].add(_hashable(value))
    if fn in {"sum", "avg"}:
        if isinstance(value, bool):
            raise QueryError(422, "invalid_aggregate_type", "Cannot sum boolean values")
        if isinstance(value, (Decimal, int)):
            addend = value if isinstance(value, Decimal) else Decimal(value)
            total = state["sum"] if state["sum"] is not None else Decimal(0)
            if not addend.is_finite() or not total.is_finite():
                raise QueryError(422, "invalid_typed_value", "Non-finite aggregate input")
            precision = max(total.adjusted(), addend.adjusted()) - min(
                total.as_tuple().exponent, addend.as_tuple().exponent) + 2
            memory_budget.check_bytes(precision + 128)
            with localcontext() as ctx:
                ctx.prec = precision
                state["sum"] = total + addend
        else:
            state["sum"] = (state["sum"] or 0.0) + float(value)
    if fn == "min" and (state["min"] is None or _less_than(value, state["min"])):
        state["min"] = value
    if fn == "max" and (state["max"] is None or _less_than(state["max"], value)):
        state["max"] = value


def _final_state(state, fn, ref_is_none: bool):
    if fn == "count":
        # COUNT(*) counts rows; COUNT(field) counts non-NULL values.
        return state["count"] if ref_is_none else state["count_value"]
    if fn == "count_distinct":
        return len(state["distinct"])
    if fn == "sum":
        return state["sum"]
    if fn == "avg":
        if state["count_value"] == 0:
            return None
        total = state["sum"]
        if isinstance(total, Decimal):
            with localcontext() as context:
                context.prec = 28
                context.rounding = "ROUND_HALF_EVEN"
                return total / Decimal(state["count_value"])
        return total / state["count_value"]
    if fn == "min":
        return state["min"]
    if fn == "max":
        return state["max"]
    raise QueryError(422, "invalid_aggregate", f"Unsupported aggregate {fn!r}")


def _uses_avg(tail) -> bool:
    return any(item[0] == "avg" for cmd in tail if cmd.op == "stats"
               for item in cmd.args if item[0] != "group")


class MemoryExecutor:
    def __init__(self, service, pipeline: Pipeline, relations: dict[str, Relation],
                 params: dict[str, Any], *, cancel_check: Callable[[], bool] | None = None,
                 memory_budget=None, derived_specs: dict[str, Any] | None = None):
        self.memory_budget = memory_budget or MemoryBudget(min(
            service.limits.memory_per_user_bytes_limit,
            service.limits.federated_side_bytes_limit + service.limits.memory_result_bytes_limit))
        self.service = service
        self.limits = service.limits
        self.pipeline = validate_pipeline(pipeline)
        self.relations = relations
        self.params = dict(params)
        self.cancel_check = cancel_check or (lambda: False)
        self.column_specs: dict[tuple[str, str], ColumnSpec] = {}
        for alias, relation in relations.items():
            for name, spec in relation.columns.items():
                self.column_specs[(alias, name)] = spec
        self._pulled: dict[str, list[dict]] = {}
        self._statements: dict[str, tuple[Any, dict, list]] = {}
        self._statement_specs: dict[str, dict[str, ColumnSpec]] = {}
        self._component_of: dict[str, str] = {}
        self._component_members: dict[str, list[str]] = {}
        self._component_commands: dict[str, list[Command]] = {}
        self._entry_kind: dict[str, str] = {}
        self._join_by_alias: dict[str, tuple[Any, Any, str]] = {}
        self._needed_cache: dict[str, set[str]] | None = None
        self.source_rows: dict[str, int] = {}
        self._keyed_pulls: dict[str, dict] = {}
        self._group_key_pushdowns: dict[str, int] = {}
        # Top-level derived specs are retained only as compile metadata for
        # safe group-key pushdown; source values never enter this structure.
        self._derived_specs = derived_specs or {}
        # Outer alias -> source name: derived specs and the service's resolved
        # relations (parents included) are keyed by source name.
        self._alias_sources = {alias: source for source, alias in pipeline.sources}
        self._pushed_statements: dict[tuple[str, str], tuple | None] = {}
        # Track charges for row dictionaries that remain live between source
        # pulls and the final result. Intermediate join rows replace their
        # inputs; releasing those charges keeps chunked execution bounded by
        # the live working set rather than by historical allocations.
        self._charged_rows: dict[int, int] = {}

    # -- shared guards ------------------------------------------------------

    def _check(self):
        self.service._deadline()
        if self.cancel_check():
            raise QueryError(409, "query_cancelled", "Query execution was cancelled")

    def _charge_row(self, row: dict) -> int:
        amount = self.memory_budget.charge(row)
        self._charged_rows[id(row)] = amount
        return amount

    def _release_rows(self, rows) -> None:
        for root, cached in list(self._pulled.items()):
            if cached is rows:
                del self._pulled[root]
        for row in rows:
            amount = self._charged_rows.pop(id(row), 0)
            if amount:
                self.memory_budget.release_bytes(amount)

    def _measure(self, row: dict, keys: list[str], specs: list[ColumnSpec]) -> int:
        _, size = _copy_line([row[key] for key in keys], specs,
                             max(1, self.limits.memory_result_bytes_limit), size_only=True)
        return size

    def _spec(self, ref: Ref) -> ColumnSpec:
        spec = self.column_specs.get((ref.alias, ref.name))
        if spec is None:
            raise QueryError(422, "unknown_field", f"Unknown field {ref.alias}.{ref.name}")
        return spec

    def _spec_for_key(self, key: str) -> ColumnSpec:
        alias, name = key.split(".", 1)
        return self.column_specs[(alias, name)]

    # -- statement execution ------------------------------------------------

    def _execute_statement(self, anchor: Relation, statement, exec_params,
                           label_specs: dict[str, ColumnSpec]):
        """Yield mapping rows; JSON text cells become exact JsonText values."""
        self._check()
        if anchor.batches is not None:
            for batch in anchor.batches(statement, exec_params, min(self.limits.batch_size, 1000)):
                self._check()
                self.memory_budget.check_temporary(batch)
                for row in batch:
                    self.memory_budget.check_temporary(row)
                    yield self._restore(row if isinstance(row, dict) else dict(row), label_specs)
        elif anchor.platform:
            self.service._platform_budget()
            result = self.service.db.execute(statement.execution_options(
                stream_results=True, yield_per=1, max_row_buffer=1), exec_params).mappings()
            try:
                while (row := result.fetchone()) is not None:
                    self._check()
                    yield self._restore(dict(row), label_specs)
            finally:
                result.close()
        else:
            raise QueryError(503, "query_provider_invalid", "External source needs a bounded stream")

    def _restore(self, row: dict, label_specs: dict[str, ColumnSpec]) -> dict:
        for label, spec in label_specs.items():
            value = row.get(label)
            if spec.type == "json" and isinstance(value, str):
                row[label] = JsonText(value)
        return row

    def _execute_relation_one(self, relation: Relation, statement, exec_params) -> dict:
        rows = self._execute_statement(relation, statement, exec_params, {})
        try:
            first = next(rows, None)
            if first is None or next(rows, None) is not None:
                raise QueryError(409, "source_incomplete", "Invalid source aggregate completion")
            # Keep the conservative probe charge used by cardinality checks;
            # it is bounded to this single aggregate row.
            self._charge_row(first)
            return first
        finally:
            rows.close()

    # -- cardinality contracts ----------------------------------------------

    def check_join_constraints(self, constraints: dict[str, JoinConstraint]) -> None:
        if not self._join_by_alias:
            for cmd in self.pipeline.commands:
                if cmd.op == "join":
                    self._join_by_alias.setdefault(cmd.args[1], cmd.args[2:5])
        for alias, constraint in constraints.items():
            entry = self._join_by_alias.get(alias)
            if entry is None:
                raise QueryError(422, "invalid_join_constraint",
                                 "Join constraint must key a joined alias", alias=alias)
            left_ref, right_ref, _kind = entry
            for side, ref in (("left", left_ref), ("right", right_ref)):
                if ref.alias not in self.relations:
                    raise QueryError(422, "invalid_join_constraint", "Unknown join alias", alias=ref.alias)
                spec = self.relations[ref.alias].columns.get(ref.name)
                if spec is None:
                    raise QueryError(422, "unknown_field", f"Unknown JOIN key {ref.alias}.{ref.name}")
                declared = constraint.declared_types()
                if declared is not None and spec.type not in declared:
                    raise QueryError(422, "join_key_type_mismatch",
                                     "Join key type does not match the declared key type",
                                     alias=ref.alias, field=ref.name, declaredType=spec.type)
                unique = (side == "right" and constraint.cardinality in {"1:1", "N:1"}) or \
                         (side == "left" and constraint.cardinality in {"1:1", "1:N"})
                if unique:
                    self._check_unique(ref, spec)

    def _check_unique(self, ref: Ref, spec: ColumnSpec) -> None:
        relation = self.relations[ref.alias]
        col = relation.expressions.get(ref.name)
        if col is None:
            col = relation.table.c[ref.name]
        dialect = relation.state.get("dialect", "postgresql")
        key = _comparable(col, spec, dialect)
        statement = sa.select(sa.func.count().label("rows"),
                              sa.func.count(key).label("non_null"),
                              sa.func.count(sa.distinct(key)).label("distinct")
                              ).select_from(relation.table)
        row = self._execute_relation_one(relation, statement, dict(relation.bind_params or {}))
        non_null, distinct = int(row["non_null"]), int(row["distinct"])
        if non_null != distinct:
            raise QueryError(422, "join_cardinality_violated",
                             "Join key is not unique as required by the declared cardinality",
                             alias=ref.alias, field=ref.name,
                             nonNullValues=non_null, distinctValues=distinct)

    # -- pipeline analysis ----------------------------------------------------

    def _analyze(self):
        joins, wheres, tail = [], [], []
        finalized = False
        distinct_seen = False
        for cmd in self.pipeline.commands:
            op = cmd.op
            if op in {"where", "join"}:
                if finalized:
                    raise QueryError(422, "invalid_operator_order", "WHERE/JOIN must precede FIELDS/STATS/SORT")
                (wheres if op == "where" else joins).append(cmd.args)
            elif op in {"fields", "stats"}:
                if finalized:
                    raise QueryError(422, "invalid_operator_order", "Choose one FIELDS or STATS before SORT")
                tail.append(cmd)
                finalized = True
            elif op == "distinct":
                if not finalized or distinct_seen:
                    raise QueryError(422, "invalid_operator_order",
                                     "DISTINCT must follow FIELDS/STATS and precede SORT/LIMIT")
                tail.append(cmd)
                distinct_seen = True
            elif op == "sort":
                if not finalized:
                    raise QueryError(422, "invalid_operator_order", "SORT requires an output projection")
                if any(c.op == "sort" for c in tail):
                    raise QueryError(422, "invalid_operator_order", "Only one SORT is supported")
                tail.append(cmd)
            elif op == "limit":
                if not finalized:
                    raise QueryError(422, "invalid_operator_order", "LIMIT requires an output projection")
                if any(c.op == "limit" for c in tail):
                    raise QueryError(422, "invalid_operator_order", "LIMIT must be the final operator")
                tail.append(cmd)
            else:
                raise QueryError(422, "invalid_query", f"Unsupported operator {op!r}")
        return joins, wheres, tail

    def _plan_components(self, joins, wheres) -> list[Expr]:
        parent: dict[str, str] = {alias: alias for alias in self.relations}

        def find(alias):
            while parent.get(alias, alias) != alias:
                alias = parent[alias]
            return alias

        self._entry_kind = {self.pipeline.alias: "base"}
        hoisted: set[str] = set()
        for _source, alias, left, right, kind in joins:
            if left.alias == alias and right.alias != alias:
                left, right = right, left
            same = self.relations[alias].connection_key == self.relations[left.alias].connection_key
            self._entry_kind[alias] = ("hoisted" if same else
                                       "cross_inner" if kind == "inner" else "cross_left")
            self._join_by_alias[alias] = (left, right, kind)
            if same:
                hoisted.add(alias)
                left_root, right_root = find(left.alias), find(alias)
                if left_root != right_root:
                    parent[left_root] = right_root

        known = {self.pipeline.alias}
        for _source, alias, left, right, _kind in joins:
            for ref in (left, right):
                if ref.alias != alias and ref.alias not in known:
                    raise QueryError(422, "invalid_join",
                                     "JOIN must connect a visible alias to the new alias")
            known.add(alias)

        order = [alias for _, alias in self.pipeline.sources]
        self._component_of = {alias: find(alias) for alias in order}
        self._component_members = {}
        for alias in order:
            self._component_members.setdefault(self._component_of[alias], []).append(alias)

        visible = {self.pipeline.alias}
        self._component_commands = {root: [] for root in self._component_members}
        in_memory: list[Expr] = []
        for cmd in self.pipeline.commands:
            if cmd.op == "join":
                if cmd.args[1] in hoisted:
                    self._component_commands[self._component_of[cmd.args[1]]].append(cmd)
                visible.add(cmd.args[1])
            elif cmd.op == "where":
                expr = cmd.args[0]
                refs = list(_iter_refs(expr))
                for ref in refs:
                    if ref.alias not in visible:
                        raise QueryError(422, "unknown_field",
                                         f"Field {ref.alias}.{ref.name} is not visible here")
                aliases = {ref.alias for ref in refs}
                roots = {self._component_of[a] for a in aliases}
                # Push only provably order-safe conjuncts: one component and no
                # alias that entered via a cross-source LEFT join.
                if (len(roots) == 1 and
                        all(self._entry_kind.get(a, "base") in {"base", "hoisted", "cross_inner"}
                            for a in aliases)):
                    self._component_commands[roots.pop()].append(cmd)
                else:
                    in_memory.append(expr)
        return in_memory

    def _needed_columns(self, joins, wheres, tail) -> dict[str, set[str]]:
        needed: dict[str, set[str]] = {alias: set() for alias in self.relations}
        projected = any(c.op in {"fields", "stats"} for c in tail)

        def add(ref: Ref):
            if (ref.alias, ref.name) not in self.column_specs:
                raise QueryError(422, "unknown_field", f"Unknown field {ref.alias}.{ref.name}")
            needed[ref.alias].add(ref.name)

        for _source, _alias, left, right, _kind in joins:
            add(left)
            add(right)
        for expr in wheres:
            for ref in _iter_refs(expr):
                add(ref)
        for cmd in tail:
            if cmd.op == "fields":
                for ref, _name in cmd.args:
                    add(ref)
            elif cmd.op == "stats":
                for arg in cmd.args:
                    if arg[0] == "group":
                        for item in arg[1]:
                            add(item if isinstance(item, Ref) else item[0])
                    elif arg[1] is not None:
                        add(arg[1])
            elif cmd.op == "sort":
                for ref, _direction, _nulls in cmd.args:
                    if isinstance(ref, Ref):
                        add(ref)
        if not projected:
            for alias, relation in self.relations.items():
                needed[alias] = set(relation.columns)
        return needed

    # -- component statements ------------------------------------------------

    def _component_statement(self, root: str):
        if root not in self._statements:
            statement, exec_params, mapping = self._build_component_statement(root)
            self._statement_specs[root] = {label: self.column_specs[(alias, name)]
                                           for label, alias, name in mapping}
            self._statements[root] = (statement, exec_params, mapping)
        return self._statements[root]

    def _build_component_statement(self, root: str, base_relation: Relation | None = None):
        needed = self._needed_cache or {}
        members = self._component_members[root]
        base = members[0]
        anchor = base_relation if base_relation is not None else self.relations[base]
        projection, mapping = [], []
        for alias in members:
            for name in sorted(needed.get(alias, ())):
                label = f"g{len(mapping)}"
                projection.append((Ref(alias, name), label))
                mapping.append((label, alias, name))
        if not projection:
            # COUNT(*) style pipelines still need a column to stream.
            name = next(iter(self.relations[base].columns))
            projection.append((Ref(base, name), "g0"))
            mapping.append(("g0", base, name))
        commands = tuple(self._component_commands[root]) + (Command("fields", tuple(projection)),)
        sub_pipeline = Pipeline(base, base, commands)
        used = params_in(commands)
        sub_params = {k: v for k, v in self.params.items() if k in used}
        dialect = "postgresql" if anchor.platform else anchor.state.get("dialect", "postgresql")
        compiled = compile_query(sub_pipeline, {a: (anchor if a == base else self.relations[a])
                                                for a in members},
                                 sub_params, dialect=dialect)
        exec_params = dict(compiled.params)
        for alias in members:
            bind = (anchor if alias == base else self.relations[alias]).bind_params
            if bind:
                exec_params.update(bind)
        return compiled.statement, exec_params, mapping

    def _anchor(self, root: str) -> Relation:
        return self.relations[self._component_members[root][0]]

    def _count_component(self, root: str) -> int:
        statement, exec_params, _mapping = self._component_statement(root)
        count_stmt = sa.select(sa.func.count().label("n")).select_from(
            statement.order_by(None).subquery())
        row = self._execute_relation_one(self._anchor(root), count_stmt, exec_params)
        return int(row["n"])

    def _materialize_component(self, root: str) -> list[dict]:
        if root in self._pulled:
            return self._pulled[root]
        statement, exec_params, mapping = self._component_statement(root)
        label_specs = self._statement_specs[root]
        count = self._count_component(root)
        if count > self.limits.federated_side_rows_limit:
            raise QueryError(422, "query_budget_exceeded",
                             "Materialized federated side exceeds the row budget; pre-aggregate or filter first",
                             budget="federated_side_rows_limit", alias=self._component_members[root][0])
        keys = [f"{alias}.{name}" for _l, alias, name in mapping]
        specs = [self.column_specs[(alias, name)] for _l, alias, name in mapping]
        rows, total_bytes = [], 0
        for raw in self._execute_statement(self._anchor(root), statement, exec_params, label_specs):
            row = {f"{alias}.{name}": raw[label] for label, alias, name in mapping}
            total_bytes += self._measure(row, keys, specs)
            if total_bytes > self.limits.federated_side_bytes_limit:
                raise QueryError(422, "query_budget_exceeded",
                                 "Materialized federated side exceeds the byte budget; pre-aggregate or filter first",
                                 budget="federated_side_bytes_limit", alias=self._component_members[root][0])
            self._charge_row(row)
            rows.append(row)
            if len(rows) > self.limits.federated_join_rows_limit:
                raise QueryError(422, "query_budget_exceeded",
                                 "Materialized federated side exceeds the bounded join row budget",
                                 budget="federated_join_rows_limit")
        self._pulled[root] = rows
        self.source_rows[self._component_members[root][0]] = len(rows)
        return rows

    # -- proven group-key pushdown --------------------------------------------
    #
    # Lemma (used twice below): for an aggregation G = gamma_{k,...};F(R) and a
    # predicate P = "k IN V" with NULL-free V, sigma_P(G(R)) = G(sigma_P(R)).
    # Every row of a group shares the group's k value, so P either keeps or
    # drops the whole group, and the surviving groups see exactly their
    # original rows; each supported aggregate (count, count_distinct, sum,
    # avg, min, max) is computed over that unchanged per-group multiset. The
    # NULL-key group is dropped by both plans because IN never matches NULL.

    @staticmethod
    def _derived_group_shape(spec: DerivedSourceSpec):
        """(stats command, group items) for a derived aggregation pipeline.

        Valid only when the pipeline's row set is exactly one row per
        surviving GROUP BY key tuple: a single STATS with non-empty groups and
        no LIMIT/DISTINCT afterwards (DISTINCT after grouping is rejected as
        unnecessary complexity; LIMIT would make sigma and gamma commute
        invalid, SORT alone cannot change the row set).
        """
        commands = spec.pipeline.commands
        stats_index = next((i for i, c in enumerate(commands) if c.op == "stats"), None)
        if stats_index is None:
            return None
        if any(c.op in {"limit", "distinct"} for c in commands[stats_index:]):
            return None
        stats_cmd = commands[stats_index]
        groups = next((arg[1] for arg in stats_cmd.args if arg[0] == "group"), ())
        if not groups:
            return None
        return stats_cmd, groups

    def _group_projection(self, name: str):
        """(spec, group key Ref, key output name) for a derived spec that is
        exactly a single-key GROUP BY aggregation over one earlier source
        (measures allowed; the key is the only join-relevant output)."""
        inner = self._derived_specs.get(name)
        if not isinstance(inner, DerivedSourceSpec):
            return None
        commands = inner.pipeline.commands
        if len(commands) != 1 or commands[0].op != "stats":
            return None
        groups = next((arg[1] for arg in commands[0].args if arg[0] == "group"), ())
        if len(groups) != 1:
            return None
        item = groups[0]
        ref, out = item if isinstance(item, tuple) else (item, item.name)
        if len(inner.pipeline.sources) != 1:
            return None
        return inner, ref, out

    def _restrict_left_join_projection(self, spec: DerivedSourceSpec, inputs: dict[str, Relation],
                                       group_ref: Ref, key_spec: ColumnSpec,
                                       dialect: str, relations_by_name: dict[str, Relation]):
        """Restrict a derived LEFT join's group-key aggregation to reachable keys.

        Shape required: the derived pipeline joins exactly one further source
        with a LEFT join, that source is itself a single-key GROUP BY
        aggregation, the join compares that key, and the pushed group key
        belongs to the base (preserved) side. The joined aggregation is then
        recompiled with ``key IN (SELECT join_key FROM base WHERE group_key IN
        <chunk keys>)`` below its GROUP BY.

        Equivalence: let K be the chunk key set and S = {base.join_key :
        base.group_key IN K}. For every base row r surviving the pushed group
        predicate (group_key(r) IN K), join_key(r) is in S by construction, so
        r's match status against the aggregation is identical restricted or
        not; base rows outside K are eliminated by the pushed predicate in
        both plans, and LEFT extension reads no other joined column values
        than the key it can reach. By the lemma above each surviving group
        keeps its exact member rows, so measures (and the one-row-per-key
        multiplicity that keeps duplicate source rows from fanning out the
        join) are unchanged.
        """
        joins = [c for c in spec.pipeline.commands if c.op == "join"]
        if len(joins) != 1 or group_ref.alias != spec.pipeline.alias:
            return inputs, False
        source_name, joined_alias, left, right, kind = joins[0].args
        if kind != "left" or left.alias != spec.pipeline.alias or right.alias != joined_alias:
            return inputs, False
        projection = self._group_projection(source_name)
        if projection is None or right.name != projection[2]:
            return inputs, False
        inner, inner_ref, inner_out = projection
        base_rel = inputs.get(spec.pipeline.alias)
        underlying = relations_by_name.get(inner.pipeline.source)
        if base_rel is None or underlying is None:
            return inputs, False
        key_in_underlying = underlying.columns.get(inner_ref.name)
        join_in_base = base_rel.columns.get(left.name)
        group_in_base = base_rel.columns.get(group_ref.name)
        if key_in_underlying is None or join_in_base is None or group_in_base is None:
            return inputs, False
        probe = base_rel.table.alias("gkprobe")
        reachable = sa.select(_comparable(probe.c[left.name], join_in_base, dialect)).where(
            _comparable(probe.c[group_ref.name], group_in_base, dialect).in_(
                sa.bindparam("semi_keys", expanding=True, type_=key_spec.sql_type())))
        inner_relations = {inner.pipeline.alias: underlying}
        inner_compiled = compile_query(inner.pipeline, inner_relations, dict(inner.params),
                                       dialect=dialect, bind_prefix=f"gk{len(self._pushed_statements)}s")
        inner_key = inner_compiled.statement.selected_columns.get(inner_out)
        if inner_key is None:
            return inputs, False
        inner_expr = getattr(inner_key, "element", inner_key)
        restricted = inner_compiled.statement.where(inner_expr.in_(reachable)).subquery(joined_alias)
        return {**inputs, joined_alias: replace(inputs[joined_alias], table=restricted,
                                                bind_params=dict(inner_compiled.params)
                                                if inner_compiled.params else None)}, True

    def _pushed_component(self, root: str, ref: Ref):
        """Keyed-pull statement with the chunk IN compiled below a derived
        GROUP BY, or None when the shape is not proven equivalent. The returned
        statement already carries its expanding ``push_keys`` bind (plus
        ``semi_keys`` when an inner projection was restricted); exec_params
        cover everything else the statement needs."""
        key = (root, ref.name)
        if key not in self._pushed_statements:
            self._pushed_statements[key] = self._build_pushed_component(root, ref)
        return self._pushed_statements[key]

    def _build_pushed_component(self, root: str, ref: Ref):
        members = self._component_members.get(root) or []
        if len(members) != 1:
            return None
        alias = members[0]
        spec = self._derived_specs.get(self._alias_sources.get(alias, ""))
        relations_by_name = getattr(self.service, "relations", None)
        if not isinstance(spec, DerivedSourceSpec) or not isinstance(relations_by_name, dict):
            return None
        shape = self._derived_group_shape(spec)
        if shape is None:
            return None
        _stats_cmd, groups = shape
        items = (item if isinstance(item, tuple) else (item, item.name) for item in groups)
        match = next(((gref, out) for gref, out in items if out == ref.name), None)
        if match is None:
            return None  # the pulled column must be a group key, not a measure
        group_ref, out = match
        key_spec = self.column_specs.get((alias, ref.name))
        if key_spec is None:
            return None
        inputs: dict[str, Relation] = {}
        try:
            for name, member_alias in validate_pipeline(spec.pipeline).sources:
                parent = relations_by_name.get(name)
                if parent is None:
                    return None
                inputs[member_alias] = parent
            anchor = inputs.get(spec.pipeline.alias)
            if anchor is None:
                return None
            dialect = "postgresql" if anchor.platform else anchor.state.get("dialect", "postgresql")
            inputs, restricted = self._restrict_left_join_projection(
                spec, inputs, group_ref, key_spec, dialect, relations_by_name)
            compiled = compile_query(spec.pipeline, inputs, dict(spec.params),
                                     dialect=dialect, bind_prefix=f"gk{len(self._pushed_statements)}")
        except QueryError:
            return None
        group_column = compiled.statement.selected_columns.get(out)
        if group_column is None:
            return None
        group_expr = getattr(group_column, "element", group_column)
        pushed = compiled.statement.where(group_expr.in_(
            sa.bindparam("push_keys", expanding=True, type_=key_spec.sql_type())))
        # The pushed table replaces the derived relation itself: its columns,
        # batches, state and fingerprints stay the derived relation's; only the
        # compiled table and its bind parameters are regenerated.
        pushed_relation = replace(self.relations[alias], table=pushed.subquery(alias),
                                  bind_params=dict(compiled.params) if compiled.params else None)
        statement, exec_params, mapping = self._build_component_statement(
            root, base_relation=pushed_relation)
        return statement, exec_params, mapping, {"semi_keys"} if restricted else set()

    def _pull_component_filtered(self, root: str, ref: Ref, keys: list) -> list[dict]:
        pushed = self._pushed_component(root, ref)
        if pushed is not None:
            statement, exec_params, mapping, extra_params = pushed
            filtered = statement
            self._component_statement(root)  # label specs are shared with the pull
            base = self._component_members[root][0]
            self._group_key_pushdowns[base] = self._group_key_pushdowns.get(base, 0) + len(keys)
        else:
            statement, exec_params, mapping = self._component_statement(root)
            extra_params = set()
            label = next(l for l, alias, name in mapping if alias == ref.alias and name == ref.name)
            spec = self.column_specs[(ref.alias, ref.name)]
            filtered = statement.where(statement.selected_columns[label].in_(
                sa.bindparam("push_keys", expanding=True, type_=spec.sql_type())))
        keys_list = [f"{alias}.{name}" for _l, alias, name in mapping]
        specs = [self.column_specs[(alias, name)] for _l, alias, name in mapping]
        rows, total_bytes = [], 0
        chunk = max(1, self.limits.federated_key_chunk_size)
        for start in range(0, len(keys), chunk):
            batch_keys = list(keys[start:start + chunk])
            exec_chunk = {**exec_params, "push_keys": batch_keys}
            if "semi_keys" in extra_params:
                exec_chunk["semi_keys"] = batch_keys
            for raw in self._execute_statement(self._anchor(root), filtered,
                                               exec_chunk, self._statement_specs[root]):
                row = {f"{alias}.{name}": raw[label] for label, alias, name in mapping}
                total_bytes += self._measure(row, keys_list, specs)
                if (total_bytes > self.limits.federated_side_bytes_limit or
                        len(rows) >= min(self.limits.federated_side_rows_limit,
                                         self.limits.federated_join_rows_limit)):
                    raise QueryError(422, "query_budget_exceeded",
                                     "Key-filtered federated side exceeds the memory budget",
                                     budget="federated_side_bytes_limit", alias=ref.alias)
                self._charge_row(row)
                rows.append(row)
        # Metrics only, mirroring the materialized path's sourceRows keying:
        # counts and bytes of this keyed pull, never key values or row data.
        # encodedRowBytes is the COPY-encoded row measurement, not driver wire bytes.
        base = self._component_members[root][0]
        self.source_rows[base] = len(rows)
        self._keyed_pulls[base] = {"chunks": len(range(0, len(keys), chunk)),
                                   "rows": len(rows),
                                   "encodedRowBytes": total_bytes}
        return rows

    # -- in-memory predicates ---------------------------------------------------

    def _eval_expr(self, expr, row: dict) -> bool | None:
        op = expr.op
        if op in {"and", "or"}:
            values = [self._eval_expr(child, row) for child in expr.args]
            decisive = False if op == "and" else True
            if any(value is decisive for value in values):
                return decisive
            return None if None in values else not decisive
        if op == "not":
            value = self._eval_expr(expr.args[0], row)
            return None if value is None else not value
        ref = expr.args[0]
        if not isinstance(ref, Ref):
            raise QueryError(422, "invalid_query", "Predicate left side must be a field reference")
        spec = self.column_specs.get((ref.alias, ref.name))
        if spec is None:
            raise QueryError(422, "unknown_field", f"Unknown field {ref.alias}.{ref.name}")
        value = row[f"{ref.alias}.{ref.name}"]
        if op == "is_null":
            return value is None
        if op == "is_not_null":
            return value is not None
        if spec.type in {"json", "binary"}:
            raise QueryError(422, "unsupported_comparison", "JSON/binary support only NULL predicates")

        def literal(arg) -> Any:
            if arg.parameter and arg.value not in self.params:
                raise QueryError(422, "missing_parameter", f"Missing parameter :{arg.value}")
            raw = self.params[arg.value] if arg.parameter else arg.value
            return _typed_value(spec, raw)

        if op == "contains":
            if spec.type != "string":
                raise QueryError(422, "unsupported_comparison", "contains requires a string field")
            return None if value is None else literal(expr.args[1]) in value
        if op == "in":
            if len(expr.args) > 1001:
                raise QueryError(422, "query_too_complex", "IN supports at most 1000 values")
            values = [literal(item) for item in expr.args[1:]]
            if value is None:
                return None
            if any(value == item for item in values if item is not None):
                return True
            return None if None in values else False
        typed = literal(expr.args[1])
        if op in {">", ">=", "<", "<="} and spec.type not in _ORDERABLE:
            raise QueryError(422, "unsupported_comparison", f"Cannot order {spec.type}")
        if value is None or typed is None:
            return None
        if op == "=":
            return value == typed
        if op in {"!=", "<>"}:
            return value != typed
        if op == ">":
            return value > typed
        if op == ">=":
            return value >= typed
        if op == "<":
            return value < typed
        if op == "<=":
            return value <= typed
        raise QueryError(422, "invalid_query", f"Unsupported predicate {op!r}")

    # -- execution ----------------------------------------------------------------

    def run(self, join_constraints: dict[str, JoinConstraint] | None = None) -> MemoryResult:
        joins, wheres, tail = self._analyze()
        for _source, alias, left, right, kind in joins:
            self._join_by_alias.setdefault(alias, (left, right, kind))
        if join_constraints:
            self.check_join_constraints(join_constraints)
        connections = {r.connection_key for r in self.relations.values()}
        if len(connections) == 1:
            return self._run_single(tail)
        return self._run_federated(joins, wheres, tail)

    def _count_statement(self, anchor: Relation, statement, exec_params) -> int:
        count_stmt = sa.select(sa.func.count().label("n")).select_from(
            statement.order_by(None).subquery())
        row = self._execute_relation_one(anchor, count_stmt, exec_params)
        return int(row["n"])

    def _run_single(self, tail) -> MemoryResult:
        first = self.relations[self.pipeline.alias]
        dialect = "postgresql" if first.platform else first.state.get("dialect", "postgresql")
        compiled = compile_query(self.pipeline, self.relations, self.params, dialect=dialect)
        exec_params = dict(compiled.params)
        for relation in self.relations.values():
            if relation.bind_params:
                exec_params.update(relation.bind_params)
        limit_cmd = next((c for c in self.pipeline.commands if c.op == "limit"), None)
        label_specs = {spec.name: spec for spec in compiled.columns}
        if limit_cmd is not None:
            # Count first (the persisted native path does the same), then stream
            # the LIMIT-bounded statement: materialization is bounded by bytes.
            total = self._count_statement(first, compiled.before_limit, exec_params)
            statement = compiled.statement
        else:
            total = None
            statement = compiled.statement
        rows: list[dict] = []
        row_bytes = 0
        streamed = 0
        for raw in self._execute_statement(first, statement, exec_params, label_specs):
            streamed += 1
            if streamed > self.limits.federated_join_rows_limit:
                raise QueryError(422, "query_budget_exceeded",
                                 "Materialized result rows exceed the bounded row budget",
                                 budget="federated_join_rows_limit")
            data = {spec.name: encode_value(raw[spec.name], spec) for spec in compiled.columns}
            row_bytes += encoded_size(data)
            if row_bytes > self.limits.memory_result_bytes_limit:
                raise QueryError(422, "query_budget_exceeded",
                                 "In-memory result exceeds the per-result byte budget",
                                 budget="memory_result_bytes_limit",
                                 limitBytes=self.limits.memory_result_bytes_limit)
            self.memory_budget.charge(data)
            rows.append(data)
        if total is None:
            total = streamed
        plan = {"strategy": "memory_source_pushdown",
                "operators": [c.op for c in self.pipeline.commands],
                "sourceRows": {self.pipeline.alias: streamed}}
        return self._result(compiled.columns, rows, row_bytes, limit_cmd, total, plan,
                            avg_semantics=None)

    @staticmethod
    def _apply_limit(rows, limit_cmd):
        if limit_cmd is None:
            return rows
        limit = limit_cmd.args[0]
        offset = limit_cmd.args[1] if len(limit_cmd.args) > 1 else 0
        return rows[offset:offset + limit]

    def _run_federated(self, joins, wheres, tail) -> MemoryResult:
        in_memory = self._plan_components(joins, wheres)
        self._needed_cache = self._needed_columns(joins, wheres, tail)

        def mapping_of(root: str):
            return self._component_statement(root)[2]

        def initial_accumulated(root: str):
            mapping = mapping_of(root)
            rows = self._materialize_component(root)
            keys = [f"{alias}.{name}" for _l, alias, name in mapping]
            specs = [self.column_specs[(alias, name)] for _l, alias, name in mapping]
            total = sum(self._measure(row, keys, specs) for row in rows)
            return rows, keys, total

        accumulated: list[dict] | None = None
        accumulated_keys: list[str] = []
        accumulated_bytes = 0

        def join_row_budget(alias: str):
            if accumulated is not None and len(accumulated) > self.limits.federated_join_rows_limit:
                raise QueryError(422, "query_budget_exceeded",
                                 "Cross-source intermediate rows exceed the bounded join row budget",
                                 budget="federated_join_rows_limit", alias=alias)

        hoisted = {alias for alias, kind in self._entry_kind.items() if kind == "hoisted"}
        for cmd in self.pipeline.commands:
            if cmd.op == "where":
                expr = cmd.args[0]
                if expr in in_memory:
                    if accumulated is None:
                        raise QueryError(422, "invalid_operator_order",
                                         "Predicate references are not visible before their sources join")
                    accumulated = [row for row in accumulated if self._eval_expr(expr, row) is True]
                    accumulated_bytes = sum(
                        self._measure(row, accumulated_keys, [self._spec_for_key(k) for k in accumulated_keys])
                        for row in accumulated) if accumulated else 0
                continue
            if cmd.op != "join":
                continue
            _source, alias, left, right, kind = cmd.args
            if left.alias == alias and right.alias != alias:
                left, right = right, left
            if alias in hoisted:
                continue
            root = self._component_of[alias]
            if accumulated is None:
                left_root = self._component_of[left.alias]
                accumulated, accumulated_keys, accumulated_bytes = initial_accumulated(left_root)
            join_row_budget(alias)
            partner_map = mapping_of(root)
            partner_cols = [f"{alias}.{name}" for _l, alias, name in partner_map]
            partner_specs = [self.column_specs[(alias, name)] for _l, alias, name in partner_map]
            left_key = f"{left.alias}.{left.name}"
            right_key = f"{right.alias}.{right.name}"
            distinct_key_set = set()
            for row in accumulated:
                self._check()
                key = row[left_key]
                if key is not None and key not in distinct_key_set:
                    self.memory_budget.charge(key, overhead=128)
                    distinct_key_set.add(key)
            self.memory_budget.check_bytes(len(distinct_key_set) * 128)
            distinct_keys = sorted(distinct_key_set,
                                   key=lambda v: (type(v).__name__, v if isinstance(v, (int, float, str)) else str(v)))
            key_limit = self.limits.federated_pushdown_keys_limit
            if kind == "left":
                # A LEFT join only needs right rows whose join key is present on
                # the left. The missing-key rows are supplied by the NULL
                # extension below, so chunked key filtering preserves the LEFT
                # semantics even when the distinct key set exceeds the single
                # IN-list safety limit. This keeps source-side aggregation on
                # the remote connection instead of materializing an unrelated
                # full right-side group set.
                partner_rows = (self._pull_component_filtered(root, right, distinct_keys)
                                if distinct_keys else [])
            elif len(distinct_keys) <= key_limit:
                partner_rows = (self._pull_component_filtered(root, right, distinct_keys)
                                if distinct_keys else [])
            else:
                # INNER join with too many left keys: materialize the partner side.
                if self._count_component(root) > self.limits.federated_side_rows_limit:
                    raise QueryError(422, "query_budget_exceeded",
                                     "Federated join side exceeds the row budget; pre-aggregate or filter first",
                                     budget="federated_side_rows_limit", alias=alias)
                partner_rows = self._materialize_component(root)
            probe: dict[Any, list[dict]] = {}
            for row in partner_rows:
                self._check()
                if row[right_key] is not None:
                    key = row[right_key]
                    self.memory_budget.charge_bytes(256 if key not in probe else 16)
                    probe.setdefault(key, []).append(row)
            merged: list[dict] = []
            merged_bytes = 0
            merged_keys = accumulated_keys + partner_cols
            merged_specs = [self._spec_for_key(k) for k in accumulated_keys] + partner_specs

            def append_joined(row):
                nonlocal merged_bytes
                self._check()
                merged_bytes += self._measure(row, merged_keys, merged_specs)
                if (len(merged) >= self.limits.federated_join_rows_limit
                        or merged_bytes > self.limits.federated_side_bytes_limit):
                    raise QueryError(422, "query_budget_exceeded",
                                     "Cross-source join result exceeds the bounded memory budget",
                                     budget="federated_join_rows_limit", alias=alias)
                self._charge_row(row)
                merged.append(row)

            for acc in accumulated:
                self._check()
                matches = probe.get(acc[left_key]) if acc[left_key] is not None else None
                if matches:
                    for partner in matches:
                        append_joined({**acc, **partner})
                elif kind == "left":
                    append_joined({**acc, **{col: None for col in partner_cols}})
            previous_accumulated = accumulated
            accumulated = merged
            accumulated_keys = accumulated_keys + [c for c in partner_cols if c not in set(accumulated_keys)]
            accumulated_bytes = merged_bytes
            # Each merged row is a fresh dictionary. The source-side rows and
            # the prior accumulator are no longer retained after this join.
            self._release_rows(previous_accumulated)
            self._release_rows(partner_rows)

        if accumulated is None:
            root = self._component_of[self.pipeline.alias]
            accumulated, accumulated_keys, accumulated_bytes = initial_accumulated(root)

        limit_cmd = next((c for c in tail if c.op == "limit"), None)
        columns, outputs = self._evaluate_tail(accumulated, tail)
        rows, row_bytes = self._encode_outputs(self._apply_limit(outputs, limit_cmd)
                                               if limit_cmd is not None else outputs, columns)
        total = len(outputs)
        plan = {"strategy": "memory_federated",
                "operators": [c.op for c in self.pipeline.commands],
                "sourceRows": dict(self.source_rows),
                "joinRows": len(accumulated),
                "keyedPull": dict(self._keyed_pulls),
                # Count of chunk keys compiled below a derived GROUP BY per
                # alias; metrics only, never key values.
                "groupKeyPushdown": dict(self._group_key_pushdowns),
                # Snapshot at plan time; charged is the conservative
                # application-side accounting, not process RSS.
                "memoryBudget": {"usedBytes": self.memory_budget.charged,
                                 "limitBytes": self.memory_budget.limit},
                "pushdown": {root: [c.op for c in cmds]
                             for root, cmds in self._component_commands.items()}}
        return self._result(columns, rows, row_bytes, limit_cmd, total, plan,
                            avg_semantics=AVG_SEMANTICS if _uses_avg(tail) else None)

    # -- tail evaluation ------------------------------------------------------------

    def _evaluate_tail(self, rows: list[dict], tail):
        fields_cmd = next((c for c in tail if c.op == "fields"), None)
        stats_cmd = next((c for c in tail if c.op == "stats"), None)
        projected: dict[Ref, str] = {}
        if fields_cmd is not None:
            columns = [replace(self._spec(ref), name=name) for ref, name in fields_cmd.args]
            outputs = []
            for row in rows:
                self._check()
                output = {name: row[f"{ref.alias}.{ref.name}"] for ref, name in fields_cmd.args}
                self.memory_budget.charge(output)
                outputs.append(output)
            projected = {ref: name for ref, name in fields_cmd.args}
        elif stats_cmd is not None:
            columns, outputs, projected = self._aggregate(rows, stats_cmd)
        else:
            columns = []
            for _source_name, alias in self.pipeline.sources:
                for name, spec in self.relations[alias].columns.items():
                    out = f"{alias}.{name}"
                    if len(out.encode("utf-8")) > 63:
                        raise QueryError(422, "invalid_output_name", "Alias long source columns with FIELDS")
                    columns.append(replace(spec, name=out))
                    projected[Ref(alias, name)] = out
            keys = [spec.name for spec in columns]
            outputs = []
            for row in rows:
                self._check()
                output = {key: row[key] for key in keys}
                self.memory_budget.charge(output)
                outputs.append(output)
        names = [spec.name for spec in columns]
        if len(names) != len(set(names)):
            raise QueryError(422, "duplicate_output", "Output names must be unique")
        for name in names:
            if not name or len(name.encode("utf-8")) > 63 or "\x00" in name:
                raise QueryError(422, "invalid_output_name",
                                 "Output names must occupy 1..63 UTF-8 bytes; alias long physical names")
        for cmd in tail:
            if cmd.op == "distinct":
                seen = set()
                deduped = []
                for row in outputs:
                    self._check()
                    marker = tuple(_hashable(row[name]) for name in names)
                    if marker not in seen:
                        self.memory_budget.charge(marker, overhead=128)
                        seen.add(marker)
                        deduped.append(row)
                outputs = deduped
            elif cmd.op == "sort":
                outputs = self._sort(outputs, cmd, columns, projected)
        return columns, outputs

    def _aggregate(self, rows, stats_cmd):
        groups = next((arg[1] for arg in stats_cmd.args if arg[0] == "group"), ())
        group_items: list[tuple[Ref, str]] = []
        columns: list[ColumnSpec] = []
        for item in groups:
            ref, out = item if isinstance(item, tuple) else (item, item.name)
            spec = self._spec(ref)
            if spec.type in {"json", "binary"}:
                raise QueryError(422, "unsupported_group_type", "Cannot group JSON/binary")
            group_items.append((ref, out))
            columns.append(replace(spec, name=out))
        aggregates: list[tuple[str, Ref | None, str]] = []
        for item in stats_cmd.args:
            if item[0] == "group":
                continue
            fn, ref, name = item
            if fn == "count" and ref is None:
                spec = ColumnSpec(name, "integer", False)
            else:
                if ref is None:
                    raise QueryError(422, "invalid_aggregate", f"{fn} requires a field")
                source_spec = self._spec(ref)
                if fn in {"sum", "avg"} and source_spec.type not in {"integer", "decimal", "float"}:
                    raise QueryError(422, "invalid_aggregate_type", f"{fn} requires a numeric field")
                if fn in {"min", "max"} and source_spec.type not in _ORDERABLE:
                    raise QueryError(422, "invalid_aggregate_type", f"{fn} requires an orderable field")
                if fn == "count_distinct" and source_spec.type in {"json", "binary"}:
                    raise QueryError(422, "invalid_aggregate_type", "COUNT DISTINCT requires a scalar field")
                if fn.startswith("count"):
                    spec = ColumnSpec(name, "integer", False)
                elif fn in {"sum", "avg"} and source_spec.type in {"integer", "decimal"}:
                    spec = ColumnSpec(name, "decimal", True)
                else:
                    spec = replace(source_spec, name=name, nullable=True)
            aggregates.append((fn, ref, name))
            columns.append(spec)
        states: dict[tuple, list[dict]] = {}
        order: list[tuple] = []
        for row in rows:
            self._check()
            key = tuple(_hashable(row[f"{ref.alias}.{ref.name}"]) for ref, _out in group_items)
            state = states.get(key)
            if state is None:
                state = [_init_state() for _ in aggregates]
                self.memory_budget.charge((key, state), overhead=192)
                states[key] = state
                order.append(key)
            for index, (fn, ref, _name) in enumerate(aggregates):
                value = None if ref is None else row[f"{ref.alias}.{ref.name}"]
                if fn == "count_distinct" and value is not None and value not in state[index]["distinct"]:
                    self.memory_budget.charge(value, overhead=96)
                before = sum(resident_size(state[index][name])
                             for name in ("count", "count_value", "sum"))
                _update_state(state[index], fn, value, self.memory_budget)
                after = sum(resident_size(state[index][name])
                            for name in ("count", "count_value", "sum"))
                self.memory_budget.charge_bytes(max(0, after - before))
        outputs = []
        if not group_items and not rows:
            # SQL semantics: aggregates without GROUP BY over empty input
            # produce exactly one row (COUNT 0, SUM NULL).
            outputs.append({name: _final_state(_init_state(), fn, ref is None)
                            for fn, ref, name in aggregates})
        for key in order:
            row = {out: _state_from_key(key, position)
                   for position, (_ref, out) in enumerate(group_items)}
            for index, (fn, ref, name) in enumerate(aggregates):
                row[name] = _final_state(states[key][index], fn, ref is None)
            self.memory_budget.charge(row)
            outputs.append(row)
        projected = {ref: out for ref, out in group_items}
        return columns, outputs, projected

    def _sort(self, outputs, cmd, columns, projected):
        spec_by_name = {spec.name: spec for spec in columns}

        def item_of(item):
            ref, direction, nulls = item
            name = projected.get(ref, ref) if isinstance(ref, Ref) else ref
            if name not in spec_by_name:
                raise QueryError(422, "unknown_output", f"Unknown sort output {name!r}")
            if spec_by_name[name].type not in _ORDERABLE | {"boolean"}:
                raise QueryError(422, "unsupported_sort_type", "Cannot sort this output type")
            return name, direction, nulls

        items = [item_of(item) for item in cmd.args]

        def compare(left: dict, right: dict) -> int:
            self._check()
            for name, direction, nulls in items:
                a, b = left[name], right[name]
                if a is None and b is None:
                    continue
                if a is None:
                    return -1 if nulls == "first" else 1
                if b is None:
                    return 1 if nulls == "first" else -1
                if a == b:
                    continue
                result = -1 if _less_than(a, b) else 1
                return result if direction == "asc" else -result
            return 0

        self.memory_budget.check_bytes(128 * len(outputs))
        return sorted(outputs, key=functools.cmp_to_key(compare))

    # -- output encoding ----------------------------------------------------------

    def _encode_outputs(self, outputs, columns) -> tuple[list[dict], int]:
        rows, row_bytes = [], 0
        for output in outputs:
            self._check()
            data = {spec.name: encode_value(output[spec.name], spec) for spec in columns}
            row_bytes += encoded_size(data)
            if row_bytes > self.limits.memory_result_bytes_limit:
                raise QueryError(422, "query_budget_exceeded",
                                 "In-memory result exceeds the per-result byte budget",
                                 budget="memory_result_bytes_limit",
                                 limitBytes=self.limits.memory_result_bytes_limit)
            self.memory_budget.charge(data)
            rows.append(data)
        return rows, row_bytes

    def _result(self, columns, rows, row_bytes, limit_cmd, total, plan, *, avg_semantics) -> MemoryResult:
        result_limited = False
        if limit_cmd is not None:
            limit, offset = limit_cmd.args[0], (limit_cmd.args[1] if len(limit_cmd.args) > 1 else 0)
            result_limited = total > limit or (offset > 0 and total > 0)
        source_states = []
        for alias, relation in self.relations.items():
            state = dict(relation.state)
            if relation.source_group is not None:
                state["completion"] = dict(relation.source_group.completion)
                state["snapshot"] = dict(relation.source_group.provenance)
            state["alias"] = alias
            source_states.append(state)
        connections = {r.connection_key for r in self.relations.values()}
        consistency = ("immutable_versions"
                       if all(s.get("consistency") == "immutable_version" for s in source_states)
                       else "single_source_snapshot" if len(connections) == 1
                       else "cross_source_non_atomic")
        if avg_semantics:
            plan["avgSemantics"] = avg_semantics
        return MemoryResult(columns=columns, rows=rows, row_bytes=row_bytes,
                            result_limited=result_limited,
                            classification=output_classification(self.pipeline),
                            avg_semantics=avg_semantics, plan=plan,
                            source_states=source_states, consistency=consistency)


def _state_from_key(key: tuple, position: int):
    return key[position]
