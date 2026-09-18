"""One isolated Playwright browser context per data-steward conversation.

The runtime connects to a separately managed Chromium over CDP.  Human input,
agent actions, network inspection and downloads all operate on the same page and
therefore share the login state without ever asking the agent for a password.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import ipaddress
import json
import logging
import mimetypes
import re
import secrets
import socket
import threading
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine
from urllib.parse import parse_qsl, unquote, unquote_to_bytes, urlparse, urlunparse

from app.config import settings
from app.data_channel.steward import workspace
from app.shared.http_transport import open_with_loopback_bypass

logger = logging.getLogger(__name__)

_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "set-cookie", "x-api-key"}
_FILE_MIMES = {
    "application/pdf", "application/zip", "application/octet-stream",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/csv",
}
_FILE_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".zip", ".json"}
_FILE_EXTS.update({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".ico", ".avif",
    ".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm", ".mov", ".avi",
})
_PAGE_ELEMENT_SELECTOR = (
    "a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],"
    "[contenteditable=true],img[src],video[src],audio[src],source[src]"
)
_PAGE_KEYS = {"page", "pageno", "page_no", "pageindex", "page_index", "current", "currentpage"}
_SIZE_KEYS = {"size", "pagesize", "page_size", "limit", "per_page", "rows"}
_OFFSET_KEYS = {"offset", "start", "skip", "from"}
_CURSOR_KEYS = {"cursor", "nextcursor", "next_cursor", "after", "continuationtoken", "continuation_token"}


class BrowserRuntimeError(RuntimeError):
    pass


def _resolve_cdp_endpoint(raw_url: str) -> str:
    """Resolve an internal HTTP CDP hostname to an IP before discovery.

    Modern Chromium rejects ``/json/version`` when the HTTP Host header is a
    Docker service name (DNS-rebinding protection).  Using the resolved IP both
    satisfies that check and makes Chromium return a WebSocket URL reachable
    from the backend container.  HTTPS endpoints retain their hostname so TLS
    certificate validation is not broken.
    """
    parsed = urlparse((raw_url or "").strip())
    host = parsed.hostname
    if parsed.scheme != "http" or not host:
        return raw_url
    try:
        ipaddress.ip_address(host)
        return raw_url
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(
            host, parsed.port or 80, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return raw_url
    if not infos:
        return raw_url
    resolved = infos[0][4][0]
    userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse(parsed._replace(netloc=f"{userinfo}{resolved}{port}"))


def probe_browser_cdp(timeout: float = 1.5) -> dict[str, Any]:
    """Cheap CDP readiness probe used by status and production readiness."""
    configured = bool((settings.steward_browser_cdp_url or "").strip())
    if not configured:
        return {"configured": False, "reachable": False, "error": "CDP URL 未配置"}
    endpoint = _resolve_cdp_endpoint(settings.steward_browser_cdp_url).rstrip("/")
    parsed_endpoint = urlparse(endpoint)
    if (
        parsed_endpoint.scheme not in {"http", "https"}
        or not parsed_endpoint.hostname
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.path not in {"", "/"}
        or parsed_endpoint.params
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        return {
            "configured": True, "reachable": False,
            "error": "浏览器健康检查要求 http/https CDP 服务根地址",
        }
    request = urllib.request.Request(
        f"{endpoint}/json/version", headers={"Connection": "close"})
    try:
        with open_with_loopback_bypass(request, timeout=timeout) as response:
            payload = json.loads(response.read(64_000).decode("utf-8"))
        websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
        if not websocket_url.startswith(("ws://", "wss://")):
            raise ValueError("响应缺少 webSocketDebuggerUrl")
        return {
            "configured": True,
            "reachable": True,
            "browser": str(payload.get("Browser") or "")[:120],
            "protocolVersion": str(payload.get("Protocol-Version") or "")[:40],
        }
    except Exception as exc:  # noqa: BLE001 — 状态端点必须返回结构化诊断
        return {
            "configured": True,
            "reachable": False,
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


def _safe_url(url: str) -> str:
    value = (url or "").strip()
    if not re.match(r"^https?://", value, re.IGNORECASE):
        raise BrowserRuntimeError("只允许访问 http/https 网址")
    parsed = urlparse(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or host in {"metadata.google.internal"}:
        raise BrowserRuntimeError("网址缺少合法主机名")
    if parsed.username or parsed.password:
        raise BrowserRuntimeError("网址中不能携带用户名或密码")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BrowserRuntimeError(f"网址无法解析：{host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or str(ip) == "169.254.169.254":
            raise BrowserRuntimeError("该地址属于本机、链路本地或元数据网段，不允许访问")
        if ip.is_private and not settings.steward_browser_allow_private_networks:
            raise BrowserRuntimeError("当前部署未允许访问内网地址")
    return value


def validate_target_url(url: str) -> str:
    """Public URL-policy entrypoint shared by direct probes and the browser."""
    return _safe_url(url)


def _redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {
        str(k): ("••••••" if str(k).lower() in _SENSITIVE_HEADERS else str(v)[:500])
        for k, v in (headers or {}).items()
    }


def _json_shape(value: Any, depth: int = 0) -> Any:
    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k): _json_shape(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [_json_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def _find_pagination_fields(value: Any, depth: int = 0) -> dict[str, Any]:
    if depth > 4:
        return {}
    if isinstance(value, dict):
        found: dict[str, Any] = {}
        for key, val in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {
                "total", "totalcount", "pages", "totalpages", "hasnext", "hasmore",
                "nextcursor", "nextpage", "continuationtoken", "page", "pageno", "pagesize",
            } and isinstance(val, (str, int, float, bool, type(None))):
                found[str(key)] = val
            nested = _find_pagination_fields(val, depth + 1)
            for nested_key, nested_value in nested.items():
                found.setdefault(nested_key, nested_value)
        return found
    if isinstance(value, list) and value:
        return _find_pagination_fields(value[0], depth + 1)
    return {}


def analyze_pagination(url: str, parsed_body: Any = None) -> dict[str, Any] | None:
    params = {
        re.sub(r"[^a-z0-9]", "", k.lower()): v
        for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)
    }
    keys = set(params)
    mode = None
    if keys & _CURSOR_KEYS:
        mode = "cursor"
    elif keys & _OFFSET_KEYS:
        mode = "offset"
    elif keys & _PAGE_KEYS:
        mode = "page"
    fields = _find_pagination_fields(parsed_body)
    if not mode and fields:
        lowered = {re.sub(r"[^a-z0-9]", "", k.lower()) for k in fields}
        mode = "cursor" if lowered & {"nextcursor", "continuationtoken"} else "page"
    if not mode and not (keys & _SIZE_KEYS):
        return None
    return {
        "mode": mode or "limit",
        "requestParams": {k: v for k, v in params.items() if k in _PAGE_KEYS | _SIZE_KEYS | _OFFSET_KEYS | _CURSOR_KEYS},
        "responseFields": fields,
    }


def _is_api(resource_type: str, content_type: str, url: str) -> bool:
    ctype = content_type.lower()
    path = urlparse(url).path.lower()
    return resource_type in {"xhr", "fetch"} or "json" in ctype or "/api/" in path or path.startswith("/api")


def _is_file(content_type: str, headers: dict[str, str], url: str) -> bool:
    mime = content_type.split(";", 1)[0].lower().strip()
    disposition = (headers.get("content-disposition") or headers.get("Content-Disposition") or "").lower()
    return (
        "attachment" in disposition
        or mime in _FILE_MIMES
        or mime.startswith(("image/", "audio/", "video/"))
        or Path(urlparse(url).path).suffix.lower() in _FILE_EXTS
    )


def _download_filename(
    url: str, headers: dict[str, str] | None, mime_type: str | None,
    preferred: str | None = None,
) -> str:
    disposition = ((headers or {}).get("content-disposition")
                   or (headers or {}).get("Content-Disposition") or "")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.IGNORECASE)
    filename = preferred or (unquote(match.group(1).strip()) if match else "")
    if not filename:
        filename = Path(urlparse(url).path).name
    mime = (mime_type or "").split(";", 1)[0].lower().strip()
    if not filename:
        filename = "download"
    if not Path(filename).suffix and mime:
        filename += mimetypes.guess_extension(mime) or ""
    return workspace.safe_filename(filename, "download.bin")


def _validate_navigation_response(response: Any, target: str) -> None:
    """Fail visibly when a CDN/WAF returns an error document to ``page.goto``.

    Playwright considers any completed HTTP response a successful navigation,
    including non-standard anti-bot statuses such as EdgeOne's 567.  Without
    this guard the steward reported an empty blocked page as "opened" and the
    agent could then invent an interface analysis from no evidence.
    """
    if response is None:
        return
    status = int(getattr(response, "status", 0) or 0)
    if status < 400:
        return
    if status == 567:
        raise BrowserRuntimeError(
            f"页面返回 HTTP 567，站点的 CDN/WAF 拒绝了平台云服务器出口：{target}。"
            "这不是浏览器未安装。请在实时浏览器中切换到“我的电脑”来源，"
            "由本机网络完成人工验证后再继续；也可按站点规则使用官方 API。"
        )
    raise BrowserRuntimeError(
        f"页面返回 HTTP {status}，可能被登录门禁、CDN/WAF 或访问策略拦截：{target}。"
        "请在数据管家的实时浏览器中完成人工验证后重试；若站点提供官方 API，"
        "也可按其公开接入规则改走 API。"
    )


def public_capture(capture: dict) -> dict:
    result = {k: v for k, v in capture.items() if k not in {"requestHeaders", "responseHeaders", "responseBody"}}
    result["requestHeaders"] = _redact_headers(capture.get("requestHeaders"))
    result["responseHeaders"] = _redact_headers(capture.get("responseHeaders"))
    body = capture.get("responseBody")
    if body:
        result["responsePreview"] = body[:12_000]
    return result


@dataclass(frozen=True)
class BrowserTarget:
    """Resolved browser provider selected for one conversation."""
    key: str
    endpoint_url: str
    source_type: str = "managed"
    label: str = "平台浏览器"
    headers: dict[str, str] = field(default_factory=dict, compare=False, repr=False)


def managed_browser_target() -> BrowserTarget:
    return BrowserTarget(key="managed", endpoint_url=settings.steward_browser_cdp_url)


@dataclass
class BrowserSession:
    conversation_id: str
    context: Any
    page: Any
    workspace: workspace.SessionWorkspace
    user_id: str | None = None
    source_key: str = "managed"
    captures: list[dict] = field(default_factory=list)
    capture_tasks: set[asyncio.Task] = field(default_factory=set)
    last_state_saved: float = 0.0
    last_active: float = field(default_factory=time.time)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    cdp_session: Any = field(default=None, repr=False)
    screencast_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    screencast_active: bool = False
    screencast_failed: bool = False
    latest_frame: dict[str, Any] | None = field(default=None, repr=False)
    frame_version: int = 0
    frame_condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    def touch(self) -> None:
        self.last_active = time.time()

    def bind_page(self, page: Any) -> None:
        self.page = page
        self.touch()

        def on_response(response: Any) -> None:
            task = asyncio.create_task(self._capture_response(response))
            self.capture_tasks.add(task)
            task.add_done_callback(self.capture_tasks.discard)

        page.on("response", on_response)

        def on_download(download: Any) -> None:
            task = asyncio.create_task(self._save_download(download))
            self.capture_tasks.add(task)
            task.add_done_callback(self.capture_tasks.discard)

        page.on("download", on_download)

    async def _save_download(self, download: Any) -> None:
        try:
            path = await download.path()
            if not path:
                return
            content = Path(path).read_bytes()
            self.workspace.save_bytes(
                self.conversation_id, download.suggested_filename or "download.bin", content,
                source="download", source_url=download.url, extract=True,
            )
        except Exception:
            logger.warning("browser initiated download could not be saved", exc_info=True)

    async def _capture_response(self, response: Any) -> None:
        request = response.request
        try:
            request_headers = await request.all_headers()
        except Exception:
            request_headers = dict(request.headers or {})
        try:
            response_headers = await response.all_headers()
        except Exception:
            response_headers = dict(response.headers or {})
        content_type = response_headers.get("content-type", "")
        api = _is_api(request.resource_type, content_type, response.url)
        file_candidate = _is_file(content_type, response_headers, response.url)
        if not api and not file_candidate:
            return
        body_text = ""
        parsed_body = None
        length = int(response_headers.get("content-length") or 0) if str(response_headers.get("content-length") or "").isdigit() else 0
        if api and length <= 500_000:
            try:
                raw = await response.body()
                if len(raw) <= 500_000:
                    body_text = raw.decode("utf-8", errors="replace")
                    if "json" in content_type or body_text.lstrip()[:1] in {"{", "["}:
                        try:
                            parsed_body = json.loads(body_text)
                        except ValueError:
                            pass
            except Exception:
                pass
        capture = {
            "id": str(uuid.uuid4()),
            "method": request.method,
            "url": response.url,
            "resourceType": request.resource_type,
            "status": response.status,
            "contentType": content_type,
            "requestHeaders": request_headers,
            "requestBody": request.post_data,
            "responseHeaders": response_headers,
            "responseBody": body_text,
            "responseShape": _json_shape(parsed_body) if parsed_body is not None else None,
            "pagination": analyze_pagination(response.url, parsed_body),
            "isApi": api,
            "isFile": file_candidate,
            "capturedAt": time.time(),
        }
        self.captures.append(capture)
        self.captures = self.captures[-max(20, int(settings.steward_browser_max_captures)):]
        self.workspace.append_capture(self.conversation_id, capture)

    async def save_state(self) -> None:
        try:
            await self.context.storage_state(path=str(self.workspace.storage_state_path(self.conversation_id)))
            self.last_state_saved = time.time()
        except Exception:
            logger.debug("browser storage state save failed", exc_info=True)

    async def save_state_if_due(self, seconds: float = 5.0) -> None:
        if time.time() - self.last_state_saved >= seconds:
            await self.save_state()


class BrowserManager:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="steward-browser", daemon=True)
        self._thread.start()
        self._sessions: dict[str, BrowserSession] = {}
        self._pw = None
        self._browsers: dict[str, Any] = {}
        self._tickets: dict[str, tuple[str, str | None, float]] = {}
        self._ticket_lock = threading.Lock()
        self._live_clients: dict[str, int] = {}
        self._live_leases: dict[str, dict[str, float]] = {}
        self._user_controls: dict[str, dict[str, Any]] = {}
        self._reaper_task: asyncio.Task | None = None
        self._registry_lock = asyncio.Lock()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Coroutine) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call(self, coro: Coroutine, timeout: float | None = None):
        timeout = timeout or max(5, int(settings.steward_browser_timeout_seconds) + 5)
        future = self._submit(coro)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise BrowserRuntimeError("浏览器操作超时") from exc

    async def acall(self, coro: Coroutine, timeout: float | None = None):
        future = self._submit(coro)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout or 45)
        except asyncio.TimeoutError as exc:
            future.cancel()
            raise BrowserRuntimeError("浏览器操作超时") from exc

    def _ensure_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        while True:
            try:
                interval = max(5, int(settings.steward_browser_reaper_interval_seconds))
                await asyncio.sleep(interval)
                await self._reap_idle()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning("steward browser idle reaper failed", exc_info=True)

    def _is_live(self, conversation_id: str) -> bool:
        self._prune_live_leases(conversation_id)
        leases = getattr(self, "_live_leases", {}).get(conversation_id, {})
        return self._live_clients.get(conversation_id, 0) > 0 or bool(leases)

    def _control_ttl(self, mode: str) -> int:
        if mode == "held":
            return max(5, int(settings.steward_browser_http_lease_seconds))
        return max(1, int(settings.steward_browser_user_activity_seconds))

    def _prune_user_control(self, conversation_id: str | None = None) -> None:
        registry = getattr(self, "_user_controls", None)
        if not registry:
            return
        now = time.monotonic()
        conversation_ids = [conversation_id] if conversation_id else list(registry)
        for cid in conversation_ids:
            state = registry.get(cid)
            if state and float(state.get("expiresAt") or 0) <= now:
                registry.pop(cid, None)

    def _control_status(self, conversation_id: str) -> dict[str, Any]:
        self._prune_user_control(conversation_id)
        state = getattr(self, "_user_controls", {}).get(conversation_id)
        if not state:
            return {
                "controller": "agent",
                "mode": "observe",
                "agentCanAct": True,
                "expiresIn": 0,
            }
        remaining = max(0.0, float(state["expiresAt"]) - time.monotonic())
        return {
            "controller": "user",
            "mode": str(state["mode"]),
            "agentCanAct": False,
            "expiresIn": max(1, int(remaining + 0.999)),
        }

    def _claim_user_control(self, conversation_id: str, client_id: str,
                            *, mode: str) -> dict[str, Any]:
        self._prune_user_control(conversation_id)
        registry = getattr(self, "_user_controls", None)
        if registry is None:
            self._user_controls = {}
            registry = self._user_controls
        current = registry.get(conversation_id)
        # Normal clicks/keystrokes must not downgrade an explicit login hold.
        if current and current.get("mode") == "held" and mode == "transient":
            current["expiresAt"] = time.monotonic() + self._control_ttl("held")
        else:
            registry[conversation_id] = {
                "clientId": client_id,
                "mode": mode,
                "expiresAt": time.monotonic() + self._control_ttl(mode),
            }
        return self._control_status(conversation_id)

    def _renew_user_control(self, conversation_id: str, client_id: str) -> None:
        self._prune_user_control(conversation_id)
        state = getattr(self, "_user_controls", {}).get(conversation_id)
        if state and state.get("clientId") == client_id and state.get("mode") == "held":
            state["expiresAt"] = time.monotonic() + self._control_ttl("held")

    def _release_user_control(self, conversation_id: str,
                              client_id: str | None = None) -> dict[str, Any]:
        state = getattr(self, "_user_controls", {}).get(conversation_id)
        if state and (client_id is None or state.get("clientId") == client_id):
            self._user_controls.pop(conversation_id, None)
        return self._control_status(conversation_id)

    def _prune_live_leases(self, conversation_id: str | None = None) -> None:
        """Drop abandoned HTTP takeover leases.

        Native WebSockets have a close event, while HTTP polling does not.  The
        renewable lease makes a crashed tab fail open for the Agent after a
        bounded delay instead of holding the conversation forever.
        """
        registry = getattr(self, "_live_leases", None)
        if not registry:
            return
        now = time.monotonic()
        conversation_ids = [conversation_id] if conversation_id else list(registry)
        for cid in conversation_ids:
            leases = registry.get(cid)
            if not leases:
                continue
            for lease_id, expires_at in list(leases.items()):
                if expires_at <= now:
                    leases.pop(lease_id, None)
            if not leases:
                registry.pop(cid, None)
                session = self._sessions.get(cid)
                loop = getattr(self, "_loop", None)
                if (
                    session is not None
                    and self._live_clients.get(cid, 0) <= 0
                    and loop is not None
                    and loop.is_running()
                ):
                    self._submit(self._stop_screencast(session))

    @staticmethod
    def _is_busy(session: BrowserSession) -> bool:
        lock = getattr(session, "operation_lock", None)
        return bool(lock and lock.locked())

    async def _reap_idle(self, *, now: float | None = None) -> list[str]:
        idle_seconds = max(30, int(settings.steward_browser_idle_timeout_seconds))
        current = time.time() if now is None else now
        expired = [
            cid for cid, session in self._sessions.items()
            if not self._is_live(cid) and not self._is_busy(session)
            and current - session.last_active >= idle_seconds
        ]
        for cid in expired:
            await self._close(cid)
        return expired

    async def _evict_lru(self, *, user_id: str | None = None) -> str:
        candidates = [
            session for cid, session in self._sessions.items()
            if not self._is_live(cid) and not self._is_busy(session)
            and (user_id is None or session.user_id == user_id)
        ]
        if not candidates:
            scope = "当前用户" if user_id else "系统"
            raise BrowserRuntimeError(
                f"{scope}的浏览器会话已达到上限，且现有会话都在实时接管或执行操作中。"
                "请先关闭一个实时浏览器窗口，或等待正在执行的浏览器操作结束后重试。"
            )
        victim = min(candidates, key=lambda item: item.last_active)
        await self._close(victim.conversation_id)
        return victim.conversation_id

    async def _ensure_capacity(self, user_id: str | None) -> list[str]:
        reclaimed = await self._reap_idle()
        global_limit = max(1, int(settings.steward_browser_max_sessions))
        while len(self._sessions) >= global_limit:
            reclaimed.append(await self._evict_lru())

        if user_id:
            per_user_limit = max(1, int(settings.steward_browser_max_sessions_per_user))
            while sum(1 for session in self._sessions.values() if session.user_id == user_id) >= per_user_limit:
                reclaimed.append(await self._evict_lru(user_id=user_id))
        return reclaimed

    def _assert_actor_allowed(self, conversation_id: str, actor: str) -> None:
        if actor == "agent" and self._control_status(conversation_id)["controller"] == "user":
            raise BrowserRuntimeError(
                "用户正在协作浏览器中操作，数据管家已在当前步骤等待。"
                "完成后点击“继续交给数据管家”即可恢复，无需关闭浏览器。"
            )

    async def _wait_actor_allowed(self, conversation_id: str, actor: str) -> None:
        if actor != "agent":
            return
        while True:
            status = self._control_status(conversation_id)
            if status["controller"] != "user":
                return
            if status["mode"] == "held":
                self._assert_actor_allowed(conversation_id, actor)
            # A normal human click or keystroke is cooperative, not an error:
            # wait for the short quiet window, then continue automatically.
            await asyncio.sleep(min(0.25, max(0.05, float(status["expiresIn"]))))

    @asynccontextmanager
    async def _browser_operation(self, session: BrowserSession,
                                 conversation_id: str, actor: str):
        """Serialize page mutations while giving freshly queued user input priority."""
        if actor == "user":
            self._claim_user_control(
                conversation_id, "rest-user", mode="transient")
        while True:
            await self._wait_actor_allowed(conversation_id, actor)
            await session.operation_lock.acquire()
            if (actor != "agent"
                    or self._control_status(conversation_id)["controller"] != "user"):
                break
            # User input may have arrived after the pre-lock check. Let that
            # queued interaction run first, then retry after the quiet window.
            session.operation_lock.release()
        try:
            yield
        finally:
            session.operation_lock.release()

    async def _attach_live(self, conversation_id: str) -> None:
        session = await self._require(conversation_id)
        # Attaching a viewer never takes control, but still wait for an
        # in-flight navigation before starting frame capture so the first frame
        # is a stable page rather than a half-painted transition.
        async with session.operation_lock:
            self._live_clients[conversation_id] = self._live_clients.get(conversation_id, 0) + 1
            session.touch()
        await self._ensure_screencast(session)

    async def attach_live(self, conversation_id: str) -> None:
        await self.acall(
            self._attach_live(conversation_id),
            timeout=max(10, int(settings.steward_browser_timeout_seconds) + 5),
        )

    async def _session_info(self, conversation_id: str) -> dict:
        """Return a cheap snapshot without scraping or mutating the page."""
        session = self._sessions.get(conversation_id)
        if not session or session.page.is_closed():
            return {
                "active": False, "url": "", "live": False,
                "collaboration": self._control_status(conversation_id),
            }
        session.touch()
        return {
            "active": True,
            "url": session.page.url,
            "live": self._is_live(conversation_id),
            "collaboration": self._control_status(conversation_id),
        }

    def session_info(self, conversation_id: str) -> dict:
        return self.call(self._session_info(conversation_id), timeout=10)

    async def _detach_live(self, conversation_id: str,
                           client_id: str | None = None) -> None:
        count = self._live_clients.get(conversation_id, 0)
        if count <= 1:
            self._live_clients.pop(conversation_id, None)
        else:
            self._live_clients[conversation_id] = count - 1
        session = self._sessions.get(conversation_id)
        if session:
            session.touch()
        if client_id:
            self._release_user_control(conversation_id, client_id)
        if not self._is_live(conversation_id) and session:
            await self._stop_screencast(session)

    async def detach_live(self, conversation_id: str,
                          client_id: str | None = None) -> None:
        await self.acall(self._detach_live(conversation_id, client_id), timeout=10)

    def _http_lease_ttl(self) -> int:
        return max(5, int(settings.steward_browser_http_lease_seconds))

    async def _attach_http_live(self, conversation_id: str) -> dict:
        session = await self._require(conversation_id)
        async with session.operation_lock:
            lease_id = secrets.token_urlsafe(24)
            expires_at = time.monotonic() + self._http_lease_ttl()
            self._live_leases.setdefault(conversation_id, {})[lease_id] = expires_at
            session.touch()
        await self._ensure_screencast(session)
        return {
            "leaseId": lease_id,
            "expiresIn": self._http_lease_ttl(),
            "frameIntervalMs": max(250, int(settings.steward_browser_http_frame_interval_ms)),
            "collaboration": self._control_status(conversation_id),
        }

    def attach_http_live(self, conversation_id: str) -> dict:
        return self.call(
            self._attach_http_live(conversation_id),
            timeout=max(10, int(settings.steward_browser_timeout_seconds) + 5),
        )

    async def _renew_http_live(self, conversation_id: str, lease_id: str) -> None:
        self._prune_live_leases(conversation_id)
        leases = self._live_leases.get(conversation_id, {})
        if not lease_id or lease_id not in leases:
            raise BrowserRuntimeError("HTTP 实时浏览器接管已过期，请重新连接")
        leases[lease_id] = time.monotonic() + self._http_lease_ttl()
        self._renew_user_control(conversation_id, lease_id)
        session = await self._require(conversation_id)
        session.touch()

    async def _http_live_screenshot(self, conversation_id: str, lease_id: str) -> dict:
        await self._renew_http_live(conversation_id, lease_id)
        frame, _version = await self._next_live_frame(
            conversation_id, client_id=lease_id, after_version=-1, timeout=0)
        return frame

    def http_live_screenshot(self, conversation_id: str, lease_id: str) -> dict:
        return self.call(
            self._http_live_screenshot(conversation_id, lease_id),
            timeout=max(10, int(settings.steward_browser_timeout_seconds) + 5),
        )

    async def _http_live_input(
        self, conversation_id: str, lease_id: str, message: dict,
    ) -> dict[str, Any]:
        await self._renew_http_live(conversation_id, lease_id)
        return await self._input(conversation_id, message, client_id=lease_id)

    def http_live_input(self, conversation_id: str, lease_id: str,
                        message: dict) -> dict[str, Any]:
        return self.call(
            self._http_live_input(conversation_id, lease_id, message),
            timeout=max(10, int(settings.steward_browser_timeout_seconds) + 5),
        )

    async def _set_live_control(self, conversation_id: str, client_id: str,
                                action: str) -> dict[str, Any]:
        session = await self._require(conversation_id)
        session.touch()
        if action == "hold":
            return self._claim_user_control(
                conversation_id, client_id, mode="held")
        if action == "release":
            return self._release_user_control(conversation_id, client_id)
        raise BrowserRuntimeError("协作浏览器控制动作只支持 hold 或 release")

    async def set_live_control(self, conversation_id: str, client_id: str,
                               action: str) -> dict[str, Any]:
        return await self.acall(
            self._set_live_control(conversation_id, client_id, action), timeout=10)

    def http_live_control(self, conversation_id: str, lease_id: str,
                          action: str) -> dict[str, Any]:
        return self.call(
            self._set_live_control(conversation_id, lease_id, action), timeout=10)

    async def _release_http_live(self, conversation_id: str, lease_id: str) -> None:
        leases = self._live_leases.get(conversation_id)
        if leases:
            leases.pop(lease_id, None)
            if not leases:
                self._live_leases.pop(conversation_id, None)
        self._release_user_control(conversation_id, lease_id)
        session = self._sessions.get(conversation_id)
        if session:
            session.touch()
            if not self._is_live(conversation_id):
                await self._stop_screencast(session)

    def release_http_live(self, conversation_id: str, lease_id: str) -> None:
        self.call(self._release_http_live(conversation_id, lease_id), timeout=10)

    async def _capacity_status(self) -> dict:
        await self._reap_idle()
        return {
            "activeSessions": len(self._sessions),
            "liveSessions": sum(1 for cid in self._sessions if self._is_live(cid)),
            "maxSessions": max(1, int(settings.steward_browser_max_sessions)),
            "maxSessionsPerUser": max(1, int(settings.steward_browser_max_sessions_per_user)),
            "idleTimeoutSeconds": max(30, int(settings.steward_browser_idle_timeout_seconds)),
        }

    def capacity_status(self) -> dict:
        return self.call(self._capacity_status(), timeout=10)

    async def _ensure_browser(self, target: BrowserTarget):
        existing = self._browsers.get(target.key)
        if existing and existing.is_connected():
            return existing
        self._browsers.pop(target.key, None)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserRuntimeError("服务端未安装 Playwright 浏览器组件") from exc
        if self._pw is None:
            self._pw = await async_playwright().start()
        try:
            endpoint = _resolve_cdp_endpoint(target.endpoint_url)
            browser = await self._pw.chromium.connect_over_cdp(
                endpoint,
                headers=target.headers or None,
                timeout=int(settings.steward_browser_timeout_seconds) * 1000,
            )
        except Exception as exc:
            raise BrowserRuntimeError(
                f"无法连接{target.label}：{exc}"
            ) from exc
        self._browsers[target.key] = browser
        return browser

    async def _test_target(self, target: BrowserTarget) -> dict:
        browser = await self._ensure_browser(target)
        return {
            "reachable": bool(browser.is_connected()),
            "sourceType": target.source_type,
            "label": target.label,
        }

    def test_target(self, target: BrowserTarget) -> dict:
        return self.call(self._test_target(target), timeout=max(
            10, int(settings.steward_browser_timeout_seconds) + 5))

    async def _route_guard(self, route: Any) -> None:
        try:
            scheme = urlparse(route.request.url).scheme.lower()
            if scheme in {"data", "blob", "about"}:
                await route.continue_()
                return
            _safe_url(route.request.url)
            await route.continue_()
        except BrowserRuntimeError:
            await route.abort("blockedbyclient")

    async def _start(self, conversation_id: str, url: str, *, user_id: str | None = None,
                     actor: str = "agent", browser_target: BrowserTarget | None = None,
                     session_workspace: workspace.SessionWorkspace | None = None) -> dict:
        navigation_target = _safe_url(url)
        selected_target = browser_target or managed_browser_target()
        session_workspace = session_workspace or workspace.steward_session_workspace()
        user_id = str(user_id) if user_id is not None else None
        self._ensure_reaper()
        await self._wait_actor_allowed(conversation_id, actor)
        session = self._sessions.get(conversation_id)
        if session is not None and session.source_key != selected_target.key:
            await self._close(conversation_id)
            session = None
        reclaimed: list[str] = []
        restored = False
        if session is None:
            async with self._registry_lock:
                # Two requests for the same conversation may arrive together.
                session = self._sessions.get(conversation_id)
                if session is None:
                    reclaimed = await self._ensure_capacity(user_id)
                    browser = await self._ensure_browser(selected_target)
                    state = session_workspace.storage_state_path(conversation_id)
                    restored = state.exists()
                    kwargs = {
                        "accept_downloads": True,
                        "viewport": {"width": 1365, "height": 768},
                        "locale": "zh-CN",
                    }
                    if restored:
                        kwargs["storage_state"] = str(state)
                    context = await browser.new_context(**kwargs)
                    await context.route("**/*", self._route_guard)
                    page = await context.new_page()
                    session = BrowserSession(
                        conversation_id, context, page,
                        workspace=session_workspace,
                        user_id=user_id, source_key=selected_target.key,
                    )
                    session.bind_page(page)

                    def on_page(new_page: Any) -> None:
                        session.bind_page(new_page)
                        if self._is_live(session.conversation_id):
                            asyncio.create_task(self._restart_screencast(session))

                    context.on("page", on_page)
                    self._sessions[conversation_id] = session
        elif user_id is not None and session.user_id is None:
            session.user_id = user_id

        async with self._browser_operation(session, conversation_id, actor):
            session.touch()
            try:
                response = await session.page.goto(
                    navigation_target,
                    wait_until="commit" if actor == "user" else "domcontentloaded",
                    timeout=int(settings.steward_browser_timeout_seconds) * 1000,
                )
                _validate_navigation_response(response, navigation_target)
            except Exception as exc:
                if "Download is starting" not in str(exc):
                    raise BrowserRuntimeError(f"页面打开失败：{exc}") from exc
                await session.page.wait_for_timeout(500)
                await session.save_state()
                session.touch()
                return {"url": session.page.url, "title": "文件下载已触发",
                        "downloadStarted": True, "targetUrl": navigation_target,
                        "browserSource": selected_target.key,
                        "restoredSession": restored, "reclaimedSessionCount": len(reclaimed)}
            await session.save_state_if_due()
            session.touch()
            result = await self._navigation_result(
                conversation_id, actor=actor, session=session)
        result.update({
            "restoredSession": restored,
            "reclaimedSessionCount": len(reclaimed),
            "browserSource": selected_target.key,
        })
        return result

    def start(self, conversation_id: str, url: str, *, user_id: str | None = None,
              actor: str = "agent", browser_target: BrowserTarget | None = None,
              session_workspace: workspace.SessionWorkspace | None = None) -> dict:
        return self.call(self._start(
            conversation_id, url, user_id=user_id, actor=actor,
            browser_target=browser_target, session_workspace=session_workspace))

    async def _require(self, conversation_id: str) -> BrowserSession:
        session = self._sessions.get(conversation_id)
        if not session or session.page.is_closed():
            raise BrowserRuntimeError("当前会话尚未启动浏览器，请先打开目标网址")
        return session

    async def _state(self, conversation_id: str, *, actor: str = "agent",
                     check_control: bool = True) -> dict:
        if check_control:
            await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        session.touch()
        page = session.page
        try:
            title = await page.title()
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            title, body = "", ""
        elements = await page.evaluate(r"""(selector) => {
          return [...document.querySelectorAll(selector)].slice(0, 120).map((el, index) => {
            const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
            const resourceUrl = el.currentSrc || el.src || el.href || '';
            return {index, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
              text: ((el.getAttribute('type') || '').toLowerCase() === 'password' ? '' :
                (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || '')).trim().replace(/\s+/g,' ').slice(0,160),
              downloadable: Boolean(resourceUrl),
              x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height),
              visible: r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'};
          }).filter(x => x.visible);
        }""", _PAGE_ELEMENT_SELECTOR)
        return {"url": page.url, "title": title, "text": body[:20_000], "elements": elements[:100]}

    def state(self, conversation_id: str, *, actor: str = "agent") -> dict:
        return self.call(self._state(conversation_id, actor=actor))

    async def _cheap_page_view(self, session: BrowserSession) -> dict:
        title = ""
        try:
            title = await session.page.title()
        except Exception:
            pass
        return {
            "url": session.page.url,
            "title": title,
            "text": "",
            "elements": [],
        }

    async def _navigation_result(
        self, conversation_id: str, *, actor: str, session: BrowserSession,
    ) -> dict:
        if actor == "user":
            return await self._cheap_page_view(session)
        return await self._state(
            conversation_id, actor=actor, check_control=False)

    async def _page_resources(
        self, conversation_id: str, keyword: str | None = None, limit: int = 50,
        *, actor: str = "agent",
    ) -> list[dict]:
        await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        async with self._browser_operation(session, conversation_id, actor):
            session.touch()
            rows = await session.page.evaluate(r"""(selector) => {
              return [...document.querySelectorAll(selector)].slice(0, 240).map((el, index) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const url = el.currentSrc || el.src || el.href || '';
                const text = (el.innerText || el.getAttribute('alt') || el.getAttribute('title') ||
                  el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 160);
                return {index, tag: el.tagName.toLowerCase(), text,
                  resourceUrl: String(url).slice(0, 2000),
                  visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none'};
              }).filter(item => item.visible && item.resourceUrl);
            }""", _PAGE_ELEMENT_SELECTOR)
        needle = (keyword or "").strip().lower()
        if needle:
            rows = [row for row in rows if needle in str(row.get("resourceUrl", "")).lower()
                    or needle in str(row.get("text", "")).lower()]
        return rows[:max(1, min(int(limit), 100))]

    def page_resources(
        self, conversation_id: str, keyword: str | None = None, limit: int = 50,
        *, actor: str = "agent",
    ) -> list[dict]:
        return self.call(self._page_resources(
            conversation_id, keyword, limit, actor=actor))

    async def _navigate(self, conversation_id: str, url: str, *, actor: str = "agent") -> dict:
        await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        target = _safe_url(url)
        async with self._browser_operation(session, conversation_id, actor):
            session.touch()
            try:
                response = await session.page.goto(
                    target,
                    wait_until="commit" if actor == "user" else "domcontentloaded",
                    timeout=int(settings.steward_browser_timeout_seconds) * 1000,
                )
                _validate_navigation_response(response, target)
            except Exception as exc:
                if "Download is starting" not in str(exc):
                    raise BrowserRuntimeError(f"页面跳转失败：{exc}") from exc
                await session.page.wait_for_timeout(500)
                await session.save_state()
                session.touch()
                return {"url": session.page.url, "title": "文件下载已触发",
                        "downloadStarted": True, "targetUrl": target}
            await session.save_state_if_due()
            session.touch()
            return await self._navigation_result(
                conversation_id, actor=actor, session=session)

    def navigate(self, conversation_id: str, url: str, *, actor: str = "agent") -> dict:
        return self.call(self._navigate(conversation_id, url, actor=actor))

    async def _finish_click(self, conversation_id: str, session: BrowserSession,
                            locator: Any, *, actor: str) -> dict:
        before = {row["id"] for row in session.workspace.list_files(conversation_id)}
        await locator.scroll_into_view_if_needed(timeout=3000)
        await locator.click(timeout=5000)
        await session.page.wait_for_timeout(500)
        pending = list(session.capture_tasks)
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=5)
            except asyncio.TimeoutError:
                pass
        await session.save_state()
        session.touch()
        state = await self._state(
            conversation_id, actor=actor, check_control=False)
        state["downloadedFiles"] = [
            row for row in session.workspace.list_files(conversation_id) if row["id"] not in before
        ]
        return state

    async def _click_text(self, conversation_id: str, text: str, *, actor: str = "agent") -> dict:
        await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        query = (text or "").strip()
        if not query:
            raise BrowserRuntimeError("点击文本不能为空")
        async with self._browser_operation(session, conversation_id, actor):
            session.touch()
            locator = session.page.get_by_text(query, exact=True).first
            if await locator.count() == 0:
                locator = session.page.get_by_text(query, exact=False).first
            if await locator.count() == 0:
                raise BrowserRuntimeError(f"当前页面未找到文本「{query}」")
            return await self._finish_click(conversation_id, session, locator, actor=actor)

    def click_text(self, conversation_id: str, text: str, *, actor: str = "agent") -> dict:
        return self.call(self._click_text(conversation_id, text, actor=actor))

    async def _click_element(self, conversation_id: str, element_index: int,
                             *, actor: str = "agent") -> dict:
        await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        async with self._browser_operation(session, conversation_id, actor):
            locator = session.page.locator(_PAGE_ELEMENT_SELECTOR).nth(int(element_index))
            if await locator.count() == 0:
                raise BrowserRuntimeError("页面元素已变化，请重新读取 browser_state 后再点击")
            return await self._finish_click(conversation_id, session, locator, actor=actor)

    def click_element(self, conversation_id: str, element_index: int,
                      *, actor: str = "agent") -> dict:
        return self.call(self._click_element(
            conversation_id, element_index, actor=actor))

    async def _scroll(self, conversation_id: str, position: str = "page_down",
                      *, actor: str = "agent") -> dict:
        await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        target = (position or "page_down").strip()
        if target not in {"top", "bottom", "page_down", "page_up"}:
            raise BrowserRuntimeError(
                "position 只能是 top、bottom、page_down 或 page_up")
        async with self._browser_operation(session, conversation_id, actor):
            session.touch()
            page = session.page
            before = await page.evaluate(
                "() => ({y: window.scrollY, height: document.body.scrollHeight, inner: window.innerHeight})")
            if target == "bottom":
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            elif target == "top":
                await page.evaluate("() => window.scrollTo(0, 0)")
            elif target == "page_down":
                await page.mouse.wheel(0, 900)
            else:
                await page.mouse.wheel(0, -900)
            await page.wait_for_timeout(600)
            after = await page.evaluate(
                "() => ({y: window.scrollY, height: document.body.scrollHeight, inner: window.innerHeight})")
            state = await self._state(
                conversation_id, actor=actor, check_control=False)
            state["scroll"] = {"position": target, "before": before, "after": after}
            return state

    def scroll(self, conversation_id: str, position: str = "page_down",
               *, actor: str = "agent") -> dict:
        return self.call(self._scroll(conversation_id, position, actor=actor))

    async def _save_page_resource(
        self, conversation_id: str, element_index: int, filename: str | None = None,
        *, actor: str = "agent",
    ) -> dict:
        await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        async with self._browser_operation(session, conversation_id, actor):
            session.touch()
            info = await session.page.evaluate(r"""({selector, index}) => {
              const el = document.querySelectorAll(selector)[index];
              if (!el) return null;
              const url = el.currentSrc || el.src || el.href || '';
              return {url: String(url), filename: el.download || el.alt || el.title || ''};
            }""", {"selector": _PAGE_ELEMENT_SELECTOR, "index": int(element_index)})
            if not info or not info.get("url"):
                raise BrowserRuntimeError(
                    "该元素没有可保存的 href/src/currentSrc；请重新调用 browser_page_resources")
            resource_url = str(info["url"])
            preferred = filename or str(info.get("filename") or "") or None
            if resource_url.startswith("data:"):
                header, _, payload = resource_url.partition(",")
                if not payload:
                    raise BrowserRuntimeError("图片 data URL 内容为空")
                mime = header[5:].split(";", 1)[0] or "application/octet-stream"
                content = base64.b64decode(payload) if ";base64" in header else unquote_to_bytes(payload)
                source_url = "data:"
                headers: dict[str, str] = {}
            elif resource_url.startswith("blob:"):
                result = await session.page.evaluate(r"""async ({url, maxBytes}) => {
                  const response = await fetch(url);
                  const blob = await response.blob();
                  if (blob.size > maxBytes) return {tooLarge: blob.size, type: blob.type};
                  const bytes = new Uint8Array(await blob.arrayBuffer());
                  let binary = '';
                  for (let i = 0; i < bytes.length; i += 0x8000) {
                    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
                  }
                  return {data: btoa(binary), type: blob.type};
                }""", {
                    "url": resource_url,
                    "maxBytes": int(settings.max_upload_mb) * 1024 * 1024,
                })
                if result.get("tooLarge"):
                    raise BrowserRuntimeError(
                        f"资源超过大小限制 {settings.max_upload_mb}MB")
                content = base64.b64decode(result.get("data") or "")
                mime = result.get("type") or "application/octet-stream"
                source_url = "blob:"
                headers = {}
            else:
                target = _safe_url(resource_url)
                response = await session.context.request.get(
                    target,
                    headers={"Referer": session.page.url},
                    timeout=int(settings.steward_browser_timeout_seconds) * 1000,
                )
                if not response.ok:
                    raise BrowserRuntimeError(f"页面资源保存失败：HTTP {response.status}")
                content = await response.body()
                headers = response.headers
                mime = headers.get("content-type") or "application/octet-stream"
                source_url = target
            saved_name = _download_filename(resource_url, headers, mime, preferred)
            row = session.workspace.save_bytes(
                conversation_id, saved_name, content, source="download",
                mime_type=mime, source_url=source_url, extract=True,
            )
            session.touch()
            return row

    def save_page_resource(
        self, conversation_id: str, element_index: int, filename: str | None = None,
        *, actor: str = "agent",
    ) -> dict:
        return self.call(self._save_page_resource(
            conversation_id, element_index, filename, actor=actor),
            timeout=max(60, int(settings.steward_browser_timeout_seconds) + 10),
        )

    async def _type(self, conversation_id: str, selector: str, text: str,
                    press_enter: bool = False, *, actor: str = "agent") -> dict:
        await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        async with self._browser_operation(session, conversation_id, actor):
            session.touch()
            locator = session.page.locator(selector).first
            if await locator.count() == 0:
                raise BrowserRuntimeError("未找到要输入的元素")
            input_type = (await locator.get_attribute("type") or "").lower()
            if input_type == "password":
                raise BrowserRuntimeError("密码必须由用户在实时浏览器画面中手动输入，Agent 不读取也不代填")
            await locator.fill(text)
            if press_enter:
                await locator.press("Enter")
            session.touch()
            return await self._state(
                conversation_id, actor=actor, check_control=False)

    def type_text(self, conversation_id: str, selector: str, text: str,
                  press_enter: bool = False, *, actor: str = "agent") -> dict:
        return self.call(self._type(conversation_id, selector, text, press_enter, actor=actor))

    async def _input(self, conversation_id: str, message: dict,
                     *, client_id: str = "live-user") -> dict[str, Any]:
        session = await self._require(conversation_id)
        kind = message.get("type")
        action = message.get("action")
        hover_move = kind == "mouse" and action == "move"
        if not hover_move:
            self._claim_user_control(
                conversation_id, client_id, mode="transient")
        page = session.page
        if hover_move:
            session.touch()
            await page.mouse.move(float(message.get("x", 0)), float(message.get("y", 0)))
            session.touch()
            return self._control_status(conversation_id)
        async with session.operation_lock:
            session.touch()
            if kind == "mouse":
                x, y = float(message.get("x", 0)), float(message.get("y", 0))
                if action == "down":
                    await page.mouse.move(x, y); await page.mouse.down(button=message.get("button", "left"))
                elif action == "up":
                    await page.mouse.move(x, y); await page.mouse.up(button=message.get("button", "left"))
                elif action == "click":
                    await page.mouse.click(x, y, button=message.get("button", "left"), click_count=int(message.get("clickCount", 1)))
            elif kind == "wheel":
                await page.mouse.wheel(float(message.get("deltaX", 0)), float(message.get("deltaY", 0)))
            elif kind == "key":
                key = str(message.get("key") or "")
                if key:
                    await page.keyboard.press(key)
            elif kind == "text":
                await page.keyboard.insert_text(str(message.get("text") or ""))
            session.touch()
            return self._control_status(conversation_id)

    async def input(self, conversation_id: str, message: dict,
                    *, client_id: str = "live-user") -> dict[str, Any]:
        return await self.acall(
            self._input(conversation_id, message, client_id=client_id))

    async def _publish_live_frame(self, session: BrowserSession, data_b64: str) -> None:
        url = ""
        try:
            if session.page and not session.page.is_closed():
                url = session.page.url
        except Exception:
            pass
        if not hasattr(session, "frame_condition"):
            session.frame_condition = asyncio.Condition()
            session.frame_version = 0
        async with session.frame_condition:
            session.latest_frame = {
                "data": data_b64,
                "url": url,
                "collaboration": self._control_status(session.conversation_id),
            }
            session.frame_version += 1
            session.frame_condition.notify_all()

    async def _on_screencast_frame(
        self, session: BrowserSession, cdp: Any, params: dict[str, Any],
    ) -> None:
        stale = (
            cdp is not getattr(session, "cdp_session", None)
            or not getattr(session, "screencast_active", False)
        )
        data = params.get("data")
        if not stale and data:
            await self._publish_live_frame(session, str(data))
        session_id = params.get("sessionId")
        if session_id is None:
            return
        delay = max(0, int(settings.steward_browser_frame_interval_ms)) / 1000
        if delay and not stale:
            await asyncio.sleep(delay)
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            logger.debug("screencast frame ack failed", exc_info=True)

    async def _detach_cdp(self, cdp: Any) -> None:
        if cdp is None:
            return
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await cdp.detach()
        except Exception:
            pass

    async def _stop_screencast(self, session: BrowserSession) -> None:
        lock = getattr(session, "screencast_lock", None)
        if lock is None:
            session.screencast_lock = asyncio.Lock()
            lock = session.screencast_lock
        async with lock:
            cdp = getattr(session, "cdp_session", None)
            session.screencast_active = False
            session.cdp_session = None
            await self._detach_cdp(cdp)

    async def _restart_screencast(self, session: BrowserSession) -> None:
        session.screencast_failed = False
        await self._stop_screencast(session)
        if self._is_live(session.conversation_id):
            await self._ensure_screencast(session)

    async def _ensure_screencast(self, session: BrowserSession) -> None:
        if getattr(session, "screencast_failed", False):
            return
        if getattr(session, "screencast_active", False) and getattr(session, "cdp_session", None) is not None:
            return
        page = getattr(session, "page", None)
        if page is None or page.is_closed():
            return
        if not hasattr(session, "screencast_lock"):
            session.screencast_lock = asyncio.Lock()
        if not hasattr(session, "frame_condition"):
            session.frame_condition = asyncio.Condition()
            session.frame_version = 0
            session.latest_frame = None
            session.cdp_session = None
        async with session.screencast_lock:
            if getattr(session, "screencast_failed", False):
                return
            if getattr(session, "screencast_active", False) and getattr(session, "cdp_session", None) is not None:
                return
            if page.is_closed():
                return
            cdp = None
            session.screencast_active = True
            try:
                cdp = await page.context.new_cdp_session(page)
                session.cdp_session = cdp

                def handler(params: dict[str, Any]) -> None:
                    asyncio.create_task(self._on_screencast_frame(session, cdp, params))

                cdp.on("Page.screencastFrame", handler)
                await cdp.send("Page.enable")
                await cdp.send("Page.startScreencast", {
                    "format": "jpeg",
                    "quality": 55,
                    "maxWidth": 1365,
                    "maxHeight": 768,
                    "everyNthFrame": 1,
                })
            except Exception:
                session.screencast_failed = True
                session.screencast_active = False
                session.cdp_session = None
                await self._detach_cdp(cdp)
                logger.warning(
                    "CDP screencast unavailable, live view falls back to screenshots",
                    exc_info=True,
                )

    async def _capture_jpeg(self, session: BrowserSession) -> str:
        image = await session.page.screenshot(
            type="jpeg", quality=55, animations="allow")
        return base64.b64encode(image).decode("ascii")

    def _screenshot_fallback_delay_s(self) -> float:
        return max(0.25, int(settings.steward_browser_frame_interval_ms) / 1000)

    async def _live_frame_payload(self, session: BrowserSession, conversation_id: str) -> tuple[dict, int]:
        page_url = session.page.url if session.page and not session.page.is_closed() else ""
        frame = dict(session.latest_frame or {})
        frame["url"] = page_url
        frame["collaboration"] = self._control_status(conversation_id)
        return frame, session.frame_version

    async def _next_live_frame(
        self, conversation_id: str, *, client_id: str | None = None,
        after_version: int = -1, timeout: float = 1.0,
    ) -> tuple[dict, int]:
        session = await self._require(conversation_id)
        if client_id:
            self._renew_user_control(conversation_id, client_id)
        session.touch()
        await self._ensure_screencast(session)
        if not getattr(session, "screencast_active", False):
            if after_version >= 0:
                await asyncio.sleep(self._screenshot_fallback_delay_s())
            await self._publish_live_frame(session, await self._capture_jpeg(session))
            return await self._live_frame_payload(session, conversation_id)
        if not hasattr(session, "frame_condition"):
            session.frame_condition = asyncio.Condition()
        if session.frame_version == after_version and timeout > 0:
            async with session.frame_condition:
                if session.frame_version == after_version:
                    try:
                        await asyncio.wait_for(
                            session.frame_condition.wait_for(
                                lambda: session.frame_version != after_version),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        pass
        if session.latest_frame is None:
            await self._publish_live_frame(session, await self._capture_jpeg(session))
        return await self._live_frame_payload(session, conversation_id)

    async def next_live_frame(
        self, conversation_id: str, *, client_id: str | None = None,
        after_version: int = -1, timeout: float = 1.0,
    ) -> tuple[dict, int]:
        return await self.acall(self._next_live_frame(
            conversation_id, client_id=client_id,
            after_version=after_version, timeout=timeout,
        ))

    async def _screenshot(self, conversation_id: str,
                          client_id: str | None = None) -> dict:
        frame, _version = await self._next_live_frame(
            conversation_id, client_id=client_id, after_version=-1, timeout=0)
        return frame

    async def screenshot(self, conversation_id: str,
                         client_id: str | None = None) -> dict:
        return await self.acall(self._screenshot(conversation_id, client_id))

    def list_captures(self, conversation_id: str, keyword: str | None = None, limit: int = 50,
                      *, session_workspace: workspace.SessionWorkspace | None = None) -> list[dict]:
        source = session_workspace or workspace.steward_session_workspace()
        rows = source.load_captures(conversation_id, max(limit * 4, 100))
        if keyword:
            needle = keyword.lower()
            rows = [r for r in rows if needle in str(r.get("url", "")).lower() or needle in str(r.get("responseBody", "")).lower()]
        return [public_capture(r) for r in rows[-max(1, min(limit, 100)):]]

    async def _download(self, conversation_id: str, capture_id: str, *, actor: str = "agent") -> dict:
        await self._wait_actor_allowed(conversation_id, actor)
        session = await self._require(conversation_id)
        capture = session.workspace.require_capture(conversation_id, capture_id)
        if capture.get("method") != "GET":
            raise BrowserRuntimeError("自动下载只重放 GET 请求，其他方法请先在页面中触发下载")
        headers = {
            k: v for k, v in (capture.get("requestHeaders") or {}).items()
            if k.lower() not in {"host", "content-length", "cookie", "accept-encoding"}
        }
        async with self._browser_operation(session, conversation_id, actor):
            session.touch()
            response = await session.context.request.get(capture["url"], headers=headers, timeout=int(settings.steward_browser_timeout_seconds) * 1000)
            if not response.ok:
                raise BrowserRuntimeError(f"下载失败：HTTP {response.status}")
            content = await response.body()
            response_headers = response.headers
            filename = _download_filename(
                capture["url"], response_headers, response_headers.get("content-type"))
            row = session.workspace.save_bytes(
                conversation_id, filename, content, source="download",
                mime_type=response_headers.get("content-type"), source_url=capture["url"], extract=True,
            )
            await session.save_state()
            session.touch()
            return row

    def download(self, conversation_id: str, capture_id: str, *, actor: str = "agent") -> dict:
        return self.call(self._download(conversation_id, capture_id, actor=actor), timeout=max(60, int(settings.steward_browser_timeout_seconds) + 10))

    async def _close(self, conversation_id: str) -> None:
        session = self._sessions.get(conversation_id)
        if session:
            async with session.operation_lock:
                if self._sessions.get(conversation_id) is not session:
                    return
                self._sessions.pop(conversation_id, None)
                await self._stop_screencast(session)
                await session.save_state()
                if session.capture_tasks:
                    await asyncio.gather(*session.capture_tasks, return_exceptions=True)
                await session.context.close()
        self._live_clients.pop(conversation_id, None)
        self._live_leases.pop(conversation_id, None)
        self._user_controls.pop(conversation_id, None)

    def close(self, conversation_id: str) -> None:
        self.call(self._close(conversation_id), timeout=20)

    def close_by_source(self, source_key: str) -> None:
        for cid, session in list(self._sessions.items()):
            if session.source_key != source_key:
                continue
            try:
                self.close(cid)
            except Exception:
                logger.warning("关闭来源 %s 的浏览器会话失败: %s", source_key, cid, exc_info=True)

    async def _close_all(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            await asyncio.gather(self._reaper_task, return_exceptions=True)
        self._reaper_task = None
        for conversation_id in list(self._sessions):
            try:
                await self._close(conversation_id)
            except Exception:
                logger.warning("failed to close steward browser session %s", conversation_id, exc_info=True)
        for key, browser in list(self._browsers.items()):
            if browser and browser.is_connected():
                try:
                    await browser.close()  # CDP detach; does not terminate remote Chromium
                except Exception:
                    logger.debug("browser CDP detach failed for %s", key, exc_info=True)
        self._browsers.clear()

    def close_all(self) -> None:
        self.call(self._close_all(), timeout=30)

    def issue_ticket(self, conversation_id: str, user_id: str | None) -> str:
        ticket = secrets.token_urlsafe(32)
        with self._ticket_lock:
            now = time.time()
            self._tickets = {k: v for k, v in self._tickets.items() if v[2] > now}
            self._tickets[ticket] = (conversation_id, user_id, now + 60)
        return ticket

    def redeem_ticket(self, ticket: str, conversation_id: str) -> tuple[bool, str | None]:
        with self._ticket_lock:
            value = self._tickets.pop(ticket, None)
        if not value or value[0] != conversation_id or value[2] < time.time():
            return False, None
        return True, value[1]


browser_manager = BrowserManager()
