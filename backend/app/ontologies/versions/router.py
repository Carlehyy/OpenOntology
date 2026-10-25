"""本体版本化路由 — 版本历史 / diff / 回滚"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.deps import get_db, get_current_user, require_admin
from app.config import settings
from app.exploration.semantic_gate import (
    semantic_consistency_issues,
    semantic_overview,
)
from app.models.ontology_version import OntologyChangeLog, OntologyVersion
from app.models.ontology import OntologyProject
from app.models.ontology_formal import (
    ObjectType as FoObjectType, LinkType as FoLinkType,
    ActionType as FoActionType, ObjectInstance as FoObjectInstance,
)
from app.models.sentinel import Sentinel
from app.ontologies.mappings.models import OntologyMapping, OntologyLinkMapping
from app.ontologies.formal_modeling import schemas as FS
from app.ontologies.formal_modeling.validation import validate_model
from app.ontologies.sentinels import validation as sentinel_validation
from app.ontologies.access import ontology_access_guard
from app.ontologies.versions import (
    promotion_service,
    release_activation_service,
    release_gate_service,
    release_service,
    rollback_service,
    trial_service,
    workspace_service,
)
from app.ontologies.versions.gate_contract import gate_error as _gate_error
from app.ontologies.versions.runtime_state_service import (  # noqa: F401
    _RUNTIME_FACT_QUERY_CHUNK,
    _RUNTIME_FACT_QUERY_POSTGRES_CHUNK,
    _RUNTIME_STATE_ACCESS_TOKEN,
    _RUNTIME_STATE_CONFLICT_LIMIT,
    _RUNTIME_STATE_INLINE_SECRET,
    _RUNTIME_STATE_JWT,
    _RUNTIME_STATE_MASK,
    _RUNTIME_STATE_SENSITIVE_FIELD,
    _dynamic_sentinel_id_conflict_errors,
    _empty_runtime_state_conflicts,
    _is_lake_projection_fact_source,
    _redact_runtime_state_value,
    _release_ancestor_context,
    _release_readiness,
    _runtime_coordinate_facts,
    _runtime_existence_facts,
    _runtime_fact_chunks,
    _runtime_fact_query_chunk_size,
    _runtime_latest_by_scope,
    _runtime_state_conflicts,
    _safe_runtime_fact_source,
    _verify_trial_dataset_pins,
)
from app.ontologies.versions.trial_service import (  # noqa: F401
    _active_trial_run,
    _as_utc,
    _finalize_trial_candidate,
    _load_trial_after_claim_loss,
    _raise_trial_already_running,
    _recover_expired_trial_runs,
    _stale_previous_trials,
    _terminal_trial_result,
    _terminalize_running_trial,
    _trial_claim_lost_error,
    _trial_lease_deadline,
    _trial_materialization_candidate,
)
from app.ontologies.versions.workspace_service import (  # noqa: F401
    _MAPPING_AUTOMATION_POLICY_KEYS,
    _canvas_node_ids,
    _current_release,
    _diff_formal,
    _draft_or_404,
    _ensure_editable_draft,
    _json_safe,
    _mapping_workspace_payload,
    _snapshot_formal,
    _trial_payload,
    _validate_workspace_mapping_policy_types,
    _validated_canvas_positions,
    _version_payload,
    _with_canvas_layout,
    _workspace_mode,
    _workspace_payload,
)
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot,
    next_release_number,
    snapshot_hash,
    snapshot_models,
)
from app.ontologies.versions.evolution_service import (
    impact_report,
    materialize_trial,
    validate_snapshot,
    validate_builtin_sentinel_contract,
    validate_expression_function_contract,
    validate_manual_mapping_trial_contract,
    validate_release_mapping_contract,
    validate_trial_mapping_contract,
)

router = APIRouter(dependencies=[Depends(ontology_access_guard)])


def _raise_publish_errors(errors: list[dict], message: str = "本体发布门禁未通过") -> None:
    return release_gate_service.raise_publish_errors(errors, message)


def _validate_sentinels(
    sentinels: list[Sentinel],
    object_types: list[FoObjectType],
    link_types: list[FoLinkType],
    actions: list[FoActionType],
) -> list[dict]:
    """Compatibility wrapper for historical imports and patch paths."""
    return release_gate_service.validate_sentinels(
        sentinels,
        object_types,
        link_types,
        actions,
        validator=sentinel_validation.validate_sentinels,
    )


def _validate_production_mappings(db: Session, ontology_id: str,
                                  mappings: list[OntologyMapping],
                                  link_mappings: list[OntologyLinkMapping],
                                  instances: list[FoObjectInstance],
                                  object_types: list[FoObjectType]) -> list[dict]:
    return release_gate_service.validate_production_mappings(
        db,
        ontology_id,
        mappings,
        link_mappings,
        instances,
        object_types,
        gate_error=_gate_error,
    )


def _release_errors(db: Session, ontology_id: str) -> list[dict]:
    """发布和回滚共用的全量、fail-closed 契约。"""
    from app.ontologies.formal_modeling import action_engine

    return release_gate_service.release_errors(
        db,
        ontology_id,
        environment=settings.environment,
        action_definition_validator=(
            action_engine.validate_action_definition
        ),
        model_validator=validate_model,
        expression_function_validator=(
            validate_expression_function_contract
        ),
        builtin_sentinel_validator=(
            validate_builtin_sentinel_contract
        ),
        sentinel_snapshotter=_snapshot_sentinel,
        sentinel_validator=_validate_sentinels,
        production_mapping_validator=_validate_production_mappings,
        gate_error=_gate_error,
    )


def _rebuild_required_query_projections(db: Session, ontology_id: str) -> dict:
    return release_activation_service.rebuild_required_query_projections(
        db,
        ontology_id,
    )


# ============ 正规模型 (fo_*) 快照与差异 ============

def _snapshot_sentinel(item: Sentinel) -> dict:
    """Compatibility wrapper for release-gate callers in this module."""
    return release_service.snapshot_release_sentinel(item)


@router.get("/{ontology_id}/versions")
def list_versions(ontology_id: str, limit: int = 20, offset: int = 0, db: Session = Depends(get_db)):
    """列出所有版本（分页）"""
    return workspace_service.list_versions(
        db,
        ontology_id,
        limit,
        offset,
    )


def _next_release_activation_number(
        db: Session, ontology_id: str) -> str:
    return release_activation_service.next_release_activation_number(
        db,
        ontology_id,
        number_allocator=next_release_number,
    )


@router.get("/{ontology_id}/current-release/workspace")
def get_current_release_workspace(
    ontology_id: str, db: Session = Depends(get_db),
):
    """Read the one authoritative published structure snapshot."""
    return workspace_service.get_current_release_workspace(db, ontology_id)


@router.get("/{ontology_id}/current-release/mappings")
def get_current_release_mappings(
    ontology_id: str, db: Session = Depends(get_db),
):
    """Read mappings frozen into the authoritative published snapshot."""
    return workspace_service.get_current_release_mappings(db, ontology_id)


@router.get("/{ontology_id}/version-tree")
def get_version_tree(ontology_id: str, db: Session = Depends(get_db)):
    return workspace_service.get_version_tree(db, ontology_id)


@router.post("/{ontology_id}/versions/{source_version_id}/drafts", status_code=201)
def create_draft_version(
    ontology_id: str, source_version_id: str, body: dict,
    db: Session = Depends(get_db), current_user=Depends(get_current_user),
):
    return workspace_service.create_draft_version(
        db,
        ontology_id,
        source_version_id,
        body,
        current_user,
    )


@router.delete("/{ontology_id}/versions/{version_id}")
def delete_draft_version(
    ontology_id: str, version_id: str,
    db: Session = Depends(get_db), current_user=Depends(get_current_user),
):
    """Delete only an unpublished leaf branch.

    A version with descendants is part of the evolution tree's provenance and
    cannot be removed. Published and superseded nodes are immutable audit facts.
    """
    return workspace_service.delete_draft_version(
        db,
        ontology_id,
        version_id,
        current_user,
        _recover_expired_trial_runs=_recover_expired_trial_runs,
    )


@router.get("/{ontology_id}/versions/{version_id}/workspace")
def get_version_workspace(
    ontology_id: str, version_id: str, db: Session = Depends(get_db),
):
    return workspace_service.get_version_workspace(
        db,
        ontology_id,
        version_id,
    )


@router.put("/{ontology_id}/layout")
def save_canvas_layout(
    ontology_id: str, body: dict, db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """保存共享画布布局；不推进模型 revision，也不改变 snapshot_hash。"""
    return workspace_service.save_canvas_layout(db, ontology_id, body)


@router.put("/{ontology_id}/versions/{version_id}/workspace")
def save_draft_workspace(
    ontology_id: str, version_id: str, body: dict,
    db: Session = Depends(get_db), current_user=Depends(get_current_user),
):
    return workspace_service.save_draft_workspace(
        db,
        ontology_id,
        version_id,
        body,
        current_user,
        _raise_publish_errors=_raise_publish_errors,
        _stale_previous_trials=_stale_previous_trials,
    )


@router.get("/{ontology_id}/versions/{version_id}/workspace/mappings")
def get_draft_mappings(
    ontology_id: str, version_id: str, db: Session = Depends(get_db),
):
    return workspace_service.get_draft_mappings(db, ontology_id, version_id)


@router.put("/{ontology_id}/versions/{version_id}/workspace/mappings")
def save_draft_mappings(
    ontology_id: str, version_id: str, body: dict,
    db: Session = Depends(get_db), current_user=Depends(get_current_user),
):
    return workspace_service.save_draft_mappings(
        db,
        ontology_id,
        version_id,
        body,
        current_user,
        _raise_publish_errors=_raise_publish_errors,
        _stale_previous_trials=_stale_previous_trials,
    )


@router.get("/{ontology_id}/versions/{version_id}/impact")
def get_draft_impact(
    ontology_id: str, version_id: str, db: Session = Depends(get_db),
):
    return workspace_service.get_draft_impact(
        db,
        ontology_id,
        version_id,
        validate_release_mapping_contract=validate_release_mapping_contract,
        semantic_overview_fn=semantic_overview,
    )


@router.get("/{ontology_id}/versions/{version_id}/semantic")
def get_version_semantic(
    ontology_id: str, version_id: str, db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """读取任一版本的业务语义层快照与一致性总览（读端点，不要求 draft）。"""
    return workspace_service.get_version_semantic(
        db,
        ontology_id,
        version_id,
        semantic_overview_fn=semantic_overview,
        semantic_consistency_fn=semantic_consistency_issues,
        current_user=current_user,
    )


@router.put("/{ontology_id}/versions/{version_id}/semantic/document")
def save_release_document(
    ontology_id: str, version_id: str, body: dict,
    db: Session = Depends(get_db), current_user=Depends(get_current_user),
):
    """受控修订当前发布版本业务文档正文（仅 documentMd，结构与指针不动）。"""
    return workspace_service.save_release_document(
        db,
        ontology_id,
        version_id,
        body,
        current_user,
        semantic_overview_fn=semantic_overview,
        semantic_consistency_fn=semantic_consistency_issues,
    )


def _snapshot_sentinel_models(snapshot: dict) -> list[SimpleNamespace]:
    return release_service.snapshot_sentinel_models(
        snapshot,
        snapshot_completer=complete_snapshot,
    )


def _invalidate_dynamic_sentinels_for_release(
        db: Session, ontology_id: str, release_id: str) -> int:
    return release_activation_service.invalidate_dynamic_sentinels_for_release(
        db,
        ontology_id,
        release_id,
    )


@router.get("/{ontology_id}/versions/{version_id}/trial-runs")
def list_trial_runs(
    ontology_id: str, version_id: str, db: Session = Depends(get_db),
):
    return trial_service.list_trial_runs(
        db,
        ontology_id,
        version_id,
        _recover_expired_trial_runs=_recover_expired_trial_runs,
        _trial_payload=_trial_payload,
    )


@router.get("/{ontology_id}/versions/{version_id}/trial-runs/{run_id}")
def get_trial_run(
    ontology_id: str, version_id: str, run_id: str,
    db: Session = Depends(get_db),
):
    return trial_service.get_trial_run(
        db,
        ontology_id,
        version_id,
        run_id,
        _recover_expired_trial_runs=_recover_expired_trial_runs,
        _trial_payload=_trial_payload,
    )


@router.post("/{ontology_id}/versions/{version_id}/trial-runs", status_code=201)
def create_trial_run(
    ontology_id: str, version_id: str, body: dict,
    db: Session = Depends(get_db), current_user=Depends(get_current_user),
):
    return trial_service.create_trial_run(
        db,
        ontology_id,
        version_id,
        body,
        current_user,
        materialize_trial=materialize_trial,
        _active_trial_run=_active_trial_run,
        _current_release=_current_release,
        _ensure_editable_draft=_ensure_editable_draft,
        _finalize_trial_candidate=_finalize_trial_candidate,
        _raise_publish_errors=_raise_publish_errors,
        _raise_trial_already_running=_raise_trial_already_running,
        _recover_expired_trial_runs=_recover_expired_trial_runs,
        _snapshot_sentinel_models=_snapshot_sentinel_models,
        _terminal_trial_result=_terminal_trial_result,
        _trial_lease_deadline=_trial_lease_deadline,
        _trial_materialization_candidate=_trial_materialization_candidate,
        _trial_payload=_trial_payload,
        _validate_sentinels=_validate_sentinels,
        semantic_consistency_fn=semantic_consistency_issues,
    )


@router.post("/{ontology_id}/versions/{version_id}/trial-preflight")
def trial_preflight(
    ontology_id: str, version_id: str,
    db: Session = Depends(get_db), _=Depends(get_current_user),
):
    """试跑前只读预检：复用试跑门禁逐项汇总，不创建试跑记录、不写库。"""
    return trial_service.trial_preflight(
        db,
        ontology_id,
        version_id,
        _active_trial_run=_active_trial_run,
        _current_release=_current_release,
        _ensure_editable_draft=_ensure_editable_draft,
        _raise_trial_already_running=_raise_trial_already_running,
        _snapshot_sentinel_models=_snapshot_sentinel_models,
        _validate_sentinels=_validate_sentinels,
        semantic_consistency_fn=semantic_consistency_issues,
    )



@router.post("/{ontology_id}/versions/{version_id}/promote", status_code=201)
def promote_draft(
    ontology_id: str, version_id: str, body: dict,
    db: Session = Depends(get_db), current_user=Depends(require_admin),
):
    from app.ontologies.mappings.mapping_service import _ontology_build_lock
    return promotion_service.promote_draft(
        ontology_id,
        version_id,
        body,
        db,
        current_user,
        _ontology_build_lock=_ontology_build_lock,
        _promote_draft_locked=_promote_draft_locked,
    )


def _promote_draft_locked(
    ontology_id: str, version_id: str, body: dict,
    db: Session, current_user,
):
    from app.ontologies.formal_modeling.derived import (
        recompute_instance_derived,
    )
    from app.ontologies.formal_modeling.facts import (
        record_link_fact,
        record_object_presence,
        record_object_tombstone,
        record_property_facts,
    )
    return promotion_service._promote_draft_locked(
        ontology_id,
        version_id,
        body,
        db,
        current_user,
        settings=settings,
        complete_snapshot=complete_snapshot,
        impact_report=impact_report,
        snapshot_hash=snapshot_hash,
        validate_builtin_sentinel_contract=(
            validate_builtin_sentinel_contract
        ),
        validate_manual_mapping_trial_contract=(
            validate_manual_mapping_trial_contract
        ),
        validate_release_mapping_contract=(
            validate_release_mapping_contract
        ),
        _current_release=_current_release,
        _diff_formal=_diff_formal,
        _dynamic_sentinel_id_conflict_errors=(
            _dynamic_sentinel_id_conflict_errors
        ),
        _invalidate_dynamic_sentinels_for_release=(
            _invalidate_dynamic_sentinels_for_release
        ),
        _json_safe=_json_safe,
        _next_release_activation_number=_next_release_activation_number,
        _raise_publish_errors=_raise_publish_errors,
        _rebuild_required_query_projections=(
            _rebuild_required_query_projections
        ),
        _release_errors=_release_errors,
        _restore_formal_snapshot=_restore_formal_snapshot,
        _runtime_state_conflicts=_runtime_state_conflicts,
        _snapshot_formal=_snapshot_formal,
        _verify_trial_dataset_pins=_verify_trial_dataset_pins,
        _version_payload=_version_payload,
        recompute_instance_derived=recompute_instance_derived,
        record_link_fact=record_link_fact,
        record_object_presence=record_object_presence,
        record_object_tombstone=record_object_tombstone,
        record_property_facts=record_property_facts,
    )


@router.post("/{ontology_id}/versions", status_code=410, deprecated=True)
def create_version(ontology_id: str, body: dict | None = None,
                   db: Session = Depends(get_db),
                   current_user=Depends(require_admin)):
    """Retired one-click publication endpoint.

    It used mutable runtime rows as its source and therefore skipped both the
    isolated trial and immutable-candidate checks.  Keeping it callable would
    make the three-state lifecycle advisory rather than authoritative.
    """
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    raise HTTPException(410, detail={
        "code": "legacy_publish_endpoint_retired",
        "message": "一键发布接口已停用；请创建草稿、完成隔离试跑后再调用 promote",
        "currentReleaseId": project.current_release_id,
        "requiredFlow": ["draft", "trial", "promote"],
    })


@router.get("/{ontology_id}/versions/{version_id}")
def get_version_detail(ontology_id: str, version_id: str, db: Session = Depends(get_db)):
    """获取版本详情（含完整快照）"""
    v = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if not v:
        raise HTTPException(404, "Version not found")
    return {"data": {
        **_version_payload(v),
        "snapshot": {
            "entities": v.snapshot_entities or [],
            "relations": v.snapshot_relations or [],
            "logic": v.snapshot_logic or [],
            "actions": v.snapshot_actions or [],
            "formal": v.snapshot_formal or None,
        },
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }}


@router.post("/{ontology_id}/unpublish", deprecated=True)
@router.post("/{ontology_id}/versions/unpublish", include_in_schema=False)
def unpublish_ontology(ontology_id: str, db: Session = Depends(get_db),
                       current_user=Depends(require_admin)):
    """Retired mutable release withdrawal endpoint."""
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    raise HTTPException(410, detail={
        "code": "unpublish_endpoint_retired",
        "message": "发布节点不可撤回；请从目标发布快照创建草稿并完成试跑晋级",
        "currentReleaseId": project.current_release_id,
        "requiredFlow": ["draft", "trial", "promote"],
    })


def _restore_formal_snapshot(
    db: Session, ontology_id: str, snap: dict,
) -> dict:
    return rollback_service._restore_formal_snapshot(
        db,
        ontology_id,
        snap,
        FS=FS,
        _json_safe=_json_safe,
    )


@router.post("/{ontology_id}/versions/{version_id}/rollback")
def rollback_version(ontology_id: str, version_id: str, db: Session = Depends(get_db),
                     current_user=Depends(require_admin)):
    """Activate a new release whose definitions come from a historic release.

    A rollback is a new deployment event, never pointer reuse. Runtime rows are
    rebound to the new immutable activation id while facts, firings and
    approvals keep the release ids under which they were originally produced.
    """
    from app.ontologies.mappings.mapping_service import _ontology_build_lock
    return rollback_service.rollback_version(
        ontology_id,
        version_id,
        db,
        current_user,
        _ontology_build_lock=_ontology_build_lock,
        _rollback_version_locked=_rollback_version_locked,
    )


def _rollback_version_locked(
        ontology_id: str, version_id: str, db: Session, current_user):
    from app.ontologies.formal_modeling.derived import (
        recompute_instance_derived,
    )
    return rollback_service._rollback_version_locked(
        ontology_id,
        version_id,
        db,
        current_user,
        settings=settings,
        snapshot_hash=snapshot_hash,
        _current_release=_current_release,
        _diff_formal=_diff_formal,
        _gate_error=_gate_error,
        _invalidate_dynamic_sentinels_for_release=(
            _invalidate_dynamic_sentinels_for_release
        ),
        _json_safe=_json_safe,
        _next_release_activation_number=_next_release_activation_number,
        _rebuild_required_query_projections=(
            _rebuild_required_query_projections
        ),
        _release_errors=_release_errors,
        _restore_formal_snapshot=_restore_formal_snapshot,
        _snapshot_formal=_snapshot_formal,
        _version_payload=_version_payload,
        recompute_instance_derived=recompute_instance_derived,
    )


@router.get("/{ontology_id}/change-logs")
def list_change_logs(ontology_id: str, object_type: str = None, limit: int = 100, db: Session = Depends(get_db)):
    """列出变更日志"""
    q = db.query(OntologyChangeLog).filter(OntologyChangeLog.ontology_id == ontology_id)
    if object_type:
        q = q.filter(OntologyChangeLog.object_type == object_type)
    logs = q.order_by(desc(OntologyChangeLog.created_at)).limit(limit).all()
    return {"data": [{
        "id": log.id,
        "action": log.action,
        "object_type": log.object_type,
        "object_name": log.object_name,
        "before": log.before,
        "after": log.after,
        "created_by_name": log.created_by_name,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    } for log in logs]}
