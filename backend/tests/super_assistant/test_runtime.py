from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import runtime
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
from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.skill_store import build_manifest, create_skill_folder, render_skill_markdown, skill_directory

# 流式改造后 runtime 每轮调 provider.chat_stream；测试统一经此伪造响应
# （content 非空时同步触发 on_delta，模拟真流式增量）。
_RUNTIME_TABLES = [
    User.__table__, ModelConfig.__table__,
    RoleMenuPermission.__table__,  # 委派目录注入的菜单权限查询
    SuperAssistantConversation.__table__, SuperAssistantSkill.__table__,
    SuperAssistantMcpServer.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantToolSetting.__table__, SuperAssistantMemory.__table__,
    SuperAssistantMulticaConfig.__table__,
    SuperAssistantMemoryProfile.__table__, SuperAssistantReflectionRun.__table__,
    SuperAssistantReflectionCandidate.__table__,
    CapabilityRevision.__table__,  # 审批后 MCP 实时复核读授权位
]


def _fake_chat_stream(responses):
    def _fake(_call_kwargs, _messages, _tools, on_delta=None):
        result = next(responses)
        content = result.get("content")
        if content and on_delta:
            on_delta(content)
        return result

    return _fake


def test_runtime_progressively_loads_folder_skill_and_persists_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_skill_root", str(tmp_path / "skills"))
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)

    folder = skill_directory("user-1", "skill-1")
    create_skill_folder(folder, render_skill_markdown(
        name="qa-skill", description="Use this skill for QA work",
        content="Read references when necessary.",
    ))
    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        ))
        model = ModelConfig(
            id="model-1", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-1",
        )
        conversation = SuperAssistantConversation(
            id="conversation-1", owner_id="user-1", title="QA",
            model_config_id="model-1",
        )
        user_message = SuperAssistantMessage(
            id="user-message-1", conversation_id=conversation.id,
            role="user", content="use qa", status="complete",
        )
        assistant_message = SuperAssistantMessage(
            id="assistant-message-1", conversation_id=conversation.id,
            role="assistant", content="", status="streaming",
        )
        skill = SuperAssistantSkill(
            id="skill-1", owner_id="user-1", name="qa-skill",
            display_name="qa-skill", description="Use this skill for QA work", triggers=[],
            folder_path=str(folder), manifest=build_manifest(folder), enabled=True,
        )
        db.add_all([model, conversation, user_message, assistant_message, skill])
        db.commit()

    responses = iter([
        {
            "content": None,
            "tool_calls": [{"id": "call-1", "name": "use_skill", "arguments": {"name": "qa-skill"}}],
            "usage": {"inputTokens": 10, "outputTokens": 2},
        },
        {
            "content": "已按目录 Skill 完成。",
            "tool_calls": [],
            "usage": {"inputTokens": 20, "outputTokens": 8},
        },
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))

    events = "".join(runtime.stream_chat(
        conversation_id="conversation-1",
        owner_id="user-1",
        assistant_message_id="assistant-message-1",
        requested_model_id="model-1",
    ))
    assert "event: tool_start" in events
    assert "event: text_delta" in events
    assert "已按目录 Skill 完成" in events

    with TestingSession() as db:
        saved = db.get(SuperAssistantMessage, "assistant-message-1")
        assert saved.status == "complete"
        assert saved.content == "已按目录 Skill 完成。"
        assert saved.token_usage == {
            "inputTokens": 30,
            "outputTokens": 10,
            "contextTokens": 20,
            "contextLimit": 64_000,
        }
        tool_run = db.query(SuperAssistantToolRun).one()
        assert tool_run.tool_name == "use_skill"
        assert tool_run.status == "success"


