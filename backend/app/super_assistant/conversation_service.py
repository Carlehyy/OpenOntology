from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

from fastapi import HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth.models import User
from app.data_channel.steward.browser_runtime import browser_manager
from app.model_configs.models import ModelConfig
from app.shared.database import SessionLocal
from app.super_assistant import files_workspace
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMessage,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantToolRun,
)
from app.super_assistant.schemas import (
    ApprovalRequest,
    ChatRequest,
    ConversationCreate,
    ConversationUpdate,
)


ConversationLookup = Callable[
    [Session, str, str],
    SuperAssistantConversation,
]
ChatStream = Callable[..., Iterator[str]]

logger = logging.getLogger(__name__)

# 超过该时长的 streaming 行视为死流（与 chat 的 409 闸门同口径）；
# 真实生成中断（进程重启/被杀）来不及走流内 GeneratorExit/异常兜底。
_STREAMING_STALE_SECONDS = 600
_INTERRUPTED_STREAM_NOTE = "上一次生成意外中断"


def _reap_stale_streaming(db: Session, conversation_id: str) -> int:
    """把会话内超时的遗留 streaming 回复标记为中断，返回回收条数。"""
    now = datetime.now(timezone.utc)
    stale_rows = db.query(SuperAssistantMessage).filter(
        SuperAssistantMessage.conversation_id == conversation_id,
        SuperAssistantMessage.role == "assistant",
        SuperAssistantMessage.status == "streaming",
    ).all()
    reaped = 0
    for row in stale_rows:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if (now - created_at).total_seconds() < _STREAMING_STALE_SECONDS:
            continue
        row.status = "error"
        row.content = row.content or _INTERRUPTED_STREAM_NOTE
        reaped += 1
    if reaped:
        db.commit()
    return reaped


def recover_interrupted_streams() -> dict[str, int]:
    """启动恢复：进程重启后遗留 streaming 的回复统一标记中断（重启语义）。"""
    db = SessionLocal()
    try:
        from app.super_assistant.models import SuperAssistantScheduledRun
        from app.super_assistant.scheduled_constants import STALE_RUNNING

        # 定时任务执行器在另一进程，会话正文跑完仍会被写回 complete；这里只保护
        # 「仍在 running 且开跑未超时」的会话。窗口必须与收割器同阈值：常量住在
        # 无依赖的 scheduled_constants，两边各自导入（直接 import scheduled_service
        # 会与 runtime 链成环，被架构守卫拦截）。
        live_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - STALE_RUNNING
        protected = {
            row[0]
            for row in db.query(SuperAssistantScheduledRun.conversation_id).filter(
                SuperAssistantScheduledRun.status == "running",
                SuperAssistantScheduledRun.started_at >= live_cutoff,
                SuperAssistantScheduledRun.conversation_id.isnot(None),
            )
        }
        stale_rows = db.query(SuperAssistantMessage).filter(
            SuperAssistantMessage.role == "assistant",
            SuperAssistantMessage.status == "streaming",
        ).all()
        interrupted = 0
        for row in stale_rows:
            if row.conversation_id in protected:
                continue
            row.status = "error"
            row.content = row.content or _INTERRUPTED_STREAM_NOTE
            interrupted += 1
        if interrupted:
            db.commit()
        return {"interrupted": interrupted}
    finally:
        db.close()


def _conversation(
    db: Session,
    owner_id: str,
    conversation_id: str,
) -> SuperAssistantConversation:
    item = db.query(SuperAssistantConversation).filter(
        SuperAssistantConversation.id == conversation_id,
        SuperAssistantConversation.owner_id == owner_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="会话不存在")
    return item


def _enabled_model(db: Session, model_config_id: str) -> ModelConfig:
    model = db.query(ModelConfig).filter(
        ModelConfig.id == model_config_id,
        ModelConfig.config_type == "llm",
        ModelConfig.enabled.is_(True),
    ).first()
    if not model:
        raise HTTPException(
            status_code=400,
            detail="所选模型不存在或未启用",
        )
    return model


def list_conversations(
    db: Session,
    current_user: User,
) -> list[SuperAssistantConversation]:
    return db.query(SuperAssistantConversation).filter(
        SuperAssistantConversation.owner_id == current_user.id,
        SuperAssistantConversation.status != "deleted",
    ).order_by(SuperAssistantConversation.updated_at.desc()).all()


