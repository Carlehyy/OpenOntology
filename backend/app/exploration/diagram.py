"""画布 → 图表的确定性生成（不经 LLM）

四种业务建模视图，全部是画布的纯投影 —— 与转化管线同一哲学：
能确定性生成的绝不交给模型，图与画布严格一致，agent 只负责"何时出图"。

  er        实体关系图：对象 + person/org 主体（它们都将转成 ObjectType）
  flow      业务流程图：某场景或流程的步骤链 + 显式条件分支
  sequence  时序图：某场景中 主体→对象 的行为协作顺序 / 流程步骤绑定的行为
  state     状态图：某对象的枚举状态属性 + 从行为结果中识别的状态迁移

对话中 agent 通过 show_diagram 工具出图（步骤事件携带 mermaid，前端直接
渲染），让用户"看图挑错"—— 图形化误解暴露远快于文字确认。
"""
from __future__ import annotations

import re

from app.exploration.canvas import _ensure_canvas, norm_name
from app.exploration.converter import map_type_hint

_CARDINALITY_MERMAID = {
    "one-to-one": "||--||",
    "one-to-many": "||--o{",
    "many-to-one": "}o--||",
    "many-to-many": "}o--o{",
}

DIAGRAM_KINDS = {
    "er": "ER 图（实体关系）",
    "flow": "业务流程图",
    "sequence": "时序图（主体协作）",
    "state": "状态图（对象生命周期）",
}


class DiagramError(ValueError):
    """画布尚不具备生成该图的条件 —— 信息原样回给 agent/用户，指明先补什么。"""


def _ident(name: str, fallback: str) -> str:
    """mermaid 的实体/节点标识符：字母数字下划线与中文安全。"""
    s = re.sub(r"[^\w一-鿿]+", "_", (name or "").strip()).strip("_")
    return s or fallback


def _text(s: str, cap: int = 60) -> str:
    """节点/消息文本：去掉会破坏 mermaid 语法的字符。"""
    t = re.sub(r'[\[\]{}()<>"`;|]', " ", str(s or "")).strip()
    t = re.sub(r"\s+", " ", t)
    return (t[: cap - 1] + "…") if len(t) > cap else t


# ---------------------------------------------------------------- ER 图


def er_mermaid(canvas) -> str:
    """对象模型（含数据实体型主体，它们同样转成 ObjectType）→ mermaid erDiagram。"""
    c = _ensure_canvas(canvas)
    # person/org 主体是数据实体恒入图；role 主体仅在被关系引用时入图（避免悄悄丢边）；
    # system 主体不建对象不入图；与对象重名时以对象为准 —— 全部与转化管线口径一致
    obj_norms = {norm_name(o.get("name", "")) for o in c["objects"]}
    referenced = {norm_name(r.get("target", ""))
                  for o in c["objects"] for r in (o.get("relations") or [])}

    def actor_in_er(a: dict) -> bool:
        kind = a.get("kind") or "role"
        if kind == "system" or norm_name(a.get("name", "")) in obj_norms:
            return False
        return kind in ("person", "org") \
            or norm_name(a.get("name", "")) in referenced \
            or norm_name(a.get("display_name", "")) in referenced

    entities = list(c["objects"]) + [a for a in c["actors"] if actor_in_er(a)]
    if not entities:
        raise DiagramError("画布还没有对象模型 —— 先在对话中沉淀业务对象")

    entity_names = {
        norm_name(value)
        for entity in entities
        for value in (entity.get("name", ""), entity.get("display_name", ""))
        if value
    }
    issues: list[str] = []
    ident_owners: dict[str, str] = {}
    for index, entity in enumerate(entities):
        label = _slabel(entity)
        ident = _ident(entity.get("name", ""), f"Object{index + 1}")
        owner = ident_owners.get(ident)
        if owner and owner != label:
            issues.append(f"实体「{owner}」与「{label}」生成了相同图标识 {ident}")
        ident_owners[ident] = label
    for obj in c["objects"]:
        label = _slabel(obj)
        attributes = obj.get("attributes") or []
        attr_names = {norm_name(attr.get("name", "")) for attr in attributes}
        key = obj.get("key_attribute")
        if not attributes:
            issues.append(f"对象「{label}」没有属性，禁止伪造 id 字段出图")
        untyped = [attr.get("name", "?") for attr in attributes
                   if not str(attr.get("type_hint") or "").strip()]
        if untyped:
            issues.append(f"对象「{label}」的属性 {', '.join(untyped[:6])} 缺少类型")
        if not key:
            issues.append(f"对象「{label}」未指定业务主键")
        elif norm_name(key) not in attr_names:
            issues.append(f"对象「{label}」的主键「{key}」不在属性列表")
        for relation in obj.get("relations") or []:
            target = relation.get("target", "")
            if norm_name(target) not in entity_names:
                issues.append(f"关系「{label} → {target}」目标未定义")
            cardinality = str(relation.get("cardinality") or "").strip().lower()
            if cardinality not in _CARDINALITY_MERMAID:
                issues.append(f"关系「{label} → {target}」缺少或使用了非法基数")
    for actor in c["actors"]:
        if (actor.get("kind") or "role") not in ("person", "org"):
            continue
        label = _slabel(actor)
        attributes = actor.get("attributes") or []
        attr_names = {norm_name(attr.get("name", "")) for attr in attributes}
        key = actor.get("key_attribute")
        if not attributes:
            issues.append(f"主体「{label}」是数据实体但没有属性")
        elif not key:
            issues.append(f"主体「{label}」是数据实体但未指定业务主键")
        elif norm_name(key) not in attr_names:
            issues.append(f"主体「{label}」的主键「{key}」不在属性列表")
        untyped = [attr.get("name", "?") for attr in attributes
                   if not str(attr.get("type_hint") or "").strip()]
        if untyped:
            issues.append(f"主体「{label}」的属性 {', '.join(untyped[:6])} 缺少类型")
    if issues:
        raise DiagramError("ER 图质量校验未通过：" + "；".join(issues[:8])
                           + "。请让 AI 修复对象主键、关系端点与基数后重试")

    lines = ["erDiagram"]
    entity_by_name: dict[str, str] = {}
    for i, o in enumerate(entities):
        ent = _ident(o.get("name", ""), f"Object{i + 1}")
        entity_by_name[norm_name(o.get("name", ""))] = ent
        if o.get("display_name"):   # 关系 target 允许写显示名
            entity_by_name.setdefault(norm_name(o["display_name"]), ent)
        key_attr = norm_name(o.get("key_attribute") or "")
        attr_lines = []
        for j, a in enumerate(o.get("attributes") or []):
            aname = _ident(a.get("name", ""), f"field{j + 1}")
            atype = map_type_hint(a.get("type_hint"))
            pk = " PK" if key_attr and norm_name(a.get("name", "")) == key_attr else ""
            attr_lines.append(f"        {atype} {aname}{pk}")
        lines.append(f"    {ent} {{")
        lines.extend(attr_lines)
        lines.append("    }")

    for o in c["objects"]:
        src = entity_by_name.get(norm_name(o.get("name", "")))
        for r in o.get("relations") or []:
            tgt = entity_by_name.get(norm_name(r.get("target", "")))
            # 端点与基数已在出图前统一校验，不再静默丢边或默认 one-to-many。
            edge = _CARDINALITY_MERMAID[(r.get("cardinality") or "").strip().lower()]
            label = (r.get("display_name") or r.get("name") or "关联").replace('"', "'")
            lines.append(f'    {src} {edge} {tgt} : "{label}"')

    return "\n".join(lines)


