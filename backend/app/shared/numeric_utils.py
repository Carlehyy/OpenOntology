"""Shared numeric text parsing for typed value coercion.

Lake snapshots store numbers as text; projection coercion must decide int
vs float without a float64 round-trip, because large integral values
(snowflake IDs, NUMERIC(38, x) columns) silently lose precision above 2^53.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

# 与 float64 可表示量级对齐的上限：超过此量级的整值不走 int 精确路径，
# 照旧落 float，由实例契约的 isfinite 校验按旧行为拦截。
_FLOAT64_LIMIT = Decimal("1e308")


def parse_numeric_text(text: str) -> int | float:
    """Parse numeric text int-first, falling back to float for true decimals.

    "9007199254740993" / "9007199254740993.00" → exact int（整值判定走
    Decimal 任意精度，不经 float64 中转）；"123.45" → float；
    "nan"/"inf"/garbage 遵循 float() 语义，由下游契约校验按旧行为处理。
    非数值文本抛 ValueError。
    """
    cleaned = str(text).strip().replace(",", "")
    try:
        return int(cleaned)
    except ValueError:
        pass
    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        dec = None
    if (dec is not None and dec.is_finite()
            and dec == dec.to_integral_value()
            and abs(dec) <= _FLOAT64_LIMIT):
        return int(dec)
    f = float(cleaned)
    return int(f) if f.is_integer() else f
