"""本体网络的只读查询服务。

职责边界：

* ``list_overview``：全部本体的清单与规模统计（类型 / 链接 / 实例数、发布状态）；
* ``build_network_graph``：逐本体复用 agent_runtime 的 ``build_workspace_graph``
  构建 L1/L2 图，合并为跨本体全局图；同名对象类型之间生成展示层桥接边；
* 路径 / 影响分析沿用单本体语义，直接委托 agent_runtime 的既有服务函数，
  仅补齐"未显式给 release 时默认取当前发布版"的口径；
* 结构投影读（文末）：SQL release 快照为权威，Neo4j ``OntologyStructureNode``
  只是可重建探索图；读侧实时 AgentProfile 授权过滤，绝不假空图/假成功。
"""
from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.ontology import OntologyProject
from app.ontologies.agent_runtime.boundary import AgentScope, ToolError, build_scope
from app.ontologies.agent_runtime.graph_service import (
    analyze_change_impact,
    build_workspace_graph,
    find_paths,
    get_instance_detail,
)
from app.ontologies.agent_runtime import schemas as S


# 全局画布预算。单本体内部还有 build_workspace_graph 自己的 800/2000 保护，
# 这里限制的是"一次跨本体请求"的总量，防止多选时响应无界膨胀。
NETWORK_MAX_ONTOLOGIES = 12
NETWORK_MAX_NODES = 800
NETWORK_MAX_EDGES = 2000
DEFAULT_LIMIT_PER_TYPE = 10
# 单请求语句超时：上亿实例下未命中缓存的计数/搜索可能长跑，
# 超时统一转为可读错误而非挂死数据库连接。
NETWORK_STATEMENT_TIMEOUT_MS = 8000
_TIMEOUT_MESSAGE = "数据量过大，请缩小范围或稍后重试"

_BRIDGE_LABEL = "同名类型"


def _arm_statement_timeout(db: Session) -> None:
    """为当前事务设置语句超时；上下文不具备事务时静默跳过。"""
    try:
        db.execute(text(f"SET LOCAL statement_timeout = '{NETWORK_STATEMENT_TIMEOUT_MS}ms'"))
    except Exception:  # noqa: BLE001 - 超时护栏失败不阻塞业务
        pass


def _raise_timeout(error: OperationalError) -> None:
    """PG 语句超时（SQLSTATE 57014）映射为可读的请求错误。"""
    if getattr(error.orig, "pgcode", None) == "57014":
        raise NetworkRequestError(_TIMEOUT_MESSAGE) from error
    raise error


def _normalize_key(raw: Optional[str]) -> str:
    """名称归一化：NFKC + casefold + 去空白，供同名启发式比较。"""
    if not raw:
        return ""
    return unicodedata.normalize("NFKC", str(raw)).strip().casefold()


def _resolve_scope(
    db: Session, project: OntologyProject, *, release_id: Optional[str] = None
) -> tuple[AgentScope, Optional[str], bool]:
    """解析一个本体的读取口径。

    返回 ``(scope, effective_release_id, published)``。已发布本体固定读
    当前发布版的快照 + 对应实例；未发布本体回退工作区实时数据（release_id
    为 None），由调用方在响应中显式标注"未发布"，避免用户误以为是发布态。
    """
    effective_release = release_id
    if effective_release is None:
        effective_release = project.current_release_id or None
    _, _, scope = build_scope(db, project.id, release_id=effective_release)
    return scope, effective_release, effective_release is not None


