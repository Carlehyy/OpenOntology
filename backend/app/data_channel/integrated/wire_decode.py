"""Exact wire-value decoding shared by source writes and replica storage.

Leaf module: it must not import ``integrated.service`` or ``replica_store``.
Values are decoded against the reflected SQLAlchemy column, preserving SQL NULL
vs JSON null, exact decimals, temporal precision and binary payloads.
"""
from __future__ import annotations

import base64
import json
import math
import re
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from app.data_channel.integrated.contracts import JsonText, fail


def _parse_temporal(value, parser):
    # fromisoformat silently drops digits beyond Python's microsecond precision.
    # Extra trailing zeroes are exact; nonzero discarded digits are never exact.
    if not isinstance(value, str) or any(
        any(digit != "0" for digit in fraction[6:])
        for fraction in re.findall(r"[.,]([0-9]+)", value)
    ):
        raise ValueError()
    # A fractional UTC offset is not a portable source timezone. Python even
    # silently treats +00:00:00.1 as UTC; reject rather than alter the instant.
    result = parser(value)
    offset = re.search(r"[+-][0-9:]+[.,]([0-9]+)$", value) if result.tzinfo else None
    if offset and any(digit != "0" for digit in offset.group(1)):
        raise ValueError()
    return result


def _check_temporal_precision(typ, result):
    if isinstance(typ, (mysql.DATETIME, mysql.TIMESTAMP, mysql.TIME)):
        precision = 0 if typ.fsp is None else typ.fsp
    else:
        precision = getattr(typ, "precision", None)
        precision = 6 if precision is None else precision
    if not isinstance(precision, int) or not 0 <= precision <= 6:
        raise ValueError()
    if result.microsecond % (10 ** (6 - precision)):
        raise ValueError()


def _decode(column, value):
    if value is None:
        if not column.nullable:
            fail(422, "NULL_NOT_ALLOWED", f"Column {column.name} is not nullable")
        # SQLAlchemy JSON normally binds Python None as JSON literal null.
        # The wire contract reserves None for SQL NULL, regardless of dialect.
        return sa.null() if isinstance(column.type, sa.JSON) else None
    typ = column.type
    try:
        if isinstance(typ, sa.Boolean):
            if not isinstance(value, bool):
                raise ValueError()
            return value
        if isinstance(typ, sa.Integer):
            if isinstance(value, dict) and set(value) == {"$integer"}:
                value = value["$integer"]
            if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"-?[0-9]+", str(value)):
                raise ValueError()
            return int(value)
        if isinstance(typ, (sa.Float, sa.REAL)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError()
            return value
        if isinstance(typ, sa.Numeric):
            if isinstance(value, dict) and set(value) == {"$decimal"}:
                value = value["$decimal"]
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError()
            result = Decimal(value)
            if not result.is_finite():
                raise ValueError()
            # Inspect decimal digits rather than quantize(): quantize uses the
            # process decimal context (often 28 digits) and may round or reject
            # a valid NUMERIC(65, 30) source value.
            normalized = result.as_tuple()
            digits = list(normalized.digits)
            exponent = normalized.exponent
            while len(digits) > 1 and digits[-1] == 0:
                digits.pop()
                exponent += 1
            fractional = max(-exponent, 0) if result else 0
            integer = max(len(digits) + exponent, 0) if result else 0
            if typ.scale is not None and fractional > typ.scale:
                raise ValueError()
            if typ.precision is not None:
                if typ.scale is not None and integer > typ.precision - typ.scale:
                    raise ValueError()
                if typ.scale is None and integer + fractional > typ.precision:
                    raise ValueError()
            return result
        if isinstance(typ, sa.DateTime):
            result = _parse_temporal(value, datetime.fromisoformat)
            has_timezone = isinstance(typ, mysql.TIMESTAMP) or bool(typ.timezone)
            if bool(result.tzinfo) != has_timezone:
                raise ValueError()
            if has_timezone:
                result = result.astimezone(timezone.utc)
                if isinstance(typ, mysql.TIMESTAMP):
                    result = result.replace(tzinfo=None)
            _check_temporal_precision(typ, result)
            return result
        if isinstance(typ, sa.Date):
            return date.fromisoformat(value)
        if isinstance(typ, sa.Time):
            result = _parse_temporal(value, time.fromisoformat)
            if bool(result.tzinfo) != bool(typ.timezone):
                raise ValueError()
            _check_temporal_precision(typ, result)
            return result
        if isinstance(typ, (sa.LargeBinary, sa.BINARY)):
            if not isinstance(value, dict) or set(value) != {"$binary"}:
                raise ValueError()
            return base64.b64decode(value["$binary"], validate=True)
        if isinstance(typ, sa.JSON):
            if not isinstance(value, str):
                raise ValueError()
            def reject_constant(_):
                raise ValueError()
            # Validate without converting high-precision JSON numbers to float;
            # bind the original text through the source engine's JSON serializer.
            json.loads(value, parse_float=Decimal, parse_constant=reject_constant)
            return JsonText(value)
        if isinstance(typ, sa.String):
            if not isinstance(value, str) or (typ.length and len(value) > typ.length):
                raise ValueError()
            return value
        if isinstance(typ, sa.Uuid):
            return uuid.UUID(value)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        fail(422, "INVALID_COLUMN_VALUE", f"Invalid or lossy value for column {column.name}")
    fail(422, "UNSUPPORTED_COLUMN_TYPE", f"Unsupported column {column.name}")
