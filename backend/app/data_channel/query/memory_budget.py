"""Conservative accounting for application-owned memory, independent of wire bytes.

``QueryExecutionBudget`` is the cumulative per-layer + whole-logical-query
resource ledger: counts and measured transfer bytes only, never source row
values. One instance is shared by every executor stage of a single logical
query and never resets; per-side/per-stage limits keep their own checks.
"""
from __future__ import annotations

import sys
import time
from dataclasses import fields, is_dataclass
from typing import Any

from .contracts import QueryError


def resident_size(value) -> int:
    size = sys.getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        # Plain dataclass state (e.g. a continuation cursor position): charge
        # the declared fields so nested key material is not silently free.
        size += sum(resident_size(getattr(value, f.name))
                    for f in fields(value) if f.name != "lock")
    if isinstance(value, dict):
        size += sum(resident_size(key) + resident_size(item) for key, item in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(resident_size(item) for item in value)
    elif isinstance(value, memoryview):
        size += value.nbytes
    return size


class MemoryBudget:
    # Shared values and discarded intermediates remain charged. This deliberately
    # overcounts application allocations; it is not a process/driver RSS meter.
    def __init__(self, limit: int):
        self.limit = limit
        self.charged = 0

    def check_temporary(self, value) -> None:
        self.check_bytes(resident_size(value))

    def check_bytes(self, size: int) -> None:
        self._check(size)

    def charge_bytes(self, size: int) -> int:
        self._check(size)
        self.charged += size
        return size

    def charge(self, value, *, overhead: int = 64) -> int:
        return self.charge_bytes(resident_size(value) + overhead)

    def release_bytes(self, size: int) -> None:
        """Release memory for an intermediate that is no longer retained.

        The execution budget tracks live application-owned intermediates. A
        federated join may discard a whole keyed-pull batch after merging it;
        keeping those historical allocations charged would reject bounded
        results solely because the query ran in multiple chunks.
        """
        if size < 0:
            raise ValueError("released memory must be non-negative")
        self.charged = max(0, self.charged - size)

    def _check(self, size: int) -> None:
        if self.charged + size > self.limit:
            raise QueryError(422, "query_budget_exceeded",
                             "Application-owned memory exceeds the execution reservation",
                             budget="memory_execution_bytes", limitBytes=self.limit)


_LAYER_KEYS = ("rows", "bytes", "statements", "countChecks")


class QueryExecutionBudget:
    """Cumulative per-layer + whole-logical-query ledger (counts only).

    One instance is shared by every stage of one logical query — the main
    executor, derived-constraint check executors, and lazy continuation pages
    — and is never reset mid-query. ``layer`` names are stable identifiers
    (source aliases); per-layer records hold counters only, never row values.

    Charging contract:

    - ``charge_statement``/``charge_rows`` sit at the source-read boundary of
      ``_execute_statement``: one statement per executed source statement, and
      rows charged the moment data arrives — per provider batch (a provider
      materializes the whole batch before yielding it, so the batch IS the
      transfer unit and a mid-batch refusal must still report every row the
      source already sent) or per fetched row for single-row platform
      streams.
    - ``charge_bytes`` is charged only where a path already measures encoded
      transfer bytes (COPY-encoded side measurements, encoded result rows); no
      second measurement pass is added.
    - ``charge_count_check`` discloses a COUNT/uniqueness probe: the statement
      itself is charged once at the central read boundary and its single
      returned aggregate row is charged as a row; the marker only discloses
      the probe. The source-side scan it triggers is bounded by the deadline
      clock and is never claimed as transferred rows or wire bytes.
    - Time uses the owning service's started/deadline clock (``started``); the
      memory/concurrency dimensions are the shared ``MemoryBudget`` reservation
      plus the registry ticket quotas — reported in ``snapshot()``, never
      duplicated or re-enforced here. ``snapshot()["timeoutMs"]`` is that one
      whole-logical-query deadline budget (``statement_timeout_ms`` over the
      started clock); a lazy continuation page additionally runs under its own
      tighter per-page statement timeout reported by the continuation layer,
      which never widens this clock.

    Totals are enforced against ``execution_total_rows_limit``,
    ``execution_total_bytes_limit`` and ``execution_source_statements_limit``.
    A rejection names the dimension, the whole-logical-query layer, the stage
    being charged and every layer's counters; it states the query is
    incomplete with no full result (never "not executed") and never claims
    resources were released — ``mark_released`` records only releases that
    have actually completed (caller invokes it after the real close), and a
    failed cleanup must not be reported as released.
    """

    def __init__(self, limits, *, memory_budget: MemoryBudget | None = None,
                 started: float | None = None):
        self.total_rows_limit = limits.execution_total_rows_limit
        self.total_bytes_limit = limits.execution_total_bytes_limit
        self.source_statements_limit = limits.execution_source_statements_limit
        self.timeout_ms = limits.statement_timeout_ms
        self.memory_budget = memory_budget
        self.started = time.monotonic() if started is None else started
        self._layers: dict[str, dict[str, int]] = {}
        self._releases: list[dict[str, Any]] = []
        self._rows = 0
        self._bytes = 0
        self._statements = 0
        self._count_checks = 0

    # -- charging ---------------------------------------------------------------

    def _layer(self, layer: str) -> dict[str, int]:
        return self._layers.setdefault(layer, dict.fromkeys(_LAYER_KEYS, 0))

    def _reject(self, budget: str, limit: int, actual: int, stage: str) -> None:
        raise QueryError(
            422, "query_budget_exceeded",
            "Logical query cumulative budget exceeded; the query is incomplete "
            "with no full result and no partial rows are returned",
            layer="logical_query", budget=budget, limit=limit, actual=actual,
            stage=stage, byLayer=self.layer_totals())

    def charge_statement(self, layer: str, *, times: int = 1) -> None:
        if times < 0:
            raise ValueError("charged statements must be non-negative")
        if times == 0:
            return
        entry = self._layer(layer)
        entry["statements"] += times
        self._statements += times
        if self._statements > self.source_statements_limit:
            self._reject("execution_source_statements_limit",
                         self.source_statements_limit, self._statements, layer)

    def ensure_statement_room(self, layer: str) -> None:
        """Refuse BEFORE further source work when the statement budget is full.

        Callers that are about to issue an optional source round trip (the
        cursor liveness probe) check room first: an exhausted whole-query
        statement budget must reject without sending anything, and an expired
        clock is handled by the caller's own deadline. Nothing is charged —
        the charge happens only if the round trip actually runs.
        """
        if self._statements + 1 > self.source_statements_limit:
            self._reject("execution_source_statements_limit",
                         self.source_statements_limit, self._statements + 1, layer)

    def charge_count_check(self, layer: str) -> None:
        # Disclosure marker only: a COUNT/uniqueness probe is a real source
        # statement (charged once at the central read boundary) that scans its
        # alias relation source-side and returns exactly one aggregate row
        # (charged as one row there). The scan cost is bounded by the deadline
        # clock; it is never claimed as transferred rows or wire bytes.
        self._layer(layer)["countChecks"] += 1
        self._count_checks += 1

    def charge_rows(self, layer: str, count: int = 1) -> None:
        if count < 0:
            raise ValueError("charged rows must be non-negative")
        entry = self._layer(layer)
        entry["rows"] += count
        self._rows += count
        if self._rows > self.total_rows_limit:
            self._reject("execution_total_rows_limit",
                         self.total_rows_limit, self._rows, layer)

    def charge_bytes(self, layer: str, size: int) -> None:
        if size < 0:
            raise ValueError("charged bytes must be non-negative")
        entry = self._layer(layer)
        entry["bytes"] += size
        self._bytes += size
        if self._bytes > self.total_bytes_limit:
            self._reject("execution_total_bytes_limit",
                         self.total_bytes_limit, self._bytes, layer)

    # -- observability ------------------------------------------------------------

    def mark_released(self, what: str) -> None:
        """Record a resource release that has actually completed.

        The caller (service stack, registry ticket, handle) performs the real
        close first and records it here afterwards; entries are confirmed
        events, never intentions, and a cleanup that failed must not be
        recorded. Release facts never appear in ``_reject`` — an error only
        lists what has really been released by the time it is raised.
        """
        self._releases.append({"what": what,
                               "elapsedMs": round((time.monotonic() - self.started) * 1000)})

    def layer_usage(self, layer: str) -> dict[str, int]:
        return dict(self._layers.get(layer, dict.fromkeys(_LAYER_KEYS, 0)))

    def layer_totals(self) -> dict[str, dict[str, int]]:
        return {layer: dict(entry) for layer, entry in self._layers.items()}

    def snapshot(self) -> dict:
        memory = None
        if self.memory_budget is not None:
            memory = {"chargedBytes": self.memory_budget.charged,
                      "limitBytes": self.memory_budget.limit}
        return {
            "elapsedMs": round((time.monotonic() - self.started) * 1000),
            "timeoutMs": self.timeout_ms,
            "totals": {"rows": self._rows, "bytes": self._bytes,
                       "statements": self._statements, "countChecks": self._count_checks},
            "limits": {"totalRows": self.total_rows_limit,
                       "totalBytes": self.total_bytes_limit,
                       "sourceStatements": self.source_statements_limit},
            "byLayer": self.layer_totals(),
            "memory": memory,
            "released": [dict(item) for item in self._releases],
        }
