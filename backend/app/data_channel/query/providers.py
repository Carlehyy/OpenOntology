"""Data-channel leaf adapters. Ontology adapters are injected by main."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
from dataclasses import fields
from datetime import datetime, timezone

import sqlalchemy as sa

from .contracts import Binding, ColumnSpec, QueryError, Relation


def column_spec(column: dict) -> ColumnSpec:
    allowed = {f.name for f in fields(ColumnSpec)}
    values = {key: value for key, value in column.items() if key in allowed}
    values["timezone"] = bool(values.get("timezone"))
    return ColumnSpec(**values)


def resolve_integrated(db, user, bindings: list[tuple[str, Binding]], limits, stack: ExitStack):
    from app.data_channel.integrated.native import open_read_scope, resolve_metadata

    metadata = resolve_metadata(db, user, [binding.asset_id for _, binding in bindings])
    grouped = defaultdict(list)
    for alias, binding in bindings:
        grouped[metadata[binding.asset_id]["connection_id"]].append((alias, binding))
    result = {}
    for connection_id, members in grouped.items():
        scope = stack.enter_context(open_read_scope(db, user, [b.asset_id for _, b in members],
                                                   statement_timeout_ms=limits.statement_timeout_ms))

        def batches(statement, params, batch_size, scope=scope):
            # The native reader fetches single driver rows and bounds each
            # yielded batch by encoded bytes as well as this row count.
            yield from scope.iter_batches(statement, params, batch_size=min(batch_size, 1000))
            if scope.completion.get("complete") is not True:
                raise QueryError(409, "source_incomplete", "Source stream ended without complete EOF evidence")

        for alias, binding in members:
            meta = scope.meta[binding.asset_id]
            table = scope.tables[binding.asset_id].alias(alias)
            expected_auth = meta["auth_fingerprint"]
            expected_schema = meta["schema_fingerprint"]

            def recheck(asset_id=binding.asset_id, auth=expected_auth, schema=expected_schema):
                current = resolve_metadata(db, user, [asset_id])[asset_id]
                if current["auth_fingerprint"] != auth or current["schema_fingerprint"] != schema:
                    raise QueryError(409, "query_context_changed", "Source authorization/schema changed")

            result[alias] = Relation(
                alias=alias, table=table, columns={c["name"]: column_spec(c) for c in meta["columns"]},
                state={"alias": alias, "kind": "integrated", "id": binding.asset_id,
                       "versionId": None, "asOf": scope.provenance["started_at"],
                       "consistency": "repeatable_read", "completeness": "complete",
                       "dialect": meta["dialect"], "snapshotId": scope.provenance["id"]},
                auth_fingerprint=expected_auth, schema_fingerprint=expected_schema,
                connection_key="source:" + connection_id, batches=batches, recheck=recheck,
                source_group=scope)
    return result


def resolve_dataset(db, user, alias, binding, limits):
    from app.auth.permissions import user_has_menu_access
    from app.data_channel.datasets import service
    from app.data_channel.datasets.models import Dataset
    from .service import _hash

    if not user or not user.is_active or not user_has_menu_access(db, user, "data.structured"):
        raise QueryError(403, "query_access_denied", "Dataset query access denied")
    dataset = db.get(Dataset, binding.dataset_id)
    if dataset is None:
        raise QueryError(404, "dataset_not_found", "Dataset not found")
    # The public version relation checks sealed manifest/version completeness;
    # legacy whole-blob versions are not silently loaded as a query fallback.
    from app.data_channel.datasets.integrated_store import IntegratedVersionError
    try:
        relation = service.get_version_relation(db, binding.dataset_id, binding.version_id)
    except IntegratedVersionError as exc:
        raise QueryError(409, "source_incomplete", "Dataset version lacks a valid sealed immutable manifest") from exc
    metadata = relation.metadata
    if not isinstance(metadata, dict): metadata = vars(metadata)
    raw_columns = metadata.get("source_schema")
    if not raw_columns or not all(isinstance(c, dict) for c in raw_columns):
        raise QueryError(422, "dataset_query_schema_unavailable", "Version has no typed query schema")
    columns = {c["name"]: column_spec(c) for c in raw_columns}
    table = relation.table.alias(alias)
    mapping = relation.column_map
    expressions = {}
    for name, spec in columns.items():
        source = table.c[mapping[name]]
        if spec.type == "binary":
            expressions[name] = sa.func.decode(source, "base64", type_=sa.LargeBinary())
        elif spec.type == "decimal":
            # Precision/scale are validated on the immutable writer boundary;
            # never allow a CAST(p,s) to silently round a stored exact value.
            expressions[name] = sa.cast(source, sa.Numeric())
        elif spec.type != "string":
            expressions[name] = sa.cast(source, spec.sql_type())
        else:
            expressions[name] = source
    auth = _hash({"user": str(user.id), "role": user.role, "dataset": binding.dataset_id})
    fingerprint = metadata.get("schema_fingerprint") or _hash(raw_columns)

    def recheck():
        if not user_has_menu_access(db, user, "data.structured") or db.get(Dataset, binding.dataset_id) is None:
            raise QueryError(403, "query_access_denied", "Dataset access revoked")
        try:
            current = service.describe_version(db, binding.dataset_id, binding.version_id)
        except IntegratedVersionError as exc:
            raise QueryError(409, "query_context_changed", "Dataset version integrity changed") from exc
        if not isinstance(current, dict): current = vars(current)
        current_fingerprint = current.get("schema_fingerprint") or _hash(current.get("source_schema", raw_columns))
        if current_fingerprint != fingerprint:
            raise QueryError(409, "query_context_changed", "Dataset version schema changed")

    return Relation(alias, table, columns,
                    {"alias": alias, "kind": "dataset", "id": binding.dataset_id,
                     "versionId": binding.version_id,
                     "asOf": metadata.get("as_of") or metadata.get("created_at") or datetime.now(timezone.utc).isoformat(),
                     "consistency": "immutable_version", "completeness": "complete"},
                    auth, fingerprint, "platform", recheck=recheck,
                    platform=True, expressions=expressions)
