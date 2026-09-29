"""审批三视角 + 完整删除安全加固。

对应本轮需求：
1. 成品数据集可完整删除，审核记录与行级修改随数据集一起清理
2. 审批可看三视角：变化量 / 上一版全量 / 本次全量
3. 删除权限收敛：人工删除限管理员；成品/人工删除被本体映射引用时拦截
"""
from __future__ import annotations

import io
import uuid

import pytest
from fastapi import HTTPException

from app.main import app
from app.routers.v2 import curated as curated_module
from app.routers.v2 import datasets as datasets_module
from app.routers.v2 import mappings as mappings_module
from app.models.v2.dataset import Dataset, DatasetVersion, DatasetVersionEvent
from app.models.v2.curated import CuratedReview
from app.data_channel.datasets.service import DatasetService, rows_to_parquet_bytes
from app.data_channel.curated.review_service import (
    ReviewApprovalError,
    ReviewService,
    current_version_review,
    load_rows_with_edits,
    require_version_approved,
)


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, bucket, key, data, content_type=""):
        uri = f"s3://{bucket}/{key}"
        self.objects[uri] = data
        return uri

    def get_object(self, uri):
        if uri not in self.objects:
            raise FileNotFoundError(uri)
        return self.objects[uri]

    def delete_object(self, uri):
        self.objects.pop(uri, None)


@pytest.fixture
def fake_storage(monkeypatch):
    from app.data_channel.datasets import service as ds_service
    fs = FakeStorage()
    monkeypatch.setattr(ds_service, "get_storage_service", lambda: fs)
    return fs


@pytest.fixture
def api(client, db, fake_storage):
    def _override():
        yield db
    for mod in (datasets_module, mappings_module, curated_module):
        app.dependency_overrides[mod.get_db] = _override
    yield client
    for mod in (datasets_module, mappings_module, curated_module):
        app.dependency_overrides.pop(mod.get_db, None)


def _login(client, username, password):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def _make_curated_with_versions(db, versions: list[list[dict]], pk: str = "id") -> str:
    """建一个 v2 curated 数据集并按序写入多个全量版本，返回 dataset id。"""
    svc = DatasetService(db)
    ds = Dataset(name=f"diff-{uuid.uuid4().hex[:6]}", kind="curated",
                 schema_json={"primary_key": pk})
    db.add(ds)
    db.commit()
    db.refresh(ds)
    for rows in versions:
        svc.create_version(ds.id, rows_to_parquet_bytes(rows), rowcount=len(rows))
    return ds.id


