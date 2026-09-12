"""业务画布 — 七类业务模型元素的 schema、操作与完整度评估

画布是「需求 → 本体」转化稳定性的第一道防线：探索 agent 通过工具调用
把对话中已确认的信息沉淀为结构化元素（而非自由文本），转化时以画布为源。

七类模型（kind，单数）与画布键（复数）：
  object   → objects     业务对象（→ ObjectType + 属性 + 关系→LinkType）
  actor    → actors      业务主体（参与方，为动作提供归属/审批语境）
  behavior → behaviors   业务行为（→ ActionType）
  event    → events      业务事件（→ 哨兵草稿 muted 影子 + 动作描述 + 文档）
  rule     → rules       业务规则（constraint|validation → disabled 校验规则；
                         approval → requiresApproval；derivation → 激活函数草稿；
                         alert → 哨兵草稿）
  scenario → scenarios   业务场景（不进本体，作为草稿的可表达性验收器；
                         可挂接流程成为情境变体）
  process  → processes   业务流程（标准骨架：步骤/主体/分支/度量；不进本体，
                         场景可挂接为情境变体）
"""
from __future__ import annotations

import copy
import json
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Optional, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


def _to_camel(s: str) -> str:
    parts = s.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


class _El(BaseModel):
    """元素基类：宽容解析（忽略未知字段，camel/snake 双收），把校验错误
    留给真正结构性的问题，便于 LLM 按错误信息修正后重试。"""
    model_config = ConfigDict(
        alias_generator=_to_camel, populate_by_name=True, extra="ignore",
        str_strip_whitespace=True,
    )

    id: Optional[str] = None
    name: str = Field(min_length=1)
    display_name: Optional[str] = None
    description: Optional[str] = None


class AttributeSpec(_El):
    name: str
    type_hint: Optional[str] = None      # 自然语言类型提示，转化时映射到 PropertyType
    required: bool = False
    enum: Optional[list[str]] = None
    notes: Optional[str] = None

    @field_validator("enum")
    @classmethod
    def validate_enum(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if len(cleaned) < 2:
            raise ValueError("枚举至少需要 2 个非空值")
        normalized = [norm_name(item) for item in cleaned]
        if len(normalized) != len(set(normalized)):
            raise ValueError("枚举值归一化后存在重复项")
        return cleaned


class RelationSpec(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)
    id: Optional[str] = None
    target: str = Field(min_length=1)    # 目标对象名
    name: Optional[str] = None           # 关系标识（英文）
    display_name: Optional[str] = None   # 关系显示名（如「归属于」）
    cardinality: Optional[str] = None    # one-to-one / one-to-many / many-to-one / many-to-many
    description: Optional[str] = None

    @field_validator("cardinality")
    @classmethod
    def validate_cardinality(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower()
        allowed = {"one-to-one", "one-to-many", "many-to-one", "many-to-many"}
        if normalized not in allowed:
            raise ValueError("基数必须是 one-to-one/one-to-many/many-to-one/many-to-many")
        return normalized


class BusinessObject(_El):
    attributes: list[AttributeSpec] = Field(default_factory=list)
    key_attribute: Optional[str] = None  # 业务主键属性名
    relations: list[RelationSpec] = Field(default_factory=list)


class Actor(_El):
    kind: str = "role"                   # person | org | system | role
    responsibilities: list[str] = Field(default_factory=list)
    # 参与方(person/org)本身也是数据实体 —— 转换后与对象同为 ObjectType，故也带属性
    attributes: list[AttributeSpec] = Field(default_factory=list)
    key_attribute: Optional[str] = None  # 业务主键属性名

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        normalized = (value or "role").strip().lower()
        if normalized not in {"person", "org", "system", "role"}:
            raise ValueError("kind 必须是 person/org/system/role")
        return normalized


class Behavior(_El):
    actor: Optional[str] = None          # 执行主体名
    object: Optional[str] = None         # 作用对象名
    trigger: Optional[str] = None
    inputs: list[AttributeSpec] = Field(default_factory=list)
    outcome: Optional[str] = None
    constraints: list[str] = Field(default_factory=list)
    needs_approval: bool = False


class Event(_El):
    source: Optional[str] = None         # 触发来源：某行为名 / external / time
    payload: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)


class Rule(_El):
    kind: str = "constraint"             # constraint | validation | derivation | approval | alert
    applies_to: Optional[str] = None     # 作用对象/行为名
    statement: Optional[str] = None
    error_message: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        normalized = (value or "constraint").strip().lower()
        if normalized not in {"constraint", "validation", "derivation", "approval", "alert"}:
            raise ValueError("kind 必须是 constraint/validation/derivation/approval/alert")
        return normalized


class MetricSpec(BaseModel):
    """流程/场景的产出度量：formula 是定量计算口径（质量门校验），
    source_objects 指向度量来源的对象名。"""
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)
    id: Optional[str] = None
    name: str = Field(min_length=1)
    display_name: Optional[str] = None
    formula: Optional[str] = None        # 计算口径（必填才达标；空/未定量由质量门拦下）
    source_objects: list[str] = Field(default_factory=list)  # 度量来源对象名
    target: Optional[str] = None         # 目标值（可空）
    description: Optional[str] = None


