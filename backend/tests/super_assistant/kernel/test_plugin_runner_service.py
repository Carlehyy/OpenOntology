"""Contract tests for the external plugin-runner boundary."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.super_assistant.kernel.contracts import ContractError
from app.super_assistant.kernel.plugin_runner import PluginInvocationEnvelope, PluginRunnerEventEnvelope
from app.super_assistant.kernel.plugin_runner_service import (
    FileRootlessSandboxAttestation,
    JOURNAL_FINISHED,
    JOURNAL_UNKNOWN,
    PluginRunnerJournal,
    PluginRunnerService,
    RootlessSandboxUnavailable,
)
from app.super_assistant.models import SuperAssistantPluginRunnerJournal


def _envelope(**overrides):
    values = {
        "request_id": "req-runner-1", "owner_id": "owner-1", "run_id": "run-1",
        "call_id": "call-1", "plugin_id": "plugin-1", "revision": 1,
        "manifest_hash": "a" * 64, "capability_revision": 1,
        "input_ref": "artifact://input", "workspace_snapshot_ref": "workspace://run-1",
        "secret_lease_refs": (), "deadline": "2030-01-01T00:00:00+00:00",
        "reply_subject": "sa.plugin.reply.req-runner-1",
    }
    values.update(overrides)
    return PluginInvocationEnvelope(**values)


def test_journal_transitions_are_monotonic_and_idempotent(db):
    envelope = _envelope()
    journal = PluginRunnerJournal(db)
    row = journal.accept(envelope)
    db.commit()
    assert row.state == "accepted"
    assert journal.accept(envelope).id == row.id
    journal.transition(row, "validated")
    journal.transition(row, "spawned")
    journal.transition(row, "finished", outcome={"kind": "completed"})
    assert db.get(SuperAssistantPluginRunnerJournal, row.id).state == JOURNAL_FINISHED
    with pytest.raises(ContractError, match="transition"):
        journal.transition(row, "unknown")


@pytest.mark.asyncio
async def test_service_missing_rootless_attestation_fails_closed_and_is_idempotent(db):
    events = []
    service = PluginRunnerService(lambda: db)

    async def publish(event):
        events.append(event)

    assert await service.handle(_envelope().to_payload(), publish)
    row = db.scalar(select(SuperAssistantPluginRunnerJournal))
    assert row.state == JOURNAL_UNKNOWN
    assert events[-1].kind == "unknown"
    # Redelivery replays the exact event so a prior transport failure can be
    # repaired; NATS Msg-Id deduplicates the transport-side effect.
    assert await service.handle(_envelope().to_payload(), publish)
    assert len(events) == 2
    assert events[1] == events[0]
    assert db.scalar(select(SuperAssistantPluginRunnerJournal)).state == JOURNAL_UNKNOWN


def test_attestation_requires_immutable_rootless_probe(tmp_path):
    path = tmp_path / "attestation.json"
    verifier = FileRootlessSandboxAttestation(path)
    envelope = _envelope()
    with pytest.raises(RootlessSandboxUnavailable):
        verifier.verify(envelope)
    path.write_text(json.dumps({"protocol": "plugin-runner.sandbox.v1", "verified": True, "mode": "rootless", "runtime": "podman", "image_digest": "sha256:" + "b" * 64,
        "network_scope_enforced": True, "workspace_scope_enforced": True,
        "secret_broker": "scope-broker.v1"}), encoding="utf-8")
    path.chmod(0o600)
    verifier.verify(envelope)
    path.chmod(0o666)
    with pytest.raises(RootlessSandboxUnavailable, match="writable"):
        verifier.verify(envelope)


def test_attestation_requires_scope_enforcement_and_secret_broker(tmp_path):
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps({
        "protocol": "plugin-runner.sandbox.v1", "verified": True,
        "mode": "rootless", "runtime": "podman",
        "image_digest": "sha256:" + "b" * 64,
    }), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(RootlessSandboxUnavailable, match="network scope"):
        FileRootlessSandboxAttestation(path).verify(_envelope())


@pytest.mark.asyncio
async def test_service_requires_contract_events_from_launcher(db, tmp_path):
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps({"protocol": "plugin-runner.sandbox.v1", "verified": True, "mode": "rootless", "runtime": "podman", "image_digest": "sha256:" + "b" * 64,
        "network_scope_enforced": True, "workspace_scope_enforced": True,
        "secret_broker": "scope-broker.v1"}), encoding="utf-8")
    path.chmod(0o600)

    async def launcher(envelope):
        return PluginRunnerEventEnvelope(
            request_id=envelope.request_id, owner_id=envelope.owner_id, run_id=envelope.run_id,
            call_id=envelope.call_id, plugin_id=envelope.plugin_id, revision=envelope.revision,
            manifest_hash=envelope.manifest_hash, capability_revision=envelope.capability_revision,
            event_seq=0, kind="completed", status="completed", payload={"ok": True}, artifacts=(),
        )

    service = PluginRunnerService(lambda: db, attestation=FileRootlessSandboxAttestation(path), launcher=launcher)
    events = []
    async def publish(event):
        events.append(event)
    assert await service.handle(_envelope().to_payload(), publish)
    row = db.scalar(select(SuperAssistantPluginRunnerJournal))
    assert row.state == JOURNAL_FINISHED
    assert events[0].kind == "completed"


@pytest.mark.asyncio
async def test_terminal_event_is_replayed_when_reply_publish_fails(db, tmp_path):
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps({
        "protocol": "plugin-runner.sandbox.v1", "verified": True,
        "mode": "rootless", "runtime": "podman",
        "image_digest": "sha256:" + "b" * 64,
        "network_scope_enforced": True, "workspace_scope_enforced": True,
        "secret_broker": "scope-broker.v1",
    }), encoding="utf-8")
    path.chmod(0o600)

    async def launcher(envelope):
        return PluginRunnerEventEnvelope(
            request_id=envelope.request_id, owner_id=envelope.owner_id,
            run_id=envelope.run_id, call_id=envelope.call_id,
            plugin_id=envelope.plugin_id, revision=envelope.revision,
            manifest_hash=envelope.manifest_hash,
            capability_revision=envelope.capability_revision, event_seq=0,
            kind="completed", status="completed", payload={"ok": True}, artifacts=(),
        )

    service = PluginRunnerService(
        lambda: db, attestation=FileRootlessSandboxAttestation(path), launcher=launcher,
    )
    attempts = 0
    delivered = []

    async def flaky_publish(event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("reply transport unavailable")
        delivered.append(event)

    with pytest.raises(RuntimeError, match="reply transport"):
        await service.handle(_envelope().to_payload(), flaky_publish)
    row = db.scalar(select(SuperAssistantPluginRunnerJournal))
    assert row.state == JOURNAL_FINISHED
    assert row.last_event["event_seq"] == 0

    assert await service.handle(_envelope().to_payload(), flaky_publish)
    assert delivered and delivered[0].kind == "completed"
