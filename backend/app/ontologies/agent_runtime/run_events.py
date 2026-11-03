"""Ordered event ledger and bounded step payloads for agent runs.

Every observable run event gets a gap-free ``seq`` allocated while holding the
run row lock.  The ledger is the replay source for SSE; the ``AgentRun`` row is
the recoverable projection (steps/content/terminal state).

Tool results are already display-filtered by the orchestrator (remote row
stripping happens in ``query_spec.public_query_result``).  This module only
enforces a hard ledger bound and removes transport-only fields so a new event
can never embed a complete remote result set.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.ontologies.agent_runtime.limits import limit
from app.ontologies.agent_runtime.run_models import AgentRun, AgentRunEvent


_TRANSPORT_KEYS = frozenset({
    "cursor",
    "nextCursor",
    "next_cursor",
    "rawRows",
    "remoteRows",
    "_cursor",
    "_remoteRows",
    "queryId",
    "query_id",
    "expiresAt",
    "expires_at",
})

_STEP_LIST_KEYS = ("rows", "groups", "series", "nodes", "edges", "impacts", "paths")

# A query spec is the replay key for opening the query view; it is never
# truncated.  The query builder caps its own spec at 16KB; 24KB is the run
# ledger's hard acceptance bound (spec + columns + metadata must still fit the
# event payload cap below).
MAX_QUERY_SPEC_BYTES = 24 * 1024

_QUERY_META_KEYS = (
    "kind", "display", "rowsAvailableVia", "rowsOmitted", "rowsAreAggregates",
    "publicNote", "columns", "returned", "counts", "hasNext", "window",
    "consistency", "drilldown", "drilldownBase", "drilldownLabels",
    "semantics", "description", "groupBy", "total", "querySpec",
)

_QUERY_ESSENTIAL_KEYS = (
    "kind", "display", "rowsAvailableVia", "rowsOmitted", "rowsAreAggregates",
    "publicNote", "columns", "returned", "counts", "hasNext", "window",
    "consistency", "drilldown", "drilldownBase", "drilldownLabels", "querySpec",
)


class RunEventPayloadTooLarge(RuntimeError):
    """A querySpec-bearing event cannot be persisted without losing its plan."""


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _strip_transport(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_transport(item)
            for key, item in value.items()
            if key not in _TRANSPORT_KEYS
        }
    if isinstance(value, list):
        return [_strip_transport(item) for item in value]
    return value


def _size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 — unrepresentable values are dropped by caller
        return 2**31


def _compact_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    compact = {}
    for key in (
        "kind", "summary", "columns", "returned", "counts", "hasNext",
        "disclaimer", "note", "_truncated", "_note", "chart",
        "visualizationTruncated", "visualizationCounts",
    ):
        if key in result:
            compact[key] = result[key]
    for key in _STEP_LIST_KEYS:
        value = result.get(key)
        if isinstance(value, list):
            compact[key] = value[:20]
    if len(compact) == 1 and "summary" in compact:
        compact["_truncated"] = True
    return compact


def _query_specs(payload: dict) -> list[Any]:
    specs = []
    for container in (payload.get("arguments"), payload.get("result")):
        if isinstance(container, dict) and container.get("querySpec") is not None:
            specs.append(container["querySpec"])
    if payload.get("querySpec") is not None:
        specs.append(payload["querySpec"])
    return specs


def _validate_query_specs(payload: dict) -> None:
    for spec in _query_specs(payload):
        if _size(spec) > MAX_QUERY_SPEC_BYTES:
            raise RunEventPayloadTooLarge(
                "run_query_spec_too_large: querySpec 超过 24KB 上限，"
                "拒绝静默截断；请缩小查询范围后重试"
            )


def _compact_query_result(result: dict) -> dict:
    compact = {
        key: result[key]
        for key in _QUERY_META_KEYS
        if key in result
    }
    if "querySpec" in result:
        compact["querySpec"] = result["querySpec"]
    compact["_truncated"] = True
    return compact


def _shrink_query_payload(payload: dict) -> dict:
    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    payload["result"] = {
        key: result[key]
        for key in _QUERY_ESSENTIAL_KEYS
        if key in result
    }
    payload["result"]["_truncated"] = True
    return payload


def bounded_step_event(event: dict) -> dict:
    """Return a step payload safe for the event ledger and run projection.

    A querySpec is the replay key for opening the query view, so it is never
    truncated: specs over 24KB fail explicitly.  Over the event cap we first
    drop rows/chart (and aggregate detail), then non-essential metadata; if a
    querySpec-bearing payload still exceeds the cap after that, we raise
    instead of silently producing an unreplayable "success".
    """
    payload = {
        key: value
        for key, value in event.items()
        if key != "type"
    }
    payload = _strip_transport(_json_safe(payload))
    _validate_query_specs(payload)
    cap = limit("run_event_payload_cap")
    if _size(payload) <= cap:
        return payload

    result = payload.get("result")
    has_query_spec = isinstance(result, dict) and "querySpec" in result
    payload["result"] = (
        _compact_query_result(result)
        if has_query_spec
        else _compact_result(result)
    )
    if _size(payload) > cap:
        arguments = payload.get("arguments")
        if isinstance(arguments, dict):
            if "querySpec" in arguments:
                payload["arguments"] = {
                    "querySpec": arguments["querySpec"],
                    "_truncated": True,
                }
            else:
                payload["arguments"] = {"_truncated": True}
    if _size(payload) > cap:
        if has_query_spec:
            payload = _shrink_query_payload(payload)
            if _size(payload) > cap:
                raise RunEventPayloadTooLarge(
                    "run_event_payload_too_large: querySpec + columns/metadata "
                    "超出 run 事件上限，拒绝静默丢查询计划"
                )
        else:
            payload["result"] = {
                "_truncated": True,
                "summary": payload.get("summary"),
            }
            if _size(payload) > cap:
                raise RunEventPayloadTooLarge(
                    "run_event_payload_too_large: 步骤载荷超出 run 事件上限"
                )
    return payload


def cap_event_payload(payload: dict) -> dict:
    """Apply the configured per-event payload bound to every ledger entry."""
    cap = limit("run_event_payload_cap")
    if _size(payload) <= cap:
        return payload
    if payload.get("querySpec") is not None or any(
        isinstance(payload.get(key), dict)
        and payload[key].get("querySpec") is not None
        for key in ("arguments", "result")
    ):
        raise RunEventPayloadTooLarge(
            "run_event_payload_too_large: 含 querySpec 的事件不得截断持久化"
        )
    content = payload.get("content")
    if isinstance(content, str):
        compact = dict(payload)
        compact["content"] = content[: max(0, cap // 2)]
        compact["_truncated"] = True
        compact["_originalChars"] = len(content)
        if _size(compact) <= cap:
            return compact
    return {
        "_truncated": True,
        "preview": json.dumps(payload, ensure_ascii=False, default=str)[:cap],
    }


def append_event(
    db: Session,
    run_id: str,
    event_type: str,
    payload: dict | None = None,
    *,
    require_status: str | None = None,
    commit: bool = False,
) -> AgentRunEvent | None:
    """Atomically allocate ``seq`` and append one event.

    Returns ``None`` when ``require_status`` no longer matches (the run was
    cancelled/finished concurrently), letting the worker stop without writing
    events after a terminal transition.
    """
    now = datetime.now(timezone.utc)
    query = db.query(AgentRun).filter(AgentRun.id == run_id)
    if require_status is not None:
        query = query.filter(AgentRun.status == require_status)
    updated = query.update(
        {
            AgentRun.last_seq: AgentRun.last_seq + 1,
            AgentRun.updated_at: now,
        },
        synchronize_session=False,
    )
    if updated != 1:
        return None
    seq = (
        db.query(AgentRun.last_seq)
        .filter(AgentRun.id == run_id)
        .scalar()
    )
    event = AgentRunEvent(
        run_id=run_id,
        seq=int(seq),
        event_type=event_type,
        payload=cap_event_payload(_json_safe(payload or {})),
        created_at=now,
    )
    db.add(event)
    if commit:
        db.commit()
    return event


class DeltaAccumulator:
    """Coalesce token deltas into checkpoints instead of one commit per token."""

    def __init__(self) -> None:
        self._buffer: list[str] = []
        self._reset = False
        self._dirty = False
        self._last_flush = time.monotonic()

    def add(self, content: str, *, reset: bool = False) -> bool:
        """Add a delta; returns True when the caller should flush now."""
        if reset:
            self._buffer.clear()
            self._reset = True
            if content:
                self._buffer.append(content)
            self._dirty = True
            self._last_flush = time.monotonic()
            return True
        if content:
            self._buffer.append(content)
            self._dirty = True
        threshold = limit("run_delta_flush_chars")
        elapsed_ms = (time.monotonic() - self._last_flush) * 1000
        return (
            self._dirty
            and (
                sum(len(part) for part in self._buffer) >= threshold
                or elapsed_ms >= limit("run_delta_flush_ms")
            )
        )

    def take(self) -> tuple[str, bool] | None:
        if not self._dirty:
            return None
        content = "".join(self._buffer)
        reset = self._reset
        self._buffer.clear()
        self._reset = False
        self._dirty = False
        self._last_flush = time.monotonic()
        return content, reset

    @property
    def dirty(self) -> bool:
        return self._dirty