class ScenarioBranch(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)
    id: Optional[str] = None
    from_step: int = Field(ge=1)          # 1-based steps 下标
    to_step: Optional[int] = Field(default=None, ge=1)  # None 表示流程结束
    condition: str = Field(min_length=1)


class Scenario(_El):
    goal: Optional[str] = None
    actors: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)     # 场景涉及的对象名（覆盖检查用）
    behaviors: list[str] = Field(default_factory=list)   # 场景涉及的行为名（覆盖检查用）
    branches: list[ScenarioBranch] = Field(default_factory=list)  # 条件分支的显式目标
    expected_outcome: Optional[str] = None
    process_ref: Optional[str] = None    # 挂接的流程名/id；非空 = 该场景是流程的情境变体
    metrics: list[MetricSpec] = Field(default_factory=list)       # 情境变体的产出度量


class ProcessStep(BaseModel):
    """流程步骤：seq 为排序键；actor/behavior 是指向主体/行为模型的显式引用，
    留空 actor 表示线下步骤，留空 behavior 表示尚未绑定行为。"""
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)
    id: Optional[str] = None
    seq: int = Field(ge=1)                # 排序键（渲染与校验按 seq 升序取步骤）
    name: str = Field(min_length=1)
    actor: Optional[str] = None           # 执行主体引用（可空 = 线下步骤）
    behavior: Optional[str] = None        # 绑定的业务行为引用（可空）
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    description: Optional[str] = None


class ProcessBranch(BaseModel):
    """流程分支：from_step/to_step 是 1-based 步骤下标（按 seq 升序后的位置），
    与 ScenarioBranch 同一口径；kind=exception 标记异常路径。"""
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, extra="ignore",
                              str_strip_whitespace=True)
    id: Optional[str] = None
    from_step: int = Field(ge=1)          # 1-based steps 下标
    to_step: Optional[int] = Field(default=None, ge=1)  # None 表示流程结束
    condition: str = Field(min_length=1)
    kind: str = "normal"                  # normal | exception

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        normalized = (value or "normal").strip().lower()
        if normalized not in {"normal", "exception"}:
            raise ValueError("kind 必须是 normal/exception")
        return normalized


class Process(_El):
    goal: Optional[str] = None
    trigger: Optional[str] = None
    steps: list[ProcessStep] = Field(default_factory=list)
    branches: list[ProcessBranch] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)     # 流程涉及的对象名（覆盖检查用）
    metrics: list[MetricSpec] = Field(default_factory=list)
    expected_outcome: Optional[str] = None


KIND_MODELS: dict[str, type[_El]] = {
    "object": BusinessObject, "actor": Actor, "behavior": Behavior,
    "event": Event, "rule": Rule, "scenario": Scenario, "process": Process,
}
KIND_KEYS = {"object": "objects", "actor": "actors", "behavior": "behaviors",
             "event": "events", "rule": "rules", "scenario": "scenarios",
             "process": "processes"}
KIND_LABELS = {"object": "对象", "actor": "主体", "behavior": "行为",
               "event": "事件", "rule": "规则", "scenario": "场景", "process": "流程"}


def norm_name(name: str) -> str:
    """名称归一化（匹配/去重键）：小写、去空白与连接符。"""
    return re.sub(r"[\s_\-]+", "", (name or "").strip().lower())


# ---------------------------------------------------------------------------
# LLM 序列化形态矫正
#
# 生产事故（2026-09 商业化审查 D-010）：MiniMax-M3 等模型对工具参数中的
# 嵌套数组无法稳定输出 JSON 数组——attributes/relations 等列表字段常被序列化
# 为 {"item": {...}} 包装对象、{"0":…,"1":…} 索引对象、单对象或 <item> XML 串。
# 顶层 json.loads 成功、Pydantic 严格 list 校验必拒，而模型对自身序列化层的
# 问题无法自纠，导致画布写入死循环（15+ 次重试全拒）。
# 矫正只识别结构形态、不猜测内容语义；无法识别的非空形态保持原样交给
# Pydantic 报错，让错误信息继续指给模型。
# ---------------------------------------------------------------------------

_ITEM_WRAP_KEYS = ("item", "items", "element", "elements", "entry", "value")


def _coerce_llm_list(value: Any) -> Optional[list]:
    """尽力把 LLM 常见的非数组序列化形态矫正为 list；无法识别返回 None。"""
    if isinstance(value, list):
        return value
    if value is None:
        return None
    if isinstance(value, dict):
        if not value:
            return None
        if (len(value) == 1 and next(iter(value)) in _ITEM_WRAP_KEYS
                and isinstance(next(iter(value.values())), (dict, list, str))):
            return _coerce_llm_list(next(iter(value.values())))
        if value and all(isinstance(key, str) and re.fullmatch(r"\d+", key)
                         for key in value):
            return [value[key] for key in sorted(value, key=int)]
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parsed = _try_json_payload(text)
        if parsed is not _UNRECOGNIZED:
            return _coerce_llm_list(parsed)
        xml_items = _try_xml_items(text)
        if xml_items is not None:
            return xml_items
    return None


