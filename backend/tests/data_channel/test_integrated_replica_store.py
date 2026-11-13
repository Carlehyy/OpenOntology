"""replica_store 聚焦测试（SQLite 测试档；PostgreSQL COPY/typed 真实语义归主会话 gate）。

覆盖：表身份与 drop 归属验证、manifest 严格校验/指纹、列名映射（超长/截断
碰撞/大小写/保留词）、全类型无损往返与 row_token wire 等价（NULL vs JSON
null、宽整数/无符号、精确 decimal、binary、时间）、COPY text 纯序列化、
apply_changes 顺序/upsert/delete/PK 变更/幂等、事务归调用方（不自 commit）、
精度与类型拒绝（不静默舍入/降级）。
"""
from __future__ import annotations

import base64
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.data_channel.integrated import replica_store as rs
from app.data_channel.integrated.contracts import JsonText, digest, row_token
from app.data_channel.integrated.replica_store import (
    ReplicaChange, ReplicaStoreError, apply_changes, build_copy_text,
    build_replica_schema, build_table, count_rows, create_table, decode_row,
    drop_owned, manifest_fingerprint, read_batches, replica_table_name,
    write_batch,
)

RUN_ID = "6f1d2c30-9f5e-4f6a-8a3b-2c5d7e9f0a1b"
BAD_ID = "not-a-uuid"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def col(name, kind, *, nullable=True, pk=False, **extra):
    entry = {"name": name, "type": kind, "source_type": kind.upper(),
             "nullable": nullable, "precision": None, "scale": None,
             "timezone": None, "collation": None, "length": None,
             "unsigned": False, "primary_key": pk}
    entry.update(extra)
    return entry


def manifest(columns, primary_key):
    return {"columns": list(columns), "primary_key": list(primary_key)}


def simple_schema():
    m = manifest([col("id", "integer", nullable=False, pk=True),
                  col("v", "string")], ["id"])
    return build_replica_schema(RUN_ID, m)


@pytest.fixture()
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


def _read_all(connection, schema):
    return [row for batch in read_batches(connection, schema) for row in batch]


# ---------------------------------------------------------------------------
# 表身份与 drop 归属
# ---------------------------------------------------------------------------

def test_replica_table_name_derivation_and_rejection():
    assert replica_table_name(RUN_ID) == "lake_ir_" + uuid.UUID(RUN_ID).hex
    assert rs.TABLE_RE.fullmatch(replica_table_name(RUN_ID))
    for bad in (BAD_ID, "", " ", 123, None, "lake_ir_" + "z" * 32):
        with pytest.raises(ReplicaStoreError) as exc:
            replica_table_name(bad)
        assert exc.value.code == "IDENTITY_INVALID"


def test_drop_owned_only_its_derived_table(conn):
    schema = simple_schema()
    create_table(conn, schema)
    assert sa.inspect(conn).has_table(schema.table_name)
    assert drop_owned(conn, RUN_ID) is True
    assert not sa.inspect(conn).has_table(schema.table_name)
    # 幂等：不存在时返回 False，不报错。
    assert drop_owned(conn, RUN_ID) is False
    with pytest.raises(ReplicaStoreError):
        drop_owned(conn, BAD_ID)


def test_drop_owned_never_drops_foreign_tables(conn):
    schema = simple_schema()
    create_table(conn, schema)
    sa.Table("lake_iv_deadbeef" + "0" * 20, sa.MetaData(),
             sa.Column("x", sa.Integer)).create(conn)
    drop_owned(conn, RUN_ID)
    assert sa.inspect(conn).has_table("lake_iv_deadbeef" + "0" * 20)


# ---------------------------------------------------------------------------
# manifest 校验 / 指纹
# ---------------------------------------------------------------------------

def test_manifest_fingerprint_matches_native_digest():
    m = manifest([col("id", "integer", nullable=False, pk=True)], ["id"])
    assert manifest_fingerprint(m) == digest(m)