# ── 三视角 ─────────────────────────────────────────────────────
def test_review_diff_three_views(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [
        [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}],   # v1
        [{"id": "1", "name": "A"}, {"id": "3", "name": "c"}],   # v2: 1 改, 2 删, 3 增
    ])
    r = api.get(f"/api/v2/curated/{ds_id}/review-diff", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json().get("data", r.json())

    assert body["current"]["version_no"] == 2
    assert body["previous"]["version_no"] == 1
    assert len(body["current"]["rows"]) == 2
    assert len(body["previous"]["rows"]) == 2

    delta = body["delta"]
    assert delta["added_count"] == 1
    assert delta["updated_count"] == 1
    assert delta["deleted_count"] == 1
    # 变化量样本可追溯
    assert delta["added_sample"][0]["id"] == "3"
    assert delta["deleted_sample"][0]["id"] == "2"
    assert delta["updated_sample"][0]["before"]["name"] == "a"
    assert delta["updated_sample"][0]["after"]["name"] == "A"


def test_review_full_views_are_explicitly_paginated(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[
        {"id": "1", "name": "a"},
        {"id": "2", "name": "b"},
        {"id": "3", "name": "c"},
    ]])

    response = api.get(
        f"/api/v2/curated/{ds_id}/review-diff?limit=1&offset=1",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json().get("data", response.json())
    current = payload["current"]
    assert current == {
        "version_no": 1,
        "dataset_version_id": current["dataset_version_id"],
        "total": 3,
        "rows": [{"id": "2", "name": "b"}],
        "offset": 1,
        "limit": 1,
        "has_more": True,
    }


def test_review_diff_exposes_backend_canonical_row_keys(api, auth_headers, db):
    """浏览器直接使用服务端 row_pk，不能用 id/行号或分隔符自行猜测。"""
    ds_id = _make_curated_with_versions(db, [[
        {"tenant": "中国区", "order_no": "1,2", "amount": 10},
        {"tenant": "中国区,1", "order_no": "2", "amount": 20},
    ]], pk="tenant,order_no")

    response = api.get(
        f"/api/v2/curated/{ds_id}/review-diff?limit=1&offset=1",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json().get("data", response.json())
    assert payload["row_pk_encoding"] == "json-array"
    assert payload["current_row_pks"] == ['["中国区,1","2"]']


def test_curated_preview_supports_explicit_pagination(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[
        {"id": "1", "name": "a"},
        {"id": "2", "name": "b"},
        {"id": "3", "name": "c"},
    ]])
    review = ReviewService(db).start_review(ds_id)
    ReviewService(db).approve(review.id)

    response = api.get(
        f"/api/v2/curated/{ds_id}/preview?limit=1&offset=1",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json().get("data", response.json())
    assert payload["rows"] == [{"id": "2", "name": "b"}]
    assert payload["columns"] == ["id", "name"]
    assert payload["total_rows"] == 3
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert payload["has_more"] is True

    version = DatasetService(db).list_versions(ds_id)[-1]
    for path in (
        f"/api/v2/datasets/{ds_id}/preview?limit=1&offset=1",
        f"/api/v2/datasets/{ds_id}/versions/{version.version_no}/preview?limit=1",
    ):
        approved_preview = api.get(path, headers=auth_headers)
        assert approved_preview.status_code == 200, (path, approved_preview.text)


def test_curated_export_is_full_and_includes_approved_edits(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[
        {"id": "1", "name": "审核前"},
        {"id": "2", "name": "保持不变"},
    ]])
    service = ReviewService(db)
    review = service.start_review(ds_id)
    service.batch_edit_rows(review.id, [{
        "row_pk": "1", "field_name": "name",
        "old_value": "审核前", "new_value": "审核后",
    }])
    service.approve(review.id)

    csv_response = api.get(
        f"/api/v2/curated/{ds_id}/export?format=csv",
        headers=auth_headers,
    )
    assert csv_response.status_code == 200, csv_response.text
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_response.headers["content-disposition"]
    csv_text = csv_response.content.decode("utf-8-sig")
    assert "id,name" in csv_text
    assert "1,审核后" in csv_text
    assert "2,保持不变" in csv_text
    assert "审核前" not in csv_text

    xlsx_response = api.get(
        f"/api/v2/curated/{ds_id}/export?format=xlsx",
        headers=auth_headers,
    )
    assert xlsx_response.status_code == 200, xlsx_response.text
    assert xlsx_response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    import openpyxl
    workbook = openpyxl.load_workbook(io.BytesIO(xlsx_response.content), read_only=True)
    values = list(workbook.active.iter_rows(values_only=True))
    assert values == [("id", "name"), ("1", "审核后"), ("2", "保持不变")]


def test_pending_curated_version_cannot_use_reviewed_export(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "待审核"}]])
    version = DatasetService(db).list_versions(ds_id)[-1]

    for path in (
        f"/api/v2/curated/{ds_id}/preview",
        f"/api/v2/curated/{ds_id}/export?format=csv",
        f"/api/v2/datasets/{ds_id}/preview",
        f"/api/v2/datasets/{ds_id}/versions/{version.version_no}/preview",
    ):
        response = api.get(path, headers=auth_headers)
        assert response.status_code == 409, (path, response.text)
        assert "dataset_version_not_approved" in response.text


