"""集成 CDC 信封解码 / 落地 / 回环接收端聚焦测试。

覆盖：官方 Debezium JSON（Kafka JsonConverter 扁平 schema 字段、
``source.connector`` 方言、PG ``payload.transaction`` + ``source.lsn``）
解析、无损值解码、坐标与代际身份；ingest 的前沿/去重/漂移/占位/多订阅
扇出/容量分诊/健康落库语义；接收端的 Standard Webhooks 校验、事务边界
（先提交后 2xx）、生命周期类拒绝不覆盖 desired 状态、换证清证与 stop
无监听泄漏。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import socket
import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy.orm import sessionmaker

from app.data_channel.integrated import cdc_port
from app.data_channel.integrated.cdc_envelope import (
    UNAVAILABLE, EnvelopeError, activation_order, decode_envelope,
)
from app.data_channel.integrated.cdc_ingest import (
    STATUS_CAPACITY, STATUS_CAPTURED, STATUS_DUPLICATE, STATUS_SKIPPED,
    STATUS_STALE, CdcPermanentError, ingest,
)
from app.data_channel.integrated.cdc_receiver import create_receiver
from app.data_channel.integrated.models import (
    IntegratedCdcSource, IntegratedCdcSubscription,
)

# ---------------------------------------------------------------------------
# 官方 Debezium 信封构造器（Kafka JsonConverter 扁平 schema 字段形态）
# ---------------------------------------------------------------------------

FULL_COLUMNS = {
    "id": {"schema": {"type": "int32", "optional": False}},
    "balance": {"schema": {"type": "int32", "optional": False}},
    "amount": {"schema": {
        "type": "string", "optional": True,
        "name": "io.debezium.data.Decimal", "version": 1,
        "parameters": {"scale": "2"}}},
    "note": {"schema": {"type": "string", "optional": True}},
}

MYSQL_SOURCE_FIELDS = [
    {"type": "string", "optional": False, "field": "version"},
    {"type": "string", "optional": False, "field": "connector"},
    {"type": "string", "optional": False, "field": "name"},
    {"type": "int64", "optional": False, "field": "ts_ms"},
    {"type": "string", "optional": True, "field": "snapshot"},
    {"type": "string", "optional": False, "field": "db"},
    {"type": "string", "optional": True, "field": "table"},
    {"type": "int64", "optional": False, "field": "server_id"},
    {"type": "string", "optional": True, "field": "gtid"},
    {"type": "string", "optional": False, "field": "file"},
    {"type": "int64", "optional": False, "field": "pos"},
    {"type": "int32", "optional": False, "field": "row"},
]

PG_SOURCE_FIELDS = [
    {"type": "string", "optional": False, "field": "version"},
    {"type": "string", "optional": False, "field": "connector"},
    {"type": "string", "optional": False, "field": "name"},
    {"type": "int64", "optional": False, "field": "ts_ms"},
    {"type": "string", "optional": True, "field": "snapshot"},
    {"type": "string", "optional": False, "field": "db"},
    {"type": "string", "optional": True, "field": "sequence"},
    {"type": "string", "optional": True, "field": "schema"},
    {"type": "string", "optional": True, "field": "table"},
    {"type": "int64", "optional": True, "field": "txId"},
    {"type": "int64", "optional": True, "field": "lsn"},
    {"type": "int64", "optional": True, "field": "xmin"},
]


def _column_fields(columns: dict[str, dict]) -> list[dict]:
    entries = []
    for name, spec in columns.items():
        entry = dict(spec["schema"])
        entry["field"] = name
        entries.append(entry)
    return entries


def _envelope(*, name: str, op: str, before, after, source: dict,
              column_schemas: dict[str, dict], transaction: dict | None = None,
              source_fields: list, ts_ms: int = 1731288000000) -> dict:
    fields_spec = [
        {"type": "struct", "optional": True, "name": f"{name}.Key",
         "field": "before", "fields": _column_fields(column_schemas)},
        {"type": "struct", "optional": True, "name": f"{name}.Value",
         "field": "after", "fields": _column_fields(column_schemas)},
        {"type": "struct", "optional": False, "name": "io.debezium.connector.common.ServerName",
         "field": "source", "fields": source_fields},
        {"type": "string", "optional": False, "field": "op"},
        {"type": "int64", "optional": True, "field": "ts_ms"},
    ]
    payload = {
        "before": before, "after": after, "source": source,
        "op": op, "ts_ms": ts_ms, "transaction": transaction,
    }
    return {"schema": {"type": "struct", "name": f"{name}.Envelope",
                       "optional": False, "fields": fields_spec},
            "payload": payload}


def mysql_envelope(*, op="c", before=None, after=None, file="mysql-bin.000003",
                   pos=157, row=0, ts_ms=1731288000000,
                   column_schemas=None) -> dict:
    columns = column_schemas or FULL_COLUMNS
    source = {
        "version": "2.7.3.Final", "connector": "mysql", "name": "dbserver1",
        "ts_ms": ts_ms, "snapshot": "false", "db": "inventory",
        "sequence": None, "table": "accounts", "server_id": 223344,
        "gtid": None, "file": file, "pos": pos, "row": row,
        "thread": None, "query": None,
    }
    return _envelope(
        name="dbserver1.inventory.accounts", op=op, before=before, after=after,
        source=source, column_schemas=columns, source_fields=MYSQL_SOURCE_FIELDS,
        ts_ms=ts_ms)


def pg_envelope(*, op="c", before=None, after=None, lsn=2337048160,
                txid="580:792:", order=1, total_order=None, ts_ms=1731288000000,
                column_schemas=None) -> dict:
    columns = column_schemas or FULL_COLUMNS
    transaction = {"id": txid, "total_order": total_order or order,
                   "data_collection_order": order}
    source = {
        "version": "2.7.3.Final", "connector": "postgresql", "name": "pgserver",
        "ts_ms": ts_ms, "snapshot": "false", "db": "postgres",
        "sequence": None, "schema": "public", "table": "accounts",
        "txId": 580, "lsn": lsn, "xmin": None,
    }
    return _envelope(
        name="pgserver.public.accounts", op=op, before=before, after=after,
        source=source, column_schemas=columns, transaction=transaction,
        source_fields=PG_SOURCE_FIELDS, ts_ms=ts_ms)


HEARTBEAT = {
    "schema": {"type": "struct", "name": "io.debezium.connector.Heartbeat",
               "optional": False,
               "fields": [{"type": "int64", "optional": False, "field": "ts_ms"}]},
    "payload": {"ts_ms": 1731288000000},
}

TRANSACTION_BEGIN = {
    "schema": {"type": "struct",
               "name": "io.debezium.connector.transaction.Envelope",
               "fields": [
                   {"type": "string", "optional": True, "field": "status"},
                   {"type": "int32", "optional": False, "field": "event_count"},
                   {"type": "array", "optional": True, "field": "data_collections"},
               ]},
    "payload": {"status": "BEGIN", "id": "580:792:", "event_count": 2,
                "data_collections": [{"data_collection": "public.accounts",
                                      "event_count": 2}]},
}


# ---------------------------------------------------------------------------
# 信封解码
# ---------------------------------------------------------------------------

class TestEnvelopeDecode:

    def test_mysql_change_official_shape(self):
        event = decode_envelope(mysql_envelope(
            op="c", after={"id": 101, "balance": 250}))
        assert event.kind == "change"
        assert event.change_kind == "created"
        assert event.schema_name == "inventory"
        assert event.table_name == "accounts"
        assert event.after == {"id": 101, "balance": 250}
        assert event.coordinates.order == ("mysql-bin.", 3, 157, 0)
        assert event.coordinates.position == {
            "dialect": "mysql", "file": "mysql-bin.000003",
            "pos": 157, "row": 0}
        assert event.occurred_at.isoformat() == "2024-11-11T01:20:00+00:00"

    def test_event_id_generation_and_connection_salt(self):
        envelope = mysql_envelope(after={"id": 1, "balance": 1})
        event = decode_envelope(envelope)
        pk = {"id": 1}
        first = event.event_id(pk, connection_id="conn-1", generation=1)
        assert first == event.event_id(pk, connection_id="conn-1", generation=1)
        assert first != event.event_id(pk, connection_id="conn-2", generation=1)
        assert first != event.event_id(pk, connection_id="conn-1", generation=2)
        # 相邻同刻同内容事件坐标不同 → 身份不同
        other = decode_envelope(mysql_envelope(
            after={"id": 1, "balance": 1}, pos=158))
        assert first != other.event_id(
            pk, connection_id="conn-1", generation=1)

    def test_decimal_string_mode_and_precise_bytes(self):
        columns = {"amount": {"schema": {
            "type": "string", "optional": True,
            "name": "io.debezium.data.Decimal", "version": 1,
            "parameters": {"scale": "2"}}}}
        event = decode_envelope(mysql_envelope(
            after={"amount": "12.34"}, column_schemas=columns))
        assert event.after["amount"] == Decimal("12.34")
        precise = {"amount": {"schema": {
            "type": "bytes", "optional": True,
            "name": "io.debezium.data.Decimal", "version": 1,
            "parameters": {"scale": "2"}}}}
        raw = base64.b64encode((-1234).to_bytes(3, "big", signed=True)).decode()
        event = decode_envelope(mysql_envelope(
            after={"amount": raw}, column_schemas=precise))
        assert event.after["amount"] == Decimal("-12.34")

    def test_temporal_lossless_and_precision_loss(self):
        columns = {
            "created_at": {"schema": {
                "type": "int64", "optional": True,
                "name": "io.debezium.time.Timestamp", "version": 1}},
            "rate": {"schema": {
                "type": "int64", "optional": True,
                "name": "io.debezium.time.MicroTimestamp", "version": 1}},
            "opened": {"schema": {
                "type": "int32", "optional": True,
                "name": "io.debezium.time.Date", "version": 1}},
            "closed_at": {"schema": {
                "type": "string", "optional": True,
                "name": "io.debezium.time.ZonedTimestamp", "version": 1}},
        }
        event = decode_envelope(mysql_envelope(after={
            "created_at": 1731288000150,
            "rate": 1731288000150123,
            "opened": 20249,
            "closed_at": "2024-11-11T03:00:00+01:00",
        }, column_schemas=columns))
        assert event.after["created_at"] == "2024-11-11T01:20:00.150000+00:00"
        assert event.after["rate"] == "2024-11-11T01:20:00.150123+00:00"
        assert event.after["opened"] == "2025-06-10"
        assert event.after["closed_at"] == "2024-11-11T03:00:00+01:00"
        nano = {"x": {"schema": {
            "type": "int64", "optional": True,
            "name": "io.debezium.time.NanoTimestamp", "version": 1}}}
        with pytest.raises(EnvelopeError) as caught:
            decode_envelope(mysql_envelope(
                after={"x": 1_731_288_000_150_123_456},
                column_schemas=nano))
        assert caught.value.code == "TIME_PRECISION_LOSS"

    def test_toast_placeholder_marked_not_value(self):
        # 无逻辑名的普通 string/bytes 列同样以字面值占位：统一识别为
        # UNAVAILABLE（无法与真值区分时宁可 fail closed）。
        event = decode_envelope(mysql_envelope(after={
            "id": 1, "note": "__debezium_unavailable_value"}))
        assert event.after["note"] is UNAVAILABLE
        columns = dict(FULL_COLUMNS)
        columns["balance"] = {"schema": {
            "type": "string", "optional": True,
            "name": "io.debezium.data.special.UnavailableValuePlaceholder",
            "version": 1}}
        event = decode_envelope(mysql_envelope(
            after={"id": 1, "balance": "__debezium_unavailable_value"},
            column_schemas=columns))
        assert event.after["balance"] is UNAVAILABLE
        # 占位字面值落在非字符串类型列（int32）上：守卫先于类型校验，
        # 判为 UNAVAILABLE 而不是类型不匹配。
        event = decode_envelope(mysql_envelope(after={
            "id": 1, "balance": "__debezium_unavailable_value"}))
        assert event.after["balance"] is UNAVAILABLE

    def test_snapshot_read_and_schemaless_rejected(self):
        with pytest.raises(EnvelopeError) as caught:
            decode_envelope(mysql_envelope(op="r", after={"id": 1, "balance": 1}))
        assert caught.value.code == "SNAPSHOT_READ_EVENT"
        with pytest.raises(EnvelopeError) as caught:
            decode_envelope({"payload": {"op": "c"}})
        assert caught.value.code == "ENVELOPE_SCHEMA_MISSING"

    def test_unsupported_dialect(self):
        envelope = mysql_envelope(after={"id": 1, "balance": 1})
        envelope["payload"]["source"]["connector"] = "oracle"
        with pytest.raises(EnvelopeError) as caught:
            decode_envelope(envelope)
        assert caught.value.code == "DIALECT_UNSUPPORTED"

    def test_pg_identity_from_source_lsn_and_payload_transaction(self):
        event = decode_envelope(pg_envelope(
            after={"id": 9, "balance": 9}, lsn=1000, order=2))
        assert event.coordinates.order == (1000, 2)
        assert event.schema_name == "public"
        envelope = pg_envelope(after={"id": 9, "balance": 9})
        envelope["payload"]["transaction"] = None
        with pytest.raises(EnvelopeError) as caught:
            decode_envelope(envelope)
        assert caught.value.code == "SOURCE_IDENTITY_MISSING"

    def test_non_row_events_classified(self):
        assert decode_envelope(HEARTBEAT).kind == "heartbeat"
        assert decode_envelope(TRANSACTION_BEGIN).kind == "transaction"
        tombstone = {"schema": mysql_envelope()["schema"], "payload": None}
        assert decode_envelope(tombstone).kind == "tombstone"

    def test_binlog_numeric_suffix_ordering(self):
        old = decode_envelope(mysql_envelope(
            file="mysql-bin.000009", pos=999, after={"id": 1, "balance": 1}))
        new = decode_envelope(mysql_envelope(
            file="mysql-bin.000010", pos=5, after={"id": 1, "balance": 1}))
        assert old.coordinates.order < new.coordinates.order
        assert new.coordinates.order > activation_order(
            {"dialect": "mysql", "file": "mysql-bin.000009",
             "pos": 999, "row": 0})


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

CONTRACT = {"columns": [
    {"name": "id", "type": "integer", "nullable": False},
    {"name": "balance", "type": "integer", "nullable": False},
    {"name": "amount", "type": "decimal", "nullable": True, "scale": 2},
    {"name": "note", "type": "string", "nullable": True},
]}


class FakePort:
    def __init__(self, statuses=None, raise_on_call=False):
        self.events: list[dict] = []
        self.calls: list[list[dict]] = []
        self._statuses = statuses or ["captured"]
        self._raise = raise_on_call

    def capture_many(self, db, events):
        self.calls.append(events)
        self.events.extend(events)
        if self._raise:
            raise RuntimeError("boom")
        return [{"status": status} for status in self._statuses]


@pytest.fixture
def connection_row(db):
    from app.data_channel.connections.models import Connection

    db.add(Connection(id="conn-1", name="cdc", kind="mysql", status="active",
                      config={}))
    db.commit()
    return "conn-1"


@pytest.fixture
def source_row(db, connection_row):
    db.add(IntegratedCdcSource(
        connection_id="conn-1", generation=1, status="active"))
    db.commit()
    return "conn-1"


def add_subscription(db, *, suffix="1", status="active", generation=1,
                     activation=None, field_map=None, contract=None,
                     table="accounts", columns=None):
    row = IntegratedCdcSubscription(
        id=f"sub-{suffix}", connection_id="conn-1", asset_id=f"asset-{suffix}",
        ontology_id=f"onto-{suffix}", release_id=f"rel-{suffix}",
        mapping_id=f"map-{suffix}", object_type_id=f"type-{suffix}",
        field_map=field_map or {"id": "source_id", "balance": "balance",
                                "note": "note"},
        primary_key_columns=["id"], contract=contract if contract is not None
        else CONTRACT,
        schema_name="inventory", table_name=table,
        schema_fingerprint="fp", activation_position=activation,
        source_generation=generation, status=status)
    db.add(row)
    db.commit()
    return row


def install_port(monkeypatch, port) -> FakePort:
    monkeypatch.setattr(cdc_port, "get_cdc_ontology_port", lambda: port)
    return port


class TestIngest:

    def test_create_captured(self, db, source_row, monkeypatch):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000003",
            "pos": 100, "row": 0})
        port = install_port(monkeypatch, FakePort())
        result = ingest(db, "conn-1", 1, mysql_envelope(
            after={"id": 101, "balance": 250, "note": "n"}))
        assert result["status"] == STATUS_CAPTURED
        assert result["change_kind"] == "created"
        [event] = port.events
        assert event["ontology_id"] == "onto-1"
        assert event["change_kind"] == "created"
        assert event["changes"] == {
            "source_id": {"old": None, "new": 101},
            "balance": {"old": None, "new": 250},
            "note": {"old": None, "new": "n"},
        }
        assert event["snapshot"]["properties"] == {
            "source_id": 101, "balance": 250, "note": "n"}
        assert event["snapshot"]["objectTypeId"] == "type-1"
        assert event["instance_id"].startswith("remote:")
        assert event["event_group_id"] != event["source_event_id"]
        assert event["occurred_at"].isoformat() == "2024-11-11T01:20:00+00:00"
        source = db.get(IntegratedCdcSource, "conn-1")
        db.flush()
        assert source.last_event_at is not None
        assert source.last_position == {
            "dialect": "mysql", "file": "mysql-bin.000003",
            "pos": 157, "row": 0}

    def test_stale_before_activation_skips_capture(
            self, db, source_row, monkeypatch):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000004",
            "pos": 500, "row": 0})
        port = install_port(monkeypatch, FakePort())
        result = ingest(db, "conn-1", 1, mysql_envelope(
            file="mysql-bin.000003", pos=157,
            after={"id": 1, "balance": 1, "note": "n"}))
        assert result["status"] == STATUS_STALE
        assert port.events == []

    def test_mysql_position_duplicate(self, db, source_row, monkeypatch):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        source = db.get(IntegratedCdcSource, "conn-1")
        source.last_position = {
            "dialect": "mysql", "file": "mysql-bin.000003",
            "pos": 999, "row": 0}
        db.commit()
        port = install_port(monkeypatch, FakePort())
        result = ingest(db, "conn-1", 1, mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"}))
        assert result["status"] == STATUS_DUPLICATE
        assert port.events == []

    def test_pg_lower_row_lsn_after_higher_commit_not_dropped(
            self, db, monkeypatch):
        from app.data_channel.connections.models import Connection

        db.add(Connection(id="conn-pg", name="pg", kind="postgresql",
                          status="active", config={}))
        db.add(IntegratedCdcSource(
            connection_id="conn-pg", generation=1, status="active",
            last_position={"dialect": "postgresql", "lsn": 2000,
                           "data_collection_order": 9}))
        db.add(IntegratedCdcSubscription(
            id="sub-pg", connection_id="conn-pg", asset_id="asset-pg",
            ontology_id="onto", release_id="rel", mapping_id="map",
            object_type_id="type",
            field_map={"id": "source_id", "balance": "balance", "note": "note"},
            primary_key_columns=["id"], contract=CONTRACT,
            schema_name="public", table_name="accounts",
            schema_fingerprint="fp",
            activation_position={"dialect": "postgresql", "lsn": 100,
                                 "data_collection_order": 0},
            source_generation=1, status="active"))
        db.commit()
        port = install_port(monkeypatch, FakePort())
        result = ingest(db, "conn-pg", 1, pg_envelope(
            lsn=1500, order=1, after={"id": 7, "balance": 7, "note": "x"}))
        # 行 WAL LSN 低于已见坐标（并发事务交错提交）不得静默丢弃。
        assert result["status"] == STATUS_CAPTURED
        assert len(port.events) == 1

    def test_obsolete_generation_rejected(self, db, source_row):
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 2, mysql_envelope(
                after={"id": 1, "balance": 1, "note": "n"}))
        assert caught.value.code == "OBSOLETE_GENERATION"
        assert caught.value.report is False

    def test_lifecycle_rejects_do_not_report(self, db, connection_row):
        db.add(IntegratedCdcSource(
            connection_id="conn-1", generation=1, status="paused"))
        db.commit()
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 1, mysql_envelope(
                after={"id": 1, "balance": 1, "note": "n"}))
        assert caught.value.code == "SOURCE_NOT_ACTIVE"
        assert caught.value.report is False

    def test_connection_inactive(self, db, source_row):
        from app.data_channel.connections.models import Connection

        connection = db.get(Connection, "conn-1")
        connection.status = "inactive"
        db.commit()
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 1, mysql_envelope(
                after={"id": 1, "balance": 1, "note": "n"}))
        assert caught.value.code == "CONNECTION_INACTIVE"
        assert caught.value.report is False

    def test_unsubscribed_table_skipped(self, db, source_row, monkeypatch):
        port = install_port(monkeypatch, FakePort())
        result = ingest(db, "conn-1", 1, mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"}))
        assert result["status"] == STATUS_SKIPPED
        assert port.events == []

    def test_subscription_from_older_generation_skipped(
            self, db, source_row, monkeypatch):
        add_subscription(db, generation=0, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        port = install_port(monkeypatch, FakePort())
        result = ingest(db, "conn-1", 1, mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"}))
        assert result["status"] == STATUS_SKIPPED
        assert port.events == []

    def test_missing_activation_rejected(self, db, source_row):
        add_subscription(db, activation=None)
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 1, mysql_envelope(
                after={"id": 1, "balance": 1, "note": "n"}))
        assert caught.value.code == "SUBSCRIPTION_NOT_ACTIVATED"

    def test_drift_missing_frozen_column(self, db, source_row):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        envelope = mysql_envelope(
            after={"id": 1},
            column_schemas={"id": {"schema": {"type": "int32",
                                              "optional": False}}})
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 1, envelope)
        assert caught.value.code == "SCHEMA_DRIFT"

    def test_payload_schema_mismatch_rejected(self, db, source_row):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        envelope = mysql_envelope(after={"id": 1, "balance": "many", "note": "n"},
                                  column_schemas=FULL_COLUMNS)
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 1, envelope)
        assert caught.value.code == "ENVELOPE_VALUE_INVALID"

    def test_contract_type_and_scale_drift_rejected(self, db, source_row):
        add_subscription(
            db, activation={"dialect": "mysql", "file": "mysql-bin.000001",
                            "pos": 0, "row": 0},
            field_map={"id": "source_id", "balance": "balance",
                       "amount": "amount"})
        # 冻结契约 balance=integer，但源列已变成 string（DDL 漂移）。
        drifted = dict(FULL_COLUMNS)
        drifted["balance"] = {"schema": {"type": "string", "optional": False}}
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 1, mysql_envelope(
                after={"id": 1, "balance": "9", "amount": "1.00"},
                column_schemas=drifted))
        assert caught.value.code == "SCHEMA_DRIFT"
        # 冻结契约 amount scale=2，但源列 scale 已变成 4。
        rescaled = dict(FULL_COLUMNS)
        rescaled["amount"] = {"schema": {
            "type": "string", "optional": True,
            "name": "io.debezium.data.Decimal", "version": 1,
            "parameters": {"scale": "4"}}}
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 1, mysql_envelope(
                after={"id": 1, "balance": 9, "amount": "1.0000"},
                column_schemas=rescaled, pos=200))
        assert caught.value.code == "SCHEMA_DRIFT"

    def test_error_source_admitted_gap_and_stopped_rejected(
            self, db, connection_row, monkeypatch):
        db.add(IntegratedCdcSource(
            connection_id="conn-1", generation=1, status="error",
            last_error_code="EXECUTOR_NO_EVIDENCE"))
        db.commit()
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        port = install_port(monkeypatch, FakePort())
        result = ingest(db, "conn-1", 1, mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"},
            column_schemas=FULL_COLUMNS))
        # 瞬态 error 必须放行：否则永远收不到恢复所需的证据。
        assert result["status"] == STATUS_CAPTURED
        assert len(port.events) == 1
        source = db.get(IntegratedCdcSource, "conn-1")
        db.flush()
        # 状态/错误码不越权改写：恢复由 executor 依据接收证据裁决。
        assert source.status == "error"
        assert source.last_error_code == "EXECUTOR_NO_EVIDENCE"
        assert source.last_event_at is not None
        for blocked in ("gap", "stopped", "paused"):
            source.status = blocked
            db.commit()
            with pytest.raises(CdcPermanentError) as caught:
                ingest(db, "conn-1", 1, mysql_envelope(
                    after={"id": 2, "balance": 2, "note": "n"},
                    column_schemas=FULL_COLUMNS, pos=400))
            assert caught.value.code == "SOURCE_NOT_ACTIVE"
            assert caught.value.report is False

    def test_toast_placeholder_rejected(self, db, source_row):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        envelope = mysql_envelope(after={
            "id": 1, "balance": 5,
            "note": "__debezium_unavailable_value"})
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-1", 1, envelope)
        assert caught.value.code == "VALUE_UNAVAILABLE"

    def test_update_changes_and_unmapped_excluded(
            self, db, source_row, monkeypatch):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        port = install_port(monkeypatch, FakePort())
        envelope = mysql_envelope(
            op="u",
            before={"id": 1, "balance": 10, "note": "a"},
            after={"id": 1, "balance": 20, "note": "a"})
        result = ingest(db, "conn-1", 1, envelope)
        assert result["status"] == STATUS_CAPTURED
        [event] = port.events
        assert event["change_kind"] == "updated"
        assert event["changes"] == {"balance": {"old": 10, "new": 20}}
        assert event["snapshot"]["properties"] == {
            "source_id": 1, "balance": 20, "note": "a"}

    def test_delete_requires_complete_before_image(self, db, monkeypatch):
        from app.data_channel.connections.models import Connection

        db.add(Connection(id="conn-pg2", name="pg2", kind="postgresql",
                          status="active", config={}))
        db.add(IntegratedCdcSource(
            connection_id="conn-pg2", generation=1, status="active"))
        db.add(IntegratedCdcSubscription(
            id="sub-pg2", connection_id="conn-pg2", asset_id="a",
            ontology_id="o", release_id="r", mapping_id="m",
            object_type_id="t",
            field_map={"id": "source_id", "balance": "balance", "note": "note"},
            primary_key_columns=["id"], contract=CONTRACT,
            schema_name="public", table_name="accounts",
            schema_fingerprint="fp",
            activation_position={"dialect": "postgresql", "lsn": 100,
                                 "data_collection_order": 0},
            source_generation=1, status="active"))
        db.commit()
        port = install_port(monkeypatch, FakePort())
        # replica identity DEFAULT 的删除 before 只含主键：快照不完整 →
        # fail closed（契约以 replica identity FULL 为前置）。
        partial = pg_envelope(
            op="d", before={"id": 42}, after=None, lsn=300, order=1,
            column_schemas={
                "id": {"schema": {"type": "int32", "optional": False}}})
        with pytest.raises(CdcPermanentError) as caught:
            ingest(db, "conn-pg2", 1, partial)
        assert caught.value.code == "SCHEMA_DRIFT"
        assert port.events == []
        # 完整 before 像（replica identity FULL）正常删除。
        complete = pg_envelope(
            op="d", before={"id": 42, "balance": 7, "amount": "1.50",
                            "note": "z"},
            after=None, lsn=301, order=1)
        result = ingest(db, "conn-pg2", 1, complete)
        assert result["status"] == STATUS_CAPTURED
        [event] = port.events
        assert event["change_kind"] == "deleted"
        assert event["changes"] == {
            "source_id": {"old": 42, "new": None},
            "balance": {"old": 7, "new": None},
            "note": {"old": "z", "new": None}}
        # amount 列在 schema 中但未被冻结映射：绝不进入本体属性。

    def test_fanout_two_subscriptions_single_batch(
            self, db, source_row, monkeypatch):
        for suffix in ("1", "2"):
            add_subscription(db, suffix=suffix, activation={
                "dialect": "mysql", "file": "mysql-bin.000001",
                "pos": 0, "row": 0})
        port = install_port(monkeypatch, FakePort(
            statuses=["captured", "captured"]))
        result = ingest(db, "conn-1", 1, mysql_envelope(
            after={"id": 5, "balance": 5, "note": "n"}))
        assert result["status"] == STATUS_CAPTURED
        assert len(port.calls) == 1
        assert len(port.events) == 2
        first, second = port.events
        assert first["ontology_id"] != second["ontology_id"]
        assert first["instance_id"] != second["instance_id"]
        assert first["event_group_id"] != second["event_group_id"]

    def test_capacity_exceeded_returns_without_advance(
            self, db, source_row, monkeypatch):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        install_port(monkeypatch, FakePort(statuses=["capacity_exceeded"]))
        result = ingest(db, "conn-1", 1, mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"}))
        assert result["status"] == STATUS_CAPACITY
        source = db.get(IntegratedCdcSource, "conn-1")
        db.refresh(source)
        assert source.last_position is None
        assert source.last_event_at is None

    def test_replay_duplicate_status(self, db, source_row, monkeypatch):
        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        install_port(monkeypatch, FakePort(statuses=["duplicate"]))
        result = ingest(db, "conn-1", 1, mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"}))
        assert result["status"] == STATUS_DUPLICATE
        source = db.get(IntegratedCdcSource, "conn-1")
        db.flush()
        assert source.last_event_at is not None
        assert source.last_position is None

    def test_port_failure_is_transient(self, db, source_row, monkeypatch):
        from app.data_channel.integrated.cdc_ingest import CdcTransientError

        add_subscription(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        install_port(monkeypatch, FakePort(raise_on_call=True))
        with pytest.raises(CdcTransientError) as caught:
            ingest(db, "conn-1", 1, mysql_envelope(
                after={"id": 1, "balance": 1, "note": "n"}))
        assert caught.value.code == "CAPTURE_FAILED"


# ---------------------------------------------------------------------------
# 接收端
# ---------------------------------------------------------------------------

SECRET = bytes(range(32))


def sign(secret: bytes, body: bytes, *, webhook_id="msg-1",
         timestamp: int | None = None) -> dict[str, str]:
    seconds = int(time.time()) if timestamp is None else timestamp
    signed = f"{webhook_id}.{seconds}.".encode("utf-8") + body
    signature = base64.b64encode(
        hmac.new(secret, signed, hashlib.sha256).digest()).decode("ascii")
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": str(seconds),
        "webhook-signature": f"v1,{signature}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }


def post(port: int, path: str, headers: dict[str, str], body: bytes):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        client.request("POST", path, body=body, headers=headers)
        response = client.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else {}
    finally:
        client.close()


class TestReceiver:

    @pytest.fixture
    def session_factory(self, db):
        maker = sessionmaker(bind=db.get_bind())

        def factory():
            return maker()

        return factory

    @pytest.fixture
    def handle(self, session_factory):
        receiver = create_receiver(session_factory=session_factory)
        yield receiver
        receiver.stop()

    def _prepare(self, db, *, status="active", activation=None):
        from app.data_channel.connections.models import Connection

        db.add(Connection(id="conn-1", name="cdc", kind="mysql",
                          status="active", config={}))
        db.add(IntegratedCdcSource(
            connection_id="conn-1", generation=1, status=status))
        if activation is not None:
            db.add(IntegratedCdcSubscription(
                id="sub-1", connection_id="conn-1", asset_id="a",
                ontology_id="o", release_id="r", mapping_id="m",
                object_type_id="t",
                field_map={"id": "source_id", "balance": "balance",
                           "note": "note"},
                primary_key_columns=["id"], contract=CONTRACT,
                schema_name="inventory", table_name="accounts",
                schema_fingerprint="fp", activation_position=activation,
                source_generation=1, status="active"))
        db.commit()

    def test_signed_change_commits_before_ack(
            self, db, handle, monkeypatch):
        self._prepare(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        port = install_port(monkeypatch, FakePort())
        path = handle.register("conn-1", 1, SECRET).path
        assert path == "/cdc/conn-1/1"
        body = json.dumps(mysql_envelope(
            after={"id": 3, "balance": 30, "note": "x"})).encode()
        status, payload = post(handle.port, path, sign(SECRET, body), body)
        assert status == 200
        assert payload == {"status": "captured"}
        assert len(port.events) == 1
        source = db.get(IntegratedCdcSource, "conn-1")
        db.refresh(source)
        assert source.last_event_at is not None  # 2xx 前已同事务落库
        assert handle.last_accepted_at("conn-1", 1) is not None

    def test_unsigned_and_unknown_path_rejected(
            self, db, handle, monkeypatch):
        self._prepare(db)
        path = handle.register("conn-1", 1, SECRET).path
        body = json.dumps(HEARTBEAT).encode()
        status, _ = post(
            handle.port, path,
            {**sign(SECRET, body), "webhook-signature": "v1,AAAA"}, body)
        assert status == 401
        status, _ = post(handle.port, "/cdc/conn-1/2", sign(SECRET, body), body)
        assert status == 404
        stale = sign(SECRET, body, timestamp=int(time.time()) - 3600)
        status, _ = post(handle.port, path, stale, body)
        assert status == 401
        status, _ = post(handle.port, path,
                         {"Content-Length": str(len(body)),
                          "Content-Type": "application/json"}, body)
        assert status == 401

    def test_oversized_rejected(self, db):
        receiver = create_receiver(session_factory=lambda: None,
                                   max_request_bytes=8)
        try:
            path = receiver.register("conn-1", 1, SECRET).path
            body = b"x" * 64
            status, _ = post(receiver.port, path, sign(SECRET, body), body)
            assert status == 413
        finally:
            receiver.stop()

    def test_heartbeat_acked_and_evidence_tracked(
            self, db, handle):
        self._prepare(db)
        path = handle.register("conn-1", 1, SECRET).path
        body = json.dumps(HEARTBEAT).encode()
        status, payload = post(handle.port, path, sign(SECRET, body), body)
        assert status == 200
        assert payload == {"status": "heartbeat"}
        assert handle.last_heartbeat_at("conn-1") is not None
        source = db.get(IntegratedCdcSource, "conn-1")
        db.refresh(source)
        assert source.heartbeat_at is not None

    def test_permanent_reject_records_health_error(
            self, db, handle):
        self._prepare(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        path = handle.register("conn-1", 1, SECRET).path
        envelope = mysql_envelope(op="r", after={"id": 1, "balance": 1})
        body = json.dumps(envelope).encode()
        status, payload = post(handle.port, path, sign(SECRET, body), body)
        assert status == 409
        assert payload == {"error": "SNAPSHOT_READ_EVENT"}
        source = db.get(IntegratedCdcSource, "conn-1")
        db.refresh(source)
        assert source.status == "paused"  # 确定性拒绝：闸不再放行
        assert source.last_error_code == "SNAPSHOT_READ_EVENT"

    def test_lifecycle_reject_keeps_desired_state(
            self, db, handle):
        self._prepare(db, status="paused")
        path = handle.register("conn-1", 1, SECRET).path
        body = json.dumps(mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"})).encode()
        status, payload = post(handle.port, path, sign(SECRET, body), body)
        assert status == 409
        assert payload == {"error": "SOURCE_NOT_ACTIVE"}
        source = db.get(IntegratedCdcSource, "conn-1")
        db.refresh(source)
        assert source.status == "paused"  # 控制面 desired 不被观测错误覆盖
        assert source.last_error_code is None

    def test_capacity_rolls_back_pauses_all_503(
            self, db, handle, monkeypatch):
        self._prepare(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        db.add(IntegratedCdcSource(
            connection_id="conn-2", generation=1, status="active"))
        db.commit()
        install_port(monkeypatch, FakePort(statuses=["capacity_exceeded"]))
        path = handle.register("conn-1", 1, SECRET).path
        body = json.dumps(mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"})).encode()
        status, payload = post(handle.port, path, sign(SECRET, body), body)
        assert status == 503
        assert payload == {"error": "capacity_exceeded"}
        for connection_id in ("conn-1", "conn-2"):
            source = db.get(IntegratedCdcSource, connection_id)
            db.refresh(source)
            assert source.status == "paused"
            assert source.last_error_code == "EVENT_CAPACITY_EXCEEDED"

    def test_transient_failure_503_with_health_error(
            self, db, handle, monkeypatch):
        self._prepare(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        install_port(monkeypatch, FakePort(raise_on_call=True))
        path = handle.register("conn-1", 1, SECRET).path
        body = json.dumps(mysql_envelope(
            after={"id": 1, "balance": 1, "note": "n"})).encode()
        status, payload = post(handle.port, path, sign(SECRET, body), body)
        assert status == 503
        assert payload == {"error": "unavailable"}
        source = db.get(IntegratedCdcSource, "conn-1")
        db.refresh(source)
        assert source.status == "error"
        assert source.last_error_code == "CAPTURE_FAILED"

    def test_reregister_rotates_path_and_clears_evidence(
            self, db, handle):
        self._prepare(db)
        first = handle.register("conn-1", 1, SECRET).path
        body = json.dumps(HEARTBEAT).encode()
        post(handle.port, first, sign(SECRET, body), body)
        assert handle.last_accepted_at("conn-1", 1) is not None
        assert handle.last_heartbeat_at("conn-1") is not None
        second = handle.register("conn-1", 2, SECRET).path
        assert second == "/cdc/conn-1/2"
        assert handle.last_accepted_at("conn-1", 1) is None
        assert handle.last_heartbeat_at("conn-1") is None
        status, _ = post(handle.port, first, sign(SECRET, body), body)
        assert status == 404  # 旧代路径立即失效

    def test_stale_inflight_handler_cannot_fake_new_registration_evidence(
            self, db, handle):
        """同连接同代重启（路径相同）：旧注册的在途请求不得推进新注册证据。"""
        self._prepare(db)
        handle.register("conn-1", 1, SECRET)
        stale_registration = handle._by_path["/cdc/conn-1/1"]
        # supervisor 重启子进程：同代重注册，路径不变但注册对象换新。
        session = handle.register("conn-1", 1, bytes(range(1, 33)))
        assert session.path == "/cdc/conn-1/1"
        assert handle._by_path[session.path] is not stale_registration
        handle._record_accept(stale_registration, True)  # 旧在途请求迟到完成
        assert handle.last_accepted_at("conn-1", 1) is None
        assert handle.last_heartbeat_at("conn-1") is None
        # 旧密钥签名的请求在新注册下直接 401，同样不产生证据。
        body = json.dumps(HEARTBEAT).encode()
        status, _ = post(handle.port, session.path, sign(SECRET, body), body)
        assert status == 401
        assert handle.last_heartbeat_at("conn-1") is None

    def test_inflight_over_cap_replies_503(self, db, session_factory):
        self._prepare(db)
        receiver = create_receiver(session_factory=session_factory,
                                   max_inflight=1)
        idle = []
        try:
            path = receiver.register("conn-1", 1, SECRET).path
            # 两个半开连接占住在途额度，第三个请求有序 503（不断连）。
            for _ in range(2):
                idle.append(socket.create_connection(
                    ("127.0.0.1", receiver.port), timeout=5))
            deadline = time.time() + 5
            while (receiver._server._active < 2 and time.time() < deadline):
                time.sleep(0.01)
            body = json.dumps(HEARTBEAT).encode()
            status, payload = post(
                receiver.port, path, sign(SECRET, body), body)
            assert status == 503
            assert payload == {"error": "busy"}
            assert receiver.last_heartbeat_at("conn-1") is None
        finally:
            for sock in idle:
                sock.close()
            receiver.stop()

    def test_stop_releases_port_and_threads(self, db, session_factory):
        self._prepare(db)
        baseline = set(threading.enumerate())
        receiver = create_receiver(session_factory=session_factory)
        try:
            path = receiver.register("conn-1", 1, SECRET).path
            port = receiver.port
            body = json.dumps(HEARTBEAT).encode()
            for _ in range(3):  # 真实处理过请求，处理线程确实产生过
                status, _ = post(port, path, sign(SECRET, body), body)
                assert status == 200
            # 留一个半开连接：stop 必须主动断开在途 socket 而非挂死。
            idle = socket.create_connection(("127.0.0.1", port), timeout=5)
        finally:
            receiver.stop()
        receiver.stop()  # 幂等
        idle.close()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        leaked = [thread for thread in threading.enumerate()
                  if thread not in baseline and thread.is_alive()]
        assert leaked == []
        with pytest.raises(RuntimeError):
            receiver.register("conn-1", 2, SECRET)

    def test_rejects_non_loopback_and_weak_secret(self):
        # host 校验先于任何 socket 绑定：拒绝时不产生监听。
        for host in ("0.0.0.0", "::", "localhost", ""):
            with pytest.raises(ValueError):
                create_receiver(host=host, session_factory=lambda: None)
        receiver = create_receiver(session_factory=lambda: None)
        try:
            assert receiver.host == "127.0.0.1"
            for secret in (b"short", bytes(23), bytes(65), "x" * 32):
                with pytest.raises(ValueError):
                    receiver.register("conn-1", 1, secret)
            session = receiver.register("conn-1", 1, SECRET)
            assert "secret" not in repr(session).lower()
            assert SECRET.hex() not in repr(session)
        finally:
            receiver.stop()

    def test_no_value_logging_smoke(self, db, handle, monkeypatch):
        """错误路径只输出封闭错误码：不回显源值/异常文本。"""
        self._prepare(db, activation={
            "dialect": "mysql", "file": "mysql-bin.000001",
            "pos": 0, "row": 0})
        path = handle.register("conn-1", 1, SECRET).path
        secret_note = "TOP-SECRET-PAYLOAD-VALUE"
        envelope = mysql_envelope(after={
            "id": 1, "balance": 1, "note": secret_note})
        body = json.dumps(envelope).encode()
        install_port(monkeypatch, FakePort(raise_on_call=True))
        status, payload = post(handle.port, path, sign(SECRET, body), body)
        assert status == 503
        assert secret_note.encode() not in json.dumps(payload).encode()
