"""完整版本树 → 隔离试跑 → 原子发布的核心回归测试。"""
from __future__ import annotations

import csv
import copy
import hashlib
import io
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.data_channel.datasets.service import DatasetService
from app.exploration.document import canvas_fingerprint
from app.exploration.reverse_projection import project_snapshot_to_canvas
from app.models.ontology import OntologyProject
from app.models.entity import Entity
from app.models.relation import Relation
from app.models.inference import AuditLog
from app.models.ontology_formal import (
    ActionExecutionLog, LinkInstance, LinkType, ObjectInstance, ObjectType,
    PropertyFact,
)
from app.models.ontology_version import (
    OntologyTrialLink, OntologyTrialObject, OntologyTrialRun, OntologyVersion,
)
from app.models.sentinel import Sentinel, SentinelFiring, SentinelMatchState
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.mapping import OntologyMapping
from app.ontologies.versions.evolution_service import (
    _compute_trial_derived, _simulate_sentinels, impact_report, snapshot_hash,
    complete_snapshot, validate_builtin_sentinel_contract,
    validate_manual_mapping_trial_contract,
    validate_release_mapping_contract, validate_snapshot,
    validate_trial_mapping_contract,
)
from app.ontologies.versions.router import (
    _snapshot_sentinel_models, _validate_sentinels,
)
from app.ontologies.versions import router as version_router
from app.ontologies.sentinels.evaluator import RESERVED_SENTINEL_ALIASES
from app.ontologies.mappings.mapping_service import MappingService


class MemoryStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str = "") -> str:
        uri = f"s3://{bucket}/{key}"
        self.objects[uri] = data
        return uri

    def get_object(self, uri: str) -> bytes:
        return self.objects[uri]

    def delete_object(self, uri: str) -> None:
        self.objects.pop(uri, None)


def _csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def _empty_csv(columns: list[str]) -> bytes:
    buffer = io.StringIO()
    csv.DictWriter(buffer, fieldnames=columns).writeheader()
    return buffer.getvalue().encode()


_SEMANTIC_TEST_DOCUMENT_MD = "# 测试需求文档\n\n语义层一致性回归用确定性文档。\n"


def _attach_semantic_layer(db, version_id: str) -> None:
    """为版本写回与当前结构快照自洽的最小业务语义层（试跑语义门禁用）。

    画布由 reverse_projection 从结构反投影生成，正向重放后与结构保持
    名集合恒等；文档与双指纹现算现填，保证一致性校验返回空 issue 列表。
    """
    row = db.query(OntologyVersion).filter_by(id=version_id).one()
    canvas = project_snapshot_to_canvas(row.snapshot_formal)
    row.snapshot_semantic = {
        "canvas": canvas,
        "canvasFingerprint": canvas_fingerprint(canvas),
        "documentMd": _SEMANTIC_TEST_DOCUMENT_MD,
        "documentTitle": "测试需求文档",
        "documentFingerprint": hashlib.sha256(
            _SEMANTIC_TEST_DOCUMENT_MD.encode("utf-8")).hexdigest(),
        "semanticRevision": 1,
    }
    db.commit()


def _root(client, headers, ontology_id: str) -> dict:
    detail = client.get(f"/api/v1/ontologies/{ontology_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    project = detail.json()["data"]
    assert project["version"] == "v0"
    assert project["current_release_version"] == "v0"
    listing = client.get("/api/v1/ontologies?page_size=1000", headers=headers)
    assert listing.status_code == 200, listing.text
    listed = next(item for item in listing.json()["data"]["items"]
                  if item["id"] == ontology_id)
    assert listed["version"] == "v0"
    assert listed["current_release_version"] == "v0"
    response = client.get(
        f"/api/v2/ontologies/{ontology_id}/version-tree", headers=headers)
    assert response.status_code == 200, response.text
    tree = response.json()["data"]
    root = next(item for item in tree["versions"] if item["version_number"] == "v0")
    assert root["node_kind"] == "release"
    assert tree["current_release_id"] == root["id"]
    assert project["current_release_id"] == root["id"]
    assert listed["current_release_id"] == root["id"]
    return root


def test_legacy_publish_and_unpublish_cannot_bypass_three_state_lifecycle(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    before_versions = db.query(OntologyVersion).filter_by(
        ontology_id=oid).count()

    publish = client.post(
        f"/api/v2/ontologies/{oid}/versions",
        headers=auth_headers,
        json={"version_label": "禁止的一键发布"},
    )
    assert publish.status_code == 410, publish.text
    assert publish.json()["detail"] == {
        "code": "legacy_publish_endpoint_retired",
        "message": "一键发布接口已停用；请创建草稿、完成隔离试跑后再调用 promote",
        "currentReleaseId": root["id"],
        "requiredFlow": ["draft", "trial", "promote"],
    }

    unpublish = client.post(
        f"/api/v2/ontologies/{oid}/unpublish", headers=auth_headers)
    assert unpublish.status_code == 410, unpublish.text
    assert unpublish.json()["detail"]["code"] == "unpublish_endpoint_retired"

    db.expire_all()
    project = db.query(OntologyProject).filter_by(id=oid).one()
    assert project.current_release_id == root["id"]
    assert project.version == "v0"
    assert db.query(OntologyVersion).filter_by(
        ontology_id=oid).count() == before_versions


def test_ontology_list_freezes_legacy_project_into_v0_release(
        client, auth_headers, admin_user, db):
    """Every management-list item exposes a current released version.

    Legacy projects created before version trees existed are repaired from
    their complete current formal projection without requiring a version-tree
    page visit first.
    """
    legacy = OntologyProject(
        id="legacy-project-without-release",
        name="旧本体发布基线回填",
        domain="其他",
        version="v0.1",
        status="draft",
        build_mode="manual",
        created_by=admin_user.id,
    )
    db.add(legacy)
    db.flush()
    db.add(ObjectType(
        id="legacy-object-type",
        ontology_id=legacy.id,
        name="LegacyOrder",
        display_name="旧订单",
        properties=[],
        interfaces=[],
        position_x=0,
        position_y=0,
    ))
    db.commit()

    response = client.get(
        "/api/v1/ontologies?page_size=1000", headers=auth_headers)
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["data"]["items"]
                if row["id"] == legacy.id)
    assert item["current_release_id"]
    assert item["current_release_version"] == "v0"
    assert item["version"] == "v0"
    assert item["entity_count"] == 1

    db.expire_all()
    stored = db.query(OntologyProject).filter_by(id=legacy.id).one()
    release = db.query(OntologyVersion).filter_by(
        id=stored.current_release_id).one()
    assert stored.version == "v0"
    assert release.node_kind == "release"
    assert release.lifecycle_status == "released"
    assert release.base_release_id == release.id
    assert [row["id"] for row in release.snapshot_formal["objectTypes"]] == [
        "legacy-object-type",
    ]


def test_current_release_read_model_ignores_mutable_runtime_drift(
        client, auth_headers, ontology, db):
    """Detail structure/mapping reads are pinned to current_release_id.

    Legacy mutable projection rows may still exist for compatibility, but they
    must never leak into the management page before a real version promotion.
    """
    ontology_id = ontology["id"]
    root = _root(client, auth_headers, ontology_id)
    db.add_all([
        ObjectType(
            id="runtime-only-type", ontology_id=ontology_id,
            name="RuntimeOnly", display_name="未发布运行表类型",
            properties=[], interfaces=[], position_x=0, position_y=0,
        ),
        OntologyMapping(
            id="runtime-only-mapping", ontology_id=ontology_id,
            curated_dataset_id=None, entity_class="RuntimeOnly",
            target_object_type_id="runtime-only-type",
            field_mapping={"source": "target"}, status="draft",
        ),
        ObjectInstance(
            id="runtime-only-instance", ontology_id=ontology_id,
            object_type_id="runtime-only-type",
            properties={"source": "draft data"}, source="pipeline",
            # No immutable release owner: this simulates the historical leak.
            ontology_release_id=None,
        ),
    ])
    db.commit()

    # Prove the compatibility projections really have drifted.
    live_types = client.get(
        f"/api/v2/formal/ontologies/{ontology_id}/object-types",
        headers=auth_headers).json()["data"]
    assert [item["id"] for item in live_types] == ["runtime-only-type"]
    assert db.query(OntologyMapping).filter_by(
        ontology_id=ontology_id).one().id == "runtime-only-mapping"

    official_instances = client.get(
        f"/api/v2/formal/ontologies/{ontology_id}/instances"
        f"?expected_release_id={root['id']}",
        headers=auth_headers)
    assert official_instances.status_code == 200, official_instances.text
    assert official_instances.json()["data"] == []
    stale_instances = client.get(
        f"/api/v2/formal/ontologies/{ontology_id}/instances"
        "?expected_release_id=stale-release",
        headers=auth_headers)
    assert stale_instances.status_code == 409, stale_instances.text
    assert stale_instances.json()["detail"]["code"] == "release_context_changed"

    structure = client.get(
        f"/api/v2/ontologies/{ontology_id}/current-release/workspace",
        headers=auth_headers)
    assert structure.status_code == 200, structure.text
    payload = structure.json()["data"]
    assert payload["version"] == "v0"
    assert payload["versionId"] == root["id"]
    assert payload["isCurrentRelease"] is True
    assert payload["editable"] is False
    assert payload["objectTypes"] == []
    assert payload["linkTypes"] == []
    assert payload["actions"] == []
    assert payload["functions"] == []

    mappings = client.get(
        f"/api/v2/ontologies/{ontology_id}/current-release/mappings",
        headers=auth_headers)
    assert mappings.status_code == 200, mappings.text
    payload = mappings.json()["data"]
    assert payload["versionId"] == root["id"]
    assert payload["versionNumber"] == "v0"
    assert payload["isCurrentRelease"] is True
    assert payload["editable"] is False
    assert payload["mappings"] == []
    assert payload["linkMappings"] == []


def _draft(client, headers, ontology_id: str, source_id: str) -> dict:
    response = client.post(
        f"/api/v2/ontologies/{ontology_id}/versions/{source_id}/drafts",
        headers=headers, json={"versionLabel": "订单演进"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_historical_release_uses_explicit_current_baseline_recovery_draft(
        client, auth_headers, ontology, admin_user, db):
    """History is a snapshot source, never an escape hatch around trial."""
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    root_row = db.query(OntologyVersion).filter_by(id=root["id"]).one()
    snap = complete_snapshot(root_row.snapshot_formal)

    target = OntologyVersion(
        id="safe-recovery-release-v1",
        ontology_id=oid,
        version_number="v1",
        version_label="历史稳定版",
        parent_version_id=root_row.id,
        base_release_id="safe-recovery-release-v1",
        node_kind="release",
        lifecycle_status="released",
        revision=0,
        snapshot_formal=copy.deepcopy(snap),
        snapshot_hash=snapshot_hash(snap),
        published_at=datetime.now(timezone.utc),
        created_by=admin_user.id,
    )
    current = OntologyVersion(
        id="safe-recovery-release-v2",
        ontology_id=oid,
        version_number="v2",
        version_label="当前发布版",
        parent_version_id=target.id,
        base_release_id="safe-recovery-release-v2",
        node_kind="release",
        lifecycle_status="released",
        revision=0,
        snapshot_formal=copy.deepcopy(snap),
        snapshot_hash=snapshot_hash(snap),
        published_at=datetime.now(timezone.utc),
        created_by=admin_user.id,
    )
    db.add_all([target, current])
    project = db.query(OntologyProject).filter_by(id=oid).one()
    project.current_release_id = current.id
    project.version = current.version_number
    project.status = "published"
    db.commit()

    endpoint = (
        f"/api/v2/ontologies/{oid}/versions/{target.id}/drafts"
    )
    missing_cas = client.post(
        endpoint,
        headers=auth_headers,
        json={"recoveryMode": "current_release_trial"},
    )
    assert missing_cas.status_code == 422, missing_cas.text
    assert missing_cas.json()["detail"]["code"] == (
        "recovery_current_release_required"
    )

    stale_page = client.post(
        endpoint,
        headers=auth_headers,
        json={
            "recoveryMode": "current_release_trial",
            "expectedCurrentReleaseId": target.id,
        },
    )
    assert stale_page.status_code == 409, stale_page.text
    assert stale_page.json()["detail"]["code"] == "recovery_base_changed"

    response = client.post(
        endpoint,
        headers=auth_headers,
        json={
            "versionLabel": "恢复 v1 规则",
            "description": "先按当前数据隔离试跑",
            "recoveryMode": "current_release_trial",
            "expectedCurrentReleaseId": current.id,
        },
    )
    assert response.status_code == 201, response.text
    recovery = response.json()["data"]
    assert recovery["parent_version_id"] == target.id
    assert recovery["base_release_id"] == current.id
    assert recovery["node_kind"] == "draft"
    assert recovery["lifecycle_status"] == "editing"
    assert recovery["snapshot_hash"] == target.snapshot_hash

    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{recovery['id']}/impact",
        headers=auth_headers,
    )
    assert impact.status_code == 200, impact.text
    assert impact.json()["data"]["baseOutdated"] is False
    assert impact.json()["data"]["currentReleaseId"] == current.id

    # A generic historical branch retains its original audit baseline and is
    # still rejected by the trial gate. Only the explicit recovery operation
    # is rebased to the current release.
    generic = _draft(client, auth_headers, oid, target.id)
    assert generic["base_release_id"] == target.id
    blocked = client.post(
        f"/api/v2/ontologies/{oid}/versions/{generic['id']}/trial-runs",
        headers=auth_headers,
        json={},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "draft_base_outdated"

    recovery_trial = client.post(
        f"/api/v2/ontologies/{oid}/versions/{recovery['id']}/trial-runs",
        headers=auth_headers,
        json={},
    )
    assert recovery_trial.status_code == 422, recovery_trial.text
    assert recovery_trial.json()["detail"]["code"] != "draft_base_outdated"

    db.expire_all()
    stored_project = db.query(OntologyProject).filter_by(id=oid).one()
    assert stored_project.current_release_id == current.id
    assert stored_project.version == "v2"


def _workspace(draft: dict) -> dict:
    return {
        "version": draft["version_number"],
        "baseRevision": f"{draft['revision']}:{draft['snapshot_hash']}",
        "objectTypes": [{
            "id": "ot-order", "name": "Order", "displayName": "订单",
            "primaryKey": "p-id", "positionX": 10, "positionY": 20,
            "properties": [
                {"id": "p-id", "name": "id", "displayName": "订单号",
                 "type": "string", "required": True},
                {"id": "p-name", "name": "name", "displayName": "名称",
                 "type": "string", "required": True},
            ],
        }],
        "linkTypes": [], "actions": [], "functions": [],
        # 即使恶意客户端提交实例，草稿工作区也不会接收或污染生产运行数据。
        "instances": [{"id": "should-not-persist", "objectTypeId": "ot-order",
                       "properties": {"id": "bad", "name": "bad"}}],
        "linkInstances": [],
    }


def _builtin_sentinel(**overrides) -> dict:
    definition = {
        "id": "sentinel-order-watch",
        "name": "order_watch",
        "displayName": "订单监控",
        "description": "",
        "bindings": [{
            "alias": "order",
            "objectTypeId": "ot-order",
            "filter": None,
        }],
        "links": [],
        "condition": "",
        "conditionRows": [],
        "conditionLogic": "and",
        "primaryAlias": "order",
        "actionIds": [],
        "actionParameters": {},
        "onChange": True,
        "onSchedule": False,
        "scanIntervalSeconds": 300,
        "triggerMode": "on_enter",
        "muted": False,
        "enabled": True,
        "status": "draft",
    }
    definition.update(overrides)
    return definition


def _configure_draft(client, headers, ontology_id: str, draft: dict, db) -> dict:
    saved = client.put(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/workspace",
        headers=headers, json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text
    revision = saved.json()["data"]["revision"]
    mapping = client.put(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/workspace/mappings",
        headers=headers, json={
            "baseRevision": revision,
            "mappings": [{
                "id": "mapping-order", "curatedDatasetId": "dataset-orders",
                "entityClass": "Order", "targetObjectTypeId": "ot-order",
                "fieldMapping": {"id": "id", "name": "name", "__primary_key__": "id"},
                "status": "draft", "confidence": 1,
            }],
            "linkMappings": [], "sentinels": [],
        },
    )
    assert mapping.status_code == 200, mapping.text
    _attach_semantic_layer(db, draft["id"])
    tree = client.get(
        f"/api/v2/ontologies/{ontology_id}/version-tree", headers=headers).json()["data"]
    return next(item for item in tree["versions"] if item["id"] == draft["id"])


def test_builtin_sentinel_workspace_rejects_non_strict_runtime_fields(
        client, auth_headers, ontology, db):
    """Raw mapping saves must not defer malformed runtime fields to promotion."""
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    draft = _draft(client, auth_headers, oid, root["id"])
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers,
        json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text
    revision = saved.json()["data"]["revision"]
    endpoint = (
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings"
    )

    invalid_cases = [
        ([_builtin_sentinel(id=" ")], "invalid_sentinel_id"),
        ([
            _builtin_sentinel(),
            _builtin_sentinel(displayName="重复 ID"),
        ], "duplicate_sentinel_id"),
        ([_builtin_sentinel(onChange="false")], "invalid_sentinel_boolean"),
        ([_builtin_sentinel(onSchedule=1)], "invalid_sentinel_boolean"),
        ([_builtin_sentinel(muted=0)], "invalid_sentinel_boolean"),
        ([_builtin_sentinel(enabled=None)], "invalid_sentinel_boolean"),
        ([
            _builtin_sentinel(triggerMode="sometimes"),
        ], "invalid_sentinel_trigger_mode"),
        ([
            _builtin_sentinel(scanIntervalSeconds="300"),
        ], "invalid_sentinel_scan_interval_type"),
        ([
            _builtin_sentinel(scanIntervalSeconds=59),
        ], "invalid_sentinel_scan_interval_range"),
        ([
            _builtin_sentinel(scanIntervalSeconds=86_401),
        ], "invalid_sentinel_scan_interval_range"),
    ]
    for sentinels, expected_code in invalid_cases:
        response = client.put(
            endpoint,
            headers=auth_headers,
            json={"baseRevision": revision, "sentinels": sentinels},
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "publish_validation_failed"
        assert expected_code in {
            item["code"] for item in detail["errors"]
        }, detail

    # The graph editor explicitly exposes this as "仅手动".  Preserve that
    # established built-in contract while still requiring both fields to be
    # genuine booleans.
    manual_only = _builtin_sentinel(onChange=False, onSchedule=False)
    assert validate_builtin_sentinel_contract([manual_only]) == []
    accepted = client.put(
        endpoint,
        headers=auth_headers,
        json={"baseRevision": revision, "sentinels": [manual_only]},
    )
    assert accepted.status_code == 200, accepted.text

    db.expire_all()
    stored = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    assert stored.snapshot_formal["sentinels"][0]["onChange"] is False
    assert stored.snapshot_formal["sentinels"][0]["onSchedule"] is False


def test_builtin_sentinel_id_cannot_collide_with_assistant_overlay(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    draft = _draft(client, auth_headers, oid, root["id"])
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers,
        json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text

    db.add(Sentinel(
        id="sentinel-shared-id",
        ontology_id=oid,
        name="assistant_watch",
        display_name="助手动态哨兵",
        bindings=[{"alias": "order", "objectTypeId": "ot-order"}],
        links=[],
        primary_alias="order",
        action_ids=[],
        action_parameters={},
        enabled=False,
        status="published",
        origin="assistant_dynamic",
    ))
    db.commit()

    response = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers,
        json={
            "baseRevision": saved.json()["data"]["revision"],
            "sentinels": [_builtin_sentinel(id="sentinel-shared-id")],
        },
    )
    assert response.status_code == 422, response.text
    assert "sentinel_id_conflicts_dynamic" in {
        item["code"] for item in response.json()["detail"]["errors"]
    }


def test_legacy_invalid_builtin_sentinel_is_blocked_before_trial_and_promote(
        client, auth_headers, ontology, admin_user, db):
    """Legacy/corrupt snapshots fail with a 422 gate, never promotion_failed."""
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    draft = _draft(client, auth_headers, oid, root["id"])
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers,
        json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text

    draft_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    corrupt = complete_snapshot(draft_row.snapshot_formal)
    corrupt["sentinels"] = [
        _builtin_sentinel(scanIntervalSeconds="not-an-integer"),
    ]
    draft_row.snapshot_formal = corrupt
    draft_row.revision = (draft_row.revision or 0) + 1
    draft_row.snapshot_hash = snapshot_hash(corrupt)
    draft_row.lifecycle_status = "editing"
    db.commit()

    trial = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers,
        json={},
    )
    assert trial.status_code == 422, trial.text
    assert "invalid_sentinel_scan_interval_type" in {
        item["code"] for item in trial.json()["detail"]["errors"]
    }
    assert db.query(OntologyTrialRun).filter_by(
        version_id=draft["id"],
    ).count() == 0

    # Simulate a legacy deployment that already persisted a "passed" trial.
    # The authoritative promote endpoint must repeat the same strict contract
    # before _restore_formal_snapshot reaches bool()/int() coercion.
    current = db.query(OntologyVersion).filter_by(id=root["id"]).one()
    report = impact_report(current.snapshot_formal, corrupt)
    legacy_run = OntologyTrialRun(
        id="legacy-invalid-sentinel-trial",
        ontology_id=oid,
        version_id=draft_row.id,
        revision=draft_row.revision,
        snapshot_hash=draft_row.snapshot_hash,
        status="passed",
        dataset_versions=[],
        result_json={
            "counts": {"objects": 0, "links": 0, "facts": 0, "datasets": 0},
            "errors": [],
        },
        impact_hash=report["impactHash"],
        created_by=admin_user.id,
        completed_at=datetime.now(timezone.utc),
    )
    draft_row.lifecycle_status = "trial_ready"
    db.add(legacy_run)
    db.commit()

    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": legacy_run.id,
            "impactHash": report["impactHash"],
        },
    )
    assert promoted.status_code == 422, promoted.text
    detail = promoted.json()["detail"]
    assert detail["code"] == "publish_validation_failed"
    assert detail["code"] != "promotion_failed"
    assert "invalid_sentinel_scan_interval_type" in {
        item["code"] for item in detail["errors"]
    }


def _dataset(db, monkeypatch) -> tuple[DatasetService, MemoryStorage]:
    storage = MemoryStorage()
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service", lambda: storage)
    service = DatasetService(db, storage=storage)
    dataset = service.create_dataset(
        "订单数据", "structured",
        schema_json={
            "primary_key": "id",
            "columns": [{"name": "id", "type": "string"},
                        {"name": "name", "type": "string"}],
        },
    )
    dataset.id = "dataset-orders"
    db.commit()
    service.create_version(
        dataset.id, _csv([{"id": "O-1", "name": "一号订单"},
                          {"id": "O-2", "name": "二号订单"}]), rowcount=2)
    return service, storage


def _promote_configured_lake_release(
    client, headers, ontology_id: str, source_release_id: str, db,
) -> dict:
    draft = _configure_draft(
        client, headers, ontology_id,
        _draft(client, headers, ontology_id, source_release_id),
        db,
    )
    run_response = client.post(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/trial-runs",
        headers=headers, json={},
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()["data"]
    impact_response = client.get(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/impact",
        headers=headers,
    )
    assert impact_response.status_code == 200, impact_response.text
    impact = impact_response.json()["data"]
    promoted = client.post(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/promote",
        headers=headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 201, promoted.text
    return promoted.json()["data"]


def _paired_dataset(db, monkeypatch) -> DatasetService:
    storage = MemoryStorage()
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service", lambda: storage)
    service = DatasetService(db, storage=storage)
    dataset = service.create_dataset(
        "对象配对数据", "structured",
        schema_json={
            "primary_key": "left_id",
            "columns": [{"name": "left_id", "type": "string"},
                        {"name": "right_id", "type": "string"}],
        },
    )
    dataset.id = "dataset-pairs"
    db.commit()
    service.create_version(
        dataset.id,
        _csv([{"left_id": "PAIR-1", "right_id": "PAIR-1"},
              {"left_id": "PAIR-2", "right_id": "PAIR-2"}]),
        rowcount=2,
    )
    return service


def _order_supplier_dataset(db, monkeypatch) -> DatasetService:
    storage = MemoryStorage()
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service", lambda: storage)
    service = DatasetService(db, storage=storage)
    dataset = service.create_dataset(
        "订单供应商宽表", "structured",
        schema_json={
            # The asset key identifies order rows, while Supplier must derive
            # its own identity from the object mapping's hidden PK marker.
            "primary_key": "order_id",
            "columns": [
                {"name": "order_id", "type": "string"},
                {"name": "supplier_id", "type": "string"},
                {"name": "supplier_name", "type": "string"},
                {"name": "tags", "type": "json"},
                {"name": "details", "type": "json"},
            ],
        },
    )
    dataset.id = "dataset-order-suppliers"
    db.commit()
    service.create_version(
        dataset.id,
        _csv([
            {"order_id": "ORDER-1", "supplier_id": "SUPPLIER-1",
             "supplier_name": "供应商一",
             "tags": '["priority", "north"]',
             "details": '{"rank": 1, "active": true}'},
            {"order_id": "ORDER-2", "supplier_id": "SUPPLIER-2",
             "supplier_name": "供应商二",
             "tags": '["standard"]',
             "details": '{"rank": 2, "active": false}'},
            {"order_id": "ORDER-3", "supplier_id": "SUPPLIER-3",
             "supplier_name": "供应商三",
             "tags": "null",
             "details": '{"rank": 3}'},
            {"order_id": "ORDER-4", "supplier_id": "SUPPLIER-4",
             "supplier_name": "供应商四",
             "tags": "[]",
             "details": "{}"},
        ]),
        rowcount=4,
    )
    return service


def test_production_manual_mapping_contract_fails_trial_readiness_and_promote(
        client, auth_headers, ontology, db, monkeypatch):
    """The same exact-pin contract must fence all three lifecycle boundaries."""
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, root["id"]),
        db,
    )
    monkeypatch.setattr(version_router.settings, "environment", "production")

    trial_response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers,
        json={},
    )
    assert trial_response.status_code == 201, trial_response.text
    trial = trial_response.json()["data"]
    assert trial["status"] == "failed"
    trial_error = next(
        item for item in trial["result"]["errors"]
        if item["code"] == "mapping_manual_automation_not_subscribed"
    )
    assert trial_error["field"] == "fieldMapping.__auto_apply_on_version__"
    assert trial_error["datasetId"] == "dataset-orders"
    assert trial_error["datasetRole"] == "object"

    # Simulate a legacy deployment that had already marked this same snapshot
    # as passed without recording its exact dataset pin. Both the read-only
    # readiness preview and authoritative promote path must still fail closed.
    run_row = db.query(OntologyTrialRun).filter_by(id=trial["id"]).one()
    draft_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    run_row.status = "passed"
    run_row.dataset_versions = []
    draft_row.lifecycle_status = "trial_ready"
    db.commit()

    impact_response = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    )
    assert impact_response.status_code == 200, impact_response.text
    impact = impact_response.json()["data"]
    readiness = impact["releaseReadiness"]
    assert readiness["ready"] is False
    readiness_errors = readiness["errors"]
    assert {
        "mapping_manual_automation_not_subscribed",
        "mapping_trial_dataset_pin_missing",
    }.issubset({item["code"] for item in readiness_errors})
    readiness_pin = next(
        item for item in readiness_errors
        if item["code"] == "mapping_trial_dataset_pin_missing"
    )
    assert readiness_pin["field"] == "trial.datasetVersions"
    assert readiness_pin["datasetId"] == "dataset-orders"
    assert readiness_pin["datasetRole"] == "object"

    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": trial["id"],
            "impactHash": impact["impactHash"],
        },
    )
    assert promoted.status_code == 422, promoted.text
    detail = promoted.json()["detail"]
    assert detail["code"] == "publish_validation_failed"
    promote_errors = detail["errors"]
    assert {
        "mapping_manual_automation_not_subscribed",
        "mapping_trial_dataset_pin_missing",
    }.issubset({item["code"] for item in promote_errors})
    promote_pin = next(
        item for item in promote_errors
        if item["code"] == "mapping_trial_dataset_pin_missing"
    )
    assert promote_pin["field"] == "trial.datasetVersions"
    assert promote_pin["datasetId"] == "dataset-orders"
    assert promote_pin["datasetRole"] == "object"


