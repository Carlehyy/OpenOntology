"""Local replica read lane: preview, query relation, status and control routes.

These tests pin the integrated snapshot assessment read contract: reads touch
the platform replica only (an unreachable source network can never fail them),
row tokens stay in the source read's namespace, and unreadable/replaced
replicas fail closed without any silent source fallback.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import native, replica_control as control
from app.data_channel.integrated import replica_read, replica_store as store, service
from app.data_channel.integrated.contracts import digest, row_token
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_models import IntegratedReplica, IntegratedReplicaRun
from app.data_channel.query.contracts import Binding, QueryLimits, QueryError
from app.data_channel.query import providers

SCHEMA = {
    "columns": [
        {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
        {"name": "note", "type": "string", "nullable": True},
        {"name": "doc", "type": "json", "nullable": True, "encoding": "json_text"},
        # Reserved word + non-ASCII-case identifiers: the physical replica
        # column is a sanitized name that differs from the source name, so any
        # expression bound by source name instead of schema.column_map breaks.
        {"name": "Order", "type": "string", "nullable": True},
        {"name": "select", "type": "string", "nullable": True},
    ],
    "primary_key": ["id"],
}
FINGERPRINT = digest(SCHEMA)
ROWS = [
    {"id": 1, "note": "first", "doc": '{"a": 1}', "Order": "o-1", "select": "s-1"},
    {"id": 2, "note": "second", "doc": None, "Order": "o-2", "select": "s-2"},
]


@pytest.fixture
def asset(db, admin_user):
    connection = Connection(
        id="replica-read-source", name="replica read fixture", kind="postgres",
        # Deliberately unreachable: a local replica read must never dial it.
        config={"host": "10.255.255.1", "port": 5432, "user": "svc",
                "password": "secret", "database": "erp"}, status="active")
    dataset = Dataset(id="replica-read-dataset", name="replica read fixture",
                      kind="integrated", source_connection_id=connection.id,
                      source_resource="public.orders", schema_json=SCHEMA)
    row = IntegratedAsset(
        id="replica-read-asset", dataset_id=dataset.id, connection_id=connection.id,
        name="replica read fixture", schema_name="public", table_name="orders",
        schema_json=SCHEMA, schema_fingerprint=FINGERPRINT, writable=False,
        created_by=admin_user.id)
    db.add_all([connection, dataset, row])
    db.commit()
    return row


def _publish_active(db, asset, rows=ROWS):
    """Drive one run through the fenced lifecycle into a published replica."""
    run = control.request_rebuild(db, asset)
    token = control.claim_run(db, run.id)
    schema = store.build_replica_schema(run.id, run.schema_json, schema_fingerprint=run.schema_fingerprint)
    store.create_table(db.connection(), schema)
    if rows:
        store.write_batch(db.connection(), schema, rows)
    db.commit()
    control.begin_load(db, run.id, token, capture_id=str(uuid4()),
                       capture_start={"lsn": 1})
    control.finish_load(db, run.id, token, completion={
        "complete": True, "row_count": len(rows), "expected_rows": len(rows),
        "schema_fingerprint": run.schema_fingerprint, "snapshot_id": "snap-1"})
    db.commit()
    control.publish_run(db, run.id, token, applied_position={"lsn": 2},
                        coverage_confirmed_at=datetime.now(timezone.utc),
                        row_count=len(rows))
    db.commit()
    return run, schema


def test_preview_reads_local_replica_with_source_namespace_row_token(db, admin_user, asset, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a local replica read must never open or probe the source")

    monkeypatch.setattr(native, "open_read_scope", forbidden)
    monkeypatch.setattr(native, "probe_source", forbidden)
    monkeypatch.setattr(native, "source_engine", forbidden)
    run, _ = _publish_active(db, asset)

    preview = service.preview_rows(db, admin_user, asset.id, 10)
    assert [row["values"]["id"] for row in preview["rows"]] == [1, 2]
    assert preview["rows"][0]["values"]["doc"] == '{"a": 1}'
    assert preview["rows"][0]["values"]["note"] == "first"
    # Row tokens bind to the full source-named row under the asset's physical
    # fingerprint: identical to what a source read of the same row produces,
    # so CAS writes against the source keep their contract.
    assert preview["rows"][0]["row_token"] == row_token(
        {"id": 1, "note": "first", "doc": '{"a": 1}', "Order": "o-1", "select": "s-1"},
        FINGERPRINT, json_columns={"doc"})
    assert preview["rows"][1]["row_token"] == row_token(
        {"id": 2, "note": "second", "doc": None, "Order": "o-2", "select": "s-2"},
        FINGERPRINT, json_columns={"doc"})
    # Sanitized physical columns are bound through schema.column_map: reserved
    # words and case-sensitive source names keep reading their own values.
    assert preview["rows"][0]["values"]["Order"] == "o-1"
    assert preview["rows"][0]["values"]["select"] == "s-1"
    state = preview["source_state"]
    assert state["kind"] == "replica_snapshot" and state["complete"] is True
    assert state["run_id"] == run.id and state["replica_status"] == "ready"
    assert state["stale"] is False and state["coverage_confirmed_at"]
    # Bounded read: limit honored without scanning the whole replica.
    assert service.preview_rows(db, admin_user, asset.id, 1)["rows"][0]["values"]["id"] == 1


def test_preview_rejects_unreadable_replicas_instead_of_falling_back(db, admin_user, asset):
    # Registration alone (pending, no active run) is not readable.
    control.ensure_replica(db, asset)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        service.preview_rows(db, admin_user, asset.id, 10)
    assert (exc.value.status_code, exc.value.detail["code"]) == (409, "replica_not_ready")
    # A confirmed gap/rebuild stays unreadable even while rebuilding.
    _publish_active(db, asset)
    control.mark_unavailable(db, asset.id, error_code="SOURCE_LOG_GAP",
                             rebuild_required=True)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        service.preview_rows(db, admin_user, asset.id, 10)
    assert exc.value.detail["code"] == "replica_not_ready"
    assert exc.value.detail["replica_status"] == "rebuild_required"
    # Stale stays readable and is annotated, never hidden.
    control.mark_unavailable(db, asset.id, error_code="SOURCE_UNREACHABLE",
                             rebuild_required=False)
    db.commit()
    preview = service.preview_rows(db, admin_user, asset.id, 10)
    assert preview["stale"] is True
    assert preview["source_state"]["replica_status"] == "stale"


def test_replica_status_dto_and_routes(db, admin_user, asset):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.data_channel.integrated.router import router
    from app.deps import get_current_user, get_db

    control.ensure_replica(db, asset)
    db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/v2/integrated-datasets")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin_user
    with TestClient(app) as client:
        endpoint = f"/api/v2/integrated-datasets/{asset.id}/replica"
        # Pending: no active run, nothing readable, no source IO.
        pending = client.get(endpoint).json()
        assert pending["status"] == "pending" and pending["complete"] is False
        assert pending["stale"] is False
        assert pending["run_id"] is None and pending["coverage_confirmed_at"] is None
        # Rebuild is admin/editor gated and only queues a fenced run.
        queued = client.post(endpoint + "/rebuild").json()
        assert client.post(endpoint + "/rebuild").status_code == 202
        assert queued["status"] == "queued" and queued["generation"] >= 1
        again = client.get(endpoint).json()
        assert again["status"] == "initializing" and again["complete"] is False
        admin_user.role = "viewer"
        db.commit()
        assert client.post(endpoint + "/rebuild").status_code == 403
        assert client.post(endpoint + "/preflight").status_code == 403
        admin_user.role = "admin"
        db.commit()

        # A published replica flips complete with coverage + row count facts.
        run, _ = _publish_active(db, asset)
        ready = client.get(endpoint).json()
        assert ready["complete"] is True and ready["run_id"] == run.id
        assert ready["stale"] is False
        assert ready["row_count"] == len(ROWS)
        assert ready["coverage_confirmed_at"] and ready["applied_at"]
        assert ready["generation"] == run.generation
        detail = client.get(f"/api/v2/integrated-datasets/{asset.id}").json()
        assert detail["replica"]["status"] == "ready"
        assert detail["replica"]["complete"] is True
        assert detail["replica"]["stale"] is False


def test_rebuild_in_progress_serves_previous_active_as_stale(db, admin_user, asset):
    run, _ = _publish_active(db, asset)
    control.request_rebuild(db, asset)
    db.commit()
    replica = db.get(IntegratedReplica, asset.id)
    assert replica.status == "initializing" and replica.active_run_id == run.id
    preview = service.preview_rows(db, admin_user, asset.id, 10)
    assert preview["stale"] is True
    assert preview["source_state"]["stale"] is True
    assert preview["source_state"]["replica_status"] == "initializing"
    # Coverage stays the last confirmed cut of the previous active run; a
    # requested rebuild alone never advances or hides it.
    assert preview["source_state"]["run_id"] == run.id
    status = replica_read.replica_status_dto(db, asset)
    assert status["complete"] is True and status["stale"] is True
    assert status["run_id"] == run.id
    binding = Binding(kind="integrated", asset_id=asset.id)
    with ExitStack() as stack:
        relation = providers.resolve_integrated(
            db, admin_user, [("src", binding)], QueryLimits(), stack)["src"]
    assert relation.state["replicaStatus"] == "initializing"
    assert relation.state["stale"] is True
    # The old run identity is still the cursor; the queued replacement does
    # not invalidate pages that serve the current active table.
    relation.recheck()


def test_resolve_integrated_is_a_platform_relation_with_cursor_recheck(db, admin_user, asset, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("resolution must not open or probe the source")

    monkeypatch.setattr(native, "open_read_scope", forbidden)
    monkeypatch.setattr(native, "probe_source", forbidden)
    run, schema = _publish_active(db, asset)
    binding = Binding(kind="integrated", asset_id=asset.id)
    with ExitStack() as stack:
        relations = providers.resolve_integrated(
            db, admin_user, [("src", binding)], QueryLimits(), stack)
        relation = relations["src"]
        assert relation.platform is True
        assert relation.connection_key == "platform"
        assert relation.batches is None
        assert relation.state["consistency"] == "replica_snapshot"
        assert relation.state["snapshotId"] == run.id
        assert relation.state["asOf"] and relation.state["uniqueKey"] == ["id"]
        # Typed SQL executes directly against the local replica table.
        statement = sa.select(relation.expressions["note"]).order_by(
            relation.expressions["id"])
        notes = [row[0] for row in db.execute(statement)]
        assert notes == ["first", "second"]
        # Unchanged durable context revalidates without any source access.
        relation.recheck()
        relation.recheck()
        # A merely requested rebuild (generation bump) does not invalidate:
        # pages still serve the current active table.
        control.request_rebuild(db, asset)
        db.commit()
        relation.recheck()
        # Publishing a new active run replaces the cursor: fail closed.
        replacement, _ = _publish_active(db, asset)
        assert replacement.id != run.id
        with pytest.raises(QueryError) as exc:
            relation.recheck()
        assert (exc.value.status, exc.value.code) == (409, "query_context_changed")
        # Reserved-word / case-sensitive source columns still bind correctly
        # through the column map on the replacement relation too.
        with ExitStack() as stack:
            fresh = providers.resolve_integrated(
                db, admin_user, [("src2", binding)], QueryLimits(), stack)["src2"]
        values = dict(db.execute(sa.select(
            fresh.expressions["Order"], fresh.expressions["select"],
            fresh.expressions["id"]).where(fresh.expressions["id"] == 1)).mappings().one())
        assert values == {"id": 1, "Order": "o-1", "select": "s-1"}


def test_resolve_integrated_rejects_unreadable_but_not_offline_sources(db, admin_user, asset):
    control.ensure_replica(db, asset)
    db.commit()
    binding = Binding(kind="integrated", asset_id=asset.id)
    with ExitStack() as stack:
        with pytest.raises(QueryError) as exc:
            providers.resolve_integrated(db, admin_user, [("src", binding)],
                                         QueryLimits(), stack)
        assert (exc.value.status, exc.value.code) == (409, "replica_not_ready")
    _publish_active(db, asset)
    # A failed-test connection is a source-side lifecycle fact: it must not
    # block reads of the already-complete local replica (offline readability).
    with ExitStack() as stack:
        relation = providers.resolve_integrated(
            db, admin_user, [("src", binding)], QueryLimits(), stack)["src"]
    db.execute(sa.update(Connection).where(Connection.id == asset.connection_id)
               .values(status="error"))
    db.commit()
    # The lifecycle flip fences pages bound to the previous context...
    with pytest.raises(QueryError) as exc:
        relation.recheck()
    assert (exc.value.status, exc.value.code) == (409, "query_context_changed")
    # ...while fresh resolutions keep serving the complete local replica.
    with ExitStack() as stack:
        offline = providers.resolve_integrated(
            db, admin_user, [("src", binding)], QueryLimits(), stack)["src"]
    assert offline.platform is True and offline.connection_key == "platform"
    offline.recheck()


def test_resolve_integrated_survives_unreachable_source_and_serves_stale(db, admin_user, asset):
    # The fixture's source network is a black hole; publishing already proved
    # local-only control. The read path must not dial it for metadata either.
    run, _ = _publish_active(db, asset)
    control.mark_unavailable(db, asset.id, error_code="SOURCE_UNREACHABLE",
                             rebuild_required=False)
    db.commit()
    binding = Binding(kind="integrated", asset_id=asset.id)
    with ExitStack() as stack:
        relation = providers.resolve_integrated(
            db, admin_user, [("src", binding)], QueryLimits(), stack)["src"]
    assert relation.state["stale"] is True
    assert relation.state["replicaStatus"] == "stale"
    relation.recheck()


def test_registration_enqueues_replica_rebuild_atomically(db, admin_user, monkeypatch):
    from app.data_channel.integrated.contracts import DBX_COMMIT, DBX_VERSION, RegisterAsset

    engine = sa.create_engine("sqlite://")
    table = sa.Table("rows", sa.MetaData(),
                     sa.Column("id", sa.BigInteger, primary_key=True),
                     sa.Column("note", sa.String(100)))
    table.create(engine)
    with engine.connect() as conn:
        _, schema, fingerprint = native.reflect(
            conn, SimpleNamespace(schema_name=None, table_name="rows"))
    monkeypatch.setattr(service, "source_engine", lambda *_: engine)
    monkeypatch.setattr(service, "reflect", lambda *_: (table, schema, fingerprint))
    monkeypatch.setattr(service, "check_transactional_table", lambda *_: None)

    class FakeDbx:
        def status(self):
            return {"configured": True, "available": True, "commit": DBX_COMMIT,
                    "version": DBX_VERSION, "reason": None}

        def describe_table(self, connection, schema_name, table_name):
            return {"backend": "dbx-web-http", "commit": DBX_COMMIT,
                    "version": DBX_VERSION, "schema_name": schema_name,
                    "table_name": table_name,
                    "columns": [{"name": column["name"],
                                 "data_type": "bigint" if column["name"] == "id"
                                 else "character varying(100)",
                                 "nullable": column["nullable"],
                                 "primary_key": column["name"] == "id",
                                 "precision": None, "scale": None, "length": None,
                                 "collation": None}
                                for column in schema["columns"]],
                    "primary_key": ["id"]}

    monkeypatch.setattr(service, "dbx", lambda: FakeDbx())
    connection = Connection(id="replica-register-source", name="register fixture",
                            kind="postgres", config={})
    db.add(connection)
    db.commit()
    try:
        result = service.register_asset(db, admin_user, RegisterAsset(
            name="replica registration", connection_id=connection.id,
            schema_name="public", table_name="rows"))
        asset_id = result["id"]
        replica = db.get(IntegratedReplica, asset_id)
        run = db.get(IntegratedReplicaRun, replica.pending_run_id)
        assert replica.status == "initializing" and replica.generation == 1
        assert run.status == "queued" and run.asset_id == asset_id
        assert run.schema_fingerprint == fingerprint
        assert result["replica"]["status"] == "initializing"
        assert result["replica"]["complete"] is False
    finally:
        engine.dispose()


def test_stop_fences_reads_and_rebuild_restores_control_plane(db, admin_user, editor_user, asset):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.data_channel.integrated.router import router
    from app.deps import get_current_user, get_db

    app = FastAPI()
    app.include_router(router, prefix="/api/v2/integrated-datasets")
    app.dependency_overrides[get_db] = lambda: db
    endpoints = f"/api/v2/integrated-datasets/{asset.id}/replica"
    _publish_active(db, asset)
    with TestClient(app) as client:
        app.dependency_overrides[get_current_user] = lambda: admin_user
        assert client.get(endpoints).json()["complete"] is True
        # Non-owner editors may rebuild/sync but stopping replication is owner/admin only.
        app.dependency_overrides[get_current_user] = lambda: editor_user
        assert client.post(endpoints + "/stop").status_code == 403
        app.dependency_overrides[get_current_user] = lambda: admin_user
        stopped = client.post(endpoints + "/stop")
        assert stopped.status_code == 202
        assert stopped.json()["status"] == "stopped" and stopped.json()["cleanup_pending"] is True
        state = client.get(endpoints).json()
        assert state["status"] == "stopped" and state["complete"] is False
        assert state["run_id"] is None and state["row_count"] is None
        with pytest.raises(HTTPException) as exc:
            service.preview_rows(db, admin_user, asset.id, 10)
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "replica_not_ready")
        # Rebuild re-enters the fenced initializer; nothing becomes readable early.
        rebuilt = client.post(endpoints + "/rebuild")
        assert rebuilt.status_code == 202 and rebuilt.json()["status"] == "queued"
        assert client.get(endpoints).json()["complete"] is False


def test_stop_without_replica_is_an_explicit_not_registered_error(db, admin_user, asset):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        service.stop_replica(db, admin_user, asset.id)
    assert (exc.value.status_code, exc.value.detail["code"]) == (404, "REPLICA_NOT_REGISTERED")
