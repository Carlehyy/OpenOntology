"""本体结构投影（OntologyStructureNode 探索图）行为测试：

  1. 白名单提取：结构 kind 齐全、同名属性按 owner 区分、秘密字段绝不入图；
  2. 悬空引用 fail-closed：缺端点/引用缺失节点直接拒绝构建；
  3. 重建 fence：release-scoped 键、无 ontology_id 信封、失败耐久 failed、
     busy CAS 冲突不是失败、末端 recheck 拒绝并发发布变化；
  4. 状态对账：release/哈希漂移报 stale、不假 ready、计数归零；
  5. 实时授权过滤：类型/属性/动作 allowlist、引用缺失 fail-closed 隐藏；
  6. API：GET 状态、POST 重建（仅 admin，busy 409）、结构图未就绪 503、
     meta 不一致 503、双端可见的 join 声明 overlay；
  7. 发布钩子：结构失败不阻断发布主流程、实例 ready 语义不变。
"""
from __future__ import annotations

import uuid

import pytest

from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion
from app.ontologies.agent_query.models import AgentJoinAssertion
from app.ontologies.agent_runtime.boundary import build_scope
from app.ontologies.graph.structure_projection import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_READY,
    STATUS_STALE,
    StructureProjectionError,
    extract_structure_graph,
    rebuild_structure_projection,
    structure_projection_status,
)
from app.ontologies.release_context import create_initial_release


SECRET_TOKEN = "conn-secret-hunter2"


def _snapshot() -> dict:
    """两个对象类型共享同名属性 status（不同 type），覆盖全部结构 kind。

    埋入秘密诱饵：mapping fieldMapping/__remote__、function body、
    action rules、参数 defaultValue —— 断言绝不入图。
    """
    return {
        "objectTypes": [
            {
                "id": "ot-a", "name": "Customer", "displayName": "客户",
                "primaryKey": "cid",
                "properties": [
                    {"id": "p-cid", "name": "cid", "type": "string",
                     "required": True, "description": "客户主键"},
                    {"id": "p-st-a", "name": "status", "type": "string",
                     "required": False},
                ],
            },
            {
                "id": "ot-b", "name": "Order", "displayName": "订单",
                "primaryKey": "oid",
                "properties": [
                    {"id": "p-st-b", "name": "status", "type": "number",
                     "required": False},
                ],
            },
        ],
        "linkTypes": [
            {"id": "lt-1", "name": "order_customer", "displayName": "订单-客户",
             "sourceObjectTypeId": "ot-b", "targetObjectTypeId": "ot-a",
             "cardinality": "many-to-one"},
        ],
        "actions": [
            {"id": "act-1", "name": "approve", "displayName": "审批",
             "objectTypeId": "ot-b", "requiresApproval": True,
             "rules": [{"secret": SECRET_TOKEN}],
             "parameters": [
                 {"name": "amount", "type": "number", "required": True,
                  "defaultValue": SECRET_TOKEN},
             ]},
        ],
        "functions": [
            {"id": "fn-1", "name": "calc", "displayName": "计算",
             "functionType": "object", "language": "expression",
             "returnType": "number", "enabled": True,
             "targetObjectTypeId": "ot-a",
             "body": f"secret-body-{SECRET_TOKEN}"},
            {"id": "fn-2", "name": "orphan", "displayName": "无归属函数",
             "functionType": "object", "language": "expression",
             "returnType": "number", "enabled": True},
            {"id": "fn-3", "name": "hooked", "displayName": "类型+动作双引用",
             "functionType": "object", "language": "expression",
             "returnType": "number", "enabled": True,
             "targetObjectTypeId": "ot-a", "targetActionId": "act-1"},
        ],
        "mappings": [
            {"id": "map-1", "entityClass": "Customer", "status": "applied",
             "targetObjectTypeId": "ot-a", "curatedDatasetId": "ds-1",
             "bindingMode": "remote",
             "remote": {
                 "assetId": "asset-1", "datasetId": "ds-1",
                 "connectionId": "conn-fixture-1", "schemaName": "public",
                 "tableName": "customers", "schemaFingerprint": "fp-1",
                 "primaryKey": ["cid"],
                 "columns": [
                     {"name": "cid", "type": "string"},
                     {"name": "status", "type": "string"},
                 ],
             },
             "fieldMapping": {"cid": "c_id", "__remote__": {
                 "connectionId": SECRET_TOKEN, "tableName": "cust"}}},
            {"id": "map-2", "entityClass": "Loose", "status": "applied"},
        ],
        "linkMappings": [
            {"id": "lmap-1", "relationType": "order_customer",
             "status": "applied",
             "srcKey": "o_cust", "tgtKey": "cid", "linkTypeId": "lt-1",
             "srcDatasetId": "ds-2", "tgtDatasetId": "ds-1",
             "fieldMapping": {"secret": SECRET_TOKEN}},
        ],
        "sentinels": [{"id": "sent-1"}],
    }


