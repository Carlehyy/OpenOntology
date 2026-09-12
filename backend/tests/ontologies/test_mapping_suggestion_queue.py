"""映射建议队列闭环端点测试：persistent 列表 / confirm / dismiss。

  1. 列表：按 version_id + status 过滤，默认仅 pending；分页 hasMore；
     形状与瞬时建议对齐（camelCase + fieldMappings + source=agent）；
  2. confirm：与人工确认瞬时建议完全同路径（save_draft_mappings 写草稿
     快照），建议置 confirmed 并回填 mappingId；按飞轮纪律回流
     MappingKnowledgeEntry；重复确认 409 不双写；同数据集+同对象条目合并；
     版本守卫（发布/试跑冻结）与权限（viewer 403）照常；
  3. dismiss：置 dismissed 可带原因；confirmed 不可驳回；重复驳回幂等；
     confirmed/dismissed 均不出现在 pending 列表。
"""
from __future__ import annotations

import uuid

import pytest

from app.main import app
from app.models.ontology import OntologyProject
from app.data_channel.datasets.models import Dataset
from app.ontologies.mappings.models import (
    MappingKnowledgeEntry,
    OntologyMappingSuggestion,
)
from app.ontologies.mappings import suggestion_router
from app.ontologies.versions.models import OntologyVersion


# ── 构造工具 ─────────────────────────────────────────────────────────────

OBJECT_TYPES = [
    {
        "id": "ot-customer",
        "name": "Customer",
        "displayName": "客户",
        "primaryKey": "customer_id",
        "properties": [
            {"id": "p-id", "name": "customer_id", "displayName": "客户编号",
             "type": "string"},
            {"id": "p-name", "name": "customer_name", "displayName": "客户名称",
             "type": "string"},
        ],
    },
]


def _snapshot(mappings=None):
    return {
        "objectTypes": OBJECT_TYPES,
        "linkTypes": [],
        "actions": [],
        "functions": [],
        "sentinels": [],
        "mappings": mappings or [],
        "linkMappings": [],
    }


def _make_ontology(db, admin_user) -> OntologyProject:
    project = OntologyProject(
        id=f"ont-{uuid.uuid4().hex[:8]}", name="客户本体", domain="test",
        created_by=admin_user.id)
    db.add(project)
    db.commit()
    return project


def _make_version(db, project, snapshot=None, *, status="editing",
                  kind="draft", number="v0.1") -> OntologyVersion:
    version = OntologyVersion(
        id=f"ver-{uuid.uuid4().hex[:8]}",
        ontology_id=project.id,
        version_number=number,
        node_kind=kind,
        lifecycle_status=status,
        snapshot_formal=_snapshot() if snapshot is None else snapshot,
        created_by=project.created_by,
    )
    db.add(version)
    db.commit()
    return version


def _make_dataset(db, name="客户表") -> Dataset:
    dataset = Dataset(
        id=f"ds-{uuid.uuid4().hex[:8]}",
        name=name,
        kind="structured",
        schema_json={
            "types_source": "declared",
            "primary_key": "cust_id",
            "columns_typed": [
                {"name": "cust_id", "type": "string", "display_name": "客户编号"},
                {"name": "cust_name", "type": "string", "display_name": "客户名称"},
            ],
        },
    )
    db.add(dataset)
    db.commit()
    return dataset


