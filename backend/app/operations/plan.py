"""运营报告结构化计划（plan v1）：normalize → compile → 渲染 markdown。

计划是文章正文的另一种权威形态：``OperationsReportArticle.plan_json`` 保存
normalize 后的规范字典，``markdown`` 由计划确定性生成（旧 markdown 文章的
``plan_json`` 为 NULL，读写路径完全不变）。

安全模型（与 report_sql 的 SQL 安全门同向 fail-closed）：

- **结构严格**：Pydantic ``extra="forbid"`` 拒绝任意 option/未知键；块类型
  只有 text/query/chart，筛选类型只有 text/number/date，聚合、时间桶、
  排序方向、limit 全部白名单且有界（列数、排序键数、块数、文本长度）。
- **字段白名单**：字段必须命中数据集 schema 声明的列名，且类型与用途对齐
  （sum/avg 只允许数值列、时间桶只允许时间列、筛选类型对齐列类型）。
- **无原始 SQL/HTML/JS**：text 块是纯文本，拒绝围栏标记（防止把生成的
  markdown 结构注入成 sql/chart/filter 围栏）与 HTML 标签；查询 SQL 全部由
  服务端从白名单标识符拼接（双引号引用），不接受任何用户 SQL 片段。源字段
  名可以是非 ASCII（真实中文列名），但含换行/控制字符或围栏标记序列的字段
  直接拒绝（字段名会进入生成的 markdown 围栏文本，必须结构性安全）。
- **输出别名与结果有界**：输出别名（含时间桶别名）由服务端生成，保证是
  ``[A-Za-z_][A-Za-z0-9_]*`` 且不超过 PostgreSQL ``NAMEDATALEN-1``（63
  字节）；源字段名不满足该形状时自动派生 ``col_N`` 等稳定合法别名。
  ``count_distinct`` 编译为 ``COUNT(DISTINCT col)``；每条查询都带 ``LIMIT``
  （未显式声明时默认 ``QUERY_DEFAULT_LIMIT``，与文章阅读 execute 的默认
  上限一致）。number/date 筛选的比较侧带 ``CAST``（NUMERIC/DATE），使
  PostgreSQL 物化 TEXT 列与 SQLite 测试库的语义一致。
- **复用既有执行门**：编译产物仍走 ``check_report_sql``（AST 校验）与
  ``execute_report_queries``（物化 + 只读事务 + 预算），本模块不新增任何
  SQL 执行路径。

已知边界（phase 1）：时间桶依赖 PostgreSQL ``DATE_TRUNC``（经 sqlglot 按
执行方言生成）；SQLite 测试库没有对应函数，带时间桶的计划只能做保存期
校验，不能在 SQLite 上试跑执行。筛选语义统一为「参数为 NULL 时不过滤、
否则等值匹配」。
"""
from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# 计划契约版本；v2 起需新增版本分支并保留 v1 读取兼容。
PLAN_VERSION = 1

# 与 markdown.py 的 SQL/筛选命名契约一致：围栏名允许中划线，参数名只允许
# 下划线（check_report_sql 的参数正则同为 [A-Za-z_][A-Za-z0-9_]{0,63}）。
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,80}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# text 块拒绝任何行首围栏标记，防止计划文本把自身注入成 sql/chart/filter 围栏。
_FENCE_LINE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})", re.MULTILINE)
# 近似拒绝完整 HTML/HTML 注释标签（<b>、</div>、<!-- -->）；"a<b" 这类无
# 闭口的普通文本不受影响。
_HTML_TAG_RE = re.compile(r"<[A-Za-z!/][^>]*>")
# 源字段名进入编译 SQL 再进入生成的 markdown 围栏文本：控制字符（换行可以
# 制造行首围栏闭合）与 ```/~~~ 序列必须整段拒绝；中文等非 ASCII 字段不受影响。
_UNSAFE_FIELD_RE = re.compile(r"[\x00-\x1f\x7f]|`{3}|~{3}")

