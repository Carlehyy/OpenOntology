"""YIYA-36 P2-5：浏览器受保护来源 / kernel 守卫（总闸测试在确认协议套件）。"""
import pytest

from app.super_assistant import browser_tools


@pytest.fixture(autouse=True)
def _browser_channel_open(monkeypatch):
    """基线门测试默认开启通道；关闭态由
    test_browser_full_channel_fail_closed_by_default 显式 monkeypatch。"""
    from app.config import settings
    monkeypatch.setattr(settings, "assistant_browser_channel_enabled",
                        True, raising=False)
from app.super_assistant.kernel import runtime as kernel_runtime


def test_browser_protected_origin_rejected(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_browser_protected_origins",
                        "https://mgmt.example.com, internal.corp", raising=False)
    for url in ("https://mgmt.example.com/admin", "http://internal.corp/x"):
        result = browser_tools._protected_origin_denied(url)
        assert result is True, url
    assert browser_tools._protected_origin_denied("https://other.example") is False
    # 空配置：来源识别不猜测（生产写工具由 _guard_browser_write_tool 兜底）
    monkeypatch.setattr(settings, "assistant_browser_protected_origins",
                        "", raising=False)
    assert browser_tools._protected_origin_denied("https://any.example") is False


def test_browser_full_channel_fail_closed_by_default(monkeypatch):
    """裁决 4：通道默认关闭 — 所有浏览器工具（含只读观测）先于
    source/session/storage 访问被拒。"""
    from app.config import settings
    from app.super_assistant import browser_tools as bt

    monkeypatch.setattr(settings, "assistant_browser_channel_enabled",
                        False, raising=False)
    result = bt._dispatch(None, conversation=None, name="browser_state",
                          arguments={})
    assert result["error"] == "capability_boundary_unenforced"
    result = bt._dispatch(None, conversation=None, name="browser_open",
                          arguments={"url": "https://x"})
    assert result["error"] == "capability_boundary_unenforced"




def test_kernel_rejects_ontology_write_capability():
    from app.super_assistant.kernel.contracts import ContractError

    for key in ("ontology.action.confirmed_execute",
                "ontology.execute:something", "ontology:execute"):
        try:
            kernel_runtime._reject_ontology_write_capability(key)
            raised = False
        except ContractError:
            raised = True
        assert raised, key
    # 只读能力不受影响
    kernel_runtime._reject_ontology_write_capability("ontology.query")
    kernel_runtime._reject_ontology_write_capability("model.chat")


def test_browser_protection_never_noop_on_empty_config(monkeypatch):
    """空 ASSISTANT_BROWSER_PROTECTED_ORIGINS 不得使保护退化成 no-op。"""
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_browser_protected_origins", "")
    # 回环（任意端口）与平台 compose 内部服务名默认受保护
    for url in (
        "http://localhost:8000/api/v2/auth/me",
        "https://127.0.0.1/admin",
        "http://[::1]:5173/",
        "http://backend:8000/api/v2/ontologies",
        "http://frontend/",
        "http://minio:9001/console",
        "http://neo4j:7474/browser",
    ):
        assert browser_tools._protected_origin_denied(url) is True, url


def test_browser_protection_derives_platform_public_origins(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_browser_protected_origins", "")
    monkeypatch.setattr(
        settings, "super_assistant_public_api_base_url",
        "https://platform.example.com", raising=False)
    monkeypatch.setattr(
        settings, "pipeline_file_public_app_base_url",
        "https://platform.example.com", raising=False)
    for url in (
        "https://platform.example.com/ontologies",
        "http://platform.example.com:8443/x",  # 同主机任意端口/协议
        "https://platform.example.com:443/api",
    ):
        assert browser_tools._protected_origin_denied(url) is True, url
    # 一般外网不受影响
    assert browser_tools._protected_origin_denied("https://www.example.com/doc") is False
    assert browser_tools._protected_origin_denied("https://docs.example.com") is False


def test_browser_page_tools_blocked_on_protected_current_page(monkeypatch, tmp_path):
    """已有会话/重定向落到受保护来源：所有页面级工具（含读取）拒绝。"""
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.auth.models import User
    from app.shared.database import Base
    from app.super_assistant.models import SuperAssistantConversation

    engine = create_engine(f"sqlite:///{tmp_path / 'browser-gate.db'}")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantConversation.__table__,
    ])
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as db:
        db.add(User(id="user-b", username="ub", email="ub@example.com",
                    password_hash="x", role="editor"))
        db.add(SuperAssistantConversation(
            id="conv-b", owner_id="user-b", title="b", model_config_id=None))
        db.commit()

    class _StubManager:
        def __init__(self, url):
            self.url = url
            self.calls = []

        def state(self, cid, *, actor="agent"):
            self.calls.append("state")
            return {"url": self.url}

        def click_text(self, cid, text, *, actor="agent"):
            self.calls.append("click")
            return {"clicked": text}

    stub = _StubManager("http://localhost:8000/admin/ontologies")
    monkeypatch.setattr(browser_tools, "browser_manager", stub)
    with Session() as db:
        output = browser_tools.execute_browser_tool(
            db=db, owner_id="user-b", conversation_id="conv-b",
            name="browser_click_text", arguments={"text": "删除"})
    assert "capability_boundary_unenforced" in output
    assert stub.calls == ["state"]  # 命中保护：不执行点击


