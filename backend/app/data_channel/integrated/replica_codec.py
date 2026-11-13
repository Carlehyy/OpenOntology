"""Lossless Debezium images for current replicas, including snapshot READs.

The Sentinel envelope contract stays unchanged. This codec never runs images
through ``cdc_envelope.decode_envelope``: that path eagerly decodes values and
would float-round JSON text and mis-handle the real Kafka Connect decimal
name. Instead, raw payload images are converted per manifest column against
the envelope's own column schemas:

- JSON text survives verbatim (validated, never re-parsed through floats);
  SQL NULL (``None``) and JSON ``null`` text stay distinct values.
- Real Debezium decimals: ``org.apache.kafka.connect.data.Decimal`` (precise
  base64, scale parameter), the io string mode, and unbounded-source
  ``io.debezium.data.VariableScaleDecimal`` structs all decode to exact
  ``$decimal`` wire scalars; manifest scale/precision/NULL semantics are
  enforced here, fail-closed.
- Temporal values decode through the existing cdc codec to ISO; zoned values
  keep their original offset. Bytes base64 → ``$binary``; wide ints →
  ``$integer``; floats reject bools and non-finite values.
- TOAST/UnavailableValue placeholders (literal or
  ``io.debezium.data.special.UnavailableValuePlaceholder`` schema name) map to
  the shared ``UNAVAILABLE`` marker when ``allow_unavailable`` — the ordered
  apply layer resolves them from its current local row — and are hard errors
  otherwise.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.data_channel.integrated import cdc_envelope
from app.data_channel.integrated.contracts import wire_value

UNAVAILABLE = cdc_envelope.UNAVAILABLE
TOAST_PLACEHOLDER = cdc_envelope.TOAST_PLACEHOLDER
_PLACEHOLDER_SCHEMA = cdc_envelope._PLACEHOLDER_SCHEMA
_KAFKA_DECIMAL = "org.apache.kafka.connect.data.Decimal"
_DBX_DECIMAL = "io.debezium.data.Decimal"
_VARIABLE_SCALE_DECIMAL = "io.debezium.data.VariableScaleDecimal"
_UNSIGNED_DIGITS = re.compile(r"^[0-9]+$")

_DIALECTS = {"mysql": "mysql", "postgresql": "postgresql"}


class ReplicaCodecError(RuntimeError):
    """确定性拒绝（错误码封闭集合）。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourceChange:
    """c/u/d 事件头：仅结构事实，不含任何解码后的行值。"""

    op: str
    dialect: str
    schema_name: str
    table_name: str
    before: dict | None
    after: dict | None


# ---------------------------------------------------------------------------
# 事件头（不做有损像解码；initial snapshot READ r 属批量 COPY 通道，不走本 codec）
# ---------------------------------------------------------------------------

def decode_change_header(envelope) -> SourceChange:
    if not isinstance(envelope, dict):
        raise ReplicaCodecError("REPLICA_ENVELOPE_INVALID")
    schema = envelope.get("schema")
    payload = envelope.get("payload")
    if not isinstance(schema, dict) or not isinstance(payload, dict):
        raise ReplicaCodecError("REPLICA_ENVELOPE_INVALID")
    op = payload.get("op")
    if op == "r":
        raise ReplicaCodecError("REPLICA_SNAPSHOT_OP")
    if op not in {"c", "u", "d"} or not isinstance(op, str):
        raise ReplicaCodecError("REPLICA_OP_UNSUPPORTED")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ReplicaCodecError("REPLICA_SOURCE_INVALID")
    connector = source.get("connector")
    dialect = _DIALECTS.get(connector) if isinstance(connector, str) else None
    if dialect is None:
        raise ReplicaCodecError("REPLICA_DIALECT_UNSUPPORTED")
    schema_name = source.get("db" if dialect == "mysql" else "schema")
    table_name = source.get("table")
    if not isinstance(schema_name, str) or not schema_name \
            or not isinstance(table_name, str) or not table_name:
        raise ReplicaCodecError("REPLICA_SOURCE_INVALID")
    before, after = payload.get("before"), payload.get("after")
    if op in {"c", "u"} and not isinstance(after, dict):
        raise ReplicaCodecError("REPLICA_IMAGE_MISSING")
    if op == "d" and not isinstance(before, dict):
        raise ReplicaCodecError("REPLICA_IMAGE_MISSING")
    return SourceChange(op=op, dialect=dialect, schema_name=schema_name,
                        table_name=table_name, before=before, after=after)


