"""Verified DML outcomes and cleanup-safe durable receipts (SQLite only)."""
from types import SimpleNamespace
import uuid

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import service
from app.data_channel.integrated.contracts import RowMutation, row_token
from app.data_channel.integrated.models import IntegratedAsset, IntegratedMutation


class _SourceConnection:
    def __init__(self, connection, events, *, outcome, close_error, rollback_error):
        self.connection = connection
        self.dialect = connection.dialect
        self.events = events
        self.outcome = outcome
        self.close_error = close_error
        self.rollback_error = rollback_error

    def execution_options(self, **_):
        return self

    def begin(self):
        transaction = self.connection.begin()
        source = self

        class SourceTransaction:
            @property
            def is_active(self):
                return transaction.is_active

            def commit(self):
                transaction.commit()
                if source.outcome == "unknown":
                    raise ConnectionError("simulated lost commit acknowledgement")

            def rollback(self):
                transaction.rollback()
                if source.rollback_error:
                    raise ConnectionError("simulated lost rollback acknowledgement")

        return SourceTransaction()

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def close(self):
        self.connection.close()
        self.events.append("close")
        if self.close_error:
            raise RuntimeError("simulated connection cleanup failure")


class _SourceEngine:
    def __init__(self, engine, events, *, outcome, close_error, rollback_error):
        self.engine = engine
        self.events = events
        self.outcome = outcome
        self.close_error = close_error
        self.rollback_error = rollback_error

    def connect(self):
        return _SourceConnection(self.engine.connect(), self.events,
                                 outcome=self.outcome, close_error=self.close_error,
                                 rollback_error=self.rollback_error)

    def dispose(self):
        self.events.append("dispose")
        raise RuntimeError("simulated pool disposal failure")


def _asset(db, user):
    source = Connection(id=str(uuid.uuid4()), name="cleanup-fixture", kind="postgres", config={})
    db.add(source)
    db.flush()
    dataset = Dataset(id=str(uuid.uuid4()), name="cleanup-fixture", kind="integrated",
                      source_connection_id=source.id, source_resource="public.cleanup_rows")
    db.add(dataset)
    db.flush()
    asset = IntegratedAsset(
        id=str(uuid.uuid4()), dataset_id=dataset.id, connection_id=source.id,
        name="cleanup-fixture", schema_name="public", table_name="cleanup_rows",
        schema_json={"columns": [], "primary_key": ["id"]}, schema_fingerprint="cleanup-schema",
        writable=True, created_by=user.id,
    )
    db.add(asset)
    db.commit()
    return asset


@pytest.mark.parametrize("rollback_error", [False, True], ids=["rollback-ok", "rollback-error"])
@pytest.mark.parametrize("close_error", [False, True], ids=["dispose-only", "close-and-dispose"])
@pytest.mark.parametrize("outcome,error_status,error_code,source_rows", [
    ("applied", None, None, 1),
    ("conflict", 409, "ROW_ALREADY_EXISTS", 1),
    ("failed", 422, "UNKNOWN_COLUMN", 0),
    ("unknown", 409, "UNKNOWN_REQUIRES_RECONCILIATION", 1),
])
def test_cleanup_errors_preserve_durable_receipt_and_never_rewrite_source(
    db, admin_user, monkeypatch, close_error, rollback_error, outcome, error_status, error_code, source_rows,
):
    asset = _asset(db, admin_user)
    source_engine = sa.create_engine("sqlite://")
    table = sa.Table("cleanup_rows", sa.MetaData(),
                     sa.Column("id", sa.Integer, primary_key=True),
                     sa.Column("name", sa.String(40), nullable=False))
    table.create(source_engine)
    if outcome == "conflict":
        with source_engine.begin() as conn:
            conn.execute(table.insert().values(id=1, name="already-present"))
    events = []
    wrapper = _SourceEngine(source_engine, events, outcome=outcome,
                            close_error=close_error, rollback_error=rollback_error)
    monkeypatch.setattr(service, "source_engine", lambda *_: wrapper)
    monkeypatch.setattr(service, "reflect", lambda *_: (table, {}, "cleanup-schema"))
    monkeypatch.setattr(service, "check_transactional_table", lambda *_: None)
    values = {"missing": "rejected"} if outcome == "failed" else {"name": "committed"}
    body = RowMutation(key={"id": 1}, values=values, idempotency_key="cleanup-receipt")
    try:
        if error_status is None:
            result = service.mutate_row(db, admin_user, asset.id, "insert", body)
            assert result["status"] == "applied"
            assert result["affected_rows"] == 1
            assert result["row_token"]
        else:
            with pytest.raises(HTTPException) as exc:
                service.mutate_row(db, admin_user, asset.id, "insert", body)
            assert exc.value.status_code == error_status
            assert exc.value.detail["code"] == error_code
            assert exc.value.detail["mutation"]["status"] == outcome
        assert events == ["close", "dispose"]

        # Read in another control-plane session: in-memory ORM state is not
        # evidence that cleanup allowed the final audit commit to happen.
        with sa.orm.Session(db.get_bind()) as audit_db:
            record = audit_db.scalars(sa.select(IntegratedMutation)).one()
            assert record.status == outcome
            assert record.error_code == error_code
            assert record.completed_at is not None
            assert record.affected_rows == (1 if outcome == "applied" else 0)
            assert set(record.result_json) <= {"row_token"}
        with source_engine.connect() as conn:
            assert conn.scalar(sa.select(sa.func.count()).select_from(table)) == source_rows

        monkeypatch.setattr(service, "source_engine", lambda *_: pytest.fail("receipt replay must not rewrite source"))
        if outcome == "applied":
            assert service.mutate_row(db, admin_user, asset.id, "insert", body)["replayed"] is True
        else:
            with pytest.raises(HTTPException) as exc:
                service.mutate_row(db, admin_user, asset.id, "insert", body)
            assert exc.value.detail["code"] == error_code
    finally:
        source_engine.dispose()