def test_manual_link_mapping_contract_reports_every_dataset_role(
        db, monkeypatch):
    """Link source, target and edge pins remain independently actionable."""
    _dataset(db, monkeypatch)
    snapshot = complete_snapshot({
        "linkMappings": [{
            "id": "link-order-flow",
            "relationType": "order_flow",
            "srcDatasetId": "dataset-orders",
            "tgtDatasetId": "dataset-orders",
            "edgeDatasetId": "dataset-orders",
            "fieldMapping": {"__auto_apply_on_version__": True},
        }],
    })

    missing_pin_errors = validate_manual_mapping_trial_contract(
        db, snapshot, [],
    )
    assert {
        (item["code"], item["datasetRole"], item["field"])
        for item in missing_pin_errors
    } == {
        (
            "link_mapping_trial_dataset_pin_missing",
            role,
            "trial.datasetVersions",
        )
        for role in ("source", "target", "edge")
    }

    dataset = db.query(Dataset).filter_by(id="dataset-orders").one()
    version = db.query(DatasetVersion).filter_by(
        id=dataset.latest_version_id,
    ).one()
    snapshot["linkMappings"][0]["fieldMapping"] = {}
    subscription_errors = validate_manual_mapping_trial_contract(
        db,
        snapshot,
        [{
            "datasetId": dataset.id,
            "versionId": version.id,
            "checksum": version.checksum,
        }],
    )
    assert {
        (item["code"], item["datasetRole"], item["field"])
        for item in subscription_errors
    } == {
        (
            "link_mapping_manual_automation_not_subscribed",
            role,
            "fieldMapping.__auto_apply_on_version__",
        )
        for role in ("source", "target", "edge")
    }


def test_release_mapping_contract_requires_every_type_and_stored_property():
    snapshot = {
        "objectTypes": [
            {
                "id": "ot-order", "name": "Order", "displayName": "订单",
                "primaryKey": "order-id", "properties": [
                    {"id": "order-id", "name": "id", "required": True},
                    # Optional still means persisted; it must be mapped. Only
                    # computed properties are exempt from the lake contract.
                    {"id": "order-name", "name": "name", "required": False},
                    {"id": "order-label", "name": "label", "required": False,
                     "source": "computed", "computed": True},
                ],
            },
            {
                "id": "ot-customer", "name": "Customer", "displayName": "客户",
                "primaryKey": "customer-id", "properties": [
                    {"id": "customer-id", "name": "id", "required": True},
                ],
            },
        ],
        "linkTypes": [{
            "id": "lt-owner", "name": "owned_by", "displayName": "所属客户",
            "sourceObjectTypeId": "ot-order", "targetObjectTypeId": "ot-customer",
            "cardinality": "many-to-one", "properties": [],
        }],
        "actions": [], "functions": [], "sentinels": [],
        "mappings": [{
            "id": "map-order", "curatedDatasetId": "orders",
            "entityClass": "Order", "targetObjectTypeId": "ot-order",
            "fieldMapping": {"id": "id"}, "status": "draft",
        }],
        "linkMappings": [],
    }

    codes = {item["code"] for item in validate_release_mapping_contract(snapshot)}
    assert "mapping_property_missing" in codes
    assert "object_type_mapping_required" in codes
    assert "link_type_mapping_required" in codes
    assert {
        item["code"] for item in validate_trial_mapping_contract(snapshot)
    } == {"trial_object_mapping_required"}
    snapshot["mappings"][0]["fieldMapping"]["name"] = "name"
    assert validate_trial_mapping_contract(snapshot) == []

    structure_only = dict(snapshot)
    structure_only["mappings"] = []
    structure_only["linkMappings"] = []
    structure_codes = {
        item["code"] for item in validate_release_mapping_contract(structure_only)
    }
    assert structure_codes == {
        "object_type_mapping_required", "link_type_mapping_required",
    }
    assert {
        item["code"] for item in validate_trial_mapping_contract(structure_only)
    } == {"trial_object_mapping_required"}

    snapshot["mappings"].append({
        "id": "map-customer", "curatedDatasetId": "customers",
        "entityClass": "Customer", "targetObjectTypeId": "ot-customer",
        "fieldMapping": {"id": "id"}, "status": "draft",
    })
    snapshot["linkMappings"].append({
        "id": "link-owner", "linkTypeId": "lt-owner", "relationType": "owned_by",
        "srcDatasetId": "orders", "tgtDatasetId": "customers",
        "srcKey": "customer_id", "tgtKey": "id", "fieldMapping": {},
        "status": "draft",
    })
    assert validate_release_mapping_contract(snapshot) == []


def test_mapping_contract_blocks_ambiguous_entity_class_before_reprojection():
    """One legacy Entity.type namespace cannot route to two Formal types."""
    snapshot = {
        "objectTypes": [
            {
                "id": "ot-left", "name": "Left", "displayName": "左",
                "primaryKey": "left-id", "properties": [{
                    "id": "left-id", "name": "left_id", "type": "string",
                    "required": True,
                }],
            },
            {
                "id": "ot-right", "name": "Right", "displayName": "右",
                "primaryKey": "right-id", "properties": [{
                    "id": "right-id", "name": "right_id", "type": "string",
                    "required": True,
                }],
            },
        ],
        "linkTypes": [], "actions": [], "functions": [], "sentinels": [],
        "mappings": [
            {
                "id": "map-left", "curatedDatasetId": "left-dataset",
                "entityClass": "SharedClass",
                "targetObjectTypeId": "ot-left",
                "fieldMapping": {
                    "left_id": "left_id", "__primary_key__": "left_id",
                },
            },
            {
                "id": "map-right", "curatedDatasetId": "right-dataset",
                "entityClass": "SharedClass",
                "targetObjectTypeId": "ot-right",
                "fieldMapping": {
                    "right_id": "right_id", "__primary_key__": "right_id",
                },
            },
        ],
        "linkMappings": [],
    }

    assert {
        item["code"] for item in validate_trial_mapping_contract(snapshot)
    } == {"mapping_entity_class_target_ambiguous"}
    assert "mapping_entity_class_target_ambiguous" in {
        item["code"] for item in validate_release_mapping_contract(snapshot)
    }


def test_builtin_sentinel_trial_resolves_parameters_and_all_link_constraints():
    """The isolated trial must model runtime joins and parameter validation.

    A triangle with one missing edge is not a match even though the first two
    links can independently join.  Once complete, the bound value is validated
    with the production Action contract without executing the action.
    """
    snapshot = {
        "objectTypes": [], "linkTypes": [], "functions": [],
        "actions": [{
            "id": "action-threshold", "name": "set_threshold",
            "displayName": "设置阈值", "objectTypeId": "type-a",
            "parameters": [{
                "name": "threshold", "displayName": "阈值",
                "type": "number", "required": True,
            }],
            "rules": [{
                "id": "notify-threshold", "name": "预警通知",
                "type": "notification", "enabled": True, "order": 0,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": "threshold={{params.threshold}}",
                },
            }],
        }],
        "mappings": [], "linkMappings": [],
        "sentinels": [{
            "id": "sentinel-triangle", "name": "triangle",
            "displayName": "三角约束", "enabled": True, "muted": False,
            "bindings": [
                {"alias": "a", "objectTypeId": "type-a"},
                {"alias": "b", "objectTypeId": "type-b"},
                {"alias": "c", "objectTypeId": "type-c"},
            ],
            "primaryAlias": "a",
            "links": [
                {"from": "a", "to": "b", "linkTypeId": "link-ab"},
                {"from": "a", "to": "c", "linkTypeId": "link-ac"},
                {"from": "b", "to": "c", "linkTypeId": "link-bc"},
            ],
            "condition": "a.threshold >= 10",
            "actionIds": ["action-threshold"],
            "actionParameters": {
                "action-threshold": {
                    "threshold": {
                        "sourceType": "property",
                        "alias": "a",
                        "property": "threshold",
                    },
                },
            },
        }],
    }
    objects = [
        {"objectId": "a-1", "objectTypeId": "type-a",
         "properties": {"threshold": 12}},
        {"objectId": "b-1", "objectTypeId": "type-b", "properties": {}},
        {"objectId": "c-1", "objectTypeId": "type-c", "properties": {}},
    ]
    links = [
        {"linkId": "ab", "linkTypeId": "link-ab",
         "sourceObjectId": "a-1", "targetObjectId": "b-1"},
        {"linkId": "ac", "linkTypeId": "link-ac",
         "sourceObjectId": "a-1", "targetObjectId": "c-1"},
    ]

    incomplete = _simulate_sentinels(snapshot, objects, links)[0]
    assert incomplete["matched"] == 0
    assert incomplete["plannedActions"] == 0
    assert incomplete["errors"] == []

    links.append({
        "linkId": "bc", "linkTypeId": "link-bc",
        "sourceObjectId": "b-1", "targetObjectId": "c-1",
    })
    complete = _simulate_sentinels(snapshot, objects, links)[0]
    assert complete["matched"] == 1
    assert complete["parameterErrorCount"] == 0
    assert complete["plannedActions"] == 1
    sample = complete["plannedActionSamples"][0]
    assert sample["actionId"] == "action-threshold"
    assert sample["actionName"] == "设置阈值"
    assert sample["edge"] == "enter"
    assert sample["targetInstanceId"] == "a-1"
    assert sample["match"] == {
        "a": "a-1", "b": "b-1", "c": "c-1"}
    assert sample["parameters"] == {"threshold": 12}
    assert sample["status"] == "success"
    assert sample["validationErrors"] == []
    assert sample["effects"][0]["type"] == "notification"
    assert sample["effects"][0]["status"] == "preview"
    assert sample["effects"][0]["committed"] is False
    assert complete["sideEffects"] == "none"

    objects[0]["properties"]["threshold"] = "not-a-number"
    snapshot["sentinels"][0]["condition"] = ""
    invalid = _simulate_sentinels(snapshot, objects, links)[0]
    assert invalid["matched"] == 1
    assert invalid["parameterErrorCount"] == 1
    assert "number" in invalid["errors"][0]


def test_builtin_sentinel_trial_validates_enter_and_leave_event_parameters():
    snapshot = {
        "objectTypes": [], "linkTypes": [], "functions": [],
        "actions": [{
            "id": "action-edge", "name": "record_edge",
            "displayName": "记录边沿", "objectTypeId": "type-order",
            "parameters": [{
                "name": "edge", "type": "string", "required": True,
                "options": ["enter", "leave"],
            }],
            "rules": [{
                "id": "notify-edge", "name": "边沿通知",
                "type": "notification", "enabled": True, "order": 0,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": "edge={{params.edge}}",
                },
            }],
        }],
        "mappings": [], "linkMappings": [],
        "sentinels": [{
            "id": "sentinel-edge", "name": "edge", "displayName": "边沿哨兵",
            "enabled": True, "muted": False,
            "bindings": [{"alias": "order", "objectTypeId": "type-order"}],
            "primaryAlias": "order", "links": [], "condition": "",
            "triggerMode": "on_enter_leave",
            "actionIds": ["action-edge"],
            "actionParameters": {
                "action-edge": {
                    "edge": {"sourceType": "edge"},
                },
            },
        }],
    }
    objects = [{
        "objectId": "order-1", "objectTypeId": "type-order",
        "properties": {"id": "O-1"},
    }]

    result = _simulate_sentinels(snapshot, objects, [])[0]
    assert result["matched"] == 1
    assert result["plannedActions"] == 2
    assert result["plannedActionSamples"][0]["parameters"] == {"edge": "enter"}
    assert result["plannedActionSamples"][1]["parameters"] == {"edge": "leave"}
    assert all(
        item["status"] == "success"
        for item in result["plannedActionSamples"])
    assert result["errors"] == []

    snapshot["actions"][0]["parameters"][0]["options"] = ["enter"]
    invalid_leave = _simulate_sentinels(snapshot, objects, [])[0]
    assert invalid_leave["plannedActions"] == 2
    assert any("leave 参数" in error and "允许选项" in error
               for error in invalid_leave["errors"])


def test_builtin_sentinel_trial_handles_missing_action_and_real_row_properties():
    snapshot = {
        "objectTypes": [], "linkTypes": [], "actions": [], "functions": [],
        "mappings": [], "linkMappings": [],
        "sentinels": [{
            "id": "sentinel-missing", "name": "missing",
            "displayName": "缺失引用哨兵", "enabled": True, "muted": False,
            "bindings": [{
                "alias": "order", "objectTypeId": "type-order",
                "filter": "order.optional_status != 'closed'",
            }],
            "primaryAlias": "order", "links": [], "condition": "",
            "actionIds": ["action-does-not-exist"],
            "actionParameters": {
                "action-does-not-exist": {"value": "{{order.id}}"},
            },
        }],
    }
    objects = [{
        "objectId": "order-1", "objectTypeId": "type-order",
        "properties": {"id": "O-1"},
    }]

    missing_filter_value = _simulate_sentinels(snapshot, objects, [])[0]
    assert missing_filter_value["matched"] == 0
    assert any("optional_status" in error and "不存在" in error
               for error in missing_filter_value["errors"])

    snapshot["sentinels"][0]["bindings"][0]["filter"] = ""
    snapshot["sentinels"][0]["condition"] = (
        "order.optional_status != 'closed'")
    missing_condition_value = _simulate_sentinels(snapshot, objects, [])[0]
    assert missing_condition_value["matched"] == 0
    assert any("optional_status" in error and "不存在" in error
               for error in missing_condition_value["errors"])

    snapshot["sentinels"][0]["condition"] = ""
    missing_action = _simulate_sentinels(snapshot, objects, [])[0]
    assert missing_action["matched"] == 1
    assert missing_action["plannedActions"] == 1
    assert any("动作不存在: action-does-not-exist" in error
               for error in missing_action["errors"])


def test_builtin_sentinel_static_gate_rejects_schema_typos_and_reserved_aliases():
    object_type = SimpleNamespace(
        id="type-order",
        properties=[
            {"name": "amount"},
            {"name": "risk-score", "source": "computed", "computed": True},
        ],
    )
    action = SimpleNamespace(
        id="action-notify", name="notify", display_name="通知",
        object_type_id="type-order",
        parameters=[{"name": "message", "type": "string", "required": True}],
    )
    sentinel = SimpleNamespace(
        id="sentinel-typo", name="typo", display_name="错别字哨兵",
        bindings=[{
            "alias": "order", "objectTypeId": "type-order",
            "filter": "order.amount > 0 and obj['missing-filter'] == 1",
        }],
        primary_alias="order",
        condition="order['risk-score'] > 10 and order.missing_condition == 1",
        links=[],
        action_ids=["action-notify"],
        action_parameters={
            "action-notify": {
                "message": (
                    "订单 {{order.missing_template}} "
                    "于 {{event.unknownEventField}}"),
            },
        },
    )
    errors = _validate_sentinels(
        [sentinel], [object_type], [], [action])
    codes = [item["code"] for item in errors]
    assert codes.count("sentinel_expression_property_not_found") == 2
    assert "sentinel_parameter_property_not_found" in codes
    assert "sentinel_event_property_not_found" in codes

    sentinel.condition = ""
    sentinel.action_ids = []
    sentinel.action_parameters = {}
    assert {
        "event", "obj", "utils", "sum", "avg", "count", "len",
        "min", "max", "round", "abs", "lower", "upper", "contains",
        "now", "True", "False", "None", "true", "false", "null",
    }.issubset(RESERVED_SENTINEL_ALIASES)
    for reserved_alias in ("event", "obj", "sum", "true"):
        sentinel.bindings = [{
            "alias": reserved_alias, "objectTypeId": "type-order",
        }]
        sentinel.primary_alias = reserved_alias
        reserved = _validate_sentinels([sentinel], [object_type], [], [])
        assert "reserved_sentinel_alias" in {
            item["code"] for item in reserved}


def test_builtin_sentinel_static_gate_rejects_parameter_type_mismatch_without_matches():
    object_type = SimpleNamespace(
        id="type-order",
        properties=[
            {"name": "amount", "type": "number"},
            {"name": "active", "type": "boolean"},
        ],
    )
    action = SimpleNamespace(
        id="action-record", name="record", display_name="记录",
        object_type_id="type-order",
        parameters=[
            {"name": "wrongProperty", "type": "boolean", "required": True},
            {"name": "exactTemplate", "type": "number", "required": True},
            {"name": "partialTemplate", "type": "number", "required": True},
            {"name": "eventValue", "type": "number", "required": True},
        ],
    )
    # No ObjectInstance or matching tuple is supplied to this release gate.
    # Compatibility must be proven from immutable schemas alone.
    sentinel = SimpleNamespace(
        id="sentinel-types", name="types", display_name="类型哨兵",
        bindings=[{"alias": "order", "objectTypeId": "type-order"}],
        primary_alias="order", condition="", links=[],
        action_ids=["action-record"],
        action_parameters={
            "action-record": {
                "wrongProperty": {
                    "sourceType": "property",
                    "alias": "order",
                    "property": "amount",
                },
                "exactTemplate": "{{order.amount}}",
                "partialTemplate": "金额={{order.amount}}",
                "eventValue": {
                    "sourceType": "event",
                    "property": "matchKey",
                },
            },
        },
    )

    errors = _validate_sentinels(
        [sentinel], [object_type], [], [action])
    mismatches = [
        item for item in errors
        if item["code"] == "sentinel_parameter_type_mismatch"
    ]

    assert {
        item["field"] for item in mismatches
    } == {
        "actionParameters.action-record.wrongProperty",
        "actionParameters.action-record.partialTemplate",
        "actionParameters.action-record.eventValue",
    }


