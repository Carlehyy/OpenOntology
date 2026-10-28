"""远程绑定走版本正门：试跑、影响分析、发布门与生产门的远程分支测试。

本地 pins/counts 口径不变；远程绑定只做元数据/受控读验证，且必须显式
标注「未物化、非全量质量」，不得冒充本地全量质量验证。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.models.ontology import OntologyProject
from app.models.ontology_formal import ObjectType, LinkType
from app.models.ontology_version import OntologyTrialRun, OntologyVersion
from app.models.v2.dataset import Dataset
from app.models.v2.mapping import OntologyMapping
from app.ontologies.versions.evolution_service import (
    complete_snapshot,
    impact_report,
    materialize_trial,
    validate_manual_mapping_trial_contract,
    validate_release_mapping_contract,
)
from app.ontologies.versions.promotion_service import _remote_release_gate_errors
from app.ontologies.versions.snapshot_contract import snapshot_hash
from app.ontologies.mappings.remote_binding import (
    BINDING_MODE_KEY,
    REMOTE_DESCRIPTOR_KEY,
    RemoteBindingError,
    clear_remote_binding_validator,
    set_remote_binding_validator,
)
from tests.ontologies.test_remote_binding import (
    DESCRIPTOR,
    StubValidator,
    _mapping_entry,
    _snapshot,
)


@pytest.fixture
def stub_port():
    validator = StubValidator()
    set_remote_binding_validator(validator)
    yield validator
    clear_remote_binding_validator()


def _codes(errors) -> set[str]:
    return {item["code"] for item in errors}


# ── 发布映射契约（release mapping contract） ───────────────────────


def test_release_contract_accepts_valid_remote_binding():
    errors = validate_release_mapping_contract(_snapshot())
    assert _codes(errors) == set()


def test_release_contract_rejects_remote_contract_violations():
    # 主键契约不一致。
    snap = _snapshot()
    snap["mappings"][0]["fieldMapping"]["__primary_key__"] = "amount"
    assert "remote_pk_contract_mismatch" in _codes(
        validate_release_mapping_contract(snap))

    # 远程映射携带自动灌入订阅。
    snap = _snapshot()
    snap["mappings"][0]["fieldMapping"]["__auto_apply_on_review__"] = True
    assert "remote_auto_apply_forbidden" in _codes(
        validate_release_mapping_contract(snap))

    # 描述符缺失（仅保留键，无 __remote__）。
    snap = _snapshot()
    snap["mappings"][0] = {
        "id": "m-remote", "curatedDatasetId": "ds-remote-1",
        "entityClass": "RemoteOrder", "targetObjectTypeId": "ot-remote",
        "fieldMapping": {"order_code": "code",
                         "__primary_key__": "order_code",
                         BINDING_MODE_KEY: "remote"},
    }
    assert "remote_descriptor_missing" in _codes(
        validate_release_mapping_contract(snap))

    # 同一 ObjectType 本地 + 远程混绑。
    snap = _snapshot()
    snap["mappings"].append({
        "id": "m-local", "curatedDatasetId": "ds-local",
        "entityClass": "RemoteOrder", "targetObjectTypeId": "ot-remote",
        "fieldMapping": {"x": "code", "__primary_key__": "x"},
    })
    assert "object_type_binding_mode_conflict" in _codes(
        validate_release_mapping_contract(snap))

    # 本地 link 引用远程绑定数据集。
    snap = _snapshot()
    snap["linkMappings"] = [{
        "id": "lk-1", "srcDatasetId": "ds-remote-1",
        "tgtDatasetId": "ds-other", "relationType": "r",
        "srcKey": "order_code", "tgtKey": "k",
        "fieldMapping": {},
    }]
    assert "link_dataset_remote_bound" in _codes(
        validate_release_mapping_contract(snap))


def test_release_contract_remote_link_rules():
    link_type = {
        "id": "lt-1", "name": "contains", "displayName": "包含",
        "sourceObjectTypeId": "ot-remote", "targetObjectTypeId": "ot-remote",
        "cardinality": "one-to-many", "properties": [],
    }
    base = dict(_snapshot(), linkTypes=[link_type])

    # 连接表（胖边）禁止。
    fat = dict(base)
    fat["linkMappings"] = [{
        "id": "lk-1", "srcDatasetId": "ds-remote-1",
        "tgtDatasetId": "ds-remote-1", "relationType": "contains",
        "srcKey": "order_code", "tgtKey": "order_code",
        "linkTypeId": "lt-1", "edgeDatasetId": "ds-edge",
        "fieldMapping": {BINDING_MODE_KEY: "remote"},
        "bindingMode": "remote",
    }]
    assert "remote_link_edge_forbidden" in _codes(
        validate_release_mapping_contract(fat))

    # 端点键不在冻结列清单。
    bad_key = dict(base)
    bad_key["linkMappings"] = [{
        "id": "lk-1", "srcDatasetId": "ds-remote-1",
        "tgtDatasetId": "ds-remote-1", "relationType": "contains",
        "srcKey": "order_code", "tgtKey": "ghost_col",
        "linkTypeId": "lt-1", "fieldMapping": {},
        "bindingMode": "remote",
    }]
    assert "remote_link_key_missing" in _codes(
        validate_release_mapping_contract(bad_key))

    # 合法远程关系定义（两端均远程绑定）通过。
    ok = dict(base)
    ok["linkMappings"] = [{
        "id": "lk-1", "srcDatasetId": "ds-remote-1",
        "tgtDatasetId": "ds-remote-1", "relationType": "contains",
        "srcKey": "order_code", "tgtKey": "order_code",
        "linkTypeId": "lt-1", "fieldMapping": {},
        "bindingMode": "remote",
    }]
    assert _codes(validate_release_mapping_contract(ok)) == set()


# ── 人工数据自动灌入契约：远程排除 ────────────────────────────────


def test_manual_mapping_trial_contract_skips_remote(db, admin_user):
    dataset = Dataset(id="ds-manual-remote", name="人工远程消费表",
                      kind="manual",
                      schema_json={"primary_key": ["k"]})
    db.add(dataset)
    db.commit()
    snap = _snapshot()
    snap["mappings"][0]["curatedDatasetId"] = dataset.id
    snap["mappings"][0]["remote"]["datasetId"] = dataset.id
    # 远程绑定：不要求订阅与精确 pin。
    assert validate_manual_mapping_trial_contract(db, snap, []) == []

    # 同一人工数据集的本地映射：仍要求订阅 + pin（本地口径不变）。
    local_snap = _snapshot()
    local_snap["mappings"] = [{
        "id": "m-local", "curatedDatasetId": dataset.id,
        "entityClass": "LocalOrder",
        "fieldMapping": {"k": "code", "__primary_key__": "k"},
    }]
    errors = validate_manual_mapping_trial_contract(db, local_snap, [])
    assert "mapping_manual_automation_not_subscribed" in _codes(errors)
    assert "mapping_trial_dataset_pin_missing" in _codes(errors)


# ── 影响分析：远程绑定显式入报告 ──────────────────────────────────


def test_impact_report_surfaces_remote_binding_changes():
    base = _snapshot()
    base["mappings"] = []
    report = impact_report(base, _snapshot())
    remote_changes = report["remoteBindings"]
    assert len(remote_changes) == 1
    assert remote_changes[0]["change"] == "added"
    assert remote_changes[0]["assetId"] == "asset-1"
    assert remote_changes[0]["schemaFingerprint"] == "fingerprint-1"
    # 同输入 → 稳定哈希（试跑/发布两侧一致比较）。
    assert impact_report(base, _snapshot())["impactHash"] == report[
        "impactHash"]

    unchanged = impact_report(_snapshot(), _snapshot())
    assert unchanged["remoteBindings"] == []


# ── 试跑远程分支 ───────────────────────────────────────────────────


def _trial_setup(db, admin_user):
    project = OntologyProject(id=str(uuid.uuid4()),
                              name=f"试跑-{uuid.uuid4().hex[:6]}",
                              domain="test", created_by=admin_user.id)
    db.add(project)
    db.flush()
    object_type = ObjectType(
        id="ot-remote", ontology_id=project.id, name="RemoteOrder",
        display_name="远程订单", primary_key="p-code", properties=[
            {"id": "p-code", "name": "code", "displayName": "编号",
             "type": "string", "required": True},
            {"id": "p-amount", "name": "amount", "displayName": "金额",
             "type": "number"},
        ], interfaces=[], position_x=0, position_y=0)
    mapping = OntologyMapping(
        id="m-remote", ontology_id=project.id,
        curated_dataset_id="ds-remote-1", entity_class="RemoteOrder",
        target_object_type_id="ot-remote",
        field_mapping={
            "order_code": "code", "amount": "amount",
            "__primary_key__": "order_code", "__pk_source__": "remote",
            BINDING_MODE_KEY: "remote",
            REMOTE_DESCRIPTOR_KEY: DESCRIPTOR,
        }, status="draft", confidence=1.0)
    db.add_all([object_type, mapping])
    db.commit()
    from app.ontologies.versions.release_service import (
        collect_publishable_snapshot,
    )
    snapshot = collect_publishable_snapshot(db, project.id)
    version = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=project.id,
        version_number="v0.1", node_kind="draft",
        lifecycle_status="editing", revision=0,
        snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
        created_by=admin_user.id)
    db.add(version)
    db.flush()
    version.base_release_id = version.id
    run = OntologyTrialRun(
        id=str(uuid.uuid4()), ontology_id=project.id, version_id=version.id,
        revision=0, snapshot_hash=version.snapshot_hash,
        base_release_id=version.id, status="running",
        dataset_versions=[], result_json={},
        created_by=admin_user.id)
    db.add(run)
    db.commit()
    return project, version, run, snapshot


def test_trial_remote_branch_verifies_without_materialization(
        db, admin_user, stub_port):
    project, version, run, snapshot = _trial_setup(db, admin_user)
    materialize_trial(db, run, complete_snapshot(snapshot))
    db.commit()
    db.refresh(run)
    assert run.status == "passed", run.result_json
    result = run.result_json
    # 本地口径：远程绑定不产生对象/关系/数据集 pin。
    assert result["counts"]["objects"] == 0
    assert result["counts"]["links"] == 0
    assert result["counts"]["datasets"] == 0
    assert run.dataset_versions == []
    check = result["remoteBindings"]["m-remote"]
    assert check["status"] == "verified"
    assert check["materialized"] is False
    assert check["schemaFingerprint"] == "fingerprint-1"
    # 受控读样本行数有界；记录中不含任何行值。
    assert check["sampleRowCount"] == 1
    assert stub_port.sample_calls and stub_port.sample_calls[0]["limit"] <= 20
    serialized = str(result)
    assert "A1" not in serialized
    # 显式 warning：远程验证不冒充全量质量。
    assert any(
        item["code"] == "remote_binding_not_materialized"
        for item in result["warnings"])


def test_trial_remote_branch_fails_closed_on_drift_and_missing_port(
        db, admin_user):
    project, version, run, snapshot = _trial_setup(db, admin_user)

    # 端口未装配 → fail-closed。
    clear_remote_binding_validator()
    materialize_trial(db, run, complete_snapshot(snapshot))
    db.commit()
    db.refresh(run)
    assert run.status == "failed"
    assert "remote_validator_unavailable" in _codes(
        run.result_json["errors"])

    # schema drift → 显式失败。
    drifted = StubValidator(descriptor={
        **DESCRIPTOR, "schemaFingerprint": "fingerprint-drifted"})
    set_remote_binding_validator(drifted)
    run2 = OntologyTrialRun(
        id=str(uuid.uuid4()), ontology_id=project.id,
        version_id=version.id, revision=0,
        snapshot_hash=version.snapshot_hash,
        base_release_id=version.id, status="running",
        dataset_versions=[], result_json={}, created_by=admin_user.id)
    db.add(run2)
    db.commit()
    materialize_trial(db, run2, complete_snapshot(snapshot))
    db.commit()
    db.refresh(run2)
    assert run2.status == "failed"
    assert "remote_binding_schema_drift" in _codes(run2.result_json["errors"])
    clear_remote_binding_validator()


# ── 发布门远程分支（promotion real branch） ───────────────────────


def test_promotion_remote_gate_requires_frozen_trial_checks(
        db, admin_user, stub_port):
    project, version, run, snapshot = _trial_setup(db, admin_user)
    snap = complete_snapshot(snapshot)

    # 没有冻结的试跑验证记录 → 拒绝。
    errors = _remote_release_gate_errors(db, snap, run, admin_user)
    assert "remote_trial_check_missing" in _codes(errors)

    # 记录与快照指纹不一致 → 拒绝。
    run.result_json = {"remoteBindings": {"m-remote": {
        "status": "verified", "schemaFingerprint": "fingerprint-stale",
    }}}
    errors = _remote_release_gate_errors(db, snap, run, admin_user)
    assert "remote_trial_check_stale" in _codes(errors)

    # 齐全且指纹一致 + 活元数据重验通过 → 通过。
    run.result_json = {"remoteBindings": {"m-remote": {
        "status": "verified", "schemaFingerprint": "fingerprint-1",
    }}}
    assert _remote_release_gate_errors(db, snap, run, admin_user) == []

    # 发布时 schema drift → 显式拒绝。
    drifted = StubValidator(descriptor={
        **DESCRIPTOR, "schemaFingerprint": "fingerprint-late-drift"})
    set_remote_binding_validator(drifted)
    errors = _remote_release_gate_errors(db, snap, run, admin_user)
    assert "remote_binding_schema_drift" in _codes(errors)
    clear_remote_binding_validator()


def test_promotion_remote_gate_covers_remote_links(
        db, admin_user, stub_port):
    project, version, run, snapshot = _trial_setup(db, admin_user)
    link_type = LinkType(
        id="lt-1", ontology_id=project.id, name="contains",
        display_name="包含", source_object_type_id="ot-remote",
        target_object_type_id="ot-remote", cardinality="one-to-many",
        properties=[])
    db.add(link_type)
    db.commit()
    snap = complete_snapshot(snapshot)
    snap["linkMappings"] = [{
        "id": "lk-remote", "srcDatasetId": "ds-remote-1",
        "tgtDatasetId": "ds-remote-1", "relationType": "contains",
        "srcKey": "order_code", "tgtKey": "order_code",
        "linkTypeId": "lt-1", "fieldMapping": {
            BINDING_MODE_KEY: "remote"},
        "bindingMode": "remote",
    }]
    run.result_json = {"remoteBindings": {"m-remote": {
        "status": "verified", "schemaFingerprint": "fingerprint-1",
    }}}
    errors = _remote_release_gate_errors(db, snap, run, admin_user)
    assert "remote_trial_check_missing" in _codes(errors)

    run.result_json = {"remoteBindings": {
        "m-remote": {"status": "verified",
                     "schemaFingerprint": "fingerprint-1"},
        "lk-remote": {"status": "verified"},
    }}
    assert _remote_release_gate_errors(db, snap, run, admin_user) == []


# ── 生产门（production gate）远程排除 ─────────────────────────────


def test_production_gate_skips_local_version_checks_for_remote(
        db, admin_user, stub_port):
    from app.ontologies.versions.release_gate_service import (
        validate_production_mappings,
    )
    project = OntologyProject(id=str(uuid.uuid4()),
                              name=f"生产-{uuid.uuid4().hex[:6]}",
                              domain="test", created_by=admin_user.id)
    db.add(project)
    object_type = ObjectType(
        id="ot-remote", ontology_id=project.id, name="RemoteOrder",
        display_name="远程订单", primary_key="p-code", properties=[
            {"id": "p-code", "name": "code", "displayName": "编号",
             "type": "string", "required": True},
        ], interfaces=[], position_x=0, position_y=0)
    # 远程数据集没有任何本地 DatasetVersion。
    dataset = Dataset(id="ds-remote-1", name="远程订单表",
                      kind="integrated",
                      schema_json={"primary_key": ["order_code"]})
    mapping = OntologyMapping(
        id="m-remote", ontology_id=project.id,
        curated_dataset_id=dataset.id, entity_class="RemoteOrder",
        target_object_type_id="ot-remote",
        field_mapping={
            "order_code": "code",
            "__primary_key__": "order_code", "__pk_source__": "remote",
            BINDING_MODE_KEY: "remote",
            REMOTE_DESCRIPTOR_KEY: DESCRIPTOR,
        }, status="applied", confidence=1.0)
    db.add_all([project, object_type, dataset, mapping])
    db.commit()

    errors = validate_production_mappings(
        db, project.id, [mapping], [], [], [object_type])
    assert _codes(errors) == set()

    # 描述符缺失的远程映射在生产门被显式拦截。
    broken = OntologyMapping(
        id="m-broken", ontology_id=project.id,
        curated_dataset_id=dataset.id, entity_class="BrokenRemote",
        target_object_type_id="ot-remote",
        field_mapping={"order_code": "code", "__primary_key__": "order_code",
                       BINDING_MODE_KEY: "remote"},
        status="applied", confidence=1.0)
    db.add(broken)
    db.commit()
    errors = validate_production_mappings(
        db, project.id, [mapping, broken], [], [], [object_type])
    assert "remote_descriptor_missing" in _codes(errors)
