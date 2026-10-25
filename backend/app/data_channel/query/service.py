"""Bounded execution on PostgreSQL and durable ordinal pagination.

Only compiler-produced SQLAlchemy selects reach a source. Cross-source inputs
are copied in bounded batches to TEMP tables; PostgreSQL performs joins/sorts/
aggregates and may spill under an enforced disk budget. No Python table joins.
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
import time
import uuid
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.data_channel.integrated.contracts import JsonText

from .compiler import compile_query, refs_in
from .contracts import (ColumnSpec, TextQueryCursorRequest, QueryError, QueryLimits,
                        TextQueryRequest, Relation, provider_for)
from .models import UnifiedQueryRow, UnifiedQuerySnapshot
from .parser import Pipeline, parse


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def encode_value(value: Any, spec: ColumnSpec) -> Any:
    if value is None: return None
    if spec.type == "json":
        # Preserve database JSON text, including arbitrary-precision JSON numbers.
        # SQL NULL is None, whereas JSON literal null is the text "null".
        return str(value) if isinstance(value, JsonText) else _json(value)
    if spec.type == "integer":
        if isinstance(value, bool) or (isinstance(value, Decimal) and value != value.to_integral_value()):
            raise QueryError(422, "invalid_typed_value", f"Non-integer value in {spec.name}")
        return str(int(value))
    if spec.type == "decimal":
        if isinstance(value, float):
            raise QueryError(422, "lossy_numeric_value", "Decimal results must not pass through float")
        number = Decimal(value)
        if not number.is_finite():
            raise QueryError(422, "invalid_typed_value", "Non-finite decimal result")
        return format(number, "f")
    if spec.type == "float":
        if not math.isfinite(float(value)):
            raise QueryError(422, "invalid_typed_value", "Non-finite float result")
        return float(value)
    if spec.type in {"date", "timestamp", "time"}:
        return value.isoformat() if isinstance(value, (datetime, date, dt_time)) else str(value)
    if spec.type == "binary": return base64.b64encode(bytes(value)).decode("ascii")
    if spec.type == "string":
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="strict")
        return str(value)
    return value


def _json_parts(value: Any):
    """JSON fragments without allocating an encoded copy of a giant string."""
    if isinstance(value, str):
        yield '"'
        for start in range(0, len(value), 16384):
            yield _json(value[start:start + 16384])[1:-1]
        yield '"'
    elif isinstance(value, dict):
        yield "{"
        # JSON object order is not semantic; do not allocate a sorted key list
        # for a driver-decoded large object just to enforce the byte budget.
        for index, (key, item) in enumerate(value.items()):
            if index: yield ","
            yield from _json_parts(key)
            yield ":"
            yield from _json_parts(item)
        yield "}"
    elif isinstance(value, (list, tuple)):
        yield "["
        for index, item in enumerate(value):
            if index: yield ","
            yield from _json_parts(item)
        yield "]"
    else:
        yield _json(value)


def _copy_parts(value: Any, spec: ColumnSpec):
    if value is None:
        yield r"\N"
        return
    if spec.type == "json":
        # Native JSON values already carry their exact text; do not quote the
        # document as a JSON string or parse/re-encode its decimal numbers.
        parts = (value[start:start + 16384] for start in range(0, len(value), 16384)) if isinstance(value, JsonText) else _json_parts(value)
    elif spec.type == "binary":
        def binary_parts():
            yield "\\x"
            view = memoryview(value)
            for start in range(0, len(view), 16384):
                yield view[start:start + 16384].hex()
        parts = binary_parts()
    else:
        if isinstance(value, (datetime, date, dt_time)): value = value.isoformat()
        elif isinstance(value, bool): value = "true" if value else "false"
        elif isinstance(value, Decimal) and not value.is_finite():
            raise QueryError(422, "invalid_typed_value", "Non-finite decimal input")
        elif isinstance(value, float) and not math.isfinite(value):
            raise QueryError(422, "invalid_typed_value", "Non-finite float input")
        raw_text = str(value)
        parts = (raw_text[start:start + 16384] for start in range(0, len(raw_text), 16384))
    for text in parts:
        if "\x00" in text:
            raise QueryError(422, "invalid_typed_value", "PostgreSQL text cannot contain NUL")
        yield text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _copy_cell(value: Any, spec: ColumnSpec) -> str:
    return "".join(_copy_parts(value, spec))


def _copy_line(values, specs: list[ColumnSpec], limit: int, *, size_only: bool = False) -> tuple[str, int]:
    """Bound UTF-8 COPY bytes while encoding, not after a full-row allocation."""
    output = None if size_only else io.StringIO()
    size = 0
    def append(part):
        nonlocal size
        size += len(part.encode("utf-8"))
        if size > limit:
            raise QueryError(422, "query_budget_exceeded", "A query row exceeds the bounded COPY buffer",
                             budget="copy_buffer_bytes", limitBytes=limit)
        if output is not None: output.write(part)
    for index, (value, spec) in enumerate(zip(values, specs)):
        if index: append("\t")
        for part in _copy_parts(value, spec): append(part)
    append("\n")
    return output.getvalue() if output is not None else "", size


def _query_dbapi_error(exc: DBAPIError) -> QueryError:
    code = getattr(exc.orig, "pgcode", None)
    if code in {"40001", "40P01"}:
        return QueryError(409, "query_retry_required", "Concurrent query state changed; explicitly rerun the query")
    if code in {"57014", "53400", "53100", "53200", "54000"}:
        return QueryError(422, "query_budget_exceeded", "Database resource budget exhausted")
    if code and code.startswith("22"):
        return QueryError(422, "invalid_typed_value", "A value in the query scope violates its declared type")
    return QueryError(503, "query_execution_failed", "Query execution failed; no partial snapshot was published")


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

    def _resolve(self, request: TextQueryRequest, pipeline: Pipeline):
        names = {name for name, _ in pipeline.sources}
        if set(request.bindings) != names:
            raise QueryError(422, "invalid_bindings", "Bindings must exactly match pipeline source names")
        for name, alias in pipeline.sources:
            binding = request.bindings[name]
            required = {"integrated": ("asset_id",), "dataset": ("dataset_id", "version_id"),
                        "ontology": ("ontology_id", "release_id", "object_type_id")}[binding.kind]
            allowed = {"kind", *required}
            values = binding.model_dump(exclude_none=True)
            if not all(values.get(key) for key in required) or set(values) != allowed:
                raise QueryError(422, "invalid_binding", f"Invalid {binding.kind} binding fields")
        for name, alias in pipeline.sources:
            binding = request.bindings[name]
            if binding.kind == "integrated":
                # Resolve all aliases together so same-connection joins share a
                # single source transaction and a single RR snapshot.
                if not hasattr(self, "_integrated_relations"):
                    from .providers import resolve_integrated
                    integrated = [(a, request.bindings[n]) for n, a in pipeline.sources
                                  if request.bindings[n].kind == "integrated"]
                    self._integrated_relations = resolve_integrated(
                        self.db, self.user, integrated, self.limits, self.stack)
                relation = self._integrated_relations[alias]
            elif binding.kind == "dataset":
                from .providers import resolve_dataset
                relation = resolve_dataset(self.db, self.user, alias, binding, self.limits)
            else:
                relation = provider_for(binding.kind)(self.db, self.user, alias, binding, self.limits)
            if relation.close: self.stack.callback(relation.close)
            if relation.state.get("completeness") != "complete":
                raise QueryError(409, "source_incomplete", "Query requires complete source scope", alias=alias)
            if not relation.auth_fingerprint or not relation.schema_fingerprint:
                raise QueryError(503, "query_provider_invalid", "Provider must pin authorization and schema")
            self.relations[alias] = relation
        self._deadline()

    def _page_scope(self, page_size: int):
        for alias, relation in self.relations.items():
            cap = relation.state.get("maxPageSize")
            if cap is not None:
                if type(cap) is not int or cap < 1:
                    raise QueryError(503, "query_provider_invalid", "Provider returned an invalid output quota")
                if page_size > cap:
                    raise QueryError(422, "query_page_size_exceeds_scope", "Page size exceeds the authorized source output quota",
                                     alias=alias, maxPageSize=cap)

    def _fingerprints(self):
        return (_hash({a: r.auth_fingerprint for a, r in self.relations.items()}),
                _hash({a: r.schema_fingerprint for a, r in self.relations.items()}))

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

    def execute(self, request: TextQueryRequest) -> dict[str, Any]:
        pipeline = parse(request.query)
        if any(value is not None and type(value) not in {str, int, float, bool}
               for value in request.params.values()):
            raise QueryError(422, "invalid_parameter_type", "Parameters must be JSON scalars, not arrays/objects")
        try:
            parameter_bytes = len(_json(request.params).encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise QueryError(422, "invalid_parameter_type", "Parameters must be finite JSON scalars") from exc
        if parameter_bytes > 1024**2:
            raise QueryError(422, "query_budget_exceeded", "Query parameters exceed 1 MiB", budget="parameter_bytes")
        if request.page_size > self.limits.max_page_size:
            raise QueryError(422, "invalid_page_size", "Page size exceeds configured transport budget")
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
            self._resolve(request, pipeline)
            self._page_scope(request.page_size)
            auth_fp, schema_fp = self._fingerprints()
            # Compile before streaming to reject invalid types/unknown fields.
            first = next(iter(self.relations.values()))
            connection_keys = {r.connection_key for r in self.relations.values()}
            native = len(connection_keys) == 1 and not first.platform
            dialect = first.state.get("dialect", "postgresql") if native else "postgresql"
            compiled = compile_query(pipeline, self.relations, request.params, dialect=dialect)
            execution_relations = self.relations
            if not native:
                needed = {ref for ref in refs_in(pipeline.commands)}
                all_columns = not any(c.op in {"fields", "stats"} for c in pipeline.commands)
                execution_relations = {
                    alias: relation if relation.platform else self._stage(
                        alias, relation, set(relation.columns) if all_columns else
                        {ref.name for ref in needed if ref.alias == alias})
                    for alias, relation in self.relations.items()
                }
                compiled = compile_query(pipeline, execution_relations, request.params)
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
                limit_cmd = pipeline.commands[-1]
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
            for relation in self.relations.values():
                state = dict(relation.state)
                if relation.source_group is not None:
                    state["completion"] = dict(relation.source_group.completion)
                    state["snapshot"] = dict(relation.source_group.provenance)
                source_states.append(state)
            consistency = ("immutable_versions" if all(s.get("consistency") == "immutable_version" for s in source_states)
                           else "single_source_snapshot" if len(connection_keys) == 1 else "cross_source_non_atomic")
            snapshot = UnifiedQuerySnapshot(
                id=snapshot_id, owner_user_id=str(self.user.id), query_hash=_hash(request.model_dump(mode="json", by_alias=True)),
                auth_fingerprint=auth_fp, schema_fingerprint=schema_fp,
                request=request.model_dump(mode="json", by_alias=True), columns=[spec.output() for spec in compiled.columns],
                source_states=source_states, consistency=consistency,
                plan={"strategy": "source_pushdown" if native else "postgresql_staged_sql" if self.staging_tables else "postgresql_sql",
                      "operators": [cmd.op for cmd in pipeline.commands], "sourceRows": self.source_rows,
                      "stagingBytes": self.stage_bytes}, result_limited=result_limited,
                total_rows=total, result_bytes=result_bytes, page_size=request.page_size,
                created_at=now, expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.limits.snapshot_ttl_seconds))
            self.db.add(snapshot)
            self.db.flush()
            self._platform_budget()
            self.db.execute(sa.insert(UnifiedQueryRow).from_select(
                ["snapshot_id", "ordinal", "row"], sa.select(sa.literal(snapshot.id), result_table.c.ordinal, result_table.c.row)))
            # Each adapter can recheck scope before results become durable.
            for relation in self.relations.values():
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
            raise _query_dbapi_error(exc) from exc
        finally:
            try:
                self.stack.close()
            finally:
                # Includes leaf HTTPException/typing failures: no request may
                # leave partially staged or unpublished rows in the Session.
                if self.db.in_transaction(): self.db.rollback()

    def _sign(self, payload: dict) -> str:
        raw = _json(payload).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return body + "." + hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()

    def _decode(self, token: str) -> dict:
        try:
            body, signature = token.rsplit(".", 1)
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            expected = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected): raise ValueError()
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("v") != 1: raise ValueError()
            exp = payload.get("exp")
            if exp is not None and (type(exp) not in {int, float} or not math.isfinite(exp)):
                raise ValueError()
            return payload
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
            raise QueryError(400, "invalid_cursor", "Invalid query cursor") from exc

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
        def encoded_size(value):
            size = 0
            for part in _json_parts(value):
                size += len(part.encode("utf-8"))
                if size > limit:
                    raise QueryError(422, "query_budget_exceeded", "A result row or metadata exceeds the page byte budget",
                                     budget="page_bytes", limitBytes=limit)
            return size
        # Reserve the full API envelope, longest possible ordinal token and the
        # longer false boolean. Actual response JSON can only be smaller.
        used = encoded_size({"data": response})
        result = self.db.execute(sa.select(UnifiedQueryRow.row).where(
            UnifiedQueryRow.snapshot_id == snapshot.id, UnifiedQueryRow.ordinal >= start).order_by(
            UnifiedQueryRow.ordinal).limit(snapshot.page_size).execution_options(
                stream_results=True, yield_per=1, max_row_buffer=1))
        try:
            while (record := result.fetchone()) is not None:
                row = record[0]
                size = encoded_size(row) + (1 if rows else 0)
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

    def fetch(self, request: TextQueryCursorRequest) -> dict:
        payload = self._decode(request.cursor)
        if payload.get("owner") != str(self.user.id):
            raise QueryError(404, "snapshot_not_found", "Query snapshot not found")
        if payload.get("exp", 0) <= datetime.now(timezone.utc).timestamp():
            raise QueryError(410, "cursor_expired", "Query cursor expired")
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
            original = TextQueryRequest.model_validate(snapshot.request)
            self._resolve(original, parse(original.query))
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


def _memory_bytes(raw: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*(B|kB|MB|GB|TB)?\s*", raw, re.IGNORECASE)
    if match is None: return None
    units = {"b": 1, "kb": 1024, "mb": 1048576, "gb": 1073741824, "tb": 1099511627776}
    return int(match[1]) * units.get((match[2] or "kb").lower(), 1024)
