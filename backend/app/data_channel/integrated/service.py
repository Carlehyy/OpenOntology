"""Application policy for live assets and durable, non-replayed source writes."""
from __future__ import annotations

import base64
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import math
import json
import re
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects import mysql

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset, DatasetVersion, IntegratedSyncRun
from app.data_channel.integrated import fields
from app.data_channel.integrated.contracts import (
    DBX_COMMIT, DBX_VERSION, JsonText, RegisterAsset, RowMutation, SourceReference,
    SourceSchemaRequest, digest, fail, row_token, wire_value,
)
from app.data_channel.integrated.fields import FieldConfigError
from app.data_channel.integrated.models import IntegratedAsset, IntegratedAssetFieldConfig, IntegratedMutation
from app.data_channel.integrated.native import (
    authorize, check_transactional_table, get_asset, open_read_scope, open_snapshot,
    normalize_row, reflect, resolve_metadata, source_engine,
)
from app.shared.encryption import encrypt


def dbx():
    from app.data_channel.integrated.dbx_adapter import DbxMetadataAdapter
    return DbxMetadataAdapter()


# Logical families used only to reject contradictory dbx/native metadata.  The
# native reflection remains the authoritative typed DDL/codec source; dbx is the
# authoritative discovery source (table existence, columns, PK, nullability).
_DBX_TYPE_FAMILIES = {
    "int": "integer", "integer": "integer", "smallint": "integer",
    "mediumint": "integer", "bigint": "integer", "tinyint": "integer",
    "int2": "integer", "int4": "integer", "int8": "integer",
    "serial": "integer", "smallserial": "integer", "bigserial": "integer",
    "decimal": "decimal", "numeric": "decimal", "dec": "decimal",
    "fixed": "decimal", "number": "decimal", "money": "decimal",
    "smallmoney": "decimal",
    "float": "float", "float4": "float", "float8": "float",
    "real": "float", "double": "float",
    "bool": "boolean", "boolean": "boolean",
    "char": "string", "character": "string", "bpchar": "string",
    "varchar": "string", "nvarchar": "string", "nchar": "string",
    "text": "string", "tinytext": "string", "mediumtext": "string",
    "longtext": "string", "citext": "string", "uuid": "string",
    "enum": "string", "set": "string",
    "date": "date",
    "timestamp": "timestamp", "timestamptz": "timestamp",
    "datetime": "timestamp",
    "time": "time", "timetz": "time",
    "json": "json", "jsonb": "json",
    "binary": "binary", "varbinary": "binary", "blob": "binary",
    "tinyblob": "binary", "mediumblob": "binary", "longblob": "binary",
    "bytea": "binary", "image": "binary",
}


def _dbx_type_family(data_type: str) -> str | None:
    base = re.split(r"[\s(\[]", data_type.strip().lower(), maxsplit=1)[0]
    return _DBX_TYPE_FAMILIES.get(base)


def _metadata_mismatch(message: str) -> None:
    fail(409, "DBX_METADATA_MISMATCH", message)


