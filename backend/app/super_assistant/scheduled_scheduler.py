"""定时任务扫描器 — APScheduler 进程内定时 + NATS 派发。

与助手评估值守、记忆宫殿合并同一模式：API 进程只扫描到期项并派发，
LLM 执行在 nats_executor。每 30 秒扫描一次；max_instances=1 防止重叠扫描。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_JOB_ID = "super-assistant-scheduled-dispatch"
SCAN_INTERVAL_SECONDS = 30


def _scan() -> None:
    from app.super_assistant.scheduled_service import dispatch_due_tasks

    try:
        dispatched = dispatch_due_tasks()
        if dispatched:
            logger.info("定时任务已派发 %s 条", dispatched)
    except Exception:  # noqa: BLE001 — 扫描失败不影响进程
        logger.exception("定时任务扫描失败")


def start() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _scan, "interval", seconds=SCAN_INTERVAL_SECONDS,
        id=_JOB_ID, max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    _scheduler.start()
    logger.info("超级助手定时任务扫描器已启动（每 %s 秒）", SCAN_INTERVAL_SECONDS)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
