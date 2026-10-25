"""Bounded native source contracts; real MySQL/PG acceptance is separate E2E."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from types import SimpleNamespace
import uuid

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.engine import make_url
from fastapi import HTTPException

from app.data_channel.integrated import dbx_adapter as dbx_adapter_module
from app.data_channel.integrated import native, service
from app.data_channel.integrated.contracts import (DBX_COMMIT, DBX_VERSION, RegisterAsset, RowMutation,
                                                  digest, row_token, wire_value)
from app.data_channel.integrated.dbx_adapter import DbxMetadataAdapter
from app.data_channel.integrated.models import IntegratedAsset, IntegratedMutation
from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.shared.config import settings


def test_lossless_wire_codec_and_token():
    row = {"id": 2**63 - 1, "amount": Decimal("999999999999.123456"),
           "blob": b"\x00\xff", "day": date(2026, 10, 24), "null": None}
    result = wire_value(row)
    assert result == {"id": {"$integer": "9223372036854775807"},
                      "amount": {"$decimal": "999999999999.123456"},
                      "blob": {"$binary": "AP8="}, "day": "2026-10-24", "null": None}
    assert row_token(row, "a") != row_token(row, "b")
    assert row_token(row, "a") == row_token(dict(reversed(list(row.items()))), "a")


def test_dbx_missing_is_explicit_and_not_native_fallback(monkeypatch):
    monkeypatch.setattr(settings, "integrated_dbx_url", "")
    monkeypatch.setattr(settings, "integrated_dbx_password", "")
    adapter = DbxMetadataAdapter()
    status = adapter.status()
    assert status == {"configured": False, "available": False, "commit": DBX_COMMIT,
                      "version": DBX_VERSION, "reason": "DBX_NOT_CONFIGURED"}
    assert adapter.configured is False
    with pytest.raises(HTTPException) as exc:
        adapter.describe_table(None, "public", "t")
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "DBX_NOT_CONFIGURED"


def _scope():
    engine = sa.create_engine("sqlite://")
    conn = engine.connect()
    table = sa.Table("source", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("group_name", sa.String(30)), sa.Column("amount", sa.Integer))
    table.create(conn)
    conn.execute(table.insert(), [{"id": n, "group_name": "a" if n < 3 else "b", "amount": n} for n in range(5)])
    asset = SimpleNamespace(id="a", schema_name=None, table_name="source")
    _, schema, fingerprint = native.reflect(conn, asset)
    asset.schema_fingerprint = fingerprint
    scope = native.NativeSourceSession(conn, [asset], {"a": {}}, {"id": "snapshot", "started_at": "now"})
    return engine, conn, scope


def test_read_completeness_counts_result_not_base_table():
    engine, conn, scope = _scope()
    try:
        table = scope.tables["a"]
        statement = sa.select(table.c.group_name, sa.func.sum(table.c.amount).label("total")).group_by(table.c.group_name)
        batches = list(scope.iter_batches(statement, batch_size=1))
        assert sum(len(b) for b in batches) == 2
        assert scope.completion["expected_rows"] == 2
        assert scope.completion["row_count"] == 2
        assert scope.completion["complete"] is True
        assert list(scope.iter_batches(sa.select(table).limit(1)))[0][0]["id"] == 0
        assert scope.completion["expected_rows"] == 1
    finally:
        conn.close()
        engine.dispose()


def test_early_stream_close_cannot_claim_complete():
    engine, conn, scope = _scope()
    try:
        iterator = scope.iter_batches(sa.select(scope.tables["a"]), batch_size=2)
        assert len(next(iterator)) == 2
        iterator.close()
        assert scope.completion["complete"] is False
        assert scope.completion["row_count"] == 2
    finally:
        conn.close()
        engine.dispose()


def test_source_port_rejects_text_and_unauthorized_table():
    engine, conn, scope = _scope()
    try:
        with pytest.raises(HTTPException, match="") as exc:
            list(scope.iter_batches(sa.text("select * from source")))
        assert exc.value.detail["code"] == "READ_SELECT_REQUIRED"
        other = sa.Table("other", sa.MetaData(), sa.Column("id", sa.Integer))
        with pytest.raises(HTTPException) as exc:
            list(scope.iter_batches(sa.select(other)))
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            list(scope.iter_batches(sa.select(scope.tables["a"]).where(sa.text("1=1"))))
        assert exc.value.detail["code"] == "RAW_SQL_DENIED"
    finally:
        conn.close()
        engine.dispose()


def test_decode_never_rounds_decimal_or_accepts_float_for_money():
    table = sa.Table("t", sa.MetaData(), sa.Column("money", sa.Numeric(30, 6)),
                     sa.Column("id", sa.BigInteger), sa.Column("blob", sa.LargeBinary))
    assert service._decode(table.c.money, {"$decimal": "123456789012345678.123456"}) == Decimal("123456789012345678.123456")
    assert service._decode(table.c.id, {"$integer": "9223372036854775807"}) == 2**63 - 1
    assert service._decode(table.c.blob, {"$binary": "AP8="}) == b"\x00\xff"
    for bad in (1.2, "1.0000001", "NaN"):
        with pytest.raises(HTTPException) as exc:
            service._decode(table.c.money, bad)
        assert exc.value.status_code == 422


def _asset(db, user):
    source = Connection(id="source", name="source", kind="postgres", config={})
    dataset = Dataset(id="dataset", name="source", kind="integrated", source_connection_id="source", source_resource="public.rows")
    db.add(source)
    db.flush()
    db.add(dataset)
    db.flush()
    asset = IntegratedAsset(id="asset", dataset_id="dataset", connection_id="source", name="source",
                            schema_name="public", table_name="rows", schema_json={"columns": [], "primary_key": ["id"]},
                            schema_fingerprint="schema", writable=True, created_by=user.id)
    db.add(asset)
    db.commit()
    return asset


def test_mutation_requires_complete_pk_and_expected_token(db, admin_user):
    asset = _asset(db, admin_user)
    for body, code in (
        (RowMutation(key={}, values={"name": "a"}, idempotency_key="1"), "COMPLETE_PRIMARY_KEY_REQUIRED"),
        (RowMutation(key={"id": 1}, values={"id": 2}, idempotency_key="2"), "PRIMARY_KEY_CHANGE_DENIED"),
        (RowMutation(key={"id": 1}, values={"name": "a"}, idempotency_key="3"), "EXPECTED_ROW_TOKEN_REQUIRED"),
    ):
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "update", body)
        assert exc.value.detail["code"] == code
    assert db.query(IntegratedMutation).count() == 0


def test_unknown_and_pending_idempotency_are_never_replayed(db, admin_user, monkeypatch):
    asset = _asset(db, admin_user)
    body = RowMutation(key={"id": 1}, values={"name": "a"}, idempotency_key="reserved")
    request_hash = digest({"operation": "insert", "body": body.model_dump(exclude={"idempotency_key"})})
    record = IntegratedMutation(id="mutation", asset_id=asset.id, actor_id=admin_user.id,
                                operation="insert", idempotency_key="reserved", request_hash=request_hash,
                                key_hash="key", schema_fingerprint="schema", status="pending", result_json={})
    db.add(record)
    db.commit()
    monkeypatch.setattr(service, "source_engine", lambda *_: pytest.fail("reserved write must not reach source"))
    for status in ("pending", "unknown"):
        record.status = status
        db.commit()
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "insert", body)
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "UNKNOWN_REQUIRES_RECONCILIATION"


def test_replay_hash_mismatch_and_applied_receipt(db, admin_user):
    asset = _asset(db, admin_user)
    body = RowMutation(key={"id": 1}, values={}, idempotency_key="applied")
    request_hash = digest({"operation": "insert", "body": body.model_dump(exclude={"idempotency_key"})})
    record = IntegratedMutation(id="mutation", asset_id=asset.id, actor_id=admin_user.id,
                                operation="insert", idempotency_key="applied", request_hash=request_hash,
                                key_hash="key", schema_fingerprint="schema", status="applied", affected_rows=1,
                                result_json={"row_token": "receipt-token"})
    db.add(record)
    db.commit()
    result = service.mutate_row(db, admin_user, asset.id, "insert", body)
    assert result["replayed"] is True
    assert result["row_token"] == "receipt-token"
    with pytest.raises(HTTPException) as exc:
        service.mutate_row(db, admin_user, asset.id, "insert", body.model_copy(update={"key": {"id": 2}}))
    assert exc.value.detail["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"


class _NativeTestConnection:
    """SQLite protocol double, not evidence for the real PG/MySQL transport."""
    def __init__(self, connection, *, ambiguous_commit=False):
        self.connection = connection
        self.dialect = connection.dialect
        self.ambiguous_commit = ambiguous_commit

    def execution_options(self, **_):
        return self

    def begin(self):
        transaction = self.connection.begin()
        if not self.ambiguous_commit:
            return transaction
        class AmbiguousTransaction:
            @property
            def is_active(self):
                return transaction.is_active
            def commit(self):
                transaction.commit()
                raise ConnectionError("simulated lost commit acknowledgement")
            def rollback(self):
                transaction.rollback()
        return AmbiguousTransaction()

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def close(self):
        self.connection.close()


class _NativeTestEngine:
    def __init__(self, engine, **kwargs):
        self.engine, self.kwargs = engine, kwargs
    def connect(self):
        return _NativeTestConnection(self.engine.connect(), **self.kwargs)
    def dispose(self):
        pass


def _write_source(monkeypatch, *, ambiguous_commit=False):
    engine = sa.create_engine("sqlite://")
    table = sa.Table("write_rows", sa.MetaData(), sa.Column("id", sa.BigInteger, primary_key=True),
                     sa.Column("name", sa.String(100), nullable=False))
    table.create(engine)
    monkeypatch.setattr(service, "source_engine", lambda *_: _NativeTestEngine(engine, ambiguous_commit=ambiguous_commit))
    monkeypatch.setattr(service, "reflect", lambda *_: (table, {}, "schema"))
    monkeypatch.setattr(service, "check_transactional_table", lambda *_: None)
    return engine, table


def test_source_crud_bound_values_conflicts_and_receipts(db, admin_user, monkeypatch):
    asset = _asset(db, admin_user)
    engine, table = _write_source(monkeypatch)
    try:
        payload = "O'Reilly; DROP TABLE write_rows; --"
        body = RowMutation(key={"id": {"$integer": "9007199254740993"}}, values={"name": payload}, idempotency_key="insert")
        inserted = service.mutate_row(db, admin_user, asset.id, "insert", body)
        assert inserted["status"] == "applied"
        assert service.mutate_row(db, admin_user, asset.id, "insert", body)["replayed"] is True
        with engine.connect() as conn:
            assert conn.execute(sa.select(table.c.name)).scalar_one() == payload
        update = RowMutation(key=body.key, values={"name": "updated"}, expected_row_token=inserted["row_token"], idempotency_key="update")
        updated = service.mutate_row(db, admin_user, asset.id, "update", update)
        assert updated["status"] == "applied"
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "update", update.model_copy(update={"idempotency_key": "stale"}))
        assert exc.value.detail["code"] == "ROW_CONFLICT"
        deletion = RowMutation(key=body.key, expected_row_token=updated["row_token"], idempotency_key="delete")
        assert service.mutate_row(db, admin_user, asset.id, "delete", deletion)["status"] == "applied"
        with engine.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
        assert {r.status for r in db.query(IntegratedMutation)} == {"applied", "conflict"}
        # Neither source values nor credentials enter the audit ledger.
        assert all(payload not in str(r.result_json) for r in db.query(IntegratedMutation))
    finally:
        engine.dispose()


def test_lost_commit_acknowledgement_is_unknown_and_never_replayed(db, admin_user, monkeypatch):
    asset = _asset(db, admin_user)
    engine, table = _write_source(monkeypatch, ambiguous_commit=True)
    try:
        body = RowMutation(key={"id": 1}, values={"name": "committed"}, idempotency_key="lost-ack")
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "insert", body)
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "UNKNOWN_REQUIRES_RECONCILIATION"
        record = db.query(IntegratedMutation).one()
        assert record.status == "unknown"
        with engine.connect() as conn:
            assert conn.execute(sa.select(table.c.name)).scalar_one() == "committed"
        monkeypatch.setattr(service, "source_engine", lambda *_: pytest.fail("unknown must never retry source"))
        with pytest.raises(HTTPException) as exc:
            service.mutate_row(db, admin_user, asset.id, "insert", body)
        assert exc.value.detail["code"] == "UNKNOWN_REQUIRES_RECONCILIATION"
    finally:
        engine.dispose()


def test_queue_is_persisted_before_dispatch_and_polled_by_exact_run(db, admin_user, monkeypatch):
    from app.data_channel.pipeline_tasks import dispatch
    from app.data_channel.datasets.models import IntegratedSyncRun
    asset = _asset(db, admin_user)
    sent = []
    def publish(run_id, asset_id, actor_id, batch_size):
        run = db.get(IntegratedSyncRun, run_id)
        assert run is not None and run.status == "queued"
        assert run.snapshot_json == {"queued": True, "actor_id": actor_id, "batch_size": batch_size}
        assert run.physical_table == "lake_iv_" + run_id.replace("-", "")
        sent.append((run_id, asset_id, actor_id, batch_size))
    monkeypatch.setattr(dispatch, "dispatch_integrated_sync", publish)
    result = service.request_sync(db, admin_user, asset.id, 10)
    assert result["status"] == "queued" and result["complete"] is False
    assert len(sent) == 1
    assert service.get_sync_run(db, admin_user, asset.id, result["run_id"]) == result
    assert service.list_sync_runs(db, admin_user, asset.id)["items"] == [result]
    with pytest.raises(HTTPException) as exc:
        service.get_sync_run(db, admin_user, asset.id, "not-this-run")
    assert exc.value.status_code == 404


def test_dispatch_failure_never_claims_completed_sync(db, admin_user, monkeypatch):
    from app.data_channel.pipeline_tasks import dispatch
    from app.data_channel.datasets.models import IntegratedSyncRun
    asset = _asset(db, admin_user)
    def unavailable(*_):
        raise ConnectionError("fake dispatch acknowledgement loss")
    monkeypatch.setattr(dispatch, "dispatch_integrated_sync", unavailable)
    with pytest.raises(HTTPException) as exc:
        service.request_sync(db, admin_user, asset.id)
    assert exc.value.status_code == 503
    run = db.query(IntegratedSyncRun).one()
    assert run.status == "failed" and run.completed_at is not None and run.version_id is None
    assert service.sync_run_dto(run)["complete"] is False


def test_mysql_time_wall_clock_is_exact_and_duration_is_rejected():
    from datetime import timedelta
    from app.data_channel.integrated.contracts import normalize_source_value
    assert wire_value(timedelta(hours=23, minutes=59, seconds=58, microseconds=123456)) == "23:59:58.123456"
    assert normalize_source_value(timedelta(0)).isoformat() == "00:00:00"
    for duration in (timedelta(hours=-1), timedelta(hours=24), timedelta(hours=838)):
        with pytest.raises(HTTPException) as exc:
            wire_value(duration)
        assert exc.value.detail["code"] == "UNSUPPORTED_TIME_DURATION"


def test_total_deadline_expires_before_count_not_per_statement():
    engine, conn, scope = _scope()
    try:
        scope._deadline = 0
        with pytest.raises(HTTPException) as exc:
            list(scope.iter_batches(sa.select(scope.tables["a"])))
        assert exc.value.detail["code"] == "QUERY_BUDGET_EXCEEDED"
        assert scope.completion["complete"] is False
    finally:
        conn.close()
        engine.dispose()


def test_mysql_temporal_metadata_normalization_and_decode_are_lossless():
    from datetime import timedelta
    from sqlalchemy.dialects import mysql
    table = sa.Table("temporal", sa.MetaData(), sa.Column("instant", mysql.TIMESTAMP(fsp=6)),
                     sa.Column("local", mysql.DATETIME(fsp=6)), sa.Column("clock", mysql.TIME(fsp=6)),
                     sa.Column("coarse", mysql.TIMESTAMP(fsp=0)))
    assert native.column_metadata(table.c.instant, mysql.dialect())["timezone"] is True
    assert native.column_metadata(table.c.local, mysql.dialect())["timezone"] is False
    clock = datetime(2026, 10, 24, 4, 34, 56, 123456)
    row = native.normalize_row({"instant": clock, "local": clock, "clock": timedelta(hours=12)}, table)
    assert row["instant"] == clock.replace(tzinfo=timezone.utc)
    assert row["local"].tzinfo is None and row["clock"].isoformat() == "12:00:00"
    decoded = service._decode(table.c.instant, "2026-10-24T12:34:56.123456+08:00")
    assert decoded == clock and decoded.tzinfo is None
    assert service._decode(table.c.local, clock.isoformat()) == clock
    for column, value in ((table.c.instant, clock.isoformat()),
                          (table.c.local, "2026-10-24T04:34:56+00:00"),
                          (table.c.clock, "04:34:56+00:00"),
                          (table.c.coarse, "2026-10-24T04:34:56.123456+00:00")):
        with pytest.raises(HTTPException) as exc:
            service._decode(column, value)
        assert exc.value.detail["code"] == "INVALID_COLUMN_VALUE"
    assert row_token(row, "schema") == row_token(native.normalize_row(row, table), "schema")


@pytest.mark.parametrize("kind", ["timestamp", "time"])
@pytest.mark.parametrize("aware", [False, True])
def test_temporal_decode_rejects_nonzero_digits_beyond_microseconds(kind, aware):
    typ = postgresql.TIMESTAMP(timezone=aware) if kind == "timestamp" else postgresql.TIME(timezone=aware)
    column = sa.Column("value", typ)
    prefix = "2026-10-24T12:34:56" if kind == "timestamp" else "12:34:56"
    suffix = "+08:00" if aware else ""
    for separator in (".", ","):
        for fraction in ("1234561", "0000001", "123456000001", "9999999"):
            with pytest.raises(HTTPException) as exc:
                service._decode(column, prefix + separator + fraction + suffix)
            assert exc.value.status_code == 422
            assert exc.value.detail["code"] == "INVALID_COLUMN_VALUE"
        exact = service._decode(column, prefix + separator + "123456000000" + suffix)
        assert exact.microsecond == 123456
        if aware:
            assert exact.utcoffset().total_seconds() == (0 if kind == "timestamp" else 8 * 3600)
            assert exact.hour == (4 if kind == "timestamp" else 12)
        else:
            assert exact.tzinfo is None


@pytest.mark.parametrize("kind", ["timestamp", "time"])
@pytest.mark.parametrize("precision", [0, 3, 6, None])
def test_postgres_temporal_decode_enforces_reflected_precision(kind, precision):
    typ = postgresql.TIMESTAMP(precision=precision) if kind == "timestamp" else postgresql.TIME(precision=precision)
    column = sa.Column("value", typ)
    prefix = "2026-10-24T12:34:56" if kind == "timestamp" else "12:34:56"
    effective = 6 if precision is None else precision
    assert service._decode(column, prefix).microsecond == 0
    assert service._decode(column, prefix + ".000000000").microsecond == 0
    exact = "123000000" if effective == 3 else "123456000"
    if effective:
        assert service._decode(column, prefix + "," + exact).microsecond == int(exact[:6])
    for fraction in ("000001", "123456", "999999") if effective < 6 else ("1234561",):
        with pytest.raises(HTTPException) as exc:
            service._decode(column, prefix + "." + fraction)
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "INVALID_COLUMN_VALUE"


@pytest.mark.parametrize("type_name", ["TIMESTAMP", "DATETIME", "TIME"])
@pytest.mark.parametrize("fsp", [0, 3, 6, None])
def test_mysql_temporal_decode_enforces_fsp_without_rounding(type_name, fsp):
    column = sa.Column("value", getattr(mysql, type_name)(fsp=fsp))
    prefix = "12:34:56" if type_name == "TIME" else "2026-10-24T12:34:56"
    suffix = "+08:00" if type_name == "TIMESTAMP" else ""
    effective = 0 if fsp is None else fsp
    assert service._decode(column, prefix + ".000000000" + suffix).microsecond == 0
    if effective:
        fraction = "123000000" if effective == 3 else "123456000"
        exact = service._decode(column, prefix + "," + fraction + suffix)
        assert exact.microsecond == int(fraction[:6])
        assert exact.tzinfo is None
        assert exact.hour == (4 if type_name == "TIMESTAMP" else 12)
    for fraction in ("000001", "123456", "999999") if effective < 6 else ("1234561",):
        with pytest.raises(HTTPException) as exc:
            service._decode(column, prefix + "." + fraction + suffix)
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "INVALID_COLUMN_VALUE"


@pytest.mark.parametrize("kind", ["timestamp", "time"])
def test_temporal_decode_rejects_fractional_timezone_offsets(kind):
    typ = postgresql.TIMESTAMP(timezone=True) if kind == "timestamp" else postgresql.TIME(timezone=True)
    column = sa.Column("value", typ)
    prefix = "2026-10-24T12:34:56.123456" if kind == "timestamp" else "12:34:56.123456"
    for suffix in ("+00:00:00.1", "-00:00:00,000001", "+08:00:00.1", "-08:00:00,000001"):
        with pytest.raises(HTTPException) as exc:
            service._decode(column, prefix + suffix)
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "INVALID_COLUMN_VALUE"
    for suffix in ("+00:00:00.000000000", "+08:00:00,000000000"):
        assert service._decode(column, prefix + suffix).microsecond == 123456


def test_postgres_temporal_decode_preserves_timezone_contract_and_utc_precision():
    stamp = sa.Column("stamp", postgresql.TIMESTAMP(timezone=True, precision=3))
    clock = sa.Column("clock", postgresql.TIME(timezone=True, precision=3))
    assert service._decode(stamp, "2026-10-24T12:34:56.123000000+08:00").isoformat() == "2026-10-24T04:34:56.123000+00:00"
    assert service._decode(stamp, "2026-10-24T04:34:56,123000000Z").isoformat() == "2026-10-24T04:34:56.123000+00:00"
    assert service._decode(clock, "12:34:56,123000000-08:00").isoformat() == "12:34:56.123000-08:00"
    # Precision is checked after UTC normalization, not only on wall-clock digits.
    for column, value in (
        (stamp, "2026-10-24T12:34:56.123+08:00:00.000001"),
        (stamp, "2026-10-24T12:34:56.123"),
        (clock, "12:34:56.123"),
        (sa.Column("naive_stamp", postgresql.TIMESTAMP()), "2026-10-24T12:34:56.123Z"),
        (sa.Column("naive_clock", postgresql.TIME()), "12:34:56.123Z"),
        (stamp, "2026-10-24T12:34:56+08:00:00.1234561"),
        (clock, "12:34:56+08:00:00,1234561"),
    ):
        with pytest.raises(HTTPException) as exc:
            service._decode(column, value)
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "INVALID_COLUMN_VALUE"


def test_sync_http_is_202_queue_only_and_run_polling_is_read_only(db, admin_user, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.data_channel.integrated.router import router
    from app.data_channel.pipeline_tasks import dispatch
    from app.deps import get_current_user, get_db
    asset = _asset(db, admin_user)
    app = FastAPI()
    app.include_router(router, prefix="/api/v2/integrated-datasets")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin_user
    published = []
    monkeypatch.setattr(dispatch, "dispatch_integrated_sync", lambda *args: published.append(args))
    with TestClient(app) as client:
        endpoint = f"/api/v2/integrated-datasets/{asset.id}"
        response = client.post(endpoint + "/sync?batch_size=17")
        assert response.status_code == 202
        queued = response.json()
        assert queued["status"] == "queued" and queued["complete"] is False
        assert queued["row_count"] == 0 and queued["version_id"] is None
        assert published == [(queued["run_id"], asset.id, admin_user.id, 17)]
        assert client.get(endpoint + "/sync-runs").json() == {"items": [queued]}
        assert client.get(endpoint + "/sync-runs/" + queued["run_id"]).json() == queued
        assert client.get(endpoint + "/sync-runs/not-owned").status_code == 404
        admin_user.role = "viewer"
        db.commit()
        assert client.post(endpoint + "/sync").status_code == 403
        assert len(published) == 1


def test_dispatch_ack_loss_does_not_fail_already_claimed_run(db, admin_user, monkeypatch):
    from app.data_channel.pipeline_tasks import dispatch
    from app.data_channel.datasets.models import IntegratedSyncRun
    asset = _asset(db, admin_user)
    def claimed_then_ack_loss(run_id, *_):
        run = db.get(IntegratedSyncRun, run_id)
        run.status = "starting"
        db.commit()
        raise ConnectionError("simulated lost broker acknowledgement after claim")
    monkeypatch.setattr(dispatch, "dispatch_integrated_sync", claimed_then_ack_loss)
    result = service.request_sync(db, admin_user, asset.id)
    assert result["status"] == "starting" and result["complete"] is False
    assert result["completed_at"] is None and result["error"] is None


def test_non_owner_editor_and_viewer_cannot_write(db, admin_user, editor_user):
    asset = _asset(db, admin_user)
    with pytest.raises(HTTPException) as exc:
        native.get_asset(db, editor_user, asset.id, write=True)
    assert exc.value.status_code == 403
    editor_user.role = "viewer"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        native.get_asset(db, editor_user, asset.id, write=True)
    assert exc.value.detail["code"] == "INTEGRATED_READ_ONLY"


def test_source_byte_measure_is_lossless_for_nested_and_wide_values():
    import json
    row = {"text": "汉字😊\n\t\\\"", "id": 2**63 - 1,
           "amount": Decimal("1234567890.123456"), "binary": b"\x00\xff",
           "date": date(2026, 10, 24), "json": {"a": [True, None, "汉"]}}
    expected = json.dumps(wire_value(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assert native._source_row_bytes(row) == len(expected.encode("utf-8"))


def test_source_batches_obey_utf8_byte_budget_without_driver_prefetch(monkeypatch):
    engine, conn, scope = _scope()
    try:
        table = scope.tables["a"]
        conn.execute(table.update().values(group_name="汉字😊" * 10))
        records = conn.execute(sa.select(table)).mappings().all()
        limit = 2 * max(native._source_row_bytes(dict(row)) for row in records) - 1
        monkeypatch.setattr(native, "MAX_SOURCE_BATCH_BYTES", limit)
        options = []
        original = conn.execution_options
        def record_options(**kwargs):
            options.append(kwargs)
            return original(**kwargs)
        monkeypatch.setattr(conn, "execution_options", record_options)
        batches = list(scope.iter_batches(sa.select(table), batch_size=1000))
        assert [len(batch) for batch in batches] == [1] * 5
        assert all(sum(native._source_row_bytes(row) for row in batch) <= limit for batch in batches)
        assert scope.completion["row_count"] == scope.completion["expected_rows"] == 5
        assert scope.completion["complete"] is True
        assert {"stream_results": True, "yield_per": 1, "max_row_buffer": 1} in options
    finally:
        conn.close()
        engine.dispose()


def test_source_oversized_row_fails_without_completion_or_cursor_leak(monkeypatch):
    engine, conn, scope = _scope()
    try:
        monkeypatch.setattr(native, "MAX_SOURCE_BATCH_BYTES", 2)
        with pytest.raises(HTTPException) as exc:
            list(scope.iter_batches(sa.select(scope.tables["a"])))
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "SOURCE_ROW_BYTES_EXCEEDED"
        assert scope.completion["complete"] is False
        assert scope.completion["row_count"] == 0
        assert scope._busy is False
        assert conn.get_execution_options()["stream_results"] is False
    finally:
        conn.close()
        engine.dispose()


def test_json_text_codec_preserves_sql_null_literal_null_and_numeric_digits():
    import json
    from app.data_channel.integrated.contracts import JsonText, serialize_source_json

    raw = '{"n":9007199254740993.123456789123456789,"$json":null,"nested":[null]}'
    table = sa.Table("json_source", sa.MetaData(),
                     sa.Column("payload", sa.JSON), sa.Column("required", sa.JSON, nullable=False))
    decoded = service._decode(table.c.payload, raw)
    assert isinstance(decoded, JsonText) and decoded == raw
    assert serialize_source_json(decoded) == raw
    assert wire_value({"payload": decoded}) == {"payload": raw}
    assert native._source_row_bytes({"payload": decoded}) == len(
        json.dumps({"payload": raw}, ensure_ascii=False, separators=(",", ":")).encode())
    literal_null = service._decode(table.c.required, "null")
    assert isinstance(literal_null, JsonText) and literal_null == "null"
    assert isinstance(service._decode(table.c.payload, None), sa.sql.elements.Null)
    assert row_token({"payload": None}, "schema") != row_token({"payload": literal_null}, "schema")
    assert row_token({"payload": JsonText(raw)}, "schema") != row_token(
        {"payload": JsonText('{"n":9007199254740993.123456789123456788,"$json":null,"nested":[null]}')}, "schema")
    assert native.column_metadata(table.c.payload, sa.dialects.postgresql.dialect())["encoding"] == "json_text"
    with pytest.raises(HTTPException) as exc:
        service._decode(table.c.required, None)
    assert exc.value.detail["code"] == "NULL_NOT_ALLOWED"
    for invalid in ("{", "NaN", "[Infinity]", {"native": "object"}, 123):
        with pytest.raises(HTTPException) as exc:
            service._decode(table.c.payload, invalid)
        assert exc.value.detail["code"] == "INVALID_COLUMN_VALUE"


def test_native_json_engine_codec_uses_raw_text_for_both_supported_drivers(monkeypatch):
    from app.data_channel.integrated.contracts import JsonText

    observed = []
    monkeypatch.setattr(native.sa, "create_engine", lambda url, **kwargs: observed.append((url, kwargs)))
    for kind in ("postgres", "mysql"):
        native.source_engine(SimpleNamespace(kind=kind, config={"database": "fixture"}))
    assert len(observed) == 2
    for _, options in observed:
        assert options["json_deserializer"]("null") == JsonText("null")
        raw = '{"n":9007199254740993.123456789123456789}'
        assert options["json_serializer"](options["json_deserializer"](raw)) == raw
        assert options["json_serializer"]({"nested": [None]}) == '{"nested":[null]}'


# ---------------------------------------------------------------------------
# Pinned dbx metadata transport (official release image HTTP surface)
# ---------------------------------------------------------------------------

def _dbx_column(name, data_type, nullable, primary=False, precision=None, scale=None,
                length=None, collation=None):
    return {"name": name, "data_type": data_type, "is_nullable": nullable,
            "is_primary_key": primary, "numeric_precision": precision,
            "numeric_scale": scale, "character_maximum_length": length,
            "collation": collation}


class _DbxHttp:
    """Minimal pinned-surface double; real dbx acceptance is the opt-in test below."""

    def __init__(self, *, tables=None, columns=None, schemas=None, databases=None,
                 version=DBX_VERSION, login_status=200, setup_required=False,
                 tables_payload="default", columns_payload="default"):
        self.tables = tables if tables is not None else []
        self.columns = columns if columns is not None else []
        self.schemas = schemas if schemas is not None else []
        self.databases = databases if databases is not None else []
        self.version = version
        self.login_status = login_status
        self.setup_required = setup_required
        self.tables_payload = tables_payload
        self.columns_payload = columns_payload
        self.connected = []
        self.disconnected = []

    def __call__(self, request):
        path = request.url.path
        if path == "/api/auth/check":
            return httpx.Response(200, json={"authenticated": False, "required": True,
                                             "setup_required": self.setup_required})
        if path == "/api/auth/login":
            headers = {"set-cookie": "dbx_session=fixture; Path=/"} if self.login_status == 200 else {}
            return httpx.Response(self.login_status, json={}, headers=headers)
        if path == "/api/version":
            return httpx.Response(200, json={"version": self.version, "demoMode": False})
        if path == "/api/connection/connect":
            payload = json.loads(request.content)
            self.connected.append(payload["config"]["id"])
            assert payload["config"]["password"] == "fixture-source-password"
            return httpx.Response(200, json="connected")
        if path == "/api/connection/disconnect":
            payload = json.loads(request.content)
            # Pinned DisconnectRequest is camelCase: connectionId.
            assert set(payload) == {"connectionId"}
            self.disconnected.append(payload["connectionId"])
            return httpx.Response(200, json="disconnected")
        if path == "/api/schema/databases":
            return httpx.Response(200, json=self.databases)
        if path == "/api/schema/schemas":
            return httpx.Response(200, json=self.schemas)
        if path == "/api/schema/tables":
            if self.tables_payload != "default":
                return httpx.Response(200, json=self.tables_payload)
            params = request.url.params
            name_filter = (params.get("filter") or "").lower()
            offset = int(params.get("offset") or 0)
            limit = int(params.get("limit") or 0)
            rows = [table for table in self.tables
                    if not name_filter
                    or name_filter in str(table.get("name", "")).lower()
                    or name_filter in str(table.get("comment") or "").lower()]
            return httpx.Response(200, json=rows[offset:offset + limit] if limit else rows)
        if path == "/api/schema/columns":
            if self.columns_payload != "default":
                return httpx.Response(200, json=self.columns_payload)
            return httpx.Response(200, json=self.columns)
        return httpx.Response(404, json={"error": "not found"})


def _install_dbx_http(monkeypatch, fixture, *, unreachable=False):
    calls = []

    def factory(base_url, timeout):
        def handler(request):
            calls.append({"method": request.method, "path": request.url.path,
                          "params": dict(request.url.params),
                          "body": json.loads(request.content) if request.content else None,
                          "cookie": request.headers.get("cookie")})
            if unreachable:
                raise httpx.ConnectError("fixture dbx is down", request=request)
            return fixture(request)
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler),
                            timeout=timeout, follow_redirects=False, trust_env=False)

    monkeypatch.setattr(dbx_adapter_module, "_http_client", factory)
    return calls


@pytest.fixture
def dbx_settings(monkeypatch):
    monkeypatch.setattr(settings, "integrated_dbx_url", "http://dbx.internal:4224")
    monkeypatch.setattr(settings, "integrated_dbx_password", "fixture-dbx-password")
    monkeypatch.setattr(settings, "integrated_dbx_commit", DBX_COMMIT)


def _dbx_source_connection():
    return SimpleNamespace(id="source-connection", name="source", kind="postgres",
                           config={"host": "127.0.0.1", "port": 5432,
                                   "database": "integrated_source", "user": "integrated_e2e",
                                   "password": "fixture-source-password"})


def _dbx_columns():
    return [
        _dbx_column("id", "bigint", False, primary=True),
        _dbx_column("amount", "numeric(30,6)", False, precision=30, scale=6),
        _dbx_column("note", "character varying(100)", True, length=100),
    ]


def test_dbx_http_list_and_describe_use_pinned_surface_transiently(monkeypatch, dbx_settings):
    fixture = _DbxHttp(schemas=["public", "pg_catalog", "information_schema"],
                       tables=[{"name": "scale_rows", "table_type": "TABLE", "comment": None}],
                       columns=_dbx_columns())
    calls = _install_dbx_http(monkeypatch, fixture)
    adapter = DbxMetadataAdapter()
    connection = _dbx_source_connection()

    assert adapter.list_resources(connection) == [{"schema_name": "public", "table_name": "scale_rows"}]
    evidence = adapter.describe_table(connection, "public", "scale_rows")
    assert evidence["backend"] == "dbx-web-http"
    assert evidence["commit"] == DBX_COMMIT and evidence["version"] == DBX_VERSION
    assert [column["name"] for column in evidence["columns"]] == ["id", "amount", "note"]
    assert evidence["primary_key"] == ["id"]
    assert evidence["columns"][1]["precision"] == 30 and evidence["columns"][1]["scale"] == 6
    assert evidence["columns"][2]["length"] == 100

    paths = [call["path"] for call in calls]
    assert "/api/query/execute" not in paths and not any("/query" in path for path in paths)
    connect_calls = [call for call in calls if call["path"] == "/api/connection/connect"]
    assert len(connect_calls) == 2
    connected_ids = [call["body"]["config"]["id"] for call in connect_calls]
    assert len(set(connected_ids)) == 2
    assert all(call["body"]["config"]["db_type"] == "postgres" for call in connect_calls)
    assert all(call["body"]["config"]["host"] == "127.0.0.1" for call in connect_calls)
    assert sorted(fixture.disconnected) == sorted(connected_ids)
    authenticated = [call for call in calls
                     if call["path"] in {"/api/version", "/api/connection/connect",
                                         "/api/schema/schemas", "/api/schema/tables",
                                         "/api/schema/columns"}]
    assert authenticated and all(call["cookie"] and "dbx_session=" in call["cookie"]
                                 for call in authenticated)


def test_dbx_status_is_verified_and_reports_failure_reasons(monkeypatch, dbx_settings):
    fixture = _DbxHttp(schemas=["public"], tables=[{"name": "scale_rows"}], columns=_dbx_columns())
    _install_dbx_http(monkeypatch, fixture)
    status = DbxMetadataAdapter().status()
    assert status["configured"] is True and status["available"] is True
    assert status["commit"] == DBX_COMMIT and status["version"] == DBX_VERSION
    assert status["reason"] is None

    mismatch = _DbxHttp(version="0.6.52")
    _install_dbx_http(monkeypatch, mismatch)
    status = DbxMetadataAdapter().status()
    assert status["configured"] is True and status["available"] is False
    assert status["reason"] == "DBX_VERSION_MISMATCH"

    unauthorized = _DbxHttp(login_status=401)
    _install_dbx_http(monkeypatch, unauthorized)
    assert DbxMetadataAdapter().status()["reason"] == "DBX_AUTH_FAILED"

    setup = _DbxHttp(setup_required=True)
    _install_dbx_http(monkeypatch, setup)
    assert DbxMetadataAdapter().status()["reason"] == "DBX_AUTH_SETUP_REQUIRED"

    _install_dbx_http(monkeypatch, _DbxHttp(), unreachable=True)
    status = DbxMetadataAdapter().status()
    assert status["configured"] is True and status["available"] is False
    assert status["reason"] == "DBX_UNREACHABLE"


def test_dbx_adapter_rejects_unsafe_or_unmappable_configuration(monkeypatch, dbx_settings):
    monkeypatch.setattr(settings, "integrated_dbx_url", "http://user:secret@dbx.internal:4224")
    status = DbxMetadataAdapter().status()
    assert status["configured"] is False and status["reason"] == "DBX_CONFIG_INVALID"
    with pytest.raises(HTTPException) as exc:
        DbxMetadataAdapter().list_resources(_dbx_source_connection())
    assert (exc.value.status_code, exc.value.detail["code"]) == (503, "DBX_CONFIG_INVALID")

    adapter = DbxMetadataAdapter()
    for config, code in (
        ({"host": "h", "database": "d", "ssl": True}, "DBX_TLS_CONFIG_UNSUPPORTED"),
        ({"connection_string": "postgresql://u:p@h:5432/d?sslmode=require"},
         "DBX_CONNECTION_STRING_UNSUPPORTED"),
        ({"host": "h"}, "DBX_DATABASE_REQUIRED"),
    ):
        connection = SimpleNamespace(id="c", name="n", kind="postgres", config=config)
        with pytest.raises(HTTPException) as exc:
            adapter._connection_config(connection)
        assert (exc.value.status_code, exc.value.detail["code"]) == (422, code)

    mysql = SimpleNamespace(id="c", name="n", kind="mysql",
                            config={"host": "127.0.0.1", "port": 3306, "user": "u",
                                    "password": "p", "database": "integrated_source"})
    mapped = adapter._connection_config(mysql)
    assert mapped["db_type"] == "mysql" and mapped["database"] == "integrated_source"
    assert mapped["id"].startswith("platform-c-")
    assert "password" in mapped


def test_dbx_connection_string_mapping_without_options_is_explicit():
    adapter = DbxMetadataAdapter()
    postgres = SimpleNamespace(
        id="dsn-pg", name="dsn source", kind="postgres",
        config={"connection_string": "postgresql://integrated_e2e:fixture@127.0.0.1:45433/integrated_source"})
    mapped = adapter._connection_config(postgres)
    assert mapped["db_type"] == "postgres"
    assert (mapped["host"], mapped["port"], mapped["username"], mapped["password"],
            mapped["database"]) == ("127.0.0.1", 45433, "integrated_e2e", "fixture", "integrated_source")
    mysql = SimpleNamespace(
        id="dsn-my", name="dsn mysql", kind="mysql",
        config={"connection_string": "mysql+pymysql://integrated_e2e:fixture@127.0.0.1:43306/integrated_source"})
    mapped = adapter._connection_config(mysql)
    assert mapped["db_type"] == "mysql" and mapped["database"] == "integrated_source"
    assert mapped["host"] == "127.0.0.1" and mapped["port"] == 43306


def test_dbx_dual_endpoint_is_explicit_and_native_engine_ignores_it(monkeypatch):
    adapter = DbxMetadataAdapter()
    native_config = {"host": "127.0.0.1", "port": 45433, "database": "integrated_source",
                     "user": "integrated_e2e", "password": "fixture"}
    explicit = SimpleNamespace(id="c", name="n", kind="postgres",
                               config={**native_config, "dbx_host": "source-pg", "dbx_port": 5432})
    mapped = adapter._connection_config(explicit)
    assert (mapped["host"], mapped["port"]) == ("source-pg", 5432)
    assert (mapped["username"], mapped["password"], mapped["database"]) == (
        "integrated_e2e", "fixture", "integrated_source")

    observed = []
    monkeypatch.setattr(native.sa, "create_engine", lambda url, **kwargs: observed.append(url))
    native.source_engine(SimpleNamespace(kind="postgres", config=explicit.config))
    assert len(observed) == 1
    assert (observed[0].host, observed[0].port) == ("127.0.0.1", 45433)

    half = SimpleNamespace(id="c", name="n", kind="postgres",
                           config={**native_config, "dbx_host": "source-pg"})
    with pytest.raises(HTTPException) as exc:
        adapter._connection_config(half)
    assert (exc.value.status_code, exc.value.detail["code"]) == (422, "DBX_ENDPOINT_INCOMPLETE")

    for patch, code in (
        ({"dbx_host": "", "dbx_port": 5432}, "DBX_ENDPOINT_INVALID"),
        ({"dbx_host": "source-pg", "dbx_port": True}, "DBX_ENDPOINT_INVALID"),
        ({"dbx_host": "source-pg", "dbx_port": 5432.5}, "DBX_ENDPOINT_INVALID"),
        ({"dbx_host": "http://source-pg", "dbx_port": 5432}, "DBX_ENDPOINT_INVALID"),
        ({"dbx_host": "source-pg", "dbx_port": 0}, "DBX_ENDPOINT_INVALID"),
        ({"dbx_host": None, "dbx_port": None}, "DBX_ENDPOINT_INVALID"),
    ):
        connection = SimpleNamespace(id="c", name="n", kind="postgres",
                                     config={**native_config, **patch})
        with pytest.raises(HTTPException) as exc:
            adapter._connection_config(connection)
        assert (exc.value.status_code, exc.value.detail["code"]) == (422, code)

    ipv6 = SimpleNamespace(id="c", name="n", kind="postgres",
                           config={**native_config, "dbx_host": "::1", "dbx_port": "5432"})
    mapped = adapter._connection_config(ipv6)
    assert (mapped["host"], mapped["port"]) == ("::1", 5432)

    absent = adapter._connection_config(SimpleNamespace(id="c", name="n", kind="postgres",
                                                       config=native_config))
    assert (absent["host"], absent["port"]) == ("127.0.0.1", 45433)


def test_dbx_http_metadata_errors_fail_closed(monkeypatch, dbx_settings):
    connection = _dbx_source_connection()

    cases = []
    tables = [{"name": "scale_rows", "table_type": "TABLE", "comment": None}]
    cases.append((_DbxHttp(tables_payload={"not": "a list"}), 503, "DBX_RESPONSE_INVALID"))
    cases.append((_DbxHttp(tables=tables, columns_payload=[]), 503, "DBX_METADATA_INCOMPLETE"))
    cases.append((_DbxHttp(tables=[], columns=_dbx_columns()), 404, "DBX_TABLE_NOT_FOUND"))
    for fixture, status, code in cases:
        _install_dbx_http(monkeypatch, fixture)
        with pytest.raises(HTTPException) as exc:
            DbxMetadataAdapter().describe_table(connection, "public", "scale_rows")
        assert (exc.value.status_code, exc.value.detail["code"]) == (status, code)

    _install_dbx_http(monkeypatch, _DbxHttp(), unreachable=True)
    with pytest.raises(HTTPException) as exc:
        DbxMetadataAdapter().describe_table(connection, "public", "scale_rows")
    assert (exc.value.status_code, exc.value.detail["code"]) == (503, "DBX_UNREACHABLE")


# ---------------------------------------------------------------------------
# dbx discovery is authoritative; native reflection is the typed re-check
# ---------------------------------------------------------------------------

class _FakeDbxAdapter:
    def __init__(self, evidence):
        self.evidence = evidence

    def status(self):
        return {"configured": True, "available": True, "commit": DBX_COMMIT,
                "version": DBX_VERSION, "reason": None}

    def describe_table(self, connection, schema_name, table_name):
        return self.evidence


_DBX_SPELLINGS = {"integer": "bigint", "decimal": "numeric(30,6)",
                  "string": "character varying(100)", "boolean": "boolean",
                  "float": "double precision", "date": "date",
                  "timestamp": "timestamp without time zone",
                  "time": "time without time zone", "json": "jsonb",
                  "binary": "bytea"}


def _dbx_evidence(schema):
    columns = [{"name": column["name"],
                "data_type": _DBX_SPELLINGS.get(column["type"], column["type"]),
                "nullable": column["nullable"],
                "primary_key": column["name"] in schema["primary_key"],
                "precision": column["precision"], "scale": column["scale"],
                "length": column["length"], "collation": column["collation"]}
               for column in schema["columns"]]
    return {"backend": "dbx-web-http", "commit": DBX_COMMIT, "version": DBX_VERSION,
            "table_name": "rows", "schema_name": "public", "table_type": "TABLE",
            "columns": columns, "primary_key": list(schema["primary_key"])}


def _registration_source(monkeypatch, db):
    engine = sa.create_engine("sqlite://")
    table = sa.Table("rows", sa.MetaData(),
                     sa.Column("id", sa.BigInteger, primary_key=True),
                     sa.Column("amount", sa.Numeric(30, 6), nullable=False),
                     sa.Column("note", sa.String(100)))
    table.create(engine)
    with engine.connect() as conn:
        _, schema, fingerprint = native.reflect(
            conn, SimpleNamespace(schema_name=None, table_name="rows"))
    monkeypatch.setattr(service, "source_engine", lambda *_: engine)
    monkeypatch.setattr(service, "reflect", lambda *_: (table, schema, fingerprint))
    monkeypatch.setattr(service, "check_transactional_table", lambda *_: None)
    connection = Connection(id="dbx-registration-source", name="dbx source",
                            kind="postgres", config={})
    db.add(connection)
    db.commit()
    return engine, schema, connection


def test_dbx_discovery_matches_reflection_and_registration_succeeds(db, admin_user, monkeypatch):
    engine, schema, connection = _registration_source(monkeypatch, db)
    try:
        evidence = _dbx_evidence(schema)
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(evidence))
        result = service.register_asset(db, admin_user, RegisterAsset(
            name="dbx discovered", connection_id=connection.id,
            schema_name="public", table_name="rows", writable=True))
        assert result["columns"] == schema["columns"]
        assert result["primary_key"] == ["id"]
        assert result["dbx"]["available"] is True
        assert result["dbx"]["commit"] == DBX_COMMIT
        assert result["capabilities"]["dbx_metadata"] is True
        assert db.query(Dataset).count() == 1 and db.query(IntegratedAsset).count() == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("mutate,code", [
    (lambda evidence: evidence.update(primary_key=["note"]), "DBX_METADATA_MISMATCH"),
    (lambda evidence: next(column for column in evidence["columns"]
                           if column["name"] == "note").update(nullable=False),
     "DBX_METADATA_MISMATCH"),
    (lambda evidence: next(column for column in evidence["columns"]
                           if column["name"] == "amount").update(data_type="jsonb"),
     "DBX_METADATA_MISMATCH"),
    (lambda evidence: evidence.update(columns=[column for column in evidence["columns"]
                                               if column["name"] != "note"]),
     "DBX_METADATA_MISMATCH"),
])
def test_dbx_metadata_mismatch_rejects_registration_without_partial_rows(
        db, admin_user, monkeypatch, mutate, code):
    engine, schema, connection = _registration_source(monkeypatch, db)
    try:
        evidence = _dbx_evidence(schema)
        mutate(evidence)
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(evidence))
        with pytest.raises(HTTPException) as exc:
            service.register_asset(db, admin_user, RegisterAsset(
                name="dbx mismatch", connection_id=connection.id,
                schema_name="public", table_name="rows", writable=True))
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == code
        assert db.query(Dataset).count() == 0 and db.query(IntegratedAsset).count() == 0
    finally:
        engine.dispose()


def test_dbx_refresh_schema_is_authoritative_and_fails_closed(db, admin_user, monkeypatch):
    engine, schema, connection = _registration_source(monkeypatch, db)
    try:
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(_dbx_evidence(schema)))
        asset = service.register_asset(db, admin_user, RegisterAsset(
            name="dbx refresh", connection_id=connection.id,
            schema_name="public", table_name="rows", writable=True))
        stale = _dbx_evidence(schema)
        stale["primary_key"] = ["note"]
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(stale))
        with pytest.raises(HTTPException) as exc:
            service.refresh_schema(db, admin_user, asset["id"])
        assert (exc.value.status_code, exc.value.detail["code"]) == (409, "DBX_METADATA_MISMATCH")
        stored = db.get(IntegratedAsset, asset["id"])
        assert stored is not None and stored.schema_json == schema
        assert stored.schema_fingerprint == asset["schema_fingerprint"]
    finally:
        engine.dispose()


def test_native_backend_cannot_impersonate_dbx(db, admin_user, monkeypatch):
    engine, schema, connection = _registration_source(monkeypatch, db)
    try:
        evidence = _dbx_evidence(schema)
        evidence["backend"] = "native"
        monkeypatch.setattr(service, "dbx", lambda: _FakeDbxAdapter(evidence))
        with pytest.raises(HTTPException) as exc:
            service.register_asset(db, admin_user, RegisterAsset(
                name="native fake", connection_id=connection.id,
                schema_name="public", table_name="rows", writable=True))
        assert (exc.value.status_code, exc.value.detail["code"]) == (503, "DBX_METADATA_INVALID")
        assert db.query(Dataset).count() == 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Opt-in genuine dbx acceptance: official image + isolated MySQL/PostgreSQL.
# Run only in the verifier slot:
#   INTEGRATED_DBX_E2E=1 INTEGRATED_DBX_URL=... INTEGRATED_DBX_PASSWORD=...
#   INTEGRATED_QUERY_POSTGRES_DSN=... INTEGRATED_QUERY_MYSQL_DSN=...
# ---------------------------------------------------------------------------

_DBX_E2E = os.environ.get("INTEGRATED_DBX_E2E") == "1"


@pytest.mark.skipif(not _DBX_E2E, reason="requires the isolated official dbx runtime and fixture sources")
def test_real_dbx_discovery_registration_and_pk_crud_for_mysql_and_postgres(db, admin_user, monkeypatch):
    dbx_url = os.environ.get("INTEGRATED_DBX_URL", "")
    dbx_password = os.environ.get("INTEGRATED_DBX_PASSWORD", "")
    sources = [("postgres", os.environ.get("INTEGRATED_QUERY_POSTGRES_DSN", "")),
               ("mysql", os.environ.get("INTEGRATED_QUERY_MYSQL_DSN", ""))]
    endpoints = {"postgres": (os.environ.get("INTEGRATED_DBX_POSTGRES_HOST", ""),
                              os.environ.get("INTEGRATED_DBX_POSTGRES_PORT", "")),
                 "mysql": (os.environ.get("INTEGRATED_DBX_MYSQL_HOST", ""),
                           os.environ.get("INTEGRATED_DBX_MYSQL_PORT", ""))}
    missing = ([name for name, value in (("INTEGRATED_DBX_URL", dbx_url),
                                         ("INTEGRATED_DBX_PASSWORD", dbx_password))
                if not value]
               + [name for name, dsn in (("INTEGRATED_QUERY_POSTGRES_DSN", sources[0][1]),
                                         ("INTEGRATED_QUERY_MYSQL_DSN", sources[1][1])) if not dsn]
               + [name for kind, (host, port) in endpoints.items()
                  for name, value in ((f"INTEGRATED_DBX_{kind.upper()}_HOST", host),
                                      (f"INTEGRATED_DBX_{kind.upper()}_PORT", port)) if not value])
    if missing:
        pytest.fail("genuine dbx environment is incomplete: missing " + ", ".join(missing))
    monkeypatch.setattr(settings, "integrated_dbx_url", dbx_url)
    monkeypatch.setattr(settings, "integrated_dbx_password", dbx_password)
    monkeypatch.setattr(settings, "integrated_dbx_commit", DBX_COMMIT)
    adapter = DbxMetadataAdapter()
    status = adapter.status()
    assert status["configured"] is True and status["available"] is True, status["reason"]

    for kind, dsn in sources:
        source_url = make_url(dsn)
        schema = "public" if kind == "postgres" else (source_url.database or "")
        table_name = "t3_dbx_" + uuid.uuid4().hex[:12]
        engine = sa.create_engine(dsn)
        table = sa.Table(table_name, sa.MetaData(),
                         sa.Column("id", sa.BigInteger, primary_key=True),
                         sa.Column("amount", sa.Numeric(30, 6), nullable=False),
                         sa.Column("note", sa.String(100)))
        try:
            table.create(engine)
            dbx_host, dbx_port = endpoints[kind]
            connection = Connection(
                id="dbx-" + uuid.uuid4().hex, name=f"dbx {kind}", kind=kind,
                config={"host": source_url.host, "port": source_url.port,
                        "user": source_url.username, "password": source_url.password,
                        "database": source_url.database,
                        "dbx_host": dbx_host, "dbx_port": int(dbx_port)})
            db.add(connection)
            db.commit()

            resources = adapter.list_resources(connection)
            assert {"schema_name": schema, "table_name": table_name} in resources
            evidence = adapter.describe_table(connection, schema, table_name)
            assert evidence["commit"] == DBX_COMMIT and evidence["version"] == DBX_VERSION
            assert evidence["primary_key"] == ["id"]
            with engine.connect() as conn:
                _, reflected, _ = native.reflect(
                    conn, SimpleNamespace(schema_name=schema, table_name=table_name))
            service.verify_dbx_metadata(evidence, reflected)
            assert len(evidence["columns"]) == len(reflected["columns"]) == 3

            asset = service.register_asset(db, admin_user, RegisterAsset(
                name=f"dbx {kind} probe", connection_id=connection.id,
                schema_name=schema, table_name=table_name, writable=True))
            assert asset["id"] and asset["dbx"]["available"] is True
            assert asset["primary_key"] == ["id"]

            inserted = service.mutate_row(db, admin_user, asset["id"], "insert", RowMutation(
                key={"id": 1}, values={"amount": "123.456789", "note": "dbx-中文"},
                idempotency_key=f"dbx-insert-{kind}"))
            assert inserted["status"] == "applied" and inserted["affected_rows"] == 1
            preview = service.preview_rows(db, admin_user, asset["id"], 10)
            assert len(preview["rows"]) == 1
            # Dual-endpoint same-source evidence: the row was written through
            # the native endpoint, and dbx discovery through its own network
            # endpoint still sees the same unique random table and columns.
            after_write = adapter.describe_table(connection, schema, table_name)
            assert [column["name"] for column in after_write["columns"]] == [
                column["name"] for column in evidence["columns"]]
            assert after_write["schema_name"] == schema and after_write["table_name"] == table_name
            token = preview["rows"][0]["row_token"]
            updated = service.mutate_row(db, admin_user, asset["id"], "update", RowMutation(
                key={"id": 1}, values={"note": "dbx-updated"}, expected_row_token=token,
                idempotency_key=f"dbx-update-{kind}"))
            assert updated["status"] == "applied"
            with pytest.raises(HTTPException) as exc:
                service.mutate_row(db, admin_user, asset["id"], "update", RowMutation(
                    key={"id": 1}, values={"note": "stale"}, expected_row_token=token,
                    idempotency_key=f"dbx-conflict-{kind}"))
            assert exc.value.detail["code"] == "ROW_CONFLICT"
            deleted = service.mutate_row(db, admin_user, asset["id"], "delete", RowMutation(
                key={"id": 1}, expected_row_token=updated["row_token"],
                idempotency_key=f"dbx-delete-{kind}"))
            assert deleted["status"] == "applied"
            with engine.connect() as conn:
                assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
        finally:
            table.drop(engine, checkfirst=True)
            engine.dispose()
