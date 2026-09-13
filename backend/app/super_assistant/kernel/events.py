"""kernel.v1 事件封套和最小 payload registry。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from .contracts import (
    CallOutcome,
    CallStatus,
    CancelReason,
    ContractError,
    RunStatus,
    StepCloseReason,
    TurnCloseReason,
    validate_call,
)

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
    "run.pause_requested": frozenset({"reason", "pause_reason", "actor"}),
    "run.recovery_requested": frozenset({"reason", "lease_epoch", "diagnostic_ref"}),
    "run.child_bound": frozenset({"parent_run_id", "child_run_id", "join_policy", "required"}),
    "run.child_joined": frozenset({"parent_run_id", "child_run_id", "join_policy", "required"}),
    "turn.started": frozenset({"turn_id", "turn_no", "trigger_ref"}),
    "turn.closed": frozenset({"turn_id", "reason"}),
    "step.started": frozenset({"step_id", "step_no"}),
    "step.closed": frozenset({"step_id", "reason"}),
    "context.snapshot": frozenset({"snapshot_id", "pack_hash", "source_refs"}),
    "request.header": frozenset({"snapshot_id", "pack_hash", "model", "prompt_ref", "capability_snapshot_ref"}),
    "assistant.delta": frozenset({"attempt_id", "delta_seq", "content_ref"}),
    "assistant.message": frozenset({"attempt_id", "message_ref"}),
    "call.intent": frozenset({"call_id", "capability_key", "capability_revision", "input_snapshot_ref", "side_effect_class", "idempotency_key"}),
    "call.progress": frozenset({"call_id", "progress_seq", "connector_id", "provider_event_id"}),
    "call.outcome_changed": frozenset({"call_id", "status", "outcome", "evidence_ref", "connector_id", "provider_event_id"}),
    "attempt.started": frozenset({"attempt_id", "provider_status", "request_ref", "started_at"}),
    "attempt.result": frozenset({"attempt_id", "provider_status", "result_ref", "error_ref", "safe_to_retry", "token_usage_ref", "cost_ref"}),
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
        validate_payload(self.event_type, self.payload, schema_version=self.schema_version)
        if self.actor.get("kind") not in ACTOR_KINDS:
            raise ContractError("unknown actor kind")
        mode = self.redaction.get("mode")
        if mode not in REDACTION_MODES:
            raise ContractError("unknown redaction mode")
        if mode == "reference" and not self.redaction.get("content_ref"):
            raise ContractError("reference redaction requires content_ref")
        if mode == "reference" and not self.redaction.get("checksum"):
            raise ContractError("reference redaction requires checksum")


def validate_payload(event_type: str, payload: dict[str, Any], *, schema_version: int = 1) -> None:
    if type(schema_version) is not int or schema_version != 1:
        raise ContractError(f"unsupported schema_version: {schema_version!r}")
    if event_type not in EVENT_TYPES:
        raise ContractError(f"unknown event type: {event_type}")
    missing = sorted(REQUIRED_PAYLOAD[event_type] - payload.keys())
    if missing:
        raise ContractError(f"missing payload fields for {event_type}: {', '.join(missing)}")
    _validate_payload_values(event_type, payload)
    _validate_payload_size(payload)


def _validate_payload_values(event_type: str, payload: dict[str, Any]) -> None:
    try:
        if event_type == "run.status_changed":
            RunStatus(payload["from"])
            RunStatus(payload["to"])
        elif event_type == "run.cancel_requested":
            CancelReason(payload["cancel_reason"])
        elif event_type == "run.cancel_timeout":
            terminal_status = RunStatus(payload["run_terminal_status"])
            if terminal_status not in {RunStatus.CANCELLED, RunStatus.EXPIRED, RunStatus.FAILED}:
                raise ContractError("cancel timeout must close the Run as cancelled, expired or failed")
        elif event_type == "call.outcome_changed":
            validate_call(CallStatus(payload["status"]), CallOutcome(payload["outcome"]))
        elif event_type == "turn.closed":
            TurnCloseReason(payload["reason"])
        elif event_type == "step.closed":
            StepCloseReason(payload["reason"])
        elif event_type == "approval.decided" and payload["decision"] not in ("approved", "denied"):
            raise ContractError("approval decision must be approved or denied")
    except ContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise ContractError(f"invalid payload values for {event_type}: {exc}") from exc


def _validate_payload_size(payload: dict[str, Any]) -> None:
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 64 * 1024:
        raise ContractError("event payload exceeds 64 KiB; store large content as an Artifact reference")
