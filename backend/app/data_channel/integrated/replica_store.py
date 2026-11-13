"""当前副本 typed 物理表存储 — integrated snapshot assessment 的独立存储基础。

本模块只做存储，不接任何业务入口、不建注册表、不改模型：调用方（上层
CDC/评估编排）持有 run_id → manifest 的映射与水位，事务边界完全由调用方
控制，本模块所有写路径（DDL/DML/COPY）一律不自 commit。

身份与命名空间（与 lake_iv_/lake_ds_ 严格隔离，严禁写那些前缀）：

- 物理表名 ``lake_ir_<32hex>``，hex = ``uuid.UUID(run_id).hex``；名字永远
  由 run_id 派生，任何入口都不接收调用方表名；
- ``drop_owned`` 只收 run_id：正则归属 + 精确重建名双重验证后才 DROP，
  禁止动态任意表名删除。

Schema 契约（与 native.column_metadata 对齐）：

- manifest 形如 ``{"columns": [...], "primary_key": [...]}``，列键必须是
  column_metadata 产出的封闭集合（json 列多一个 ``encoding`` 提示键）；
- 列类型经 ``query.contracts.ColumnSpec.sql_type()``（integer+unsigned →
  ``Numeric(20,0)`` 精确 bigint unsigned；decimal → ``Numeric(p,s)`` 不隐
  式舍入）；不支持类型/非法 precision 显式拒绝，绝不静默降级；
- ``schema_fingerprint`` 沿用 ``integrated.contracts.digest(manifest)``
  （与 native.reflect / asset.schema_fingerprint 同一命名空间），传入值与
  重算不符即拒绝；
- 列名映射复用 ``datasets.lake_store.sanitize_lake_column``：源标识符可
  能超长/大小写敏感/保留词，确定性映射到该函数的 48 字节上限物理列名
  （按字节截断不切字符、截断碰撞加序号后缀，留量低于 PG 63 标识符上限），
  源名只在 Python 侧出现，物理表内以隔离列名存储。

值编解码（wire ↔ 存储）：

- 输入行是源 wire 形态（``contracts.wire_value`` 输出）：``None`` = SQL
  NULL、宽整数/十进制/二进制为 ``$integer/$decimal/$binary`` 标量、时间
  为 ISO 串、json 为 json text 串；
- 解码复用 leaf ``integrated.wire_decode._decode``（精确解码器：精度位数
  检查、时间严格性、json 校验回 JsonText）。该 leaf 不导入 service/store，
  因此不会形成 source-write 与 replica-storage 的运行时环；
- 读取返回源列名键的 native 值（Decimal/datetime/bytes/JsonText），tz
  时间统一 UTC。row_token 兼容性：SQLite 测试已证 wire 等价；**PostgreSQL
  端（COPY/json 原文/timestamptz）语义按构造成立但未经本模块测试验证，
  归主会话真实 PG gate。**

存储形态：PostgreSQL 为真 typed 列（BIGINT/NUMERIC/TIMESTAMP[TZ]/TIME/
DATE/BOOLEAN/TEXT/FLOAT/BYTEA/JSON，仅源 PK 一个约束索引）；SQLite 测试
变体把 Numeric 与 tz-DateTime、JSON 存为 exact TEXT（SQLite 的 Numeric
处理经过 float，实测 2^64-1 差 1，不可用于精确断言）。

apply_changes 语义：严格按调用方给定顺序逐条执行；upsert = ON CONFLICT(pk)
DO UPDATE（全 PK 表 DO NOTHING）；带 ``before_key`` 的 upsert 先删旧键再
upsert 新行（PK 变更）；delete 不存在为幂等 0 行。乱序防护/水位/幂等排序
由上层负责：本模块**不**承诺 upsert 本身能防乱序（后到的旧像会覆盖新像，
这是调用方的排序责任）。
"""
from __future__ import annotations

import io
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.data_channel.datasets.lake_store import sanitize_lake_column
from app.data_channel.integrated.contracts import JsonText, digest
from app.data_channel.query.contracts import ColumnSpec
from app.data_channel.integrated.wire_decode import _decode

TABLE_PREFIX = "lake_ir_"
TABLE_RE = re.compile(r"^lake_ir_[0-9a-f]{32}$")

