"""用户插件 manifest 与隔离生命周期的纯校验。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .connectors import TrustLevel
from .contracts import ContractError


class PluginState(StrEnum):
    INSPECTING = "inspecting"
    INSTALLED = "installed"
    ENABLED = "enabled"
    DRAINING = "draining"
    DISABLED = "disabled"
    UNINSTALLED = "uninstalled"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    key: str
    revision: int
    entrypoint: str
    trust_level: TrustLevel
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    network_scope: tuple[str, ...] = ()
    workspace_scope: tuple[str, ...] = ()
    secret_refs: tuple[str, ...] = ()

    def validate(self, *, host_capabilities: frozenset[str]) -> None:
        if not self.key or self.revision < 1 or not self.entrypoint:
            raise ContractError("plugin manifest requires key, positive revision and entrypoint")
        if not set(self.capabilities) <= host_capabilities:
            raise ContractError("plugin requests capability outside host registry")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ContractError("plugin capabilities must be unique")
        forbidden = {"event_store.write", "lease.fence", "database.raw"}
        if forbidden & set(self.permissions):
            raise ContractError("plugin cannot access kernel internals")
        if self.trust_level == TrustLevel.USER_UNTRUSTED and not self.workspace_scope:
            raise ContractError("user_untrusted plugin requires explicit workspace scope")


@dataclass(frozen=True, slots=True)
class PluginRecord:
    manifest: PluginManifest
    state: PluginState = PluginState.INSTALLED
    active_calls: int = 0


class PluginCatalog:
    def __init__(self) -> None:
        self._records: dict[tuple[str, int], PluginRecord] = {}

    def install(self, manifest: PluginManifest, *, host_capabilities: frozenset[str]) -> PluginRecord:
        manifest.validate(host_capabilities=host_capabilities)
        key = (manifest.key, manifest.revision)
        if key in self._records:
            raise ContractError("plugin revision already installed")
        record = PluginRecord(manifest)
        self._records[key] = record
        return record

    def start_drain(self, key: str, revision: int) -> PluginRecord:
        record = self._get(key, revision)
        if record.state in {PluginState.UNINSTALLED, PluginState.DRAINING}:
            raise ContractError("plugin is not drainable")
        record = PluginRecord(record.manifest, PluginState.DRAINING, record.active_calls)
        self._records[(key, revision)] = record
        return record

    def acquire_call(self, key: str, revision: int) -> PluginRecord:
        """Atomically admit a call only while the revision is enabled."""
        record = self._get(key, revision)
        if record.state is not PluginState.ENABLED:
            raise ContractError("plugin revision is not enabled")
        record = PluginRecord(record.manifest, record.state, record.active_calls + 1)
        self._records[(key, revision)] = record
        return record

    def release_call(self, key: str, revision: int) -> PluginRecord:
        record = self._get(key, revision)
        if record.active_calls <= 0:
            raise ContractError("plugin has no active call")
        record = PluginRecord(record.manifest, record.state, record.active_calls - 1)
        self._records[(key, revision)] = record
        return record

    def enable(self, key: str, revision: int) -> PluginRecord:
        record = self._get(key, revision)
        if record.state not in {PluginState.INSTALLED, PluginState.DISABLED}:
            raise ContractError("plugin revision cannot be enabled")
        record = PluginRecord(record.manifest, PluginState.ENABLED, record.active_calls)
        self._records[(key, revision)] = record
        return record

    def uninstall(self, key: str, revision: int) -> None:
        record = self._get(key, revision)
        if record.active_calls:
            raise ContractError("plugin has active calls; drain before uninstall")
        self._records[(key, revision)] = PluginRecord(record.manifest, PluginState.UNINSTALLED)

    def _get(self, key: str, revision: int) -> PluginRecord:
        try:
            return self._records[(key, revision)]
        except KeyError as exc:
            raise ContractError("unknown plugin revision") from exc
