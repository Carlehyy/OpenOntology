"""External CDC → Sentinel ingress adapter (no local ObjectInstance mirror).

A remote (federated) object mapping has no local rows.  Row changes observed
on the source are admitted here as release-scoped key facts plus one durable
``external_change`` outbox row, both written in the *caller's* transaction, so
a source position is acknowledged if and only if its facts are durable.

Authority is the frozen release: subscriptions and scope checks read the
project's ``current_release_id`` pointer and the immutable release snapshot.
The ontology domain never imports ``data_channel``; the composition root
injects this adapter into the integrated ingress.

Processing is per outbox event (one source row event), never through the
shared pattern cursor: event-log ids are allocated before commit, so a
late-committing source could otherwise land below an already advanced
watermark and be skipped.
"""
from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.ontologies.mappings.remote_binding import (
    BINDING_MODE_REMOTE,
    RemoteBindingError,
    remote_bindings_by_type,
    snapshot_entry_binding,
)
from app.ontologies.projects.models import OntologyProject
from app.ontologies.sentinels.cep import contract as cep_contract
from app.ontologies.sentinels.cep.event_store import (
    external_source_event_rows,
    record_external_change,
)
from app.ontologies.sentinels.models import SentinelCdcOutbox, SentinelEventLog
from app.ontologies.versions.models import OntologyVersion

logger = logging.getLogger(__name__)

EXTERNAL_CHANGE = cep_contract.OUTBOX_EVENT_EXTERNAL_CHANGE

STATUS_CAPTURED = "captured"
STATUS_DUPLICATE = "duplicate"
STATUS_STALE = "stale"
STATUS_CAPACITY_EXCEEDED = "capacity_exceeded"

# Persisted by the ingress on every relevant source when capture reports
# ``capacity_exceeded``; exported so both sides name the condition identically.
CAPACITY_ERROR_CODE = "EVENT_CAPACITY_EXCEEDED"

_IDENTITY_MAX_LENGTH = 64
_SCOPE_FIELDS = (
    "ontology_id", "release_id", "mapping_id", "object_type_id", "asset_id",
)
_EVENT_FIELDS = (
    *_SCOPE_FIELDS, "instance_id", "source_event_id", "event_group_id",
    "change_kind",
)
_DEDUPE_PREFIX = "external"  # mirrored in cep.contract.external_outbox_dedupe_key
# Source coordinates a sender may attach to the frozen post-image.  Anything
# else outside the mapped properties is dropped before persistence.
_SAFE_SNAPSHOT_COORDINATES = ("sourcePosition",)

# Transaction-scoped advisory lock: reserves event-log capacity atomically
# across sources once the log is near its cap.  It is always the last lock a
# capture takes and nothing is acquired while holding it.
_CAPACITY_ADVISORY_LOCK_KEY = 0x53454C43  # "SELC"

