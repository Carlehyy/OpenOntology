"""multica 服务层：配置门控、斜杠解析、连接测试与工具执行。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant import multica_client, multica_service
from app.super_assistant.models import SuperAssistantMulticaConfig
from app.super_assistant.schemas import MulticaConfigUpdate
from app.super_assistant.multica_client import MulticaClientError


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'multica.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantMulticaConfig.__table__,
    ])
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        ))
        db.commit()
        yield db


def _enabled_config(db, **overrides):
    # upsert 语义：同一测试内多次调用时更新现有行而不是重复插主键
    config = db.get(SuperAssistantMulticaConfig, "user-1")
    if config is None:
        config = SuperAssistantMulticaConfig(owner_id="user-1", base_url="", workspace_id="")
        db.add(config)
    config.base_url = overrides.get("base_url", "http://127.0.0.1:8080")
    config.workspace_id = overrides.get("workspace_id", "ws-1")
    config.workspace_name = overrides.get("workspace_name", "My Workspace")
    config.token_encrypted = overrides.get(
        "token_encrypted", multica_service.encrypt("mul-token"),
    )
    config.enabled = overrides.get("enabled", True)
    db.commit()
    return config


# ---------------------------------------------------------------------------
# 斜杠命令解析
# ---------------------------------------------------------------------------

def test_parse_slash_command_variants():
    ok = multica_service.parse_slash_command("/multica:list_agents", configured=True)
    assert (ok.state, ok.tool_name, ok.tail) == ("ok", "multica_list_agents", "")
    alias = multica_service.parse_slash_command("/Multica:AGENTS", configured=True)
    assert alias.tool_name == "multica_list_agents"
    fullwidth = multica_service.parse_slash_command(
        "/multica：create_task 给全栈工程师修复登录", configured=True,
    )
    assert fullwidth.tool_name == "multica_create_task"
    assert fullwidth.tail == "给全栈工程师修复登录"
    unknown = multica_service.parse_slash_command("/multica:warp", configured=True)
    assert (unknown.state, unknown.raw_command) == ("unknown", "warp")
    unconfigured = multica_service.parse_slash_command("/multica:list_agents", configured=False)
    assert unconfigured.state == "unconfigured"
    # 非命令前缀（含消息中间出现）一律不解析
    for message in ("你好", "普通消息 /multica:list_agents", "/multicasomething", "/multica"):
        assert multica_service.parse_slash_command(message, configured=True) is None


def test_guidance_text_lists_available_commands():
    unknown = multica_service.parse_slash_command("/multica:warp", configured=True)
    text = multica_service.guidance_text(unknown)
    assert "未知的 multica 命令“warp”" in text
    for usage in ("/multica:list_agents", "/multica:list_tasks", "/multica:create_task"):
        assert usage in text
    unconfigured = multica_service.parse_slash_command("/multica:list_agents", configured=False)
    assert "外部集成" in multica_service.guidance_text(unconfigured)


# ---------------------------------------------------------------------------
# 配置 CRUD 与门控
# ---------------------------------------------------------------------------

def test_save_config_encrypts_token_and_normalizes_base_url(session):
    body = MulticaConfigUpdate(
        base_url="http://127.0.0.1:8080/", token="mul-secret",
        workspace_id="ws-1", workspace_name="My Workspace", enabled=True,
    )
    config = multica_service.save_config(session, "user-1", body)
    assert config.base_url == "http://127.0.0.1:8080"
    assert config.workspace_name == "My Workspace"
    assert config.token_encrypted and config.token_encrypted != "mul-secret"
    assert multica_service.decrypt_token(config) == "mul-secret"


def test_save_config_keeps_existing_token_when_blank(session):
    _enabled_config(session)
    body = MulticaConfigUpdate(
        base_url="http://127.0.0.1:8080", token=None,
        workspace_id="ws-2", enabled=True,
    )
    config = multica_service.save_config(session, "user-1", body)
    assert multica_service.decrypt_token(config) == "mul-token"
    assert config.workspace_id == "ws-2"
    # workspace_name 缺省时保留已存名称（不被置空）
    assert config.workspace_name == "My Workspace"


def test_save_config_rejects_enabling_without_token(session):
    _enabled_config(session, token_encrypted=None)
    body = MulticaConfigUpdate(
        base_url="http://127.0.0.1:8080", token=None,
        workspace_id="ws-1", enabled=True,
    )
    with pytest.raises(multica_service.MulticaServiceError, match="API Token"):
        multica_service.save_config(session, "user-1", body)


def test_save_config_rejects_invalid_url(session):
    body = MulticaConfigUpdate(
        base_url="ftp://multica.local", token="mul",
        workspace_id="ws-1", enabled=False,
    )
    with pytest.raises(MulticaClientError):
        multica_service.save_config(session, "user-1", body)


def test_config_view_gates_commands_by_availability(session):
    empty = multica_service.config_view(multica_service.get_config(session, "user-1"))
    assert empty.configured is False and empty.commands == []

    disabled = _enabled_config(session, enabled=False)
    view = multica_service.config_view(disabled)
    assert view.configured is True and view.enabled is False and view.commands == []

    enabled = _enabled_config(session)
    view = multica_service.config_view(enabled)
    assert view.enabled is True and view.token_set is True
    assert view.workspace_name == "My Workspace"
    assert [item.command for item in view.commands] == [
        "list_agents", "list_tasks", "create_task",
    ]
    assert [item.write for item in view.commands] == [False, False, True]

    incomplete = _enabled_config(session, workspace_id=" ")
    assert multica_service.config_view(incomplete).enabled is False


def test_active_config_requires_complete_and_enabled_row(session):
    assert multica_service.active_config(session, "user-1") is None
    _enabled_config(session)
    assert multica_service.active_config(session, "user-1") is not None
    _enabled_config(session, enabled=False)
    assert multica_service.active_config(session, "user-1") is None


# ---------------------------------------------------------------------------
# 连接测试
# ---------------------------------------------------------------------------

def test_connection_success_records_status_and_workspaces(session, monkeypatch):
    _enabled_config(session)
    monkeypatch.setattr(
        multica_client, "fetch_me",
        lambda base_url, token: {"name": "admin", "email": "admin@multica.local"},
    )
    monkeypatch.setattr(
        multica_client, "list_workspaces",
        lambda base_url, token: [{"id": "ws-1", "name": "My Workspace", "slug": "my"}],
    )
    result = multica_service.test_connection(session, "user-1")
    assert result.ok is True
    assert result.account_name == "admin"
    assert result.workspaces[0].id == "ws-1"
    config = multica_service.get_config(session, "user-1")
    assert config.last_test_status == "success"
    # 连接测试顺带回填当前工作区的显示名（下拉不再显示裸 UUID）
    assert config.workspace_name == "My Workspace"


def test_connection_failure_returns_ok_false(session, monkeypatch):
    _enabled_config(session)
    def _boom(base_url, token):
        raise MulticaClientError("multica 请求失败（/api/me）：HTTP 401 missing authorization")
    monkeypatch.setattr(multica_client, "fetch_me", _boom)
    result = multica_service.test_connection(session, "user-1")
    assert result.ok is False and "401" in result.message
    assert multica_service.get_config(session, "user-1").last_test_status == "error"


def test_connection_requires_url_and_token(session):
    result = multica_service.test_connection(session, "user-1")
    assert result.ok is False and "请先填写" in result.message


# ---------------------------------------------------------------------------
# 已保存配置的工作区列表（配置弹窗打开即拉取）
# ---------------------------------------------------------------------------

def test_list_config_workspaces_returns_live_list_without_test_side_effects(
    session, monkeypatch,
):
    config = _enabled_config(session)
    monkeypatch.setattr(
        multica_client, "list_workspaces",
        lambda base_url, token: [
            {"id": "ws-1", "name": "My Workspace", "slug": "my"},
            {"id": "ws-2", "name": "E2E 工作区", "slug": "e2e"},
        ],
    )
    result = multica_service.list_config_workspaces(session, "user-1")
    assert [item.id for item in result.workspaces] == ["ws-1", "ws-2"]
    assert result.workspaces[1].name == "E2E 工作区"
    # 只读拉取：不写连接测试状态列（区别于 test_connection）
    assert config.last_test_status is None


def test_list_config_workspaces_requires_complete_config(session):
    with pytest.raises(multica_service.MulticaServiceError, match="尚未配置"):
        multica_service.list_config_workspaces(session, "user-1")
    # 已保存地址但缺凭据同样视为未配置完整
    _enabled_config(session, token_encrypted=None)
    with pytest.raises(multica_service.MulticaServiceError, match="尚未配置"):
        multica_service.list_config_workspaces(session, "user-1")


# ---------------------------------------------------------------------------
# 工具执行
# ---------------------------------------------------------------------------

def test_execute_tool_rejects_unconfigured(session):
    with pytest.raises(multica_service.MulticaServiceError, match="未配置"):
        multica_service.execute_tool(session, "user-1", "multica_list_agents", {})


def test_execute_list_agents_trims_heavy_fields(session, monkeypatch):
    _enabled_config(session)
    monkeypatch.setattr(
        multica_client, "list_agents",
        lambda base_url, token, workspace_id: [
            {
                "id": "agent-1", "name": "全栈工程师（KiMi）",
                "description": "由 Kimi Code CLI 驱动的编码智能体",
                "instructions": "## 角色\n超长系统提示" * 50,
                "runtime_bound": True,
            },
        ],
    )
    payload = json.loads(multica_service.execute_tool(session, "user-1", "multica_list_agents", {}))
    assert payload["count"] == 1
    assert payload["agents"][0]["name"] == "全栈工程师（KiMi）"
    assert "instructions" not in payload["agents"][0]
    assert payload["agents"][0]["runtime_bound"] is True


def test_execute_list_tasks_clamps_limit(session, monkeypatch):
    _enabled_config(session)
    captured: list = []

    def _capture(base_url, token, workspace_id, *, status, assignee, limit):
        captured.append({"status": status, "assignee": assignee, "limit": limit})
        return [{"identifier": "MYW-1", "title": "任务", "status": "in_progress"}]

    monkeypatch.setattr(multica_client, "list_issues", _capture)
    payload = json.loads(multica_service.execute_tool(session, "user-1", "multica_list_tasks", {
        "limit": 999, "assignee": "  ",
    }))
    assert captured[0]["limit"] == multica_service._MAX_LIST_LIMIT
    assert captured[0]["assignee"] is None
    assert payload["issues"][0]["identifier"] == "MYW-1"


def test_execute_create_task_requires_title(session):
    _enabled_config(session)
    with pytest.raises(multica_service.MulticaServiceError, match="title"):
        multica_service.execute_tool(session, "user-1", "multica_create_task", {"title": "  "})


def test_execute_create_task_resolves_assignee_name_to_agent_id(session, monkeypatch):
    _enabled_config(session)
    monkeypatch.setattr(
        multica_client, "list_agents",
        lambda base_url, token, workspace_id: [
            {"id": "agent-1", "name": "全栈工程师（KiMi）"},
        ],
    )
    created: list = []
    def _capture(base_url, token, workspace_id, *, title, description, assignee_id, allow_duplicate=False):
        created.append({"title": title, "description": description, "assignee_id": assignee_id})
        return {"identifier": "MYW-99", "title": title, "status": "open"}

    monkeypatch.setattr(multica_client, "create_issue", _capture)
    payload = json.loads(multica_service.execute_tool(session, "user-1", "multica_create_task", {
        "title": "修复登录", "assignee": "全栈工程师", "description": "略",
    }))
    # 名称 → ID 解析后按服务端契约提交（assignee_type=agent + assignee_id）
    assert created[0]["assignee_id"] == "agent-1"
    assert payload["created"] is True and payload["issue"]["identifier"] == "MYW-99"
    assert payload["assignee"] == "全栈工程师（KiMi）"
    assert "指派给 全栈工程师（KiMi）" in payload["note"]

    # 智能体 UUID 直传：跳过名称解析
    payload_direct = json.loads(multica_service.execute_tool(session, "user-1", "multica_create_task", {
        "title": "再次修复", "assignee": "258652f6-44fa-498b-83b7-ec5016552931",
    }))
    assert created[1]["assignee_id"] == "258652f6-44fa-498b-83b7-ec5016552931"
    assert payload_direct["assignee"] == "258652f6-44fa-498b-83b7-ec5016552931"


def test_execute_create_task_reports_unknown_assignee(session, monkeypatch):
    _enabled_config(session)
    monkeypatch.setattr(
        multica_client, "list_agents",
        lambda base_url, token, workspace_id: [{"id": "agent-1", "name": "软件架构师"}],
    )
    monkeypatch.setattr(
        multica_client, "create_issue",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("解析失败不应建单")),
    )
    with pytest.raises(multica_client.MulticaClientError, match="未在工作区找到"):
        multica_service.execute_tool(session, "user-1", "multica_create_task", {
            "title": "修复登录", "assignee": "不存在",
        })