# ---------------------------------------------------------------- 场景/流程定位


def _pick_scenario(c: dict, target: str | None) -> dict:
    scenarios = c["scenarios"]
    if not scenarios:
        raise DiagramError("画布还没有场景模型 —— 先与用户确认一个端到端业务场景")
    if target:
        t = norm_name(target)
        for s in scenarios:
            if norm_name(s.get("name", "")) == t or norm_name(s.get("display_name", "")) == t:
                return s
        raise DiagramError(f"场景「{target}」不存在。已有场景: "
                           + ", ".join(str(s.get("display_name") or s.get("name")) for s in scenarios))
    return scenarios[0]


def _pick_flow_target(c: dict, target: str | None) -> tuple[dict, str]:
    """flow/sequence 的 target 解析：在场景与流程两个集合中定位，返回 (元素, kind)。

    与 _pick_state_object 同一口径：稳定 id 精确匹配优先，其次 name/display_name；
    多候选（含跨类同名）报歧义错误要求改用元素 id —— 不沿用 _pick_scenario 的
    「第一命中即返回」。target 缺省时取第一个场景，无场景时取第一个流程。
    """
    scenarios = c["scenarios"]
    processes = c["processes"]
    if target:
        t = str(target)
        for kind, collection in (("scenario", scenarios), ("process", processes)):
            hit = next((item for item in collection if str(item.get("id") or "") == t), None)
            if hit is not None:
                return hit, kind
        normalized = norm_name(t)
        matches = [
            (item, kind)
            for kind, collection in (("scenario", scenarios), ("process", processes))
            for item in collection
            if norm_name(item.get("name", "")) == normalized
            or norm_name(item.get("display_name", "")) == normalized
        ]
        if len(matches) > 1:
            raise DiagramError(
                f"「{target}」匹配到多个场景/流程候选，请使用画布元素 id 精确指定")
        if matches:
            return matches[0]
        raise DiagramError(f"场景或流程「{target}」不存在")
    if scenarios:
        return scenarios[0], "scenario"
    if processes:
        return processes[0], "process"
    raise DiagramError("画布还没有场景或流程模型 —— 先与用户确认一个端到端业务流程")


def _pick_process(c: dict, target: str | None) -> dict:
    """仅在流程集合中定位（质量门逐个流程分析时用稳定 id 精确指定）。"""
    processes = c["processes"]
    if not processes:
        raise DiagramError("画布还没有流程模型 —— 先与用户梳理一个端到端业务流程")
    if target:
        process = next(
            (item for item in processes if str(item.get("id") or "") == str(target)),
            None,
        )
        if process is None:
            normalized = norm_name(str(target))
            matches = [
                item for item in processes
                if norm_name(item.get("name", "")) == normalized
                or norm_name(item.get("display_name", "")) == normalized
            ]
            if len(matches) > 1:
                raise DiagramError(
                    f"流程「{target}」匹配到多个候选，请使用画布元素 id 精确指定")
            process = matches[0] if matches else None
        if process is None:
            raise DiagramError(f"流程「{target}」不存在")
        return process
    return processes[0]


def _slabel(x: dict) -> str:
    return str(x.get("display_name") or x.get("name") or "?")


# ---------------------------------------------------------------- 业务流程图


_CONDITIONAL_STEP_RE = re.compile(
    r"(?:如果|是否|否则|若(?!干)|条件(?:为|是|满足)|\bif\b|\bwhen\b|[?？])",
    re.IGNORECASE,
)


def _branch_issues(nodes: list[int], branches: list[dict],
                   regex_conditional: set[int]) -> tuple[list[str], dict[int, list[dict]], list[int]]:
    """分支字段校验 + 条件步骤 ≥2 出边 + 条件去重（场景/流程共用的纯图口径）。

    nodes 是全部步骤的合法编号（1-based；流程按 seq 升序后的位置）。条件步骤
    只用显式分支边、无隐含 next-step 边 —— 条件节点由步骤文本正则命中与显式
    分支源的并集决定。
    """
    valid = set(nodes)
    issues: list[str] = []
    by_source: dict[int, list[dict]] = {}
    for branch in branches:
        source = branch.get("from_step")
        destination = branch.get("to_step")
        condition = str(branch.get("condition") or "").strip()
        if not isinstance(source, int) or source not in valid:
            issues.append(f"分支起点 {source} 不在步骤 1..{len(nodes)} 范围内")
            continue
        if destination is not None and (
            not isinstance(destination, int) or destination not in valid
        ):
            issues.append(f"步骤 {source} 的分支目标 {destination} 不在步骤范围内")
            continue
        if not condition:
            issues.append(f"步骤 {source} 的分支缺少条件标签")
            continue
        by_source.setdefault(source, []).append(branch)

    conditional_steps = set(regex_conditional) | set(by_source)
    for index in sorted(conditional_steps):
        outgoing = by_source.get(index, [])
        if len(outgoing) < 2:
            issues.append(f"条件步骤 {index} 至少需要 2 条显式 branches，当前 {len(outgoing)} 条")
        conditions = [norm_name(str(branch.get("condition") or "")) for branch in outgoing]
        if len(conditions) != len(set(conditions)):
            issues.append(f"条件步骤 {index} 存在重复分支条件")
    return issues, by_source, sorted(conditional_steps)