# Derived bindings of released snapshots, memoized per process and keyed by
# content hash (see ``release_subscriptions``).  Bounded; cleared when full.
_BINDING_CACHE_LIMIT = 128
_binding_cache: dict[tuple[str, str, str], list[dict]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def external_dedupe_key(
        *, ontology_id: str, release_id: str, asset_id: str, mapping_id: str,
        object_type_id: str, source_event_id: str) -> str:
    """Durable outbox identity of one source row event (mirrors the fact index)."""
    return cep_contract.external_outbox_dedupe_key(
        ontology_id=ontology_id, release_id=release_id, asset_id=asset_id,
        mapping_id=mapping_id, object_type_id=object_type_id,
        source_event_id=source_event_id)


def external_control_identity(
        *, asset_id: str, mapping_id: str, object_type_id: str,
        source_event_id: str, event_group_id: str = "") -> dict:
    """Identity payload stored in the outbox ``result_json.control``."""
    return {
        "kind": EXTERNAL_CHANGE,
        "assetId": str(asset_id),
        "mappingId": str(mapping_id),
        "objectTypeId": str(object_type_id),
        "sourceEventId": str(source_event_id),
        "eventGroupId": str(event_group_id or ""),
    }


def external_identity_from_row(row) -> dict | None:
    """Read one external event's identity back from an outbox row.

    The control payload is durable across claim, retry and dead-letter
    finalization, and is written in the same transaction as the facts.
    """
    payload = (
        row.result_json.get("control", {})
        if isinstance(row.result_json, dict)
        and isinstance(row.result_json.get("control"), dict) else {}
    )
    if str(payload.get("kind") or "") != EXTERNAL_CHANGE:
        return None
    source_event_id = str(payload.get("sourceEventId") or "")
    if not all((row.ontology_id, row.ontology_release_id,
                payload.get("assetId"), payload.get("mappingId"),
                payload.get("objectTypeId"), source_event_id)):
        return None
    return {
        "ontology_id": str(row.ontology_id),
        "release_id": str(row.ontology_release_id),
        "asset_id": str(payload["assetId"]),
        "mapping_id": str(payload["mappingId"]),
        "object_type_id": str(payload["objectTypeId"]),
        "source_event_id": source_event_id,
        "event_group_id": str(payload.get("eventGroupId") or ""),
    }


def _object_properties(object_type: dict) -> list[dict]:
    return [
        dict(item) for item in (object_type.get("properties") or [])
        if isinstance(item, dict) and item.get("name")
    ]


def _physical_resolution(columns: list) -> tuple[set[str], dict[str, str]]:
    """Physical column names plus logical-alias → physical mapping.

    A descriptor frozen from an asset with a logical field config carries
    columns whose ``name`` is the logical alias and whose ``source_key`` is
    the physical column.  Without a config both are the physical name.
    """
    physical: set[str] = set()
    logical_to_physical: dict[str, str] = {}
    for column in columns:
        if not isinstance(column, dict):
            continue
        name = str(column.get("name") or "")
        source_key = column.get("source_key")
        if source_key:
            physical.add(str(source_key))
            if name:
                logical_to_physical[name] = str(source_key)
        elif name:
            physical.add(name)
    return physical, logical_to_physical


def _resolve_physical(key, physical: set[str],
                      logical_to_physical: dict[str, str]) -> str | None:
    key = str(key)
    if key in physical:
        return key
    return logical_to_physical.get(key)


def _descriptor_logical_fingerprint(
        snapshot: dict | None, mapping_id: str) -> str | None:
    """Recover the frozen descriptor's logical fingerprint by mapping id."""
    for entry in (snapshot or {}).get("mappings") or []:
        if not isinstance(entry, dict) or str(entry.get("id") or "") != mapping_id:
            continue
        try:
            mode, descriptor = snapshot_entry_binding(entry)
        except RemoteBindingError:
            return None
        if mode == BINDING_MODE_REMOTE and isinstance(descriptor, dict):
            value = descriptor.get("logicalFingerprint")
            return str(value) if value else None
    return None


def release_remote_subscriptions(
        ontology_id: str, release_id: str, snapshot: dict | None) -> list[dict]:
    """Remote object bindings of one frozen release as ingress subscriptions.

    Only unambiguous bindings whose ObjectType exists in the same snapshot and
    which map at least one physical column to a declared property are
    returned.  ``field_map`` is PHYSICAL source column → ontology property
    NAME; ``primary_key_columns`` are PHYSICAL.  Snapshot field maps written
    with logical aliases are resolved through the descriptor's
    ``source_key`` map.
    """
    try:
        grouped = remote_bindings_by_type(snapshot)
    except RemoteBindingError as exc:
        logger.error(
            "release %s of ontology %s has an invalid remote binding and "
            "yields no CDC subscriptions: %s", release_id, ontology_id,
            exc.code)
        return []
    object_types = {
        str(item.get("id")): item
        for item in ((snapshot or {}).get("objectTypes") or [])
        if isinstance(item, dict) and item.get("id")
    }
    subscriptions: list[dict] = []
    for object_type_id in sorted(grouped):
        bindings = grouped[object_type_id]
        if len(bindings) != 1:
            # Refuse to guess which table owns the type's identity.
            logger.error(
                "ObjectType %s in release %s has %d remote bindings; "
                "ambiguous types are not subscribed", object_type_id,
                release_id, len(bindings))
            continue
        binding = bindings[0]
        object_type = object_types.get(object_type_id)
        if object_type is None or not binding["mappingId"]:
            continue
        properties = _object_properties(object_type)
        declared = {str(item["name"]) for item in properties}
        physical, logical_to_physical = _physical_resolution(
            binding["columns"])
        field_map: dict[str, str] = {}
        ambiguous = False
        for column, target in binding["fieldMap"].items():
            source = _resolve_physical(column, physical, logical_to_physical)
            if source is None or target not in declared:
                continue
            if field_map.setdefault(source, target) != target:
                # One physical column addressed twice with different targets.
                ambiguous = True
        primary_key_columns = []
        for column in binding["primaryKey"]:
            source = _resolve_physical(column, physical, logical_to_physical)
            if source is None:
                primary_key_columns = []
                break
            primary_key_columns.append(source)
        if ambiguous or not field_map or len(
                set(field_map.values())) != len(field_map):
            # Nothing addressable, or the column ↔ property map is not 1:1.
            continue
        if not primary_key_columns or len(set(primary_key_columns)) != len(
                primary_key_columns):
            # Row identity must stay physical and unambiguous.
            continue
        subscriptions.append({
            "ontology_id": str(ontology_id),
            "release_id": str(release_id),
            "mapping_id": binding["mappingId"],
            "object_type_id": object_type_id,
            "asset_id": binding["assetId"],
            "connection_id": binding["connectionId"],
            "schema_name": binding["schemaName"],
            "table_name": binding["tableName"],
            "schema_fingerprint": binding["schemaFingerprint"],
            "logical_fingerprint": _descriptor_logical_fingerprint(
                snapshot, binding["mappingId"]),
            "field_map": field_map,
            "primary_key_columns": primary_key_columns,
            "contract": {
                "columns": [dict(column) for column in binding["columns"]
                            if isinstance(column, dict)],
                "object_properties": properties,
            },
        })
    return subscriptions


def release_subscriptions(
        db: Session, ontology_id: str, release_id: str) -> list[dict] | None:
    """Subscriptions of one released version; ``None`` if it is not a release.

    The release is re-validated on every call.  Derived bindings are memoized
    per (ontology, release, snapshot hash): the content hash, not the id
    alone, is the identity, so a recycled id can never serve another
    snapshot's bindings.  A release without a recorded hash is never cached.
    """
    row = db.query(OntologyVersion.snapshot_hash).filter(
        OntologyVersion.id == release_id,
        OntologyVersion.ontology_id == ontology_id,
        OntologyVersion.node_kind == "release",
        OntologyVersion.lifecycle_status == "released",
    ).first()
    if row is None:
        return None
    key = (str(ontology_id), str(release_id), str(row[0])) if row[0] else None
    if key is not None and key in _binding_cache:
        return _binding_cache[key]
    snapshot = db.query(OntologyVersion.snapshot_formal).filter(
        OntologyVersion.id == release_id).scalar()
    subscriptions = release_remote_subscriptions(
        ontology_id, release_id, snapshot or {})
    if key is not None:
        if len(_binding_cache) >= _BINDING_CACHE_LIMIT:
            _binding_cache.clear()
        _binding_cache[key] = subscriptions
    return subscriptions


def clear_subscription_cache() -> None:
    _binding_cache.clear()


def _locked_current_release_id(db: Session, ontology_id: str) -> str | None:
    """Read and fence the release pointer for the caller's transaction.

    Same lock as the CDC executor: FOR KEY SHARE blocks promotion/rollback's
    pointer switch until the caller commits, and coexists with other readers.
    """
    statement = select(OntologyProject.current_release_id).where(
        OntologyProject.id == ontology_id)
    if db.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(read=True, key_share=True)
    row = db.execute(statement).first()
    current = row[0] if row is not None else None
    return str(current) if current else None


def _required_text(event: dict, field: str) -> str:
    value = event.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"external event is missing {field}")
    return value


