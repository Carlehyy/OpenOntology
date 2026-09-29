"""Pipeline Task creation, mutation, deletion, and scheduler refresh."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.pipeline_tasks import cache as _cache
from app.data_channel.pipeline_tasks.models import PipelineTask
from app.data_channel.pipeline_tasks.validation_service import (
    ensure_pipeline_ready_for_enable,
)
from app.data_channel.pipelines.models import Pipeline

logger = logging.getLogger(__name__)


LifecycleDependency = Callable[..., Any]


def _refresh_scheduler(task_id: str) -> dict | None:
    """CRUD 落库后刷新调度器，并把调度不可用带回调用方在响应中呈现。

    过去这里 ``except: pass``：任务已保存但调度未生效时用户无从得知，
    调度与数据库状态静默漂移。返回 None 表示调用方注入的刷新函数不带
    状态（兼容旧注入桩），不附加调度状态到响应。
    """
    try:
        from app.data_channel.sync_tasks.scheduler import (
            get_sync_scheduler,
        )

        scheduler = get_sync_scheduler()
        if not scheduler.started:
            logger.warning(
                "PipelineTask %s 已保存，但调度器未启动，本次变更未注册调度",
                task_id)
            return {"status": "not_started"}
        if scheduler.reload_pipeline_task(task_id):
            return {"status": "ok"}
        logger.warning(
            "PipelineTask %s 已保存，但调度注册失败（详见调度器日志）", task_id)
        return {"status": "failed"}
    except Exception as exc:  # noqa: BLE001 — 状态带回响应，保存结果不被掩盖
        logger.warning("PipelineTask %s 调度刷新异常: %s", task_id, exc)
        return {"status": "failed", "error": str(exc)}


def create_task(
    body: Any,
    db: Session,
    current_user: Any,
    *,
    validate_fn: LifecycleDependency,
    refresh_scheduler_fn: LifecycleDependency,
    with_pipeline_info_fn: LifecycleDependency,
) -> dict:
    _, pipeline_primary_key = validate_fn(db, body)
    task = PipelineTask(
        id=str(uuid.uuid4()),
        name=body.name,
        description=body.description,
        pipeline_id=body.pipeline_id,
        write_mode=body.write_mode,
        primary_key=pipeline_primary_key,
        soft_delete_column=(
            body.soft_delete_column or ""
        ).strip(),
        cursor_column=(body.cursor_column or "").strip(),
        skip_empty=body.skip_empty,
        schedule_type=body.schedule_type,
        cron_expression=body.cron_expression or "",
        interval_seconds=body.interval_seconds or 0,
        enabled=body.enabled,
        status="idle",
        created_by=getattr(current_user, "id", None),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    refresh_result = refresh_scheduler_fn(task.id)
    _cache.invalidate_all()
    payload = with_pipeline_info_fn(db, [task])[0]
    if isinstance(refresh_result, dict):
        payload["scheduler_refresh"] = refresh_result
    return payload


def update_task(
    task_id: str,
    body: Any,
    db: Session,
    *,
    validate_fn: LifecycleDependency,
    refresh_scheduler_fn: LifecycleDependency,
    with_pipeline_info_fn: LifecycleDependency,
) -> dict:
    task = (
        db.query(PipelineTask)
        .filter(PipelineTask.id == task_id)
        .first()
    )
    if not task:
        raise HTTPException(404, "PipelineTask not found")
    _, pipeline_primary_key = validate_fn(
        db,
        body,
        existing=task,
    )
    payload = body.model_dump(exclude_unset=True)
    # 停用 → 启用走与启停开关相同的流水线校验（409）；已启用任务的
    # 改名/改调度不受影响。校验作用于保存后生效的流水线（换绑场景
    # validation 已按新流水线把过关）。
    effective_pipeline_id = payload.get("pipeline_id", task.pipeline_id)
    if not task.enabled and payload.get("enabled", task.enabled):
        ensure_pipeline_ready_for_enable(db, effective_pipeline_id)
    previous_cursor_column = task.cursor_column or ""
    for field, value in payload.items():
        if field == "primary_key":
            continue
        setattr(task, field, value)
    # 游标列被修改/清空时，旧水位对新列不再可比较，必须归零——下次运行
    # 按全量重建水位，避免沿用旧列的水位静默漏数
    if (task.cursor_column or "") != previous_cursor_column:
        task.last_cursor_value = ""
    # 兼容字段只保留当前发布契约快照，修复历史任务可能存在的自定义值。
    task.primary_key = pipeline_primary_key
    task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    refresh_result = refresh_scheduler_fn(task.id)
    _cache.invalidate_all()
    payload = with_pipeline_info_fn(db, [task])[0]
    if isinstance(refresh_result, dict):
        payload["scheduler_refresh"] = refresh_result
    return payload


def delete_task(
    task_id: str,
    db: Session,
    *,
    refresh_scheduler_fn: LifecycleDependency,
) -> dict:
    task = (
        db.query(PipelineTask)
        .filter(PipelineTask.id == task_id)
        .first()
    )
    if not task:
        raise HTTPException(404, "PipelineTask not found")
    db.delete(task)
    db.commit()
    refresh_result = refresh_scheduler_fn(task_id)
    _cache.invalidate_all()
    result = {"status": "ok"}
    if isinstance(refresh_result, dict):
        result["scheduler_refresh"] = refresh_result
    return result


def toggle_task(
    task_id: str,
    enabled: bool,
    db: Session,
    *,
    refresh_scheduler_fn: LifecycleDependency,
) -> dict:
    task = (
        db.query(PipelineTask)
        .filter(PipelineTask.id == task_id)
        .first()
    )
    if not task:
        raise HTTPException(404, "PipelineTask not found")
    if enabled:
        pipeline = (
            db.query(Pipeline)
            .filter(Pipeline.id == task.pipeline_id)
            .first()
        )
        if (
            not pipeline
            or (pipeline.status or "draft") != "published"
            or pipeline.enabled is False
        ):
            raise HTTPException(
                409,
                "关联流水线未发布或已停用，不能启用该调度任务",
            )
    task.enabled = enabled
    task.updated_at = datetime.utcnow()
    db.commit()
    refresh_result = refresh_scheduler_fn(task.id)
    _cache.invalidate_all()
    payload = task.to_dict()
    if isinstance(refresh_result, dict):
        payload["scheduler_refresh"] = refresh_result
    return payload
