"""Neo4j 图数据库服务"""
from __future__ import annotations
import json
import logging
import time
from base64 import b64decode, b64encode
from typing import Any

logger = logging.getLogger(__name__)

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None  # type: ignore

# 连接失败后多少秒内不再重试（避免每个请求都白等连接超时）
_RETRY_INTERVAL = 60.0
_JSON_FIELDS_KEY = "__ontology_json_fields"
_JSON_VALUE_PREFIX = "__ONTOLOGY_JSON_V1__:"
_ESCAPED_STRING_PREFIX = "__ONTOLOGY_STRING_V1__:"
_NEO4J_INT64_MIN = -(1 << 63)
_NEO4J_INT64_MAX = (1 << 63) - 1


def _encode_properties(properties: dict) -> dict:
    """Convert nested SQL JSON values to Neo4j-supported property values."""
    encoded: dict[str, Any] = {}
    for raw_key, value in dict(properties or {}).items():
        key = str(raw_key)
        if isinstance(value, str):
            if value.startswith((_JSON_VALUE_PREFIX, _ESCAPED_STRING_PREFIX)):
                encoded[key] = _ESCAPED_STRING_PREFIX + b64encode(
                    value.encode("utf-8"),
                ).decode("ascii")
            else:
                encoded[key] = value
            continue
        if value is None or isinstance(value, (float, bool)):
            encoded[key] = value
            continue
        if isinstance(value, int):
            # Neo4j integers are signed 64-bit. Preserve an oversized SQL JSON
            # integer as text instead of letting the driver reject the entire
            # projection rebuild.
            encoded[key] = (
                value
                if _NEO4J_INT64_MIN <= value <= _NEO4J_INT64_MAX
                else str(value)
            )
            continue
        encoded[key] = _JSON_VALUE_PREFIX + json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        )
    return encoded


def _decode_properties(properties: dict) -> dict:
    decoded = dict(properties or {})
    # Read compatibility for projections written by the short-lived metadata
    # encoding before every upgraded ontology is synchronously rebuilt.
    legacy_fields = decoded.get(_JSON_FIELDS_KEY)
    if isinstance(legacy_fields, list):
        decoded.pop(_JSON_FIELDS_KEY, None)
    else:
        legacy_fields = []
    for key in legacy_fields:
        value = decoded.get(key)
        if not isinstance(value, str):
            continue
        try:
            decoded[key] = json.loads(value)
        except (TypeError, ValueError):
            raise RuntimeError(
                f"Neo4j projection property {key!r} contains invalid JSON"
            )
    for key, value in list(decoded.items()):
        if not isinstance(value, str):
            continue
        try:
            if value.startswith(_JSON_VALUE_PREFIX):
                decoded[key] = json.loads(value[len(_JSON_VALUE_PREFIX):])
            elif value.startswith(_ESCAPED_STRING_PREFIX):
                decoded[key] = b64decode(
                    value[len(_ESCAPED_STRING_PREFIX):],
                    validate=True,
                ).decode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RuntimeError(
                f"Neo4j projection property {key!r} has invalid type encoding"
            ) from exc
    return decoded


def _port_open(uri: str, timeout: float = 0.5) -> bool:
    """快速探测 bolt URI 的 host:port 是否可连。localhost 强制走 IPv4 避免
    Windows 上 IPv6(::1) 解析拖慢到 1.6s+。"""
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 7687
        if host in ("localhost", "::1"):
            host = "127.0.0.1"
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


