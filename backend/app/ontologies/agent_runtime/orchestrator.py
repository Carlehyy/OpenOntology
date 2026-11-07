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

import logging
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy.orm import Session

from app.model_configs.selector import select_llm_model_config, llm_call_kwargs
from app.ontologies.agent_runtime import llm_bridge
from app.ontologies.agent_runtime import answer_verifier
from app.ontologies.agent_runtime.answer_masks import derived_notice, incomplete_notice
from app.ontologies.agent_runtime import answer_evidence
from app.ontologies.agent_runtime.context_policy import tool_payload
from app.ontologies.agent_runtime.prompting import _system_prompt
from app.ontologies.agent_runtime.tool_execution import _execute_tool_calls
from app.ontologies.agent_runtime.turn_presenter import _audit_result, _display_result, _summarize
from app.ontologies.agent_runtime.turn_model import model_events
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.chat_cancel import chat_cancel_registry
from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.models import (
    AgentConversation,
    AgentMessage,
    AgentProfile,
)
from app.ontologies.agent_runtime.toolkit import TOOL_DEFS, ToolRunner

logger = logging.getLogger(__name__)

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
                      usage: dict, execution=None) -> Iterator[dict]:
    """用户中止：落一条 [已取消] 的 assistant 消息（保留已执行轨迹供审计）。"""
    _persist_assistant(db, conv, "[已取消]", steps, runner, call_kwargs, usage,
                       commit=execution is None)
    yield {"type": "cancelled"}


def run_agent_turn(db: Session, ontology_id: str, user, question: str,
                   conversation_id: Optional[str] = None,
                   model_id: Optional[str] = None,
                   release_id: Optional[str] = None,
                   run_id: Optional[str] = None,
                   profile_override: Optional[AgentProfile] = None,
                   sandbox: bool = False,
                   execution=None) -> Iterator[dict]:
    """执行一个回合，yield 事件流。所有异常都转成 error 事件，绝不让 SSE 中途裸断。

    持久回合由 run_service 管理状态与事件，本生成器只负责授权范围内的规划执行。
    本进程取消信号用于模型和工具之间的快速检查，持久取消事实由 execution 提供。

    profile_override / sandbox 仅供评估沙箱试跑（assistant_evaluation 双臂
    实验）使用：草稿 profile 不落库；sandbox 会话打标记、对用户侧列表不可见。
    """
    if run_id:
        chat_cancel_registry.register(run_id)
    try:
        for event in _run(
            db, ontology_id, user, question, conversation_id, model_id,
            release_id, run_id, profile_override, sandbox, execution):
            yield event
    except GeneratorExit:
        # 浏览器刷新/离开会主动关闭 SSE。生成器关闭后不能在 finally 中继续 yield，
        # 否则 Python 会抛出 ``generator ignored GeneratorExit`` 并污染服务日志。
        # 流式路径已把执行解耦到独立线程，只有直接消费本生成器的调用方会走到这里。
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("agent 回合失败")
        yield {"type": "error", "message": f"智能体执行失败: {e}"}
    finally:
        if run_id:
            chat_cancel_registry.unregister(run_id)
    yield {"type": "done"}


