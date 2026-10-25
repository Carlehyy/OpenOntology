"""Opt-in real PostgreSQL native lane; NOT dbx registration/API acceptance.

Run only from a sanitized immutable runtime copy against the owned integrated
E2E stack. ORM seed deliberately bypasses unavailable dbx metadata registration.
Every source/platform object lives in a fresh UUID schema and is cleaned up.
"""
import os
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.auth.models import User
from app.database import Base
from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import native, service
from app.data_channel.integrated.contracts import RowMutation
from app.data_channel.integrated.models import IntegratedAsset, IntegratedMutation

pytestmark = pytest.mark.skipif(os.environ.get("INTEGRATED_NATIVE_PG_E2E") != "1",
                                reason="requires explicitly owned loopback PostgreSQL fixture stack")


def test_real_pg_native_snapshot_crud_conflicts_precision_and_cleanup():
    token = uuid.uuid4().hex
    schema = "source_crud_" + token
    source_url = "postgresql+psycopg2://integrated_e2e:integrated_e2e_fixture@127.0.0.1:45433/integrated_source"
    platform_url = "postgresql+psycopg2://integrated_e2e:integrated_e2e_fixture@127.0.0.1:45432/integrated_metadata"
    source_engine = sa.create_engine(source_url, hide_parameters=True)
    platform_root = sa.create_engine(platform_url, hide_parameters=True)
    platform = sa.create_engine(platform_url, connect_args={"options": "-csearch_path=" + schema}, hide_parameters=True)
    source_created = platform_created = False
    try:
        with source_engine.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            source_created = True
        with platform_root.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
            platform_created = True
        # Private schema, not a migration claim; full Alembic evidence is separate.
        Base.metadata.create_all(platform)
        source = sa.Table("rows", sa.MetaData(), sa.Column("id", sa.BigInteger, primary_key=True),
                          sa.Column("amount", sa.Numeric(30, 6), nullable=False),
                          sa.Column("day", sa.Date), sa.Column("stamp", sa.DateTime(timezone=True)),
                          sa.Column("blob", sa.LargeBinary), sa.Column("note", sa.Text), schema=schema)
        source.create(source_engine)
        with Session(platform) as db:
            actor = User(id=str(uuid.uuid4()), username="native_" + token,
                         email=token + "@example.test", role="admin", is_active=True,
                         password_hash="not-a-login-fixture")
            db.add(actor)
            db.flush()
            connection = Connection(id=str(uuid.uuid4()), name="native fixture", kind="postgres",
                                    config={"host": "127.0.0.1", "port": 45433, "database": "integrated_source",
                                            "user": "integrated_e2e", "password": "integrated_e2e_fixture"}, created_by=actor.id)
            db.add(connection)
            db.flush()
            dataset = Dataset(id=str(uuid.uuid4()), name="native fixture", kind="integrated",
                              source_connection_id=connection.id, source_resource=schema + ".rows")
            db.add(dataset)
            db.flush()
            with source_engine.connect() as conn:
                _, columns, fingerprint = native.reflect(conn, SimpleNamespace(schema_name=schema, table_name="rows"))
            asset = IntegratedAsset(id=str(uuid.uuid4()), dataset_id=dataset.id, connection_id=connection.id,
                                    name="native fixture", schema_name=schema, table_name="rows", writable=True,
                                    schema_json=columns, schema_fingerprint=fingerprint, created_by=actor.id)
            db.add(asset)
            db.commit()
            values = {"amount": {"$decimal": "123456789012345678.123456"}, "day": "2026-10-24",
                      "stamp": "2026-10-24T12:34:56.123456+08:00", "blob": {"$binary": "AP8="},
                      "note": "O'Reilly; DROP TABLE rows; --"}
            body = RowMutation(key={"id": {"$integer": "9007199254740993"}}, values=values, idempotency_key="insert")
            inserted = service.mutate_row(db, actor, asset.id, "insert", body)
            assert inserted["status"] == "applied"
            assert service.mutate_row(db, actor, asset.id, "insert", body)["replayed"] is True
            read = service.preview_rows(db, actor, asset.id, 10)
            assert read["source_state"]["consistent"] and read["source_state"]["complete"]
            observed = read["rows"][0]
            assert observed["row_token"] == inserted["row_token"]
            assert observed["values"]["id"] == body.key["id"]
            assert observed["values"]["amount"] == values["amount"]
            assert observed["values"]["blob"] == values["blob"]
            assert datetime.fromisoformat(observed["values"]["stamp"]) == datetime.fromisoformat(values["stamp"])
            with native.open_read_scope(db, actor, [asset.id]) as scope:
                statement = sa.select(scope.tables[asset.id])
                cursor_events = []

                def observe_cursor(conn, cursor, sql, parameters, context, executemany):
                    cursor_events.append({
                        "named": bool(cursor.name), "sql": sql.lower(),
                        "stream": context.execution_options.get("stream_results", False),
                        "yield_per": context.execution_options.get("yield_per"),
                        "max_row_buffer": context.execution_options.get("max_row_buffer"),
                    })

                sa.event.listen(scope.connection, "before_cursor_execute", observe_cursor)
                try:
                    before = list(scope.iter_batches(statement))[0][0]
                    streams = [event for event in cursor_events if event["stream"]]
                    assert len(streams) == 1 and streams[0]["named"]
                    assert streams[0]["yield_per"] == streams[0]["max_row_buffer"] == 1
                    options = scope.connection.get_execution_options()
                    assert options["stream_results"] is False and options["yield_per"] is None
                    assert scope.completion["complete"] is True
                    # The real driver must switch back to ordinary cursors for
                    # both EOF reflection and later session-control statements.
                    assert any("pg_catalog" in event["sql"] for event in cursor_events if not event["stream"])
                    scope.connection.execute(sa.text("SET LOCAL application_name = 'native_pg_fixture'"))
                    assert native.reflect(scope.connection, asset)[2] == fingerprint
                    assert all(not event["named"] for event in cursor_events if not event["stream"])
                    # Explicit generator cancellation must also release the named
                    # cursor, clear proof, and leave this same transaction usable.
                    stream = scope.iter_batches(statement, batch_size=1)
                    try:
                        assert next(stream) == [before]
                    finally:
                        stream.close()
                    options = scope.connection.get_execution_options()
                    assert options["stream_results"] is False and options["yield_per"] is None
                    assert scope.completion["complete"] is False and scope._busy is False
                    assert scope.connection.execute(sa.select(sa.literal(1))).scalar_one() == 1
                    assert cursor_events[-1]["named"] is False
                    with source_engine.begin() as external:
                        external.execute(source.update().values(note="external"))
                    again = list(scope.iter_batches(statement))[0][0]
                    assert again == before
                    assert again["amount"] == Decimal("123456789012345678.123456")
                    assert again["day"] == date(2026, 10, 24)
                    assert bytes(again["blob"]) == b"\x00\xff"
                finally:
                    sa.event.remove(scope.connection, "before_cursor_execute", observe_cursor)
            stale = RowMutation(key=body.key, values={"note": "stale"}, expected_row_token=inserted["row_token"], idempotency_key="stale")
            with pytest.raises(HTTPException) as exc:
                service.mutate_row(db, actor, asset.id, "update", stale)
            assert exc.value.detail["code"] == "ROW_CONFLICT"
            current = service.preview_rows(db, actor, asset.id, 1)["rows"][0]["row_token"]
            updated = service.mutate_row(db, actor, asset.id, "update", stale.model_copy(update={"idempotency_key": "update", "expected_row_token": current}))
            # Matched-row semantics: an ordinary no-op UPDATE is still applied.
            noop = service.mutate_row(db, actor, asset.id, "update", RowMutation(
                key=body.key, values={"note": "stale"}, expected_row_token=updated["row_token"],
                idempotency_key="noop-update",
            ))
            assert noop["status"] == "applied" and noop["affected_rows"] == 1
            assert noop["row_token"] == updated["row_token"]
            with source_engine.connect() as conn:
                protected_row = dict(conn.execute(sa.select(source)).mappings().one())
            # A genuine PG BEFORE trigger can suppress a matched UPDATE/DELETE.
            # The source command affected zero rows; it must never be reported as
            # applied merely because the pre-write CAS read found one row.
            assert schema == "source_crud_" + token and uuid.UUID(token).hex == token
            with source_engine.begin() as conn:
                conn.execute(sa.text(f'CREATE FUNCTION "{schema}".suppress_write() RETURNS trigger '
                                     'LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END; $$'))
                conn.execute(sa.text(f'CREATE TRIGGER suppress_write BEFORE UPDATE OR DELETE ON "{schema}".rows '
                                     f'FOR EACH ROW EXECUTE FUNCTION "{schema}".suppress_write()'))
            source_dml = []

            def observe_source_dml(conn, _cursor, sql, _parameters, _context, _many):
                verb = sql.lstrip().split(None, 1)[0].upper()
                if conn.engine.url.port == 45433 and verb in {"UPDATE", "DELETE"}:
                    source_dml.append(verb)

            sa.event.listen(sa.engine.Engine, "before_cursor_execute", observe_source_dml)
            try:
                for operation in ("update", "delete"):
                    blocked_body = RowMutation(
                        key=body.key, values={"note": "suppressed-update"} if operation == "update" else {},
                        expected_row_token=noop["row_token"], idempotency_key="trigger-suppressed-" + operation,
                    )
                    attempts = len(source_dml)
                    with pytest.raises(HTTPException) as suppressed:
                        service.mutate_row(db, actor, asset.id, operation, blocked_body)
                    assert len(source_dml) == attempts + 1 and source_dml[-1] == operation.upper()
                    assert suppressed.value.status_code == 409
                    assert suppressed.value.detail["code"] == "SOURCE_WRITE_NOT_APPLIED"
                    conflict = suppressed.value.detail["mutation"]
                    assert conflict["status"] == "conflict" and conflict["affected_rows"] == 0
                    assert conflict["error_code"] == "SOURCE_WRITE_NOT_APPLIED" and conflict["replayed"] is False
                    with pytest.raises(HTTPException) as replay:
                        service.mutate_row(db, actor, asset.id, operation, blocked_body)
                    assert replay.value.status_code == 409
                    assert replay.value.detail["code"] == "SOURCE_WRITE_NOT_APPLIED"
                    replayed = replay.value.detail["mutation"]
                    assert replayed["id"] == conflict["id"] and replayed["replayed"] is True
                    assert replayed["status"] == "conflict" and replayed["affected_rows"] == 0
                    assert len(source_dml) == attempts + 1, "idempotent conflict replay must not retry source DML"
                    with source_engine.connect() as conn:
                        assert dict(conn.execute(sa.select(source)).mappings().one()) == protected_row
                    assert service.preview_rows(db, actor, asset.id, 1)["rows"][0]["row_token"] == noop["row_token"]
            finally:
                sa.event.remove(sa.engine.Engine, "before_cursor_execute", observe_source_dml)
                assert schema == "source_crud_" + token and uuid.UUID(token).hex == token
                with source_engine.begin() as conn:
                    conn.execute(sa.text(f'DROP TRIGGER suppress_write ON "{schema}".rows'))
                    conn.execute(sa.text(f'DROP FUNCTION "{schema}".suppress_write()'))
            deleted = service.mutate_row(db, actor, asset.id, "delete", RowMutation(key=body.key, expected_row_token=noop["row_token"], idempotency_key="delete"))
            assert deleted["status"] == "applied"
            with source_engine.connect() as conn:
                assert conn.execute(sa.select(sa.func.count()).select_from(source)).scalar_one() == 0
            with native.open_snapshot(db, asset.id, user=actor, batch_size=1) as stream:
                assert list(stream.iter_batches()) == []
                assert stream.completion["complete"] is True
                assert stream.descriptor["expected_rows"] == 0
            with pytest.raises(HTTPException) as exc:
                with native.open_read_scope(db, actor, [asset.id], statement_timeout_ms=30) as scope:
                    list(scope.iter_batches(sa.select(sa.func.pg_sleep(0.2))))
            assert exc.value.detail["code"] == "QUERY_BUDGET_EXCEEDED"
            audits = db.scalars(sa.select(IntegratedMutation).where(IntegratedMutation.asset_id == asset.id)).all()
            assert len(audits) == 7 and {a.status for a in audits} == {"applied", "conflict"}
            assert sum(a.status == "applied" and a.affected_rows == 1 for a in audits) == 4
            assert sum(a.status == "conflict" and a.affected_rows == 0
                       and a.error_code == "SOURCE_WRITE_NOT_APPLIED" for a in audits) == 2
            assert all(values["note"] not in str(a.result_json) for a in audits)

            # Exercise the actual PG reflected typmods, not only _decode unit
            # fixtures: PostgreSQL otherwise rounds lossy values on assignment.
            temporal = sa.Table(
                "temporal_rows", sa.MetaData(),
                sa.Column("id", sa.BigInteger, primary_key=True),
                sa.Column("clock0", postgresql.TIME(precision=0), nullable=False),
                sa.Column("clock3", postgresql.TIME(precision=3), nullable=False),
                sa.Column("stamp0", postgresql.TIMESTAMP(precision=0), nullable=False),
                sa.Column("stamp3", postgresql.TIMESTAMP(precision=3), nullable=False),
                schema=schema,
            )
            temporal.create(source_engine)
            temporal_baseline = {
                "id": 1, "clock0": time(1, 2, 3), "clock3": time(1, 2, 3, 456000),
                "stamp0": datetime(2026, 10, 24, 1, 2, 3),
                "stamp3": datetime(2026, 10, 24, 1, 2, 3, 456000),
            }
            with source_engine.begin() as conn:
                conn.execute(temporal.insert().values(**temporal_baseline))
            temporal_dataset = Dataset(
                id=str(uuid.uuid4()), name="native temporal fixture", kind="integrated",
                source_connection_id=connection.id, source_resource=schema + ".temporal_rows",
            )
            db.add(temporal_dataset)
            db.flush()
            with source_engine.connect() as conn:
                reflected, temporal_schema, temporal_fingerprint = native.reflect(
                    conn, SimpleNamespace(schema_name=schema, table_name="temporal_rows"),
                )
            assert {name: reflected.c[name].type.precision for name in ("clock0", "clock3", "stamp0", "stamp3")} == {
                "clock0": 0, "clock3": 3, "stamp0": 0, "stamp3": 3,
            }
            assert all(reflected.c[name].type.timezone is False for name in ("clock0", "clock3", "stamp0", "stamp3"))
            temporal_asset = IntegratedAsset(
                id=str(uuid.uuid4()), dataset_id=temporal_dataset.id, connection_id=connection.id,
                name="native temporal fixture", schema_name=schema, table_name="temporal_rows", writable=True,
                schema_json=temporal_schema, schema_fingerprint=temporal_fingerprint, created_by=actor.id,
            )
            db.add(temporal_asset)
            db.commit()
            temporal_token = service.preview_rows(db, actor, temporal_asset.id, 1)["rows"][0]["row_token"]
            invalid_temporals = [
                ("clock0", "12:34:56.1"),
                ("clock3", "12:34:56.1234"),
                ("stamp0", "2026-10-24T12:34:56.1"),
                ("stamp3", "2026-10-24T12:34:56.1234"),
                ("clock0", "12:34:56+00:00"),
                ("clock3", "12:34:56.123+00:00"),
                ("stamp0", "2026-10-24T12:34:56+00:00"),
                ("stamp3", "2026-10-24T12:34:56.123+00:00"),
            ]
            for index, (column, value) in enumerate(invalid_temporals):
                with pytest.raises(HTTPException) as rejected:
                    service.mutate_row(db, actor, temporal_asset.id, "update", RowMutation(
                        key={"id": 1}, values={column: value}, expected_row_token=temporal_token,
                        idempotency_key=f"temporal-rejected-{index}",
                    ))
                assert rejected.value.status_code == 422
                assert rejected.value.detail["code"] == "INVALID_COLUMN_VALUE"
                mutation = rejected.value.detail["mutation"]
                assert mutation["status"] == "failed" and mutation["affected_rows"] == 0
                with source_engine.connect() as conn:
                    assert dict(conn.execute(sa.select(temporal)).mappings().one()) == temporal_baseline
                assert service.preview_rows(db, actor, temporal_asset.id, 1)["rows"][0]["row_token"] == temporal_token
            exact_temporals = {
                "clock0": "12:34:56", "clock3": "12:34:56.1230000",
                "stamp0": "2026-10-24T12:34:56", "stamp3": "2026-10-24T12:34:56.1230000",
            }
            temporal_receipt = service.mutate_row(db, actor, temporal_asset.id, "update", RowMutation(
                key={"id": 1}, values=exact_temporals, expected_row_token=temporal_token,
                idempotency_key="temporal-exact",
            ))
            assert temporal_receipt["status"] == "applied" and temporal_receipt["affected_rows"] == 1
            with source_engine.connect() as conn:
                assert dict(conn.execute(sa.select(temporal)).mappings().one()) == {
                    "id": 1, "clock0": time(12, 34, 56), "clock3": time(12, 34, 56, 123000),
                    "stamp0": datetime(2026, 10, 24, 12, 34, 56),
                    "stamp3": datetime(2026, 10, 24, 12, 34, 56, 123000),
                }
            temporal_read = service.preview_rows(db, actor, temporal_asset.id, 1)["rows"][0]
            assert temporal_read["row_token"] == temporal_receipt["row_token"] != temporal_token
            assert temporal_read["values"] == {
                "id": 1, "clock0": "12:34:56", "clock3": "12:34:56.123000",
                "stamp0": "2026-10-24T12:34:56", "stamp3": "2026-10-24T12:34:56.123000",
            }
            temporal_audits = db.scalars(sa.select(IntegratedMutation).where(
                IntegratedMutation.asset_id == temporal_asset.id,
            )).all()
            assert len(temporal_audits) == len(invalid_temporals) + 1
            assert sum(a.status == "failed" and a.error_code == "INVALID_COLUMN_VALUE" for a in temporal_audits) == len(invalid_temporals)
            assert sum(a.status == "applied" for a in temporal_audits) == 1
    finally:
        platform.dispose()
        # Exact UUID ownership is verified before each destructive schema drop.
        assert schema == "source_crud_" + token and uuid.UUID(token).hex == token
        if source_created:
            with source_engine.begin() as conn:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))
        if platform_created:
            with platform_root.begin() as conn:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))
        source_engine.dispose()
        platform_root.dispose()
