from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api_hub import config as api_hub_config
from app.api_hub import db as api_hub_db
from app.auth.models import User
from app.community import mcp_export
from app.shared.database import Base
from app.super_assistant import mcp_server_service
from app.super_assistant.mcp_client import encrypt_headers
from app.super_assistant.models import SuperAssistantMcpServer
from app.super_assistant.schemas import McpServerCreate


@pytest.fixture
def databases(tmp_path, monkeypatch):
    monkeypatch.setattr(api_hub_config, "DB_PATH", tmp_path / "api_hub.db")
    api_hub_db.init_db()
    engine = create_engine(f"sqlite:///{tmp_path / 'community.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, SuperAssistantMcpServer.__table__],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        db.add(
            User(
                id="owner-1",
                username="owner-one",
                email="owner-one@example.com",
                password_hash="unused",
                role="editor",
            ),
        )
        db.commit()
        yield db
    engine.dispose()


def _http_server(db, **overrides) -> SuperAssistantMcpServer:
    encrypted, names = encrypt_headers({"x-open-operator": "{{operator}}"})
    server = SuperAssistantMcpServer(
        owner_id="owner-1",
        name="dmp-mcp-server",
        display_name="DMP 数据服务",
        description="数据管理平台 MCP",
        builtin_key=None,
        transport="streamable_http",
        url="https://mcpgateway.example.com/mcp/abc",
        headers_encrypted=encrypted,
        header_names=names,
        command=None,
        args=[],
        env_encrypted=None,
        env_names=[],
        tool_manifest=[
            {
                "name": "search_dmp",
                "description": "检索 DMP 数据",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                },
            },
            {
                "name": "list_tables",
                "description": "列出可用表",
                "input_schema": {"type": "object", "properties": {}},
            },
        ],
        last_test_status="success",
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    return server


def test_json_rpc_body_uses_schema_defaults_and_placeholders():
    body = json.loads(
        mcp_export.json_rpc_body(
            "search_dmp",
            {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "flags": {"type": "array"},
                },
            },
        )
    )
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "search_dmp"
    assert body["params"]["arguments"] == {
        "keyword": "",
        "limit": 10,
        "flags": [],
    }


def test_export_creates_http_interfaces_with_decrypted_headers(databases):
    server = _http_server(databases)
    result = mcp_export.export_server_tools(
        databases,
        "owner-1",
        server.id,
        ["search_dmp"],
    )
    assert [item["tool"] for item in result.created] == ["search_dmp"]
    assert result.skipped == []
    with api_hub_db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM interfaces WHERE id = ?",
            (result.created[0]["id"],),
        ).fetchone()
    assert row["name"] == "DMP 数据服务 · search_dmp"
    assert row["group_name"] == mcp_export.EXPORT_GROUP_NAME
    assert row["method"] == "POST"
    assert row["url"] == "https://mcpgateway.example.com/mcp/abc"
    assert row["body_type"] == "json"
    assert "tools/call" in row["body_content"]
    assert '"key": "x-open-operator", "value": "{{operator}}"' in row["headers"]
    assert row["open_enabled"] == 0
    assert row["http_enabled"] == 0
    assert "search_dmp" in row["description"]


def test_export_skips_existing_name_and_reports(databases):
    server = _http_server(databases)
    first = mcp_export.export_server_tools(
        databases, "owner-1", server.id, ["search_dmp"],
    )
    second = mcp_export.export_server_tools(
        databases, "owner-1", server.id, ["search_dmp", "list_tables"],
    )
    assert [item["tool"] for item in second.created] == ["list_tables"]
    assert second.skipped == [
        {
            "tool": "search_dmp",
            "reason": f"同名接口「{first.created[0]['name']}」已存在，已跳过",
        }
    ]


