"""plan v1 规范化与编译的单元边界：字段白名单、类型对齐、别名/围栏安全、
COUNT(DISTINCT)、默认 LIMIT、筛选 CAST 与参数类型检查。"""
from __future__ import annotations

import pytest

from app.data_channel.query.report_sql import check_report_sql
from app.operations.markdown import validate_markdown
from app.operations.plan import (
    QUERY_DEFAULT_LIMIT,
    PlanValidationError,
    ReportPlan,
    compile_plan_queries,
    compile_query_sql,
    normalize_plan,
    plan_markdown,
    validate_plan_params,
)

_FIELD_TYPES = {
    "region": "string",
    "amount": "number",
    "created_at": "timestamp",
    "day": "date",
    "区域": "string",
    "下单时间": "timestamp",
}


def _plan(**overrides) -> ReportPlan:
    payload: dict = {
        "version": 1,
        "filters": [{"name": "region", "type": "text", "field": "region"}],
        "blocks": [
            {"type": "text", "text": "订单概览"},
            {"type": "query", "name": "q1", "columns": [
                {"field": "region"},
                {"agg": "count", "alias": "orders"},
                {"agg": "sum", "field": "amount", "alias": "total"},
             ], "sort": [{"field": "total", "dir": "desc"}]},
            {"type": "chart", "chart": "bar", "query": "q1",
             "x": "region", "y": ["total"]},
        ],
    }
    payload.update(overrides)
    return ReportPlan.model_validate(payload)


def test_normalize_happy_path_collects_filter_and_outputs() -> None:
    normalized = normalize_plan(_plan(), field_types=_FIELD_TYPES)
    assert normalized["filters"] == [
        {"name": "region", "type": "text", "field": "region"}]
    query = normalized["blocks"][1]
    assert query["columns"] == [
        {"field": "region", "agg": None, "alias": "region"},
        {"field": None, "agg": "count", "alias": "orders"},
        {"field": "amount", "agg": "sum", "alias": "total"},
    ]
    # plan_json 的存储形态必须能继续经 ReportPlan 回读（update_article 换数据集
    # 时的重校验依赖这一往返）。
    assert normalize_plan(ReportPlan.model_validate(normalized),
                          field_types=_FIELD_TYPES) == normalized


def test_normalize_rejects_unknown_field_and_type_misalignment() -> None:
    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(blocks=[{"type": "query", "name": "q", "columns": [
            {"field": "nope"}]}]), field_types=_FIELD_TYPES)
    assert any("不在数据集字段白名单内" in e for e in exc.value.errors)

    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(blocks=[{"type": "query", "name": "q", "columns": [
            {"agg": "sum", "field": "region"}]}]), field_types=_FIELD_TYPES)
    assert any("只支持数值列" in e for e in exc.value.errors)

    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(filters=[{"name": "r", "type": "number", "field": "region"}]),
                       field_types=_FIELD_TYPES)
    assert any("不兼容" in e for e in exc.value.errors)

    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(blocks=[{"type": "query", "name": "q", "columns": [
            {"field": "region"}], "time_bucket": {"field": "region", "unit": "day"}}]),
            field_types=_FIELD_TYPES)
    assert any("时间桶字段" in e for e in exc.value.errors)


def test_normalize_rejects_fences_html_and_blank_text() -> None:
    for text in ("正文\n```sql evil\nSELECT 1\n```", "换行围栏\n~~~\n", "带 <b>标签</b>", "   "):
        with pytest.raises(PlanValidationError):
            normalize_plan(_plan(blocks=[{"type": "text", "text": text}]),
                           field_types=_FIELD_TYPES)


def test_normalize_caps_query_blocks_at_execution_budget() -> None:
    blocks = [{"type": "query", "name": f"q{index}",
               "columns": [{"field": "region"}]} for index in range(21)]
    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(filters=[], blocks=blocks), field_types=_FIELD_TYPES)
    assert any("查询块数量不能超过 20" in e for e in exc.value.errors)


