"""Administrator MCP directory contract for the pinned WeKnora service.

The directory is the only surface allowed to create the bounded OpenOntology
knowledge base and its read-only endpoint upstream. Tokens are minted by
WeKnora and must never travel back to the browser: every response body is
asserted token-free while the platform row keeps ciphertext only. The fake
admin API below mirrors the native HTTP contract (``success``/``data``
envelopes, login-issued bearer) through ``httpx.MockTransport``.
"""
from __future__ import annotations

import itertools
import json
import re
import secrets

import httpx
import pytest

from app.shared.config import settings
from app.super_assistant import mcp_client, mcp_server_service
from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.models import SuperAssistantMcpServer, SuperAssistantWeKnoraIntegration
from app.super_assistant.weknora import connections, directory
from app.super_assistant.weknora.client import WeKnoraClient


BASE = "/api/v2/super-assistant/weknora"
LIST_ENDPOINTS = f"{BASE}/mcp-endpoints"
INITIALIZE = f"{BASE}/mcp-initialize"

FAKE_ORIGIN = "http://127.0.0.1:8787"
ADMIN_EMAIL = "admin@weknora.example.com"
ADMIN_PASSWORD = "Native-Relay-2026"

# 平台承诺托管的最小只读工具集（多一不放行、缺范围校验工具不托管）
READ_TOOLS = [
    "list_knowledge_bases", "search_knowledge", "grep_chunks", "list_documents",
    "read_document", "wiki_search", "wiki_read_page", "wiki_index",
]
DEFAULT_MODELS = [
    {"id": "model-embedding-1", "type": "Embedding", "status": "active",
     "default_for": ["Embedding"]},
    {"id": "model-qa-1", "type": "KnowledgeQA", "status": "active", "is_default": True},
]


def read_manifest(names=None) -> list[dict]:
    return [
        {"name": name, "description": f"{name} 只读知识工具",
         "input_schema": {"type": "object"}}
        for name in (READ_TOOLS if names is None else names)
    ]


