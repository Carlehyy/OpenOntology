"""跨会话记忆管理路由（挂载于 /api/v2/super-assistant 主前缀下）。

全部端点按当前用户 owner 鉴权（bank 跨会话共享，但来源/设置/状态查询
一律过滤 owner）。写路径 commit 后按行内 dispatch_id 代次派发 NATS 加速
推进；派发失败由 memory_tasks.drain_once 补发兜底。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.model_configs.models import ModelConfig
from app.super_assistant import hindsight_memory
from app.super_assistant.memory_models import SuperAssistantMemorySetting
from app.super_assistant.memory_schemas import (
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


@router.get("/memory/settings", response_model=MemorySettingsOut)
def get_memory_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    setting = hindsight_memory.get_settings(db, current_user.id)
    return MemorySettingsOut(
        enabled=setting.enabled, model_config_id=setting.model_config_id,
    )


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
    return MemorySettingsOut(
        enabled=setting.enabled, model_config_id=setting.model_config_id,
    )


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
