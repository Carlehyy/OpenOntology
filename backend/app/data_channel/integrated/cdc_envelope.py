"""Debezium JSON 信封解码 — 集成 CDC 事件进入平台前的唯一形态校验层。

受支持形态（其余全部确定性拒绝，绝不静默降级）：

- 官方 Debezium JSON converter 输出且 ``schemas.enable=true``：Kafka
  JsonConverter 的 schema 字段是扁平的（字段项自身即 schema，字段名在
  ``field`` 键），``before``/``after`` 是 value schema 下的内嵌 struct；
- 仅 MySQL 与 PostgreSQL 变更事件（``op`` ∈ c/u/d）：方言由
  ``source.connector``（"mysql"/"postgresql"）判定；快照 READ（``r``）在
  no-data 无回填架构下拒绝；心跳、事务元数据（BEGIN/END）与墓碑按非行
  事件显式分类，由接收端有意确认；
- 身份坐标必须是不可变源坐标，而非时间戳/内容哈希：MySQL file/pos/row，
  PostgreSQL lsn + payload.transaction.id + data_collection_order（缺省
  回退 total_order）。``source_event_id`` 由连接器（含运行代 generation
  命名空间）+ 坐标 + op + 命名空间主键哈希派生：同一事件重试稳定；同刻
  同内容的合法相邻事件坐标不同故天然区分；PG 同一 lsn 内同行多次更新/
  主键变更对由 order（必要时 op+主键）区分。PG ``lsn`` 按事件上报值参与
  比较且只做 fail-closed 前沿判断 — 行 WAL LSN 顺序与提交输出顺序不同，
  不承诺提交时间覆盖，也不用行 LSN 做全局去重；
- 值按 schema 严格无损解码：Decimal（运行时 ``decimal.handling.mode=
  string`` 按字符串解析，precise 的 base64 字节同样实现，均由 schema 逻
  辑名 + scale 参数还原精确 ``Decimal``）、Date/Time/Timestamp 系列（整数
  精确换算，纳秒余数拒绝而非截断）、Zoned*（保留原始偏移的 ISO 串）、
  Json（解析为结构）、TOAST/不可用占位符返回 ``UNAVAILABLE`` 标记，由
  ingest 对映射字段拒绝。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.shared.exact_json import dumps_exact

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
EPOCH_DATE = date(1970, 1, 1)
TOAST_PLACEHOLDER = "__debezium_unavailable_value"
_PLACEHOLDER_SCHEMA = "io.debezium.data.special.UnavailableValuePlaceholder"
_CHANGE_SUFFIX = ".Envelope"
_HEARTBEAT_SUFFIX = ".Heartbeat"

DIALECT_MYSQL = "mysql"
DIALECT_POSTGRES = "postgresql"
_CONNECTOR_DIALECTS = {"mysql": DIALECT_MYSQL, "postgresql": DIALECT_POSTGRES}

KIND_CHANGE = "change"
KIND_HEARTBEAT = "heartbeat"
KIND_TRANSACTION = "transaction"
KIND_TOMBSTONE = "tombstone"

# 与 Sentinel 现有词汇一致（EVENT_KIND_*），不发明新状态词。
CHANGE_KINDS = {"c": "created", "u": "updated", "d": "deleted"}

_DAY_MICROS = 86_400 * 1_000_000
_BINLOG_SUFFIX = re.compile(r"^(?P<stem>.*?)(?P<digits>\d+)$")


class EnvelopeError(Exception):
    """确定性（永久）拒绝：信封不符合受支持的 Debezium 形态。

    ``code`` 属封闭集合；``message`` 只描述结构缺陷，绝不携带源行值。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Unavailable:
    """TOAST/不可用占位单例：绝不作为真实值进入本体。"""

    def __new__(cls):
        if not hasattr(cls, "_instance"):
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - 诊断展示
        return "UNAVAILABLE"


UNAVAILABLE = _Unavailable()


@dataclass(frozen=True)
class SourceCoordinates:
    """不可变源身份坐标（同一 connector 命名空间内可全序比较）。"""

    dialect: str
    identity: dict      # 参与事件身份哈希的规范坐标（JSON 安全）
    order: tuple        # 激活前沿比较用的全序键
    position: dict      # 可安全发布的 sourcePosition（snake 键，含 dialect）