def test_rejected_curated_version_is_only_readable_through_review_diff(
        api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[
        {"id": "1", "name": "应被拒绝"},
    ]])
    service = ReviewService(db)
    review = service.start_review(ds_id)
    service.reject(review.id, notes="质量不达标")
    version = DatasetService(db).list_versions(ds_id)[-1]

    for path in (
        f"/api/v2/curated/{ds_id}/preview",
        f"/api/v2/curated/{ds_id}/export?format=xlsx",
        f"/api/v2/datasets/{ds_id}/preview",
        f"/api/v2/datasets/{ds_id}/versions/{version.version_no}/preview",
    ):
        response = api.get(path, headers=auth_headers)
        assert response.status_code == 409, (path, response.text)
        assert "dataset_version_rejected" in response.text

    audit = api.get(
        f"/api/v2/curated/{ds_id}/review-diff?review_id={review.id}",
        headers=auth_headers,
    )
    assert audit.status_code == 200, audit.text
    payload = audit.json().get("data", audit.json())
    assert payload["review"]["status"] == "rejected"
    assert payload["current"]["dataset_version_id"] == version.id
    assert payload["current"]["rows"] == [{"id": "1", "name": "应被拒绝"}]


def test_review_diff_without_primary_key_uses_full_row_add_delete(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [
        [{"body": {"status": "old"}, "webhook": "same"}],
        [{"body": {"status": "new"}, "webhook": "same"}],
    ], pk="")
    response = api.get(
        f"/api/v2/curated/{ds_id}/review-diff",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json().get("data", response.json())
    assert payload["pk"] == []
    assert payload["delta"]["keyed_by"] is None
    assert payload["delta"]["updated_count"] == 0
    assert payload["delta"]["added_count"] == 1
    assert payload["delta"]["deleted_count"] == 1


def test_curated_schema_uses_pipeline_field_display_names(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[{
        "order_id": "SO-1",
        "body": {"status": "new"},
        "amount": 5,
        "promised_at": "2026-07-24",
        "active": False,
        "fallback": "legacy",
    }]], pk="order_id")
    dataset = db.query(Dataset).filter(Dataset.id == ds_id).one()
    dataset.schema_json = {
        **dict(dataset.schema_json or {}),
        "columns": [
            "order_id", "body", "amount", "promised_at", "active", "fallback",
        ],
        "columns_typed": [
            {"name": "order_id", "type": "string"},
            {"name": "body", "type": "json"},
            {"name": "amount", "type": "float"},
            {"name": "promised_at", "type": "timestamp"},
            {"name": "active", "type": "boolean"},
            {"name": "fallback", "type": "string"},
        ],
        "types_source": "published_pipeline_contract",
        "field_names": {
            "order_id": "订单编号",
            "body": "body",
        },
        "contract_definitions": [
            {"field_key": "order_id", "field_name": "订单编号", "field_type": "string", "nullable": False},
            {"field_key": "body", "field_name": "body", "field_type": "json", "nullable": True},
            {"field_key": "amount", "field_name": "订单金额", "field_type": "float", "nullable": False},
            {"field_key": "promised_at", "field_name": "承诺日期", "field_type": "timestamp", "nullable": False},
            {"field_key": "active", "field_name": "有效标记", "field_type": "boolean", "nullable": False},
        ],
    }
    db.commit()

    response = api.get(f"/api/v2/datasets/{ds_id}/schema", headers=auth_headers)
    assert response.status_code == 200, response.text
    columns = {
        column["name"]: column
        for column in response.json().get("data", response.json())["columns"]
    }
    assert columns["order_id"]["display_name"] == "订单编号"
    assert columns["order_id"]["display_name_configured"] is True
    assert columns["body"]["display_name"] == "body"
    assert columns["body"]["display_name_configured"] is True
    assert columns["fallback"]["display_name"] == "fallback"
    assert columns["fallback"]["display_name_configured"] is False
    assert {name: columns[name]["type"] for name in (
        "order_id", "body", "amount", "promised_at", "active", "fallback",
    )} == {
        "order_id": "string",
        "body": "json",
        # 物理快照分别是 JSON 文本、"5"、日期文本和 "False"；发布契约
        # 必须胜过基于这些字符串样值的二次推断。
        "amount": "float",
        "promised_at": "timestamp",
        "active": "boolean",
        "fallback": "string",
    }