@pytest.fixture
def verified_source(db, admin_user, monkeypatch):
    engines = []

    def setup(operation, *, trigger=None, rowcount=None, sane_rowcount=None, result_close_error=False):
        asset = _asset(db, admin_user)
        engine = sa.create_engine("sqlite://")
        engines.append(engine)
        table = sa.Table("cleanup_rows", sa.MetaData(),
                         sa.Column("id", sa.Integer, primary_key=True),
                         sa.Column("name", sa.String(40), nullable=False))
        table.create(engine)
        initial = [] if operation == "insert" else [(1, "original")]
        with engine.begin() as conn:
            if initial:
                conn.execute(table.insert().values(id=1, name="original"))
            if trigger:
                conn.exec_driver_sql(trigger)
        events, writes = [], []
        wrapper = _SourceEngine(engine, events, outcome="applied", close_error=True, rollback_error=True)
        monkeypatch.setattr(service, "source_engine", lambda *_: wrapper)
        monkeypatch.setattr(service, "reflect", lambda *_: (table, {}, "cleanup-schema"))
        monkeypatch.setattr(service, "check_transactional_table", lambda *_: None)
        execute = _SourceConnection.execute

        def track_execute(connection, statement, *args, **kwargs):
            result = execute(connection, statement, *args, **kwargs)
            if not any(getattr(statement, flag, False) for flag in ("is_insert", "is_update", "is_delete")):
                return result
            writes.append(statement.__visit_name__)

            def close():
                result.close()
                events.append("result-close")
                if result_close_error:
                    raise RuntimeError("simulated result cleanup failure")

            # Deliberate driver protocol double for unavailable/incorrect counts;
            # real PostgreSQL trigger behavior is covered separately in E2E.
            return SimpleNamespace(
                rowcount=result.rowcount if rowcount is None else rowcount,
                supports_sane_rowcount=lambda: result.supports_sane_rowcount() if sane_rowcount is None else sane_rowcount,
                close=close,
            )

        monkeypatch.setattr(_SourceConnection, "execute", track_execute)
        values = {} if operation == "delete" else {"name": "changed"}
        body = RowMutation(
            key={"id": 1}, values=values, idempotency_key="verified-write",
            expected_row_token=None if operation == "insert" else row_token({"id": 1, "name": "original"}, "cleanup-schema"),
        )
        return SimpleNamespace(asset=asset, engine=engine, table=table, body=body,
                               events=events, writes=writes, initial=initial)

    yield setup
    for engine in engines:
        engine.dispose()


def _assert_write_rejected_and_not_replayed(db, admin_user, monkeypatch, source, operation):
    with pytest.raises(HTTPException) as exc:
        service.mutate_row(db, admin_user, source.asset.id, operation, source.body)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "SOURCE_WRITE_NOT_APPLIED"
    assert exc.value.detail["mutation"]["status"] == "conflict"
    assert exc.value.detail["mutation"]["affected_rows"] == 0
    assert exc.value.detail["mutation"]["replayed"] is False
    assert source.events == ["result-close", "close", "dispose"]
    assert source.writes == [operation]
    with sa.orm.Session(db.get_bind()) as audit_db:
        record = audit_db.scalars(sa.select(IntegratedMutation)).one()
        assert record.status == "conflict" and record.affected_rows == 0
        assert record.error_code == "SOURCE_WRITE_NOT_APPLIED"
        assert record.completed_at is not None and record.result_json == {}
    with source.engine.connect() as conn:
        assert [tuple(row) for row in conn.execute(sa.select(source.table).order_by(source.table.c.id))] == source.initial
    monkeypatch.setattr(service, "source_engine", lambda *_: pytest.fail("rejected receipt must not repeat source DML"))
    with pytest.raises(HTTPException) as replay:
        service.mutate_row(db, admin_user, source.asset.id, operation, source.body)
    assert replay.value.status_code == 409
    assert replay.value.detail["code"] == "SOURCE_WRITE_NOT_APPLIED"
    assert replay.value.detail["mutation"]["replayed"] is True
    assert source.writes == [operation]