@dataclass(frozen=True)
class DecodedEvent:
    kind: str
    connector: str
    change_kind: str | None
    schema_name: str | None
    table_name: str | None
    before: dict | None
    after: dict | None
    available_fields: frozenset
    after_column_schemas: dict[str, dict]
    before_column_schemas: dict[str, dict]
    occurred_at: datetime | None
    coordinates: SourceCoordinates | None
    op: str | None

    @property
    def authoritative_schemas(self) -> dict[str, dict]:
        """契约校验用列 schema：after 像优先（权威行形态），删除事件回退
        before 像（可空性标记在部分 before 形态不可信，调用方需跳过）。"""
        if self.after is not None:
            return self.after_column_schemas
        return self.before_column_schemas

    def event_id(self, pk: dict[str, Any], *, connection_id: str,
                 generation: int) -> str:
        """确定性事件身份：连接器 + connection_id/generation 盐 + 坐标 +
        op + 命名空间主键，绝不掺时间戳/正文。换连接/换代后同坐标同主键
        不会误碰撞（重指向时 topic 前缀可能复用）。"""
        material = {
            "connector": self.connector,
            "connection": str(connection_id),
            "coordinates": self.coordinates.identity,
            "dialect": self.coordinates.dialect,
            "generation": int(generation),
            "op": self.op,
            "pk": {str(key): pk[key] for key in sorted(pk)},
        }
        return hashlib.sha256(
            dumps_exact(material).encode("utf-8")).hexdigest()