# ---------------------------------------------------------------------------
# 像解码（raw payload image → store wire；逐列合约校验 + 精确转换）
# ---------------------------------------------------------------------------

def _image_parts(envelope, image: str):
    if image not in {"before", "after"}:
        raise ReplicaCodecError("REPLICA_ENVELOPE_INVALID")
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if not isinstance(payload, dict):
        raise ReplicaCodecError("REPLICA_ENVELOPE_INVALID")
    schema = envelope.get("schema")
    if not isinstance(schema, dict) or not isinstance(schema.get("fields"), list):
        raise ReplicaCodecError("REPLICA_ENVELOPE_INVALID")
    entries = {entry.get("field"): entry for entry in schema["fields"]
               if isinstance(entry, dict) and isinstance(entry.get("field"), str)}
    image_entry = entries.get(image)
    if not isinstance(image_entry, dict) \
            or not isinstance(image_entry.get("fields"), list):
        raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT")
    column_schemas = {entry.get("field"): entry for entry in image_entry["fields"]
                      if isinstance(entry, dict) and isinstance(entry.get("field"), str)}
    return payload.get(image), column_schemas


def _alias_decimal(entry: dict) -> dict:
    """Kafka Connect 内建 decimal 名与 io 名参数语义相同；仅别名，绝不做数值转换。"""
    if entry.get("name") == _KAFKA_DECIMAL:
        aliased = dict(entry)
        aliased["name"] = _DBX_DECIMAL
        return aliased
    return entry


def _check_contract(column: dict, entry: dict) -> None:
    try:
        contract = cdc_envelope.column_contract(_alias_decimal(entry))
    except cdc_envelope.EnvelopeError:
        raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT") from None
    if contract["type"] != column["type"]:
        # MySQL BIGINT UNSIGNED under bigint.unsigned.handling.mode=precise
        # is a Kafka Connect Decimal (precision 20, scale 0) instead of int64.
        # Accept exactly that lossless form for unsigned integer columns.
        unsigned_decimal = (column["type"] == "integer"
                            and bool(column.get("unsigned"))
                            and contract["type"] == "decimal"
                            and contract["scale"] == 0)
        if not unsigned_decimal:
            raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT")
    if column["type"] == "decimal" and column.get("scale") is not None \
            and contract["scale"] is not None \
            and contract["scale"] != column["scale"]:
        raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT")
    if column["type"] == "timestamp" \
            and (entry.get("name") == "io.debezium.time.ZonedTimestamp") \
            != bool(column.get("timezone")):
        raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT")
    if column["type"] == "time" \
            and entry.get("name") in {"io.debezium.time.ZonedTime",
                                      "io.debezium.time.ZonedTimestamp"}:
        raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT")


def _decimal_digits(value: Decimal, precision, scale) -> None:
    """镜像 store 的位数检查：scale 溢出/整数位溢出拒绝，绝不四舍五入。"""
    if precision is None and scale is None:
        return
    normalized = value.as_tuple()
    digits, exponent = list(normalized.digits), normalized.exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    fractional = max(-exponent, 0) if value else 0
    integer = max(len(digits) + exponent, 0) if value else 0
    if scale is not None and fractional > scale:
        raise ReplicaCodecError("REPLICA_DECIMAL_INVALID")
    if precision is not None:
        if scale is not None and integer > precision - scale:
            raise ReplicaCodecError("REPLICA_DECIMAL_INVALID")
        if scale is None and integer + fractional > precision:
            raise ReplicaCodecError("REPLICA_DECIMAL_INVALID")