class FakeStructureNeo:
    """结构投影测试替身：记录写入、按 marker 分发读/删。"""

    def __init__(self, *, available: bool = True):
        self.available = available
        self.nodes: dict[str, dict] = {}
        self.relations: list[tuple] = []
        self.delete_calls: list[tuple] = []

    def batch_upsert_entities(self, label, entities, key_field="id",
                              replace_properties=False):
        assert label == "OntologyStructureNode"
        assert key_field == "structure_key"
        if not self.available:
            return 0
        for entity in entities:
            self.nodes[entity[key_field]] = dict(entity)
        return len(entities)

    def batch_upsert_relations(self, src_label, tgt_label, rel_type, relations,
                               key_field="id"):
        written = 0
        index = {
            (rel[0], rel[1], rel[2], rel[3].get("id")): pos
            for pos, rel in enumerate(self.relations)
        }
        for relation in relations:
            if (relation["src_key"] in self.nodes
                    and relation["tgt_key"] in self.nodes):
                entry = (
                    rel_type, relation["src_key"], relation["tgt_key"],
                    dict(relation["props"]))
                key = (entry[0], entry[1], entry[2], entry[3].get("id"))
                if key in index:
                    self.relations[index[key]] = entry
                else:
                    index[key] = len(self.relations)
                    self.relations.append(entry)
                written += 1
        return written

    def run_cypher(self, query, params=None):
        params = params or {}
        if "$meta_key" in query:
            meta = self.nodes.get(params["meta_key"])
            if not meta:
                return []
            if query.strip().endswith("RETURN n"):  # 读侧取整节点
                return [{"n": dict(meta)}]
            # 状态核验取扁平计数字段
            return [{"nodes": meta.get("node_count"),
                     "edges": meta.get("edge_count"),
                     "hash": meta.get("snapshot_hash")}]
        if "count(n) AS c" in query:
            count = sum(
                1 for node in self.nodes.values()
                if node.get("structure_ontology_id") == params["oid"]
                and node.get("structure_release_id") == params["rid"]
                and node.get("kind") != "release_meta")
            return [{"c": count}]
        if "count(r) AS c" in query:
            count = sum(
                1 for _rel_type, src, tgt, _props in self.relations
                if all(self.nodes.get(end, {}).get(field) == value
                       for end in (src, tgt)
                       for field, value in (
                           ("structure_ontology_id", params["oid"]),
                           ("structure_release_id", params["rid"])))
                and self.nodes.get(src, {}).get("kind") != "release_meta"
                and self.nodes.get(tgt, {}).get("kind") != "release_meta")
            return [{"c": count}]
        if "node_count" in query:
            meta = self.nodes.get(params["meta_key"])
            if meta:
                return [{"nodes": meta["node_count"],
                         "edges": meta["edge_count"]}]
            return []
        if "DETACH DELETE" in query:
            keep = params["keep"]
            stale_keys = [
                key for key, node in self.nodes.items()
                if node.get("structure_release_id") != keep
            ]
            for key in stale_keys:
                self.nodes.pop(key)
            self.relations = [
                rel for rel in self.relations
                if rel[1] in self.nodes and rel[2] in self.nodes
            ]
            self.delete_calls.append((params["ontology_id"], keep,
                                      len(stale_keys)))
            return [{"deleted": len(stale_keys)}] if stale_keys else []
        if "rel_type" in query:
            return [
                {"source": src, "target": tgt, "rel_type": rel_type,
                 "props": dict(props)}
                for rel_type, src, tgt, props in self.relations
                if all(self.nodes.get(end, {}).get(field) == value
                       for end in (src, tgt)
                       for field, value in (
                           ("structure_ontology_id", params["oid"]),
                           ("structure_release_id", params["rid"])))
            ]
        if "release_meta" in query:
            return [
                {"n": dict(node)} for node in self.nodes.values()
                if node.get("structure_ontology_id") == params["oid"]
                and node.get("structure_release_id") == params["rid"]
                and node.get("kind") != "release_meta"
            ]
        return []

    def close(self):
        pass


@pytest.fixture
def ontology_with_release(db, admin_user):
    project = OntologyProject(
        id=str(uuid.uuid4()), name="结构投影测试", domain="供应链",
        created_by=admin_user.id, status="draft",
    )
    db.add(project)
    db.commit()
    release = create_initial_release(
        db, project, snapshot=_snapshot(), created_by=admin_user.id)
    db.commit()
    return project, release


def _new_release(db, project, snapshot, number="v1"):
    release = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=project.id,
        version_number=number, version_label=number, node_kind="release",
        lifecycle_status="released", revision=0, snapshot_formal=snapshot,
        created_by=project.created_by,
    )
    db.add(release)
    db.flush()
    project.current_release_id = release.id
    project.version = number
    db.commit()
    return release


def _rebuild(db, project, fake):
    return rebuild_structure_projection(db, project.id, service=fake)