def create_conversation(
    body: ConversationCreate,
    db: Session,
    current_user: User,
) -> SuperAssistantConversation:
    if body.model_config_id:
        _enabled_model(db, body.model_config_id)
    item = SuperAssistantConversation(
        owner_id=current_user.id,
        title=body.title.strip() or "新会话",
        model_config_id=body.model_config_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    db: Session,
    current_user: User,
    *,
    conversation_lookup_fn: ConversationLookup = _conversation,
) -> SuperAssistantConversation:
    item = conversation_lookup_fn(
        db,
        current_user.id,
        conversation_id,
    )
    if body.title is not None:
        item.title = body.title.strip()
    if "model_config_id" in body.model_fields_set:
        if body.model_config_id:
            _enabled_model(db, body.model_config_id)
        item.model_config_id = body.model_config_id
    if body.status is not None:
        item.status = body.status
    db.commit()
    db.refresh(item)
    return item


def delete_conversation(
    conversation_id: str,
    db: Session,
    current_user: User,
    *,
    conversation_lookup_fn: ConversationLookup = _conversation,
) -> Response:
    item = conversation_lookup_fn(
        db,
        current_user.id,
        conversation_id,
    )
    try:
        # 先释放浏览器会话再删库（与 steward 同序）；运行时故障不阻断删除
        browser_manager.close(conversation_id)
    except Exception:
        logger.warning("会话浏览器关闭失败: %s", conversation_id, exc_info=True)
    db.query(SuperAssistantReflectionCandidate).filter(
        SuperAssistantReflectionCandidate.conversation_id == item.id,
    ).delete(synchronize_session=False)
    db.query(SuperAssistantReflectionRun).filter(
        SuperAssistantReflectionRun.conversation_id == item.id,
    ).delete(synchronize_session=False)
    db.query(SuperAssistantToolRun).filter(
        SuperAssistantToolRun.conversation_id == item.id,
    ).delete(synchronize_session=False)
    db.query(SuperAssistantMessage).filter(
        SuperAssistantMessage.conversation_id == item.id,
    ).delete(synchronize_session=False)
    db.delete(item)
    db.commit()
    files_workspace.remove_session_files(conversation_id)
    return Response(status_code=204)


def list_messages(
    conversation_id: str,
    db: Session,
    current_user: User,
    *,
    conversation_lookup_fn: ConversationLookup = _conversation,
) -> list[SuperAssistantMessage]:
    item = conversation_lookup_fn(
        db,
        current_user.id,
        conversation_id,
    )
    # 读取兜底：进程死亡遗留的死流行在此回收，前端不再渲染永久"正在思考"
    _reap_stale_streaming(db, item.id)
    return db.query(SuperAssistantMessage).filter(
        SuperAssistantMessage.conversation_id == item.id,
    ).order_by(SuperAssistantMessage.created_at.asc()).all()


def export_conversation(
    conversation_id: str,
    db: Session,
    current_user: User,
    *,
    conversation_lookup_fn: ConversationLookup = _conversation,
) -> dict:
    """导出完整会话审计记录（与会话操作右键菜单「导出会话」配套）。

    与 steward 域导出同约定：format/version 标识负载结构；messages 携带
    steps 工具轨迹与 token 用量，summary 汇总统计。导出前与 list_messages
    同口径回收死流，避免进程死亡遗留的 streaming 空消息被定格进审计快照。
    """
    item = conversation_lookup_fn(
        db,
        current_user.id,
        conversation_id,
    )
    _reap_stale_streaming(db, item.id)
    messages = (
        db.query(SuperAssistantMessage)
        .filter(SuperAssistantMessage.conversation_id == item.id)
        .order_by(
            SuperAssistantMessage.created_at.asc(),
            SuperAssistantMessage.id.asc(),
        )
        .all()
    )
    tool_step_count = sum(len(message.steps or []) for message in messages)
    # runtime 写入的 token_usage 键名为 camelCase（inputTokens/outputTokens，
    # 见 runtime.py 组装处），导出统计按真实键名求和，勿用 total_tokens 假键
    total_tokens = sum(
        int((message.token_usage or {}).get("inputTokens", 0))
        + int((message.token_usage or {}).get("outputTokens", 0))
        for message in messages
    )
    return {
        "format": "openontology.super-assistant.conversation",
        "version": 1,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "conversation": {
            "id": item.id,
            "title": item.title,
            "model_config_id": item.model_config_id,
            "browser_source_id": item.browser_source_id,
            "status": item.status,
            "summary": item.summary,
            "summary_message_count": item.summary_message_count,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "messageCount": len(messages),
            "messages": [
                {
                    "id": message.id,
                    "conversation_id": message.conversation_id,
                    "role": message.role,
                    "content": message.content,
                    "status": message.status,
                    "steps": message.steps,
                    "token_usage": message.token_usage,
                    "created_at": message.created_at,
                }
                for message in messages
            ],
        },
        "summary": {
            "messageCount": len(messages),
            "toolStepCount": tool_step_count,
            "totalTokens": total_tokens,
        },
    }


