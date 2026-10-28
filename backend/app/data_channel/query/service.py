"""Bounded execution on PostgreSQL, durable ordinal pagination, and an
in-process memory mode.

Persisted mode (default): only compiler-produced SQLAlchemy selects reach a
source. Cross-source inputs are copied in bounded batches to TEMP tables;
PostgreSQL performs joins/sorts/aggregates and may spill under an enforced
disk budget. No Python table joins.

Memory mode (explicit result_storage="memory"): no PostgreSQL TEMP tables, no
COPY, no UnifiedQueryRow writes. Results live in bounded, TTL'd in-process
handles; cursors are bound to the serving instance so restarts answer 410
instead of re-executing. Cross-source joins/aggregations run in the bounded
federated executor.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import math
import re
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.data_channel.integrated.contracts import JsonText
from app.shared.query_control import (
    QueryControl, current_query_control, interruptible_connection, query_control_scope,
)

from .compiler import compile_query, refs_in
from .contracts import (Binding, ColumnSpec, DerivedSourceSpec, JoinConstraint,
                        TextQueryCursorRequest, QueryError, QueryLimits,
                        TextQueryRequest, TypedQueryRequest, Relation,
                        canonical_json, provider_for, stable_hash)
from .derived import resolve_derived
from .encoding import _copy_cell, _copy_line, _copy_parts, _json_parts, encode_value, encoded_size
from .federated import MemoryExecutor
from .handles import INSTANCE_ID, REGISTRY, MemoryHandle, handle_expiry, new_handle_id
from .models import UnifiedQueryRow, UnifiedQuerySnapshot
from .memory_budget import MemoryBudget, resident_size
from .parser import Pipeline, parse, pipeline_from_dict, pipeline_to_dict, validate_pipeline

_json = canonical_json
_hash = stable_hash


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _query_dbapi_error(exc: DBAPIError) -> QueryError:
    code = getattr(exc.orig, "pgcode", None)
    driver_args = getattr(exc.orig, "args", ())
    if driver_args and driver_args[0] in {3024, 1969}:
        return QueryError(422, "query_budget_exceeded", "Source query execution deadline exceeded",
                          budget="statement_timeout_ms")
    if code in {"40001", "40P01"}:
        return QueryError(409, "query_retry_required", "Concurrent query state changed; explicitly rerun the query")
    if code in {"57014", "53400", "53100", "53200", "54000"}:
        return QueryError(422, "query_budget_exceeded", "Database resource budget exhausted")
    if code and code.startswith("22"):
        return QueryError(422, "invalid_typed_value", "A value in the query scope violates its declared type")
    return QueryError(503, "query_execution_failed", "Query execution failed; no partial snapshot was published")


@dataclass
class _Plan:
    pipeline: Pipeline
    bindings: dict[str, Binding]
    derived: dict[str, DerivedSourceSpec]
    join_constraints: dict[str, JoinConstraint]
    params: dict[str, Any]
    page_size: int
    result_storage: str
    request_kind: str  # "text" | "typed"
    stored_request: dict
    query_hash: str
    origin: dict[str, Any] | None = None
    request_fingerprint: str | None = None
    session_kind: str | None = None
    session_id: str | None = None


def _scalar_params_ok(params: dict[str, Any]) -> None:
    if any(value is not None and type(value) not in {str, int, float, bool}
           for value in params.values()):
        raise QueryError(422, "invalid_parameter_type", "Parameters must be JSON scalars, not arrays/objects")
    try:
        parameter_bytes = len(_json(params).encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise QueryError(422, "invalid_parameter_type", "Parameters must be finite JSON scalars") from exc
    if parameter_bytes > 1024**2:
        raise QueryError(422, "query_budget_exceeded", "Query parameters exceed 1 MiB", budget="parameter_bytes")


def _params_used(pipeline: Pipeline) -> set[str]:
    from .parser import params_in
    return params_in(pipeline.commands)


class QueryService:
    def __init__(self, db: Session, user: Any, *, secret: str,
                 limits: QueryLimits | None = None):
        self.db, self.user, self.secret = db, user, secret
        self.limits = limits or QueryLimits()
        self.started = time.monotonic()
        self.stack = ExitStack()
        self.relations: dict[str, Relation] = {}
        self.source_rows: dict[str, int] = {}
        self.staging_tables: list[sa.Table] = []
        self.stage_bytes = 0

    def _deadline(self):
        control = current_query_control()
        if control is not None and control.cancelled():
            raise QueryError(409, "query_cancelled", "Query execution was cancelled")
        if (time.monotonic() - self.started) * 1000 > self.limits.statement_timeout_ms:
            raise QueryError(422, "query_budget_exceeded", "Query execution deadline exceeded",
                             budget="statement_timeout_ms")

    def _platform_budget(self):
        self._deadline()
        remaining = max(1, int(self.limits.statement_timeout_ms - (time.monotonic() - self.started) * 1000))
        self.db.execute(sa.text("SELECT set_config('statement_timeout', :value, true)"),
                        {"value": f"{remaining}ms"})

    def _pin(self):
        if self.db.get_bind().dialect.name != "postgresql":
            raise QueryError(503, "query_backend_unavailable", "Unified execution requires PostgreSQL")
        if self.db.in_transaction(): self.db.rollback()
        self.db.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        settings = {
            "statement_timeout": f"{self.limits.statement_timeout_ms}ms",
            "lock_timeout": f"{self.limits.lock_timeout_ms}ms",
            "work_mem": f"{self.limits.work_mem_mib}MB",
            "TimeZone": "UTC",
        }
        try:
            for name, value in settings.items():
                self.db.execute(sa.text("SELECT set_config(:name, :value, true)"),
                                {"name": name, "value": value})
            # temp_file_limit is SUSET. Ordinary roles may already have a tighter
            # effective cap set by operations; otherwise setting it must succeed.
            raw = str(self.db.execute(sa.text("SHOW temp_file_limit")).scalar_one())
            cap = _memory_bytes(raw)
            if cap is None or cap > self.limits.temp_file_limit_mib * 1048576:
                self.db.execute(sa.text("SELECT set_config('temp_file_limit', :value, true)"),
                                {"value": f"{self.limits.temp_file_limit_mib}MB"})
        except DBAPIError as exc:
            raise QueryError(503, "query_budget_unavailable",
                             "PostgreSQL query budget requires an effective role temp_file_limit or SET permission") from exc

    def _pin_memory(self):
        """Fresh bounded read transaction for platform-local leaves only.

        Memory mode never shares a caller's metadata transaction across source
        IO (idle_in_transaction kills it); any pending transaction is rolled
        back first. No temp tables exist in this mode, so no temp budget is
        configured. Pure-native queries skip PostgreSQL entirely.
        """
        if self.db.get_bind().dialect.name != "postgresql":
            raise QueryError(503, "query_backend_unavailable", "Unified execution requires PostgreSQL")
        if self.db.in_transaction(): self.db.rollback()
        self.db.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        for name, value in {"statement_timeout": f"{self.limits.statement_timeout_ms}ms",
                            "TimeZone": "UTC"}.items():
            self.db.execute(sa.text("SELECT set_config(:name, :value, true)"),
                            {"name": name, "value": value})

    # -- request normalization ------------------------------------------------

    def _plan_from_text(self, request: TextQueryRequest) -> _Plan:
        pipeline = parse(request.query)
        dump = request.model_dump(mode="json", by_alias=True)
        return _Plan(pipeline=pipeline, bindings=dict(request.bindings), derived={},
                    join_constraints={}, params=dict(request.params),
                    page_size=request.page_size, result_storage=request.result_storage,
                    request_kind="text", stored_request={"kind": "text", "request": dump},
                    query_hash=_hash(dump))

    def _plan_from_typed(self, request: TypedQueryRequest) -> _Plan:
        if not isinstance(request, TypedQueryRequest):
            raise QueryError(422, "invalid_request", "A TypedQueryRequest is required")
        pipeline = validate_pipeline(request.pipeline)
        if not isinstance(request.bindings, dict) or not 1 <= len(request.bindings) <= 16:
            raise QueryError(422, "invalid_bindings", "Between 1 and 16 leaf bindings are required")
        for name, binding in request.bindings.items():
            if not isinstance(name, str) or not isinstance(binding, Binding):
                raise QueryError(422, "invalid_bindings", "Bindings must map names to Binding objects")
        derived: dict[str, DerivedSourceSpec] = {}
        for alias, spec in (request.derived or {}).items():
            if not isinstance(spec, DerivedSourceSpec):
                raise QueryError(422, "invalid_derived", "Derived specs must be DerivedSourceSpec objects")
            derived[alias] = spec
        constraints = self._join_constraints(request)
        params = dict(request.params or {})
        _scalar_params_ok(params)
        for alias, spec in derived.items():
            _scalar_params_ok(dict(spec.params))
        used = _params_used(pipeline)
        for spec in derived.values():
            used |= _params_used(spec.pipeline)
        unused = set(params) - used
        if unused:
            raise QueryError(422, "unused_parameter",
                             f"Unused parameter(s): {', '.join(sorted(unused))}")
        if type(request.page_size) is not int or not 1 <= request.page_size <= 1000:
            raise QueryError(422, "invalid_page_size", "Page size must be 1..1000")
        for label, value in (("session_kind", request.session_kind), ("session_id", request.session_id)):
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 128):
                raise QueryError(422, "invalid_request", f"{label} must be a 1..128 byte string or null")
        stored = {"kind": "typed", "pipeline": pipeline_to_dict(pipeline),
                  "bindings": {k: b.model_dump(mode="json", by_alias=True) for k, b in request.bindings.items()},
                  "derived": {a: {"source": s.source, "pipeline": pipeline_to_dict(s.pipeline),
                                  "params": dict(s.params)} for a, s in derived.items()},
                  "joinConstraints": {a: {"cardinality": c.cardinality, "keyType": c.key_type}
                                      for a, c in constraints.items()},
                  "params": params, "pageSize": request.page_size,
                  "resultStorage": request.result_storage,
                  "sessionKind": request.session_kind, "sessionId": request.session_id}
        # request_fingerprint is an opaque upstream binding (echoed on the
        # handle and memory cursors); the core's own canonical hash above is
        # the internal integrity anchor and never requires upstream replication.
        # The core's canonical hash covers origin (process-memory only) and is
        # the internal integrity anchor; request_fingerprint stays opaque.
        query_hash = _hash({**stored, "origin": request.origin})
        return _Plan(pipeline=pipeline, bindings=dict(request.bindings), derived=derived,
                     join_constraints=constraints, params=params,
                     page_size=request.page_size, result_storage=request.result_storage,
                     request_kind="typed", stored_request=stored, query_hash=query_hash,
                     origin=request.origin, request_fingerprint=request.request_fingerprint,
                     session_kind=request.session_kind, session_id=request.session_id)

    @staticmethod
    def _join_constraints(request: TypedQueryRequest) -> dict[str, JoinConstraint]:
        constraints: dict[str, JoinConstraint] = {}
        for alias, constraint in (request.join_constraints or {}).items():
            if not isinstance(alias, str) or not isinstance(constraint, JoinConstraint):
                raise QueryError(422, "invalid_join_constraint",
                                 "Join constraints must map aliases to JoinConstraint objects")
            constraints[alias] = constraint
        if not constraints and isinstance(request.origin, dict):
            # Bridge plans may declare constraints inside origin; explicit
            # fields always win and duplicates are not treated as conflicts.
            raw = request.origin.get("joinConstraints")
            if isinstance(raw, dict):
                for alias, item in raw.items():
                    if (not isinstance(alias, str) or not isinstance(item, dict)
                            or item.get("cardinality") not in {"1:1", "1:N", "N:1", "N:M"}):
                        raise QueryError(422, "invalid_join_constraint",
                                         "origin.joinConstraints entries are malformed")
                    key_type = item.get("keyType")
                    if key_type is not None and key_type not in {"uuid", "integer", "string", "decimal", "number"}:
                        raise QueryError(422, "invalid_join_constraint", "Unknown join key type")
                    constraints[alias] = JoinConstraint(cardinality=item["cardinality"], key_type=key_type)
        return constraints

    def _plan_from_stored(self, stored: dict) -> _Plan:
        # Both the historical raw TextQueryRequest dump and the wrapped forms
        # remain readable: durability of existing snapshots is a contract.
        if isinstance(stored, dict) and stored.get("kind") == "typed":
            pass
        elif isinstance(stored, dict) and stored.get("kind") == "text":
            stored = stored["request"]
        elif not (isinstance(stored, dict) and stored.get("language") == "spl"):
            raise QueryError(404, "snapshot_not_found", "Query snapshot not found")
        if isinstance(stored, dict) and stored.get("kind") == "typed":
            try:
                pipeline = pipeline_from_dict(stored["pipeline"])
                bindings = {k: Binding.model_validate(v) for k, v in stored["bindings"].items()}
                derived = {alias: DerivedSourceSpec(
                    alias=alias, source=spec["source"],
                    pipeline=pipeline_from_dict(spec["pipeline"]), params=dict(spec["params"]))
                    for alias, spec in stored.get("derived", {}).items()}
                constraints = {alias: JoinConstraint(cardinality=spec["cardinality"], key_type=spec.get("keyType"))
                               for alias, spec in stored.get("joinConstraints", {}).items()}
            except (KeyError, TypeError, ValueError) as exc:
                raise QueryError(409, "query_context_changed", "Stored request cannot be rebuilt") from exc
            return _Plan(pipeline=pipeline, bindings=bindings, derived=derived,
                         join_constraints=constraints, params=dict(stored["params"]),
                         page_size=stored["pageSize"], result_storage=stored["resultStorage"],
                         request_kind="typed", stored_request=stored,
                         query_hash=_hash({**stored, "origin": None}),
                         session_kind=stored.get("sessionKind"), session_id=stored.get("sessionId"))
        return self._plan_from_text(TextQueryRequest.model_validate(stored))

    # -- resolution ------------------------------------------------------------

    def _resolve(self, plan: _Plan, *, recheck_only: bool = False):
        pipeline = plan.pipeline
        names = {name for name, _ in pipeline.sources}
        available = set(plan.bindings) | set(plan.derived)
        if not names or not names <= available:
            raise QueryError(422, "invalid_bindings",
                             "Pipeline sources must reference bindings or derived aliases")
        # Bounded closure: outer pipelines may reference only top-level derived
        # aliases; their leaf dependencies resolve transitively. Every declared
        # binding and derived alias must be reachable (no dead sources).
        closure, stack = set(names), list(names)
        while stack:
            current = stack.pop()
            spec = plan.derived.get(current)
            if spec is None:
                continue
            for source_name, _alias in validate_pipeline(spec.pipeline).sources:
                if source_name not in available:
                    raise QueryError(422, "invalid_bindings",
                                     f"Derived source {current!r} references unknown source {source_name!r}")
                if source_name not in closure:
                    closure.add(source_name)
                    stack.append(source_name)
        if closure != available:
            raise QueryError(422, "invalid_bindings",
                             "Every binding and derived alias must be reachable from the pipeline")
        for name, binding in plan.bindings.items():
            required = {"integrated": ("asset_id",), "dataset": ("dataset_id", "version_id"),
                        "ontology": ("ontology_id", "release_id", "object_type_id")}[binding.kind]
            allowed = {"kind", *required}
            values = binding.model_dump(exclude_none=True)
            if not all(values.get(key) for key in required) or set(values) != allowed:
                raise QueryError(422, "invalid_binding", f"Invalid {binding.kind} binding fields")
        # A leaf may surface only inside derived pipelines; collect each name's
        # alias from every pipeline that references it (first wins).
        leaf_aliases: dict[str, str] = {}
        for name, alias in pipeline.sources:
            if name in plan.bindings:
                leaf_aliases.setdefault(name, alias)
        for spec in plan.derived.values():
            for name, alias in validate_pipeline(spec.pipeline).sources:
                if name in plan.bindings:
                    leaf_aliases.setdefault(name, alias)
        for name in plan.bindings:
            if name not in leaf_aliases:
                raise QueryError(422, "invalid_bindings", f"Binding {name!r} is never referenced")
        if recheck_only:
            from .providers import resolve_integrated
            integrated = [(leaf_aliases[n], plan.bindings[n]) for n in plan.bindings
                          if plan.bindings[n].kind == "integrated"]
            integrated_relations = resolve_integrated(
                self.db, self.user, integrated, self.limits, self.stack,
                recheck_only=True) if integrated else {}
            for name, binding in plan.bindings.items():
                alias = leaf_aliases[name]
                if binding.kind == "integrated":
                    relation = integrated_relations[alias]
                elif binding.kind == "dataset":
                    from .providers import resolve_dataset
                    relation = resolve_dataset(self.db, self.user, alias, binding, self.limits)
                else:
                    relation = provider_for(binding.kind)(self.db, self.user, alias, binding, self.limits)
                if relation.close:
                    self.stack.callback(relation.close)
                self.relations[name] = relation
            if plan.derived:
                self.relations = resolve_derived(self.relations, plan.derived)
            for name, relation in self.relations.items():
                if relation.state.get("completeness") != "complete":
                    raise QueryError(409, "source_incomplete", "Query requires complete source scope", alias=name)
                if not relation.auth_fingerprint or not relation.schema_fingerprint:
                    raise QueryError(503, "query_provider_invalid", "Provider must pin authorization and schema")
            self._deadline()
            return
        for name, binding in plan.bindings.items():
            alias = leaf_aliases[name]
            if binding.kind == "integrated":
                # Resolve all aliases together so same-connection joins share a
                # single source transaction and a single RR snapshot.
                if not hasattr(self, "_integrated_relations"):
                    from .providers import resolve_integrated
                    integrated = [(leaf_aliases[n], plan.bindings[n]) for n in plan.bindings
                                  if plan.bindings[n].kind == "integrated"]
                    self._integrated_relations = resolve_integrated(
                        self.db, self.user, integrated, self.limits, self.stack)
                relation = self._integrated_relations[alias]
            elif binding.kind == "dataset":
                from .providers import resolve_dataset
                relation = resolve_dataset(self.db, self.user, alias, binding, self.limits)
            else:
                relation = provider_for(binding.kind)(self.db, self.user, alias, binding, self.limits)
            if relation.close: self.stack.callback(relation.close)
            self.relations[name] = relation
        # Relations stay keyed by source NAME; each compilation maps them onto
        # the aliases its own pipeline uses.
        if plan.derived:
            self.relations = resolve_derived(self.relations, plan.derived)
        for name, relation in self.relations.items():
            if relation.state.get("completeness") != "complete":
                raise QueryError(409, "source_incomplete", "Query requires complete source scope", alias=name)
            if not relation.auth_fingerprint or not relation.schema_fingerprint:
                raise QueryError(503, "query_provider_invalid", "Provider must pin authorization and schema")
        self._deadline()

    @staticmethod
    def _plan_needs_pin(plan: _Plan) -> bool:
        # Dataset versions and the ontology read port resolve and execute on
        # the platform database; integrated sources bring their own scopes.
        return any(binding.kind in {"dataset", "ontology"} for binding in plan.bindings.values())

    def _execution_relations(self, pipeline: Pipeline) -> dict[str, Relation]:
        """Alias-keyed relations for one pipeline's compilation/execution."""
        execution: dict[str, Relation] = {}
        for name, alias in pipeline.sources:
            relation = self.relations.get(name)
            if relation is None:
                raise QueryError(422, "invalid_bindings", f"Source {name!r} was not resolved")
            if alias in execution:
                raise QueryError(422, "duplicate_alias", "Pipeline aliases must be unique")
            execution[alias] = relation
        return execution

    def _validate_derived_constraints(self, plan: _Plan, *, memory_budget=None,
                                      cancel_check=None) -> None:
        """Enforce declared cardinality for joins INSIDE derived pipelines too.

        The outer executor only sees outer joins; a derived spec's own multi-
        table joins carry the same declared constraints and are checked here
        against the derived scope (the parent relations it compiles over).
        """
        if not plan.join_constraints:
            return
        outer_joins = {cmd.args[1] for cmd in plan.pipeline.commands if cmd.op == "join"}
        known = set(outer_joins)
        for spec in plan.derived.values():
            known |= {cmd.args[1] for cmd in spec.pipeline.commands if cmd.op == "join"}
        unknown = set(plan.join_constraints) - known
        if unknown:
            raise QueryError(422, "invalid_join_constraint",
                             "Join constraint must key a joined alias",
                             aliases=sorted(unknown))
        for spec in plan.derived.values():
            pipeline = spec.pipeline
            relevant = {alias: constraint for alias, constraint in plan.join_constraints.items()
                        if alias not in outer_joins
                        and any(cmd.op == "join" and cmd.args[1] == alias
                                for cmd in pipeline.commands)}
            if not relevant:
                continue
            local = self._execution_relations(pipeline)
            MemoryExecutor(self, pipeline, local, spec.params,
                           memory_budget=memory_budget,
                           cancel_check=cancel_check).check_join_constraints(relevant)

    # Semantic-to-native type compatibility: exact-decision only, never a cast.
    # A semantic "number" may bind native integer/decimal (both exact); float is
    # rejected because it cannot represent the semantic contract exactly, and
    # string never silently converts to a number.
    _TYPE_COMPAT = {
        "string": {"string"}, "integer": {"integer"}, "number": {"integer", "decimal"},
        "decimal": {"decimal"}, "boolean": {"boolean"}, "date": {"date"},
        "datetime": {"timestamp"}, "timestamp": {"timestamp"}, "time": {"time"},
        "json": {"json"},
    }

    @classmethod
    def _check_expectations(cls, origin: dict | None, relations: dict[str, Relation]) -> None:
        if not isinstance(origin, dict):
            return
        expectations = origin.get("expectations")
        if expectations is None:
            return
        if not isinstance(expectations, dict):
            raise QueryError(422, "invalid_request", "origin.expectations must map aliases to metadata")
        for alias, meta in expectations.items():
            relation = relations.get(alias)
            if relation is None or not isinstance(meta, dict):
                raise QueryError(422, "invalid_request",
                                 "Expectations must reference a resolved source alias", alias=alias)
            schema_fp = meta.get("schemaFingerprint")
            if schema_fp is not None:
                if not isinstance(schema_fp, str) or schema_fp != relation.schema_fingerprint:
                    raise QueryError(409, "query_context_changed",
                                     "Source schema no longer matches the frozen binding expectation",
                                     alias=alias)
            property_map = meta.get("propertyMap")
            declared = meta.get("declaredTypes") if isinstance(meta.get("declaredTypes"), dict) else {}
            if property_map is not None:
                if not isinstance(property_map, dict):
                    raise QueryError(422, "invalid_request", "propertyMap must map semantic to physical names")
                for semantic, physical in property_map.items():
                    spec = relation.columns.get(physical)
                    if spec is None:
                        raise QueryError(409, "query_context_changed",
                                         "Physical column for a semantic property disappeared",
                                         alias=alias, semantic=semantic)
                    if semantic in declared:
                        expected = declared[semantic]
                        compatible = cls._TYPE_COMPAT.get(expected)
                        if compatible is None:
                            raise QueryError(422, "invalid_request",
                                             "Unknown declared semantic type", alias=alias,
                                             semantic=semantic, declaredType=expected)
                        if spec.type not in compatible:
                            raise QueryError(409, "query_context_changed",
                                             "Declared type is not exactly representable by the physical column",
                                             alias=alias, semantic=semantic,
                                             declaredType=expected, physicalType=spec.type)

    def _page_scope(self, page_size: int):
        for alias, relation in self.relations.items():
            cap = relation.state.get("maxPageSize")
            if cap is not None:
                if type(cap) is not int or cap < 1:
                    raise QueryError(503, "query_provider_invalid", "Provider returned an invalid output quota")
                if page_size > cap:
                    raise QueryError(422, "query_page_size_exceeds_scope",
                                     "Page size exceeds the authorized source output quota",
                                     alias=alias, maxPageSize=cap)

    def _fingerprints(self):
        return (_hash({a: r.auth_fingerprint for a, r in self.relations.items()}),
                _hash({a: r.schema_fingerprint for a, r in self.relations.items()}))

    # -- persisted execution ---------------------------------------------------

    def _physical_size(self, table: sa.Table) -> int:
        return int(self.db.execute(sa.text("SELECT pg_total_relation_size(CAST(:name AS regclass))"),
                                   {"name": table.name}).scalar_one())

    def _check_staging_size(self):
        self.stage_bytes = sum(self._physical_size(table) for table in self.staging_tables)
        if self.stage_bytes > self.limits.staging_bytes_limit:
            raise QueryError(422, "query_budget_exceeded", "Physical input staging disk budget exceeded",
                             budget="staging_bytes_limit", actualBytes=self.stage_bytes)
        self._deadline()

    def _copy(self, table: sa.Table, names: list[str], specs: list[ColumnSpec], rows):
        buffer = io.StringIO()
        buffered_bytes = 0
        # A row-count bound alone does not bound memory for wide TEXT/JSON.
        # Large rows fail explicitly instead of truncating cells or returning a
        # partial success. Every COPY buffer is at most 16 MiB of UTF-8 text.
        buffer_limit = min(16 * 1024**2, self.limits.result_bytes_limit)
        preparer = self.db.get_bind().dialect.identifier_preparer
        columns = ", ".join(preparer.quote(name) for name in names)
        sql = f"COPY {preparer.quote(table.name)} ({columns}) FROM STDIN WITH (FORMAT text)"
        connection = self.db.connection().connection.driver_connection
        with connection.cursor() as cursor:
            for row in rows:
                values = [row[name] for name in names] if hasattr(row, "keys") else row
                text, size = _copy_line(values, specs, buffer_limit)
                if buffered_bytes + size > buffer_limit:
                    self._platform_budget()
                    buffer.seek(0)
                    cursor.copy_expert(sql, buffer)
                    buffer = io.StringIO()
                    buffered_bytes = 0
                buffer.write(text)
                buffered_bytes += size
            if buffered_bytes:
                self._platform_budget()
                buffer.seek(0)
                cursor.copy_expert(sql, buffer)

    def _stage(self, alias: str, relation: Relation, needed: set[str]) -> Relation:
        # COUNT(*) still streams a constant row per input; no hidden source cap.
        cols = list(needed) or [next(iter(relation.columns))]
        if relation.batches is None:
            raise QueryError(503, "query_provider_invalid", "External source needs a bounded stream")
        table = sa.Table("uq_in_" + uuid.uuid4().hex, sa.MetaData(),
                         *(sa.Column(f"c{i}", relation.columns[name].sql_type()) for i, name in enumerate(cols)),
                         prefixes=["TEMPORARY"], postgresql_on_commit="DROP")
        table.create(self.db.connection())
        self.staging_tables.append(table)
        source_columns = [(relation.expressions[name] if name in relation.expressions else relation.table.c[name]).label(name) for name in cols]
        statement = sa.select(*source_columns).select_from(relation.table)
        count = 0
        # The provider validates complete EOF in its own pinned source snapshot.
        for batch in relation.batches(statement, {}, self.limits.batch_size):
            if len(batch) > self.limits.batch_size:
                raise QueryError(503, "query_provider_invalid", "Provider exceeded negotiated batch size")
            physical_names = [f"c{i}" for i in range(len(cols))]
            normalized = ([row[name] for name in cols] for row in batch)
            self._copy(table, physical_names, [relation.columns[name] for name in cols], normalized)
            count += len(batch)
            self._check_staging_size()
        self.source_rows[alias] = count
        if relation.source_group is not None:
            relation.state["inputCompletion"] = dict(relation.source_group.completion)
        return replace(relation, table=table, platform=True,
                       columns={name: relation.columns[name] for name in cols},
                       expressions={name: table.c[f"c{i}"] for i, name in enumerate(cols)})

    def _query_rows(self, compiled, relations, native: bool):
        def restore_json(row):
            if not any(spec.type == "json" for spec in compiled.columns):
                return row
            restored = dict(row)
            for spec in compiled.columns:
                value = restored[spec.name]
                if spec.type != "json" or value is None:
                    continue
                if not isinstance(value, str):
                    raise QueryError(422, "invalid_typed_value", "JSON text projection is unavailable")
                # Guard the driver's one raw cell before JsonText copies it.
                _copy_line([value], [ColumnSpec(spec.name, "string")],
                           min(16 * 1024**2, self.limits.result_bytes_limit), size_only=True)
                restored[spec.name] = JsonText(value)
            return restored
        if native:
            relation = next(iter(relations.values()))
            if relation.batches is None:
                raise QueryError(503, "query_provider_invalid", "Source stream is not available")
            for batch in relation.batches(compiled.statement, compiled.params, self.limits.batch_size):
                yield [restore_json(row) for row in batch]
        else:
            self._platform_budget()
            # Row-count-only prefetch can allocate gigabytes before COPY sees
            # a wide TEXT/JSON row. Permit only one DBAPI row in flight; COPY
            # incrementally enforces the exact encoded-byte limit below. The
            # driver's allocation for one cell itself cannot be bounded here.
            result = self.db.execute(compiled.statement.execution_options(
                stream_results=True, yield_per=1, max_row_buffer=1), compiled.params).mappings()
            batch, batch_bytes = [], 0
            buffer_limit = min(16 * 1024**2, self.limits.result_bytes_limit)
            try:
                while (row := result.fetchone()) is not None:
                    self._deadline()
                    row = restore_json(row)
                    _, size = _copy_line((row[c.name] for c in compiled.columns), compiled.columns,
                                         buffer_limit, size_only=True)
                    if batch and batch_bytes + size > buffer_limit:
                        yield batch
                        batch, batch_bytes = [], 0
                    batch.append(row)
                    batch_bytes += size
                    if len(batch) >= min(self.limits.batch_size, 1000):
                        yield batch
                        batch, batch_bytes = [], 0
                if batch: yield batch
            finally:
                result.close()

    # -- public entries ----------------------------------------------------------

    def execute(self, request: TextQueryRequest) -> dict[str, Any]:
        return self._execute_plan(self._plan_from_text(request))

    def execute_typed(self, request: TypedQueryRequest) -> dict[str, Any]:
        return self._execute_plan(self._plan_from_typed(request))

    def _execute_plan(self, plan: _Plan) -> dict[str, Any]:
        _scalar_params_ok(plan.params)
        if plan.page_size > self.limits.max_page_size:
            raise QueryError(422, "invalid_page_size", "Page size exceeds configured transport budget")
        if plan.result_storage == "memory":
            return self._execute_memory(plan)
        if plan.result_storage != "persisted":
            raise QueryError(422, "invalid_result_storage", "result_storage must be persisted or memory")
        return self._execute_persisted(plan)

    def _execute_persisted(self, plan: _Plan) -> dict[str, Any]:
        try:
            self._pin()
            # Serialize per-user quota within this transaction. This is a lock,
            # not a mutable session variable; expiry cleanup uses fixed tables.
            self.db.execute(sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
                            {"owner": "unified-query:" + str(self.user.id)})
            now = datetime.now(timezone.utc)
            expired = sa.select(UnifiedQuerySnapshot.id).where(UnifiedQuerySnapshot.expires_at <= now)
            self.db.execute(sa.delete(UnifiedQueryRow).where(UnifiedQueryRow.snapshot_id.in_(expired)))
            self.db.execute(sa.delete(UnifiedQuerySnapshot).where(UnifiedQuerySnapshot.expires_at <= now))
            active = self.db.scalar(sa.select(sa.func.count()).select_from(UnifiedQuerySnapshot).where(
                UnifiedQuerySnapshot.owner_user_id == str(self.user.id), UnifiedQuerySnapshot.expires_at > now))
            if active >= self.limits.max_active_per_user:
                raise QueryError(429, "query_snapshot_quota_exceeded", "Too many unexpired query snapshots")
            self._resolve(plan)
            self._page_scope(plan.page_size)
            auth_fp, schema_fp = self._fingerprints()
            # Compile before streaming to reject invalid types/unknown fields.
            execution_relations = self._execution_relations(plan.pipeline)
            first = next(iter(execution_relations.values()))
            connection_keys = {r.connection_key for r in execution_relations.values()}
            native = len(connection_keys) == 1 and not first.platform
            dialect = first.state.get("dialect", "postgresql") if native else "postgresql"
            compiled = compile_query(plan.pipeline, execution_relations, plan.params, dialect=dialect)
            if not native:
                needed = {ref for ref in refs_in(plan.pipeline.commands)}
                all_columns = not any(c.op in {"fields", "stats"} for c in plan.pipeline.commands)
                execution_relations = {
                    alias: relation if relation.platform else self._stage(
                        alias, relation, set(relation.columns) if all_columns else
                        {ref.name for ref in needed if ref.alias == alias})
                    for alias, relation in execution_relations.items()
                }
                compiled = compile_query(plan.pipeline, execution_relations, plan.params)
            result_limited = False
            if compiled.before_limit is not None:
                count_stmt = sa.select(sa.func.count().label("n")).select_from(
                    compiled.before_limit.order_by(None).subquery())
                if native:
                    count_rows = [row for batch in first.batches(count_stmt, compiled.params, self.limits.batch_size)
                                  for row in batch]
                    if len(count_rows) != 1:
                        raise QueryError(409, "source_incomplete", "Invalid source aggregate completion")
                    n = count_rows[0]["n"]
                else:
                    self._platform_budget()
                    n = self.db.execute(count_stmt, compiled.params).scalar_one()
                limit_cmd = plan.pipeline.commands[-1]
                result_limited = n > limit_cmd.args[0] or (len(limit_cmd.args) > 1 and limit_cmd.args[1] > 0 and n > 0)
            snapshot_id = str(uuid.uuid4())
            result_table = sa.Table("uq_out_" + uuid.uuid4().hex, sa.MetaData(),
                                    sa.Column("snapshot_id", sa.String(36), primary_key=True),
                                    sa.Column("ordinal", sa.BigInteger, primary_key=True),
                                    sa.Column("row", sa.JSON, nullable=False), prefixes=["TEMPORARY"],
                                    postgresql_on_commit="DROP")
            result_table.create(self.db.connection())
            total = 0
            result_bytes = self._physical_size(result_table)
            if result_bytes > self.limits.result_bytes_limit:
                raise QueryError(422, "query_budget_exceeded", "Physical result snapshot disk budget exceeded",
                                 budget="result_bytes_limit", actualBytes=result_bytes)
            result_specs = [ColumnSpec("snapshot_id", "string", False), ColumnSpec("ordinal", "integer", False), ColumnSpec("row", "json", False)]
            exec_params = dict(compiled.params)
            for relation in execution_relations.values():
                if relation.bind_params:
                    exec_params.update(relation.bind_params)
            for batch in self._query_rows(compiled, execution_relations, native):
                def encoded_rows():
                    nonlocal total
                    for row in batch:
                        mapped = row if hasattr(row, "keys") else dict(zip([c.name for c in compiled.columns], row))
                        data = {spec.name: encode_value(mapped[spec.name], spec) for spec in compiled.columns}
                        yield (snapshot_id, total, data)
                        total += 1
                self._copy(result_table, ["snapshot_id", "ordinal", "row"], result_specs, encoded_rows())
                result_bytes = self._physical_size(result_table)
                if result_bytes > self.limits.result_bytes_limit:
                    raise QueryError(422, "query_budget_exceeded", "Physical result snapshot disk budget exceeded",
                                     budget="result_bytes_limit", actualBytes=result_bytes)
                self._deadline()
            source_states = []
            for alias, relation in execution_relations.items():
                state = dict(relation.state)
                if relation.source_group is not None:
                    state["completion"] = dict(relation.source_group.completion)
                    state["snapshot"] = dict(relation.source_group.provenance)
                state["alias"] = alias
                source_states.append(state)
            consistency = ("immutable_versions" if all(s.get("consistency") == "immutable_version" for s in source_states)
                           else "single_source_snapshot" if len(connection_keys) == 1 else "cross_source_non_atomic")
            snapshot = UnifiedQuerySnapshot(
                id=snapshot_id, owner_user_id=str(self.user.id), query_hash=plan.query_hash,
                auth_fingerprint=auth_fp, schema_fingerprint=schema_fp,
                request=plan.stored_request, columns=[spec.output() for spec in compiled.columns],
                source_states=source_states, consistency=consistency,
                plan={"strategy": "source_pushdown" if native else "postgresql_staged_sql" if self.staging_tables else "postgresql_sql",
                      "operators": [cmd.op for cmd in plan.pipeline.commands], "sourceRows": self.source_rows,
                      "stagingBytes": self.stage_bytes}, result_limited=result_limited,
                total_rows=total, result_bytes=result_bytes, page_size=plan.page_size,
                created_at=now, expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.limits.snapshot_ttl_seconds))
            self.db.add(snapshot)
            self.db.flush()
            self._platform_budget()
            self.db.execute(sa.insert(UnifiedQueryRow).from_select(
                ["snapshot_id", "ordinal", "row"], sa.select(sa.literal(snapshot.id), result_table.c.ordinal, result_table.c.row)))
            # Each adapter can recheck scope before results become durable.
            for relation in execution_relations.values():
                if relation.recheck: relation.recheck()
            # Source teardown/transaction completion is part of successful EOF.
            # Do not publish a platform snapshot and only then discover a failed
            # source context exit.
            self.stack.close()
            self._deadline()
            response = self._page(snapshot, 0)
            self.db.commit()
            return response
        except QueryError:
            self.db.rollback()
            raise
        except DBAPIError as exc:
            self.db.rollback()
            control = current_query_control()
            if control is not None and control.cancelled():
                raise QueryError(409, "query_cancelled", "Query execution was cancelled") from None
            raise _query_dbapi_error(exc) from exc
        finally:
            try:
                self.stack.__exit__(*sys.exc_info())
            finally:
                # Includes leaf HTTPException/typing failures: no request may
                # leave partially staged or unpublished rows in the Session.
                if self.db.in_transaction(): self.db.rollback()

    # -- memory execution -----------------------------------------------------

    def _execute_memory(self, plan: _Plan) -> dict[str, Any]:
        control = current_query_control() or QueryControl()
        with query_control_scope(control):
            return self._execute_memory_controlled(plan, control)

    def _execute_memory_controlled(self, plan: _Plan, control: QueryControl) -> dict[str, Any]:
        handle_id = control.query_id or new_handle_id()
        ticket = None
        try:
            self._deadline()
            ticket = REGISTRY.open_ticket(handle_id, str(self.user.id), self.limits,
                                          interrupt=control.cancel)
            budget = MemoryBudget(ticket.reservation_bytes)
            # Platform-local leaves (dataset versions, ontology read port)
            # require the fresh RR read transaction BEFORE resolution, so all
            # metadata resolution and execution share one snapshot; external
            # sources keep their own scopes.
            pinned = self._plan_needs_pin(plan)
            if pinned:
                self._pin_memory()
                self.stack.enter_context(interruptible_connection(self.db.connection()))
            self._resolve(plan)
            if not pinned and self.db.in_transaction():
                # End platform metadata reads before long external source IO.
                self.db.rollback()
            self._check_expectations(plan.origin, self.relations)
            cancel_check = lambda: control.cancelled() or REGISTRY.ticket_cancelled(handle_id)
            self._validate_derived_constraints(plan, memory_budget=budget,
                                              cancel_check=cancel_check)
            execution_relations = self._execution_relations(plan.pipeline)
            self._page_scope(plan.page_size)
            executor = MemoryExecutor(self, plan.pipeline, execution_relations, plan.params,
                                      cancel_check=cancel_check, memory_budget=budget,
                                      derived_specs=plan.derived)
            outer_joins = {cmd.args[1] for cmd in plan.pipeline.commands if cmd.op == "join"}
            result = executor.run({alias: constraint for alias, constraint in plan.join_constraints.items()
                                   if alias in outer_joins})
            # EOF re-authorization on every relation before publication.
            for relation in execution_relations.values():
                if relation.recheck: relation.recheck()
            handle = MemoryHandle(
                id=handle_id, owner_user_id=str(self.user.id), instance_id=INSTANCE_ID,
                columns=[spec.output() for spec in result.columns], rows=result.rows,
                total_rows=len(result.rows), result_limited=result.result_limited,
                page_size=plan.page_size, created_at=datetime.now(timezone.utc),
                expires_at=handle_expiry(self.limits.memory_handle_ttl_seconds),
                auth_fingerprints={a: r.auth_fingerprint for a, r in execution_relations.items()},
                schema_fingerprints={a: r.schema_fingerprint for a, r in execution_relations.items()},
                query_hash=plan.query_hash, request_fingerprint=plan.request_fingerprint,
                request_kind=plan.request_kind, stored_request=plan.stored_request,
                source_states=result.source_states, consistency=result.consistency,
                plan=result.plan, classification=result.classification,
                avg_semantics=result.avg_semantics, bytes=0,
                session_kind=plan.session_kind, session_id=plan.session_id, origin=plan.origin)
            metadata = {key: value for key, value in vars(handle).items() if key != "rows"}
            budget.check_temporary(metadata)
            handle.bytes = resident_size(result.rows) + resident_size(metadata)
            # Source teardown is part of successful EOF, before publication.
            self.stack.close()
            self._deadline()
            REGISTRY.register(handle, self.limits)
            try:
                return self._page_memory(handle, 0)
            except BaseException:
                REGISTRY.cancel(handle_id, str(self.user.id))
                raise
        except QueryError:
            if self.db.in_transaction(): self.db.rollback()
            raise
        except DBAPIError as exc:
            if self.db.in_transaction(): self.db.rollback()
            if control.cancelled():
                raise QueryError(409, "query_cancelled", "Query execution was cancelled") from None
            self._deadline()
            raise _query_dbapi_error(exc) from exc
        finally:
            try:
                self.stack.__exit__(*sys.exc_info())
            finally:
                try:
                    if self.db.in_transaction(): self.db.rollback()
                finally:
                    if ticket is not None:
                        REGISTRY.close_ticket(handle_id)

    def cancel(self, query_id: str) -> dict[str, Any]:
        cancelled = REGISTRY.cancel(query_id, str(self.user.id))
        return {"queryId": query_id, "cancelled": cancelled}

    # -- cursors ----------------------------------------------------------------

    def _sign(self, payload: dict, *, memory: bool = False) -> str:
        raw = _json(payload).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        token = body + "." + hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        return "mem." + token if memory else token

    def _decode(self, token: str) -> dict:
        memory = token.startswith("mem.")
        material = token[4:] if memory else token
        try:
            body, signature = material.rsplit(".", 1)
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            expected = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected): raise ValueError()
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("v") != 1: raise ValueError()
            exp = payload.get("exp")
            if exp is not None and (type(exp) not in {int, float} or not math.isfinite(exp)):
                raise ValueError()
            if memory != (payload.get("mode") == "memory"): raise ValueError()
            return payload
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
            raise QueryError(400, "invalid_cursor", "Invalid query cursor") from exc

    def fetch(self, request: TextQueryCursorRequest, *, session_kind: str | None = None,
              session_id: str | None = None) -> dict:
        payload = self._decode(request.cursor)
        if payload.get("mode") == "memory":
            return self._fetch_memory(payload, session_kind, session_id)
        primary_error = None
        try:
            self._pin()
            snapshot = self.db.get(UnifiedQuerySnapshot, payload.get("id"))
            if snapshot is None or snapshot.owner_user_id != str(self.user.id):
                raise QueryError(404, "snapshot_not_found", "Query snapshot not found")
            if _utc(snapshot.expires_at) <= datetime.now(timezone.utc):
                raise QueryError(410, "cursor_expired", "Query cursor expired")
            if (payload.get("q") != snapshot.query_hash or payload.get("a") != snapshot.auth_fingerprint
                    or payload.get("s") != snapshot.schema_fingerprint or payload.get("size") != snapshot.page_size):
                raise QueryError(400, "invalid_cursor", "Cursor does not match its result snapshot")
            start = payload.get("next")
            if type(start) is not int or start < 0 or start > snapshot.total_rows:
                raise QueryError(400, "invalid_cursor", "Invalid cursor ordinal")
            plan = self._plan_from_stored(snapshot.request)
            self._resolve(plan, recheck_only=True)
            if self._fingerprints() != (snapshot.auth_fingerprint, snapshot.schema_fingerprint):
                raise QueryError(409, "query_context_changed", "Authorization or source schema changed; rerun query")
            self._page_scope(snapshot.page_size)
            for relation in self.relations.values():
                if relation.recheck: relation.recheck()
            return self._page(snapshot, start)
        except DBAPIError as exc:
            primary_error = _query_dbapi_error(exc)
            raise primary_error from exc
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                try:
                    self.db.rollback()
                finally:
                    self.stack.close()
            except BaseException as exc:
                # Cleanup must not replace the page's original failure.
                if primary_error is None:
                    if isinstance(exc, DBAPIError):
                        raise _query_dbapi_error(exc) from exc
                    raise

    def memory_origin(self, token: str) -> dict:
        """Owner/expiry/signature-validated access to a handle's origin request.

        Never executes and never pages: the bridge re-validates ontology
        permissions, re-prepares its plan, and then calls fetch with the same
        session identity. Origin values never leave process memory.
        """
        import copy
        payload = self._decode(token)
        if payload.get("mode") != "memory":
            raise QueryError(400, "invalid_cursor", "Not a memory-mode cursor")
        if payload.get("owner") != str(self.user.id):
            raise QueryError(404, "snapshot_not_found", "Query result not found")
        if payload.get("exp", 0) <= datetime.now(timezone.utc).timestamp():
            raise QueryError(410, "cursor_expired", "Query cursor expired")
        if payload.get("inst") != INSTANCE_ID:
            raise QueryError(410, "result_handle_expired",
                             "Result handle belongs to another instance; rerun the query")
        handle = REGISTRY.get(payload.get("id"))
        if handle is None or handle.id != payload.get("id") or handle.query_hash != payload.get("q"):
            raise QueryError(410, "result_handle_expired",
                             "Result handle expired or was cancelled; rerun the query")
        return {"queryId": handle.id, "queryHash": handle.query_hash,
                "requestFingerprint": handle.request_fingerprint,
                "classification": handle.classification,
                "expiresAt": _utc(handle.expires_at).isoformat(),
                "session": {"kind": handle.session_kind, "id": handle.session_id},
                "origin": copy.deepcopy(handle.origin)}

    def _fetch_memory(self, payload: dict, session_kind: str | None, session_id: str | None) -> dict:
        if payload.get("owner") != str(self.user.id):
            raise QueryError(404, "snapshot_not_found", "Query result not found")
        if payload.get("exp", 0) <= datetime.now(timezone.utc).timestamp():
            raise QueryError(410, "cursor_expired", "Query cursor expired")
        if payload.get("inst") != INSTANCE_ID:
            # A different process instance must never impersonate the snapshot.
            raise QueryError(410, "result_handle_expired",
                             "Result handle belongs to another instance; rerun the query")
        primary_error = None
        try:
            handle = REGISTRY.get(payload.get("id"))
            if handle is None or handle.query_hash != payload.get("q"):
                raise QueryError(410, "result_handle_expired",
                                 "Result handle expired or was cancelled; rerun the query")
            if handle.session_kind is not None or handle.session_id is not None:
                if (session_kind, session_id) != (handle.session_kind, handle.session_id):
                    raise QueryError(403, "cursor_session_mismatch",
                                     "Cursor belongs to a different session; rerun the query")
            start = payload.get("next")
            if type(start) is not int or start < 0 or start > handle.total_rows:
                raise QueryError(400, "invalid_cursor", "Invalid cursor ordinal")
            if payload.get("size") != handle.page_size:
                raise QueryError(400, "invalid_cursor", "Cursor does not match its result handle")
            plan = self._plan_from_stored(handle.stored_request)
            pinned = self._plan_needs_pin(plan)
            if pinned:
                self._pin_memory()
                self.stack.enter_context(interruptible_connection(self.db.connection()))
            self._resolve(plan, recheck_only=True)
            if not pinned and self.db.in_transaction():
                self.db.rollback()
            execution_relations = self._execution_relations(plan.pipeline)
            self._check_expectations(handle.origin, self.relations)
            # Cardinality belongs to the original source snapshots, not the
            # source's current rows. Paging only revalidates metadata access.
            if ({a: r.auth_fingerprint for a, r in execution_relations.items()} != handle.auth_fingerprints or
                    {a: r.schema_fingerprint for a, r in execution_relations.items()} != handle.schema_fingerprints):
                raise QueryError(409, "query_context_changed", "Authorization or source schema changed; rerun query")
            self._page_scope(handle.page_size)
            for relation in execution_relations.values():
                if relation.recheck: relation.recheck()
            return self._page_memory(handle, start)
        except DBAPIError as exc:
            primary_error = _query_dbapi_error(exc)
            raise primary_error from exc
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                try:
                    if self.db.in_transaction(): self.db.rollback()
                finally:
                    self.stack.close()
            except BaseException as exc:
                if primary_error is None:
                    if isinstance(exc, DBAPIError):
                        raise _query_dbapi_error(exc) from exc
                    raise

    # -- paging -----------------------------------------------------------------

    def _page(self, snapshot: UnifiedQuerySnapshot, start: int) -> dict:
        # Transport pages are bounded by BOTH requested rows and complete UTF-8
        # response bytes. A short byte-limited page is not semantic truncation.
        limit = min(16 * 1024**2, self.limits.result_bytes_limit)
        def cursor_at(ordinal):
            return self._sign({"v": 1, "id": snapshot.id, "owner": snapshot.owner_user_id,
                               "q": snapshot.query_hash, "a": snapshot.auth_fingerprint,
                               "s": snapshot.schema_fingerprint, "next": ordinal,
                               "size": snapshot.page_size, "exp": _utc(snapshot.expires_at).timestamp()})
        rows = []
        response = {"queryId": snapshot.id, "schemaVersion": 1, "columns": snapshot.columns,
                    "rows": rows, "sourceStates": snapshot.source_states, "consistency": snapshot.consistency,
                    "exactness": "exact_within_scope", "countsScope": "authorized_query_scope_at_source_snapshots",
                    "totalRows": snapshot.total_rows, "resultLimited": snapshot.result_limited,
                    "page": {"pageSize": snapshot.page_size, "hasMore": False,
                             "cursor": cursor_at(snapshot.total_rows), "byteLimit": limit},
                    "expiresAt": _utc(snapshot.expires_at).isoformat(), "plan": snapshot.plan}
        def check_size(value):
            size = 0
            for part in _json_parts(value):
                size += len(part.encode("utf-8"))
                if size > limit:
                    raise QueryError(422, "query_budget_exceeded", "A result row or metadata exceeds the page byte budget",
                                     budget="page_bytes", limitBytes=limit)
            return size
        # Reserve the full API envelope, longest possible ordinal token and the
        # longer false boolean. Actual response JSON can only be smaller.
        used = check_size({"data": response})
        result = self.db.execute(sa.select(UnifiedQueryRow.row).where(
            UnifiedQueryRow.snapshot_id == snapshot.id, UnifiedQueryRow.ordinal >= start).order_by(
            UnifiedQueryRow.ordinal).limit(snapshot.page_size).execution_options(
            stream_results=True, yield_per=1, max_row_buffer=1))
        try:
            while (record := result.fetchone()) is not None:
                row = record[0]
                size = check_size(row) + (1 if rows else 0)
                if used + size > limit:
                    if rows: break  # This row remains at the next cursor ordinal.
                    raise QueryError(422, "query_budget_exceeded", "A result row exceeds the page byte budget",
                                     budget="page_bytes", limitBytes=limit)
                rows.append(row)
                used += size
        finally:
            result.close()
        more = start + len(rows) < snapshot.total_rows
        response["page"].update(hasMore=more, cursor=cursor_at(start + len(rows)) if more else None)
        return response

    def _page_memory(self, handle: MemoryHandle, start: int) -> dict:
        limit = min(16 * 1024**2, self.limits.result_bytes_limit)
        def cursor_at(ordinal):
            return self._sign({"v": 1, "mode": "memory", "inst": INSTANCE_ID, "id": handle.id,
                               "owner": handle.owner_user_id, "q": handle.query_hash,
                               "a": _hash(handle.auth_fingerprints), "s": _hash(handle.schema_fingerprints),
                               "next": ordinal, "size": handle.page_size,
                               "exp": _utc(handle.expires_at).timestamp()}, memory=True)
        rows: list[dict] = []
        response = {"queryId": handle.id, "schemaVersion": 1, "columns": handle.columns,
                    "rows": rows, "sourceStates": handle.source_states, "consistency": handle.consistency,
                    "exactness": "exact_within_scope", "countsScope": "authorized_query_scope_at_source_snapshots",
                    "totalRows": handle.total_rows, "resultLimited": handle.result_limited,
                    "persistence": {"mode": "ephemeral", "classification": handle.classification},
                    "page": {"pageSize": handle.page_size, "hasMore": False,
                             "cursor": cursor_at(handle.total_rows), "byteLimit": limit},
                    "expiresAt": _utc(handle.expires_at).isoformat(), "plan": handle.plan}

        def check_size(value):
            size = 0
            for part in _json_parts(value):
                size += len(part.encode("utf-8"))
                if size > limit:
                    raise QueryError(422, "query_budget_exceeded",
                                     "A result row or metadata exceeds the page byte budget",
                                     budget="page_bytes", limitBytes=limit)
            return size

        used = check_size({"data": response})
        for row in handle.rows[start:start + handle.page_size]:
            size = check_size(row) + (1 if rows else 0)
            if used + size > limit:
                if rows: break
                raise QueryError(422, "query_budget_exceeded", "A result row exceeds the page byte budget",
                                 budget="page_bytes", limitBytes=limit)
            rows.append(row)
            used += size
        more = start + len(rows) < handle.total_rows
        response["page"].update(hasMore=more, cursor=cursor_at(start + len(rows)) if more else None)
        return response


def _memory_bytes(raw: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*(B|kB|MB|GB|TB)?\s*", raw, re.IGNORECASE)
    if match is None: return None
    units = {"b": 1, "kb": 1024, "mb": 1048576, "gb": 1073741824, "tb": 1099511627776}
    return int(match[1]) * units.get((match[2] or "kb").lower(), 1024)
