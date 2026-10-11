"""DatasetVersion 事件异步处理任务体（NATS executor 线程内直调）。

API 进程的 drain 线程只负责 claim + 派发；映射全量对账、Neo4j 整图重建与
哨兵屏障等重活在本 worker 进程内完成，完成/失败后由本任务确认 durable 事件
（claim CAS 保证只有当前 owner 能确认）。任务中途崩溃时事件保持 claimed，
超过 claim 超时后由 drain 重新派发——映射对账幂等、哨兵 match-state 与
动作幂等键防止重复副作用。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def process_dataset_version_event(event_id: str, claim_token: str):
    """处理一个已被 claim 的 DatasetVersion 事件并确认其终态。"""
    from app.ontologies.sentinels.cdc import register_cdc
    from app.data_channel.datasets.version_events import run_claimed_event

    # executor 进程不运行 FastAPI 的 lifespan（CDC 监听在 API 进程注册）。
    # 事件处理内含同步哨兵屏障，必须先注册 CDC 使 Formal 投影提交时原子产生
    # durable outbox 行；注册仅开启监听不启动第二个消费者（与 API 进程的
    # 恢复 worker 分工一致，见 mapping_apply 任务）。
    register_cdc(start_worker=False)

    try:
        result = run_claimed_event(event_id, claim_token)
    except RuntimeError as exc:
        # claim 已被其他 worker 接管（claim 超时回收或并发重派）：本条
        # 投递已无意义，必须就地消化——外抛会 nak 无限重投，双 executor
        # 部署下放大为重投风暴（2026-10-11 生产实例：4 条积压事件各
        # 重试 51+ 次吃满 8 核）。
        if "claim lost" in str(exc):
            logger.warning(
                "DatasetVersion event %s claim 已被接管，丢弃本条投递",
                event_id)
            return "lost_claim"
        raise
    logger.info("DatasetVersion event %s completed in worker", event_id)
    return result
