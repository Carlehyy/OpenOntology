"""超级助手浏览器协作端点（/api/v2/super-assistant/browser/*）与 browser_* 工具。

覆盖：来源 CRUD 委托、会话归属 404 语义（无 admin 旁路）、bind 写列与浏览器
会话释放、start/captures 的超助工作区透传、ticket 签发、删除会话触发浏览器
关闭、内置工具目录/分派与系统提示注入。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.data_channel.steward.browser_runtime import BrowserRuntimeError, browser_manager
from app.data_channel.steward.models import StewardBrowserSource, StewardConversation
from app.deps import get_current_user, get_db
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import browser, browser_tools, conversation_service, files_workspace, runtime
from app.super_assistant import router as conversation_router
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMessage,
    SuperAssistantMulticaConfig,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantRemoteAgent,
    SuperAssistantToolRun,
    SuperAssistantToolSetting,
)

_ENDPOINT_TABLES = [
    User.__table__,
    SuperAssistantConversation.__table__,
    StewardBrowserSource.__table__,
    StewardConversation.__table__,  # delete_browser_source 级联查询
]

_CATALOG_TABLES = [
    User.__table__,
    RoleMenuPermission.__table__,
    SuperAssistantToolSetting.__table__,
    SuperAssistantMulticaConfig.__table__,
    SuperAssistantRemoteAgent.__table__,
]

_DELETE_TABLES = [
    User.__table__,
    SuperAssistantConversation.__table__,
    SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__,
    SuperAssistantReflectionRun.__table__,
    SuperAssistantReflectionCandidate.__table__,
]


def _session_factory(tmp_path, name: str, tables):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=tables)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _user(user_id: str, role: str = "editor") -> User:
    return User(
        id=user_id, username=f"u-{user_id}", email=f"{user_id}@example.com",
        password_hash="unused", role=role,
    )


def _make_client(tmp_path, *, owner: str = "user-1"):
    TestingSession = _session_factory(tmp_path, "browser-router.db", _ENDPOINT_TABLES)
    with TestingSession() as db:
        db.add(_user("user-1"))
        db.add(_user("user-2"))
        db.add(SuperAssistantConversation(
            id="11111111-1111-1111-1111-111111111111", owner_id=owner, title="浏览器",
        ))
        db.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(browser.router, prefix="/api/v2/super-assistant")
    app.include_router(conversation_router.router, prefix="/api/v2/super-assistant")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: _user("user-1")
    return TestClient(app), TestingSession


def test_browser_source_crud_delegates_to_shared_service(tmp_path):
    client, _ = _make_client(tmp_path)
    base = "/api/v2/super-assistant/browser/sources"

    listed = client.get(base)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["data"]] == ["managed"]

    created = client.post(base, json={"name": "我的 Mac", "sourceType": "companion"})
    assert created.status_code == 201, created.text
    source = created.json()["data"]
    assert source["pairingToken"]
    assert source["sourceType"] == "companion"

    renamed = client.patch(f"{base}/{source['id']}", json={"name": "办公电脑"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["name"] == "办公电脑"

    rotated = client.post(f"{base}/{source['id']}/rotate-token")
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["data"]["pairingToken"] != source["pairingToken"]

    listed = client.get(base).json()["data"]
    assert [row["id"] for row in listed] == ["managed", source["id"]]
    assert "pairingToken" not in listed[1]

    deleted = client.delete(f"{base}/{source['id']}")
    assert deleted.status_code == 204, deleted.text
    assert [row["id"] for row in client.get(base).json()["data"]] == ["managed"]


def test_companion_script_downloads_with_stable_filename(tmp_path):
    client, _ = _make_client(tmp_path)
    resp = client.get("/api/v2/super-assistant/browser/companion/script")
    assert resp.status_code == 200, resp.text
    assert "openontology-browser-companion.mjs" in resp.headers["content-disposition"]


def test_conversation_endpoints_enforce_ownership_without_admin_bypass(tmp_path):
    # 会话属于 user-2：当前用户 user-1（即使 admin 也不例外）一律 404
    client, _ = _make_client(tmp_path, owner="user-2")
    cid = "11111111-1111-1111-1111-111111111111"
    checks = [
        client.get(f"/api/v2/super-assistant/conversations/{cid}/browser/session"),
        client.get(f"/api/v2/super-assistant/conversations/{cid}/browser/captures"),
        client.post(f"/api/v2/super-assistant/conversations/{cid}/browser/ticket"),
        client.post(
            f"/api/v2/super-assistant/conversations/{cid}/browser/navigate",
            json={"url": "https://example.com"},
        ),
        client.put(
            f"/api/v2/super-assistant/conversations/{cid}/browser/source",
            json={"sourceId": None},
        ),
        client.post(
            f"/api/v2/super-assistant/conversations/{cid}/browser/start",
            json={"url": "https://example.com"},
        ),
    ]
    assert [resp.status_code for resp in checks] == [404] * len(checks)


def test_bind_browser_source_writes_column_and_closes_session(tmp_path, monkeypatch):
    client, TestingSession = _make_client(tmp_path)
    source = client.post(
        "/api/v2/super-assistant/browser/sources",
        json={"name": "我的 Mac", "sourceType": "companion"},
    ).json()["data"]
    closed = []
    monkeypatch.setattr(browser_manager, "close", lambda cid: closed.append(cid))

    bound = client.put(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/source",
        json={"sourceId": source["id"]},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["data"] == {
        "conversationId": "11111111-1111-1111-1111-111111111111",
        "browserSourceId": source["id"],
    }
    assert closed == ["11111111-1111-1111-1111-111111111111"]
    with TestingSession() as db:
        conversation = db.get(SuperAssistantConversation, "11111111-1111-1111-1111-111111111111")
        assert conversation.browser_source_id == source["id"]

    reset = client.put(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/source",
        json={"sourceId": "managed"},
    )
    assert reset.json()["data"]["browserSourceId"] == "managed"
    with TestingSession() as db:
        assert db.get(SuperAssistantConversation, "11111111-1111-1111-1111-111111111111").browser_source_id is None


def test_conversation_out_exposes_browser_source_id(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path)
    source = client.post(
        "/api/v2/super-assistant/browser/sources",
        json={"name": "我的 Mac", "sourceType": "companion"},
    ).json()["data"]
    monkeypatch.setattr(browser_manager, "close", lambda cid: None)
    cid = "11111111-1111-1111-1111-111111111111"

    bound = client.put(
        f"/api/v2/super-assistant/conversations/{cid}/browser/source",
        json={"sourceId": source["id"]},
    )
    assert bound.status_code == 200, bound.text

    listed = client.get("/api/v2/super-assistant/conversations")
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == cid)
    assert row["browser_source_id"] == source["id"]

    patched = client.patch(
        f"/api/v2/super-assistant/conversations/{cid}", json={"title": "改名"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["browser_source_id"] == source["id"]

    reset = client.put(
        f"/api/v2/super-assistant/conversations/{cid}/browser/source",
        json={"sourceId": "managed"},
    )
    assert reset.status_code == 200, reset.text
    listed = client.get("/api/v2/super-assistant/conversations").json()
    assert next(item for item in listed if item["id"] == cid)["browser_source_id"] is None


def test_bind_rejects_foreign_browser_source(tmp_path):
    client, TestingSession = _make_client(tmp_path)
    with TestingSession() as db:
        db.add(StewardBrowserSource(
            id="source-2", user_id="user-2", name="他人电脑",
            source_type="companion", enabled=True,
        ))
        db.commit()
    resp = client.put(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/source",
        json={"sourceId": "source-2"},
    )
    assert resp.status_code == 422, resp.text
    assert "他人的浏览器来源" in resp.json()["detail"]


def test_start_browser_resolves_target_with_super_assistant_workspace(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path)
    monkeypatch.setattr(
        settings, "super_assistant_workspace_root", str(tmp_path / "super"),
    )
    captured = {}

    def fake_start(cid, url, **kwargs):
        captured.update({"cid": cid, "url": url, **kwargs})
        return {"url": url, "browserSource": kwargs["browser_target"].key}

    monkeypatch.setattr(browser_manager, "start", fake_start)
    resp = client.post(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/start",
        json={"url": "https://example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["browserSource"] == "managed"
    assert captured["cid"] == "11111111-1111-1111-1111-111111111111"
    assert captured["actor"] == "user"
    assert captured["user_id"] == "user-1"
    assert captured["browser_target"].key == "managed"
    assert captured["session_workspace"]._root == (tmp_path / "super").resolve()


def test_start_browser_with_offline_companion_source_returns_422(tmp_path):
    client, _ = _make_client(tmp_path)
    source = client.post(
        "/api/v2/super-assistant/browser/sources",
        json={"name": "我的 Mac", "sourceType": "companion"},
    ).json()["data"]
    client.put(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/source",
        json={"sourceId": source["id"]},
    )
    resp = client.post(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/start",
        json={"url": "https://example.com"},
    )
    assert resp.status_code == 422, resp.text
    assert "尚未在线" in resp.json()["detail"]


def test_navigate_without_started_browser_returns_422(tmp_path):
    client, _ = _make_client(tmp_path)
    resp = client.post(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/navigate",
        json={"url": "https://example.com"},
    )
    assert resp.status_code == 422, resp.text
    assert "尚未启动浏览器" in resp.json()["detail"]


def test_captures_read_from_super_assistant_workspace(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path)
    monkeypatch.setattr(settings, "steward_workspace_root", str(tmp_path / "steward"))
    monkeypatch.setattr(settings, "super_assistant_workspace_root", str(tmp_path / "super"))
    cid = "11111111-1111-1111-1111-111111111111"
    from app.data_channel.steward import workspace as steward_workspace

    steward_workspace.steward_session_workspace().append_capture(cid, {
        "id": "cap-steward", "method": "GET", "url": "https://s.example/x",
    })
    files_workspace.session_workspace().append_capture(cid, {
        "id": "cap-super", "method": "GET", "url": "https://sa.example/y",
    })

    resp = client.get(f"/api/v2/super-assistant/conversations/{cid}/browser/captures")
    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()["data"]] == ["cap-super"]


def test_ticket_issues_single_use_ticket(tmp_path):
    client, _ = _make_client(tmp_path)
    resp = client.post(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/ticket",
    )
    assert resp.status_code == 200, resp.text
    ticket = resp.json()["data"]["ticket"]
    assert resp.json()["data"]["expiresIn"] == 60
    assert browser_manager.redeem_ticket(ticket, "11111111-1111-1111-1111-111111111111") == (True, "user-1")
    assert browser_manager.redeem_ticket(ticket, "11111111-1111-1111-1111-111111111111") == (False, None)


def test_live_http_family_delegates_with_envelope(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path)
    collaboration = {
        "controller": "agent", "mode": "observe", "agentCanAct": True, "expiresIn": 0,
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
        lambda cid, lease, message: collaboration,
    )
    monkeypatch.setattr(
        browser_manager, "http_live_control",
        lambda cid, lease, action: collaboration,
    )
    released = []
    monkeypatch.setattr(
        browser_manager, "release_http_live",
        lambda cid, lease: released.append((cid, lease)),
    )
    base = "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/live-http"

    attached = client.post(base)
    assert attached.status_code == 200, attached.text
    assert attached.json()["data"]["leaseId"] == "lease-1"
    frame = client.post(f"{base}/frame", json={"leaseId": "lease-1"})
    assert frame.json()["data"]["data"] == "jpeg-base64"
    assert frame.headers["cache-control"] == "no-store"
    sent = client.post(
        f"{base}/input",
        json={"leaseId": "lease-1", "message": {"type": "key", "key": "Enter"}},
    )
    assert sent.json()["data"]["accepted"] is True
    held = client.post(
        f"{base}/control", json={"leaseId": "lease-1", "action": "hold"},
    )
    assert held.json()["data"]["collaboration"]["controller"] == "agent"
    done = client.post(f"{base}/release", json={"leaseId": "lease-1"})
    assert done.json()["data"] == {"released": True}
    assert released == [("11111111-1111-1111-1111-111111111111", "lease-1")]


def test_session_info_reports_inactive_without_browser(tmp_path):
    client, _ = _make_client(tmp_path)
    resp = client.get(
        "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/session",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["active"] is False


def test_live_websocket_double_mounted_under_super_assistant_prefix(client):
    from starlette.websockets import WebSocketDisconnect

    paths = {getattr(route, "path", "") for route in client.app.routes}
    assert (
        "/api/v2/super-assistant/conversations/{conversation_id}/browser/live"
        in paths
    )
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            "/api/v2/super-assistant/conversations/11111111-1111-1111-1111-111111111111/browser/live"
            "?ticket=invalid"
        ):
            pass
    assert caught.value.code == 4401


def test_delete_conversation_closes_browser_session(tmp_path, monkeypatch):
    TestingSession = _session_factory(tmp_path, "browser-delete.db", _DELETE_TABLES)
    with TestingSession() as db:
        db.add(_user("user-1"))
        db.add(SuperAssistantConversation(
            id="11111111-1111-1111-1111-111111111111", owner_id="user-1", title="浏览器",
        ))
        db.commit()
    closed = []
    monkeypatch.setattr(browser_manager, "close", lambda cid: closed.append(cid))
    monkeypatch.setattr(
        settings, "super_assistant_workspace_root", str(tmp_path / "super"),
    )

    with TestingSession() as db:
        response = conversation_service.delete_conversation(
            "11111111-1111-1111-1111-111111111111", db, _user("user-1"),
        )
        assert response.status_code == 204
        assert db.get(SuperAssistantConversation, "11111111-1111-1111-1111-111111111111") is None
    assert closed == ["11111111-1111-1111-1111-111111111111"]


def test_builtin_tools_and_catalog_include_browser_tools(tmp_path):
    names_default = {tool["name"] for tool in runtime._builtin_tools(False)}
    names_agent = {tool["name"] for tool in runtime._builtin_tools(True)}
    assert browser_tools.BROWSER_TOOL_NAMES <= names_default
    assert browser_tools.BROWSER_TOOL_NAMES <= names_agent
    assert {
        "browser_state", "browser_page_resources", "browser_network_requests",
    } <= runtime._READ_ONLY_BUILTIN_TOOLS
    # 与数据管家对齐：浏览器写操作免审批
    assert not (
        browser_tools.BROWSER_TOOL_NAMES & runtime._CONFIRMATION_REQUIRED_BUILTIN_TOOLS
    )

    TestingSession = _session_factory(tmp_path, "browser-catalog.db", _CATALOG_TABLES)
    with TestingSession() as db:
        db.add(_user("user-1"))
        db.commit()
        catalog = {
            entry["name"]: entry
            for entry in runtime.builtin_tool_catalog(db, "user-1")
        }
    for name in browser_tools.BROWSER_TOOL_NAMES:
        assert catalog[name]["enabled"] is True
        assert catalog[name]["available"] is True
    assert catalog["browser_state"]["category"] == "read_only"
    assert catalog["browser_network_requests"]["category"] == "read_only"
    assert catalog["browser_open"]["category"] == "standard"


def test_execute_browser_tool_open_uses_super_assistant_workspace(tmp_path, monkeypatch):
    TestingSession = _session_factory(tmp_path, "browser-tool.db", _ENDPOINT_TABLES)
    with TestingSession() as db:
        db.add(_user("user-1"))
        db.add(SuperAssistantConversation(
            id="11111111-1111-1111-1111-111111111111", owner_id="user-1", title="浏览器",
        ))
        db.commit()
    monkeypatch.setattr(
        settings, "super_assistant_workspace_root", str(tmp_path / "super"),
    )
    captured = {}

    def fake_start(cid, url, **kwargs):
        captured.update({"cid": cid, "url": url, **kwargs})
        return {"url": url}

    monkeypatch.setattr(browser_manager, "start", fake_start)
    with TestingSession() as db:
        result = json.loads(browser_tools.execute_browser_tool(
            db, owner_id="user-1", conversation_id="11111111-1111-1111-1111-111111111111",
            name="browser_open", arguments={"url": "https://example.com"},
        ))
    assert result == {"url": "https://example.com"}
    assert captured["actor"] == "agent"
    assert captured["user_id"] == "user-1"
    assert captured["browser_target"].key == "managed"
    assert captured["session_workspace"]._root == (tmp_path / "super").resolve()


def test_execute_browser_tool_maps_ownership_and_runtime_errors(tmp_path, monkeypatch):
    TestingSession = _session_factory(tmp_path, "browser-tool-err.db", _ENDPOINT_TABLES)
    with TestingSession() as db:
        db.add(_user("user-1"))
        db.add(SuperAssistantConversation(
            id="11111111-1111-1111-1111-111111111111", owner_id="user-1", title="浏览器",
        ))
        db.commit()

    with TestingSession() as db:
        missing = json.loads(browser_tools.execute_browser_tool(
            db, owner_id="user-1", conversation_id="no-such-conversation",
            name="browser_state", arguments={},
        ))
        foreign = json.loads(browser_tools.execute_browser_tool(
            db, owner_id="user-2", conversation_id="11111111-1111-1111-1111-111111111111",
            name="browser_state", arguments={},
        ))
    assert missing == {"error": "会话不存在"}
    assert foreign == {"error": "会话不存在"}

    def broken_state(cid, *, actor):
        raise BrowserRuntimeError("无法连接平台浏览器")

    monkeypatch.setattr(browser_manager, "state", broken_state)
    with TestingSession() as db:
        failed = json.loads(browser_tools.execute_browser_tool(
            db, owner_id="user-1", conversation_id="11111111-1111-1111-1111-111111111111",
            name="browser_state", arguments={},
        ))
    assert failed == {"error": "无法连接平台浏览器"}


def test_system_prompt_includes_browser_collaboration_rules():
    prompt = runtime._system_prompt([])
    assert "浏览器协作" in prompt
    assert "browser_network_requests" in prompt
    assert "browser_scroll" in prompt
    assert "索要密码" in prompt


def test_execute_browser_scroll_dispatches_position(tmp_path, monkeypatch):
    TestingSession = _session_factory(tmp_path, "browser-scroll.db", _ENDPOINT_TABLES)
    with TestingSession() as db:
        db.add(_user("user-1"))
        db.add(SuperAssistantConversation(
            id="11111111-1111-1111-1111-111111111111", owner_id="user-1", title="浏览器",
        ))
        db.commit()
    captured = {}

    def fake_scroll(cid, position, **kwargs):
        captured.update({"cid": cid, "position": position, **kwargs})
        return {"url": "https://example.com", "scroll": {"position": position}}

    monkeypatch.setattr(browser_manager, "scroll", fake_scroll)
    with TestingSession() as db:
        result = json.loads(browser_tools.execute_browser_tool(
            db, owner_id="user-1", conversation_id="11111111-1111-1111-1111-111111111111",
            name="browser_scroll", arguments={"position": "bottom"},
        ))
    assert result["scroll"]["position"] == "bottom"
    assert captured == {
        "cid": "11111111-1111-1111-1111-111111111111",
        "position": "bottom",
        "actor": "agent",
    }