class FakeWeKnoraAPI:
    """In-memory native admin API served over ``httpx.MockTransport``.

    Login is the only place where the platform presents relay credentials;
    the MCP token is visible solely in create/rotate responses, mirroring the
    real service so secrecy assertions exercise the true wire format.
    """

    def __init__(self, *, models=None):
        self.models = list(models) if models is not None else [dict(m) for m in DEFAULT_MODELS]
        self.knowledge_bases: list[dict] = []
        self.endpoints: list[dict] = []
        self.tokens: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.created_knowledge_bases: list[dict] = []
        self.created_endpoints: list[dict] = []
        self.rotate_requests = 0
        self.reject_login = False
        self._sequence = itertools.count(1)

    # ---- fixture-side state shaping -------------------------------------
    def add_knowledge_base(self, identifier, name, *, type="document",
                           embedding_model_id="model-embedding-1",
                           summary_model_id="model-qa-1") -> dict:
        item = {"id": identifier, "name": name, "type": type,
                "embedding_model_id": embedding_model_id,
                "summary_model_id": summary_model_id}
        self.knowledge_bases.append(item)
        return item

    def add_endpoint(self, identifier, name, knowledge_base_ids, *,
                     tools=None, enabled=True) -> dict:
        token = "mcp_" + secrets.token_hex(16)
        item = {"id": identifier, "name": name, "enabled": enabled,
                "knowledge_base_ids": list(knowledge_base_ids),
                "tools": list(tools if tools is not None else READ_TOOLS),
                "default_agent_id": "",
                "token_hint": f"mcp_…{token[-4:]}"}
        self.endpoints.append(item)
        self.tokens[identifier] = token
        return item

    def simulate_native_rotation(self, identifier: str) -> None:
        token = "mcp_" + secrets.token_hex(16)
        self.tokens[identifier] = token
        for endpoint in self.endpoints:
            if endpoint["id"] == identifier:
                endpoint["token_hint"] = f"mcp_…{token[-4:]}"

    def post_paths(self) -> list[str]:
        return [path for method, path in self.requests if method == "POST"]

    # ---- transport ------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content.decode()) if request.content else {}
        self.requests.append((method, path))
        if path == "/api/v1/auth/login":
            if (self.reject_login or body.get("email") != ADMIN_EMAIL
                    or body.get("password") != ADMIN_PASSWORD):
                return httpx.Response(401, json={"success": False})
            return httpx.Response(200, json={
                "success": True, "token": "admin-session-token",
                "user": {"is_system_admin": True}})
        if request.headers.get("Authorization") != "Bearer admin-session-token":
            return httpx.Response(401, json={"success": False})
        if method == "GET" and path == "/api/v1/models":
            return httpx.Response(200, json={"success": True, "data": [dict(m) for m in self.models]})
        if method == "GET" and path == "/api/v1/knowledge-bases":
            return httpx.Response(200, json={"success": True, "data": [dict(k) for k in self.knowledge_bases]})
        if method == "POST" and path == "/api/v1/knowledge-bases":
            view = {"id": f"kb-{next(self._sequence):04d}", "name": body.get("name"),
                    "type": body.get("type"),
                    "embedding_model_id": body.get("embedding_model_id"),
                    "summary_model_id": body.get("summary_model_id")}
            self.knowledge_bases.append(view)
            self.created_knowledge_bases.append(dict(body))
            return httpx.Response(200, json={"success": True, "data": dict(view)})
        if method == "GET" and path == "/api/v1/mcp-endpoints":
            return httpx.Response(200, json={"success": True, "data": [dict(e) for e in self.endpoints]})
        if method == "POST" and path == "/api/v1/mcp-endpoints":
            identifier = f"ep-{next(self._sequence):04d}"
            token = "mcp_" + secrets.token_hex(16)
            view = {"id": identifier, "name": body.get("name"),
                    "enabled": body.get("enabled", True),
                    "knowledge_base_ids": list(body.get("knowledge_base_ids") or []),
                    "tools": list(body.get("tools") or []),
                    "default_agent_id": body.get("default_agent_id", ""),
                    "token_hint": f"mcp_…{token[-4:]}"}
            self.endpoints.append(view)
            self.tokens[identifier] = token
            self.created_endpoints.append(dict(body))
            data = dict(view)
            data["token"] = token
            return httpx.Response(200, json={"success": True, "data": data})
        match = re.fullmatch(r"/api/v1/mcp-endpoints/([^/]+)/rotate-token", path)
        if method == "POST" and match:
            identifier = match.group(1)
            token = "mcp_" + secrets.token_hex(16)
            self.tokens[identifier] = token
            for endpoint in self.endpoints:
                if endpoint["id"] == identifier:
                    endpoint["token_hint"] = f"mcp_…{token[-4:]}"
            self.rotate_requests += 1
            return httpx.Response(200, json={"success": True, "data": {"token": token}})
        match = re.fullmatch(r"/api/v1/mcp-endpoints/([^/]+)", path)
        if method == "GET" and match:
            for endpoint in self.endpoints:
                if endpoint["id"] == match.group(1):
                    return httpx.Response(200, json={"success": True, "data": dict(endpoint)})
        return httpx.Response(404, json={"success": False})


class DiscoveryScript:
    """Stand-in for live MCP discovery; tests retune ``manifest``/``error``."""

    def __init__(self):
        self.manifest = read_manifest()
        self.error: str | None = None

    async def __call__(self, **connection):
        if self.error is not None:
            raise mcp_client.McpClientError(self.error)
        return json.loads(json.dumps(self.manifest))


def install_fake_weknora(monkeypatch, api: FakeWeKnoraAPI) -> FakeWeKnoraAPI:
    monkeypatch.setattr(settings, "weknora_base_url", FAKE_ORIGIN)
    monkeypatch.setattr(settings, "weknora_admin_email", ADMIN_EMAIL)
    monkeypatch.setattr(settings, "weknora_admin_password", ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "weknora_timeout_seconds", 5)
    monkeypatch.setattr(settings, "super_assistant_mcp_internal_base_urls", "")
    transport = httpx.MockTransport(api.handler)

    def factory(**kwargs):
        return WeKnoraClient(transport=transport, **kwargs)

    # 目录/连接模块各自 from-import 了 WeKnoraClient，需逐模块替换
    monkeypatch.setattr(directory, "WeKnoraClient", factory)
    monkeypatch.setattr(connections, "WeKnoraClient", factory)
    return api


