"""HTTP contract for the WeKnora native-Web redirect and SSO entry routes.

The browser only needs the public native Web origin and, on click, a one-time
SSO redirect URL. Models, knowledge bases and MCP endpoints are configured
inside WeKnora's own Web, so these routes must not touch local state, the
database or the network. The administrator credentials used by the one-click
SSO relay stay server side: they never appear in any response.
"""
from __future__ import annotations

import pytest

from app.shared.config import Settings, settings
from app.super_assistant.weknora import sso


PATH = "/api/v2/super-assistant/weknora"
ENTRY_PATH = "/api/v2/super-assistant/weknora/entry"
ADMIN_EMAIL = "admin@weknora.example.com"
ADMIN_PASSWORD = "Relay-Only-WeKnora-2026"


@pytest.fixture
def weknora_web(monkeypatch):
    monkeypatch.setattr(settings, "weknora_web_url", "https://kb.example.com/")
    return "https://kb.example.com"


@pytest.fixture
def weknora_sso(monkeypatch, weknora_web):
    monkeypatch.setattr(settings, "weknora_admin_email", ADMIN_EMAIL)
    monkeypatch.setattr(settings, "weknora_admin_password", ADMIN_PASSWORD)
    return weknora_web


def test_authenticated_user_receives_only_public_web_origin(
    client, auth_headers, weknora_web,
):
    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"web_url": "https://kb.example.com"}
    assert "knowledge_base_name" not in response.text
    assert "integration.json" not in response.text
    assert "mcp" not in response.text.lower()


def test_route_requires_authentication(client, weknora_web):
    assert client.get(PATH).status_code in {401, 403}


def test_route_requires_super_assistant_menu(
    client, db, admin_user, auth_headers, weknora_web,
):
    from app.auth.models import RoleMenuPermission

    admin_user.role = "editor"
    db.add(RoleMenuPermission(role="editor", menu_keys=["overview"]))
    db.commit()

    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_route_no_longer_keeps_local_integration_state():
    assert not hasattr(settings, "weknora_state_path")
    assert not hasattr(settings, "weknora_state_origin")


def test_runtime_settings_hold_only_the_sso_relay_credentials():
    # 一键免登录中继只需要管理员邮箱/密码做服务端登录；密码不进 repr，
    # 其余 WeKnora 密钥（DB/Redis/签名密钥）仍不进平台 settings。
    assert Settings.model_fields["weknora_admin_password"].repr is False
    assert Settings.model_fields["weknora_admin_password"].default == ""
    for forbidden in (
        "weknora_admin_user", "weknora_db_password", "weknora_redis_password",
        "weknora_jwt_secret", "weknora_system_aes_key", "weknora_system_signing_key",
    ):
        assert not hasattr(settings, forbidden)


def test_entry_returns_one_time_redirect_without_leaking_credentials(
    client, auth_headers, admin_user, weknora_sso,
):
    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 200
    assert set(response.json()) == {"redirect_url"}
    redirect_url = response.json()["redirect_url"]
    prefix = "https://kb.example.com/sso/entry?ticket="
    assert redirect_url.startswith(prefix)
    # 票据绑定签发它的平台用户，且能被 consume 侧验签
    claims = sso._decode_ticket(redirect_url[len(prefix):])
    assert claims is not None
    assert claims["uid"] == str(admin_user.id)
    assert ADMIN_PASSWORD not in response.text
    assert ADMIN_EMAIL not in response.text


def test_entry_issues_a_fresh_ticket_per_request(
    client, auth_headers, weknora_sso,
):
    first = client.post(ENTRY_PATH, headers=auth_headers).json()["redirect_url"]
    second = client.post(ENTRY_PATH, headers=auth_headers).json()["redirect_url"]

    assert first != second


def test_entry_requires_authentication(client, weknora_sso):
    assert client.post(ENTRY_PATH).status_code in {401, 403}


def test_entry_requires_super_assistant_menu(
    client, db, admin_user, auth_headers, weknora_sso,
):
    from app.auth.models import RoleMenuPermission

    admin_user.role = "editor"
    db.add(RoleMenuPermission(role="editor", menu_keys=["overview"]))
    db.commit()

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_entry_fails_closed_on_invalid_web_origin(
    client, auth_headers, monkeypatch, weknora_sso,
):
    monkeypatch.setattr(settings, "weknora_web_url", "https://kb.example.com/admin")

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "知识图谱服务尚未就绪"


@pytest.mark.parametrize(
    ("field", "value"),
    [("weknora_admin_password", ""), ("weknora_admin_email", "  ")],
    ids=["no-password", "blank-email"],
)
def test_entry_fails_closed_without_relay_credentials(
    client, auth_headers, monkeypatch, weknora_sso, field, value,
):
    monkeypatch.setattr(settings, field, value)

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "领域知识单点登录未配置"
    assert "ticket" not in response.text


@pytest.mark.parametrize("web_url", [
    "javascript:alert(1)",
    "https://user:secret@kb.example.com",
    "https://kb.example.com/admin",
    "https://kb.example.com/?token=leaked",
    "https://kb.example.com/#fragment",
    "not-a-url",
])
def test_invalid_web_origin_fails_closed_without_reflection(
    client, auth_headers, monkeypatch, web_url,
):
    monkeypatch.setattr(settings, "weknora_web_url", web_url)

    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "知识图谱服务尚未就绪"
    assert web_url not in response.text
    assert "secret" not in response.text
