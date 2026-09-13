"""CapabilityRevision 的持久化快照服务。"""
from __future__ import annotations

from sqlalchemy import select
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
) -> CapabilityRevision:
    existing = db.scalar(select(CapabilityRevision).where(CapabilityRevision.key == descriptor.key, CapabilityRevision.revision == descriptor.revision))
    values = {
        "source": source, "manifest_hash": manifest_hash,
        "trust_level": trust_level.value, "permissions": permissions or [],
        "input_schema": {}, "output_schema": {}, "side_effect_class": side_effect_class,
        "supports_stream": descriptor.supports_stream, "supports_cancel": descriptor.supports_cancel,
        "supports_approval": False, "supports_artifact": descriptor.supports_artifact,
        "supports_query_status": descriptor.supports_query_status,
        "workspace_scope": [], "network_scope": [], "secret_refs": [], "enabled": True,
    }
    if existing is not None:
        if any(getattr(existing, key) != value for key, value in values.items()):
            raise ContractError("capability revision is immutable")
        return existing
    row = CapabilityRevision(key=descriptor.key, revision=descriptor.revision, **values)
    db.add(row)
    db.flush()
    return row