def test_runtime_excludes_disabled_skill_and_rejects_direct_loading(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_skill_root", str(tmp_path / "skills"))
    engine = create_engine(
        f"sqlite:///{tmp_path / 'disabled-skill-runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)

    folder = skill_directory("user-disabled", "skill-disabled")
    create_skill_folder(folder, render_skill_markdown(
        name="disabled-skill", description="This description must not enter the prompt",
        content="These instructions must not be available.",
    ))
    with TestingSession() as db:
        db.add(User(
            id="user-disabled", username="disabled-owner", email="disabled@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id="model-disabled", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-disabled",
        ))
        db.add(SuperAssistantConversation(
            id="conversation-disabled", owner_id="user-disabled", title="Disabled skill",
            model_config_id="model-disabled",
        ))
        db.add(SuperAssistantMessage(
            id="user-message-disabled", conversation_id="conversation-disabled",
            role="user", content="do not use disabled skill", status="complete",
        ))
        db.add(SuperAssistantMessage(
            id="assistant-message-disabled", conversation_id="conversation-disabled",
            role="assistant", content="", status="streaming",
        ))
        db.add(SuperAssistantSkill(
            id="skill-disabled", owner_id="user-disabled", name="disabled-skill",
            display_name="disabled-skill", description="This description must not enter the prompt",
            triggers=[], folder_path=str(folder), manifest=build_manifest(folder), enabled=False,
        ))
        db.commit()

    def fake_chat_stream(_call_kwargs, messages, _tools, on_delta=None):
        assert "disabled-skill" not in messages[0]["content"]
        assert "This description must not enter the prompt" not in messages[0]["content"]
        if on_delta:
            on_delta("停用的 Skill 未进入运行时。")
        return {
            "content": "停用的 Skill 未进入运行时。",
            "tool_calls": [],
            "usage": {"inputTokens": 12, "outputTokens": 6},
        }

    monkeypatch.setattr(runtime.provider, "chat_stream", fake_chat_stream)
    events = "".join(runtime.stream_chat(
        conversation_id="conversation-disabled",
        owner_id="user-disabled",
        assistant_message_id="assistant-message-disabled",
        requested_model_id="model-disabled",
    ))
    assert "停用的 Skill 未进入运行时" in events

    with TestingSession() as db:
        result = json.loads(runtime._execute_builtin(
            db, "user-disabled", "use_skill", {"name": "disabled-skill"},
        ))
    assert "不存在或未启用" in result["error"]


def test_runtime_executes_builtin_minio_mcp_without_network_or_credentials(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'minio-runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)
    with TestingSession() as db:
        db.add(User(
            id="user-minio", username="owner", email="minio@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id="model-minio", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-minio",
        ))
        db.add(SuperAssistantConversation(
            id="conversation-minio", owner_id="user-minio", title="MinIO",
            model_config_id="model-minio",
        ))
        db.add_all([
            SuperAssistantMessage(
                id="user-message-minio", conversation_id="conversation-minio",
                role="user", content="上传文件到 MinIO", status="complete",
            ),
            SuperAssistantMessage(
                id="assistant-message-minio", conversation_id="conversation-minio",
                role="assistant", content="", status="streaming",
            ),
            SuperAssistantMcpServer(
                id="server-minio", owner_id="user-minio", name="platform_minio",
                builtin_key="minio", transport="streamable_http", url="builtin://minio",
                header_names=[], args=[], env_names=[], enabled=True,
                require_confirmation=False,
                tool_manifest=[{
                    "name": "minio_upload_text",
                    "description": "上传文本",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "bucket": {"type": "string"},
                            "key": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["bucket", "key", "content"],
                    },
                }],
            ),
        ])
        db.commit()

    calls = []
    monkeypatch.setattr(
        runtime,
        "execute_minio_tool",
        lambda db, name, arguments, **kwargs: calls.append((name, arguments, kwargs)) or
        '{"ok":true,"result":{"uri":"s3://openontology/note.txt"}}',
    )
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "call-minio",
                "name": "mcp__platform_minio__minio_upload_text",
                "arguments": {"bucket": "openontology", "key": "note.txt", "content": "hello"},
            }],
            "usage": {"inputTokens": 15, "outputTokens": 3},
        },
        {
            "content": "文件已上传到 s3://openontology/note.txt。",
            "tool_calls": [],
            "usage": {"inputTokens": 25, "outputTokens": 9},
        },
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))

    events = "".join(runtime.stream_chat(
        conversation_id="conversation-minio",
        owner_id="user-minio",
        assistant_message_id="assistant-message-minio",
        requested_model_id="model-minio",
    ))
    assert "s3://openontology/note.txt" in events
    assert calls == [(
        "minio_upload_text",
        {"bucket": "openontology", "key": "note.txt", "content": "hello"},
        {"actor_type": "super_assistant", "actor_id": "user-minio"},
    )]
    with TestingSession() as db:
        run = db.query(SuperAssistantToolRun).one()
        assert run.server_id == "server-minio"
        assert run.status == "success"
        assert "s3://openontology/note.txt" in run.result


