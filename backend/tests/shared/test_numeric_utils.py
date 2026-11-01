"""shared.numeric_utils.parse_numeric_text 单元测试。

钉住两级精确判定：int（纯整数字面）→ 精确 Decimal（一切带小数/科学计数
字面，不经 float64、不折叠尾零——"7.0000" 的 scale-4 token 是统一 SUM
保 scale 的一部分）→ float（超量级/nan/inf/garbage 按 float() 语义交给
契约校验）。
"""
import pytest

from app.shared.numeric_utils import parse_numeric_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9007199254740993", 9007199254740993),
        # numeric 列归一化形态：精度与 scale 都逐字保留（不再折叠成 int）
        ("9007199254740993.00", "9007199254740993.00"),
        ("9,007,199,254,740,993", 9007199254740993),
        ("9223372036854775807.000", "9223372036854775807.000"),
        ("-9007199254740993.00", "-9007199254740993.00"),
        ("123.45", "123.45"),
        ("0.1", "0.1"),
        ("3.0", "3.0"),
        ("1e3", "1E+3"),
        ("12_000", 12000),  # Python 数值下划线语义，float() 路径本就接受
        ("+7", 7),
        (" 42 ", 42),
        # goal §8 严格金额：源 scale 字面逐字保留，绝不 100.5000→100.5
        ("100.5000", "100.5000"),
        ("50.2500", "50.2500"),
        ("7.0000", "7.0000"),
    ],
)
def test_parse_numeric_text_exact_precision(raw, expected):
    parsed = parse_numeric_text(raw)
    if isinstance(expected, int):
        assert parsed == expected
        assert isinstance(parsed, int)
    else:
        from decimal import Decimal
        assert parsed == Decimal(expected)
        assert isinstance(parsed, Decimal)
        # 字面与 scale 逐字相等（exponent 即 scale 的负数形态）
        assert str(parsed) == expected


def test_parse_numeric_text_keeps_float_semantics_for_extremes():
    # 超 float64 量级的整值不做精确路径：照旧 inf/float，由实例契约拦截
    import math

    assert math.isinf(parse_numeric_text("1e400"))
    assert math.isnan(parse_numeric_text("nan"))
    assert parse_numeric_text("-inf") == float("-inf")


def test_parse_numeric_text_decimal_compares_natively_with_int_float():
    """Decimal 与 int/float 的比较运算原生兼容（哨兵/动作数值条件语义不变）。"""
    parsed = parse_numeric_text("100.5000")
    assert parsed == 100.5
    assert parsed > 100
    assert parsed < 101
    assert parse_numeric_text("3.0") == 3


def test_parse_numeric_text_rejects_garbage():
    for raw in ("abc", "", "12.34.56"):
        with pytest.raises(ValueError):
            parse_numeric_text(raw)


def test_parse_numeric_text_strips_thousand_separators():
    # "1," 经千分位剥离为 "1"，与既有 float 路径语义一致
    assert parse_numeric_text("1,") == 1


def test_parse_numeric_text_giant_digits_do_not_breach_contract():
    """对抗回归：数百位数字串不得以巨型 int/Decimal 击穿实例契约的 float()
    转换（OverflowError 未捕获会让投影以未分类异常崩溃），照旧转 inf 拒绝。"""
    import math

    for length in (400, 4300, 5000):
        assert math.isinf(parse_numeric_text("9" * length))


def test_parse_numeric_text_rejects_overlong_input():
    with pytest.raises(ValueError, match="超长"):
        parse_numeric_text("9" * 1_000_001)


def test_parse_numeric_text_context_overflow_payload_returns_inf():
    """对抗复核回归：37 字符的科学计数载荷（28 个 9 + e999999）让 abs(dec)
    在默认 context 下抛 decimal.Overflow——运算块必须整体纳入捕获，照旧
    返回 inf 交给契约拒绝，而不是逃出未捕获异常。"""
    import math

    assert math.isinf(parse_numeric_text("9." + "9" * 27 + "e999999"))
