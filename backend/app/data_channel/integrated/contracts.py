"""Transport-independent integrated-source contracts and lossless value codec."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import hashlib
import json
import math
import struct
import uuid
from typing import Any, Iterable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

# Official release pin: annotated tag v0.6.53 -> commit 0137e0c.
# The requested historical pin c6fb06c is that commit plus one
# JDBC-plugin-version bump; no official image was built for it.
DBX_COMMIT = "0137e0cbc8b663da5a17ec424c6d47e4e5570ead"
DBX_VERSION = "0.6.53"


from app.shared.value_types import JsonText


def serialize_source_json(value: Any) -> str:
    if isinstance(value, JsonText):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class FieldConfigEntry(BaseModel):
    """One logical field: a physical source column addressed by its alias.

    Only the logical name and display name are configurable; type, nullability
    and primary-key membership are always physical truth.
    """
    model_config = ConfigDict(extra="forbid")
    source_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str | None = Field(default=None, max_length=200)


class SourceConnectionConfig(BaseModel):
    """Field-based MySQL/PostgreSQL source configuration (never echoed back)."""
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    database: str = Field(default="", max_length=128)
    user: str = Field(min_length=1, max_length=128)
    password: str = Field(default="", max_length=512)
    dbx_host: str | None = Field(default=None, max_length=253)
    dbx_port: int | None = Field(default=None, ge=1, le=65535)


class SourceConnection(BaseModel):
    """An inline (not yet persisted) MySQL/PostgreSQL source definition."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["mysql", "postgres"]
    config: SourceConnectionConfig

    @model_validator(mode="after")
    def _kind_rules(self):
        # PostgreSQL has no single default database for discovery: both the
        # native lane and dbx require an explicit database to reflect against.
        if self.kind == "postgres" and not self.config.database.strip():
            raise ValueError("PostgreSQL sources require an explicit database")
        has_host, has_port = self.config.dbx_host is not None, self.config.dbx_port is not None
        if has_host != has_port:
            raise ValueError("dbx_host and dbx_port must be supplied together")
        return self


class SourceReference(BaseModel):
    """Exactly one reference: an existing connection or an inline definition."""
    model_config = ConfigDict(extra="forbid")
    connection_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_connection: SourceConnection | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.connection_id is None) == (self.source_connection is None):
            raise ValueError("Provide exactly one of connection_id or source_connection")
        return self


class SourceSchemaRequest(SourceReference):
    """Physical schema discovery for one table of a referenced source."""
    model_config = ConfigDict(extra="forbid")
    schema_name: str = Field(min_length=1, max_length=128)
    table_name: str = Field(min_length=1, max_length=128)


class RegisterAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    # Exactly one of an existing connection or an inline source definition.
    connection_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_connection: SourceConnection | None = None
    schema_name: str = Field(min_length=1, max_length=128)
    table_name: str = Field(min_length=1, max_length=128)
    writable: bool = False
    # Absent field_config keeps the original full-schema identity.
    field_config: list[FieldConfigEntry] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self):
        if (self.connection_id is None) == (self.source_connection is None):
            raise ValueError("Provide exactly one of connection_id or source_connection")
        return self


class RowMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: dict[str, Any]
    values: dict[str, Any] = Field(default_factory=dict)
    expected_row_token: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)


def fail(status: int, code: str, message: str, **extra: Any) -> None:
    # extra keys (e.g. the budget dimension on a timeout refusal) merge into
    # the machine-readable detail; existing callers are unchanged.
    raise HTTPException(status, {"code": code, "message": message, **extra})


def normalize_source_value(value: Any) -> Any:
    # PyMySQL represents TIME as timedelta, including durations that cannot be
    # represented as SQL wall-clock TIME. Never wrap/truncate a negative or >=24h
    # duration into a different value.
    if isinstance(value, dt.timedelta):
        micros = (value.days * 86400 + value.seconds) * 1000000 + value.microseconds
        if not 0 <= micros < 86400 * 1000000:
            fail(422, "UNSUPPORTED_TIME_DURATION", "Source TIME duration is outside the supported wall-clock range")
        seconds, microsecond = divmod(micros, 1000000)
        hour, seconds = divmod(seconds, 3600)
        minute, second = divmod(seconds, 60)
        return dt.time(hour, minute, second, microsecond)
    return value


