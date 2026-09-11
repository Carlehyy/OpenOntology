"""记忆宫殿图谱存储（Neo4j，参照 semantica GraphBuilder 的核心语义）。

与本体投影共用同一 Neo4j 实例但互不越界：本体域写 OntologyEntity，
本模块写 PalaceEntity/RELATED，靠标签 + owner_id 属性隔离（平台惯例：
单标签 + 属性命名空间，复合唯一约束交给应用层 MERGE 键）。

实体消解（semantica DuplicateDetector/EntityMerger 的 v1 简化版）：
merge_key = owner_id + 规范名（折叠空白、casefold），同键 MERGE 自然合并；
每个节点/关系带 file_ids + source_files 溯源列表，文件删除/重建时剥离
该文件来源，来源清空的节点与关系随之清理（图随文件库演化，不残留孤儿）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_NODE_LABEL = "PalaceEntity"
_REL_TYPE = "RELATED"

# 本体发布文档的图谱系统作用域：owner_id 位置使用该哨兵（users.id 均为
# UUID，字符串哨兵不可能与真实用户碰撞），所有 Cypher 沿用同一 owner
# 过滤口径。单一共享作用域意味着跨本体同名实体在此层自然合并——这正是
# 「本体知识层」作为一份平台共享图谱的语义。
ONTOLOGY_DOCUMENTS_SCOPE = "__ontology_documents__"

_WS_RE = re.compile(r"\s+")


class PalaceGraphUnavailable(RuntimeError):
    """Neo4j 不可用：抽取与检索不可用，但不得阻断对话主链路。"""


def normalize_name(name: str) -> str:
    """实体规范名：折叠空白、去掉首尾标点、casefold（拉丁归一，CJK 原样）。"""
    cleaned = _WS_RE.sub(" ", str(name or "")).strip().strip("，。；：！？、,.;:!?“”\"'()（）")
    return cleaned.casefold()


def _dedupe_str_list(items: Any) -> list[str]:
    """读取侧列表去重（保序）：merge_entities 合并允许列表内重复条目，
    owner_graph/search 返回前在 Python 侧收敛，前端不做去重逻辑。"""
    return list(dict.fromkeys(str(item) for item in items or [] if str(item)))


def entity_key(owner_id: str, name: str) -> str:
    return f"{owner_id}:{normalize_name(name)}"


def relation_key(owner_id: str, source: str, relation: str, target: str) -> str:
    return "|".join((
        owner_id, normalize_name(source), normalize_name(relation), normalize_name(target),
    ))


def _service():
    from app.ontologies.graph.neo4j_service import Neo4jService

    service = Neo4jService()
    if not service.available:
        service.close()
        raise PalaceGraphUnavailable("Neo4j 不可用，记忆宫殿图谱暂不可用")
    return service


def merge_extraction(
    owner_id: str,
    file_id: str,
    filename: str,
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> tuple[int, int]:
    """把一次抽取结果 MERGE 进用户图谱；返回（合并实体数, 关系数）。

    entities: [{key, name, type, aliases, mentions}]；relations:
    [{key, src_key, tgt_key, name}]。端点未先落库的 relation 直接丢弃。
    """
    service = _service()
    merged_entities = 0
    merged_relations = 0
    try:
        if entities:
            service.run_cypher(
                f"""
                UNWIND $items AS e
                MERGE (n:{_NODE_LABEL} {{merge_key: e.key}})
                ON CREATE SET
                  n.created_at = datetime(), n.owner_id = $owner_id,
                  n.name = e.name, n.type = e.type, n.aliases = e.aliases,
                  n.source_files = [$filename], n.file_ids = [$file_id]
                FOREACH (_ IN CASE WHEN $file_id IN coalesce(n.file_ids, []) THEN [] ELSE [1] END |
                  SET n.file_ids = coalesce(n.file_ids, []) + $file_id,
                      n.source_files = coalesce(n.source_files, []) + $filename
                )
                SET n.mention_count = coalesce(n.mention_count, 0) + e.mentions,
                    n.updated_at = datetime()
                """,
                {
                    "items": entities,
                    "owner_id": owner_id,
                    "file_id": file_id,
                    "filename": filename,
                },
            )
            merged_entities = len(entities)
        if relations:
            service.run_cypher(
                f"""
                UNWIND $items AS r
                MATCH (s:{_NODE_LABEL} {{merge_key: r.src_key}})
                MATCH (t:{_NODE_LABEL} {{merge_key: r.tgt_key}})
                MERGE (s)-[rel:{_REL_TYPE} {{merge_key: r.key}}]->(t)
                FOREACH (_ IN CASE WHEN $file_id IN coalesce(rel.file_ids, []) THEN [] ELSE [1] END |
                  SET rel.file_ids = coalesce(rel.file_ids, []) + $file_id,
                      rel.source_files = coalesce(rel.source_files, []) + $filename
                )
                SET rel.name = r.name, rel.owner_id = $owner_id, rel.updated_at = datetime()
                """,
                {
                    "items": relations,
                    "owner_id": owner_id,
                    "file_id": file_id,
                    "filename": filename,
                },
            )
            merged_relations = len(relations)
    finally:
        service.close()
    return merged_entities, merged_relations


def remove_file_graph(owner_id: str, file_id: str, filename: str) -> None:
    """删除/重建文件前剥离其图谱贡献：先清边，再清点（DETACH 删空点）。"""
    service = _service()
    try:
        service.run_cypher(
            f"""
            MATCH ()-[r:{_REL_TYPE}]->()
            WHERE r.owner_id = $owner_id AND $file_id IN coalesce(r.file_ids, [])
            WITH r,
                 [f IN coalesce(r.file_ids, []) WHERE f <> $file_id] AS keep_ids,
                 [s IN coalesce(r.source_files, []) WHERE s <> $filename] AS keep_files
            SET r.file_ids = keep_ids, r.source_files = keep_files
            WITH r WHERE size(coalesce(r.file_ids, [])) = 0
            DELETE r
            """,
            {"owner_id": owner_id, "file_id": file_id, "filename": filename},
        )
        service.run_cypher(
            f"""
            MATCH (n:{_NODE_LABEL} {{owner_id: $owner_id}})
            WHERE $file_id IN coalesce(n.file_ids, [])
            WITH n,
                 [f IN coalesce(n.file_ids, []) WHERE f <> $file_id] AS keep_ids,
                 [s IN coalesce(n.source_files, []) WHERE s <> $filename] AS keep_files
            SET n.file_ids = keep_ids, n.source_files = keep_files
            WITH n WHERE size(coalesce(n.file_ids, [])) = 0
            DETACH DELETE n
            """,
            {"owner_id": owner_id, "file_id": file_id, "filename": filename},
        )
    finally:
        service.close()


def record_match_counts(owner_id: str, keys: list[str]) -> None:
    """检索命中计数：被选为锚点的实体 match_count +1（效果闭环信号）。

    merge_key 本身携带 owner 前缀，Cypher 无需再按 owner 过滤；调用方
    （search）须自行消化异常——计数失败只记日志，不得影响检索结果。
    """
    if not keys:
        return
    service = _service()
    try:
        service.run_cypher(
            f"""
            UNWIND $keys AS k
            MATCH (n:{_NODE_LABEL} {{merge_key: k}})
            SET n.match_count = coalesce(n.match_count, 0) + 1
            """,
            {"keys": list(keys)},
        )
    finally:
        service.close()
    logger.debug("记忆宫殿命中计数完成（owner=%s，锚点 %d 个）", owner_id, len(keys))


def owner_entities(owner_id: str, limit: int = 2000) -> list[dict[str, Any]]:
    """该用户全部实体的精简行（merge_key/name/type/aliases/计数），供
    consolidate 候选检测离线拉取；按提及数降序，limit 保护超大图。"""
    service = _service()
    try:
        return service.run_cypher(
            f"""
            MATCH (n:{_NODE_LABEL} {{owner_id: $owner_id}})
            WITH n ORDER BY coalesce(n.mention_count, 0) DESC, n.name ASC
            LIMIT $limit
            RETURN n.merge_key AS merge_key, n.name AS name, n.type AS type,
                   coalesce(n.aliases, []) AS aliases,
                   coalesce(n.source_files, []) AS source_files,
                   coalesce(n.mention_count, 0) AS mention_count
            """,
            {"owner_id": owner_id, "limit": int(limit)},
        )
    finally:
        service.close()


def merge_entities(owner_id: str, canonical_key: str, absorbed_keys: list[str]) -> int:
    """把 absorbed 实体并入 canonical 实体；返回实际吸收的实体数。

    semantica EntityMerger 的 Neo4j 简化契约，三条语句按序执行：

    1. 属性合并：absorbed 的 name 入 aliases、mention/match 计数累加、
       溯源列表（file_ids/source_files）拼接——列表内允许重复条目，
       读取侧（owner_graph/search）由 _dedupe_str_list 收敛；
    2. 边重定向：absorbed 上的 RELATED 复制为 canonical 出边（SET nr =
       properties(r) 保留全部属性）；与已有边重复可接受，查询侧 DISTINCT；
    3. DETACH DELETE absorbed 节点。
    """
    keys = [key for key in absorbed_keys if key and key != canonical_key]
    if not keys:
        return 0
    params = {"owner_id": owner_id, "canonical": canonical_key, "absorbed": keys}
    service = _service()
    try:
        merged = service.run_cypher(
            f"""
            MATCH (a:{_NODE_LABEL} {{merge_key: $canonical}})
            MATCH (b:{_NODE_LABEL}) WHERE b.merge_key IN $absorbed
            SET a.aliases = [x IN coalesce(a.aliases, []) + [b.name] + coalesce(b.aliases, [])
                             WHERE x IS NOT NULL AND x <> a.name],
                a.mention_count = coalesce(a.mention_count, 0) + coalesce(b.mention_count, 0),
                a.match_count = coalesce(a.match_count, 0) + coalesce(b.match_count, 0),
                a.file_ids = [x IN coalesce(a.file_ids, []) + coalesce(b.file_ids, [])
                              WHERE x IS NOT NULL],
                a.source_files = [x IN coalesce(a.source_files, []) + coalesce(b.source_files, [])
                                  WHERE x IS NOT NULL],
                a.updated_at = datetime()
            RETURN count(b) AS c
            """,
            params,
        )
        service.run_cypher(
            f"""
            MATCH (a:{_NODE_LABEL} {{merge_key: $canonical}})
            MATCH (b:{_NODE_LABEL}) WHERE b.merge_key IN $absorbed
            MATCH (b)-[r:{_REL_TYPE}]-(other:{_NODE_LABEL})
            WHERE other.merge_key <> $canonical
            CREATE (a)-[nr:{_REL_TYPE}]->(other)
            SET nr = properties(r)
            """,
            params,
        )
        service.run_cypher(
            f"""
            MATCH (b:{_NODE_LABEL}) WHERE b.merge_key IN $absorbed
            DETACH DELETE b
            """,
            params,
        )
    finally:
        service.close()
    return int((merged or [{}])[0].get("c") or 0)


def owner_graph(owner_id: str, *, node_limit: int = 300) -> dict[str, Any]:
    """图谱可视化数据：按提及数取前 N 节点，再取这些节点之间的边。"""
    service = _service()
    try:
        nodes = service.run_cypher(
            f"""
            MATCH (n:{_NODE_LABEL} {{owner_id: $owner_id}})
            WITH n ORDER BY coalesce(n.mention_count, 0) DESC, n.name ASC
            LIMIT $limit
            RETURN n.merge_key AS id, n.name AS name, n.type AS type,
                   coalesce(n.aliases, []) AS aliases,
                   coalesce(n.source_files, []) AS source_files,
                   coalesce(n.file_ids, []) AS file_ids,
                   coalesce(n.mention_count, 0) AS mention_count,
                   coalesce(n.match_count, 0) AS match_count
            """,
            {"owner_id": owner_id, "limit": int(node_limit)},
        )
        entity_total = service.run_cypher(
            f"MATCH (n:{_NODE_LABEL} {{owner_id: $owner_id}}) RETURN count(n) AS c",
            {"owner_id": owner_id},
        )
        relation_total = service.run_cypher(
            f"MATCH ()-[r:{_REL_TYPE} {{owner_id: $owner_id}}]->() RETURN count(r) AS c",
            {"owner_id": owner_id},
        )
        edges: list[dict[str, Any]] = []
        if nodes:
            ids = [node["id"] for node in nodes]
            edges = service.run_cypher(
                f"""
                MATCH (s:{_NODE_LABEL} {{owner_id: $owner_id}})-[r:{_REL_TYPE}]->(t:{_NODE_LABEL})
                WHERE s.merge_key IN $ids AND t.merge_key IN $ids
                RETURN s.merge_key AS source, t.merge_key AS target,
                       r.name AS name, coalesce(r.source_files, []) AS source_files,
                       coalesce(r.file_ids, []) AS file_ids
                LIMIT $edge_limit
                """,
                {"owner_id": owner_id, "ids": ids, "edge_limit": int(node_limit) * 4},
            )
    finally:
        service.close()
    for node in nodes:
        node["source_files"] = _dedupe_str_list(node.get("source_files"))
        node["file_ids"] = _dedupe_str_list(node.get("file_ids"))
    for edge in edges:
        edge["file_ids"] = _dedupe_str_list(edge.get("file_ids"))
    return {
        "nodes": nodes,
        "edges": edges,
        "totals": {
            "entities": int((entity_total or [{}])[0].get("c") or 0),
            "relations": int((relation_total or [{}])[0].get("c") or 0),
        },
        "truncated": bool(nodes) and int((entity_total or [{}])[0].get("c") or 0) > len(nodes),
    }


def search(
    owner_id: str,
    terms: list[str],
    *,
    top_k: int = 5,
    max_scan: int = 2000,
) -> dict[str, Any]:
    """词法检索：锚点实体打分（命中名/别名）+ 一跳邻域展开。

    返回 {"entities": [...], "relations": [...]}；无命中返回空结构。
    扫描上限 max_scan 保护大图（按提及数取前若干）。
    """
    service = _service()
    try:
        rows = service.run_cypher(
            f"""
            MATCH (n:{_NODE_LABEL} {{owner_id: $owner_id}})
            WITH n ORDER BY coalesce(n.mention_count, 0) DESC
            LIMIT $limit
            RETURN n.merge_key AS id, n.name AS name, n.type AS type,
                   coalesce(n.aliases, []) AS aliases,
                   coalesce(n.source_files, []) AS source_files,
                   coalesce(n.file_ids, []) AS file_ids,
                   coalesce(n.match_count, 0) AS match_count
            """,
            {"owner_id": owner_id, "limit": int(max_scan)},
        )
    finally:
        service.close()
    for row in rows:
        row["source_files"] = _dedupe_str_list(row.get("source_files"))
        row["file_ids"] = _dedupe_str_list(row.get("file_ids"))

    def score(row: dict[str, Any]) -> int:
        name = str(row.get("name") or "").casefold()
        aliases = [str(item).casefold() for item in row.get("aliases") or []]
        total = 0
        for term in terms:
            if term in name:
                total += 3
            elif any(term in alias for alias in aliases):
                total += 2
        return total

    anchors = sorted(
        ({"row": row, "score": score(row)} for row in rows),
        key=lambda item: (-item["score"], item["row"].get("name") or ""),
    )
    anchors = [item["row"] for item in anchors if item["score"] > 0][: max(1, int(top_k))]
    if not anchors:
        return {"entities": [], "relations": []}

    # 命中计数是效果闭环信号，属尽力而为：失败只记日志，绝不影响检索结果
    anchor_ids = [row["id"] for row in anchors]
    try:
        record_match_counts(owner_id, anchor_ids)
    except Exception:
        logger.warning("记忆宫殿检索命中计数失败（owner=%s）", owner_id, exc_info=True)

    service = _service()
    try:
        relations = service.run_cypher(
            f"""
            MATCH (s:{_NODE_LABEL} {{owner_id: $owner_id}})-[r:{_REL_TYPE}]-(t:{_NODE_LABEL})
            WHERE s.merge_key IN $ids
            RETURN DISTINCT s.merge_key AS source, t.merge_key AS target,
                            s.name AS source_name, t.name AS target_name,
                            r.name AS name, coalesce(r.source_files, []) AS source_files,
                            coalesce(r.file_ids, []) AS file_ids
            LIMIT 40
            """,
            {"owner_id": owner_id, "ids": anchor_ids},
        )
    finally:
        service.close()
    for relation in relations:
        relation["source_files"] = _dedupe_str_list(relation.get("source_files"))
        relation["file_ids"] = _dedupe_str_list(relation.get("file_ids"))
    neighbor_ids = list(dict.fromkeys(
        [item["source"] for item in relations] + [item["target"] for item in relations]
    ))
    by_id = {row["id"]: row for row in rows}
    entities = [by_id[key] for key in neighbor_ids if key in by_id]
    return {"entities": entities, "relations": relations}
