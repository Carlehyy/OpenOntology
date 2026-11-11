from __future__ import annotations

import time

from app.operations.markdown import parse_blocks, validate_markdown


def test_parse_blocks_keeps_headings_inside_sql_fence_out_of_content_blocks():
    result = validate_markdown(
        "# 标题\n\n正文\n\n```sql daily\nSELECT * FROM dataset\n# not a heading\n```\n"
        "\n```chart\nquery: daily\ntype: line\nx: day\ny: total\n```\n"
    )
    assert result.ok
    assert [block.kind for block in result.blocks] == ["prose", "sql", "chart"]
    assert result.blocks[1].name == "daily"


def test_validate_markdown_rejects_duplicate_queries_and_broken_chart_reference():
    result = validate_markdown(
        "```sql daily\nSELECT 1 FROM dataset\n```\n"
        "```sql daily\nSELECT 2 FROM dataset\n```\n"
        "```chart\nquery: missing\ntype: table\n```\n"
    )
    assert not result.ok
    assert any("重复" in error for error in result.errors)
    assert any("不存在" in error for error in result.errors)


def test_validate_markdown_rejects_unsupported_chart_and_filter():
    result = validate_markdown(
        "```sql daily\nSELECT * FROM dataset WHERE region = :region\n```\n"
        "```filter\nname: region\ntype: boolean\n```\n"
        "```chart\nquery: daily\ntype: radar\n```\n"
    )
    assert not result.ok
    assert any("筛选类型" in error for error in result.errors)
    assert any("图表类型" in error for error in result.errors)


def test_parse_blocks_preserves_unclosed_fence_as_prose():
    blocks = parse_blocks("开头\n```sql query\nSELECT 1 FROM dataset\n")
    assert len(blocks) == 1
    assert blocks[0].kind == "prose"
    assert "SELECT 1" in blocks[0].body


def test_chart_config_lines_follow_frontend_key_value_syntax():
    result = validate_markdown(
        "```sql daily_rev\nSELECT 1 FROM dataset\n```\n"
        "```chart\n"
        "  query :  daily_rev  \n"
        "type:bar\n"
        "x:day\n"
        "y: total , orders \n"
        "sql_text: SELECT a : b FROM t\n"
        "1bad: ignored\n"
        "- dash: ignored\n"
        "not a config line\n"
        "   \n"
        "```\n"
    )
    assert result.ok, result.errors
    block = result.blocks[-1]
    assert block.kind == "chart"
    assert block.config == {
        "query": "daily_rev",
        "type": "bar",
        "x": "day",
        "y": "total , orders",
        "sql_text": "SELECT a : b FROM t",
    }


def test_chart_config_line_duplicate_key_keeps_last_value():
    result = validate_markdown(
        "```sql q\nSELECT 1 FROM dataset\n```\n"
        "```chart\nquery: q\ntype: table\ntype: bar\nx: day\n```\n"
    )
    assert result.ok, result.errors
    assert result.blocks[-1].config["type"] == "bar"


def test_config_lines_with_long_whitespace_stay_linear():
    # `key:` + N 空白 + 字符 + N 空白 + 字符曾让贪婪 \s* 与懒惰 .*? 互相
    # 回溯（O(N^2)）；等价改写后必须保持线性，超时上限远高于线性耗时、
    # 远低于旧实现的回溯耗时（N=100_000 时旧正则约 15s，新实现 <5ms）。
    spaces = " " * 100_000
    start = time.perf_counter()
    result = validate_markdown(
        "```sql q\nSELECT 1 FROM dataset\n```\n"
        "```chart\n"
        "query: q\n"
        "type: table\n"
        f"note:{spaces}a{spaces}a\n"
        f"{spaces}\n"
        "```\n"
    )
    elapsed = time.perf_counter() - start
    assert result.ok, result.errors
    assert result.blocks[-1].config["note"] == f"a{spaces}a"
    assert elapsed < 5.0
