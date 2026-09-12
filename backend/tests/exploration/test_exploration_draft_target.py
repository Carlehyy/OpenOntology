"""草稿目标本体缺省绑定（create_draft 的 target 兜底规则）。

  1. 绑定会话不传 targetOntologyId → 默认沉淀回会话绑定本体
  2. 显式传 targetOntologyId → 以显式为准（覆盖会话绑定）
  3. 未绑定会话不传 → None（应用时新建本体，语义不变）
"""
from __future__ import annotations

from app.exploration.models import ExplorationDraft

from tests.exploration.test_exploration import _make_draft

BASE = "/api/v2/exploration"


def _bound_session(client, auth_headers, ontology_id: str) -> dict:
    r = client.post(f"{BASE}/sessions", headers=auth_headers,
                    json={"ontologyId": ontology_id})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _named_ontology(client, auth_headers, name: str) -> dict:
    r = client.post("/api/v1/ontologies", headers=auth_headers,
                    json={"name": name, "domain": "供应链"})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_draft_target_defaults_to_bound_session_ontology(
        client, auth_headers, db, ontology):
    bound = _bound_session(client, auth_headers, ontology["id"])
    draft = _make_draft(client, auth_headers, bound["id"], db)
    assert draft["targetOntologyId"] == ontology["id"]
    row = db.query(ExplorationDraft).filter_by(id=draft["id"]).one()
    assert row.target_ontology_id == ontology["id"]


def test_draft_target_explicit_wins_over_bound_session(
        client, auth_headers, db, ontology):
    bound = _bound_session(client, auth_headers, ontology["id"])
    explicit = _named_ontology(client, auth_headers, "显式目标本体")
    draft = _make_draft(client, auth_headers, bound["id"], db,
                        target_ontology_id=explicit["id"])
    assert draft["targetOntologyId"] == explicit["id"]
    row = db.query(ExplorationDraft).filter_by(id=draft["id"]).one()
    assert row.target_ontology_id == explicit["id"]


def test_draft_target_stays_none_for_unbound_session(client, auth_headers, db):
    r = client.post(f"{BASE}/sessions", headers=auth_headers, json={})
    assert r.status_code == 201, r.text
    draft = _make_draft(client, auth_headers, r.json()["data"]["id"], db)
    assert draft["targetOntologyId"] is None
    row = db.query(ExplorationDraft).filter_by(id=draft["id"]).one()
    assert row.target_ontology_id is None
