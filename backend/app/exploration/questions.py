"""澄清账本（question ledger）— 把「不确定」变成可销账的显性负债

设计动机：探索对话的质量瓶颈不在"沉淀了多少"，而在"还有多少事悬而未决、
被模棱两可地带过"。账本把 agent 提出的每个关键问题登记为一条负债：

  - blocking（B 类）：企业特有口径，必须用户拍板 —— 阈值/枚举边界/审批线/
    级联策略/主键口径/基数。有任何一条未销账，质量门不放行草稿。
  - advisory（A 类）：行业常识，AI 先给建议值（suggestion），用户可改可确认。

销账（resolve）时强制「定量」：blocking 问题的结论若仍含模糊词且不含任何
数值/枚举选项，直接拒绝并把原因回填给 LLM —— 让它继续追问，而不是记下一句
"金额较大时需要审批"这类无法形式化的话。

账本存储在会话画布 JSON 的 questions 键下（随 canvas 事件推送、随文档/草稿
快照冻结），与七类模型元素同生命周期，天然可追溯。
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.exploration.canvas import norm_name

QUESTIONS_KEY = "questions"

KIND_BLOCKING = "blocking"
KIND_ADVISORY = "advisory"

# 模糊表述词表：出现任一且结论中无任何数量信息 → 视为未定量
_VAGUE_TERMS = (
    "大额", "小额", "大量", "少量", "较大", "较小", "较多", "较少", "很多", "很少",
    "及时", "尽快", "定期", "不定期", "长期", "短期", "频繁", "偶尔", "一段时间",
    "若干", "一些", "多次", "数次", "适当", "合理", "必要时", "视情况", "酌情",
    "大概", "大约", "差不多", "可能", "或许", "一般来说", "通常", "正常范围",
    "过高", "过低", "太久", "太多", "太少", "超时",
    # 未绑定的占位口径。仅出现这些词并不等于给出了规则参数。
    "阈值", "门槛", "上限", "下限", "规定时间", "一定期限", "满足条件", "符合条件",
    "待定", "tbd", "待补",
)
# 数量信号：阿拉伯/全角数字、中文数词（含"两"）、比较/枚举记号
_QUANT_RE = re.compile(r"[0-9０-９]|[一二两三四五六七八九十百千万亿]|[≥≤><=%％]")
_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
_UNIT = (
    r"(?:亿元|万元|千元|元|人民币|美元|欧元|%|％|个百分点|"
    r"毫秒|秒钟|秒|分钟|小时|工作日|自然日|天|日|周|星期|个月|月|季度|年|"
    r"人|次|个|笔|件|条|份|项|台|家|单)"
)
_BOUND_VALUE_RE = re.compile(
    rf"(?:>=|<=|>|<|=|≥|≤|超过|高于|低于|少于|多于|不低于|不高于|不超过|至少|至多)"
    rf"\s*{_NUMBER}\s*{_UNIT}?"
    rf"|{_NUMBER}\s*{_UNIT}?\s*(?:以上|以下|以内|以外|之内|之前|之后|内|前|后|起)"
    rf"|(?:是指|定义为|明确为|设定为|设为|等于|为)\s*"
    rf"(?:[^，。；;\n]{{0,18}}?)?(?:>=|<=|>|<|=|≥|≤)?\s*{_NUMBER}\s*{_UNIT}?"
    rf"|(?:每|每隔)\s*(?:{_NUMBER}\s*)?(?:秒|分钟|小时|天|日|周|星期|月|季度|年)"
)
_ABSOLUTE_VAGUE_TERMS = {
    "不定期", "必要时", "视情况", "酌情", "适当", "合理", "大概", "大约", "差不多",
    "可能", "或许", "一般来说", "通常", "正常范围", "待定", "tbd",
}
_TIME_VAGUE_TERMS = {
    "及时", "尽快", "定期", "长期", "短期", "频繁", "偶尔", "一段时间", "超时", "太久",
    "规定时间", "一定期限",
}
_MONEY_VAGUE_TERMS = {"大额", "小额"}
_TIME_UNIT_RE = re.compile(r"(?:毫秒|秒钟|秒|分钟|小时|工作日|自然日|天|日|周|星期|个月|月|季度|年)")
_MONEY_UNIT_RE = re.compile(r"(?:亿元|万元|千元|元|人民币|美元|欧元|%|％|个百分点)")
_CURRENCY_UNIT_RE = re.compile(r"(?:亿元|万元|千元|元|人民币|美元|欧元)")

_CARDINALITY_ALIASES = {
    "one-to-one": {"onetoone", "一对一", "1对1"},
    "one-to-many": {"onetomany", "一对多", "1对多"},
    "many-to-one": {"manytoone", "多对一", "多对1"},
    "many-to-many": {"manytomany", "多对多"},
}
_FIELD_ALIASES = {
    "主键": "key_attribute",
    "业务主键": "key_attribute",
    "primarykey": "key_attribute",
    "key": "key_attribute",
    "基数": "cardinality",
    "枚举": "enum",
    "枚举值": "enum",
    "状态值": "enum",
    "执行主体": "actor",
    "作用对象": "object",
    "预期结果": "expected_outcome",
    "结果": "outcome",
    "来源": "source",
    "规则表述": "statement",
    # 流程/场景新字段：target 路径可用中文别名定位（如「支付场景.所属流程」）
    "流程": "process_ref",
    "所属流程": "process_ref",
    "挂接流程": "process_ref",
    "步骤": "steps",
    "流程步骤": "steps",
    "分支": "branches",
    "条件分支": "branches",
    "度量": "metrics",
    "产出度量": "metrics",
    "指标": "metrics",
    "口径": "formula",
    "计算口径": "formula",
}


def vague_terms_in(text: str) -> list[str]:
    """文本中命中的模糊词（无数量信号时才算未定量，见 is_quantified）。"""
    t = text or ""
    return [w for w in _VAGUE_TERMS if w in t]


def _normalized_text(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _term_is_bound(text: str, term: str, start: int) -> bool:
    """判断某个模糊词是否在局部语义中被真正绑定，而非被无关数字“蹭过”。"""
    if term.lower() in _ABSOLUTE_VAGUE_TERMS:
        # “通常 ≥ 5 万”仍不是可执行规则；应删除“通常”或明确例外集合。
        return False
    window = text[max(0, start - 32): start + len(term) + 48]
    match = _BOUND_VALUE_RE.search(window)
    if match is None:
        return False
    bound = match.group(0)
    if term in _TIME_VAGUE_TERMS and not _TIME_UNIT_RE.search(bound):
        return False
    if term in _MONEY_VAGUE_TERMS:
        nearby = window[max(0, match.start() - 12):match.end() + 12]
        if not _MONEY_UNIT_RE.search(bound) and not re.search(r"金额|价格|货款|额度|费用", nearby):
            return False
    return True


def is_quantified(text: str) -> bool:
    """结论是否明确可执行，而不是仅在任意位置出现一个数字。

    例：
      - 「大额指 ≥ 50000 元」→ 通过
      - 「大额订单由 2 人审批」→ 不通过（2 是审批人数，不是“大额”定义）
      - 「金额超过阈值时审批」→ 不通过（阈值仍是占位符）
    """
    t = _normalized_text(text)
    if not t:
        return False
    vague = vague_terms_in(t)
    if not vague:
        return True
    if not _QUANT_RE.search(t):
        return False
    lower = t.lower()
    return all(
        _term_is_bound(t, term, match.start())
        for term in vague
        for match in re.finditer(re.escape(term.lower()), lower)
    )


def resolution_relevance_issue(question: Any, resolution: str) -> Optional[str]:
    """检查“数字答非所问”。

    数字本身不是答案：金额问题不能用“2 人审批”销账，时限问题不能用
    “3 个角色”销账。只对能稳定识别的企业口径类别做严格检查，其他问题
    保持向后兼容，交给 target/画布一致性检查。
    """
    q = question if isinstance(question, dict) else {}
    answer = _normalized_text(resolution)
    blob = f"{q.get('question') or ''} {q.get('target') or ''}".lower()
    question_text = str(q.get("question") or "").lower()
    has_number = bool(_QUANT_RE.search(answer))
    amount_question = bool(
        re.search(
            r"(?:金额|大额|小额|额度|价格|费用|货款|amount|price|cost)"
            r".*(?:多少|阈值|门槛|上限|下限|标准|界限|定义|是多少)",
            question_text,
        )
        or re.search(
            r"(?:多少|阈值|门槛|上限|下限|标准|界限)"
            r".*(?:金额|大额|小额|额度|价格|费用|货款|amount|price|cost)",
            question_text,
        )
    )
    if amount_question:
        if not has_number or not _CURRENCY_UNIT_RE.search(answer):
            return "问题询问金额/额度口径，结论必须给出货币单位，不能用其他数字代替"
    percentage_question = bool(
        re.search(r"(?:比例|百分比|比率|占比|费率).*(?:多少|阈值|门槛|是多少)", question_text)
    )
    if percentage_question and (not has_number or not re.search(r"(?:%|％|个百分点)", answer)):
        return "问题询问比例口径，结论必须给出百分比单位"
    time_question = bool(
        re.search(r"几天|多久|多长时间|时限.*(?:多少|多长|多久|是多少)|"
                  r"期限.*(?:多少|多长|多久|是多少)|时间窗口.*(?:多少|多长|多久|是多少)|"
                  r"周期.*(?:多少|多长|多久|是多少)|频率.*(?:多少|多久|是多少)",
                  question_text)
    )
    if time_question:
        if not has_number or not _TIME_UNIT_RE.search(answer):
            return "问题询问时限/周期口径，结论必须给出明确时间单位，不能用其他数字代替"
    if re.search(r"几人|多少人|人数.*(?:多少|是多少)|几次|多少次|数量.*(?:多少|是多少)|count", question_text):
        if not has_number or not re.search(r"(?:人|次|个|笔|件|条|份|项|台|家|单)", answer):
            return "问题询问人数/次数/数量，结论必须给出匹配的计量单位"
    if re.search(r"基数|一对一|一对多|多对一|多对多|cardinality", blob):
        if _cardinality_in(answer) is None:
            return "问题询问关系基数，结论必须明确为一对一/一对多/多对一/多对多"
    if re.search(r"有哪些|枚举值|状态清单|enum", blob):
        choices = [item.strip() for item in re.split(r"[/、,，;；|]", answer) if item.strip()]
        if len(choices) < 2:
            return "问题询问枚举清单，结论必须列出至少两个明确选项"
    return None


class Question(BaseModel):
    """账本条目。宽容解析（同画布元素约定），LLM 给错字段名可自愈。"""
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: Optional[str] = None
    question: str
    kind: str = KIND_BLOCKING                 # blocking | advisory
    target: Optional[str] = None              # 关联元素名（或 元素名.字段）
    options: list[str] = Field(default_factory=list)   # 结构化候选，用户点选即答
    suggestion: Optional[str] = None          # advisory 的 AI 建议值
    status: str = "open"                      # open | resolved | dismissed
    resolution: Optional[str] = None          # 定量结论（销账时必填）
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_questions(canvas: Any) -> list[dict]:
    items = (canvas or {}).get(QUESTIONS_KEY)
    return [dict(x) for x in items] if isinstance(items, list) else []


def blocking_liabilities(canvas: Any) -> list[dict]:
    """仍会堵门的 blocking 条目。

    dismissed 只是“暂缓”，不是业务结论；因此它不会出现在 open_questions，
    但仍是一项 blocking liability。这个区分保留旧 API 语义，又避免假全绿。
    """
    return [
        q for q in get_questions(canvas)
        if (q.get("kind") or KIND_BLOCKING) == KIND_BLOCKING
        and q.get("status") != "resolved"
    ]


def open_questions(canvas: Any, kind: Optional[str] = None) -> list[dict]:
    out = [q for q in get_questions(canvas) if q.get("status") == "open"]
    if kind:
        out = [q for q in out if (q.get("kind") or KIND_BLOCKING) == kind]
    return out


def _field_norm(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def _cardinality_in(text: str) -> Optional[str]:
    normalized = _field_norm(_normalized_text(text))
    for canonical, aliases in _CARDINALITY_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return canonical
    return None


def _flatten_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [
            text
            for key, item in value.items()
            if key not in {"id", "created_at", "resolved_at"}
            for text in _flatten_text(item)
        ]
    if isinstance(value, list):
        return [text for item in value for text in _flatten_text(item)]
    # ProcessStep.seq、ScenarioBranch.from_step/to_step 等纯数值字段不是证据文本，
    # 防止「2 人审批」类答案被 seq=2 假一致放行（bool 不是此类数值，保持原样）。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return []
    return [str(value)]


def _matches_named_item(item: Any, token: str) -> bool:
    if not isinstance(item, dict):
        return False
    wanted = norm_name(token)
    return wanted in {
        norm_name(str(item.get(key) or ""))
        for key in ("id", "name", "display_name", "target")
    } - {""}


def _resolve_child(value: Any, token: str) -> tuple[bool, Any]:
    """解析 target 路径的一段；同时支持字段名和命名子元素。"""
    if isinstance(value, dict):
        requested = _FIELD_ALIASES.get(_field_norm(token), token)
        requested_norm = _field_norm(requested)
        direct = [item for key, item in value.items() if _field_norm(key) == requested_norm]
        if len(direct) == 1:
            return True, direct[0]
        candidates = [
            item
            for child in value.values()
            if isinstance(child, list)
            for item in child
            if _matches_named_item(item, token)
        ]
        if len(candidates) == 1:
            return True, candidates[0]
        return False, None
    if isinstance(value, list):
        candidates = [item for item in value if _matches_named_item(item, token)]
        if len(candidates) == 1:
            return True, candidates[0]
    return False, None


def _find_target_root(canvas: Any, token: str) -> tuple[Optional[dict], Optional[str]]:
    candidates: list[tuple[dict, str]] = []
    c = canvas if isinstance(canvas, dict) else {}
    for key in ("objects", "actors", "behaviors", "events", "rules", "scenarios", "processes"):
        for item in c.get(key) or []:
            if _matches_named_item(item, token):
                candidates.append((item, key))
    if len(candidates) != 1:
        return None, "不存在" if not candidates else "不唯一"
    return candidates[0]


def _infer_target_value(root: dict, current: Any, question: str) -> tuple[Any, bool]:
    """问题语义足够明确时，把元素级 target 收窄到实际决策字段。"""
    blob = str(question or "")
    if not isinstance(current, dict):
        return current, True
    if "主键" in blob and current.get("key_attribute") is not None:
        return current.get("key_attribute"), True
    if ("基数" in blob or "几对几" in blob) and current.get("cardinality") is not None:
        return current.get("cardinality"), True
    if ("枚举" in blob or "状态" in blob or "有哪些" in blob) and current.get("enum"):
        return current.get("enum"), True
    if "执行主体" in blob and current.get("actor") is not None:
        return current.get("actor"), True
    if "作用对象" in blob and current.get("object") is not None:
        return current.get("object"), True
    if ("预期结果" in blob or "最终结果" in blob) and current.get("expected_outcome") is not None:
        return current.get("expected_outcome"), True
    if "来源" in blob and current.get("source") is not None:
        return current.get("source"), True
    # 对象级“状态有哪些”可唯一落到一个枚举属性；多个枚举时必须把 target 写细。
    if "状态" in blob or "枚举" in blob or "有哪些" in blob:
        enums = [attr.get("enum") for attr in (current.get("attributes") or []) if attr.get("enum")]
        if len(enums) == 1:
            return enums[0], True
    if ("基数" in blob or "几对几" in blob) and len(current.get("relations") or []) == 1:
        return current["relations"][0].get("cardinality"), True
    return current, False


def _target_resolution(canvas: Any, target: str, question: str) -> tuple[Optional[dict], Any, bool, Optional[str]]:
    parts = [part.strip() for part in re.split(r"[./]", str(target or "")) if part.strip()]
    if not parts:
        return None, None, False, "target 为空"
    found = _find_target_root(canvas, parts[0])
    root, root_kind = found
    if root is None:
        return None, None, False, f"target 根元素「{parts[0]}」{root_kind}"
    current: Any = root
    for token in parts[1:]:
        ok, current = _resolve_child(current, token)
        if not ok:
            return root, None, False, f"target 路径「{target}」无法解析到画布字段"
    current, direct = _infer_target_value(root, current, question)
    return root, current, direct, None


def _linked_evidence(canvas: Any, root: dict) -> list[str]:
    """只扩展到与 target 直接相连的规则/行为/场景/流程，避免无关数字造成假一致。"""
    evidence = _flatten_text(root)
    aliases = {
        norm_name(str(root.get(key) or ""))
        for key in ("name", "display_name", "id")
    } - {""}
    c = canvas if isinstance(canvas, dict) else {}
    related_behaviors: list[dict] = []
    for behavior in c.get("behaviors") or []:
        endpoints = {
            norm_name(str(behavior.get(key) or ""))
            for key in ("actor", "object", "name", "display_name")
        } - {""}
        if aliases & endpoints:
            related_behaviors.append(behavior)
            evidence.extend(_flatten_text(behavior))
    behavior_aliases = {
        norm_name(str(behavior.get(key) or ""))
        for behavior in related_behaviors
        for key in ("name", "display_name", "id")
    } - {""}
    for rule in c.get("rules") or []:
        if norm_name(str(rule.get("applies_to") or "")) in aliases | behavior_aliases:
            evidence.extend(_flatten_text(rule))
    for scenario in c.get("scenarios") or []:
        refs = {
            norm_name(str(value))
            for field in ("actors", "objects", "behaviors")
            for value in (scenario.get(field) or [])
        }
        if refs & (aliases | behavior_aliases):
            evidence.extend(_flatten_text(scenario))
    # 流程元素：顶层 objects 引用与步骤的 actor/behavior 显式绑定都算直接相连
    for process in c.get("processes") or []:
        refs = {norm_name(str(value)) for value in (process.get("objects") or [])}
        refs |= {
            norm_name(str(step.get(key) or ""))
            for step in (process.get("steps") or [])
            if isinstance(step, dict)
            for key in ("actor", "behavior")
        } - {""}
        if refs & (aliases | behavior_aliases):
            evidence.extend(_flatten_text(process))
    return evidence


def _quantity_tokens(text: str) -> list[tuple[str, str, str]]:
    value = _normalized_text(text)
    replacements = (
        ("大于等于", ">="), ("不低于", ">="), ("至少", ">="), ("≥", ">="),
        ("小于等于", "<="), ("不高于", "<="), ("不超过", "<="), ("至多", "<="), ("≤", "<="),
        ("超过", ">"), ("高于", ">"), ("大于", ">"),
        ("低于", "<"), ("少于", "<"), ("小于", "<"),
    )
    for source, replacement in replacements:
        value = value.replace(source, replacement)
    pattern = re.compile(rf"(?P<op>>=|<=|>|<|=)?\s*(?P<num>{_NUMBER})\s*(?P<unit>{_UNIT})?")
    tokens: list[tuple[str, str, str]] = []
    for match in pattern.finditer(value):
        op = match.group("op") or ""
        tail = value[match.end():match.end() + 3]
        if not op:
            if tail.startswith("以上") or tail.startswith("起"):
                op = ">="
            elif tail.startswith("以下") or tail.startswith("以内") or tail.startswith("之内"):
                op = "<="
        tokens.append((op, match.group("num"), match.group("unit") or ""))
    return tokens


def _value_evidence(canvas: Any, root: dict, value: Any) -> list[str]:
    evidence = _flatten_text(value)
    # key_attribute 使用英文 name，用户通常用中文 display_name 回答；两者均是同一证据。
    if isinstance(value, str) and norm_name(value) == norm_name(str(root.get("key_attribute") or "")):
        for attr in root.get("attributes") or []:
            if norm_name(str(attr.get("name") or "")) == norm_name(value):
                evidence.extend(_flatten_text(attr))
                break
    # 引用字段通常保存 canonical name，而用户按 display_name 作答；两者应视为同一值。
    if isinstance(value, str):
        c = canvas if isinstance(canvas, dict) else {}
        for key in ("objects", "actors", "behaviors", "events", "rules", "scenarios"):
            for item in c.get(key) or []:
                if _matches_named_item(item, value):
                    evidence.extend(
                        str(item.get(field))
                        for field in ("name", "display_name", "id")
                        if item.get(field)
                    )
    return evidence


def _resolution_matches_evidence(resolution: str, evidence: list[str]) -> bool:
    answer = _normalized_text(resolution)
    evidence_text = "；".join(_normalized_text(value) for value in evidence if str(value).strip())

    answer_cardinality = _cardinality_in(answer)
    if answer_cardinality:
        return _cardinality_in(evidence_text) == answer_cardinality

    quantities = _quantity_tokens(answer)
    if quantities:
        available = _quantity_tokens(evidence_text)
        for answer_op, answer_number, answer_unit in quantities:
            matches = [
                (op, number, unit)
                for op, number, unit in available
                if number == answer_number
                and (not answer_unit or not unit or answer_unit == unit)
                and (not answer_op or not op or answer_op == op)
            ]
            if not matches:
                return False
        return True

    choices = [item.strip() for item in re.split(r"[/、,，;；|]", answer) if item.strip()]
    if len(choices) >= 2:
        compact_evidence = norm_name(evidence_text)
        return all(norm_name(choice) in compact_evidence for choice in choices)

    compact_answer = norm_name(answer)
    return bool(compact_answer) and any(
        compact_answer in norm_name(value) or norm_name(value) in compact_answer
        for value in evidence
        if norm_name(value)
    )


def resolved_question_issues(canvas: Any) -> tuple[list[str], list[str]]:
    """复核已销账 blocking 的答案质量及其是否真正落入 target 画布。

    返回 (blocking, advisory)。target 根元素已不存在（概念被合并/删除的存量
    数据）降级为 advisory：结论无处落字不是质量问题，硬堵会让 questions 门
    永久无法清零（生产死锁：销账 target「探索起点」元素不存在）。其余复核
    （未定量/答非所问/结论未写入画布）保持 blocking。
    """
    issues: list[str] = []
    advisories: list[str] = []
    for q in get_questions(canvas):
        if (q.get("kind") or KIND_BLOCKING) != KIND_BLOCKING or q.get("status") != "resolved":
            continue
        label = str(q.get("question") or "?")
        resolution = str(q.get("resolution") or "").strip()
        if not resolution:
            issues.append(f"已销账问题「{label}」缺少 resolution")
            continue
        if not is_quantified(resolution):
            issues.append(f"已销账问题「{label}」的结论仍未定量或含未绑定口径")
            continue
        relevance = resolution_relevance_issue(q, resolution)
        if relevance:
            issues.append(f"已销账问题「{label}」答复不匹配：{relevance}")
            continue
        target = str(q.get("target") or "").strip()
        if not target:
            continue
        root, value, direct, error = _target_resolution(canvas, target, label)
        if error:
            if error.startswith("target 根元素"):
                advisories.append(
                    f"已销账问题「{label}」的 {error}（元素已变更/移除，"
                    "结论待人工核对，不再堵门）")
            else:
                issues.append(f"已销账问题「{label}」的 {error}")
            continue
        assert root is not None
        evidence = _value_evidence(canvas, root, value) if direct else _linked_evidence(canvas, root)
        if not _resolution_matches_evidence(resolution, evidence):
            issues.append(
                f"已销账问题「{label}」的结论「{resolution}」尚未写入 target「{target}」对应画布字段"
            )
    return issues, advisories


def _with_questions(canvas: Any, items: list[dict]) -> dict:
    """返回带新账本的全新画布 dict（JSON 列必须整体重赋值才会写库）。"""
    out = dict(canvas) if isinstance(canvas, dict) else {}
    out[QUESTIONS_KEY] = items
    return out


def raise_questions(canvas: Any, raw_items: list[dict]) -> tuple[dict, list[str], list[str]]:
    """登记问题（按归一化问题文本去重，重复登记返回既有 id）。

    返回 (新画布, 生效/命中的 id 列表, 错误列表)。
    """
    items = get_questions(canvas)
    by_text = {norm_name(q.get("question", "")): q for q in items}
    ids: list[str] = []
    errors: list[str] = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            errors.append(f"问题必须是对象，收到: {type(raw).__name__}")
            continue
        try:
            q = Question.model_validate(raw)
        except ValidationError as e:
            bad = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                            for err in e.errors()[:3])
            errors.append(f"问题「{str(raw.get('question', '?'))[:40]}」不合法: {bad}")
            continue
        if q.kind not in (KIND_BLOCKING, KIND_ADVISORY):
            errors.append(f"问题「{q.question[:40]}」kind 必须是 blocking 或 advisory")
            continue
        existing = by_text.get(norm_name(q.question))
        if existing is not None:
            # 已有同题：open 的直接复用；已销账的不复活（避免循环追问）
            ids.append(existing.get("id") or "")
            continue
        data = q.model_dump(exclude_none=True)
        data["id"] = q.id or f"q-{uuid.uuid4().hex[:8]}"
        data["status"] = "open"
        data.pop("resolution", None)
        data["created_at"] = _now_iso()
        items.append(data)
        by_text[norm_name(q.question)] = data
        ids.append(data["id"])
    return _with_questions(canvas, items), ids, errors


_DELEGATION_OPTION_RE = re.compile(
    r"^(?:按|选|就按|按照)?\s*(?:选项\s*|第\s*)?(?P<opt>[A-Da-d1-4１-４])\s*"
    r"(?:个|来|执行|办|处理|即可|就行|可以)?$")
_DELEGATION_DEFAULT_RE = re.compile(
    r"^(?:按|就按|按照)?\s*(?:默认|建议|推荐)(?:值)?\s*"
    r"(?:来|执行|办|处理|即可|就行|可以)?$")
_ABDICATION_RE = re.compile(
    r"^(?:都?可以|都行|随便|无所谓|你来?定|你决定|你看着办|由你|你选)$")

_OPTION_INDEX = {"a": 0, "b": 1, "c": 2, "d": 3,
                 "1": 0, "2": 1, "3": 2, "4": 3,
                 "１": 0, "２": 1, "３": 2, "４": 3}


def _expand_delegated_resolution(question: Any, resolution: str) -> Optional[str]:
    """把「按选项B/第2个/按默认」展开为所引用候选的字面定量值。

    生产实测：用户答「可以的，都按默认来」被销账拒绝，agent 只能再问一轮。
    委托指向的候选本身已定量时，落账存候选字面值（证据匹配依赖具体数值，
    见 resolved_question_issues）。拍板权不放松：开放式放权不展开、找不到
    所指候选时不展开，交由调用方给出明确拒绝。
    """
    answer = _normalized_text(resolution)
    options = [str(o).strip() for o in (question.get("options") or [])
               if str(o).strip()]
    if not options:
        return None
    opt_match = _DELEGATION_OPTION_RE.match(answer)
    if opt_match is not None:
        idx = _OPTION_INDEX.get(opt_match.group("opt").lower())
        if idx is not None and idx < len(options):
            return options[idx]
        return None
    if _DELEGATION_DEFAULT_RE.match(answer):
        marked = next((o for o in options if "默认" in o), None)
        if marked is not None:
            return marked
        suggestion = str(question.get("suggestion") or "").strip()
        if suggestion and is_quantified(suggestion):
            return suggestion
    return None


def resolve_questions(canvas: Any, raw_items: list[dict]) -> tuple[dict, list[dict], list[str]]:
    """销账。blocking 问题的 resolution 必须定量，否则拒绝该条并回填原因。

    raw_items: [{id(账本 id 或问题原文), resolution, status?: resolved|dismissed}]
    返回 (新画布, 生效条目列表, 错误列表)。
    """
    items = get_questions(canvas)
    by_id = {q.get("id"): q for q in items if q.get("id")}
    by_text = {norm_name(q.get("question", "")): q for q in items}
    done: list[dict] = []
    errors: list[str] = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            errors.append(f"销账条目必须是对象，收到: {type(raw).__name__}")
            continue
        key = str(raw.get("id") or raw.get("question") or "").strip()
        q = by_id.get(key) or by_text.get(norm_name(key))
        if q is None:
            errors.append(f"账本中找不到问题「{key[:40]}」")
            continue
        status = str(raw.get("status") or "resolved")
        if status not in ("resolved", "dismissed"):
            errors.append(f"问题「{q.get('question', '')[:40]}」status 只能是 resolved 或 dismissed")
            continue
        resolution = str(raw.get("resolution") or "").strip()
        if not resolution:
            errors.append(f"问题「{q.get('question', '')[:40]}」缺少 resolution"
                          f"（{'搁置也要写明原因' if status == 'dismissed' else '请写入定量结论'}）")
            continue
        if status == "resolved" and (q.get("kind") or KIND_BLOCKING) == KIND_BLOCKING:
            label = str(q.get("question", ""))[:40]
            if _ABDICATION_RE.match(_normalized_text(resolution)):
                errors.append(
                    f"问题「{label}」：开放式放权（都可以/你定）不能作为堵门结论"
                    " —— 请让用户在候选中明确拍板")
                continue
            if _DELEGATION_DEFAULT_RE.match(_normalized_text(resolution)):
                expanded = _expand_delegated_resolution(q, resolution)
                if expanded is None:
                    errors.append(
                        f"问题「{label}」：候选中没有标注「默认」的选项，无法按默认销账"
                        " —— 请让用户在候选中明确选择")
                    continue
                resolution = expanded
            else:
                expanded = _expand_delegated_resolution(q, resolution)
                if expanded is not None:
                    resolution = expanded
            if not is_quantified(resolution):
                hits = "、".join(vague_terms_in(resolution)[:3]) or "未给出明确结论"
                errors.append(
                    f"问题「{label}」的结论仍未定量或含未绑定口径（{hits}）"
                    f"—— 请追问用户拿到与问题匹配的数字+单位、明确边界或枚举值后再销账"
                )
                continue
            relevance = resolution_relevance_issue(q, resolution)
            if relevance:
                errors.append(f"问题「{label}」答复不匹配：{relevance}")
                continue
        q["status"] = status
        q["resolution"] = resolution
        q["resolved_at"] = _now_iso()
        done.append({"id": q.get("id"), "question": q.get("question"),
                     "status": status, "resolution": resolution})
    return _with_questions(canvas, items), done, errors


def ledger_summary(canvas: Any, max_items: int = 12) -> str:
    """未清负债的紧凑摘要；dismissed blocking 仍明确显示为堵门。"""
    opens = open_questions(canvas)
    non_open_liabilities = [
        q for q in blocking_liabilities(canvas)
        if q.get("status") != "open"
    ]
    pending = opens + non_open_liabilities
    if not pending:
        return "（无开放问题 —— 账本已清）"
    pending.sort(key=lambda q: (
        0 if (q.get("kind") or KIND_BLOCKING) == KIND_BLOCKING else 1,
        0 if q.get("status") == "open" else 1,
    ))
    lines = []
    for q in pending[:max_items]:
        if q.get("status") == "dismissed":
            tag = "堵门·已搁置，仍未解决"
        elif (q.get("kind") or KIND_BLOCKING) == KIND_BLOCKING and q.get("status") != "open":
            tag = f"堵门·非法状态 {q.get('status')!s}"
        else:
            tag = "堵门" if (q.get("kind") or KIND_BLOCKING) == KIND_BLOCKING else "建议待确认"
        opt = f"（候选: {' / '.join(str(o) for o in q.get('options') or [])[:80]}）" \
            if q.get("options") else ""
        tgt = f"[{q.get('target')}] " if q.get("target") else ""
        lines.append(f"- ({tag}) {tgt}{q.get('question')}{opt} (id={q.get('id')})")
    if len(pending) > max_items:
        lines.append(f"- …共 {len(pending)} 个未清负债")
    return "\n".join(lines)
