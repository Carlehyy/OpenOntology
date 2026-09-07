import hashlib
import io
import uuid

import pytest

from app.auth.models import User
from app.auth.service import hash_password
from app.data_channel.datasets.sharing_models import ManualDatasetChange, ManualDatasetShare
from app.data_channel.datasets import router as datasets_module
from app.data_channel.datasets import sharing_router as sharing_module
from app.main import app
from app.shared.encryption import decrypt


CSV = "编号,名称,数量\nA1,苹果,10\nA2,香蕉,20\n"


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, bucket, key, data, content_type=""):
        uri = f"s3://{bucket}/{key}"
        self.objects[uri] = data
        return uri

    def get_object(self, uri):
        return self.objects[uri]

    def delete_object(self, uri):
        self.objects.pop(uri, None)


@pytest.fixture
def api(client, db, monkeypatch):
    storage = FakeStorage()
    from app.data_channel.datasets import service as dataset_service
    monkeypatch.setattr(dataset_service, "get_storage_service", lambda: storage)

    def override_db():
        yield db

    app.dependency_overrides[datasets_module.get_db] = override_db
    app.dependency_overrides[sharing_module.get_db] = override_db
    yield client
    app.dependency_overrides.pop(datasets_module.get_db, None)
    app.dependency_overrides.pop(sharing_module.get_db, None)


def _dataset(api, auth_headers):
    response = api.post(
        "/api/v2/datasets/upload",
        files={"file": ("inventory.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=auth_headers,
    )
    dataset_id = response.json()["data"]["id"]
    declared = api.put(
        f"/api/v2/datasets/{dataset_id}/contract",
        json={"primary_key": "编号"}, headers=auth_headers)
    assert declared.status_code == 200
    return dataset_id


def _share(api, auth_headers, dataset_id, permission="edit"):
    response = api.post(
        f"/api/v2/manual-dataset-sharing/{dataset_id}/shares",
        json={"permission": permission, "label": "供应商维护", "expires_in_days": 30},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_view_link_is_anonymous_read_only_and_token_is_encrypted_for_reuse(api, auth_headers, db):
    dataset_id = _dataset(api, auth_headers)
    created = _share(api, auth_headers, dataset_id, "view")

    public = api.get(f"/api/public/manual-datasets/{created['token']}")
    assert public.status_code == 200
    payload = public.json()
    assert payload["dataset"]["name"] == "inventory"
    assert payload["share"]["permission"] == "view"
    assert payload["changes"] == []
    assert payload["dataset"]["column_meta"]["编号"] == {
        "display_name": "编号",
        "nullable": False,
    }

    second_page = api.get(
        f"/api/public/manual-datasets/{created['token']}?limit=1&offset=1",
    )
    assert second_page.status_code == 200
    assert second_page.json()["dataset"]["total_rows"] == 2
    assert [row["编号"] for row in second_page.json()["dataset"]["rows"]] == ["A2"]

    denied = api.post(f"/api/public/manual-datasets/{created['token']}/changes", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A1"}, "values": {"数量": "99"}}],
    })
    assert denied.status_code == 403

    record = db.query(ManualDatasetShare).filter(ManualDatasetShare.id == created["id"]).one()
    assert record.token_hash == hashlib.sha256(created["token"].encode()).hexdigest()
    assert created["token"] not in record.token_hash
    assert created["token"] not in record.token_encrypted
    assert decrypt(record.token_encrypted) == created["token"]

    listed = api.get(
        f"/api/v2/manual-dataset-sharing/{dataset_id}/shares",
        headers=auth_headers,
    )
    assert listed.status_code == 200
    assert listed.json()[0]["token"] == created["token"]

    revoked = api.delete(
        f"/api/v2/manual-dataset-sharing/shares/{created['id']}",
        headers=auth_headers,
    )
    assert revoked.status_code == 200
    assert api.get(f"/api/public/manual-datasets/{created['token']}").status_code == 404


def test_external_edit_is_validated_then_waits_for_approval_before_new_version(api, auth_headers, db):
    dataset_id = _dataset(api, auth_headers)
    created = _share(api, auth_headers, dataset_id)
    token = created["token"]

    invalid = api.post(f"/api/public/manual-datasets/{token}/changes", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A1"}, "values": {"编号": "A2"}}],
    })
    assert invalid.status_code == 400
    assert "主键列" in str(invalid.json()["detail"])
    assert "不允许修改" in str(invalid.json()["detail"])
    assert db.query(ManualDatasetChange).count() == 0

    submitted = api.post(f"/api/public/manual-datasets/{token}/changes", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A1"}, "values": {"数量": "99"}}],
        "inserts": [{"values": {"编号": "A3", "名称": "橙子", "数量": "7"}}],
        "deletes": [{"key": {"编号": "A2"}}],
    })
    assert submitted.status_code == 201, submitted.text
    change_id = submitted.json()["id"]
    versions = api.get(f"/api/v2/datasets/{dataset_id}/versions", headers=auth_headers).json()
    assert len(versions) == 1  # pending proposals are not visible to lake consumers

    progress = api.get(f"/api/public/manual-datasets/{token}").json()["changes"]
    assert progress[0]["status"] == "pending"
    duplicate_submission = api.post(f"/api/public/manual-datasets/{token}/changes", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A1"}, "values": {"数量": "100"}}],
    })
    assert duplicate_submission.status_code == 409
    assert "正在审批" in duplicate_submission.json()["detail"]["message"]

    approved = api.post(
        f"/api/v2/manual-dataset-sharing/changes/{change_id}/review",
        json={"decision": "approve", "comment": "核对通过"}, headers=auth_headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["applied_version_no"] == 2

    preview = api.get(f"/api/v2/datasets/{dataset_id}/preview", headers=auth_headers).json()
    rows = {row["编号"]: row for row in preview["rows"]}
    assert rows["A1"]["数量"] == "99"
    assert rows["A3"]["名称"] == "橙子"
    assert "A2" not in rows
    progress = api.get(f"/api/public/manual-datasets/{token}").json()["changes"]
    assert progress[0]["status"] == "approved"
    assert progress[0]["review_comment"] == "核对通过"


def test_rejection_requires_reason_and_reason_is_visible_to_external_editor(api, auth_headers):
    dataset_id = _dataset(api, auth_headers)
    token = _share(api, auth_headers, dataset_id)["token"]
    submitted = api.post(f"/api/public/manual-datasets/{token}/changes", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A1"}, "values": {"数量": "88"}}],
    }).json()

    missing_reason = api.post(
        f"/api/v2/manual-dataset-sharing/changes/{submitted['id']}/review",
        json={"decision": "reject", "comment": ""}, headers=auth_headers)
    assert missing_reason.status_code == 400

    rejected = api.post(
        f"/api/v2/manual-dataset-sharing/changes/{submitted['id']}/review",
        json={"decision": "reject", "comment": "数量缺少来源依据"}, headers=auth_headers)
    assert rejected.status_code == 200
    progress = api.get(f"/api/public/manual-datasets/{token}").json()["changes"][0]
    assert progress["status"] == "rejected"
    assert progress["review_comment"] == "数量缺少来源依据"


