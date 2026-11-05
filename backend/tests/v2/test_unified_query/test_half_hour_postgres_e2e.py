"""Opt-in real PostgreSQL acceptance for the half-hour time bucket.

The half-hour bucket truncates to the hour and adds the half selected by the
minute component; the output keeps the timestamp type (never a date) and the
source timezone flag. Calendar buckets are covered by the SQLite/MySQL suites;
this module proves the PostgreSQL expression executes on a real engine.

Safety: explicit ``INTEGRATED_QUERY_E2E=1`` plus an owned loopback PostgreSQL
DSN; one UUID-named table is created and always dropped.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.data_channel.integrated import native
from app.data_channel.integrated.native import NativeSourceSession
from app.data_channel.query.compiler import compile_query
from app.data_channel.query.contracts import ColumnSpec, Relation
from app.data_channel.query.parser import Command, Pipeline, Ref

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATED_QUERY_E2E") != "1",
    reason="explicit isolated real-PostgreSQL opt-in required")


def _pg_url() -> str:
    raw = os.environ.get("INTEGRATED_QUERY_POSTGRES_DSN")
    if not raw:
        pytest.fail("INTEGRATED_QUERY_POSTGRES_DSN is required; no default/production fallback")
    url = sa.engine.make_url(raw)
    if (url.get_backend_name() != "postgresql" or url.host != "127.0.0.1"
            or url.database != "integrated_source" or url.query or "#" in raw):
        pytest.fail("DSN must be the owned loopback PostgreSQL integrated_source fixture database")
    return raw


def _pipeline():
    return Pipeline("t", "t", (
        Command("stats", (("count", None, "n"),
                          ("group", ((Ref("t", "occurred_at"), "bucket", "half_hour"),)))),
        Command("sort", (("bucket", "asc", "last"),)),
    ))


def _relation(table, *, timezone_flag=False):
    return Relation(
        "t", table,
        {"id": ColumnSpec("id", "integer", False),
         "occurred_at": ColumnSpec("occurred_at", "timestamp", timezone=timezone_flag)},
        {"dialect": "postgresql", "completeness": "complete"},
        "auth", "schema", "source:pg-half-hour")


def test_pg_half_hour_bucket_executes_and_floors_to_half_hours():
    engine = sa.create_engine(_pg_url())
    name = "uqe_half_" + uuid.uuid4().hex
    table = sa.Table(
        name, sa.MetaData(),
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True))
    table.create(engine)
    try:
        with engine.begin() as connection:
            connection.execute(table.insert(), [
                {"id": 1, "occurred_at": datetime(2026, 1, 1, 10, 0, 0)},
                {"id": 2, "occurred_at": datetime(2026, 1, 1, 10, 29, 59)},
                {"id": 3, "occurred_at": datetime(2026, 1, 1, 10, 30, 0)},
                {"id": 4, "occurred_at": datetime(2026, 1, 1, 10, 59, 59)},
                {"id": 5, "occurred_at": datetime(2026, 1, 1, 23, 59, 59)},
                {"id": 6, "occurred_at": None},
            ])
        compiled = compile_query(_pipeline(), {"t": _relation(table)}, {}, dialect="postgresql")
        assert compiled.columns[0].type == "timestamp"
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TIME ZONE 'UTC'")
            result = connection.execute(compiled.statement, compiled.params or {}).mappings()
            rows = [dict(row) for row in result]
        assert [(row["bucket"], int(row["n"])) for row in rows] == [
            (datetime(2026, 1, 1, 10, 0, 0), 2),
            (datetime(2026, 1, 1, 10, 30, 0), 2),
            (datetime(2026, 1, 1, 23, 30, 0), 1),
            (None, 1)]
    finally:
        table.drop(engine)
        engine.dispose()


def test_pg_native_read_port_executes_half_hour_and_keeps_raw_sql_denied():
    engine = sa.create_engine(_pg_url())
    name = "uqe_native_half_" + uuid.uuid4().hex
    table = sa.Table(
        name, sa.MetaData(),
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True))
    table.create(engine)
    connection = engine.connect()
    try:
        with connection.begin():
            connection.execute(table.insert(), [
                {"id": 1, "occurred_at": datetime(2026, 1, 1, 10, 0, 0)},
                {"id": 2, "occurred_at": datetime(2026, 1, 1, 10, 29, 59)},
                {"id": 3, "occurred_at": datetime(2026, 1, 1, 10, 30, 0)},
                {"id": 4, "occurred_at": datetime(2026, 1, 1, 10, 59, 59)},
                {"id": 5, "occurred_at": datetime(2026, 1, 1, 23, 59, 59)},
                {"id": 6, "occurred_at": None},
            ])
        asset = SimpleNamespace(
            id="asset-native-half", table_name=name, schema_name="public",
            dataset_id="dataset-native-half", connection_id="connection-native-half",
            schema_fingerprint=None)
        _, _schema, fingerprint = native.reflect(connection, asset)
        asset.schema_fingerprint = fingerprint
        scope = NativeSourceSession(
            connection, [asset],
            {asset.id: {"connection_id": asset.connection_id}},
            {"id": str(uuid.uuid4()), "started_at": "2026-11-05T00:00:00+00:00"})
        compiled = compile_query(
            _pipeline(), {"t": _relation(scope.tables[asset.id].alias("t"))},
            {}, dialect="postgresql")
        assert scope.expected_rows(compiled.statement, compiled.params) == 4
        rows = [row for batch in
                scope.iter_batches(compiled.statement, compiled.params, batch_size=2)
                for row in batch]
        assert [(row["bucket"], int(row["n"])) for row in rows] == [
            (datetime(2026, 1, 1, 10, 0, 0), 2),
            (datetime(2026, 1, 1, 10, 30, 0), 2),
            (datetime(2026, 1, 1, 23, 30, 0), 1),
            (None, 1)]
        assert scope.completion["complete"] is True
        assert scope.completion["row_count"] == 4
        # The guard is not loosened: raw fragments stay denied.
        for raw in (sa.text("1"), sa.literal_column("interval '30 minutes'")):
            with pytest.raises(HTTPException) as exc:
                scope.expected_rows(sa.select(raw))
            assert exc.value.status_code == 422
            assert exc.value.detail["code"] == "RAW_SQL_DENIED"
    finally:
        connection.close()
        table.drop(engine)
        engine.dispose()


def test_pg_half_hour_bucket_preserves_timestamptz_flag():
    meta = sa.MetaData()
    table = sa.Table(
        "half_hour_tz_probe", meta,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
    relation = _relation(table, timezone_flag=True)
    compiled = compile_query(_pipeline(), {"t": relation}, {}, dialect="postgresql")
    spec = next(c for c in compiled.columns if c.name == "bucket")
    assert spec.type == "timestamp" and spec.timezone is True
    # The expression keeps the timestamptz type end to end.
    sql = str(compiled.statement.compile(dialect=postgresql.dialect()))
    assert "date_trunc" in sql and "make_interval" in sql