def test_normalize_chart_contract_rules() -> None:
    cases = [
        {"chart": "kpi", "query": "q1", "x": "region", "y": ["total"]},   # kpi 不支持 x
        {"chart": "pie", "query": "q1", "x": "region", "y": []},          # pie 需要恰好一个 y
        {"chart": "bar", "query": "q1", "x": "region", "y": []},          # bar 至少一个 y
        {"chart": "table", "query": "q1", "x": "region"},                 # table 不支持 x
        {"chart": "bar", "query": "missing", "x": "region", "y": ["total"]},
        {"chart": "bar", "query": "q1", "x": "amount", "y": ["total"]},   # x 不是输出列
    ]
    for case in cases:
        blocks = [
            {"type": "query", "name": "q1", "columns": [
                {"field": "region"}, {"agg": "sum", "field": "amount", "alias": "total"}]},
            {"type": "chart", **case},
        ]
        with pytest.raises(PlanValidationError):
            normalize_plan(_plan(filters=[], blocks=blocks), field_types=_FIELD_TYPES)


def test_normalize_generates_stable_legal_aliases_for_non_ascii_fields() -> None:
    plan = _plan(
        filters=[],
        blocks=[
            {"type": "query", "name": "q", "columns": [
                {"field": "区域"}, {"field": "region"},
                {"agg": "sum", "field": "amount"}],
             "time_bucket": {"field": "下单时间", "unit": "month"}},
            {"type": "chart", "chart": "bar", "query": "q",
             "x": "col_1", "y": ["sum_amount"]},
        ],
    )
    normalized = normalize_plan(plan, field_types=_FIELD_TYPES)
    query = normalized["blocks"][0]
    assert [column["alias"] for column in query["columns"]] == [
        "col_1", "region", "sum_amount"]
    assert query["time_bucket"]["alias"] == "bucket_2"
    # 稳定性：同一计划重复规范化结果一致。
    assert normalize_plan(ReportPlan.model_validate(normalized),
                          field_types=_FIELD_TYPES) == normalized
    markdown = plan_markdown(normalized)
    assert validate_markdown(markdown).ok
    assert 'AS "col_1"' in markdown
    assert 'AS "bucket_2"' in markdown


def test_normalize_rejects_oversized_alias_and_unsafe_source_field() -> None:
    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(blocks=[{"type": "query", "name": "q", "columns": [
            {"field": "region", "alias": "a" * 64}]}]), field_types=_FIELD_TYPES)
    assert any("63 字节" in e for e in exc.value.errors)

    hostile = "x\n```sql evil"
    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(blocks=[{"type": "query", "name": "q", "columns": [
            {"field": hostile}]}]), field_types={hostile: "string"})
    assert any("围栏标记" in e for e in exc.value.errors)


def test_normalize_rejects_duplicate_query_and_output_names() -> None:
    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(blocks=[
            {"type": "query", "name": "q", "columns": [{"field": "region"}]},
            {"type": "query", "name": "q", "columns": [{"field": "amount"}]},
        ]), field_types=_FIELD_TYPES)
    assert any("查询名重复" in e for e in exc.value.errors)

    with pytest.raises(PlanValidationError) as exc:
        normalize_plan(_plan(blocks=[{"type": "query", "name": "q", "columns": [
            {"field": "region"}, {"field": "region", "alias": "region"}]}]),
            field_types=_FIELD_TYPES)
    assert any("输出列重复" in e for e in exc.value.errors)


def test_compile_count_distinct_uses_count_function() -> None:
    normalized = normalize_plan(_plan(blocks=[{"type": "query", "name": "q", "columns": [
        {"agg": "count_distinct", "field": "region"}]}]), field_types=_FIELD_TYPES)
    sql = compile_query_sql(normalized["blocks"][0], [])
    assert 'COUNT(DISTINCT "region")' in sql
    assert "COUNT_DISTINCT" not in sql
    assert check_report_sql(sql).ok


