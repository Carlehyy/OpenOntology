"""内置工具目录与用户级启停（/api/v2/super-assistant/tools）及运行时过滤。

三层行为分别验证：
- 目录 API：跨模式并集、条件可用性标注（available 与 enabled 是两个维度）、
  分类（read_only / confirmation_required / standard）；
- 启停写入：upsert 禁用名单、未知工具 404、条件不可用工具可预先禁用；
- 运行时生效：stream_chat 目录过滤、模型幻觉调用禁用工具的执行级拒绝、
  /multica: 强制命令命中禁用工具时给确定性引导。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.deps import get_current_user, get_db
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import multica_service, runtime
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMessage,
    SuperAssistantMcpServer,
    SuperAssistantMulticaConfig,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantRemoteAgent,
    SuperAssistantSkill,
    SuperAssistantToolRun,
    SuperAssistantToolSetting,
)
from app.super_assistant.tools import router as tools_router

_CATALOG_TABLES = [
    User.__table__,
    RoleMenuPermission.__table__,  # delegation_tools 的菜单权限查询
    SuperAssistantToolSetting.__table__,
    SuperAssistantMulticaConfig.__table__,
    SuperAssistantRemoteAgent.__table__,
]

_RUNTIME_TABLES = [
    User.__table__, ModelConfig.__table__, RoleMenuPermission.__table__,
    SuperAssistantConversation.__table__, SuperAssistantSkill.__table__,
    SuperAssistantMcpServer.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantToolSetting.__table__,
    SuperAssistantMemory.__table__, SuperAssistantMulticaConfig.__table__,
    SuperAssistantMemoryProfile.__table__, SuperAssistantRemoteAgent.__table__,
    SuperAssistantReflectionRun.__table__, SuperAssistantReflectionCandidate.__table__,
]


def _make_client(tmp_path, *, multica_active: bool = False) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tools-router.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_CATALOG_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(tools_router, prefix="/api/v2/super-assistant")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: User(
        id="user-1", username="owner", email="owner@example.com",
        password_hash="unused", role="editor",
    )
    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        ))
        if multica_active:
            db.add(SuperAssistantMulticaConfig(
                owner_id="user-1",
                base_url="http://127.0.0.1:8080",
                workspace_id="ws-1",
                token_encrypted=multica_service.encrypt("mul-token"),
                enabled=True,
            ))
        db.commit()
    return TestClient(app)


def _tools_by_name(client: TestClient) -> dict[str, dict]:
    resp = client.get("/api/v2/super-assistant/tools")
    assert resp.status_code == 200, resp.text
    return {item["name"]: item for item in resp.json()}


def test_catalog_defaults_to_all_enabled_with_availability_annotations(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_web_fetch_enabled", False)
    monkeypatch.setattr(settings, "super_assistant_web_search_backend", "")
    by_name = _tools_by_name(_make_client(tmp_path))

    # 跨模式并集：恒有内置 + agent 专属 todo + 条件工具（web/multica/委派）
    assert {
        "use_skill", "memory_search", "memory_save", "subagent",
        "todo_write", "web_fetch", "web_search",
        "multica_list_agents", "multica_create_task", "delegate_to_assistant",
    } <= set(by_name)
    # 默认全部启用（缺行=全启用）
    assert all(item["enabled"] for item in by_name.values())
    # 条件不可用与用户启停是两个维度：不可用但 enabled=True
    assert by_name["web_fetch"]["available"] is False
    assert by_name["web_fetch"]["unavailable_reason"]
    assert by_name["web_search"]["available"] is False
    assert by_name["multica_list_agents"]["available"] is False
    # editor 默认有可委派的平台助手（菜单权限开放）→ 委派工具可用
    assert by_name["delegate_to_assistant"]["available"] is True
    assert by_name["todo_write"]["available"] is False
    assert by_name["memory_search"]["available"] is True
    assert by_name["memory_search"]["unavailable_reason"] is None
    # 分类标注
    assert by_name["memory_search"]["category"] == "read_only"
    assert by_name["multica_create_task"]["category"] == "confirmation_required"
    assert by_name["memory_save"]["category"] == "standard"
    # 声明完整性：参数 schema 与描述不缺
    assert by_name["memory_search"]["parameters"]["required"] == ["query"]
    assert by_name["delegate_to_assistant"]["description"]


def test_catalog_reflects_enabled_web_and_multica_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_web_fetch_enabled", True)
    monkeypatch.setattr(settings, "super_assistant_web_search_backend", "tavily")
    by_name = _tools_by_name(_make_client(tmp_path, multica_active=True))
    assert by_name["web_fetch"]["available"] is True
    assert by_name["web_fetch"]["unavailable_reason"] is None
    assert by_name["web_search"]["available"] is True
    assert by_name["multica_create_task"]["available"] is True


def test_patch_toggles_tool_enabled_roundtrip(tmp_path):
    client = _make_client(tmp_path)

    disabled = client.patch(
        "/api/v2/super-assistant/tools/memory_search", json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    by_name = _tools_by_name(client)
    assert by_name["memory_search"]["enabled"] is False
    # 不连坐：其它工具保持启用
    assert by_name["memory_save"]["enabled"] is True

    re_enabled = client.patch(
        "/api/v2/super-assistant/tools/memory_search", json={"enabled": True},
    )
    assert re_enabled.status_code == 200, re_enabled.text
    assert re_enabled.json()["enabled"] is True
    assert _tools_by_name(client)["memory_search"]["enabled"] is True


def test_patch_rejects_unknown_and_mcp_tool_names(tmp_path):
    client = _make_client(tmp_path)
    for name in ("no_such_tool", "mcp__server__tool"):
        resp = client.patch(
            f"/api/v2/super-assistant/tools/{name}", json={"enabled": False},
        )
        assert resp.status_code == 404, name
    # 未写入任何名单
    assert all(item["enabled"] for item in _tools_by_name(client).values())


def test_patch_allows_predisabling_conditionally_unavailable_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_web_search_backend", "")
    client = _make_client(tmp_path)
    # 平台未配置搜索后端：仍可预先禁用，配置生效后即被运行时过滤
    resp = client.patch(
        "/api/v2/super-assistant/tools/web_search", json={"enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    assert resp.json()["available"] is False


def test_catalog_marks_delegation_unavailable_without_assistants(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime.delegation, "delegation_tools", lambda db, owner_id: [],
    )
    by_name = _tools_by_name(_make_client(tmp_path))
    entry = by_name["delegate_to_assistant"]
    assert entry["available"] is False
    assert entry["unavailable_reason"] == "当前没有可委派的助手"


def _prepare_runtime(tmp_path, monkeypatch, *, message: str, disabled_tools: list[str],
                     configured: bool, responses, seen=None, db_name: str = "tools-runtime.db"):
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
            id="conversation-1", owner_id="user-1", title="tools", model_config_id="model-1",
        ))
        db.add(SuperAssistantMessage(
            id="user-message-1", conversation_id="conversation-1",
            role="user", content=message, status="complete",
        ))
        db.add(SuperAssistantMessage(
            id="assistant-message-1", conversation_id="conversation-1",
            role="assistant", content="", status="streaming",
        ))
        if disabled_tools:
            db.add(SuperAssistantToolSetting(
                owner_id="user-1", disabled_tools=disabled_tools,
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

    def _fake(_call_kwargs, _messages, tools, on_delta=None):
        if seen is not None:
            seen.append([tool["name"] for tool in tools])
        result = next(responses)
        content = result.get("content")
        if content and on_delta:
            on_delta(content)
        return result

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake)
    return TestingSession


def _stream():
    return runtime.stream_chat(
        conversation_id="conversation-1",
        owner_id="user-1",
        assistant_message_id="assistant-message-1",
        requested_model_id="model-1",
    )


def test_stream_chat_filters_disabled_builtin_tools(tmp_path, monkeypatch):
    seen: list[list[str]] = []
    responses = iter([
        {"content": "好的。", "tool_calls": [], "usage": {"inputTokens": 5, "outputTokens": 5}},
    ])
    _prepare_runtime(
        tmp_path, monkeypatch,
        message="查一下记忆", disabled_tools=["memory_search"],
        configured=False, responses=responses, seen=seen,
    )
    events = "".join(_stream())
    assert "event: message_end" in events
    catalog_names = seen[0]
    assert "memory_search" not in catalog_names
    # 其它内置工具不受影响
    assert "memory_save" in catalog_names
    assert "use_skill" in catalog_names


def test_stream_chat_denies_hallucinated_call_to_disabled_tool(tmp_path, monkeypatch):
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "call-1", "name": "memory_search",
                "arguments": {"query": "anything"},
            }],
            "usage": {"inputTokens": 10, "outputTokens": 2},
        },
        {"content": "无法查询。", "tool_calls": [], "usage": {"inputTokens": 20, "outputTokens": 8}},
    ])
    session_factory = _prepare_runtime(
        tmp_path, monkeypatch,
        message="查一下记忆", disabled_tools=["memory_search"],
        configured=False, responses=responses,
    )
    events = "".join(_stream())
    # 模型幻觉调用已禁用工具：执行级拒绝并如实回灌
    assert "工具已被用户禁用" in events
    with session_factory() as db:
        run = db.query(SuperAssistantToolRun).one()
        assert run.tool_name == "memory_search"
        assert run.status == "denied"


def test_multica_slash_to_disabled_tool_returns_guidance_without_llm(tmp_path, monkeypatch):
    seen: list[list[str]] = []
    responses = iter([])  # 引导路径不经 LLM：被调用即失败
    _prepare_runtime(
        tmp_path, monkeypatch,
        message="/multica:agents", disabled_tools=["multica_list_agents"],
        configured=True, responses=responses, seen=seen,
    )
    events = "".join(_stream())
    assert "已被禁用" in events
    assert "「工具」标签页" in events
    assert not seen


def test_subagent_catalog_and_execution_respect_disabled_tools(tmp_path, monkeypatch):
    """P1 回归：禁用名单必须传导进子代理，不得成为间接调用旁路。"""
    from app.super_assistant import subagent

    tools = subagent._subagent_tools({"web_fetch", "think"})
    names = {schema["name"] for schema in tools}
    assert "web_fetch" not in names and "think" not in names
    assert "use_skill" in names

    engine = create_engine(
        f"sqlite:///{tmp_path / 'subagent.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantSkill.__table__,
    ])
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        ))
        db.commit()

    captured: list[list[str]] = []
    captured_messages: list[list[dict]] = []

    def fake_chat(_call_kwargs, messages, tools, **_kwargs):
        captured.append([schema["name"] for schema in tools])
        captured_messages.append([dict(item) for item in messages])
        # 首轮幻觉调用已禁用工具：执行级兜底应拒绝并让子代理收敛
        if len(captured) == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "c1", "name": "web_fetch", "arguments": {"url": "http://example.com"}}],
            }
        return {"content": "无法联网。"}

    monkeypatch.setattr(subagent.provider, "chat", fake_chat)
    with TestingSession() as db:
        output = subagent.run_subagent(
            db, "user-1", {}, "查证 X", disabled_tools={"web_fetch"},
        )
    assert "web_fetch" not in captured[0]
    # denied 结果回灌给子代理模型（而非出现在最终结论文本）
    tool_results = [
        item.get("content") for item in captured_messages[1]
        if item.get("role") == "tool"
    ]
    assert any("工具已被用户禁用" in str(content) for content in tool_results)
    assert "无法联网" in output


def test_approval_window_disable_blocks_execution(tmp_path, monkeypatch):
    """P2 回归：审批等待期间禁用的内置工具，批准后不再执行。"""
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "call-1", "name": "multica_create_task",
                "arguments": {"title": "t"},
            }],
            "usage": {"inputTokens": 10, "outputTokens": 2},
        },
        {"content": "已处理。", "tool_calls": [], "usage": {"inputTokens": 20, "outputTokens": 8}},
    ])
    session_factory = _prepare_runtime(
        tmp_path, monkeypatch,
        message="给张三建个任务", disabled_tools=[],
        configured=True, responses=responses,
    )

    def fake_wait(db, tool_run, assistant_message):
        # 模拟审批等待窗口内用户在配置面板禁用了该工具，随后批准
        db.add(SuperAssistantToolSetting(
            owner_id="user-1", disabled_tools=["multica_create_task"],
        ))
        tool_run.status = "approved"
        db.commit()
        return "approved"

    monkeypatch.setattr(runtime, "_wait_for_confirmation", fake_wait)
    events = "".join(_stream())
    assert "工具已被用户禁用" in events
    with session_factory() as db:
        run = db.query(SuperAssistantToolRun).one()
        assert run.tool_name == "multica_create_task"
        assert run.status == "denied"


def test_dirty_disabled_tools_json_does_not_break_catalog_or_chat(tmp_path, monkeypatch):
    """P3 回归：库外写入的脏 JSON 只被静默收敛，不打挂目录与聊天。"""
    client = _make_client(tmp_path)
    # 直接改库写入两种脏形态（绕过应用层写入路径）
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tools-router.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO super_assistant_tool_settings (owner_id, disabled_tools, updated_at)"
            " VALUES ('user-1', '[{\"k\": 1}, 42]', CURRENT_TIMESTAMP)"
        ))
    engine.dispose()

    by_name = _tools_by_name(client)
    assert by_name["memory_search"]["enabled"] is True  # 非 str 元素被剔除
    patched = client.patch(
        "/api/v2/super-assistant/tools/memory_search", json={"enabled": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["enabled"] is False
    # 写回后名单恢复正常形态，再启用不受脏数据影响
    assert client.patch(
        "/api/v2/super-assistant/tools/memory_search", json={"enabled": True},
    ).status_code == 200


def test_set_tool_enabled_retries_on_first_write_conflict(tmp_path):
    """P2 回归：并发首写撞 owner 主键时回滚重放，不丢开关也不抛 500。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'conflict.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, RoleMenuPermission.__table__,
        SuperAssistantToolSetting.__table__, SuperAssistantMulticaConfig.__table__,
        SuperAssistantRemoteAgent.__table__,
    ])
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        ))
        db.commit()

    with TestingSession() as db:
        # 模拟并发首写：session 里挂一条未提交的同主键行，函数内查询
        # （autoflush=False）看不到它，insert 提交时撞唯一键 → 走重试
        db.add(SuperAssistantToolSetting(owner_id="user-1", disabled_tools=[]))
        entry = runtime.set_builtin_tool_enabled(db, "user-1", "web_search", False)
        assert entry["enabled"] is False
        db.commit()

    with TestingSession() as db:
        rows = db.query(SuperAssistantToolSetting).all()
        assert len(rows) == 1
        assert "web_search" in rows[0].disabled_tools


