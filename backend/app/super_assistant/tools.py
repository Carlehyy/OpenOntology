"""内置工具目录与用户级启停端点（独立子路由：router.py 已贴近架构测试行数上限）。

GET /tools 返回非 MCP 工具目录全景（声明/分类/条件可用性/启停状态），
PATCH /tools/{tool_name} 切换单个工具启停；路径与主路由同前缀，鉴权由
main.py 挂载时的 menu_guard("super_assistant") 统一声明。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import runtime
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