def test_fingerprint_mismatch_rejected():
    m = manifest([col("id", "integer", nullable=False, pk=True)], ["id"])
    with pytest.raises(ReplicaStoreError) as exc:
        build_replica_schema(RUN_ID, m, schema_fingerprint="0" * 64)
    assert exc.value.code == "MANIFEST_INVALID"
    schema = build_replica_schema(RUN_ID, m, schema_fingerprint=digest(m))
    assert schema.schema_fingerprint == digest(m)


@pytest.mark.parametrize("columns,primary_key,code", [
    ([], [], "MANIFEST_INVALID"),  # 空列 / 空 PK
    ([col("id", "integer", nullable=False)], [], "MANIFEST_INVALID"),
    ([col("id", "integer", nullable=False)], ["missing"], "MANIFEST_INVALID"),
    ([col("id", "integer", nullable=False), col("id", "string")], ["id"],
     "MANIFEST_INVALID"),  # 重复列名
    ([col("id", "integer", nullable=False), col("v", "string")], ["id", "id"],
     "MANIFEST_INVALID"),  # 重复 PK
    ([col("id", "array", nullable=False)], ["id"], "UNSUPPORTED_COLUMN_TYPE"),
    ([col("id", "integer", nullable=False),
      col("t", "time", timezone=True)], ["id"], "UNSUPPORTED_COLUMN_TYPE"),
    ([col("id", "json", nullable=False)], ["id"], "MANIFEST_INVALID"),  # json PK
    ([col("id", "decimal", nullable=False, precision=5, scale=6)], ["id"],
     "MANIFEST_INVALID"),  # scale > precision
    ([col("id", "decimal", nullable=False, precision=0)], ["id"],
     "MANIFEST_INVALID"),
    ([col("id", "decimal", nullable=False, precision=1001)], ["id"],
     "MANIFEST_INVALID"),
    ([col("id", "integer", nullable=False, precision=True)], ["id"],
     "MANIFEST_INVALID"),  # bool 不是 precision
    ([col("id", "integer", nullable="yes")], ["id"], "MANIFEST_INVALID"),
    ([col("id", "integer", nullable=False, unsigned="no")], ["id"],
     "MANIFEST_INVALID"),
    ([{"name": "id", "type": "integer", "nullable": False, "oops": 1}], ["id"],
     "MANIFEST_INVALID"),  # 未知列键
    ([{"name": "id", "type": "integer"}], ["id"], "MANIFEST_INVALID"),  # 缺键
    ([col("id", "integer", nullable=False, pk=False)], ["id"], "MANIFEST_INVALID"),
])
def test_invalid_manifests_rejected(columns, primary_key, code):
    with pytest.raises(ReplicaStoreError) as exc:
        build_replica_schema(RUN_ID, manifest(columns, primary_key))
    assert exc.value.code == code


def test_manifest_extra_top_level_key_rejected():
    m = manifest([col("id", "integer", nullable=False, pk=True)], ["id"])
    m["unexpected"] = 1
    with pytest.raises(ReplicaStoreError) as exc:
        build_replica_schema(RUN_ID, m)
    assert exc.value.code == "MANIFEST_INVALID"


def test_non_dict_manifest_rejected():
    for bad in (None, [], "columns", 5):
        with pytest.raises(ReplicaStoreError):
            build_replica_schema(RUN_ID, bad)


def test_collation_is_metadata_not_policy():
    # collation 只是携带的元数据：类型化副本存数据，不在此做策略拒绝。
    m = manifest([col("id", "integer", nullable=False, pk=True),
                  col("s", "string", collation="utf8mb4_bin")], ["id"])
    build_replica_schema(RUN_ID, m)


# ---------------------------------------------------------------------------
# 列名映射（超长/碰撞/大小写/保留词），确定性
# ---------------------------------------------------------------------------

