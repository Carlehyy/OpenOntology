"""Native capability lane: bounded RR scans and parameter-bound transactions.

This is an explicit execution backend, not a fallback for dbx. dbx discovery is
owned by dbx_adapter. Native reflection is needed for typed SQL compilation and
checks the registered schema before and after each complete read.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy.dialects import mysql
import json
import time
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import Select, visitors
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.selectable import TableClause

from app.auth.permissions import user_has_menu_access
from app.data_channel.connections.models import Connection
from app.data_channel.integrated.contracts import JsonText, digest, fail, normalize_source_value, serialize_source_json, wire_value
from app.data_channel.integrated.models import IntegratedAsset
from app.shared.encryption import decrypt
from app.shared.query_control import current_query_control, interruptible_connection


def authorize(db, user, *, write=False):
    if not user or not user.is_active or not user_has_menu_access(db, user, "data.structured"):
        fail(403, "INTEGRATED_ACCESS_DENIED", "Integrated dataset access denied")
    if write and user.role not in {"admin", "editor"}:
        fail(403, "INTEGRATED_READ_ONLY", "Only admin/editor may change integrated datasets")


def get_asset(db, user, asset_id, *, write=False):
    authorize(db, user, write=write)
    asset = db.execute(sa.select(IntegratedAsset).where(
        IntegratedAsset.id == asset_id).execution_options(
            populate_existing=True)).scalar_one_or_none()
    if asset is None:
        fail(404, "ASSET_NOT_FOUND", "Integrated dataset not found")
    if write and (not asset.writable or (user.role != "admin" and asset.created_by != user.id)):
        fail(403, "SOURCE_WRITE_DENIED", "Source writing must be explicitly enabled by the asset owner")
    return asset


def connection_config(connection):
    if connection.kind not in {"mysql", "postgres"}:
        fail(422, "UNSUPPORTED_SOURCE", "Only MySQL/PostgreSQL integrated sources are supported")
    config = connection.config or {}
    if "_encrypted" in config:
        try:
            config = json.loads(decrypt(config["_encrypted"]))
        except Exception:
            fail(503, "SOURCE_CONFIG_UNAVAILABLE", "Cannot decrypt source configuration")
    return config


def _source_url(connection):
    """Driver-normalized source URL shared by read engines and liveness probes."""
    config = connection_config(connection)
    try:
        if config.get("connection_string"):
            url = make_url(config["connection_string"])
            if connection.kind == "postgres" and url.get_backend_name() in {"postgres", "postgresql"}:
                url = url.set(drivername="postgresql+psycopg2")
            elif connection.kind == "mysql" and url.get_backend_name() == "mysql":
                url = url.set(drivername="mysql+pymysql")
            else:
                fail(422, "SOURCE_DRIVER_MISMATCH", "Connection driver does not match source kind")
            return url
        return URL.create(
            "postgresql+psycopg2" if connection.kind == "postgres" else "mysql+pymysql",
            username=config.get("user", config.get("username")), password=config.get("password"),
            host=config.get("host", "127.0.0.1"),
            port=int(config.get("port", 5432 if connection.kind == "postgres" else 3306)),
            database=config.get("database"),
        )
    except (ValueError, TypeError, sa.exc.ArgumentError):
        # make_url raises ArgumentError for unparseable connection strings; a
        # malformed stored config is a configuration defect, never evidence
        # about physical availability.
        fail(422, "INVALID_SOURCE_CONFIG", "Invalid relational source configuration")


def source_engine(connection):
    try:
        url = _source_url(connection)
        connect_args = {"connect_timeout": 10}
        if connection.kind == "mysql":
            connect_args.update(read_timeout=300, write_timeout=30)
        return sa.create_engine(
            url, poolclass=NullPool, connect_args=connect_args, hide_parameters=True,
            json_deserializer=JsonText, json_serializer=serialize_source_json,
        )
    except (ValueError, TypeError):
        fail(422, "INVALID_SOURCE_CONFIG", "Invalid relational source configuration")


# Bounded liveness probe budget: short enough to fail a cursor page closed
# quickly, long enough to survive a cold TCP handshake on a healthy source.
PROBE_TIMEOUT_SECONDS = 2


def _mysql_canceller(source):
    if source.kind != "mysql":
        return None
    url = _source_url(source)

    def cancel(thread_id):
        engine = sa.create_engine(
            url, poolclass=NullPool, hide_parameters=True,
            connect_args={"connect_timeout": PROBE_TIMEOUT_SECONDS,
                          "read_timeout": PROBE_TIMEOUT_SECONDS,
                          "write_timeout": PROBE_TIMEOUT_SECONDS},
        )
        try:
            with engine.connect() as conn:
                # The id comes from the live driver, never from a request.
                conn.exec_driver_sql(f"KILL QUERY {int(thread_id)}")
        finally:
            engine.dispose()

    return cancel


def probe_source(connection, *, timeout_seconds: int = PROBE_TIMEOUT_SECONDS):
    """One bounded SELECT 1 on a dedicated short-timeout engine.

    Physical availability only: never reflects schema, never reads source
    rows, never opens a transaction. The connection is intentionally fresh
    per probe (a cached or pooled liveness conclusion would be exactly the
    staleness this probe exists to detect); NullPool keeps the cost one
    connection per attempt, and the request query-control interrupt can
    cancel the socket when the client disconnects.

    Budget coverage per attempt: connect_timeout bounds connection
    establishment on both drivers, and MySQL read/write timeouts bound the
    socket client-side. psycopg2 exposes no client read deadline, so the
    PostgreSQL session additionally carries a libpq ``options``
    statement_timeout of the same budget, bounding *server-side* execution
    of the single SELECT 1. This is still not a wall-clock guarantee on the
    whole probe: a server that pauses before running the statement, or a
    blackholed read on an already established socket, is bounded only by
    query-control interruption — the one transport limit that remains.
    """
    try:
        url = _source_url(connection)
        connect_args = {"connect_timeout": timeout_seconds}
        if connection.kind == "mysql":
            connect_args.update(read_timeout=timeout_seconds, write_timeout=timeout_seconds)
        else:
            # postgresql+psycopg2 (connection_config rejects every other kind):
            # no per-query socket deadline exists driver-side, so make the
            # server cancel the probe statement itself once the budget is spent.
            connect_args["options"] = f"-c statement_timeout={timeout_seconds * 1000}"
        engine = sa.create_engine(url, poolclass=NullPool, connect_args=connect_args,
                                  hide_parameters=True)
        try:
            with engine.connect() as conn:
                with interruptible_connection(conn, mysql_cancel=_mysql_canceller(connection)):
                    conn.exec_driver_sql("SELECT 1")
        finally:
            engine.dispose()
    except sa.exc.DBAPIError:
        control = current_query_control()
        if control is not None and control.cancelled():
            fail(409, "query_cancelled", "Source availability probe was cancelled")
        fail(503, "SOURCE_UNAVAILABLE", "Source is not physically reachable; availability fails closed")
    except sa.exc.SQLAlchemyError:
        fail(503, "SOURCE_UNAVAILABLE", "Source is not physically reachable; availability fails closed")


def column_metadata(column, dialect):
    typ = column.type
    if isinstance(typ, sa.Boolean):
        logical = "boolean"
    elif isinstance(typ, sa.BigInteger):
        logical = "integer"
    elif isinstance(typ, sa.Integer):
        logical = "integer"
    elif isinstance(typ, (sa.Float, sa.REAL)):
        logical = "float"
    elif isinstance(typ, sa.Numeric):
        logical = "decimal"
    elif isinstance(typ, sa.DateTime):
        logical = "timestamp"
    elif isinstance(typ, sa.Date):
        logical = "date"
    elif isinstance(typ, sa.Time):
        logical = "time"
    elif isinstance(typ, sa.JSON):
        logical = "json"
    elif isinstance(typ, (sa.LargeBinary, sa.BINARY)):
        logical = "binary"
    elif isinstance(typ, (sa.String, sa.Uuid)):
        logical = "string"
    else:
        # Unsupported arrays/ranges/spatial are not silently downcast.
        fail(422, "UNSUPPORTED_COLUMN_TYPE", f"Unsupported physical type for column {column.name}")
    return {"name": column.name, "type": logical,
            "source_type": str(typ.compile(dialect=dialect)),
            **({"encoding": "json_text"} if logical == "json" else {}),
            "nullable": bool(column.nullable),
            "precision": getattr(typ, "precision", None), "scale": getattr(typ, "scale", None),
            "timezone": (isinstance(typ, mysql.TIMESTAMP) or bool(getattr(typ, "timezone", False))) if logical in {"timestamp", "time"} else None,
            "collation": getattr(typ, "collation", None),
            "length": getattr(typ, "length", None),
            "unsigned": bool(getattr(typ, "unsigned", False)),
            "primary_key": bool(column.primary_key)}


def normalize_row(row, columns):
    columns = columns.c if hasattr(columns, "c") else columns
    result = {}
    for name, value in row.items():
        value = normalize_source_value(value)
        if (name in columns and isinstance(columns[name].type, mysql.TIMESTAMP)
                and isinstance(value, datetime) and value.tzinfo is None):
            # The session is explicitly UTC. MySQL TIMESTAMP is an instant,
            # unlike DATETIME, and PyMySQL does not attach timezone information.
            value = value.replace(tzinfo=timezone.utc)
        result[name] = value
    return result


MAX_SOURCE_BATCH_BYTES = 16 * 1024**2


def _source_row_bytes(row):
    """Measure lossless JSON in small chunks, without building a wide row blob.

    A driver must allocate a single source cell before Python can inspect it.
    We never compound that unavoidable allocation with a row-count-sized driver
    prefetch or an unbounded normalized batch.
    """
    size = 0

    def add(text):
        nonlocal size
        size += len(text.encode("utf-8"))
        if size > MAX_SOURCE_BATCH_BYTES:
            fail(422, "SOURCE_ROW_BYTES_EXCEEDED", "Source row exceeds the 16 MiB stream byte budget")

    def measure(value):
        if isinstance(value, str):
            if len(value) > MAX_SOURCE_BATCH_BYTES - size:
                fail(422, "SOURCE_ROW_BYTES_EXCEEDED", "Source row exceeds the 16 MiB stream byte budget")
            add('"')
            for start in range(0, len(value), 16384):
                add(json.dumps(value[start:start + 16384], ensure_ascii=False)[1:-1])
            add('"')
        elif isinstance(value, dict):
            add("{")
            for index, (key, child) in enumerate(value.items()):
                if index:
                    add(",")
                measure(str(key))
                add(":")
                measure(child)
            add("}")
        elif isinstance(value, (list, tuple)):
            add("[")
            for index, child in enumerate(value):
                if index:
                    add(",")
                measure(child)
            add("]")
        else:
            if isinstance(value, (bytes, bytearray, memoryview)) and len(value) > MAX_SOURCE_BATCH_BYTES:
                fail(422, "SOURCE_ROW_BYTES_EXCEEDED", "Source row exceeds the 16 MiB stream byte budget")
            encoded = wire_value(value)
            if encoded is not value:
                measure(encoded)
            else:
                add(json.dumps(value, ensure_ascii=False, allow_nan=False))

    measure(row)
    return size


def reflect(conn, asset):
    # SQLAlchemy quotes these identifiers; no identifier is interpolated into SQL.
    table = sa.Table(asset.table_name, sa.MetaData(), schema=asset.schema_name, autoload_with=conn)
    columns = [column_metadata(c, conn.dialect) for c in table.columns]
    schema = {"columns": columns, "primary_key": [c.name for c in table.primary_key]}
    return table, schema, digest(schema)


def check_transactional_table(conn, asset):
    if conn.dialect.name == "mysql":
        kind = conn.execute(sa.text(
            "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table"
        ), {"schema": asset.schema_name, "table": asset.table_name}).scalar_one_or_none()
        if not kind or kind.upper() != "INNODB":
            fail(422, "NONTRANSACTIONAL_SOURCE", "Strong snapshots/writes require a MySQL InnoDB base table")


def resolve_metadata(db, user, asset_ids):
    authorize(db, user)
    result = {}
    for asset_id in dict.fromkeys(asset_ids):
        asset = get_asset(db, user, asset_id)
        # Refresh past the session identity map: a credential rotation committed
        # by another session must never serve stale credentials or a stale
        # auth fingerprint from this session's cached Connection row.
        connection = db.execute(
            sa.select(Connection)
            .where(Connection.id == asset.connection_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if connection is None:
            fail(404, "CONNECTION_NOT_FOUND", "Source connection not found")
        result[asset_id] = {
            "asset_id": asset.id, "dataset_id": asset.dataset_id,
            "connection_id": asset.connection_id,
            # "inactive" is the creation/config-rotation default (untested),
            # "active" a passed test, "error" a failed test.
            "connection_status": connection.status,
            "dialect": "postgresql" if connection.kind == "postgres" else "mysql",
            "columns": asset.schema_json["columns"],
            "primary_key": asset.schema_json["primary_key"],
            "schema_fingerprint": asset.schema_fingerprint,
            "auth_fingerprint": digest({
                "user": user.id, "role": user.role, "asset": asset.id,
                # Source identity revision: rotating the connection config or
                # changing its lifecycle state must invalidate in-flight query
                # cursors and result handles bound to the previous context.
                # Only a one-way digest participates; raw config values
                # (plaintext or ciphertext) never leave here and are never
                # logged.
                "source_config_revision": digest(connection.config or {}),
                "source_lifecycle_status": connection.status,
            }),
        }
    return result


class NativeSourceSession:
    def __init__(self, conn, assets, metadata, snapshot, *, deadline=None):
        self.connection = conn
        self._deadline = deadline
        self.assets = assets
        self.meta = metadata
        self.tables = {}
        self.provenance = snapshot
        self.completion = {"complete": False, "row_count": 0, "snapshot_id": snapshot["id"]}
        self._busy = False
        self._counted_statement = None
        self._counted_params = None
        self._counted_rows = None
        for asset in assets:
            self._remaining(configure=True)
            check_transactional_table(conn, asset)
            table, _, fingerprint = reflect(conn, asset)
            self._remaining()
            if fingerprint != asset.schema_fingerprint:
                fail(409, "SOURCE_SCHEMA_DRIFT", "Source schema changed; explicitly refresh the asset schema")
            self.tables[asset.id] = table
            self.meta[asset.id].update(as_of=snapshot["started_at"], consistency="per_source_repeatable_read")

    def _validate_select(self, statement):
        if not isinstance(statement, Select):
            fail(422, "READ_SELECT_REQUIRED", "Source read port accepts only a typed SELECT")
        allowed = {t for t in self.tables.values()}
        for node in visitors.iterate(statement):
            if isinstance(node, TextClause) or (isinstance(node, ColumnClause) and node.is_literal and node.name != "*"):
                fail(422, "RAW_SQL_DENIED", "Raw SQL is not accepted by the source read port")
            if isinstance(node, TableClause) and node not in allowed:
                fail(403, "UNRESOLVED_SOURCE_TABLE", "SELECT references an unauthorized table")

    def _remaining(self, *, configure=False):
        control = current_query_control()
        if control is not None and control.cancelled():
            fail(409, "query_cancelled", "Source query was cancelled")
        if self._deadline is None:
            return
        remaining = int((self._deadline - time.monotonic()) * 1000)
        if remaining < 1:
            fail(422, "QUERY_BUDGET_EXCEEDED", "Source read exceeded the total query time budget")
        if configure:
            if self.connection.dialect.name == "postgresql":
                self.connection.execute(sa.text("SELECT set_config('statement_timeout', :timeout, true)"),
                                        {"timeout": str(remaining)})
            elif self.connection.dialect.name == "mysql":
                self.connection.execute(sa.text("SET SESSION MAX_EXECUTION_TIME = :timeout"),
                                        {"timeout": remaining})

    def expected_rows(self, statement, params=None):
        if self._busy:
            fail(409, "SOURCE_CURSOR_BUSY", "Exhaust the current source stream before counting another")
        self._validate_select(statement)
        self._remaining(configure=True)
        # Count the compiled result (including GROUP/LIMIT), not the base table.
        result = int(self.connection.execute(
            sa.select(sa.func.count()).select_from(statement.order_by(None).subquery()), params or {}).scalar_one())
        self._remaining()
        self._counted_statement, self._counted_params, self._counted_rows = statement, dict(params or {}), result
        return result

    def iter_batches(self, statement, params=None, *, batch_size=1000):
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            fail(422, "INVALID_BATCH_SIZE", "Batch size must be between 1 and 1000")
        self._validate_select(statement)
        if self._busy:
            fail(409, "SOURCE_CURSOR_BUSY", "Exhaust the current source stream before starting another")
        # Reset proof before any operation can fail. Reuse only an identical
        # statement object/parameters counted within this same read-only scope.
        self.completion = {"complete": False, "row_count": 0, "snapshot_id": self.provenance["id"]}
        if self._counted_statement is statement and self._counted_params == (params or {}):
            expected = self._counted_rows
        else:
            expected = self.expected_rows(statement, params)
        self.completion["expected_rows"] = expected
        self._busy = True
        cursor = None
        try:
            self._remaining(configure=True)
            cursor = self.connection.execution_options(
                stream_results=True, yield_per=1, max_row_buffer=1,
            ).execute(statement, params or {})
            rows = cursor.mappings()
            selected = dict(zip(statement.selected_columns.keys(), statement.selected_columns))
            batch, batch_bytes = [], 0
            while True:
                self._remaining()
                raw = rows.fetchone()
                self._remaining()
                if raw is None:
                    break
                row = normalize_row(raw, selected)
                row_bytes = _source_row_bytes(row)
                if batch and batch_bytes + row_bytes > MAX_SOURCE_BATCH_BYTES:
                    self.completion["row_count"] += len(batch)
                    yield batch
                    batch, batch_bytes = [], 0
                batch.append(row)
                batch_bytes += row_bytes
                if len(batch) == batch_size:
                    self.completion["row_count"] += len(batch)
                    yield batch
                    batch, batch_bytes = [], 0
            if batch:
                self.completion["row_count"] += len(batch)
                yield batch
            cursor.close()
            cursor = None
            # Reflection/SET must use an ordinary cursor after a PG server-side
            # stream, never a named cursor for a non-SELECT command.
            self.connection.execution_options(stream_results=False, yield_per=None)
            self._remaining(configure=True)
            # No DDL drift may be silently blessed at the completeness boundary.
            for asset in self.assets:
                _, _, fingerprint = reflect(self.connection, asset)
                if fingerprint != asset.schema_fingerprint:
                    fail(409, "SOURCE_SCHEMA_DRIFT", "Source schema changed while reading")
            if self.completion["row_count"] != expected:
                fail(409, "INCOMPLETE_SOURCE_READ", "Source row count did not match the snapshot count")
            self._remaining()
            self.completion["complete"] = True
        finally:
            if cursor is not None:
                cursor.close()
            self.connection.execution_options(stream_results=False, yield_per=None)
            self._busy = False


@contextmanager
def open_read_scope(db, user, asset_ids, *, statement_timeout_ms=300000):
    if isinstance(statement_timeout_ms, bool) or not isinstance(statement_timeout_ms, int) or not 1 <= statement_timeout_ms <= 2**31 - 1:
        fail(422, "INVALID_SOURCE_TIMEOUT", "Source statement timeout must be a positive signed 32-bit millisecond value")
    deadline = time.monotonic() + statement_timeout_ms / 1000
    meta = resolve_metadata(db, user, asset_ids)
    if not meta:
        fail(422, "EMPTY_SOURCE_SCOPE", "At least one source asset is required")
    if len({m["connection_id"] for m in meta.values()}) != 1:
        fail(422, "MIXED_SOURCE_SCOPE", "A read scope must use exactly one connection")
    assets = [get_asset(db, user, i) for i in meta]
    source = db.get(Connection, assets[0].connection_id)
    engine = source_engine(source)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn, interruptible_connection(
            conn, mysql_cancel=_mysql_canceller(source),
        ):
            with conn.begin():
                if conn.dialect.name == "postgresql":
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    conn.execute(sa.text("SELECT set_config('TimeZone', 'UTC', true)"))
                    conn.execute(sa.text("SELECT set_config('statement_timeout', :timeout, true)"),
                                 {"timeout": str(statement_timeout_ms)})
                    conn.execute(sa.text("SELECT set_config('lock_timeout', :timeout, true)"),
                                 {"timeout": str(min(statement_timeout_ms, 10000))})
                elif conn.dialect.name == "mysql":
                    # SET TRANSACTION must be issued before the transaction's
                    # first read; InnoDB is independently verified below.
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    conn.exec_driver_sql("SET SESSION time_zone = '+00:00'")
                    conn.execute(sa.text("SET SESSION MAX_EXECUTION_TIME = :timeout"),
                                 {"timeout": statement_timeout_ms})
                snapshot = {"id": str(uuid.uuid4()), "kind": "repeatable_read",
                            "consistent": True, "started_at": datetime.now(timezone.utc).isoformat(),
                            "connection_id": source.id, "execution_backend": "native"}
                scope = NativeSourceSession(conn, assets, meta, snapshot, deadline=deadline)
                yield scope
    except sa.exc.DBAPIError as exc:
        control = current_query_control()
        if control is not None and control.cancelled():
            fail(409, "query_cancelled", "Source query was cancelled")
        original = exc.orig
        code = getattr(original, "pgcode", None)
        mysql_code = original.args[0] if getattr(original, "args", ()) else None
        if code == "57014" or mysql_code in {3024, 1969} or time.monotonic() >= deadline:
            fail(422, "QUERY_BUDGET_EXCEEDED", "Source read exceeded the total query time budget")
        fail(503, "SOURCE_UNAVAILABLE", "Source read was unavailable or interrupted")
    finally:
        engine.dispose()


class SnapshotStream:
    def __init__(self, scope, asset, batch_size):
        self.scope = scope
        table = scope.tables[asset.id]
        self.statement = sa.select(table)
        if table.primary_key.columns:
            self.statement = self.statement.order_by(*table.primary_key.columns)
        self.batch_size = batch_size
        self._started = False
        self.descriptor = {
            "asset_id": asset.id, "dataset_id": asset.dataset_id,
            "connection_id": asset.connection_id, "resource": f"{asset.schema_name}.{asset.table_name}",
            "schema": asset.schema_json["columns"], "primary_key": asset.schema_json["primary_key"],
            "schema_fingerprint": asset.schema_fingerprint,
            "expected_rows": scope.expected_rows(self.statement), "snapshot": scope.provenance,
        }

    @property
    def completion(self):
        return {**self.scope.completion, "schema_fingerprint": self.descriptor["schema_fingerprint"]}

    def iter_batches(self):
        if self._started:
            fail(409, "SNAPSHOT_ALREADY_CONSUMED", "A snapshot stream can only be consumed once; no resume is supported")
        self._started = True
        yield from self.scope.iter_batches(self.statement, batch_size=self.batch_size)


@contextmanager
def open_snapshot(db, asset_id, *, user, batch_size=1000):
    with open_read_scope(db, user, [asset_id]) as scope:
        yield SnapshotStream(scope, get_asset(db, user, asset_id), batch_size)
