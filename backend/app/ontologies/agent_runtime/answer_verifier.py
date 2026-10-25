"""确定性结论校验：数值须由完整工具证据背书；失败先纠正，仍失败则显式标注。

保留历史年份豁免及百分比变体；Decimal 内部比较不会混淆相邻 bigint。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Iterable

# 数字抽取：覆盖 整数 / 小数 / 千分位 / 负号。年份先整体剥离（否则 "2026"
# 会被切成 202+6），版本号等含 4 位数字的字符串也会被剥离年份部分——可接受的
# 误伤，宁可少校验也不把年份当数据断言。
_NUMBER_RE = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
JS_SAFE_INTEGER = 2**53 - 1


@dataclass
class VerificationResult:
    """一次校验的结构化结果（可序列化进 answer 事件与审计）。"""
    answer_numbers: list[Decimal] = field(default_factory=list)
    unverified: list[Decimal] = field(default_factory=list)
    matched: list[Decimal] = field(default_factory=list)
    retried: bool = False

    @property
    def passed(self) -> bool:
        return not self.unverified

    def summary(self) -> dict:
        return {
            "passed": self.passed,
            "answerNumberCount": len(self.answer_numbers),
            "unverified": [_json_number(value) for value in self.unverified],
            "retried": self.retried,
        }


def _json_number(value) -> int | float | str:
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return str(value)
    if value == value.to_integral_value() and abs(value) <= JS_SAFE_INTEGER:
        return int(value)
    # Preserve the old numeric summary when it round-trips exactly; otherwise
    # use a lossless decimal string, never a rounded proof of a different claim.
    if abs(value) > JS_SAFE_INTEGER or not value.is_finite():
        return str(value)
    number = float(value)
    return number if Decimal(str(number)) == value else str(value)


def _public_number(value: Decimal):
    """Use historical float aliases only when their decimal spelling is exact."""
    if abs(value) <= JS_SAFE_INTEGER:
        number = float(value)
        if Decimal(str(number)) == value:
            return number
    return value


def _numbers_in_text_decimal(text: str) -> list[Decimal]:
    cleaned = _YEAR_RE.sub(" ", text or "")
    return [Decimal(raw.replace(",", ""))
            for raw in _NUMBER_RE.findall(cleaned)]


def _numbers_in_value(value: object) -> list[Decimal]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
            return [number] if number.is_finite() else []
        except InvalidOperation:
            return []
    if isinstance(value, str):
        return _numbers_in_text_decimal(value)
    if isinstance(value, (dict, list, tuple)):
        children = value.values() if isinstance(value, dict) else value
        return [number for child in children for number in _numbers_in_value(child)]
    return []


def _with_ratio_variants(numbers: Iterable[Decimal]) -> set[Decimal]:
    allowed: set[Decimal] = set()
    for number in numbers:
        allowed.add(number)
        # Ratio variants are a display convenience for ordinary magnitudes,
        # never a reason to round a bigint or allocate unbounded exponent work.
        if abs(number) > JS_SAFE_INTEGER or abs(number.adjusted()) > 100:
            continue
        with localcontext() as context:
            context.prec = max(28, len(number.as_tuple().digits) + 108)
            allowed.add(round(number * 100, 2))
            if number != 0:
                allowed.add(round(number / 100, 4))
    return allowed


def result_number_universe(steps: Iterable[dict]) -> set:
    """完整工具结果的 Decimal 数值集合及无损历史 float 别名（含百分比变体）。"""
    numbers: list[Decimal] = []
    for step in steps:
        result = step.get("result")
        if not isinstance(result, dict):
            continue
        if (result.get("error") or result.get("_truncated") or result.get("partial")
                or result.get("exact") is False
                or result.get("completeness", "complete") != "complete"):
            # 出错/不完整的输入不能作为精确数值背书；输出分页本身不等于输入不完整。
            continue
        numbers.extend(_numbers_in_value(result))
    allowed = _with_ratio_variants(numbers)
    # Preserve compatibility for exact, safe values returned by this helper;
    # verification itself uses Decimal values below and remains lossless.
    for value in tuple(allowed):
        public = _public_number(value)
        if isinstance(public, float):
            allowed.add(public)
    return allowed


def answer_numbers(answer: str) -> list:
    """回答文本中的数值断言（保留出现顺序与重复）。"""
    return [_public_number(value) for value in _numbers_in_text_decimal(answer)]


def verify_answer(answer: str, steps: Iterable[dict]) -> VerificationResult:
    universe = result_number_universe(steps)
    numbers = _numbers_in_text_decimal(answer)
    result = VerificationResult(answer_numbers=numbers)
    for number in numbers:
        (result.matched if number in universe else result.unverified).append(number)
    return result


def correction_prompt(unverified: Iterable[Decimal]) -> str:
    values = "、".join(str(_json_number(value)) for value in unverified)
    return (
        f"# 结论校验失败\n"
        f"你刚才回答中的以下数字未能对应到任何工具返回结果，"
        f"不得编造数据：{values}。\n"
        f"请重新作答：只保留能对应到工具结果的数字；若确实缺少数据，"
        f"明确说明缺少什么并删除无法佐证的数字。不要新增其它工具调用。"
    )


def unverified_notice(unverified: Iterable[Decimal]) -> str:
    values = "、".join(str(_json_number(value)) for value in unverified)
    return (
        f"\n\n> ⚠️ 结论校验：以下数字未能与工具返回结果对应，请自行核对后再使用：{values}。"
    )
