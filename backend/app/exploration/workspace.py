"""会话隔离文件空间。

所有外部调用只持有文件 ID / 逻辑路径；物理路径由服务端生成并固定在
``uploads/exploration/<session-id>`` 下。这样既给探索 Agent 完整的会话内
读写能力，也不把宿主机路径暴露给模型或浏览器。
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import uuid
from pathlib import PurePosixPath

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.exploration.models import ExplorationAttachment, ExplorationSession
from app.services.document_service import convert_document

TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".xml", ".yaml", ".yml", ".mmd", ".mermaid"}
STORED_TEXT_CAP = 200_000
READ_TEXT_CAP = 500_000
MAX_FILES_PER_SESSION = 100
MAX_LOGICAL_PATH = 480


def normalize_logical_path(raw: str) -> str:
    """返回安全的 POSIX 逻辑路径；拒绝绝对路径、空段与 ``..``。"""
    value = str(raw or "").replace("\\", "/").strip()
    if not value or len(value) > MAX_LOGICAL_PATH or "\x00" in value:
        raise ValueError("文件路径为空或过长")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("文件路径不合法：仅允许会话空间内的相对路径")
    clean_parts = [re.sub(r"[\x00-\x1f]", "", part).strip() for part in path.parts]
    if any(not part for part in clean_parts):
        raise ValueError("文件路径包含空名称")
    return "/".join(clean_parts)


def is_text_editable(path: str, mime_type: str | None = None) -> bool:
    ext = PurePosixPath(path).suffix.lower()
    return ext in TEXT_EXTENSIONS or bool(mime_type and mime_type.startswith("text/"))


def physical_path(session_id: str, file_id: str, logical_path: str) -> str:
    ext = PurePosixPath(logical_path).suffix.lower()
    safe_ext = ext if re.fullmatch(r"\.[a-z0-9]{1,12}", ext) else ""
    root = os.path.abspath(os.path.join(settings.uploads_dir, "exploration", session_id))
    os.makedirs(root, exist_ok=True)
    candidate = os.path.abspath(os.path.join(root, f"{file_id}{safe_ext}"))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError("文件物理路径越界")
    return candidate


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: str, content: bytes) -> None:
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def require_file(db: Session, session_id: str, file_ref: str) -> ExplorationAttachment:
    """Resolve a session file by opaque ID, with logical-path compatibility.

    HTTP clients normally use the opaque ID.  Tool-calling models sometimes copy
    the path returned by ``list`` into ``file_id``; accepting an exact logical
    path keeps that harmless mistake inside the same session boundary instead of
    turning it into a failed tool loop.  The ID lookup remains authoritative.
    """
    query = db.query(ExplorationAttachment).filter(
        ExplorationAttachment.session_id == session_id)
    row = query.filter(ExplorationAttachment.id == file_ref).first()
    if not row:
        row = query.filter(ExplorationAttachment.relative_path == file_ref).first()
    if not row:
        raise HTTPException(404, "会话文件不存在")
    return row


def _ensure_capacity(db: Session, session_id: str) -> None:
    count = db.query(ExplorationAttachment).filter(
        ExplorationAttachment.session_id == session_id).count()
    if count >= MAX_FILES_PER_SESSION:
        raise HTTPException(422, f"本会话文件已达上限（{MAX_FILES_PER_SESSION} 个）")


def _ensure_unique_path(db: Session, session_id: str, logical_path: str,
                        exclude_id: str | None = None) -> None:
    q = db.query(ExplorationAttachment).filter(
        ExplorationAttachment.session_id == session_id,
        ExplorationAttachment.relative_path == logical_path)
    if exclude_id:
        q = q.filter(ExplorationAttachment.id != exclude_id)
    if q.first():
        raise HTTPException(409, f"会话空间中已存在「{logical_path}」")


def create_bytes(db: Session, session: ExplorationSession, logical_path: str,
                 content: bytes, mime_type: str | None = None,
                 source: str = "upload") -> ExplorationAttachment:
    _ensure_capacity(db, session.id)
    try:
        logical_path = normalize_logical_path(logical_path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _ensure_unique_path(db, session.id, logical_path)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"文件超过大小限制 {settings.max_upload_mb}MB")

    file_id = str(uuid.uuid4())
    mime = mime_type or mimetypes.guess_type(logical_path)[0] or "application/octet-stream"
    path = physical_path(session.id, file_id, logical_path)
    _atomic_write(path, content)

    editable = is_text_editable(logical_path, mime)
    conversion = convert_document(path, mime)
    extracted = conversion.content or ""
    row = ExplorationAttachment(
        id=file_id, session_id=session.id,
        filename=PurePosixPath(logical_path).name, relative_path=logical_path,
        mime_type=mime, file_size=len(content), file_path=path,
        sha256=_digest(content), version=1, source=source, editable=editable,
        extracted_text=extracted[:STORED_TEXT_CAP], char_count=len(extracted),
        status="ready" if conversion.ok or editable else "failed",
        error=None if conversion.ok or editable else (conversion.error or "文件无法读取"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_text(db: Session, session: ExplorationSession, logical_path: str,
                content: str, mime_type: str | None = None,
                source: str = "agent") -> ExplorationAttachment:
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(422, "文本必须是 UTF-8 可编码内容") from exc
    mime = mime_type or mimetypes.guess_type(logical_path)[0] or "text/plain"
    if not is_text_editable(logical_path, mime):
        raise HTTPException(422, "创建接口仅支持文本类文件（md/txt/csv/json/xml/yaml/mermaid）")
    return create_bytes(db, session, logical_path, encoded, mime, source)


def read_text(row: ExplorationAttachment) -> str:
    if not row.editable:
        raise HTTPException(415, "该文件不是可直接编辑的文本文件")
    if not row.file_path or not os.path.isfile(row.file_path):
        raise HTTPException(410, "文件内容已丢失")
    if row.file_size > READ_TEXT_CAP:
        raise HTTPException(413, f"文本文件超过在线读取上限 {READ_TEXT_CAP} 字节，请下载后查看")
    with open(row.file_path, "r", encoding="utf-8", errors="strict") as handle:
        return handle.read()


def update_text(db: Session, row: ExplorationAttachment, content: str,
                expected_version: int | None = None, source: str = "agent") -> ExplorationAttachment:
    """Update bytes while preserving the file's immutable ownership source.

    ``source`` identifies the current editor. Ownership is monotonic: an
    explicit user save may promote an agent draft to user-owned evidence, while
    an agent edit can never demote an uploaded/user-owned file into an
    agent-owned file. Mutation authorization relies on this persisted boundary.
    """
    if expected_version is not None and row.version != expected_version:
        raise HTTPException(409, detail={
            "code": "workspace_version_conflict",
            "message": f"文件版本已从 {expected_version} 变为 {row.version}，请重新读取后再保存",
            "currentVersion": row.version,
        })
    if not row.editable:
        raise HTTPException(415, "该文件不是可直接编辑的文本文件")
    encoded = content.encode("utf-8")
    if len(encoded) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"文件超过大小限制 {settings.max_upload_mb}MB")
    if not row.file_path:
        raise HTTPException(410, "文件物理路径不存在")
    _atomic_write(row.file_path, encoded)
    row.file_size = len(encoded)
    row.sha256 = _digest(encoded)
    row.version = (row.version or 0) + 1
    if source == "user" and row.source == "agent":
        row.source = "user"
    row.extracted_text = content[:STORED_TEXT_CAP]
    row.char_count = len(content)
    row.summary = None  # 正文已变，索引卡下回合懒生成重建
    row.status = "ready"
    row.error = None
    db.commit()
    db.refresh(row)
    return row


def refresh_binary_metadata(db: Session, row: ExplorationAttachment,
                            source: str = "agent") -> ExplorationAttachment:
    """Refresh an edited binary while preserving its immutable ownership source.

    ``source`` identifies the current editor for API compatibility only. Office
    agent edits never demote the attachment's persisted ownership boundary.
    """
    if not row.file_path or not os.path.isfile(row.file_path):
        raise HTTPException(410, "文件内容已丢失")
    with open(row.file_path, "rb") as handle:
        content = handle.read()
    conversion = convert_document(row.file_path, row.mime_type)
    extracted = conversion.content or ""
    row.file_size = len(content)
    row.sha256 = _digest(content)
    row.version = (row.version or 0) + 1
    row.extracted_text = extracted[:STORED_TEXT_CAP]
    row.char_count = len(extracted)
    row.summary = None  # 正文已变，索引卡下回合懒生成重建
    row.status = "ready" if conversion.ok else "failed"
    row.error = None if conversion.ok else conversion.error
    db.commit()
    db.refresh(row)
    return row


def delete_file(db: Session, row: ExplorationAttachment) -> None:
    if row.file_path and os.path.exists(row.file_path):
        try:
            os.remove(row.file_path)
        except OSError as exc:
            raise HTTPException(500, "文件删除失败，请稍后重试") from exc
    db.delete(row)
    db.commit()
