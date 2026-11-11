"""HTTP contract for the Kaneo native-Web redirect and SSO entry routes.

The browser needs the public native Web origin (nginx HTTPS entry) and, on
click, a one-time SSO redirect URL. Projects and tasks are configured inside
Kaneo's own Web, so these routes must not touch local state or the database.
The administrator credentials used by the one-click SSO relay stay server
side: they never appear in any response.
"""
from __future__ import annotations

import pytest

from app.shared.config import Settings, settings
from app.super_assistant.kaneo import sso


PATH = "/api/v2/super-assistant/kaneo"
ENTRY_PATH = "/api/v2/super-assistant/kaneo/entry"
ADMIN_EMAIL = "admin@kaneo.example.com"
ADMIN_PASSWORD = "Relay-Only-Kaneo-2026"


@pytest.fixture
def kaneo_web(monkeypatch):
    monkeypatch.setattr(settings, "kaneo_web_url", "https://pm.example.com/")
    return "https://pm.example.com"


@pytest.fixture
def kaneo_sso(monkeypatch, kaneo_web):
    monkeypatch.setattr(settings, "kaneo_admin_email", ADMIN_EMAIL)
    monkeypatch.setattr(settings, "kaneo_admin_password", ADMIN_PASSWORD)
    return kaneo_web


def test_authenticated_user_receives_only_public_web_origin(
    client, auth_headers, kaneo_sso,
):
    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"web_url": "https://pm.example.com"}
    assert "token" not in response.text
    assert "password" not in response.text.lower()
    assert ADMIN_PASSWORD not in response.text


def test_route_requires_authentication(client, kaneo_web):
    assert client.get(PATH).status_code in {401, 403}


def test_route_requires_super_assistant_menu(
    client, db, admin_user, auth_headers, kaneo_web,
):
    from app.auth.models import RoleMenuPermission

    admin_user.role = "editor"
    db.add(RoleMenuPermission(role="editor", menu_keys=["overview"]))
    db.commit()

    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_runtime_settings_hold_only_the_sso_relay_credentials():
    # 一键免登录中继只需要管理员邮箱/密码做服务端 sign-in；密码不进 repr，
    # 其余 Kaneo 密钥（API key/token/DB 密码/签名密钥）仍不进平台 settings。
    assert Settings.model_fields["kaneo_admin_password"].repr is False
    assert Settings.model_fields["kaneo_admin_password"].default == ""
    for forbidden in (
        "kaneo_admin_user", "kaneo_api_key", "kaneo_token",
        "kaneo_db_password", "kaneo_auth_secret",
    ):
        assert not hasattr(settings, forbidden)


@pytest.mark.parametrize("web_url", [
    "javascript:alert(1)",
    "https://user:secret@pm.example.com",
    "https://pm.example.com/board",
    "https://pm.example.com/?token=leaked",
    "https://pm.example.com/#fragment",
    "not-a-url",
])
def test_invalid_web_origin_fails_closed_without_reflection(
    client, auth_headers, monkeypatch, web_url,
):
    monkeypatch.setattr(settings, "kaneo_web_url", web_url)

    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "项目任务服务尚未就绪"
    assert web_url not in response.text
    assert "secret" not in response.text


def test_entry_returns_one_time_redirect_without_leaking_credentials(
    client, auth_headers, admin_user, kaneo_sso,
):
    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 200
    assert set(response.json()) == {"redirect_url"}
    redirect_url = response.json()["redirect_url"]
    prefix = "https://pm.example.com/sso/entry?ticket="
    assert redirect_url.startswith(prefix)
    # 票据绑定签发它的平台用户，且能被 consume 侧验签
    claims = sso._decode_ticket(redirect_url[len(prefix):])
    assert claims is not None
    assert claims["uid"] == str(admin_user.id)
    assert ADMIN_PASSWORD not in response.text
    assert ADMIN_EMAIL not in response.text


def test_entry_issues_a_fresh_ticket_per_request(
    client, auth_headers, kaneo_sso,
):
    first = client.post(ENTRY_PATH, headers=auth_headers).json()["redirect_url"]
    second = client.post(ENTRY_PATH, headers=auth_headers).json()["redirect_url"]

    assert first != second


def test_entry_requires_authentication(client, kaneo_sso):
    assert client.post(ENTRY_PATH).status_code in {401, 403}


def test_entry_requires_super_assistant_menu(
    client, db, admin_user, auth_headers, kaneo_sso,
):
    from app.auth.models import RoleMenuPermission

    admin_user.role = "editor"
    db.add(RoleMenuPermission(role="editor", menu_keys=["overview"]))
    db.commit()

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_entry_fails_closed_on_invalid_web_origin(
    client, auth_headers, monkeypatch, kaneo_sso,
):
    monkeypatch.setattr(settings, "kaneo_web_url", "https://pm.example.com/board")

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "项目任务服务尚未就绪"


@pytest.mark.parametrize(
    ("field", "value"),
    [("kaneo_admin_password", ""), ("kaneo_admin_email", "  ")],
    ids=["no-password", "blank-email"],
)
def test_entry_fails_closed_without_relay_credentials(
    client, auth_headers, monkeypatch, kaneo_sso, field, value,
):
    monkeypatch.setattr(settings, field, value)

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "项目任务单点登录未配置"
    assert "ticket" not in response.text