def wire_value(value: Any) -> Any:
    """Never send bigint/decimal through a Javascript floating-point number.

    Ordinary integers remain numbers for usability. Wide integers, DECIMAL,
    binary and non-finite floats are tagged so request decoding is unambiguous.
    JSON objects cannot accidentally impersonate a tagged scalar: decoding is
    performed against the reflected physical column type, not on arbitrary JSON.
    """
    value = normalize_source_value(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return {"$integer": str(value)} if abs(value) > 2**53 - 1 else value
    if isinstance(value, decimal.Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$binary": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        fail(422, "NONFINITE_SOURCE_VALUE", "Non-finite source values are unsupported")
    if isinstance(value, dict):
        return {str(k): wire_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_value(v) for v in value]
    return value


def digest(value: Any) -> str:
    raw = json.dumps(wire_value(value), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class _JsonObject:
    """Parsed JSON object retaining duplicate keys for exact CAS hashing."""

    __slots__ = ("pairs",)

    def __init__(self, pairs):
        self.pairs = pairs


def _reject_json_constant(_value):
    raise ValueError()


def _canonical_json_number(value) -> str:
    # Decimal(str(float)) is only used for Python-native dict/list fixtures;
    # production JSON text always parses through parse_float=Decimal, so no
    # binary float ever enters this path.
    number = (value if isinstance(value, decimal.Decimal)
              else decimal.Decimal(str(value)) if isinstance(value, float)
              else decimal.Decimal(value))
    if not number.is_finite():
        raise ValueError()
    # str(Decimal) preserves every significant digit, trailing zeroes and the
    # source's numeric scale (so JSON 1 and 1.0 stay distinct). It never rounds
    # or routes a digit through binary float; whitespace and key order are the
    # only representations canonicalization removes.
    return str(number)


def _canonical_json_node(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, _JsonObject):
        pairs = value.pairs
    elif isinstance(value, dict):
        pairs = list(value.items())
    else:
        pairs = None
    if pairs is not None:
        members = [
            (json.dumps(str(key), ensure_ascii=False), _canonical_json_node(member))
            for key, member in pairs
        ]
        # Stable sort by key only: object key order is irrelevant, but the
        # relative order of duplicate keys is source-visible and must remain
        # part of the token ({"a":1,"a":2} != {"a":2,"a":1}).
        members.sort(key=lambda pair: pair[0])
        return "{" + ",".join(f"{key}:{member}" for key, member in members) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json_node(member) for member in value) + "]"
    if isinstance(value, (int, float, decimal.Decimal)):
        return _canonical_json_number(value)
    raise ValueError()


def canonical_json_text(value: Any) -> str:
    """Canonical JSON text used only for typed JSON columns in row tokens.

    Whitespace and object key order are removed; all numbers are parsed as
    Decimal and never through binary float, so large integers and high-precision
    decimals keep every digit. JSON literal ``null`` is the text ``"null"`` while
    SQL NULL remains ``None``. Ordinary string columns are never passed here.
    """
    if isinstance(value, JsonText):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    elif isinstance(value, (bytes, bytearray, memoryview)):
        try:
            raw = bytes(value).decode("utf-8", "strict")
        except UnicodeDecodeError:
            fail(422, "INVALID_COLUMN_VALUE", "Invalid UTF-8 JSON value")
    elif isinstance(value, (dict, list, tuple, _JsonObject)):
        try:
            return _canonical_json_node(value)
        except (ValueError, TypeError):
            fail(422, "INVALID_COLUMN_VALUE", "Invalid JSON value for row token")
    else:
        fail(422, "INVALID_COLUMN_VALUE", "Invalid JSON value for row token")
    try:
        parsed = json.loads(
            raw, parse_int=decimal.Decimal, parse_float=decimal.Decimal,
            parse_constant=_reject_json_constant, object_pairs_hook=_JsonObject)
        return _canonical_json_node(parsed)
    except (ValueError, TypeError):
        fail(422, "INVALID_COLUMN_VALUE", "Invalid JSON value for row token")


def _canonical_float32(value: Any) -> float:
    """Exact IEEE-754 single-precision value, widened losslessly to a Python float.

    This is the source column's real precision, not a 6-digit display rounding:
    two different float32 values stay different. It makes a Debezium short
    decimal (e.g. 1.234567) hash identically to the source's exact float32 bits
    (1.2345670461654663) without lowering CAS precision.
    """
    if isinstance(value, bool):
        fail(422, "INVALID_COLUMN_VALUE", "Invalid single-precision value")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        fail(422, "INVALID_COLUMN_VALUE", "Invalid single-precision value")
    if not math.isfinite(number):
        fail(422, "INVALID_COLUMN_VALUE", "Non-finite single-precision value")
    try:
        widened = struct.unpack("<f", struct.pack("<f", number))[0]
    except (OverflowError, struct.error):
        fail(422, "INVALID_COLUMN_VALUE", "Single-precision value out of range")
    if not math.isfinite(widened):
        fail(422, "INVALID_COLUMN_VALUE", "Single-precision value out of range")
    return widened


def _canonical_token_row(row: dict, json_columns: Iterable[str],
                         float32_columns: Iterable[str]) -> dict:
    json_set = set(json_columns)
    float32_set = set(float32_columns)
    result = {}
    for name, value in row.items():
        if value is None:
            # SQL NULL is distinct from JSON literal null (which canonicalizes
            # to the text "null" below).
            result[name] = None
        elif name in json_set:
            result[name] = canonical_json_text(value)
        elif name in float32_set:
            result[name] = _canonical_float32(value)
        else:
            # Text/varchar values that merely look like JSON, DECIMAL, big
            # integers and all other columns keep their exact wire value.
            result[name] = value
    return result


def row_token(row: dict, schema_fingerprint: str, *,
              json_columns: Iterable[str] = (),
              float32_columns: Iterable[str] = ()) -> str:
    """Hash the full physical row in the source read/token namespace.

    ``json_columns`` and ``float32_columns`` must come from typed source/replica
    metadata (never from value sniffing). Changing this canonicalization
    invalidates previously issued tokens: CAS rejects them with an explicit
    conflict; there is no dual-token fallback.
    """
    return digest({"schema": schema_fingerprint,
                   "row": _canonical_token_row(
                       row, json_columns, float32_columns)})
