"""质量门（readiness gates）— 画布 → 本体草稿的准入检查

十道门与图谱编辑器/转化管线的真实需求一一对应：编辑器里一个"好"的本体
对象需要主键、带类型的属性、可解析且带基数的关系、绑定了对象的动作、
可形式化（定量）的规则条件、来源可追溯的哨兵；流程编排门再把流程模型的
步骤落位、分支可达与度量定量显性拦下。凡是转化时只能靠回退兜底
（自动补 id 主键、基数默认 one-to-many、规则待形式化却连数字都没有）的，
都在这里显性拦下，逼着对话把口径问清，而不是把含糊留给编辑器。

每道门产出 blocking / advisory 两级问题项：
  - blocking：不解决就不该生成草稿（可 force 越权，留痕）
  - advisory：不拦路，但列出来供追问与人审

画布零流程时 processes 门无条件通过（vacuous pass）—— 流程是 P0 新模型，
所有 blocking 只针对已存在流程的畸形，存量会话与旧画布不受新门影响。

evaluate() 纯函数、确定性，同一画布永远得到同一份报告 —— 与转化管线同哲学。
"""
from __future__ import annotations

from typing import Any

from app.exploration import diagram as D
from app.exploration.canvas import _ensure_canvas, norm_name
from app.exploration.converter import CARDINALITIES
from app.exploration.questions import (
    KIND_ADVISORY,
    KIND_BLOCKING,
    blocking_liabilities,
    is_quantified,
    open_questions,
    resolved_question_issues,
    vague_terms_in,
)

# 门定义顺序即阶段推进顺序（第一道未过的门 = 当前建议聚焦的阶段）
_GATE_STAGES = [
    ("scope", "业务边界", "阶段0 · 定边界：先有至少一个流程或场景和一个主体"),
    ("objects", "对象齐备", "阶段1 · 对象与主键：属性、类型、业务主键"),
    ("relations", "关系闭合", "阶段2 · 关系与基数：目标可解析、基数已确认"),
    ("behaviors", "行为落位", "阶段3 · 行为：谁(主体)对什么(对象)做"),
    ("lifecycles", "状态闭环", "阶段3 · 生命周期：枚举与迁移使用同一口径且没有孤立状态"),
    ("rules", "规则定量", "阶段4 · 规则定量：阈值/枚举/边界给到数字"),
    ("events", "事件可追溯", "阶段4 · 事件：来源指向行为/external/time"),
    ("processes", "流程编排", "阶段5 · 流程编排：步骤落位、分支可达、度量定量"),
    ("questions", "疑问清零", "阶段6 · 清账：回答账本里剩余的堵门问题"),
    ("coverage", "场景验收", "阶段7 · 验收：场景引用的对象/行为都已定义"),
]
_STAGE_READY = "已就绪 · 全部质量门通过，可生成需求文档与本体草稿"


def _label(x: dict) -> str:
    return str(x.get("display_name") or x.get("name") or "?")


