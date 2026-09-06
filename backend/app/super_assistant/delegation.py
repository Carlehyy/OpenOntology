"""超级助手委派执行器 — 引擎侧，零助手知识。

工作模型（对齐 runtime._chat_round 的线程+队列先例）：
- 子回合在守护线程 + 独立 SessionLocal 中执行：子域编排器内部 commit，
  绝不与 stream_chat 主会话共用事务（父取消/失败时子回合已提交的消息
  天然保留，这正是"子会话可复用"的产品语义）；
- 主生成器每 0.5s 轮询事件队列：查父取消、查超时、周期性产出 SSE 注释
  心跳（``": ping"`` 是注释不是事件，不触碰固定 10 种事件的 SSE 契约）；
- 取消 = 停止等待并把委派行转终态；能协作取消的子助手经 adapter 桥接
  真正停下（如本体助手），不能的（如业务探索）在后台跑到终态，由工作
  线程按 only-if-running 收尾行状态；
- 舱壁：模块级信号量限并发（每委派占 1 线程 + 1 DB 连接），超限立即
  返回"通道忙"，不排队放大资源占用。

会话引用（conversation_ref）由委派表独占，绝不进入 LLM 可见上下文：
resume 解析 = 按 (super_conversation_id, assistant_key) 查最近一条。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from app.assistant_hub import registry as assistant_registry
from app.assistant_hub.contract import (
    STATUS_FAILED,
    AssistantHubError,
    TurnResult,
)
from app.assistant_hub.registry import DELEGATION_TOOL_NAME
from app.auth.models import User
from app.shared.config import settings
from app.shared.database import SessionLocal
from app.super_assistant.models import SuperAssistantDelegation

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.5
_HEARTBEAT_POLLS = 20  # 每 20 次空转（约 10s）产出一条注释心跳

SYSTEM_PROMPT_RULE = """你已接入平台内其他助手（见 delegate_to_assistant 工具目录），作为用户的分身替用户与他们协作：
- 任务落在某个助手的能力域时优先委派，不要用通用工具硬做该域的专业工作。
- 委派的 task 必须自包含（子助手看不到本会话历史），关键背景不足时先向用户确认。
- 子助手答复中的澄清问题：能依据本会话上下文回答，就直接再次委派作答；答不了再转述给用户等待答复。
- 同一会话内再次委派同一助手默认续用上次子会话，不要重述全部背景；确需另起一条线时传 session=new。
- 如实转述子助手的结果与失败原因，不替它编造内容。"""

_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def _delegation_semaphore() -> threading.Semaphore:
    global _semaphore
    with _semaphore_lock:
        if _semaphore is None:
            _semaphore = threading.Semaphore(
                max(1, settings.super_assistant_delegation_max_concurrent)
            )
        return _semaphore


def _result_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def delegation_tools(db, owner_id: str) -> list[dict[str, Any]]:
    """按用户菜单权限返回委派工具 schema（无可委派助手时为空）。"""
    user = db.get(User, owner_id)
    if user is None:
        return []
    schema = assistant_registry.delegation_tool_schema(
        assistant_registry.permitted_assistants(db, user)
    )
    return [schema] if schema else []


def _latest_delegation(
    db, owner_id: str, conversation_id: str, assistant_key: str,
) -> SuperAssistantDelegation | None:
    return (
        db.query(SuperAssistantDelegation)
        .filter(
            SuperAssistantDelegation.owner_id == owner_id,
            SuperAssistantDelegation.super_conversation_id == conversation_id,
            SuperAssistantDelegation.assistant_key == assistant_key,
        )
        .order_by(
            SuperAssistantDelegation.last_turn_at.desc(),
            SuperAssistantDelegation.created_at.desc(),
        )
        .first()
    )


def _transition_if_running(
    db, delegation_id: str, *, status: str,
    summary: str = "", conversation_ref: str | None = None,
) -> bool:
    """only-if-running 的终态转移：父级（取消/超时）与工作线程竞争收尾。"""
    row = db.query(SuperAssistantDelegation).filter(
        SuperAssistantDelegation.id == delegation_id,
        SuperAssistantDelegation.status == "running",
    ).first()
    if row is None:
        return False
    row.status = status
    if summary:
        row.summary = summary[:200]
    if conversation_ref:
        row.conversation_ref = conversation_ref
    row.last_turn_at = datetime.now(timezone.utc)
    db.commit()
    return True


def run_delegation_tool(
    db, *,
    owner_id: str,
    conversation_id: str,
    arguments: dict[str, Any],
    should_cancel: Callable[[], bool],
) -> Iterator[str]:
    """执行一次委派；等待期间产出 SSE 注释心跳，return 工具结果 JSON。

    由 stream_chat 的串行工具分支 ``yield from`` 驱动（委派写子会话，
    绝不进只读并行池）。
    """
    assistant_key = str(arguments.get("assistant") or "").strip()
    task = str(arguments.get("task") or "").strip()
    session_policy = str(arguments.get("session") or "resume").strip()
    raw_context = arguments.get("context")
    context = raw_context if isinstance(raw_context, dict) else {}

    assistant = assistant_registry.get_assistant(assistant_key)
    user = db.get(User, owner_id)
    # 执行时权限重验：assistant 是自由字符串，注入时过滤只决定可见性
    if (
        assistant is None
        or user is None
        or assistant not in assistant_registry.permitted_assistants(db, user)
    ):
        return _result_json({
            "status": "failed",
            "error": f"助手 {assistant_key or '(空)'} 不可用或你无权委派它",
        })
    if not task:
        return _result_json({"status": "failed", "error": "task 不能为空"})

    semaphore = _delegation_semaphore()
    if not semaphore.acquire(blocking=False):
        return _result_json({
            "status": "failed",
            "error": (
                f"委派通道忙（并发上限 "
                f"{settings.super_assistant_delegation_max_concurrent}），请稍后再试"
            ),
        })

    try:
        resume_row = (
            None
            if session_policy == "new"
            else _latest_delegation(db, owner_id, conversation_id, assistant_key)
        )
        row = SuperAssistantDelegation(
            owner_id=owner_id,
            super_conversation_id=conversation_id,
            assistant_key=assistant_key,
            conversation_ref=resume_row.conversation_ref if resume_row else None,
            status="running",
            summary="",
            last_turn_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        events: queue.Queue = queue.Queue()
        cancel_event = threading.Event()
        sentinel = object()

        def _work() -> None:
            child_db = SessionLocal()
            try:
                ref = row.conversation_ref
                if ref is None:
                    ref = assistant.start(child_db, user, context=context)
                result: TurnResult | None = None
                for item in assistant.run_turn(
                    child_db, user, ref, task, cancel_event=cancel_event,
                ):
                    if isinstance(item, TurnResult):
                        result = item
                    events.put(("event", item))
                if result is None:
                    result = TurnResult(status=STATUS_FAILED, content="子助手未返回终态")
                events.put(("done", (result, ref)))
            except AssistantHubError as exc:
                events.put(("hub_error", str(exc)))
            except Exception:  # noqa: BLE001 — 工作线程异常经队列回传，不裸死
                logger.exception("委派子回合线程异常 assistant=%s", assistant_key)
                events.put(("hub_error", "子助手执行线程异常"))
            finally:
                child_db.close()
                events.put(sentinel)

        threading.Thread(
            target=_work, daemon=True, name=f"sa-delegate-{assistant_key[:20]}",
        ).start()

        deadline = time.monotonic() + max(
            1, settings.super_assistant_delegation_timeout_seconds,
        )
        idle_polls = 0
        while True:
            try:
                item = events.get(timeout=_POLL_INTERVAL_SECONDS)
            except queue.Empty:
                idle_polls += 1
                if idle_polls % 2 == 0 and should_cancel():
                    cancel_event.set()
                    _transition_if_running(
                        db, row.id, status="cancelled", summary="用户停止生成",
                    )
                    return _result_json({
                        "status": "cancelled",
                        "assistant": assistant.spec().label,
                        "content": "已停止等待子助手；子会话已保留，可稍后继续委派追问。",
                    })
                if time.monotonic() > deadline:
                    cancel_event.set()
                    _transition_if_running(
                        db, row.id, status="timeout", summary="委派超时",
                    )
                    return _result_json({
                        "status": "failed",
                        "assistant": assistant.spec().label,
                        "error": (
                            f"子助手超过 "
                            f"{settings.super_assistant_delegation_timeout_seconds}s"
                            " 未完成，已停止等待；子会话已保留，可稍后继续追问。"
                        ),
                    })
                if idle_polls % _HEARTBEAT_POLLS == 0:
                    yield ": ping\n\n"
                continue
            if item is sentinel:
                continue
            kind, payload = item
            if kind == "done":
                result, ref_used = payload
                _transition_if_running(
                    db,
                    row.id,
                    status=result.status,
                    summary=result.content,
                    conversation_ref=result.conversation_ref or ref_used,
                )
                return _result_json({
                    "status": result.status,
                    "assistant": assistant.spec().label,
                    "content": result.content,
                    "note": result.note,
                    "createdNewConversation": result.created_new_conversation,
                    "resumed": resume_row is not None,
                    "usage": result.usage,
                })
            if kind == "hub_error":
                _transition_if_running(
                    db, row.id, status="failed", summary=str(payload),
                )
                return _result_json({
                    "status": "failed",
                    "assistant": assistant.spec().label,
                    "error": str(payload),
                })
            # kind == "event"：进度事件只驱动心跳节奏，不进入 LLM 上下文
    finally:
        semaphore.release()