def event_group_id(*, ontology_id: str, release_id: str, mapping_id: str,
                   object_type_id: str, source_event_id: str) -> str:
    """行级事件分组（64hex）：同一事件的字段事实同组，不同行事件绝不合并。

    事务/gtid 不作为分组 — 同事务不同行的事件不得并入同一组。
    """
    material = dumps_exact({
        "mapping": str(mapping_id),
        "objectType": str(object_type_id),
        "ontology": str(ontology_id),
        "release": str(release_id),
        "sourceEvent": str(source_event_id),
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 解码入口
# ---------------------------------------------------------------------------

def decode_envelope(raw: Any) -> DecodedEvent:
    """把一条 HTTP 投递的 Debezium value 信封解码为受控事件或非行事件。"""
    if not isinstance(raw, dict):
        raise EnvelopeError("ENVELOPE_INVALID", "record must be a JSON object")
    schema = raw.get("schema")
    if not isinstance(schema, dict) or not schema.get("name"):
        raise EnvelopeError(
            "ENVELOPE_SCHEMA_MISSING",
            "Debezium JSON converter must run with schemas.enable=true")
    payload = raw.get("payload")
    name = str(schema["name"])
    if payload is None:
        return _non_change(KIND_TOMBSTONE, payload)
    if not isinstance(payload, dict):
        raise EnvelopeError("ENVELOPE_INVALID", "payload must be an object")
    if name.endswith(_HEARTBEAT_SUFFIX):
        return _non_change(KIND_HEARTBEAT, payload)
    status = payload.get("status")
    if isinstance(status, str) and status in {"BEGIN", "END"}:
        return _non_change(KIND_TRANSACTION, payload)
    if not name.endswith(_CHANGE_SUFFIX):
        raise EnvelopeError(
            "ENVELOPE_SCHEMA_UNEXPECTED",
            f"unsupported record schema {name!r}")
    return _decode_change(schema, payload)


def _non_change(kind: str, payload) -> DecodedEvent:
    return DecodedEvent(
        kind=kind, connector="", change_kind=None, schema_name=None,
        table_name=None, before=None, after=None, available_fields=frozenset(),
        after_column_schemas={}, before_column_schemas={},
        occurred_at=_payload_ts(payload), coordinates=None, op=None)


def _payload_ts(payload: Any) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    ts = payload.get("ts_ms")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return _epoch_datetime(int(ts) * 1000)
    return None


# ---------------------------------------------------------------------------
# 变更事件
# ---------------------------------------------------------------------------

def _decode_change(schema: dict, payload: dict) -> DecodedEvent:
    op = payload.get("op")
    if not isinstance(op, str):
        raise EnvelopeError("ENVELOPE_INVALID", "change op is required")
    if op == "r":
        raise EnvelopeError(
            "SNAPSHOT_READ_EVENT",
            "snapshot READ events are not accepted in no-data CDC")
    if op not in CHANGE_KINDS:
        raise EnvelopeError("ENVELOPE_INVALID", f"unsupported op {op!r}")

    source = payload.get("source")
    if not isinstance(source, dict):
        raise EnvelopeError(
            "ENVELOPE_SOURCE_INVALID", "source block is required")
    connector = source.get("connector")
    if not isinstance(connector, str) or not connector:
        raise EnvelopeError(
            "ENVELOPE_SOURCE_INVALID", "source.connector is required")
    dialect = _CONNECTOR_DIALECTS.get(connector)
    if dialect is None:
        raise EnvelopeError(
            "DIALECT_UNSUPPORTED", f"unsupported source connector {connector!r}")

    fields = _schema_fields(schema, "value schema")
    before, before_schemas = _decode_image(payload.get("before"), fields, "before")
    after, after_schemas = _decode_image(payload.get("after"), fields, "after")
    if op in {"c", "u"} and after is None:
        raise EnvelopeError("IMAGE_MISSING", f"{op} event requires after image")
    if op == "d" and before is None:
        raise EnvelopeError("IMAGE_MISSING", "d event requires before image")

    ts = source.get("ts_ms")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        ts = payload.get("ts_ms")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        raise EnvelopeError("TIMESTAMP_MISSING", "source.ts_ms is required")
    occurred_at = _epoch_datetime(int(ts) * 1000)

    coordinates = _coordinates(dialect, source, payload)
    schema_name = _require_name(
        source, "db" if dialect == DIALECT_MYSQL else "schema")
    return DecodedEvent(
        kind=KIND_CHANGE, connector=connector,
        change_kind=CHANGE_KINDS[op], schema_name=schema_name,
        table_name=_require_name(source, "table"),
        before=before, after=after,
        available_fields=frozenset({*before_schemas, *after_schemas}),
        after_column_schemas=after_schemas,
        before_column_schemas=before_schemas,
        occurred_at=occurred_at, coordinates=coordinates, op=op)


def _schema_fields(schema: dict, label: str) -> dict[str, dict]:
    """Kafka JsonConverter 扁平字段形态：字段项自身即 schema，名字在
    ``field`` 键（没有嵌套的 entry["schema"]）。"""
    fields = schema.get("fields")
    if not isinstance(fields, list):
        raise EnvelopeError(
            "ENVELOPE_SCHEMA_UNEXPECTED", f"{label} has no fields")
    indexed: dict[str, dict] = {}
    for entry in fields:
        if isinstance(entry, dict) and isinstance(entry.get("field"), str):
            indexed[entry["field"]] = entry
    return indexed


def _decode_image(value: Any, fields: dict[str, dict], label: str):
    if value is None:
        return None, {}
    entry = fields.get(label)
    if not isinstance(entry, dict) or not isinstance(entry.get("fields"), list):
        raise EnvelopeError(
            "ENVELOPE_SCHEMA_UNEXPECTED", f"{label} schema is missing")
    names = set(_schema_fields(entry, f"{label} schema"))
    if not names:
        raise EnvelopeError(
            "ENVELOPE_SCHEMA_UNEXPECTED", f"{label} schema has no fields")
    if not isinstance(value, dict) or value.keys() - names:
        raise EnvelopeError(
            "ENVELOPE_FIELD_UNEXPECTED",
            f"{label} payload does not match its schema")
    decoded = _decode(value, entry)
    column_schemas = {
        entry_["field"]: entry_
        for entry_ in entry["fields"]
        if isinstance(entry_, dict) and isinstance(entry_.get("field"), str)}
    return decoded, column_schemas


def _require_name(source: dict, key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise EnvelopeError(
            "ENVELOPE_SOURCE_INVALID", f"source.{key} is required")
    return value


# ---------------------------------------------------------------------------
# 源身份坐标
# ---------------------------------------------------------------------------

def _binlog_file_key(file: str) -> tuple[str, int]:
    """binlog 文件全序键：文件名按数字后缀比较（"…9" < "…10"）。"""
    match = _BINLOG_SUFFIX.match(file)
    if match is None:
        return (file, -1)
    return (match.group("stem"), int(match.group("digits")))


def _coordinates(dialect: str, source: dict,
                 payload: dict) -> SourceCoordinates:
    if dialect == DIALECT_MYSQL:
        file = _require_name(source, "file")
        pos, row = source.get("pos"), source.get("row")
        if (not isinstance(pos, int) or isinstance(pos, bool) or pos < 0
                or not isinstance(row, int) or isinstance(row, bool) or row < 0):
            raise EnvelopeError(
                "SOURCE_IDENTITY_MISSING",
                "mysql identity requires non-negative pos and row")
        return SourceCoordinates(
            dialect=dialect,
            identity={"file": file, "pos": pos, "row": row},
            order=(*_binlog_file_key(file), pos, row),
            position={"dialect": DIALECT_MYSQL, "file": file,
                      "pos": pos, "row": row})
    lsn, order, txid = _postgres_identity(source, payload)
    # 声明语义（fail closed）：lsn（source 块）取事件上报值参与激活前沿
    # 单调比较 — 低于激活前沿即 stale 拒绝；行 WAL LSN 顺序与提交输出顺
    # 序不同，故绝不以行 LSN 做全局去重，提交时间覆盖不做承诺。
    return SourceCoordinates(
        dialect=DIALECT_POSTGRES,
        identity={"lsn": lsn, "transaction_id": txid, "order": order},
        order=(lsn, order),
        position={"dialect": DIALECT_POSTGRES, "lsn": lsn,
                  "transaction_id": txid, "data_collection_order": order})


def _postgres_identity(source: dict, payload: dict) -> tuple[int, int, str]:
    # 官方形态：lsn 在 source 块；transaction（id/total_order/
    # data_collection_order）在 payload 块（provide.transaction.metadata）。
    transaction = payload.get("transaction")
    txid = transaction.get("id") if isinstance(transaction, dict) else None
    if not isinstance(txid, str) or not txid:
        raise EnvelopeError(
            "SOURCE_IDENTITY_MISSING",
            "postgres identity requires provide.transaction.metadata=true "
            "(payload.transaction.id)")
    lsn = source.get("lsn")
    if not isinstance(lsn, int) or isinstance(lsn, bool) or lsn < 0:
        raise EnvelopeError(
            "SOURCE_IDENTITY_MISSING", "postgres identity requires source.lsn")
    order = transaction.get(
        "data_collection_order", transaction.get("total_order"))
    if not isinstance(order, int) or isinstance(order, bool) or order < 0:
        raise EnvelopeError(
            "SOURCE_IDENTITY_MISSING",
            "postgres identity requires data_collection_order/total_order")
    return lsn, order, txid


def activation_order(position: Any) -> tuple:
    """把 activation_position/last_position 归一到与事件坐标同构的全序键。

    语义：坐标元组严格大于前沿的事件才投递；等于或更早 → stale 拒绝。
    PG 同一 lsn 内多行事件由 data_collection_order 区分，比较键稳定。
    """
    if not isinstance(position, dict):
        raise EnvelopeError(
            "POSITION_INVALID", "position must be an object")
    dialect = position.get("dialect")
    try:
        if dialect == DIALECT_MYSQL:
            return (*_binlog_file_key(str(position["file"])),
                    int(position["pos"]), int(position.get("row", 0)))
        if dialect == DIALECT_POSTGRES:
            return (int(position["lsn"]),
                    int(position.get("data_collection_order", 0)))
    except (KeyError, TypeError, ValueError) as exc:
        raise EnvelopeError(
            "POSITION_INVALID", "position is not a valid coordinate") from exc
    raise EnvelopeError(
        "POSITION_INVALID", f"unsupported position dialect {dialect!r}")


# ---------------------------------------------------------------------------
# 值解码（严格无损）
# ---------------------------------------------------------------------------

_TIME_UNIT = {"io.debezium.time.Time": 1000,
              "io.debezium.time.MicroTime": 1,
              "io.debezium.time.NanoTime": -1}
_TS_UNIT = {"io.debezium.time.Timestamp": 1000,
            "io.debezium.time.MicroTimestamp": 1,
            "io.debezium.time.NanoTimestamp": -1}


def _decode(value: Any, schema: dict) -> Any:
    if value is None:
        return None
    # TOAST/不可用占位优先于一切类型解码（官方以专用逻辑名 + 字面值双形
    # 态出现，且可能落在任何类型的列 schema 上）：宁可 fail closed 也不把
    # 占位当真值写入本体。
    if value == TOAST_PLACEHOLDER:
        return UNAVAILABLE
    name = schema.get("name")
    if isinstance(name, str):
        if name == _PLACEHOLDER_SCHEMA:
            return UNAVAILABLE
        decoded = _decode_named(name, value, schema)
        if decoded is not None:
            return decoded
    stype = schema.get("type")
    if stype == "struct":
        return _decode_struct(value, schema)
    if stype == "array":
        if not isinstance(value, list):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "array payload mismatch")
        items = schema.get("items")
        return [_decode(item, items if isinstance(items, dict) else {})
                for item in value]
    if stype == "map":
        if not isinstance(value, dict):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "map payload mismatch")
        values_schema = schema.get("values")
        return {str(key): _decode(item, values_schema if isinstance(
            values_schema, dict) else {}) for key, item in value.items()}
    if stype == "boolean":
        if not isinstance(value, bool):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "boolean payload mismatch")
        return value
    if stype in {"int8", "int16", "int32", "int64"}:
        if not isinstance(value, int) or isinstance(value, bool):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "integer payload mismatch")
        return value
    if stype in {"float", "double"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "float payload mismatch")
        return float(value)
    if stype == "string":
        if not isinstance(value, str):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "string payload mismatch")
        return value
    if stype == "bytes":
        if not isinstance(value, str):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "bytes payload mismatch")
        return value
    raise EnvelopeError(
        "ENVELOPE_TYPE_UNSUPPORTED", f"unsupported schema type {stype!r}")


