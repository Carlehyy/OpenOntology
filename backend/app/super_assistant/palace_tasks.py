"""记忆宫殿图谱抽取的 NATS 消息处理器（executor 进程内运行）。

与反思 handler 同一模式；两点差异：

1. 抽取是分钟级长任务，handler 内用进程级信号量（_palace_semaphore）
   把本类任务的并发压到配置上限
   （settings.super_assistant_palace_extract_concurrency，默认 1）。该闸
   仍与 executor 的全局并发信号量相互隔离：全局闸至少保留一个名额给
   reflect.* 等短任务，杜绝建图饿死反思。
2. 消息等待期间由 executor 的 in_progress 周期续约兜底，信号量排队不会
   触发 ack_wait 重投；业务异常在 handler 内消化（build 行自身记 error），
   不向外抛——逃到 executor 的异常会被 nak 重投，而抽取失败重投无意义。

consolidate（聚类合并）是轻量离线任务：不经抽取信号量，独立开 Session
同步执行，异常同样内消化。
"""
from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

# 进程级抽取并发闸（并发隔离纪律）：按配置值缓存，首个 handler 到达时构建
_PALACE_SLOT: threading.Semaphore | None = None
_PALACE_SLOT_LOCK = threading.Lock()


def _palace_semaphore() -> threading.Semaphore:
    """抽取并发闸：读 settings 缓存构建；配置项未就绪时防御回退 1。"""
    global _PALACE_SLOT
    with _PALACE_SLOT_LOCK:
        if _PALACE_SLOT is None:
            from app.shared.config import settings

            concurrency = max(
                1,
                int(
                    getattr(
                        settings, "super_assistant_palace_extract_concurrency", 1,
                    ) or 1
                ),
            )
            _PALACE_SLOT = threading.Semaphore(concurrency)
        return _PALACE_SLOT


async def run_palace_extract_message(payload: dict) -> None:
    """super_assistant.palace.extract：单文件的图谱抽取。"""
    owner_id = str(payload["owner_id"])
    file_id = str(payload["file_id"])

    from app.database import SessionLocal
    from app.super_assistant import palace_service

    def _run() -> None:
        with _palace_semaphore():
            db = SessionLocal()
            try:
                palace_service.run_build(db, owner_id, file_id)
                logger.info("记忆宫殿图谱抽取完成（file=%s）", file_id)
            except Exception:
                logger.exception("记忆宫殿图谱抽取执行失败（file=%s）", file_id)
            finally:
                db.close()

    await asyncio.to_thread(_run)


async def run_palace_ontology_document_message(payload: dict) -> None:
    """ontology.documents.published：本体发布态业务文档的共享镜像建图。

    与抽取同一并发闸（本体文档建图也是分钟级 LLM 长任务）；摄取+建图整体
    置于信号量内，同本体的事件自然串行。业务异常在 handler 内消化
    （行内 status=failed 记 error），每日对账按指纹自愈重试。
    """
    ontology_id = str(payload.get("ontology_id") or "?")

    from app.database import SessionLocal
    from app.super_assistant import palace_service

    def _run() -> None:
        with _palace_semaphore():
            db = SessionLocal()
            try:
                palace_service.ingest_ontology_document(db, payload)
                logger.info("本体文档共享建图处理完成（ontology=%s）", ontology_id)
            except Exception:
                logger.exception("本体文档共享建图执行失败（ontology=%s）", ontology_id)
            finally:
                db.close()

    await asyncio.to_thread(_run)


async def run_palace_consolidate_message(payload: dict) -> None:
    """super_assistant.palace.consolidate：用户图谱的定期聚类合并。

    合并本身轻量且离线（候选预筛在内存、确认只有一次 LLM 调用），
    不占抽取信号量；失败等下一调度周期重试即可，nak 重投无意义。
    """
    owner_id = str(payload["owner_id"])

    from app.database import SessionLocal
    from app.super_assistant import palace_consolidate

    def _run() -> None:
        db = SessionLocal()
        try:
            result = palace_consolidate.run_consolidation(db, owner_id)
            logger.info(
                "记忆宫殿聚类合并完成（owner=%s，候选 %d 组，确认合并 %d 组 %d 个实体）",
                owner_id,
                result.get("candidates") or 0,
                len(result.get("merged_groups") or []),
                result.get("merged_entities") or 0,
            )
        except Exception:
            logger.exception("记忆宫殿聚类合并执行失败（owner=%s）", owner_id)
        finally:
            db.close()

    await asyncio.to_thread(_run)
