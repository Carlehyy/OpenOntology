from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import (
    conversation_service,
    mcp_server_service,
    router,
    skill_service,
)
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMcpServer,
    SuperAssistantMessage,
    SuperAssistantScheduledRun,
    SuperAssistantScheduledTask,
    SuperAssistantSkill,
    SuperAssistantToolRun,
)
from app.super_assistant.schemas import (
    ApprovalRequest,
    ChatRequest,
    SkillCreate,
    SkillFileContent,
)


def test_chat_cancel_and_tool_decision_preserve_runtime_semantics(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'chat.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            ModelConfig.__table__,
            SuperAssistantConversation.__table__,
            SuperAssistantMcpServer.__table__,
            SuperAssistantMessage.__table__,
            SuperAssistantToolRun.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    with Session() as db:
        user = User(
            id="chat-owner",
            username="chat-owner",
            email="chat-owner@example.com",
            password_hash="unused",
            role="editor",
        )
        conversation = SuperAssistantConversation(
            id="conversation-1",
            owner_id=user.id,
            title="新会话",
        )
        interrupted = SuperAssistantMessage(
            id="interrupted-message",
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="streaming",
            created_at=now - timedelta(minutes=11),
        )
        db.add_all([user, conversation, interrupted])
        db.commit()

        observed = {}

        def fake_stream_chat(**kwargs):
            observed.update(kwargs)
            return iter(["event: done\ndata: {}\n\n"])

        monkeypatch.setattr(router, "stream_chat", fake_stream_chat)
        response = router.chat(
            conversation.id,
            ChatRequest(message="请分析这批订单"),
            db,
            user,
        )

        messages = db.query(SuperAssistantMessage).filter(
            SuperAssistantMessage.conversation_id
            == conversation.id,
        ).order_by(SuperAssistantMessage.created_at.asc()).all()
        current = messages[-1]
        assert interrupted.status == "error"
        assert interrupted.content == "上一次生成意外中断"
        assert [item.role for item in messages[-2:]] == [
            "user",
            "assistant",
        ]
        assert current.status == "streaming"
        assert conversation.title == "请分析这批订单"
        assert observed == {
            "conversation_id": conversation.id,
            "owner_id": user.id,
            "assistant_message_id": current.id,
            "requested_model_id": None,
            "agent_mode": False,
        }
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == (
            "no-cache, no-transform"
        )
        assert response.headers["x-accel-buffering"] == "no"

        with pytest.raises(HTTPException) as active:
            router.chat(
                conversation.id,
                ChatRequest(message="不要并发生成"),
                db,
                user,
            )
        assert (active.value.status_code, active.value.detail) == (
            409,
            "当前会话仍有一条回复正在生成",
        )

        assert router.cancel_chat(
            conversation.id,
            db,
            user,
        ) == {"cancelled": True}
        assert current.status == "cancelled"
        assert current.content == "已停止生成"

        approved = SuperAssistantToolRun(
            id="tool-approved",
            conversation_id=conversation.id,
            assistant_message_id=current.id,
            call_id="call-approved",
            tool_name="mcp__search",
            arguments={},
            status="awaiting_confirmation",
            requires_confirmation=True,
        )
        denied = SuperAssistantToolRun(
            id="tool-denied",
            conversation_id=conversation.id,
            assistant_message_id=current.id,
            call_id="call-denied",
            tool_name="mcp__write",
            arguments={},
            status="awaiting_confirmation",
            requires_confirmation=True,
        )
        db.add_all([approved, denied])
        db.commit()

        assert router.decide_tool_run(
            approved.id,
            ApprovalRequest(decision="approve"),
            db,
            user,
        ) == {"id": approved.id, "status": "approved"}
        assert approved.decision == "approve"
        assert approved.completed_at is None

        assert router.decide_tool_run(
            denied.id,
            ApprovalRequest(decision="deny"),
            db,
            user,
        ) == {"id": denied.id, "status": "denied"}
        assert denied.decision == "deny"
        assert denied.completed_at is not None

        with pytest.raises(HTTPException) as decided:
            router.decide_tool_run(
                denied.id,
                ApprovalRequest(decision="approve"),
                db,
                user,
            )
        assert (decided.value.status_code, decided.value.detail) == (
            409,
            "该工具调用已处理或已过期",
        )

    engine.dispose()


def test_chat_passes_agent_mode_to_stream_chat(tmp_path, monkeypatch):
    """conversation_service.chat 把 body.agent_mode 透传给 stream_chat_fn。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-mode.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            ModelConfig.__table__,
            SuperAssistantConversation.__table__,
            SuperAssistantMessage.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    with Session() as db:
        user = User(
            id="agent-owner",
            username="agent-owner",
            email="agent-owner@example.com",
            password_hash="unused",
            role="editor",
        )
        db.add(user)
        db.add(SuperAssistantConversation(
            id="conversation-agent",
            owner_id=user.id,
            title="新会话",
        ))
        db.commit()

        observed = {}

        def fake_stream_chat(**kwargs):
            observed.update(kwargs)
            return iter(["event: done\ndata: {}\n\n"])

        monkeypatch.setattr(router, "stream_chat", fake_stream_chat)
        router.chat(
            "conversation-agent",
            ChatRequest(message="自主完成这项任务", agent_mode=True),
            db,
            user,
        )
        assert observed["agent_mode"] is True

    engine.dispose()


def test_message_listing_reaps_stale_streaming_rows(tmp_path):
    """list_messages 读取兜底：超时死流行标记中断，活跃 streaming 行不受影响。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'reap.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            SuperAssistantConversation.__table__,
            SuperAssistantMessage.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    with Session() as db:
        user = User(
            id="reap-owner",
            username="reap-owner",
            email="reap-owner@example.com",
            password_hash="unused",
            role="editor",
        )
        conversation = SuperAssistantConversation(
            id="conversation-reap",
            owner_id=user.id,
            title="新会话",
        )
        stale = SuperAssistantMessage(
            id="stale-message",
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="streaming",
            created_at=now - timedelta(minutes=11),
        )
        fresh = SuperAssistantMessage(
            id="fresh-message",
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="streaming",
            created_at=now,
        )
        db.add_all([user, conversation, stale, fresh])
        db.commit()

        rows = conversation_service.list_messages(conversation.id, db, user)

        assert [row.id for row in rows] == ["stale-message", "fresh-message"]
        assert stale.status == "error"
        assert stale.content == "上一次生成意外中断"
        assert fresh.status == "streaming"
        assert fresh.content == ""

    engine.dispose()


def test_recover_interrupted_streams_marks_stale_rows(tmp_path, monkeypatch):
    """启动恢复：所有遗留 streaming 回复跨会话统一标记中断。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'recover.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            SuperAssistantConversation.__table__,
            SuperAssistantMessage.__table__,
            SuperAssistantScheduledTask.__table__,
            SuperAssistantScheduledRun.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        user = User(
            id="recover-owner",
            username="recover-owner",
            email="recover-owner@example.com",
            password_hash="unused",
            role="editor",
        )
        first = SuperAssistantConversation(
            id="conversation-a", owner_id=user.id, title="新会话",
        )
        second = SuperAssistantConversation(
            id="conversation-b", owner_id=user.id, title="新会话",
        )
        stuck_a = SuperAssistantMessage(
            id="stuck-a",
            conversation_id=first.id,
            role="assistant",
            content="",
            status="streaming",
        )
        stuck_b = SuperAssistantMessage(
            id="stuck-b",
            conversation_id=second.id,
            role="assistant",
            content="",
            status="streaming",
        )
        done = SuperAssistantMessage(
            id="done-message",
            conversation_id=first.id,
            role="assistant",
            content="已完成",
            status="complete",
        )
        db.add_all([user, first, second, stuck_a, stuck_b, done])
        db.commit()

    monkeypatch.setattr(conversation_service, "SessionLocal", Session)
    assert conversation_service.recover_interrupted_streams() == {
        "interrupted": 2,
    }

    with Session() as db:
        statuses = {
            row.id: (row.status, row.content)
            for row in db.query(SuperAssistantMessage).all()
        }
    assert statuses["stuck-a"] == ("error", "上一次生成意外中断")
    assert statuses["stuck-b"] == ("error", "上一次生成意外中断")
    assert statuses["done-message"] == ("complete", "已完成")

    engine.dispose()


def test_create_skill_rolls_back_db_before_folder_compensation(
    monkeypatch,
):
    order: list[str] = []

    class FailingDatabase:
        def add(self, _item):
            order.append("db.add")

        def commit(self):
            order.append("db.commit")
            raise IntegrityError(
                "INSERT",
                {},
                RuntimeError("duplicate"),
            )

        def rollback(self):
            order.append("db.rollback")

        def refresh(self, _item):
            raise AssertionError("failed commit must not refresh")

    folder = Path("/tmp/super-assistant-skill-test")
    monkeypatch.setattr(
        skill_service,
        "skill_directory",
        lambda *_args: folder,
    )
    monkeypatch.setattr(
        skill_service,
        "render_skill_markdown",
        lambda **_kwargs: "markdown",
    )
    monkeypatch.setattr(
        skill_service,
        "create_skill_folder",
        lambda *_args: order.append("fs.create"),
    )
    monkeypatch.setattr(
        skill_service,
        "build_manifest",
        lambda *_args: (
            order.append("fs.manifest")
            or [{"path": "SKILL.md"}]
        ),
    )
    monkeypatch.setattr(
        skill_service,
        "delete_skill_folder",
        lambda *_args: order.append("fs.cleanup"),
    )

    with pytest.raises(HTTPException) as duplicate:
        skill_service.create_skill(
            SkillCreate(
                name="atomic-skill",
                description="Atomic test",
                content="Follow the instructions.",
            ),
            FailingDatabase(),
            SimpleNamespace(id="owner-1"),
        )
    assert (duplicate.value.status_code, duplicate.value.detail) == (
        409,
        "同名 Skill 已存在",
    )
    assert order == [
        "fs.create",
        "fs.manifest",
        "db.add",
        "db.commit",
        "db.rollback",
        "fs.cleanup",
    ]


@pytest.mark.asyncio
async def test_import_skill_rolls_back_db_before_folder_compensation(
    monkeypatch,
):
    order: list[str] = []

    class Archive:
        filename = "skill.zip"

        async def read(self, _limit):
            order.append("archive.read")
            return b"zip"

    class FailingDatabase:
        def add(self, _item):
            order.append("db.add")

        def commit(self):
            order.append("db.commit")
            raise IntegrityError(
                "INSERT",
                {},
                RuntimeError("duplicate"),
            )

        def rollback(self):
            order.append("db.rollback")

        def refresh(self, _item):
            raise AssertionError("failed commit must not refresh")

    folder = Path("/tmp/super-assistant-import-test")
    monkeypatch.setattr(
        skill_service,
        "skill_directory",
        lambda *_args: folder,
    )
    monkeypatch.setattr(
        skill_service,
        "import_skill_archive",
        lambda *_args: (
            order.append("fs.import")
            or {
                "name": "imported-skill",
                "description": "Imported",
            }
        ),
    )
    monkeypatch.setattr(
        skill_service,
        "build_manifest",
        lambda *_args: (
            order.append("fs.manifest")
            or [{"path": "SKILL.md"}]
        ),
    )
    monkeypatch.setattr(
        skill_service,
        "delete_skill_folder",
        lambda *_args: order.append("fs.cleanup"),
    )

    with pytest.raises(HTTPException) as duplicate:
        await skill_service.import_skill(
            Archive(),
            FailingDatabase(),
            SimpleNamespace(id="owner-1"),
        )
    assert (duplicate.value.status_code, duplicate.value.detail) == (
        409,
        "同名 Skill 已存在",
    )
    assert order == [
        "archive.read",
        "fs.import",
        "fs.manifest",
        "db.add",
        "db.commit",
        "db.rollback",
        "fs.cleanup",
    ]


def test_new_skill_file_is_removed_before_db_rollback_on_limit(
    monkeypatch,
):
    order: list[str] = []
    item = SimpleNamespace(
        id="skill-1",
        name="atomic-skill",
        display_name="atomic-skill",
        description="Atomic",
        triggers=[],
        manifest=[{"path": "SKILL.md"}],
        revision=1,
    )

    class Database:
        def commit(self):
            raise AssertionError("limit failure must not commit")

        def rollback(self):
            order.append("db.rollback")

    monkeypatch.setattr(
        settings,
        "super_assistant_max_skill_files",
        1,
    )
    monkeypatch.setattr(
        skill_service,
        "skill_directory",
        lambda *_args: Path("/tmp/skill-file-limit"),
    )
    monkeypatch.setattr(
        skill_service,
        "write_text_file",
        lambda *_args: order.append("fs.write"),
    )
    monkeypatch.setattr(
        skill_service,
        "build_manifest",
        lambda *_args: (
            order.append("fs.manifest")
            or [
                {"path": "SKILL.md"},
                {"path": "references/new.md"},
            ]
        ),
    )
    monkeypatch.setattr(
        skill_service,
        "delete_file",
        lambda *_args: order.append("fs.delete-new"),
    )

    with pytest.raises(HTTPException) as limited:
        skill_service.put_skill_file(
            item.id,
            "references/new.md",
            SkillFileContent(content="new"),
            Database(),
            SimpleNamespace(id="owner-1"),
            skill_lookup_fn=lambda *_args: item,
        )
    assert (limited.value.status_code, limited.value.detail) == (
        400,
        "Skill 文件数量超过限制",
    )
    assert order == [
        "fs.write",
        "fs.manifest",
        "fs.delete-new",
        "db.rollback",
    ]


def test_mcp_unavailable_error_remains_http_503():
    mapped = router._mcp_http_error(
        mcp_server_service.McpServerUnavailableError(
            "platform minio unavailable",
        ),
    )
    assert (mapped.status_code, mapped.detail) == (
        503,
        "platform minio unavailable",
    )
