"""Bounded immutable snapshot contracts (SQLite; not production volume proof)."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys
import time as wall_clock
import uuid

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.data_channel.connections.models import Connection
from app.data_channel.datasets import integrated_store as store
from app.data_channel.datasets.lake_gate import IntegratedSnapshotGate, LakeGateError
from app.data_channel.datasets.models import Dataset, DatasetVersion, DatasetVersionEvent, IntegratedSyncRun
from app.data_channel.integrated.contracts import digest
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.sync_tasks import integrated_sync as sync


def column(name, kind, source_type, *, nullable=True, **metadata):
    return {"name": name, "type": kind, "source_type": source_type, "nullable": nullable,
            "precision": None, "scale": None, "timezone": None, "collation": None,
            "length": None, "unsigned": False, **metadata}


def descriptor(schema=None, primary_key=None):
    schema = schema or [column("id", "integer", "BIGINT", nullable=False),
                        column("name", "string", "TEXT")]
    primary_key = ["id"] if primary_key is None else primary_key
    return {"schema": schema, "primary_key": primary_key,
            "schema_fingerprint": digest({"columns": schema, "primary_key": primary_key})}


@pytest.fixture(autouse=True)
def cleanup_relations(db):
    yield
    db.rollback()
    connection = db.connection()
    for name in sa.inspect(connection).get_table_names():
        if name.startswith(store.TABLE_PREFIX):
            # SQLAlchemy's DDL compiler quotes the discovered exact identifier.
            sa.Table(name, sa.MetaData()).drop(connection)
    db.commit()


def make_descriptor(db, user, **kwargs):
    item = descriptor(**kwargs)
    connection = Connection(id=str(uuid.uuid4()), name="fixture", kind="postgres", config={})
    db.add(connection)
    db.flush()
    dataset = Dataset(id=str(uuid.uuid4()), name="fixture", kind="integrated",
                      source_connection_id=connection.id, source_resource="public.fixture")
    db.add(dataset)
    db.flush()
    asset = IntegratedAsset(id=str(uuid.uuid4()), dataset_id=dataset.id, connection_id=connection.id,
                            name="fixture", schema_name="public", table_name="fixture",
                            schema_json={"columns": item["schema"], "primary_key": item["primary_key"]},
                            schema_fingerprint=item["schema_fingerprint"], writable=False, created_by=user.id)
    db.add(asset)
    db.commit()
    item.update(asset_id=asset.id, dataset_id=dataset.id, connection_id=connection.id,
                resource="public.fixture", expected_rows=2,
                snapshot={"id": str(uuid.uuid4()), "consistent": True, "kind": "repeatable_read",
                          "started_at": "2026-10-24T08:00:00+00:00"})
    return item


def completion(run, **overrides):
    return {"complete": True, "row_count": run.expected_rows,
            "schema_fingerprint": run.schema_json["schema_fingerprint"],
            "snapshot_id": run.snapshot_json["id"], **overrides}


def load(db, item, rows=None):
    run, gate = store.begin_snapshot(db, item)
    hasher = store.snapshot_hasher(run)
    if rows is None:
        rows = [{"id": 1, "name": None}, {"id": 2, "name": ""}]
    store.write_snapshot_batch(db, run, gate, rows, hasher)
    return run, hasher


def publish(db, item, rows=None):
    run, hasher = load(db, item, rows)
    version = store.seal_snapshot(db, run, completion(run), hasher.hexdigest())
    return run, version


def test_gate_lossless_nullable_scalar_codec():
    schema = [column("id", "integer", "BIGINT", nullable=False),
              column("amount", "decimal", "NUMERIC(30, 6)", precision=30, scale=6),
              column("flag", "boolean", "BOOLEAN"), column("blob", "binary", "BYTEA"),
              column("stamp", "timestamp", "TIMESTAMP WITH TIME ZONE", timezone=True),
              column("day", "date", "DATE"), column("clock", "time", "TIME", timezone=False),
              column("json", "json", "JSONB"), column("text", "string", "TEXT"),
              column("empty", "string", "TEXT")]
    gate = IntegratedSnapshotGate(descriptor(schema))
    rows = gate.accept_batch([{"id": 2**63 - 1, "amount": Decimal("999999999999999999.123456"),
                               "flag": False, "blob": b"\x00\xff", "stamp": datetime(2026, 10, 24, tzinfo=timezone.utc),
                               "day": date(2026, 10, 24), "clock": time(12, 0, 1, 123456),
                               "json": "a string", "text": None, "empty": ""}])
    assert rows == [{"id": "9223372036854775807", "amount": "999999999999999999.123456",
                     "flag": "false", "blob": "AP8=", "stamp": "2026-10-24T00:00:00+00:00",
                     "day": "2026-10-24", "clock": "12:00:01.123456", "json": '"a string"',
                     "text": None, "empty": ""}]


@pytest.mark.parametrize("kind,source,value,metadata", [
    ("decimal", "NUMERIC(5, 2)", Decimal("123.456"), {"precision": 5, "scale": 2}),
    ("decimal", "NUMERIC(5, 2)", Decimal("1234.5"), {"precision": 5, "scale": 2}),
    ("decimal", "NUMERIC(5, 2)", 1.25, {"precision": 5, "scale": 2}),
    ("integer", "BIGINT", 2**63, {}),
    ("integer", "BIGINT", True, {}),
    ("boolean", "BOOLEAN", "yes", {}),
    ("timestamp", "TIMESTAMP WITH TIME ZONE", datetime(2026, 1, 1), {"timezone": True}),
    ("string", "VARCHAR(3)", "abcd", {"length": 3}),
])
def test_gate_rejects_lossy_declared_values(kind, source, value, metadata):
    gate = IntegratedSnapshotGate(descriptor([column("id", kind, source, nullable=False, **metadata)]))
    with pytest.raises(LakeGateError):
        gate.accept_batch([{"id": value}])


def test_gate_rejects_schema_drift_and_unbounded_batches():
    gate = IntegratedSnapshotGate(descriptor())
    with pytest.raises(LakeGateError):
        gate.accept_batch([{"id": 1, "name": "a", "new": "column"}])
    with pytest.raises(LakeGateError):
        gate.accept_batch([{"id": 1, "name": "a"}] * 1001)
    with pytest.raises(LakeGateError):
        IntegratedSnapshotGate({**descriptor(), "schema_fingerprint": "modified"})


def test_private_snapshot_not_readable_until_atomic_seal(db, admin_user):
    item = make_descriptor(db, admin_user)
    run, hasher = load(db, item)
    assert db.get(Dataset, run.dataset_id).latest_version_id is None
    assert db.query(DatasetVersion).count() == 0
    assert db.query(DatasetVersionEvent).count() == 0
    with pytest.raises(store.IntegratedVersionError):
        store.describe_version(db, run.dataset_id, run.id)
    version = store.seal_snapshot(db, run, completion(run), hasher.hexdigest())
    assert run.status == "sealed"
    assert db.get(Dataset, run.dataset_id).latest_version_id == version.id
    assert db.query(DatasetVersionEvent).one().dataset_version_id == version.id
    metadata = store.describe_version(db, run.dataset_id, version.id)
    assert metadata["complete"] is True and metadata["status"] == "sealed"
    assert metadata["source_schema"] == item["schema"]
    assert metadata["row_count"] == 2
    assert list(store.iter_version_rows(db, run.dataset_id, version.id, batch_size=1)) == [
        [{"id": "1", "name": None}], [{"id": "2", "name": ""}]]


@pytest.mark.parametrize("proof", [{"complete": False}, {"row_count": 1},
                                   {"snapshot_id": "different"}, {"schema_fingerprint": "different"}])
def test_incomplete_eof_cannot_publish(db, admin_user, proof):
    run, hasher = load(db, make_descriptor(db, admin_user))
    with pytest.raises(store.IntegratedVersionError):
        store.seal_snapshot(db, run, completion(run, **proof), hasher.hexdigest())
    assert db.query(DatasetVersion).count() == 0
    assert db.query(DatasetVersionEvent).count() == 0


def test_physical_checksum_is_independent_of_source_hasher(db, admin_user):
    run, hasher = load(db, make_descriptor(db, admin_user))
    table = store._table(run)
    db.execute(table.update().values(name="tampered"))
    db.commit()
    with pytest.raises(store.IntegratedVersionError, match="checksum"):
        store.seal_snapshot(db, run, completion(run), hasher.hexdigest())
    assert db.query(DatasetVersion).count() == 0


def test_database_pk_constraint_and_cleanup(db, admin_user):
    item = make_descriptor(db, admin_user)
    run, gate = store.begin_snapshot(db, item)
    hasher = store.snapshot_hasher(run)
    store.write_snapshot_batch(db, run, gate, [{"id": 1, "name": "first"}], hasher)
    with pytest.raises(IntegrityError):
        store.write_snapshot_batch(db, run, gate, [{"id": 1, "name": "duplicate"}], hasher)
    store.fail_snapshot(db, run.id)
    assert db.get(IntegratedSyncRun, run.id).status == "failed"
    assert not sa.inspect(db.connection()).has_table(run.physical_table)
    assert db.query(DatasetVersion).count() == 0


def test_sealed_relation_rejects_dml(db, admin_user):
    run, version = publish(db, make_descriptor(db, admin_user))
    relation = store.get_version_relation(db, run.dataset_id, version.id)
    for statement in (relation.table.update().values(name="change"), relation.table.delete(),
                      relation.table.insert().values(__row_seq=3, id="3", name="new")):
        with pytest.raises(IntegrityError, match="immutable"):
            db.execute(statement)
        db.rollback()
    store.fail_snapshot(db, run.id)
    assert store.describe_version(db, run.dataset_id, version.id)["row_count"] == 2


def test_new_version_does_not_mutate_pinned_reads_and_stale_run_cannot_seal(db, admin_user):
    item = make_descriptor(db, admin_user)
    first_run, first_version = publish(db, item)
    stale_run, stale_hash = load(db, item)
    second_run, second_version = publish(db, item, [{"id": 1, "name": "new"}, {"id": 2, "name": "next"}])
    assert second_run.physical_table != first_run.physical_table
    with pytest.raises(store.IntegratedVersionError, match="newer"):
        store.seal_snapshot(db, stale_run, completion(stale_run), stale_hash.hexdigest())
    assert list(store.iter_version_rows(db, item["dataset_id"], first_version.id))[0][0]["name"] is None
    assert list(store.iter_version_rows(db, item["dataset_id"], second_version.id))[0][0]["name"] == "new"


def test_keyset_projection_cursor_and_lookup(db, admin_user):
    run, version = publish(db, make_descriptor(db, admin_user))
    queries = []
    def record(_conn, _cursor, sql, *_):
        queries.append(sql)
    sa.event.listen(db.get_bind(), "before_cursor_execute", record)
    try:
        assert list(store.iter_version_rows(db, run.dataset_id, version.id, columns=["name"],
                                            after_row_key=1, batch_size=1)) == [[{"name": ""}]]
        assert list(store.lookup_version_rows(db, run.dataset_id, version.id, column="name", keys=[None])) == [
            [{"id": "1", "name": None}]]
    finally:
        sa.event.remove(db.get_bind(), "before_cursor_execute", record)
    # SQLite emits OFFSET 0 for LIMIT; no nonzero offset pagination is permitted.
    assert all("OFFSET" not in sql or "LIMIT ? OFFSET ?" in sql for sql in queries)
    for size in (0, 1001, True, 1.5):
        with pytest.raises(store.IntegratedVersionError):
            list(store.iter_version_rows(db, run.dataset_id, version.id, batch_size=size))


def test_schema_change_requires_explicit_migration(db, admin_user):
    item = make_descriptor(db, admin_user)
    publish(db, item)
    changed = descriptor([*item["schema"], column("extra", "string", "TEXT")])
    asset = db.get(IntegratedAsset, item["asset_id"])
    asset.schema_fingerprint = changed["schema_fingerprint"]
    db.commit()
    with pytest.raises(store.IntegratedVersionError, match="migration"):
        store.begin_snapshot(db, {**item, **changed})


def fake_source(monkeypatch, item, *, close_error=False):
    @contextmanager
    def open_source(*_args, **_kwargs):
        yield SimpleNamespace(descriptor=item,
                              iter_batches=lambda: iter([[{"id": 1, "name": None}, {"id": 2, "name": ""}]]),
                              completion={"complete": True, "row_count": 2,
                                          "schema_fingerprint": item["schema_fingerprint"],
                                          "snapshot_id": item["snapshot"]["id"]})
        if close_error:
            raise ConnectionError("source snapshot transaction close failed")
    monkeypatch.setattr(sync, "open_snapshot", open_source)


def test_source_context_must_exit_before_sealing(db, admin_user, monkeypatch):
    item = make_descriptor(db, admin_user)
    fake_source(monkeypatch, item, close_error=True)
    with pytest.raises(ConnectionError, match="close failed"):
        sync.sync_integrated_asset(db, item["asset_id"], user=admin_user)
    assert db.query(IntegratedSyncRun).one().status == "failed"
    assert db.query(DatasetVersion).count() == 0
    assert db.query(DatasetVersionEvent).count() == 0


def queue(db, item, user):
    identifier = str(uuid.uuid4())
    run = IntegratedSyncRun(id=identifier, asset_id=item["asset_id"], dataset_id=item["dataset_id"],
                            physical_table=store.TABLE_PREFIX + uuid.UUID(identifier).hex,
                            status="queued", schema_json={"queued": True},
                            snapshot_json={"queued": True, "actor_id": user.id, "batch_size": 1000},
                            expected_rows=0, row_count=0)
    db.add(run)
    db.commit()
    return run


def test_queued_worker_uses_same_run_and_duplicate_cannot_cleanup_active_run(db, admin_user, monkeypatch):
    item = make_descriptor(db, admin_user)
    fake_source(monkeypatch, item)
    run = queue(db, item, admin_user)
    run.status = "starting"
    db.commit()
    with pytest.raises(store.IntegratedVersionError, match="claimed"):
        sync.sync_integrated_asset(db, item["asset_id"], user=admin_user, run_id=run.id)
    assert run.status == "starting"
    run.status = "queued"
    db.commit()
    result = sync.sync_integrated_asset(db, item["asset_id"], user=admin_user, run_id=run.id)
    assert result["run_id"] == run.id and result["complete"] is True
    assert db.query(IntegratedSyncRun).count() == 1
    assert sync.sync_integrated_asset(db, item["asset_id"], user=admin_user, run_id=run.id)["version_id"] == result["version_id"]


def test_revoked_actor_queued_failure_is_durable(db, admin_user, monkeypatch):
    item = make_descriptor(db, admin_user)
    run = queue(db, item, admin_user)
    admin_user.is_active = False
    db.commit()
    with pytest.raises(HTTPException):
        sync.sync_integrated_asset(db, item["asset_id"], user=admin_user, run_id=run.id)
    assert db.get(IntegratedSyncRun, run.id).status == "failed"
    assert db.query(DatasetVersion).count() == 0


def test_deleted_actor_queued_failure_is_durable_without_source_access(db, admin_user, monkeypatch):
    item = make_descriptor(db, admin_user)
    run = queue(db, item, admin_user)
    actor_id = admin_user.id
    db.delete(admin_user)
    db.commit()
    opened = []
    monkeypatch.setattr(sync, "open_snapshot", lambda *_a, **_kw: opened.append(True))
    with pytest.raises(store.IntegratedVersionError, match="actor no longer exists"):
        sync.sync_integrated_asset(db, item["asset_id"], user=None,
                                   batch_size=1000, run_id=run.id, actor_id=actor_id)
    failed = db.get(IntegratedSyncRun, run.id)
    assert failed.status == "failed"
    assert failed.snapshot_json["actor_id"] == actor_id
    assert not opened
    assert db.query(DatasetVersion).count() == 0


def worker_session_factory(db, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    import app.database

    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(app.database, "SessionLocal", factory)
    return factory


@pytest.mark.parametrize("status", ["starting", "loading", "sealed", "failed"])
def test_worker_receipts_duplicate_without_source_or_cleanup(db, admin_user, monkeypatch, status):
    from app.tasks.v2.integrated_sync import integrated_sync_task

    item = make_descriptor(db, admin_user)
    run = queue(db, item, admin_user)
    run.status = status
    db.commit()
    worker_session_factory(db, monkeypatch)
    monkeypatch.setattr(sync, "sync_integrated_asset", lambda *_a, **_kw: pytest.fail("duplicate executed"))
    result = integrated_sync_task(item["asset_id"], admin_user.id, run_id=run.id)
    assert result["run_id"] == run.id and result["status"] == status
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == status
    assert db.query(IntegratedSyncRun).count() == 1


@pytest.mark.parametrize("field,value", [("asset_id", "wrong"), ("actor_id", "wrong"), ("batch_size", 500)])
def test_worker_mismatched_wakeup_cannot_claim_queued_run(db, admin_user, monkeypatch, field, value):
    from app.tasks.v2.integrated_sync import integrated_sync_task

    item = make_descriptor(db, admin_user)
    run = queue(db, item, admin_user)
    worker_session_factory(db, monkeypatch)
    monkeypatch.setattr(sync, "sync_integrated_asset", lambda *_a, **_kw: pytest.fail("mismatch executed"))
    inputs = {"asset_id": item["asset_id"], "actor_id": admin_user.id, "batch_size": 1000, "run_id": run.id}
    inputs[field] = value
    result = integrated_sync_task(**inputs)
    assert result["status"] == "queued" and "does not match" in result["error"]
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == "queued"


@pytest.mark.parametrize("deleted", [False, True])
def test_worker_persists_missing_or_revoked_actor_then_receipts_failure(db, admin_user, monkeypatch, deleted):
    from app.tasks.v2.integrated_sync import integrated_sync_task

    item = make_descriptor(db, admin_user)
    run = queue(db, item, admin_user)
    actor_id = admin_user.id
    if deleted:
        db.delete(admin_user)
    else:
        admin_user.is_active = False
    db.commit()
    worker_session_factory(db, monkeypatch)
    monkeypatch.setattr(sync, "open_snapshot", lambda *_a, **_kw: pytest.fail("unauthorized source access"))
    result = integrated_sync_task(item["asset_id"], actor_id, run_id=run.id)
    assert result["status"] == "failed" and result["complete"] is False
    if deleted:
        assert "actor no longer exists" in result["error"]
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == "failed"
    assert db.query(DatasetVersion).count() == 0
    assert db.query(IntegratedSyncRun).count() == 1


@pytest.mark.parametrize("deleted", [False, True])
def test_worker_acknowledges_confirmed_claim_race_without_failing_other_owner(db, admin_user, monkeypatch, deleted):
    from app.tasks.v2.integrated_sync import integrated_sync_task

    item = make_descriptor(db, admin_user)
    run = queue(db, item, admin_user)
    actor_id = admin_user.id
    if deleted:
        db.delete(admin_user)
        db.commit()
    worker_session_factory(db, monkeypatch)
    real_sync = sync.sync_integrated_asset

    def lose_claim(worker_db, *args, **kwargs):
        # Another delivery claimed after this worker observed queued.
        worker_db.execute(sa.update(IntegratedSyncRun).where(IntegratedSyncRun.id == run.id)
                          .values(status="starting").execution_options(synchronize_session=False))
        worker_db.commit()
        return real_sync(worker_db, *args, **kwargs)

    monkeypatch.setattr(sync, "sync_integrated_asset", lose_claim)
    monkeypatch.setattr(sync, "open_snapshot", lambda *_a, **_kw: pytest.fail("lost owner accessed source"))
    monkeypatch.setattr(sync, "fail_snapshot", lambda *_a, **_kw: pytest.fail("lost owner cleaned run"))
    result = integrated_sync_task(item["asset_id"], actor_id, run_id=run.id)
    assert result["status"] == "starting"
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == "starting"
    assert db.query(DatasetVersion).count() == 0


@pytest.mark.parametrize("durable_status", ["queued", "starting", "loading"])
def test_worker_propagates_unconfirmed_infrastructure_failure(db, admin_user, monkeypatch, durable_status):
    from app.tasks.v2.integrated_sync import integrated_sync_task

    item = make_descriptor(db, admin_user)
    run = queue(db, item, admin_user)
    worker_session_factory(db, monkeypatch)

    def unavailable(worker_db, *_a, **_kw):
        worker_db.execute(sa.update(IntegratedSyncRun).where(IntegratedSyncRun.id == run.id)
                          .values(status=durable_status))
        worker_db.commit()
        raise ConnectionError("platform outcome unconfirmed")

    monkeypatch.setattr(sync, "sync_integrated_asset", unavailable)
    with pytest.raises(ConnectionError, match="outcome unconfirmed"):
        integrated_sync_task(item["asset_id"], admin_user.id, run_id=run.id)
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == durable_status


def test_public_integrated_ports_preserve_raw_text_and_explicit_decoding(db, admin_user):
    from app.data_channel.datasets import service

    item = make_descriptor(db, admin_user)
    run, version = publish(db, item)
    metadata = service.describe_version(db, run.dataset_id, version.id)
    assert metadata["columns"] == ["id", "name"]
    assert "review_status" not in metadata and "review_report_id" not in metadata
    assert list(service.iter_version_rows(db, run.dataset_id, version.id, batch_size=1)) == [
        [{"id": "1", "name": None}], [{"id": "2", "name": ""}],
    ]
    assert store.decode_version_row({"id": "1", "name": None}, metadata["source_schema"]) == {"id": 1, "name": None}
    assert list(service.lookup_version_rows(db, run.dataset_id, version.id, column="id", keys=["1"])) == [
        [{"id": "1", "name": None}],
    ]
    assert service.get_version_relation(db, run.dataset_id, version.id).metadata["version_id"] == version.id


@pytest.mark.parametrize("kind,source_type,empty,other,encoded", [
    ("string", "TEXT", "", "next", ""),
    ("binary", "BYTEA", b"", b"next", ""),
])
def test_empty_nonnull_source_pk_seals_and_duplicate_still_fails(
        db, admin_user, kind, source_type, empty, other, encoded):
    item = make_descriptor(db, admin_user, schema=[column("key", kind, source_type, nullable=False)],
                           primary_key=["key"])
    run, version = publish(db, item, [{"key": empty}, {"key": other}])
    batches = list(store.iter_version_rows(db, run.dataset_id, version.id))
    assert batches[0][0] == {"key": encoded}
    assert store.decode_version_row(batches[0][0], item["schema"]) == {"key": empty}
    duplicate, gate = store.begin_snapshot(db, item)
    with pytest.raises(IntegrityError):
        store.write_snapshot_batch(db, duplicate, gate, [{"key": empty}, {"key": empty}],
                                   store.snapshot_hasher(duplicate))
    db.rollback()
    with pytest.raises(LakeGateError, match="NULL is not allowed"):
        gate.accept_batch([{"key": None}])


def test_integrated_public_pk_uses_frozen_manifest_not_current_dataset(db, admin_user):
    from app.data_channel.datasets import service

    item = make_descriptor(db, admin_user)
    run, version = publish(db, item)
    dataset = db.get(Dataset, run.dataset_id)
    dataset.schema_json = {**dataset.schema_json, "primary_key_columns": ["name"], "primary_key": "name"}
    db.commit()
    assert service.describe_version(db, run.dataset_id, version.id)["primary_key_columns"] == ["id"]


def test_json_null_is_distinct_from_sql_null_and_user_objects(db, admin_user):
    from app.data_channel.integrated.contracts import JsonText

    precise = '{"number": 0.12345678901234567890123456789012345678901234567890, "verywide": 1234567890123456789012345678901234567890}'
    item = make_descriptor(db, admin_user, schema=[
        column("id", "integer", "BIGINT", nullable=False),
        column("required", "json", "JSONB", nullable=False, encoding="json_text"),
        column("optional", "json", "JSONB", encoding="json_text"),
    ])
    rows = [
        {"id": 1, "required": JsonText("null"), "optional": None},
        {"id": 2, "required": {"$json": None}, "optional": {"nested": [None]}},
        {"id": 3, "required": JsonText(precise), "optional": JsonText('"json scalar string"')},
    ]
    item["expected_rows"] = len(rows)
    run, version = publish(db, item, rows)
    stored = [row for batch in store.iter_version_rows(db, run.dataset_id, version.id) for row in batch]
    assert stored == [
        {"id": "1", "required": "null", "optional": None},
        {"id": "2", "required": '{"$json":null}', "optional": '{"nested":[null]}'},
        {"id": "3", "required": precise, "optional": '"json scalar string"'},
    ]
    decoded = [store.decode_version_row(row, item["schema"]) for row in stored]
    assert decoded[0]["required"] == "null" and isinstance(decoded[0]["required"], JsonText)
    assert decoded[0]["optional"] is None
    assert decoded[1] == {"id": 2, "required": JsonText('{"$json":null}'), "optional": JsonText('{"nested":[null]}')}
    assert decoded[2]["required"] == precise and isinstance(decoded[2]["required"], JsonText)
    assert decoded[2]["optional"] == rows[2]["optional"]
    with pytest.raises(LakeGateError, match="NULL is not allowed"):
        IntegratedSnapshotGate(item).accept_batch([{"id": 3, "required": None, "optional": None}])


def test_physical_scan_fetches_one_row_and_caps_wide_batches():
    payload = "宽" * (1024**2 // 3)
    table = sa.Table("fixture", sa.MetaData(), sa.Column(store.ROW_ORDINAL, sa.BigInteger),
                     sa.Column("payload", sa.Text))

    class Rows:
        count = 0
        closed = False

        def mappings(self):
            return self

        def fetchone(self):
            if self.count == 1001:
                return None
            self.count += 1
            return {store.ROW_ORDINAL: self.count, "payload": payload}

        def close(self):
            self.closed = True

    rows = Rows()

    class DB:
        def execute(self, statement):
            options = statement.get_execution_options()
            assert options["stream_results"] is True
            assert options["yield_per"] == options["max_row_buffer"] == 1
            return rows

    seen = 0
    for batch in store._iter_physical(DB(), table):
        assert 0 < len(batch) < 1000
        assert sum(store._physical_row_bytes(row) for row in batch) <= store.MAX_BATCH_BYTES
        seen += len(batch)
    assert seen == 1001 and rows.closed


def test_seal_read_and_lookup_keep_byte_cap_and_exact_eof(db, admin_user, monkeypatch):
    monkeypatch.setattr(store, "MAX_BATCH_BYTES", 96)
    item = make_descriptor(db, admin_user)
    item["expected_rows"] = 7
    rows = [{"id": index, "name": "宽" * 15} for index in range(7)]
    run, version = publish(db, item, rows)
    for batches in (
        list(store.iter_version_rows(db, run.dataset_id, version.id)),
        list(store.lookup_version_rows(db, run.dataset_id, version.id, column="id", keys=[str(i) for i in range(7)])),
    ):
        assert sum(map(len, batches)) == 7
        assert len(batches) > 1
        assert [row for batch in batches for row in batch] == [
            {"id": str(row["id"]), "name": row["name"]} for row in rows]
        assert all(sum(store._physical_row_bytes(row) for row in batch) <= 96 for batch in batches)


# ---------------------------------------------------------------------------
# Crash recovery: lease ownership, safe disposal and full-snapshot retry
# ---------------------------------------------------------------------------

def attach_owner(db, run, actor_id, *, token="owner-token", expired=False,
                 seconds=90):
    """Attach a real owner lease to a run the way the sync worker does."""
    now = datetime.now(timezone.utc)
    lease = store.new_owner_lease(actor_id=actor_id, token=token, seconds=seconds)
    lease = {
        **lease,
        "expires_at": (now - timedelta(seconds=5) if expired
                       else now + timedelta(seconds=seconds)).isoformat(),
    }
    run.snapshot_json = {
        **run.snapshot_json, "actor_id": actor_id, "batch_size": 1000,
        "lease": lease,
    }
    db.commit()
    return lease


def recovery_audit_rows(db):
    from app.data_channel.datasets.models import IntegratedSyncRecovery
    return db.query(IntegratedSyncRecovery).all()


def test_claim_stores_lease_and_sealed_run_can_never_be_recovered(db, admin_user, monkeypatch):
    item = make_descriptor(db, admin_user)
    fake_source(monkeypatch, item)
    run = queue(db, item, admin_user)
    result = sync.sync_integrated_asset(db, item["asset_id"], user=admin_user, run_id=run.id)
    db.expire_all()
    sealed = db.get(IntegratedSyncRun, run.id)
    lease = store.lease_of(sealed)
    assert sealed.status == "sealed" and result["version_id"] == sealed.version_id
    assert lease is not None and lease["actor_id"] == admin_user.id
    assert lease["token"] and lease["pid"] > 0
    assert store.lease_expires_at(sealed) is not None
    assert store.renew_run_lease(db, sealed.id, lease["token"], now=datetime.now(timezone.utc)) is False
    with pytest.raises(store.IntegratedVersionError, match="sealed"):
        store.recover_abandoned_snapshot(
            db, sealed.id, operator="ops", reason="attempted reclaim")
    assert db.query(DatasetVersion).count() == 1
    assert store.describe_version(db, run.dataset_id, result["version_id"])["complete"] is True


def test_live_owner_refuses_takeover_and_keeps_redelivery_wakeup(db, admin_user, monkeypatch):
    from app.tasks.v2.integrated_sync import IntegratedLeaseActive, integrated_sync_task

    item = make_descriptor(db, admin_user)
    run, gate = store.begin_snapshot(db, item)
    store.write_snapshot_batch(db, run, gate, [{"id": 1, "name": "a"}], store.snapshot_hasher(run))
    attach_owner(db, run, admin_user.id, expired=False, seconds=90)

    with pytest.raises(store.IntegratedVersionError, match="live"):
        store.recover_abandoned_snapshot(db, run.id, operator="ops", reason="guess")
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == "loading"
    assert sa.inspect(db.connection()).has_table(run.physical_table)
    assert recovery_audit_rows(db) == []
    assert db.query(DatasetVersion).count() == 0

    worker_session_factory(db, monkeypatch)
    with pytest.raises(IntegratedLeaseActive) as raised:
        integrated_sync_task(item["asset_id"], admin_user.id, run_id=run.id)
    assert raised.value.retry_after > 0
    db.expire_all()
    unchanged = db.get(IntegratedSyncRun, run.id)
    assert unchanged.status == "loading" and unchanged.row_count == 1
    assert sa.inspect(db.connection()).has_table(run.physical_table)
    assert db.query(DatasetVersion).count() == 0


def test_unverifiable_owner_is_reported_instead_of_acked_as_recovered(db, admin_user, monkeypatch):
    from app.tasks.v2.integrated_sync import integrated_sync_task

    item = make_descriptor(db, admin_user)
    run, gate = store.begin_snapshot(db, item)
    store.write_snapshot_batch(db, run, gate, [{"id": 1, "name": "a"}], store.snapshot_hasher(run))
    run.snapshot_json = {**run.snapshot_json, "actor_id": admin_user.id, "batch_size": 1000}
    db.commit()
    worker_session_factory(db, monkeypatch)
    result = integrated_sync_task(item["asset_id"], admin_user.id, run_id=run.id)
    assert result["status"] == "loading" and result["complete"] is False
    assert "verifiable owner lease" in result["error"]
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == "loading"
    assert sa.inspect(db.connection()).has_table(run.physical_table)
    assert recovery_audit_rows(db) == []
    with pytest.raises(store.IntegratedVersionError, match="cannot be proven"):
        store.recover_abandoned_snapshot(db, run.id, operator="ops", reason="guess")


def test_expired_owner_is_recovered_atomically_audited_and_idempotent(db, admin_user):
    crashed = make_descriptor(db, admin_user)
    crashed_run, gate = store.begin_snapshot(db, crashed)
    hasher = store.snapshot_hasher(crashed_run)
    store.write_snapshot_batch(db, crashed_run, gate, [{"id": 1, "name": "a"}], hasher)
    attach_owner(db, crashed_run, admin_user.id, token="dead-owner", expired=True)

    other_run, other_version = publish(db, make_descriptor(db, admin_user))
    other_table = other_run.physical_table

    receipt = store.recover_abandoned_snapshot(
        db, crashed_run.id, operator="ops:alice", reason="worker SIGKILL proven")
    assert receipt["status"] == "failed" and receipt["recovered"] is True
    assert receipt["restart_required"] is True and receipt["complete"] is False
    assert receipt["original_status"] == "loading" and receipt["row_count"] == 1
    assert receipt["ownership"]["method"] == "owner_lease_expired"
    assert receipt["ownership"]["owner_token"] == "dead-owner"
    assert receipt["physical_table"] == crashed_run.physical_table
    assert receipt["table_existed"] is True and receipt["table_dropped"] is True
    assert receipt["idempotent"] is False

    db.expire_all()
    failed = db.get(IntegratedSyncRun, crashed_run.id)
    assert failed.status == "failed" and failed.version_id is None
    assert failed.error.startswith("INTEGRATED_SNAPSHOT_ABANDONED")
    assert not sa.inspect(db.connection()).has_table(crashed_run.physical_table)
    audit = recovery_audit_rows(db)
    assert len(audit) == 1
    assert audit[0].run_id == crashed_run.id and audit[0].recovered_by == "ops:alice"
    assert audit[0].original_status == "loading"
    assert audit[0].physical_table == crashed_run.physical_table
    assert audit[0].table_dropped is True

    # A late worker cleanup must never overwrite the first durable receipt.
    store.fail_snapshot(db, crashed_run.id)
    db.expire_all()
    assert len(recovery_audit_rows(db)) == 1
    assert db.get(IntegratedSyncRun, crashed_run.id).status == "failed"

    repeated = store.recover_abandoned_snapshot(
        db, crashed_run.id, operator="ops:bob", reason="second attempt")
    assert repeated["idempotent"] is True
    assert repeated["recovered_at"] == receipt["recovered_at"]
    assert repeated["operator"] == "ops:alice"
    assert len(recovery_audit_rows(db)) == 1

    # The sealed version and its immutable relation are untouched.
    assert db.get(DatasetVersion, other_version.id) is not None
    assert sa.inspect(db.connection()).has_table(other_table)
    assert store.describe_version(db, other_run.dataset_id, other_version.id)["row_count"] == 2
    assert db.query(DatasetVersionEvent).count() == 1


def test_operator_evidence_must_name_the_exact_owner_and_is_audited(db, admin_user):
    item = make_descriptor(db, admin_user)
    run, gate = store.begin_snapshot(db, item)
    store.write_snapshot_batch(db, run, gate, [{"id": 1, "name": "a"}], store.snapshot_hasher(run))
    lease = attach_owner(db, run, admin_user.id, token="live-token", expired=False, seconds=90)
    observed = datetime.now(timezone.utc).isoformat()
    with pytest.raises(store.IntegratedVersionError, match="does not identify"):
        store.recover_abandoned_snapshot(
            db, run.id, operator="ops", reason="proof mismatch",
            proof={"kind": "worker_terminated", "reference": "pid:1",
                   "observed_at": observed, "owner_token": "someone-else"})
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == "loading"
    receipt = store.recover_abandoned_snapshot(
        db, run.id, operator="ops", reason="worker terminated out of band",
        proof={"kind": "worker_terminated", "reference": "pid:4242",
               "observed_at": observed, "owner_token": lease["token"]})
    assert receipt["recovered"] is True
    assert receipt["ownership"]["method"] == "operator_evidence"
    assert receipt["ownership"]["kind"] == "worker_terminated"
    assert receipt["ownership"]["reference"] == "pid:4242"
    assert recovery_audit_rows(db)[0].ownership_method == "operator_evidence"
    assert not sa.inspect(db.connection()).has_table(run.physical_table)


def test_recovery_sweep_skips_live_and_unverifiable_runs(db, admin_user):
    expired_item = make_descriptor(db, admin_user)
    expired_run, _ = store.begin_snapshot(db, expired_item)
    attach_owner(db, expired_run, admin_user.id, token="sweep-dead", expired=True)
    live_item = make_descriptor(db, admin_user)
    live_run, _ = store.begin_snapshot(db, live_item)
    attach_owner(db, live_run, admin_user.id, token="sweep-live", expired=False, seconds=90)
    unknown_item = make_descriptor(db, admin_user)
    unknown_run, _ = store.begin_snapshot(db, unknown_item)

    receipts = store.recover_expired_snapshots(db, operator="ops:sweep")
    assert [item["run_id"] for item in receipts] == [expired_run.id]
    assert receipts[0]["recovered"] is True
    db.expire_all()
    assert db.get(IntegratedSyncRun, expired_run.id).status == "failed"
    assert db.get(IntegratedSyncRun, live_run.id).status == "loading"
    assert db.get(IntegratedSyncRun, unknown_run.id).status == "loading"
    assert sa.inspect(db.connection()).has_table(live_run.physical_table)
    assert sa.inspect(db.connection()).has_table(unknown_run.physical_table)


def test_recovery_cli_exposes_the_management_entry_point(db, admin_user, monkeypatch, capsys):
    item = make_descriptor(db, admin_user)
    run, _ = store.begin_snapshot(db, item)
    attach_owner(db, run, admin_user.id, token="cli-dead", expired=True)
    worker_session_factory(db, monkeypatch)
    assert sync.main([
        "recover", "--run-id", run.id, "--operator", "ops:cli",
        "--reason", "process died",
    ]) == 0
    receipt = __import__("json").loads(capsys.readouterr().out)
    assert receipt["run_id"] == run.id and receipt["recovered"] is True
    db.expire_all()
    assert db.get(IntegratedSyncRun, run.id).status == "failed"
    assert len(recovery_audit_rows(db)) == 1


# ---------------------------------------------------------------------------
# Real process fault injection: SIGKILL a live sync worker, then recover
# ---------------------------------------------------------------------------

CRASH_WORKER = r'''
import json, os, sys, time
from contextlib import contextmanager

payload = json.load(open(sys.argv[1]))
marker = sys.argv[2]
os.environ["DATABASE_URL"] = "sqlite:///" + payload["db_path"]
sys.path.insert(0, payload["backend_path"])

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.model_registry import import_all_models
import_all_models()
from app.auth.models import User
from app.data_channel.sync_tasks import integrated_sync as sync

item = payload["descriptor"]
rows = payload["rows"]

@contextmanager
def open_source(*_args, **_kwargs):
    class Source:
        descriptor = item
        def iter_batches(self):
            yield rows
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("first-batch-committed")
            time.sleep(300)
    source = Source()
    source.completion = {
        "complete": True, "row_count": len(rows),
        "schema_fingerprint": item["schema_fingerprint"],
        "snapshot_id": item["snapshot"]["id"],
    }
    yield source

sync.open_snapshot = open_source
sync.authorize = lambda *_args, **_kwargs: None

engine = create_engine("sqlite:///" + payload["db_path"],
                       connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
with Session() as db:
    user = db.get(User, payload["actor_id"])
    sync.sync_integrated_asset(
        db, payload["asset_id"], user=user, batch_size=1000,
        run_id=payload["run_id"], lease_seconds=payload["lease_seconds"],
        heartbeat_seconds=payload["heartbeat_seconds"])
'''


def crash_engine(tmp_path):
    from app.database import Base
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'integrated-crash.db'}",
        connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def seed_crash_run(Session, *, expected_rows=3, actor_id=None):
    from app.auth.models import User

    item = descriptor(schema=[column("id", "integer", "BIGINT", nullable=False),
                              column("name", "string", "TEXT")], primary_key=["id"])
    item["expected_rows"] = expected_rows
    item["snapshot"] = {"id": str(uuid.uuid4()), "consistent": True,
                        "kind": "repeatable_read",
                        "started_at": "2026-10-24T08:00:00+00:00"}
    with Session() as db:
        user = User(id=actor_id or str(uuid.uuid4()), username="crash-admin",
                    email="crash@test.com", password_hash="x", role="admin",
                    is_active=True)
        connection = Connection(id=str(uuid.uuid4()), name="crash", kind="postgres", config={})
        dataset = Dataset(id=str(uuid.uuid4()), name="crash", kind="integrated",
                          source_connection_id=connection.id,
                          source_resource="public.crash")
        asset = IntegratedAsset(
            id=str(uuid.uuid4()), dataset_id=dataset.id, connection_id=connection.id,
            name="crash", schema_name="public", table_name="crash",
            schema_json={"columns": item["schema"], "primary_key": item["primary_key"]},
            schema_fingerprint=item["schema_fingerprint"], writable=False,
            created_by=user.id)
        run = queue_for(db, asset, dataset, user)
        db.add_all([user, connection, dataset, asset, run])
        db.commit()
        item.update(asset_id=asset.id, dataset_id=dataset.id,
                    connection_id=connection.id, resource="public.crash",
                    snapshot=item["snapshot"])
        return {"item": item, "actor_id": user.id, "run_id": run.id,
                "rows": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"},
                         {"id": 3, "name": "c"}]}


def queue_for(db, asset, dataset, user):
    identifier = str(uuid.uuid4())
    run = IntegratedSyncRun(
        id=identifier, asset_id=asset.id, dataset_id=dataset.id,
        physical_table=store.TABLE_PREFIX + uuid.UUID(identifier).hex,
        status="queued", schema_json={"queued": True},
        snapshot_json={"queued": True, "actor_id": user.id, "batch_size": 1000},
        expected_rows=0, row_count=0)
    db.add(run)
    db.flush()
    return run


def start_crash_worker(tmp_path, setup, *, db_path, lease_seconds=2,
                       heartbeat_seconds=60):
    import json as json_module
    payload = {
        "descriptor": setup["item"], "asset_id": setup["item"]["asset_id"],
        "actor_id": setup["actor_id"], "run_id": setup["run_id"],
        "rows": setup["rows"], "db_path": str(db_path),
        "backend_path": str(Path(__file__).resolve().parents[3]),
        "lease_seconds": lease_seconds, "heartbeat_seconds": heartbeat_seconds,
    }
    payload_path = tmp_path / f"payload-{uuid.uuid4().hex}.json"
    marker = tmp_path / f"marker-{uuid.uuid4().hex}"
    payload_path.write_text(json_module.dumps(payload), encoding="utf-8")
    # Sanitized child environment: no inherited DB/secret/stack variables, only
    # this test's own temporary SQLite file plus the test profile toggles.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "ENVIRONMENT": "test",
        "DATABASE_URL": f"sqlite:///{db_path}",
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIPELINE_TASK_CACHE_ENABLED": "false",
        "ONTOLOGY_DETAIL_CACHE_ENABLED": "false",
        "ONTOLOGY_NETWORK_CACHE_ENABLED": "false",
        "WORLD_MODEL_CACHE_ENABLED": "false",
        "ONTOLOGY_VERSION_TREE_CACHE_ENABLED": "false",
        "ONTOLOGY_LIST_CACHE_ENABLED": "false",
        "PLATFORM_STATS_CACHE_ENABLED": "false",
        "DATASET_EVENT_DISPATCH_MODE": "sync",
    }
    process = subprocess.Popen(
        [sys.executable, "-c", CRASH_WORKER, str(payload_path), str(marker)],
        cwd=Path(__file__).resolve().parents[4], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = wall_clock.monotonic() + 30
    while not marker.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(
                "crash worker exited before its first committed batch:\n"
                f"{stdout.decode(errors='replace')}\n{stderr.decode(errors='replace')}")
        if wall_clock.monotonic() > deadline:
            process.kill()
            process.communicate(timeout=5)
            raise AssertionError("crash worker never committed its first batch")
        wall_clock.sleep(0.05)
    return process, marker


def kill_and_wait(process):
    process.kill()  # SIGKILL: no Python cleanup, exactly like a power loss
    process.communicate(timeout=10)
    assert process.returncode == -9


def wait_lease_expiry(Session, run_id):
    with Session() as db:
        run = db.get(IntegratedSyncRun, run_id)
        expires = store.lease_expires_at(run)
    if expires is not None:
        remaining = (expires - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            wall_clock.sleep(remaining + 0.3)


def test_real_sigkill_worker_leaves_private_run_then_redelivery_recovers_and_retries(
        tmp_path, monkeypatch):
    from app.tasks.v2.integrated_sync import integrated_sync_task

    engine, Session = crash_engine(tmp_path)
    setup = seed_crash_run(Session)
    item, run_id, actor_id = setup["item"], setup["run_id"], setup["actor_id"]
    process, _marker = start_crash_worker(
        tmp_path, setup, db_path=tmp_path / "integrated-crash.db", lease_seconds=2)
    kill_and_wait(process)

    with Session() as db:
        crashed = db.get(IntegratedSyncRun, run_id)
        assert crashed.status == "loading"
        assert crashed.row_count == 3 and crashed.version_id is None
        assert crashed.checksum is None
        assert crashed.expected_rows == 3
        assert crashed.snapshot_json["lease"]["pid"] == process.pid
        assert sa.inspect(db.connection()).has_table(crashed.physical_table)
        assert db.query(DatasetVersion).count() == 0
        assert db.query(DatasetVersionEvent).count() == 0
        physical_table = crashed.physical_table

    wait_lease_expiry(Session, run_id)
    monkeypatch.setattr("app.database.SessionLocal", Session)

    # Duplicate delivery: first disposes the provably dead owner, the second
    # returns the same durable receipt without any further mutation.
    first = integrated_sync_task(item["asset_id"], actor_id, run_id=run_id)
    assert first["status"] == "failed" and first["complete"] is False
    assert first["recovery"]["recovered"] is True
    assert first["recovery"]["ownership"]["method"] == "owner_lease_expired"
    assert first["recovery"]["physical_table"] == physical_table
    second = integrated_sync_task(item["asset_id"], actor_id, run_id=run_id)
    assert second["recovery"]["idempotent"] is True
    assert second["recovery"]["recovered_at"] == first["recovery"]["recovered_at"]

    with Session() as db:
        failed = db.get(IntegratedSyncRun, run_id)
        assert failed.status == "failed" and failed.version_id is None
        assert not sa.inspect(db.connection()).has_table(physical_table)
        assert db.query(DatasetVersion).count() == 0
        assert len(recovery_audit_rows(db)) == 1

    # A safe full retry uses a brand new run, private relation and snapshot.
    with Session() as db:
        dataset = db.get(Dataset, item["dataset_id"])
        asset = db.get(IntegratedAsset, item["asset_id"])
        user = db.get(__import__("app.auth.models", fromlist=["User"]).User, actor_id)
        retry = queue_for(db, asset, dataset, user)
        retry_id = retry.id
        db.commit()

    @contextmanager
    def fast_source(*_args, **_kwargs):
        snapshot = {**item["snapshot"], "id": str(uuid.uuid4())}
        retry_item = {**item, "snapshot": snapshot}
        rows = setup["rows"]
        source = SimpleNamespace(
            descriptor=retry_item,
            iter_batches=lambda: iter([rows]),
            completion={"complete": True, "row_count": len(rows),
                        "schema_fingerprint": item["schema_fingerprint"],
                        "snapshot_id": snapshot["id"]},
        )
        yield source

    monkeypatch.setattr(sync, "open_snapshot", fast_source)
    monkeypatch.setattr(sync, "authorize", lambda *_args, **_kwargs: None)
    with Session() as db:
        user = db.get(__import__("app.auth.models", fromlist=["User"]).User, actor_id)
        sealed = sync.sync_integrated_asset(
            db, item["asset_id"], user=user, run_id=retry_id)
        assert sealed["run_id"] == retry_id and sealed["complete"] is True
        new_run = db.get(IntegratedSyncRun, retry_id)
        old_run = db.get(IntegratedSyncRun, run_id)
        assert new_run.status == "sealed" and new_run.version_id == sealed["version_id"]
        assert new_run.physical_table != physical_table
        assert sa.inspect(db.connection()).has_table(new_run.physical_table)
        assert old_run.status == "failed" and old_run.version_id is None
        assert db.get(Dataset, item["dataset_id"]).latest_version_id == sealed["version_id"]
        assert db.query(DatasetVersion).count() == 1
    engine.dispose()


def test_real_sigkill_with_live_lease_blocks_worker_but_operator_proof_recovers(
        tmp_path, monkeypatch):
    from app.tasks.v2.integrated_sync import IntegratedLeaseActive, integrated_sync_task

    engine, Session = crash_engine(tmp_path)
    setup = seed_crash_run(Session)
    item, run_id, actor_id = setup["item"], setup["run_id"], setup["actor_id"]
    process, _marker = start_crash_worker(
        tmp_path, setup, db_path=tmp_path / "integrated-crash.db",
        lease_seconds=60, heartbeat_seconds=60)
    monkeypatch.setattr("app.database.SessionLocal", Session)
    try:
        with pytest.raises(IntegratedLeaseActive) as raised:
            integrated_sync_task(item["asset_id"], actor_id, run_id=run_id)
        assert raised.value.retry_after > 0
        with Session() as db:
            live = db.get(IntegratedSyncRun, run_id)
            assert live.status == "loading" and live.version_id is None
            assert sa.inspect(db.connection()).has_table(live.physical_table)
            assert db.query(DatasetVersion).count() == 0
            token = store.lease_of(live)["token"]
    finally:
        kill_and_wait(process)

    with Session() as db:
        receipt = store.recover_abandoned_snapshot(
            db, run_id, operator="ops:oncall",
            reason="SIGKILL of the lease owner confirmed out of band",
            proof={"kind": "worker_terminated", "reference": f"pid:{process.pid}",
                   "observed_at": datetime.now(timezone.utc).isoformat(),
                   "owner_token": token},
            now=datetime.now(timezone.utc))
        assert receipt["recovered"] is True
        assert receipt["ownership"]["method"] == "operator_evidence"
        assert receipt["ownership"]["reference"] == f"pid:{process.pid}"
        assert not sa.inspect(db.connection()).has_table(receipt["physical_table"])
        assert recovery_audit_rows(db)[0].ownership_method == "operator_evidence"
    engine.dispose()