def _decode_struct(value: dict, schema: dict) -> dict:
    fields = _schema_fields(schema, "struct schema")
    if value.keys() - set(fields):
        raise EnvelopeError(
            "ENVELOPE_FIELD_UNEXPECTED", "payload carries unknown struct fields")
    return {key: _decode(raw, fields[key]) for key, raw in value.items()}


def _decode_named(name: str, value: Any, schema: dict) -> Any:
    if name == "io.debezium.data.Decimal":
        return _decode_decimal(value, _scale_of(schema), schema)
    if name == "io.debezium.data.VariableScaleDecimal":
        if not isinstance(value, dict):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "decimal payload mismatch")
        inner = value.get("value")
        if (not isinstance(value.get("scale"), int)
                or not isinstance(inner, str)):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "decimal payload mismatch")
        return _decode_decimal(inner, value["scale"], {"type": "string"})
    if name == "io.debezium.time.Date":
        return _date_of_day(value).isoformat()
    if name in _TIME_UNIT:
        return _time_of_day(value, name).isoformat()
    if name in _TS_UNIT:
        return _timestamp_of_epoch(value, name).isoformat()
    if name in {"io.debezium.time.ZonedTime", "io.debezium.time.ZonedTimestamp"}:
        if not isinstance(value, str):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "zoned payload mismatch")
        _require_iso(value)
        return value
    if name in {"io.debezium.time.Year", "io.debezium.data.Enum",
                "io.debezium.data.EnumSet"}:
        return value
    if name == "io.debezium.data.Json":
        if not isinstance(value, str):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "json payload mismatch")
        try:
            return json.loads(value)
        except ValueError as exc:
            raise EnvelopeError(
                "JSON_VALUE_INVALID", "json payload is not valid JSON") from exc
    return None


