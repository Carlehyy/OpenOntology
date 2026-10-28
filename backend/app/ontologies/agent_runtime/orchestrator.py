"""
编排循环 (Orchestrator) — LLM ⇄ 受限工具集的 agent 回合

一次用户提问 = 一个回合（turn）。回合内 LLM 最多走 profile.max_steps 步工具
调用，每步产出一个可流式推送的事件；回合结束把完整轨迹（steps / citations /
proposals / token 用量）持久化为 AgentMessage —— 审计与回放的最小单元。

事件流协议（SSE 每条 data 一个 JSON）：
  {"type": "meta",   "conversationId", "model"}
  {"type": "step",   "tool", "arguments", "summary", "durationMs", "result", "error"?}
  {"type": "answer", "content", "citations", "proposals", "usage", "verification"?}
  {"type": "error",  "message"}
  {"type": "cancelled"}
  {"type": "done"}

step.result 是适合前端展示的工具输出（过大自动截断）；持久化时 result 保存完整
审计结果，displayResult 仅在需要截断展示时保存，兼顾完整导出与历史回放性能。

终答前经 answer_verifier 做确定性结论校验（数值断言必须能对应到工具结果），
失败先给模型一次修正机会，仍不通过则显式标注。全部限额来自 limits 统一注册表。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy.orm import Session

from app.model_configs.selector import select_llm_model_config, llm_call_kwargs
from app.ontologies.agent_runtime import llm_bridge
from app.ontologies.agent_runtime import answer_verifier
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.chat_cancel import chat_cancel_registry
from app.ontologies.agent_runtime.chat_runs import chat_run_registry
from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.models import (
    AgentConversation,
    AgentMessage,
    AgentProfile,
)
from app.ontologies.agent_runtime.toolkit import TOOL_DEFS, ToolRunner

logger = logging.getLogger(__name__)

# 各限额的默认值见 agent_runtime/limits.py 统一注册表（环境变量可覆盖）。


# 图表可视化能力（普通字符串，含 JSON 花括号，勿并入 f-string）
_CHARTS_GUIDE = """# 图表可视化
当工具查回的数据适合可视化时，你可以在回答里输出一个图表代码块，前端会把它渲染成交互式图表。
用法：另起一行，写一个语言标记为 chart 的围栏代码块，块内是一段 JSON：

```chart
{"type": "bar", "title": "各状态订单数", "data": [{"label": "待付款", "value": 12}, {"label": "已发货", "value": 34}]}
```

字段说明：
- type：bar 柱状（分类对比）| line 折线（时间趋势）| area 面积 | pie 饼图（占比构成）
- data：数组，每项 {"label": 分类或时间, "value": 数值}
- title / xLabel / yLabel：可选
- 多序列（可选）：加 "series": [{"key": "销量", "name": "销量"}, {"key": "利润", "name": "利润"}]，
  此时每个 data 项用这些 key 提供多个数值，如 {"label": "1月", "销量": 100, "利润": 20}

准则：
1. aggregate_objects 的统计结果，平台会依据真实数据自动渲染图表，你不必再为它输出 chart 代码块；仅当要可视化聚合之外的数据（如按历史事实画趋势）时才手写 chart 块。
2. 图表数据必须来自工具返回的真实结果，禁止编造数字。
3. 图表是文字结论的补充，仍要用文字说清关键结论，不能只甩一个图。
4. 数据点很少（1-2 个）、或本就是明细列表时，用表格或文字即可，不要为画图而画图。
5. 分类分布/对比优先 bar 或 pie；随时间或历史变化的趋势用 line 或 area。"""


def _system_prompt(scope) -> str:
    extra = (scope.profile.system_prompt_extra or "").strip()
    extra_block = ("\n# 附加指令\n" + extra) if extra else ""
    return f"""你是本体「{scope.ontology.name}」上的业务智能体，运行在 OntoPrompt 平台的授权边界内。

# 你的世界观
下面的本体技能卡是你对业务世界的全部认知。你只能通过平台提供的工具与这些对象、链接、动作交互；不存在其它数据源，不要假设本体之外还有表或字段。

{scope.skill_card()}

