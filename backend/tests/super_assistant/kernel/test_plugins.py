import pytest

from app.super_assistant.kernel.connectors import TrustLevel
from app.super_assistant.kernel.contracts import ContractError
from app.super_assistant.kernel.plugins import PluginCatalog, PluginManifest, PluginState
from app.super_assistant.kernel.plugin_host import PluginHostError, ProcessPluginHost


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


def test_plugin_catalog_drain_stops_new_calls_and_waits_for_active_calls():
    catalog = PluginCatalog()
    catalog.install(
        PluginManifest(key="user.calendar", revision=1, entrypoint="plugin:main", trust_level=TrustLevel.VERIFIED),
        host_capabilities=frozenset(),
    )
    catalog.enable("user.calendar", 1)
    assert catalog.acquire_call("user.calendar", 1).active_calls == 1
    catalog.start_drain("user.calendar", 1)
    with pytest.raises(ContractError, match="not enabled"):
        catalog.acquire_call("user.calendar", 1)
    assert catalog.release_call("user.calendar", 1).active_calls == 0
    catalog.uninstall("user.calendar", 1)


@pytest.mark.asyncio
async def test_process_plugin_host_uses_json_lines_and_rejects_capability_expansion():
    import shlex
    import sys

    script = (
        "import json,sys; "
        "[print(json.dumps({'ok':True,'secret':__import__('os').environ.get('PLUGIN_SECRET')}), flush=True) "
        "for line in sys.stdin if json.loads(line).get('op') == 'health']"
    )
    manifest = PluginManifest(
        key="user.echo", revision=1,
        entrypoint=f"{sys.executable} -c {shlex.quote(script)}",
        trust_level=TrustLevel.VERIFIED,
    )
    host = ProcessPluginHost(manifest, secret_env={"PLUGIN_SECRET": "short-lived"})
    result = await host.health()
    assert result == {"ok": True, "secret": "short-lived"}
    await host.stop()

    bad_script = "import sys; print('{\\\"capabilities\\\":[]}'); sys.stdout.flush()"
    bad = ProcessPluginHost(
        PluginManifest(key="user.bad", revision=1, entrypoint=f"{sys.executable} -c {shlex.quote(bad_script)}", trust_level=TrustLevel.VERIFIED),
    )
    with pytest.raises(PluginHostError, match="expand capabilities"):
        await bad.health()
    await bad.stop()
