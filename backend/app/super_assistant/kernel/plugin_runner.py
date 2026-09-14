"""Contract for the isolated user-plugin runner.

The API/kernel must not infer an OS sandbox from a process host.  A production
runner is a separate service and receives only this bounded, owner-scoped
envelope over an internal durable transport.  The module intentionally does
not start processes or implement a pretend sandbox; it is the shared wire
contract used by the future rootless runner and by the fail-closed kernel
gate.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .contracts import ContractError


PLUGIN_RUNNER_PROTOCOL = "plugin-runner.v1"
PLUGIN_RUNNER_STREAM = "SA_PLUGIN_RUNNER_V1"
PLUGIN_RUNNER_SUBJECT = "sa.plugin.invoke"
PLUGIN_RUNNER_REPLY_PREFIX = "sa.plugin.reply."
PLUGIN_RUNNER_DURABLE = "sa-plugin-runner-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_INPUT_REF_BYTES = 1024 * 1024
_MAX_LEASE_REFS = 32
_MAX_REF_LENGTH = 512
_ENVELOPE_FIELDS = frozenset(
    {
        "request_id",
        "owner_id",
        "run_id",
        "call_id",
        "plugin_id",
        "revision",
        "manifest_hash",
        "capability_revision",
        "input_ref",
        "workspace_snapshot_ref",
        "secret_lease_refs",
        "deadline",
        "reply_subject",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "request_id",
        "owner_id",
        "run_id",
        "call_id",
        "plugin_id",
        "revision",
        "manifest_hash",
        "capability_revision",
        "event_seq",
        "kind",
        "status",
        "payload",
        "artifacts",
    }
)
_ARTIFACT_FIELDS = frozenset({"artifact_ref", "name", "mime_type", "size", "checksum"})
_EVENT_KINDS = frozenset({
    "progress", "approval_requested", "artifact", "completed", "failed", "cancelled", "unknown",
})
_EVENT_STATUS = {
    "progress": "running",
    "approval_requested": "waiting_approval",
    "artifact": "running",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "unknown": "unknown",
}
_MAX_EVENT_PAYLOAD_BYTES = 256 * 1024
_MAX_ARTIFACTS = 32


def _required_text(name: str, value: Any, *, max_length: int = _MAX_REF_LENGTH) -> str:
    result = str(value or "")
    if not result or len(result) > max_length or any(ord(char) < 0x20 for char in result):
        raise ContractError(f"plugin runner {name} is invalid")
    return result


@dataclass(frozen=True, slots=True)
class PluginInvocationEnvelope:
    """Immutable identity and capability snapshot for one plugin Call.

    ``secret_lease_refs`` are opaque lease identifiers, never secret values.
    The future runner must resolve them through a broker and bind the lease to
    this exact owner/run/call/manifest tuple before spawning a sandbox.
    """

    request_id: str
    owner_id: str
    run_id: str
    call_id: str
    plugin_id: str
    revision: int
    manifest_hash: str
    capability_revision: int
    input_ref: str
    workspace_snapshot_ref: str
    secret_lease_refs: tuple[str, ...]
    deadline: str
    reply_subject: str

    def __post_init__(self) -> None:
        for name in ("request_id", "owner_id", "run_id", "call_id", "plugin_id"):
            _required_text(name, getattr(self, name))
        if int(self.revision) < 1 or int(self.capability_revision) < 1:
            raise ContractError("plugin runner revisions must be positive")
        if not _SHA256.fullmatch(str(self.manifest_hash)):
            raise ContractError("plugin runner manifest_hash must be a lowercase sha256")
        if not isinstance(self.input_ref, str) or not self.input_ref:
            raise ContractError("plugin runner input_ref is required")
        if len(self.input_ref.encode("utf-8")) > _MAX_INPUT_REF_BYTES:
            raise ContractError("plugin runner input_ref exceeds 1 MiB")
        if not isinstance(self.workspace_snapshot_ref, str) or len(self.workspace_snapshot_ref) > _MAX_REF_LENGTH:
            raise ContractError("plugin runner workspace_snapshot_ref is invalid")
        if not isinstance(self.secret_lease_refs, tuple) or len(self.secret_lease_refs) > _MAX_LEASE_REFS:
            raise ContractError("plugin runner secret lease refs exceed limit")
        for value in self.secret_lease_refs:
            _required_text("secret_lease_ref", value)
        _required_text("deadline", self.deadline, max_length=80)
        subject = _required_text("reply_subject", self.reply_subject, max_length=255)
        if not subject.startswith(PLUGIN_RUNNER_REPLY_PREFIX) or any(char in subject for char in "*>"):
            raise ContractError("plugin runner reply_subject is outside the reply namespace")

    @property
    def msg_id(self) -> str:
        """JetStream deduplication key; redelivery must not spawn again."""

        return self.request_id

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["protocol"] = PLUGIN_RUNNER_PROTOCOL
        payload["secret_lease_refs"] = list(self.secret_lease_refs)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PluginInvocationEnvelope":
        if not isinstance(payload, Mapping) or payload.get("protocol") != PLUGIN_RUNNER_PROTOCOL:
            raise ContractError("unsupported plugin runner protocol")
        keys = set(payload) - {"protocol"}
        if keys != _ENVELOPE_FIELDS:
            raise ContractError("plugin runner envelope fields do not match the contract")
        leases = payload.get("secret_lease_refs")
        if not isinstance(leases, (list, tuple)):
            raise ContractError("plugin runner secret_lease_refs must be a list")
        try:
            revision = int(payload["revision"])
            capability_revision = int(payload["capability_revision"])
        except (TypeError, ValueError) as exc:
            raise ContractError("plugin runner revisions must be integers") from exc
        return cls(
            request_id=str(payload["request_id"]),
            owner_id=str(payload["owner_id"]),
            run_id=str(payload["run_id"]),
            call_id=str(payload["call_id"]),
            plugin_id=str(payload["plugin_id"]),
            revision=revision,
            manifest_hash=str(payload["manifest_hash"]),
            capability_revision=capability_revision,
            input_ref=payload["input_ref"],
            workspace_snapshot_ref=payload["workspace_snapshot_ref"],
            secret_lease_refs=tuple(str(item) for item in leases),
            deadline=str(payload["deadline"]),
            reply_subject=str(payload["reply_subject"]),
        )


@dataclass(frozen=True, slots=True)
class PluginRunnerEventEnvelope:
    """Bound, ordered runner-to-kernel event for progress and final results.

    Every event repeats the invocation identity.  The reconcile adapter must
    reject an event whose owner/run/call/plugin/revision or manifest hash does
    not match the frozen Call before acknowledging it.  ``artifacts`` contain
    only opaque references and integrity metadata; bytes and secret values
    never travel through this subject.
    """

    request_id: str
    owner_id: str
    run_id: str
    call_id: str
    plugin_id: str
    revision: int
    manifest_hash: str
    capability_revision: int
    event_seq: int
    kind: str
    status: str
    payload: Mapping[str, Any]
    artifacts: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for name in ("request_id", "owner_id", "run_id", "call_id", "plugin_id"):
            _required_text(name, getattr(self, name))
        if int(self.revision) < 1 or int(self.capability_revision) < 1:
            raise ContractError("plugin runner event revisions must be positive")
        if not _SHA256.fullmatch(str(self.manifest_hash)):
            raise ContractError("plugin runner event manifest_hash must be a lowercase sha256")
        if int(self.event_seq) < 0:
            raise ContractError("plugin runner event_seq must be non-negative")
        kind = _required_text("event kind", self.kind, max_length=32)
        if kind not in _EVENT_KINDS or self.status != _EVENT_STATUS[kind]:
            raise ContractError("plugin runner event kind/status is invalid")
        if not isinstance(self.payload, Mapping):
            raise ContractError("plugin runner event payload must be an object")
        try:
            payload_size = len(json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ContractError("plugin runner event payload is not JSON serializable") from exc
        if payload_size > _MAX_EVENT_PAYLOAD_BYTES:
            raise ContractError("plugin runner event payload exceeds 256 KiB")
        if not isinstance(self.artifacts, tuple) or len(self.artifacts) > _MAX_ARTIFACTS:
            raise ContractError("plugin runner event artifacts exceed limit")
        for artifact in self.artifacts:
            if not isinstance(artifact, Mapping) or set(artifact) != _ARTIFACT_FIELDS:
                raise ContractError("plugin runner artifact fields do not match the contract")
            _required_text("artifact_ref", artifact["artifact_ref"])
            _required_text("artifact name", artifact["name"], max_length=255)
            _required_text("artifact mime_type", artifact["mime_type"], max_length=127)
            try:
                size = int(artifact["size"])
            except (TypeError, ValueError) as exc:
                raise ContractError("plugin runner artifact size must be an integer") from exc
            if size < 0 or size > 1024 * 1024 * 1024:
                raise ContractError("plugin runner artifact size is out of bounds")
            if not _SHA256.fullmatch(str(artifact["checksum"])):
                raise ContractError("plugin runner artifact checksum must be a lowercase sha256")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["protocol"] = PLUGIN_RUNNER_PROTOCOL
        payload["artifacts"] = [dict(item) for item in self.artifacts]
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PluginRunnerEventEnvelope":
        if not isinstance(payload, Mapping) or payload.get("protocol") != PLUGIN_RUNNER_PROTOCOL:
            raise ContractError("unsupported plugin runner event protocol")
        keys = set(payload) - {"protocol"}
        if keys != _EVENT_FIELDS:
            raise ContractError("plugin runner event fields do not match the contract")
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, (list, tuple)):
            raise ContractError("plugin runner event artifacts must be a list")
        try:
            revision = int(payload["revision"])
            capability_revision = int(payload["capability_revision"])
            event_seq = int(payload["event_seq"])
        except (TypeError, ValueError) as exc:
            raise ContractError("plugin runner event numeric fields are invalid") from exc
        return cls(
            request_id=str(payload["request_id"]),
            owner_id=str(payload["owner_id"]),
            run_id=str(payload["run_id"]),
            call_id=str(payload["call_id"]),
            plugin_id=str(payload["plugin_id"]),
            revision=revision,
            manifest_hash=str(payload["manifest_hash"]),
            capability_revision=capability_revision,
            event_seq=event_seq,
            kind=str(payload["kind"]),
            status=str(payload["status"]),
            payload=payload["payload"],
            artifacts=tuple(artifacts),
        )