def _section_from_scope(
    project: OntologyProject, scope: AgentScope, release_id: Optional[str],
    *, fresh: bool = False,
) -> dict:
    counts = scope.instance_counts(fresh=fresh)
    version = None
    if release_id and scope.release is not None:
        version = getattr(scope.release, "version_number", None)
    from app.ontologies.agent_runtime.boundary import remote_object_type_ids

    remote_count = len(remote_object_type_ids(scope) & set(scope.object_types))
    local_instances = int(sum(counts.values()))
    # instanceCount 契约保持"本地物化实例数"（远程不经本地统计，绝不为
    # 远程冒充 0 行数）：全远程本体 instanceCount=0 需结合
    # unknownRemoteTypeCount>=1 读作"本地 0 + 远程未知"，而非"无数据"。
    return {
        "id": project.id,
        "name": project.name,
        "domain": project.domain,
        "published": release_id is not None,
        "releaseId": release_id,
        "version": version,
        "typeCount": len(scope.object_types),
        "linkTypeCount": len(scope.link_types),
        "instanceCount": local_instances,
        "knownLocalInstanceCount": local_instances,
        "unknownRemoteTypeCount": remote_count,
    }


def _ontology_section(
    db: Session, project: OntologyProject, *, fresh: bool = False,
) -> dict:
    """单个本体的概览统计（overview 与 graph 共用口径）。"""
    scope, release_id, _published = _resolve_scope(db, project)
    return _section_from_scope(project, scope, release_id, fresh=fresh)


def list_overview(db: Session, *, fresh: bool = False) -> list[dict]:
    _arm_statement_timeout(db)
    try:
        return _list_overview(db, fresh=fresh)
    except OperationalError as error:
        _raise_timeout(error)
        raise


def _list_overview(db: Session, *, fresh: bool) -> list[dict]:
    projects = (
        db.query(OntologyProject).order_by(OntologyProject.created_at, OntologyProject.id).all()
    )
    items: list[dict] = []
    for project in projects:
        try:
            items.append(_ontology_section(db, project, fresh=fresh))
        except ToolError as error:
            # 单个本体口径异常（如发布指针损坏）不拖垮整个清单。
            items.append({
                "id": project.id,
                "name": project.name,
                "domain": project.domain,
                "published": False,
                "releaseId": None,
                "version": None,
                "typeCount": 0,
                "linkTypeCount": 0,
                "instanceCount": 0,
                "knownLocalInstanceCount": 0,
                "unknownRemoteTypeCount": 0,
                "error": str(error),
            })
    return items


class NetworkRequestError(ToolError):
    """请求参数层面的错误（区别于单个本体的数据错误）。"""


def _tagged(node: dict, section: dict) -> dict:
    tagged = dict(node)
    tagged["ontologyId"] = section["id"]
    tagged["ontologyName"] = section["name"]
    return tagged


def _tagged_edge(edge: dict, section: dict) -> dict:
    tagged = dict(edge)
    tagged["ontologyId"] = section["id"]
    tagged["ontologyName"] = section["name"]
    return tagged


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


def compute_bridge_groups(type_nodes: list[dict]) -> list[dict]:
    """把不同本体间同名的对象类型聚成桥接组（纯展示层启发式）。

    匹配规则：technical name 或 display_name 归一化后相同即视为同名；
    同一本体内部的两个类型永远不会互连。节点键带 ontologyId 前缀，即使
    两个本体存在同 id 的类型（测试夹具常见）也不会被误并成一个节点。
    返回的组内成员按 (ontologyId, label) 稳定排序，保证响应可复现。
    """
    def uid(node: dict) -> str:
        return f"{node['ontologyId']}::{node['id']}"

    uf = _UnionFind()
    for node in type_nodes:
        uf.add(uid(node))
    by_name: dict[str, list[dict]] = {}
    by_display: dict[str, list[dict]] = {}
    for node in type_nodes:
        technical = _normalize_key(node.get("technicalName"))
        display = _normalize_key(node.get("label"))
        for key_bucket, key in ((by_name, technical), (by_display, display)):
            if not key:
                continue
            for peer in key_bucket.get(key, []):
                uf.union(uid(node), uid(peer))
            key_bucket.setdefault(key, []).append(node)

    members: dict[str, list[dict]] = {}
    for node in type_nodes:
        members.setdefault(uf.find(uid(node)), []).append(node)

    groups: list[dict] = []
    for group_nodes in members.values():
        ontology_ids = {node["ontologyId"] for node in group_nodes}
        if len(ontology_ids) < 2:
            continue
        ordered = sorted(group_nodes, key=lambda n: (n["ontologyId"], n["label"]))
        groups.append({
            "key": f"bridge-group-{uuid4().hex[:8]}",
            "label": ordered[0]["label"],
            "members": [
                {
                    "nodeId": node["id"],
                    "entityId": node["entityId"],
                    "ontologyId": node["ontologyId"],
                    "ontologyName": node["ontologyName"],
                    "label": node["label"],
                }
                for node in ordered
            ],
        })
    groups.sort(key=lambda g: g["label"])
    return groups


