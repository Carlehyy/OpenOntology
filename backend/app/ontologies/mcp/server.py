"""本体 MCP StreamableHTTP 无状态 JSON 传输。

端点 ``/api/v2/formal/ontologies/{ontology_id}/mcp``（含尾斜杠变体），纯
ASGI 挂载不进 OpenAPI/JWT 守卫；单 Server + 单 stateless manager 服务所有
本体，manager 每次 start 新建（SDK run() 单实例一次性）。防线：Host/Origin
允许清单 → 请求体上限 → 头认证与业务授权 → manager。凭据与请求头值绝不
进入日志或异常；401 不带 WWW-Authenticate（避免误导客户端走 OAuth）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

import anyio
import jsonschema
from mcp import types as mcp_types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.shared.config import settings
import app.ontologies.mcp.auth as mcp_auth

logger = logging.getLogger("app.ontologies.mcp")

MCP_SERVER_NAME = "openontology"
TOOL_NAMES = ("ontology_schema", "ontology_query", "ontology_query_page")
MCP_PATH_SUFFIX = "mcp"
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
TOOL_EXECUTION_TIMEOUT_SECONDS = 120.0
TOOL_EXIT_GRACE_SECONDS = 30.0
DISCONNECT_POLL_SECONDS = 0.1

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


class _EndpointError(Exception):
    """传输层拒绝：静态 code/message，可安全透出。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


class _RequestBodyTooLarge(Exception):
    pass


@dataclass
class _McpRequest:
    """跨请求/线程传递的已认证请求上下文（只含标量）。"""

    ontology_id: str
    user_id: str
    username: str
    role: str


_current_request: ContextVar[_McpRequest | None] = ContextVar(
    "openontology_mcp_request", default=None
)
# 当前 tools/call 的取消控制：断连 watcher 与超时/取消路径共享。
_current_control: ContextVar = ContextVar("openontology_mcp_control", default=None)

# 业务侧契约（app.ontologies.mcp.service，业务域拥有）：懒加载避免导入序
# 耦合；测试经 monkeypatch 替换该缓存做协议级验证。
_SERVICE_MODULE = None


def _service():
    global _SERVICE_MODULE
    if _SERVICE_MODULE is None:
        import app.ontologies.mcp.service as service

        _SERVICE_MODULE = service
    return _SERVICE_MODULE


@contextmanager
def _session_scope():
    """worker 线程内短生命周期会话：创建、使用、关闭同一线程。

    事件循环侧不触碰会话；测试经 monkeypatch 本函数注入测试会话。
    """
    from app.shared.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client_key(scope) -> str:
    client = scope.get("client")
    # 只用 IP：端口每次连接都变，进 key 会绕过失败节流；反代真实客户端
    # IP 由可信代理层约束，不信任任意 X-Forwarded-For。
    return client[0] if client else "unknown"


def _cors_entries() -> tuple[set[str], set[str], bool]:
    """解析 cors_allowed_origins：(显式 origin 串, 显式 hostname 集, 是否 ``*``)。"""
    raw = {
        item.strip() for item in settings.cors_allowed_origins.split(",") if item.strip()
    }
    wildcard = "*" in raw
    explicit = raw - {"*"}
    hostnames: set[str] = set()
    for origin in explicit:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            continue
        if parsed.scheme in ("http", "https") and parsed.hostname:
            hostnames.add(parsed.hostname.strip("[]").lower())
    return explicit, hostnames, wildcard


def _host_header_hostname(host: str) -> str:
    try:
        parsed = urlsplit(f"//{host}")
        return (parsed.hostname or "").strip("[]").lower()
    except ValueError:
        return ""


def _validate_host(headers: Headers) -> None:
    """Host 允许清单：显式 CORS origin 的 hostname + loopback。

    仅结构校验挡不住 DNS rebinding；nginx ``$host`` 不带端口，按 hostname
    匹配、不强制端口。
    """
    host = headers.get("host")
    if (
        not host
        or len(host) > 255
        or any(ord(char) < 0x20 or ord(char) == 0x7F or char in " \t" for char in host)
    ):
        raise _EndpointError(400, "invalid_host", "Host 头无效")
    hostname = _host_header_hostname(host)
    if not hostname:
        raise _EndpointError(400, "invalid_host", "Host 头无效")
    if hostname in _LOOPBACK_HOSTNAMES:
        return
    _, explicit_hostnames, _ = _cors_entries()
    if hostname in explicit_hostnames:
        return
    raise _EndpointError(400, "invalid_host", "Host 不在允许清单")


