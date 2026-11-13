#!/usr/bin/env python3
r"""Real-PostgreSQL focused gate for integrated replica_store (typed physical tables).

Closes the gap the SQLite store tests leave open (replica_store module docstring:
"PostgreSQL 端（COPY/json 原文/timestamptz）语义按构造成立但未经本模块测试验证").
Everything here runs against a real PostgreSQL 16 server via the isolated fixture
network oo-replica-spike-20261112 (target-pg:5432, user replica_fixture). It never
touches production: DATABASE_URL is asserted to point at the replica_store_probe
fixture database before any app import.

Reproduce (from the worktree root):

  docker exec oo-replica-spike-20261112-target-pg-1 createdb -U replica_fixture replica_store_probe
  docker run --rm --network oo-replica-spike-20261112 \
    -e PYTHONPATH=/work -e PYTHONDONTWRITEBYTECODE=1 -e ENVIRONMENT=test \
    -e SECRET_KEY=replica-store-probe-secret-fixture \
    -e ENCRYPTION_KEY=dATFIbEs3YLWEmmigDIrozBhYzSPcPfPO5T82nO8mgI= \
    -e DATABASE_URL=postgresql://replica_fixture:replica_fixture@target-pg:5432/replica_store_probe \
    -v "$PWD/backend:/work:ro" -v "$PWD/scripts/e2e:/probe:ro" \
    -v "$PWD/.artifacts/replica-store-pg:/artifacts" -w /tmp \
    --entrypoint python3 openontology-cdc-executor:canary \
    /probe/integrated_replica_store_pg.py --out /artifacts

Coverage (all against real PG typed columns / COPY / timestamptz / json raw text):
- DDL is truly typed (int8/numeric(20,0)/numeric(65,30)/timestamptz/time/date/
  bool/text/float8/bytea/json), PK-only index, no SERIAL defaults;
- full-type lossless round-trip incl. SQL NULL vs JSON null, exact raw JsonText
  (spacing, duplicate keys, \u escapes preserved = json not jsonb), signed
  bigint extremes, unsigned 2^64-1, decimal(65,30) extremes, tz timestamps
  normalized to UTC, binary incl. zero bytes, COPY-escape strings;
- row_token exactness: token(read-back native row) == token(expected native row)
  for every row;
- COPY initial load in batches of 1000 and streaming read_batches batch 1000;
- apply_changes: upsert, repeated upsert (last image wins), PK change via
  before_key (incl. before_key of a missing key), delete, idempotent delete;
- transaction ownership: DDL+COPY rollback leaves no table, change rollback
  leaves prior state, duplicate-PK COPY fails loudly and the txn is recoverable
  (store never self-commits);
- ownership guard: drop_owned only drops its derived lake_ir_ table, foreign
  lake_iv_/lake_ds_ tables survive, invalid run ids rejected.

Exit code 0 iff every check passes; results land in <out>/results.json.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import uuid as uuidlib

# --- fixture guard: must run before importing any app.* module ---------------
_EXPECTED_DB = "replica_store_probe"
_database_url = os.environ.get("DATABASE_URL", "")
if (not _database_url.startswith("postgresql://")
        or f"@target-pg:5432/{_EXPECTED_DB}" not in _database_url):
    sys.exit(f"refusing to run: DATABASE_URL must target the fixture "
             f"target-pg:5432/{_EXPECTED_DB} (got {_database_url!r})")

import sqlalchemy as sa

from app.data_channel.integrated import replica_store as rs
from app.data_channel.integrated.contracts import JsonText, digest, row_token, wire_value
from app.data_channel.integrated.replica_store import (
    ReplicaChange, ReplicaStoreError, apply_changes, build_copy_text,
    build_replica_schema, count_rows, create_table, decode_row, drop_owned,
    read_batches, replica_table_name, write_batch,
)

RUN_MAIN = "6f1d2c30-9f5e-4f6a-8a3b-2c5d7e9f0a1b"      # 主全类型表
RUN_TXN = "7a2e3d41-0b6f-5d7b-9c4d-3d6e8f0a1b2c"       # DDL/COPY 回滚表
RUN_DROP = "8b3f4e52-1c70-6e8c-ad5e-4e7f9a1b2c3d"      # drop 归属表
N_ROWS = 2500
COPY_BATCH = 1000

G_JSON = ('{"dup":1,  "dup":2,"big":123456789012345678901234567890.123456789,'
          '"nested":{"a":[1,2,{"k":"v"}]},"unicode":"héllo☃","s p a c e":  1}')
G_JSON_NULL = "null"                       # JSON null（在值，非 SQL NULL）
G_JSON_STR = r'"top-level string with \"esc\" and \\ and \u00e9"'
G_JSON_BIG = '{"n":123456789012345678901234567890.123456789}'
G_JSON_ESC = r'{"\u00e9":"caf\u00e9 \u2603"}'   # raw \u 转义必须原样保留（json 非 jsonb）
G_JSON_UP = '{"z":  1, "a":[1,  2,  3]}'
TXT_ESCAPE = "tab\there\nnewline\ncr\r back\\slash \\\\N literal and trailing spaces  "
LNAME = ("Ratio With A Very Long Mixed Case Name That Exceeds Forty Eight Bytes "
         "For The Sanitizer")                # 触发 sanitize_lake_column 截断映射

RESULTS: list[dict] = []
EDGES: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail and not ok else ""))
    return bool(ok)


def edge(name: str, detail: str) -> None:
    EDGES.append({"name": name, "detail": detail})
    print(f"[EDGE] {name} :: {detail}")


def expect_reject(name: str, code: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except ReplicaStoreError as exc:
        check(name, exc.code == code, f"code={exc.code} expected={code}")
        return
    check(name, False, f"expected ReplicaStoreError {code}, nothing raised")


def col(name, kind, *, nullable=True, pk=False, **extra):
    entry = {"name": name, "type": kind, "source_type": kind.upper(),
             "nullable": nullable, "precision": None, "scale": None,
             "timezone": None, "collation": None, "length": None,
             "unsigned": False, "primary_key": pk}
    entry.update(extra)
    return entry


MANIFEST = {"columns": [
    col("id", "integer", nullable=False, pk=True),
    col("big_signed", "integer"),
    col("big_unsigned", "integer", unsigned=True),
    col("amount", "decimal", precision=65, scale=30),
    col("ts_utc", "timestamp", timezone=True),
    col("ts_naive", "timestamp"),
    col("wall_time", "time"),
    col("day", "date"),
    col("flag", "boolean"),
    col("doc", "json"),
    col("blob", "binary"),
    col("txt", "string"),
    col("uuid_like", "string"),
    col("ratio", "float"),
    col(LNAME, "float"),
], "primary_key": ["id"]}

PAD30 = "0" * 30
TZ530 = timezone(timedelta(hours=5, minutes=30))


def expected_row(i: int) -> dict:
    """独立构造的期望 native 行（不经被测模块的解码器）。"""
    if i == 10:  # 全可空列 NULL 行
        return {"id": i, "big_signed": None, "big_unsigned": None, "amount": None,
                "ts_utc": None, "ts_naive": None, "wall_time": None, "day": None,
                "flag": None, "doc": None, "blob": None, "txt": None,
                "uuid_like": None, "ratio": None, LNAME: None}
    row = {
        "id": i,
        "big_signed": {1: -(2**63), 2: 2**63 - 1, 3: -(2**53) - 1, 4: 2**53}.get(i, i * 1000 - 7),
        "big_unsigned": {1: 2**64 - 1, 2: 2**64 - 2, 3: 0}.get(
            i, 18446744073709551000 + i),
        "amount": {1: Decimal("9" * 35 + "." + "9" * 30),
                   2: Decimal("-0.000000000000000000000000000001"),
                   3: Decimal("0." + PAD30),
                   4: Decimal("-" + "9" * 35 + "." + "9" * 30)}.get(
            i, Decimal(f"{i}.45" + PAD30[2:])),
        "ts_utc": {1: datetime(2026, 11, 12, 1, 2, 3, 123456, tzinfo=timezone.utc),
                   2: datetime(2026, 11, 12, 7, 0, 0, tzinfo=TZ530).astimezone(timezone.utc),
                   3: datetime(1970, 1, 1, tzinfo=timezone.utc),
                   4: datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)}.get(
            i, datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)),
        "ts_naive": {1: datetime(2026, 11, 12, 1, 2, 3, 999999),
                     2: datetime(1, 1, 1),
                     3: datetime(2026, 11, 12, 0, 0, 0)}.get(
            i, datetime(2026, 3, 1) + timedelta(seconds=i)),
        "wall_time": {1: time(23, 59, 59, 999999), 2: time(0, 0, 0)}.get(
            i, time((i // 60) % 24, i % 60, (i * 7) % 60, (i * 11) % 1000000)),
        "day": {1: date(2026, 11, 12), 2: date(1, 1, 1), 3: date(9999, 12, 31)}.get(
            i, date(2026, 1, 1) + timedelta(days=i % 300)),
        "flag": {1: True, 2: False}.get(i, i % 2 == 0),
        "doc": {1: JsonText(G_JSON), 2: JsonText(G_JSON_NULL), 4: JsonText(G_JSON_STR),
                5: JsonText(G_JSON_BIG), 6: JsonText(G_JSON_ESC), 7: JsonText("[]"),
                8: JsonText("{}")}.get(i, JsonText(json.dumps({"i": i, "s": f"v{i}"*3},
                                                              separators=(",", ":")))),
        "blob": {1: bytes(range(256)), 2: b"", 3: b"\x00", 4: b"\x00\xff\x00"}.get(
            i, i.to_bytes(4, "big") * 3),
        "txt": {1: TXT_ESCAPE, 2: "", 3: "null", 4: "\\N", 5: "0"}.get(i, f"row-{i}"),
        "uuid_like": "6f1d2c30-9f5e-4f6a-8a3b-2c5d7e9f0a1b" if i == 1
                     else str(uuidlib.UUID(int=i)),
        "ratio": {1: 0.1, 2: -0.0, 3: 1.7976931348623157e+308, 4: 5e-324}.get(i, i / 7.0),
        LNAME: i / 3.0,
    }
    return row


def wire_row(i: int) -> dict:
    """源读 wire 形态：native 期望值经 contracts.wire_value（与真实源读同一编码）。"""
    return {k: wire_value(v) for k, v in expected_row(i).items()}


def read_all(conn, schema, batch_size=1000):
    chunks = []
    for batch in read_batches(conn, schema, batch_size=batch_size):
        chunks.append(batch)
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/artifacts")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    engine = sa.create_engine(
        "postgresql+psycopg2://replica_fixture:replica_fixture@target-pg:5432/"
        + _EXPECTED_DB,
        connect_args={"options": "-c timezone=UTC"})

    # -- 1. 身份派生 / 指纹（PG 路径同款纯函数，快速复核） --------------------
    check("identity/name derivation", replica_table_name(RUN_MAIN)
          == "lake_ir_" + uuidlib.UUID(RUN_MAIN).hex
          and rs.TABLE_RE.fullmatch(replica_table_name(RUN_MAIN)))
    expect_reject("identity rejects non-uuid", "IDENTITY_INVALID",
                  replica_table_name, "not-a-uuid")
    fingerprint = digest(MANIFEST)
    schema = build_replica_schema(RUN_MAIN, MANIFEST, schema_fingerprint=fingerprint)
    check("schema fingerprint binding", schema.schema_fingerprint == fingerprint)
    expect_reject("fingerprint mismatch rejected", "MANIFEST_INVALID",
                  build_replica_schema, RUN_MAIN, MANIFEST, schema_fingerprint="0" * 64)
    long_phys = schema.column_map[LNAME]
    check("long source name mapped to sanitized physical name",
          long_phys != LNAME and len(long_phys.encode()) <= 63,
          f"physical={long_phys!r}")

    # -- 2. 建表 + 真实 PG 类型断言 -------------------------------------------
    with engine.connect() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS lake_ir_"
                             + uuidlib.UUID(RUN_MAIN).hex))
        conn.commit()
    with engine.connect() as conn:
        create_table(conn, schema)
        conn.commit()
    try:
        with engine.connect() as conn:
            create_table(conn, schema)
            conn.commit()
        check("create_table twice errors", False, "recreate succeeded")
    except sa.exc.SQLAlchemyError as exc:
        check("create_table twice errors", True, type(exc).__name__)

    with engine.connect() as conn:
        catalog = {r.column_name: r for r in conn.execute(sa.text(
            "SELECT column_name, data_type, udt_name, numeric_precision,"
            " numeric_scale, datetime_precision, column_default"
            " FROM information_schema.columns WHERE table_name = :t"),
            {"t": schema.table_name})}
    expected_types = {
        "id": ("bigint", None), "big_signed": ("bigint", None),
        "big_unsigned": ("numeric", (20, 0)), "amount": ("numeric", (65, 30)),
        "ts_utc": ("timestamp with time zone", None),
        "ts_naive": ("timestamp without time zone", None),
        "wall_time": ("time without time zone", None), "day": ("date", None),
        "flag": ("boolean", None), "doc": ("json", None), "blob": ("bytea", None),
        "txt": ("text", None), "uuid_like": ("text", None),
        "ratio": ("double precision", None), long_phys: ("double precision", None),
    }
    phys = {c.physical_name: c for c in schema.columns}
    problems = []
    for pname, (dtype, nums) in expected_types.items():
        row = catalog.get(pname)
        if row is None:
            problems.append(f"{pname}: missing")
            continue
        if row.data_type != dtype:
            problems.append(f"{pname}: {row.data_type}!={dtype}")
        if nums:
            if (row.numeric_precision, row.numeric_scale) != nums:
                problems.append(f"{pname}: ({row.numeric_precision},{row.numeric_scale})!={nums}")
        if row.column_default is not None:
            problems.append(f"{pname}: has default {row.column_default!r}")
    check("PG physical column types exact (typed DDL, no serial defaults)",
          not problems, "; ".join(problems))
    with engine.connect() as conn:
        pk_cols = [r.attname for r in conn.execute(sa.text(
            "SELECT a.attname FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid"
            " JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=ANY(i.indkey)"
            " WHERE c.relname=:t AND i.indisprimary"), {"t": schema.table_name})]
        n_indexes = conn.execute(sa.text(
            "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid"
            " WHERE i.indrelid = CAST(:t AS regclass)"), {"t": schema.table_name}).scalar_one()
    check("PK-only index on source PK",
          pk_cols == [phys["id"].physical_name] and n_indexes == 1,
          f"pk={pk_cols} indexes={n_indexes}")

    # -- 3. COPY 初始装载（1000/批）+ 流式读取 + 全类型往返 + token 精确性 ----
    rows_wire = [wire_row(i) for i in range(N_ROWS)]
    # decode_row 必须把 wire 解回期望 native（解码一致性独立断言一次）
    sample_ok, sample_detail = True, ""
    for i in (0, 1, 2, 3, 4, 5, 6, 8, 10, 999):
        decoded = decode_row(schema, rows_wire[i])
        if decoded != expected_row(i):
            sample_ok, sample_detail = False, f"row {i}: {decoded}"
            break
    check("decode_row(wire) == expected native (sample)", sample_ok, sample_detail)

    copy_text = build_copy_text(schema, [decode_row(schema, rows_wire[10])])
    check("COPY text NULL sentinel row (all-nullable \\N)",
          copy_text.count("\\N") == 14 and copy_text.count("\t") == 14, repr(copy_text[:120]))

    with engine.connect() as conn:
        conn.execute(sa.text(f"TRUNCATE {schema.table_name}"))
        conn.commit()
    with engine.connect() as conn:
        tx = conn.begin()
        for start in range(0, N_ROWS, COPY_BATCH):
            write_batch(conn, schema, rows_wire[start:start + COPY_BATCH])
        tx.commit()
    with engine.connect() as conn:
        check("count_rows after COPY load", count_rows(conn, schema) == N_ROWS)
        chunks = read_all(conn, schema, batch_size=1000)
    sizes = [len(c) for c in chunks]
    check("streamed read_batches batch_size=1000 chunking",
          sizes == [1000, 1000, 500], f"sizes={sizes[:5]}")
    read_rows = [row for chunk in chunks for row in chunk]
    ids = [r["id"] for r in read_rows]
    check("read order is PK ascending", ids == sorted(ids))

    mismatch, token_mismatch = [], []
    for i, got in enumerate(read_rows):
        want = expected_row(i)
        if got != want:
            mismatch.append((i, {k: (got[k], want[k]) for k in want if got.get(k) != want[k]}))
        elif row_token(got, fingerprint) != row_token(want, fingerprint):
            token_mismatch.append(i)
    check("full-type lossless round-trip on real PG (all 2500 rows)",
          not mismatch, f"first={mismatch[0] if mismatch else None}")
    check("row_token exactness read-back vs expected (all 2500 rows)",
          not token_mismatch, f"rows={token_mismatch[:5]}")
    got1 = read_rows[1]
    check("json raw text byte-exact (spacing/dup keys/\\u escapes preserved)",
          str(got1["doc"]) == G_JSON and str(read_rows[2]["doc"]) == G_JSON_NULL
          and str(read_rows[6]["doc"]) == G_JSON_ESC
          and str(read_rows[5]["doc"]) == G_JSON_BIG,
          repr([str(read_rows[k]["doc"])[:60] for k in (1, 2, 6, 5)]))
    check("SQL NULL vs JSON null distinct",
          read_rows[10]["doc"] is None and read_rows[2]["doc"] is not None
          and str(read_rows[2]["doc"]) == "null")
    check("unsigned 2^64-1 read back as int",
          got1["big_unsigned"] == 2**64 - 1 and isinstance(got1["big_unsigned"], int))
    check("signed bigint extremes exact",
          got1["big_signed"] == -(2**63) and read_rows[2]["big_signed"] == 2**63 - 1)
    check("decimal(65,30) extremes exact",
          str(got1["amount"]) == "9" * 35 + "." + "9" * 30
          and str(read_rows[2]["amount"]) == str(Decimal("-0.000000000000000000000000000001")))
    check("timestamptz normalized to UTC instant",
          got1["ts_utc"] == datetime(2026, 11, 12, 1, 2, 3, 123456, tzinfo=timezone.utc)
          and read_rows[2]["ts_utc"] == datetime(2026, 11, 12, 1, 30, 0, tzinfo=timezone.utc))
    check("binary zero bytes exact", got1["blob"] == bytes(range(256))
          and read_rows[3]["blob"] == b"\x00" and isinstance(got1["blob"], bytes))
    check("COPY-escaped string exact",
          got1["txt"] == TXT_ESCAPE and read_rows[4]["txt"] == "\\N"
          and read_rows[2]["txt"] == "")

    # 信息性：非源形态（未按 scale 补零）的 decimal wire 在 PG 会被补零，
    # 与 SQLite 变体（保持原样）方言不同；真实源读 wire 总是补零的。
    with engine.connect() as conn:
        tx = conn.begin()
        unpadded = dict(rows_wire[500])
        unpadded["id"] = 950001
        unpadded["amount"] = {"$decimal": "1.1"}
        write_batch(conn, schema, [unpadded])
        tx.commit()
    with engine.connect() as conn:
        back = conn.execute(sa.text(
            f"SELECT amount FROM {schema.table_name} WHERE id = 950001")).scalar_one()
        edge("unpadded decimal wire input",
             f'input "1.1" -> PG numeric(65,30) reads back "{back}" '
             f'(source-read wires are scale-padded, so the pipeline path is unaffected)')
    with engine.connect() as conn:
        tx = conn.begin()
        conn.execute(sa.text(f"DELETE FROM {schema.table_name} WHERE id = 950001"))
        tx.commit()

    # -- 4. apply_changes：upsert / 重复 upsert / PK 变更 / delete ------------
    def full_wire(i: int, **over) -> dict:
        row = dict(rows_wire[0])
        row["id"] = i
        row.update(over)
        return row

    with engine.connect() as conn:
        tx = conn.begin()
        apply_changes(conn, schema, [])
        apply_changes(conn, schema, [ReplicaChange(op="upsert", values=full_wire(900001))])
        tx.commit()
        rows = [r for c in read_all(conn, schema) for r in c if r["id"] == 900001]
    base = expected_row(0) | {"id": 900001}
    check("upsert new row round-trips",
          len(rows) == 1 and rows[0] == base
          and row_token(rows[0], fingerprint) == row_token(base, fingerprint))

    changed = full_wire(900001, txt="second image", doc=G_JSON_UP,
                        amount={"$decimal": "-2.500000000000000000000000000000"})
    with engine.connect() as conn:
        tx = conn.begin()
        apply_changes(conn, schema, [ReplicaChange(op="upsert", values=changed)])
        apply_changes(conn, schema, [ReplicaChange(op="upsert", values=changed)])
        tx.commit()
        rows = [r for c in read_all(conn, schema) for r in c if r["id"] == 900001]
        total = count_rows(conn, schema)
    exp_changed = expected_row(0) | {"id": 900001, "txt": "second image",
                                     "doc": JsonText(G_JSON_UP),
                                     "amount": Decimal("-2.500000000000000000000000000000")}
    check("repeated upsert is idempotent, last image wins",
          len(rows) == 1 and total == N_ROWS + 1 and rows[0] == exp_changed
          and str(rows[0]["doc"]) == G_JSON_UP)

    pk_change = full_wire(900002, txt="moved")
    with engine.connect() as conn:
        tx = conn.begin()
        apply_changes(conn, schema, [ReplicaChange(op="upsert", values=pk_change,
                                                   before_key={"id": 900001})])
        tx.commit()
        got = {r["id"]: r for c in read_all(conn, schema) for r in c
               if r["id"] in (900001, 900002)}
    check("PK change via before_key: old key gone, new row exact",
          900001 not in got and got[900002]["txt"] == "moved")

    with engine.connect() as conn:
        tx = conn.begin()
        # before_key 指向不存在键：delete 0 行后照常 upsert（幂等语义）
        apply_changes(conn, schema, [ReplicaChange(op="upsert", values=full_wire(900003),
                                                   before_key={"id": 777777})])
        tx.commit()
    with engine.connect() as conn:
        before = count_rows(conn, schema)
    with engine.connect() as conn:
        tx = conn.begin()
        apply_changes(conn, schema, [ReplicaChange(op="delete", key={"id": 900003})])
        after_del = count_rows(conn, schema)
        apply_changes(conn, schema, [ReplicaChange(op="delete", key={"id": 900003})])
        after_del2 = count_rows(conn, schema)
        tx.commit()
    check("delete + idempotent delete of missing key",
          after_del == before - 1 and after_del2 == after_del)

    with engine.connect() as conn:
        tx = conn.begin()
        try:
            apply_changes(conn, schema, [ReplicaChange(op="explode", values={})])
            check("unknown change op rejected", False, "accepted")
        except ReplicaStoreError as exc:
            check("unknown change op rejected", exc.code == "INVALID_CHANGE", exc.code)
        tx.rollback()
    bad = full_wire(900004)
    bad["big_unsigned"] = {"$integer": "-1"}
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            apply_changes(conn, schema, [ReplicaChange(op="upsert", values=bad)])
            check("negative unsigned rejected", False, "accepted")
        except ReplicaStoreError as exc:
            check("negative unsigned rejected", exc.code == "INVALID_VALUE", exc.code)
        tx.rollback()

    # -- 5. 事务归调用方（store 绝不自 commit） -------------------------------
    schema_txn = build_replica_schema(RUN_TXN, MANIFEST, schema_fingerprint=fingerprint)
    with engine.connect() as conn:
        tx = conn.begin()
        create_table(conn, schema_txn)
        write_batch(conn, schema_txn, rows_wire[:5])
        tx.rollback()
        exists = sa.inspect(conn).has_table(schema_txn.table_name)
    check("DDL+COPY fully rolled back: table gone (no self-commit)", not exists)

    with engine.connect() as conn:
        tx = conn.begin()
        apply_changes(conn, schema, [ReplicaChange(op="upsert", values=full_wire(900005))])
        tx.rollback()
        rows = [r for c in read_all(conn, schema) for r in c if r["id"] == 900005]
    check("apply_changes rollback leaves prior state", not rows)

    with engine.connect() as conn:
        tx = conn.begin()
        dup = [full_wire(910001), dict(full_wire(910001))]
        error_name = ""
        try:
            write_batch(conn, schema, dup)
            check("duplicate-PK COPY fails loudly", False, "no error")
        except Exception as exc:  # psycopg2 UniqueViolation（原始 COPY 游标）
            error_name = type(exc).__name__
            check("duplicate-PK COPY fails loudly",
                  "Unique" in error_name or "Integrity" in error_name, error_name)
        tx.rollback()
        still = count_rows(conn, schema)
    check("txn recoverable after COPY failure",
          still == N_ROWS + 1, f"count={still}")

    # -- 6. drop 归属与防任意表删除 -------------------------------------------
    with engine.connect() as conn:
        conn.execute(sa.text(
            f"DROP TABLE IF EXISTS {schema_txn.table_name};"
            "CREATE TABLE IF NOT EXISTS lake_iv_foreignprobe (x int);"
            "CREATE TABLE IF NOT EXISTS lake_ds_foreignprobe (x int)"))
        conn.commit()
    with engine.connect() as conn:
        tx = conn.begin()
        create_table(conn, schema_txn)
        tx.commit()  # 表先持久存在，drop 回滚测试才有意义
    with engine.connect() as conn:
        tx = conn.begin()
        gone = drop_owned(conn, RUN_TXN)
        tx.rollback()  # drop 同样不自 commit：回滚后表仍在
    with engine.connect() as conn:
        survived = sa.inspect(conn).has_table(schema_txn.table_name)
    with engine.connect() as conn:
        tx = conn.begin()
        drop_owned(conn, RUN_TXN)
        tx.commit()
        final = not sa.inspect(conn).has_table(schema_txn.table_name)
        foreign = conn.execute(sa.text(
            "SELECT count(*) FROM information_schema.tables WHERE table_name IN"
            " ('lake_iv_foreignprobe','lake_ds_foreignprobe')")).scalar_one()
    check("drop_owned reports existence and honors caller rollback",
          gone is True and survived and final,
          f"gone={gone} survived_after_rollback={survived} absent_after_commit={final}")
    check("foreign lake_iv_/lake_ds_ tables survive drop_owned", foreign == 2)
    with engine.connect() as conn:
        expect_reject("drop_owned rejects invalid run id", "IDENTITY_INVALID",
                      drop_owned, conn, "'; DROP TABLE lake_iv_foreignprobe; --")

    engine.dispose()

    passed = sum(1 for r in RESULTS if r["ok"])
    failed = len(RESULTS) - passed
    payload = {"passed": passed, "failed": failed, "results": RESULTS, "edges": EDGES,
               "database": _EXPECTED_DB, "rows": N_ROWS}
    (out / "results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\n{passed} passed, {failed} failed, {len(EDGES)} edges "
          f"-> {out / 'results.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