def test_column_mapping_isolated_and_deterministic():
    long_a = "大列名" + "x" * 80 + "A"
    long_b = "大列名" + "x" * 80 + "B"
    columns = [col("id", "integer", nullable=False, pk=True),
               col("Name", "string"), col("name", "string"),
               col("select", "string"), col(long_a, "string"),
               col(long_b, "string")]
    schema = build_replica_schema(RUN_ID, manifest(columns, ["id"]))
    mapping = schema.column_map
    # 大小写敏感隔离 + 保留词保留原文（SQLAlchemy 负责引用）
    assert mapping["Name"] != mapping["name"]
    assert mapping["select"] == "select"
    # 截断到 sanitize 的 48 字节上限内；两个超长同前缀列必须区分开
    assert all(len(physical.encode("utf-8")) <= 48 for physical in mapping.values())
    assert mapping[long_a] != mapping[long_b]
    assert len(set(mapping.values())) == len(mapping)
    # 确定性：同一 manifest 重建得到同一映射
    again = build_replica_schema(RUN_ID, manifest(columns, ["id"]))
    assert again.column_map == mapping


def test_build_table_dialect_gate():
    schema = simple_schema()
    build_table(schema, "postgresql")
    build_table(schema, "sqlite")
    with pytest.raises(ReplicaStoreError) as exc:
        build_table(schema, "mysql")
    assert exc.value.code == "DIALECT_UNSUPPORTED"


# ---------------------------------------------------------------------------
# 全类型无损往返 + row_token wire 等价
# ---------------------------------------------------------------------------

DOC_TEXT = '{"a": 1.10, "b": [3.141592653589793238462643383279]}'
BLOB = b"\x00\x01\xfe binary \xff"


def full_manifest():
    columns = [
        col("id", "integer", nullable=False, pk=True, source_type="bigint"),
        col("label", "string"),
        col("big_u", "integer", unsigned=True, source_type="bigint unsigned"),
        col("big_s", "integer", source_type="bigint"),
        col("amount", "decimal", precision=12, scale=4),
        col("wide", "decimal", precision=65, scale=30),
        col("ratio", "float"),
        col("flag", "boolean"),
        col("day", "date"),
        col("moment_naive", "timestamp", timezone=False),
        col("moment_utc", "timestamp", timezone=True),
        col("wall", "time", timezone=False),
        col("blob", "binary"),
        col("doc", "json", encoding="json_text"),
        col("doc_sql_null", "json", encoding="json_text"),
        col("doc_json_null", "json", encoding="json_text"),
    ]
    return manifest(columns, ["id"])


def full_wire_row():
    return {
        "id": 1,
        "label": 'Alice "文本"',
        "big_u": {"$integer": "18446744073709551615"},
        "big_s": {"$integer": "9007199254740993"},
        "amount": {"$decimal": "123.4500"},
        "wide": {"$decimal": "12345678901234567890.123456789012345678901234567890"},
        "ratio": 0.1,
        "flag": True,
        "day": "2026-11-12",
        "moment_naive": "2026-11-12T01:02:03.456789",
        "moment_utc": "2026-11-12T09:30:00.123456+08:00",
        "wall": "23:59:59.999999",
        "blob": {"$binary": base64.b64encode(BLOB).decode("ascii")},
        "doc": DOC_TEXT,
        "doc_sql_null": None,
        "doc_json_null": "null",
    }


def full_native_row():
    return {
        "id": 1,
        "label": 'Alice "文本"',
        "big_u": 18446744073709551615,  # int：wire $integer，绝非 Decimal/$decimal
        "big_s": 9007199254740993,
        "amount": Decimal("123.4500"),
        "wide": Decimal("12345678901234567890.123456789012345678901234567890"),
        "ratio": 0.1,
        "flag": True,
        "day": date(2026, 11, 12),
        "moment_naive": datetime(2026, 11, 12, 1, 2, 3, 456789),
        "moment_utc": datetime(2026, 11, 12, 1, 30, 0, 123456, tzinfo=timezone.utc),
        "wall": time(23, 59, 59, 999999),
        "blob": BLOB,
        "doc": JsonText(DOC_TEXT),
        "doc_sql_null": None,
        "doc_json_null": JsonText("null"),
    }