def _validated_changes(event: dict) -> dict[str, dict]:
    changes = event.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("external event changes must be an object")
    normalized: dict[str, dict] = {}
    for key, values in changes.items():
        if (not isinstance(values, dict)
                or "old" not in values or "new" not in values):
            raise ValueError(
                "external event changes must map each key to {'old', 'new'}")
        normalized[str(key)] = {"old": values["old"], "new": values["new"]}
    return normalized


def _frozen_snapshot(event: dict, mapped: set[str]) -> dict:
    """Post-image restricted to mapped properties plus safe source coordinates.

    The stored snapshot is rebuilt from a whitelist, so an unmapped source
    column can never be persisted even if a sender includes one.
    """
    raw = event.get("snapshot")
    if not isinstance(raw, dict):
        raise ValueError("external event snapshot must be an object")
    properties = raw.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("external event snapshot is missing properties")
    snapshot = {
        "id": event["instance_id"],
        "objectTypeId": event["object_type_id"],
        "properties": {
            str(key): value for key, value in properties.items()
            if str(key) in mapped
        },
        # Derived values are not computable without a local projection.
        "computed": {},
        "source": {
            "assetId": event["asset_id"],
            "mappingId": event["mapping_id"],
            "sourceEventId": event["source_event_id"],
            "eventGroupId": event["event_group_id"],
            "changeKind": event["change_kind"],
        },
    }
    for coordinate in _SAFE_SNAPSHOT_COORDINATES:
        if raw.get(coordinate) is not None:
            snapshot[coordinate] = raw[coordinate]
    return snapshot


