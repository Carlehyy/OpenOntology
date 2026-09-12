"""业务探索 HTTP API — /api/v2/exploration/*."""
import json
import logging
import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db, get_current_user
from app.exploration import (
    application_service as _application_service,
    attachment_service as _attachment_service,
    canvas as C,
    converter,
    document_service as _document_service,
    draft_service as _draft_service,
    readiness as R,
    schemas as S,
    session_service as _session_service,
    streaming_service as _streaming_service,
    workspace as W,
)
from app.exploration.diagram import (
    DIAGRAM_KINDS,
    DiagramError,
    build_diagram,
)
from app.exploration.document import (
    document_source_state,
    generate_document,
)
from app.exploration.models import (
    ExplorationAttachment,
    ExplorationDocument,
    ExplorationDraft,
    ExplorationMessage,
    ExplorationSession,
)
from app.exploration.orchestrator import run_exploration_turn
from app.model_configs.selector import (
    llm_call_kwargs,
    select_llm_model_config,
)
from app.models.ontology import OntologyProject
from app.ontologies.access import (
    require_ontology_access,
    require_ontology_create_access,
)
from app.ontologies.release_context import create_initial_release
from app.ontologies.versions.models import OntologyTrialRun, OntologyVersion
from app.ontologies.versions.release_service import (
    collect_publishable_snapshot,
    resolve_current_release,
)
from app.ontologies.versions.trial_service import _stale_previous_trials
from app.ontologies.versions.workspace_service import (
    _diff_formal,
    create_draft_version,
)


router = APIRouter()
logger = logging.getLogger(__name__)


# Historical private helpers remain call-time wrappers, so imports and patches
# against ``app.exploration.router`` retain their behavior.
def _ok(data):
    return _session_service._ok(data)


def _require_session(
    db: Session,
    session_id: str,
    current_user,
) -> ExplorationSession:
    return _session_service._require_session(
        db, session_id, current_user, session_model=ExplorationSession
    )


def _session_out(s: ExplorationSession) -> dict:
    return _session_service._session_out(s, schemas_module=S)


def _message_out(
    message: ExplorationMessage,
    canvas: dict,
) -> dict:
    return _session_service._message_out(
        message,
        canvas,
        schemas_module=S,
        build_diagram_fn=build_diagram,
        diagram_error_cls=DiagramError,
    )


def _document_out(
    document: ExplorationDocument,
    session: ExplorationSession,
    *,
    list_item: bool = False,
) -> dict:
    return _document_service._document_out(
        document,
        session,
        list_item=list_item,
        document_source_state_fn=document_source_state,
        schemas_module=S,
    )


def _remove_attachment_file(path: str | None) -> None:
    return _attachment_service._remove_attachment_file(
        path, os_module=os, logger_obj=logger
    )


def _attachment_out(a: ExplorationAttachment) -> dict:
    return _attachment_service._attachment_out(
        a, schemas_module=S
    )


def _require_document(
    db: Session,
    document_id: str,
    current_user,
) -> ExplorationDocument:
    return _document_service._require_document(
        db,
        document_id,
        current_user,
        document_model=ExplorationDocument,
        require_session_fn=_require_session,
    )


def _require_draft(
    db: Session,
    draft_id: str,
    current_user,
) -> ExplorationDraft:
    return _draft_service._require_draft(
        db,
        draft_id,
        current_user,
        draft_model=ExplorationDraft,
        require_session_fn=_require_session,
    )


def _document_dependencies() -> dict:
    return {
        "select_llm_model_config_fn": select_llm_model_config,
        "llm_call_kwargs_fn": llm_call_kwargs,
        "generate_document_fn": generate_document,
        "document_out_fn": _document_out,
    }


def _draft_dependencies() -> dict:
    return {
        "require_document_fn": _require_document,
        "require_session_fn": _require_session,
        "document_source_state_fn": document_source_state,
        "require_ontology_access_fn": require_ontology_access,
        "select_llm_model_config_fn": select_llm_model_config,
        "llm_call_kwargs_fn": llm_call_kwargs,
        "converter_module": converter,
    }


def _application_dependencies() -> dict:
    return {
        "require_draft_fn": _require_draft,
        "require_ontology_access_fn": require_ontology_access,
        "require_ontology_create_access_fn": require_ontology_create_access,
        "converter_module": converter,
        "project_model": OntologyProject,
        "session_model": ExplorationSession,
        "document_model": ExplorationDocument,
        "version_model": OntologyVersion,
        "trial_run_model": OntologyTrialRun,
        "resolve_current_release_fn": resolve_current_release,
        "create_initial_release_fn": create_initial_release,
        "collect_publishable_snapshot_fn": collect_publishable_snapshot,
        "create_draft_version_fn": create_draft_version,
        "stale_trials_fn": _stale_previous_trials,
        "diff_formal_fn": _diff_formal,
    }


