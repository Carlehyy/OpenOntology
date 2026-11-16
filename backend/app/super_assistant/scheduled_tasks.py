"""旧日程 NATS 入口：把尚未执行的 queued 记录交给 AutoLoop。"""
from __future__ import annotations

import asyncio


async def run_scheduled_task_message(payload: dict) -> None:
    """提交接收事实后才返回；事务异常交给 executor NAK。"""
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("定时任务消息缺少 run_id")

    from app.super_assistant.scheduled_service import execute_run

    await asyncio.to_thread(execute_run, run_id)
