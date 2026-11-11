"""HTTP contract for the Kaneo native-Web redirect route.

The browser only needs the public native Web origin (nginx HTTPS entry).
Projects and tasks are configured inside Kaneo's own Web, so this route must
not touch local state, the database or the network, and the platform must
not hold Kaneo account credentials.
"""
from __future__ import annotations

import pytest

from app.shared.config import settings


PATH = "/api/v2/super-assistant/kaneo"


@pytest.fixture
def kaneo_web(monkeypatch):
    monkeypatch.setattr(settings, "kaneo_web_url", "https://pm.example.com/")
    return "https://pm.example.com"


def test_authenticated_user_receives_only_public_web_origin(
    client, auth_headers, kaneo_web,
):
    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"web_url": "https://pm.example.com"}
    assert "token" not in response.text
    assert "password" not in response.text.lower()


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


def test_platform_runtime_settings_hold_no_kaneo_credentials():
    for forbidden in (
        "kaneo_admin_password", "kaneo_admin_email", "kaneo_admin_user",
        "kaneo_api_key", "kaneo_token",
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
