"""业务探索 API Schemas — 对外 camelCase，复用 formal 的 CamelModel 约定。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import Field

from app.ontologies.formal_modeling.schemas import CamelModel


class SessionCreate(CamelModel):
    title: Optional[str] = None
    # 可选绑定本体版本锚点（版本业务语义层挂载点）；绑定时按版本引导初始画布
    ontology_id: Optional[str] = None
    ontology_version_id: Optional[str] = None


class SessionOut(CamelModel):
    id: str
    title: str
    canvas_version: int = 0
    status: str = "active"
    ontology_id: Optional[str] = None
    ontology_version_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ChatRequest(CamelModel):
    message: str
    model_id: Optional[str] = None
    web_search: bool = False
    stream: bool = True


class MessageOut(CamelModel):
    id: str
    role: str
    content: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    model: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    created_at: datetime


class GenerateDocumentRequest(CamelModel):
    model_id: Optional[str] = None


class DocumentOut(CamelModel):
    id: str
    session_id: str
    title: str
    content_md: str
    version: int
    source_canvas_version: Optional[int] = None
    source_canvas_fingerprint: str
    current_canvas_version: int
    current_canvas_fingerprint: str
    is_stale: bool
    created_at: datetime


class DocumentListItem(CamelModel):
    id: str
    session_id: str
    title: str
    version: int
    source_canvas_version: Optional[int] = None
    source_canvas_fingerprint: str
    current_canvas_version: int
    current_canvas_fingerprint: str
    is_stale: bool
    created_at: datetime


class GenerateDraftRequest(CamelModel):
    target_ontology_id: Optional[str] = None   # 缺省取会话绑定本体；会话未绑定 = 应用时新建本体
    model_id: Optional[str] = None             # LLM 补缺用的模型；缺省用系统默认
    force: bool = False                        # 质量门未通过时显式越权（留痕于草稿报告）


class DraftOut(CamelModel):
    id: str
    session_id: str
    document_id: str
    target_ontology_id: Optional[str] = None
    draft: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any] = Field(default_factory=dict)
    status: str = "draft"
    applied_ontology_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AttachmentOut(CamelModel):
    id: str
    session_id: str
    filename: str
    relative_path: str = ""
    mime_type: Optional[str] = None
    file_size: int = 0
    char_count: int = 0
    sha256: Optional[str] = None
    version: int = 1
    source: str = "upload"
    editable: bool = False
    status: str = "ready"
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class WorkspaceTextCreate(CamelModel):
    path: str
    content: str = ""
    mime_type: Optional[str] = None


class WorkspaceTextUpdate(CamelModel):
    content: str
    expected_version: Optional[int] = None


class WorkspaceTextOut(CamelModel):
    id: str
    relative_path: str
    content: str
    version: int
    sha256: Optional[str] = None


class WorkspacePreviewOut(CamelModel):
    id: str
    relative_path: str
    content: str
    version: int
    mime_type: Optional[str] = None
    editable: bool = False
    truncated: bool = False


class NewOntologySpec(CamelModel):
    name: str
    domain: Optional[str] = None
    description: Optional[str] = None


class ApplyDraftRequest(CamelModel):
    # None = 全部应用；[] = 一个都不选（校验拒绝）
    selected_keys: Optional[list[str]] = None
    new_ontology: Optional[NewOntologySpec] = None


class DraftValidationRequest(CamelModel):
    selected_keys: Optional[list[str]] = None


# ---------------------------------------------------------------- 本体预览投影（只读）


class OntologyPreviewItem(CamelModel):
    key: str
    name: str
    display_name: str
    # add=将新增 / exists=已存在将跳过 / conflict=同名冲突不进默认选择集
    disposition: str


class OntologyPreviewReadiness(CamelModel):
    ready: bool
    gates_passed: int
    gates_total: int
    blocking_count: int
    advisory_count: int


class OntologyPreviewOut(CamelModel):
    """画布 → 本体预览投影：确定性转换 + 与绑定版本基线比对，不落库。"""
    canvas_fingerprint: str
    canvas_version: int = 0
    bound: bool = False
    ontology_id: Optional[str] = None
    ontology_version_id: Optional[str] = None
    readiness: OntologyPreviewReadiness
    projected: dict[str, list[OntologyPreviewItem]] = Field(default_factory=dict)
    semantic_issues: list[dict[str, Any]] = Field(default_factory=list)
