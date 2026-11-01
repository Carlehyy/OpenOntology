"""Shared numeric text parsing for typed value coercion.

Lake snapshots store numbers as text; projection coercion must decide int
vs float without a float64 round-trip, because large integral values
(snowflake IDs, NUMERIC(38, x) columns) silently lose precision above 2^53.
Fractional (and scale-bearing integral) literals parse to exact ``Decimal``
so the source scale survives the JSON storage token (typed/decimal_string
contract) and aggregate SUM keeps the uploaded literal scale.
"""
from __future__ import annotations

import decimal
from decimal import Decimal

# 与 float64 可表示量级对齐的上限：超过此量级的精确值不做精确路径，
# 照旧落 float，由实例契约的 isfinite 校验按旧行为拦截。
_FLOAT64_LIMIT = Decimal("1e308")
_INT_LIMIT = int(_FLOAT64_LIMIT)
# 数值文本长度上限：超长输入（对抗载荷/脏数据）直接判非法，不进入解析。
_MAX_TEXT_LENGTH = 1_000_000


def parse_numeric_text(text: str) -> int | float | Decimal:
    """Parse numeric text int-first; non-integral literals keep exact ``Decimal``.

    纯整数字面（"9007199254740993"）→ 精确 int；带小数/指数/尾零字面
    （"123.45" / "100.5000" / "7.0000" / "9007199254740993.00" / "1e3"）
    → 精确 ``Decimal``，源字面与 scale 逐字保留，投影落库时按 exact JSON
    数值 token 写出，查询 SUM 才能保 scale；"nan"/"inf"/garbage 遵循
    float() 语义（非数值文本抛 ValueError），由下游契约校验按旧行为处理。

    所有精确路径都受 _FLOAT64_LIMIT 量级守卫：对抗性巨型数字串（数百位
    以上）不会以巨型 int/Decimal 击穿实例契约校验，而是照旧转 inf 被
    契约拒绝。

    兼容口径：``Decimal`` 与 int/float 的相等/大小比较原生可用，但**算术**
    不能直接和 float 混合（``Decimal + float`` 抛 TypeError）。表达式/派生
    函数的数值域仍按改动前的 float 语义在 function_engine 边界归一，不能
    把这里的 Decimal 当成"处处可混算"的数值。
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
    # 精确判定块整体纳入捕获：abs() 在默认 context（Emax=999999）下是
    # 上下文敏感运算，37 字符的科学计数载荷即可触发 decimal.Overflow ——
    # 一律回落 float() 语义，保持旧行为。
    try:
        dec = Decimal(cleaned)
        if not dec.is_finite() or abs(dec) > _FLOAT64_LIMIT:
            return float(cleaned)
        # 小数/科学计数字面保持精确 Decimal：不经 float64、不折叠尾零
        # （"7.0000" 的 scale-4 token 是统一 SUM 保 scale 的一部分，
        # "9007199254740993.00" 的精度+scale 同样逐字保留）。
        return dec
    except decimal.DecimalException:
        # 非数值/对抗载荷：交给 float() 抛 ValueError 或产出 nan|inf，
        # 由实例契约校验按旧行为拒绝。
        return float(cleaned)


def _is_numeric_value(value) -> bool:
    """int/float/Decimal 属于数值域；bool 保持独立类型语义。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def numeric_values_equal(left, right) -> bool:
    """按数值比较 int/float/Decimal（``Decimal("0.1") == 0.1``）。

    Python 的 ``Decimal == float`` 是"精确二进制值比较"：``Decimal("0.1")``
    与 float ``0.1`` 不相等。投影重跑时旧值从 JSON 读回是 float、新值经
    coercion 是 Decimal，若直接用 ``!=`` 判变更会把同一业务值误判为变化，
    造成重复 facts/CDC 事件。这里统一走 ``Decimal(str(value))`` 的正常数值
    比较（非数值对返回 False，bool 不与 1 混同）。
    """
    if not (_is_numeric_value(left) and _is_numeric_value(right)):
        return False
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (ValueError, ArithmeticError):
        return left == right


def values_equal(left, right) -> bool:
    """JSON 形值的语义相等：容器递归，数值跨 int/float/Decimal 归一比较。"""
    if isinstance(left, dict) and isinstance(right, dict):
        return (left.keys() == right.keys()
                and all(values_equal(left[key], right[key]) for key in left))
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return (len(left) == len(right)
                and all(values_equal(a, b) for a, b in zip(left, right)))
    if _is_numeric_value(left) or _is_numeric_value(right):
        return numeric_values_equal(left, right)
    return left == right
