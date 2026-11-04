"""HTTP contract for the WeKnora native-Web redirect route.

The browser only needs the public native Web origin.  Models, knowledge bases
and MCP endpoints are configured inside WeKnora's own Web, so this route must
not touch local state, the database or the network.
"""
from __future__ import annotations

import pytest

from app.shared.config import settings


PATH = "/api/v2/super-assistant/weknora"


@pytest.fixture
def weknora_web(monkeypatch):
    monkeypatch.setattr(settings, "weknora_web_url", "https://kb.example.com/")
    return "https://kb.example.com"


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
