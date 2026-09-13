"""kernel.v1 outbox/recovery scheduler (APScheduler producer side)."""
from __future__ import annotations

import logging

from app.shared.database import SessionLocal

logger = logging.getLogger(__name__)
_scheduler = None


def _drain() -> None:
    from .outbox import publish_due_once, recover_expired_claims
    from .recovery import expire_due_runs_once, join_ready_parents_once, recover_stuck_runs_once

    db = SessionLocal()
    try:
        recover_expired_claims(db)
        expire_due_runs_once(db)
        recover_stuck_runs_once(db)
        join_ready_parents_once(db)
        publish_due_once(db, batch_size=20)
    except Exception:
        logger.exception("kernel execution outbox drain failed")
    finally:
        db.close()


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(_drain, "interval", seconds=5, id="sa-kernel-outbox", max_instances=1, coalesce=True)
    _scheduler.start()


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
