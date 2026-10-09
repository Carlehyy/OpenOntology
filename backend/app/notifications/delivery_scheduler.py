"""消息通知投递扫描器 — APScheduler 进程内定时。

与超级助手定时任务扫描器同一模式：每 30 秒扫描 pending 投递单并同步执行
apprise 发送；max_instances=1 防重叠，失败重试由 attempts 上限收敛。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_JOB_ID = "notifications-delivery-dispatch"
SCAN_INTERVAL_SECONDS = 30


def _scan() -> None:
    from app.database import SessionLocal
    from app.notifications.channel_service import dispatch_pending_deliveries

    db = SessionLocal()
    try:
        result = dispatch_pending_deliveries(db)
        if result.get("sent") or result.get("failed"):
            logger.info(
                "消息通知投递：成功 %s 条、终态失败 %s 条",
                result.get("sent", 0),
                result.get("failed", 0),
            )
    except Exception:  # noqa: BLE001 — 扫描失败不影响进程
        logger.exception("消息通知投递扫描失败")
    finally:
        db.close()


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
    logger.info("消息通知投递扫描器已启动（每 %s 秒）", SCAN_INTERVAL_SECONDS)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
