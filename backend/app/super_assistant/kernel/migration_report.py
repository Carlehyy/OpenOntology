"""Inventory and explicit backfill boundary for legacy Super Assistant data.

The report remains read-only by default.  ``backfill_legacy_data`` is the only
mutation entry point and requires ``apply=True``; every generated kernel row
contains the immutable legacy source and migration id so an operator can audit
or roll it back without guessing a Run/assistant binding.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantDelegation,
    SuperAssistantMemory,
    SuperAssistantMcpServer,
    SuperAssistantPalaceFile,
    SuperAssistantRemoteAgent,
    SuperAssistantSkill,
)
from .contracts import CallOutcome, CallStatus, RunStatus, StepCloseReason, TurnCloseReason
from .models import (
    CapabilityRevision,
    ExecutionCall,
    ExecutionEvent,
    ExecutionRun,
    ExecutionStep,
    ExecutionTurn,
)
from .store import _hash_payload, _new_id, append_event
from .connectors import TrustLevel


@dataclass(frozen=True, slots=True)
class LegacyDisposition:
    source: str
    total: int
    mappable: int
    readonly: int
    rule: str


@dataclass(frozen=True, slots=True)
class LegacyBackfillResult:
    """Stable operator-facing result; no raw secret/config values are included."""

    migration_id: str
    mode: str
    mutated: bool
    created_runs: tuple[str, ...] = ()
    created_capabilities: tuple[str, ...] = ()
    readonly: tuple[dict[str, str], ...] = ()
    skipped: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["created_runs"] = list(self.created_runs)
        value["created_capabilities"] = list(self.created_capabilities)
        value["readonly"] = list(self.readonly)
        value["skipped"] = list(self.skipped)
        return value


def _count(db: Session, model: type[Any], owner_id: str | None) -> int:
    stmt = select(func.count()).select_from(model)
    if owner_id is not None and hasattr(model, "owner_id"):
        stmt = stmt.where(model.owner_id == owner_id)
    return int(db.scalar(stmt) or 0)


def _owner_filter(model: type[Any], owner_id: str | None):
    return [model.owner_id == owner_id] if owner_id is not None and hasattr(model, "owner_id") else []


def _legacy_mappable_delegations(db: Session, owner_id: str | None = None) -> list[Any]:
    rows = db.scalars(
        select(SuperAssistantDelegation)
        .where(*_owner_filter(SuperAssistantDelegation, owner_id))
        .order_by(SuperAssistantDelegation.created_at, SuperAssistantDelegation.id)
    ).all()
    # The FK normally guarantees the conversation; the explicit checks also
    # protect imports from old databases where constraints were not enforced.
    return [
        row for row in rows
        if row.super_conversation_id and row.assistant_key and row.owner_id
        and db.scalar(select(SuperAssistantConversation.id).where(
            SuperAssistantConversation.id == row.super_conversation_id,
            SuperAssistantConversation.owner_id == row.owner_id,
        )) is not None
    ]


def _source_ref(kind: str, source_id: str) -> dict[str, str]:
    tables = {
        "delegation": "super_assistant_delegations",
        "delegations": "super_assistant_delegations",
        "memory": "super_assistant_memories",
        "palace_file": "super_assistant_palace_files",
        "mcp": "super_assistant_mcp_servers",
        "skill": "super_assistant_skills",
        "remote_agent": "super_assistant_remote_agents",
        # assistant capabilities are keyed by assistant_hub key rather than a
        # persisted row; retain a synthetic namespace without claiming a table.
        "assistant": "assistant_hub.capability",
    }
    table = tables.get(kind, f"legacy:{kind}")
    return {
        "table": table,
        "kind": kind,
        "id": str(source_id),
        "revision": "legacy",
        "locator": f"{table}://{source_id}",
        "recipe_revision": "legacy.v1",
        "extraction_id": f"legacy:{kind}:{source_id}",
    }


def _legacy_capability_key(kind: str, source_id: str) -> str:
    # IDs are UUIDs in current schema but truncation protects old imports with
    # unexpectedly long identifiers while retaining a deterministic key.
    return f"legacy.{kind}.{str(source_id)[:220]}"


def _manifest_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_legacy_capability(
    db: Session,
    *,
    kind: str,
    source_id: str,
    owner_id: str,
    manifest: Any,
    transport: str,
    migration_id: str,
    supports_stream: bool = False,
    supports_query_status: bool = False,
) -> tuple[CapabilityRevision, bool]:
    key = _legacy_capability_key(kind, source_id)
    source_ref = _source_ref(kind, source_id)
    metadata = {
        "legacy_migration_id": migration_id,
        "legacy_source": source_ref,
        "owner_id": owner_id,
    }
    values = {
        "source": f"legacy_{kind}",
        "manifest_hash": _manifest_hash(manifest),
        "trust_level": TrustLevel.USER_UNTRUSTED.value,
        "permissions": [],
        "input_schema": metadata,
        "output_schema": {},
        "side_effect_class": "external_async" if kind == "remote_agent" else "external_sync",
        "supports_stream": supports_stream,
        "supports_cancel": False,
        "supports_approval": True,
        "supports_artifact": False,
        "supports_query_status": supports_query_status,
        "workspace_scope": [],
        "network_scope": [transport] if transport else [],
        "secret_refs": [],
        "enabled": True,
    }
    # A capability key identifies the legacy source, while each migration id
    # represents an immutable snapshot of that source.  Reusing revision 1 for
    # a later snapshot would either raise on legitimate source changes or make
    # rollback of one batch disable another batch's capability.
    revisions = db.scalars(
        select(CapabilityRevision)
        .where(CapabilityRevision.key == key)
        .order_by(CapabilityRevision.revision.desc())
        .with_for_update()
    ).all()
    for existing in revisions:
        existing_metadata = existing.input_schema if isinstance(existing.input_schema, dict) else {}
        if (existing_metadata.get("legacy_migration_id") == migration_id
                and existing_metadata.get("owner_id") == owner_id):
            immutable_values = {field: value for field, value in values.items() if field != "enabled"}
            if any(getattr(existing, field) != value for field, value in immutable_values.items()):
                raise ValueError(f"legacy capability revision is immutable: {key}@{existing.revision}")
            return existing, False
    next_revision = (revisions[0].revision + 1) if revisions else 1
    row = CapabilityRevision(key=key, revision=next_revision, **values)
    db.add(row)
    db.flush()
    return row, True


def _status_mapping(status: str) -> tuple[str, str, str, str, str]:
    """Return run status, call status, outcome, turn reason, step reason."""
    normalized = (status or "").strip().lower()
    if normalized in {"answered", "complete", "completed", "success"}:
        return (RunStatus.COMPLETED.value, CallStatus.CLOSED.value, CallOutcome.COMPLETED.value,
                TurnCloseReason.COMPLETED.value, StepCloseReason.DECISION_COMPLETE.value)
    if normalized in {"cancelled", "canceled"}:
        return (RunStatus.CANCELLED.value, CallStatus.CLOSED.value, CallOutcome.CANCELLED_CONFIRMED.value,
                TurnCloseReason.CANCELLED.value, StepCloseReason.CANCELLED.value)
    if normalized in {"failed", "error"}:
        return (RunStatus.FAILED.value, CallStatus.CLOSED.value, CallOutcome.FAILED.value,
                TurnCloseReason.FAILED.value, StepCloseReason.FAILED.value)
    # running, timeout and unknown outcomes remain explicitly unresolved.  A
    # timeout is not rewritten as failure because the old path cannot prove
    # whether the remote assistant completed after the last poll.
    return (RunStatus.WAITING_EXTERNAL.value, CallStatus.WAITING_EXTERNAL.value, CallOutcome.OUTCOME_UNKNOWN.value,
            TurnCloseReason.WAITING_EXTERNAL.value, StepCloseReason.WAITING_EXTERNAL.value)


def build_legacy_migration_report(db: Session, *, owner_id: str | None = None) -> dict[str, Any]:
    """Return a report without changing a single legacy row.

    ``readonly`` is intentionally explicit: rows that cannot be represented by
    kernel.v1 remain available to the legacy read paths and are never silently
    rebound to another Run or capability revision.
    """
    delegation_total = _count(db, SuperAssistantDelegation, owner_id)
    delegation_mappable = len(_legacy_mappable_delegations(db, owner_id))
    memory_total = _count(db, SuperAssistantMemory, owner_id)
    palace_total = _count(db, SuperAssistantPalaceFile, owner_id)
    mcp_total = _count(db, SuperAssistantMcpServer, owner_id)
    skill_total = _count(db, SuperAssistantSkill, owner_id)
    remote_agent_total = _count(db, SuperAssistantRemoteAgent, owner_id)

    rows = [
        LegacyDisposition("delegation", delegation_total, delegation_mappable, delegation_total - delegation_mappable,
                          "回填 legacy Run/Call；不改变 assistant_key 或 conversation_ref；缺少会话引用的行只读"),
        LegacyDisposition("memory", memory_total, 0, memory_total,
                          "保留 legacy/source 与原文；标记 risk=unknown，不伪造 source_ref"),
        LegacyDisposition("palace_file", palace_total, 0, palace_total,
                          "保留 legacy 文件与 content_hash；无 kernel Run 归属的历史事实只读，不升级为 current"),
        LegacyDisposition("mcp_server", mcp_total, mcp_total, 0,
                          "通过 manifest 生成 immutable CapabilityRevision；不把旧配置提升为 trusted"),
        LegacyDisposition("skill", skill_total, skill_total, 0,
                          "通过 manifest/文件 revision 生成 immutable CapabilityRevision；保留原 owner scope"),
        LegacyDisposition("remote_agent", remote_agent_total, remote_agent_total, 0,
                          "通过 endpoint/模式生成 immutable CapabilityRevision；token 不复制、不提升信任"),
    ]
    return {
        "schema_version": "kernel.v1.legacy-disposition.v1",
        "owner_id": owner_id,
        "mutated": False,
        "rows": [asdict(row) for row in rows],
        "totals": {
            "legacy_rows": sum(row.total for row in rows),
            "mappable_rows": sum(row.mappable for row in rows),
            "readonly_rows": sum(row.readonly for row in rows),
        },
        "review_required": [row.source for row in rows if row.readonly],
    }


def _append_legacy_run(
    db: Session, row: Any, *, migration_id: str, capability: CapabilityRevision,
) -> tuple[ExecutionRun, bool]:
    """Import one delegation as historical facts without creating dispatch work."""
    source = _source_ref("delegations", row.id)
    key = f"legacy-delegation:{row.id}"
    existing = db.scalar(select(ExecutionRun).where(
        ExecutionRun.execution_version == "legacy",
        ExecutionRun.idempotency_key == key,
        ExecutionRun.owner_id == row.owner_id,
    ).with_for_update())
    if existing is not None:
        snapshot = json.loads(existing.binding_snapshot_ref or "{}")
        if snapshot.get("legacy_source") != source:
            raise ValueError(f"legacy idempotency collision: {row.id}")
        return existing, False
    run_status, call_status, outcome, turn_reason, step_reason = _status_mapping(row.status)
    payload = {
        "legacy_migration_id": migration_id,
        "legacy_source": source,
        "assistant_key": row.assistant_key,
        "conversation_ref": row.conversation_ref,
        "status": row.status,
    }
    run = ExecutionRun(
        id=_new_id(), owner_id=row.owner_id, conversation_id=row.super_conversation_id,
        execution_version="legacy", status=run_status,
        cancel_reason="user" if run_status == RunStatus.CANCELLED.value else None,
        wait_reason="legacy_outcome_unknown" if outcome == CallOutcome.OUTCOME_UNKNOWN.value else None,
        goal=row.summary.strip() or f"Legacy delegation to {row.assistant_key}",
        deadline=None, version=1, next_event_seq=0, lease_epoch=0,
        idempotency_key=key, payload_hash=_hash_payload(payload), binding_mode="legacy",
        binding_snapshot_ref=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        join_policy="all", finished_at=row.updated_at if run_status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value} else None,
    )
    db.add(run)
    db.flush()
    command_id = f"legacy-migration:{migration_id}:{row.id}"
    append_event(db, run, event_type="run.created", payload={
        "execution_version": "legacy", "conversation_id": row.super_conversation_id,
        "legacy_source": source,
    }, actor={"kind": "system"}, command_id=command_id, idempotency_key=f"{key}:created")
    append_event(db, run, event_type="run.status_changed", payload={
        "from": RunStatus.QUEUED.value, "to": run_status, "reason": "legacy_backfill",
        "actor": "system", "version": run.version,
    }, actor={"kind": "system"}, command_id=command_id, idempotency_key=f"{key}:status")
    turn = ExecutionTurn(run_id=run.id, turn_no=0, trigger_ref=f"legacy://delegation/{row.id}", status="closed", close_reason=turn_reason, closed_at=row.updated_at)
    db.add(turn); db.flush()
    append_event(db, run, event_type="turn.started", payload={"turn_id": turn.id, "turn_no": 0, "trigger_ref": turn.trigger_ref}, actor={"kind": "system"}, command_id=command_id, idempotency_key=f"{key}:turn-start")
    append_event(db, run, event_type="turn.closed", payload={"turn_id": turn.id, "reason": turn_reason}, actor={"kind": "system"}, command_id=command_id, idempotency_key=f"{key}:turn-close")
    step = ExecutionStep(turn_id=turn.id, step_no=0, request_snapshot_ref=f"legacy://delegation/{row.id}/request", status="closed", close_reason=step_reason, closed_at=row.updated_at)
    db.add(step); db.flush()
    append_event(db, run, event_type="step.started", payload={"step_id": step.id, "step_no": 0}, actor={"kind": "system"}, command_id=command_id, idempotency_key=f"{key}:step-start")
    append_event(db, run, event_type="step.closed", payload={"step_id": step.id, "reason": step_reason}, actor={"kind": "system"}, command_id=command_id, idempotency_key=f"{key}:step-close")
    call = ExecutionCall(
        run_id=run.id, turn_id=turn.id, step_id=step.id, call_index=0,
        capability_key=capability.key, capability_revision=capability.revision,
        target_ref=row.conversation_ref, input_snapshot_ref=f"legacy://delegation/{row.id}/input",
        side_effect_class="external_async", idempotency_key=f"{key}:call", status=call_status,
        outcome=outcome, remote_task_ref=row.conversation_ref,
        evidence_ref=f"legacy://delegation/{row.id}/evidence",
    )
    db.add(call); db.flush()
    append_event(db, run, event_type="call.intent", payload={
        "call_id": call.id, "capability_key": capability.key, "capability_revision": capability.revision,
        "input_snapshot_ref": call.input_snapshot_ref, "side_effect_class": call.side_effect_class,
        "idempotency_key": call.idempotency_key,
    }, actor={"kind": "system"}, command_id=command_id, idempotency_key=f"{key}:call-intent")
    provider_event_id = f"legacy:{row.id}:outcome"
    append_event(db, run, event_type="call.outcome_changed", payload={
        "call_id": call.id, "status": call.status, "outcome": call.outcome, "evidence_ref": call.evidence_ref,
        "connector_id": "legacy", "provider_event_id": provider_event_id,
    }, actor={"kind": "system"}, command_id=command_id, idempotency_key=f"{key}:call-outcome", connector_id="legacy", provider_event_id=provider_event_id)
    return run, True


def backfill_legacy_data(
    db: Session, *, owner_id: str | None = None, migration_id: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Dry-run by default; apply historical facts only with explicit opt-in."""
    migration_id = migration_id or f"legacy-{uuid.uuid4().hex}"
    report = build_legacy_migration_report(db, owner_id=owner_id)
    readonly: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    created_runs: list[str] = []
    created_capabilities: list[str] = []
    if not apply:
        report.update({"migration_id": migration_id, "mode": "dry_run", "mutated": False})
        return report
    try:
        for row in db.scalars(select(SuperAssistantDelegation).where(*_owner_filter(SuperAssistantDelegation, owner_id)).order_by(SuperAssistantDelegation.id)).all():
            if row not in _legacy_mappable_delegations(db, owner_id):
                readonly.append({"source": "delegation", "id": row.id, "reason": "missing_owner_or_conversation_or_assistant_key"})
                continue
            existing_run = db.scalar(select(ExecutionRun).where(
                ExecutionRun.execution_version == "legacy",
                ExecutionRun.idempotency_key == f"legacy-delegation:{row.id}",
                ExecutionRun.owner_id == row.owner_id,
            ))
            if existing_run is not None:
                skipped.append({"source": "delegation", "id": row.id, "reason": "already_backfilled"})
                continue
            cap, cap_created = _ensure_legacy_capability(
                db, kind="assistant", source_id=row.assistant_key, owner_id=row.owner_id,
                manifest={"assistant_key": row.assistant_key}, transport="assistant_hub", migration_id=migration_id,
            )
            if cap_created: created_capabilities.append(cap.key)
            run, created = _append_legacy_run(db, row, migration_id=migration_id, capability=cap)
            if created: created_runs.append(run.id)
            else: skipped.append({"source": "delegation", "id": row.id, "reason": "already_backfilled"})
        for model, kind, transport in ((SuperAssistantMcpServer, "mcp", "mcp"), (SuperAssistantSkill, "skill", "local"), (SuperAssistantRemoteAgent, "remote_agent", "rap.v1")):
            for row in db.scalars(select(model).where(*_owner_filter(model, owner_id)).order_by(model.id)).all():
                # Timestamps/counters are mutable operational state; they must
                # not make an otherwise identical capability revision appear
                # different on a replay after SQLite/Postgres timezone casting.
                manifest = {column.name: getattr(row, column.name) for column in model.__table__.columns
                            if column.name not in {"headers_encrypted", "env_encrypted", "token_encrypted"}
                            and not column.name.endswith("_at")}
                cap, created = _ensure_legacy_capability(
                    db, kind=kind, source_id=row.id, owner_id=row.owner_id, manifest=manifest,
                    transport=transport, migration_id=migration_id,
                    supports_stream=bool(kind == "mcp" and row.transport in {"sse", "streamable_http"}),
                    supports_query_status=bool(kind == "remote_agent" and row.mode == "pull"),
                )
                if created: created_capabilities.append(cap.key)
                else: skipped.append({"source": kind, "id": row.id, "reason": "already_backfilled"})
        # Memory and Palace are source/read models, not execution facts.  Do
        # not manufacture a Run ownership or provenance link for them.
        for model, kind, reason in ((SuperAssistantMemory, "memory", "legacy memory has no source_ref"), (SuperAssistantPalaceFile, "palace_file", "legacy palace fact has no execution Run provenance")):
            for row in db.scalars(select(model).where(*_owner_filter(model, owner_id))).all():
                readonly.append({"source": kind, "id": row.id, "reason": reason})
        db.commit()
    except Exception:
        db.rollback()
        raise
    result = LegacyBackfillResult(migration_id, "apply", bool(created_runs or created_capabilities), tuple(created_runs), tuple(created_capabilities), tuple(readonly), tuple(skipped)).as_dict()
    result.update({"schema_version": report["schema_version"], "owner_id": owner_id, "rows": report["rows"], "totals": report["totals"], "review_required": report["review_required"]})
    return result


