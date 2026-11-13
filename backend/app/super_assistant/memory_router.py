"""跨会话记忆管理路由（挂载于 /api/v2/super-assistant 主前缀下）。

全部端点按当前用户 owner 鉴权（bank 跨会话共享，但来源/设置/状态查询
一律过滤 owner）。写路径 commit 后按行内 dispatch_id 代次派发 NATS 加速
推进；派发失败由 memory_tasks.drain_once 补发兜底。
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.model_configs.models import ModelConfig
from app.shared.config import settings as platform_settings
from app.super_assistant import hindsight_memory, hindsight_sso
from app.super_assistant.memory_models import SuperAssistantMemorySetting
from app.super_assistant.memory_schemas import (
    MemoryConsoleEntryOut,
    MemorySearchOut,
    MemorySearchRequest,
    MemorySettingsOut,
    MemorySettingsUpdate,
    MemoryStatusOut,
    RecallItemOut,
    SourceCreate,
    SourceListOut,
    SourceOut,
)

logger = logging.getLogger(__name__)

router = APIRouter()
# 控制面一键免密中继（/memory/console/sso）：浏览器以顶层导航抵达，带不上
# 平台 Bearer 头——有意不挂 assistant_guard，凭证是 entry 签发的一次性票据
# （HMAC + 90s TTL + Redis 单次消费），管理员边界在签发时已校验。
console_router = APIRouter()

# 中继路径相对本路由的挂载前缀（main.py 固定 /api/v2/super-assistant）；
# entry 需要返回可直接导航的绝对路径，故在此展开一次。
_CONSOLE_RELAY_PATH = "/memory/console/sso"
_API_PREFIX = "/api/v2/super-assistant"

# 初期仅允许 openai / openai 兼容线
_ALLOWED_PROVIDERS = {"openai", "compatible"}


def _dispatch(source_id: str, dispatch_id: str) -> None:
    """commit 后按当前代次令牌尽力派发；失败不阻断（drain_once 兜底）。"""
    try:
        from app.data_channel.pipeline_tasks.dispatch import (
            dispatch_super_assistant_memory_sync,
        )

        dispatch_super_assistant_memory_sync(source_id, dispatch_id)
    except Exception:
        logger.warning("记忆同步派发失败（source=%s），等待对账补发", source_id)


def _validate_model_config(db: Session, model_config_id: str) -> ModelConfig:
    """显式指定的记忆模型必须合法且启用；失效直接 400，不回退默认。"""
    config = db.get(ModelConfig, model_config_id)
    if config is None or config.config_type != "llm":
        raise HTTPException(status_code=400, detail="模型配置不存在或不是 LLM")
    if not config.enabled:
        raise HTTPException(status_code=400, detail="模型配置未启用")
    if str(config.provider or "").lower() not in _ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail="记忆模型初期仅支持 openai / openai 兼容线",
        )
    from app.super_assistant import memory_llm

    result = memory_llm.validate_memory_model(db, config.id)
    if not isinstance(result, dict) or not result.get("ok"):
        message = (
            str(result.get("message") or "记忆模型能力测试未通过")
            if isinstance(result, dict) else "记忆模型能力测试未通过"
        )
        raise HTTPException(status_code=400, detail=message[:300])
    return config


def _strict_web_origin(configured: str) -> str | None:
    """归一化部署级 Web 入口；任何非 origin 形态返回 None（不静默纠正）。

    控制面地址由部署推导/运维填写，须是 scheme://host[:port]：带路径、查询、
    凭据、空白、控制字符或反斜杠一律拒绝，避免把非入口形态下发给浏览器。
    """
    try:
        parsed = urlsplit(configured)
        port = parsed.port
        if (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and parsed.path in {"", "/"} and "?" not in configured and "#" not in configured
            and not any(
                char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
                for char in configured
            )
        ):
            host = parsed.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            suffix = f":{port}" if port is not None else ""
            return f"{parsed.scheme}://{host}{suffix}"
    except ValueError:
        pass
    return None


def _memory_settings_out(
    setting: SuperAssistantMemorySetting, current_user: User,
) -> MemorySettingsOut:
    web_url = None
    # 原生控制台是部署级管理入口，不沿用用户级 bank 的隔离边界。
    # 只向管理员返回独立配置的地址，不推导内网端口，也不附带服务凭据。
    configured = platform_settings.hindsight_web_url
    if current_user.role == "admin" and configured:
        web_url = _strict_web_origin(configured)
    return MemorySettingsOut(
        enabled=setting.enabled, model_config_id=setting.model_config_id,
        web_url=web_url,
    )


@router.get("/memory/settings", response_model=MemorySettingsOut)
def get_memory_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    setting = hindsight_memory.get_settings(db, current_user.id)
    return _memory_settings_out(setting, current_user)


@router.patch("/memory/settings", response_model=MemorySettingsOut)
def update_memory_settings(
    body: MemorySettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    setting = db.get(SuperAssistantMemorySetting, current_user.id)
    if setting is None:
        setting = SuperAssistantMemorySetting(owner_id=current_user.id)
        db.add(setting)
    if body.enabled is not None:
        setting.enabled = body.enabled
    # model_config_id 显式传 null 表示清空选择；未传字段不覆盖
    if "model_config_id" in body.model_fields_set:
        if body.model_config_id:
            config = _validate_model_config(db, body.model_config_id)
            setting.model_config_id = config.id
        else:
            setting.model_config_id = None
    db.commit()
    db.refresh(setting)
    return _memory_settings_out(setting, current_user)


@router.post("/memory/console/entry", response_model=MemoryConsoleEntryOut)
def create_memory_console_entry(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MemoryConsoleEntryOut:
    """签发一次性票据，返回平台 origin 下的控制面免密中继地址。

    管理员边界在这里校验；中继本身无平台守卫（浏览器顶层导航带不上 Bearer
    头），票据即凭证。任一前置条件不满足都 fail-closed，前端保留"原生地址
    直开 + 控制台密钥登录"的手动兜底。
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问记忆控制台")
    web_url = _strict_web_origin(platform_settings.hindsight_web_url)
    if web_url is None:
        raise HTTPException(status_code=503, detail="记忆控制台入口未配置")
    if not hindsight_sso.relay_ready():
        raise HTTPException(status_code=503, detail="记忆控制台单点登录未配置")
    # 会话 cookie 按主机存储（忽略端口）：只有平台与控制面同主机时，中继
    # 下发的 cookie 才会被控制面接收。平台改用域名访问时退回手动密钥登录。
    console_host = urlsplit(web_url).hostname or ""
    if (request.url.hostname or "").lower() != console_host:
        raise HTTPException(
            status_code=503,
            detail="记忆控制台与当前访问地址不同源，请改用访问密钥登录",
        )
    ticket = hindsight_sso.issue_ticket(str(current_user.id))
    return MemoryConsoleEntryOut(
        redirect_url=f"{_API_PREFIX}{_CONSOLE_RELAY_PATH}?ticket={ticket}",
    )


