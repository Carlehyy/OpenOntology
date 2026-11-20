"""Connector/Capability 边界。

Agent 和插件只通过这个窄接口进入 Kernel；CapabilityRevision 是不可变快照，
运行中的 Run 永远使用创建时解析出的 revision，不随目录热更新。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

import httpx

from .contracts import ContractError

logger = logging.getLogger(__name__)


class TrustLevel(StrEnum):
    PLATFORM = "platform"
    VERIFIED = "verified"
    USER_UNTRUSTED = "user_untrusted"


# Multica tool targets routed through the service-layer channel.  Calls on
# these targets can bind an immutable Multica profile revision as their
# execution source.
MULTICA_TOOL_KEYS = frozenset({"multica_list_agents", "multica_list_tasks", "multica_create_task"})

_MULTICA_REF_SOURCE_KEYS = ("profile_revision_id", "base_url", "workspace_id")


def multica_ref_metadata(remote_task_ref: str | None) -> dict[str, str] | None:
    """Profile-source metadata embedded in a Multica ``remote_task_ref``.

    Returns ``None`` for legacy references (or unparseable payloads) that carry
    no unique source; callers decide their own fail-closed policy for those.
    Extra keys are ignored by the service-side parser, so enrichment stays
    backward compatible.
    """
    if not remote_task_ref:
        return None
    try:
        value = json.loads(remote_task_ref)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    metadata = {key: str(value[key]) for key in _MULTICA_REF_SOURCE_KEYS if value.get(key)}
    return metadata or None


def multica_config_for(
    db, owner_id: str, revision_id: str | None = None, *,
    base_url: str | None = None, workspace_id: str | None = None,
):
    """Resolve the Multica source a Call/step must use, or ``None`` (fail closed).

    - ``revision_id``: the immutable bound source via ``config_for_revision``.
      Retired revisions and soft-deleted profiles still resolve; revoked or
      unusable revisions do not, and the active profile is never borrowed.
    - ``base_url`` + ``workspace_id``: endpoint/workspace evidence carried by
      an old reference resolves through ``config_for_source`` — the latest
      usable unrevoked revision of exactly that source; credential ambiguity
      raises instead of guessing.
    - otherwise: the legacy ``active_config`` row (``None`` when unconfigured).

    Service refusals are translated to ``None`` so every caller (kernel
    closures, autoloop worker/watchdog/routing) fails closed instead of
    silently switching to a different source.
    """
    from app.super_assistant import multica_service

    try:
        if revision_id:
            return multica_service.config_for_revision(db, owner_id, str(revision_id))
        if base_url and workspace_id:
            return multica_service.config_for_source(
                db, owner_id, base_url=base_url, workspace_id=workspace_id,
            )
        return multica_service.active_config(db, owner_id)
    except multica_service.MulticaServiceError:
        return None


def multica_token(config) -> str:
    """Usable PAT for a live config row or an immutable revision snapshot."""
    from app.super_assistant import multica_service

    return multica_service.decrypt_token(config)


def stable_manifest_hash(value: dict) -> str:
    """The one manifest-digest formula for frozen capability snapshots.

    Lives in the lowest kernel layer so both the Multica digest below and
    ``runtime._external_config_hash`` (MCP/remote-agent manifests) share a
    single implementation without any resolver-layer import cycle.  The exact
    serialization is contract: rows already persisted by earlier code must
    keep matching byte for byte.
    """
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def multica_config_digest_value(config) -> str:
    """Stable capability digest for a live config row or revision snapshot.

    Hashes exactly the historical multica manifest fields — kind, endpoint,
    workspace, stored credential — with raw column values, so capability
    snapshots persisted by pre-upgrade code keep matching byte for byte.
    The digest covers only these fields: it is not proof of workspace_name,
    ``enabled`` or any other projection column.  Both config rows and
    revision snapshots expose the same three attributes.
    """
    return stable_manifest_hash({
        "kind": "multica",
        "base_url": config.base_url,
        "workspace_id": config.workspace_id,
        "token": config.token_encrypted,
    })


def verify_multica_ref_source(remote_task_ref: str | None, config, revision_id: str | None = None) -> None:
    """Fail closed when a provider reference belongs to a different source.

    Cross-workspace identity check: once a reference records the profile it was
    created under, a later query/cancel must resolve that exact profile.  A
    mismatch raises instead of guessing, so an issue identity can never be
    replayed against a workspace it does not belong to.
    """
    metadata = multica_ref_metadata(remote_task_ref)
    if metadata is None:
        return
    if revision_id and metadata.get("profile_revision_id") and metadata["profile_revision_id"] != str(revision_id):
        raise ContractError("multica remote reference belongs to a different profile revision")
    workspace = metadata.get("workspace_id")
    if workspace and workspace != str(config.workspace_id or ""):
        raise ContractError("multica remote reference belongs to a different workspace")
    base = metadata.get("base_url")
    if base and base != str(config.base_url or ""):
        raise ContractError("multica remote reference belongs to a different service endpoint")


def _multica_ref_identity(remote_task_ref: str | None) -> dict[str, Any] | None:
    """Parse a Multica-shaped reference body, or ``None`` for anything else."""
    if not remote_task_ref:
        return None
    try:
        value = json.loads(remote_task_ref)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("issue_ref") else None


def multica_learned_remote_ref(call, candidate: str | None) -> str:
    """Classify a query observation's reference against its Call.

    A Multica query can learn the original run/task id (and the provider's
    immutable issue id) that the issue-create response could not carry; the
    service returns the enriched ``remote_task_ref`` with the observation.
    Returns one of three durable decisions:

    - ``"adopted"`` — the candidate preserves the identity the Call already
      records (same issue, same task run once pinned, same profile source)
      and adds information; ``call.remote_task_ref`` was updated.
    - ``"noop"`` — nothing to decide: identical reference, a non-Multica
      (opaque) candidate or Call reference, or a Call without a reference.
      Identity is enriched, never invented; the observation stays applicable.
    - ``"rejected"`` — the candidate is Multica-shaped but contradicts the
      pinned identity (a rerun's task id, a foreign issue, a different
      revision/endpoint/workspace, a dropped pinned identity).  The
      observation describes a different remote execution and must be dropped
      without applying its state: a wrong task's status must never close or
      fail the original Call.
    """
    learned = _multica_ref_identity(candidate)
    current = _multica_ref_identity(call.remote_task_ref)
    if learned is None or current is None:
        # Only Multica-shaped references are learned, and only onto a Call
        # that already holds one: identity must be enriched, never invented.
        return "noop"
    # Once an immutable provider identity is pinned it can never be replaced
    # or dropped; only enrichment of missing keys is accepted.
    for key in ("task_id", "issue_id"):
        held = current.get(key)
        if held is not None and learned.get(key) != held:
            logger.warning(
                "rejecting multica remote_task_ref update: pinned %s changed for call=%s",
                key, getattr(call, "id", "?"),
            )
            return "rejected"
    # Issue identity: the immutable issue id proves the same issue (a renamed
    # identifier alias is accepted); without it the identifier must match.
    if not (current.get("issue_id") and learned.get("issue_id")):
        if str(learned["issue_ref"]) != str(current["issue_ref"]):
            logger.warning(
                "rejecting multica remote_task_ref update: issue identity changed for call=%s",
                getattr(call, "id", "?"),
            )
            return "rejected"
    current_source = multica_ref_metadata(call.remote_task_ref) or {}
    learned_source = multica_ref_metadata(candidate) or {}
    for key in _MULTICA_REF_SOURCE_KEYS:
        if current_source.get(key) and learned_source.get(key) != current_source.get(key):
            logger.warning(
                "rejecting multica remote_task_ref update: source %s changed for call=%s",
                key, getattr(call, "id", "?"),
            )
            return "rejected"
    bound = getattr(call, "multica_profile_revision_id", None)
    if bound and learned_source.get("profile_revision_id") and learned_source["profile_revision_id"] != str(bound):
        logger.warning(
            "rejecting multica remote_task_ref update: revision differs from call binding for call=%s",
            getattr(call, "id", "?"),
        )
        return "rejected"
    updated = str(candidate)
    if updated == str(call.remote_task_ref):
        return "noop"
    call.remote_task_ref = updated
    return "adopted"


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
    supports_approval: bool = False
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
            # Rebuilding a connector after worker restart or credential
            # rotation is safe when the immutable descriptor is identical.
            # A descriptor change still remains an immutable-revision error.
            if existing.descriptor() != descriptor:
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

    Direct mode calls the configured HTTP endpoint.  Pull mode enqueues a
    durable RAP task through owner-scoped callbacks and reconciles the task
    queue; the protocol has no cancellation callback after a task is claimed.
    """

    agent_id: str
    key: str
    endpoint: str
    token: str = ""
    timeout_seconds: int = 120
    revision: int = 1
    # Hash of the persisted endpoint/credential snapshot used by this
    # connector.  CapabilityRevision stores it so a later config update can
    # never silently retarget an old waiting Call.
    manifest_hash: str | None = None
    mode: str = "direct"
    pull_enqueue: Callable[[str, str | None, int, str], Mapping[str, Any]] | None = field(default=None, compare=False, repr=False)
    pull_query: Callable[[str], Mapping[str, Any]] | None = field(default=None, compare=False, repr=False)
    pull_cancel: Callable[[str], Mapping[str, Any]] | None = field(default=None, compare=False, repr=False)
    transport: Any = field(default=None, compare=False, repr=False)

    def descriptor(self) -> AgentDescriptor:
        return AgentDescriptor(
            agent_id=self.agent_id,
            key=self.key,
            revision=self.revision,
            transport="rap.v1",
            session_policy=SessionPolicy.RESUMABLE,
            supports_stream=False,
            supports_cancel=self.mode == "pull" and self.pull_cancel is not None,
            supports_push=False,
            supports_query_status=self.mode == "pull" and self.pull_query is not None,
            # RAP v1 accepts bounded structured artifacts in its terminal
            # response; progress/cancellation remain explicit callback or
            # pull capabilities rather than being overclaimed here.
            supports_artifact=True,
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
        if self.mode == "pull":
            if self.pull_enqueue is None:
                raise ContractError("RAP pull connector is not configured")
            timeout = max(1, int(self.timeout_seconds))
            if isinstance(deadline, datetime):
                timeout = max(1, min(timeout, int((deadline - datetime.now(timezone.utc)).total_seconds())))
            queued = await asyncio.to_thread(
                self.pull_enqueue, body["message"], body.get("session_ref"), timeout, call_id,
            )
            if not isinstance(queued, Mapping) or not queued.get("remote_task_ref"):
                raise ContractError("RAP pull enqueue did not return a remote task reference")
            return {"run_id": run_id, "call_id": call_id, **dict(queued)}
        # Configuration-time validation is insufficient: an operator may have
        # changed the persisted endpoint, and DNS may have changed since the
        # capability revision was registered. Re-run the shared SSRF guard at
        # the last point before a real network request. Injected transports
        # are test seams and intentionally bypass DNS resolution.
        if self.transport is None:
            try:
                from app.super_assistant.mcp_client import McpClientError, validate_mcp_url
                validate_mcp_url(self.endpoint, require_https=True)
            except McpClientError as exc:
                raise ContractError(f"remote connector endpoint rejected: {exc}") from exc
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        timeout = max(1.0, float(self.timeout_seconds))
        if isinstance(deadline, datetime):
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            timeout = max(1.0, min(timeout, remaining))
        client_kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": False}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            # Read the untrusted remote response incrementally.  Parsing via
            # ``client.post(...).json()`` first materializes an arbitrarily
            # large body before the kernel's later 20k content cap can help.
            chunks: list[bytes] = []
            size = 0
            async with client.stream("POST", self.endpoint, headers=headers, json=body) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 256 * 1024:
                        raise ContractError("remote connector response exceeds 256 KiB")
                    chunks.append(chunk)
            try:
                value = json.loads(b"".join(chunks))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContractError("remote connector response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ContractError("remote connector response must be an object")
        status = str(value.get("status") or "failed").lower()
        if status not in {"answered", "failed", "cancelled"}:
            # Unknown provider state is not evidence of failure.
            status = "unknown"
        return {
            "run_id": run_id,
            "call_id": call_id,
            "status": status,
            "content": str(value.get("content") or ""),
            "session_ref": value.get("session_ref"),
            "note": str(value.get("note") or "")[:2000],
            "artifacts": value.get("artifacts") or [],
            "provider_status": status,
        }

    async def cancel(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        if self.pull_cancel is not None:
            return await asyncio.to_thread(self.pull_cancel, remote_task_ref)
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}

    async def query_status(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        if self.pull_query is not None:
            return await asyncio.to_thread(self.pull_query, remote_task_ref)
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
    manifest_hash: str | None = None
    # 平台内置通道：执行必须走各自的服务层内部端点，不能把用户可见的
    # transport/url/凭据当作可配置目标。
    builtin_key: str | None = None
    # 受管 WeKnora 端点（builtin_key 保持 None）：transport/url/headers 仅是
    # 验证链冻结的连接快照，invoke 必须经 weknora.execution 即时复核
    # 端点/范围/工具一致性后再走标准 MCP；scope/owner_id 为复核授权输入。
    weknora_endpoint_id: str | None = None
    weknora_scope: Mapping[str, Any] | None = None
    owner_id: str | None = None

    @property
    def namespaced_key(self) -> str:
        from app.super_assistant.mcp_client import namespaced_tool_name

        return namespaced_tool_name(self.server_name, self.tool_name)

    @property
    def side_effect_class(self) -> str:
        # 通用 MCP 维持既有的外部异步口径（派发前必须用户审批）；内置通道
        # 由 runtime 的服务层分支执行，不进入本连接器。
        return "external_async"

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
        if self.builtin_key is not None:
            raise ContractError("platform builtin MCP must use its in-process channel")
        if self.weknora_endpoint_id is not None:
            from app.super_assistant.weknora import execution as weknora_execution

            content = await weknora_execution.call_tool(
                server_id=self.server_id,
                owner_id=str(self.owner_id or ""),
                endpoint_id=self.weknora_endpoint_id,
                scope=dict(self.weknora_scope or {}),
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


@dataclass(frozen=True, slots=True)
class MulticaToolConnector:
    """Adapter for the existing Multica service under the kernel Call API."""

    tool_name: str
    executor: Callable[[dict[str, Any]], str] = field(compare=False, repr=False)
    query_executor: Callable[[str], Mapping[str, Any]] | None = field(default=None, compare=False, repr=False)
    cancel_executor: Callable[[str], Mapping[str, Any]] | None = field(default=None, compare=False, repr=False)
    revision: int = 1
    manifest_hash: str | None = None
    # Immutable Multica profile snapshot the owning Call was bound to.  The
    # closures are constructed by the runtime against exactly this source, so
    # query/cancel/execute never silently retarget a waiting Call to the
    # currently active profile.  None keeps the legacy active-config semantics.
    profile_revision_id: str | None = None
    profile_base_url: str | None = field(default=None, compare=False)
    profile_workspace_id: str | None = field(default=None, compare=False)

    def descriptor(self) -> AgentDescriptor:
        return AgentDescriptor(
            agent_id=f"multica:{self.tool_name}",
            key=self.tool_name,
            revision=self.revision,
            transport="multica",
            capabilities=("workspace",),
            session_policy=SessionPolicy.STATELESS,
            supports_stream=False,
            supports_cancel=self.cancel_executor is not None,
            supports_push=False,
            supports_query_status=self.query_executor is not None,
            supports_artifact=False,
        )

    def _bound_remote_ref(self, remote_ref: Any) -> str:
        """Attach the profile source metadata to a fresh provider reference."""
        if not self.profile_revision_id or not isinstance(remote_ref, str):
            return str(remote_ref)
        try:
            value = json.loads(remote_ref)
        except (TypeError, ValueError, json.JSONDecodeError):
            # An unparseable provider reference is preserved verbatim; the
            # kernel never invents identity metadata it cannot verify.
            return remote_ref
        if not isinstance(value, dict):
            return remote_ref
        value.setdefault("profile_revision_id", str(self.profile_revision_id))
        if self.profile_base_url:
            value.setdefault("base_url", str(self.profile_base_url))
        if self.profile_workspace_id:
            value.setdefault("workspace_id", str(self.profile_workspace_id))
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    async def invoke(self, *, run_id: str, call_id: str, input_ref: str, deadline) -> Mapping[str, Any]:
        try:
            value = json.loads(input_ref or "{}")
        except (TypeError, ValueError):
            value = {}
        arguments = value.get("arguments") if isinstance(value, dict) else {}
        if not isinstance(arguments, dict):
            raise ContractError("Multica connector arguments must be an object")
        content = await asyncio.to_thread(self.executor, arguments)
        # ``multica_create_task`` is provider-asynchronous: preserve the
        # issue/task identity while keeping list/read tools synchronous.
        parsed: dict[str, Any] = {}
        if isinstance(content, str):
            try:
                value = json.loads(content)
                if isinstance(value, dict):
                    parsed = value
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if self.tool_name == "multica_create_task" and parsed.get("created"):
            remote_ref = parsed.get("remote_task_ref")
            if remote_ref:
                issue = parsed.get("issue") if isinstance(parsed.get("issue"), dict) else {}
                provider_status = str(issue.get("status") or "running")
                normalized = provider_status.lower().replace("-", "_")
                if normalized in {"completed", "complete", "done", "closed", "resolved", "success", "succeeded", "finished"}:
                    status = "answered"
                elif normalized in {"failed", "failure", "error", "errored", "blocked"}:
                    status = "failed"
                elif normalized in {"cancelled", "canceled", "aborted", "stopped"}:
                    status = "cancelled"
                else:
                    status = "running"
                return {
                    "run_id": run_id,
                    "call_id": call_id,
                    "status": status,
                    "content": str(parsed.get("note") or content),
                    "remote_task_ref": self._bound_remote_ref(remote_ref),
                    "provider_status": provider_status,
                }
        return {
            "run_id": run_id,
            "call_id": call_id,
            "status": "answered",
            "content": str(content),
            "provider_status": "completed",
        }

    async def cancel(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        if self.cancel_executor is None:
            return {"status": "unsupported", "remote_task_ref": remote_task_ref}
        return await asyncio.to_thread(self.cancel_executor, remote_task_ref)

    async def query_status(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        if self.query_executor is None:
            return {"status": "unsupported", "remote_task_ref": remote_task_ref}
        return await asyncio.to_thread(self.query_executor, remote_task_ref)


@dataclass
class ProcessPluginConnector:
    """Kernel adapter for one persisted process-plugin revision.

    Admission/release callbacks are supplied by the owner-scoped service so
    unloading a plugin cannot race a new Call.  The host response is treated
    as data only; it cannot alter the selected capability revision.
    """

    plugin_id: str
    descriptor_value: AgentDescriptor
    host: Any
    admit: Callable[[], None] = field(compare=False, repr=False)
    release: Callable[[], None] = field(compare=False, repr=False)

    def descriptor(self) -> AgentDescriptor:
        return self.descriptor_value

    async def invoke(self, *, run_id: str, call_id: str, input_ref: str, deadline) -> Mapping[str, Any]:
        self.admit()
        try:
            timeout = 120.0
            if isinstance(deadline, datetime):
                timeout = max(1.0, min(timeout, (deadline - datetime.now(timezone.utc)).total_seconds()))
            result = await self.host.health(timeout=min(5.0, timeout))
            if result.get("ok") is False:
                raise ContractError("process plugin health check failed")
            value = await self.host.invoke({"run_id": run_id, "call_id": call_id, "input_ref": input_ref}, timeout=timeout)
            if not isinstance(value, dict):
                raise ContractError("process plugin result must be an object")
            return {"run_id": run_id, "call_id": call_id, **value}
        finally:
            try:
                stop = getattr(self.host, "stop", None)
                if stop is not None:
                    await stop()
            finally:
                # Admission is a durable counter; release it even when host
                # cleanup itself fails so drain cannot wedge permanently.
                self.release()

    async def cancel(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}

    async def query_status(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}


@dataclass(frozen=True)
class PluginRunnerConnector:
    """NATS adapter for a user plugin executed by the external runner.

    The API/executor process only publishes an immutable invocation envelope;
    it never starts plugin code.  Completion and progress arrive through the
    runner reply stream and are reconciled by the durable adapter, so the
    initial call returns an opaque request reference immediately.
    """

    owner_id: str
    plugin_id: str
    descriptor_value: AgentDescriptor
    revision: int
    capability_revision: int
    manifest_hash: str
    workspace_snapshot_ref: str
    # Frozen owner policy forwarded to the isolated runner.  These values are
    # authorization inputs, not advisory metadata.
    network_scope: tuple[str, ...] = ()
    workspace_scope: tuple[str, ...] = ()
    secret_lease_refs: tuple[str, ...] = ()

    def descriptor(self) -> AgentDescriptor:
        # Changing the execution host must not expand the immutable
        # capability snapshot frozen when the plugin was installed.
        return self.descriptor_value

    async def invoke(self, *, run_id: str, call_id: str, input_ref: str, deadline) -> Mapping[str, Any]:
        from .plugin_runner import (
            PLUGIN_RUNNER_REPLY_PREFIX,
            PLUGIN_RUNNER_STREAM,
            PLUGIN_RUNNER_SUBJECT,
            PluginInvocationEnvelope,
        )
        from app.data_channel.pipeline_tasks.dispatch import _dispatch_sync_to_stream

        request_id = f"plugin-call:{call_id}"
        envelope = PluginInvocationEnvelope(
            request_id=request_id, owner_id=self.owner_id, run_id=run_id,
            call_id=call_id, plugin_id=self.plugin_id, revision=self.revision,
            manifest_hash=self.manifest_hash, capability_revision=self.capability_revision,
            input_ref=input_ref, workspace_snapshot_ref=self.workspace_snapshot_ref,
            network_scope=tuple(self.network_scope), workspace_scope=tuple(self.workspace_scope),
            secret_lease_refs=tuple(self.secret_lease_refs),
            deadline=deadline.isoformat() if isinstance(deadline, datetime) else str(deadline),
            reply_subject=f"{PLUGIN_RUNNER_REPLY_PREFIX}{request_id}",
        )
        await asyncio.to_thread(
            _dispatch_sync_to_stream, PLUGIN_RUNNER_SUBJECT, envelope.to_payload(),
            envelope.msg_id, PLUGIN_RUNNER_STREAM,
        )
        return {
            "run_id": run_id, "call_id": call_id, "status": "accepted",
            "remote_task_ref": request_id, "provider_event_id": f"{request_id}:accepted",
        }

    async def cancel(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        # Cancellation control messages require a runner-side process handle;
        # until that contract is deployed, report unsupported rather than
        # claiming that a user process stopped.
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}

    async def query_status(self, *, remote_task_ref: str) -> Mapping[str, Any]:
        return {"status": "unsupported", "remote_task_ref": remote_task_ref}
