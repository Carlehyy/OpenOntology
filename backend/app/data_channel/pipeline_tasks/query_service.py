"""Pipeline Task catalog, list enrichment, and operational statistics."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.data_channel.pipeline_tasks.models import PipelineTask
from app.data_channel.pipeline_tasks.selection_service import (  # noqa: F401
    _curated_columns,
    selectable_pipelines,
)
from app.data_channel.pipelines.models import Pipeline, PipelineRun

logger = logging.getLogger(__name__)

QueryDependency = Callable[..., Any]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# 列表页 10s 级轮询的降级路径：意外异常每个进程只记一次，避免刷屏。
# 调度器未启动等预期降级走正常返回值，不在此列。
_LOGGED_DEGRADATIONS: set[str] = set()


def _log_degradation_once(site: str, exc: Exception) -> None:
    if site in _LOGGED_DEGRADATIONS:
        return
    _LOGGED_DEGRADATIONS.add(site)
    logger.warning(
        "%s 降级（本进程仅记录一次）: %s", site, exc, exc_info=True)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """数据库裸时间统一按 UTC 解释；带时区值统一转换为 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _shanghai_day_start_utc(local_day) -> datetime:
    """上海自然日零点转换为数据库使用的 UTC naive 边界。"""
    local_start = datetime.combine(
        local_day,
        datetime.min.time(),
        tzinfo=SHANGHAI_TZ,
    )
    return local_start.astimezone(timezone.utc).replace(tzinfo=None)


def _shanghai_date(value: datetime):
    return _as_utc(value).astimezone(SHANGHAI_TZ).date()


def _live_next_run_map(task_ids: list[str]) -> dict[str, str]:
    """优先取调度器里 Job 的真实 next_run_time（最准，反映 misfire/coalesce 等）。"""
    output: dict[str, str] = {}
    try:
        from app.data_channel.sync_tasks.scheduler import (
            get_sync_scheduler,
        )

        scheduler = get_sync_scheduler()
        if not scheduler.started:
            return output
        for task_id in task_ids:
            job = scheduler.scheduler.get_job(
                f"pipe_task:{task_id}"
            )
            if job and job.next_run_time:
                output[task_id] = job.next_run_time.isoformat()
    except Exception as exc:
        # 调度器未启动走上方正常返回；到这里的是意外异常（导入失败等）
        _log_degradation_once("live_next_run_map", exc)
    return output


def _computed_next_run(task) -> str | None:
    """Compute a fallback next run when the scheduler has no live job."""
    if not task.enabled:
        return None
    from datetime import timedelta, timezone as utc_timezone

    # INTERVAL：last_run + 间隔；从未运行则以当前时间起算
    if (
        task.schedule_type == "INTERVAL"
        and (task.interval_seconds or 0) > 0
    ):
        base = task.last_run_at or datetime.utcnow()
        if base.tzinfo is None:
            base = base.replace(tzinfo=utc_timezone.utc)
        return (
            base + timedelta(seconds=task.interval_seconds)
        ).isoformat()

    # CRON：标准 5 段表达式，用触发器推算下次触发点
    if (
        task.schedule_type == "CRON"
        and (task.cron_expression or "").strip()
    ):
        parts = task.cron_expression.strip().split()
        if len(parts) != 5:
            return None
        try:
            try:
                import pytz

                task_timezone = pytz.timezone("Asia/Shanghai")
            except Exception:
                from zoneinfo import ZoneInfo

                task_timezone = ZoneInfo("Asia/Shanghai")
            from apscheduler.triggers.cron import CronTrigger

            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                timezone=task_timezone,
            )
            next_run = trigger.get_next_fire_time(
                None,
                datetime.now(task_timezone),
            )
            return next_run.isoformat() if next_run else None
        except Exception:
            return None
    return None


def _last_impact_map(
    db: Session,
    task_ids: list[str],
) -> dict[str, dict]:
    """Return each task's latest asset-lake impact summary."""
    if not task_ids:
        return {}
    output: dict[str, dict] = {}
    try:
        latest_created = (
            db.query(
                PipelineRun.task_id,
                func.max(PipelineRun.created_at).label("mx"),
            )
            .filter(PipelineRun.task_id.in_(task_ids))
            .group_by(PipelineRun.task_id)
            .subquery()
        )
        # 只取 task_id 与 stats 两列：stats 之外的大字段（error_log 等）不参与计算
        latest = (
            db.query(PipelineRun.task_id, PipelineRun.stats)
            .join(
                latest_created,
                (PipelineRun.task_id == latest_created.c.task_id)
                & (PipelineRun.created_at == latest_created.c.mx),
            )
            .all()
        )
        for task_id, stats in latest:
            impact = (stats or {}).get("lake_impact")
            if task_id not in output and impact:
                output[task_id] = impact
    except Exception as exc:
        _log_degradation_once("last_impact_map", exc)
    return output


