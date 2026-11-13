"""replica_codec 聚焦测试（纯函数；SQLite profile 下无 DB 依赖）。

契约被测面：decode_change_header 只认 c/u/d（r 属 initial snapshot 批量 COPY
通道，确定性拒绝）；decode_source_image 按 manifest + 信封列 schema 严格合约
（类型/scale/时间 tz 语义/列集合）做无损 wire 转换——JSON 原文不经浮点、
SQL NULL 与 JSON null 分立、真实 Kafka Connect Decimal（base64+scale）与 io
string 模式都精确到 ``$decimal``、位数溢出拒绝、float 拒绝 bool/非有限、
TOAST 占位（字面量或 UnavailableValuePlaceholder schema 名）映射 UNAVAILABLE
或确定性拒绝、PK 子集像（PG 默认 replica identity）支持。
"""
from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.data_channel.integrated import cdc_envelope
from app.data_channel.integrated.replica_codec import (
    UNAVAILABLE, ReplicaCodecError, decode_change_header, decode_source_image,
)

PRECISION_JSON = '{"pi": 3.14159265358979323846264338327950288}'
BLOB = b"\x00\xde\xad\xbe\xef"
BLOB_B64 = base64.b64encode(BLOB).decode("ascii")
MOMENT = "2026-11-12T09:30:00.123456+08:00"
TOAST = cdc_envelope.TOAST_PLACEHOLDER


def manifest(extra_columns=()):
    columns = [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "label", "type": "string", "nullable": True},
        {"name": "amount", "type": "decimal", "nullable": True,
         "precision": 12, "scale": 4},
        {"name": "ratio", "type": "float", "nullable": True},
        {"name": "flag", "type": "boolean", "nullable": True},
        {"name": "day", "type": "date", "nullable": True},
        {"name": "wall", "type": "time", "nullable": True},
        {"name": "moment", "type": "timestamp", "nullable": True, "timezone": True},
        {"name": "naive_moment", "type": "timestamp", "nullable": True},
        {"name": "doc", "type": "json", "nullable": True},
        {"name": "blob", "type": "binary", "nullable": True},
        {"name": "big_u", "type": "integer", "nullable": True, "unsigned": True},
    ]
    columns.extend(extra_columns)
    return {"columns": columns, "primary_key": ["id"]}


def field(name, schema):
    return {"field": name, "optional": True, **schema}


def default_fields():
    return [
        field("id", {"type": "int64"}),
        field("label", {"type": "string"}),
        field("amount", {"name": "io.debezium.data.Decimal", "type": "string",
                         "parameters": {"scale": "4"}}),
        field("ratio", {"type": "double"}),
        field("flag", {"type": "boolean"}),
        field("day", {"name": "io.debezium.time.Date", "type": "int32"}),
        field("wall", {"name": "io.debezium.time.MicroTime", "type": "int64"}),
        field("moment", {"name": "io.debezium.time.ZonedTimestamp", "type": "string"}),
        field("naive_moment", {"name": "io.debezium.time.MicroTimestamp", "type": "int64"}),
        field("doc", {"name": "io.debezium.data.Json", "type": "string"}),
        field("blob", {"type": "bytes"}),
        field("big_u", {"type": "int64"}),
    ]


def envelope(*, op="c", after=None, before=None, fields=None, source=None,
             connector="postgresql", table="orders", schema_name="public"):
    fields = default_fields() if fields is None else fields
    if source is None:
        source = {"connector": connector, "db": "oodb", "schema": schema_name,
                  "table": table, "ts_ms": 1760000000000}
    payload = {"op": op, "ts_ms": 1760000000000, "source": source}
    if before is not None:
        payload["before"] = before
    if after is not None:
        payload["after"] = after
    return {"schema": {"name": "oodb.public.orders.Envelope", "type": "struct", "fields": [
        {"field": "op", "type": "string"},
        {"field": "ts_ms", "type": "int64"},
        {"field": "source", "type": "struct"},
        {"field": "before", "type": "struct", "optional": True, "fields": fields},
        {"field": "after", "type": "struct", "optional": True, "fields": fields},
    ]}, "payload": payload}


def full_after(**override):
    after = {
        "id": 1, "label": "one", "amount": "123.4500", "ratio": 0.1, "flag": True,
        "day": 20406, "wall": 86399999999, "moment": MOMENT,
        "naive_moment": 1793000000000000, "doc": PRECISION_JSON,
        "blob": BLOB_B64, "big_u": 18446744073709551615,
    }
    after.update(override)
    return after