TEXT_BLOCK_MAX_CHARS = 20_000
QUERY_COLUMNS_MAX = 20
QUERY_BLOCKS_MAX = 20
SORT_KEYS_MAX = 3
FILTERS_MAX = 8
BLOCKS_MAX = 50
CHART_SERIES_MAX = 4
# 文章阅读器的既有 execute 默认上限是 200；v1 计划保持同一上限，
# 避免保存后生成的 Markdown 在阅读态因请求级 limit 更小而假失败。
QUERY_LIMIT_MAX = 200
# 未显式声明 limit 的查询块编译时写入的默认 LIMIT（与阅读 execute 默认一致）。
QUERY_DEFAULT_LIMIT = 200
# PostgreSQL NAMEDATALEN - 1：服务端生成的输出别名（含时间桶别名）的字节
# 上限，超限会被服务端静默截断导致引用错位。
ALIAS_MAX_BYTES = 63

CHART_KINDS = ("kpi", "bar", "line", "pie", "table")
FILTER_TYPES = ("text", "number", "date")
AGGREGATIONS = ("count", "count_distinct", "sum", "avg", "min", "max")
BUCKET_UNITS = ("minute", "hour", "day", "week", "month")

_NUMERIC_TYPES = {"integer", "float", "number", "decimal"}
_TEMPORAL_TYPES = {"date", "timestamp", "datetime"}
_SCALAR_TYPES = _NUMERIC_TYPES | _TEMPORAL_TYPES | {"string", "boolean"}


def _normalized_type(value: object) -> str:
    """把资产湖 schema 的同义类型收敛到报告计划类型。"""
    raw = str(value or "string").strip().lower()
    return {
        "numeric": "number",
        "double": "float",
        "double precision": "float",
        "int": "integer",
        "bigint": "integer",
        "datetime64": "timestamp",
    }.get(raw, raw)
_FILTER_FIELD_TYPES = {
    "text": {"string"},
    "number": _NUMERIC_TYPES,
    "date": _TEMPORAL_TYPES,
}
AggName = Literal["count", "count_distinct", "sum", "avg", "min", "max"]


class PlanValidationError(ValueError):
    """计划未通过规范化校验；errors 为面向用户的中文问题列表。"""

    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors))
        self.errors = list(errors)


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanFilter(_PlanModel):
    name: str = Field(min_length=1, max_length=64)
    type: Literal["text", "number", "date"]
    field: str | None = Field(default=None, min_length=1, max_length=64)


class PlanTextBlock(_PlanModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=TEXT_BLOCK_MAX_CHARS)


class PlanQueryColumn(_PlanModel):
    field: str | None = Field(default=None, min_length=1, max_length=64)
    agg: AggName | None = None
    alias: str | None = Field(default=None, min_length=1, max_length=64)


class PlanTimeBucket(_PlanModel):
    field: str = Field(min_length=1, max_length=64)
    unit: Literal["minute", "hour", "day", "week", "month"]
    # 输出列名；缺省由 normalize 派生（field__unit 或稳定自动别名）并回填，
    # plan_json 的存储形态因此可以继续经 ReportPlan.model_validate 回读。
    alias: str | None = Field(default=None, min_length=1, max_length=64)


class PlanSortKey(_PlanModel):
    field: str = Field(min_length=1, max_length=64)
    dir: Literal["asc", "desc"] = "asc"


class PlanQueryBlock(_PlanModel):
    type: Literal["query"]
    name: str = Field(min_length=1, max_length=128)
    columns: list[PlanQueryColumn] = Field(min_length=1, max_length=QUERY_COLUMNS_MAX)
    time_bucket: PlanTimeBucket | None = None
    sort: list[PlanSortKey] = Field(default_factory=list, max_length=SORT_KEYS_MAX)
    limit: int | None = Field(default=None, ge=1, le=QUERY_LIMIT_MAX)


class PlanChartBlock(_PlanModel):
    type: Literal["chart"]
    chart: Literal["kpi", "bar", "line", "pie", "table"]
    query: str = Field(min_length=1, max_length=128)
    x: str | None = Field(default=None, min_length=1, max_length=64)
    y: list[str] = Field(default_factory=list, max_length=CHART_SERIES_MAX)


