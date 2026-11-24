"""Multica 原生 Web 一键直达入口：一次性票据签发与免登录中继 consume。

平台默认 profile 的「打开 Multica」点击后先领一次性票据，再经
multica-gateway 在 Multica origin 抵达 consume：服务端以中继账号 + 上游
固定验证码登录 Multica 并透传会话 cookie。邮箱、验证码与票据只存在于
部署环境变量与服务器内存，不进日志与接口响应。中继细节见 multica_sso。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth.models import User
from app.deps import get_current_user
from app.shared.config import settings
from app.super_assistant import multica_sso
from app.super_assistant.web_entry import origin


router = APIRouter(tags=["super-assistant"])
# consume 不挂平台守卫：浏览器经 multica-gateway 反代访问（带不上平台
# Bearer 头），凭证就是一次性票据本身；登录与菜单守卫在 entry 签发时完成。
sso_router = APIRouter(tags=["super-assistant"])


class MulticaEntryOut(BaseModel):
    redirect_url: str


def _web_origin() -> str:
    try:
        return origin(settings.multica_default_web_url)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Multica 工作台入口未配置") from exc


@router.post("/multica/entry", response_model=MulticaEntryOut)
def create_multica_entry(
    current_user: User = Depends(get_current_user),
) -> MulticaEntryOut:
    web_url = _web_origin()
    if not multica_sso.sso_entry_enabled():
        # 部署未提供中继账号/固定验证码时 fail-closed；前端保留原生 URL 直开
        # 兜底（Multica 自带的验证码登录页）。
        raise HTTPException(status_code=503, detail="Multica 一键进入未配置")
    ticket = multica_sso.issue_ticket(str(current_user.id))
    return MulticaEntryOut(redirect_url=f"{web_url}/sso/entry?ticket={ticket}")


@sso_router.get("/multica/sso/consume")
def consume_multica_entry(ticket: str = "") -> RedirectResponse:
    # 任何失败都 302 回 Multica 根路径（未登录会被上游送到自带登录页），
    # 与引入直达前的体验一致；成功时额外透传会话 Set-Cookie。
    redirect = RedirectResponse(url="/", status_code=302)
    if multica_sso.consume_ticket(ticket) is None:
        return redirect
    cookies = multica_sso.sign_in_session_cookies()
    if cookies is None:
        return redirect
    for cookie in cookies:
        redirect.headers.append("set-cookie", cookie)
    return redirect
