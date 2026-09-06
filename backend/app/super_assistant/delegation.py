"""超级助手委派执行器 — 引擎侧，零助手知识。

工作模型（对齐 runtime._chat_round 的线程+队列先例）：
- 子回合在守护线程 + 独立 SessionLocal 中执行（含以子会话自取的 user）：
  子域编排器内部 commit，绝不与 stream_chat 主会话共用事务（父取消/失败
  时子回合已提交的消息天然保留，这正是"子会话可复用"的产品语义）；
- 主生成器每 0.5s 轮询事件队列：查父取消、查超时、周期性产出 SSE 注释
  心跳（``": ping"`` 是注释不是事件，不触碰固定 10 种事件的 SSE 契约）；
- 行状态收尾是双写者竞争、单值落定：消费侧（取消/超时/完成）与工作线程
  都只做 only-if-running 的原子 UPDATE；conversation_ref 独立按
  only-if-NULL 回填——首回合取消/超时后子会话引用不丢，resume 不退化；
- 孤儿 running 行兜底：SSE 断开时生成器 finally 置 cancel_event 尽快停
  子回合；进程崩溃残留由下次委派前的陈旧行回收（interrupted）清理，
  避免部分唯一索引把该 (会话, 助手) 的后续委派永久毒化；
- 舱壁：模块级信号量限"同时在等待委派结果"的请求数（每委派占 1 等待
  线程 + 1 子会话连接），超限立即返回"通道忙"；超时/取消返回即释放额度，
  不可协作取消的子回合线程可能仍在后台跑到终态（由工作线程收尾）。

会话引用（conversation_ref）由委派表独占，绝不进入 LLM 可见上下文：
resume 解析 = 按 (super_conversation_id, assistant_key) 查最近一条。
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from sqlalchemy import update

from app.assistant_hub import registry as assistant_registry
from app.assistant_hub.contract import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    AssistantHubError,
    TurnResult,
)
from app.assistant_hub.registry import DELEGATION_TOOL_NAME
from app.auth.models import User
from app.shared.config import settings
from app.shared.database import SessionLocal
from app.super_assistant import remote_agent_service  # noqa: F401 导入即注册动态助手 provider
from app.super_assistant.models import SuperAssistantDelegation

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.5
_HEARTBEAT_POLLS = 20  # 每 20 次空转（约 10s）产出一条注释心跳

SYSTEM_PROMPT_RULE = """你已接入平台内其他助手（见 delegate_to_assistant 工具目录），作为用户的分身替用户与他们协作：
- 任务落在某个助手的能力域时优先委派，不要用通用工具硬做该域的专业工作。
- 委派的 task 必须自包含（子助手看不到本会话历史），关键背景不足时先向用户确认。
- 子助手答复中的澄清问题：能依据本会话上下文回答，就直接再次委派作答；答不了再转述给用户等待答复。
- 同一会话内再次委派同一助手默认续用上次子会话，不要重述全部背景；确需另起一条线时传 session=new。
- 如实转述子助手的结果与失败原因，不替它编造内容。
- 转述子助手的答复，或声称"已询问/已委派"之前，本会话必须已实际调用过 delegate_to_assistant 并拿到返回；还没有就先调用。被追问委派结果而本回合尚未调用时：立即委派一次，或如实说明尚未委派——绝不凭记忆或推测补写"工具返回"（包括 resumed、content 等字段）。"""

# 允许写入 delegations.status 的终态集合（含回收态 interrupted）
_TERMINAL_STATUSES = frozenset({STATUS_ANSWERED, STATUS_FAILED, STATUS_CANCELLED})

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


# 虚构委派的完成时声称模式（观测告警与下一轮提醒注入共用）
FABRICATION_CLAIM_PATTERN = re.compile(
    r"已委派|已询问|已在同一子会话|助手(答复|返回|回复|说|表示)"
)


def suspected_fabrication_reminder(messages) -> str:
    """上一条 assistant 消息声称了子助手结果、且本会话从未真正委派过时，
    返回注入下一轮 system 提示的定向提醒；其余情况返回空串。

    "会话从未委派过"是关键判别：真实委派过之后的后续消息里合理地引用
    "本体助手说过…"不属于虚构，不注入。
    """
    if not messages:
        return ""
    ever_delegated = any(
        any(
            step.get("toolName") == DELEGATION_TOOL_NAME
            for step in (getattr(message, "steps", None) or [])
        )
        for message in messages
        if getattr(message, "role", None) == "assistant"
    )
    if ever_delegated:
        return ""
    last_assistant = next(
        (message for message in reversed(messages)
         if getattr(message, "role", None) == "assistant"),
        None,
    )
    if last_assistant is None:
        return ""
    if FABRICATION_CLAIM_PATTERN.search(getattr(last_assistant, "content", "") or ""):
        return (
            "提醒：上一条回复声称了子助手结果，但本会话从未调用过 "
            "delegate_to_assistant。本轮若要转述任何子助手内容，必须先实际调用"
            "该工具；否则直接以自己的口吻回答，不要提及子助手。"
        )
    return ""


def _capped_content(content: str) -> str:
    """预截子助手内容，保证外层统一截断（super_assistant_tool_result_chars）
    不会切掉结果 JSON 的尾部字段（usage/resumed 等），LLM 永远拿到合法 JSON。"""
    cap = max(1_000, settings.super_assistant_tool_result_chars - 2_000)
    if len(content) <= cap:
        return content
    return content[:cap] + "\n…[子助手结果已截断，可在对应助手会话中查看全文]"


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


def _reclaim_stale_running(
    db, owner_id: str, conversation_id: str, assistant_key: str,
) -> int:
    """回收进程崩溃等残留的陈旧 running 行（转 interrupted）。

    部分唯一索引只允许每 (会话, 助手) 一条 running 行；不回收则后续委派
    的 INSERT 必然撞唯一索引且无自愈路径。窗口取 超时+60s：仍在超时窗内
    的 running 行可能是真实在跑的子回合（其工作线程会自行收尾）。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=max(1, settings.super_assistant_delegation_timeout_seconds) + 60
    )
    result = db.execute(
        update(SuperAssistantDelegation)
        .where(
            SuperAssistantDelegation.owner_id == owner_id,
            SuperAssistantDelegation.super_conversation_id == conversation_id,
            SuperAssistantDelegation.assistant_key == assistant_key,
            SuperAssistantDelegation.status == "running",
            SuperAssistantDelegation.last_turn_at < cutoff,
        )
        .values(status="interrupted", summary="进程中断残留回收")
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return int(result.rowcount or 0)


def _transition_if_running(
    db, delegation_id: str, *, status: str,
    summary: str = "", conversation_ref: str | None = None,
) -> bool:
    """消费侧终态转移：单条原子 UPDATE only-if-running（与工作线程竞争收尾）。"""
    values: dict[str, Any] = {"status": status, "last_turn_at": datetime.now(timezone.utc)}
    if summary:
        values["summary"] = summary[:200]
    if conversation_ref:
        values["conversation_ref"] = conversation_ref
    result = db.execute(
        update(SuperAssistantDelegation)
        .where(
            SuperAssistantDelegation.id == delegation_id,
            SuperAssistantDelegation.status == "running",
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return bool(result.rowcount)


def _worker_finalize(
    child_db, delegation_id: str, *, status: str,
    summary: str = "", conversation_ref: str | None = None,
) -> None:
    """工作线程侧收尾（best-effort，失败只记日志）：

    1. conversation_ref 按 only-if-NULL 回填——即使消费侧已因超时/取消
       把行转终态，子会话引用也不丢（首回合 ref 只在这里产生）；
    2. 终态转移 only-if-running——消费侧已收尾则不覆盖（如 timeout 不被
       answered 覆盖）。失败时由下次委派前的陈旧行回收兜底。
    """
    now = datetime.now(timezone.utc)
    try:
        if conversation_ref:
            child_db.execute(
                update(SuperAssistantDelegation)
                .where(
                    SuperAssistantDelegation.id == delegation_id,
                    SuperAssistantDelegation.conversation_ref.is_(None),
                )
                .values(conversation_ref=conversation_ref, last_turn_at=now)
                .execution_options(synchronize_session=False)
            )
        child_db.execute(
            update(SuperAssistantDelegation)
            .where(
                SuperAssistantDelegation.id == delegation_id,
                SuperAssistantDelegation.status == "running",
            )
            .values(
                status=status,
                summary=(summary or "")[:200],
                last_turn_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        child_db.commit()
    except Exception:  # noqa: BLE001 — 收尾失败不拖垮子回合结果回传
        child_db.rollback()
        logger.warning(
            "委派行工作线程收尾失败 delegation_id=%s", delegation_id, exc_info=True,
        )


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

    user = db.get(User, owner_id)
    # 动态条目（用户自配远程助手）需带 db+user 解析；引擎流程不变
    assistant = assistant_registry.get_assistant(assistant_key, db=db, user=user)
    # 执行时权限重验：assistant 是自由字符串，注入时过滤只决定可见性
    permitted_keys = (
        None
        if assistant is None or user is None
        else {
            item.spec().key
            for item in assistant_registry.permitted_assistants(db, user)
        }
    )
    if (
        assistant is None
        or user is None
        or assistant.spec().key not in (permitted_keys or set())
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

    cancel_event = threading.Event()
    try:
        resume_row = (
            None
            if session_policy == "new"
            else _latest_delegation(db, owner_id, conversation_id, assistant_key)
        )
        _reclaim_stale_running(db, owner_id, conversation_id, assistant_key)
        row = SuperAssistantDelegation(
            owner_id=owner_id,
            super_conversation_id=conversation_id,
            assistant_key=assistant_key,
            # ref 只以纯字符串进出线程，绝不跨线程读 ORM 属性
            conversation_ref=str(resume_row.conversation_ref) if resume_row and resume_row.conversation_ref else None,
            status="running",
            summary="",
            last_turn_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        initial_ref: str | None = row.conversation_ref
        delegation_id = row.id

        events: queue.Queue = queue.Queue()
        sentinel = object()

        def _work() -> None:
            child_db = SessionLocal()
            final_status, final_summary, final_ref = STATUS_FAILED, "", initial_ref
            try:
                # 子线程自取 user：父会话实例非线程安全，绝不跨线程共享
                worker_user = child_db.get(User, owner_id)
                if worker_user is None:
                    events.put(("hub_error", "用户不存在"))
                    final_summary = "用户不存在"
                    return
                ref = initial_ref
                if ref is None:
                    ref = assistant.start(child_db, worker_user, context=context)
                final_ref = ref
                result: TurnResult | None = None
                for item in assistant.run_turn(
                    child_db, worker_user, ref, task, cancel_event=cancel_event,
                ):
                    if isinstance(item, TurnResult):
                        result = item
                    events.put(("event", item))
                if result is None:
                    result = TurnResult(status=STATUS_FAILED, content="子助手未返回终态")
                final_status = (
                    result.status if result.status in _TERMINAL_STATUSES else STATUS_FAILED
                )
                final_summary = result.content
                # 收尾引用以结果为准：本回合可能新建/轮换了子会话（如远程助手
                # 首回合签发 session_ref），worker 侧先落库时不能只回填传入引用
                final_ref = result.conversation_ref or ref
                events.put(("done", (result, ref)))
            except AssistantHubError as exc:
                final_summary = str(exc)
                events.put(("hub_error", str(exc)))
            except Exception:  # noqa: BLE001 — 工作线程异常经队列回传，不裸死
                logger.exception("委派子回合线程异常 assistant=%s", assistant_key)
                final_summary = "子助手执行线程异常"
                events.put(("hub_error", "子助手执行线程异常"))
            finally:
                _worker_finalize(
                    child_db, delegation_id,
                    status=final_status, summary=final_summary,
                    conversation_ref=final_ref,
                )
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
                        db, delegation_id, status="cancelled", summary="用户停止生成",
                    )
                    return _result_json({
                        "status": "cancelled",
                        "assistant": assistant.spec().label,
                        "content": "已停止等待子助手；子会话已保留，可稍后继续委派追问。",
                    })
                if time.monotonic() > deadline:
                    cancel_event.set()
                    _transition_if_running(
                        db, delegation_id, status="timeout", summary="委派超时",
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
                    delegation_id,
                    status=result.status if result.status in _TERMINAL_STATUSES else STATUS_FAILED,
                    summary=result.content,
                    conversation_ref=result.conversation_ref or ref_used,
                )
                return _result_json({
                    "status": result.status,
                    "assistant": assistant.spec().label,
                    "content": _capped_content(result.content),
                    "note": result.note,
                    "createdNewConversation": result.created_new_conversation,
                    "resumed": resume_row is not None and session_policy != "new",
                    "usage": result.usage,
                })
            if kind == "hub_error":
                _transition_if_running(
                    db, delegation_id, status="failed", summary=str(payload),
                )
                return _result_json({
                    "status": "failed",
                    "assistant": assistant.spec().label,
                    "error": str(payload)[:1_000],
                })
            # kind == "event"：进度事件只驱动心跳节奏，不进入 LLM 上下文
    finally:
        # 客户端断开（GeneratorExit）/正常退出统一路径：通知子回合尽快到终态，
        # 由工作线程收尾行状态；额度即时释放（不可协作取消的子回合后台跑完）
        cancel_event.set()
        semaphore.release()
