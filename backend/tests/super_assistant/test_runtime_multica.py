"""multica 集成在 runtime 的行为：门控、斜杠命令、审批与 SSE 契约。"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.shared.database import Base
from app.super_assistant import multica_client, multica_service, runtime
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMcpServer,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMessage,
    SuperAssistantMulticaConfig,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantSkill,
    SuperAssistantToolRun,
    SuperAssistantToolSetting,
)

_RUNTIME_TABLES = [
    User.__table__, ModelConfig.__table__,
    RoleMenuPermission.__table__,  # 委派目录注入的菜单权限查询
    SuperAssistantConversation.__table__, SuperAssistantSkill.__table__,
    SuperAssistantMcpServer.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantToolSetting.__table__, SuperAssistantMemory.__table__,
    SuperAssistantMemoryProfile.__table__, SuperAssistantReflectionRun.__table__,
    SuperAssistantReflectionCandidate.__table__, SuperAssistantMulticaConfig.__table__,
]


def _fake_chat_stream(responses, seen=None):
    """伪造 provider.chat_stream：记录每次调用的 (messages, tools)。"""

    def _fake(_call_kwargs, messages, tools, on_delta=None):
        if seen is not None:
            seen.append({"messages": [dict(item) for item in messages], "tools": list(tools)})
        result = next(responses)
        content = result.get("content")
        if content and on_delta:
            on_delta(content)
        return result

    return _fake


def _prepare(tmp_path, monkeypatch, *, message: str, configured: bool, responses, seen=None,
             db_name: str = "multica-runtime.db"):
    engine = create_engine(
        f"sqlite:///{tmp_path / db_name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)

    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id="model-1", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-1",
        ))
        db.add(SuperAssistantConversation(
            id="conversation-1", owner_id="user-1", title="multica", model_config_id="model-1",
        ))
        db.add(SuperAssistantMessage(
            id="user-message-1", conversation_id="conversation-1",
            role="user", content=message, status="complete",
        ))
        db.add(SuperAssistantMessage(
            id="assistant-message-1", conversation_id="conversation-1",
            role="assistant", content="", status="streaming",
        ))
        if configured:
            db.add(SuperAssistantMulticaConfig(
                owner_id="user-1",
                base_url="http://127.0.0.1:8080",
                workspace_id="ws-1",
                token_encrypted=multica_service.encrypt("mul-token"),
                enabled=True,
            ))
        db.commit()

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses, seen))
    return TestingSession


def _stream():
    return runtime.stream_chat(
        conversation_id="conversation-1",
        owner_id="user-1",
        assistant_message_id="assistant-message-1",
        requested_model_id="model-1",
    )


def test_slash_without_config_returns_guidance_without_llm(tmp_path, monkeypatch):
    seen: list = []
    # 若被调用会耗尽迭代器/记录调用——两种都断言为“零次”
    _prepare(
        tmp_path, monkeypatch,
        message="/multica:list_agents", configured=False,
        responses=iter([]), seen=seen,
    )
    events = "".join(_stream())
    assert "multica 集成尚未配置" in events
    assert "外部集成" in events
    assert "event: tool_start" not in events
    assert "event: text_delta" in events
    assert seen == []  # 确定性引导：完全不经过 LLM


def test_slash_unknown_command_lists_available_commands(tmp_path, monkeypatch):
    seen: list = []
    _prepare(
        tmp_path, monkeypatch,
        message="/multica:warp speed", configured=True,
        responses=iter([]), seen=seen,
    )
    events = "".join(_stream())
    assert "未知的 multica 命令“warp”" in events
    assert "/multica:list_agents" in events and "/multica:create_task" in events
    assert "event: tool_start" not in events
    assert seen == []


def test_read_command_without_tail_executes_directly(tmp_path, monkeypatch):
    seen: list = []
    TestingSession = _prepare(
        tmp_path, monkeypatch,
        message="/multica:list_agents", configured=True,
        responses=iter([
            {"content": "当前工作台有 1 个智能体：全栈工程师（KiMi）。",
             "tool_calls": [], "usage": {"inputTokens": 5, "outputTokens": 3}},
        ]),
        seen=seen,
    )
    monkeypatch.setattr(
        multica_client, "list_agents",
        lambda base_url, token, workspace_id: [
            {"id": "agent-1", "name": "全栈工程师（KiMi）",
             "description": "编码智能体", "runtime_bound": True},
        ],
    )
    events = "".join(_stream())
    assert "event: tool_start" in events
    assert "multica_list_agents" in events
    assert "event: tool_result" in events
    assert "全栈工程师（KiMi）" in events
    # 总结轮不提供任何工具（避免总结轮再触发工具调用）
    assert len(seen) == 1
    assert seen[0]["tools"] == []
    assert any(item.get("role") == "tool" for item in seen[0]["messages"])

    with TestingSession() as db:
        saved = db.get(SuperAssistantMessage, "assistant-message-1")
        assert saved.status == "complete"
        assert saved.content == "当前工作台有 1 个智能体：全栈工程师（KiMi）。"
        tool_run = db.query(SuperAssistantToolRun).one()
        assert tool_run.tool_name == "multica_list_agents"
        assert tool_run.status == "success"
        assert tool_run.requires_confirmation is False


def test_read_command_with_tail_routes_through_forced_llm_round(tmp_path, monkeypatch):
    seen: list = []
    _prepare(
        tmp_path, monkeypatch,
        message="/multica:list_tasks 只看进行中的", configured=True,
        responses=iter([
            {"content": None, "tool_calls": [
                {"id": "call-1", "name": "multica_list_tasks",
                 "arguments": {"status": "in_progress"}},
            ], "usage": {"inputTokens": 5, "outputTokens": 3}},
            {"content": "有 1 个进行中的任务。", "tool_calls": [],
             "usage": {"inputTokens": 7, "outputTokens": 4}},
        ]),
        seen=seen,
    )
    captured: list = []

    def _capture(base_url, token, workspace_id, *, status, assignee, limit):
        captured.append({"status": status})
        return [{"identifier": "MYW-86", "title": "UI 优化", "status": "in_progress"}]

    monkeypatch.setattr(multica_client, "list_issues", _capture)
    events = "".join(_stream())
    # 强制轮：首轮工具目录收敛到命令指定的工具，且用户消息被注入平台指令
    assert seen[0]["tools"] == [item for item in seen[0]["tools"]]
    assert [item["name"] for item in seen[0]["tools"]] == ["multica_list_tasks"]
    last_user = [item for item in seen[0]["messages"] if item.get("role") == "user"][-1]
    assert "[平台指令]" in last_user["content"] and "multica_list_tasks" in last_user["content"]
    assert captured[0]["status"] == "in_progress"
    assert "event: tool_result" in events
    assert "MYW-86" in events


def test_create_task_command_requires_confirmation_then_executes(tmp_path, monkeypatch):
    seen: list = []
    TestingSession = _prepare(
        tmp_path, monkeypatch,
        message="/multica:create_task 给全栈工程师（KiMi）修复登录问题", configured=True,
        responses=iter([
            {"content": None, "tool_calls": [
                {"id": "call-1", "name": "multica_create_task",
                 "arguments": {"title": "修复登录问题", "assignee": "全栈工程师（KiMi）"}},
            ], "usage": {"inputTokens": 5, "outputTokens": 3}},
            {"content": "任务 MYW-99 已创建并指派。", "tool_calls": [],
             "usage": {"inputTokens": 9, "outputTokens": 6}},
        ]),
        seen=seen,
    )
    created: list = []

    def _capture(base_url, token, workspace_id, *, title, description, assignee_id, allow_duplicate=False):
        created.append({"title": title, "assignee_id": assignee_id})
        return {"identifier": "MYW-99", "title": title, "status": "open"}

    monkeypatch.setattr(multica_client, "create_issue", _capture)
    monkeypatch.setattr(
        multica_client, "list_agents",
        lambda base_url, token, workspace_id: [
            {"id": "258652f6-44fa-498b-83b7-ec5016552931", "name": "全栈工程师（KiMi）"},
        ],
    )
    monkeypatch.setattr(
        runtime, "_wait_for_confirmation",
        lambda db, tool_run, message: "approved",
    )
    events = "".join(_stream())
    assert "event: tool_confirmation_required" in events
    assert '"serverName": "multica"' in events
    assert '"toolName": "multica_create_task"' in events
    assert "event: tool_result" in events
    assert created[0] == {"title": "修复登录问题", "assignee_id": "258652f6-44fa-498b-83b7-ec5016552931"}
    assert "任务 MYW-99 已创建并指派。" in events

    with TestingSession() as db:
        tool_run = db.query(SuperAssistantToolRun).one()
        assert tool_run.requires_confirmation is True
        assert tool_run.status == "success"


def test_create_task_denied_by_user_records_denial(tmp_path, monkeypatch):
    _prepare(
        tmp_path, monkeypatch,
        message="/multica:create_task 给全栈工程师（KiMi）修复登录问题", configured=True,
        responses=iter([
            {"content": None, "tool_calls": [
                {"id": "call-1", "name": "multica_create_task",
                 "arguments": {"title": "修复登录问题"}},
            ], "usage": {"inputTokens": 5, "outputTokens": 3}},
            {"content": "已取消创建。", "tool_calls": [],
             "usage": {"inputTokens": 7, "outputTokens": 4}},
        ]),
    )
    monkeypatch.setattr(
        multica_client, "create_issue",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("被拒绝后不应执行创建")),
    )
    monkeypatch.setattr(
        runtime, "_wait_for_confirmation",
        lambda db, tool_run, message: "denied",
    )
    events = "".join(_stream())
    assert "event: tool_confirmation_required" in events
    assert "event: tool_result" in events
    assert "已取消创建。" in events


def test_tool_catalog_gated_by_config(tmp_path, monkeypatch):
    seen: list = []
    _prepare(
        tmp_path, monkeypatch,
        message="你好", configured=True,
        responses=iter([
            {"content": "你好！", "tool_calls": [],
             "usage": {"inputTokens": 3, "outputTokens": 2}},
        ]),
        seen=seen,
    )
    "".join(_stream())
    tool_names = {item["name"] for item in seen[0]["tools"]}
    assert {"multica_list_agents", "multica_list_tasks", "multica_create_task"} <= tool_names

    seen_unconfigured: list = []
    _prepare(
        tmp_path, monkeypatch,
        message="你好", configured=False,
        responses=iter([
            {"content": "你好！", "tool_calls": [],
             "usage": {"inputTokens": 3, "outputTokens": 2}},
        ]),
        seen=seen_unconfigured,
        db_name="multica-runtime-unconfigured.db",
    )
    "".join(_stream())
    tool_names = {item["name"] for item in seen_unconfigured[0]["tools"]}
    assert not any(name.startswith("multica_") for name in tool_names)
    # 普通消息（无斜杠前缀）不会被当作命令处理
    assert "[平台指令]" not in json.dumps(seen_unconfigured[0]["messages"], ensure_ascii=False)