# 行为准则
1. 先查证后回答：任何涉及数据的结论都必须来自工具返回结果，禁止编造，也不要用本体之外的常识或训练记忆去补充业务事实。工具结果不足以回答时，明确说明还缺什么、需要用户补充或授权什么，而不是猜。多个结果相互矛盾时，指出差异并优先采信更具体、更有出处的来源。工具返回 partial=true 时，说明这是基于部分数据的估计。
2. 用中文回答，简洁、结构化；提到具体对象时带上它的标签（如订单号、名称），便于用户核对。
3. 统计类问题（总共/平均/最多/分布）优先用 aggregate_objects，不要拉列表自己数；已知关系序列的多跳问题用 traverse_path；明确询问两个具体实例之间有哪些路径时，用 find_paths，必要时先 search_objects 确认两端 id。
3b. 结构发现里 dataAccess.mode=remote 的对象类型是远程绑定：其数据不在本地工具的数据面内。本地 search/aggregate/遍历/图谱/历史工具会显式报 remote_ontology_semantic_query_required，此时如实告知用户本地助手没有该远程数据，引导改用超级助手 ontology_query 或统一查询 API；绝不能把 0/空结果当作答案，也不要换别的本地工具重试同一类型。
4. 用户问「为什么是这个值 / 谁改的 / 什么时候变的」，用 get_object_history 给出带出处的回答。
5. 用户问跨对象因果问题（「这个值为什么变了 / 哪个决策或动作导致的 / 谁批准的 / 后来又变成了什么 / 这个派生值怎么算出来的」），用 trace_causal_chain 沿事实流追溯：它给出 决策→动作→事实变更→后续覆盖 的因果链与出处，比 get_object_history 的单实例历史更进一步。链受深度与数量上限约束，返回 truncated=true 时必须说明这是部分链、可能还有未展示的上游或下游。
6. 需要修改数据时：只能用 propose_action 做预演并生成提案，真实执行由用户在界面上确认。绝不能声称你已经完成了修改。
7. 工具报错时，阅读错误信息里给出的可用选项，修正参数后重试；同一错误不要重复第三次。
8. 用户询问某字段拟议变化会波及哪些对象时，先用 analyze_change_impact 展示直接/间接关系可达范围。它是只读模拟，不代表确定业务因果；除非工具返回了受治理的因果规则，否则必须称为“关联范围”，不得声称这些对象一定会改变。
9. 管理哨兵时，只能管理来源为 assistant_dynamic 的动态哨兵。发布版本内置哨兵只读且绝不能生成其编辑、启停或删除提案。所有动态哨兵变更必须先用 propose_dynamic_sentinel_change 生成提案，等待用户在界面确认；创建后默认停用，必须通过当前发布版全量试跑才能启用。
10. 用户问「这条哨兵为什么触发 / 为什么没触发 / 为什么执行了动作」时，用 explain_sentinel_firing：它基于触发记录与命中快照做确定性还原，给出条件表达式、每个命中元组的对象与属性值证据、条件与绑定过滤的求值结果、状态语义与动作结果。解释以工具返回为准，不得臆造求值细节；snapshotMissing=true 或 matchLimit 截断时必须如实说明。
11. 用户明确要“决策推演、比较未来方案、what-if、辅助决策”时，必须调用 run_decision_simulation。可以先用查询工具澄清对象，但不能用普通文字冒充推演结果。推演中的多视角数量不是概率，评分不是因果证明；回答要说明数据截止时间、关键假设、分歧、早期信号和停止条件。推演只给建议，任何真实动作仍须另行生成提案并由用户确认。
12. 用户问「未来会怎样 / 如果…会怎样 / 态势推演」且面向数据演化时：先用 list_world_model_services 查看本本体可用的世界模型推演服务；有可用服务时，按其 exampleInput 形状用查询工具收集真实数据组装 context，再调用 run_world_model_simulation。推演返回的数字必须原样引用，禁止自行换算或外推；回答需注明所用服务名与推演步长。无可用服务时如实说明，不能用文字冒充推演结果。它与决策推演分工不同：比较方案取舍用 run_decision_simulation，问数据演化轨迹用世界模型。

