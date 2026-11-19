"""Discover native endpoints and initialize one bounded default knowledge connection."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.super_assistant.models import SuperAssistantMcpServer, SuperAssistantWeKnoraIntegration
from app.super_assistant.weknora.client import WeKnoraClient, WeKnoraError, connection_url, service_origin
from app.super_assistant.weknora.connections import save_connection, verify_server
from app.super_assistant.weknora.status import invalidate
from app.super_assistant.weknora.locking import directory_lock
from app.super_assistant.weknora.policy import ENDPOINT_NAME, KNOWLEDGE_BASE_NAME, READ_TOOLS, same_scope, scope_snapshot, validate_endpoint
from app.super_assistant.weknora.schemas import WeKnoraDirectoryOut, WeKnoraEndpointOut


def integration(db: Session, owner_id: str) -> SuperAssistantWeKnoraIntegration | None:
    return db.get(SuperAssistantWeKnoraIntegration, owner_id, populate_existing=True)


def _integration(db: Session, owner_id: str) -> SuperAssistantWeKnoraIntegration:
    item = integration(db, owner_id)
    if item is None:
        item = SuperAssistantWeKnoraIntegration(owner_id=owner_id, origin=service_origin())
        db.add(item)
        db.commit()
    if item.origin != service_origin():
        raise WeKnoraError("WEKNORA_ORIGIN_CHANGED", "WeKnora 服务地址与已接入地址不一致，请先核对服务配置", 409)
    return item


def _render(db: Session, owner_id: str, endpoints: list[dict], knowledge_bases: list[dict]) -> WeKnoraDirectoryOut:
    state = integration(db, owner_id)
    servers = db.query(SuperAssistantMcpServer).filter(
        SuperAssistantMcpServer.owner_id == owner_id,
        SuperAssistantMcpServer.weknora_endpoint_id.is_not(None),
    ).all()
    by_id = {server.weknora_endpoint_id: server for server in servers}
    references = {str(kb["id"]): {"id": str(kb["id"]), "name": str(kb.get("name") or "知识库")} for kb in knowledge_bases if kb.get("id")}
    rows = []
    for endpoint in endpoints:
        identifier = str(endpoint.get("id") or "")
        server = by_id.pop(identifier, None)
        message = None
        readonly = True
        try:
            validate_endpoint({**endpoint, "enabled": True})
            scope_snapshot({**endpoint, "enabled": True}, knowledge_bases)
        except WeKnoraError as exc:
            message, readonly = str(exc), False
        status = "not_connected"
        if endpoint.get("enabled") is not True:
            status, message = "unavailable", "此 WeKnora 端点已在原生页面停用"
        elif not readonly:
            status = "unavailable"
        elif server:
            try:
                matched = same_scope(server.weknora_scope, endpoint, knowledge_bases)
            except WeKnoraError:
                matched = False
            if not matched:
                status, message = "needs_verification", "端点范围、工具或凭据已变化，请重新验证或授权"
            elif not server.constrained or server.last_test_status != "success":
                status, message = "needs_verification", server.last_test_message or "连接尚未验证，请重新验证"
            elif not server.enabled:
                status = "disabled"
            else:
                status = "ready"
        if server and status in {"unavailable", "needs_verification"} and server.constrained:
            invalidate(db, server, message or "WeKnora 连接需要重新验证")
        selected = endpoint.get("knowledge_base_ids") or []
        rows.append(WeKnoraEndpointOut(
            id=identifier, name=str(endpoint.get("name") or "WeKnora")[:255],
            enabled=endpoint.get("enabled") is True, knowledge_base_ids=selected,
            knowledge_bases=[references[key] for key in (selected or references) if key in references],
            tools=endpoint.get("tools") or [], readonly=readonly,
            server_id=server.id if server else None, connection_status=status, message=message,
        ))
    for identifier, server in by_id.items():
        message = "此端点已在 WeKnora 中删除，请选择其他端点；断开连接不会删除任何知识库"
        if server.constrained:
            invalidate(db, server, message)
        rows.append(WeKnoraEndpointOut(
            id=identifier, name=server.display_name or server.name, enabled=False,
            knowledge_base_ids=(server.weknora_scope or {}).get("knowledge_base_ids", []),
            knowledge_bases=[], tools=[str(tool.get("name") or "") for tool in server.tool_manifest],
            readonly=True, server_id=server.id, connection_status="unavailable", message=message, missing=True,
        ))
    return WeKnoraDirectoryOut(
        endpoints=rows, initialized=bool(state and state.initialized_at),
        message=state.last_error if state else None,
    )


def list_directory(db: Session, owner_id: str) -> WeKnoraDirectoryOut:
    with WeKnoraClient() as client:
        return _render(db, owner_id, client.list("mcp-endpoints"), client.list("knowledge-bases"))


def _model(models: list[dict], kind: str) -> str:
    candidates = [model for model in models if model.get("type") == kind and model.get("status") == "active" and model.get("id")]
    defaults = [model for model in candidates if kind in (model.get("default_for") or [])]
    if not defaults:
        defaults = [model for model in candidates if model.get("is_default") is True]
    selected = defaults if defaults else candidates
    if len(selected) != 1:
        label = "Embedding" if kind == "Embedding" else "聊天/摘要"
        raise WeKnoraError("WEKNORA_MODELS_REQUIRED", f"请先在领域知识原生页面配置并指定默认 {label} 模型，再重试初始化；平台不会使用自身模型", 409)
    return str(selected[0]["id"])


def initialize(db: Session, owner_id: str, *, recreate: bool = False) -> WeKnoraDirectoryOut:
    with directory_lock(db), WeKnoraClient() as client:
        state = _integration(db, owner_id)
        endpoints = client.list("mcp-endpoints")
        knowledge_bases = client.list("knowledge-bases")
        if endpoints:
            state.initialized_at = state.initialized_at or datetime.now(timezone.utc)
            state.last_error = None
            db.commit()
            return _render(db, owner_id, endpoints, knowledge_bases)
        if state.initialized_at and not recreate:
            return _render(db, owner_id, endpoints, knowledge_bases)
        try:
            connection_url("00000000-0000-0000-0000-000000000000")
            matches = [kb for kb in knowledge_bases if kb.get("name") == KNOWLEDGE_BASE_NAME]
            if len(matches) > 1:
                raise WeKnoraError("WEKNORA_KB_AMBIGUOUS", "存在多个 OpenOntology 知识库，请在领域知识页面处理重名后重试", 409)
            kb = matches[0] if matches else None
            models = client.list("models")
            if kb:
                configured = {str(model.get("id")): model for model in models if model.get("status") == "active"}
                if (kb.get("type") != "document"
                        or configured.get(kb.get("embedding_model_id"), {}).get("type") != "Embedding"
                        or configured.get(kb.get("summary_model_id"), {}).get("type") != "KnowledgeQA"):
                    raise WeKnoraError("WEKNORA_MODELS_REQUIRED", "请先在领域知识页面为 OpenOntology 配置可用的 Embedding 与聊天/摘要模型，再重试初始化", 409)
            else:
                embedding, summary = _model(models, "Embedding"), _model(models, "KnowledgeQA")
            # Persist intent before an external write. A lost creation response
            # must lead to discovery/authorization, never an implicit token rotation.
            state.initialized_at = datetime.now(timezone.utc)
            state.last_error = None
            db.commit()
            if kb is None:
                kb = client.create("knowledge-bases", {
                    "name": KNOWLEDGE_BASE_NAME, "type": "document",
                    "description": "OpenOntology 超级助手共享知识库",
                    "embedding_model_id": embedding, "summary_model_id": summary,
                    "indexing_strategy": {"vector_enabled": True, "keyword_enabled": True, "wiki_enabled": False, "graph_enabled": False},
                })
            state.default_knowledge_base_id = str(kb["id"])
            db.commit()
            knowledge_bases = client.list("knowledge-bases")
            # Do not create again if an administrator concurrently added an endpoint.
            endpoints = client.list("mcp-endpoints")
            if endpoints:
                return _render(db, owner_id, endpoints, knowledge_bases)
            endpoint = client.create("mcp-endpoints", {
                "name": ENDPOINT_NAME, "description": "OpenOntology 超级助手只读知识能力",
                "enabled": True, "knowledge_base_ids": [str(kb["id"])],
                "tools": list(READ_TOOLS), "default_agent_id": "",
            })
            state.default_endpoint_id = str(endpoint["id"])
            # The token and external identifier are committed before discovery.
            server = save_connection(db, owner_id, endpoint, str(endpoint.get("token") or ""))
            verify_server(db, owner_id, server.id)
            state.last_error = None
            db.commit()
            return _render(db, owner_id, client.list("mcp-endpoints"), client.list("knowledge-bases"))
        except WeKnoraError as exc:
            state.last_error = str(exc)
            db.commit()
            raise
