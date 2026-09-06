"""Snapshot restoration and atomic rollback activation service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.ontologies import cache as ontology_cache
from app.ontologies.actions.models import Action
from app.ontologies.entities.models import Entity
from app.ontologies.formal_modeling.models import (
    ActionType as FoActionType,
    LinkInstance as FoLinkInstance,
    LinkType as FoLinkType,
    ObjectInstance as FoObjectInstance,
    ObjectType as FoObjectType,
    OntologyFunction as FoFunction,
)
from app.ontologies.inference.models import AuditLog
from app.ontologies.logic.models import LogicRule
from app.ontologies.mappings.models import OntologyLinkMapping, OntologyMapping
from app.ontologies.projects.models import OntologyProject
from app.ontologies.relations.models import Relation
from app.ontologies.sentinels.models import Sentinel, SentinelMatchState
from app.ontologies.versions.models import OntologyVersion


def _restore_formal_snapshot(
    db: Session, ontology_id: str, snap: dict, *, FS, _json_safe,
) -> dict:
    """只恢复定义；Object/Link Instance 始终保留，交由回滚门禁校验。"""
    for model in (FoLinkType, FoActionType, FoFunction, FoObjectType):
        db.query(model).filter(model.ontology_id == ontology_id).delete(
            synchronize_session=False)

    def restore(model, create_schema, items):
        for item in items or []:
            parsed = create_schema.model_validate(item)
            data = parsed.model_dump(exclude_none=False)
            if "source" in item:
                data["source"] = _json_safe(item.get("source"))
            db.add(model(
                id=item.get("id") or str(uuid.uuid4()),
                ontology_id=ontology_id,
                **data,
            ))

    restore(FoObjectType, FS.ObjectTypeCreate, snap.get("objectTypes"))
    restore(FoLinkType, FS.LinkTypeCreate, snap.get("linkTypes"))
    restore(FoActionType, FS.ActionTypeCreate, snap.get("actions"))
    restore(FoFunction, FS.FunctionCreate, snap.get("functions"))

    # 旧版快照没有下列 key 时保留当前定义，不把“未快照”误解为空集合。
    if "sentinels" in snap:
        builtin_ids = [item[0] for item in db.query(Sentinel.id).filter(
            Sentinel.ontology_id == ontology_id,
            Sentinel.origin == "release_builtin",
        ).all()]
        db.query(SentinelMatchState).filter(
            SentinelMatchState.ontology_id == ontology_id,
            SentinelMatchState.sentinel_id.in_(builtin_ids or [""]),
        ).delete(synchronize_session=False)
        from app.ontologies.sentinels.models import (
            SentinelPatternCursor,
            SentinelPatternState,
        )
        db.query(SentinelPatternState).filter(
            SentinelPatternState.ontology_id == ontology_id,
            SentinelPatternState.sentinel_id.in_(builtin_ids or [""]),
        ).delete(synchronize_session=False)
        db.query(SentinelPatternCursor).filter(
            SentinelPatternCursor.ontology_id == ontology_id,
            SentinelPatternCursor.sentinel_id.in_(builtin_ids or [""]),
        ).delete(synchronize_session=False)
        db.query(Sentinel).filter(
            Sentinel.ontology_id == ontology_id,
            Sentinel.origin == "release_builtin",
        ).delete(synchronize_session=False)
        for item in snap.get("sentinels") or []:
            db.add(Sentinel(
                id=item.get("id") or str(uuid.uuid4()),
                ontology_id=ontology_id,
                name=item.get("name") or "",
                display_name=item.get("displayName") or item.get("name") or "",
                description=item.get("description"),
                bindings=_json_safe(item.get("bindings") or []),
                links=_json_safe(item.get("links") or []),
                condition=item.get("condition"),
                pattern=_json_safe(item.get("pattern"))
                if isinstance(item.get("pattern"), dict) else None,
                condition_rows=_json_safe(item.get("conditionRows") or []),
                condition_logic=item.get("conditionLogic") or "and",
                primary_alias=item.get("primaryAlias"),
                action_ids=_json_safe(item.get("actionIds") or []),
                action_parameters=_json_safe(item.get("actionParameters") or {}),
                on_change=bool(item.get("onChange", True)),
                on_schedule=bool(item.get("onSchedule", False)),
                scan_interval_seconds=int(item.get("scanIntervalSeconds") or 300),
                trigger_mode=item.get("triggerMode") or "on_enter",
                muted=bool(item.get("muted", False)),
                enabled=bool(item.get("enabled", True)),
                status=item.get("status") or "draft",
                origin="release_builtin",
                source=_json_safe(item.get("source")),
            ))

    if "mappings" in snap:
        db.query(OntologyMapping).filter(
            OntologyMapping.ontology_id == ontology_id,
        ).delete(synchronize_session=False)
        for item in snap.get("mappings") or []:
            db.add(OntologyMapping(
                id=item.get("id") or str(uuid.uuid4()),
                ontology_id=ontology_id,
                curated_dataset_id=item.get("curatedDatasetId"),
                entity_class=item.get("entityClass") or "",
                field_mapping=_json_safe(item.get("fieldMapping") or {}),
                target_object_type_id=item.get("targetObjectTypeId"),
                status=item.get("status") or "draft",
                confidence=item.get("confidence"),
            ))

    if "linkMappings" in snap:
        db.query(OntologyLinkMapping).filter(
            OntologyLinkMapping.ontology_id == ontology_id,
        ).delete(synchronize_session=False)
        for item in snap.get("linkMappings") or []:
            db.add(OntologyLinkMapping(
                id=item.get("id") or str(uuid.uuid4()),
                ontology_id=ontology_id,
                src_dataset_id=item.get("srcDatasetId"),
                tgt_dataset_id=item.get("tgtDatasetId"),
                relation_type=item.get("relationType") or "",
                src_key=item.get("srcKey") or "",
                tgt_key=item.get("tgtKey") or "",
                status=item.get("status") or "draft",
                link_type_id=item.get("linkTypeId"),
                edge_dataset_id=item.get("edgeDatasetId"),
                field_mapping=_json_safe(item.get("fieldMapping") or {}),
            ))

    # SessionLocal deliberately uses ``autoflush=False``. Promotion and
    # rollback immediately query the definitions restored above in order to
    # publish Sentinels, pin mappings and validate the candidate. Without an
    # explicit flush those queries see an empty/old projection, leaving new
    # mappings as ``draft`` and their dataset-version lineage unset until the
    # later validation fails. Make restoration an observable unit before any
    # caller continues.
    db.flush()

    return {
        "objectTypes": len(snap.get("objectTypes") or []),
        "linkTypes": len(snap.get("linkTypes") or []),
        "actions": len(snap.get("actions") or []),
        "functions": len(snap.get("functions") or []),
        "sentinels": len(snap.get("sentinels") or []) if "sentinels" in snap else None,
        "mappings": len(snap.get("mappings") or []) if "mappings" in snap else None,
        "linkMappings": len(snap.get("linkMappings") or []) if "linkMappings" in snap else None,
        "retainedInstances": db.query(FoObjectInstance).filter(
            FoObjectInstance.ontology_id == ontology_id).count(),
        "retainedLinkInstances": db.query(FoLinkInstance).filter(
            FoLinkInstance.ontology_id == ontology_id).count(),
        # 保留旧客户端字段，新契约下永远为 0。
        "prunedInstances": 0,
        "prunedLinkInstances": 0,
    }


def rollback_version(
    ontology_id: str,
    version_id: str,
    db: Session,
    current_user: Any,
    *,
    _ontology_build_lock,
    _rollback_version_locked,
):
    """Activate a new release whose definitions come from a historic release.

    A rollback is a new deployment event, never pointer reuse. Runtime rows are
    rebound to the new immutable activation id while facts, firings and
    approvals keep the release ids under which they were originally produced.
    """
    with _ontology_build_lock(db, ontology_id):
        result = _rollback_version_locked(
            ontology_id, version_id, db, current_user)
    # 回滚生成新激活版本并移动发布指针：版本树整体换键（fail-open）。
    ontology_cache.invalidate_version_tree()
    return result


def _rollback_version_locked(
    ontology_id: str,
    version_id: str,
    db: Session,
    current_user: Any,
    *,
    settings,
    snapshot_hash,
    _current_release,
    _diff_formal,
    _gate_error,
    _invalidate_dynamic_sentinels_for_release,
    _json_safe,
    _next_release_activation_number,
    _rebuild_required_query_projections,
    _release_errors,
    _restore_formal_snapshot,
    _snapshot_formal,
    _version_payload,
    recompute_instance_derived,
):
    v = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if not v:
        raise HTTPException(404, "Version not found")
    if v.node_kind != "release" or v.lifecycle_status != "released":
        raise HTTPException(409, detail={
            "code": "draft_cannot_rollback",
            "message": "草稿不能成为运行版本；请先完成试跑并晋级",
        })
    if v.snapshot_formal is None:
        raise HTTPException(409, detail={
            "code": "legacy_snapshot_incomplete",
            "message": "目标发布节点缺少完整结构快照，不能安全激活",
        })

    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).with_for_update().first()
    if not project:
        raise HTTPException(404, "Ontology not found")
    current = _current_release(db, project)
    activation = None
    formal_restored = None
    projection_check = None

    from app.ontologies.projection_state import snapshot as projection_snapshot
    current_projection = projection_snapshot(db, ontology_id)
    if current_projection.status != "ready":
        raise HTTPException(503, detail={
            "code": "ontology_projection_not_ready",
            "message": "当前本体查询投影未就绪，禁止回滚；请先执行图修复",
            "projectionStatus": current_projection.status,
        })

    # Block readers before a rollback candidate is written to Neo4j. The
    # outer advisory ontology lock remains held across this fence commit.
    from app.ontologies.projection_state import mark_projecting
    mark_projecting(db, ontology_id)
    db.commit()
    try:
        # The fence is already durable. Re-querying the project can still fail,
        # so it must be covered by the same rollback/compensation path as the
        # candidate restore below; otherwise ``projecting`` can be stranded.
        project = db.query(OntologyProject).filter(
            OntologyProject.id == ontology_id,
        ).with_for_update().first()
        if project is None:
            raise HTTPException(404, "Ontology not found")

        # 旧扁平投影也随版本恢复；先删关系，再删实体，避免 FK 顺序错误。
        db.query(Relation).filter(Relation.ontology_id == ontology_id).delete(
            synchronize_session=False)
        db.query(Entity).filter(Entity.ontology_id == ontology_id).delete(
            synchronize_session=False)
        db.query(LogicRule).filter(LogicRule.ontology_id == ontology_id).delete(
            synchronize_session=False)
        db.query(Action).filter(Action.ontology_id == ontology_id).delete(
            synchronize_session=False)

        for item in (v.snapshot_entities or []):
            db.add(Entity(
                id=item.get("id") or str(uuid.uuid4()),
                ontology_id=ontology_id,
                name_cn=item.get("name_cn") or "",
                name_en=item.get("name_en") or "",
                type=item.get("type") or "",
                description=item.get("description") or "",
                confidence=item.get("confidence", 1.0),
                properties=_json_safe(item.get("properties") or {}),
            ))
        for item in (v.snapshot_relations or []):
            db.add(Relation(
                id=item.get("id") or str(uuid.uuid4()),
                ontology_id=ontology_id,
                source_entity=item.get("source_entity") or "",
                target_entity=item.get("target_entity") or "",
                type=item.get("type") or "关联",
                confidence=item.get("confidence", 1.0),
                properties=_json_safe(item.get("properties") or {}),
            ))
        for item in (v.snapshot_logic or []):
            db.add(LogicRule(
                id=item.get("id") or str(uuid.uuid4()),
                ontology_id=ontology_id,
                name_cn=item.get("name_cn") or "",
                formula=item.get("formula"),
                enabled=bool(item.get("enabled", True)),
                status=item.get("status") or "draft",
            ))
        for item in (v.snapshot_actions or []):
            db.add(Action(
                id=item.get("id") or str(uuid.uuid4()),
                ontology_id=ontology_id,
                name_cn=item.get("name_cn") or "",
                enabled=bool(item.get("enabled", True)),
                status=item.get("status") or "draft",
            ))

        formal_restored = _restore_formal_snapshot(
            db, ontology_id, dict(v.snapshot_formal or {}))
        for sentinel in db.query(Sentinel).filter(
                Sentinel.ontology_id == ontology_id,
                Sentinel.origin == "release_builtin").all():
            sentinel.status = "published"
        restored_object_types = {
            item.id: item
            for item in db.query(FoObjectType).filter(
                FoObjectType.ontology_id == ontology_id).all()
        }
        for instance in db.query(FoObjectInstance).filter(
                FoObjectInstance.ontology_id == ontology_id).all():
            # No computed value from the release being left is authoritative
            # under the restored function set. Server-owned values are rebuilt
            # after the new activation becomes the transaction's current
            # release; legacy/browser-only values remain absent.
            instance.computed = {}

        # flush 后在“快照定义 + 原有实例”的合并视图上重跑发布门禁。
        # 任何悬挂/类型/主键/基数错误都不能通过删实例“修好”。
        db.flush()
        errors = _release_errors(db, ontology_id)
        if errors:
            db.rollback()
            raise HTTPException(409, detail={
                "code": "rollback_validation_failed",
                "message": f"回滚快照与当前运行实例不兼容（{len(errors)} 个错误）",
                "errors": errors,
            })

        activation_id = str(uuid.uuid4())
        activation_number = _next_release_activation_number(db, ontology_id)
        activation_snapshot = _snapshot_formal(db, ontology_id)
        activation = OntologyVersion(
            id=activation_id,
            ontology_id=ontology_id,
            version_number=activation_number,
            version_label=f"回滚至 {v.version_number}",
            description=v.description or "",
            parent_version_id=current.id,
            base_release_id=activation_id,
            promoted_from_id=None,
            node_kind="release",
            lifecycle_status="released",
            revision=0,
            snapshot_entities=_json_safe(v.snapshot_entities or []),
            snapshot_relations=_json_safe(v.snapshot_relations or []),
            snapshot_logic=_json_safe(v.snapshot_logic or []),
            snapshot_actions=_json_safe(v.snapshot_actions or []),
            snapshot_formal=activation_snapshot,
            snapshot_hash=snapshot_hash(activation_snapshot),
            canvas_layout=_json_safe(v.canvas_layout or {}),
            snapshot_semantic=_json_safe(v.snapshot_semantic),
            published_at=datetime.now(timezone.utc),
            change_summary={
                "formal": _diff_formal(
                    current.snapshot_formal, activation_snapshot),
                "rollback": {
                    "targetReleaseId": v.id,
                    "targetVersionNumber": v.version_number,
                    "previousReleaseId": current.id,
                    "previousVersionNumber": current.version_number,
                },
            },
            created_by=current_user.id,
        )
        db.add(activation)
        # Persist the FK/self-FK target before rebinding the project and runtime
        # projection to the new activation id.
        db.flush()
        db.query(FoObjectInstance).filter(
            FoObjectInstance.ontology_id == ontology_id,
        ).update(
            {FoObjectInstance.ontology_release_id: activation.id},
            synchronize_session="fetch",
        )
        db.query(FoLinkInstance).filter(
            FoLinkInstance.ontology_id == ontology_id,
        ).update(
            {FoLinkInstance.ontology_release_id: activation.id},
            synchronize_session="fetch",
        )
        project.version = activation.version_number
        project.status = "published"
        project.current_release_id = activation.id
        db.flush()

        # Retained objects must not carry computed values produced by the
        # release being left. Recompute against the restored target functions
        # after rebinding every row to the new activation.
        for instance in db.query(FoObjectInstance).filter(
                FoObjectInstance.ontology_id == ontology_id).all():
            object_type = restored_object_types.get(instance.object_type_id)
            recompute_instance_derived(
                db,
                ontology_id=ontology_id,
                instance=instance,
                object_type=object_type,
                caused_by=activation.id,
            )
        db.flush()
        post_activation_errors = _release_errors(db, ontology_id)
        if post_activation_errors:
            raise HTTPException(409, detail={
                "code": "rollback_validation_failed",
                "message": (
                    "回滚快照重算派生投影后不满足发布契约"
                    f"（{len(post_activation_errors)} 个错误）"),
                "errors": post_activation_errors,
            })

        invalidated_dynamic_sentinels = _invalidate_dynamic_sentinels_for_release(
            db, ontology_id, activation.id)
        db.add(AuditLog(
            id=str(uuid.uuid4()),
            ontology_id=ontology_id,
            event_type="rollback",
            event_subtype="release_activated",
            user_id=current_user.id,
            user_name=current_user.username,
            description=(
                f"从 {v.version_number} 快照激活新发布版本 "
                f"{activation.version_number}"),
            object_type="ontology_version",
            object_id=activation.id,
            meta={
                "target_release_id": v.id,
                "target_version_number": v.version_number,
                "previous_release_id": current.id,
                "activation_release_id": activation.id,
                "activation_version_number": activation.version_number,
                "invalidated_dynamic_sentinels": invalidated_dynamic_sentinels,
            },
        ))
        db.flush()
        if settings.environment != "test":
            try:
                projection_check = _rebuild_required_query_projections(
                    db, ontology_id)
            except Exception as projection_exc:  # noqa: BLE001
                raise HTTPException(503, detail={
                    "code": "rollback_projection_not_ready",
                    "message": (
                        "Neo4j 构建回滚候选投影时失败；"
                        "发布激活事务已回滚"),
                    "projection": {
                        "ready": False,
                        "error": str(projection_exc),
                    },
                }) from projection_exc
            if not projection_check["ready"]:
                raise HTTPException(503, detail={
                    "code": "rollback_projection_not_ready",
                    "message": (
                        "Neo4j 未能构建回滚候选投影；"
                        "发布激活事务已回滚"),
                    "projection": projection_check,
                })
        from app.ontologies.projection_state import mark_ready
        mark_ready(db, ontology_id)
        db.commit()
    except HTTPException as exc:
        db.rollback()
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if (
            exc.status_code == 503
            and detail.get("code") == "rollback_projection_not_ready"
        ):
            try:
                compensation = _rebuild_required_query_projections(
                    db, ontology_id)
            except Exception as compensation_exc:  # noqa: BLE001
                compensation = {
                    "ready": False,
                    "error": str(compensation_exc),
                }
            from app.ontologies.projection_state import mark_failed, mark_ready
            if compensation.get("ready"):
                mark_ready(db, ontology_id)
            else:
                mark_failed(
                    db,
                    ontology_id,
                    compensation.get("error")
                    or "Neo4j rollback compensation rebuild failed",
                )
            db.commit()
            raise HTTPException(503, detail={
                **detail,
                "compensation": compensation,
            }) from exc
        from app.ontologies.projection_state import mark_ready
        mark_ready(db, ontology_id)
        db.commit()
        raise
    except Exception as exc:
        db.rollback()
        compensation = None
        if settings.environment != "test":
            try:
                # The candidate SQL transaction has rolled back. Rebuild from
                # the now-authoritative previous release before advertising
                # readiness; the candidate may already have reached Neo4j.
                compensation = _rebuild_required_query_projections(
                    db, ontology_id)
            except Exception as compensation_exc:  # noqa: BLE001
                compensation = {
                    "ready": False,
                    "error": str(compensation_exc),
                }
        from app.ontologies.projection_state import mark_failed, mark_ready
        if settings.environment == "test" or (
            compensation is not None and compensation.get("ready")
        ):
            mark_ready(db, ontology_id)
        else:
            mark_failed(
                db,
                ontology_id,
                (compensation or {}).get("error")
                or "Neo4j rollback compensation rebuild failed",
            )
        db.commit()
        raise HTTPException(409, detail={
            "code": "rollback_restore_failed",
            "message": f"回滚恢复失败，当前本体保持不变: {exc}",
            "errors": [_gate_error(
                "rollback_restore_failed", "ontologyVersion", str(exc),
                item_id=version_id, name=v.version_number)],
            "compensation": compensation,
        }) from exc

    if settings.environment == "test":
        try:
            projection_check = _rebuild_required_query_projections(
                db, ontology_id)
        except Exception as projection_exc:  # noqa: BLE001
            # Unit tests do not require a live query store. Surface its mocked
            # state without weakening any real runtime environment.
            projection_check = {
                "ready": False,
                "error": str(projection_exc),
                "nonBlocking": True,
            }

    return {"data": {
        **_version_payload(activation),
        "rolled_back_to_id": v.id,
        "rolled_back_to_version": v.version_number,
        "status": "published",
        "message": "Rollback successful",
        "formal_restored": formal_restored,
        "query_projection": projection_check,
    }}