def bridge_edges(groups: Iterable[dict]) -> list[dict]:
    """同名桥接组 → 组内两两虚线边（纯展示层，不落库）。"""
    edges: list[dict] = []
    for group in groups:
        members = group["members"]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                left, right = members[i], members[j]
                pair = sorted([left["nodeId"], right["nodeId"]])
                edges.append({
                    "id": f"bridge:{pair[0]}::{pair[1]}",
                    "kind": "bridge",
                    "source": left["nodeId"],
                    "target": right["nodeId"],
                    "label": _BRIDGE_LABEL,
                    "bridgeGroup": group["key"],
                    "crossOntology": True,
                })
    return edges


def _truncate_merged(
    nodes: list[dict], edges: list[dict]
) -> tuple[list[dict], list[dict], dict]:
    truncated = False
    kept_nodes = nodes[:NETWORK_MAX_NODES]
    if len(nodes) > NETWORK_MAX_NODES:
        truncated = True
    visible_ids = {node["id"] for node in kept_nodes}
    kept_edges: list[dict] = []
    dropped_edges = 0
    for edge in edges:
        if edge["source"] not in visible_ids or edge["target"] not in visible_ids:
            dropped_edges += 1
            truncated = True
            continue
        if len(kept_edges) >= NETWORK_MAX_EDGES:
            dropped_edges += 1
            truncated = True
            continue
        kept_edges.append(edge)
    meta = {
        "nodeBudget": NETWORK_MAX_NODES,
        "edgeBudget": NETWORK_MAX_EDGES,
        "truncated": truncated,
        "droppedEdges": dropped_edges,
        "nodeCount": len(kept_nodes),
        "edgeCount": len(kept_edges),
    }
    return kept_nodes, kept_edges, meta


def build_network_graph(
    db: Session,
    *,
    ontology_ids: list[str],
    level: int = 2,
    query: Optional[str] = None,
    limit_per_type: int = DEFAULT_LIMIT_PER_TYPE,
    bridge_same_name: bool = True,
    fresh: bool = False,
) -> dict:
    """构建跨本体全局图：逐本体投影后合并 + 同名类型桥接。"""
    _arm_statement_timeout(db)
    try:
        return _build_network_graph(
            db,
            ontology_ids=ontology_ids,
            level=level,
            query=query,
            limit_per_type=limit_per_type,
            bridge_same_name=bridge_same_name,
            fresh=fresh,
        )
    except OperationalError as error:
        _raise_timeout(error)
        raise