_UNRECOGNIZED = object()


def _try_json_payload(text: str) -> Any:
    stripped = text.removeprefix("```json").removeprefix("```") \
        .removesuffix("```").strip()
    if not stripped.startswith(("[", "{")):
        return _UNRECOGNIZED
    try:
        return json.loads(stripped)
    except ValueError:
        return _UNRECOGNIZED


def _try_xml_items(text: str) -> Optional[list]:
    """把 <item>…</item> 串（可带一层容器标签）解析为子项 dict 列表。"""
    if not text.startswith("<"):
        return None
    try:
        root = ET.fromstring(f"<root>{text}</root>")
    except ET.ParseError:
        return None
    children = list(root)
    if not children:
        return []
    tags = [child.tag for child in children]
    if any(tag in _ITEM_WRAP_KEYS for tag in tags):
        picked = [child for child in children if child.tag in _ITEM_WRAP_KEYS]
    elif (len(children) == 1 and len(children[0]) > 0
          and all(child.tag == tags[0] for child in children)):
        picked = list(children[0])  # <attributes><item/></attributes> 类容器
    else:
        picked = children
    return [_xml_node_to_dict(node) for node in picked]


def _xml_node_to_dict(node: ET.Element) -> dict:
    out: dict = {}
    for child in node:
        key = child.tag
        if len(child) > 0:
            item = _xml_node_to_dict(child)
        else:
            item = (child.text or "").strip()
            if not item:
                continue
        if key in out:  # 同名兄弟标签（如多个 <enum>）收进列表
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(item)
        else:
            out[key] = item
    return out


def _annotation_allows_list(annotation: Any) -> bool:
    for candidate in (annotation, *get_args(annotation)):
        if get_origin(candidate) is list or candidate is list:
            return True
    return False


def _coerce_element_list_fields(model: type[BaseModel], raw: dict,
                                kind: Optional[str] = None) -> dict:
    """按模型的 list 字段矫正元素内的坏形态；无法识别的非空形态原样保留。

    子表字段（attributes/steps 等）内的子项还有自己的 list 字段
    （enum/inputs/source_objects），按 _NESTED_MODELS 的子模型再矫正一层。
    矫正出的空表不等于模型显式 ``[]`` 清空——非列表来源的空结果一律视为
    「未提供」，防止把空包装对象误读成清空指令。
    矫正发生在入参的浅拷贝上：orchestrator 把同一段 arguments dict 同时用于
    工具执行、step 审计持久化与下一轮对话历史，原地改写会让审计记录失真。
    """
    for name, field in model.model_fields.items():
        if not _annotation_allows_list(field.annotation):
            continue
        keys = [name]
        if isinstance(field.alias, str) and field.alias != name:
            keys.append(field.alias)
        present = [key for key in keys if key in raw]
        if not present:
            continue
        original = raw[present[0]]
        coerced = original if isinstance(original, list) else _coerce_llm_list(original)
        if coerced is None:
            if original is None or original == "" or original == {}:
                for key in present:
                    raw.pop(key, None)
            continue
        if not coerced and not isinstance(original, list):
            for key in present:
                raw.pop(key, None)
            continue
        for key in present:
            raw.pop(key, None)
        raw[name] = coerced
    if kind:
        for field, child_model in _NESTED_MODELS.get(kind, {}).items():
            value = raw.get(field)
            if isinstance(value, list):
                raw[field] = [
                    _coerce_element_list_fields(child_model, dict(item))
                    if isinstance(item, dict) else item
                    for item in value
                ]
    return raw


def empty_canvas() -> dict:
    return {k: [] for k in KIND_KEYS.values()}


def _ensure_canvas(canvas: Any) -> dict:
    out = dict(canvas) if isinstance(canvas, dict) else {}
    for k in KIND_KEYS.values():
        items = out.get(k)
        out[k] = list(items) if isinstance(items, list) else []
    return out


_NESTED_MODELS: dict[str, dict[str, type[BaseModel]]] = {
    "object": {"attributes": AttributeSpec, "relations": RelationSpec},
    "actor": {"attributes": AttributeSpec},
    "behavior": {"inputs": AttributeSpec},
    "scenario": {"branches": ScenarioBranch, "metrics": MetricSpec},
    "process": {"steps": ProcessStep, "branches": ProcessBranch, "metrics": MetricSpec},
}


def _canonical_patch(model: type[BaseModel], raw: dict) -> dict:
    """把 camel/snake 双写的稀疏补丁统一成模型内部字段名。

    这里故意不先做完整 model_validate：已存在的父/子元素允许仅凭 id
    修改一个字段，最终合并后的完整对象才接受 Pydantic 校验。
    """
    aliases: dict[str, str] = {}
    for name, field in model.model_fields.items():
        aliases[name] = name
        if isinstance(field.alias, str):
            aliases[field.alias] = name
    return {aliases[key]: value for key, value in raw.items() if key in aliases}