def _flow_graph_issues(nodes: list[int], by_source: dict[int, list[dict]]) -> list[str]:
    """可达性/可结束性双 BFS（场景/流程共用的纯图口径）。

    非条件步骤有隐含 next-step 边；条件步骤（by_source 中的源）只用显式分支边；
    最后一步与 to_step=None 的分支通向结束节点。
    """
    order = list(nodes)
    if not order:
        return []
    nxt = {node: order[i + 1] for i, node in enumerate(order[:-1])}
    adjacency: dict[int, set[int]] = {node: set() for node in order}
    for node in order:
        if node in by_source:
            adjacency[node].update(int(branch["to_step"])
                                   for branch in by_source[node]
                                   if branch.get("to_step") is not None)
        elif node in nxt:
            adjacency[node].add(nxt[node])
    # 从第 1 步做可达性检查；被孤立的步骤不能进入平台图表。
    reachable: set[int] = set()
    stack = [order[0]]
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(adjacency[node] - reachable)
    issues: list[str] = []
    unreachable = [str(i) for i in order if i not in reachable]
    if unreachable:
        issues.append("以下步骤从流程起点不可达：" + "、".join(unreachable))
    reverse: dict[int, set[int]] = {node: set() for node in order}
    end_predecessors: set[int] = set()
    for source, destinations in adjacency.items():
        for destination in destinations:
            reverse[destination].add(source)
        if source in by_source:
            if any(branch.get("to_step") is None for branch in by_source[source]):
                end_predecessors.add(source)
        elif source == order[-1]:
            end_predecessors.add(source)
    can_finish: set[int] = set()
    stack = list(end_predecessors)
    while stack:
        node = stack.pop()
        if node in can_finish:
            continue
        can_finish.add(node)
        stack.extend(reverse[node] - can_finish)
    no_exit = [str(i) for i in sorted(reachable) if i not in can_finish]
    if no_exit:
        issues.append("以下步骤没有通往结束节点的路径：" + "、".join(no_exit))
    return issues


def scenario_model_analysis(canvas, target: str | None = None) -> dict:
    """场景的确定性完整性分析，同时供质量门、流程图和时序图使用。

    场景不是一个“有名字就算存在”的标签，而是端到端验收用例。目标、参与者、
    步骤、对象/行为引用、预期结果和条件分支缺一块，都不能用它证明本体可表达。
    """
    c = _ensure_canvas(canvas)
    return _scenario_analysis(c, _pick_scenario(c, target))


def _scenario_analysis(c: dict, scenario: dict) -> dict:
    """场景分析主体（target 已由调用方解析成具体场景元素）。"""
    label = _slabel(scenario)
    issues: list[str] = []
    warnings: list[str] = []

    if not str(scenario.get("goal") or "").strip():
        issues.append("缺少业务目标（goal）")
    if not str(scenario.get("expected_outcome") or "").strip():
        issues.append("缺少可验收的预期结果（expected_outcome）")

    steps = scenario.get("steps") or []
    if not steps:
        issues.append("没有流程步骤（steps）")
    else:
        blank_steps = [str(index) for index, step in enumerate(steps, 1) if not str(step).strip()]
        if blank_steps:
            issues.append("存在空白步骤：" + "、".join(blank_steps[:8]))

    actor_lookup = {
        norm_name(str(value)): actor
        for actor in c["actors"]
        for value in (actor.get("name"), actor.get("display_name"), actor.get("id"))
        if value
    }
    entity_lookup = {
        norm_name(str(value)): entity
        for entity in (
            list(c["objects"])
            + [actor for actor in c["actors"] if (actor.get("kind") or "role") != "system"]
        )
        for value in (entity.get("name"), entity.get("display_name"), entity.get("id"))
        if value
    }
    behavior_lookup = {
        norm_name(str(value)): behavior
        for behavior in c["behaviors"]
        for value in (behavior.get("name"), behavior.get("display_name"), behavior.get("id"))
        if value
    }

    raw_actors = [str(value) for value in (scenario.get("actors") or []) if str(value).strip()]
    raw_objects = [str(value) for value in (scenario.get("objects") or []) if str(value).strip()]
    raw_behaviors = [str(value) for value in (scenario.get("behaviors") or []) if str(value).strip()]
    if not raw_actors:
        issues.append("没有参与主体引用（actors）")
    if not raw_objects:
        issues.append("没有业务对象引用（objects）")
    if not raw_behaviors:
        issues.append("没有业务行为引用（behaviors）")

    unresolved_actors = [value for value in raw_actors if norm_name(value) not in actor_lookup]
    unresolved_objects = [value for value in raw_objects if norm_name(value) not in entity_lookup]
    unresolved_behaviors = [value for value in raw_behaviors if norm_name(value) not in behavior_lookup]
    if unresolved_actors:
        issues.append("引用未定义主体：" + "、".join(unresolved_actors[:8]))
    if unresolved_objects:
        issues.append("引用未定义对象/实体主体：" + "、".join(unresolved_objects[:8]))
    if unresolved_behaviors:
        issues.append("引用未定义行为：" + "、".join(unresolved_behaviors[:8]))

    scenario_actor_names = {norm_name(value) for value in raw_actors}
    scenario_object_names = {norm_name(value) for value in raw_objects}
    for raw in raw_behaviors:
        behavior = behavior_lookup.get(norm_name(raw))
        if behavior is None:
            continue
        behavior_label = _slabel(behavior)
        actor = norm_name(str(behavior.get("actor") or ""))
        obj = norm_name(str(behavior.get("object") or ""))
        # 同一实体允许场景/行为分别使用 name 与 display_name，因此通过 lookup
        # 解析成对象身份后比较，而不是直接比较字符串。
        actor_entity = actor_lookup.get(actor)
        actor_aliases = {
            norm_name(str(value))
            for value in (
                (actor_entity or {}).get("name"),
                (actor_entity or {}).get("display_name"),
                (actor_entity or {}).get("id"),
            )
            if value
        }
        object_entity = entity_lookup.get(obj)
        object_aliases = {
            norm_name(str(value))
            for value in (
                (object_entity or {}).get("name"),
                (object_entity or {}).get("display_name"),
                (object_entity or {}).get("id"),
            )
            if value
        }
        if actor and actor_entity is not None and not (actor_aliases & scenario_actor_names):
            issues.append(f"行为「{behavior_label}」的执行主体未列入场景 actors")
        if obj and object_entity is not None and not (object_aliases & scenario_object_names):
            issues.append(f"行为「{behavior_label}」的作用对象未列入场景 objects")

    branches = scenario.get("branches") or []
    nodes = list(range(1, len(steps) + 1))
    regex_conditional = {
        index for index, raw in enumerate(steps, 1)
        if _CONDITIONAL_STEP_RE.search(str(raw))
    }
    branch_issues, by_source, conditional_steps = _branch_issues(
        nodes, branches, regex_conditional)
    issues.extend(branch_issues)

    return {
        "scenario": scenario,
        "label": label,
        "steps": steps,
        "branches": branches,
        "by_source": by_source,
        "conditional_steps": conditional_steps,
        "issues": issues,
        "warnings": warnings,
    }