def test_runtime_executes_builtin_api_hub_mcp_without_http_client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api-hub-runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)
    with TestingSession() as db:
        db.add(User(
            id="user-hub", username="hub-owner", email="hub@example.com",
            password_hash="unused", role="admin",
        ))
        db.add(ModelConfig(
            id="model-hub", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-hub",
        ))
        db.add(SuperAssistantConversation(
            id="conversation-hub", owner_id="user-hub", title="API Hub",
            model_config_id="model-hub",
        ))
        db.add_all([
            SuperAssistantMessage(
                id="user-message-hub", conversation_id="conversation-hub",
                role="user", content="列出接口", status="complete",
            ),
            SuperAssistantMessage(
                id="assistant-message-hub", conversation_id="conversation-hub",
                role="assistant", content="", status="streaming",
            ),
            SuperAssistantMcpServer(
                id="server-hub", owner_id="user-hub", name="platform_api_hub",
                builtin_key="api_hub", transport="streamable_http",
                url="builtin://api-hub",
                header_names=[], args=[], env_names=[], enabled=True,
                require_confirmation=False,
                tool_manifest=[{
                    "name": "list_env_vars",
                    "description": "列出环境变量",
                    "input_schema": {"type": "object", "properties": {}},
                }],
            ),
        ])
        db.commit()

    calls = []

    def fake_execute(db, *, user, name, arguments, conversation_id=None, target_url_guard=None):
        calls.append((user.id, name, arguments, conversation_id))
        return json.dumps({
            "notice": "该结果含个人变量明文。不要写入记忆。",
            "count": 1,
            "variables": [{"key": "REGION", "value": "should-not-preview", "placeholder": "{{env:REGION}}"}],
        }, ensure_ascii=False)

    monkeypatch.setattr("app.api_hub.assistant_mcp.execute_tool", fake_execute)
    monkeypatch.setattr(
        runtime,
        "call_tool",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError(f"unexpected HTTP MCP call {kwargs}")),
    )
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "call-hub",
                "name": "mcp__platform_api_hub__list_env_vars",
                "arguments": {},
            }],
            "usage": {"inputTokens": 12, "outputTokens": 2},
        },
        {
            "content": "已列出环境变量。",
            "tool_calls": [],
            "usage": {"inputTokens": 18, "outputTokens": 6},
        },
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))

    events = "".join(runtime.stream_chat(
        conversation_id="conversation-hub",
        owner_id="user-hub",
        assistant_message_id="assistant-message-hub",
        requested_model_id="model-hub",
    ))
    assert "已列出环境变量" in events
    assert "should-not-preview" not in events
    assert calls == [("user-hub", "list_env_vars", {}, "conversation-hub")]
    with TestingSession() as db:
        run = db.query(SuperAssistantToolRun).one()
        assert run.server_id == "server-hub"
        assert run.status == "success"
        assert "should-not-preview" in run.result
        saved = db.get(SuperAssistantMessage, "assistant-message-hub")
        preview = (saved.steps or [])[0]["preview"]
        assert "should-not-preview" not in preview
        assert "***" in preview


def test_looks_like_tool_preamble_detects_spoken_intent():
    assert runtime.looks_like_tool_preamble("我先看看当前浏览器状态，确认一下到底发生了什么。")
    assert runtime.looks_like_tool_preamble("我先看看浏览器当前状态。")
    assert runtime.looks_like_tool_preamble(
        "我刚才的操作其实没有真正控制你的浏览器——只调了工具，但你的浏览器并没有真的打开 B 站往下翻。让我重新来一次，确保这次真的打开浏览器并滚到底部。"
    )
    assert runtime.looks_like_tool_preamble(
        "抱歉，之前的滚动操作确实没在你的浏览器里生效。我现在重新来过：先打开浏览器，再滚到底部。"
    )
    assert not runtime.looks_like_tool_preamble("好的。")
    assert not runtime.looks_like_tool_preamble("# 结论\n\n页面没有滚动。")
    assert not runtime.looks_like_tool_preamble("我先看看" + "啊" * 400)


