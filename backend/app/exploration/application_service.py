"""Human-reviewed draft application workflow for Exploration.

apply_draft 两条路径：
  - 新建本体：converter.apply_draft 写 fo_* live 表 → create_initial_release
    冻结 v0 基线（携带业务语义层）→ mark_projecting + Neo4j 重建。
  - 合并进已有本体：走版本正门 —— converter.apply_draft_to_snapshot 把勾选
    元素合并进目标草稿版本（会话绑定的有效草稿，或从当前发布分叉的新草稿）
    的结构快照，并整体更新版本业务语义层；不触碰 live 表、不建 release、
    不重建图投影（发布时才物化）。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.exploration import converter
from app.exploration.document import _SOURCE_META_KEY, canvas_fingerprint
from app.exploration.draft_service import _require_draft
from app.exploration.models import ExplorationDocument, ExplorationSession
from app.exploration.session_service import _ok
from app.models.ontology import OntologyProject
from app.ontologies.access import (
    require_ontology_access,
    require_ontology_create_access,
)
from app.ontologies.release_context import create_initial_release
from app.ontologies.projection_state import (
    ProjectionRebuildError,
    mark_projecting,
    rebuild_after_commit,
)
from app.ontologies.runtime_fence import _ontology_build_lock
from app.ontologies.versions.models import OntologyTrialRun, OntologyVersion
from app.ontologies.versions.release_service import (
    collect_publishable_snapshot,
    resolve_current_release,
)
from app.ontologies.versions.snapshot_contract import json_safe
from app.ontologies.versions.trial_service import _stale_previous_trials
from app.ontologies.versions.workspace_service import (
    _diff_formal,
    create_draft_version,
)


def _finish_projection(db: Session, ontology_id: str) -> None:
    try:
        rebuild_after_commit(db, ontology_id)
    except ProjectionRebuildError as exc:
        raise HTTPException(503, detail={
            "code": "ontology_projection_failed",
            "message": (
                "业务探索草稿已保存到关系型真相，但 Neo4j 图投影失败；"
                "图读取已阻断，请执行图修复"
            ),
            "ontology_id": ontology_id,
        }) from exc


def _semantic_revision_of(snapshot_semantic: Any) -> int:
    semantic = snapshot_semantic if isinstance(snapshot_semantic, dict) else {}
    revision = semantic.get("semanticRevision")
    return revision if isinstance(revision, int) else 0


def _build_semantic_layer(
    db: Session,
    draft,
    *,
    document_model=ExplorationDocument,
    base_revision: int = 0,
) -> dict | None:
    """从需求文档构造版本业务语义层（画布 + 文档 + 指纹 + 血缘）。

    画布取自文档生成时刻的快照并剔除 _document_source 元键（文档自己的来源
    封装不参与业务内容）；文档缺失时不阻塞落地，返回 None 保留既有语义层。
    """
    document = db.query(document_model).filter(
        document_model.id == draft.document_id
    ).first()
    if document is None:
        return None
    raw_canvas = (
        document.canvas_snapshot
        if isinstance(document.canvas_snapshot, dict)
        else {}
    )
    canvas = {k: v for k, v in raw_canvas.items() if k != _SOURCE_META_KEY}
    document_md = document.content_md or ""
    return json_safe({
        "canvas": canvas,
        "canvasFingerprint": canvas_fingerprint(canvas),
        "documentMd": document_md,
        "documentTitle": document.title or "",
        "documentFingerprint": hashlib.sha256(
            document_md.encode("utf-8")).hexdigest(),
        "sourceSessionId": draft.session_id,
        "sourceDocumentId": draft.document_id,
        "semanticRevision": int(base_revision or 0) + 1,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    })


def _locked_validation_error(validation: dict) -> HTTPException:
    return HTTPException(
        422,
        detail={
            "code": "draft_validation_failed",
            "message": (
                "本体草稿选择集锁内复检未通过"
                f"（{len(validation['errors'])} 项错误），已拒绝落地"
            ),
            "validation": validation,
        },
    )


def apply_draft(
    draft_id: str,
    body,
    db: Session,
    current_user,
    *,
    require_draft_fn=_require_draft,
    require_ontology_access_fn=require_ontology_access,
    require_ontology_create_access_fn=require_ontology_create_access,
    converter_module=converter,
    project_model=OntologyProject,
    session_model=ExplorationSession,
    document_model=ExplorationDocument,
    version_model=OntologyVersion,
    trial_run_model=OntologyTrialRun,
    resolve_current_release_fn=resolve_current_release,
    create_initial_release_fn=create_initial_release,
    collect_publishable_snapshot_fn=collect_publishable_snapshot,
    create_draft_version_fn=create_draft_version,
    stale_trials_fn=_stale_previous_trials,
    diff_formal_fn=_diff_formal,
    ok_fn: Callable[[Any], dict] = _ok,
):
    """Apply one reviewed selection in a single relational transaction."""
    draft = require_draft_fn(db, draft_id, current_user)
    if draft.status == "discarded":
        raise HTTPException(
            409,
            "该草稿已废弃，不可应用；如需落地请重新生成草稿",
        )
    if (
        body.selected_keys is not None
        and len(body.selected_keys) == 0
    ):
        raise HTTPException(422, "未勾选任何草稿元素")

    session = db.query(session_model).filter(
        session_model.id == draft.session_id
    ).first()
    bound_version_id = (
        getattr(session, "ontology_version_id", None)
        if session is not None else None
    )
    validation_target_id = (
        draft.applied_ontology_id
        or draft.target_ontology_id
    )
    if validation_target_id:
        target_project = require_ontology_access_fn(
            db,
            validation_target_id,
            current_user,
            write=True,
        )
    else:
        require_ontology_create_access_fn(current_user)
        target_project = None
    validation_existing = None
    if validation_target_id:
        # 合并路径的预检基线：目标草稿版本/当前发布的结构快照（权威真相）；
        # 无可用快照时回退 live 表口径（与 legacy 基线修复内容同构）。
        baseline = converter_module.resolve_merge_baseline_snapshot(
            db,
            validation_target_id,
            bound_version_id=bound_version_id,
            current_release_id=getattr(
                target_project, "current_release_id", None),
        )
        validation_existing = converter_module.existing_name_sets(
            db,
            validation_target_id,
            snapshot=baseline,
        )
    validation = converter_module.validate_draft_selection(
        draft.draft or {},
        body.selected_keys,
        existing=validation_existing,
    )
    if not validation["valid"]:
        raise HTTPException(
            422,
            detail={
                "code": "draft_validation_failed",
                "message": (
                    "本体草稿选择集预检未通过"
                    f"（{len(validation['errors'])} 项错误），已拒绝落地"
                ),
                "validation": validation,
            },
        )

    project = None
    created_project = False
    release = None
    if draft.applied_ontology_id:
        # Re-application stays pinned to the first materialized ontology.
        project = db.query(project_model).filter(
            project_model.id == draft.applied_ontology_id
        ).first()
    if project is None and draft.target_ontology_id:
        project = db.query(project_model).filter(
            project_model.id == draft.target_ontology_id
        ).first()
        if not project:
            raise HTTPException(
                404,
                "目标本体不存在（可能已被删除）",
            )
    if project is None:
        if (
            not body.new_ontology
            or not (body.new_ontology.name or "").strip()
        ):
            raise HTTPException(
                422,
                "该草稿目标为新建本体，请提供 newOntology.name",
            )
        name = body.new_ontology.name.strip()
        if db.query(project_model).filter(
            project_model.name.ilike(name)
        ).first():
            raise HTTPException(409, f"本体名称「{name}」已存在")
        project = project_model(
            name=name,
            domain=(
                body.new_ontology.domain or "业务探索"
            ).strip() or "业务探索",
            description=(
                body.new_ontology.description
                or f"由业务探索会话生成（草稿 {draft.id[:8]}）"
            ),
            build_mode="business_exploration",
            created_by=getattr(current_user, "id", None),
        )
        db.add(project)
        db.flush()
        created_project = True

    with _ontology_build_lock(db, project.id):
        # Re-resolve and lock the project after acquiring the canonical
        # ontology fence.  The access/selection preflight above deliberately
        # happens before any write, but another writer could have changed the
        # target structure while this request was waiting for the lock.
        locked_project = db.query(project_model).filter(
            project_model.id == project.id,
        ).with_for_update().first()
        if locked_project is None:
            raise HTTPException(404, "目标本体不存在（可能已被删除）")
        project = locked_project

        lineage = {
            "sessionId": draft.session_id,
            "documentId": draft.document_id,
            "draftId": draft.id,
        }
        applied_version_id = None
        applied_version_number = None
        if not created_project:
            # 合并路径：走版本正门，把勾选元素写进目标草稿版本的结构快照。
            target_version = None
            if bound_version_id:
                bound = db.query(version_model).filter(
                    version_model.id == str(bound_version_id),
                    version_model.ontology_id == project.id,
                ).with_for_update().first()
                if (
                    bound is not None
                    and bound.node_kind == "draft"
                    and bound.lifecycle_status == "editing"
                ):
                    target_version = bound
            if target_version is None:
                current_release = resolve_current_release_fn(db, project)
                baseline_snapshot = current_release.snapshot_formal
            else:
                baseline_snapshot = target_version.snapshot_formal
            locked_validation = converter_module.validate_draft_selection(
                draft.draft or {},
                body.selected_keys,
                snapshot=baseline_snapshot,
            )
            if not locked_validation["valid"]:
                raise _locked_validation_error(locked_validation)
            if target_version is None:
                # 分叉前先把会话锚到目标本体：分叉内部提交会一并持久化锚点，
                # 后续失败重试仍合并进同一草稿分支。
                if session is not None:
                    session.ontology_id = project.id
                forked = create_draft_version_fn(
                    db,
                    project.id,
                    current_release.id,
                    {
                        "version_label": "业务探索合并",
                        "description": (
                            "由业务探索草稿合并创建"
                            f"（草稿 {draft.id[:8]}）"
                        ),
                    },
                    current_user,
                )
                target_version = db.query(version_model).filter(
                    version_model.id == forked["data"]["id"],
                ).with_for_update().first()
                if target_version is None:
                    raise HTTPException(404, "合并目标草稿版本创建失败")
            if session is not None:
                session.ontology_id = project.id
                session.ontology_version_id = target_version.id
            running_trial = db.query(trial_run_model).filter(
                trial_run_model.version_id == target_version.id,
                trial_run_model.status == "running",
            ).first()
            if running_trial is not None:
                raise HTTPException(409, detail={
                    "code": "trial_running",
                    "message": "试跑进行中，语义层与结构已锁定",
                    "trialRunId": running_trial.id,
                })
            result = converter_module.apply_draft_to_snapshot(
                db,
                draft.draft or {},
                body.selected_keys,
                target_version,
                lineage=lineage,
                stale_trials_fn=stale_trials_fn,
                diff_formal_fn=diff_formal_fn,
            )
            semantic = _build_semantic_layer(
                db,
                draft,
                document_model=document_model,
                base_revision=_semantic_revision_of(
                    target_version.snapshot_semantic
                ),
            )
            if semantic is not None:
                target_version.snapshot_semantic = semantic
            applied_version_id = target_version.id
            applied_version_number = target_version.version_number
        else:
            # 新建本体路径：live 表落地 + 冻结 v0 基线（带业务语义层）。
            locked_validation = converter_module.validate_draft_selection(
                draft.draft or {},
                body.selected_keys,
                existing=converter_module.existing_name_sets(db, project.id),
            )
            if not locked_validation["valid"]:
                raise _locked_validation_error(locked_validation)
            result = converter_module.apply_draft(
                db,
                draft.draft or {},
                body.selected_keys,
                project.id,
                lineage=lineage,
            )
            release = create_initial_release_fn(
                db,
                project,
                snapshot=collect_publishable_snapshot_fn(
                    db,
                    project.id,
                ),
                created_by=getattr(current_user, "id", None),
                version_label="业务探索初始基线",
                description="由业务探索草稿生成的完整发布基线",
                semantic=_build_semantic_layer(
                    db,
                    draft,
                    document_model=document_model,
                ),
            )
            applied_version_id = release.id
            applied_version_number = release.version_number
            if session is not None:
                session.ontology_id = project.id
                session.ontology_version_id = release.id
            # Schema types affect how current and future runtime rows are
            # labelled in Neo4j.  Persist the fence atomically with the
            # reviewed draft even on an idempotent re-apply, so retrying a
            # prior failed response repairs the complete projection instead
            # of reporting a false success.
            mark_projecting(db, project.id)
        draft.status = "applied"
        draft.applied_ontology_id = project.id
        draft.applied_version_id = applied_version_id
        db.commit()
        # 落地即业务文档出生点（新建本体的 v0 自带语义层）：提交后广播
        # 自包含事件供宫殿共享建图。必须在 commit 之后——提交前广播会在
        # 事务失败时留下永不自愈的幻影镜像（重试落地会创建新的本体 id）。
        if created_project and release is not None:
            from app.ontologies.published_documents import notify_published_document

            notify_published_document(project, release)
        if created_project:
            _finish_projection(db, project.id)
        return ok_fn({
            "ontologyId": project.id,
            "ontologyName": project.name,
            **result,
            "versionId": applied_version_id,
            "versionNumber": applied_version_number,
        })