class ReportPlan(_PlanModel):
    """HTTP 层的计划 DTO：先做结构校验（extra 键即拒绝），字段白名单在
    normalize_plan 里结合数据集 schema 完成。"""

    version: Literal[1]
    filters: list[PlanFilter] = Field(default_factory=list, max_length=FILTERS_MAX)
    blocks: list[Annotated[
        PlanTextBlock | PlanQueryBlock | PlanChartBlock,
        Field(discriminator="type"),
    ]] = Field(min_length=1, max_length=BLOCKS_MAX)


def dataset_field_types(dataset) -> dict[str, str]:
    """数据集 schema 声明的「列名 → 类型」白名单（与 report_sql._version_meta
    同源：columns_typed 优先，回退 columns 全 string）。不触发任何数据扫描。"""
    schema = dict(getattr(dataset, "schema_json", None) or {})
    typed = [item for item in (schema.get("columns_typed") or []) if isinstance(item, dict)]
    if typed:
        return {str(item["name"]): _normalized_type(item.get("type"))
                for item in typed if item.get("name")}
    columns = schema.get("columns") or []
    return {
        (item if isinstance(item, str) else str(item.get("name"))):
        (_normalized_type(item.get("type")) if isinstance(item, dict) else "string")
        for item in columns
        if (isinstance(item, str) and item) or (isinstance(item, dict) and item.get("name"))
    }


def field_types_from_columns(columns: list[dict]) -> dict[str, str]:
    """``[{"name": ..., "type": ...}]``（如 get_schema 的推导结果）→ 计划字段
    白名单；schema 未声明列的历史 CSV 数据集经此与计划编辑器共用同一字段口径。"""
    return {
        str(item["name"]): _normalized_type(item.get("type"))
        for item in columns or []
        if isinstance(item, dict) and item.get("name")
    }


def _column_type(field_types: dict[str, str], field: str, errors: list[str],
                 where: str) -> str | None:
    if _UNSAFE_FIELD_RE.search(field):
        errors.append(f"{where}：字段 {field!r} 含换行/控制字符或围栏标记，不能用于报告计划")
        return None
    field_type = field_types.get(field)
    if field_type is None:
        errors.append(f"{where}：字段 {field!r} 不在数据集字段白名单内")
        return None
    return field_type


def _legal_alias(name: str) -> bool:
    """输出别名形状：ASCII 短标识符且不超过 PostgreSQL 63 字节上限。"""
    return bool(_IDENT_RE.fullmatch(name)) and len(name.encode("utf-8")) <= ALIAS_MAX_BYTES


def validate_plan_params(filters: list[dict], params: dict) -> list[str]:
    """筛选参数与筛选器声明类型的对齐检查（NULL 表示不过滤，恒放行）。

    在编译/执行前拦截类型错配，避免落到数据库层才是晦涩的 22xxx/42883
    错误（例如 text 筛选收到数字、number 筛选收到字符串）。
    """
    declared = {item["name"]: item["type"] for item in filters or []}
    errors: list[str] = []
    for name, value in (params or {}).items():
        filter_type = declared.get(name)
        if filter_type is None or value is None:
            continue
        if filter_type == "text" and not isinstance(value, str):
            errors.append(f"筛选参数 {name} 必须是字符串（text 筛选器）")
        elif filter_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"筛选参数 {name} 必须是数字（number 筛选器）")
            elif isinstance(value, float) and not math.isfinite(value):
                errors.append(f"筛选参数 {name} 必须是有限数字")
        elif filter_type == "date" and (not isinstance(value, str) or not value.strip()):
            errors.append(f"筛选参数 {name} 必须是非空日期字符串（date 筛选器）")
    return errors