def _capacity_exceeded(db: Session, new_rows: int) -> int | None:
    """Row count when admitting ``new_rows`` would exceed the cap, else ``None``.

    On PostgreSQL every external capture first takes one transaction-scoped
    advisory lock, held until the caller commits, so check-then-insert is
    atomic across sources and concurrent captures cannot jointly overshoot.
    Ids only grow, so the highest id bounds the row count: the exact count is
    needed only once that bound no longer fits.  Organic (local) writers are
    not gated by this reservation.
    """
    cap = cep_contract.EVENT_LOG_MAX_ROWS
    new_rows = int(new_rows)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _CAPACITY_ADVISORY_LOCK_KEY})
    peak = int(db.query(func.max(SentinelEventLog.id)).scalar() or 0)
    if peak + new_rows <= cap:
        return None
    total = int(db.query(func.count(SentinelEventLog.id)).scalar() or 0)
    return total if total + new_rows > cap else None


def _validated_event(event: dict) -> dict:
    """Structural validation shared by :meth:`capture` and :meth:`capture_many`."""
    if not isinstance(event, dict):
        raise ValueError("external event must be an object")
    for field in _EVENT_FIELDS:
        _required_text(event, field)
    for field in ("source_event_id", "event_group_id"):
        if len(event[field]) > _IDENTITY_MAX_LENGTH:
            raise ValueError(
                f"external event {field} exceeds "
                f"{_IDENTITY_MAX_LENGTH} characters")
    if event["change_kind"] not in cep_contract.EVENT_KINDS:
        raise ValueError(
            f"unsupported external change_kind: {event['change_kind']}")
    if not isinstance(event.get("occurred_at"), datetime):
        raise ValueError("external event occurred_at must be a datetime")
    _validated_changes(event)
    return event


