"""HTTP chat uses the durable run ledger, independently of its SSE subscriber."""
from __future__ import annotations

import uuid

from app.ontologies.agent_runtime import run_service
from app.ontologies.agent_runtime.chat_service import stream_events
from app.ontologies.agent_runtime.models import AgentMessage
from app.ontologies.agent_runtime.orchestrator import run_agent_turn
from app.ontologies.agent_runtime.run_models import AgentRun
from app.ontologies.agent_runtime.schemas import ChatRequest
from tests.conftest import TestSession


def _fo(ontology_id: str) -> str:
    return f"/api/v2/formal/ontologies/{ontology_id}"


def _add_llm_config(db, admin_user, name: str) -> None:
    from app.models.model_config import ModelConfig
    db.add(ModelConfig(id=str(uuid.uuid4()), name=name, provider="openai",
                       config_type="llm", models=[f"{name}-model"],
                       created_by=admin_user.id))
    db.commit()


def test_chat_run_endpoint_reports_terminal_after_turn(
        client, auth_headers, ontology, db, admin_user, monkeypatch):
    oid = ontology["id"]
    _add_llm_config(db, admin_user, "run-status-fake")
    from app.ontologies.agent_runtime import llm_bridge
    seen = []
    def fake_chat(_kwargs, messages, _tools):
        seen.append(messages)
        return {"content": "查询完成。", "tool_calls": [],
                "usage": {"inputTokens": 5, "outputTokens": 3}}
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    response = client.post(f"{_fo(oid)}/agent/chat", headers=auth_headers,
                           json={"message": "查询订单", "stream": False,
                                 "runId": "run-status-terminal"})
    assert response.status_code == 200, response.text
    conv_id = response.json()["data"]["conversationId"]
    response = client.get(f"{_fo(oid)}/agent/chat/runs/run-status-terminal", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "succeeded"
    assert data["conversationId"] == conv_id
    assert data["startedAt"] and data["finishedAt"]
    messages = db.query(AgentMessage).filter(AgentMessage.conversation_id == conv_id).all()
    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message["content"] for message in seen[0] if message["role"] == "user"] == ["查询订单"]


def test_sync_repeated_submission_does_not_repeat_model_or_messages(
        client, auth_headers, ontology, db, admin_user, monkeypatch):
    _add_llm_config(db, admin_user, "idempotent-sync")
    from app.ontologies.agent_runtime import llm_bridge
    calls = []
    def fake_chat(*_):
        calls.append(1)
        return {"content": "完成", "tool_calls": [], "usage": None}
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    body = {"message": "重复提交", "stream": False, "runId": "sync-repeat"}
    first = client.post(f"{_fo(ontology['id'])}/agent/chat", headers=auth_headers, json=body)
    second = client.post(f"{_fo(ontology['id'])}/agent/chat", headers=auth_headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert len(calls) == 1
    assert db.query(AgentMessage).count() == 2


def test_chat_run_endpoint_rejects_unknown_ontology(client, auth_headers):
    response = client.get(f"{_fo('ontology-missing')}/agent/chat/runs/run-x", headers=auth_headers)
    assert response.status_code == 404


def test_stream_survives_client_disconnect(ontology, db, admin_user, monkeypatch):
    _add_llm_config(db, admin_user, "disconnect-fake")
    from app.ontologies.agent_runtime import llm_bridge
    dispatched = []
    monkeypatch.setattr("app.ontologies.agent_runtime.run_common._dispatch_port", dispatched.append)
    monkeypatch.setattr(llm_bridge, "chat", lambda *_: {
        "content": "断开后仍在执行的回答。", "tool_calls": [], "usage": None})
    stream = stream_events(
        ontology["id"], admin_user,
        ChatRequest(message="继续执行", runId="run-disconnect-survives"),
        db=db, run_turn_fn=run_agent_turn)
    assert dispatched == ["run-disconnect-survives"]
    assert '"type": "meta"' in next(stream)
    stream.close()
    assert db.get(AgentRun, dispatched[0]).status == "queued"
    run_service.execute_run(dispatched[0], db=db, session_factory=TestSession,
                            stream_model=False)
    db.expire_all()
    run = db.get(AgentRun, dispatched[0])
    assert run.status == "succeeded"
    messages = db.query(AgentMessage).filter(AgentMessage.conversation_id == run.conversation_id).all()
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[1].content == "断开后仍在执行的回答。"


def test_cancel_still_works_with_decoupled_stream(ontology, db, admin_user, monkeypatch):
    dispatched = []
    monkeypatch.setattr("app.ontologies.agent_runtime.run_common._dispatch_port", dispatched.append)
    stream = stream_events(
        ontology["id"], admin_user,
        ChatRequest(message="等待取消", runId="run-decouple-cancel"), db=db)
    next(stream)
    stream.close()
    cancelled = run_service.cancel_run(db, ontology["id"], admin_user, dispatched[0])
    assert cancelled["cancelled"] is True
    run_service.execute_run(dispatched[0], db=db, session_factory=TestSession,
                            run_turn_fn=lambda *_args, **_kwargs: (_ for _ in () ))
    db.expire_all()
    assert db.get(AgentRun, dispatched[0]).status == "cancelled"
    assert db.query(AgentMessage).count() == 1
