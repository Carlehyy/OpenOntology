"""Neo4j 索引初始化 — 提升查询性能"""
from __future__ import annotations
import logging
from app.ontologies.graph.neo4j_service import Neo4jService

logger = logging.getLogger(__name__)

# 核心索引定义
INDEXES = [
    # 全量重建只写 OntologyEntity；projection_key 是跨本体安全的 MERGE 键。
    "CREATE INDEX ontology_entity_projection_key IF NOT EXISTS FOR (n:OntologyEntity) ON (n.projection_key)",
    # 按 ontology_id 过滤单个本体。
    "CREATE INDEX ontology_entity_ontology_id IF NOT EXISTS FOR (n:OntologyEntity) ON (n.ontology_id)",
    # id 是对外稳定身份，只在本体内唯一。
    "CREATE INDEX ontology_entity_id IF NOT EXISTS FOR (n:OntologyEntity) ON (n.id)",
    # 按名称搜索（关键词查找）
    "CREATE INDEX ontology_entity_name_cn IF NOT EXISTS FOR (n:OntologyEntity) ON (n.name_cn)",
    # label_filter 的对外语义是业务对象类型，实际按 type 属性过滤。
    "CREATE INDEX ontology_entity_type IF NOT EXISTS FOR (n:OntologyEntity) ON (n.type)",
]

# Neo4j 的属性范围索引必须指定节点标签。不能用
# ``FOR (n) ON (n.ontology_id)`` 创建跨标签属性索引；无标签语法只适用于
# ``ON EACH labels(n)`` 的 token lookup index，而该索引不会加速属性过滤。

# 约束定义（唯一性）
CONSTRAINTS = [
    # 同一本体内 source_row_key 唯一（防止重复 MERGE）
    # "CREATE CONSTRAINT entity_unique IF NOT EXISTS FOR (n:Entity) REQUIRE (n.ontology_id, n.id) IS UNIQUE",
    # 注：Neo4j Community 版不支持复合约束，暂时注释
]


def setup_indexes(neo4j: Neo4jService | None = None) -> dict:
    """
    执行所有索引创建语句。
    幂等操作（IF NOT EXISTS），重复执行无副作用。
    """
    svc = neo4j or Neo4jService()
    if not svc.available:
        logger.warning("Neo4j 不可用，跳过索引初始化")
        return {"status": "skipped", "reason": "neo4j_unavailable"}

    results = []
    for stmt in INDEXES:
        try:
            svc.run_cypher(stmt)
            results.append({"index": stmt[:60] + "...", "status": "ok"})
            logger.info(f"索引创建成功: {stmt[:60]}...")
        except Exception as e:
            results.append({"index": stmt[:60] + "...", "status": "error", "error": str(e)})
            logger.warning(f"索引创建失败（可能已存在）: {e}")

    svc.close()
    return {"status": "done", "results": results, "count": len(results)}