def test_runtime_continues_when_model_announces_a_tool_but_does_not_call_it(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_skill_root", str(tmp_path / "skills"))
    engine = create_engine(
        f"sqlite:///{tmp_path / 'preamble-runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)

    folder = skill_directory("user-preamble", "skill-preamble")
    create_skill_folder(folder, render_skill_markdown(
        name="qa-skill", description="Use this skill for QA work",
        content="Read references when necessary.",
    ))
    with TestingSession() as db:
        db.add(User(
            id="user-preamble", username="owner", email="preamble@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id="model-preamble", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-preamble",
        ))
        db.add(SuperAssistantConversation(
            id="conversation-preamble", owner_id="user-preamble", title="Preamble",
            model_config_id="model-preamble",
        ))
        db.add_all([
            SuperAssistantMessage(
                id="user-message-preamble", conversation_id="conversation-preamble",
                role="user", content="看一下浏览器", status="complete",
            ),
            SuperAssistantMessage(
                id="assistant-message-preamble", conversation_id="conversation-preamble",
                role="assistant", content="", status="streaming",
            ),
            SuperAssistantSkill(
                id="skill-preamble", owner_id="user-preamble", name="qa-skill",
                display_name="qa-skill", description="Use this skill for QA work",
                triggers=[], folder_path=str(folder), manifest=build_manifest(folder),
                enabled=True,
            ),
        ])
        db.commit()

    seen_prompts: list[list[dict]] = []

    def _fake(_call_kwargs, messages, _tools, on_delta=None):
        seen_prompts.append(messages)
        result = next(responses)
        content = result.get("content")
        if content and on_delta:
            on_delta(content)
        return result

    responses = iter([
        {
            "content": "我先看看当前浏览器状态，确认一下到底发生了什么。",
            "tool_calls": [],
            "usage": {"inputTokens": 10, "outputTokens": 12},
        },
        {
            "content": None,
            "tool_calls": [{"id": "call-1", "name": "use_skill", "arguments": {"name": "qa-skill"}}],
            "usage": {"inputTokens": 12, "outputTokens": 4},
        },
        {
            "content": "页面还停在首页，没有往下翻。",
            "tool_calls": [],
            "usage": {"inputTokens": 20, "outputTokens": 8},
        },
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake)

    events = "".join(runtime.stream_chat(
        conversation_id="conversation-preamble",
        owner_id="user-preamble",
        assistant_message_id="assistant-message-preamble",
        requested_model_id="model-preamble",
        agent_mode=True,
    ))
    assert "event: tool_start" in events
    assert "页面还停在首页" in events
    assert any(
        item.get("content") == runtime._TOOL_PREAMBLE_NUDGE
        for prompt in seen_prompts
        for item in prompt
    )
    with TestingSession() as db:
        saved = db.get(SuperAssistantMessage, "assistant-message-preamble")
        assert saved.status == "complete"
        assert "我先看看当前浏览器状态" in saved.content
        assert "页面还停在首页" in saved.content
        assert db.query(SuperAssistantToolRun).one().status == "success"


def _prepare_ontology_execute_runtime(tmp_path, monkeypatch, responses, db_name):
    """ontology_execute_action 确认卡路径的最小运行时夹具（与 minio 用例同构）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / db_name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)
    with TestingSession() as db:
        db.add(User(
            id="user-onto", username="onto-owner", email="onto@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id="model-onto", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-onto",
        ))
        db.add(SuperAssistantConversation(
            id="conversation-onto", owner_id="user-onto", title="onto",
            model_config_id="model-onto",
        ))
        db.add_all([
            SuperAssistantMessage(
                id="user-message-onto", conversation_id="conversation-onto",
                role="user", content="把订单标记为已支付", status="complete",
            ),
            SuperAssistantMessage(
                id="assistant-message-onto", conversation_id="conversation-onto",
                role="assistant", content="", status="streaming",
            ),
        ])
        db.commit()
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    return TestingSession


def test_ontology_execute_confirmation_card_carries_server_display(tmp_path, monkeypatch):
    """direct 确认卡必须携带服务端持久确认的完整 display，而非只有 ID。"""
    from app.super_assistant import ontology_tools

    confirmation = {
        "confirmationId": "conf-1",
        "state": "prepared",
        "payloadHash": "hash-1",
        "display": {
            "ontology": {"id": "ont-1", "name": "订单本体"},
            "action": {"id": "act-1", "name": "标记已支付"},
            "target": {"id": "inst-1", "label": "SO-001"},
            "parameters": [{"name": "note", "value": "user asked", "source": "request"}],
            "release": {"id": "rel-1", "version": 3},
            "requiresApproval": True,
        },
        "expiresAt": "2099-01-01T00:00:00+00:00",
        "requiresApproval": True,
        "replayed": False,
        "operationId": None,
        "executionLogId": None,
        "approvalLogId": None,
        "failureCode": None,
        "blockReason": None,
        "decidedAt": None,
        "createdAt": "2026-10-19T00:00:00+00:00",
    }
    monkeypatch.setattr(
        ontology_tools, "confirmation_display_for_run",
        lambda db, *, owner_id, conversation_id, tool_run: dict(confirmation),
    )
    # 卡片等待即刻返回 denied，聚焦断言 SSE 载荷本身
    monkeypatch.setattr(
        runtime, "_wait_for_confirmation",
        lambda db, tool_run, assistant_message: "denied",
    )
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "call-onto-1", "name": "ontology_execute_action",
                "arguments": {"confirmationId": "conf-1"},
            }],
            "usage": {"inputTokens": 12, "outputTokens": 4},
        },
        {"content": "用户拒绝了执行。", "tool_calls": [],
         "usage": {"inputTokens": 20, "outputTokens": 8}},
    ])
    session_factory = _prepare_ontology_execute_runtime(
        tmp_path, monkeypatch, responses, "onto-card.db")

    events = "".join(runtime.stream_chat(
        conversation_id="conversation-onto",
        owner_id="user-onto",
        assistant_message_id="assistant-message-onto",
        requested_model_id="model-onto",
    ))
    assert "event: tool_confirmation_required" in events
    card = json.loads(events.split("event: tool_confirmation_required\ndata: ")[1].split("\n\n")[0])
    # 既有字段兼容保留 + 完整服务端确认载荷
    assert card["toolName"] == "ontology_execute_action"
    assert card["serverName"] == "multica"
    assert card["arguments"] == {"confirmationId": "conf-1"}
    assert card["ontologyConfirmation"] == confirmation
    display = card["ontologyConfirmation"]["display"]
    assert display["ontology"]["name"] == "订单本体"
    assert display["release"] == {"id": "rel-1", "version": 3}
    with session_factory() as db:
        run = db.query(SuperAssistantToolRun).one()
        assert run.status == "denied"


def test_ontology_execute_unverifiable_confirmation_never_yields_approvable_card(tmp_path, monkeypatch):
    """确认不可验证（不存在/跨会话/已过期）时不出可批准裸 ID 卡，直接拒绝。"""
    from app.super_assistant import ontology_tools

    monkeypatch.setattr(
        ontology_tools, "confirmation_display_for_run",
        lambda db, *, owner_id, conversation_id, tool_run: None,
    )
    responses = iter([
        {
            "content": None,
            "tool_calls": [{
                "id": "call-onto-2", "name": "ontology_execute_action",
                "arguments": {"confirmationId": "fabricated-or-expired"},
            }],
            "usage": {"inputTokens": 12, "outputTokens": 4},
        },
        {"content": "确认已失效，请重新准备。", "tool_calls": [],
         "usage": {"inputTokens": 20, "outputTokens": 8}},
    ])
    session_factory = _prepare_ontology_execute_runtime(
        tmp_path, monkeypatch, responses, "onto-unverifiable.db")

    events = "".join(runtime.stream_chat(
        conversation_id="conversation-onto",
        owner_id="user-onto",
        assistant_message_id="assistant-message-onto",
        requested_model_id="model-onto",
    ))
    assert "tool_confirmation_required" not in events
    assert "confirmation_not_verifiable" in events
    with session_factory() as db:
        run = db.query(SuperAssistantToolRun).one()
        assert run.status == "denied"
        assert "本体确认不可验证" in run.result


def _prepare_mcp_approval_runtime(tmp_path, monkeypatch, responses, mutate_during_wait, db_name):
    """带受确认 MCP server 的运行时夹具；审批等待窗口内用独立会话改 server。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / db_name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)
    with TestingSession() as db:
        db.add(User(
            id="user-mcp", username="mcp-owner", email="mcp@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id="model-mcp", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-mcp",
        ))
        db.add(SuperAssistantConversation(
            id="conversation-mcp", owner_id="user-mcp", title="mcp",
            model_config_id="model-mcp",
        ))
        db.add(SuperAssistantMcpServer(
            id="server-ext", owner_id="user-mcp", name="ext",
            builtin_key=None, constrained=True,
            transport="streamable_http", url="https://mcp.example.test/sse",
            header_names=[], args=[], env_names=[], enabled=True,
            require_confirmation=True,
            manifest_revision=2,
            tool_manifest=[{
                "name": "lookup", "description": "lookup",
                "input_schema": {"type": "object", "properties": {}},
            }],
        ))
        db.add_all([
            SuperAssistantMessage(
                id="user-message-mcp", conversation_id="conversation-mcp",
                role="user", content="查一下", status="complete",
            ),
            SuperAssistantMessage(
                id="assistant-message-mcp", conversation_id="conversation-mcp",
                role="assistant", content="", status="streaming",
            ),
        ])
        db.commit()

    def _wait(db, tool_run, assistant_message):
        with TestingSession() as other:
            mutate_during_wait(other)
            other.commit()
        return "approved"

    monkeypatch.setattr(runtime, "_wait_for_confirmation", _wait)
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    return TestingSession


_MCP_CALL = {
    "content": None,
    "tool_calls": [{
        "id": "call-mcp-1", "name": "mcp__ext__lookup", "arguments": {},
    }],
    "usage": {"inputTokens": 10, "outputTokens": 2},
}
_MCP_SUMMARY = {
    "content": "已处理。", "tool_calls": [],
    "usage": {"inputTokens": 20, "outputTokens": 6},
}


def test_mcp_approval_recheck_denies_when_server_disabled_during_wait(tmp_path, monkeypatch):
    def disable(db):
        server = db.get(SuperAssistantMcpServer, "server-ext")
        server.enabled = False
    session_factory = _prepare_mcp_approval_runtime(
        tmp_path, monkeypatch,
        iter([_MCP_CALL, _MCP_SUMMARY]), disable, "mcp-disabled.db")
    events = "".join(runtime.stream_chat(
        conversation_id="conversation-mcp", owner_id="user-mcp",
        assistant_message_id="assistant-message-mcp", requested_model_id="model-mcp"))
    assert "MCP server 已停用或删除" in events
    with session_factory() as db:
        assert db.query(SuperAssistantToolRun).one().status == "denied"


def test_mcp_approval_recheck_denies_when_constrained_revoked_during_wait(tmp_path, monkeypatch):
    def revoke(db):
        server = db.get(SuperAssistantMcpServer, "server-ext")
        server.constrained = False
    session_factory = _prepare_mcp_approval_runtime(
        tmp_path, monkeypatch,
        iter([_MCP_CALL, _MCP_SUMMARY]), revoke, "mcp-revoked.db")
    events = "".join(runtime.stream_chat(
        conversation_id="conversation-mcp", owner_id="user-mcp",
        assistant_message_id="assistant-message-mcp", requested_model_id="model-mcp"))
    assert "受约束登记已撤销" in events
    with session_factory() as db:
        assert db.query(SuperAssistantToolRun).one().status == "denied"


def test_mcp_approval_recheck_denies_when_manifest_revision_bumped_during_wait(tmp_path, monkeypatch):
    def bump(db):
        server = db.get(SuperAssistantMcpServer, "server-ext")
        server.manifest_revision = 3
    session_factory = _prepare_mcp_approval_runtime(
        tmp_path, monkeypatch,
        iter([_MCP_CALL, _MCP_SUMMARY]), bump, "mcp-revision.db")
    events = "".join(runtime.stream_chat(
        conversation_id="conversation-mcp", owner_id="user-mcp",
        assistant_message_id="assistant-message-mcp", requested_model_id="model-mcp"))
    assert "mcp_manifest_changed" in events
    with session_factory() as db:
        assert db.query(SuperAssistantToolRun).one().status == "denied"


def test_mcp_approval_recheck_allows_unchanged_server_to_execute(tmp_path, monkeypatch):
    calls = []

    async def fake_call_tool(**kwargs):
        calls.append(kwargs)
        return '{"ok":true}'
    monkeypatch.setattr(runtime, "call_tool", fake_call_tool)
    session_factory = _prepare_mcp_approval_runtime(
        tmp_path, monkeypatch,
        iter([_MCP_CALL, _MCP_SUMMARY]), lambda db: None, "mcp-unchanged.db")
    events = "".join(runtime.stream_chat(
        conversation_id="conversation-mcp", owner_id="user-mcp",
        assistant_message_id="assistant-message-mcp", requested_model_id="model-mcp"))
    assert "mcp_manifest_changed" not in events
    assert "已停用或删除" not in events
    assert calls and calls[0]["url"] == "https://mcp.example.test/sse"
    with session_factory() as db:
        assert db.query(SuperAssistantToolRun).one().status == "success"


def test_mcp_approval_recheck_denies_when_capability_revision_revoked(tmp_path, monkeypatch):
    from app.super_assistant.kernel.models import CapabilityRevision

    def revoke(db):
        db.add(CapabilityRevision(
            key="mcp__ext__lookup", revision=2, enabled=False,
            source="mcp", manifest_hash="hash", trust_level="user_untrusted",
            side_effect_class="unknown"))
    session_factory = _prepare_mcp_approval_runtime(
        tmp_path, monkeypatch,
        iter([_MCP_CALL, _MCP_SUMMARY]), revoke, "mcp-capability.db")
    events = "".join(runtime.stream_chat(
        conversation_id="conversation-mcp", owner_id="user-mcp",
        assistant_message_id="assistant-message-mcp", requested_model_id="model-mcp"))
    assert "capability_revision_revoked" in events
    with session_factory() as db:
        assert db.query(SuperAssistantToolRun).one().status == "denied"


_JOIN_ARGS = {
    "leftOntologyId": "ont-a", "leftObjectTypeId": "ot-order",
    "leftProperty": "order_no", "rightOntologyId": "ont-b",
    "rightObjectTypeId": "ot-shipment", "rightProperty": "ref_no",
    "keyType": "string", "businessMeaning": "订单号与运单参考号一致",
}


def _prepare_join_runtime(tmp_path, monkeypatch, responses, db_name, *, approved):
    engine = create_engine(
        f"sqlite:///{tmp_path / db_name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)
    with TestingSession() as db:
        db.add(User(
            id="user-join", username="join-owner", email="join@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id="model-join", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by="user-join",
        ))
        db.add(SuperAssistantConversation(
            id="conversation-join", owner_id="user-join", title="join",
            model_config_id="model-join",
        ))
        db.add_all([
            SuperAssistantMessage(
                id="user-message-join", conversation_id="conversation-join",
                role="user", content="这两个订单和运单是一个业务键", status="complete",
            ),
            SuperAssistantMessage(
                id="assistant-message-join", conversation_id="conversation-join",
                role="assistant", content="", status="streaming",
            ),
        ])
        db.commit()

    def _wait(db, tool_run, assistant_message):
        if approved:
            tool_run.status = "approved"
            db.commit()
        return "approved" if approved else "denied"

    monkeypatch.setattr(runtime, "_wait_for_confirmation", _wait)
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))
    return TestingSession


def test_join_assertion_card_and_registration_flow(tmp_path, monkeypatch):
    from app.super_assistant import ontology_tools

    registered = {}

    def fake_register(db, user, body):
        registered.update(body)
        return {"id": "assert-1", "evidenceRef": "assertion:assert-1"}
    monkeypatch.setattr(ontology_tools, "register_join_assertion", fake_register)
    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "call-join-1", "name": "ontology_register_join_assertion",
            "arguments": dict(_JOIN_ARGS)}],
         "usage": {"inputTokens": 10, "outputTokens": 2}},
        {"content": "声明已登记。", "tool_calls": [],
         "usage": {"inputTokens": 20, "outputTokens": 6}},
    ])
    session_factory = _prepare_join_runtime(
        tmp_path, monkeypatch, responses, "join-approved.db", approved=True)
    events = "".join(runtime.stream_chat(
        conversation_id="conversation-join", owner_id="user-join",
        assistant_message_id="assistant-message-join", requested_model_id="model-join"))
    # 确认卡携带服务端构造的两端/键型/业务含义展示
    assert "event: tool_confirmation_required" in events
    card = json.loads(events.split("event: tool_confirmation_required\ndata: ")[1].split("\n\n")[0])
    assert card["joinAssertion"] == _JOIN_ARGS
    # 批准后按冻结参数登记（不读可伪造的工具入参）
    assert registered == {
        "left_ontology_id": "ont-a", "left_object_type_id": "ot-order",
        "left_property": "order_no", "right_ontology_id": "ont-b",
        "right_object_type_id": "ot-shipment", "right_property": "ref_no",
        "key_type": "string", "business_meaning": "订单号与运单参考号一致",
    }
    with session_factory() as db:
        assert db.query(SuperAssistantToolRun).one().status == "success"