MAX_BATCH_SIZE = 1000
# PostgreSQL NUMERIC 精度上限；超出按无效元数据拒绝。
_MAX_NUMERIC_PRECISION = 1000

# column_metadata 产出的封闭列键集合；"encoding" 仅 json 列携带。
_COLUMN_REQUIRED_KEYS = frozenset({"name", "type", "nullable"})
_COLUMN_OPTIONAL_KEYS = frozenset({
    "source_type", "precision", "scale", "timezone", "collation", "length",
    "unsigned", "primary_key", "encoding",
})

_SUPPORTED_TYPES = frozenset({
    "string", "integer", "float", "boolean", "date", "json", "binary",
    "decimal", "timestamp", "time",
})


class ReplicaStoreError(RuntimeError):
    """无效元数据/身份/值/变更的确定性拒绝（错误码封闭集合）。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def replica_table_name(run_id: str) -> str:
    """run_id → 物理表名；非法 UUID 一律拒绝，名字只从这里派生。"""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ReplicaStoreError("IDENTITY_INVALID", "replica run id must be a UUID string")
    try:
        hexed = uuid.UUID(run_id).hex
    except (ValueError, AttributeError, TypeError):
        raise ReplicaStoreError(
            "IDENTITY_INVALID", "replica run id is not a valid UUID") from None
    name = TABLE_PREFIX + hexed
    if not TABLE_RE.fullmatch(name):
        raise ReplicaStoreError("IDENTITY_INVALID", "derived replica table name is invalid")
    return name


def manifest_fingerprint(manifest: dict) -> str:
    """与 native.reflect / asset.schema_fingerprint 同一指纹算法。"""
    return digest(manifest)


@dataclass(frozen=True)
class ReplicaColumn:
    source_name: str
    physical_name: str
    spec: ColumnSpec
    # 逻辑列（源名 + 逻辑类型 + 可空性），供 wire 解码使用；与方言无关。
    decode_column: Any


@dataclass(frozen=True)
class ReplicaSchema:
    run_id: str
    table_name: str
    columns: tuple[ReplicaColumn, ...]
    primary_key: tuple[str, ...]  # 源列名，manifest 顺序
    schema_fingerprint: str

    @property
    def column_map(self) -> dict[str, str]:
        return {c.source_name: c.physical_name for c in self.columns}

    def column(self, source_name: str) -> ReplicaColumn:
        for col in self.columns:
            if col.source_name == source_name:
                return col
        raise ReplicaStoreError("UNKNOWN_COLUMN", f"unknown replica column {source_name!r}")


@dataclass(frozen=True)
class ReplicaChange:
    """一条有序变更。upsert 需全行 wire values（可选 before_key = 旧主键，
    PK 变更时先删旧键再 upsert）；delete 需精确主键 wire dict。"""

    op: str  # "upsert" | "delete"
    values: dict[str, Any] | None = None
    key: dict[str, Any] | None = None
    before_key: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 元数据校验与 schema 构建（纯函数）
# ---------------------------------------------------------------------------

def _require_key(manifest: dict, key: str) -> Any:
    if key not in manifest:
        raise ReplicaStoreError("MANIFEST_INVALID", f"manifest is missing {key!r}")
    return manifest[key]


def _validate_column_spec(entry: dict) -> ColumnSpec:
    if not isinstance(entry, dict):
        raise ReplicaStoreError("MANIFEST_INVALID", "manifest column entries must be objects")
    unknown = set(entry) - _COLUMN_REQUIRED_KEYS - _COLUMN_OPTIONAL_KEYS
    if unknown:
        raise ReplicaStoreError(
            "MANIFEST_INVALID", f"manifest column carries unknown keys {sorted(unknown)}")
    missing = _COLUMN_REQUIRED_KEYS - set(entry)
    if missing:
        raise ReplicaStoreError(
            "MANIFEST_INVALID", f"manifest column is missing keys {sorted(missing)}")
    name = entry["name"]
    kind = entry["type"]
    if not isinstance(name, str) or not name.strip():
        raise ReplicaStoreError("MANIFEST_INVALID", "column name must be a non-empty string")
    if not isinstance(kind, str) or kind not in _SUPPORTED_TYPES:
        raise ReplicaStoreError(
            "UNSUPPORTED_COLUMN_TYPE", f"column {name!r} has unsupported type {kind!r}")
    if not isinstance(entry["nullable"], bool):
        raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} nullable must be boolean")

    precision, scale = entry.get("precision"), entry.get("scale")
    for label, value in (("precision", precision), ("scale", scale)):
        if value is None or isinstance(value, bool) or not isinstance(value, int):
            if value is not None:
                raise ReplicaStoreError(
                    "MANIFEST_INVALID", f"column {name!r} {label} must be an integer or null")
    if precision is not None and not 1 <= precision <= _MAX_NUMERIC_PRECISION:
        raise ReplicaStoreError(
            "MANIFEST_INVALID", f"column {name!r} precision is outside 1..{_MAX_NUMERIC_PRECISION}")
    if scale is not None and scale < 0:
        raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} scale must be non-negative")
    if (precision is not None and scale is not None and scale > precision):
        raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} scale exceeds precision")

    timezone_flag = entry.get("timezone")
    if timezone_flag is not None and not isinstance(timezone_flag, bool):
        raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} timezone must be boolean or null")
    if kind == "time" and timezone_flag:
        # PG TIME WITH TIME ZONE 的回读随会话时区漂移，无法保证与源读
        # wire 等价；显式拒绝而不是静默给出不可比的值。
        raise ReplicaStoreError(
            "UNSUPPORTED_COLUMN_TYPE", f"column {name!r}: time with timezone is not supported")

    unsigned = entry.get("unsigned", False)
    if not isinstance(unsigned, bool):
        raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} unsigned must be boolean")
    source_type, collation = entry.get("source_type"), entry.get("collation")
    if source_type is not None and not isinstance(source_type, str):
        raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} source_type must be a string")
    if collation is not None and not isinstance(collation, str):
        raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} collation must be a string")
    encoding = entry.get("encoding")
    if kind == "json":
        if encoding is not None and encoding != "json_text":
            raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} encoding must be json_text")
    elif encoding is not None:
        raise ReplicaStoreError("MANIFEST_INVALID", f"column {name!r} carries an unexpected encoding key")

    return ColumnSpec(name=name, type=kind, nullable=entry["nullable"],
                      precision=precision, scale=scale,
                      timezone=bool(timezone_flag), source_type=source_type,
                      collation=collation, unsigned=unsigned)


def build_replica_schema(run_id: str, manifest: dict, *,
                         schema_fingerprint: str | None = None) -> ReplicaSchema:
    """校验 manifest 并构建副本 schema（纯函数，不触库）。

    ``schema_fingerprint`` 传入时必须等于 ``digest(manifest)``，否则按无效
    元数据拒绝（把副本行绑进与源读相同的 row_token 命名空间）。
    """
    if not isinstance(manifest, dict):
        raise ReplicaStoreError("MANIFEST_INVALID", "manifest must be an object")
    extra = set(manifest) - {"columns", "primary_key"}
    if extra:
        raise ReplicaStoreError(
            "MANIFEST_INVALID", f"manifest carries unknown keys {sorted(extra)}")
    table_name = replica_table_name(run_id)
    raw_columns = _require_key(manifest, "columns")
    primary_key = _require_key(manifest, "primary_key")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ReplicaStoreError("MANIFEST_INVALID", "manifest columns must be a non-empty list")
    if not isinstance(primary_key, list) or not primary_key:
        raise ReplicaStoreError("MANIFEST_INVALID", "manifest primary_key must be a non-empty list")

    specs: list[ColumnSpec] = []
    for entry in raw_columns:
        specs.append(_validate_column_spec(entry))
    names = [spec.name for spec in specs]
    if len(set(names)) != len(names):
        raise ReplicaStoreError("MANIFEST_INVALID", "manifest contains duplicate column names")
    if any(not isinstance(k, str) or not k for k in primary_key):
        raise ReplicaStoreError("MANIFEST_INVALID", "primary_key entries must be non-empty strings")
    if len(set(primary_key)) != len(primary_key):
        raise ReplicaStoreError("MANIFEST_INVALID", "primary_key contains duplicates")
    unknown_pk = [k for k in primary_key if k not in set(names)]
    if unknown_pk:
        raise ReplicaStoreError(
            "MANIFEST_INVALID", f"primary_key references unknown columns {unknown_pk}")
    flagged = {entry["name"] for entry in raw_columns if entry.get("primary_key")}
    if flagged != set(primary_key):
        raise ReplicaStoreError(
            "MANIFEST_INVALID", "column primary_key flags disagree with the primary_key list")
    by_name = {spec.name: spec for spec in specs}
    for key in primary_key:
        if by_name[key].type == "json":
            # PG json 无相等约束，不可能承载 PK；按无效元数据拒绝。
            raise ReplicaStoreError(
                "MANIFEST_INVALID", f"primary key column {key!r} cannot be json")

    if schema_fingerprint is not None and schema_fingerprint != digest(manifest):
        raise ReplicaStoreError("MANIFEST_INVALID", "schema_fingerprint does not match the manifest")

    taken: set[str] = set()
    columns = tuple(
        ReplicaColumn(
            source_name=spec.name,
            physical_name=sanitize_lake_column(spec.name, taken),
            spec=spec,
            decode_column=sa.Column(spec.name, spec.sql_type(), nullable=spec.nullable),
        )
        for spec in specs)
    return ReplicaSchema(run_id=run_id, table_name=table_name, columns=columns,
                         primary_key=tuple(primary_key),
                         schema_fingerprint=schema_fingerprint or digest(manifest))


# ---------------------------------------------------------------------------
# SQLite 测试变体类型（精确性 shim；PG 路径保持真 typed 列）
# ---------------------------------------------------------------------------

class _ExactNumeric(sa.TypeDecorator):
    """NUMERIC 存储；SQLite 存 exact 十进制文本（SQLite Numeric 经 float 有损）。"""

    impl = sa.Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(sa.Text())
        return dialect.type_descriptor(self.impl)

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name != "sqlite":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None or dialect.name != "sqlite":
            return value
        return Decimal(value)


class _ExactTzDateTime(sa.TypeDecorator):
    """TIMESTAMP WITH TIME ZONE 存储；SQLite 存带偏移 ISO 文本（原生丢 tzinfo）。"""

    impl = sa.DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(sa.Text())
        return dialect.type_descriptor(self.impl)

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name != "sqlite":
            return value
        return value.isoformat()

    def process_result_value(self, value, dialect):
        if value is None or dialect.name != "sqlite":
            return value
        return datetime.fromisoformat(value)


def build_table(schema: ReplicaSchema, dialect_name: str) -> sa.Table:
    """构建物理表对象（纯函数，不触库）。列序 = manifest 列序。"""
    if dialect_name not in {"postgresql", "sqlite"}:
        raise ReplicaStoreError(
            "DIALECT_UNSUPPORTED",
            f"replica storage supports PostgreSQL (or SQLite tests), not {dialect_name!r}")
    pk = set(schema.primary_key)
    columns: list[sa.Column] = []
    for col in schema.columns:
        logical = col.spec.sql_type()
        # Numeric 存储列按 manifest 判定（sa.Float 是 sa.Numeric 子类，
        # 不能用 isinstance 区分）：decimal 与 unsigned integer（Numeric(20,0)）。
        numeric_storage = (col.spec.type == "decimal"
                           or (col.spec.type == "integer" and col.spec.unsigned))
        if dialect_name == "sqlite" and col.spec.type == "json":
            # JSON 原文以 TEXT 存（绑定/回读都走原文，不经 json.dumps）。
            physical: sa.types.TypeEngine = sa.JSON().with_variant(sa.Text(), "sqlite")
        elif dialect_name == "sqlite" and numeric_storage:
            physical = _ExactNumeric(col.spec.precision, col.spec.scale)
        elif (dialect_name == "sqlite" and col.spec.type == "timestamp"
              and col.spec.timezone):
            physical = _ExactTzDateTime(timezone=True)
        elif (col.spec.type == "float"
              and str(col.spec.source_type or "").strip().upper()
              in {"FLOAT", "REAL"}):
            # Source column is single precision (MySQL FLOAT, PostgreSQL REAL).
            # Store it as REAL so the platform rounds the Debezium short
            # decimal to the exact float32 bits; a Float64 target would retain
            # the short decimal instead of the source value.
            physical = sa.REAL()
        else:
            physical = logical
        # autoincrement=False：副本是数据搬运，绝不能让整型主键落成
        # BIGSERIAL/SERIAL（序列 DEFAULT 会改变表身份语义）。
        columns.append(sa.Column(col.physical_name, physical,
                                 nullable=col.spec.nullable and col.source_name not in pk,
                                 autoincrement=False))
    mapping = schema.column_map
    pk_physical = [mapping[name] for name in schema.primary_key]
    return sa.Table(schema.table_name, sa.MetaData(), *columns,
                    sa.PrimaryKeyConstraint(*pk_physical, name=f"pk_{schema.table_name}"))


# ---------------------------------------------------------------------------
# DDL（调用方事务内执行，不自 commit）
# ---------------------------------------------------------------------------

def create_table(conn: sa.Connection, schema: ReplicaSchema) -> None:
    """建表；表已存在时由数据库报错（绝不静默复用既有物理表）。"""
    build_table(schema, conn.dialect.name).create(conn)


def drop_owned(conn: sa.Connection, run_id: str) -> bool:
    """删除且仅删除 run_id 派生名下的副本表；返回删除前是否存在。"""
    name = replica_table_name(run_id)
    if not TABLE_RE.fullmatch(name):  # 双重验证：派生失败时上面已拒绝
        raise ReplicaStoreError("IDENTITY_INVALID", "refusing to drop a non-replica table")
    existed = sa.inspect(conn).has_table(name)
    if existed:
        sa.Table(name, sa.MetaData()).drop(conn)
    return existed


# ---------------------------------------------------------------------------
# wire 值解码（复用 leaf integrated.wire_decode；不反向导入 service）
# ---------------------------------------------------------------------------

def _decode_wire(col: ReplicaColumn, value: Any) -> Any:
    # Numeric 列（decimal / unsigned integer）只认 $decimal 标量；宽无符号
    # 整数在 wire 上是 $integer 标量，这里解 tag 后仍走 _decode 的位数检查。
    if col.spec.type in {"integer", "decimal"} and isinstance(value, dict) \
            and set(value) == {"$integer"}:
        value = value["$integer"]
    if col.spec.type == "float" and isinstance(value, Decimal):
        # HTTP payloads parse fractional tokens as Decimal. A finite Decimal
        # that came from a source float4/float8 is the native double value;
        # non-finite input (or an overflow) stays fail-closed.
        if not value.is_finite():
            raise ReplicaStoreError(
                "INVALID_VALUE", f"column {col.source_name!r} is not a finite float")
        converted = float(value)
        if not math.isfinite(converted):
            raise ReplicaStoreError(
                "INVALID_VALUE", f"column {col.source_name!r} is not a finite float")
        value = converted
    try:
        result = _decode(col.decode_column, value)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise ReplicaStoreError(
            "INVALID_VALUE",
            f"invalid value for column {col.source_name!r}"
            + (f" ({detail.get('code')})" if detail.get("code") else "")) from exc
    if col.spec.unsigned and isinstance(result, (int, Decimal)) and result < 0:
        raise ReplicaStoreError(
            "INVALID_VALUE", f"column {col.source_name!r} is unsigned but received a negative value")
    return result


def decode_row(schema: ReplicaSchema, row: dict) -> dict[str, Any]:
    """wire 全行 → 绑定值（源列名键）。行必须携带恰好全部 manifest 列。"""
    if not isinstance(row, dict):
        raise ReplicaStoreError("INVALID_VALUE", "row must be an object keyed by source column names")
    expected = {c.source_name for c in schema.columns}
    if set(row) != expected:
        missing, unknown = sorted(expected - set(row)), sorted(set(row) - expected)
        raise ReplicaStoreError(
            "UNKNOWN_COLUMN",
            f"row must carry exactly the manifest columns (missing={missing}, unknown={unknown})")
    pk = set(schema.primary_key)
    decoded: dict[str, Any] = {}
    for col in schema.columns:
        value = row[col.source_name]
        if value is None:
            if not col.spec.nullable or col.source_name in pk:
                raise ReplicaStoreError(
                    "INVALID_VALUE", f"column {col.source_name!r} is not nullable")
            decoded[col.source_name] = None
        else:
            decoded[col.source_name] = _decode_wire(col, value)
    return decoded


def _decode_key(schema: ReplicaSchema, key: dict, *, label: str) -> dict[str, Any]:
    if not isinstance(key, dict) or set(key) != set(schema.primary_key):
        raise ReplicaStoreError(
            "INVALID_VALUE",
            f"{label} must carry exactly the primary key columns {list(schema.primary_key)}")
    return {name: _decode_wire(schema.column(name), key[name])
            for name in schema.primary_key}


# ---------------------------------------------------------------------------
# 初始装载（PG COPY；SQLite 测试走 executemany）
# ---------------------------------------------------------------------------

def _copy_text_field(value: Any, spec: ColumnSpec) -> str:
    """COPY text 格式的原文本（未转义）。bytea 用 hex 格式，bool 用 true/false。"""
    if spec.type == "boolean":
        return "true" if value else "false"
    if spec.type == "binary":
        return "\\x" + value.hex()
    return str(value)  # JsonText / Decimal / int / float / temporal / text 原文


def _copy_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r"))


def build_copy_text(schema: ReplicaSchema, rows: list[dict]) -> str:
    """解码后的绑定行 → COPY text 流（纯函数，便于对序列化本身做断言）。

    ``\\N`` 表示 SQL NULL；json 为原文（text COPY 下 NULL 与空文本可区分）。
    """
    names = [c.physical_name for c in schema.columns]
    buffer = io.StringIO()
    for row in rows:
        fields = []
        for col in schema.columns:
            value = row[col.source_name]
            fields.append("\\N" if value is None
                          else _copy_escape(_copy_text_field(value, col.spec)))
        buffer.write("\t".join(fields) + "\n")
    return buffer.getvalue()


def _copy_write(conn: sa.Connection, table: sa.Table, schema: ReplicaSchema,
                rows: list[dict]) -> None:
    payload = build_copy_text(schema, rows)
    quote = conn.dialect.identifier_preparer.quote
    names = list(table.c.keys())
    statement = (f"COPY {quote(table.name)} "
                 f"({', '.join(quote(n) for n in names)}) FROM STDIN")
    cursor = conn.connection.driver_connection.cursor()
    try:
        cursor.copy_expert(statement, io.StringIO(payload))
    finally:
        cursor.close()


def write_batch(conn: sa.Connection, schema: ReplicaSchema, rows: list[dict]) -> None:
    """初始装载一批 wire 全行（幂等性/计数/水位归上层；重复主键由 PK 约束报错）。"""
    if not isinstance(rows, list):
        raise ReplicaStoreError("INVALID_VALUE", "write_batch expects a list of rows")
    if not rows:
        return
    decoded = [decode_row(schema, row) for row in rows]
    table = build_table(schema, conn.dialect.name)
    if conn.dialect.name == "postgresql":
        _copy_write(conn, table, schema, decoded)
        return
    mapping = schema.column_map
    conn.execute(table.insert(), [
        {mapping[name]: row[name] for name in mapping} for row in decoded])


# ---------------------------------------------------------------------------
# CDC 变更应用（顺序 = 调用方给定顺序；事务归调用方）
# ---------------------------------------------------------------------------

def _delete_where(conn: sa.Connection, table: sa.Table, schema: ReplicaSchema,
                  key: dict[str, Any]) -> None:
    conditions = [table.c[schema.column_map[name]] == key[name]
                  for name in schema.primary_key]
    conn.execute(table.delete().where(sa.and_(*conditions)))


def _upsert_statement(conn: sa.Connection, table: sa.Table,
                      schema: ReplicaSchema) -> sa.Insert:
    mapping = schema.column_map
    values: dict[str, Any] = {}
    for col in schema.columns:
        physical = mapping[col.source_name]
        if col.spec.type == "json" and conn.dialect.name == "postgresql":
            # json 原文经 Text 参数显式 CAST：不经引擎 json 序列化器，原文落库。
            values[physical] = sa.cast(sa.bindparam(physical, type_=sa.Text()), sa.JSON())
        else:
            values[physical] = sa.bindparam(physical, type_=table.c[physical].type)
    pk_physical = [mapping[name] for name in schema.primary_key]
    non_pk = [mapping[c.source_name] for c in schema.columns
              if c.source_name not in set(schema.primary_key)]
    if conn.dialect.name == "postgresql":
        statement = pg_insert(table).values(**values)
    else:
        statement = sqlite_insert(table).values(**values)
    if not non_pk:
        # 全 PK 表：行身份完全由 PK 决定，无可更新列。
        return statement.on_conflict_do_nothing(index_elements=pk_physical)
    return statement.on_conflict_do_update(
        index_elements=pk_physical,
        set_={name: statement.excluded[name] for name in non_pk})


def apply_changes(conn: sa.Connection, schema: ReplicaSchema,
                  changes: list[ReplicaChange]) -> None:
    """按给定顺序应用变更；每条变更即最终语义（无缓冲重排）。

    upsert 幂等（ON CONFLICT 更新）；delete 幂等（不存在为 0 行）；
    ``before_key`` 存在时先删旧主键行再 upsert 新行（PK 变更）。
    """
    if not isinstance(changes, list):
        raise ReplicaStoreError("INVALID_CHANGE", "apply_changes expects a list of ReplicaChange")
    if not changes:
        return
    table = build_table(schema, conn.dialect.name)
    upsert = _upsert_statement(conn, table, schema)
    mapping = schema.column_map
    for change in changes:
        if not isinstance(change, ReplicaChange):
            raise ReplicaStoreError("INVALID_CHANGE", "changes must be ReplicaChange instances")
        if change.op == "delete":
            key = _decode_key(schema, change.key or {}, label="delete key")
            _delete_where(conn, table, schema, key)
        elif change.op == "upsert":
            if change.before_key is not None:
                before = _decode_key(schema, change.before_key, label="before_key")
                _delete_where(conn, table, schema, before)
            row = decode_row(schema, change.values or {})
            conn.execute(upsert, [{mapping[name]: row[name] for name in mapping}])
        else:
            raise ReplicaStoreError(
                "INVALID_CHANGE", f"unknown change op {change.op!r}")


# ---------------------------------------------------------------------------
# 读取（源列名 + native 值；row_token 兼容：SQLite 已测，PG 归主会话 gate）
# ---------------------------------------------------------------------------

def _normalize_read_value(col: ReplicaColumn, value: Any) -> Any:
    if value is None:
        return None
    if col.spec.type == "json":
        # 读取经 CAST(... AS TEXT)：拿到的是存储原文（含 JSON null 的 "null"）。
        return JsonText(value)
    if col.spec.type == "integer" and isinstance(value, Decimal):
        # unsigned bigint 存 NUMERIC(20,0)，驱动回 Decimal；wire/row_token 语义
        # 与源读一致必须是 int（$integer 标量），不是 $decimal。
        return int(value)
    if col.spec.type == "timestamp" and isinstance(value, datetime) \
            and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    if col.spec.type == "binary" and isinstance(value, (memoryview, bytearray)):
        return bytes(value)
    return value


def read_batches(conn: sa.Connection, schema: ReplicaSchema, *,
                 batch_size: int = 500) -> Iterator[list[dict]]:
    """按主键序流式读取全表，产出源列名键的 native 行（list 批）。"""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) \
            or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ReplicaStoreError(
            "INVALID_VALUE", f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    table = build_table(schema, conn.dialect.name)
    selected = []
    for col in schema.columns:
        expression = table.c[col.physical_name]
        if col.spec.type == "json":
            expression = expression.cast(sa.Text())
        selected.append(expression.label(col.source_name))
    order = [table.c[schema.column_map[name]] for name in schema.primary_key]
    statement = sa.select(*selected).order_by(*order)
    executor = (conn.execution_options(stream_results=True, yield_per=1)
                if conn.dialect.name == "postgresql" else conn)
    result = executor.execute(statement)
    try:
        cursor = result.mappings()
        while True:
            chunk = cursor.fetchmany(batch_size)
            if not chunk:
                break
            yield [
                {col.source_name: _normalize_read_value(col, row[col.source_name])
                 for col in schema.columns}
                for row in chunk]
    finally:
        result.close()


def count_rows(conn: sa.Connection, schema: ReplicaSchema) -> int:
    table = build_table(schema, conn.dialect.name)
    return int(conn.execute(
        sa.select(sa.func.count()).select_from(table)).scalar_one())
