"""kernel.v1 最小可恢复激活循环。

该实现先把模型一步执行、结果 Artifact 和事件事实串成完整闭环；工具/远端
Connector 通过同一 Call 接口逐步接入，不把模型调用塞回 HTTP 请求线程。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.model_configs.llm_gateway import strip_think_content
from app.model_configs.selector import llm_call_kwargs, select_llm_model_config
from app.shared.database import SessionLocal
from app.super_assistant import delegation, provider
from app.super_assistant.models import SuperAssistantConversation

from .contracts import CallOutcome, CallStatus, RunStatus
from .context import ContextCandidate, ContextPackPlanner, ContextTier, SourceRef
from .context_sources import collect_context_candidates, mark_selected_context_sources
from .reconciler import ReconcileAction, RemoteObservation, decide_reconciliation, normalize_remote_state
from .models import (
    Artifact,
    ExecutionEvent,
    ContextSnapshot,
    ExecutionAttempt,
    ExecutionCall,
    ExecutionRun,
    ExecutionStep,
    ExecutionTurn,
    InboxItem,
)
from .policies import ErrorEnvelope, ExecutionPolicy, SideEffectClass
from .store import _now, acquire_lease, append_event, assert_lease

logger = logging.getLogger(__name__)


def _checksum(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _run_id(payload: dict) -> str:
    value = payload.get("run_id") or str(payload.get("message_ref", "")).removeprefix("run://")
    if not value:
        raise ValueError("execution message missing run_id")
    return str(value)


async def process_execution_message(payload: dict) -> None:
    """Run one durable activation and continue its model loop until it yields.

    A Run is deliberately advanced in short, persisted Steps. Every model
    request gets its own Call/Attempt and the next request is only issued after
    the previous facts are committed. Waiting states close the current Turn;
    a later user input, approval decision, or connector observation wakes a
    fresh activation through the existing outbox path.
    """
    run_id = _run_id(payload)
    db = SessionLocal()
    token = None
    try:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
        terminal = {s.value for s in {RunStatus.CANCELLED, RunStatus.EXPIRED, RunStatus.COMPLETED, RunStatus.FAILED}}
        if run is None or run.status in terminal:
            db.rollback(); return
        if run.status not in {RunStatus.QUEUED.value, RunStatus.ACTIVE.value}:
            db.rollback(); return
        policy = _policy_for_run(run)
        token = acquire_lease(db, run_id=run.id, worker_id=f"kernel:{uuid.uuid4().hex[:12]}", ttl=policy.step_model_timeout + timedelta(seconds=30))
        before = run.status
        if before == RunStatus.QUEUED.value:
            run.status = RunStatus.ACTIVE.value; run.version += 1
            append_event(db, run, event_type="run.status_changed", payload={"from": before, "to": run.status, "reason": "activation", "actor": "worker", "version": run.version}, actor={"kind": "worker"}, command_id=str(payload.get("command_id") or uuid.uuid4()), idempotency_key=f"activate:{run.id}", lease=token)
        conversation = db.scalar(select(SuperAssistantConversation).where(SuperAssistantConversation.id == run.conversation_id))
        if conversation is None: raise RuntimeError("conversation missing for execution Run")
        messages = _rebuild_messages(db, run)
        delegation_tools = delegation.delegation_tools(db, run.owner_id)
        tools = delegation_tools
        db.commit()
        turn = _open_turn(db, run, trigger_ref=str(payload.get("message_ref") or f"run:{run.id}"), lease=token)
        db.commit()
        from sqlalchemy import func
        total_steps = db.scalar(select(func.count(ExecutionStep.id)).where(ExecutionStep.turn_id == turn.id)) or 0
        prior_turn_events = db.scalars(select(ExecutionEvent).where(ExecutionEvent.run_id == run.id, ExecutionEvent.event_type == "turn.started")).all()
        turn_started = any((event.payload or {}).get("turn_id") == turn.id for event in prior_turn_events)
        db.rollback()  # scalar() starts an implicit read transaction
        yielded = False
        for step_no in range(int(total_steps), policy.max_steps):
            db.begin()
            run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
            if run is None or run.status in terminal:
                db.rollback(); return
            assert_lease(run, token)
            turn = db.get(ExecutionTurn, turn.id)
            if turn is None: raise RuntimeError("execution Turn disappeared")
            step = ExecutionStep(turn_id=turn.id, step_no=step_no, status="open")
            db.add(step); db.flush()
            call = ExecutionCall(run_id=run.id, turn_id=turn.id, step_id=step.id, call_index=_next_call_index(db, run.id), capability_key="model.chat", capability_revision=1, input_snapshot_ref=f"run:{run.id}:context:{turn.turn_no}:{step_no}", side_effect_class="read_only", authorization_snapshot_ref=run.permission_snapshot_ref, idempotency_key=f"model:{run.id}:{turn.turn_no}:{step_no}", status="running", outcome="accepted")
            db.add(call); db.flush()
            attempt = ExecutionAttempt(call_id=call.id, attempt_no=1, provider_status="started")
            db.add(attempt); db.flush()
            candidates = [ContextCandidate(SourceRef("run_goal", run.id, str(run.version), f"run://{run.id}/goal", "kernel.v1", "kernel.v1"), run.goal, ContextTier.REQUIRED, relevance=1.0, section="working")]
            candidates.extend(collect_context_candidates(db, run.owner_id, run.goal))
            pack = ContextPackPlanner().plan(candidates)
            mark_selected_context_sources(db, pack.source_refs)
            snapshot = ContextSnapshot(run_id=run.id, turn_id=turn.id, attempt_id=attempt.id, pack_hash=pack.pack_hash, source_refs=list(pack.source_refs), budget=pack.budget, policy_revision="kernel.v1", redaction_revision="kernel.v1")
            db.add(snapshot); db.flush()
            if not turn_started:
                append_event(db, run, event_type="turn.started", payload={"turn_id": turn.id, "turn_no": turn.turn_no, "trigger_ref": turn.trigger_ref or f"run:{run.id}"}, actor={"kind": "worker"}, command_id=f"turn:{turn.id}:start", idempotency_key=f"turn-start:{turn.id}", lease=token)
                turn_started = 1
            append_event(db, run, event_type="step.started", payload={"step_id": step.id, "step_no": step_no}, actor={"kind": "worker"}, command_id=f"step:{step.id}:start", idempotency_key=f"step-start:{step.id}", lease=token)
            append_event(db, run, event_type="call.intent", payload={"call_id": call.id, "capability_key": call.capability_key, "capability_revision": call.capability_revision, "input_snapshot_ref": call.input_snapshot_ref, "side_effect_class": call.side_effect_class, "idempotency_key": call.idempotency_key}, actor={"kind": "worker"}, command_id=f"call:{call.id}:intent", idempotency_key=f"call-intent:{call.id}", lease=token)
            append_event(db, run, event_type="context.snapshot", payload={"snapshot_id": snapshot.id, "pack_hash": snapshot.pack_hash, "source_refs": list(pack.source_refs)}, actor={"kind": "worker"}, command_id=f"step:{step.id}", idempotency_key=f"context:{snapshot.id}", lease=token)
            append_event(db, run, event_type="attempt.started", payload={"attempt_id": attempt.id, "provider_status": "started", "request_ref": f"context:{snapshot.id}", "started_at": attempt.started_at.isoformat()}, actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}:start", idempotency_key=f"attempt-start:{attempt.id}", lease=token)
            attempt.transport_request_ref = f"context:{snapshot.id}"
            db.commit()
            config = select_llm_model_config(db=db, model_id=conversation.model_config_id, purpose_tags=("super_assistant",), allow_vlm=False)
            call_kwargs = llm_call_kwargs(config)
            if not call_kwargs: raise provider.ProviderError("没有可用的文本模型，请先配置模型")
            db.rollback(); db.begin()
            run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update()); assert run is not None; assert_lease(run, token)
            append_event(db, run, event_type="request.header", payload={"snapshot_id": snapshot.id, "pack_hash": snapshot.pack_hash, "model": str(call_kwargs.get("model") or "unknown"), "prompt_ref": "inline://kernel-system-prompt", "capability_snapshot_ref": "capability://kernel.v1"}, actor={"kind": "worker"}, command_id=f"step:{step.id}", idempotency_key=f"request:{attempt.id}", lease=token)
            db.commit()
            # ContextPack is the model-facing request view. Keep the durable
            # message history compact while injecting the current selected
            # sources for each Step.
            request_messages = [*messages, {"role": "system", "content": pack.content}]
            result = await asyncio.to_thread(provider.chat, call_kwargs, request_messages, tools)
            tool_calls = result.get("tool_calls") or []
            wait = _wait_request(result, tool_calls)
            if tool_calls and wait is None:
                handled = False
                for tool_call in tool_calls:
                    if tool_call.get("name") == "delegate_to_assistant" and not handled:
                        delegate_result = _invoke_hub_delegation(db, run, tool_call.get("arguments") or {})
                        messages.extend([{ "role": "assistant", "content": result.get("content"), "tool_calls": tool_calls }, { "role": "tool", "tool_call_id": tool_call.get("id"), "name": tool_call.get("name"), "content": delegate_result }])
                        handled = True; break
                if not handled:
                    wait = {"kind": "external_event", "reason": "connector_call", "target_ref": str(tool_calls[0].get("name") or "external")}
            content = strip_think_content(str(result.get("content") or ""))
            db.rollback()  # connector/tool adapters may have opened an implicit transaction
            db.begin(); run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update()); assert run is not None; assert_lease(run, token)
            call = db.get(ExecutionCall, call.id); attempt = db.get(ExecutionAttempt, attempt.id)
            artifact = _persist_assistant_artifact(db, run, call, attempt, content, token) if content else None
            _close_model_call(db, run, call, attempt, step, artifact, token)
            if wait is not None:
                _persist_waiting_state(db, run, turn, step, wait, token, call=call); db.commit(); yielded = True; break
            if not tool_calls:
                if not content: raise provider.ProviderError("模型未返回有效内容")
                step.status, step.close_reason, step.closed_at = "closed", "decision_complete", _now()
                append_event(db, run, event_type="step.closed", payload={"step_id": step.id, "reason": "decision_complete"}, actor={"kind": "worker"}, command_id=f"step:{step.id}:close", idempotency_key=f"step-close:{step.id}", lease=token)
                turn.status, turn.close_reason, turn.closed_at = "closed", "completed", _now()
                before = run.status; run.status, run.version, run.finished_at, run.wait_reason = "completed", run.version + 1, _now(), None
                append_event(db, run, event_type="turn.closed", payload={"turn_id": turn.id, "reason": "completed"}, actor={"kind": "worker"}, command_id=f"turn:{turn.id}:close", idempotency_key=f"turn-close:{turn.id}", lease=token)
                append_event(db, run, event_type="run.status_changed", payload={"from": before, "to": run.status, "reason": "goal_result", "actor": "worker", "version": run.version}, actor={"kind": "worker"}, command_id=f"run:{run.id}:complete", idempotency_key=f"complete:{run.id}", lease=token)
                run.lease_owner, run.lease_expires_at = None, None
                db.commit(); yielded = True; break
            # Tool result is fed into the next model step while this Turn stays open.
            step.status, step.close_reason, step.closed_at = "closed", "decision_complete", _now()
            db.commit()
        if not yielded:
            db.begin(); run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update()); assert run is not None; assert_lease(run, token)
            step = db.scalar(select(ExecutionStep).where(ExecutionStep.turn_id == turn.id).order_by(ExecutionStep.step_no.desc()))
            _persist_waiting_state(db, run, turn, step, {"kind": "resume", "reason": "max_steps"}, token); db.commit()
    except Exception as exc:
        db.rollback(); logger.exception("kernel Run %s execution failed", run_id); _mark_run_failed(run_id, exc)
    finally:
        db.close()


def _policy_for_run(run: ExecutionRun) -> ExecutionPolicy:
    values = {}
    if run.budget_snapshot_ref:
        try: values = json.loads(run.budget_snapshot_ref) if isinstance(run.budget_snapshot_ref, str) else dict(run.budget_snapshot_ref)
        except (TypeError, ValueError, json.JSONDecodeError): values = {}
    try: max_steps = max(1, min(int(values.get("max_steps", 8)), 128))
    except (TypeError, ValueError): max_steps = 8
    return ExecutionPolicy(max_steps=max_steps)


def _rebuild_messages(db, run: ExecutionRun) -> list[dict]:
    messages = [{"role": "system", "content": "你是 OpenOntology 的超级助手。请直接完成用户目标，并在无法完成时说明原因。"}, {"role": "user", "content": run.goal}]
    artifacts = db.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.kind == "assistant.message").order_by(Artifact.id)).all()
    for artifact in artifacts:
        if artifact.inline_content: messages.append({"role": "assistant", "content": artifact.inline_content})
    pending = db.scalars(select(InboxItem).where(InboxItem.run_id == run.id, InboxItem.status == "pending", InboxItem.source == "user").order_by(InboxItem.accepted_at, InboxItem.id)).all()
    for item in pending:
        payload = item.payload or {}; content = payload.get("content") or payload.get("content_ref")
        if content: messages.append({"role": "user", "content": str(content)})
        item.status, item.consumed_at = "consumed", _now()
    from .models import Approval
    approvals = db.scalars(select(Approval).where(Approval.run_id == run.id, Approval.status.in_(("approved", "denied"))).order_by(Approval.decided_at, Approval.id)).all()
    for approval in approvals:
        messages.append({"role": "system", "content": f"用户审批结果：{approval.status}（审批 {approval.id}）"})
    return messages


def _open_turn(db, run, *, trigger_ref: str, lease) -> ExecutionTurn:
    turn = db.scalar(select(ExecutionTurn).where(ExecutionTurn.run_id == run.id, ExecutionTurn.status == "open").order_by(ExecutionTurn.turn_no.desc()))
    if turn is not None: return turn
    latest = db.scalar(select(ExecutionTurn.turn_no).where(ExecutionTurn.run_id == run.id).order_by(ExecutionTurn.turn_no.desc()))
    turn = ExecutionTurn(run_id=run.id, turn_no=(latest + 1 if latest is not None else 0), trigger_ref=trigger_ref); db.add(turn); db.flush(); return turn


def _next_call_index(db, run_id: str) -> int:
    latest = db.scalar(select(ExecutionCall.call_index).where(ExecutionCall.run_id == run_id).order_by(ExecutionCall.call_index.desc()))
    return int(latest + 1 if latest is not None else 0)


def _wait_request(result: dict, tool_calls: list[dict]) -> dict | None:
    value = result.get("wait_for_input") or result.get("needs_input") or result.get("request_input")
    if value:
        detail = value if isinstance(value, dict) else {"question": str(value)}
        return {"kind": "question_answer", "reason": detail.get("reason") or "question", "question_id": detail.get("question_id") or uuid.uuid4().hex, "question": detail.get("question") or detail.get("prompt") or "请补充必要信息"}
    value = result.get("wait_for_approval") or result.get("approval_request")
    if value:
        detail = value if isinstance(value, dict) else {"target": str(value)}
        return {"kind": "approval_decision", "reason": "approval_required", "target_ref": detail.get("target_ref") or detail.get("target") or "approval", "target_summary": detail.get("target_summary") or detail.get("target") or "需要用户审批", "parameter_summary": detail.get("parameter_summary") or "{}", "scope_summary": detail.get("scope_summary") or "run scope"}
    value = result.get("wait_for_external") or result.get("external_call")
    if value:
        detail = value if isinstance(value, dict) else {"target_ref": str(value)}
        return {"kind": "external_event", "reason": detail.get("reason") or "external_call", "target_ref": detail.get("target_ref") or detail.get("target") or "external"}
    return None


def _persist_assistant_artifact(db, run, call, attempt, content: str, token):
    artifact = Artifact(owner_id=run.owner_id, run_id=run.id, call_id=call.id, kind="assistant.message", mime_type="text/markdown", size=len(content.encode()), checksum=_checksum(content), storage_ref=f"inline://{run.id}/{attempt.id}", inline_content=content, status="complete", integrity_status="verified", business_status="success", visibility="owner")
    db.add(artifact); db.flush()
    append_event(db, run, event_type="assistant.delta", payload={"attempt_id": attempt.id, "delta_seq": 0, "content_ref": f"artifact://{artifact.id}"}, actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}", idempotency_key=f"delta:{attempt.id}:0", lease=token)
    append_event(db, run, event_type="assistant.message", payload={"attempt_id": attempt.id, "message_ref": f"artifact://{artifact.id}"}, actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}", idempotency_key=f"message:{attempt.id}", lease=token)
    return artifact


def _close_model_call(db, run, call, attempt, step, artifact, token):
    evidence_ref = f"artifact://{artifact.id}" if artifact is not None else None
    append_event(db, run, event_type="call.outcome_changed", payload={"status": "closed", "outcome": "completed", "evidence_ref": evidence_ref, "connector_id": None, "provider_event_id": None}, actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}", idempotency_key=f"outcome:{call.id}", lease=token)
    append_event(db, run, event_type="attempt.result", payload={"attempt_id": attempt.id, "provider_status": "completed", "result_ref": evidence_ref, "error_ref": None, "safe_to_retry": False, "token_usage_ref": None, "cost_ref": None}, actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}", idempotency_key=f"result:{attempt.id}", lease=token)
    call.status, call.outcome = "closed", "completed"; attempt.provider_status, attempt.result_ref, attempt.finished_at = "completed", evidence_ref, _now()


def _persist_waiting_state(db, run, turn, step, wait, token, *, call=None):
    kind = wait.get("kind") or "resume"; run.wait_reason = wait.get("reason") or kind; target_ref = wait.get("target_ref"); inbox = None
    if kind == "approval_decision":
        from .models import Approval
        approval = Approval(owner_id=run.owner_id, run_id=run.id, call_id=call.id if call is not None else None, target_summary=str(wait.get("target_summary") or "需要用户审批"), parameter_summary=str(wait.get("parameter_summary") or "{}"), scope_summary=str(wait.get("scope_summary") or "run scope"), capability_revision=1, parameter_hash=_checksum(str(wait)), status="pending", expires_at=min(run.deadline or (_now() + timedelta(hours=24)), _now() + timedelta(hours=1)))
        db.add(approval); db.flush()
        inbox = InboxItem(run_id=run.id, kind=kind, priority=10, status="pending", approval_id=approval.id, target_ref=target_ref or approval.id, payload={"approval_id": approval.id}, source="system", idempotency_key=f"approval:{approval.id}"); db.add(inbox); db.flush()
        append_event(db, run, event_type="approval.requested", payload={"approval_id": approval.id, "run_id": run.id, "call_id": approval.call_id, "scope_snapshot_ref": run.permission_snapshot_ref or f"run://{run.id}/scope", "expires_at": approval.expires_at.isoformat()}, actor={"kind": "worker"}, command_id=f"approval:{approval.id}", idempotency_key=f"approval-request:{approval.id}", lease=token)
    elif kind in {"question_answer", "external_event"}:
        inbox = InboxItem(run_id=run.id, kind=kind, priority=20 if kind == "external_event" else 30, status="pending", question_id=wait.get("question_id"), target_ref=target_ref, payload={"question": wait.get("question")} if kind == "question_answer" else {"target_ref": target_ref}, source="system", idempotency_key=f"wait:{run.id}:{run.version}:{kind}"); db.add(inbox); db.flush()
    before = run.status; run.status = {"question_answer": "waiting_input", "approval_decision": "waiting_approval", "external_event": "waiting_external", "resume": "waiting_retry"}.get(kind, "waiting_retry"); run.version += 1
    append_event(db, run, event_type="inbox.appended", payload={"inbox_id": inbox.id if inbox else f"run:{run.id}:wait", "kind": kind, "target_ref": target_ref or (inbox.id if inbox else run.id), "expiry_policy": "none"}, actor={"kind": "worker"}, command_id=f"wait:{run.id}:{run.version}", idempotency_key=f"wait-event:{run.id}:{run.version}", lease=token)
    reason = {
        "waiting_input": "waiting_input", "waiting_approval": "waiting_approval",
        "waiting_external": "waiting_external", "waiting_retry": "waiting_retry",
    }.get(run.status, "waiting_retry")
    if step is not None:
        if step.status != "closed":
            step.status, step.close_reason, step.closed_at = "closed", reason, _now()
            append_event(db, run, event_type="step.closed", payload={"step_id": step.id, "reason": reason}, actor={"kind": "worker"}, command_id=f"step:{step.id}:wait", idempotency_key=f"step-wait:{step.id}", lease=token)
        elif step.close_reason != reason:
            # The model Call may already have closed the Step as
            # ``decision_complete`` immediately before the activation limit.
            # Update the durable projection without emitting a second close
            # event; an event-sourced projection sees the final reason.
            step.close_reason = reason
    turn.status, turn.close_reason, turn.closed_at = "closed", reason, _now(); append_event(db, run, event_type="turn.closed", payload={"turn_id": turn.id, "reason": reason}, actor={"kind": "worker"}, command_id=f"turn:{turn.id}:wait", idempotency_key=f"turn-wait:{turn.id}", lease=token)
    append_event(db, run, event_type="run.status_changed", payload={"from": before, "to": run.status, "reason": run.wait_reason, "actor": "worker", "version": run.version}, actor={"kind": "worker"}, command_id=f"run:{run.id}:wait:{run.version}", idempotency_key=f"run-wait:{run.id}:{run.version}", lease=token)
    # A yielded Turn must not hold a worker lease across human/external wait.
    run.lease_owner, run.lease_expires_at = None, None

def _mark_run_failed(run_id: str, exc: Exception) -> None:
    db = SessionLocal()
    try:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
        if run is None or run.status in {s.value for s in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.EXPIRED}}:
            db.rollback()
            return
        before = run.status
        # Cancellation/deadline intent wins over a late worker exception.  A
        # worker must never turn a user cancellation into a misleading failure.
        if before in {RunStatus.CANCEL_REQUESTED.value, RunStatus.CANCELLING.value}:
            terminal = RunStatus.EXPIRED.value if run.cancel_reason == "deadline" else RunStatus.CANCELLED.value
            run.status, run.version, run.finished_at = terminal, run.version + 1, _now()
            error = ErrorEnvelope("cancelled_during_execution", str(exc), retryable=False, safe_to_retry=False)
        else:
            run.status, run.version, run.finished_at = "failed", run.version + 1, _now()
            error = ErrorEnvelope("execution_failed", str(exc), retryable=False, safe_to_retry=False)
        append_event(
            db, run, event_type="run.status_changed",
            payload={"from": before, "to": run.status, "reason": error.error_code, "actor": "worker", "version": run.version},
            actor={"kind": "worker"}, command_id=f"run:{run.id}:failure", idempotency_key=f"failure:{run.id}",
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("kernel Run %s failure could not be persisted", run_id)
    finally:
        db.close()


async def reconcile_execution_message(payload: dict) -> None:
    """应用一次 Connector 对账观察，并以事实事件推进 Call。

    终态 Run 不会被迟到回调重开；未知结果按指数退避，超过预算标记人工介入。
    """
    run_id, call_id = payload.get("run_id"), payload.get("call_id")
    if not run_id or not call_id:
        logger.warning("kernel reconciliation message missing run_id/call_id")
        return
    db = SessionLocal()
    try:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == str(run_id)).with_for_update())
        call = db.scalar(select(ExecutionCall).where(ExecutionCall.id == str(call_id), ExecutionCall.run_id == str(run_id)).with_for_update())
        if run is None or call is None:
            db.rollback(); return
        latest_attempt = db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.call_id == call.id).order_by(ExecutionAttempt.attempt_no.desc()))
        try:
            run_status, call_status, call_outcome = RunStatus(run.status), CallStatus(call.status), CallOutcome(call.outcome)
        except ValueError:
            db.rollback(); return
        side_effect = SideEffectClass(call.side_effect_class) if call.side_effect_class in {x.value for x in SideEffectClass} else SideEffectClass.READ_ONLY
        provider_event_id, connector_id = payload.get("provider_event_id"), payload.get("connector_id")
        observation = RemoteObservation(normalize_remote_state(payload.get("remote_state") or payload.get("status")), provider_event_id=provider_event_id, evidence_ref=payload.get("evidence_ref"), raw_state=str(payload.get("remote_state") or payload.get("status") or ""))
        decision = decide_reconciliation(observation=observation, run_status=run_status, call_status=call_status, call_outcome=call_outcome, side_effect=side_effect, safe_to_retry=bool(getattr(latest_attempt, "safe_to_retry", False)), reconcile_attempt_count=call.reconcile_attempt_count, policy=ExecutionPolicy(), now=_now())
        if decision.action is ReconcileAction.IGNORE_LATE:
            status, outcome = call_status, call_outcome
            call.remote_observed_state_ref = observation.raw_state
        else:
            status, outcome = decision.call_status, decision.call_outcome
            call.status, call.outcome = status.value, outcome.value
            call.reconcile_attempt_count += 1
            call.next_reconcile_at = decision.next_reconcile_at
            call.remote_observed_state_ref = observation.raw_state
            call.evidence_ref = observation.evidence_ref
            call.provider_event_id = provider_event_id or call.provider_event_id
            call.manual_attention = decision.action is ReconcileAction.MANUAL_ATTENTION
            if call.manual_attention:
                _append_manual_attention(db, run, call, observation.raw_state)
        key = provider_event_id or str(call.reconcile_attempt_count)
        append_event(db, run, event_type="call.outcome_changed", payload={"status": status.value, "outcome": outcome.value, "evidence_ref": observation.evidence_ref, "connector_id": connector_id, "provider_event_id": provider_event_id}, actor={"kind": "reconciler"}, command_id=f"reconcile:{call.id}:{key}", idempotency_key=f"reconcile:{call.id}:{key}", connector_id=connector_id, provider_event_id=provider_event_id)
        db.commit()
    except Exception:
        db.rollback(); logger.exception("kernel reconciliation failed for run=%s call=%s", run_id, call_id)
    finally:
        db.close()


def _append_manual_attention(db, run: ExecutionRun, call: ExecutionCall, observed_state: str) -> None:
    """Create one durable operator inbox item for an exhausted reconciliation."""
    key = f"manual-attention:{call.id}"
    existing = db.scalar(select(InboxItem).where(InboxItem.run_id == run.id, InboxItem.idempotency_key == key))
    if existing is not None:
        return
    item = InboxItem(
        run_id=run.id, kind="manual_attention", priority=0, status="pending",
        call_id=call.id, target_ref=call.target_ref or call.id,
        payload={"call_id": call.id, "observed_state": observed_state, "reason": "reconciliation_exhausted"},
        source="system", idempotency_key=key, accepted_at=_now(),
    )
    db.add(item)
    db.flush()
    append_event(
        db, run, event_type="inbox.appended",
        payload={"inbox_id": item.id, "kind": item.kind, "target_ref": item.target_ref, "expiry_policy": "none"},
        actor={"kind": "system"}, command_id=key, idempotency_key=key,
    )


def _invoke_hub_delegation(db, run: ExecutionRun, arguments: dict) -> str:
    generator = delegation.run_delegation_tool(
        db, owner_id=run.owner_id, conversation_id=run.conversation_id,
        arguments=arguments, should_cancel=lambda: False,
    )
    while True:
        try:
            next(generator)
        except StopIteration as stop:
            return str(stop.value or "")
