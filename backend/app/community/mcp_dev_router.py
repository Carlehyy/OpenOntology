"""开放社区 · 自研 MCP（开发 MCP）路由。

挂在 /api/v2/community 前缀下（与 mcp-servers 系列同菜单边界
community.plugins），业务实现全部委托 super_assistant.mcp_dev_service
——与 mcp_export 同一依赖方向先例：community 是协议适配层。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import mcp_dev_service
from app.super_assistant import schemas as sa_schemas


router = APIRouter()


def _http_error(exc: mcp_dev_service.McpDevServiceError) -> HTTPException:
    if isinstance(exc, mcp_dev_service.McpDevNotFoundError):
        status_code = 404
    elif isinstance(exc, mcp_dev_service.McpDevConflictError):
        status_code = 409
    elif isinstance(exc, mcp_dev_service.McpDevUnavailableError):
        status_code = 502
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=str(exc))


@router.get("/mcp-dev/projects")
def list_projects(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[sa_schemas.McpDevProjectOut]:
    try:
        return mcp_dev_service.list_projects(db, current_user.id)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc


@router.post("/mcp-dev/projects", status_code=201)
def create_project(
    body: sa_schemas.McpDevProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> sa_schemas.McpDevProjectDetailOut:
    try:
        return mcp_dev_service.create_project(db, current_user.id, body)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/mcp-dev/projects/{project_id}")
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> sa_schemas.McpDevProjectDetailOut:
    try:
        return mcp_dev_service.get_project(db, current_user.id, project_id)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc


@router.patch("/mcp-dev/projects/{project_id}")
def update_project(
    project_id: str,
    body: sa_schemas.McpDevProjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> sa_schemas.McpDevProjectDetailOut:
    try:
        return mcp_dev_service.update_project(db, current_user.id, project_id, body)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc


@router.delete("/mcp-dev/projects/{project_id}", status_code=204)
def remove_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    try:
        mcp_dev_service.remove_project(db, current_user.id, project_id)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.post("/mcp-dev/projects/{project_id}/execute")
def execute_project(
    project_id: str,
    body: sa_schemas.McpDevExecuteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> sa_schemas.McpDevExecuteOut:
    """试跑：tool_name 为空时解析工具清单，否则以入参调用该工具。"""
    try:
        return mcp_dev_service.execute_project(db, current_user.id, project_id, body)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc


@router.post("/mcp-dev/projects/{project_id}/save")
def save_project(
    project_id: str,
    body: sa_schemas.McpDevSaveIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> sa_schemas.McpDevSaveOut:
    """保存脚本：服务端复核通过后冻结版本（保留 20 版）。"""
    try:
        return mcp_dev_service.save_project(db, current_user.id, project_id, body)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/mcp-dev/projects/{project_id}/versions")
def list_versions(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[sa_schemas.McpDevVersionOut]:
    try:
        return mcp_dev_service.list_versions(db, current_user.id, project_id)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/mcp-dev/projects/{project_id}/versions/{version_no}")
def get_version(
    project_id: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> sa_schemas.McpDevVersionDetailOut:
    try:
        return mcp_dev_service.get_version(db, current_user.id, project_id, version_no)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc


@router.post("/mcp-dev/projects/{project_id}/publish")
def publish_project(
    project_id: str,
    body: sa_schemas.McpDevPublishIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> sa_schemas.McpDevPublishOut:
    """发布为 MCP：逐工具样例真实执行 + 描述完备性闸门，通过后固化入库。"""
    try:
        return mcp_dev_service.publish_project(db, current_user.id, project_id, body)
    except mcp_dev_service.McpDevServiceError as exc:
        raise _http_error(exc) from exc