def test_review_diff_first_version_all_added(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [
        [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}],   # 仅 v1
    ])
    r = api.get(f"/api/v2/curated/{ds_id}/review-diff", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json().get("data", r.json())
    assert body["previous"]["version_no"] is None
    assert body["delta"]["added_count"] == 2
    assert body["delta"]["updated_count"] == 0
    assert body["delta"]["deleted_count"] == 0


# ── 审核行身份与版本绑定 ───────────────────────────────────────
def test_review_edit_uses_arbitrary_single_primary_key(db, fake_storage):
    ds_id = _make_curated_with_versions(db, [[
        {"order_no": "SO-1", "name": "原名称"},
        {"order_no": "SO-2", "name": "不应修改"},
    ]], pk="order_no")
    svc = ReviewService(db)
    review = svc.start_review(ds_id)
    assert review.dataset_version_id is not None
    svc.batch_edit_rows(review.id, [{
        "row_pk": "SO-1", "field_name": "name",
        "old_value": "原名称", "new_value": "新名称",
    }])
    result = svc.apply_edits_to_snapshot(review.id, [
        {"order_no": "SO-1", "name": "原名称"},
        {"order_no": "SO-2", "name": "不应修改"},
    ])
    assert result[0]["name"] == "新名称"
    assert result[1]["name"] == "不应修改"


def test_review_edit_with_wrong_row_key_is_not_silently_ignored(db, fake_storage):
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "原名称"}]], pk="order_no")
    svc = ReviewService(db)
    review = svc.start_review(ds_id)
    with pytest.raises(HTTPException) as exc:
        svc.batch_edit_rows(review.id, [{
            "row_pk": "NOT-EXISTS", "field_name": "name",
            "old_value": None, "new_value": "错误目标",
        }])
    assert exc.value.detail["code"] == "review_row_not_found"


def test_review_edit_rejects_unknown_field_and_type_mismatch(db, fake_storage):
    svc = DatasetService(db)
    ds = Dataset(
        name="typed-review", kind="curated",
        schema_json={
            "primary_key": "id",
            "columns": ["id", "amount"],
            "columns_typed": [
                {"name": "id", "type": "string"},
                {"name": "amount", "type": "integer"},
            ],
        },
    )
    db.add(ds)
    db.commit()
    svc.create_version(
        ds.id, rows_to_parquet_bytes([{"id": "1", "amount": "10"}]), rowcount=1)
    review = ReviewService(db).start_review(ds.id)

    with pytest.raises(HTTPException) as unknown:
        ReviewService(db).batch_edit_rows(review.id, [{
            "row_pk": "1", "field_name": "injected", "new_value": "x",
        }])
    assert unknown.value.detail["code"] == "review_unknown_field"
    with pytest.raises(HTTPException) as mismatch:
        ReviewService(db).batch_edit_rows(review.id, [{
            "row_pk": "1", "field_name": "amount", "new_value": "不是整数",
        }])
    assert mismatch.value.detail["code"] == "review_type_mismatch"


def test_review_edit_enforces_nullable_and_applies_canonical_types(db, fake_storage):
    svc = DatasetService(db)
    schema = {
        "primary_key": "id",
        "columns": ["id", "amount", "active", "metadata"],
        "columns_typed": [
            {"name": "id", "type": "string"},
            {"name": "amount", "type": "integer"},
            {"name": "active", "type": "boolean"},
            {"name": "metadata", "type": "json"},
        ],
        "contract_definitions": [
            {"field_key": "id", "field_type": "string", "nullable": False},
            {"field_key": "amount", "field_type": "integer", "nullable": False},
            {"field_key": "active", "field_type": "boolean", "nullable": False},
            {"field_key": "metadata", "field_type": "json", "nullable": True},
        ],
    }
    ds = Dataset(name="typed-canonical-review", kind="curated", schema_json=schema)
    db.add(ds)
    db.commit()
    original = {"id": "1", "amount": 10, "active": True, "metadata": {}}
    svc.create_version(
        ds.id, rows_to_parquet_bytes([original]), rowcount=1,
        schema_json=schema,
    )
    review_svc = ReviewService(db)
    review = review_svc.start_review(ds.id)

    with pytest.raises(HTTPException) as null_edit:
        review_svc.batch_edit_rows(review.id, [{
            "row_pk": "1", "field_name": "amount", "new_value": None,
        }])
    assert null_edit.value.detail["code"] == "review_null_forbidden"

    review_svc.batch_edit_rows(review.id, [
        {"row_pk": "1", "field_name": "amount", "new_value": "1,234"},
        {"row_pk": "1", "field_name": "active", "new_value": "false"},
        {"row_pk": "1", "field_name": "metadata", "new_value": '{"source":"review"}'},
    ])
    edited = review_svc.apply_edits_to_snapshot(review.id, [original])[0]
    assert edited["amount"] == 1234
    assert isinstance(edited["amount"], int)
    assert edited["active"] is False
    assert edited["metadata"] == {"source": "review"}