def _child_match_index(items: list[dict], raw: dict, model: type[BaseModel]) -> Optional[int]:
    child_id = str(raw.get("id") or "").strip()
    if child_id:
        for index, item in enumerate(items):
            if str(item.get("id") or "") == child_id:
                return index

    if model is AttributeSpec:
        name = norm_name(str(raw.get("name") or ""))
        if name:
            for index, item in enumerate(items):
                if norm_name(str(item.get("name") or "")) == name:
                    return index
    elif model is RelationSpec:
        name = norm_name(str(raw.get("name") or ""))
        target = norm_name(str(raw.get("target") or ""))
        if name:
            for index, item in enumerate(items):
                if norm_name(str(item.get("name") or "")) == name:
                    return index
        if target:
            matches = [
                index for index, item in enumerate(items)
                if norm_name(str(item.get("target") or "")) == target
            ]
            # 同一对象允许多条不同语义关系；只在目标唯一时把 target 当后备键。
            if len(matches) == 1:
                return matches[0]
    elif model is ScenarioBranch:
        from_step = raw.get("from_step", raw.get("fromStep"))
        condition = norm_name(str(raw.get("condition") or ""))
        if from_step is not None and condition:
            for index, item in enumerate(items):
                if item.get("from_step") == from_step \
                        and norm_name(str(item.get("condition") or "")) == condition:
                    return index
    elif model is ProcessStep:
        seq = raw.get("seq")
        name = norm_name(str(raw.get("name") or ""))
        if seq is not None and name:
            for index, item in enumerate(items):
                if item.get("seq") == seq \
                        and norm_name(str(item.get("name") or "")) == name:
                    return index
    elif model is ProcessBranch:
        from_step = raw.get("from_step", raw.get("fromStep"))
        condition = norm_name(str(raw.get("condition") or ""))
        if from_step is not None and condition:
            for index, item in enumerate(items):
                if item.get("from_step") == from_step \
                        and norm_name(str(item.get("condition") or "")) == condition:
                    return index
    elif model is MetricSpec:
        name = norm_name(str(raw.get("name") or ""))
        if name:
            for index, item in enumerate(items):
                if norm_name(str(item.get("name") or "")) == name:
                    return index
    return None


def _validation_message(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
        for item in error.errors()[:3]
    )


def _ensure_child_ids(items: list[dict], model: type[BaseModel]) -> list[dict]:
    """为支持 id 定位的结构化子项补稳定 id；不改变其余字段。"""
    if "id" not in model.model_fields:
        return items
    out: list[dict] = []
    for item in items:
        data = dict(item)
        data["id"] = data.get("id") or f"sub-{uuid.uuid4().hex[:10]}"
        out.append(data)
    return out


def _merge_nested_list(existing: list[dict], patches: list[Any],
                       model: type[BaseModel], field: str) -> tuple[list[dict], list[str]]:
    """合并结构化子项。

    - 非空列表是按 id（其次自然键）的增量补丁，不会替换未提及子项；
    - [] 仍是显式清空，兼容历史工具调用；
    - 子项 ``{"id": "...", "_delete": true}`` 是可审计的显式删除。
    """
    if not patches:
        return [], []

    items = [dict(item) for item in existing if isinstance(item, dict)]
    errors: list[str] = []
    for raw in patches:
        if not isinstance(raw, dict):
            errors.append(f"{field} 子项必须是对象，收到: {type(raw).__name__}")
            continue
        index = _child_match_index(items, raw, model)
        deleting = raw.get("_delete") is True
        if deleting:
            if index is None:
                identity = raw.get("id") or raw.get("name") or raw.get("target") or "?"
                errors.append(f"{field} 删除目标「{identity}」不存在")
            else:
                items.pop(index)
            continue

        patch = _canonical_patch(model, raw)
        try:
            if index is None:
                child = model.model_validate(patch).model_dump(exclude_none=True)
                child = _ensure_child_ids([child], model)[0]
                items.append(child)
            else:
                current = dict(items[index])
                # 子项中的显式 null 有实际含义（如 branch.to_step=null、清除 enum），
                # 因此与父元素沿用的“None 不覆盖”策略不同。
                candidate = {**current, **patch}
                child = model.model_validate(candidate).model_dump(exclude_none=True)
                child["id"] = current.get("id") or child.get("id") \
                    or f"sub-{uuid.uuid4().hex[:10]}"
                items[index] = child
        except ValidationError as error:
            identity = raw.get("id") or raw.get("name") or raw.get("target") or "?"
            errors.append(f"{field} 子项「{identity}」不合法: {_validation_message(error)}")

    return _ensure_child_ids(items, model), errors


def _new_element(model: type[_El], kind: str, raw: dict) -> dict:
    data = model.model_validate(raw).model_dump(exclude_none=True)
    data["id"] = data.get("id") or f"el-{uuid.uuid4().hex[:8]}"
    for field, child_model in _NESTED_MODELS.get(kind, {}).items():
        data[field] = _ensure_child_ids(
            [dict(item) for item in (data.get(field) or [])], child_model)
    return data