def test_builtin_sentinel_gate_rejects_required_action_param_from_optional_property():
    object_type = SimpleNamespace(
        id="type-order",
        properties=[
            {"name": "optional_reason", "type": "string", "required": False},
            {"name": "required_reason", "type": "string", "required": True},
        ],
    )
    action = SimpleNamespace(
        id="action-review", name="review", display_name="复核",
        object_type_id="type-order",
        parameters=[
            {"name": "reason", "type": "string", "required": True},
            {"name": "fallback", "type": "string", "required": True,
             "defaultValue": "system"},
        ],
        rules=[],
    )
    sentinel = SimpleNamespace(
        id="sentinel-review", name="review", display_name="复核哨兵",
        bindings=[{"alias": "order", "objectTypeId": "type-order"}],
        primary_alias="order", condition="", links=[],
        action_ids=["action-review"],
        action_parameters={
            "action-review": {
                "reason": {
                    "sourceType": "property",
                    "alias": "order",
                    "property": "optional_reason",
                },
                "fallback": "{{order.optional_reason}}",
            },
        },
        trigger_mode="on_enter",
    )

    errors = _validate_sentinels(
        [sentinel], [object_type], [], [action])
    optional_supply = [
        item for item in errors
        if item["code"] == "sentinel_required_parameter_optional_property"
    ]
    assert [item["field"] for item in optional_supply] == [
        "actionParameters.action-review.reason",
    ]

    sentinel.action_parameters["action-review"]["reason"] = {
        "sourceType": "property",
        "alias": "order",
        "property": "required_reason",
    }
    assert "sentinel_required_parameter_optional_property" not in {
        item["code"] for item in _validate_sentinels(
            [sentinel], [object_type], [], [action])
    }


def test_builtin_sentinel_gate_rejects_leave_action_needing_live_links():
    order_type = SimpleNamespace(
        id="type-order", properties=[{"name": "email", "type": "string"}])
    user_type = SimpleNamespace(
        id="type-user", properties=[{"name": "email", "type": "string"}])
    link_type = SimpleNamespace(
        id="link-owner",
        source_object_type_id="type-order",
        target_object_type_id="type-user",
    )
    action = SimpleNamespace(
        id="action-notify-owner", name="notify_owner", display_name="通知负责人",
        object_type_id="type-order", parameters=[],
        rules=[{
            "id": "notify", "type": "notification", "enabled": True,
            "config": {
                "channel": "internal",
                "recipientSource": "link",
                "linkTypeId": "link-owner",
                "recipientProperty": "email",
                "message": "订单已离开匹配",
            },
        }],
    )
    sentinel = SimpleNamespace(
        id="sentinel-leave", name="leave", display_name="离开哨兵",
        bindings=[{"alias": "order", "objectTypeId": "type-order"}],
        primary_alias="order", condition="", links=[],
        action_ids=["action-notify-owner"], action_parameters={},
        trigger_mode="on_enter_leave",
    )

    errors = _validate_sentinels(
        [sentinel], [order_type, user_type], [link_type], [action])
    assert "sentinel_leave_action_not_snapshot_safe" in {
        item["code"] for item in errors}

    sentinel.trigger_mode = "on_enter"
    enter_only = _validate_sentinels(
        [sentinel], [order_type, user_type], [link_type], [action])
    assert "sentinel_leave_action_not_snapshot_safe" not in {
        item["code"] for item in enter_only}

    snapshot_model = _snapshot_sentinel_models({
        "sentinels": [{
            "id": "sentinel-leave",
            "name": "leave",
            "triggerMode": "on_enter_leave",
        }],
    })[0]
    assert snapshot_model.trigger_mode == "on_enter_leave"


def test_snapshot_gate_rejects_action_that_cannot_execute():
    snapshot = {
        "objectTypes": [{
            "id": "type-order", "name": "Order", "displayName": "订单",
            "primaryKey": "order-id",
            "properties": [{
                "id": "order-id", "name": "id", "type": "string",
                "required": True,
            }],
        }],
        "linkTypes": [],
        "actions": [{
            "id": "action-no-effect", "name": "no_effect",
            "displayName": "无效果动作", "objectTypeId": "type-order",
            "parameters": [], "rules": [],
        }],
        "functions": [], "instances": [], "linkInstances": [],
        "mappings": [], "linkMappings": [], "sentinels": [],
    }

    errors = validate_snapshot(snapshot)

    assert "invalid_action_definition" in {item["code"] for item in errors}
    assert any("没有启用的可执行副作用规则" in item["message"]
               for item in errors)


def _dormant_action_snapshot(**action_overrides) -> dict:
    action = {
        "id": "action-no-effect", "name": "no_effect",
        "displayName": "无效果动作", "objectTypeId": "type-order",
        "parameters": [], "rules": [],
    }
    action.update(action_overrides)
    return {
        "objectTypes": [{
            "id": "type-order", "name": "Order", "displayName": "订单",
            "primaryKey": "order-id",
            "properties": [{
                "id": "order-id", "name": "id", "type": "string",
                "required": True,
            }],
        }],
        "linkTypes": [],
        "actions": [action],
        "functions": [], "instances": [], "linkInstances": [],
        "mappings": [], "linkMappings": [], "sentinels": [],
    }


def test_snapshot_gate_layers_dormant_action_rules_for_draft_save():
    """分层口径：草稿保存期把「无可执行副作用规则」降级为 warning；默认严格不变。"""
    snapshot = _dormant_action_snapshot()

    strict = validate_snapshot(snapshot)
    assert any("没有启用的可执行副作用规则" in item["message"]
               for item in strict)

    warnings: list[dict] = []
    errors = validate_snapshot(
        snapshot,
        require_object_type=False,
        require_executable_action_rules=False,
        warnings_out=warnings,
    )
    assert errors == []
    assert [item["code"] for item in warnings] == ["invalid_action_definition"]
    assert warnings[0]["kind"] == "action"
    assert warnings[0]["id"] == "action-no-effect"
    assert "没有启用的可执行副作用规则" in warnings[0]["message"]

    # 宽松模式但未提供 warnings_out：降级信息不静默丢失，回退到 errors
    fallback = validate_snapshot(snapshot, require_executable_action_rules=False)
    assert any("没有启用的可执行副作用规则" in item["message"]
               for item in fallback)

    # 宽松只降级这一条：其他动作定义错误（如校验表达式引用不存在属性）仍拦截
    broken = _dormant_action_snapshot(rules=[{
        "id": "rule-bad", "type": "validation", "enabled": True, "order": 0,
        "config": {"type": "validation",
                   "condition": "object.missing_amount > 0"},
    }])
    warnings = []
    errors = validate_snapshot(
        broken,
        require_object_type=False,
        require_executable_action_rules=False,
        warnings_out=warnings,
    )
    assert any("校验表达式无效" in item["message"] for item in errors)
    assert any("没有启用的可执行副作用规则" in item["message"]
               for item in warnings)


def test_snapshot_gate_exempts_approval_only_action_in_both_modes():
    """requiresApproval 对齐运行时 approval_proposal_only：挂起审批不记伪成功，
    即使没有启用的可执行副作用规则也语义合法，严格/宽松两种模式都豁免。"""
    snapshot = _dormant_action_snapshot(requiresApproval=True)

    assert validate_snapshot(snapshot) == []
    warnings: list[dict] = []
    errors = validate_snapshot(
        snapshot,
        require_object_type=False,
        require_executable_action_rules=False,
        warnings_out=warnings,
    )
    assert errors == []
    assert warnings == []


def test_snapshot_gate_compiles_expression_functions_without_data_rows():
    snapshot = {
        "objectTypes": [{
            "id": "type-order", "name": "Order", "displayName": "订单",
            "primaryKey": "order-id",
            "properties": [{
                "id": "order-id", "name": "id", "type": "string",
                "required": True,
            }],
        }],
        "linkTypes": [], "actions": [],
        "functions": [
            {
                "id": "fn-unknown", "name": "unknown",
                "displayName": "未知变量函数",
                "functionType": "object", "language": "expression",
                "targetObjectTypeId": "type-order", "parameters": [],
                "returnType": "number",
                "body": "unknown_variable + 1",
                "enabled": True,
            },
            {
                "id": "fn-missing-property", "name": "missing_property",
                "displayName": "缺失属性函数",
                "functionType": "object", "language": "expression",
                "targetObjectTypeId": "type-order", "parameters": [],
                "returnType": "number",
                "body": "object.missing_amount + 1",
                "enabled": True,
            },
        ],
        "instances": [], "linkInstances": [],
        "mappings": [], "linkMappings": [], "sentinels": [],
    }

    errors = validate_snapshot(snapshot)

    function_errors = [
        item for item in errors
        if item["code"] == "invalid_expression_function"
    ]
    assert function_errors
    assert any("unknown_variable" in item["message"]
               for item in function_errors)
    assert any("object.missing_amount" in item["message"]
               for item in function_errors)


def test_builtin_sentinel_trial_fails_closed_at_candidate_cap():
    snapshot = {
        "objectTypes": [], "linkTypes": [], "actions": [], "functions": [],
        "mappings": [], "linkMappings": [],
        "sentinels": [{
            "id": "sentinel-cap", "name": "cap", "displayName": "容量保护",
            "enabled": True, "muted": False,
            "bindings": [{"alias": "row", "objectTypeId": "type-row"}],
            "primaryAlias": "row", "links": [], "condition": "",
            "actionIds": [], "actionParameters": {},
        }],
    }
    objects = [{
        "objectId": f"row-{index}", "objectTypeId": "type-row",
        "properties": {"index": index},
    } for index in range(1001)]

    result = _simulate_sentinels(snapshot, objects, [])[0]

    assert result["candidateCapReached"] is True
    assert result["candidateCount"] == 1000
    assert result["errors"] == [
        "跨对象候选组合超过安全上限 1000，请收窄绑定过滤条件后重试",
    ]


def test_builtin_sentinel_trial_uses_expression_derived_properties():
    snapshot = {
        "objectTypes": [{
            "id": "type-order", "name": "Order", "displayName": "订单",
            "primaryKey": "order-id",
            "properties": [
                {"id": "order-id", "name": "id", "type": "string",
                 "required": True},
                {"id": "order-amount", "name": "amount", "type": "number"},
                {"id": "order-score", "name": "score", "type": "number",
                 "source": "computed", "computed": True,
                 "functionId": "fn-score"},
            ],
        }],
        "linkTypes": [],
        "actions": [{
            "id": "action-score", "name": "record_score",
            "displayName": "记录评分", "objectTypeId": "type-order",
            "parameters": [{
                "name": "score", "type": "number", "required": True,
            }],
            "rules": [{
                "id": "notify-score", "name": "评分通知",
                "type": "notification", "enabled": True, "order": 0,
                "config": {
                    "channel": "internal",
                    "recipientSource": "constant",
                    "recipient": "ops",
                    "messageTemplate": "score={{params.score}}",
                },
            }],
        }],
        "functions": [{
            "id": "fn-score", "name": "score", "displayName": "评分",
            "functionType": "object", "language": "expression",
            "targetObjectTypeId": "type-order", "parameters": [],
            "returnType": "number", "body": "object.amount * 2",
            "enabled": True,
        }],
        "mappings": [], "linkMappings": [],
        "sentinels": [{
            "id": "sentinel-score", "name": "high_score",
            "displayName": "高评分", "enabled": True, "muted": False,
            "bindings": [{"alias": "order", "objectTypeId": "type-order"}],
            "primaryAlias": "order", "links": [],
            "condition": "order.score >= 10",
            "actionIds": ["action-score"],
            "actionParameters": {
                "action-score": {
                    "score": "{{order.score}}",
                },
            },
        }],
    }
    objects = [{
        "objectId": "order-1", "objectTypeId": "type-order",
        "properties": {"id": "O-1", "amount": 6},
    }]

    assert _compute_trial_derived(snapshot, objects) == []
    assert objects[0]["computed"] == {"score": 12}
    result = _simulate_sentinels(snapshot, objects, [])[0]
    assert result["matched"] == 1
    assert result["plannedActionSamples"][0]["parameters"] == {"score": 12}
    assert result["errors"] == []


def test_builtin_sentinel_trial_previews_complete_action_plan_without_effects():
    snapshot = {
        "objectTypes": [
            {
                "id": "type-order", "name": "Order",
                "displayName": "订单", "primaryKey": "order-id",
                "properties": [
                    {"id": "order-id", "name": "id", "type": "string",
                     "required": True},
                    {"id": "amount", "name": "amount", "type": "number"},
                    {"id": "status", "name": "status", "type": "string"},
                    {"id": "email", "name": "email", "type": "string"},
                ],
            },
            {
                "id": "type-audit", "name": "Audit",
                "displayName": "审计记录", "primaryKey": "audit-id",
                "properties": [
                    {"id": "audit-id", "name": "id", "type": "string",
                     "required": True},
                ],
            },
            {
                "id": "type-recipient", "name": "Recipient",
                "displayName": "收件人", "primaryKey": "recipient-id",
                "properties": [
                    {"id": "recipient-id", "name": "id", "type": "string",
                     "required": True},
                    {"id": "recipient-email", "name": "email",
                     "type": "string"},
                ],
            },
        ],
        "linkTypes": [
            {
                "id": "link-audit", "name": "has_audit",
                "displayName": "审计链接",
                "sourceObjectTypeId": "type-order",
                "targetObjectTypeId": "type-audit",
                "cardinality": "one-to-many", "properties": [],
            },
            {
                "id": "link-recipient", "name": "has_recipient",
                "displayName": "收件人链接",
                "sourceObjectTypeId": "type-order",
                "targetObjectTypeId": "type-recipient",
                "cardinality": "one-to-many", "properties": [],
            },
        ],
        "functions": [],
        "actions": [{
            "id": "action-review", "name": "review_order",
            "displayName": "复核订单", "objectTypeId": "type-order",
            "parameters": [],
            "rules": [
                {
                    "id": "validate-amount", "name": "金额校验",
                    "type": "validation", "enabled": True, "order": 0,
                    "config": {
                        "condition": "object.amount > 0",
                        "errorMessage": "金额必须为正数",
                    },
                },
                {
                    "id": "create-audit", "name": "创建审计",
                    "type": "create_object", "enabled": True, "order": 1,
                    "config": {
                        "targetObjectTypeId": "type-audit",
                        "propertyMappings": [],
                    },
                },
                {
                    "id": "update-status", "name": "更新状态",
                    "type": "update_property", "enabled": True, "order": 2,
                    "config": {
                        "targetProperty": "status",
                        "valueSource": "constant",
                        "value": "\"reviewed\"",
                    },
                },
                {
                    "id": "link-audit", "name": "关联审计",
                    "type": "create_link", "enabled": True, "order": 3,
                    "config": {
                        "linkTypeId": "link-audit",
                        "targetSource": "created_object",
                        "targetValue": "type-audit",
                    },
                },
                {
                    "id": "delete-old-recipient", "name": "删除旧收件人",
                    "type": "delete_link", "enabled": True, "order": 4,
                    "config": {
                        "linkTypeId": "link-recipient",
                        "condition": (
                            "target.email == 'old@example.com'"),
                    },
                },
                {
                    "id": "notify-owner", "name": "站内通知",
                    "type": "notification", "enabled": True, "order": 5,
                    "config": {
                        "channel": "internal",
                        "recipientSource": "property",
                        "recipient": "email",
                        "messageTemplate": (
                            "order={{object.id}},status={{object.status}}"),
                    },
                },
                {
                    "id": "webhook-review", "name": "Webhook",
                    "type": "webhook", "enabled": True, "order": 6,
                    "config": {
                        "url": "https://8.8.8.8/review",
                        "method": "POST",
                        "bodyTemplate": (
                            "{\"id\":\"{{object.id}}\","
                            "\"status\":\"{{object.status}}\"}"),
                    },
                },
            ],
        }],
        "mappings": [], "linkMappings": [],
        "sentinels": [{
            "id": "sentinel-review", "name": "review",
            "displayName": "订单复核", "enabled": True, "muted": False,
            "bindings": [{
                "alias": "order", "objectTypeId": "type-order",
            }],
            "primaryAlias": "order", "links": [],
            "condition": "order.amount >= 100",
            "actionIds": ["action-review"],
            "actionParameters": {},
        }],
    }
    objects = [
        {
            "objectId": "order-1", "objectTypeId": "type-order",
            "properties": {
                "id": "O-1", "amount": 120, "status": "new",
                "email": "owner@example.com",
            },
        },
        {
            "objectId": "recipient-1",
            "objectTypeId": "type-recipient",
            "properties": {
                "id": "R-1", "email": "old@example.com",
            },
        },
    ]
    links = [{
        "linkId": "old-recipient-link",
        "linkTypeId": "link-recipient",
        "sourceObjectId": "order-1",
        "targetObjectId": "recipient-1",
        "properties": {},
    }]

    result = _simulate_sentinels(snapshot, objects, links)[0]

    assert result["activation"] == "active"
    assert result["errors"] == []
    plan = result["plannedActionSamples"][0]
    assert plan["status"] == "success"
    assert [item["type"] for item in plan["effects"]] == [
        "create_object", "update_property", "create_link",
        "delete_link", "notification", "webhook",
    ]
    assert all(
        item["status"] == "preview"
        and item["committed"] is False
        for item in plan["effects"])
    assert plan["effects"][3]["matchedLinkIds"] == [
        "old-recipient-link"]
    assert plan["effects"][4]["recipient"] == "owner@example.com"
    assert plan["effects"][5]["method"] == "POST"
    assert plan["sideEffects"] == "none"

    # Disabled/muted controls runtime activation only. Their definitions still
    # need a complete trial; otherwise enabling/unmuting later would reveal an
    # action failure that the isolated workspace silently skipped.
    snapshot["sentinels"][0]["enabled"] = False
    snapshot["actions"][0]["rules"][0]["config"]["condition"] = (
        "object.missing_amount > 0")
    disabled = _simulate_sentinels(snapshot, objects, links)[0]
    assert disabled["activation"] == "disabled"
    assert any("missing_amount" in item for item in disabled["errors"])
    snapshot["sentinels"][0]["enabled"] = True
    snapshot["sentinels"][0]["muted"] = True
    muted = _simulate_sentinels(snapshot, objects, links)[0]
    assert muted["activation"] == "muted"
    assert any("missing_amount" in item for item in muted["errors"])


def test_trial_derived_fails_closed_for_missing_and_disabled_functions():
    base_snapshot = {
        "objectTypes": [{
            "id": "type-order", "name": "Order", "displayName": "订单",
            "primaryKey": "order-id",
            "properties": [
                {"id": "order-id", "name": "id", "type": "string",
                 "required": True},
                {"id": "order-score", "name": "score", "type": "number",
                 "source": "computed", "computed": True,
                 "functionId": "fn-score"},
            ],
        }],
        "linkTypes": [], "actions": [], "mappings": [],
        "linkMappings": [], "sentinels": [],
    }

    missing_objects = [{
        "objectId": "order-missing", "objectTypeId": "type-order",
        "properties": {"id": "O-missing"},
    }]
    missing_snapshot = {**base_snapshot, "functions": []}
    missing_errors = _compute_trial_derived(
        missing_snapshot, missing_objects)
    assert missing_objects[0]["computed"] == {}
    assert {
        item["code"] for item in missing_errors
    } == {"derived_property_evaluation_failed"}
    assert "函数不存在" in missing_errors[0]["message"]

    disabled_objects = [{
        "objectId": "order-disabled", "objectTypeId": "type-order",
        "properties": {"id": "O-disabled"},
    }]
    disabled_snapshot = {
        **base_snapshot,
        "functions": [{
            "id": "fn-score", "name": "score", "displayName": "评分",
            "functionType": "object", "language": "expression",
            "targetObjectTypeId": "type-order", "parameters": [],
            "returnType": "number", "body": "1", "enabled": False,
        }],
    }
    disabled_errors = _compute_trial_derived(
        disabled_snapshot, disabled_objects)
    assert disabled_objects[0]["computed"] == {}
    assert {
        item["code"] for item in disabled_errors
    } == {"derived_property_evaluation_failed"}
    assert "已禁用" in disabled_errors[0]["message"]


def test_trial_derived_uses_the_same_object_collection_scope_as_production():
    snapshot = {
        "objectTypes": [
            {
                "id": "type-order", "name": "Order", "displayName": "订单",
                "primaryKey": "order-id",
                "properties": [
                    {"id": "order-id", "name": "id", "type": "string",
                     "required": True},
                    {
                        "id": "order-local-count", "name": "localCount",
                        "type": "number", "source": "computed",
                        "computed": True, "functionId": "fn-local-count",
                    },
                    {
                        "id": "order-set-count", "name": "orderCount",
                        "type": "number", "source": "computed",
                        "computed": True, "functionId": "fn-set-count",
                    },
                ],
            },
            {
                "id": "type-customer", "name": "Customer",
                "displayName": "客户", "primaryKey": "customer-id",
                "properties": [{
                    "id": "customer-id", "name": "id", "type": "string",
                    "required": True,
                }],
            },
        ],
        "linkTypes": [], "actions": [], "mappings": [],
        "linkMappings": [], "sentinels": [],
        "functions": [
            {
                "id": "fn-local-count", "name": "local_count",
                "displayName": "对象函数集合可见数",
                "functionType": "object", "language": "expression",
                "targetObjectTypeId": "type-order", "parameters": [],
                "returnType": "number", "body": "len(objects)",
                "enabled": True,
            },
            {
                "id": "fn-set-count", "name": "set_count",
                "displayName": "订单集合数",
                "functionType": "object_set", "language": "expression",
                "targetObjectTypeId": "type-order", "parameters": [],
                "returnType": "number", "body": "len(objects)",
                "enabled": True,
            },
        ],
    }
    objects = [
        {
            "objectId": "order-1", "objectTypeId": "type-order",
            "properties": {"id": "O-1"},
        },
        {
            "objectId": "order-2", "objectTypeId": "type-order",
            "properties": {"id": "O-2"},
        },
        {
            "objectId": "customer-1", "objectTypeId": "type-customer",
            "properties": {"id": "C-1"},
        },
    ]

    assert _compute_trial_derived(snapshot, objects) == []
    # Production exposes no collection to an object function, while an
    # object_set function sees only its target object type.
    assert objects[0]["computed"] == {
        "localCount": 0,
        "orderCount": 2,
    }
    assert objects[1]["computed"] == {
        "localCount": 0,
        "orderCount": 2,
    }


