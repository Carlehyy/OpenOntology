"""本体网络 API — /api/v2/ontology-network/*

  GET  /overview                        全部本体清单 + 规模统计
  GET  /graph                           跨本体全局图（L1/L2 + 同名桥接）
  GET  /{ontology_id}/instances/{id}    实例详情（右侧面板字段列表）
  POST /{ontology_id}/paths             单本体最短路径（复用 agent_runtime 服务）
  POST /{ontology_id}/impact            单本体只读影响推演（dry-run）
  GET  /{ontology_id}/structure-projection        结构投影状态（SQL fence 对账）
  POST /{ontology_id}/structure-projection/rebuild 结构投影重建（仅 admin）
  GET  /{ontology_id}/structure-graph             当前 release 结构图（实时授权过滤）

既有五个端点只读不变；结构投影三端点中仅 rebuild 是写（Neo4j 探索图，
SQL release 快照仍是权威）。路由层不做业务判断，应用逻辑在 service.py；
权限由挂载点的 ``menu_guard("ontology_model.network")`` 统一守卫，与页面
菜单键一致；单本体端点再叠加 require_ontology_access。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db, get_current_user
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.graph.structure_projection import (
    StructureProjectionError,
    StructureProjectionRejected,
    rebuild_structure_projection,
    structure_projection_status,
)
from app.ontologies.network import cache as network_cache
from app.ontologies.network import service


router = APIRouter()


def _ok(data):
    return {"data": data}


@router.get("/overview")
def get_overview(
    fresh: bool = Query(default=False, description="跳过实例计数缓存，强制直查"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """全部本体的发布口径与规模统计，供页面数据范围选择器使用。"""
    # 全局聚合读：短 TTL 响应缓存 + 写路径 bump 版本换键（fail-open）；
    # fresh 手动刷新完全绕过，直查语义与现状一致。
    if fresh:
        return _ok(service.list_overview(db, fresh=True))

    def build():
        # jsonable_encoder 归一 datetime 等，保证缓存命中与直查逐字节一致。
        return jsonable_encoder(_ok(service.list_overview(db, fresh=False)))

    return network_cache.cached_call(
        network_cache.overview_cache_key(),
        settings.ontology_network_cache_ttl_seconds,
        build,
    )


@router.get("/graph")
def get_network_graph(
    ontology_ids: str = Query(..., max_length=2000, description="逗号分隔的本体 id"),
    level: int = Query(default=2, ge=1, le=2),
    query: str | None = Query(default=None, max_length=200),
    limit_per_type: int = Query(default=service.DEFAULT_LIMIT_PER_TYPE, ge=1, le=20),
    bridge_same_name: bool = Query(default=True),
    fresh: bool = Query(default=False, description="跳过实例计数缓存，强制直查"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """跨本体全局图：各本体子图叠加 + 可选同名类型虚线桥接。"""
    # 跨本体聚合读：参数维度进缓存键，短 TTL + 写路径 bump 失效（fail-open）；
    # fresh 手动刷新完全绕过。
    try:
        if fresh:
            return _ok(service.build_network_graph(
                db,
                ontology_ids=ontology_ids.split(","),
                level=level,
                query=query,
                limit_per_type=limit_per_type,
                bridge_same_name=bridge_same_name,
                fresh=True,
            ))

        def build():
            return jsonable_encoder(_ok(service.build_network_graph(
                db,
                ontology_ids=ontology_ids.split(","),
                level=level,
                query=query,
                limit_per_type=limit_per_type,
                bridge_same_name=bridge_same_name,
                fresh=False,
            )))

        return network_cache.cached_call(
            network_cache.graph_cache_key(
                ontology_ids, level, query, limit_per_type, bridge_same_name
            ),
            settings.ontology_network_cache_ttl_seconds,
            build,
        )
    except ToolError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/{ontology_id}/instances/{instance_id}")
def get_network_instance(
    ontology_id: str,
    instance_id: str,
    release_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    try:
        data = service.network_instance_detail(
            db, ontology_id, instance_id, release_id=release_id)
    except ToolError as error:
        raise HTTPException(404, str(error)) from error
    return _ok(data)


@router.post("/{ontology_id}/paths")
def query_network_paths(
    ontology_id: str,
    body: S.GraphPathRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    try:
        data = service.network_find_paths(db, ontology_id, body)
    except ToolError as error:
        raise HTTPException(422, str(error)) from error
    return _ok(data)


@router.post("/{ontology_id}/impact")
def query_network_impact(
    ontology_id: str,
    body: S.GraphImpactRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    try:
        data = service.network_analyze_impact(db, ontology_id, body)
    except ToolError as error:
        raise HTTPException(422, str(error)) from error
    return _ok(data)


@router.get("/{ontology_id}/structure-projection")
def get_structure_projection_status(
    ontology_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """结构投影 fence 状态：与当前发布指针/哈希实时对账，drift 不掩饰。

    具 network 菜单读权限即可查看（viewer 可看状态）；计数来自图 meta
    节点，漂移时如实归零。
    """
    require_ontology_access(db, ontology_id, user, write=False)
    try:
        return _ok(structure_projection_status(db, ontology_id))
    except StructureProjectionError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/{ontology_id}/structure-projection/rebuild")
def rebuild_structure_projection_endpoint(
    ontology_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """从当前 release 快照重建结构探索图（仅管理员）。

    并发语义：rebuild 持 build lock 串行执行；重复触发排队后幂等重建；
    持锁接管遗留 building fence（前持有者已消失），不存在不可恢复的
    永久 building。
    """
    if str(getattr(user, "role", "") or "") != "admin":
        raise HTTPException(403, "Only administrators may rebuild the structure projection")
    require_ontology_access(db, ontology_id, user, write=True)
    from app.ontologies.runtime_fence import _ontology_build_lock

    try:
        with _ontology_build_lock(db, ontology_id):
            rebuild_structure_projection(db, ontology_id)
    except StructureProjectionRejected as error:
        raise HTTPException(409, detail={
            "code": "structure_projection_rejected",
            "message": str(error),
        }) from error
    except StructureProjectionError as error:
        raise HTTPException(503, detail={
            "code": "structure_projection_failed",
            "message": str(error),
        }) from error
    return _ok(structure_projection_status(db, ontology_id))


@router.get("/{ontology_id}/structure-graph")
def get_structure_graph(
    ontology_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """当前 release 结构图（实时 user/profile 过滤 + 本人 join 声明 overlay）。

    权限过滤在读侧实时执行，静态图不作授权依据；未就绪/漂移返回 503，
    绝不以空图或旧 release 子图冒充成功。
    """
    require_ontology_access(db, ontology_id, user, write=False)
    try:
        return _ok(service.get_structure_graph(db, ontology_id, user))
    except service.StructureGraphError as error:
        raise HTTPException(error.http_status, detail={
            "message": str(error),
            **error.payload,
        }) from error
    except ToolError as error:
        raise HTTPException(404, str(error)) from error
