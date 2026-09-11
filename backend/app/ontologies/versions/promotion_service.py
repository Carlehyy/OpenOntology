"""Atomic draft promotion into an immutable ontology release."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.ontologies.formal_modeling.models import (
    LinkInstance as FoLinkInstance,
    ObjectInstance as FoObjectInstance,
)
from app.ontologies.inference.models import AuditLog
from app.ontologies.mappings.models import OntologyLinkMapping, OntologyMapping
from app.ontologies.projects.models import OntologyProject
from app.ontologies.sentinels.models import Sentinel
from app.ontologies.versions.models import (
    OntologyTrialLink,
    OntologyTrialObject,
    OntologyTrialRun,
    OntologyVersion,
)


def promote_draft(
    ontology_id: str,
    version_id: str,
    body: dict,
    db: Session,
    current_user: Any,
    *,
    _ontology_build_lock,
    _promote_draft_locked,
):
    # Acquire the cross-process projection lock before the project row lock.
    # ``build_all`` uses the same advisory→row order; reversing it here would
    # allow an ABBA deadlock during publication.
    with _ontology_build_lock(db, ontology_id):
        result = _promote_draft_locked(
            ontology_id, version_id, body, db, current_user)
    # 新发布改变当前发布指针：详情头版本号、总览统计口径、待审批队列、
    # 跨本体网络图、版本树与列表卡片指标全部换键（fail-open）。
    from app.ontologies import cache as ontology_cache
    from app.ontologies.network import cache as network_cache
    ontology_cache.invalidate_detail()
    ontology_cache.invalidate_overview()
    ontology_cache.invalidate_instance_counts()
    ontology_cache.invalidate_pending()
    ontology_cache.invalidate_version_tree()
    network_cache.invalidate_network()
    ontology_cache.invalidate_list()
    return result


def _promote_draft_locked(
    ontology_id: str,
    version_id: str,
    body: dict,
    db: Session,
    current_user: Any,
    *,
    settings,
    complete_snapshot,
    impact_report,
    snapshot_hash,
    validate_builtin_sentinel_contract,
    validate_manual_mapping_trial_contract,
    validate_release_mapping_contract,
    _current_release,
    _diff_formal,
    _dynamic_sentinel_id_conflict_errors,
    _invalidate_dynamic_sentinels_for_release,
    _json_safe,
    _next_release_activation_number,
    _raise_publish_errors,
    _rebuild_required_query_projections,
    _release_errors,
    _restore_formal_snapshot,
    _runtime_state_conflicts,
    _snapshot_formal,
    _verify_trial_dataset_pins,
    _version_payload,
    recompute_instance_derived,
    record_link_fact,
    record_object_presence,
    record_object_tombstone,
    record_property_facts,
):
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).with_for_update().first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    current = _current_release(db, project)
    draft = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).with_for_update().first()
    if draft is None:
        raise HTTPException(404, "Version not found")
    if draft.node_kind != "draft":
        raise HTTPException(409, detail={
            "code": "promotion_requires_draft", "message": "只能晋级草稿分支",
        })
    if draft.lifecycle_status != "trial_ready":
        raise HTTPException(409, detail={
            "code": "trial_ready_required",
            "message": "只有已通过并冻结的试跑态版本可以转为发布态",
        })
    if draft.base_release_id != current.id:
        raise HTTPException(409, detail={
            "code": "draft_base_outdated",
            "message": "草稿基线不是当前发布版，拒绝覆盖并发发布",
            "draftBaseReleaseId": draft.base_release_id,
            "currentReleaseId": current.id,
        })
    trial_run_id = body.get("trial_run_id", body.get("trialRunId"))
    run = db.query(OntologyTrialRun).filter(
        OntologyTrialRun.id == trial_run_id,
        OntologyTrialRun.ontology_id == ontology_id,
        OntologyTrialRun.version_id == draft.id,
    ).first()
    if run is None or run.status != "passed":
        raise HTTPException(409, detail={
            "code": "passed_trial_required", "message": "发布前必须选择一次通过的试跑",
        })
    snap = complete_snapshot(draft.snapshot_formal)
    current_hash = snapshot_hash(snap)
    if (run.revision != (draft.revision or 0)
            or run.snapshot_hash != draft.snapshot_hash
            or run.snapshot_hash != current_hash):
        run.status = "stale"
        db.commit()
        raise HTTPException(409, detail={
            "code": "trial_snapshot_stale",
            "message": "试跑快照与当前结构不一致，请从该版本创建新草稿后重新试跑",
        })
    sentinel_errors = validate_builtin_sentinel_contract(snap["sentinels"])
    sentinel_errors.extend(_dynamic_sentinel_id_conflict_errors(
        db, ontology_id, snap["sentinels"],
    ))
    _raise_publish_errors(
        sentinel_errors,
        "发布前建模内置哨兵字段校验未通过",
    )
    _raise_publish_errors(
        validate_release_mapping_contract(snap),
        "发布前数据映射完整性校验未通过",
    )
    report = impact_report(current.snapshot_formal, snap)
    acknowledged = body.get("impact_hash", body.get("impactHash"))
    if not acknowledged or acknowledged != report["impactHash"] or run.impact_hash != report["impactHash"]:
        raise HTTPException(409, detail={
            "code": "impact_review_required",
            "message": "影响分析已变化或尚未确认，请重新审核",
            "currentImpactHash": report["impactHash"],
        })
    trial_pin_errors = _verify_trial_dataset_pins(db, run)
    if settings.environment == "production":
        trial_pin_errors.extend(validate_manual_mapping_trial_contract(
            db, snap, run.dataset_versions,
        ))
    _raise_publish_errors(
        trial_pin_errors,
        "试跑数据版本或人工数据自动灌入契约已变化",
    )

    trial_objects = db.query(OntologyTrialObject).filter(
        OntologyTrialObject.trial_run_id == run.id).all()
    trial_links = db.query(OntologyTrialLink).filter(
        OntologyTrialLink.trial_run_id == run.id).all()
    expected = (run.result_json or {}).get("counts") or {}
    if len(trial_objects) != int(expected.get("objects") or 0) or len(trial_links) != int(expected.get("links") or 0):
        raise HTTPException(409, detail={
            "code": "trial_materialization_incomplete",
            "message": "试跑隔离投影不完整；请从该版本创建新草稿后重新试跑",
        })

    # This check intentionally runs inside the same cross-process projection
    # lock and project-row transaction used by action/manual runtime writers.
    # It is repeated here (rather than trusting the impact preview) so an
    # action committed after trial/preview cannot be silently overwritten.
    # No formal definition, projection, Fact, audit, or release row has been
    # mutated at this point.
    runtime_conflicts = _runtime_state_conflicts(
        db,
        ontology_id=ontology_id,
        current_release_id=current.id,
        trial_objects=trial_objects,
        trial_links=trial_links,
    )
    if runtime_conflicts["totalCount"]:
        raise HTTPException(409, detail={
            "code": "runtime_state_conflict",
            "message": (
                "试跑候选会覆盖当前发布版中的非数据湖运行态事实；"
                "发布已在写入前停止，请重新审核运行态与候选值"
            ),
            "runtimeStateConflicts": runtime_conflicts,
        })

    from app.ontologies.projection_state import snapshot as projection_snapshot
    current_projection = projection_snapshot(db, ontology_id)
    if current_projection.status != "ready":
        raise HTTPException(503, detail={
            "code": "ontology_projection_not_ready",
            "message": "当前本体查询投影未就绪，禁止发布；请先执行图修复",
            "projectionStatus": current_projection.status,
        })

    # Persist a project-level fence before the non-transactional candidate
    # projection can become visible. The advisory ontology lock remains held
    # across this commit, so no other runtime mutation can enter the gap.
    current_version_number = current.version_number
    from app.ontologies.projection_state import mark_projecting
    mark_projecting(db, ontology_id)
    db.commit()
    try:
        # Everything after the durable fence commit belongs to one compensation
        # domain.  SQLAlchemy expires loaded ORM rows on commit, so even these
        # apparently read-only preparations can perform I/O and fail.  Leaving
        # them outside this ``try`` would strand the project in ``projecting``.
        project = db.query(OntologyProject).filter(
            OntologyProject.id == ontology_id,
        ).with_for_update().first()
        if project is None:
            raise HTTPException(404, "Ontology not found")

        old_objects = db.query(FoObjectInstance).filter(
            FoObjectInstance.ontology_id == ontology_id).all()
        old_links = db.query(FoLinkInstance).filter(
            FoLinkInstance.ontology_id == ontology_id).all()
        old_object_by_id = {item.id: item for item in old_objects}
        candidate_ids = {item.object_id for item in trial_objects}
        candidate_link_ids = {item.link_id for item in trial_links}
        release_id = str(uuid.uuid4())
        release_number = _next_release_activation_number(db, ontology_id)
        source = f"ontology-release://{release_id}"

        _restore_formal_snapshot(db, ontology_id, snap)
        pinned = {str(item.get("datasetId")): item for item in (run.dataset_versions or [])}
        for mapping in db.query(OntologyMapping).filter(
                OntologyMapping.ontology_id == ontology_id).all():
            mapping.status = "applied"
            fields = dict(mapping.field_mapping or {})
            pin = pinned.get(str(mapping.curated_dataset_id))
            if pin:
                fields["__applied_dataset_version_id__"] = pin.get("versionId")
            mapping.field_mapping = fields
        for mapping in db.query(OntologyLinkMapping).filter(
                OntologyLinkMapping.ontology_id == ontology_id).all():
            mapping.status = "active"
            fields = dict(mapping.field_mapping or {})
            for role, dataset_id in (
                ("source", mapping.src_dataset_id),
                ("target", mapping.tgt_dataset_id),
                ("edge", mapping.edge_dataset_id),
            ):
                if dataset_id and str(dataset_id) in pinned:
                    fields[f"__applied_{role}_version_id__"] = pinned[str(dataset_id)].get("versionId")
            mapping.field_mapping = fields
        for sentinel in db.query(Sentinel).filter(
                Sentinel.ontology_id == ontology_id,
                Sentinel.origin == "release_builtin").all():
            sentinel.status = "published"

        for item in old_links:
            if item.id not in candidate_link_ids:
                record_link_fact(
                    db, ontology_id=ontology_id, link_instance_id=item.id,
                    link_type_id=item.link_type_id, exists=False,
                    source=source, actor_id=current_user.id, caused_by=run.id,
                    ontology_version=release_number,
                    ontology_release_id=release_id)
        for item in old_objects:
            if item.id not in candidate_ids:
                record_object_tombstone(
                    db, ontology_id=ontology_id, instance_id=item.id,
                    object_type_id=item.object_type_id, source=source,
                    actor_id=current_user.id, caused_by=run.id,
                    ontology_version=release_number,
                    ontology_release_id=release_id)

        db.query(FoLinkInstance).filter(
            FoLinkInstance.ontology_id == ontology_id).delete(synchronize_session=False)
        db.query(FoObjectInstance).filter(
            FoObjectInstance.ontology_id == ontology_id).delete(synchronize_session=False)
        # bulk delete 不会同步 Session identity map。后续用相同稳定 ID 写入
        # 新投影前先移除旧 ORM 身份，避免 v1→v2 时出现对象冲突或脏缓存。
        for old_item in [*old_links, *old_objects]:
            db.expunge(old_item)
        promoted_instances: list[
            tuple[FoObjectInstance, list, OntologyTrialObject]
        ] = []
        for item in trial_objects:
            old = old_object_by_id.get(item.object_id)
            old_props = dict(old.properties or {}) if old else None
            new_props = dict(item.properties or {})
            if old is None:
                # Only a genuine creation/revival starts a new existence edge.
                # Backfilling exists=True for a legacy already-present object
                # would make as-of queries incorrectly report it absent before
                # this release.  Existence precedes properties so no cutoff can
                # observe properties before the object itself exists.
                record_object_presence(
                    db, ontology_id=ontology_id, instance_id=item.object_id,
                    object_type_id=item.object_type_id, source=source,
                    actor_id=current_user.id, caused_by=run.id,
                    ontology_version=release_number,
                    ontology_release_id=release_id,
                )
            new_facts = record_property_facts(
                db, ontology_id=ontology_id, instance_id=item.object_id,
                object_type_id=item.object_type_id, old_props=old_props,
                new_props=new_props, source=source, actor_id=current_user.id,
                caused_by=run.id, confidence=1.0,
                ontology_version=release_number,
                ontology_release_id=release_id)
            promoted_instance = FoObjectInstance(
                id=item.object_id, ontology_id=ontology_id,
                ontology_release_id=release_id,
                object_type_id=item.object_type_id,
                properties=dict(item.properties or {}),
                computed=dict(item.computed or {}),
                source="pipeline", external_id=item.external_id,
            )
            db.add(promoted_instance)
            promoted_instances.append((promoted_instance, new_facts, item))
        # Collection-scope derived functions must observe the complete promoted
        # object universe, not a prefix determined by insertion order.
        db.flush()
        for item in trial_links:
            db.add(FoLinkInstance(
                id=item.link_id, ontology_id=ontology_id,
                ontology_release_id=release_id,
                link_type_id=item.link_type_id,
                source_object_id=item.source_object_id,
                target_object_id=item.target_object_id,
                properties=dict(item.properties or {}),
                source_relation_id=item.source_relation_id,
            ))
            record_link_fact(
                db, ontology_id=ontology_id, link_instance_id=item.link_id,
                link_type_id=item.link_type_id, exists=True,
                source=source, actor_id=current_user.id, caused_by=run.id,
                ontology_version=release_number,
                ontology_release_id=release_id)
        db.flush()
        for promoted_instance, new_facts, trial_item in promoted_instances:
            recompute_instance_derived(
                db,
                ontology_id=ontology_id,
                instance=promoted_instance,
                trigger_facts=new_facts,
                caused_by=run.id,
            )
            if dict(promoted_instance.computed or {}) != dict(
                    trial_item.computed or {}):
                raise RuntimeError(
                    "试跑冻结的派生值与发布激活时重算结果不一致: "
                    f"{trial_item.object_id}")
        db.flush()
        _raise_publish_errors(_release_errors(db, ontology_id))

        # The release snapshot is the activated, self-contained definition
        # set—not the pre-activation draft JSON. Mapping application pins and
        # built-in Sentinel publication status are part of what a later
        # rollback must be able to restore without consulting mutable rows.
        release_snapshot = _snapshot_formal(db, ontology_id)
        release = OntologyVersion(
            id=release_id, ontology_id=ontology_id,
            version_number=release_number,
            version_label=str(body.get("version_label") or body.get("versionLabel") or draft.version_label or ""),
            description=str(body.get("description") or draft.description or ""),
            parent_version_id=current.id, base_release_id=release_id,
            promoted_from_id=draft.id, node_kind="release",
            lifecycle_status="released", revision=0,
            snapshot_formal=release_snapshot,
            snapshot_hash=snapshot_hash(release_snapshot),
            canvas_layout=_json_safe(draft.canvas_layout or {}),
            snapshot_semantic=_json_safe(draft.snapshot_semantic),
            published_at=datetime.now(timezone.utc),
            change_summary={"formal": _diff_formal(
                                current.snapshot_formal, release_snapshot),
                            "impact": report},
            created_by=current_user.id,
        )
        db.add(release)
        # ``OntologyProject.current_release_id`` is a real PostgreSQL foreign
        # key.  SQLAlchemy cannot infer the flush dependency from plain FK
        # scalar assignments, so updating the project pointer in the same
        # flush can issue the UPDATE before the version INSERT.  Persist the
        # release row first; the surrounding transaction still makes the
        # promotion atomic and a later failure rolls both changes back.
        db.flush()
        project.current_release_id = release.id
        project.version = release_number
        project.status = "published"
        draft.lifecycle_status = "superseded"
        invalidated_dynamic_sentinels = _invalidate_dynamic_sentinels_for_release(
            db, ontology_id, release.id)
        db.add(AuditLog(
            id=str(uuid.uuid4()), ontology_id=ontology_id,
            event_type="publish", event_subtype="draft_promoted",
            user_id=current_user.id, user_name=current_user.username,
            description=f"将 {draft.version_number} 晋级为 {release_number}",
            object_type="ontology_version", object_id=release.id,
            meta={"draft_version_id": draft.id, "trial_run_id": run.id,
                  "impact_hash": report["impactHash"],
                  "invalidated_dynamic_sentinels": invalidated_dynamic_sentinels},
        ))
        db.flush()
        projection_check = None
        if settings.environment != "test":
            projection_check = _rebuild_required_query_projections(db, ontology_id)
            if not projection_check["ready"]:
                raise RuntimeError("Neo4j candidate projection is not ready")
        from app.ontologies.projection_state import mark_ready
        mark_ready(db, ontology_id)
        db.commit()
        # 发布成功（事务已提交）：发布态业务文档若随版本变化，广播自包含
        # 事件供宫殿共享建图；无语义层时为 no-op，失败只记日志。
        from app.ontologies.published_documents import notify_published_document

        notify_published_document(project, release)
    except HTTPException:
        db.rollback()
        from app.ontologies.projection_state import mark_ready
        mark_ready(db, ontology_id)
        db.commit()
        raise
    except Exception as exc:
        db.rollback()
        compensation = None
        if settings.environment != "test":
            try:
                compensation = _rebuild_required_query_projections(db, ontology_id)
            except Exception as compensation_exc:  # noqa: BLE001
                compensation = {"ready": False, "error": str(compensation_exc)}
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
                or "Neo4j compensation rebuild failed",
            )
        db.commit()
        raise HTTPException(503, detail={
            "code": "promotion_failed",
            "message": f"发布事务已回滚，当前发布版保持 {current_version_number}: {exc}",
            "compensation": compensation,
        }) from exc

    return {"data": {
        **_version_payload(release),
        "trial_run_id": run.id, "impact_hash": report["impactHash"],
        "query_projection": projection_check,
    }}
