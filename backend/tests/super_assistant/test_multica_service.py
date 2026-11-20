"""multica 服务层：配置门控、斜杠解析、连接测试与工具执行。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant import multica_client, multica_service
from app.super_assistant.models import (
    SuperAssistantMulticaConfig,
    SuperAssistantMulticaProfile,
    SuperAssistantMulticaProfileRevision,
)
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
        SuperAssistantMulticaProfile.__table__, SuperAssistantMulticaProfileRevision.__table__,
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
    # upsert 语义：同一测试内多次调用时更新现有行而不是重复插主键。
    # 服务层已 profile-only（无 schema 缺失回退、无 lazy legacy 采纳）：
    # 门控数据必须落在真实 custom profile/revision 上，旧表行只是携带
    # profile 指针的生效投影。畸形状态（缺凭据/空工作区/enabled=False）
    # 无法经 save_config 构造，因此直接以 ORM 落库并同步指针。
    profile = db.scalar(select(SuperAssistantMulticaProfile).where(
        SuperAssistantMulticaProfile.owner_id == "user-1",
    ))
    if profile is None:
        profile = SuperAssistantMulticaProfile(
            owner_id="user-1", name="我的 multica", kind="custom", is_active=True,
        )
        db.add(profile)
        db.flush()
    revision = db.scalar(
        select(SuperAssistantMulticaProfileRevision)
        .where(SuperAssistantMulticaProfileRevision.profile_id == profile.id)
        .order_by(SuperAssistantMulticaProfileRevision.revision_no.desc())
    )
    if revision is None:
        revision = SuperAssistantMulticaProfileRevision(
            profile_id=profile.id, revision_no=1,
        )
        db.add(revision)
        db.flush()
    revision.base_url = overrides.get("base_url", "http://127.0.0.1:8080")
    revision.workspace_id = overrides.get("workspace_id", "ws-1")
    revision.workspace_name = overrides.get("workspace_name", "My Workspace")
    revision.token_encrypted = overrides.get(
        "token_encrypted", multica_service.encrypt("mul-token"),
    )
    revision.enabled = overrides.get("enabled", True)
    config = db.get(SuperAssistantMulticaConfig, "user-1")
    if config is None:
        config = SuperAssistantMulticaConfig(owner_id="user-1", base_url="", workspace_id="")
        db.add(config)
    config.base_url = revision.base_url
    config.workspace_id = revision.workspace_id
    config.workspace_name = revision.workspace_name
    config.token_encrypted = revision.token_encrypted
    config.enabled = revision.enabled
    config.profile_id = profile.id
    config.profile_revision_id = revision.id
    config.source_kind = "custom"
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


def test_external_task_query_and_cancel_preserve_provider_identity(session, monkeypatch):
    _enabled_config(session)
    # legacy 形态的 ref（不含 profile_revision_id 等来源键）只在显式
    # 供应 revision_id 时被接受；新工作产生的 ref 由 execute_tool 带全来源键
    revision_id = multica_service.get_config(session, "user-1").profile_revision_id
    assert revision_id is not None
    ref = json.dumps({"issue_ref": "MYW-99", "task_id": None})
    monkeypatch.setattr(
        multica_client,
        "get_issue",
        lambda *args: {
            "id": "issue-99", "identifier": "MYW-99", "status": "in_progress",
            # 来源校验：issue 必须携带与生效配置一致的 workspace 身份
            "workspace_id": "ws-1",
        },
    )
    monkeypatch.setattr(
        multica_client,
        "list_task_runs",
        lambda *args: [{
            "id": "task-99", "status": "running", "issue_id": "issue-99", "workspace_id": "ws-1",
        }],
    )
    monkeypatch.setattr(
        multica_client,
        "cancel_task",
        lambda *args: {
            "status": "cancelled", "id": "task-99", "issue_id": "issue-99", "workspace_id": "ws-1",
        },
    )
    observed = multica_service.query_external_task(session, "user-1", ref, revision_id=revision_id)
    assert observed["status"] == "running"
    assert json.loads(observed["remote_task_ref"]) == {
        "issue_ref": "MYW-99", "task_id": "task-99", "issue_id": "issue-99",
        "profile_revision_id": revision_id,
        "base_url": "http://127.0.0.1:8080",
        "workspace_id": "ws-1",
    }
    cancelled = multica_service.cancel_external_task(session, "user-1", observed["remote_task_ref"])
    assert cancelled["status"] == "cancelled"


@pytest.mark.parametrize("profile_api", [False, True], ids=["legacy-put", "profile-patch"])
def test_endpoint_change_requires_new_explicit_credential(session, profile_api):
    config = _enabled_config(session)
    profile_id, revision_id = config.profile_id, config.profile_revision_id
    if profile_api:
        body = multica_service.MulticaProfileUpdate(base_url="http://127.0.0.1:9090", workspace_id="ws-2")
        save = lambda: multica_service.update_profile(session, "user-1", profile_id, body)
    else:
        body = MulticaConfigUpdate(base_url="http://127.0.0.1:9090", workspace_id="ws-2")
        save = lambda: multica_service.save_config(session, "user-1", body)
    with pytest.raises(multica_service.MulticaServiceError, match="重新填写 API Token"):
        save()
    # 被拒绝的保存不得追加新 revision：profile 的当前不可变 revision 不变。
    assert multica_service._current_revision(session, profile_id).id == revision_id
    assert multica_service.config_for_revision(session, "user-1", revision_id).base_url == "http://127.0.0.1:8080"
    body.token = "explicit-new-credential"
    save()
    updated = multica_service.active_config(session, "user-1")
    assert updated.base_url == "http://127.0.0.1:9090"
    assert multica_service.decrypt_token(updated) == "explicit-new-credential"
    assert multica_service.decrypt_token(multica_service.config_for_revision(session, "user-1", revision_id)) == "mul-token"


@pytest.fixture()
def external_task(session, monkeypatch):
    config = _enabled_config(session)
    issue = {"id": "issue-99", "identifier": "MYW-99", "workspace_id": "ws-1", "status": "in_progress"}
    task = {"id": "task-99", "issue_id": "issue-99", "workspace_id": "ws-1", "status": "running"}
    data = {"issue": issue, "runs": [task], "calls": [], "cancel_response": {**task, "status": "cancelled"}}
    data["ref"] = multica_service._external_ref(issue, task, config=config)

    def get_issue(base, token, workspace, reference):
        data["calls"].append(("get_issue", base, workspace, reference))
        return data["issue"]

    def list_runs(base, token, workspace, reference):
        data["calls"].append(("list_task_runs", base, workspace, reference))
        return data["runs"]

    def cancel(base, token, workspace, reference, task_id):
        data["calls"].append(("cancel_task", base, workspace, reference, task_id))
        return data["cancel_response"]

    monkeypatch.setattr(multica_client, "get_issue", get_issue)
    monkeypatch.setattr(multica_client, "list_task_runs", list_runs)
    monkeypatch.setattr(multica_client, "cancel_task", cancel)
    return data


@pytest.mark.parametrize("operation", [multica_service.query_external_task, multica_service.cancel_external_task])
def test_external_task_without_source_never_uses_active_config(session, external_task, operation):
    ref = json.dumps({"issue_ref": "MYW-99", "task_id": "task-99"})
    with pytest.raises(multica_service.MulticaServiceError, match="缺少可验证的来源"):
        operation(session, "user-1", ref)
    assert external_task["calls"] == []


@pytest.mark.parametrize("changed", [
    {"workspace_id": "foreign-workspace"}, {"workspace_id": None}, {"id": "foreign-issue"}, {"id": None},
])
@pytest.mark.parametrize("operation", [multica_service.query_external_task, multica_service.cancel_external_task])
def test_external_task_rejects_foreign_issue_before_loading_runs(session, external_task, operation, changed):
    external_task["issue"].update(changed)
    with pytest.raises(multica_service.MulticaServiceError, match="身份"):
        operation(session, "user-1", external_task["ref"])
    assert [call[0] for call in external_task["calls"]] == ["get_issue"]


@pytest.mark.parametrize("operation", [multica_service.query_external_task, multica_service.cancel_external_task])
def test_external_task_observes_original_terminal_run_without_cancelling_rerun(session, external_task, operation):
    external_task["runs"][0].update(status="completed", result="original result")
    external_task["runs"].insert(0, {
        "id": "rerun-100", "issue_id": "issue-99", "workspace_id": "ws-1", "status": "running",
    })
    result = operation(session, "user-1", external_task["ref"])
    assert result["status"] == "completed"
    assert result["content"] == "original result"
    assert json.loads(result["remote_task_ref"])["task_id"] == "task-99"
    assert [call[0] for call in external_task["calls"]] == ["get_issue", "list_task_runs"]


@pytest.mark.parametrize("task_id", ["task-99", None])
@pytest.mark.parametrize("operation", [multica_service.query_external_task, multica_service.cancel_external_task])
def test_external_task_rejects_missing_or_ambiguous_original_run(session, external_task, operation, task_id):
    external_task["runs"] = [
        {"id": "rerun-100", "issue_id": "issue-99", "workspace_id": "ws-1", "status": "running"},
        {"id": "rerun-101", "issue_id": "issue-99", "workspace_id": "ws-1", "status": "completed"},
    ]
    ref = json.loads(external_task["ref"])
    ref["task_id"] = task_id
    with pytest.raises(multica_service.MulticaServiceError, match="人工处理"):
        operation(session, "user-1", json.dumps(ref))
    assert not any(call[0] == "cancel_task" for call in external_task["calls"])


@pytest.mark.parametrize("changed", [
    {"issue_id": "foreign-issue"}, {"workspace_id": "foreign-workspace"}, {"status": None},
])
def test_external_task_rejects_run_identity_mismatch(session, external_task, changed):
    external_task["runs"][0].update(changed)
    with pytest.raises(multica_service.MulticaServiceError, match="执行身份"):
        multica_service.cancel_external_task(session, "user-1", external_task["ref"])
    assert not any(call[0] == "cancel_task" for call in external_task["calls"])


def test_external_task_uses_immutable_issue_id_after_identifier_changes(session, external_task):
    external_task["issue"]["identifier"] = "RENAMED-99"
    observed = multica_service.query_external_task(session, "user-1", external_task["ref"])
    assert external_task["calls"][0][-1] == "issue-99"
    ref = json.loads(observed["remote_task_ref"])
    assert ref["issue_id"] == "issue-99"
    assert ref["issue_ref"] == "RENAMED-99"
    assert ref["task_id"] == "task-99"


def test_external_task_observation_identity_replays_but_advances_with_status(session, external_task):
    query = lambda: multica_service.query_external_task(session, "user-1", external_task["ref"])
    first = query()
    assert query() == first
    external_task["runs"][0].update(status="completed", result="PAT echo mul-token")
    completed = query()
    assert completed["status"] == "completed"
    assert "mul-token" not in json.dumps(completed)
    assert "[redacted]" in completed["content"]
    assert completed["provider_event_id"] != first["provider_event_id"]
    assert query()["provider_event_id"] == completed["provider_event_id"]


def test_external_task_cancel_response_without_original_identity_is_unknown(session, external_task):
    external_task["cancel_response"] = {"id": "different-run", "status": "cancelled"}
    with pytest.raises(multica_client.MulticaRequestError) as raised:
        multica_service.cancel_external_task(session, "user-1", external_task["ref"])
    assert raised.value.outcome == "unknown"
    assert external_task["calls"][-1] == (
        "cancel_task", "http://127.0.0.1:8080", "ws-1", "issue-99", "task-99",
    )
