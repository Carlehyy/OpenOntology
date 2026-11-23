"""multica 多 profile 路由：HTTP 契约（400/404/409/201）与凭据脱敏。

激活端点走真实校验链（fetch_me + list_workspaces + 工作区归属），测试只
在网络层 mock multica_client；平台默认派生设置通过 patch
`_platform_default_settings` 注入。
"""
from __future__ import annotations

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.shared.database import Base
from app.super_assistant import multica as multica_router
from app.super_assistant import multica_client, multica_service
from app.super_assistant.models import (
    SuperAssistantMulticaConfig,
    SuperAssistantMulticaProfile,
    SuperAssistantMulticaProfileRevision,
)

PREFIX = "/api/v2/super-assistant/multica"
DEFAULTS = {
    "base_url": "http://127.0.0.1:9090",
    "web_url": "http://127.0.0.1:3000",
    "workspace_id": "ws-default",
    "workspace_name": "平台工作区",
    "token": "platform-token",
}
VISIBLE_WORKSPACES = ("ws-1", "ws-b", "ws-default", "ws-9")


def _client(tmp_path, monkeypatch, *, user_id="user-1", defaults=True):
    # 激活/测试连接需要真实校验链：仅 mock 网络层，不 patch 服务
    monkeypatch.setattr(
        multica_client, "fetch_me", lambda base_url, token: {"name": "admin"},
    )
    monkeypatch.setattr(
        multica_client, "list_workspaces",
        lambda base_url, token: [
            {"id": identity, "name": identity, "slug": identity}
            for identity in VISIBLE_WORKSPACES
        ],
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / 'multica-profile-router.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantMulticaConfig.__table__,
        SuperAssistantMulticaProfile.__table__,
        SuperAssistantMulticaProfileRevision.__table__,
    ])
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        for uid, username in (("user-1", "owner"), ("user-2", "intruder")):
            if db.get(User, uid) is None:
                db.add(User(
                    id=uid, username=username, email=f"{username}@example.com",
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
        id=user_id, username="acting", email="acting@example.com",
        password_hash="unused", role="editor",
    )
    if defaults:
        monkeypatch.setattr(
            multica_service, "_platform_default_settings", lambda: dict(DEFAULTS),
        )
    return TestClient(app)


def _create_profile(client, **overrides):
    payload = {
        "name": "团队实例",
        "base_url": "http://127.0.0.1:8080",
        "web_url": "http://127.0.0.1:18100",
        "workspace_id": "ws-1",
        "workspace_name": "My Workspace",
        "token": "mul-secret",
        "enabled": True,
    }
    payload.update(overrides)
    return client.post(f"{PREFIX}/profiles", json=payload)


def test_list_profiles_reports_platform_default_and_effective(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get(f"{PREFIX}/profiles")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["default_profile_id"] is not None
    # 无生效 custom 时平台默认自动成为激活绑定（两者 ID 一致）
    assert body["active_profile_id"] == body["default_profile_id"]
    assert [item["kind"] for item in body["profiles"]] == ["platform_default"]
    assert body["profiles"][0]["is_active"] is True
    assert body["profiles"][0]["web_url"] == DEFAULTS["web_url"]
    assert body["effective"]["base_url"] == DEFAULTS["base_url"]
    assert body["effective"]["enabled"] is True
    # 平台默认凭据不回显
    assert "token" not in body["profiles"][0]


def test_create_activate_and_token_redaction_roundtrip(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    created = _create_profile(client)
    assert created.status_code == 201, created.text
    profile = created.json()
    assert profile["kind"] == "custom" and profile["is_active"] is False
    assert profile["token_set"] is True and "token" not in profile
    assert profile["web_url"] == "http://127.0.0.1:18100"

    activated = client.post(f"{PREFIX}/profiles/{profile['id']}/activate")
    assert activated.status_code == 200, activated.text
    assert activated.json()["is_active"] is True
    assert activated.json()["last_test_status"] == "success"

    listing = client.get(f"{PREFIX}/profiles").json()
    assert listing["active_profile_id"] == profile["id"]
    by_kind = {item["kind"]: item for item in listing["profiles"]}
    assert by_kind["custom"]["web_url"] == "http://127.0.0.1:18100"
    assert by_kind["platform_default"]["web_url"] == DEFAULTS["web_url"]
    # 生效切换到自定义；legacy GET 仍只提供 API 配置。
    legacy = client.get(f"{PREFIX}/config").json()
    assert legacy["base_url"] == "http://127.0.0.1:8080" and legacy["enabled"] is True
    assert "web_url" not in legacy and "web_url" not in listing["effective"]


def test_create_rejects_tokenless_enable_and_invalid_url(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, defaults=False)
    no_token = _create_profile(client, token=None)
    assert no_token.status_code == 400
    bad_url = _create_profile(client, base_url="ftp://multica.local")
    assert bad_url.status_code == 400
    bad_web_url = _create_profile(client, web_url="javascript:alert(1)")
    assert bad_web_url.status_code == 400
    assert "工作台地址" in bad_web_url.json()["detail"]
    assert client.get(f"{PREFIX}/profiles").json()["profiles"] == []


def test_profile_not_found_and_platform_conflict_map_to_404_409(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.patch(
        f"{PREFIX}/profiles/missing", json={"name": "x"},
    ).status_code == 404
    assert client.delete(f"{PREFIX}/profiles/missing").status_code == 404

    default_id = client.get(f"{PREFIX}/profiles").json()["default_profile_id"]
    assert client.patch(
        f"{PREFIX}/profiles/{default_id}", json={"name": "改名"},
    ).status_code == 409
    assert client.delete(f"{PREFIX}/profiles/{default_id}").status_code == 409


def test_activate_incomplete_profile_conflicts(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, defaults=False)
    profile = _create_profile(client, token=None, enabled=False).json()
    response = client.post(f"{PREFIX}/profiles/{profile['id']}/activate")
    assert response.status_code == 409


def test_activate_rejects_workspace_out_of_token_scope_keeps_previous_active(
    tmp_path, monkeypatch,
):
    client = _client(tmp_path, monkeypatch, defaults=False)
    keeper = _create_profile(client).json()
    assert client.post(f"{PREFIX}/profiles/{keeper['id']}/activate").status_code == 200
    hidden = _create_profile(client, name="越权工作区", workspace_id="ws-x").json()

    denied = client.post(f"{PREFIX}/profiles/{hidden['id']}/activate")
    assert denied.status_code == 409
    assert "无权访问" in denied.json()["detail"]

    listing = client.get(f"{PREFIX}/profiles").json()
    assert listing["active_profile_id"] == keeper["id"]
    assert client.get(f"{PREFIX}/config").json()["base_url"] == "http://127.0.0.1:8080"


def test_activate_upstream_failure_maps_409_and_keeps_previous_active(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, defaults=False)
    keeper = _create_profile(client).json()
    client.post(f"{PREFIX}/profiles/{keeper['id']}/activate")
    other = _create_profile(client, name="备用", workspace_id="ws-b").json()

    def _unreachable(base_url, token):
        raise multica_client.MulticaClientError("multica 请求未取得可靠响应")

    monkeypatch.setattr(multica_client, "fetch_me", _unreachable)
    failed = client.post(f"{PREFIX}/profiles/{other['id']}/activate")
    assert failed.status_code == 409
    assert "连接验证失败" in failed.json()["detail"]

    listing = client.get(f"{PREFIX}/profiles").json()
    assert listing["active_profile_id"] == keeper["id"]


def test_disable_active_custom_falls_back_to_platform_default(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    profile = _create_profile(client).json()
    client.post(f"{PREFIX}/profiles/{profile['id']}/activate")

    disabled = client.patch(f"{PREFIX}/profiles/{profile['id']}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    body = disabled.json()
    assert body["enabled"] is False and body["is_active"] is False

    listing = client.get(f"{PREFIX}/profiles").json()
    assert listing["active_profile_id"] == listing["default_profile_id"] != profile["id"]
    assert client.get(f"{PREFIX}/config").json()["base_url"] == DEFAULTS["base_url"]


def test_update_keeps_blank_token_and_bumps_revision(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, defaults=False)
    profile = _create_profile(client).json()
    updated = client.patch(
        f"{PREFIX}/profiles/{profile['id']}", json={"workspace_id": "ws-2"},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["revision"] == 2 and body["workspace_id"] == "ws-2"
    assert body["token_set"] is True  # 凭据保留
    assert body["web_url"] == profile["web_url"]  # 缺省不修改

    cleared = client.patch(f"{PREFIX}/profiles/{profile['id']}", json={"web_url": None})
    assert cleared.status_code == 200
    assert cleared.json()["web_url"] is None and cleared.json()["revision"] == 2
    assert client.get(f"{PREFIX}/profiles").json()["profiles"][0]["web_url"] is None


def test_delete_falls_back_to_platform_default(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    profile = _create_profile(client).json()
    assert client.post(f"{PREFIX}/profiles/{profile['id']}/activate").status_code == 200
    deleted = client.delete(f"{PREFIX}/profiles/{profile['id']}")
    assert deleted.status_code == 204
    listing = client.get(f"{PREFIX}/profiles").json()
    assert listing["active_profile_id"] == listing["default_profile_id"]
    assert [item["kind"] for item in listing["profiles"]] == ["platform_default"]


def test_workspace_collision_after_soft_delete_maps_409(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, defaults=False)
    doomed = _create_profile(client, workspace_id="ws-collide").json()
    assert client.delete(f"{PREFIX}/profiles/{doomed['id']}").status_code == 204

    conflict = _create_profile(client, name="换址重绑", workspace_id="ws-collide",
                               base_url="http://127.0.0.1:9999")
    assert conflict.status_code == 409
    assert "已绑定其他" in conflict.json()["detail"]
    # 同地址复用同一工作区不受影响
    reborn = _create_profile(client, name="同址复用", workspace_id="ws-collide")
    assert reborn.status_code == 201


def test_other_owner_gets_404_and_never_sees_foreign_profiles(tmp_path, monkeypatch):
    owner = _client(tmp_path, monkeypatch, defaults=False)
    profile = _create_profile(owner).json()
    assert owner.post(f"{PREFIX}/profiles/{profile['id']}/activate").status_code == 200

    intruder = _client(tmp_path, monkeypatch, user_id="user-2", defaults=False)
    assert intruder.patch(
        f"{PREFIX}/profiles/{profile['id']}", json={"name": "抢注"},
    ).status_code == 404
    assert intruder.post(f"{PREFIX}/profiles/{profile['id']}/activate").status_code == 404
    assert intruder.post(f"{PREFIX}/profiles/{profile['id']}/test", json={}).status_code == 404
    assert intruder.delete(f"{PREFIX}/profiles/{profile['id']}").status_code == 404
    listings = intruder.get(f"{PREFIX}/profiles").json()["profiles"]
    assert all(item["id"] != profile["id"] for item in listings)
    # 属主的绑定不受探测影响
    assert owner.get(f"{PREFIX}/config").json()["base_url"] == "http://127.0.0.1:8080"


def test_profile_test_records_status_on_revision(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    profile = _create_profile(client).json()
    monkeypatch.setattr(
        multica_client, "fetch_me", lambda base_url, token: {"name": "admin"},
    )
    monkeypatch.setattr(
        multica_client, "list_workspaces",
        lambda base_url, token: [{"id": "ws-1", "name": "My Workspace", "slug": "my"}],
    )
    result = client.post(f"{PREFIX}/profiles/{profile['id']}/test", json={})
    assert result.status_code == 200, result.text
    assert result.json()["ok"] is True
    listing = client.get(f"{PREFIX}/profiles").json()
    saved = next(item for item in listing["profiles"] if item["id"] == profile["id"])
    assert saved["last_test_status"] == "success"


def test_profile_workspaces_maps_unconfigured_to_400_and_upstream_to_502(
    tmp_path, monkeypatch,
):
    client = _client(tmp_path, monkeypatch, defaults=False)
    profile = _create_profile(client, token=None, enabled=False).json()
    unconfigured = client.get(f"{PREFIX}/profiles/{profile['id']}/workspaces")
    assert unconfigured.status_code == 400

    complete = _create_profile(client, name="第二套").json()

    def _boom(base_url, token):
        raise multica_client.MulticaClientError(
            "multica 请求失败（/api/workspaces）：HTTP 503 upstream unavailable",
        )

    monkeypatch.setattr(multica_client, "list_workspaces", _boom)
    upstream = client.get(f"{PREFIX}/profiles/{complete['id']}/workspaces")
    assert upstream.status_code == 502


def test_legacy_put_over_platform_default_creates_custom(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    saved = client.put(f"{PREFIX}/config", json={
        "base_url": "http://127.0.0.1:8080",
        "token": "mul-secret",
        "workspace_id": "ws-1",
        "enabled": True,
    })
    assert saved.status_code == 200, saved.text
    listing = client.get(f"{PREFIX}/profiles").json()
    kinds = [item["kind"] for item in listing["profiles"]]
    assert kinds[0] == "platform_default" and "custom" in kinds
    custom = next(item for item in listing["profiles"] if item["kind"] == "custom")
    assert listing["active_profile_id"] == custom["id"]
    assert custom["revision"] == 1


def test_legacy_put_identical_payload_without_token_keeps_revision(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, defaults=False)
    payload = {
        "base_url": "http://127.0.0.1:8080",
        "token": "mul-secret",
        "workspace_id": "ws-1",
        "workspace_name": "My Workspace",
        "enabled": True,
    }
    first = client.put(f"{PREFIX}/config", json=payload)
    assert first.status_code == 200, first.text
    again = client.put(f"{PREFIX}/config", json={
        key: value for key, value in payload.items() if key != "token"
    })
    assert again.status_code == 200, again.text
    listing = client.get(f"{PREFIX}/profiles").json()
    custom = next(item for item in listing["profiles"] if item["kind"] == "custom")
    # 内容与凭据未变化：不追加 revision、凭据保留、仍是激活绑定
    assert custom["revision"] == 1 and custom["token_set"] is True
    assert listing["active_profile_id"] == custom["id"]


def test_default_settings_removal_stops_default_readd_keeps_singleton(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    first = client.get(f"{PREFIX}/profiles").json()
    default_id = first["default_profile_id"]
    assert first["active_profile_id"] == default_id

    blanks = {key: "" for key in DEFAULTS}
    monkeypatch.setattr(multica_service, "_platform_default_settings", lambda: dict(blanks))
    stopped = client.get(f"{PREFIX}/profiles").json()
    assert stopped["profiles"] == [] and stopped["active_profile_id"] is None
    assert stopped["effective"]["enabled"] is False
    assert client.get(f"{PREFIX}/config").json()["enabled"] is False

    monkeypatch.setattr(multica_service, "_platform_default_settings", lambda: dict(DEFAULTS))
    rebound = client.get(f"{PREFIX}/profiles").json()
    assert [item["kind"] for item in rebound["profiles"]] == ["platform_default"]
    assert rebound["default_profile_id"] == default_id
    assert rebound["active_profile_id"] == default_id


def test_unconfigured_config_get_returns_complete_disabled_view(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, defaults=False)
    blanks = {key: "" for key in DEFAULTS}
    monkeypatch.setattr(multica_service, "_platform_default_settings", lambda: dict(blanks))
    body = client.get(f"{PREFIX}/config").json()
    assert body["configured"] is False and body["enabled"] is False
    assert body["base_url"] == "" and body["workspace_id"] == ""
    assert body["workspace_name"] == "" and body["token_set"] is False
    assert body["commands"] == []
