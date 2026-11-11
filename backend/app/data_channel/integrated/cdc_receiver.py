"""CDC 本机接收端 — Debezium Server HTTP sink 的唯一回环目标。

架构约束：监听 127.0.0.1（工厂拒绝其他 host）临时端口，只服务与 CDC
executor 同容器的 Debezium HTTP sink；不注册任何 API/公开路由，不引入
静态密钥环境变量 — 每个连接 + 代的签名密钥由 supervisor 随机生成并在注
册时注入（24..64 原始字节）。

安全基线：

- Standard Webhooks v1 对称签名校验：``webhook-id``/``webhook-timestamp``/
  ``webhook-signature``，HMAC-SHA256 over ``id.timestamp.原始请求体``，
  base64 恒定时间比较；时间戳偏差超容忍窗拒绝；
- 路径必须与注册会话精确匹配（``/cdc/{connection_id}/{generation}``），
  未知路径/未注册代一律 404；重注册（换代）清除该连接全部 last_accepted/
  last_heartbeat 证据，杜绝旧代心跳伪造健康；请求体字节数硬上限（413）；
  并发连接数硬上限（超限立即断开，无排队放大）；
- 请求体以 ``parse_float=Decimal`` 解析（浮点 token 不经 double 损耗，
  double 列由 schema 校验后显式转换）；全程不记录请求体、行数据、签名与
  密钥；错误响应只携带封闭错误码，不回显源值或异常文本；
- 接收端拥有事务边界：ingest 成功（captured/duplicate/stale/skipped/
  心跳/控制事件）先 commit 再回 2xx；capacity_exceeded 先回滚本事件、
  再以独立事务把全部 source 置 paused（EVENT_CAPACITY_EXCEEDED）后 503
  重试不丢事件；其余失败回滚后以独立短事务经 ``cdc_control.report_source``
  （代际 fenced）持久化 source 健康错误，再返回 409/503 — 绝不提交部分
  事实，也绝不假成功；生命周期类拒绝（换代/停用/断连）不落 error 状态；
- ``stop()`` 幂等且彻底：停 accept 循环、断开在途连接、join 全部处理线
  程与 serve 线程，端口随 socket 关闭释放（测试断言无监听泄漏）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.data_channel.integrated.cdc_envelope import EnvelopeError
from app.data_channel.integrated.cdc_ingest import (
    ACK_STATUSES, ERROR_CAPACITY_EXCEEDED, STATUS_CAPACITY,
    CdcPermanentError, CdcTransientError, ingest, mark_all_sources_paused,
    record_source_error,
)

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 300
DEFAULT_MAX_CONCURRENT_CONNECTIONS = 8
PATH_PREFIX = "/cdc/"


@dataclass(frozen=True)
class ReceiverSession:
    """注册回执：回调路径与代际身份（不含密钥 — 密钥只在注册方手中）。"""

    connection_id: str
    generation: int
    path: str


class _Registration:
    __slots__ = ("connection_id", "generation", "secret", "path")

    def __init__(self, connection_id: str, generation: int, secret: bytes):
        self.connection_id = connection_id
        self.generation = generation
        self.secret = secret
        self.path = f"{PATH_PREFIX}{connection_id}/{generation}"


class _BoundedHttpServer(ThreadingHTTPServer):
    # 非守护线程 + block_on_close：server_close() 会 join 全部处理线程，
    # stop() 再主动断开在途 socket 兜底，保证不遗留任何线程/监听。超限时
    # 由处理线程回 503（保有序回绝而非断连，便于 sink 退避重试）。
    daemon_threads = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address, handler, *, max_inflight: int):
        self._max_inflight = max_inflight
        self._active = 0
        self._sockets: set[socket.socket] = set()
        self._state_lock = threading.Lock()
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        with self._state_lock:
            self._active += 1
            self._sockets.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._state_lock:
                self._active -= 1
                self._sockets.discard(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._state_lock:
                self._active -= 1
                self._sockets.discard(request)

    def inflight_over_cap(self) -> bool:
        with self._state_lock:
            return self._active > self._max_inflight

    def close_active_sockets(self) -> None:
        with self._state_lock:
            pending = list(self._sockets)
        for sock in pending:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


class ReceiverHandle:
    """supervisor 持有的接收端句柄；stop 后端口释放、线程全部回收。"""

    def __init__(self, *, host: str, port: int, max_request_bytes: int,
                 timestamp_tolerance_seconds: int, max_inflight: int,
                 session_factory, clock):
        self._clock = clock
        self._session_factory = session_factory
        self._max_request_bytes = max_request_bytes
        self._max_inflight = max_inflight
        self._tolerance = timestamp_tolerance_seconds
        self._lock = threading.Lock()
        self._by_connection: dict[str, _Registration] = {}
        self._by_path: dict[str, _Registration] = {}
        self._last_accepted: dict[tuple[str, int], float] = {}
        self._last_heartbeat: dict[str, float] = {}
        self._stopped = False

        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 30

            def log_message(self, *args):  # 静默：不落任何请求痕迹
                return

            def _respond(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
                receiver._handle_post(self)

            def do_GET(self):  # noqa: N802
                self._respond(405, {"error": "method_not_allowed"})

            do_PUT = do_DELETE = do_PATCH = do_HEAD = do_GET

        self._server = _BoundedHttpServer(
            (host, port), _Handler, max_inflight=max_inflight)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="cdc-receiver-serve", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ 公共

    @property
    def host(self) -> str:
        return self._server.server_address[0]

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def register(self, connection_id: str, generation: int,
                 secret: bytes) -> ReceiverSession:
        """注册（或换代重注册）一个源；返回会话（含回调路径）。

        重注册同一连接会替换旧代：旧路径立即 404，且该连接的全部
        last_accepted/last_heartbeat 证据被清除（新代必须自己证明健康）。
        密钥只留在注册表内，不出现在返回值/repr 中。
        """
        connection_id = str(connection_id)
        generation = int(generation)
        if not connection_id or "/" in connection_id:
            raise ValueError("connection_id must be a path-safe identifier")
        if not isinstance(secret, (bytes, bytearray)) or not 24 <= len(secret) <= 64:
            raise ValueError("secret must be raw bytes of 24..64 length")
        with self._lock:
            if self._stopped:
                raise RuntimeError("receiver is stopped")
            old = self._by_connection.get(connection_id)
            if old is not None:
                self._by_path.pop(old.path, None)
            self._last_accepted = {
                key: value for key, value in self._last_accepted.items()
                if key[0] != connection_id}
            self._last_heartbeat.pop(connection_id, None)
            registration = _Registration(
                connection_id, generation, bytes(secret))
            self._by_connection[connection_id] = registration
            self._by_path[registration.path] = registration
            return ReceiverSession(
                connection_id=connection_id, generation=generation,
                path=registration.path)

    def unregister(self, connection_id: str) -> None:
        with self._lock:
            registration = self._by_connection.pop(str(connection_id), None)
            if registration is not None:
                self._by_path.pop(registration.path, None)

    def last_accepted_at(self, connection_id: str,
                         generation: int) -> float | None:
        with self._lock:
            return self._last_accepted.get(
                (str(connection_id), int(generation)))

    def last_heartbeat_at(self, connection_id: str) -> float | None:
        with self._lock:
            return self._last_heartbeat.get(str(connection_id))

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._by_connection.clear()
            self._by_path.clear()
        self._server.shutdown()
        self._server.close_active_sockets()
        self._server.server_close()
        self._thread.join(timeout=10)

    # ------------------------------------------------------------------ 内部

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if self._server.inflight_over_cap():
            handler._respond(503, {"error": "busy"})
            return
        registration = self._registration_for(handler.path)
        if registration is None:
            handler._respond(404, {"error": "not_found"})
            return
        length_header = handler.headers.get("Content-Length")
        if length_header is None:
            handler._respond(411, {"error": "length_required"})
            return
        try:
            length = int(length_header)
        except ValueError:
            handler._respond(400, {"error": "bad_request"})
            return
        if length < 0 or length > self._max_request_bytes:
            handler._respond(413, {"error": "payload_too_large"})
            return
        try:
            body = handler.rfile.read(length)
        except (TimeoutError, OSError):
            return
        if len(body) != length:
            return
        if not self._verify_signature(registration, handler, body):
            handler._respond(401, {"error": "unauthorized"})
            return
        try:
            envelope = json.loads(body, parse_float=Decimal)
        except ValueError:
            handler._respond(400, {"error": "bad_request"})
            return
        self._dispatch(handler, registration, envelope)

    def _registration_for(self, path: str) -> _Registration | None:
        with self._lock:
            return self._by_path.get(path)

    def _verify_signature(self, registration: _Registration,
                          handler: BaseHTTPRequestHandler,
                          body: bytes) -> bool:
        webhook_id = handler.headers.get("webhook-id")
        timestamp = handler.headers.get("webhook-timestamp")
        signature = handler.headers.get("webhook-signature")
        if not webhook_id or not timestamp or not signature:
            return False
        try:
            seconds = int(timestamp)
        except ValueError:
            return False
        if abs(self._clock() - seconds) > self._tolerance:
            return False
        signed = f"{webhook_id}.{timestamp}.".encode("utf-8") + body
        expected = base64.b64encode(
            hmac.new(registration.secret, signed, hashlib.sha256).digest()
        ).decode("ascii")
        for token in signature.split():
            scheme, _, value = token.partition(",")
            if scheme == "v1" and hmac.compare_digest(value, expected):
                return True
        return False

    def _dispatch(self, handler: BaseHTTPRequestHandler,
                  registration: _Registration, envelope) -> None:
        heartbeat = _is_heartbeat(envelope)
        status = "unavailable"
        try:
            with self._session_factory() as db:
                result = ingest(
                    db, registration.connection_id,
                    registration.generation, envelope)
                status = str(result.get("status") or "")
                if status == STATUS_CAPACITY:
                    db.rollback()
                    self._maintenance(lambda session: mark_all_sources_paused(
                        session, ERROR_CAPACITY_EXCEEDED))
                    handler._respond(503, {"error": "capacity_exceeded"})
                    return
                if status not in ACK_STATUSES:
                    db.rollback()
                    handler._respond(500, {"error": "ingest_invalid_status"})
                    return
                db.commit()
        except (CdcPermanentError, EnvelopeError) as exc:
            code = str(exc.code)
            if getattr(exc, "report", True):
                self._record_error(registration, code, permanent=True)
            handler._respond(409, {"error": code})
            return
        except CdcTransientError as exc:
            self._record_error(registration, str(exc.code), permanent=False)
            handler._respond(503, {"error": "unavailable"})
            return
        except Exception:  # noqa: BLE001 - 未知异常按瞬态处理，不回显内容
            self._record_error(
                registration, "INGEST_UNAVAILABLE", permanent=False)
            handler._respond(503, {"error": "unavailable"})
            return
        self._record_accept(registration, heartbeat)
        handler._respond(200, {"status": status})

    def _record_accept(self, registration: _Registration,
                       heartbeat: bool) -> None:
        now = self._clock()
        with self._lock:
            # 当前注册栅栏：换代重注册后，旧代在途请求（同连接同代同路径）
            # 不得以对象身份冒充新注册推进健康证据。
            if self._by_path.get(registration.path) is not registration:
                return
            self._last_accepted[
                (registration.connection_id, registration.generation)] = now
            if heartbeat:
                self._last_heartbeat[registration.connection_id] = now

    def _record_error(self, registration: _Registration, code: str,
                      *, permanent: bool) -> None:
        self._maintenance(lambda session: record_source_error(
            session, registration.connection_id, code,
            generation=registration.generation, permanent=permanent))

    def _maintenance(self, action) -> None:
        """独立短事务执行健康落库；失败不影响既有响应语义。"""
        try:
            with self._session_factory() as session:
                action(session)
                session.commit()
        except Exception:  # noqa: BLE001 - 健康落库尽力而为，绝不传播源值
            return


def _is_heartbeat(envelope) -> bool:
    schema = envelope.get("schema") if isinstance(envelope, dict) else None
    name = schema.get("name") if isinstance(schema, dict) else None
    return isinstance(name, str) and name.endswith(".Heartbeat")


def create_receiver(*, host: str = LOOPBACK_HOST, port: int = 0,
                    session_factory=None,
                    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
                    timestamp_tolerance_seconds: int = (
                        DEFAULT_TIMESTAMP_TOLERANCE_SECONDS),
                    max_inflight: int = DEFAULT_MAX_CONCURRENT_CONNECTIONS,
                    clock: Callable[[], float] = time.time) -> ReceiverHandle:
    """绑定并启动回环接收端；返回 supervisor 持有的已启动 ReceiverHandle。

    只接受 ``host="127.0.0.1"``（其他值在绑定任何 socket 之前拒绝）与
    临时/指定端口；``session_factory`` 默认使用应用 ``SessionLocal``（每个
    请求独立短会话）。测试经 ``session_factory``/``clock`` 注入替身。
    """
    if host != LOOPBACK_HOST:
        raise ValueError(f"cdc receiver binds loopback only (got {host!r})")
    if isinstance(port, bool) or not isinstance(port, int) or port < 0:
        raise ValueError("port must be a non-negative int")
    if session_factory is None:
        from app.shared.database import SessionLocal

        session_factory = SessionLocal
    if isinstance(max_inflight, bool) or not isinstance(max_inflight, int)             or max_inflight < 1:
        raise ValueError("max_inflight must be a positive int")
    return ReceiverHandle(
        host=host, port=port, max_request_bytes=max_request_bytes,
        timestamp_tolerance_seconds=timestamp_tolerance_seconds,
        max_inflight=max_inflight,
        session_factory=session_factory, clock=clock)


__all__ = [
    "DEFAULT_MAX_CONCURRENT_CONNECTIONS",
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_TIMESTAMP_TOLERANCE_SECONDS",
    "LOOPBACK_HOST",
    "PATH_PREFIX",
    "ReceiverHandle",
    "ReceiverSession",
    "create_receiver",
]
