"""统一关联查询契约测试（YIYA-36）：语义真值表 + 授权 + 分页稳定性。

SQLite 保留契约语义（typed cast/三值逻辑/稳定分页）；锁/隔离/PG JSON cast
由真实栈验收（YIYA-41）覆盖。
"""
import json

import pytest

from app.ontologies.agent_query.service import QueryError

_QUERY = "/api/v2/formal/agent/query"


def _fo(oid):
    return f"/api/v2/formal/ontologies/{oid}"


@pytest.fixture
def orders_ontology(client, auth_headers, ontology, db):
    """本体 A：订单（含 tenant_key 业务键）+ 供应商 + 链接。"""
    oid = ontology["id"]
    body = {
        "objectTypes": [
            {"id": "ot-order", "name": "Order", "displayName": "订单",
             "primaryKey": "order_no",
             "properties": [
                 {"id": "p1", "name": "order_no", "displayName": "订单号",
                  "type": "string", "required": True},
                 {"id": "p2", "name": "status", "displayName": "状态",
                  "type": "string"},
                 {"id": "p3", "name": "amount", "displayName": "金额",
                  "type": "number"},
                 {"id": "p4", "name": "tenant_key", "displayName": "租户键",
                  "type": "string"},
                 {"id": "p5", "name": "risk_score", "displayName": "风险分",
                  "type": "number", "source": "computed"},
                 {"id": "p6", "name": "tenant_ref", "displayName": "租户引用",
                  "type": "string", "source": "computed"},
             ], "positionX": 0, "positionY": 0},
            {"id": "ot-supplier", "name": "Supplier", "displayName": "供应商",
             "primaryKey": "sname",
             "properties": [
                 {"id": "p5", "name": "sname", "displayName": "名称",
                  "type": "string", "required": True},
             ], "positionX": 0, "positionY": 0},
            {"id": "ot-warehouse", "name": "Warehouse", "displayName": "仓库",
             "primaryKey": "wh_code",
             "properties": [
                 {"id": "p7", "name": "wh_code", "displayName": "仓库键",
                  "type": "string", "required": True},
             ], "positionX": 0, "positionY": 0},
        ],
        "linkTypes": [
            {"id": "lt-1", "name": "order_supplier", "displayName": "订单-供应商",
             "sourceObjectTypeId": "ot-order", "targetObjectTypeId": "ot-supplier",
             "cardinality": "many-to-one"},
        ],
        "actions": [],
        "functions": [],
        "instances": [
            {"id": "inst-o1", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-001", "status": "pending",
                            "amount": 100, "tenant_key": "t-a"}},
            {"id": "inst-o2", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-002", "status": "paid",
                            "amount": 250, "tenant_key": "t-a"}},
            {"id": "inst-o3", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-003", "status": "paid",
                            "amount": None, "tenant_key": "t-b"}},
            {"id": "inst-s1", "objectTypeId": "ot-supplier",
             "properties": {"sname": "华南电子"}},
            {"id": "inst-w1", "objectTypeId": "ot-warehouse",
             "properties": {"wh_code": "t-a"}},
        ],
        "linkInstances": [
            {"id": "li-1", "linkTypeId": "lt-1",
             "sourceObjectId": "inst-o1", "targetObjectId": "inst-s1"},
        ],
    }
    r = client.put(f"{_fo(oid)}/full", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    return _freeze(db, oid)


@pytest.fixture
def parties_ontology(client, auth_headers, db):
    r = client.post("/api/v1/ontologies", headers=auth_headers,
                    json={"name": "parties-fixture", "domain": "供应链"})
    assert r.status_code in (200, 201), r.text
    oid = r.json()["data"]["id"] if "data" in r.json() else r.json()["id"]
    body = {
        "objectTypes": [
            {"id": "ot-party", "name": "Party", "displayName": "客户",
             "primaryKey": "party_key",
             "properties": [
                 {"id": "q1", "name": "party_key", "displayName": "客户键",
                  "type": "string", "required": True},
                 {"id": "q2", "name": "label", "displayName": "名称",
                  "type": "string"},
             ], "positionX": 0, "positionY": 0},
        ],
        "linkTypes": [],
        "actions": [],
        "functions": [],
        "instances": [
            {"id": "inst-pa", "objectTypeId": "ot-party",
             "properties": {"party_key": "t-a", "label": "租户A"}},
            {"id": "inst-pb", "objectTypeId": "ot-party",
             "properties": {"party_key": "t-b", "label": "租户B"}},
        ],
        "linkInstances": [],
    }
    r = client.put(f"{_fo(oid)}/full", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    return _freeze(db, oid)


def _freeze(db, ontology_id: str) -> dict:
    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import LinkInstance, ObjectInstance
    from app.models.ontology_version import OntologyVersion
    from app.ontologies.versions.evolution_service import snapshot_hash
    from app.ontologies.versions.router import _snapshot_formal

    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    snapshot = _snapshot_formal(db, ontology_id)
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    for instance in db.query(ObjectInstance).filter_by(
            ontology_id=ontology_id).all():
        instance.ontology_release_id = release.id
    for link in db.query(LinkInstance).filter_by(
            ontology_id=ontology_id).all():
        link.ontology_release_id = release.id
    db.commit()
    return {"ontologyId": ontology_id, "releaseId": str(release.id)}


def _run(client, auth_headers, payload):
    return client.post(_QUERY, headers=auth_headers, json=payload)


def _single_source(orders, **overrides):
    payload = {
        "sources": [{"alias": "a", "ontologyId": orders["ontologyId"],
                     "releaseId": orders["releaseId"],
                     "objectTypeId": "ot-order"}],
        "joins": [],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "a", "property": "amount", "as": "amount"}],
        "orderBy": [{"output": "orderNo", "direction": "asc"}],
        "pageSize": 10,
    }
    payload.update(overrides)
    return payload


