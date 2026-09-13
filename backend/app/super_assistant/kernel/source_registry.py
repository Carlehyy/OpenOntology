"""Durable source lifecycle markers shared by memory, palace and kernel context."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .models import ContextSourceTombstone, ExecutionRun


def source_ref_dict(*, kind: str, source_id: str, revision: str, locator: str,
                    recipe_revision: str, extraction_id: str) -> dict[str, str]:
    return {
        "kind": str(kind), "id": str(source_id), "revision": str(revision),
        "locator": str(locator), "recipe_revision": str(recipe_revision),
        "extraction_id": str(extraction_id),
    }


def _missing_table(exc: OperationalError) -> bool:
    return "no such table" in str(exc).lower() or "does not exist" in str(exc).lower()


def tombstoned_source_ids(db: Session, owner_id: str, kind: str) -> set[str]:
    try:
        return set(db.scalars(select(ContextSourceTombstone.source_id).where(
            ContextSourceTombstone.owner_id == owner_id,
            ContextSourceTombstone.kind == kind,
        )).all())
    except OperationalError as exc:
        # Legacy-only test databases and pre-0109 rolling deployments do not
        # have the optional marker table yet; preserve the old read path until
        # the migration has run. Production migration failures still surface.
        if not _missing_table(exc):
            raise
        db.rollback()
        return set()


def is_source_tombstoned(db: Session, *, owner_id: str, kind: str, source_id: str) -> bool:
    try:
        return db.scalar(select(ContextSourceTombstone.id).where(
            ContextSourceTombstone.owner_id == owner_id,
            ContextSourceTombstone.kind == kind,
            ContextSourceTombstone.source_id == source_id,
        )) is not None
    except OperationalError as exc:
        if not _missing_table(exc):
            raise
        db.rollback()
        return False


def tombstone_source(
    db: Session,
    *, owner_id: str, kind: str, source_id: str, revision: str,
    locator: str, recipe_revision: str, extraction_id: str, reason: str,
    run: ExecutionRun | None = None, actor: dict[str, str] | None = None,
) -> ContextSourceTombstone:
    """Insert an idempotent deletion marker and optionally emit kernel event.

    Legacy deletion routes call this without ``run``; a Run-owned source
    retirement can pass a locked Run to obtain the ``source.tombstoned`` fact in
    the same transaction. The unique source key means retries cannot emit a
    second lifecycle marker.
    """
    try:
        existing = db.scalar(select(ContextSourceTombstone).where(
            ContextSourceTombstone.owner_id == owner_id,
            ContextSourceTombstone.kind == kind,
            ContextSourceTombstone.source_id == source_id,
        ))
    except OperationalError as exc:
        if not _missing_table(exc):
            raise
        db.rollback()
        return None  # type: ignore[return-value]
    if existing is not None:
        return existing
    row = ContextSourceTombstone(
        owner_id=owner_id, kind=kind, source_id=source_id,
        revision=str(revision), locator=str(locator),
        recipe_revision=str(recipe_revision), extraction_id=str(extraction_id),
        reason=str(reason)[:500], tombstone_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    if run is not None:
        if run.owner_id != owner_id:
            raise ValueError("source tombstone owner mismatch")
        # Lazy import keeps source registry usable by migration/bootstrap code.
        from .store import append_event
        ref = source_ref_dict(
            kind=kind, source_id=source_id, revision=revision,
            locator=locator, recipe_revision=recipe_revision,
            extraction_id=extraction_id,
        )
        command = f"source-tombstone:{kind}:{source_id}"
        append_event(
            db, run, event_type="source.tombstoned",
            payload={"source_ref": ref, "reason": str(reason)[:500], "tombstone_at": row.tombstone_at.isoformat()},
            actor=actor or {"kind": "system"}, command_id=command,
            idempotency_key=command,
        )
    return row