# 引用字段的占位词：写进画布会让质量门报「引用未定义」，反过来消耗回合清理
# （生产事故：流程 objects 写入「（待补）」）。引用必须真实，占位词在写入时拒绝。
_PLACEHOLDER_REF_RE = re.compile(r"待补|待定|TODO|TBD", re.IGNORECASE)


def _iter_reference_values(kind: str, element: dict):
    """产出 (字段名, 引用值)；只覆盖跨元素引用字段，不碰自由文本描述。"""

    def values(field: str):
        raw = element.get(field)
        if isinstance(raw, str) and raw.strip():
            yield field, raw
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    yield field, item

    if kind == "object":
        for rel in element.get("relations") or []:
            if isinstance(rel, dict):
                target = str(rel.get("target") or "")
                if target.strip():
                    yield "relations.target", target
    elif kind == "behavior":
        yield from values("actor")
        yield from values("object")
    elif kind == "event":
        yield from values("source")
    elif kind == "rule":
        yield from values("applies_to")
    elif kind == "process":
        yield from values("objects")
        for step in element.get("steps") or []:
            if isinstance(step, dict):
                for field in ("actor", "behavior"):
                    value = str(step.get(field) or "")
                    if value.strip():
                        yield f"steps.{field}", value
        for metric in element.get("metrics") or []:
            if isinstance(metric, dict):
                for source in metric.get("source_objects") or []:
                    if isinstance(source, str) and source.strip():
                        yield "metrics.source_objects", source
    elif kind == "scenario":
        for field in ("actors", "objects", "behaviors"):
            yield from values(field)
        yield from values("process_ref")


def _reference_placeholder_errors(kind: str, element: dict) -> list[str]:
    hits = [
        f"{field}「{value}」"
        for field, value in _iter_reference_values(kind, element)
        if _PLACEHOLDER_REF_RE.search(value)
    ]
    if not hits:
        return []
    return [
        f"元素「{element.get('name', '?')}」的引用字段含占位词（{'、'.join(hits[:3])}）——"
        "引用必须写真实对象/主体/行为名；未确认的引用先省略该字段或先向用户澄清。"
        "写入「待补/待定」会让质量门报「引用未定义」，再反过来消耗回合清理"
    ]


def upsert_elements(canvas: Any, kind: str, elements: Any) -> tuple[dict, list[str], list[str]]:
    """按 id（其次归一化 name）upsert；返回 (新画布, 生效元素 id 列表, 错误列表)。

    已有元素使用稀疏字段补丁。attributes / relations / inputs / branches /
    steps / metrics 使用子项 id（其次自然键）增量合并；只有显式 [] 才清空整表。

    elements 与元素内的列表字段容忍 LLM 的坏序列化形态（见 _coerce_llm_list）；
    始终返回全新 dict —— SQLAlchemy JSON 列必须整体重新赋值才会写库。
    """
    model = KIND_MODELS.get(kind)
    if model is None:
        return _ensure_canvas(canvas), [], [f"未知模型类别: {kind}（可选: {', '.join(KIND_MODELS)}）"]

    if elements is None:
        elements = []
    elif not isinstance(elements, list):
        coerced = _coerce_llm_list(elements)
        if coerced is None:
            return _ensure_canvas(canvas), [], [
                "elements 必须是元素对象的 JSON 数组；收到无法解析的"
                f" {type(elements).__name__} 形态。attributes/relations/steps 等"
                "子表字段同样必须是数组（不要用 {\"item\":…} 包装、索引对象或 XML 串）。"
            ]
        elements = coerced

    out = _ensure_canvas(canvas)
    key = KIND_KEYS[kind]
    items: list[dict] = [dict(x) for x in out[key]]
    applied: list[str] = []
    errors: list[str] = []
    for raw in elements or []:
        if not isinstance(raw, dict):
            errors.append(f"元素必须是对象，收到: {type(raw).__name__}")
            continue
        # 浅拷贝后再矫正：调用方（orchestrator step 审计 / 对话历史）还持有原 dict
        raw = _coerce_element_list_fields(model, dict(raw), kind)

        raw_id = str(raw.get("id") or "").strip()
        raw_name = str(raw.get("name") or "").strip()
        idx = None
        if raw_id:
            idx = next((i for i, item in enumerate(items)
                        if str(item.get("id") or "") == raw_id), None)
        if idx is None and raw_name:
            normalized = norm_name(raw_name)
            idx = next((i for i, item in enumerate(items)
                        if norm_name(str(item.get("name") or "")) == normalized), None)

        if idx is not None:
            current = dict(items[idx])
            patch = _canonical_patch(model, raw)
            data = dict(current)
            nested_errors: list[str] = []
            for field, value in patch.items():
                if field in _NESTED_MODELS.get(kind, {}):
                    if not isinstance(value, list):
                        nested_errors.append(f"{field}: 必须是数组")
                        continue
                    merged, child_errors = _merge_nested_list(
                        current.get(field) or [], value,
                        _NESTED_MODELS[kind][field], field)
                    data[field] = merged
                    nested_errors.extend(child_errors)
                elif value is not None:
                    # 与旧语义一致：父元素显式 null 不擦除已确认值。
                    data[field] = value
            if nested_errors:
                errors.append(
                    f"元素「{current.get('name', raw_name or raw_id or '?')}」不合法: "
                    + "; ".join(nested_errors[:3]))
                continue
            data["id"] = current.get("id") or raw_id or f"el-{uuid.uuid4().hex[:8]}"
            try:
                data = model.model_validate(data).model_dump(exclude_none=True)
            except ValidationError as error:
                errors.append(
                    f"元素「{current.get('name', raw_name or raw_id or '?')}」合并后不合法: "
                    f"{_validation_message(error)}")
                continue
            duplicate = next(
                (
                    item for item_index, item in enumerate(items)
                    if item_index != idx
                    and norm_name(str(item.get("name") or ""))
                    == norm_name(str(data.get("name") or ""))
                ),
                None,
            )
            if duplicate is not None:
                errors.append(
                    f"元素「{current.get('name', raw_name or raw_id or '?')}」重命名后与"
                    f"已有元素「{duplicate.get('name')}」的稳定 name 冲突；"
                    "请保留唯一英文标识符，不能仅靠 id 制造同名元素")
                continue
            items[idx] = data
        else:
            if not raw_name:
                errors.append(
                    f"元素「{raw_id or '?'}」不合法: 新元素必须提供 name；"
                    f"未找到 id「{raw_id or '?'}」对应的已有元素")
                continue
            try:
                data = _new_element(model, kind, raw)
            except ValidationError as error:
                errors.append(f"元素「{raw_name or '?'}」不合法: {_validation_message(error)}")
                continue
            items.append(data)
        placeholder_errors = _reference_placeholder_errors(kind, data)
        if placeholder_errors:
            errors.extend(placeholder_errors)
            continue
        applied.append(data["id"])

    out[key] = items
    return out, applied, errors