def normalize_plan(plan: ReportPlan, *, field_types: dict[str, str]) -> dict:
    """结构校验后的计划 + 数据集字段白名单 → 规范化字典（plan_json 的存储形态）。

    任一规则不满足即抛 PlanValidationError（收集全部问题），绝不部分放行。
    """
    errors: list[str] = []

    filters: list[dict] = []
    filter_names: set[str] = set()
    for item in plan.filters:
        name = item.name.strip()
        if not _IDENT_RE.fullmatch(name):
            errors.append(f"筛选器 name {item.name!r} 必须是字母/下划线开头的短标识符")
            continue
        if name in filter_names:
            errors.append(f"筛选器重复：{name}")
            continue
        field = (item.field or name).strip()
        field_type = _column_type(field_types, field, errors, f"筛选器 {name}")
        if field_type is not None and field_type not in _FILTER_FIELD_TYPES[item.type]:
            errors.append(
                f"筛选器 {name} 类型 {item.type} 与字段 {field} 的类型 "
                f"{field_type} 不兼容")
            continue
        filter_names.add(name)
        filters.append({"name": name, "type": item.type, "field": field})

    blocks: list[dict] = []
    query_names: set[str] = set()
    query_outputs: dict[str, set[str]] = {}
    query_block_count = 0
    # chart 块引用的查询可能声明在其后：先占位保持块顺序，全部查询收集完
    # 再统一校验并回填规范形态。
    pending_charts: list[tuple[int, str, PlanChartBlock]] = []
    for index, block in enumerate(plan.blocks):
        where = f"块 {index + 1}"
        if isinstance(block, PlanTextBlock):
            text = block.text
            if _FENCE_LINE_RE.search(text):
                errors.append(f"{where}：text 块不能包含围栏标记（``` 或 ~~~）")
            if _HTML_TAG_RE.search(text):
                errors.append(f"{where}：text 块不能包含 HTML 标签")
            if not text.strip():
                errors.append(f"{where}：text 块不能为空白")
            blocks.append({"type": "text", "text": text})
        elif isinstance(block, PlanQueryBlock):
            query_block_count += 1
            if query_block_count > QUERY_BLOCKS_MAX:
                errors.append(f"{where}：查询块数量不能超过 {QUERY_BLOCKS_MAX} 个")
            name = block.name.strip()
            if not _NAME_RE.fullmatch(name):
                errors.append(f"{where}：查询名 {block.name!r} 必须是字母开头的短名称")
                continue
            if name in query_names:
                errors.append(f"{where}：查询名重复：{name}")
                continue
            outputs: list[str] = []
            output_set: set[str] = set()
            columns: list[dict] = []
            auto_seq = 0

            def _auto_alias(prefix: str) -> str:
                # 按块内位置派生稳定别名：同一计划每次规范化结果一致。
                nonlocal auto_seq
                while True:
                    auto_seq += 1
                    candidate = f"{prefix}{auto_seq}"
                    if candidate not in output_set:
                        return candidate

            def _resolved_alias(explicit: str | None, default: str, prefix: str,
                                cwhere: str) -> str:
                """显式别名合法即用；缺省用默认候选；默认候选不是合法输出
                别名（中文源字段、超 63 字节等）时派生稳定自动别名。
                空串表示显式别名非法（错误已收集）。"""
                alias = (explicit or "").strip()
                if alias:
                    if _legal_alias(alias):
                        return alias
                    errors.append(
                        f"{cwhere}：别名 {explicit!r} 必须是字母/下划线开头的"
                        f"短标识符且不超过 {ALIAS_MAX_BYTES} 字节")
                    return ""
                if _legal_alias(default):
                    return default
                return _auto_alias(prefix)

            for column in block.columns:
                cwhere = f"{where} 查询 {name}"
                agg = column.agg
                if agg is None:
                    field = (column.field or "").strip()
                    if not field:
                        errors.append(f"{cwhere}：普通列必须指定 field")
                        continue
                    if _column_type(field_types, field, errors, cwhere) is None:
                        continue
                    alias = _resolved_alias(column.alias, field, "col_", cwhere)
                    if not alias:
                        continue
                    entry = (field, None, alias)
                else:
                    field = (column.field or "").strip() or None
                    if agg != "count" and field is None:
                        errors.append(f"{cwhere}：聚合 {agg} 必须指定 field")
                        continue
                    field_type: str | None = "any"
                    if field is not None:
                        field_type = _column_type(field_types, field, errors, cwhere)
                        if field_type is not None:
                            if agg in {"sum", "avg"} and field_type not in _NUMERIC_TYPES:
                                errors.append(
                                    f"{cwhere}：聚合 {agg} 只支持数值列，"
                                    f"字段 {field} 是 {field_type}")
                                field_type = None
                            elif agg in {"min", "max"} and field_type not in _SCALAR_TYPES:
                                errors.append(
                                    f"{cwhere}：聚合 {agg} 不支持 {field_type} 列")
                                field_type = None
                    if field_type is None:
                        continue
                    alias = _resolved_alias(
                        column.alias,
                        "count" if field is None else f"{agg}_{field}",
                        f"{agg}_", cwhere)
                    if not alias:
                        continue
                    entry = (field, agg, alias)
                if alias in output_set:
                    errors.append(f"{cwhere}：输出列重复：{alias}")
                    continue
                output_set.add(alias)
                outputs.append(alias)
                columns.append({"field": entry[0], "agg": entry[1], "alias": entry[2]})

            bucket: dict | None = None
            if block.time_bucket is not None:
                bwhere = f"{where} 时间桶"
                bfield = block.time_bucket.field.strip()
                btype = _column_type(field_types, bfield, errors, bwhere)
                if btype is not None:
                    if btype not in _TEMPORAL_TYPES:
                        errors.append(
                            f"{where}：时间桶字段 {bfield} 必须是 date/timestamp 列"
                            f"（实际 {btype}）")
                    else:
                        alias = _resolved_alias(
                            block.time_bucket.alias,
                            f"{bfield}__{block.time_bucket.unit}",
                            "bucket_", bwhere)
                        if alias:
                            bucket = {
                                "field": bfield,
                                "unit": block.time_bucket.unit,
                                "alias": alias,
                            }
                            if alias in output_set:
                                errors.append(f"{where}：时间桶输出列重复：{alias}")
                            else:
                                output_set.add(alias)
                                outputs.append(alias)

            sort_keys: list[dict] = []
            sort_seen: set[str] = set()
            for key in block.sort:
                target = key.field.strip()
                if target not in output_set:
                    errors.append(
                        f"{where}：排序键 {target!r} 必须引用查询输出列"
                        f"（{', '.join(outputs) or '无'}）")
                    continue
                if target in sort_seen:
                    errors.append(f"{where}：排序键重复：{target}")
                    continue
                sort_seen.add(target)
                sort_keys.append({"field": target, "dir": key.dir})

            query_names.add(name)
            query_outputs[name] = output_set
            blocks.append({
                "type": "query", "name": name, "columns": columns,
                "time_bucket": bucket, "sort": sort_keys, "limit": block.limit,
            })
        else:
            pending_charts.append((len(blocks), where, block))

    # chart 块引用校验放在两遍之后：查询块可能声明在 chart 之后。
    for position, where, block in reversed(pending_charts):
        outputs = query_outputs.get(block.query.strip())
        if outputs is None:
            errors.append(f"{where}：图表引用了不存在的查询：{block.query!r}")
            continue
        chart = block.chart
        x, y = block.x, list(block.y)
        if chart in {"kpi", "table"} and x is not None:
            errors.append(f"{where}：{chart} 图表不支持 x")
        if chart in {"bar", "line", "pie"} and x is None:
            errors.append(f"{where}：{chart} 图表需要 x 列")
        if chart in {"kpi", "pie"} and len(y) != 1:
            errors.append(f"{where}：{chart} 图表需要恰好一个 y 列")
        if chart in {"bar", "line"} and not y:
            errors.append(f"{where}：{chart} 图表至少需要一个 y 列")
        if chart == "table" and y:
            errors.append(f"{where}：table 图表不支持 y")
        for target in ([x] if x else []) + y:
            if target not in outputs:
                errors.append(
                    f"{where}：图表列 {target!r} 不是查询 {block.query!r} 的输出列")
        blocks.insert(position, {
            "type": "chart", "chart": chart, "query": block.query.strip(),
            "x": x, "y": y,
        })

    if errors:
        raise PlanValidationError(errors)
    return {"version": PLAN_VERSION, "filters": filters, "blocks": blocks}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# number/date 筛选的比较侧 CAST：PostgreSQL 物化临时表是 TEXT 列（类型化在