def _validate_origin(headers: Headers) -> None:
    origin = headers.get("origin")
    if origin is None:
        # 非浏览器客户端（curl 等）不发 Origin，允许。
        return
    explicit, _, wildcard = _cors_entries()
    if origin in explicit:
        return
    if wildcard:
        # 开发通配：仅放行 loopback origin，防 DNS rebinding。
        try:
            hostname = (urlsplit(origin).hostname or "").strip("[]").lower()
        except ValueError:
            hostname = ""
        if hostname in _LOOPBACK_HOSTNAMES:
            return
    raise _EndpointError(403, "origin_not_allowed", "Origin 不在允许列表")


def _validate_content_length(headers: Headers) -> None:
    raw = headers.get("content-length")
    if raw is None:
        return
    try:
        length = int(raw)
    except ValueError:
        raise _EndpointError(400, "invalid_content_length", "Content-Length 无效") from None
    if length < 0 or length > MAX_REQUEST_BODY_BYTES:
        raise _EndpointError(413, "payload_too_large", "请求体过大")


def _counting_receive(receive):
    """流式请求体也受上限约束：无 Content-Length 时按累计字节数拒绝。"""
    seen = 0

    async def wrapped():
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body", b""))
            if seen > MAX_REQUEST_BODY_BYTES:
                raise _RequestBodyTooLarge()
        return message

    return wrapped


def _authenticate_and_authorize(
    ontology_id: str, username: str, password: str, client: str
) -> mcp_auth.AuthenticatedPrincipal:
    """worker 线程：认证 + agent 菜单 + 业务授权（同一会话、同一线程）。"""
    from app.auth.models import User

    with _session_scope() as db:
        principal = mcp_auth.authenticate(db, username, password, client=client)
        try:
            _service().authorize_request(
                db, db.get(User, principal.user_id), ontology_id
            )
        except _service().McpError as exc:
            # 业务授权拒绝按其 status 映射 HTTP，只透静态 code/message。
            raise mcp_auth.McpAuthError(
                int(getattr(exc, "status", 403) or 403),
                str(getattr(exc, "code", "forbidden") or "forbidden"),
                str(getattr(exc, "message", "无权访问") or "无权访问"),
            ) from exc
        return principal


def _run_tool(
    payload: _McpRequest, tool_name: str, arguments: dict, control
) -> tuple[str, object]:
    """worker 线程：自建会话执行业务 run_tool（允许 commit）。

    返回 ("ok", result) 或 ("error", payload)：4xx 的 detail() 整体透传
    （409 恢复环依赖），5xx 只回静态错误。QueryControl scope 使
    interruptible_connection 感知取消。
    """
    from app.auth.models import User
    from app.shared.query_control import query_control_scope

    service = _service()
    try:
        with _session_scope() as db:
            user = db.get(User, payload.user_id)
            with query_control_scope(control):
                result = service.run_tool(
                    db, user, payload.ontology_id, tool_name, arguments
                )
        return ("ok", result)
    except service.McpError as exc:
        status = int(getattr(exc, "status", 500) or 500)
        if status >= 500:
            logger.error("MCP tool business failure (tool=%s, status=%s)", tool_name, status)
            return ("error", {"code": "tool_internal_error", "message": "工具执行失败"})
        detail = getattr(exc, "detail", None)
        payload_out = (
            detail()
            if callable(detail)
            else {
                "code": getattr(exc, "code", "tool_error"),
                "message": getattr(exc, "message", "工具执行失败"),
            }
        )
        return ("error", payload_out)
    except Exception:
        logger.exception("MCP tool execution failed (tool=%s)", tool_name)
        return ("error", {"code": "tool_internal_error", "message": "工具执行失败"})


def _tool_error_result(payload: dict) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        isError=True,
        content=[
            mcp_types.TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False)
            )
        ],
    )


def _strict_input_schema(schema: object) -> dict:
    strict = dict(schema) if isinstance(schema, dict) else {}
    strict.setdefault("type", "object")
    strict["additionalProperties"] = False
    return strict