# ---------------------------------------------------------------- 会话


@router.get("/sessions")
def list_sessions(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _session_service.list_sessions(
        db, current_user, session_out_fn=_session_out, ok_fn=_ok
    )


@router.post("/sessions", status_code=201)
def create_session(
    body: S.SessionCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _session_service.create_session(
        body, db, current_user, session_out_fn=_session_out, ok_fn=_ok
    )


@router.get("/draft-ontologies")
def list_draft_ontologies(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """业务澄清分支②入口：有编辑中草稿版本、且当前用户可写的本体清单。"""
    return _session_service.list_draft_bindable_ontologies(db, current_user, ok_fn=_ok)


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _session_service.get_session(
        session_id,
        db,
        current_user,
        require_session_fn=_require_session,
        session_out_fn=_session_out,
        message_out_fn=_message_out,
        ok_fn=_ok,
    )


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _session_service.delete_session(
        session_id,
        db,
        current_user,
        require_session_fn=_require_session,
        remove_attachment_file_fn=_remove_attachment_file,
    )


@router.get("/sessions/{session_id}/canvas")
def get_canvas(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _session_service.get_canvas(
        session_id, db, current_user,
        require_session_fn=_require_session, ok_fn=_ok,
    )


@router.get("/sessions/{session_id}/readiness")
def get_readiness(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """质量门报告（确定性评估，与草稿生成闸门同一口径）。"""
    return _session_service.get_readiness(
        session_id, db, current_user,
        require_session_fn=_require_session, ok_fn=_ok,
    )


@router.get("/sessions/{session_id}/ontology-preview")
def get_ontology_preview(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """本体预览投影（只读）：当前画布确定性转换为五类集合，与绑定版本基线比对
    得出 add/exists/conflict 去向；不写版本，版本落库仍走质量门+人工确认。"""
    return _session_service.get_ontology_preview(
        session_id, db, current_user,
        require_session_fn=_require_session, ok_fn=_ok,
    )


@router.get("/sessions/{session_id}/diagrams/{kind}")
def get_diagram(
    session_id: str,
    kind: str,
    target: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """画布 → 业务建模图表（er/flow/sequence/state，确定性生成，不经 LLM）。

    flow/sequence 可用 ?target=场景名 指定场景；state 用 ?target=对象名。
    """
    return _session_service.get_diagram(
        session_id,
        kind,
        target,
        db,
        current_user,
        require_session_fn=_require_session,
        build_diagram_fn=build_diagram,
        diagram_error_cls=DiagramError,
        ok_fn=_ok,
    )


@router.get("/sessions/{session_id}/diagrams")
def list_diagram_kinds(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _session_service.list_diagram_kinds(
        session_id,
        db,
        current_user,
        require_session_fn=_require_session,
        diagram_kinds=DIAGRAM_KINDS,
        ok_fn=_ok,
    )


# ---------------------------------------------------------------- 会话附件


@router.get("/sessions/{session_id}/attachments")
def list_attachments(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _attachment_service.list_attachments(
        session_id,
        db,
        current_user,
        require_session_fn=_require_session,
        attachment_out_fn=_attachment_out,
        ok_fn=_ok,
    )


@router.post("/sessions/{session_id}/attachments", status_code=201)
async def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """上传会话参考资料：确定性转为文本后随每个对话回合注入引导师上下文。
    附件严格绑定本会话，跨会话不可见，随会话删除一并清理。"""
    return await _attachment_service.upload_attachment(
        session_id,
        file,
        db,
        current_user,
        require_session_fn=_require_session,
        settings_obj=settings,
        workspace_module=W,
        attachment_out_fn=_attachment_out,
        ok_fn=_ok,
    )


@router.post("/sessions/{session_id}/workspace/files", status_code=201)
def create_workspace_text_file(
    session_id: str,
    body: S.WorkspaceTextCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """在会话空间中新建文本文件（用户与 Agent 使用同一套并发/隔离契约）。"""
    return _attachment_service.create_workspace_text_file(
        session_id, body, db, current_user,
        require_session_fn=_require_session,
        attachment_out_fn=_attachment_out, ok_fn=_ok,
    )


@router.get("/sessions/{session_id}/attachments/{attachment_id}/content")
def get_workspace_text_file(
    session_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _attachment_service.get_workspace_text_file(
        session_id, attachment_id, db, current_user,
        require_session_fn=_require_session, ok_fn=_ok,
    )


@router.get("/sessions/{session_id}/attachments/{attachment_id}/preview")
def preview_workspace_file(
    session_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """预览常见文件：文本读取原文，Office/PDF 等返回确定性抽取文本。"""
    return _attachment_service.preview_workspace_file(
        session_id, attachment_id, db, current_user,
        require_session_fn=_require_session, ok_fn=_ok,
    )


@router.put("/sessions/{session_id}/attachments/{attachment_id}/content")
def update_workspace_text_file(
    session_id: str,
    attachment_id: str,
    body: S.WorkspaceTextUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _attachment_service.update_workspace_text_file(
        session_id,
        attachment_id,
        body,
        db,
        current_user,
        require_session_fn=_require_session,
        attachment_out_fn=_attachment_out,
        ok_fn=_ok,
    )


@router.get("/sessions/{session_id}/attachments/{attachment_id}/download")
def download_workspace_file(
    session_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _attachment_service.download_workspace_file(
        session_id,
        attachment_id,
        db,
        current_user,
        require_session_fn=_require_session,
        file_response_cls=FileResponse,
    )


@router.delete(
    "/sessions/{session_id}/attachments/{attachment_id}",
    status_code=204,
)
def delete_attachment(
    session_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _attachment_service.delete_attachment(
        session_id, attachment_id, db, current_user,
        require_session_fn=_require_session,
    )


# ---------------------------------------------------------------- 对话


@router.post("/sessions/{session_id}/chat")
def chat(
    session_id: str,
    body: S.ChatRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _streaming_service.chat(
        session_id,
        body,
        db,
        current_user,
        require_session_fn=_require_session,
        run_turn_fn=run_exploration_turn,
        ok_fn=_ok,
        json_module=json,
        streaming_response_cls=StreamingResponse,
    )


# ---------------------------------------------------------------- 需求文档


@router.post("/sessions/{session_id}/documents", status_code=201)
def create_document(
    session_id: str,
    body: S.GenerateDocumentRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _document_service.create_document(
        session_id, body, db, current_user,
        require_session_fn=_require_session, ok_fn=_ok,
        **_document_dependencies(),
    )


@router.get("/sessions/{session_id}/documents")
def list_documents(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _document_service.list_documents(
        session_id, db, current_user,
        require_session_fn=_require_session,
        document_out_fn=_document_out, ok_fn=_ok,
    )


@router.get("/documents/{document_id}")
def get_document(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _document_service.get_document(
        document_id, db, current_user,
        require_document_fn=_require_document,
        require_session_fn=_require_session,
        document_out_fn=_document_out, ok_fn=_ok,
    )


# ---------------------------------------------------------------- 本体草稿


@router.post("/documents/{document_id}/drafts", status_code=201)
def create_draft(
    document_id: str,
    body: S.GenerateDraftRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """需求文档 → 本体草稿。质量门是准入闸：堵门项未清零时拒绝生成，
    除非 force=true 显式越权（越权事实与未决项写入草稿报告，人审可见）。"""
    return _draft_service.create_draft(
        document_id, body, db, current_user,
        readiness_module=R, draft_model=ExplorationDraft,
        schemas_module=S, ok_fn=_ok, **_draft_dependencies(),
    )


@router.get("/sessions/{session_id}/drafts")
def list_drafts(
    session_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _draft_service.list_drafts(
        session_id, db, current_user,
        require_session_fn=_require_session, schemas_module=S, ok_fn=_ok,
    )


@router.get("/drafts/{draft_id}")
def get_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _draft_service.get_draft(
        draft_id, db, current_user,
        require_draft_fn=_require_draft, schemas_module=S, ok_fn=_ok,
    )


@router.post("/drafts/{draft_id}/validate")
def validate_draft(
    draft_id: str,
    body: S.DraftValidationRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """按最终选择集执行确定性预检；与 apply 使用完全相同的校验函数。"""
    return _draft_service.validate_draft(
        draft_id,
        body,
        db,
        current_user,
        require_draft_fn=_require_draft,
        require_ontology_access_fn=require_ontology_access,
        converter_module=converter,
        ok_fn=_ok,
    )


@router.post("/drafts/{draft_id}/apply")
def apply_draft(
    draft_id: str,
    body: S.ApplyDraftRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """人审勾选后的真实落地。草稿→本体是唯一写路径，且保守合并（同名跳过）。

    可重复应用（同名跳过使其幂等）：部分勾选落地后，剩余元素可再次勾选落地；
    首次落地后再次应用固定合并进首次的目标本体，不再新建。废弃的草稿不可应用。
    """
    return _application_service.apply_draft(
        draft_id, body, db, current_user,
        ok_fn=_ok, **_application_dependencies(),
    )


@router.post("/drafts/{draft_id}/discard")
def discard_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """废弃草稿（幂等）：废弃后不可再应用；记录保留在列表中可追溯。"""
    return _draft_service.discard_draft(
        draft_id, db, current_user,
        require_draft_fn=_require_draft, schemas_module=S, ok_fn=_ok,
    )