def test_compile_always_emits_bounded_limit() -> None:
    def sql_for(limit: int | None, result_limit: int | None) -> str:
        normalized = normalize_plan(_plan(blocks=[{"type": "query", "name": "q", "columns": [
            {"field": "region"}], **({"limit": limit} if limit is not None else {})}]),
            field_types=_FIELD_TYPES)
        return compile_query_sql(normalized["blocks"][0], [],
                                 result_limit=result_limit)

    assert sql_for(None, None).endswith(f"LIMIT {QUERY_DEFAULT_LIMIT}")
    assert sql_for(50, None).endswith("LIMIT 50")
    assert sql_for(50, 1000).endswith("LIMIT 50")
    # 请求级预算必须收紧 SQL 内 LIMIT，否则执行器把可执行的查询误报超预算。
    assert sql_for(200, 100).endswith("LIMIT 100")
    assert sql_for(None, 100).endswith("LIMIT 100")
    # 预算高于默认上限时保持计划默认（与阅读 execute 的默认一致）。
    assert sql_for(None, 1000).endswith(f"LIMIT {QUERY_DEFAULT_LIMIT}")


def test_compile_filter_casts_by_declared_type() -> None:
    plan = _plan(filters=[
        {"name": "region", "type": "text", "field": "region"},
        {"name": "min_amount", "type": "number", "field": "amount"},
        {"name": "d", "type": "date", "field": "day"},
    ])
    normalized = normalize_plan(plan, field_types=_FIELD_TYPES)
    sql = compile_query_sql(normalized["blocks"][1], normalized["filters"])
    assert '(:region IS NULL OR "region" = :region)' in sql
    assert '(:min_amount IS NULL OR CAST("amount" AS NUMERIC) = :min_amount)' in sql
    assert '(:d IS NULL OR CAST("day" AS DATE) = :d)' in sql
    check = check_report_sql(sql)
    assert check.ok, check.errors
    assert set(check.parameters) == {"region", "min_amount", "d"}


def test_compile_time_bucket_groups_and_orders_by_stored_alias() -> None:
    plan = _plan(blocks=[{"type": "query", "name": "q", "columns": [
        {"agg": "count", "alias": "orders"}],
        "time_bucket": {"field": "created_at", "unit": "day"},
        "sort": [{"field": "created_at__day", "dir": "asc"}]}])
    normalized = normalize_plan(plan, field_types=_FIELD_TYPES)
    sql = compile_query_sql(normalized["blocks"][0], [])
    assert "DATE_TRUNC('day', \"created_at\") AS \"created_at__day\"" in sql
    assert "GROUP BY DATE_TRUNC('day', \"created_at\")" in sql
    assert 'ORDER BY "created_at__day" ASC' in sql
    assert check_report_sql(sql).ok


def test_compile_plan_queries_carries_result_limit() -> None:
    normalized = normalize_plan(
        _plan(blocks=[
            {"type": "query", "name": "a", "columns": [{"field": "region"}]},
            {"type": "query", "name": "b", "columns": [{"field": "amount"}], "limit": 10},
        ]),
        field_types=_FIELD_TYPES)
    queries = compile_plan_queries(normalized, result_limit=50)
    assert set(queries) == {"a", "b"}
    assert queries["a"].endswith("LIMIT 50")
    assert queries["b"].endswith("LIMIT 10")
    for sql in queries.values():
        assert check_report_sql(sql).ok


def test_validate_plan_params_type_rules() -> None:
    filters = [
        {"name": "region", "type": "text", "field": "region"},
        {"name": "min_amount", "type": "number", "field": "amount"},
        {"name": "d", "type": "date", "field": "day"},
    ]
    assert validate_plan_params(filters, {
        "region": "east", "min_amount": 15, "d": "2026-01-01"}) == []
    # NULL 表示不过滤，恒放行。
    assert validate_plan_params(filters, {"region": None, "min_amount": None}) == []
    errors = validate_plan_params(filters, {
        "region": 5, "min_amount": "15", "d": 20260101, "other": True})
    assert len(errors) == 3
    assert any("region" in e and "字符串" in e for e in errors)
    assert any("min_amount" in e and "数字" in e for e in errors)
    assert any("d" in e and "日期字符串" in e for e in errors)
    assert validate_plan_params(filters, {"min_amount": True}) != []
    assert validate_plan_params(filters, {"min_amount": float("nan")}) != []
