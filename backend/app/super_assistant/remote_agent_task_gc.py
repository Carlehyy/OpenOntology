"""远程助手任务队列的例行清理（APScheduler 进程内定时，先例 palace_consolidate）。

done/expired 任务行与孤儿行（进程崩溃遗留的 pending/claimed，过期后对
认领不可见）不清理会随时间无限累积。保留口径：expires_at 早于
now - TASK_RETENTION_DAYS 的行删除——任务 expires_at ≤ 创建时间 + 超时
（≤10 分钟），故"过期后 7 天"与"完成后 7 天"相差不超过超时窗口，
单谓词即可同时覆盖三类行，无需按状态分支。

DELETE 是单条语句的轻量维护，不走 NATS 派发；max_instances=1 +
coalesce 防重入。test 环境不注册（lifecycle 侧统一门禁）。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from app.shared.database import SessionLocal
from app.super_assistant.models import (
    SuperAssistantRemoteAgentInvite,
    SuperAssistantRemoteAgentTask,
)
from app.super_assistant.remote_agent_service import _utcnow

logger = logging.getLogger(__name__)

TASK_RETENTION_DAYS = 7
INVITE_RETENTION_DAYS = 30
_JOB_ID = "super_assistant_remote_agent_task_gc"
_scheduler = None


def prune_once() -> int:
    """删除保留期外的任务行，返回删除行数（测试直接调用断言）。"""
    cutoff = _utcnow() - timedelta(days=TASK_RETENTION_DAYS)
    invite_cutoff = _utcnow() - timedelta(days=INVITE_RETENTION_DAYS)
    db = SessionLocal()
    try:
        removed = (
            db.query(SuperAssistantRemoteAgentTask)
            .filter(SuperAssistantRemoteAgentTask.expires_at < cutoff)
            .delete(synchronize_session=False)
        )
        # 过期超 30 天的邀请行连同可解密令牌备份一并清除（含已消费/已撤销）
        db.query(SuperAssistantRemoteAgentInvite).filter(
            SuperAssistantRemoteAgentInvite.expires_at < invite_cutoff,
        ).delete(synchronize_session=False)
        db.commit()
        if removed:
            logger.info("远程助手任务清理完成：删除 %s 行（保留 %s 天）", removed, TASK_RETENTION_DAYS)
        return removed
    finally:
        db.close()


def start() -> None:
    """启动每日 04:00 的清理定时器（错开 03:00 的记忆宫殿任务）。"""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        prune_once, "cron", hour=4, minute=0,
        id=_JOB_ID, max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("远程助手任务清理定时器已启动（每天 04:00，本地时区）")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
