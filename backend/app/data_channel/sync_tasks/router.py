"""V2 数据同步任务（DataSyncTask）路由——旧体系已整体退役。

读写入口一律返回 410 并指引迁移数据任务池（PipelineTask）；仅保留
DELETE 供清理历史遗留的旧任务记录。按退役纪律先保留路由观察一个
发布周期，确认无外部调用方后再删路由与 OpenAPI 条目。
"""
from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.data_channel.sync_tasks.models import DataSyncTask

router = APIRouter(dependencies=[Depends(get_current_user)])

_RETIRED_DETAIL = (
    "旧版 DataSyncTask 已停用。请先发布并启用 n8n 流水线，"
    "再到数据任务池创建 PipelineTask；存量任务仅保留只读审计。"
)
_RETIRED_READ_DETAIL = (
    "旧版 DataSyncTask 查询接口已停用；任务调度与运行历史"
    "请使用数据任务池（/api/v2/pipeline-tasks）。"
)


def _reject_retired_write() -> None:
    raise HTTPException(status_code=410, detail=_RETIRED_DETAIL)


def _reject_retired_read() -> None:
    raise HTTPException(status_code=410, detail=_RETIRED_READ_DETAIL)


def _refresh_scheduler(task_id: str) -> None:
    # DELETE 旧任务后摘除调度器内存里可能残留的旧 Job（幂等，摘不到为准）
    try:
        from app.data_channel.sync_tasks.scheduler import get_sync_scheduler
        get_sync_scheduler().reload_task(task_id)
    except Exception:
        pass


class SyncTaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = ""
    connection_id: str
    source_table: Optional[str] = ""
    source_query: Optional[str] = ""
    sync_mode: Literal["SNAPSHOT", "APPEND"] = "SNAPSHOT"
    primary_key: Optional[str] = ""
    watermark_column: Optional[str] = ""
    is_deleted_column: Optional[str] = ""
    schedule_type: Literal["MANUAL", "CRON", "INTERVAL"] = "MANUAL"
    cron_expression: Optional[str] = ""
    interval_seconds: Optional[int] = 0
    enabled: bool = True
    trigger_pipeline_id: Optional[str] = ""


class SyncTaskUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    source_table: Optional[str] = None
    source_query: Optional[str] = None
    sync_mode: Optional[Literal["SNAPSHOT", "APPEND"]] = None
    primary_key: Optional[str] = None
    watermark_column: Optional[str] = None
    is_deleted_column: Optional[str] = None
    schedule_type: Optional[Literal["MANUAL", "CRON", "INTERVAL"]] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    enabled: Optional[bool] = None
    trigger_pipeline_id: Optional[str] = None


# ========== 固定路径（必须放在 /{task_id} 之前） ==========


@router.get("/stats")
def stats_overview():
    _reject_retired_read()


@router.get("/scheduler/status")
def scheduler_status():
    _reject_retired_read()


@router.get("/sources/{conn_id}/tables")
def list_source_tables(conn_id: str):
    _reject_retired_read()


@router.get("/sources/{conn_id}/tables/{table}/sample")
def preview_source_table(conn_id: str, table: str):
    _reject_retired_read()


# ========== CRUD（读写入口均已退役为 410） ==========


@router.post("", status_code=201)
def create_task(body: SyncTaskCreate, db: Session = Depends(get_db)):
    _reject_retired_write()


@router.get("")
def list_tasks():
    _reject_retired_read()


@router.get("/{task_id}")
def get_task(task_id: str):
    _reject_retired_read()


@router.put("/{task_id}")
def update_task(task_id: str, body: SyncTaskUpdate, db: Session = Depends(get_db)):
    _reject_retired_write()


@router.delete("/{task_id}")
def delete_task(task_id: str, db: Session = Depends(get_db)):
    """删除历史遗留的旧任务记录（旧写入口已 410，仅剩清理用途）。"""
    task = db.query(DataSyncTask).filter(DataSyncTask.id == task_id).first()
    if not task:
        raise HTTPException(404, "SyncTask not found")
    db.delete(task)
    db.commit()
    _refresh_scheduler(task_id)
    return {"status": "ok"}


@router.post("/{task_id}/toggle")
def toggle_task(task_id: str, enabled: bool, db: Session = Depends(get_db)):
    if enabled:
        _reject_retired_write()
    task = db.query(DataSyncTask).filter(DataSyncTask.id == task_id).first()
    if not task:
        raise HTTPException(404, "SyncTask not found")
    task.enabled = enabled
    task.updated_at = datetime.utcnow()
    db.commit()
    _refresh_scheduler(task.id)
    return task.to_dict()


@router.post("/{task_id}/trigger")
def trigger_task(
    task_id: str,
    background: BackgroundTasks,
    sync: bool = False,
    db: Session = Depends(get_db),
):
    _reject_retired_write()


@router.get("/{task_id}/histories")
def list_histories(task_id: str):
    _reject_retired_read()
