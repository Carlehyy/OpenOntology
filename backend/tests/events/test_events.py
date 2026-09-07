import csv
import hashlib
import io
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone

from app.auth.models import User
from app.auth.service import hash_password
from app.events import models as event_models
from app.events import router as event_router
from app.events.models import EventAttachment, EventIngestKey, RegisteredEvent
from app.shared.config import settings


def _event(event_no: str, severity: str, recorded_at: datetime) -> RegisteredEvent:
    return RegisteredEvent(
        event_no=event_no,
        title=event_no,
        severity=severity,
        recorded_at=recorded_at,
        source_type=event_models.SOURCE_PLATFORM,
        status=event_models.STATUS_ACTIVE,
    )


def test_event_stats_returns_real_seven_day_shanghai_trend(
    client, auth_headers, db, monkeypatch,
):
    monkeypatch.setattr(
        "app.events.router._now_utc",
        lambda: datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc),
    )
    db.add_all([
        # UTC 7/18 16:30 = 上海 7/19 00:30，应计入上海“今日”。
        _event("EVT-TODAY-INFO", "info", datetime(2026, 7, 18, 16, 30)),
        _event("EVT-YESTERDAY-MEDIUM", "medium", datetime(2026, 7, 17, 16, 30)),
        # UTC 7/12 15:59 = 上海 7/12 23:59，已经超出最近 7 个自然日。
        _event("EVT-OUTSIDE-HIGH", "high", datetime(2026, 7, 12, 15, 59)),
    ])
    db.commit()

    response = client.get("/api/v2/events/stats/summary", headers=auth_headers)

    assert response.status_code == 200
    stats = response.json()["data"]
    assert stats["today"] == 1
    assert len(stats["trend7d"]) == 7
    assert [item["date"] for item in stats["trend7d"]] == [
        "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16",
        "2026-07-17", "2026-07-18", "2026-07-19",
    ]
    by_day = {item["date"]: item for item in stats["trend7d"]}
    assert by_day["2026-07-19"]["bySeverity"]["info"] == 1
    assert by_day["2026-07-18"]["bySeverity"]["medium"] == 1
    assert sum(item["total"] for item in stats["trend7d"]) == 2


def test_event_list_binds_snake_case_filters_and_page_size(
    client, auth_headers, db,
):
    """回归（MYW-42）：前端曾误发 camelCase 查询参数，导致来源筛选失效、
    分页大小退化为后端默认值。此用例锁定 /api/v2/events 的参数契约。"""
    def event_with_source(no: str, source_type: str, at: datetime) -> RegisteredEvent:
        return RegisteredEvent(
            event_no=no, title=no, severity="info", recorded_at=at,
            source_type=source_type, status=event_models.STATUS_ACTIVE,
        )

    db.add_all([
        event_with_source("EVT-A-PLAT", event_models.SOURCE_PLATFORM, datetime(2026, 7, 18, 1, 0)),
        event_with_source("EVT-B-API", event_models.SOURCE_API, datetime(2026, 7, 18, 2, 0)),
        event_with_source("EVT-C-API", event_models.SOURCE_API, datetime(2026, 7, 18, 3, 0)),
    ])
    db.commit()

    filtered = client.get(
        "/api/v2/events",
        params={"source_type": "api"},
        headers=auth_headers,
    )
    assert filtered.status_code == 200
    assert [item["eventNo"] for item in filtered.json()["data"]["items"]] == [
        "EVT-C-API", "EVT-B-API",
    ]

    second_page = client.get(
        "/api/v2/events",
        params={"page": 2, "page_size": 1},
        headers=auth_headers,
    )
    assert second_page.status_code == 200
    page_data = second_page.json()["data"]
    assert page_data["pageSize"] == 1
    assert [item["eventNo"] for item in page_data["items"]] == ["EVT-B-API"]