def _exact_decimal(number: int, scale) -> Decimal:
    """Unscaled big integer + non-negative scale → exact Decimal.

    The coefficient/exponent tuple constructor bypasses the process decimal
    context, so NUMERIC(65,30)-class values stay exact. ``scale`` must be a real
    int: a bool/str would silently change the value.
    """
    if type(scale) is not int or scale < 0:
        raise ValueError()
    return Decimal((int(number < 0), tuple(map(int, str(abs(number)))), -scale))


def _decimal(value, field: dict, column: dict) -> dict:
    try:
        if field.get("name") == _VARIABLE_SCALE_DECIMAL:
            # Unbounded source NUMERIC: value is a struct {scale, value(bytes)}.
            if not isinstance(value, dict):
                raise ValueError()
            encoded, scale = value.get("value"), value.get("scale")
            if not isinstance(encoded, str):
                raise ValueError()
            number = int.from_bytes(
                base64.b64decode(encoded, validate=True), "big", signed=True)
            result = _exact_decimal(number, scale)
        elif field.get("type") == "bytes":
            raw = base64.b64decode(value, validate=True)
            number = int.from_bytes(raw, "big", signed=True)
            result = _exact_decimal(
                number, int((field.get("parameters") or {})["scale"]))
        else:
            if not isinstance(value, str):
                raise ValueError()
            result = Decimal(value)
        if not result.is_finite():
            raise ValueError()
    except (ValueError, TypeError, KeyError, InvalidOperation, binascii.Error):
        raise ReplicaCodecError("REPLICA_DECIMAL_INVALID") from None
    _decimal_digits(result, column.get("precision"), column.get("scale"))
    return {"$decimal": str(result)}


def _temporal(value, field: dict, column: dict):
    try:
        decoded = cdc_envelope._decode(value, field)
    except cdc_envelope.EnvelopeError:
        raise ReplicaCodecError("REPLICA_TEMPORAL_INVALID") from None
    if isinstance(decoded, str):
        # Zoned* 原串保留原始偏移；epoch 型 Timestamp 的 ISO 串以 UTC 编码
        # “墙上时间”——naive manifest 列的 wire 必须无偏移（store 严格拒绝，
        # 合约层已保证 naive 列不可能出现 Zoned 名）。
        if column["type"] == "timestamp" and not column.get("timezone"):
            parsed = dt.datetime.fromisoformat(decoded)
            if parsed.tzinfo is not None:
                return parsed.replace(tzinfo=None).isoformat()
        return decoded
    if isinstance(decoded, dt.datetime):
        if not column.get("timezone") and decoded.tzinfo is not None:
            decoded = decoded.replace(tzinfo=None)
        return decoded.isoformat()
    if isinstance(decoded, (dt.date, dt.time)):
        return decoded.isoformat()
    raise ReplicaCodecError("REPLICA_TEMPORAL_INVALID")


