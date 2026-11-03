"""Deterministic tests for the one-shot WeKnora bootstrap.

The fake WeKnora below mirrors the v0.9.0 REST shapes that bootstrap relies
on: auth config/login/register, model, knowledge-base and MCP-endpoint lists,
the one-time endpoint token, and the system-admin registration setting.  No
test reaches a real service, and MCP is supplied as an in-process fake.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import stat
from urllib.parse import parse_qs

import httpx
import pytest

from app.super_assistant.weknora import bootstrap, policy


FORMAL_BASE = "http://weknora-app:8080"
BOOTSTRAP_BASE = "http://weknora-app-init:8080"


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    values = {
        "WEKNORA_BASE_URL": FORMAL_BASE,
        "WEKNORA_BOOTSTRAP_BASE_URL": BOOTSTRAP_BASE,
        "WEKNORA_STATE_PATH": str(tmp_path / "state" / "integration.json"),
        "WEKNORA_ADMIN_USER": "admin",
        "WEKNORA_ADMIN_EMAIL": "admin@example.com",
        "WEKNORA_ADMIN_PASSWORD": "Abcd1234efgh",
        "WEKNORA_CHAT_BASE_URL": "http://llm.internal:8000/v1",
        "WEKNORA_CHAT_API_KEY": "chat-secret-key",
        "WEKNORA_CHAT_MODEL": "chat-model",
        "WEKNORA_EMBEDDING_BASE_URL": "http://embed.internal:8000/v1",
        "WEKNORA_EMBEDDING_API_KEY": "embed-secret-key",
        "WEKNORA_EMBEDDING_MODEL": "embed-model",
        "WEKNORA_EMBEDDING_DIMENSIONS": "1024",
        "WEKNORA_KNOWLEDGE_BASE_NAME": "共享知识库",
        "WEKNORA_TIMEOUT_SECONDS": "15",
    }
    values.update(overrides)
    return values


def _config(tmp_path: Path, **overrides: str) -> bootstrap.BootstrapConfig:
    return bootstrap.build_config(_env(tmp_path, **overrides))


def _state(config: bootstrap.BootstrapConfig) -> dict:
    return json.loads(Path(config.state_path).read_text(encoding="utf-8"))


def _transport(server: "FakeWeKnora") -> httpx.MockTransport:
    return httpx.MockTransport(server.handler)


def _default_tools() -> list[dict]:
    return [
        {
            "name": name,
            "description": f"{name} description",
            "input_schema": {"type": "object", "properties": {}},
        }
        for name in bootstrap.READ_TOOL_NAMES
    ]


class FakeMcp:
    def __init__(self, *, kb_ids: list[str] | None = None,
                 tools: list[dict] | None = None) -> None:
        self.kb_ids = list(kb_ids or [])
        self.tools = tools if tools is not None else _default_tools()
        self.calls: list[tuple[str, dict]] = []
        self.fail_discover = False
        self.fail_call = False
        self.call_raw: str | None = None

    async def discover_tools(self, **kwargs):
        self.calls.append(("discover", kwargs))
        if self.fail_discover:
            raise RuntimeError("discovery failed")
        return copy.deepcopy(self.tools)

    async def call_tool(self, **kwargs):
        self.calls.append(("call", kwargs))
        if self.fail_call:
            raise RuntimeError("tool call failed")
        if self.call_raw is not None:
            return self.call_raw
        payload = {
            "knowledge_bases": [{"id": kb_id} for kb_id in self.kb_ids],
            "total": len(self.kb_ids),
        }
        return json.dumps({
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload,
            "isError": False,
        })


class FakeWeKnora:
    """Small v0.9.0-compatible REST fake used through httpx.MockTransport."""

    def __init__(self) -> None:
        self.registration_mode = "self_serve"
        self.users: dict[str, dict] = {}
        self.models: list[dict] = []
        self.kbs: list[dict] = []
        self.endpoints: list[dict] = []
        self.tokens: dict[str, str] = {}
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.login_requests: list[dict] = []
        self.register_requests: list[dict] = []
        self.model_creates: list[dict] = []
        self.kb_creates: list[dict] = []
        self.endpoint_creates: list[dict] = []
        self.rotations: list[str] = []
        self.setting_updates: list[dict] = []
        self.debug_requests: list[dict] = []
        self.debug_results: dict[str, tuple[int, dict]] = {}
        self.error_on: dict[tuple[str, str], tuple[int, dict]] = {}
        self._model_seq = 0
        self._kb_seq = 0
        self._endpoint_seq = 0
        self._token_seq = 0

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _response(status: int, payload: dict) -> httpx.Response:
        return httpx.Response(status, json=payload)

    @staticmethod
    def _public_model(model: dict) -> dict:
        row = copy.deepcopy(model)
        parameters = row.get("parameters")
        if isinstance(parameters, dict):
            parameters.pop("api_key", None)
        return row

    @staticmethod
    def _public_user(user: dict) -> dict:
        return {
            key: user[key]
            for key in ("id", "username", "email", "is_system_admin")
        }

    def _user_by_id(self, user_id: str) -> dict | None:
        return next(
            (user for user in self.users.values() if user["id"] == user_id),
            None,
        )

    def _authorized_user(self, request: httpx.Request) -> dict | None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        return self._user_by_id(self.tokens.get(header[7:], ""))

    def _new_token(self, prefix: str) -> str:
        self._token_seq += 1
        return f"mcp_{prefix}{self._token_seq:032d}"

    # -- transport handler ----------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = None
        self.calls.append((method, request.url.host, path, body))

        override = self.error_on.get((method, path))
        if override is not None:
            return self._response(*override)

        public = {
            ("GET", "/api/v1/auth/config"),
            ("POST", "/api/v1/auth/login"),
            ("POST", "/api/v1/auth/register"),
        }
        user = None
        if (method, path) not in public and path.startswith("/api/v1/"):
            user = self._authorized_user(request)
            if user is None:
                return self._response(401, {"error": "unauthorized"})
            if path.startswith("/api/v1/system/admin/") and not user["is_system_admin"]:
                return self._response(403, {"error": "forbidden"})

        if method == "GET" and path == "/api/v1/auth/config":
            return self._response(200, {
                "success": True,
                "registration_mode": self.registration_mode,
                "complex_password_enabled": True,
            })

        if method == "POST" and path == "/api/v1/auth/login":
            self.login_requests.append(body or {})
            email = (body or {}).get("email")
            password = (body or {}).get("password")
            account = self.users.get(email)
            if account is None or account["password"] != password:
                return self._response(401, {"error": "Login failed"})
            token = f"jwt-{account['id']}"
            self.tokens[token] = account["id"]
            return self._response(200, {
                "success": True,
                "token": token,
                "user": self._public_user(account),
            })

        if method == "POST" and path == "/api/v1/auth/register":
            self.register_requests.append(body or {})
            if self.registration_mode != "self_serve":
                return self._response(403, {"error": "Public registration is disabled"})
            email = (body or {}).get("email")
            if email in self.users:
                return self._response(400, {"error": "email already exists"})
            account = {
                "id": f"user-{len(self.users) + 1}",
                "username": (body or {}).get("username"),
                "email": email,
                "password": (body or {}).get("password"),
                "is_system_admin": False,
            }
            self.users[email] = account
            return self._response(201, {
                "success": True,
                "user": self._public_user(account),
            })

        if method == "GET" and path == "/api/v1/auth/me":
            assert user is not None
            return self._response(200, {
                "success": True,
                "data": {"user": self._public_user(user)},
            })

        if (
            method == "POST"
            and path.startswith("/api/v1/models/")
            and path.endswith("/debug")
        ):
            model_id = path.removeprefix("/api/v1/models/").removesuffix("/debug")
            try:
                form = parse_qs(request.content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                form = {}
            self.debug_requests.append({
                "model_id": model_id,
                "content_type": request.headers.get("content-type", ""),
                "form": form,
            })
            override = self.debug_results.get(model_id)
            if override is not None:
                return self._response(*override)
            model = next(
                (item for item in self.models if item.get("id") == model_id),
                None,
            )
            if model is None:
                return self._response(404, {"error": "not found"})
            if model.get("type") == "Embedding":
                parameters = model.get("parameters") or {}
                dimensions = (
                    parameters.get("embedding_parameters") or {}
                ).get("dimension", 1024)
                return self._response(200, {
                    "success": True,
                    "data": {
                        "ok": True,
                        "elapsed_ms": 1,
                        "request": {},
                        "raw_response": [0.1, 0.2],
                        "observations": {"dimension": dimensions},
                    },
                })
            return self._response(200, {
                "success": True,
                "data": {
                    "ok": True,
                    "elapsed_ms": 1,
                    "request": {},
                    "raw_response": {
                        "content": "pong",
                        "finish_reason": "stop",
                    },
                    "observations": {"answer_characters": 4},
                },
            })

        if method == "GET" and path == "/api/v1/models":
            return self._response(200, {
                "success": True,
                "data": [self._public_model(model) for model in self.models],
            })

        if method == "POST" and path == "/api/v1/models":
            self.model_creates.append(body or {})
            self._model_seq += 1
            row = {"id": f"model-{self._model_seq}", **(body or {}), "status": "active"}
            self.models.append(row)
            return self._response(201, {
                "success": True,
                "data": self._public_model(row),
            })

        if method == "GET" and path == "/api/v1/knowledge-bases":
            return self._response(200, {
                "success": True,
                "data": copy.deepcopy(self.kbs),
            })

        if method == "POST" and path == "/api/v1/knowledge-bases":
            self.kb_creates.append(body or {})
            self._kb_seq += 1
            row = {"id": f"kb-{self._kb_seq}", **(body or {})}
            row.setdefault("type", "document")
            self.kbs.append(row)
            return self._response(201, {"success": True, "data": copy.deepcopy(row)})

        if method == "GET" and path == "/api/v1/mcp-endpoints":
            return self._response(200, {
                "success": True,
                "data": copy.deepcopy(self.endpoints),
            })

        if method == "POST" and path == "/api/v1/mcp-endpoints":
            self.endpoint_creates.append(body or {})
            self._endpoint_seq += 1
            row = {"id": f"endpoint-{self._endpoint_seq}", **(body or {})}
            self.endpoints.append(row)
            return self._response(201, {
                "success": True,
                "data": {
                    **copy.deepcopy(row),
                    "token": self._new_token("create"),
                },
            })

        if (
            method == "POST"
            and path.startswith("/api/v1/mcp-endpoints/")
            and path.endswith("/rotate-token")
        ):
            endpoint_id = path.removeprefix("/api/v1/mcp-endpoints/").removesuffix(
                "/rotate-token"
            )
            row = next(
                (item for item in self.endpoints if item["id"] == endpoint_id),
                None,
            )
            if row is None:
                return self._response(404, {"error": "mcp endpoint not found"})
            self.rotations.append(endpoint_id)
            return self._response(200, {
                "success": True,
                "data": {
                    **copy.deepcopy(row),
                    "token": self._new_token("rotate"),
                },
            })

        if method == "PUT" and path.startswith("/api/v1/system/admin/settings/"):
            key = path.rsplit("/", 1)[-1]
            value = (body or {}).get("value")
            self.setting_updates.append({"key": key, "value": value})
            if key == "auth.registration_mode":
                self.registration_mode = value
            return self._response(200, {"key": key, "value": value})

        return self._response(404, {"error": "not found"})


def _seed_ready(server: FakeWeKnora, config: bootstrap.BootstrapConfig, *,
                kb_id: str = "kb-existing",
                endpoint_id: str = "endpoint-existing",
                is_system_admin: bool = False) -> None:
    """Seed the exact resources a previous, state-less run would have left."""
    server.users[config.admin_email] = {
        "id": "user-existing",
        "username": config.admin_username,
        "email": config.admin_email,
        "password": config.admin_password,
        "is_system_admin": is_system_admin,
    }
    server.models = [
        {
            "id": "chat-existing",
            "name": config.chat_model,
            "type": "KnowledgeQA",
            "source": "remote",
            "status": "active",
            "parameters": {
                "base_url": config.chat_base_url,
                "provider": "generic",
                "interface_type": "openai",
            },
        },
        {
            "id": "embedding-existing",
            "name": config.embedding_model,
            "type": "Embedding",
            "source": "remote",
            "status": "active",
            "parameters": {
                "base_url": config.embedding_base_url,
                "provider": "generic",
                "interface_type": "openai",
                "embedding_parameters": {"dimension": config.embedding_dimensions},
            },
        },
    ]
    server.kbs = [{
        "id": kb_id,
        "name": config.knowledge_base_name,
        "type": "document",
        "embedding_model_id": "embedding-existing",
        "indexing_strategy": {
            "vector_enabled": True,
            "keyword_enabled": True,
            "wiki_enabled": False,
            "graph_enabled": True,
        },
        "extract_config": bootstrap._extract_config(),
    }]
    server.endpoints = [{
        "id": endpoint_id,
        "name": config.endpoint_name,
        "enabled": True,
        "knowledge_base_ids": [kb_id],
        "tools": list(bootstrap.READ_TOOL_NAMES),
        "default_agent_id": "",
    }]


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def test_read_only_tool_list_uses_policy_single_source():
    expected = tuple(
        name for name in policy.WEKNORA_ALLOWED_TOOLS if name != "ask"
    )
    assert bootstrap.READ_TOOL_NAMES == expected
    assert bootstrap.READ_TOOLS == frozenset(expected)
    assert len(bootstrap.READ_TOOL_NAMES) == 8
    assert "ask" not in bootstrap.READ_TOOLS
    assert bootstrap.REQUIRED_TOOLS == frozenset(
        name for name in policy.WEKNORA_REQUIRED_TOOLS
        if name in bootstrap.READ_TOOLS
    )


def test_initialize_creates_exact_resources_and_state(tmp_path):
    server = FakeWeKnora()
    mcp = FakeMcp(kb_ids=["kb-1"])
    config = _config(tmp_path)

    state = _run(bootstrap.initialize(config, transport=_transport(server), mcp=mcp))

    assert state["version"] == 1
    assert state["base_url"] == FORMAL_BASE
    assert state["mcp_endpoint_id"] == "endpoint-1"
    assert state["mcp_token"].startswith("mcp_")
    assert state["knowledge_base_id"] == "kb-1"
    assert state["knowledge_base_name"] == "共享知识库"
    assert state["chat_model_id"] == "model-1"
    assert state["embedding_model_id"] == "model-2"
    assert state["admin_user_id"] == "user-1"
    assert state["verified"] is False
    assert {tool["name"] for tool in state["tools"]} == bootstrap.READ_TOOLS

    path = Path(config.state_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert _state(config) == state

    # Login failed first, registration created the admin, then login succeeded.
    assert server.register_requests == [{
        "username": "admin",
        "email": "admin@example.com",
        "password": "Abcd1234efgh",
    }]
    assert len(server.login_requests) == 2

    chat, embedding = server.model_creates
    assert chat["name"] == "chat-model"
    assert chat["type"] == "KnowledgeQA"
    assert chat["source"] == "remote"
    assert chat["parameters"]["provider"] == "generic"
    assert chat["parameters"]["interface_type"] == "openai"
    assert chat["parameters"]["base_url"] == "http://llm.internal:8000/v1"
    assert embedding["type"] == "Embedding"
    assert embedding["parameters"]["embedding_parameters"] == {"dimension": 1024}
    assert "embedding_parameters" not in chat["parameters"]

    # The platform MCP surface is exactly the eight read-only tools; ask is a
    # hard denial because it is not read-only and can execute agent tools.
    assert not any(
        call[2].startswith("/api/v1/agents") for call in server.calls
    )

    kb_request = server.kb_creates[0]
    assert kb_request["name"] == "共享知识库"
    assert kb_request["embedding_model_id"] == "model-2"
    assert kb_request["summary_model_id"] == "model-1"
    assert kb_request["indexing_strategy"]["graph_enabled"] is True
    extract = kb_request["extract_config"]
    assert extract["enabled"] is True
    assert extract["text"]
    assert extract["tags"]
    assert extract["nodes"]
    assert extract["relations"]

    endpoint_request = server.endpoint_creates[0]
    assert endpoint_request["name"] == "共享知识库 MCP"
    assert endpoint_request["enabled"] is True
    assert endpoint_request["knowledge_base_ids"] == ["kb-1"]
    assert endpoint_request["default_agent_id"] == ""
    assert len(endpoint_request["tools"]) == 8
    assert "ask" not in endpoint_request["tools"]
    assert set(endpoint_request["tools"]) == bootstrap.READ_TOOLS
    assert set(endpoint_request["tools"]).isdisjoint(bootstrap._WRITE_TOOLS)

    kind, call = mcp.calls[0]
    assert kind == "discover"
    assert call["internal_base_url"] == BOOTSTRAP_BASE
    assert call["url"] == f"{BOOTSTRAP_BASE}/mcp/endpoint-1"
    assert call["headers"]["Authorization"] == f"Bearer {state['mcp_token']}"

    # Re-running the init must adopt every exact resource and never rotate.
    state_again = _run(
        bootstrap.initialize(config, transport=_transport(server), mcp=mcp)
    )
    assert state_again == state
    assert len(server.model_creates) == 2
    assert len(server.kb_creates) == 1
    assert len(server.endpoint_creates) == 1
    assert server.rotations == []
    assert len(server.register_requests) == 1
    assert not any(
        call[2].startswith("/api/v1/agents") for call in server.calls
    )


def test_state_missing_endpoint_is_adopted_with_one_rotation(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _seed_ready(server, config)
    mcp = FakeMcp(kb_ids=["kb-existing"])

    state = _run(bootstrap.initialize(config, transport=_transport(server), mcp=mcp))

    assert server.model_creates == []
    assert server.kb_creates == []
    assert server.endpoint_creates == []
    assert server.rotations == ["endpoint-existing"]
    assert state["mcp_endpoint_id"] == "endpoint-existing"
    assert state["knowledge_base_id"] == "kb-existing"
    assert state["mcp_token"].startswith("mcp_rotate")
    assert state["chat_model_id"] == "chat-existing"
    assert state["embedding_model_id"] == "embedding-existing"
    assert server.login_requests and server.register_requests == []

    # A second run sees the saved token and must not rotate again.
    state_again = _run(
        bootstrap.initialize(config, transport=_transport(server), mcp=mcp)
    )
    assert state_again["mcp_token"] == state["mcp_token"]
    assert server.rotations == ["endpoint-existing"]


@pytest.mark.parametrize(
    "mutation",
    ["two_kbs", "write_tool", "ask"],
    ids=["two-kbs", "write-tool", "ask"],
)
def test_init_refuses_modified_endpoint_without_duplicates(tmp_path, mutation):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _seed_ready(server, config)
    endpoint = server.endpoints[0]
    if mutation == "two_kbs":
        endpoint["knowledge_base_ids"] = ["kb-existing", "kb-other"]
    elif mutation == "write_tool":
        tools = list(bootstrap.READ_TOOL_NAMES)
        tools[-1] = "add_document"
        endpoint["tools"] = tools
    else:
        endpoint["tools"] = [*bootstrap.READ_TOOL_NAMES, "ask"]
    mcp = FakeMcp(kb_ids=["kb-existing"])

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(config, transport=_transport(server), mcp=mcp))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_ENDPOINT_CONFLICT"
    assert server.endpoint_creates == []
    assert server.rotations == []
    assert not Path(config.state_path).exists()


def test_mcp_write_tool_is_rejected_then_next_run_recovers(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    bad_tools = _default_tools() + [{
        "name": "add_document",
        "description": "write",
        "input_schema": {"type": "object", "properties": {}},
    }]
    bad_mcp = FakeMcp(kb_ids=["kb-1"], tools=bad_tools)

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(config, transport=_transport(server), mcp=bad_mcp))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_UNEXPECTED_TOOL"
    assert not Path(config.state_path).exists()
    assert len(server.endpoint_creates) == 1

    # The endpoint was created before discovery failed.  The retry must adopt
    # it, rotate its one-time token exactly once, and never create a duplicate.
    good_mcp = FakeMcp(kb_ids=["kb-1"])
    state = _run(bootstrap.initialize(config, transport=_transport(server), mcp=good_mcp))

    assert len(server.endpoint_creates) == 1
    assert server.rotations == ["endpoint-1"]
    assert state["mcp_endpoint_id"] == "endpoint-1"
    assert state["mcp_token"].startswith("mcp_rotate")


def test_state_with_wrong_password_is_not_overwritten(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    before = Path(config.state_path).read_bytes()
    server.users[config.admin_email]["password"] = "DifferentPass123"
    creates_before = (
        len(server.model_creates),
        len(server.kb_creates),
        len(server.endpoint_creates),
        len(server.rotations),
    )

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_LOGIN_FAILED"
    assert Path(config.state_path).read_bytes() == before
    assert len(server.register_requests) == 1
    assert (
        len(server.model_creates),
        len(server.kb_creates),
        len(server.endpoint_creates),
        len(server.rotations),
    ) == creates_before


def test_registration_closed_without_state_fails_before_register(tmp_path):
    server = FakeWeKnora()
    server.registration_mode = "invite_only"
    config = _config(tmp_path)

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp()))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_REGISTRATION_CLOSED"
    assert server.register_requests == []
    assert server.model_creates == []
    assert not Path(config.state_path).exists()


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def test_verify_closes_registration_and_marks_state_verified(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    first = _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True
    mcp = FakeMcp(kb_ids=["kb-1"])
    creates_before = (
        len(server.model_creates),
        len(server.kb_creates),
        len(server.endpoint_creates),
    )

    verified = _run(bootstrap.verify(config, transport=_transport(server), mcp=mcp))

    assert verified["verified"] is True
    assert verified["mcp_token"] == first["mcp_token"]
    assert verified["knowledge_base_id"] == first["knowledge_base_id"]
    assert server.registration_mode == "invite_only"
    assert server.setting_updates == [{
        "key": "auth.registration_mode",
        "value": "invite_only",
    }]
    assert server.rotations == []
    assert not any(
        call[2].startswith("/api/v1/agents") for call in server.calls
    )
    assert (
        len(server.model_creates),
        len(server.kb_creates),
        len(server.endpoint_creates),
    ) == creates_before
    assert stat.S_IMODE(Path(config.state_path).stat().st_mode) == 0o600
    assert _state(config)["verified"] is True

    # One minimal form-urlencoded debug call per saved model, using the saved
    # credentials rather than a caller-supplied key.
    assert len(server.debug_requests) == 2
    chat_debug, embedding_debug = server.debug_requests
    assert chat_debug["model_id"] == "model-1"
    assert chat_debug["form"] == {"input": ["ping"]}
    assert chat_debug["content_type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert embedding_debug["model_id"] == "model-2"
    assert embedding_debug["form"] == {"input": ["hello"]}
    assert embedding_debug["content_type"].startswith(
        "application/x-www-form-urlencoded"
    )
    for debug in server.debug_requests:
        assert "api_key" not in debug["form"]
        assert "key" not in debug["form"]

    # Verify must talk to the formal app origin, not the temporary init app.
    logins = [
        call for call in server.calls
        if call[0] == "POST" and call[2] == "/api/v1/auth/login"
    ]
    assert logins[-1][1] == "weknora-app"
    assert any(
        call[0] == "PUT" and call[1] == "weknora-app"
        and call[2].endswith("/settings/auth.registration_mode")
        for call in server.calls
    )

    kinds = [kind for kind, _ in mcp.calls]
    assert kinds == ["discover", "call"]
    discover = mcp.calls[0][1]
    assert discover["internal_base_url"] == FORMAL_BASE
    assert discover["url"] == f"{FORMAL_BASE}/mcp/endpoint-1"
    call = mcp.calls[1][1]
    assert call["internal_base_url"] == FORMAL_BASE
    assert call["tool_name"] == "list_knowledge_bases"
    assert call["arguments"] == {}


def test_verify_requires_system_admin_before_closing_registration(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    state = _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.verify(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_NOT_SYSTEM_ADMIN"
    assert server.registration_mode == "self_serve"
    assert server.setting_updates == []
    assert _state(config) == state


def test_verify_rejects_graph_disabled_knowledge_base(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True
    server.kbs[0]["indexing_strategy"]["graph_enabled"] = False

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.verify(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_GRAPH_DISABLED"
    assert _state(config)["verified"] is False


@pytest.mark.parametrize(
    ("kb_ids", "code"),
    [
        ([], "WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_UNAVAILABLE"),
        (["other-kb"], "WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_UNAVAILABLE"),
        (["kb-1", "kb-2"], "WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_SCOPE_CHANGED"),
        (["kb-2", "kb-1"], "WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_SCOPE_CHANGED"),
    ],
    ids=["empty", "wrong-only", "bound-plus-other", "other-plus-bound"],
)
def test_verify_requires_exactly_one_bound_knowledge_base(tmp_path, kb_ids, code):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.verify(config, transport=_transport(server), mcp=FakeMcp(kb_ids=kb_ids)))

    assert exc.value.code == code
    assert _state(config)["verified"] is False


def test_verify_rejects_plain_text_kb_listing(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True
    mcp = FakeMcp(kb_ids=["kb-1"])
    mcp.call_raw = json.dumps({
        "content": [{"type": "text", "text": "the knowledge base kb-1 is available"}],
        "isError": False,
    })

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.verify(config, transport=_transport(server), mcp=mcp))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_INVALID_KNOWLEDGE_BASE_RESPONSE"
    assert _state(config)["verified"] is False


def test_verify_accepts_text_mirror_without_structured_content(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True
    mcp = FakeMcp(kb_ids=["kb-1"])
    mcp.call_raw = json.dumps({
        "content": [{
            "type": "text",
            "text": json.dumps({"knowledge_bases": [{"id": "kb-1"}], "total": 1}),
        }],
        "isError": False,
    })

    verified = _run(bootstrap.verify(config, transport=_transport(server), mcp=mcp))

    assert verified["verified"] is True


@pytest.mark.parametrize(
    ("model_id", "code"),
    [
        ("model-1", "WEKNORA_BOOTSTRAP_CHAT_MODEL_SMOKE_FAILED"),
        ("model-2", "WEKNORA_BOOTSTRAP_EMBEDDING_MODEL_SMOKE_FAILED"),
    ],
    ids=["chat", "embedding"],
)
def test_verify_model_debug_ok_false_fails(tmp_path, model_id, code):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True
    server.debug_results[model_id] = (200, {
        "success": True,
        "data": {
            "ok": False,
            "error": "upstream rejected api_key=chat-secret-key",
            "raw_response": None,
            "observations": {},
        },
    })

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.verify(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))

    assert exc.value.code == code
    assert "chat-secret-key" not in str(exc.value)
    assert "chat-secret-key" not in repr(exc.value)
    assert _state(config)["verified"] is False


@pytest.mark.parametrize(
    "dimension",
    [1023, True, "1024", None],
    ids=["wrong-int", "bool", "string", "missing"],
)
def test_verify_embedding_debug_dimension_must_match(tmp_path, dimension):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True
    server.debug_results["model-2"] = (200, {
        "success": True,
        "data": {
            "ok": True,
            "raw_response": [0.1, 0.2],
            "observations": {"dimension": dimension},
        },
    })

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.verify(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_EMBEDDING_DIMENSION_MISMATCH"
    assert _state(config)["verified"] is False


def test_model_debug_error_body_never_reaches_the_cli(
    tmp_path, monkeypatch, capsys,
):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True
    server.debug_results["model-1"] = (200, {
        "success": True,
        "data": {
            "ok": False,
            "error": (
                "api_key=chat-secret-key password=Abcd1234efgh "
                "token=mcp_leakedtoken000000000000"
            ),
            "raw_response": {"error": "embed-secret-key"},
            "observations": {},
        },
    })
    monkeypatch.setattr(bootstrap, "load_config", lambda for_verify=False: config)

    result = bootstrap.main(
        ["--verify"],
        transport=_transport(server),
        mcp=FakeMcp(kb_ids=["kb-1"]),
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "WEKNORA_BOOTSTRAP_CHAT_MODEL_SMOKE_FAILED" in captured.err
    for secret in (
        "chat-secret-key",
        "embed-secret-key",
        "Abcd1234efgh",
        "mcp_leakedtoken",
    ):
        assert secret not in captured.out
        assert secret not in captured.err


def test_reinitialize_does_not_preserve_verified_state(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    server.users[config.admin_email]["is_system_admin"] = True
    assert _run(
        bootstrap.verify(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"]))
    )["verified"] is True

    state = _run(
        bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"]))
    )

    assert state["verified"] is False
    assert _state(config)["verified"] is False


def test_verify_requires_initialized_state(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.verify(config, transport=_transport(server), mcp=FakeMcp()))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_NOT_INITIALIZED"
    assert server.calls == []


# ---------------------------------------------------------------------------
# Configuration, state safety and redaction
# ---------------------------------------------------------------------------


def test_configuration_missing_and_weak_inputs_fail_closed(tmp_path):
    env = _env(tmp_path)
    env.pop("WEKNORA_EMBEDDING_API_KEY")
    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap.build_config(env)
    assert exc.value.code == "WEKNORA_BOOTSTRAP_CONFIG_MISSING"

    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap.build_config(_env(tmp_path, WEKNORA_ADMIN_PASSWORD="abcdefgh"))
    assert exc.value.code == "WEKNORA_BOOTSTRAP_CONFIG_INVALID"

    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap.build_config(_env(tmp_path, WEKNORA_ADMIN_PASSWORD="short1"))
    assert exc.value.code == "WEKNORA_BOOTSTRAP_CONFIG_INVALID"

    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap.build_config(_env(tmp_path, WEKNORA_BASE_URL="http://host:8080/path"))
    assert exc.value.code == "WEKNORA_BOOTSTRAP_CONFIG_INVALID"

    verify_env = _env(tmp_path)
    verify_env.pop("WEKNORA_BOOTSTRAP_BASE_URL")
    verify_config = bootstrap.build_config(verify_env, for_verify=True)
    assert verify_config.bootstrap_base_url is None


def test_timeout_bounds_match_settings(tmp_path):
    assert bootstrap.build_config(
        _env(tmp_path, WEKNORA_TIMEOUT_SECONDS="3")
    ).timeout_seconds == 3
    assert bootstrap.build_config(
        _env(tmp_path, WEKNORA_TIMEOUT_SECONDS="60")
    ).timeout_seconds == 60
    env = _env(tmp_path)
    env.pop("WEKNORA_TIMEOUT_SECONDS")
    assert bootstrap.build_config(env).timeout_seconds == 15

    for invalid in ("2", "61", "15.5"):
        with pytest.raises(bootstrap.BootstrapError) as exc:
            bootstrap.build_config(
                _env(tmp_path, WEKNORA_TIMEOUT_SECONDS=invalid)
            )
        assert exc.value.code == "WEKNORA_BOOTSTRAP_CONFIG_INVALID"


def test_paginated_list_envelope_is_rejected(tmp_path):
    server = FakeWeKnora()
    server.error_on[("GET", "/api/v1/models")] = (
        200,
        {"success": True, "data": {"items": [], "total": 0}},
    )
    config = _config(tmp_path)

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp()))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_INVALID_RESPONSE"
    assert not Path(config.state_path).exists()


def test_state_symlink_and_symlinked_parent_are_rejected(tmp_path):
    server = FakeWeKnora()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "state-link.json"
    link.symlink_to(target)
    config = _config(tmp_path, WEKNORA_STATE_PATH=str(link))

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp()))
    assert exc.value.code == "WEKNORA_BOOTSTRAP_STATE_UNSAFE"
    assert server.calls == []

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    config = _config(tmp_path, WEKNORA_STATE_PATH=str(alias / "integration.json"))
    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp()))
    assert exc.value.code == "WEKNORA_BOOTSTRAP_STATE_UNSAFE"
    assert server.calls == []


def test_state_origin_mismatch_is_rejected(tmp_path):
    server = FakeWeKnora()
    config = _config(tmp_path)
    _run(bootstrap.initialize(config, transport=_transport(server), mcp=FakeMcp(kb_ids=["kb-1"])))
    other = _config(tmp_path, WEKNORA_BASE_URL="http://other-app:8080")
    calls_before = len(server.calls)

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(other, transport=_transport(server), mcp=FakeMcp()))

    assert exc.value.code == "WEKNORA_BOOTSTRAP_STATE_ORIGIN_MISMATCH"
    assert len(server.calls) == calls_before


def test_http_body_and_credentials_never_reach_the_cli(tmp_path, monkeypatch, capsys):
    secret_body = {
        "error": (
            "api_key=chat-secret-key password=Abcd1234efgh "
            "token=mcp_leakedtoken000000000000"
        )
    }
    server = FakeWeKnora()
    server.error_on[("GET", "/api/v1/models")] = (500, secret_body)
    config = _config(tmp_path)
    transport = _transport(server)

    with pytest.raises(bootstrap.BootstrapError) as exc:
        _run(bootstrap.initialize(config, transport=transport, mcp=FakeMcp()))
    assert str(exc.value) == "WEKNORA_BOOTSTRAP_HTTP_FAILED"
    assert exc.value.status == 500
    for secret in ("chat-secret-key", "Abcd1234efgh", "mcp_leakedtoken"):
        assert secret not in repr(exc.value)
        assert secret not in str(exc.value)

    monkeypatch.setattr(bootstrap, "load_config", lambda for_verify=False: config)
    result = bootstrap.main([], transport=transport, mcp=FakeMcp())
    captured = capsys.readouterr()
    assert result == 1
    assert "WEKNORA_BOOTSTRAP_HTTP_FAILED" in captured.err
    for secret in ("chat-secret-key", "Abcd1234efgh", "mcp_leakedtoken"):
        assert secret not in captured.out
        assert secret not in captured.err
