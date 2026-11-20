"""multica 外部集成端点（独立子路由：router.py 已贴近架构测试行数上限）。

配置读写与连接测试；路径与主路由同前缀，鉴权由 main.py 挂载时的
menu_guard("super_assistant") 统一声明。多 profile 端点与旧四端点并存：
旧 /multica/config|test|workspaces 契约不变（统一读写生效 profile），
/multica/profiles* 为显式多 profile 管理面。路由层只做错误
码翻译，全部业务/事务归 multica_service。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import multica_service
from app.super_assistant.multica_client import MulticaClientError
from app.super_assistant.schemas import (
    MulticaConfigOut,
    MulticaConfigUpdate,
    MulticaProfileCreate,
    MulticaProfileOut,
    MulticaProfileTestRequest,
    MulticaProfileUpdate,
    MulticaProfilesOut,
    MulticaTestOut,
    MulticaTestRequest,
    MulticaWorkspacesOut,
)

router = APIRouter()


def _profile_http_error(exc: Exception) -> HTTPException:
    """服务层错误 → HTTP 状态码：缺失 404、冲突 409、其余请求错误 400。"""
    if isinstance(exc, multica_service.MulticaProfileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, multica_service.MulticaProfileConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/multica/config", response_model=MulticaConfigOut)
def get_multica_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaConfigOut:
    return multica_service.read_config(db, current_user.id)


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


@router.get("/multica/profiles", response_model=MulticaProfilesOut)
def list_multica_profiles(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaProfilesOut:
    try:
        return multica_service.list_profiles(db, current_user.id)
    except multica_service.MulticaServiceError as exc:
        raise _profile_http_error(exc) from exc


@router.post("/multica/profiles", response_model=MulticaProfileOut, status_code=201)
def create_multica_profile(
    body: MulticaProfileCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaProfileOut:
    try:
        return multica_service.create_profile(db, current_user.id, body)
    except multica_service.MulticaServiceError as exc:
        raise _profile_http_error(exc) from exc
    except MulticaClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/multica/profiles/{profile_id}", response_model=MulticaProfileOut)
def update_multica_profile(
    profile_id: str,
    body: MulticaProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaProfileOut:
    try:
        return multica_service.update_profile(db, current_user.id, profile_id, body)
    except multica_service.MulticaServiceError as exc:
        raise _profile_http_error(exc) from exc
    except MulticaClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/multica/profiles/{profile_id}/activate", response_model=MulticaProfileOut)
def activate_multica_profile(
    profile_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaProfileOut:
    try:
        return multica_service.activate_profile(db, current_user.id, profile_id)
    except multica_service.MulticaServiceError as exc:
        raise _profile_http_error(exc) from exc


@router.delete("/multica/profiles/{profile_id}", status_code=204)
def delete_multica_profile(
    profile_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    try:
        multica_service.delete_profile(db, current_user.id, profile_id)
    except multica_service.MulticaServiceError as exc:
        raise _profile_http_error(exc) from exc
    return Response(status_code=204)


@router.post("/multica/profiles/{profile_id}/test", response_model=MulticaTestOut)
def test_multica_profile(
    profile_id: str,
    body: MulticaProfileTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaTestOut:
    try:
        return multica_service.test_profile(
            db, current_user.id, profile_id,
            base_url=body.base_url, token=body.token,
        )
    except multica_service.MulticaServiceError as exc:
        raise _profile_http_error(exc) from exc
    except MulticaClientError as exc:
        # 地址/SSRF 校验失败是请求错误；连通性失败由 service 内部转 ok=False
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/multica/profiles/{profile_id}/workspaces", response_model=MulticaWorkspacesOut)
def list_multica_profile_workspaces(
    profile_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MulticaWorkspacesOut:
    try:
        return multica_service.list_profile_workspaces(db, current_user.id, profile_id)
    except multica_service.MulticaServiceError as exc:
        raise _profile_http_error(exc) from exc
    except MulticaClientError as exc:
        # 与 GET /multica/workspaces 同口径：上游不可达按网关失败 502
        raise HTTPException(status_code=502, detail=str(exc)) from exc
