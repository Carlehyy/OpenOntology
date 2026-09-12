"""本体预览投影端点 GET /sessions/{id}/ontology-preview（只读，确定性，无 LLM）：

  1. 绑定会话：基线草稿版本含同名对象 —— 定义一致 → exists（将跳过），
     定义不同 → conflict（不进默认选择集），基线外新元素 → add
  2. 未绑定会话：无比对基线，全部 add
  3. 画布含非法元素（行为主体未解析）→ semanticIssues 透出 blocking
  4. 只读性：调用前后会话画布与绑定版本快照不变；重复调用结果一致
"""
from __future__ import annotations

import copy
import uuid

from app.exploration import canvas as C
from app.exploration.document import canvas_fingerprint
from app.exploration.models import ExplorationSession
from app.models.ontology import OntologyProject
from app.ontologies.versions.models import OntologyVersion

BASE = "/api/v2/exploration"


def _seed_canvas(db, session_id: str, canvas: dict) -> None:
    s = db.query(ExplorationSession).filter(ExplorationSession.id == session_id).first()
    s.canvas = canvas
    s.canvas_version = (s.canvas_version or 0) + 1
    db.commit()


def _order_canvas() -> dict:
    cv = C.empty_canvas()
    cv, _, errs = C.upsert_elements(cv, "object", [
        {"name": "Order", "displayName": "订单", "keyAttribute": "order_no",
         "attributes": [
             {"name": "order_no", "displayName": "订单号", "typeHint": "文本", "required": True},
             {"name": "amount", "displayName": "金额", "typeHint": "金额"},
         ]},
        {"name": "Customer", "displayName": "客户", "keyAttribute": "customer_no",
         "attributes": [
             {"name": "customer_no", "displayName": "客户编号", "typeHint": "文本",
              "required": True},
         ]},
        {"name": "Supplier", "displayName": "供应商", "keyAttribute": "sid",
         "attributes": [
             {"name": "sid", "displayName": "编号", "typeHint": "文本", "required": True},
         ]},
    ])
    assert not errs
    return cv


def _baseline_snapshot() -> dict:
    """基线：Order 与画布定义一致（→ exists），Supplier 显示名/属性不同（→ conflict）。"""
    return {
        "objectTypes": [
            {"id": "ot-order", "name": "Order", "displayName": "订单", "description": "",
             "primaryKey": "p-no",
             "properties": [
                 {"id": "p-no", "name": "order_no", "displayName": "订单号",
                  "type": "string", "required": True},
                 {"id": "p-amt", "name": "amount", "displayName": "金额",
                  "type": "number", "required": False},
             ]},
            {"id": "ot-supplier", "name": "Supplier", "displayName": "旧供应商",
             "description": "", "primaryKey": "p-sid",
             "properties": [
                 {"id": "p-sid", "name": "sid", "displayName": "编号",
                  "type": "string", "required": True},
             ]},
        ],
        "linkTypes": [], "actions": [], "functions": [], "sentinels": [],
    }


def _bound_session(client, auth_headers, db) -> tuple[dict, OntologyVersion]:
    """本体 + 编辑中草稿版本（携带基线结构快照）→ 绑定该版本的会话。"""
    r = client.post("/api/v1/ontologies", headers=auth_headers,
                    json={"name": "订单本体", "domain": "供应链"})
    assert r.status_code == 201, r.text
    ontology_id = r.json()["data"]["id"]
    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    draft_version = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=ontology_id, version_number="v0.1",
        version_label="业务探索合并", node_kind="draft", lifecycle_status="editing",
        revision=0, snapshot_formal=_baseline_snapshot(),
        created_by=project.created_by,
    )
    db.add(draft_version)
    db.commit()
    r = client.post(f"{BASE}/sessions", headers=auth_headers,
                    json={"ontologyId": ontology_id,
                          "ontologyVersionId": draft_version.id})
    assert r.status_code == 201, r.text
    return r.json()["data"], draft_version


def _preview(client, auth_headers, session_id: str) -> dict:
    r = client.get(f"{BASE}/sessions/{session_id}/ontology-preview",
                   headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_preview_bound_session_marks_exists_conflict_add(client, auth_headers, db):
    bound, _version = _bound_session(client, auth_headers, db)
    _seed_canvas(db, bound["id"], _order_canvas())

    data = _preview(client, auth_headers, bound["id"])
    assert data["bound"] is True
    assert data["ontologyId"]
    assert data["canvasFingerprint"] == canvas_fingerprint(
        db.query(ExplorationSession).filter_by(id=bound["id"]).one().canvas)
    assert data["readiness"]["gatesTotal"] == 10

    objects = {item["name"]: item for item in data["projected"]["objectTypes"]}
    assert objects["Order"]["disposition"] == "exists"
    assert objects["Order"]["displayName"] == "订单"
    assert objects["Supplier"]["disposition"] == "conflict"
    assert objects["Customer"]["disposition"] == "add"
    for coll in ("linkTypes", "actions", "functions", "sentinels"):
        assert data["projected"][coll] == []


def test_preview_unbound_session_all_add(client, auth_headers, db):
    r = client.post(f"{BASE}/sessions", headers=auth_headers, json={})
    assert r.status_code == 201, r.text
    session = r.json()["data"]
    _seed_canvas(db, session["id"], _order_canvas())

    data = _preview(client, auth_headers, session["id"])
    assert data["bound"] is False
    assert data["ontologyId"] is None
    items = [item for coll in data["projected"].values() for item in coll]
    assert items and all(item["disposition"] == "add" for item in items)


def test_preview_surfaces_semantic_issues(client, auth_headers, db):
    r = client.post(f"{BASE}/sessions", headers=auth_headers, json={})
    session = r.json()["data"]
    cv = C.empty_canvas()
    cv, _, errs = C.upsert_elements(cv, "object", [
        {"name": "Order", "displayName": "订单", "keyAttribute": "order_no",
         "attributes": [{"name": "order_no", "typeHint": "文本", "required": True}]},
    ])
    assert not errs
    cv, _, errs = C.upsert_elements(cv, "behavior", [
        {"name": "mark_paid", "displayName": "标记支付", "actor": "Nobody",
         "object": "Order"},
    ])
    assert not errs
    _seed_canvas(db, session["id"], cv)

    data = _preview(client, auth_headers, session["id"])
    blocking = [i for i in data["semanticIssues"] if i["severity"] == "blocking"]
    assert any(i["code"] == "behavior_actor_unresolved" for i in blocking)
    actions = data["projected"]["actions"]
    assert [a["name"] for a in actions] == ["mark_paid"]


def test_preview_is_read_only_and_deterministic(client, auth_headers, db):
    bound, version = _bound_session(client, auth_headers, db)
    _seed_canvas(db, bound["id"], _order_canvas())

    before_canvas = copy.deepcopy(
        db.query(ExplorationSession).filter_by(id=bound["id"]).one().canvas)
    before_canvas_version = db.query(ExplorationSession).filter_by(id=bound["id"]).one().canvas_version
    before_snapshot = copy.deepcopy(version.snapshot_formal)
    before_revision = version.revision
    before_hash = version.snapshot_hash

    first = _preview(client, auth_headers, bound["id"])
    second = _preview(client, auth_headers, bound["id"])
    assert first == second

    row = db.query(ExplorationSession).filter_by(id=bound["id"]).one()
    db.refresh(version)
    assert row.canvas == before_canvas
    assert row.canvas_version == before_canvas_version
    assert version.snapshot_formal == before_snapshot
    assert version.revision == before_revision
    assert version.snapshot_hash == before_hash
