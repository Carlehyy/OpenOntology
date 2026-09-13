"""Connector/Capability 边界。

Agent 和插件只通过这个窄接口进入 Kernel；CapabilityRevision 是不可变快照，
运行中的 Run 永远使用创建时解析出的 revision，不随目录热更新。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable

import httpx

from .contracts import ContractError


class TrustLevel(StrEnum):
    PLATFORM = "platform"
    VERIFIED = "verified"
    USER_UNTRUSTED = "user_untrusted"


class SessionPolicy(StrEnum):
    NEW_ONLY = "new_only"
    RESUMABLE = "resumable"
    STATELESS = "stateless"


@dataclass(frozen=True, slots=True)
class ContextRequirements:
    required_refs: tuple[str, ...] = ()
    optional_refs: tuple[str, ...] = ()
    max_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AgentDescriptor:
    agent_id: str
    key: str
    revision: int
    transport: str
    capabilities: tuple[str, ...] = ()
    context_requirements: ContextRequirements = field(default_factory=ContextRequirements)
    binding_requirements: tuple[str, ...] = ()
    session_policy: SessionPolicy = SessionPolicy.STATELESS
    supports_stream: bool = False
    supports_cancel: bool = False
    supports_push: bool = False
    supports_query_status: bool = False
    supports_artifact: bool = False

    def __post_init__(self) -> None:
        if not self.agent_id or not self.key or self.revision < 1:
            raise ContractError("AgentDescriptor requires stable id/key and positive revision")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ContractError("AgentDescriptor capabilities must be unique")
        if self.supports_push and self.transport not in {"rap.v1", "webhook", "native"}:
            raise ContractError("supports_push requires a push-capable transport")


@runtime_checkable
class AgentConnector(Protocol):
    def descriptor(self) -> AgentDescriptor: ...

    async def invoke(self, *, run_id: str, call_id: str, input_ref: str, deadline) -> Mapping[str, Any]: ...

    async def cancel(self, *, remote_task_ref: str) -> Mapping[str, Any]: ...

    async def query_status(self, *, remote_task_ref: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class BindingSnapshot:
    binding_mode: str
    owner_id: str
    ontology_id: str | None = None
    draft_version_id: str | None = None
    lifecycle: str | None = None
    write_permission_hash: str | None = None


class DomainBindingResolver:
    """委派调用的绑定门；direct UI 保持可为空/current-release。"""

    def resolve(self, *, owner_id: str, binding_mode: str, binding: Mapping[str, Any] | None) -> BindingSnapshot:
        values = dict(binding or {})
        if binding_mode == "delegated":
            required = {"ontology_id", "draft_version_id", "lifecycle", "write_permission_hash"}
            if required - values.keys() or values.get("lifecycle") != "editing":
                raise ContractError("delegated binding requires ontology draft editing snapshot")
            if not values.get("write_permission_hash"):
                raise ContractError("delegated binding requires write permission hash")
            return BindingSnapshot(
                binding_mode="delegated", owner_id=owner_id,
                ontology_id=str(values["ontology_id"]), draft_version_id=str(values["draft_version_id"]),
                lifecycle="editing", write_permission_hash=str(values["write_permission_hash"]),
            )
        if binding_mode in {"direct_ui", "legacy"}:
            return BindingSnapshot(
                binding_mode=binding_mode, owner_id=owner_id,
                ontology_id=str(values["ontology_id"]) if values.get("ontology_id") else None,
                draft_version_id=str(values["draft_version_id"]) if values.get("draft_version_id") else None,
                lifecycle=values.get("lifecycle"),
            )
        raise ContractError(f"unknown binding_mode: {binding_mode}")


class CapabilityRegistry:
    """进程内 immutable capability registry；插件不能在响应中扩展目录。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], tuple[AgentDescriptor, TrustLevel]] = {}

    def register(self, descriptor: AgentDescriptor, trust_level: TrustLevel) -> None:
        key = (descriptor.key, descriptor.revision)
        if key in self._entries:
            if self._entries[key] != (descriptor, trust_level):
                raise ContractError("capability revision is immutable")
            return
        self._entries[key] = (descriptor, trust_level)

    def get(self, key: str, revision: int) -> tuple[AgentDescriptor, TrustLevel]:
        try:
            return self._entries[(key, revision)]
        except KeyError as exc:
            raise ContractError("unknown capability revision") from exc

    def snapshot(self) -> tuple[tuple[AgentDescriptor, TrustLevel], ...]:
        return tuple(self._entries.values())


