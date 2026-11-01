"""Release-authoritative ontology structure projection into Neo4j.

SQL 本体定义与 release 快照是唯一权威；本模块把当前 release 快照的结构
（对象类型/属性/链接类型/动作/函数/映射/链接映射）投影为可重建的 Neo4j
探索图。助手的结构消费（``ontology_structure``）继续走 SQL，不经此图。

关键约束：

* 独立 label ``OntologyStructureNode`` + ``kind`` 属性；MERGE 键
  ``structure_key = "{release_id}::{kind}::{local_id}"``，release-scoped、
  多 release 子图并存、重建幂等。属性节点 local_id 带所属对象类型前缀，
  避免不同类型的同名属性在图内合并串权限。
* 信封属性只用 ``structure_ontology_id`` / ``structure_release_id``，刻意
  不写 ``ontology_id``：实例投影的 ``delete_by_ontology`` 按
  ``{ontology_id}`` 无 label 匹配删除，结构节点必须不被其命中；实例图
  ``OntologyEntity`` 不被本模块触碰。
* 只投白名单标量定义字段。绝不序列化 mapping ``fieldMapping``（含
  ``__remote__`` 冻结连接描述符）、函数 ``body``/缓存配置、动作
  ``rules`` 或参数默认值。
* 跨本体 ``AgentJoinAssertion`` 是用户级声明而非全局治理认证，永不入图；
  读侧以 SQL 实时 overlay 呈现。
* 重建失败耐久置 ``failed`` 并抛 ``StructureProjectionError``，不静默
  fallback 假 ready。``commit=False`` 供发布/回滚钩子在调用方事务内复用
  （fence 状态随调用方事务提交；回滚遗留的孤儿图写物由 drift 对账兜底）。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.ontologies.versions.snapshot_contract import snapshot_hash

logger = logging.getLogger(__name__)

STRUCTURE_LABEL = "OntologyStructureNode"
STRUCTURE_KEY_FIELD = "structure_key"
STRUCTURE_DELETE_BATCH = 1000

# 存储状态 empty/building/ready/failed；stale 为读侧派生（ready 但当前
# 发布指针/哈希已变），不落库。
STATUS_EMPTY = "empty"
STATUS_BUILDING = "building"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_STALE = "stale"  # read-side only
# ready 且无漂移但图 meta 不可达/不一致：如实 not-ready，不静默零计数。
STATUS_UNAVAILABLE = "unavailable"  # read-side only

KIND_OBJECT_TYPE = "object_type"
KIND_PROPERTY = "property"
KIND_LINK_TYPE = "link_type"
KIND_ACTION = "action"
KIND_FUNCTION = "function"
KIND_MAPPING = "mapping"
KIND_LINK_MAPPING = "link_mapping"
KIND_META = "release_meta"

REL_LINK_TYPE = "STRUCTURE_LINK_TYPE"
REL_TYPE_PROPERTY = "STRUCTURE_TYPE_PROPERTY"
REL_ACTION_ON_TYPE = "STRUCTURE_ACTION_ON_TYPE"
REL_FUNCTION_REF = "STRUCTURE_FUNCTION_REF"
REL_MAPPING_TARGET = "STRUCTURE_MAPPING_TARGET"
REL_LINK_MAPPING_TARGET = "STRUCTURE_LINK_MAPPING_TARGET"


class StructureProjectionError(RuntimeError):
    """结构投影重建失败（claim 之后的失败：耐久 failed 已写入，绝不假 ready）。"""


class StructureProjectionRejected(StructureProjectionError):
    """请求/上下文层拒绝：SQL 权威判定本次无从投影，fence 不做任何变更。

    典型场景：本体不存在、无有效当前发布指针。此时不存在"投影构建
    失败"——是请求本身不成立，fence 保持原状（旧产物由 drift 对账
    表达 stale/unavailable），不得写入 failed 污染状态。
    """


def _claim_building(db: Session, ontology_id: str) -> None:
    """在 build lock 内声明 building。

    持锁者看到遗留 ``building`` 说明前一持有者已消失（进程死亡/未收尾）：
    锁保证没有并存重建，直接接管恢复，绝不把管理员锁死在永久 building。
    """
    row = _project_row(db, ontology_id)
    previous = str(row.structure_projection_status or STATUS_EMPTY)
    if previous == STATUS_BUILDING:
        logger.warning(
            "接管遗留 structure building fence（前一持有者未收尾）: %s",
            ontology_id)
    row.structure_projection_status = STATUS_BUILDING
    row.structure_projection_error = None


def structure_key(release_id: str, kind: str, local_id: str) -> str:
    return f"{release_id}::{kind}::{local_id}"


# ----------------------------------------------------------- 白名单提取


def _scalar(value: Any) -> Any:
    """白名单字段只允许标量；意外的嵌套值降级为字符串，不透传结构。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _pick(raw: dict, *fields: str) -> dict:
    return {
        key: _scalar(raw.get(key))
        for key in fields
        if raw.get(key) is not None
    }