def process_model_analysis(canvas, target: str | None = None) -> dict:
    """流程的确定性完整性分析，同时供质量门、流程图和时序图使用。

    流程是端到端标准骨架：业务目标、按 seq 排序的步骤（主体/行为显式绑定）、
    显式分支、对象引用与预期结果缺一块，都不能作为场景的挂接主干。
    goal/expected_outcome 口径与场景一致；分支与可达性校验复用
    _branch_issues/_flow_graph_issues，不复制第二份口径。
    """
    c = _ensure_canvas(canvas)
    process = _pick_process(c, target)
    label = _slabel(process)
    issues: list[str] = []
    warnings: list[str] = []

    if not str(process.get("goal") or "").strip():
        issues.append("缺少业务目标（goal）")
    if not str(process.get("expected_outcome") or "").strip():
        issues.append("缺少可验收的预期结果（expected_outcome）")

    steps = sorted(
        (step for step in (process.get("steps") or []) if isinstance(step, dict)),
        key=lambda step: int(step.get("seq") or 0),
    )
    if not steps:
        issues.append("没有流程步骤（steps）")

    # 实体口径与 readiness 一致：对象 + 非 system 主体（它们都将转成 ObjectType）
    entity_lookup = {
        norm_name(str(value)): entity
        for entity in (
            list(c["objects"])
            + [actor for actor in c["actors"] if (actor.get("kind") or "role") != "system"]
        )
        for value in (entity.get("name"), entity.get("display_name"), entity.get("id"))
        if value
    }
    behavior_lookup = {
        norm_name(str(value)): behavior
        for behavior in c["behaviors"]
        for value in (behavior.get("name"), behavior.get("display_name"), behavior.get("id"))
        if value
    }
    unresolved_step_actors: list[str] = []
    unresolved_step_behaviors: list[str] = []
    for index, step in enumerate(steps, 1):
        actor = str(step.get("actor") or "").strip()
        if actor and norm_name(actor) not in entity_lookup:
            unresolved_step_actors.append(f"步骤{index}的「{actor}」")
        behavior = str(step.get("behavior") or "").strip()
        if behavior and norm_name(behavior) not in behavior_lookup:
            unresolved_step_behaviors.append(f"步骤{index}的「{behavior}」")
    if unresolved_step_actors:
        issues.append("步骤引用未定义主体：" + "、".join(unresolved_step_actors[:8]))
    if unresolved_step_behaviors:
        issues.append("步骤引用未定义行为：" + "、".join(unresolved_step_behaviors[:8]))

    raw_objects = [str(value) for value in (process.get("objects") or []) if str(value).strip()]
    unresolved_objects = [value for value in raw_objects if norm_name(value) not in entity_lookup]
    if unresolved_objects:
        issues.append("引用未定义对象/实体主体：" + "、".join(unresolved_objects[:8]))

    branches = process.get("branches") or []
    nodes = list(range(1, len(steps) + 1))
    regex_conditional = {
        index for index, step in enumerate(steps, 1)
        if _CONDITIONAL_STEP_RE.search(
            f"{step.get('name') or ''} {step.get('description') or ''}")
    }
    branch_issues, by_source, conditional_steps = _branch_issues(
        nodes, branches, regex_conditional)
    issues.extend(branch_issues)
    # 分支字段合法后才做可达/可结束校验（与场景「先分析、再走查」两阶段口径一致）
    if steps and not branch_issues:
        issues.extend(_flow_graph_issues(nodes, by_source))

    return {
        "process": process,
        "label": label,
        "steps": steps,
        "branches": branches,
        "by_source": by_source,
        "conditional_steps": conditional_steps,
        "issues": issues,
        "warnings": warnings,
    }


