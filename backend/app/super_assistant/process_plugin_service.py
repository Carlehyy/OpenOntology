"""Durable user process-plugin installation and lifecycle service.

The service owns database state; :mod:`kernel.plugins` remains the pure policy
implementation used by the host.  A plugin is never callable merely because
its manifest is stored: its CapabilityRevision is enabled only after the
explicit enable operation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.super_assistant.kernel.capability_service import (
    persist_capability_revision,
    revoke_capability_revisions,
    set_capability_revision_enabled,
)
from app.super_assistant.kernel.connectors import AgentDescriptor, SessionPolicy, TrustLevel
from app.super_assistant.kernel.contracts import ContractError
from app.super_assistant.kernel.plugin_host import PluginHostError, ProcessPluginHost
from app.super_assistant.kernel.plugins import PluginManifest, PluginState
from app.super_assistant.models import SuperAssistantProcessPlugin
from app.super_assistant.schemas import ProcessPluginCreate


class ProcessPluginServiceError(Exception):
    pass


class ProcessPluginNotFoundError(ProcessPluginServiceError):
    pass


class ProcessPluginConflictError(ProcessPluginServiceError):
    pass


class ProcessPluginValidationError(ProcessPluginServiceError):
    pass


class ProcessPluginBusyError(ProcessPluginServiceError):
    pass


# These are the host-facing capabilities that a process may request.  The
# plugin cannot turn them into shell/database access; the process host still
# enforces a separate OS/deployment sandbox.
PROCESS_PLUGIN_HOST_CAPABILITIES = frozenset({
    "context.read", "artifact.write", "network.request", "workspace.read",
})


def capability_key(owner_id: str, key: str) -> str:
    """Avoid the global CapabilityRevision key collision across owners."""
    return f"plugin:{owner_id}:{key}"


def _manifest_hash(body: ProcessPluginCreate) -> str:
    value = body.model_dump(mode="json", by_alias=False)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _manifest(row: SuperAssistantProcessPlugin) -> PluginManifest:
    return PluginManifest(
        key=capability_key(row.owner_id, row.key),
        revision=row.revision,
        entrypoint=row.entrypoint,
        trust_level=TrustLevel(row.trust_level),
        capabilities=tuple(row.capabilities or ()),
        permissions=tuple(row.permissions or ()),
        network_scope=tuple(row.network_scope or ()),
        workspace_scope=tuple(row.workspace_scope or ()),
        secret_refs=tuple(row.secret_refs or ()),
    )


def _descriptor(row: SuperAssistantProcessPlugin) -> AgentDescriptor:
    return AgentDescriptor(
        agent_id=f"plugin:{row.id}", key=capability_key(row.owner_id, row.key),
        revision=row.revision, transport="process_plugin",
        capabilities=tuple(row.capabilities or ()), session_policy=SessionPolicy.STATELESS,
        supports_stream=False, supports_cancel=False, supports_push=False,
        supports_query_status=False, supports_artifact="artifact.write" in (row.capabilities or ()),
    )


def _freeze_capability(db: Session, row: SuperAssistantProcessPlugin, *, enabled: bool) -> None:
    descriptor = _descriptor(row)
    persist_capability_revision(
        db, descriptor, source="process_plugin", trust_level=TrustLevel(row.trust_level),
        manifest_hash=row.manifest_hash, permissions=list(row.permissions or []),
        workspace_scope=list(row.workspace_scope or []),
        network_scope=list(row.network_scope or []),
        secret_refs=list(row.secret_refs or []),
    )
    set_capability_revision_enabled(db, descriptor.key, descriptor.revision, enabled=enabled)


def get_process_plugin(db: Session, owner_id: str, plugin_id: str, *, lock: bool = False) -> SuperAssistantProcessPlugin:
    statement = select(SuperAssistantProcessPlugin).where(
        SuperAssistantProcessPlugin.id == plugin_id,
        SuperAssistantProcessPlugin.owner_id == owner_id,
    )
    if lock:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        raise ProcessPluginNotFoundError("process plugin 不存在")
    return row


def list_process_plugins(db: Session, owner_id: str, *, include_uninstalled: bool = False) -> list[SuperAssistantProcessPlugin]:
    statement = select(SuperAssistantProcessPlugin).where(SuperAssistantProcessPlugin.owner_id == owner_id)
    if not include_uninstalled:
        statement = statement.where(SuperAssistantProcessPlugin.state != PluginState.UNINSTALLED.value)
    return list(db.scalars(statement.order_by(SuperAssistantProcessPlugin.updated_at.desc())).all())


def install_process_plugin(db: Session, owner_id: str, body: ProcessPluginCreate) -> SuperAssistantProcessPlugin:
    # This route is the user installation boundary.  ``verified`` is reserved
    # for a future platform-signed/admin flow and must not be user-selectable.
    if body.trust_level != TrustLevel.USER_UNTRUSTED.value:
        raise ProcessPluginValidationError("用户安装的进程插件必须标记为 user_untrusted")
    try:
        manifest = PluginManifest(
            key=capability_key(owner_id, body.key), revision=body.revision,
            entrypoint=body.entrypoint, trust_level=TrustLevel(body.trust_level),
            capabilities=tuple(body.capabilities), permissions=tuple(body.permissions),
            network_scope=tuple(body.network_scope), workspace_scope=tuple(body.workspace_scope),
            secret_refs=tuple(body.secret_refs),
        )
        manifest.validate(host_capabilities=PROCESS_PLUGIN_HOST_CAPABILITIES)
    except (ContractError, ValueError) as exc:
        raise ProcessPluginValidationError(str(exc)) from exc
    row = SuperAssistantProcessPlugin(
        owner_id=owner_id, key=body.key, display_name=body.display_name,
        description=body.description, revision=body.revision, entrypoint=body.entrypoint,
        trust_level=body.trust_level, capabilities=body.capabilities,
        permissions=body.permissions, network_scope=body.network_scope,
        workspace_scope=body.workspace_scope, secret_refs=body.secret_refs,
        manifest_hash=_manifest_hash(body), state=PluginState.INSTALLED.value,
    )
    try:
        db.add(row)
        db.flush()
        _freeze_capability(db, row, enabled=False)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as exc:
        db.rollback()
        raise ProcessPluginConflictError("相同 owner 的插件 key/revision 已安装") from exc
    except Exception:
        db.rollback()
        raise


async def _probe_plugin(row: SuperAssistantProcessPlugin) -> dict:
    """Run the mandatory pre-enable handshake and always tear down the host."""
    host = ProcessPluginHost(_manifest(row))
    try:
        return await host.health(timeout=5.0)
    finally:
        # A health probe is a short-lived process.  It must not leave a child
        # (or its secret environment) running while the revision is disabled.
        await host.stop()


def _healthcheck_before_enable(row: SuperAssistantProcessPlugin) -> dict:
    # The route is intentionally synchronous and runs in FastAPI's worker
    # thread.  Refuse to create an un-awaited coroutine when called from an
    # event loop by an internal caller.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        raise ProcessPluginValidationError("插件健康检查必须在同步 worker 中执行")
    try:
        return asyncio.run(_probe_plugin(row))
    except (PluginHostError, OSError, ValueError) as exc:
        raise ProcessPluginValidationError(f"插件健康检查失败: {str(exc)[:400]}") from exc


def enable_process_plugin(db: Session, owner_id: str, plugin_id: str) -> SuperAssistantProcessPlugin:
    row = get_process_plugin(db, owner_id, plugin_id, lock=True)
    if row.state not in {PluginState.INSTALLED.value, PluginState.DISABLED.value}:
        raise ProcessPluginValidationError("当前插件状态不可启用")
    try:
        health = _healthcheck_before_enable(row)
    except ProcessPluginValidationError as exc:
        row.last_health_status = "failed"
        row.last_health_message = str(exc)[:500]
        # Capability remains disabled and the persisted lifecycle state is
        # explicit, so a failed probe cannot accidentally become callable.
        _freeze_capability(db, row, enabled=False)
        db.commit()
        raise
    _freeze_capability(db, row, enabled=True)
    row.state = PluginState.ENABLED.value
    row.last_health_status = "healthy"
    row.last_health_message = None
    db.commit(); db.refresh(row)
    return row


def disable_process_plugin(db: Session, owner_id: str, plugin_id: str) -> SuperAssistantProcessPlugin:
    row = get_process_plugin(db, owner_id, plugin_id, lock=True)
    if row.state in {PluginState.UNINSTALLED.value, PluginState.DRAINING.value}:
        raise ProcessPluginValidationError("当前插件状态不可禁用")
    revoke_capability_revisions(db, [capability_key(row.owner_id, row.key)], revision=row.revision)
    row.state = PluginState.DISABLED.value
    db.commit(); db.refresh(row)
    return row


def uninstall_process_plugin(db: Session, owner_id: str, plugin_id: str) -> None:
    row = get_process_plugin(db, owner_id, plugin_id, lock=True)
    if row.state == PluginState.UNINSTALLED.value:
        return
    revoke_capability_revisions(db, [capability_key(row.owner_id, row.key)], revision=row.revision)
    if row.active_calls:
        row.state = PluginState.DRAINING.value
        row.drain_started_at = datetime.now(timezone.utc)
        db.commit()
        raise ProcessPluginBusyError("插件仍有在途调用，已进入 draining，请稍后重试卸载")
    row.state = PluginState.UNINSTALLED.value
    row.uninstalled_at = datetime.now(timezone.utc)
    db.commit()


def admit_plugin_call(db: Session, owner_id: str, plugin_id: str) -> SuperAssistantProcessPlugin:
    """Atomically admit only enabled revisions; host releases this count."""
    row = get_process_plugin(db, owner_id, plugin_id, lock=True)
    if row.state != PluginState.ENABLED.value:
        raise ProcessPluginValidationError("插件 revision 未启用")
    row.active_calls += 1
    db.flush()
    return row


def release_plugin_call(db: Session, owner_id: str, plugin_id: str) -> SuperAssistantProcessPlugin:
    row = get_process_plugin(db, owner_id, plugin_id, lock=True)
    if row.active_calls <= 0:
        raise ProcessPluginValidationError("插件没有在途调用")
    row.active_calls -= 1
    if row.state == PluginState.DRAINING.value and row.active_calls == 0:
        row.state = PluginState.DISABLED.value
    db.flush()
    return row