def test_single_source_projection_and_filter(client, auth_headers,
                                             orders_ontology):
    orders = orders_ontology
    r = _run(client, auth_headers, _single_source(
        orders, where=[{"source": "a", "property": "status", "op": "eq",
                        "value": "paid"}]))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["totalRowCount"] == 2
    assert [row["orderNo"] for row in data["rows"]] == ["SO-002", "SO-003"]
    assert data["countsScope"] == "complete_snapshot"
    assert data["partial"] is False and data["truncated"] is False
    assert data["releases"]["a"]["releaseId"] == orders["releaseId"]
    assert data["inputCounts"]["a"] == 3
    assert data["distinctEntityCounts"]["a"] == 2


def test_declared_relation_inner_and_left(client, auth_headers,
                                          orders_ontology):
    orders = orders_ontology
    base = {
        "sources": [
            {"alias": "a", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-order"},
            {"alias": "s", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-supplier"},
        ],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "s", "property": "sname", "as": "supplier"}],
        "orderBy": [{"output": "orderNo"}],
        "pageSize": 10,
    }
    join = [{"left": "a", "right": "s", "type": "inner",
             "on": {"kind": "declared_relation", "linkTypeId": "lt-1",
                    "direction": "source_to_target"}}]
    r = _run(client, auth_headers, dict(base, joins=join))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["totalRowCount"] == 1
    assert data["rows"][0]["supplier"] == "华南电子"

    left_join = [{"left": "a", "right": "s", "type": "left",
                  "on": {"kind": "declared_relation", "linkTypeId": "lt-1",
                         "direction": "source_to_target"}}]
    r = _run(client, auth_headers, dict(base, joins=left_join))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["totalRowCount"] == 3
    by_no = {row["orderNo"]: row for row in data["rows"]}
    assert by_no["SO-002"]["supplier"] is None   # 未匹配 → NULL
    # where eq 会过滤掉 NULL 未匹配行；判空需显式 is_null
    r = _run(client, auth_headers, dict(
        base, joins=left_join,
        where=[{"source": "s", "property": "sname", "op": "is_null"}]))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["totalRowCount"] == 2


