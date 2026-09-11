"""
流水线任务 NATS JetStream 派发

Web 进程只负责「送达」：把触发请求发布到 JetStream 工作队列流，由独立
executor 进程消费执行。重复消息无害——执行引擎的数据库原子租约
（``engine._claim_task``）保证同一任务最多一个执行者领取成功，重复
投递在 claim 失败返回「任务正在执行中」后直接 ack 丢弃；UI 手动运行与
数据集导入则以各自的数据库/文件状态机兜底幂等。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

PIPELINE_STREAM = "PIPELINE_TASKS"
PIPELINE_EXECUTE_SUBJECT = "pipeline.task.execute"
PIPELINE_RUN_SUBJECT = "task.pipeline.run"
MAPPING_APPLY_SUBJECT = "task.mapping.apply"
CONNECTION_SYNC_SUBJECT = "task.connection.sync"
DATASET_EVENT_SUBJECT = "task.dataset.event"
DATASET_IMPORT_SUBJECT = "task.dataset.import"
DATASET_MIGRATE_SUBJECT = "task.dataset.migrate"
SUPER_ASSISTANT_REFLECT_MICRO_SUBJECT = "super_assistant.reflect.micro"
SUPER_ASSISTANT_REFLECT_FULL_SUBJECT = "super_assistant.reflect.full"
SUPER_ASSISTANT_REFLECT_FOCUSED_SUBJECT = "super_assistant.reflect.focused"
SUPER_ASSISTANT_PALACE_EXTRACT_SUBJECT = "super_assistant.palace.extract"
SUPER_ASSISTANT_PALACE_CONSOLIDATE_SUBJECT = "super_assistant.palace.consolidate"
ONTOLOGY_DOCUMENT_PUBLISHED_SUBJECT = "ontology.documents.published"
ASSISTANT_EVAL_AUTOPILOT_SUBJECT = "assistant_evaluation.autopilot.cycle"
# 流的全部订阅主题：扩容只能追加，旧 subject 与旧 durable 保持不变
PIPELINE_STREAM_SUBJECTS = (
    PIPELINE_EXECUTE_SUBJECT,
    PIPELINE_RUN_SUBJECT,
    DATASET_IMPORT_SUBJECT,
    # 只能追加：Mapping 应用 / Connection 同步 / 事件终态（Celery 退役迁移）
    MAPPING_APPLY_SUBJECT,
    CONNECTION_SYNC_SUBJECT,
    DATASET_EVENT_SUBJECT,
    SUPER_ASSISTANT_REFLECT_MICRO_SUBJECT,
    SUPER_ASSISTANT_REFLECT_FULL_SUBJECT,
    SUPER_ASSISTANT_REFLECT_FOCUSED_SUBJECT,
    DATASET_MIGRATE_SUBJECT,
    # 只能追加：值守循环（助手评估数据飞轮 M3）
    ASSISTANT_EVAL_AUTOPILOT_SUBJECT,
    # 只能追加：记忆宫殿图谱抽取（超级助手）
    SUPER_ASSISTANT_PALACE_EXTRACT_SUBJECT,
    # 只能追加：记忆宫殿图谱定期聚类合并（超级助手）
    SUPER_ASSISTANT_PALACE_CONSOLIDATE_SUBJECT,
    # 只能追加：本体发布态业务文档就绪（本体域派发、宫殿消费）
    ONTOLOGY_DOCUMENT_PUBLISHED_SUBJECT,
)

# 进程内缓存：每个进程只在首次派发时确保一次 Stream
_stream_ensured = False


async def ensure_pipeline_stream(js) -> None:
    """确保流水线执行流存在并覆盖全部 subject；已存在则合并演进。"""
    from nats.js.api import RetentionPolicy, StreamConfig

    config = StreamConfig(
        name=PIPELINE_STREAM,
        subjects=list(PIPELINE_STREAM_SUBJECTS),
        # 工作队列语义：每条消息只被一个 executor 消费
        retention=RetentionPolicy.WORK_QUEUE,
        max_age=7 * 24 * 3600,       # 7 天兜底保留
        duplicate_window=10 * 60,    # 10 分钟 Msg-Id 去重窗
    )
    try:
        await js.add_stream(config)
    except Exception as exc:
        # 多进程同时首次派发时只有一个能建流成功；“名字已占用”即已存在
        if "already in use" not in str(exc):
            raise
        # 生产上可能存在只含旧 subject 的流：合并 subjects 后原地更新，
        # 其余配置（retention/max_age/duplicate_window）一并收敛到当前声明
        info = await js.stream_info(PIPELINE_STREAM)
        existing = [str(subject) for subject in (info.config.subjects or [])]
        merged = sorted(set(existing) | set(PIPELINE_STREAM_SUBJECTS))
        if merged != sorted(existing):
            config.subjects = merged
            await js.update_stream(config)


async def _dispatch(
    subject: str,
    payload: dict,
    nats_url: str,
    msg_id: str,
) -> None:
    global _stream_ensured
    import nats

    nc = await nats.connect(nats_url, connect_timeout=3)
    try:
        js = nc.jetstream()
        if not _stream_ensured:
            await ensure_pipeline_stream(js)
            _stream_ensured = True
        body = json.dumps(
            {**payload, "dispatched_at": datetime.utcnow().isoformat()},
            # ensure_ascii=False：消息体按 UTF-8 字节编码（CJK 3 字节而非
            # \uXXXX 6 字节），体积闸按 UTF-8 字节数计才有意义
            ensure_ascii=False,
        ).encode()
        await js.publish(subject, body, headers={"Nats-Msg-Id": msg_id})
    finally:
        await nc.drain()


# 模块级专用事件循环线程：FastAPI 的 async def 端点在事件循环上运行，
# asyncio.run() 在其中会直接报「cannot be called from a running event
# loop」（同步 def 端点跑线程池则没事）——数据集导入曾因此全线 503。
# 独立循环线程对两种调用上下文都安全。
_dispatch_loop: asyncio.AbstractEventLoop | None = None
_dispatch_loop_lock = threading.Lock()


def _get_dispatch_loop() -> asyncio.AbstractEventLoop:
    global _dispatch_loop
    with _dispatch_loop_lock:
        if _dispatch_loop is None or _dispatch_loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="nats-dispatch-loop",
                daemon=True,
            )
            thread.start()
            _dispatch_loop = loop
        return _dispatch_loop


def _dispatch_sync(subject: str, payload: dict, msg_id: str) -> None:
    from app.config import settings

    nats_url = (settings.nats_url or "").strip()
    if not nats_url:
        raise RuntimeError(
            "后台任务派发失败：未配置 NATS_URL（JetStream 消息通道），"
            "请在环境配置中显式设置后重试"
        )
    future = asyncio.run_coroutine_threadsafe(
        _dispatch(subject, payload, nats_url, msg_id),
        _get_dispatch_loop(),
    )
    future.result(timeout=15)


def dispatch_task(subject: str, payload: dict) -> None:
    """通用同步派发入口：供 FastAPI 请求线程投递任意后台任务消息。

    nats.py 是 asyncio 客户端；每次派发建立独立短连接，避免跨线程共享
    事件循环。请求频率低（手动触发级），连接开销可忽略。Msg-Id 惯例为
    「subject + 参数 + 纳秒时间戳」，去重窗内不误伤正常连发。
    """
    params = ":".join(f"{key}={payload[key]}" for key in sorted(payload))
    _dispatch_sync(subject, payload, f"{subject}:{params}:{time.time_ns()}")


def dispatch_pipeline_task(task_id: str, trigger_type: str,
                           full_refresh: bool = False) -> None:
    """同步派发入口：供 APScheduler 调度线程与 FastAPI 请求线程调用。

    保持原有 subject 与 Msg-Id 格式不变，旧 durable 消费语义不受影响；
    payload 新增 full_refresh 键为 additive（旧消费侧缺省视为 False）。
    """
    _dispatch_sync(
        PIPELINE_EXECUTE_SUBJECT,
        {"task_id": task_id, "trigger_type": trigger_type,
         "full_refresh": bool(full_refresh)},
        f"{task_id}:{trigger_type}:{time.time_ns()}",
    )


_SUPER_ASSISTANT_REFLECT_SUBJECTS = {
    "micro": SUPER_ASSISTANT_REFLECT_MICRO_SUBJECT,
    "full": SUPER_ASSISTANT_REFLECT_FULL_SUBJECT,
    "focused": SUPER_ASSISTANT_REFLECT_FOCUSED_SUBJECT,
}


def dispatch_super_assistant_reflection(kind: str, payload: dict) -> None:
    """超级助手反思任务派发入口：kind ∈ micro/full/focused。

    payload 约定：owner_id/conversation_id 必填；micro 与 focused 另需
    message_id，focused 可选 hint。消费侧的幂等由反思 run 记录兜底。
    """
    subject = _SUPER_ASSISTANT_REFLECT_SUBJECTS.get(kind)
    if subject is None:
        raise ValueError(f"未知的反思任务类型: {kind!r}")
    dispatch_task(subject, payload)


def dispatch_super_assistant_palace_extract(owner_id: str, file_id: str) -> None:
    """记忆宫殿图谱抽取派发入口。

    payload 约定：owner_id/file_id 必填。消费侧幂等由 palace build 记录
    （同 (file_id, sha256) 成功即跳过）兜底；无 NATS_URL 时调用方降级为
    守护线程内联（见 palace_service.request_build）。
    """
    dispatch_task(SUPER_ASSISTANT_PALACE_EXTRACT_SUBJECT, {
        "owner_id": owner_id,
        "file_id": file_id,
    })


def dispatch_super_assistant_palace_consolidate(owner_id: str) -> None:
    """记忆宫殿聚类合并派发入口（每日调度 / 手动触发共用）。

    payload 约定：owner_id 必填。合并本身幂等（重复实体已收敛时候选
    检测为空，直接空跑）；失败等下一调度周期重试，无需 Msg-Id 级防重。
    """
    dispatch_task(SUPER_ASSISTANT_PALACE_CONSOLIDATE_SUBJECT, {
        "owner_id": owner_id,
    })


def dispatch_ontology_document_published(payload: dict) -> None:
    """本体发布态业务文档就绪事件派发入口（本体域发布/回滚/落地 + 每日对账）。

    payload 约定：ontology_id/version_id/title/document_md/fingerprint 必填，
    内容随消息自包含——消费方（super_assistant 宫殿）不得反向依赖本体域，
    幂等由消费侧按 (ontology_id, fingerprint) 状态机兜底。事件语义是
    「该本体当前发布态业务文档为 X」，重复投递无害。

    不走 dispatch_task：其 Msg-Id 惯例按 payload 全量值拼接，document_md
    会被复制进 NATS 消息头，大文档直接超 max_payload。这里沿用
    dispatch_pipeline_task 的直调形式，Msg-Id 只携带本体标识 + 指纹。
    """
    _dispatch_sync(
        ONTOLOGY_DOCUMENT_PUBLISHED_SUBJECT,
        dict(payload),
        (
            f"{ONTOLOGY_DOCUMENT_PUBLISHED_SUBJECT}:"
            f"{payload.get('ontology_id')}:{payload.get('fingerprint')}:{time.time_ns()}"
        ),
    )


def dispatch_assistant_eval_autopilot(config_id: str) -> None:
    """助手评估值守循环派发入口（APScheduler 调度线程 / 手动触发共用）。

    payload 约定：config_id 必填。重复触发由三重防线兜底——JetStream
    Msg-Id 去重窗、配置行的 last_dispatched_at 时段标记、消费侧 DB
    防重入检查。
    """
    dispatch_task(
        ASSISTANT_EVAL_AUTOPILOT_SUBJECT,
        {"config_id": config_id},
    )