def _property_nodes(object_type: dict) -> tuple[list[dict], list[dict]]:
    """对象类型属性的白名单投影。

    local_id = "{ownerTypeId}::{propName}"：不同类型的同名属性（如 status）
    是不同节点，授权过滤按 owner+name 判定，公开侧 attributes.objectTypeId
    标注归属，接口 localId 仍返回纯属性名。
    """
    owner_id = str(object_type.get("id") or "")
    type_key = structure_key("{release}", KIND_OBJECT_TYPE, owner_id)
    nodes: list[dict] = []
    edges: list[dict] = []
    for prop in object_type.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name")
        if not name:
            continue
        attributes = _pick(prop, "displayName", "type", "required", "description")
        attributes["objectTypeId"] = owner_id
        attributes["source"] = (
            "computed" if prop.get("source") == "computed" else "stored")
        local_id = f"{owner_id}::{name}"
        nodes.append({
            "kind": KIND_PROPERTY,
            "local_id": local_id,
            "name": str(name),
            "display_name": str(prop.get("displayName") or name),
            "description": _scalar(prop.get("description")),
            "attributes": attributes,
        })
        edges.append({
            "type": REL_TYPE_PROPERTY,
            "source": type_key,
            "target": structure_key("{release}", KIND_PROPERTY, local_id),
            "id": f"{REL_TYPE_PROPERTY}:{owner_id}:{name}",
        })
    return nodes, edges


def _require_endpoint(item_id: str, field: str, value: object) -> str:
    """必填引用缺失即失败：宁可不构建也不生成悬空结构。"""
    if value is None or not str(value).strip():
        raise StructureProjectionError(
            f"快照定义不完整（{item_id} 缺少 {field}），结构投影拒绝构建")
    return str(value)