@pytest.fixture
def api(monkeypatch):
    return install_fake_weknora(monkeypatch, FakeWeKnoraAPI())


@pytest.fixture
def discovery(monkeypatch):
    script = DiscoveryScript()
    monkeypatch.setattr(mcp_client, "discover_tools", script)
    return script


@pytest.fixture
def editor_headers(client, db, editor_user):
    from app.auth.models import RoleMenuPermission

    db.add(RoleMenuPermission(role="editor", menu_keys=["super_assistant"]))
    db.commit()
    response = client.post(
        "/api/v1/auth/login", json={"username": "editor", "password": "editor123"})
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def capability_rows(db, server) -> list:
    return db.query(CapabilityRevision).filter(
        CapabilityRevision.key.like(f"mcp__{server.name}__%"),
    ).all()


def the_server(db) -> SuperAssistantMcpServer:
    return db.query(SuperAssistantMcpServer).one()


def connect_ready(client, api, auth_headers, *, identifier="ep-team") -> dict:
    api.add_knowledge_base("kb-team", "Team KB")
    endpoint = api.add_endpoint(identifier, "Team Endpoint", ["kb-team"])
    response = client.post(
        f"{LIST_ENDPOINTS}/{identifier}/connect", headers=auth_headers,
        json={"token": api.tokens[identifier]})
    assert response.status_code == 200
    return endpoint


# ───────────────────────── 管理员目录与鉴权 ─────────────────────────


def test_directory_routes_are_admin_only(client, api, discovery, editor_headers):
    identifier = api.add_endpoint("ep-foreign", "Foreign", [])["id"]
    for method, path in (
        ("get", LIST_ENDPOINTS),
        ("post", INITIALIZE),
        ("post", f"{LIST_ENDPOINTS}/{identifier}/connect"),
        ("post", f"{LIST_ENDPOINTS}/{identifier}/verify"),
    ):
        kwargs = {} if method == "get" else {"json": {}}
        response = getattr(client, method)(path, headers=editor_headers, **kwargs)
        assert response.status_code == 403, (method, path)
    assert client.get(LIST_ENDPOINTS).status_code in {401, 403}


