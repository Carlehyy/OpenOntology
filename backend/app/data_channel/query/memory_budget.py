"""Conservative accounting for application-owned memory, independent of wire bytes."""
from __future__ import annotations

import sys

from .contracts import QueryError


def resident_size(value) -> int:
    size = sys.getsizeof(value)
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
