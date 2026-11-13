"""Local reads over the current typed replica (``lake_ir_*``) — never source IO.

This module is the read lane of the integrated snapshot assessment: previews
and unified-query relations resolve the durable ``active_run_id`` pointer and
compile typed SQL against the replica's physical platform table. The source
network is never touched here, so an unreachable source can never fail a local
read; replica coverage is disclosed through its own state instead (``stale``
reads are annotated, ``rebuild_required``/``blocked`` reject).

State honesty: a replica read serves the last confirmed coverage instant
(``coverage_confirmed_at``), labeled ``replica_snapshot``. It is neither
remote-live nor immutable, and cross-source queries carry no shared
source-side instant — that vocabulary is decided by the query core.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException

from app.data_channel.integrated import native
from app.data_channel.integrated import replica_control as control
from app.data_channel.integrated import replica_store as store
from app.data_channel.integrated.contracts import fail, row_token, wire_value
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_models import IntegratedReplica, IntegratedReplicaRun
from app.data_channel.query.contracts import QueryError

# Stable error codes shared by the HTTP lane and the query provider. Driver
# text, credentials and source values never appear in any of them.
_HTTP_ERRORS = {
    "REPLICA_NOT_REGISTERED": (409, "replica_not_ready",
                               "The asset has no local replica; request a rebuild"),
    "REPLICA_NOT_READY": (409, "replica_not_ready",
                          "The local replica is not currently readable; request a rebuild"),
    "REPLICA_SCHEMA_CHANGED": (409, "replica_schema_changed",
                               "The source schema changed; refresh and rebuild the replica"),
    "ACTIVE_REPLICA_MISSING": (503, "replica_inconsistent",
                               "The published replica pointer is inconsistent"),
    "REPLICA_RUN_MISSING": (503, "replica_inconsistent",
                            "The published replica run is missing"),
}
_QUERY_ERRORS = {
    "REPLICA_NOT_REGISTERED": (409, "replica_not_ready",
                               "The asset has no local replica; request a rebuild"),
    "REPLICA_NOT_READY": (409, "replica_not_ready",
                          "The local replica is not currently readable; request a rebuild"),
    "REPLICA_SCHEMA_CHANGED": (409, "replica_schema_changed",
                               "The source schema changed; refresh and rebuild the replica"),
    "ACTIVE_REPLICA_MISSING": (503, "replica_inconsistent",
                               "The published replica pointer is inconsistent"),
    "REPLICA_RUN_MISSING": (503, "replica_inconsistent",
                            "The published replica run is missing"),
}


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).isoformat()


def _stale(replica: IntegratedReplica) -> bool:
    """Readable but not fully caught up: anything other than a clean ``ready``.

    ``initializing``/``catching_up``/``stale`` may still serve the previous
    complete active run, but its coverage is already behind the replication
    frontier — reads must be annotated, never presented as fresh.
    """
    return replica.status != "ready"


def _fail_http(code: str, status: str | None = None) -> None:
    http_status, public, message = _HTTP_ERRORS.get(
        code, (503, "replica_inconsistent", "The local replica cannot be read"))
    extra = {"replica_status": status} if status else {}
    fail(http_status, public, message, **extra)


def _fail_query(code: str, status: str | None = None) -> None:
    query_status, public, message = _QUERY_ERRORS.get(
        code, (503, "replica_inconsistent", "The local replica cannot be read"))
    extra = {"replica_status": status} if status else {}
    raise QueryError(query_status, public, message, **extra)


def replica_status_dto(db, asset) -> dict[str, Any] | None:
    """Platform-only status projection; None when no replica row exists.

    ``complete`` means an active run exists under the asset's current schema
    fingerprint and the replica state admits reads. It never probes or opens
    the source.
    """
    replica = db.get(IntegratedReplica, asset.id)
    if replica is None:
        return None
    run = db.get(IntegratedReplicaRun, replica.active_run_id) \
        if replica.active_run_id else None
    complete = bool(
        run is not None and run.status == "active"
        and replica.status in control.READABLE_STATUSES
        and replica.schema_fingerprint == asset.schema_fingerprint
        and run.schema_fingerprint == asset.schema_fingerprint)
    return {
        "status": replica.status,
        "complete": complete,
        # A non-ready replica that still serves an old active run is readable
        # but stale: the label must not imply the last confirmed coverage is
        # the last source change time.
        "stale": bool(complete and _stale(replica)),
        "row_count": run.row_count if run is not None else None,
        "applied_at": _utc_iso(replica.applied_at),
        "coverage_confirmed_at": _utc_iso(replica.coverage_confirmed_at),
        "last_error_code": replica.last_error_code,
        "generation": replica.generation,
        "run_id": replica.active_run_id,
    }


def open_readable(db, asset) -> tuple[IntegratedReplica, IntegratedReplicaRun,
                                      store.ReplicaSchema, sa.Table]:
    """Resolve the readable active replica to its schema and physical table.

    Raises the integrated HTTP lane's structured errors (the query provider
    wraps the same control codes into ``QueryError`` separately).
    """
    try:
        replica, run = control.readable_run(db, asset)
        schema = store.build_replica_schema(
            run.id, run.schema_json, schema_fingerprint=run.schema_fingerprint)
        table = store.build_table(schema, db.get_bind().dialect.name)
    except control.ReplicaControlError as exc:
        current = db.get(IntegratedReplica, asset.id)
        _fail_http(exc.code, current.status if current is not None else None)
    except store.ReplicaStoreError:
        _fail_http("ACTIVE_REPLICA_MISSING")
    # Inspect through the session's own connection: the caller may have created
    # the physical table in this still-open transaction (SQLite visibility),
    # and no extra engine connection is taken for the catalog lookup.
    if not sa.inspect(db.connection()).has_table(schema.table_name):
        fail(503, "replica_table_missing",
             "The published replica table is missing; request a rebuild")
    return replica, run, schema, table


def replica_cursor(db, asset_id: str) -> tuple[IntegratedReplica, IntegratedReplicaRun, store.ReplicaSchema]:
    """Re-read asset + readable replica cursor; raises control/store errors."""
    asset = db.get(_asset_model(), asset_id)
    if asset is None:
        raise control.ReplicaControlError("REPLICA_NOT_REGISTERED")
    replica, run = control.readable_run(db, asset)
    schema = store.build_replica_schema(run.id, run.schema_json, schema_fingerprint=run.schema_fingerprint)
    return replica, run, schema


def _asset_model():
    return IntegratedAsset


def replica_expressions(schema: store.ReplicaSchema, table: sa.Table) -> dict[str, Any]:
    """Source-named column expressions over the replica table.

    Physical replica columns come from ``schema.column_map``; JSON columns are
    read as their stored text so the wire keeps exact JSON (the same shape
    ``store.read_batches`` produces).
    """
    expressions = {}
    for column in schema.columns:
        expression = table.c[column.physical_name]
        if column.spec.type == "json":
            expression = expression.cast(sa.Text())
        expressions[column.source_name] = expression.label(column.source_name)
    return expressions


def preview_rows(db, user, asset_id, limit=100):
    """Bounded local preview over the current replica, source-free.

    Row tokens bind to the full source-named row under the asset's physical
    schema fingerprint — the same namespace a source read produces — so CAS
    writes keep working against the source row_token contract.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        fail(422, "INVALID_PREVIEW_LIMIT", "Preview limit must be between 1 and 1000")
    asset = native.get_asset(db, user, asset_id)
    meta = native.resolve_metadata(db, user, [asset_id])[asset_id]
    replica, run, schema, table = open_readable(db, asset)
    expressions = replica_expressions(schema, table)
    order = [table.c[schema.column_map[name]] for name in schema.primary_key]
    statement = sa.select(*(expression.label(name)
                            for name, expression in expressions.items())
                          ).order_by(*order).limit(limit)
    raw_rows = db.execute(statement).mappings().all()
    projection = meta.get("projection")
    rows = []
    for raw in raw_rows:
        row = {column.source_name: store._normalize_read_value(column, raw[column.source_name])
               for column in schema.columns}
        token = row_token(
            row, meta["schema_fingerprint"],
            json_columns={column.source_name for column in schema.columns
                          if column.spec.type == "json"},
            float32_columns=native.replica_token_float32_columns(
                schema.columns, meta["dialect"]))
        if projection is None:
            values = row
        else:
            values = {column["name"]: row[column["source_key"]]
                      for column in projection["columns"]}
        rows.append({"values": wire_value(values), "row_token": token})
    coverage = replica.coverage_confirmed_at or replica.applied_at
    stale = _stale(replica)
    state = {
        "id": run.id, "kind": "replica_snapshot", "consistent": True,
        "started_at": _utc_iso(coverage), "complete": True,
        "row_count": len(rows), "replica_status": replica.status,
        "stale": stale,
        # Coverage confirmation is the verified replication cut, not the last
        # source change time: quiet tables keep their confirmed coverage.
        "coverage_confirmed_at": _utc_iso(replica.coverage_confirmed_at),
        "generation": replica.generation, "run_id": run.id,
    }
    return {"rows": rows,
            "columns": projection["columns"] if projection else meta["columns"],
            "source_state": state, "preview": True, "limit": limit,
            "stale": stale}