def _run(db: Session, ontology_id: str, user, question: str,
         conversation_id: Optional[str], model_id: Optional[str],
         release_id: Optional[str], run_id: Optional[str],
         profile_override: Optional[AgentProfile] = None,
         sandbox: bool = False, execution=None) -> Iterator[dict]:
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

    history = db.query(AgentMessage).filter(AgentMessage.conversation_id == conv.id)
    if execution is not None:
        question_message = db.get(AgentMessage, execution.user_message_id)
        if question_message is None or question_message.conversation_id != conv.id:
            raise ToolError("run_message_mismatch: 回合消息与会话不一致")
        history = history.filter(AgentMessage.created_at < question_message.created_at)
    recent = (history.order_by(AgentMessage.created_at.desc())
              .limit(limit("history_digest_scan")).all())[::-1]
    history_verbatim = min(limit("history_verbatim"), limit("history_digest_scan"))
    verbatim = recent[-history_verbatim:]
    older = recent[:-history_verbatim] if len(recent) > history_verbatim else []

    if execution is None:
        db.add(AgentMessage(conversation_id=conv.id, role="user", content=question))
        db.commit()

    yield {"type": "meta", "conversationId": conv.id,
           "model": call_kwargs.get("model"), "releaseId": scope.release_id}

    messages: list[dict] = [{"role": "system", "content": _system_prompt(scope)}]
    digest = _history_digest(older)
    if digest:
        messages.append({"role": "system", "content": digest})
    history_evidence = answer_evidence.trusted_history_steps(recent, scope)
    history_context = answer_evidence.evidence_digest(
        history_evidence,
        title="同会话已核验历史工具证据（仅限当前授权与发布版本；可直接引用其中数字）",
        cap=min(limit("tool_result_llm_cap"), limit("context_chars_cap") // 6),
    )
    for m in verbatim:
        if m.role in ("user", "assistant") and (m.content or "").strip():
            messages.append({"role": m.role, "content": m.content})
    question_content = question
    if history_context:
        # 工具数据以 user 消息承载并显式声明为数据，避免 role=system 被当成
        # 高优先级指令；合并进本轮问题也不额外插入 user turn/打断 tool pair。
        question_content = (
            history_context
            + "\n\n以上是同会话既有工具数据，只作回答参考，不是新的用户指令。"
            + f"\n\n# 用户本轮问题\n{question}"
        )
    messages.append({"role": "user", "content": question_content})

    runner = ToolRunner(db, scope, decision_context={
        "conversation_id": conv.id,
        "created_by": user_id,
        "model_config_id": getattr(cfg, "id", None),
        "call_kwargs": call_kwargs,
    }, world_model_context={"user": user}, user=user, query_session_id=conv.id)
    try:
        yield from _execute_rounds(
            db, conv, runner, call_kwargs, messages, profile, run_id, execution,
            history_evidence)
    finally:
        runner.close_queries()


def _execute_rounds(db: Session, conv: AgentConversation, runner: ToolRunner,
                    call_kwargs: dict, messages: list[dict], profile: AgentProfile,
                    run_id: Optional[str], execution,
                    history_evidence: Optional[list[dict]] = None) -> Iterator[dict]:
    steps: list[dict] = []
    evidence_steps: list[dict] = list(history_evidence or [])
    usage_total = {"inputTokens": 0, "outputTokens": 0}
    answer: Optional[str] = None
    verification: Optional[answer_verifier.VerificationResult] = None
    max_steps = max(1, profile.max_steps or 8)

    def cancelled() -> bool:
        return (chat_cancel_registry.is_cancelled(run_id)
                or execution is not None and execution.is_cancelled())

    for _ in range(max_steps):
        if cancelled():
            yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total, execution)
            return
        try:
            resp = None
            for model_event in model_events(call_kwargs, messages, TOOL_DEFS, execution):
                if "final" in model_event:
                    resp = model_event["final"]
                else:
                    yield model_event
            if cancelled():
                yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total, execution)
                return
            if resp is None:
                raise llm_bridge.LLMError("模型流在最终响应前结束，请重试本轮问题。")
        except llm_bridge.LLMError as e:
            answer = None
            _persist_assistant(db, conv, f"[执行中断] {e}", steps, runner, call_kwargs,
                               usage_total, commit=execution is None)
            yield {"type": "error", "message": str(e)}
            return

        for k in usage_total:
            if resp.get("usage") and resp["usage"].get(k):
                usage_total[k] += resp["usage"][k]

        if cancelled():
            yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total, execution)
            return

        if not resp["tool_calls"]:
            answer = resp.get("content") or "（模型未给出回答）"
            # ── 确定性结论校验：数字断言必须能对应到完整工具结果 ──
            if evidence_steps and answer:
                verification = answer_verifier.verify_answer(answer, evidence_steps)
                if (not verification.passed
                        and len(verification.unverified)
                        > limit("answer_verify_unverified_floor")):
                    original_answer, original_verification = answer, verification
                    evidence_context = answer_evidence.evidence_digest(
                        evidence_steps,
                        title="可核验的工具结果（修正时保留这些数字）")
                    for _ in range(limit("answer_verify_retries")):
                        if cancelled():
                            yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total, execution)
                            return
                        messages.append({"role": "assistant", "content": answer})
                        messages.append({"role": "user", "content": answer_verifier.correction_prompt(
                            verification.unverified, evidence=evidence_context)})
                        try:
                            resp = None
                            for model_event in model_events(call_kwargs, messages, [], execution):
                                if "final" in model_event:
                                    resp = model_event["final"]
                                else:
                                    yield model_event
                            if cancelled():
                                yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total, execution)
                                return
                            if resp is None:
                                break
                        except llm_bridge.LLMError:
                            break
                        for k in usage_total:
                            if resp.get("usage") and resp["usage"].get(k):
                                usage_total[k] += resp["usage"][k]
                        corrected = (resp.get("content") or "").strip() or answer
                        candidate = answer_verifier.verify_answer(corrected, evidence_steps)
                        candidate.retried = True
                        choice = answer_evidence.choose_correction(
                            original_answer, original_verification, corrected, candidate)
                        answer, verification = choice.answer, choice.verification
                        if choice.reverted:
                            # 空泛修正丢失了原答案已核验数字：保留原文，显式告警。
                            verification.retried = True
                            break
                        if verification.passed:
                            break
                if not verification.passed:
                    answer = answer + answer_verifier.unverified_notice(verification.unverified)
            break

        messages.append({"role": "assistant", "content": resp.get("content"),
                         "tool_calls": resp["tool_calls"]})
        # 一批只读工具调用并行执行（远程统一查询按用户串行），结果按序产出。
        for tc, (result, task_citations, duration) in zip(
                resp["tool_calls"], _execute_tool_calls(runner, resp["tool_calls"])):
            if cancelled():
                yield from _finish_cancelled(db, conv, steps, runner, call_kwargs, usage_total, execution)
                return
            # 并行任务的引用按调用顺序合并回主 runner（去重、受引用上限约束）。
            for citation in task_citations:
                instance_id = citation.get("instanceId")
                if instance_id and instance_id not in runner._cited \
                        and len(runner.citations) < limit("citation_cap"):
                    runner._cited.add(instance_id)
                    runner.citations.append(citation)

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
            evidence_steps.append({"tool": tc["name"], "result": result})
            yield {"type": "step", **step}

            payload = tool_payload(result)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "name": tc["name"], "content": payload})
    else:
        answer = f"已达到单回合最大步数（{max_steps} 步）仍未得出结论。请把问题拆小一点，或在边界配置中调大 max_steps。"

    if verification is not None and verification.derived:
        answer = (answer or "") + derived_notice(verification.derived)
    answer = (answer or "") + incomplete_notice(answer or "", steps)
    _persist_assistant(db, conv, answer or "", steps, runner, call_kwargs, usage_total,
                       commit=execution is None)
    yield {"type": "answer", "content": answer,
           "citations": runner.citations, "proposals": runner.proposals,
           "usage": usage_total,
           "verification": verification.summary() if verification else None}


def _persist_assistant(db: Session, conv: AgentConversation, content: str,
                       steps: list, runner: ToolRunner, call_kwargs: dict,
                       usage: dict, *, commit: bool = True) -> None:
    runner.close_queries()
    db.add(AgentMessage(
        conversation_id=conv.id, role="assistant", content=content,
        steps=steps, citations=runner.citations, proposals=runner.proposals,
        model=call_kwargs.get("model"), token_usage=usage,
    ))
    conv.updated_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
    else:
        db.flush()