# 子查询投影里），SQLite 测试库不做投影替换；显式 CAST 让两种方言的等值
# 语义一致，也避免参数为 NULL 时依赖驱动端的类型推断。
_FILTER_PARAM_CASTS = {"text": None, "number": "NUMERIC", "date": "DATE"}


def compile_query_sql(
    block: dict,
    filters: list[dict],
    *,
    result_limit: int | None = None,
) -> str:
    """规范化的 query 块 → 受控命名 SQL（再经 check_report_sql 执行门）。

    ``result_limit`` 是一次预览/执行的共同结果预算。每条查询总是带 LIMIT：
    未显式声明 limit 的块默认 ``QUERY_DEFAULT_LIMIT``，并与请求级预算取小，
    否则执行器会在读到预算外的第一行时把一个本可执行的查询误报为超预算。
    """
    projections: list[str] = []
    group_exprs: list[str] = []
    has_aggregate = False
    for column in block["columns"]:
        field, agg, alias = column["field"], column["agg"], column["alias"]
        if agg is None:
            projections.append(f"{_quote(field)} AS {_quote(alias)}")
            group_exprs.append(_quote(field))
            continue
        has_aggregate = True
        if agg == "count":
            inner = "*" if field is None else _quote(field)
        elif agg == "count_distinct":
            inner = f"DISTINCT {_quote(field)}"
        else:
            inner = _quote(field)
        sql_func = "COUNT" if agg == "count_distinct" else agg.upper()
        projections.append(f"{sql_func}({inner}) AS {_quote(alias)}")
    bucket = block.get("time_bucket")
    if bucket is not None:
        has_aggregate = True
        expression = f"DATE_TRUNC('{bucket['unit']}', {_quote(bucket['field'])})"
        projections.append(f"{expression} AS {_quote(bucket['alias'])}")
        group_exprs.append(expression)

    lines = ["SELECT " + ", ".join(projections), "FROM dataset"]
    if filters:
        conditions = []
        for item in filters:
            field_expr = _quote(item["field"])
            cast = _FILTER_PARAM_CASTS.get(item["type"])
            if cast is not None:
                field_expr = f"CAST({field_expr} AS {cast})"
            conditions.append(
                f"(:{item['name']} IS NULL OR {field_expr} = :{item['name']})")
        lines.append("WHERE " + " AND ".join(conditions))
    if has_aggregate and group_exprs:
        lines.append("GROUP BY " + ", ".join(group_exprs))
    if block["sort"]:
        lines.append("ORDER BY " + ", ".join(
            f"{_quote(key['field'])} {'ASC' if key['dir'] == 'asc' else 'DESC'}"
            for key in block["sort"]))
    block_limit = block.get("limit")
    cap = int(result_limit) if result_limit is not None else QUERY_DEFAULT_LIMIT
    requested = int(block_limit) if block_limit is not None else QUERY_DEFAULT_LIMIT
    lines.append(f"LIMIT {min(requested, cap)}")
    return "\n".join(lines)