def canvas_elements_page(canvas: Any, kind: str, ids: Optional[list[str]] = None,
                         offset: int = 0, limit: int = 10,
                         fields: Optional[list[str]] = None,
                         nested_field: Optional[str] = None,
                         nested_offset: int = 0,
                         nested_limit: int = 50) -> dict:
    """读取完整 canonical 元素；支持极大元素的字段/嵌套列表分页。

    默认返回元素的全部字段。只有显式传 fields 或 nested_field 时才投影，
    且始终保留 id/name 便于下一次安全 patch。
    """
    if kind not in KIND_MODELS:
        raise ValueError(f"未知模型类别: {kind}")
    c = _ensure_canvas(canvas)
    source = [copy.deepcopy(item) for item in c[KIND_KEYS[kind]]
              if isinstance(item, dict)]
    requested = [str(value).strip() for value in (ids or []) if str(value).strip()]
    if requested:
        exact = set(requested)
        normalized = {norm_name(value) for value in requested}
        source = [
            item for item in source
            if str(item.get("id") or "") in exact
            or norm_name(str(item.get("name") or "")) in normalized
            or norm_name(str(item.get("display_name") or "")) in normalized
        ]

    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(50, int(limit or 10)))
    total = len(source)
    page_items = source[safe_offset:safe_offset + safe_limit]

    allowed_fields = set(KIND_MODELS[kind].model_fields)
    selected_fields: Optional[list[str]] = None
    if fields:
        selected_fields = []
        for raw_field in fields:
            field = str(raw_field).strip()
            snake = next(
                (name for name, info in KIND_MODELS[kind].model_fields.items()
                 if field in {name, info.alias}), None)
            if snake and snake not in selected_fields:
                selected_fields.append(snake)
        if not selected_fields:
            raise ValueError("fields 未包含该模型的合法字段")

    nested_name = str(nested_field or "").strip()
    if nested_name:
        nested_name = next(
            (name for name, info in KIND_MODELS[kind].model_fields.items()
             if nested_name in {name, info.alias}), "")
        if not nested_name or nested_name not in allowed_fields:
            raise ValueError(f"nested_field 不是 {kind} 的合法字段")
        if nested_name not in _NESTED_MODELS.get(kind, {}):
            raise ValueError(f"{nested_name} 不是可分页的结构化子项字段")

    nested_pages: list[dict] = []
    projected: list[dict] = []
    for item in page_items:
        if selected_fields is not None:
            item = {key: value for key, value in item.items()
                    if key in {"id", "name", nested_name} or key in selected_fields}
        if nested_name:
            values = list(item.get(nested_name) or [])
            start = max(0, int(nested_offset or 0))
            size = max(1, min(200, int(nested_limit or 50)))
            item = {
                "id": item.get("id"),
                "name": item.get("name"),
                nested_name: values[start:start + size],
            }
            nested_pages.append({
                "elementId": item.get("id"),
                "field": nested_name,
                "offset": start,
                "limit": size,
                "returned": len(item[nested_name]),
                "total": len(values),
                "hasMore": start + size < len(values),
                "nextOffset": start + size if start + size < len(values) else None,
            })
        projected.append(item)

    has_more = safe_offset + safe_limit < total
    return {
        "kind": kind,
        "elements": projected,
        "page": {
            "offset": safe_offset,
            "limit": safe_limit,
            "returned": len(projected),
            "total": total,
            "hasMore": has_more,
            "nextOffset": safe_offset + safe_limit if has_more else None,
        },
        "nestedPages": nested_pages,
        "truncated": has_more or any(page["hasMore"] for page in nested_pages),
    }