def test_review_edit_supports_composite_primary_key_without_collision(db, fake_storage):
    ds_id = _make_curated_with_versions(db, [[
        {"tenant": "cn", "order_no": "1,2", "amount": "10"},
        {"tenant": "cn,1", "order_no": "2", "amount": "20"},
    ]], pk="tenant,order_no")
    svc = ReviewService(db)
    review = svc.start_review(ds_id)
    svc.batch_edit_rows(review.id, [{
        # JSON 数组编码不会把 ("cn", "1,2") 与 ("cn,1", "2") 混为一行
        "row_pk": ["cn", "1,2"], "field_name": "amount",
        "old_value": "10", "new_value": "11",
    }])
    result = svc.apply_edits_to_snapshot(review.id, [
        {"tenant": "cn", "order_no": "1,2", "amount": "10"},
        {"tenant": "cn,1", "order_no": "2", "amount": "20"},
    ])
    assert [r["amount"] for r in result] == ["11", "20"]


def test_review_cannot_approve_after_new_dataset_version_arrives(db, fake_storage):
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "v1"}]], pk="order_no")
    review_svc = ReviewService(db)
    review = review_svc.start_review(ds_id)
    reviewed_version_id = review.dataset_version_id

    DatasetService(db).create_version(
        ds_id, rows_to_parquet_bytes([{"order_no": "SO-1", "name": "v2"}]), rowcount=1)
    with pytest.raises(HTTPException) as exc:
        review_svc.approve(review.id)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "review_version_stale"
    assert exc.value.detail["review_dataset_version_id"] == reviewed_version_id
    db.refresh(review)
    assert review.status == "pending"


def test_review_cannot_save_edits_after_new_dataset_version_arrives(db, fake_storage):
    """页面打开后的新版本不能让行级修改悄悄落进旧审核。"""
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "v1"}]], pk="order_no")
    review_svc = ReviewService(db)
    review = review_svc.start_review(ds_id)

    DatasetService(db).create_version(
        ds_id, rows_to_parquet_bytes([{"order_no": "SO-1", "name": "v2"}]), rowcount=1)
    with pytest.raises(HTTPException) as exc:
        review_svc.batch_edit_rows(review.id, [{
            "row_pk": "SO-1", "field_name": "name",
            "old_value": "v1", "new_value": "用户以为在改 v2",
        }])
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "review_version_stale"


def test_review_diff_keeps_showing_bound_version_after_new_version(api, auth_headers, db):
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "v1"}]], pk="order_no")
    review = ReviewService(db).start_review(ds_id)
    DatasetService(db).create_version(
        ds_id, rows_to_parquet_bytes([{"order_no": "SO-1", "name": "v2"}]), rowcount=1)

    response = api.get(
        f"/api/v2/curated/{ds_id}/review-diff",
        params={"review_id": review.id}, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json().get("data", response.json())
    assert body["current"]["version_no"] == 1
    assert body["current"]["rows"][0]["name"] == "v1"
    assert body["review"]["stale"] is True
    assert body["review"]["latest_version_no"] == 2


def test_approved_v1_edits_do_not_leak_into_v2_export(db, fake_storage):
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "v1"}]], pk="order_no")
    review_svc = ReviewService(db)
    review = review_svc.start_review(ds_id)
    review_svc.batch_edit_rows(review.id, [{
        "row_pk": "SO-1", "field_name": "name",
        "old_value": "v1", "new_value": "v1-approved",
    }])
    review_svc.approve(review.id)
    DatasetService(db).create_version(
        ds_id, rows_to_parquet_bytes([{"order_no": "SO-1", "name": "v2"}]), rowcount=1)

    assert load_rows_with_edits(db, ds_id)[0]["name"] == "v2"


