#!/usr/bin/env python3
"""Ontology-assistant recap acceptance against an isolated real stack.

Covers the 2026-11 recap findings end-to-end through the real HTTP front door:

* 47 remotely mapped properties (``describe_objects`` queryable flags and the
  bounded default projection ``MAX_SELECT_ITEMS=32`` + ``omittedFields``);
* day/week/month/quarter/year time-bucket aggregation with an explicit
  ``order_by=group asc`` (MySQL ONLY_FULL_GROUP_BY outer-sort regression);
* Top-10 grouped aggregation (``limit=10`` pushed to the source, exact
  ``totalGroups``, ``truncated``/``matched`` honesty, and a model-visible
  preview that keeps 10 rows instead of 5);
* zero-result aggregation with ``dataCoverage``;
* windowed distinct-user analysis for a whole absolute window;
* the retro question "same module, every 30-minute window, >=5 distinct users"
  as a formal success case through the governed ``window_distinct`` mode,
  checked against the local oracle (qualifying windows/modules).  A separate
  guard proves that other window granularities are rejected explicitly; a
  ``--window30-contract`` JSON can override the expression for contract
  revisions.

Every run uses ``POST /agent/chat/runs`` so PostgreSQL, the NATS durable run
ledger, the executor, the query engine and the remote source are real.  Only
the upstream LLM HTTP boundary is a deterministic loopback stub.

Safety / ownership
------------------
* Requires ``OPENONTOLOGY_RECAP_E2E=1`` and ``ENVIRONMENT=test``.
* API and model URLs must be explicit loopback origins; no production writes.
* Owns an isolated MySQL 8.4 container on 127.0.0.1:33317 and a pinned DBX
  metadata container on 127.0.0.1:18039 (both labelled and recorded here).
  Existing containers with the same names but different ownership labels are
  never removed.  Other MySQL/DBX containers (for example port 33316) are
  never touched.
* The fixture table lives in the container-owned ``recap_fixture`` database.
  The synthetic ontology, release and conversations stay in the isolated
  backend database for real-UI inspection; the report prints the exact
  teardown commands.
* The JSON report contains fixed fields, counts, booleans and durations only:
  no credentials, connection strings, API keys or row values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
ARTIFACT_DIR = REPO_ROOT / ".artifacts" / "ontology-assistant-recap"
DEFAULT_REPORT = ARTIFACT_DIR / "report.json"
DEFAULT_BASE_URL = "http://127.0.0.1:18036"
DEFAULT_MODEL_URL = "http://127.0.0.1:18038/v1"

SENTINEL = "OPENONTOLOGY_RECAP_E2E"
MODEL_NAME = "fixture-recap-model"
FIXTURE_API_KEY = "sk-fixture-recap-e2e"
FIXTURE_ADMIN_PASSWORD_ENV = "OPENONTOLOGY_E2E_ADMIN_PASSWORD"

MYSQL_PORT = 33317
MYSQL_DATABASE = "recap_fixture"
MYSQL_USER = "recap_e2e"
MYSQL_PASSWORD = "recap_e2e_fixture"
MYSQL_ROOT_PASSWORD = "recap_root_fixture"
MYSQL_CONTAINER = "oo-recap-mysql-33317"
DBX_PORT = 18039
DBX_PASSWORD = "recap-dbx-fixture"
DBX_CONTAINER = "oo-recap-dbx-18039"
NETWORK = "oo-recap-fixture-net"
DBX_IMAGE = (
    "docker.cnb.cool/dbxio.com/dbx@"
    "sha256:b661f7e35deed06d9611dcda79c44ffcc8e4fd0f44308028ea77f7f18b36638c"
)
FIXTURE_LABEL = "oo.recap.fixture=ontology-assistant-recap"
ROLE_LABEL = "oo.recap.role"
NETWORK_ROLE = "network"
NETWORK_RECORD = ARTIFACT_DIR / "fixture-network.json"

TARGET_TIMEZONE = "Asia/Shanghai"
TARGET_TZ = ZoneInfo(TARGET_TIMEZONE)
SEPTEMBER_MODULES = ("m1", "m2", "m3")
OCT_LOCAL_START = "2026-10-01T00:00:00"
OCT_LOCAL_END = "2026-11-01T00:00:00"
SEPT_LOCAL_START = "2026-09-01T00:00:00"
SEPT_LOCAL_END = "2026-10-01T00:00:00"

MAX_SELECT_ITEMS = 32
PROPERTY_COUNT = 47
BUCKETS = ("day", "week", "month", "quarter", "year")
TERMINAL_RUN_STATUSES = {"succeeded", "error", "cancelled", "interrupted"}

_REDACT_VALUES: set[str] = set()
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{6,}\b")


def _register_secret(value: str | None) -> None:
    if value:
        _REDACT_VALUES.add(str(value))


def redact(text: Any) -> str:
    value = str(text)
    for secret in sorted(_REDACT_VALUES, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "***")
    value = _BEARER_RE.sub(r"\1***", value)
    value = _SK_RE.sub("sk-***", value)
    return value


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def validate_loopback_url(value: str, *, allow_v1_path: bool) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL port is invalid") from exc
    if parsed.scheme != "http":
        raise ValueError("fixture URL must use http on loopback")
    if parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("fixture URL host must be an explicit loopback address")
    if parsed.username or parsed.password:
        raise ValueError("fixture URL must not embed userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("fixture URL must not carry query/fragment")
    if port is None:
        raise ValueError("fixture URL must carry an explicit port")
    path = parsed.path.rstrip("/")
    allowed_paths = {"", "/v1"} if allow_v1_path else {""}
    if path not in allowed_paths:
        raise ValueError("fixture URL path is not allowed")
    return value.rstrip("/")


def _decode_json(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"detail": text[:2000]}


def _detail_summary(detail: Any) -> str:
    if isinstance(detail, dict):
        nested = detail.get("detail")
        source = nested if isinstance(nested, dict) else detail
        parts = []
        for key in ("code", "error"):
            if source.get(key):
                parts.append(str(source[key]))
        if source.get("message"):
            parts.append(str(source["message"]))
        if parts:
            return " | ".join(parts)
    return redact(detail)[:500]


class ApiFailure(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: Any):
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail
        super().__init__(
            f"{method} {path} -> HTTP {status}: {_detail_summary(detail)}"
        )


class Api:
    def __init__(self, base_url: str, *, timeout: float = 60.0):
        self.base_url = validate_loopback_url(base_url, allow_v1_path=False)
        self.timeout = timeout
        self.token: str | None = None

    def raw(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        payload = None if body is None else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urlrequest.Request(
            self.base_url + path, data=payload, headers=headers, method=method,
        )
        try:
            with urlrequest.urlopen(
                request, timeout=timeout or self.timeout,
            ) as response:
                return response.status, _decode_json(response.read())
        except urlerror.HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            return exc.code, _decode_json(raw)

    def ok(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        expected: tuple[int, ...] = (200,),
        timeout: float | None = None,
    ) -> Any:
        status, data = self.raw(method, path, body, timeout=timeout)
        if status not in expected:
            raise ApiFailure(method, path, status, data)
        return data


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# ---------------------------------------------------------------- model stub


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _message_text(message)
    return ""


def stub_scenario(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if "结论校验失败" in text:
            # Correction round: keep the scenario from earlier user turns.
            continue
        match = re.search(r"recap\.([A-Za-z0-9_.-]+)", text)
        if match:
            return match.group(1)
    for message in reversed(messages):
        if message.get("role") == "user":
            match = re.search(r"recap\.([A-Za-z0-9_.-]+)", _message_text(message))
            if match:
                return match.group(1)
    return "unrecognized"


def _has_tool_result_after_last_user(messages: list[dict]) -> bool:
    last_user = -1
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            last_user = index
    return any(
        message.get("role") == "tool"
        for message in messages[last_user + 1:]
    )


def _last_tool_content(messages: list[dict]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") for item in content
                if isinstance(item, dict)
            )
        if isinstance(content, str) and content.strip():
            return content
    return None


def _last_tool_payload(messages: list[dict]) -> dict[str, Any] | None:
    content = _last_tool_content(messages)
    if content is None:
        return None
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _int_field(payload: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    try:
        return int(Decimal(str(value)))
    except Exception:  # noqa: BLE001 - fixture answer assembly only
        return None


def _window30_refusal() -> str:
    # No digits: the failed tool result carries no numeric evidence, so an
    # honest refusal must not assert window/group counts either.
    return (
        "当前本体助手不支持该时间窗口粒度，无法给出窗口数与模块数。"
        "现有能力只能提供整段时间窗口内的去重统计；我没有把整窗结果冒充窗口计数。"
    )


def final_answer(scenario: str, payload: dict[str, Any] | None) -> str:
    base = scenario.split(".", 1)[0]
    if not isinstance(payload, dict):
        return "没有可核验的工具结果，无法给出结论。"
    if payload.get("error"):
        if base in {"window30", "window30_guard"}:
            return _window30_refusal()
        return "工具执行失败，无法给出统计结论。"
    if base == "describe47":
        types = payload.get("objectTypes") or []
        entry = types[0] if types else {}
        count = _int_field(entry, "propertyCount")
        queryable = _int_field(entry, "queryablePropertyCount")
        return (
            f"Supplier 共 {count} 个属性，其中可查询 {queryable} 个；"
            "远程对象只允许查询有列映射的属性。"
        )
    if base == "projection47":
        projection = payload.get("projection") or {}
        cap = _int_field(projection, "cap")
        # Do not assert list lengths here: only values stored in the tool
        # result are verifiable evidence.  The acceptance script checks the
        # selected/omitted counts directly.
        return (
            f"默认投影上限 {cap}；省略字段来自 projection.omittedFields，"
            "只是本次未返回，不代表字段不存在或值为空。"
        )
    if base == "top10":
        groups = payload.get("groups") or []
        total = _int_field(payload, "totalGroups")
        limit = _int_field(payload, "groupLimit")
        return (
            f"模型实际收到 {len(groups)} 组，总组数 {total}，上限 {limit}；"
            "其余分组未返回且不可翻页。"
        )
    if base == "zero":
        value = _int_field(payload, "value")
        matched = _int_field(payload, "matched")
        coverage = payload.get("dataCoverage") or {}
        reason = str(coverage.get("reason") or "")
        return f"匹配 {value} 行，覆盖原因 {reason}，零结果说明为 {matched} 行。"
    if base == "distinct_window":
        distinct = _int_field(payload, "distinctSubjects")
        candidates = _int_field(payload, "candidateRows")
        threshold = _int_field(payload, "threshold")
        return (
            f"窗口内去重用户 {distinct} 个，候选事件 {candidates} 条，"
            f"阈值 {threshold}。"
        )
    if base == "distinct_module":
        matching = _int_field(payload, "matchingGroups")
        total = _int_field(payload, "totalGroups")
        threshold = _int_field(payload, "threshold")
        return (
            f"达标模块 {matching} 个，总模块 {total} 个，阈值 {threshold}。"
        )
    if base in {"window30", "window30_guard"}:
        contract = _WINDOW30_CONTRACT
        if base == "window30" and isinstance(contract, dict):
            expected = contract.get("expected") or {}
            parts = []
            for key, value in expected.items():
                actual = payload.get(key)
                if isinstance(actual, (int, float)) and not isinstance(actual, bool):
                    parts.append(f"{key}={int(actual)}")
            if parts:
                return "固定窗口统计：" + "；".join(parts) + "。"
        windows = _int_field(payload, "qualifyingWindows")
        modules = _int_field(payload, "qualifyingModules")
        threshold = _int_field(payload, "threshold")
        if windows is not None and modules is not None:
            return (
                f"半小时固定窗口达标 {windows} 个，涉及模块 {modules} 个，"
                f"每窗口去重主体阈值 {threshold}。"
            )
        return _window30_refusal()
    if base == "bucket":
        returned = _int_field(payload, "returned")
        total = _int_field(payload, "totalGroups")
        limit = _int_field(payload, "groupLimit")
        return (
            f"时间桶返回 {returned} 组，总组数 {total}，上限 {limit}。"
        )
    return "工具结果已返回。"


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_fixture_{uuid.uuid4().hex[:16]}",
        "name": name,
        "arguments": arguments,
    }


_WINDOW30_CONTRACT: dict[str, Any] | None = None


def _window_argument(start: str, end: str) -> dict[str, Any]:
    return {
        "property": "occurred_at",
        "timezone": TARGET_TIMEZONE,
        "start": start,
        "end": end,
    }


def stub_tool(scenario: str, messages: list[dict]) -> dict[str, Any] | None:
    base = scenario.split(".", 1)[0]
    if base == "describe47":
        return _tool_call("describe_objects", {"object_types": ["Supplier"]})
    if base == "projection47":
        return _tool_call("query_objects", {
            "object_type": "Supplier", "page_size": 3,
        })
    if base == "bucket":
        bucket = scenario.rsplit(".", 1)[-1]
        return _tool_call("aggregate_objects", {
            "object_type": "Supplier",
            "metric": "count",
            "group_by": [{"property": "occurred_at", "bucket": bucket}],
            "order_by": {"output": "group", "direction": "asc"},
        })
    if base == "top10":
        return _tool_call("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "group_by": "region", "limit": 10,
        })
    if base == "zero":
        return _tool_call("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "filters": [{
                "property": "region", "op": "eq",
                "value": "__recap_absent__",
            }],
        })
    if base == "distinct_window":
        return _tool_call("analyze_sequences", {
            "object_type": "Supplier",
            "mode": "distinct",
            "subject_property": "user_id",
            "time_property": "occurred_at",
            "threshold": 5,
            "window": _window_argument(OCT_LOCAL_START, OCT_LOCAL_END),
        })
    if base == "distinct_module":
        return _tool_call("analyze_sequences", {
            "object_type": "Supplier",
            "mode": "distinct",
            "subject_property": "user_id",
            "time_property": "occurred_at",
            "threshold": 5,
            "filters": [{
                "property": "module", "op": "in",
                "value": list(SEPTEMBER_MODULES),
            }],
            "window": _window_argument(SEPT_LOCAL_START, SEPT_LOCAL_END),
            "group_by": [{"property": "module"}],
        })
    if base == "window30":
        contract = _WINDOW30_CONTRACT
        if isinstance(contract, dict) and contract.get("tool"):
            return _tool_call(
                str(contract["tool"]),
                dict(contract.get("arguments") or {}),
            )
        # Governed fixed 30-minute window: module + half_hour inner
        # count_distinct, threshold-filtered outer module aggregation.
        return _tool_call("analyze_sequences", {
            "object_type": "Supplier",
            "mode": "window_distinct",
            "subject_property": "user_id",
            "time_property": "occurred_at",
            "threshold": 5,
            "window_minutes": 30,
            "filters": [{
                "property": "module", "op": "in",
                "value": list(SEPTEMBER_MODULES),
            }],
            "window": _window_argument(SEPT_LOCAL_START, SEPT_LOCAL_END),
            "group_by": [{"property": "module"}],
        })
    if base == "window30_guard":
        # Boundary guard: any fixed window other than 30 minutes must be
        # rejected explicitly instead of being approximated.
        return _tool_call("analyze_sequences", {
            "object_type": "Supplier",
            "mode": "window_distinct",
            "subject_property": "user_id",
            "time_property": "occurred_at",
            "threshold": 5,
            "window_minutes": 60,
            "filters": [{
                "property": "module", "op": "in",
                "value": list(SEPTEMBER_MODULES),
            }],
            "window": _window_argument(SEPT_LOCAL_START, SEPT_LOCAL_END),
            "group_by": [{"property": "module"}],
        })
    return None


def stub_plan(
    scenario: str, messages: list[dict],
) -> tuple[dict[str, Any] | None, str]:
    last_user = _last_user_text(messages)
    if "结论校验失败" in last_user:
        # Correction round: never issue a second tool call; re-answer from
        # the already persisted tool evidence.
        return None, final_answer(scenario, _last_tool_payload(messages))
    if not _has_tool_result_after_last_user(messages):
        if "Connectivity check" in last_user:
            return None, "PONG"
        return stub_tool(scenario, messages), ""
    return None, final_answer(scenario, _last_tool_payload(messages))


class StubState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: Counter[str] = Counter()
        self._tool_rounds: Counter[str] = Counter()
        self._audit: dict[str, dict[str, Any]] = {}

    def record(self, scenario: str, *, tool_round: bool) -> None:
        with self._lock:
            self._calls[scenario] += 1
            if tool_round:
                self._tool_rounds[scenario] += 1

    def record_tool_payload(self, scenario: str, content: str) -> None:
        """Audit only booleans/counts: no tool payload or token is retained."""
        with self._lock:
            entry = self._audit.setdefault(scenario, {
                "toolPayloadChars": 0,
                "sampleWindowsSeen": False,
                "drilldownKeySeen": False,
                "tokenFieldSeen": False,
                "moduleQuerySpecSeen": False,
                "sampleWindowDrilldownTokens": False,
            })
            entry["toolPayloadChars"] = max(
                int(entry["toolPayloadChars"]), len(content))
            entry["sampleWindowsSeen"] = (
                entry["sampleWindowsSeen"] or '"sampleWindows"' in content)
            entry["drilldownKeySeen"] = (
                entry["drilldownKeySeen"] or '"drilldown"' in content)
            entry["tokenFieldSeen"] = (
                entry["tokenFieldSeen"] or '"token"' in content)
            entry["moduleQuerySpecSeen"] = (
                entry["moduleQuerySpecSeen"] or '"moduleQuerySpec"' in content)
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = None
            samples = (
                payload.get("sampleWindows")
                if isinstance(payload, dict) else None
            )
            if isinstance(samples, list):
                entry["sampleWindowDrilldownTokens"] = (
                    entry["sampleWindowDrilldownTokens"]
                    or any(
                        isinstance(item, dict)
                        and ("drilldown" in item or "token" in item)
                        for item in samples
                    )
                )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": dict(self._calls),
                "toolRounds": dict(self._tool_rounds),
                "audit": {key: dict(value)
                          for key, value in self._audit.items()},
            }


def _json_response(handler: BaseHTTPRequestHandler, status: int,
                   payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _sse_chunk(handler: BaseHTTPRequestHandler, model: str,
               delta: dict[str, Any], finish_reason: str | None = None,
               usage: dict[str, int] | None = None) -> None:
    payload: dict[str, Any] = {
        "id": "chatcmpl-recap-fixture",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": (
            [] if usage is not None else [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }]
        ),
    }
    if usage is not None:
        payload["usage"] = usage
    handler.wfile.write(
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
    handler.wfile.flush()


def _completion_message(plan_tool: dict[str, Any] | None, content: str,
                        ) -> tuple[str, list[dict] | None]:
    tool_calls = None
    if plan_tool:
        tool_calls = [{
            "id": plan_tool["id"],
            "type": "function",
            "function": {
                "name": plan_tool["name"],
                "arguments": json.dumps(
                    plan_tool["arguments"], ensure_ascii=False,
                    separators=(",", ":")),
            },
        }]
    return content, tool_calls


def _stream_completion(handler: BaseHTTPRequestHandler, model: str,
                       tool: dict[str, Any] | None, content: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    if content:
        _sse_chunk(handler, model, {"role": "assistant", "content": content})
    if tool:
        _sse_chunk(handler, model, {
            "role": "assistant",
            "tool_calls": [{
                "index": 0,
                "id": tool["id"],
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "arguments": json.dumps(
                        tool["arguments"], ensure_ascii=False,
                        separators=(",", ":")),
                },
            }],
        })
    _sse_chunk(handler, model, {},
               finish_reason="tool_calls" if tool else "stop")
    _sse_chunk(handler, model, {}, usage={
        "prompt_tokens": 12, "completion_tokens": 4,
    })
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def _non_stream_completion(handler: BaseHTTPRequestHandler, model: str,
                           tool: dict[str, Any] | None,
                           content: str) -> None:
    message_content, tool_calls = _completion_message(tool, content)
    message: dict[str, Any] = {"role": "assistant", "content": message_content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    _json_response(handler, 200, {
        "id": "chatcmpl-recap-fixture",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
        },
    })


def make_stub_handler(state: StubState):
    class ModelStubHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, _format: str, *args: Any) -> None:  # noqa: A003
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return body if isinstance(body, dict) else {}

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            path = urlsplit(self.path).path
            if path == "/__fixture/health":
                _json_response(self, 200, {
                    "ok": True, "mode": "recap-model-stub",
                })
                return
            if path == "/__fixture/stats":
                _json_response(self, 200, state.snapshot())
                return
            if path in {"/v1/models", "/models"}:
                _json_response(self, 200, {
                    "object": "list",
                    "data": [{"id": MODEL_NAME, "object": "model"}],
                })
                return
            _json_response(self, 404, {"error": {"message": "fixture_not_found"}})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
            path = urlsplit(self.path).path
            if path not in {"/v1/chat/completions", "/chat/completions"}:
                _json_response(self, 404, {
                    "error": {"message": "fixture_not_found"}})
                return
            body = self._read_json()
            messages = body.get("messages")
            if not isinstance(messages, list):
                messages = []
            scenario = stub_scenario(messages)
            tool_round = _has_tool_result_after_last_user(messages)
            state.record(scenario, tool_round=tool_round)
            tool_content = _last_tool_content(messages)
            if tool_content is not None:
                state.record_tool_payload(scenario, tool_content)
            tool, content = stub_plan(scenario, messages)
            model = str(body.get("model") or MODEL_NAME)
            try:
                if bool(body.get("stream")):
                    _stream_completion(self, model, tool, content)
                else:
                    _non_stream_completion(self, model, tool, content)
            except (BrokenPipeError, ConnectionResetError):
                return

    return ModelStubHandler


def start_model_stub(host: str, port: int) -> tuple[ThreadingHTTPServer,
                                                    StubState]:
    state = StubState()
    server = ThreadingHTTPServer((host, port), make_stub_handler(state))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    return server, state


def _stub_health(origin: str) -> dict[str, Any] | None:
    try:
        with urlrequest.urlopen(
            origin + "/__fixture/health", timeout=2.0,
        ) as response:
            payload = _decode_json(response.read())
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 - readiness probe only
        return None


def _health_ok(origin: str) -> bool:
    payload = _stub_health(origin)
    return isinstance(payload, dict) and payload.get("ok") is True


def _stub_stats(origin: str) -> dict[str, Any]:
    try:
        with urlrequest.urlopen(
            origin + "/__fixture/stats", timeout=3.0,
        ) as response:
            payload = _decode_json(response.read())
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001 - evidence only
        return {}


# ------------------------------------------------------------- fixture data

REGION_ORDER = [f"r{index:02d}" for index in range(1, 13)]
REGION_COUNTS = [8, 7, 6, 5, 4, 3, 2, 1, 1, 1, 1, 1]
GENERIC_DATES = [
    "2025-06-01T00:00:00", "2025-11-11T11:00:00", "2025-12-29T00:30:00",
    "2026-01-03T10:00:00", "2026-01-06T10:00:00", "2026-01-13T10:00:00",
    "2026-02-20T10:00:00", "2026-03-08T10:00:00", "2026-03-09T10:00:00",
    "2026-06-15T10:00:00", "2026-07-01T10:00:00", "2026-08-15T10:00:00",
    "2026-11-05T08:00:00", "2026-12-31T23:00:00",
]
# September: m1 has 7 distinct users, m2 has 3, m3 has 5.
SEPTEMBER_ROWS = [
    ("m1", "u30", "2026-09-10T09:00:00"),
    ("m1", "u31", "2026-09-10T09:05:00"),
    ("m1", "u32", "2026-09-10T09:10:00"),
    ("m1", "u33", "2026-09-10T09:15:00"),
    ("m1", "u34", "2026-09-10T09:20:00"),
    ("m1", "u35", "2026-09-10T09:25:00"),
    ("m1", "u36", "2026-09-10T09:45:00"),
    ("m1", "u30", "2026-09-11T09:05:00"),
    ("m2", "u40", "2026-09-10T09:05:00"),
    ("m2", "u41", "2026-09-10T09:20:00"),
    ("m2", "u42", "2026-09-20T03:00:00"),
    ("m3", "u50", "2026-09-15T01:00:00"),
    ("m3", "u51", "2026-09-15T01:05:00"),
    ("m3", "u52", "2026-09-15T01:10:00"),
    ("m3", "u53", "2026-09-15T01:20:00"),
    ("m3", "u54", "2026-09-15T01:25:00"),
]
OCTOBER_ROWS = [
    ("m1", "u20", "2026-09-30T16:00:00"),
    ("m2", "u21", "2026-10-01T02:00:00"),
    ("m1", "u20", "2026-10-15T12:00:00"),
    ("m2", "u22", "2026-10-31T15:00:00"),
    ("m3", "u23", "2026-10-31T15:59:59"),
    # Boundary probes stay outside the September module oracle (m0) and the
    # October window (before start / at exclusive end).
    ("m0", "u90", "2026-09-30T15:59:59"),
    ("m2", "u91", "2026-10-31T16:00:00"),
]
FIXTURE_COLUMNS = (
    [("supplier_id", "VARCHAR(32) NOT NULL"),
     # TIMESTAMP (not DATETIME) so the reflected column is UTC-aware and the
     # governed absolute [start,end) window values (tz-aware) are accepted.
     ("occurred_at", "TIMESTAMP NULL"),
     ("region", "VARCHAR(32) NULL"),
     ("module", "VARCHAR(32) NULL"),
     ("user_id", "VARCHAR(32) NULL")]
    + [(f"field_{index:02d}", "VARCHAR(64) NULL") for index in range(1, 43)]
)
ONTOLOGY_PROPERTIES = (
    [("supplier_id", "供应商编号", "string", True),
     ("occurred_at", "发生时间", "datetime", False),
     ("region", "区域", "string", False),
     ("module", "模块", "string", False),
     ("user_id", "用户", "string", False)]
    + [(f"field_{index:02d}", f"字段{index:02d}", "string", False)
       for index in range(1, 43)]
)


@dataclass(frozen=True)
class FixtureRow:
    supplier_id: str
    occurred_at: datetime
    region: str
    module: str
    user_id: str
    fields: dict[str, str]


def fixture_rows() -> list[FixtureRow]:
    rows: list[FixtureRow] = []

    def add(region: str, module: str, user_id: str,
            when: str) -> None:
        index = len(rows) + 1
        fields = {
            f"field_{field_index:02d}":
                f"{region}-{module}-{field_index:02d}"
            for field_index in range(1, 43)
        }
        rows.append(FixtureRow(
            supplier_id=f"SUP-{index:05d}",
            occurred_at=datetime.fromisoformat(when),
            region=region, module=module, user_id=user_id, fields=fields,
        ))

    for region_index, (region, count) in enumerate(
            zip(REGION_ORDER, REGION_COUNTS)):
        for offset in range(count):
            when = GENERIC_DATES[
                (region_index * 3 + offset) % len(GENERIC_DATES)]
            add(region, f"m{((region_index + offset) % 3) + 1}",
                f"u{(region_index * 2 + offset) % 19 + 1:02d}", when)
    for index, (module, user_id, when) in enumerate(SEPTEMBER_ROWS):
        add(REGION_ORDER[index % len(REGION_ORDER)], module, user_id, when)
    for index, (module, user_id, when) in enumerate(OCTOBER_ROWS):
        add(REGION_ORDER[(index + 4) % len(REGION_ORDER)],
            module, user_id, when)
    return rows


def _bucket_start(value: datetime, bucket: str) -> date:
    day = value.date()
    if bucket == "day":
        return day
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    if bucket == "quarter":
        return date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)
    if bucket == "year":
        return date(day.year, 1, 1)
    raise ValueError(f"unsupported bucket {bucket}")


def _local_half_hour(value: datetime) -> datetime:
    local = value.replace(tzinfo=timezone.utc).astimezone(TARGET_TZ)
    minute = 0 if local.minute < 30 else 30
    return local.replace(minute=minute, second=0, microsecond=0)


@dataclass
class Oracle:
    rows: list[FixtureRow]
    region_counts: Counter[str]
    bucket_counts: dict[str, Counter[str]]
    october_distinct: int
    october_candidates: int
    september_module_distinct: dict[str, int]
    window30_by_module: dict[str, int]
    window30_total: int
    window30_modules: int
    top10: list[tuple[str, int]]
    bundle_sha256: str = field(default="")

    def report(self) -> dict[str, Any]:
        return {
            "rows": len(self.rows),
            "regions": len(self.region_counts),
            "top10": [
                {"region": region, "count": count}
                for region, count in self.top10
            ],
            "bucketGroups": {
                bucket: len(counts)
                for bucket, counts in self.bucket_counts.items()
            },
            "octoberWindow": {
                "distinctUsers": self.october_distinct,
                "candidateRows": self.october_candidates,
            },
            "septemberModuleDistinct": self.september_module_distinct,
            "window30": {
                "timezone": TARGET_TIMEZONE,
                "alignment": (
                    "half-hour tumbling windows on UTC/source timestamps; "
                    "Asia/Shanghai is UTC+08:00, a multiple of 30 minutes, "
                    "so local wall-clock half-hours coincide"
                ),
                "qualifyingWindows": self.window30_total,
                "qualifyingModules": self.window30_modules,
                "byModule": self.window30_by_module,
            },
            "fixtureSha256": self.bundle_sha256,
        }


def build_oracle(rows: list[FixtureRow]) -> Oracle:
    region_counts = Counter(row.region for row in rows)
    bucket_counts: dict[str, Counter[str]] = {}
    for bucket in BUCKETS:
        bucket_counts[bucket] = Counter(
            _bucket_start(row.occurred_at, bucket).isoformat()
            for row in rows
        )
    october_start = datetime.fromisoformat("2026-09-30T16:00:00")
    october_end = datetime.fromisoformat("2026-10-31T16:00:00")
    october = [
        row for row in rows
        if october_start <= row.occurred_at < october_end
    ]
    sept_start = datetime.fromisoformat("2026-08-31T16:00:00")
    sept_end = datetime.fromisoformat("2026-09-30T16:00:00")
    september = [
        row for row in rows
        if sept_start <= row.occurred_at < sept_end
        and row.module in SEPTEMBER_MODULES
    ]
    module_users: dict[str, set[str]] = {}
    for row in september:
        module_users.setdefault(row.module, set()).add(row.user_id)
    window30: Counter[str] = Counter()
    window30_users: dict[tuple[str, datetime], set[str]] = {}
    for row in september:
        key = (row.module, _local_half_hour(row.occurred_at))
        window30_users.setdefault(key, set()).add(row.user_id)
    for (module, _start), users in window30_users.items():
        if len(users) >= 5:
            window30[module] += 1
    top10 = sorted(
        region_counts.items(), key=lambda item: (-item[1], item[0]),
    )[:10]
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps({
            "id": row.supplier_id,
            "at": row.occurred_at.isoformat(),
            "region": row.region,
            "module": row.module,
            "user": row.user_id,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return Oracle(
        rows=rows,
        region_counts=region_counts,
        bucket_counts=bucket_counts,
        october_distinct=len({row.user_id for row in october}),
        october_candidates=len(october),
        september_module_distinct={
            module: len(users) for module, users in sorted(module_users.items())
        },
        window30_by_module={
            module: int(count) for module, count in sorted(window30.items())
        },
        window30_total=int(sum(window30.values())),
        window30_modules=len(window30),
        top10=top10,
        bundle_sha256=digest.hexdigest(),
    )


# --------------------------------------------------------------- infra


class InfraError(RuntimeError):
    pass


def _run(command: list[str], *, timeout: float = 120.0,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise InfraError(
            f"{command[0]} failed ({result.returncode}): "
            f"{redact(result.stderr.strip())[:400]}"
        )
    return result


def _container_labels(name: str) -> dict[str, str]:
    result = _run(["docker", "inspect", name], check=False)
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
        return payload[0]["Config"]["Labels"] or {}
    except (KeyError, IndexError, json.JSONDecodeError):
        return {}


def _container_running(name: str) -> bool:
    result = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _network_labels(name: str) -> dict[str, str] | None:
    result = _run(["docker", "network", "inspect", name], check=False)
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        return payload[0].get("Labels") or {}
    except (IndexError, KeyError, json.JSONDecodeError):
        return {}


def _owned_container_on_network(name: str, role: str, network: str) -> bool:
    labels = _container_labels(name)
    if labels.get("oo.recap.fixture") != "ontology-assistant-recap" \
            or labels.get("oo.recap.role") != role:
        return False
    result = _run(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", name],
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        networks = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False
    return isinstance(networks, dict) and network in networks


def _read_network_record() -> dict[str, Any]:
    try:
        payload = json.loads(NETWORK_RECORD.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_network_record(state: str) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "network": NETWORK,
        "owner": "ontology-assistant-recap",
        "state": state,
        "createdAt": iso_now(),
        "labels": {
            "oo.recap.fixture": "ontology-assistant-recap",
            "oo.recap.role": NETWORK_ROLE,
        },
    }
    NETWORK_RECORD.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(NETWORK_RECORD, 0o600)
    except OSError:
        pass


def _ensure_fixture_network() -> str:
    """Reuse only a labelled/recorded recap network; never relabel or delete
    a pre-existing unknown network."""
    labels = _network_labels(NETWORK)
    if labels is None:
        _run([
            "docker", "network", "create",
            "--label", FIXTURE_LABEL,
            "--label", f"{ROLE_LABEL}={NETWORK_ROLE}",
            NETWORK,
        ])
        _write_network_record("created")
        return "created"
    if labels.get("oo.recap.fixture") == "ontology-assistant-recap" \
            and labels.get("oo.recap.role") == NETWORK_ROLE:
        _write_network_record("owned_label")
        return "owned_label"
    record = _read_network_record()
    if record.get("network") == NETWORK and \
            record.get("owner") == "ontology-assistant-recap":
        return "owned_runtime_record"
    if _owned_container_on_network(MYSQL_CONTAINER, "mysql", NETWORK) and \
            _owned_container_on_network(DBX_CONTAINER, "dbx", NETWORK):
        # Legacy network created before network labels existed, but both
        # owned fixture containers prove runtime ownership. Do not relabel.
        _write_network_record("adopted_existing_containers")
        return "adopted_existing_containers"
    raise InfraError(
        f"network {NETWORK} exists without recap ownership labels or record; "
        "refusing to reuse or remove it")


def _ensure_container(name: str, role: str, run: list[str]) -> str:
    labels = _container_labels(name)
    if labels:
        if labels.get("oo.recap.fixture") != "ontology-assistant-recap" \
                or labels.get("oo.recap.role") != role:
            raise InfraError(
                f"container {name} exists but is not owned by this recap "
                "fixture; refusing to reuse or remove it")
        if not _container_running(name):
            _run(["docker", "start", name], timeout=120.0)
            return "restarted"
        return "reused"
    _run(run, timeout=180.0)
    return "created"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _wait_port(port: int, *, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port):
            return
        time.sleep(0.4)
    raise InfraError(f"fixture port {port} did not become ready")


def _wait_mysql(*, timeout: float = 180.0) -> None:
    import pymysql

    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            connection = pymysql.connect(
                host="127.0.0.1", port=MYSQL_PORT, user=MYSQL_USER,
                password=MYSQL_PASSWORD, database=MYSQL_DATABASE,
                charset="utf8mb4", autocommit=True, connect_timeout=3,
            )
        except Exception as exc:  # noqa: BLE001 - readiness loop only
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
            continue
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            return
        finally:
            connection.close()
    raise InfraError(
        f"fixture MySQL did not accept SELECT 1: {redact(last_error)[:300]}")


def _dbx_ready() -> bool:
    try:
        with urlrequest.urlopen(
            f"http://127.0.0.1:{DBX_PORT}/api/auth/check", timeout=2.0,
        ) as response:
            payload = _decode_json(response.read())
        return isinstance(payload, dict) and (
            "required" in payload or "setup_required" in payload
        )
    except Exception:  # noqa: BLE001 - readiness probe only
        return False


class Infra:
    def __init__(self) -> None:
        self.mysql_started = "reused"
        self.dbx_started = "reused"
        self.network_state = "reused"
        self.network_owned = False
        self.table_name = ""

    def ensure(self) -> None:
        if _run(["docker", "version"], check=False).returncode != 0:
            raise InfraError("docker is not available; fixture infra cannot start")
        self.network_state = _ensure_fixture_network()
        self.network_owned = True
        self.mysql_started = _ensure_container(
            MYSQL_CONTAINER, "mysql", [
                "docker", "run", "-d", "--name", MYSQL_CONTAINER,
                "--network", NETWORK,
                "--label", FIXTURE_LABEL, "--label", f"{ROLE_LABEL}=mysql",
                "-p", f"127.0.0.1:{MYSQL_PORT}:3306",
                "-e", f"MYSQL_ROOT_PASSWORD={MYSQL_ROOT_PASSWORD}",
                "-e", f"MYSQL_DATABASE={MYSQL_DATABASE}",
                "-e", f"MYSQL_USER={MYSQL_USER}",
                "-e", f"MYSQL_PASSWORD={MYSQL_PASSWORD}",
                "mysql:8.4",
                "--character-set-server=utf8mb4",
                "--collation-server=utf8mb4_0900_ai_ci",
            ],
        )
        self.dbx_started = _ensure_container(
            DBX_CONTAINER, "dbx", [
                "docker", "run", "-d", "--name", DBX_CONTAINER,
                "--network", NETWORK,
                "--label", FIXTURE_LABEL, "--label", f"{ROLE_LABEL}=dbx",
                "-p", f"127.0.0.1:{DBX_PORT}:4224",
                "-e", f"DBX_PASSWORD={DBX_PASSWORD}",
                "-e", "DBX_BIND_ADDR=0.0.0.0",
                "-e", "DBX_DATA_DIR=/app/data",
                "--mount", "type=tmpfs,destination=/app/data",
                DBX_IMAGE,
            ],
        )
        _wait_port(MYSQL_PORT)
        _wait_mysql()
        _wait_port(DBX_PORT)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not _dbx_ready():
            time.sleep(0.5)
        if not _dbx_ready():
            raise InfraError("pinned DBX fixture did not answer /api/auth/check")

    def create_table(self, nonce: str) -> str:
        import pymysql

        table_name = f"recap_metrics_{nonce}"
        columns = ",\n  ".join(
            f"`{name}` {definition}" for name, definition in FIXTURE_COLUMNS
        )
        connection = pymysql.connect(
            host="127.0.0.1", port=MYSQL_PORT, user=MYSQL_USER,
            password=MYSQL_PASSWORD, database=MYSQL_DATABASE,
            charset="utf8mb4", autocommit=True,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"CREATE TABLE `{table_name}` (\n  {columns},\n"
                    "  PRIMARY KEY (`supplier_id`)\n"
                    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                )
                rows = fixture_rows()
                names = ["supplier_id", "occurred_at", "region", "module",
                        "user_id"] + [f"field_{index:02d}"
                                      for index in range(1, 43)]
                placeholders = ", ".join(["%s"] * len(names))
                values = []
                for row in rows:
                    values.append([
                        row.supplier_id, row.occurred_at, row.region,
                        row.module, row.user_id,
                        *[row.fields[f"field_{index:02d}"]
                          for index in range(1, 43)],
                    ])
                cursor.executemany(
                    f"INSERT INTO `{table_name}` "
                    f"(`{'`, `'.join(names)}`) VALUES ({placeholders})",
                    values,
                )
                cursor.execute(f"SELECT COUNT(*) FROM `{table_name}`")
                count = int(cursor.fetchone()[0])
                if count != len(rows):
                    raise InfraError(
                        f"MySQL fixture row count {count} != {len(rows)}")
        finally:
            connection.close()
        self.table_name = table_name
        return table_name

    def teardown_commands(self) -> list[str]:
        commands = [
            f"docker rm -f {DBX_CONTAINER}",
            f"docker rm -f {MYSQL_CONTAINER}",
        ]
        if self.network_owned:
            commands.append(f"docker network rm {NETWORK}")
        return commands


# ---------------------------------------------------------------- publish


def _login(api: Api, user: str, password: str) -> str:
    payload = api.ok("POST", "/api/v1/auth/login", {
        "username": user, "password": password,
    })
    data = _unwrap(payload) or {}
    token = data.get("access_token")
    if not token:
        raise AssertionError("login response did not include access_token")
    _register_secret(token)
    api.token = token
    return str((data.get("user") or {}).get("role") or "")


def build_object_type(ontology_id: str) -> dict[str, Any]:
    type_id = f"ot-recap-supplier-{ontology_id[:8]}"
    properties = []
    for index, (name, display, prop_type, required) in enumerate(
            ONTOLOGY_PROPERTIES):
        properties.append({
            "id": f"prop-{index:02d}-{name}",
            "name": name,
            "displayName": display,
            "type": prop_type,
            "required": required,
        })
    return {
        "id": type_id,
        "name": "Supplier",
        "displayName": "供应商复盘夹具",
        "description": "Synthetic recap acceptance object type",
        "primaryKey": "supplier_id",
        "properties": properties,
        "interfaces": [],
        "positionX": 0.0,
        "positionY": 0.0,
    }


def _create_ontology(api: Api, name: str) -> dict[str, Any]:
    payload = api.ok("POST", "/api/v1/ontologies", {
        "name": name,
        "domain": "其他",
        "description": "Synthetic recap acceptance ontology",
        "icon": "network",
    }, expected=(201,))
    data = _unwrap(payload) or {}
    if not data.get("id") or not data.get("current_release_id"):
        raise AssertionError("ontology create response lacks ids")
    return data


def _save_full(api: Api, ontology_id: str,
               object_type: dict[str, Any]) -> dict[str, Any]:
    payload = api.ok(
        "PUT", f"/api/v2/formal/ontologies/{ontology_id}/full",
        {"objectTypes": [object_type]},
    )
    return _unwrap(payload) or {}


def _get_full(api: Api, ontology_id: str) -> dict[str, Any]:
    return _unwrap(api.ok(
        "GET", f"/api/v2/formal/ontologies/{ontology_id}/full")) or {}


def _register_asset(api: Api, *, name: str, table_name: str) -> dict[str, Any]:
    payload = api.ok("POST", "/api/v2/integrated-datasets", {
        "name": name,
        "source_connection": {
            "name": f"{name}-source",
            "kind": "mysql",
            "config": {
                "host": "127.0.0.1",
                "port": MYSQL_PORT,
                "database": MYSQL_DATABASE,
                "user": MYSQL_USER,
                "password": MYSQL_PASSWORD,
                "dbx_host": MYSQL_CONTAINER,
                "dbx_port": 3306,
            },
        },
        "schema_name": MYSQL_DATABASE,
        "table_name": table_name,
        "writable": False,
    }, expected=(201,))
    data = _unwrap(payload) or {}
    if not data.get("id") or not data.get("dataset_id"):
        raise AssertionError("asset registration response lacks ids")
    return data


def _create_remote_mapping(
    api: Api, ontology_id: str, *, dataset_id: str,
    object_type_id: str, field_mapping: dict[str, str],
) -> str:
    payload = api.ok("POST", f"/api/v2/ontologies/{ontology_id}/mappings", {
        "curated_dataset_id": dataset_id,
        "entity_class": "Supplier",
        "field_mapping": field_mapping,
        "target_object_type_id": object_type_id,
        "binding_mode": "remote",
        "confidence": 1.0,
    })
    data = _unwrap(payload) or {}
    mapping_id = data.get("mapping_id") or data.get("id")
    if not mapping_id:
        raise AssertionError("mapping create response lacks mapping id")
    return str(mapping_id)


def _create_draft(api: Api, ontology_id: str,
                  source_version_id: str) -> dict[str, Any]:
    payload = api.ok(
        "POST",
        f"/api/v2/ontologies/{ontology_id}/versions/"
        f"{source_version_id}/drafts",
        {},
        expected=(201,),
    )
    data = _unwrap(payload) or {}
    if not data.get("id"):
        raise AssertionError("draft create response lacks version id")
    return data


def _workspace(api: Api, ontology_id: str,
               version_id: str) -> dict[str, Any]:
    return _unwrap(api.ok(
        "GET", f"/api/v2/ontologies/{ontology_id}/versions/"
               f"{version_id}/workspace")) or {}


def _save_workspace(api: Api, ontology_id: str, version_id: str,
                    object_types: list[dict[str, Any]]) -> dict[str, Any]:
    workspace = _workspace(api, ontology_id, version_id)
    revision = workspace.get("revision")
    body = {
        "baseRevision": revision,
        "objectTypes": object_types,
        "linkTypes": [],
        "actions": [],
        "functions": [],
    }
    payload = api.ok(
        "PUT",
        f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace",
        body,
    )
    return _unwrap(payload) or {}


def _save_workspace_mappings(
    api: Api, ontology_id: str, version_id: str, mapping_id: str,
    *, dataset_id: str, object_type_id: str,
    field_mapping: dict[str, str],
) -> dict[str, Any]:
    current = _workspace(api, ontology_id, version_id)
    body = {
        "baseRevision": current.get("revision"),
        "mappings": [{
            "id": mapping_id,
            "curatedDatasetId": dataset_id,
            "entityClass": "Supplier",
            "targetObjectTypeId": object_type_id,
            "fieldMapping": field_mapping,
            "bindingMode": "remote",
        }],
        "linkMappings": [],
        "sentinels": current.get("sentinels") or [],
    }
    payload = api.ok(
        "PUT",
        f"/api/v2/ontologies/{ontology_id}/versions/"
        f"{version_id}/workspace/mappings",
        body,
    )
    return _unwrap(payload) or {}


def _create_trial(api: Api, ontology_id: str,
                  version_id: str) -> dict[str, Any]:
    payload = api.ok(
        "POST",
        f"/api/v2/ontologies/{ontology_id}/versions/"
        f"{version_id}/trial-runs",
        {},
        expected=(201,),
    )
    return _unwrap(payload) or {}


def _impact(api: Api, ontology_id: str, version_id: str) -> dict[str, Any]:
    return _unwrap(api.ok(
        "GET", f"/api/v2/ontologies/{ontology_id}/versions/"
               f"{version_id}/impact")) or {}


def _promote(api: Api, ontology_id: str, version_id: str, *,
             trial_run_id: str, impact_hash: str) -> dict[str, Any]:
    payload = api.ok(
        "POST",
        f"/api/v2/ontologies/{ontology_id}/versions/"
        f"{version_id}/promote",
        {"trial_run_id": trial_run_id, "impact_hash": impact_hash},
        expected=(201,),
    )
    return _unwrap(payload) or {}


# --------------------------------------------------------------- runs


def _run_path(ontology_id: str, run_id: str) -> str:
    return (f"/api/v2/formal/ontologies/{ontology_id}"
            f"/agent/chat/runs/{run_id}")


def _conversation_path(ontology_id: str, conversation_id: str) -> str:
    return (f"/api/v2/formal/ontologies/{ontology_id}"
            f"/agent/conversations/{conversation_id}")


def _conversation_export_path(ontology_id: str, conversation_id: str) -> str:
    return (_conversation_path(ontology_id, conversation_id) + "/export")


def _submit_run(api: Api, ontology_id: str, release_id: str,
                model_id: str, *, run_id: str, message: str) -> dict[str, Any]:
    payload = api.ok(
        "POST",
        f"/api/v2/formal/ontologies/{ontology_id}/agent/chat/runs",
        {
            "message": message,
            "runId": run_id,
            "modelId": model_id,
            "releaseId": release_id,
            "stream": True,
        },
    )
    data = _unwrap(payload) or {}
    if data.get("releaseId") != release_id:
        raise AssertionError(
            f"submitted run froze release {data.get('releaseId')!r}, "
            f"expected {release_id!r}")
    return data


def _get_run(api: Api, ontology_id: str, run_id: str) -> dict[str, Any]:
    return _unwrap(api.ok("GET", _run_path(ontology_id, run_id))) or {}


def _wait_run(api: Api, ontology_id: str, run_id: str, *,
              timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _get_run(api, ontology_id, run_id)
        if last.get("status") in TERMINAL_RUN_STATUSES:
            return last
        time.sleep(0.25)
    status = (last or {}).get("status")
    hint = (" (still queued: is the NATS executor running?)"
            if status == "queued" else "")
    raise TimeoutError(
        f"run {run_id} did not reach a terminal state; "
        f"last status={status}{hint}")


def _conversation(api: Api, ontology_id: str,
                  conversation_id: str) -> dict[str, Any]:
    return _unwrap(api.ok(
        "GET", _conversation_path(ontology_id, conversation_id))) or {}


def _conversation_export(api: Api, ontology_id: str,
                         conversation_id: str) -> dict[str, Any]:
    """Full conversation export: unlike the display detail, step results here
    are not truncated for presentation."""
    return _unwrap(api.ok(
        "GET", _conversation_export_path(ontology_id, conversation_id))) or {}


def _query_view_page(
    api: Api, ontology_id: str, conversation_id: str,
    query_spec: dict[str, Any], *, page_size: int = 3,
    drilldown: str | None = None,
) -> dict[str, Any]:
    """Replay a tool querySpec (optionally a signed drilldown) via query-view."""
    body: dict[str, Any] = {
        "conversationId": conversation_id,
        "querySpec": query_spec,
        "pageSize": page_size,
    }
    if drilldown:
        body["drilldown"] = drilldown
    return _unwrap(api.ok(
        "POST",
        f"/api/v2/formal/ontologies/{ontology_id}/agent/query-view",
        body,
    )) or {}


def _step_tool(step: dict[str, Any]) -> str:
    return str(step.get("tool") or step.get("name") or "")


def _run_case(
    api: Api, *, ontology_id: str, release_id: str, model_id: str,
    run_id: str, message: str, timeout: float,
) -> dict[str, Any]:
    submitted = _submit_run(
        api, ontology_id, release_id, model_id,
        run_id=run_id, message=message,
    )
    conversation_id = str(submitted.get("conversationId") or "")
    if not conversation_id:
        raise AssertionError("run submission lacks conversationId")
    final = _wait_run(api, ontology_id, run_id, timeout=timeout)
    if final.get("status") != "succeeded":
        raise AssertionError(
            f"run {run_id} ended {final.get('status')!r}: "
            f"{redact(final.get('error'))[:600]}")
    content = str(final.get("content") or "")
    verification = final.get("verification")
    if isinstance(verification, dict) and verification.get("passed") is False:
        raise AssertionError(
            f"answer verification failed: {verification.get('unverified')}")
    if "结论校验" in content:
        raise AssertionError("answer carried an unverified-number warning")
    conversation = _conversation(api, ontology_id, conversation_id)
    if conversation.get("ontologyReleaseId") != release_id:
        raise AssertionError("conversation is not pinned to the release")
    export = _conversation_export(api, ontology_id, conversation_id)
    full_steps: list[dict[str, Any]] = []
    for message in reversed(export.get("messages") or []):
        if message.get("role") == "assistant" and message.get("steps"):
            full_steps = list(message.get("steps") or [])
            break
    if not full_steps:
        # Older backends may not export full steps; the display steps are
        # still evidence for small results.
        full_steps = list(final.get("steps") or [])
    return {
        "runId": run_id,
        "conversationId": conversation_id,
        "status": final.get("status"),
        "content": content,
        "steps": full_steps,
        "displaySteps": final.get("steps") or [],
        "verification": verification,
        "messages": conversation.get("messages") or [],
    }


def _single_step(run: dict[str, Any], tool: str) -> dict[str, Any]:
    steps = run.get("steps") or []
    if len(steps) != 1:
        raise AssertionError(
            f"expected one {tool} step, got tools="
            f"{[_step_tool(step) for step in steps]}")
    step = steps[0]
    if _step_tool(step) != tool:
        raise AssertionError(
            f"expected tool {tool!r}, got {_step_tool(step)!r}")
    result = step.get("result")
    if not isinstance(result, dict):
        raise AssertionError(f"{tool} step has no result object")
    return result


# ------------------------------------------------------------- cases


class CaseRecorder:
    def __init__(self) -> None:
        self.cases: list[dict[str, Any]] = []

    def run(self, case_id: str, fn: Callable[[], dict[str, Any] | None],
            *, status: str = "passed") -> dict[str, Any]:
        started = time.monotonic()
        started_at = iso_now()
        try:
            details = fn() or {}
            if not isinstance(details, dict):
                raise TypeError(f"case {case_id} returned non-dict details")
        except Exception as exc:  # noqa: BLE001 - evidence then re-raise
            self.cases.append({
                "id": case_id,
                "status": "failed",
                "startedAt": started_at,
                "durationMs": int((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {redact(exc)[:800]}",
            })
            raise
        self.cases.append({
            "id": case_id,
            "status": status,
            "startedAt": started_at,
            "durationMs": int((time.monotonic() - started) * 1000),
            "details": details,
        })
        return details


def _assert_group_order(groups: list[dict[str, Any]], *,
                        direction: str) -> list[str]:
    values = [
        str((group.get("groupValues") or [group.get("group")])[0])
        for group in groups
    ]
    if values != sorted(values, reverse=(direction == "desc")):
        raise AssertionError(f"group order is not {direction}: {values[:5]}")
    return values


def _case_describe(api: Api, ctx: dict[str, Any]) -> dict[str, Any]:
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"], run_id=f"{ctx['nonce']}-describe47",
        message="recap.describe47 请描述 Supplier 的授权属性与可查询性",
        timeout=ctx["timeout"],
    )
    result = _single_step(run, "describe_objects")
    types = result.get("objectTypes") or []
    if len(types) != 1:
        raise AssertionError("describe_objects did not return one object type")
    entry = types[0]
    if int(entry.get("propertyCount") or 0) != PROPERTY_COUNT:
        raise AssertionError(
            f"propertyCount {entry.get('propertyCount')} != {PROPERTY_COUNT}")
    if int(entry.get("queryablePropertyCount") or 0) != PROPERTY_COUNT:
        raise AssertionError("not every mapped property is marked queryable")
    properties = entry.get("properties") or []
    if len(properties) != PROPERTY_COUNT:
        raise AssertionError("describe properties length != 47")
    if any(item.get("queryable") is not True for item in properties):
        raise AssertionError("describe marks a mapped property unqueryable")
    return {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "propertyCount": entry.get("propertyCount"),
        "queryablePropertyCount": entry.get("queryablePropertyCount"),
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


def _case_projection(api: Api, ctx: dict[str, Any]) -> dict[str, Any]:
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"], run_id=f"{ctx['nonce']}-projection47",
        message="recap.projection47 默认投影列出 Supplier",
        timeout=ctx["timeout"],
    )
    result = _single_step(run, "query_objects")
    projection = result.get("projection")
    if not isinstance(projection, dict):
        raise AssertionError("query_objects default projection metadata missing")
    selected = projection.get("selectedFields") or []
    omitted = projection.get("omittedFields") or []
    if int(projection.get("cap") or 0) != MAX_SELECT_ITEMS:
        raise AssertionError("projection cap is not the 32-item contract")
    if len(selected) != MAX_SELECT_ITEMS:
        raise AssertionError(
            f"default projection selected {len(selected)} != {MAX_SELECT_ITEMS}")
    if len(omitted) != PROPERTY_COUNT - MAX_SELECT_ITEMS:
        raise AssertionError(
            f"default projection omitted {len(omitted)} fields, "
            f"expected {PROPERTY_COUNT - MAX_SELECT_ITEMS}")
    if selected[0] != "supplier_id":
        raise AssertionError("primary key is not the first projected field")
    query_spec = result.get("querySpec")
    if not query_spec:
        raise AssertionError("bounded projection lost the replayable querySpec")
    rows = result.get("rows") or []
    rows_source = "tool_result"
    if not rows and result.get("rowsOmitted") is True:
        # Remote query-view results deliberately omit detail rows from the
        # model payload; the signed query-view surface is the replay path.
        page = _query_view_page(
            api, ctx["ontologyId"], run["conversationId"], query_spec,
            page_size=3,
        )
        rows = page.get("rows") or []
        rows_source = "query_view"
    if not rows:
        raise AssertionError("default projection returned no rows")
    for row in rows:
        leaked = [name for name in omitted if name in row]
        if leaked:
            raise AssertionError(
                f"omitted fields leaked into observed rows: {leaked[:3]}")
        if "supplier_id" not in row:
            raise AssertionError("projected rows lack the primary key")
    columns = result.get("columns") or []
    column_names = {
        str(item.get("name")) for item in columns if isinstance(item, dict)
    }
    if column_names and column_names & set(omitted):
        raise AssertionError("omitted fields leaked into the column contract")
    return {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "cap": projection.get("cap"),
        "selectedCount": len(selected),
        "omittedCount": len(omitted),
        "omittedSample": omitted[:3],
        "rows": len(rows),
        "rowsSource": rows_source,
        "rowsOmittedFromToolPayload": bool(result.get("rowsOmitted")),
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


def _case_bucket(api: Api, ctx: dict[str, Any], bucket: str,
                 oracle: Oracle) -> dict[str, Any]:
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"],
        run_id=f"{ctx['nonce']}-bucket-{bucket}",
        message=f"recap.bucket.{bucket} 按{bucket}统计 Supplier 趋势",
        timeout=ctx["timeout"],
    )
    result = _single_step(run, "aggregate_objects")
    groups = result.get("groups") or []
    if not groups:
        raise AssertionError("bucket aggregation returned no groups")
    if result.get("groupBuckets") != [bucket]:
        raise AssertionError(
            f"groupBuckets {result.get('groupBuckets')} != [{bucket!r}]")
    labels = _assert_group_order(groups, direction="asc")
    actual = Counter()
    for group in groups:
        values = group.get("groupValues") or []
        label = values[0] if values else group.get("group")
        count = group.get("count")
        if count is None:
            count = group.get("value")
        actual[str(label)] = int(Decimal(str(count)))
    expected = dict(oracle.bucket_counts[bucket])
    if actual != expected:
        missing = sorted(set(expected) - set(actual))[:5]
        extra = sorted(set(actual) - set(expected))[:5]
        raise AssertionError(
            f"{bucket} bucket counts mismatch; missing={missing} extra={extra}")
    total = int(result.get("totalGroups") or 0)
    if total != len(expected):
        raise AssertionError(
            f"{bucket} totalGroups {total} != oracle {len(expected)}")
    return {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "bucket": bucket,
        "groups": len(groups),
        "totalGroups": total,
        "groupLimit": result.get("groupLimit"),
        "orderedAscending": True,
        "countsSha256": hashlib.sha256(
            json.dumps(sorted(actual.items()), ensure_ascii=False)
            .encode("utf-8")).hexdigest(),
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


def _case_top10(api: Api, ctx: dict[str, Any],
                oracle: Oracle) -> dict[str, Any]:
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"], run_id=f"{ctx['nonce']}-top10",
        message="recap.top10 列出区域 Top10",
        timeout=ctx["timeout"],
    )
    result = _single_step(run, "aggregate_objects")
    groups = result.get("groups") or []
    if len(groups) != 10:
        raise AssertionError(
            f"top10 returned {len(groups)} groups, expected 10")
    if int(result.get("groupLimit") or 0) != 10:
        raise AssertionError("groupLimit is not 10")
    total = int(result.get("totalGroups") or 0)
    if total != len(oracle.region_counts):
        raise AssertionError(
            f"totalGroups {total} != oracle {len(oracle.region_counts)}")
    if total <= 10:
        raise AssertionError("fixture did not produce more than 10 groups")
    if result.get("truncated") is not True:
        raise AssertionError("top10 truncation is not flagged")
    if result.get("matched") is not None:
        raise AssertionError("truncated top10 must not publish a matched sum")
    if not result.get("truncationNote"):
        raise AssertionError("top10 lacks a truncation note")
    labels = [
        str((group.get("groupValues") or [group.get("group")])[0])
        for group in groups
    ]
    expected_labels = [region for region, _count in oracle.top10]
    if labels != expected_labels:
        raise AssertionError(
            f"top10 order {labels} != oracle {expected_labels}")
    counts = []
    for group in groups:
        value = group.get("count")
        if value is None:
            value = group.get("value")
        counts.append(int(Decimal(str(value))))
    expected_counts = [count for _region, count in oracle.top10]
    if counts != expected_counts:
        raise AssertionError(f"top10 counts {counts} != {expected_counts}")
    if any(left < right for left, right in zip(counts, counts[1:])):
        raise AssertionError(f"top10 counts are not non-increasing: {counts}")
    if f"{len(groups)} 组" not in run["content"]:
        raise AssertionError(
            "model answer did not reflect the 10-row preview it received")
    return {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "returned": len(groups),
        "totalGroups": total,
        "groupLimit": result.get("groupLimit"),
        "truncated": result.get("truncated"),
        "matched": result.get("matched"),
        "previewRowsSeenByModel": len(groups),
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


def _case_zero(api: Api, ctx: dict[str, Any]) -> dict[str, Any]:
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"], run_id=f"{ctx['nonce']}-zero",
        message="recap.zero 查询不存在的区域",
        timeout=ctx["timeout"],
    )
    result = _single_step(run, "aggregate_objects")
    coverage = result.get("dataCoverage")
    if not isinstance(coverage, dict):
        raise AssertionError("zero result lacks dataCoverage")
    if int(Decimal(str(result.get("value") or 0))) != 0:
        raise AssertionError("zero case value is not 0")
    if int(Decimal(str(result.get("matched") or 0))) != 0:
        raise AssertionError("zero case matched is not 0")
    if coverage.get("zeroResult") is not True:
        raise AssertionError("dataCoverage.zeroResult is not true")
    if coverage.get("reason") != "filter_no_match":
        raise AssertionError(
            f"zero reason {coverage.get('reason')!r} != filter_no_match")
    return {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "value": result.get("value"),
        "matched": result.get("matched"),
        "zeroResult": coverage.get("zeroResult"),
        "reason": coverage.get("reason"),
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


def _case_distinct_window(api: Api, ctx: dict[str, Any],
                          oracle: Oracle) -> dict[str, Any]:
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"], run_id=f"{ctx['nonce']}-distinct-window",
        message="recap.distinct_window 统计 10 月窗口内不同用户",
        timeout=ctx["timeout"],
    )
    result = _single_step(run, "analyze_sequences")
    distinct = int(Decimal(str(result.get("distinctSubjects") or 0)))
    candidates = int(Decimal(str(result.get("candidateRows") or 0)))
    threshold = int(Decimal(str(result.get("threshold") or 0)))
    if distinct != oracle.october_distinct:
        raise AssertionError(
            f"window distinct {distinct} != oracle {oracle.october_distinct}")
    if candidates != oracle.october_candidates:
        raise AssertionError(
            f"candidate rows {candidates} != oracle "
            f"{oracle.october_candidates}")
    if result.get("meetsThreshold") is not (distinct >= threshold):
        raise AssertionError("meetsThreshold is inconsistent with the result")
    window = result.get("window") or {}
    if window.get("startUtc") != "2026-09-30T16:00:00+00:00" or \
            window.get("endUtc") != "2026-10-31T16:00:00+00:00":
        raise AssertionError(f"resolved window is wrong: {window}")
    return {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "distinctSubjects": distinct,
        "candidateRows": candidates,
        "threshold": threshold,
        "meetsThreshold": result.get("meetsThreshold"),
        "window": window,
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


def _case_distinct_module(api: Api, ctx: dict[str, Any],
                          oracle: Oracle) -> dict[str, Any]:
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"], run_id=f"{ctx['nonce']}-distinct-module",
        message="recap.distinct_module 9 月按模块统计不同用户",
        timeout=ctx["timeout"],
    )
    result = _single_step(run, "analyze_sequences")
    groups = result.get("groups") or []
    actual: dict[str, int] = {}
    for group in groups:
        values = group.get("groupValues") or []
        module = str(values[0]) if values else ""
        actual[module] = int(Decimal(
            str(group.get("distinctSubjects") or 0)))
    expected = oracle.september_module_distinct
    if actual != expected:
        raise AssertionError(
            f"per-module distinct {actual} != oracle {expected}")
    threshold = int(Decimal(str(result.get("threshold") or 0)))
    expected_matching = sum(
        1 for value in expected.values() if value >= threshold)
    matching = int(Decimal(str(result.get("matchingGroups") or 0)))
    if matching != expected_matching:
        raise AssertionError(
            f"matchingGroups {matching} != oracle {expected_matching}")
    total = int(result.get("totalGroups") or 0)
    if total != len(expected):
        raise AssertionError(
            f"totalGroups {total} != oracle {len(expected)}")
    return {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "groups": actual,
        "matchingGroups": matching,
        "totalGroups": total,
        "threshold": threshold,
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


def _parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_row_value(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key and key in row and row[key] is not None:
            return row[key]
    return None


def _case_window30(api: Api, ctx: dict[str, Any],
                   oracle: Oracle) -> tuple[str, dict[str, Any]]:
    """Formal success acceptance for the fixed 30-minute window question.

    Uses the governed ``analyze_sequences`` ``window_distinct`` mode (module +
    half_hour inner count_distinct, threshold-filtered outer module
    aggregation) and validates every returned aggregate against the local
    oracle.  A ``--window30-contract`` file takes precedence for a future
    contract revision.  If the backend does not expose the capability the
    case is ``blocked_by_capability`` (never passed).
    """
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"], run_id=f"{ctx['nonce']}-window30",
        message="recap.window30 统计 9 月同一模块每半小时窗口内不同用户数",
        timeout=ctx["timeout"],
    )
    steps = run.get("steps") or []
    if len(steps) != 1:
        raise AssertionError("window30 run did not persist exactly one step")
    step = steps[0]
    if _step_tool(step) != "analyze_sequences":
        raise AssertionError(
            f"window30 used {_step_tool(step)!r}, expected analyze_sequences")
    result = step.get("result") or {}
    contract = _WINDOW30_CONTRACT
    if isinstance(contract, dict):
        if result.get("error"):
            raise AssertionError(
                f"governed window30 call failed: {result.get('error')}")
        expected = contract.get("expected") or {}
        actual: dict[str, Any] = {}
        for key, wanted in expected.items():
            actual[key] = result.get(key)
            if actual[key] != wanted:
                raise AssertionError(
                    f"window30 {key}={actual[key]!r} != expected {wanted!r}")
        if not expected:
            raise AssertionError("window30 contract has no expected fields")
        return "passed", {
            "runId": run["runId"],
            "conversationId": run["conversationId"],
            "contract": True,
            "result": actual,
            "oracle": oracle.report()["window30"],
            "answer": run["content"],
            "verificationPassed": (
                run["verification"].get("passed")
                if isinstance(run["verification"], dict) else None
            ),
        }

    error = str(result.get("error") or "")
    if error:
        capability_codes = (
            "sequence_window_minutes_unsupported",
            "sequence_window_required",
            "sequence_mode_unsupported",
            "sequence_invalid_group",
        )
        if not any(code in error for code in capability_codes):
            raise AssertionError(
                "window30 failed for a non-capability reason: " + error)
        if "时间窗口粒度" not in run["content"]:
            raise AssertionError(
                "model did not state the capability limitation explicitly")
        return "blocked_by_capability", {
            "runId": run["runId"],
            "conversationId": run["conversationId"],
            "contract": False,
            "toolError": error[:300],
            "oracle": oracle.report()["window30"],
            "honestRejection": True,
            "answer": run["content"],
            "verificationPassed": (
                run["verification"].get("passed")
                if isinstance(run["verification"], dict) else None
            ),
        }

    if result.get("mode") != "window_distinct":
        raise AssertionError(
            f"window30 mode {result.get('mode')!r} != window_distinct")
    if int(Decimal(str(result.get("windowMinutes") or 0))) != 30:
        raise AssertionError("window30 windowMinutes != 30")
    if result.get("windowBucket") != "half_hour":
        raise AssertionError(
            f"window30 bucket {result.get('windowBucket')!r} != half_hour")
    if result.get("windowTimezone") != TARGET_TIMEZONE:
        raise AssertionError(
            f"window30 timezone {result.get('windowTimezone')!r} != "
            f"{TARGET_TIMEZONE!r}")
    if result.get("evaluatedWindows") is not None:
        raise AssertionError("window30 evaluatedWindows must be null")
    if "half-hour" not in str(result.get("windowAlignment") or ""):
        raise AssertionError("window30 alignment contract is missing")
    threshold = int(Decimal(str(result.get("threshold") or 0)))
    if threshold != 5:
        raise AssertionError(f"window30 threshold {threshold} != 5")
    windows = int(Decimal(str(result.get("qualifyingWindows") or 0)))
    modules = int(Decimal(str(result.get("qualifyingModules") or 0)))
    module_count = int(Decimal(str(result.get("moduleCount") or 0)))
    if windows != oracle.window30_total:
        raise AssertionError(
            f"qualifyingWindows {windows} != oracle {oracle.window30_total}")
    if modules != oracle.window30_modules:
        raise AssertionError(
            f"qualifyingModules {modules} != oracle "
            f"{oracle.window30_modules}")
    if module_count != oracle.window30_modules:
        raise AssertionError(
            f"moduleCount {module_count} != oracle "
            f"{oracle.window30_modules}")
    actual_modules: dict[str, int] = {}
    for entry in result.get("modules") or []:
        values = entry.get("moduleValues") or []
        module = str(values[0]) if values else str(entry.get("module") or "")
        actual_modules[module] = int(Decimal(
            str(entry.get("qualifyingWindows") or 0)))
    if actual_modules != oracle.window30_by_module:
        raise AssertionError(
            f"per-module windows {actual_modules} != oracle "
            f"{oracle.window30_by_module}")
    if result.get("modulesTruncated") is True:
        raise AssertionError("window30 module list was truncated")
    if result.get("exact") is not True or \
            result.get("completeness") != "complete":
        raise AssertionError("window30 result is not complete/exact")
    window = result.get("window") or {}
    if window.get("startUtc") != "2026-08-31T16:00:00+00:00" or \
            window.get("endUtc") != "2026-09-30T16:00:00+00:00":
        raise AssertionError(f"window30 resolved window is wrong: {window}")
    if f"达标 {windows} 个" not in run["content"] or \
            f"模块 {modules} 个" not in run["content"]:
        raise AssertionError(
            "model answer did not reflect the governed window counts")

    # Primary querySpec must replay the exact headline totals (one row), while
    # the module view is a separate moduleQuerySpec.
    primary_spec = result.get("querySpec")
    if not isinstance(primary_spec, dict):
        raise AssertionError("window30 primary querySpec is missing")
    if (primary_spec.get("description") or {}).get("view") != "totals":
        raise AssertionError(
            "window30 primary querySpec is not the totals view")
    primary_page = _query_view_page(
        api, ctx["ontologyId"], run["conversationId"], primary_spec,
        page_size=1,
    )
    primary_rows = primary_page.get("rows") or []
    if len(primary_rows) != 1:
        raise AssertionError(
            f"window30 primary query-view returned {len(primary_rows)} rows, "
            "expected 1")
    primary_row = primary_rows[0]

    def _pick_value(row, output, label):
        for key in (output, label):
            if key in row:
                return row[key]
        return None

    primary_windows = int(Decimal(str(
        _pick_value(primary_row, "qualifyingWindows", "达标窗口数") or 0)))
    primary_modules = int(Decimal(str(
        _pick_value(primary_row, "qualifyingModules", "达标模块数") or 0)))
    if primary_windows != oracle.window30_total or \
            primary_modules != oracle.window30_modules:
        raise AssertionError(
            "window30 primary query-view totals "
            f"{primary_windows}/{primary_modules} != oracle "
            f"{oracle.window30_total}/{oracle.window30_modules}")
    if primary_page.get("hasNext") is True:
        raise AssertionError("window30 primary totals unexpectedly paginated")

    module_spec = result.get("moduleQuerySpec")
    if not isinstance(module_spec, dict):
        raise AssertionError("window30 moduleQuerySpec is missing")
    if (module_spec.get("description") or {}).get("view") != "modules":
        raise AssertionError(
            "window30 moduleQuerySpec is not the modules view")
    order_items = (module_spec.get("query") or {}).get("orderBy") or []
    order_pairs = [
        (str(item.get("output")), str(item.get("direction")))
        for item in order_items if isinstance(item, dict)
    ]
    expected_order = [
        ("qualifyingWindows", "desc"),
        ("maxDistinctSubjects", "desc"),
        ("g0", "asc"),
    ]
    if order_pairs[:3] != expected_order:
        raise AssertionError(
            f"window30 moduleQuerySpec order {order_pairs[:3]} != "
            f"{expected_order}")

    # Signed sample-window drilldown: the token is used only in this request
    # and is never printed or persisted in the report.
    samples = result.get("sampleWindows") or []
    if not samples:
        raise AssertionError("window30 sampleWindows is empty")
    sample = samples[0]
    sample_module_values = sample.get("moduleValues") or []
    sample_module = (
        str(sample_module_values[0]) if sample_module_values
        else str(sample.get("module") or "")
    )
    expected_candidate_rows = int(Decimal(str(
        sample.get("candidateRows") or 0)))
    expected_distinct = int(Decimal(str(
        sample.get("distinctSubjects") or 0)))
    interval_start = _parse_utc(sample.get("windowStartUtc"))
    interval_end = _parse_utc(sample.get("windowEndUtc"))
    if interval_start is None or interval_end is None or \
            not interval_start < interval_end:
        raise AssertionError("window30 sample interval is missing/invalid")
    sample_token = (sample.get("drilldown") or {}).get("token")
    if not sample_token:
        raise AssertionError("window30 sample drilldown token is missing")
    try:
        drilldown_page = _query_view_page(
            api, ctx["ontologyId"], run["conversationId"], primary_spec,
            page_size=min(200, max(10, expected_candidate_rows)),
            drilldown=str(sample_token),
        )
    except ApiFailure as exc:
        # Never include the request payload (and therefore the token) in the
        # evidence; the summary is code/message only.
        raise AssertionError(
            "window30 sample drilldown failed "
            f"HTTP {exc.status}: {_detail_summary(exc.detail)}"
        ) from None
    drilldown_rows = drilldown_page.get("rows") or []
    if drilldown_page.get("hasNext") is True:
        raise AssertionError(
            "window30 sample drilldown unexpectedly paginated")
    if len(drilldown_rows) != expected_candidate_rows:
        raise AssertionError(
            "window30 sample drilldown rows "
            f"{len(drilldown_rows)} != candidateRows {expected_candidate_rows}")
    drilldown_labels = primary_spec.get("drilldownLabels") or {}
    user_keys = [str(drilldown_labels.get("c0") or ""), "user_id", "c0"]
    time_keys = [str(drilldown_labels.get("c1") or ""),
                 "occurred_at", "c1"]
    module_keys = [str(drilldown_labels.get("c2") or ""), "module", "c2"]
    distinct_users: set[str] = set()
    bad_modules = 0
    bad_intervals = 0
    for row in drilldown_rows:
        if not isinstance(row, dict):
            raise AssertionError("window30 sample drilldown row is not an object")
        user = _first_row_value(row, user_keys)
        if user is None or str(user) == "":
            raise AssertionError("window30 sample drilldown row lacks a user")
        distinct_users.add(str(user))
        module = _first_row_value(row, module_keys)
        if str(module) != sample_module:
            bad_modules += 1
        occurred = _parse_utc(_first_row_value(row, time_keys))
        if occurred is None or not (
                interval_start <= occurred < interval_end):
            bad_intervals += 1
    if bad_modules:
        raise AssertionError(
            f"window30 sample drilldown leaked {bad_modules} rows outside "
            f"module {sample_module!r}")
    if bad_intervals:
        raise AssertionError(
            "window30 sample drilldown leaked "
            f"{bad_intervals} rows outside the sample interval")
    if len(distinct_users) != expected_distinct:
        raise AssertionError(
            "window30 sample drilldown distinct users "
            f"{len(distinct_users)} != {expected_distinct}")

    # The model-facing tool payload must carry the sample windows without any
    # signed per-window drilldown token. The loopback fixture records only
    # booleans, never token values.
    audit = (
        (_stub_stats(ctx["modelOrigin"]).get("audit") or {}).get("window30")
        or {}
    )
    if audit.get("drilldownKeySeen") or audit.get("tokenFieldSeen") or \
            audit.get("sampleWindowDrilldownTokens"):
        raise AssertionError(
            "signed sampleWindows drilldown token reached the model payload")
    if audit.get("sampleWindowsSeen") is not True:
        raise AssertionError(
            "model payload audit did not observe sampleWindows; "
            "cannot prove token stripping")
    return "passed", {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "contract": False,
        "mode": result.get("mode"),
        "windowMinutes": result.get("windowMinutes"),
        "windowBucket": result.get("windowBucket"),
        "threshold": threshold,
        "qualifyingWindows": windows,
        "qualifyingModules": modules,
        "moduleCount": module_count,
        "modules": actual_modules,
        "primaryQueryView": {
            "rows": len(primary_rows),
            "qualifyingWindows": primary_windows,
            "qualifyingModules": primary_modules,
            "hasNext": primary_page.get("hasNext"),
        },
        "moduleQuerySpec": {
            "present": True,
            "view": "modules",
            "order": [list(pair) for pair in order_pairs[:3]],
        },
        "sampleDrilldown": {
            "module": sample_module,
            "expectedCandidateRows": expected_candidate_rows,
            "expectedDistinctUsers": expected_distinct,
            "rows": len(drilldown_rows),
            "distinctUsers": len(distinct_users),
            "allRowsInModule": bad_modules == 0,
            "allRowsWithinInterval": bad_intervals == 0,
            "intervalStartUtc": interval_start.isoformat(),
            "intervalEndUtc": interval_end.isoformat(),
            "hasNext": drilldown_page.get("hasNext"),
        },
        "modelPayloadAudit": audit,
        "oracle": oracle.report()["window30"],
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


def _case_window30_guard(api: Api, ctx: dict[str, Any]) -> dict[str, Any]:
    """Boundary guard: non-30-minute fixed windows are rejected explicitly."""
    run = _run_case(
        api, ontology_id=ctx["ontologyId"], release_id=ctx["releaseId"],
        model_id=ctx["modelId"], run_id=f"{ctx['nonce']}-window30-guard",
        message="recap.window30_guard 尝试 60 分钟固定窗口",
        timeout=ctx["timeout"],
    )
    result = _single_step(run, "analyze_sequences")
    error = str(result.get("error") or "")
    if "sequence_window_minutes_unsupported" not in error and \
            "仅支持固定 30 分钟窗口" not in error:
        raise AssertionError(
            "60-minute window was not rejected explicitly: " + error)
    if "时间窗口粒度" not in run["content"]:
        raise AssertionError(
            "model answer did not state the unsupported window granularity")
    return {
        "runId": run["runId"],
        "conversationId": run["conversationId"],
        "toolError": error[:300],
        "honestRejection": True,
        "answer": run["content"],
        "verificationPassed": (
            run["verification"].get("passed")
            if isinstance(run["verification"], dict) else None
        ),
    }


# ---------------------------------------------------------------- model


def _create_fake_model(api: Api, *, name: str,
                       model_base_url: str) -> str:
    _register_secret(FIXTURE_API_KEY)
    payload = api.ok("POST", "/api/v1/models", {
        "name": name,
        "config_type": "llm",
        "provider": "openai",
        "api_base": model_base_url,
        "api_key": FIXTURE_API_KEY,
        "models": [MODEL_NAME],
        "options": {"max_output_tokens": 256},
        "enabled": False,
    }, expected=(201,))
    data = _unwrap(payload) or {}
    model_id = data.get("id")
    if not model_id:
        raise AssertionError("model config create response lacks id")
    tested = _unwrap(api.ok(
        "POST", f"/api/v1/models/{model_id}/test")) or {}
    if not tested.get("ok"):
        raise AssertionError(
            "model connectivity test failed: "
            f"{tested.get('code')} {redact(tested.get('message'))[:300]}")
    enabled = _unwrap(api.ok(
        "POST", f"/api/v1/models/{model_id}/enabled", {"enabled": True})) or {}
    if not enabled.get("enabled"):
        raise AssertionError("model config was not enabled")
    return str(model_id)


# ------------------------------------------------------------------ main


def load_window30_contract(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("tool"):
        raise ValueError("window30 contract must contain a tool name")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Isolated ontology-assistant recap acceptance driver and "
            "loopback OpenAI-compatible model stub."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Requires a separately started backend (18036) and NATS "
            "executor. This script owns MySQL 33317, DBX 18039 and the "
            "model stub 18038; it never starts or stops the main backend, "
            "the main model (18037), or any other compose project."
        ),
    )
    parser.add_argument("--serve-model", action="store_true",
                        help="serve the fake LLM boundary instead of driving")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18038)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model-base-url", default=DEFAULT_MODEL_URL)
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--ontology-name-prefix",
                        default="assistant-recap-e2e")
    parser.add_argument("--model-config-prefix",
                        default="assistant-recap-e2e-model")
    parser.add_argument("--keep-model-config", action="store_true")
    parser.add_argument("--skip-infra", action="store_true",
                        help="reuse already running fixture MySQL/DBX")
    parser.add_argument("--window30-contract", type=Path, default=None,
                        help=(
                            "JSON file with the governed 30-minute window "
                            "tool/arguments/expected fields; enables the "
                            "formal success acceptance"
                        ))
    return parser


def serve_model(args: argparse.Namespace) -> int:
    if args.host != "127.0.0.1":
        print("[model-stub] ERROR: host must be 127.0.0.1", file=sys.stderr)
        return 2
    if not 0 <= args.port <= 65535:
        print("[model-stub] ERROR: port must be 0..65535", file=sys.stderr)
        return 2
    server, _state = start_model_stub(args.host, args.port)
    bound = server.server_address[1]
    print(
        f"[model-stub] listening http://{args.host}:{bound}/v1 "
        f"(fixture model: {MODEL_NAME})", flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("[model-stub] stopping", flush=True)
    finally:
        server.server_close()
    return 0


def _model_config_id(api: Api, name: str) -> str | None:
    status, payload = api.raw("GET", "/api/v1/models")
    if status != 200:
        return None
    items = _unwrap(payload)
    if isinstance(items, dict):
        items = items.get("items")
    for item in items or []:
        if isinstance(item, dict) and item.get("name") == name:
            return str(item.get("id") or "") or None
    return None


def run_driver(args: argparse.Namespace, password: str) -> int:
    _register_secret(password)
    _register_secret(FIXTURE_API_KEY)
    _register_secret(MYSQL_PASSWORD)
    _register_secret(DBX_PASSWORD)
    api = Api(args.base_url, timeout=min(float(args.timeout_seconds), 60.0))
    model_base = validate_loopback_url(args.model_base_url, allow_v1_path=True)
    nonce = uuid.uuid4().hex[:10]
    recorder = CaseRecorder()
    oracle = build_oracle(fixture_rows())
    infra = Infra()
    model_server: ThreadingHTTPServer | None = None
    model_state: StubState | None = None
    state: dict[str, Any] = {"nonce": nonce, "timeout": args.timeout_seconds}
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "ontology-assistant-recap-e2e",
        "status": "running",
        "startedAt": iso_now(),
        "finishedAt": None,
        "apiBase": api.base_url,
        "modelBase": model_base,
        "loopbackOnly": True,
        "fixture": {
            "ontologyName": f"{args.ontology_name_prefix}-{nonce}",
            "rowCount": len(oracle.rows),
            "propertyCount": PROPERTY_COUNT,
            "mappedPropertyCount": PROPERTY_COUNT,
            "windows": {
                "october": {
                    "timezone": TARGET_TIMEZONE,
                    "localStart": OCT_LOCAL_START,
                    "localEnd": OCT_LOCAL_END,
                },
                "september": {
                    "timezone": TARGET_TIMEZONE,
                    "localStart": SEPT_LOCAL_START,
                    "localEnd": SEPT_LOCAL_END,
                },
            },
        },
        "infra": {
            "mysql": {
                "container": MYSQL_CONTAINER,
                "port": MYSQL_PORT,
                "database": MYSQL_DATABASE,
                "table": None,
                "state": None,
            },
            "dbx": {
                "container": DBX_CONTAINER,
                "port": DBX_PORT,
                "image": DBX_IMAGE,
                "state": None,
            },
            "network": {
                "name": NETWORK,
                "state": None,
            },
            "modelStub": {
                "mode": "in-process",
                "baseUrl": model_base,
                "pid": os.getpid(),
            },
        },
        "oracle": oracle.report(),
        "publish": None,
        "cases": recorder.cases,
        "window30": {
            "status": "not_run",
            "question": (
                "September, same module, every 30-minute fixed window, "
                "at least 5 distinct users: qualifying window/module counts"
            ),
            "governedContractProvided": bool(args.window30_contract),
            "expression": "analyze_sequences mode=window_distinct "
                          "window_minutes=30",
        },
        "browser": None,
        "cleanup": {
            "commands": [],
            "note": (
                "Fixture MySQL/DBX and the synthetic ontology are left in "
                "place for real-UI inspection. Run the recorded commands to "
                "remove only this script's labelled containers/network."
            ),
        },
        "failures": [],
        "notes": [
            "Synthetic 63-row loopback fixture; not production-scale or "
            "production-environment acceptance.",
            "The driver imports no backend module and never writes SQL to "
            "the platform database; all platform effects use real HTTP.",
            "Only the upstream LLM HTTP boundary is faked; PostgreSQL, NATS "
            "durable runs, the executor and the remote MySQL source are real.",
            "The 30-minute fixed-window question uses the governed "
            "analyze_sequences window_distinct expression and is checked "
            "against the local oracle; a separate guard proves other "
            "granularities are rejected explicitly.  Without the backend "
            "capability the case is blocked_by_capability, never passed.",
        ],
    }

    failure: Exception | None = None
    model_id: str | None = None
    try:
        if not args.skip_infra:
            infra.ensure()
            table_name = infra.create_table(nonce)
        else:
            table_name = os.environ.get("RECAP_FIXTURE_TABLE", "")
            if not table_name:
                raise InfraError(
                    "--skip-infra requires RECAP_FIXTURE_TABLE")
        report["infra"]["mysql"]["state"] = infra.mysql_started
        report["infra"]["mysql"]["table"] = table_name
        report["infra"]["dbx"]["state"] = infra.dbx_started
        report["infra"]["network"]["state"] = infra.network_state
        report["cleanup"]["commands"] = infra.teardown_commands()

        model_origin = _origin(model_base)
        existing = _stub_health(model_origin)
        if existing is not None:
            if existing.get("mode") != "recap-model-stub":
                raise InfraError(
                    f"model stub port is already owned by another process: "
                    f"{model_origin}")
            report["infra"]["modelStub"]["mode"] = "external"
            report["infra"]["modelStub"]["pid"] = None
            report["infra"]["modelStub"]["boundPort"] = int(
                urlsplit(model_base).port or 0)
            pid_file = ARTIFACT_DIR / "model-stub.pid"
            if pid_file.exists():
                try:
                    stub_pid = pid_file.read_text().strip()
                    if stub_pid.isdigit():
                        report["infra"]["modelStub"]["pid"] = int(stub_pid)
                        report["cleanup"]["commands"].append(
                            f"kill {stub_pid}")
                except OSError:
                    pass
        else:
            parsed = urlsplit(model_base)
            model_server, model_state = start_model_stub(
                "127.0.0.1", int(parsed.port or 0))
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not _health_ok(model_origin):
                time.sleep(0.1)
            if not _health_ok(model_origin):
                raise InfraError(
                    "in-process model stub did not become ready")
            report["infra"]["modelStub"]["boundPort"] = (
                model_server.server_address[1])

        health = api.ok("GET", "/health/live")
        role = _login(api, args.admin_user, password)
        report["auth"] = {"live": bool(health), "role": role}
        model_id = _create_fake_model(
            api, name=f"{args.model_config_prefix}-{nonce}",
            model_base_url=model_base)
        report["modelConfigId"] = model_id
        report["modelConfigKept"] = bool(args.keep_model_config)

        def publish_case() -> dict[str, Any]:
            ontology = _create_ontology(
                api, f"{args.ontology_name_prefix}-{nonce}")
            ontology_id = str(ontology["id"])
            state["ontologyId"] = ontology_id
            state["v0ReleaseId"] = str(ontology["current_release_id"])
            object_type = build_object_type(ontology_id)
            _save_full(api, ontology_id, object_type)
            full = _get_full(api, ontology_id)
            saved_types = full.get("objectTypes") or []
            saved = next(
                (item for item in saved_types
                 if item.get("name") == "Supplier"), None)
            if not saved or len(saved.get("properties") or []) != PROPERTY_COUNT:
                raise AssertionError(
                    "full save did not persist 47 properties")
            object_type_id = str(saved["id"])
            state["objectTypeId"] = object_type_id
            asset = _register_asset(
                api, name=f"recap-asset-{nonce}", table_name=table_name)
            dataset_id = str(asset["dataset_id"])
            state["datasetId"] = dataset_id
            state["assetId"] = str(asset["id"])
            field_mapping = {
                name: name for name, _display, _type, _required
                in ONTOLOGY_PROPERTIES
            }
            mapping_id = _create_remote_mapping(
                api, ontology_id, dataset_id=dataset_id,
                object_type_id=object_type_id,
                field_mapping=field_mapping,
            )
            state["mappingId"] = mapping_id
            draft = _create_draft(api, ontology_id, state["v0ReleaseId"])
            draft_id = str(draft["id"])
            state["draftId"] = draft_id
            _save_workspace(api, ontology_id, draft_id, [saved])
            _save_workspace_mappings(
                api, ontology_id, draft_id, mapping_id,
                dataset_id=dataset_id, object_type_id=object_type_id,
                field_mapping=field_mapping,
            )
            trial = _create_trial(api, ontology_id, draft_id)
            if trial.get("status") != "passed":
                raise AssertionError(
                    f"trial did not pass: {trial.get('status')} "
                    f"{redact(trial.get('result'))[:600]}")
            trial_id = str(trial["id"])
            state["trialId"] = trial_id
            impact = _impact(api, ontology_id, draft_id)
            impact_hash = str(impact.get("impactHash") or "")
            if not impact_hash:
                raise AssertionError("impact report lacks impactHash")
            promoted = _promote(
                api, ontology_id, draft_id,
                trial_run_id=trial_id, impact_hash=impact_hash)
            release_id = str(promoted.get("id") or "")
            if not release_id:
                raise AssertionError("promote response lacks release id")
            if promoted.get("node_kind") != "release" or \
                    promoted.get("lifecycle_status") != "released":
                raise AssertionError(
                    "promoted version is not an immutable release")
            project = _unwrap(api.ok(
                "GET", f"/api/v1/ontologies/{ontology_id}")) or {}
            if project.get("current_release_id") != release_id:
                raise AssertionError(
                    "project current_release_id does not point at the new "
                    "release")
            versions = api.ok(
                "GET", f"/api/v2/ontologies/{ontology_id}/versions") or {}
            if versions.get("current_release_id") != release_id:
                raise AssertionError("versions endpoint release mismatch")
            workspace = _workspace(api, ontology_id, release_id)
            release_mappings = workspace.get("mappings") or []
            if not any(item.get("id") == mapping_id
                       for item in release_mappings):
                raise AssertionError("release snapshot lost the remote mapping")
            state["releaseId"] = release_id
            print(
                "[recap-e2e] published: "
                f"ontologyId={ontology_id} releaseId={release_id} "
                f"objectTypeId={object_type_id} assetId={asset['id']} "
                f"datasetId={dataset_id}",
                flush=True,
            )
            return {
                "ontologyId": ontology_id,
                "v0ReleaseId": state["v0ReleaseId"],
                "releaseId": release_id,
                "objectTypeId": object_type_id,
                "assetId": asset["id"],
                "datasetId": dataset_id,
                "mappingId": mapping_id,
                "draftId": draft_id,
                "trialRunId": trial_id,
                "trialStatus": trial.get("status"),
                "impactHash": impact_hash,
                "mappingCount": len(release_mappings),
                "properties": len(saved.get("properties") or []),
                "publishPath": (
                    "POST /api/v1/ontologies -> PUT /full -> "
                    "POST /integrated-datasets -> POST /mappings(remote) -> "
                    "drafts -> workspace(+mappings) -> trial-runs -> promote"
                ),
            }

        publish_details = recorder.run(
            "publish_remote_release_frontdoor", publish_case)
        report["publish"] = publish_details
        ontology_id = state["ontologyId"]
        release_id = state["releaseId"]
        model_id = str(model_id)
        run_ctx = {
            "ontologyId": ontology_id,
            "releaseId": release_id,
            "modelId": model_id,
            "modelOrigin": _origin(model_base),
            "nonce": nonce,
            "timeout": float(args.timeout_seconds),
        }

        recorder.run("describe_47_mapped_properties",
                     lambda: _case_describe(api, run_ctx))
        recorder.run("default_projection_bounded_32_of_47",
                     lambda: _case_projection(api, run_ctx))
        for bucket in BUCKETS:
            recorder.run(
                f"bucket_{bucket}_sorted_asc",
                lambda bucket=bucket: _case_bucket(
                    api, run_ctx, bucket, oracle),
            )
        recorder.run("top10_source_limit_and_preview",
                     lambda: _case_top10(api, run_ctx, oracle))
        recorder.run("zero_result_with_coverage",
                     lambda: _case_zero(api, run_ctx))
        recorder.run("distinct_users_absolute_window",
                     lambda: _case_distinct_window(api, run_ctx, oracle))
        recorder.run("distinct_users_per_module_september",
                     lambda: _case_distinct_module(api, run_ctx, oracle))

        window30_status, window30_details = _case_window30(
            api, run_ctx, oracle)
        if window30_status == "passed":
            window30_guard = recorder.run(
                "window30_capability_guard",
                lambda: _case_window30_guard(api, run_ctx),
            )
        else:
            # The guard only makes sense once the governed expression exists;
            # without it the success case is already blocked, not passed.
            window30_guard = {
                "status": "skipped",
                "reason": "window_distinct_unavailable",
            }
        report["window30"] = {
            "status": window30_status,
            "governedContractProvided": bool(args.window30_contract),
            "expression": (
                "analyze_sequences mode=window_distinct window_minutes=30"
                if not args.window30_contract else "external contract"
            ),
            "oracle": oracle.report()["window30"],
            "details": window30_details,
            "guard": window30_guard,
            "note": (
                "The 30-minute fixed-window question is a formal success "
                "case checked against the local oracle.  The capability "
                "guard only proves that other window granularities are "
                "rejected explicitly and never approximated."
            ),
        }

        conversation_routes = []
        for case in recorder.cases:
            details = case.get("details") or {}
            conversation_id = details.get("conversationId")
            if not conversation_id:
                continue
            conversation_routes.append({
                "caseId": case.get("id"),
                "conversationId": conversation_id,
                "hashRoute": (
                    f"#/agent?ontology_id={ontology_id}"
                    f"&conversation_id={conversation_id}"
                ),
            })
        window30_conversation = window30_details.get("conversationId")
        if window30_conversation and all(
                item["conversationId"] != window30_conversation
                for item in conversation_routes):
            conversation_routes.append({
                "caseId": "window30_fixed_30min",
                "conversationId": window30_conversation,
                "hashRoute": (
                    f"#/agent?ontology_id={ontology_id}"
                    f"&conversation_id={window30_conversation}"
                ),
            })
        conversation_ids = [
            item["conversationId"] for item in conversation_routes
        ]
        primary_route = conversation_routes[0] if conversation_routes else None
        report["browser"] = {
            "ontologyId": ontology_id,
            "releaseId": release_id,
            "conversationIds": conversation_ids,
            "conversationRoutes": conversation_routes,
            "hashRouteTemplate": (
                f"#/agent?ontology_id={ontology_id}"
                "&conversation_id=<conversationId>"
            ),
            "primaryConversationId": (
                primary_route["conversationId"] if primary_route else None),
            "primaryHashRoute": (
                primary_route["hashRoute"] if primary_route else None),
            "adminUser": args.admin_user,
            "note": (
                "Open the Hash route in the authenticated frontend to "
                "inspect the persisted conversations and tool steps."
            ),
        }
        if model_state is not None:
            report["modelStubStats"] = model_state.snapshot()
        else:
            try:
                with urlrequest.urlopen(
                    model_origin + "/__fixture/stats", timeout=3.0,
                ) as response:
                    stats = _decode_json(response.read())
                if isinstance(stats, dict):
                    report["modelStubStats"] = stats
            except Exception:  # noqa: BLE001 - evidence only
                pass
        if any(case.get("status") == "failed" for case in recorder.cases):
            report["status"] = "failed"
        elif window30_status == "passed":
            report["status"] = "passed"
        elif window30_status == "blocked_by_capability":
            # Every executed case passed, but the recap's 30-minute window
            # question has no governed expression yet.  Do not call the
            # overall acceptance complete.
            report["status"] = "passed_with_capability_gap"
        else:
            report["status"] = "failed"
    except Exception as exc:  # noqa: BLE001 - evidence then exit nonzero
        failure = exc
        report["status"] = "failed"
        report["failures"].append(
            f"{type(exc).__name__}: {redact(exc)[:1200]}")
        if state.get("ontologyId"):
            report["partialState"] = {
                key: state.get(key) for key in (
                    "ontologyId", "v0ReleaseId", "releaseId", "objectTypeId",
                    "assetId", "datasetId", "mappingId", "draftId",
                    "trialId",
                ) if state.get(key)
            }
    finally:
        if model_server is not None:
            model_server.shutdown()
            model_server.server_close()
        if model_id and not args.keep_model_config and api.token:
            try:
                status, _ = api.raw(
                    "DELETE", f"/api/v1/models/{model_id}", timeout=10.0)
                report["modelConfigDeleted"] = status in (204, 200, 404)
            except Exception as exc:  # noqa: BLE001 - cleanup only
                report["modelConfigDeleted"] = False
                report["cleanup"]["modelConfigError"] = (
                    f"{type(exc).__name__}: {redact(exc)[:200]}")
        report["finishedAt"] = iso_now()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(args.report, 0o600)
        except OSError:
            pass
        print(f"[recap-e2e] report: {args.report}", flush=True)
        if failure is not None:
            print(
                f"[recap-e2e] FAILED: {type(failure).__name__}: "
                f"{redact(failure)[:800]}",
                file=sys.stderr, flush=True,
            )
    return 0 if failure is None else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.serve_model:
        return serve_model(args)
    if os.environ.get(SENTINEL) != "1":
        parser.error(
            f"{SENTINEL}=1 is required (isolated synthetic fixture only)")
    if os.environ.get("ENVIRONMENT") != "test":
        parser.error("ENVIRONMENT=test is required (no production writes)")
    password = args.admin_password or os.getenv(FIXTURE_ADMIN_PASSWORD_ENV)
    if not password:
        parser.error(
            f"--admin-password or {FIXTURE_ADMIN_PASSWORD_ENV} is required")
    try:
        validate_loopback_url(args.base_url, allow_v1_path=False)
        validate_loopback_url(args.model_base_url, allow_v1_path=True)
        contract = load_window30_contract(args.window30_contract)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    global _WINDOW30_CONTRACT
    _WINDOW30_CONTRACT = contract
    return run_driver(args, password)


if __name__ == "__main__":
    raise SystemExit(main())
