"""Shared numeric text parsing for typed value coercion.

Lake snapshots store numbers as text; projection coercion must decide int
vs float without a float64 round-trip, because large integral values
(snowflake IDs, NUMERIC(38, x) columns) silently lose precision above 2^53.
"""
from __future__ import annotations

import decimal
import math
from decimal import Decimal

# 与 float64 可表示量级对齐的上限：超过此量级的整值不做 int 精确路径，
# 照旧落 float，由实例契约的 isfinite 校验按旧行为拦截。
_FLOAT64_LIMIT = Decimal("1e308")
_INT_LIMIT = int(_FLOAT64_LIMIT)
# 数值文本长度上限：超长输入（对抗载荷/脏数据）直接判非法，不进入解析。
_MAX_TEXT_LENGTH = 1_000_000


def parse_numeric_text(text: str) -> int | float:
    """Parse numeric text int-first, falling back to float for true decimals.

    "9007199254740993" / "9007199254740993.00" → exact int（整值判定走
    Decimal 任意精度，不经 float64 中转）；"123.45" → float；
    "nan"/"inf"/garbage 遵循 float() 语义，由下游契约校验按旧行为处理。
    非数值文本抛 ValueError。

    所有整值路径都受 _FLOAT64_LIMIT 量级守卫：对抗性巨型数字串（数百位
    以上）不会以巨型 int 击穿实例契约校验（float() 转换 OverflowError），
    而是照旧转 inf 被契约拒绝。
    """
    cleaned = str(text).strip().replace(",", "")
    if len(cleaned) > _MAX_TEXT_LENGTH:
        raise ValueError(
            f"数值文本超长（{len(cleaned)} > {_MAX_TEXT_LENGTH} 字符）")
    try:
        value = int(cleaned)
    except ValueError:
        pass
    else:
        if -_INT_LIMIT <= value <= _INT_LIMIT:
            return value
    # Decimal 判定块整体纳入捕获：abs()/比较在默认 context（Emax=999999）
    # 下是上下文敏感运算，37 字符的科学计数载荷即可触发 decimal.Overflow
    try:
        dec = Decimal(cleaned)
        integral = (dec.is_finite() and dec == dec.to_integral_value()
                    and abs(dec) <= _FLOAT64_LIMIT)
    except decimal.DecimalException:
        integral = False
    if integral:
        return int(dec)
    f = float(cleaned)
    # isfinite 同时消除对 Python 版本的依赖（<=3.11 的 inf.is_integer() 为 True）
    if not math.isfinite(f) or not f.is_integer():
        return f
    return int(f)