def _augment_remote_structure(scope: AgentScope, graph: dict) -> dict:
    """网络层补齐远程绑定类型的结构可见性。

    agent 本地图谱（``build_workspace_graph``）按既有契约只承载本地物化
    数据面，远程类型被显式排除——本体类型全部远程绑定时画布 0 节点并
    误导为"无类型"。网络画布是结构视图：远程类型以 ``remote: true``、
    实例数 ``null``（unknown，绝不以 0 冒充真实行数）出现；已发布关系
    的 schema 边允许展示（发布关系是治理事实），业务 join 仍由断言
    证据守卫，不因本展示放宽。类型/关系全部来自当前 release + profile
    授权 scope，不引入无过滤 fallback。
    """
    from app.ontologies.agent_runtime.boundary import remote_object_type_ids

    remote_ids = remote_object_type_ids(scope)
    if not remote_ids:
        return graph
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    existing_node_ids = {node.get("id") for node in nodes}
    existing_edge_ids = {edge.get("id") for edge in edges}
    for object_type in scope.object_types.values():
        if object_type.id not in remote_ids:
            continue
        node_id = f"type:{object_type.id}"
        if node_id in existing_node_ids:
            continue
        nodes.append({
            "id": node_id,
            "entityId": object_type.id,
            "kind": "object_type",
            "remote": True,
            "label": object_type.display_name,
            "technicalName": object_type.name,
            "objectTypeId": object_type.id,
            "count": None,
            "color": object_type.color,
            "description": object_type.description,
            "propertiesCount": len(object_type.properties or []),
        })
    for link_type in scope.link_types.values():
        if not ({link_type.source_object_type_id,
                 link_type.target_object_type_id} & remote_ids):
            continue
        edge_id = f"schema:{link_type.id}"
        if edge_id in existing_edge_ids:
            continue
        edges.append({
            "id": edge_id,
            "entityId": link_type.id,
            "kind": "schema_relation",
            "source": f"type:{link_type.source_object_type_id}",
            "target": f"type:{link_type.target_object_type_id}",
            "label": link_type.display_name,
            "linkTypeId": link_type.id,
            "cardinality": link_type.cardinality,
            "publishedStructureOnly": True,
        })
    return {**graph, "nodes": nodes, "edges": edges}


def _build_network_graph(
    db: Session,
    *,
    ontology_ids: list[str],
    level: int = 2,
    query: Optional[str] = None,
    limit_per_type: int = DEFAULT_LIMIT_PER_TYPE,
    bridge_same_name: bool = True,
    fresh: bool = False,
) -> dict:
    unique_ids = list(dict.fromkeys(oid.strip() for oid in ontology_ids if oid.strip()))
    if not unique_ids:
        raise NetworkRequestError("请至少选择一个本体")
    if len(unique_ids) > NETWORK_MAX_ONTOLOGIES:
        raise NetworkRequestError(f"一次最多同时查看 {NETWORK_MAX_ONTOLOGIES} 个本体")
    depth = 1 if level <= 1 else 2
    per_type = max(1, min(int(limit_per_type), 20))
    keyword = (query or "").strip() or None

    nodes: list[dict] = []
    edges: list[dict] = []
    sections: list[dict] = []
    errors: list[dict] = []

    for ontology_id in unique_ids:
        project = (
            db.query(OntologyProject)
            .filter(OntologyProject.id == ontology_id)
            .first()
        )
        if not project:
            errors.append({"ontologyId": ontology_id, "message": "本体不存在"})
            continue
        try:
            scope, _release_id, _published = _resolve_scope(db, project)
            section = _section_from_scope(project, scope, _release_id, fresh=fresh)
            graph = build_workspace_graph(
                scope,
                depth=depth,
                query=keyword,
                limit_per_type=per_type,
            )
            graph = _augment_remote_structure(scope, graph)
        except ToolError as error:
            errors.append({"ontologyId": ontology_id, "message": str(error)})
            continue
        sections.append(section)
        nodes.extend(_tagged(node, section) for node in graph["nodes"])
        edges.extend(_tagged_edge(edge, section) for edge in graph["edges"])

    type_nodes = [node for node in nodes if node.get("kind") == "object_type"]
    groups = compute_bridge_groups(type_nodes) if bridge_same_name else []
    all_edges = edges + bridge_edges(groups)

    kept_nodes, kept_edges, meta = _truncate_merged(nodes, all_edges)
    total_instances = sum(section["instanceCount"] for section in sections)
    # totalInstances 契约为本地物化合计；远程类型数单独给出，页面汇总
    # 不得把"本地 0 + 远程未知"展示成"总实例 0"。
    return {
        "level": depth,
        "query": keyword,
        "limitPerType": per_type,
        "ontologies": sections,
        "errors": errors,
        "nodes": kept_nodes,
        "edges": kept_edges,
        "bridges": {"enabled": bool(bridge_same_name), "groups": groups},
        "meta": {
            **meta,
            "selectedOntologies": len(sections),
            "totalInstances": total_instances,
            "totalUnknownRemoteTypes": sum(
                section["unknownRemoteTypeCount"] for section in sections),
        },
    }