def test_join_assertion_denied_decision_never_registers(tmp_path, monkeypatch):
    from app.super_assistant import ontology_tools

    def fake_register(db, user, body):
        raise AssertionError("用户拒绝时不得登记")
    monkeypatch.setattr(ontology_tools, "register_join_assertion", fake_register)
    responses = iter([
        {"content": None, "tool_calls": [{
            "id": "call-join-2", "name": "ontology_register_join_assertion",
            "arguments": dict(_JOIN_ARGS)}],
         "usage": {"inputTokens": 10, "outputTokens": 2}},
        {"content": "好的，不登记。", "tool_calls": [],
         "usage": {"inputTokens": 20, "outputTokens": 6}},
    ])
    session_factory = _prepare_join_runtime(
        tmp_path, monkeypatch, responses, "join-denied.db", approved=False)
    events = "".join(runtime.stream_chat(
        conversation_id="conversation-join", owner_id="user-join",
        assistant_message_id="assistant-message-join", requested_model_id="model-join"))
    assert "event: tool_confirmation_required" in events
    with session_factory() as db:
        assert db.query(SuperAssistantToolRun).one().status == "denied"


def test_join_assertion_executor_rejects_unapproved_tool_run(tmp_path, monkeypatch):
    from app.super_assistant import ontology_tools

    monkeypatch.setattr(
        ontology_tools, "register_join_assertion",
        lambda db, user, body: (_ for _ in ()).throw(
            AssertionError("未批准不得登记")))
    engine = create_engine(f"sqlite:///{tmp_path / 'join-direct.db'}")
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        db.add(User(id="user-join", username="join-owner", email="j@example.com",
                    password_hash="x", role="editor"))
        db.add(SuperAssistantConversation(
            id="conversation-join", owner_id="user-join", title="t",
            model_config_id=None))
        db.commit()
        for status, tool_run in (
                ("awaiting_confirmation", None),
                ("awaiting_confirmation", SuperAssistantToolRun(
                    id="r-await", conversation_id="conversation-join",
                    assistant_message_id="m", call_id="c",
                    tool_name="ontology_register_join_assertion",
                    arguments=dict(_JOIN_ARGS),
                    status="awaiting_confirmation", requires_confirmation=True)),
        ):
            result = json.loads(ontology_tools.execute_ontology_tool(
                db, owner_id="user-join", conversation_id="conversation-join",
                name="ontology_register_join_assertion", arguments=dict(_JOIN_ARGS),
                tool_run=tool_run))
            assert result["error"] in (
                "join_assertion_requires_user_approval",
                "join_assertion_not_approved"), status