def test_quick_approve_decides_existing_pending_row_edits(api, auth_headers, db):
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "原名称"}]], pk="order_no")
    svc = ReviewService(db)
    pending = svc.start_review(ds_id)
    svc.batch_edit_rows(pending.id, [{
        "row_pk": "SO-1", "field_name": "name",
        "old_value": "原名称", "new_value": "审核修正",
    }])

    response = api.post(
        f"/api/v2/curated/{ds_id}/review",
        params={"action": "approve"}, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json().get("data", response.json())
    assert body["review_id"] == pending.id
    assert load_rows_with_edits(db, ds_id)[0]["name"] == "审核修正"


def test_repeated_start_review_reuses_pending_session_for_same_version(db, fake_storage):
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "原名称", "amount": "10"}]],
        pk="order_no")
    svc = ReviewService(db)
    first = svc.start_review(ds_id)
    svc.batch_edit_rows(first.id, [{
        "row_pk": "SO-1", "field_name": "name",
        "old_value": "原名称", "new_value": "第一批修改",
    }])
    second = svc.start_review(ds_id)
    assert second.id == first.id
    svc.batch_edit_rows(second.id, [{
        "row_pk": "SO-1", "field_name": "amount",
        "old_value": "10", "new_value": "11",
    }])
    svc.approve(second.id)
    exported = load_rows_with_edits(db, ds_id)[0]
    assert exported["name"] == "第一批修改"
    assert exported["amount"] == "11"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_terminal_review_cannot_be_reopened_for_same_version(
        decision, db, fake_storage):
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "当前版本"}]], pk="order_no")
    svc = ReviewService(db)
    terminal = svc.start_review(ds_id)
    getattr(svc, decision)(terminal.id)

    with pytest.raises(HTTPException) as exc_info:
        svc.start_review(ds_id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "review_version_already_decided"
    assert exc_info.value.detail["review_id"] == terminal.id
    assert exc_info.value.detail["status"] == (
        "approved" if decision == "approve" else "rejected")

    DatasetService(db).create_version(
        ds_id,
        rows_to_parquet_bytes([{"order_no": "SO-1", "name": "下一版本"}]),
        rowcount=1,
    )
    next_review = svc.start_review(ds_id)
    assert next_review.id != terminal.id
    assert next_review.dataset_version_id != terminal.dataset_version_id


def test_latest_terminal_decision_controls_legacy_duplicate_reviews(
        db, fake_storage):
    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "当前版本"}]], pk="order_no")
    svc = ReviewService(db)
    approved = svc.start_review(ds_id)
    svc.approve(approved.id)

    # 兼容历史脏数据：旧版本曾允许同一不可变版本重复发起审核。治理读取必须
    # 服从最新终局决定，不能在较新的 rejected 后继续命中任意旧 approved。
    rejected = CuratedReview(
        curated_dataset_id=ds_id,
        dataset_version_id=approved.dataset_version_id,
        status="rejected",
    )
    db.add(rejected)
    db.commit()

    version = db.query(DatasetVersion).filter(
        DatasetVersion.id == approved.dataset_version_id).one()
    with pytest.raises(ReviewApprovalError):
        require_version_approved(db, ds_id, version)
    assert current_version_review(db, ds_id, status="approved") is None