def test_approval_refuses_to_overwrite_a_newer_dataset_version(api, auth_headers):
    dataset_id = _dataset(api, auth_headers)
    token = _share(api, auth_headers, dataset_id)["token"]
    submitted = api.post(f"/api/public/manual-datasets/{token}/changes", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A1"}, "values": {"数量": "88"}}],
    }).json()

    direct = api.post(f"/api/v2/datasets/{dataset_id}/rows/edit", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A2"}, "values": {"数量": "21"}}],
    }, headers=auth_headers)
    assert direct.status_code == 200

    conflict = api.post(
        f"/api/v2/manual-dataset-sharing/changes/{submitted['id']}/review",
        json={"decision": "approve", "comment": ""}, headers=auth_headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "manual_share_base_version_conflict"


def test_management_changes_support_pagination_status_and_search_filters(api, auth_headers):
    dataset_id = _dataset(api, auth_headers)

    rejected_token = _share(api, auth_headers, dataset_id)["token"]
    rejected_change = api.post(f"/api/public/manual-datasets/{rejected_token}/changes", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A1"}, "values": {"数量": "11"}}],
    }).json()
    rejected = api.post(
        f"/api/v2/manual-dataset-sharing/changes/{rejected_change['id']}/review",
        json={"decision": "reject", "comment": "缺少依据"}, headers=auth_headers)
    assert rejected.status_code == 200

    approved_token = _share(api, auth_headers, dataset_id)["token"]
    approved_change = api.post(f"/api/public/manual-datasets/{approved_token}/changes", json={
        "base_version_no": 1,
        "updates": [{"key": {"编号": "A1"}, "values": {"数量": "12"}}],
    }).json()
    approved = api.post(
        f"/api/v2/manual-dataset-sharing/changes/{approved_change['id']}/review",
        json={"decision": "approve", "comment": "通过"}, headers=auth_headers)
    assert approved.status_code == 200

    pending_token = _share(api, auth_headers, dataset_id)["token"]
    pending = api.post(f"/api/public/manual-datasets/{pending_token}/changes", json={
        "base_version_no": 2,
        "updates": [{"key": {"编号": "A2"}, "values": {"数量": "22"}}],
    })
    assert pending.status_code == 201

    pending_page = api.get(
        "/api/v2/manual-dataset-sharing/changes?status=pending&page=1&page_size=1",
        headers=auth_headers,
    )
    assert pending_page.status_code == 200
    assert pending_page.json()["total"] == 1
    assert pending_page.json()["page"] == 1
    assert pending_page.json()["page_size"] == 1
    assert [item["status"] for item in pending_page.json()["items"]] == ["pending"]

    first_page = api.get(
        "/api/v2/manual-dataset-sharing/changes?page=1&page_size=2&search=inventory",
        headers=auth_headers,
    ).json()
    second_page = api.get(
        "/api/v2/manual-dataset-sharing/changes?page=2&page_size=2&search=inventory",
        headers=auth_headers,
    ).json()
    assert first_page["total"] == 3
    assert len(first_page["items"]) == 2
    assert len(second_page["items"]) == 1

    no_match = api.get(
        "/api/v2/manual-dataset-sharing/changes?search=不存在的数据集",
        headers=auth_headers,
    ).json()
    assert no_match["total"] == 0
    assert no_match["items"] == []

    invalid_status = api.get(
        "/api/v2/manual-dataset-sharing/changes?status=unknown",
        headers=auth_headers,
    )
    assert invalid_status.status_code == 400


