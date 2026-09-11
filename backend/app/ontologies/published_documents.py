"""本体发布态业务文档：聚合读 + 变更广播 + 每日对账。

「业务文档」不是独立文档表：它是当前发布版本 ``snapshot_semantic.documentMd``
（Markdown，DB JSON 列），权威指针为 ``OntologyProject.current_release_id``。
改变它的时机只有三个：promote（草稿晋级发布）、rollback（激活历史版本）、
探索落地新建本体（冻结带语义层的 v0）——本模块在这些成功路径上派发自包含
NATS 事件（内容随消息走，消费方 super_assistant 宫殿不得反向依赖本体域），
消费侧按 (ontology_id, fingerprint) 幂等；每日对账扫描重放全部事件，兜底
派发失败/漏埋点导致的漂移（指纹一致时消费端直接 no-op）。

广播是尽力而为：任何失败只记日志，绝不影响发布/回滚主链路的可用性。
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.data_channel.pipeline_tasks.dispatch import dispatch_ontology_document_published
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion

logger = logging.getLogger(__name__)

# JetStream 默认单消息上限 1MB（含 NATS 头部）；语义层 documentMd 的
# UTF-8 字节数超过该量级时放弃事件派发（业务文档为 Markdown 文本，正常
# 远低于此），对账扫描同样跳过。按字节而非字符计数：CJK 文本每字符 3 字节。
_MAX_DOCUMENT_MD_BYTES = 700_000


def _document_md_too_large(document_md: str) -> bool:
    return len(document_md.encode("utf-8")) > _MAX_DOCUMENT_MD_BYTES


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


def document_payload(project_name: str, release: OntologyVersion) -> dict | None:
    """发布版本 → 自包含事件 payload；该版本无业务文档时返回 None。"""
    extracted = _semantic_document(release.snapshot_semantic)
    if extracted is None:
        return None
    document_md, title, fingerprint = extracted
    return {
        "ontology_id": release.ontology_id,
        "ontology_name": str(project_name or ""),
        "version_id": release.id,
        "version_number": str(release.version_number or ""),
        "title": title or f"{project_name} 业务文档",
        "document_md": document_md,
        "fingerprint": fingerprint,
        "published_at": (
            release.published_at.isoformat()
            if release.published_at is not None else None
        ),
    }


def notify_published_document(project: OntologyProject, release: OntologyVersion) -> None:
    """发布/回滚/探索落地成功后调用：失效聚合缓存 + 尽力派发图谱事件。

    必须在事务提交之后调用（消费侧虽不回读本体表，但事件语义是「该
    版本已是当前发布态」）；任何异常就地消化，漏派由每日对账兜底。
    """
    try:
        from app.ontologies import cache as ontology_cache

        ontology_cache.invalidate_published_documents()
        payload = document_payload(project.name, release)
        if payload is None:
            return
        if _document_md_too_large(payload["document_md"]):
            logger.warning(
                "发布态业务文档超限，跳过知识图谱事件派发（ontology=%s，%d 字节）",
                payload["ontology_id"], len(payload["document_md"].encode("utf-8")),
            )
            return
        dispatch_ontology_document_published(payload)
        logger.info(
            "发布态业务文档图谱事件已派发（ontology=%s，version=%s）",
            payload["ontology_id"], payload["version_number"],
        )
    except Exception:  # noqa: BLE001 — 广播失败不影响发布主链路
        logger.exception(
            "发布态业务文档图谱事件派发失败（ontology=%s）",
            getattr(release, "ontology_id", "?"),
        )


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
    """各本体最新发布态业务文档摘要（不含正文，正文随事件/预览端点走）。"""
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


# ---------------------------------------------------------------------------
# 每日对账（APScheduler 进程内定时 + NATS JetStream，palace_consolidate 同模式）
# ---------------------------------------------------------------------------

_scheduler = None
_JOB_ID = "ontology-published-docs-reconcile-dispatch"


def _dispatch_reconciliation() -> None:
    """重放全部发布态业务文档事件：消费侧指纹一致即 no-op，漂移时自愈。"""
    from app.shared.database import SessionLocal

    payloads: list[dict] = []
    db = SessionLocal()
    try:
        for project, release in _published_rows(db):
            payload = document_payload(project.name, release)
            if payload is not None and not _document_md_too_large(payload["document_md"]):
                payloads.append(payload)
    finally:
        db.close()
    if not payloads:
        return
    for payload in payloads:
        try:
            dispatch_ontology_document_published(payload)
        except Exception:  # noqa: BLE001 — 单条派发失败不影响其余
            logger.exception(
                "发布态业务文档对账派发失败（ontology=%s）", payload.get("ontology_id"),
            )
    logger.info("发布态业务文档对账完成（重放 %d 条事件）", len(payloads))


def start() -> None:
    """启动每日 04:00 的对账定时器；settings 开关关闭时为 no-op。"""
    global _scheduler
    if not getattr(settings, "ontology_published_documents_reconcile_enabled", True):
        return
    if _scheduler is not None and _scheduler.running:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _dispatch_reconciliation, "cron", hour=4, minute=0,
        id=_JOB_ID, max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("本体发布文档对账定时器已启动（每天 04:00，本地时区）")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