def _filter(db, project, release, fake):
    """纯过滤层测试入口：直接用替身内的节点/边，不经 Neo4jService。"""
    from app.ontologies.network import service as network_service
    raw_nodes = [
        dict(node) for node in fake.nodes.values()
        if node.get("structure_release_id") == release.id
        and node.get("kind") != "release_meta"
    ]
    raw_edges = [
        {"id": props.get("id"), "type": rel_type, "source": src,
         "target": tgt}
        for rel_type, src, tgt, props in fake.relations
        if fake.nodes.get(src, {}).get("structure_release_id") == release.id
    ]
    scope = build_scope(db, project.id, release_id=release.id)[2]
    return network_service._filter_structure_by_scope(scope, raw_nodes, raw_edges)


# ------------------------------------------------------------- 白名单提取


def test_extract_covers_all_kinds_and_scopes_same_name_property():
    nodes, edges = extract_structure_graph(_snapshot())
    kinds = {node["kind"] for node in nodes}
    assert kinds == {
        "object_type", "property", "link_type", "action",
        "function", "mapping", "link_mapping",
    }
    status_props = [n for n in nodes if n["local_id"].endswith("::status")]
    assert {n["local_id"] for n in status_props} == {
        "ot-a::status", "ot-b::status"}
    by_id = {n["local_id"]: n for n in status_props}
    assert by_id["ot-a::status"]["attributes"]["type"] == "string"
    assert by_id["ot-b::status"]["attributes"]["type"] == "number"
    assert by_id["ot-a::status"]["attributes"]["objectTypeId"] == "ot-a"


def test_extract_never_serializes_secrets():
    import json
    nodes, edges = extract_structure_graph(_snapshot())
    blob = json.dumps([nodes, edges], ensure_ascii=False, default=str)
    for banned in (
        SECRET_TOKEN, "fieldMapping", "__remote__", "secret-body",
        "cacheStrategy", "defaultValue",
    ):
        assert banned not in blob, banned
    action = next(n for n in nodes if n["kind"] == "action")
    assert "rules" not in action["attributes"]
    assert action["attributes"]["parameters"] == [
        {"name": "amount", "displayName": "amount", "type": "number",
         "required": True},
    ]
    function = next(n for n in nodes if n["kind"] == "function")
    assert "body" not in function["attributes"]


def test_extract_missing_link_endpoint_fails():
    snapshot = _snapshot()
    snapshot["linkTypes"][0]["sourceObjectTypeId"] = None
    with pytest.raises(StructureProjectionError):
        extract_structure_graph(snapshot)


def test_extract_dangling_reference_fails():
    snapshot = _snapshot()
    snapshot["functions"][0]["targetActionId"] = "act-missing"
    with pytest.raises(StructureProjectionError):
        extract_structure_graph(snapshot)


# ----------------------------------------------------------- 重建与 fence


def test_rebuild_writes_release_scoped_envelope(db, ontology_with_release):
    project, release = ontology_with_release
    fake = FakeStructureNeo()
    result = _rebuild(db, project, fake)
    assert result["status"] == STATUS_READY
    assert result["releaseId"] == release.id
    assert db.query(OntologyProject).get(project.id).structure_projection_status \
        == STATUS_READY
    assert fake.nodes[f"{release.id}::release_meta"]["node_count"] \
        == result["nodeCount"]
    for node in fake.nodes.values():
        # 信封独立属性：实例投影 delete_by_ontology(ontology_id) 不可命中。
        assert "ontology_id" not in node
        assert node["structure_ontology_id"] == project.id
        assert node["structure_release_id"] == release.id
    assert any(k.endswith("::property::ot-a::status") for k in fake.nodes)
    assert any(k.endswith("::property::ot-b::status") for k in fake.nodes)


def test_rebuild_idempotent_diff_stable(db, ontology_with_release):
    project, _release = ontology_with_release
    first = FakeStructureNeo()
    _rebuild(db, project, first)
    second = FakeStructureNeo()
    _rebuild(db, project, second)
    assert set(first.nodes) == set(second.nodes)
    assert {(r[0], r[1], r[2]) for r in first.relations} == {
        (r[0], r[1], r[2]) for r in second.relations}


def test_rebuild_failure_marks_failed_not_ready(db, ontology_with_release):
    project, _release = ontology_with_release
    fake = FakeStructureNeo(available=False)
    with pytest.raises(StructureProjectionError):
        _rebuild(db, project, fake)
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_FAILED
    assert row.structure_projection_error
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == STATUS_FAILED
    assert status["drift"] is False  # 无产物，无从漂移


def test_status_reports_stale_after_release_change(db, ontology_with_release):
    project, release = ontology_with_release
    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    assert structure_projection_status(
        db, project.id, service=fake)["status"] == STATUS_READY

    _new_release(db, project, _snapshot())  # 新发布指针
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == STATUS_STALE
    assert status["drift"] is True
    assert status["nodeCount"] == 0  # 漂移产物不可读，计数归零
    # 漂移时 version 必须是产物所属旧 release 的版本号，不是当前新版本。
    assert status["version"] == release.version_number

    # 重建后收敛：旧 release 子图被替换（fake delete 记录 keep=新 release）。
    _rebuild(db, project, fake)
    assert fake.delete_calls[-1][1] == project.current_release_id
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == STATUS_READY
    assert status["drift"] is False
    assert status["nodeCount"] > 0


