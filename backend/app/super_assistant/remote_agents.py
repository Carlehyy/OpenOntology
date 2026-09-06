"""远程助手端点（独立子路由：router.py 已贴近架构测试行数上限）。

声明式注册的 CRUD 与连接测试；路径与主路由同前缀，鉴权由 main.py 挂载
时的 menu_guard("super_assistant") 统一声明。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import remote_agent_service
from app.super_assistant.schemas import (
    RemoteAgentCreate,
    RemoteAgentOut,
    RemoteAgentTestOut,
    RemoteAgentUpdate,
)

router = APIRouter()


@router.get("/remote-agents", response_model=list[RemoteAgentOut])
def list_remote_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[RemoteAgentOut]:
    return remote_agent_service.list_agents(db, current_user.id)


@router.post("/remote-agents", response_model=RemoteAgentOut)
def create_remote_agent(
    body: RemoteAgentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RemoteAgentOut:
    try:
        return remote_agent_service.create_agent(db, current_user.id, body)
    except remote_agent_service.RemoteAgentServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/remote-agents/{agent_id}", response_model=RemoteAgentOut)
def update_remote_agent(
    agent_id: str,
    body: RemoteAgentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RemoteAgentOut:
    try:
        return remote_agent_service.update_agent(db, current_user.id, agent_id, body)
    except remote_agent_service.RemoteAgentServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/remote-agents/{agent_id}", response_model=None, status_code=204)
def delete_remote_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    remote_agent_service.delete_agent(db, current_user.id, agent_id)


@router.post("/remote-agents/{agent_id}/test", response_model=RemoteAgentTestOut)
def test_remote_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RemoteAgentTestOut:
    return remote_agent_service.test_agent(db, current_user.id, agent_id)