def _with_pipeline_info(
    db: Session,
    tasks: list[PipelineTask],
) -> list[dict]:
    # 只取当前页任务实际引用的流水线、只取展示需要的标量列——
    # Pipeline 行含 definition/spec 等大 JSON 列，全表拉取会随流水线数量劣化。
    pipeline_ids = {task.pipeline_id for task in tasks if task.pipeline_id}
    pipelines = {}
    if pipeline_ids:
        pipelines = {
            row.id: row
            for row in db.query(
                Pipeline.id,
                Pipeline.name,
                Pipeline.status,
                Pipeline.enabled,
                Pipeline.version,
            ).filter(Pipeline.id.in_(pipeline_ids)).all()
        }
    task_ids = [task.id for task in tasks]
    live_next_runs = _live_next_run_map(task_ids)
    impacts = _last_impact_map(db, task_ids)
    items = []
    for task in tasks:
        item = task.to_dict()
        pipeline = pipelines.get(task.pipeline_id)
        item["pipeline_name"] = (
            pipeline.name if pipeline else "(已删除)"
        )
        item["pipeline_status"] = (
            (pipeline.status or "draft")
            if pipeline
            else "deleted"
        )
        item["pipeline_enabled"] = (
            bool(pipeline.enabled) if pipeline else False
        )
        item["pipeline_version"] = (
            (pipeline.version or 1) if pipeline else None
        )
        item["next_run_at"] = (
            live_next_runs.get(task.id)
            or _computed_next_run(task)
        )
        item["last_impact"] = impacts.get(task.id)
        items.append(item)
    return items


def stats_overview(
    db: Session,
    *,
    now_utc_fn: QueryDependency,
    shanghai_day_start_utc_fn: QueryDependency,
    shanghai_date_fn: QueryDependency,
    utc_iso_fn: QueryDependency,
) -> dict:
    total = db.query(PipelineTask).count()
    running = (
        db.query(PipelineTask)
        .filter(PipelineTask.status == "running")
        .count()
    )
    success = (
        db.query(PipelineTask)
        .filter(PipelineTask.status == "success")
        .count()
    )
    idle = (
        db.query(PipelineTask)
        .filter(PipelineTask.status == "idle")
        .count()
    )
    enabled = (
        db.query(PipelineTask)
        .filter(PipelineTask.enabled.is_(True))
        .count()
    )
    failed = (
        db.query(PipelineTask)
        .filter(PipelineTask.status == "failed")
        .count()
    )

    # 执行次数/异常数：以任务触发的 PipelineRun 为口径（今日 + 累计）
    task_runs = db.query(PipelineRun).filter(
        PipelineRun.task_id.isnot(None)
    )
    local_today = now_utc_fn().astimezone(SHANGHAI_TZ).date()
    today_start = shanghai_day_start_utc_fn(local_today)
    today_runs = task_runs.filter(
        PipelineRun.created_at >= today_start
    ).count()
    today_errors = task_runs.filter(
        PipelineRun.created_at >= today_start,
        PipelineRun.status == "failed",
    ).count()
    total_runs = task_runs.count()
    total_errors = task_runs.filter(
        PipelineRun.status == "failed"
    ).count()

    # 最近 7 个自然日的真实运行次数。前端不得用随机/合成曲线冒充观测数据。
    first_day = local_today - timedelta(days=6)
    trend = {
        (first_day + timedelta(days=index)).isoformat(): {
            "runs": 0,
            "errors": 0,
        }
        for index in range(7)
    }
    recent = (
        task_runs.filter(
            PipelineRun.created_at
            >= shanghai_day_start_utc_fn(first_day)
        )
        .with_entities(
            PipelineRun.created_at,
            PipelineRun.status,
        )
        .all()
    )
    for created_at, run_status in recent:
        if not created_at:
            continue
        key = shanghai_date_fn(created_at).isoformat()
        if key not in trend:
            continue
        trend[key]["runs"] += 1
        if run_status == "failed":
            trend[key]["errors"] += 1

    # 右侧执行动态：按执行创建时间倒序展示最新 30 条真实记录，口径与统计一致。
    # 只 select 展示需要的列——Pipeline 行含 definition/spec/validation_attestation
    # 大 JSON，整行物化会随流水线定义体积放大每次轮询的响应。
    recent_runs = (
        db.query(
            PipelineRun.id, PipelineRun.status, PipelineRun.stats,
            PipelineRun.started_at, PipelineRun.created_at,
            PipelineRun.finished_at, PipelineRun.error_log,
            PipelineTask.id, PipelineTask.name, Pipeline.name,
        )
        .join(PipelineTask, PipelineTask.id == PipelineRun.task_id)
        .join(Pipeline, Pipeline.id == PipelineRun.pipeline_id)
        .order_by(PipelineRun.created_at.desc())
        .limit(30)
        .all()
    )

    return {
        "total": total,
        "running": running,
        "success": success,
        "idle": idle,
        "enabled": enabled,
        "failed": failed,
        "today_runs": today_runs,
        "today_errors": today_errors,
        "total_runs": total_runs,
        "total_errors": total_errors,
        "trend_7d": [
            {
                "date": day,
                "runs": counts["runs"],
                "errors": counts["errors"],
            }
            for day, counts in trend.items()
        ],
        "recent_runs": [
            {
                "id": run_id,
                "task_id": task_id,
                "task_name": task_name,
                "pipeline_name": pipeline_name,
                "status": run_status,
                "trigger_type": (run_stats or {}).get(
                    "trigger_type",
                    "manual",
                ),
                "started_at": utc_iso_fn(started_at or created_at),
                "finished_at": utc_iso_fn(finished_at),
                "rows_out": (run_stats or {}).get("rows_out", 0),
                "lake_impact": (run_stats or {}).get(
                    "lake_impact"
                ),
                "error_message": error_log or "",
            }
            for (
                run_id, run_status, run_stats, started_at, created_at,
                finished_at, error_log, task_id, task_name, pipeline_name
            ) in recent_runs
        ],
    }