def _scale_of(schema: dict) -> int:
    parameters = schema.get("parameters")
    raw = parameters.get("scale") if isinstance(parameters, dict) else None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise EnvelopeError(
            "ENVELOPE_SCHEMA_UNEXPECTED",
            "decimal schema requires a scale parameter") from exc


def _decode_decimal(value: Any, scale: int, schema: dict) -> Decimal:
    """无损 Decimal：string 模式（运行时约定）解析数值串，precise 的
    base64 字节按 scale 还原；两者都经 schema 逻辑名 + scale 声明。"""
    if schema.get("type") == "bytes":
        if not isinstance(value, str):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "decimal payload mismatch")
        try:
            raw = base64.b64decode(value, validate=True)
            unscaled = int.from_bytes(raw, "big", signed=True)
        except (binascii.Error, ValueError) as exc:
            raise EnvelopeError(
                "ENVELOPE_VALUE_INVALID", "decimal payload is not base64") from exc
        decimal = Decimal(unscaled) if scale == 0 else (
            Decimal(unscaled) / (Decimal(10) ** scale))
    else:
        if not isinstance(value, str):
            raise EnvelopeError("ENVELOPE_VALUE_INVALID", "decimal payload mismatch")
        try:
            decimal = Decimal(value)
        except InvalidOperation as exc:
            raise EnvelopeError(
                "ENVELOPE_VALUE_INVALID",
                "decimal payload is not a number string") from exc
    if not decimal.is_finite():
        raise EnvelopeError("ENVELOPE_VALUE_INVALID", "decimal is not finite")
    return decimal