def test_full_roundtrip_and_row_token_equivalence(conn):
    schema = build_replica_schema(RUN_ID, full_manifest())
    create_table(conn, schema)
    write_batch(conn, schema, [full_wire_row()])
    rows = _read_all(conn, schema)
    assert len(rows) == 1
    row = rows[0]
    expected = full_native_row()
    for name, value in expected.items():
        assert row[name] == value, name
    # SQL NULL 与 JSON null 是两个值，绝不互串
    assert row["doc_sql_null"] is None
    assert isinstance(row["doc_json_null"], JsonText)
    assert row["doc_json_null"] == "null"
    assert isinstance(row["doc"], JsonText)
    assert str(row["doc"]) == DOC_TEXT  # json 原文逐字保留
    # row_token 与源读 wire 完全同一命名空间
    assert row_token(row, schema.schema_fingerprint) == \
        row_token(expected, schema.schema_fingerprint)


def test_empty_table_supported(conn):
    schema = simple_schema()
    create_table(conn, schema)
    write_batch(conn, schema, [])
    assert count_rows(conn, schema) == 0
    assert _read_all(conn, schema) == []


def test_write_batch_duplicate_pk_fails_loud(conn):
    schema = simple_schema()
    create_table(conn, schema)
    write_batch(conn, schema, [{"id": 1, "v": "a"}])
    with pytest.raises(sa.exc.IntegrityError):
        write_batch(conn, schema, [{"id": 1, "v": "b"}])
    # 调用方事务内：批次失败不落库
    assert count_rows(conn, schema) == 1


# ---------------------------------------------------------------------------
# 精度/类型拒绝（不静默舍入、不静默降级）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row,code", [
    ({"id": 1, "amount": {"$decimal": "1.23456"}}, "INVALID_VALUE"),  # scale 溢出不四舍五入
    ({"id": 1, "amount": {"$decimal": "100000000.00"}}, "INVALID_VALUE"),  # 整数位溢出
    ({"id": 1, "big_u": {"$integer": "-1"}}, "INVALID_VALUE"),  # 负无符号
    ({"id": 1, "big_u": {"$integer": "100000000000000000000"}}, "INVALID_VALUE"),  # 21 位
    ({"id": None}, "INVALID_VALUE"),  # 主键空
    ({"id": 1, "flag": "yes"}, "INVALID_VALUE"),
    ({"id": 1, "ratio": "abc"}, "INVALID_VALUE"),
    ({"id": 1, "blob": "raw-string"}, "INVALID_VALUE"),  # binary 必须走 $binary
    ({"id": 1, "doc": "{o"}, "INVALID_VALUE"),
    ({"id": 1, "day": "2026-13-01"}, "INVALID_VALUE"),
])
def test_lossy_or_invalid_values_rejected(row, code):
    schema = build_replica_schema(RUN_ID, full_manifest())
    # 缺列补齐（本组只关心对应列的拒绝）
    wire = {**full_wire_row(), **row}
    with pytest.raises(ReplicaStoreError) as exc:
        decode_row(schema, wire)
    assert exc.value.code == code


def test_float_accepts_finite_decimal_from_http_boundary():
    # 接收端 parse_float=Decimal：源 float4/8 的有限 Decimal 必须按 native
    # float 语义绑定；非有限/溢出仍拒绝，绝不静默写入。
    schema = build_replica_schema(RUN_ID, full_manifest())
    decoded = decode_row(schema, {**full_wire_row(), "ratio": Decimal("0.1")})
    assert decoded["ratio"] == 0.1 and isinstance(decoded["ratio"], float)
    decoded = decode_row(schema, {**full_wire_row(), "ratio": Decimal("1E-400")})
    assert decoded["ratio"] == 0.0
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
                Decimal("1E+400")):
        with pytest.raises(ReplicaStoreError) as exc:
            decode_row(schema, {**full_wire_row(), "ratio": bad})
        assert exc.value.code == "INVALID_VALUE"