def test_review_session_approve_uses_durable_mapping_handoff(
        api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "a"}]])
    session = api.post(
        f"/api/v2/curated/{ds_id}/reviews", headers=auth_headers).json()
    review_id = session.get("data", session)["review_id"]
    response = api.post(
        f"/api/v2/curated/reviews/{review_id}/approve", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json().get("data", response.json())
    dispatch = payload["mapping_dispatch"]
    assert dispatch["status"] == "queued"
    assert dispatch["durable"] is True
    event = db.query(DatasetVersionEvent).filter_by(
        event_type="curated_review_approved").one()
    assert event.id == dispatch["event_id"]
    assert event.status == "review_pending"


# ── 完整删除已审批数据集 ───────────────────────────────────────
def test_approved_curated_can_be_fully_deleted(api, auth_headers, db, admin_user):
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "a"}]])
    # 审批通过
    r = api.post(f"/api/v2/curated/{ds_id}/review", params={"action": "approve"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    review_id = r.json().get("data", r.json())["review_id"]

    r = api.delete(f"/api/v2/curated/{ds_id}", headers=auth_headers)
    assert r.status_code == 204, r.text
    assert db.query(CuratedReview).filter(CuratedReview.id == review_id).count() == 0
    assert db.query(DatasetVersion).filter(DatasetVersion.dataset_id == ds_id).count() == 0
    assert db.query(Dataset).filter(Dataset.id == ds_id).count() == 0


def test_new_version_does_not_inherit_old_approval(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "v1"}]])
    version_1 = db.query(DatasetVersion).filter_by(dataset_id=ds_id).one()

    r = api.post(
        f"/api/v2/curated/{ds_id}/review",
        params={"action": "approve"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    review = db.query(CuratedReview).filter_by(
        id=r.json().get("data", r.json())["review_id"]).one()
    assert review.dataset_version_id == version_1.id

    DatasetService(db).create_version(
        ds_id, rows_to_parquet_bytes([{"id": "1", "name": "v2"}]), rowcount=1)

    r = api.get("/api/v2/curated", headers=auth_headers)
    assert r.status_code == 200, r.text
    payload = r.json()
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    current = next(row for row in rows if row["id"] == ds_id)
    assert current["status"] == "pending_review"
    detail = api.get(f"/api/v2/curated/{ds_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json().get("data", detail.json())["status"] == "pending_review"


def test_stale_review_cannot_approve_newer_version(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "v1"}]])
    r = api.post(f"/api/v2/curated/{ds_id}/reviews", headers=auth_headers)
    assert r.status_code == 200, r.text
    review_id = r.json().get("data", r.json())["review_id"]

    DatasetService(db).create_version(
        ds_id, rows_to_parquet_bytes([{"id": "1", "name": "v2"}]), rowcount=1)
    r = api.post(
        f"/api/v2/curated/reviews/{review_id}/approve", headers=auth_headers)
    assert r.status_code == 409, r.text
    db.expire_all()
    assert db.query(CuratedReview).filter_by(id=review_id).one().status == "pending"


def test_pending_curated_can_be_deleted(api, auth_headers, db):
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "a"}]])
    r = api.delete(f"/api/v2/curated/{ds_id}", headers=auth_headers)
    assert r.status_code == 204, r.text


def test_pending_review_and_edits_are_removed_with_dataset(api, auth_headers, db):
    from app.models.v2.curated import CuratedReview, CuratedRowEdit

    ds_id = _make_curated_with_versions(
        db, [[{"order_no": "SO-1", "name": "a"}]], pk="order_no")
    review = ReviewService(db).start_review(ds_id)
    review_id = review.id
    ReviewService(db).batch_edit_rows(review.id, [{
        "row_pk": "SO-1", "field_name": "name",
        "old_value": "a", "new_value": "A",
    }])

    response = api.delete(f"/api/v2/curated/{ds_id}", headers=auth_headers)
    assert response.status_code == 204, response.text
    assert db.query(CuratedRowEdit).filter(CuratedRowEdit.review_id == review_id).count() == 0
    assert db.query(CuratedReview).filter(CuratedReview.id == review_id).count() == 0
    assert db.query(Dataset).filter(Dataset.id == ds_id).count() == 0
    assert db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == ds_id).count() == 0


# ── 依赖检查：被本体映射引用时拦截 ─────────────────────────────
def test_curated_delete_blocked_by_mapping(api, auth_headers, db):
    from app.ontologies.mappings.models import OntologyMapping
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "a"}]])
    db.add(OntologyMapping(ontology_id="ont-x", curated_dataset_id=ds_id,
                           entity_class="产品", field_mapping={}))
    db.commit()
    r = api.delete(f"/api/v2/curated/{ds_id}", headers=auth_headers)
    assert r.status_code == 409, r.text
    assert "in_use" in r.text
    # force 已禁用：真实外键与 API 语义一致，必须先解除依赖
    r = api.delete(f"/api/v2/curated/{ds_id}?force=true", headers=auth_headers)
    assert r.status_code == 400, r.text
    assert "force" in r.text