def canonical_snapshot(canvas: Any, max_chars: int = 24_000) -> dict:
    """给系统提示使用的合法 JSON 快照；超限时明确降级为索引而非截断 JSON。"""
    c = copy.deepcopy(_ensure_canvas(canvas))
    full = {"complete": True, "canvas": c}
    serialized = json.dumps(full, ensure_ascii=False, separators=(",", ":"), default=str)
    safe_cap = max(1_000, int(max_chars))
    if len(serialized) <= safe_cap:
        return full

    # 降级索引自身也必须有界，避免极端元素数量再次撑爆上下文。
    index_limit = 50
    fallback = {
        "complete": False,
        "reason": "canonical canvas exceeds inline context budget; use get_canvas_elements",
        "serializedChars": len(serialized),
        "counts": {key: len(c[key]) for key in KIND_KEYS.values()},
        "index": {
            key: [
                {"id": str(item.get("id") or "")[:120],
                 "name": str(item.get("name") or "")[:120],
                 "display_name": str(item.get("display_name") or "")[:120]}
                for item in c[key][:index_limit] if isinstance(item, dict)
            ]
            for key in KIND_KEYS.values()
        },
        "indexTruncated": {
            key: len(c[key]) > index_limit for key in KIND_KEYS.values()
        },
    }
    fallback_json = json.dumps(
        fallback, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(fallback_json) <= safe_cap:
        return fallback
    return {
        "complete": False,
        "reason": "canonical canvas and index exceed inline context budget; "
                  "use get_canvas_elements",
        "serializedChars": len(serialized),
        "counts": fallback["counts"],
        "indexOmitted": True,
    }


def canonical_snapshot_json(canvas: Any, max_chars: int = 24_000) -> str:
    snapshot = canonical_snapshot(canvas, max_chars=max_chars)
    pretty = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    if len(pretty) <= max(1_000, int(max_chars)):
        return pretty
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), default=str)


def remove_elements(canvas: Any, kind: str, ids: list[str]) -> tuple[dict, int, list[str]]:
    """按 id 或名称删除；返回 (新画布, 删除数, 未命中列表)。"""
    if kind not in KIND_MODELS:
        return _ensure_canvas(canvas), 0, [f"未知模型类别: {kind}"]
    out = _ensure_canvas(canvas)
    key = KIND_KEYS[kind]
    targets = {str(i) for i in (ids or [])} | {norm_name(str(i)) for i in (ids or [])}
    targets.discard("")
    kept: list[dict] = []
    hit_keys: set[str] = set()
    for x in out[key]:
        hit = ({x.get("id"), norm_name(x.get("name", ""))} - {None, ""}) & targets
        if hit:
            hit_keys |= hit
        else:
            kept.append(x)
    removed = len(out[key]) - len(kept)
    missing = [str(i) for i in (ids or [])
               if str(i) not in hit_keys and norm_name(str(i)) not in hit_keys]
    out[key] = kept
    return out, removed, missing


