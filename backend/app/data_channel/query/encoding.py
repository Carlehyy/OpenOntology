"""Value encoding and streaming byte measurement shared by all execution paths."""
from __future__ import annotations

import base64
import io
import json
import math
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any

from app.data_channel.integrated.contracts import JsonText

from .contracts import ColumnSpec, QueryError, canonical_json


def encode_value(value: Any, spec: ColumnSpec) -> Any:
    if value is None: return None
    if spec.type == "json":
        # Preserve database JSON text, including arbitrary-precision JSON numbers.
        # SQL NULL is None, whereas JSON literal null is the text "null".
        return str(value) if isinstance(value, JsonText) else canonical_json(value)
    if spec.type == "integer":
        if isinstance(value, bool) or (isinstance(value, Decimal) and value != value.to_integral_value()):
            raise QueryError(422, "invalid_typed_value", f"Non-integer value in {spec.name}")
        return str(int(value))
    if spec.type == "decimal":
        if isinstance(value, float):
            raise QueryError(422, "lossy_numeric_value", "Decimal results must not pass through float")
        number = Decimal(value)
        if not number.is_finite():
            raise QueryError(422, "invalid_typed_value", "Non-finite decimal result")
        return format(number, "f")
    if spec.type == "float":
        if not math.isfinite(float(value)):
            raise QueryError(422, "invalid_typed_value", "Non-finite float result")
        return float(value)
    if spec.type in {"date", "timestamp", "time"}:
        return value.isoformat() if isinstance(value, (datetime, date, dt_time)) else str(value)
    if spec.type == "binary": return base64.b64encode(bytes(value)).decode("ascii")
    if spec.type == "string":
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="strict")
        return str(value)
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _json_parts(value: Any):
    """JSON fragments without allocating an encoded copy of a giant string."""
    if isinstance(value, str):
        yield '"'
        for start in range(0, len(value), 16384):
            yield _json(value[start:start + 16384])[1:-1]
        yield '"'
    elif isinstance(value, dict):
        yield "{"
        # JSON object order is not semantic; do not allocate a sorted key list
        # for a driver-decoded large object just to enforce the byte budget.
        for index, (key, item) in enumerate(value.items()):
            if index: yield ","
            yield from _json_parts(key)
            yield ":"
            yield from _json_parts(item)
        yield "}"
    elif isinstance(value, (list, tuple)):
        yield "["
        for index, item in enumerate(value):
            if index: yield ","
            yield from _json_parts(item)
        yield "]"
    else:
        yield _json(value)


def encoded_size(value: Any) -> int:
    """Exact encoded JSON byte length, measured in bounded chunks."""
    size = 0
    for part in _json_parts(value):
        size += len(part.encode("utf-8"))
    return size


def _copy_parts(value: Any, spec: ColumnSpec):
    if value is None:
        yield r"\N"
        return
    if spec.type == "json":
        # Native JSON values already carry their exact text; do not quote the
        # document as a JSON string or parse/re-encode its decimal numbers.
        parts = (value[start:start + 16384] for start in range(0, len(value), 16384)) if isinstance(value, JsonText) else _json_parts(value)
    elif spec.type == "binary":
        def binary_parts():
            yield "\\x"
            view = memoryview(value)
            for start in range(0, len(view), 16384):
                yield view[start:start + 16384].hex()
        parts = binary_parts()
    else:
        if isinstance(value, (datetime, date, dt_time)): value = value.isoformat()
        elif isinstance(value, bool): value = "true" if value else "false"
        elif isinstance(value, Decimal) and not value.is_finite():
            raise QueryError(422, "invalid_typed_value", "Non-finite decimal input")
        elif isinstance(value, float) and not math.isfinite(value):
            raise QueryError(422, "invalid_typed_value", "Non-finite float input")
        raw_text = str(value)
        parts = (raw_text[start:start + 16384] for start in range(0, len(raw_text), 16384))
    for text in parts:
        if "\x00" in text:
            raise QueryError(422, "invalid_typed_value", "PostgreSQL text cannot contain NUL")
        yield text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _copy_cell(value: Any, spec: ColumnSpec) -> str:
    return "".join(_copy_parts(value, spec))


def _copy_line(values, specs: list[ColumnSpec], limit: int, *, size_only: bool = False) -> tuple[str, int]:
    """Bound UTF-8 COPY bytes while encoding, not after a full-row allocation."""
    output = None if size_only else io.StringIO()
    size = 0
    def append(part):
        nonlocal size
        size += len(part.encode("utf-8"))
        if size > limit:
            raise QueryError(422, "query_budget_exceeded", "A query row exceeds the bounded COPY buffer",
                             budget="copy_buffer_bytes", limitBytes=limit)
        if output is not None: output.write(part)
    for index, (value, spec) in enumerate(zip(values, specs)):
        if index: append("\t")
        for part in _copy_parts(value, spec): append(part)
    append("\n")
    return output.getvalue() if output is not None else "", size
