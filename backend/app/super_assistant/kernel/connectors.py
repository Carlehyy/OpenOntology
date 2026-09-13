"""Connector/Capability 边界。

Agent 和插件只通过这个窄接口进入 Kernel；CapabilityRevision 是不可变快照，
运行中的 Run 永远使用创建时解析出的 revision，不随目录热更新。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable

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

