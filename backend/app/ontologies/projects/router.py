from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from typing import Optional
from app.config import settings
from app.deps import get_db, get_current_user
from app.models.ontology import OntologyProject
from app.models.user import User
from app.models.ontology_version import OntologyVersion
from app.ontologies.release_context import create_initial_release
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot,
)
from app.ontologies.versions.release_service import resolve_current_release
from app.ontologies.access import require_ontology_access
from app.ontologies import cache as ontology_cache
from app.ontologies.network import cache as network_cache
from app.ontologies.export.schemas import OntologyStructurePackage
from app.ontologies.export.service import import_structure_package
from app.ontologies.projection_state import mark_failed, mark_projecting
from app.ontologies.runtime_fence import _ontology_build_lock
from app.schemas.ontology import OntologyCreate, OntologyOut, OntologyListItem, OntologyUpdate
from app.settings.domains.service import (
    LEGACY_ONTOLOGY_DESCRIPTION,
    ensure_domain,
    find_domain,
)
import uuid

router = APIRouter()

# Historical installations can contain ontologies created before configurable
# domains existed. Keep those names valid while accepting every domain managed
# through System Settings.
LEGACY_DOMAINS = {
    "供应链", "采购", "财务", "医疗", "金融", "法律", "教育", "科技",
    "制造", "能源", "其他",
}


def _validate_domain(db: Session, domain: str, *, current_user: User) -> None:
    exists = find_domain(db, domain, for_update=True)
    if exists is not None:
        return
    if domain in LEGACY_DOMAINS:
        ensure_domain(
            db,
            name=domain,
            description=LEGACY_ONTOLOGY_DESCRIPTION,
            created_by=current_user.id,
        )
        return
    raise HTTPException(422, detail={
        "error": "INVALID_DOMAIN",
        "message": f"领域「{domain}」不存在，请先在系统设置中添加",
    })


def _release_map(db: Session, projects: list[OntologyProject]) -> dict[str, OntologyVersion]:
    ids = {item.current_release_id for item in projects if item.current_release_id}
    if not ids:
        return {}
    return {item.id: item for item in db.query(OntologyVersion).filter(
        OntologyVersion.id.in_(ids),
        OntologyVersion.node_kind == "release",
        OntologyVersion.lifecycle_status == "released",
    ).all()}


def _resolved_release_map(
    db: Session,
    projects: list[OntologyProject],
) -> dict[str, OntologyVersion]:
    """Resolve every project's current immutable release, repairing legacy rows.

    Ontologies created before the version-tree migration may have definitions
    but no release row/pointer.  The version service already knows how to
    freeze that complete projection into a v0 migration baseline; resolve it
    here as well so every list consumer (especially the assistant) observes
    the same invariant without first opening the version tree.
    """
    releases = _release_map(db, projects)
    repaired = False
    for project in projects:
        if releases.get(project.current_release_id) is not None:
            continue
        locked_project = db.query(OntologyProject).filter(
            OntologyProject.id == project.id,
        ).with_for_update().populate_existing().one()
        locked_release = _release_map(db, [locked_project]).get(
            locked_project.current_release_id)
        if locked_release is not None:
            releases[locked_release.id] = locked_release
            continue
        release = resolve_current_release(db, locked_project)
        releases[release.id] = release
        repaired = True
    if repaired:
        db.commit()
    return releases


def _project_payload(project: OntologyProject, schema, release: OntologyVersion | None = None) -> dict:
    """对外版本始终来自发布指针，避免列表、详情和版本树显示不一致。"""
    data = schema.model_validate(project).model_dump()
    data["current_release_id"] = release.id if release else project.current_release_id
    data["current_release_version"] = release.version_number if release else None
    if release is not None:
        data["version"] = release.version_number
    return data


def _release_structure_counts(release: OntologyVersion | None) -> dict[str, int]:
    """Return card metrics from the immutable current release snapshot."""
    snapshot = complete_snapshot(release.snapshot_formal if release else None)
    return {
        "entity_count": len(snapshot["objectTypes"]),
        "relation_count": len(snapshot["linkTypes"]),
        "action_count": len(snapshot["actions"]),
        "sentinel_count": len(snapshot["sentinels"]),
    }


def _cached_release_counts(release: OntologyVersion | None) -> dict[str, int]:
    """卡片指标只依赖不可变发布快照：按 release 分键长 TTL 缓存。"""
    if release is None:
        return _release_structure_counts(release)
    return ontology_cache.list_cached_call(
        ontology_cache.release_counts_cache_key(release.id),
        ontology_cache.RELEASE_COUNTS_TTL_SECONDS,
        lambda: _release_structure_counts(release),
    )


