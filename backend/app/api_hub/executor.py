"""统一接口执行器。

网页调试、MCP、n8n 内部代理与普通 HTTP 发布都走这里，确保动态参数合并
和调用审计的行为一致。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from . import config, db, mcp_bridge, tls
from .outbound_security import OutboundTargetError, request_with_safe_redirects
from .personal_ref import PERSONAL_REF_RE

_MAX_BODY_CHARS = 1_000_000
_MAX_SNAPSHOT_BODY_CHARS = 100_000
_UNSET = object()
_REQUEST_GATE = threading.BoundedSemaphore(config.MAX_INFLIGHT_REQUESTS)
_LOG = logging.getLogger(__name__)
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


@dataclass
class RequestFile:
    """One runtime multipart file. File bytes are never persisted in interface config/history."""

    field_name: str
    filename: str
    stream: Any
    content_type: str = "application/octet-stream"
    size: int | None = None


@dataclass
class RequestOverrides:
    """一次调用允许覆盖的请求值和审计上下文。"""

    path_params: list[tuple[str, str]] | None = None
    query_params: list[tuple[str, str]] | None = None
    headers: list[tuple[str, str]] | None = None
    body: Any = _UNSET
    content_type: str | None = None
    multipart_fields: list[tuple[str, str]] | None = None
    files: list[RequestFile] | None = None
    source: str = "ui"
    proxy_key_id: int | None = None
    proxy_key_name: str | None = None
    source_ip: str | None = None
    # 当前调用者，用于解析接口配置里的 {{privacy:KEY}} 占位符。
    # 仅 JWT 路径（接口管理调试 / 已存接口调用）传入；公开代理与 n8n
    # 内部代理无用户上下文，留空即不解析占位符。
    actor: Any = None


def _parse_form(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.append((key.strip(), value.strip()))
    return out


def _kv_pairs(items) -> list[tuple[str, str]]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            key, value = item.get("key"), item.get("value", "")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            key, value = item[0], item[1]
        else:
            continue
        key = str(key or "").strip()
        if key:
            out.append((key, str(value if value is not None else "")))
    return out


def _merge_query(defaults, overrides) -> list[tuple[str, str]]:
    base = _kv_pairs(defaults)
    if overrides is None:
        return base
    incoming = _kv_pairs(overrides)
    replaced = {key for key, _ in incoming}
    return [(key, value) for key, value in base if key not in replaced] + incoming


def _merge_headers(defaults, overrides) -> dict[str, str]:
    merged: dict[str, tuple[str, str]] = {}
    for key, value in _kv_pairs(defaults):
        merged[key.lower()] = (key, value)
    if overrides is not None:
        for key, value in _kv_pairs(overrides):
            merged[key.lower()] = (key, value)
    headers = {original: value for original, value in merged.values()}
    reserved = _HOP_BY_HOP_HEADERS | {config.PROXY_KEY_HEADER.lower()}
    for key in list(headers):
        if key.lower() in reserved:
            headers.pop(key, None)
    return headers


_PATH_PARAMETER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


def _resolve_url(url: str, path_params) -> str:
    supplied = {key: value for key, value in _kv_pairs(path_params)}
    required = set(_PATH_PARAMETER_RE.findall(url))
    missing = sorted(required - supplied.keys())
    if missing:
        raise ValueError("缺少 Path 参数：" + ", ".join(missing))
    unknown = sorted(supplied.keys() - required)
    if unknown:
        raise ValueError("URL 中不存在这些 Path 参数：" + ", ".join(unknown))
    return _PATH_PARAMETER_RE.sub(
        lambda match: quote(supplied[match.group(1)], safe=""), url
    )


def _pop_header(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for key in list(headers):
        if key.lower() == target:
            return headers.pop(key)
    return None


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    _pop_header(headers, name)
    headers[name] = value


def _snapshot_body(value: Any) -> Any:
    """保留调用历史中的原始请求体，仅限制快照大小。"""
    if value is _UNSET:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        return value

    if len(text) > _MAX_SNAPSHOT_BODY_CHARS:
        text = text[:_MAX_SNAPSHOT_BODY_CHARS] + "\n…（请求体快照已截断）"
    return text


def _snapshot_response_body(value: str) -> str:
    text = value or ""
    if len(text) > _MAX_SNAPSHOT_BODY_CHARS:
        return text[:_MAX_SNAPSHOT_BODY_CHARS] + "\n…（响应体快照已截断）"
    return text


def _snapshot_headers(headers: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"key": key, "value": str(value)}
        for key, value in headers.items()
    ]


def _build_kwargs(
    iface: dict,
    overrides: RequestOverrides,
) -> tuple[dict, dict, str]:
    """构造 requests kwargs 与审计快照。

    返回 ``(kwargs, snapshot, resolved_url)``：``resolved_url`` 是真正发往
    上游的地址；``snapshot["url"]`` 在 URL 含个人变量占位符时是 ``***``，
    仅供审计展示，不能当请求地址用。
    """
    from .personal_ref import PERSONAL_REF_RE, resolve_personal_refs

    actor = overrides.actor
    orig_url = iface.get("url") or ""
    orig_headers = iface.get("headers", [])
    orig_body = iface.get("body_content", "") or ""

    # Track which values carried a personal-variable placeholder so the audit
    # snapshot can mask them as *** after resolution.  Redaction by regex would
    # miss the plaintext (the placeholder is already gone), so we record up front.
    url_has_ref = bool(PERSONAL_REF_RE.search(orig_url))
    header_has_ref: set[str] = set()  # lowercased header names
    for item in orig_headers:
        if isinstance(item, dict) and PERSONAL_REF_RE.search(item.get("value", "") or ""):
            header_has_ref.add((item.get("key") or "").lower())
    body_has_ref = bool(PERSONAL_REF_RE.search(orig_body))

    if actor is not None:
        resolved_url_raw = resolve_personal_refs(orig_url, actor)
        resolved_headers = [
            {**item, "value": resolve_personal_refs(item.get("value", ""), actor)}
            for item in orig_headers
        ]
        resolved_body = resolve_personal_refs(orig_body, actor)
    else:
        resolved_url_raw = orig_url
        resolved_headers = orig_headers
        resolved_body = orig_body

    resolved_url = _resolve_url(resolved_url_raw, overrides.path_params)
    query = _merge_query(iface.get("query_params", []), overrides.query_params)
    headers = _merge_headers(resolved_headers, overrides.headers)
    kwargs: dict[str, Any] = {
        "params": query or None,
        "timeout": config.HTTP_TIMEOUT,
    }

    body_for_snapshot: Any = _UNSET
    if overrides.multipart_fields is not None or overrides.files is not None:
        fields = _kv_pairs(overrides.multipart_fields)
        parts: list[tuple[str, tuple]] = [
            (key, (None, value)) for key, value in fields
        ]
        file_snapshot = []
        for item in overrides.files or []:
            try:
                item.stream.seek(0)
            except (AttributeError, OSError, ValueError):
                pass
            parts.append(
                (
                    item.field_name,
                    (
                        item.filename or "upload",
                        item.stream,
                        item.content_type or "application/octet-stream",
                    ),
                )
            )
            file_snapshot.append(
                {
                    "field": item.field_name,
                    "filename": item.filename or "upload",
                    "content_type": item.content_type or "application/octet-stream",
                    "size": item.size,
                }
            )
        # Let requests generate the multipart boundary. A saved/static Content-Type
        # would otherwise carry a stale boundary and corrupt the upload.
        _pop_header(headers, "Content-Type")
        if parts:
            kwargs["files"] = parts
        body_for_snapshot = {
            "fields": [{"key": key, "value": value} for key, value in fields],
            "files": file_snapshot,
        }
    elif overrides.body is not _UNSET:
        kwargs["data"] = overrides.body
        body_for_snapshot = overrides.body
        if overrides.content_type:
            _set_header(headers, "Content-Type", overrides.content_type)
    else:
        body_type = iface.get("body_type", "none")
        body = resolved_body
        if body_type == "json" and body.strip():
            kwargs["data"] = body.encode("utf-8")
            body_for_snapshot = body
            if not any(key.lower() == "content-type" for key in headers):
                headers["Content-Type"] = "application/json; charset=utf-8"
        elif body_type == "form" and body.strip():
            form = _parse_form(body)
            kwargs["data"] = form
            body_for_snapshot = body
        elif body_type == "multipart":
            form = _parse_form(body)
            _pop_header(headers, "Content-Type")
            if form:
                kwargs["files"] = [(key, (None, value)) for key, value in form]
            body_for_snapshot = {
                "fields": [{"key": key, "value": value} for key, value in form],
                "files": [],
            }
        elif body_type == "raw" and body:
            kwargs["data"] = body.encode("utf-8")
            body_for_snapshot = body

    kwargs["headers"] = headers or None

    # 审计快照不得记录个人变量（{{privacy:}}/{{env:}}）明文。原始值含占位符的
    # 字段，解析后
    # 明文已进入 kwargs 发往上游；snapshot 里直接整体置 ***，不再靠正则在明文
    # 里找占位符（占位符解析后已消失，正则必然漏）。
    _MASKED = "***"
    snapshot_url = _MASKED if url_has_ref else resolved_url
    snapshot_headers = {
        key: (_MASKED if key.lower() in header_has_ref else value)
        for key, value in headers.items()
    }
    snapshot_body = body_for_snapshot
    if body_has_ref:
        if isinstance(snapshot_body, str):
            snapshot_body = _MASKED
        elif isinstance(snapshot_body, dict) and "fields" in snapshot_body:
            # multipart form 字段——无法按字段名判断是否含占位符，
            # 整体脱敏更安全（form 字段值短，丢失可读性可接受）。
            snapshot_body = {
                **snapshot_body,
                "fields": [
                    {**f, "value": _MASKED}
                    for f in snapshot_body["fields"]
                ],
            }
    snapshot = {
        "method": iface.get("method"),
        "url": snapshot_url,
        "query_params": [
            {"key": key, "value": str(value)}
            for key, value in query
        ],
        "headers": _snapshot_headers(snapshot_headers),
        "body_type": iface.get("body_type"),
        "body_content": _snapshot_body(snapshot_body),
        "source": overrides.source,
        "proxy_key_name": overrides.proxy_key_name,
        "source_ip": overrides.source_ip,
    }
    return kwargs, snapshot, resolved_url


def _safe_text(resp: requests.Response) -> str:
    content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].lower()
    textual = (
        content_type.startswith("text/")
        or content_type.endswith("+json")
        or content_type.endswith("+xml")
        or content_type in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-www-form-urlencoded",
            "application/graphql",
        }
    )
    if content_type and not textual:
        return f"（二进制响应：{len(resp.content)} bytes，Content-Type: {content_type}）"
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        text = resp.content.decode("utf-8", errors="replace")
    if len(text) > _MAX_BODY_CHARS:
        text = text[:_MAX_BODY_CHARS] + "\n\n…（响应过大，已截断显示）"
    return text


def _blank_result() -> dict:
    return {
        "ok": False,
        "status_code": None,
        "elapsed_ms": None,
        "response_headers": {},
        "response_body": "",
        "response_content": None,
        "content_type": "",
        "error": None,
        "error_type": None,
        "relogin": False,
    }


def run_interface(
    iface: dict,
    overrides: RequestOverrides | None = None,
    *,
    include_response_content: bool = False,
    query_override: dict | None = None,
    body_override: str | None = None,
) -> dict:
    """执行接口；旧的 query_override/body_override 参数继续兼容 n8n 调用。"""
    method = (iface.get("method") or "GET").upper()
    url = (iface.get("url") or "").strip()
    if overrides is None:
        overrides = RequestOverrides(
            query_params=list((query_override or {}).items()) if query_override else None,
            body=body_override if body_override is not None else _UNSET,
            source="n8n_proxy" if query_override is not None or body_override is not None else "ui",
        )
    result = _blank_result()

    if not url:
        result["error"] = "URL 不能为空"
        result["error_type"] = "configuration"
        _save_run(iface, result, overrides, None)
        return result

    # 无用户身份的调用链路（内部代理/数据管家等）不会解析个人变量占位符；
    # URL 命中占位符时直接按配置错误终止，恢复占位符不出站的 fail-closed
    # 语义（公开代理在发布时已拦截，此处兜住其余无 actor 入口）。
    if overrides.actor is None and PERSONAL_REF_RE.search(url):
        result["error"] = (
            "接口 URL 含个人变量占位符（{{privacy:}}/{{env:}}），"
            "该调用链路无用户身份、不会解析占位符"
        )
        result["error_type"] = "configuration"
        _save_run(iface, result, overrides, None)
        return result

    if not _REQUEST_GATE.acquire(timeout=config.REQUEST_QUEUE_TIMEOUT):
        result["elapsed_ms"] = 0
        result["error"] = "接口调用繁忙，请稍后重试"
        result["error_type"] = "overloaded"
        _save_run(iface, result, overrides, None)
        return result

    try:
        # mcp-bridge:// 接口不走出站 HTTP：进程内分发为服务端 MCP 调用，
        # 与直连接口共享并发闸门和调用审计。
        if mcp_bridge.is_bridge_url(url):
            result, snapshot = mcp_bridge.run_bridge_interface(
                iface, overrides, include_response_content=include_response_content,
            )
            _save_run(iface, result, overrides, snapshot)
            return result

        session = requests.Session()
        # On Windows, use the OS-managed CA stores so corporate roots pushed
        # via Group Policy (e.g. Huawei internal CAs) are trusted for outbound
        # calls to internal HTTPS endpoints. certifi alone lacks these roots.
        tls.configure_session(session, use_system_trust=True)

        try:
            kwargs, snapshot, request_url = _build_kwargs(iface, overrides)
        except ValueError as exc:
            result["error"] = str(exc)
            result["error_type"] = "configuration"
            _save_run(iface, result, overrides, None)
            return result

        trusted_hosts = config.OUTBOUND_TRUSTED_HOSTS
        start = time.perf_counter()
        try:
            resp = request_with_safe_redirects(
                session, method, request_url, trusted_hosts=trusted_hosts, **kwargs
            )
            result["status_code"] = resp.status_code
            result["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
            result["response_headers"] = dict(resp.headers)
            result["content_type"] = resp.headers.get("Content-Type", "")
            result["response_body"] = _safe_text(resp)
            if include_response_content:
                result["response_content"] = resp.content
            if not 200 <= resp.status_code < 300:
                result["error"] = result["error"] or f"上游返回 HTTP {resp.status_code}"
                result["error_type"] = result["error_type"] or "upstream_http"
            result["ok"] = result["error"] is None
        except requests.Timeout as exc:
            result["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
            result["error"] = f"请求超时：{exc}"
            result["error_type"] = "timeout"
        except OutboundTargetError as exc:
            result["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
            result["error"] = f"接口 URL 无效：{exc}"
            result["error_type"] = "configuration"
        except requests.RequestException as exc:
            result["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
            result["error"] = f"请求失败：{exc}"
            result["error_type"] = "network"

        _save_run(iface, result, overrides, snapshot)
        return result
    finally:
        _REQUEST_GATE.release()


def _save_run(
    iface: dict,
    result: dict,
    overrides: RequestOverrides,
    snapshot: dict | None,
) -> None:
    interface_id = iface.get("id")
    if not interface_id:
        return
    if snapshot is None:
        from .personal_ref import redact_personal_refs

        raw_headers_fallback = {
            item.get("key", ""): redact_personal_refs(item.get("value", ""))
            for item in iface.get("headers", [])
            if item.get("key")
        }
        snapshot = {
            "method": iface.get("method"),
            "url": redact_personal_refs(iface.get("url") or ""),
            "query_params": [
                {
                    "key": item.get("key", ""),
                    "value": str(item.get("value", "")),
                }
                for item in iface.get("query_params", [])
            ],
            "headers": _snapshot_headers(raw_headers_fallback),
            "body_type": iface.get("body_type"),
            "body_content": _snapshot_body(
                redact_personal_refs(iface.get("body_content", ""))
            ),
            "source": overrides.source,
            "proxy_key_name": overrides.proxy_key_name,
            "source_ip": overrides.source_ip,
        }
    now = datetime.now(timezone.utc).isoformat()
    try:
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs(interface_id, ok, status_code, elapsed_ms, request_snapshot, "
                "response_headers, response_body, error, relogin, source, proxy_key_id, "
                "proxy_key_name, source_ip, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    interface_id,
                    1 if result["ok"] else 0,
                    result["status_code"],
                    result["elapsed_ms"],
                    json.dumps(snapshot, ensure_ascii=False),
                    json.dumps(
                        result["response_headers"],
                        ensure_ascii=False,
                    ),
                    _snapshot_response_body(result["response_body"]),
                    result["error"],
                    1 if result["relogin"] else 0,
                    overrides.source,
                    overrides.proxy_key_id,
                    overrides.proxy_key_name,
                    overrides.source_ip,
                    now,
                ),
            )
            result["run_id"] = cur.lastrowid
            if overrides.proxy_key_id:
                conn.execute(
                    "UPDATE proxy_keys SET last_used_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, overrides.proxy_key_id),
                )
            conn.execute(
                "DELETE FROM runs WHERE interface_id = ? AND id NOT IN "
                "(SELECT id FROM runs WHERE interface_id = ? ORDER BY id DESC LIMIT ?)",
                (interface_id, interface_id, config.MAX_RUNS_PER_INTERFACE),
            )
    except sqlite3.OperationalError:
        # SQLite is a temporary single-worker bridge.  After the configured
        # busy timeout, an audit lock must not turn an already completed
        # upstream request into a user-visible 5xx.  Preserve the request
        # result and leave a warning for operations until PostgreSQL is used.
        result.pop("run_id", None)
        _LOG.warning("API-Hub audit write skipped because SQLite stayed busy")
        return
