import pytest

from app.data_channel.query.report_sql import check_report_sql


def test_report_sql_accepts_single_dataset_cte_aggregate_and_named_parameter():
    checked = check_report_sql(
        "WITH daily AS ("
        "SELECT region, COUNT(*) AS total FROM dataset "
        "WHERE (:region IS NULL OR region = :region) GROUP BY region) "
        "SELECT region, total, ROW_NUMBER() OVER (ORDER BY total DESC) AS rank "
        "FROM daily ORDER BY total DESC"
    )
    assert checked.ok, checked.errors
    assert checked.parameters == ("region",)


def test_report_sql_rejects_physical_tables_and_cross_dataset_references():
    checked = check_report_sql("SELECT * FROM dataset JOIN another_table ON true")
    assert not checked.ok
    assert any("跨数据集" in error or "another_table" in error for error in checked.errors)


def test_report_sql_rejects_mutation_and_position_parameters():
    for sql in (
        "DELETE FROM dataset",
        "SELECT * INTO report_copy FROM dataset",
        "SELECT * FROM dataset WHERE id = $1",
        "SELECT pg_sleep(5) FROM dataset",
    ):
        checked = check_report_sql(sql)
        assert not checked.ok, sql


def test_report_sql_rejects_recursive_cte():
    checked = check_report_sql(
        "WITH RECURSIVE nodes AS (SELECT 1 FROM dataset UNION ALL SELECT 1 FROM nodes) "
        "SELECT * FROM nodes"
    )
    assert not checked.ok
    assert any("RECURSIVE" in error for error in checked.errors)


def test_report_sql_requires_dataset_relation():
    checked = check_report_sql("SELECT 1")
    assert not checked.ok
    assert any("dataset" in error for error in checked.errors)


@pytest.mark.parametrize("sql", [
    "WITH users AS (SELECT * FROM users) SELECT * FROM users CROSS JOIN dataset",
    "WITH first AS (SELECT * FROM users), users AS (SELECT * FROM dataset) SELECT * FROM first",
    "SELECT * FROM users CROSS JOIN (WITH users AS (SELECT * FROM dataset) SELECT * FROM users) x",
    "WITH pg_shadow AS (SELECT * FROM pg_shadow) SELECT * FROM pg_shadow CROSS JOIN dataset",
    "SELECT * FROM public.dataset",
    "SELECT * FROM dataset JOIN public.users u ON true",
    "SELECT evil_schema.replace(region, 'a', 'b') FROM dataset",
    "SELECT region::evil.custom_type FROM dataset",
    "SELECT region::regclass FROM dataset",
    "SELECT * FROM (WITH RECURSIVE x AS (SELECT * FROM dataset UNION ALL SELECT * FROM x) SELECT * FROM x) y",
    "SELECT * FROM dataset; SELECT * FROM dataset",
    "SELECT * FROM dataset CROSS JOIN generate_series(1,100) x",
])
def test_report_sql_rejects_scope_and_function_escape(sql):
    checked = check_report_sql(sql)
    assert not checked.ok, sql


@pytest.mark.parametrize("expression", [
    "CASE WHEN total > 0 THEN 'yes' ELSE 'no' END",
    "DATE_TRUNC('day', created_at)",
    "STRING_AGG(region, ',')", "TO_CHAR(created_at, 'YYYY-MM')",
    "TO_DATE(region, 'YYYY-MM-DD')", "TO_TIMESTAMP(region, 'YYYY-MM-DD')",
    "POSITION('x' IN region)", "STRPOS(region, 'x')", "BOOL_AND(total > 0)",
    "BOOL_OR(total > 0)", "VAR_POP(total)", "SHA256(region::bytea)",
])
def test_report_sql_accepts_normalized_analytics_functions(expression):
    checked = check_report_sql(f"SELECT {expression} FROM dataset")
    assert checked.ok, checked.errors


def test_report_sql_accepts_nested_scopes_and_reused_cte_names():
    checked = check_report_sql(
        "WITH summary AS (SELECT region FROM dataset) "
        "SELECT * FROM summary UNION ALL "
        "SELECT * FROM (WITH summary AS (SELECT region FROM dataset) SELECT * FROM summary) nested"
    )
    assert checked.ok, checked.errors