def _convert(column: dict, value, field: dict):
    kind = column["type"]
    name = column["name"]
    if value is None:
        return None
    if value == TOAST_PLACEHOLDER or field.get("name") == _PLACEHOLDER_SCHEMA:
        raise ReplicaCodecError("REPLICA_VALUE_UNAVAILABLE")
    if kind == "json":
        if not isinstance(value, str):
            raise ReplicaCodecError("REPLICA_JSON_INVALID")
        try:
            # 仅校验结构良好性；原文逐字保留，绝不经浮点。
            json.loads(value, parse_float=Decimal,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError):
            raise ReplicaCodecError("REPLICA_JSON_INVALID") from None
        return value
    if kind == "decimal":
        return _decimal(value, field, column)
    if kind == "binary":
        if not isinstance(value, str):
            raise ReplicaCodecError("REPLICA_BINARY_INVALID")
        try:
            base64.b64decode(value, validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise ReplicaCodecError("REPLICA_BINARY_INVALID") from None
        return {"$binary": value}
    if kind in {"timestamp", "time", "date"}:
        return _temporal(value, field, column)
    if kind == "float":
        if isinstance(value, Decimal):
            # The HTTP boundary parses every fractional JSON token with
            # parse_float=Decimal. A finite Decimal that came from a source
            # float4/float8 is converted to the native float representation;
            # bool/NaN/Infinity/overflow still fail closed.
            if not value.is_finite():
                raise ReplicaCodecError("REPLICA_FLOAT_INVALID")
            converted = float(value)
            if not math.isfinite(converted):
                raise ReplicaCodecError("REPLICA_FLOAT_INVALID")
            return converted
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            raise ReplicaCodecError("REPLICA_FLOAT_INVALID")
        return float(value)
    if kind == "integer":
        if column.get("unsigned") and field.get("name") in {
                _KAFKA_DECIMAL, _DBX_DECIMAL, _VARIABLE_SCALE_DECIMAL}:
            # precise mode BIGINT UNSIGNED: Decimal(scale 0) → exact int.
            number = Decimal(_decimal(value, field, column)["$decimal"])
            if not number.is_finite() or number < 0 \
                    or number != number.to_integral_value():
                raise ReplicaCodecError("REPLICA_INTEGER_INVALID")
            return wire_value(int(number))
        if column.get("unsigned") and isinstance(value, str):
            if not _UNSIGNED_DIGITS.fullmatch(value):
                raise ReplicaCodecError("REPLICA_INTEGER_INVALID")
            return wire_value(int(value))
        if type(value) is not int:
            raise ReplicaCodecError("REPLICA_INTEGER_INVALID")
        if column.get("unsigned") and value < 0:
            raise ReplicaCodecError("REPLICA_INTEGER_INVALID")
        return wire_value(value)
    if kind == "boolean":
        if type(value) is not bool:
            raise ReplicaCodecError("REPLICA_BOOLEAN_INVALID")
        return value
    if kind == "string":
        if not isinstance(value, str):
            raise ReplicaCodecError("REPLICA_STRING_INVALID")
        return value
    raise ReplicaCodecError("REPLICA_TYPE_UNSUPPORTED")


def decode_source_image(envelope: dict, manifest: dict, *, image="after",
                        allow_unavailable=False, columns=None) -> dict | None:
    """一个 before/after 像 → 源列名键的 store wire 值。

    ``columns=None`` 严格全像（键集合必须恰为 manifest 列集合）；给定列名
    子集时像键必须覆盖子集且不超出 manifest（PG 默认 replica identity 的
    delete/u before 像仅含主键）。像缺失（None）返回 None。占位列在
    ``allow_unavailable=True`` 时映射为 ``UNAVAILABLE`` 标记（由有序 apply
    从当前本地行补值），否则确定性拒绝。
    """
    raw, column_schemas = _image_parts(envelope, image)
    if raw is None:
        return None
    manifest_columns = manifest.get("columns") if isinstance(manifest, dict) else None
    if not isinstance(manifest_columns, list) or not manifest_columns:
        raise ReplicaCodecError("REPLICA_MANIFEST_INVALID")
    by_name = {}
    for column in manifest_columns:
        if isinstance(column, dict) and isinstance(column.get("name"), str):
            by_name[column["name"]] = column
    requested = [column["name"] for column in manifest_columns] \
        if columns is None else list(columns or [])
    if not requested or any(name not in by_name for name in requested):
        raise ReplicaCodecError("REPLICA_MANIFEST_INVALID")
    if not isinstance(raw, dict):
        raise ReplicaCodecError("REPLICA_ENVELOPE_INVALID")
    if columns is None:
        if set(raw) != set(by_name):
            raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT")
    elif not set(requested) <= set(raw) <= set(by_name):
        raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT")
    for name in requested:
        entry = column_schemas.get(name)
        if not isinstance(entry, dict):
            raise ReplicaCodecError("REPLICA_SCHEMA_DRIFT")
        _check_contract(by_name[name], entry)
    result = {}
    for name in requested:
        column, field = by_name[name], column_schemas[name]
        value = raw[name]
        if value == TOAST_PLACEHOLDER or field.get("name") == _PLACEHOLDER_SCHEMA:
            if not allow_unavailable:
                raise ReplicaCodecError("REPLICA_VALUE_UNAVAILABLE")
            result[name] = UNAVAILABLE
            continue
        result[name] = _convert(column, value, field)
    return result