def extract_structure_graph(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """从 release 快照提取白名单结构节点与边（纯函数，``{release}`` 占位）。

    输入是 ``complete_snapshot`` 归一后的快照。任何生成边的引用都必须
    解析到已存在节点；悬空引用抛错而不是悄悄漏关系。
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    for item in snapshot.get("objectTypes") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        item_id = str(item["id"])
        nodes.append({
            "kind": KIND_OBJECT_TYPE,
            "local_id": item_id,
            "name": str(item.get("name") or item_id),
            "display_name": str(item.get("displayName") or item.get("name") or item_id),
            "description": _scalar(item.get("description")),
            "attributes": _pick(item, "icon", "color", "primaryKey"),
        })
        prop_nodes, prop_edges = _property_nodes(item)
        nodes.extend(prop_nodes)
        edges.extend(prop_edges)

    for item in snapshot.get("linkTypes") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        item_id = str(item["id"])
        source_type = _require_endpoint(
            item_id, "sourceObjectTypeId", item.get("sourceObjectTypeId"))
        target_type = _require_endpoint(
            item_id, "targetObjectTypeId", item.get("targetObjectTypeId"))
        nodes.append({
            "kind": KIND_LINK_TYPE,
            "local_id": item_id,
            "name": str(item.get("name") or item_id),
            "display_name": str(item.get("displayName") or item.get("name") or item_id),
            "description": _scalar(item.get("description")),
            "attributes": _pick(
                item, "cardinality", "sourceRole", "targetRole",
                "sourceObjectTypeId", "targetObjectTypeId"),
        })
        edges.append({
            "type": REL_LINK_TYPE,
            "source": structure_key("{release}", KIND_OBJECT_TYPE, source_type),
            "target": structure_key("{release}", KIND_OBJECT_TYPE, target_type),
            "id": f"{REL_LINK_TYPE}:{item_id}",
        })

    for item in snapshot.get("actions") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        item_id = str(item["id"])
        parameters = []
        for param in item.get("parameters") or []:
            if not isinstance(param, dict) or not param.get("name"):
                continue
            # 只投 name/displayName/type/required；默认值可能嵌引用或秘密。
            parameters.append({
                "name": str(param.get("name")),
                "displayName": str(param.get("displayName") or param.get("name")),
                "type": str(param.get("type") or "string"),
                "required": bool(param.get("required")),
            })
        attributes = _pick(
            item, "objectTypeId", "requiresApproval", "validationFunctionId")
        attributes["parameters"] = parameters
        nodes.append({
            "kind": KIND_ACTION,
            "local_id": item_id,
            "name": str(item.get("name") or item_id),
            "display_name": str(item.get("displayName") or item.get("name") or item_id),
            "description": _scalar(item.get("description")),
            "attributes": attributes,
        })
        if item.get("objectTypeId"):
            edges.append({
                "type": REL_ACTION_ON_TYPE,
                "source": structure_key("{release}", KIND_ACTION, item_id),
                "target": structure_key(
                    "{release}", KIND_OBJECT_TYPE, str(item["objectTypeId"])),
                "id": f"{REL_ACTION_ON_TYPE}:{item_id}",
            })

    for item in snapshot.get("functions") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        item_id = str(item["id"])
        # 可执行定义关联（类型/语言/目标引用），不含 body、parameters、缓存。
        nodes.append({
            "kind": KIND_FUNCTION,
            "local_id": item_id,
            "name": str(item.get("name") or item_id),
            "display_name": str(item.get("displayName") or item.get("name") or item_id),
            "description": _scalar(item.get("description")),
            "attributes": _pick(
                item, "functionType", "language", "returnType", "enabled",
                "targetObjectTypeId", "targetActionId"),
        })
        for field, kind in (
            ("targetObjectTypeId", KIND_OBJECT_TYPE),
            ("targetActionId", KIND_ACTION),
        ):
            if item.get(field):
                edges.append({
                    "type": REL_FUNCTION_REF,
                    "source": structure_key("{release}", KIND_FUNCTION, item_id),
                    "target": structure_key("{release}", kind, str(item[field])),
                    "id": f"{REL_FUNCTION_REF}:{item_id}:{field}",
                })

    for item in snapshot.get("mappings") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        item_id = str(item["id"])
        # 只投定义引用元数据；绝不投 fieldMapping（含 __remote__ 连接
        # 描述符）与评估数据。
        nodes.append({
            "kind": KIND_MAPPING,
            "local_id": item_id,
            "name": str(item.get("entityClass") or item_id),
            "display_name": str(item.get("entityClass") or item_id),
            "description": None,
            "attributes": _pick(
                item, "entityClass", "targetObjectTypeId", "status",
                "bindingMode", "curatedDatasetId"),
        })
        if item.get("targetObjectTypeId"):
            edges.append({
                "type": REL_MAPPING_TARGET,
                "source": structure_key("{release}", KIND_MAPPING, item_id),
                "target": structure_key(
                    "{release}", KIND_OBJECT_TYPE, str(item["targetObjectTypeId"])),
                "id": f"{REL_MAPPING_TARGET}:{item_id}",
            })

    for item in snapshot.get("linkMappings") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        item_id = str(item["id"])
        nodes.append({
            "kind": KIND_LINK_MAPPING,
            "local_id": item_id,
            "name": str(item.get("relationType") or item_id),
            "display_name": str(item.get("relationType") or item_id),
            "description": None,
            "attributes": _pick(
                item, "relationType", "srcKey", "tgtKey", "status",
                "bindingMode", "linkTypeId", "srcDatasetId", "tgtDatasetId",
                "edgeDatasetId"),
        })
        if item.get("linkTypeId"):
            edges.append({
                "type": REL_LINK_MAPPING_TARGET,
                "source": structure_key("{release}", KIND_LINK_MAPPING, item_id),
                "target": structure_key(
                    "{release}", KIND_LINK_TYPE, str(item["linkTypeId"])),
                "id": f"{REL_LINK_MAPPING_TARGET}:{item_id}",
            })

    _validate_endpoints(nodes, edges)
    return nodes, edges


def _validate_endpoints(nodes: list[dict], edges: list[dict]) -> None:
    """所有生成边必须解析到已存在节点；悬空引用 fail-closed。"""
    known = {structure_key("{release}", n["kind"], n["local_id"]) for n in nodes}
    for edge in edges:
        for role in ("source", "target"):
            if edge[role] not in known:
                raise StructureProjectionError(
                    f"快照结构引用悬空（{edge['id']} {role}={edge[role]}），"
                    "结构投影拒绝构建")


# ----------------------------------------------------------- fence 与重建


def _project_row(db: Session, ontology_id: str):
    from app.ontologies.projects.models import OntologyProject

    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if project is None:
        raise StructureProjectionRejected(f"ontology not found: {ontology_id}")
    return project


def mark_structure_failed(db: Session, ontology_id: str, error: object) -> None:
    row = _project_row(db, ontology_id)
    row.structure_projection_status = STATUS_FAILED
    row.structure_projection_error = str(error)[:1000]


def mark_structure_ready(
    db: Session, ontology_id: str, *, release_id: str, release_hash: str,
) -> None:
    row = _project_row(db, ontology_id)
    row.structure_projection_status = STATUS_READY
    row.structure_projection_error = None
    row.structure_projection_release_id = release_id
    row.structure_projection_hash = release_hash


def _current_release(db: Session, ontology_id: str):
    from app.ontologies.release_context import current_release_context

    try:
        return current_release_context(db, ontology_id)
    except Exception as exc:  # noqa: BLE001 - HTTPException 不越过投影边界
        detail = getattr(exc, "detail", None)
        message = detail.get("message") if isinstance(detail, dict) else None
        raise StructureProjectionRejected(message or str(exc)) from exc


def _bind_nodes(items: Iterable[dict], release_id: str) -> list[dict]:
    bound = []
    for item in items:
        envelope = {
            **item,
            "structure_key": structure_key(
                release_id, item["kind"], item["local_id"]),
            "structure_release_id": release_id,
        }
        envelope.pop("ontology_id", None)
        bound.append(envelope)
    return bound


def _bind_edges(items: Iterable[dict], release_id: str) -> list[dict]:
    bound = []
    for item in items:
        edge = dict(item)
        edge["source"] = edge["source"].replace("{release}", release_id)
        edge["target"] = edge["target"].replace("{release}", release_id)
        bound.append(edge)
    return bound


def _neo4j_service():
    from app.ontologies.graph.neo4j_service import Neo4jService

    return Neo4jService()


def _delete_other_releases(neo, ontology_id: str, keep_release_id: str) -> None:
    """删除旧 release 结构子图（label 限定 + structure_ontology_id 匹配）。

    在新 release 子图写入成功之后调用，实现"旧子图保留至新 ready"的
    原子替换语义；失败抛错由调用方置 failed（重试重建幂等收敛）。
    """
    node_query = f"""
    MATCH (n:{STRUCTURE_LABEL})
    WHERE n.structure_ontology_id = $ontology_id
      AND n.structure_release_id IS NOT NULL
      AND n.structure_release_id <> $keep
    WITH n LIMIT $batch
    DETACH DELETE n
    RETURN count(n) AS deleted
    """
    while True:
        record = neo.run_cypher(
            node_query,
            {"ontology_id": ontology_id, "keep": keep_release_id,
             "batch": STRUCTURE_DELETE_BATCH},
        )
        deleted = int(record[0]["deleted"]) if record else 0
        if deleted <= 0:
            return


def rebuild_structure_projection(
    db: Session,
    ontology_id: str,
    *,
    service=None,
    commit: bool = True,
) -> dict:
    """从当前 release 快照重建结构投影，返回最终 status 载荷。

    并发控制遵循实例投影先例：调用方持有 `_ontology_build_lock`
    （router 端点显式获取；发布/回滚钩子在 promotion/rollback 既有锁
    内执行）。锁内声明 building；持锁时接管遗留 ``building``（前持有
    者已消失），不存在不可恢复的永久 building；重复触发在锁上排队
    串行幂等重建。

    ``commit=True``（手动重建入口）：自带 fence 事务，失败 rollback 后
    耐久化 failed 并抛 ``StructureProjectionError``。
    ``commit=False``（发布/回滚钩子）：不 commit 不 rollback，fence 状态
    随调用方事务提交；失败在会话内置 failed 后抛错，由钩子决定不阻断
    发布主流程。
    """
    context = _current_release(db, ontology_id)
    release = context.release
    release_id = str(release.id)
    release_hash = str(
        getattr(release, "snapshot_hash", None)
        or snapshot_hash(context.snapshot))

    _claim_building(db, ontology_id)
    if commit:
        db.commit()

    neo = service
    owns_service = neo is None
    try:
        # claim 之后的全部可失败步骤（含快照提取/悬空引用校验）都必须在
        # failed finalize 保护内：否则无效快照会把 fence 永久留在 building。
        raw_nodes, raw_edges = extract_structure_graph(context.snapshot)
        nodes = _bind_nodes(raw_nodes, release_id)
        for node in nodes:
            node["structure_ontology_id"] = ontology_id
        edges = _bind_edges(raw_edges, release_id)

        meta_node = {
            "structure_key": f"{release_id}::release_meta",
            "structure_release_id": release_id,
            "structure_ontology_id": ontology_id,
            "kind": KIND_META,
            "local_id": "release_meta",
            "snapshot_hash": release_hash,
            "version": str(getattr(release, "version_number", "") or ""),
            "node_count": len(nodes),
            "edge_count": len(edges),
        }

        if neo is None:
            neo = _neo4j_service()
        if not neo.available:
            raise StructureProjectionError(
                "Neo4j 不可用，结构投影重建失败（状态已置 failed，不会假 ready）")
        written = neo.batch_upsert_entities(
            STRUCTURE_LABEL, nodes, key_field=STRUCTURE_KEY_FIELD,
            replace_properties=True,
        )
        if written != len(nodes):
            raise StructureProjectionError(
                f"结构节点写入不完整: expected={len(nodes)} actual={written}")
        grouped: dict[str, list[dict]] = {}
        for edge in edges:
            grouped.setdefault(edge["type"], []).append({
                "src_key": edge["source"],
                "tgt_key": edge["target"],
                "props": {
                    "id": edge["id"],
                    "structure_release_id": release_id,
                    "structure_ontology_id": ontology_id,
                },
            })
        for rel_type, batch in grouped.items():
            written = neo.batch_upsert_relations(
                STRUCTURE_LABEL, STRUCTURE_LABEL, rel_type, batch,
                key_field=STRUCTURE_KEY_FIELD,
            )
            if written != len(batch):
                raise StructureProjectionError(
                    f"结构关系写入不完整 ({rel_type}): "
                    f"expected={len(batch)} actual={written}")
        neo.batch_upsert_entities(
            STRUCTURE_LABEL, [meta_node], key_field=STRUCTURE_KEY_FIELD,
            replace_properties=True,
        )
        _delete_other_releases(neo, ontology_id, release_id)
        # 末端 recheck：重建期间的并发发布不得被静默吸收成 ready。
        from app.ontologies.projects.models import OntologyProject

        pointer = db.query(OntologyProject.current_release_id).filter(
            OntologyProject.id == ontology_id).first()
        if not pointer or str(pointer[0] or "") != release_id:
            raise StructureProjectionError(
                "当前发布指针在重建期间变化，本次产物已作废并置 failed；"
                "请重新触发重建")
        mark_structure_ready(
            db, ontology_id, release_id=release_id, release_hash=release_hash)
        if commit:
            db.commit()
        return {
            "status": STATUS_READY,
            "releaseId": release_id,
            "snapshotHash": release_hash,
            "nodeCount": len(nodes),
            "edgeCount": len(edges),
        }
    except StructureProjectionRejected:
        raise  # pre-claim 拒绝：fence 不动（SQL 权威拒绝，非构建失败）
    except Exception as exc:  # noqa: BLE001 - 耐久 failed 高于异常形状
        if commit:
            db.rollback()
        message = str(exc) or type(exc).__name__
        try:
            mark_structure_failed(db, ontology_id, message)
            if commit:
                db.commit()
        except Exception as fence_exc:  # noqa: BLE001
            logger.warning("structure fence finalize failed: %s", fence_exc)
            raise StructureProjectionError(
                f"结构投影失败且状态落库失败: {message}") from fence_exc
        if isinstance(exc, StructureProjectionError):
            raise
        raise StructureProjectionError(
            f"结构投影重建失败: {message}") from exc
    finally:
        if owns_service and neo is not None:
            try:
                neo.close()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------- 状态对账


def _strict_count(value: object) -> int | None:
    """计数只接受非负整数：bool/float/字符串/负数一律不通过。

    原生 count() 恒为 int；浮点（1.9/12.0）或负数属 malformed meta，
    不得经 int() 截断成"看起来合法"的计数。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _verify_structure_meta(
    ontology_id: str, release_id: str | None, stored_hash: str | None,
    *, service=None,
) -> tuple[bool, int, int]:
    """核验图 meta 与实际子图：可达、哈希一致、meta 计数=真实计数。

    返回 ``(ok, node_count, edge_count)``。图不可达/meta 缺失/哈希不符/
    meta 自报计数与 label+release 作用域内实际节点边数不符（节点被
    精确删除后的残留 meta）一律 ``ok=False`` —— ready 必须建立在可
    核验产物之上，不能只信 meta 自报数字，也不静默零计数伪装。
    """
    if not release_id:
        return False, 0, 0
    neo = service
    owns_service = neo is None
    try:
        if neo is None:
            neo = _neo4j_service()
        if not neo.available:
            return False, 0, 0
        params = {"oid": ontology_id, "rid": release_id}
        meta_rows = neo.run_cypher(f"""
            MATCH (n:{STRUCTURE_LABEL} {{
                {STRUCTURE_KEY_FIELD}: $meta_key, kind: $kind
            }})
            RETURN n.node_count AS nodes, n.edge_count AS edges,
                   n.snapshot_hash AS hash
        """, {"meta_key": f"{release_id}::release_meta", "kind": KIND_META})
        if not meta_rows:
            return False, 0, 0
        meta = meta_rows[0]
        node_count = _strict_count(meta.get("nodes"))
        edge_count = _strict_count(meta.get("edges"))
        if node_count is None or edge_count is None:
            return False, 0, 0
        if stored_hash is not None and str(meta.get("hash") or "") != str(stored_hash):
            return False, 0, 0
        # 实际作用域计数：捕捉"删了节点但 meta 残留"的图内漂移。
        # 边计数两端都限定本 ontology+release：跨本体/跨 release 的
        # 残留边既不计为我们的结构，也不会反向污染一致性判定。
        actual_nodes = neo.run_cypher(f"""
            MATCH (n:{STRUCTURE_LABEL})
            WHERE n.structure_ontology_id = $oid
              AND n.structure_release_id = $rid
              AND n.kind <> '{KIND_META}'
            RETURN count(n) AS c
        """, params)
        actual_edges = neo.run_cypher(f"""
            MATCH (a:{STRUCTURE_LABEL})-[r]->(b:{STRUCTURE_LABEL})
            WHERE a.structure_ontology_id = $oid
              AND a.structure_release_id = $rid
              AND b.structure_ontology_id = $oid
              AND b.structure_release_id = $rid
              AND a.kind <> '{KIND_META}' AND b.kind <> '{KIND_META}'
            RETURN count(r) AS c
        """, params)
        if (not actual_nodes or not actual_edges
                or _strict_count(actual_nodes[0].get("c")) != node_count
                or _strict_count(actual_edges[0].get("c")) != edge_count):
            return False, 0, 0
        return True, node_count, edge_count
    except Exception:  # noqa: BLE001 - 核验失败即不可用，不静默降级
        return False, 0, 0
    finally:
        if owns_service and neo is not None:
            try:
                neo.close()
            except Exception:  # noqa: BLE001
                pass


def structure_projection_status(
    db: Session, ontology_id: str, *, service=None,
) -> dict:
    """读侧对账：存储 fence 状态 vs 当前发布指针/哈希，绝不假 ready。

    ``drift`` 定义：已构建过（有 releaseId）且与 SQL 当前发布不一致。
    ``empty`` 时 drift=false（尚无产物，无从漂移）。ready 且无漂移时还
    必须能核验图 meta（可达 + 哈希一致），否则如实报 ``unavailable``。
    """
    project = _project_row(db, ontology_id)
    stored = str(project.structure_projection_status or STATUS_EMPTY)
    stored_release = project.structure_projection_release_id
    stored_hash = project.structure_projection_hash

    current_id: str | None = None
    current_version: str | None = None
    current_hash: str | None = None
    from app.ontologies.release_context import current_release_context
    try:
        context = current_release_context(db, ontology_id)
        current_id = str(context.release.id)
        current_version = str(context.release.version_number)
        current_hash = str(
            getattr(context.release, "snapshot_hash", None)
            or snapshot_hash(context.snapshot))
    except Exception:  # noqa: BLE001 - 无当前发布 = 任何产物都已漂移
        current_id = None

    drift = bool(
        stored_release
        and (current_id is None or stored_release != current_id
             or (stored_hash or None) != current_hash)
    )
    reported = stored
    error = project.structure_projection_error
    node_count = edge_count = 0
    if stored == STATUS_READY:
        if drift:
            # 漂移产物不可读：计数归零，避免 UI 把旧 release 子图当数据。
            reported = STATUS_STALE
        else:
            ok, node_count, edge_count = _verify_structure_meta(
                ontology_id, stored_release, stored_hash, service=service)
            if not ok:
                reported = STATUS_UNAVAILABLE
                error = error or (
                    "结构图不可达或 meta 一致性无法核验，请重新触发重建")

    return {
        "status": reported,
        "releaseId": stored_release,
        "version": _stored_version(
            db, ontology_id, stored_release, current_version),
        "snapshotHash": stored_hash,
        "drift": drift,
        "error": error,
        "nodeCount": node_count,
        "edgeCount": edge_count,
    }


def _stored_version(
    db: Session, ontology_id: str, release_id: str | None,
    current_version: str | None,
) -> str | None:
    """构建产物所属 release 的版本号；漂移时如实返回旧 release 版本。"""
    if release_id is None:
        return current_version
    from app.ontologies.versions.models import OntologyVersion

    row = db.query(OntologyVersion.version_number).filter(
        OntologyVersion.id == release_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    return str(row[0]) if row else current_version
