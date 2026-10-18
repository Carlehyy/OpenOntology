"""确认操作派发 worker — APScheduler 扫描 + 崩溃窗口恢复。

DB 里的 dispatch 状态是事实源；本进程内调度只负责唤醒（每 30 秒有界扫描，
每批 ≤100）。claim/lease/fence 由 ``service`` 的持久 CAS 提供，多个唤醒并发
时只有一名持有者能入引擎。
"""
from __future__ import annotations

import logging

from app.ontologies.agent_confirmation import models as M
from app.ontologies.agent_confirmation.service import (
    _now, _fresh_session, reconcile_operation, run_operation_inline,
)

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 30
BATCH_LIMIT = 100

_scheduler = None
_JOB_ID = "ontology_action_confirmation_dispatch"


def _scan() -> None:
    db = _fresh_session()
    try:
        from app.ontologies.agent_query.models import AgentQuerySnapshot

        # 查询快照 TTL 物理清理（有界批次）
        db.query(AgentQuerySnapshot).filter(
            AgentQuerySnapshot.expires_at < _now()).limit(
                500).delete(synchronize_session=False)
        db.commit()
        queued = (db.query(M.ActionOperation.id)
                  .filter(M.ActionOperation.state == "dispatch_queued")
                  .order_by(M.ActionOperation.queued_at)
                  .limit(BATCH_LIMIT).all())
        stale = (db.query(M.ActionOperation.id)
                 .filter(M.ActionOperation.state == "executing",
                         M.ActionOperation.lease_expires_at < _now())
                 .order_by(M.ActionOperation.queued_at)
                 .limit(BATCH_LIMIT).all())
        pending = (db.query(M.ActionOperation.id)
                   .filter(M.ActionOperation.state == "pending_approval")
                   .order_by(M.ActionOperation.queued_at)
                   .limit(BATCH_LIMIT).all())
        queue_ids = [row.id for row in queued]
        stale_ids = [row.id for row in stale]
        pending_ids = [row.id for row in pending]
    finally:
        db.close()
    for operation_id in queue_ids:
        try:
            run_operation_inline(operation_id)
        except Exception:  # noqa: BLE001 - 单条失败不阻断批次
            logger.exception("confirmation dispatch failed op=%s",
                             operation_id)
    for operation_id in stale_ids + pending_ids:
        try:
            reconcile_operation(operation_id)
        except Exception:  # noqa: BLE001
            logger.exception("confirmation reconcile failed op=%s",
                             operation_id)


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _scan, "interval", seconds=SCAN_INTERVAL_SECONDS, id=_JOB_ID,
        max_instances=1, coalesce=True, misfire_grace_time=60)
    _scheduler.start()
    logger.info("ontology action confirmation worker started")


def shutdown() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
