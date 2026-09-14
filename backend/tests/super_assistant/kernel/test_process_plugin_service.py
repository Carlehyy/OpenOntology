import pytest
import shlex
import sys
from types import SimpleNamespace

from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.process_plugin_service import (
    ProcessPluginBusyError,
    ProcessPluginValidationError,
    capability_key,
    disable_process_plugin,
    enable_process_plugin,
    install_process_plugin,
    uninstall_process_plugin,
    admit_plugin_call,
    reclaim_expired_plugin_calls,
)
from datetime import datetime, timedelta, timezone
from app.super_assistant.models import SuperAssistantProcessPluginInvocation
from app.super_assistant.schemas import ProcessPluginCreate


def _body(**overrides):
    script = (
        "import json,sys; "
        "[print(json.dumps({'ok':True,'key':json.loads(line).get('key',''),"
        "'revision':json.loads(line).get('revision',1),'protocol':'plugin.v1'}), flush=True) "
        "for line in sys.stdin]"
    )
    values = {
        "key": "user.echo",
        "revision": 1,
        "entrypoint": f"{sys.executable} -c {shlex.quote(script)}",
        "trust_level": "user_untrusted",
        "workspace_scope": ["/tmp"],
    }
    values.update(overrides)
    return ProcessPluginCreate(**values)


def test_process_plugin_lifecycle_persists_and_revokes_capability(db, admin_user):
    row = install_process_plugin(db, admin_user.id, _body())
    cap = db.get(CapabilityRevision, (capability_key(admin_user.id, "user.echo"), 1))
    assert row.state == "installed"
    assert cap is not None and cap.enabled is False

    enable_process_plugin(db, admin_user.id, row.id)
    db.refresh(cap)
    assert cap.enabled is True
    disable_process_plugin(db, admin_user.id, row.id)
    db.refresh(cap)
    assert cap.enabled is False

    uninstall_process_plugin(db, admin_user.id, row.id)
    db.refresh(row)
    assert row.state == "uninstalled"
    assert row.uninstalled_at is not None


def test_uninstall_enters_drain_when_calls_are_active(db, admin_user):
    row = install_process_plugin(db, admin_user.id, _body(key="user.busy"))
    enable_process_plugin(db, admin_user.id, row.id)
    row.active_calls = 1
    db.commit()
    with pytest.raises(ProcessPluginBusyError):
        uninstall_process_plugin(db, admin_user.id, row.id)
    db.refresh(row)
    assert row.state == "draining"


def test_process_plugin_invocation_lease_reclaims_after_worker_loss(db, admin_user):
    row = install_process_plugin(db, admin_user.id, _body(key="user.lease"))
    enable_process_plugin(db, admin_user.id, row.id)
    lease_id = admit_plugin_call(db, admin_user.id, row.id, call_id="call-lost", lease_seconds=1)
    db.commit()
    assert row.active_calls == 1
    reclaimed = reclaim_expired_plugin_calls(
        db, owner_id=admin_user.id, plugin_id=row.id,
        now=datetime.now(timezone.utc) + timedelta(seconds=2),
    )
    db.commit()
    assert reclaimed == 1
    db.refresh(row)
    invocation = db.get(SuperAssistantProcessPluginInvocation, lease_id)
    assert row.active_calls == 0 and invocation.state == "expired"
    uninstall_process_plugin(db, admin_user.id, row.id)
    db.refresh(row)
    assert row.state == "uninstalled"


def test_process_plugin_manifest_rejects_unknown_host_capability(db, admin_user):
    with pytest.raises(ProcessPluginValidationError, match="outside"):
        install_process_plugin(db, admin_user.id, _body(key="user.invalid", capabilities=["kernel.raw"]))


def test_runtime_resolves_enabled_plugin_without_manifest_hash_conflict(db, admin_user, monkeypatch):
    from app.super_assistant.kernel import runtime

    monkeypatch.setattr("app.shared.config.settings.super_assistant_process_plugin_runner_mode", "direct_dev", raising=False)
    row = install_process_plugin(db, admin_user.id, _body(key="user.runtime", revision=3))
    enable_process_plugin(db, admin_user.id, row.id)
    call = SimpleNamespace(target_ref=row.id, capability_revision=3, capability_key=capability_key(admin_user.id, row.key))
    run = SimpleNamespace(owner_id=admin_user.id)
    connector = runtime._resolve_external_connector(db, run, call)
    assert connector is not None
    assert connector.descriptor().key == capability_key(admin_user.id, row.key)


