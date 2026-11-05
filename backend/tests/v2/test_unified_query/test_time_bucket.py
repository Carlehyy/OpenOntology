"""时间分桶分组（day/week/month/quarter/year）的编译与执行语义。

桶起点统一 ISO 日期（周为周一）：PostgreSQL date_trunc、MySQL 原生日期
算术、SQLite strftime/date 修饰符三方言；跨源 Python 聚合经 _bucket_start
镜像同一语义。非时间字段分桶 fail closed（422），不静默按原值分组。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import ColumnClause, TextClause

from app.data_channel.query.compiler import compile_query
from app.data_channel.query.contracts import ColumnSpec, DerivedSourceSpec, QueryError, Relation
from app.data_channel.query.derived import resolve_derived
from app.data_channel.query.federated import _bucket_start, _group_item_parts
from app.data_channel.query.parser import Command, Expr, Pipeline, Ref, Value


def _relation(alias, table, specs, engine):
    def batches(statement, params, batch_size):
        result = engine.connect().execute(statement, params or {}).mappings()
        rows = [dict(row) for row in result]
        result.close()
        yield rows

    return Relation(alias=alias, table=table,
                    columns={spec.name: spec for spec in specs},
                    batches=batches, connection_key="local", state={},
                    auth_fingerprint="af", schema_fingerprint="sf")


@pytest.fixture
def events():
    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    table = sa.Table(
        "events", meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("occurred_at", sa.Date(), nullable=False),
        sa.Column("name", sa.String(32), nullable=True))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), [
            {"id": 1, "occurred_at": date(2026, 1, 3), "name": "a"},    # 周六
            {"id": 2, "occurred_at": date(2026, 1, 6), "name": "b"},    # 周三
            {"id": 3, "occurred_at": date(2026, 1, 13), "name": "a"},   # 周二
            {"id": 4, "occurred_at": date(2026, 2, 20), "name": "b"},   # 周五
            {"id": 5, "occurred_at": date(2026, 3, 8), "name": "a"},    # 周日
            {"id": 6, "occurred_at": date(2026, 10, 4), "name": "c"},   # 周日
        ])
    specs = [ColumnSpec("id", "integer"), ColumnSpec("occurred_at", "date"),
             ColumnSpec("name", "string")]
    relation = _relation("t", table, specs, engine)
    yield engine, table, relation
    engine.dispose()


def _bucket_pipeline(bucket: str):
    return Pipeline("t", "t", (
        Command("stats", (
            ("count", None, "n"),
            ("group", ((Ref("t", "occurred_at"), "bucket", bucket),)),
        )),
        Command("sort", (("bucket", "asc", "last"),)),
    ))


def _run_bucket(engine, relation, bucket: str):
    compiled = compile_query(_bucket_pipeline(bucket), {"t": relation}, {},
                             dialect="sqlite")
    with engine.connect() as conn:
        rows = [dict(row) for row in
                conn.execute(compiled.statement, compiled.params or {}).mappings()]
    return rows


@pytest.mark.parametrize("bucket,expected", [
    ("month", [("2026-01-01", 3), ("2026-02-01", 1),
               ("2026-03-01", 1), ("2026-10-01", 1)]),
    ("week", [("2025-12-29", 1), ("2026-01-05", 1), ("2026-01-12", 1),
              ("2026-02-16", 1), ("2026-03-02", 1), ("2026-09-28", 1)]),
    ("quarter", [("2026-01-01", 5), ("2026-10-01", 1)]),
    ("year", [("2026-01-01", 6)]),
    ("day", [("2026-01-03", 1), ("2026-01-06", 1), ("2026-01-13", 1),
             ("2026-02-20", 1), ("2026-03-08", 1), ("2026-10-04", 1)]),
])
def test_bucket_grouping_executes_with_monday_weeks(events, bucket, expected):
    engine, _table, relation = events
    rows = _run_bucket(engine, relation, bucket)
    got = [(row["bucket"], int(row["n"])) for row in rows]
    assert got == expected


def test_bucket_dialect_sql_shapes(events):
    _engine, _table, relation = events
    postgres = compile_query(_bucket_pipeline("month"), {"t": relation}, {},
                             dialect="postgresql")
    pg_sql = str(postgres.statement.compile(
        dialect=sa.dialects.postgresql.dialect()))
    assert "date_trunc" in pg_sql.lower()

    mysql = compile_query(_bucket_pipeline("week"), {"t": relation}, {},
                          dialect="mysql")
    my_sql = str(mysql.statement.compile(dialect=sa.dialects.mysql.dialect()))
    assert "weekday" in my_sql.lower() and "to_days" in my_sql.lower()

    quarter = compile_query(_bucket_pipeline("quarter"), {"t": relation}, {},
                            dialect="mysql")
    q_sql = str(quarter.statement.compile(dialect=sa.dialects.mysql.dialect()))
    assert "period_add" in q_sql.lower()


@pytest.mark.parametrize("bucket", ["day", "week", "month", "quarter", "year", "half_hour"])
def test_mysql_bucket_sort_never_references_ungrouped_base_columns(events, bucket):
    """MySQL 8.4 ONLY_FULL_GROUP_BY (ERROR 1055) regression, real-run found.

    The NULL-ordering CASE used to re-expand the bucket expression over the
    derived table's base column, which MySQL rejects even though SELECT and
    GROUP BY carry the identical expression. The sort must read the grouped
    output in an outer projection; the ORDER BY clause may not touch qf_*
    base columns at all.
    """
    _engine, _table, relation = events
    compiled = compile_query(_bucket_pipeline(bucket), {"t": relation}, {},
                             dialect="mysql")
    sql = str(compiled.statement.compile(dialect=sa.dialects.mysql.dialect()))
    select_sql, order_sql = sql.split("ORDER BY", 1)
    assert "GROUP BY" in select_sql and "qf_" in select_sql
    assert "qf_" not in order_sql
    assert "IS NULL" in order_sql and "bucket" in order_sql


def test_mysql_non_bucket_sort_stays_single_layer(events):
    _engine, _table, relation = events
    pipeline = Pipeline("t", "t", (
        Command("stats", (("count", None, "n"),
                          ("group", ((Ref("t", "name"), "name"),)))),
        Command("sort", (("n", "desc", "last"),)),
    ))
    compiled = compile_query(pipeline, {"t": relation}, {}, dialect="mysql")
    sql = str(compiled.statement.compile(dialect=sa.dialects.mysql.dialect()))
    # The ONLY_FULL_GROUP_BY workaround is scoped to bucketed sort outputs;
    # plain grouped sorts keep their existing statement shape.
    assert sql.count("FROM (SELECT") == 1


def test_half_hour_bucket_executes_on_sqlite_with_timestamp_output():
    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    table = sa.Table(
        "ticks", meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), [
            {"id": 1, "occurred_at": datetime(2026, 1, 1, 10, 0, 0)},
            {"id": 2, "occurred_at": datetime(2026, 1, 1, 10, 29, 59)},
            {"id": 3, "occurred_at": datetime(2026, 1, 1, 10, 30, 0)},
            {"id": 4, "occurred_at": datetime(2026, 1, 1, 10, 59, 59)},
            {"id": 5, "occurred_at": datetime(2026, 1, 1, 23, 59, 59)},
            {"id": 6, "occurred_at": None},
        ])
    specs = [ColumnSpec("id", "integer"), ColumnSpec("occurred_at", "timestamp")]
    pipeline = Pipeline("t", "t", (
        Command("stats", (("count", None, "n"),
                          ("group", ((Ref("t", "occurred_at"), "bucket", "half_hour"),)))),
        Command("sort", (("bucket", "asc", "last"),)),
    ))
    compiled = compile_query(pipeline, {"t": _relation("t", table, specs, engine)}, {},
                             dialect="sqlite")
    assert compiled.columns[0].type == "timestamp"
    with engine.connect() as conn:
        rows = [dict(row) for row in
                conn.execute(compiled.statement, compiled.params or {}).mappings()]
    assert [(row["bucket"], int(row["n"])) for row in rows] == [
        ("2026-01-01 10:00:00", 2), ("2026-01-01 10:30:00", 2),
        ("2026-01-01 23:30:00", 1), (None, 1)]
    engine.dispose()


def test_half_hour_output_keeps_source_timezone_flag(events):
    _engine, _table, relation = events
    relation.columns["occurred_at"] = ColumnSpec("occurred_at", "timestamp", timezone=True)
    compiled = compile_query(_bucket_pipeline("half_hour"), {"t": relation}, {},
                             dialect="sqlite")
    spec = next(c for c in compiled.columns if c.name == "bucket")
    assert spec.type == "timestamp" and spec.timezone is True


@pytest.mark.parametrize("bucket", ["day", "week", "month", "quarter", "year", "half_hour"])
@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "sqlite"])
def test_bucket_expression_is_typed_for_native_read_port(events, bucket, dialect):
    """The native read port rejects TextClause/literal columns (RAW_SQL_DENIED).

    Every bucket expression must be a typed SQLAlchemy construct: interval
    units/values cannot be raw SQL fragments. This is the compiler-side guard
    contract; the real read-port execution is covered by the opt-in MySQL/PG
    suites.
    """
    _engine, _table, relation = events
    compiled = compile_query(_bucket_pipeline(bucket), {"t": relation}, {},
                             dialect=dialect)
    raw = [node for node in visitors.iterate(compiled.statement)
           if isinstance(node, TextClause)
           or (isinstance(node, ColumnClause) and node.is_literal and node.name != "*")]
    assert raw == []


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_derived_half_hour_subquery_is_typed_for_native_read_port(events, dialect):
    """QS-subquery shape: inner half-hour aggregation + outer threshold/summary.

    The native guard traverses the whole statement, including an inner derived
    subquery. No TextClause/interval-literal fragment may hide in the nested
    half-hour expression.
    """
    _engine, _table, relation = events
    relation.state["dialect"] = dialect
    inner = DerivedSourceSpec(
        alias="inner", source="t", pipeline=_bucket_pipeline("half_hour"), params={})
    relations = resolve_derived({"t": relation}, {"inner": inner})
    outer = Pipeline("inner", "inner", (
        Command("where", (Expr(">=", (Ref("inner", "bucket"),
                                      Value("2026-01-01T00:00:00")),),)),
        Command("fields", ((Ref("inner", "bucket"), "bucket"),
                           (Ref("inner", "n"), "n"))),
        Command("sort", (("bucket", "asc", "last"),)),
    ))
    compiled = compile_query(outer, {"inner": relations["inner"]}, {}, dialect=dialect)
    raw = [node for node in visitors.iterate(compiled.statement)
           if isinstance(node, TextClause)
           or (isinstance(node, ColumnClause) and node.is_literal and node.name != "*")]
    assert raw == []


def test_bucket_requires_temporal_column(events):
    _engine, _table, relation = events
    pipeline = Pipeline("t", "t", (
        Command("stats", (
            ("count", None, "n"),
            ("group", ((Ref("t", "name"), "bucket", "month"),)),
        )),
    ))
    with pytest.raises(QueryError) as exc:
        compile_query(pipeline, {"t": relation}, {}, dialect="sqlite")
    assert exc.value.code == "unsupported_group_bucket"


def test_bucket_output_column_is_date_typed(events):
    _engine, _table, relation = events
    compiled = compile_query(_bucket_pipeline("month"), {"t": relation}, {},
                             dialect="sqlite")
    spec = next(c for c in compiled.columns if c.name == "bucket")
    assert spec.type == "date" and spec.nullable is True


def test_bounded_stats_limit_compiles_after_sort(events):
    _engine, _table, relation = events
    pipeline = Pipeline("t", "t", (
        *_bucket_pipeline("month").commands,
        Command("limit", (2, 0)),
    ))
    compiled = compile_query(pipeline, {"t": relation}, {}, dialect="sqlite")
    assert compiled.before_limit is not None
    assert "LIMIT" in str(compiled.statement.compile(
        dialect=sa.dialects.sqlite.dialect())).upper()


class TestBucketStart:
    def test_python_bucket_semantics(self):
        assert _bucket_start(date(2026, 1, 3), "week") == date(2025, 12, 29)
        assert _bucket_start(date(2026, 3, 8), "week") == date(2026, 3, 2)
        assert _bucket_start(date(2026, 2, 20), "month") == date(2026, 2, 1)
        assert _bucket_start(date(2026, 3, 8), "quarter") == date(2026, 1, 1)
        assert _bucket_start(date(2026, 10, 4), "quarter") == date(2026, 10, 1)
        assert _bucket_start(date(2026, 10, 4), "year") == date(2026, 1, 1)
        assert _bucket_start(None, "month") is None

    def test_python_bucket_accepts_datetime_and_iso_strings(self):
        assert _bucket_start(datetime(2026, 2, 20, 10, 30), "month") == date(2026, 2, 1)
        assert _bucket_start("2026-02-20 10:30:00", "month") == date(2026, 2, 1)
        assert _bucket_start("2026-02-20", "week") == date(2026, 2, 16)

    def test_python_bucket_rejects_non_temporal(self):
        with pytest.raises(QueryError):
            _bucket_start("not-a-date", "month")

    def test_python_half_hour_bucket_floors_and_keeps_datetime(self):
        assert _bucket_start(datetime(2026, 1, 1, 10, 0, 0), "half_hour") == \
            datetime(2026, 1, 1, 10, 0, 0)
        assert _bucket_start(datetime(2026, 1, 1, 10, 29, 59), "half_hour") == \
            datetime(2026, 1, 1, 10, 0, 0)
        assert _bucket_start(datetime(2026, 1, 1, 10, 30, 0), "half_hour") == \
            datetime(2026, 1, 1, 10, 30, 0)
        assert _bucket_start(datetime(2026, 1, 1, 10, 59, 59, 999999), "half_hour") == \
            datetime(2026, 1, 1, 10, 30, 0)
        assert _bucket_start(date(2026, 1, 1), "half_hour") == \
            datetime(2026, 1, 1, 0, 0, 0)
        assert _bucket_start("2026-01-01 10:45:30", "half_hour") == \
            datetime(2026, 1, 1, 10, 30, 0)
        assert _bucket_start("2026-01-01", "half_hour") == \
            datetime(2026, 1, 1, 0, 0, 0)

    def test_python_half_hour_bucket_preserves_timezone(self):
        aware = datetime(2026, 1, 1, 10, 45, tzinfo=timezone.utc)
        assert _bucket_start(aware, "half_hour") == \
            datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)
        assert _bucket_start(aware, "half_hour").tzinfo is timezone.utc


def test_group_item_parts_variants():
    ref = Ref("t", "d")
    assert _group_item_parts(ref) == (ref, "d", None)
    assert _group_item_parts((ref, "out")) == (ref, "out", None)
    assert _group_item_parts((ref, "out", "month")) == (ref, "out", "month")
    assert _group_item_parts((ref, "out", None)) == (ref, "out", None)
