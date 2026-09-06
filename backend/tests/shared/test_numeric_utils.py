"""shared.numeric_utils.parse_numeric_text 单元测试。

钉住三级整值判定：int → Decimal（带 ".00" 尾缀的大整数不经 float64）→
float（真小数/极端值按 float() 语义交给契约校验）。
"""
import pytest

from app.shared.numeric_utils import parse_numeric_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9007199254740993", 9007199254740993),
        ("9007199254740993.00", 9007199254740993),  # numeric 列归一化形态
        ("9,007,199,254,740,993", 9007199254740993),
        ("9223372036854775807.000", 9223372036854775807),
        ("-9007199254740993.00", -9007199254740993),
        ("123.45", 123.45),
        ("0.1", 0.1),
        ("3.0", 3),
        ("1e3", 1000),
        ("12_000", 12000),  # Python 数值下划线语义，float() 路径本就接受
        ("+7", 7),
        (" 42 ", 42),
    ],
)
def test_parse_numeric_text_integral_precision(raw, expected):
    assert parse_numeric_text(raw) == expected
    if isinstance(expected, int):
        assert isinstance(parse_numeric_text(raw), int)


def test_parse_numeric_text_keeps_float_semantics_for_extremes():
    # 超 float64 量级的整值不做精确路径：照旧 inf/float，由实例契约拦截
    import math

    assert math.isinf(parse_numeric_text("1e400"))
    assert math.isnan(parse_numeric_text("nan"))
    assert parse_numeric_text("-inf") == float("-inf")


def test_parse_numeric_text_rejects_garbage():
    for raw in ("abc", "", "12.34.56"):
        with pytest.raises(ValueError):
            parse_numeric_text(raw)


def test_parse_numeric_text_strips_thousand_separators():
    # "1," 经千分位剥离为 "1"，与既有 float 路径语义一致
    assert parse_numeric_text("1,") == 1
