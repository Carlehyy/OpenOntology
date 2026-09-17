"""定时任务 NATS 消息处理器（executor 进程内运行）。"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_scheduled_task_message(payload: dict) -> None:
    """super_assistant.scheduled.run：无人值守执行一条到期计划。"""
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        logger.error("定时任务消息缺少 run_id，丢弃")
        return

    from app.super_assistant.scheduled_service import execute_run

    def _run() -> None:
        try:
            execute_run(run_id)
        except Exception:
            logger.exception("定时任务执行器异常 run=%s", run_id)

    await asyncio.to_thread(_run)