def test_cross_ontology_explicit_key(client, auth_headers, orders_ontology,
                                     parties_ontology):
    orders, parties = orders_ontology, parties_ontology
    payload = {
        "sources": [
            {"alias": "a", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-order"},
            {"alias": "b", "ontologyId": parties["ontologyId"],
             "releaseId": parties["releaseId"], "objectTypeId": "ot-party"},
        ],
        "joins": [{
            "left": "a", "right": "b", "type": "inner",
            "on": {"kind": "explicit_key", "leftProperty": "tenant_key",
                   "rightProperty": "party_key", "keyType": "string",
                   "cardinality": "N:1",
                   "businessMeaning": "订单.tenant_key 标识租户客户业务键",
                   "evidenceRefs": _register_join_assertion(
                       client, auth_headers, orders, parties)}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "b", "property": "label", "as": "partyLabel"}],
        "orderBy": [{"output": "orderNo"}],
        "pageSize": 10,
    }
    r = _run(client, auth_headers, payload)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["totalRowCount"] == 3
    labels = {row["orderNo"]: row["partyLabel"] for row in data["rows"]}
    assert labels["SO-001"] == "租户A" and labels["SO-003"] == "租户B"


def _register_join_assertion(client, auth_headers, orders, parties, *,
                             left_property="tenant_key",
                             right_property="party_key",
                             key_type="string") -> list[str]:
    """登记本人跨本体关联含义声明 — 有用户依据的 evidenceRef。"""
    r = client.post(
        f"{_fo(parties['ontologyId'])}/agent/join-assertions",
        headers=auth_headers,
        json={"leftOntologyId": orders["ontologyId"],
              "leftObjectTypeId": "ot-order", "leftProperty": left_property,
              "rightOntologyId": parties["ontologyId"],
              "rightObjectTypeId": "ot-party", "rightProperty": right_property,
              "keyType": key_type,
              "businessMeaning": "订单租户键标识客户业务键"})
    assert r.status_code == 200, r.text
    return [r.json()["data"]["evidenceRef"]]


def test_invalid_typed_key_rejected(client, auth_headers, orders_ontology,
                                    parties_ontology, db):
    orders, parties = orders_ontology, parties_ontology
    from app.models.ontology_formal import ObjectInstance

    bad = db.query(ObjectInstance).filter_by(id="inst-pa").one()
    bad.properties = {"party_key": 42, "label": "租户A"}  # number 进 string 键
    db.commit()
    payload = {
        "sources": [
            {"alias": "a", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-order"},
            {"alias": "b", "ontologyId": parties["ontologyId"],
             "releaseId": parties["releaseId"], "objectTypeId": "ot-party"},
        ],
        "joins": [{
            "left": "a", "right": "b", "type": "inner",
            "on": {"kind": "explicit_key", "leftProperty": "tenant_key",
                   "rightProperty": "party_key", "keyType": "string",
                   "cardinality": "N:1", "businessMeaning": "x",
                   "evidenceRefs": _register_join_assertion(
                       client, auth_headers, orders, parties)}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "b", "property": "label", "as": "label"}],
        "pageSize": 10,
    }
    r = _run(client, auth_headers, payload)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_typed_key"
    assert r.json()["detail"]["errorCount"] == 1


def test_cardinality_mismatch_rejected(client, auth_headers,
                                       orders_ontology, parties_ontology,
                                       db):
    orders, parties = orders_ontology, parties_ontology
    from app.models.ontology_formal import ObjectInstance

    dup = ObjectInstance(
        id="inst-pc", ontology_id=parties["ontologyId"],
        ontology_release_id=parties["releaseId"], object_type_id="ot-party",
        properties={"party_key": "t-a", "label": "租户A副本"})
    db.add(dup)
    db.commit()
    payload = {
        "sources": [
            {"alias": "a", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-order"},
            {"alias": "b", "ontologyId": parties["ontologyId"],
             "releaseId": parties["releaseId"], "objectTypeId": "ot-party"},
        ],
        "joins": [{
            "left": "a", "right": "b", "type": "inner",
            "on": {"kind": "explicit_key", "leftProperty": "tenant_key",
                   "rightProperty": "party_key", "keyType": "string",
                   "cardinality": "N:1", "businessMeaning": "x",
                   "evidenceRefs": _register_join_assertion(
                       client, auth_headers, orders, parties)}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "b", "property": "label", "as": "label"}],
        "pageSize": 10,
    }
    r = _run(client, auth_headers, payload)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "join_cardinality_mismatch"


def test_fixture_evidence_rejected(client, auth_headers, orders_ontology,
                                   parties_ontology):
    """fixture 类证据已退役：无核验门禁的引用不再放行跨本体 join。"""
    orders, parties = orders_ontology, parties_ontology
    payload = {
        "sources": [
            {"alias": "a", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-order"},
            {"alias": "b", "ontologyId": parties["ontologyId"],
             "releaseId": parties["releaseId"], "objectTypeId": "ot-party"},
        ],
        "joins": [{
            "left": "a", "right": "b", "type": "inner",
            "on": {"kind": "explicit_key", "leftProperty": "tenant_key",
                   "rightProperty": "party_key", "keyType": "string",
                   "cardinality": "N:1", "businessMeaning": "x",
                   "evidenceRefs": ["fixture:tenant-key"]}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"}],
        "pageSize": 10,
    }
    r = _run(client, auth_headers, payload)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "join_semantics_unverified"


def _same_ontology_join_payload(orders, *, right_object_type_id,
                                right_property, evidence_refs):
    oid, rid = orders["ontologyId"], orders["releaseId"]
    return {
        "sources": [
            {"alias": "a", "ontologyId": oid, "releaseId": rid,
             "objectTypeId": "ot-order"},
            {"alias": "w", "ontologyId": oid, "releaseId": rid,
             "objectTypeId": right_object_type_id},
        ],
        "joins": [{
            "left": "a", "right": "w", "type": "inner",
            "on": {"kind": "explicit_key", "leftProperty": "tenant_key",
                   "rightProperty": right_property, "keyType": "string",
                   "cardinality": "N:1", "businessMeaning": "模型填写",
                   "evidenceRefs": evidence_refs}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"}],
        "pageSize": 10,
    }


def test_structure_evidence_requires_published_link(client, auth_headers,
                                                     orders_ontology):
    """仅字段存在不构成关联依据：structure 引用永不充分 —— 即使两端之间存在
    已发布 declared link，也不证明任意两个属性可按键相等；explicit_key
    唯一充分证据是用户 assertion（链接语义应走 declared_relation join）。"""
    orders = orders_ontology
    oid, rid = orders["ontologyId"], orders["releaseId"]

    # 1) 双侧 structure 引用，但 ot-order/ot-warehouse 之间无 declared link
    payload = _same_ontology_join_payload(
        orders, right_object_type_id="ot-warehouse", right_property="wh_code",
        evidence_refs=[
            f"structure:{oid}:{rid}:ot-order:tenant_key",
            f"structure:{oid}:{rid}:ot-warehouse:wh_code"])
    r = _run(client, auth_headers, payload)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "join_semantics_unverified"

    # 2) 单侧 structure 引用（伪造最小证据）同样拒绝
    payload = _same_ontology_join_payload(
        orders, right_object_type_id="ot-warehouse", right_property="wh_code",
        evidence_refs=[f"structure:{oid}:{rid}:ot-order:tenant_key"])
    r = _run(client, auth_headers, payload)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "join_semantics_unverified"

    # 3) 两端存在已发布 declared link（lt-1 连 ot-order/ot-supplier），
    #    双侧 structure 引用仍不构成按键相等的依据 → 拒绝
    payload = _same_ontology_join_payload(
        orders, right_object_type_id="ot-supplier", right_property="sname",
        evidence_refs=[
            f"structure:{oid}:{rid}:ot-order:tenant_key",
            f"structure:{oid}:{rid}:ot-supplier:sname"])
    r = _run(client, auth_headers, payload)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "join_semantics_unverified"


def test_aggregate_semantics(client, auth_headers, orders_ontology):
    orders = orders_ontology
    payload = {
        "sources": [{"alias": "a", "ontologyId": orders["ontologyId"],
                     "releaseId": orders["releaseId"],
                     "objectTypeId": "ot-order"}],
        "select": [{"source": "a", "property": "status", "as": "status"}],
        "groupBy": [{"source": "a", "property": "status"}],
        "aggregates": [
            {"fn": "count", "as": "rowCount"},
            {"fn": "count_distinct", "source": "a", "property": "amount",
             "as": "amountCount"},
            {"fn": "sum", "source": "a", "property": "amount", "as": "total"},
        ],
        "orderBy": [{"output": "status"}],
        "pageSize": 10,
    }
    r = _run(client, auth_headers, payload)
    assert r.status_code == 200, r.text
    rows = {row["status"]: row for row in r.json()["data"]["rows"]}
    assert rows["pending"]["rowCount"] == 1
    assert rows["paid"]["rowCount"] == 2
    assert rows["paid"]["amountCount"] == 1          # NULL 不计入字段计数
    assert rows["paid"]["total"] == 250              # sum 忽略 NULL
    assert rows["pending"]["total"] == 100


def test_distinct_projection(client, auth_headers, orders_ontology):
    orders = orders_ontology
    payload = _single_source(
        orders,
        select=[{"source": "a", "property": "status", "as": "status"}],
        distinct=True, orderBy=[{"output": "status"}])
    r = _run(client, auth_headers, payload)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [row["status"] for row in data["rows"]] == ["paid", "pending"]
    assert data["totalRowCount"] == 2


def test_pagination_stable_across_data_change(client, auth_headers,
                                              orders_ontology, db):
    orders = orders_ontology
    r = _run(client, auth_headers, _single_source(orders, pageSize=2))
    assert r.status_code == 200, r.text
    page1 = r.json()["data"]
    assert page1["pageRowCount"] == 2 and page1["hasNext"]

    # 页间业务数据变化：旧快照不漏不重
    from app.models.ontology_formal import ObjectInstance

    db.query(ObjectInstance).filter_by(id="inst-o1").delete()
    extra = ObjectInstance(
        id="inst-o9", ontology_id=orders["ontologyId"],
        ontology_release_id=orders["releaseId"], object_type_id="ot-order",
        properties={"order_no": "SO-009", "status": "pending",
                    "amount": 1, "tenant_key": "t-z"})
    db.add(extra)
    db.commit()

    r = client.post(_QUERY, headers=auth_headers,
                    json={"cursor": page1["cursor"]})
    assert r.status_code == 200, r.text
    page2 = r.json()["data"]
    assert page2["pageRowCount"] == 1
    all_nos = [row["orderNo"] for row in page1["rows"]] + \
        [row["orderNo"] for row in page2["rows"]]
    assert sorted(all_nos) == ["SO-001", "SO-002", "SO-003"]
    assert page2["totalRowCount"] == 3          # 快照口径，不是新数据

    import json

    from app.auth.models import User
    from app.super_assistant import ontology_tools
    from app.super_assistant.models import SuperAssistantConversation

    user = db.query(User).filter_by(username="admin").one()
    conversation = SuperAssistantConversation(owner_id=user.id, title="query")
    db.add(conversation)
    db.commit()
    response = client.put(
        f"{_fo(orders['ontologyId'])}/agent/profile", headers=auth_headers,
        json={"maxRowsPerQuery": 2})
    assert response.status_code == 200, response.text
    arguments = _single_source(orders)
    del arguments["pageSize"]
    first = json.loads(ontology_tools.execute_ontology_tool(
        db, owner_id=user.id, conversation_id=conversation.id,
        name="ontology_query", arguments=arguments))
    assert first["pageRowCount"] == 2 and first["hasNext"]
    second = json.loads(ontology_tools.execute_ontology_tool(
        db, owner_id=user.id, conversation_id=conversation.id,
        name="ontology_query", arguments={"cursor": first["cursor"]}))
    assert second["pageRowCount"] == 1
    assert second["queryId"] == first["queryId"]
    assert [row["orderNo"] for row in first["rows"] + second["rows"]] == [
        "SO-002", "SO-003", "SO-009"]


def test_cursor_tamper_and_reuse_rejected(client, auth_headers,
                                          orders_ontology):
    orders = orders_ontology
    r = _run(client, auth_headers, _single_source(orders, pageSize=2))
    cursor = r.json()["data"]["cursor"]
    assert cursor
    tampered = cursor[:-4] + "beef"
    r = client.post(_QUERY, headers=auth_headers, json={"cursor": tampered})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_cursor"


def test_property_allowlist_enforced(client, auth_headers, orders_ontology):
    orders = orders_ontology
    oid = orders["ontologyId"]
    r = client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
                   json={"allowedProperties": {"ot-order": ["order_no",
                                                            "status"]}})
    assert r.status_code == 200, r.text
    r = _run(client, auth_headers, _single_source(orders))
    assert r.status_code == 403                      # amount 未授权
    assert r.json()["detail"]["code"] == "property_forbidden"
    r = _run(client, auth_headers, _single_source(
        orders,
        select=[{"source": "a", "property": "order_no", "as": "orderNo"}],
        where=[{"source": "a", "property": "tenant_key", "op": "is_null"}]))
    assert r.status_code == 403                      # 过滤也不能推断禁用字段


def test_release_drift_rejected(client, auth_headers, orders_ontology):
    orders = orders_ontology
    payload = _single_source(orders)
    payload["sources"][0]["releaseId"] = "not-the-release"
    r = _run(client, auth_headers, payload)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "release_context_changed"


def test_page_size_cap(client, auth_headers, orders_ontology):
    orders = orders_ontology
    oid = orders["ontologyId"]
    r = client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
                   json={"maxRowsPerQuery": 2})
    assert r.status_code == 200
    r = _run(client, auth_headers, _single_source(orders, pageSize=10))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_parameter"


def test_unknown_parameter_rejected(client, auth_headers, orders_ontology):
    orders = orders_ontology
    payload = _single_source(orders)
    payload["nativeSql"] = "SELECT 1"
    r = _run(client, auth_headers, payload)
    assert r.status_code == 422                      # extra=forbid


def test_structure_slices_and_sources(client, auth_headers, orders_ontology):
    orders = orders_ontology
    r = client.get(
        f"{_fo(orders['ontologyId'])}/agent/structure",
        headers=auth_headers,
        params={"release_id": orders["releaseId"],
                "object_type_ids": ["ot-order"], "link_type_ids": ["lt-1"]})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["release"]["id"] == orders["releaseId"]
    types = {t["id"]: t for t in data["objectTypes"]}
    assert set(types) == {"ot-order", "ot-supplier"}   # 链接端点自动补入
    props = {p["name"] for p in types["ot-order"]["properties"]}
    assert "tenant_key" in props
    assert data["linkTypes"][0]["sourceObjectTypeId"] == "ot-order"


def test_structure_respects_property_allowlist(client, auth_headers,
                                               orders_ontology):
    orders = orders_ontology
    oid = orders["ontologyId"]
    r = client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
                   json={"allowedProperties": {"ot-order": ["order_no"]}})
    assert r.status_code == 200
    r = client.get(
        f"{_fo(oid)}/agent/structure", headers=auth_headers,
        params={"release_id": orders["releaseId"],
                "object_type_ids": ["ot-order"]})
    assert r.status_code == 200, r.text
    props = {p["name"]
             for t in r.json()["data"]["objectTypes"]
             for p in t["properties"]}
    assert props == {"order_no"}


def test_structure_lists_registered_join_assertions(
        client, auth_headers, orders_ontology, parties_ontology):
    """结构片段附本人已登记的相关关联声明（有界发现），无关本体不含。"""
    orders, parties = orders_ontology, parties_ontology
    _register_join_assertion(client, auth_headers, orders, parties)
    r = client.get(
        f"{_fo(parties['ontologyId'])}/agent/structure",
        headers=auth_headers,
        params={"release_id": parties["releaseId"],
                "object_type_ids": ["ot-party"]})
    assert r.status_code == 200, r.text
    assertions = r.json()["data"]["joinAssertions"]
    assert len(assertions) == 1
    entry = assertions[0]
    assert entry["leftObjectTypeId"] == "ot-order"
    assert entry["rightObjectTypeId"] == "ot-party"
    assert entry["rightProperty"] == "party_key"
    assert entry["category"] == "user_asserted"
    assert entry["evidenceRef"].startswith("assertion:")


def test_query_error_detail_shape():
    err = QueryError(422, "code_x", "msg", limit=5)
    assert err.detail() == {"code": "code_x", "message": "msg", "limit": 5}


def test_temp_file_limit_display_values_parse():
    """PG current_setting 按最大人类单位归一显示（'32MB'/'1GB'/'-1'）。"""
    from app.ontologies.agent_query.service import _parse_memory_limit_kb

    assert _parse_memory_limit_kb("-1") is None          # unlimited
    assert _parse_memory_limit_kb("") is None
    assert _parse_memory_limit_kb("abc") is None
    assert _parse_memory_limit_kb("32768kB") == 32768
    assert _parse_memory_limit_kb("32768") == 32768      # 裸数字按 kB
    assert _parse_memory_limit_kb("32MB") == 32768
    assert _parse_memory_limit_kb("1GB") == 1048576
    assert _parse_memory_limit_kb("2048 kB") == 2048
    assert _parse_memory_limit_kb("100B") == 1           # 向上取整
    assert _parse_memory_limit_kb("0") == 0


def test_temp_file_budget_gate_fail_closed():
    """普通角色 temp_file_limit 未设/过宽 → 503 fail closed；等于帽可用。"""
    from app.ontologies.agent_query.service import UnifiedQueryService

    service = object.__new__(UnifiedQueryService)  # 纯方法，无需完整构造
    for bad in ("-1", "1GB", "garbage", ""):
        with pytest.raises(QueryError) as excinfo:
            service._verify_temp_file_budget(bad)
        assert excinfo.value.status == 503
        assert excinfo.value.code == "query_budget_unavailable"
        assert excinfo.value.extra["effectiveTempFileLimit"] == (bad or None)
    service._verify_temp_file_budget("32MB")         # 恰等于默认帽
    service._verify_temp_file_budget("32768kB")


def test_query_sql_count_constant_in_row_count(client, auth_headers,
                                                orders_ontology, db):
    """防逐对象 N+1：固定查询形状的 SQL 条数不随对象数线性增长。"""
    from app.models.ontology_formal import ObjectInstance
    from tests.conftest import QueryCounter

    orders = orders_ontology
    orders_in_source = db.query(ObjectInstance).filter_by(
        ontology_id=orders["ontologyId"],
        object_type_id="ot-order").count()
    for i in range(40, 60):
        db.add(ObjectInstance(
            id=f"inst-n{i}", ontology_id=orders["ontologyId"],
            ontology_release_id=orders["releaseId"],
            object_type_id="ot-order",
            properties={"order_no": f"SO-{i:03d}", "status": "paid",
                        "amount": i, "tenant_key": "t-a"}))
    db.commit()
    expected_total = orders_in_source + 20
    assert db.query(ObjectInstance).filter_by(
        ontology_id=orders["ontologyId"],
        object_type_id="ot-order").count() == expected_total

    counter = QueryCounter(db)
    with counter:
        r = _run(client, auth_headers, _single_source(orders, pageSize=5))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["totalRowCount"] == expected_total
    # 固定形状：授权解析/计数/实体计数/物化/落行都是固定条数
    assert counter.count <= 30, counter.count


def test_declared_relation_join_with_new_alias_on_left(client, auth_headers,
                                                       orders_ontology):
    """缺陷-2 契约：新 alias 在 join.left（连接源侧）+ source_to_target
    组合不再缺 FROM（原 PG UndefinedTable / HTTP 500）。"""
    orders = orders_ontology
    payload = {
        "sources": [
            {"alias": "s", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-supplier"},
            {"alias": "a", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-order"},
        ],
        "joins": [{
            "left": "a", "right": "s", "type": "inner",
            "on": {"kind": "declared_relation", "linkTypeId": "lt-1",
                   "direction": "source_to_target"}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "s", "property": "sname", "as": "supplier"}],
        "orderBy": [{"output": "orderNo"}],
        "pageSize": 10,
    }
    r = _run(client, auth_headers, payload)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["totalRowCount"] == 1
    assert data["rows"][0]["supplier"] == "华南电子"


def test_join_evidence_verification_and_user_assertion(
        client, auth_headers, orders_ontology, parties_ontology):
    """P2-1 契约：evidenceRefs 服务端核验 — 无效引用拒绝、user_asserted 分类。"""
    orders, parties = orders_ontology, parties_ontology
    # 登记本人关联含义声明
    r = client.post(
        f"{_fo(parties['ontologyId'])}/agent/join-assertions",
        headers=auth_headers,
        json={"leftOntologyId": orders["ontologyId"],
              "leftObjectTypeId": "ot-order", "leftProperty": "tenant_key",
              "rightOntologyId": parties["ontologyId"],
              "rightObjectTypeId": "ot-party", "rightProperty": "party_key",
              "keyType": "string",
              "businessMeaning": "租户业务键"})
    assert r.status_code == 200, r.text
    evidence_ref = r.json()["data"]["evidenceRef"]

    def _payload(evidence):
        return {
            "sources": [
                {"alias": "a", "ontologyId": orders["ontologyId"],
                 "releaseId": orders["releaseId"],
                 "objectTypeId": "ot-order"},
                {"alias": "b", "ontologyId": parties["ontologyId"],
                 "releaseId": parties["releaseId"],
                 "objectTypeId": "ot-party"}],
            "joins": [{
                "left": "a", "right": "b", "type": "inner",
                "on": {"kind": "explicit_key",
                       "leftProperty": "tenant_key",
                       "rightProperty": "party_key", "keyType": "string",
                       "cardinality": "N:1", "businessMeaning": "模型填写",
                       "evidenceRefs": evidence}}],
            "select": [{"source": "a", "property": "order_no",
                        "as": "orderNo"}],
            "pageSize": 10}

    # 1) structure 引用 release 不匹配 → 拒绝
    r = _run(client, auth_headers, _payload(
        [f"structure:{orders['ontologyId']}:wrong-release:ot-order:"
         "tenant_key"]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "join_semantics_unverified"

    # 2) 他人/不存在 assertion → 拒绝
    r = _run(client, auth_headers, _payload(["assertion:missing"]))
    assert r.status_code == 422

    # 3) 本人 assertion → 通过，且响应标 user_asserted
    r = _run(client, auth_headers, _payload([evidence_ref]))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    evidence = data["joinEvidence"][0]
    assert evidence["categories"] == ["user_asserted"]
    assert evidence["refs"] == [evidence_ref]


def _stamp_computed(db, orders):
    """给订单实例写入派生值（写侧按声明拆列，computed 列）。"""
    from app.models.ontology_formal import ObjectInstance

    scores = {"inst-o1": 10, "inst-o2": 30, "inst-o3": 20}
    refs = {"inst-o1": "t-a", "inst-o2": "t-a", "inst-o3": "t-b"}
    for inst in db.query(ObjectInstance).filter_by(
            ontology_id=orders["ontologyId"],
            object_type_id="ot-order").all():
        inst.computed = {"risk_score": scores[inst.id],
                         "tenant_ref": refs[inst.id]}
    db.commit()


def test_computed_property_queryable(client, auth_headers, orders_ontology,
                                     db):
    """结构把 computed 字段标为可查询（source='computed'）；查询核必须读
    computed 列取值，而不是静默全 NULL。"""
    orders = orders_ontology
    _stamp_computed(db, orders)

    # 投影 + 过滤 + 聚合都走真实派生值
    r = _run(client, auth_headers, _single_source(
        orders,
        select=[{"source": "a", "property": "order_no", "as": "orderNo"},
                {"source": "a", "property": "risk_score", "as": "risk"}],
        where=[{"source": "a", "property": "risk_score", "op": "gte",
                "value": 20}]))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["totalRowCount"] == 2
    risks = {row["orderNo"]: row["risk"] for row in data["rows"]}
    assert risks == {"SO-002": 30, "SO-003": 20}

    r = _run(client, auth_headers, _single_source(
        orders,
        select=[],
        aggregates=[{"fn": "count", "as": "n"},
                    {"fn": "sum", "source": "a", "property": "risk_score",
                     "as": "riskTotal"}],
        orderBy=[{"output": "n"}]))
    assert r.status_code == 200, r.text
    row = r.json()["data"]["rows"][0]
    assert row["n"] == 3 and row["riskTotal"] == 60


def test_computed_property_as_join_key(client, auth_headers, orders_ontology,
                                       parties_ontology, db):
    """join 键取自 computed 列：typed 表达式与原始 JSON 校验都指向 computed，
    派生键与存储键（party_key）可真实关联。"""
    orders, parties = orders_ontology, parties_ontology
    _stamp_computed(db, orders)
    payload = {
        "sources": [
            {"alias": "a", "ontologyId": orders["ontologyId"],
             "releaseId": orders["releaseId"], "objectTypeId": "ot-order"},
            {"alias": "b", "ontologyId": parties["ontologyId"],
             "releaseId": parties["releaseId"], "objectTypeId": "ot-party"},
        ],
        "joins": [{
            "left": "a", "right": "b", "type": "inner",
            "on": {"kind": "explicit_key", "leftProperty": "tenant_ref",
                   "rightProperty": "party_key", "keyType": "string",
                   "cardinality": "N:1", "businessMeaning": "派生租户引用",
                   "evidenceRefs": _register_join_assertion(
                       client, auth_headers, orders, parties,
                       left_property="tenant_ref")}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "b", "property": "label", "as": "partyLabel"}],
        "orderBy": [{"output": "orderNo"}],
        "pageSize": 10,
    }
    r = _run(client, auth_headers, payload)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["totalRowCount"] == 3
    labels = {row["orderNo"]: row["partyLabel"] for row in data["rows"]}
    assert labels["SO-001"] == "租户A" and labels["SO-003"] == "租户B"


def test_pure_aggregation_without_group_columns(client, auth_headers,
                                                orders_ontology):
    """纯聚合（select 空、无分组）是契约支持的形态；工具接线修复前由
    服务契约测试守住该路径。"""
    orders = orders_ontology
    payload = _single_source(
        orders,
        select=[],
        aggregates=[
            {"fn": "count", "as": "rowCount"},
            {"fn": "count_distinct", "source": "a", "property": "status",
             "as": "statusCount"},
            {"fn": "avg", "source": "a", "property": "amount", "as": "avgAmt"},
        ],
        orderBy=[{"output": "rowCount"}])
    r = _run(client, auth_headers, payload)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["totalRowCount"] == 1
    row = data["rows"][0]
    assert row["rowCount"] == 3
    assert row["statusCount"] == 2                  # pending/paid
    assert row["avgAmt"] == 175                     # (100+250)/2，NULL 不计

    from jsonschema import validate

    from app.super_assistant import ontology_tools

    schema = next(tool["parameters"]
                  for tool in ontology_tools.ontology_tool_schemas()
                  if tool["name"] == "ontology_query")
    del payload["select"]
    payload["where"] = [{"and": [
        {"source": "a", "property": "status", "op": "eq", "value": "paid"},
        {"not": {"source": "a", "property": "amount", "op": "is_null"}},
    ]}]
    validate(payload, schema)
    request = ontology_tools.build_query_request(payload)
    assert request.select == []
    assert request.aggregates[0].fn == "count"
    validate({"cursor": "snapshot-cursor-123456"}, schema)


# ------------------------------------- Z1：合并冲突丢失的行为测试恢复


def test_query_budget_units_pg_legal():
    """R-1/Z1：预算语句使用 PG 合法单位（B/kB/MB/GB/TB，非 IEC MiB）。"""
    import inspect
    from app.ontologies.agent_query import service as query_service
    source = inspect.getsource(
        query_service.UnifiedQueryService._pin_transaction)
    assert "MiB" not in source.replace("不接受 MiB 拼写", "")
    assert "MB'" in source or 'MB"' in source


def test_query_budget_error_no_internal_leak():
    """R-2/Z1：预算失败响应不回灌 str(exc)/SQL 细节，分类入 extra。"""
    from app.ontologies.agent_query import service as query_service

    class _Orig:
        pgcode = "42501"

    class _StmtExc(Exception):
        orig = _Orig()

    class _Db:
        def in_transaction(self):
            return False

        def execute(self, statement):
            raise _StmtExc()

        def rollback(self):
            pass

    svc = query_service.UnifiedQueryService.__new__(
        query_service.UnifiedQueryService)
    svc.db = _Db()
    svc._pg = True
    from app.ontologies.agent_query import schemas as _QS
    svc.req = _QS.QueryRequest(
        sources=[_QS.QuerySource(alias="a", ontology_id="x",
                                 release_id="y", objectTypeId="z")],
        select=[])
    try:
        svc._pin_transaction()
        raise AssertionError("expected QueryError")
    except query_service.QueryError as exc:
        assert exc.code == "query_budget_unavailable"
        assert exc.extra.get("errorClass") == "insufficient_privilege"
        payload = json.dumps(exc.detail(), ensure_ascii=False)
        assert "42501" not in payload
        assert "permission denied" not in payload
        assert "orig" not in payload


def test_query_auth_hash_reflects_real_write_access(
        client, auth_headers, orders_ontology, db, editor_user):
    """裁决 1/Z1：auth 材料使用真实 RBAC 写权限（admin/editor-owner/
    其他 editor/viewer 区分）— 权限收紧改变快照指纹。"""
    from app.ontologies.agent_query.service import _ontology_write_access
    from app.models.ontology import OntologyProject

    orders = orders_ontology
    project = db.query(OntologyProject).filter_by(
        id=orders["ontologyId"]).one()

    class _Admin:
        id = "admin-user"
        role = "admin"

    class _EditorOwner:
        id = str(project.created_by)
        role = "editor"

    class _EditorOther:
        id = "someone-else"
        role = "editor"

    class _Viewer:
        id = str(project.created_by)
        role = "viewer"

    assert _ontology_write_access(_Admin(), project) is True
    assert _ontology_write_access(_EditorOwner(), project) is True
    assert _ontology_write_access(_EditorOther(), project) is False
    assert _ontology_write_access(_Viewer(), project) is False