def test_browser_unreadable_state_fails_closed_for_page_tools(monkeypatch, tmp_path):
    """预检不可读（非无会话）时不得旁路副作用工具。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.auth.models import User
    from app.shared.database import Base
    from app.super_assistant.models import SuperAssistantConversation

    engine = create_engine(f"sqlite:///{tmp_path / 'browser-fc.db'}")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantConversation.__table__,
    ])
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as db:
        db.add(User(id="user-b", username="ub", email="ub@example.com",
                    password_hash="x", role="editor"))
        db.add(SuperAssistantConversation(
            id="conv-b", owner_id="user-b", title="b", model_config_id=None))
        db.commit()

    class _BrokenManager:
        def state(self, cid, *, actor="agent"):
            raise RuntimeError("cdp daemon down")

        def click_text(self, cid, text, *, actor="agent"):
            raise AssertionError("不可核实时不得执行副作用工具")

    monkeypatch.setattr(browser_tools, "browser_manager", _BrokenManager())
    with Session() as db:
        output = browser_tools.execute_browser_tool(
            db=db, owner_id="user-b", conversation_id="conv-b",
            name="browser_click_text", arguments={"text": "ok"})
    assert "capability_boundary_unverifiable" in output


def test_browser_open_denies_redirect_landing_on_protected_origin(monkeypatch, tmp_path):
    """open/navigate 落地 URL 命中受保护来源：不回显页面内容。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.auth.models import User
    from app.shared.database import Base
    from app.super_assistant import files_workspace
    from app.super_assistant.models import SuperAssistantConversation

    engine = create_engine(f"sqlite:///{tmp_path / 'browser-nav.db'}")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantConversation.__table__,
    ])
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as db:
        db.add(User(id="user-b", username="ub", email="ub@example.com",
                    password_hash="x", role="editor"))
        db.add(SuperAssistantConversation(
            id="conv-b", owner_id="user-b", title="b", model_config_id=None))
        db.commit()

    class _RedirectManager:
        def start(self, cid, url, *, user_id=None, actor="agent",
                  browser_target=None, session_workspace=None):
            return {"url": "http://localhost:8000/admin", "title": "管理后台",
                    "text": "SECRET", "elements": []}

    monkeypatch.setattr(browser_sources_module(), "resolve_target", lambda *a, **k: None)
    monkeypatch.setattr(browser_tools, "browser_manager", _RedirectManager())
    with Session() as db:
        output = browser_tools.execute_browser_tool(
            db=db, owner_id="user-b", conversation_id="conv-b",
            name="browser_open", arguments={"url": "https://sso.partner.example/init"})
    assert "capability_boundary_unenforced" in output
    assert "SECRET" not in output


def browser_sources_module():
    from app.super_assistant import browser_tools as bt
    import app.data_channel.steward.browser_sources as module
    return module


def test_browser_capture_replay_blocked_on_protected_capture(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.auth.models import User
    from app.shared.database import Base
    from app.super_assistant.models import SuperAssistantConversation

    engine = create_engine(f"sqlite:///{tmp_path / 'browser-cap.db'}")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantConversation.__table__,
    ])
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as db:
        db.add(User(id="user-b", username="ub", email="ub@example.com",
                    password_hash="x", role="editor"))
        db.add(SuperAssistantConversation(
            id="conv-b", owner_id="user-b", title="b", model_config_id=None))
        db.commit()

    class _CaptureManager:
        def state(self, cid, *, actor="agent"):
            return {"url": "https://partner.example/dashboard"}

        def list_captures(self, cid, keyword=None, limit=50, *, session_workspace=None):
            return [
                {"id": "cap-ok", "url": "https://partner.example/export.csv"},
                {"id": "cap-evil", "url": "http://backend:8000/api/v2/ontologies"},
            ]

        def download(self, cid, capture_id, *, actor="agent"):
            raise AssertionError("受保护捕获不得重放下载")

    monkeypatch.setattr(browser_tools, "browser_manager", _CaptureManager())
    with Session() as db:
        output = browser_tools.execute_browser_tool(
            db=db, owner_id="user-b", conversation_id="conv-b",
            name="download_captured_file", arguments={"capture_id": "cap-evil"})
    assert "capability_boundary_unenforced" in output


def test_browser_channel_gates_navigate_input_control_and_teardown_release(
        client, auth_headers, monkeypatch):
    """Z4：navigate/input/control 走通道闸；release 仅清场例外（不驱动）。"""
    from app.config import settings
    from app.super_assistant import browser as browser_router
    from app.data_channel.steward import browser_session_service as bss

    monkeypatch.setattr(settings, "assistant_browser_channel_enabled",
                        False, raising=False)
    calls = []
    monkeypatch.setattr(
        bss, "navigate_browser",
        lambda *a, **k: calls.append("navigate") or {"data": {}})
    monkeypatch.setattr(
        bss, "browser_live_http_input",
        lambda *a, **k: calls.append("input") or {"data": {}})
    monkeypatch.setattr(
        bss, "browser_live_http_control",
        lambda *a, **k: calls.append("control") or {"data": {}})
    releases = []
    monkeypatch.setattr(
        bss, "release_browser_live_http",
        lambda *a, **k: releases.append(k.get("teardown_only")) or
        {"data": {"released": True}})

    for path, payload in (
            ("/conversations/c1/browser/navigate", {"url": "https://x"}),
            ("/conversations/c1/browser/live-http/input",
             {"leaseId": "L1", "message": {}}),
            ("/conversations/c1/browser/live-http/control",
             {"leaseId": "L1", "action": "pause"})):
        r = client.post(f"/api/v2/super-assistant{path}",
                        headers=auth_headers, json=payload)
        assert r.status_code == 403, (path, r.status_code)
        assert r.json()["detail"]["code"] == "capability_boundary_unenforced"
    assert calls == []          # 通道关闭：不驱动任何浏览器行为

    # release：清场例外 —— 放行且 teardown_only=True（仅断开租约）
    r = client.post(
        "/api/v2/super-assistant/conversations/c1/browser/live-http/release",
        headers=auth_headers, json={"leaseId": "L1"})
    assert r.status_code == 200, r.text
    assert releases == [True]