@router.get("")
def list_ontologies(
    name: Optional[str] = None,
    domain: Optional[str] = None,
    page: int = 1, page_size: int = 20,
    db: Session = Depends(get_db), _=Depends(get_current_user)
):
    # 列表页高频入口：响应整体短 TTL 缓存 + 写路径 bump 版本换键；
    # 卡片指标来自不可变发布快照，按 release 分键长 TTL（fail-open）。
    def build():
        q = db.query(OntologyProject)
        if name:
            q = q.filter(OntologyProject.name.ilike(f"%{name}%"))
        if domain:
            q = q.filter(OntologyProject.domain == domain)
        total = q.count()
        items = q.order_by(OntologyProject.created_at.desc()).offset((page-1)*page_size).limit(page_size).all()
        releases = _resolved_release_map(db, items)
        result = []
        for item in items:
            release = releases.get(item.current_release_id)
            d = _project_payload(item, OntologyListItem, release)
            d.update(_cached_release_counts(release))
            result.append(d)
        # jsonable_encoder 归一 datetime，保证缓存命中与直查返回逐字节一致。
        return jsonable_encoder(
            {"data": {"items": result, "total": total, "page": page, "page_size": page_size}}
        )
    return ontology_cache.list_cached_call(
        ontology_cache.list_cache_key(name, domain, page, page_size),
        settings.ontology_list_cache_ttl_seconds,
        build,
    )