def flow_mermaid(canvas, target: str | None = None) -> tuple[str, str, list[str]]:
    """场景/流程步骤 → flowchart；条件步骤必须有显式、可验证的分支目标。"""
    c = _ensure_canvas(canvas)
    element, kind = _pick_flow_target(c, target)
    if kind == "process":
        return _process_flow_mermaid(c, process_model_analysis(
            c, element.get("id") or element.get("name")))
    if str(element.get("process_ref") or "").strip() and not (element.get("steps") or []):
        raise DiagramError(f"场景「{_slabel(element)}」走所属流程主路径，请直接查看流程图")
    analysis = _scenario_analysis(c, element)
    s = analysis["scenario"]
    steps = analysis["steps"]
    by_source = analysis["by_source"]
    if analysis["issues"]:
        raise DiagramError("流程图质量校验未通过：" + "；".join(analysis["issues"][:8])
                           + "。请让 AI 在场景 branches 中补齐 from_step/to_step/condition 后重试")
    graph_issues = _flow_graph_issues(list(range(1, len(steps) + 1)), by_source)
    if graph_issues:
        raise DiagramError("流程图质量校验未通过：" + graph_issues[0])

    beh_actor = {norm_name(b.get("name", "")): b.get("actor", "")
                 for b in c["behaviors"]}
    beh_display = {norm_name(b.get("name", "")): _slabel(b) for b in c["behaviors"]}

    direction = "LR" if len(steps) >= 7 else "TD"
    lines = [f"flowchart {direction}", f'    S0(["开始：{_text(s.get("goal") or _slabel(s), 30)}"])']
    for i, raw in enumerate(steps, 1):
        text = _text(str(raw), 44)
        nid = f"S{i}"
        # 步骤文本命中已定义行为 → 标注执行主体（图与模型互相印证）
        hit = next((n for n, disp in beh_display.items()
                    if (n and n in norm_name(text))
                    or (disp and disp != "?" and disp in text)), None)
        if hit and beh_actor.get(hit):
            text = f"{text}｜{beh_actor[hit]}"
        if i in by_source:
            lines.append(f'    {nid}{{"{text}"}}')
        else:
            lines.append(f'    {nid}["{text}"]')
    outcome = _text(s.get("expected_outcome") or "结束", 30)
    lines.append(f'    SE(["{outcome}"])')
    lines.append("    S0 --> S1")
    for index in range(1, len(steps) + 1):
        if index in by_source:
            for branch in by_source[index]:
                target_node = f"S{branch['to_step']}" if branch.get("to_step") is not None else "SE"
                lines.append(f'    S{index} -->|"{_text(branch["condition"], 28)}"| {target_node}')
        else:
            target_node = f"S{index + 1}" if index < len(steps) else "SE"
            lines.append(f"    S{index} --> {target_node}")
    return "\n".join(lines), _slabel(s), analysis["warnings"]


def _process_flow_mermaid(c: dict, analysis: dict) -> tuple[str, str, list[str]]:
    """流程结构化步骤 → flowchart；行为/主体标注用显式 step.behavior/step.actor。"""
    process = analysis["process"]
    steps = analysis["steps"]
    by_source = analysis["by_source"]
    if analysis["issues"]:
        raise DiagramError("流程图质量校验未通过：" + "；".join(analysis["issues"][:8])
                           + "。请让 AI 在流程 steps/branches 中补齐后重试")

    beh_display = {norm_name(str(value)): _slabel(b)
                   for b in c["behaviors"]
                   for value in (b.get("name"), b.get("display_name"), b.get("id")) if value}

    direction = "LR" if len(steps) >= 7 else "TD"
    lines = [f"flowchart {direction}",
             f'    S0(["开始：{_text(process.get("goal") or _slabel(process), 30)}"])']
    for i, step in enumerate(steps, 1):
        text = _text(str(step.get("name") or ""), 44)
        nid = f"S{i}"
        # 标注来自显式绑定字段，不做步骤文本启发式：优先执行主体，缺省时用行为显示名
        note = str(step.get("actor") or "").strip()
        if not note and str(step.get("behavior") or "").strip():
            note = beh_display.get(norm_name(str(step["behavior"])), str(step["behavior"]))
        if note:
            text = f"{text}｜{note}"
        if i in by_source:
            lines.append(f'    {nid}{{"{text}"}}')
        else:
            lines.append(f'    {nid}["{text}"]')
    outcome = _text(process.get("expected_outcome") or "结束", 30)
    lines.append(f'    SE(["{outcome}"])')
    lines.append("    S0 --> S1")
    for index in range(1, len(steps) + 1):
        if index in by_source:
            for branch in by_source[index]:
                target_node = f"S{branch['to_step']}" if branch.get("to_step") is not None else "SE"
                lines.append(f'    S{index} -->|"{_text(branch["condition"], 28)}"| {target_node}')
        else:
            target_node = f"S{index + 1}" if index < len(steps) else "SE"
            lines.append(f"    S{index} --> {target_node}")
    return "\n".join(lines), _slabel(process), analysis["warnings"]


# ---------------------------------------------------------------- 时序图