class ConnectorRegistry:
    """Runtime connector directory keyed by the immutable AgentDescriptor.

    The capability registry remains the source of truth for what a Run was
    authorised to call.  This directory only resolves the already-selected
    revision to a transport implementation; registering a connector never
    mutates a descriptor or the capability snapshot.
    """

    def __init__(self) -> None:
        self._connectors: dict[tuple[str, int], AgentConnector] = {}

    def register(self, connector: AgentConnector) -> None:
        descriptor = connector.descriptor()
        key = (descriptor.key, descriptor.revision)
        existing = self._connectors.get(key)
        if existing is not None and existing is not connector:
            raise ContractError("connector revision is already registered")
        self._connectors[key] = connector

    def resolve(self, key: str, revision: int) -> AgentConnector:
        try:
            return self._connectors[(key, revision)]
        except KeyError as exc:
            raise ContractError("unknown connector revision") from exc

    async def invoke(self, *, key: str, revision: int, run_id: str, call_id: str, input_ref: str, deadline) -> Mapping[str, Any]:
        return await self.resolve(key, revision).invoke(
            run_id=run_id, call_id=call_id, input_ref=input_ref, deadline=deadline,
        )


@dataclass(frozen=True, slots=True)
class RemoteAgentHttpConnector:
    """RAP-compatible HTTP connector backed by the existing remote-agent API.

    It deliberately exposes only the capabilities the legacy endpoint can
    prove.  Cancellation and status polling remain unsupported until the
    remote endpoint advertises those operations; callers therefore enter the
    kernel's normal ``outcome_unknown``/reconciliation path instead of
    guessing a result.
    """

    agent_id: str
    key: str
    endpoint: str
    token: str = ""
    timeout_seconds: int = 120
    revision: int = 1
    transport: Any = field(default=None, compare=False, repr=False)

    def descriptor(self) -> AgentDescriptor:
        return AgentDescriptor(
            agent_id=self.agent_id,
            key=self.key,
            revision=self.revision,
            transport="rap.v1",
            session_policy=SessionPolicy.RESUMABLE,
            supports_stream=False,
            supports_cancel=False,
            supports_push=False,
            supports_query_status=False,
            supports_artifact=False,
        )

    @staticmethod
    def _input(input_ref: str) -> dict[str, Any]:
        try:
            value = json.loads(input_ref)
        except (TypeError, ValueError):
            return {"message": str(input_ref), "session_ref": None}
        if not isinstance(value, dict) or not isinstance(value.get("message"), str):
            raise ContractError("remote connector input_ref must contain a message")
        return {"message": value["message"], "session_ref": value.get("session_ref")}

    async def invoke(self, *, run_id: str, call_id: str, input_ref: str, deadline) -> Mapping[str, Any]:
        body = self._input(input_ref)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        timeout = max(1.0, float(self.timeout_seconds))
        if isinstance(deadline, datetime):
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            timeout = max(1.0, min(timeout, remaining))
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(self.endpoint, headers=headers, json=body)
            response.raise_for_status()
            value = response.json()
        if not isinstance(value, dict):
            raise ContractError("remote connector response must be an object")
        status = str(value.get("status") or "failed").lower()
        if status not in {"answered", "failed", "cancelled"}:
            status = "failed"
        return {
            "run_id": run_id,
            "call_id": call_id,
            "status": status,
            "content": str(value.get("content") or ""),
            "session_ref": value.get("session_ref"),
            "note": str(value.get("note") or "")[:2000],
            "provider_status": status,
        }

    async def cancel(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}

    async def query_status(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}


@dataclass(frozen=True, slots=True)
class McpToolConnector:
    """A single namespaced MCP tool exposed through the kernel boundary."""

    server_id: str
    server_name: str
    tool_name: str
    transport: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    revision: int = 1

    @property
    def namespaced_key(self) -> str:
        from app.super_assistant.mcp_client import namespaced_tool_name

        return namespaced_tool_name(self.server_name, self.tool_name)

    def descriptor(self) -> AgentDescriptor:
        return AgentDescriptor(
            agent_id=f"mcp:{self.server_id}:{self.tool_name}",
            key=self.namespaced_key,
            revision=self.revision,
            transport="mcp",
            session_policy=SessionPolicy.STATELESS,
            supports_stream=False,
            supports_cancel=False,
            supports_push=False,
            supports_query_status=False,
            supports_artifact=False,
        )

    async def invoke(self, *, run_id: str, call_id: str, input_ref: str, deadline) -> Mapping[str, Any]:
        try:
            value = json.loads(input_ref or "{}")
        except (TypeError, ValueError):
            value = {}
        arguments = value.get("arguments") if isinstance(value, dict) else {}
        if not isinstance(arguments, dict):
            raise ContractError("MCP connector arguments must be an object")
        from app.super_assistant.mcp_client import call_tool

        content = await call_tool(
            transport=self.transport,
            url=self.url,
            headers=dict(self.headers),
            tool_name=self.tool_name,
            arguments=arguments,
            command=self.command,
            args=list(self.args),
            env=dict(self.env),
        )
        return {
            "run_id": run_id,
            "call_id": call_id,
            "status": "answered",
            "content": str(content),
            "provider_status": "completed",
        }

    async def cancel(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}

    async def query_status(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}