def test_ontology_execute_result_state_maps_tool_run_status_honestly(tmp_path, monkeypatch):
    """确认协议返回 pending_approval/unknown 等不得把步骤标成成功。"""
    cases = {
        "pending_approval": "pending_approval",
        "unknown": "unknown",
        "compensation_required": "compensation_required",
        "failed_not_executed": "failed_not_executed",
        "executing": "running",
        "succeeded": "success",
    }
    from app.super_assistant import ontology_tools

    for state, expected_status in cases.items():
        confirmation = {"confirmationId": "c", "state": "prepared",
                        "payloadHash": "h", "display": {}}
        monkeypatch.setattr(
            ontology_tools, "confirmation_display_for_run",
            lambda db, *, owner_id, conversation_id, tool_run,
            _c=confirmation: dict(_c))
        monkeypatch.setattr(
            ontology_tools, "confirmation_owner_lookup",
            lambda db, user, cid: "ont-1")

        def _wait(db, tool_run, assistant_message):
            tool_run.status = "approved"
            db.commit()
            return "approved"
        monkeypatch.setattr(runtime, "_wait_for_confirmation", _wait)
        # 执行器返回确认协议真实状态
        monkeypatch.setattr(
            ontology_tools, "execute_confirmation",
            lambda db, user, oid, cid, **kw: {"state": state})
        responses = iter([
            {"content": None, "tool_calls": [{
                "id": "call-st", "name": "ontology_execute_action",
                "arguments": {"confirmationId": "c"}}],
             "usage": {"inputTokens": 1, "outputTokens": 1}},
            {"content": "done", "tool_calls": [],
             "usage": {"inputTokens": 1, "outputTokens": 1}},
        ])
        session_factory = _prepare_ontology_execute_runtime(
            tmp_path, monkeypatch, responses, f"state-{state}.db")
        events = "".join(runtime.stream_chat(
            conversation_id="conversation-onto", owner_id="user-onto",
            assistant_message_id="assistant-message-onto",
            requested_model_id="model-onto"))
        with session_factory() as db:
            run = db.query(SuperAssistantToolRun).one()
            assert run.status == expected_status, (state, run.status)


