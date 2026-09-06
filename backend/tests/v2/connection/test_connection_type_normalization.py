"""连接同步的类型归一化与列契约测试。

覆盖三块：
1. 方言物理类型 → 湖词表映射（穷举 + 标志 + 未识别降级）；
2. 数据库原生值 → JSON 可存储标量（datetime/Decimal/bytes 曾让整次
   同步 TypeError，这是本组测试钉死的回归）；
3. 同步路径的列契约：内省优先、采样兜底、降级留痕。
"""
import base64
import datetime as dt
import decimal
import json
import logging
import uuid as uuid_module

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import mysql, postgresql

from app.data_channel.connections import type_normalization as tn
from app.data_channel.connections.models import Connection
from app.data_channel.connections.sql_connector import SQLConnector
from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.tasks.v2.connection_sync import sync_connection


@pytest.mark.parametrize(
    ("raw_type", "expected_type", "expected_flags"),
    [
        # 整数族（含 MySQL 限定词/参数与 PG 别名）
        ("TINYINT", "integer", ()),
        ("SMALLINT UNSIGNED", "integer", ()),
        ("MEDIUMINT", "integer", ()),
        ("INT", "integer", ()),
        ("int(11)", "integer", ()),
        ("BIGINT", "integer", ()),
        ("SERIAL", "integer", ()),
        ("BIGSERIAL", "integer", ()),
        ("YEAR", "integer", ()),
        ("BIT", "integer", ("bit",)),
        # 数值：湖词表没有 decimal，定点/金额必须有损标志
        ("FLOAT", "float", ()),
        ("DOUBLE", "float", ()),
        ("DOUBLE PRECISION", "float", ()),
        ("REAL", "float", ()),
        ("DECIMAL(10,2)", "float", ("lossy",)),
        ("NUMERIC(20,4)", "float", ("lossy",)),
        ("MONEY", "float", ("lossy", "money")),
        # 布尔（MySQL 事实布尔别名）
        ("BOOLEAN", "boolean", ()),
        ("BOOL", "boolean", ()),
        ("TINYINT(1)", "boolean", ("mysql_bool_alias",)),
        ("tinyint(1) unsigned", "boolean", ("mysql_bool_alias",)),
        # 文本族
        ("CHAR(36)", "string", ()),
        ("VARCHAR(255)", "string", ()),
        ("CHARACTER VARYING(100)", "string", ()),
        ("TEXT", "string", ()),
        ("LONGTEXT", "string", ()),
        ("UUID", "string", ()),
        ("ENUM('a','b')", "string", ("enum",)),
        ("SET('x','y')", "string", ("set",)),
        ("INET", "string", ()),
        # 时间族：湖词表只有 timestamp；纯时刻/时长没有湖类型
        ("DATE", "timestamp", ("date_only",)),
        ("DATETIME", "timestamp", ()),
        ("TIMESTAMP", "timestamp", ()),
        ("TIMESTAMP WITH TIME ZONE", "timestamp", ("tz_aware",)),
        ("TIMESTAMPTZ", "timestamp", ("tz_aware",)),
        ("TIMESTAMP WITHOUT TIME ZONE", "timestamp", ()),
        ("TIME", "string", ("time_of_day",)),
        ("INTERVAL", "string", ("duration",)),
        # 结构化与二进制
        ("JSON", "json", ()),
        ("JSONB", "json", ()),
        ("BLOB", "string", ("binary",)),
        ("VARBINARY(64)", "string", ("binary",)),
        ("BYTEA", "string", ("binary",)),
        # PG 数组容器收敛为 json
        ("INTEGER[]", "json", ("array",)),
        ("TEXT[]", "json", ("array",)),
        # 空间
        ("GEOMETRY", "string", ("spatial",)),
        # 未识别方言类型：降级 string + unmapped，绝不阻断入湖
        ("SOME_FUTURE_EXTENSION", "string", ("unmapped",)),
        ("", "string", ("unmapped",)),
        (None, "string", ("unmapped",)),
    ],
)
def test_sql_type_mapping(raw_type, expected_type, expected_flags):
    lake_type, flags = tn.map_sql_type(raw_type)
    assert lake_type == expected_type
    assert tuple(flags) == expected_flags