def compile_plan_queries(plan: dict, *, result_limit: int | None = None) -> dict[str, str]:
    """规范化计划 → {查询名: SQL}；只读调用方（预览/生成 markdown）使用。"""
    filters = plan.get("filters") or []
    queries: dict[str, str] = {}
    for block in plan.get("blocks") or []:
        if block.get("type") == "query":
            queries[str(block["name"])] = compile_query_sql(
                block, filters, result_limit=result_limit)
    return queries


def plan_markdown(plan: dict) -> str:
    """规范化计划 → 与旧 markdown 契约兼容的文章正文（同名围栏可被
    markdown.validate_markdown 与既有执行路径原样消费）。"""
    chunks: list[str] = []
    for block in plan.get("blocks") or []:
        kind = block.get("type")
        if kind == "text":
            chunks.append(block["text"].strip())
        elif kind == "query":
            sql = compile_query_sql(block, plan.get("filters") or [])
            chunks.append(f"```sql {block['name']}\n{sql}\n```")
        elif kind == "chart":
            config = [f"query: {block['query']}", f"type: {block['chart']}"]
            if block.get("x"):
                config.append(f"x: {block['x']}")
            if block.get("y"):
                config.append(f"y: {', '.join(block['y'])}")
            chunks.append("```chart\n" + "\n".join(config) + "\n```")
    for item in plan.get("filters") or []:
        chunks.append(f"```filter\nname: {item['name']}\ntype: {item['type']}\n```")
    return "\n\n".join(chunk for chunk in chunks if chunk) + "\n"
