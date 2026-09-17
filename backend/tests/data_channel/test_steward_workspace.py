import asyncio
import json
import io
import time
import uuid
import zipfile
import struct
from types import SimpleNamespace

import pytest

from app.api_hub import config as api_hub_config, db as api_hub_db
from app.config import settings
from app.data_channel.steward import browser_sources, file_tools, workspace
from app.data_channel.steward.browser_runtime import (
    BrowserManager, BrowserRuntimeError, _resolve_cdp_endpoint,
    _is_file, _validate_navigation_response, analyze_pagination, browser_manager,
    probe_browser_cdp, public_capture, validate_target_url,
)
from app.data_channel.steward.toolkit import ToolRunner
from app.data_channel.steward.models import StewardBrowserSource
from app.data_channel.steward.companion import CompanionHub


@pytest.fixture
def steward_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "steward_workspace_root", str(tmp_path / "sessions"))
    return tmp_path


def test_workspace_isolates_sessions_and_archive_excludes_browser_secrets(steward_workspace):
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    row = workspace.save_bytes(
        first, "需求说明.md", b"source: https://example.com/report\n", source="upload",
        mime_type="text/markdown",
    )

    assert [item["id"] for item in workspace.list_files(first)] == [row["id"]]
    assert workspace.list_files(second) == []
    assert row["urls"] == ["https://example.com/report"]
    with pytest.raises(workspace.WorkspaceError):
        workspace._within(first, "../../outside.txt")

    workspace.storage_state_path(first).write_text(
        json.dumps({"cookies": [{"value": "browser-secret"}]}), encoding="utf-8")
    workspace.append_capture(first, {
        "id": "capture", "requestHeaders": {"Authorization": "Bearer raw-secret"},
    })
    archive = workspace.archive_path(first)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        combined = b"".join(zf.read(name) for name in names)
    assert names == ["files/需求说明.md", "manifest.json"]
    assert b"browser-secret" not in combined
    assert b"raw-secret" not in combined


def test_stream_upload_enforces_limit_without_leaving_partial_file(steward_workspace, monkeypatch):
    cid = str(uuid.uuid4())
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    with pytest.raises(workspace.WorkspaceError):
        workspace.save_stream(
            cid, "too-large.txt", io.BytesIO(b"x"), source="upload",
            mime_type="text/plain",
        )
    assert workspace.list_files(cid) == []
    assert list((workspace.session_root(cid) / "files").iterdir()) == []


@pytest.mark.parametrize(
    ("filename", "content", "prefix"),
    [
        ("报告.docx", "第一段\n\n第二段", b"PK"),
        ("汇报.pptx", "# 结论\n完成验证", b"PK"),
        ("数据.xlsx", "名称,数量\n甲,2", b"PK"),
        ("说明.pdf", "中文 PDF 内容", b"%PDF"),
        ("记录.md", "# 会话记录", b"# "),
    ],
)
def test_generated_documents_are_real_files_inside_session(
    steward_workspace, filename, content, prefix,
):
    cid = str(uuid.uuid4())
    row = file_tools.create(cid, filename, content, title="数据管家产出")
    registered, path = workspace.require_file(cid, row["id"])

    assert path.read_bytes().startswith(prefix)
    assert path.parent == workspace.session_root(cid) / "files"
    assert registered["source"] == "generated"
    assert registered["size"] > 0


def test_tool_runner_can_create_edit_and_delete_word_without_crossing_session(
    steward_workspace,
):
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    runner = ToolRunner(None, user_id="u1", conversation_id=first)
    created = runner.run("create_session_file", {
        "filename": "200字说明.docx", "title": "说明",
        "content": "这是由数据管家生成的正文。" * 20,
    })

    assert "error" not in created
    artifact_id = created["file"]["id"]
    assert workspace.list_files(second) == []
    edited = runner.run("edit_session_file", {
        "artifact_id": artifact_id, "mode": "append", "content": "追加结论。",
    })
    assert "error" not in edited
    assert edited["file"]["source"] == "edited"
    assert len(workspace.list_files(first)) == 2
    assert "追加结论" in workspace.extracted_text(first, edited["file"]["id"])

    deleted = runner.run("delete_session_file", {"artifact_id": artifact_id})
    assert deleted["deleted"] is True
    assert [row["id"] for row in workspace.list_files(first)] == [edited["file"]["id"]]
    with pytest.raises(workspace.WorkspaceError):
        workspace.require_file(second, edited["file"]["id"])


