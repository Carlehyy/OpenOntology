"""本体发布是运行安全边界：只有通过全量契约的 draft 才能生成不可变快照。"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models.ontology import OntologyProject
from app.models.ontology_formal import (
    ObjectType, LinkType, ActionType, OntologyFunction,
    ObjectInstance,
)
from app.models.sentinel import Sentinel
from app.models.ontology_version import OntologyTrialRun, OntologyVersion
from app.models.v2.mapping import OntologyMapping, OntologyLinkMapping
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.curated import CuratedReview
from app.ontologies.versions import router as version_router
from app.ontologies.versions.evolution_service import (
    complete_snapshot,
    impact_report,
    snapshot_hash,
)


def _versions(ontology_id: str) -> str:
    return f"/api/v2/ontologies/{ontology_id}/versions"


def _codes(response) -> set[str]:
    return {item["code"] for item in response.json()["detail"]["errors"]}


def _release_node(
        db, project: OntologyProject, *, item_id: str, number: str,
        snapshot: dict, parent_id: str) -> OntologyVersion:
    release = OntologyVersion(
        id=item_id, ontology_id=project.id, version_number=number,
        version_label=number, parent_version_id=parent_id,
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
        published_at=datetime.now(timezone.utc), created_by=project.created_by,
    )
    db.add(release)
    db.flush()
    release.base_release_id = release.id
    return release


def _object_type(ontology_id: str, *, item_id: str = "ot-order",
                 with_contract: bool = False) -> ObjectType:
    properties = []
    primary_key = None
    if with_contract:
        properties = [{
            "id": "p-code", "name": "code", "displayName": "编号",
            "type": "string", "required": True,
        }]
        primary_key = "p-code"
    return ObjectType(
        id=item_id, ontology_id=ontology_id,
        name="Order", display_name="订单",
        primary_key=primary_key, properties=properties,
        interfaces=[], position_x=0, position_y=0,
    )


def _rollback_release_pair(db, ontology_id: str, *, prefix: str):
    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    object_type = _object_type(ontology_id)
    object_type.display_name = "历史订单"
    db.add(object_type)
    db.flush()
    target = _release_node(
        db,
        project,
        item_id=f"{prefix}-v1",
        number="v1",
        snapshot=version_router._snapshot_formal(db, ontology_id),
        parent_id=project.current_release_id,
    )
    object_type.display_name = "当前订单"
    db.flush()
    current = _release_node(
        db,
        project,
        item_id=f"{prefix}-v2",
        number="v2",
        snapshot=version_router._snapshot_formal(db, ontology_id),
        parent_id=target.id,
    )
    project.current_release_id = current.id
    project.version = current.version_number
    project.status = "published"
    db.commit()
    return target, current, object_type


def _editor_headers(client, editor_user) -> dict:
    response = client.post("/api/v1/auth/login", json={
        "username": editor_user.username, "password": "editor123",
    })
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_publish_requires_formal_contract_admin_and_draft_state(
        client, auth_headers, ontology, db, editor_user):
    oid = ontology["id"]

    assert "object_type_required" in {
        item["code"] for item in version_router._release_errors(db, oid)}

    object_type = _object_type(oid)
    function = OntologyFunction(
        id="fn-ts", ontology_id=oid,
        name="unsafe", display_name="未受控脚本",
        function_type="object", language="typescript",
        target_object_type_id=object_type.id,
        parameters=[], return_type="string", body="return 'x'", enabled=True,
    )
    db.add_all([object_type, function]); db.commit()

    assert "enabled_typescript_function_forbidden" in {
        item["code"] for item in version_router._release_errors(db, oid)}

    function.enabled = False
    db.commit()
    assert version_router._release_errors(db, oid) == []
    project = db.query(OntologyProject).filter_by(id=oid).one()
    current_release_id = project.current_release_id
    response = client.post(
        _versions(oid), headers=auth_headers, json={"version_label": "v1"})
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["code"] == "legacy_publish_endpoint_retired"

    # 已废弃入口在任何状态都不能绕过草稿→试跑→晋级。
    response = client.post(_versions(oid), headers=auth_headers)
    assert response.status_code == 410
    db.refresh(project)
    assert project.current_release_id == current_release_id

    editor_headers = _editor_headers(client, editor_user)
    assert client.post(f"/api/v2/ontologies/{oid}/unpublish",
                       headers=editor_headers).status_code == 403
    response = client.post(f"/api/v2/ontologies/{oid}/unpublish", headers=auth_headers)
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "unpublish_endpoint_retired"
    assert client.post(_versions(oid), headers=editor_headers, json={}).status_code == 403
    assert client.post(f"{_versions(oid)}/{current_release_id}/rollback",
                       headers=editor_headers).status_code == 403


def test_legacy_publish_cannot_elevate_mutable_builtin_sentinel(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    db.add(_object_type(oid))
    db.commit()
    base = f"/api/v1/ontologies/{oid}/sentinels"

    response = client.post(f"{base}/", headers=auth_headers, json={
        "name": "watch_orders", "displayName": "监控订单",
        "bindings": [{"alias": "order", "objectTypeId": "ot-order"}],
        "primaryAlias": "order", "actionIds": [],
        "status": "published",
    })
    assert response.status_code == 201, response.text
    sentinel_id = response.json()["data"]["id"]
    assert response.json()["data"]["status"] == "draft"

    legacy = client.post(_versions(oid), headers=auth_headers, json={})
    assert legacy.status_code == 410
    assert client.get(
        f"{base}/{sentinel_id}",
        headers=auth_headers).json()["data"]["status"] == "draft"
    response = client.put(
        f"{base}/{sentinel_id}", headers=auth_headers,
        json={"condition": "order.amount > 10"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "draft"


def test_publish_validates_sentinel_references_aliases_and_required_action_params(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    order = _object_type(oid)
    customer = ObjectType(
        id="ot-customer", ontology_id=oid,
        name="Customer", display_name="客户", properties=[], interfaces=[],
        position_x=0, position_y=0,
    )
    relation = LinkType(
        id="lt-owner", ontology_id=oid,
        name="owned_by", display_name="归属",
        source_object_type_id=order.id,
        target_object_type_id=customer.id,
        cardinality="many-to-one", properties=[],
    )
    action = ActionType(
        id="act-review", ontology_id=oid,
        name="review", display_name="发起复核",
        object_type_id=order.id,
        parameters=[
            {"name": "reason", "type": "string", "required": True},
            {"name": "severity", "type": "string", "required": True,
             "defaultValue": "normal"},
        ],
        rules=[{
            "id": "notify-review", "type": "notification",
            "name": "通知复核队列", "enabled": True, "order": 0,
            "config": {
                "channel": "internal",
                "recipientSource": "constant",
                "recipient": "review-queue",
                "messageTemplate": "需要人工复核",
            },
        }], requires_approval=False,
    )
    sentinel = Sentinel(
        id="sentinel-review", ontology_id=oid,
        name="review_orders", display_name="订单复核哨兵",
        bindings=[
            {"alias": "order", "objectTypeId": order.id},
            {"alias": "order", "objectTypeId": customer.id},
        ],
        links=[{"from": "order", "to": "missing", "linkTypeId": "missing-link"}],
        primary_alias="missing",
        action_ids=[action.id, "missing-action"],
        action_parameters={},
        enabled=True, status="draft",
    )
    db.add_all([order, customer, relation, action, sentinel]); db.commit()

    codes = {
        item["code"] for item in version_router._release_errors(db, oid)}
    assert {
        "duplicate_sentinel_alias",
        "invalid_sentinel_primary_alias",
        "sentinel_link_alias_not_found",
        "sentinel_link_type_not_found",
        "sentinel_action_not_found",
        "sentinel_required_action_parameter_missing",
    } <= codes

    sentinel.bindings = [
        {"alias": "order", "objectTypeId": order.id},
        {"alias": "customer", "objectTypeId": customer.id},
    ]
    sentinel.links = [{
        "from": "order", "to": "customer", "linkTypeId": relation.id,
    }]
    sentinel.primary_alias = "order"
    sentinel.action_ids = [action.id]
    sentinel.action_parameters = {
        action.id: {"reason": {"source": "constant", "value": "risk"}},
    }
    db.commit()

    assert version_router._release_errors(db, oid) == []
    db.refresh(sentinel)
    assert sentinel.status == "draft"


def test_snapshot_diff_and_rollback_restore_configs_without_deleting_instances(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    object_type = _object_type(oid, with_contract=True)
    instance = ObjectInstance(
        id="order-1", ontology_id=oid, object_type_id=object_type.id,
        properties={"code": "O-1"}, computed={}, source="pipeline",
    )
    sentinel = Sentinel(
        id="sentinel-1", ontology_id=oid,
        name="watch", display_name="监控订单",
        bindings=[{"alias": "order", "objectTypeId": object_type.id}],
        links=[], primary_alias="order", action_ids=[], action_parameters={},
        enabled=True, status="draft",
    )
    dataset = Dataset(id="dataset-1", name=f"dataset-{uuid.uuid4().hex}", kind="curated")
    mapping = OntologyMapping(
        id="mapping-1", ontology_id=oid, curated_dataset_id=dataset.id,
        entity_class="Order", target_object_type_id=object_type.id,
        field_mapping={"__primary_key__": "code"}, status="draft",
    )
    link_mapping = OntologyLinkMapping(
        id="link-mapping-1", ontology_id=oid,
        src_dataset_id=dataset.id, tgt_dataset_id=dataset.id,
        relation_type="SELF", src_key="code", tgt_key="code", status="draft",
        field_mapping={},
    )
    db.add_all([object_type, instance, sentinel, dataset, mapping, link_mapping])
    db.commit()

    project = db.query(OntologyProject).filter_by(id=oid).one()
    root_id = project.current_release_id
    baseline = version_router._snapshot_formal(db, oid)
    v1_row = _release_node(
        db, project, item_id="release-config-v1", number="v1",
        snapshot=baseline, parent_id=root_id)
    db.commit()
    v1 = v1_row.id
    detail = client.get(f"{_versions(oid)}/{v1}", headers=auth_headers).json()["data"]
    snap = detail["snapshot"]["formal"]
    assert len(snap["sentinels"]) == len(snap["mappings"]) == len(snap["linkMappings"]) == 1

    object_type.display_name = "已修改订单"
    sentinel.display_name = "已修改哨兵"
    mapping.entity_class = "ChangedOrder"
    link_mapping.relation_type = "CHANGED"
    db.commit()

    changed = version_router._snapshot_formal(db, oid)
    diff = version_router._diff_formal(baseline, changed)
    assert diff["sentinels"]["modified"] == 1
    assert diff["mappings"]["modified"] == 1
    assert diff["linkMappings"]["modified"] == 1
    v2_row = _release_node(
        db, project, item_id="release-config-v2", number="v2",
        snapshot=changed, parent_id=v1)
    project.current_release_id = v2_row.id
    project.version = "v2"
    project.status = "published"
    instance.ontology_release_id = v2_row.id
    db.commit()
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections",
        lambda *_args, **_kwargs: {
            "ready": True, "neo4j": "ok",
        },
    )

    response = client.post(f"{_versions(oid)}/{v1}/rollback", headers=auth_headers)
    assert response.status_code == 200, response.text
    restored = response.json()["data"]["formal_restored"]
    assert restored["retainedInstances"] == 1 and restored["prunedInstances"] == 0

    db.expire_all()
    assert db.query(ObjectType).filter_by(id="ot-order").one().display_name == "订单"
    assert db.query(Sentinel).filter_by(id="sentinel-1").one().display_name == "监控订单"
    assert db.query(OntologyMapping).filter_by(id="mapping-1").one().entity_class == "Order"
    assert db.query(OntologyLinkMapping).filter_by(id="link-mapping-1").one().relation_type == "SELF"
    assert db.query(ObjectInstance).filter_by(id="order-1").count() == 1
    project = db.query(OntologyProject).filter_by(id=oid).one()
    assert project.status == "published"
    assert project.current_release_id == response.json()["data"]["id"]
    assert project.current_release_id not in {v1, v2_row.id}


def test_rollback_is_atomic_when_snapshot_rejects_retained_instance(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    object_type = _object_type(oid, with_contract=True)
    instance = ObjectInstance(
        id="order-bad", ontology_id=oid, object_type_id=object_type.id,
        properties={"code": "O-1"}, computed={}, source="pipeline",
    )
    db.add_all([object_type, instance]); db.commit()
    project = db.query(OntologyProject).filter_by(id=oid).one()
    version = _release_node(
        db, project, item_id="release-strict-v1", number="v1",
        snapshot=version_router._snapshot_formal(db, oid),
        parent_id=project.current_release_id,
    )
    db.commit()
    version_id = version.id

    # 当前发布投影放宽为开放 schema，并写入与旧快照不兼容的实例。
    object_type.primary_key = None
    object_type.properties = []
    object_type.display_name = "当前草稿"
    instance.properties = {"undeclared": True}
    current = _release_node(
        db, project, item_id="release-open-v2", number="v2",
        snapshot=version_router._snapshot_formal(db, oid),
        parent_id=version_id,
    )
    project.current_release_id = current.id
    project.version = current.version_number
    project.status = "published"
    instance.ontology_release_id = current.id
    db.commit()
    current_id = current.id
    db.expunge_all()

    response = client.post(f"{_versions(oid)}/{version_id}/rollback", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "rollback_validation_failed"
    assert {"unknown_property", "required_property_missing", "primary_key_missing"} <= _codes(response)

    # 整个恢复事务已回滚：schema、实例和发布状态都保持请求前状态。
    current_type = db.query(ObjectType).filter_by(id="ot-order").one()
    current_instance = db.query(ObjectInstance).filter_by(id="order-bad").one()
    assert current_type.display_name == "当前草稿" and current_type.properties == []
    assert current_instance.properties == {"undeclared": True}
    unchanged_project = db.query(OntologyProject).filter_by(id=oid).one()
    assert unchanged_project.status == "published"
    assert unchanged_project.current_release_id == current_id


@pytest.mark.parametrize("runtime_environment", ["development", "production"])
def test_runtime_rollback_projection_failure_keeps_previous_activation(
        client, auth_headers, ontology, db, monkeypatch, runtime_environment):
    oid = ontology["id"]
    project = db.query(OntologyProject).filter_by(id=oid).one()
    object_type = _object_type(oid)
    object_type.display_name = "历史订单"
    db.add(object_type)
    db.flush()
    target = _release_node(
        db, project, item_id="rollback-projection-v1", number="v1",
        snapshot=version_router._snapshot_formal(db, oid),
        parent_id=project.current_release_id,
    )
    object_type.display_name = "当前订单"
    db.flush()
    current = _release_node(
        db, project, item_id="rollback-projection-v2", number="v2",
        snapshot=version_router._snapshot_formal(db, oid),
        parent_id=target.id,
    )
    project.current_release_id = current.id
    project.version = current.version_number
    project.status = "published"
    db.commit()
    current_id = current.id
    version_count = db.query(OntologyVersion).filter_by(
        ontology_id=oid).count()
    monkeypatch.setattr(
        version_router.settings, "environment", runtime_environment)
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections",
        lambda *_args, **_kwargs: {
            "ready": False, "neo4j": "error",
        },
    )

    response = client.post(
        f"{_versions(oid)}/{target.id}/rollback", headers=auth_headers)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "rollback_projection_not_ready"
    assert response.json()["detail"]["compensation"]["ready"] is False
    db.expire_all()
    project = db.query(OntologyProject).filter_by(id=oid).one()
    assert project.current_release_id == current_id
    assert project.version == "v2"
    assert db.query(ObjectType).filter_by(
        id=object_type.id).one().display_name == "当前订单"
    assert db.query(OntologyVersion).filter_by(
        ontology_id=oid).count() == version_count


@pytest.mark.parametrize("compensation_ready", [True, False])
def test_promotion_post_fence_preparation_failure_is_compensated(
        client, auth_headers, ontology, db, admin_user, monkeypatch,
        compensation_ready):
    """An expired-row/query failure after the fence must never strand it."""
    oid = ontology["id"]
    project = db.query(OntologyProject).filter_by(id=oid).one()
    current = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    current_id = current.id
    candidate = complete_snapshot(current.snapshot_formal)
    candidate_hash = snapshot_hash(candidate)
    report = impact_report(current.snapshot_formal, candidate)
    draft = OntologyVersion(
        id="promotion-fence-gap-draft",
        ontology_id=oid,
        version_number="v0.1",
        version_label="故障注入草稿",
        parent_version_id=current.id,
        base_release_id=current.id,
        node_kind="draft",
        lifecycle_status="trial_ready",
        revision=0,
        snapshot_formal=candidate,
        snapshot_hash=candidate_hash,
        created_by=admin_user.id,
    )
    run = OntologyTrialRun(
        id="promotion-fence-gap-run",
        ontology_id=oid,
        version_id=draft.id,
        revision=0,
        snapshot_hash=candidate_hash,
        base_release_id=current.id,
        status="passed",
        dataset_versions=[],
        result_json={
            "counts": {"objects": 0, "links": 0},
            "errors": [],
        },
        impact_hash=report["impactHash"],
        created_by=admin_user.id,
        completed_at=datetime.now(timezone.utc),
    )
    db.add_all([draft, run])
    db.commit()
    version_count = db.query(OntologyVersion).filter_by(
        ontology_id=oid).count()

    observations = []

    def compensate(session, ontology_id):
        persisted = session.query(OntologyProject).filter_by(
            id=ontology_id).one()
        observations.append((
            persisted.current_release_id,
            persisted.projection_status,
        ))
        return {
            "ready": compensation_ready,
            "neo4j": "ok" if compensation_ready else "error",
            **({} if compensation_ready else {
                "error": "injected promotion compensation failure",
            }),
        }

    def fail_after_fence(*_args, **_kwargs):
        raise RuntimeError("injected promotion post-fence preparation failure")

    monkeypatch.setattr(version_router.settings, "environment", "development")
    # Keep this test focused on the durable post-fence boundary rather than the
    # normal trial mapping gate; the injected failure occurs before restoration.
    monkeypatch.setattr(
        version_router, "validate_release_mapping_contract", lambda _snap: [])
    monkeypatch.setattr(
        version_router, "_verify_trial_dataset_pins", lambda *_args: [])
    monkeypatch.setattr(
        version_router, "_runtime_state_conflicts",
        lambda *_args, **_kwargs: {"totalCount": 0, "items": []},
    )
    monkeypatch.setattr(
        version_router, "_next_release_activation_number", fail_after_fence)
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections", compensate)

    response = client.post(
        f"{_versions(oid)}/{draft.id}/promote",
        headers=auth_headers,
        json={
            "trialRunId": run.id,
            "impactHash": report["impactHash"],
        },
    )

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "promotion_failed"
    assert detail["compensation"]["ready"] is compensation_ready
    assert observations == [(current_id, "projecting")]
    db.expire_all()
    persisted = db.query(OntologyProject).filter_by(id=oid).one()
    assert persisted.current_release_id == current_id
    assert persisted.projection_status == (
        "ready" if compensation_ready else "failed")
    assert db.query(OntologyVersion).filter_by(
        ontology_id=oid).count() == version_count


def test_rollback_post_fence_preparation_failure_is_compensated(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    target, current, object_type = _rollback_release_pair(
        db, oid, prefix="rollback-fence-gap")
    version_count = db.query(OntologyVersion).filter_by(
        ontology_id=oid).count()
    observations = []

    def compensate(session, ontology_id):
        persisted = session.query(OntologyProject).filter_by(
            id=ontology_id).one()
        current_type = session.query(ObjectType).filter_by(
            id=object_type.id).one()
        observations.append((
            persisted.current_release_id,
            persisted.projection_status,
            current_type.display_name,
        ))
        return {"ready": True, "neo4j": "ok"}

    def fail_after_fence(*_args, **_kwargs):
        raise RuntimeError("injected rollback post-fence preparation failure")

    monkeypatch.setattr(version_router.settings, "environment", "development")
    monkeypatch.setattr(
        version_router, "_next_release_activation_number", fail_after_fence)
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections", compensate)

    response = client.post(
        f"{_versions(oid)}/{target.id}/rollback", headers=auth_headers)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "rollback_restore_failed"
    assert detail["compensation"]["ready"] is True
    assert observations == [(current.id, "projecting", "当前订单")]
    db.expire_all()
    persisted = db.query(OntologyProject).filter_by(id=oid).one()
    assert persisted.current_release_id == current.id
    assert persisted.projection_status == "ready"
    assert db.query(ObjectType).filter_by(
        id=object_type.id).one().display_name == "当前订单"
    assert db.query(OntologyVersion).filter_by(
        ontology_id=oid).count() == version_count


@pytest.mark.parametrize("compensation_ready", [True, False])
def test_rollback_final_commit_failure_reprojects_durable_sql_before_ready(
        client, auth_headers, ontology, db, monkeypatch, compensation_ready):
    """Candidate Neo4j must be compensated if the SQL activation rolls back."""
    oid = ontology["id"]
    target, current, object_type = _rollback_release_pair(
        db, oid, prefix="rollback-final-commit")
    version_count = db.query(OntologyVersion).filter_by(
        ontology_id=oid).count()
    projected_release = {"id": current.id}
    observations = []

    def rebuild(session, ontology_id):
        persisted = session.query(OntologyProject).filter_by(
            id=ontology_id).one()
        current_type = session.query(ObjectType).filter_by(
            id=object_type.id).one()
        call_no = len(observations) + 1
        ready = True if call_no == 1 else compensation_ready
        observations.append((
            persisted.current_release_id,
            persisted.projection_status,
            current_type.display_name,
            ready,
        ))
        if ready:
            projected_release["id"] = persisted.current_release_id
        return {
            "ready": ready,
            "neo4j": "ok" if ready else "error",
            **({} if ready else {
                "error": "injected rollback compensation failure",
            }),
        }

    original_commit = db.commit
    commit_calls = 0

    def fail_candidate_commit():
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 2:
            raise RuntimeError("injected rollback activation commit failure")
        return original_commit()

    monkeypatch.setattr(version_router.settings, "environment", "development")
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections", rebuild)
    monkeypatch.setattr(db, "commit", fail_candidate_commit)

    response = client.post(
        f"{_versions(oid)}/{target.id}/rollback", headers=auth_headers)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "rollback_restore_failed"
    assert detail["compensation"]["ready"] is compensation_ready
    assert commit_calls == 3
    assert len(observations) == 2
    candidate_release_id, candidate_status, candidate_name, candidate_ready = (
        observations[0]
    )
    assert candidate_release_id != current.id
    assert (candidate_status, candidate_name, candidate_ready) == (
        "projecting", "历史订单", True)
    assert observations[1] == (
        current.id,
        "projecting",
        "当前订单",
        compensation_ready,
    )
    db.expire_all()
    persisted = db.query(OntologyProject).filter_by(id=oid).one()
    assert persisted.current_release_id == current.id
    assert persisted.projection_status == (
        "ready" if compensation_ready else "failed")
    assert projected_release["id"] == (
        current.id if compensation_ready else candidate_release_id)
    assert db.query(ObjectType).filter_by(
        id=object_type.id).one().display_name == "当前订单"
    assert db.query(OntologyVersion).filter_by(
        ontology_id=oid).count() == version_count


def test_production_publish_requires_current_approved_applied_mapping(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    object_type = _object_type(oid)
    instance = ObjectInstance(
        id="order-prod", ontology_id=oid, object_type_id=object_type.id,
        properties={"code": "O-1"}, computed={}, source="pipeline",
        external_id="lake-order-prod",
    )
    db.add_all([object_type, instance]); db.commit()
    monkeypatch.setattr(version_router.settings, "environment", "production")
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections",
        lambda *_args, **_kwargs: {
            "ready": True, "neo4j": "ok",
        })

    codes = {
        item["code"] for item in version_router._release_errors(db, oid)}
    assert "production_mapping_required" in codes

    dataset = Dataset(
        id="prod-dataset", name=f"prod-{uuid.uuid4().hex}", kind="curated")
    version_1 = DatasetVersion(
        id="dataset-v1", dataset_id=dataset.id, version_no=1,
        storage_uri="s3://curated/prod-v1.csv", checksum="1" * 64)
    version_2 = DatasetVersion(
        id="dataset-v2", dataset_id=dataset.id, version_no=2,
        storage_uri="s3://curated/prod-v2.csv", checksum="2" * 64)
    dataset.latest_version_id = version_2.id
    mapping = OntologyMapping(
        id="prod-mapping", ontology_id=oid, curated_dataset_id=dataset.id,
        entity_class="Order", target_object_type_id=object_type.id,
        field_mapping={"__applied_dataset_version_id__": version_1.id},
        status="draft",
    )
    db.add_all([dataset, version_1, version_2, mapping]); db.commit()

    codes = {
        item["code"] for item in version_router._release_errors(db, oid)}
    assert {
        "mapping_not_applied",
        "latest_dataset_version_not_approved",
        "mapping_applied_version_stale",
    } <= codes

    mapping.status = "applied"
    mapping.field_mapping = {"__applied_dataset_version_id__": version_2.id}
    db.commit()
    codes = {
        item["code"] for item in version_router._release_errors(db, oid)}
    assert codes == {
        "latest_dataset_version_not_approved",
        "mapping_curated_automation_not_subscribed",
    }

    db.add(CuratedReview(
        id="review-v2", curated_dataset_id=dataset.id,
        dataset_version_id=version_2.id, status="approved",
    ))
    db.commit()
    codes = {
        item["code"] for item in version_router._release_errors(db, oid)}
    assert codes == {"mapping_curated_automation_not_subscribed"}

    # 断点2：curated 映射发布前必须订阅“审批通过后自动灌入”
    mapping.field_mapping = {
        "__applied_dataset_version_id__": version_2.id,
        "__auto_apply_on_review__": True,
    }
    db.commit()
    assert version_router._release_errors(db, oid) == []


def test_production_publish_accepts_governed_manual_version_subscription(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    object_type = _object_type(oid, with_contract=True)
    instance = ObjectInstance(
        id="manual-prod-instance", ontology_id=oid,
        object_type_id=object_type.id,
        properties={"code": "M-1"}, computed={}, source="pipeline",
        external_id="manual-row-M-1",
    )
    dataset = Dataset(
        id="manual-prod-dataset", name="人工生产台账", kind="structured",
        schema_json={
            "origin": "manual", "primary_key": "code", "pk_source": "manual",
            "columns": ["code"],
            "columns_typed": [{
                "name": "code", "type": "string", "nullable": False,
            }],
            "types_source": "declared",
        },
    )
    version = DatasetVersion(
        id="manual-prod-v1", dataset_id=dataset.id, version_no=1,
        storage_uri="s3://manual/prod-v1.csv", checksum="a" * 64,
    )
    dataset.latest_version_id = version.id
    mapping = OntologyMapping(
        id="manual-prod-mapping", ontology_id=oid,
        curated_dataset_id=dataset.id, entity_class="Order",
        target_object_type_id=object_type.id, status="applied",
        field_mapping={
            "code": "code", "__primary_key__": "code",
            "__pk_source__": "lake",
            "__auto_apply_on_version__": True,
            "__applied_dataset_version_id__": version.id,
        },
    )
    db.add_all([object_type, instance, dataset, version, mapping])
    db.commit()

    monkeypatch.setattr(version_router.settings, "environment", "production")
    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections",
        lambda *_args, **_kwargs: {
            "ready": True, "neo4j": "ok",
        },
    )

    assert version_router._release_errors(db, oid) == []


def test_retired_publish_endpoint_never_rebuilds_or_switches_projection(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    db.add(_object_type(oid))
    db.commit()
    monkeypatch.setattr(version_router.settings, "environment", "production")
    calls = []

    def unexpected_rebuild(*_args, **_kwargs):
        calls.append(True)
        return {
            "ready": False, "neo4j": "error",
        }

    monkeypatch.setattr(
        version_router, "_rebuild_required_query_projections",
        unexpected_rebuild)
    project = db.query(OntologyProject).filter_by(id=oid).one()
    release_id = project.current_release_id

    response = client.post(_versions(oid), headers=auth_headers, json={})

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "legacy_publish_endpoint_retired"
    assert calls == []
    db.refresh(project)
    assert project.current_release_id == release_id


def test_release_gate_blocks_dormant_action_but_exempts_approval(db, ontology):
    """发布门保持严格：无启用副作用规则的动作阻断发布；
    requiresApproval 动作按运行时审批挂起语义豁免同一条目。"""
    oid = ontology["id"]
    object_type = _object_type(oid, item_id="ot-dormant", with_contract=True)
    action = ActionType(
        id="act-dormant", ontology_id=oid,
        name="dormant_action", display_name="休眠动作",
        object_type_id=object_type.id, parameters=[],
        rules=[{
            "id": "rule-pending", "type": "validation",
            "name": "待形式化: 金额约束", "enabled": False, "order": 0,
            "config": {"type": "validation", "condition": "",
                       "errorMessage": "金额必须大于 0"},
        }],
        requires_approval=False,
    )
    db.add_all([object_type, action])
    db.commit()

    codes = {item["code"] for item in version_router._release_errors(db, oid)}
    assert "invalid_action_definition" in codes
    assert any(
        "没有启用的可执行副作用规则" in item["message"]
        for item in version_router._release_errors(db, oid)
    )

    action.requires_approval = True
    db.commit()
    codes = {item["code"] for item in version_router._release_errors(db, oid)}
    assert "invalid_action_definition" not in codes
