"""Kaneo 前端跳转入口与一键免登录中继。

Kaneo 的原生 Web 负责项目、任务与工作区配置；平台只暴露经过 origin
校验的公开 Web 地址（nginx HTTPS 入口），不读取项目/任务数据。一键
免登录（共享管理员会话，2026-11-11 决策）：entry 签发一次性票据，
consume 经 kaneo-gateway 反代抵达后服务端登录 Kaneo 并透传会话
cookie；管理员凭据只存在于部署环境变量（backend 容器），仅用于生成
会话，不进日志与接口响应。
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth.models import User
from app.deps import get_current_user
from app.shared.config import settings
from app.super_assistant.kaneo import sso
from app.super_assistant.web_entry import origin


router = APIRouter(tags=["super-assistant"])
# consume 不挂平台守卫：浏览器经 Kaneo origin 的网关反代访问（带不上平台
# Bearer 头），凭证就是一次性票据本身；登录与菜单守卫在 entry 签发时完成。
sso_router = APIRouter(tags=["super-assistant"])


class KaneoIntegrationOut(BaseModel):
    web_url: str


class KaneoEntryOut(BaseModel):
    redirect_url: str


@router.get("/kaneo", response_model=KaneoIntegrationOut)
def read_kaneo_integration(
    current_user: User = Depends(get_current_user),
) -> KaneoIntegrationOut:
    # 复用 WeKnora 的 origin 归一化校验：两个集成的入口 URL 语义一致，
    # 共享实现避免出现两套平行的 URL 判定规则。
    try:
        web_url = origin(settings.kaneo_web_url)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="项目任务服务尚未就绪") from exc
    return KaneoIntegrationOut(web_url=web_url)


@router.post("/kaneo/entry", response_model=KaneoEntryOut)
def create_kaneo_entry(
    current_user: User = Depends(get_current_user),
) -> KaneoEntryOut:
    try:
        web_url = origin(settings.kaneo_web_url)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="项目任务服务尚未就绪") from exc
    if not settings.kaneo_admin_email.strip() or not settings.kaneo_admin_password:
        # 部署未提供中继凭据（如绕过 deploy 脚本裸跑 compose）时 fail-closed；
        # 前端保留原生 URL 直开兜底（Kaneo 自己的登录页）。
        raise HTTPException(status_code=503, detail="项目任务单点登录未配置")
    ticket = sso.issue_ticket(str(current_user.id))
    return KaneoEntryOut(redirect_url=f"{web_url}/sso/entry?ticket={ticket}")


@sso_router.get("/kaneo/sso/consume")
def consume_kaneo_entry(ticket: str = "") -> RedirectResponse:
    # 任何失败都 302 回 Kaneo 根路径（未登录会被上游送到自带登录页），
    # 与引入免登录前的手动登录体验一致；成功时额外透传会话 Set-Cookie。
    redirect = RedirectResponse(url="/", status_code=302)
    if sso.consume_ticket(ticket) is None:
        return redirect
    cookies = sso.sign_in_session_cookies()
    if cookies is None:
        return redirect
    for cookie in cookies:
        redirect.headers.append("set-cookie", cookie)
    return redirect