def test_disabled_delegation_tool_removes_prompt_section(tmp_path, monkeypatch):
    """P3 回归：禁用委派工具后系统提示不再注入委派规则（与目录同源）。"""
    seen_prompts: list[str] = []

    def make_responses():
        return iter([
            {"content": "好的。", "tool_calls": [], "usage": {"inputTokens": 5, "outputTokens": 5}},
        ])

    for disabled, expect_rule in (["delegate_to_assistant"], False), ([], True):
        seen_prompts.clear()
        # 先搭运行时（其内部的 chat_stream patch 会被下面的捕获版覆盖）
        _prepare_runtime(
            tmp_path, monkeypatch,
            message="帮我委派任务", disabled_tools=disabled,
            configured=False, responses=make_responses(),
            db_name=f"delegation-prompt-{len(disabled)}.db",
        )

        def _capture(_call_kwargs, messages, _tools, on_delta=None):
            seen_prompts.append(str(messages[0]["content"]))
            result = next(make_responses())
            if result.get("content") and on_delta:
                on_delta(result["content"])
            return result

        monkeypatch.setattr(runtime.provider, "chat_stream", _capture)
        "".join(_stream())
        rule_in_prompt = runtime.delegation.SYSTEM_PROMPT_RULE in seen_prompts[0]
        assert rule_in_prompt is expect_rule, (
            f"disabled={disabled}: 委派规则注入与预期不符（{rule_in_prompt}）"
        )
