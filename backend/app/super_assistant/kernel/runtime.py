"""kernel.v1 最小可恢复激活循环。

该实现先把模型一步执行、结果 Artifact 和事件事实串成完整闭环；工具/远端
Connector 通过同一 Call 接口逐步接入，不把模型调用塞回 HTTP 请求线程。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.model_configs.llm_gateway import strip_think_content
from app.model_configs.selector import llm_call_kwargs, select_llm_model_config
from app.shared.database import SessionLocal
from app.super_assistant import delegation, provider
from app.super_assistant.models import SuperAssistantConversation

from .contracts import CallOutcome, CallStatus, RunStatus
from .context import ContextCandidate, ContextPackPlanner, ContextTier, SourceRef
from .reconciler import ReconcileAction, RemoteObservation, decide_reconciliation, normalize_remote_state
from .models import (
    Artifact,
    ContextSnapshot,
    ExecutionAttempt,
    ExecutionCall,
    ExecutionRun,
    ExecutionStep,
    ExecutionTurn,
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
    """处理一条 queued Run；重复投递由 Run 状态和 lease fencing 消化。"""
    run_id = _run_id(payload)
    db = SessionLocal()
    token = None
    try:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
        if run is None or run.status in {s.value for s in {RunStatus.CANCELLED, RunStatus.EXPIRED, RunStatus.COMPLETED, RunStatus.FAILED}}:
            db.rollback()
            return
        if run.status not in {RunStatus.QUEUED.value, RunStatus.ACTIVE.value}:
            db.rollback()
            return
        token = acquire_lease(db, run_id=run.id, worker_id=f"kernel:{uuid.uuid4().hex[:12]}")
        before = run.status
        if before == RunStatus.QUEUED.value:
            run.status = RunStatus.ACTIVE.value
            run.version += 1
            append_event(
                db, run, event_type="run.status_changed",
                payload={"from": before, "to": run.status, "reason": "activation", "actor": "worker", "version": run.version},
                actor={"kind": "worker"}, command_id=str(payload.get("command_id") or uuid.uuid4()),
                idempotency_key=f"activate:{run.id}", lease=token,
            )
        conversation = db.scalar(select(SuperAssistantConversation).where(SuperAssistantConversation.id == run.conversation_id))
        if conversation is None:
            raise RuntimeError("conversation missing for execution Run")
        turn = ExecutionTurn(run_id=run.id, turn_no=0, trigger_ref=f"run:{run.id}")
        db.add(turn)
        db.flush()
        step = ExecutionStep(turn_id=turn.id, step_no=0)
        db.add(step)
        db.flush()
        call = ExecutionCall(
            run_id=run.id, turn_id=turn.id, step_id=step.id, call_index=0,
            capability_key="model.chat", capability_revision=1,
            input_snapshot_ref=f"run:{run.id}:context", side_effect_class="read_only",
            authorization_snapshot_ref=run.permission_snapshot_ref,
            idempotency_key=f"model:{run.id}:0", status="running", outcome="accepted",
        )
        db.add(call)
        db.flush()
        attempt = ExecutionAttempt(call_id=call.id, attempt_no=1, provider_status="started")
        db.add(attempt)
        db.flush()
        pack = ContextPackPlanner().plan([
            ContextCandidate(
                SourceRef("run_goal", run.id, str(run.version), f"run://{run.id}/goal", "kernel.v1", "kernel.v1"),
                run.goal, ContextTier.REQUIRED, relevance=1.0, section="working",
            ),
        ])
        snapshot = ContextSnapshot(
            run_id=run.id, turn_id=turn.id, attempt_id=attempt.id,
            pack_hash=pack.pack_hash, source_refs=list(pack.source_refs),
            budget=pack.budget,
            policy_revision="kernel.v1", redaction_revision="kernel.v1",
        )
        db.add(snapshot)
        db.flush()
        snapshot_id, pack_hash, goal_text = snapshot.id, snapshot.pack_hash, run.goal
        append_event(
            db, run, event_type="context.snapshot",
            payload={"snapshot_id": snapshot_id, "pack_hash": pack_hash, "source_refs": list(pack.source_refs)},
            actor={"kind": "worker"}, command_id=f"step:{step.id}", idempotency_key=f"context:{snapshot.id}", lease=token,
        )
        attempt.transport_request_ref = f"context:{snapshot.id}"
        db.commit()

        config = select_llm_model_config(
            db=db, model_id=conversation.model_config_id,
            purpose_tags=("super_assistant",), allow_vlm=False,
        )
        call_kwargs = llm_call_kwargs(config)
        if not call_kwargs:
            raise provider.ProviderError("没有可用的文本模型，请先配置模型")
        # selector 读取会触发 SQLAlchemy autobegin；显式重置后开启本次
        # request.header 事实事务，避免把读取事务误当作写事务。
        db.rollback()
        db.begin()
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
        assert run is not None
        assert_lease(run, token)
        append_event(
            db, run, event_type="request.header",
            payload={"snapshot_id": snapshot_id, "pack_hash": pack_hash, "model": str(call_kwargs.get("model") or "unknown"), "prompt_ref": "inline://kernel-system-prompt", "capability_snapshot_ref": "capability://kernel.v1"},
            actor={"kind": "worker"}, command_id=f"step:{step.id}", idempotency_key=f"request:{attempt.id}", lease=token,
        )
        db.commit()
        delegation_tools = delegation.delegation_tools(db, run.owner_id)
        tools = delegation_tools
        messages = [
            {"role": "system", "content": "你是 OpenOntology 的超级助手。请直接完成用户目标，并在无法完成时说明原因。"},
            {"role": "user", "content": pack.content},
        ]
        result = provider.chat(
            call_kwargs,
            messages,
            tools,
        )
        tool_calls = result.get("tool_calls") or []
        if tool_calls:
            # M4 第一条 Connector：平台 Assistant Hub。每个委派仍由旧
            # adapter 自己隔离子会话；Kernel 只保存调用事实和结果引用。
            for tool_call in tool_calls[:1]:
                if tool_call.get("name") != "delegate_to_assistant":
                    continue
                delegate_result = _invoke_hub_delegation(db, run, tool_call.get("arguments") or {})
                messages.extend([
                    {"role": "assistant", "content": result.get("content"), "tool_calls": tool_calls},
                    {"role": "tool", "tool_call_id": tool_call.get("id"), "name": tool_call.get("name"), "content": delegate_result},
                ])
                result = provider.chat(call_kwargs, messages, tools)
        content = strip_think_content(str(result.get("content") or ""))
        if not content:
            raise provider.ProviderError("模型未返回有效内容")
        db.rollback()
        db.begin()
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
        assert run is not None
        assert_lease(run, token)
        call = db.get(ExecutionCall, call.id)
        attempt = db.get(ExecutionAttempt, attempt.id)
        artifact = Artifact(
            owner_id=run.owner_id, run_id=run.id, call_id=call.id,
            kind="assistant.message", mime_type="text/markdown", size=len(content.encode()),
            checksum=_checksum(content), storage_ref=f"inline://{run.id}/{attempt.id}",
            inline_content=content, status="complete", integrity_status="verified", business_status="success", visibility="owner",
        )
        db.add(artifact)
        db.flush()
        append_event(
            db, run, event_type="assistant.delta",
            payload={"attempt_id": attempt.id, "delta_seq": 0, "content_ref": f"artifact://{artifact.id}"},
            actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}", idempotency_key=f"delta:{attempt.id}:0", lease=token,
        )
        append_event(
            db, run, event_type="assistant.message",
            payload={"attempt_id": attempt.id, "message_ref": f"artifact://{artifact.id}"},
            actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}", idempotency_key=f"message:{attempt.id}", lease=token,
        )
        append_event(
            db, run, event_type="call.outcome_changed",
            payload={"status": "closed", "outcome": "completed", "evidence_ref": f"artifact://{artifact.id}", "connector_id": None, "provider_event_id": None},
            actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}", idempotency_key=f"outcome:{call.id}", lease=token,
        )
        append_event(
            db, run, event_type="attempt.result",
            payload={"attempt_id": attempt.id, "provider_status": "completed", "result_ref": f"artifact://{artifact.id}", "error_ref": None, "safe_to_retry": False, "token_usage_ref": None, "cost_ref": None},
            actor={"kind": "worker"}, command_id=f"attempt:{attempt.id}", idempotency_key=f"result:{attempt.id}", lease=token,
        )
        call.status, call.outcome = "closed", "completed"
        attempt.provider_status, attempt.result_ref, attempt.finished_at = "completed", f"artifact://{artifact.id}", _now()
        step.status, step.close_reason, step.closed_at = "closed", "decision_complete", _now()
        turn.status, turn.close_reason, turn.closed_at = "closed", "completed", _now()
        before = run.status
        run.status, run.version, run.finished_at = "completed", run.version + 1, _now()
        append_event(
            db, run, event_type="run.status_changed",
            payload={"from": before, "to": run.status, "reason": "goal_result", "actor": "worker", "version": run.version},
            actor={"kind": "worker"}, command_id=f"run:{run.id}:complete", idempotency_key=f"complete:{run.id}", lease=token,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("kernel Run %s execution failed", run_id)
        _mark_run_failed(run_id, exc)
    finally:
        db.close()


def _mark_run_failed(run_id: str, exc: Exception) -> None:
    db = SessionLocal()
    try:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
        if run is None or run.status in {s.value for s in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.EXPIRED}}:
            db.rollback()
            return
        before = run.status
        run.status, run.version, run.finished_at = "failed", run.version + 1, _now()
        error = ErrorEnvelope("execution_failed", str(exc), retryable=False, safe_to_retry=False)
        append_event(
            db, run, event_type="run.status_changed",
            payload={"from": before, "to": "failed", "reason": error.error_code, "actor": "worker", "version": run.version},
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
        key = provider_event_id or str(call.reconcile_attempt_count)
        append_event(db, run, event_type="call.outcome_changed", payload={"status": status.value, "outcome": outcome.value, "evidence_ref": observation.evidence_ref, "connector_id": connector_id, "provider_event_id": provider_event_id}, actor={"kind": "reconciler"}, command_id=f"reconcile:{call.id}:{key}", idempotency_key=f"reconcile:{call.id}:{key}", connector_id=connector_id, provider_event_id=provider_event_id)
        db.commit()
    except Exception:
        db.rollback(); logger.exception("kernel reconciliation failed for run=%s call=%s", run_id, call_id)
    finally:
        db.close()


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
