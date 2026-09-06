"""连接器物理类型与原生值 → 资产湖归一化契约。

湖列词表是 lake_gate 的六值词表（string/integer/float/boolean/timestamp/
json）；MySQL/PostgreSQL 等方言的物理类型差异必须在连接器层收窄，本体
映射侧只与湖词表对话（request_validation._normal_mapping_type 已含湖词表
到语义层词表的别名桥）。未识别类型降级 string + unmapped 标志，绝不阻断
入湖（与 lake_gate「类型推断永不阻断入湖」同一哲学）。

元数据持久化采用「瘦 columns_typed + 平行 source_schema 键」：columns_typed
只写 {name, type}，源物理类型与损失标志放 schema_json["source_schema"]。
persist_contract / normalize_definitions 按白名单重建 columns_typed，直接
在其中加字段会被剥掉；平行键不经过任何重建路径。
"""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import uuid as uuid_module

# 基础类型名（lower + 去括号参数 + 去数组标记后）→ (湖类型, 标志)
_SQL_TYPE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    # —— 整数族（MySQL + PostgreSQL）——
    "tinyint": ("integer", ()),
    "smallint": ("integer", ()),
    "mediumint": ("integer", ()),
    "int": ("integer", ()),
    "integer": ("integer", ()),
    "bigint": ("integer", ()),
    "int2": ("integer", ()),
    "int4": ("integer", ()),
    "int8": ("integer", ()),
    "serial": ("integer", ()),
    "smallserial": ("integer", ()),
    "bigserial": ("integer", ()),
    "year": ("integer", ()),
    "bit": ("integer", ("bit",)),
    # —— 数值：湖词表没有 decimal，定点/金额转 float 是有损的 ——
    "float": ("float", ()),
    "float4": ("float", ()),
    "float8": ("float", ()),
    "double": ("float", ()),
    "double precision": ("float", ()),
    "real": ("float", ()),
    "decimal": ("float", ("lossy",)),
    "numeric": ("float", ("lossy",)),
    "money": ("float", ("lossy", "money")),
    # —— 布尔 ——
    "boolean": ("boolean", ()),
    "bool": ("boolean", ()),
    # —— 文本族 ——
    "char": ("string", ()),
    "character": ("string", ()),
    "character varying": ("string", ()),
    "varchar": ("string", ()),
    "nchar": ("string", ()),
    "nvarchar": ("string", ()),
    "text": ("string", ()),
    "tinytext": ("string", ()),
    "mediumtext": ("string", ()),
    "longtext": ("string", ()),
    "string": ("string", ()),
    "name": ("string", ()),
    "citext": ("string", ()),
    "uuid": ("string", ()),
    "enum": ("string", ("enum",)),
    "set": ("string", ("set",)),
    "inet": ("string", ()),
    "cidr": ("string", ()),
    "macaddr": ("string", ()),
    "xml": ("string", ()),
    # —— 时间族：湖词表只有 timestamp；纯时刻/时长没有湖类型 ——
    "date": ("timestamp", ("date_only",)),
    "datetime": ("timestamp", ()),
    "timestamp": ("timestamp", ()),
    "timestamp with time zone": ("timestamp", ("tz_aware",)),
    "timestamptz": ("timestamp", ("tz_aware",)),
    "timestamp without time zone": ("timestamp", ()),
    "time": ("string", ("time_of_day",)),
    "time with time zone": ("string", ("time_of_day",)),
    "timetz": ("string", ("time_of_day",)),
    "interval": ("string", ("duration",)),
    # —— 结构化 ——
    "json": ("json", ()),
    "jsonb": ("json", ()),
    # —— 二进制：湖契约是文本列，二进制值 base64 后入湖 ——
    "binary": ("string", ("binary",)),
    "varbinary": ("string", ("binary",)),
    "blob": ("string", ("binary",)),
    "tinyblob": ("string", ("binary",)),
    "mediumblob": ("string", ("binary",)),
    "longblob": ("string", ("binary",)),
    "bytea": ("string", ("binary",)),
    # —— 空间 ——
    "geometry": ("string", ("spatial",)),
    "point": ("string", ("spatial",)),
    "linestring": ("string", ("spatial",)),
    "polygon": ("string", ("spatial",)),
    "multipoint": ("string", ("spatial",)),
    "geography": ("string", ("spatial",)),
}


def _base_name(raw_type: object) -> str:
    """方言类型名 → 基础名：lower、去括号参数、压空白、去数组/限定词标记。"""
    name = " ".join(str(raw_type or "").split()).lower()
    name = name.split("(", 1)[0].strip()
    name = " ".join(name.split())
    for qualifier in ("unsigned", "signed", "zerofill"):
        if name.endswith(f" {qualifier}"):
            name = name[: -len(qualifier) - 1].strip()
    if name.endswith("[]"):  # PG 反射的 integer[] 记法
        name = name[:-2].strip()
    if name.startswith("_") and len(name) > 1:  # PG 反射的 _integer 记法
        name = name[1:]
    return name


def _python_lake_type(raw_type: object) -> tuple[str | None, tuple[str, ...]]:
    """SQLAlchemy TypeEngine.python_type 兜底：方言自定义类型常有精确的 Python 映射。"""
    try:
        py = getattr(raw_type, "python_type", None)
        if py is None:
            return None, ()
        if issubclass(py, bool):
            return "boolean", ()
        if issubclass(py, int):
            return "integer", ()
        if issubclass(py, float):
            return "float", ()
        if issubclass(py, decimal.Decimal):
            return "float", ("lossy",)
        if issubclass(py, (dt.datetime, dt.date)):
            return "timestamp", ()
        if issubclass(py, dt.time):
            return "string", ("time_of_day",)
        if issubclass(py, (bytes, bytearray)):
            return "string", ("binary",)
        if issubclass(py, (dict, list)):
            return "json", ()
        if issubclass(py, str):
            return "string", ()
    except (NotImplementedError, TypeError):
        return None, ()
    return None, ()


