"""监控看板卡片：路由/权限/参数/revision/排序/对账/补偿/会话（SQLite 单测）。

Grafana 边界用 httpx.MockTransport 驱动的真实 ``GrafanaClient`` 桩（固定
镜像 PoC 实测端点形态）——单测覆盖业务分支与不确定网络结果的映射；跨进程
并发/域锁证明由 ``scripts/operations_dashboards_postgres_e2e.py`` 在真实
PostgreSQL 上承担，不以 SQLite 代替。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import uuid

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from app.auth.models import RoleMenuPermission, User
from app.operations import models as m
from app.operations.dashboards import (
    grafana_session,
    schemas as dashboards_schemas,
    service as dashboards_service,
)
from app.operations.grafana import client as grafana_client
from app.services.auth_service import hash_password
from app.shared import config as shared_config

BASE = "/api/v2/operations/dashboards"
STUB_PASSWORD = "StubSharedSecret2026!"

STUB_BASIC = "Basic " + base64.b64encode(
    f"admin:{STUB_PASSWORD}".encode()
).decode()

_CREATE_PATH = re.compile(r"^/grafana/api/dashboards/uid/([^/]+)$")


class StubGrafana:
    """固定镜像实测 API 面的最小桩：health/user/folder/create/delete/search。"""

    def __init__(self):
        self.uids: set[str] = set()
        self.folder_created = 0
        self.search_calls: list[list[str]] = []
        self.search_limits: list[int] = []
        self.fail_search = None  # None | "timeout" | "invalid" | "error" | "unauthorized"
        self.fail_create = None  # None | "timeout" | "error"
        self.fail_delete = None  # None | "timeout" | "error"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/grafana/api/health":
            return httpx.Response(
                200, json={"database": "ok", "version": "13.3.1"}
            )
        if request.headers.get("Authorization") != STUB_BASIC:
            return httpx.Response(401, json={"message": "unauthorized"})
        if path == "/grafana/api/user":
            return httpx.Response(
                200, json={"login": "admin", "isGrafanaAdmin": True}
            )
        if path == "/grafana/api/folders/openontology":
            if self.folder_created:
                return httpx.Response(
                    200, json={"uid": "openontology", "title": "OpenOntology 监控看板"}
                )
            return httpx.Response(404, json={"message": "folder not found"})
        if path == "/grafana/api/folders" and request.method == "POST":
            self.folder_created += 1
            return httpx.Response(200, json={"uid": "openontology"})
        if path == "/grafana/api/dashboards/db" and request.method == "POST":
            if self.fail_create == "timeout":
                raise httpx.ConnectError("connection refused upstream")
            if self.fail_create == "error":
                return httpx.Response(500, text=f"secret-upstream-body-{STUB_PASSWORD}")
            body = json.loads(request.content)
            uid = body["dashboard"]["uid"]
            assert body["dashboard"]["panels"] == []
            assert body["dashboard"]["schemaVersion"] == 40
            assert body["folderUid"] == "openontology"
            assert body["overwrite"] is False
            self.uids.add(uid)
            return httpx.Response(200, json={"id": 1, "uid": uid})
        match = _CREATE_PATH.match(path)
        if match and request.method == "DELETE":
            if self.fail_delete == "timeout":
                raise httpx.ConnectError("connection refused upstream")
            if self.fail_delete == "error":
                return httpx.Response(500, text="secret-upstream-body")
            uid = match.group(1)
            if uid in self.uids:
                self.uids.discard(uid)
                return httpx.Response(200, json={"message": "deleted"})
            return httpx.Response(404, json={"message": "not found"})
        if path == "/grafana/api/search":
            if self.fail_search == "timeout":
                raise httpx.ConnectError("connection refused upstream")
            if self.fail_search == "error":
                return httpx.Response(500, text="secret-upstream-body")
            if self.fail_search == "unauthorized":
                return httpx.Response(401, json={"message": "unauthorized"})
            if self.fail_search == "invalid":
                return httpx.Response(200, text="not-json-at-all")
            uids = request.url.params.get_list("dashboardUIDs")
            limit = int(request.url.params.get("limit"))
            self.search_calls.append(list(uids))
            self.search_limits.append(limit)
            found = [
                {"uid": uid, "title": uid, "url": f"/grafana/d/{uid}"}
                for uid in uids
                if uid in self.uids
            ]
            return httpx.Response(200, json=found[:limit])
        raise AssertionError(f"unexpected stub API: {request.method} {path}")


@pytest.fixture
def stub_grafana(monkeypatch):
    stub = StubGrafana()
    stub_transport = httpx.MockTransport(stub.handler)

    def build_client(*, transport=None):
        return grafana_client.GrafanaClient(
            base_url="http://grafana-stub",
            password=STUB_PASSWORD,
            transport=stub_transport,
        )

    monkeypatch.setattr(dashboards_service.grafana_client, "build_client", build_client)
    return stub


def _make_user(db, username: str, role: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        email=f"{username}@test.com",
        password_hash=hash_password("test123"),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "test123"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _seed_rows(db, count: int) -> list[m.OperationsDashboard]:
    rows = []
    for index in range(count):
        row = m.OperationsDashboard(
            id=str(uuid.uuid4()),
            name=f"看板 {index}",
            description="",
            icon="activity",
            grafana_uid=f"oo-seed{index:04d}",
            sort_order=index,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


CARD_FIELDS = {
    "id", "name", "description", "icon", "grafana_uid", "grafana_status",
    "edit_path", "preview_path", "sort_order", "revision", "created_at",
    "updated_at",
}


# ---------------------------------------------------------------------------
# C02/C04：权限、创建、列表、更新、删除
# ---------------------------------------------------------------------------


def test_create_list_update_delete_roundtrip(client, db, stub_grafana):
    _make_user(db, "dash_editor", "editor")
    headers = _login(client, "dash_editor")

    created = client.post(
        BASE, json={"name": "  运维总览 ", "description": " 核心指标 ", "icon": "gauge"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    card = created.json()["data"]
    assert set(card) == CARD_FIELDS
    assert card["name"] == "运维总览"
    assert card["description"] == "核心指标"
    assert card["icon"] == "gauge"
    assert card["grafana_status"] == "ok"
    assert card["grafana_uid"].startswith("oo-")
    assert card["edit_path"] == f"/grafana/d/{card['grafana_uid']}?orgId=1"
    assert card["preview_path"] == f"/grafana/d/{card['grafana_uid']}?orgId=1&kiosk"
    assert card["sort_order"] == 0
    assert card["revision"] == 1
    assert card["created_at"] and card["updated_at"]

    listed = client.get(BASE, headers=headers).json()["data"]
    assert listed["grafana_available"] is True
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == card["id"]

    updated = client.patch(
        f"{BASE}/{card['id']}",
        json={"name": "运维总览 v2", "description": "", "icon": "server",
              "expected_revision": 1},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    updated_card = updated.json()["data"]
    assert updated_card["revision"] == 2
    assert updated_card["name"] == "运维总览 v2"
    assert updated_card["icon"] == "server"
    # 平台改名只改卡片元数据：桩上除对账 search 外没有任何 dashboard 写请求。
    assert stub_grafana.uids == {card["grafana_uid"]}

    deleted = client.delete(f"{BASE}/{card['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"] == {"status": "deleted", "id": card["id"]}
    assert stub_grafana.uids == set()
    assert client.get(BASE, headers=headers).json()["data"]["total"] == 0


def test_create_appends_sort_order_and_allows_duplicate_names(client, db, stub_grafana):
    _make_user(db, "dup_editor", "editor")
    headers = _login(client, "dup_editor")
    first = client.post(BASE, json={"name": "同名看板", "icon": "activity"}, headers=headers)
    assert first.status_code == 201, first.text
    assert first.json()["data"]["sort_order"] == 0
    second = client.post(BASE, json={"name": "同名看板", "icon": "gauge"}, headers=headers)
    assert second.status_code == 201, second.text
    assert second.json()["data"]["sort_order"] == 1
    assert first.json()["data"]["grafana_uid"] != second.json()["data"]["grafana_uid"]


def test_write_roles_rejected_for_viewer_and_custom(client, db, stub_grafana):
    viewer = _make_user(db, "dash_viewer", "viewer")
    headers = _login(client, "dash_viewer")
    owner = _make_user(db, "owner_editor", "editor")
    owner_headers = _login(client, "owner_editor")
    card = client.post(BASE, json={"name": "基线", "icon": "activity"}, headers=owner_headers).json()["data"]

    for method, path, payload in (
        ("POST", BASE, {"name": "x", "icon": "activity"}),
        ("PATCH", f"{BASE}/{card['id']}", {"name": "x"}),
        ("DELETE", f"{BASE}/{card['id']}", None),
        ("POST", f"{BASE}/reorder", {"dashboard_ids": [card["id"]]}),
    ):
        denied = client.request(method, path, json=payload, headers=headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["code"] == "DASHBOARD_WRITE_DENIED"
    # viewer 仍可读与签发会话（菜单授权内，不要求写角色）。
    assert client.get(BASE, headers=headers).status_code == 200
    assert client.post(f"{BASE}/grafana-session", headers=headers).status_code == 204
    assert viewer.role == "viewer"


def test_menu_guard_and_custom_role_boundaries(client, db, stub_grafana):
    _make_user(db, "plain_custom", "custom")
    headers = _login(client, "plain_custom")
    assert client.get(BASE, headers=headers).status_code == 403
    assert client.post(f"{BASE}/grafana-session", headers=headers).status_code == 403

    db.add(RoleMenuPermission(role="custom", menu_keys=["operations.dashboard"]))
    db.commit()
    _make_user(db, "menu_custom", "custom")
    menu_headers = _login(client, "menu_custom")
    assert client.get(BASE, headers=menu_headers).status_code == 200
    assert client.post(f"{BASE}/grafana-session", headers=menu_headers).status_code == 204
    denied = client.post(
        BASE, json={"name": "x", "icon": "activity"}, headers=menu_headers
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "DASHBOARD_WRITE_DENIED"


def test_missing_bearer_rejected(client, db, stub_grafana):
    assert client.get(BASE).status_code == 403


# ---------------------------------------------------------------------------
# C03：参数校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_create_blank_name_rejected(client, db, stub_grafana, name):
    _make_user(db, "name_editor", "editor")
    headers = _login(client, "name_editor")
    rejected = client.post(
        BASE, json={"name": name, "icon": "activity"}, headers=headers
    )
    assert rejected.status_code == 422
    if name:
        # trim 后空白：业务错误码 dashboard_name_required
        assert rejected.json()["detail"]["code"] == "dashboard_name_required"
    else:
        # 空字符串：标准 Pydantic 422 结构，不伪造业务码
        assert isinstance(rejected.json()["detail"], list)


def test_create_name_length_boundaries(client, db, stub_grafana):
    _make_user(db, "len_editor", "editor")
    headers = _login(client, "len_editor")
    # C03：200 通过、201 拒绝（schema 级长度，标准 422 结构，不伪造业务码）。
    ok = client.post(BASE, json={"name": "名" * 200, "icon": "activity"}, headers=headers)
    assert ok.status_code == 201, ok.text
    too_long = client.post(BASE, json={"name": "名" * 201, "icon": "activity"}, headers=headers)
    assert too_long.status_code == 422
    assert isinstance(too_long.json()["detail"], list)
    trimmed = client.post(
        BASE, json={"name": "  " + "名" * 200 + "\t", "icon": "activity"},
        headers=headers,
    )
    assert trimmed.status_code == 201, trimmed.text
    trimmed_card = trimmed.json()["data"]
    assert trimmed_card["name"] == "名" * 200
    patched = client.patch(
        f"{BASE}/{trimmed_card['id']}",
        json={"name": "\t" + "改" * 200 + "  "}, headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["name"] == "改" * 200
    rejected_patch = client.patch(
        f"{BASE}/{trimmed_card['id']}", json={"name": "改" * 201}, headers=headers,
    )
    assert rejected_patch.status_code == 422


def test_description_and_icon_validation(client, db, stub_grafana):
    _make_user(db, "desc_editor", "editor")
    headers = _login(client, "desc_editor")
    assert client.post(
        BASE, json={"name": "x", "description": "描" * 500, "icon": "activity"},
        headers=headers,
    ).status_code == 201
    assert client.post(
        BASE, json={"name": "x", "description": "描" * 501, "icon": "activity"},
        headers=headers,
    ).status_code == 422
    trimmed = client.post(
        BASE,
        json={"name": "说明边界", "description": "  " + "描" * 500 + "\n", "icon": "activity"},
        headers=headers,
    )
    assert trimmed.status_code == 201, trimmed.text
    trimmed_card = trimmed.json()["data"]
    assert trimmed_card["description"] == "描" * 500
    patched = client.patch(
        f"{BASE}/{trimmed_card['id']}",
        json={"description": "\t" + "改" * 500 + "  "}, headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["description"] == "改" * 500
    rejected_patch = client.patch(
        f"{BASE}/{trimmed_card['id']}", json={"description": "改" * 501}, headers=headers,
    )
    assert rejected_patch.status_code == 422
    for icon in dashboards_schemas.DASHBOARD_ICONS:
        assert client.post(
            BASE, json={"name": f"卡片{icon}", "icon": icon}, headers=headers
        ).status_code == 201
    invalid = client.post(BASE, json={"name": "x", "icon": "rocket"}, headers=headers)
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "dashboard_icon_invalid"
    missing_icon = client.post(BASE, json={"name": "x"}, headers=headers)
    assert missing_icon.status_code == 422  # 标准结构 422，不伪造业务码


def test_update_explicit_none_and_empty_patch_semantics(client, db, stub_grafana):
    _make_user(db, "patch_editor", "editor")
    headers = _login(client, "patch_editor")
    card = client.post(BASE, json={"name": "初版", "icon": "activity"}, headers=headers).json()["data"]

    for field in ("name", "description", "icon", "expected_revision"):
        rejected = client.patch(
            f"{BASE}/{card['id']}", json={field: None}, headers=headers
        )
        assert rejected.status_code == 422, (field, rejected.text)

    empty = client.patch(f"{BASE}/{card['id']}", json={}, headers=headers)
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"]["revision"] == 1
    assert empty.json()["data"]["updated_at"] == card["updated_at"]

    same_value = client.patch(
        f"{BASE}/{card['id']}", json={"name": "初版"}, headers=headers
    )
    assert same_value.status_code == 200
    assert same_value.json()["data"]["revision"] == 2  # 有效更新即使值相同也 +1


def test_update_not_found_and_blank_name(client, db, stub_grafana):
    _make_user(db, "nf_editor", "editor")
    headers = _login(client, "nf_editor")
    missing = client.patch(
        f"{BASE}/no-such-id", json={"name": "x"}, headers=headers
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "dashboard_not_found"
    card = client.post(BASE, json={"name": "基线", "icon": "activity"}, headers=headers).json()["data"]
    blank = client.patch(f"{BASE}/{card['id']}", json={"name": "  "}, headers=headers)
    assert blank.status_code == 422
    assert blank.json()["detail"]["code"] == "dashboard_name_required"


# ---------------------------------------------------------------------------
# revision 冲突
# ---------------------------------------------------------------------------


def test_update_revision_conflict(client, db, stub_grafana):
    _make_user(db, "rev_editor", "editor")
    headers = _login(client, "rev_editor")
    card = client.post(BASE, json={"name": "并发基线", "icon": "activity"}, headers=headers).json()["data"]
    ok = client.patch(
        f"{BASE}/{card['id']}", json={"name": "新名", "expected_revision": 1},
        headers=headers,
    )
    assert ok.status_code == 200
    conflict = client.patch(
        f"{BASE}/{card['id']}", json={"name": "旧名", "expected_revision": 1},
        headers=headers,
    )
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["code"] == "dashboard_revision_conflict"
    assert detail["revision"] == 2
    assert "已被修改" in detail["message"]
    retried = client.patch(
        f"{BASE}/{card['id']}", json={"name": "重试名", "expected_revision": 2},
        headers=headers,
    )
    assert retried.status_code == 200
    assert retried.json()["data"]["name"] == "重试名"


# ---------------------------------------------------------------------------
# 全量排序
# ---------------------------------------------------------------------------


def _ids(client, headers):
    return [item["id"] for item in client.get(BASE, headers=headers).json()["data"]["items"]]


def test_reorder_exact_set_and_mismatch_errors(client, db, stub_grafana):
    _make_user(db, "order_editor", "editor")
    headers = _login(client, "order_editor")
    ids = []
    for index in range(3):
        ids.append(
            client.post(
                BASE, json={"name": f"卡片{index}", "icon": "activity"}, headers=headers
            ).json()["data"]["id"]
        )

    reordered = client.post(f"{BASE}/reorder", json={"dashboard_ids": ids[::-1]}, headers=headers)
    assert reordered.status_code == 200, reordered.text
    items = reordered.json()["data"]["items"]
    assert [item["id"] for item in items] == ids[::-1]
    assert [item["sort_order"] for item in items] == [0, 1, 2]
    assert _ids(client, headers) == ids[::-1]

    for payload in (
        {"dashboard_ids": ids[:2]},                                  # 遗漏
        {"dashboard_ids": ids + ids[:1]},                             # 重复
        {"dashboard_ids": ids + ["unknown-id"]},                      # 未知
        {"dashboard_ids": [ids[0], ids[0], ids[1]]},                  # 重复+遗漏
    ):
        mismatch = client.post(f"{BASE}/reorder", json=payload, headers=headers)
        assert mismatch.status_code == 409, payload
        assert mismatch.json()["detail"]["code"] == "dashboard_reorder_mismatch"
    # 失败后顺序保持不变。
    assert _ids(client, headers) == ids[::-1]


def test_reorder_empty_collection_and_hole_compaction(client, db, stub_grafana):
    _make_user(db, "hole_editor", "editor")
    headers = _login(client, "hole_editor")
    assert client.post(f"{BASE}/reorder", json={"dashboard_ids": []}, headers=headers).status_code == 200

    ids = [
        client.post(BASE, json={"name": f"洞{index}", "icon": "activity"}, headers=headers).json()["data"]["id"]
        for index in range(3)
    ]
    # 删除中间卡片留下 sort_order 空洞。
    assert client.delete(f"{BASE}/{ids[1]}", headers=headers).status_code == 200
    remaining = _ids(client, headers)
    assert remaining == [ids[0], ids[2]]
    # 位置未变的重排：压缩空洞为 0..N-1，但不动 revision/updated_at。
    before = client.get(BASE, headers=headers).json()["data"]["items"]
    same_order = client.post(f"{BASE}/reorder", json={"dashboard_ids": remaining}, headers=headers)
    assert same_order.status_code == 200
    after = client.get(BASE, headers=headers).json()["data"]["items"]
    assert [item["sort_order"] for item in after] == [0, 1]
    assert [(item["revision"], item["updated_at"]) for item in after] == [
        (item["revision"], item["updated_at"]) for item in before
    ]
    assert [item["id"] for item in after] == remaining


def test_reorder_moves_bump_revision_only_for_moved_rows(client, db, stub_grafana):
    _make_user(db, "move_editor", "editor")
    headers = _login(client, "move_editor")
    ids = [
        client.post(BASE, json={"name": f"移{index}", "icon": "activity"}, headers=headers).json()["data"]["id"]
        for index in range(3)
    ]
    before = {item["id"]: item for item in client.get(BASE, headers=headers).json()["data"]["items"]}
    # 只交换后两张：第一张位置不变（revision 不变），后两张 +1。
    swapped = client.post(f"{BASE}/reorder", json={"dashboard_ids": [ids[0], ids[2], ids[1]]}, headers=headers)
    assert swapped.status_code == 200
    after = {item["id"]: item for item in client.get(BASE, headers=headers).json()["data"]["items"]}
    assert after[ids[0]]["revision"] == before[ids[0]]["revision"]
    assert after[ids[1]]["revision"] == before[ids[1]]["revision"] + 1
    assert after[ids[2]]["revision"] == before[ids[2]]["revision"] + 1


# ---------------------------------------------------------------------------
# C05：不确定网络结果与三态对账
# ---------------------------------------------------------------------------


def test_reconciliation_missing_and_chunking(client, db, stub_grafana):
    _make_user(db, "rec_editor", "editor")
    headers = _login(client, "rec_editor")
    rows = _seed_rows(db, 250)
    for row in rows:
        stub_grafana.uids.add(row.grafana_uid)

    listed = client.get(BASE, headers=headers).json()["data"]
    assert listed["grafana_available"] is True
    assert listed["total"] == 250
    assert all(item["grafana_status"] == "ok" for item in listed["items"])
    # 分块对账：3 块（100+100+50），每块 limit ≥ 块大小（统一 100）。
    assert [len(chunk) for chunk in stub_grafana.search_calls] == [100, 100, 50]
    assert stub_grafana.search_limits == [100, 100, 100]

    # 远端删除一张（如原生界面删除）：完整成功对账 → missing。
    stub_grafana.uids.discard(rows[0].grafana_uid)
    listed = client.get(BASE, headers=headers).json()["data"]
    by_uid = {item["grafana_uid"]: item for item in listed["items"]}
    assert by_uid[rows[0].grafana_uid]["grafana_status"] == "missing"
    assert by_uid[rows[1].grafana_uid]["grafana_status"] == "ok"


@pytest.mark.parametrize("failure", ["timeout", "error", "unauthorized", "invalid"])
def test_any_failed_chunk_makes_all_unknown(client, db, stub_grafana, failure):
    _make_user(db, "unk_editor", "editor")
    headers = _login(client, "unk_editor")
    rows = _seed_rows(db, 3)
    for row in rows:
        stub_grafana.uids.add(row.grafana_uid)
    stub_grafana.fail_search = failure

    listed = client.get(BASE, headers=headers).json()["data"]
    assert listed["grafana_available"] is False
    assert all(item["grafana_status"] == "unknown" for item in listed["items"])
    assert listed["total"] == 3


def test_probe_failure_does_not_block_metadata_writes(client, db, stub_grafana):
    _make_user(db, "probe_editor", "editor")
    headers = _login(client, "probe_editor")
    card = client.post(BASE, json={"name": "探测", "icon": "activity"}, headers=headers).json()["data"]
    stub_grafana.fail_search = "error"

    patched = client.patch(
        f"{BASE}/{card['id']}", json={"name": "探测改", "expected_revision": 1},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["grafana_status"] == "unknown"
    assert patched.json()["data"]["name"] == "探测改"

    ids = [item["id"] for item in client.get(BASE, headers=headers).json()["data"]["items"]]
    reordered = client.post(f"{BASE}/reorder", json={"dashboard_ids": ids}, headers=headers)
    assert reordered.status_code == 200
    assert reordered.json()["data"]["items"][0]["grafana_status"] == "unknown"


def test_missing_grafana_config_fails_closed_at_use_time(client, db, monkeypatch):
    monkeypatch.setattr(shared_config.settings, "grafana_admin_password", "")
    _make_user(db, "cfg_editor", "editor")
    headers = _login(client, "cfg_editor")
    created = client.post(BASE, json={"name": "配置缺失", "icon": "activity"}, headers=headers)
    assert created.status_code == 503
    assert created.json()["detail"]["code"] == "grafana_not_configured"
    listed = client.get(BASE, headers=headers).json()["data"]
    assert listed["grafana_available"] is False


def test_grafana_error_mapping_is_sanitized(client, db, stub_grafana, caplog):
    _make_user(db, "err_editor", "editor")
    headers = _login(client, "err_editor")

    with caplog.at_level(logging.WARNING, logger="app.operations.dashboards"):
        stub_grafana.fail_create = "timeout"
        unreachable = client.post(BASE, json={"name": "超时", "icon": "activity"}, headers=headers)
        assert unreachable.status_code == 502
        assert unreachable.json()["detail"]["code"] == "grafana_unavailable"
        timeout_logs = [
            record.getMessage()
            for record in caplog.records
            if "GRAFANA_CREATE_FAILED" in record.getMessage()
        ]
        assert len(timeout_logs) == 1
        assert "stage=create" in timeout_logs[0]
        assert re.search(r"uid=oo-[a-f0-9]{12}", timeout_logs[0])
        assert "error_type=GrafanaUnavailable" in timeout_logs[0]
        assert STUB_PASSWORD not in timeout_logs[0]
        caplog.clear()

        stub_grafana.fail_create = "error"
        failed = client.post(BASE, json={"name": "上游错", "icon": "activity"}, headers=headers)
        assert failed.status_code == 502
        detail = failed.json()["detail"]
        assert detail["code"] == "grafana_operation_failed"
        assert detail["grafana_status"] == 500
        assert "secret-upstream-body" not in failed.text
        assert STUB_PASSWORD not in failed.text
        operation_logs = [
            record.getMessage()
            for record in caplog.records
            if "GRAFANA_CREATE_FAILED" in record.getMessage()
        ]
        assert len(operation_logs) == 1
        assert "stage=create" in operation_logs[0]
        assert re.search(r"uid=oo-[a-f0-9]{12}", operation_logs[0])
        assert "error_type=GrafanaOperationFailed" in operation_logs[0]
        assert "secret-upstream-body" not in operation_logs[0]
        assert STUB_PASSWORD not in operation_logs[0]

    # 删除遇到不可达：本地卡片必须保留。
    stub_grafana.fail_create = None
    card = client.post(BASE, json={"name": "保留", "icon": "activity"}, headers=headers).json()["data"]
    stub_grafana.fail_delete = "timeout"
    denied = client.delete(f"{BASE}/{card['id']}", headers=headers)
    assert denied.status_code == 502
    assert denied.json()["detail"]["code"] == "grafana_unavailable"
    assert client.get(BASE, headers=headers).json()["data"]["total"] == 1


# ---------------------------------------------------------------------------
# C06：跨系统失败与补偿（受控故障注入，非端到端）
# ---------------------------------------------------------------------------


def test_create_db_failure_compensates_remote(client, db, stub_grafana, monkeypatch, caplog):
    _make_user(db, "fail_editor", "editor")
    headers = _login(client, "fail_editor")

    def broken_commit():
        raise RuntimeError("controlled local commit failure")

    monkeypatch.setattr(db, "commit", broken_commit)
    with caplog.at_level(logging.WARNING, logger="app.operations.dashboards"):
        failed = client.post(BASE, json={"name": "补偿", "icon": "activity"}, headers=headers)
    assert failed.status_code == 500
    assert failed.json()["detail"]["code"] == "dashboard_persist_failed"
    # 远端已创建的空看板必须补偿删除；本地无行；日志记录阶段与结果。
    assert stub_grafana.uids == set()
    assert db.query(m.OperationsDashboard).count() == 0
    compensation_logs = [
        record.getMessage()
        for record in caplog.records
        if "GRAFANA_COMPENSATION" in record.getMessage()
    ]
    assert any(
        "stage=create_persist_failed" in message and "result=deleted" in message
        for message in compensation_logs
    )
    # 日志带 UID 便于追踪，但绝不含凭据。
    assert all("uid=oo-" in message for message in compensation_logs)
    assert all(STUB_PASSWORD not in message for message in compensation_logs)


def test_create_compensation_failure_logs_uid_only(client, db, stub_grafana, monkeypatch, caplog):
    _make_user(db, "cf_editor", "editor")
    headers = _login(client, "cf_editor")
    stub_grafana.fail_delete = "error"

    def broken_commit():
        raise RuntimeError("controlled local commit failure")

    monkeypatch.setattr(db, "commit", broken_commit)
    with caplog.at_level(logging.ERROR, logger="app.operations.dashboards"):
        failed = client.post(BASE, json={"name": "孤儿", "icon": "activity"}, headers=headers)
    assert failed.status_code == 500
    assert failed.json()["detail"]["code"] == "dashboard_persist_failed"
    # 补偿失败：远端 UID 保留（潜在孤儿），日志记录精确 UID。
    assert len(stub_grafana.uids) == 1
    orphan_uid = next(iter(stub_grafana.uids))
    assert orphan_uid.startswith("oo-")
    messages = [record.getMessage() for record in caplog.records]
    assert any("GRAFANA_COMPENSATION_FAILED" in message and orphan_uid in message
               for message in messages)
    assert all(STUB_PASSWORD not in message for message in messages)


def test_delete_db_failure_keeps_card_and_retry_completes(client, db, stub_grafana, monkeypatch):
    _make_user(db, "del_editor", "editor")
    headers = _login(client, "del_editor")
    card = client.post(BASE, json={"name": "删除", "icon": "activity"}, headers=headers).json()["data"]

    real_commit = db.commit

    def broken_commit():
        raise RuntimeError("controlled local commit failure")

    monkeypatch.setattr(db, "commit", broken_commit)
    failed = client.delete(f"{BASE}/{card['id']}", headers=headers)
    assert failed.status_code == 500
    assert failed.json()["detail"]["code"] == "dashboard_persist_failed"
    # 远端已删、本地保留：列表显示 missing，可重试完成删除。
    assert stub_grafana.uids == set()
    monkeypatch.setattr(db, "commit", real_commit)
    listed = client.get(BASE, headers=headers).json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["grafana_status"] == "missing"
    retried = client.delete(f"{BASE}/{card['id']}", headers=headers)
    assert retried.status_code == 200
    assert client.get(BASE, headers=headers).json()["data"]["total"] == 0


@pytest.mark.parametrize("operation", ["create", "delete", "reorder"])
def test_domain_lock_timeout_rolls_back_and_retry_succeeds(
    client, db, stub_grafana, monkeypatch, operation,
):
    _make_user(db, "busy_editor", "editor")
    headers = _login(client, "busy_editor")
    rows = _seed_rows(db, 2)
    ids = [row.id for row in rows]
    stub_grafana.uids.update(row.grafana_uid for row in rows)
    existing_uids = set(stub_grafana.uids)
    rollback_calls = []
    real_rollback = db.rollback

    def rollback():
        rollback_calls.append(True)
        real_rollback()

    def busy(_db):
        raise dashboards_service._WriteBusy()

    def write():
        if operation == "create":
            return client.post(
                BASE, json={"name": "锁释放后重试", "icon": "activity"}, headers=headers,
            )
        if operation == "delete":
            return client.delete(f"{BASE}/{ids[0]}", headers=headers)
        return client.post(
            f"{BASE}/reorder", json={"dashboard_ids": ids[::-1]}, headers=headers,
        )

    with monkeypatch.context() as patch:
        patch.setattr(dashboards_service, "_begin_write_transaction", busy)
        patch.setattr(db, "rollback", rollback)
        failed = write()
    assert failed.status_code == 409, failed.text
    assert failed.json()["detail"]["code"] == "dashboard_write_busy"
    assert rollback_calls
    assert stub_grafana.uids == existing_uids
    unchanged = db.query(m.OperationsDashboard).order_by(m.OperationsDashboard.sort_order).all()
    assert [row.id for row in unchanged] == ids
    assert [row.revision for row in unchanged] == [1, 1]
    retried = write()
    assert retried.status_code == (201 if operation == "create" else 200), retried.text


@pytest.mark.parametrize("operation", ["create", "update", "delete", "reorder"])
def test_database_write_lock_timeout_rolls_back_and_retry_succeeds(
    client, db, stub_grafana, monkeypatch, operation,
):
    _make_user(db, "write_busy_editor", "editor")
    headers = _login(client, "write_busy_editor")
    rows = _seed_rows(db, 2)
    ids = [row.id for row in rows]
    stub_grafana.uids.update(row.grafana_uid for row in rows)
    existing_uids = set(stub_grafana.uids)
    rollback_calls = []
    real_rollback = db.rollback

    class LockTimeout(Exception):
        sqlstate = "55P03"

    def broken_commit():
        raise OperationalError("controlled write", {}, LockTimeout("lock_timeout"))

    def rollback():
        rollback_calls.append(True)
        real_rollback()

    def write():
        if operation == "create":
            return client.post(
                BASE, json={"name": "写入重试", "icon": "activity"}, headers=headers,
            )
        if operation == "update":
            return client.patch(
                f"{BASE}/{ids[0]}",
                json={"name": "更新重试", "expected_revision": 1}, headers=headers,
            )
        if operation == "delete":
            return client.delete(f"{BASE}/{ids[0]}", headers=headers)
        return client.post(
            f"{BASE}/reorder", json={"dashboard_ids": ids[::-1]}, headers=headers,
        )

    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", broken_commit)
        patch.setattr(db, "rollback", rollback)
        failed = write()
    assert failed.status_code == 409, failed.text
    assert failed.json()["detail"]["code"] == "dashboard_write_busy"
    assert rollback_calls
    unchanged = db.query(m.OperationsDashboard).order_by(m.OperationsDashboard.sort_order).all()
    assert [row.id for row in unchanged] == ids
    assert [row.name for row in unchanged] == ["看板 0", "看板 1"]
    assert [row.revision for row in unchanged] == [1, 1]
    # 创建的远端看板已补偿；删除提交失败则保留本地卡片，404 重试仍可完成。
    expected_uids = existing_uids - {rows[0].grafana_uid} if operation == "delete" else existing_uids
    assert stub_grafana.uids == expected_uids
    retried = write()
    assert retried.status_code == (201 if operation == "create" else 200), retried.text


# ---------------------------------------------------------------------------
# C07：独立看板会话 cookie
# ---------------------------------------------------------------------------


def _issue_session_cookie(client, headers, extra_headers=None):
    response = client.post(
        f"{BASE}/grafana-session", headers={**headers, **(extra_headers or {})}
    )
    assert response.status_code == 204, response.text
    assert response.content == b""
    return response.headers.get("set-cookie", "")


def test_grafana_session_cookie_attributes_and_roundtrip(client, db, stub_grafana):
    _make_user(db, "sess_editor", "editor")
    headers = _login(client, "sess_editor")
    cookie_header = _issue_session_cookie(client, headers)
    assert "oo_grafana_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "Path=/grafana/" in cookie_header
    assert "SameSite=lax" in cookie_header or "samesite=lax" in cookie_header
    assert "Max-Age=28800" in cookie_header
    assert "Secure" not in cookie_header
    assert "Domain=" not in cookie_header

    token = cookie_header.split("oo_grafana_session=", 1)[1].split(";", 1)[0]
    claims = grafana_session.verify_session(token)
    assert claims is not None
    user = db.query(User).filter(User.username == "sess_editor").one()
    assert claims["uid"] == user.id
    assert claims["role"] == "editor"


def test_grafana_session_cookie_secure_under_trusted_proxy(client, db, stub_grafana):
    _make_user(db, "sec_editor", "editor")
    headers = _login(client, "sec_editor")
    cookie_header = _issue_session_cookie(
        client, headers, {"x-forwarded-proto": "https, http"}
    )
    assert "Secure" in cookie_header


def test_grafana_session_requires_bearer_and_menu_only(client, db, stub_grafana):
    _make_user(db, "sess_custom", "custom")
    headers = _login(client, "sess_custom")
    # custom 默认无 operations.dashboard 菜单：签发被拒。
    assert client.post(f"{BASE}/grafana-session", headers=headers).status_code == 403
    db.add(RoleMenuPermission(role="custom", menu_keys=["operations.dashboard"]))
    db.commit()
    assert client.post(f"{BASE}/grafana-session", headers=headers).status_code == 204
    # 无 Bearer 一律拒绝。
    assert client.post(f"{BASE}/grafana-session").status_code == 403


def test_grafana_auth_uses_cookie_only(client, db, stub_grafana):
    _make_user(db, "auth_editor", "editor")
    headers = _login(client, "auth_editor")
    cookie_header = _issue_session_cookie(client, headers)
    token = cookie_header.split("oo_grafana_session=", 1)[1].split(";", 1)[0]

    # 不带 Bearer（甚至带垃圾 Bearer）也能通过：本端点只依赖 cookie。
    ok = client.get(
        f"{BASE}/grafana-auth",
        headers={"Cookie": f"oo_grafana_session={token}"},
    )
    assert ok.status_code == 204
    assert ok.headers.get("x-grafana-user") == shared_config.settings.grafana_admin_user
    assert ok.content == b""
    ok_garbage_bearer = client.get(
        f"{BASE}/grafana-auth",
        headers={
            "Cookie": f"oo_grafana_session={token}",
            "Authorization": "Bearer garbage",
        },
    )
    assert ok_garbage_bearer.status_code == 204

    # Path=/grafana/ 的 cookie 不会自然随 /api 请求发送；显式携带才生效。
    assert client.get(f"{BASE}/grafana-auth").status_code == 401
    tampered = client.get(
        f"{BASE}/grafana-auth",
        headers={"Cookie": f"oo_grafana_session={token[:-2]}xy"},
    )
    assert tampered.status_code == 401
    assert "grafana_session_invalid" in tampered.text


def test_grafana_session_verify_rejects_structural_attacks():
    token, _exp = grafana_session.issue_session(
        user_id="u-1", role="viewer", ttl_seconds=28800
    )
    assert grafana_session.verify_session(token) is not None
    # 损坏结构
    assert grafana_session.verify_session("") is None
    assert grafana_session.verify_session("no-separator") is None
    assert grafana_session.verify_session(token + ".extra") is None
    assert grafana_session.verify_session("!!!illegal!!!." + token.split(".")[1]) is None
    assert grafana_session.verify_session(token.split(".")[0] + ".!!!") is None
    # 篡改 payload（签名不匹配）
    body, mac = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["role"] = "admin"
    forged_body = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    assert grafana_session.verify_session(f"{forged_body}.{mac}") is None


def _token_with_claims(claims: dict) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    mac = grafana_session._encode(grafana_session._digest(body))
    return f"{body}.{mac}"


def test_grafana_session_verify_requires_exact_claim_types():
    base_claims = {"exp": 10 ** 12, "jti": "j-1", "role": "viewer", "uid": "u-1"}
    assert grafana_session.verify_session(_token_with_claims(base_claims)) is not None
    # bool 冒充 exp 必须拒绝
    bool_exp = dict(base_claims, exp=True)
    assert grafana_session.verify_session(_token_with_claims(bool_exp)) is None
    assert grafana_session.verify_session(
        _token_with_claims(dict(base_claims, exp=10 ** 12 + 0.5))
    ) is None  # float exp
    assert grafana_session.verify_session(
        _token_with_claims(dict(base_claims, exp=str(10 ** 12)))
    ) is None  # str exp
    for key in ("uid", "role", "jti"):
        assert grafana_session.verify_session(
            _token_with_claims(dict(base_claims, **{key: ""}))
        ) is None
        assert grafana_session.verify_session(
            _token_with_claims(dict(base_claims, **{key: 123}))
        ) is None
    assert grafana_session.verify_session("e30.signature-only") is None


def test_grafana_session_domain_prefix_isolation():
    """与平台其他 HMAC 用途互不通用：无前缀或其他域前缀签名一律拒绝。"""
    claims = {"exp": 10 ** 12, "jti": "j-1", "role": "viewer", "uid": "u-1"}
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")

    def _sign(prefix: str) -> str:
        mac = base64.urlsafe_b64encode(
            hmac.new(
                shared_config.settings.secret_key.encode(),
                f"{prefix}{body}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode().rstrip("=")
        return f"{body}.{mac}"

    assert grafana_session.verify_session(_sign("")) is None
    assert grafana_session.verify_session(_sign("kaneo:sso:ticket:")) is None
    assert grafana_session.verify_session(
        _sign(grafana_session.DOMAIN_PREFIX)
    ) is not None


def test_grafana_session_expiry_with_fixed_clock(monkeypatch):
    class FixedClock:
        def __init__(self, now: int):
            self.now = now

        def time(self):
            return self.now

    clock = FixedClock(1000)
    monkeypatch.setattr(grafana_session, "time", clock)
    token, exp = grafana_session.issue_session(
        user_id="u-1", role="viewer", ttl_seconds=28800
    )
    assert exp == 1000 + 28800
    assert grafana_session.verify_session(token) is not None
    clock.now = exp - 1
    assert grafana_session.verify_session(token) is not None
    clock.now = exp
    assert grafana_session.verify_session(token) is None
    clock.now = exp + 1
    assert grafana_session.verify_session(token) is None


def test_issued_session_survives_platform_logout_and_revocation(db):
    """已签发 cookie 不回查用户库/平台 token 撤销状态（冻结契约 §3）。"""
    token, _exp = grafana_session.issue_session(
        user_id="gone-user", role="viewer", ttl_seconds=28800
    )
    assert grafana_session.verify_session(token) is not None
    assert db.query(User).filter(User.id == "gone-user").count() == 0
    # 平台端退出/改密/撤销 Bearer 都不进入校验路径。
    assert grafana_session.verify_session(token) is not None


# ---------------------------------------------------------------------------
# 配置秘密边界（C08）
# ---------------------------------------------------------------------------


def test_grafana_password_hidden_from_repr_and_worker_import():
    settings = shared_config.settings
    assert settings.grafana_admin_password == ""
    assert "grafana_admin_password" not in repr(settings)
    assert settings.grafana_base_url == "http://grafana:3000"
    assert settings.grafana_timeout_seconds == 15
    assert settings.grafana_session_ttl_seconds == 28800
    assert settings.grafana_folder_uid == "openontology"
    assert settings.grafana_folder_title == "OpenOntology 监控看板"


def test_grafana_client_not_configured_at_construction():
    with pytest.raises(grafana_client.GrafanaNotConfigured):
        grafana_client.GrafanaClient(password="")
