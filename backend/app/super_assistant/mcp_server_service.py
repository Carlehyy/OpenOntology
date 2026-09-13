from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.settings.object_storage.service import (
    get_workspace_minio_service,
    minio_tool_manifest,
)
from app.super_assistant import mcp_dev_service
from app.super_assistant.mcp_client import (
    McpClientError,
    decrypt_env,
    decrypt_headers,
    discover_tools,
    encrypt_env,
    encrypt_headers,
    normalize_connection,
)
from app.super_assistant.models import SuperAssistantMcpDevProject, SuperAssistantMcpServer
from app.super_assistant.schemas import McpServerCreate, McpServerUpdate, McpTestOut


class McpServerServiceError(Exception):
    """Base error translated to an HTTP response by each protocol adapter."""


class McpServerNotFoundError(McpServerServiceError):
    pass


class McpServerValidationError(McpServerServiceError):
    pass


class McpServerConflictError(McpServerServiceError):
    pass


class McpServerUnavailableError(McpServerServiceError):
    pass


def get_mcp_server(
    db: Session,
    owner_id: str,
    server_id: str,
    *,
    include_builtins: bool,
) -> SuperAssistantMcpServer:
    query = db.query(SuperAssistantMcpServer).filter(
        SuperAssistantMcpServer.id == server_id,
        SuperAssistantMcpServer.owner_id == owner_id,
    )
    if not include_builtins:
        query = query.filter(SuperAssistantMcpServer.builtin_key.is_(None))
    item = query.first()
    if item is None:
        raise McpServerNotFoundError("MCP Server 不存在")
    return item


def list_mcp_servers(
    db: Session,
    owner_id: str,
    *,
    include_builtins: bool,
) -> list[SuperAssistantMcpServer]:
    query = db.query(SuperAssistantMcpServer).filter(
        SuperAssistantMcpServer.owner_id == owner_id,
    )
    if not include_builtins:
        query = query.filter(SuperAssistantMcpServer.builtin_key.is_(None))
    return query.order_by(SuperAssistantMcpServer.updated_at.desc()).all()


def create_mcp_server(
    db: Session,
    owner_id: str,
    body: McpServerCreate,
) -> SuperAssistantMcpServer:
    try:
        transport, url, command, args = normalize_connection(
            transport=body.transport,
            url=body.url,
            command=body.command,
            args=body.args,
        )
        encrypted, names = encrypt_headers(body.headers)
        env_encrypted, env_names = encrypt_env(body.env)
    except McpClientError as exc:
        raise McpServerValidationError(str(exc)) from exc

    item = SuperAssistantMcpServer(
        owner_id=owner_id,
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        builtin_key=None,
        transport=transport,
        url=url,
        headers_encrypted=encrypted,
        header_names=names,
        command=command,
        args=args,
        env_encrypted=env_encrypted,
        env_names=env_names,
        enabled=body.enabled,
        require_confirmation=body.require_confirmation,
    )
    try:
        db.add(item)
        db.commit()
        db.refresh(item)
        return item
    except IntegrityError as exc:
        db.rollback()
        raise McpServerConflictError("同名 MCP Server 已存在") from exc


def update_mcp_server(
    db: Session,
    owner_id: str,
    server_id: str,
    body: McpServerUpdate,
    *,
    include_builtins: bool,
) -> SuperAssistantMcpServer:
    item = get_mcp_server(
        db,
        owner_id,
        server_id,
        include_builtins=include_builtins,
    )
    try:
        if item.builtin_key and body.model_fields_set - {
            "enabled",
            "require_confirmation",
        }:
            raise McpClientError(
                "平台内置 MCP 仅允许修改启用和执行确认设置"
            )
        if item.dev_project_id and body.model_fields_set - {
            "enabled",
            "require_confirmation",
        }:
            raise McpClientError(
                "自研 MCP 的连接与工具由开发页管理：这里仅允许修改启用和执行确认设置"
            )

        connection_changed = any(
            value is not None
            for value in (body.transport, body.url, body.command, body.args)
        )
        if connection_changed:
            transport, url, command, args = normalize_connection(
                transport=body.transport or item.transport,
                url=body.url if body.url is not None else item.url,
                command=body.command if body.command is not None else item.command,
                args=body.args if body.args is not None else item.args,
            )
            item.transport = transport
            item.url = url
            item.command = command
            item.args = args
            item.tool_manifest = []
            item.last_test_status = None
            item.last_test_message = None
        if body.headers is not None:
            item.headers_encrypted, item.header_names = encrypt_headers(body.headers)
            item.tool_manifest = []
            item.last_test_status = None
            item.last_test_message = None
        if body.env is not None:
            item.env_encrypted, item.env_names = encrypt_env(body.env)
            item.tool_manifest = []
            item.last_test_status = None
            item.last_test_message = None
        if body.display_name is not None:
            item.display_name = body.display_name
        if body.description is not None:
            item.description = body.description
        if body.enabled is not None:
            item.enabled = body.enabled
        if body.require_confirmation is not None:
            item.require_confirmation = body.require_confirmation
        db.commit()
        db.refresh(item)
        return item
    except McpClientError as exc:
        db.rollback()
        raise McpServerValidationError(str(exc)) from exc


