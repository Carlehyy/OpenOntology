from __future__ import annotations

# Compatibility imports are intentionally retained: older tests and extensions
# patch these names on the router module. Handlers pass the sensitive seams into
# application services at request time.
import io
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.model_configs.models import ModelConfig
from app.super_assistant import conversation_files
from app.super_assistant import (
    conversation_service as _conversation_service,
)
from app.super_assistant import mcp_server_service
from app.super_assistant import memory_service
from app.super_assistant import palace_consolidate
from app.super_assistant import palace_service
from app.super_assistant import reflection_service
from app.super_assistant.palace_service import (
    PalaceContentUpdate,
    PalaceFileMove,
    PalaceFolderCreate,
    PalaceFolderRename,
    PalaceNoteCreate,
)
from app.super_assistant import skill_service as _skill_service
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMcpServer,
    SuperAssistantMessage,
    SuperAssistantSkill,
    SuperAssistantToolRun,
)
from app.super_assistant.runtime import stream_chat
from app.super_assistant.schemas import (
    ApprovalRequest,
    ChatRequest,
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    McpTestOut,
    MemoryCreate,
    MemoryDistillReport,
    MemoryDistillRequest,
    MemoryOut,
    MemoryUpdate,
    MessageOut,
    ReflectionCandidateOut,
    ReflectionDecisionRequest,
    ReflectionFullAccepted,
    ReflectionFullRequest,
    ReflectionSettingsOut,
    ReflectionSettingsUpdate,
    SkillCreate,
    SkillFileContent,
    SkillOut,
    SkillUpdate,
)
from app.super_assistant.skill_store import (
    SkillStoreError,
    build_manifest,
    create_skill_folder,
    delete_file,
    delete_skill_folder,
    export_skill_archive,
    import_skill_archive,
    parse_skill_markdown,
    read_text_file,
    render_skill_markdown,
    skill_directory,
    write_text_file,
)
from app.super_assistant.search import search_conversations  # noqa: F401  # 再导出供架构测试签名守卫
from app.super_assistant.multica import (  # noqa: F401  # 再导出供架构测试签名守卫
    get_multica_config,
    list_multica_workspaces,
    test_multica_connection,
    update_multica_config,
)
from app.settings.object_storage.service import (
    get_workspace_minio_service,
    minio_tool_manifest,
)


router = APIRouter()

# Private compatibility helpers remain importable by identity while their
# implementation now lives with the application transaction it protects.
_conversation = _conversation_service._conversation
_skill = _skill_service._skill
_storage_error = _skill_service._storage_error


def _mcp_http_error(
    exc: mcp_server_service.McpServerServiceError,
) -> HTTPException:
    if isinstance(exc, mcp_server_service.McpServerNotFoundError):
        status_code = 404
    elif isinstance(exc, mcp_server_service.McpServerConflictError):
        status_code = 409
    elif isinstance(
        exc,
        mcp_server_service.McpServerUnavailableError,
    ):
        status_code = 503
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=str(exc))


def _memory_conflict_response(
    exc: memory_service.MemoryConflictError,
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "existing": {
                "id": exc.existing_id,
                "content": exc.existing_content,
                "similarity": exc.similarity,
            },
        },
    )


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _conversation_service.list_conversations(db, current_user)


