"""Typed row-token canonicalization contracts.

These tests pin the shared source/replica token rules that keep controlled
source CRUD on the source-authoritative CAS path:

- JSON canonicalization only for columns whose typed metadata says JSON;
  whitespace/key order are removed, numeric scale and duplicate-key order are
  preserved, SQL NULL stays distinct from JSON literal null.
- Single-precision columns hash their exact IEEE-754 float32 bits. This maps a
  Debezium short decimal and the source's CAST(... AS DOUBLE) value to the same
  token without a 6-digit display rounding.
- Source token SELECT aliases never collide with real column names.
"""
from __future__ import annotations

import struct

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from app.data_channel.integrated import native
from app.data_channel.integrated.contracts import JsonText, row_token
from app.data_channel.integrated.replica_store import (
    build_replica_schema, build_table,
)

FP = "token-contract-fingerprint"


def _widened(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def test_json_canonicalization_is_typed_and_keeps_source_semantics():
    spaced = '{"b": 2, "a": 1, "n": 123456789012345678901234567890, "d": 1.0}'
    compact = '{"a":1,"b":2,"n":123456789012345678901234567890,"d":1.0}'
    assert row_token({"j": spaced}, FP, json_columns={"j"}) == \
        row_token({"j": compact}, FP, json_columns={"j"})

    # JSON number spelling/scale remains meaningful (source MySQL keeps 1 and
    # 1.0 distinct); canonicalization is not a numeric-value rounder.
    assert row_token({"j": "1"}, FP, json_columns={"j"}) != \
        row_token({"j": "1.0"}, FP, json_columns={"j"})
    assert row_token(
        {"j": '{"n": 123456789012345678901234567890}'}, FP,
        json_columns={"j"}) != row_token(
        {"j": '{"n": 123456789012345678901234567891}'}, FP,
        json_columns={"j"})

    # Duplicate keys are source-visible when their relative order changes.
    assert row_token({"j": '{"a":1,"a":2}'}, FP, json_columns={"j"}) == \
        row_token({"j": '{"a":1,"a":2}'}, FP, json_columns={"j"})
    assert row_token({"j": '{"a":1,"a":2}'}, FP, json_columns={"j"}) != \
        row_token({"j": '{"a":2,"a":1}'}, FP, json_columns={"j"})

    # SQL NULL must never collapse into JSON literal null.
    assert row_token({"j": None}, FP, json_columns={"j"}) != \
        row_token({"j": "null"}, FP, json_columns={"j"})
    # JSON scalar string and number stay distinct.
    assert row_token({"j": '1'}, FP, json_columns={"j"}) != \
        row_token({"j": '"1"'}, FP, json_columns={"j"})

    # A TEXT column containing JSON-looking text is not canonicalized.
    assert row_token({"s": '{"a": 1}'}, FP) != \
        row_token({"s": '{"a":1}'}, FP)


def test_float32_token_uses_exact_ieee_bits_not_display_rounding():
    short = 1.234567
    exact = _widened(short)
    assert exact != short
    assert row_token({"f": short}, FP, float32_columns={"f"}) == \
        row_token({"f": exact}, FP, float32_columns={"f"})
    # Exact float32 bits are retained: two distinct single-precision values
    # never collide, unlike a FLT_DIG=6 display round.
    other = _widened(1.234568)
    assert other != exact
    assert row_token({"f": exact}, FP, float32_columns={"f"}) != \
        row_token({"f": other}, FP, float32_columns={"f"})
    # Without typed metadata the values are not silently rounded.
    assert row_token({"f": short}, FP) != row_token({"f": exact}, FP)
    # Double columns are not float32-canonicalized.
    assert row_token({"d": 1.2345678901234567}, FP) != \
        row_token({"d": _widened(1.2345678901234567)}, FP)
    # SQL NULL remains NULL.
    assert row_token({"f": None}, FP, float32_columns={"f"}) == \
        row_token({"f": None}, FP, float32_columns={"f"})


def test_source_token_select_casts_float_family_and_alias_collision_safe():
    table = sa.Table(
        "token_source", sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("f", mysql.FLOAT),
        sa.Column("d", mysql.DOUBLE),
        sa.Column("__oo_token_exact_0", sa.String(20)),
        sa.Column("__oo_token_exact_1", sa.String(20)),
    )
    aliases = native.source_token_exact_aliases(table)
    assert set(aliases) == {"f", "d"}
    assert len(set(aliases.values())) == len(aliases)
    assert not set(aliases.values()) & set(table.c.keys())

    # Exact alias expressions are SELECT Cast nodes even though the MySQL
    # dialect's standalone compiler warns; execution emits CAST(... AS DOUBLE).
    assert isinstance(native._token_expression(table.c.f), sa.sql.elements.Cast)
    assert isinstance(native._token_expression(table.c.d), sa.sql.elements.Cast)
    assert native._token_expression(table.c.id) is table.c.id

    selected = native.source_token_select(table, include_source=True)
    keys = list(selected.selected_columns.keys())
    assert "f" in keys and "d" in keys
    assert aliases["f"] in keys and aliases["d"] in keys
    assert len(keys) == len(set(keys))

    row = {
        "id": 1,
        "f": 1.234567,  # Debezium short/display value
        "d": 1.2345678901,  # driver Decimal scale, exact alias wins
        "__oo_token_exact_0": "real-column-0",
        "__oo_token_exact_1": "real-column-1",
        aliases["f"]: _widened(1.234567),
        aliases["d"]: 1.2345678901234567,
    }
    token_row = native.token_row_from_source(row, table, aliases)
    assert token_row["f"] == _widened(1.234567)
    assert token_row["d"] == 1.2345678901234567
    assert token_row["__oo_token_exact_0"] == "real-column-0"
    assert token_row["__oo_token_exact_1"] == "real-column-1"


def test_replica_store_real_only_for_true_single_precision_source_type():
    manifest = {
        "columns": [
            {"name": "id", "type": "integer", "source_type": "INTEGER",
             "nullable": False, "primary_key": True},
            {"name": "mf", "type": "float", "source_type": "FLOAT",
             "nullable": True, "primary_key": False},
            {"name": "pr", "type": "float", "source_type": "REAL",
             "nullable": True, "primary_key": False},
            {"name": "md", "type": "float", "source_type": "DOUBLE",
             "nullable": True, "primary_key": False},
            {"name": "pgd", "type": "float", "source_type": "DOUBLE PRECISION",
             "nullable": True, "primary_key": False},
        ],
        "primary_key": ["id"],
    }
    schema = build_replica_schema(
        "6f1d2c30-9f5e-4f6a-8a3b-2c5d7e9f0a1b", manifest)
    table = build_table(schema, "postgresql")
    assert str(table.c.mf.type).upper() == "REAL"
    assert str(table.c.pr.type).upper() == "REAL"
    assert str(table.c.md.type).upper() != "REAL"
    assert str(table.c.pgd.type).upper() != "REAL"
    assert native.replica_token_float32_columns(schema.columns, "mysql") == {"mf"}
    assert native.replica_token_float32_columns(schema.columns, "postgresql") == {"pr"}