class Neo4jService:
    """Neo4j 连接与 CRUD 服务

    使用默认配置时全进程共享同一个 driver（neo4j driver 线程安全），
    避免每个请求重建连接；连接失败后 60 秒内直接判定不可用。
    """

    _shared_driver = None
    _shared_unavailable_until: float = 0.0

    @classmethod
    def clear_unavailable_backoff(cls) -> None:
        """Let the next business request reconnect after a successful probe."""
        cls._shared_unavailable_until = 0.0

    def __init__(self, uri: str | None = None, user: str | None = None, password: str | None = None):
        from app.config import settings
        self._is_default_config = uri is None and user is None and password is None
        self._uri = uri or settings.neo4j_uri
        self._user = user or settings.neo4j_user
        self._password = password or settings.neo4j_password
        self._driver = None
        self._available = False
        self._init_driver()

    def _init_driver(self):
        cls = Neo4jService
        if self._is_default_config:
            if cls._shared_driver is not None:
                self._driver = cls._shared_driver
                self._available = True
                return
            if time.monotonic() < cls._shared_unavailable_until:
                return
        try:
            if GraphDatabase is None:
                raise RuntimeError("neo4j package not installed")
            # The socket probe is an optimisation for the application-wide
            # default connection only.  Explicit connection parameters are
            # commonly used by connection tests and may be handled by a custom
            # driver/resolver, so the Neo4j driver remains the source of truth.
            if self._is_default_config and not _port_open(self._uri):
                raise RuntimeError("Neo4j port not reachable")
            driver = GraphDatabase.driver(
                self._uri, auth=(self._user, self._password),
                connection_timeout=3.0,
            )
            driver.verify_connectivity()
            self._driver = driver
            self._available = True
            if self._is_default_config:
                cls._shared_driver = driver
            logger.info("Neo4j connected")
        except Exception as e:
            logger.warning(f"Neo4j unavailable: {e}")
            self._available = False
            if self._is_default_config:
                cls._shared_unavailable_until = time.monotonic() + _RETRY_INTERVAL

    @property
    def available(self) -> bool:
        return self._available

    def close(self):
        # 共享 driver 不在此关闭（其他请求仍在使用）
        if self._driver is not None and self._driver is not Neo4jService._shared_driver:
            self._driver.close()

    # ── 写入 ────────────────────────────────────────────────────────

    def upsert_entity(self, label: str, props: dict, key_field: str = "id") -> str | None:
        """实体 MERGE — 存在则更新, 不存在则创建"""
        if not self._available:
            return None
        from app.ontologies.graph.cypher_builder import validate_label

        label = validate_label(label)
        key_field = validate_label(key_field)
        query = f"""
        MERGE (n:{label} {{{key_field}: $key}})
        SET n += $props,
            n.updated_at = coalesce($props.updated_at, datetime())
        RETURN elementId(n) AS eid
        """
        encoded_props = _encode_properties(props)
        with self._driver.session() as session:
            result = session.run(
                query,
                key=encoded_props.get(key_field),
                props=encoded_props,
            )
            record = result.single()
            return record["eid"] if record else None

    def upsert_relation(self, src_label: str, src_key: str, tgt_label: str, tgt_key: str,
                        rel_type: str, props: dict | None = None, key_field: str = "id") -> bool:
        """关系 MERGE"""
        if not self._available:
            return False
        from app.ontologies.graph.cypher_builder import validate_label

        src_label = validate_label(src_label)
        tgt_label = validate_label(tgt_label)
        rel_type = validate_label(rel_type)
        key_field = validate_label(key_field)
        relationship_props = _encode_properties(props or {})
        relationship_id = relationship_props.get("id")
        merge_clause = (
            f"MERGE (s)-[r:{rel_type} {{id: $relationship_id}}]->(t)"
            if relationship_id is not None
            else f"MERGE (s)-[r:{rel_type}]->(t)"
        )
        query = f"""
        MATCH (s:{src_label} {{{key_field}: $src_key}})
        MATCH (t:{tgt_label} {{{key_field}: $tgt_key}})
        {merge_clause}
        SET r += $props,
            r.updated_at = coalesce($props.updated_at, datetime())
        RETURN r
        """
        with self._driver.session() as session:
            result = session.run(
                query,
                src_key=src_key,
                tgt_key=tgt_key,
                relationship_id=relationship_id,
                props=relationship_props,
            )
            return result.single() is not None

    def batch_upsert_entities(
        self,
        label: str,
        entities: list[dict],
        key_field: str = "id",
        replace_properties: bool = False,
    ) -> int:
        """批量 MERGE — 每批 1000 条。

        默认保持已有节点的未提交属性；派生投影可显式选择以当前
        权威行完整替换属性，避免已删业务字段滞留在图中。
        """
        if not self._available or not entities:
            return 0
        from app.ontologies.graph.cypher_builder import validate_label

        label = validate_label(label)
        key_field = validate_label(key_field)
        property_assignment = (
            "SET n = e.props"
            if replace_properties
            else "SET n += e.props"
        )
        query = f"""
        UNWIND $batch AS e
        MERGE (n:{label} {{{key_field}: e.key}})
        {property_assignment}
        SET n.updated_at = coalesce(e.props.updated_at, datetime())
        RETURN count(n) AS written
        """
        count = 0
        chunk_size = 1000
        with self._driver.session() as session:
            for i in range(0, len(entities), chunk_size):
                chunk = entities[i:i + chunk_size]
                batch = []
                for entity in chunk:
                    encoded = _encode_properties(entity)
                    batch.append({
                        "key": encoded.get(key_field),
                        "props": encoded,
                    })
                record = session.run(query, batch=batch).single()
                count += int(record["written"] if record else 0)
        return count

    def batch_upsert_relations(
        self, src_label: str, tgt_label: str, rel_type: str,
        relations: list[dict], key_field: str = "id",
    ) -> int:
        """UNWIND bounded relationship batches and report actual matched writes.

        Stable relationship IDs preserve parallel links; a missing endpoint
        reduces the write count so the projection caller can fail closed.
        """
        if not self._available or not relations:
            return 0
        from app.ontologies.graph.cypher_builder import validate_label

        src_label = validate_label(src_label)
        tgt_label = validate_label(tgt_label)
        rel_type = validate_label(rel_type)
        key_field = validate_label(key_field)
        query = f"""
        UNWIND $batch AS e
        MATCH (s:{src_label} {{{key_field}: e.src_key}})
        MATCH (t:{tgt_label} {{{key_field}: e.tgt_key}})
        MERGE (s)-[r:{rel_type} {{id: e.props.id}}]->(t)
        SET r += e.props,
            r.updated_at = coalesce(e.props.updated_at, datetime())
        RETURN count(r) AS written
        """
        count = 0
        with self._driver.session() as session:
            for offset in range(0, len(relations), 1000):
                batch = []
                for relation in relations[offset:offset + 1000]:
                    props = _encode_properties(relation["props"])
                    if props.get("id") is None:
                        raise ValueError("Batch relationships require a stable id")
                    batch.append({
                        "src_key": relation["src_key"],
                        "tgt_key": relation["tgt_key"], "props": props,
                    })
                record = session.run(query, batch=batch).single()
                count += int(record["written"] if record else 0)
        return count

    def batch_delete_entities(self, ontology_id: str, entity_ids: list[str]) -> int:
        """Delete stale materialized nodes by stable source id (relationships cascade)."""
        if not self._available or not entity_ids:
            return 0
        query = """
        UNWIND $ids AS entity_id
        MATCH (n {ontology_id: $ontology_id, id: entity_id})
        DETACH DELETE n
        RETURN count(n) AS deleted
        """
        with self._driver.session() as session:
            record = session.run(
                query, ontology_id=ontology_id, ids=list(entity_ids)).single()
            return int(record["deleted"] if record else 0)

    # ── 读取 ────────────────────────────────────────────────────────

    def run_cypher(self, query: str, params: dict | None = None) -> list[dict]:
        """执行 Cypher 查询"""
        if not self._available:
            return []
        with self._driver.session() as session:
            result = session.run(query, **(params or {}))
            return [dict(record) for record in result]

    def get_graph_data(self, ontology_id: str, limit: int = 200,
                       label_filter: str | None = None) -> dict:
        """返回图谱可视化用节点/边数据 (分两步查询避免 LIMIT 吞掉边)"""
        if not self._available:
            return {"nodes": [], "edges": []}

        with self._driver.session() as session:
            # Step 1: 获取节点
            node_query = """
            MATCH (n:OntologyEntity)
            WHERE n.ontology_id = $ontology_id
              AND ($type_filter IS NULL OR n.type = $type_filter)
            RETURN n
            LIMIT $limit
            """
            nodes_map = {}
            node_result = session.run(
                node_query,
                ontology_id=ontology_id,
                type_filter=label_filter,
                limit=limit,
            )
            for record in node_result:
                nd = record.get("n")
                if nd:
                    properties = _decode_properties(dict(nd))
                    nid = properties.get("id")
                    if not nid:
                        raise RuntimeError(
                            "Neo4j projection node is missing stable id; rebuild required"
                        )
                    nid = str(nid)
                    nodes_map[nid] = {
                        "id": nid,
                        "labels": list(nd.labels),
                        "properties": properties,
                    }

            # Step 2: 获取这些节点之间的边
            if nodes_map:
                edge_query = """
                MATCH (n)-[r]->(m)
                WHERE n.ontology_id = $ontology_id AND m.ontology_id = $ontology_id
                RETURN r, n, m
                LIMIT 1000
                """
                edges = []
                node_id_set = set(nodes_map.keys())
                edge_result = session.run(edge_query, ontology_id=ontology_id)
                for record in edge_result:
                    r = record.get("r")
                    n2 = record.get("n")
                    m2 = record.get("m")
                    if r and n2 and m2:
                        n_props = _decode_properties(dict(n2))
                        m_props = _decode_properties(dict(m2))
                        source_id = n_props.get("id")
                        target_id = m_props.get("id")
                        rel_props = _decode_properties(dict(r))
                        relationship_id = rel_props.get("id")
                        if not source_id or not target_id or not relationship_id:
                            raise RuntimeError(
                                "Neo4j projection relationship is missing stable ids; rebuild required"
                            )
                        source_id = str(source_id)
                        target_id = str(target_id)
                        relationship_id = str(relationship_id)
                        if source_id in node_id_set or target_id in node_id_set:
                            if source_id not in nodes_map:
                                nodes_map[source_id] = {
                                    "id": source_id,
                                    "labels": list(n2.labels),
                                    "properties": n_props,
                                }
                            if target_id not in nodes_map:
                                nodes_map[target_id] = {
                                    "id": target_id,
                                    "labels": list(m2.labels),
                                    "properties": m_props,
                                }
                            edges.append({
                                "id": relationship_id,
                                "source": source_id,
                                "target": target_id,
                                "type": rel_props.get("semantic_type") or r.type,
                                "properties": rel_props,
                            })
            else:
                edges = []

        return {"nodes": list(nodes_map.values()), "edges": edges}

    def delete_by_ontology(self, ontology_id: str) -> int:
        """Delete in bounded transactions, including high-degree relationships."""
        if not self._available:
            return 0
        # Delete edges separately so a single high-degree node cannot make one
        # DETACH DELETE transaction retain millions of relationship deletions.
        edge_query = """
        MATCH (n {ontology_id: $ontology_id})-[r]-()
        WITH DISTINCT r LIMIT $batch_size
        DELETE r
        RETURN count(r) AS deleted
        """
        node_query = """
        MATCH (n {ontology_id: $ontology_id})
        WITH n LIMIT $batch_size
        DELETE n
        RETURN count(n) AS deleted
        """
        count = 0
        with self._driver.session() as session:
            for query in (edge_query, node_query):
                while True:
                    record = session.run(
                        query, ontology_id=ontology_id, batch_size=1000,
                    ).single()
                    deleted = int(record["deleted"] if record else 0)
                    if query == node_query:
                        count += deleted
                    if deleted == 0:
                        break
        return count