@router.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=201,
)
def create_conversation(
    body: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _conversation_service.create_conversation(
        body,
        db,
        current_user,
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationOut,
)
def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _conversation_service.update_conversation(
        conversation_id,
        body,
        db,
        current_user,
        conversation_lookup_fn=_conversation,
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=204,
)
def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _conversation_service.delete_conversation(
        conversation_id,
        db,
        current_user,
        conversation_lookup_fn=_conversation,
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
def list_messages(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _conversation_service.list_messages(
        conversation_id,
        db,
        current_user,
        conversation_lookup_fn=_conversation,
    )


@router.post("/conversations/{conversation_id}/chat")
def chat(
    conversation_id: str,
    body: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _conversation_service.chat(
        conversation_id,
        body,
        db,
        current_user,
        conversation_lookup_fn=_conversation,
        stream_chat_fn=stream_chat,
    )


@router.post(
    "/conversations/{conversation_id}/cancel",
    status_code=202,
)
def cancel_chat(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _conversation_service.cancel_chat(
        conversation_id,
        db,
        current_user,
        conversation_lookup_fn=_conversation,
    )


@router.get("/conversations/{conversation_id}/files")
def list_conversation_files(conversation_id: str, db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    return conversation_files.list_files(db, current_user, conversation_id)

@router.post("/conversations/{conversation_id}/files", status_code=201)
async def upload_conversation_file(conversation_id: str, file: UploadFile = File(...),
                                   db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return conversation_files.upload_file(db, current_user, conversation_id, file)

@router.get("/conversations/{conversation_id}/files/{artifact_id}")
def download_conversation_file(conversation_id: str, artifact_id: str, db: Session = Depends(get_db),
                               current_user: User = Depends(get_current_user)):
    row, path = conversation_files.download_file(db, current_user, conversation_id, artifact_id)
    return FileResponse(path, filename=row["filename"], media_type=row.get("mimeType"))

@router.get("/conversations/{conversation_id}/files/{artifact_id}/preview")
def preview_conversation_file(conversation_id: str, artifact_id: str, max_chars: int = Query(60000, le=100000),
                              db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return conversation_files.preview_file(db, current_user, conversation_id, artifact_id, max_chars)

@router.delete("/conversations/{conversation_id}/files/{artifact_id}", status_code=204)
def delete_conversation_file(conversation_id: str, artifact_id: str, db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    conversation_files.delete_file(db, current_user, conversation_id, artifact_id)


# ---------------------------------------------------------------------------
# 记忆宫殿：用户级文件库 + 知识图谱（跨会话长期知识）
# ---------------------------------------------------------------------------

@router.get("/palace/files")
def list_palace_files(db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    return palace_service.list_files(db, current_user.id)

@router.post("/palace/files", status_code=201)
async def upload_palace_file(file: UploadFile = File(...),
                             db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
                             folder_path: str = Form("")):
    return palace_service.upload_file(db, current_user, file, folder_path)

@router.post("/palace/files/batch", status_code=201)
def batch_import_palace_files(archive: UploadFile = File(...),
                              db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return palace_service.batch_import_files(db, current_user, archive)

@router.put("/palace/files/{file_id}/content")
def update_palace_file_content(file_id: str, body: PalaceContentUpdate,
                               db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return palace_service.update_file_content(db, current_user.id, file_id, body.content)

@router.post("/palace/files/{file_id}/replace")
def replace_palace_file(file_id: str, file: UploadFile = File(...),
                        db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return palace_service.replace_file(db, current_user.id, file_id, file)

@router.get("/palace/files/{file_id}/preview")
def preview_palace_file(file_id: str, max_chars: int = Query(60000, le=200000),
                        db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return palace_service.preview_file(db, current_user.id, file_id, max_chars)

@router.get("/palace/files/{file_id}/raw")
def raw_palace_file(file_id: str, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """原始字节内联读取（中栏图片预览等）；属主校验在 service 层。"""
    path, media_type, filename = palace_service.raw_file(db, current_user.id, file_id)
    return FileResponse(path, media_type=media_type, filename=filename, content_disposition_type="inline")

@router.delete("/palace/files/{file_id}", status_code=204)
def delete_palace_file(file_id: str, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    palace_service.delete_palace_file(db, current_user.id, file_id)

@router.post("/palace/files/{file_id}/rebuild", status_code=202)
def rebuild_palace_file(file_id: str, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    return palace_service.rebuild_file(db, current_user.id, file_id)

@router.get("/palace/folders")
def list_palace_folders(db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    return palace_service.list_folders(db, current_user.id)

@router.post("/palace/folders", status_code=201)
def create_palace_folder(body: PalaceFolderCreate, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    return palace_service.create_folder(db, current_user.id, body.path)

@router.post("/palace/files/notes", status_code=201)
def create_palace_note(body: PalaceNoteCreate, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    return palace_service.create_note(db, current_user, body)

@router.patch("/palace/files/{file_id}")
def move_palace_file(file_id: str, body: PalaceFileMove, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    return palace_service.move_file(db, current_user.id, file_id, body.folder_path)

@router.patch("/palace/folders/{folder_id}")
def rename_palace_folder(folder_id: str, body: PalaceFolderRename, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    return palace_service.rename_folder(db, current_user.id, folder_id, body.path)

@router.delete("/palace/folders/{folder_id}", status_code=204)
def delete_palace_folder(folder_id: str, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    palace_service.delete_folder(db, current_user.id, folder_id)

@router.get("/palace/graph")
def palace_graph_view(db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    return palace_service.graph_overview(db, current_user.id)

@router.get("/palace/graph/search")
def search_palace_graph(q: str = Query(...), db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    return palace_service.search_graph(db, current_user.id, q)

@router.post("/palace/graph/consolidate")
def consolidate_palace_graph(db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    return palace_consolidate.run_consolidation(db, current_user.id)


@router.post("/tool-runs/{tool_run_id}/decision")
def decide_tool_run(
    tool_run_id: str,
    body: ApprovalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _conversation_service.decide_tool_run(
        tool_run_id,
        body,
        db,
        current_user,
    )


@router.get("/skills", response_model=list[SkillOut])
def list_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.list_skills(db, current_user)


@router.post("/skills", response_model=SkillOut, status_code=201)
def create_skill(
    body: SkillCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.create_skill(body, db, current_user)


@router.post(
    "/skills/import",
    response_model=SkillOut,
    status_code=201,
)
async def import_skill(
    archive: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _skill_service.import_skill(
        archive,
        db,
        current_user,
    )


@router.patch("/skills/{skill_id}", response_model=SkillOut)
def update_skill(
    skill_id: str,
    body: SkillUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.update_skill(
        skill_id,
        body,
        db,
        current_user,
        skill_lookup_fn=_skill,
    )


@router.delete("/skills/{skill_id}", status_code=204)
def remove_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.remove_skill(
        skill_id,
        db,
        current_user,
        skill_lookup_fn=_skill,
        storage_error_fn=_storage_error,
    )


@router.get("/skills/{skill_id}/files")
def list_skill_files(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.list_skill_files(
        skill_id,
        db,
        current_user,
        skill_lookup_fn=_skill,
        storage_error_fn=_storage_error,
    )


@router.get("/skills/{skill_id}/files/{file_path:path}")
def get_skill_file(
    skill_id: str,
    file_path: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.get_skill_file(
        skill_id,
        file_path,
        db,
        current_user,
        skill_lookup_fn=_skill,
        storage_error_fn=_storage_error,
    )


@router.put("/skills/{skill_id}/files/{file_path:path}")
def put_skill_file(
    skill_id: str,
    file_path: str,
    body: SkillFileContent,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.put_skill_file(
        skill_id,
        file_path,
        body,
        db,
        current_user,
        skill_lookup_fn=_skill,
        storage_error_fn=_storage_error,
    )


@router.delete(
    "/skills/{skill_id}/files/{file_path:path}",
    status_code=204,
)
def remove_skill_file(
    skill_id: str,
    file_path: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.remove_skill_file(
        skill_id,
        file_path,
        db,
        current_user,
        skill_lookup_fn=_skill,
        storage_error_fn=_storage_error,
    )


@router.get("/skills/{skill_id}/export")
def export_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _skill_service.export_skill(
        skill_id,
        db,
        current_user,
        skill_lookup_fn=_skill,
        storage_error_fn=_storage_error,
    )


@router.get("/mcp-servers", response_model=list[McpServerOut])
def list_mcp_servers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return mcp_server_service.list_mcp_servers(
        db,
        current_user.id,
        include_builtins=True,
    )


@router.post(
    "/mcp-servers",
    response_model=McpServerOut,
    status_code=201,
)
def create_mcp_server(
    body: McpServerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return mcp_server_service.create_mcp_server(
            db,
            current_user.id,
            body,
        )
    except mcp_server_service.McpServerServiceError as exc:
        raise _mcp_http_error(exc) from exc


@router.patch(
    "/mcp-servers/{server_id}",
    response_model=McpServerOut,
)
def update_mcp_server(
    server_id: str,
    body: McpServerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return mcp_server_service.update_mcp_server(
            db,
            current_user.id,
            server_id,
            body,
            include_builtins=True,
        )
    except mcp_server_service.McpServerServiceError as exc:
        raise _mcp_http_error(exc) from exc


@router.delete("/mcp-servers/{server_id}", status_code=204)
def remove_mcp_server(
    server_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        mcp_server_service.remove_mcp_server(
            db,
            current_user.id,
            server_id,
            include_builtins=True,
        )
    except mcp_server_service.McpServerServiceError as exc:
        raise _mcp_http_error(exc) from exc
    return Response(status_code=204)


@router.post(
    "/mcp-servers/{server_id}/test",
    response_model=McpTestOut,
)
async def test_mcp_server(
    server_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return await mcp_server_service.test_mcp_server(
            db,
            current_user.id,
            server_id,
            include_builtins=True,
        )
    except mcp_server_service.McpServerServiceError as exc:
        raise _mcp_http_error(exc) from exc


@router.post(
    "/mcp-servers/platform-minio",
    response_model=McpServerOut,
)
def install_platform_minio_mcp(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return mcp_server_service.install_platform_minio_mcp(
            db,
            current_user.id,
            workspace_minio_service_factory=get_workspace_minio_service,
            minio_tool_manifest_fn=minio_tool_manifest,
        )
    except mcp_server_service.McpServerServiceError as exc:
        raise _mcp_http_error(exc) from exc


@router.get("/memories", response_model=list[MemoryOut])
def list_memories(
    zone: str | None = None,
    include_superseded: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return memory_service.list_memories(
        db,
        current_user.id,
        zone=zone,
        include_superseded=include_superseded,
    )


@router.post("/memories", response_model=MemoryOut, status_code=201)
def create_memory(
    body: MemoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return memory_service.create_memory(
            db,
            current_user.id,
            body.content,
            zone=body.zone,
            pinned=body.pinned,
            tags=body.tags,
            confidence="high",
            source="user",
        )
    except memory_service.MemoryConflictError as exc:
        return _memory_conflict_response(exc)


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
def update_memory(
    memory_id: str,
    body: MemoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        memory = memory_service.update_memory(
            db,
            current_user.id,
            memory_id,
            content=body.content,
            zone=body.zone,
            pinned=body.pinned,
            tags=body.tags,
        )
    except memory_service.MemoryConflictError as exc:
        return _memory_conflict_response(exc)
    if memory is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return memory


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(
    memory_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted = memory_service.delete_memory(
        db,
        current_user.id,
        memory_id,
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return Response(status_code=204)


@router.get("/memories/distill-report", response_model=MemoryDistillReport)
def get_memory_distill_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {
        "clusters": memory_service.find_distill_clusters(
            db,
            current_user.id,
        )
    }


@router.post("/memories/distill", response_model=MemoryOut, status_code=201)
def distill_memories(
    body: MemoryDistillRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return memory_service.apply_distill(
            db,
            current_user.id,
            body.member_ids,
            merged_content=body.merged_content,
            use_llm=body.use_llm,
        )
    except memory_service.MemoryDistillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except memory_service.MemoryDistillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _reflection_value_error(exc: ValueError) -> HTTPException:
    """reflection_service 的 ValueError 消息契约 → HTTP 状态码。"""
    message = str(exc)
    if message in {"候选不存在", "会话不存在"}:
        status_code = 404
    elif message in {"候选已处理", "同名 Skill 已存在"}:
        status_code = 409
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=message)


@router.get(
    "/reflection/candidates",
    response_model=list[ReflectionCandidateOut],
)
def list_reflection_candidates(
    status: Literal["pending", "accepted", "rejected", "all"] = "pending",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return reflection_service.list_candidates(
        db,
        current_user.id,
        status=status,
    )


@router.post(
    "/reflection/candidates/{candidate_id}/decision",
    response_model=ReflectionCandidateOut,
)
def decide_reflection_candidate(
    candidate_id: str,
    body: ReflectionDecisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return reflection_service.decide_candidate(
            db,
            current_user.id,
            candidate_id,
            body.decision,
            edited_payload=body.payload,
        )
    except memory_service.MemoryConflictError as exc:
        return _memory_conflict_response(exc)
    except ValueError as exc:
        raise _reflection_value_error(exc) from exc


@router.post(
    "/reflection/full",
    response_model=ReflectionFullAccepted,
    status_code=202,
)
def request_full_reflection(
    body: ReflectionFullRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return reflection_service.request_full_reflection(
            db,
            current_user.id,
            body.conversation_id,
        )
    except ValueError as exc:
        raise _reflection_value_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/reflection/settings",
    response_model=ReflectionSettingsOut,
)
def get_reflection_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return reflection_service.get_reflection_settings(db, current_user.id)


@router.put(
    "/reflection/settings",
    response_model=ReflectionSettingsOut,
)
def update_reflection_settings(
    body: ReflectionSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return reflection_service.update_reflection_settings(
        db,
        current_user.id,
        body.auto_accept_enabled,
    )