def sequence_mermaid(canvas, target: str | None = None) -> tuple[str, str]:
    """场景 behaviors / 流程步骤绑定行为的顺序 → mermaid sequenceDiagram（主体 →> 对象 : 行为）。"""
    c = _ensure_canvas(canvas)
    element, kind = _pick_flow_target(c, target)
    if kind == "process":
        analysis = process_model_analysis(c, element.get("id") or element.get("name"))
        label = analysis["label"]
        if analysis["issues"]:
            raise DiagramError(
                f"时序图质量校验未通过：流程「{label}」"
                + "；".join(analysis["issues"][:8])
                + "。请让 AI 补齐流程目标、步骤绑定与显式分支后重试"
            )
        # 行为顺序按 step.seq 抽取非空 behavior（步骤已在分析中按 seq 排序且引用可解析）
        behavior_lookup = {
            norm_name(str(value)): behavior
            for behavior in c["behaviors"]
            for value in (behavior.get("name"), behavior.get("display_name"), behavior.get("id"))
            if value
        }
        picked = [
            behavior_lookup[norm_name(str(step["behavior"]))]
            for step in analysis["steps"]
            if str(step.get("behavior") or "").strip()
        ]
        if not picked:
            raise DiagramError(f"流程「{label}」的步骤还没有绑定行为（behavior）——"
                               "先把步骤涉及的行为绑进流程模型，时序图才能反映协作顺序")
        return _sequence_render(c, picked), label

    if str(element.get("process_ref") or "").strip() and not (element.get("steps") or []):
        raise DiagramError(f"场景「{_slabel(element)}」走所属流程主路径，请直接查看流程图")
    analysis = _scenario_analysis(c, element)
    s = analysis["scenario"]
    if analysis["issues"]:
        raise DiagramError(
            f"时序图质量校验未通过：场景「{_slabel(s)}」"
            + "；".join(analysis["issues"][:8])
            + "。请让 AI 补齐场景目标、引用、步骤、结果与显式分支后重试"
        )
    beh_by_name = {norm_name(b.get("name", "")): b for b in c["behaviors"]}
    for b in c["behaviors"]:
        if b.get("display_name"):
            beh_by_name.setdefault(norm_name(b["display_name"]), b)

    raw_names = [str(value) for value in (s.get("behaviors") or [])]
    names = [norm_name(value) for value in raw_names]
    missing = [raw for raw, normalized in zip(raw_names, names) if normalized not in beh_by_name]
    if missing:
        raise DiagramError(f"时序图质量校验未通过：场景「{_slabel(s)}」引用了未定义行为："
                           + "、".join(missing[:8]) + "。请让 AI 修复场景 behaviors 后重试")
    picked = [beh_by_name[n] for n in names]
    if not picked:
        raise DiagramError(f"场景「{_slabel(s)}」还没有关联行为（behaviors）——"
                           "先把场景涉及的行为补进场景模型，时序图才能反映协作顺序")
    return _sequence_render(c, picked), _slabel(s)


def _sequence_render(c: dict, picked: list[dict]) -> str:
    """已解析行为列表 → sequenceDiagram 文本（场景/流程共用的渲染尾段）。"""
    incomplete = [_slabel(behavior) for behavior in picked
                  if not behavior.get("actor") or not behavior.get("object")]
    if incomplete:
        raise DiagramError("时序图质量校验未通过：以下行为缺少 actor 或 object："
                           + "、".join(incomplete[:8]) + "。请让 AI 补齐后重试")
    object_names = {norm_name(value) for obj in c["objects"]
                    for value in (obj.get("name", ""), obj.get("display_name", "")) if value}
    actor_names = {norm_name(value) for actor in c["actors"]
                   for value in (actor.get("name", ""), actor.get("display_name", "")) if value}
    unresolved = [_slabel(behavior) for behavior in picked
                  if norm_name(behavior.get("actor", "")) not in actor_names | object_names
                  or norm_name(behavior.get("object", "")) not in actor_names | object_names]
    if unresolved:
        raise DiagramError("时序图质量校验未通过：以下行为的 actor/object 引用未定义："
                           + "、".join(unresolved[:8]) + "。请让 AI 修复引用后重试")

    participants: list[tuple[str, str]] = []   # (ident, display)
    seen: set[str] = set()

    def part(name: str, fallback: str) -> str:
        ident = _ident(name, fallback)
        if ident not in seen:
            seen.add(ident)
            participants.append((ident, _text(name or fallback, 20)))
        return ident

    body: list[str] = []
    for b in picked:
        actor = part(b["actor"], "Actor")
        obj = part(b["object"], "Object")
        label = _text(_slabel(b), 40)
        trig = _text(b.get("trigger") or "", 30)
        body.append(f"    {actor}->>+{obj}: {label}" + (f"（{trig}）" if trig else ""))
        outcome = _text(b.get("outcome") or "", 40)
        body.append(f"    {obj}-->>-{actor}: {outcome or '完成'}")

    lines = ["sequenceDiagram", "    autonumber"]
    lines += [f"    participant {i} as {d}" for i, d in participants]
    lines += body
    return "\n".join(lines)


# ---------------------------------------------------------------- 状态图

# 源端禁止吞并叙述（逗号/分号/引号/括号都是"从…变为…"短语的边界），
# 否则「从无到有，初始创建后…」会把整句叙述当成源状态（生产实发）。
_FROM_TRANSITION_RE = re.compile(
    r"从\s*[「『\"']?([^，。；;\n「」『』()（）]{1,40}?)[」』\"']?\s*"
    r"(?:变更为|变为|改为|置为|转为|流转到)\s*"
    r"[「『\"']?([^，。；;\n]{1,40}?)[」』\"']?(?=，|。|；|;|\n|$)")
_STATUS_VALUE_RE = re.compile(
    r"(?:\bstatus\b|\bstate\b|状态|阶段)\s*(?:=|为|保持(?:为)?)\s*"
    r"[「『\"']?([^，。；;、\s时且后前]{1,40})[」』\"']?", re.IGNORECASE)


def _status_attr(obj: dict) -> dict | None:
    """只把显式命名为状态/阶段的枚举当生命周期，不能拿任意枚举冒充状态。"""
    for attr in obj.get("attributes") or []:
        if not attr.get("enum"):
            continue
        label = f"{attr.get('name', '')}{attr.get('display_name', '')}".lower()
        if any(k in label for k in ("状态", "status", "state", "阶段", "stage")):
            return attr
    return None


def _pick_state_object(canvas: dict, target: str | None) -> tuple[dict, dict]:
    objects = canvas["objects"]
    if not objects:
        raise DiagramError("画布还没有对象模型")
    if target:
        normalized = norm_name(target)
        # readiness 使用稳定 id 精确定位，避免历史脏数据中的重名/显示名歧义
        # 让状态分析误选另一个对象。对外仍兼容 name/display_name。
        obj = next(
            (item for item in objects if str(item.get("id") or "") == str(target)),
            None,
        )
        if obj is None:
            matches = [
                item for item in objects
                if norm_name(item.get("name", "")) == normalized
                or norm_name(item.get("display_name", "")) == normalized
            ]
            if len(matches) > 1:
                raise DiagramError(
                    f"对象「{target}」匹配到多个候选，请使用画布元素 id 精确指定")
            obj = matches[0] if matches else None
        if obj is None:
            raise DiagramError(f"对象「{target}」不存在")
        attr = _status_attr(obj)
        if attr is None:
            raise DiagramError(f"对象「{_slabel(obj)}」没有状态/阶段枚举属性 ——"
                               "普通枚举不能自动当作生命周期，请先确认状态字段与全部状态值")
        return obj, attr
    obj = next((item for item in objects if _status_attr(item)), None)
    if obj is None:
        raise DiagramError("没有任何对象拥有状态/阶段枚举属性 —— 先确认哪个对象有生命周期状态")
    return obj, _status_attr(obj)  # type: ignore[return-value]


