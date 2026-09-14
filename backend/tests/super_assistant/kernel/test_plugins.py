import pytest

from app.super_assistant.kernel.connectors import TrustLevel
from app.super_assistant.kernel.contracts import ContractError
from app.super_assistant.kernel.plugins import PluginCatalog, PluginManifest, PluginState
from app.super_assistant.kernel.plugin_host import PluginHostError, ProcessPluginHost
from app.super_assistant.kernel.plugin_runner import (
    PLUGIN_RUNNER_PROTOCOL,
    PluginInvocationEnvelope,
    PluginRunnerEventEnvelope,
)


def _runner_envelope(**overrides):
    values = {
        "request_id": "req-1",
        "owner_id": "owner-1",
        "run_id": "run-1",
        "call_id": "call-1",
        "plugin_id": "plugin-1",
        "revision": 2,
        "manifest_hash": "a" * 64,
        "capability_revision": 2,
        "input_ref": '{"message":"hello"}',
        "workspace_snapshot_ref": "workspace:owner-1/run-1",
        "secret_lease_refs": ("lease:one",),
        "deadline": "2030-01-01T00:00:00+00:00",
        "reply_subject": "sa.plugin.reply.req-1",
    }
    values.update(overrides)
    return PluginInvocationEnvelope(**values)


def test_plugin_runner_envelope_round_trips_and_uses_request_id_for_deduplication():
    envelope = _runner_envelope()
    payload = envelope.to_payload()
    assert payload["protocol"] == PLUGIN_RUNNER_PROTOCOL
    assert payload["secret_lease_refs"] == ["lease:one"]
    assert "short-lived-secret" not in str(payload)
    restored = PluginInvocationEnvelope.from_payload(payload)
    assert restored == envelope
    assert restored.msg_id == "req-1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"manifest_hash": "not-a-digest"},
        {"reply_subject": "sa.plugin.reply.*"},
        {"revision": 0},
        {"input_ref": "x" * (1024 * 1024 + 1)},
    ],
)
def test_plugin_runner_envelope_rejects_unsafe_identity_or_bounds(overrides):
    with pytest.raises(ContractError):
        _runner_envelope(**overrides)


def test_plugin_runner_envelope_rejects_unknown_or_missing_fields():
    payload = _runner_envelope().to_payload()
    payload["unexpected"] = "must-not-cross-contract"
    with pytest.raises(ContractError, match="fields"):
        PluginInvocationEnvelope.from_payload(payload)
    del payload["unexpected"]
    del payload["reply_subject"]
    with pytest.raises(ContractError, match="fields"):
        PluginInvocationEnvelope.from_payload(payload)


def test_plugin_runner_event_round_trips_structured_artifact_and_identity():
    event = PluginRunnerEventEnvelope(
        request_id="req-1", owner_id="owner-1", run_id="run-1", call_id="call-1",
        plugin_id="plugin-1", revision=2, manifest_hash="a" * 64,
        capability_revision=2, event_seq=4, kind="completed", status="completed",
        payload={"summary": "done"}, artifacts=({
            "artifact_ref": "artifact://owner-1/run-1/call-1/out",
            "name": "result.json", "mime_type": "application/json", "size": 12,
            "checksum": "b" * 64,
        },),
    )
    payload = event.to_payload()
    restored = PluginRunnerEventEnvelope.from_payload(payload)
    assert restored == event
    assert payload["artifacts"][0]["artifact_ref"].startswith("artifact://")


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "completed", "status": "running"},
        {"kind": "progress", "status": "running", "event_seq": -1},
        {"kind": "artifact", "status": "running", "artifacts": ({"artifact_ref": "x"},)},
    ],
)
def test_plugin_runner_event_rejects_invalid_status_sequence_or_artifact(overrides):
    values = {
        "request_id": "req-1", "owner_id": "owner-1", "run_id": "run-1", "call_id": "call-1",
        "plugin_id": "plugin-1", "revision": 1, "manifest_hash": "a" * 64,
        "capability_revision": 1, "event_seq": 0, "kind": "progress", "status": "running",
        "payload": {}, "artifacts": (),
    }
    values.update(overrides)
    with pytest.raises(ContractError):
        PluginRunnerEventEnvelope(**values)


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
async def test_process_plugin_host_uses_json_lines_and_rejects_capability_expansion(monkeypatch):
    import shlex
    import sys

    script = (
        "import json,sys; "
        "[print(json.dumps({'ok':True,'key':'user.echo','revision':1,'protocol':'plugin.v1','secret':__import__('os').environ.get('PLUGIN_SECRET'),'leak':__import__('os').environ.get('PLUGIN_LEAK')}), flush=True) "
        "for line in sys.stdin if json.loads(line).get('op') == 'health']"
    )
    manifest = PluginManifest(
        key="user.echo", revision=1,
        entrypoint=f"{sys.executable} -c {shlex.quote(script)}",
        trust_level=TrustLevel.VERIFIED, secret_refs=("PLUGIN_SECRET",),
    )
    monkeypatch.setenv("PLUGIN_LEAK", "must-not-cross-plugin-boundary")
    host = ProcessPluginHost(manifest, secret_env={"PLUGIN_SECRET": "short-lived", "PLUGIN_LEAK": "wrong"})
    result = await host.health()
    assert result == {"ok": True, "key": "user.echo", "revision": 1, "protocol": "plugin.v1", "secret": "short-lived", "leak": None}
    await host.stop()

    bad_script = "import sys; print('{\\\"capabilities\\\":[]}'); sys.stdout.flush()"
    bad = ProcessPluginHost(
        PluginManifest(key="user.bad", revision=1, entrypoint=f"{sys.executable} -c {shlex.quote(bad_script)}", trust_level=TrustLevel.VERIFIED),
    )
    with pytest.raises(PluginHostError, match="expand capabilities"):
        await bad.health()
    await bad.stop()


@pytest.mark.asyncio
async def test_process_plugin_health_requires_identity_protocol_and_ok():
    import shlex
    import sys

    script = (
        "import json,sys; "
        "[print(json.dumps({'ok':True}), flush=True) for line in sys.stdin]"
    )
    host = ProcessPluginHost(PluginManifest(
        key="user.missing", revision=7,
        entrypoint=f"{sys.executable} -c {shlex.quote(script)}",
        trust_level=TrustLevel.VERIFIED,
    ))
    with pytest.raises(PluginHostError, match="identity mismatch"):
        await host.health(timeout=1)
    assert host._process is None


@pytest.mark.asyncio
async def test_process_plugin_timeout_terminates_child():
    import shlex
    import sys

    script = "import time,sys; [time.sleep(10) for _ in sys.stdin]"
    host = ProcessPluginHost(PluginManifest(
        key="user.timeout", revision=1,
        entrypoint=f"{sys.executable} -c {shlex.quote(script)}",
        trust_level=TrustLevel.VERIFIED,
    ))
    with pytest.raises(PluginHostError, match="timed out"):
        await host.invoke({"x": 1}, timeout=0.05)
    assert host._process is None


@pytest.mark.asyncio
async def test_process_plugin_rejects_unframed_oversized_response_before_memory_growth():
    import shlex
    import sys

    script = "import sys; sys.stdout.write('x' * (1024 * 1024 + 1024)); sys.stdout.flush()"
    host = ProcessPluginHost(PluginManifest(
        key="user.frame", revision=1,
        entrypoint=f"{sys.executable} -c {shlex.quote(script)}",
        trust_level=TrustLevel.VERIFIED,
    ))
    with pytest.raises(PluginHostError, match="frame is too large"):
        await host.invoke({"x": 1}, timeout=1)
    assert host._process is None