def test_commit_false_rides_caller_transaction(db, ontology_with_release):
    project, _release = ontology_with_release
    fake = FakeStructureNeo()
    rebuild_structure_projection(db, project.id, service=fake, commit=False)
    db.rollback()
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_EMPTY
    # 图写物已存在但 SQL fence 未 ready → 读侧被拒，孤儿物由 drift 兜底。
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == STATUS_EMPTY


def test_rebuild_takes_over_abandoned_building(db, ontology_with_release):
    """前持有者消失遗留的 building：持锁重建接管恢复，不锁死管理员。"""
    project, _release = ontology_with_release
    row = db.query(OntologyProject).get(project.id)
    row.structure_projection_status = "building"  # 模拟进程死亡遗留
    db.commit()
    fake = FakeStructureNeo()
    result = _rebuild(db, project, fake)
    assert result["status"] == STATUS_READY
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_READY
    assert len(fake.nodes) > 0


def test_extraction_failure_after_claim_marks_failed_not_building(
        db, ontology_with_release):
    """claim 之后的无效快照（悬空引用）必须落入 failed finalize。"""
    project, release = ontology_with_release
    # JSON 列需整体重赋值（原位变异不被 SQLAlchemy 脏检测）。
    release.snapshot_formal = {
        **(release.snapshot_formal or {}),
        "functions": [
            {"id": "fn-bad", "name": "bad", "displayName": "坏引用",
             "targetActionId": "act-missing"}],
    }
    db.commit()
    fake = FakeStructureNeo()
    with pytest.raises(StructureProjectionError, match="悬空"):
        _rebuild(db, project, fake)
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_FAILED
    assert "悬空" in row.structure_projection_error


def test_rebuild_end_recheck_rejects_concurrent_release_change(
        db, ontology_with_release):
    project, _release = ontology_with_release
    fake = FakeStructureNeo()
    original = fake.batch_upsert_relations
    raced = {"done": False}

    def racing_writer(src_label, tgt_label, rel_type, relations, key_field="id"):
        # 模拟重建期间的并发发布：指针在图写入中途变化（只触发一次）。
        if not raced["done"]:
            raced["done"] = True
            _new_release(db, project, _snapshot(), number="v9")
        return original(src_label, tgt_label, rel_type, relations,
                        key_field=key_field)

    fake.batch_upsert_relations = racing_writer
    with pytest.raises(StructureProjectionError, match="发布指针"):
        _rebuild(db, project, fake)
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_FAILED
    assert "发布指针" in row.structure_projection_error
    # 本次从未记录 ready 产物（release_id 列为空）→ drift 按定义为 False；
    # 图内 v0 孤儿写物读侧不可见，待下次成功重建时清理。
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == STATUS_FAILED
    assert status["drift"] is False


# ----------------------------------------------------------- 实时授权过滤


def test_scope_filter_drops_hidden_type_and_same_name_property(
        db, ontology_with_release):
    project, release = ontology_with_release
    _, profile, _scope = build_scope(db, project.id, release_id=release.id)
    profile.allowed_object_type_ids = ["ot-a"]
    db.commit()

    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    nodes, edges = _filter(db, project, release, fake)
    kinds = {(n["kind"], n.get("local_id")) for n in nodes}
    assert ("object_type", "ot-a") in kinds
    assert ("object_type", "ot-b") not in kinds
    # 隐藏类型的同名属性整体剔除；可见类型的同名属性保留且归属正确。
    assert ("property", "ot-a::status") in kinds
    assert ("property", "ot-b::status") not in kinds
    # 链接类型两端不可见 → 节点与边都剔除。
    assert not any(n["kind"] == "link_type" for n in nodes)
    assert all(
        edge["source"] in {n["structure_key"] for n in nodes}
        and edge["target"] in {n["structure_key"] for n in nodes}
        for edge in edges)


def test_property_allowlist_uses_pure_name(db, ontology_with_release):
    project, release = ontology_with_release
    _, profile, _ = build_scope(db, project.id, release_id=release.id)
    profile.allowed_properties = {"ot-a": ["cid"]}
    db.commit()

    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    nodes, _edges = _filter(db, project, release, fake)
    local_ids = {n["local_id"] for n in nodes}
    assert "ot-a::status" not in local_ids
    assert "ot-a::cid" in local_ids


def test_filter_fail_closed_for_unowned_definitions(db, ontology_with_release):
    """无归属目标的函数、无目标类型的映射：即使全量授权也隐藏。"""
    project, release = ontology_with_release
    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    nodes, _edges = _filter(db, project, release, fake)
    local_ids = {n["local_id"] for n in nodes}
    assert "fn-2" not in local_ids  # 无 targetType/targetAction 的函数
    assert "map-2" not in local_ids  # 无 targetObjectTypeId 的映射