def _clean_state_value(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^(?:[\w一-鿿]+\.)?(?:status|state|状态|阶段)\s*[:=]?\s*",
                  "", text, flags=re.IGNORECASE)
    return text.strip(" \t\r\n「」『』\"'`()[]{}：:")


def _resolve_state_value(raw: str, by_norm: dict) -> str | None:
    """把捕获值解析到状态枚举；容忍目标状态后紧跟的括号/引号补充说明。

    生产文本常见「从『修复中』变为『已闭环』（验证结果为…）」：正则目的端
    会连同括号说明一起捕获成『已闭环』（验证结果为…）。逐个闭合符截断、
    取第一个命中枚举的前缀 —— 既不丢真实迁移，也不把括号说明误报为未知
    状态（那会让 lifecycles 质量门出现模型无法清掉的孤立状态误报）。
    """
    text = str(raw or "")
    direct = by_norm.get(norm_name(_clean_state_value(text)))
    if direct:
        return direct
    for idx, ch in enumerate(text):
        if ch in "」』\"')）":
            candidate = by_norm.get(
                norm_name(_clean_state_value(text[:idx + 1])))
            if candidate:
                return candidate
    return None


def state_model_analysis(canvas, target: str | None = None) -> dict:
    """分析生命周期的一致性；结果同时供质量门和状态图生成器使用。"""
    c = _ensure_canvas(canvas)
    obj, attr = _pick_state_object(c, target)
    states = [str(value).strip() for value in (attr.get("enum") or []) if str(value).strip()]
    by_norm = {norm_name(value): value for value in states}
    object_names = {norm_name(obj.get("name", "")), norm_name(obj.get("display_name", ""))}
    object_names.discard("")

    edges: set[tuple[str, str, str]] = set()
    unknown_refs: set[tuple[str, str]] = set()
    for behavior in c["behaviors"]:
        if norm_name(behavior.get("object", "")) not in object_names:
            continue
        label = _slabel(behavior)
        blob = "；".join(str(behavior.get(key) or "")
                        for key in ("outcome", "description", "trigger"))
        for match in _FROM_TRANSITION_RE.finditer(blob):
            raw_src = _clean_state_value(match.group(1))
            raw_dst = _clean_state_value(match.group(2))
            src = _resolve_state_value(raw_src, by_norm)
            dst = _resolve_state_value(raw_dst, by_norm)
            prefix = blob[max(0, match.start() - 40):match.start()]
            explicit_status = bool(re.search(
                r"(?:\bstatus\b|\bstate\b|状态|阶段)\s*$", prefix, re.IGNORECASE))
            if src and dst:
                if src != dst:
                    edges.add((src, dst, label))
            elif explicit_status or bool(src) != bool(dst):
                # “从false变为true”可能描述的是任意布尔属性；只有显式写了
                # status/state，或一端已命中状态枚举时，才把未知值判为状态错误。
                for raw, resolved in ((raw_src, src), (raw_dst, dst)):
                    if raw and not resolved:
                        unknown_refs.add((label, raw))
        # 即使没有形成迁移，status=值 / 状态保持值 也必须属于枚举；否则平台
        # 会出现“画布已就绪但图上丢节点/丢边”的双重口径。
        for match in _STATUS_VALUE_RE.finditer(blob):
            raw = _clean_state_value(match.group(1))
            if raw and norm_name(raw) not in by_norm:
                unknown_refs.add((label, raw))

    issues: list[str] = []
    if len(states) < 2:
        issues.append(f"对象「{_slabel(obj)}」的状态枚举少于 2 个值")
    for behavior, value in sorted(unknown_refs):
        issues.append(f"行为「{behavior}」引用状态「{value}」，但它不在枚举 {states}")
    if states and not edges:
        issues.append(f"对象「{_slabel(obj)}」没有可验证的状态迁移；行为 outcome 需写明与枚举完全一致的「从X变为Y」")

    connected = {value for src, dst, _ in edges for value in (src, dst)}
    isolated = [value for value in states if value not in connected]
    if edges and isolated:
        issues.append("存在无任何迁入/迁出的孤立状态：" + "、".join(isolated[:8]))
        carriers = _cross_object_transition_carriers(c, obj, object_names, by_norm)
        if carriers:
            issues.append(
                "以下行为描述了本对象的状态迁移但未挂载在本对象上（"
                + "；".join(carriers[:3])
                + f"）—— 请把这些行为的 object 改为「{_slabel(obj)}」，"
                "或把对应状态迁移拆到本对象的独立行为上（生产事故：迁移承载行为"
                "挂在别的对象上，本对象生命周期永远缺少该迁入边，报错却只说"
                "「孤立状态」让人误判为解析问题）")

    sources = {src for src, _, _ in edges}
    destinations = {dst for _, dst, _ in edges}
    initial_candidates = sorted(sources - destinations, key=norm_name)
    warnings: list[str] = []
    initial = initial_candidates[0] if len(initial_candidates) == 1 else None
    if edges and initial is None:
        warnings.append("无法从已确认迁移唯一确定初始状态，图中不绘制虚假的起点")
    return {
        "object": obj, "attribute": attr, "states": states,
        "edges": sorted(edges, key=lambda edge: (norm_name(edge[0]), norm_name(edge[1]), edge[2])),
        "initial": initial, "isolated": isolated, "issues": issues, "warnings": warnings,
    }


