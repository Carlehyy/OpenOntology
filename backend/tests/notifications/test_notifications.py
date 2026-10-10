"""消息通知域 — API 集成测试

覆盖：创建/校验、tab 列表与游标分页、详情自动已读、处置状态（标记/归档/
未读）、全部已读、单条删除（连带附件落盘文件）、附件上传下载/白名单/大小
上限、跨管理员状态隔离、event_id 幂等（service 级，对外投递接口 M2 前置）。
对抗场景：未登录/非管理员越权、伪造游标、非法 tab、越权附件下载、超限正文。
"""
from __future__ import annotations

import hashlib

from app.config import settings
from app.notifications import service as notification_service


def _create(client, headers, *, title="测试消息", body="# 正文\n内容", priority="normal"):
    resp = client.post(
        "/api/v2/notifications",
        json={"title": title, "body": body, "priority": priority},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _editor_headers(client):
    resp = client.post(
        "/api/v1/auth/login", json={"username": "editor", "password": "editor123"}
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


# ── 创建与权限 ─────────────────────────────────────────────────


def test_create_manual_message(client, auth_headers):
    data = _create(client, auth_headers, title="任务失败", body="## 详情\n失败原因", priority="urgent")
    assert data["title"] == "任务失败"
    assert data["priority"] == "urgent"
    assert data["sourceType"] == "manual"
    assert data["sourceSystem"] == "platform"
    assert data["body"] == "## 详情\n失败原因"
    assert data["isRead"] is True  # 发送者本人查看即已读
    assert data["attachments"] == []


def test_create_requires_authentication(client):
    # 平台约定：未携带令牌统一 403（deps.get_current_user 显式抛出）
    assert client.get("/api/v2/notifications").status_code == 403
    assert client.post("/api/v2/notifications", json={"title": "x"}).status_code == 403


def test_create_requires_admin(client, editor_user):
    resp = client.post(
        "/api/v2/notifications",
        json={"title": "越权"},
        headers=_editor_headers(client),
    )
    assert resp.status_code == 403


def test_create_validation_errors(client, auth_headers):
    assert client.post("/api/v2/notifications", json={"title": ""}, headers=auth_headers).status_code == 422
    assert (
        client.post(
            "/api/v2/notifications",
            json={"title": "x", "priority": "critical"},
            headers=auth_headers,
        ).status_code
        == 422
    )
    # 524289 字符超过 schema 上限
    assert (
        client.post(
            "/api/v2/notifications",
            json={"title": "x", "body": "a" * 524289},
            headers=auth_headers,
        ).status_code
        == 422
    )
    # service 层按字节兜底（中文正文按 UTF-8 字节数计，字符数未超 pydantic 上限）
    assert (
        client.post(
            "/api/v2/notifications",
            json={"title": "x", "body": "中" * (524288 // 3 + 1)},
            headers=auth_headers,
        ).status_code
        == 413
    )


# ── 列表 / 汇总 / tab 语义 ────────────────────────────────────


def _seed_three_states(client, headers):
    """A=未读、B=已读+已标记、C=已读+已归档。"""
    a = _create(client, headers, title="A-未读告警", priority="high")
    b = _create(client, headers, title="B-重要消息")
    c = _create(client, headers, title="C-噪音消息")
    for message_id, patch in (
        (a["id"], {"isRead": False}),
        (b["id"], {"isRead": True, "isStarred": True}),
        (c["id"], {"isRead": True, "isArchived": True}),
    ):
        resp = client.patch(f"/api/v2/notifications/{message_id}", json=patch, headers=headers)
        assert resp.status_code == 200, resp.text
    return a, b, c


def test_list_tabs_and_summary(client, auth_headers):
    a, b, c = _seed_three_states(client, auth_headers)

    def _ids(tab):
        resp = client.get(f"/api/v2/notifications?tab={tab}", headers=auth_headers)
        assert resp.status_code == 200
        return [item["id"] for item in resp.json()["data"]["items"]]

    assert set(_ids("all")) == {a["id"], b["id"]}      # 归档不进默认视图
    assert _ids("unread") == [a["id"]]
    assert _ids("starred") == [b["id"]]
    assert _ids("archived") == [c["id"]]

    summary = client.get("/api/v2/notifications/summary", headers=auth_headers).json()["data"]
    assert summary == {"unreadCount": 1, "starredCount": 1, "archivedCount": 1, "totalCount": 2}

    # 列表条目只带预览与计数，不携带全文
    listing = client.get("/api/v2/notifications?tab=all", headers=auth_headers).json()["data"]["items"]
    assert listing, "默认视图不应为空"
    item = listing[0]
    assert "body" not in item
    assert "bodyPreview" in item
    assert "attachmentCount" in item


def test_list_pagination_cursor(client, auth_headers):
    created = [_create(client, auth_headers, title=f"分页-{i}") for i in range(5)]
    expected = [row["id"] for row in reversed(created)]

    collected = []
    cursor = None
    for _ in range(3):
        url = "/api/v2/notifications?limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        resp = client.get(url, headers=auth_headers)
        assert resp.status_code == 200
        payload = resp.json()["data"]
        collected.extend(item["id"] for item in payload["items"])
        assert payload["hasMore"] is (len(payload["items"]) == 2)
        cursor = payload["nextCursor"]
        if not cursor:
            break
    assert collected == expected  # 按创建时间倒序、无重无漏


def test_list_rejects_invalid_tab_and_cursor(client, auth_headers):
    assert (
        client.get("/api/v2/notifications?tab=deleted", headers=auth_headers).status_code
        == 400
    )
    assert (
        client.get("/api/v2/notifications?cursor=%21%21%21", headers=auth_headers).status_code
        == 400
    )


# ── 详情自动已读 + 状态操作 ───────────────────────────────────


def test_detail_marks_read_and_can_revert(client, auth_headers):
    message = _create(client, auth_headers)
    client.patch(f"/api/v2/notifications/{message['id']}", json={"isRead": False}, headers=auth_headers)

    detail = client.get(f"/api/v2/notifications/{message['id']}", headers=auth_headers).json()["data"]
    assert detail["isRead"] is True and detail["readAt"]

    again = client.patch(
        f"/api/v2/notifications/{message['id']}", json={"isRead": False}, headers=auth_headers
    ).json()["data"]
    assert again["isRead"] is False and again["readAt"] is None


def test_patch_state_partial_update(client, auth_headers):
    message = _create(client, auth_headers)
    base = client.patch(
        f"/api/v2/notifications/{message['id']}",
        json={"isStarred": True},
        headers=auth_headers,
    ).json()["data"]
    assert base["isStarred"] is True and base["starredAt"]
    assert base["isRead"] is True  # 未携带字段保持原值

    archived = client.patch(
        f"/api/v2/notifications/{message['id']}", json={"isArchived": True}, headers=auth_headers
    ).json()["data"]
    assert archived["isArchived"] is True and archived["isStarred"] is True  # 标记与归档互不影响

    unstarred = client.patch(
        f"/api/v2/notifications/{message['id']}", json={"isStarred": False}, headers=auth_headers
    ).json()["data"]
    assert unstarred["isStarred"] is False and unstarred["starredAt"] is None


def test_patch_rejects_empty_and_missing(client, auth_headers):
    message = _create(client, auth_headers)
    assert (
        client.patch(f"/api/v2/notifications/{message['id']}", json={}, headers=auth_headers).status_code
        == 422
    )
    assert client.patch("/api/v2/notifications/missing", json={"isRead": True},
                        headers=auth_headers).status_code == 404


def test_read_all(client, auth_headers):
    messages = [_create(client, auth_headers) for _ in range(3)]
    for row in messages:
        client.patch(f"/api/v2/notifications/{row['id']}", json={"isRead": False, "isStarred": True},
                     headers=auth_headers)

    result = client.post("/api/v2/notifications/read-all", headers=auth_headers).json()["data"]
    assert result["updated"] == 3
    summary = client.get("/api/v2/notifications/summary", headers=auth_headers).json()["data"]
    assert summary["unreadCount"] == 0 and summary["starredCount"] == 3  # 标记不受影响


def test_state_isolation_between_admins(client, auth_headers, admin_user, db):
    from app.services.auth_service import hash_password
    from app.models.user import User
    import uuid as uuid_mod

    other = User(id=str(uuid_mod.uuid4()), username="admin2", email="admin2@test.com",
                 password_hash=hash_password("admin123"), role="admin")
    db.add(other); db.commit()
    token = client.post("/api/v1/auth/login",
                        json={"username": "admin2", "password": "admin123"}).json()["data"]["access_token"]
    other_headers = {"Authorization": f"Bearer {token}"}

    message = _create(client, auth_headers)
    client.patch(f"/api/v2/notifications/{message['id']}",
                 json={"isRead": True, "isStarred": True}, headers=auth_headers)

    other_view = client.get(f"/api/v2/notifications/{message['id']}", headers=other_headers).json()["data"]
    assert other_view["isStarred"] is False  # 管理员之间状态互不可见
    assert other_view["isRead"] is True      # 查看即已读（自己的状态行）


# ── 删除 ──────────────────────────────────────────────────────


def test_delete_message_with_attachments(client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    message = _create(client, auth_headers)
    upload = {"file": ("note.txt", b"secret content", "text/plain")}
    att = client.post(f"/api/v2/notifications/{message['id']}/attachments",
                      files=upload, headers=auth_headers).json()["data"]

    resp = client.delete(f"/api/v2/notifications/{message['id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert client.get(f"/api/v2/notifications/{message['id']}", headers=auth_headers).status_code == 404
    # 附件端点与落盘文件一并清理（消息目录残留时应为空目录）
    assert client.get(
        f"/api/v2/notifications/{message['id']}/attachments/{att['id']}/download",
        headers=auth_headers,
    ).status_code == 404
    import os
    msg_dir = os.path.join(str(tmp_path), "notifications", message["id"])
    assert not os.path.exists(msg_dir) or os.listdir(msg_dir) == []
    assert client.delete(f"/api/v2/notifications/{message['id']}", headers=auth_headers).status_code == 404


# ── 附件 ──────────────────────────────────────────────────────


def test_attachment_upload_and_download(client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    message = _create(client, auth_headers)

    payload = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    resp = client.post(
        f"/api/v2/notifications/{message['id']}/attachments",
        files={"file": ("图表.png", payload, "image/png")},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    att = resp.json()["data"]
    assert att["sha256"] == hashlib.sha256(payload).hexdigest()
    assert att["fileSize"] == len(payload)
    assert att["url"].endswith(f"/notifications/{message['id']}/attachments/{att['id']}/download")

    # 列表计数与详情附件清单
    listing = client.get("/api/v2/notifications", headers=auth_headers).json()["data"]["items"]
    assert listing[0]["attachmentCount"] == 1
    detail = client.get(f"/api/v2/notifications/{message['id']}", headers=auth_headers).json()["data"]
    assert detail["attachments"][0]["filename"] == "图表.png"

    # 下载内容一致
    download = client.get(att["url"], headers=auth_headers)
    assert download.status_code == 200 and download.content == payload


def test_attachment_download_rejects_foreign_message(client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    m1 = _create(client, auth_headers)
    m2 = _create(client, auth_headers)
    att = client.post(f"/api/v2/notifications/{m1['id']}/attachments",
                      files={"file": ("a.txt", b"x", "text/plain")},
                      headers=auth_headers).json()["data"]
    # 用另一条消息的 id 取该附件 → 404，防止跨消息附件枚举
    assert client.get(
        f"/api/v2/notifications/{m2['id']}/attachments/{att['id']}/download",
        headers=auth_headers,
    ).status_code == 404


def test_attachment_extension_whitelist(client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    monkeypatch.setattr(settings, "notification_attachment_extensions", "png,txt")
    message = _create(client, auth_headers)
    resp = client.post(
        f"/api/v2/notifications/{message['id']}/attachments",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        headers=auth_headers,
    )
    assert resp.status_code == 400


def test_attachment_size_limit_cleans_partial_file(client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    message = _create(client, auth_headers)
    resp = client.post(
        f"/api/v2/notifications/{message['id']}/attachments",
        files={"file": ("big.bin", b"x" * 1024, "application/octet-stream")},
        headers=auth_headers,
    )
    assert resp.status_code == 413
    import os
    assert os.listdir(os.path.join(str(tmp_path), "notifications", message["id"])) == []


def test_attachments_require_admin(client, auth_headers, editor_user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    message = _create(client, auth_headers)
    resp = client.post(
        f"/api/v2/notifications/{message['id']}/attachments",
        files={"file": ("a.txt", b"x", "text/plain")},
        headers=_editor_headers(client),
    )
    assert resp.status_code == 403
    assert client.get("/api/v2/notifications/summary", headers=_editor_headers(client)).status_code == 403


# ── event_id 幂等（service 级；对外投递接口 M2 复用）─────────


def test_create_event_id_idempotent(db, admin_user):
    first = notification_service.create_message(
        db, title="外部事件", body_md="v1", event_id="ext:incident-1",
        source_system="billing", source_type="ingest", user=admin_user,
    )
    second = notification_service.create_message(
        db, title="外部事件(重放)", body_md="v1-replayed", event_id="ext:incident-1",
        source_system="billing", source_type="ingest", user=admin_user,
    )
    assert first.id == second.id  # 同 event_id 重放返回既有消息，不新建

    distinct = notification_service.create_message(
        db, title="另一事件", event_id="ext:incident-2",
        source_system="billing", source_type="ingest",
    )
    assert distinct.id != first.id


def test_internal_publish_helper(db, admin_user):
    message = notification_service.publish_notification(
        db, title="任务池告警", body_md="任务 X 失败", priority="high",
    )
    assert message.source_type == "internal"
    assert message.source_system == "platform"


# ── 对抗式审查补充用例（游标丢行 / 孤儿行 / 越权 / 文件丢失 / 边界）──


def test_cursor_same_timestamp_no_drops(client, auth_headers, db):
    """同一微秒时刻创建的多条消息，翻页必须无重无漏（毫秒截断回归）。"""
    from datetime import datetime

    from app.notifications.models import NotificationMessage

    created = [_create(client, auth_headers, title=f"同刻-{i}") for i in range(3)]
    same_moment = datetime(2026, 10, 9, 12, 0, 0, 123456)
    db.query(NotificationMessage).filter(
        NotificationMessage.id.in_([row["id"] for row in created])
    ).update({NotificationMessage.created_at: same_moment}, synchronize_session=False)
    db.commit()

    collected, cursor = [], None
    while True:
        url = "/api/v2/notifications?limit=1"
        if cursor:
            url += f"&cursor={cursor}"
        payload = client.get(url, headers=auth_headers).json()["data"]
        collected.extend(item["id"] for item in payload["items"])
        cursor = payload["nextCursor"]
        if not cursor:
            break
    assert sorted(collected) == sorted(row["id"] for row in created)


def test_delete_leaves_no_orphan_rows(client, auth_headers, db, tmp_path, monkeypatch):
    """SQLite 不强制外键：删除必须显式清理状态行/附件行，不留孤儿。"""
    from app.notifications.models import (
        NotificationAttachment,
        NotificationMessageState,
    )

    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    message = _create(client, auth_headers)
    client.patch(f"/api/v2/notifications/{message['id']}", json={"isStarred": True},
                 headers=auth_headers)
    client.post(f"/api/v2/notifications/{message['id']}/attachments",
                files={"file": ("a.txt", b"x", "text/plain")}, headers=auth_headers)

    client.delete(f"/api/v2/notifications/{message['id']}", headers=auth_headers)

    assert db.query(NotificationMessageState).filter_by(message_id=message["id"]).count() == 0
    assert db.query(NotificationAttachment).filter_by(message_id=message["id"]).count() == 0


def test_read_patch_delete_require_admin(client, auth_headers, editor_user):
    message = _create(client, auth_headers)
    headers = _editor_headers(client)
    assert client.get(f"/api/v2/notifications/{message['id']}", headers=headers).status_code == 403
    assert client.patch(f"/api/v2/notifications/{message['id']}",
                        json={"isRead": True}, headers=headers).status_code == 403
    assert client.delete(f"/api/v2/notifications/{message['id']}",
                         headers=headers).status_code == 403
    assert client.post("/api/v2/notifications/read-all", headers=headers).status_code == 403


def test_attachment_file_missing_returns_410(client, auth_headers, tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    message = _create(client, auth_headers)
    att = client.post(f"/api/v2/notifications/{message['id']}/attachments",
                      files={"file": ("a.txt", b"x", "text/plain")},
                      headers=auth_headers).json()["data"]
    # 直接删掉落盘文件模拟存储故障：行还在、文件没了 → 410 而非 500
    for root, _dirs, files in os.walk(str(tmp_path)):
        for name in files:
            os.remove(os.path.join(root, name))
    assert client.get(att["url"], headers=auth_headers).status_code == 410


def test_title_byte_boundary_service_level(db, admin_user):
    # 300 个中文字符 = 900 字节，恰好达标；301 个 = 903 字节拒绝
    ok = notification_service.create_message(db, title="标" * 300, user=admin_user)
    assert ok.title
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        notification_service.create_message(db, title="标" * 301, user=admin_user)
    assert exc.value.status_code == 422


def test_event_id_scoped_by_source_system(db, admin_user):
    """幂等键按 (source_system, event_id) 作用域：不同系统的同名事件互不冲突。"""
    billing = notification_service.create_message(
        db, title="billing-1", event_id="incident-1", source_system="billing",
        source_type="ingest",
    )
    crm = notification_service.create_message(
        db, title="crm-1", event_id="incident-1", source_system="crm",
        source_type="ingest",
    )
    assert billing.id != crm.id
    replay = notification_service.create_message(
        db, title="billing-1-replay", event_id="incident-1", source_system="billing",
        source_type="ingest",
    )
    assert replay.id == billing.id


# ── M2：对外投递接口（X-API-Key）──────────────────────────────

INGEST = "/api/v2/notifications/ingest"


def _mint_key(client, headers, *, name="billing", allowed=None):
    payload = {"name": name}
    if allowed is not None:
        payload["allowedSourceSystem"] = allowed
    resp = client.post("/api/v2/notifications/ingest-keys", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _ingest_headers(key: dict) -> dict:
    return {"X-API-Key": key["plaintextKey"]}


def test_ingest_key_lifecycle(client, auth_headers):
    key = _mint_key(client, auth_headers)
    # 明文一次性返回：前缀可识别、列表永不携带明文
    assert key["plaintextKey"].startswith(f"{key['keyPrefix']}_")
    listed = client.get("/api/v2/notifications/ingest-keys", headers=auth_headers).json()["data"]
    assert [row["id"] for row in listed] == [key["id"]]
    assert all("plaintextKey" not in row for row in listed)
    assert key["allowedSourceSystem"] is None

    # 吊销后立即失效；重复吊销 404 语义由不存在处理
    assert client.delete(f"/api/v2/notifications/ingest-keys/{key['id']}", headers=auth_headers).status_code == 200
    revoked = client.get("/api/v2/notifications/ingest-keys", headers=auth_headers).json()["data"][0]
    assert revoked["enabled"] is False and revoked["revokedAt"]
    assert client.post(INGEST, json={"title": "x"}, headers=_ingest_headers(key)).status_code == 401
    assert client.delete("/api/v2/notifications/ingest-keys/missing", headers=auth_headers).status_code == 404


def test_ingest_key_management_requires_admin(client, auth_headers, editor_user):
    headers = _editor_headers(client)
    assert client.get("/api/v2/notifications/ingest-keys", headers=headers).status_code == 403
    assert client.post("/api/v2/notifications/ingest-keys", json={"name": "x"}, headers=headers).status_code == 403
    _mint_key(client, auth_headers)
    key_id = client.get("/api/v2/notifications/ingest-keys", headers=auth_headers).json()["data"][0]["id"]
    assert client.delete(f"/api/v2/notifications/ingest-keys/{key_id}", headers=headers).status_code == 403


def test_ingest_message_creates_and_idempotent(client, auth_headers):
    key = _mint_key(client, auth_headers, name="billing")
    headers = _ingest_headers(key)

    first = client.post(INGEST, json={
        "eventId": "incident-1", "title": "账单异常", "body": "# 详情\n环比 +300%",
        "priority": "urgent",
    }, headers=headers)
    assert first.status_code == 200, first.text
    payload = first.json()["data"]
    assert payload["idempotent"] is False
    message = payload["message"]
    assert message["sourceType"] == "ingest"
    assert message["sourceSystem"] == "billing"  # 缺省回退密钥名
    assert message["eventId"] == "incident-1"

    # 重放同 eventId：幂等命中，不新建
    replay = client.post(INGEST, json={
        "eventId": "incident-1", "title": "账单异常（重发）", "body": "changed",
    }, headers=headers).json()["data"]
    assert replay["idempotent"] is True
    assert replay["message"]["id"] == message["id"]
    assert replay["message"]["title"] == "账单异常"

    # 站内可见且未读；last_used_at 已更新
    detail = client.get(f"/api/v2/notifications/{message['id']}", headers=auth_headers).json()["data"]
    assert detail["body"] == "# 详情\n环比 +300%"
    listed = client.get("/api/v2/notifications/ingest-keys", headers=auth_headers).json()["data"]
    assert listed[0]["lastUsedAt"] is not None


def test_ingest_auth_failures(client, auth_headers):
    _mint_key(client, auth_headers)
    assert client.post(INGEST, json={"title": "x"}).status_code == 401  # 无密钥
    assert client.post(INGEST, json={"title": "x"}, headers={"X-API-Key": "ob_notif_wrong_secret"}).status_code == 401
    # 畸形 JSON / 非对象
    key = _mint_key(client, auth_headers, name="k2")
    headers = _ingest_headers(key)
    resp = client.post(INGEST, content=b"not-json", headers=headers)
    assert resp.status_code == 422
    resp = client.post(INGEST, json=["array"], headers=headers)
    assert resp.status_code == 422


def test_ingest_scope_and_validation(client, auth_headers):
    scoped = _mint_key(client, auth_headers, name="crm", allowed="crm")
    headers = _ingest_headers(scoped)
    ok = client.post(INGEST, json={"sourceSystem": "crm", "title": "客户提醒"}, headers=headers)
    assert ok.status_code == 200
    # 伪装其它来源系统 → 403
    assert client.post(INGEST, json={"sourceSystem": "billing", "title": "越权"}, headers=headers).status_code == 403
    # 校验：空标题 / 非法优先级
    assert client.post(INGEST, json={"title": " "}, headers=headers).status_code == 422
    assert client.post(INGEST, json={"title": "x", "priority": "critical"}, headers=headers).status_code == 422


def test_ingest_attachment_flow(client, auth_headers):
    key = _mint_key(client, auth_headers, name="billing")
    headers = _ingest_headers(key)
    message = client.post(INGEST, json={"eventId": "inc-9", "title": "带附件告警"}, headers=headers).json()["data"]["message"]

    att = client.post(
        f"{INGEST}/{message['id']}/attachments",
        files={"file": ("证据.png", b"\x89PNG-bytes", "image/png")},
        headers=headers,
    )
    assert att.status_code == 201, att.text
    data = att.json()["data"]
    assert data["url"].endswith(f"/notifications/{message['id']}/attachments/{data['id']}/download")

    # 管理员站内可见并可下载（JWT）；附件来源以密钥前缀留痕
    detail = client.get(f"/api/v2/notifications/{message['id']}", headers=auth_headers).json()["data"]
    assert detail["attachments"][0]["filename"] == "证据.png"
    download = client.get(data["url"], headers=auth_headers)
    assert download.status_code == 200 and download.content == b"\x89PNG-bytes"


def test_ingest_attachment_rejects_non_ingest_message(client, auth_headers, tmp_path, monkeypatch):
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "uploads_dir", str(tmp_path))
    key = _mint_key(client, auth_headers, name="billing")
    headers = _ingest_headers(key)

    manual = _create(client, auth_headers)  # sourceType=manual
    resp = client.post(
        f"{INGEST}/{manual['id']}/attachments",
        files={"file": ("a.txt", b"x", "text/plain")},
        headers=headers,
    )
    assert resp.status_code == 403  # 仅 ingest 消息可经密钥追加附件


def test_ingest_does_not_mark_read(client, auth_headers):
    key = _mint_key(client, auth_headers, name="billing")
    message = client.post(INGEST, json={"title": "未读保持"}, headers=_ingest_headers(key)).json()["data"]["message"]
    summary = client.get("/api/v2/notifications/summary", headers=auth_headers).json()["data"]
    assert summary["unreadCount"] >= 1  # 投递不改变管理员的阅读状态


# ── M2 对抗审查补充：跨密钥注入 / 类型与长度 / 作用域缺省 / 归属留痕 ──


def test_ingest_attachment_cross_key_injection_blocked(client, auth_headers, tmp_path, monkeypatch):
    """密钥 B 不能向密钥 A 投递的消息注入附件（归属列防线）。"""
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    key_a = _mint_key(client, auth_headers, name="billing")
    key_b = _mint_key(client, auth_headers, name="crm")
    message = client.post(INGEST, json={"title": "A 的告警"}, headers=_ingest_headers(key_a)).json()["data"]["message"]

    resp = client.post(
        f"{INGEST}/{message['id']}/attachments",
        files={"file": ("evil.txt", b"x", "text/plain")},
        headers=_ingest_headers(key_b),
    )
    assert resp.status_code == 403
    # 密钥 A 本人仍可追加
    ok = client.post(
        f"{INGEST}/{message['id']}/attachments",
        files={"file": ("ok.txt", b"x", "text/plain")},
        headers=_ingest_headers(key_a),
    )
    assert ok.status_code == 201


def test_ingest_rejects_oversized_event_id_and_non_string(client, auth_headers):
    key = _mint_key(client, auth_headers, name="billing")
    headers = _ingest_headers(key)
    # 超 255 字符的 eventId 直接 422（不静默截断成撞幂等键）
    assert client.post(INGEST, json={"title": "x", "eventId": "A" * 256}, headers=headers).status_code == 422
    # 非字符串字段 422（不做 Python repr 落库）
    assert client.post(INGEST, json={"title": {"nested": 1}}, headers=headers).status_code == 422
    assert client.post(INGEST, json={"title": "x", "body": ["list"]}, headers=headers).status_code == 422
    assert client.post(INGEST, json={"title": "x", "priority": 3}, headers=headers).status_code == 422


def test_scoped_key_defaults_source_system_to_scope(client, auth_headers):
    """作用域密钥缺省 sourceSystem 时强制为作用域（显式异名仍 403）。"""
    scoped = _mint_key(client, auth_headers, name="crm-key", allowed="crm")
    headers = _ingest_headers(scoped)
    ok = client.post(INGEST, json={"title": "缺省来源"}, headers=headers)
    assert ok.status_code == 200
    assert ok.json()["data"]["message"]["sourceSystem"] == "crm"
    denied = client.post(INGEST, json={"title": "x", "sourceSystem": "billing"}, headers=headers)
    assert denied.status_code == 403


def test_ingest_attachment_records_key_prefix_provenance(client, auth_headers, db, tmp_path, monkeypatch):
    """附件来源以密钥前缀留痕，且随附件行同事务一次提交。"""
    from app.notifications.models import NotificationAttachment as AttModel

    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    key = _mint_key(client, auth_headers, name="billing")
    message = client.post(INGEST, json={"title": "溯源"}, headers=_ingest_headers(key)).json()["data"]["message"]
    att = client.post(
        f"{INGEST}/{message['id']}/attachments",
        files={"file": ("a.txt", b"x", "text/plain")},
        headers=_ingest_headers(key),
    ).json()["data"]
    record = db.query(AttModel).filter(AttModel.id == att["id"]).first()
    assert record is not None
    assert record.uploaded_by == key["keyPrefix"]
    assert record.uploaded_by.startswith("ob_notif_")


def test_revoked_key_last_used_at_frozen(client, auth_headers):
    key = _mint_key(client, auth_headers, name="billing")
    headers = _ingest_headers(key)
    client.post(INGEST, json={"title": "first"}, headers=headers)
    client.delete(f"/api/v2/notifications/ingest-keys/{key['id']}", headers=auth_headers)
    used_at = client.get("/api/v2/notifications/ingest-keys", headers=auth_headers).json()["data"][0]["lastUsedAt"]
    assert client.post(INGEST, json={"title": "after revoke"}, headers=headers).status_code == 401
    frozen = client.get("/api/v2/notifications/ingest-keys", headers=auth_headers).json()["data"][0]["lastUsedAt"]
    assert frozen == used_at


# ── M3：渠道转发（apprise，测试内 mock 发送函数）──────────────

CHANNELS = "/api/v2/notifications/channels"


def _make_channel(client, headers, *, name="钉钉运维群", url="json://hooks.example/abc", enabled=True):
    resp = client.post(CHANNELS, json={"name": name, "template": "custom", "params": {"url": url}}, headers=headers)
    assert resp.status_code == 201, resp.text
    row = resp.json()["data"]
    if not enabled:
        row = client.patch(f"{CHANNELS}/{row['id']}", json={"enabled": False}, headers=headers).json()["data"]
    return row


def test_channel_crud_and_masking(client, auth_headers, db):
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationChannel

    row = _make_channel(client, auth_headers)
    # 响应与列表永不携带 URL 明文/密文/参数明文
    assert "appriseUrl" not in row and "apprise_url_encrypted" not in row
    assert row["template"] == "custom" and row["templateName"]
    listed = client.get(CHANNELS, headers=auth_headers).json()["data"]
    assert len(listed) == 1

    # 密文落库且可解回原文（Fernet）
    record = db.query(NotificationChannel).first()
    assert record.apprise_url_encrypted != "json://hooks.example/abc"
    assert cs.channel_url(record) == "json://hooks.example/abc"
    assert cs.mask_apprise_url("json://hooks.example/abc12345") == "json://…2345"
    assert cs.mask_apprise_url("json://abc") == "json://…"  # 过短时只留协议

    # 重名 409；改名/启停/换 URL
    assert client.post(CHANNELS, json={"name": "钉钉运维群", "template": "custom", "params": {"url": "json://x"}}, headers=auth_headers).status_code == 409
    updated = client.patch(f"{CHANNELS}/{row['id']}", json={"name": "值班群", "enabled": False}, headers=auth_headers).json()["data"]
    assert updated["name"] == "值班群" and updated["enabled"] is False
    client.patch(f"{CHANNELS}/{row['id']}", json={"params": {"url": "json://hooks.example/new"}}, headers=auth_headers)
    assert cs.channel_url(db.query(NotificationChannel).first()) == "json://hooks.example/new"

    assert client.delete(f"{CHANNELS}/{row['id']}", headers=auth_headers).status_code == 200
    assert client.get(CHANNELS, headers=auth_headers).json()["data"] == []
    assert client.delete(f"{CHANNELS}/{row['id']}", headers=auth_headers).status_code == 404


def test_channel_validation(client, auth_headers):
    assert client.post(CHANNELS, json={"name": "x", "template": "custom", "params": {"url": "no-scheme"}}, headers=auth_headers).status_code == 422
    assert client.post(CHANNELS, json={"name": "", "template": "custom", "params": {"url": "json://a"}}, headers=auth_headers).status_code == 422
    assert client.get(CHANNELS, headers=auth_headers).status_code == 200


def test_channel_requires_admin(client, auth_headers, editor_user):
    headers = _editor_headers(client)
    assert client.get(CHANNELS, headers=headers).status_code == 403
    assert client.post(CHANNELS, json={"name": "x", "template": "custom", "params": {"url": "json://a"}}, headers=headers).status_code == 403


def test_fan_out_on_create_and_no_duplicate_on_replay(client, auth_headers, db, monkeypatch):
    from app.notifications.models import NotificationDelivery

    _make_channel(client, auth_headers, name="渠道A")
    _make_channel(client, auth_headers, name="渠道B", enabled=False)  # 停用渠道不扇出

    key = _mint_key(client, auth_headers, name="billing")
    message = client.post(INGEST, json={"eventId": "inc-fan", "title": "扇出"}, headers=_ingest_headers(key)).json()["data"]["message"]

    rows = db.query(NotificationDelivery).filter_by(message_id=message["id"]).all()
    assert len(rows) == 1 and rows[0].status == "pending"  # 仅启用渠道

    # 幂等重放不重复扇出、也不产生新投递单
    client.post(INGEST, json={"eventId": "inc-fan", "title": "扇出重放"}, headers=_ingest_headers(key))
    assert db.query(NotificationDelivery).filter_by(message_id=message["id"]).count() == 1

    # 手动发送同样扇出
    manual = _create(client, auth_headers)
    assert db.query(NotificationDelivery).filter_by(message_id=manual["id"]).count() == 1


def test_dispatch_success_marks_sent(client, auth_headers, db, monkeypatch):
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationChannel, NotificationDelivery

    channel = _make_channel(client, auth_headers)
    sent_calls: list[tuple[str, str, str]] = []

    def fake_send(url, *, title, body):
        sent_calls.append((url, title, body))
        return None

    monkeypatch.setattr(cs, "_send_via_apprise", fake_send)
    _create(client, auth_headers, title="转发成功", body="# 正文")

    result = cs.dispatch_pending_deliveries(db)
    assert result["sent"] == 1 and result["failed"] == 0
    delivery = db.query(NotificationDelivery).first()
    assert delivery.status == "sent" and delivery.sent_at and delivery.last_error == ""
    record = db.query(NotificationChannel).first()
    assert record.last_status == "sent" and record.last_sent_at
    assert sent_calls[0][0] == "json://hooks.example/abc"
    assert sent_calls[0][1] == "转发成功"
    assert sent_calls[0][2].startswith("# 正文")


def test_dispatch_retry_then_failed(client, auth_headers, db, monkeypatch):
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationChannel, NotificationDelivery

    _make_channel(client, auth_headers)

    def boom(url, *, title, body):
        raise RuntimeError("connection timeout")

    monkeypatch.setattr(cs, "_send_via_apprise", boom)
    _create(client, auth_headers, title="会失败的消息")

    # 前两次：仍 pending 等待重试
    cs.dispatch_pending_deliveries(db)
    cs.dispatch_pending_deliveries(db)
    delivery = db.query(NotificationDelivery).first()
    assert delivery.status == "pending" and delivery.attempts == 2 and "timeout" in delivery.last_error

    # 第三次：达上限转终态 failed，渠道最近状态同步
    cs.dispatch_pending_deliveries(db)
    assert delivery.status == "failed" and delivery.attempts == 3
    assert db.query(NotificationChannel).first().last_status == "failed"


def test_channel_test_send_direct(client, auth_headers, db, monkeypatch):
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationChannel, NotificationDelivery, NotificationMessage

    channel = _make_channel(client, auth_headers)
    calls: list[str] = []
    monkeypatch.setattr(cs, "_send_via_apprise", lambda url, *, title, body: calls.append(title))

    ok = client.post(f"{CHANNELS}/{channel['id']}/test", headers=auth_headers).json()["data"]
    assert ok["ok"] is True and "已发送" in ok["message"]
    assert calls and calls[0].startswith("【消息通知】")
    # 直发不落消息表、不产生投递单
    assert db.query(NotificationDelivery).count() == 0
    assert db.query(NotificationMessage).count() == 0
    assert db.query(NotificationChannel).first().last_status == "sent"

    # 失败路径：错误回传且渠道状态置 failed
    def boom(url, *, title, body):
        raise RuntimeError("bad token")
    monkeypatch.setattr(cs, "_send_via_apprise", boom)
    fail = client.post(f"{CHANNELS}/{channel['id']}/test", headers=auth_headers).json()["data"]
    assert fail["ok"] is False and "bad token" in fail["message"]


# ── M3 对抗审查补充：停用冻结 / 密文损坏 / 404 ────────────────


def test_disabled_channel_freezes_pending_deliveries(client, auth_headers, db, monkeypatch):
    """停用渠道：积压 pending 单不外发（止损）；重新启用后恢复投递。"""
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationDelivery

    channel = _make_channel(client, auth_headers)
    calls: list[str] = []
    monkeypatch.setattr(cs, "_send_via_apprise", lambda url, *, title, body: calls.append(title))
    _create(client, auth_headers, title="将被冻结")
    assert db.query(NotificationDelivery).count() == 1

    client.patch(f"{CHANNELS}/{channel['id']}", json={"enabled": False}, headers=auth_headers)
    result = cs.dispatch_pending_deliveries(db)
    assert result["scanned"] == 0 and calls == []  # 停用即冻结
    delivery = db.query(NotificationDelivery).first()
    assert delivery.status == "pending" and delivery.attempts == 0  # 次数不被消耗

    client.patch(f"{CHANNELS}/{channel['id']}", json={"enabled": True}, headers=auth_headers)
    result = cs.dispatch_pending_deliveries(db)
    assert result["sent"] == 1 and calls == ["将被冻结"]  # 启用即恢复


def test_corrupted_ciphertext_fails_delivery_not_500(client, auth_headers, db, monkeypatch):
    """渠道密文损坏：投递走重试→failed，不抛 500 死循环。"""
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationChannel, NotificationDelivery

    channel = _make_channel(client, auth_headers)
    record = db.query(NotificationChannel).first()
    record.apprise_url_encrypted = "not-a-fernet-token"
    db.commit()
    _create(client, auth_headers, title="密文损坏场景")

    for _ in range(3):
        cs.dispatch_pending_deliveries(db)
    delivery = db.query(NotificationDelivery).first()
    assert delivery.status == "failed" and delivery.attempts == 3
    assert db.query(NotificationChannel).first().last_status == "failed"


def test_channel_endpoints_404(client, auth_headers):
    assert client.patch(f"{CHANNELS}/missing", json={"enabled": True}, headers=auth_headers).status_code == 404
    assert client.delete(f"{CHANNELS}/missing", headers=auth_headers).status_code == 404
    assert client.post(f"{CHANNELS}/missing/test", headers=auth_headers).status_code == 404


def test_delete_channel_removes_pending_deliveries_sqlite(client, auth_headers, db, monkeypatch):
    """SQLite 无外键强制：删除渠道必须显式清投递单，不留孤儿。"""
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationDelivery

    channel = _make_channel(client, auth_headers)
    monkeypatch.setattr(cs, "_send_via_apprise", lambda url, *, title, body: None)
    _create(client, auth_headers, title="待删渠道的投递")
    assert db.query(NotificationDelivery).count() == 1

    client.delete(f"{CHANNELS}/{channel['id']}", headers=auth_headers)
    assert db.query(NotificationDelivery).count() == 0


# ── L2：渠道模板化 + 平台 SMTP ────────────────────────────────


def test_channel_template_definitions(client, auth_headers):
    defs = client.get("/api/v2/notifications/channel-templates", headers=auth_headers).json()["data"]
    ids = {d["id"] for d in defs}
    assert {"dingtalk", "feishu", "wecom", "tgram", "email", "webhook", "custom"} <= ids
    dingtalk = next(d for d in defs if d["id"] == "dingtalk")
    assert any(f["key"] == "token" and f["secret"] for f in dingtalk["fields"])
    assert any(f["hint"] for f in dingtalk["fields"])  # 每个字段带人话指引


def test_templated_channel_url_built_and_masked(client, auth_headers, db):
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationChannel

    def create(template, params, name):
        resp = client.post(CHANNELS, json={"name": name, "template": template, "params": params}, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]

    # 钉钉：整段 webhook 粘贴解析 + 加签
    ding = create("dingtalk", {"token": "6fb1fa7c2b", "secret": "SECxyz1234"}, "钉钉")
    row = db.query(NotificationChannel).filter_by(name="钉钉").first()
    assert cs.channel_url(row) == "dingtalk://SECxyz1234@6fb1fa7c2b/"
    assert ding["templateName"] == "钉钉机器人"
    assert ding["display"] == "6fb1…7c2b · SECx…1234"  # 敏感字段脱敏

    # 飞书：粘贴完整 webhook 自动解析 token
    fei = create("feishu", {"token": "https://open.feishu.cn/open-apis/bot/v2/hook/abc-def-123456"}, "飞书")
    row = db.query(NotificationChannel).filter_by(name="飞书").first()
    assert cs.channel_url(row) == "feishu://abc-def-123456/"

    # 企微：粘贴完整 webhook 解析 key
    wec = create("wecom", {"key": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123-xyz"}, "企微")
    row = db.query(NotificationChannel).filter_by(name="企微").first()
    assert cs.channel_url(row) == "wecombot://abc123-xyz"

    # Telegram
    create("tgram", {"botToken": "123:AAHxxx", "chatId": "98765"}, "TG")
    row = db.query(NotificationChannel).filter_by(name="TG").first()
    assert cs.channel_url(row) == "tgram://123:AAHxxx/98765"

    # 通用 Webhook：https → jsons
    create("webhook", {"url": "https://hooks.example.com/p?x=1"}, "钩子")
    row = db.query(NotificationChannel).filter_by(name="钩子").first()
    assert cs.channel_url(row) == "jsons://hooks.example.com/p?x=1"

    # 必填缺失 422；未知模板 422
    assert client.post(CHANNELS, json={"name": "x", "template": "dingtalk", "params": {}}, headers=auth_headers).status_code == 422
    assert client.post(CHANNELS, json={"name": "x", "template": "nope", "params": {}}, headers=auth_headers).status_code == 422


def test_update_channel_params_rebuilds_url(client, auth_headers, db):
    from app.notifications import channel_service as cs
    from app.notifications.models import NotificationChannel

    client.post(CHANNELS, json={"name": "钉钉", "template": "dingtalk", "params": {"token": "token-aaaa"}}, headers=auth_headers)
    row_id = client.get(CHANNELS, headers=auth_headers).json()["data"][0]["id"]
    resp = client.patch(f"{CHANNELS}/{row_id}", json={"params": {"token": "token-bbbb", "secret": "secret-cccc"}}, headers=auth_headers)
    assert resp.status_code == 200
    row = db.query(NotificationChannel).filter_by(id=row_id).first()
    assert cs.channel_url(row) == "dingtalk://secret-cccc@token-bbbb/"
    data = resp.json()["data"]
    assert data["display"] == "toke…bbbb · secr…cccc"


def test_smtp_settings_and_email_channel(client, auth_headers, db, monkeypatch):
    from app.notifications import channel_service as cs

    # 未配置 SMTP：邮件渠道 422 并提示先配置
    resp = client.post(CHANNELS, json={"name": "邮件", "template": "email", "params": {"recipients": "ops@example.com"}}, headers=auth_headers)
    assert resp.status_code == 422 and "发件" in resp.json()["detail"]

    # SMTP 保存（密码不回明文，留空=保持）
    put = client.put("/api/v2/notifications/smtp", json={
        "host": "smtp.example.com", "port": 465, "username": "noreply@example.com",
        "password": "pass123", "sender": "平台通知", "useTls": True,
    }, headers=auth_headers)
    assert put.status_code == 200
    out = put.json()["data"]
    assert out["configured"] is True and out["hasPassword"] is True
    assert "password" not in out and "pass123" not in str(out)

    # 留空密码更新不覆盖
    client.put("/api/v2/notifications/smtp", json={
        "host": "smtp2.example.com", "port": 587, "username": "noreply@example.com",
        "sender": "", "useTls": False,
    }, headers=auth_headers)
    out2 = client.get("/api/v2/notifications/smtp", headers=auth_headers).json()["data"]
    assert out2["host"] == "smtp2.example.com" and out2["hasPassword"] is True

    # 邮件渠道可建，URL 由 SMTP 拼装（含多收件人）
    sent: list[str] = []
    monkeypatch.setattr(cs, "_send_via_apprise", lambda url, *, title, body: sent.append(url))
    resp = client.post(CHANNELS, json={"name": "邮件", "template": "email", "params": {"recipients": "ops@example.com, dev@example.com"}}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["display"] == "ops@example.com, dev@example.com"  # 收件人非敏感回显原文

    # SMTP 测试邮件（走被 mock 的发送）
    result = client.post("/api/v2/notifications/smtp/test", json={"to": "me@example.com"}, headers=auth_headers).json()["data"]
    assert result["ok"] is True and sent and sent[0].startswith("mailtos://")


def test_smtp_test_requires_complete_config(client, auth_headers):
    assert client.post("/api/v2/notifications/smtp/test", json={"to": "a@b.com"}, headers=auth_headers).status_code == 422