def rejects(error, fn):
    with pytest.raises(ReplicaCodecError) as exc:
        fn()
    assert exc.value.code == error
    return exc.value


# ---------------------------------------------------------------------------
# decode_source_image：全像无损转换
# ---------------------------------------------------------------------------

def test_full_image_converts_losslessly():
    decoded = decode_source_image(
        envelope(after=full_after()), manifest(), image="after")
    assert decoded["id"] == 1
    assert decoded["label"] == "one"
    assert decoded["amount"] == {"$decimal": "123.4500"}
    assert decoded["ratio"] == 0.1
    assert decoded["flag"] is True
    assert decoded["day"] == (date(1970, 1, 1) + timedelta(days=20406)).isoformat()
    assert decoded["wall"] == "23:59:59.999999"
    assert decoded["moment"] == MOMENT  # Zoned 串保留原始偏移
    naive = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        microseconds=1793000000000000)
    assert decoded["naive_moment"] == naive.replace(tzinfo=None).isoformat()
    assert decoded["doc"] == PRECISION_JSON  # JSON 原文逐字直达
    assert decoded["blob"] == {"$binary": BLOB_B64}
    assert decoded["big_u"] == {"$integer": "18446744073709551615"}  # 宽整数


def test_sql_null_and_json_null_stay_distinct():
    decoded = decode_source_image(
        envelope(after=full_after(doc=None, ratio=None)), manifest(), image="after")
    assert decoded["doc"] is None and decoded["ratio"] is None
    decoded = decode_source_image(
        envelope(after=full_after(doc="null")), manifest(), image="after")
    assert decoded["doc"] == "null"  # JSON null 文本不是 SQL NULL