def list_tasks(
    search: Optional[str],
    status: Optional[str],
    enabled: Optional[bool],
    pipeline_id: Optional[str],
    page: int,
    page_size: int,
    db: Session,
    *,
    with_pipeline_info_fn: QueryDependency,
) -> dict:
    # 单页上限 100（与历史接口同一量级）：任务列表会 join 流水线并附带
    # 调度/影响摘要，超大 page_size 会把一次轮询放大成全表物化。
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size 必须在 1 到 100 之间")
    query = db.query(PipelineTask)
    if search:
        query = query.join(
            Pipeline,
            Pipeline.id == PipelineTask.pipeline_id,
        ).filter(
            or_(
                PipelineTask.name.ilike(f"%{search}%"),
                Pipeline.name.ilike(f"%{search}%"),
            )
        )
    if status:
        query = query.filter(PipelineTask.status == status)
    if enabled is not None:
        query = query.filter(PipelineTask.enabled.is_(enabled))
    if pipeline_id:
        query = query.filter(
            PipelineTask.pipeline_id == pipeline_id
        )
    total = query.count()
    tasks = (
        query.order_by(PipelineTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "items": with_pipeline_info_fn(db, tasks),
        "page": page,
        "page_size": page_size,
    }


def pipeline_filter_options(db: Session) -> dict:
    """Return only pipelines referenced by one or more tasks."""
    from sqlalchemy import func

    rows = (
        db.query(
            PipelineTask.pipeline_id,
            Pipeline.name,
            func.count(PipelineTask.id).label("task_count"),
        )
        .outerjoin(
            Pipeline,
            Pipeline.id == PipelineTask.pipeline_id,
        )
        .group_by(PipelineTask.pipeline_id, Pipeline.name)
        .order_by(
            Pipeline.name.asc(),
            PipelineTask.pipeline_id.asc(),
        )
        .all()
    )
    return {
        "items": [
            {
                "id": pipeline_id,
                "name": pipeline_name or "(流水线已删除)",
                "task_count": int(task_count),
            }
            for pipeline_id, pipeline_name, task_count in rows
        ]
    }


def get_task(
    task_id: str,
    db: Session,
    *,
    with_pipeline_info_fn: QueryDependency,
) -> dict:
    task = (
        db.query(PipelineTask)
        .filter(PipelineTask.id == task_id)
        .first()
    )
    if not task:
        raise HTTPException(404, "PipelineTask not found")
    return with_pipeline_info_fn(db, [task])[0]