@pytest.mark.parametrize(("field", "value", "code"), [
    ("reject_login", True, "WEKNORA_LOGIN_REJECTED"),
    ("weknora_admin_password", "", "WEKNORA_CREDENTIALS_MISSING"),
], ids=["native-login-rejected", "relay-credentials-missing"])
def test_directory_fails_closed_on_admin_auth_break(
    client, api, discovery, auth_headers, monkeypatch, field, value, code,
):
    if field == "reject_login":
        api.reject_login = True
    else:
        monkeypatch.setattr(settings, field, value)

    response = client.get(LIST_ENDPOINTS, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == code


# ───────────────── 初始化语义：GET 只读，POST 才确认闩 ─────────────────


def test_get_is_read_only_while_post_confirms_existing_directory(
    client, api, discovery, auth_headers, db,
):
    api.add_endpoint("ep-native", "Native", [])

    listing = client.get(LIST_ENDPOINTS, headers=auth_headers)

    assert listing.status_code == 200
    assert listing.json()["endpoints"][0]["id"] == "ep-native"
    assert listing.json()["initialized"] is False
    # GET 不落集成行、不发起任何上游写
    assert db.query(SuperAssistantWeKnoraIntegration).count() == 0
    assert set(api.post_paths()) <= {"/api/v1/auth/login"}

    confirmed = client.post(INITIALIZE, headers=auth_headers, json={})

    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["initialized"] is True
    assert [(e["id"], e["connection_status"]) for e in body["endpoints"]] == [
        ("ep-native", "not_connected")]
    state = db.query(SuperAssistantWeKnoraIntegration).one()
    assert state.initialized_at is not None
    # 已有目录只确认闩：不重建 endpoint，不隐式轮换，不建镜像行
    assert set(api.post_paths()) <= {"/api/v1/auth/login"}
    assert db.query(SuperAssistantMcpServer).count() == 0
    # A native endpoint resolves an earlier default-creation prerequisite error.
    state.last_error = "请先配置 Embedding 模型"
    db.commit()
    recovered = client.post(INITIALIZE, headers=auth_headers, json={})
    assert recovered.status_code == 200
    assert recovered.json()["message"] is None
    db.refresh(state)
    assert state.last_error is None


# ───────── 默认初始化：OpenOntology KB + 8 只读工具 endpoint ─────────


def test_initialize_creates_readonly_endpoint_and_keeps_token_server_side(
    client, api, discovery, auth_headers, db,
):
    response = client.post(INITIALIZE, headers=auth_headers, json={})

    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is True

    assert len(api.created_knowledge_bases) == 1
    kb_body = api.created_knowledge_bases[0]
    assert kb_body["name"] == "OpenOntology"
    assert kb_body["type"] == "document"
    assert kb_body["embedding_model_id"] == "model-embedding-1"
    assert kb_body["summary_model_id"] == "model-qa-1"
    assert kb_body["indexing_strategy"]["vector_enabled"] is True
    assert kb_body["indexing_strategy"]["wiki_enabled"] is False

    assert len(api.created_endpoints) == 1
    endpoint_body = api.created_endpoints[0]
    assert len(endpoint_body["tools"]) == 8
    assert set(endpoint_body["tools"]) == set(READ_TOOLS)
    assert endpoint_body["enabled"] is True
    assert endpoint_body["knowledge_base_ids"] == [api.knowledge_bases[0]["id"]]

    identifier = api.endpoints[0]["id"]
    token = api.tokens[identifier]
    assert token.startswith("mcp_")

    row = body["endpoints"][0]
    assert row["id"] == identifier
    assert row["connection_status"] == "ready"
    assert row["readonly"] is True
    assert row["server_id"] is not None
    assert row["knowledge_bases"] == [
        {"id": api.knowledge_bases[0]["id"], "name": "OpenOntology"}]

    server = the_server(db)
    assert server.weknora_endpoint_id == identifier
    assert server.transport == "streamable_http"
    assert server.url == f"{FAKE_ORIGIN}/mcp/{identifier}"
    assert server.constrained is True
    assert server.last_test_status == "success"
    assert server.header_names == ["Authorization"]

    state = db.query(SuperAssistantWeKnoraIntegration).one()
    assert state.default_knowledge_base_id == api.knowledge_bases[0]["id"]
    assert state.default_endpoint_id == identifier
    assert state.last_error is None

    # token 只存在于密文与服务侧：不出现在任何响应里，也不隐式轮换
    assert token not in response.text
    assert token not in client.get(LIST_ENDPOINTS, headers=auth_headers).text
    assert "rotate-token" not in "".join(api.post_paths())
    assert mcp_client.decrypt_headers(server.headers_encrypted) == {
        "Authorization": f"Bearer {token}"}
    assert token not in (server.headers_encrypted or "")

    # 重复初始化幂等：不重复创建，也不重建镜像行
    second = client.post(INITIALIZE, headers=auth_headers, json={})
    assert second.status_code == 200
    assert second.json()["initialized"] is True
    assert len(api.created_knowledge_bases) == 1
    assert len(api.created_endpoints) == 1
    assert db.query(SuperAssistantMcpServer).count() == 1
    assert second.json()["endpoints"][0]["connection_status"] == "ready"


def test_initialize_latch_prevents_recreation_after_native_delete(
    client, api, discovery, auth_headers, db, admin_user,
):
    assert client.post(INITIALIZE, headers=auth_headers, json={}).status_code == 200
    server = the_server(db)
    mcp_server_service.remove_mcp_server(
        db, admin_user.id, server.id, include_builtins=True)
    api.endpoints.clear()
    api.requests.clear()

    response = client.post(INITIALIZE, headers=auth_headers, json={})

    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is True
    assert body["endpoints"] == []
    # 闩置位后原生删除不触发自动重建
    assert len(api.created_endpoints) == 1
    assert db.query(SuperAssistantMcpServer).count() == 0
    assert set(api.post_paths()) <= {"/api/v1/auth/login"}


def test_explicit_recreate_reuses_knowledge_base_with_new_endpoint_identity(
    client, api, discovery, auth_headers, db, admin_user,
):
    assert client.post(INITIALIZE, headers=auth_headers, json={}).status_code == 200
    first = the_server(db)
    old_identifier = first.weknora_endpoint_id
    old_name = first.name
    old_capabilities = capability_rows(db, first)
    mcp_server_service.remove_mcp_server(
        db, admin_user.id, first.id, include_builtins=True)
    api.endpoints.clear()

    response = client.post(INITIALIZE, headers=auth_headers, json={"recreate": True})

    assert response.status_code == 200
    assert len(api.created_endpoints) == 2
    assert len(api.created_knowledge_bases) == 1  # 知识库复用，不重建
    new_identifier = api.endpoints[0]["id"]
    assert new_identifier != old_identifier
    row = response.json()["endpoints"][0]
    assert row["id"] == new_identifier
    assert row["connection_status"] == "ready"

    server = the_server(db)
    assert server.weknora_endpoint_id == new_identifier
    # 新端点使用独立名称与能力版本；旧端点的历史能力保持撤销。
    assert server.name != old_name
    assert server.manifest_revision == 1
    rows = capability_rows(db, server)
    assert rows and all(r.revision == server.manifest_revision and r.enabled
                        for r in rows)
    for revision in old_capabilities:
        db.refresh(revision)
        assert revision.enabled is False
    state = db.query(SuperAssistantWeKnoraIntegration).one()
    assert state.default_endpoint_id == new_identifier


# ──────── 初始化门槛：缺模型/多模型无默认/重名 KB/KB 缺模型 ────────


@pytest.mark.parametrize(("shape", "code"), [
    ("no-models", "WEKNORA_MODELS_REQUIRED"),
    ("inactive-models", "WEKNORA_MODELS_REQUIRED"),
    ("no-unique-default", "WEKNORA_MODELS_REQUIRED"),
    ("duplicate-openontology-kbs", "WEKNORA_KB_AMBIGUOUS"),
    ("existing-kb-without-models", "WEKNORA_MODELS_REQUIRED"),
])
def test_initialize_gate_blocks_without_latching(
    client, api, discovery, auth_headers, db, shape, code,
):
    if shape == "no-models":
        api.models = []
    elif shape == "inactive-models":
        api.models = [{"id": "model-embedding-1", "type": "Embedding",
                       "status": "inactive", "default_for": ["Embedding"]}]
    elif shape == "no-unique-default":
        api.models = [
            {"id": "model-embedding-1", "type": "Embedding", "status": "active",
             "default_for": ["Embedding"]},
            {"id": "model-qa-1", "type": "KnowledgeQA", "status": "active"},
            {"id": "model-qa-2", "type": "KnowledgeQA", "status": "active"},
        ]
    elif shape == "duplicate-openontology-kbs":
        api.add_knowledge_base("kb-a", "OpenOntology")
        api.add_knowledge_base("kb-b", "OpenOntology")
    else:
        api.add_knowledge_base("kb-old", "OpenOntology",
                               embedding_model_id=None, summary_model_id=None)

    response = client.post(INITIALIZE, headers=auth_headers, json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    assert api.created_knowledge_bases == []
    assert api.created_endpoints == []
    state = db.query(SuperAssistantWeKnoraIntegration).one()
    assert state.initialized_at is None  # 上游写之前失败不置闩
    assert state.last_error


@pytest.mark.parametrize("resource", ["knowledge-bases", "mcp-endpoints"])
def test_lost_creation_response_requires_discovery_instead_of_repeated_writes(
    client, api, discovery, auth_headers, db, monkeypatch, resource,
):
    original = WeKnoraClient.create

    def lose_response(native, kind, body):
        result = original(native, kind, body)
        if kind == resource:
            from app.super_assistant.weknora.client import WeKnoraError
            raise WeKnoraError("WEKNORA_UNAVAILABLE", "WeKnora 暂时无法连接，请稍后重试")
        return result

    monkeypatch.setattr(WeKnoraClient, "create", lose_response)
    failed = client.post(INITIALIZE, headers=auth_headers, json={})
    assert failed.status_code == 503
    assert db.query(SuperAssistantWeKnoraIntegration).one().initialized_at is not None
    before = (len(api.created_knowledge_bases), len(api.created_endpoints))
    repeated = client.post(INITIALIZE, headers=auth_headers, json={})
    assert repeated.status_code == 200
    assert (len(api.created_knowledge_bases), len(api.created_endpoints)) == before
    assert api.rotate_requests == 0
    assert db.query(SuperAssistantMcpServer).count() == 0
    if resource == "mcp-endpoints":
        row = repeated.json()["endpoints"][0]
        assert row["connection_status"] == "not_connected"
        assert row["server_id"] is None
        assert api.tokens[row["id"]] not in repeated.text


def test_disabled_connection_stays_disabled_after_successful_reverification(
    client, api, discovery, auth_headers, db, admin_user,
):
    from app.super_assistant.schemas import McpServerUpdate

    connect_ready(client, api, auth_headers)
    server = the_server(db)
    mcp_server_service.update_mcp_server(
        db, admin_user.id, server.id, McpServerUpdate(enabled=False), include_builtins=True)
    response = client.post(f"{LIST_ENDPOINTS}/ep-team/verify", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["last_test_status"] == "success"
    assert response.json()["enabled"] is False
    assert all(not row.enabled for row in capability_rows(db, the_server(db)))


# ───────────── 接入既有端点：token 必填、格式校验、互斥轮换 ─────────────


@pytest.mark.parametrize(("body", "code"), [
    ({}, "WEKNORA_TOKEN_REQUIRED"),
    ({"token": "short"}, "WEKNORA_TOKEN_INVALID"),
    ({"token": "mcp_" + "a" * 24, "rotate_token": True}, "WEKNORA_AUTH_AMBIGUOUS"),
], ids=["no-token", "malformed-token", "token-and-rotate"])
def test_connect_rejects_incomplete_authorization(
    client, api, discovery, auth_headers, db, body, code,
):
    api.add_knowledge_base("kb-team", "Team KB")
    api.add_endpoint("ep-team", "Team Endpoint", ["kb-team"])

    response = client.post(f"{LIST_ENDPOINTS}/ep-team/connect",
                           headers=auth_headers, json=body)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert db.query(SuperAssistantMcpServer).count() == 0
    assert api.rotate_requests == 0
    # Invalid credential-bearing JSON shapes must not echo raw input either.
    secret = "mcp_synthetic_malformed_shape_secret"
    malformed = client.post(f"{LIST_ENDPOINTS}/ep-team/connect", headers=auth_headers,
                            json={"token": {"saved": secret}})
    assert malformed.status_code == 422
    assert secret not in malformed.text


def test_connect_with_token_verifies_scope_and_persists_latch(
    client, api, discovery, auth_headers, db,
):
    api.add_knowledge_base("kb-team", "Team KB")
    api.add_endpoint("ep-team", "Team Endpoint", ["kb-team"])
    token = api.tokens["ep-team"]

    response = client.post(f"{LIST_ENDPOINTS}/ep-team/connect",
                           headers=auth_headers, json={"token": token})

    assert response.status_code == 200
    assert token not in response.text
    body = response.json()
    assert body["constrained"] is True
    assert body["weknora_scope"]["knowledge_base_ids"] == ["kb-team"]
    assert body["weknora_scope"]["resolved_knowledge_base_ids"] == ["kb-team"]
    assert set(body["weknora_scope"]["tools"]) == set(READ_TOOLS)
    assert token not in json.dumps(body["weknora_scope"])

    server = the_server(db)
    assert server.url == f"{FAKE_ORIGIN}/mcp/ep-team"
    assert server.constrained is True
    assert server.last_test_status == "success"
    assert mcp_client.decrypt_headers(server.headers_encrypted) == {
        "Authorization": f"Bearer {token}"}
    assert token not in (server.headers_encrypted or "")
    assert api.rotate_requests == 0

    # 接入既有端点同样持久化初始化闩
    state = db.query(SuperAssistantWeKnoraIntegration).one()
    assert state.initialized_at is not None
    assert state.last_error is None


def test_reconnect_reuses_ciphertext_and_rotation_needs_explicit_request(
    client, api, discovery, auth_headers, db,
):
    endpoint = connect_ready(client, api, auth_headers)
    identifier = endpoint["id"]
    first_token = api.tokens[identifier]
    first_ciphertext = the_server(db).headers_encrypted

    again = client.post(f"{LIST_ENDPOINTS}/{identifier}/connect",
                        headers=auth_headers, json={})

    assert again.status_code == 200
    assert api.rotate_requests == 0  # 同 server 重连复用密文，不隐式轮换
    server = the_server(db)
    assert server.headers_encrypted == first_ciphertext
    assert mcp_client.decrypt_headers(server.headers_encrypted) == {
        "Authorization": f"Bearer {first_token}"}

    rotated = client.post(f"{LIST_ENDPOINTS}/{identifier}/connect",
                          headers=auth_headers, json={"rotate_token": True})

    assert rotated.status_code == 200
    assert api.rotate_requests == 1
    second_token = api.tokens[identifier]
    assert second_token != first_token
    assert second_token not in rotated.text
    server = the_server(db)
    assert mcp_client.decrypt_headers(server.headers_encrypted) == {
        "Authorization": f"Bearer {second_token}"}
    assert server.constrained is True


def test_reconnect_after_disconnect_continues_capability_revisions(
    client, api, discovery, auth_headers, db, admin_user,
):
    endpoint = connect_ready(client, api, auth_headers)
    first = the_server(db)
    assert first.manifest_revision == 1
    mcp_server_service.remove_mcp_server(
        db, admin_user.id, first.id, include_builtins=True)

    response = client.post(
        f"{LIST_ENDPOINTS}/{endpoint['id']}/connect", headers=auth_headers,
        json={"token": api.tokens[endpoint["id"]]})

    assert response.status_code == 200
    server = the_server(db)
    # 同名连接重建立即在历史能力版本之上续编（新密文哈希不可复用 rev1）
    assert server.manifest_revision >= 2
    rows = capability_rows(db, server)
    assert rows and all(
        r.enabled for r in rows if r.revision == server.manifest_revision)


# ────────── 目录漂移：范围/凭据/停用/删除 → 撤销信任与能力 ──────────


@pytest.mark.parametrize(("drift", "status", "extra"), [
    ("scope", "needs_verification", None),
    ("token-hint", "needs_verification", None),
    ("disabled", "unavailable", None),
    ("deleted", "unavailable", {"missing": True}),
], ids=["scope-drift", "token-hint-drift", "disabled", "deleted"])
def test_directory_drift_revokes_trust_and_capabilities(
    client, api, discovery, auth_headers, db, drift, status, extra,
):
    endpoint = connect_ready(client, api, auth_headers)
    if drift == "scope":
        api.add_knowledge_base("kb-other", "Other KB")
        api.endpoints[0]["knowledge_base_ids"] = ["kb-team", "kb-other"]
    elif drift == "token-hint":
        api.simulate_native_rotation(endpoint["id"])
    elif drift == "disabled":
        api.endpoints[0]["enabled"] = False
    else:
        api.endpoints.clear()

    response = client.get(LIST_ENDPOINTS, headers=auth_headers)

    row = response.json()["endpoints"][0]
    assert row["connection_status"] == status
    for key, value in (extra or {}).items():
        assert row[key] == value
    server = the_server(db)
    assert server.constrained is False
    assert server.last_test_status == "error"
    rows = capability_rows(db, server)
    assert rows and all(not r.enabled for r in rows)
    if drift == "token-hint":
        assert api.rotate_requests == 0  # 漂移撤销信任，而非代为轮换


def test_unsafe_or_scope_blind_endpoint_is_never_hosted(
    client, api, discovery, auth_headers, db,
):
    api.add_knowledge_base("kb-x", "Knowledge")
    api.add_endpoint("ep-unsafe", "Unsafe", ["kb-x"],
                     tools=["search_knowledge", "delete_document"])
    api.add_endpoint(
        "ep-noscope", "No Scope Tool", ["kb-x"],
        tools=[t for t in READ_TOOLS if t != "list_knowledge_bases"])

    response = client.get(LIST_ENDPOINTS, headers=auth_headers)

    rows = {row["id"]: row for row in response.json()["endpoints"]}
    assert rows["ep-unsafe"]["readonly"] is False
    assert rows["ep-unsafe"]["connection_status"] == "unavailable"
    assert rows["ep-noscope"]["readonly"] is False
    assert rows["ep-noscope"]["connection_status"] == "unavailable"

    unsafe = client.post(f"{LIST_ENDPOINTS}/ep-unsafe/connect", headers=auth_headers,
                         json={"token": api.tokens["ep-unsafe"]})
    noscope = client.post(f"{LIST_ENDPOINTS}/ep-noscope/connect", headers=auth_headers,
                          json={"token": api.tokens["ep-noscope"]})
    assert unsafe.status_code == 409
    assert unsafe.json()["detail"]["code"] == "WEKNORA_TOOLS_UNSAFE"
    assert noscope.status_code == 409
    assert noscope.json()["detail"]["code"] == "WEKNORA_SCOPE_TOOL_REQUIRED"
    assert db.query(SuperAssistantMcpServer).count() == 0


# ─────────────── 验证：失败安全保留停用选择、版本轮转 ───────────────


def test_verify_failure_is_fail_closed_and_preserves_the_enabled_choice(
    client, api, discovery, auth_headers, db,
):
    connect_ready(client, api, auth_headers)
    discovery.error = "connection refused"

    response = client.post(f"{LIST_ENDPOINTS}/ep-team/verify", headers=auth_headers)

    assert response.status_code == 200  # 验证失败安全：返回错误状态而非异常
    body = response.json()
    assert body["constrained"] is False
    assert body["last_test_status"] == "error"
    assert body["enabled"] is True  # 停用选择不因失败验证被翻转
    listing = client.get(LIST_ENDPOINTS, headers=auth_headers)
    assert listing.json()["endpoints"][0]["connection_status"] == "needs_verification"

    server = the_server(db)
    server.enabled = False
    db.commit()
    discovery.error = "still down"
    again = client.post(f"{LIST_ENDPOINTS}/ep-team/verify", headers=auth_headers)
    assert again.status_code == 200
    assert again.json()["enabled"] is False
    assert again.json()["constrained"] is False
    discovery.error = None
    recovered = client.post(f"{LIST_ENDPOINTS}/ep-team/verify", headers=auth_headers)
    assert recovered.status_code == 200
    assert recovered.json()["last_test_status"] == "success"
    server = the_server(db)
    assert server.manifest_revision == 2
    assert server.enabled is False
    assert all(not row.enabled for row in capability_rows(db, server))


def test_reverify_with_changed_tools_rotates_capability_revision(
    client, api, discovery, auth_headers, db,
):
    connect_ready(client, api, auth_headers)
    reduced = READ_TOOLS[:5]
    api.endpoints[0]["tools"] = reduced
    discovery.manifest = read_manifest(reduced)

    response = client.post(f"{LIST_ENDPOINTS}/ep-team/verify", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["last_test_status"] == "success"
    server = the_server(db)
    assert server.manifest_revision == 2
    assert [tool["name"] for tool in server.tool_manifest] == reduced
    rows = capability_rows(db, server)
    assert all(not r.enabled for r in rows if r.revision == 1)
    assert rows and all(r.enabled for r in rows if r.revision == 2)
