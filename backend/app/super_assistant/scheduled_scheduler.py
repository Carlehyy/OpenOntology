"""定时任务扫描器 — APScheduler 进程内定时 + NATS 派发。

API 进程每 30 秒把到期槽位和 AutoLoop 接收事实同事务保存，
随后发布持久派发身份；max_instances=1 防止重叠扫描。
"""
from __future__ import annotations

import logging
from threading import Lock

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = Lock()
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
    with _lock:
        if _scheduler is not None and _scheduler.running:
            return
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        scheduler.add_job(
            _scan, "interval", seconds=SCAN_INTERVAL_SECONDS,
            id=_JOB_ID, max_instances=1, coalesce=True, misfire_grace_time=60,
        )
        try:
            scheduler.start()
        except BaseException:
            if scheduler.running:
                scheduler.shutdown(wait=True)
            raise
        _scheduler = scheduler
        logger.info("超级助手定时任务扫描器已启动（每 %s 秒）", SCAN_INTERVAL_SECONDS)


def shutdown() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            if _scheduler.running:
                _scheduler.shutdown(wait=True)
            _scheduler = None
