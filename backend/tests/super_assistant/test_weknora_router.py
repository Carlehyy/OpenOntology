"""HTTP contract for the WeKnora frontend bootstrap route.

The browser only needs the public native Web origin and the display name of
the shared knowledge base.  Tokens, state-file paths and internal URLs stay in
the deployment state, and an unverified state must fail closed with 503.
"""
from __future__ import annotations

import json

import pytest

from app.shared.config import settings


PATH = "/api/v2/super-assistant/weknora"
BASE_URL = "http://weknora-app:8080"
MCP_TOKEN = "mcp_synthetic_router_token_not_a_secret"
MANIFEST = [
    {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }
    for name in ("list_knowledge_bases", "read_document", "search_knowledge")
]


def _write_state(path, **overrides):
    state = {
        "version": 1,
        "verified": True,
        "base_url": BASE_URL,
        "mcp_endpoint_id": "endpoint-test",
        "mcp_token": MCP_TOKEN,
        "knowledge_base_id": "kb-test",
        "knowledge_base_name": "共享知识库",
        "tools": MANIFEST,
        **overrides,
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


@pytest.fixture
def weknora_state(tmp_path, monkeypatch):
    path = _write_state(tmp_path / "integration.json")
    monkeypatch.setattr(settings, "weknora_state_path", str(path))
    monkeypatch.setattr(settings, "weknora_base_url", BASE_URL)
    monkeypatch.setattr(settings, "weknora_web_url", "https://kb.example.com/")
    return path


def test_authenticated_user_receives_only_public_integration_fields(
    client, auth_headers, weknora_state,
):
    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "web_url": "https://kb.example.com",
        "knowledge_base_name": "共享知识库",
    }
    assert MCP_TOKEN not in response.text
    assert "integration.json" not in response.text
    assert BASE_URL not in response.text


def test_route_requires_authentication(client, weknora_state):
    assert client.get(PATH).status_code in {401, 403}


def test_route_requires_super_assistant_menu(
    client, db, admin_user, auth_headers, weknora_state,
):
    from app.auth.models import RoleMenuPermission

    admin_user.role = "editor"
    db.add(RoleMenuPermission(role="editor", menu_keys=["overview"]))
    db.commit()

    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_missing_state_fails_closed_with_503(client, auth_headers, tmp_path, monkeypatch):
    missing = tmp_path / "missing-integration.json"
    monkeypatch.setattr(settings, "weknora_state_path", str(missing))
    monkeypatch.setattr(settings, "weknora_base_url", BASE_URL)

    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "知识图谱服务尚未就绪"
    assert str(missing) not in response.text


@pytest.mark.parametrize("verified", [False, None, "missing"])
def test_unverified_state_fails_closed_with_503(
    verified, client, auth_headers, tmp_path, monkeypatch,
):
    path = _write_state(tmp_path / "integration.json")
    state = json.loads(path.read_text(encoding="utf-8"))
    if verified == "missing":
        state.pop("verified")
    else:
        state["verified"] = verified
    path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(settings, "weknora_state_path", str(path))
    monkeypatch.setattr(settings, "weknora_base_url", BASE_URL)

    response = client.get(PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "知识图谱服务尚未就绪"
    assert MCP_TOKEN not in response.text
    assert str(path) not in response.text


def test_mcp_server_list_failure_does_not_expose_weknora_state(
    client, auth_headers, weknora_state, monkeypatch,
):
    from app.super_assistant import router as super_assistant_router
    from app.super_assistant.weknora.service import WeKnoraError

    def fail(*_args, **_kwargs):
        raise WeKnoraError(
            f"token={MCP_TOKEN} state={weknora_state} "
            f"mcp={BASE_URL}/mcp/endpoint-test"
        )

    monkeypatch.setattr(
        super_assistant_router.mcp_server_service,
        "list_mcp_servers",
        fail,
    )

    response = client.get(
        "/api/v2/super-assistant/mcp-servers", headers=auth_headers,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "知识图谱服务尚未就绪"
    assert MCP_TOKEN not in response.text
    assert str(weknora_state) not in response.text
    assert f"{BASE_URL}/mcp/endpoint-test" not in response.text


def test_invalid_state_or_web_origin_fails_closed_without_leaking_state(
    client, auth_headers, tmp_path, monkeypatch,
):
    invalid = _write_state(
        tmp_path / "integration.json",
        mcp_token="not-an-mcp-token-leaked-value",
        db_password="database-secret-must-not-leak",
    )
    monkeypatch.setattr(settings, "weknora_state_path", str(invalid))
    monkeypatch.setattr(settings, "weknora_base_url", BASE_URL)
    monkeypatch.setattr(settings, "weknora_web_url", "https://kb.example.com/")

    invalid_state = client.get(PATH, headers=auth_headers)

    assert invalid_state.status_code == 503
    assert "not-an-mcp-token-leaked-value" not in invalid_state.text
    assert "database-secret-must-not-leak" not in invalid_state.text

    _write_state(invalid)
    monkeypatch.setattr(settings, "weknora_web_url", "javascript:alert(1)")
    invalid_origin = client.get(PATH, headers=auth_headers)

    assert invalid_origin.status_code == 503
    assert "kb.example.com" not in invalid_origin.text
