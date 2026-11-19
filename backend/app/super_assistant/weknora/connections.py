"""Bind native endpoints to owner-scoped MCP rows and publish only verified read tools."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import re

from sqlalchemy.orm import Session

from app.super_assistant import mcp_client, mcp_manifest
from app.super_assistant.kernel.capability_service import revoke_capability_revisions
from app.super_assistant.models import SuperAssistantMcpServer
from app.super_assistant.weknora.client import WeKnoraClient, WeKnoraError, connection_url, endpoint_id, service_origin
from app.super_assistant.weknora.locking import directory_lock
from app.super_assistant.weknora.policy import scope_snapshot, validate_endpoint, validate_manifest
from app.super_assistant.weknora.status import invalidate


_TOKEN = re.compile(r"mcp_[A-Za-z0-9_-]{16,256}")


def get_connection(db: Session, owner_id: str, identifier: str) -> SuperAssistantMcpServer | None:
    return db.query(SuperAssistantMcpServer).filter_by(
        owner_id=owner_id, weknora_endpoint_id=identifier,
    ).populate_existing().with_for_update().first()


def save_connection(
    db: Session, owner_id: str, endpoint: dict, token: str | None,
) -> SuperAssistantMcpServer:
    identifier = endpoint_id(str(endpoint.get("id") or ""))
    validate_endpoint(endpoint)
    server = get_connection(db, owner_id, identifier)
    url = connection_url(identifier)
    if token is not None and not _TOKEN.fullmatch(token):
        raise WeKnoraError("WEKNORA_TOKEN_INVALID", "请输入此端点的完整 MCP token（mcp_ 开头）", 422)
    if server is None:
        if not token:
            raise WeKnoraError("WEKNORA_TOKEN_REQUIRED", "接入已有 WeKnora 端点需要输入其 token，或确认轮换后接入", 422)
        suffix = identifier.replace("-", "")[:8].lower()
        if not re.fullmatch(r"[0-9a-f]{8}", suffix):
            suffix = hashlib.sha256(identifier.encode()).hexdigest()[:8]
        name = f"platform_weknora_{suffix}"
        if db.query(SuperAssistantMcpServer.id).filter_by(owner_id=owner_id, name=name).first():
            raise WeKnoraError("WEKNORA_NAME_CONFLICT", "此端点的连接名称已被占用，请先处理现有连接", 409)
        server = SuperAssistantMcpServer(
            owner_id=owner_id, name=name, display_name=str(endpoint.get("name") or "WeKnora")[:200],
            description="WeKnora 原生只读知识端点", transport="streamable_http", url=url,
            weknora_endpoint_id=identifier, enabled=True, require_confirmation=True,
        )
        db.add(server)
    if server.url != url or server.transport != "streamable_http":
        raise WeKnoraError("WEKNORA_ORIGIN_CHANGED", "WeKnora 服务地址已变化，请先断开旧连接再重新接入", 409)
    if token is not None:
        try:
            previous = mcp_client.decrypt_headers(server.headers_encrypted)
        except mcp_client.McpClientError:
            previous = {}
        if previous.get("Authorization") != f"Bearer {token}":
            revoke_capability_revisions(db, mcp_manifest.manifest_keys(server), revision=int(server.manifest_revision or 1))
            server.headers_encrypted, server.header_names = mcp_client.encrypt_headers({"Authorization": f"Bearer {token}"})
            server.constrained = False
            server.last_test_status = None
            server.last_test_message = "凭据已更新，正在验证端点"
    if not server.headers_encrypted:
        raise WeKnoraError("WEKNORA_TOKEN_REQUIRED", "此连接尚未保存 token，请重新授权", 422)
    server.display_name = str(endpoint.get("name") or "WeKnora")[:200]
    db.commit()
    db.refresh(server)
    return server


def _endpoint_configuration(identifier: str) -> tuple[dict, list[dict]]:
    with WeKnoraClient() as client:
        return client.endpoint(identifier), client.list("knowledge-bases")


def verify_server(db: Session, owner_id: str, server_id: str) -> SuperAssistantMcpServer:
    """Run in a request worker thread so row-lock waits never block the API loop."""
    server = db.query(SuperAssistantMcpServer).filter_by(
        id=server_id, owner_id=owner_id,
    ).populate_existing().with_for_update().first()
    if server is None or not server.weknora_endpoint_id:
        raise WeKnoraError("WEKNORA_CONNECTION_MISSING", "WeKnora 连接已不存在", 404)
    server.last_tested_at = datetime.now(timezone.utc)
    try:
        endpoint, knowledge_bases = _endpoint_configuration(server.weknora_endpoint_id)
        snapshot = scope_snapshot(endpoint, knowledge_bases)
        if server.url != connection_url(server.weknora_endpoint_id) or server.transport != "streamable_http":
            raise WeKnoraError("WEKNORA_ORIGIN_CHANGED", "WeKnora 服务地址已变化，请断开旧连接后重新接入", 409)
        tools = asyncio.run(mcp_client.discover_tools(
            transport=server.transport, url=server.url,
            headers=mcp_client.decrypt_headers(server.headers_encrypted),
        ))
        validate_manifest(endpoint, tools)
        current_endpoint, current_bases = _endpoint_configuration(server.weknora_endpoint_id)
        if scope_snapshot(current_endpoint, current_bases) != snapshot:
            raise WeKnoraError("WEKNORA_SCOPE_CHANGED", "WeKnora 端点在验证期间变化，请重试验证", 409)
        old_digest = server.manifest_hash
        old_keys = mcp_manifest.manifest_keys(server)
        snapshot["tool_manifest"] = tools
        server.weknora_scope = snapshot
        digest = mcp_manifest.manifest_hash(server, tools)
        revision = int(server.manifest_revision or 1)
        if old_digest and (digest != old_digest or not server.constrained or server.last_test_status != "success"):
            revoke_capability_revisions(db, old_keys, revision=revision)
            revision += 1
        if not old_digest:
            from app.super_assistant.kernel.models import CapabilityRevision
            from sqlalchemy import func

            keys = [mcp_client.namespaced_tool_name(server.name, tool["name"]) for tool in tools]
            previous = db.query(func.max(CapabilityRevision.revision)).filter(CapabilityRevision.key.in_(keys)).scalar()
            if previous is not None:
                revision = max(revision, int(previous) + 1)
        mcp_manifest.freeze_manifest(db, server, tools, revision, enabled=server.enabled)
        server.manifest_revision = revision
        server.manifest_hash = digest
        server.tool_manifest = tools
        server.constrained = True
        server.last_test_status = "success"
        server.last_test_message = f"WeKnora 连接已验证，{len(tools)} 个只读工具"
        server.display_name = str(endpoint.get("name") or "WeKnora")[:200]
        db.commit()
    except (WeKnoraError, mcp_client.McpClientError) as exc:
        message = str(exc) if isinstance(exc, WeKnoraError) else "WeKnora MCP 连接失败，请检查端点 token 或服务状态后重新授权"
        invalidate(db, server, message)
    db.refresh(server)
    return server


def connect_endpoint(
    db: Session, owner_id: str, identifier: str, *, token: str | None = None, rotate_token: bool = False,
) -> SuperAssistantMcpServer:
    if token and rotate_token:
        raise WeKnoraError("WEKNORA_AUTH_AMBIGUOUS", "输入 token 与轮换 token 不能同时选择", 422)
    with directory_lock(db), WeKnoraClient() as client:
        from app.super_assistant.models import SuperAssistantWeKnoraIntegration

        state = db.get(SuperAssistantWeKnoraIntegration, owner_id)
        if state is not None and state.origin != service_origin():
            raise WeKnoraError("WEKNORA_ORIGIN_CHANGED", "WeKnora 服务地址已变化，请先核对原有接入", 409)
        endpoint = client.endpoint(identifier)
        validate_endpoint(endpoint)
        scope_snapshot(endpoint, client.list("knowledge-bases"))
        # Validate the target before a token rotation with external effects.
        connection_url(identifier)
        if rotate_token:
            token = str(client.rotate(identifier).get("token") or "")
        server = save_connection(db, owner_id, endpoint, token)
        if state is None:
            state = SuperAssistantWeKnoraIntegration(owner_id=owner_id, origin=service_origin())
            db.add(state)
        if state.origin != service_origin():
            raise WeKnoraError("WEKNORA_ORIGIN_CHANGED", "WeKnora 服务地址已变化，请先核对原有接入", 409)
        state.initialized_at = state.initialized_at or datetime.now(timezone.utc)
        state.last_error = None
        db.commit()
        return verify_server(db, owner_id, server.id)