# ------------------------------------------------ 路径 / 影响 / 实例详情


def network_instance_detail(
    db: Session, ontology_id: str, instance_id: str, *, release_id: Optional[str] = None
) -> dict:
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise ToolError("本体不存在")
    scope, _release_id, _published = _resolve_scope(db, project, release_id=release_id)
    return get_instance_detail(scope, instance_id)


def network_find_paths(
    db: Session, ontology_id: str, body: S.GraphPathRequest
) -> dict:
    _arm_statement_timeout(db)
    try:
        return _network_find_paths(db, ontology_id, body)
    except OperationalError as error:
        _raise_timeout(error)
        raise


def _network_find_paths(
    db: Session, ontology_id: str, body: S.GraphPathRequest
) -> dict:
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise ToolError("本体不存在")
    scope, _release_id, _published = _resolve_scope(db, project, release_id=body.release_id)
    return find_paths(
        scope,
        body.source_instance_id,
        body.target_instance_id,
        direction=body.direction,
        max_depth=body.max_depth,
        max_paths=body.max_paths,
    )


def network_analyze_impact(
    db: Session, ontology_id: str, body: S.GraphImpactRequest
) -> dict:
    _arm_statement_timeout(db)
    try:
        return _network_analyze_impact(db, ontology_id, body)
    except OperationalError as error:
        _raise_timeout(error)
        raise


def _network_analyze_impact(
    db: Session, ontology_id: str, body: S.GraphImpactRequest
) -> dict:
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise ToolError("本体不存在")
    scope, _release_id, _published = _resolve_scope(db, project, release_id=body.release_id)
    return analyze_change_impact(
        scope,
        body.instance_id,
        body.property,
        body.proposed_value,
        direction=body.direction,
        max_depth=body.max_depth,
    )


# ------------------------------------------------ 结构投影读（探索视图）

from app.ontologies.graph.structure_projection import (  # noqa: E402
    KIND_ACTION,
    KIND_FUNCTION,
    KIND_LINK_MAPPING,
    KIND_LINK_TYPE,
    KIND_MAPPING,
    KIND_OBJECT_TYPE,
    KIND_PROPERTY,
    STRUCTURE_LABEL,
    structure_projection_status,
)


class StructureGraphError(ToolError):
    """结构图不可读：未就绪/漂移/图不一致/Neo4j 不可用。绝不假空图。"""

    def __init__(self, message: str, *, payload: dict | None = None,
                 http_status: int = 503):
        super().__init__(message)
        self.payload = payload or {}
        self.http_status = http_status


