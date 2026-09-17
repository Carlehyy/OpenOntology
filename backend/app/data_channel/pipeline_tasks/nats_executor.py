"""
流水线任务 NATS executor 进程

独立进程消费 JetStream 工作队列并执行后台任务，把执行负载（GIL、内存、
数据库连接）移出 Web 进程。NATS 只负责送达；并发正确性由执行引擎的
数据库原子租约（``engine._claim_task``）与各任务自身的数据库/文件
状态机兜底，重复消息在执行侧幂等消化后直接 ack 丢弃。

当前消费的 subject：

- ``pipeline.task.execute``：数据任务池的调度/手动单任务触发；
- ``task.pipeline.run``：UI 手动运行整条流水线（不带 write_opts）；
- ``task.dataset.import``：数据集导入的解析（inspect）/提交（commit）；
- ``task.dataset.migrate``：成品数据集异步迁移为人工数据集；
- ``super_assistant.reflect.micro/full/focused``：超级助手三种自我进化
  反思任务（micro 每轮后 / full 手动 / focused 定向技能）；
- ``super_assistant.palace.extract/consolidate``：记忆宫殿图谱抽取与
  定期聚类合并。

每个 subject 使用独立 durable pull consumer，共享进程级并发信号量。
启动方式::

    python -m app.data_channel.pipeline_tasks.nats_executor

已知语义：进程被打断时，in-flight 任务表现为「执行中断」，其数据库租约
最长 6 小时（``engine._TASK_LEASE``）后过期，再由 Web 进程内的对账器
（默认 5 分钟周期，``reconciler.reconcile_pipeline_executions``）收口为
failed——与此前部署打断内联执行的语义一致。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = "/tmp/nats_executor.heartbeat"
# compose stop 默认宽限 30 秒，这里留出 5 秒余量
_SHUTDOWN_TIMEOUT_SECONDS = 25
# ack_wait=30s，执行期间每 20 秒续约一次防止重投
_IN_PROGRESS_INTERVAL_SECONDS = 20
_FETCH_TIMEOUT_SECONDS = 5
_HEARTBEAT_INTERVAL_SECONDS = 5
_CONSUMER_DURABLE = "pipeline-executor"
_PIPELINE_RUN_DURABLE = "pipeline-run-executor"
_DATASET_IMPORT_DURABLE = "dataset-import-executor"
_MAPPING_APPLY_DURABLE = "mapping-apply-executor"
_CONNECTION_SYNC_DURABLE = "connection-sync-executor"
_DATASET_EVENT_DURABLE = "dataset-event-executor"
_DATASET_MIGRATE_DURABLE = "dataset-migrate-executor"
_SUPER_ASSISTANT_REFLECT_MICRO_DURABLE = "super-assistant-reflect-micro"
_ASSISTANT_EVAL_AUTOPILOT_DURABLE = "assistant-eval-autopilot"
_SUPER_ASSISTANT_REFLECT_FULL_DURABLE = "super-assistant-reflect-full"
_SUPER_ASSISTANT_REFLECT_FOCUSED_DURABLE = "super-assistant-reflect-focused"
_SUPER_ASSISTANT_PALACE_EXTRACT_DURABLE = "super-assistant-palace-extract"
_SUPER_ASSISTANT_PALACE_CONSOLIDATE_DURABLE = "super-assistant-palace-consolidate"
_ONTOLOGY_DOCUMENT_PUBLISHED_DURABLE = "ontology-documents-published"
_EXECUTION_KERNEL_DURABLE = "sa-kernel-v1"
_EXECUTION_CALL_DURABLE = "sa-call-v1"
_EXECUTION_RECONCILER_DURABLE = "sa-reconciler-v1"
_PLUGIN_RUNNER_REPLY_DURABLE = "sa-plugin-reply-v1"

# 消息处理器：解析后的 payload → 协程；业务异常必须在 handler 内消化，
# 逃到 ``_process_message`` 的异常一律 nak 重投
MessageHandler = Callable[[dict], Awaitable[None]]


def _touch_heartbeat() -> None:
    """刷新心跳文件 mtime，供 compose 健康检查判断主循环存活。"""
    now = time.time()
    fd = os.open(HEARTBEAT_PATH, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    os.close(fd)
    os.utime(HEARTBEAT_PATH, (now, now))


async def _execute_pipeline_task_message(payload: dict) -> None:
    """pipeline.task.execute：数据任务池调度/手动触发的单任务执行。"""
    task_id = str(payload["task_id"])
    trigger_type = str(payload.get("trigger_type") or "manual")
    full_refresh = bool(payload.get("full_refresh", False))

    from app.data_channel.pipeline_tasks.engine import execute_pipeline_task

    result = await asyncio.to_thread(
        execute_pipeline_task, task_id, trigger_type, full_refresh)
    if isinstance(result, dict) and result.get("status") == "ok":
        logger.info("PipelineTask %s 执行完成", task_id)
    else:
        # claim 冲突（"任务正在执行中"）与业务失败都算送达成功：
        # 正确性由数据库租约兜底，消息直接 ack 丢弃
        logger.warning(
            "PipelineTask %s 执行未成功: %s",
            task_id,
            (result or {}).get("error") if isinstance(result, dict) else result,
        )


async def _run_pipeline_run_message(payload: dict) -> None:
    """task.pipeline.run：UI 手动运行整条流水线（不带 write_opts）。"""
    pipeline_id = str(payload["pipeline_id"])
    run_id = str(payload["run_id"])

    from app.tasks.v2.pipeline_run import pipeline_run_task

    # Celery 包装下裸函数在 .run 上；无 Celery 环境时本体即可调用
    run_fn = getattr(pipeline_run_task, "run", pipeline_run_task)
    await asyncio.to_thread(run_fn, pipeline_id, run_id)
    logger.info("Pipeline %s 运行 %s 执行完成", pipeline_id, run_id)


async def _run_dataset_import_message(payload: dict) -> None:
    """task.dataset.import：数据集导入的解析/提交两个阶段。"""
    job_id = str(payload["job_id"])
    kind = str(payload["kind"])

    from app.tasks.v2 import dataset_import

    task = {
        "inspect": dataset_import.inspect_dataset_import,
        "commit": dataset_import.commit_dataset_import,
    }.get(kind)
    if task is None:
        raise ValueError(f"未知的数据集导入任务类型: {kind!r}")
    # 同 pipeline_run_task：兼容 Celery 包装与裸函数两种形态
    run_fn = getattr(task, "run", task)
    await asyncio.to_thread(run_fn, job_id)
    logger.info("数据集导入任务 %s（%s）执行完成", job_id, kind)


async def _run_dataset_migrate_message(payload: dict) -> None:
    """task.dataset.migrate：成品数据集异步迁移为人工数据集。"""
    job_id = str(payload["job_id"])
    source_dataset_id = str(payload["source_dataset_id"])

    from app.tasks.v2 import dataset_migration

    await asyncio.to_thread(
        dataset_migration.migrate_curated_to_manual,
        job_id,
        source_dataset_id,
    )
    logger.info("数据集迁移任务 %s 执行完成", job_id)


async def _run_mapping_apply_message(payload: dict) -> None:
    """task.mapping.apply：增量编排触发的 Mapping 异步应用。"""
    from app.tasks.v2.mapping_apply import mapping_apply_task

    await asyncio.to_thread(
        mapping_apply_task, str(payload["mapping_id"]), str(payload["ontology_id"]))
    logger.info("Mapping %s 应用完成", payload["mapping_id"])


async def _run_connection_sync_message(payload: dict) -> None:
    """task.connection.sync：手动触发的 Connection 异步数据同步。"""
    from app.tasks.v2.connection_sync import sync_connection

    result = await asyncio.to_thread(
        sync_connection,
        str(payload["connection_id"]),
        str(payload.get("mode") or "full"),
        payload.get("resource") or None,
    )
    logger.info("Connection %s 异步同步完成: %s", payload["connection_id"],
                result.get("status") if isinstance(result, dict) else result)


async def _run_dataset_event_message(payload: dict) -> None:
    """task.dataset.event：已 claim 的 DatasetVersion 事件终态处理。"""
    from app.tasks.v2.dataset_event_processing import process_dataset_version_event

    await asyncio.to_thread(
        process_dataset_version_event,
        str(payload["event_id"]), str(payload["claim_token"]))
    logger.info("DatasetVersion 事件 %s 处理完成", payload["event_id"])


def _handler_registry():
    """subject → (durable, handler)：每 subject 独立 durable pull consumer。"""
    from app.data_channel.pipeline_tasks.dispatch import (
        ASSISTANT_EVAL_AUTOPILOT_SUBJECT,
        CONNECTION_SYNC_SUBJECT,
        DATASET_EVENT_SUBJECT,
        DATASET_IMPORT_SUBJECT,
        DATASET_MIGRATE_SUBJECT,
        MAPPING_APPLY_SUBJECT,
        ONTOLOGY_DOCUMENT_PUBLISHED_SUBJECT,
        PIPELINE_EXECUTE_SUBJECT,
        PIPELINE_RUN_SUBJECT,
        SUPER_ASSISTANT_PALACE_CONSOLIDATE_SUBJECT,
        SUPER_ASSISTANT_PALACE_EXTRACT_SUBJECT,
        SUPER_ASSISTANT_REFLECT_FOCUSED_SUBJECT,
        SUPER_ASSISTANT_REFLECT_FULL_SUBJECT,
        SUPER_ASSISTANT_REFLECT_MICRO_SUBJECT,
    )
    from app.assistant_evaluation import autopilot_tasks
    from app.super_assistant import palace_tasks, reflection_tasks

    return (
        (PIPELINE_EXECUTE_SUBJECT, _CONSUMER_DURABLE, _execute_pipeline_task_message),
        (PIPELINE_RUN_SUBJECT, _PIPELINE_RUN_DURABLE, _run_pipeline_run_message),
        (DATASET_IMPORT_SUBJECT, _DATASET_IMPORT_DURABLE, _run_dataset_import_message),
        (MAPPING_APPLY_SUBJECT, _MAPPING_APPLY_DURABLE, _run_mapping_apply_message),
        (CONNECTION_SYNC_SUBJECT, _CONNECTION_SYNC_DURABLE, _run_connection_sync_message),
        (DATASET_EVENT_SUBJECT, _DATASET_EVENT_DURABLE, _run_dataset_event_message),
        (
            SUPER_ASSISTANT_REFLECT_MICRO_SUBJECT,
            _SUPER_ASSISTANT_REFLECT_MICRO_DURABLE,
            reflection_tasks.run_micro_reflection_message,
        ),
        (
            SUPER_ASSISTANT_REFLECT_FULL_SUBJECT,
            _SUPER_ASSISTANT_REFLECT_FULL_DURABLE,
            reflection_tasks.run_full_reflection_message,
        ),
        (
            SUPER_ASSISTANT_REFLECT_FOCUSED_SUBJECT,
            _SUPER_ASSISTANT_REFLECT_FOCUSED_DURABLE,
            reflection_tasks.run_focused_reflection_message,
        ),
        (
            DATASET_MIGRATE_SUBJECT,
            _DATASET_MIGRATE_DURABLE,
            _run_dataset_migrate_message,
        ),
        (
            ASSISTANT_EVAL_AUTOPILOT_SUBJECT,
            _ASSISTANT_EVAL_AUTOPILOT_DURABLE,
            autopilot_tasks.run_autopilot_cycle_message,
        ),
        (
            SUPER_ASSISTANT_PALACE_EXTRACT_SUBJECT,
            _SUPER_ASSISTANT_PALACE_EXTRACT_DURABLE,
            palace_tasks.run_palace_extract_message,
        ),
        (
            SUPER_ASSISTANT_PALACE_CONSOLIDATE_SUBJECT,
            _SUPER_ASSISTANT_PALACE_CONSOLIDATE_DURABLE,
            palace_tasks.run_palace_consolidate_message,
        ),
        (
            ONTOLOGY_DOCUMENT_PUBLISHED_SUBJECT,
            _ONTOLOGY_DOCUMENT_PUBLISHED_DURABLE,
            palace_tasks.run_palace_ontology_document_message,
        ),
    )


async def _run_kernel_execution_message(payload: dict) -> None:
    from app.super_assistant.kernel.runtime import process_execution_message

    await process_execution_message(payload)


async def _run_kernel_call_message(payload: dict) -> None:
    from app.super_assistant.kernel.runtime import process_external_call_message

    # A call message is the only durable trigger for the initial provider
    # invocation.  The runtime returns False when it could not persist a
    # state transition (for example a transient DB failure); acknowledging
    # that message would strand a waiting Call with no remote task reference.
    applied = await process_external_call_message(payload)
    if applied is False:
        raise RuntimeError("kernel external call was not applied")


async def _run_kernel_reconcile_message(payload: dict) -> None:
    from app.super_assistant.kernel.runtime import reconcile_execution_message

    # ``reconcile_execution_message`` deliberately keeps a narrow direct-call
    # contract and returns ``False`` when the observation could not be
    # durably applied (for example a transient database failure).  Do not ack
    # such a message: an ack here would permanently lose the provider
    # observation and leave the Call waiting forever.  Missing/stale
    # run/call references are treated as successful no-ops by the runtime and
    # therefore still ack normally.
    applied = await reconcile_execution_message(payload)
    if applied is False:
        raise RuntimeError("kernel reconciliation observation was not applied")


async def _run_plugin_runner_reply_message(payload: dict) -> None:
    """Validate one runner event and enqueue it for the Kernel reconciler.

    The reply consumer never mutates a Call directly.  It only persists an
    owner/run/call-bound observation in the existing outbox, preserving the
    single state-transition authority and provider-event idempotency rules.
    """
    from app.super_assistant.kernel.plugin_runner import PluginRunnerEventEnvelope
    from app.super_assistant.kernel.store import enqueue_reconcile_observation
    from app.super_assistant.kernel.models import ExecutionCall, ExecutionRun
    from app.super_assistant.models import SuperAssistantProcessPlugin
    from app.shared.database import SessionLocal
    from sqlalchemy import select

    try:
        event = PluginRunnerEventEnvelope.from_payload(payload)
    except Exception as exc:
        logger.error("invalid plugin runner reply envelope: %s", str(exc)[:300])
        return

    db = SessionLocal()
    try:
        run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == event.run_id).with_for_update())
        call = db.scalar(select(ExecutionCall).where(
            ExecutionCall.id == event.call_id, ExecutionCall.run_id == event.run_id,
        ).with_for_update())
        plugin = db.scalar(select(SuperAssistantProcessPlugin).where(
            SuperAssistantProcessPlugin.owner_id == event.owner_id,
            (SuperAssistantProcessPlugin.id == event.plugin_id)
            | (SuperAssistantProcessPlugin.key == event.plugin_id),
        ))
        if run is None or call is None or run.owner_id != event.owner_id or plugin is None:
            db.rollback()
            return
        if call.capability_revision != event.capability_revision or plugin.revision != event.revision:
            db.rollback()
            logger.error("plugin runner reply revision mismatch for call=%s", event.call_id)
            return
        if event.manifest_hash != plugin.manifest_hash or event.request_id != f"plugin-call:{call.id}":
            db.rollback()
            logger.error("plugin runner reply invocation identity mismatch for call=%s", event.call_id)
            return
        if call.target_ref not in {plugin.id, plugin.key}:
            db.rollback()
            logger.error("plugin runner reply target mismatch for call=%s", event.call_id)
            return
        state = {
            "progress": "running", "artifact": "running",
            "completed": "completed", "failed": "failed",
            "cancelled": "cancelled", "unknown": "unknown",
            # Approval is intentionally left unresolved until the adapter has
            # a durable Approval/Inbox mapping; never claim completion.
            "approval_requested": "unknown",
        }[event.kind]
        content = ""
        for key in ("content", "message", "summary"):
            value = event.payload.get(key)
            if isinstance(value, str):
                content = value[:20000]
                break
        artifacts = [
            {
                "kind": "external.artifact", "name": item["name"],
                "mime_type": item["mime_type"], "size": item["size"],
                "checksum": item["checksum"], "storage_ref": item["artifact_ref"],
            }
            for item in event.artifacts
        ]
        enqueue_reconcile_observation(
            db, run=run, call=call,
            observation={
                "remote_state": state, "status": state,
                "provider_event_id": f"{event.request_id}:{event.event_seq}",
                "connector_id": f"plugin-runner:{event.plugin_id}",
                "content": content, "artifacts": artifacts,
                "evidence_ref": artifacts[0]["storage_ref"] if artifacts else None,
                "event_kind": event.kind, "event_payload": dict(event.payload),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("plugin runner reply could not be persisted for call=%s", event.call_id)
        raise
    finally:
        db.close()


def _execution_handler_registry():
    """kernel.v1 使用独立 stream，但复用本 executor 进程和并发治理。"""
    from app.data_channel.pipeline_tasks.dispatch import (
        EXECUTION_CALL_SUBJECT,
        EXECUTION_RECONCILE_SUBJECT,
        EXECUTION_RUN_SUBJECT,
    )
    return (
        (EXECUTION_RUN_SUBJECT, _EXECUTION_KERNEL_DURABLE, _run_kernel_execution_message),
        (EXECUTION_CALL_SUBJECT, _EXECUTION_CALL_DURABLE, _run_kernel_call_message),
        (EXECUTION_RECONCILE_SUBJECT, _EXECUTION_RECONCILER_DURABLE, _run_kernel_reconcile_message),
    )


class PipelineExecutor:
    """流水线执行消费者：拉取消息、限并发执行、按结果 ack/nak。"""

    def __init__(self) -> None:
        from app.config import settings

        self._concurrency = max(
            1, int(settings.pipeline_executor_concurrency or 2)
        )
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._shutdown = asyncio.Event()
        self._in_flight: set[asyncio.Task] = set()

    def request_shutdown(self) -> None:
        """信号回调：停止拉取与续约，在途执行最多再宽限一段后退出。"""
        if not self._shutdown.is_set():
            logger.info("流水线 executor 收到关闭信号，停止拉取新消息")
        self._shutdown.set()

    async def _process_message(
        self,
        msg,
        handler: MessageHandler,
        description: str,
    ) -> None:
        """执行一条消息并按结果 ack/nak；执行期间周期续约。"""
        try:
            payload = json.loads(msg.data.decode())
        except Exception:
            # 不可解析的消息无法通过重投修复：nak 只会空烧投递次数，
            # 必须显式 ack 丢弃（数据库侧的事件幂等仍是最终防线）。
            logger.error("%s 消息无法解析，ack 丢弃: %r", description, msg.data)
            await msg.ack()
            return

        execute = asyncio.ensure_future(handler(payload))
        while True:
            done, _ = await asyncio.wait(
                {execute}, timeout=_IN_PROGRESS_INTERVAL_SECONDS
            )
            if done:
                break
            if self._shutdown.is_set():
                # 关闭流程中不再续约，但仍等执行线程自然结束；消息若因
                # ack_wait 到期被重投，由数据库租约/文件状态机拦下重复执行
                continue
            try:
                await msg.in_progress()
            except Exception:
                logger.exception("%s 执行消息续约失败", description)
        try:
            execute.result()
        except Exception:
            # handler 内部消化所有业务异常；能逃到这里的是解析/线程级意外，
            # 交给 nak 重投。带延迟防止瞬时故障瞬间烧完投递——丢一条
            # kernel 派发消息意味着 Run 挂死到 deadline 兜底。
            logger.exception("%s 执行异常逃逸", description)
            await msg.nak(delay=5)
            return
        await msg.ack()

    async def _run_message(
        self,
        msg,
        handler: MessageHandler,
        description: str,
    ) -> None:
        try:
            await self._process_message(msg, handler, description)
        except Exception:
            logger.exception("%s 消息处理异常", description)
            try:
                await msg.nak(delay=5)
            except Exception:
                logger.exception("%s 消息 nak 失败", description)
        finally:
            self._semaphore.release()

    async def _fetch_loop(
        self,
        subscription,
        handler: MessageHandler,
        description: str,
    ) -> None:
        """批量拉取消息并限并发派发执行；关闭信号到来时退出循环。"""
        while not self._shutdown.is_set():
            try:
                messages = await subscription.fetch(
                    batch=self._concurrency, timeout=_FETCH_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                # nats.py 拉取超时即为空批次，继续等下一批/关闭信号
                continue
            except Exception:
                if self._shutdown.is_set():
                    break
                logger.exception("%s 消息拉取失败，5 秒后重试", description)
                await asyncio.sleep(5)
                continue
            for msg in messages:
                if self._shutdown.is_set():
                    # 已拉取但未开始的消息立即 nak，让其他 executor 尽快接管
                    try:
                        await msg.nak()
                    except Exception:
                        logger.exception("关闭时退回未执行消息失败")
                    continue
                await self._semaphore.acquire()
                if self._shutdown.is_set():
                    # 等待信号量期间收到关闭信号：同样不再启动新执行
                    self._semaphore.release()
                    try:
                        await msg.nak()
                    except Exception:
                        logger.exception("关闭时退回未执行消息失败")
                    continue
                task = asyncio.ensure_future(
                    self._run_message(msg, handler, description)
                )
                self._in_flight.add(task)
                task.add_done_callback(self._in_flight.discard)

    async def _heartbeat_loop(self) -> None:
        while True:
            _touch_heartbeat()
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)

    async def run(self) -> None:
        import nats
        from nats.js.api import ConsumerConfig

        from app.config import settings
        from app.data_channel.pipeline_tasks.dispatch import (
            PIPELINE_STREAM,
            ensure_pipeline_stream,
            EXECUTION_STREAM,
            ensure_execution_stream,
        )
        from app.super_assistant.kernel.plugin_runner import PLUGIN_RUNNER_REPLY_PREFIX, PLUGIN_RUNNER_STREAM
        from app.super_assistant.kernel.plugin_runner_service import ensure_plugin_runner_stream

        nc = await nats.connect(settings.nats_url.strip(), connect_timeout=3)
        heartbeat = asyncio.ensure_future(self._heartbeat_loop())
        try:
            js = nc.jetstream()
            await ensure_pipeline_stream(js)
            await ensure_execution_stream(js)
            await ensure_plugin_runner_stream(js)
            loops = []
            for subject, durable, handler in _handler_registry():
                subscription = await js.pull_subscribe(
                    subject,
                    durable=durable,
                    stream=PIPELINE_STREAM,
                    config=ConsumerConfig(ack_wait=60),
                )
                logger.info(
                    "流水线 executor 已订阅 %s（durable=%s）",
                    subject,
                    durable,
                )
                loops.append(
                    asyncio.ensure_future(
                        self._fetch_loop(subscription, handler, subject)
                    )
                )
            for subject, durable, handler in _execution_handler_registry():
                subscription = await js.pull_subscribe(
                    subject,
                    durable=durable,
                    stream=EXECUTION_STREAM,
                    config=ConsumerConfig(ack_wait=60),
                )
                logger.info(
                    "kernel executor 已订阅 %s（durable=%s）", subject, durable,
                )
                loops.append(
                    asyncio.ensure_future(
                        self._fetch_loop(subscription, handler, subject)
                    )
                )
            reply_subscription = await js.pull_subscribe(
                f"{PLUGIN_RUNNER_REPLY_PREFIX}*",
                durable=_PLUGIN_RUNNER_REPLY_DURABLE,
                stream=PLUGIN_RUNNER_STREAM,
                config=ConsumerConfig(ack_wait=60),
            )
            loops.append(asyncio.ensure_future(self._fetch_loop(
                reply_subscription, _run_plugin_runner_reply_message,
                "plugin-runner-reply",
            )))
            logger.info(
                "流水线 executor 已启动（并发上限 %d，%d 个 subject）",
                self._concurrency,
                len(loops),
            )
            await asyncio.gather(*loops)
            if self._in_flight:
                logger.info(
                    "等待 %d 个在途执行结束（至多 %d 秒）",
                    len(self._in_flight),
                    _SHUTDOWN_TIMEOUT_SECONDS,
                )
                _, pending = await asyncio.wait(
                    self._in_flight, timeout=_SHUTDOWN_TIMEOUT_SECONDS
                )
                for task in pending:
                    task.cancel()
            logger.info("流水线 executor 已退出")
        finally:
            heartbeat.cancel()
            try:
                await nc.drain()
            except Exception:
                logger.exception("流水线 executor 关闭 NATS 连接失败")


async def _async_main() -> int:
    from app.config import settings

    nats_url = (settings.nats_url or "").strip()
    if not nats_url:
        print(
            "流水线 executor 启动失败：未配置 NATS_URL（JetStream 消息通道），"
            "生产环境必须显式配置后再启动",
            file=sys.stderr,
        )
        return 1

    executor = PipelineExecutor()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, executor.request_shutdown)
        except (NotImplementedError, RuntimeError):  # 非 Unix 事件循环
            pass
    await executor.run()
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # executor 进程不经过 app.main 的导入链，必须自行注册全部表映射——
    # 否则首次 INSERT 的依赖排序解析不到 FK 目标表（如 v2_dataset_versions），
    # 运行记录初始化会以 NoReferencedTableError 失败。
    from app.model_registry import import_all_models

    import_all_models()
    raise SystemExit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
