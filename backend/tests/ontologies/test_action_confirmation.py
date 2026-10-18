"""动作确认协议契约测试（YIYA-36）：prepare 幂等、一次确认、guard、执行幂等。"""
import uuid

import pytest

from app.ontologies.agent_confirmation import models as CM


def _fo(oid):
    return f"/api/v2/formal/ontologies/{oid}"


@pytest.fixture
def modeled(client, auth_headers, ontology, db):
    oid = ontology["id"]
    body = {
        "objectTypes": [
            {"id": "ot-order", "name": "Order", "displayName": "订单",
             "primaryKey": "order_no",
             "properties": [
                 {"id": "p1", "name": "order_no", "displayName": "订单号",
                  "type": "string", "required": True},
                 {"id": "p2", "name": "status", "displayName": "状态",
                  "type": "string"},
             ], "positionX": 0, "positionY": 0},
        ],
        "linkTypes": [],
        "actions": [
            {"id": "act-1", "name": "mark_paid", "displayName": "标记已支付",
             "objectTypeId": "ot-order", "requiresApproval": False,
             "parameters": [
                 {"name": "note", "displayName": "备注", "type": "string"},
                 {"name": "level", "displayName": "级别", "type": "number"},
             ],
             "rules": [{"type": "update_property", "name": "set-status",
                        "enabled": True, "order": 0,
                        "config": {"targetProperty": "status",
                                   "valueSource": "constant",
                                   "value": "\"paid\""}}]},
            {"id": "act-2", "name": "cancel_order", "displayName": "取消订单",
             "objectTypeId": "ot-order", "requiresApproval": True,
             "parameters": [],
             "rules": [{"type": "update_property", "name": "set-status",
                        "enabled": True, "order": 0,
                        "config": {"targetProperty": "status",
                                   "valueSource": "constant",
                                   "value": "\"cancelled\""}}]},
            {"id": "act-3", "name": "notify_party", "displayName": "通知外部系统",
             "objectTypeId": "ot-order", "requiresApproval": False,
             "parameters": [],
             "rules": [{
                 "id": "hook", "type": "webhook", "name": "notify partner",
                 "enabled": True, "order": 0,
                 "config": {"url": "http://127.0.0.1:9/hook",
                            "method": "POST",
                            "bodyTemplate": "{\"order\":\"{{object.order_no}}\"}"},
             }]},
        ],
        "functions": [],
        "instances": [
            {"id": "inst-o1", "objectTypeId": "ot-order",
             "properties": {"order_no": "SO-001", "status": "pending"}},
        ],
        "linkInstances": [],
    }
    r = client.put(f"{_fo(oid)}/full", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    pr = client.put(f"{_fo(oid)}/agent/profile", headers=auth_headers,
                    json={"allowedActionIds": ["act-1", "act-2", "act-3"]})
    assert pr.status_code == 200, pr.text
    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import ObjectInstance
    from app.models.ontology_version import OntologyVersion
    from app.ontologies.versions.evolution_service import snapshot_hash
    from app.ontologies.versions.router import _snapshot_formal

    project = db.query(OntologyProject).filter_by(id=oid).one()
    release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    snapshot = _snapshot_formal(db, oid)
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    for inst in db.query(ObjectInstance).filter_by(ontology_id=oid).all():
        inst.ontology_release_id = release.id
    db.commit()
    return {"oid": oid, "releaseId": str(release.id)}


@pytest.fixture
def conversation(db, modeled, auth_headers):
    """带提案消息的用户会话 — clientSubmissionId 的映射目标。"""
    from app.ontologies.agent_runtime.models import (
        AgentConversation, AgentMessage,
    )
    from app.models.user import User

    user = db.query(User).first()
    conv = AgentConversation(id=f"conv-{uuid.uuid4().hex[:8]}",
                             ontology_id=modeled["oid"], user_id=user.id,
                             title="t")
    db.add(conv)
    db.flush()
    user_msg = AgentMessage(id=f"msg-u-{uuid.uuid4().hex[:8]}",
                            conversation_id=conv.id, role="user",
                            content="把 SO-001 标记为已支付")
    db.add(user_msg)
    assistant_msg = AgentMessage(
        id=f"msg-a-{uuid.uuid4().hex[:8]}", conversation_id=conv.id,
        role="assistant", content="已准备提案",
        proposals=[{"kind": "action", "proposalId": "prop-1",
                    "actionId": "act-1", "actionName": "标记已支付",
                    "parameters": {"note": "user asked", "level": 2},
                    "targetInstanceId": "inst-o1",
                    "releaseId": modeled["releaseId"],
                    "requiresApproval": False, "status": "success",
                    "validationErrors": [], "effects": []}])
    db.add(assistant_msg)
    db.commit()
    return {"conv": conv, "userMsg": user_msg, "assistantMsg": assistant_msg}


def _prepare(client, headers, modeled, conv, **overrides):
    payload = {
        "sessionKind": "ontology_agent",
        "sessionId": conv["conv"].id,
        "clientSubmissionId": "prop-1",
        "actionId": "act-1",
        "releaseId": modeled["releaseId"],
        "targetInstanceId": "inst-o1",
        "parameters": {"note": "user asked", "level": 2},
    }
    payload.update(overrides)
    r = client.post(f"{_fo(modeled['oid'])}/agent/proposals",
                    headers=headers, json=payload)
    assert r.status_code in (200, 201), r.text
    return r


def _decide(client, headers, modeled, conv, cid, payload_hash, decision,
            decision_id="dec-1"):
    return client.post(
        f"{_fo(modeled['oid'])}/agent/proposals/{cid}/decision",
        headers=headers,
        json={"decision": decision, "sessionKind": "ontology_agent",
              "sessionId": conv["conv"].id, "decisionId": decision_id,
              "payloadHash": payload_hash})


def _execute(client, headers, modeled, conv, cid):
    return client.post(
        f"{_fo(modeled['oid'])}/agent/execute-proposal", headers=headers,
        json={"confirmationId": cid, "sessionKind": "ontology_agent",
              "sessionId": conv["conv"].id})


def _confirmation(db, cid):
    return db.query(CM.ActionConfirmation).filter_by(id=cid).first()


def test_prepare_replay_returns_same_cid(client, auth_headers, modeled,
                                         conversation):
    r1 = _prepare(client, auth_headers, modeled, conversation)
    assert r1.status_code == 201
    data1 = r1.json()["data"]
    assert data1["state"] == "prepared"
    assert data1["replayed"] is False
    assert data1["requiresApproval"] is False
    assert data1["display"]["release"]["id"] == modeled["releaseId"]

    r2 = _prepare(client, auth_headers, modeled, conversation)
    assert r2.status_code == 200
    data2 = r2.json()["data"]
    assert data2["confirmationId"] == data1["confirmationId"]
    assert data2["replayed"] is True


def test_same_slot_different_payload_conflict(client, auth_headers, modeled,
                                              conversation):
    _prepare(client, auth_headers, modeled, conversation)
    payload = {
        "sessionKind": "ontology_agent",
        "sessionId": conversation["conv"].id,
        "clientSubmissionId": "prop-1",
        "actionId": "act-1",
        "releaseId": modeled["releaseId"],
        "targetInstanceId": "inst-o1",
        "parameters": {"note": "changed", "level": 9},
    }
    r = client.post(f"{_fo(modeled['oid'])}/agent/proposals",
                    headers=auth_headers, json=payload)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "prepare_payload_conflict"


def test_invalid_parameters_rejected_without_side_effect(
        client, auth_headers, modeled, conversation):
    r = client.post(
        f"{_fo(modeled['oid'])}/agent/proposals", headers=auth_headers,
        json={"sessionKind": "ontology_agent",
              "sessionId": conversation["conv"].id,
              "clientSubmissionId": "prop-1", "actionId": "act-1",
              "releaseId": modeled["releaseId"],
              "targetInstanceId": "inst-o1",
              "parameters": {"note": "x", "level": "not-a-number"}})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_parameter"
    assert r.json()["detail"]["validationErrors"]


def test_approve_execute_success_and_idempotent(client, auth_headers,
                                                modeled, conversation, db):
    from app.models.ontology_formal import ObjectInstance

    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash

    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "approve")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["state"] == "dispatch_queued"

    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["state"] == "succeeded"
    assert data["executionLogId"]

    inst = db.query(ObjectInstance).filter_by(id="inst-o1").first()
    db.refresh(inst)
    assert inst.properties["status"] == "paid"

    # 重复执行/重复批准都是重放：恰好一条引擎日志、无二次副作用
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200
    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "approve")
    assert r.status_code == 200
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    logs = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == modeled["oid"],
        ActionExecutionLog.idempotency_key == f"proposal:{cid}").all()
    assert len(logs) == 1