{_CHARTS_GUIDE}
{extra_block}"""


def _summarize(name: str, result: dict) -> str:
    """步骤的一句话摘要，给前端时间线用。"""
    if "error" in result:
        return str(result["error"])[:120]
    if name == "search_objects":
        return f"{result.get('objectType', '')} 命中 {result.get('total', 0)} 条，返回 {result.get('returned', 0)} 条"
    if name == "get_object":
        return f"{result.get('objectType', '')} · {len(result.get('links', []))} 组关联"
    if name == "traverse_links":
        return f"{result.get('from', '')} —{result.get('linkType', '')}→ {result.get('returned', 0)} 个对象"
    if name == "traverse_path":
        return f"{result.get('from', '')} 经 {len(result.get('path') or [])} 跳 → {result.get('returned', 0)} 个终点对象"
    if name == "find_paths":
        return f"{result.get('sourceLabel', '')} → {result.get('targetLabel', '')}，找到 {len(result.get('paths') or [])} 条路径"
    if name == "analyze_change_impact":
        summary = result.get("summary") or {}
        return f"关联影响预演：直接 {summary.get('direct', 0)}，间接 {summary.get('indirect', 0)}"
    if name == "aggregate_objects":
        if "groups" in result:
            return f"{result.get('objectType', '')} 按 {result.get('groupBy')} 分 {len(result['groups'])} 组"
        return f"{result.get('objectType', '')} {result.get('metric')} = {result.get('value')}"
    if name == "get_object_history":
        return f"{result.get('instance', '')} 的 {len(result.get('facts', []))} 条事实"
    if name == "trace_causal_chain":
        summary = result.get("summary") or {}
        return (f"因果链：{summary.get('factNodes', 0)} 条事实 · "
                f"{summary.get('actionNodes', 0)} 次动作 · "
                f"{summary.get('decisionNodes', 0)} 条决策"
                + ("（部分链）" if result.get("truncated") else ""))
    if name == "explain_sentinel_firing":
        sentinel = result.get("sentinel") or {}
        firing = result.get("firing") or {}
        limits = result.get("explanationLimits") or {}
        return (f"哨兵「{sentinel.get('displayName', '')}」触发解释"
                f"（{firing.get('status')}，展开 {len(result.get('matchedTuples', []))}"
                f"/{limits.get('totalMatches', 0)} 个命中）")
    if name == "list_actions":
        return f"{len(result.get('actions', []))} 个可用动作"
    if name == "run_decision_simulation":
        return (f"决策推演完成：{result.get('perspectiveCount', 0)} 个视角，"
                f"建议「{result.get('recommendedOption') or '待复核'}」")
    if name == "list_world_model_services":
        return (f"世界模型推演服务：{len(result.get('available', []))} 个可用、"
                f"{len(result.get('blocked', []))} 个不可用")
    if name == "run_world_model_simulation":
        return (f"世界模型推演「{result.get('serviceName', '')}」"
                f"{'完成' if result.get('ok') else '失败'}（{result.get('durationMs', 0)}ms）")
    if name == "list_dynamic_sentinels":
        return f"{len(result.get('sentinels', []))} 个助手动态哨兵"
    if name == "propose_dynamic_sentinel_change":
        p = result.get("proposal") or {}
        return f"动态哨兵{p.get('operation', '')}提案：{p.get('sentinelName', '')}"
    if name == "propose_action":
        p = result.get("proposal") or {}
        ok = p.get("status") == "success"
        return f"提案「{p.get('actionName', '')}」预演{'通过' if ok else '未通过'}（{len(p.get('effects') or [])} 项效果）"
    return "完成"


def _compact_graph_node(node: dict) -> dict:
    """移除仅用于详情面板的重字段，保留图谱渲染和节点定位所需字段。"""
    keys = (
        "id", "entityId", "kind", "label", "secondaryLabel", "objectTypeId",
        "objectTypeLabel", "instanceId", "propertyName", "propertyType", "value",
    )
    return {key: node[key] for key in keys if key in node}


def _compact_graph_edge(edge: dict) -> dict:
    keys = ("id", "entityId", "kind", "source", "target", "label", "linkTypeId")
    return {key: edge[key] for key in keys if key in edge}


def _compact_path(path: dict) -> dict:
    keys = ("nodeIds", "edgeIds", "hops")
    return {key: path[key] for key in keys if key in path}


def _graph_display_result(result: dict) -> dict:
    """为助手联动保留可解析图结构，同时按相关性限制消息与数据库体积。"""
    node_limit, edge_limit, impact_limit = 80, 120, 60
    compact = {key: value for key, value in result.items()
               if key not in {"nodes", "edges", "paths", "impacts"}}
    compact["nodes"] = [_compact_graph_node(node)
                        for node in (result.get("nodes") or [])[:node_limit]]
    compact["edges"] = [_compact_graph_edge(edge)
                        for edge in (result.get("edges") or [])[:edge_limit]]
    if "paths" in result:
        compact["paths"] = [_compact_path(path) for path in (result.get("paths") or [])[:5]]
    if "impacts" in result:
        impacts = []
        for item in (result.get("impacts") or [])[:impact_limit]:
            retained = {key: item[key] for key in (
                "instanceId", "label", "objectType", "depth", "classification", "certainty",
            ) if key in item}
            retained["path"] = _compact_path(item.get("path") or {})
            impacts.append(retained)
        compact["impacts"] = impacts

    original_counts = {
        "nodes": len(result.get("nodes") or []),
        "edges": len(result.get("edges") or []),
        "impacts": len(result.get("impacts") or []),
    }
    displayed_counts = {
        "nodes": len(compact["nodes"]),
        "edges": len(compact["edges"]),
        "impacts": len(compact.get("impacts") or []),
    }
    compact["visualizationTruncated"] = original_counts != displayed_counts
    compact["visualizationCounts"] = {
        "available": original_counts,
        "displayed": displayed_counts,
    }

    # 极密图谱继续按层级收紧，但始终保留合法、可直接渲染的 JSON，而不是字符串预览。
    payload = json.dumps(compact, ensure_ascii=False, default=str)
    if len(payload) > limit("graph_display_cap"):
        compact["nodes"] = compact["nodes"][:40]
        compact["edges"] = compact["edges"][:60]
        if "impacts" in compact:
            compact["impacts"] = compact["impacts"][:30]
        compact["visualizationTruncated"] = True
        compact["visualizationCounts"]["displayed"] = {
            "nodes": len(compact["nodes"]),
            "edges": len(compact["edges"]),
            "impacts": len(compact.get("impacts") or []),
        }
    if len(json.dumps(compact, ensure_ascii=False, default=str)) > limit("graph_display_cap"):
        compact["nodes"] = compact["nodes"][:32]
        compact["edges"] = compact["edges"][:48]
        if "impacts" in compact:
            compact["impacts"] = compact["impacts"][:24]
        compact["visualizationCounts"]["displayed"] = {
            "nodes": len(compact["nodes"]),
            "edges": len(compact["edges"]),
            "impacts": len(compact.get("impacts") or []),
        }
    return compact


def _display_result(result: dict):
    """给前端「查看工具输出」用的结果；过大则截断，避免 SSE 与页面膨胀。"""
    cap = limit("tool_result_display_cap")
    try:
        payload = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return {"_note": "结果无法序列化展示"}
    if len(payload) <= cap:
        return result
    if result.get("kind") in {"path", "impact"}:
        return _graph_display_result(result)
    return {
        "_truncated": True,
        "_note": f"结果较大（约 {len(payload)} 字符），此处仅展示前 {cap} 字符预览。",
        "preview": payload[:cap],
    }


def _audit_result(result: dict):
    """把完整工具结果规范化为 JSON，供持久化与后续审计导出。"""
    try:
        return json.loads(json.dumps(result, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return {"_serializationError": "工具结果无法序列化"}


def _history_digest(older: list) -> Optional[str]:
    """把滑出窗口的更早消息压成一段轻量「对话回顾」——只保留早前的提问，
    维持长对话里的指代与延续。不额外调用 LLM、不落新表、不跨会话，纯会话内续接。"""
    qs = [(m.content or "").strip() for m in older
          if m.role == "user" and (m.content or "").strip()]
    if not qs:
        return None
    lines = "\n".join(f"- {q[:80]}" for q in qs[-15:])
    return "# 对话回顾（更早轮次的提问，帮助理解指代与延续；不必逐条重新回答）\n" + lines


def _finish_cancelled(db: Session, conv: AgentConversation, steps: list,
                      runner: ToolRunner, call_kwargs: dict,
                      usage: dict) -> Iterator[dict]:
    """用户中止：落一条 [已取消] 的 assistant 消息（保留已执行轨迹供审计）。"""
    _persist_assistant(db, conv, "[已取消]", steps, runner, call_kwargs, usage)
    yield {"type": "cancelled"}


def run_agent_turn(db: Session, ontology_id: str, user, question: str,
                   conversation_id: Optional[str] = None,
                   model_id: Optional[str] = None,
                   release_id: Optional[str] = None,
                   run_id: Optional[str] = None,
                   profile_override: Optional[AgentProfile] = None,
                   sandbox: bool = False) -> Iterator[dict]:
    """执行一个回合，yield 事件流。所有异常都转成 error 事件，绝不让 SSE 中途裸断。

    回合状态同步写入 chat_run_registry：SSE 推送与执行解耦后（MYW-71），前端
    在离开页面后凭 run_id 轮询回合状态，即可恢复「正在处理」的展示。

    profile_override / sandbox 仅供评估沙箱试跑（assistant_evaluation 双臂
    实验）使用：草稿 profile 不落库；sandbox 会话打标记、对用户侧列表不可见。
    """
    if run_id:
        chat_cancel_registry.register(run_id)
        chat_run_registry.register(run_id, ontology_id)
    terminal_status = "error"
    try:
        for event in _run(
            db, ontology_id, user, question, conversation_id, model_id,
            release_id, run_id, profile_override, sandbox):
            event_type = event.get("type")
            if event_type == "meta" and run_id:
                chat_run_registry.attach_conversation(
                    run_id, event.get("conversationId"))
            if event_type == "answer":
                terminal_status = "succeeded"
            elif event_type in ("cancelled", "error"):
                terminal_status = event_type
            yield event
    except GeneratorExit:
        # 浏览器刷新/离开会主动关闭 SSE。生成器关闭后不能在 finally 中继续 yield，
        # 否则 Python 会抛出 ``generator ignored GeneratorExit`` 并污染服务日志。
        # 流式路径已把执行解耦到独立线程，只有直接消费本生成器的调用方会走到这里。
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("agent 回合失败")
        terminal_status = "error"
        yield {"type": "error", "message": f"智能体执行失败: {e}"}
    finally:
        if run_id:
            chat_run_registry.mark_finished(run_id, terminal_status)
            chat_cancel_registry.unregister(run_id)
    yield {"type": "done"}


def _run(db: Session, ontology_id: str, user, question: str,
         conversation_id: Optional[str], model_id: Optional[str],
         release_id: Optional[str], run_id: Optional[str],
         profile_override: Optional[AgentProfile] = None,
         sandbox: bool = False) -> Iterator[dict]:
    try:
        ontology, profile, scope = build_scope(
            db, ontology_id, release_id=release_id)
    except ToolError as e:
        yield {"type": "error", "message": str(e)}
        return

    if profile_override is not None:
        # 评估沙箱试跑：同一本体与 release 界内换用草稿 profile（只读、不落库）
        profile = profile_override
        scope = scope.with_profile(profile_override)

    if not profile.enabled:
        yield {"type": "error", "message": "该本体的智能体已被停用，请联系管理员在边界配置中启用。"}
        return

    cfg = select_llm_model_config(db, model_id=model_id or profile.default_model_id)
    call_kwargs = llm_call_kwargs(cfg)
    if not call_kwargs:
        yield {"type": "error",
               "message": "尚未配置可用的 LLM。请先到「模型配置」添加一个对话模型（OpenAI 兼容或 Anthropic）。"}
        return

    user_id = getattr(user, "id", None)
    conv = None
    if conversation_id:
        conv = db.query(AgentConversation).filter(
            AgentConversation.id == conversation_id,
            AgentConversation.ontology_id == ontology_id,
            AgentConversation.user_id == user_id,
            AgentConversation.ontology_release_id == scope.release_id,
        ).first()
    if not conv:
        conv = AgentConversation(
                                 ontology_id=ontology_id,
                                 ontology_release_id=scope.release_id,
                                 user_id=user_id,
                                 title=(f"[评估沙箱] {question.strip()[:50]}" if sandbox
                                        else question.strip()[:60] or "新对话"),
                                 is_sandbox=bool(sandbox))
        db.add(conv)
        db.flush()

    recent = (db.query(AgentMessage)
              .filter(AgentMessage.conversation_id == conv.id)
              .order_by(AgentMessage.created_at.desc())
              .limit(limit("history_digest_scan")).all())[::-1]
    history_verbatim = min(limit("history_verbatim"), limit("history_digest_scan"))
    verbatim = recent[-history_verbatim:]
    older = recent[:-history_verbatim] if len(recent) > history_verbatim else []

    db.add(AgentMessage(conversation_id=conv.id, role="user", content=question))
    db.commit()

    yield {"type": "meta", "conversationId": conv.id,
           "model": call_kwargs.get("model"), "releaseId": scope.release_id}

    messages: list[dict] = [{"role": "system", "content": _system_prompt(scope)}]
    digest = _history_digest(older)
    if digest:
        messages.append({"role": "system", "content": digest})
    for m in verbatim:
        if m.role in ("user", "assistant") and (m.content or "").strip():
            messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": question})

    runner = ToolRunner(db, scope, decision_context={
        "conversation_id": conv.id,
        "created_by": user_id,
        "model_config_id": getattr(cfg, "id", None),
        "call_kwargs": call_kwargs,
    }, world_model_context={"user": user})
    steps: list[dict] = []
    usage_total = {"inputTokens": 0, "outputTokens": 0}
    answer: Optional[str] = None
    verification: Optional[answer_verifier.VerificationResult] = None
    max_steps = max(1, profile.max_steps or 8)

    for _ in range(max_steps):
        if chat_cancel_registry.is_cancelled(run_id):
            yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total)
            return
        try:
            resp = llm_bridge.chat(call_kwargs, messages, TOOL_DEFS)
        except llm_bridge.LLMError as e:
            answer = None
            yield {"type": "error", "message": str(e)}
            _persist_assistant(db, conv, f"[执行中断] {e}", steps, runner, call_kwargs, usage_total)
            return

        for k in usage_total:
            if resp.get("usage") and resp["usage"].get(k):
                usage_total[k] += resp["usage"][k]

        if chat_cancel_registry.is_cancelled(run_id):
            yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total)
            return

        if not resp["tool_calls"]:
            answer = resp.get("content") or "（模型未给出回答）"
            # ── 确定性结论校验：数字断言必须能对应到工具结果 ──
            if steps and answer:
                verification = answer_verifier.verify_answer(answer, steps)
                if (not verification.passed
                        and len(verification.unverified)
                        > limit("answer_verify_unverified_floor")):
                    for _ in range(limit("answer_verify_retries")):
                        if chat_cancel_registry.is_cancelled(run_id):
                            yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total)
                            return
                        messages.append({"role": "assistant", "content": answer})
                        messages.append({"role": "user", "content": answer_verifier.correction_prompt(
                            verification.unverified)})
                        try:
                            resp = llm_bridge.chat(call_kwargs, messages, [])
                        except llm_bridge.LLMError:
                            break
                        for k in usage_total:
                            if resp.get("usage") and resp["usage"].get(k):
                                usage_total[k] += resp["usage"][k]
                        answer = resp.get("content") or answer
                        verification = answer_verifier.verify_answer(answer, steps)
                        verification.retried = True
                        if verification.passed:
                            break
                if not verification.passed:
                    answer = answer + answer_verifier.unverified_notice(verification.unverified)
            break

        messages.append({"role": "assistant", "content": resp.get("content"),
                         "tool_calls": resp["tool_calls"]})
        for tc in resp["tool_calls"]:
            if chat_cancel_registry.is_cancelled(run_id):
                yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total)
                return
            started = time.time()
            try:
                result = runner.run(tc["name"], tc.get("arguments") or {})
            except ToolError as e:
                result = {"error": str(e)}
            except Exception as e:  # noqa: BLE001 — 工具内部意外不摧毁回合
                logger.exception("工具 %s 执行异常", tc["name"])
                result = {"error": f"工具内部错误: {e}"}
            duration = int((time.time() - started) * 1000)

            display_result = _display_result(result)
            step = {"tool": tc["name"], "arguments": tc.get("arguments") or {},
                    "summary": _summarize(tc["name"], result), "durationMs": duration,
                    "result": display_result}
            if "error" in result:
                step["error"] = result["error"]
            audit_step = {**step, "result": _audit_result(result)}
            if display_result is not result:
                audit_step["displayResult"] = display_result
            steps.append(audit_step)
            yield {"type": "step", **step}

            payload = json.dumps(result, ensure_ascii=False, default=str)
            if len(payload) > limit("tool_result_llm_cap"):
                payload = payload[:limit("tool_result_llm_cap")] + '…（结果过长已截断，请缩小查询范围）"}'
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "name": tc["name"], "content": payload})
    else:
        answer = f"已达到单回合最大步数（{max_steps} 步）仍未得出结论。请把问题拆小一点，或在边界配置中调大 max_steps。"

    _persist_assistant(db, conv, answer or "", steps, runner, call_kwargs, usage_total)
    yield {"type": "answer", "content": answer,
           "citations": runner.citations, "proposals": runner.proposals,
           "usage": usage_total,
           "verification": verification.summary() if verification else None}


def _persist_assistant(db: Session, conv: AgentConversation, content: str,
                       steps: list, runner: ToolRunner, call_kwargs: dict,
                       usage: dict) -> None:
    db.add(AgentMessage(
        conversation_id=conv.id, role="assistant", content=content,
        steps=steps, citations=runner.citations, proposals=runner.proposals,
        model=call_kwargs.get("model"), token_usage=usage,
    ))
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
