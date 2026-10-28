"""智能助手的数据图谱查询与只读推演服务。

所有入口都接收 :class:`AgentScope`，因此节点、关系和字段只能来自当前助手的
授权世界。这里故意只读取正规 ``fo_*`` 投影，不依赖 Neo4j；前者是智能助手
当前的事实源，且测试环境与轻量部署都能得到一致结果。

三类能力共享同一套有界查询：

* ``build_workspace_graph``：L1 类型、L2 实例、L3 聚焦实例属性；
* ``find_paths``：两个实例之间的最短路径（最多返回少量候选）；
* ``analyze_change_impact``：字段拟议变更的关系可达范围，只做 dry-run。

关系可达并不等于业务因果，所以影响结果明确标注 ``association_only``。只有
未来接入经过治理的影响规则后，才能把某条结果升级为确定性业务影响。
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable, Optional

from sqlalchemy import String, Text, cast, or_

from app.models.ontology_formal import LinkInstance, ObjectInstance
from app.ontologies.agent_runtime.boundary import (
    AgentScope, ToolError, remote_object_type_ids, require_materialized_object_type,
)


_MAX_DEPTH = 6
_MAX_PATHS = 5
_MAX_VISIBLE_NODES = 800
_MAX_VISIBLE_EDGES = 2000
_MAX_PROPERTIES = 60


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _instance_query(scope: AgentScope):
    query = scope.db.query(ObjectInstance).filter(
        ObjectInstance.ontology_id == scope.ontology.id)
    if scope.release_id is not None:
        query = query.filter(ObjectInstance.ontology_release_id == scope.release_id)
    return query


def _link_query(scope: AgentScope):
    query = scope.db.query(LinkInstance).filter(
        LinkInstance.ontology_id == scope.ontology.id)
    if scope.release_id is not None:
        query = query.filter(LinkInstance.ontology_release_id == scope.release_id)
    return query


def _property_name(object_type, property_ref: Optional[str]) -> Optional[str]:
    if not property_ref:
        return None
    for prop in object_type.properties or []:
        if not isinstance(prop, dict):
            continue
        if property_ref in (prop.get("id"), prop.get("name")):
            return prop.get("name")
    return property_ref


def instance_label(scope: AgentScope, instance: ObjectInstance) -> str:
    """生成稳定且便于用户核对的实例标签。"""
    object_type = scope.object_types.get(instance.object_type_id)
    values = {**(instance.properties or {}), **(instance.computed or {})}
    if object_type:
        primary_name = _property_name(object_type, object_type.primary_key)
        if primary_name and values.get(primary_name) not in (None, ""):
            return str(values[primary_name])[:120]
        for prop in object_type.properties or []:
            if not isinstance(prop, dict):
                continue
            value = values.get(prop.get("name"))
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return str(value)[:120]
    return str(instance.external_id or instance.id)[:120]


def _display_value(value: Any, limit: int = 120) -> str:
    if value is None:
        return "空值"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        text = str(value)
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _authorized_values(scope: AgentScope, object_type, instance) -> dict:
    """属性级授权后的可见值集合（P2-2：图谱预览/属性节点不泄露禁用属性）。"""
    values = {**(instance.properties or {}), **(instance.computed or {})}
    if object_type is None:
        return {}
    allowed = scope.allowed_property_names(object_type.id)
    if allowed is None:
        return values
    return {k: v for k, v in values.items() if k in allowed}


def _instance_preview(scope: AgentScope, instance: ObjectInstance) -> list[dict]:
    object_type = scope.object_types.get(instance.object_type_id)
    values = _authorized_values(scope, object_type, instance)
    definitions = {
        p.get("name"): p for p in (object_type.properties or [])
        if isinstance(p, dict) and p.get("name")
    } if object_type else {}
    preview: list[dict] = []
    for name, value in values.items():
        if value in (None, "") or isinstance(value, (dict, list)):
            continue
        definition = definitions.get(name) or {}
        preview.append({
            "name": name,
            "label": definition.get("displayName") or definition.get("display_name") or name,
            "value": _display_value(value, 48),
        })
        if len(preview) >= 3:
            break
    return preview


def _type_node(scope: AgentScope, object_type, count: int) -> dict:
    return {
        "id": f"type:{object_type.id}",
        "entityId": object_type.id,
        "kind": "object_type",
        "label": object_type.display_name,
        "technicalName": object_type.name,
        "objectTypeId": object_type.id,
        "count": int(count),
        "color": object_type.color,
        "description": object_type.description,
        "propertiesCount": len(object_type.properties or []),
    }


def _instance_node(scope: AgentScope, instance: ObjectInstance) -> dict:
    object_type = scope.object_types[instance.object_type_id]
    return {
        "id": f"instance:{instance.id}",
        "entityId": instance.id,
        "kind": "instance",
        "label": instance_label(scope, instance),
        "secondaryLabel": object_type.display_name,
        "objectTypeId": object_type.id,
        "objectTypeLabel": object_type.display_name,
        "source": instance.source,
        "externalId": instance.external_id,
        "preview": _instance_preview(scope, instance),
        "updatedAt": instance.updated_at.isoformat() if instance.updated_at else None,
    }


def _property_nodes(scope: AgentScope, instance: ObjectInstance) -> tuple[list[dict], list[dict], bool]:
    object_type = scope.object_types[instance.object_type_id]
    definitions = {
        p.get("name"): p for p in (object_type.properties or [])
        if isinstance(p, dict) and p.get("name")
    }
    values = _authorized_values(scope, object_type, instance)
    nodes: list[dict] = []
    edges: list[dict] = []
    items = list(values.items())
    truncated = len(items) > _MAX_PROPERTIES
    for index, (name, value) in enumerate(items[:_MAX_PROPERTIES]):
        definition = definitions.get(name) or {}
        node_id = f"property:{instance.id}:{index}"
        nodes.append({
            "id": node_id,
            "entityId": f"{instance.id}:{name}",
            "kind": "property",
            "label": definition.get("displayName") or definition.get("display_name") or name,
            "secondaryLabel": _display_value(value),
            "instanceId": instance.id,
            "objectTypeId": instance.object_type_id,
            "propertyName": name,
            "propertyType": definition.get("type") or "unknown",
            "value": value,
            "isNull": value is None,
        })
        edges.append({
            "id": f"attribute:{instance.id}:{index}",
            "kind": "attribute",
            "source": f"instance:{instance.id}",
            "target": node_id,
            "label": "属性",
        })
    return nodes, edges, truncated


def _link_edge(scope: AgentScope, link: LinkInstance) -> dict:
    link_type = scope.link_types[link.link_type_id]
    return {
        "id": f"link:{link.id}",
        "entityId": link.id,
        "kind": "relation",
        "source": f"instance:{link.source_object_id}",
        "target": f"instance:{link.target_object_id}",
        "label": link_type.display_name,
        "linkTypeId": link_type.id,
        "linkTypeName": link_type.name,
        "properties": link.properties or {},
    }


def _schema_edge(link_type) -> dict:
    return {
        "id": f"schema:{link_type.id}",
        "entityId": link_type.id,
        "kind": "schema_relation",
        "source": f"type:{link_type.source_object_type_id}",
        "target": f"type:{link_type.target_object_type_id}",
        "label": link_type.display_name,
        "linkTypeId": link_type.id,
        "cardinality": link_type.cardinality,
    }


def _contains_edge(type_id: str, instance_id: str) -> dict:
    return {
        "id": f"contains:{type_id}:{instance_id}",
        "kind": "contains",
        "source": f"type:{type_id}",
        "target": f"instance:{instance_id}",
        "label": "实例",
    }


def _require_materialized_instance(scope: AgentScope, instance: ObjectInstance) -> None:
    """实例所属类型为远程绑定时拒绝：本地图谱只承载本地物化数据面。"""
    object_type = scope.object_types.get(instance.object_type_id)
    if object_type is not None:
        require_materialized_object_type(scope, object_type)


def build_workspace_graph(
    scope: AgentScope,
    *,
    depth: int = 2,
    query: Optional[str] = None,
    object_type_ref: Optional[str] = None,
    focus_instance_id: Optional[str] = None,
    limit_per_type: int = 20,
) -> dict:
    depth = _clamp(depth, 1, 3, 2)
    profile_limit = max(1, int(scope.profile.max_rows_per_query or 50))
    per_type = _clamp(limit_per_type, 1, min(50, profile_limit), 20)
    remote_ids = remote_object_type_ids(scope)
    counts = scope.instance_counts()
    materialized_counts = {
        type_id: count for type_id, count in counts.items()
        if type_id not in remote_ids
    }
    remote_types = [
        object_type for object_type in scope.object_types.values()
        if object_type.id in remote_ids
    ]
    object_types = [
        object_type for object_type in scope.object_types.values()
        if object_type.id not in remote_ids
    ]
    selected_type_id: Optional[str] = None
    if object_type_ref:
        selected = scope.require_object_type(object_type_ref)
        require_materialized_object_type(scope, selected)
        selected_type_id = selected.id

    nodes = [_type_node(scope, object_type, counts.get(object_type.id, 0))
             for object_type in object_types]
    # 远程端点的链接类型不出现在本地图谱：schema 边若渲染，会以"零实例/零边"
    # 冒充完整关系图景；远程部分在 meta.remoteObjectTypes/remoteLinkTypes 诚实标注。
    visible_link_types = [
        link_type for link_type in scope.link_types.values()
        if link_type.source_object_type_id not in remote_ids
        and link_type.target_object_type_id not in remote_ids
    ]
    remote_link_types = [
        {"id": link_type.id, "name": link_type.name,
         "displayName": link_type.display_name}
        for link_type in scope.link_types.values()
        if link_type not in visible_link_types
    ]
    edges = [_schema_edge(link_type) for link_type in visible_link_types]
    loaded_instances: list[ObjectInstance] = []
    total_matches = 0
    truncated = False
    keyword = (query or "").strip()

    if depth >= 2:
        target_types = [scope.object_types[selected_type_id]] if selected_type_id else object_types
        for object_type in target_types:
            remaining = _MAX_VISIBLE_NODES - len(nodes)
            if remaining <= 0:
                truncated = True
                break
            take = min(per_type, remaining)
            q = _instance_query(scope).filter(
                ObjectInstance.object_type_id == object_type.id)
            if keyword:
                pattern = f"%{keyword}%"
                q = q.filter(or_(
                    ObjectInstance.id.ilike(pattern),
                    ObjectInstance.external_id.ilike(pattern),
                    # CAST AS TEXT 与迁移 0076 的 (properties::text) 表达式索引
                    # 严格一致，规划器才能命中 trgm GIN；VARCHAR 会退化为全表扫。
                    cast(ObjectInstance.properties, Text).ilike(pattern),
                    cast(ObjectInstance.computed, Text).ilike(pattern),
                ))
            # 无关键词时复用一次 group-by 得到的计数，避免对象类型越多就多出一轮 count 查询。
            matched_count = q.count() if keyword else counts.get(object_type.id, 0)
            total_matches += matched_count
            page = q.order_by(ObjectInstance.updated_at.desc(), ObjectInstance.id).limit(take).all()
            loaded_instances.extend(page)
            if matched_count > len(page):
                truncated = True

        for instance in loaded_instances:
            nodes.append(_instance_node(scope, instance))
            edges.append(_contains_edge(instance.object_type_id, instance.id))

        instance_ids = [instance.id for instance in loaded_instances]
        visible_link_ids = list(scope.link_types)
        if instance_ids and visible_link_ids:
            links = (_link_query(scope)
                     .filter(
                         LinkInstance.link_type_id.in_(visible_link_ids),
                         LinkInstance.source_object_id.in_(instance_ids),
                         LinkInstance.target_object_id.in_(instance_ids),
                     )
                     .order_by(LinkInstance.id)
                     .limit(_MAX_VISIBLE_EDGES + 1).all())
            if len(links) > _MAX_VISIBLE_EDGES:
                truncated = True
            edges.extend(_link_edge(scope, link) for link in links[:_MAX_VISIBLE_EDGES])

    property_truncated = False
    if depth == 3:
        if not focus_instance_id:
            raise ToolError("展开到属性层前，请先选择一个实例")
        focus = scope.visible_instance(
            scope.db.query(ObjectInstance).filter(ObjectInstance.id == focus_instance_id).first())
        if all(instance.id != focus.id for instance in loaded_instances):
            nodes.append(_instance_node(scope, focus))
            edges.append(_contains_edge(focus.object_type_id, focus.id))
        property_nodes, property_edges, property_truncated = _property_nodes(scope, focus)
        nodes.extend(property_nodes)
        edges.extend(property_edges)

    return {
        "ontologyId": scope.ontology.id,
        "ontologyName": scope.ontology.name,
        "depth": depth,
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "query": keyword or None,
            "objectTypeId": selected_type_id,
            "focusInstanceId": focus_instance_id,
            "instanceCounts": materialized_counts,
            "loadedInstances": len(loaded_instances),
            "matchedInstances": (total_matches if depth >= 2
                                 else sum(materialized_counts.values())),
            "limitPerType": per_type,
            "truncated": truncated or property_truncated,
            "propertyTruncated": property_truncated,
            "nodeBudget": _MAX_VISIBLE_NODES,
            "edgeBudget": _MAX_VISIBLE_EDGES,
            # 混类型诚实化：远程绑定部分不进图、不计数，也不冒充全量。
            # coverage 告知本图只覆盖本地物化数据面；远程部分显式列出，
            # 消费方（前端/LLM）不得把图当作完整业务图景。
            "coverage": ("materialized_only" if remote_ids
                         else "authorized_scope"),
            "remoteObjectTypes": [
                {"id": object_type.id, "name": object_type.name,
                 "displayName": object_type.display_name}
                for object_type in remote_types
            ],
            "remoteLinkTypes": remote_link_types,
        },
    }


def get_instance_detail(scope: AgentScope, instance_id: str) -> dict:
    instance = scope.visible_instance(
        scope.db.query(ObjectInstance).filter(ObjectInstance.id == instance_id).first())
    object_type = scope.object_types[instance.object_type_id]
    # P2-2：属性级授权同样作用于详情读路径 — 定义与值都按 allowlist 收敛
    allowed = scope.allowed_property_names(object_type.id)
    visible_defs = [
        p for p in (object_type.properties or [])
        if allowed is None or not isinstance(p, dict)
        or p.get("name") in allowed
    ]
    authorized = _authorized_values(scope, object_type, instance)
    properties = {k: v for k, v in (instance.properties or {}).items()
                  if k in authorized}
    computed = {k: v for k, v in (instance.computed or {}).items()
                if k in authorized}
    return {
        "id": instance.id,
        "label": instance_label(scope, instance),
        "objectType": {
            "id": object_type.id,
            "name": object_type.name,
            "displayName": object_type.display_name,
            "primaryKey": object_type.primary_key,
            "properties": visible_defs,
        },
        "properties": properties,
        "computed": computed,
        "source": instance.source,
        "externalId": instance.external_id,
        "createdAt": instance.created_at.isoformat() if instance.created_at else None,
        "updatedAt": instance.updated_at.isoformat() if instance.updated_at else None,
    }


def _visible_instance(scope: AgentScope, instance_id: str) -> ObjectInstance:
    return scope.visible_instance(
        scope.db.query(ObjectInstance).filter(ObjectInstance.id == instance_id).first())


def _frontier_links(
    scope: AgentScope,
    frontier: set[str],
    direction: str,
    remaining: int,
    seen_edges: set[str],
) -> tuple[list[LinkInstance], bool]:
    if not frontier or not scope.link_types or remaining <= 0:
        return [], False
    q = _link_query(scope).filter(
        LinkInstance.link_type_id.in_(list(scope.link_types)))
    if seen_edges:
        q = q.filter(~LinkInstance.id.in_(seen_edges))
    if direction == "outgoing":
        q = q.filter(LinkInstance.source_object_id.in_(frontier))
    elif direction == "incoming":
        q = q.filter(LinkInstance.target_object_id.in_(frontier))
    else:
        q = q.filter(or_(
            LinkInstance.source_object_id.in_(frontier),
            LinkInstance.target_object_id.in_(frontier),
        ))
    rows = q.order_by(LinkInstance.id).limit(remaining + 1).all()
    endpoint_ids = {
        endpoint_id
        for row in rows
        for endpoint_id in (row.source_object_id, row.target_object_id)
    }
    visible_endpoint_ids = {
        row.id for row in _instance_query(scope).filter(
            ObjectInstance.object_type_id.in_(list(scope.object_types)),
            ObjectInstance.id.in_(endpoint_ids),
        ).all()
    } if endpoint_ids else set()
    # 即便历史脏数据把允许的链接类型指向了越权对象，也不能经路径结果泄露其 id。
    visible_rows = [
        row for row in rows
        if row.source_object_id in visible_endpoint_ids
        and row.target_object_id in visible_endpoint_ids
    ]
    return visible_rows[:remaining], len(rows) > remaining


def _expand_graph(
    scope: AgentScope,
    start_id: str,
    *,
    direction: str,
    max_depth: int,
    stop_id: Optional[str] = None,
) -> tuple[dict[str, list[tuple[str, LinkInstance, str]]], dict[str, int], bool]:
    if direction not in {"both", "outgoing", "incoming"}:
        raise ToolError("direction 只能是 both、outgoing 或 incoming")
    adjacency: dict[str, list[tuple[str, LinkInstance, str]]] = defaultdict(list)
    depths = {start_id: 0}
    frontier = {start_id}
    seen_edges: set[str] = set()
    truncated = False

    for depth in range(max_depth):
        remaining = _MAX_VISIBLE_EDGES - len(seen_edges)
        rows, edge_truncated = _frontier_links(
            scope, frontier, direction, remaining, seen_edges)
        truncated = truncated or edge_truncated
        next_frontier: set[str] = set()
        for link in rows:
            if link.id in seen_edges:
                continue
            seen_edges.add(link.id)
            if direction in {"both", "outgoing"} and link.source_object_id in frontier:
                target_known = link.target_object_id in depths
                if target_known or len(depths) < _MAX_VISIBLE_NODES:
                    adjacency[link.source_object_id].append((link.target_object_id, link, "out"))
                else:
                    truncated = True
                if not target_known and len(depths) < _MAX_VISIBLE_NODES:
                    depths[link.target_object_id] = depth + 1
                    next_frontier.add(link.target_object_id)
            if direction in {"both", "incoming"} and link.target_object_id in frontier:
                source_known = link.source_object_id in depths
                if source_known or len(depths) < _MAX_VISIBLE_NODES:
                    adjacency[link.target_object_id].append((link.source_object_id, link, "in"))
                else:
                    truncated = True
                if not source_known and len(depths) < _MAX_VISIBLE_NODES:
                    depths[link.source_object_id] = depth + 1
                    next_frontier.add(link.source_object_id)
        if len(depths) >= _MAX_VISIBLE_NODES:
            truncated = True
            break
        if stop_id and stop_id in next_frontier:
            break
        if not next_frontier:
            break
        frontier = next_frontier
    return adjacency, depths, truncated


def _path_payload(
    nodes: list[str],
    traversals: list[tuple[LinkInstance, str]],
) -> dict:
    return {
        "nodeIds": nodes,
        "edgeIds": [link.id for link, _ in traversals],
        "steps": [
            {"linkInstanceId": link.id, "linkTypeId": link.link_type_id, "direction": direction}
            for link, direction in traversals
        ],
        "hops": len(traversals),
    }


def _instances_by_ids(scope: AgentScope, ids: Iterable[str]) -> dict[str, ObjectInstance]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        return {}
    rows = (_instance_query(scope).filter(ObjectInstance.id.in_(unique)).all())
    return {row.id: row for row in rows if row.object_type_id in scope.object_types}


def find_paths(
    scope: AgentScope,
    source_instance_id: str,
    target_instance_id: str,
    *,
    direction: str = "both",
    max_depth: int = 5,
    max_paths: int = 3,
) -> dict:
    source = _visible_instance(scope, source_instance_id)
    target = _visible_instance(scope, target_instance_id)
    _require_materialized_instance(scope, source)
    _require_materialized_instance(scope, target)
    depth_limit = _clamp(max_depth, 1, _MAX_DEPTH, 5)
    path_limit = _clamp(max_paths, 1, _MAX_PATHS, 3)

    if source.id == target.id:
        paths = [_path_payload([source.id], [])]
        adjacency: dict[str, list[tuple[str, LinkInstance, str]]] = {}
        truncated = False
    else:
        adjacency, _, truncated = _expand_graph(
            scope, source.id, direction=direction, max_depth=depth_limit, stop_id=target.id)
        paths = []
        queue = deque([(source.id, [source.id], [])])
        while queue and len(paths) < path_limit:
            current, node_path, edge_path = queue.popleft()
            if len(edge_path) >= depth_limit:
                continue
            for neighbor, link, traversal_direction in adjacency.get(current, []):
                if neighbor in node_path:
                    continue
                next_nodes = [*node_path, neighbor]
                next_edges = [*edge_path, (link, traversal_direction)]
                if neighbor == target.id:
                    paths.append(_path_payload(next_nodes, next_edges))
                else:
                    queue.append((neighbor, next_nodes, next_edges))

    node_ids = [node_id for path in paths for node_id in path["nodeIds"]]
    instances = _instances_by_ids(scope, node_ids)
    edge_ids = {edge_id for path in paths for edge_id in path["edgeIds"]}
    link_rows = []
    if edge_ids:
        link_rows = (_link_query(scope).filter(LinkInstance.id.in_(edge_ids)).all())
    return {
        "kind": "path",
        "sourceInstanceId": source.id,
        "targetInstanceId": target.id,
        "sourceLabel": instance_label(scope, source),
        "targetLabel": instance_label(scope, target),
        "direction": direction,
        "maxDepth": depth_limit,
        "paths": paths,
        "nodes": [_instance_node(scope, instances[node_id])
                  for node_id in dict.fromkeys(node_ids) if node_id in instances],
        "edges": [_link_edge(scope, link) for link in link_rows
                  if link.link_type_id in scope.link_types],
        "found": bool(paths),
        "truncated": truncated,
    }


def _parent_paths(
    start_id: str,
    adjacency: dict[str, list[tuple[str, LinkInstance, str]]],
    depth_limit: int,
) -> tuple[dict[str, int], dict[str, tuple[str, LinkInstance, str]]]:
    depths = {start_id: 0}
    parents: dict[str, tuple[str, LinkInstance, str]] = {}
    queue = deque([start_id])
    while queue:
        current = queue.popleft()
        if depths[current] >= depth_limit:
            continue
        for neighbor, link, traversal_direction in adjacency.get(current, []):
            if neighbor in depths:
                continue
            depths[neighbor] = depths[current] + 1
            parents[neighbor] = (current, link, traversal_direction)
            queue.append(neighbor)
    return depths, parents


def _reconstruct_parent_path(
    start_id: str,
    node_id: str,
    parents: dict[str, tuple[str, LinkInstance, str]],
) -> dict:
    nodes = [node_id]
    edges: list[tuple[LinkInstance, str]] = []
    current = node_id
    while current != start_id and current in parents:
        parent, link, direction = parents[current]
        nodes.append(parent)
        edges.append((link, direction))
        current = parent
    nodes.reverse()
    edges.reverse()
    return _path_payload(nodes, edges)


def analyze_change_impact(
    scope: AgentScope,
    instance_id: str,
    property_ref: str,
    proposed_value: Any,
    *,
    direction: str = "both",
    max_depth: int = 3,
) -> dict:
    source = _visible_instance(scope, instance_id)
    _require_materialized_instance(scope, source)
    object_type = scope.object_types[source.object_type_id]
    property_name = scope.resolve_property(object_type, property_ref)
    depth_limit = _clamp(max_depth, 1, 4, 3)
    current_values = {**(source.properties or {}), **(source.computed or {})}

    adjacency, _, truncated = _expand_graph(
        scope, source.id, direction=direction, max_depth=depth_limit)
    depths, parents = _parent_paths(source.id, adjacency, depth_limit)
    affected_ids = [node_id for node_id, depth in depths.items() if depth > 0]
    all_ids = [source.id, *affected_ids]
    instances = _instances_by_ids(scope, all_ids)

    impact_items = []
    used_edge_ids: set[str] = set()
    for node_id in sorted(affected_ids, key=lambda item: (depths[item], item)):
        path = _reconstruct_parent_path(source.id, node_id, parents)
        used_edge_ids.update(path["edgeIds"])
        instance = instances.get(node_id)
        if not instance:
            continue
        impact_items.append({
            "instanceId": node_id,
            "label": instance_label(scope, instance),
            "objectType": scope.object_types[instance.object_type_id].display_name,
            "depth": depths[node_id],
            "classification": "direct" if depths[node_id] == 1 else "indirect",
            "certainty": "related",
            "path": path,
        })

    links = []
    if used_edge_ids:
        links = (_link_query(scope).filter(LinkInstance.id.in_(used_edge_ids)).all())
    direct_count = sum(1 for item in impact_items if item["classification"] == "direct")
    return {
        "kind": "impact",
        "mode": "association_only",
        "change": {
            "instanceId": source.id,
            "instanceLabel": instance_label(scope, source),
            "objectType": object_type.display_name,
            "property": property_name,
            "propertyLabel": next((
                prop.get("displayName") or prop.get("display_name") or prop.get("name")
                for prop in object_type.properties or []
                if isinstance(prop, dict) and prop.get("name") == property_name
            ), property_name),
            "currentValue": current_values.get(property_name),
            "proposedValue": proposed_value,
        },
        "direction": direction,
        "maxDepth": depth_limit,
        "summary": {
            "related": len(impact_items),
            "direct": direct_count,
            "indirect": len(impact_items) - direct_count,
        },
        "impacts": impact_items,
        "nodes": [_instance_node(scope, instances[node_id])
                  for node_id in all_ids if node_id in instances],
        "edges": [_link_edge(scope, link) for link in links
                  if link.link_type_id in scope.link_types],
        "truncated": truncated,
        "disclaimer": "当前结果表示关系可达范围，不等同于确定的业务因果；真实写入尚未发生。",
    }
