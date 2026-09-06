"""Shared timestamp serialization helpers.

Application timestamps are stored as UTC. SQLite and some PostgreSQL driver /
column combinations can return those values as naive ``datetime`` objects,
though. Serializing such a value directly makes browsers interpret it as
local time and shifts audit history by the client's UTC offset.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import overload


def as_utc(value: datetime) -> datetime:
    """Interpret database-naive timestamps as UTC and normalize aware values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@overload
def utc_iso(value: datetime) -> str: ...


@overload
def utc_iso(value: None) -> None: ...


def utc_iso(value: datetime | None) -> str | None:
    """Return an ISO-8601 UTC timestamp with an explicit ``Z`` designator."""
    if value is None:
        return None
    return as_utc(value).isoformat().replace("+00:00", "Z")


def parse_temporal_text(text: str) -> datetime:
    """Parse ISO-ish temporal text with pragmatic normalization.

    Database drivers and CSV sources commonly emit ``2026-01-15 10:30:00``
    (space separator) or a trailing ``Z``; strict ``datetime.fromisoformat``
    consumers need the canonical ``T`` separator and an explicit offset, so
    normalize before parsing.  Raises ``ValueError`` on non-ISO input.
    """
    normalized = text.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    normalized = normalized.replace(" ", "T", 1)
    return datetime.fromisoformat(normalized)