@pytest.mark.parametrize(
    ("raw_type", "expected_type", "expected_flags"),
    [
        # 真实方言 TypeEngine 对象：str() 会丢构造参数，标志必须来自对象属性。
        # 标志顺序固定为「对象探测标志在前、映射表标志在后」。
        (postgresql.TIMESTAMP(timezone=True), "timestamp", ("tz_aware",)),
        (postgresql.TIMESTAMP(), "timestamp", ()),
        (postgresql.TIME(timezone=True), "string", ("tz_aware", "time_of_day")),
        (mysql.TINYINT(display_width=1), "boolean", ("mysql_bool_alias",)),
        (mysql.TINYINT(display_width=4), "integer", ()),
        (postgresql.ARRAY(postgresql.INTEGER()), "json", ("array",)),
        (postgresql.ARRAY(postgresql.TEXT()), "json", ("array",)),
        (postgresql.ARRAY(postgresql.NUMERIC(10, 2)), "json",
         ("lossy", "array")),
        (postgresql.JSONB(), "json", ()),
        (mysql.DATETIME(fsp=6), "timestamp", ()),
    ],
)
def test_sql_type_mapping_with_real_dialect_engines(
        raw_type, expected_type, expected_flags):
    lake_type, flags = tn.map_sql_type(raw_type)
    assert lake_type == expected_type
    assert tuple(flags) == expected_flags
    # normalize_sql_column 的 source_type 也必须保留构造参数信息
    entry = tn.normalize_sql_column("c", raw_type)
    assert entry["type"] == expected_type
    assert entry["flags"] == list(expected_flags)


def test_scalar_array_payload_does_not_crash_sync(db, monkeypatch):
    """REST 端点返回标量数组是存量可工作输入，列契约推断不得让同步崩溃。"""

    class _ScalarListConnector:
        def list_resources(self):
            return ["tags"]

        def pull_full(self, _resource):
            return ["a", 1, {"mixed": "row"}]

    connection = _make_connection(db, "conn-scalar-array", kind="rest")
    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: _ScalarListConnector(),
    )

    result = sync_connection(connection.id, db=db)

    assert result["status"] == "ok"
    dataset = db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id).one()
    version = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset.id).one()
    assert json.loads(bytes(version.data_blob).decode("utf-8")) == [
        "a", 1, {"mixed": "row"}]
    # 标量行没有列概念；dict 行的列照常进入契约
    schema = dataset.schema_json
    assert schema["types_source"] == "sample_inference"
    assert schema["columns"] == ["mixed"]


def test_normalize_cell_converts_native_values_to_json_scalars():
    aware = dt.timezone(dt.timedelta(hours=8))
    assert tn.normalize_cell(
        dt.datetime(2026, 9, 6, 10, 30, 0)) == "2026-09-06T10:30:00"
    assert tn.normalize_cell(
        dt.datetime(2026, 9, 6, 10, 30, 0, tzinfo=aware)) == "2026-09-06T10:30:00+08:00"
    assert tn.normalize_cell(dt.date(2026, 9, 6)) == "2026-09-06"
    assert tn.normalize_cell(dt.time(10, 30, 5)) == "10:30:05"
    assert tn.normalize_cell(dt.timedelta(hours=1, minutes=5)) == "1:05:00"
    assert tn.normalize_cell(decimal.Decimal("123.45")) == "123.45"
    assert tn.normalize_cell(b"\x00\xff") == "AP8="
    assert tn.normalize_cell(uuid_module.UUID(
        "12345678-1234-5678-1234-567812345678")) == "12345678-1234-5678-1234-567812345678"
    # 嵌套容器递归归一化（Mongo/PG jsonb 场景）
    nested = tn.normalize_cell({
        "created": dt.datetime(2026, 9, 6, 10, 30, 0),
        "tags": [decimal.Decimal("0.5"), b"\x01"],
    })
    assert nested == {"created": "2026-09-06T10:30:00", "tags": ["0.5", "AQ=="]}
    # JSON 原生标量与 None 原样透传
    for value in (None, True, 1, 1.5, "text"):
        assert tn.normalize_cell(value) is value
    # 归一化输出必须整体可 JSON 序列化（P0 回归）
    json.dumps(tn.normalize_rows([{
        "at": dt.datetime(2026, 9, 6, 10, 30),
        "amount": decimal.Decimal("9.99"),
        "payload": b"\x00",
        "meta": {"when": dt.date(2026, 9, 6)},
    }]))


