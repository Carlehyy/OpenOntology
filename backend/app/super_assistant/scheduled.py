"""定时任务 HTTP 子路由（router.py 已贴近行数上限）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.super_assistant import scheduled_service
from app.super_assistant.schemas import (
    ScheduledRunOut,
    ScheduledTaskCreate,
    ScheduledTaskOut,
    ScheduledTaskUpdate,
)

router = APIRouter()


def _http_error(exc: scheduled_service.ScheduledTaskError) -> HTTPException:
    if isinstance(exc, scheduled_service.ScheduledTaskNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/scheduled-tasks", response_model=list[ScheduledTaskOut])
def list_scheduled_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ScheduledTaskOut]:
    return scheduled_service.list_tasks(db, current_user.id)


@router.post("/scheduled-tasks", response_model=ScheduledTaskOut, status_code=status.HTTP_201_CREATED)
def create_scheduled_task(
    body: ScheduledTaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskOut:
    try:
        return scheduled_service.create_task(db, current_user.id, body)
    except scheduled_service.ScheduledTaskError as exc:
        raise _http_error(exc) from exc


@router.get("/scheduled-tasks/{task_id}", response_model=ScheduledTaskOut)
def get_scheduled_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskOut:
    try:
        return scheduled_service.get_task(db, current_user.id, task_id)
    except scheduled_service.ScheduledTaskError as exc:
        raise _http_error(exc) from exc


@router.patch("/scheduled-tasks/{task_id}", response_model=ScheduledTaskOut)
def update_scheduled_task(
    task_id: str,
    body: ScheduledTaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskOut:
    try:
        return scheduled_service.update_task(db, current_user.id, task_id, body)
    except scheduled_service.ScheduledTaskError as exc:
        raise _http_error(exc) from exc


@router.delete("/scheduled-tasks/{task_id}", status_code=204)
def delete_scheduled_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    try:
        scheduled_service.delete_task(db, current_user.id, task_id)
    except scheduled_service.ScheduledTaskError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.get("/scheduled-tasks/{task_id}/runs", response_model=list[ScheduledRunOut])
def list_scheduled_runs(
    task_id: str,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ScheduledRunOut]:
    try:
        return scheduled_service.list_runs(db, current_user.id, task_id, limit=limit)
    except scheduled_service.ScheduledTaskError as exc:
        raise _http_error(exc) from exc


@router.get("/scheduled-tasks/{task_id}/runs/{run_id}", response_model=ScheduledRunOut)
def get_scheduled_run(
    task_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ScheduledRunOut:
    try:
        return scheduled_service.get_run(db, current_user.id, task_id, run_id)
    except scheduled_service.ScheduledTaskError as exc:
        raise _http_error(exc) from exc