def test_stats_severity_distribution_and_trend_cover_all_statuses(
    client, auth_headers, db, monkeypatch,
):
    """回归（MYW-42）：级别分布与 7 日趋势须与总数卡同口径（含归档），
    避免同屏“合计 4 vs 总数 6”的矛盾。"""
    monkeypatch.setattr(
        "app.events.router._now_utc",
        lambda: datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc),
    )
    archived = _event("EVT-ARCHIVED-HIGH", "high", datetime(2026, 7, 18, 8, 0))
    archived.status = event_models.STATUS_ARCHIVED
    db.add_all([
        _event("EVT-ACTIVE-INFO", "info", datetime(2026, 7, 18, 9, 0)),
        archived,
    ])
    db.commit()

    stats = client.get("/api/v2/events/stats/summary", headers=auth_headers).json()["data"]

    assert stats["total"] == 2
    assert stats["active"] == 1
    assert stats["archived"] == 1
    assert sum(stats["bySeverity"].values()) == 2
    assert stats["bySeverity"]["high"] == 1
    assert stats["bySeverity"]["info"] == 1
    trend_total = sum(item["total"] for item in stats["trend7d"])
    assert trend_total == 2
    by_day = {item["date"]: item for item in stats["trend7d"]}
    assert by_day["2026-07-18"]["bySeverity"]["high"] == 1


def test_export_csv_filters_escapes_and_marks_bom(
    client, auth_headers, db,
):
    """导出端点：默认仅活跃、status=all 含归档、UTF-8 BOM、防公式注入。"""
    formula = _event("=EVT-FORMULA", "critical", datetime(2026, 7, 18, 5, 0))
    formula.title = "=1+1 危险标题"
    archived = _event("EVT-EXPORT-ARCHIVED", "low", datetime(2026, 7, 18, 6, 0))
    archived.status = event_models.STATUS_ARCHIVED
    db.add_all([formula, archived])
    db.commit()

    default_export = client.get("/api/v2/events/export", headers=auth_headers)
    assert default_export.status_code == 200
    assert default_export.headers["content-type"].startswith("text/csv")
    assert "attachment" in default_export.headers["content-disposition"]
    # BOM：Excel 依赖它识别 UTF-8 中文表头
    assert default_export.content.startswith(b"\xef\xbb\xbf")

    rows = list(csv.reader(io.StringIO(default_export.content.decode("utf-8-sig"))))
    assert rows[0][:5] == ["事件编号", "标题", "事件类型", "级别", "状态"]
    assert len(rows) == 2  # 表头 + 仅 1 条活跃事件
    assert rows[1][0] == "'=EVT-FORMULA"  # 编号同样受公式注入防护
    assert rows[1][1] == "'=1+1 危险标题"
    assert rows[1][3] == "严重"
    assert rows[1][4] == "活跃"

    all_export = client.get(
        "/api/v2/events/export",
        params={"status": "all"},
        headers=auth_headers,
    )
    assert all_export.status_code == 200
    all_rows = list(csv.reader(io.StringIO(all_export.content.decode("utf-8-sig"))))
    assert len(all_rows) == 3
    # 按 recorded_at 倒序：后登记的归档事件在前
    assert all_rows[1][0] == "EVT-EXPORT-ARCHIVED"
    assert all_rows[1][4] == "归档"
    assert all_rows[2][0] == "'=EVT-FORMULA"

    archived_only = client.get(
        "/api/v2/events/export",
        params={"status": "archived"},
        headers=auth_headers,
    )
    archived_rows = list(
        csv.reader(io.StringIO(archived_only.content.decode("utf-8-sig")))
    )
    assert [row[0] for row in archived_rows[1:]] == ["EVT-EXPORT-ARCHIVED"]


def test_ingest_keys_support_server_side_pagination_and_filters(client, auth_headers):
    created = []
    for name, source_system in [
        ("分页测试-MES-主密钥", "MES"),
        ("分页测试-CRM-主密钥", "CRM"),
        ("分页测试-MES-旧密钥", "MES-LEGACY"),
    ]:
        response = client.post(
            "/api/v2/events/ingest-keys",
            headers=auth_headers,
            json={"name": name, "allowedSourceSystem": source_system},
        )
        assert response.status_code == 201
        created.append(response.json()["data"])

    revoked = client.delete(
        f"/api/v2/events/ingest-keys/{created[2]['id']}",
        headers=auth_headers,
    )
    assert revoked.status_code == 200

    first_page = client.get(
        "/api/v2/events/ingest-keys",
        headers=auth_headers,
        params={"q": "分页测试", "page": 1, "page_size": 2},
    )
    assert first_page.status_code == 200
    page_data = first_page.json()["data"]
    assert page_data["total"] == 3
    assert page_data["page"] == 1
    assert page_data["pageSize"] == 2
    assert len(page_data["items"]) == 2

    second_page = client.get(
        "/api/v2/events/ingest-keys",
        headers=auth_headers,
        params={"q": "分页测试", "page": 2, "page_size": 2},
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["data"]["items"]) == 1

    active_mes = client.get(
        "/api/v2/events/ingest-keys",
        headers=auth_headers,
        params={
            "q": "分页测试",
            "status": "active",
            "source_system": "MES",
            "page_size": 10,
        },
    )
    assert active_mes.status_code == 200
    active_data = active_mes.json()["data"]
    assert active_data["total"] == 1
    assert active_data["items"][0]["name"] == "分页测试-MES-主密钥"

    revoked_keys = client.get(
        "/api/v2/events/ingest-keys",
        headers=auth_headers,
        params={"q": "分页测试", "status": "revoked", "page_size": 10},
    )
    assert revoked_keys.status_code == 200
    revoked_data = revoked_keys.json()["data"]
    assert revoked_data["total"] == 1
    assert revoked_data["items"][0]["name"] == "分页测试-MES-旧密钥"


