#!/usr/bin/env python3
"""Loopback ontology-assistant runtime E2E driver.

Backend/PostgreSQL/NATS/query are real HTTP; only the LLM HTTP boundary is
faked by ``--serve-model``. Evidence: .artifacts/runtime-e2e/report.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = REPO_ROOT / ".artifacts" / "runtime-e2e" / "report.json"
DEFAULT_BASE_URL = "http://127.0.0.1:18036"
DEFAULT_MODEL_BASE_URL = "http://127.0.0.1:18037/v1"

WINDOW_TIMEZONE = "Asia/Shanghai"
WINDOW_LOCAL_START = "2026-10-01T00:00:00"
WINDOW_LOCAL_END = "2026-10-02T00:00:00"
WINDOW_START = datetime(2026, 9, 30, 16, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 10, 1, 16, 0, 0, tzinfo=timezone.utc)
WINDOW_START_ISO = WINDOW_START.isoformat()
WINDOW_END_ISO = WINDOW_END.isoformat()

FIXTURE_ROW_COUNT = 128
TERMINAL_RUN_STATUSES = {"succeeded", "error", "cancelled", "interrupted"}
ACTIVE_RUN_STATUSES = {"queued", "running"}
MODEL_NAME = "fixture-runtime-model"
FIXTURE_API_KEY = "sk-fixture-runtime-e2e"

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
        return {"detail": text[:1000]}


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
    return redact(detail)[:400]


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

    def open_sse(self, path: str, *, timeout: float) -> Any:
        headers = {"Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urlrequest.Request(
            self.base_url + path, headers=headers, method="GET",
        )
        try:
            return urlrequest.urlopen(request, timeout=timeout)
        except urlerror.HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            raise ApiFailure("GET", path, exc.code, _decode_json(raw)) from exc


def iter_sse(response: Any, *, deadline: float) -> Iterator[dict]:
    buffered: list[str] = []
    while time.monotonic() < deadline:
        line = response.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text == "":
            if buffered:
                payload = "\n".join(buffered)
                buffered = []
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
            continue
        if text.startswith(":"):
            continue
        if text.startswith("data:"):
            buffered.append(text[5:].lstrip())
    if buffered:
        try:
            event = json.loads("\n".join(buffered))
        except json.JSONDecodeError:
            return
        if isinstance(event, dict):
            yield event


def read_sse(
    api: Api,
    path: str,
    *,
    timeout: float,
    stop_on_done: bool = True,
) -> list[dict]:
    deadline = time.monotonic() + timeout
    events: list[dict] = []
    response = api.open_sse(path, timeout=timeout)
    try:
        for event in iter_sse(response, deadline=deadline):
            events.append(event)
            if stop_on_done and event.get("type") == "done":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("SSE subscription did not finish before timeout")
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - close only
            pass
    return events


@dataclass(frozen=True)
class FixtureEvent:
    event_id: str
    actor: str
    action: str
    when: datetime
    region: str | None

    def properties(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "event_id": self.event_id,
            "actor": self.actor,
            "action": self.action,
            "occurred_at": self.when.astimezone(timezone.utc).isoformat(),
        }
        if self.region is not None:
            values["region"] = self.region
        return values


def _event(event_id: str, actor: str, action: str,
           when: datetime, region: str | None) -> FixtureEvent:
    return FixtureEvent(
        event_id=event_id, actor=actor, action=action, when=when,
        region=region,
    )


def fixture_events() -> list[FixtureEvent]:
    start = WINDOW_START
    end = WINDOW_END
    events: list[FixtureEvent] = []
    for index in range(10):
        region = None if index == 9 else ("north" if index % 2 == 0 else "south")
        events.append(_event(
            f"ev-register-{index:02d}", f"u{index + 1:02d}", "register",
            start + timedelta(minutes=index + 1), region,
        ))
    events.append(_event(
        "ev-register-dup", "u01", "register",
        start + timedelta(minutes=1), "north",
    ))
    events.append(_event(
        "ev-boundary-start", "u11", "boundary", start, "north"))
    events.append(_event(
        "ev-boundary-before", "u11", "boundary",
        start - timedelta(seconds=1), "north"))
    events.append(_event(
        "ev-boundary-end", "u12", "boundary", end, "south"))
    events.append(_event(
        "ev-boundary-after", "u12", "boundary",
        end + timedelta(seconds=1), "south"))
    filler_regions = (["north"] * 49) + (["south"] * 49) + ([None] * 15)
    for index, region in enumerate(filler_regions):
        action = "read" if index % 2 == 0 else "approve"
        events.append(_event(
            f"ev-fill-{index:03d}", f"f{index + 1:03d}", action,
            start + timedelta(minutes=index + 1), region,
        ))
    if len(events) != FIXTURE_ROW_COUNT:
        raise RuntimeError(
            f"fixture generator produced {len(events)} rows, "
            f"expected {FIXTURE_ROW_COUNT}")
    return events


def _region_label(value: str | None) -> str:
    return "null" if value is None else str(value)


@dataclass
class Oracle:
    events: list[FixtureEvent]
    included: list[FixtureEvent]
    detail_ids: list[str]
    region_counts: dict[str, int]
    read_region_counts: dict[str, int]
    sequence_distinct: int
    sequence_candidates: int
    sequence_thresholds: dict[str, bool]
    boundary: dict[str, bool]

    def report(self) -> dict[str, Any]:
        return {
            "rows": len(self.events),
            "includedRows": len(self.included),
            "excludedRows": len(self.events) - len(self.included),
            "boundary": self.boundary,
            "detailIdsSha256": hashlib.sha256(
                "\n".join(self.detail_ids).encode("utf-8")).hexdigest(),
            "sequence": {
                "registerRows": self.sequence_candidates,
                "distinctSubjects": self.sequence_distinct,
                "thresholds": self.sequence_thresholds,
            },
            "regionCounts": self.region_counts,
            "readRegionCounts": self.read_region_counts,
        }


def build_oracle(events: list[FixtureEvent]) -> Oracle:
    included = [event for event in events if WINDOW_START <= event.when < WINDOW_END]
    included_ids = {event.event_id for event in included}
    detail_ids = sorted(event.event_id for event in included)
    region_counts = Counter(_region_label(event.region) for event in included)
    read_counts = Counter(
        _region_label(event.region)
        for event in included if event.action == "read"
    )
    register_rows = [event for event in included if event.action == "register"]
    distinct = len({event.actor for event in register_rows})
    boundary = {
        "startIncluded": "ev-boundary-start" in included_ids,
        "beforeExcluded": "ev-boundary-before" not in included_ids,
        "endExcluded": "ev-boundary-end" not in included_ids,
        "afterExcluded": "ev-boundary-after" not in included_ids,
    }
    if not all(boundary.values()):
        raise RuntimeError("fixed-window boundary oracle is inconsistent")
    if distinct != 10 or len(register_rows) != 11:
        raise RuntimeError("sequence oracle expected 10 distinct / 11 rows")
    return Oracle(
        events=events,
        included=included,
        detail_ids=detail_ids,
        region_counts=dict(region_counts),
        read_region_counts={
            "north": int(read_counts.get("north", 0)),
            "south": int(read_counts.get("south", 0)),
        },
        sequence_distinct=distinct,
        sequence_candidates=len(register_rows),
        sequence_thresholds={
            "9": distinct >= 9,
            "10": distinct >= 10,
            "11": distinct >= 11,
        },
        boundary=boundary,
    )


def build_import_package(name: str) -> dict[str, Any]:
    return {
        "format": "ontology-structure",
        "formatVersion": 1,
        "exportedAt": "2026-11-03T00:00:00+00:00",
        "ontology": {
            "name": name,
            "domain": "runtime-e2e",
            "description": "Synthetic isolated runtime E2E fixture ontology",
            "icon": "network",
            "sourceVersion": "fixture-v1",
            "sourceStatus": "published",
        },
        "structure": {
            "objectTypes": [{
                "id": "fixture-object-event",
                "name": "Event",
                "displayName": "事件",
                "description": "Runtime E2E deterministic event fixture",
                "primaryKey": "event_id",
                "properties": [
                    {"id": "fixture-prop-event-id", "name": "event_id",
                     "displayName": "事件号", "type": "string", "required": True},
                    {"id": "fixture-prop-actor", "name": "actor",
                     "displayName": "登记人", "type": "string"},
                    {"id": "fixture-prop-action", "name": "action",
                     "displayName": "动作", "type": "string"},
                    {"id": "fixture-prop-occurred-at", "name": "occurred_at",
                     "displayName": "发生时间", "type": "datetime"},
                    {"id": "fixture-prop-region", "name": "region",
                     "displayName": "区域", "type": "string"},
                ],
                "interfaces": [],
                "positionX": 0.0,
                "positionY": 0.0,
            }],
            "linkTypes": [],
            "actions": [],
            "functions": [],
            "sentinels": [],
        },
        "canvasLayout": {},
    }


def _window_argument() -> dict[str, Any]:
    return {
        "property": "occurred_at",
        "timezone": WINDOW_TIMEZONE,
        "start": WINDOW_LOCAL_START,
        "end": WINDOW_LOCAL_END,
    }


GATE_TIMEOUT_SECONDS = 300.0


class StubState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._total = 0
        self._gates: dict[str, dict[str, Any]] = {}

    def record(self, scenario: str) -> None:
        with self._lock:
            self._counts[scenario] += 1
            self._total += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "requests": self._total,
                "byScenario": dict(sorted(self._counts.items())),
            }

    def reset(self) -> None:
        with self._lock:
            gates = list(self._gates.values())
            self._gates.clear()
            self._counts.clear()
            self._total = 0
        for gate in gates:
            gate["event"].set()

    def scenario_count(self, scenario: str) -> int:
        with self._lock:
            return int(self._counts.get(scenario, 0))

    def arm_gate(self, name: str) -> None:
        with self._lock:
            self._gates[name] = {
                "event": threading.Event(), "armed": True, "waiting": 0,
            }

    def release_gate(self, name: str) -> bool:
        with self._lock:
            gate = self._gates.get(name)
            if gate is None:
                return False
            gate["armed"] = False
        gate["event"].set()
        return True

    def release_all_gates(self) -> None:
        with self._lock:
            gates = list(self._gates.values())
            self._gates.clear()
        for gate in gates:
            gate["event"].set()

    def wait_gate(self, name: str,
                  timeout: float = GATE_TIMEOUT_SECONDS) -> bool:
        with self._lock:
            gate = self._gates.get(name)
            if gate is None or not gate["armed"]:
                return True
            gate["waiting"] += 1
            event = gate["event"]
        try:
            return event.wait(timeout)
        finally:
            with self._lock:
                gate["waiting"] = max(0, gate["waiting"] - 1)

    def gate_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "gates": {
                    name: {
                        "armed": bool(gate["armed"]),
                        "waiting": int(gate["waiting"]),
                        "released": gate["event"].is_set(),
                    }
                    for name, gate in self._gates.items()
                },
            }


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


def stub_scenario(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if "Connectivity check" in text:
            return "connectivity"
        match = re.search(r"fixture\.([A-Za-z0-9_.-]+)", text)
        if match:
            return match.group(1)
        return "unrecognized"
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


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_fixture_{uuid.uuid4().hex[:16]}",
        "name": name,
        "arguments": arguments,
    }


def stub_plan(scenario: str, has_tool_result: bool) -> dict[str, Any]:
    base = scenario.split(".", 1)[0]
    if scenario == "connectivity":
        return {"before": [("PONG", 0.0)], "gate": None, "after": [],
                "tool_call": None}
    if scenario == "cancel":
        return {"before": [("fixture-cancel-", 0.0)], "gate": "cancel",
                "after": [("ok", 0.0)], "tool_call": None}
    if scenario == "disconnect":
        if has_tool_result:
            return {"before": [("fixture-disconnect-", 0.0)],
                    "gate": "disconnect", "after": [("ok", 0.0)],
                    "tool_call": None}
        return {
            "before": [("fixture-disconnect-tool", 0.0)], "gate": None,
            "after": [],
            "tool_call": _tool_call("query_objects", {
                "object_type": "Event",
                "select": ["event_id"],
                "page_size": 5,
                "window": _window_argument(),
            }),
        }
    if scenario == "idempotent":
        return {"before": [("fixture-idempotent-", 0.0)],
                "gate": "idempotent", "after": [("ok", 0.0)],
                "tool_call": None}
    if base == "detail":
        if has_tool_result:
            return {"before": [("fixture-detail-ok", 0.0)], "gate": None,
                    "after": [], "tool_call": None}
        return {
            "before": [("fixture-detail-tool", 0.0)], "gate": None,
            "after": [],
            "tool_call": _tool_call("query_objects", {
                "object_type": "Event",
                "select": ["event_id", "actor", "action", "occurred_at",
                           "region"],
                "order_by": [{"output": "event_id", "direction": "asc"}],
                "page_size": 17,
                "window": _window_argument(),
            }),
        }
    if base == "stats":
        if has_tool_result:
            return {"before": [("fixture-stats-ok", 0.0)], "gate": None,
                    "after": [], "tool_call": None}
        return {
            "before": [("fixture-stats-tool", 0.0)], "gate": None,
            "after": [],
            "tool_call": _tool_call("query_objects", {
                "object_type": "Event",
                "group_by": [{"property": "region"}],
                "aggregates": [{"fn": "count"}],
                "page_size": 50,
                "window": _window_argument(),
            }),
        }
    if base == "sequence":
        if has_tool_result:
            return {"before": [("fixture-sequence-ok", 0.0)], "gate": None,
                    "after": [], "tool_call": None}
        suffix = scenario.rsplit(".", 1)[-1]
        try:
            threshold = int(suffix)
        except ValueError:
            threshold = 10
        return {
            "before": [("fixture-sequence-tool", 0.0)], "gate": None,
            "after": [],
            "tool_call": _tool_call("analyze_sequences", {
                "object_type": "Event",
                "mode": "distinct",
                "subject_property": "actor",
                "time_property": "occurred_at",
                "threshold": threshold,
                "filters": [{"property": "action", "op": "eq",
                             "value": "register"}],
                "window": _window_argument(),
            }),
        }
    if base == "concurrent":
        region = "north" if scenario.endswith(".A") else "south"
        gate = f"concurrent.{'A' if scenario.endswith('.A') else 'B'}"
        if has_tool_result:
            return {"before": [("fixture-concurrent-ok", 0.0)], "gate": None,
                    "after": [], "tool_call": None}
        return {
            "before": [("fixture-concurrent-tool", 0.0)], "gate": gate,
            "after": [],
            "tool_call": _tool_call("query_objects", {
                "object_type": "Event",
                "filters": [
                    {"property": "action", "op": "eq", "value": "read"},
                    {"property": "region", "op": "eq", "value": region},
                ],
                "select": ["event_id", "region"],
                "order_by": [{"output": "event_id", "direction": "asc"}],
                "page_size": 20,
                "window": _window_argument(),
            }),
        }
    return {"before": [("fixture-ok", 0.0)], "gate": None, "after": [],
            "tool_call": None}


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
        "id": "chatcmpl-fixture",
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


def _completion_message(plan: dict[str, Any]) -> tuple[str, list[dict] | None]:
    content = "".join(
        text for text, _delay in (*plan["before"], *plan["after"]))
    tool = plan["tool_call"]
    tool_calls = None
    if tool:
        tool_calls = [{
            "id": tool["id"],
            "type": "function",
            "function": {
                "name": tool["name"],
                "arguments": json.dumps(
                    tool["arguments"], ensure_ascii=False, separators=(",", ":")),
            },
        }]
    return content, tool_calls


def _stream_completion(handler: BaseHTTPRequestHandler, model: str,
                       plan: dict[str, Any], state: StubState) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    first = True

    def send_chunks(chunks: list[tuple[str, float]]) -> None:
        nonlocal first
        for text, delay in chunks:
            delta: dict[str, Any] = {"content": text}
            if first:
                delta["role"] = "assistant"
                first = False
            _sse_chunk(handler, model, delta)
            if delay:
                time.sleep(delay)

    send_chunks(plan["before"])
    if plan.get("gate"):
        state.wait_gate(plan["gate"])
    send_chunks(plan["after"])
    tool = plan["tool_call"]
    finish_reason = "stop"
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
        finish_reason = "tool_calls"
    _sse_chunk(handler, model, {}, finish_reason=finish_reason)
    _sse_chunk(handler, model, {}, usage={
        "prompt_tokens": 12, "completion_tokens": 4,
    })
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def _non_stream_completion(handler: BaseHTTPRequestHandler, model: str,
                           plan: dict[str, Any], state: StubState) -> None:
    if plan.get("gate"):
        state.wait_gate(plan["gate"])
    content, tool_calls = _completion_message(plan)
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    _json_response(handler, 200, {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4,
                  "total_tokens": 16},
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
                    "ok": True, "mode": "openai-compatible-model-stub",
                })
                return
            if path == "/__fixture/stats":
                _json_response(self, 200, state.snapshot())
                return
            if path == "/__fixture/gate":
                _json_response(self, 200, state.gate_status())
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
            if path == "/__fixture/reset":
                state.reset()
                _json_response(self, 200, {"ok": True, "reset": True})
                return
            if path == "/__fixture/gate/release-all":
                state.release_all_gates()
                _json_response(self, 200, {"ok": True})
                return
            if path in {"/__fixture/gate/arm", "/__fixture/gate/release"}:
                body = self._read_json()
                name = str(body.get("name") or "").strip()
                if not name:
                    _json_response(self, 422, {
                        "error": {"message": "gate name required"}})
                    return
                if path.endswith("/arm"):
                    state.arm_gate(name)
                    _json_response(self, 200, {"ok": True, "armed": name})
                else:
                    released = state.release_gate(name)
                    _json_response(self, 200, {
                        "ok": True, "released": released, "name": name})
                return
            if path not in {"/v1/chat/completions", "/chat/completions"}:
                _json_response(self, 404, {
                    "error": {"message": "fixture_not_found"}})
                return
            body = self._read_json()
            messages = body.get("messages")
            if not isinstance(messages, list):
                messages = []
            scenario = stub_scenario(messages)
            state.record(scenario)
            plan = stub_plan(scenario, _has_tool_result_after_last_user(messages))
            model = str(body.get("model") or MODEL_NAME)
            try:
                if bool(body.get("stream")):
                    _stream_completion(self, model, plan, state)
                else:
                    _non_stream_completion(self, model, plan, state)
            except (BrokenPipeError, ConnectionResetError):
                return

    return ModelStubHandler


def serve_model(args: argparse.Namespace) -> int:
    if args.host != "127.0.0.1":
        print("[model-stub] ERROR: host must be 127.0.0.1 (loopback only)",
              file=sys.stderr)
        return 2
    if args.port < 0 or args.port > 65535:
        print("[model-stub] ERROR: port must be 0..65535", file=sys.stderr)
        return 2
    state = StubState()
    server = ThreadingHTTPServer((args.host, args.port), make_stub_handler(state))
    server.daemon_threads = True
    bound_port = server.server_address[1]
    print(
        f"[model-stub] listening http://{args.host}:{bound_port}/v1 "
        f"(fixture model: {MODEL_NAME}; health: "
        f"http://{args.host}:{bound_port}/__fixture/health)",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("[model-stub] stopping", flush=True)
    finally:
        server.server_close()
    return 0


class CaseRecorder:
    def __init__(self) -> None:
        self.cases: list[dict[str, Any]] = []

    def run(self, case_id: str, fn: Callable[[], dict[str, Any] | None],
            ) -> dict[str, Any]:
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
                "error": f"{type(exc).__name__}: {redact(exc)[:600]}",
            })
            raise
        self.cases.append({
            "id": case_id,
            "status": "passed",
            "startedAt": started_at,
            "durationMs": int((time.monotonic() - started) * 1000),
            "details": details,
        })
        return details


def _run_path(ontology_id: str, run_id: str) -> str:
    return (
        f"/api/v2/formal/ontologies/{ontology_id}"
        f"/agent/chat/runs/{run_id}"
    )


def _conversation_path(ontology_id: str, conversation_id: str) -> str:
    return (
        f"/api/v2/formal/ontologies/{ontology_id}"
        f"/agent/conversations/{conversation_id}"
    )


def _query_view_path(ontology_id: str) -> str:
    return f"/api/v2/formal/ontologies/{ontology_id}/agent/query-view"


def _submit_run(
    api: Api,
    ontology_id: str,
    release_id: str,
    model_id: str,
    *,
    run_id: str,
    message: str,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message": message,
        "runId": run_id,
        "modelId": model_id,
        "releaseId": release_id,
        "stream": True,
    }
    if conversation_id:
        body["conversationId"] = conversation_id
    payload = api.ok(
        "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/chat/runs",
        body, expected=(200,),
    )
    data = payload["data"]
    if data.get("releaseId") != release_id:
        raise AssertionError(
            f"submitted run froze release {data.get('releaseId')!r}, "
            f"expected {release_id!r}")
    _track_run(data["runId"])
    return data


def _get_run(api: Api, ontology_id: str, run_id: str) -> dict[str, Any]:
    return api.ok(
        "GET", _run_path(ontology_id, run_id),
    )["data"]


def _wait_run(
    api: Api,
    ontology_id: str,
    run_id: str,
    *,
    timeout: float,
    terminal: set[str] | None = None,
) -> dict[str, Any]:
    terminal = terminal or TERMINAL_RUN_STATUSES
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _get_run(api, ontology_id, run_id)
        if last["status"] in terminal:
            return last
        time.sleep(0.2)
    status = (last or {}).get("status")
    hint = (
        " (still queued: is the NATS executor running via executor-up?)"
        if status == "queued" else ""
    )
    raise TimeoutError(
        f"run {run_id} did not reach a terminal state before timeout; "
        f"last status={status}{hint}")


def _conversation(api: Api, ontology_id: str,
                  conversation_id: str) -> dict[str, Any]:
    return api.ok(
        "GET", _conversation_path(ontology_id, conversation_id),
    )["data"]


def _messages(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    return list(conversation.get("messages") or [])


def _count_roles(messages: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(message.get("role")) for message in messages)


def _assert_query_window(query_spec: dict[str, Any]) -> None:
    resolved = query_spec.get("resolvedWindow") or {}
    if not isinstance(resolved, dict):
        raise AssertionError("querySpec has no resolvedWindow")
    if resolved.get("startUtc") != WINDOW_START_ISO:
        raise AssertionError(
            f"querySpec startUtc {resolved.get('startUtc')!r} "
            f"!= {WINDOW_START_ISO!r}")
    if resolved.get("endUtc") != WINDOW_END_ISO:
        raise AssertionError(
            f"querySpec endUtc {resolved.get('endUtc')!r} "
            f"!= {WINDOW_END_ISO!r}")
    if resolved.get("exclusiveEnd") is not True:
        raise AssertionError("querySpec window is not right-open")
    query = query_spec.get("query") or {}
    leaves = {
        (leaf.get("op"), leaf.get("value"))
        for leaf in (query.get("where") or [])
        if isinstance(leaf, dict)
    }
    if ("gte", WINDOW_START_ISO) not in leaves:
        raise AssertionError("querySpec lacks the frozen gte start filter")
    if ("lt", WINDOW_END_ISO) not in leaves:
        raise AssertionError("querySpec lacks the frozen lt end filter")


def _query_view_pages(
    api: Api,
    ontology_id: str,
    conversation_id: str,
    *,
    query_spec: dict[str, Any] | None = None,
    drilldown: str | None = None,
    page_size: int = 17,
    cursor: str | None = None,
) -> dict[str, Any]:
    path = _query_view_path(ontology_id)
    body: dict[str, Any] = {"conversationId": conversation_id}
    if cursor:
        if query_spec is None:
            raise AssertionError("query-view cursor requires querySpec")
        body["querySpec"] = query_spec
        body["cursor"] = cursor
    else:
        if query_spec is None:
            raise AssertionError("first query-view page requires querySpec")
        body["querySpec"] = query_spec
        if drilldown is not None:
            body["drilldown"] = drilldown
        body["pageSize"] = page_size
    first = api.ok("POST", path, body)["data"]
    continuation_spec = first.get("querySpec") or query_spec
    rows = list(first.get("rows") or [])
    pages = 1
    seen_cursors: set[str] = set()
    current = first
    next_cursor = first.get("cursor") if first.get("hasNext") else None
    while current.get("hasNext"):
        if not next_cursor:
            raise AssertionError("query-view hasNext without a cursor")
        if next_cursor in seen_cursors:
            raise AssertionError("query-view repeated a cursor")
        seen_cursors.add(next_cursor)
        pages += 1
        if pages > 50:
            raise AssertionError("query-view pagination exceeded 50 pages")
        current = api.ok("POST", path, {
            "conversationId": conversation_id,
            "querySpec": continuation_spec,
            "cursor": next_cursor,
        })["data"]
        continuation_spec = current.get("querySpec") or continuation_spec
        page_rows = list(current.get("rows") or [])
        if page_size and len(page_rows) > page_size:
            raise AssertionError("query-view returned more rows than pageSize")
        rows.extend(page_rows)
        next_cursor = current.get("cursor") if current.get("hasNext") else None
    return {
        "rows": rows,
        "pages": pages,
        "returned": len(rows),
        "totalRowCount": (
            int(first["totalRowCount"])
            if first.get("totalRowCount") is not None else None
        ),
        "released": bool(current.get("released")),
        "resolvedWindow": current.get("resolvedWindow")
        or first.get("resolvedWindow"),
        "firstCursor": first.get("cursor"),
    }


_TRACKED_RUN_IDS: list[str] = []


def _track_run(run_id: str) -> None:
    if run_id and run_id not in _TRACKED_RUN_IDS:
        _TRACKED_RUN_IDS.append(run_id)


def _gate_arm(stub: Api, name: str) -> None:
    stub.ok("POST", "/__fixture/gate/arm", {"name": name})


def _gate_release(stub: Api, name: str) -> None:
    stub.ok("POST", "/__fixture/gate/release", {"name": name})


def _gate_release_all(stub: Api) -> None:
    stub.ok("POST", "/__fixture/gate/release-all", {}, timeout=2.0)


def _wait_gate_waiting(stub: Api, name: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = stub.ok("GET", "/__fixture/gate")
        info = (payload.get("gates") or {}).get(name) or {}
        if int(info.get("waiting") or 0) >= 1:
            return
        if not info.get("armed"):
            raise AssertionError(f"gate {name!r} is not armed")
        time.sleep(0.05)
    raise TimeoutError(f"gate {name!r} was not reached before timeout")


def _read_first_sse(api: Api, path: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    response = api.open_sse(path, timeout=timeout)
    try:
        for event in iter_sse(response, deadline=deadline):
            return event
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - close only
            pass
    raise AssertionError("SSE subscription produced no frame")


def _cleanup_tracked_runs(api: Api, ontology_id: str | None) -> dict[str, Any]:
    if not api.token or not ontology_id:
        return {"tracked": len(_TRACKED_RUN_IDS), "cancelled": []}
    cancelled: list[dict[str, Any]] = []
    for run_id in list(_TRACKED_RUN_IDS):
        try:
            status, body = api.raw(
                "GET", _run_path(ontology_id, run_id), timeout=5.0)
            snapshot = (body or {}).get("data") or {}
            current = snapshot.get("status")
            if status == 200 and current in ACTIVE_RUN_STATUSES:
                cancel_status, _ = api.raw(
                    "POST", _run_path(ontology_id, run_id) + "/cancel",
                    timeout=5.0)
                cancelled.append({
                    "runId": run_id, "status": current,
                    "cancelHttp": cancel_status,
                })
        except Exception as exc:  # noqa: BLE001 - cleanup only
            cancelled.append({
                "runId": run_id,
                "error": f"{type(exc).__name__}: {redact(exc)[:200]}",
            })
    return {"tracked": len(_TRACKED_RUN_IDS), "cancelled": cancelled}


def _case_api_live(api: Api) -> dict[str, Any]:
    live = api.ok("GET", "/health/live")
    try:
        ready_status, _ready = api.raw("GET", "/health/ready")
    except Exception:  # noqa: BLE001 - advisory only
        ready_status = None
    return {"healthLive": 200, "healthReadyStatus": ready_status,
            "liveBody": live if isinstance(live, dict) else None}


def _case_model_stub(stub: Api) -> dict[str, Any]:
    health = stub.ok("GET", "/__fixture/health")
    stub.ok("POST", "/__fixture/reset", {})
    gates = stub.ok("GET", "/__fixture/gate")
    if not isinstance(gates, dict) or "gates" not in gates:
        raise AssertionError("model stub does not expose the gate control")
    return {"health": bool(health and health.get("ok")),
            "gateEndpoint": True, "statsReset": True}


def _login(api: Api, user: str, password: str) -> dict[str, Any]:
    payload = api.ok("POST", "/api/v1/auth/login", {
        "username": user, "password": password,
    })
    data = payload.get("data") or {}
    token = data.get("access_token")
    if not token:
        raise AssertionError("login response did not include access_token")
    _register_secret(token)
    api.token = token
    role = (data.get("user") or {}).get("role")
    return {"authenticated": True, "role": role or "admin_fixture"}


def _publish_ontology(
    api: Api,
    oracle: Oracle,
    *,
    name: str,
    nonce: str,
) -> dict[str, Any]:
    package = build_import_package(name)
    payload = api.ok(
        "POST", "/api/v1/ontologies/import", package, expected=(201,),
    )
    data = payload.get("data") or {}
    ontology = data.get("ontology") or {}
    version = data.get("version") or {}
    ontology_id = ontology.get("id")
    release_id = version.get("id")
    if not ontology_id or not release_id:
        raise AssertionError("import response lacks ontology/version ids")
    if ontology.get("status") != "published":
        raise AssertionError("imported ontology is not published")
    if ontology.get("current_release_id") != release_id:
        raise AssertionError("imported ontology release pointer mismatch")
    if version.get("node_kind") != "release" or \
            version.get("lifecycle_status") != "released":
        raise AssertionError("imported v0 is not an immutable release")

    versions = api.ok(
        "GET", f"/api/v2/ontologies/{ontology_id}/versions",
    )
    if versions.get("current_release_id") != release_id:
        raise AssertionError("versions endpoint current_release_id mismatch")
    release_nodes = [
        item for item in (versions.get("data") or [])
        if item.get("id") == release_id
    ]
    if not release_nodes:
        raise AssertionError("versions endpoint does not list the release")
    node = release_nodes[0]
    if node.get("node_kind") != "release" or \
            node.get("lifecycle_status") != "released":
        raise AssertionError("listed release node has wrong lifecycle")

    workspace = api.ok(
        "GET",
        f"/api/v2/ontologies/{ontology_id}/current-release/workspace",
    )["data"]
    object_type_names = {
        item.get("name") for item in (workspace.get("objectTypes") or [])
    }
    if "Event" not in object_type_names:
        raise AssertionError("published workspace lacks the Event type")

    full = api.ok(
        "GET", f"/api/v2/formal/ontologies/{ontology_id}/full",
    )["data"]
    event_type = next(
        (item for item in (full.get("objectTypes") or [])
         if item.get("name") == "Event"),
        None,
    )
    if not event_type or not event_type.get("id"):
        raise AssertionError("full ontology lacks the Event object type id")
    object_type_id = event_type["id"]
    print(
        "[runtime-e2e] fixture ontology created: "
        f"ontologyId={ontology_id} releaseId={release_id} "
        f"version={version.get('version_number')}",
        flush=True,
    )

    release_tagged = 0
    for event in oracle.events:
        body = {
            "id": f"inst-{nonce}-{event.event_id}",
            "objectTypeId": object_type_id,
            "properties": event.properties(),
            "source": "manual",
        }
        created = api.ok(
            "POST",
            f"/api/v2/formal/ontologies/{ontology_id}/instances",
            body, expected=(201,),
        )["data"]
        if created.get("ontologyReleaseId") != release_id:
            raise AssertionError(
                f"instance {event.event_id} was not tagged with the release")
        release_tagged += 1
    if release_tagged != len(oracle.events):
        raise AssertionError("not every fixture instance was release-tagged")

    print(
        "[runtime-e2e] fixture ontology published: "
        f"ontologyId={ontology_id} releaseId={release_id} "
        f"version={version.get('version_number')} "
        f"rows={release_tagged}",
        flush=True,
    )
    return {
        "ontologyId": ontology_id,
        "releaseId": release_id,
        "versionNumber": version.get("version_number"),
        "snapshotHash": version.get("snapshot_hash"),
        "ontologyName": ontology.get("name"),
        "publishPath": "POST /api/v1/ontologies/import (v0 released) "
                       "+ POST /instances (release-tagged)",
        "objectTypeId": object_type_id,
        "instancesCreated": release_tagged,
        "releaseTaggedInstances": release_tagged,
    }


def _create_fake_model(
    api: Api, *, name: str, model_base_url: str,
    on_created: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    _register_secret(FIXTURE_API_KEY)
    created = api.ok("POST", "/api/v1/models", {
        "name": name,
        "config_type": "llm",
        "provider": "openai",
        "api_base": model_base_url,
        "api_key": FIXTURE_API_KEY,
        "models": [MODEL_NAME],
        "options": {"max_output_tokens": 128},
        "enabled": False,
    }, expected=(201,))["data"]
    model_id = created.get("id")
    if not model_id:
        raise AssertionError("model config create response lacks id")
    if on_created is not None:
        on_created(model_id)
    tested = api.ok("POST", f"/api/v1/models/{model_id}/test")["data"]
    if not tested.get("ok"):
        raise AssertionError(
            "model connectivity test failed: "
            f"{tested.get('code')} {redact(tested.get('message'))[:300]}"
        )
    enabled = api.ok(
        "POST", f"/api/v1/models/{model_id}/enabled", {"enabled": True},
    )["data"]
    if not enabled.get("enabled"):
        raise AssertionError("model config was not enabled after a passing test")
    return {
        "modelConfigId": model_id,
        "provider": "openai",
        "model": MODEL_NAME,
        "tested": True,
        "enabled": True,
    }


def _run_detail_stream(
    api: Api,
    *,
    ontology_id: str,
    release_id: str,
    model_id: str,
    run_id: str,
    timeout: float,
    oracle: Oracle,
) -> tuple[dict[str, Any], dict[str, Any]]:
    submitted = _submit_run(
        api, ontology_id, release_id, model_id,
        run_id=run_id, message="fixture.detail list fixture events",
    )
    conversation_id = submitted["conversationId"]
    events = read_sse(
        api, _run_path(ontology_id, run_id) + "/events?after_seq=0",
        timeout=timeout,
    )
    final = _wait_run(
        api, ontology_id, run_id, timeout=timeout,
        terminal=TERMINAL_RUN_STATUSES,
    )
    if final["status"] != "succeeded":
        raise AssertionError(
            f"detail run ended {final['status']!r}: "
            f"{redact(final.get('error'))[:500]}")
    snapshots = [event for event in events if event.get("type") == "snapshot"]
    if not snapshots:
        raise AssertionError("SSE did not start with a snapshot frame")
    seqs = [int(event["seq"]) for event in events if "seq" in event]
    if seqs != sorted(seqs) or len(seqs) != len(set(seqs)):
        raise AssertionError("SSE seq is not strictly monotonic")
    if not any(event.get("type") == "delta" for event in events):
        raise AssertionError("SSE did not carry model delta events")
    steps = [event for event in events if event.get("type") == "step"]
    if len(steps) != 1 or steps[0].get("tool") != "query_objects":
        raise AssertionError("expected exactly one query_objects step event")
    result = steps[0].get("result") or {}
    query_spec = result.get("querySpec")
    if not isinstance(query_spec, dict):
        raise AssertionError("query_objects step has no replayable querySpec")
    _assert_query_window(query_spec)
    total = int(result.get("totalRowCount"))
    if total != len(oracle.included):
        raise AssertionError(
            f"tool totalRowCount {total} != oracle {len(oracle.included)}")
    if not result.get("hasNext"):
        raise AssertionError("detail tool page unexpectedly had no next page")
    answers = [event for event in events if event.get("type") == "answer"]
    if len(answers) != 1 or answers[0].get("content") != "fixture-detail-ok":
        raise AssertionError("SSE answer event is missing or unexpected")
    if not any(event.get("type") == "done" for event in events):
        raise AssertionError("SSE did not end with a done event")
    if final["content"] != "fixture-detail-ok":
        raise AssertionError("detail run content was not persisted")
    if len(final.get("steps") or []) != 1:
        raise AssertionError("detail run ledger did not persist one step")

    conversation = _conversation(api, ontology_id, conversation_id)
    if conversation.get("ontologyReleaseId") != release_id:
        raise AssertionError("detail conversation is not pinned to the release")
    messages = _messages(conversation)
    roles = _count_roles(messages)
    if roles.get("user") != 1 or roles.get("assistant") != 1:
        raise AssertionError(
            f"detail conversation roles are {dict(roles)}, expected 1/1")
    assistant = next(item for item in messages if item.get("role") == "assistant")
    if assistant.get("content") != "fixture-detail-ok":
        raise AssertionError("assistant message content was not persisted")

    details = {
        "ontologyId": ontology_id,
        "releaseId": release_id,
        "runId": run_id,
        "conversationId": conversation_id,
        "snapshotStatus": (snapshots[0].get("snapshot") or {}).get("status"),
        "eventCount": len(events),
        "seqCount": len(seqs),
        "lastSeq": seqs[-1] if seqs else None,
        "sawDelta": True,
        "stepTool": "query_objects",
        "toolTotalRowCount": total,
        "stepHasNext": True,
        "answerPersisted": True,
        "terminalStatus": final["status"],
    }
    return details, {
        "runId": run_id,
        "conversationId": conversation_id,
        "querySpec": query_spec,
    }


def _run_query_view_detail(
    api: Api,
    *,
    ontology_id: str,
    conversation_id: str,
    query_spec: dict[str, Any],
    oracle: Oracle,
) -> dict[str, Any]:
    page = _query_view_pages(
        api, ontology_id, conversation_id,
        query_spec=query_spec, page_size=17,
    )
    if page["totalRowCount"] is None:
        raise AssertionError("query-view did not return totalRowCount")
    if page["totalRowCount"] != len(oracle.included):
        raise AssertionError(
            f"query-view totalRowCount {page['totalRowCount']} "
            f"!= oracle {len(oracle.included)}")
    ids = [row.get("event_id") for row in page["rows"]]
    if any(not isinstance(item, str) or not item for item in ids):
        raise AssertionError("query-view rows lack event_id values")
    if ids != sorted(ids):
        raise AssertionError("query-view rows are not ordered by event_id")
    if len(ids) != len(set(ids)):
        raise AssertionError("query-view pagination duplicated rows")
    expected = list(oracle.detail_ids)
    if ids != expected:
        missing = sorted(set(expected) - set(ids))
        extra = sorted(set(ids) - set(expected))
        raise AssertionError(
            f"query-view cursor coverage mismatch; missing={missing[:5]} "
            f"extra={extra[:5]}")
    if not page["released"]:
        raise AssertionError("final query-view page did not release the handle")
    if page["pages"] < 2:
        raise AssertionError("query-view cursor pagination never advanced")
    return {
        "pages": page["pages"],
        "returned": page["returned"],
        "unique": len(set(ids)),
        "totalRowCount": page["totalRowCount"],
        "cursorWasRequired": page["firstCursor"] is not None,
        "releasedOnLastPage": page["released"],
        "missing": 0,
        "duplicates": 0,
    }


def _run_stats_drilldown(
    api: Api,
    *,
    ontology_id: str,
    release_id: str,
    model_id: str,
    run_id: str,
    timeout: float,
    oracle: Oracle,
) -> dict[str, Any]:
    submitted = _submit_run(
        api, ontology_id, release_id, model_id,
        run_id=run_id, message="fixture.stats group fixture events",
    )
    conversation_id = submitted["conversationId"]
    conversation = _conversation(api, ontology_id, conversation_id)
    if conversation.get("ontologyReleaseId") != release_id:
        raise AssertionError("stats conversation is not pinned to the release")
    final = _wait_run(api, ontology_id, run_id, timeout=timeout)
    if final["status"] != "succeeded":
        raise AssertionError(f"stats run ended {final['status']!r}")
    steps = final.get("steps") or []
    if len(steps) != 1 or steps[0].get("tool") != "query_objects":
        raise AssertionError("stats run did not persist one query_objects step")
    result = steps[0].get("result") or {}
    query_spec = result.get("querySpec")
    if not isinstance(query_spec, dict):
        raise AssertionError("stats step has no replayable querySpec")
    _assert_query_window(query_spec)
    rows = list(result.get("rows") or [])
    counts: dict[str, int] = {}
    by_label: dict[str, dict[str, Any]] = {}
    for row in rows:
        group_values = row.get("groupValues") or []
        label = _region_label(
            group_values[0] if group_values else row.get("region"))
        value = row.get("count")
        if value is None:
            value = row.get("value")
        if value is None:
            raise AssertionError(f"group {label!r} lacks a numeric count")
        counts[label] = int(value)
        by_label[label] = row
    if counts != oracle.region_counts:
        raise AssertionError(
            f"group counts {counts} != oracle {oracle.region_counts}")
    if sum(counts.values()) != len(oracle.included):
        raise AssertionError("group counts do not sum to in-window rows")

    checks: list[dict[str, Any]] = []
    for label in sorted(oracle.region_counts):
        row = by_label.get(label)
        if row is None:
            raise AssertionError(f"missing group row for {label!r}")
        drilldown = (row.get("drilldown") or {}).get("token")
        if not drilldown:
            raise AssertionError(f"group {label!r} has no drilldown token")
        page = _query_view_pages(
            api, ontology_id, conversation_id,
            query_spec=query_spec, drilldown=drilldown, page_size=20,
        )
        if page["totalRowCount"] != counts[label]:
            raise AssertionError(
                f"drilldown {label!r} total {page['totalRowCount']} "
                f"!= group count {counts[label]}")
        if not page["released"]:
            raise AssertionError(
                f"drilldown {label!r} did not release its final handle")
        if page["returned"] != counts[label]:
            raise AssertionError(
                f"drilldown {label!r} returned {page['returned']} rows, "
                f"expected {counts[label]}")
        for detail_row in page["rows"]:
            if _region_label(detail_row.get("region")) != label:
                raise AssertionError(
                    f"drilldown {label!r} leaked a row from another group")
        window = page.get("resolvedWindow") or {}
        if window.get("startUtc") != WINDOW_START_ISO or \
                window.get("endUtc") != WINDOW_END_ISO:
            raise AssertionError(
                f"drilldown {label!r} did not preserve the frozen window")
        checks.append({
            "group": label,
            "groupCount": counts[label],
            "drilldownRows": page["returned"],
            "drilldownPages": page["pages"],
        })
    return {
        "runId": run_id,
        "conversationId": conversation_id,
        "groupCount": len(counts),
        "totalRows": sum(counts.values()),
        "groups": checks,
        "windowStartUtc": WINDOW_START_ISO,
        "windowEndUtc": WINDOW_END_ISO,
    }


def _run_sequences(
    api: Api,
    *,
    ontology_id: str,
    release_id: str,
    model_id: str,
    nonce: str,
    timeout: float,
    oracle: Oracle,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for threshold in (9, 10, 11):
        run_id = f"{nonce}-sequence-{threshold}"
        submitted = _submit_run(
            api, ontology_id, release_id, model_id,
            run_id=run_id,
            message=f"fixture.sequence.{threshold} distinct fixture actors",
        )
        final = _wait_run(api, ontology_id, run_id, timeout=timeout)
        if final["status"] != "succeeded":
            raise AssertionError(
                f"sequence {threshold} run ended {final['status']!r}")
        steps = final.get("steps") or []
        if len(steps) != 1 or steps[0].get("tool") != "analyze_sequences":
            raise AssertionError(
                f"sequence {threshold} did not persist analyze_sequences")
        result = steps[0].get("result") or {}
        _assert_query_window(result.get("querySpec") or {})
        distinct = int(result.get("distinctSubjects"))
        candidates = int(result.get("candidateRows"))
        meets = bool(result.get("meetsThreshold"))
        if distinct != oracle.sequence_distinct:
            raise AssertionError(
                f"sequence {threshold} distinctSubjects {distinct} "
                f"!= oracle {oracle.sequence_distinct}")
        if candidates != oracle.sequence_candidates:
            raise AssertionError(
                f"sequence {threshold} candidateRows {candidates} "
                f"!= oracle {oracle.sequence_candidates}")
        expected_meets = oracle.sequence_thresholds[str(threshold)]
        if meets is not expected_meets:
            raise AssertionError(
                f"sequence {threshold} meetsThreshold {meets} "
                f"!= oracle {expected_meets}")
        results[str(threshold)] = {
            "runId": run_id,
            "distinctSubjects": distinct,
            "candidateRows": candidates,
            "meetsThreshold": meets,
            "expectedMeetsThreshold": expected_meets,
        }
    return {
        "thresholds": results,
        "distinctSubjects": oracle.sequence_distinct,
        "candidateRows": oracle.sequence_candidates,
        "duplicateRows": oracle.sequence_candidates - oracle.sequence_distinct,
    }


def _run_disconnect_resubscribe(
    api: Api,
    stub: Api,
    *,
    ontology_id: str,
    release_id: str,
    model_id: str,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    _gate_arm(stub, "disconnect")
    submitted = _submit_run(
        api, ontology_id, release_id, model_id,
        run_id=run_id, message="fixture.disconnect continue after browser close",
    )
    conversation_id = submitted["conversationId"]
    response = api.open_sse(
        _run_path(ontology_id, run_id) + "/events?after_seq=0",
        timeout=timeout,
    )
    last_seq = 0
    saw_step = False
    try:
        deadline = time.monotonic() + timeout
        for event in iter_sse(response, deadline=deadline):
            if "seq" in event:
                last_seq = max(last_seq, int(event["seq"]))
            if event.get("type") == "step":
                if event.get("error"):
                    raise AssertionError(
                        f"disconnect progress step failed: "
                        f"{redact(event.get('error'))[:300]}")
                saw_step = True
                break
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - close only
            pass
    if not saw_step:
        raise AssertionError(
            "disconnect probe never observed a persisted tool step")
    _wait_gate_waiting(stub, "disconnect")
    during = _get_run(api, ontology_id, run_id)
    if during["status"] != "running":
        raise AssertionError(
            f"run after SSE disconnect is {during['status']!r}, expected running")
    if during.get("cancelRequestedAt"):
        raise AssertionError("SSE disconnect recorded a cancel request")
    resumed = _read_first_sse(
        api,
        _run_path(ontology_id, run_id) + f"/events?after_seq={last_seq}",
        timeout=10.0,
    )
    resumed_snapshot = resumed.get("snapshot") or {}
    if resumed.get("type") != "snapshot" or \
            resumed_snapshot.get("status") != "running":
        raise AssertionError(
            "resubscribe while gated did not expose a running snapshot")
    if not resumed_snapshot.get("steps"):
        raise AssertionError(
            "resubscribe while gated lost the persisted progress step")
    _gate_release(stub, "disconnect")

    final = _wait_run(api, ontology_id, run_id, timeout=timeout)
    if final["status"] != "succeeded" or final["content"] != "fixture-disconnect-ok":
        raise AssertionError(
            f"disconnect run did not continue to success: {final['status']!r}")

    replay = read_sse(
        api,
        _run_path(ontology_id, run_id) + f"/events?after_seq={last_seq}",
        timeout=timeout,
    )
    replay_snapshots = [event for event in replay
                        if event.get("type") == "snapshot"]
    if not replay_snapshots or \
            (replay_snapshots[0].get("snapshot") or {}).get("status") != "succeeded":
        raise AssertionError("resubscribe snapshot did not report success")
    replay_seqs = [int(event["seq"]) for event in replay if "seq" in event]
    if any(seq <= last_seq for seq in replay_seqs):
        raise AssertionError("after_seq replay returned already-seen events")
    if not any(event.get("type") == "done" for event in replay):
        raise AssertionError("resubscribe did not replay the done event")

    conversation = _conversation(api, ontology_id, conversation_id)
    roles = _count_roles(_messages(conversation))
    if roles.get("user") != 1 or roles.get("assistant") != 1:
        raise AssertionError("disconnect conversation did not persist 1/1 messages")
    return {
        "runId": run_id,
        "conversationId": conversation_id,
        "lastSeqBeforeDisconnect": last_seq,
        "statusAfterDisconnect": "running",
        "gateBlockedWhileDisconnected": True,
        "persistedStepBeforeRelease": True,
        "resubscribedWhileGatedStatus": "running",
        "cancelRequestedAt": None,
        "finalStatus": final["status"],
        "replayEvents": len(replay),
        "replayedDone": True,
        "answerPersisted": True,
    }


def _cancel_flow(
    api: Api,
    stub: Api,
    *,
    ontology_id: str,
    release_id: str,
    model_id: str,
    nonce: str,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = f"{nonce}-cancel"
    second_run_id = f"{nonce}-cancel-second"
    _gate_arm(stub, "cancel")
    submitted = _submit_run(
        api, ontology_id, release_id, model_id,
        run_id=run_id, message="fixture.cancel long running fixture turn",
    )
    conversation_id = submitted["conversationId"]
    _wait_gate_waiting(stub, "cancel")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _get_run(api, ontology_id, run_id)
        if current["status"] == "running":
            break
        if current["status"] in TERMINAL_RUN_STATUSES:
            raise AssertionError("cancel probe became terminal before cancel")
        time.sleep(0.2)
    else:
        raise AssertionError("cancel probe never entered running state")

    second_status, second_body = api.raw(
        "POST",
        f"/api/v2/formal/ontologies/{ontology_id}/agent/chat/runs",
        {
            "message": "fixture.idempotent second run must be rejected",
            "runId": second_run_id,
            "conversationId": conversation_id,
            "modelId": model_id,
            "releaseId": release_id,
            "stream": True,
        },
    )
    if second_status != 409:
        raise AssertionError(
            f"second active run in the same conversation returned "
            f"HTTP {second_status}, expected 409")
    code = None
    if isinstance(second_body, dict):
        detail = second_body.get("detail")
        if isinstance(detail, dict):
            code = detail.get("code")
    if code != "conversation_busy":
        raise AssertionError(
            f"409 detail code is {code!r}, expected conversation_busy")

    cancel_payload = api.ok(
        "POST", _run_path(ontology_id, run_id) + "/cancel",
    )["data"]
    if cancel_payload.get("cancelled") is not True:
        raise AssertionError("running cancel was not recorded")
    if cancel_payload.get("status") != "running":
        raise AssertionError("running cancel prematurely left running state")
    if not cancel_payload.get("cancelRequestedAt"):
        raise AssertionError("cancel response lacks cancelRequestedAt")
    _gate_release(stub, "cancel")

    final = _wait_run(api, ontology_id, run_id, timeout=timeout)
    if final["status"] != "cancelled":
        raise AssertionError(f"cancel run ended {final['status']!r}")
    if final.get("steps"):
        raise AssertionError("cancelled run persisted late tool steps")
    replay = read_sse(
        api,
        _run_path(ontology_id, run_id) + "/events?after_seq=0",
        timeout=timeout,
    )
    event_types = [event.get("type") for event in replay]
    if "step" in event_types:
        raise AssertionError("cancelled run emitted a step event")
    if "answer" in event_types:
        raise AssertionError("cancelled run emitted an answer event")
    if "cancelled" not in event_types or not event_types or \
            event_types[-1] != "done":
        raise AssertionError(
            f"cancelled run event sequence is {event_types}, "
            "expected cancelled + terminal done")
    seqs = [int(event["seq"]) for event in replay if "seq" in event]
    conversation = _conversation(api, ontology_id, conversation_id)
    if conversation.get("ontologyReleaseId") != release_id:
        raise AssertionError("cancel conversation is not release-pinned")
    roles = _count_roles(_messages(conversation))
    if roles.get("user") != 1:
        raise AssertionError(
            f"rejected second run inserted a user message: {dict(roles)}")

    first_details = {
        "firstRunId": run_id,
        "conversationId": conversation_id,
        "firstRunStatus": "running",
        "firstRunGateBlocked": True,
        "secondRunId": second_run_id,
        "secondStatus": second_status,
        "secondDetailCode": code,
    }
    second_details = {
        "runId": run_id,
        "conversationId": conversation_id,
        "cancelResponseStatus": "running",
        "cancelRequestedAt": True,
        "terminalStatus": final["status"],
        "stepEvents": 0,
        "answerEvents": 0,
        "terminalEvent": event_types[-1],
        "lastSeq": seqs[-1] if seqs else None,
        "lateStepsAfterTerminal": 0,
        "conversationUserMessages": roles.get("user", 0),
    }
    return first_details, second_details


def _retry_cancelled_run(
    api: Api,
    stub: Api,
    *,
    ontology_id: str,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    source = _get_run(api, ontology_id, run_id)
    conversation_id = source["conversationId"]
    before = _count_roles(_messages(_conversation(
        api, ontology_id, conversation_id)))
    _gate_arm(stub, "cancel")
    retried = api.ok(
        "POST", _run_path(ontology_id, run_id) + "/retry",
    )["data"]
    retry_run_id = retried["runId"]
    _track_run(retry_run_id)
    _wait_gate_waiting(stub, "cancel")
    if retried.get("retryOf") != run_id:
        raise AssertionError("retry response did not link retryOf")
    if retried.get("conversationId") != conversation_id:
        raise AssertionError("retry moved the original conversation")
    if retried.get("question") != source.get("question"):
        raise AssertionError("retry did not reuse the original question")
    _gate_release(stub, "cancel")
    final = _wait_run(api, ontology_id, retry_run_id, timeout=timeout)
    if final["status"] != "succeeded":
        raise AssertionError(f"retry run ended {final['status']!r}")
    if final.get("content") != "fixture-cancel-ok":
        raise AssertionError("retry answer was not persisted")
    after = _count_roles(_messages(_conversation(
        api, ontology_id, conversation_id)))
    if after.get("user") != 1:
        raise AssertionError(
            f"retry duplicated the user message: {dict(after)}")
    if before.get("user") != after.get("user"):
        raise AssertionError("retry changed the user message count")
    if after.get("assistant", 0) < 2:
        raise AssertionError(
            "retry did not persist its assistant answer alongside the source")
    return {
        "sourceRunId": run_id,
        "retryRunId": retry_run_id,
        "retryOf": retried.get("retryOf"),
        "conversationId": conversation_id,
        "userMessagesBefore": before.get("user", 0),
        "userMessagesAfter": after.get("user", 0),
        "assistantMessagesAfter": after.get("assistant", 0),
        "status": final["status"],
        "answerPersisted": True,
    }


def _concurrent_runs(
    api: Api,
    stub: Api,
    *,
    ontology_id: str,
    release_id: str,
    model_id: str,
    nonce: str,
    timeout: float,
    oracle: Oracle,
) -> dict[str, Any]:
    run_a = f"{nonce}-concurrent-a"
    run_b = f"{nonce}-concurrent-b"
    _gate_arm(stub, "concurrent.A")
    _gate_arm(stub, "concurrent.B")
    submitted_a = _submit_run(
        api, ontology_id, release_id, model_id,
        run_id=run_a, message="fixture.concurrent.A north fixture events",
    )
    submitted_b = _submit_run(
        api, ontology_id, release_id, model_id,
        run_id=run_b, message="fixture.concurrent.B south fixture events",
    )
    conv_a = submitted_a["conversationId"]
    conv_b = submitted_b["conversationId"]
    if conv_a == conv_b:
        raise AssertionError("concurrent runs unexpectedly shared a conversation")
    _wait_gate_waiting(stub, "concurrent.A")
    _wait_gate_waiting(stub, "concurrent.B")
    status_a = _get_run(api, ontology_id, run_a)["status"]
    status_b = _get_run(api, ontology_id, run_b)["status"]
    if status_a not in ACTIVE_RUN_STATUSES or status_b not in ACTIVE_RUN_STATUSES:
        raise AssertionError(
            f"concurrent runs were not both active: {status_a}/{status_b}")
    _gate_release(stub, "concurrent.A")
    _gate_release(stub, "concurrent.B")
    final_a = _wait_run(api, ontology_id, run_a, timeout=timeout)
    final_b = _wait_run(api, ontology_id, run_b, timeout=timeout)
    if final_a["status"] != "succeeded" or final_b["status"] != "succeeded":
        raise AssertionError(
            f"concurrent runs ended {final_a['status']}/{final_b['status']}")
    if final_a.get("content") != "fixture-concurrent-ok" or \
            final_b.get("content") != "fixture-concurrent-ok":
        raise AssertionError("concurrent run answers were not isolated")
    step_a = (final_a.get("steps") or [{}])[0].get("result") or {}
    step_b = (final_b.get("steps") or [{}])[0].get("result") or {}
    total_a = int(step_a.get("totalRowCount"))
    total_b = int(step_b.get("totalRowCount"))
    expected_a = oracle.read_region_counts["north"]
    expected_b = oracle.read_region_counts["south"]
    if total_a != expected_a or total_b != expected_b:
        raise AssertionError(
            f"concurrent totals {total_a}/{total_b} != oracle "
            f"{expected_a}/{expected_b}")
    _assert_query_window(step_a.get("querySpec") or {})
    _assert_query_window(step_b.get("querySpec") or {})
    conv_a_data = _conversation(api, ontology_id, conv_a)
    conv_b_data = _conversation(api, ontology_id, conv_b)
    if conv_a_data.get("ontologyReleaseId") != release_id or \
            conv_b_data.get("ontologyReleaseId") != release_id:
        raise AssertionError("concurrent conversations are not release-pinned")
    roles_a = _count_roles(_messages(conv_a_data))
    roles_b = _count_roles(_messages(conv_b_data))
    if roles_a.get("user") != 1 or roles_a.get("assistant") != 1:
        raise AssertionError("concurrent A conversation is not isolated")
    if roles_b.get("user") != 1 or roles_b.get("assistant") != 1:
        raise AssertionError("concurrent B conversation is not isolated")
    return {
        "runA": run_a,
        "runB": run_b,
        "conversationA": conv_a,
        "conversationB": conv_b,
        "bothActiveAtSubmit": True,
        "bothBlockedOnGate": True,
        "statusA": final_a["status"],
        "statusB": final_b["status"],
        "northRows": total_a,
        "southRows": total_b,
        "expectedNorthRows": expected_a,
        "expectedSouthRows": expected_b,
        "isolatedConversations": True,
    }


def _idempotent_submit(
    api: Api,
    stub: Api,
    *,
    ontology_id: str,
    release_id: str,
    model_id: str,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    before = stub.ok("GET", "/__fixture/stats")
    before_count = int((before.get("byScenario") or {}).get("idempotent", 0))
    body = {
        "message": "fixture.idempotent repeated submit fixture",
        "runId": run_id,
        "modelId": model_id,
        "releaseId": release_id,
        "stream": True,
    }
    _gate_arm(stub, "idempotent")
    first = api.ok(
        "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/chat/runs",
        body,
    )["data"]
    _track_run(first["runId"])
    _wait_gate_waiting(stub, "idempotent")
    second = api.ok(
        "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/chat/runs",
        body,
    )["data"]
    if first["runId"] != second["runId"] or first["runId"] != run_id:
        raise AssertionError("duplicate runId submission returned another run")
    if first["conversationId"] != second["conversationId"]:
        raise AssertionError("duplicate runId submission returned another conversation")
    if first.get("releaseId") != release_id or \
            second.get("releaseId") != release_id:
        raise AssertionError("duplicate runId submission changed the release")
    conversation_id = first["conversationId"]
    _gate_release(stub, "idempotent")
    final = _wait_run(api, ontology_id, run_id, timeout=timeout)
    if final["status"] != "succeeded" or \
            final.get("content") != "fixture-idempotent-ok":
        raise AssertionError(
            f"idempotent run did not succeed: {final['status']!r}")
    conversation = _conversation(api, ontology_id, conversation_id)
    if conversation.get("ontologyReleaseId") != release_id:
        raise AssertionError("idempotent conversation is not release-pinned")
    messages = _messages(conversation)
    roles = _count_roles(messages)
    if roles.get("user") != 1:
        raise AssertionError(
            f"duplicate submit inserted another user message: {dict(roles)}")
    if roles.get("assistant") != 1:
        raise AssertionError(
            f"duplicate submit inserted another assistant message: {dict(roles)}")
    after = stub.ok("GET", "/__fixture/stats")
    after_count = int((after.get("byScenario") or {}).get("idempotent", 0))
    if after_count - before_count != 1:
        raise AssertionError(
            f"duplicate submit caused {after_count - before_count} LLM calls")
    return {
        "runId": run_id,
        "conversationId": conversation_id,
        "duplicateResponseStatus": second.get("status"),
        "duplicateSubmitWhileGateBlocked": True,
        "userMessages": roles.get("user", 0),
        "assistantMessages": roles.get("assistant", 0),
        "llmCallsDelta": after_count - before_count,
        "terminalStatus": final["status"],
    }


def run_driver(args: argparse.Namespace, password: str) -> int:
    _register_secret(password)
    _register_secret(FIXTURE_API_KEY)
    api = Api(args.base_url, timeout=min(float(args.timeout_seconds), 60.0))
    model_base = validate_loopback_url(args.model_base_url, allow_v1_path=True)
    stub = Api(_origin(model_base), timeout=10.0)
    oracle = build_oracle(fixture_events())
    nonce = uuid.uuid4().hex[:8]
    _TRACKED_RUN_IDS.clear()
    recorder = CaseRecorder()
    state: dict[str, Any] = {}
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "ontology-assistant-runtime-e2e",
        "status": "running",
        "startedAt": iso_now(),
        "finishedAt": None,
        "apiBase": api.base_url,
        "modelBase": model_base,
        "loopbackOnly": True,
        "fixture": {
            "ontologyName": f"{args.ontology_name_prefix}-{nonce}",
            "rowCount": len(oracle.events),
            "includedRows": len(oracle.included),
            "window": {
                "timezone": WINDOW_TIMEZONE,
                "localStart": WINDOW_LOCAL_START,
                "localEnd": WINDOW_LOCAL_END,
                "startUtc": WINDOW_START_ISO,
                "endUtc": WINDOW_END_ISO,
                "exclusiveEnd": True,
            },
        },
        "oracle": oracle.report(),
        "publish": None,
        "cases": recorder.cases,
        "failures": [],
        "notes": [
            "Synthetic 128-row loopback fixture; not a production-scale or "
            "production-environment acceptance.",
            "The driver imports no backend module and never writes SQL; all "
            "front-door effects are real HTTP calls.",
            "The model stub fakes only the upstream LLM HTTP boundary; "
            "PostgreSQL, NATS executor, run ledger and query engine are real.",
            "The synthetic ontology is intentionally left in the fixture "
            "database for real-UI inspection; the fake model config is "
            "deleted unless --keep-model-config is passed.",
            "The optional frontend real-stack Playwright spec and its "
            "classification config are owned by the frontend agent.",
        ],
    }
    if model_base != args.model_base_url.rstrip("/"):
        report["modelBaseNormalized"] = True

    failure: Exception | None = None
    try:
        recorder.run("fixture_api_live", lambda: _case_api_live(api))
        recorder.run("model_stub_live", lambda: _case_model_stub(stub))
        recorder.run(
            "login_fixture_admin",
            lambda: _login(api, args.admin_user, password),
        )
        publish_details = recorder.run(
            "publish_frontdoor_release",
            lambda: _publish_ontology(
                api, oracle,
                name=f"{args.ontology_name_prefix}-{nonce}",
                nonce=nonce,
            ),
        )
        report["publish"] = publish_details
        state.update(publish_details)
        ontology_id = state["ontologyId"]
        release_id = state["releaseId"]

        model_details = recorder.run(
            "fake_model_config_test_enable",
            lambda: _create_fake_model(
                api,
                name=f"{args.model_config_prefix}-{nonce}",
                model_base_url=model_base,
                on_created=lambda value: state.__setitem__(
                    "modelConfigId", value),
            ),
        )
        state["modelConfigId"] = model_details["modelConfigId"]
        model_id = model_details["modelConfigId"]

        detail_state: dict[str, Any] = {}

        def _detail_case() -> dict[str, Any]:
            details, payload = _run_detail_stream(
                api,
                ontology_id=ontology_id,
                release_id=release_id,
                model_id=model_id,
                run_id=f"{nonce}-detail",
                timeout=float(args.timeout_seconds),
                oracle=oracle,
            )
            detail_state.update(payload)
            return details

        recorder.run(
            "stream_snapshot_seq_tool_answer_persisted", _detail_case)
        recorder.run(
            "query_view_detail_cursor_pagination",
            lambda: _run_query_view_detail(
                api,
                ontology_id=ontology_id,
                conversation_id=detail_state["conversationId"],
                query_spec=detail_state["querySpec"],
                oracle=oracle,
            ),
        )
        recorder.run(
            "stats_group_drilldown_fixed_window",
            lambda: _run_stats_drilldown(
                api,
                ontology_id=ontology_id,
                release_id=release_id,
                model_id=model_id,
                run_id=f"{nonce}-stats",
                timeout=float(args.timeout_seconds),
                oracle=oracle,
            ),
        )
        recorder.run(
            "sequence_thresholds_9_10_11_with_duplicate_and_boundaries",
            lambda: _run_sequences(
                api,
                ontology_id=ontology_id,
                release_id=release_id,
                model_id=model_id,
                nonce=nonce,
                timeout=float(args.timeout_seconds),
                oracle=oracle,
            ),
        )
        recorder.run(
            "sse_disconnect_resubscribe_not_cancelled",
            lambda: _run_disconnect_resubscribe(
                api, stub,
                ontology_id=ontology_id,
                release_id=release_id,
                model_id=model_id,
                run_id=f"{nonce}-disconnect",
                timeout=float(args.timeout_seconds),
            ),
        )

        cancel_details: dict[str, Any] = {}

        def _single_flight_case() -> dict[str, Any]:
            first, second = _cancel_flow(
                api, stub,
                ontology_id=ontology_id,
                release_id=release_id,
                model_id=model_id,
                nonce=nonce,
                timeout=float(args.timeout_seconds),
            )
            cancel_details.update(first)
            cancel_details["_cancel"] = second
            return first

        recorder.run(
            "same_conversation_second_run_409", _single_flight_case)
        recorder.run(
            "cancel_terminal_without_late_steps",
            lambda: cancel_details["_cancel"],
        )
        recorder.run(
            "retry_does_not_duplicate_user_message",
            lambda: _retry_cancelled_run(
                api, stub,
                ontology_id=ontology_id,
                run_id=cancel_details["firstRunId"],
                timeout=float(args.timeout_seconds),
            ),
        )
        recorder.run(
            "concurrent_runs_conversation_isolated",
            lambda: _concurrent_runs(
                api, stub,
                ontology_id=ontology_id,
                release_id=release_id,
                model_id=model_id,
                nonce=nonce,
                timeout=float(args.timeout_seconds),
                oracle=oracle,
            ),
        )
        recorder.run(
            "same_run_id_no_duplicate_llm_or_user_message",
            lambda: _idempotent_submit(
                api, stub,
                ontology_id=ontology_id,
                release_id=release_id,
                model_id=model_id,
                run_id=f"{nonce}-idempotent",
                timeout=float(args.timeout_seconds),
            ),
        )
        report["status"] = "passed"
    except Exception as exc:  # noqa: BLE001 - evidence is the product here
        failure = exc
        report["status"] = "failed"
        report["failures"].append({
            "case": recorder.cases[-1]["id"] if recorder.cases else "startup",
            "error": f"{type(exc).__name__}: {redact(exc)[:800]}",
        })
        print(
            f"[runtime-e2e] FAIL {report['failures'][-1]['case']}: "
            f"{redact(exc)[:500]}",
            file=sys.stderr,
        )
    finally:
        try:
            report["runCleanup"] = _cleanup_tracked_runs(
                api, state.get("ontologyId"))
        except Exception as exc:  # noqa: BLE001 - cleanup only
            report["runCleanup"] = {
                "error": f"{type(exc).__name__}: {redact(exc)[:200]}"}
        try:
            _gate_release_all(stub)
            report["gateCleanup"] = "released"
        except Exception as exc:  # noqa: BLE001 - cleanup only
            report["gateCleanup"] = (
                f"failed: {type(exc).__name__}: {redact(exc)[:200]}")
        model_id = state.get("modelConfigId")
        if model_id and api.token and not args.keep_model_config:
            try:
                status, _ = api.raw(
                    "DELETE", f"/api/v1/models/{model_id}", timeout=5.0)
                report["modelConfigCleanup"] = (
                    "deleted" if status in (200, 204, 404) else f"http_{status}"
                )
            except Exception as exc:  # noqa: BLE001 - cleanup only
                report["modelConfigCleanup"] = (
                    f"failed: {type(exc).__name__}: {redact(exc)[:200]}")
        try:
            stats = stub.ok("GET", "/__fixture/stats", timeout=2.0)
            if isinstance(stats, dict):
                report["modelStubStats"] = {
                    "requests": stats.get("requests"),
                    "byScenario": stats.get("byScenario"),
                }
        except Exception:  # noqa: BLE001 - evidence only
            report["modelStubStats"] = {"available": False}
        report["finishedAt"] = iso_now()
        report["durationMs"] = int(
            (datetime.fromisoformat(report["finishedAt"])
             - datetime.fromisoformat(report["startedAt"])).total_seconds() * 1000
        )
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str)
                + "\n",
                encoding="utf-8",
            )
            print(f"[runtime-e2e] report: {args.report}", flush=True)
        except Exception as exc:  # noqa: BLE001 - best effort evidence
            print(
                f"[runtime-e2e] ERROR writing report: {redact(exc)[:300]}",
                file=sys.stderr,
            )
            report["status"] = "failed"
            report["failures"].append({
                "case": "write_report",
                "error": f"{type(exc).__name__}: {redact(exc)[:400]}",
            })
            if failure is None:
                failure = exc

    if report["status"] == "passed":
        print(
            "[runtime-e2e] PASS: "
            f"{len(recorder.cases)} cases; ontologyId="
            f"{state.get('ontologyId')} releaseId={state.get('releaseId')}",
            flush=True,
        )
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Isolated ontology-assistant runtime E2E driver and loopback "
            "OpenAI-compatible model stub."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The driver never starts Docker, the API, or the executor. "
            "Run stack-up/backend-up/executor-up separately, then start "
            "--serve-model and run this driver against 127.0.0.1 fixtures."
        ),
    )
    parser.add_argument(
        "--serve-model", action="store_true",
        help="serve the fake OpenAI-compatible LLM boundary instead of driving",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="model stub bind host (loopback only; default 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=18037,
        help="model stub bind port (default 18037; 0 picks a free port)",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"fixture API base URL (default {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--model-base-url", default=DEFAULT_MODEL_BASE_URL,
        help=(
            "fake model OpenAI-compatible base URL used in the model config "
            f"(default {DEFAULT_MODEL_BASE_URL})"
        ),
    )
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument(
        "--admin-password", default=None,
        help=(
            "fixture admin password (or OPENONTOLOGY_E2E_ADMIN_PASSWORD); "
            "required outside --serve-model"
        ),
    )
    parser.add_argument(
        "--report", type=Path, default=DEFAULT_REPORT,
        help=f"JSON evidence path (default {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=180.0,
        help="per-run/SSE wait timeout in seconds (default 180)",
    )
    parser.add_argument(
        "--ontology-name-prefix", default="assistant-runtime-e2e",
        help="synthetic ontology name prefix",
    )
    parser.add_argument(
        "--model-config-prefix", default="assistant-runtime-e2e-model",
        help="fake model config name prefix",
    )
    parser.add_argument(
        "--keep-model-config", action="store_true",
        help="leave the tested/enabled fake model config in place",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.serve_model:
        return serve_model(args)
    password = args.admin_password or os.getenv(
        "OPENONTOLOGY_E2E_ADMIN_PASSWORD")
    if not password:
        parser.error(
            "--admin-password or OPENONTOLOGY_E2E_ADMIN_PASSWORD is required")
    try:
        validate_loopback_url(args.base_url, allow_v1_path=False)
        validate_loopback_url(args.model_base_url, allow_v1_path=True)
    except ValueError as exc:
        parser.error(str(exc))
    return run_driver(args, password)


if __name__ == "__main__":
    raise SystemExit(main())
