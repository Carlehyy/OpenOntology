"""Draft generation, query, validation, and lifecycle services."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.exploration import converter
from app.exploration import readiness as R
from app.exploration import schemas as S
from app.exploration.document import document_source_state
from app.exploration.document_service import _require_document
from app.exploration.models import (
    ExplorationDraft,
    ExplorationSession,
)
from app.exploration.session_service import _ok, _require_session
from app.model_configs.selector import (
    llm_call_kwargs,
    select_llm_model_config,
)
from app.ontologies.access import require_ontology_access


def _require_draft(
    db: Session,
    draft_id: str,
    current_user,
    *,
    draft_model=ExplorationDraft,
    require_session_fn=_require_session,
) -> ExplorationDraft:
    draft = db.query(draft_model).filter(
        draft_model.id == draft_id
    ).first()
    if not draft:
        raise HTTPException(404, "本体草稿不存在")
    require_session_fn(db, draft.session_id, current_user)
    return draft


def create_draft(
    document_id: str,
    body,
    db: Session,
    current_user,
    *,
    require_document_fn=_require_document,
    require_session_fn=_require_session,
    document_source_state_fn=document_source_state,
    readiness_module=R,
    require_ontology_access_fn=require_ontology_access,
    converter_module=converter,
    select_llm_model_config_fn=select_llm_model_config,
    llm_call_kwargs_fn=llm_call_kwargs,
    draft_model=ExplorationDraft,
    schemas_module=S,
    ok_fn: Callable[[Any], dict] = _ok,
):
    document = require_document_fn(db, document_id, current_user)
    session = require_session_fn(
        db,
        document.session_id,
        current_user,
    )
    source_state = document_source_state_fn(document, session)
    if source_state["is_stale"] and not body.force:
        raise HTTPException(
            409,
            detail={
                "code": "stale_document",
                "message": (
                    "该需求文档对应的画布已发生变化。请从当前画布重新生成文档，"
                    "或显式 force=true 使用旧快照生成并留下越权记录。"
                ),
                "source": {
                    "sourceCanvasVersion": (
                        source_state["source_canvas_version"]
                    ),
                    "sourceCanvasFingerprint": (
                        source_state["source_canvas_fingerprint"]
                    ),
                    "currentCanvasVersion": (
                        source_state["current_canvas_version"]
                    ),
                    "currentCanvasFingerprint": (
                        source_state["current_canvas_fingerprint"]
                    ),
                    "isStale": True,
                },
            },
        )

    readiness = readiness_module.evaluate(document.canvas_snapshot or {})
    if not readiness["ready"] and not body.force:
        raise HTTPException(
            422,
            detail={
                "code": "quality_gate_blocked",
                "message": (
                    "质量门未通过"
                    f"（{readiness['gatesPassed']}/"
                    f"{readiness['gatesTotal']} 门，"
                    f"剩余 {readiness['blockingCount']} 项堵门问题）。"
                    "请回到对话完成定量澄清后重新生成文档，"
                    "或显式越权强制生成。"
                ),
                "readiness": readiness,
            },
        )

    # 目标本体：显式传参优先；缺省时会话已绑定本体则默认沉淀回该本体，
    # 未绑定会话维持 None（应用时新建本体）。
    target_ontology_id = body.target_ontology_id or session.ontology_id
    existing = None
    if target_ontology_id:
        require_ontology_access_fn(
            db,
            target_ontology_id,
            current_user,
            write=True,
        )
        existing = converter_module.existing_name_sets(
            db,
            target_ontology_id,
        )

    config = select_llm_model_config_fn(db, model_id=body.model_id)
    try:
        call_kwargs = llm_call_kwargs_fn(config)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    draft_data, report = converter_module.build_draft(
        document.canvas_snapshot or {},
        existing=existing,
        call_kwargs=call_kwargs,
    )
    blocking_semantics = [
        item
        for item in report.get("semanticIssues", [])
        if item.get("severity") == "blocking"
    ]
    if blocking_semantics and not body.force:
        raise HTTPException(
            422,
            detail={
                "code": "semantic_conversion_blocked",
                "message": (
                    f"画布有 {len(blocking_semantics)} 项语义无法无损转换，"
                    "已拒绝生成可落地草稿。"
                    "请修正目标引用/规则作用域后重新生成，"
                    "或显式 force=true 越权。"
                ),
                "semanticIssues": blocking_semantics,
            },
        )

    report["sourceDocument"] = {
        "sourceCanvasVersion": source_state["source_canvas_version"],
        "sourceCanvasFingerprint": (
            source_state["source_canvas_fingerprint"]
        ),
        "currentCanvasVersion": source_state["current_canvas_version"],
        "currentCanvasFingerprint": (
            source_state["current_canvas_fingerprint"]
        ),
        "isStale": source_state["is_stale"],
    }
    if source_state["is_stale"]:
        report["staleDocumentOverride"] = True
        report["warnings"] = [
            "⚠️ 使用已过期需求文档的画布快照强制生成；"
            "当前画布与文档来源指纹不一致"
        ] + report.get("warnings", [])
    if blocking_semantics:
        report["semanticOverride"] = True
        report["warnings"] = [
            f"⚠️ {len(blocking_semantics)} 项不可无损转换语义被显式越权；"
            "对应元素仅以可保留部分生成，必须重点人工复核"
        ] + report.get("warnings", [])

    # Persist the exact gate decision shown during human review.
    report["readiness"] = {
        key: readiness[key]
        for key in (
            "ready",
            "stage",
            "gatesPassed",
            "gatesTotal",
            "blockingCount",
            "advisoryCount",
        )
    }
    if not readiness["ready"]:
        report["gateOverride"] = True
        blocking = [
            f"[{gate['label']}] {item}"
            for gate in readiness["gates"]
            for item in gate["blockingItems"]
        ]
        report["warnings"] = [
            "⚠️ 质量门未通过被显式越权："
            f"{readiness['blockingCount']} 项堵门问题未解决即生成草稿"
        ] + blocking[:12] + report.get("warnings", [])

    row = draft_model(
        session_id=document.session_id,
        document_id=document.id,
        target_ontology_id=target_ontology_id,
        draft=draft_data,
        report=report,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ok_fn(
        schemas_module.DraftOut.model_validate(row).model_dump(
            by_alias=True
        )
    )


def list_drafts(
    session_id: str,
    db: Session,
    current_user,
    *,
    require_session_fn=_require_session,
    draft_model=ExplorationDraft,
    schemas_module=S,
    ok_fn: Callable[[Any], dict] = _ok,
):
    session = require_session_fn(db, session_id, current_user)
    rows = (
        db.query(draft_model)
        .filter(draft_model.session_id == session.id)
        .order_by(draft_model.created_at.desc())
        .all()
    )
    return ok_fn([
        schemas_module.DraftOut.model_validate(row).model_dump(
            by_alias=True
        )
        for row in rows
    ])


def get_draft(
    draft_id: str,
    db: Session,
    current_user,
    *,
    require_draft_fn=_require_draft,
    schemas_module=S,
    ok_fn: Callable[[Any], dict] = _ok,
):
    draft = require_draft_fn(db, draft_id, current_user)
    return ok_fn(
        schemas_module.DraftOut.model_validate(draft).model_dump(
            by_alias=True
        )
    )


def validate_draft(
    draft_id: str,
    body,
    db: Session,
    current_user,
    *,
    require_draft_fn=_require_draft,
    require_ontology_access_fn=require_ontology_access,
    converter_module=converter,
    session_model=ExplorationSession,
    ok_fn: Callable[[Any], dict] = _ok,
):
    draft = require_draft_fn(db, draft_id, current_user)
    target_id = (
        draft.applied_ontology_id
        or draft.target_ontology_id
    )
    existing = None
    if target_id:
        project = require_ontology_access_fn(
            db,
            target_id,
            current_user,
            write=True,
        )
        # 合并路径的预检基线与 apply 同一口径：目标草稿版本/当前发布的结构
        # 快照优先（live 表会漏掉草稿内未发布元素），无快照时回退 live 表。
        session = db.query(session_model).filter(
            session_model.id == draft.session_id
        ).first()
        baseline = converter_module.resolve_merge_baseline_snapshot(
            db,
            target_id,
            bound_version_id=(
                getattr(session, "ontology_version_id", None)
                if session is not None else None
            ),
            current_release_id=getattr(project, "current_release_id", None),
        )
        existing = converter_module.existing_name_sets(
            db, target_id, snapshot=baseline,
        )
    return ok_fn(converter_module.validate_draft_selection(
        draft.draft or {},
        body.selected_keys,
        existing=existing,
    ))


def discard_draft(
    draft_id: str,
    db: Session,
    current_user,
    *,
    require_draft_fn=_require_draft,
    schemas_module=S,
    ok_fn: Callable[[Any], dict] = _ok,
):
    draft = require_draft_fn(db, draft_id, current_user)
    if draft.status != "discarded":
        draft.status = "discarded"
        db.commit()
        db.refresh(draft)
    return ok_fn(
        schemas_module.DraftOut.model_validate(draft).model_dump(
            by_alias=True
        )
    )
