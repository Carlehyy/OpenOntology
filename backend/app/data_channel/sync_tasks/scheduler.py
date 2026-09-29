"""
数据任务池调度器 (APScheduler 后台线程)
- 只调度已发布流水线对应的 PipelineTask
- 旧 DataSyncTask 仅保留历史审计，不再注册或执行
- 同一任务同一时间只允许一个实例运行（内存锁）
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# Job ID 前缀
_JOB_PREFIX = "sync_task:"
_PIPE_JOB_PREFIX = "pipe_task:"
_DATASET_EVENT_JOB_ID = "dataset_version_events:drain"
_PIPELINE_RECONCILE_JOB_ID = "pipeline_executions:reconcile"
# 正在执行的任务锁
_running_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()


def _get_task_lock(task_id: str) -> threading.Lock:
    with _global_lock:
        if task_id not in _running_locks:
            _running_locks[task_id] = threading.Lock()
        return _running_locks[task_id]


def _job_runner(task_id: str) -> None:
    """兼容旧 APScheduler 引用；退休任务永远不再执行。"""
    logger.warning("已忽略退休的 DataSyncTask 调度回调: %s", task_id)


def _pipeline_job_runner(task_id: str) -> None:
    """调度器回调：把流水线调度任务派发到 NATS，由独立 executor 进程执行。

    派发失败只记日志：下一个调度周期会自然重试；重复派发由执行引擎的
    数据库原子租约兜底，不会产生并发执行。
    """
    lock = _get_task_lock(f"pipe:{task_id}")
    if not lock.acquire(blocking=False):
        logger.info(f"PipelineTask {task_id} 正在派发，跳过本次调度")
        return
    try:
        from app.data_channel.pipeline_tasks.dispatch import dispatch_pipeline_task
        dispatch_pipeline_task(task_id, "scheduled")
        logger.info(f"PipelineTask {task_id} 已派发调度执行")
    except Exception as e:
        logger.error(f"PipelineTask {task_id} 派发失败（下一调度周期自动重试）: {e}")
    finally:
        lock.release()


def _dataset_event_job_runner() -> None:
    """Continuously drain durable lake-version events."""
    try:
        from app.data_channel.datasets.version_events import (
            drain_dataset_version_events,
        )
        result = drain_dataset_version_events()
        if result.get("processed") or result.get("retried"):
            logger.info("DatasetVersion event outbox: %s", result)
    except Exception:
        logger.exception("DatasetVersion event outbox worker failed")


def _pipeline_reconcile_job_runner() -> None:
    """周期收口进程退出留下的中断流水线执行（只处理租约已过期的）。"""
    try:
        from app.database import SessionLocal
        from app.data_channel.pipeline_tasks.reconciler import (
            reconcile_pipeline_executions,
        )
        db = SessionLocal()
        try:
            result = reconcile_pipeline_executions(db)
        finally:
            db.close()
        if result.get("tasks_failed") or result.get("runs_failed"):
            logger.info("PipelineTask 对账器收口中断执行: %s", result)
    except Exception:
        logger.exception("PipelineTask 对账器执行失败")


class SyncScheduler:
    """同步任务调度器 — 单例"""

    _instance: "SyncScheduler | None" = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self._started = False
        self._last_error: str | None = None

    @property
    def scheduler(self):
        return self._scheduler

    @property
    def started(self):
        return self._started

    @property
    def healthy(self) -> bool:
        return bool(
            self._started
            and getattr(self._scheduler, "running", False)
            and self._last_error is None)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @classmethod
    def get(cls) -> "SyncScheduler":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def start(self) -> None:
        if self._started:
            return
        try:
            self._scheduler.start()
            self._started = True
            self.reload_all()
            from app.config import settings
            self._scheduler.add_job(
                _dataset_event_job_runner,
                trigger=IntervalTrigger(
                    seconds=max(1, int(settings.dataset_event_poll_seconds or 2))),
                id=_DATASET_EVENT_JOB_ID,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=30,
            )
            self._scheduler.add_job(
                _pipeline_reconcile_job_runner,
                trigger=IntervalTrigger(
                    seconds=max(
                        1,
                        int(settings.pipeline_run_reconcile_interval_seconds or 300),
                    )),
                id=_PIPELINE_RECONCILE_JOB_ID,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=60,
            )
            # Fail closed on a missing production migration and recover any
            # events left behind by the previous process before reporting ready.
            from app.data_channel.datasets.version_events import (
                drain_dataset_version_events,
            )
            drain_dataset_version_events(
                limit=int(settings.dataset_event_batch_size or 20),
                strict_schema=settings.environment == "production",
            )
            logger.info("DataSyncScheduler 已启动")
        except Exception as e:
            self._last_error = str(e)
            self._started = False
            # 底层调度器可能已在 RUNNING（如 drain 预跑失败）：必须一并停掉，
            # 否则 _started=False 与实际运行状态错位——已注册的旧 job 会继续
            # 触发，而所有 reload 都被「调度器未启动」拒绝。
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            logger.error(f"DataSyncScheduler 启动失败: {e}")

    def shutdown(self) -> None:
        if self._started:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._started = False

    def _job_id(self, task_id: str) -> str:
        return f"{_JOB_PREFIX}{task_id}"

    def _add_job_for_task(self, task) -> None:
        """移除旧任务残留 Job；DataSyncTask 不再进入执行主链路。"""
        job_id = self._job_id(task.id)
        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            pass
        logger.info("DataSyncTask %s 已退休，不注册调度", task.id)

    def _add_job_for_pipeline_task(self, task) -> bool:
        """为流水线调度任务注册 APScheduler Job（与同步任务同一调度器实例）。

        返回注册结果供 CRUD 响应呈现「已保存但未注册调度」：
        True = 已注册或有意不注册（disabled/MANUAL）；False = 注册失败。
        """
        job_id = f"{_PIPE_JOB_PREFIX}{task.id}"
        try:
            self._scheduler.remove_job(job_id)
        except JobLookupError:
            pass
        if not task.enabled:
            return True
        if task.schedule_type == "CRON":
            if not task.cron_expression:
                logger.warning(
                    f"PipelineTask {task.id} schedule_type=CRON 但 cron 表达式为空，未注册调度")
                return False
            try:
                parts = task.cron_expression.strip().split()
                if len(parts) != 5:
                    logger.warning(f"PipelineTask {task.id} cron 表达式不合法: {task.cron_expression}")
                    return False
                trigger = CronTrigger(
                    minute=parts[0], hour=parts[1], day=parts[2],
                    month=parts[3], day_of_week=parts[4],
                )
                self._scheduler.add_job(
                    _pipeline_job_runner, trigger=trigger, id=job_id,
                    args=[task.id], replace_existing=True,
                    misfire_grace_time=60, coalesce=True, max_instances=1,
                )
                logger.info(f"已注册流水线任务 CRON 调度: {task.name} ({task.cron_expression})")
                return True
            except Exception as e:
                logger.error(f"注册流水线任务 CRON 失败 {task.id}: {e}")
                return False
        elif task.schedule_type == "INTERVAL":
            if not task.interval_seconds or task.interval_seconds <= 0:
                logger.warning(
                    f"PipelineTask {task.id} schedule_type=INTERVAL 但 "
                    f"interval_seconds={task.interval_seconds} 非正数，未注册调度")
                return False
            try:
                trigger = IntervalTrigger(seconds=task.interval_seconds)
                self._scheduler.add_job(
                    _pipeline_job_runner, trigger=trigger, id=job_id,
                    args=[task.id], replace_existing=True,
                    misfire_grace_time=60, coalesce=True, max_instances=1,
                )
                logger.info(f"已注册流水线任务 INTERVAL 调度: {task.name} ({task.interval_seconds}s)")
                return True
            except Exception as e:
                logger.error(f"注册流水线任务 INTERVAL 失败 {task.id}: {e}")
                return False
        # MANUAL: 不注册调度（有意行为，视为成功）
        return True

    def reload_all(self) -> None:
        """只从 DB 加载 PipelineTask，并清除所有旧 SyncTask Job。"""
        if not self._started:
            return
        self._last_error = None
        try:
            from app.database import SessionLocal
            from app.data_channel.pipeline_tasks.models import PipelineTask
            db = SessionLocal()
            try:
                # 先清除所有相关 job
                for job in self._scheduler.get_jobs():
                    if (job.id.startswith(_JOB_PREFIX)
                            or job.id.startswith(_PIPE_JOB_PREFIX)):
                        try:
                            self._scheduler.remove_job(job.id)
                        except JobLookupError:
                            pass
                        except Exception:
                            # 单个残留 job 清理失败不能打断其余任务的
                            # 全量重注册（换持久化 jobstore 时可能出现）
                            logger.exception(
                                "reload_all 清理 job %s 失败，继续", job.id)
                for t in db.query(PipelineTask).all():
                    self._add_job_for_pipeline_task(t)
            finally:
                db.close()
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"reload_all 失败: {e}")

    def reload_pipeline_task(self, task_id: str) -> bool:
        """更新单个流水线调度任务的 Job（任务 CRUD 后调用）。

        返回是否成功应用：True = 已注册/有意不注册/任务已删除并清理；
        False = 调度器未启动、注册失败或查询异常。调用方据此在 CRUD
        响应中呈现「已保存但未注册调度」。
        """
        if not self._started:
            return False
        try:
            from app.database import SessionLocal
            from app.data_channel.pipeline_tasks.models import PipelineTask
            db = SessionLocal()
            try:
                task = db.query(PipelineTask).filter(PipelineTask.id == task_id).first()
                if task:
                    return self._add_job_for_pipeline_task(task)
                try:
                    self._scheduler.remove_job(f"{_PIPE_JOB_PREFIX}{task_id}")
                except JobLookupError:
                    pass
                return True
            finally:
                db.close()
        except Exception as e:
            logger.error(f"reload_pipeline_task 失败: {e}")
            return False

    def reload_task(self, task_id: str) -> None:
        """兼容旧路由：只清理残留 Job，绝不重新注册。"""
        if not self._started:
            return
        try:
            self._scheduler.remove_job(self._job_id(task_id))
        except JobLookupError:
            # APScheduler 未找到 job 是正常的幂等结果。
            pass
        except Exception as e:
            logger.error(f"清理旧 DataSyncTask job 失败: {e}")


def get_sync_scheduler() -> SyncScheduler:
    return SyncScheduler.get()
