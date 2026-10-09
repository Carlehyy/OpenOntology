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
