"""FastAPI process lifecycle orchestration.

The ordering in this module is a runtime contract: database preparation and
owned background services start in the same sequence they historically used in
``app.main`` and shut down in the reverse ownership-safe sequence.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings


_main_logger = logging.getLogger("app.main")


@asynccontextmanager
async def application_lifespan(
    app: FastAPI,
    *,
    seed_database: Callable[[], None],
) -> AsyncIterator[None]:
    sentinel_attempted = False
    data_scheduler = None
    data_scheduler_attempted = False
    file_cleanup_task = None
    perf_monitor_started = False
    runtime_resources_started = False
    try:
        seed_database()

        if settings.environment != "test":
            # Probe every configured integration before starting any owned
            # background thread. Chromium CDP and (outside production) n8n
            # are handled as advisory by this probe; PostgreSQL, Redis,
            # Neo4j, MinIO and production n8n fail closed.
            from app.shared.dependency_probe import (
                probe_startup_dependencies,
            )

            probe_startup_dependencies()

        # 初始化 Neo4j 索引。除显式单元测试环境外，索引初始化是启动契约，
        # 不能让应用在缺少图约束/索引时继续对外服务。
        try:
            from app.ontologies.graph.index_setup import setup_indexes

            index_result = setup_indexes()
            if settings.environment != "test" and (
                index_result.get("status") != "done"
                or any(
                    item.get("status") != "ok"
                    for item in index_result.get("results", [])
                )
            ):
                raise RuntimeError(
                    f"Neo4j index setup incomplete: {index_result}"
                )
        except Exception as exc:
            if settings.environment != "test":
                raise RuntimeError(
                    "Neo4j indexes failed to initialize"
                ) from exc

        # Migrations deliberately fence every pre-existing ontology because
        # its older Neo4j shape cannot be assumed to satisfy the stable-ID
        # contract. Reconcile before any worker or request can observe it.
        if settings.environment != "test":
            try:
                from app.ontologies.projection_state import (
                    repair_unready_projections,
                )

                repaired = repair_unready_projections()
                if repaired:
                    _main_logger.info(
                        "Validated %s ontology Neo4j projection(s) at startup",
                        repaired,
                    )
            except Exception as exc:
                raise RuntimeError(
                    "Ontology Neo4j projections failed to initialize"
                ) from exc

        # External state is now validated. Only after that barrier may the
        # process create background workers which would otherwise leak when a
        # later startup check fails.
        from app.api_hub import db as api_hub_db

        api_hub_db.init_db()

        # 哨兵引擎：注册 CDC(监听对象改动→变化驱动) + 启动定期扫描 worker
        try:
            from app.services.sentinel import register_cdc, start_scan_worker

            sentinel_attempted = True
            register_cdc(start_worker=True)
            sentinel_started = start_scan_worker()
            if settings.environment == "production" and not sentinel_started:
                raise RuntimeError(
                    "Sentinel scan worker is disabled or failed to start"
                )
        except Exception as exc:
            if settings.environment == "production":
                raise RuntimeError(
                    "Sentinel engine failed to initialize"
                ) from exc
            _main_logger.warning("Sentinel 启动失败: %s", exc)

        # 启动数据同步任务调度器（后台线程）
        try:
            from app.data_channel.sync_tasks.scheduler import get_sync_scheduler

            data_scheduler = get_sync_scheduler()
            data_scheduler_attempted = True
            data_scheduler.start()
            if settings.environment == "production" and not data_scheduler.healthy:
                raise RuntimeError(
                    "Data scheduler is not healthy: "
                    f"{data_scheduler.last_error or 'not running'}"
                )
        except Exception as exc:
            if settings.environment == "production":
                raise RuntimeError(
                    "Data scheduler failed to initialize"
                ) from exc
            _main_logger.warning("SyncScheduler 启动失败: %s", exc)

        # 助手评估任务恢复：queued 重排、running 标记中断（旁路能力，失败不阻断启动）
        if settings.environment != "test":
            try:
                from app.assistant_evaluation.service import (
                    recover_interrupted_tasks,
                )

                recovery = recover_interrupted_tasks()
                if recovery.get("requeued") or recovery.get("interrupted"):
                    _main_logger.info(
                        "助手评估任务恢复：重排 %s 个、标记中断 %s 个",
                        recovery.get("requeued"), recovery.get("interrupted"),
                    )
            except Exception as exc:
                _main_logger.warning("助手评估任务恢复失败: %s", exc)

        # 超级助手会话恢复：进程重启遗留的 streaming 回复标记中断（旁路能力，
        # 失败不阻断启动——与助手评估任务恢复同策略）
        if settings.environment != "test":
            try:
                from app.super_assistant.conversation_service import (
                    recover_interrupted_streams,
                )

                stream_recovery = recover_interrupted_streams()
                if stream_recovery.get("interrupted"):
                    _main_logger.info(
                        "超级助手会话恢复：标记中断 %s 条遗留 streaming 回复",
                        stream_recovery.get("interrupted"),
                    )
            except Exception as exc:
                _main_logger.warning("超级助手会话恢复失败: %s", exc)

        # 助手评估值守定时器（APScheduler 进程内定时 + NATS 派发；旁路能力，
        # 失败不阻断启动——与任务恢复同策略）
        if settings.environment != "test":
            try:
                from app.assistant_evaluation.autopilot_scheduler import (
                    start as start_autopilot_scheduler,
                )

                start_autopilot_scheduler()
            except Exception as exc:
                _main_logger.warning("助手评估值守定时器启动失败: %s", exc)

        # 记忆宫殿聚类合并定时器（每天 03:00 为有图谱的用户派发合并任务；
        # APScheduler 进程内定时 + NATS JetStream，旁路能力，失败不阻断启动）
        if settings.environment != "test":
            try:
                from app.super_assistant.palace_consolidate import (
                    start as start_palace_consolidate,
                )

                start_palace_consolidate()
            except Exception as exc:
                _main_logger.warning("记忆宫殿聚类合并定时器启动失败: %s", exc)

        from app.data_channel.file_assets.service import (
            file_asset_cleanup_loop,
        )

        file_cleanup_task = asyncio.create_task(file_asset_cleanup_loop())

        # API 性能监控：聚合刷新与保留清理的后台维护循环。
        # 失败仅告警不阻断启动——监控是旁路能力，不能影响平台可用性。
        try:
            from app.platform.observability import collector as perf_collector

            perf_collector.collector.start()
            perf_monitor_started = True
        except Exception as exc:
            _main_logger.warning("API 性能监控后台任务启动失败: %s", exc)

        runtime_resources_started = True
        yield
    finally:
        if file_cleanup_task is not None:
            file_cleanup_task.cancel()
            try:
                await file_cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                _main_logger.exception("File cleanup task shutdown failed")
        if perf_monitor_started:
            try:
                from app.platform.observability import collector as perf_collector

                await perf_collector.collector.stop()
            except Exception:  # noqa: BLE001
                _main_logger.exception("API 性能监控后台任务关闭失败")
        if runtime_resources_started:
            try:
                from app.data_channel.steward.browser_runtime import (
                    browser_manager,
                )

                browser_manager.close_all()
            except Exception:  # noqa: BLE001
                _main_logger.exception("Browser runtime cleanup failed")
        if data_scheduler_attempted and data_scheduler is not None:
            try:
                data_scheduler.shutdown()
            except Exception:  # noqa: BLE001
                _main_logger.exception("Data scheduler cleanup failed")
        if sentinel_attempted:
            try:
                from app.services.sentinel import (
                    stop_cdc_worker,
                    stop_scan_worker,
                )

                # Stop the producer first, then the durable outbox consumer.
                stop_scan_worker()
                stop_cdc_worker()
            except Exception:  # noqa: BLE001
                _main_logger.exception("Sentinel cleanup failed")
        try:
            from app.super_assistant import palace_consolidate

            palace_consolidate.shutdown()
        except Exception:  # noqa: BLE001
            _main_logger.exception("Palace consolidate scheduler cleanup failed")