def map_sql_type(raw_type: object) -> tuple[str, tuple[str, ...]]:
    """方言物理类型 → (湖归一化类型, 标志)。未识别降级 string + unmapped。

    SQL 反射返回的是 TypeEngine 对象，str() 会丢构造参数（PG
    TIMESTAMP(timezone=True) → 'TIMESTAMP'、MySQL TINYINT(display_width=1)
    → 'TINYINT'），方言标志必须先从对象属性探测，字符串形态只作为
    显式传入类型名时的补充。
    """
    raw_text = " ".join(str(raw_type or "").split())
    lowered = raw_text.lower()
    name = _base_name(raw_text)

    flags: list[str] = []
    if getattr(raw_type, "timezone", False):
        flags.append("tz_aware")
    if name == "tinyint" and getattr(raw_type, "display_width", None) == 1:
        flags.append("mysql_bool_alias")
    if lowered.startswith("tinyint") and "(1)" in lowered.replace(" ", ""):
        flags.append("mysql_bool_alias")

    is_array = (
        lowered.endswith("[]")
        or lowered.startswith("_")
        # SQLAlchemy ARRAY 的鸭子特征（避免本模块直接依赖 sqlalchemy）
        or getattr(raw_type, "item_type", None) is not None
    )

    mapped = _SQL_TYPE_MAP.get(name)
    if mapped is not None:
        lake_type, table_flags = mapped
    else:
        lake_type, table_flags = _python_lake_type(raw_type)
        if lake_type is None:
            lake_type, table_flags = "string", ("unmapped",)

    all_flags = tuple(dict.fromkeys(flags + list(table_flags)))
    if "mysql_bool_alias" in all_flags:
        # MySQL 事实布尔：TINYINT(1) 是 BOOL/BOOLEAN 的官方别名
        return "boolean", all_flags
    if is_array:
        # 数组容器统一落 json（湖契约的 json 文本可无损承载任意数组），
        # 并总是带 array 标志——python_type 兜底也返回 json，不能因此丢
        # 标志。元素类型（NUMERIC[] 的 lossy 等）经递归映射合并进容器标志。
        item = getattr(raw_type, "item_type", None)
        element_flags: tuple[str, ...] = ()
        if item is not None:
            _, element_flags = map_sql_type(item)
        return "json", tuple(dict.fromkeys(
            list(all_flags) + list(element_flags) + ["array"]))
    return lake_type, all_flags


def normalize_sql_column(name: object, raw_type: object) -> dict:
    """一列的完整归一化结果：湖类型 + 源类型 + 方言标志。"""
    lake_type, flags = map_sql_type(raw_type)
    return {
        "name": str(name or ""),
        "type": lake_type,
        "source_type": " ".join(str(raw_type or "").split())[:200],
        "flags": list(flags),
    }


def introspected_columns_typed(columns: list[dict]) -> tuple[list[dict], dict]:
    """连接器内省契约 → (瘦 columns_typed, 平行 source_schema 键)。

    输入是 ConnectorBase.introspect_schema 的返回（每列 {name, type,
    source_type, flags}，方言归一化已在连接器内经 normalize_sql_column
    完成）；此处只做形状拆分，不做二次类型归一——湖词表的 type 再过一遍
    map_sql_type 会把源类型与方言标志洗掉。
    """
    typed: list[dict] = []
    source_schema: dict[str, dict] = {}
    for column in columns or []:
        name = str(column.get("name") or "")
        if not name:
            continue
        typed.append({"name": name, "type": str(column.get("type") or "string")})
        source_schema[name] = {
            "source_type": str(column.get("source_type") or ""),
            "flags": [str(flag) for flag in (column.get("flags") or [])],
        }
    return typed, source_schema


def normalize_cell(value):
    """数据库原生值 → 湖契约可存储的 JSON 标量/容器。

    datetime/Decimal/bytes 等对象不归一化时 json.dumps 直接 TypeError，
    含日期/金额/二进制列的表曾因此整次同步失败。datetime 用 isoformat
    （与 lake_gate._DATE_RE / SchemaInferenceStep 认定的 timestamp 形态
    一致），Decimal 保留字符串原文（不丢精度，采样期再按 float 解析），
    bytes 转 base64 文本（列级 binary 标志见 map_sql_type）。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dt.datetime):  # 必须在 date 之前判断（子类关系）
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, uuid_module.UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: normalize_cell(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_cell(item) for item in value]
    return str(value)


def normalize_rows(rows: list, primary_key_columns: tuple | list = ()) -> list:
    """整表归一化；主键列的值额外文本化（见 normalize_primary_key_value）。"""
    pk_columns = [str(column) for column in (primary_key_columns or ())]
    out = []
    for row in rows:
        normalized = normalize_cell(row)
        if pk_columns and isinstance(normalized, dict):
            for column in pk_columns:
                if column in normalized:
                    normalized[column] = normalize_primary_key_value(
                        normalized[column])
        out.append(normalized)
    return out


def normalize_primary_key_value(value):
    """主键列值 → 文本身份。

    数值型 ID 统一转字符串：跨源 join 需要文本口径（Mongo _id 是文本、
    MySQL id 是整数，类型不同永不相交），投影侧 2^53 之上也不会再受
    float64 表示影响；与 Foundry 等本体检平台「数值 ID 先 cast string
    再做主键」的通行做法一致。None 表示业务空值，交由非空校验拦截。
    """
    normalized = normalize_cell(value)
    if normalized is None:
        return None
    if not isinstance(normalized, str):
        return str(normalized)
    return normalized
