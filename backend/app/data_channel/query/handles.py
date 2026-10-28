"""In-process, bounded-TTL memory result handles.

Ephemeral results never touch PostgreSQL: no TEMP tables, no COPY, no row
tables. A handle is owned by exactly one process instance; cursors carry the
instance id so a restarted or sibling worker answers 410 instead of silently
re-executing the query. Application-owned objects are conservatively charged
against an execution reservation, separately from encoded-byte limits; this
is not a process or driver RSS meter. Reservations and published handles use
the same global and per-user quotas.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .contracts import QueryError

INSTANCE_ID = uuid.uuid4().hex


@dataclass
class MemoryHandle:
    id: str
    owner_user_id: str
    instance_id: str
    columns: list[dict]
    rows: list[dict]  # encoded, JSON-ready values keyed by output name
    total_rows: int
    result_limited: bool
    page_size: int
    created_at: datetime
    expires_at: datetime
    auth_fingerprints: dict[str, str]
    schema_fingerprints: dict[str, str]
    query_hash: str
    request_fingerprint: str | None
    request_kind: str  # "text" | "typed" — "external" cannot be re-resolved
    stored_request: dict
    source_states: list[dict]
    consistency: str
    plan: dict
    classification: str
    avg_semantics: str | None
    bytes: int
    session_kind: str | None = None
    session_id: str | None = None
    origin: Any = None  # bridge semantic request; process-memory only, never echoed
    cancelled: bool = False


@dataclass
class _Ticket:
    id: str
    owner_user_id: str
    reservation_bytes: int
    interrupt: Callable[[], None] | None = None
    cancelled: bool = False


class MemoryResultRegistry:
    """Quotas: global bytes (reserved + stored), per-user bytes and handles."""

    def __init__(self):
        self._lock = threading.Lock()
        self.handles: dict[str, MemoryHandle] = {}
        self._tickets: dict[str, _Ticket] = {}
        self._cancelled_tickets: set[str] = set()

    def sweep(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            expired = [h for h in self.handles.values() if h.expires_at <= now]
            for handle in expired:
                del self.handles[handle.id]

    def _stored_bytes(self) -> tuple[int, dict[str, int], dict[str, int]]:
        total = 0
        per_user_bytes: dict[str, int] = {}
        per_user_count: dict[str, int] = {}
        for handle in self.handles.values():
            total += handle.bytes
            per_user_bytes[handle.owner_user_id] = per_user_bytes.get(handle.owner_user_id, 0) + handle.bytes
            per_user_count[handle.owner_user_id] = per_user_count.get(handle.owner_user_id, 0) + 1
        return total, per_user_bytes, per_user_count

    def _reserved_bytes(self, *, exclude: str | None = None,
                        per_user: dict[str, int] | None = None) -> int:
        total = 0
        for ticket in self._tickets.values():
            if ticket.id == exclude:
                continue
            total += ticket.reservation_bytes
            if per_user is not None:
                per_user[ticket.owner_user_id] = (per_user.get(ticket.owner_user_id, 0)
                                                  + ticket.reservation_bytes)
        return total

    def open_ticket(self, handle_id: str, owner_user_id: str, limits, *, interrupt=None) -> _Ticket:
        self.sweep()
        with self._lock:
            if handle_id in self._tickets or handle_id in self.handles:
                raise QueryError(409, "query_id_in_use", "Query id already has a live execution or result")
            _, _, counts = self._stored_bytes()
            active = counts.get(owner_user_id, 0) + sum(
                1 for t in self._tickets.values() if t.owner_user_id == owner_user_id)
            if active >= limits.memory_max_handles_per_user:
                raise QueryError(429, "memory_quota_exceeded",
                                 "Too many active in-memory query results", quota="max_handles_per_user")
            stored, user_bytes, _ = self._stored_bytes()
            reserved_by_user: dict[str, int] = {}
            reserved = self._reserved_bytes(per_user=reserved_by_user)
            allowance = limits.memory_per_user_bytes_limit - (
                user_bytes.get(owner_user_id, 0) + reserved_by_user.get(owner_user_id, 0))
            worst_case = limits.federated_side_bytes_limit + limits.memory_result_bytes_limit
            minimum = min(worst_case, limits.memory_per_user_bytes_limit, 1024 ** 2)
            if allowance < minimum:
                raise QueryError(429, "memory_reservation_exceeded",
                                 "Per-user in-memory query budget cannot reserve another execution",
                                 quota="memory_per_user_bytes_limit")
            reservation = min(worst_case, allowance)
            if stored + reserved + reservation > limits.memory_total_bytes_limit:
                raise QueryError(429, "memory_reservation_exceeded",
                                 "Global in-memory query budget is reserved by running queries",
                                 quota="memory_total_bytes_limit")
            ticket = _Ticket(handle_id, owner_user_id, reservation, interrupt)
            self._tickets[handle_id] = ticket
            return ticket

    def close_ticket(self, handle_id: str) -> None:
        with self._lock:
            self._tickets.pop(handle_id, None)
            self._cancelled_tickets.discard(handle_id)

    def ticket_cancelled(self, handle_id: str) -> bool:
        with self._lock:
            if handle_id in self._cancelled_tickets:
                return True
            ticket = self._tickets.get(handle_id)
            return bool(ticket and ticket.cancelled)

    def register(self, handle: MemoryHandle, limits) -> None:
        """Atomically replace the publisher's reservation with its stored handle."""
        self.sweep()
        with self._lock:
            ticket = self._tickets.get(handle.id)
            if handle.id in self._cancelled_tickets or (ticket is not None and ticket.cancelled):
                raise QueryError(409, "query_cancelled", "Query execution was cancelled")
            stored, user_bytes, counts = self._stored_bytes()
            reserved_by_user: dict[str, int] = {}
            reserved = self._reserved_bytes(exclude=handle.id, per_user=reserved_by_user)
            user_total = (user_bytes.get(handle.owner_user_id, 0) + handle.bytes
                          + reserved_by_user.get(handle.owner_user_id, 0))
            if user_total > limits.memory_per_user_bytes_limit:
                raise QueryError(429, "memory_quota_exceeded",
                                 "Per-user in-memory result byte budget exceeded",
                                 quota="memory_per_user_bytes_limit", limitBytes=limits.memory_per_user_bytes_limit)
            if stored + handle.bytes + reserved > limits.memory_total_bytes_limit:
                raise QueryError(429, "memory_quota_exceeded",
                                 "Global in-memory result byte budget exceeded",
                                 quota="memory_total_bytes_limit", limitBytes=limits.memory_total_bytes_limit)
            other_tickets = sum(t.owner_user_id == handle.owner_user_id
                                for key, t in self._tickets.items() if key != handle.id)
            if counts.get(handle.owner_user_id, 0) + other_tickets >= limits.memory_max_handles_per_user:
                raise QueryError(429, "memory_quota_exceeded", "Too many in-memory result handles",
                                 quota="max_handles_per_user")
            self.handles[handle.id] = handle
            self._tickets.pop(handle.id, None)

    def get(self, handle_id: str) -> MemoryHandle | None:
        self.sweep()
        with self._lock:
            handle = self.handles.get(handle_id)
        if handle is not None and handle.expires_at <= datetime.now(timezone.utc):
            self.cancel(handle_id, handle.owner_user_id)
            return None
        return handle

    def cancel(self, handle_id: str, owner_user_id: str) -> bool:
        interrupt = None
        with self._lock:
            ticket = self._tickets.get(handle_id)
            if ticket is not None:
                if ticket.owner_user_id != owner_user_id:
                    return False
                ticket.cancelled = True
                self._cancelled_tickets.add(handle_id)
                interrupt = ticket.interrupt
            else:
                handle = self.handles.get(handle_id)
                if handle is None or handle.owner_user_id != owner_user_id:
                    return False
                handle.cancelled = True
                del self.handles[handle_id]
        if interrupt is not None:
            interrupt()
        return True


REGISTRY = MemoryResultRegistry()


def new_handle_id() -> str:
    return uuid.uuid4().hex


def handle_expiry(ttl_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