def test_api_hub_super_assistant_channel_blocks_platform_targets(tmp_path, monkeypatch):
    """超级助手通道内 API Hub 接口指向平台来源/未受约束桥接即拒绝。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'hub-guard.db'}")
    Base.metadata.create_all(bind=engine, tables=_RUNTIME_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        # 平台公开来源派生
        assert runtime._api_hub_target_denied(
            db, "https://platform.example.com/api/v2/ontologies") is False  # 需配置派生
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "super_assistant_public_api_base_url",
                            "https://platform.example.com", raising=False)
        assert runtime._api_hub_target_denied(
            db, "https://platform.example.com/api/v2/ontologies") is True
        assert runtime._api_hub_target_denied(
            db, "http://localhost:8000/api/v2/formal/ontologies") is True
        # 桥接调用必须回到 MCP 本身的授权和确认通道。
        unconstrained = SuperAssistantMcpServer(
            id="srv-ext", owner_id="u1", name="ext", builtin_key=None,
            transport="streamable_http", url="https://127.0.0.1:9/sse",
            header_names=[], args=[], env_names=[], enabled=True,
            require_confirmation=True, tool_manifest=[])
        builtin = SuperAssistantMcpServer(
            id="srv-minio", owner_id="u1", name="platform_minio",
            builtin_key="minio", transport="streamable_http",
            url="builtin://minio", header_names=[], args=[], env_names=[],
            enabled=True, require_confirmation=True, tool_manifest=[])
        db.add_all([unconstrained, builtin])
        db.commit()
        assert runtime._api_hub_target_denied(
            db, "mcp-bridge://srv-ext/minio_upload_text") is True
        assert runtime._api_hub_target_denied(
            db, "mcp-bridge://srv-minio/minio_status") is True
        assert runtime._api_hub_target_denied(
            db, "mcp-bridge://missing/tool") is True
        assert runtime._api_hub_target_denied(
            db, "https://partner.example.com/api") is False