def remove_mcp_server(
    db: Session,
    owner_id: str,
    server_id: str,
    *,
    include_builtins: bool,
) -> None:
    item = get_mcp_server(
        db,
        owner_id,
        server_id,
        include_builtins=include_builtins,
    )
    # 自研 MCP 与开发项目一一对应：删除任一侧都整体清理（版本随之删除）
    if item.dev_project_id:
        project = db.query(SuperAssistantMcpDevProject).filter(
            SuperAssistantMcpDevProject.id == item.dev_project_id,
            SuperAssistantMcpDevProject.owner_id == owner_id,
        ).first()
        if project is not None:
            mcp_dev_service.delete_project_row(db, project)
    db.delete(item)
    db.commit()


async def test_mcp_server(
    db: Session,
    owner_id: str,
    server_id: str,
    *,
    include_builtins: bool,
) -> McpTestOut:
    item = get_mcp_server(
        db,
        owner_id,
        server_id,
        include_builtins=include_builtins,
    )
    item.last_tested_at = datetime.now(timezone.utc)
    try:
        if item.dev_project_id is not None:
            # 自研 MCP：测试 = 重跑发布闸门（逐工具样例真实执行）
            return mcp_dev_service.verify_published_server(db, owner_id, item)
        if item.builtin_key == "minio":
            get_workspace_minio_service().status()
            tools = minio_tool_manifest()
        else:
            tools = await discover_tools(
                transport=item.transport,
                url=item.url,
                headers=decrypt_headers(item.headers_encrypted),
                command=item.command,
                args=item.args,
                env=decrypt_env(item.env_encrypted),
            )
        item.tool_manifest = tools
        item.last_test_status = "success"
        item.last_test_message = f"连接成功，发现 {len(tools)} 个工具"
        db.commit()
        return McpTestOut(ok=True, message=item.last_test_message, tools=tools)
    except Exception as exc:
        item.tool_manifest = []
        item.last_test_status = "error"
        item.last_test_message = str(exc)[:500]
        db.commit()
        return McpTestOut(ok=False, message=str(exc), tools=[])


def install_platform_minio_mcp(
    db: Session,
    owner_id: str,
    *,
    workspace_minio_service_factory=get_workspace_minio_service,
    minio_tool_manifest_fn=minio_tool_manifest,
) -> SuperAssistantMcpServer:
    try:
        workspace_minio_service_factory().status()
    except Exception as exc:
        raise McpServerUnavailableError(
            f"平台 MinIO 当前不可用：{exc}",
        ) from exc

    item = db.query(SuperAssistantMcpServer).filter(
        SuperAssistantMcpServer.owner_id == owner_id,
        SuperAssistantMcpServer.builtin_key == "minio",
    ).first()
    if not item:
        name_taken = db.query(SuperAssistantMcpServer).filter(
            SuperAssistantMcpServer.owner_id == owner_id,
            SuperAssistantMcpServer.name == "platform_minio",
        ).first()
        if name_taken:
            raise McpServerConflictError(
                "已有同名 platform_minio MCP，请先重命名或删除",
            )
        item = SuperAssistantMcpServer(
            owner_id=owner_id,
            name="platform_minio",
            builtin_key="minio",
            transport="streamable_http",
            url="builtin://minio",
            headers_encrypted=None,
            header_names=[],
            command=None,
            args=[],
            env_encrypted=None,
            env_names=[],
            enabled=True,
            require_confirmation=True,
        )
        db.add(item)
    item.tool_manifest = minio_tool_manifest_fn()
    item.last_test_status = "success"
    item.last_test_message = (
        f"平台内置连接成功，发现 {len(item.tool_manifest)} 个工具"
    )
    item.last_tested_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return item
