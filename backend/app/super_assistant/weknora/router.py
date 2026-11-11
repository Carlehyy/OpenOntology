"""WeKnora 前端跳转入口与一键免登录中继。

WeKnora 的原生 Web 负责模型、知识库与 MCP endpoint 配置；平台只暴露经过
origin 校验的公开 Web 地址，不读取本地 state、知识库或 MCP 清单。一键
免登录（共享管理员会话，与 Kaneo 中继同构）：entry 签发一次性票据，
consume 经 weknora-gateway 反代抵达后服务端登录 WeKnora，把登录响应经
其原生 ``#oidc_result=`` 管道交给 WeKnora 前端自持久化；管理员凭据只
存在于部署环境变量（backend 容器），仅用于换取会话，不进日志与接口响应。
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth.models import User
from app.deps import get_current_user
from app.shared.config import settings
from app.super_assistant.web_entry import origin
from app.super_assistant.weknora import sso


router = APIRouter(tags=["super-assistant"])
# consume 不挂平台守卫：浏览器经 WeKnora origin 的网关反代访问（带不上平台
# Bearer 头），凭证就是一次性票据本身；登录与菜单守卫在 entry 签发时完成。
sso_router = APIRouter(tags=["super-assistant"])


class WeKnoraIntegrationOut(BaseModel):
    web_url: str


class WeKnoraEntryOut(BaseModel):
    redirect_url: str


@router.get("/weknora", response_model=WeKnoraIntegrationOut)
def read_weknora_integration(
    current_user: User = Depends(get_current_user),
) -> WeKnoraIntegrationOut:
    try:
        web_url = origin(settings.weknora_web_url)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="知识图谱服务尚未就绪") from exc
    return WeKnoraIntegrationOut(web_url=web_url)


@router.post("/weknora/entry", response_model=WeKnoraEntryOut)
def create_weknora_entry(
    current_user: User = Depends(get_current_user),
) -> WeKnoraEntryOut:
    try:
        web_url = origin(settings.weknora_web_url)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="知识图谱服务尚未就绪") from exc
    if not settings.weknora_admin_email.strip() or not settings.weknora_admin_password:
        # 部署未提供中继凭据（如绕过 deploy 脚本裸跑 compose）时 fail-closed；
        # 前端保留原生 URL 直开兜底（WeKnora 自己的登录页）。
        raise HTTPException(status_code=503, detail="领域知识单点登录未配置")
    ticket = sso.issue_ticket(str(current_user.id))
    return WeKnoraEntryOut(redirect_url=f"{web_url}/sso/entry?ticket={ticket}")


@sso_router.get("/weknora/sso/consume")
def consume_weknora_entry(ticket: str = "") -> RedirectResponse:
    # 任何失败都 302 回 WeKnora 根路径（未登录会被其路由守卫送到自带登录
    # 页），与引入免登录前的手动登录体验一致；成功时 302 到原生会话落盘
    # 管道。令牌只存在于 URL fragment，响应钉 no-store 防中间层缓存。
    redirect = RedirectResponse(url="/", status_code=302)
    redirect.headers["cache-control"] = "no-store"
    if sso.consume_ticket(ticket) is None:
        return redirect
    fragment = sso.sign_in_handoff_fragment()
    if fragment is None:
        return redirect
    redirect = RedirectResponse(url=f"/#oidc_result={fragment}", status_code=302)
    redirect.headers["cache-control"] = "no-store"
    return redirect