def test_trial_derived_rejects_result_outside_computed_property_type():
    snapshot = {
        "objectTypes": [{
            "id": "type-order", "name": "Order", "displayName": "订单",
            "primaryKey": "order-id",
            "properties": [
                {"id": "order-id", "name": "id", "type": "string",
                 "required": True},
                {"id": "order-score", "name": "score", "type": "number",
                 "source": "computed", "computed": True,
                 "functionId": "fn-score"},
            ],
        }],
        "functions": [{
            "id": "fn-score", "name": "score", "displayName": "评分",
            "functionType": "object", "language": "expression",
            "targetObjectTypeId": "type-order", "parameters": [],
            "returnType": "number", "body": "'not-a-number'",
            "enabled": True,
        }],
        "linkTypes": [], "actions": [], "mappings": [],
        "linkMappings": [], "sentinels": [],
    }
    objects = [{
        "objectId": "order-1", "objectTypeId": "type-order",
        "properties": {"id": "O-1"},
    }]

    errors = _compute_trial_derived(snapshot, objects)

    assert objects[0]["computed"] == {}
    assert [item["code"] for item in errors] == [
        "property_type_mismatch",
    ]
    assert "期望 number，实际为 str" in errors[0]["message"]


def test_structure_only_draft_cannot_enter_trial(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    draft = _draft(client, auth_headers, oid, root["id"])
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text
    _attach_semantic_layer(db, draft["id"])

    trial = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert trial.status_code == 422, trial.text
    detail = trial.json()["detail"]
    assert detail["code"] == "publish_validation_failed"
    assert {item["code"] for item in detail["errors"]} == {
        "trial_object_mapping_required",
    }


def _finish_stub_trial(candidate: SimpleNamespace) -> None:
    candidate.dataset_versions = []
    candidate.result_json = {
        "counts": {"objects": 0, "links": 0, "facts": 0, "datasets": 0},
        "errors": [],
        "warnings": [],
        "samples": {"objects": [], "links": []},
        "actionsExecuted": 0,
        "sideEffects": "blocked",
    }
    candidate.status = "passed"
    candidate.completed_at = datetime.now(timezone.utc)


def test_trial_start_is_single_flight_while_first_materialization_is_running(
        client, auth_headers, ontology, admin_user, db, monkeypatch):
    """A second API worker observes the durable first claim and fails 409."""
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, _root(
            client, auth_headers, oid)["id"]),
        db,
    )
    actor = SimpleNamespace(id=admin_user.id, username=admin_user.username)
    db.rollback()

    entered = threading.Event()
    release = threading.Event()

    def blocked_materialize(_session, candidate, _snapshot):
        entered.set()
        assert release.wait(5), "test did not release trial materialization"
        _finish_stub_trial(candidate)
        return candidate.result_json

    monkeypatch.setattr(
        version_router, "materialize_trial", blocked_materialize)
    factory = sessionmaker(
        bind=db.get_bind(), autoflush=False, expire_on_commit=True)
    outcome: dict[str, object] = {}

    def start_first() -> None:
        session = factory()
        try:
            outcome["response"] = version_router.create_trial_run(
                oid, draft["id"], {}, session, actor)
        except BaseException as exc:  # captured for assertion in the test thread
            outcome["error"] = exc
        finally:
            session.close()

    worker = threading.Thread(target=start_first, daemon=True)
    worker.start()
    try:
        assert entered.wait(5), "first trial did not reach materialization"
        competing_session = factory()
        try:
            with pytest.raises(HTTPException) as caught:
                version_router.create_trial_run(
                    oid, draft["id"], {}, competing_session, actor)
            assert caught.value.status_code == 409
            assert caught.value.detail["code"] == "trial_already_running"
            assert caught.value.detail["trialRunId"]
            assert caught.value.detail["leaseExpiresAt"]
        finally:
            competing_session.rollback()
            competing_session.close()
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    first = outcome["response"]["data"]
    assert first["status"] == "passed"
    db.expire_all()
    assert db.query(OntologyTrialRun).filter_by(
        version_id=draft["id"]).count() == 1


def test_expired_trial_is_recovered_for_retry_and_branch_deletion(
        client, auth_headers, ontology, admin_user, db, monkeypatch):
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, root["id"]),
        db,
    )
    now = datetime.now(timezone.utc)
    expired = OntologyTrialRun(
        id="expired-trial-for-retry",
        ontology_id=oid,
        version_id=draft["id"],
        revision=draft["revision"],
        snapshot_hash=draft["snapshot_hash"],
        base_release_id=root["id"],
        claim_token="expired-claim-for-retry",
        lease_expires_at=now - timedelta(seconds=1),
        status="running",
        dataset_versions=[],
        result_json={},
        impact_hash="expired-impact",
        created_by=admin_user.id,
        created_at=now - timedelta(hours=2),
    )
    db.add(expired)
    db.commit()

    def successful_materialize(_session, candidate, _snapshot):
        _finish_stub_trial(candidate)
        return candidate.result_json

    monkeypatch.setattr(
        version_router, "materialize_trial", successful_materialize)
    retried = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers,
        json={},
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["data"]["status"] == "passed"
    assert retried.json()["data"]["id"] != expired.id

    db.expire_all()
    recovered = db.query(OntologyTrialRun).filter_by(id=expired.id).one()
    assert recovered.status == "stale"
    assert recovered.claim_token is None
    assert recovered.lease_expires_at is None
    assert recovered.result_json["errors"][0]["code"] == "trial_run_timeout"

    deletable = _draft(client, auth_headers, oid, root["id"])
    abandoned = OntologyTrialRun(
        id="expired-trial-for-delete",
        ontology_id=oid,
        version_id=deletable["id"],
        revision=deletable["revision"],
        snapshot_hash=deletable["snapshot_hash"],
        base_release_id=root["id"],
        claim_token="expired-claim-for-delete",
        lease_expires_at=now - timedelta(seconds=1),
        status="running",
        dataset_versions=[],
        result_json={},
        impact_hash="expired-delete-impact",
        created_by=admin_user.id,
        created_at=now - timedelta(hours=2),
    )
    db.add(abandoned)
    db.commit()
    abandoned_id = abandoned.id
    deleted = client.delete(
        f"/api/v2/ontologies/{oid}/versions/{deletable['id']}",
        headers=auth_headers,
    )
    assert deleted.status_code == 200, deleted.text
    db.expire_all()
    assert db.query(OntologyVersion).filter_by(
        id=deletable["id"]).first() is None
    assert db.query(OntologyTrialRun).filter_by(
        id=abandoned_id).first() is None


def test_late_trial_completion_cannot_revive_a_drifted_draft(
        client, auth_headers, ontology, admin_user, db, monkeypatch):
    """Completion re-locks the draft and fences lifecycle/revision/hash drift."""
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, _root(
            client, auth_headers, oid)["id"]),
        db,
    )
    actor = SimpleNamespace(id=admin_user.id, username=admin_user.username)
    db.rollback()
    entered = threading.Event()
    release = threading.Event()

    def blocked_materialize(_session, candidate, _snapshot):
        entered.set()
        assert release.wait(5), "test did not release trial materialization"
        _finish_stub_trial(candidate)
        return candidate.result_json

    monkeypatch.setattr(
        version_router, "materialize_trial", blocked_materialize)
    factory = sessionmaker(
        bind=db.get_bind(), autoflush=False, expire_on_commit=True)
    outcome: dict[str, object] = {}

    def start_trial() -> None:
        session = factory()
        try:
            outcome["response"] = version_router.create_trial_run(
                oid, draft["id"], {}, session, actor)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            session.close()

    worker = threading.Thread(target=start_trial, daemon=True)
    worker.start()
    try:
        assert entered.wait(5), "trial did not reach materialization"
        drift_session = factory()
        try:
            drifted = drift_session.query(OntologyVersion).filter_by(
                id=draft["id"]).one()
            drifted.lifecycle_status = "superseded"
            drifted.revision = (drifted.revision or 0) + 1
            changed_snapshot = copy.deepcopy(drifted.snapshot_formal)
            changed_snapshot["objectTypes"][0]["displayName"] = "迟到修改"
            drifted.snapshot_formal = changed_snapshot
            drifted.snapshot_hash = "f" * 64
            current = drift_session.query(OntologyVersion).filter_by(
                id=drifted.base_release_id).one()
            concurrent_release_id = "release-created-during-trial"
            drift_session.add(OntologyVersion(
                id=concurrent_release_id,
                ontology_id=oid,
                version_number="v-concurrent",
                version_label="并发发布",
                base_release_id=concurrent_release_id,
                node_kind="release",
                lifecycle_status="released",
                revision=0,
                snapshot_formal=copy.deepcopy(current.snapshot_formal),
                snapshot_hash=current.snapshot_hash,
                published_at=datetime.now(timezone.utc),
                created_by=actor.id,
            ))
            project = drift_session.query(OntologyProject).filter_by(
                id=oid).one()
            project.current_release_id = concurrent_release_id
            project.version = "v-concurrent"
            drift_session.commit()
        finally:
            drift_session.close()
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    late = outcome["response"]["data"]
    assert late["status"] == "stale"
    error = late["result"]["errors"][0]
    assert error["code"] == "trial_completion_conflict"
    assert {
        "lifecycle_changed",
        "revision_changed",
        "snapshot_hash_changed",
        "snapshot_content_changed",
        "current_release_changed",
    }.issubset(set(error["conflicts"]))

    db.expire_all()
    stored_draft = db.query(OntologyVersion).filter_by(
        id=draft["id"]).one()
    stored_run = db.query(OntologyTrialRun).filter_by(id=late["id"]).one()
    assert stored_draft.lifecycle_status == "superseded"
    assert stored_draft.revision == draft["revision"] + 1
    assert stored_run.status == "stale"
    assert stored_run.claim_token is None
    assert db.query(OntologyTrialObject).filter_by(
        trial_run_id=stored_run.id).count() == 0


def test_one_mapped_object_can_enter_trial_with_other_types_unmapped(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    draft = _draft(client, auth_headers, oid, _root(
        client, auth_headers, oid)["id"])
    workspace = _workspace(draft)
    workspace["objectTypes"].append({
        "id": "ot-customer", "name": "Customer", "displayName": "客户",
        "primaryKey": "customer-id", "positionX": 360, "positionY": 20,
        "properties": [{
            "id": "customer-id", "name": "id", "displayName": "客户编号",
            "type": "string", "required": True,
        }],
    })
    workspace["linkTypes"].append({
        "id": "lt-owner", "name": "owned_by", "displayName": "所属客户",
        "sourceObjectTypeId": "ot-order",
        "targetObjectTypeId": "ot-customer",
        "cardinality": "many-to-one",
        "properties": [],
    })
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=workspace,
    )
    assert saved.status_code == 200, saved.text
    revision = saved.json()["data"]["revision"]
    mapped = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers, json={
            "baseRevision": revision,
            "mappings": [{
                "id": "mapping-order", "curatedDatasetId": "dataset-orders",
                "entityClass": "Order", "targetObjectTypeId": "ot-order",
                "fieldMapping": {
                    "id": "id", "name": "name", "__primary_key__": "id",
                },
                "status": "draft", "confidence": 1,
            }],
            "linkMappings": [], "sentinels": [],
        },
    )
    assert mapped.status_code == 200, mapped.text
    _attach_semantic_layer(db, draft["id"])

    trial = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert trial.status_code == 201, trial.text
    run = trial.json()["data"]
    assert run["status"] == "passed"
    assert run["result"]["counts"] == {
        "objects": 2, "links": 0, "facts": 4, "datasets": 1,
    }
    assert {item["code"] for item in run["result"]["warnings"]} == {
        "object_type_unmapped",
    }


def test_version_tree_uses_complete_snapshots_and_dependency_numbering(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    first = _draft(client, auth_headers, oid, root["id"])
    assert first["version_number"] == "v0.1"
    configured = _configure_draft(client, auth_headers, oid, first, db)

    nested = _draft(client, auth_headers, oid, configured["id"])
    sibling = _draft(client, auth_headers, oid, root["id"])
    assert nested["version_number"] == "v0.1.1"
    assert sibling["version_number"] == "v0.2"

    release_workspace = client.get(
        f"/api/v2/ontologies/{oid}/versions/{root['id']}/workspace",
        headers=auth_headers,
    )
    assert release_workspace.status_code == 200, release_workspace.text
    assert release_workspace.json()["data"]["workspaceMode"] == "release"
    assert release_workspace.json()["data"]["editable"] is False

    draft_workspace = client.get(
        f"/api/v2/ontologies/{oid}/versions/{nested['id']}/workspace",
        headers=auth_headers,
    )
    assert draft_workspace.status_code == 200, draft_workspace.text
    assert draft_workspace.json()["data"]["workspaceMode"] == "draft"
    assert draft_workspace.json()["data"]["editable"] is True

    detail = client.get(
        f"/api/v2/ontologies/{oid}/versions/{nested['id']}",
        headers=auth_headers).json()["data"]
    formal = detail["snapshot"]["formal"]
    assert set(formal) == {
        "objectTypes", "linkTypes", "actions", "functions",
        "sentinels", "mappings", "linkMappings",
    }
    assert formal["objectTypes"][0]["id"] == "ot-order"
    assert formal["mappings"][0]["id"] == "mapping-order"
    assert db.query(ObjectInstance).filter_by(ontology_id=oid).count() == 0


def test_only_unpublished_leaf_branches_can_be_deleted(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    parent = _draft(client, auth_headers, oid, root["id"])
    child = _draft(client, auth_headers, oid, parent["id"])

    non_leaf = client.delete(
        f"/api/v2/ontologies/{oid}/versions/{parent['id']}",
        headers=auth_headers,
    )
    assert non_leaf.status_code == 409, non_leaf.text
    assert non_leaf.json()["detail"]["code"] == "version_not_leaf"

    deleted_child = client.delete(
        f"/api/v2/ontologies/{oid}/versions/{child['id']}",
        headers=auth_headers,
    )
    assert deleted_child.status_code == 200, deleted_child.text
    assert deleted_child.json()["data"]["id"] == child["id"]

    # Deleted version numbers remain reserved in the audit trail. Recreating a
    # child under the same parent must advance instead of reusing v0.1.1.
    replacement = _draft(client, auth_headers, oid, parent["id"])
    assert replacement["version_number"] == "v0.1.2"
    assert client.delete(
        f"/api/v2/ontologies/{oid}/versions/{replacement['id']}",
        headers=auth_headers,
    ).status_code == 200

    # A trial-ready branch is still unpublished and may be deleted when it is
    # a leaf; isolated objects/runs are removed by the FK cascade.
    _dataset(db, monkeypatch)
    configured = _configure_draft(client, auth_headers, oid, parent, db)
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{parent['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert run.status_code == 201, run.text
    assert run.json()["data"]["status"] == "passed"
    deleted_trial = client.delete(
        f"/api/v2/ontologies/{oid}/versions/{configured['id']}",
        headers=auth_headers,
    )
    assert deleted_trial.status_code == 200, deleted_trial.text
    db.expire_all()
    assert db.query(OntologyTrialRun).filter_by(version_id=parent["id"]).count() == 0

    immutable_release = client.delete(
        f"/api/v2/ontologies/{oid}/versions/{root['id']}",
        headers=auth_headers,
    )
    assert immutable_release.status_code == 409, immutable_release.text
    assert immutable_release.json()["detail"]["code"] == "version_delete_forbidden"


def test_legacy_current_version_is_frozen_as_complete_migration_baseline(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    row = db.query(OntologyVersion).filter_by(id=root["id"]).one()
    row.snapshot_formal = None
    row.snapshot_hash = None
    db.commit()

    tree_response = client.get(
        f"/api/v2/ontologies/{oid}/version-tree", headers=auth_headers)
    assert tree_response.status_code == 200, tree_response.text
    db.expire_all()
    migrated = db.query(OntologyVersion).filter_by(id=root["id"]).one()
    assert set(migrated.snapshot_formal) == {
        "objectTypes", "linkTypes", "actions", "functions",
        "sentinels", "mappings", "linkMappings",
    }
    assert len(migrated.snapshot_hash) == 64


def test_trial_uses_real_pinned_data_but_isolates_runtime_and_side_effects(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, _root(client, auth_headers, oid)["id"]),
        db,
    )

    trial = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert trial.status_code == 201, trial.text
    run = trial.json()["data"]
    assert run["status"] == "passed", run
    assert run["result"]["counts"] == {
        "objects": 2, "links": 0, "facts": 4, "datasets": 1}
    assert run["result"]["actionsExecuted"] == 0
    assert run["result"]["sideEffects"] == "blocked"
    assert len(run["dataset_versions"]) == 1
    assert db.query(ObjectInstance).filter_by(ontology_id=oid).count() == 0
    assert db.query(PropertyFact).filter_by(ontology_id=oid).count() == 0
    assert db.query(OntologyTrialObject).filter_by(trial_run_id=run["id"]).count() == 2


def test_trial_freezes_computed_projection_and_promotion_activates_exact_values(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    draft = _draft(client, auth_headers, oid, root["id"])
    workspace = _workspace(draft)
    workspace["objectTypes"][0]["properties"].append({
        "id": "p-label", "name": "label", "displayName": "派生标签",
        "type": "string", "required": False,
        "source": "computed", "computed": True,
        "functionId": "fn-label",
    })
    workspace["functions"] = [{
        "id": "fn-label", "name": "derive_label",
        "displayName": "生成派生标签", "functionType": "object",
        "language": "expression", "targetObjectTypeId": "ot-order",
        "parameters": [], "returnType": "string",
        "body": "object['name'] + '-derived'", "enabled": True,
    }]
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=workspace,
    )
    assert saved.status_code == 200, saved.text
    mapping = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers,
        json={
            "baseRevision": saved.json()["data"]["revision"],
            "mappings": [{
                "id": "mapping-order",
                "curatedDatasetId": "dataset-orders",
                "entityClass": "Order",
                "targetObjectTypeId": "ot-order",
                "fieldMapping": {
                    "id": "id", "name": "name", "__primary_key__": "id",
                },
                "status": "draft", "confidence": 1,
            }],
            "linkMappings": [], "sentinels": [],
        },
    )
    assert mapping.status_code == 200, mapping.text
    _attach_semantic_layer(db, draft["id"])

    trial = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert trial.status_code == 201, trial.text
    run = trial.json()["data"]
    assert run["status"] == "passed", run
    frozen = db.query(OntologyTrialObject).filter_by(
        trial_run_id=run["id"]).order_by(OntologyTrialObject.object_id).all()
    assert {item.computed["label"] for item in frozen} == {
        "一号订单-derived", "二号订单-derived",
    }
    trial_workspace = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers,
    )
    assert trial_workspace.status_code == 200, trial_workspace.text
    assert {
        item["computed"]["label"]
        for item in trial_workspace.json()["data"]["instances"]
    } == {"一号订单-derived", "二号订单-derived"}

    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert impact["releaseReadiness"]["ready"] is True
    assert impact["releaseReadiness"][
        "runtimeStateConflicts"]["linkConflictCount"] == 0
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": run["id"],
            "impactHash": impact["impactHash"],
        },
    )
    assert promoted.status_code == 201, promoted.text
    release_id = promoted.json()["data"]["id"]
    assert {
        item.computed["label"]
        for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid, ontology_release_id=release_id).all()
    } == {"一号订单-derived", "二号订单-derived"}


def test_trial_keeps_same_dataset_mappings_separate_by_endpoint_type(
        client, auth_headers, ontology, db, monkeypatch):
    """同一资产映射多个对象类型时，关系端点不能被最后一个映射覆盖。"""
    oid = ontology["id"]
    dataset_service = _paired_dataset(db, monkeypatch)
    draft = _draft(client, auth_headers, oid, _root(
        client, auth_headers, oid)["id"])
    workspace = {
        "baseRevision": f"{draft['revision']}:{draft['snapshot_hash']}",
        "version": draft["version_number"],
        "objectTypes": [
            {"id": "ot-left", "name": "Left", "displayName": "左对象",
             "primaryKey": "left-id", "properties": [
                 {"id": "left-id", "name": "left_id", "displayName": "左ID",
                  "type": "string", "required": True}]},
            {"id": "ot-right", "name": "Right", "displayName": "右对象",
             "primaryKey": "right-id", "properties": [
                 {"id": "right-id", "name": "right_id", "displayName": "右ID",
                  "type": "string", "required": True}]},
        ],
        "linkTypes": [{
            "id": "lt-pair", "name": "paired_with", "displayName": "配对",
            "sourceObjectTypeId": "ot-left", "targetObjectTypeId": "ot-right",
            "cardinality": "one-to-one", "properties": [],
        }],
        "actions": [], "functions": [], "instances": [], "linkInstances": [],
    }
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=workspace)
    assert saved.status_code == 200, saved.text
    revision = saved.json()["data"]["revision"]
    mapped = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers, json={
            "baseRevision": revision,
            "mappings": [
                {"id": "map-left", "curatedDatasetId": "dataset-pairs",
                 "entityClass": "Left", "targetObjectTypeId": "ot-left",
                 "fieldMapping": {"left_id": "left_id", "__primary_key__": "left_id"}},
                {"id": "map-right", "curatedDatasetId": "dataset-pairs",
                 "entityClass": "Right", "targetObjectTypeId": "ot-right",
                 "fieldMapping": {"right_id": "right_id", "__primary_key__": "right_id"}},
            ],
            "linkMappings": [{
                "id": "lm-pairs", "linkTypeId": "lt-pair",
                "relationType": "paired_with", "srcDatasetId": "dataset-pairs",
                "tgtDatasetId": "dataset-pairs", "edgeDatasetId": None,
                "srcKey": "left_id", "tgtKey": "right_id", "fieldMapping": {},
            }],
            "sentinels": [],
        })
    assert mapped.status_code == 200, mapped.text
    _attach_semantic_layer(db, draft["id"])

    trial = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={})
    assert trial.status_code == 201, trial.text
    run = trial.json()["data"]
    assert run["status"] == "passed", run
    assert run["result"]["counts"]["objects"] == 4
    assert run["result"]["counts"]["links"] == 2
    assert db.query(OntologyTrialLink).filter_by(trial_run_id=run["id"]).count() == 2
    trial_objects = db.query(OntologyTrialObject).filter_by(
        trial_run_id=run["id"]).all()
    trial_links = db.query(OntologyTrialLink).filter_by(
        trial_run_id=run["id"]).all()
    trial_object_ids = {item.object_id for item in trial_objects}
    trial_link_ids = {item.link_id for item in trial_links}
    trial_relation_ids = {item.source_relation_id for item in trial_links}
    assert None not in trial_relation_ids
    assert {
        item.external_id for item in trial_objects
    }.isdisjoint({"left_id:PAIR-1", "right_id:PAIR-1"})

    # Real sample-data closure: promote the isolated projection, then run the
    # normal MappingService against the exact same approved lake version.  No
    # object/link may be duplicated or re-keyed and every promoted edge must
    # adopt the real Relation lineage produced by the mapping run.
    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert impact["releaseReadiness"]["ready"] is True
    assert impact["releaseReadiness"][
        "runtimeStateConflicts"]["linkConflictCount"] == 0
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 201, promoted.text
    release_id = promoted.json()["data"]["id"]
    db.expire_all()
    assert {
        item.id for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid).all()
    } == trial_object_ids
    assert {
        item.id for item in db.query(LinkInstance).filter_by(
            ontology_id=oid).all()
    } == trial_link_ids
    assert {
        item.source_relation_id for item in db.query(LinkInstance).filter_by(
            ontology_id=oid).all()
    } == trial_relation_ids

    db.add_all([
        ObjectInstance(
            id="action-created-alert",
            ontology_id=oid,
            ontology_release_id=release_id,
            object_type_id="ot-left",
            properties={"left_id": "ACTION-ALERT"},
            computed={},
            source="action",
            external_id=None,
        ),
        ObjectInstance(
            id="manual-created-note",
            ontology_id=oid,
            ontology_release_id=release_id,
            object_type_id="ot-right",
            properties={"right_id": "MANUAL-NOTE"},
            computed={},
            source="manual",
            external_id=None,
        ),
    ])
    db.commit()

    # The next real lake version removes *all* rows before any legacy Entity has
    # ever been materialized.  A zero-row version is an authoritative source
    # state, not permission to skip projection.  Only Formal reconciliation can
    # see and tombstone these trial-promoted objects/links; action/manual
    # business state must survive.
    dataset_service.create_version(
        "dataset-pairs",
        _empty_csv(["left_id", "right_id"]),
        rowcount=0,
    )

    projection = MappingService(db).build_all(oid, require_approved=True)
    assert projection["formal_projection"]["object_instances"] == 0
    assert projection["formal_projection"]["link_instances"] == 0
    assert projection["formal_projection"]["removed_object_instances"] == 4
    assert projection["formal_projection"]["removed_link_instances"] == 2
    db.expire_all()
    assert db.query(Entity).filter_by(ontology_id=oid).count() == 0
    assert {
        item.id for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid).all()
    } == {"action-created-alert", "manual-created-note"}
    assert db.query(ObjectInstance).filter_by(
        id="action-created-alert", source="action").one()
    assert db.query(ObjectInstance).filter_by(
        id="manual-created-note", source="manual").one()
    assert {
        item.external_id for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid, source="pipeline").all()
    } == set()
    assert db.query(LinkInstance).filter_by(ontology_id=oid).count() == 0
    assert db.query(Relation).filter_by(ontology_id=oid).count() == 0


