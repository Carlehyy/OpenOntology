"""本体助手聊天后台回合的任务入口与对账调度器。

平台任务模式（Celery 已退役）：Web 请求只把 run 落库并派发 run_id；真正的
LLM 回合由 ``nats_executor`` 消费 ``ontology.assistant.run`` 后调用
``run_service.execute_run`` 执行。本模块同时提供 exec 进程的消息 handler
和 Web 进程内的 APScheduler 对账扫描。

对账语义由 ``run_service.reconcile_runs()`` 持有：只回收租约已过期的
running run（标记 interrupted），并自行重派 queued run。调度器不扫描业务
表，也不在启动时粗暴中断未过期 running——多个 executor/Web 实例并存时，
存活 worker 的租约仍然有效，必须继续等待。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# queued 补发 + 过期租约回收周期：30-60 秒之间，避免故障后长时间悬空。
RECONCILE_INTERVAL_SECONDS = 45
_JOB_ID = "ontology-assistant-run-reconcile"
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()


async def run_ontology_assistant_run_message(payload: dict) -> None:
    """``ontology.assistant.run``：认领并执行一条已落库的后台回合。

    ``execute_run`` 是同步函数且负责业务幂等（重复消息、终态、租约冲突
    都在服务内消化）。只有数据库瞬时故障等可重试异常才逃逸到 executor，
    由 nak 延迟重投；格式坏消息直接丢弃，避免毒消息无限重投。
    """
    if not isinstance(payload, dict):
        logger.error("本体助手回合消息不是对象，丢弃")
        return
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        logger.error("本体助手回合消息缺少 run_id，丢弃")
        return

    from app.ontologies.agent_runtime import run_service

    await asyncio.to_thread(run_service.execute_run, run_id.strip())


def reconcile_once() -> dict[str, int]:
    """执行一次对账；返回 ``{"interrupted": n, "redispatched": m}``。

    ``reconcile_runs`` 自己派发 queued run，因此调度器不重复调用派发端口。
    服务返回非法值时抛出异常，由扫描器记录且不阻断下一周期。
    """
    from app.ontologies.agent_runtime import run_service

    result = run_service.reconcile_runs()
    if not isinstance(result, dict):
        raise TypeError("reconcile_runs 必须返回计数 dict")
    return {
        "interrupted": int(result.get("interrupted", 0)),
        "redispatched": int(result.get("redispatched", 0)),
    }


def _scan() -> None:
    try:
        counts = reconcile_once()
        if any(counts.values()):
            logger.info(
                "本体助手回合对账：过期中断 %s 个、queued 重派 %s 个",
                counts["interrupted"], counts["redispatched"],
            )
    except Exception:  # noqa: BLE001 — 对账失败不阻断 API 进程
        logger.exception("本体助手回合对账扫描失败")


def start() -> None:
    """启动 Web 进程内对账扫描器；重复调用幂等（只注册一个 job/线程）。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None and _scheduler.running:
            return
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        scheduler.add_job(
            _scan,
            "interval",
            seconds=RECONCILE_INTERVAL_SECONDS,
            id=_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
            # 启动后立即做一次租约/队列对账：只处理已过期租约，不碰活跃 worker。
            next_run_time=datetime.now(),
        )
        scheduler.start()
        _scheduler = scheduler
        logger.info(
            "本体助手回合对账器已启动（每 %s 秒）", RECONCILE_INTERVAL_SECONDS
        )


def shutdown() -> None:
    global _scheduler
    with _scheduler_lock:
        scheduler, _scheduler = _scheduler, None
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
