"""WeKnora native-Web entry, SSO relay and administrator MCP directory.

Native Web owns model and endpoint editing. Endpoint discovery and bounded
initialization use private administrator APIs; credentials stay server side.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth.models import User
from app.deps import get_current_user, get_db, require_admin
from sqlalchemy.orm import Session
from app.shared.config import settings
from app.super_assistant.web_entry import origin
from app.super_assistant.weknora import connections, directory, sso
from app.super_assistant.weknora.client import WeKnoraError
from app.super_assistant.weknora.schemas import (
    WeKnoraConnectIn, WeKnoraDirectoryOut, WeKnoraInitializeIn,
)
from app.super_assistant.schemas import McpServerOut


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


def _http_error(exc: WeKnoraError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


@router.get("/weknora/mcp-endpoints", response_model=WeKnoraDirectoryOut)
def list_weknora_mcp_endpoints(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    try:
        return directory.list_directory(db, current_user.id)
    except WeKnoraError as exc:
        raise _http_error(exc) from exc


@router.post("/weknora/mcp-initialize", response_model=WeKnoraDirectoryOut)
def initialize_weknora_mcp(
    body: WeKnoraInitializeIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    try:
        return directory.initialize(db, current_user.id, recreate=body.recreate)
    except WeKnoraError as exc:
        raise _http_error(exc) from exc


@router.post("/weknora/mcp-endpoints/{endpoint_id}/connect", response_model=McpServerOut)
def connect_weknora_mcp_endpoint(
    endpoint_id: str,
    body: WeKnoraConnectIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    try:
        return connections.connect_endpoint(
            db, current_user.id, endpoint_id,
            token=body.token.get_secret_value() if body.token is not None else None,
            rotate_token=body.rotate_token,
        )
    except WeKnoraError as exc:
        raise _http_error(exc) from exc


@router.post("/weknora/mcp-endpoints/{endpoint_id}/verify", response_model=McpServerOut)
def verify_weknora_mcp_endpoint(
    endpoint_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    server = connections.get_connection(db, current_user.id, endpoint_id)
    if server is None:
        raise HTTPException(status_code=404, detail="WeKnora 连接已不存在，请先接入端点")
    try:
        return connections.verify_server(db, current_user.id, server.id)
    except WeKnoraError as exc:
        raise _http_error(exc) from exc


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