def rollback_legacy_backfill(
    db: Session, *, migration_id: str, owner_id: str | None = None, apply: bool = False,
) -> dict[str, Any]:
    """Report or explicitly tombstone only rows tagged with one migration id.

    Rollback never deletes execution events/facts: a historical backfill is an
    audit record.  Generated capabilities are disabled and generated Runs get
    a source tombstone event, so operators can stop their use without erasing
    the evidence used to explain what happened.
    """
    runs = db.scalars(select(ExecutionRun).where(ExecutionRun.execution_version == "legacy")).all()
    target_runs: list[ExecutionRun] = []
    for run in runs:
        try: metadata = json.loads(run.binding_snapshot_ref or "{}")
        except (TypeError, ValueError): continue
        if metadata.get("legacy_migration_id") == migration_id and (owner_id is None or run.owner_id == owner_id):
            target_runs.append(run)
    caps = []
    for cap in db.scalars(select(CapabilityRevision)).all():
        metadata = cap.input_schema if isinstance(cap.input_schema, dict) else {}
        if metadata.get("legacy_migration_id") == migration_id and (owner_id is None or metadata.get("owner_id") == owner_id): caps.append(cap)
    result = {"schema_version": "kernel.v1.legacy-disposition.v1", "migration_id": migration_id, "mode": "rollback" if apply else "rollback_dry_run", "mutated": False, "run_ids": [r.id for r in target_runs], "capability_keys": [c.key for c in caps]}
    if not apply: return result
    for run in target_runs:
        try:
            metadata = json.loads(run.binding_snapshot_ref or "{}")
        except (TypeError, ValueError):
            metadata = {}
        source = metadata.get("legacy_source")
        if not isinstance(source, dict) or not source.get("kind") or not source.get("id") or not source.get("locator"):
            continue
        marker = f"legacy-rollback:{migration_id}:{run.id}"
        already = db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.run_id == run.id,
            ExecutionEvent.event_type == "source.tombstoned",
            ExecutionEvent.idempotency_key == marker,
        ))
        if already is None:
            append_event(db, run, event_type="source.tombstoned", payload={
                "source_ref": source, "reason": "legacy_backfill_rollback", "tombstone_at": _utcnow_iso(),
            }, actor={"kind": "system"}, command_id=marker, idempotency_key=marker)
            run.version += 1
    for cap in caps:
        cap.enabled = False
    db.commit(); result["mutated"] = bool(target_runs or caps)
    return result


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