def test_status_unavailable_when_ready_but_meta_missing(
        db, ontology_with_release):
    """ready 且无漂移但图 meta 缺失/不可达：如实 unavailable，不静默零计数。"""
    project, release = ontology_with_release
    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    assert structure_projection_status(
        db, project.id, service=fake)["status"] == STATUS_READY

    fake.nodes.pop(f"{release.id}::release_meta")
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == "unavailable"
    assert status["nodeCount"] == 0
    assert status["drift"] is False
    assert status["error"]

    unreachable = FakeStructureNeo(available=False)
    status = structure_projection_status(db, project.id, service=unreachable)
    assert status["status"] == "unavailable"


def test_action_allowlist_hides_action(db, ontology_with_release):
    project, release = ontology_with_release
    _, profile, _ = build_scope(db, project.id, release_id=release.id)
    profile.allowed_action_ids = []
    db.commit()

    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    nodes, _edges = _filter(db, project, release, fake)
    assert not any(n["kind"] == "action" for n in nodes)


def test_link_type_allowlist_hides_link_and_link_mapping(
        db, ontology_with_release):
    """链接类型走 scope.link_types allowlist：收紧后 link_type/link_mapping 隐藏。"""
    project, release = ontology_with_release
    _, profile, _ = build_scope(db, project.id, release_id=release.id)
    profile.allowed_link_type_ids = []
    db.commit()

    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    nodes, _edges = _filter(db, project, release, fake)
    kinds = {n["kind"] for n in nodes}
    assert "link_type" not in kinds
    assert "link_mapping" not in kinds


def test_function_with_hidden_action_ref_is_hidden(db, ontology_with_release):
    """函数同时引用类型与动作：类型可见但动作被 allowlist 隐藏 → 整体隐藏。"""
    project, release = ontology_with_release
    _, profile, _ = build_scope(db, project.id, release_id=release.id)
    profile.allowed_action_ids = []  # act-1 不可见，ot-a 仍可见
    db.commit()

    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    nodes, _edges = _filter(db, project, release, fake)
    local_ids = {n["local_id"] for n in nodes}
    assert "fn-3" not in local_ids  # 双引用函数被隐藏
    assert "fn-1" in local_ids     # 仅类型引用的函数保留


# ----------------------------------------------------------- API 行为


def _structure(path: str) -> str:
    return f"/api/v2/ontology-network{path}"


def test_api_status_and_admin_rebuild_guard(
        client, auth_headers, db, ontology_with_release, monkeypatch):
    project, _release = ontology_with_release
    fake = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: fake)

    r = client.get(_structure(f"/{project.id}/structure-projection"),
                   headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == STATUS_EMPTY
    assert r.json()["data"]["drift"] is False

    r = client.post(_structure(f"/{project.id}/structure-projection/rebuild"),
                    headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == STATUS_READY
    assert r.json()["data"]["nodeCount"] > 0


def test_api_rebuild_rejects_editor(
        client, editor_user, db, ontology_with_release, monkeypatch):
    project, _release = ontology_with_release
    fake = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: fake)
    token = client.post(
        "/api/v1/auth/login",
        json={"username": "editor", "password": "editor123"}).json()["data"]
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    r = client.post(_structure(f"/{project.id}/structure-projection/rebuild"),
                    headers=headers)
    assert r.status_code == 403
    assert fake.nodes == {}


def test_api_rebuild_serializes_and_recovers_building(
        client, auth_headers, db, ontology_with_release, monkeypatch):
    """并发语义：遗留 building 由持锁重建接管恢复，重复触发串行成功。"""
    project, _release = ontology_with_release
    fake = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: fake)
    row = db.query(OntologyProject).get(project.id)
    row.structure_projection_status = "building"
    db.commit()
    r = client.post(_structure(f"/{project.id}/structure-projection/rebuild"),
                    headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == STATUS_READY


def test_api_structure_graph_not_ready_503(
        client, auth_headers, db, ontology_with_release, monkeypatch):
    project, _release = ontology_with_release
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: FakeStructureNeo())
    r = client.get(_structure(f"/{project.id}/structure-graph"),
                   headers=auth_headers)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "ontology_structure_projection_not_ready"
    assert detail["projection_status"] == STATUS_EMPTY


def test_api_structure_graph_inconsistent_meta_503(
        client, auth_headers, db, ontology_with_release, monkeypatch):
    """图 meta 计数与实际子图不符（并发删除/半成品）不得以残图冒充成功。"""
    project, release = ontology_with_release
    shared = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: shared)
    r = client.post(_structure(f"/{project.id}/structure-projection/rebuild"),
                    headers=auth_headers)
    assert r.status_code == 200, r.text

    # meta 存在但计数错报：状态核验层先拦截（unavailable），读侧绝不放行。
    shared.nodes[f"{release.id}::release_meta"]["node_count"] = 999
    monkeypatch.setattr(
        "app.ontologies.graph.neo4j_service.Neo4jService",
        lambda *a, **k: shared)
    r = client.get(_structure(f"/{project.id}/structure-graph"),
                   headers=auth_headers)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "ontology_structure_projection_not_ready"
    assert detail["projection_status"] == "unavailable"