def test_timezone_strictness():
    schema = build_replica_schema(RUN_ID, full_manifest())
    naive_into_tz = {**full_wire_row(), "moment_utc": "2026-11-12T01:02:03"}
    offset_into_naive = {**full_wire_row(), "moment_naive": "2026-11-12T01:02:03+00:00"}
    for row in (naive_into_tz, offset_into_naive):
        with pytest.raises(ReplicaStoreError) as exc:
            decode_row(schema, row)
        assert exc.value.code == "INVALID_VALUE"


def test_decode_row_requires_exact_column_set():
    schema = build_replica_schema(RUN_ID, full_manifest())
    with pytest.raises(ReplicaStoreError) as exc:
        decode_row(schema, {"id": 1})
    assert exc.value.code == "UNKNOWN_COLUMN"
    with pytest.raises(ReplicaStoreError) as exc:
        decode_row(schema, {**full_wire_row(), "extra": 1})
    assert exc.value.code == "UNKNOWN_COLUMN"


# ---------------------------------------------------------------------------
# COPY text 纯序列化（PG COPY 执行本身归主会话真实 gate）
# ---------------------------------------------------------------------------

def test_build_copy_text_escaping_and_order():
    m = manifest([col("id", "integer", nullable=False, pk=True),
                  col("f", "boolean"), col("b", "binary"), col("j", "json"),
                  col("t", "string"), col("n", "string"),
                  col("ts", "timestamp", timezone=True),
                  col("d", "decimal", precision=8, scale=4)], ["id"])
    schema = build_replica_schema(RUN_ID, m)
    wire = {
        "id": 7, "f": True, "b": {"$binary": base64.b64encode(b"ab").decode()},
        "j": '{"a":\t1}', "t": "a\tb\nc\\d", "n": None,
        "ts": "2026-11-12T01:30:00.123456+00:00",
        "d": {"$decimal": "123.4500"}}
    decoded = decode_row(schema, wire)
    assert build_copy_text(schema, [decoded]) == (
        "7\ttrue\t\\\\x6162\t"            # bytea hex，反斜杠转义
        '{"a":\\t1}\t'                     # json 原文，内部 tab 转义
        "a\\tb\\nc\\\\d\t"                 # text 转义：tab/换行/反斜杠
        "\\N\t"                            # SQL NULL
        "2026-11-12 01:30:00.123456+00:00\t"
        "123.4500\n")
    # 列序 = manifest 列序；多行多行输出
    second = decode_row(schema, {**wire, "id": 8, "f": False, "n": "x"})
    text = build_copy_text(schema, [decoded, second])
    assert text.count("\n") == 2
    assert text.splitlines()[1].startswith("8\tfalse\t")


# ---------------------------------------------------------------------------
# apply_changes：顺序 / upsert / delete / PK 变更 / 幂等
# ---------------------------------------------------------------------------

def row(id_, v):
    return {"id": id_, "v": v}


def test_upsert_insert_update_and_idempotent_replay(conn):
    schema = simple_schema()
    create_table(conn, schema)
    apply_changes(conn, schema, [ReplicaChange(op="upsert", values=row(1, "a"))])
    assert _read_all(conn, schema) == [{"id": 1, "v": "a"}]
    apply_changes(conn, schema, [ReplicaChange(op="upsert", values=row(1, "b"))])
    apply_changes(conn, schema, [ReplicaChange(op="upsert", values=row(1, "b"))])
    assert _read_all(conn, schema) == [{"id": 1, "v": "b"}]
    assert count_rows(conn, schema) == 1


