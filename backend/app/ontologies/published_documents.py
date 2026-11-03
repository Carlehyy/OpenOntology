"""本体发布态业务文档：聚合读。

「业务文档」不是独立文档表：它是当前发布版本 ``snapshot_semantic.documentMd``
（Markdown，DB JSON 列），权威指针为 ``OntologyProject.current_release_id``。
本模块只提供发布态业务文档的聚合读；发布/回滚/修订成功后由调用方失效
聚合缓存。
"""
from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion

# 语义层 documentMd 的保存上限（UTF-8 字节）。按字节而非字符计数：CJK
# 文本每字符 3 字节。
_MAX_DOCUMENT_MD_BYTES = 700_000


def _semantic_document(semantic) -> tuple[str, str, str] | None:
    """从语义层提取 (documentMd, title, fingerprint)；无业务文档返回 None。"""
    if not isinstance(semantic, dict):
        return None
    document_md = str(semantic.get("documentMd") or "").strip()
    if not document_md:
        return None
    title = str(semantic.get("documentTitle") or "").strip()
    fingerprint = str(semantic.get("documentFingerprint") or "").strip()
    if not fingerprint:
        fingerprint = hashlib.sha256(document_md.encode("utf-8")).hexdigest()
    return document_md, title, fingerprint


def _published_rows(db: Session) -> list[tuple[OntologyProject, OntologyVersion]]:
    """全部 (project, 当前发布版本) 对：只认发布指针，不推断最大版本号。"""
    return (
        db.query(OntologyProject, OntologyVersion)
        .join(
            OntologyVersion,
            OntologyVersion.id == OntologyProject.current_release_id,
        )
        .filter(
            OntologyVersion.node_kind == "release",
            OntologyVersion.lifecycle_status == "released",
        )
        .order_by(OntologyProject.created_at.desc())
        .all()
    )


def list_published_documents(db: Session) -> list[dict]:
    """各本体最新发布态业务文档摘要（不含正文）。"""
    items: list[dict] = []
    for project, release in _published_rows(db):
        extracted = _semantic_document(release.snapshot_semantic)
        if extracted is None:
            continue
        document_md, title, fingerprint = extracted
        items.append({
            "ontologyId": project.id,
            "ontologyName": project.name,
            "versionId": release.id,
            "versionNumber": str(release.version_number or ""),
            "title": title or f"{project.name} 业务文档",
            "fingerprint": fingerprint,
            "documentChars": len(document_md),
            "publishedAt": (
                release.published_at.isoformat()
                if release.published_at is not None else None
            ),
        })
    return items