@console_router.get(_CONSOLE_RELAY_PATH)
def consume_memory_console_entry(ticket: str = "") -> RedirectResponse:
    """校验票据并在服务端登录控制面，把会话 cookie 透传给浏览器。

    任何失败都 302 回控制面根地址且不种 cookie，落到其自带密钥登录页；
    响应钉 no-store，密钥与票据都不进响应体与日志。
    """
    web_url = _strict_web_origin(platform_settings.hindsight_web_url)
    redirect = RedirectResponse(url=web_url or "/", status_code=302)
    redirect.headers["cache-control"] = "no-store"
    if web_url is None or hindsight_sso.consume_ticket(ticket) is None:
        return redirect
    cookies = hindsight_sso.sign_in_session_cookies()
    if cookies is None:
        return redirect
    for cookie in cookies:
        redirect.headers.append("set-cookie", cookie)
    return redirect


@router.get("/memory/status", response_model=MemoryStatusOut)
def get_memory_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return MemoryStatusOut(**hindsight_memory.memory_status(db, current_user.id))


@router.get("/memory/sources", response_model=SourceListOut)
def list_memory_sources(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    query: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, total = hindsight_memory.list_sources(
        db, current_user.id, limit=limit, offset=offset, query=query,
    )
    return SourceListOut(
        items=[SourceOut.model_validate(item) for item in items], total=total,
    )


@router.post("/memory/search", response_model=MemorySearchOut)
def search_memories(
    body: MemorySearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = hindsight_memory.search_memories(db, current_user.id, body.query)
    return MemorySearchOut(
        state=result["state"],
        message=result.get("message"),
        items=[RecallItemOut(**item) for item in result.get("items") or []],
    )


@router.post("/memory/sources", response_model=SourceOut, status_code=201)
def create_memory_source(
    body: SourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        source = hindsight_memory.create_manual_source(
            db,
            owner_id=current_user.id,
            content=body.content,
            correction_of=body.correction_of,
        )
    except hindsight_memory.MemorySourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(source)
    _dispatch(source.id, source.dispatch_id)
    return SourceOut.model_validate(source)


@router.delete("/memory/sources/{source_id}", response_model=SourceOut)
def delete_memory_source(
    source_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    source = hindsight_memory.forget_source(db, current_user.id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="记忆来源不存在")
    db.commit()
    db.refresh(source)
    _dispatch(source.id, source.dispatch_id)
    return SourceOut.model_validate(source)


@router.post("/memory/sources/{source_id}/retry", response_model=SourceOut)
def retry_memory_source(
    source_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        source = hindsight_memory.retry_source(db, current_user.id, source_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=404, detail="记忆来源不存在")
    db.commit()
    db.refresh(source)
    _dispatch(source.id, source.dispatch_id)
    return SourceOut.model_validate(source)