@router.post("", status_code=201)
def create_ontology(body: OntologyCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if getattr(current_user, "role", "") not in ("admin", "editor"):
        raise HTTPException(403, "Viewer role is read-only")
    _validate_domain(db, body.domain, current_user=current_user)
    existing = db.query(OntologyProject).filter(OntologyProject.name.ilike(body.name)).first()
    if existing:
        raise HTTPException(status_code=409, detail={"error": "DUPLICATE_NAME", "message": f"Ontology 名称「{body.name}」已存在", "existing_id": existing.id})
    project = OntologyProject(id=str(uuid.uuid4()), name=body.name, domain=body.domain,
                               description=body.description, icon=body.icon,
                               # Internal compatibility value only; creation no
                               # longer branches into LLM/Pipeline workflows.
                               build_mode=body.build_mode or "manual", version="v0",
                               created_by=current_user.id)
    db.add(project)
    db.flush()
    # v0 是不可变的完整空结构发布基线。project.status 暂时保留为 draft 仅兼容
    # 旧编辑接口；新版本工作流只认 version.node_kind/current_release_id。
    root = create_initial_release(
        db,
        project,
        snapshot=None,
        created_by=current_user.id,
        description="系统创建的完整空结构基线",
    )
    db.commit(); db.refresh(project)
    network_cache.invalidate_network()
    ontology_cache.invalidate_version_tree()
    ontology_cache.invalidate_list()
    return {"data": _project_payload(project, OntologyOut, root)}


@router.post("/import", status_code=201)
def import_ontology_structure(
    body: OntologyStructurePackage,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a published v0 ontology from a local structure JSON package."""
    if getattr(current_user, "role", "") not in ("admin", "editor"):
        raise HTTPException(403, "Viewer role is read-only")
    result = import_structure_package(db, body, current_user=current_user)
    network_cache.invalidate_network()
    ontology_cache.invalidate_version_tree()
    ontology_cache.invalidate_list()
    return {"data": result}

@router.get("/published-documents")
def list_published_documents(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """各本体最新发布态业务文档摘要（超级助手知识图谱「本体文档」目录数据源）。

    只认 current_release_id 指针，语义层无 documentMd 的本体不出现；摘要不含
    正文。读多写少（弹窗打开时拉取），整体短 TTL 缓存 + 发布/回滚/落地路径
    bump 版本换键（fail-open）。
    """
    from app.ontologies import published_documents

    return ontology_cache.list_cached_call(
        ontology_cache.published_documents_cache_key(),
        settings.ontology_list_cache_ttl_seconds,
        lambda: {"data": {"items": published_documents.list_published_documents(db)}},
    )


@router.get("/{ontology_id}")
def get_ontology(ontology_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    # 详情页高频入口：短 TTL 只读缓存，写操作 bump 版本即整体失效（fail-open）。
    return ontology_cache.cached_call(
        ontology_cache.detail_cache_key(ontology_id),
        settings.ontology_detail_cache_ttl_seconds,
        lambda: _get_ontology_uncached(ontology_id, db),
    )


def _get_ontology_uncached(ontology_id: str, db: Session) -> dict:
    p = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not p:
        raise HTTPException(404, "Not found")
    release = _resolved_release_map(db, [p]).get(p.current_release_id)
    # jsonable_encoder 把 datetime 归一为与 FastAPI 响应一致的 ISO 串，
    # 保证缓存命中与直查两种路径返回逐字节相同的 JSON。
    return jsonable_encoder(
        {"data": _project_payload(p, OntologyOut, release)})


@router.post("/{ontology_id}/assistant-card-clicks")
def record_assistant_card_click(
    ontology_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """本体助手卡片确认选中一次的全局计数（不按用户区分）。

    只由助手卡片轮播的确认选中触发；下拉框切换与深链进入不调用本端点。
    SQL 侧原子自增，避免并发点击丢失计数。
    """
    updated = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id,
    ).update(
        {OntologyProject.assistant_card_clicks: OntologyProject.assistant_card_clicks + 1},
        synchronize_session=False,
    )
    if not updated:
        raise HTTPException(404, "Not found")
    db.commit()
    clicks = db.query(OntologyProject.assistant_card_clicks).filter(
        OntologyProject.id == ontology_id,
    ).scalar()
    return {"data": {"id": ontology_id, "assistant_card_clicks": clicks}}

@router.put("/{ontology_id}")
def update_ontology(ontology_id: str, body: OntologyUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    p = require_ontology_access(db, ontology_id, current_user, write=True)
    update = body.model_dump(exclude_none=True)
    if "domain" in update and update["domain"] != p.domain:
        _validate_domain(db, update["domain"], current_user=current_user)
    if "name" in update:
        existing = db.query(OntologyProject).filter(
            OntologyProject.name.ilike(update["name"]),
            OntologyProject.id != ontology_id,
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail={
                "error": "DUPLICATE_NAME",
                "message": f"Ontology 名称「{update['name']}」已存在",
                "existing_id": existing.id,
            })
    for k, v in update.items():
        setattr(p, k, v)
    db.commit(); db.refresh(p)
    ontology_cache.invalidate_detail()
    network_cache.invalidate_network()
    ontology_cache.invalidate_list()
    release = _resolved_release_map(db, [p]).get(p.current_release_id)
    return {"data": _project_payload(p, OntologyOut, release)}

@router.delete("/{ontology_id}", status_code=204)
def delete_ontology(ontology_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    with _ontology_build_lock(db, ontology_id):
        p = require_ontology_access(
            db,
            ontology_id,
            current_user,
            write=True,
        )
        # Commit the durable read fence before deleting the derived graph.
        # If SQL deletion later fails, readers remain blocked until retry or
        # startup repair reconstructs the still-authoritative SQL truth.
        mark_projecting(db, ontology_id)
        db.commit()

        from app.ontologies.graph.neo4j_service import Neo4jService

        neo4j = Neo4jService()
        try:
            if not neo4j.available:
                raise RuntimeError("Neo4j is unavailable")
            neo4j.delete_by_ontology(ontology_id)
        except Exception as exc:
            db.rollback()
            mark_failed(
                db,
                ontology_id,
                f"Neo4j ontology deletion failed: {type(exc).__name__}",
            )
            db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ontology_projection_delete_failed",
                    "message": "Neo4j 本体投影删除失败，SQL 本体已保留",
                },
            ) from exc
        finally:
            neo4j.close()

        try:
            db.delete(p)
            db.commit()
        except Exception as exc:
            db.rollback()
            persisted = db.query(OntologyProject).filter(
                OntologyProject.id == ontology_id,
            ).first()
            if persisted is None:
                # Database drivers may report a connection/commit error after
                # PostgreSQL has durably accepted the DELETE. Neo4j was already
                # cleared above, so an absent authoritative row is the complete
                # requested outcome and must converge to the normal 204 result.
                ontology_cache.invalidate_detail()
                ontology_cache.invalidate_overview()
                ontology_cache.invalidate_instance_counts()
                ontology_cache.invalidate_pending()
                network_cache.invalidate_network()
                ontology_cache.invalidate_version_tree()
                ontology_cache.invalidate_list()
                return None
            mark_failed(
                db,
                ontology_id,
                f"SQL ontology deletion failed: {type(exc).__name__}",
            )
            db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ontology_delete_incomplete",
                    "message": "本体删除未完成，已阻断图读取，请重试",
                },
            ) from exc
        else:
            ontology_cache.invalidate_detail()
            ontology_cache.invalidate_overview()
            ontology_cache.invalidate_instance_counts()
            ontology_cache.invalidate_pending()
            network_cache.invalidate_network()
            ontology_cache.invalidate_version_tree()
            ontology_cache.invalidate_list()
