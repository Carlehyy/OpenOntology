"""确定性结论校验：数值须由完整工具证据背书；失败先纠正，仍失败则显式标注。

保留历史年份豁免、日期整体屏蔽与百分比变体；Decimal 比较不混淆相邻 bigint。
引用编号与标识符（UUID/时间戳/Id 类键）既不算数值断言，也不充当证据。
答案侧额外屏蔽排版序号（1./1、/（1）/第N）、时钟、空格分隔日期时间、
编号/版本号（SO-001、v1.2.3）与数字区间 —— 它们不是数值断言。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Iterable

# 日期须先整体屏蔽，避免年份剥离后残留月份充当计数或证据。
# 不将小数点视为日期分隔符；数量与「5个月」等时长仍须核对。
_NUMBER_RE = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_DATE_RE = re.compile(
    r"(?<!\d)(?:(?:19|20)\d{2}(?:\s*[-/]\s*\d{1,2}){1,2}"
    r"|(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?)?"
    r"|\d{1,2}\s*月份?(?:\s*\d{1,2}\s*[日号])?)(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# 引用编号（[1]、[2,3]、「4」）是排版标记，不是数值断言。
_CITE_RE = re.compile(r"\[\d+(?:\s*[,，、]\s*\d+)*\]|[「『]\d+[」』]")
# UUID 与 ISO 时间戳是标识符：其数字片段不得进入允许集合，也不触发告警。
_UUID_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])")
_STAMP_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                       r"(?:Z|[+-]\d{2}:?\d{2})?(?!\d)")
# 工具结果顶层的可重放查询计划/页长是请求参数，不是观测证据；计划里的
# pageSize、过滤值/params 不能为回答中的数字断言背书（业务行内同名字段不受影响）。
_REQUEST_METADATA_KEYS = frozenset({"querySpec", "pageSize", "page_size"})
JS_SAFE_INTEGER = 2**53 - 1

# 答案侧排版/标识符屏蔽（仅用于答案文本；工具证据集合保持原清洗语义）：
# 有序列号（1. / 1、/（1）/第N）、时钟、空格分隔日期时间、带标签或含字母的
# 编号/版本（SO-001、v1.2.3、IPv6）与数字区间（1-2、010-12345678）是排版
# 或标识信息，不构成数值断言 —— 引用编号/UUID/ISO 时间戳屏蔽的延伸。
_ANSWER_MASKS: tuple[tuple[re.Pattern, str], ...] = (
    (_UUID_RE, " "),
    (_STAMP_RE, " "),
    # 空格分隔的日期时间整体屏蔽（先于日期与时钟，避免拆出 10:20:30）。
    (re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?!\d)"), " "),
    (_DATE_RE, " "),
    (_YEAR_RE, " "),
    (re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?!\d)"), " "),
    (_CITE_RE, " "),
    # 带标签的编号/账号：标签后的字母数字串整体是标识符，不是数值断言。
    (re.compile(r"(?<![0-9A-Za-z])(?:[Ii][Dd]|编号|单号|订单号|工单号|工号|流水号"
                r"|手机号|电话|时间戳|epoch|毫秒)\s*[:：of]*\s*[0-9A-Za-z][0-9A-Za-z_.\-]*"), " "),
    # 含字母又含数字的代号/版本号（SO-001、v1.2.3、IPv6、GPT4）整体是标识符。
    (re.compile(r"(?<![0-9A-Za-z])[0-9A-Za-z_.\-]*[A-Za-z][0-9A-Za-z_.\-]*\d"
                r"[0-9A-Za-z_.\-]*(?![0-9A-Za-z])"), " "),
    # 数字区间（1-2 个、010-12345678、2026-2027）表达范围，不拆成两个断言。
    (re.compile(r"(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*[-–—~～至到]\s*"
                r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\d)"), " "),
    # 有序列号：行首 1./1、/1) 与任意位置的（1）。
    (re.compile(r"(?m)^[ \t>*(–—-]*\d{1,3}[.、)）](?!\d)"), " "),
    (re.compile(r"(?<!\d)[（(]\d{1,3}[)）](?!\d)"), " "),
    (re.compile(r"(?<!\d)(?:\d{1,3}、)+(?:\d{1,3}(?!\d))?"), " "),
    (re.compile(r"第\s*\d{1,4}(?!\d)"), " "),
)


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
    cleaned = _CITE_RE.sub(" ", _UUID_RE.sub(" ", _STAMP_RE.sub(" ", text or "")))
    cleaned = _YEAR_RE.sub(" ", _DATE_RE.sub(" ", cleaned))
    return [Decimal(raw.replace(",", ""))
            for raw in _NUMBER_RE.findall(cleaned)]


def _numbers_in_answer_decimal(text: str) -> list[Decimal]:
    """答案文本中的数值断言：先经答案侧排版/标识符屏蔽再抽取。"""
    cleaned = text or ""
    for pattern, replacement in _ANSWER_MASKS:
        cleaned = pattern.sub(replacement, cleaned)
    return [Decimal(raw.replace(",", ""))
            for raw in _NUMBER_RE.findall(cleaned)]


def _identifier_key(key: object) -> bool:
    """queryId/instanceId/observedAt/created_at/ids 等运行时元数据键：其值是
    标识符或时间戳，数字片段（UUID、时刻）不得充当数值证据。"""
    lowered = key.lower() if isinstance(key, str) else ""
    return isinstance(key, str) and (
        key == "id" or lowered in {"ids", "uuid", "timestamp", "epoch"}
        or key.endswith(("Id", "_id", "At", "_at", "_utc"))
        # 签名/哈希/游标是标识符，其十六进制片段不能进入数值证据集合。
        or lowered.endswith(("hash", "digest", "token", "cursor")))


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
        if isinstance(value, dict):
            children = [item for key, item in value.items()
                        if not _identifier_key(key)]
        else:
            children = list(value)
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
        evidence = {key: value for key, value in result.items()
                    if key not in _REQUEST_METADATA_KEYS}
        numbers.extend(_numbers_in_value(evidence))
    allowed = _with_ratio_variants(numbers)
    # Historical float aliases stay for exact, safe values; verification keeps Decimal.
    allowed.update(p for p in map(_public_number, tuple(allowed)) if isinstance(p, float))
    return allowed


def answer_numbers(answer: str) -> list:
    """回答文本中的数值断言（保留出现顺序与重复）。"""
    return [_public_number(value) for value in _numbers_in_answer_decimal(answer)]


def verify_answer(answer: str, steps: Iterable[dict]) -> VerificationResult:
    universe = result_number_universe(steps)
    numbers = _numbers_in_answer_decimal(answer)
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
