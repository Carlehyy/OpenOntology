import pytest

from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.process_plugin_service import (
    ProcessPluginBusyError,
    ProcessPluginValidationError,
    capability_key,
    disable_process_plugin,
    enable_process_plugin,
    install_process_plugin,
    uninstall_process_plugin,
)
from app.super_assistant.schemas import ProcessPluginCreate


def _body(**overrides):
    values = {
        "key": "user.echo",
        "revision": 1,
        "entrypoint": "python plugin.py",
        "trust_level": "user_untrusted",
        "workspace_scope": ["/tmp/plugin"],
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


def test_process_plugin_manifest_rejects_unknown_host_capability(db, admin_user):
    with pytest.raises(ProcessPluginValidationError, match="outside"):
        install_process_plugin(db, admin_user.id, _body(key="user.invalid", capabilities=["kernel.raw"]))