def test_deny_has_no_business_effect(client, auth_headers, modeled,
                                     conversation, db):
    from app.models.ontology_formal import ObjectInstance

    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "deny")
    assert r.status_code == 200
    assert r.json()["data"]["state"] == "denied"
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200
    assert r.json()["data"]["state"] == "denied"
    inst = db.query(ObjectInstance).filter_by(id="inst-o1").first()
    db.refresh(inst)
    assert inst.properties["status"] == "pending"
    assert db.query(CM.ActionOperation).count() == 0


def test_reverse_decision_conflict(client, auth_headers, modeled,
                                   conversation, db):
    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "deny")
    assert r.status_code == 200
    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "approve", decision_id="dec-2")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "decision_conflict"


def test_execute_before_approval_rejected(client, auth_headers, modeled,
                                          conversation):
    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "confirmation_not_approved"


def test_old_execute_shape_rejected(client, auth_headers, modeled,
                                    conversation):
    r = client.post(f"{_fo(modeled['oid'])}/agent/execute-proposal",
                    headers=auth_headers,
                    json={"actionId": "act-1", "targetInstanceId": "inst-o1",
                          "parameters": {"note": "x"},
                          "releaseId": modeled["releaseId"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "confirmation_required"


def test_ttl_expiry(client, auth_headers, modeled, conversation, db):
    from datetime import datetime, timedelta, timezone

    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    confirmation = _confirmation(db, cid)
    confirmation.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    payload_hash = confirmation.payload_hash
    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "approve")
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "confirmation_expired"


def test_release_drift_expires_confirmation(client, auth_headers, modeled,
                                            conversation, db):
    from app.models.ontology import OntologyProject
    from app.models.ontology_version import OntologyVersion

    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    project = db.query(OntologyProject).filter_by(
        id=modeled["oid"]).one()
    old_release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    new_release = OntologyVersion(
        id=f"rel-{uuid.uuid4().hex[:8]}", ontology_id=project.id,
        version_number="v1", node_kind="release", lifecycle_status="released",
        created_by=old_release.created_by,
        snapshot_formal=old_release.snapshot_formal,
        snapshot_hash=old_release.snapshot_hash)
    db.add(new_release)
    db.flush()
    project.current_release_id = new_release.id
    db.commit()

    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "approve")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "release_context_changed"
    assert detail["currentState"] == "expired"
    assert _confirmation(db, cid).state == "expired"


def test_guard_blocks_same_payload_in_other_session(
        client, auth_headers, modeled, conversation, db):
    from app.ontologies.agent_runtime.models import (
        AgentConversation, AgentMessage,
    )
    from app.models.user import User

    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "approve")
    assert r.status_code == 200

    # 第二个会话（多标签）准备相同载荷：可建卡，但确认被 guard 拒绝
    user = db.query(User).first()
    conv2 = AgentConversation(id=f"conv-{uuid.uuid4().hex[:8]}",
                              ontology_id=modeled["oid"], user_id=user.id)
    db.add(conv2)
    db.flush()
    msg_u = AgentMessage(id=f"mu-{uuid.uuid4().hex[:6]}",
                         conversation_id=conv2.id, role="user", content="again")
    db.add(msg_u)
    db.add(AgentMessage(
        id=f"ma-{uuid.uuid4().hex[:6]}", conversation_id=conv2.id,
        role="assistant", content="re-prepared",
        proposals=[{"kind": "action", "proposalId": "prop-2",
                    "actionId": "act-1",
                    "parameters": {"note": "user asked", "level": 2},
                    "targetInstanceId": "inst-o1",
                    "releaseId": modeled["releaseId"],
                    "status": "success", "validationErrors": [],
                    "effects": []}]))
    db.commit()
    conv2_ctx = {"conv": conv2}
    cid2 = _prepare(client, auth_headers, modeled, conv2_ctx,
                    clientSubmissionId="prop-2").json()["data"][
                        "confirmationId"]
    assert cid2 != cid
    payload2 = _confirmation(db, cid2).payload_hash
    r = _decide(client, auth_headers, modeled, conv2_ctx, cid2, payload2,
                "approve")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "operation_unresolved"


