import uuid
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from app.models.user import User
from app.super_assistant.models import SuperAssistantConversation, SuperAssistantRemoteAgent
from app.shared.encryption import encrypt
from app.super_assistant.kernel.callbacks import append_agent_callback, receive_agent_callback
from app.super_assistant.kernel.schemas import AgentCallbackRequest
from app.super_assistant.kernel.capability_service import persist_capability_revision
from app.super_assistant.kernel.connectors import AgentDescriptor, TrustLevel
from app.super_assistant.kernel.contracts import ContractError
from app.super_assistant.kernel.models import CapabilityRevision, ExecutionCall, ExecutionEvent
from app.super_assistant.kernel.store import create_run


def test_capability_revision_and_callback_are_immutable_and_scoped(db):
    owner = User(id=str(uuid.uuid4()), username=f"cap-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="cap")
    db.add(conversation); db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="goal", idempotency_key="create")
    call = ExecutionCall(run_id=run.id, call_index=0, capability_key="external.agent", capability_revision=1, side_effect_class="external_async", idempotency_key="call", status="waiting_external", outcome="remote_running")
    db.add(call); db.flush()
    descriptor = AgentDescriptor(agent_id="a1", key="external.agent", revision=1, transport="rap.v1", supports_query_status=True)
    persist_capability_revision(db, descriptor, source="user", trust_level=TrustLevel.USER_UNTRUSTED, manifest_hash="h")
    db.commit()
    with pytest.raises(ContractError, match="immutable"):
        persist_capability_revision(db, descriptor, source="user", trust_level=TrustLevel.VERIFIED, manifest_hash="different")
    append_agent_callback(db, owner_id=owner.id, run_id=run.id, call_id=call.id, connector_id="connector-1", provider_event_id="event-1", event_type="call.outcome_changed", payload={"call_id": call.id, "status": "closed", "outcome": "completed", "evidence_ref": "artifact://a", "connector_id": "connector-1", "provider_event_id": "event-1"})
    db.commit()
    assert db.query(ExecutionEvent).filter_by(provider_event_id="event-1").count() == 1
    with pytest.raises(KeyError):
        append_agent_callback(db, owner_id="other", run_id=run.id, call_id=call.id, connector_id="connector-1", provider_event_id="event-2", event_type="call.progress", payload={"call_id": call.id, "progress_seq": 1, "connector_id": "connector-1", "provider_event_id": "event-2"})


def test_callback_connector_is_fenced_by_call_target(db):
    owner = User(id=str(uuid.uuid4()), username=f"cap-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="cap-target")
    db.add(conversation); db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="goal", idempotency_key="create-target")
    call = ExecutionCall(run_id=run.id, call_index=0, capability_key="external.agent", capability_revision=1, target_ref="connector-1", side_effect_class="external_async", idempotency_key="call-target", status="waiting_external", outcome="remote_running")
    db.add(call); db.commit()
    with pytest.raises(ContractError, match="does not match"):
        append_agent_callback(db, owner_id=owner.id, run_id=run.id, call_id=call.id, connector_id="connector-2", provider_event_id="event-target", event_type="call.progress", payload={"call_id": call.id, "progress_seq": 1, "connector_id": "connector-2", "provider_event_id": "event-target"})


def test_hmac_callback_ingress_is_authenticated(db, monkeypatch):
    owner = User(id=str(uuid.uuid4()), username=f"cb-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conversation = SuperAssistantConversation(owner_id=owner.id, title="callback")
    db.add(conversation); db.flush()
    remote = SuperAssistantRemoteAgent(owner_id=owner.id, key="remote.callback", label="callback", endpoint="", token_encrypted=encrypt("callback-secret"), enabled=True, mode="pull")
    db.add(remote); db.flush()
    run, _ = create_run(db, owner_id=owner.id, conversation_id=conversation.id, goal="goal", idempotency_key="callback-run")
    call = ExecutionCall(run_id=run.id, call_index=0, capability_key="external.agent", capability_revision=1, target_ref=remote.id, side_effect_class="external_async", idempotency_key="callback-call", status="waiting_external", outcome="remote_running")
    db.add(call); db.commit()
    callback_payload = {"status": "closed", "outcome": "completed", "evidence_ref": None, "content": "done"}
    payload_hash = hashlib.sha256(json.dumps(callback_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    body = AgentCallbackRequest(connector_id=remote.id, request_id="request-1", provider_event_id="provider-1", payload_hash=payload_hash, event_type="call.outcome_changed", payload=callback_payload)
    timestamp = int(time.time())
    message = f"{timestamp}.request-1.{call.id}.provider-1.{payload_hash}".encode()
    signature = "sha256=" + hmac.new(b"callback-secret", message, hashlib.sha256).hexdigest()
    async def reconcile(_payload):
        return True
    monkeypatch.setattr("app.super_assistant.kernel.runtime.reconcile_execution_message", reconcile)
    assert receive_agent_callback(run.id, call.id, body, db, str(timestamp), signature)["accepted"] is True
