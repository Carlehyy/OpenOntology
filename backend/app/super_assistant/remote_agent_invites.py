"""远程助手邀请端点（属主侧，独立子路由：router.py 已贴近架构测试行数上限）。

创建/列出/撤销一次性接入邀请，并支持「待使用」邀请的邀请函重发。
路径与主路由同前缀，鉴权由 main.py 挂载时的 menu_guard("super_assistant")
统一声明；兑换端点在 remote_agent_public（公开，凭邀请令牌门禁）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import remote_agent_invite_service

router = APIRouter()


@router.post("/remote-agent-invites", response_model=remote_agent_invite_service.RemoteAgentInviteCreatedOut)
def create_remote_agent_invite(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> remote_agent_invite_service.RemoteAgentInviteCreatedOut:
    try:
        return remote_agent_invite_service.create_invite(db, current_user.id)
    except remote_agent_invite_service.RemoteAgentServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/remote-agent-invites", response_model=list[remote_agent_invite_service.RemoteAgentInviteOut])
def list_remote_agent_invites(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[remote_agent_invite_service.RemoteAgentInviteOut]:
    return remote_agent_invite_service.list_invites(db, current_user.id)


@router.get(
    "/remote-agent-invites/{invite_id}/prompt",
    response_class=PlainTextResponse,
)
def get_remote_agent_invite_prompt(
    invite_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> str:
    return remote_agent_invite_service.invite_prompt(db, current_user.id, invite_id)


@router.delete("/remote-agent-invites/{invite_id}", status_code=204, response_model=None)
def revoke_remote_agent_invite(
    invite_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    remote_agent_invite_service.revoke_invite(db, current_user.id, invite_id)
