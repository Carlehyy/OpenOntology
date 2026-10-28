"""Atomic draft promotion into an immutable ontology release."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.ontologies.formal_modeling.facts import property_fact_batch, record_link_property_facts
from app.ontologies.versions.batching import query_batches

from app.ontologies.formal_modeling.models import (
    LinkInstance as FoLinkInstance,
    ObjectInstance as FoObjectInstance,
    ObjectType as FoObjectType,
    PropertyFact,
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


def _link_fact_version(item, endpoint_datasets, mappings_by_type, pinned):
    """Resolve lineage only from the frozen mapping and this trial's pins."""
    if not item.source_relation_id:
        return None  # A manual link has no pipeline mapping provenance.
    source_dataset = endpoint_datasets.get(item.source_object_id)
    target_dataset = endpoint_datasets.get(item.target_object_id)
    versions = {
        (pinned.get(str(mapping.get("edgeDatasetId") or mapping.get("srcDatasetId"))) or {}).get("versionId")
        for mapping in mappings_by_type.get(item.link_type_id, [])
        if source_dataset and target_dataset
        and str(mapping.get("srcDatasetId") or "") == source_dataset
        and str(mapping.get("tgtDatasetId") or "") == target_dataset
    }
    if len(versions) > 1:
        raise HTTPException(409, detail={
            "code": "link_lineage_ambiguous",
            "message": f"链接 {item.link_id} 的冻结映射数据版本血缘不唯一，拒绝猜测来源版本",
            "linkId": item.link_id,
        })
    return next(iter(versions), None)