def test_runtime_fails_closed_without_dedicated_plugin_runner(db, admin_user, monkeypatch):
    from app.super_assistant.kernel import runtime

    monkeypatch.setattr("app.shared.config.settings.super_assistant_process_plugin_runner_mode", "disabled", raising=False)
    row = install_process_plugin(db, admin_user.id, _body(key="user.runner-required", revision=5))
    enable_process_plugin(db, admin_user.id, row.id)
    call = SimpleNamespace(
        target_ref=row.id,
        capability_revision=row.revision,
        capability_key=capability_key(admin_user.id, row.key),
    )
    run = SimpleNamespace(owner_id=admin_user.id)
    assert runtime._resolve_external_connector(db, run, call) is None


def test_enable_requires_healthy_handshake_and_keeps_plugin_disabled(db, admin_user):
    script = "import sys; [print('{\\\"ok\\\":true}', flush=True) for line in sys.stdin]"
    row = install_process_plugin(
        db,
        admin_user.id,
        _body(key="user.unhealthy", entrypoint=f"{sys.executable} -c {shlex.quote(script)}"),
    )
    with pytest.raises(ProcessPluginValidationError, match="健康检查失败"):
        enable_process_plugin(db, admin_user.id, row.id)
    db.refresh(row)
    cap = db.get(CapabilityRevision, (capability_key(admin_user.id, "user.unhealthy"), 1))
    assert row.state == "disabled"
    assert row.last_health_status == "failed"
    assert cap is not None and cap.enabled is False


@pytest.mark.parametrize("environment", ["production", "Production", " production ", "prod", " PROD "])
def test_production_fails_closed_for_user_untrusted_plugin(db, admin_user, monkeypatch, environment):
    from app.super_assistant import process_plugin_service
    row = install_process_plugin(db, admin_user.id, _body(key="user.production"))
    monkeypatch.setattr(process_plugin_service.settings, "environment", environment)
    with pytest.raises(ProcessPluginValidationError, match="隔离 runner"):
        enable_process_plugin(db, admin_user.id, row.id)
    db.refresh(row)
    assert row.state == "installed"


def test_tampered_trust_level_cannot_elevate_process_plugin(db, admin_user):
    """A mutable DB trust string must never create an executable elevation."""
    from app.super_assistant.kernel import runtime

    row = install_process_plugin(db, admin_user.id, _body(key="user.tampered"))
    row.trust_level = "verified"
    db.commit()
    with pytest.raises(ProcessPluginValidationError, match="未经平台签名"):
        enable_process_plugin(db, admin_user.id, row.id)
    call = SimpleNamespace(target_ref=row.id, capability_revision=row.revision, capability_key=capability_key(admin_user.id, row.key))
    run = SimpleNamespace(owner_id=admin_user.id)
    assert runtime._resolve_external_connector(db, run, call) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("entrypoint", "/tmp/attacker-plugin"), ("capabilities", ["context.read", "network.request"])],
)
def test_runtime_rejects_tampered_manifest_and_revokes_capability(db, admin_user, field, value):
    """Changing executable or policy fields cannot reuse an old manifest hash."""
    from app.super_assistant.kernel import runtime

    row = install_process_plugin(db, admin_user.id, _body(key=f"user.tampered.{field}", revision=4))
    enable_process_plugin(db, admin_user.id, row.id)
    old_hash = row.manifest_hash
    setattr(row, field, value)
    # Simulate a direct database edit that deliberately preserves the old
    # digest. The resolver must catch it before creating a child process.
    row.manifest_hash = old_hash
    db.commit()

    call = SimpleNamespace(
        target_ref=row.id, capability_revision=row.revision,
        capability_key=capability_key(admin_user.id, row.key),
    )
    run = SimpleNamespace(owner_id=admin_user.id)
    assert runtime._resolve_external_connector(db, run, call) is None
    cap = db.get(CapabilityRevision, (capability_key(admin_user.id, row.key), row.revision))
    assert cap is not None and cap.enabled is False
