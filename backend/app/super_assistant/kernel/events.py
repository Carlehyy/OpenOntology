"""kernel.v1 事件封套和最小 payload registry。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import ContractError

EVENT_TYPES = frozenset({
    "run.created", "run.status_changed", "run.expiry_requested", "run.cancel_requested",
    "run.cancel_timeout", "run.pause_requested", "run.recovery_requested",
    "run.child_bound", "run.child_joined", "turn.started", "turn.closed", "step.started", "step.closed",
    "context.snapshot", "request.header", "assistant.delta", "assistant.message",
    "call.intent", "call.progress", "call.outcome_changed", "attempt.started", "attempt.result",
    "inbox.appended", "inbox.claimed", "inbox.expired", "approval.requested", "approval.decided",
    "approval.expired", "approval.revoked", "artifact.declared", "artifact.chunked", "artifact.completed",
    "projection.applied", "projection.failed", "source.tombstoned",
})
ACTOR_KINDS = frozenset({"user", "worker", "reconciler", "connector", "plugin", "system"})
REDACTION_MODES = frozenset({"none", "reference", "redacted"})

REQUIRED_PAYLOAD: dict[str, frozenset[str]] = {
    "run.created": frozenset({"execution_version", "conversation_id"}),
    "run.status_changed": frozenset({"from", "to", "reason", "actor", "version"}),
    "run.expiry_requested": frozenset({"reason", "deadline", "unresolved_call_ids"}),
    "run.cancel_requested": frozenset({"reason", "cancel_reason", "actor"}),
    "run.cancel_timeout": frozenset({"reason", "cancel_deadline", "unresolved_call_ids", "run_terminal_status"}),
    "run.pause_requested": frozenset({"reason", "actor"}),
    "run.recovery_requested": frozenset({"reason", "lease_epoch", "diagnostic_ref"}),
    "run.child_bound": frozenset({"parent_run_id", "child_run_id", "join_policy", "required"}),
    "run.child_joined": frozenset({"parent_run_id", "child_run_id", "join_policy", "required"}),
    "turn.started": frozenset({"turn_id", "turn_no", "trigger_ref"}),
    "turn.closed": frozenset({"turn_id", "reason"}),
    "step.started": frozenset({"step_id", "step_no"}),
    "step.closed": frozenset({"step_id", "reason"}),
    "context.snapshot": frozenset({"snapshot_id", "pack_hash", "source_refs"}),
    "request.header": frozenset({"snapshot_id", "model", "capability_snapshot_ref"}),
    "assistant.delta": frozenset({"attempt_id", "delta_seq", "content_ref"}),
    "assistant.message": frozenset({"attempt_id", "message_ref"}),
    "call.intent": frozenset({"call_id", "capability_key", "capability_revision", "input_snapshot_ref", "side_effect_class", "idempotency_key"}),
    "call.progress": frozenset({"call_id", "progress_seq", "connector_id", "provider_event_id"}),
    "call.outcome_changed": frozenset({"status", "outcome", "evidence_ref", "connector_id", "provider_event_id"}),
    "attempt.started": frozenset({"attempt_id", "provider_status", "request_ref", "started_at"}),
    "attempt.result": frozenset({"attempt_id", "provider_status", "safe_to_retry"}),
    "inbox.appended": frozenset({"inbox_id", "kind", "target_ref", "expiry_policy"}),
    "inbox.claimed": frozenset({"inbox_id", "claim_token", "claim_expires_at", "actor"}),
    "inbox.expired": frozenset({"inbox_id", "kind", "target_ref", "question_id", "question_expires_at", "expiry_policy", "accepted_at"}),
    "approval.requested": frozenset({"approval_id", "run_id", "call_id", "scope_snapshot_ref", "expires_at"}),
    "approval.decided": frozenset({"approval_id", "decision", "actor", "decided_at", "authorization_hash"}),
    "approval.expired": frozenset({"approval_id", "reason", "actor", "occurred_at"}),
    "approval.revoked": frozenset({"approval_id", "reason", "actor", "occurred_at"}),
    "artifact.declared": frozenset({"artifact_id", "kind", "mime_type", "size", "checksum", "storage_ref", "visibility"}),
    "artifact.chunked": frozenset({"artifact_id", "chunk_index", "offset", "checksum"}),
    "artifact.completed": frozenset({"artifact_id", "checksum", "integrity_status", "business_status"}),
    "projection.applied": frozenset({"projection", "cursor"}),
    "projection.failed": frozenset({"projection", "cursor", "error_ref"}),
    "source.tombstoned": frozenset({"source_ref", "reason", "tombstone_at"}),
}


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    run_id: str
    seq: int
    event_type: str
    schema_version: int
    occurred_at: datetime
    actor: dict[str, str]
    causation_id: str
    correlation_id: str
    command_id: str
    idempotency_key: str
    payload: dict[str, Any]
    redaction: dict[str, Any]

    def validate(self) -> None:
        if self.seq < 0:
            raise ContractError("event seq must be non-negative")
        if self.schema_version < 1:
            raise ContractError("schema_version must be positive")
        if self.event_type not in EVENT_TYPES:
            raise ContractError(f"unknown event type: {self.event_type}")
        if self.actor.get("kind") not in ACTOR_KINDS:
            raise ContractError("unknown actor kind")
        mode = self.redaction.get("mode")
        if mode not in REDACTION_MODES:
            raise ContractError("unknown redaction mode")
        if mode == "reference" and not self.redaction.get("content_ref"):
            raise ContractError("reference redaction requires content_ref")
        if mode == "reference" and not self.redaction.get("checksum"):
            raise ContractError("reference redaction requires checksum")
        required = REQUIRED_PAYLOAD.get(self.event_type, frozenset())
        missing = sorted(required - self.payload.keys())
        if missing:
            raise ContractError(f"missing payload fields for {self.event_type}: {', '.join(missing)}")


def validate_payload(event_type: str, payload: dict[str, Any]) -> None:
    if event_type not in EVENT_TYPES:
        raise ContractError(f"unknown event type: {event_type}")
    missing = sorted(REQUIRED_PAYLOAD[event_type] - payload.keys())
    if missing:
        raise ContractError(f"missing payload fields for {event_type}: {', '.join(missing)}")
