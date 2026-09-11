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
from sqlalchemy import create_engine
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
                     configured: bool, responses, seen=None):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tools-runtime.db'}",
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