def test_event_attachment_streams_and_cleans_file_when_limit_exceeded(
    client, auth_headers, db, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    created = client.post(
        "/api/v2/events",
        headers=auth_headers,
        json={"title": "附件流式上传测试", "severity": "info"},
    )
    assert created.status_code == 201
    event_id = created.json()["data"]["id"]

    oversized = client.post(
        f"/api/v2/events/{event_id}/attachments",
        headers=auth_headers,
        files={"file": ("oversized.pdf", b"x" * (1024 * 1024 + 1), "application/pdf")},
    )
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == "文件超过大小限制 1MB"
    assert db.query(EventAttachment).filter(EventAttachment.event_id == event_id).count() == 0
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]

    valid_content = b"stable-stream" * 4096
    uploaded = client.post(
        f"/api/v2/events/{event_id}/attachments",
        headers=auth_headers,
        files={"file": ("valid.pdf", valid_content, "application/pdf")},
    )
    assert uploaded.status_code == 201
    attachment = uploaded.json()["data"]
    assert attachment["fileSize"] == len(valid_content)
    assert attachment["sha256"] == hashlib.sha256(valid_content).hexdigest()


def test_event_attachments_accept_mail_formats_and_zip_is_temporary(
    client, auth_headers, monkeypatch, tmp_path,
):
    uploads_dir = tmp_path / "uploads"
    archives_dir = tmp_path / "archives"
    archives_dir.mkdir()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_dir))
    monkeypatch.setattr(settings, "event_attachment_extensions", "*")

    created = client.post(
        "/api/v2/events",
        headers=auth_headers,
        json={"title": "邮件附件兼容与打包测试", "severity": "info"},
    )
    assert created.status_code == 201
    event_id = created.json()["data"]["id"]

    payloads = {
        "incident.eml": b"Subject: alarm\r\n\r\nmail body",
        "outlook.msg": b"mock-outlook-message",
    }
    for filename, content in payloads.items():
        uploaded = client.post(
            f"/api/v2/events/{event_id}/attachments",
            headers=auth_headers,
            files={"file": (filename, content, "application/octet-stream")},
        )
        assert uploaded.status_code == 201, uploaded.text

    original_named_temporary_file = tempfile.NamedTemporaryFile

    def temporary_archive(*args, **kwargs):
        return original_named_temporary_file(*args, dir=archives_dir, **kwargs)

    monkeypatch.setattr(event_router.tempfile, "NamedTemporaryFile", temporary_archive)
    downloaded = client.get(
        f"/api/v2/events/{event_id}/attachments/download-all",
        headers=auth_headers,
    )

    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"] == "application/zip"
    assert "attachment" in downloaded.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert archive.namelist() == list(payloads)
        for filename, content in payloads.items():
            assert archive.read(filename) == content
    assert list(archives_dir.iterdir()) == []