def test_normalize_sql_column_keeps_dialect_metadata():
    entry = tn.normalize_sql_column("amount", "DECIMAL(10,2)")
    assert entry == {
        "name": "amount",
        "type": "float",
        "source_type": "DECIMAL(10,2)",
        "flags": ["lossy"],
    }
    assert tn.normalize_sql_column("id", "BIGINT") == {
        "name": "id", "type": "integer", "source_type": "BIGINT", "flags": [],
    }


def test_introspected_columns_typed_keeps_contract_skinny():
    # 输入是连接器契约（已完成归一化），拆分层不得二次映射
    typed, source_schema = tn.introspected_columns_typed([
        {"name": "id", "type": "integer", "source_type": "BIGINT", "flags": []},
        {"name": "amount", "type": "float",
         "source_type": "DECIMAL(10,2)", "flags": ["lossy"]},
        {"name": "", "type": "string", "source_type": "TEXT", "flags": []},
    ])
    assert typed == [
        {"name": "id", "type": "integer"},
        {"name": "amount", "type": "float"},
    ]
    assert source_schema == {
        "id": {"source_type": "BIGINT", "flags": []},
        "amount": {"source_type": "DECIMAL(10,2)", "flags": ["lossy"]},
    }


def test_sql_connector_introspect_schema_reflects_metadata(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE orders ("
            "id INTEGER, name TEXT, price NUMERIC(10, 2), created DATETIME)"))
        conn.commit()

    connector = SQLConnector({})
    monkeypatch.setattr(SQLConnector, "_get_engine", lambda self: engine)

    columns = {c["name"]: c for c in connector.introspect_schema("orders")}
    assert columns["id"]["type"] == "integer"
    assert columns["name"]["type"] == "string"
    assert columns["price"]["type"] == "float"
    assert "lossy" in columns["price"]["flags"]
    assert columns["created"]["type"] == "timestamp"
    assert columns["price"]["source_type"].upper().startswith("NUMERIC")

    # 资源名仍走标识符白名单，反射失败必须抛出而不是返回空清单
    with pytest.raises(ValueError):
        connector.introspect_schema("bad;drop table orders")


class _TypedConnector:
    """带元数据内省的连接器：返回数据库原生 Python 值。"""

    def list_resources(self):
        return ["orders"]

    def pull_full(self, _resource):
        return [{
            "id": 1,
            "amount": decimal.Decimal("123.45"),
            "created_at": dt.datetime(2026, 9, 6, 10, 30, 0),
            "payload": b"\x00\xff",
        }]

    def introspect_schema(self, _resource):
        return [
            {"name": "id", "type": "integer", "source_type": "BIGINT", "flags": []},
            {"name": "amount", "type": "float",
             "source_type": "DECIMAL(10,2)", "flags": ["lossy"]},
            {"name": "created_at", "type": "timestamp",
             "source_type": "DATETIME", "flags": []},
            {"name": "payload", "type": "string",
             "source_type": "BLOB", "flags": ["binary"]},
        ]


class _UntypedConnector:
    """未实现内省的存量连接器（rest/file/aihot 形态）。"""

    def list_resources(self):
        return ["events"]

    def pull_full(self, _resource):
        return [{"title": "t", "created_at": dt.datetime(2026, 9, 6, 10, 30, 0)}]