def _optional_int(value, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        fail(503, "DBX_METADATA_INVALID", f"dbx returned an invalid {label}")
    return value


def verify_dbx_metadata(evidence, schema) -> None:
    """Fail closed unless dbx discovery agrees with the reflected source schema.

    Names, primary key and nullability must agree exactly.  Contradictory type
    families and any declared precision/scale/length/collation disagreement are
    rejected; unknown dbx type spellings are left to the native typed lane.
    """
    if not isinstance(evidence, dict) or evidence.get("backend") != "dbx-web-http":
        fail(503, "DBX_METADATA_INVALID", "dbx metadata did not come from the pinned dbx backend")
    if evidence.get("commit") != DBX_COMMIT or evidence.get("version") != DBX_VERSION:
        fail(503, "DBX_METADATA_INVALID", "dbx metadata is not bound to the pinned release")
    columns = evidence.get("columns")
    if not isinstance(columns, list) or not columns:
        fail(503, "DBX_METADATA_INCOMPLETE", "dbx did not return authoritative column metadata")
    discovered = {}
    for column in columns:
        if not isinstance(column, dict):
            fail(503, "DBX_METADATA_INVALID", "dbx returned invalid column metadata")
        name, data_type = column.get("name"), column.get("data_type")
        nullable, primary = column.get("nullable"), column.get("primary_key")
        if (not isinstance(name, str) or not name or name in discovered
                or not isinstance(data_type, str) or not data_type
                or not isinstance(nullable, bool) or not isinstance(primary, bool)):
            fail(503, "DBX_METADATA_INVALID", "dbx returned invalid column metadata")
        discovered[name] = column
    discovered_primary_key = evidence.get("primary_key")
    if (not isinstance(discovered_primary_key, list)
            or not all(isinstance(name, str) for name in discovered_primary_key)
            or len(set(discovered_primary_key)) != len(discovered_primary_key)
            or not set(discovered_primary_key) <= set(discovered)):
        fail(503, "DBX_METADATA_INVALID", "dbx returned an invalid primary-key description")
    reflected = {column["name"]: column for column in schema.get("columns", [])}
    if set(discovered) != set(reflected):
        _metadata_mismatch("dbx column discovery disagrees with the reflected source schema")
    if set(discovered_primary_key) != set(schema.get("primary_key") or []):
        _metadata_mismatch("dbx primary-key discovery disagrees with the reflected source schema")
    for name, reflected_column in reflected.items():
        discovered_column = discovered[name]
        if bool(discovered_column["nullable"]) != bool(reflected_column.get("nullable")):
            _metadata_mismatch(f"dbx nullability disagrees for column {name}")
        family = _dbx_type_family(discovered_column["data_type"])
        native_type = reflected_column.get("type")
        # MySQL reports BOOLEAN as tinyint(1); the native dialect may reflect
        # either BOOLEAN or TINYINT.  Both are compatible with that discovery
        # spelling, and the native lane still owns the actual codec type.
        if (family == "integer" and native_type == "boolean"
                and discovered_column["data_type"].strip().lower().startswith(("tinyint(1)", "bit(1)"))):
            family = "boolean"
        if family is not None and family != native_type:
            _metadata_mismatch(f"dbx type family disagrees for column {name}")
        for key in ("precision", "scale", "length"):
            dbx_value = _optional_int(discovered_column.get(key), key)
            native_value = _optional_int(reflected_column.get(key), key)
            if dbx_value is not None and native_value is not None and dbx_value != native_value:
                _metadata_mismatch(f"dbx {key} disagrees for column {name}")
        dbx_collation = discovered_column.get("collation")
        native_collation = reflected_column.get("collation")
        if (isinstance(dbx_collation, str) and dbx_collation.strip()
                and isinstance(native_collation, str) and native_collation.strip()
                and dbx_collation.strip().casefold() != native_collation.strip().casefold()):
            _metadata_mismatch(f"dbx collation disagrees for column {name}")


_UNSET = object()


def raw_config_of(asset):
    """Raw stored field config of an asset (relationship read), or None."""
    row = asset.field_config_row
    return row.field_config if row is not None else None


def asset_projection(asset, raw_config=_UNSET):
    """Validated logical projection of an asset, or None; fails closed."""
    if raw_config is _UNSET:
        raw_config = raw_config_of(asset)
    try:
        return fields.projection_of(asset.schema_json, raw_config)
    except FieldConfigError:
        fail(409, "ASSET_FIELD_CONFIG_INVALID",
             "The stored field configuration is inconsistent with the source schema")


def asset_dto(db, user, asset, *, dbx_status=None, raw_config=_UNSET):
    dataset = db.get(Dataset, asset.dataset_id)
    write = asset.writable and user.role in {"admin", "editor"} and (user.role == "admin" or user.id == asset.created_by)
    status = dbx_status if dbx_status is not None else dbx().status()
    # Columns and the primary key are addressed through logical names when the
    # asset is configured; the schema_fingerprint stays the physical identity.
    projection = asset_projection(asset, raw_config)
    result = {"id": asset.id, "dataset_id": asset.dataset_id, "name": asset.name,
              "connection_id": asset.connection_id, "schema_name": asset.schema_name,
              "table_name": asset.table_name,
              "columns": projection["columns"] if projection else asset.schema_json["columns"],
              "primary_key": projection["primary_key"] if projection else asset.schema_json["primary_key"],
              "schema_fingerprint": asset.schema_fingerprint, "writable": asset.writable,
              "capabilities": {"read": True, "write": write, "sync": user.role in {"admin", "editor"},
                             "dbx_metadata": status.get("available") is True},
              "backends": {"snapshot": "native", "mutation": "native", "metadata": "dbx"},
              "dbx": status, "status": "registered",
              "latest_version_id": dataset.latest_version_id if dataset else None,
              "created_at": asset.created_at, "updated_at": asset.updated_at}
    if projection is not None:
        result["field_config"] = projection["field_config"]
        result["logical_fingerprint"] = projection["fingerprint"]
    return result


def list_assets(db, user):
    authorize(db, user)
    assets = db.scalars(sa.select(IntegratedAsset).order_by(IntegratedAsset.created_at.desc())).all()
    status = dbx().status()
    # One batched read keeps the listing free of per-asset config lookups.
    configs = {row.asset_id: row.field_config for row in db.scalars(
        sa.select(IntegratedAssetFieldConfig).where(
            IntegratedAssetFieldConfig.asset_id.in_([a.id for a in assets])))} if assets else {}
    return {"items": [asset_dto(db, user, a, dbx_status=status, raw_config=configs.get(a.id))
                      for a in assets], "total": len(assets)}


def _connection(db, connection_id):
    connection = db.get(Connection, connection_id)
    if connection is None:
        fail(404, "CONNECTION_NOT_FOUND", "Source connection not found")
    if connection.kind not in {"mysql", "postgres"}:
        fail(422, "UNSUPPORTED_SOURCE", "Only MySQL/PostgreSQL integrated sources are supported")
    return connection


def _transient_source(body: SourceReference):
    """Inline source handle with the shared connection shape; never persisted."""
    source = body.source_connection
    config = {"host": source.config.host, "port": source.config.port,
              "database": source.config.database, "user": source.config.user,
              "password": source.config.password}
    if source.config.dbx_host is not None:
        config["dbx_host"] = source.config.dbx_host
        config["dbx_port"] = source.config.dbx_port
    return SimpleNamespace(id="inline-" + uuid.uuid4().hex,
                           name=source.name.strip() or "integrated source",
                           kind=source.kind, config=config)


def resolve_source_reference(db, user, body: SourceReference):
    """Resolve a SourceReference to a persisted or transient source handle.

    Probes accept arbitrary external credentials and network destinations, so
    they are guarded exactly like registration: admin/editor with data.structured
    access.  Read-only roles never reach an external source through this port.
    """
    authorize(db, user, write=True)
    if body.connection_id is not None:
        return _connection(db, body.connection_id)
    return _transient_source(body)


def _probe_failure(exc: SQLAlchemyError) -> tuple[str, str]:
    """Classify a driver failure into a safe code/message pair.

    Classification inspects driver error codes and well-known keywords only;
    the original exception text, host, user and credentials never enter the
    response or logs.
    """
    original = getattr(exc, "orig", None) or exc
    name = type(original).__name__.lower()
    pgcode = str(getattr(original, "pgcode", "") or "")
    errno = next((value for value in (getattr(original, "args", ()) or ())
                  if isinstance(value, int)), None)
    try:
        text = str(original).lower()
    except Exception:
        text = ""
    if (pgcode in {"28P01", "28000"} or errno in {1044, 1045, 1698, 2062}
            or "invalidpassword" in name
            or "password authentication failed" in text or "access denied" in text):
        return ("AUTHENTICATION_FAILED", "The source rejected the supplied credentials")
    if (errno in {2002, 2003, 2005, 2006, 1042, 1130} or "gaierror" in name
            or "timeout" in name or "timed out" in text or "connection refused" in text
            or "could not connect" in text or "connection timed out" in text):
        return ("CONNECT_TIMEOUT", "The source endpoint is unreachable or did not answer in time")
    return ("SOURCE_UNAVAILABLE", "The source could not be reached with the supplied configuration")


def test_source(db, user, body: SourceReference):
    """Connectivity probe through the native lane; no persistence, no echo."""
    connection = resolve_source_reference(db, user, body)
    engine = None
    try:
        engine = source_engine(connection)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return {"success": True}
    except SQLAlchemyError as exc:
        # Structured, diagnosable failure without driver text or credentials.
        code, message = _probe_failure(exc)
        return {"success": False, "code": code, "message": message}
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


def list_source_resources(db, user, body: SourceReference):
    """dbx-owned table discovery for a referenced source."""
    connection = resolve_source_reference(db, user, body)
    return {"items": dbx().list_resources(connection)}


def describe_source_schema(db, user, body: SourceSchemaRequest):
    """Physical schema discovery with the exact register authority; no persistence."""
    if any("." in n or "\x00" in n for n in (body.schema_name, body.table_name)):
        fail(422, "INVALID_RESOURCE", "Schema/table names must be separate identifiers without dots")
    connection = resolve_source_reference(db, user, body)
    schema, fingerprint = _verified_source_schema(connection, body.schema_name, body.table_name)
    return {"columns": schema["columns"], "primary_key": schema["primary_key"],
            "schema_fingerprint": fingerprint}


def _verified_source_schema(connection, schema_name, table_name):
    """dbx-owned discovery cross-verified by native reflection (register authority)."""
    evidence = dbx().describe_table(connection, schema_name, table_name)
    probe = SimpleNamespace(schema_name=schema_name, table_name=table_name)
    engine = source_engine(connection)
    try:
        with engine.connect() as conn:
            _, schema, fingerprint = reflect(conn, probe)
            check_transactional_table(conn, probe)
    except SQLAlchemyError:
        fail(503, "SOURCE_UNAVAILABLE", "Source metadata is unavailable")
    finally:
        engine.dispose()
    verify_dbx_metadata(evidence, schema)
    return schema, fingerprint


def _field_config_or_fail(raw_entries, schema):
    try:
        return fields.normalize_field_config(raw_entries, schema)
    except FieldConfigError as exc:
        fail(422, exc.code, exc.message)


def register_asset(db, user, body):
    authorize(db, user, write=True)
    if body.connection_id is not None:
        connection = _connection(db, body.connection_id)
        new_connection = None
    else:
        source = body.source_connection
        name = source.name.strip()
        if not name:
            fail(422, "EMPTY_SOURCE_NAME", "Source connection name cannot be blank")
        # Duplicate connection names make name-keyed datasets/tasks ambiguous
        # (commercial review D-005); the shared Connection model owns naming.
        if db.scalar(sa.select(Connection.id).where(Connection.name == name)):
            fail(409, "CONNECTION_NAME_CONFLICT", "A connection with this name already exists")
        config = _transient_source(body).config
        new_connection = Connection(
            id=str(uuid.uuid4()), name=name, kind=source.kind,
            config={"_encrypted": encrypt(json.dumps(config, ensure_ascii=False))},
            status="inactive", created_by=user.id)
        connection = new_connection
    # A two-part physical resource has unambiguous stable identity. Quoted SQL
    # identifiers remain safe, but dots in either part would make identity ambiguous.
    if any("." in n or "\x00" in n for n in (body.schema_name, body.table_name)):
        fail(422, "INVALID_RESOURCE", "Schema/table names must be separate identifiers without dots")
    resource = f"{body.schema_name}.{body.table_name}"
    if db.scalar(sa.select(Dataset.id).where(Dataset.source_connection_id == connection.id, Dataset.source_resource == resource)):
        fail(409, "RESOURCE_ALREADY_REGISTERED", "A dataset already owns this connection/resource; it cannot be silently adopted")
    # Discovery is genuinely dbx-owned, not silently routed to the native lane.
    # The inline connection is verified against the real source before any row
    # is persisted: a failed probe leaves nothing behind.
    schema, fingerprint = _verified_source_schema(connection, body.schema_name, body.table_name)
    config = (_field_config_or_fail([entry.model_dump() for entry in body.field_config], schema)
              if body.field_config is not None else None)
    name = body.name.strip()
    if not name:
        fail(422, "EMPTY_ASSET_NAME", "Asset name cannot be blank")
    dataset = Dataset(id=str(uuid.uuid4()), name=name, kind="integrated",
                      source_connection_id=connection.id, source_resource=resource, schema_json=schema)
    asset = IntegratedAsset(id=str(uuid.uuid4()), dataset_id=dataset.id,
                            connection_id=connection.id, name=name,
                            schema_name=body.schema_name, table_name=body.table_name,
                            schema_json=schema, schema_fingerprint=fingerprint,
                            writable=body.writable, created_by=user.id)
    if config is not None:
        asset.field_config_row = IntegratedAssetFieldConfig(
            asset_id=asset.id, field_config=config)
    if new_connection is not None:
        db.add(new_connection)
    db.add(dataset)
    try:
        # Connection encryption, dataset, asset and its optional field config
        # commit atomically; no partial registration can survive a failure.
        db.flush()
        db.add(asset)
        db.commit()
    except IntegrityError:
        db.rollback()
        fail(409, "RESOURCE_ALREADY_REGISTERED", "A dataset already owns this connection/resource")
    return asset_dto(db, user, asset)


def refresh_schema(db, user, asset_id):
    authorize(db, user, write=True)
    asset = get_asset(db, user, asset_id)
    if user.role != "admin" and asset.created_by != user.id:
        fail(403, "ASSET_OWNER_REQUIRED", "Only the owner or admin may refresh schema")
    connection = _connection(db, asset.connection_id)
    schema, fingerprint = _verified_source_schema(connection, asset.schema_name, asset.table_name)
    raw_config = raw_config_of(asset)
    normalized = None
    if raw_config is not None:
        try:
            normalized = fields.normalize_field_config(raw_config, schema)
        except FieldConfigError as exc:
            # A mapped source column disappeared: refuse to refresh rather
            # than silently dropping or widening the logical projection.
            code = ("FIELD_CONFIG_SOURCE_MISSING"
                    if exc.code == "FIELD_CONFIG_UNKNOWN_SOURCE" else exc.code)
            fail(409, code, f"Refresh rejected: {exc.message}")
    asset.schema_json, asset.schema_fingerprint = schema, fingerprint
    # The one-to-one config row is preserved across refreshes by construction.
    if normalized is not None:
        asset.field_config_row.field_config = normalized
    dataset = db.get(Dataset, asset.dataset_id)
    if dataset is None:
        db.rollback()
        fail(409, "ASSET_DATASET_MISSING", "Integrated dataset metadata is inconsistent")
    # Versions retain their own frozen schema. Do not reinterpret prior versions.
    if dataset.latest_version_id is None:
        dataset.schema_json = schema
    db.commit()
    return asset_dto(db, user, asset)


def preview_rows(db, user, asset_id, limit=100):
    if type(limit) is not int or not 1 <= limit <= 1000:
        fail(422, "INVALID_PREVIEW_LIMIT", "Preview limit must be between 1 and 1000")
    with open_read_scope(db, user, [asset_id]) as scope:
        table = scope.tables[asset_id]
        statement = sa.select(table)
        if table.primary_key.columns:
            statement = statement.order_by(*table.primary_key.columns)
        statement = statement.limit(limit)
        meta = scope.meta[asset_id]
        projection = meta.get("projection")
        rows = []
        for batch in scope.iter_batches(statement):
            for row in batch:
                # Tokens bind to the FULL physical row under the physical
                # fingerprint, so a later locked re-read recomputes the
                # identical token even when the response is projected.
                token = row_token(row, meta["schema_fingerprint"])
                if projection is None:
                    values = row
                else:
                    values = {column["name"]: row[column["source_key"]]
                              for column in projection["columns"]}
                rows.append({"values": wire_value(values), "row_token": token})
        return {"rows": rows,
                "columns": projection["columns"] if projection else meta["columns"],
                "source_state": {**scope.provenance, **scope.completion},
                "preview": True, "limit": limit}


def versions(db, user, asset_id):
    asset = get_asset(db, user, asset_id)
    records = db.scalars(sa.select(DatasetVersion).where(DatasetVersion.dataset_id == asset.dataset_id)
                         .order_by(DatasetVersion.version_no.desc()).limit(100)).all()
    run_by_version = {run.version_id: run for run in db.scalars(sa.select(IntegratedSyncRun).where(
        IntegratedSyncRun.asset_id == asset.id,
        IntegratedSyncRun.version_id.in_([v.id for v in records]),
        IntegratedSyncRun.status == "sealed",
    )).all()} if records else {}
    return {"items": [{"id": v.id, "version_no": v.version_no, "row_count": v.rowcount,
                       "checksum": v.checksum, "created_at": v.created_at,
                       "status": "sealed" if v.id in run_by_version else "unverified",
                       "complete": v.id in run_by_version,
                       "run_id": run_by_version[v.id].id if v.id in run_by_version else None}
                      for v in records]}


def sync_run_dto(run):
    return {"run_id": run.id, "asset_id": run.asset_id, "dataset_id": run.dataset_id,
            "status": run.status, "complete": run.status == "sealed" and run.version_id is not None,
            "row_count": run.row_count, "expected_rows": run.expected_rows,
            "version_id": run.version_id, "error": run.error,
            "checksum": run.checksum,
            "created_at": run.created_at, "updated_at": run.updated_at,
            "started_at": run.snapshot_json.get("started_at"), "completed_at": run.completed_at}


def request_sync(db, user, asset_id, batch_size=1000):
    authorize(db, user, write=True)
    asset = get_asset(db, user, asset_id)
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        fail(422, "INVALID_BATCH_SIZE", "Batch size must be between 1 and 1000")
    dataset = db.get(Dataset, asset.dataset_id)
    if dataset is None:
        fail(409, "ASSET_DATASET_MISSING", "Integrated dataset metadata is inconsistent")
    run_id = str(uuid.uuid4())
    run = IntegratedSyncRun(
        id=run_id, dataset_id=asset.dataset_id, asset_id=asset.id,
        base_version_id=dataset.latest_version_id, status="queued",
        physical_table="lake_iv_" + uuid.UUID(run_id).hex,
        schema_json={"queued": True},
        snapshot_json={"queued": True, "actor_id": user.id, "batch_size": batch_size},
        expected_rows=0, row_count=0,
    )
    db.add(run)
    db.commit()
    from app.data_channel.pipeline_tasks.dispatch import dispatch_integrated_sync
    try:
        dispatch_integrated_sync(run_id, asset.id, user.id, batch_size)
    except Exception:
        # A publish acknowledgement can itself be lost. Only transition a still
        # queued run; a worker that has already claimed it must not be overwritten.
        changed = db.execute(sa.update(IntegratedSyncRun).where(
            IntegratedSyncRun.id == run_id, IntegratedSyncRun.status == "queued"
        ).values(status="failed", error="Background dispatch was not confirmed; no completed snapshot is available",
                 completed_at=datetime.now(timezone.utc)))
        db.commit()
        db.refresh(run)
        if not changed.rowcount and run.status in {"starting", "loading", "sealed"}:
            return sync_run_dto(run)
        raise HTTPException(503, {"code": "INTEGRATED_DISPATCH_UNCONFIRMED", "run": sync_run_dto(run)})
    return sync_run_dto(run)


def list_sync_runs(db, user, asset_id):
    get_asset(db, user, asset_id)
    runs = db.scalars(sa.select(IntegratedSyncRun).where(IntegratedSyncRun.asset_id == asset_id)
                      .order_by(IntegratedSyncRun.created_at.desc()).limit(100)).all()
    return {"items": [sync_run_dto(run) for run in runs]}


def get_sync_run(db, user, asset_id, run_id):
    get_asset(db, user, asset_id)
    run = db.get(IntegratedSyncRun, run_id)
    if run is None or run.asset_id != asset_id:
        fail(404, "SYNC_RUN_NOT_FOUND", "Integrated sync run not found")
    return sync_run_dto(run)


def _parse_temporal(value, parser):
    # fromisoformat silently drops digits beyond Python's microsecond precision.
    # Extra trailing zeroes are exact; nonzero discarded digits are never exact.
    if not isinstance(value, str) or any(
        any(digit != "0" for digit in fraction[6:])
        for fraction in re.findall(r"[.,]([0-9]+)", value)
    ):
        raise ValueError()
    # A fractional UTC offset is not a portable source timezone. Python even
    # silently treats +00:00:00.1 as UTC; reject rather than alter the instant.
    result = parser(value)
    offset = re.search(r"[+-][0-9:]+[.,]([0-9]+)$", value) if result.tzinfo else None
    if offset and any(digit != "0" for digit in offset.group(1)):
        raise ValueError()
    return result


def _check_temporal_precision(typ, result):
    if isinstance(typ, (mysql.DATETIME, mysql.TIMESTAMP, mysql.TIME)):
        precision = 0 if typ.fsp is None else typ.fsp
    else:
        precision = getattr(typ, "precision", None)
        precision = 6 if precision is None else precision
    if not isinstance(precision, int) or not 0 <= precision <= 6:
        raise ValueError()
    if result.microsecond % (10 ** (6 - precision)):
        raise ValueError()


def _decode(column, value):
    if value is None:
        if not column.nullable:
            fail(422, "NULL_NOT_ALLOWED", f"Column {column.name} is not nullable")
        # SQLAlchemy JSON normally binds Python None as JSON literal null.
        # The wire contract reserves None for SQL NULL, regardless of dialect.
        return sa.null() if isinstance(column.type, sa.JSON) else None
    typ = column.type
    try:
        if isinstance(typ, sa.Boolean):
            if not isinstance(value, bool):
                raise ValueError()
            return value
        if isinstance(typ, sa.Integer):
            if isinstance(value, dict) and set(value) == {"$integer"}:
                value = value["$integer"]
            if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"-?[0-9]+", str(value)):
                raise ValueError()
            return int(value)
        if isinstance(typ, (sa.Float, sa.REAL)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError()
            return value
        if isinstance(typ, sa.Numeric):
            if isinstance(value, dict) and set(value) == {"$decimal"}:
                value = value["$decimal"]
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError()
            result = Decimal(value)
            if not result.is_finite():
                raise ValueError()
            # Inspect decimal digits rather than quantize(): quantize uses the
            # process decimal context (often 28 digits) and may round or reject
            # a valid NUMERIC(65, 30) source value.
            normalized = result.as_tuple()
            digits = list(normalized.digits)
            exponent = normalized.exponent
            while len(digits) > 1 and digits[-1] == 0:
                digits.pop()
                exponent += 1
            fractional = max(-exponent, 0) if result else 0
            integer = max(len(digits) + exponent, 0) if result else 0
            if typ.scale is not None and fractional > typ.scale:
                raise ValueError()
            if typ.precision is not None:
                if typ.scale is not None and integer > typ.precision - typ.scale:
                    raise ValueError()
                if typ.scale is None and integer + fractional > typ.precision:
                    raise ValueError()
            return result
        if isinstance(typ, sa.DateTime):
            result = _parse_temporal(value, datetime.fromisoformat)
            has_timezone = isinstance(typ, mysql.TIMESTAMP) or bool(typ.timezone)
            if bool(result.tzinfo) != has_timezone:
                raise ValueError()
            if has_timezone:
                result = result.astimezone(timezone.utc)
                if isinstance(typ, mysql.TIMESTAMP):
                    result = result.replace(tzinfo=None)
            _check_temporal_precision(typ, result)
            return result
        if isinstance(typ, sa.Date):
            return date.fromisoformat(value)
        if isinstance(typ, sa.Time):
            result = _parse_temporal(value, time.fromisoformat)
            if bool(result.tzinfo) != bool(typ.timezone):
                raise ValueError()
            _check_temporal_precision(typ, result)
            return result
        if isinstance(typ, (sa.LargeBinary, sa.BINARY)):
            if not isinstance(value, dict) or set(value) != {"$binary"}:
                raise ValueError()
            return base64.b64decode(value["$binary"], validate=True)
        if isinstance(typ, sa.JSON):
            if not isinstance(value, str):
                raise ValueError()
            def reject_constant(_):
                raise ValueError()
            # Validate without converting high-precision JSON numbers to float;
            # bind the original text through the source engine's JSON serializer.
            json.loads(value, parse_float=Decimal, parse_constant=reject_constant)
            return JsonText(value)
        if isinstance(typ, sa.String):
            if not isinstance(value, str) or (typ.length and len(value) > typ.length):
                raise ValueError()
            return value
        if isinstance(typ, sa.Uuid):
            return uuid.UUID(value)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        fail(422, "INVALID_COLUMN_VALUE", f"Invalid or lossy value for column {column.name}")
    fail(422, "UNSUPPORTED_COLUMN_TYPE", f"Unsupported column {column.name}")


def receipt(record, *, replayed=False):
    return {"id": record.id, "asset_id": record.asset_id, "status": record.status,
            "operation": record.operation, "affected_rows": record.affected_rows,
            "error_code": record.error_code, "replayed": replayed, **record.result_json}


def _replay(record, request_hash):
    if record.request_hash != request_hash:
        fail(409, "IDEMPOTENCY_PAYLOAD_MISMATCH", "This idempotency key belongs to a different request")
    if record.status in {"unknown", "pending"}:
        fail(409, "UNKNOWN_REQUIRES_RECONCILIATION", "Source outcome is unconfirmed; do not replay. Reconcile against the source and mutation receipt.")
    if record.status != "applied":
        raise HTTPException(409 if record.status == "conflict" else 422, {"code": record.error_code, "mutation": receipt(record, replayed=True)})
    return receipt(record, replayed=True)


def mutate_row(db, user, asset_id, operation, body):
    if operation not in {"insert", "update", "delete"}:
        fail(422, "INVALID_MUTATION_OPERATION", "Only a single-row insert, update or delete is supported")
    asset = get_asset(db, user, asset_id, write=True)
    # Keys and values are addressed through logical names when the asset is
    # configured; the physical primary key never changes.
    projection = asset_projection(asset)
    if projection is not None:
        pk = projection["primary_key"]
        addressable = set(projection["logical_to_physical"])
    else:
        pk = asset.schema_json.get("primary_key", [])
        addressable = None
    if not pk or set(body.key) != set(pk) or any(v is None for v in body.key.values()):
        fail(422, "COMPLETE_PRIMARY_KEY_REQUIRED", "Supply every explicit primary-key column and no other key")
    if set(body.values) & set(pk):
        fail(422, "PRIMARY_KEY_CHANGE_DENIED", "Primary-key values belong only in key and cannot be changed")
    if addressable is not None and set(body.values) - addressable:
        # Unmapped physical columns are not part of the asset's addressing
        # namespace and must never be written through the logical projection.
        fail(422, "UNKNOWN_COLUMN", "Mutation contains an unknown column")
    if operation in {"update", "delete"} and not body.expected_row_token:
        fail(422, "EXPECTED_ROW_TOKEN_REQUIRED", "Updates/deletes require the row token from a source read")
    if operation == "update" and not body.values:
        fail(422, "EMPTY_UPDATE", "An update must change at least one non-primary-key column")
    if operation == "delete" and body.values:
        fail(422, "DELETE_VALUES_DENIED", "Delete does not accept replacement values")
    request_hash = digest({"operation": operation, "body": body.model_dump(exclude={"idempotency_key"})})
    record = db.scalar(sa.select(IntegratedMutation).where(
        IntegratedMutation.asset_id == asset.id, IntegratedMutation.actor_id == user.id,
        IntegratedMutation.idempotency_key == body.idempotency_key))
    if record:
        return _replay(record, request_hash)
    record = IntegratedMutation(id=str(uuid.uuid4()), asset_id=asset.id, actor_id=user.id,
                                idempotency_key=body.idempotency_key, operation=operation,
                                request_hash=request_hash, key_hash=digest(body.key),
                                schema_fingerprint=asset.schema_fingerprint, status="pending", result_json={})
    db.add(record)
    try:
        # Reserve and persist audit intent BEFORE any source write can commit.
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = db.scalar(sa.select(IntegratedMutation).where(
            IntegratedMutation.asset_id == asset.id, IntegratedMutation.actor_id == user.id,
            IntegratedMutation.idempotency_key == body.idempotency_key))
        if prior is None:
            raise
        return _replay(prior, request_hash)
    connection = _connection(db, asset.connection_id)
    engine = None
    conn = None
    transaction = None
    committing = False
    error_status = None
    try:
        engine = source_engine(connection)
        conn = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        transaction = conn.begin()
        if conn.dialect.name == "postgresql":
            conn.execute(sa.text("SELECT set_config('TimeZone', 'UTC', true)"))
            conn.execute(sa.text("SELECT set_config('statement_timeout', '30000', true)"))
            conn.execute(sa.text("SELECT set_config('lock_timeout', '10000', true)"))
        elif conn.dialect.name == "mysql":
            conn.exec_driver_sql("SET SESSION time_zone = '+00:00'")
            conn.exec_driver_sql("SET SESSION sql_mode = CONCAT_WS(',', @@SESSION.sql_mode, 'STRICT_ALL_TABLES')")
            conn.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 10"))
        check_transactional_table(conn, asset)
        table, _, fingerprint = reflect(conn, asset)
        if fingerprint != asset.schema_fingerprint:
            fail(409, "SOURCE_SCHEMA_DRIFT", "Source schema changed; refresh before writing")
        to_physical = projection["logical_to_physical"] if projection is not None else {}
        physical_names = {to_physical.get(name, name) for name in body.key}
        physical_names |= {to_physical.get(name, name) for name in body.values}
        if physical_names - set(table.c.keys()):
            fail(422, "UNKNOWN_COLUMN", "Mutation contains an unknown column")
        key = {to_physical.get(name, name): _decode(table.c[to_physical.get(name, name)], value)
               for name, value in body.key.items()}
        values = {to_physical.get(name, name): _decode(table.c[to_physical.get(name, name)], value)
                  for name, value in body.values.items()}
        condition = sa.and_(*(table.c[name] == value for name, value in key.items()))
        existing = conn.execute(sa.select(table).where(condition).with_for_update()).mappings().first()
        if operation == "insert":
            if existing is not None:
                fail(409, "ROW_ALREADY_EXISTS", "The requested primary key already exists")
            result = conn.execute(sa.insert(table).values(**key, **values))
        else:
            if existing is None:
                fail(409, "ROW_NOT_FOUND", "The row no longer exists")
            if row_token(normalize_row(existing, table), fingerprint) != body.expected_row_token:
                fail(409, "ROW_CONFLICT", "The source row changed since it was read")
            if operation == "update":
                result = conn.execute(sa.update(table).where(condition).values(**values))
            else:
                result = conn.execute(sa.delete(table).where(condition))
        try:
            # SQLAlchemy MySQL uses matched rows, so an ordinary no-op UPDATE
            # still reports one. Suppressed or unprovable writes cannot succeed.
            affected_rows = result.rowcount
            if not result.supports_sane_rowcount() or affected_rows != 1:
                fail(409, "SOURCE_WRITE_NOT_APPLIED", "Source did not confirm a single-row write")
        finally:
            try:
                result.close()
            except Exception:
                pass
        remaining = conn.execute(sa.select(table).where(condition)).mappings().one_or_none()
        if (remaining is not None) == (operation == "delete"):
            fail(409, "SOURCE_WRITE_NOT_APPLIED", "Source row does not match the requested operation")
        if operation != "delete":
            updated = normalize_row(remaining, table)
            record.result_json = {"row_token": row_token(updated, fingerprint)}
        committing = True
        transaction.commit()
        record.status = "applied"
        record.affected_rows = affected_rows
    except HTTPException as exc:
        if transaction and transaction.is_active:
            try:
                transaction.rollback()
            except Exception:
                pass
        record.status = "conflict" if exc.status_code == 409 else "failed"
        record.error_code = exc.detail.get("code", "SOURCE_WRITE_REJECTED") if isinstance(exc.detail, dict) else "SOURCE_WRITE_REJECTED"
        record.error = "Source write was rejected before commit"
        error_status = exc.status_code
    except Exception:
        # A dropped connection or metadata failure after issuing COMMIT may
        # have committed. Never infer rollback or automatically replay it.
        record.status = "unknown" if committing else "failed"
        record.error_code = "UNKNOWN_REQUIRES_RECONCILIATION" if committing else "SOURCE_WRITE_FAILED"
        record.error = "Source outcome is unconfirmed" if committing else "Source transaction failed before commit"
        error_status = 409 if committing else 503
        if transaction and transaction.is_active:
            try:
                transaction.rollback()
            except Exception:
                pass
    finally:
        # Resource cleanup cannot erase a known commit outcome or skip the
        # durable audit finalization below. No source values are logged.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
    record.completed_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception:
        db.rollback()
        fail(409, "UNKNOWN_REQUIRES_RECONCILIATION", "Mutation audit could not be finalized; do not replay the source write")
    if error_status:
        raise HTTPException(error_status, {"code": record.error_code, "mutation": receipt(record)})
    return receipt(record)
