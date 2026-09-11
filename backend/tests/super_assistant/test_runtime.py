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
