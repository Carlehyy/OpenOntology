"""本体 MCP 端点协议与传输测试。

业务实现（service/schemas）由业务域测试覆盖；这里钉死传输契约：路由精确
匹配、头认证与节流、Host/Origin 防线、请求体上限、tools/list 与
tools/call 白名单与参数校验、业务错误 detail 透传、QueryControl 取消、
SDK manager 可重复启停与启动失败资源释放。
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import app.ontologies.mcp.auth as mcp_auth
import app.ontologies.mcp.server as mcp_server
from app.main import app as main_app

ONTOLOGY_ID = "00000000-0000-0000-0000-0000000000aa"
MCP_PATH = f"/api/v2/formal/ontologies/{ONTOLOGY_ID}/mcp"

JSON_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


class StubService:
    """业务 service 契约桩：只实现 transport 依赖的三个入口。"""

    def __init__(self):
        self.worker_exited = threading.Event()
        self.observed_control_cancelled = None

    class McpError(Exception):
        def __init__(self, status, code, message, **extra):
            super().__init__(message)
            self.status = status
            self.code = code
            self.message = message
            self.extra = extra

        def detail(self):
            return {"code": self.code, "message": self.message, **self.extra}

    def authorize_request(self, db, user, ontology_id):
        if ontology_id != ONTOLOGY_ID:
            raise self.McpError(404, "ontology_not_found", "本体不存在")
        return {}

    def tool_manifests(self):
        return [
            {
                "name": "ontology_schema",
                "description": "schema tool",
                "inputSchema": {
                    "type": "object",
                    "properties": {"releaseId": {"type": "string"}},
                    "required": ["releaseId"],
                },
            },
            {
                "name": "ontology_query",
                "description": "query tool",
                "inputSchema": {
                    "type": "object",
                    "properties": {"queryText": {"type": "string"}},
                    "required": ["queryText"],
                },
            },
            {
                "name": "ontology_query_page",
                "description": "page tool",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def run_tool(self, db, user, ontology_id, tool_name, arguments):
        from app.shared.query_control import current_query_control

        try:
            if tool_name == "ontology_query" and arguments.get("queryText") == "boom409":
                raise self.McpError(
                    409,
                    "release_moved",
                    "版本已变化",
                    currentReleaseId="r-2",
                    guidance="请重新读取 schema",
                )
            if tool_name == "ontology_query" and arguments.get("queryText") == "boom500":
                raise self.McpError(500, "internal", "内部错误", raw="secret-trace")
            if tool_name == "ontology_query" and arguments.get("queryText") == "crash":
                raise RuntimeError("raw business crash")
            if tool_name == "ontology_query" and arguments.get("queryText") == "watch-control":
                control = current_query_control()
                deadline = threading.Event().wait  # noqa: F841
                for _ in range(500):
                    if control is not None and control.cancelled():
                        self.observed_control_cancelled = True
                        break
                    threading.Event().wait(0.01)
                else:
                    self.observed_control_cancelled = False
                return {"observed": self.observed_control_cancelled}
            if tool_name == "ontology_query" and arguments.get("queryText") == "slow":
                for _ in range(500):
                    if current_query_control() is not None and current_query_control().cancelled():
                        break
                    threading.Event().wait(0.01)
                return {"done": True}
            if tool_name == "ontology_query" and arguments.get("queryText") == "cjk-bulk":
                # 对齐业务 _canonical 16KiB schema 分页预算的真实量级：50 条
                # 典型 CJK 条目紧凑序列化 ≈18.9KiB；SDK 默认 pretty 文本会到
                # ≈38.8KiB（超 pi 20KiB 截断）。
                return {
                    "tool": tool_name,
                    "items": [
                        {
                            "id": f"obj-{index:03d}",
                            "name": f"对象类型{index:03d}",
                            "description": "用于结构发现的中文业务描述字段，" * 2,
                            "properties": [
                                {"id": f"attr-{index}-{sub}", "name": f"属性{sub}", "type": "string"}
                                for sub in range(4)
                            ],
                        }
                        for index in range(50)
                    ],
                    "hasNext": False,
                }
            return {
                "tool": tool_name,
                "userId": str(user.id),
                "ontologyId": ontology_id,
                "note": "中文描述",
            }
        finally:
            self.worker_exited.set()


@pytest.fixture
def stub_service(monkeypatch):
    service = StubService()
    monkeypatch.setattr(mcp_server, "_SERVICE_MODULE", service)
    return service


@pytest.fixture(autouse=True)
def _mcp_session_scope(monkeypatch, db):
    """把 worker 线程会话缝替换为共享测试会话。

    生产 ``_session_scope`` 只走 canonical ``SessionLocal``；测试缝归测试
    代码。TestSession 允许跨线程（check_same_thread=False），worker 对会话
    的使用是串行的，关闭由 ``db`` fixture 统一负责。
    """

    @contextmanager
    def _test_scope():
        yield db

    monkeypatch.setattr(mcp_server, "_session_scope", _test_scope)


@pytest.fixture(autouse=True)
def _reset_auth_throttle():
    yield
    mcp_auth.reset_auth_state()


def _rpc(method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _mcp_headers(password="admin123", **extra):
    headers = {
        "Host": "localhost",
        "X-OpenOntology-Username": "admin",
        "X-OpenOntology-Password": password,
        **JSON_HEADERS,
    }
    headers.update(extra)
    return headers


def test_initialize_handshake_and_trailing_slash(client, admin_user, stub_service):
    body = _rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    )
    for path in (MCP_PATH, MCP_PATH + "/"):
        response = client.post(path, headers=_mcp_headers(), json=body)
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["serverInfo"]["name"] == "openontology"
        assert "tools" in result["capabilities"]


def test_list_tools_whitelist_readonly_and_strict_schema(client, admin_user, stub_service):
    response = client.post(MCP_PATH, headers=_mcp_headers(), json=_rpc("tools/list"))
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert sorted(tool["name"] for tool in tools) == [
        "ontology_query",
        "ontology_query_page",
        "ontology_schema",
    ]
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["inputSchema"]["additionalProperties"] is False


def test_call_tool_rejects_unknown_tool_and_unknown_field(client, admin_user, stub_service):
    response = client.post(
        MCP_PATH,
        headers=_mcp_headers(),
        json=_rpc("tools/call", {"name": "not_a_tool", "arguments": {}}),
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "unknown_tool" in result["content"][0]["text"]

    response = client.post(
        MCP_PATH,
        headers=_mcp_headers(),
        json=_rpc(
            "tools/call",
            {"name": "ontology_query", "arguments": {"queryText": "x", "evil": 1}},
        ),
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    payload = result["content"][0]["text"]
    assert "invalid_arguments" in payload
    # 输入值绝不回显（ValidationError 默认 message 会内嵌实例内容）。
    assert "queryText" not in payload or "参数校验失败" in payload


def test_call_tool_passes_context_and_business_409_detail(
    client, admin_user, stub_service
):
    response = client.post(
        MCP_PATH,
        headers=_mcp_headers(),
        json=_rpc("tools/call", {"name": "ontology_query", "arguments": {"queryText": "q"}}),
    )
    assert response.status_code == 200
    structured = {
        "tool": "ontology_query",
        "userId": admin_user.id,
        "ontologyId": ONTOLOGY_ID,
        "note": "中文描述",
    }
    assert response.json()["result"]["structuredContent"] == structured
    # 文本通道显式紧凑序列化：非 ASCII 不 6 字节转义、无 pretty 空白，
    # 否则 SDK 默认 dumps(indent=2, ensure_ascii=True) 会突破业务 16KiB
    # schema 分页预算。
    import json as _json

    text = response.json()["result"]["content"][0]["text"]
    assert text == _json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    assert "\\u" not in text and "\n" not in text

    # 大体量 CJK 结果：文本通道必须低于 pi 的 20KiB 截断阈值（SDK 默认
    # pretty/ensure_ascii 会把它推到 ≈25KiB），structuredContent 不变。
    bulk = client.post(
        MCP_PATH,
        headers=_mcp_headers(),
        json=_rpc("tools/call", {"name": "ontology_query", "arguments": {"queryText": "cjk-bulk"}}),
    )
    assert bulk.status_code == 200
    bulk_result = bulk.json()["result"]
    bulk_text = bulk_result["content"][0]["text"]
    assert len(bulk_text.encode("utf-8")) > 10 * 1024  # 真实量级，非玩具负载
    assert len(bulk_text.encode("utf-8")) < 20 * 1024
    assert bulk_text == _json.dumps(
        bulk_result["structuredContent"], ensure_ascii=False, separators=(",", ":")
    )

    response = client.post(
        MCP_PATH,
        headers=_mcp_headers(),
        json=_rpc(
            "tools/call",
            {"name": "ontology_query", "arguments": {"queryText": "boom409"}},
        ),
    )
    assert response.status_code == 200
    import json as _json

    payload = _json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["code"] == "release_moved"
    assert payload["currentReleaseId"] == "r-2"
    assert payload["guidance"] == "请重新读取 schema"


def test_call_tool_never_leaks_5xx_details(client, admin_user, stub_service):
    for trigger in ("boom500", "crash"):
        response = client.post(
            MCP_PATH,
            headers=_mcp_headers(),
            json=_rpc(
                "tools/call",
                {"name": "ontology_query", "arguments": {"queryText": trigger}},
            ),
        )
        assert response.status_code == 200
        text = response.json()["result"]["content"][0]["text"]
        assert "secret-trace" not in text
        assert "raw business crash" not in text
        assert "tool_internal_error" in text


def test_missing_credentials_and_wrong_password(client, admin_user, stub_service):
    response = client.post(MCP_PATH, headers={**JSON_HEADERS, "Host": "localhost"}, json=_rpc("tools/list"))
    assert response.status_code == 401
    assert response.json()["code"] == "missing_credentials"
    # 401 不带 WWW-Authenticate：避免误导 MCP 客户端进入 OAuth 分支。
    assert "www-authenticate" not in {key.lower() for key in response.headers}

    response = client.post(MCP_PATH, headers=_mcp_headers(password="wrong"), json=_rpc("tools/list"))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert "wrong" not in response.text


def test_credential_format_limits(client, admin_user, stub_service):
    for username, password in (
        ("a" * 65, "x"),
        ("a", "p" * 129),
        ("a\x01b", "x"),
        ("a", "p\x7f"),
    ):
        response = client.post(
            MCP_PATH,
            headers={
                "Host": "localhost",
                "X-OpenOntology-Username": username,
                "X-OpenOntology-Password": password,
                **JSON_HEADERS,
            },
            json=_rpc("tools/list"),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_credentials_format"
        # 凭据值绝不回显。
        assert password[:4] not in response.text


def test_failure_throttle_locks_after_burst(client, admin_user, stub_service):
    for _ in range(mcp_auth.MAX_AUTH_FAILURES):
        response = client.post(MCP_PATH, headers=_mcp_headers(password="wrong"), json=_rpc("tools/list"))
        assert response.status_code == 401
    response = client.post(MCP_PATH, headers=_mcp_headers(), json=_rpc("tools/list"))
    assert response.status_code == 429
    assert response.json()["code"] == "auth_throttled"
    assert int(response.headers["Retry-After"]) >= 1


def test_auth_concurrency_cap_rejects_without_queueing(client, admin_user, stub_service):
    acquired = []
    try:
        while mcp_auth._auth_slots.acquire(blocking=False):
            acquired.append(True)
        response = client.post(MCP_PATH, headers=_mcp_headers(), json=_rpc("tools/list"))
        assert response.status_code == 429
        assert response.json()["code"] == "auth_busy"
        assert response.headers["Retry-After"] == "1"
    finally:
        for _ in acquired:
            mcp_auth._auth_slots.release()


def test_editor_role_allowed_and_business_authorize_maps_to_http(
    client, editor_user, stub_service
):
    headers = _mcp_headers(password="editor123")
    headers["X-OpenOntology-Username"] = "editor"
    response = client.post(MCP_PATH, headers=headers, json=_rpc("tools/list"))
    assert response.status_code == 200

    # 业务授权拒绝按 McpError.status 映射，只透静态 code/message。
    response = client.post(
        "/api/v2/formal/ontologies/not-this-ontology/mcp",
        headers=headers,
        json=_rpc("tools/list"),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "ontology_not_found"


def test_origin_and_host_guards(client, admin_user, stub_service):
    # 开发通配（默认 cors=*）：仅 loopback origin 放行。
    response = client.post(
        MCP_PATH, headers=_mcp_headers(Origin="http://evil.example"), json=_rpc("tools/list")
    )
    assert response.status_code == 403
    assert response.json()["code"] == "origin_not_allowed"

    response = client.post(
        MCP_PATH,
        headers=_mcp_headers(Origin="http://localhost:5173"),
        json=_rpc("tools/list"),
    )
    assert response.status_code == 200

    # 无 Origin（curl 等）放行；Host 必须在允许清单（显式 origin hostname
    # 或 loopback），DNS rebinding 的任意域名 Host 拒绝。
    response = client.post(MCP_PATH, headers=_mcp_headers(), json=_rpc("tools/list"))
    assert response.status_code == 200

    response = client.post(
        MCP_PATH, headers=_mcp_headers(Host="evil.example"), json=_rpc("tools/list")
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_host"

    response = client.post(
        MCP_PATH, headers=_mcp_headers(Host="127.0.0.1:8000"), json=_rpc("tools/list")
    )
    assert response.status_code == 200


def test_get_method_not_allowed_and_retired_endpoints_stay_404(client, admin_user, stub_service):
    assert client.get(MCP_PATH, headers=_mcp_headers()).status_code == 405
    assert client.post("/mcp", headers=_mcp_headers(), json={}).status_code == 404
    assert client.post("/api-hub/mcp", headers=_mcp_headers(), json={}).status_code == 404
    assert client.post("/api-hub/mcp/system", headers=_mcp_headers(), json={}).status_code == 404


def test_content_length_guard(monkeypatch, client, admin_user, stub_service):
    from starlette.datastructures import Headers

    with pytest.raises(mcp_server._EndpointError) as exc_info:
        mcp_server._validate_content_length(
            Headers({"content-length": str(mcp_server.MAX_REQUEST_BODY_BYTES + 1)})
        )
    assert exc_info.value.status == 413

    async def fake_receive():
        return {"type": "http.request", "body": b"x" * (mcp_server.MAX_REQUEST_BODY_BYTES + 1), "more_body": False}

    wrapped = mcp_server._counting_receive(fake_receive)
    with pytest.raises(mcp_server._RequestBodyTooLarge):
        asyncio.run(wrapped())


def test_tool_timeout_returns_static_error_and_worker_exits(
    monkeypatch, client, admin_user, stub_service
):
    monkeypatch.setattr(mcp_server, "TOOL_EXECUTION_TIMEOUT_SECONDS", 0.2)
    stub_service.worker_exited.clear()
    response = client.post(
        MCP_PATH,
        headers=_mcp_headers(),
        json=_rpc("tools/call", {"name": "ontology_query", "arguments": {"queryText": "slow"}}),
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    import json as _json

    payload = _json.loads(result["content"][0]["text"])
    assert payload["code"] == "query_timeout"
    # 取消标记已置位且线程已安全退出（不 abandon 持有会话的线程）。
    assert stub_service.worker_exited.wait(timeout=5)


def test_disconnect_cancels_query_control_visible_in_worker(
    monkeypatch, client, admin_user, stub_service
):
    from starlette.requests import Request as StarletteRequest

    async def always_disconnected(self):
        return True

    monkeypatch.setattr(StarletteRequest, "is_disconnected", always_disconnected)
    response = client.post(
        MCP_PATH,
        headers=_mcp_headers(),
        json=_rpc(
            "tools/call",
            {"name": "ontology_query", "arguments": {"queryText": "watch-control"}},
        ),
    )
    assert response.status_code == 200
    result = response.json()["result"]
    structured = result.get("structuredContent") or {}
    # 断连 → watcher 取消 QueryControl → worker 内 contextvar 可见取消。
    assert structured.get("observed") is True


def test_transport_reusable_across_repeated_lifespans(admin_user, stub_service):
    # client fixture 每个用例都完整启停一次 lifespan；这里显式再来一轮，
    # 钉死「manager run() 单实例一次性 → 每次 start 新建」的契约。
    for _ in range(2):
        with TestClient(main_app) as repeated:
            response = repeated.post(MCP_PATH, headers=_mcp_headers(), json=_rpc("tools/list"))
            assert response.status_code == 200
            assert len(response.json()["result"]["tools"]) == 3


def test_lifespan_startup_failure_raises_and_releases_transport(
    monkeypatch, admin_user, stub_service
):
    async def broken_start(self):
        raise RuntimeError("boom")

    original_start = mcp_server.McpTransport.start
    monkeypatch.setattr(mcp_server.McpTransport, "start", broken_start)
    with pytest.raises(RuntimeError, match="boom"):
        with TestClient(main_app):
            pass
    assert mcp_server.transport.running is False
    # 修复后同一进程内仍可正常启停（finally 已清理，无残留状态）。
    # 只恢复 start 本身，不能 monkeypatch.undo()——那会连业务桩一起回滚。
    monkeypatch.setattr(mcp_server.McpTransport, "start", original_start)
    with TestClient(main_app) as recovered:
        response = recovered.post(MCP_PATH, headers=_mcp_headers(), json=_rpc("tools/list"))
        assert response.status_code == 200


def test_transport_not_running_returns_503(monkeypatch):
    async def run_case():
        scope = {"type": "http", "method": "POST", "path": MCP_PATH, "headers": []}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        # 强制未启动状态（测试进程里 transport 可能已被其他用例启停）。
        original = mcp_server.transport._manager
        mcp_server.transport._manager = None
        try:
            await mcp_server.transport.handle(scope, receive, send)
        finally:
            mcp_server.transport._manager = original
        return sent

    sent = asyncio.run(run_case())
    assert sent[0]["status"] == 503
