"""Transport-independent integrated-source contracts and lossless value codec."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import hashlib
import json
import math
import uuid
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

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


class RegisterAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    connection_id: str
    schema_name: str = Field(min_length=1, max_length=128)
    table_name: str = Field(min_length=1, max_length=128)
    writable: bool = False


class RowMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: dict[str, Any]
    values: dict[str, Any] = Field(default_factory=dict)
    expected_row_token: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)


def fail(status: int, code: str, message: str) -> None:
    raise HTTPException(status, {"code": code, "message": message})


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


def row_token(row: dict, schema_fingerprint: str) -> str:
    return digest({"schema": schema_fingerprint, "row": row})
