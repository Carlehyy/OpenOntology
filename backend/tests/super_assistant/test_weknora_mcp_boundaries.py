"""托管 WeKnora MCP 行的服务边界单测。

覆盖三条不变量：插件社区（include_builtins=False）不可见也不可触达托管行、
通用更新仅允许启停与执行确认（连接/凭据/受约束登记由 weknora 验证链派生）、
连接测试早期委托 weknora.connections.verify_server 且删除只做本地撤销。
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant import mcp_manifest, mcp_server_service
from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.models import SuperAssistantMcpServer
from app.super_assistant.schemas import McpServerCreate, McpServerUpdate
from app.super_assistant.weknora import connections as weknora_connections


@pytest.fixture
def service_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'weknora-boundary.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, SuperAssistantMcpServer.__table__, CapabilityRevision.__table__],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add(
            User(
                id="owner-w",
                username="owner-w",
                email="owner-w@example.com",
                password_hash="unused",
                role="editor",
            )
        )
        session.commit()
        yield session
    engine.dispose()


def _managed_row(db, **overrides) -> SuperAssistantMcpServer:
    values = {
        "owner_id": "owner-w",
        "name": "platform_weknora_abcd1234",
        "display_name": "知识库",
        "description": "WeKnora 原生只读知识端点",
        "builtin_key": None,
        "transport": "streamable_http",
        "url": "https://weknora.example.com/mcp/ep-1",
        "headers_encrypted": None,
        "header_names": [],
        "command": None,
        "args": [],
        "env_encrypted": None,
        "env_names": [],
        "enabled": False,
        "require_confirmation": True,
        "constrained": False,
        "weknora_endpoint_id": "ep-1",
    }
    values.update(overrides)
    row = SuperAssistantMcpServer(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_community_scope_hides_managed_weknora_rows(service_db):
    normal = mcp_server_service.create_mcp_server(
        service_db,
        "owner-w",
        McpServerCreate(name="normal_tools", transport="stdio", command="node"),
    )
    managed = _managed_row(service_db)

    community = mcp_server_service.list_mcp_servers(
        service_db, "owner-w", include_builtins=False,
    )
    assert [row.id for row in community] == [normal.id]

    assistant = mcp_server_service.list_mcp_servers(
        service_db, "owner-w", include_builtins=True,
    )
    assert {row.id for row in assistant} == {normal.id, managed.id}

    with pytest.raises(mcp_server_service.McpServerNotFoundError):
        mcp_server_service.get_mcp_server(
            service_db, "owner-w", managed.id, include_builtins=False,
        )


@pytest.mark.asyncio
async def test_community_endpoints_cannot_touch_or_verify_managed_rows(
    service_db, monkeypatch,
):
    managed = _managed_row(service_db)

    async def forbidden(*args, **kwargs):
        raise AssertionError("插件社区不得触发 WeKnora 验证链路")

    monkeypatch.setattr(weknora_connections, "verify_server", forbidden)

    with pytest.raises(mcp_server_service.McpServerNotFoundError):
        await mcp_server_service.test_mcp_server(
            service_db, "owner-w", managed.id, include_builtins=False,
        )
    with pytest.raises(mcp_server_service.McpServerNotFoundError):
        mcp_server_service.update_mcp_server(
            service_db, "owner-w", managed.id,
            McpServerUpdate(url="https://evil.example.com/mcp"),
            include_builtins=False,
        )
    with pytest.raises(mcp_server_service.McpServerNotFoundError):
        mcp_server_service.remove_mcp_server(
            service_db, "owner-w", managed.id, include_builtins=False,
        )
    assert service_db.get(SuperAssistantMcpServer, managed.id) is not None


def test_managed_update_restricts_fields_and_gates_enable(service_db):
    managed = _managed_row(service_db)

    # 连接字段、凭据、展示名与受约束登记（admin 手工登记也不行）全部拒绝
    for body in (
        McpServerUpdate(url="https://evil.example.com/mcp"),
        McpServerUpdate(transport="stdio", command="node"),
        McpServerUpdate(headers={"Authorization": "Bearer leaked"}),
        McpServerUpdate(display_name="改名"),
        McpServerUpdate(constrained=True),
    ):
        with pytest.raises(
            mcp_server_service.McpServerValidationError,
            match="仅允许修改启用与执行确认",
        ):
            mcp_server_service.update_mcp_server(
                service_db, "owner-w", managed.id, body, include_builtins=True,
            )
        service_db.rollback()

    # 启用闸门：constrained=False 或未成功验证都拒绝，且给出可行动指引
    with pytest.raises(
        mcp_server_service.McpServerValidationError, match="需先通过连接验证",
    ):
        mcp_server_service.update_mcp_server(
            service_db, "owner-w", managed.id,
            McpServerUpdate(enabled=True), include_builtins=True,
        )
    service_db.rollback()
    managed.last_test_status = "success"
    service_db.commit()
    with pytest.raises(
        mcp_server_service.McpServerValidationError, match="需先通过连接验证",
    ):
        mcp_server_service.update_mcp_server(
            service_db, "owner-w", managed.id,
            McpServerUpdate(enabled=True), include_builtins=True,
        )
    service_db.rollback()

    # 验证链路落库 constrained=True 后：启用/停用/确认开关均放行
    managed.constrained = True
    service_db.commit()
    enabled = mcp_server_service.update_mcp_server(
        service_db, "owner-w", managed.id,
        McpServerUpdate(enabled=True), include_builtins=True,
    )
    assert enabled.enabled is True
    disabled = mcp_server_service.update_mcp_server(
        service_db, "owner-w", managed.id,
        McpServerUpdate(enabled=False), include_builtins=True,
    )
    assert disabled.enabled is False
    confirmed = mcp_server_service.update_mcp_server(
        service_db, "owner-w", managed.id,
        McpServerUpdate(require_confirmation=False), include_builtins=True,
    )
    assert confirmed.require_confirmation is False
    assert confirmed.url == "https://weknora.example.com/mcp/ep-1"
    assert confirmed.constrained is True


@pytest.mark.asyncio
async def test_managed_test_delegates_to_weknora_verify(service_db, monkeypatch):
    managed = _managed_row(service_db)
    calls: list[tuple[str, str]] = []
    request_thread = threading.get_ident()

    def fake_verify(db, owner_id, server_id):
        assert threading.get_ident() != request_thread, "row-lock waits must not block the API event loop"
        calls.append((owner_id, server_id))
        managed.last_test_status = "success"
        managed.last_test_message = "WeKnora 连接已验证，2 个只读工具"
        managed.tool_manifest = [{"name": "search_knowledge"}]
        return managed

    monkeypatch.setattr(weknora_connections, "verify_server", fake_verify)
    ok = await mcp_server_service.test_mcp_server(
        service_db, "owner-w", managed.id, include_builtins=True,
    )
    assert ok.ok is True
    assert ok.tools == [{"name": "search_knowledge"}]
    assert "已验证" in ok.message
    assert calls == [("owner-w", managed.id)]

    def failing_verify(db, owner_id, server_id):
        # 失败安全在 verify_server 内部落库：constrained 复位 + error 状态
        managed.constrained = False
        managed.last_test_status = "error"
        managed.last_test_message = "WeKnora MCP 连接失败，请检查端点 token"
        return managed

    monkeypatch.setattr(weknora_connections, "verify_server", failing_verify)
    failed = await mcp_server_service.test_mcp_server(
        service_db, "owner-w", managed.id, include_builtins=True,
    )
    assert failed.ok is False
    assert failed.tools == []
    assert "连接失败" in failed.message


def test_managed_remove_is_local_revoke_and_delete(service_db):
    from app.super_assistant.mcp_client import namespaced_tool_name

    tools = [{"name": "search_knowledge", "input_schema": {"type": "object"}}]
    managed = _managed_row(
        service_db, tool_manifest=tools, manifest_revision=1,
    )
    mcp_manifest.freeze_manifest(service_db, managed, tools, 1, enabled=True)
    service_db.commit()
    key = namespaced_tool_name(managed.name, "search_knowledge")

    mcp_server_service.remove_mcp_server(
        service_db, "owner-w", managed.id, include_builtins=True,
    )

    assert service_db.get(SuperAssistantMcpServer, managed.id) is None
    # 本地撤销：快照行留存审计、在线授权位关闭；没有上游 DELETE 副作用
    capability = service_db.get(CapabilityRevision, (key, 1))
    assert capability is not None and capability.enabled is False