def test_trial_uses_each_object_mapping_primary_key_for_order_supplier_fat_table(
        client, auth_headers, ontology, db, monkeypatch):
    """订单宽表应生成独立 Order/Supplier 对象，并完整连接四条供货关系。"""
    oid = ontology["id"]
    _order_supplier_dataset(db, monkeypatch)
    draft = _draft(client, auth_headers, oid, _root(
        client, auth_headers, oid)["id"])
    workspace = {
        "baseRevision": f"{draft['revision']}:{draft['snapshot_hash']}",
        "version": draft["version_number"],
        "objectTypes": [
            {
                "id": "ot-order",
                "name": "Order",
                "displayName": "订单",
                "primaryKey": "prop-order-id",
                "properties": [{
                    "id": "prop-order-id",
                    "name": "order_id",
                    "displayName": "订单编号",
                    "type": "string",
                    "required": True,
                }],
            },
            {
                "id": "ot-supplier",
                "name": "Supplier",
                "displayName": "供应商",
                "primaryKey": "prop-supplier-id",
                "properties": [
                    {
                        "id": "prop-supplier-id",
                        "name": "supplier_id",
                        "displayName": "供应商编号",
                        "type": "string",
                        "required": True,
                    },
                    {
                        "id": "prop-supplier-name",
                        "name": "supplier_name",
                        "displayName": "供应商名称",
                        "type": "string",
                    },
                ],
            },
        ],
        "linkTypes": [{
            "id": "lt-supplied-by",
            "name": "supplied_by",
            "displayName": "由供应商供货",
            "sourceObjectTypeId": "ot-order",
            "targetObjectTypeId": "ot-supplier",
            "cardinality": "many-to-one",
            "properties": [
                {
                    "id": "prop-link-tags",
                    "name": "tags",
                    "displayName": "标签",
                    "type": "array",
                },
                {
                    "id": "prop-link-details",
                    "name": "details",
                    "displayName": "明细",
                    "type": "object",
                },
            ],
        }],
        "actions": [],
        "functions": [],
        "instances": [],
        "linkInstances": [],
    }
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers,
        json=workspace,
    )
    assert saved.status_code == 200, saved.text

    mapped = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers,
        json={
            "baseRevision": saved.json()["data"]["revision"],
            "mappings": [
                {
                    "id": "map-order",
                    "curatedDatasetId": "dataset-order-suppliers",
                    "entityClass": "Order",
                    "targetObjectTypeId": "ot-order",
                    "fieldMapping": {
                        "order_id": "order_id",
                        "__primary_key__": "order_id",
                    },
                },
                {
                    "id": "map-supplier",
                    "curatedDatasetId": "dataset-order-suppliers",
                    "entityClass": "Supplier",
                    "targetObjectTypeId": "ot-supplier",
                    "fieldMapping": {
                        "supplier_id": "supplier_id",
                        "supplier_name": "supplier_name",
                        "__primary_key__": "supplier_id",
                    },
                },
            ],
            "linkMappings": [{
                "id": "lm-supplied-by",
                "linkTypeId": "lt-supplied-by",
                "relationType": "supplied_by",
                "srcDatasetId": "dataset-order-suppliers",
                "tgtDatasetId": "dataset-order-suppliers",
                "edgeDatasetId": "dataset-order-suppliers",
                "srcKey": "order_id",
                "tgtKey": "supplier_id",
                "fieldMapping": {
                    "tags": "tags",
                    "details": "details",
                },
            }],
            "sentinels": [],
        },
    )
    assert mapped.status_code == 200, mapped.text
    _attach_semantic_layer(db, draft["id"])

    trial_response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers,
        json={},
    )
    assert trial_response.status_code == 201, trial_response.text
    trial = trial_response.json()["data"]
    assert trial["status"] == "passed", trial
    assert trial["result"]["counts"]["objects"] == 8
    assert trial["result"]["counts"]["links"] == 4
    assert trial["result"]["errors"] == []

    trial_objects = db.query(OntologyTrialObject).filter_by(
        trial_run_id=trial["id"]).all()
    assert sum(item.object_type_id == "ot-order"
               for item in trial_objects) == 4
    assert sum(item.object_type_id == "ot-supplier"
               for item in trial_objects) == 4
    trial_links = db.query(OntologyTrialLink).filter_by(
        trial_run_id=trial["id"]).order_by(
        OntologyTrialLink.source_object_id).all()
    assert len(trial_links) == 4
    assert all(
        item.properties["tags"] is None
        or isinstance(item.properties["tags"], list)
        for item in trial_links
    )
    assert all(isinstance(item.properties["details"], dict)
               for item in trial_links)
    assert {item.properties["details"].get("rank") for item in trial_links} == {
        None, 1, 2, 3,
    }

    # Promotion must activate the exact isolated values, and the first regular
    # pipeline refresh must read the same CSV strings through Formal projection
    # without regressing the released LinkInstance properties back to strings.
    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": trial["id"],
            "impactHash": impact["impactHash"],
        },
    )
    assert promoted.status_code == 201, promoted.text
    release_id = promoted.json()["data"]["id"]

    MappingService(db).build_all(oid, require_approved=True)
    db.expire_all()
    runtime_links = db.query(LinkInstance).filter_by(
        ontology_id=oid,
        ontology_release_id=release_id,
    ).all()
    assert len(runtime_links) == 4
    assert all(
        item.properties["tags"] is None
        or isinstance(item.properties["tags"], list)
        for item in runtime_links
    )
    assert all(isinstance(item.properties["details"], dict)
               for item in runtime_links)


def test_passed_trial_is_frozen_and_can_only_continue_in_a_new_branch(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, _root(client, auth_headers, oid)["id"]),
        db,
    )
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={}).json()["data"]
    workspace = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers).json()["data"]
    assert workspace["workspaceMode"] == "trial"
    assert workspace["editable"] is False
    assert workspace["trialRun"]["id"] == run["id"]
    assert workspace["trialRun"]["status"] == "passed"
    assert len(workspace["instances"]) == 2
    assert {item["properties"]["id"] for item in workspace["instances"]} == {
        "O-1", "O-2",
    }
    assert workspace["linkInstances"] == []
    workspace["baseRevision"] = workspace["revision"]
    workspace["objectTypes"][0]["displayName"] = "订单（已修改）"
    frozen = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=workspace)
    assert frozen.status_code == 409, frozen.text
    assert frozen.json()["detail"]["code"] == "trial_snapshot_frozen"

    frozen_mapping = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers, json={"baseRevision": workspace["revision"], "mappings": []})
    assert frozen_mapping.status_code == 409, frozen_mapping.text
    assert frozen_mapping.json()["detail"]["code"] == "trial_snapshot_frozen"

    # 画布坐标属于独立展示元数据：试跑快照冻结后仍可调整，且不会推进
    # revision、改变 snapshot_hash 或污染被试跑验证的正式模型快照。
    # 数据映射工作台的节点位置共享同一布局存储，使用 object:/dataset: 等
    # 命名空间 id，仅接受被版本映射引用的元素。
    frozen_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    frozen_revision = frozen_row.revision
    frozen_hash = frozen_row.snapshot_hash
    saved_layout = client.put(
        f"/api/v2/ontologies/{oid}/layout",
        headers=auth_headers,
        json={
            "versionId": draft["id"],
            "positions": {
                "ot-order": {"x": 640, "y": 360},
                "property:ot-order:p-name": {"x": 920, "y": 430},
                "l1:ot-order": {"x": 180, "y": 140},
                "l2:property:ot-order:p-name": {"x": 480, "y": 320},
                "object:ot-order": {"x": 460, "y": 240},
                "dataset:dataset-orders": {"x": 60, "y": 120},
            },
        },
    )
    assert saved_layout.status_code == 200, saved_layout.text
    assert saved_layout.json()["data"]["positions"]["object:ot-order"] == {
        "x": 460.0, "y": 240.0,
    }
    assert saved_layout.json()["data"]["positions"]["dataset:dataset-orders"] == {
        "x": 60.0, "y": 120.0,
    }
    unknown_layout_node = client.put(
        f"/api/v2/ontologies/{oid}/layout",
        headers=auth_headers,
        json={
            "versionId": draft["id"],
            "positions": {
                "dataset:dataset-unknown": {"x": 60, "y": 120},
            },
        },
    )
    assert unknown_layout_node.status_code == 422, unknown_layout_node.text
    assert unknown_layout_node.json()["detail"]["code"] == "invalid_canvas_layout"
    unknown_relation_node = client.put(
        f"/api/v2/ontologies/{oid}/layout",
        headers=auth_headers,
        json={
            "versionId": draft["id"],
            "positions": {
                "relation:lt-unknown": {"x": 60, "y": 120},
            },
        },
    )
    assert unknown_relation_node.status_code == 422, unknown_relation_node.text
    assert unknown_relation_node.json()["detail"]["code"] == "invalid_canvas_layout"
    assert saved_layout.json()["data"]["positions"]["property:ot-order:p-name"] == {
        "x": 920.0, "y": 430.0,
    }
    assert saved_layout.json()["data"]["positions"]["l1:ot-order"] == {
        "x": 180.0, "y": 140.0,
    }
    assert saved_layout.json()["data"]["positions"]["l2:property:ot-order:p-name"] == {
        "x": 480.0, "y": 320.0,
    }
    moved_workspace = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers,
    ).json()["data"]
    assert moved_workspace["objectTypes"][0]["positionX"] == 640
    assert moved_workspace["objectTypes"][0]["positionY"] == 360
    assert moved_workspace["canvasLayout"]["property:ot-order:p-name"] == {
        "x": 920.0, "y": 430.0,
    }
    assert moved_workspace["canvasLayout"]["l1:ot-order"] == {
        "x": 180.0, "y": 140.0,
    }
    assert moved_workspace["canvasLayout"]["l2:property:ot-order:p-name"] == {
        "x": 480.0, "y": 320.0,
    }
    db.expire_all()
    frozen_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    assert frozen_row.revision == frozen_revision
    assert frozen_row.snapshot_hash == frozen_hash
    assert frozen_row.snapshot_formal["objectTypes"][0]["positionX"] == 10
    assert frozen_row.snapshot_formal["objectTypes"][0]["positionY"] == 20

    rerun = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={})
    assert rerun.status_code == 409, rerun.text
    assert rerun.json()["detail"]["code"] == "trial_snapshot_frozen"

    db.expire_all()
    assert db.query(OntologyTrialRun).filter_by(id=run["id"]).one().status == "passed"

    next_draft = _draft(client, auth_headers, oid, draft["id"])
    assert next_draft["version_number"] == "v0.1.1"
    next_workspace = client.get(
        f"/api/v2/ontologies/{oid}/versions/{next_draft['id']}/workspace",
        headers=auth_headers).json()["data"]
    assert next_workspace["workspaceMode"] == "draft"
    assert next_workspace["editable"] is True
    assert next_workspace["objectTypes"][0]["positionX"] == 640
    assert next_workspace["objectTypes"][0]["positionY"] == 360
    assert next_workspace["canvasLayout"]["property:ot-order:p-name"] == {
        "x": 920.0, "y": 430.0,
    }
    assert next_workspace["canvasLayout"]["l1:ot-order"] == {
        "x": 180.0, "y": 140.0,
    }
    assert next_workspace["canvasLayout"]["l2:property:ot-order:p-name"] == {
        "x": 480.0, "y": 320.0,
    }
    next_row = db.query(OntologyVersion).filter_by(id=next_draft["id"]).one()
    assert next_row.snapshot_formal["objectTypes"][0]["positionX"] == 10
    assert next_row.snapshot_formal["objectTypes"][0]["positionY"] == 20
    assert next_row.canvas_layout["ot-order"] == {"x": 640.0, "y": 360.0}
    assert next_row.canvas_layout["property:ot-order:p-name"] == {
        "x": 920.0, "y": 430.0,
    }
    assert next_row.canvas_layout["l1:ot-order"] == {"x": 180.0, "y": 140.0}
    assert next_row.canvas_layout["l2:property:ot-order:p-name"] == {
        "x": 480.0, "y": 320.0,
    }


def test_promotion_switches_exact_trial_projection_and_keeps_fact_history(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, root["id"]),
        db,
    )
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={}).json()["data"]
    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers).json()["data"]
    assert impact["releaseReadiness"] == {
        "ready": True,
        "blockingCount": 0,
        "errors": [],
        "trialRunId": run["id"],
        "runtimeStateConflicts": {
            "totalCount": 0,
            "propertyConflictCount": 0,
            "objectConflictCount": 0,
            "linkConflictCount": 0,
            "itemLimit": 50,
            "truncated": False,
            "items": [],
        },
        "repairStrategy": None,
        "repairSourceVersionId": draft["id"],
    }

    # Promotion checks the lifecycle state itself, not merely the existence of
    # a passed run. This protects the transition if persisted state is ever
    # repaired or imported independently of trial records.
    draft_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    draft_row.lifecycle_status = "editing"
    db.commit()
    invalid_transition = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert invalid_transition.status_code == 409, invalid_transition.text
    assert invalid_transition.json()["detail"]["code"] == "trial_ready_required"
    draft_row.lifecycle_status = "trial_ready"
    db.commit()

    dynamic = Sentinel(
        id="dynamic-before-promote", ontology_id=oid,
        name="assistant_orders", display_name="助手订单哨兵",
        bindings=[{"alias": "order", "objectTypeId": "ot-order"}],
        links=[], condition=None, primary_alias="order",
        action_ids=[], action_parameters={},
        enabled=True, status="published", origin="assistant_dynamic",
        bound_release_id=root["id"], definition_revision=1,
        validation_report={"passed": True},
        last_trial_at=datetime.now(timezone.utc),
        last_trial_release_id=root["id"], last_trial_revision=1,
        last_trial_report={"passed": True},
    )
    db.add_all([
        dynamic,
        SentinelMatchState(
            id="dynamic-before-promote-match", ontology_id=oid,
            sentinel_id=dynamic.id, match_key="order=old",
            match_detail={"order": "old"}, runtime_status="completed",
        ),
    ])
    db.commit()

    unsafe_pointer_flushes: list[str] = []

    def capture_release_pointer_order(session, _flush_context, _instances):
        new_release_ids = {
            item.id for item in session.new
            if isinstance(item, OntologyVersion) and item.node_kind == "release"
        }
        for item in session.dirty:
            if (isinstance(item, OntologyProject)
                    and item.current_release_id in new_release_ids):
                unsafe_pointer_flushes.append(item.current_release_id)

    event.listen(db, "before_flush", capture_release_pointer_order)
    try:
        promoted = client.post(
            f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
            headers=auth_headers,
            json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
        )
    finally:
        event.remove(db, "before_flush", capture_release_pointer_order)
    assert promoted.status_code == 201, promoted.text
    assert unsafe_pointer_flushes == []
    release = promoted.json()["data"]
    assert release["version_number"] == "v1"
    assert release["node_kind"] == "release"
    assert release["promoted_from_id"] == draft["id"]

    db.expire_all()
    project = db.query(OntologyProject).filter_by(id=oid).one()
    assert project.current_release_id == release["id"] and project.version == "v1"
    assert db.query(ObjectInstance).filter_by(ontology_id=oid).count() == 2
    assert {
        item.ontology_release_id
        for item in db.query(ObjectInstance).filter_by(ontology_id=oid).all()
    } == {release["id"]}
    assert db.query(PropertyFact).filter_by(ontology_id=oid).count() == 6
    assert db.query(PropertyFact).filter_by(
        ontology_id=oid, kind="object").count() == 2
    assert db.query(OntologyVersion).filter_by(id=root["id"]).one().snapshot_formal is not None
    assert db.query(OntologyVersion).filter_by(id=draft["id"]).one().lifecycle_status == "superseded"
    release_row = db.query(OntologyVersion).filter_by(id=release["id"]).one()
    frozen_mapping = release_row.snapshot_formal["mappings"][0]
    assert frozen_mapping["status"] == "applied"
    assert frozen_mapping["fieldMapping"][
        "__applied_dataset_version_id__"
    ] == run["dataset_versions"][0]["versionId"]
    db.refresh(dynamic)
    assert dynamic.enabled is False
    assert dynamic.bound_release_id == root["id"]
    assert dynamic.last_trial_release_id is None
    assert dynamic.last_trial_revision is None
    assert dynamic.last_trial_report is None
    assert db.query(SentinelMatchState).filter_by(
        sentinel_id=dynamic.id).count() == 0

    # 当前发布和历史发布同样允许保存布局；运行时 full 视图读取布局覆盖，
    # 发布快照的哈希和内容保持原样。映射画布的 object:/dataset: 位置同样可写。
    release_row = db.query(OntologyVersion).filter_by(id=release["id"]).one()
    release_hash = release_row.snapshot_hash
    moved_release = client.put(
        f"/api/v2/ontologies/{oid}/layout",
        headers=auth_headers,
        json={"positions": {
            "ot-order": {"x": 720, "y": 420},
            "object:ot-order": {"x": 500, "y": 260},
            "dataset:dataset-orders": {"x": 80, "y": 160},
        }},
    )
    assert moved_release.status_code == 200, moved_release.text
    assert moved_release.json()["data"]["versionId"] == release["id"]
    assert moved_release.json()["data"]["positions"]["object:ot-order"] == {
        "x": 500.0, "y": 260.0,
    }
    assert moved_release.json()["data"]["positions"]["dataset:dataset-orders"] == {
        "x": 80.0, "y": 160.0,
    }
    runtime = client.get(
        f"/api/v2/formal/ontologies/{oid}/full", headers=auth_headers,
    ).json()["data"]
    assert runtime["objectTypes"][0]["positionX"] == 720
    assert runtime["objectTypes"][0]["positionY"] == 420
    db.expire_all()
    release_row = db.query(OntologyVersion).filter_by(id=release["id"]).one()
    assert release_row.snapshot_hash == release_hash
    assert release_row.snapshot_formal["objectTypes"][0]["positionX"] == 10
    assert release_row.snapshot_formal["objectTypes"][0]["positionY"] == 20

    # 同一完整结构和映射可以继续从 v1 分支、试跑并晋级为 v2；复用稳定
    # 定义 ID 时不能和当前生产投影冲突，也不能重建对象身份。
    second_draft = _draft(client, auth_headers, oid, release["id"])
    assert second_draft["version_number"] == "v1.1"
    second_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{second_draft['id']}/trial-runs",
        headers=auth_headers, json={}).json()["data"]
    assert second_run["status"] == "passed", second_run
    second_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{second_draft['id']}/impact",
        headers=auth_headers).json()["data"]
    second_promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{second_draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": second_run["id"],
              "impactHash": second_impact["impactHash"]},
    )
    assert second_promoted.status_code == 201, second_promoted.text
    release_v2 = second_promoted.json()["data"]
    assert release_v2["version_number"] == "v2"
    assert release_v2["parent_version_id"] == release["id"]
    db.expire_all()
    project = db.query(OntologyProject).filter_by(id=oid).one()
    assert project.current_release_id == release_v2["id"]
    assert db.query(ObjectInstance).filter_by(ontology_id=oid).count() == 2
    assert {
        item.ontology_release_id
        for item in db.query(ObjectInstance).filter_by(ontology_id=oid).all()
    } == {release_v2["id"]}
    assert db.query(PropertyFact).filter_by(ontology_id=oid).count() == 6