def test_hitl_flow_pending_approval_then_reconcile(client, auth_headers,
                                                   modeled, conversation, db):
    from app.models.ontology_formal import ObjectInstance
    from app.ontologies.formal_modeling.models import ActionExecutionLog
    from app.ontologies.agent_confirmation import service as confirmation_svc

    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-2",
                   parameters={}).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "approve")
    assert r.status_code == 200
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["state"] == "pending_approval"
    assert data["requiresApproval"] is True
    approval_log_id = data["approvalLogId"]

    inst = db.query(ObjectInstance).filter_by(id="inst-o1").first()
    db.refresh(inst)
    assert inst.properties["status"] == "pending"   # 批准前不变更业务数据

    # 管理员批准 → 既有 HITL 技术执行（approval-execution 键）
    r = client.post(f"{_fo(modeled['oid'])}/action-logs/{approval_log_id}/decide",
                    headers=auth_headers,
                    json={"decision": "approved", "reason": "ok",
                          "releaseId": modeled["releaseId"]})
    assert r.status_code == 200, r.text
    decision = r.json()["data"]
    assert decision["executionLog"]["status"] == "success"

    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "succeeded"
    db.refresh(inst)
    assert inst.properties["status"] == "cancelled"
    # HITL 技术执行日志不再占用 proposal 键
    keys = [row.idempotency_key for row in db.query(
        ActionExecutionLog).filter_by(ontology_id=modeled["oid"]).all()]
    assert f"proposal:{cid}" in keys