def _read_structure_subgraph(
    ontology_id: str, release_id: str, expected_hash: str,
) -> tuple[list, list]:
    """读当前 release 结构子图，并按 meta 节点做一致性核验。

    meta 缺失、哈希不符或实际节点/边数与 meta 计数不一致（如被并发删除
    或残留半成品）即抛错，不以空图冒充成功。
    """
    from app.ontologies.graph.neo4j_service import (
        Neo4jService, _decode_properties,
    )

    neo = Neo4jService()
    if not neo.available:
        raise StructureGraphError("Neo4j 不可用，结构图暂不能读取")
    try:
        params = {"oid": ontology_id, "rid": release_id}
        base = (
            f"MATCH (n:{STRUCTURE_LABEL}) WHERE n.structure_ontology_id "
            f"= $oid AND n.structure_release_id = $rid"
        )
        node_rows = neo.run_cypher(
            f"{base} AND n.kind <> 'release_meta' RETURN n", params)
        nodes = [
            _decode_properties(dict(row["n"])) for row in node_rows if row.get("n")
        ]
        edge_rows = neo.run_cypher(
            f"MATCH (a:{STRUCTURE_LABEL})-[r]->(b:{STRUCTURE_LABEL}) "
            "WHERE a.structure_ontology_id = $oid AND a.structure_release_id = $rid "
            "AND b.structure_ontology_id = $oid AND b.structure_release_id = $rid "
            "RETURN a.structure_key AS source, b.structure_key AS target, "
            "type(r) AS rel_type, r AS props",
            params,
        )
        edges = [
            {
                "id": _decode_properties(dict(row["props"])).get("id"),
                "type": row["rel_type"],
                "source": row["source"],
                "target": row["target"],
            }
            for row in edge_rows
        ]
        meta_rows = neo.run_cypher(
            f"MATCH (n:{STRUCTURE_LABEL} {{structure_key: $meta_key}}) RETURN n",
            {"meta_key": f"{release_id}::release_meta"},
        )
        meta = (
            _decode_properties(dict(meta_rows[0]["n"]))
            if meta_rows and meta_rows[0].get("n") else None
        )
        # 计数严格非负整数：合法 0（空 release）不得被 or -1 误判；
        # malformed meta（字符串/None/布尔/浮点）判不一致。
        def _strict(value) -> int | None:
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                return None
            return value

        meta_nodes = _strict(meta.get("node_count")) if meta else None
        meta_edges = _strict(meta.get("edge_count")) if meta else None
        consistent = (
            meta is not None
            and str(meta.get("snapshot_hash") or "") == str(expected_hash)
            and meta_nodes is not None and meta_nodes == len(nodes)
            and meta_edges is not None and meta_edges == len(edges)
        )
        if not consistent:
            raise StructureGraphError(
                "结构图与 release meta 不一致（可能被并发删除或存在半成品），"
                "请重新触发重建",
                payload={"code": "structure_projection_inconsistent"},
            )
        return nodes, edges
    finally:
        neo.close()