def test_runtime_action_state_conflict_is_previewed_and_promotion_writes_nothing(
        client, auth_headers, ontology, db, monkeypatch):
    """A lake candidate must never silently erase the current action result."""
    from app.ontologies.formal_modeling.facts import record_property_facts

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    run_response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()["data"]

    current_object = next(
        item for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid, ontology_release_id=current["id"]).all()
        if (item.properties or {}).get("id") == "O-1"
    )
    old_props = dict(current_object.properties or {})
    runtime_props = {**old_props, "name": "动作确认的风险订单"}
    action_facts = record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props=old_props,
        new_props=runtime_props,
        source="action://mark-risk",
        caused_by="action-log-runtime-conflict",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    current_object.properties = runtime_props
    db.commit()
    assert len(action_facts) == 1

    impact_response = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    )
    assert impact_response.status_code == 200, impact_response.text
    impact = impact_response.json()["data"]
    readiness = impact["releaseReadiness"]
    assert readiness["ready"] is False
    assert readiness["blockingCount"] == 1
    assert readiness["errors"][0]["code"] == "runtime_state_conflict"
    assert readiness["errors"][0]["conflictCount"] == 1
    assert readiness["repairStrategy"] is None
    conflicts = readiness["runtimeStateConflicts"]
    assert conflicts == {
        "totalCount": 1,
        "propertyConflictCount": 1,
        "objectConflictCount": 0,
        "linkConflictCount": 0,
        "itemLimit": 50,
        "truncated": False,
        "items": [{
            "resourceKind": "objectProperty",
            "objectId": current_object.id,
            "objectTypeId": "ot-order",
            "property": "name",
            "current": "动作确认的风险订单",
            "currentPresent": True,
            "candidate": "一号订单",
            "candidatePresent": True,
            "candidateObjectPresent": True,
            "source": "action://mark-risk",
            "factId": action_facts[0].id,
        }],
    }

    before = {
        "release": db.query(OntologyProject).filter_by(id=oid).one().current_release_id,
        "versions": db.query(OntologyVersion).filter_by(ontology_id=oid).count(),
        "facts": db.query(PropertyFact).filter_by(ontology_id=oid).count(),
        "objects": db.query(ObjectInstance).filter_by(ontology_id=oid).count(),
        "audit": db.query(AuditLog).filter_by(ontology_id=oid).count(),
    }
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 409, promoted.text
    detail = promoted.json()["detail"]
    assert detail["code"] == "runtime_state_conflict"
    assert detail["runtimeStateConflicts"] == conflicts
    assert "动作确认的风险订单" not in detail["message"]

    db.expire_all()
    assert {
        "release": db.query(OntologyProject).filter_by(id=oid).one().current_release_id,
        "versions": db.query(OntologyVersion).filter_by(ontology_id=oid).count(),
        "facts": db.query(PropertyFact).filter_by(ontology_id=oid).count(),
        "objects": db.query(ObjectInstance).filter_by(ontology_id=oid).count(),
        "audit": db.query(AuditLog).filter_by(ontology_id=oid).count(),
    } == before
    persisted = db.query(ObjectInstance).filter_by(id=current_object.id).one()
    assert persisted.properties["name"] == "动作确认的风险订单"
    assert db.query(OntologyVersion).filter_by(
        id=draft["id"]).one().lifecycle_status == "trial_ready"


def test_runtime_action_committed_after_impact_preview_is_rechecked_on_promote(
        client, auth_headers, ontology, db, monkeypatch):
    """The impact response is advisory; promote repeats the gate under locks."""
    from app.ontologies.formal_modeling.facts import record_property_facts

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    preview = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert preview["releaseReadiness"]["ready"] is True
    assert preview["releaseReadiness"][
        "runtimeStateConflicts"]["totalCount"] == 0

    current_object = next(
        item for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid, ontology_release_id=current["id"]).all()
        if (item.properties or {}).get("id") == "O-2"
    )
    old_props = dict(current_object.properties or {})
    runtime_props = {**old_props, "name": "试跑后动作写入"}
    record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props=old_props,
        new_props=runtime_props,
        source="action://after-trial",
        caused_by="action-log-after-trial",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    current_object.properties = runtime_props
    db.commit()
    before_fact_count = db.query(PropertyFact).filter_by(
        ontology_id=oid).count()

    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": preview["impactHash"]},
    )
    assert promoted.status_code == 409, promoted.text
    assert promoted.json()["detail"]["code"] == "runtime_state_conflict"
    conflict = promoted.json()["detail"]["runtimeStateConflicts"]["items"][0]
    assert conflict["objectId"] == current_object.id
    assert conflict["current"] == "试跑后动作写入"
    assert conflict["candidate"] == "二号订单"
    db.expire_all()
    assert db.query(OntologyProject).filter_by(
        id=oid).one().current_release_id == current["id"]
    assert db.query(PropertyFact).filter_by(
        ontology_id=oid).count() == before_fact_count
    assert db.query(ObjectInstance).filter_by(
        id=current_object.id).one().properties["name"] == "试跑后动作写入"