def test_api_structure_graph_meta_missing_503(
        client, auth_headers, db, ontology_with_release, monkeypatch):
    """meta 整体丢失：status 核验即 unavailable，读侧 fail-closed 503。"""
    project, release = ontology_with_release
    shared = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: shared)
    r = client.post(_structure(f"/{project.id}/structure-projection/rebuild"),
                    headers=auth_headers)
    assert r.status_code == 200, r.text

    shared.nodes.pop(f"{release.id}::release_meta")  # meta 丢失
    monkeypatch.setattr(
        "app.ontologies.graph.neo4j_service.Neo4jService",
        lambda *a, **k: shared)
    r = client.get(_structure(f"/{project.id}/structure-graph"),
                   headers=auth_headers)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "ontology_structure_projection_not_ready"
    assert detail["projection_status"] == "unavailable"


def test_api_structure_graph_serves_scoped_graph_with_overlay(
        client, auth_headers, db, ontology_with_release, admin_user,
        monkeypatch):
    project, release = ontology_with_release
    shared = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: shared)
    r = client.post(_structure(f"/{project.id}/structure-projection/rebuild"),
                    headers=auth_headers)
    assert r.status_code == 200, r.text

    # 双端均可见（两侧都是本本体已授权类型/属性）→ overlay 出现。
    db.add(AgentJoinAssertion(
        id="asr-1", created_by=str(admin_user.id),
        left_ontology_id=project.id, left_object_type_id="ot-a",
        left_property="cid", right_ontology_id=project.id,
        right_object_type_id="ot-b", right_property="status",
        key_type="string", business_meaning="同一客户"))
    # 右侧本体不存在/类型不在其当前 release → 整条声明不可见。
    db.add(AgentJoinAssertion(
        id="asr-2", created_by=str(admin_user.id),
        left_ontology_id=project.id, left_object_type_id="ot-a",
        left_property="cid", right_ontology_id="ghost",
        right_object_type_id="ot-x", right_property="xid",
        key_type="string", business_meaning="幽灵侧"))
    # 属性不在当前 release 结构（已删除属性）→ 失效证据不出 overlay。
    db.add(AgentJoinAssertion(
        id="asr-3", created_by=str(admin_user.id),
        left_ontology_id=project.id, left_object_type_id="ot-a",
        left_property="ghost_prop", right_ontology_id=project.id,
        right_object_type_id="ot-b", right_property="status",
        key_type="string", business_meaning="失效属性"))
    db.commit()

    monkeypatch.setattr(
        "app.ontologies.graph.neo4j_service.Neo4jService",
        lambda *a, **k: shared)
    r = client.get(_structure(f"/{project.id}/structure-graph"),
                   headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "ready"
    assert data["release"]["releaseId"] == release.id
    kinds = {node["kind"] for node in data["nodes"]}
    assert "object_type" in kinds and "property" in kinds
    assertion_ids = {o["assertionId"]
                     for o in data["overlays"]["joinAssertions"]}
    assert assertion_ids == {"asr-1"}


# ----------------------------------------------------------- 发布钩子


class _InstanceService:
    def __init__(self, _db):
        pass

    def _rebuild_neo4j_projection(self, _oid):
        return True


def test_activation_hook_structure_failure_does_not_block_promotion(
        db, ontology_with_release, monkeypatch):
    project, _release = ontology_with_release
    from app.ontologies.versions import release_activation_service
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: FakeStructureNeo(available=False))
    result = release_activation_service.rebuild_required_query_projections(
        db, project.id, mapping_service_factory=_InstanceService)
    assert result["ready"] is True
    assert result["neo4j"] == "ok"
    assert result["structure"]["status"] == STATUS_FAILED
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_FAILED


def test_activation_hook_success_reports_structure_ready(
        db, ontology_with_release, monkeypatch):
    project, _release = ontology_with_release
    from app.ontologies.versions import release_activation_service
    fake = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: fake)
    result = release_activation_service.rebuild_required_query_projections(
        db, project.id, mapping_service_factory=_InstanceService)
    assert result["structure"]["status"] == STATUS_READY
    assert result["structure"]["nodeCount"] > 0


# --------------------------------------- 真实缺陷回归（runtime03 证据）


def test_status_detects_actual_node_count_drift(db, ontology_with_release):
    """删除一个定义节点但 meta 残留：状态必须 unavailable，不得报 ready。

    对应真实证据 structure-runtime03-count-drift.json（status ready/
    count12 而图 503）。meta 自报计数不可信，须与实际作用域计数比对。
    """
    project, release = ontology_with_release
    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    assert structure_projection_status(
        db, project.id, service=fake)["status"] == STATUS_READY

    victim = next(key for key in fake.nodes
                  if key.endswith("::property::ot-a::status"))
    fake.nodes.pop(victim)  # 精确删除一个定义节点，meta 保持原样

    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == "unavailable"
    assert status["drift"] is False
    assert status["error"]

    # rebuild 恢复收敛。
    _rebuild(db, project, fake)
    assert structure_projection_status(
        db, project.id, service=fake)["status"] == STATUS_READY