def _make_suggestion(db, project, version, dataset, *, status="pending",
                     field_mapping=None, pk="cust_id", note="列名同名") -> OntologyMappingSuggestion:
    row = OntologyMappingSuggestion(
        id=f"sug-{uuid.uuid4().hex[:8]}",
        ontology_id=project.id,
        version_id=version.id,
        dataset_id=dataset.id,
        object_type_id="ot-customer",
        entity_class="Customer",
        field_mapping=(
            {"cust_id": "customer_id", "cust_name": "customer_name"}
            if field_mapping is None else field_mapping),
        primary_key_column=pk,
        status=status,
        source="agent",
        note=note,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def suggestion_api(client, db):
    def override_db():
        yield db
    app.dependency_overrides[suggestion_router.get_db] = override_db
    yield client
    app.dependency_overrides.pop(suggestion_router.get_db, None)


def _queue_url(ontology_id: str, version_id: str) -> str:
    return (f"/api/v2/ontologies/{ontology_id}/versions/{version_id}"
            "/mapping-suggestions/persistent")


def _action_url(ontology_id: str, version_id: str, suggestion_id: str,
                action: str) -> str:
    return (f"/api/v2/ontologies/{ontology_id}/versions/{version_id}"
            f"/mapping-suggestions/{suggestion_id}/{action}")


# ── 列表端点 ─────────────────────────────────────────────────────────────

def test_list_persistent_filters_pending_and_paginates(suggestion_api, db,
                                                       admin_user,
                                                       auth_headers):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    first = _make_suggestion(db, project, version, dataset)
    _make_suggestion(db, project, version, dataset,
                     field_mapping={"cust_name": "customer_name"}, pk=None)
    _make_suggestion(db, project, version, dataset, status="dismissed")

    url = _queue_url(project.id, version.id)
    response = suggestion_api.get(url, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    # 默认仅 pending；dismissed 不出现
    assert body["total"] == 2
    assert body["hasMore"] is False
    assert {item["id"] for item in body["suggestions"]} == {
        first.id, body["suggestions"][1]["id"]}
    item = next(row for row in body["suggestions"] if row["id"] == first.id)
    assert item["datasetId"] == dataset.id
    assert item["datasetName"] == "客户表"
    assert item["objectTypeId"] == "ot-customer"
    assert item["objectName"] == "Customer"
    assert item["source"] == "agent"
    assert item["note"] == "列名同名"
    assert item["status"] == "pending"
    assert item["createdAt"]
    assert item["primaryKeyColumn"] == "cust_id"
    assert [field["column"] for field in item["fieldMappings"]] == [
        "cust_id", "cust_name"]
    assert all(field["verdict"] == "unsure" for field in item["fieldMappings"])
    assert all(field["source"] == "agent" for field in item["fieldMappings"])

    # 分页
    page = suggestion_api.get(f"{url}?limit=1", headers=auth_headers).json()
    assert page["hasMore"] is True and len(page["suggestions"]) == 1
    rest = suggestion_api.get(f"{url}?limit=1&offset=1",
                              headers=auth_headers).json()
    assert rest["suggestions"][0]["id"] != page["suggestions"][0]["id"]

    # dismissed 可显式查询
    dismissed = suggestion_api.get(f"{url}?status=dismissed",
                                   headers=auth_headers).json()
    assert dismissed["total"] == 1

    # 版本不存在 → 404
    ghost = suggestion_api.get(_queue_url(project.id, "ver-ghost"),
                               headers=auth_headers)
    assert ghost.status_code == 404


# ── confirm 端点 ─────────────────────────────────────────────────────────

def test_confirm_writes_snapshot_mapping_and_harvests(suggestion_api, db,
                                                      admin_user,
                                                      auth_headers):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    suggestion = _make_suggestion(db, project, version, dataset)
    revision_before = version.revision

    response = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "confirm"),
        headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["mappingId"]
    assert body["revision"]

    # 建议状态落库 + 回填 mappingId
    stored = db.query(OntologyMappingSuggestion).filter_by(
        id=suggestion.id).one()
    assert stored.status == "confirmed"
    assert stored.confirmed_mapping_id == body["mappingId"]

    # 与人工保存画布映射同路径：草稿快照出现正式映射条目，revision 推进
    db.refresh(version)
    assert version.revision == revision_before + 1
    mappings = version.snapshot_formal["mappings"]
    assert len(mappings) == 1
    entry = mappings[0]
    assert entry["id"] == body["mappingId"]
    assert entry["curatedDatasetId"] == dataset.id
    assert entry["entityClass"] == "Customer"
    assert entry["targetObjectTypeId"] == "ot-customer"
    assert entry["fieldMapping"] == {
        "cust_id": "customer_id",
        "cust_name": "customer_name",
        "__primary_key__": "cust_id",
    }

    # 知识飞轮回流：确认过的列→属性入知识库
    entries = {(item.column_key, item.property_name)
               for item in db.query(MappingKnowledgeEntry).all()}
    assert entries == {("cust_id", "customer_id"),
                       ("cust_name", "customer_name")}
    assert body["knowledgeHits"] >= 2

    # 幂等：重复确认 409，不双写映射
    again = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "confirm"),
        headers=auth_headers)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "already_confirmed"
    assert again.json()["detail"]["mappingId"] == body["mappingId"]
    db.refresh(version)
    assert len(version.snapshot_formal["mappings"]) == 1

    # confirmed 不再出现在 pending 列表
    pending = suggestion_api.get(_queue_url(project.id, version.id),
                                 headers=auth_headers).json()
    assert pending["total"] == 0


def test_confirm_merges_into_existing_mapping_entry(suggestion_api, db,
                                                    admin_user, auth_headers):
    project = _make_ontology(db, admin_user)
    dataset = _make_dataset(db)
    version = _make_version(db, project, _snapshot(mappings=[{
        "id": "map-existing",
        "curatedDatasetId": dataset.id,
        "entityClass": "Customer",
        "targetObjectTypeId": "ot-customer",
        "fieldMapping": {"cust_id": "customer_id"},
    }]))
    suggestion = _make_suggestion(
        db, project, version, dataset,
        field_mapping={"cust_name": "customer_name"}, pk="cust_id")

    response = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "confirm"),
        headers=auth_headers)
    assert response.status_code == 200, response.text
    # 合并进既有条目：不产生重复 mapping，id 不变，建议键并入
    assert response.json()["mappingId"] == "map-existing"
    db.refresh(version)
    mappings = version.snapshot_formal["mappings"]
    assert len(mappings) == 1
    assert mappings[0]["id"] == "map-existing"
    assert mappings[0]["fieldMapping"] == {
        "cust_id": "customer_id",
        "cust_name": "customer_name",
        "__primary_key__": "cust_id",
    }