def _tool_manifests() -> dict[str, dict]:
    manifests: dict[str, dict] = {}
    for manifest in _service().tool_manifests() or ():
        name = manifest.get("name")
        if name in TOOL_NAMES:
            manifests[name] = manifest
    return manifests


async def _watch_disconnect(request: Request) -> None:
    while True:
        if await request.is_disconnected():
            control = _current_control.get()
            if control is not None:
                control.cancel()
            return
        await asyncio.sleep(DISCONNECT_POLL_SECONDS)


def _build_mcp_server() -> Server:
    server = Server(MCP_SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=name,
                description=str(manifest.get("description") or ""),
                inputSchema=_strict_input_schema(manifest.get("inputSchema")),
                annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
            )
            for name, manifest in sorted(_tool_manifests().items())
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        if name not in TOOL_NAMES or name not in _tool_manifests():
            return _tool_error_result({"code": "unknown_tool", "message": "未知工具"})
        schema = _strict_input_schema(_tool_manifests()[name].get("inputSchema"))
        try:
            jsonschema.validate(instance=arguments or {}, schema=schema)
        except jsonschema.ValidationError as exc:
            # 不回显输入值（ValidationError.message 会内嵌实例内容）。
            where = "/".join(str(part) for part in exc.absolute_path) or "arguments"
            return _tool_error_result(
                {
                    "code": "invalid_arguments",
                    "message": f"参数校验失败: {where} ({exc.validator})",
                }
            )

        payload = _current_request.get()
        if payload is None:
            logger.error("MCP call_tool without request context (tool=%s)", name)
            return _tool_error_result(
                {"code": "tool_internal_error", "message": "工具执行失败"}
            )

        from app.shared.query_control import QueryControl

        control = QueryControl()
        control_token = _current_control.set(control)
        task = asyncio.create_task(
            asyncio.to_thread(_run_tool, payload, name, arguments or {}, control)
        )
        watch: asyncio.Task | None = None
        try:
            # 断连即取消：watcher 在 control 置入当前上下文之后创建，
            # 继承的 contextvar 快照能看到本 control。
            starlette_request = None
            with contextlib.suppress(LookupError, AttributeError):
                starlette_request = server.request_context.request
            if isinstance(starlette_request, Request):
                watch = asyncio.create_task(_watch_disconnect(starlette_request))
            try:
                status, result = await asyncio.wait_for(
                    asyncio.shield(task), timeout=TOOL_EXECUTION_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                # 先取消标记（interruptible_connection 让 DB 读尽快中断）再等
                # worker 安全退出；绝不 abandon 持有会话的线程。超时后一律回
                # 静态错误，不透传 worker 迟到的结果。
                control.cancel()
                logger.warning("MCP tool execution timed out (tool=%s)", name)
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=TOOL_EXIT_GRACE_SECONDS
                    )
                return _tool_error_result(
                    {"code": "query_timeout", "message": "查询执行超时"}
                )
            except (asyncio.CancelledError, anyio.get_cancelled_exc_class()):
                # 请求被取消：置标记 + shield 下等线程收尾（会话线程内自关）。
                control.cancel()
                with anyio.CancelScope(shield=True):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            asyncio.shield(task), timeout=TOOL_EXIT_GRACE_SECONDS
                        )
                raise
            if status == "error":
                return _tool_error_result(result)
            if result is None:
                return _tool_error_result(
                    {"code": "tool_internal_error", "message": "工具执行失败"}
                )
            if isinstance(result, mcp_types.CallToolResult):
                return result
            # SDK 对 dict 默认 json.dumps(indent=2, ensure_ascii=True)：非
            # ASCII 6 字节转义 + pretty 空白会突破业务 16KiB schema 分页
            # 预算。这里显式紧凑序列化（ensure_ascii=False）。
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(
                        type="text",
                        text=json.dumps(
                            result, ensure_ascii=False, separators=(",", ":")
                        ),
                    )
                ],
                structuredContent=result,
            )
        finally:
            control.cancel()
            if watch is not None:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watch
            _current_control.reset(control_token)

    return server