class ExternalCdcOntologyAdapter:
    """Ontology-side port of the integrated CDC ingress."""

    def subscriptions(self, db: Session) -> list[dict]:
        """Remote bindings of every project's current released version."""
        rows = db.query(
            OntologyProject.id, OntologyProject.current_release_id,
        ).filter(
            OntologyProject.current_release_id.isnot(None),
        ).order_by(OntologyProject.id.asc()).all()
        subscriptions: list[dict] = []
        for ontology_id, release_id in rows:
            found = release_subscriptions(
                db, str(ontology_id), str(release_id))
            if found is None:
                # Pointer does not reference a valid immutable release.
                logger.error(
                    "ontology %s points at %s which is not a released "
                    "version; no CDC subscriptions", ontology_id, release_id)
                continue
            # Callers own what they receive; the memoized bindings stay pristine.
            subscriptions.extend(deepcopy(item) for item in found)
        return subscriptions

    def capture(self, db: Session, event: dict) -> dict:
        """Admit one source row event inside the caller's transaction.

        Never commits or rolls back.  Returns ``{"status": ...}``:

        ``captured``           facts and the outbox row are pending in the
                               caller's transaction; acknowledge after commit.
        ``duplicate``          this source event was already admitted.
        ``stale``              the release is no longer current or does not
                               bind this mapping; nothing was written.
        ``capacity_exceeded``  the event log is full; nothing was written and
                               the caller must pause ingest on every source.
        """
        event = _validated_event(event)
        current = _locked_current_release_id(db, event["ontology_id"])
        return self._capture_locked(db, event, current)

    def capture_many(self, db: Session, events: list) -> list:
        """Admit a fan-out batch inside one caller transaction.

        Lock order for a batch is fixed: every distinct project pointer is
        locked (sorted by ontology id, FOR KEY SHARE) before the first fact is
        written, so the global capacity advisory lock taken inside capture is
        always the last lock and never precedes a project lock of the same
        transaction.  Results are positionally aligned with ``events``.
        """
        if not isinstance(events, list):
            raise ValueError("external events must be a list")
        scoped = [_validated_event(event) for event in events]
        currents: dict[str, str | None] = {}
        for ontology_id in sorted({event["ontology_id"] for event in scoped}):
            currents[ontology_id] = _locked_current_release_id(db, ontology_id)
        return [
            self._capture_locked(db, event, currents[event["ontology_id"]])
            for event in scoped
        ]

    def _capture_locked(self, db: Session, event: dict,
                        current: str | None) -> dict:
        """Capture body; the caller already fenced this project's pointer."""
        ontology_id = event["ontology_id"]
        release_id = event["release_id"]
        occurred_at = event["occurred_at"]
        changes = _validated_changes(event)
        if current != release_id:
            return {
                "status": STATUS_STALE, "reason": "release_not_current",
                "current_release_id": current,
            }
        subscriptions = release_subscriptions(db, ontology_id, release_id)
        binding = next((
            item for item in (subscriptions or [])
            if item["mapping_id"] == event["mapping_id"]
            and item["object_type_id"] == event["object_type_id"]
            and item["asset_id"] == event["asset_id"]
        ), None)
        if binding is None:
            return {
                "status": STATUS_STALE, "reason": "mapping_not_in_release",
                "current_release_id": current,
            }

        mapped = set(binding["field_map"].values())
        if event["change_kind"] == cep_contract.EVENT_KIND_DELETED:
            field_changes = {
                cep_contract.DELETED_EVENT_KEY: {"old": None, "new": None}}
        else:
            field_changes = {
                key: values for key, values in changes.items()
                if key in mapped
            }
        if not field_changes:
            # Only unmapped columns changed: nothing a Sentinel may observe.
            return {"status": STATUS_CAPTURED, "facts": 0, "outbox_id": None}

        identity = {
            "ontology_id": ontology_id, "release_id": release_id,
            "asset_id": event["asset_id"], "mapping_id": event["mapping_id"],
            "object_type_id": event["object_type_id"],
            "source_event_id": event["source_event_id"],
        }
        # Replays are acknowledged before the capacity gate: an already
        # admitted event must stay acknowledgeable while the log is full.
        known = external_source_event_rows(db, **identity)
        if known:
            if {str(row.key) for row in known} != set(field_changes):
                logger.error(
                    "external source event %s was resent with different "
                    "keys; the first admission stands",
                    event["source_event_id"])
            return {"status": STATUS_DUPLICATE, "facts": 0, "outbox_id": None}
        # Facts and their trigger are pruned on different clocks: a completed
        # event's facts can be gone while its outbox row still holds the
        # unique dedupe key.  That row alone proves the event was admitted.
        dedupe_key = external_dedupe_key(**identity)
        trigger = db.query(SentinelCdcOutbox.id).filter(
            SentinelCdcOutbox.dedupe_key == dedupe_key).first()
        if trigger is not None:
            return {
                "status": STATUS_DUPLICATE, "facts": 0,
                "outbox_id": str(trigger[0]),
            }

        total = _capacity_exceeded(db, len(field_changes))
        if total is not None:
            return {
                "status": STATUS_CAPACITY_EXCEEDED,
                "reason": "event_log_capacity",
                "error_code": CAPACITY_ERROR_CODE,
                "event_log_rows": total,
                "event_log_max_rows": cep_contract.EVENT_LOG_MAX_ROWS,
            }

        inserted = record_external_change(
            db, ontology_id=ontology_id, release_id=release_id,
            object_type_id=event["object_type_id"],
            instance_id=event["instance_id"],
            source_event_id=event["source_event_id"],
            event_group_id=event["event_group_id"],
            asset_id=event["asset_id"], mapping_id=event["mapping_id"],
            change_kind=event["change_kind"], field_changes=field_changes,
            snapshot=_frozen_snapshot(event, mapped),
            occurred_at=occurred_at)
        if not inserted:
            # Lost a race to a concurrent admission of the same source event.
            return {"status": STATUS_DUPLICATE, "facts": 0, "outbox_id": None}

        outbox = SentinelCdcOutbox(
            id=str(uuid.uuid4()),
            chain_id=uuid.uuid4().hex,
            ontology_id=ontology_id,
            ontology_release_id=release_id,
            event_kind=EXTERNAL_CHANGE,
            dedupe_key=dedupe_key,
            object_type_id=event["object_type_id"],
            changed_keys=sorted(field_changes),
            link_change=False,
            cascade_depth=0,
            mapping_ids=[event["mapping_id"]],
            status="cdc_pending",
            attempts=0,
            available_at=_now(),
            result_json={"control": external_control_identity(
                asset_id=event["asset_id"], mapping_id=event["mapping_id"],
                object_type_id=event["object_type_id"],
                source_event_id=event["source_event_id"],
                event_group_id=event["event_group_id"])},
        )
        db.add(outbox)
        return {
            "status": STATUS_CAPTURED, "facts": len(field_changes),
            "outbox_id": outbox.id,
        }