def chat(
    conversation_id: str,
    body: ChatRequest,
    db: Session,
    current_user: User,
    *,
    conversation_lookup_fn: ConversationLookup = _conversation,
    stream_chat_fn: ChatStream,
) -> StreamingResponse:
    conversation = conversation_lookup_fn(
        db,
        current_user.id,
        conversation_id,
    )
    _reap_stale_streaming(db, conversation.id)
    active = db.query(SuperAssistantMessage).filter(
        SuperAssistantMessage.conversation_id == conversation.id,
        SuperAssistantMessage.role == "assistant",
        SuperAssistantMessage.status == "streaming",
    ).order_by(SuperAssistantMessage.created_at.desc()).first()
    if active:
        raise HTTPException(
            status_code=409,
            detail="当前会话仍有一条回复正在生成",
        )

    if body.model_config_id:
        _enabled_model(db, body.model_config_id)
        conversation.model_config_id = body.model_config_id

    user_count = db.query(SuperAssistantMessage).filter(
        SuperAssistantMessage.conversation_id == conversation.id,
        SuperAssistantMessage.role == "user",
    ).count()
    user_message = SuperAssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content=body.message.strip(),
        status="complete",
    )
    assistant_message = SuperAssistantMessage(
        conversation_id=conversation.id,
        role="assistant",
        content="",
        status="streaming",
    )
    db.add_all([user_message, assistant_message])
    if user_count == 0 and conversation.title == "新会话":
        conversation.title = (
            body.message.strip().replace("\n", " ")[:40]
        )
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(assistant_message)

    return StreamingResponse(
        stream_chat_fn(
            conversation_id=conversation.id,
            owner_id=current_user.id,
            assistant_message_id=assistant_message.id,
            requested_model_id=body.model_config_id,
            agent_mode=body.agent_mode,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def cancel_chat(
    conversation_id: str,
    db: Session,
    current_user: User,
    *,
    conversation_lookup_fn: ConversationLookup = _conversation,
) -> dict[str, bool]:
    conversation = conversation_lookup_fn(
        db,
        current_user.id,
        conversation_id,
    )
    active = db.query(SuperAssistantMessage).filter(
        SuperAssistantMessage.conversation_id == conversation.id,
        SuperAssistantMessage.role == "assistant",
        SuperAssistantMessage.status == "streaming",
    ).order_by(SuperAssistantMessage.created_at.desc()).first()
    if active:
        active.status = "cancelled"
        active.content = active.content or "已停止生成"
        db.commit()
    return {"cancelled": bool(active)}


def decide_tool_run(
    tool_run_id: str,
    body: ApprovalRequest,
    db: Session,
    current_user: User,
) -> dict[str, str]:
    tool_run = db.query(SuperAssistantToolRun).join(
        SuperAssistantConversation,
        SuperAssistantConversation.id
        == SuperAssistantToolRun.conversation_id,
    ).filter(
        SuperAssistantToolRun.id == tool_run_id,
        SuperAssistantConversation.owner_id == current_user.id,
    ).first()
    if not tool_run:
        raise HTTPException(
            status_code=404,
            detail="工具调用不存在",
        )
    if tool_run.status != "awaiting_confirmation":
        raise HTTPException(
            status_code=409,
            detail="该工具调用已处理或已过期",
        )
    tool_run.decision = body.decision
    tool_run.status = (
        "approved" if body.decision == "approve" else "denied"
    )
    if body.decision == "deny":
        tool_run.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": tool_run.id, "status": tool_run.status}