def test_delete_existing_and_missing_idempotent(conn):
    schema = simple_schema()
    create_table(conn, schema)
    write_batch(conn, schema, [row(1, "a"), row(2, "b")])
    apply_changes(conn, schema, [ReplicaChange(op="delete", key={"id": 1})])
    apply_changes(conn, schema, [ReplicaChange(op="delete", key={"id": 1})])
    apply_changes(conn, schema, [ReplicaChange(op="delete", key={"id": 99})])
    assert _read_all(conn, schema) == [{"id": 2, "v": "b"}]


def test_pk_change_deletes_before_key_then_upserts_after(conn):
    schema = simple_schema()
    create_table(conn, schema)
    write_batch(conn, schema, [row(1, "a")])
    apply_changes(conn, schema, [
        ReplicaChange(op="upsert", values=row(2, "moved"), before_key={"id": 1})])
    assert _read_all(conn, schema) == [{"id": 2, "v": "moved"}]


def test_changes_apply_in_caller_order(conn):
    schema = simple_schema()
    create_table(conn, schema)
    apply_changes(conn, schema, [
        ReplicaChange(op="upsert", values=row(1, "x")),
        ReplicaChange(op="delete", key={"id": 1})])
    assert _read_all(conn, schema) == []
    apply_changes(conn, schema, [
        ReplicaChange(op="delete", key={"id": 1}),
        ReplicaChange(op="upsert", values=row(1, "y"))])
    assert _read_all(conn, schema) == [{"id": 1, "v": "y"}]


def test_all_pk_table_upsert_do_nothing(conn):
    m = manifest([col("id", "integer", nullable=False, pk=True)], ["id"])
    schema = build_replica_schema(RUN_ID, m)
    create_table(conn, schema)
    apply_changes(conn, schema, [ReplicaChange(op="upsert", values={"id": 1})])
    apply_changes(conn, schema, [ReplicaChange(op="upsert", values={"id": 1})])
    apply_changes(conn, schema, [ReplicaChange(op="upsert", values={"id": 2})])
    assert _read_all(conn, schema) == [{"id": 1}, {"id": 2}]


def test_composite_pk_roundtrip_and_delete(conn):
    m = manifest([col("k1", "integer", nullable=False, pk=True),
                  col("k2", "string", nullable=False, pk=True),
                  col("v", "decimal", precision=10, scale=2)], ["k1", "k2"])
    schema = build_replica_schema(RUN_ID, m)
    create_table(conn, schema)
    write_batch(conn, schema, [
        {"k1": 1, "k2": "b", "v": {"$decimal": "1.10"}},
        {"k1": 1, "k2": "a", "v": {"$decimal": "2.20"}},
    ])
    # 主键序读取：k1, k2
    assert _read_all(conn, schema) == [
        {"k1": 1, "k2": "a", "v": Decimal("2.20")},
        {"k1": 1, "k2": "b", "v": Decimal("1.10")}]
    apply_changes(conn, schema, [
        ReplicaChange(op="delete", key={"k1": 1, "k2": "a"})])
    assert count_rows(conn, schema) == 1
    # 复合键必须完整：缺列/多列都拒绝
    with pytest.raises(ReplicaStoreError):
        apply_changes(conn, schema, [ReplicaChange(op="delete", key={"k1": 1})])
    with pytest.raises(ReplicaStoreError):
        apply_changes(conn, schema, [
            ReplicaChange(op="delete", key={"k1": 1, "k2": "b", "v": None})])