def evaluate(canvas: Any) -> dict:
    """画布 → 质量门报告：{ready, stage, gates[], blockingCount, advisoryCount, openQuestions}"""
    c = _ensure_canvas(canvas)
    objects = c["objects"]
    actors = c["actors"]
    behaviors = c["behaviors"]
    events = c["events"]
    rules = c["rules"]
    scenarios = c["scenarios"]
    processes = c["processes"]

    obj_names = {norm_name(o.get("name", "")) for o in objects} \
        | {norm_name(o.get("display_name", "")) for o in objects if o.get("display_name")}
    actor_names = {norm_name(a.get("name", "")) for a in actors} \
        | {norm_name(a.get("display_name", "")) for a in actors if a.get("display_name")}
    beh_names = {norm_name(b.get("name", "")) for b in behaviors} \
        | {norm_name(b.get("display_name", "")) for b in behaviors if b.get("display_name")}
    # 可解析实体口径与转化管线一致：非 system 主体同样转成 ObjectType，
    # 关系/行为/规则指向它们是合法的（system 主体不建对象，不算）
    entity_names = obj_names | {
        n for a in actors if (a.get("kind") or "role") != "system"
        for n in (norm_name(a.get("name", "")), norm_name(a.get("display_name", "")))
        if n
    }

    gates: list[dict] = []

    def gate(gid: str, blocking: list[str], advisory: list[str]) -> None:
        label = next(lbl for g, lbl, _ in _GATE_STAGES if g == gid)
        gates.append({"id": gid, "label": label, "passed": not blocking,
                      "blockingItems": blocking, "advisoryItems": advisory})

    # -- scope：边界 —— 流程/场景圈定业务边界，主体圈定参与方
    blk, adv = [], []
    for collection, label in (
        (objects, "对象"), (actors, "主体"), (behaviors, "行为"),
        (events, "事件"), (rules, "规则"), (scenarios, "场景"),
    ):
        by_name: dict[str, list[str]] = {}
        for item in collection:
            normalized = norm_name(str(item.get("name") or ""))
            if normalized:
                by_name.setdefault(normalized, []).append(_label(item))
        for labels in by_name.values():
            if len(labels) > 1:
                blk.append(
                    f"{label}模型存在重复稳定 name：{'、'.join(labels[:4])}；"
                    "请先合并或重命名，避免引用歧义")
    if not scenarios and not processes:
        blk.append("还没有任何流程或场景 —— 先让用户讲一个端到端流程或一个典型情境")
    if not actors:
        blk.append("还没有任何主体 —— 谁在参与这些业务？")
    if not objects:
        blk.append("还没有任何业务对象")
    gate("scope", blk, adv)

    # -- objects：编辑器需要 主键 + 带类型属性；person/org 主体同样是数据实体
    blk, adv = [], []
    for o in objects:
        name = _label(o)
        attrs = o.get("attributes") or []
        if not attrs:
            blk.append(f"对象「{name}」还没有属性")
            continue
        attr_names = {norm_name(a.get("name", "")) for a in attrs}
        key = o.get("key_attribute")
        if not key:
            blk.append(f"对象「{name}」未指定业务主键属性（落地后编辑器需要主键标识实例）")
        elif norm_name(key) not in attr_names:
            blk.append(f"对象「{name}」的主键「{key}」不在属性列表中")
        untyped = [a.get("name", "?") for a in attrs if not (a.get("type_hint") or "").strip()]
        if untyped:
            blk.append(f"对象「{name}」的属性 {', '.join(untyped[:6])} 缺类型提示（必须与图谱编辑器类型契约对齐）")
        for a in attrs:
            hint = (a.get("type_hint") or "")
            if ("枚举" in hint or "enum" in hint.lower()) and not a.get("enum"):
                blk.append(f"对象「{name}」的属性「{a.get('name')}」标记为枚举但未给出枚举值清单")
    for a in actors:
        if (a.get("kind") or "role") in ("person", "org"):
            name = _label(a)
            if not (a.get("attributes") or []):
                blk.append(f"主体「{name}」是 {a.get('kind')} 类数据实体，还没有识别/档案属性")
            elif not a.get("key_attribute"):
                blk.append(f"主体「{name}」未指定业务主键")
            untyped = [p.get("name", "?") for p in (a.get("attributes") or [])
                       if not (p.get("type_hint") or "").strip()]
            if untyped:
                blk.append(f"主体「{name}」的属性 {', '.join(untyped[:6])} 缺类型提示")
    gate("objects", blk, adv)

    # -- relations：链接端点必须可解析、基数必须已确认（基数是典型 B 类口径）
    blk, adv = [], []
    for o in objects:
        for r in o.get("relations") or []:
            rel = f"{_label(o)} → {r.get('target')}"
            if norm_name(r.get("target", "")) not in entity_names:
                blk.append(f"关系「{rel}」指向未定义的对象/主体")
            card = (r.get("cardinality") or "").strip().lower()
            if not card:
                blk.append(f"关系「{rel}」未确认基数（one-to-one/one-to-many/many-to-one/many-to-many）")
            elif card not in CARDINALITIES:
                blk.append(f"关系「{rel}」基数「{card}」不合法")
    gate("relations", blk, adv)

    # -- behaviors：动作要能绑定到对象、归属到主体
    blk, adv = [], []
    for b in behaviors:
        name = _label(b)
        if not b.get("actor"):
            blk.append(f"行为「{name}」缺少执行主体")
        elif norm_name(b.get("actor", "")) not in actor_names | obj_names:
            blk.append(f"行为「{name}」的执行主体「{b.get('actor')}」未在主体模型中定义")
        if not b.get("object"):
            blk.append(f"行为「{name}」缺少作用对象")
        elif norm_name(b.get("object", "")) not in entity_names:
            blk.append(f"行为「{name}」作用的对象「{b.get('object')}」未在对象/主体模型中定义")
        if not (b.get("trigger") or "").strip():
            adv.append(f"行为「{name}」未说明触发条件")
        if not (b.get("outcome") or "").strip():
            adv.append(f"行为「{name}」未说明结果")
        vague = [w for cst in (b.get("constraints") or [])
                 for w in (vague_terms_in(cst) if not is_quantified(cst) else [])]
        if vague:
            blk.append(f"行为「{name}」的约束含未定量表述（{'、'.join(sorted(set(vague))[:3])}）—— 请把口径问成数字/枚举")
    gate("behaviors", blk, adv)

    # -- lifecycles：状态图必须是可验证投影；枚举/行为用词不一致、无迁移、
    # 孤立状态都堵门，避免把“能画出来”误当成“模型完整”。
    blk, adv = [], []
    for obj in objects:
        if D._status_attr(obj) is None:
            continue
        try:
            analysis = D.state_model_analysis(
                c, obj.get("id") or obj.get("name"))
        except D.DiagramError as error:
            blk.append(f"对象「{_label(obj)}」：生命周期无法安全解析（{error}）")
            continue
        blk.extend(f"对象「{_label(obj)}」：{issue}" for issue in analysis["issues"])
        adv.extend(f"对象「{_label(obj)}」：{warning}" for warning in analysis["warnings"])
    gate("lifecycles", blk, adv)

    # -- rules：规则将转成校验/函数/哨兵草稿，表述必须可形式化（定量）
    blk, adv = [], []
    for r in rules:
        name = _label(r)
        stmt = (r.get("statement") or r.get("description") or "").strip()
        if not stmt:
            blk.append(f"规则「{name}」没有表述（statement）")
        elif not is_quantified(stmt):
            hits = "、".join(vague_terms_in(stmt)[:3])
            blk.append(f"规则「{name}」表述未定量（{hits}）—— 阈值/时限/枚举要有具体数字或清单")
        tgt = norm_name(r.get("applies_to") or "")
        if not tgt:
            blk.append(f"规则「{name}」未指定作用对象/行为（applies_to）")
        elif tgt not in entity_names | beh_names:
            blk.append(f"规则「{name}」作用的「{r.get('applies_to')}」未在对象/主体/行为模型中定义")
    gate("rules", blk, adv)

    # -- events：事件转哨兵草稿，来源决定触发方式（变化驱动/定时扫描）
    blk, adv = [], []
    for e in events:
        name = _label(e)
        src = norm_name(e.get("source") or "")
        if not src:
            blk.append(f"事件「{name}」缺少来源（某行为名 / external / time）")
        elif src not in beh_names and src not in ("external", "time"):
            blk.append(f"事件「{name}」的来源「{e.get('source')}」不是已定义的行为，也不是 external/time")
        if not (e.get("consequences") or []):
            adv.append(f"事件「{name}」未说明后果 —— 发生之后业务上要做什么？")
    gate("events", blk, adv)

    # -- processes：流程编排 —— 画布零流程时本门无条件通过（vacuous pass），
    # 所有 blocking 只针对已存在流程的畸形；分支/可达性复用 diagram 共享校验。
    blk, adv = [], []
    for p in processes:
        pname = _label(p)
        steps = [step for step in (p.get("steps") or []) if isinstance(step, dict)]
        if steps and len(steps) < 2:
            blk.append(f"流程「{pname}」的步骤少于 2 步 —— 端到端流程至少要有起点与终点")
        try:
            analysis = D.process_model_analysis(c, p.get("id") or p.get("name"))
        except D.DiagramError as error:
            blk.append(f"流程「{pname}」无法定位：{error}")
            continue
        blk.extend(f"流程「{pname}」：{issue}" for issue in analysis["issues"])
        for metric in p.get("metrics") or []:
            mname = metric.get("name") or "?"
            formula = str(metric.get("formula") or "").strip()
            if not formula:
                blk.append(f"流程「{pname}」的度量「{mname}」缺少计算口径（formula）")
            elif not is_quantified(formula):
                hits = "、".join(vague_terms_in(formula)[:3]) or "无数量信号"
                blk.append(f"流程「{pname}」的度量「{mname}」口径未定量（{hits}）—— 要有具体数字或枚举")
            for ref in metric.get("source_objects") or []:
                if norm_name(ref) not in entity_names:
                    blk.append(f"流程「{pname}」的度量「{mname}」的来源对象「{ref}」未在对象/主体模型中定义")
        branches = p.get("branches") or []
        if not any(branch.get("kind") == "exception" for branch in branches):
            adv.append(f"流程「{pname}」没有异常分支（exception）—— 出错/驳回时流程往哪走？")
        if not (p.get("metrics") or []):
            adv.append(f"流程「{pname}」还没有产出度量（metrics）—— 用什么数字证明流程达成了目标？")
        if steps and not any(str(step.get("behavior") or "").strip() for step in steps):
            adv.append(f"流程「{pname}」的全部步骤都未绑定行为 —— 步骤与行为模型脱节")
    gate("processes", blk, adv)

    # -- questions：账本清零 —— dismissed 只是暂缓，已销账答案还须与 target 画布一致
    opens_b = open_questions(canvas, KIND_BLOCKING)
    opens_a = open_questions(canvas, KIND_ADVISORY)
    liabilities = blocking_liabilities(canvas)
    blk = []
    for q in liabilities:
        if q.get("status") == "dismissed":
            reason = str(q.get("resolution") or "未说明原因")
            blk.append(f"堵门问题被搁置但尚未解决：{q.get('question')}（{reason}）")
        else:
            blk.append(f"开放堵门问题：{q.get('question')}")
    resolved_blk, resolved_adv = resolved_question_issues(canvas)
    blk.extend(resolved_blk)
    adv = [f"AI 建议待确认：{q.get('question')}" for q in opens_a]
    adv.extend(resolved_adv)
    gate("questions", blk, adv)

    # -- coverage：场景是端到端验收器，不接受只有名字的空壳场景；
    # 挂接流程的场景从简，覆盖口径含流程引用（对象/行为被任一场景或流程引用即覆盖）。
    blk, adv = [], []
    covered_obj: set[str] = set()
    covered_beh: set[str] = set()
    object_identity = {
        norm_name(str(value)): norm_name(str(obj.get("name") or ""))
        for obj in objects
        for value in (obj.get("name"), obj.get("display_name"), obj.get("id"))
        if value
    }
    behavior_identity = {
        norm_name(str(value)): norm_name(str(behavior.get("name") or ""))
        for behavior in behaviors
        for value in (behavior.get("name"), behavior.get("display_name"), behavior.get("id"))
        if value
    }
    process_identity = {
        norm_name(str(value)): norm_name(str(process.get("name") or ""))
        for process in processes
        for value in (process.get("name"), process.get("display_name"), process.get("id"))
        if value
    }
    for p in processes:
        for x in p.get("objects") or []:
            if norm_name(x) in entity_names:
                canonical = object_identity.get(norm_name(x))
                if canonical:
                    covered_obj.add(canonical)
        for step in p.get("steps") or []:
            behavior = str(step.get("behavior") or "").strip()
            if behavior and norm_name(behavior) in beh_names:
                canonical = behavior_identity.get(norm_name(behavior))
                if canonical:
                    covered_beh.add(canonical)
    for s in scenarios:
        sname = _label(s)
        for x in s.get("objects") or []:
            if norm_name(x) in entity_names:
                canonical = object_identity.get(norm_name(x))
                if canonical:
                    covered_obj.add(canonical)
        for x in s.get("behaviors") or []:
            if norm_name(x) in beh_names:
                canonical = behavior_identity.get(norm_name(x))
                if canonical:
                    covered_beh.add(canonical)
        process_ref = str(s.get("process_ref") or "").strip()
        if process_ref:
            if norm_name(process_ref) not in process_identity:
                blk.append(f"场景「{sname}」挂接的流程「{process_ref}」不存在")
                continue
            # 挂接场景从简：只强制 goal 非空；steps 为空 = 走流程主路径，
            # 跳过场景级 flow 校验；objects/behaviors 若填必须可解析；
            # steps 非空则落入下方与未挂接场景一致的现有校验。
            if not str(s.get("goal") or "").strip():
                blk.append(f"场景「{sname}」缺少业务目标（goal）")
            if not (s.get("steps") or []):
                unresolved_objects = [str(x) for x in (s.get("objects") or [])
                                      if norm_name(str(x)) not in entity_names]
                unresolved_behaviors = [str(x) for x in (s.get("behaviors") or [])
                                        if norm_name(str(x)) not in beh_names]
                if unresolved_objects:
                    blk.append(f"场景「{sname}」引用未定义对象/实体主体："
                               + "、".join(unresolved_objects[:8]))
                if unresolved_behaviors:
                    blk.append(f"场景「{sname}」引用未定义行为："
                               + "、".join(unresolved_behaviors[:8]))
                continue
        try:
            analysis = D.scenario_model_analysis(c, s.get("name") or s.get("display_name"))
        except D.DiagramError as error:
            blk.append(f"场景「{sname}」无法定位：{error}")
            continue
        blk.extend(f"场景「{sname}」：{issue}" for issue in analysis["issues"])
        # 分支字段合法不等于流程可完成；完整场景继续做可达/可结束检查。
        if not analysis["issues"]:
            try:
                D.flow_mermaid(c, s.get("name") or s.get("display_name"))
            except D.DiagramError as error:
                if "流程图质量校验未通过" in str(error):
                    blk.append(f"场景「{sname}」的流程结构不完整：{error}")
    if scenarios or processes:
        un_obj = [_label(o) for o in objects if norm_name(o.get("name", "")) not in covered_obj]
        un_beh = [_label(b) for b in behaviors if norm_name(b.get("name", "")) not in covered_beh]
        if un_obj:
            adv.append(f"对象 {', '.join(un_obj[:6])} 未被任何场景或流程覆盖")
        if un_beh:
            adv.append(f"行为 {', '.join(un_beh[:6])} 未被任何场景或流程覆盖")
    gate("coverage", blk, adv)

    blocking_count = sum(len(g["blockingItems"]) for g in gates)
    advisory_count = sum(len(g["advisoryItems"]) for g in gates)
    ready = blocking_count == 0
    stage = _STAGE_READY if ready else \
        next(st for gid, _, st in _GATE_STAGES
             for g in gates if g["id"] == gid and not g["passed"])
    passed = sum(1 for g in gates if g["passed"])
    return {
        "ready": ready,
        "stage": stage,
        "gatesPassed": passed,
        "gatesTotal": len(gates),
        "blockingCount": blocking_count,
        "advisoryCount": advisory_count,
        "openQuestions": {
            "blocking": len(opens_b),
            "advisory": len(opens_a),
            "dismissedBlocking": sum(1 for q in liabilities if q.get("status") == "dismissed"),
        },
        "gates": gates,
    }


def summary_text(rd: dict, max_items: int = 10) -> str:
    """质量门报告 → 紧凑文本（注入系统提示 / 文档附录）。"""
    lines = [f"{'✅ 已就绪' if rd['ready'] else '⛔ 未就绪'}"
             f"（{rd['gatesPassed']}/{rd['gatesTotal']} 门通过，"
             f"堵门项 {rd['blockingCount']}，建议项 {rd['advisoryCount']}）",
             f"当前阶段：{rd['stage']}"]
    shown = 0
    for g in rd["gates"]:
        for item in g["blockingItems"]:
            if shown >= max_items:
                lines.append(f"- …其余 {rd['blockingCount'] - shown} 项堵门问题略")
                return "\n".join(lines)
            lines.append(f"- [{g['label']}] {item}")
            shown += 1
    return "\n".join(lines)
