"""内置工具目录、用户级启停与待确认工具卡恢复。

GET /tools 返回非 MCP 工具目录全景（声明/分类/条件可用性/启停状态），
PATCH /tools/{tool_name} 切换单个工具启停；GET
/conversations/{id}/pending-confirmation 恢复会话内待确认工具卡（与 SSE
tool_confirmation_required 同形载荷）。路径与主路由同前缀，鉴权由
main.py 挂载时的 menu_guard("super_assistant") 统一声明。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import ontology_tools, runtime
from app.super_assistant.conversation_service import _conversation
from app.super_assistant.models import (
    SuperAssistantMcpServer,
    SuperAssistantToolRun,
)
from app.super_assistant.schemas import (
    AssistantToolEnabledUpdate,
    AssistantToolOut,
)

router = APIRouter()


@router.get("/tools", response_model=list[AssistantToolOut])
def list_assistant_tools(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AssistantToolOut]:
    return runtime.builtin_tool_catalog(db, current_user.id)


@router.patch("/tools/{tool_name}", response_model=AssistantToolOut)
def update_assistant_tool_enabled(
    tool_name: str,
    body: AssistantToolEnabledUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AssistantToolOut:
    try:
        return runtime.set_builtin_tool_enabled(db, current_user.id, tool_name, body.enabled)
    except runtime.UnknownBuiltinToolError as exc:
        raise HTTPException(status_code=404, detail=f"未知内置工具 {exc}") from exc


@router.get("/conversations/{conversation_id}/pending-confirmation")
def get_pending_confirmation(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    return pending_confirmation(db, current_user, conversation_id)


def pending_confirmation(db: Session, current_user: User, conversation_id: str) -> dict:
    """恢复会话内待确认工具卡：``{"data": 卡载荷 | None}``（apiClientV2 信封）。

    载荷与 SSE ``tool_confirmation_required`` 同形；本体执行卡携带服务端
    持久确认的 ``ontologyConfirmation`` 完整展示。本体确认不可验证或已
    离开 ``prepared`` 状态时不恢复可点击卡（data=null）——approve 在服务端
    同样会被确认协议拒绝，不允许裸 confirmationId 卡复活。
    """
    conversation = _conversation(db, current_user.id, conversation_id)
    tool_run = (
        db.query(SuperAssistantToolRun)
        .filter(
            SuperAssistantToolRun.conversation_id == conversation.id,
            SuperAssistantToolRun.status == "awaiting_confirmation",
        )
        .order_by(SuperAssistantToolRun.created_at.desc())
        .first()
    )
    if tool_run is None:
        return {"data": None}
    card: dict = {
        "toolRunId": tool_run.id,
        "toolName": tool_run.tool_name,
        "serverName": "multica",
        "arguments": tool_run.arguments or {},
    }
    if tool_run.server_id:
        server = db.query(SuperAssistantMcpServer).filter(
            SuperAssistantMcpServer.id == tool_run.server_id,
            SuperAssistantMcpServer.owner_id == current_user.id,
        ).first()
        if server is not None:
            card["serverName"] = server.name
            # 与 SSE 同形：展示去掉 mcp__<server>__ 命名空间前缀的原始工具名
            prefix = f"mcp__{re.sub(r'[^a-zA-Z0-9_-]', '_', server.name)}__"
            if tool_run.tool_name.startswith(prefix):
                card["toolName"] = tool_run.tool_name[len(prefix):]
    if tool_run.tool_name == ontology_tools.ONTOLOGY_EXECUTE_TOOL:
        confirmation = ontology_tools.confirmation_display_for_run(
            db,
            owner_id=current_user.id,
            conversation_id=conversation.id,
            tool_run=tool_run,
        )
        if confirmation is None or confirmation.get("state") != "prepared":
            return {"data": None}
        card["ontologyConfirmation"] = confirmation
    if tool_run.tool_name == ontology_tools.ONTOLOGY_REGISTER_JOIN_TOOL:
        join_display = ontology_tools.join_assertion_display(tool_run)
        if join_display is None:
            return {"data": None}
        card["joinAssertion"] = join_display
    return {"data": card}
