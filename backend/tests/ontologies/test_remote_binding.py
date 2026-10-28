"""远程联邦绑定契约、定义期门禁与本地物化隔离的单元测试。

远程绑定只有一个定义来源：对象映射 field_mapping 的保留键
（__binding_mode__/__remote__）与快照顶层镜像（bindingMode/remote）。
本文件覆盖：描述符归一化、发布读取端口、validator 端口注册与冻结/重验、
workspace 服务端冻结、live CRUD 门禁（auto-apply 拒绝、混绑拒绝、
本地 link 引用远程数据集拒绝）、以及 apply/build_all 的显式隔离。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.ontology import OntologyProject
from app.models.ontology_formal import ObjectType, LinkType
from app.models.v2.dataset import Dataset
from app.models.v2.mapping import OntologyMapping, OntologyLinkMapping
from app.ontologies.mappings import entity_mapping_workflow as _entity_workflow
from app.ontologies.mappings import link_mapping_workflow as _link_workflow
from app.ontologies.mappings import automation_subscription
from app.ontologies.mappings import router as mapping_router
from app.ontologies.mappings.mapping_service import (
    MappingApplyError,
    MappingService,
    MappingSourceError,
)
from app.ontologies.mappings.remote_binding import (
    BINDING_MODE_LOCAL,
    BINDING_MODE_REMOTE,
    BINDING_MODE_KEY,
    REMOTE_DESCRIPTOR_KEY,
    RemoteBindingAmbiguous,
    RemoteBindingError,
    RemoteBindingPortError,
    clear_remote_binding_validator,
    freeze_remote_descriptor,
    get_remote_binding,
    get_remote_link_bindings,
    mapping_binding_mode,
    normalize_binding_mode,
    normalize_remote_descriptor,
    published_mapping_contract,
    remote_binding_validator,
    remote_binding_http_error,
    remote_field_mapping_errors,
    set_remote_binding_validator,
    verify_remote_binding,
)
from app.ontologies.versions.snapshot_contract import snapshot_hash


DESCRIPTOR = {
    "assetId": "asset-1",
    "datasetId": "ds-remote-1",
    "connectionId": "conn-1",
    "schemaName": "sales",
    "tableName": "orders",
    "columns": [
        {"name": "order_code", "type": "string", "nullable": False,
         "length": 64},
        {"name": "amount", "type": "decimal", "nullable": True,
         "precision": 18, "scale": 4},
        {"name": "created_at", "type": "timestamp", "nullable": True,
         "timezone": False},
    ],
    "primaryKey": ["order_code"],
    "schemaFingerprint": "fingerprint-1",
}


class StubValidator:
    """accepts/sample 端口桩：返回冻结元数据与受控读样本，不触真实源。"""

    def __init__(self, descriptor=None, rows=None, accept_error=None):
        self.descriptor = descriptor or DESCRIPTOR
        self.rows = rows or [{"order_code": "A1", "amount": "1.5",
                              "created_at": "2026-01-01T00:00:00"}]
        self.accept_error = accept_error
        self.accept_calls: list[dict] = []
        self.sample_calls: list[dict] = []

    def accepts(self, db, user, descriptor):
        self.accept_calls.append(dict(descriptor or {}))
        if self.accept_error is not None:
            raise self.accept_error
        return dict(self.descriptor)

    def sample(self, db, user, descriptor, *, limit):
        self.sample_calls.append({"limit": limit})
        return list(self.rows[:limit])


@pytest.fixture
def stub_port():
    validator = StubValidator()
    set_remote_binding_validator(validator)
    yield validator
    clear_remote_binding_validator()


def _mapping_entry(**overrides):
    entry = {
        "id": "m-remote",
        "curatedDatasetId": "ds-remote-1",
        "entityClass": "RemoteOrder",
        "fieldMapping": {
            "order_code": "code", "amount": "amount",
            "__primary_key__": "order_code",
        },
        "targetObjectTypeId": "ot-remote",
        "confidence": 1.0,
        "bindingMode": "remote",
        "remote": dict(DESCRIPTOR),
    }
    entry.update(overrides)
    return entry


def _snapshot(**overrides):
    snap = {
        "objectTypes": [{
            "id": "ot-remote", "name": "RemoteOrder",
            "displayName": "远程订单", "properties": [
                {"id": "p-code", "name": "code", "displayName": "编号",
                 "type": "string", "required": True},
                {"id": "p-amount", "name": "amount", "displayName": "金额",
                 "type": "number"},
            ],
            "primaryKey": "p-code",
        }],
        "linkTypes": [], "actions": [], "functions": [], "sentinels": [],
        "mappings": [_mapping_entry()], "linkMappings": [],
    }
    snap.update(overrides)
    return snap


# ── 描述符与绑定模式归一化 ─────────────────────────────────────────


def test_normalize_binding_mode():
    assert normalize_binding_mode(None) == BINDING_MODE_LOCAL
    assert normalize_binding_mode("") == BINDING_MODE_LOCAL
    assert normalize_binding_mode("remote") == BINDING_MODE_REMOTE
    with pytest.raises(RemoteBindingError) as exc:
        normalize_binding_mode("federated")
    assert exc.value.code == "binding_mode_invalid"


def test_descriptor_normalization_preserves_full_column_spec():
    frozen = normalize_remote_descriptor(DESCRIPTOR)
    assert frozen["columns"][1]["precision"] == 18
    assert frozen["columns"][1]["scale"] == 4
    assert frozen["columns"][2]["timezone"] is False
    assert frozen["primaryKey"] == ["order_code"]

    with pytest.raises(RemoteBindingError) as missing:
        normalize_remote_descriptor({k: v for k, v in DESCRIPTOR.items()
                                     if k != "schemaFingerprint"})
    assert missing.value.code == "remote_descriptor_invalid"

    with pytest.raises(RemoteBindingError) as bad_pk:
        normalize_remote_descriptor({**DESCRIPTOR, "primaryKey": ["ghost"]})
    assert bad_pk.value.code == "remote_descriptor_invalid"

    with pytest.raises(RemoteBindingError) as dup:
        normalize_remote_descriptor({
            **DESCRIPTOR,
            "columns": DESCRIPTOR["columns"] + [DESCRIPTOR["columns"][0]],
        })
    assert dup.value.code == "remote_descriptor_invalid"


def test_remote_field_mapping_errors_contract():
    field_mapping = {"order_code": "code", "amount": "amount",
                     "__primary_key__": "order_code"}
    assert remote_field_mapping_errors(field_mapping, DESCRIPTOR) == []

    pk_mismatch = remote_field_mapping_errors(
        {**field_mapping, "__primary_key__": "amount"}, DESCRIPTOR)
    assert [item["code"] for item in pk_mismatch] == [
        "remote_pk_contract_mismatch"]

    missing = remote_field_mapping_errors(
        {**field_mapping, "ghost_col": "name"}, DESCRIPTOR)
    assert [item["code"] for item in missing] == ["remote_mapping_column_missing"]

    unmapped_pk = remote_field_mapping_errors(
        {"amount": "amount", "__primary_key__": "order_code"}, DESCRIPTOR)
    assert [item["code"] for item in unmapped_pk] == ["remote_pk_not_mapped"]


# ── 快照读取端口（bridge 契约） ────────────────────────────────────


def test_get_remote_binding_unique_and_ambiguous():
    snapshot = _snapshot()
    binding = get_remote_binding(snapshot, "ot-remote")
    assert binding is not None
    assert binding["mappingId"] == "m-remote"
    assert binding["assetId"] == "asset-1"
    assert binding["primaryKey"] == ["order_code"]
    assert binding["fieldMap"] == {"order_code": "code", "amount": "amount"}
    # 历史快照（无 bindingMode/remote 键）一律视为本地绑定。
    legacy = _snapshot()
    legacy["mappings"] = [{
        "id": "m-legacy", "curatedDatasetId": "ds-x",
        "entityClass": "Legacy", "fieldMapping": {"a": "b"},
    }]
    assert get_remote_binding(legacy, "ot-remote") is None

    ambiguous = _snapshot()
    second = _mapping_entry(id="m-remote-2")
    ambiguous["mappings"] = [_mapping_entry(), second]
    with pytest.raises(RemoteBindingAmbiguous):
        get_remote_binding(ambiguous, "ot-remote")


def test_get_remote_link_bindings_and_fingerprint_mismatch_entry():
    snapshot = _snapshot(linkMappings=[{
        "id": "lk-1", "srcDatasetId": "ds-remote-1",
        "tgtDatasetId": "ds-remote-2", "relationType": "contains",
        "srcKey": "order_code", "tgtKey": "order_code",
        "linkTypeId": "lt-1", "bindingMode": "remote",
        "fieldMapping": {BINDING_MODE_KEY: BINDING_MODE_REMOTE},
    }])
    links = get_remote_link_bindings(snapshot)
    assert len(links) == 1
    assert links[0]["srcKey"] == "order_code"
    assert get_remote_link_bindings(snapshot, "lt-1") == links
    assert get_remote_link_bindings(snapshot, "lt-other") == []


def test_published_mapping_contract_reads_release_snapshot(db, admin_user):
    project = OntologyProject(id=str(uuid.uuid4()), name="远程本体",
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
            BINDING_MODE_KEY: BINDING_MODE_REMOTE,
            REMOTE_DESCRIPTOR_KEY: DESCRIPTOR,
        }, status="applied", confidence=1.0)
    db.add_all([object_type, mapping])
    db.flush()
    from app.ontologies.versions.release_service import (
        collect_publishable_snapshot,
    )
    snapshot = collect_publishable_snapshot(db, project.id)
    entry = snapshot["mappings"][0]
    assert entry["bindingMode"] == "remote"
    assert entry["remote"]["assetId"] == "asset-1"

    from app.models.ontology_version import OntologyVersion
    from datetime import datetime, timezone
    release = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=project.id, version_number="v1",
        node_kind="release", lifecycle_status="released",
        snapshot_formal=snapshot, snapshot_hash=snapshot_hash(snapshot),
        published_at=datetime.now(timezone.utc),
        created_by=admin_user.id)
    db.add(release)
    db.flush()
    release.base_release_id = release.id
    project.current_release_id = release.id
    db.commit()

    contract = published_mapping_contract(db, project.id)
    assert contract["releaseId"] == release.id
    remote_entries = [item for item in contract["mappings"]
                      if item["bindingMode"] == "remote"]
    assert len(remote_entries) == 1
    assert remote_entries[0]["fieldMap"]["order_code"] == "code"
    assert remote_entries[0]["primaryKeyColumns"] == ["order_code"]
    assert remote_entries[0]["remote"]["schemaFingerprint"] == "fingerprint-1"


# ── validator 端口注册与冻结/重验 ──────────────────────────────────


def test_validator_port_registry_fail_closed():
    clear_remote_binding_validator()
    with pytest.raises(RemoteBindingPortError) as exc:
        remote_binding_validator()
    assert exc.value.code == "remote_validator_unavailable"


def test_freeze_and_verify_remote_binding(db, admin_user, stub_port):
    frozen = freeze_remote_descriptor(db, admin_user, "ds-remote-1")
    assert frozen == DESCRIPTOR
    assert stub_port.accept_calls and stub_port.accept_calls[0] == {
        "datasetId": "ds-remote-1"}

    result = verify_remote_binding(
        db, admin_user, frozen, sample=True, sample_limit=5)
    assert result["sampleRowCount"] == 1
    assert result["live"]["schemaFingerprint"] == "fingerprint-1"

    # schema drift：活指纹与冻结描述符不一致 → 显式失败。
    drifted = StubValidator(descriptor={**DESCRIPTOR,
                                        "schemaFingerprint": "fingerprint-2"})
    set_remote_binding_validator(drifted)
    with pytest.raises(RemoteBindingError) as drift:
        verify_remote_binding(db, admin_user, frozen, sample=False)
    assert drift.value.code == "remote_binding_schema_drift"

    # 资产身份变化（重注册）→ 显式失败。
    reidentified = StubValidator(descriptor={**DESCRIPTOR, "assetId": "asset-9"})
    set_remote_binding_validator(reidentified)
    with pytest.raises(RemoteBindingError) as identity:
        verify_remote_binding(db, admin_user, frozen, sample=False)
    assert identity.value.code == "remote_binding_identity_changed"

    # 适配器 403 → 授权失败语义。
    forbidden = StubValidator(
        accept_error=HTTPException(403, detail={"code": "FORBIDDEN",
                                                "message": "no"}))
    set_remote_binding_validator(forbidden)
    with pytest.raises(RemoteBindingError) as unauthorized:
        freeze_remote_descriptor(db, admin_user, "ds-remote-1")
    assert unauthorized.value.code == "remote_asset_unauthorized"

    # HTTP 状态映射：未装配端口 → 503。
    clear_remote_binding_validator()
    error = remote_binding_http_error(RemoteBindingPortError(
        "remote_validator_unavailable", "x"))
    assert error.status_code == 503


# ── workspace 服务端冻结 ───────────────────────────────────────────


def test_workspace_freeze_discards_client_metadata(db, admin_user, stub_port):
    forged = dict(DESCRIPTOR)
    forged["schemaFingerprint"] = "client-forged"
    snap = _snapshot()
    snap["mappings"][0]["remote"] = forged
    snap["mappings"][0]["fieldMapping"].update({
        BINDING_MODE_KEY: BINDING_MODE_REMOTE,
        REMOTE_DESCRIPTOR_KEY: forged,
    })
    from app.ontologies.mappings.remote_binding import (
        normalize_workspace_remote_entries,
    )
    normalize_workspace_remote_entries(db, admin_user, snap)
    entry = snap["mappings"][0]
    # 客户端伪造元数据被丢弃，一律以服务端冻结为准。
    assert entry["remote"] == DESCRIPTOR
    assert entry["fieldMapping"][REMOTE_DESCRIPTOR_KEY] == DESCRIPTOR
    assert entry["fieldMapping"]["__primary_key__"] == "order_code"
    assert entry["fieldMapping"]["__pk_source__"] == "remote"


def test_workspace_freeze_rejects_remote_auto_apply(
        db, admin_user, stub_port):
    from app.ontologies.mappings.remote_binding import (
        normalize_workspace_remote_entries,
    )
    snap = _snapshot()
    snap["mappings"][0]["fieldMapping"]["__auto_apply_on_review__"] = True
    with pytest.raises(HTTPException) as exc:
        normalize_workspace_remote_entries(db, admin_user, snap)
    errors = exc.value.detail["errors"]
    assert errors[0]["code"] == "remote_auto_apply_forbidden"


def test_workspace_freeze_rejects_mixed_binding_and_local_links(
        db, admin_user, stub_port):
    from app.ontologies.mappings.remote_binding import (
        normalize_workspace_remote_entries,
    )
    snap = _snapshot()
    snap["mappings"].append({
        "id": "m-local", "curatedDatasetId": "ds-local",
        "entityClass": "RemoteOrder", "fieldMapping": {"x": "code"},
        "targetObjectTypeId": "ot-remote",
    })
    with pytest.raises(HTTPException) as mixed:
        normalize_workspace_remote_entries(db, admin_user, snap)
    assert mixed.value.detail["errors"][0][
        "code"] == "object_type_binding_mode_conflict"

    snap2 = _snapshot()
    snap2["linkMappings"] = [{
        "id": "lk-local", "srcDatasetId": "ds-remote-1",
        "tgtDatasetId": "ds-local", "relationType": "r",
        "srcKey": "order_code", "tgtKey": "k", "linkTypeId": "lt-1",
        "fieldMapping": {},
    }]
    with pytest.raises(HTTPException) as local_link:
        normalize_workspace_remote_entries(db, admin_user, snap2)
    assert local_link.value.detail["errors"][0][
        "code"] == "link_dataset_remote_bound"


def test_workspace_remote_link_definition_validates(db, admin_user, stub_port):
    from app.ontologies.mappings.remote_binding import (
        normalize_workspace_remote_entries,
    )
    snap = _snapshot()
    snap["linkTypes"] = [{
        "id": "lt-1", "name": "contains", "displayName": "包含",
        "sourceObjectTypeId": "ot-remote", "targetObjectTypeId": "ot-remote",
        "cardinality": "one-to-many", "properties": [],
    }]
    snap["linkMappings"] = [{
        "id": "lk-remote", "srcDatasetId": "ds-remote-1",
        "tgtDatasetId": "ds-remote-1", "relationType": "contains",
        "srcKey": "order_code", "tgtKey": "order_code",
        "linkTypeId": "lt-1", "bindingMode": "remote", "fieldMapping": {},
    }]
    normalize_workspace_remote_entries(db, admin_user, snap)
    assert snap["linkMappings"][0]["fieldMapping"][
        BINDING_MODE_KEY] == BINDING_MODE_REMOTE

    bad = _snapshot(linkTypes=snap["linkTypes"])
    bad["linkMappings"] = [dict(snap["linkMappings"][0],
                                tgtKey="ghost_col")]
    with pytest.raises(HTTPException) as key_missing:
        normalize_workspace_remote_entries(db, admin_user, bad)
    assert key_missing.value.detail["errors"][0][
        "code"] == "remote_link_key_missing"


def test_workspace_remote_to_local_switch_restores_lake_pk(
        db, admin_user, stub_port):
    dataset = Dataset(id="ds-remote-1", name="远程订单表",
                      kind="integrated",
                      schema_json={"primary_key": "order_code"})
    db.add(dataset)
    db.commit()
    from app.ontologies.mappings.remote_binding import (
        normalize_workspace_remote_entries,
    )
    snap = _snapshot()
    snap["mappings"][0]["bindingMode"] = "local"
    normalize_workspace_remote_entries(db, admin_user, snap)
    entry = snap["mappings"][0]
    assert entry["bindingMode"] == BINDING_MODE_LOCAL
    assert "remote" not in entry
    assert entry["fieldMapping"]["__primary_key__"] == "order_code"
    assert entry["fieldMapping"]["__pk_source__"] == "lake"
    assert BINDING_MODE_KEY not in entry["fieldMapping"]
    assert REMOTE_DESCRIPTOR_KEY not in entry["fieldMapping"]


def test_workspace_local_entries_untouched(db, admin_user, stub_port):
    from app.ontologies.mappings.remote_binding import (
        normalize_workspace_remote_entries,
    )
    snap = _snapshot()
    snap["mappings"] = [{
        "id": "m-local", "curatedDatasetId": "ds-local",
        "entityClass": "Local", "fieldMapping": {
            "a": "code", "__primary_key__": "a",
            "__auto_apply_on_version__": True,
        },
    }]
    before = {k: dict(v) if isinstance(v, dict) else v
              for k, v in snap["mappings"][0].items()}
    normalize_workspace_remote_entries(db, admin_user, snap)
    assert snap["mappings"][0] == before


# ── live CRUD 门禁 ─────────────────────────────────────────────────


def _draft_project(db, admin_user) -> OntologyProject:
    project = OntologyProject(id=str(uuid.uuid4()), name=f"本体-{uuid.uuid4().hex[:6]}",
                              domain="test", created_by=admin_user.id)
    db.add(project)
    db.commit()
    return project


def _object_type(db, project) -> ObjectType:
    object_type = ObjectType(
        id="ot-remote", ontology_id=project.id, name="RemoteOrder",
        display_name="远程订单", primary_key="p-code", properties=[
            {"id": "p-code", "name": "code", "displayName": "编号",
             "type": "string", "required": True},
            {"id": "p-amount", "name": "amount", "displayName": "金额",
             "type": "number"},
        ], interfaces=[], position_x=0, position_y=0)
    db.add(object_type)
    db.commit()
    return object_type


def _create_body(dataset_id, object_type, **overrides):
    return mapping_router.CreateMappingRequest(
        curated_dataset_id=dataset_id,
        entity_class="RemoteOrder",
        field_mapping={"order_code": "code", "amount": "amount"},
        target_object_type_id=object_type.id,
        binding_mode="remote",
        **overrides,
    )


def test_create_remote_mapping_freezes_server_descriptor(
        db, admin_user, stub_port):
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    result = _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    mapping = db.query(OntologyMapping).filter_by(
        id=result["mapping_id"]).one()
    assert mapping_binding_mode(mapping.field_mapping) == "remote"
    assert mapping.field_mapping[REMOTE_DESCRIPTOR_KEY] == DESCRIPTOR
    assert mapping.field_mapping["__primary_key__"] == "order_code"
    assert mapping.field_mapping["__pk_source__"] == "remote"

    # 幂等重放：同一定义再次创建返回同一映射。
    replay = _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    assert replay["mapping_id"] == mapping.id
    assert replay.get("idempotent_replay") is True


def test_create_remote_mapping_rejects_auto_apply_and_mixed(
        db, admin_user, stub_port):
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    with pytest.raises(HTTPException) as flags:
        _entity_workflow.create_mapping(
            db, project.id, _create_body("ds-remote-1", object_type,
                                         auto_apply_on_version=True),
            rules=mapping_router._entity_mapping_rules(), user=admin_user)
    assert flags.value.detail["code"] == "remote_auto_apply_forbidden"

    _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    # 同一 ObjectType 再挂本地绑定 → 混绑拒绝。
    with pytest.raises(HTTPException) as mixed:
        _entity_workflow.create_mapping(
            db, project.id,
            mapping_router.CreateMappingRequest(
                curated_dataset_id="ds-local", entity_class="RemoteOrder",
                field_mapping={"x": "code"},
                target_object_type_id=object_type.id),
            rules=mapping_router._entity_mapping_rules(), user=admin_user)
    assert mixed.value.detail["code"] == "object_type_binding_mode_conflict"


def test_create_remote_mapping_without_port_fails_closed(
        db, admin_user):
    clear_remote_binding_validator()
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    with pytest.raises(HTTPException) as exc:
        _entity_workflow.create_mapping(
            db, project.id, _create_body("ds-remote-1", object_type),
            rules=mapping_router._entity_mapping_rules(), user=admin_user)
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "remote_validator_unavailable"


def test_update_mapping_switches_remote_and_local(
        db, admin_user, stub_port):
    dataset = Dataset(id="ds-remote-1", name="远程订单表",
                      kind="integrated",
                      schema_json={"primary_key": "order_code",
                                   "columns_typed": [
                                       {"name": "order_code", "type": "string"},
                                       {"name": "amount", "type": "number"}]})
    db.add(dataset)
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    result = _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    mapping = db.query(OntologyMapping).filter_by(
        id=result["mapping_id"]).one()

    # remote 期间更新 field_mapping：对冻结列校验，非法列被拒。
    with pytest.raises(HTTPException) as bad_column:
        _entity_workflow.update_mapping(
            db, project.id, mapping.id,
            mapping_router.UpdateMappingRequest(
                field_mapping={"ghost": "code"}),
            rules=mapping_router._entity_mapping_rules(), user=admin_user)
    assert bad_column.value.detail["code"] in {
        "mapping_type_mismatch", "remote_binding_contract_failed"}

    # remote 上开自动灌入 → 拒绝。
    with pytest.raises(HTTPException) as flag:
        _entity_workflow.update_mapping(
            db, project.id, mapping.id,
            mapping_router.UpdateMappingRequest(
                auto_apply_on_version=True),
            rules=mapping_router._entity_mapping_rules(), user=admin_user)
    assert flag.value.detail["code"] == "remote_auto_apply_forbidden"

    # 显式重发 binding_mode=remote → 服务端重新冻结。
    refreshed = StubValidator(descriptor={
        **DESCRIPTOR, "schemaFingerprint": "fingerprint-refreshed"})
    set_remote_binding_validator(refreshed)
    _entity_workflow.update_mapping(
        db, project.id, mapping.id,
        mapping_router.UpdateMappingRequest(binding_mode="remote"),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    db.refresh(mapping)
    assert mapping.field_mapping[REMOTE_DESCRIPTOR_KEY][
        "schemaFingerprint"] == "fingerprint-refreshed"

    # remote → local：恢复湖主键契约并剥离冻结键。
    _entity_workflow.update_mapping(
        db, project.id, mapping.id,
        mapping_router.UpdateMappingRequest(binding_mode="local"),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    db.refresh(mapping)
    assert mapping_binding_mode(mapping.field_mapping) == "local"
    assert mapping.field_mapping["__pk_source__"] == "lake"
    assert REMOTE_DESCRIPTOR_KEY not in mapping.field_mapping


def test_local_link_on_remote_dataset_rejected(db, admin_user, stub_port):
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    with pytest.raises(HTTPException) as exc:
        _link_workflow.create_link_mapping(
            db, project.id,
            mapping_router.LinkMappingCreate(
                src_dataset_id="ds-remote-1", tgt_dataset_id="ds-other",
                relation_type="r", src_key="order_code", tgt_key="k"),
            rules=mapping_router._link_mapping_rules(), user=admin_user)
    assert exc.value.detail["code"] == "link_dataset_remote_bound"


def test_remote_link_definition_requires_remote_endpoints(
        db, admin_user, stub_port):
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    link_type = LinkType(
        id="lt-1", ontology_id=project.id, name="contains",
        display_name="包含", source_object_type_id=object_type.id,
        target_object_type_id=object_type.id, cardinality="one-to-many",
        properties=[])
    db.add(link_type)
    db.commit()

    # 无远程对象映射时：远程关系定义被拒绝。
    with pytest.raises(HTTPException) as no_endpoint:
        _link_workflow.create_link_mapping(
            db, project.id,
            mapping_router.LinkMappingCreate(
                src_dataset_id="ds-remote-1", tgt_dataset_id="ds-remote-1",
                relation_type="contains", src_key="order_code",
                tgt_key="order_code", link_type_id="lt-1",
                binding_mode="remote"),
            rules=mapping_router._link_mapping_rules(), user=admin_user)
    assert no_endpoint.value.detail["code"] == "remote_link_endpoint_invalid"

    _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    result = _link_workflow.create_link_mapping(
        db, project.id,
        mapping_router.LinkMappingCreate(
            src_dataset_id="ds-remote-1", tgt_dataset_id="ds-remote-1",
            relation_type="contains", src_key="order_code",
            tgt_key="order_code", link_type_id="lt-1",
            binding_mode="remote"),
        rules=mapping_router._link_mapping_rules(), user=admin_user)
    assert result["binding_mode"] == "remote"
    assert result["match_count"] is None
    link = db.query(OntologyLinkMapping).filter_by(
        id=result["link_mapping_id"]).one()
    assert link.field_mapping[BINDING_MODE_KEY] == BINDING_MODE_REMOTE


# ── 本地物化路径隔离 ───────────────────────────────────────────────


def test_apply_and_source_rows_isolate_remote_binding(
        db, admin_user, stub_port):
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    result = _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    mapping = db.query(OntologyMapping).filter_by(
        id=result["mapping_id"]).one()

    service = MappingService(db)
    with pytest.raises(MappingSourceError):
        from app.ontologies.mappings.mapping_service import (
            load_mapping_source_rows,
        )
        load_mapping_source_rows(db, mapping)
    with pytest.raises(MappingApplyError) as apply_error:
        service.apply_mapping(mapping.id, [])
    assert "远程绑定" in str(apply_error.value)


def test_build_all_reports_remote_bindings_without_materialization(
        db, admin_user, stub_port):
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    service = MappingService(db)
    result = service.build_all(project.id, require_approved=True)
    skipped = result["remote_bindings"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "remote_binding_not_materialized"
    assert skipped[0]["materialized"] is False
    assert result["total_entities"] == 0


# ── 自动订阅排除 ───────────────────────────────────────────────────


def test_automation_subscription_excludes_remote(
        db, admin_user, stub_port):
    project = _draft_project(db, admin_user)
    object_type = _object_type(db, project)
    dataset = Dataset(id="ds-manual", name="人工表", kind="manual",
                      schema_json={"primary_key": ["k"]})
    db.add(dataset)
    db.commit()
    remote = _entity_workflow.create_mapping(
        db, project.id, _create_body("ds-remote-1", object_type),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)
    # 本地人工数据集映射（未指定目标类型，按名匹配，不构成同类型混绑）。
    _entity_workflow.create_mapping(
        db, project.id,
        mapping_router.CreateMappingRequest(
            curated_dataset_id="ds-manual", entity_class="LocalOrder",
            field_mapping={"k": "code"}),
        rules=mapping_router._entity_mapping_rules(), user=admin_user)

    overview = automation_subscription.automation_overview(db, project.id)
    by_id = {item["mapping_id"]: item for item in overview["items"]}
    remote_item = by_id[remote["mapping_id"]]
    assert remote_item["binding_mode"] == "remote"
    assert remote_item["needs_subscription"] is False
    assert remote_item["version_eligibility_reason"] == (
        "remote_binding_not_automatable")

    result = automation_subscription.subscribe_automation(
        db, project.id, on_version=True, on_review=True)
    assert remote["mapping_id"] not in result["subscribed"]
    assert any(
        item["mapping_id"] == remote["mapping_id"]
        and item["reason"] == "remote_binding_not_automatable"
        for item in result["skipped"])
