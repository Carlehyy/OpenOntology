"""multica 外部集成路由：配置读写与连接测试的 HTTP 契约。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.shared.database import Base
from app.super_assistant import multica as multica_router
from app.super_assistant import multica_client
from app.super_assistant.models import SuperAssistantMulticaConfig


def _client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'multica-router.db'}",
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

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(multica_router.router, prefix="/api/v2/super-assistant")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: User(
        id="user-1", username="owner", email="owner@example.com",
        password_hash="unused", role="editor",
    )
    return TestClient(app)


def test_get_config_reports_unconfigured_state(tmp_path):
    response = _client(tmp_path).get("/api/v2/super-assistant/multica/config")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["enabled"] is False
    assert body["token_set"] is False
    assert body["commands"] == []


def test_put_config_roundtrip_and_token_redaction(tmp_path):
    client = _client(tmp_path)
    saved = client.put("/api/v2/super-assistant/multica/config", json={
        "base_url": "http://127.0.0.1:8080",
        "token": "mul-secret",
        "workspace_id": "ws-1",
        "workspace_name": "My Workspace",
        "enabled": True,
    })
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["configured"] is True and body["enabled"] is True
    assert body["workspace_name"] == "My Workspace"
    assert body["token_set"] is True
    assert "token" not in body  # 凭据永不回显
    assert [item["command"] for item in body["commands"]] == [
        "list_agents", "list_tasks", "create_task",
    ]

    # 不带 token 的更新保留已存凭据
    again = client.put("/api/v2/super-assistant/multica/config", json={
        "base_url": "http://127.0.0.1:8080",
        "workspace_id": "ws-2",
        "enabled": True,
    })
    assert again.status_code == 200
    assert again.json()["token_set"] is True
    fetched = client.get("/api/v2/super-assistant/multica/config").json()
    assert fetched["workspace_id"] == "ws-2"
    assert fetched["enabled"] is True and len(fetched["commands"]) == 3


def test_put_config_rejects_bad_url_and_tokenless_enable(tmp_path):
    client = _client(tmp_path)
    bad_url = client.put("/api/v2/super-assistant/multica/config", json={
        "base_url": "ftp://multica.local", "token": "x", "workspace_id": "ws", "enabled": False,
    })
    assert bad_url.status_code == 400

    no_token = client.put("/api/v2/super-assistant/multica/config", json={
        "base_url": "http://127.0.0.1:8080", "workspace_id": "ws", "enabled": True,
    })
    assert no_token.status_code == 400


def test_post_test_uses_draft_or_stored_credentials(tmp_path, monkeypatch):
    client = _client(tmp_path)
    client.put("/api/v2/super-assistant/multica/config", json={
        "base_url": "http://127.0.0.1:8080",
        "token": "mul-secret",
        "workspace_id": "ws-1",
        "enabled": True,
    })
    seen: list = []

    def _fake_me(base_url, token):
        seen.append((base_url, token))
        return {"name": "admin"}

    monkeypatch.setattr(multica_client, "fetch_me", _fake_me)
    monkeypatch.setattr(
        multica_client, "list_workspaces",
        lambda base_url, token: [{"id": "ws-1", "name": "My Workspace", "slug": "my-workspace"}],
    )

    # 草稿缺省 → 回落已保存配置
    result = client.post("/api/v2/super-assistant/multica/test", json={})
    assert result.status_code == 200
    assert result.json()["ok"] is True
    assert result.json()["workspaces"][0]["name"] == "My Workspace"
    assert seen[0] == ("http://127.0.0.1:8080", "mul-secret")

    # 草稿优先于已保存值
    client.post("/api/v2/super-assistant/multica/test", json={
        "base_url": "http://127.0.0.1:9999", "token": "mul-draft",
    })
    assert seen[1] == ("http://127.0.0.1:9999", "mul-draft")


def test_post_test_maps_invalid_url_to_400(tmp_path):
    client = _client(tmp_path)
    response = client.post("/api/v2/super-assistant/multica/test", json={
        "base_url": "not a url",
    })
    assert response.status_code == 400


def test_get_workspaces_lists_saved_config_workspaces(tmp_path, monkeypatch):
    client = _client(tmp_path)
    client.put("/api/v2/super-assistant/multica/config", json={
        "base_url": "http://127.0.0.1:8080",
        "token": "mul-secret",
        "workspace_id": "ws-1",
        "enabled": True,
    })
    monkeypatch.setattr(
        multica_client, "list_workspaces",
        lambda base_url, token: [
            {"id": "ws-1", "name": "My Workspace", "slug": "my"},
            {"id": "ws-2", "name": "E2E 工作区", "slug": "e2e"},
        ],
    )
    result = client.get("/api/v2/super-assistant/multica/workspaces")
    assert result.status_code == 200, result.text
    assert [item["id"] for item in result.json()["workspaces"]] == ["ws-1", "ws-2"]


def test_get_workspaces_maps_unconfigured_to_400_and_upstream_error_to_502(
    tmp_path, monkeypatch,
):
    client = _client(tmp_path)
    # 未保存配置：按请求错误返回 400
    unconfigured = client.get("/api/v2/super-assistant/multica/workspaces")
    assert unconfigured.status_code == 400

    client.put("/api/v2/super-assistant/multica/config", json={
        "base_url": "http://127.0.0.1:8080",
        "token": "mul-secret",
        "workspace_id": "ws-1",
        "enabled": True,
    })

    def _boom(base_url, token):
        raise multica_client.MulticaClientError(
            "multica 请求失败（/api/workspaces）：HTTP 503 upstream unavailable",
        )

    monkeypatch.setattr(multica_client, "list_workspaces", _boom)
    upstream = client.get("/api/v2/super-assistant/multica/workspaces")
    assert upstream.status_code == 502
    assert "503" in upstream.json()["detail"]