def _cross_object_transition_carriers(c: dict, obj: dict, object_names: set[str],
                                      by_norm: dict) -> list[str]:
    """找出叙述了本对象状态迁移、但 object 挂在别的对象上的行为。

    状态分析只扫描挂载在本对象上的行为；迁移承载行为挂错对象会让目标状态
    表现为「孤立」，且原报错不解释原因（生产：verify_repair 挂在
    repair_verification 上，vuln_alert 的「已闭环」因此永远孤立）。
    判定须同时满足：① 迁移端点/状态值命中本对象枚举；② 文本提及本对象
    名称 —— 两个条件叠加避免不同对象共享同名状态时的误报。
    """
    aliases = {
        str(obj.get(key) or "").strip()
        for key in ("name", "display_name")
    }
    aliases.discard("")
    carriers: list[str] = []
    for behavior in c["behaviors"]:
        if norm_name(behavior.get("object", "")) in object_names:
            continue
        blob = "；".join(str(behavior.get(key) or "")
                        for key in ("outcome", "description", "trigger"))
        if not any(alias in blob for alias in aliases):
            continue
        hit = False
        for match in _FROM_TRANSITION_RE.finditer(blob):
            src = _resolve_state_value(_clean_state_value(match.group(1)), by_norm)
            dst = _resolve_state_value(_clean_state_value(match.group(2)), by_norm)
            if src or dst:
                hit = True
                break
        if not hit:
            for match in _STATUS_VALUE_RE.finditer(blob):
                raw = _clean_state_value(match.group(1))
                if raw and norm_name(raw) in by_norm:
                    hit = True
                    break
        if hit:
            carriers.append(
                f"行为「{_slabel(behavior)}」({behavior.get('name') or '?'})"
                f"当前 object={behavior.get('object') or '?'}")
    return carriers


def state_mermaid(canvas, target: str | None = None) -> tuple[str, str, list[str]]:
    """对象的枚举状态属性 → mermaid stateDiagram；从行为结果文本识别显式迁移。"""
    analysis = state_model_analysis(canvas, target)
    obj = analysis["object"]
    attr = analysis["attribute"]
    states = analysis["states"]
    if analysis["issues"]:
        detail = "；".join(analysis["issues"][:6])
        raise DiagramError(
            "状态图质量校验未通过：" + detail
            + "。请先修复画布后重试；校验错误会回填给 AI 继续补齐。"
            "规范写法：行为 outcome 中每次状态迁移单独成句，形如"
            "「状态从『已确认』变为『规避中』」，状态名与枚举完全一致；"
            "括号补充说明必须另起一句（用分号隔开），不得紧跟在目标状态之后；"
            "多源迁移（从A或B变为C）拆成「从A变为C」「从B变为C」两句。")
    ids = {v: f"st{i}" for i, v in enumerate(states)}

    lines = ["stateDiagram-v2", "    direction LR"]
    for v in states:
        lines.append(f'    {ids[v]} : {_text(v, 20)}')
    if analysis["initial"]:
        lines.append(f"    [*] --> {ids[analysis['initial']]}")
    for src, dst, label in analysis["edges"]:
        lines.append(f"    {ids[src]} --> {ids[dst]} : {_text(label, 20)}")
    return ("\n".join(lines),
            f"{_slabel(obj)} · {attr.get('display_name') or attr.get('name')}",
            analysis["warnings"])


# ---------------------------------------------------------------- 统一入口


def build_diagram(canvas, kind: str, target: str | None = None) -> dict:
    """生成指定图表：{kind, title, target?, mermaid}。条件不足抛 DiagramError。"""
    k = (kind or "").strip().lower()
    if k not in DIAGRAM_KINDS:
        raise DiagramError(f"不支持的图表类型「{kind}」，可选: {', '.join(DIAGRAM_KINDS)}")
    if k == "er":
        c = _ensure_canvas(canvas)
        density = len(c["objects"]) + sum(len(o.get("relations") or []) for o in c["objects"])
        return {"kind": "er", "title": DIAGRAM_KINDS["er"], "mermaid": er_mermaid(canvas),
                "warnings": [], "layout": _layout_budget("er", density)}
    if k == "flow":
        mermaid, scen, warnings = flow_mermaid(canvas, target)
        element, _ = _pick_flow_target(_ensure_canvas(canvas), target)
        steps = len(element.get("steps") or [])
        return {"kind": "flow", "title": f"{DIAGRAM_KINDS['flow']} · {scen}",
                "target": scen, "mermaid": mermaid, "warnings": warnings,
                "layout": _layout_budget("flow", steps)}
    if k == "sequence":
        mermaid, scen = sequence_mermaid(canvas, target)
        element, element_kind = _pick_flow_target(_ensure_canvas(canvas), target)
        if element_kind == "process":
            behaviors = sum(1 for step in (element.get("steps") or [])
                            if str(step.get("behavior") or "").strip())
        else:
            behaviors = len(element.get("behaviors") or [])
        return {"kind": "sequence", "title": f"{DIAGRAM_KINDS['sequence']} · {scen}",
                "target": scen, "mermaid": mermaid, "warnings": [],
                "layout": _layout_budget("sequence", behaviors)}
    mermaid, label, warnings = state_mermaid(canvas, target)
    return {"kind": "state", "title": f"{DIAGRAM_KINDS['state']} · {label}",
            "target": label, "mermaid": mermaid, "warnings": warnings,
            "layout": _layout_budget("state", mermaid.count(" : "))}


def _layout_budget(kind: str, density: int) -> dict:
    """Flint 风格的目标尺寸 + 有界增长元数据，供前端缩略/预览双态使用。"""
    base = {"width": 640, "height": 360}
    if kind in ("er", "sequence"):
        base = {"width": 720, "height": 400}
    stretch = min(2.0, 1.0 + max(0, density - 5) * 0.08)
    return {"baseSize": base,
            "canvasSize": {"width": min(1440, round(base["width"] * stretch)),
                           "height": min(900, round(base["height"] * stretch))},
            "density": density}