@pytest.mark.parametrize("operation", ["insert", "update", "delete"])
def test_trigger_suppressed_dml_is_rolled_back_and_never_receipted_applied(
    db, admin_user, monkeypatch, verified_source, operation,
):
    trigger = f"CREATE TRIGGER suppress_write BEFORE {operation.upper()} ON cleanup_rows BEGIN SELECT RAISE(IGNORE); END"
    source = verified_source(operation, trigger=trigger)
    _assert_write_rejected_and_not_replayed(db, admin_user, monkeypatch, source, operation)


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_trigger_moved_primary_key_is_rolled_back_without_a_false_receipt(
    db, admin_user, monkeypatch, verified_source, operation,
):
    trigger = (
        f"CREATE TRIGGER move_pk AFTER {operation.upper()} ON cleanup_rows WHEN NEW.id = 1 "
        "BEGIN UPDATE cleanup_rows SET id = 2 WHERE id = NEW.id; END"
    )
    source = verified_source(operation, trigger=trigger)
    _assert_write_rejected_and_not_replayed(db, admin_user, monkeypatch, source, operation)


def test_trigger_reinserted_delete_is_rolled_back_without_a_false_receipt(
    db, admin_user, monkeypatch, verified_source,
):
    trigger = (
        "CREATE TRIGGER replace_deleted AFTER DELETE ON cleanup_rows "
        "BEGIN INSERT INTO cleanup_rows (id, name) VALUES (OLD.id, OLD.name); END"
    )
    source = verified_source("delete", trigger=trigger)
    _assert_write_rejected_and_not_replayed(db, admin_user, monkeypatch, source, "delete")


@pytest.mark.parametrize("operation", ["insert", "update", "delete"])
@pytest.mark.parametrize("rowcount,sane_rowcount", [(-1, True), (0, True), (2, True), (1, False)])
def test_unproven_or_non_single_row_dml_is_rolled_back_before_commit(
    db, admin_user, monkeypatch, verified_source, operation, rowcount, sane_rowcount,
):
    source = verified_source(operation, rowcount=rowcount, sane_rowcount=sane_rowcount)
    _assert_write_rejected_and_not_replayed(db, admin_user, monkeypatch, source, operation)


@pytest.mark.parametrize("result_close_error", [False, True])
@pytest.mark.parametrize("operation", ["insert", "update", "delete", "noop-update"])
def test_verified_single_row_dml_and_matched_noop_survive_cleanup_errors(
    db, admin_user, monkeypatch, verified_source, operation, result_close_error,
):
    actual_operation = "update" if operation == "noop-update" else operation
    source = verified_source(actual_operation, result_close_error=result_close_error)
    if operation == "noop-update":
        source.body = source.body.model_copy(update={"values": {"name": "original"}})
    result = service.mutate_row(db, admin_user, source.asset.id, actual_operation, source.body)
    assert result["status"] == "applied" and result["affected_rows"] == 1
    assert result["replayed"] is False
    assert source.events == ["result-close", "close", "dispose"]
    assert source.writes == [actual_operation]
    expected = [] if operation == "delete" else [(1, "original" if operation == "noop-update" else "changed")]
    with source.engine.connect() as conn:
        assert [tuple(row) for row in conn.execute(sa.select(source.table))] == expected
    with sa.orm.Session(db.get_bind()) as audit_db:
        record = audit_db.scalars(sa.select(IntegratedMutation)).one()
        assert record.status == "applied" and record.affected_rows == 1
        assert record.completed_at is not None and record.error_code is None
        assert set(record.result_json) == (set() if operation == "delete" else {"row_token"})
    if operation != "delete":
        assert result["row_token"] == row_token({"id": 1, "name": expected[0][1]}, "cleanup-schema")
    monkeypatch.setattr(service, "source_engine", lambda *_: pytest.fail("applied replay must not repeat source DML"))
    assert service.mutate_row(db, admin_user, source.asset.id, actual_operation, source.body)["replayed"] is True
    assert source.writes == [actual_operation]
