"""任务实例 — 对账层：审批过期 / 租约回收 / 派发重投 / 并行补位。

与超级助手定时扫描器同一模式：API 进程内 APScheduler 定时扫描，
旁路能力（失败不阻断启动）；状态推进全部走引擎（行锁 + 事件）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.task_instances import engine
from app.task_instances.models import (
    APPROVAL_PENDING,
    INSTANCE_ACTIVE,
    NODE_DISPATCHED,
    NODE_PENDING,
    NODE_RUNNING,
    TaskApproval,
    TaskInstance,
    TaskNodeRun,
)

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_JOB_ID = "task-instances-reconcile"
SCAN_INTERVAL_SECONDS = 30
# dispatched 但长期无人认领 → 重投 NATS（传输层丢失的自愈窗口）
REDISPATCH_AFTER_SECONDS = 120

_SYSTEM_EXPIRY = "system:expiry"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 读回的朴素时间戳补 UTC 时区，兼容与 aware 的比较。"""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def expire_approvals_once(db: Session) -> int:
    """过期审批自动 rejected（actor=system:expiry），路由同人工拒绝。"""
    due = db.query(TaskApproval).filter(
        TaskApproval.status == APPROVAL_PENDING,
        TaskApproval.expires_at.isnot(None),
        TaskApproval.expires_at < _now(),
    ).all()
    expired = 0
    for approval in due:
        instance = db.query(TaskInstance).filter(
            TaskInstance.id == approval.instance_id).first()
        if instance is None or instance.status != INSTANCE_ACTIVE:
            approval.status = "expired"
            continue
        from app.task_instances import events as ev
        ev.append_event(
            db, approval.instance_id, ev.APPROVAL_EXPIRED,
            {"node_id": _node_id_of(db, approval), "approval_id": approval.id},
            node_run_id=approval.node_run_id, actor=_SYSTEM_EXPIRY,
            event_budget=engine._policies(
                instance.spec_snapshot or {}).event_budget)
        try:
            engine.decide_approval(
                db, approval.id, "rejected",
                reason="审批等待超时，系统自动驳回",
                decided_by=_SYSTEM_EXPIRY)
            db.commit()
            expired += 1
        except Exception:  # noqa: BLE001 — 单条失败不影响其余对账
            db.rollback()
            logger.exception("过期审批处理失败: %s", approval.id)
    return expired


def reclaim_and_redispatch_once(db: Session) -> dict[str, int]:
    """租约过期回收 + 未认领重投 + pending 补位，返回计数。"""
    reclaimed = redispatched = promoted = 0
    horizon = _now() - timedelta(seconds=REDISPATCH_AFTER_SECONDS)
    candidates = db.query(TaskNodeRun).filter(
        TaskNodeRun.status.in_((NODE_DISPATCHED, NODE_RUNNING)),
    ).all()
    stale = [run for run in candidates
             if (_aware(run.lease_expires_at) is not None
                 and _aware(run.lease_expires_at) < _now())
             or (_aware(run.dispatched_at) is not None
                 and _aware(run.dispatched_at) < horizon)]
    for run in stale:
        lease = _aware(run.lease_expires_at)
        if run.status == NODE_RUNNING and lease and lease >= _now():
            continue  # 仅 dispatched 未认领，走重投而非回收
        try:
            result = engine.reconcile_stale_attempt(db, run)
            db.commit()
            engine_result_dispatch(result)
            reclaimed += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("租约回收失败: %s", run.id)
    unclaimed = [run for run in candidates
                 if run.status == NODE_DISPATCHED
                 and _aware(run.dispatched_at) is not None
                 and _aware(run.dispatched_at) < horizon]
    for run in unclaimed:
        _dispatch(run.id)
        redispatched += 1
    pending_instances = db.query(TaskInstance.id).filter(
        TaskInstance.status == INSTANCE_ACTIVE,
        TaskInstance.id.in_(
            db.query(TaskNodeRun.instance_id).filter(
                TaskNodeRun.status == NODE_PENDING)),
    ).all()
    for (instance_id,) in pending_instances:
        try:
            result = engine.promote_pending(db, instance_id)
            db.commit()
            engine_result_dispatch(result)
            promoted += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("并行补位失败: %s", instance_id)
    return {"reclaimed": reclaimed, "redispatched": redispatched,
            "promoted": promoted}


def engine_result_dispatch(result: engine.EngineResult) -> None:
    from app.task_instances.service import _dispatch_node

    for node_run_id in result.dispatches:
        _dispatch_node(node_run_id)


def _dispatch(node_run_id: str) -> None:
    try:
        from app.task_instances.service import _dispatch_node

        _dispatch_node(node_run_id)
    except Exception:  # noqa: BLE001
        logger.exception("对账重投失败: %s", node_run_id)


def _node_id_of(db: Session, approval: TaskApproval) -> str:
    run = db.query(TaskNodeRun).filter(
        TaskNodeRun.id == approval.node_run_id).first()
    return run.node_id if run else ""


def reconcile_once() -> dict[str, int]:
    """一次完整对账（独立会话；调度器与测试共用入口）。"""
    db: Session = SessionLocal()
    try:
        expired = expire_approvals_once(db)
        counts = reclaim_and_redispatch_once(db)
        counts["expired_approvals"] = expired
        return counts
    finally:
        db.close()


def _scan() -> None:
    try:
        counts = reconcile_once()
        if any(counts.values()):
            logger.info("任务实例对账：%s", counts)
    except Exception:  # noqa: BLE001 — 扫描失败不影响进程
        logger.exception("任务实例对账扫描失败")


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
    logger.info("任务实例对账扫描器已启动（每 %s 秒）", SCAN_INTERVAL_SECONDS)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