def _int_of(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise EnvelopeError("ENVELOPE_VALUE_INVALID", f"{label} payload mismatch")
    return value


def _date_of_day(value: Any) -> date:
    days = _int_of(value, "date")
    try:
        return EPOCH_DATE + timedelta(days=days)
    except (OverflowError, ValueError) as exc:
        raise EnvelopeError(
            "TEMPORAL_VALUE_INVALID", "date value out of range") from exc


def _time_of_day(value: Any, name: str) -> time:
    unit = _TIME_UNIT[name]
    raw = _int_of(value, "time")
    if raw < 0:
        raise EnvelopeError("TEMPORAL_VALUE_INVALID", "negative time of day")
    if unit == -1:
        if raw % 1000:
            raise EnvelopeError(
                "TIME_PRECISION_LOSS",
                "nanosecond time cannot be represented losslessly")
        raw, unit = raw // 1000, 1
    micros = raw * unit
    if micros >= _DAY_MICROS:
        raise EnvelopeError("TEMPORAL_VALUE_INVALID", "time of day out of range")
    hour, rest = divmod(micros, 3_600_000_000)
    minute, rest = divmod(rest, 60_000_000)
    second, microsecond = divmod(rest, 1_000_000)
    return time(hour=hour, minute=minute, second=second, microsecond=microsecond)


def _timestamp_of_epoch(value: Any, name: str) -> datetime:
    factor = _TS_UNIT[name]
    raw = _int_of(value, "timestamp")
    if factor == -1:
        if raw % 1000:
            raise EnvelopeError(
                "TIME_PRECISION_LOSS",
                "nanosecond timestamp cannot be represented losslessly")
        raw, factor = raw // 1000, 1
    return _epoch_datetime(raw * factor)


def _epoch_datetime(micros: int) -> datetime:
    try:
        return EPOCH + timedelta(microseconds=micros)
    except (OverflowError, ValueError) as exc:
        raise EnvelopeError(
            "TEMPORAL_VALUE_INVALID", "timestamp value out of range") from exc


def _require_iso(value: str) -> None:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EnvelopeError(
            "TEMPORAL_VALUE_INVALID", "zoned temporal value is not ISO-8601") from exc


# ---------------------------------------------------------------------------
# 冻结契约校验（列 schema 归一 → 订阅冻结类型契约比对）
# ---------------------------------------------------------------------------

_NAMED_KINDS = {
    "io.debezium.data.Decimal": "decimal",
    "io.debezium.data.VariableScaleDecimal": "decimal",
    "io.debezium.time.Date": "date",
    "io.debezium.time.Time": "time",
    "io.debezium.time.MicroTime": "time",
    "io.debezium.time.NanoTime": "time",
    "io.debezium.time.ZonedTime": "time",
    "io.debezium.time.Timestamp": "timestamp",
    "io.debezium.time.MicroTimestamp": "timestamp",
    "io.debezium.time.NanoTimestamp": "timestamp",
    "io.debezium.time.ZonedTimestamp": "timestamp",
    "io.debezium.time.Year": "integer",
    "io.debezium.data.Enum": "string",
    "io.debezium.data.EnumSet": "string",
    "io.debezium.data.Json": "json",
}
_PRIMITIVE_KINDS = {
    "boolean": "boolean",
    "int8": "integer", "int16": "integer", "int32": "integer", "int64": "integer",
    "float": "float", "double": "float",
    "string": "string", "bytes": "binary",
}


def column_contract(column_schema: dict) -> dict:
    """把一列的信封 schema 归一为冻结契约可比对的形式。

    返回 ``{"type": kind, "nullable": bool, "scale": int | None}``；
    无法归一的类型抛 ``EnvelopeError``（fail closed，绝不静默放行）。
    """
    if not isinstance(column_schema, dict):
        raise EnvelopeError(
            "ENVELOPE_SCHEMA_UNEXPECTED", "column schema is missing")
    name = column_schema.get("name")
    kind = None
    if isinstance(name, str) and name in _NAMED_KINDS:
        kind = _NAMED_KINDS[name]
    elif column_schema.get("type") in _PRIMITIVE_KINDS:
        kind = _PRIMITIVE_KINDS[column_schema["type"]]
    else:
        raise EnvelopeError(
            "ENVELOPE_TYPE_UNSUPPORTED",
            f"column type {column_schema.get('name') or column_schema.get('type')!r} "
            "cannot be validated against the frozen contract")
    scale = None
    if kind == "decimal":
        parameters = column_schema.get("parameters")
        raw = parameters.get("scale") if isinstance(parameters, dict) else None
        if raw is not None:
            try:
                scale = int(raw)
            except (TypeError, ValueError) as exc:
                raise EnvelopeError(
                    "ENVELOPE_SCHEMA_UNEXPECTED",
                    "decimal schema scale is malformed") from exc
    return {
        "type": kind,
        "nullable": bool(column_schema.get("optional", False)),
        "scale": scale,
    }