class _BrokenIntrospectionConnector(_UntypedConnector):
    def introspect_schema(self, _resource):
        raise RuntimeError("permission denied")


def _make_connection(db, conn_id, kind="mysql"):
    connection = Connection(
        id=conn_id, name=conn_id, kind=kind, config={}, status="inactive",
    )
    db.add(connection)
    db.commit()
    return connection


def test_sync_normalizes_native_values_and_persists_introspected_contract(
        db, monkeypatch):
    connection = _make_connection(db, "conn-typed")
    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: _TypedConnector(),
    )

    result = sync_connection(connection.id, db=db)

    assert result["status"] == "ok"
    dataset = db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id).one()
    version = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset.id).one()

    # P0 回归：原生 datetime/Decimal/bytes 不再让同步失败，且落库为湖契约
    # 可存储的 ISO/base64 文本
    rows = json.loads(bytes(version.data_blob).decode("utf-8"))
    assert rows == [{
        "id": 1,
        "amount": "123.45",
        "created_at": "2026-09-06T10:30:00",
        "payload": "AP8=",
    }]

    schema = dataset.schema_json
    assert schema["types_source"] == "connector_introspection"
    assert schema["columns"] == ["id", "amount", "created_at", "payload"]
    assert schema["columns_typed"] == [
        {"name": "id", "type": "integer"},
        {"name": "amount", "type": "float"},
        {"name": "created_at", "type": "timestamp"},
        {"name": "payload", "type": "string"},
    ]
    assert schema["source_schema"]["amount"] == {
        "source_type": "DECIMAL(10,2)", "flags": ["lossy"]}


def test_sync_falls_back_to_sample_inference_without_introspection(
        db, monkeypatch, caplog):
    connection = _make_connection(db, "conn-untyped", kind="rest")
    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: _UntypedConnector(),
    )

    with caplog.at_level(logging.WARNING, logger="app.tasks.v2.connection_sync"):
        result = sync_connection(connection.id, db=db)

    assert result["status"] == "ok"
    dataset = db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id).one()
    schema = dataset.schema_json
    assert schema["types_source"] == "sample_inference"
    assert "source_schema" not in schema
    types = {c["name"]: c["type"] for c in schema["columns_typed"]}
    # 归一化后的 ISO 字符串在采样推断下识别为 timestamp
    assert types["created_at"] == "timestamp"
    assert types["title"] == "string"
    # 未实现内省是正常形态，不是故障，不得产生告警噪音
    assert not any(
        "introspection" in record.getMessage() for record in caplog.records)


def test_sync_introspection_failure_degrades_with_traceable_warning(
        db, monkeypatch, caplog):
    connection = _make_connection(db, "conn-broken-introspection")
    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: _BrokenIntrospectionConnector(),
    )

    with caplog.at_level(logging.WARNING, logger="app.tasks.v2.connection_sync"):
        result = sync_connection(connection.id, db=db)

    assert result["status"] == "ok"
    dataset = db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id).one()
    assert dataset.schema_json["types_source"] == "sample_inference"
    assert any("permission denied" in record.getMessage() for record in caplog.records)


class _FakeStorage:
    def put_bytes(self, bucket, key, data, *_args, **_kwargs):
        return f"s3://{bucket}/{key}"

    def delete_object(self, _uri):
        return None


def test_sync_unstructured_payload_keeps_schema_untouched(db, monkeypatch):
    connection = _make_connection(db, "conn-bytes", kind="rest")

    class _BytesConnector:
        def list_resources(self):
            return ["dump"]

        def pull_full(self, _resource):
            return b"binary blob"

    monkeypatch.setattr(
        "app.services.connection.registry.get_connector",
        lambda _kind, _config: _BytesConnector(),
    )
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service",
        lambda: _FakeStorage(),
    )

    result = sync_connection(connection.id, db=db)

    assert result["status"] == "ok"
    dataset = db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id).one()
    assert dataset.kind == "unstructured"
    assert dataset.schema_json in (None, {})