@pytest.mark.parametrize("change,code", [
    (ReplicaChange(op="replace", values={"id": 1, "v": "x"}), "INVALID_CHANGE"),
    (ReplicaChange(op="delete"), "INVALID_VALUE"),
    (ReplicaChange(op="delete", key={"id": 1, "v": "x"}), "INVALID_VALUE"),
    (ReplicaChange(op="upsert", values={"id": 1}), "UNKNOWN_COLUMN"),  # 缺列
    (ReplicaChange(op="upsert", values={"id": 1, "v": "x"}, before_key={"v": "x"}),
     "INVALID_VALUE"),  # before_key 必须是完整主键
])
def test_invalid_changes_rejected(conn, change, code):
    schema = simple_schema()
    with pytest.raises(ReplicaStoreError) as exc:
        apply_changes(conn, schema, [change])
    assert exc.value.code == code


# ---------------------------------------------------------------------------
# 事务边界归调用方（本模块绝不自 commit）
# ---------------------------------------------------------------------------

def test_all_writes_join_caller_transaction_no_self_commit():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        schema = simple_schema()
        # pysqlite 驱动对 DDL 隐式提交，建表先行；被测的是 DML 绝不自 commit。
        create_table(connection, schema)
        connection.commit()  # 结束 DDL 触发的 autobegin
        transaction = connection.begin()
        write_batch(connection, schema, [row(1, "a")])
        apply_changes(connection, schema, [
            ReplicaChange(op="upsert", values=row(2, "b"))])
        assert count_rows(connection, schema) == 2
        transaction.rollback()  # 调用方回滚：写入必须随之消失
        assert count_rows(connection, schema) == 0


def test_read_batch_size_validation(conn):
    schema = simple_schema()
    create_table(conn, schema)
    for bad in (0, 1001, True, "50"):
        with pytest.raises(ReplicaStoreError):
            list(read_batches(conn, schema, batch_size=bad))


# ---------------------------------------------------------------------------
# PG 侧编译面冒烟（无 PG 服务：只证 DDL/upsert SQL 形态；真实执行归主会话 gate）
# ---------------------------------------------------------------------------

class _PgConnStub:
    class dialect:  # noqa: N806 - 仅提供 dialect.name 的桩
        name = "postgresql"


def test_postgresql_typed_ddl_compiles():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    schema = build_replica_schema(RUN_ID, full_manifest())
    table = build_table(schema, "postgresql")
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    for fragment in (
            "BIGINT NOT NULL",            # 有符号整数
            "NUMERIC(20, 0)",             # 无符号 bigint → Numeric(20,0)
            "NUMERIC(12, 4)", "NUMERIC(65, 30)",
            "TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP WITH TIME ZONE",
            "TIME WITHOUT TIME ZONE", "DATE", "BYTEA", "JSON", "BOOLEAN",
            "REAL", "TEXT",
            f"CONSTRAINT {schema.table_name.replace('lake_ir_', 'pk_lake_ir_')} PRIMARY KEY"):
        assert fragment in ddl, fragment


def test_postgresql_upsert_sql_compiles_with_json_cast():
    from sqlalchemy.dialects import postgresql

    schema = build_replica_schema(RUN_ID, full_manifest())
    table = build_table(schema, "postgresql")
    statement = rs._upsert_statement(_PgConnStub(), table, schema)
    sql_text = str(statement.compile(dialect=postgresql.dialect()))
    assert "INSERT INTO" in sql_text
    assert "ON CONFLICT" in sql_text and "DO UPDATE SET" in sql_text
    assert "excluded." in sql_text
    # json 原文必须以 CAST(param AS JSON) 直达，绝不走引擎 json 序列化器
    assert sql_text.count("CAST(") == 3  # doc / doc_sql_null / doc_json_null
    assert " AS JSON)" in sql_text


def test_postgresql_all_pk_upsert_do_nothing_compiles():
    from sqlalchemy.dialects import postgresql

    m = manifest([col("id", "integer", nullable=False, pk=True)], ["id"])
    schema = build_replica_schema(RUN_ID, m)
    table = build_table(schema, "postgresql")
    sql_text = str(rs._upsert_statement(_PgConnStub(), table, schema)
                   .compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql_text and "DO NOTHING" in sql_text
