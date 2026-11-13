"""Replica 批量接收端 — capture 作用域的回环 HTTP sink 目标。

复用 ``cdc_receiver.ReceiverHandle`` 的全部交通机制（回环绑定、Standard
Webhooks v1 HMAC、路径/代际栅栏、请求体 16MiB 上限、并发上限、彻底
``stop()``），子类只覆写业务分发 ``_dispatch`` —— 不重写 HTTP/HMAC/server
实现，Sentinel CDC 接收端零改动。

与 CDC 单事件 envelope 的差异在本模块内封闭：

- 每个 capture UUID 一个注册项（``register(capture.id, generation, secret)``），
  请求体是 JSON 数组批（1..2048，与 ``replica_journal.MAX_BATCH_RECORDS``
  同源）；浮点 token 仍由交通层 ``parse_float=Decimal`` 精确解析；
- ``_dispatch`` 在调用方事务内调 ``replica_ingest.ingest_batch(db,
  capture_id, records)``，成功先 ``commit`` 后回 2xx（durable-before-ack）；
  任何失败全批回滚，再以独立事务 ``replica_ingest.mark_capture_error(
  db, capture_id, code, retryable=...)`` 持久化封闭错误码；
- 错误面封闭：``ReplicaIngestError`` 契约（``code``/``retryable``）驱动
  409（不可重试，回显封闭 code）/ 503（可重试，统一 ``unavailable``）；
  非契约异常折叠为 ``INGEST_UNAVAILABLE``，绝不回显源值或异常文本；
- 代际栅栏：DB 中 capture.generation 与注册代不一致时按生命周期类拒绝
  （409 ``OBSOLETE_CAPTURE_GENERATION``）—— 旧注册代既不能写入，也不得
  经 ``mark_capture_error`` 篡改当前 capture 的健康状态；
- ``replica_ingest`` 经 lazy import 解析，测试/组合方可注入 callable 替身；
  本模块不提供任何 ingest fallback 实现。
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler

import sqlalchemy as sa

from app.data_channel.integrated.cdc_receiver import (
    DEFAULT_MAX_CONCURRENT_CONNECTIONS,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    LOOPBACK_HOST,
    ReceiverHandle,
)
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture,
)
from app.data_channel.integrated.replica_journal import MAX_BATCH_RECORDS

OBSOLETE_CAPTURE_GENERATION = "OBSOLETE_CAPTURE_GENERATION"
UNKNOWN_INGEST_CODE = "INGEST_UNAVAILABLE"
_CLOSED_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")

IngestBatch = Callable[..., object]
MarkCaptureError = Callable[..., object]


def _default_ingest_batch(db, capture_id: str, records: list) -> dict:
    from app.data_channel.integrated import replica_ingest

    return replica_ingest.ingest_batch(db, capture_id, records)


def _default_mark_capture_error(db, capture_id: str, code: str,
                                *, retryable: bool) -> None:
    from app.data_channel.integrated import replica_ingest

    return replica_ingest.mark_capture_error(
        db, capture_id, code, retryable=retryable)


def _closed_error(exc: Exception) -> tuple[str, bool]:
    """按 ReplicaIngestError 契约折叠为封闭 (code, retryable)。

    契约属性（``code: str`` / ``retryable: bool``）即识别面；code 还须是
    大写封闭标识（对齐 ``last_error_code`` 列宽与 journal 码风），否则按
    未知异常折叠 —— 任何路径都不回显异常文本或源值。
    """
    code = getattr(exc, "code", None)
    retryable = getattr(exc, "retryable", None)
    if (isinstance(code, str) and isinstance(retryable, bool)
            and _CLOSED_CODE.fullmatch(code)):
        return code, retryable
    return UNKNOWN_INGEST_CODE, True


def _result_status(result) -> str:
    if isinstance(result, dict):
        status = result.get("status")
        if isinstance(status, str) and status:
            return status
    return "committed"


class _GenerationFenced(Exception):
    """旧注册代请求：生命周期类拒绝，不落 capture 错误状态。"""


class ReplicaReceiverHandle(ReceiverHandle):
    """每个 capture 一个注册项的批量接收端句柄。"""

    def __init__(self, *, host: str, port: int, max_request_bytes: int,
                 timestamp_tolerance_seconds: int, max_inflight: int,
                 session_factory, clock,
                 ingest_batch: IngestBatch | None = None,
                 mark_capture_error: MarkCaptureError | None = None):
        super().__init__(
            host=host, port=port, max_request_bytes=max_request_bytes,
            timestamp_tolerance_seconds=timestamp_tolerance_seconds,
            max_inflight=max_inflight, session_factory=session_factory,
            clock=clock)
        self._replica_ingest_batch = (
            ingest_batch if ingest_batch is not None else _default_ingest_batch)
        self._replica_mark_capture_error = (
            mark_capture_error if mark_capture_error is not None
            else _default_mark_capture_error)

    # ------------------------------------------------------------------ 分发

    def _dispatch(self, handler: BaseHTTPRequestHandler,
                  registration, envelope) -> None:
        if (not isinstance(envelope, list)
                or not 1 <= len(envelope) <= MAX_BATCH_RECORDS):
            handler._respond(400, {"error": "bad_request"})
            return
        capture_id = registration.connection_id
        try:
            with self._session_factory() as db:
                try:
                    self._reject_stale_generation(
                        db, capture_id, registration.generation)
                    result = self._replica_ingest_batch(
                        db, capture_id, envelope)
                    db.commit()
                except Exception:
                    db.rollback()  # 全批失败：绝不提交部分事实
                    raise
        except _GenerationFenced:
            handler._respond(409, {"error": OBSOLETE_CAPTURE_GENERATION})
            return
        except Exception as exc:  # noqa: BLE001 - 未知异常按瞬态处理，不回显内容
            code, retryable = _closed_error(exc)
            self._maintenance(lambda session: (
                self._replica_mark_capture_error(
                    session, capture_id, code, retryable=retryable)))
            if retryable:
                handler._respond(503, {"error": "unavailable"})
            else:
                handler._respond(409, {"error": code})
            return
        self._record_accept(registration, False)
        handler._respond(200, {"status": _result_status(result)})

    def _reject_stale_generation(self, db, capture_id: str,
                                 generation: int) -> None:
        """注册代必须等于 DB 当前代；capture 不存在时交给 ingest 判定。"""
        current = db.scalar(sa.select(
            IntegratedReplicaCapture.generation).where(
            IntegratedReplicaCapture.id == capture_id))
        if current is not None and current != generation:
            raise _GenerationFenced


def create_replica_receiver(*, host: str = LOOPBACK_HOST, port: int = 0,
                            session_factory=None,
                            max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
                            timestamp_tolerance_seconds: int = (
                                DEFAULT_TIMESTAMP_TOLERANCE_SECONDS),
                            max_inflight: int = DEFAULT_MAX_CONCURRENT_CONNECTIONS,
                            clock: Callable[[], float] = time.time,
                            ingest_batch: IngestBatch | None = None,
                            mark_capture_error: MarkCaptureError | None = None,
                            ) -> ReplicaReceiverHandle:
    """绑定并启动 replica 批量接收端；参数语义与 ``create_receiver`` 一致。

    ``ingest_batch``/``mark_capture_error`` 缺省经 lazy import 解析到
    ``replica_ingest``；测试注入替身时模块完全不被导入。
    """
    if host != LOOPBACK_HOST:
        raise ValueError(
            f"replica receiver binds loopback only (got {host!r})")
    if isinstance(port, bool) or not isinstance(port, int) or port < 0:
        raise ValueError("port must be a non-negative int")
    if session_factory is None:
        from app.shared.database import SessionLocal

        session_factory = SessionLocal
    if (isinstance(max_inflight, bool) or not isinstance(max_inflight, int)
            or max_inflight < 1):
        raise ValueError("max_inflight must be a positive int")
    return ReplicaReceiverHandle(
        host=host, port=port, max_request_bytes=max_request_bytes,
        timestamp_tolerance_seconds=timestamp_tolerance_seconds,
        max_inflight=max_inflight, session_factory=session_factory,
        clock=clock, ingest_batch=ingest_batch,
        mark_capture_error=mark_capture_error)


__all__ = [
    "DEFAULT_MAX_CONCURRENT_CONNECTIONS",
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_TIMESTAMP_TOLERANCE_SECONDS",
    "LOOPBACK_HOST",
    "MAX_BATCH_RECORDS",
    "OBSOLETE_CAPTURE_GENERATION",
    "ReplicaReceiverHandle",
    "UNKNOWN_INGEST_CODE",
    "create_replica_receiver",
]