def test_cross_user_get_returns_404(client, auth_headers, modeled,
                                   conversation, db, editor_user):
    from tests.conftest import TestSession

    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    token = _login(db, editor_user.username, "editor123")
    headers2 = {"Authorization": f"Bearer {token}"}
    r = client.get(
        f"{_fo(modeled['oid'])}/agent/proposals/{cid}",
        headers=headers2,
        params={"session_kind": "ontology_agent",
                "session_id": conversation["conv"].id})
    assert r.status_code == 404



# ------------------------------------------------- P1 整改：guard 释放缺陷


def _second_session_same_payload(client, auth_headers, modeled, db,
                                 action_id, parameters):
    """另一会话对同一载荷 prepare+approve：返回 (cid2, approve 响应)。"""
    from app.ontologies.agent_runtime.models import (
        AgentConversation, AgentMessage,
    )
    from app.models.user import User

    user = db.query(User).first()
    conv2 = AgentConversation(id=f"conv-{uuid.uuid4().hex[:8]}",
                              ontology_id=modeled["oid"], user_id=user.id)
    db.add(conv2)
    db.flush()
    db.add(AgentMessage(id=f"mu-{uuid.uuid4().hex[:6]}",
                        conversation_id=conv2.id, role="user", content="again"))
    db.add(AgentMessage(
        id=f"ma-{uuid.uuid4().hex[:6]}", conversation_id=conv2.id,
        role="assistant", content="re-prepared",
        proposals=[{"kind": "action", "proposalId": "prop-x",
                    "actionId": action_id, "parameters": parameters,
                    "targetInstanceId": "inst-o1",
                    "releaseId": modeled["releaseId"],
                    "status": "success", "validationErrors": [],
                    "effects": []}]))
    db.commit()
    r = client.post(
        f"{_fo(modeled['oid'])}/agent/proposals", headers=auth_headers,
        json={"sessionKind": "ontology_agent", "sessionId": conv2.id,
              "clientSubmissionId": "prop-x", "actionId": action_id,
              "releaseId": modeled["releaseId"],
              "targetInstanceId": "inst-o1", "parameters": parameters})
    assert r.status_code in (200, 201), r.text
    data = r.json()["data"]
    r2 = client.post(
        f"{_fo(modeled['oid'])}/agent/proposals/{data['confirmationId']}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "ontology_agent",
              "sessionId": conv2.id, "decisionId": "dx",
              "payloadHash": data["payloadHash"]})
    return data["confirmationId"], r2


