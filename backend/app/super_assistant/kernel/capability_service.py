"""CapabilityRevision 的持久化快照服务。"""
from __future__ import annotations

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from .connectors import AgentDescriptor, TrustLevel
from .contracts import ContractError
from .models import CapabilityRevision


def persist_capability_revision(
    db: Session,
    descriptor: AgentDescriptor,
    *,
    source: str,
    trust_level: TrustLevel,
    manifest_hash: str,
    permissions: list[str] | None = None,
    side_effect_class: str = "external_async",
    workspace_scope: list[str] | None = None,
    network_scope: list[str] | None = None,
    secret_refs: list[str] | None = None,
) -> CapabilityRevision:
    existing = db.scalar(select(CapabilityRevision).where(CapabilityRevision.key == descriptor.key, CapabilityRevision.revision == descriptor.revision))
    values = {
        "source": source, "manifest_hash": manifest_hash,
        "trust_level": trust_level.value, "permissions": permissions or [],
        "input_schema": {}, "output_schema": {}, "side_effect_class": side_effect_class,
        "supports_stream": descriptor.supports_stream, "supports_cancel": descriptor.supports_cancel,
        "supports_approval": descriptor.supports_approval, "supports_artifact": descriptor.supports_artifact,
        "supports_query_status": descriptor.supports_query_status,
        "workspace_scope": list(workspace_scope or []),
        "network_scope": list(network_scope or []),
        "secret_refs": list(secret_refs or []), "enabled": True,
    }
    if existing is not None:
        # ``enabled`` is the live authorization bit.  It is intentionally
        # mutable during plugin/MCP disable, drain and re-enable; the
        # manifest and transport fields above remain immutable.
        if any(getattr(existing, key) != value for key, value in values.items() if key != "enabled"):
            raise ContractError("capability revision is immutable")
        return existing
    row = CapabilityRevision(key=descriptor.key, revision=descriptor.revision, **values)
    db.add(row)
    db.flush()
    return row


def revoke_capability_revisions(
    db: Session,
    keys: list[str] | tuple[str, ...] | set[str],
    *,
    revision: int | None = None,
) -> int:
    """Disable previously frozen capability snapshots.

    Capability rows are immutable identity snapshots; revocation changes only
    the live authorization bit.  Keeping the row (rather than deleting it)
    lets historical Calls remain auditable and prevents a disabled MCP tool
    from being recreated accidentally by a manifest refresh.
    """
    normalized = {str(key) for key in keys if str(key)}
    if not normalized:
        return 0
    # A few legacy SQLite fixtures are intentionally created before the
    # kernel migration.  Their MCP lifecycle must remain testable while the
    # production schema is upgraded; there is simply no snapshot to revoke.
    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table(CapabilityRevision.__tablename__):
        return 0
    statement = select(CapabilityRevision).where(CapabilityRevision.key.in_(normalized))
    if revision is not None:
        statement = statement.where(CapabilityRevision.revision == revision)
    rows = list(db.scalars(statement).all())
    for row in rows:
        row.enabled = False
    if rows:
        db.flush()
    return len(rows)


def set_capability_revision_enabled(
    db: Session,
    key: str,
    revision: int,
    *,
    enabled: bool,
) -> bool:
    """Toggle only the live authorization bit of an immutable snapshot."""
    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table(CapabilityRevision.__tablename__):
        return False
    row = db.scalar(select(CapabilityRevision).where(
        CapabilityRevision.key == str(key), CapabilityRevision.revision == int(revision),
    ))
    if row is None:
        return False
    row.enabled = bool(enabled)
    db.flush()
    return True
