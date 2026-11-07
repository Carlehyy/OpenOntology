"""Conservative answer-only layout/date masks and derived-value classification."""
from __future__ import annotations

import re

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
    # 仅「≥2 个数字顿号相连且以数字结尾」的纯数字串是序号；「201、Resolved」
    # 是标签+数值。边界断言防止把「53,225、25,801」的千分位局部掩掉。
    (re.compile(r"(?<![\d,])(?:\d{1,3}、)+\d{1,3}(?![\d,])"), " "),
    (re.compile(r"第\s*\d{1,4}(?!\d)"), " "),
)



_ORDINAL = re.compile(r"排名|序号|名次|#|\b(?:rank|index)\b", re.IGNORECASE)
_SHORT_DATE = re.compile(
    r"(?<![\d/])(?:1[0-2]|0?[1-9])\s*/\s*(?:3[01]|[12]\d|0?[1-9])(?![\d/])")
_DATE_PREFIX = re.compile(
    r"(?:截至|截止|日期|区间|范围|覆盖|数据期|统计期|数据时间)"
    r"(?:为|是|从|自)?\s*[:：]?\s*(?P<duration>\d+\s*天\s*[（(]\s*)?$")
_DERIVED_VALUE = re.compile(
    r"(?:占比?|百分比|百分之|合计|总计|小计|推算|换算|折合|平均|同比|环比|倍数)"
    r"\s*(?:(?:为|是|约|达|得到|结果|等于|可得|[:：])\s*){0,2}"
    r"(?P<value>" + _NUMBER_RE.pattern + r")")


def mask_layout_dates(text: str) -> str:
    """Only ordinal headers establish table ranks; short dates need a date cue."""
    lines = []
    ordinal_table = False
    for line in text.splitlines(keepends=True):
        if not line.lstrip().startswith("|") or line.count("|") < 2:
            ordinal_table = False
            lines.append(line)
            continue
        parts = line.split("|")
        first = parts[1].strip().strip("*_ ")
        if _ORDINAL.search(first):
            ordinal_table = True
        elif ordinal_table and re.fullmatch(r"[1-9]\d{0,2}", first):
            parts[1] = " "
            line = "|".join(parts)
        elif not re.fullmatch(r"[:\- ]+", first) and not first.isdigit():
            ordinal_table = False
        lines.append(line)
    cleaned = "".join(lines)

    def mask_clause(match):
        clause = match.group()
        spans = []
        previous_end = None
        for date in _SHORT_DATE.finditer(clause):
            cue = _DATE_PREFIX.search(clause[:date.start()])
            continuation = (previous_end is not None and re.fullmatch(
                r"\s*[-–—~～至到]\s*", clause[previous_end:date.start()]))
            if cue or continuation:
                spans.append(date.span())
                if cue and cue.group("duration"):
                    spans.append(cue.span("duration"))
                previous_end = date.end()
            else:
                previous_end = None
        for start, end in reversed(sorted(spans)):
            clause = clause[:start] + " " + clause[end:]
        return clause

    return re.sub(r"[^；;。！？!?\n，]+", mask_clause, cleaned)


def derived_number_spans(answer: str) -> list[tuple[int, int]]:
    """Mark adjacent derived values without weakening other observations."""
    spans = [match.span("value") for match in _DERIVED_VALUE.finditer(answer)]
    for percent in re.finditer(_NUMBER_RE.pattern + r"\s*[%％]", answer):
        spans.append(percent.span())
    return spans


def derived_notice(values) -> str:
    numbers = "、".join(str(value) for value in values)
    return ("\n\n> 推算未核验：以下数字为推算或占比等推导值，平台未做推算核验，"
            f"也未判定为错误：{numbers}。")


def incomplete_notice(answer: str, steps) -> str:
    """Preserve current-turn incomplete contains evidence in the final answer."""
    unresolved = any(
        str(step.get("error") or
        (step.get("result") or {}).get("error") or "").startswith(
            "remote_aggregate_contains_unresolved:") for step in steps)
    if not unresolved or re.search(r"截断|未完整|未枚举|预算|超过.{0,8}上限|不完整", answer):
        return ""
    return ("\n\n> 查询范围提示：本轮 contains 匹配值因达到枚举预算或 IN 上限而未枚举完整，"
            "相关统计未完成，不能作为完整结果使用；请缩小筛选范围后重试。")