def test_kafka_connect_precise_decimal_is_exact():
    def precise(unscaled, scale):
        raw = int(unscaled).to_bytes(max(1, (int(unscaled).bit_length() + 8) // 8),
                                     "big", signed=True)
        return base64.b64encode(raw).decode("ascii")

    def fields(scale="4"):
        built = default_fields()
        return [dict(entry, name="org.apache.kafka.connect.data.Decimal",
                     type="bytes", parameters={"scale": str(scale)})
                if entry["field"] == "amount" else entry for entry in built]

    decoded = decode_source_image(
        envelope(after=full_after(amount=precise(1234500, 4)), fields=fields()),
        manifest(), image="after")
    assert decoded["amount"] == {"$decimal": "123.4500"}
    decoded = decode_source_image(
        envelope(after=full_after(amount=precise(-105, 4)), fields=fields()),
        manifest(), image="after")
    assert decoded["amount"] == {"$decimal": "-0.0105"}


def variable_decimal(scale, unscaled):
    raw = int(unscaled).to_bytes(max(1, (int(unscaled).bit_length() + 8) // 8),
                                 "big", signed=True)
    return {"scale": scale, "value": base64.b64encode(raw).decode("ascii")}


def variable_manifest():
    built = manifest()
    for column in built["columns"]:
        if column["name"] == "amount":
            column.update(precision=None, scale=None)
    return built


def variable_fields():
    built = default_fields()
    return [dict(entry, name="io.debezium.data.VariableScaleDecimal", type="struct")
            if entry["field"] == "amount" else entry for entry in built]


def test_variable_scale_decimal_is_exact():
    # PostgreSQL 无精度约束 NUMERIC 在 precise 模式下发 VariableScaleDecimal
    # 结构（scale + 无标度字节）；必须与固定 scale Decimal 一样无损。
    decoded = decode_source_image(
        envelope(after=full_after(amount=variable_decimal(2, 123456)),
                 fields=variable_fields()), variable_manifest(), image="after")
    assert decoded["amount"] == {"$decimal": "1234.56"}
    decoded = decode_source_image(
        envelope(after=full_after(amount=variable_decimal(4, -105)),
                 fields=variable_fields()), variable_manifest(), image="after")
    assert decoded["amount"] == {"$decimal": "-0.0105"}
    decoded = decode_source_image(
        envelope(after=full_after(amount=variable_decimal(0, 0)),
                 fields=variable_fields()), variable_manifest(), image="after")
    assert decoded["amount"] == {"$decimal": "0"}
    huge = 10 ** 60 + 1
    decoded = decode_source_image(
        envelope(after=full_after(amount=variable_decimal(30, huge)),
                 fields=variable_fields()), variable_manifest(), image="after")
    assert decoded["amount"] == {"$decimal": str(
        Decimal((0, tuple(map(int, str(huge))), -30)))}


def test_variable_scale_decimal_invalid_shapes_rejected():
    for bad in ({"scale": -1, "value": "AQ=="},
                {"scale": True, "value": "AQ=="},
                {"scale": "2", "value": "AQ=="},
                {"scale": 2, "value": [1]},
                {"scale": 2},
                "not-a-struct",
                {"scale": 2, "value": "not base64!!"}):
        rejects("REPLICA_DECIMAL_INVALID", lambda bad=bad: decode_source_image(
            envelope(after=full_after(amount=bad), fields=variable_fields()),
            variable_manifest(), image="after"))
    # 固定 scale manifest 仍按自身 scale/precision 拒绝溢出，绝不四舍五入。
    rejects("REPLICA_DECIMAL_INVALID", lambda: decode_source_image(
        envelope(after=full_after(amount=variable_decimal(2, 10 ** 11)),
                 fields=variable_fields()), manifest(), image="after"))


def test_decimal_scale_and_precision_enforced():
    rejects("REPLICA_DECIMAL_INVALID", lambda: decode_source_image(
        envelope(after=full_after(amount="1.23456")), manifest(), image="after"))
    rejects("REPLICA_DECIMAL_INVALID", lambda: decode_source_image(
        envelope(after=full_after(amount="100000000.00")), manifest(), image="after"))
    # manifest scale 与信封 scale 声明不一致：schema 漂移，绝不猜测
    fields = default_fields()
    fields[2] = dict(fields[2], parameters={"scale": "2"})
    rejects("REPLICA_SCHEMA_DRIFT", lambda: decode_source_image(
        envelope(after=full_after(), fields=fields), manifest(), image="after"))


def test_float_rejects_bool_and_nonfinite():
    rejects("REPLICA_FLOAT_INVALID", lambda: decode_source_image(
        envelope(after=full_after(ratio=True)), manifest(), image="after"))
    rejects("REPLICA_FLOAT_INVALID", lambda: decode_source_image(
        envelope(after=full_after(ratio=float("nan"))), manifest(), image="after"))
    rejects("REPLICA_FLOAT_INVALID", lambda: decode_source_image(
        envelope(after=full_after(ratio=float("inf"))), manifest(), image="after"))
    rejects("REPLICA_FLOAT_INVALID", lambda: decode_source_image(
        envelope(after=full_after(ratio="0.1")), manifest(), image="after"))


def test_float_accepts_finite_decimal_from_receiver_boundary():
    # 接收端 parse_float=Decimal：源 float4/8 的 JSON 小数到这里是 Decimal，
    # 有限值必须按 native float 语义接收，绝不当 REPLICA_FLOAT_INVALID。
    record = json.loads(json.dumps(envelope(after=full_after(ratio=0.1))),
                        parse_float=Decimal)
    value = record["payload"]["after"]["ratio"]
    assert isinstance(value, Decimal)
    decoded = decode_source_image(record, manifest(), image="after")
    assert decoded["ratio"] == 0.1 and isinstance(decoded["ratio"], float)
    for wire, expected in ((Decimal("-1.25"), -1.25),
                           (Decimal("1E+3"), 1000.0),
                           (Decimal("1.7976931348623157E+308"), 1.7976931348623157e308),
                           (Decimal("1E-400"), 0.0)):
        decoded = decode_source_image(
            envelope(after=full_after(ratio=wire)), manifest(), image="after")
        assert decoded["ratio"] == expected
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
                Decimal("1E+400")):
        rejects("REPLICA_FLOAT_INVALID", lambda bad=bad: decode_source_image(
            envelope(after=full_after(ratio=bad)), manifest(), image="after"))


def test_unsigned_bigint_precise_decimal_contract():
    # MySQL 3.7.1 bigint.unsigned.handling.mode=precise：BIGINT UNSIGNED 的
    # wire 是 Kafka Decimal(bytes, scale=0, precision=20)，不是 int64。
    def precise_fields():
        built = default_fields()
        return [dict(entry, name="org.apache.kafka.connect.data.Decimal",
                     type="bytes",
                     parameters={"scale": "0", "connect.decimal.precision": "20"})
                if entry["field"] == "big_u" else entry for entry in built]

    def encoded(unscaled):
        size = max(1, (int(unscaled).bit_length() + 8) // 8)
        return base64.b64encode(
            int(unscaled).to_bytes(size, "big", signed=True)).decode("ascii")

    max_u64 = 2 ** 64 - 1
    decoded = decode_source_image(
        envelope(after=full_after(big_u=encoded(max_u64)),
                 fields=precise_fields()), manifest(), image="after")
    assert decoded["big_u"] == {"$integer": str(max_u64)}
    decoded = decode_source_image(
        envelope(after=full_after(big_u=encoded(42)),
                 fields=precise_fields()), manifest(), image="after")
    assert decoded["big_u"] == 42
    rejects("REPLICA_INTEGER_INVALID", lambda: decode_source_image(
        envelope(after=full_after(big_u=encoded(-1)),
                 fields=precise_fields()), manifest(), image="after"))
    rejects("REPLICA_DECIMAL_INVALID", lambda: decode_source_image(
        envelope(after=full_after(big_u="not base64!!"),
                 fields=precise_fields()), manifest(), image="after"))
    # 非 0 scale 的 Decimal 不是合法 unsigned bigint 契约。
    scaled = precise_fields()
    scaled[-1] = dict(scaled[-1], parameters={"scale": "1",
                                             "connect.decimal.precision": "20"})
    rejects("REPLICA_SCHEMA_DRIFT", lambda: decode_source_image(
        envelope(after=full_after(big_u=encoded(42)), fields=scaled),
        manifest(), image="after"))


def test_placeholders_map_to_marker_or_reject():
    literal = envelope(after=full_after(blob=TOAST))
    rejects("REPLICA_VALUE_UNAVAILABLE", lambda: decode_source_image(
        literal, manifest(), image="after"))
    decoded = decode_source_image(
        literal, manifest(), image="after", allow_unavailable=True)
    assert decoded["blob"] is UNAVAILABLE
    fields = default_fields()
    fields[10] = dict(fields[10], name=cdc_envelope._PLACEHOLDER_SCHEMA)
    schema_named = envelope(after=full_after(blob="ignored"), fields=fields)
    decoded = decode_source_image(
        schema_named, manifest(), image="after", allow_unavailable=True)
    assert decoded["blob"] is UNAVAILABLE


def test_contract_drift_rejected():
    def swap(schema):
        fields = default_fields()
        fields[1] = dict(fields[1], **schema)
        return fields

    rejects("REPLICA_SCHEMA_DRIFT", lambda: decode_source_image(
        envelope(after=full_after(), fields=swap({"type": "int64"})),
        manifest(), image="after"))  # label string != integer
    rejects("REPLICA_SCHEMA_DRIFT", lambda: decode_source_image(
        envelope(after=full_after(), fields=swap(
            {"name": "io.debezium.time.MicroTimestamp", "type": "int64"})),
        manifest(), image="after"))  # zoned 语义不一致
    rejects("REPLICA_SCHEMA_DRIFT", lambda: decode_source_image(
        envelope(after={"id": 1}), manifest(), image="after"))  # 列集合缺失
    rejects("REPLICA_SCHEMA_DRIFT", lambda: decode_source_image(
        envelope(after={**full_after(), "extra": 1}), manifest(), image="after"))
    # zoned time 无法回读（store 拒绝 time with timezone）
    fields = default_fields()
    fields[6] = dict(fields[6], name="io.debezium.time.ZonedTime")
    rejects("REPLICA_SCHEMA_DRIFT", lambda: decode_source_image(
        envelope(after=full_after(), fields=fields), manifest(), image="after"))


def test_integer_and_unsigned_semantics():
    rejects("REPLICA_INTEGER_INVALID", lambda: decode_source_image(
        envelope(after=full_after(id=True)), manifest(), image="after"))
    rejects("REPLICA_INTEGER_INVALID", lambda: decode_source_image(
        envelope(after=full_after(big_u=-1)), manifest(), image="after"))
    decoded = decode_source_image(
        envelope(after=full_after(big_u="18446744073709551615")),
        manifest(), image="after")
    assert decoded["big_u"] == {"$integer": "18446744073709551615"}


def test_json_and_binary_validation():
    rejects("REPLICA_JSON_INVALID", lambda: decode_source_image(
        envelope(after=full_after(doc="{oops")), manifest(), image="after"))
    rejects("REPLICA_JSON_INVALID", lambda: decode_source_image(
        envelope(after=full_after(doc=12)), manifest(), image="after"))
    rejects("REPLICA_BINARY_INVALID", lambda: decode_source_image(
        envelope(after=full_after(blob="not base64!!")), manifest(), image="after"))


def test_subset_image_for_primary_key_only_before():
    record = envelope(op="d", before=full_after(), after=None)
    decoded = decode_source_image(record, manifest(), image="before", columns=["id"])
    assert decoded == {"id": 1}
    # 子集模式：未知列名属 manifest 无效；越出 manifest 的像键属 schema 漂移
    rejects("REPLICA_MANIFEST_INVALID", lambda: decode_source_image(
        record, manifest(), image="before", columns=["id", "missing"]))
    rejects("REPLICA_SCHEMA_DRIFT", lambda: decode_source_image(
        envelope(op="d", before={**full_after(), "extra": 1}, after=None),
        manifest(), image="before", columns=["id"]))
    # 主键为 NULL/占位：调用方（apply）按 IMAGE_INCOMPLETE 拒绝
    null_pk = decode_source_image(
        envelope(op="d", before=full_after(id=None), after=None),
        manifest(), image="before", columns=["id"])
    assert null_pk == {"id": None}


def test_missing_image_returns_none_and_invalid_shapes():
    assert decode_source_image(
        envelope(op="c", after=None), manifest(), image="before") is None
    rejects("REPLICA_ENVELOPE_INVALID", lambda: decode_source_image(
        {"schema": {}, "payload": "x"}, manifest(), image="after"))
    rejects("REPLICA_ENVELOPE_INVALID", lambda: decode_source_image(
        envelope(after=full_after()), manifest(), image="middle"))
    rejects("REPLICA_MANIFEST_INVALID", lambda: decode_source_image(
        envelope(after=full_after()), {"columns": [], "primary_key": ["id"]},
        image="after"))


# ---------------------------------------------------------------------------
# decode_change_header：仅 c/u/d，r 拒绝
# ---------------------------------------------------------------------------

def test_header_extracts_structure_without_decoding_values():
    header = decode_change_header(envelope(after=full_after(doc="{oops")))
    assert (header.op, header.dialect, header.schema_name, header.table_name) == \
        ("c", "postgresql", "public", "orders")
    assert header.after["id"] == 1 and header.before is None
    mysql = decode_change_header(envelope(
        connector="mysql", after=full_after(),
        source={"connector": "mysql", "db": "oodb", "table": "orders",
                "file": "mysql-bin.000003", "pos": 4, "row": 0, "ts_ms": 1}))
    assert (mysql.dialect, mysql.schema_name) == ("mysql", "oodb")


@pytest.mark.parametrize("mutate,code", [
    (lambda e: e["payload"].__setitem__("op", "r"), "REPLICA_SNAPSHOT_OP"),
    (lambda e: e["payload"].__setitem__("op", "t"), "REPLICA_OP_UNSUPPORTED"),
    (lambda e: e["payload"].__setitem__("op", 7), "REPLICA_OP_UNSUPPORTED"),
    (lambda e: e["payload"].__setitem__("source", None), "REPLICA_SOURCE_INVALID"),
    (lambda e: e["payload"]["source"].__setitem__("connector", "oracle"),
     "REPLICA_DIALECT_UNSUPPORTED"),
    (lambda e: e["payload"]["source"].__delitem__("table"), "REPLICA_SOURCE_INVALID"),
    (lambda e: e["payload"].__setitem__("after", None), "REPLICA_IMAGE_MISSING"),
])
def test_header_rejects_invalid_events(mutate, code):
    record = envelope(after=full_after())
    mutate(record)
    rejects(code, lambda: decode_change_header(record))


def test_header_delete_requires_before_and_update_requires_after():
    rejects("REPLICA_IMAGE_MISSING", lambda: decode_change_header(
        envelope(op="d", before=None)))
    header = decode_change_header(envelope(op="d", before=full_after(), after=None))
    assert header.op == "d" and header.before["id"] == 1
    rejects("REPLICA_IMAGE_MISSING", lambda: decode_change_header(
        envelope(op="u", before=full_after(), after=None)))
