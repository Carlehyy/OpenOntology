"""Release-scoped Sentinel queries and API response projections."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.sentinel import Notification, Sentinel, SentinelFiring
from app.ontologies.sentinels.dynamic_service import ORIGIN_BUILTIN
from app.shared.time_utils import utc_iso


QueryDependency = Callable[..., Any]


def _dict(sentinel: Sentinel) -> dict[str, Any]:
    return {
        "id": sentinel.id,
        "ontologyId": sentinel.ontology_id,
        "name": sentinel.name,
        "displayName": sentinel.display_name,
        "description": sentinel.description,
        "bindings": sentinel.bindings or [],
        "links": sentinel.links or [],
        "condition": sentinel.condition,
        "conditionRows": sentinel.condition_rows or [],
        "conditionLogic": sentinel.condition_logic or "and",
        "primaryAlias": sentinel.primary_alias,
        "actionIds": sentinel.action_ids or [],
        "actionParameters": sentinel.action_parameters or {},
        "onChange": sentinel.on_change,
        "onSchedule": sentinel.on_schedule,
        "scanIntervalSeconds": sentinel.scan_interval_seconds,
        "triggerMode": sentinel.trigger_mode,
        "pattern": sentinel.pattern if isinstance(
            sentinel.pattern, dict) else None,
        "muted": sentinel.muted,
        "lastScannedAt": utc_iso(sentinel.last_scanned_at),
        "enabled": sentinel.enabled,
        "status": sentinel.status,
        "enableGeneration": int(sentinel.enable_generation or 0),
        "releaseId": sentinel.bound_release_id,
        "origin": sentinel.origin,
        "source": sentinel.source,
        "createdAt": utc_iso(sentinel.created_at),
        "updatedAt": utc_iso(sentinel.updated_at),
    }


def _released_dict(
    ontology_id: str,
    release_id: str,
    raw: dict,
    live: Sentinel | None,
) -> dict[str, Any]:
    """Serialize definition fields only from the immutable release snapshot.

    ``sentinels`` is also the mutable runtime projection and can contain a draft
    that has not been promoted yet. Definition fields therefore remain frozen;
    enabled/muted and last-scanned telemetry are the deliberately mutable
    operational overlay consumed by the runtime.
    """
    operational = (
        live
        if live is not None and live.status == "published"
        else None
    )
    return {
        "id": str(raw.get("id") or ""),
        "ontologyId": ontology_id,
        "name": str(raw.get("name") or ""),
        "displayName": raw.get("displayName") or raw.get("name") or "",
        "description": raw.get("description"),
        "bindings": raw.get("bindings") or [],
        "links": raw.get("links") or [],
        "condition": raw.get("condition"),
        "conditionRows": raw.get("conditionRows") or [],
        "conditionLogic": raw.get("conditionLogic") or "and",
        "primaryAlias": raw.get("primaryAlias"),
        "actionIds": raw.get("actionIds") or [],
        "actionParameters": raw.get("actionParameters") or {},
        "onChange": bool(raw.get("onChange", True)),
        "onSchedule": bool(raw.get("onSchedule", False)),
        "scanIntervalSeconds": int(
            raw.get("scanIntervalSeconds") or 300
        ),
        "triggerMode": raw.get("triggerMode") or "on_enter",
        "pattern": raw.get("pattern") if isinstance(
            raw.get("pattern"), dict) else None,
        "muted": (
            bool(operational.muted)
            if operational is not None
            else bool(raw.get("muted", False))
        ),
        "lastScannedAt": (
            utc_iso(operational.last_scanned_at)
            if operational is not None
            else None
        ),
        "enabled": (
            bool(operational.enabled)
            if operational is not None
            else bool(raw.get("enabled", True))
        ),
        "enableGeneration": (
            int(operational.enable_generation or 0)
            if operational is not None
            else 0
        ),
        "releaseId": release_id,
        "status": "published",
        "origin": ORIGIN_BUILTIN,
        "source": raw.get("source"),
        "createdAt": None,
        "updatedAt": None,
    }


def list_sentinels(
    ontology_id: str,
    release_id: str | None,
    db: Session,
    *,
    current_release_context_fn: QueryDependency,
    released_dict_fn: QueryDependency,
) -> dict:
    release = current_release_context_fn(
        db,
        ontology_id,
        expected_release_id=release_id,
    )
    released = [
        item
        for item in release.snapshot["sentinels"]
        if isinstance(item, dict) and item.get("id")
    ]
    ids = {str(item["id"]) for item in released}
    live_by_id = (
        {
            item.id: item
            for item in db.query(Sentinel)
            .filter(
                Sentinel.ontology_id == ontology_id,
                Sentinel.origin == ORIGIN_BUILTIN,
                Sentinel.id.in_(ids),
            )
            .all()
        }
        if ids
        else {}
    )
    return {
        "data": [
            released_dict_fn(
                ontology_id,
                release.id,
                item,
                live_by_id.get(str(item["id"])),
            )
            for item in released
        ]
    }


def list_firings(
    ontology_id: str,
    sentinel_id: str | None,
    limit: int,
    release_id: str | None,
    include_history: bool,
    db: Session,
    *,
    current_release_context_fn: QueryDependency,
) -> dict:
    query = db.query(SentinelFiring).filter(
        SentinelFiring.ontology_id == ontology_id
    )
    released_names: dict[str, str] = {}
    if not include_history:
        release = current_release_context_fn(
            db,
            ontology_id,
            expected_release_id=release_id,
        )
        builtin_ids = {
            str(item["id"])
            for item in release.snapshot["sentinels"]
            if item.get("id")
        }
        dynamic_ids = {
            str(item[0])
            for item in db.query(Sentinel.id)
            .filter(
                Sentinel.ontology_id == ontology_id,
                Sentinel.origin == "assistant_dynamic",
                Sentinel.bound_release_id == release.id,
            )
            .all()
        }
        allowed_ids = builtin_ids | dynamic_ids
        released_names = {
            str(item["id"]): str(
                item.get("displayName") or item.get("name") or ""
            )
            for item in release.snapshot["sentinels"]
            if item.get("id")
        }
        # Firing lineage, rather than snapshot membership, is authoritative:
        # assistant-created overlays are release-bound but intentionally absent
        # from the built-in Sentinel snapshot.
        if not allowed_ids:
            return {"data": []}
        query = query.filter(
            SentinelFiring.ontology_release_id == release.id,
            SentinelFiring.sentinel_id.in_(allowed_ids),
        )
    if sentinel_id:
        query = query.filter(SentinelFiring.sentinel_id == sentinel_id)
    items = (
        query.order_by(SentinelFiring.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "data": [
            {
                "id": firing.id,
                "sentinelId": firing.sentinel_id,
                "sentinelName": (
                    released_names.get(firing.sentinel_id)
                    or firing.sentinel_name
                ),
                "triggerSource": firing.trigger_source,
                "status": firing.status,
                "matchCount": firing.match_count,
                "matches": firing.matches or [],
                "entered": firing.entered or [],
                "left": firing.left or [],
                "actionResults": firing.action_results or [],
                "error": firing.error,
                "durationMs": firing.duration_ms,
                "ontologyVersion": firing.ontology_version,
                "ontologyReleaseId": firing.ontology_release_id,
                "createdAt": utc_iso(firing.created_at),
            }
            for firing in items
        ]
    }


def list_notifications(
    ontology_id: str,
    limit: int,
    release_id: str | None,
    include_history: bool,
    db: Session,
    *,
    current_release_context_fn: QueryDependency,
) -> dict:
    query = db.query(Notification).filter(
        Notification.ontology_id == ontology_id
    )
    if not include_history:
        release = current_release_context_fn(
            db,
            ontology_id,
            expected_release_id=release_id,
        )
        query = query.filter(
            Notification.ontology_release_id == release.id
        )
    items = (
        query.order_by(Notification.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "data": [
            {
                "id": notification.id,
                "channel": notification.channel,
                "recipient": notification.recipient,
                "subject": notification.subject,
                "body": notification.body,
                "relatedObjectId": notification.related_object_id,
                "actionId": notification.action_id,
                "status": notification.status,
                "ontologyReleaseId": notification.ontology_release_id,
                "sentinelId": notification.sentinel_id,
                "actionLogId": notification.action_log_id,
                "createdAt": utc_iso(notification.created_at),
            }
            for notification in items
        ]
    }


def get_sentinel(
    ontology_id: str,
    sentinel_id: str,
    db: Session,
    *,
    dict_fn: QueryDependency,
) -> dict:
    sentinel = (
        db.query(Sentinel)
        .filter(
            Sentinel.id == sentinel_id,
            Sentinel.ontology_id == ontology_id,
            Sentinel.origin == ORIGIN_BUILTIN,
        )
        .first()
    )
    if not sentinel:
        raise HTTPException(404, "Sentinel not found")
    return {"data": dict_fn(sentinel)}
