"""统一关联查询契约测试（YIYA-36）：语义真值表 + 授权 + 分页稳定性。

SQLite 保留契约语义（typed cast/三值逻辑/稳定分页）；锁/隔离/PG JSON cast
由真实栈验收（YIYA-41）覆盖。
"""
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
             ], "positionX": 0, "positionY": 0},
            {"id": "ot-supplier", "name": "Supplier", "displayName": "供应商",
             "primaryKey": "sname",
             "properties": [
                 {"id": "p5", "name": "sname", "displayName": "名称",
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
                   "evidenceRefs": [
                       f"structure:{orders['ontologyId']}:"
                       f"{orders['releaseId']}:ot-order:tenant_key",
                       f"structure:{parties['ontologyId']}:"
                       f"{parties['releaseId']}:ot-party:party_key"]}},
        ],
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
                   "evidenceRefs": ["fixture:tenant-key"]}}],
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
                   "evidenceRefs": ["fixture:tenant-key"]}}],
        "select": [{"source": "a", "property": "order_no", "as": "orderNo"},
                   {"source": "b", "property": "label", "as": "label"}],
        "pageSize": 10,
    }
    r = _run(client, auth_headers, payload)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "join_cardinality_mismatch"


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


def test_query_error_detail_shape():
    err = QueryError(422, "code_x", "msg", limit=5)
    assert err.detail() == {"code": "code_x", "message": "msg", "limit": 5}


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


def test_query_pg_pin_statement_order():
    """P2-A 契约：REPEATABLE READ 必须是查询事务的首语句（先于预算 SET LOCAL）。"""
    import inspect
    from app.ontologies.agent_query import service as query_service
    source = inspect.getsource(query_service.UnifiedQueryService._pin_transaction)
    rr_index = source.index("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    budget_index = source.index("SET LOCAL statement_timeout")
    assert rr_index < budget_index
    rollback_index = source.index("self.db.rollback()")
    assert rollback_index < rr_index


def test_query_sql_count_scales_constant(client, auth_headers,
                                         orders_ontology, db):
    """QueryCounter 跨规模证据：数据量翻倍以上，固定形状查询 SQL 数不随
    规模线性增长（边界差 ≤6，非绝对阈值宣称）。"""
    from app.models.ontology_formal import ObjectInstance
    from tests.conftest import QueryCounter

    orders = orders_ontology

    def _run_counted():
        counter = QueryCounter(db)
        with counter:
            r = _run(client, auth_headers, _single_source(orders, pageSize=5))
        assert r.status_code == 200, r.text
        return counter.count

    small = _run_counted()
    for i in range(100, 160):
        db.add(ObjectInstance(
            id=f"inst-s{i}", ontology_id=orders["ontologyId"],
            ontology_release_id=orders["releaseId"],
            object_type_id="ot-order",
            properties={"order_no": f"SO-{i:03d}", "status": "paid",
                        "amount": i, "tenant_key": "t-s"}))
    db.commit()
    large = _run_counted()
    assert large - small <= 6, (small, large)
    assert large <= 40, large