# ── 人工数据集删除：限管理员 ───────────────────────────────────
def _upload_manual(api, headers) -> str:
    csv = "id,name\n1,苹果\n2,香蕉\n"
    r = api.post("/api/v2/datasets/upload",
                 files={"file": ("库存.csv", io.BytesIO(csv.encode("utf-8")), "text/csv")},
                 headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def test_manual_delete_requires_admin(api, auth_headers, db, editor_user):
    ds_id = _upload_manual(api, auth_headers)  # 管理员上传
    editor_headers = _login(api, "editor", "editor123")
    r = api.delete(f"/api/v2/datasets/{ds_id}", headers=editor_headers)
    assert r.status_code == 403, r.text
    # 管理员可删
    r = api.delete(f"/api/v2/datasets/{ds_id}", headers=auth_headers)
    assert r.status_code == 200, r.text


def test_manual_delete_blocked_by_mapping(api, auth_headers, db):
    from app.ontologies.mappings.models import OntologyMapping
    ds_id = _upload_manual(api, auth_headers)
    db.add(OntologyMapping(ontology_id="ont-y", curated_dataset_id=ds_id,
                           entity_class="库存", field_mapping={}))
    db.commit()
    r = api.delete(f"/api/v2/datasets/{ds_id}", headers=auth_headers)
    assert r.status_code == 409, r.text
    assert "本体映射" in r.text


# ── 审批决定人：决定时写入 reviewer_id ────────────────────────────

def test_one_step_review_records_decider_admin_id(api, auth_headers, db, admin_user):
    """一步审批端点必须把 require_admin 给出的管理员 id 写入 reviewer_id。

    历史行为：三个决定端点都把 _admin 丢掉，库里的 reviewer_id 恒为空，
    无法回答「是谁批的」。"""
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "a"}]])
    r = api.post(f"/api/v2/curated/{ds_id}/review",
                 params={"action": "approve", "notes": "一步审批"},
                 headers=auth_headers)
    assert r.status_code == 200, r.text
    review_id = r.json().get("data", r.json())["review_id"]
    review = db.query(CuratedReview).filter(CuratedReview.id == review_id).one()
    assert review.status == "approved"
    assert review.reviewer_id == admin_user.id


def test_decision_overwrites_reviewer_when_reusing_pending_review(
        api, auth_headers, db, admin_user):
    """复用已有 pending 审核时，决定人写在决定当时并覆盖（而非沿用发起人）。"""
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "a"}]])
    started = api.post(f"/api/v2/curated/{ds_id}/reviews", headers=auth_headers)
    assert started.status_code == 200, started.text
    review_id = started.json().get("data", started.json())["review_id"]
    # 发起端点未登记发起人：决定人必须由决定动作补写
    assert db.query(CuratedReview).filter(
        CuratedReview.id == review_id).one().reviewer_id is None

    approved = api.post(f"/api/v2/curated/reviews/{review_id}/approve",
                        params={"notes": "通过"}, headers=auth_headers)
    assert approved.status_code == 200, approved.text
    review = db.query(CuratedReview).filter(
        CuratedReview.id == review_id).one()
    assert review.status == "approved"
    assert review.reviewer_id == admin_user.id

    detail = api.get(f"/api/v2/curated/reviews/{review_id}", headers=auth_headers)
    body = detail.json().get("data", detail.json())
    assert body["reviewer_id"] == admin_user.id


def test_reject_endpoint_records_decider(api, auth_headers, db, admin_user):
    ds_id = _make_curated_with_versions(db, [[{"id": "1", "name": "a"}]])
    started = api.post(f"/api/v2/curated/{ds_id}/reviews", headers=auth_headers)
    assert started.status_code == 200, started.text
    review_id = started.json().get("data", started.json())["review_id"]
    r = api.post(f"/api/v2/curated/reviews/{review_id}/reject",
                 params={"notes": "质量不达标"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    review = db.query(CuratedReview).filter(
        CuratedReview.id == review_id).one()
    assert review.status == "rejected"
    assert review.reviewer_id == admin_user.id
