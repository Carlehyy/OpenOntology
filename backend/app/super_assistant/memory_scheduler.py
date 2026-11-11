"""记忆来源同步对账：API 进程只派发，提取由 NATS executor 执行。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _scan() -> None:
    from app.super_assistant.memory_tasks import drain_once

    try:
        drain_once()
    except Exception:
        logger.exception("记忆同步对账失败")


def start() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _scan, "interval", seconds=5, id="super-assistant-memory-sync",
        max_instances=1, coalesce=True, misfire_grace_time=30,
    )
    _scheduler.start()


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