def _filter_structure_by_scope(scope, nodes: list, edges: list) -> tuple[list, list]:
    """实时 AgentProfile 过滤：静态图不作授权依据，fail-closed。

    口径说明：``AgentProfile`` 是本体级配置（``build_scope`` 无用户参数），
    本过滤对所有请求方一致；用户差异只体现在路由的 require_ontology_access
    与 join overlay 的双端校验。规则与 boundary 一致：不可见对象类型
    整体剔除（含其属性/链接/动作/映射引用）；链接类型直接以
    ``scope.link_types`` allowlist 判定（已同时编码两端类型可见性）；
    函数同时有类型与动作引用时两者都须授权；属性按父类型 allowlist 用
    纯属性名判定；无归属目标或引用缺失的节点一律隐藏；边两端必须可见。
    """
    visible_types = set(scope.object_types)
    visible_links = set(scope.link_types)
    visible_actions = set(scope.actions)

    # 属性节点的父类型经 STRUCTURE_TYPE_PROPERTY 边推导（key 末段即 local_id）。
    property_parent: dict[str, str] = {}
    for edge in edges:
        if edge["type"] == "STRUCTURE_TYPE_PROPERTY":
            property_parent[edge["target"]] = edge["source"].rsplit("::", 1)[-1]

    def _link_type_visible(link_id: object) -> bool:
        # scope.link_types 已编码 allowlist + 两端类型可见；缺失即隐藏。
        return bool(link_id) and str(link_id) in visible_links

    kept: list[dict] = []
    kept_keys: set[str] = set()
    for node in nodes:
        kind = node.get("kind")
        local_id = str(node.get("local_id") or "")
        attributes = node.get("attributes") or {}
        if kind == KIND_OBJECT_TYPE:
            visible = local_id in visible_types
        elif kind == KIND_PROPERTY:
            parent = property_parent.get(node.get("structure_key") or "")
            visible = False
            if parent is not None and parent in visible_types:
                allowed = scope.allowed_property_names(parent)
                # 纯属性名（node.name）判定，local_id 带类型前缀不可比。
                visible = allowed is None or str(
                    node.get("name") or "") in allowed
        elif kind == KIND_LINK_TYPE:
            visible = local_id in visible_links
        elif kind == KIND_ACTION:
            # scope.actions 已同时编码动作 allowlist 与绑定类型可见性。
            visible = local_id in visible_actions
            if visible:
                target_type = attributes.get("objectTypeId")
                if target_type is not None:
                    allowed = scope.allowed_property_names(target_type)
                    if (allowed is not None
                            and isinstance(attributes.get("parameters"), list)):
                        attributes = dict(attributes)
                        attributes["parameters"] = [
                            p for p in attributes["parameters"]
                            if isinstance(p, dict) and p.get("name") in allowed
                        ]
        elif kind == KIND_FUNCTION:
            target_type = attributes.get("targetObjectTypeId")
            target_action = attributes.get("targetActionId")
            if target_type is not None:
                # 类型可见且（若有动作引用）动作也须授权，防借函数泄隐藏动作。
                visible = target_type in visible_types and (
                    target_action is None
                    or str(target_action) in visible_actions)
            elif target_action is not None:
                visible = str(target_action) in visible_actions
            else:
                visible = False  # 无归属目标的可执行定义不外泄
        elif kind == KIND_MAPPING:
            # 映射必须挂接可见类型；无目标类型的映射定义不默认放行。
            visible = attributes.get("targetObjectTypeId") in visible_types
        elif kind == KIND_LINK_MAPPING:
            visible = _link_type_visible(attributes.get("linkTypeId"))
        else:
            visible = False
        if visible:
            node = dict(node)
            if kind == KIND_ACTION:
                node["attributes"] = attributes
            kept.append(node)
            kept_keys.add(str(node.get("structure_key") or ""))

    kept_edges = [
        edge for edge in edges
        if edge["source"] in kept_keys and edge["target"] in kept_keys
    ]
    return kept, kept_edges


def _assertion_side_visible(
    db: Session, user, ontology_id: str, object_type_id: str, prop: str,
) -> bool:
    """断言一侧当前是否可见（本体读权限 + 当前 release + 类型/属性授权）。

    属性必须存在于当前 release 的类型结构中（release 已删除的属性算
    失效证据），且在属性 allowlist 内；任一环节失败（无权限、无当前
    发布、类型/属性不在当前 release、profile 停用）都判不可见。
    """
    from app.ontologies.access import require_ontology_access
    from app.ontologies.release_context import current_release_context

    try:
        require_ontology_access(db, ontology_id, user, write=False)
        context = current_release_context(db, ontology_id)
        _project, profile, scope = build_scope(
            db, ontology_id, release_id=context.release.id)
        if not profile.enabled:
            return False
        object_type = scope.object_types.get(object_type_id)
        if object_type is None:
            return False
        declared = {
            p.get("name")
            for p in (getattr(object_type, "properties", None) or [])
            if isinstance(p, dict) and p.get("name")
        }
        if prop not in declared:
            return False  # 属性不在当前 release 结构：失效证据不外显
        allowed = scope.allowed_property_names(object_type_id)
        return allowed is None or prop in allowed
    except Exception:  # noqa: BLE001 - 授权链任何失败都判不可见
        return False