def test_ingest_key_plaintext_is_one_time_and_never_persisted(client, auth_headers, db):
    """商用加固回归：密钥明文仅创建响应一次性返回；列表/吊销响应不携带，
    鉴权只依赖 sha256，明文不落库不影响已有密钥上报。"""
    created = client.post(
        "/api/v2/events/ingest-keys",
        headers=auth_headers,
        json={"name": "一次性明文测试", "allowedSourceSystem": "MES"},
    )
    assert created.status_code == 201
    key = created.json()["data"]
    assert key["plaintextKey"].startswith(key["keyPrefix"])
    assert db.query(EventIngestKey).filter(
        EventIngestKey.key_hash
        == hashlib.sha256(key["plaintextKey"].encode()).hexdigest()
    ).count() == 1

    listed = client.get("/api/v2/events/ingest-keys", headers=auth_headers).json()["data"]
    assert listed["total"] == 1
    assert "plaintextKey" not in listed["items"][0]

    ingested = client.post(
        "/api/v2/ingest/events",
        headers={"X-API-Key": key["plaintextKey"]},
        json={"title": "明文不落库回归", "source_system": "MES", "source_ref": "WO-PLAIN-1"},
    )
    assert ingested.status_code == 200
    assert ingested.json()["data"]["idempotent"] is False

    # (source_system, source_ref) 幂等键：重复投递命中既有事件
    again = client.post(
        "/api/v2/ingest/events",
        headers={"X-API-Key": key["plaintextKey"]},
        json={"title": "明文不落库回归", "source_system": "MES", "source_ref": "WO-PLAIN-1"},
    )
    assert again.status_code == 200
    assert again.json()["data"]["idempotent"] is True

    revoked = client.delete(
        f"/api/v2/events/ingest-keys/{key['id']}",
        headers=auth_headers,
    )
    assert revoked.status_code == 200
    assert "plaintextKey" not in revoked.json()["data"]

    # 吊销后密钥立即失效
    rejected = client.post(
        "/api/v2/ingest/events",
        headers={"X-API-Key": key["plaintextKey"]},
        json={"title": "吊销后上报"},
    )
    assert rejected.status_code == 401


def test_event_write_paths_full_lifecycle(client, admin_user, auth_headers, db):
    """写路径回归（此前零覆盖）：POST/PATCH/status/DELETE 全链路 + 审计轨迹 +
    物理删除的 admin 边界。"""
    created = client.post(
        "/api/v2/events",
        headers=auth_headers,
        json={"title": "写路径生命周期", "severity": "high", "eventType": "设备异常", "tags": ["产线"]},
    )
    assert created.status_code == 201
    ev = created.json()["data"]
    assert ev["status"] == "active"
    assert ev["sourceType"] == "platform"
    assert ev["severity"] == "high"

    assert client.post(
        "/api/v2/events", headers=auth_headers, json={"title": "   "},
    ).status_code == 422

    patched = client.patch(
        f"/api/v2/events/{ev['id']}",
        headers=auth_headers,
        json={"severity": "critical", "description": "升级处理"},
    )
    assert patched.status_code == 200
    body = patched.json()["data"]
    assert body["severity"] == "critical"
    assert body["description"] == "升级处理"

    archived = client.post(
        f"/api/v2/events/{ev['id']}/status",
        headers=auth_headers,
        json={"status": "archived", "note": "处置完成"},
    )
    assert archived.status_code == 200
    assert archived.json()["data"]["status"] == "archived"
    assert client.post(
        f"/api/v2/events/{ev['id']}/status",
        headers=auth_headers,
        json={"status": "unknown"},
    ).status_code == 422

    # 软删除=归档，对已归档事件幂等
    soft = client.delete(f"/api/v2/events/{ev['id']}", headers=auth_headers)
    assert soft.status_code == 200
    assert soft.json()["data"]["status"] == "archived"

    detail = client.get(f"/api/v2/events/{ev['id']}", headers=auth_headers).json()["data"]
    actions = [entry["action"] for entry in detail["auditTrail"]]
    assert set(actions) == {"created", "updated", "status_changed"}
    assert len(actions) == 3

    # 物理删除仅 admin；viewer 走 soft-delete 语义但 hard=true 被拒
    # （用户管理 API 已退役，测试用户直写数据库，与 conftest 夹具同款）
    db.add(User(id=str(uuid.uuid4()), username="evt_viewer", email="evt_viewer@test.com",
                password_hash=hash_password("viewer123"), role="viewer"))
    db.commit()
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "evt_viewer", "password": "viewer123"},
    )
    viewer_headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    assert client.delete(
        f"/api/v2/events/{ev['id']}?hard=true", headers=viewer_headers,
    ).status_code == 403

    hard = client.delete(f"/api/v2/events/{ev['id']}?hard=true", headers=auth_headers)
    assert hard.status_code == 200
    assert client.get(f"/api/v2/events/{ev['id']}", headers=auth_headers).status_code == 404