def test_browser_source_secrets_are_encrypted_and_resolve_to_target(db, editor_user, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    source, token = browser_sources.create_source(
        db, editor_user.id, name="测试 CDP", source_type="remote_cdp",
        endpoint_url="https://browser.example/cdp",
        headers={"Authorization": "Bearer very-secret"},
    )

    assert token is None
    assert "browser.example" not in source.endpoint_url_encrypted
    assert "very-secret" not in source.headers_encrypted
    target = browser_sources.resolve_target(db, source.id, editor_user.id)
    assert target.endpoint_url == "https://browser.example/cdp"
    assert target.headers == {"Authorization": "Bearer very-secret"}
    with pytest.raises(Exception, match="他人的浏览器来源"):
        browser_sources.resolve_target(db, source.id, "another-user")


def test_browser_source_api_binds_companion_to_one_conversation(
    client, auth_headers, db, monkeypatch,
):
    monkeypatch.setattr(settings, "environment", "development")
    conversation = client.post(
        "/api/v2/steward/conversations", json={"title": "浏览器来源测试"},
        headers=auth_headers,
    ).json()["data"]
    response = client.post(
        "/api/v2/steward/browser/sources",
        json={"name": "我的 Mac", "sourceType": "companion"}, headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    created = response.json()["data"]
    assert created["pairingToken"]
    assert created["online"] is False

    listed = client.get("/api/v2/steward/browser/sources", headers=auth_headers).json()["data"]
    assert [row["id"] for row in listed] == ["managed", created["id"]]
    assert "pairingToken" not in listed[1]
    bound = client.put(
        f"/api/v2/steward/conversations/{conversation['id']}/browser/source",
        json={"sourceId": created["id"]}, headers=auth_headers,
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["data"]["browserSourceId"] == created["id"]

    source = db.query(StewardBrowserSource).filter_by(id=created["id"]).one()
    assert source.device_token_hash != created["pairingToken"]
    assert len(source.device_token_hash) == 64


@pytest.mark.asyncio
async def test_companion_hub_proxies_loopback_tcp_in_both_directions():
    class FakeWebSocket:
        def __init__(self):
            self.sent = asyncio.Queue()
            self.incoming = asyncio.Queue()

        async def send_text(self, value):
            await self.sent.put(("text", value))

        async def send_bytes(self, value):
            await self.sent.put(("bytes", value))

        async def receive(self):
            return await self.incoming.get()

    hub = CompanionHub()
    websocket = FakeWebSocket()
    connection = await hub.register("source", websocket)
    run_task = asyncio.create_task(connection.run())
    reader, writer = await asyncio.open_connection("127.0.0.1", connection.port)
    try:
        kind, control_raw = await asyncio.wait_for(websocket.sent.get(), 2)
        control = json.loads(control_raw)
        assert kind == "text" and control["type"] == "open"
        stream_id = control["streamId"]

        writer.write(b"client-to-browser")
        await writer.drain()
        kind, packet = await asyncio.wait_for(websocket.sent.get(), 2)
        assert kind == "bytes"
        assert struct.unpack("!I", packet[:4])[0] == stream_id
        assert packet[4:] == b"client-to-browser"

        await websocket.incoming.put({
            "type": "websocket.receive",
            "bytes": struct.pack("!I", stream_id) + b"browser-to-client",
        })
        assert await asyncio.wait_for(reader.readexactly(17), 2) == b"browser-to-client"
    finally:
        writer.close()
        await writer.wait_closed()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await hub.unregister("source", connection)


def test_network_capture_redacts_auth_and_detects_page_offset_and_cursor():
    capture = {
        "id": "c1", "requestHeaders": {"Authorization": "Bearer secret", "Accept": "application/json"},
        "responseHeaders": {"Set-Cookie": "sid=secret"}, "responseBody": '{"ok":true}',
    }
    public = public_capture(capture)
    assert public["requestHeaders"]["Authorization"] == "••••••"
    assert public["responseHeaders"]["Set-Cookie"] == "••••••"
    assert "secret" not in json.dumps(public)

    page = analyze_pagination(
        "https://example.com/api/orders?page=2&pageSize=50",
        {"data": [], "total": 120, "hasNext": True},
    )
    assert page and page["mode"] == "page"
    assert page["requestParams"] == {"page": "2", "pagesize": "50"}
    offset = analyze_pagination("https://example.com/api/orders?offset=50&limit=50", [])
    assert offset and offset["mode"] == "offset"
    cursor = analyze_pagination("https://example.com/api/orders", {"nextCursor": "abc"})
    assert cursor and cursor["mode"] == "cursor"


def test_url_policy_blocks_loopback_and_metadata(monkeypatch):
    with pytest.raises(BrowserRuntimeError):
        validate_target_url("http://127.0.0.1/admin")
    with pytest.raises(BrowserRuntimeError):
        validate_target_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(BrowserRuntimeError):
        validate_target_url("https://user:password@example.com")

    monkeypatch.setattr(
        "app.data_channel.steward.browser_runtime.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("10.20.30.40", 443))],
    )
    monkeypatch.setattr(settings, "steward_browser_allow_private_networks", True)
    assert validate_target_url("https://intranet.example/path") == "https://intranet.example/path"
    monkeypatch.setattr(settings, "steward_browser_allow_private_networks", False)
    with pytest.raises(BrowserRuntimeError):
        validate_target_url("https://intranet.example/path")


def test_internal_cdp_hostname_resolves_to_ip_for_chromium_host_validation(monkeypatch):
    monkeypatch.setattr(
        "app.data_channel.steward.browser_runtime.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("172.19.0.10", 9222))],
    )
    assert _resolve_cdp_endpoint("http://browser:9222") == "http://172.19.0.10:9222"
    assert _resolve_cdp_endpoint("http://127.0.0.1:9222") == "http://127.0.0.1:9222"
    assert _resolve_cdp_endpoint("https://browser.example/cdp") == "https://browser.example/cdp"


def test_browser_cdp_probe_validates_websocket_endpoint(monkeypatch):
    requested = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({
                "Browser": "Chrome/Test", "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": "ws://172.19.0.10:9222/devtools/browser/test",
            }).encode()

    monkeypatch.setattr(settings, "steward_browser_cdp_url", "http://browser:9222/")
    monkeypatch.setattr(
        "app.data_channel.steward.browser_runtime.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("172.19.0.10", 9222))],
    )
    def fake_urlopen(request, timeout):
        requested["url"] = request.full_url
        requested["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "app.data_channel.steward.browser_runtime.urllib.request.urlopen",
        fake_urlopen,
    )

    status = probe_browser_cdp()

    assert status == {
        "configured": True, "reachable": True,
        "browser": "Chrome/Test", "protocolVersion": "1.3",
    }
    assert requested == {
        "url": "http://172.19.0.10:9222/json/version",
        "timeout": 1.5,
    }


def test_browser_cdp_probe_rejects_precomposed_discovery_path(monkeypatch):
    monkeypatch.setattr(
        settings,
        "steward_browser_cdp_url",
        "http://127.0.0.1:9222/json/version",
    )

    status = probe_browser_cdp()

    assert status == {
        "configured": True,
        "reachable": False,
        "error": "浏览器健康检查要求 http/https CDP 服务根地址",
    }


def test_navigation_rejects_waf_error_instead_of_reporting_blank_success():
    class Response:
        status = 567

    with pytest.raises(BrowserRuntimeError) as caught:
        _validate_navigation_response(Response(), "https://example.com/all")

    message = str(caught.value)
    assert "HTTP 567" in message
    assert "CDN/WAF" in message
    assert "实时浏览器" in message


def test_navigation_allows_success_and_download_without_response():
    class Response:
        status = 200

    # 校验器只以异常表达拒绝；放行路径显式断言无返回值
    assert _validate_navigation_response(Response(), "https://example.com/all") is None
    assert _validate_navigation_response(None, "https://example.com/download") is None


@pytest.mark.parametrize(
    ("content_type", "url"),
    [
        ("image/png", "https://cdn.example/assets/hash"),
        ("image/webp", "https://cdn.example/banner.webp?sign=abc"),
        ("video/mp4", "https://cdn.example/media/stream"),
        ("application/octet-stream", "https://cdn.example/report.pdf"),
    ],
)
def test_static_media_responses_are_downloadable_file_captures(content_type, url):
    assert _is_file(content_type, {}, url) is True


@pytest.mark.asyncio
async def test_save_page_data_resource_stays_inside_conversation_workspace(
    steward_workspace,
):
    cid = str(uuid.uuid4())

    class Page:
        url = "https://example.com/gallery"

        def is_closed(self):
            return False

        async def evaluate(self, _script, _argument):
            return {
                "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
                "filename": "页面图片",
            }

    session = SimpleNamespace(
        page=Page(), operation_lock=asyncio.Lock(), touch=lambda: None,
        workspace=workspace,
    )
    manager = BrowserManager.__new__(BrowserManager)
    manager._sessions = {cid: session}
    manager._live_clients = {}

    row = await manager._save_page_resource(cid, 3, actor="agent")
    registered, path = workspace.require_file(cid, row["id"])

    assert registered["filename"] == "页面图片.png"
    assert registered["source"] == "download"
    assert path.parent == workspace.session_root(cid) / "files"
    assert path.read_bytes().startswith(b"\x89PNG")


def test_register_proxy_interface_uses_capture_without_leaking_auth(
    steward_workspace, monkeypatch,
):
    cid = str(uuid.uuid4())
    monkeypatch.setattr(api_hub_config, "DB_PATH", steward_workspace / "api-hub.db")
    monkeypatch.setattr(api_hub_config, "SYSTEM_MCP_TOKEN", "system-token")
    monkeypatch.setattr(api_hub_config, "INTERNAL_PROXY_TOKEN", "internal-token")
    api_hub_db.init_db()
    workspace.append_capture(cid, {
        "id": "cap-1", "method": "GET",
        "url": "https://service.example/api/orders?page=1&pageSize=20",
        "requestHeaders": {
            "Accept": "application/json", "Authorization": "Bearer captured-secret",
            "User-Agent": "not-copied",
        },
        "requestBody": None,
    })
    runner = ToolRunner(
        None, user_id="u1", conversation_id=cid, api_hub_allowed=True,
    )
    result = runner.run("register_proxy_interface", {
        "capture_id": "cap-1", "name": "订单接口", "include_auth": True,
    })

    assert "error" not in result
    assert result["interface"]["authCopied"] is True
    assert "captured-secret" not in json.dumps(result, ensure_ascii=False)
    with api_hub_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM interfaces").fetchone()
    headers = json.loads(row["headers"])
    assert {item["key"] for item in headers} == {"Accept", "Authorization"}
    assert row["url"] == "https://service.example/api/orders"
    assert row["open_enabled"] == 0
    assert row["created_by"] == "u1"
    assert json.loads(row["query_params"])[0] == {"key": "page", "value": "1"}
    assert result["proxyUrl"].endswith(f"/{row['id']}/invoke")


def test_live_browser_ticket_is_single_use():
    cid = str(uuid.uuid4())
    ticket = browser_manager.issue_ticket(cid, "user-1")
    assert browser_manager.redeem_ticket(ticket, cid) == (True, "user-1")
    assert browser_manager.redeem_ticket(ticket, cid) == (False, None)


def _manager_for_lifecycle_tests(sessions, live=None):
    manager = BrowserManager.__new__(BrowserManager)
    manager._sessions = {session.conversation_id: session for session in sessions}
    manager._live_clients = live or {}
    closed = []

    async def close(conversation_id):
        closed.append(conversation_id)
        manager._sessions.pop(conversation_id, None)

    manager._close = close
    return manager, closed


def test_idle_browser_reaper_preserves_recent_and_live_sessions(monkeypatch):
    now = time.time()
    sessions = [
        SimpleNamespace(conversation_id="expired", user_id="u1", last_active=now - 90),
        SimpleNamespace(conversation_id="recent", user_id="u1", last_active=now - 10),
        SimpleNamespace(conversation_id="live", user_id="u2", last_active=now - 90),
    ]
    manager, closed = _manager_for_lifecycle_tests(sessions, {"live": 1})
    monkeypatch.setattr(settings, "steward_browser_idle_timeout_seconds", 30)

    reclaimed = asyncio.run(manager._reap_idle(now=now))

    assert reclaimed == ["expired"]
    assert closed == ["expired"]
    assert set(manager._sessions) == {"recent", "live"}


def test_browser_capacity_evicts_lru_for_global_and_user_limits(monkeypatch):
    now = time.time()
    sessions = [
        SimpleNamespace(conversation_id="u1-old", user_id="u1", last_active=now - 20),
        SimpleNamespace(conversation_id="u2-live", user_id="u2", last_active=now - 10),
    ]
    manager, closed = _manager_for_lifecycle_tests(sessions, {"u2-live": 1})
    monkeypatch.setattr(settings, "steward_browser_idle_timeout_seconds", 900)
    monkeypatch.setattr(settings, "steward_browser_max_sessions", 2)
    monkeypatch.setattr(settings, "steward_browser_max_sessions_per_user", 1)

    reclaimed = asyncio.run(manager._ensure_capacity("u1"))

    assert reclaimed == ["u1-old"]
    assert closed == ["u1-old"]
    assert set(manager._sessions) == {"u2-live"}


def test_live_browser_observer_does_not_block_agent():
    manager, _ = _manager_for_lifecycle_tests([], {"conversation": 1})

    manager._assert_actor_allowed("conversation", "agent")
    manager._assert_actor_allowed("conversation", "user")


def test_explicit_user_control_blocks_agent_until_released(monkeypatch):
    manager, _ = _manager_for_lifecycle_tests([], {"conversation": 1})
    manager._user_controls = {}
    monkeypatch.setattr(settings, "steward_browser_http_lease_seconds", 5)

    status = manager._claim_user_control(
        "conversation", "viewer-1", mode="held")

    assert status["controller"] == "user"
    assert status["mode"] == "held"
    with pytest.raises(BrowserRuntimeError, match="协作浏览器"):
        manager._assert_actor_allowed("conversation", "agent")
    manager._assert_actor_allowed("conversation", "user")

    released = manager._release_user_control("conversation", "viewer-1")
    assert released["controller"] == "agent"
    manager._assert_actor_allowed("conversation", "agent")


def test_transient_user_activity_expires_without_closing_live_browser(monkeypatch):
    manager, _ = _manager_for_lifecycle_tests([], {"conversation": 1})
    manager._user_controls = {}
    monkeypatch.setattr(settings, "steward_browser_user_activity_seconds", 1)

    manager._claim_user_control("conversation", "viewer-1", mode="transient")
    with pytest.raises(BrowserRuntimeError, match="协作浏览器"):
        manager._assert_actor_allowed("conversation", "agent")

    manager._user_controls["conversation"]["expiresAt"] = time.monotonic() - 1
    manager._assert_actor_allowed("conversation", "agent")
    assert manager._is_live("conversation") is True


@pytest.mark.asyncio
async def test_queued_user_input_gets_priority_over_agent_operation(monkeypatch):
    manager, _ = _manager_for_lifecycle_tests([], {"conversation": 1})
    manager._user_controls = {}
    monkeypatch.setattr(settings, "steward_browser_user_activity_seconds", 1)
    operation_lock = asyncio.Lock()
    session = SimpleNamespace(operation_lock=operation_lock)
    order = []

    await operation_lock.acquire()

    async def agent_operation():
        async with manager._browser_operation(session, "conversation", "agent"):
            order.append("agent")

    async def user_operation():
        async with operation_lock:
            order.append("user")
            manager._user_controls["conversation"]["expiresAt"] = time.monotonic() - 1

    agent_task = asyncio.create_task(agent_operation())
    await asyncio.sleep(0)
    manager._claim_user_control("conversation", "viewer-1", mode="transient")
    user_task = asyncio.create_task(user_operation())
    operation_lock.release()

    await asyncio.wait_for(user_task, timeout=1)
    await asyncio.wait_for(agent_task, timeout=1)
    assert order == ["user", "agent"]


def test_http_live_lease_is_observer_and_expires(monkeypatch):
    session = SimpleNamespace(
        conversation_id="conversation",
        operation_lock=asyncio.Lock(),
        touch=lambda: None,
        page=SimpleNamespace(is_closed=lambda: False),
    )
    manager = BrowserManager.__new__(BrowserManager)
    manager._sessions = {"conversation": session}
    manager._live_clients = {}
    manager._live_leases = {}
    manager._user_controls = {}
    monkeypatch.setattr(settings, "steward_browser_http_lease_seconds", 5)
    monkeypatch.setattr(settings, "steward_browser_http_frame_interval_ms", 500)

    attached = asyncio.run(manager._attach_http_live("conversation"))

    assert attached["expiresIn"] == 5
    assert attached["frameIntervalMs"] == 500
    assert attached["collaboration"]["controller"] == "agent"
    manager._assert_actor_allowed("conversation", "agent")

    manager._live_leases["conversation"][attached["leaseId"]] = time.monotonic() - 1
    manager._assert_actor_allowed("conversation", "agent")
    assert manager._is_live("conversation") is False
    assert manager._live_leases == {}


def test_http_live_fallback_routes_keep_auth_and_input_contract(
    client, auth_headers, monkeypatch,
):
    conversation = client.post(
        "/api/v2/steward/conversations", json={"title": "HTTP 实时浏览器"},
        headers=auth_headers,
    ).json()["data"]
    calls = []
    collaboration = {
        "controller": "agent", "mode": "observe",
        "agentCanAct": True, "expiresIn": 0,
    }
    monkeypatch.setattr(browser_manager, "attach_http_live", lambda cid: {
        "leaseId": "lease-1", "expiresIn": 30, "frameIntervalMs": 500,
        "collaboration": collaboration,
    })
    monkeypatch.setattr(browser_manager, "http_live_screenshot", lambda cid, lease: {
        "data": "jpeg-base64", "url": "https://example.com/current",
        "collaboration": collaboration,
    })
    monkeypatch.setattr(
        browser_manager, "http_live_input",
        lambda cid, lease, message: (
            calls.append((cid, lease, message)) or {
                "controller": "user", "mode": "transient",
                "agentCanAct": False, "expiresIn": 3,
            }
        ),
    )
    monkeypatch.setattr(
        browser_manager, "http_live_control",
        lambda cid, lease, action: (
            calls.append((cid, lease, action)) or {
                "controller": "user" if action == "hold" else "agent",
                "mode": "held" if action == "hold" else "observe",
                "agentCanAct": action != "hold", "expiresIn": 30 if action == "hold" else 0,
            }
        ),
    )
    monkeypatch.setattr(
        browser_manager, "release_http_live",
        lambda cid, lease: calls.append((cid, lease, "released")),
    )
    base = f"/api/v2/steward/conversations/{conversation['id']}/browser/live-http"

    unauthorized = client.post(base)
    assert unauthorized.status_code in {401, 403}
    attached = client.post(base, headers=auth_headers)
    assert attached.status_code == 200
    assert attached.json()["data"]["leaseId"] == "lease-1"
    frame = client.post(
        f"{base}/frame", json={"leaseId": "lease-1"}, headers=auth_headers,
    )
    assert frame.json()["data"]["data"] == "jpeg-base64"
    assert frame.headers["cache-control"] == "no-store"
    sent = client.post(
        f"{base}/input",
        json={"leaseId": "lease-1", "message": {"type": "key", "key": "Enter"}},
        headers=auth_headers,
    )
    assert sent.json()["data"]["accepted"] is True
    assert sent.json()["data"]["collaboration"]["mode"] == "transient"
    held = client.post(
        f"{base}/control",
        json={"leaseId": "lease-1", "action": "hold"},
        headers=auth_headers,
    )
    assert held.json()["data"]["collaboration"]["mode"] == "held"
    released = client.post(
        f"{base}/release", json={"leaseId": "lease-1"}, headers=auth_headers,
    )
    assert released.json()["data"] == {"released": True}
    assert calls == [
        (conversation["id"], "lease-1", {"type": "key", "key": "Enter"}),
        (conversation["id"], "lease-1", "hold"),
        (conversation["id"], "lease-1", "released"),
    ]


def test_browser_session_info_reports_existing_agent_page():
    class Page:
        url = "https://example.com/current"

        def is_closed(self):
            return False

    session = SimpleNamespace(page=Page(), touch=lambda: None)
    manager = BrowserManager.__new__(BrowserManager)
    manager._sessions = {"conversation": session}
    manager._live_clients = {}

    assert asyncio.run(manager._session_info("conversation")) == {
        "active": True,
        "url": "https://example.com/current",
        "live": False,
        "collaboration": {
            "controller": "agent", "mode": "observe",
            "agentCanAct": True, "expiresIn": 0,
        },
    }
    assert asyncio.run(manager._session_info("missing")) == {
        "active": False,
        "url": "",
        "live": False,
        "collaboration": {
            "controller": "agent", "mode": "observe",
            "agentCanAct": True, "expiresIn": 0,
        },
    }


@pytest.mark.asyncio
async def test_live_attach_waits_for_inflight_browser_operation():
    session = SimpleNamespace(
        operation_lock=asyncio.Lock(),
        touch=lambda: None,
        page=SimpleNamespace(is_closed=lambda: False),
    )
    manager = BrowserManager.__new__(BrowserManager)
    manager._sessions = {"conversation": session}
    manager._live_clients = {}

    await session.operation_lock.acquire()
    attach = asyncio.create_task(manager._attach_live("conversation"))
    await asyncio.sleep(0)
    assert not attach.done()
    assert manager._live_clients == {}

    session.operation_lock.release()
    await attach
    assert manager._live_clients == {"conversation": 1}


@pytest.mark.asyncio
async def test_capture_response_appends_to_injected_session_workspace(
    steward_workspace, tmp_path,
):
    from app.data_channel.steward.browser_runtime import BrowserSession

    cid = str(uuid.uuid4())
    custom = workspace.SessionWorkspace(tmp_path / "super")

    class _Request:
        method = "GET"
        resource_type = "xhr"
        post_data = None
        headers = {"accept": "application/json"}

        async def all_headers(self):
            return dict(self.headers)

    class _Response:
        url = "https://example.com/api/orders"
        status = 200
        request = _Request()
        headers = {"content-type": "application/json"}

        async def all_headers(self):
            return dict(self.headers)

        async def body(self):
            return b'{"ok": true}'

    session = BrowserSession(cid, None, None, workspace=custom)
    await session._capture_response(_Response())

    rows = custom.load_captures(cid)
    assert [row["url"] for row in rows] == ["https://example.com/api/orders"]
    # 默认 steward 根下不得出现同一会话的捕获（注入工作区生效）
    assert workspace.load_captures(cid) == []


@pytest.mark.asyncio
async def test_save_state_writes_to_injected_session_workspace(
    steward_workspace, tmp_path,
):
    from app.data_channel.steward.browser_runtime import BrowserSession

    cid = str(uuid.uuid4())
    custom = workspace.SessionWorkspace(tmp_path / "super")
    saved_to = []

    class _Context:
        async def storage_state(self, path):
            saved_to.append(path)

    session = BrowserSession(cid, _Context(), None, workspace=custom)
    await session.save_state()

    assert saved_to == [str(custom.storage_state_path(cid))]


def test_list_captures_defaults_to_steward_root_and_accepts_override(
    steward_workspace, tmp_path,
):
    cid = str(uuid.uuid4())
    custom = workspace.SessionWorkspace(tmp_path / "super")
    workspace.append_capture(cid, {"id": "cap-steward", "method": "GET", "url": "https://s.example/x"})
    custom.append_capture(cid, {"id": "cap-super", "method": "GET", "url": "https://sa.example/y"})

    manager = BrowserManager.__new__(BrowserManager)
    assert [row["id"] for row in manager.list_captures(cid)] == ["cap-steward"]
    assert [
        row["id"] for row in manager.list_captures(cid, session_workspace=custom)
    ] == ["cap-super"]


def test_close_by_source_closes_only_matching_sessions():
    manager = BrowserManager.__new__(BrowserManager)
    manager._sessions = {
        "keep": SimpleNamespace(source_key="managed"),
        "drop-1": SimpleNamespace(source_key="companion:src-1"),
        "drop-2": SimpleNamespace(source_key="companion:src-1"),
    }
    closed = []
    manager.close = lambda cid: (closed.append(cid), manager._sessions.pop(cid))

    manager.close_by_source("companion:src-1")

    assert sorted(closed) == ["drop-1", "drop-2"]
    assert set(manager._sessions) == {"keep"}
    manager.close_by_source("companion:src-1")  # 幂等：无匹配会话时不动作
    assert closed == ["drop-1", "drop-2"]