@pytest.fixture
def empty_ontology_with_release(db, admin_user):
    project = OntologyProject(
        id=str(uuid.uuid4()), name="空结构本体", domain="供应链",
        created_by=admin_user.id, status="draft",
    )
    db.add(project)
    db.commit()
    release = create_initial_release(
        db, project, snapshot={}, created_by=admin_user.id)
    db.commit()
    return project, release


def test_empty_release_graph_serves_zero_counts(
        client, auth_headers, db, empty_ontology_with_release, monkeypatch):
    """空 release（0 节点 0 边）合法：rebuild ready 且 graph 200 空集。

    对应真实证据 structure-runtime03-empty-release.json（`or -1` 把合法
    0 误判为不一致导致 503）。
    """
    project, release = empty_ontology_with_release
    fake = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: fake)
    r = client.post(
        f"/api/v2/ontology-network/{project.id}/structure-projection/rebuild",
        headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == STATUS_READY
    assert r.json()["data"]["nodeCount"] == 0

    monkeypatch.setattr(
        "app.ontologies.graph.neo4j_service.Neo4jService",
        lambda *a, **k: fake)
    r = client.get(
        f"/api/v2/ontology-network/{project.id}/structure-graph",
        headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["nodes"] == []
    assert data["edges"] == []
    assert data["meta"]["truncated"] is False


def test_malformed_meta_counts_rejected(db, ontology_with_release):
    """meta 计数为字符串/缺失：判不可用，不当作 0 或崩溃。"""
    project, release = ontology_with_release
    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    fake.nodes[f"{release.id}::release_meta"]["node_count"] = "12"
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == "unavailable"

    fake.nodes[f"{release.id}::release_meta"]["node_count"] = None
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == "unavailable"

    # 浮点计数（原生 count 恒为 int）同样判 malformed，不截断成合法值。
    fake.nodes[f"{release.id}::release_meta"]["node_count"] = 12.5
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == "unavailable"
    fake.nodes[f"{release.id}::release_meta"]["node_count"] = 13.0
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == "unavailable"
    fake.nodes[f"{release.id}::release_meta"]["node_count"] = -1
    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == "unavailable"


def test_meta_count_mismatch_read_side_503(
        client, auth_headers, db, ontology_with_release, monkeypatch):
    """meta 计数与实际子图不符：读侧 503 inconsistent（不Partial 冒充）。"""
    project, release = ontology_with_release
    fake = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: fake)
    r = client.post(
        f"/api/v2/ontology-network/{project.id}/structure-projection/rebuild",
        headers=auth_headers)
    assert r.status_code == 200, r.text

    # 图内漂移：删一个节点且把 meta 计数改回自洽——读侧独立核验仍拦截
    # （状态层与读层各自比对实际子图，纵深防御）。
    victim = next(key for key in fake.nodes
                  if key.endswith("::property::ot-b::status"))
    fake.nodes.pop(victim)  # meta 计数不修正：读层按实际比对必须拦截
    meta_key = f"{release.id}::release_meta"
    monkeypatch.setattr(
        "app.ontologies.graph.neo4j_service.Neo4jService",
        lambda *a, **k: fake)
    from app.ontologies.network import service as network_service
    with pytest.raises(network_service.StructureGraphError) as excinfo:
        network_service._read_structure_subgraph(
            project.id, release.id, fake.nodes[meta_key]["snapshot_hash"])
    assert excinfo.value.payload["code"] == "structure_projection_inconsistent"


def test_network_graph_includes_remote_types_with_unknown_count(
        client, auth_headers, db, ontology_with_release):
    """全/半远程绑定本体：网络画布必须出现远程类型（count=null）。

    agent 本地图谱按契约排除远程类型；网络结构视图在 network 层补齐，
    实例数 unknown（None）不以 0 冒充；已发布关系 schema 边标注
    publishedStructureOnly。消除"选中本体无类型"误导（A20）。
    """
    project, _release = ontology_with_release
    # 快照 mapping map-1 已是 bindingMode=remote → ot-a 为远程绑定类型。
    r = client.get(
        f"/api/v2/ontology-network/graph?ontology_ids={project.id}&level=1",
        headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    nodes = {node["id"]: node for node in data["nodes"]}
    assert "type:ot-a" in nodes  # 远程类型可见
    assert nodes["type:ot-a"]["remote"] is True
    assert nodes["type:ot-a"]["count"] is None  # unknown，非 0
    assert "type:ot-b" in nodes
    edge_ids = {edge["id"] for edge in data["edges"]}
    assert "schema:lt-1" in edge_ids  # 含远程端点的已发布关系结构边
    edge = next(e for e in data["edges"] if e["id"] == "schema:lt-1")
    assert edge.get("publishedStructureOnly") is True
    # 汇总计数诚实口径：本地 0 + 远程未知，不得误读为"总实例 0"。
    section = data["ontologies"][0]
    assert section["knownLocalInstanceCount"] == 0
    assert section["unknownRemoteTypeCount"] == 1
    assert section["instanceCount"] == 0
    assert data["meta"]["totalUnknownRemoteTypes"] == 1


def test_cross_ontology_edge_not_leaked_nor_counted(
        db, ontology_with_release, monkeypatch):
    """跨本体/跨 release 残留边：不计入一致性判定、不进读响应。"""
    project, release = ontology_with_release
    fake = FakeStructureNeo()
    _rebuild(db, project, fake)
    monkeypatch.setattr(
        "app.ontologies.graph.neo4j_service.Neo4jService",
        lambda *a, **k: fake)

    foreign_key = f"{release.id}::object_type::foreign"
    fake.nodes[foreign_key] = {
        "structure_key": foreign_key, "kind": "object_type",
        "local_id": "foreign",
        "structure_ontology_id": "other-ontology",
        "structure_release_id": release.id,
    }
    src_key = f"{release.id}::object_type::ot-a"
    fake.relations.append((
        "STRUCTURE_CROSS", src_key, foreign_key, {"id": "x-cross"}))

    status = structure_projection_status(db, project.id, service=fake)
    assert status["status"] == STATUS_READY  # 跨本体边不误报漂移

    from app.ontologies.network import service as network_service
    _nodes, edges = network_service._read_structure_subgraph(
        project.id, release.id,
        fake.nodes[f"{release.id}::release_meta"]["snapshot_hash"])
    assert all(edge["target"] != foreign_key for edge in edges)
    assert all(edge["source"] != foreign_key for edge in edges)


def test_postcommit_hook_leaves_no_pending_fence_writes(
        db, ontology_with_release, monkeypatch):
    """事务所有权回归：commit 后调用的结构钩子必须收口自己的事务。

    真实缺陷（evolution 回滚链 database locked）：rollback 测试分支在
    主事务 commit 之后调钩子，commit=False 会把 OntologyProject fence
    写遗留在共享会话——后续任意查询 autoflush 挂起写事务，SQLite 下
    独立会话（DatasetWriteLock）INSERT 直接锁库。契约：post-commit 调用
    方传 structure_commit=True，返回时会话零 pending 写、fence 已持久化。
    """
    from app.ontologies.versions import release_activation_service

    project, _release = ontology_with_release
    fake = FakeStructureNeo()
    monkeypatch.setattr(
        "app.ontologies.graph.structure_projection._neo4j_service",
        lambda: fake)

    class _InstanceService:
        def __init__(self, _db):
            pass

        def _rebuild_neo4j_projection(self, _oid):
            return True

    db.commit()  # 模拟主事务已提交（rollback 测试分支的调用时点）
    result = release_activation_service.rebuild_required_query_projections(
        db, project.id, mapping_service_factory=_InstanceService,
        structure_commit=True,
    )
    assert result["structure"]["status"] == STATUS_READY

    db.rollback()  # 若有遗留 pending 写，rollback 会吞掉 fence——验证不存在
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_READY  # 已持久化

    # 会话无 pending 脏写：新查询不会 autoflush 出写事务。
    from sqlalchemy import inspect as sa_inspect
    assert not any(
        state.obj.__class__.__name__ == "OntologyProject"
        for state in db.dirty
    )


def test_rejected_paths_leave_fence_untouched(db, ontology_with_release):
    """pre-claim 拒绝（无当前发布/本体不存在）：fence 不写任何状态。

    契约：SQL 权威层拒绝是 rejected（fence 原状），不是构建失败
    failed——拒绝后旧产物由 drift 对账表达，不得污染 fence。
    """
    from app.ontologies.graph.structure_projection import (
        StructureProjectionRejected,
        rebuild_structure_projection,
    )
    from app.ontologies.versions import release_activation_service

    project, release = ontology_with_release
    project.current_release_id = None  # 无有效当前发布指针
    db.commit()

    with pytest.raises(StructureProjectionRejected):
        rebuild_structure_projection(db, project.id, service=FakeStructureNeo())
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_EMPTY  # fence 未动

    with pytest.raises(StructureProjectionRejected):
        rebuild_structure_projection(
            db, "no-such-ontology", service=FakeStructureNeo())

    class _InstanceService:
        def __init__(self, _db):
            pass

        def _rebuild_neo4j_projection(self, _oid):
            return True

    result = release_activation_service.rebuild_required_query_projections(
        db, project.id, mapping_service_factory=_InstanceService,
        structure_commit=True,
    )
    # 钩子如实报 rejected（非 failed），实例 ready 语义不受影响。
    assert result["ready"] is True
    assert result["structure"]["status"] == "rejected"
    row = db.query(OntologyProject).get(project.id)
    assert row.structure_projection_status == STATUS_EMPTY  # 仍未写 fence


def test_api_rebuild_rejected_returns_409(
        client, auth_headers, db, ontology_with_release):
    project, _release = ontology_with_release
    project.current_release_id = None
    db.commit()
    r = client.post(
        f"/api/v2/ontology-network/{project.id}/structure-projection/rebuild",
        headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "structure_projection_rejected"
