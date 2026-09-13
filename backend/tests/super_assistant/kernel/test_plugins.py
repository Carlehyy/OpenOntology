import pytest

from app.super_assistant.kernel.connectors import TrustLevel
from app.super_assistant.kernel.contracts import ContractError
from app.super_assistant.kernel.plugins import PluginCatalog, PluginManifest, PluginState


def test_plugin_manifest_cannot_expand_capability_or_kernel_access():
    catalog = PluginCatalog()
    manifest = PluginManifest(
        key="user.mail", revision=1, entrypoint="plugin:main",
        trust_level=TrustLevel.USER_UNTRUSTED, capabilities=("mail.send",),
        permissions=("network.request",), workspace_scope=("/tmp/plugin",),
    )
    record = catalog.install(manifest, host_capabilities=frozenset({"mail.send"}))
    assert record.state is PluginState.INSTALLED
    with pytest.raises(ContractError, match="outside"):
        PluginManifest(
            key="user.bad", revision=1, entrypoint="plugin:main",
            trust_level=TrustLevel.VERIFIED, capabilities=("kernel.raw",),
        ).validate(host_capabilities=frozenset())
    with pytest.raises(ContractError, match="internals"):
        PluginManifest(
            key="user.bad", revision=2, entrypoint="plugin:main",
            trust_level=TrustLevel.VERIFIED, permissions=("event_store.write",),
        ).validate(host_capabilities=frozenset())


def test_plugin_drain_must_precede_uninstall():
    catalog = PluginCatalog()
    catalog.install(
        PluginManifest(key="user.mail", revision=1, entrypoint="plugin:main", trust_level=TrustLevel.VERIFIED),
        host_capabilities=frozenset(),
    )
    draining = catalog.start_drain("user.mail", 1)
    assert draining.state is PluginState.DRAINING
    catalog.uninstall("user.mail", 1)