def test_confirm_guards(suggestion_api, db, admin_user, auth_headers):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)

    # 建议不存在
    ghost = suggestion_api.post(
        _action_url(project.id, version.id, "sug-ghost", "confirm"),
        headers=auth_headers)
    assert ghost.status_code == 404

    # 已驳回建议不能确认
    dismissed = _make_suggestion(db, project, version, dataset,
                                 status="dismissed")
    response = suggestion_api.post(
        _action_url(project.id, version.id, dismissed.id, "confirm"),
        headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "suggestion_not_pending"

    # 发布版本：草稿写守卫照常
    release = _make_version(db, project, kind="release", status="released",
                            number="v0")
    on_release = _make_suggestion(db, project, release, dataset)
    frozen = suggestion_api.post(
        _action_url(project.id, release.id, on_release.id, "confirm"),
        headers=auth_headers)
    assert frozen.status_code == 409
    assert frozen.json()["detail"]["code"] == "immutable_release"

    # 试跑态冻结
    trial = _make_version(db, project, status="trial_ready", number="v0.2")
    on_trial = _make_suggestion(db, project, trial, dataset)
    blocked = suggestion_api.post(
        _action_url(project.id, trial.id, on_trial.id, "confirm"),
        headers=auth_headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "trial_snapshot_frozen"

    # 全部守卫路径均不产生映射/状态变化
    db.refresh(version)
    assert version.snapshot_formal["mappings"] == []
    assert db.query(MappingKnowledgeEntry).count() == 0


def test_confirm_forbidden_for_viewer(suggestion_api, db, admin_user):
    from app.auth.service import hash_password
    from app.models.user import User

    viewer = User(id=str(uuid.uuid4()), username="viewer", email="v@test.com",
                  password_hash=hash_password("viewer123"), role="viewer")
    db.add(viewer)
    db.commit()
    token = suggestion_api.post("/api/v1/auth/login", json={
        "username": "viewer", "password": "viewer123"}).json()["data"]["access_token"]
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    suggestion = _make_suggestion(db, project, version, dataset)

    headers = {"Authorization": f"Bearer {token}"}
    for action in ("confirm", "dismiss"):
        response = suggestion_api.post(
            _action_url(project.id, version.id, suggestion.id, action),
            headers=headers)
        assert response.status_code == 403
    # 只读列表对 viewer 放行（与瞬时建议端点同一守卫口径）
    assert suggestion_api.get(
        _queue_url(project.id, version.id), headers=headers).status_code == 200


# ── dismiss 端点 ─────────────────────────────────────────────────────────

def test_dismiss_marks_and_is_idempotent(suggestion_api, db, admin_user,
                                         auth_headers):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    suggestion = _make_suggestion(db, project, version, dataset)

    response = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "dismiss"),
        json={"reason": "列口径不对"},
        headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "dismissed"
    assert response.json()["reused"] is False

    stored = db.query(OntologyMappingSuggestion).filter_by(
        id=suggestion.id).one()
    assert stored.status == "dismissed"
    assert stored.status_reason == "列口径不对"

    # 草稿快照不受影响
    db.refresh(version)
    assert version.snapshot_formal["mappings"] == []

    # dismissed 不出现在 pending 列表，可显式查询
    pending = suggestion_api.get(_queue_url(project.id, version.id),
                                 headers=auth_headers).json()
    assert pending["total"] == 0
    dismissed = suggestion_api.get(
        f"{_queue_url(project.id, version.id)}?status=dismissed",
        headers=auth_headers).json()
    assert dismissed["total"] == 1
    assert dismissed["suggestions"][0]["statusReason"] == "列口径不对"

    # 重复驳回幂等返回现值
    again = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "dismiss"),
        json={"reason": "换个理由"},
        headers=auth_headers)
    assert again.status_code == 200
    assert again.json()["reused"] is True
    assert again.json()["statusReason"] == "列口径不对"


def test_dismiss_rejects_confirmed_suggestion(suggestion_api, db, admin_user,
                                              auth_headers):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    suggestion = _make_suggestion(db, project, version, dataset)
    confirmed = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "confirm"),
        headers=auth_headers)
    assert confirmed.status_code == 200

    response = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "dismiss"),
        json={"reason": "反悔"},
        headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_confirmed"


# ── confirm 状态机原子化与锚定复查（P2-2）─────────────────────────────────