def _login_user(api, username, password):
    login = api.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['data']['access_token']}"}


def _create_user(db, api, username, password, role):
    # 用户管理 API 已退役；测试用户直写数据库后走真实登录链路拿 token
    db.add(User(id=str(uuid.uuid4()), username=username, email=f"{username}@test.com",
                password_hash=hash_password(password), role=role))
    db.commit()
    return _login_user(api, username, password)


def test_share_listing_and_revocation_are_scoped_to_creator_or_admin(api, auth_headers, db):
    """属主语义（对齐 file_assets）：列表按创建者过滤、吊销限创建者/admin。
    此前任意 data 菜单用户可枚举全部分享并解出 token、吊销任意分享。"""
    dataset_id = _dataset(api, auth_headers)
    admin_share = _share(api, auth_headers, dataset_id, "view")

    editor_headers = _create_user(db, api, "share_editor", "editor123", "editor")
    editor_share = _share(api, editor_headers, dataset_id, "view")
    viewer_headers = _create_user(db, api, "share_viewer", "viewer123", "viewer")

    # viewer（只读）枚举不到任何他人分享与 token
    viewer_listed = api.get(
        f"/api/v2/manual-dataset-sharing/{dataset_id}/shares", headers=viewer_headers)
    assert viewer_listed.status_code == 200
    assert viewer_listed.json() == []

    # 普通用户列表只见自己创建的分享，token 仅对可见行恢复
    editor_listed = api.get(
        f"/api/v2/manual-dataset-sharing/{dataset_id}/shares", headers=editor_headers)
    assert editor_listed.status_code == 200
    assert [row["id"] for row in editor_listed.json()] == [editor_share["id"]]
    assert editor_listed.json()[0]["token"] == editor_share["token"]

    # 普通用户不能吊销他人分享；自己的可吊销
    denied = api.delete(
        f"/api/v2/manual-dataset-sharing/shares/{admin_share['id']}",
        headers=editor_headers,
    )
    assert denied.status_code == 403
    own = api.delete(
        f"/api/v2/manual-dataset-sharing/shares/{editor_share['id']}",
        headers=editor_headers,
    )
    assert own.status_code == 200

    # admin 全量可见、可吊销任何分享
    admin_listed = api.get(
        f"/api/v2/manual-dataset-sharing/{dataset_id}/shares", headers=auth_headers)
    assert [row["id"] for row in admin_listed.json()] == [editor_share["id"], admin_share["id"]]
    revoked = api.delete(
        f"/api/v2/manual-dataset-sharing/shares/{admin_share['id']}",
        headers=auth_headers,
    )
    assert revoked.status_code == 200
