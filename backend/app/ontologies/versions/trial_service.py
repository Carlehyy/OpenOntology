"""Trial lease, single-flight, materialization, and finalization service."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.ontologies import cache as ontology_cache
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot,
    snapshot_hash,
    snapshot_models,
)
from app.ontologies.versions.evolution_service import (
    impact_report,
    split_mapping_coverage_errors,
    validate_snapshot,
    validate_trial_mapping_contract,
)
from app.ontologies.versions.gate_contract import gate_error as _gate_error
from app.ontologies.versions.models import OntologyTrialRun, OntologyVersion
from app.ontologies.versions.runtime_state_service import (
    _dynamic_sentinel_id_conflict_errors,
)
from app.ontologies.versions.workspace_service import _trial_payload


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive DateTime values for lease comparisons."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _trial_lease_deadline(now: datetime | None = None) -> datetime:
    seconds = max(int(settings.ontology_trial_lease_seconds), 1)
    return _as_utc(now or datetime.now(timezone.utc)) + timedelta(seconds=seconds)


def _terminal_trial_result(
        run: OntologyTrialRun, code: str, message: str, **context: Any) -> dict:
    error = _gate_error(
        code, "trialRun", message, item_id=run.id)
    error.update({
        key: value for key, value in context.items() if value is not None
    })
    return {
        "counts": {"objects": 0, "links": 0, "facts": 0, "datasets": 0},
        "errors": [error],
        "warnings": [],
        "samples": {"objects": [], "links": []},
        "actionsExecuted": 0,
        "sideEffects": "blocked",
    }


def _terminalize_running_trial(
        run: OntologyTrialRun, *, code: str, message: str,
        now: datetime | None = None, terminal_status: str = "stale",
        **context: Any,
) -> bool:
    """Release one active slot without allowing its old worker to write back."""
    if run.status != "running":
        return False
    completed_at = _as_utc(now or datetime.now(timezone.utc))
    run.status = terminal_status
    run.completed_at = completed_at
    run.claim_token = None
    run.lease_expires_at = None
    run.result_json = _terminal_trial_result(
        run, code, message, **context)
    return True


def _recover_expired_trial_runs(
        db: Session, ontology_id: str, version_id: str,
        *, now: datetime | None = None,
) -> list[OntologyTrialRun]:
    """Terminalize abandoned claims so retry and branch deletion cannot wedge."""
    checked_at = _as_utc(now or datetime.now(timezone.utc))
    runs = db.query(OntologyTrialRun).filter(
        OntologyTrialRun.ontology_id == ontology_id,
        OntologyTrialRun.version_id == version_id,
        OntologyTrialRun.status == "running",
    ).with_for_update().all()
    recovered: list[OntologyTrialRun] = []
    for run in runs:
        if not run.claim_token or run.lease_expires_at is None:
            if _terminalize_running_trial(
                    run,
                    code="trial_claim_invalid",
                    message="试跑运行记录缺少有效执行凭据，已安全终结；请重新试跑",
                    now=checked_at):
                recovered.append(run)
            continue
        expires_at = run.lease_expires_at
        if _as_utc(expires_at) <= checked_at and _terminalize_running_trial(
                run,
                code="trial_run_timeout",
                message="试跑执行租约已超时，原运行已安全终结；可以重新试跑",
                now=checked_at,
                expiredAt=_as_utc(expires_at).isoformat()):
            recovered.append(run)
    if recovered:
        db.flush()
    return recovered


def _active_trial_run(
        db: Session, ontology_id: str, version_id: str,
) -> OntologyTrialRun | None:
    return db.query(OntologyTrialRun).filter(
        OntologyTrialRun.ontology_id == ontology_id,
        OntologyTrialRun.version_id == version_id,
        OntologyTrialRun.status == "running",
    ).order_by(desc(OntologyTrialRun.created_at)).first()


def _raise_trial_already_running(run: OntologyTrialRun) -> None:
    raise HTTPException(409, detail={
        "code": "trial_already_running",
        "message": "该草稿已有权威试跑正在执行，请等待完成或租约回收",
        "trialRunId": run.id,
        "leaseExpiresAt": (
            run.lease_expires_at.isoformat()
            if run.lease_expires_at else None
        ),
    })


def _stale_previous_trials(db: Session, draft: OntologyVersion) -> None:
    # Editing invalidates an in-flight snapshot immediately.  Clearing its
    # claim releases the single-flight slot, while the old materializer is
    # fenced again at terminal write-back.
    now = datetime.now(timezone.utc)
    for run in db.query(OntologyTrialRun).filter(
            OntologyTrialRun.version_id == draft.id,
            OntologyTrialRun.status == "running",
    ).with_for_update().all():
        _terminalize_running_trial(
            run,
            code="trial_snapshot_changed",
            message="草稿在试跑执行期间发生修改，原试跑已失效",
            now=now,
            currentRevision=draft.revision,
            currentSnapshotHash=draft.snapshot_hash,
        )
    db.query(OntologyTrialRun).filter(
        OntologyTrialRun.version_id == draft.id,
        OntologyTrialRun.status == "passed",
    ).update({OntologyTrialRun.status: "stale"}, synchronize_session=False)


def _trial_materialization_candidate(run: OntologyTrialRun) -> SimpleNamespace:
    """Use a detached result carrier so heavy work cannot update the claim row."""
    return SimpleNamespace(
        id=run.id,
        ontology_id=run.ontology_id,
        version_id=run.version_id,
        revision=run.revision,
        snapshot_hash=run.snapshot_hash,
        base_release_id=run.base_release_id,
        status="running",
        dataset_versions=[],
        result_json={},
        impact_hash=run.impact_hash,
        created_by=run.created_by,
        created_at=run.created_at,
        completed_at=None,
    )


def _trial_claim_lost_error(run_id: str) -> HTTPException:
    return HTTPException(409, detail={
        "code": "trial_claim_lost",
        "message": "试跑执行权已被回收或版本已删除，迟到结果未写入",
        "trialRunId": run_id,
    })


def _load_trial_after_claim_loss(
        db: Session, ontology_id: str, version_id: str, run_id: str,
) -> OntologyTrialRun:
    run = db.query(OntologyTrialRun).filter(
        OntologyTrialRun.id == run_id,
        OntologyTrialRun.ontology_id == ontology_id,
        OntologyTrialRun.version_id == version_id,
    ).first()
    if run is None or run.status == "running":
        raise _trial_claim_lost_error(run_id)
    return run


def _finalize_trial_candidate(
        db: Session, *, ontology_id: str, version_id: str,
        run_id: str, claim_token: str, candidate: SimpleNamespace,
) -> OntologyTrialRun:
    """CAS-like terminal write after reacquiring every authoritative row.

    Materialized child rows remain pending in the Session until these locks and
    invariants pass.  Any drift rolls the whole candidate projection back
    before the persisted run is marked stale.
    """
    now = datetime.now(timezone.utc)
    with db.no_autoflush:
        project = db.query(OntologyProject).filter(
            OntologyProject.id == ontology_id,
        ).populate_existing().with_for_update().first()
        draft = db.query(OntologyVersion).filter(
            OntologyVersion.id == version_id,
            OntologyVersion.ontology_id == ontology_id,
        ).populate_existing().with_for_update().first()
        claimed = db.query(OntologyTrialRun).filter(
            OntologyTrialRun.id == run_id,
            OntologyTrialRun.ontology_id == ontology_id,
            OntologyTrialRun.version_id == version_id,
        ).populate_existing().with_for_update().first()

    if claimed is None:
        db.rollback()
        raise _trial_claim_lost_error(run_id)
    if claimed.status != "running" or claimed.claim_token != claim_token:
        db.rollback()
        return _load_trial_after_claim_loss(
            db, ontology_id, version_id, run_id)

    conflicts: list[str] = []
    if project is None:
        conflicts.append("ontology_missing")
    if draft is None:
        conflicts.append("draft_missing")
    else:
        if draft.node_kind != "draft":
            conflicts.append("node_kind_changed")
        if draft.lifecycle_status != "editing":
            conflicts.append("lifecycle_changed")
        if (draft.revision or 0) != claimed.revision:
            conflicts.append("revision_changed")
        if draft.snapshot_hash != claimed.snapshot_hash:
            conflicts.append("snapshot_hash_changed")
        try:
            formal_hash = snapshot_hash(complete_snapshot(
                draft.snapshot_formal))
        except Exception:
            formal_hash = None
        if formal_hash != claimed.snapshot_hash:
            conflicts.append("snapshot_content_changed")
        if draft.base_release_id != claimed.base_release_id:
            conflicts.append("draft_base_changed")
    if (
        project is not None
        and project.current_release_id != claimed.base_release_id
    ):
        conflicts.append("current_release_changed")
    if not claimed.base_release_id:
        conflicts.append("trial_base_missing")
    if (
        claimed.lease_expires_at is None
        or _as_utc(claimed.lease_expires_at) <= _as_utc(now)
    ):
        conflicts.append("lease_expired")

    if conflicts:
        # Drop every pending trial object/link before updating the durable
        # running row.  This also releases the project/draft/run locks.
        db.rollback()
        claimed = db.query(OntologyTrialRun).filter(
            OntologyTrialRun.id == run_id,
            OntologyTrialRun.ontology_id == ontology_id,
            OntologyTrialRun.version_id == version_id,
        ).with_for_update().first()
        if claimed is None:
            raise _trial_claim_lost_error(run_id)
        if claimed.status != "running" or claimed.claim_token != claim_token:
            db.rollback()
            return _load_trial_after_claim_loss(
                db, ontology_id, version_id, run_id)
        _terminalize_running_trial(
            claimed,
            code="trial_completion_conflict",
            message="试跑完成时草稿或发布基线已变化，迟到结果未写入",
            now=now,
            conflicts=conflicts,
        )
        db.commit()
        return claimed

    if candidate.status not in {"passed", "failed"}:
        db.rollback()
        raise RuntimeError("trial materializer did not produce a terminal status")

    claimed.dataset_versions = candidate.dataset_versions or []
    claimed.result_json = candidate.result_json or {}
    claimed.status = candidate.status
    claimed.completed_at = candidate.completed_at or now
    claimed.claim_token = None
    claimed.lease_expires_at = None
    if candidate.status == "passed":
        draft.lifecycle_status = "trial_ready"
    db.commit()
    return claimed


def list_trial_runs(
    db: Session,
    ontology_id: str,
    version_id: str,
    *,
    _recover_expired_trial_runs,
    _trial_payload,
):
    version = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if version is None:
        raise HTTPException(404, "Version not found")
    if _recover_expired_trial_runs(db, ontology_id, version_id):
        db.commit()
    runs = db.query(OntologyTrialRun).filter(
        OntologyTrialRun.ontology_id == ontology_id,
        OntologyTrialRun.version_id == version_id,
    ).order_by(desc(OntologyTrialRun.created_at)).all()
    return {"data": [_trial_payload(item) for item in runs]}


def get_trial_run(
    db: Session,
    ontology_id: str,
    version_id: str,
    run_id: str,
    *,
    _recover_expired_trial_runs,
    _trial_payload,
):
    if _recover_expired_trial_runs(db, ontology_id, version_id):
        db.commit()
    run = db.query(OntologyTrialRun).filter(
        OntologyTrialRun.id == run_id,
        OntologyTrialRun.ontology_id == ontology_id,
        OntologyTrialRun.version_id == version_id,
    ).first()
    if run is None:
        raise HTTPException(404, "Trial run not found")
    return {"data": _trial_payload(run)}


def _preflight_gate_error(exc: HTTPException) -> dict:
    """把 create_trial_run 链路抛出的结构化 HTTPException 转成 gate_error 形状。"""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    error = _gate_error(
        str(detail.get("code") or "unknown"),
        "",
        str(detail.get("message") or exc.detail),
    )
    for key, value in detail.items():
        if key not in ("code", "message"):
            error[key] = value
    return error


def trial_preflight(
    db: Session,
    ontology_id: str,
    version_id: str,
    *,
    _active_trial_run,
    _current_release,
    _ensure_editable_draft,
    _raise_trial_already_running,
    _snapshot_sentinel_models,
    _validate_sentinels,
    semantic_consistency_fn=None,
):
    """试跑前只读预检：按 create_trial_run 的同一批门禁逐项检查并汇总。

    advisory 语义：不创建试跑记录、不回收过期租约、不取行锁，唯一的
    权威入口仍然是 create_trial_run。
    """
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id,
    ).first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    draft = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if draft is None:
        raise HTTPException(404, "Version not found")

    checks: list[dict] = []

    def record(check_id: str, label: str, errors: list[dict],
               warnings: list[dict] | None = None) -> None:
        warnings = warnings or []
        checks.append({
            "id": check_id,
            "label": label,
            "status": "fail" if errors else ("warn" if warnings else "pass"),
            "errors": errors,
            "warnings": warnings,
        })

    editable_errors: list[dict] = []
    try:
        if draft.node_kind != "draft":
            raise HTTPException(409, detail={
                "code": "trial_requires_draft", "message": "只有草稿分支可以试跑",
            })
        _ensure_editable_draft(draft)
    except HTTPException as exc:
        editable_errors.append(_preflight_gate_error(exc))
    record("editable_draft", "草稿可编辑性", editable_errors)

    # 不调用 _recover_expired_trial_runs（它会写库）；租约回收仍由权威试跑入口负责。
    single_flight_errors: list[dict] = []
    active = _active_trial_run(db, ontology_id, version_id)
    if active is not None:
        try:
            _raise_trial_already_running(active)
        except HTTPException as exc:
            single_flight_errors.append(_preflight_gate_error(exc))
    record("single_flight", "无进行中的试跑", single_flight_errors)

    base_errors: list[dict] = []
    current = _current_release(db, project)
    if draft.base_release_id != current.id:
        try:
            raise HTTPException(409, detail={
                "code": "draft_base_outdated",
                "message": "当前发布版已变化，请从最新发布版创建草稿并合并改动后再试跑",
                "draftBaseReleaseId": draft.base_release_id,
                "currentReleaseId": current.id,
            })
        except HTTPException as exc:
            base_errors.append(_preflight_gate_error(exc))
    record("base_up_to_date", "基线未过期", base_errors)

    snap = complete_snapshot(draft.snapshot_formal)
    structure_errors = validate_snapshot(snap)
    structure_errors.extend(_dynamic_sentinel_id_conflict_errors(
        db, ontology_id, snap["sentinels"],
    ))
    try:
        models = snapshot_models(snap)
        structure_errors.extend(_validate_sentinels(
            _snapshot_sentinel_models(snap), models["objectTypes"],
            models["linkTypes"], models["actions"],
        ))
    except Exception as exc:
        structure_errors.append(_gate_error(
            "sentinel_validation_failed", "sentinel", str(exc)))
    record("structure", "结构校验", structure_errors)

    # 映射覆盖类问题（未映射/未完整映射）降级为 warn：不阻断试跑，只提示
    # 发布后将没有实例数据的风险；绑定完整性问题仍 fail。
    mapping_errors, mapping_coverage = split_mapping_coverage_errors(
        validate_trial_mapping_contract(snap))
    record("mapping_contract", "试跑映射契约", mapping_errors,
           warnings=mapping_coverage)

    # 业务语义层一致性：结构元素必须在业务画布中有对应语义，反之亦然。
    semantic_errors: list[dict] = []
    if semantic_consistency_fn is not None:
        semantic_errors = semantic_consistency_fn(draft.snapshot_semantic, snap)
    record("semantic_consistency", "业务语义一致性", semantic_errors)

    return {"data": {
        "ok": all(check["status"] in ("pass", "warn") for check in checks),
        "versionId": draft.id,
        "revision": f"{draft.revision or 0}:{draft.snapshot_hash or snapshot_hash(snap)}",
        "checks": checks,
    }}


def create_trial_run(
    db: Session,
    ontology_id: str,
    version_id: str,
    body: dict,
    current_user: Any,
    *,
    materialize_trial,
    _active_trial_run,
    _current_release,
    _ensure_editable_draft,
    _finalize_trial_candidate,
    _raise_publish_errors,
    _raise_trial_already_running,
    _recover_expired_trial_runs,
    _snapshot_sentinel_models,
    _terminal_trial_result,
    _trial_lease_deadline,
    _trial_materialization_candidate,
    _trial_payload,
    _validate_sentinels,
    semantic_consistency_fn=None,
):
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id,
    ).with_for_update().first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    draft = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).with_for_update().first()
    if draft is None:
        raise HTTPException(404, "Version not found")
    if draft.node_kind != "draft":
        raise HTTPException(409, detail={
            "code": "trial_requires_draft", "message": "只有草稿分支可以试跑",
        })
    _recover_expired_trial_runs(db, ontology_id, version_id)
    active = _active_trial_run(db, ontology_id, version_id)
    if active is not None:
        _raise_trial_already_running(active)
    _ensure_editable_draft(draft)
    current = _current_release(db, project)
    if draft.base_release_id != current.id:
        raise HTTPException(409, detail={
            "code": "draft_base_outdated",
            "message": "当前发布版已变化，请从最新发布版创建草稿并合并改动后再试跑",
            "draftBaseReleaseId": draft.base_release_id,
            "currentReleaseId": current.id,
        })
    snap = complete_snapshot(draft.snapshot_formal)
    structural_errors = validate_snapshot(snap)
    structural_errors.extend(_dynamic_sentinel_id_conflict_errors(
        db, ontology_id, snap["sentinels"],
    ))
    # 映射覆盖类问题不阻断试跑：物料化结果会携带风险警告；绑定完整性
    # 问题（如 entityClass 歧义路由）仍并入结构性错误阻断。
    structural_errors.extend(split_mapping_coverage_errors(
        validate_trial_mapping_contract(snap))[0])
    try:
        models = snapshot_models(snap)
        structural_errors.extend(_validate_sentinels(
            _snapshot_sentinel_models(snap), models["objectTypes"],
            models["linkTypes"], models["actions"],
        ))
    except Exception as exc:
        structural_errors.append(_gate_error(
            "sentinel_validation_failed", "sentinel", str(exc)))
    # 业务语义层一致性：结构元素必须在业务画布中有对应语义，反之亦然。
    if semantic_consistency_fn is not None:
        structural_errors.extend(semantic_consistency_fn(
            draft.snapshot_semantic, snap))
    _raise_publish_errors(structural_errors, "试跑前发布就绪校验未通过")

    report = impact_report(current.snapshot_formal, snap)
    claim_token = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    run = OntologyTrialRun(
        id=run_id, ontology_id=ontology_id, version_id=draft.id,
        revision=draft.revision or 0,
        snapshot_hash=draft.snapshot_hash or snapshot_hash(snap),
        base_release_id=current.id,
        claim_token=claim_token,
        lease_expires_at=_trial_lease_deadline(),
        status="running", dataset_versions=[], result_json={},
        impact_hash=report["impactHash"], created_by=current_user.id,
    )
    db.add(run)
    # running 记录先落盘；进程中断后不会伪装成“从未试跑”。
    try:
        db.flush()
        candidate = _trial_materialization_candidate(run)
        db.commit()
        # 版本树展示最近试跑状态：发起试跑立即换键（fail-open）。
        ontology_cache.invalidate_version_tree()
    except IntegrityError:
        db.rollback()
        competing = _active_trial_run(db, ontology_id, version_id)
        if competing is not None:
            _raise_trial_already_running(competing)
        raise

    try:
        materialize_trial(db, candidate, snap)
    except Exception as exc:
        # Discard any partially-added trial objects/links.  The detached
        # candidate then follows the same locked terminal-write path as a
        # normal materialization result.
        db.rollback()
        candidate.status = "failed"
        candidate.dataset_versions = []
        candidate.completed_at = datetime.now(timezone.utc)
        candidate.result_json = _terminal_trial_result(
            candidate,
            "trial_internal_error",
            f"试跑事务已回滚: {exc}",
        )

    try:
        run = _finalize_trial_candidate(
            db,
            ontology_id=ontology_id,
            version_id=version_id,
            run_id=run_id,
            claim_token=claim_token,
            candidate=candidate,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # A flush/commit failure must not leave the claim running forever.
        db.rollback()
        candidate.status = "failed"
        candidate.dataset_versions = []
        candidate.completed_at = datetime.now(timezone.utc)
        candidate.result_json = _terminal_trial_result(
            candidate,
            "trial_internal_error",
            f"试跑终态写入已回滚: {exc}",
        )
        run = _finalize_trial_candidate(
            db,
            ontology_id=ontology_id,
            version_id=version_id,
            run_id=run_id,
            claim_token=claim_token,
            candidate=candidate,
        )
    return {"data": _trial_payload(run)}