def test_confirm_rejects_when_anchor_object_removed(suggestion_api, db,
                                                    admin_user, auth_headers):
    """锚定复查：提案后锚定对象从快照中移除 → 422 object_not_found，
    不写悬空映射，建议保持 pending。"""
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    suggestion = _make_suggestion(db, project, version, dataset)

    # 提案后对象被移除（快照不再含 ot-customer）
    version.snapshot_formal = _snapshot()
    version.snapshot_formal["objectTypes"] = []
    db.commit()

    response = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "confirm"),
        headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "object_not_found"

    stored = db.query(OntologyMappingSuggestion).filter_by(
        id=suggestion.id).one()
    assert stored.status == "pending"
    db.refresh(version)
    assert version.snapshot_formal["mappings"] == []


def test_confirm_rolls_back_status_when_save_fails(suggestion_api, db,
                                                   admin_user, auth_headers,
                                                   monkeypatch):
    """原子化：save_draft_mappings 抛错时建议状态翻转随同事务回滚，
    不产生「映射未进草稿而建议已 confirmed」的中间态。"""
    from fastapi import HTTPException

    from app.ontologies.versions import workspace_service

    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    suggestion = _make_suggestion(db, project, version, dataset)

    def boom(*_args, **_kwargs):
        raise HTTPException(409, detail={
            "code": "conflict", "message": "模拟并发保存冲突"})

    monkeypatch.setattr(workspace_service, "save_draft_mappings", boom)
    response = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "confirm"),
        headers=auth_headers)
    assert response.status_code == 409

    stored = db.query(OntologyMappingSuggestion).filter_by(
        id=suggestion.id).one()
    assert stored.status == "pending"
    assert stored.confirmed_mapping_id is None
    db.refresh(version)
    assert version.snapshot_formal["mappings"] == []


def test_confirm_harvest_failure_keeps_confirmed_state(suggestion_api, db,
                                                       admin_user,
                                                       auth_headers,
                                                       monkeypatch):
    """harvest 失败只记日志：已确认状态与草稿映射不被推翻，knowledgeHits=0。"""
    from app.ontologies.mappings import suggestion_service

    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    suggestion = _make_suggestion(db, project, version, dataset)

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟知识库不可用")

    monkeypatch.setattr(
        suggestion_service.mapping_knowledge, "harvest_snapshot_mappings", boom)
    response = suggestion_api.post(
        _action_url(project.id, version.id, suggestion.id, "confirm"),
        headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["knowledgeHits"] == 0

    stored = db.query(OntologyMappingSuggestion).filter_by(
        id=suggestion.id).one()
    assert stored.status == "confirmed"
    db.refresh(version)
    assert len(version.snapshot_formal["mappings"]) == 1


# ── status 白名单（P2-2）──────────────────────────────────────────────────

def test_list_status_param_whitelist(suggestion_api, db, admin_user,
                                     auth_headers):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    _make_suggestion(db, project, version, dataset)

    url = _queue_url(project.id, version.id)
    bad = suggestion_api.get(f"{url}?status=foo", headers=auth_headers)
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "invalid_status"
    for ok in ("pending", "confirmed", "dismissed"):
        assert suggestion_api.get(f"{url}?status={ok}",
                                  headers=auth_headers).status_code == 200


# ── get_mapping_overview 数据集截断标记（P3-1）────────────────────────────

def test_overview_explicit_dataset_pinned_and_truncation_flag(db, admin_user):
    """显式 dataset_id 钉到清单首位（永不被 involved>10 截断挤掉），
    截断时如实返回 datasetsTruncated。"""
    from app.ontologies.mappings import suggestion_service

    project = _make_ontology(db, admin_user)
    datasets = [_make_dataset(db, name=f"表{i}") for i in range(12)]
    version = _make_version(db, project, _snapshot(mappings=[
        {
            "id": f"map-{i}",
            "curatedDatasetId": item.id,
            "entityClass": "Customer",
            "targetObjectTypeId": "ot-customer",
            "fieldMapping": {"cust_id": "customer_id"},
        }
        for i, item in enumerate(datasets[:11])
    ]))
    focused = datasets[11]

    overview = suggestion_service.get_mapping_overview(
        db, project.id, version.id, dataset_id=focused.id)

    assert overview["datasetsTruncated"] is True
    assert overview["datasets"][0]["id"] == focused.id  # 钉到首位，未被挤掉
    assert len(overview["datasets"]) == 10

    untruncated = suggestion_service.get_mapping_overview(
        db, project.id, version.id)
    assert untruncated["datasetsTruncated"] is True   # 11 个涉及数据集 > 10

    small = _make_version(db, project, number="v0.2")
    small_overview = suggestion_service.get_mapping_overview(
        db, project.id, small.id)
    assert small_overview["datasetsTruncated"] is False
