"""Controlled read-only report SQL over data-channel dataset versions.

公开受控端口：运营报告（``app/operations``）的文章 SQL 在此校验并执行。
调用方（operations router 经其 query_service）必须先完成菜单/角色授权；
本模块只负责 SQL 安全门、数据集版本解析与受限执行，不做 HTTP。

安全模型（层层 fail-closed，缺一层都不算数）：

1. **AST 校验**（sqlglot，PostgreSQL 方言，不做正则假安全）：单条只读
   SELECT（可带 CTE/UNION/窗口/聚合），FROM 中只允许逻辑关系 ``dataset``
   与语句内定义的 CTE 名；拒绝多语句、DML/DDL/SELECT INTO/锁、任意物理
   表、跨库限定名、未绑定参数（``$1``/``:name``/``%(name)s``）、
   WITH RECURSIVE 与函数默认拒绝（仅显式允许的分析函数白名单）。
2. **物理替换**：校验后的 AST 中所有 ``dataset`` 表引用被替换为「物理列
   AS 逻辑列」的子查询（integrated 封存关系 / curated 湖表 / 物化临时表），
   用户 SQL 永不接触物理表名。
3. **数据库侧兜底**：PostgreSQL 上在 READ ONLY + REPEATABLE READ 事务内
   执行，statement_timeout/lock_timeout/work_mem/temp_file_limit 沿用
   统一查询预算（``configured_limits``）；即便校验出现缺口，只读事务也
   让任何写路径在数据库层硬失败。

数据集三类资产的版本解析（执行时取「最新可用版本」，不持久化 version_id）：

- ``integrated``：复用 ``integrated_store.get_version_relation`` 的封存
  不可变关系（sealed manifest 校验失败即拒绝）；
- ``curated``：先过既有审核门禁（当前版本必须 approved），无已批准行级
  编辑时直接映射湖表（lake_store 列映射）；有行编辑或遗留 blob 版本时按
  ``iter_rows_with_edits`` 的权威叠加语义有界分批物化到事务内临时表；
- ``structured``/``semi``（人工/遗留）：经 ``iter_version_rows`` 有界分批
  物化到事务内临时表（湖内快照文本语义，全 TEXT 列）。

物化与结果都受显式预算约束：超出即返回明确错误，绝不静默截断。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
import time
import uuid
from decimal import Decimal
from typing import Any, Iterator

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from .contracts import QueryError
from .limits import configured_limits

logger = logging.getLogger(__name__)

# 报告 SQL 的逻辑关系名：文章 SQL 只能 FROM dataset（或语句内 CTE）。
LOGICAL_RELATION = "dataset"
# 单条 SQL 上限（Markdown fenced block 级别，非整篇文章）。
SQL_TEXT_MAX_CHARS = 20_000

_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

# 语句/变更类节点：出现即拒绝（含 sqlglot 对 EXPLAIN/DO/CALL 等的 Command 兜底）。
_FORBIDDEN_NODES: tuple[tuple[type, str], ...] = (
    (exp.Insert, "INSERT"),
    (exp.Update, "UPDATE"),
    (exp.Delete, "DELETE"),
    (exp.Merge, "MERGE"),
    (exp.Create, "CREATE"),
    (exp.Drop, "DROP"),
    (exp.Alter, "ALTER"),
    (exp.TruncateTable, "TRUNCATE"),
    (exp.Command, "非 SELECT 语句（EXPLAIN/DO/CALL 等）"),
    (exp.Set, "SET"),
    (exp.Copy, "COPY"),
    (exp.Grant, "GRANT/REVOKE"),
    (exp.Transaction, "事务控制"),
    (exp.Commit, "COMMIT"),
    (exp.Rollback, "ROLLBACK"),
    (exp.Into, "SELECT INTO"),
    (exp.Lock, "锁子句（FOR UPDATE/SHARE 等）"),
    (exp.UserDefinedFunction, "自定义函数"),
    # 命名占位符（:name）在执行时由 SQLAlchemy 绑定；位置参数（$1/@1）不允许，
    # 因为报告 filter 只提供稳定的命名契约。
    (exp.Parameter, "未绑定的位置参数"),
)

# 函数白名单（大写归一后比对；默认拒绝一切未知函数）。
_ALLOWED_FUNCTIONS: frozenset[str] = frozenset({
    # 聚合
    "COUNT", "SUM", "AVG", "MIN", "MAX", "BOOL_AND", "BOOL_OR", "EVERY",
    "ARRAY_AGG", "STRING_AGG", "PERCENTILE_CONT", "PERCENTILE_DISC",
    "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
    "VARIANCE", "VAR_POP", "VAR_SAMP",
    # 窗口
    "ROW_NUMBER", "RANK", "DENSE_RANK", "PERCENT_RANK", "CUME_DIST",
    "NTILE", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE",
    # 数学
    "ABS", "ROUND", "CEIL", "CEILING", "FLOOR", "MOD", "SQRT", "POWER",
    "EXP", "LN", "LOG", "LOG10", "SIGN", "DIV", "TRUNC", "GCD", "LCM",
    # 字符串（刻意排除 repeat/lpad/rpad 等可按入参放大内存的构造函数）
    "LENGTH", "CHAR_LENGTH", "CHARACTER_LENGTH", "BIT_LENGTH",
    "LOWER", "UPPER", "INITCAP",
    "SUBSTR", "SUBSTRING", "LEFT", "RIGHT",
    "TRIM", "LTRIM", "RTRIM", "BTRIM",
    "REPLACE", "TRANSLATE", "CONCAT", "CONCAT_WS", "SPLIT_PART",
    "STRPOS", "POSITION", "REGEXP_REPLACE",
    "MD5", "SHA256", "TO_CHAR",
    # 日期时间
    "NOW", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME",
    "DATE_TRUNC", "DATE_PART", "EXTRACT", "AGE", "TO_DATE", "TO_TIMESTAMP",
    "MAKE_DATE", "MAKE_INTERVAL", "JUSTIFY_DAYS", "JUSTIFY_HOURS",
    # 空值/类型/条件
    "COALESCE", "NULLIF", "GREATEST", "LEAST", "CAST", "TRY_CAST",
    "CASE", "IF", "INTERVAL", "DISTINCT",
    # sqlglot 将 PostgreSQL 函数归一为跨方言 AST 名称。
    "TIMESTAMP_TRUNC", "GROUP_CONCAT", "TIME_TO_STR", "STR_TO_DATE",
    "STR_TO_TIME", "STR_POSITION", "LOGICAL_AND", "LOGICAL_OR",
    "VARIANCE_POP", "S_H_A2_DIGEST",
})


@dataclasses.dataclass(frozen=True)
class ReportSqlCheck:
    """校验结果：errors 非空即拒绝（fail-closed，tree 不外泄）。"""

    ok: bool
    errors: tuple[str, ...]
    tree: exp.Expression | None = None
    parameters: tuple[str, ...] = ()


def _fold(identifier: exp.Identifier) -> tuple[str, bool]:
    """(按 PostgreSQL 折叠规则归一的名字, 是否带引号)。"""
    return (identifier.name if identifier.quoted else identifier.name.lower(),
            bool(identifier.quoted))


def _folded_name(name: str, quoted: bool | None) -> str:
    return name if quoted else name.lower()


def check_report_sql(sql: str) -> ReportSqlCheck:
    """校验一条报告 SQL；返回全部问题（不抛异常，供保存期逐块反馈）。"""
    errors: list[str] = []
    if not isinstance(sql, str) or not sql.strip():
        return ReportSqlCheck(False, ("SQL 不能为空",))
    if len(sql) > SQL_TEXT_MAX_CHARS:
        return ReportSqlCheck(False, (f"SQL 超过 {SQL_TEXT_MAX_CHARS} 字符上限",))

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except ParseError as exc:
        return ReportSqlCheck(False, (f"SQL 解析失败：{str(exc).splitlines()[0]}",))
    if statements is None or len(statements) != 1 or statements[0] is None:
        return ReportSqlCheck(False, ("只允许单条 SELECT 语句",))
    tree = statements[0]

    if not isinstance(tree, _ALLOWED_ROOTS):
        return ReportSqlCheck(False, ("只允许单条 SELECT 语句（含 UNION/CTE）",))

    # SELECT INTO / FOR UPDATE 等挂在 Select 参数上的子句。
    for node in tree.find_all(exp.Select):
        if node.args.get("into") is not None:
            errors.append("拒绝 SELECT INTO")
        if node.args.get("locks"):
            errors.append("拒绝锁子句（FOR UPDATE/SHARE 等）")
        hints = node.args.get("hint")
        if hints is not None:
            errors.append("拒绝查询提示注释（hint）")

    for with_arg in tree.find_all(exp.With):
        if with_arg.args.get("recursive"):
            errors.append("拒绝 WITH RECURSIVE（不可证明有界的递归）")
            break
    if sum(1 for _ in tree.walk()) > 2000:
        errors.append("SQL 超过语法复杂度上限")
    if any(isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func)
           for node in tree.walk()):
        errors.append("拒绝带 schema 限定的函数")

    # 语句/变更节点与未绑定参数。
    for node_type, label in _FORBIDDEN_NODES:
        for _ in tree.find_all(node_type):
            errors.append(f"拒绝 {label}")
            break

    # CTE 名单（拒绝与逻辑关系同名，防止替换被遮蔽）。
    for cte in tree.find_all(exp.CTE):
        alias = cte.args.get("alias")
        identifier = alias.args.get("this") if isinstance(alias, exp.TableAlias) else None
        if not isinstance(identifier, exp.Identifier):
            errors.append("CTE 必须有显式别名")
            continue
        folded, _ = _fold(identifier)
        if folded == LOGICAL_RELATION:
            errors.append("CTE 名与逻辑关系 dataset 冲突")

    # 每个引用按实际作用域解析。CTE 在其定义体中不可见，前向/嵌套 WITH
    # 也不能放行同名物理表；全局收集别名会越过这个隔离边界。
    dataset_references = 0
    try:
        scopes = list(traverse_scope(tree))
        for scope in scopes:
            for table in scope.tables:
                identifier = table.this
                if not isinstance(identifier, exp.Identifier):
                    errors.append("FROM 子句只允许逻辑关系 dataset 与语句内 CTE（不允许表函数）")
                    continue
                if table.db or table.catalog:
                    errors.append("拒绝跨库/跨 schema 限定名")
                    continue
                resolved = scope.sources.get(table.alias_or_name)
                if isinstance(resolved, Scope):
                    continue
                folded, _ = _fold(identifier)
                if isinstance(resolved, exp.Table) and folded == LOGICAL_RELATION:
                    dataset_references += 1
                else:
                    errors.append(
                        f"只允许引用逻辑关系 dataset（发现 {identifier.name!r}）；"
                        "跨数据集查询不受支持")
            if any(not isinstance(source, (Scope, exp.Table))
                   for source in scope.sources.values()):
                errors.append("拒绝表函数或未知关系来源")
        scoped_tables = {id(table) for scope in scopes for table in scope.tables}
        if any(id(table) not in scoped_tables for table in tree.find_all(exp.Table)):
            errors.append("无法解析表引用的作用域")
    except (ValueError, sqlglot.errors.OptimizeError):
        errors.append("无法解析查询作用域")
    if dataset_references == 0:
        errors.append("查询必须引用逻辑关系 dataset")

    # 函数默认拒绝：白名单外一律拒绝（含 Anonymous 未知函数）。
    for func in tree.find_all(exp.Func):
        # sqlglot models boolean operators (OR/AND) as Func nodes too; they
        # are grammar operators rather than callable functions and are safe.
        if isinstance(func, (exp.And, exp.Or, exp.Not)):
            continue
        name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
        if not name or name.upper() not in _ALLOWED_FUNCTIONS:
            errors.append(f"拒绝函数 {name or '<unknown>'!r}（不在报告 SQL 白名单内）")
            break

    if any(True for _ in tree.find_all(exp.ObjectIdentifier)):
        errors.append("拒绝数据库对象标识类型转换")
    allowed_types = {"BOOLEAN", "TINYINT", "SMALLINT", "INT", "BIGINT", "FLOAT", "DOUBLE",
                     "DECIMAL", "CHAR", "VARCHAR", "TEXT", "DATE", "TIME", "TIMETZ",
                     "TIMESTAMP", "TIMESTAMPTZ", "INTERVAL", "JSON", "JSONB", "BINARY", "VARBINARY", "UUID"}
    if any(getattr(target.this, "value", None) not in allowed_types for target in tree.find_all(exp.DataType)):
        errors.append("拒绝非标量或自定义类型转换")
    if any(not node.name for node in tree.find_all(exp.Placeholder)):
        errors.append("只允许命名参数")
    parameters = tuple(sorted({node.name for node in tree.find_all(exp.Placeholder)
                               if isinstance(node.name, str) and node.name}))
    invalid_parameters = [name for name in parameters if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name)]
    if invalid_parameters:
        errors.append(f"参数名不合法：{', '.join(invalid_parameters)}")

    if errors:
        return ReportSqlCheck(False, tuple(errors), parameters=parameters)
    return ReportSqlCheck(True, (), tree, parameters)


@dataclasses.dataclass(frozen=True)
class _DatasetSource:
    """执行期解析出的「最新可用版本」物理来源。"""

    source: str  # sealed_relation | lake_table | materialized_temp
    physical_name: str
    column_map: dict[str, str]  # 逻辑列名 -> 物理列名
    temp_table: sa.Table | None  # 物化临时表（随事务销毁）
    meta: dict[str, Any]


def _version_meta(dataset, version, *, review_status: str | None = None) -> dict[str, Any]:
    schema = dict(getattr(dataset, "schema_json", None) or {})
    typed = [item for item in (schema.get("columns_typed") or []) if isinstance(item, dict)]
    if not typed:
        typed = [
            {"name": str(item), "type": "string"}
            for item in (schema.get("columns") or [])
            if isinstance(item, str)
        ]
    meta: dict[str, Any] = {
        "id": dataset.id,
        "name": dataset.name,
        "kind": (dataset.kind or "").lower(),
        "version_id": version.id,
        "version_no": version.version_no,
        "as_of": version.created_at.isoformat() if version.created_at else None,
        "rowcount": int(version.rowcount) if version.rowcount is not None else None,
        "columns_typed": typed,
        "source_schema": typed,
    }
    if review_status is not None:
        meta["review_status"] = review_status
    return meta


def _resolve_dataset_source(db: Session, dataset_id: str) -> _DatasetSource:
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.datasets.version_pointer import resolve_current_version

    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise QueryError(404, "report_dataset_not_found", "数据集不存在")
    version = resolve_current_version(db, dataset_id)
    if version is None:
        raise QueryError(409, "report_dataset_version_unavailable",
                         "数据集尚无可用数据版本")
    kind = (dataset.kind or "").strip().lower()

    if kind == "integrated":
        from app.data_channel.datasets.integrated_store import (
            IntegratedVersionError, get_version_relation,
        )
        try:
            relation = get_version_relation(db, dataset_id, version.id)
        except IntegratedVersionError as exc:
            raise QueryError(409, "report_dataset_version_unavailable",
                             f"集成数据集当前版本不可用：{exc}") from exc
        column_map = {str(logical): str(physical)
                      for logical, physical in relation.column_map.items()}
        if not column_map:
            raise QueryError(409, "report_dataset_version_unavailable",
                             "集成数据集当前版本没有可查询的列")
        return _DatasetSource(
            source="sealed_relation",
            physical_name=relation.table.name,
            column_map=column_map,
            temp_table=None,
            meta={**_version_meta(dataset, version),
                  "columns_typed": relation.metadata["columns_typed"],
                  "source_schema": relation.metadata["source_schema"],
                  "schema_fingerprint": relation.metadata["schema_fingerprint"],
                  "as_of": relation.metadata["as_of"]})

    if kind == "curated":
        from app.data_channel.curated.approved_version_reader import (
            ReviewApprovalError, has_approved_edits, require_version_approved,
            version_review,
        )
        try:
            require_version_approved(db, dataset_id, version)
        except ReviewApprovalError as exc:
            review = version_review(db, dataset_id, version)
            raise QueryError(
                409,
                "report_dataset_version_rejected"
                if review is not None and review.status == "rejected"
                else "report_dataset_version_not_approved",
                f"成品数据集当前版本不可用于报告查询：{exc}",
                dataset_version_id=version.id,
                version_no=version.version_no,
                review_status=(review.status if review is not None
                               else "pending_review"),
            ) from exc
        review = version_review(db, dataset_id, version)
        meta = _version_meta(
            dataset, version,
            review_status=(review.status if review is not None
                           else "pending_review"))

        from app.data_channel.datasets import lake_store
        if (lake_store.version_uses_lake(dataset, version)
                and not has_approved_edits(db, dataset_id, version)):
            raw_mapping = (dataset.schema_json or {}).get("lake_columns")
            mapping = ({str(k): str(v) for k, v in raw_mapping.items()}
                       if isinstance(raw_mapping, dict) else None)
            if not mapping or not lake_store.lake_table_exists(db, dataset_id):
                raise QueryError(409, "report_dataset_version_unavailable",
                                 "成品数据集尚无物理湖表，无法执行报告查询")
            # 湖表当前态 == 已审批当前版本（指针语义 + 无行编辑叠加）。
            return _DatasetSource(
                source="lake_table",
                physical_name=lake_store.lake_table_name(dataset_id),
                column_map=mapping,
                temp_table=None,
                meta=meta)
        # 有已批准行编辑或遗留 blob 版本：按权威叠加语义物化。
        from app.data_channel.curated.approved_version_reader import (
            iter_rows_with_edits,
        )
        return _materialize(
            db, meta, iter_rows_with_edits(db, dataset_id,
                                           require_approved=True, version=version),
            expected_columns=_curated_logical_columns(dataset))

    if kind in {"structured", "semi"}:
        from app.data_channel.datasets.service import (
            DatasetReadError, iter_version_rows,
        )
        try:
            batches = iter_version_rows(db, dataset_id, version.id)
        except DatasetReadError as exc:
            raise QueryError(409, "report_dataset_version_unavailable",
                             f"数据集版本读取失败：{exc}") from exc
        schema = dataset.schema_json or {}
        expected = ([str(c) for c in schema.get("columns") or []
                     if isinstance(c, str)] or None)
        return _materialize(db, _version_meta(dataset, version), batches,
                            expected_columns=expected)

    raise QueryError(409, "report_dataset_kind_unsupported",
                     f"数据集类型 {kind!r} 不支持报告 SQL（仅支持 "
                     "integrated/curated/structured/semi）")


def _curated_logical_columns(dataset) -> list[str] | None:
    schema = dataset.schema_json or {}
    columns: list[str] = []
    for item in schema.get("columns") or []:
        name = item.get("name") if isinstance(item, dict) else item
        name = str(name or "").strip()
        if name and name not in columns:
            columns.append(name)
    if columns:
        return columns
    for name in (schema.get("lake_columns") or {}):
        columns.append(str(name))
    return columns or None


def _materialize(
    db: Session, meta: dict[str, Any], batches: Iterator[list[dict]],
    *, expected_columns: list[str] | None,
) -> _DatasetSource:
    """有界分批把权威行物化到事务内临时表（全 TEXT 列，快照文本语义）。"""
    from app.data_channel.datasets.snapshot_text import snapshot_cell_text

    limits = configured_limits()
    rows_budget = limits.federated_side_rows_limit
    bytes_budget = min(limits.federated_side_bytes_limit,
                       limits.staging_bytes_limit)
    table_name = "rep_ds_" + uuid.uuid4().hex
    columns: list[str] = []
    table: sa.Table | None = None
    total_rows = 0
    total_bytes = 0
    try:
        for batch in batches:
            if not batch:
                continue
            if not columns:
                columns = [str(name) for name in batch[0].keys()]
                if expected_columns is not None:
                    columns = [name for name in expected_columns
                               if name in set(columns)] or columns
                table = sa.Table(
                    table_name, sa.MetaData(),
                    *(sa.Column(name, sa.Text) for name in columns),
                    prefixes=["TEMPORARY"])
                table.create(db.connection())
            if total_rows + len(batch) > rows_budget:
                raise QueryError(422, "report_materialization_budget_exceeded",
                                 f"数据集版本行数超过报告查询物化预算 "
                                 f"({rows_budget} 行)，已拒绝执行",
                                 budget="materialized_rows", limitRows=rows_budget)
            payload = []
            for row in batch:
                total_bytes += sum(
                    len(snapshot_cell_text(row.get(name)).encode("utf-8"))
                    for name in columns)
                if total_bytes > bytes_budget:
                    raise QueryError(422, "report_materialization_budget_exceeded",
                                     f"数据集版本内容超过报告查询物化字节预算，已拒绝执行",
                                     budget="materialized_bytes", limitBytes=bytes_budget)
                payload.append({name: snapshot_cell_text(row.get(name))
                                for name in columns})
            db.connection().execute(sa.insert(table), payload)
            total_rows += len(batch)
        if table is None:
            # 空版本：按期望列建空临时表，查询返回零行而不是失败。
            columns = list(expected_columns or [])
            if columns:
                table = sa.Table(
                    table_name, sa.MetaData(),
                    *(sa.Column(name, sa.Text) for name in columns),
                    prefixes=["TEMPORARY"])
                table.create(db.connection())
        if table is None:
            raise QueryError(409, "report_dataset_version_unavailable",
                             "数据集版本没有可查询的列")
    except QueryError:
        _drop_temp(db, table)
        raise
    except DBAPIError as exc:
        _drop_temp(db, table)
        raise QueryError(503, "report_execution_failed",
                         "物化数据集版本失败") from exc
    return _DatasetSource(
        source="materialized_temp",
        physical_name=table_name,
        column_map={name: name for name in columns},
        temp_table=table,
        meta={**meta, "materialized_rows": total_rows})


def _drop_temp(db: Session, table: sa.Table | None) -> None:
    if table is None:
        return
    try:
        table.drop(db.connection(), checkfirst=True)
    except DBAPIError:
        logger.warning("report temp table cleanup failed", exc_info=True)


def _substitute_dataset(tree: exp.Expression, source: _DatasetSource, *, postgres: bool = True) -> exp.Expression:
    """把校验后的 AST 中所有 dataset 表引用替换为物理投影子查询。"""

    type_map = {
        "integer": "BIGINT", "float": "DOUBLE PRECISION", "number": "DOUBLE PRECISION",
        "decimal": "NUMERIC", "boolean": "BOOLEAN", "date": "DATE",
        "timestamp": "TIMESTAMP", "json": "JSONB", "binary": "BYTEA",
        "string": "TEXT",
    }
    typed_columns = source.meta.get("columns_typed") or source.meta.get("source_schema") or []
    declared_types = {
        str(item.get("name")): str(item.get("type") or "string").lower()
        for item in typed_columns if isinstance(item, dict) and item.get("name")
    }
    projections = []
    for logical, physical in source.column_map.items():
        expression: exp.Expression = exp.column(physical, quoted=True)
        target = type_map.get(declared_types.get(logical, "string"))
        # Physical lake/manual staging values are canonical TEXT. Cast only when
        # the frozen contract declares a non-text type; invalid values fail closed
        # in PostgreSQL instead of silently changing aggregation semantics.
        if target and target != "TEXT":
            # 湖内空单元格按空文本保存；声明为数值/时间等标量时恢复 NULL，
            # 非空但不合法的文本仍由 PostgreSQL 明确拒绝。
            expression = exp.Nullif(this=expression, expression=exp.Literal.string(""))
            expression = exp.Cast(this=expression, to=exp.DataType.build(target))
        projections.append(exp.alias_(expression, logical, quoted=True))
    physical_select = exp.Select(expressions=projections)
    physical_select.set(
        "from_",
        exp.From(this=exp.Table(
            this=exp.to_identifier(source.physical_name, quoted=True),
            db=exp.to_identifier("pg_temp" if source.temp_table is not None else "public", quoted=True)
            if postgres else None)))

    tables = [t for t in tree.find_all(exp.Table)
              if isinstance(t.this, exp.Identifier)
              and _folded_name(t.this.name, t.this.quoted) == LOGICAL_RELATION
              and not t.db and not t.catalog]
    for table in tables:
        alias_arg = table.args.get("alias")
        if isinstance(alias_arg, exp.TableAlias):
            alias_identifier = alias_arg.args.get("this")
            alias = exp.TableAlias(this=exp.to_identifier(
                alias_identifier.name if isinstance(alias_identifier, exp.Identifier)
                else LOGICAL_RELATION,
                quoted=bool(alias_identifier.quoted)
                if isinstance(alias_identifier, exp.Identifier) else False))
        else:
            alias = exp.TableAlias(this=exp.to_identifier(
                table.this.name, quoted=bool(table.this.quoted)))
        table.replace(exp.Subquery(this=physical_select.copy(), alias=alias))
    return tree


def _pg_budget(db: Session, limits, started: float) -> None:
    """在当前 READ ONLY 事务内沿用统一查询的执行预算（PostgreSQL）。"""
    import time

    remaining = max(1, int(limits.statement_timeout_ms
                           - (time.monotonic() - started) * 1000))
    settings = {
        "statement_timeout": f"{remaining}ms",
        "lock_timeout": f"{limits.lock_timeout_ms}ms",
        "work_mem": f"{limits.work_mem_mib}MB",
        "TimeZone": "UTC",
    }
    for name, value in settings.items():
        db.execute(sa.text("SELECT set_config(:name, :value, true)"),
                   {"name": name, "value": value})
    try:
        raw = str(db.execute(sa.text("SHOW temp_file_limit")).scalar_one())
        cap = _memory_bytes(raw)
        if cap is None or cap > limits.temp_file_limit_mib * 1048576:
            db.execute(sa.text("SELECT set_config('temp_file_limit', :value, true)"),
                       {"value": f"{limits.temp_file_limit_mib}MB"})
    except DBAPIError as exc:
        raise QueryError(503, "query_budget_unavailable",
                         "PostgreSQL query budget requires an effective role "
                         "temp_file_limit or SET permission") from exc


def _memory_bytes(raw: str) -> int | None:
    """PostgreSQL 内存/磁盘配额字符串 → 字节数（与统一查询同口径）。"""
    import re

    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    match = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]*)\s*", raw)
    if not match:
        return None
    number, unit = match.groups()
    multiplier = units.get(unit.upper(), 1) if unit else 1
    return int(number) * multiplier


def _json_cell(value: Any) -> Any:
    """结果单元格 → JSON 安全值；非有限数值显式失败。"""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise QueryError(422, "invalid_typed_value", "结果包含非有限数值")
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QueryError(422, "invalid_typed_value", "结果包含非有限数值")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _dataset_snapshot_key(db: Session, dataset_id: str) -> str:
    """跨 staging/只读事务复核当前版本、模式及审核编辑；不读取版本载荷。"""
    from app.data_channel.datasets.models import Dataset, DatasetVersion
    from app.data_channel.curated.models import CuratedReview, CuratedRowEdit
    from .contracts import stable_hash

    dataset = db.execute(sa.select(
        Dataset.latest_version_id, Dataset.schema_json, Dataset.updated_at,
    ).where(Dataset.id == dataset_id)).one_or_none()
    if dataset is None:
        raise QueryError(404, "report_dataset_not_found", "数据集不存在")
    versions = db.execute(sa.select(
        DatasetVersion.id, DatasetVersion.version_no, DatasetVersion.checksum,
    ).where(DatasetVersion.dataset_id == dataset_id).order_by(
        DatasetVersion.version_no.desc()).limit(1)).all()
    reviews = db.execute(sa.select(
        CuratedReview.id, CuratedReview.dataset_version_id, CuratedReview.status,
        CuratedReview.decided_at,
    ).where(CuratedReview.curated_dataset_id == dataset_id).order_by(CuratedReview.id)).all()
    edits = db.execute(sa.select(
        CuratedRowEdit.id, CuratedRowEdit.row_pk, CuratedRowEdit.field_name,
        CuratedRowEdit.new_value, CuratedRowEdit.edited_at,
    ).join(CuratedReview, CuratedReview.id == CuratedRowEdit.review_id).where(
        CuratedReview.curated_dataset_id == dataset_id,
        CuratedReview.status == "approved",
    ).order_by(CuratedRowEdit.id)).all()
    def values(rows):
        return [[value.isoformat() if hasattr(value, "isoformat") else value
                 for value in row] for row in rows]
    return stable_hash([values([dataset]), values(versions), values(reviews), values(edits)])


def _run_checked_query(db: Session, check: ReportSqlCheck, source: _DatasetSource,
                       params: dict[str, Any], limit: int, bytes_budget: int) -> dict:
    # 直接使用驱动占位符执行生成的 AST，避免对 SQL 文本做参数正则替换，
    # 也避免 SQLAlchemy text() 把字符串内的 :name 误认成参数。
    postgres = db.get_bind().dialect.name == "postgresql"
    tree = _substitute_dataset(check.tree.copy(), source, postgres=postgres)
    final_sql = tree.sql(dialect="postgres" if postgres else "sqlite", comments=False)
    bound = {name: params.get(name) for name in check.parameters}
    if postgres:
        # sqlglot 只对 AST 占位符生成 %(name)s；字面量中的百分号交由驱动转义。
        from sqlglot.dialects.postgres import Postgres
        class DriverGenerator(Postgres.Generator):
            MOD_OPERATOR = "%%"

            def literal_sql(self, expression):
                return super().literal_sql(expression).replace("%", "%%")

            def identifier_sql(self, expression):
                return super().identifier_sql(expression).replace("%", "%%")

            def bytestring_sql(self, expression):
                return super().bytestring_sql(expression).replace("%", "%%")
        final_sql = DriverGenerator(dialect=Postgres(), comments=False).generate(tree)
    with db.connection().exec_driver_sql(final_sql, bound, execution_options={
        "stream_results": True, "yield_per": 1, "max_row_buffer": 1,
    }) as result:
        names = list(result.keys())
        if len(names) != len(set(names)):
            raise QueryError(422, "report_duplicate_columns", "查询结果列名重复，请为各列设置唯一别名")
        rows = []
        encoded_bytes = 0
        for row in result:
            if len(rows) == limit:
                raise QueryError(422, "report_result_budget_exceeded",
                                 "查询结果超过行数上限，请使用聚合、筛选或 LIMIT 缩小结果",
                                 budget="result_rows", limitRows=limit)
            cells = [_json_cell(value) for value in row]
            encoded_bytes += len(json.dumps(cells, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if encoded_bytes > bytes_budget:
                raise QueryError(422, "report_result_budget_exceeded", "查询结果超过返回字节预算",
                                 budget="result_bytes", limitBytes=bytes_budget)
            rows.append(dict(zip(names, cells)))
    return {"columns": names, "rows": rows, "rowcount": len(rows), "truncated": False,
            "dataset": source.meta, "source": source.source}


def execute_report_queries(db: Session, dataset_id: str, queries: dict[str, str], *,
                           params: dict[str, Any] | None = None, limit: int = 200) -> dict[str, dict]:
    """一次物化、同一物理连接和只读快照内执行整篇文章的命名查询。"""
    if not queries or len(queries) > 20:
        raise QueryError(422, "report_query_count_invalid", "每次执行必须包含 1..20 个命名查询")
    checks = {name: check_report_sql(sql) for name, sql in queries.items()}
    for name, check in checks.items():
        if not check.ok:
            raise QueryError(422, "report_sql_invalid", "报告 SQL 未通过安全校验",
                             query=name, errors=list(check.errors))
    params = dict(params or {})
    if any(value is not None and (not isinstance(value, (str, int, float, bool))
           or isinstance(value, float) and not math.isfinite(value)) for value in params.values()):
        raise QueryError(422, "report_sql_params_invalid", "筛选参数只能是有限的标量值")
    limits = configured_limits()
    if type(limit) is not int or not 1 <= limit <= limits.max_page_size:
        raise QueryError(422, "invalid_result_limit", f"limit 必须是 1..{limits.max_page_size} 的整数")

    started = time.monotonic()
    # 独立 Session 不提交/回滚调用方的工作单元；Connection 在 staging commit
    # 后仍由本函数持有，保证临时表和用户查询落在同一个数据库会话。
    bind = db.get_bind()
    engine = bind.engine if isinstance(bind, sa.engine.Connection) else bind
    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with Session(bind=connection, expire_on_commit=False) as worker:
            source = None
            try:
                if postgres:
                    _pg_budget(worker, limits, started)
                snapshot = _dataset_snapshot_key(worker, dataset_id)
                source = _resolve_dataset_source(worker, dataset_id)
                worker.commit()
                if postgres:
                    worker.execute(sa.text("SET TRANSACTION READ ONLY"))
                    _pg_budget(worker, limits, started)
                if _dataset_snapshot_key(worker, dataset_id) != snapshot:
                    raise QueryError(409, "report_dataset_changed", "数据版本或模式已变化，请重新查询")
                if postgres:
                    worker.execute(sa.text("SET LOCAL search_path = pg_catalog"))
                results = {}
                remaining_bytes = limits.memory_result_bytes_limit
                for name, check in checks.items():
                    if postgres:
                        _pg_budget(worker, limits, started)
                    result = _run_checked_query(worker, check, source, params, limit, remaining_bytes)
                    remaining_bytes -= len(json.dumps(result["rows"], ensure_ascii=False).encode("utf-8"))
                    result["duration_ms"] = max(0, int((time.monotonic() - started) * 1000))
                    results[name] = result
                return results
            except DBAPIError as exc:
                raise _report_dbapi_error(exc) from exc
            finally:
                worker.rollback()
                if source is not None and source.temp_table is not None:
                    try:
                        source.temp_table.drop(connection, checkfirst=True)
                        connection.commit()
                    except DBAPIError:
                        connection.invalidate()
                        logger.warning("report temp table cleanup failed", exc_info=True)


def execute_report_query(db: Session, dataset_id: str, sql: str, *,
                         params: dict[str, Any] | None = None, limit: int = 200) -> dict[str, Any]:
    return execute_report_queries(db, dataset_id, {"query": sql}, params=params, limit=limit)["query"]


def _report_dbapi_error(exc: DBAPIError) -> QueryError:
    """把驱动错误映射为稳定的报告查询错误码（不回显 SQL/参数）。"""
    original = getattr(exc, "orig", None)
    code = getattr(original, "pgcode", None)
    logger.warning(
        "report sql driver error driver=%s sqlstate=%s",
        type(exc).__name__, code)
    if code == "42703":
        return QueryError(422, "report_unknown_column", "查询引用了不存在的列")
    if code == "42P01":
        return QueryError(422, "report_unknown_relation", "查询引用了不存在的关系")
    if code in {"42883", "42846"}:
        return QueryError(422, "report_unknown_function_or_cast",
                          "查询使用了不存在的函数或不兼容的类型转换")
    if code and code.startswith("22"):
        return QueryError(422, "invalid_typed_value",
                          "查询值违反其声明类型")
    if code in {"57014", "53400", "53100", "53200", "54000"}:
        return QueryError(422, "report_budget_exceeded",
                          "查询超出数据库资源预算")
    if code == "42501":
        return QueryError(403, "report_permission_denied",
                          "数据库拒绝了该只读查询")
    return QueryError(503, "report_execution_failed", "报告查询执行失败")