def test_export_bridges_stdio_and_sse_without_leaking_credentials(databases):
    server = _http_server(databases)
    server.transport = "stdio"
    server.url = ""
    server.command = "npx"
    server.args = ["-y", "worldbank-mcp"]
    databases.commit()
    result = mcp_export.export_server_tools(
        databases, "owner-1", server.id, ["search_dmp"],
    )
    assert [item["tool"] for item in result.created] == ["search_dmp"]
    with api_hub_db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM interfaces WHERE id = ?",
            (result.created[0]["id"],),
        ).fetchone()
    assert row["url"] == f"mcp-bridge://{server.id}/search_dmp"
    assert row["method"] == "POST"
    assert row["body_type"] == "json"
    assert "tools/call" in row["body_content"]
    assert "平台桥接" in row["description"]
    # 桥接接口不携带 MCP 凭据：请求头只有协议所需的 Content-Type/Accept
    assert "x-open-operator" not in row["headers"]
    assert '"key": "Content-Type"' in row["headers"]

    server.transport = "sse"
    server.url = "https://sse.example.com/mcp"
    databases.commit()
    sse_result = mcp_export.export_server_tools(
        databases, "owner-1", server.id, ["list_tables"],
    )
    assert [item["tool"] for item in sse_result.created] == ["list_tables"]
    with api_hub_db.get_conn() as conn:
        sse_row = conn.execute(
            "SELECT url FROM interfaces WHERE id = ?",
            (sse_result.created[0]["id"],),
        ).fetchone()
    assert sse_row["url"] == f"mcp-bridge://{server.id}/list_tables"


def test_export_rejects_unknown_tools(databases):
    server = _http_server(databases)
    with pytest.raises(
        mcp_server_service.McpServerValidationError,
        match="未在工具清单中找到",
    ):
        mcp_export.export_server_tools(
            databases, "owner-1", server.id, ["missing_tool"],
        )


def test_export_requires_tested_manifest(databases):
    encrypted, names = encrypt_headers({})
    empty = SuperAssistantMcpServer(
        owner_id="owner-1",
        name="fresh-server",
        display_name="Fresh",
        description="未测试",
        builtin_key=None,
        transport="streamable_http",
        url="https://fresh.example.com/mcp",
        headers_encrypted=encrypted,
        header_names=names,
        command=None,
        args=[],
        env_encrypted=None,
        env_names=[],
        tool_manifest=[],
    )
    databases.add(empty)
    databases.commit()
    databases.refresh(empty)
    with pytest.raises(
        mcp_server_service.McpServerValidationError,
        match="尚未发现工具",
    ):
        mcp_export.export_server_tools(
            databases, "owner-1", empty.id, ["any"],
        )


def test_create_mcp_server_display_fields_default_empty(databases):
    body = McpServerCreate(
        name="no_display",
        transport="streamable_http",
        url="https://example.com/mcp",
    )
    assert body.display_name == ""
    assert body.description == ""


def _weknora_server(db) -> SuperAssistantMcpServer:
    server = SuperAssistantMcpServer(
        owner_id="owner-1",
        name="platform_weknora_abcd1234",
        display_name="知识库",
        description="WeKnora 原生只读知识端点",
        builtin_key=None,
        transport="streamable_http",
        url="https://weknora.example.com/mcp/ep-1",
        headers_encrypted=None,
        header_names=[],
        command=None,
        args=[],
        env_encrypted=None,
        env_names=[],
        tool_manifest=[
            {"name": "search_knowledge", "description": "检索知识", "input_schema": {"type": "object"}},
        ],
        last_test_status="success",
        weknora_endpoint_id="ep-1",
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    return server


def test_export_hides_managed_weknora_rows(databases):
    managed = _weknora_server(databases)
    # include_builtins=False 的服务过滤已把托管行挡在社区之外
    with pytest.raises(
        mcp_server_service.McpServerNotFoundError,
        match="MCP Server 不存在",
    ):
        mcp_export.export_server_tools(
            databases, "owner-1", managed.id, ["search_knowledge"],
        )


def test_export_explicitly_rejects_managed_weknora_rows(databases, monkeypatch):
    managed = _weknora_server(databases)
    # 纵深防御：即便服务过滤回归（绕过 include_builtins 拿到行），
    # 导出层仍按 weknora_endpoint_id 绑定列显式拒绝，不信名称。
    monkeypatch.setattr(
        mcp_server_service,
        "get_mcp_server",
        lambda db, owner_id, server_id, *, include_builtins: managed,
    )
    with pytest.raises(
        mcp_server_service.McpServerValidationError,
        match="托管 WeKnora MCP 不支持转接口",
    ):
        mcp_export.export_server_tools(
            databases, "owner-1", managed.id, ["search_knowledge"],
        )
    with api_hub_db.get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM interfaces").fetchone()[0]
    assert count == 0
