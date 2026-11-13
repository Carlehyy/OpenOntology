"""Data-channel leaf adapters. Ontology adapters are injected by main."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
from dataclasses import fields
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import HTTPException

from .contracts import Binding, ColumnSpec, QueryError, Relation
from sqlalchemy.orm import Session
from app.auth.models import User


def column_spec(column: dict) -> ColumnSpec:
    allowed = {f.name for f in fields(ColumnSpec)}
    values = {key: value for key, value in column.items() if key in allowed}
    values["timezone"] = bool(values.get("timezone"))
    return ColumnSpec(**values)


def integrated_auth_fingerprint(auth_fingerprint: str, run_id: str) -> str:
    """Pin the authorization context *and* the active replica run identity.

    Authorization/schema fingerprints stay unchanged across a replica rebuild,
    so a memory handle (or persisted snapshot) keyed only by them could accept
    page 2 from a replacement run — mixing generations. Folding the run id into
    the existing auth fingerprint makes run replacement a context change while
    leaving the raw ``resolve_metadata`` authorization revision contract (and
    its source-config revision) untouched.
    """
    from app.data_channel.integrated.contracts import digest
    return digest({"authorization": auth_fingerprint, "replica_run": run_id})


def resolve_integrated(db, user, bindings: list[tuple[str, Binding]], limits, stack: ExitStack,
                       *, recheck_only: bool = False, execution_budget=None):
    """Resolve integrated assets as platform-local replica relations.

    Reads compile typed SQL against the current replica's ``lake_ir_*`` table
    on the platform database: ``platform=True``, ``connection_key='platform'``
    and no bounded stream (no staging copy of source tables). Resolution and
    page rechecks touch platform rows only — an unreachable source network can
    never fail a local read, and there is deliberately no remote probe and no
    silent fallback to a source read.
    """
    from app.data_channel.integrated import native, replica_control, replica_read
    from app.data_channel.integrated.replica_store import ReplicaStoreError

    metadata = native.resolve_metadata(db, user, [binding.asset_id for _, binding in bindings])
    grouped = defaultdict(list)
    for alias, binding in bindings:
        grouped[metadata[binding.asset_id]["connection_id"]].append((alias, binding))
    # Connection lifecycle states (even a failed-test "error") are source-side
    # facts: they gate nothing here. Reads serve the local replica, so an
    # offline/unreachable/failed source never blocks them; lifecycle changes
    # still fence in-flight pages through the status input in the auth
    # fingerprint (recheck below fails closed on rotation).
    result = {}
    for connection_id, members in grouped.items():
        for alias, binding in members:
            asset = native.get_asset(db, user, binding.asset_id)
            try:
                replica, run, schema, physical = replica_read.open_readable(db, asset)
            except replica_control.ReplicaControlError as exc:
                current = db.get(replica_control.IntegratedReplica, asset.id)
                replica_read._fail_query(
                    exc.code, current.status if current is not None else None)
            except ReplicaStoreError:
                replica_read._fail_query("ACTIVE_REPLICA_MISSING")
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                raise QueryError(exc.status_code, detail.get("code", "replica_inconsistent"),
                                 detail.get("message", "The local replica cannot be read")) from None
            meta = metadata[binding.asset_id]
            expected_auth = meta["auth_fingerprint"]
            expected_schema = meta["schema_fingerprint"]
            # Expressions bind through schema.column_map onto the ALIASED
            # physical replica table (physical names are sanitized; a source
            # name must never be used to index the physical columns). JSON
            # columns keep their CAST to stored text so exact JSON survives.
            table = physical.alias(alias)
            mapping = schema.column_map

            def physical_expression(source_name: str):
                expression = table.c[mapping[source_name]]
                if schema.column(source_name).spec.type == "json":
                    expression = expression.cast(sa.Text())
                return expression

            projection = meta.get("projection")
            if projection is not None:
                columns = {c["name"]: column_spec(c) for c in projection["columns"]}
                expressions = {name: physical_expression(source).label(name)
                               for name, source in projection["logical_to_physical"].items()}
            else:
                columns = {c["name"]: column_spec(c) for c in meta["columns"]}
                expressions = {name: physical_expression(name).label(name)
                               for name in mapping}
            # Complete structural primary key for deterministic keyset paging.
            # Logical names under a configured projection, source names
            # otherwise. Column visibility is never widened: a key outside the
            # authorized columns stays unset and lazy continuation is
            # ineligible for that relation (bounded materialization instead).
            if projection is not None:
                unique = [str(name) for name in projection.get("primary_key") or []]
            else:
                unique = [str(name) for name in meta.get("primary_key") or []]
            unique = unique if unique and all(name in columns for name in unique) else None
            coverage = replica.coverage_confirmed_at or replica.applied_at

            def recheck(asset_id=binding.asset_id, auth=expected_auth, schema=expected_schema,
                        run_id=run.id):
                # Fingerprint drift (auth/schema/lifecycle status) fails closed
                # first; the active run identity is then re-read from durable
                # platform rows so a page can never serve a replica that was
                # published over, retired or made unreadable. The run id is the
                # cursor: a merely REQUESTED rebuild (generation bump) does not
                # invalidate pages still served by the current active table.
                # No remote probe, no silent source fallback.
                with Session(bind=db.get_bind(), autoflush=False) as fresh_db:
                    fresh_user = fresh_db.get(User, user.id)
                    current = native.resolve_metadata(fresh_db, fresh_user, [asset_id])[asset_id]
                    if current["auth_fingerprint"] != auth or current["schema_fingerprint"] != schema:
                        raise QueryError(409, "query_context_changed",
                                         "Source authorization/schema changed")
                    try:
                        _, fresh_run, _ = replica_read.replica_cursor(fresh_db, asset_id)
                    except replica_control.ReplicaControlError as exc:
                        row = fresh_db.get(replica_control.IntegratedReplica, asset_id)
                        replica_read._fail_query(
                            exc.code, row.status if row is not None else None)
                    except ReplicaStoreError:
                        replica_read._fail_query("ACTIVE_REPLICA_MISSING")
                    if fresh_run.id != run_id:
                        raise QueryError(409, "query_context_changed",
                                         "The local replica was rebuilt; rerun the query")

            result[alias] = Relation(
                alias=alias, table=table, columns=columns,
                state={"alias": alias, "kind": "integrated", "id": binding.asset_id,
                       "versionId": None,
                       # A replica read serves its last confirmed coverage
                       # instant — not remote live, not an immutable version.
                       "asOf": replica_read._utc_iso(coverage),
                       "consistency": "replica_snapshot", "completeness": "complete",
                       "dialect": db.get_bind().dialect.name, "snapshotId": run.id,
                       "uniqueKey": unique, "replicaStatus": replica.status,
                       # Anything except a clean ready is readable-but-stale:
                       # initializing/catching_up keep serving the previous
                       # active run while coverage lags the replication cut.
                       "stale": replica.status != "ready",
                       "generation": replica.generation},
                auth_fingerprint=integrated_auth_fingerprint(expected_auth, run.id),
                schema_fingerprint=expected_schema,
                connection_key="platform",
                batches=None, recheck=recheck,
                platform=True, expressions=expressions)
    return result


def composed_consistency(source_states: list[dict], connection_keys: set[str]) -> str:
    """Whole-result consistency vocabulary across leaf source states.

    Replica reads add ``replica_snapshot`` leaves: each serves its own last
    confirmed coverage instant, so an all-replica result is labeled
    ``replica_snapshots`` (per-leaf coverage, no shared source-side instant),
    and any replica leaf mixed with other sources is honestly
    ``cross_source_non_atomic`` rather than a single-source snapshot.
    """
    consistencies = {state.get("consistency") for state in source_states}
    if consistencies == {"replica_snapshot"}:
        return "replica_snapshots"
    if "replica_snapshot" in consistencies:
        return "cross_source_non_atomic"
    return ("immutable_versions" if consistencies == {"immutable_version"}
            else "single_source_snapshot" if len(connection_keys) == 1
            else "cross_source_non_atomic")


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
    # Immutable sealed versions carry their complete primary key in the
    # manifest; keyset paging over an immutable version is deterministic.
    unique = [str(name) for name in metadata.get("primary_key_columns") or []
              if isinstance(name, str)]
    unique = unique if unique and all(name in columns for name in unique) else None

    def recheck():
        with Session(bind=db.get_bind(), autoflush=False) as fresh_db:
            fresh_user = fresh_db.get(User, user.id)
            if (fresh_user is None or not fresh_user.is_active
                    or not user_has_menu_access(fresh_db, fresh_user, "data.structured")
                    or fresh_db.get(Dataset, binding.dataset_id) is None):
                raise QueryError(403, "query_access_denied", "Dataset access revoked")
            current_auth = _hash({"user": str(fresh_user.id), "role": fresh_user.role,
                                  "dataset": binding.dataset_id})
            if current_auth != auth:
                raise QueryError(403, "query_access_denied", "Dataset authorization changed")
            try:
                current = service.describe_version(fresh_db, binding.dataset_id, binding.version_id)
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
                     "consistency": "immutable_version", "completeness": "complete",
                     "uniqueKey": unique},
                    auth, fingerprint, "platform", recheck=recheck,
                    platform=True, expressions=expressions)
