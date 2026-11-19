"""Knowledge endpoint policy, independent of MCP transports and database sessions."""
from __future__ import annotations

from app.super_assistant.weknora.client import WeKnoraError, endpoint_id, service_origin


READ_TOOLS = (
    "list_knowledge_bases", "search_knowledge", "grep_chunks", "list_documents",
    "read_document", "wiki_search", "wiki_read_page", "wiki_index",
)
KNOWLEDGE_BASE_NAME = "OpenOntology"
ENDPOINT_NAME = "OpenOntology"


def validate_endpoint(endpoint: dict) -> None:
    endpoint_id(str(endpoint.get("id") or ""))
    if endpoint.get("enabled") is not True:
        raise WeKnoraError("WEKNORA_ENDPOINT_DISABLED", "此 WeKnora 端点已停用，请在领域知识页面启用后重新验证", 409)
    tools = endpoint.get("tools")
    if not isinstance(tools, list) or not tools or any(not isinstance(tool, str) or tool not in READ_TOOLS for tool in tools):
        raise WeKnoraError("WEKNORA_TOOLS_UNSAFE", "此端点包含非只读或未知工具，请在领域知识页面仅保留只读知识工具", 409)
    if "list_knowledge_bases" not in tools:
        raise WeKnoraError("WEKNORA_SCOPE_TOOL_REQUIRED", "此端点需要保留 list_knowledge_bases 工具，以便平台核验知识库范围", 409)
    scope = endpoint.get("knowledge_base_ids")
    if not isinstance(scope, list) or any(not isinstance(identifier, str) or not identifier for identifier in scope):
        raise WeKnoraError("WEKNORA_SCOPE_INVALID", "WeKnora 知识库范围无效，请在领域知识页面检查", 409)


def scope_snapshot(endpoint: dict, knowledge_bases: list[dict]) -> dict:
    validate_endpoint(endpoint)
    selected = set(endpoint["knowledge_base_ids"])
    available = {str(item.get("id")) for item in knowledge_bases if item.get("id")}
    if selected - available:
        raise WeKnoraError("WEKNORA_KNOWLEDGE_MISSING", "端点绑定的知识库已不存在，请在领域知识页面修正范围后重新验证", 409)
    return {
        "origin": service_origin(), "endpoint_id": endpoint_id(str(endpoint["id"])),
        "knowledge_base_ids": sorted(selected),
        "resolved_knowledge_base_ids": sorted(selected or available),
        "tools": sorted(set(endpoint["tools"])),
        "default_agent_id": str(endpoint.get("default_agent_id") or ""),
        "token_hint": str(endpoint.get("token_hint") or ""),
    }


def same_scope(snapshot: dict | None, endpoint: dict, knowledge_bases: list[dict]) -> bool:
    if not snapshot:
        return False
    expected = {key: value for key, value in snapshot.items() if key != "tool_manifest"}
    return expected == scope_snapshot(endpoint, knowledge_bases)


def validate_manifest(endpoint: dict, tools: list[dict]) -> None:
    names = [str(item.get("name") or "") for item in tools]
    if len(names) != len(set(names)) or set(names) != set(endpoint["tools"]):
        raise WeKnoraError("WEKNORA_MANIFEST_CHANGED", "WeKnora 实际工具清单与端点配置不一致，请重新验证", 409)
    if any(name not in READ_TOOLS for name in names):
        raise WeKnoraError("WEKNORA_TOOLS_UNSAFE", "WeKnora 端点只允许只读知识工具", 409)