def test_runtime_link_conflicts_cover_create_delete_and_same_id_drift(
        client, auth_headers, ontology, db, monkeypatch):
    """Runtime link existence and identity cannot be erased or resurrected."""
    from app.ontologies.formal_modeling.facts import record_link_fact

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    objects = sorted(
        db.query(ObjectInstance).filter_by(
            ontology_id=oid, ontology_release_id=current["id"]).all(),
        key=lambda item: item.id,
    )
    source_id, target_id = objects[0].id, objects[1].id

    created_link = LinkInstance(
        id="runtime-action-created-link",
        ontology_id=oid,
        ontology_release_id=current["id"],
        link_type_id="lt-runtime-created",
        source_object_id=source_id,
        target_object_id=target_id,
        properties={"label": "动作创建"},
    )
    drifted_link = LinkInstance(
        id="runtime-action-drifted-link",
        ontology_id=oid,
        ontology_release_id=current["id"],
        link_type_id="lt-runtime-current",
        source_object_id=source_id,
        target_object_id=target_id,
        properties={
            "label": "当前动作关系",
            "api_key": "current-link-secret",
        },
    )
    unknown_link = LinkInstance(
        id="runtime-unknown-link",
        ontology_id=oid,
        ontology_release_id=current["id"],
        link_type_id="lt-runtime-unknown",
        source_object_id=source_id,
        target_object_id=target_id,
        properties={"label": "无事实来源关系"},
    )
    db.add_all([created_link, drifted_link, unknown_link])
    db.flush()
    created_fact = record_link_fact(
        db,
        ontology_id=oid,
        link_instance_id=created_link.id,
        link_type_id=created_link.link_type_id,
        exists=True,
        source="action://create-link",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    drifted_fact = record_link_fact(
        db,
        ontology_id=oid,
        link_instance_id=drifted_link.id,
        link_type_id=drifted_link.link_type_id,
        exists=True,
        source="manual",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    deleted_fact = record_link_fact(
        db,
        ontology_id=oid,
        link_instance_id="runtime-action-deleted-link",
        link_type_id="lt-runtime-deleted",
        exists=False,
        source="action://delete-link",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    record_link_fact(
        db,
        ontology_id=oid,
        link_instance_id="lake-mapping-deleted-link",
        link_type_id="lt-lake-mapping",
        exists=False,
        source="link-mapping://lm-orders",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    db.add_all([
        OntologyTrialLink(
            trial_run_id=run["id"],
            link_id="runtime-action-deleted-link",
            link_type_id="lt-runtime-deleted",
            source_object_id=source_id,
            target_object_id=target_id,
            properties={"label": "试跑将重新创建"},
        ),
        OntologyTrialLink(
            trial_run_id=run["id"],
            link_id=drifted_link.id,
            link_type_id="lt-runtime-candidate",
            source_object_id=target_id,
            target_object_id=source_id,
            properties={
                "label": "候选关系",
                "api_key": "candidate-link-secret",
            },
        ),
        OntologyTrialLink(
            trial_run_id=run["id"],
            link_id="lake-mapping-deleted-link",
            link_type_id="lt-lake-mapping",
            source_object_id=source_id,
            target_object_id=target_id,
            properties={"label": "正规湖映射恢复"},
        ),
    ])
    run_row = db.query(OntologyTrialRun).filter_by(id=run["id"]).one()
    result = copy.deepcopy(run_row.result_json or {})
    result["counts"] = {**dict(result.get("counts") or {}), "links": 3}
    run_row.result_json = result
    db.commit()

    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    readiness = impact["releaseReadiness"]
    assert readiness["ready"] is False
    report = readiness["runtimeStateConflicts"]
    assert report["totalCount"] == 4
    assert report["propertyConflictCount"] == 0
    assert report["linkConflictCount"] == 4
    items = {item["linkId"]: item for item in report["items"]}
    assert set(items) == {
        created_link.id,
        "runtime-action-deleted-link",
        drifted_link.id,
        unknown_link.id,
    }
    assert "lake-mapping-deleted-link" not in items
    assert items[created_link.id]["current"]["exists"] is True
    assert items[created_link.id]["candidate"] == {"exists": False}
    assert items[created_link.id]["source"] == "action://create-link"
    assert items[created_link.id]["factId"] == created_fact.id
    assert items["runtime-action-deleted-link"]["current"] == {
        "exists": False,
    }
    assert items["runtime-action-deleted-link"]["candidate"][
        "exists"] is True
    assert items["runtime-action-deleted-link"][
        "source"] == "action://delete-link"
    assert items["runtime-action-deleted-link"]["factId"] == deleted_fact.id
    drift = items[drifted_link.id]
    assert drift["current"]["linkTypeId"] == "lt-runtime-current"
    assert drift["candidate"]["linkTypeId"] == "lt-runtime-candidate"
    assert drift["current"]["sourceObjectId"] == source_id
    assert drift["candidate"]["sourceObjectId"] == target_id
    assert drift["current"]["properties"]["api_key"] == "••••••（已隐藏）"
    assert drift["candidate"]["properties"]["api_key"] == "••••••（已隐藏）"
    assert "current-link-secret" not in str(report)
    assert "candidate-link-secret" not in str(report)
    assert drift["factId"] == drifted_fact.id
    assert items[unknown_link.id]["source"] == "unknown"
    assert items[unknown_link.id]["factId"] is None

    before = {
        "release": db.query(OntologyProject).filter_by(id=oid).one().current_release_id,
        "facts": db.query(PropertyFact).filter_by(ontology_id=oid).count(),
        "links": db.query(LinkInstance).filter_by(ontology_id=oid).count(),
        "versions": db.query(OntologyVersion).filter_by(ontology_id=oid).count(),
    }
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 409, promoted.text
    assert promoted.json()["detail"]["code"] == "runtime_state_conflict"
    assert promoted.json()["detail"][
        "runtimeStateConflicts"]["linkConflictCount"] == 4
    assert "current-link-secret" not in promoted.text
    assert "candidate-link-secret" not in promoted.text
    db.expire_all()
    assert {
        "release": db.query(OntologyProject).filter_by(id=oid).one().current_release_id,
        "facts": db.query(PropertyFact).filter_by(ontology_id=oid).count(),
        "links": db.query(LinkInstance).filter_by(ontology_id=oid).count(),
        "versions": db.query(OntologyVersion).filter_by(ontology_id=oid).count(),
    } == before


def test_normal_promotion_adopts_stable_link_without_relation_lineage(
        client, auth_headers, ontology, db, monkeypatch):
    """An explicit full promotion resets even a legacy link without Relation."""
    from app.ontologies.formal_modeling.facts import record_link_fact

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    release_v1 = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    stable_link_type_id = "lt-legacy-stable"
    release_row = db.query(OntologyVersion).filter_by(
        id=release_v1["id"]).one()
    release_snapshot = copy.deepcopy(release_row.snapshot_formal or {})
    release_snapshot["linkTypes"] = [{
        "id": stable_link_type_id,
        "name": "legacy_stable",
        "displayName": "无 Relation 血缘稳定关系",
        "sourceObjectTypeId": "ot-order",
        "targetObjectTypeId": "ot-order",
        "cardinality": "many-to-many",
        "properties": [],
    }]
    release_row.snapshot_formal = release_snapshot
    release_row.snapshot_hash = snapshot_hash(release_snapshot)
    _attach_semantic_layer(db, release_v1["id"])
    db.add(LinkType(
        id=stable_link_type_id,
        ontology_id=oid,
        name="legacy_stable",
        display_name="无 Relation 血缘稳定关系",
        source_object_type_id="ot-order",
        target_object_type_id="ot-order",
        cardinality="many-to-many",
        properties=[],
    ))
    original_release_mapping_contract = (
        version_router.validate_release_mapping_contract)
    monkeypatch.setattr(
        version_router,
        "validate_release_mapping_contract",
        lambda snapshot: [
            error
            for error in original_release_mapping_contract(snapshot)
            if error.get("code") != "link_type_mapping_required"
        ],
    )
    db.commit()
    objects = sorted(
        db.query(ObjectInstance).filter_by(
            ontology_id=oid,
            ontology_release_id=release_v1["id"],
        ).all(),
        key=lambda item: item.id,
    )
    stable_link_id = "stable-link-without-relation"
    source_object_id = objects[0].id
    target_object_id = objects[1].id
    stable_link = LinkInstance(
        id=stable_link_id,
        ontology_id=oid,
        ontology_release_id=release_v1["id"],
        link_type_id=stable_link_type_id,
        source_object_id=source_object_id,
        target_object_id=target_object_id,
        properties={"state": "adopted"},
        source_relation_id=None,
    )
    db.add(stable_link)
    action_fact = record_link_fact(
        db,
        ontology_id=oid,
        link_instance_id=stable_link_id,
        link_type_id=stable_link_type_id,
        exists=True,
        source="action://create-stable-link",
        ontology_version=release_v1["version_number"],
        ontology_release_id=release_v1["id"],
    )
    db.commit()

    adopting_draft = _draft(client, auth_headers, oid, release_v1["id"])
    adopting_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{adopting_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    db.add(OntologyTrialLink(
        trial_run_id=adopting_run["id"],
        link_id=stable_link_id,
        link_type_id=stable_link_type_id,
        source_object_id=source_object_id,
        target_object_id=target_object_id,
        properties={"state": "adopted"},
        source_relation_id=None,
    ))
    run_row = db.query(OntologyTrialRun).filter_by(
        id=adopting_run["id"]).one()
    result = copy.deepcopy(run_row.result_json or {})
    result["counts"] = {
        **dict(result.get("counts") or {}),
        "links": 1,
    }
    run_row.result_json = result
    db.commit()

    adopting_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{adopting_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert adopting_impact["releaseReadiness"]["ready"] is True
    promoted_response = client.post(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{adopting_draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": adopting_run["id"],
            "impactHash": adopting_impact["impactHash"],
        },
    )
    assert promoted_response.status_code == 201, promoted_response.text
    release_v2 = promoted_response.json()["data"]
    db.expire_all()
    promoted_link = db.query(LinkInstance).filter_by(
        id=stable_link_id,
        ontology_release_id=release_v2["id"],
    ).one()
    assert promoted_link.source_relation_id is None
    assert db.query(PropertyFact).filter_by(
        ontology_id=oid,
        ontology_release_id=release_v2["id"],
        instance_id=stable_link_id,
        kind="link",
    ).count() == 0
    assert action_fact is not None

    # The next candidate may change/delete the now-explicit baseline.  The
    # ancestor action source no longer blocks it, and lack of Relation lineage
    # is relevant only to rollback's implicit inheritance branch.
    next_draft = _draft(client, auth_headers, oid, release_v2["id"])
    next_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{next_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert next_run.status_code == 201, next_run.text
    next_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{next_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert next_impact["releaseReadiness"]["ready"] is True
    assert next_impact["releaseReadiness"][
        "runtimeStateConflicts"]["linkConflictCount"] == 0

    rollback_response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{release_v1['id']}/rollback",
        headers=auth_headers,
    )
    assert rollback_response.status_code == 200, rollback_response.text
    activation = rollback_response.json()["data"]
    db.expire_all()
    rollback_link = db.query(LinkInstance).filter_by(
        id=stable_link_id,
        ontology_release_id=activation["id"],
    ).one()
    assert rollback_link.source_relation_id is None
    rollback_draft = _draft(client, auth_headers, oid, activation["id"])
    rollback_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{rollback_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert rollback_run.status_code == 201, rollback_run.text
    rollback_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{rollback_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    rollback_report = rollback_impact["releaseReadiness"][
        "runtimeStateConflicts"]
    assert rollback_report["linkConflictCount"] == 1
    rollback_conflict = next(
        item for item in rollback_report["items"]
        if item["resourceKind"] == "link"
    )
    assert rollback_conflict["linkId"] == stable_link_id
    assert rollback_conflict["source"] == "unknown"
    assert rollback_conflict["factId"] is None


def test_runtime_object_tombstone_blocks_candidate_revival(
        client, auth_headers, ontology, db, monkeypatch):
    from app.ontologies.formal_modeling.facts import record_object_tombstone

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    deleted = next(
        item for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid, ontology_release_id=current["id"]).all()
        if (item.properties or {}).get("id") == "O-1"
    )
    tombstone = record_object_tombstone(
        db,
        ontology_id=oid,
        instance_id=deleted.id,
        object_type_id=deleted.object_type_id,
        source="manual",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    db.delete(deleted)
    db.commit()

    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    report = impact["releaseReadiness"]["runtimeStateConflicts"]
    assert report["objectConflictCount"] == 1
    conflict = next(
        item for item in report["items"]
        if item["resourceKind"] == "object"
    )
    assert conflict["objectId"] == deleted.id
    assert conflict["current"] == {"exists": False}
    assert conflict["candidate"]["exists"] is True
    assert conflict["source"] == "manual"
    assert conflict["factId"] == tombstone.id
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 409, promoted.text
    assert promoted.json()["detail"]["code"] == "runtime_state_conflict"


def test_lake_mapping_tombstone_allows_candidate_object_revival(
        client, auth_headers, ontology, db, monkeypatch):
    from app.ontologies.formal_modeling.facts import record_object_tombstone

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    deleted = next(
        item for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid, ontology_release_id=current["id"]).all()
        if (item.properties or {}).get("id") == "O-1"
    )
    record_object_tombstone(
        db,
        ontology_id=oid,
        instance_id=deleted.id,
        object_type_id=deleted.object_type_id,
        source="mapping://mapping-order",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    db.delete(deleted)
    db.commit()

    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert impact["releaseReadiness"]["ready"] is True
    assert impact["releaseReadiness"][
        "runtimeStateConflicts"]["objectConflictCount"] == 0
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 201, promoted.text
    db.expire_all()
    assert db.query(ObjectInstance).filter_by(
        ontology_id=oid,
        ontology_release_id=promoted.json()["data"]["id"],
    ).count() == 2


def test_property_removal_fact_guards_revival_without_blocking_new_or_lake_fields(
        client, auth_headers, ontology, db, monkeypatch):
    from app.ontologies.formal_modeling.facts import record_property_facts

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    current_object = next(
        item for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid, ontology_release_id=current["id"]).all()
        if (item.properties or {}).get("id") == "O-1"
    )
    trial_object = db.query(OntologyTrialObject).filter_by(
        trial_run_id=run["id"], object_id=current_object.id,
    ).one()
    trial_object.properties = {
        **dict(trial_object.properties or {}),
        "action_removed": "候选重加",
        "lake_removed": "湖端重加",
        "legacy_ambiguous": "旧 null 事实不能证明删除",
        "genuinely_new": "正常新增",
    }
    baseline = dict(current_object.properties or {})
    record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props=baseline,
        new_props={**baseline, "action_removed": "动作临时值"},
        source="action://temporary",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    action_removal = record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props={**baseline, "action_removed": "动作临时值"},
        new_props=baseline,
        source="action://remove?api_key=raw-source-secret",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props=baseline,
        new_props={**baseline, "lake_removed": None},
        source="mapping://mapping-order",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    lake_removal = record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props={**baseline, "lake_removed": None},
        new_props=baseline,
        source="mapping://mapping-order",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    db.add(PropertyFact(
        ontology_id=oid,
        ontology_release_id=current["id"],
        ontology_version=current["version_number"],
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        property_name="legacy_ambiguous",
        value={"v": None},
        kind="property",
        source="mapping://legacy-null",
        seq=1,
    ))
    db.commit()

    assert len(action_removal) == 1
    assert action_removal[0].value == {"v": None, "present": False}
    assert len(lake_removal) == 1
    assert lake_removal[0].value == {"v": None, "present": False}
    impact_response = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    )
    assert impact_response.status_code == 200, impact_response.text
    assert "raw-source-secret" not in impact_response.text
    report = impact_response.json()["data"]["releaseReadiness"][
        "runtimeStateConflicts"]
    property_conflicts = [
        item for item in report["items"]
        if item["resourceKind"] == "objectProperty"
    ]
    assert [item["property"] for item in property_conflicts] == [
        "action_removed",
        "legacy_ambiguous",
    ]
    conflict = property_conflicts[0]
    assert conflict["current"] is None
    assert conflict["currentPresent"] is False
    assert conflict["candidate"] == "候选重加"
    assert conflict["candidatePresent"] is True
    assert conflict["source"] == "action://remove?api_key=[凭据已隐藏]"
    assert conflict["factId"] == action_removal[0].id
    legacy_conflict = property_conflicts[1]
    assert legacy_conflict["currentPresent"] is False
    assert legacy_conflict["source"] == "unknown"
    assert legacy_conflict["factId"] is None


def test_object_existence_fact_chain_survives_lake_reintroduction(db, ontology):
    from app.ontologies.formal_modeling.facts import (
        record_object_presence,
        record_object_tombstone,
    )

    oid = ontology["id"]
    object_id = "lake-object-reintroduced"
    created = record_object_presence(
        db, ontology_id=oid, instance_id=object_id,
        object_type_id="ot-order", source="mapping://mapping-order")
    deleted = record_object_tombstone(
        db, ontology_id=oid, instance_id=object_id,
        object_type_id="ot-order", source="mapping://mapping-order")
    reintroduced = record_object_presence(
        db, ontology_id=oid, instance_id=object_id,
        object_type_id="ot-order", source="mapping://mapping-order")
    deleted_again = record_object_tombstone(
        db, ontology_id=oid, instance_id=object_id,
        object_type_id="ot-order", source="mapping://mapping-order")
    db.commit()

    assert [fact.value for fact in (
        created, deleted, reintroduced, deleted_again,
    )] == [
        {"v": True}, {"v": False}, {"v": True}, {"v": False},
    ]
    assert [fact.seq for fact in (
        created, deleted, reintroduced, deleted_again,
    )] == [1, 2, 3, 4]
    assert deleted.supersedes_id == created.id
    assert reintroduced.supersedes_id == deleted.id
    assert deleted_again.supersedes_id == reintroduced.id


def test_promotion_does_not_backfill_presence_for_legacy_existing_objects(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    release_v1 = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    # Simulate an installation upgraded from before object-presence facts.
    db.query(PropertyFact).filter_by(
        ontology_id=oid, kind="object",
    ).delete(synchronize_session=False)
    db.commit()

    draft = _draft(client, auth_headers, oid, release_v1["id"])
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 201, promoted.text
    assert db.query(PropertyFact).filter_by(
        ontology_id=oid, kind="object",
    ).count() == 0


def test_zero_property_runtime_object_cannot_be_silently_deleted(
        client, auth_headers, ontology, db, monkeypatch):
    from app.ontologies.formal_modeling.facts import record_object_presence

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    runtime_object = ObjectInstance(
        id="runtime-empty-object",
        ontology_id=oid,
        ontology_release_id=current["id"],
        object_type_id="ot-order",
        properties={},
        computed={},
        source="action",
    )
    db.add(runtime_object)
    presence = record_object_presence(
        db,
        ontology_id=oid,
        instance_id=runtime_object.id,
        object_type_id=runtime_object.object_type_id,
        source="action://create-empty",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    db.commit()

    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    report = impact["releaseReadiness"]["runtimeStateConflicts"]
    object_conflict = next(
        item for item in report["items"]
        if item["resourceKind"] == "object"
        and item["objectId"] == runtime_object.id
    )
    assert object_conflict["current"]["exists"] is True
    assert object_conflict["candidate"] == {"exists": False}
    assert object_conflict["source"] == "action://create-empty"
    assert object_conflict["factId"] == presence.id


def test_runtime_fact_query_does_not_expand_coordinate_cross_product(
        db, ontology):
    oid = ontology["id"]
    release_id = db.query(OntologyProject).filter_by(id=oid).one().current_release_id
    recorded_at = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    facts = [
        PropertyFact(
            id="fact-requested-coordinate-old",
            ontology_id=oid,
            ontology_release_id=release_id,
            instance_id="object-A",
            object_type_id="ot-order",
            property_name="field-x",
            value={"v": "requested", "present": True},
            kind="property",
            source="pipeline",
            seq=1,
            recorded_at=recorded_at,
        ),
        PropertyFact(
            id="fact-requested-coordinate-a",
            ontology_id=oid,
            ontology_release_id=release_id,
            instance_id="object-A",
            object_type_id="ot-order",
            property_name="field-x",
            value={"v": "same-time-lower-id", "present": True},
            kind="property",
            source="pipeline",
            seq=2,
            recorded_at=recorded_at,
        ),
        PropertyFact(
            id="fact-requested-coordinate-z",
            ontology_id=oid,
            ontology_release_id=release_id,
            instance_id="object-A",
            object_type_id="ot-order",
            property_name="field-x",
            value={"v": "canonical-latest", "present": True},
            kind="property",
            source="pipeline",
            seq=2,
            recorded_at=recorded_at,
        ),
        PropertyFact(
            id="fact-cross-coordinate-a",
            ontology_id=oid,
            ontology_release_id=release_id,
            instance_id="object-A",
            object_type_id="ot-order",
            property_name="field-y",
            value={"v": "must-not-load", "present": True},
            kind="property",
            source="pipeline",
            seq=1,
            recorded_at=recorded_at,
        ),
        PropertyFact(
            id="fact-cross-coordinate-b",
            ontology_id=oid,
            ontology_release_id=release_id,
            instance_id="object-B",
            object_type_id="ot-order",
            property_name="field-x",
            value={"v": "must-not-load", "present": True},
            kind="property",
            source="pipeline",
            seq=1,
            recorded_at=recorded_at,
        ),
        PropertyFact(
            id="fact-object-exists-old",
            ontology_id=oid,
            ontology_release_id=release_id,
            instance_id="object-exists-window",
            object_type_id="ot-order",
            property_name="exists",
            value={"v": False},
            kind="object",
            source="pipeline",
            seq=1,
            recorded_at=recorded_at,
        ),
        PropertyFact(
            id="fact-object-exists-z",
            ontology_id=oid,
            ontology_release_id=release_id,
            instance_id="object-exists-window",
            object_type_id="ot-order",
            property_name="exists",
            value={"v": True},
            kind="object",
            source="pipeline",
            seq=2,
            recorded_at=recorded_at,
        ),
    ]
    db.add_all(facts)
    db.commit()

    selected = version_router._runtime_coordinate_facts(
        db,
        ontology_id=oid,
        release_ids=[release_id],
        kind="property",
        coordinates=[("object-A", "field-x"), ("object-B", "field-y")],
    )
    assert [fact.id for fact in selected] == [
        "fact-requested-coordinate-z",
    ]
    existence = version_router._runtime_existence_facts(
        db,
        ontology_id=oid,
        release_ids=[release_id],
        kind="object",
        instance_ids=["object-exists-window"],
    )
    assert [fact.id for fact in existence] == ["fact-object-exists-z"]


def test_runtime_lake_source_allowlist_is_explicit():
    assert version_router._is_lake_projection_fact_source("pipeline")
    assert version_router._is_lake_projection_fact_source(
        "pipeline-reconcile")
    assert version_router._is_lake_projection_fact_source(
        "pipeline://historical")
    assert version_router._is_lake_projection_fact_source(
        "ontology-release://release-id")
    assert version_router._is_lake_projection_fact_source(
        "mapping://mapping-id")
    assert version_router._is_lake_projection_fact_source(
        "link-mapping://mapping-id")
    assert not version_router._is_lake_projection_fact_source(
        "pipeline-custom")


def test_runtime_property_conflict_values_are_redacted_by_field_name(
        client, auth_headers, ontology, db, monkeypatch):
    from app.ontologies.formal_modeling.facts import record_property_facts

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    current_object = db.query(ObjectInstance).filter_by(
        ontology_id=oid, ontology_release_id=current["id"]).first()
    old_props = dict(current_object.properties or {})
    runtime_props = {
        **old_props,
        "password": "runtime-password-plain",
        "api_key": "runtime-api-key-plain",
        "profile": {
            "credential": "nested-credential-plain",
            "note": "token=inline-secret-plain",
        },
    }
    record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props=old_props,
        new_props=runtime_props,
        source="manual",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    current_object.properties = {
        **runtime_props,
        "legacy_note": "无事实来源但仍须阻断",
    }
    current_object.source = "manual"
    db.commit()

    response = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    serialized = response.text
    assert "runtime-password-plain" not in serialized
    assert "runtime-api-key-plain" not in serialized
    assert "nested-credential-plain" not in serialized
    assert "inline-secret-plain" not in serialized
    conflicts = response.json()["data"]["releaseReadiness"][
        "runtimeStateConflicts"]["items"]
    by_property = {
        item["property"]: item for item in conflicts
        if item["resourceKind"] == "objectProperty"
    }
    assert by_property["password"]["current"] == "••••••（已隐藏）"
    assert by_property["api_key"]["current"] == "••••••（已隐藏）"
    assert by_property["profile"]["current"]["credential"] == "••••••（已隐藏）"
    assert by_property["profile"]["current"]["note"] == "token=[凭据已隐藏]"
    assert by_property["legacy_note"]["source"] == "unknown"
    assert by_property["legacy_note"]["factId"] is None


def test_collector_fetches_before_runtime_write_lock_and_revalidates_inside(
        client, auth_headers, ontology, db, monkeypatch):
    from app.data_channel.connections import collectors_router

    oid = ontology["id"]
    db.add(ObjectType(
        id="ot-collector-lock",
        ontology_id=oid,
        name="CollectorLock",
        display_name="采集锁验证",
        properties=[],
        interfaces=[],
    ))
    db.commit()
    events: list[str] = []
    held = {"value": False}

    def fetch_items(**_kwargs):
        assert held["value"] is False
        events.append("fetch")
        return {"items": []}

    @contextmanager
    def runtime_lock(_db, locked_ontology_id):
        assert locked_ontology_id == oid
        assert events == ["fetch"]
        held["value"] = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            held["value"] = False

    monkeypatch.setattr(collectors_router.aihot, "fetch_items", fetch_items)
    monkeypatch.setattr(
        collectors_router, "_ontology_build_lock", runtime_lock)
    response = client.post(
        f"/api/v2/collectors/aihot/collect/{oid}",
        headers=auth_headers,
        json={
            "object_type_id": "ot-collector-lock",
            "mode": "selected",
            "take": 1,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["collected"] == 0
    assert events == ["fetch", "lock-enter", "lock-exit"]


def test_collector_write_requires_ontology_owner_or_admin(
        client, ontology, db, editor_user, monkeypatch):
    """Authentication alone must not authorize writes into another ontology."""
    from app.data_channel.connections import collectors_router

    oid = ontology["id"]
    editor_login = client.post(
        "/api/v1/auth/login",
        json={"username": editor_user.username, "password": "editor123"},
    )
    assert editor_login.status_code == 200, editor_login.text
    editor_headers = {
        "Authorization": (
            f"Bearer {editor_login.json()['data']['access_token']}"
        ),
    }
    fetch_calls: list[str] = []

    def fetch_items(**_kwargs):
        fetch_calls.append("fetch")
        return {"items": []}

    monkeypatch.setattr(collectors_router.aihot, "fetch_items", fetch_items)
    payload = {
        "object_type_id": "ot-collector-owner",
        "mode": "selected",
        "take": 1,
    }

    denied = client.post(
        f"/api/v2/collectors/aihot/collect/{oid}",
        headers=editor_headers,
        json=payload,
    )
    assert denied.status_code == 403, denied.text
    assert fetch_calls == []

    project = db.query(OntologyProject).filter_by(id=oid).one()
    project.created_by = editor_user.id
    db.add(ObjectType(
        id="ot-collector-owner",
        ontology_id=oid,
        name="CollectorOwner",
        display_name="采集权限验证",
        properties=[],
        interfaces=[],
    ))
    db.commit()

    allowed = client.post(
        f"/api/v2/collectors/aihot/collect/{oid}",
        headers=editor_headers,
        json=payload,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["data"]["collected"] == 0
    assert fetch_calls == ["fetch"]


def test_pipeline_fact_difference_does_not_block_promotion(
        client, auth_headers, ontology, db, monkeypatch):
    """Pure lake/projection drift stays a normal release replacement."""
    from app.ontologies.formal_modeling.facts import record_property_facts

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    current = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    draft = _draft(client, auth_headers, oid, current["id"])
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]

    current_object = next(
        item for item in db.query(ObjectInstance).filter_by(
            ontology_id=oid, ontology_release_id=current["id"]).all()
        if (item.properties or {}).get("id") == "O-1"
    )
    old_props = dict(current_object.properties or {})
    action_props = {**old_props, "name": "已被后续流水线取代的动作值"}
    record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props=old_props,
        new_props=action_props,
        source="action://superseded-before-pipeline",
        caused_by="action-log-superseded",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    current_object.properties = action_props
    pipeline_props = {**old_props, "name": "流水线刷新中的值"}
    record_property_facts(
        db,
        ontology_id=oid,
        instance_id=current_object.id,
        object_type_id=current_object.object_type_id,
        old_props=action_props,
        new_props=pipeline_props,
        source="pipeline",
        ontology_version=current["version_number"],
        ontology_release_id=current["id"],
    )
    current_object.properties = pipeline_props
    db.commit()

    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert impact["releaseReadiness"]["ready"] is True
    assert impact["releaseReadiness"][
        "runtimeStateConflicts"]["totalCount"] == 0
    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 201, promoted.text
    db.expire_all()
    assert db.query(OntologyProject).filter_by(
        id=oid).one().current_release_id == promoted.json()["data"]["id"]
    assert db.query(ObjectInstance).filter_by(
        id=current_object.id).one().properties["name"] == "一号订单"


def test_inherited_lake_facts_allow_change_and_delete_after_noop_release(
        client, auth_headers, ontology, db, monkeypatch):
    """Unchanged properties inherit authoritative lake provenance by ancestry."""
    service, _ = _dataset(db, monkeypatch)
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    release_v1 = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)

    noop_draft = _draft(client, auth_headers, oid, release_v1["id"])
    noop_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{noop_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    noop_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{noop_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    noop_promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{noop_draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": noop_run["id"],
            "impactHash": noop_impact["impactHash"],
        },
    )
    assert noop_promoted.status_code == 201, noop_promoted.text
    release_v2 = noop_promoted.json()["data"]
    assert db.query(PropertyFact).filter_by(
        ontology_id=oid,
        ontology_release_id=release_v2["id"],
    ).count() == 0
    second_noop_draft = _draft(
        client, auth_headers, oid, release_v2["id"])
    second_noop_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{second_noop_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    second_noop_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{second_noop_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    second_noop_promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{second_noop_draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": second_noop_run["id"],
            "impactHash": second_noop_impact["impactHash"],
        },
    )
    assert second_noop_promoted.status_code == 201, (
        second_noop_promoted.text)
    release_v3 = second_noop_promoted.json()["data"]
    assert db.query(PropertyFact).filter_by(
        ontology_id=oid,
        ontology_release_id=release_v3["id"],
    ).count() == 0

    # A normal approved lake snapshot changes O-1 and removes O-2.  The latest
    # authoritative facts live before two consecutive no-op activations.
    service.create_version(
        "dataset-orders",
        _csv([{"id": "O-1", "name": "一号订单（湖端更新）"}]),
        rowcount=1,
    )
    changed_draft = _draft(client, auth_headers, oid, release_v3["id"])
    changed_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{changed_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert changed_run.status_code == 201, changed_run.text
    changed_run_data = changed_run.json()["data"]
    changed_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{changed_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    readiness = changed_impact["releaseReadiness"]
    assert readiness["ready"] is True
    assert readiness["runtimeStateConflicts"]["totalCount"] == 0
    changed_promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{changed_draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": changed_run_data["id"],
            "impactHash": changed_impact["impactHash"],
        },
    )
    assert changed_promoted.status_code == 201, changed_promoted.text
    db.expire_all()
    current_objects = db.query(ObjectInstance).filter_by(
        ontology_id=oid,
        ontology_release_id=changed_promoted.json()["data"]["id"],
    ).all()
    assert [item.properties for item in current_objects] == [{
        "id": "O-1",
        "name": "一号订单（湖端更新）",
    }]


def test_rollback_activation_inherits_post_baseline_action_update_and_delete(
        client, auth_headers, ontology, db, monkeypatch):
    from app.ontologies.formal_modeling.facts import (
        record_link_fact,
        record_object_tombstone,
        record_property_facts,
    )

    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    release_v1 = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    noop_draft = _draft(client, auth_headers, oid, release_v1["id"])
    noop_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{noop_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    noop_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{noop_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    release_v2 = client.post(
        f"/api/v2/ontologies/{oid}/versions/{noop_draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": noop_run["id"],
            "impactHash": noop_impact["impactHash"],
        },
    ).json()["data"]
    objects = db.query(ObjectInstance).filter_by(
        ontology_id=oid, ontology_release_id=release_v2["id"],
    ).all()
    changed = next(
        item for item in objects if (item.properties or {}).get("id") == "O-1")
    deleted = next(
        item for item in objects if (item.properties or {}).get("id") == "O-2")
    changed_baseline = dict(changed.properties or {})
    runtime_props = {
        **changed_baseline,
        "name": "回滚后仍必须保留的动作值",
    }
    action_fact = record_property_facts(
        db,
        ontology_id=oid,
        instance_id=changed.id,
        object_type_id=changed.object_type_id,
        old_props=changed_baseline,
        new_props=runtime_props,
        source="action://after-v2",
        ontology_version=release_v2["version_number"],
        ontology_release_id=release_v2["id"],
    )[0]
    changed.properties = runtime_props
    tombstone = record_object_tombstone(
        db,
        ontology_id=oid,
        instance_id=deleted.id,
        object_type_id=deleted.object_type_id,
        source="action://delete-after-v2",
        ontology_version=release_v2["version_number"],
        ontology_release_id=release_v2["id"],
    )
    link_tombstone = record_link_fact(
        db,
        ontology_id=oid,
        link_instance_id="link-deleted-after-v2",
        link_type_id="lt-runtime",
        exists=False,
        source="action://delete-link-after-v2",
        ontology_version=release_v2["version_number"],
        ontology_release_id=release_v2["id"],
    )
    db.delete(deleted)
    db.commit()

    rollback = client.post(
        f"/api/v2/ontologies/{oid}/versions/{release_v1['id']}/rollback",
        headers=auth_headers,
    )
    assert rollback.status_code == 200, rollback.text
    activation = rollback.json()["data"]
    recovery_draft = _draft(client, auth_headers, oid, activation["id"])
    recovery_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{recovery_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    db.add(OntologyTrialLink(
        trial_run_id=recovery_run["id"],
        link_id="link-deleted-after-v2",
        link_type_id="lt-runtime",
        source_object_id=changed.id,
        target_object_id=deleted.id,
        properties={},
    ))
    recovery_run_row = db.query(OntologyTrialRun).filter_by(
        id=recovery_run["id"]).one()
    recovery_result = copy.deepcopy(recovery_run_row.result_json or {})
    recovery_result["counts"] = {
        **dict(recovery_result.get("counts") or {}),
        "links": 1,
    }
    recovery_run_row.result_json = recovery_result
    db.commit()
    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{recovery_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    report = impact["releaseReadiness"]["runtimeStateConflicts"]
    assert report["propertyConflictCount"] == 1
    assert report["objectConflictCount"] == 1
    assert report["linkConflictCount"] == 1
    by_kind = {
        (item["resourceKind"], item.get("objectId")): item
        for item in report["items"]
    }
    property_conflict = by_kind[("objectProperty", changed.id)]
    assert property_conflict["source"] == "action://after-v2"
    assert property_conflict["factId"] == action_fact.id
    object_conflict = by_kind[("object", deleted.id)]
    assert object_conflict["source"] == "action://delete-after-v2"
    assert object_conflict["factId"] == tombstone.id
    link_conflict = next(
        item for item in report["items"]
        if item["resourceKind"] == "link"
    )
    assert link_conflict["source"] == "action://delete-link-after-v2"
    assert link_conflict["factId"] == link_tombstone.id


def test_promoted_baseline_prevents_old_action_provenance_crossing_rollback(
        client, auth_headers, ontology, db, monkeypatch):
    from app.ontologies.formal_modeling.facts import (
        record_link_fact,
        record_object_tombstone,
        record_property_facts,
    )

    oid = ontology["id"]
    service, _ = _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    release_v1 = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    objects = db.query(ObjectInstance).filter_by(
        ontology_id=oid, ontology_release_id=release_v1["id"],
    ).all()
    changed = next(
        item for item in objects if (item.properties or {}).get("id") == "O-1")
    deleted = next(
        item for item in objects if (item.properties or {}).get("id") == "O-2")
    baseline = dict(changed.properties or {})
    adopted = {**baseline, "name": "R2 明确采纳的旧动作值"}
    record_property_facts(
        db,
        ontology_id=oid,
        instance_id=changed.id,
        object_type_id=changed.object_type_id,
        old_props=baseline,
        new_props=adopted,
        source="action://before-r2-baseline",
        ontology_version=release_v1["version_number"],
        ontology_release_id=release_v1["id"],
    )
    changed.properties = adopted
    record_object_tombstone(
        db,
        ontology_id=oid,
        instance_id=deleted.id,
        object_type_id=deleted.object_type_id,
        source="action://delete-before-r2-baseline",
        ontology_version=release_v1["version_number"],
        ontology_release_id=release_v1["id"],
    )
    record_link_fact(
        db,
        ontology_id=oid,
        link_instance_id="link-deleted-before-r2-baseline",
        link_type_id="lt-runtime",
        exists=False,
        source="action://delete-link-before-r2-baseline",
        ontology_version=release_v1["version_number"],
        ontology_release_id=release_v1["id"],
    )
    db.delete(deleted)
    db.commit()

    # The isolated trial exactly adopts both runtime outcomes, making them the
    # new release baseline without appending duplicate facts on R2.
    service.create_version(
        "dataset-orders",
        _csv([{"id": "O-1", "name": adopted["name"]}]),
        rowcount=1,
    )
    adopting_draft = _draft(client, auth_headers, oid, release_v1["id"])
    adopting_run = client.post(
        f"/api/v2/ontologies/{oid}/versions/"
        f"{adopting_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]
    adopting_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{adopting_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert adopting_impact["releaseReadiness"]["ready"] is True
    release_v2_response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{adopting_draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": adopting_run["id"],
            "impactHash": adopting_impact["impactHash"],
        },
    )
    assert release_v2_response.status_code == 201, release_v2_response.text
    release_v2 = release_v2_response.json()["data"]
    assert db.query(PropertyFact).filter_by(
        ontology_id=oid,
        ontology_release_id=release_v2["id"],
    ).count() == 0

    rollback = client.post(
        f"/api/v2/ontologies/{oid}/versions/{release_v1['id']}/rollback",
        headers=auth_headers,
    )
    assert rollback.status_code == 200, rollback.text
    activation = rollback.json()["data"]
    service.create_version(
        "dataset-orders",
        _csv([
            {"id": "O-1", "name": "边界后的湖端新值"},
            {"id": "O-2", "name": "二号订单恢复"},
        ]),
        rowcount=2,
    )
    future_draft = _draft(client, auth_headers, oid, activation["id"])
    future_run_response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{future_draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert future_run_response.status_code == 201, future_run_response.text
    future_run = future_run_response.json()["data"]
    db.add(OntologyTrialLink(
        trial_run_id=future_run["id"],
        link_id="link-deleted-before-r2-baseline",
        link_type_id="lt-runtime",
        source_object_id=changed.id,
        target_object_id=deleted.id,
        properties={},
    ))
    future_run_row = db.query(OntologyTrialRun).filter_by(
        id=future_run["id"]).one()
    future_result = copy.deepcopy(future_run_row.result_json or {})
    future_result["counts"] = {
        **dict(future_result.get("counts") or {}),
        "links": 1,
    }
    future_run_row.result_json = future_result
    db.commit()
    future_impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{future_draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]
    assert future_impact["releaseReadiness"]["ready"] is True
    assert future_impact["releaseReadiness"][
        "runtimeStateConflicts"]["totalCount"] == 0


def test_production_promotion_observes_restored_mapping_before_runtime_gate(
        client, auth_headers, ontology, db, monkeypatch):
    """Autoflush-off sessions must still publish and pin candidate mappings."""
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, root["id"]),
        db,
    )
    draft_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    candidate = copy.deepcopy(draft_row.snapshot_formal)
    candidate["mappings"][0]["fieldMapping"][
        "__auto_apply_on_version__"
    ] = True
    draft_row.snapshot_formal = candidate
    draft_row.snapshot_hash = snapshot_hash(candidate)
    db.commit()

    monkeypatch.setattr(version_router.settings, "environment", "production")
    monkeypatch.setattr(
        version_router,
        "_rebuild_required_query_projections",
        lambda *_args, **_kwargs: {
            "ready": True,
            "neo4j": "ok",
        },
    )
    run_response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers,
        json={},
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()["data"]
    assert run["status"] == "passed", run
    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    ).json()["data"]

    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={
            "trialRunId": run["id"],
            "impactHash": impact["impactHash"],
        },
    )

    assert promoted.status_code == 201, promoted.text
    db.expire_all()
    mapping = db.query(OntologyMapping).filter_by(
        ontology_id=oid,
        id="mapping-order",
    ).one()
    assert mapping.status == "applied"
    assert mapping.field_mapping["__auto_apply_on_version__"] is True
    assert mapping.field_mapping[
        "__applied_dataset_version_id__"
    ] == run["dataset_versions"][0]["versionId"]


def test_rollback_creates_new_activation_and_preserves_historical_runtime_lineage(
        client, auth_headers, ontology, db, admin_user, monkeypatch):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)

    def snapshot(display_name: str) -> dict:
        suffix = "old" if display_name == "历史" else "new"
        return {
            "objectTypes": [
                {
                    "id": "ot-source", "name": "Source",
                    "displayName": f"{display_name}源",
                    "primaryKey": "p-source-id",
                    "properties": [{
                        "id": "p-source-id", "name": "id",
                        "displayName": "源编号", "type": "string",
                        "required": True,
                    }, {
                        "id": "p-source-label", "name": "label",
                        "displayName": "派生标签", "type": "string",
                        "required": False, "source": "computed",
                        "computed": True, "functionId": "fn-source-label",
                    }],
                    "interfaces": [], "positionX": 0, "positionY": 0,
                },
                {
                    "id": "ot-target", "name": "Target",
                    "displayName": f"{display_name}目标",
                    "primaryKey": "p-target-id",
                    "properties": [{
                        "id": "p-target-id", "name": "id",
                        "displayName": "目标编号", "type": "string",
                        "required": True,
                    }],
                    "interfaces": [], "positionX": 0, "positionY": 0,
                },
            ],
            "linkTypes": [{
                "id": "lt-related", "name": "related",
                "displayName": "关联",
                "sourceObjectTypeId": "ot-source",
                "targetObjectTypeId": "ot-target",
                "cardinality": "many-to-many", "properties": [],
            }],
            "actions": [], "functions": [{
                "id": "fn-source-label", "name": "source_label",
                "displayName": "源标签", "functionType": "object",
                "language": "expression",
                "targetObjectTypeId": "ot-source",
                "parameters": [], "returnType": "string",
                "body": f"object['id'] + '-{suffix}'",
                "enabled": True,
            }],
            "sentinels": [{
                "id": "sentinel-builtin", "name": "watch_source",
                "displayName": f"{display_name}内置哨兵",
                "bindings": [{
                    "alias": "source", "objectTypeId": "ot-source",
                }],
                "links": [], "condition": None, "conditionRows": [],
                "conditionLogic": "and", "primaryAlias": "source",
                "actionIds": [], "actionParameters": {},
                "onChange": True, "onSchedule": False,
                "scanIntervalSeconds": 300,
                "triggerMode": "on_enter_leave",
                "muted": False, "enabled": True, "status": "published",
            }],
            "mappings": [], "linkMappings": [],
        }

    target_snapshot = snapshot("历史")
    current_snapshot = snapshot("当前")
    target = OntologyVersion(
        id="release-v1-target", ontology_id=oid, version_number="v1",
        version_label="历史发布", parent_version_id=root["id"],
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_formal=target_snapshot,
        snapshot_hash=snapshot_hash(target_snapshot),
        published_at=datetime.now(timezone.utc),
        created_by=admin_user.id,
    )
    db.add(target)
    db.flush()
    target.base_release_id = target.id
    target_id = target.id
    current = OntologyVersion(
        id="release-v2-current", ontology_id=oid, version_number="v2",
        version_label="当前发布", parent_version_id=target.id,
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_formal=current_snapshot,
        snapshot_hash=snapshot_hash(current_snapshot),
        published_at=datetime.now(timezone.utc),
        created_by=admin_user.id,
    )
    db.add(current)
    db.flush()
    current.base_release_id = current.id
    current_id = current.id
    # Reproduce a legacy pointer-reuse history: a later release number exists,
    # while the authoritative pointer was moved back to v2. New activation
    # numbering must append globally instead of generating an out-of-order v3.
    dormant = OntologyVersion(
        id="release-v7-historical", ontology_id=oid, version_number="v7",
        version_label="历史上更晚的节点", parent_version_id=current.id,
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_formal=current_snapshot,
        snapshot_hash=snapshot_hash(current_snapshot),
        published_at=datetime.now(timezone.utc),
        created_by=admin_user.id,
    )
    db.add(dormant)
    db.flush()
    dormant.base_release_id = dormant.id
    project = db.query(OntologyProject).filter_by(id=oid).one()
    project.current_release_id = current.id
    project.version = current.version_number
    project.status = "published"
    version_router._restore_formal_snapshot(db, oid, current_snapshot)
    db.flush()
    db.add_all([
        ObjectInstance(
            id="source-1", ontology_id=oid,
            ontology_release_id=current.id,
            object_type_id="ot-source", properties={"id": "S-1"},
            computed={"label": "S-1-new"},
            source="pipeline", external_id="S-1",
        ),
        ObjectInstance(
            id="target-1", ontology_id=oid,
            ontology_release_id=current.id,
            object_type_id="ot-target", properties={"id": "T-1"},
            computed={}, source="pipeline", external_id="T-1",
        ),
        LinkInstance(
            id="related-1", ontology_id=oid,
            ontology_release_id=current.id,
            link_type_id="lt-related",
            source_object_id="source-1", target_object_id="target-1",
            properties={},
        ),
        Sentinel(
            id="sentinel-dynamic", ontology_id=oid,
            name="assistant_watch", display_name="助手动态哨兵",
            bindings=[{"alias": "source", "objectTypeId": "ot-source"}],
            links=[], condition=None, primary_alias="source",
            action_ids=[], action_parameters={},
            enabled=True, status="published", origin="assistant_dynamic",
            bound_release_id=current.id, definition_revision=3,
            validation_report={"passed": True},
            last_trial_at=datetime.now(timezone.utc),
            last_trial_release_id=current.id, last_trial_revision=3,
            last_trial_report={"passed": True},
        ),
        SentinelMatchState(
            id="match-builtin", ontology_id=oid,
            sentinel_id="sentinel-builtin", match_key="source-1",
            match_detail={"source": "source-1"}, runtime_status="completed",
        ),
        SentinelMatchState(
            id="match-dynamic", ontology_id=oid,
            sentinel_id="sentinel-dynamic", match_key="source-1",
            match_detail={"source": "source-1"}, runtime_status="completed",
        ),
        SentinelFiring(
            id="firing-v2", ontology_id=oid,
            sentinel_id="sentinel-dynamic", sentinel_name="助手动态哨兵",
            trigger_source="change", matches=[], entered=[], left=[],
            action_results=[], status="fired",
            ontology_version="v2", ontology_release_id=current.id,
        ),
        ActionExecutionLog(
            id="pending-v2", ontology_id=oid, action_id="historical-action",
            object_type_id="ot-source", object_instance_id="source-1",
            parameters={}, status="pending", validation_errors=[], effects=[],
            dry_run=False, ontology_version="v2",
            ontology_release_id=current.id,
        ),
    ])
    db.commit()
    db.expunge_all()
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections",
        lambda *_args, **_kwargs: {
            "ready": True, "neo4j": "ok",
        },
    )

    response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{target_id}/rollback",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    activation = response.json()["data"]
    assert activation["id"] not in {target_id, current_id}
    assert activation["version_number"] == "v8"
    assert activation["parent_version_id"] == current_id
    assert activation["rolled_back_to_id"] == target_id

    db.expire_all()
    project = db.query(OntologyProject).filter_by(id=oid).one()
    assert project.current_release_id == activation["id"]
    assert project.version == "v8"
    assert {
        item.ontology_release_id
        for item in db.query(ObjectInstance).filter_by(ontology_id=oid).all()
    } == {activation["id"]}
    assert db.query(ObjectInstance).filter_by(
        id="source-1").one().computed == {"label": "S-1-old"}
    assert {
        item.ontology_release_id
        for item in db.query(LinkInstance).filter_by(ontology_id=oid).all()
    } == {activation["id"]}
    builtin = db.query(Sentinel).filter_by(id="sentinel-builtin").one()
    assert builtin.status == "published"
    assert builtin.trigger_mode == "on_enter_leave"
    dynamic = db.query(Sentinel).filter_by(id="sentinel-dynamic").one()
    assert dynamic.enabled is False
    assert dynamic.bound_release_id == current_id
    assert dynamic.last_trial_at is None
    assert dynamic.last_trial_release_id is None
    assert dynamic.last_trial_revision is None
    assert dynamic.last_trial_report is None
    assert db.query(SentinelMatchState).filter_by(ontology_id=oid).count() == 0

    firing = db.query(SentinelFiring).filter_by(id="firing-v2").one()
    pending = db.query(ActionExecutionLog).filter_by(id="pending-v2").one()
    assert firing.ontology_release_id == current_id
    assert pending.ontology_release_id == current_id
    derived = db.query(PropertyFact).filter_by(
        ontology_id=oid, instance_id="source-1",
        property_name="label", kind="derived").one()
    assert derived.ontology_release_id == activation["id"]
    decision = client.post(
        f"/api/v2/formal/ontologies/{oid}/action-logs/pending-v2/decide",
        headers=auth_headers,
        json={"decision": "approved", "releaseId": activation["id"]},
    )
    assert decision.status_code == 409, decision.text
    assert "发布节点不一致" in str(decision.json()["detail"])


def test_release_readiness_groups_legacy_missing_property_mappings(
        client, auth_headers, ontology, db, monkeypatch):
    """Legacy passed trials still receive a friendly, fail-closed preflight.

    Older deployments could freeze a trial while one or more persisted fields
    were not mapped. The impact endpoint exposes structured target/field
    metadata so the UI can group blockers instead of dumping messages.
    """
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, root["id"]),
        db,
    )
    run_payload = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    ).json()["data"]

    current_row = db.query(OntologyVersion).filter_by(id=root["id"]).one()
    draft_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    run_row = db.query(OntologyTrialRun).filter_by(id=run_payload["id"]).one()
    legacy_snapshot = copy.deepcopy(draft_row.snapshot_formal)
    legacy_snapshot["mappings"][0]["fieldMapping"].pop("name")
    legacy_hash = snapshot_hash(legacy_snapshot)
    draft_row.snapshot_formal = legacy_snapshot
    draft_row.snapshot_hash = legacy_hash
    draft_row.lifecycle_status = "trial_ready"
    run_row.snapshot_hash = legacy_hash
    run_row.impact_hash = impact_report(
        current_row.snapshot_formal, legacy_snapshot)["impactHash"]
    db.commit()

    response = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    readiness = response.json()["data"]["releaseReadiness"]
    assert readiness["ready"] is False
    assert readiness["blockingCount"] == 1
    assert readiness["trialRunId"] == run_payload["id"]
    assert readiness["repairStrategy"] == "create_draft"
    assert readiness["repairSourceVersionId"] == draft["id"]
    assert readiness["errors"] == [{
        "code": "mapping_property_missing",
        "kind": "mapping",
        "id": "mapping-order",
        "name": "Order",
        "targetId": "ot-order",
        "targetName": "订单",
        "message": "Mapping「Order」未覆盖 ObjectType「订单」的存储属性「name」",
        "field": "name",
    }]


def test_new_lake_version_after_trial_requires_rerun(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    service, _ = _dataset(db, monkeypatch)
    draft = _configure_draft(
        client, auth_headers, oid,
        _draft(client, auth_headers, oid, _root(client, auth_headers, oid)["id"]),
        db,
    )
    run = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={}).json()["data"]
    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact",
        headers=auth_headers).json()["data"]
    service.create_version(
        "dataset-orders", _csv([{"id": "O-3", "name": "三号订单"}]),
        rowcount=1)

    promoted = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/promote",
        headers=auth_headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]},
    )
    assert promoted.status_code == 422
    codes = {item["code"] for item in promoted.json()["detail"]["errors"]}
    assert "trial_dataset_version_stale" in codes
    db.expire_all()
    assert db.query(OntologyProject).filter_by(id=oid).one().current_release_id == _root(
        client, auth_headers, oid)["id"]
    assert db.query(ObjectInstance).filter_by(ontology_id=oid).count() == 0


def test_impact_report_marks_breaking_property_changes(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    first = _configure_draft(
        client, auth_headers, oid, _draft(client, auth_headers, oid, root["id"]),
        db)
    # 直接把构造好的候选作为当前发布基线，专测语义影响分析，无需引入数据湖。
    root_row = db.query(OntologyVersion).filter_by(id=root["id"]).one()
    candidate = db.query(OntologyVersion).filter_by(id=first["id"]).one()
    root_row.snapshot_formal = candidate.snapshot_formal
    root_row.snapshot_hash = candidate.snapshot_hash
    db.commit()

    second = _draft(client, auth_headers, oid, root["id"])
    workspace = client.get(
        f"/api/v2/ontologies/{oid}/versions/{second['id']}/workspace",
        headers=auth_headers).json()["data"]
    workspace["baseRevision"] = workspace["revision"]
    workspace["objectTypes"][0]["properties"][1]["type"] = "number"
    assert client.put(
        f"/api/v2/ontologies/{oid}/versions/{second['id']}/workspace",
        headers=auth_headers, json=workspace).status_code == 200
    impact = client.get(
        f"/api/v2/ontologies/{oid}/versions/{second['id']}/impact",
        headers=auth_headers).json()["data"]
    assert impact["breakingCount"] == 1
    assert impact["breaking"][0]["code"] == "property_type_changed"
    assert len(impact["impactHash"]) == 64


# ---------------------------------------------------------------- 业务语义层接线


def test_semantic_layer_summary_and_branch_inheritance(
        client, auth_headers, ontology, db):
    """版本摘要透出 hasSemanticLayer/semanticRevision；分支继承整份语义层。"""
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    tree = client.get(
        f"/api/v2/ontologies/{oid}/version-tree", headers=auth_headers,
    ).json()["data"]
    root_payload = next(
        item for item in tree["versions"] if item["id"] == root["id"])
    assert root_payload["hasSemanticLayer"] is False
    assert root_payload["semanticRevision"] == 0

    draft = _draft(client, auth_headers, oid, root["id"])
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text
    _attach_semantic_layer(db, draft["id"])

    tree = client.get(
        f"/api/v2/ontologies/{oid}/version-tree", headers=auth_headers,
    ).json()["data"]
    draft_payload = next(
        item for item in tree["versions"] if item["id"] == draft["id"])
    assert draft_payload["hasSemanticLayer"] is True
    assert draft_payload["semanticRevision"] == 1
    # 摘要只给标记与修订号，不透出整份画布/文档 JSON
    assert "canvas" not in draft_payload
    assert "documentMd" not in draft_payload

    child = _draft(client, auth_headers, oid, draft["id"])
    assert child["hasSemanticLayer"] is True
    assert child["semanticRevision"] == 1
    db.expire_all()
    parent_row = db.query(OntologyVersion).filter_by(id=draft["id"]).one()
    child_row = db.query(OntologyVersion).filter_by(id=child["id"]).one()
    assert child_row.snapshot_semantic == parent_row.snapshot_semantic


def test_trial_gate_requires_semantic_layer_for_non_empty_structure(
        client, auth_headers, ontology, db, monkeypatch):
    """结构非空但无语义层的草稿被试跑门禁拦下；补上自洽语义层后放行。"""
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    draft = _draft(client, auth_headers, oid, root["id"])
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text
    mapped = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace/mappings",
        headers=auth_headers, json={
            "baseRevision": saved.json()["data"]["revision"],
            "mappings": [{
                "id": "mapping-order", "curatedDatasetId": "dataset-orders",
                "entityClass": "Order", "targetObjectTypeId": "ot-order",
                "fieldMapping": {
                    "id": "id", "name": "name", "__primary_key__": "id",
                },
                "status": "draft", "confidence": 1,
            }],
            "linkMappings": [], "sentinels": [],
        },
    )
    assert mapped.status_code == 200, mapped.text

    blocked = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert blocked.status_code == 422, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "publish_validation_failed"
    assert {item["code"] for item in detail["errors"]} == {
        "semantic_business_missing",
    }
    error = detail["errors"][0]
    assert error["kind"] == "objectType"
    assert error["id"] == "Order"
    assert "在业务画布中没有对应" in error["message"]
    assert db.query(OntologyTrialRun).filter_by(
        version_id=draft["id"]).count() == 0

    _attach_semantic_layer(db, draft["id"])
    trial = client.post(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/trial-runs",
        headers=auth_headers, json={},
    )
    assert trial.status_code == 201, trial.text
    assert trial.json()["data"]["status"] == "passed"


def test_promotion_carries_semantic_layer_to_release(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    _dataset(db, monkeypatch)
    root = _root(client, auth_headers, oid)
    release = _promote_configured_lake_release(
        client, auth_headers, oid, root["id"], db)
    assert release["hasSemanticLayer"] is True
    assert release["semanticRevision"] == 1

    db.expire_all()
    release_row = db.query(OntologyVersion).filter_by(id=release["id"]).one()
    semantic = release_row.snapshot_semantic
    assert semantic["documentTitle"] == "测试需求文档"
    assert semantic["documentFingerprint"] == hashlib.sha256(
        _SEMANTIC_TEST_DOCUMENT_MD.encode("utf-8")).hexdigest()
    assert semantic["canvasFingerprint"] == canvas_fingerprint(
        semantic["canvas"])
    assert [item["name"] for item in semantic["canvas"]["objects"]] == ["Order"]
    # 语义层是展示/语义元数据，不参与结构快照哈希契约
    assert release_row.snapshot_hash == snapshot_hash(
        release_row.snapshot_formal)


def test_rollback_activation_inherits_semantic_layer(
        client, auth_headers, ontology, db, admin_user, monkeypatch):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)

    def make_release(version_id: str, number: str, parent_id: str,
                     *, with_semantic: bool) -> OntologyVersion:
        snapshot = {
            "objectTypes": [{
                "id": "ot-order", "name": "Order", "displayName": "订单",
                "primaryKey": "p-id",
                "properties": [{
                    "id": "p-id", "name": "id", "displayName": "订单号",
                    "type": "string", "required": True,
                }],
            }],
            "linkTypes": [], "actions": [], "functions": [],
            "sentinels": [], "mappings": [], "linkMappings": [],
        }
        row = OntologyVersion(
            id=version_id, ontology_id=oid, version_number=number,
            parent_version_id=parent_id, base_release_id=version_id,
            node_kind="release", lifecycle_status="released", revision=0,
            snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
            published_at=datetime.now(timezone.utc),
            created_by=admin_user.id,
        )
        db.add(row)
        db.flush()
        if with_semantic:
            _attach_semantic_layer(db, row.id)
        return row

    target = make_release(
        "rollback-semantic-v1", "v1", root["id"], with_semantic=True)
    current = make_release(
        "rollback-semantic-v2", "v2", target.id, with_semantic=False)
    project = db.query(OntologyProject).filter_by(id=oid).one()
    project.current_release_id = current.id
    project.version = current.version_number
    project.status = "published"
    db.commit()
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections",
        lambda *_args, **_kwargs: {"ready": True, "neo4j": "ok"},
    )

    response = client.post(
        f"/api/v2/ontologies/{oid}/versions/{target.id}/rollback",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    activation = response.json()["data"]
    assert activation["hasSemanticLayer"] is True
    assert activation["semanticRevision"] == 1
    db.expire_all()
    activation_row = db.query(OntologyVersion).filter_by(
        id=activation["id"]).one()
    target_row = db.query(OntologyVersion).filter_by(id=target.id).one()
    assert activation_row.snapshot_semantic == target_row.snapshot_semantic

    # 历史版本没有语义层时，回滚激活也不伪造语义层
    plain = client.post(
        f"/api/v2/ontologies/{oid}/versions/{current.id}/rollback",
        headers=auth_headers,
    )
    assert plain.status_code == 200, plain.text
    assert plain.json()["data"]["hasSemanticLayer"] is False
    assert plain.json()["data"]["semanticRevision"] == 0


def test_impact_and_semantic_endpoints_expose_semantic_overview(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    root = _root(client, auth_headers, oid)
    draft = _draft(client, auth_headers, oid, root["id"])
    saved = client.put(
        f"/api/v2/ontologies/{oid}/versions/{draft['id']}/workspace",
        headers=auth_headers, json=_workspace(draft),
    )
    assert saved.status_code == 200, saved.text

    impact_url = f"/api/v2/ontologies/{oid}/versions/{draft['id']}/impact"
    impact = client.get(impact_url, headers=auth_headers)
    assert impact.status_code == 200, impact.text
    overview = impact.json()["data"]["semanticOverview"]
    assert overview["hasSemanticLayer"] is False
    assert overview["documentTitle"] is None
    assert overview["structureCounts"]["objectTypes"] == 1
    assert overview["consistency"]["byCode"] == {"semantic_business_missing": 1}

    _attach_semantic_layer(db, draft["id"])
    impact = client.get(impact_url, headers=auth_headers).json()["data"]
    overview = impact["semanticOverview"]
    assert overview["hasSemanticLayer"] is True
    assert overview["documentTitle"] == "测试需求文档"
    assert overview["documentStale"] is False
    assert overview["canvasCounts"]["objects"] == 1
    assert overview["structureCounts"]["objectTypes"] == 1
    assert overview["consistency"] == {"issueCount": 0, "byCode": {}}
    # 纯增量：影响分析既有字段保持原样
    assert "releaseReadiness" in impact
    assert len(impact["impactHash"]) == 64

    semantic_url = f"/api/v2/ontologies/{oid}/versions/{draft['id']}/semantic"
    detail = client.get(semantic_url, headers=auth_headers)
    assert detail.status_code == 200, detail.text
    payload = detail.json()["data"]
    assert payload["semantic"]["documentTitle"] == "测试需求文档"
    assert payload["semantic"]["semanticRevision"] == 1
    assert payload["semantic"]["canvas"]["objects"][0]["name"] == "Order"
    assert payload["overview"] == overview

    # 读端点不要求 draft：发布版本同样可读；v0 尚未沉淀语义层
    root_detail = client.get(
        f"/api/v2/ontologies/{oid}/versions/{root['id']}/semantic",
        headers=auth_headers,
    )
    assert root_detail.status_code == 200, root_detail.text
    root_payload = root_detail.json()["data"]
    assert root_payload["semantic"] is None
    assert root_payload["overview"]["hasSemanticLayer"] is False
    assert root_payload["overview"]["structureCounts"]["objectTypes"] == 0

    missing = client.get(
        f"/api/v2/ontologies/{oid}/versions/no-such-version/semantic",
        headers=auth_headers,
    )
    assert missing.status_code == 404, missing.text


def test_create_initial_release_persists_optional_semantic_layer(
        db, admin_user):
    from app.ontologies.release_context import create_initial_release

    project = OntologyProject(
        id="semantic-initial-release-project", name="语义层初始基线",
        domain="其他", version="v0.1", status="draft", build_mode="manual",
        created_by=admin_user.id,
    )
    db.add(project)
    db.flush()
    release = create_initial_release(
        db, project, snapshot=None, created_by=admin_user.id)
    assert release.snapshot_semantic is None

    second = OntologyProject(
        id="semantic-initial-release-project-2", name="语义层初始基线二",
        domain="其他", version="v0.1", status="draft", build_mode="manual",
        created_by=admin_user.id,
    )
    db.add(second)
    db.flush()
    layer = {"canvas": {"objects": []}, "semanticRevision": 7}
    release_with_layer = create_initial_release(
        db, second, snapshot=None, created_by=admin_user.id, semantic=layer)
    assert release_with_layer.snapshot_semantic == layer