def test_engine_exception_with_external_attempt_keeps_guard(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """P1 契约：引擎异常 + 未结案外部 attempt → unknown，guard 不释放。"""
    from app.ontologies.formal_modeling import action_engine as engine_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("engine crashed mid-effects")

    monkeypatch.setattr(engine_mod, "execute_action", _boom)
    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-3",
                   parameters={}).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    r = _decide(client, auth_headers, modeled, conversation, cid,
                payload_hash, "approve")
    assert r.status_code == 200, r.text
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["state"] == "unknown"
    assert data["failureCode"] == "execution_result_unknown"

    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    assert operation.state == "unknown"
    attempts = db.query(CM.ActionExternalAttempt).filter_by(
        operation_id=operation.id).all()
    assert attempts, "外部 attempt 必须在引擎调用前持久化"
    assert all(a.state in ("started", "unknown") for a in attempts)
    assert {a.stable_delivery_key for a in attempts} == {
        f"proposal-external:{cid}:{a.effect_ordinal}" for a in attempts}

    # guard 保留的持久证据：另一会话同载荷确认被拒
    _, r2 = _second_session_same_payload(
        client, auth_headers, modeled, db, "act-3", {})
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "operation_unresolved"


def test_engine_failed_result_with_attempt_stays_unknown(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """P1 契约：引擎返回 failed（无 uncertainty 标记）但存在未结案
    attempt 且 engine_entered 已落库 → 同样 unknown，不释放 guard。"""
    from app.ontologies.formal_modeling import action_engine as engine_mod

    monkeypatch.setattr(
        engine_mod, "execute_action",
        lambda *a, **k: {"status": "failed", "errorMessage": "hook 5xx"})
    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-3",
                   parameters={}).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["state"] == "unknown"
    _, r2 = _second_session_same_payload(
        client, auth_headers, modeled, db, "act-3", {})
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "operation_unresolved"


def test_local_failure_without_external_effects_releases_guard(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """无外部效果点的本地失败可证明未执行 → failed_not_executed 且
    guard 释放（另一会话同载荷可确认）。"""
    from app.ontologies.formal_modeling import action_engine as engine_mod

    monkeypatch.setattr(
        engine_mod, "execute_action",
        lambda *a, **k: {"status": "failed", "errorMessage": "validation"})
    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-1",
                   parameters={"note": "n", "level": 1}).json()[
                       "data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["state"] == "failed_not_executed"
    _, r2 = _second_session_same_payload(
        client, auth_headers, modeled, db, "act-1",
        {"note": "n", "level": 1})
    assert r2.status_code == 200, r2.text


def test_crash_after_commit_recovers_from_same_key_log(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """崩溃窗口：引擎已提交 success 日志（proposal:{cid} 键）后异常 →
    恢复按同 key 结案日志映射 succeeded，不重执行。"""
    from app.ontologies.formal_modeling import action_engine as engine_mod
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    def _commit_then_die(db, ontology_id, body, **kwargs):
        log = ActionExecutionLog(
            id=f"log-{uuid.uuid4().hex[:6]}", ontology_id=ontology_id,
            action_id=body.action_id, status="success",
            parameters=body.parameters,
            idempotency_key=body.idempotency_key)
        db.add(log)
        db.commit()
        raise RuntimeError("response lost after commit")

    monkeypatch.setattr(engine_mod, "execute_action", _commit_then_die)
    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-3",
                   parameters={}).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["state"] == "succeeded"
    assert data["executionLogId"]
    # 同 key 日志恰一条，未发生重复执行
    logs = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == modeled["oid"],
        ActionExecutionLog.idempotency_key == f"proposal:{cid}").all()
    assert len(logs) == 1

def _login(db, username, password):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"]