def completeness(canvas: Any) -> dict:
    """完整度信号：各类计数 + 缺口清单（驱动前端提示与 agent 追问方向）。"""
    c = _ensure_canvas(canvas)
    counts = {k: len(v) for k, v in c.items() if k in KIND_KEYS.values()}
    gaps: list[str] = []

    obj_names = {norm_name(o.get("name", "")) for o in c["objects"]} \
        | {norm_name(o.get("display_name", "")) for o in c["objects"] if o.get("display_name")}
    actor_names = {norm_name(a.get("name", "")) for a in c["actors"]} \
        | {norm_name(a.get("display_name", "")) for a in c["actors"] if a.get("display_name")}
    entity_names = obj_names | {
        n for a in c["actors"] if (a.get("kind") or "role") != "system"
        for n in (norm_name(a.get("name", "")), norm_name(a.get("display_name", ""))) if n
    }
    beh_names = {norm_name(b.get("name", "")) for b in c["behaviors"]}

    for o in c["objects"]:
        if not o.get("attributes"):
            gaps.append(f"对象「{o.get('name')}」还没有属性")
        elif not o.get("key_attribute"):
            gaps.append(f"对象「{o.get('name')}」未指定业务主键属性")
        for r in o.get("relations") or []:
            if norm_name(r.get("target", "")) not in entity_names:
                gaps.append(f"对象「{o.get('name')}」的关系指向未定义的对象/主体「{r.get('target')}」")
    for a in c["actors"]:
        # person/org 类主体是数据实体，应像对象一样有属性（system/role 可无）
        if (a.get("kind") or "role") in ("person", "org") and not a.get("attributes"):
            label = a.get("display_name") or a.get("name")
            gaps.append(f"主体「{label}」是 {a.get('kind')} 类数据实体，还没有属性（如编码/名称/联系方式/状态）")
    for b in c["behaviors"]:
        if not b.get("actor"):
            gaps.append(f"行为「{b.get('name')}」缺少执行主体")
        elif norm_name(b.get("actor", "")) not in actor_names | obj_names:
            gaps.append(f"行为「{b.get('name')}」的执行主体「{b.get('actor')}」尚未在主体模型中定义")
        if not b.get("object"):
            gaps.append(f"行为「{b.get('name')}」缺少作用对象")
        elif norm_name(b.get("object", "")) not in entity_names:
            gaps.append(f"行为「{b.get('name')}」作用的对象「{b.get('object')}」尚未在对象/主体模型中定义")
    for r in c["rules"]:
        tgt = norm_name(r.get("applies_to", "") or "")
        if tgt and tgt not in obj_names | beh_names:
            gaps.append(f"规则「{r.get('name')}」作用的「{r.get('applies_to')}」未在对象/行为模型中定义")
    for p in c["processes"]:
        pname = p.get("name")
        steps = p.get("steps") or []
        for step in steps:
            actor = str(step.get("actor") or "").strip()
            if actor and norm_name(actor) not in entity_names:
                gaps.append(f"流程「{pname}」步骤「{step.get('name')}」的执行主体「{actor}」尚未在对象/主体模型中定义")
            behavior = str(step.get("behavior") or "").strip()
            if behavior and norm_name(behavior) not in beh_names:
                gaps.append(f"流程「{pname}」步骤「{step.get('name')}」绑定的行为「{behavior}」尚未在行为模型中定义")
        for branch in p.get("branches") or []:
            source = branch.get("from_step")
            destination = branch.get("to_step")
            if not isinstance(source, int) or source < 1 or source > len(steps):
                gaps.append(f"流程「{pname}」的分支起点 {source} 不在步骤 1..{len(steps)} 范围内")
            elif destination is not None and (
                not isinstance(destination, int) or destination < 1 or destination > len(steps)
            ):
                gaps.append(f"流程「{pname}」的分支目标 {destination} 不在步骤范围内")
        for metric in p.get("metrics") or []:
            for ref in metric.get("source_objects") or []:
                if norm_name(ref) not in entity_names:
                    gaps.append(f"流程「{pname}」度量「{metric.get('name')}」的来源对象「{ref}」尚未在对象/主体模型中定义")
    if counts["objects"] and not counts["scenarios"]:
        gaps.append("还没有场景模型 —— 场景用于验收本体草稿能否表达完整业务流程")
    if counts["objects"] and not counts["processes"]:
        # 仅建议项：流程是 P0 新模型，绝不进质量门 blocking（vacuous pass 铁律）
        gaps.append("还没有流程模型 —— 流程编排步骤/分支/度量，场景可挂接为情境变体")
    for p in c["processes"]:
        if not (p.get("metrics") or []):
            gaps.append(f"流程「{p.get('name')}」还没有产出度量 —— 用什么数字证明流程达成了目标？")
    if counts["objects"] and not counts["actors"]:
        gaps.append("还没有主体模型 —— 谁在使用这些对象、执行这些行为？")

    return {"counts": counts, "gaps": gaps[:20]}


def canvas_summary(canvas: Any, max_items: int = 30) -> str:
    """画布的紧凑文本摘要，注入探索 agent 系统提示。"""
    c = _ensure_canvas(canvas)
    lines: list[str] = []
    for kind, key in KIND_KEYS.items():
        items = c[key]
        if not items:
            lines.append(f"- {KIND_LABELS[kind]}模型: (空)")
            continue
        parts = []
        for x in items[:max_items]:
            name = x.get("name", "?")
            extra = ""
            if kind == "object":
                attrs = [a.get("name", "") for a in (x.get("attributes") or [])]
                rels = [f"→{r.get('target')}" for r in (x.get("relations") or [])]
                extra = f"({', '.join(attrs[:12])}{'…' if len(attrs) > 12 else ''}" \
                        + (f"; 关系: {', '.join(rels)}" if rels else "") + ")"
            elif kind == "behavior":
                extra = f"({x.get('actor') or '?'} 对 {x.get('object') or '?'})"
            elif kind == "rule":
                extra = f"[{x.get('kind', '')}→{x.get('applies_to') or '?'}]"
            elif kind == "actor":
                attrs = [a.get("name", "") for a in (x.get("attributes") or [])]
                extra = f"[{x.get('kind', '')}]" \
                        + (f"({', '.join(attrs[:12])}{'…' if len(attrs) > 12 else ''})" if attrs else "")
            elif kind == "process":
                extra = f"({len(x.get('steps') or [])}步 {len(x.get('branches') or [])}分支 " \
                        f"{len(x.get('metrics') or [])}指标)"
            parts.append(f"{name}{extra} (id={x.get('id')})")
        suffix = f" …共{len(items)}项" if len(items) > max_items else ""
        lines.append(f"- {KIND_LABELS[kind]}模型({len(items)}): " + "; ".join(parts) + suffix)
    return "\n".join(lines)
