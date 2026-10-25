"""Governed draft business-semantic write (PUT .../versions/{v}/semantic).

The endpoint is additive: it stores an exploration canvas + document on an
editing draft under a revision CAS, derives fingerprints/semanticRevision on the
server, reuses the existing semantic consistency gate and version/trial/audit
semantics, and never writes live formal projection rows or bypasses the gate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.exploration import canvas as C
from app.exploration import converter as CV
from app.exploration.document import canvas_fingerprint
from app.exploration.semantic_gate import (
    canvas_schema_issues,
    semantic_consistency_issues,
)
from app.ontologies.inference.models import AuditLog
from app.ontologies.versions import semantic_service
from app.ontologies.versions.models import OntologyTrialRun, OntologyVersion
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot,
    snapshot_hash,
)

URL = "/api/v2/ontologies/{oid}/versions/{vid}/semantic"


def _canvas(attribute_count: int = 47) -> dict:
    canvas = C.empty_canvas()
    attributes = [{
        "name": f"f{index:02d}",
        "displayName": f"字段{index:02d}",
        "typeHint": "文本",
        "required": index == 0,
    } for index in range(attribute_count)]
    canvas, _, errors = C.upsert_elements(canvas, "object", [{
        "name": "PgRecord",
        "displayName": "PG记录",
        "description": "47 字段集成数据映射的业务对象",
        "keyAttribute": "f00",
        "attributes": attributes,
        "relations": [],
    }])
    assert not errors
    return canvas


def _formal_from_draft(draft: dict) -> dict:
    """Mechanical draft → formal snapshot fixture (same shape as apply_draft)."""
    id_by_key: dict[str, str] = {}
    object_types = []
    for index, ot in enumerate(draft["objectTypes"]):
        oid = f"ot-{index:03d}"
        id_by_key[ot["key"]] = oid
        object_types.append({
            "id": oid, "name": ot["name"], "displayName": ot["displayName"],
            "description": ot.get("description") or "",
            "primaryKey": ot.get("primaryKey"),
            "properties": ot.get("properties") or [],
        })
    link_types = [{
        "id": f"lt-{i:03d}", "name": lt["name"],
        "displayName": lt["displayName"],
        "sourceObjectTypeId": id_by_key.get(lt.get("sourceKey"), ""),
        "targetObjectTypeId": id_by_key.get(lt.get("targetKey"), ""),
        "cardinality": lt.get("cardinality") or "one-to-many",
    } for i, lt in enumerate(draft["linkTypes"])]
    actions = [{
        "id": f"act-{i:03d}", "name": a["name"],
        "displayName": a["displayName"],
        "objectTypeId": id_by_key.get(a.get("objectTypeKey")),
        "parameters": a.get("parameters") or [],
        "requiresApproval": bool(a.get("requiresApproval")),
    } for i, a in enumerate(draft["actions"])]
    functions = [{
        "id": f"fn-{i:03d}", "name": f["name"],
        "displayName": f["displayName"],
        "description": f.get("description") or "",
        "targetObjectTypeId": id_by_key.get(f.get("targetObjectTypeKey")),
        "source": {"semanticRole": f.get("semanticRole"),
                   "originKind": f.get("originKind")},
    } for i, f in enumerate(draft["functions"])]
    sentinels = [{
        "id": f"sen-{i:03d}", "name": s["name"],
        "displayName": s["displayName"],
        "description": s.get("description") or "",
        "onChange": bool(s.get("onChange")),
        "onSchedule": bool(s.get("onSchedule")),
        "bindings": ([{"alias": "a", "objectTypeId": id_by_key[s["bindingObjectKey"]]}]
                     if s.get("bindingObjectKey") in id_by_key else []),
        "source": {"originKind": s.get("originKind")},
    } for i, s in enumerate(draft["sentinels"])]
    return {"objectTypes": object_types, "linkTypes": link_types,
            "actions": actions, "functions": functions, "sentinels": sentinels}


def _create_draft(db, ontology_id: str, created_by: str, canvas: dict,
                  *, number: str = "v0.9", lifecycle: str = "editing",
                  revision: int = 0, semantic: dict | None = None
                  ) -> OntologyVersion:
    warnings: list[str] = []
    draft_payload = CV._deterministic_draft(canvas, warnings)
    formal = complete_snapshot(_formal_from_draft(draft_payload))
    draft = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=ontology_id,
        version_number=number, version_label="语义写测试草稿",
        node_kind="draft", lifecycle_status=lifecycle,
        revision=revision, snapshot_formal=formal,
        snapshot_hash=snapshot_hash(formal), snapshot_semantic=semantic,
        created_by=created_by,
    )
    db.add(draft)
    db.commit()
    return draft


def _body(draft: OntologyVersion, canvas: dict, **overrides) -> dict:
    body = {
        "baseRevision": f"{draft.revision}:{draft.snapshot_hash}",
        "canvas": canvas,
        "documentMd": "# 47 字段集成映射\n\n确定性业务定义，字段 f00..f46。\n",
        "documentTitle": "47 字段集成映射",
    }
    body.update(overrides)
    return body


def _put(client, auth_headers, ontology_id: str, version_id: str, body: dict):
    return client.put(URL.format(oid=ontology_id, vid=version_id),
                      headers=auth_headers, json=body)


def _fresh(db, model, identifier: str):
    db.expire_all()
    return db.get(model, identifier)


def _editor_headers(client) -> dict:
    response = client.post("/api/v1/auth/login",
                           json={"username": "editor", "password": "editor123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _trial(db, draft: OntologyVersion, *, status: str) -> OntologyTrialRun:
    run = OntologyTrialRun(
        id=str(uuid.uuid4()), ontology_id=draft.ontology_id,
        version_id=draft.id, revision=draft.revision,
        snapshot_hash=draft.snapshot_hash, base_release_id=None,
        claim_token=str(uuid.uuid4()),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        status=status, dataset_versions=[], result_json={}, created_by="admin",
    )
    db.add(run)
    db.commit()
    return run


def test_semantic_write_positive_47_fields_and_get_compatibility(
        client, auth_headers, ontology, db, admin_user):
    canvas = _canvas()
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas)
    original_revision = draft.revision
    original_hash = draft.snapshot_hash
    response = _put(client, auth_headers, ontology["id"], draft.id,
                    _body(draft, canvas))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    expected_canvas_fp = canvas_fingerprint(canvas)
    expected_document_fp = hashlib.sha256(
        _body(draft, canvas)["documentMd"].encode("utf-8")).hexdigest()
    assert data["versionId"] == draft.id
    assert data["revision"].startswith(f"{original_revision + 1}:")
    assert data["semanticRevision"] == 1
    assert data["canvasFingerprint"] == expected_canvas_fp
    assert data["documentFingerprint"] == expected_document_fp
    assert data["snapshotHash"] == original_hash
    assert data["issues"] == []

    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.revision == original_revision + 1
    assert stored.snapshot_hash == original_hash  # formal hash untouched
    assert stored.snapshot_formal == draft.snapshot_formal
    assert stored.snapshot_semantic["canvasFingerprint"] == expected_canvas_fp
    assert stored.snapshot_semantic["documentFingerprint"] == expected_document_fp
    assert stored.snapshot_semantic["semanticRevision"] == 1

    fetched = client.get(f"/api/v2/ontologies/{ontology['id']}/versions/{draft.id}/semantic",
                         headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    payload = fetched.json()["data"]
    assert payload["overview"]["hasSemanticLayer"] is True
    assert payload["issues"] == []
    assert payload["semantic"]["semanticRevision"] == 1
    assert payload["semantic"]["canvasFingerprint"] == expected_canvas_fp

    audit = db.query(AuditLog).filter_by(
        ontology_id=ontology["id"],
        event_subtype="version_semantic_saved",
        object_id=draft.id,
    ).one()
    assert audit.meta["semanticRevision"] == 1
    assert audit.meta["canvasFingerprint"] == expected_canvas_fp
    assert audit.before_state == {"semanticRevision": 0}
    assert audit.after_state["semanticRevision"] == 1
    assert audit.after_state["documentFingerprint"] == expected_document_fp

    # Workspace save after the semantic write uses the new revision token and
    # keeps the semantic layer untouched (compatibility path).
    workspace = client.put(
        f"/api/v2/ontologies/{ontology['id']}/versions/{draft.id}/workspace",
        headers=auth_headers,
        json={
            "baseRevision": data["revision"],
            "objectTypes": [{
                "id": "ot-000", "name": "PgRecord", "displayName": "PG记录",
                "primaryKey": stored.snapshot_formal["objectTypes"][0]["primaryKey"],
                "properties": stored.snapshot_formal["objectTypes"][0]["properties"],
            }],
            "linkTypes": [], "actions": [], "functions": [],
            "instances": [], "linkInstances": [],
        })
    assert workspace.status_code == 200, workspace.text
    after_workspace = _fresh(db, OntologyVersion, draft.id)
    assert after_workspace.snapshot_semantic["canvasFingerprint"] == expected_canvas_fp


def test_semantic_write_requires_cas_and_rejects_caller_derived_fields(
        client, auth_headers, ontology, db, admin_user):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.1")
    missing = _put(client, auth_headers, ontology["id"], draft.id,
                   {k: v for k, v in _body(draft, canvas).items()
                    if k not in {"baseRevision", "expectedRevision"}})
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "semantic_cas_required"

    wrong = _put(client, auth_headers, ontology["id"], draft.id,
                 _body(draft, canvas, baseRevision="999:deadbeef"))
    assert wrong.status_code == 409
    assert wrong.json()["detail"]["code"] == "conflict"
    assert wrong.json()["detail"]["currentRevision"].startswith("0:")

    extra = _put(client, auth_headers, ontology["id"], draft.id,
                 {**_body(draft, canvas), "canvasFingerprint": "caller",
                  "semanticRevision": 99})
    assert extra.status_code == 422
    assert extra.json()["detail"]["code"] == "invalid_semantic_layer"

    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic is None
    assert stored.revision == draft.revision
    assert db.query(AuditLog).filter_by(
        object_id=draft.id, event_subtype="version_semantic_saved").count() == 0


def test_semantic_write_rejects_inconsistent_canvas_and_leaves_no_partial_state(
        client, auth_headers, ontology, db, admin_user):
    canvas = _canvas(47)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.2")
    inconsistent = _canvas(46)
    response = _put(client, auth_headers, ontology["id"], draft.id,
                    _body(draft, inconsistent))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "semantic_inconsistent"
    assert "semantic_signature_mismatch" in {
        item["code"] for item in detail["errors"]}
    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic is None
    assert stored.revision == draft.revision
    assert stored.snapshot_hash == draft.snapshot_hash
    assert db.query(AuditLog).filter_by(
        object_id=draft.id, event_subtype="version_semantic_saved").count() == 0


def test_semantic_write_rejects_non_editing_release_and_running_trial(
        client, auth_headers, ontology, db, admin_user):
    release = db.query(OntologyVersion).filter_by(
        id=ontology["current_release_id"]).one()
    canvas = _canvas(2)
    release_response = _put(
        client, auth_headers, ontology["id"], release.id,
        {"baseRevision": f"{release.revision}:{release.snapshot_hash}",
         "canvas": canvas, "documentMd": "# doc", "documentTitle": "doc"})
    assert release_response.status_code == 409
    assert release_response.json()["detail"]["code"] == "immutable_release"

    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.3")
    trial_ready = _fresh(db, OntologyVersion, draft.id)
    trial_ready.lifecycle_status = "trial_ready"
    db.commit()
    frozen = _put(client, auth_headers, ontology["id"], draft.id,
                  _body(draft, canvas))
    assert frozen.status_code == 409
    assert frozen.json()["detail"]["code"] == "trial_snapshot_frozen"

    draft2 = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.4")
    running = _trial(db, draft2, status="running")
    blocked = _put(client, auth_headers, ontology["id"], draft2.id,
                   _body(draft2, canvas))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "trial_running"
    assert blocked.json()["detail"]["trialRunId"] == running.id
    stored = _fresh(db, OntologyVersion, draft2.id)
    assert stored.snapshot_semantic is None and stored.revision == draft2.revision


def test_semantic_write_permission_and_cross_ontology_are_rejected(
        client, auth_headers, ontology, db, admin_user, editor_user):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.5")
    denied = _put(client, _editor_headers(client), ontology["id"], draft.id,
                  _body(draft, canvas))
    assert denied.status_code == 403

    other = client.post("/api/v1/ontologies",
                        headers=auth_headers,
                        json={"name": "另一个语义写本体", "domain": "供应链"})
    assert other.status_code == 201, other.text
    other_id = other.json()["data"]["id"]
    other_draft = _create_draft(db, other_id, admin_user.id, canvas, number="v0.6")
    cross = _put(client, auth_headers, ontology["id"], other_draft.id,
                 _body(other_draft, canvas))
    assert cross.status_code == 404
    assert cross.json()["detail"]["code"] == "VERSION_NOT_FOUND"
    assert _fresh(db, OntologyVersion, other_draft.id).snapshot_semantic is None


def test_semantic_write_rejects_concurrent_structure_revision(
        client, auth_headers, ontology, db, admin_user, monkeypatch):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.7")
    original_revision = draft.revision
    original = semantic_service._assert_consistent
    calls = {"count": 0}

    def drifting(payload, snapshot_formal, semantic_consistency_fn):
        original(payload, snapshot_formal, semantic_consistency_fn)
        calls["count"] += 1
        if calls["count"] == 1:
            # A concurrent structure writer commits after our read-phase check.
            db.execute(text(
                "UPDATE ontology_versions SET revision = revision + 1 "
                "WHERE id = :id"), {"id": draft.id})
            db.commit()

    monkeypatch.setattr(semantic_service, "_assert_consistent", drifting)
    response = _put(client, auth_headers, ontology["id"], draft.id,
                    _body(draft, canvas))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "conflict"
    assert response.json()["detail"]["currentRevision"].startswith(
        f"{original_revision + 1}:")
    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic is None
    assert stored.revision == original_revision + 1


def test_semantic_write_rolls_back_on_commit_failure_and_stales_old_trials(
        client, auth_headers, ontology, db, admin_user, monkeypatch):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.8")
    passed = _trial(db, draft, status="passed")

    def broken_commit(_db):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(semantic_service, "_commit", broken_commit)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        _put(client, auth_headers, ontology["id"], draft.id, _body(draft, canvas))
    db.rollback()
    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic is None
    assert stored.revision == draft.revision
    assert db.query(AuditLog).filter_by(
        object_id=draft.id, event_subtype="version_semantic_saved").count() == 0

    monkeypatch.setattr(semantic_service, "_commit", lambda _db: _db.commit())
    response = _put(client, auth_headers, ontology["id"], draft.id,
                    _body(draft, canvas))
    assert response.status_code == 200, response.text
    stale = _fresh(db, OntologyTrialRun, passed.id)
    assert stale.status == "stale"


def test_semantic_write_rejects_unknown_canvas_and_nested_keys(
        client, auth_headers, ontology, db, admin_user):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.10")

    unknown_top = _put(
        client, auth_headers, ontology["id"], draft.id,
        _body(draft, {**canvas, "unexpected": []}))
    assert unknown_top.status_code == 422, unknown_top.text
    assert unknown_top.json()["detail"]["code"] == "invalid_semantic_layer"
    assert unknown_top.json()["detail"]["fields"] == ["unexpected"]

    nested = copy.deepcopy(canvas)
    nested["objects"][0]["attributes"][0]["surprise"] = 1
    unknown_nested = _put(client, auth_headers, ontology["id"], draft.id,
                          _body(draft, nested))
    assert unknown_nested.status_code == 422, unknown_nested.text
    detail = unknown_nested.json()["detail"]
    assert detail["code"] == "semantic_inconsistent"
    assert any(item["code"] == "semantic_canvas_invalid"
               for item in detail["errors"])

    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic is None
    assert stored.revision == draft.revision
    assert db.query(AuditLog).filter_by(
        object_id=draft.id, event_subtype="version_semantic_saved").count() == 0


def test_semantic_write_size_and_nested_limits_before_fingerprint(
        client, auth_headers, ontology, db, admin_user):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.11")

    too_many_children = copy.deepcopy(canvas)
    too_many_children["objects"][0]["attributes"] = [
        {"name": f"n{i}", "typeHint": "文本"} for i in range(501)]
    nested = _put(client, auth_headers, ontology["id"], draft.id,
                  _body(draft, too_many_children))
    assert nested.status_code == 422
    assert nested.json()["detail"]["code"] == "invalid_semantic_layer"
    assert "exceeds 500" in nested.json()["detail"]["message"]

    oversized_canvas = copy.deepcopy(canvas)
    oversized_canvas["objects"][0]["description"] = "x" * 1_000_001
    canvas_big = _put(client, auth_headers, ontology["id"], draft.id,
                      _body(draft, oversized_canvas))
    assert canvas_big.status_code == 422
    assert canvas_big.json()["detail"]["code"] == "invalid_semantic_layer"
    assert "serialized bytes" in canvas_big.json()["detail"]["message"]

    document_big = _put(
        client, auth_headers, ontology["id"], draft.id,
        _body(draft, canvas, documentMd="x" * 1_000_001))
    assert document_big.status_code == 422
    assert document_big.json()["detail"]["code"] == "invalid_semantic_layer"

    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic is None
    assert stored.revision == draft.revision
    assert db.query(AuditLog).filter_by(
        object_id=draft.id, event_subtype="version_semantic_saved").count() == 0


def test_semantic_write_writer_vs_writer_stale_cas(
        client, auth_headers, ontology, db, admin_user):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.12")
    body = _body(draft, canvas)
    first = _put(client, auth_headers, ontology["id"], draft.id, body)
    assert first.status_code == 200, first.text
    second = _put(client, auth_headers, ontology["id"], draft.id, body)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "conflict"
    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic["semanticRevision"] == 1
    assert db.query(AuditLog).filter_by(
        object_id=draft.id, event_subtype="version_semantic_saved").count() == 1


def test_semantic_write_stored_payload_is_release_copy_safe(
        client, auth_headers, ontology, db, admin_user):
    canvas = _canvas(47)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.13")
    response = _put(client, auth_headers, ontology["id"], draft.id,
                    _body(draft, canvas))
    assert response.status_code == 200, response.text
    stored = _fresh(db, OntologyVersion, draft.id)
    semantic = stored.snapshot_semantic
    assert semantic is not None
    assert set(semantic["canvas"]) == {
        "objects", "actors", "behaviors", "events", "rules",
        "scenarios", "processes"}
    assert canvas_schema_issues(semantic["canvas"]) == []
    assert semantic_consistency_issues(copy.deepcopy(semantic),
                                       stored.snapshot_formal) == []
    # promotion_service copies snapshot_semantic verbatim; the canonical,
    # bounded payload is therefore the release-copy boundary.
    serialized = json.dumps(semantic, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
    assert len(serialized) <= 2_000_000


def test_semantic_service_requires_direct_authorization(
        ontology, db, admin_user, editor_user):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.14")
    with pytest.raises(Exception) as caught:
        semantic_service.save_version_semantic(
            db, ontology["id"], draft.id, _body(draft, canvas), editor_user,
            canvas_fingerprint_fn=canvas_fingerprint,
            semantic_consistency_fn=semantic_consistency_issues,
        )
    assert getattr(caught.value, "status_code", None) == 403
    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic is None


def test_size_limits_reject_before_fingerprint_and_db_write(
        ontology, db, admin_user):
    canvas = _canvas(2)
    draft = _create_draft(db, ontology["id"], admin_user.id, canvas, number="v0.15")
    oversized = copy.deepcopy(canvas)
    oversized["objects"][0]["description"] = "x" * 1_000_001
    called = []

    def fingerprint_spy(value):
        called.append(value)
        return "spy"

    with pytest.raises(HTTPException) as caught:
        semantic_service.save_version_semantic(
            db, ontology["id"], draft.id, _body(draft, oversized), admin_user,
            canvas_fingerprint_fn=fingerprint_spy,
            semantic_consistency_fn=semantic_consistency_issues,
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "invalid_semantic_layer"
    assert called == [], "fingerprint must not run for oversized canvas"
    stored = _fresh(db, OntologyVersion, draft.id)
    assert stored.snapshot_semantic is None
    assert stored.revision == draft.revision
