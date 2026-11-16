"""Recover durable AutoLoop dispatches and publish pending wakeups."""
from __future__ import annotations

import logging
from threading import Lock
from time import monotonic

from apscheduler.schedulers.background import BackgroundScheduler

from app.shared.config import settings

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None
_lock = Lock()
_next_watchdog = 0.0


def _scan() -> None:
    global _next_watchdog
    from app.super_assistant.autoloop.dispatching import scan_pending
    from app.super_assistant.autoloop.watchdog import scan_watchdog
    from app.super_assistant.autoloop.worker import scan_actions
    from app.super_assistant.provider import DailyLimitExceeded, daily_limit_reached

    if daily_limit_reached():
        return
    for name, scan in (("dispatch", scan_pending), ("actions", scan_actions)):
        try:
            scan()
        except DailyLimitExceeded:
            return
        except Exception:
            logger.exception("AutoLoop %s scan failed", name)
    if monotonic() >= _next_watchdog:
        _next_watchdog = monotonic() + settings.autoloop_watchdog_interval_seconds
        try:
            scan_watchdog()
        except DailyLimitExceeded:
            return
        except Exception:
            logger.exception("AutoLoop watchdog scan failed")


def start() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None and _scheduler.running:
            return
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            _scan, "interval", seconds=5, id="super-assistant-autoloop-dispatch",
            max_instances=1, coalesce=True, misfire_grace_time=30,
        )
        try:
            scheduler.start()
        except BaseException:
            if scheduler.running:
                scheduler.shutdown(wait=True)
            raise
        _scheduler = scheduler


def shutdown() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            if _scheduler.running:
                _scheduler.shutdown(wait=True)
            _scheduler = None
