"""direct 本体确认载荷契约（YIYA-36）：SSE/历史恢复共用服务端持久 display。

确认卡内容只能来自服务端确认记录（经 ontology_tools → assistant_facade 按
owner/conversation 校验），模型与前端都不能凭 confirmationId 编造；不可
验证/过期不恢复可点击卡，approve 在服务端同样被确认协议拒绝。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.ontologies.agent_confirmation import models as CM
from app.shared.database import Base
from app.super_assistant import conversation_service, ontology_tools, tools
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMcpServer,
    SuperAssistantMessage,
    SuperAssistantToolRun,
)

_DISPLAY = {
    "ontology": {"id": "ont-1", "name": "订单本体"},
    "action": {"id": "act-1", "name": "标记已支付"},
    "target": {"id": "inst-1", "label": "SO-001"},
    "parameters": [{"name": "note", "value": "user asked", "source": "request"}],
    "release": {"id": "rel-1", "version": 3},
    "requiresApproval": True,
}


def _prepare(tmp_path, confirmation_state="prepared"):
    engine = create_engine(f"sqlite:///{tmp_path / 'onto-payload.db'}")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantConversation.__table__,
        SuperAssistantMessage.__table__, SuperAssistantToolRun.__table__,
        SuperAssistantMcpServer.__table__, CM.ActionConfirmation.__table__,
    ])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        db.add(User(id="user-1", username="owner", email="o@example.com",
                    password_hash="x", role="editor"))
        db.add(User(id="user-2", username="other", email="t@example.com",
                    password_hash="x", role="editor"))
        db.add(SuperAssistantConversation(
            id="conv-1", owner_id="user-1", title="t", model_config_id=None))
        db.add(SuperAssistantMessage(
            id="msg-1", conversation_id="conv-1", role="user",
            content="确认执行", status="complete"))
        db.add(SuperAssistantMessage(
            id="msg-a", conversation_id="conv-1", role="assistant",
            content="", status="streaming"))
        expires = datetime.now(timezone.utc).replace(tzinfo=None) + (
            timedelta(hours=1) if confirmation_state == "prepared"
            else timedelta(hours=-1))
        db.add(CM.ActionConfirmation(
            id="conf-ok", actor_user_id="user-1", session_kind="super_assistant",
            session_id="conv-1", root_intent_id="msg-1",
            action_request_slot="slot-1", prepare_request_id="prep-1",
            origin="super_assistant", source_call_id="call-1",
            ontology_id="ont-1", release_id="rel-1", action_id="act-1",
            target_instance_id="inst-1",
            final_parameters={"note": "user asked"},
            parameter_sources={"note": {"kind": "request"}},
            requires_approval=True, payload_hash="hash-1",
            display_payload=_DISPLAY, state=confirmation_state,
            expires_at=expires,
        ))
        db.commit()
    return Session


def _ontology_run(db, run_id, *, cid="conf-ok", status="awaiting_confirmation"):
    run = SuperAssistantToolRun(
        id=run_id, conversation_id="conv-1", assistant_message_id="msg-a",
        call_id=run_id, tool_name="ontology_execute_action", server_id=None,
        arguments={"confirmationId": cid}, status=status,
        requires_confirmation=True)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def test_confirmation_display_for_run_validates_owner_and_conversation(tmp_path):
    Session = _prepare(tmp_path)
    with Session() as db:
        run = _ontology_run(db, "run-ok")
        payload = ontology_tools.confirmation_display_for_run(
            db, owner_id="user-1", conversation_id="conv-1", tool_run=run)
        # 服务端持久 display 原样透出（display 键与 kernel 侧统一）
        assert payload["confirmationId"] == "conf-ok"
        assert payload["state"] == "prepared"
        assert payload["payloadHash"] == "hash-1"
        assert payload["display"] == _DISPLAY
        assert payload["requiresApproval"] is True
        run.status = "running"
        message = db.get(SuperAssistantMessage, "msg-a")
        message.steps = [{"toolRunId": run.id,
                          "toolName": "ontology_execute_action",
                          "status": "running"}]
        confirmation = db.get(CM.ActionConfirmation, "conf-ok")
        for state, expected in (("unknown", "unknown"),
                                ("pending_approval", "pending_approval"),
                                ("succeeded", "success")):
            confirmation.state = state
            db.commit()
            ontology_tools.refresh_action_steps(db, "user-1", "conv-1", [message])
            assert message.steps[0]["status"] == expected
            assert json.loads(message.steps[0]["preview"])["state"] == state
        confirmation.state = "prepared"
        db.commit()
        # 跨 owner / 跨会话 / 不存在 / 缺 confirmationId：一律取不到
        assert ontology_tools.confirmation_display_for_run(
            db, owner_id="user-2", conversation_id="conv-1", tool_run=run) is None
        assert ontology_tools.confirmation_display_for_run(
            db, owner_id="user-1", conversation_id="conv-other", tool_run=run) is None
        assert ontology_tools.confirmation_display_for_run(
            db, owner_id="user-1", conversation_id="conv-1",
            tool_run=_ontology_run(db, "run-missing", cid="nope")) is None
        assert ontology_tools.confirmation_display_for_run(
            db, owner_id="user-1", conversation_id="conv-1",
            tool_run=_ontology_run(db, "run-empty", cid="")) is None


def test_pending_confirmation_restores_only_verifiable_prepared_card(tmp_path):
    Session = _prepare(tmp_path)
    user = User(id="user-1", username="owner", email="o@example.com",
                password_hash="x", role="editor")
    with Session() as db:
        assert tools.pending_confirmation(db, user, "conv-1") == {"data": None}

        run = _ontology_run(db, "run-ok")
        restored = tools.pending_confirmation(db, user, "conv-1")["data"]
        assert restored["toolRunId"] == run.id
        assert restored["toolName"] == "ontology_execute_action"
        assert restored["serverName"] == "multica"
        assert restored["arguments"] == {"confirmationId": "conf-ok"}
        assert restored["ontologyConfirmation"]["display"] == _DISPLAY

        # 会话不归属当前用户：404，不泄露
        other = User(id="user-2", username="other", email="t@example.com",
                     password_hash="x", role="editor")
        with pytest.raises(HTTPException) as exc:
            tools.pending_confirmation(db, other, "conv-1")
        assert exc.value.status_code == 404
        run.status = "approved"
        db.commit()
        assert tools.pending_confirmation(db, user, "conv-1") == {"data": None}


def test_pending_confirmation_expired_confirmation_not_restorable(tmp_path):
    Session = _prepare(tmp_path, confirmation_state="expired")
    user = User(id="user-1", username="owner", email="o@example.com",
                password_hash="x", role="editor")
    with Session() as db:
        _ontology_run(db, "run-exp")
        assert tools.pending_confirmation(db, user, "conv-1") == {"data": None}


def test_pending_confirmation_restores_mcp_card_with_original_tool_name(tmp_path):
    Session = _prepare(tmp_path)
    user = User(id="user-1", username="owner", email="o@example.com",
                password_hash="x", role="editor")
    with Session() as db:
        db.add(SuperAssistantMcpServer(
            id="srv-1", owner_id="user-1", name="ext_tools", builtin_key=None,
            transport="streamable_http", url="https://mcp.example.test/sse",
            header_names=[], args=[], env_names=[], enabled=True,
            require_confirmation=True, tool_manifest=[]))
        db.add(SuperAssistantToolRun(
            id="run-mcp", conversation_id="conv-1", assistant_message_id="msg-a",
            call_id="c-mcp", tool_name="mcp__ext_tools__lookup", server_id="srv-1",
            arguments={"q": 1}, status="awaiting_confirmation",
            requires_confirmation=True))
        db.commit()
        restored = tools.pending_confirmation(db, user, "conv-1")["data"]
        assert restored["toolRunId"] == "run-mcp"
        assert restored["serverName"] == "ext_tools"
        assert restored["toolName"] == "lookup"  # 去命名空间前缀，与 SSE 同形
        assert restored["arguments"] == {"q": 1}
        assert "ontologyConfirmation" not in restored


def test_decide_tool_run_rejects_fabricated_confirmation_server_side(tmp_path):
    Session = _prepare(tmp_path)
    user = User(id="user-1", username="owner", email="o@example.com",
                password_hash="x", role="editor")
    with Session() as db:
        run = _ontology_run(db, "run-fake", cid="fabricated")
        with pytest.raises(HTTPException) as exc:
            conversation_service.decide_tool_run(
                "run-fake", type("Body", (), {"decision": "approve"})(), db, user)
        assert exc.value.status_code in (404, 410)
        db.refresh(run)
        # 服务端拒绝后本地 tool_run 不得被误置为 approved
        assert run.status == "awaiting_confirmation"


def test_pending_confirmation_envelope_is_json_serializable(tmp_path):
    Session = _prepare(tmp_path)
    user = User(id="user-1", username="owner", email="o@example.com",
                password_hash="x", role="editor")
    with Session() as db:
        _ontology_run(db, "run-json")
        payload = tools.pending_confirmation(db, user, "conv-1")
        assert set(payload) == {"data"}
        assert json.dumps(payload, ensure_ascii=False)


def test_pending_confirmation_restores_join_assertion_card(tmp_path):
    Session = _prepare(tmp_path)
    user = User(id="user-1", username="owner", email="o@example.com",
                password_hash="x", role="editor")
    with Session() as db:
        db.add(SuperAssistantToolRun(
            id="run-join", conversation_id="conv-1", assistant_message_id="msg-a",
            call_id="c-join", tool_name="ontology_register_join_assertion",
            server_id=None, arguments={
                "leftOntologyId": "ont-a", "leftObjectTypeId": "ot-order",
                "leftProperty": "order_no", "rightOntologyId": "ont-b",
                "rightObjectTypeId": "ot-ship", "rightProperty": "ref_no",
                "keyType": "string", "businessMeaning": "订单号=运单参考号",
            }, status="awaiting_confirmation", requires_confirmation=True))
        db.commit()
        restored = tools.pending_confirmation(db, user, "conv-1")["data"]
        assert restored["toolName"] == "ontology_register_join_assertion"
        display = restored["joinAssertion"]
        assert display["leftOntologyId"] == "ont-a"
        assert display["keyType"] == "string"
        assert display["businessMeaning"] == "订单号=运单参考号"
        # 参数不完整的声明卡不恢复
        db.query(SuperAssistantToolRun).filter(
            SuperAssistantToolRun.id == "run-join").one().arguments = {
                "leftOntologyId": "ont-a"}
        db.commit()
        assert tools.pending_confirmation(db, user, "conv-1") == {"data": None}
