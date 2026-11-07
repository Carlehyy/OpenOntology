"""HTTP chat uses the durable run ledger, independently of its SSE subscriber."""
from __future__ import annotations

import uuid

import pytest

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



def _ledger_counts(db):
    from app.ontologies.agent_runtime.models import AgentConversation
    from app.ontologies.agent_runtime.run_models import AgentRunEvent
    return tuple(db.query(model).count() for model in (
        AgentConversation, AgentMessage, AgentRun, AgentRunEvent))


def _copy_release(db, source_ontology_id, admin_user, **overrides):
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.versions.models import OntologyVersion
    current = db.get(OntologyProject, source_ontology_id).current_release_id
    original = db.get(OntologyVersion, current)
    values = dict(ontology_id=source_ontology_id, version_number="v-next",
                  created_by=admin_user.id, node_kind="release",
                  lifecycle_status="released", snapshot_formal=original.snapshot_formal,
                  snapshot_hash=original.snapshot_hash)
    values.update(overrides)
    release = OntologyVersion(**values)
    db.add(release)
    db.flush()
    return release.id


def test_default_release_is_returned_and_persisted_by_sync_chat(
        client, auth_headers, ontology, db, admin_user, monkeypatch):
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.agent_runtime.models import AgentConversation
    from app.ontologies.agent_runtime.run_models import AgentRunEvent
    oid = ontology["id"]
    expected = db.get(OntologyProject, oid).current_release_id
    _add_llm_config(db, admin_user, "default-release-sync")
    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", lambda *_: {
        "content": "当前发布目录可用。", "tool_calls": [], "usage": None})
    response = client.post(f"{_fo(oid)}/agent/chat", headers=auth_headers,
                           json={"message": "查看目录", "stream": False,
                                 "runId": "default-release-sync"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    db.expire_all()
    run = db.get(AgentRun, "default-release-sync")
    conversation = db.get(AgentConversation, run.conversation_id)
    meta = db.query(AgentRunEvent).filter_by(
        run_id=run.id, event_type="meta").order_by(AgentRunEvent.seq).first()
    assert expected and data["releaseId"] == expected
    assert run.release_id == conversation.ontology_release_id == expected
    assert meta.payload["releaseId"] == expected


def test_default_release_replay_and_worker_keep_original_pin(
        ontology, db, admin_user):
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.agent_runtime.models import AgentConversation
    project = db.get(OntologyProject, ontology["id"])
    original = project.current_release_id
    body = ChatRequest(message="核对钉定", runId="default-release-replay", stream=False)
    first = run_service.submit_run(db, project.id, admin_user, body, dispatch=False)
    replacement = _copy_release(db, project.id, admin_user)
    project.current_release_id = replacement
    db.commit()
    repeated = run_service.submit_run(db, project.id, admin_user, body, dispatch=False)
    assert repeated == first and first["releaseId"] == original
    project.current_release_id = None
    db.commit()
    assert run_service.submit_run(db, project.id, admin_user, body, dispatch=False) == first
    project.current_release_id = replacement
    db.commit()
    seen = []

    def worker(_db, _oid, _user, _question, **kwargs):
        seen.append(kwargs["release_id"])
        yield {"type": "answer", "content": "钉定保持。", "citations": [],
               "proposals": [], "usage": None}

    run_service.execute_run(first["runId"], db=db, session_factory=TestSession,
                            run_turn_fn=worker, stream_model=False)
    db.expire_all()
    assert seen == [original]
    assert db.get(AgentRun, first["runId"]).release_id == original
    assert db.get(AgentConversation, first["conversationId"]).ontology_release_id == original
    fresh = run_service.submit_run(db, project.id, admin_user,
        ChatRequest(message="新回合", runId="default-release-new"), dispatch=False)
    assert fresh["releaseId"] == replacement


def test_missing_release_never_rebinds_historical_or_unpinned_conversation(
        ontology, db, admin_user, editor_user):
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.agent_runtime.models import AgentConversation
    from app.ontologies.agent_runtime.application_errors import AgentRuntimeApplicationError
    project = db.get(OntologyProject, ontology["id"])
    original = project.current_release_id
    replacement = _copy_release(db, project.id, admin_user)
    historical = AgentConversation(ontology_id=project.id, user_id=admin_user.id,
                                   ontology_release_id=original, title="固定旧版")
    unpinned = AgentConversation(ontology_id=project.id, user_id=admin_user.id,
                                 ontology_release_id=None, title="未钉定")
    other = AgentConversation(ontology_id=project.id, user_id=editor_user.id,
                              ontology_release_id=replacement, title="他人会话")
    db.add_all([historical, unpinned, other])
    project.current_release_id = replacement
    db.commit()
    for conversation, explicit, kind, code in (
        (historical, None, "conflict", "release_context_changed"),
        (historical, replacement, "conflict", "conversation_release_mismatch"),
        (unpinned, None, "conflict", "conversation_release_missing"),
        (other, None, "forbidden", None),
    ):
        before = _ledger_counts(db)
        with pytest.raises(AgentRuntimeApplicationError) as caught:
            run_service.submit_run(db, project.id, admin_user,
                ChatRequest(message="继续", conversationId=conversation.id,
                            releaseId=explicit), dispatch=False)
        assert caught.value.kind == kind
        if code:
            assert caught.value.detail["code"] == code
            assert "新建会话" in caught.value.detail["message"]
        assert _ledger_counts(db) == before
    assert historical.ontology_release_id == original
    assert unpinned.ontology_release_id is None


@pytest.mark.parametrize("case", ["missing", "draft", "superseded", "foreign"])
def test_invalid_default_release_fails_before_ledger_writes(
        case, client, auth_headers, ontology, db, admin_user):
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.versions.models import OntologyVersion
    project = db.get(OntologyProject, ontology["id"])
    if case == "missing":
        project.current_release_id = None
    elif case == "foreign":
        other = OntologyProject(name="foreign", domain="test", created_by=admin_user.id)
        db.add(other)
        db.flush()
        foreign = _copy_release(db, project.id, admin_user, ontology_id=other.id)
        project.current_release_id = foreign
    else:
        release = db.get(OntologyVersion, project.current_release_id)
        release.node_kind = "draft" if case == "draft" else "release"
        release.lifecycle_status = "editing" if case == "draft" else "superseded"
    db.commit()
    before = _ledger_counts(db)
    response = client.post(f"{_fo(project.id)}/agent/chat", headers=auth_headers,
                           json={"message": "不能执行", "stream": False})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == ("current_release_missing" if case == "missing"
                              else "current_release_invalid")
    assert "发布本体" in detail["message"]
    assert _ledger_counts(db) == before


@pytest.mark.parametrize("case", ["stale", "draft", "foreign"])
def test_explicit_invalid_release_still_fails_before_ledger_writes(
        case, client, auth_headers, ontology, db, admin_user):
    from app.ontologies.projects.models import OntologyProject
    project = db.get(OntologyProject, ontology["id"])
    values = {}
    if case == "draft":
        values.update(node_kind="draft", lifecycle_status="editing")
    if case == "foreign":
        other = OntologyProject(name="foreign", domain="test", created_by=admin_user.id)
        db.add(other)
        db.flush()
        values["ontology_id"] = other.id
    explicit = _copy_release(db, project.id, admin_user, **values)
    db.commit()
    before = _ledger_counts(db)
    response = client.post(f"{_fo(project.id)}/agent/chat", headers=auth_headers,
                           json={"message": "不能执行", "stream": False,
                                 "releaseId": explicit})
    assert response.status_code == 422, response.text
    assert _ledger_counts(db) == before
