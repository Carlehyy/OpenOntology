from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.shared.database import Base
from app.super_assistant import router
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMessage,
)


def _build_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'export.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__,
        SuperAssistantConversation.__table__,
        SuperAssistantMessage.__table__,
    ])
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        ))
        db.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    def override_user():
        return User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        )

    app = FastAPI()
    app.include_router(router.router, prefix="/api/v2/super-assistant")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    return TestClient(app), TestingSession


def test_export_conversation_returns_full_audit_payload(tmp_path):
    client, TestingSession = _build_client(tmp_path)
    conversation = client.post("/api/v2/super-assistant/conversations", json={
        "title": "导出用会话",
    }).json()

    with TestingSession() as db:
        db.add(SuperAssistantMessage(
            conversation_id=conversation["id"],
            role="user",
            content="统计一下订单量",
            status="complete",
            steps=[],
            token_usage={},
        ))
        db.add(SuperAssistantMessage(
            conversation_id=conversation["id"],
            role="assistant",
            content="本月订单量 1200",
            status="complete",
            steps=[{"tool": "query", "arguments": {}, "summary": "查库"}],
            # 键名与 runtime 真实写入一致（camelCase），防统计假绿
            token_usage={"inputTokens": 30, "outputTokens": 12},
        ))
        db.commit()

    response = client.get(
        f"/api/v2/super-assistant/conversations/{conversation['id']}/export"
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["format"] == "openontology.super-assistant.conversation"
    assert payload["version"] == 1
    assert payload["exportedAt"]

    exported = payload["conversation"]
    assert exported["id"] == conversation["id"]
    assert exported["title"] == "导出用会话"
    assert exported["status"] == "active"
    assert exported["messageCount"] == 2
    assert [message["role"] for message in exported["messages"]] == ["user", "assistant"]
    assert exported["messages"][1]["steps"][0]["tool"] == "query"

    assert payload["summary"] == {
        "messageCount": 2,
        "toolStepCount": 1,
        "totalTokens": 42,
    }


def test_export_conversation_empty_conversation_has_zero_counts(tmp_path):
    client, _ = _build_client(tmp_path)
    conversation = client.post("/api/v2/super-assistant/conversations", json={}).json()

    payload = client.get(
        f"/api/v2/super-assistant/conversations/{conversation['id']}/export"
    ).json()

    assert payload["conversation"]["messageCount"] == 0
    assert payload["conversation"]["messages"] == []
    assert payload["summary"] == {
        "messageCount": 0,
        "toolStepCount": 0,
        "totalTokens": 0,
    }


def test_export_conversation_reaps_stale_streaming_before_snapshot(tmp_path):
    """导出前与 list_messages 同口径回收死流：超时 streaming 消息定格为 error+兜底文案。"""
    client, TestingSession = _build_client(tmp_path)
    conversation = client.post("/api/v2/super-assistant/conversations", json={}).json()

    with TestingSession() as db:
        db.add(SuperAssistantMessage(
            conversation_id=conversation["id"],
            role="assistant",
            content="",
            status="streaming",
            steps=[],
            token_usage={},
            created_at=datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=11),
        ))
        db.commit()

    payload = client.get(
        f"/api/v2/super-assistant/conversations/{conversation['id']}/export"
    ).json()
    message = payload["conversation"]["messages"][0]
    assert message["status"] == "error"
    assert message["content"] == "上一次生成意外中断"


def test_export_conversation_rejects_other_users_conversation(tmp_path):
    client, TestingSession = _build_client(tmp_path)
    conversation = client.post("/api/v2/super-assistant/conversations", json={}).json()

    app = client.app
    app.dependency_overrides[get_current_user] = lambda: User(
        id="user-2", username="intruder", email="intruder@example.com",
        password_hash="unused", role="editor",
    )
    response = client.get(
        f"/api/v2/super-assistant/conversations/{conversation['id']}/export"
    )
    assert response.status_code == 404


def test_export_conversation_unknown_id_returns_404(tmp_path):
    client, _ = _build_client(tmp_path)
    assert client.get(
        "/api/v2/super-assistant/conversations/missing-id/export"
    ).status_code == 404
