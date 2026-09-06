"""Current-release repair and publishable definition snapshots.

These helpers may add or repair release rows and therefore flush pending ORM
state, but they never commit. The calling application flow owns its transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.ontologies.formal_modeling import schemas as FS
from app.ontologies.formal_modeling.models import (
    ActionType as FoActionType,
    LinkType as FoLinkType,
    ObjectType as FoObjectType,
    OntologyFunction as FoFunction,
)
from app.ontologies.mappings.models import OntologyLinkMapping, OntologyMapping
from app.ontologies.projects.models import OntologyProject
from app.ontologies.sentinels.models import Sentinel
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot,
    json_safe,
    snapshot_hash,
)
from app.ontologies.versions.models import OntologyVersion


def snapshot_release_sentinel(item: Sentinel) -> dict:
    return json_safe({
        "id": item.id,
        "name": item.name,
        "displayName": item.display_name,
        "description": item.description,
        "bindings": item.bindings or [],
        "links": item.links or [],
        "condition": item.condition,
        "pattern": item.pattern if isinstance(item.pattern, dict) else None,
        "conditionRows": item.condition_rows or [],
        "conditionLogic": item.condition_logic or "and",
        "primaryAlias": item.primary_alias,
        "actionIds": item.action_ids or [],
        "actionParameters": item.action_parameters or {},
        "onChange": bool(item.on_change),
        "onSchedule": bool(item.on_schedule),
        "scanIntervalSeconds": item.scan_interval_seconds,
        "triggerMode": item.trigger_mode,
        "muted": bool(item.muted),
        "enabled": bool(item.enabled),
        "status": item.status,
        "source": item.source,
    })


def snapshot_sentinel_models(
    snapshot: dict,
    *,
    snapshot_completer: Callable[[dict | None], dict] = complete_snapshot,
) -> list[SimpleNamespace]:
    """Build the lightweight Sentinel model used by isolated trial checks."""
    result = []
    for item in snapshot_completer(snapshot)["sentinels"]:
        result.append(SimpleNamespace(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            display_name=str(
                item.get("displayName") or item.get("name") or ""
            ),
            bindings=item.get("bindings") or [],
            links=item.get("links") or [],
            condition=item.get("condition"),
            primary_alias=item.get("primaryAlias"),
            action_ids=item.get("actionIds") or [],
            action_parameters=item.get("actionParameters") or {},
            trigger_mode=item.get("triggerMode") or "on_enter",
        ))
    return result


def _snapshot_mapping(item: OntologyMapping) -> dict:
    return json_safe({
        "id": item.id,
        "curatedDatasetId": item.curated_dataset_id,
        "entityClass": item.entity_class,
        "fieldMapping": item.field_mapping or {},
        "targetObjectTypeId": item.target_object_type_id,
        "status": item.status,
        "confidence": item.confidence,
    })


def _snapshot_link_mapping(item: OntologyLinkMapping) -> dict:
    return json_safe({
        "id": item.id,
        "srcDatasetId": item.src_dataset_id,
        "tgtDatasetId": item.tgt_dataset_id,
        "relationType": item.relation_type,
        "srcKey": item.src_key,
        "tgtKey": item.tgt_key,
        "status": item.status,
        "linkTypeId": item.link_type_id,
        "edgeDatasetId": item.edge_dataset_id,
        "fieldMapping": item.field_mapping or {},
    })


def collect_publishable_snapshot(db: Session, ontology_id: str) -> dict:
    """Serialize the mutable publishable definitions into a complete snapshot.

    Runtime instances, Sentinel match state, and execution logs are deliberately
    excluded because they belong to the Facts/runtime layer.
    """

    def query(model):
        return db.query(model).filter(model.ontology_id == ontology_id).all()

    return {
        "objectTypes": [
            FS.ObjectTypeOut.model_validate(item).model_dump(
                mode="json",
                by_alias=True,
            )
            for item in query(FoObjectType)
        ],
        "linkTypes": [
            FS.LinkTypeOut.model_validate(item).model_dump(
                mode="json",
                by_alias=True,
            )
            for item in query(FoLinkType)
        ],
        "actions": [
            FS.ActionTypeOut.model_validate(item).model_dump(
                mode="json",
                by_alias=True,
            )
            for item in query(FoActionType)
        ],
        "functions": [
            FS.FunctionOut.model_validate(item).model_dump(
                mode="json",
                by_alias=True,
            )
            for item in query(FoFunction)
        ],
        "sentinels": [
            snapshot_release_sentinel(item)
            for item in db.query(Sentinel).filter(
                Sentinel.ontology_id == ontology_id,
                Sentinel.origin == "release_builtin",
            ).all()
        ],
        "mappings": [
            _snapshot_mapping(item)
            for item in query(OntologyMapping)
        ],
        "linkMappings": [
            _snapshot_link_mapping(item)
            for item in query(OntologyLinkMapping)
        ],
    }


def resolve_current_release(
    db: Session,
    project: OntologyProject,
    *,
    snapshot_loader: Callable[[Session, str], dict] | None = None,
) -> OntologyVersion:
    """Resolve the current immutable release and repair legacy baselines.

    A historical project without a valid release receives a complete v0
    migration baseline. A historical release with missing snapshot/hash fields
    is repaired from observable current definitions. This function only
    flushes; the caller retains commit/rollback ownership.
    """
    if snapshot_loader is None:
        snapshot_loader = collect_publishable_snapshot

    current = None
    if project.current_release_id:
        current = db.query(OntologyVersion).filter(
            OntologyVersion.id == project.current_release_id,
            OntologyVersion.ontology_id == project.id,
            OntologyVersion.node_kind == "release",
            OntologyVersion.lifecycle_status == "released",
        ).first()
    if current is None:
        current = db.query(OntologyVersion).filter(
            OntologyVersion.ontology_id == project.id,
            OntologyVersion.node_kind == "release",
            OntologyVersion.lifecycle_status == "released",
        ).order_by(
            desc(OntologyVersion.published_at),
            desc(OntologyVersion.created_at),
        ).first()
    if current is None:
        snapshot = complete_snapshot(snapshot_loader(db, project.id))
        release_id = str(uuid.uuid4())
        current = OntologyVersion(
            id=release_id,
            ontology_id=project.id,
            version_number="v0",
            version_label="迁移基线",
            description="从升级前当前完整结构生成",
            base_release_id=release_id,
            node_kind="release",
            lifecycle_status="released",
            revision=0,
            snapshot_formal=snapshot,
            snapshot_hash=snapshot_hash(snapshot),
            published_at=datetime.now(timezone.utc),
            created_by=project.created_by,
        )
        db.add(current)
        db.flush()
    elif current.snapshot_formal is None:
        current.snapshot_formal = complete_snapshot(
            snapshot_loader(db, project.id)
        )
        current.snapshot_hash = snapshot_hash(current.snapshot_formal)
        current.published_at = current.published_at or current.created_at
        db.flush()
    elif not current.snapshot_hash:
        current.snapshot_formal = complete_snapshot(current.snapshot_formal)
        current.snapshot_hash = snapshot_hash(current.snapshot_formal)
        current.published_at = current.published_at or current.created_at
        db.flush()
    if project.current_release_id != current.id:
        project.current_release_id = current.id
        project.version = current.version_number
        db.flush()
    return current