def _join_assertion_overlays(db: Session, user, ontology_id: str) -> list[dict]:
    """本人跨本体关联声明实时 overlay（SQL 权威，永不入图、跨用户不可见）。

    只有两侧当前都对该用户可见（读权限 + 当前 release + 类型/属性授权）
    的声明才返回，避免借 overlay 泄露另一本体的类型/属性元数据。
    """
    from sqlalchemy import or_
    from app.ontologies.agent_query.models import AgentJoinAssertion

    rows = (
        db.query(AgentJoinAssertion)
        .filter(
            AgentJoinAssertion.created_by == str(user.id),
            or_(
                AgentJoinAssertion.left_ontology_id == ontology_id,
                AgentJoinAssertion.right_ontology_id == ontology_id,
            ),
        )
        .order_by(AgentJoinAssertion.created_at.desc())
        .limit(20).all()
    )
    overlays = []
    for row in rows:
        if not (_assertion_side_visible(
                    db, user, row.left_ontology_id,
                    row.left_object_type_id, row.left_property)
                and _assertion_side_visible(
                    db, user, row.right_ontology_id,
                    row.right_object_type_id, row.right_property)):
            continue
        overlays.append({
            "assertionId": row.id,
            "category": "user_asserted",
            "leftOntologyId": row.left_ontology_id,
            "leftObjectTypeId": row.left_object_type_id,
            "leftProperty": row.left_property,
            "rightOntologyId": row.right_ontology_id,
            "rightObjectTypeId": row.right_object_type_id,
            "rightProperty": row.right_property,
            "keyType": row.key_type,
            "businessMeaning": row.business_meaning,
        })
    return overlays


def get_structure_graph(db: Session, ontology_id: str, user) -> dict:
    """当前 release 结构图（实时授权过滤 + 双端可见的 join 声明 overlay）。

    只在 fence ready 且无 drift、图与 meta 一致时服务；AgentProfile 停用
    时拒绝（403）；旧 release 子图绝不混读。
    """
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise ToolError("本体不存在")

    status = structure_projection_status(db, ontology_id)
    if status["status"] != "ready" or status["drift"]:
        raise StructureGraphError(
            "结构投影未就绪或已漂移，请先重建（状态对账以当前发布为准）",
            payload={
                "code": "ontology_structure_projection_not_ready",
                "projection_status": status["status"],
                "drift": status["drift"],
                "error": status["error"],
            },
        )

    raw_nodes, raw_edges = _read_structure_subgraph(
        ontology_id, str(status["releaseId"]), str(status["snapshotHash"]))
    _project, profile, scope = build_scope(
        db, ontology_id, release_id=str(status["releaseId"]))
    if not profile.enabled:
        raise StructureGraphError(
            "该本体的智能体已停用，结构探索视图不可用",
            payload={"code": "profile_disabled"}, http_status=403,
        )
    nodes, edges = _filter_structure_by_scope(scope, raw_nodes, raw_edges)

    api_nodes = [
        {
            "id": node.get("structure_key"),
            "kind": node.get("kind"),
            "localId": node.get("local_id"),
            "name": node.get("name"),
            "displayName": node.get("display_name"),
            "description": node.get("description"),
            "attributes": node.get("attributes") or {},
        }
        for node in nodes
    ]
    api_edges = [
        {
            "id": edge["id"] or f"{edge['type']}:{edge['source']}->{edge['target']}",
            "type": edge["type"],
            "source": edge["source"],
            "target": edge["target"],
        }
        for edge in edges
    ]
    kept_nodes, kept_edges, meta = _truncate_merged(api_nodes, api_edges)
    return {
        "release": {
            "releaseId": status["releaseId"],
            "version": scope.release.version if scope.release is not None
            else status.get("version"),
            "snapshotHash": status["snapshotHash"],
        },
        "status": "ready",
        "nodes": kept_nodes,
        "edges": kept_edges,
        "overlays": {
            "joinAssertions": _join_assertion_overlays(db, user, ontology_id),
        },
        "meta": meta,
    }
