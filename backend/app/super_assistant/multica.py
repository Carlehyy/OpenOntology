"""multica 外部集成端点（独立子路由：router.py 已贴近架构测试行数上限）。

配置读写与连接测试；路径与主路由同前缀，鉴权由 main.py 挂载时的
menu_guard("super_assistant") 统一声明。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import multica_service
from app.super_assistant.multica_client import MulticaClientError
from app.super_assistant.schemas import (
    MulticaConfigOut,
    MulticaConfigUpdate,
    MulticaTestOut,
    MulticaTestRequest,
    MulticaWorkspacesOut,
)

router = APIRouter()


@router.get("/multica/config", response_model=MulticaConfigOut)
def get_multica_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaConfigOut:
    return multica_service.config_view(
        multica_service.get_config(db, current_user.id),
    )


@router.put("/multica/config", response_model=MulticaConfigOut)
def update_multica_config(
    body: MulticaConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaConfigOut:
    try:
        config = multica_service.save_config(db, current_user.id, body)
    except (MulticaClientError, multica_service.MulticaServiceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return multica_service.config_view(config)


@router.get("/multica/workspaces", response_model=MulticaWorkspacesOut)
def list_multica_workspaces(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaWorkspacesOut:
    try:
        return multica_service.list_config_workspaces(db, current_user.id)
    except multica_service.MulticaServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MulticaClientError as exc:
        # 上游 multica 实例不可达：网关侧失败，前端回落已保存工作区兜底
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/multica/test", response_model=MulticaTestOut)
def test_multica_connection(
    body: MulticaTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaTestOut:
    try:
        return multica_service.test_connection(
            db,
            current_user.id,
            base_url=body.base_url,
            token=body.token,
        )
    except MulticaClientError as exc:
        # 地址/SSRF 校验失败是请求错误；连通性失败由 service 内部转 ok=False
        raise HTTPException(status_code=400, detail=str(exc)) from exc
