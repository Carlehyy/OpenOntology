"""MCP 本体只读工具服务层契约测试。

覆盖：schema bootstrap/按类型分页（预算内不静默截断、关系 afterRelation
keyset 推进）、授权收敛（隐藏对象/属性、游标授权指纹）、actions 不暴露、
release 漂移 409（附当前版本与重读指引，含查询后续页路径）、query 必填
releaseId、SPL/跨本体源不可表达、结构树查询（明细/过滤/排序/聚合/同本体
关系 join）、分页与终页快照回收（配额共享）、跨入口/跨本体 cursor 拒绝、
远程路径版本元数据精确回填与句柄取消。
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.ontologies.agent_query.service import execute_unified_query
from app.ontologies.mcp import service as mcp_service
from app.ontologies.mcp.service import McpError


@pytest.fixture
def mcp_ontology(client, auth_headers, db):
    """本体：订单/供应商 + 多条声明链接 + 一个动作（断言动作不外泄）。"""
    r = client.post("/api/v1/ontologies", headers=auth_headers,
                    json={"name": "mcp-fixture", "domain": "供应链"})
    oid = r.json()["data"]["id"]
    body = {
        "objectTypes": [
            {"id": "ot-order", "name": "Order", "displayName": "订单",
             "description": "销售订单事实", "primaryKey": "order_no",
             "properties": [
                 {"id": "p1", "name": "order_no", "displayName": "订单号",
                  "type": "string", "required": True,
                  "description": "业务唯一单号"},
                 {"id": "p2", "name": "status", "displayName": "状态",
                  "type": "string", "description": "pending|paid"},
                 {"id": "p3", "name": "amount", "displayName": "金额",
                  "type": "number"},
                 {"id": "p4", "name": "secret_note", "displayName": "内部备注",
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
            {"id": "lt-1", "name": "order_supplier",
             "displayName": "订单-供应商", "description": "供货关系",
             "sourceObjectTypeId": "ot-order",
             "targetObjectTypeId": "ot-supplier",
             "cardinality": "many-to-one"},
            {"id": "lt-2", "name": "order_supplier_backup",
             "displayName": "订单-备选供应商",
             "sourceObjectTypeId": "ot-order",
             "targetObjectTypeId": "ot-supplier",
             "cardinality": "many-to-many"},
            {"id": "lt-3", "name": "supplier_order_history",
             "displayName": "供应商-历史订单",
             "sourceObjectTypeId": "ot-supplier",
             "targetObjectTypeId": "ot-order",
             "cardinality": "one-to-many"},
        ],
        "actions": [
            {"id": "act-1", "name": "cancel_order", "displayName": "取消订单",
             "objectTypeId": "ot-order",
             "parameters": [
                 {"name": "order_no", "type": "string", "required": True}],
             "requiresApproval": True},
        ],
        "functions": [],
        "instances": [
            {"id": "inst-o1", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-001", "status": "pending",
                            "amount": 100, "secret_note": "x"}},
            {"id": "inst-o2", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-002", "status": "paid",
                            "amount": 250}},
            {"id": "inst-o3", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-003", "status": "paid",
                            "amount": 50}},
            {"id": "inst-s1", "objectTypeId": "ot-supplier",
             "properties": {"sname": "华南电子"}},
        ],
        "linkInstances": [
            {"id": "li-1", "linkTypeId": "lt-1",
             "sourceObjectId": "inst-o1", "targetObjectId": "inst-s1"},
        ],
    }
    r = client.put(f"/api/v2/formal/ontologies/{oid}/full",
                   headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    return _freeze(db, oid)


def _freeze(db, ontology_id: str) -> dict:
    from app.ontologies.projects.models import OntologyProject
    from app.models.ontology_formal import LinkInstance, ObjectInstance
    from app.ontologies.versions.models import OntologyVersion
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
    return {"ontologyId": ontology_id, "releaseId": str(release.id),
            "versionNumber": str(release.version_number)}


def _promote_release(db, ontology_id: str) -> dict:
    """模拟一次发布晋级：新建 released 版本并移动 current 指针。"""
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.versions.models import OntologyVersion
    from app.ontologies.versions.snapshot_contract import (
        complete_snapshot, next_release_number, snapshot_hash,
    )

    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    old = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    frozen = complete_snapshot(old.snapshot_formal)
    new = OntologyVersion(
        id="rel-promoted-1", ontology_id=ontology_id,
        version_number=next_release_number(old.version_number),
        version_label="mcp-drift", description="drift",
        base_release_id=old.base_release_id or old.id,
        node_kind="release", lifecycle_status="released", revision=1,
        snapshot_formal=frozen, snapshot_hash=snapshot_hash(frozen),
        created_by="admin",
    )
    db.add(new)
    db.flush()
    project.current_release_id = new.id
    project.version = new.version_number
    db.commit()
    return {"releaseId": new.id, "versionNumber": new.version_number}


def _query_payload(fx, **tree):
    body = {"sources": [{"alias": "s0", "objectTypeId": "ot-order"}],
             "select": [{"source": "s0", "property": "order_no",
                         "as": "orderNo"},
                        {"source": "s0", "property": "amount", "as": "amount"}],
             "orderBy": [{"output": "orderNo", "direction": "asc"}],
             "pageSize": 2}
    body.update(tree)
    return {"releaseId": fx["releaseId"], "query": body}


def _read_all_type_pages(db, user, oid, **kwargs):
    pages = []
    cursor = None
    while True:
        payload = {"objectTypeId": "ot-order"}
        if cursor:
            payload = {"cursor": cursor}
        out = mcp_service.describe_schema(db, user, oid, payload)
        pages.append(out)
        cursor = out["cursor"]
        if not cursor:
            return pages


# ------------------------------------------------------------- schema 工具

def test_schema_bootstrap_without_arguments(db, admin_user, mcp_ontology,
                                             monkeypatch):
    out = mcp_service.describe_schema(db, admin_user,
                                      mcp_ontology["ontologyId"], {})
    assert out["ontologyId"] == mcp_ontology["ontologyId"]
    assert out["releaseId"] == mcp_ontology["releaseId"]
    assert out["versionNumber"] == mcp_ontology["versionNumber"]
    assert out["observedAt"]
    ids = {item["objectTypeId"] for item in out["objectTypes"]}
    assert ids == {"ot-order", "ot-supplier"}
    order = next(item for item in out["objectTypes"]
                 if item["objectTypeId"] == "ot-order")
    assert order["propertyCount"] == 4
    assert order["instanceMode"] == "materialized"
    assert order["description"] == "销售订单事实"
    # 首页附确定性查询说明（可保存 Markdown，固定模板不含任意标题）
    assert "ontology_query" in out["queryGuide"]
    assert mcp_ontology["releaseId"] in out["queryGuide"]
    assert "queryGuideOmitted" not in out
    # 防御性省略时显式标记，绝不静默缺失
    monkeypatch.setattr(mcp_service, "_query_guide", lambda context: None)
    omitted = mcp_service.describe_schema(
        db, admin_user, mcp_ontology["ontologyId"], {})
    assert omitted["queryGuideOmitted"] is True
    assert "queryGuide" not in omitted
    # 动作/函数/哨兵/关联声明不出现
    serialized = json.dumps(out, ensure_ascii=False)
    for banned in ("cancel_order", "act-1", "actions", "functions",
                   "sentinels", "joinAssertions"):
        assert banned not in serialized, banned


def test_schema_type_detail_includes_properties_and_relations(
        db, admin_user, mcp_ontology):
    out = mcp_service.describe_schema(
        db, admin_user, mcp_ontology["ontologyId"],
        {"objectTypeId": "ot-order"})
    names = {p["name"] for p in out["properties"]}
    assert names == {"order_no", "status", "amount", "secret_note"}
    prop = next(p for p in out["properties"] if p["name"] == "order_no")
    assert prop["description"] == "业务唯一单号"
    assert prop["queryable"] is True
    assert out["relationCount"] == 3
    relation = next(r for r in out["relations"]
                    if r["linkTypeId"] == "lt-1")
    assert relation["direction"] == "outgoing"
    assert relation["description"] == "供货关系"
    assert relation["otherObjectTypeId"] == "ot-supplier"
    assert relation["otherObjectType"] == "供应商"
    incoming = next(r for r in out["relations"]
                    if r["linkTypeId"] == "lt-3")
    assert incoming["direction"] == "incoming"
    assert out["hasNext"] is False and out["cursor"] is None


def test_schema_overview_pagination_budget_no_silent_truncation(
        db, admin_user, mcp_ontology, monkeypatch):
    order_entry = {
        "objectTypeId": "ot-order", "name": "Order", "displayName": "订单",
        "description": "销售订单事实", "primaryKey": "order_no",
        "propertyCount": 4, "instanceMode": "materialized"}
    budget = (mcp_service._entry_bytes(order_entry) + 16
              + mcp_service._ENVELOPE_RESERVE_BYTES)
    monkeypatch.setattr(mcp_service, "MCP_SCHEMA_PAGE_BUDGET_BYTES", budget)
    out = mcp_service.describe_schema(db, admin_user,
                                      mcp_ontology["ontologyId"], {})
    assert len(out["objectTypes"]) == 1
    assert out["hasNext"] is True and out["cursor"]
    page2 = mcp_service.describe_schema(
        db, admin_user, mcp_ontology["ontologyId"], {"cursor": out["cursor"]})
    assert {item["objectTypeId"] for item in page2["objectTypes"]} == \
        {"ot-supplier"}
    assert page2["hasNext"] is False

    # 单项摘要即超预算：显式 422，绝不静默截断。
    monkeypatch.setattr(mcp_service, "MCP_SCHEMA_PAGE_BUDGET_BYTES", 40)
    with pytest.raises(McpError) as excinfo:
        mcp_service.describe_schema(db, admin_user,
                                    mcp_ontology["ontologyId"], {})
    assert excinfo.value.status == 422
    assert excinfo.value.code == "schema_item_exceeds_page_budget"
    assert excinfo.value.extra["itemKind"] == "objectType"


def test_schema_relation_pages_advance_by_after_relation(
        db, admin_user, mcp_ontology, monkeypatch):
    """关系多页：afterRelation keyset 确定推进，不重复、不漏。"""
    full = mcp_service.describe_schema(
        db, admin_user, mcp_ontology["ontologyId"],
        {"objectTypeId": "ot-order"})
    header_b = mcp_service._entry_bytes(full["objectType"])
    props_b = sum(mcp_service._entry_bytes(p) for p in full["properties"])
    first_rel_b = mcp_service._entry_bytes(full["relations"][0])
    # 每页恰好装下 header+全部属性+1 条关系
    budget = (mcp_service._ENVELOPE_RESERVE_BYTES + header_b + props_b
              + first_rel_b + 8)
    monkeypatch.setattr(mcp_service, "MCP_SCHEMA_PAGE_BUDGET_BYTES", budget)

    pages = _read_all_type_pages(db, admin_user, mcp_ontology["ontologyId"])
    seen: list[str] = []
    for page in pages:
        seen.extend(r["linkTypeId"] for r in page["relations"])
    assert seen == ["lt-1", "lt-2", "lt-3"]
    assert pages[-1]["hasNext"] is False
    # 每个关系页的属性段为空（属性已在首页发完）
    assert all(p["properties"] == [] for p in pages[1:])


def test_schema_cursor_rejects_release_mismatch(db, admin_user,
                                                mcp_ontology, monkeypatch):
    order_entry = {
        "objectTypeId": "ot-order", "name": "Order", "displayName": "订单",
        "description": "销售订单事实", "primaryKey": "order_no",
        "propertyCount": 4, "instanceMode": "materialized"}
    monkeypatch.setattr(
        mcp_service, "MCP_SCHEMA_PAGE_BUDGET_BYTES",
        mcp_service._entry_bytes(order_entry) + 16
        + mcp_service._ENVELOPE_RESERVE_BYTES)
    out = mcp_service.describe_schema(db, admin_user,
                                      mcp_ontology["ontologyId"], {})
    with pytest.raises(McpError) as excinfo:
        mcp_service.describe_schema(
            db, admin_user, mcp_ontology["ontologyId"],
            {"cursor": out["cursor"], "releaseId": "another-release"})
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_schema_cursor"


def test_schema_cursor_rejects_scope_change(
        client, auth_headers, db, admin_user, mcp_ontology, monkeypatch):
    order_entry = {
        "objectTypeId": "ot-order", "name": "Order", "displayName": "订单",
        "description": "销售订单事实", "primaryKey": "order_no",
        "propertyCount": 4, "instanceMode": "materialized"}
    monkeypatch.setattr(
        mcp_service, "MCP_SCHEMA_PAGE_BUDGET_BYTES",
        mcp_service._entry_bytes(order_entry) + 16
        + mcp_service._ENVELOPE_RESERVE_BYTES)
    out = mcp_service.describe_schema(db, admin_user,
                                      mcp_ontology["ontologyId"], {})
    # 翻页之间收紧授权：游标携带的授权指纹失效 → 409
    r = client.put(
        f"/api/v2/formal/ontologies/{mcp_ontology['ontologyId']}"
        "/agent/profile",
        headers=auth_headers, json={"allowedObjectTypeIds": ["ot-supplier"]})
    assert r.status_code == 200, r.text
    with pytest.raises(McpError) as excinfo:
        mcp_service.describe_schema(
            db, admin_user, mcp_ontology["ontologyId"],
            {"cursor": out["cursor"]})
    assert excinfo.value.status == 409
    assert excinfo.value.code == "schema_context_changed"


def test_schema_respects_profile_allowlists(
        client, auth_headers, db, admin_user, mcp_ontology):
    oid = mcp_ontology["ontologyId"]
    r = client.put(f"/api/v2/formal/ontologies/{oid}/agent/profile",
                   headers=auth_headers,
                   json={"allowedObjectTypeIds": ["ot-order"],
                         "allowedProperties": {"ot-order": ["order_no",
                                                           "status",
                                                           "amount"]}})
    assert r.status_code == 200, r.text

    out = mcp_service.describe_schema(db, admin_user, oid, {})
    assert {item["objectTypeId"] for item in out["objectTypes"]} == \
        {"ot-order"}
    detail = mcp_service.describe_schema(db, admin_user, oid,
                                         {"objectTypeId": "ot-order"})
    # 隐藏属性不出现，另一端隐藏的链接也不出现
    assert {p["name"] for p in detail["properties"]} == \
        {"order_no", "status", "amount"}
    assert detail["relations"] == []

    # 隐藏属性查询被拒（结构树正门强制属性授权）
    with pytest.raises(McpError) as excinfo:
        mcp_service.run_query(db, admin_user, oid, _query_payload(
            mcp_ontology,
            select=[{"source": "s0", "property": "secret_note", "as": "note"}],
            orderBy=[]))
    assert excinfo.value.status == 403
    assert excinfo.value.code == "property_forbidden"


def test_schema_stale_release_rejected_with_current_version(
        db, admin_user, mcp_ontology):
    with pytest.raises(McpError) as excinfo:
        mcp_service.describe_schema(db, admin_user,
                                    mcp_ontology["ontologyId"],
                                    {"releaseId": "not-the-release"})
    assert excinfo.value.status == 409
    assert excinfo.value.code == "release_context_changed"
    assert excinfo.value.extra["currentReleaseId"] == mcp_ontology["releaseId"]
    assert excinfo.value.extra["currentVersionNumber"] == \
        mcp_ontology["versionNumber"]
    assert "ontology_schema" in excinfo.value.extra["guidance"]


def test_schema_cursor_scope_binding(db, admin_user, mcp_ontology):
    def token(owner, exp_offset):
        return mcp_service._sign({
            "v": 1, "kind": "mcp_schema", "owner": owner,
            "ontology": mcp_ontology["ontologyId"],
            "release": mcp_ontology["releaseId"], "mode": "type",
            "typeId": "ot-order", "afterProperty": "", "afterRelation": "",
            "relationsStarted": False,
            "exp": int(time.time()) + exp_offset})

    # 他人游标：签名有效但 owner 不匹配 → 400
    with pytest.raises(McpError) as excinfo:
        mcp_service.describe_schema(
            db, admin_user, mcp_ontology["ontologyId"],
            {"cursor": token("someone-else", 60)})
    assert excinfo.value.status == 400
    # 过期游标 → 410
    with pytest.raises(McpError) as excinfo:
        mcp_service.describe_schema(
            db, admin_user, mcp_ontology["ontologyId"],
            {"cursor": token(str(admin_user.id), -1)})
    assert excinfo.value.status == 410
    # 篡改签名 → 400
    valid = token(str(admin_user.id), 60)
    with pytest.raises(McpError):
        mcp_service.describe_schema(
            db, admin_user, mcp_ontology["ontologyId"],
            {"cursor": valid[:-2] + "zz"})


# ------------------------------------------------------------- query 工具

def test_query_requires_release_id(db, admin_user, mcp_ontology):
    with pytest.raises(McpError) as excinfo:
        mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"],
                              {"query": {"sources": [
                                  {"alias": "s0", "objectTypeId": "ot-order"}],
                                  "select": [{"source": "s0",
                                              "property": "order_no",
                                              "as": "orderNo"}]}})
    assert excinfo.value.status == 422
    assert excinfo.value.code == "invalid_input"


def test_query_rejects_spl_and_foreign_source_fields(
        db, admin_user, mcp_ontology):
    oid = mcp_ontology["ontologyId"]
    # SPL/params/semanticBindings 在输入模型不可表达（extra=forbid）
    with pytest.raises(McpError) as excinfo:
        mcp_service.run_query(db, admin_user, oid, {
            "releaseId": mcp_ontology["releaseId"],
            "spl": "from orders | fields order_no"})
    assert excinfo.value.code == "invalid_input"
    with pytest.raises(McpError) as excinfo:
        mcp_service.run_query(db, admin_user, oid, {
            "releaseId": mcp_ontology["releaseId"],
            "query": {"sources": [
                {"alias": "s0", "objectTypeId": "ot-order",
                 "ontologyId": oid, "releaseId": mcp_ontology["releaseId"]}],
                "select": []}})
    assert excinfo.value.code == "invalid_input"


def test_query_source_object_type_xor_subquery(db, admin_user, mcp_ontology):
    """派生源不再伪造 objectTypeId；对象源缺 objectTypeId 被拒。"""
    from pydantic import ValidationError

    from app.ontologies.mcp.schemas import McpQuerySource

    with pytest.raises(ValidationError):
        McpQuerySource(alias="s0")
    with pytest.raises(ValidationError):
        McpQuerySource(alias="s0", object_type_id="ot-order",
                       subquery={"sources": [
                           {"alias": "t0", "objectTypeId": "ot-supplier"}],
                           "select": []})
    derived = McpQuerySource(
        alias="s0",
        subquery={"sources": [{"alias": "t0", "objectTypeId": "ot-supplier"}],
                  "select": [{"source": "t0", "property": "sname",
                              "as": "name"}]})
    assert derived.object_type_id is None


def test_query_foreign_object_type_rejected(db, admin_user, client,
                                            auth_headers, mcp_ontology):
    """跨本体对象类型：即使注入绑定本体身份，授权解析也按 403 拒绝。"""
    r = client.post("/api/v1/ontologies", headers=auth_headers,
                    json={"name": "mcp-other", "domain": "其他"})
    other_id = r.json()["data"]["id"]
    with pytest.raises(McpError) as excinfo:
        mcp_service.run_query(
            db, admin_user, mcp_ontology["ontologyId"],
            {"releaseId": mcp_ontology["releaseId"],
             "query": {"sources": [
                 {"alias": "s0", "objectTypeId": f"{other_id}:foreign"}],
                 "select": [{"source": "s0", "property": "id",
                             "as": "x"}]}})
    assert excinfo.value.status == 403
    assert excinfo.value.code == "object_type_forbidden"


def test_query_detail_filter_order_and_envelope(db, admin_user, mcp_ontology):
    out = mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"],
                                _query_payload(
                                    mcp_ontology,
                                    where=[{"source": "s0",
                                            "property": "status",
                                            "op": "eq", "value": "paid"}]))
    assert out["releaseId"] == mcp_ontology["releaseId"]
    assert out["versionNumber"] == mcp_ontology["versionNumber"]
    assert out["observedAt"] and out["queryId"]
    assert out["rows"] == [{"orderNo": "SO-002", "amount": 250},
                           {"orderNo": "SO-003", "amount": 50}]
    assert out["releases"]["s0"]["versionNumber"] == \
        mcp_ontology["versionNumber"]
    assert out["releases"]["s0"]["ontologyId"] == mcp_ontology["ontologyId"]
    assert out["hasNext"] is False
    # 终页已回收：本页即终页
    assert out["released"] is True


def test_query_aggregation(db, admin_user, mcp_ontology):
    out = mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"],
                                _query_payload(
                                    mcp_ontology,
                                    select=[{"source": "s0",
                                             "property": "status",
                                             "as": "status"}],
                                    groupBy=[{"source": "s0",
                                              "property": "status"}],
                                    aggregates=[
                                        {"fn": "count", "as": "cnt"},
                                        {"fn": "sum", "source": "s0",
                                         "property": "amount", "as": "total"}],
                                    orderBy=[{"output": "status"}]))
    assert out["rows"] == [
        {"status": "paid", "cnt": 2, "total": 300},
        {"status": "pending", "cnt": 1, "total": 100}]


def test_query_declared_relation_join_within_ontology(
        db, admin_user, mcp_ontology):
    out = mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"], {
        "releaseId": mcp_ontology["releaseId"],
        "query": {
            "sources": [
                {"alias": "s0", "objectTypeId": "ot-order"},
                {"alias": "s1", "objectTypeId": "ot-supplier"}],
            "joins": [{"left": "s0", "right": "s1", "type": "inner",
                       "on": {"kind": "declared_relation",
                              "linkTypeId": "lt-1",
                              "direction": "source_to_target"}}],
            "select": [
                {"source": "s0", "property": "order_no", "as": "orderNo"},
                {"source": "s1", "property": "sname", "as": "supplier"}],
        }})
    assert out["rows"] == [{"orderNo": "SO-001", "supplier": "华南电子"}]
    assert set(out["releases"]) == {"s0", "s1"}
    # 全部 nested sources 钉在绑定本体
    assert all(entry["ontologyId"] == mcp_ontology["ontologyId"]
               for entry in out["releases"].values())


def test_query_stale_release_409_after_promotion(db, admin_user,
                                                 mcp_ontology):
    promoted = _promote_release(db, mcp_ontology["ontologyId"])
    with pytest.raises(McpError) as excinfo:
        mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"],
                              _query_payload(mcp_ontology))
    assert excinfo.value.status == 409
    assert excinfo.value.code == "release_context_changed"
    assert excinfo.value.extra["currentReleaseId"] == promoted["releaseId"]
    assert excinfo.value.extra["currentVersionNumber"] == \
        promoted["versionNumber"]
    assert "ontology_schema" in excinfo.value.extra["guidance"]


# ------------------------------------------------- 分页/快照回收/cursor 隔离

def _snapshot_count(db, query_id: str) -> int:
    from app.ontologies.agent_query.models import AgentQuerySnapshot

    return db.query(AgentQuerySnapshot).filter(
        AgentQuerySnapshot.id == query_id).count()


def test_pagination_terminal_page_releases_snapshot(db, admin_user,
                                                    mcp_ontology):
    oid = mcp_ontology["ontologyId"]
    page1 = mcp_service.run_query(db, admin_user, oid,
                                  _query_payload(mcp_ontology))
    assert page1["rows"] == [{"orderNo": "SO-001", "amount": 100},
                             {"orderNo": "SO-002", "amount": 250}]
    assert page1["hasNext"] is True and page1["cursor"]
    assert page1.get("released") is None
    # 中间页：快照仍在（会话 kind/id 绑定正确）
    assert _snapshot_count(db, page1["queryId"]) == 1
    from app.ontologies.agent_query.models import AgentQuerySnapshot
    snapshot = db.query(AgentQuerySnapshot).filter(
        AgentQuerySnapshot.id == page1["queryId"]).one()
    assert snapshot.session_kind == "mcp"
    assert snapshot.session_id == mcp_service.mcp_session_id(
        admin_user, oid)

    page2 = mcp_service.fetch_query_page(db, admin_user, oid,
                                         {"cursor": page1["cursor"]})
    assert page2["rows"] == [{"orderNo": "SO-003", "amount": 50}]
    assert page2["hasNext"] is False
    # 终页回收：快照删除，配额归还（配额按 owner 全入口共享）
    assert page2["released"] is True
    assert _snapshot_count(db, page2["queryId"]) == 0


def test_page_cursor_after_release_promotion_409(db, admin_user,
                                                 mcp_ontology):
    """续页时 release 已漂移：QueryError 409 也带当前版本与重读指引。"""
    page1 = mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"],
                                  _query_payload(mcp_ontology))
    promoted = _promote_release(db, mcp_ontology["ontologyId"])
    with pytest.raises(McpError) as excinfo:
        mcp_service.fetch_query_page(db, admin_user,
                                     mcp_ontology["ontologyId"],
                                     {"cursor": page1["cursor"]})
    assert excinfo.value.status == 409
    assert excinfo.value.code == "release_context_changed"
    assert excinfo.value.extra["currentReleaseId"] == promoted["releaseId"]
    assert excinfo.value.extra["currentVersionNumber"] == \
        promoted["versionNumber"]
    assert "ontology_schema" in excinfo.value.extra["guidance"]


def test_cross_ontology_cursor_rejected(db, admin_user, client, auth_headers,
                                        mcp_ontology):
    other = client.post("/api/v1/ontologies", headers=auth_headers,
                        json={"name": "mcp-cursor-other", "domain": "其他"}
                        ).json()["data"]["id"]
    page1 = mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"],
                                  _query_payload(mcp_ontology))
    with pytest.raises(McpError) as excinfo:
        mcp_service.fetch_query_page(db, admin_user, other,
                                     {"cursor": page1["cursor"]})
    assert excinfo.value.status == 404
    assert excinfo.value.code == "snapshot_not_found"


def test_cross_entry_cursor_rejected(db, admin_user, mcp_ontology):
    """助手入口（ontology_agent 会话）的 cursor 不能进 MCP 续页。"""
    from app.ontologies.agent_query import schemas as QS

    request = QS.QueryRequest(
        sources=[QS.QuerySource(alias="s0",
                                ontology_id=mcp_ontology["ontologyId"],
                                release_id=mcp_ontology["releaseId"],
                                object_type_id="ot-order")],
        select=[QS.SelectItem(source="s0", property="order_no", as_="orderNo"),
                QS.SelectItem(source="s0", property="amount", as_="amount")],
        order_by=[QS.OrderByItem(output="orderNo")], page_size=1)
    data = execute_unified_query(
        db, admin_user, request, session_kind="ontology_agent",
        session_id="conv-1")
    assert data["hasNext"] and data["cursor"]
    with pytest.raises(McpError) as excinfo:
        mcp_service.fetch_query_page(db, admin_user,
                                     mcp_ontology["ontologyId"],
                                     {"cursor": data["cursor"]})
    assert excinfo.value.status == 404
    assert excinfo.value.code == "snapshot_not_found"


def test_expired_cursor_rejected(db, admin_user, mcp_ontology):
    page1 = mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"],
                                  _query_payload(mcp_ontology))
    from app.ontologies.agent_query.models import AgentQuerySnapshot

    snapshot = db.query(AgentQuerySnapshot).filter(
        AgentQuerySnapshot.id == page1["queryId"]).one()
    # SQLite 存 naive UTC：按同一约定回拨过期
    snapshot.expires_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                           - timedelta(seconds=1))
    db.commit()
    with pytest.raises(McpError) as excinfo:
        mcp_service.fetch_query_page(db, admin_user,
                                     mcp_ontology["ontologyId"],
                                     {"cursor": page1["cursor"]})
    assert excinfo.value.status == 410
    assert excinfo.value.code == "query_snapshot_expired"


# ------------------------------------------------------------ 远程路径

class _FakeRemotePort:
    """远程联邦端口替身：返回的 releases 无 versionNumber（与真实端口一致）。"""

    def __init__(self, release_id, ontology_id):
        self.release_id = release_id
        self.ontology_id = ontology_id
        self.cancelled: list[str] = []
        self.token = "mem." + "p2" + "x" * 32

    def execute(self, db, user, plan, *, request, session_kind, session_id):
        assert session_kind == "mcp"
        assert session_id == mcp_service.mcp_session_id(
            user, self.ontology_id)
        return {
            "queryId": "mem-remote-1",
            "rows": [{"orderNo": "SO-001"}, {"orderNo": "SO-002"}],
            "columns": [{"name": "orderNo", "type": "string",
                         "nullable": True,
                         "sourceRefs": {"alias": "s0"}}],
            "totalRowCount": 3, "rowCounting": "complete",
            "hasNext": True, "cursor": self.token,
            "partial": False, "truncated": False, "exact": True,
            "completeness": "complete", "countsScope": "complete_snapshot",
            "observedAt": "2026-11-09T00:00:00+00:00",
            "citations": [],
            "releases": {"s0": {
                "ontologyId": self.ontology_id,
                "releaseId": self.release_id,
                "objectTypeId": "ot-order", "dataMode": "remote"}},
            "persistence": {"mode": "ephemeral"},
        }

    def fetch(self, db, user, token, *, session_kind, session_id, prepare):
        assert token == self.token
        return {
            "queryId": "mem-remote-1",
            "rows": [{"orderNo": "SO-003"}],
            "columns": [{"name": "orderNo", "type": "string",
                         "nullable": True,
                         "sourceRefs": {"alias": "s0"}}],
            "totalRowCount": 3, "rowCounting": "complete",
            "hasNext": False, "cursor": None,
            "partial": False, "truncated": False, "exact": True,
            "completeness": "complete", "countsScope": "complete_snapshot",
            "observedAt": "2026-11-09T00:00:01+00:00",
            "citations": [],
            "releases": {"s0": {
                "ontologyId": self.ontology_id,
                "releaseId": self.release_id,
                "objectTypeId": "ot-order", "dataMode": "remote"}},
            "persistence": {"mode": "ephemeral"},
        }

    def cancel(self, db, user, query_id):
        self.cancelled.append(query_id)
        return {"cancelled": True}


def test_remote_query_version_metadata_and_handle_cancel(
        db, admin_user, mcp_ontology, monkeypatch):
    from app.ontologies.agent_query import remote_query

    port = _FakeRemotePort(mcp_ontology["releaseId"],
                           mcp_ontology["ontologyId"])
    monkeypatch.setattr(remote_query, "_port", port)
    monkeypatch.setattr(remote_query, "uses_remote_sources",
                        lambda db, request: True)

    out = mcp_service.run_query(db, admin_user, mcp_ontology["ontologyId"],
                                _query_payload(mcp_ontology))
    # 远程结果 releases 缺失 versionNumber → 按 release_id 精确回填
    assert out["releases"]["s0"]["versionNumber"] == \
        mcp_ontology["versionNumber"]
    assert out["versionNumber"] == mcp_ontology["versionNumber"]
    assert out["hasNext"] is True
    assert port.cancelled == []

    final = mcp_service.fetch_query_page(db, admin_user,
                                         mcp_ontology["ontologyId"],
                                         {"cursor": out["cursor"]})
    assert final["hasNext"] is False
    # 终页取消远程句柄（10 个快照/句柄额度不会被 MCP 占死）
    assert port.cancelled == ["mem-remote-1"]
    assert final["released"] is True


# ------------------------------------------------------------- 协议层入口

def _collect_property_names(node, into):
    if isinstance(node, dict):
        if "properties" in node and isinstance(node["properties"], dict):
            into.update(node["properties"].keys())
        for value in node.values():
            _collect_property_names(value, into)
    elif isinstance(node, list):
        for item in node:
            _collect_property_names(item, into)


def test_tool_manifests_and_run_tool_dispatch(db, admin_user, mcp_ontology):
    manifests = mcp_service.tool_manifests()
    assert [m["name"] for m in manifests] == [
        "ontology_schema", "ontology_query", "ontology_query_page"]
    query_manifest = next(m for m in manifests
                          if m["name"] == "ontology_query")
    names: set[str] = set()
    _collect_property_names(query_manifest["inputSchema"], names)
    # SPL 面字段在输入 schema 任何层级都不可表达；源上没有本体身份字段
    # （顶层 releaseId 是必填的发布钉定，语义上不属于源身份）
    for banned in ("spl", "semanticBindings", "params", "ontologyId"):
        assert banned not in names, banned
    assert names <= {
        "releaseId", "query", "sources", "joins", "sourceFilters", "where",
        "select", "groupBy", "aggregates", "distinct", "orderBy", "pageSize",
        "boundedStats", "alias", "objectTypeId", "subquery", "source",
        "property", "as", "op", "value", "and", "or", "not", "left", "right",
        "type", "on", "kind", "leftProperty", "rightProperty", "keyType",
        "cardinality", "businessMeaning", "evidenceRefs", "linkTypeId",
        "direction", "bucket", "fn", "output", "nulls", "filter"}

    out = mcp_service.run_tool(db, admin_user, mcp_ontology["ontologyId"],
                               "ontology_schema", {})
    assert out["objectTypes"]

    with pytest.raises(McpError) as excinfo:
        mcp_service.run_tool(db, admin_user, mcp_ontology["ontologyId"],
                             "ontology_action", {})
    assert excinfo.value.status == 404


def test_authorize_request_read_only_guard(
        client, auth_headers, db, admin_user, mcp_ontology):
    out = mcp_service.authorize_request(db, admin_user,
                                        mcp_ontology["ontologyId"])
    assert out["releaseId"] == mcp_ontology["releaseId"]
    assert out["versionNumber"] == mcp_ontology["versionNumber"]
    assert out["sessionKind"] == "mcp"
    assert out["sessionId"] == mcp_service.mcp_session_id(
        admin_user, mcp_ontology["ontologyId"])
    # 协议层 tool/list 初始化即拒绝停用的本体助手
    r = client.put(
        f"/api/v2/formal/ontologies/{mcp_ontology['ontologyId']}"
        "/agent/profile", headers=auth_headers, json={"enabled": False})
    assert r.status_code == 200, r.text
    with pytest.raises(McpError) as excinfo:
        mcp_service.authorize_request(db, admin_user,
                                      mcp_ontology["ontologyId"])
    assert excinfo.value.status == 403
    assert excinfo.value.code == "profile_disabled"
    with pytest.raises(McpError) as excinfo:
        mcp_service.authorize_request(db, admin_user, "missing-ontology")
    assert excinfo.value.status == 404


def test_inactive_user_denied(db, admin_user, mcp_ontology):
    admin_user.is_active = False
    db.commit()
    with pytest.raises(McpError) as excinfo:
        mcp_service.describe_schema(db, admin_user,
                                    mcp_ontology["ontologyId"], {})
    assert excinfo.value.status == 403
    admin_user.is_active = True
    db.commit()