class McpTransport:
    """可重复启停的 StreamableHTTP 无状态传输（每次 start 新建 manager）。"""

    def __init__(self) -> None:
        self._manager: StreamableHTTPSessionManager | None = None
        self._run_context = None

    @property
    def running(self) -> bool:
        return self._manager is not None

    async def start(self) -> None:
        if self._manager is not None:
            return
        manager = StreamableHTTPSessionManager(
            app=_build_mcp_server(),
            json_response=True,
            stateless=True,
        )
        run_context = manager.run()
        try:
            await run_context.__aenter__()
        except BaseException:
            with contextlib.suppress(BaseException):
                await run_context.__aexit__(None, None, None)
            raise
        self._manager = manager
        self._run_context = run_context

    async def stop(self) -> None:
        if self._manager is None:
            return
        run_context = self._run_context
        self._manager = None
        self._run_context = None
        await run_context.__aexit__(None, None, None)

    async def handle(self, scope, receive, send) -> None:
        manager = self._manager
        if manager is None:
            response = JSONResponse(
                status_code=503,
                content={"code": "mcp_not_running", "message": "MCP 服务未启动"},
            )
            await response(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


transport = McpTransport()


def _error_response(status: int, code: str, message: str, *, retry_after: int = 0) -> JSONResponse:
    headers = {}
    if retry_after > 0:
        headers["Retry-After"] = str(retry_after)
    # 401 刻意不带 WWW-Authenticate：避免误导 MCP 客户端进入 OAuth 分支。
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message},
        headers=headers,
    )


class McpEndpoint:
    """本体 MCP 端点的纯 ASGI 入口（Starlette Route 挂载，含 path_params）。"""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        counting_receive = _counting_receive(receive)
        try:
            headers = Headers(scope=scope)
            _validate_host(headers)
            _validate_origin(headers)
            _validate_content_length(headers)
            username, password = mcp_auth.extract_credentials(headers)
            ontology_id = scope.get("path_params", {}).get("ontology_id", "")
            if not ontology_id or len(ontology_id) > 64:
                raise _EndpointError(400, "invalid_ontology_id", "本体标识无效")
            principal = await asyncio.to_thread(
                _authenticate_and_authorize,
                ontology_id,
                username,
                password,
                _client_key(scope),
            )
            payload = _McpRequest(
                ontology_id=ontology_id,
                user_id=principal.user_id,
                username=principal.username,
                role=principal.role,
            )
        except mcp_auth.McpAuthError as exc:
            await _error_response(
                exc.status, exc.code, exc.message, retry_after=exc.retry_after
            )(scope, receive, send)
            return
        except _EndpointError as exc:
            await _error_response(exc.status, exc.code, exc.message)(
                scope, receive, send
            )
            return
        except _RequestBodyTooLarge:
            await _error_response(413, "payload_too_large", "请求体过大")(
                scope, receive, send
            )
            return
        except Exception:
            # 兜底：异常文本可能携带请求头值，只落静态 code，不打 traceback。
            logger.error("MCP endpoint rejected a request (code=mcp_internal_error)")
            await _error_response(500, "mcp_internal_error", "服务内部错误")(
                scope, receive, send
            )
            return

        token = _current_request.set(payload)
        try:
            await transport.handle(scope, counting_receive, send)
        except _RequestBodyTooLarge:
            # manager 读体阶段超限；响应可能尚未开始，尽力回 413。
            with contextlib.suppress(Exception):
                await _error_response(413, "payload_too_large", "请求体过大")(
                    scope, receive, send
                )
        finally:
            _current_request.reset(token)


endpoint = McpEndpoint()


def register_routes(app) -> None:
    """把 MCP 端点挂载为纯 ASGI 精确路由（含尾斜杠变体，不进 OpenAPI）。"""
    for path in (
        f"/api/v2/formal/ontologies/{{ontology_id}}/{MCP_PATH_SUFFIX}",
        f"/api/v2/formal/ontologies/{{ontology_id}}/{MCP_PATH_SUFFIX}/",
    ):
        # endpoint 是 callable 实例：Starlette 对函数型 endpoint 会按
        # request_response 包装（endpoint(request)），对实例按纯 ASGI 使用。
        app.router.routes.append(
            Route(path, endpoint=endpoint, methods=["POST", "OPTIONS"])
        )