def _remote_release_gate_errors(db, snap, run, current_user) -> list[dict]:
    """Remote-binding branch of the promotion gate.

    Local pins/counts keep their exact semantics elsewhere in the promotion
    flow.  Remote bindings cannot masquerade as local full-quality
    materialization, so they pass only when (a) the frozen isolated trial
    recorded a verified remote check for every remote definition in the
    snapshot and (b) the live remote metadata still matches the frozen
    descriptor at publication time (authorization + schema drift re-check).
    """
    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        RemoteBindingError,
        snapshot_entry_binding,
        snapshot_link_binding_mode,
        verify_remote_binding,
    )

    trial_checks = ((run.result_json or {}).get("remoteBindings") or {})
    errors: list[dict] = []

    def gate_error(code, kind, item_id, name, message, **context):
        payload = {"code": code, "kind": kind, "id": item_id, "name": name,
                   "message": message}
        payload.update({key: value for key, value in context.items()
                        if value is not None})
        errors.append(payload)

    for entry in snap["mappings"]:
        if not isinstance(entry, dict):
            continue
        try:
            mode, descriptor = snapshot_entry_binding(entry)
        except RemoteBindingError as exc:
            gate_error(exc.code, "mapping", str(entry.get("id") or ""),
                       str(entry.get("entityClass") or ""),
                       f"远程绑定描述符非法：{exc.message}", **exc.context)
            continue
        if mode != BINDING_MODE_REMOTE or descriptor is None:
            continue
        mapping_id = str(entry.get("id") or "")
        label = str(entry.get("entityClass") or mapping_id)
        check = trial_checks.get(mapping_id)
        if not isinstance(check, dict) or check.get("status") != "verified":
            gate_error(
                "remote_trial_check_missing", "mapping", mapping_id, label,
                "远程绑定缺少已冻结的试跑验证记录；请重新试跑后再发布")
            continue
        if str(check.get("schemaFingerprint") or "") != str(
                descriptor.get("schemaFingerprint") or ""):
            gate_error(
                "remote_trial_check_stale", "mapping", mapping_id, label,
                "试跑冻结的远程指纹与当前快照不一致；请重新试跑后再发布")
            continue
        try:
            verify_remote_binding(db, current_user, descriptor, sample=False)
        except RemoteBindingError as exc:
            gate_error(
                exc.code, "mapping", mapping_id, label,
                f"发布时远程绑定重验失败：{exc.message}", **exc.context)

    for entry in snap["linkMappings"]:
        if not isinstance(entry, dict):
            continue
        try:
            mode = snapshot_link_binding_mode(entry)
        except RemoteBindingError as exc:
            gate_error(exc.code, "linkMapping", str(entry.get("id") or ""),
                       str(entry.get("relationType") or ""),
                       f"远程关系绑定模式非法：{exc.message}", **exc.context)
            continue
        if mode != BINDING_MODE_REMOTE:
            continue
        mapping_id = str(entry.get("id") or "")
        label = str(entry.get("relationType") or mapping_id)
        check = trial_checks.get(mapping_id)
        if not isinstance(check, dict) or check.get("status") != "verified":
            gate_error(
                "remote_trial_check_missing", "linkMapping", mapping_id, label,
                "远程关系定义缺少已冻结的试跑验证记录；请重新试跑后再发布")
    return errors


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

    # 远程绑定发布正门分支：本地 pins/counts 的完整性口径保持不变（下方
    # 逐项校验），远程绑定必须持有一致的冻结试跑验证记录，且发布时对冻结
    # 元数据做授权与 schema drift 重验；验证器未装配时 fail-closed。
    _raise_publish_errors(
        _remote_release_gate_errors(db, snap, run, current_user),
        "远程绑定发布校验未通过",
    )

    trial_objects = db.query(OntologyTrialObject).filter(
        OntologyTrialObject.trial_run_id == run.id)
    trial_links = db.query(OntologyTrialLink).filter(
        OntologyTrialLink.trial_run_id == run.id)
    expected = (run.result_json or {}).get("counts") or {}
    if (trial_objects.count() != int(expected.get("objects") or 0)
            or trial_links.count() != int(expected.get("links") or 0)):
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
            FoObjectInstance.ontology_id == ontology_id)
        old_links = db.query(FoLinkInstance).filter(
            FoLinkInstance.ontology_id == ontology_id)
        release_id = str(uuid.uuid4())
        release_number = _next_release_activation_number(db, ontology_id)
        source = f"ontology-release://{release_id}"

        _restore_formal_snapshot(db, ontology_id, snap)
        pinned = {str(item.get("datasetId")): item for item in (run.dataset_versions or [])}
        link_mappings_by_type = {}
        for mapping in snap["linkMappings"]:
            link_mappings_by_type.setdefault(str(mapping.get("linkTypeId") or ""), []).append(mapping)
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

        # SQL anti-joins find removals without holding all old/candidate IDs.
        missing_links = old_links.filter(~trial_links.filter(
            OntologyTrialLink.link_id == FoLinkInstance.id,
        ).exists())
        for batch in query_batches(missing_links, FoLinkInstance.id):
            with property_fact_batch(db, ontology_id=ontology_id,
                                     instance_ids=(item.id for item in batch)):
                for item in batch:
                    record_link_fact(
                        db, ontology_id=ontology_id, link_instance_id=item.id,
                        link_type_id=item.link_type_id, exists=False,
                        source=source, actor_id=current_user.id, caused_by=run.id,
                        ontology_version=release_number,
                        ontology_release_id=release_id)
            for item in batch:
                db.delete(item)
            db.flush()
        missing_objects = old_objects.filter(~trial_objects.filter(
            OntologyTrialObject.object_id == FoObjectInstance.id,
        ).exists())
        for batch in query_batches(missing_objects, FoObjectInstance.id):
            with property_fact_batch(db, ontology_id=ontology_id,
                                     instance_ids=(item.id for item in batch)):
                for item in batch:
                    record_object_tombstone(
                        db, ontology_id=ontology_id, instance_id=item.id,
                        object_type_id=item.object_type_id, source=source,
                        actor_id=current_user.id, caused_by=run.id,
                        ontology_version=release_number,
                        ontology_release_id=release_id)
            for item in batch:
                db.delete(item)
            db.flush()

        for batch in query_batches(trial_objects, OntologyTrialObject.object_id):
            ids = [item.object_id for item in batch]
            old_by_id = {item.id: item for item in old_objects.filter(
                FoObjectInstance.id.in_(ids),
            ).all()}
            with property_fact_batch(db, ontology_id=ontology_id, instance_ids=ids):
                for item in batch:
                    old = old_by_id.get(item.object_id)
                    old_props = dict(old.properties or {}) if old is not None else None
                    if old is None:
                        # Do not fabricate an existence edge for legacy objects
                        # already present before this release.
                        record_object_presence(
                            db, ontology_id=ontology_id, instance_id=item.object_id,
                            object_type_id=item.object_type_id, source=source,
                            actor_id=current_user.id, caused_by=run.id,
                            ontology_version=release_number,
                            ontology_release_id=release_id,
                            source_dataset_version_id=item.source_dataset_version_id,
                        )
                    record_property_facts(
                        db, ontology_id=ontology_id, instance_id=item.object_id,
                        object_type_id=item.object_type_id, old_props=old_props,
                        new_props=dict(item.properties or {}), source=source,
                        actor_id=current_user.id, caused_by=run.id, confidence=1.0,
                        ontology_version=release_number,
                        ontology_release_id=release_id,
                        source_dataset_version_id=item.source_dataset_version_id,
                    )
            # Replace only this page. No giant delete/identity-map mirror is
            # needed, while retaining the original creation timestamp semantics.
            for old in old_by_id.values():
                db.delete(old)
            db.flush()
            db.add_all([
                FoObjectInstance(
                    id=item.object_id, ontology_id=ontology_id,
                    ontology_release_id=release_id,
                    object_type_id=item.object_type_id,
                    properties=dict(item.properties or {}),
                    computed=dict(item.computed or {}), source="pipeline",
                    external_id=item.external_id,
                ) for item in batch
            ])
            db.flush()

        for batch in query_batches(trial_links, OntologyTrialLink.link_id):
            ids = [item.link_id for item in batch]
            old_props_by_id = {item.id: dict(item.properties or {}) for item in old_links.filter(
                FoLinkInstance.id.in_(ids),
            ).all()}
            endpoint_ids = {endpoint for item in batch for endpoint in (
                item.source_object_id, item.target_object_id)}
            endpoint_datasets = dict(trial_objects.with_entities(
                OntologyTrialObject.object_id, OntologyTrialObject.source_dataset_id,
            ).filter(OntologyTrialObject.object_id.in_(endpoint_ids)).all())
            old_links.filter(FoLinkInstance.id.in_(ids)).delete(
                synchronize_session="fetch",
            )
            db.add_all([
                FoLinkInstance(
                    id=item.link_id, ontology_id=ontology_id,
                    ontology_release_id=release_id, link_type_id=item.link_type_id,
                    source_object_id=item.source_object_id,
                    target_object_id=item.target_object_id,
                    properties=dict(item.properties or {}),
                    source_relation_id=item.source_relation_id,
                ) for item in batch
            ])
            with property_fact_batch(db, ontology_id=ontology_id, instance_ids=ids):
                for item in batch:
                    source_version = _link_fact_version(
                        item, endpoint_datasets, link_mappings_by_type, pinned)
                    record_link_fact(
                        db, ontology_id=ontology_id, link_instance_id=item.link_id,
                        link_type_id=item.link_type_id, exists=True, source=source,
                        actor_id=current_user.id, caused_by=run.id,
                        ontology_version=release_number,
                        ontology_release_id=release_id,
                        source_dataset_version_id=source_version)
                    record_link_property_facts(
                        db, ontology_id=ontology_id, instance_id=item.link_id,
                        link_type_id=item.link_type_id,
                        old_props=old_props_by_id.get(item.link_id),
                        new_props=dict(item.properties or {}), source=source,
                        actor_id=current_user.id, caused_by=run.id,
                        ontology_version=release_number,
                        ontology_release_id=release_id,
                        source_dataset_version_id=source_version)

        # Only now may collection functions run: they must observe the complete
        # promoted universe, not the current insertion page. Trigger facts are
        # reloaded per page from this activation, never retained for all rows.
        object_types = {item.id: item for item in db.query(FoObjectType).filter(
            FoObjectType.ontology_id == ontology_id,
        ).all()}
        for batch in query_batches(trial_objects, OntologyTrialObject.object_id):
            ids = [item.object_id for item in batch]
            live_by_id = {item.id: item for item in old_objects.filter(
                FoObjectInstance.id.in_(ids),
            ).all()}
            facts_by_id: dict[str, list] = {}
            for fact in db.query(PropertyFact).filter(
                PropertyFact.ontology_id == ontology_id,
                PropertyFact.ontology_release_id == release_id,
                PropertyFact.kind == "property",
                PropertyFact.instance_id.in_(ids),
            ).all():
                facts_by_id.setdefault(fact.instance_id, []).append(fact)
            with property_fact_batch(db, ontology_id=ontology_id, instance_ids=ids):
                for trial_item in batch:
                    promoted_instance = live_by_id[trial_item.object_id]
                    recompute_instance_derived(
                        db, ontology_id=ontology_id, instance=promoted_instance,
                        object_type=object_types.get(trial_item.object_type_id),
                        trigger_facts=facts_by_id.get(trial_item.object_id, []),
                        caused_by=run.id,
                    )
                    if dict(promoted_instance.computed or {}) != dict(
                            trial_item.computed or {}):
                        raise RuntimeError(
                            "试跑冻结的派生值与发布激活时重算结果不一致: "
                            f"{trial_item.object_id}")
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
