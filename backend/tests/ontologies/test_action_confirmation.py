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
        proposals=[
            {"kind": "action", "proposalId": "prop-1",
             "actionId": "act-1", "actionName": "标记已支付",
             "parameters": {"note": "user asked", "level": 2},
             "targetInstanceId": "inst-o1",
             "releaseId": modeled["releaseId"],
             "requiresApproval": False, "status": "success",
             "validationErrors": [], "effects": []},
            {"kind": "action", "proposalId": "prop-k1",
             "actionId": "act-1", "actionName": "标记已支付",
             "parameters": {"note": "k", "level": 1},
             "targetInstanceId": "inst-o1",
             "releaseId": modeled["releaseId"],
             "requiresApproval": False, "status": "success",
             "validationErrors": [], "effects": []},
            {"kind": "action", "proposalId": "prop-k2",
             "actionId": "act-3", "actionName": "通知外部系统",
             "parameters": {}, "targetInstanceId": "inst-o1",
             "releaseId": modeled["releaseId"],
             "requiresApproval": False, "status": "success",
             "validationErrors": [], "effects": []},
            {"kind": "action", "proposalId": "prop-k5",
             "actionId": "act-2", "actionName": "取消订单",
             "parameters": {}, "targetInstanceId": "inst-o1",
             "releaseId": modeled["releaseId"],
             "requiresApproval": True, "status": "success",
             "validationErrors": [], "effects": []},
        ])
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
    # 无外部效果点的动作（act-1）：本地 success 日志即可证明恢复
    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-1",
                   parameters={"note": "n", "level": 1}).json()[
                       "data"]["confirmationId"]
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


def test_crash_after_commit_with_attempts_stays_unknown(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """缺陷-4 契约：引擎已提交 success 日志但外部 attempt 未结案 →
    unknown、guard 保留；本地已提交效果记入对账证据，不宣称未发生。"""
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
    assert data["state"] == "unknown"
    assert data["failureCode"] == "execution_result_unknown"
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    assert operation.state == "unknown"
    assert operation.reconcile_evidence["localSuccessLogId"]
    # 本地 success 日志确实存在（已提交效果不否认），但只恰一条
    logs = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == modeled["oid"],
        ActionExecutionLog.idempotency_key == f"proposal:{cid}").all()
    assert len(logs) == 1
    # guard 保留：另一会话同载荷确认被拒
    _, r2 = _second_session_same_payload(
        client, auth_headers, modeled, db, "act-3", {})
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "operation_unresolved"


def test_worker_scan_survives_cleanup_and_advances(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """缺陷-1 契约：TTL 清理不再使整轮扫描崩溃；dispatch/pending 推进。"""
    from app.ontologies.agent_confirmation import worker as confirmation_worker
    from app.ontologies.agent_query.models import AgentQuerySnapshot
    from datetime import datetime, timedelta, timezone

    # 过期快照（触发清理分支）+ 一个 dispatch_queued 操作（触发派发分支）
    db.add(AgentQuerySnapshot(
        id="snap-expired", owner_user_id="u1", session_kind="direct",
        session_id="s1", query_hash="h", auth_hash="h", releases={},
        schema_version=1, order_spec=[], columns=[], counts={}, citations=[],
        page_size=10, total_row_count=0,
        observed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=10)))
    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-1",
                   parameters={"note": "n", "level": 1}).json()[
                       "data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")

    from app.ontologies.agent_confirmation import service as confirmation_svc

    worker_open = confirmation_worker._fresh_session
    service_open = confirmation_svc._fresh_session
    monkeypatch.setattr(confirmation_worker, "_fresh_session", lambda: db)
    monkeypatch.setattr(confirmation_svc, "_fresh_session", lambda: db)
    try:
        confirmation_worker._scan()  # 不得抛出
    finally:
        monkeypatch.setattr(confirmation_worker, "_fresh_session", worker_open)
        monkeypatch.setattr(confirmation_svc, "_fresh_session", service_open)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "succeeded", confirmation.state
    assert db.query(AgentQuerySnapshot).filter_by(
        id="snap-expired").count() == 0


def test_kill_switch_blocks_approve_and_worker_claim(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    from app.config import settings
    from app.ontologies.agent_confirmation import service as confirmation_svc

    cid = None
    r = client.post(
        f"/api/v2/formal/ontologies/{modeled['oid']}/agent/proposals",
        headers=auth_headers,
        json={"sessionKind": "ontology_agent",
              "sessionId": conversation["conv"].id,
              "clientSubmissionId": "prop-1", "actionId": "act-1",
              "releaseId": modeled["releaseId"],
              "targetInstanceId": "inst-o1",
              "parameters": {"note": "n", "level": 1}})
    assert r.status_code == 201, r.text
    cid = r.json()["data"]["confirmationId"]
    payload_hash = r.json()["data"]["payloadHash"]

    monkeypatch.setattr(settings, "assistant_action_execution_enabled",
                        False, raising=False)
    r = client.post(
        f"/api/v2/formal/ontologies/{modeled['oid']}/agent/proposals/{cid}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "ontology_agent",
              "sessionId": conversation["conv"].id, "decisionId": "d1",
              "payloadHash": payload_hash})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "assistant_writes_disabled"

    # worker claim 也被总闸拦住
    assert confirmation_svc._claim("nonexistent", db) is None



# ------------------------------------- P2-5 kernel gated 写映射（契约测试）

import asyncio as _asyncio
import uuid as _uuid
from datetime import timedelta as _timedelta

from app.models.user import User
from app.super_assistant.models import SuperAssistantConversation as _SAConv
from app.super_assistant.kernel import runtime as kernel_runtime
from app.super_assistant.kernel.models import (
    Approval as KernelApproval, ExecutionCall as KernelCall,
)
from app.super_assistant.kernel.store import (
    acquire_lease as _acquire_lease, create_run as _create_run,
)
from app.super_assistant import ontology_tools


class _NoCloseSession:
    """测试替身：kernel 消费者结束时会 close 会话；测试会话由 fixture 管理。"""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        return None


def _kernel_seed(db):
    owner = db.query(User).first()
    conversation = _SAConv(owner_id=owner.id, title="k")
    db.add(conversation)
    db.flush()
    run, _ = _create_run(db, owner_id=owner.id,
                         conversation_id=conversation.id, goal="goal",
                         idempotency_key=f"run-{_uuid.uuid4().hex[:8]}")
    db.commit()
    return owner, conversation, run


def _kernel_request_gated_call(db, run, conversation_id, confirmation_id,
                               ontology_id, payload_hash):
    token = _acquire_lease(db, run_id=run.id, worker_id="worker-test",
                           ttl=_timedelta(minutes=5))
    turn = kernel_runtime._open_turn(db, run, trigger_ref="test", lease=token)
    from app.super_assistant.kernel.models import ExecutionStep
    step = ExecutionStep(turn_id=turn.id, step_no=0, status="open")
    db.add(step)
    db.flush()
    kernel_runtime._persist_waiting_state(
        db, run, turn, step,
        {"kind": "external_event", "reason": "ontology_execute",
         "target_ref": ontology_tools.ONTOLOGY_EXECUTE_CAPABILITY,
         "_detail": {
             "capability": ontology_tools.ONTOLOGY_EXECUTE_CAPABILITY,
             "confirmationId": confirmation_id,
             "sessionId": conversation_id,
             "ontologyId": ontology_id,
             "payloadHash": payload_hash,
         }},
        token)
    db.commit()
    call = db.query(KernelCall).filter(
        KernelCall.run_id == run.id,
        KernelCall.capability_key
        == ontology_tools.ONTOLOGY_EXECUTE_CAPABILITY).one()
    approval = db.query(KernelApproval).filter(
        KernelApproval.call_id == call.id).one()
    return call, approval


def _kernel_prepare(client, auth_headers, modeled, conversation, submission,
                    action_id, parameters):
    r = client.post(
        f"/api/v2/formal/ontologies/{modeled['oid']}/agent/proposals",
        headers=auth_headers,
        json={"sessionKind": "super_assistant",
              "sessionId": conversation["conv"].id,
              "clientSubmissionId": submission, "actionId": action_id,
              "releaseId": modeled["releaseId"],
              "targetInstanceId": "inst-o1", "parameters": parameters})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _kernel_decide(client, auth_headers, run, approval, decision, key):
    return client.post(
        f"/api/v2/super-assistant/runs/{run.id}/approvals/{approval.id}/decision",
        headers={**auth_headers, "If-Match": str(run.version),
                 "Idempotency-Key": key},
        json={"decision": decision, "idempotency_key": key})


def test_kernel_ontology_gate_full_journey(client, auth_headers, modeled,
                                           conversation, db, monkeypatch):
    """approve → 确认协议派发 → outbox 消费 → Call 收敛 succeeded。"""
    monkeypatch.setattr(kernel_runtime, "SessionLocal",
                        lambda: _NoCloseSession(db))
    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k1", "act-1", {"note": "k", "level": 1})
    cid = prepared["confirmationId"]
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, cid,
        modeled["oid"], prepared["payloadHash"])
    assert call.side_effect_class == "non_idempotent_write"
    assert call.status == "offered" and call.outcome == "not_sent"
    assert approval.status == "pending"
    assert approval.parameter_hash == kernel_runtime._checksum(
        call.input_snapshot_ref)
    assert run.status == "waiting_approval"

    r = _kernel_decide(client, auth_headers, run, approval, "approved",
                       "dec-k1")
    assert r.status_code == 202, r.text
    confirmation = _confirmation(db, cid)
    db.refresh(confirmation)
    assert confirmation.state == "dispatch_queued"
    db.refresh(run)
    assert run.status == "waiting_external"
    from app.super_assistant.kernel.models import ExecutionDispatchOutbox
    assert db.query(ExecutionDispatchOutbox).filter_by(
        command_id=f"external-dispatch:{call.id}").count() == 1

    db.refresh(call)
    applied = _asyncio.run(kernel_runtime.process_external_call_message(
        {"run_id": run.id, "call_id": call.id}))
    assert applied is True
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "succeeded"
    call = db.query(KernelCall).filter_by(id=call.id).one()
    assert call.status == "closed"
    assert call.outcome == "completed"
    assert call.remote_observed_state_ref == "confirmation:succeeded"
    from app.models.ontology_formal import ObjectInstance
    inst = db.query(ObjectInstance).filter_by(id="inst-o1").one()
    db.refresh(inst)
    assert inst.properties["status"] == "paid"
    from app.ontologies.formal_modeling.models import ActionExecutionLog
    logs = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == modeled["oid"],
        ActionExecutionLog.idempotency_key == f"proposal:{cid}").all()
    assert len(logs) == 1
    applied = _asyncio.run(kernel_runtime.process_external_call_message(
        {"run_id": run.id, "call_id": call.id}))
    assert applied is True
    assert db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == modeled["oid"],
        ActionExecutionLog.idempotency_key == f"proposal:{cid}").count() == 1


def test_kernel_ontology_gate_deny_has_no_business_effect(
        client, auth_headers, modeled, conversation, db):
    from app.models.ontology_formal import ObjectInstance

    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k1", "act-1", {"note": "k", "level": 1})
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, prepared["confirmationId"],
        modeled["oid"], prepared["payloadHash"])
    r = _kernel_decide(client, auth_headers, run, approval, "denied",
                       "dec-k2")
    assert r.status_code == 202, r.text
    confirmation = _confirmation(db, prepared["confirmationId"])
    db.refresh(confirmation)
    assert confirmation.state == "denied"
    db.refresh(call)
    assert call.status == "closed" and call.outcome == "not_sent"
    inst = db.query(ObjectInstance).filter_by(id="inst-o1").one()
    db.refresh(inst)
    assert inst.properties["status"] == "pending"


def test_kernel_ontology_gate_unknown_keeps_manual_attention(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    from app.ontologies.formal_modeling import action_engine as engine_mod

    monkeypatch.setattr(kernel_runtime, "SessionLocal",
                        lambda: _NoCloseSession(db))

    def _boom(*args, **kwargs):
        raise RuntimeError("crash mid-effects")

    monkeypatch.setattr(engine_mod, "execute_action", _boom)
    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k2", "act-3", {})
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, prepared["confirmationId"],
        modeled["oid"], prepared["payloadHash"])
    r = _kernel_decide(client, auth_headers, run, approval, "approved",
                       "dec-k3")
    assert r.status_code == 202, r.text
    applied = _asyncio.run(kernel_runtime.process_external_call_message(
        {"run_id": run.id, "call_id": call.id}))
    assert applied is True
    db.expire_all()
    confirmation = _confirmation(db, prepared["confirmationId"])
    assert confirmation.state == "unknown"
    call = db.query(KernelCall).filter_by(id=call.id).one()
    assert call.outcome == "outcome_unknown"
    assert call.manual_attention is True
    assert call.status == "reconciling"


def test_kernel_ontology_gate_kill_switch_consistent(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    from app.config import settings

    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k1", "act-1", {"note": "k", "level": 1})
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, prepared["confirmationId"],
        modeled["oid"], prepared["payloadHash"])
    monkeypatch.setattr(settings, "assistant_action_execution_enabled",
                        False, raising=False)
    r = _kernel_decide(client, auth_headers, run, approval, "approved",
                       "dec-k4")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "assistant_writes_disabled"


def test_kernel_ontology_gate_converges_pending_to_succeeded(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    monkeypatch.setattr(kernel_runtime, "SessionLocal",
                        lambda: _NoCloseSession(db))
    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k5", "act-2", {})
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, prepared["confirmationId"],
        modeled["oid"], prepared["payloadHash"])
    r = _kernel_decide(client, auth_headers, run, approval, "approved",
                       "dec-k5")
    assert r.status_code == 202, r.text
    applied = _asyncio.run(kernel_runtime.process_external_call_message(
        {"run_id": run.id, "call_id": call.id}))
    assert applied is True
    db.expire_all()
    call = db.query(KernelCall).filter_by(id=call.id).one()
    assert call.status == "waiting_external"
    assert call.remote_observed_state_ref == "confirmation:pending_approval"

    confirmation = _confirmation(db, prepared["confirmationId"])
    db.refresh(confirmation)
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=confirmation.id).one()
    r = client.post(
        f"/api/v2/formal/ontologies/{modeled['oid']}/action-logs/"
        f"{operation.approval_log_id}/decide",
        headers=auth_headers,
        json={"decision": "approved", "reason": "ok",
              "releaseId": modeled["releaseId"]})
    assert r.status_code == 200, r.text

    converged = kernel_runtime.converge_ontology_execute_calls(db)
    assert converged >= 1
    db.expire_all()
    confirmation = _confirmation(db, prepared["confirmationId"])
    assert confirmation.state == "succeeded"
    call = db.query(KernelCall).filter_by(id=call.id).one()
    assert call.status == "closed"
    assert call.outcome == "completed"


# ---------------- lease 过期/worker 崩溃恢复：本地日志必须经外部证据裁决

def _simulate_claimed_worker_died(db, cid, *, engine_entered=True):
    """真实崩溃模拟：claim → durable engine_entered → 外部 attempt 预登记
    （如适用）→ 租约过期。返回 operation。"""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    from app.ontologies.agent_confirmation import service as confirmation_svc

    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    assert confirmation_svc._claim(operation.id, db) is not None
    if engine_entered:
        confirmation_svc._mark_engine_entered(operation.id, db)
        confirmation = _confirmation(db, cid)
        operation = db.query(CM.ActionOperation).filter_by(
            confirmation_id=cid).one()
        confirmation_svc._record_external_attempts_started(
            db, operation, confirmation)
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    operation.lease_expires_at = _dt.now(_tz.utc) - _td(seconds=1)
    db.commit()
    return db.query(CM.ActionOperation).filter_by(confirmation_id=cid).one()


def _engine_log(db, ontology_id, cid, status, *, error_message=None):
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    log = ActionExecutionLog(
        id=f"log-{uuid.uuid4().hex[:6]}", ontology_id=ontology_id,
        action_id="act-3", status=status, parameters={},
        error_message=error_message, idempotency_key=f"proposal:{cid}")
    db.add(log)
    db.commit()
    return log


def test_reconcile_expired_lease_success_log_with_attempt_stays_unknown(
        client, auth_headers, modeled, conversation, db):
    """审查缺陷：lease 过期恢复看到本地 success 日志不能直接结案 —
    外部 attempt 未结案 → unknown、guard 保留（真实状态，非假成功）。"""
    from app.ontologies.agent_confirmation import service as confirmation_svc

    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-k2", actionId="act-3",
                   parameters={}).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    operation = _simulate_claimed_worker_died(db, cid)
    assert db.query(CM.ActionExternalAttempt).filter_by(
        operation_id=operation.id).count() == 1
    _engine_log(db, modeled["oid"], cid, "success")

    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "unknown"
    assert confirmation.failure_code == "execution_result_unknown"
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    assert operation.state == "unknown"
    assert operation.reconcile_evidence["reason"] == (
        "local_success_with_unresolved_external_attempt")
    # guard 保留：另一会话同载荷确认被拒
    _, r2 = _second_session_same_payload(
        client, auth_headers, modeled, db, "act-3", {})
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "operation_unresolved"


def test_reconcile_expired_lease_success_log_clean_closes_succeeded(
        client, auth_headers, modeled, conversation, db):
    """无外部效果点：本地 success 日志 + 全部 attempt 可证未发送 →
    succeeded 且 guard 释放（另一会话同载荷可确认）。"""
    from app.ontologies.agent_confirmation import service as confirmation_svc

    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-1",
                   parameters={"note": "n", "level": 1}).json()[
                       "data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    operation = _simulate_claimed_worker_died(db, cid)
    assert db.query(CM.ActionExternalAttempt).filter_by(
        operation_id=operation.id).count() == 0
    _engine_log(db, modeled["oid"], cid, "success")

    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "succeeded"
    _, r2 = _second_session_same_payload(
        client, auth_headers, modeled, db, "act-1",
        {"note": "n", "level": 1})
    assert r2.status_code == 200, r2.text


def test_reconcile_expired_lease_failed_log_with_attempt_stays_unknown(
        client, auth_headers, modeled, conversation, db):
    """本地 failed 日志同样不能单独证明未执行：未结案 attempt →
    unknown、guard 保留（防御分支：引擎失败通常清键，键保留时更须裁决）。"""
    from app.ontologies.agent_confirmation import service as confirmation_svc

    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-k2", actionId="act-3",
                   parameters={}).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    operation = _simulate_claimed_worker_died(db, cid)
    _engine_log(db, modeled["oid"], cid, "failed",
                error_message="hook 5xx")

    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "unknown"
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    assert operation.state == "unknown"
    _, r2 = _second_session_same_payload(
        client, auth_headers, modeled, db, "act-3", {})
    assert r2.status_code == 409


def test_reconcile_expired_lease_pending_log_matches_pending_recovery(
        client, auth_headers, modeled, conversation, db):
    """pending 日志恢复与 pending 审批恢复一致：进入 pending_approval，
    预登记 attempt 标记 proven_not_applied（引擎在效果阶段前返回）。"""
    from app.ontologies.agent_confirmation import service as confirmation_svc

    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-k2", actionId="act-3",
                   parameters={}).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    operation = _simulate_claimed_worker_died(db, cid)
    log = _engine_log(db, modeled["oid"], cid, "pending")

    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "pending_approval"
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    assert operation.state == "pending_approval"
    assert operation.approval_log_id == log.id
    attempts = db.query(CM.ActionExternalAttempt).filter_by(
        operation_id=operation.id).all()
    assert attempts
    assert all(a.state == "proven_not_applied" for a in attempts)
    assert all(a.evidence_ref == "engine_returned_pending_before_effects"
               for a in attempts)


def test_reconcile_expired_lease_engine_entered_without_log_unknown(
        client, auth_headers, modeled, conversation, db):
    """引擎已进入但无结案日志：unknown，guard 不释放（真崩溃窗口）。"""
    from app.ontologies.agent_confirmation import service as confirmation_svc

    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-1",
                   parameters={"note": "n", "level": 1}).json()[
                       "data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    operation = _simulate_claimed_worker_died(db, cid)

    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "unknown"
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    assert operation.state == "unknown"
    assert operation.reconcile_evidence["reason"] == (
        "engine_entered_without_terminal_evidence")


def test_reconcile_expired_lease_before_engine_entered_requeues(
        client, auth_headers, modeled, conversation, db):
    """engine_entered 为空且租约过期：安全重新派发同一 operation，
    后续 worker 消费收敛 succeeded（durable 状态机恢复闭环）。"""
    from app.ontologies.agent_confirmation import service as confirmation_svc

    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-1", actionId="act-1",
                   parameters={"note": "n", "level": 1}).json()[
                       "data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    operation = _simulate_claimed_worker_died(db, cid,
                                              engine_entered=False)
    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    assert operation.state == "dispatch_queued"
    assert operation.claimed_at is None
    assert operation.lease_expires_at is None

    confirmation_svc.run_operation_inline(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "succeeded"


def test_worker_lifecycle_start_shutdown_leaves_no_thread():
    """worker 启停不留线程（AGENTS 自测清理硬性要求）。"""
    from app.ontologies.agent_confirmation import worker as confirmation_worker

    confirmation_worker.start()
    scheduler = confirmation_worker._scheduler
    assert scheduler is not None
    thread = getattr(scheduler, "_thread", None)
    confirmation_worker.start()  # 幂等：重复启动不新建调度器
    assert confirmation_worker._scheduler is scheduler
    confirmation_worker.shutdown()
    assert confirmation_worker._scheduler is None
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


# ---------------- kernel 审批卡/Call 状态可读字段（服务端确认详情）

def _kernel_run_view(client, auth_headers, run):
    r = client.get(f"/api/v2/super-assistant/runs/{run.id}",
                   headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_kernel_run_view_approval_card_and_confirmation_display(
        client, auth_headers, modeled, conversation, db):
    """审批卡必须可读：action/target/参数/来源来自确认 display，RunView
    与 approval.requested 事件同构；Call 带确认投影与 manual_attention。"""
    from app.super_assistant.kernel.models import ExecutionEvent

    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k1", "act-1", {"note": "k", "level": 1})
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, prepared["confirmationId"],
        modeled["oid"], prepared["payloadHash"])
    db.refresh(approval)
    # 摘要来自 display，而非裸 confirmationId / 原始 JSON
    assert "标记已支付" in approval.target_summary
    assert "inst-o1" in approval.target_summary
    assert "note='k'" in approval.parameter_summary
    assert "source: user_input" in approval.parameter_summary
    assert "requiresApproval" in approval.scope_summary

    view = _kernel_run_view(client, auth_headers, run)
    call_view = next(c for c in view["calls"]
                     if c["call_id"] == call.id)
    assert call_view["capability_key"] \
        == ontology_tools.ONTOLOGY_EXECUTE_CAPABILITY
    assert call_view["manual_attention"] is False
    confirmation = call_view["confirmation"]
    assert confirmation["confirmationId"] == prepared["confirmationId"]
    assert confirmation["state"] == "prepared"
    assert confirmation["display"]["action"]["name"] == "标记已支付"
    assert confirmation["display"]["target"]["id"] == "inst-o1"
    assert confirmation["payloadHash"] == prepared["payloadHash"]

    inbox_view = next(i for i in view["current_inbox"]
                      if i["kind"] == "approval_decision")
    card = inbox_view["approval"]
    assert card["approval_id"] == approval.id
    assert "标记已支付" in card["target_summary"]
    assert card["confirmation"]["confirmationId"] \
        == prepared["confirmationId"]
    assert card["confirmation"]["display"]["action"]["id"] == "act-1"

    # approval.requested 事件同构：前端 SSE reducer 不回查 GET 也能渲染
    event = db.query(ExecutionEvent).filter(
        ExecutionEvent.run_id == run.id,
        ExecutionEvent.event_type == "approval.requested").one()
    payload = event.payload
    assert "标记已支付" in payload["target_summary"]
    assert payload["confirmation_id"] == prepared["confirmationId"]
    assert payload["confirmation"]["display"]["action"]["name"] == "标记已支付"


def test_kernel_run_view_unknown_call_is_readable_not_fake_success(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """unknown 结果的 Call 在 RunView 中可读：manual_attention + 确认
    state/blockReason，不伪装成功。"""
    from app.ontologies.formal_modeling import action_engine as engine_mod

    monkeypatch.setattr(kernel_runtime, "SessionLocal",
                        lambda: _NoCloseSession(db))

    def _boom(*args, **kwargs):
        raise RuntimeError("crash mid-effects")

    monkeypatch.setattr(engine_mod, "execute_action", _boom)
    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k2", "act-3", {})
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, prepared["confirmationId"],
        modeled["oid"], prepared["payloadHash"])
    r = _kernel_decide(client, auth_headers, run, approval, "approved",
                       "dec-view1")
    assert r.status_code == 202, r.text
    applied = _asyncio.run(kernel_runtime.process_external_call_message(
        {"run_id": run.id, "call_id": call.id}))
    assert applied is True
    db.expire_all()

    view = _kernel_run_view(client, auth_headers, run)
    call_view = next(c for c in view["calls"] if c["call_id"] == call.id)
    assert call_view["manual_attention"] is True
    assert call_view["status"] == "reconciling"
    assert call_view["outcome"] == "outcome_unknown"
    assert call_view["confirmation"]["state"] == "unknown"
    assert "1040787244" in call_view["confirmation"]["blockReason"]
    assert "不自动" in call_view["confirmation"]["blockReason"] or \
        "无自动超时释放" in call_view["confirmation"]["blockReason"]


def test_kernel_approval_without_readable_display_fails_closed(
        client, auth_headers, modeled, conversation, db):
    """确认不可读时不得落一张只有 confirmationId 的裸审批卡。"""
    from app.super_assistant.kernel.contracts import ContractError
    from app.super_assistant.kernel.models import Approval as KernelApproval

    owner, sa_conv, run = _kernel_seed(db)
    with pytest.raises(ContractError):
        _kernel_request_gated_call(
            db, run, conversation["conv"].id, "cid-not-exist",
            modeled["oid"], "hash-not-exist")
    db.rollback()
    assert db.query(KernelApproval).filter_by(run_id=run.id).count() == 0


def test_kernel_run_view_after_success_shows_confirmation_state(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """成功路径的 RunView：call closed/completed + confirmation.state
    succeeded；call.outcome_changed 事件带 manual_attention/confirmation。"""
    from app.super_assistant.kernel.models import ExecutionEvent

    monkeypatch.setattr(kernel_runtime, "SessionLocal",
                        lambda: _NoCloseSession(db))
    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k1", "act-1", {"note": "k", "level": 1})
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, prepared["confirmationId"],
        modeled["oid"], prepared["payloadHash"])
    r = _kernel_decide(client, auth_headers, run, approval, "approved",
                       "dec-view2")
    assert r.status_code == 202, r.text
    applied = _asyncio.run(kernel_runtime.process_external_call_message(
        {"run_id": run.id, "call_id": call.id}))
    assert applied is True
    db.expire_all()

    view = _kernel_run_view(client, auth_headers, run)
    call_view = next(c for c in view["calls"] if c["call_id"] == call.id)
    assert call_view["status"] == "closed"
    assert call_view["outcome"] == "completed"
    assert call_view["manual_attention"] is False
    assert call_view["confirmation"]["state"] == "succeeded"
    assert call_view["remote_observed_state_ref"] == "confirmation:succeeded"

    events = db.query(ExecutionEvent).filter(
        ExecutionEvent.run_id == run.id,
        ExecutionEvent.event_type == "call.outcome_changed").all()
    final = [e for e in events
             if e.payload.get("confirmationState") == "succeeded"]
    assert final, "成功收敛事件必须携带确认状态"
    assert final[-1].payload["manual_attention"] is False
    assert final[-1].payload["confirmation"]["state"] == "succeeded"
    assert final[-1].payload["confirmation"]["display"]["action"][
        "id"] == "act-1"


def test_kernel_approve_requires_readable_bound_confirmation(
        client, auth_headers, modeled, conversation, db):
    """决策端点 fail-closed 合同：确认不可读（404）/载荷指纹被换时，
    审批不得生效 — 不存在 blind approve 裸 ID 的路径。"""
    prepared = _kernel_prepare(client, auth_headers, modeled, conversation,
                               "prop-k1", "act-1", {"note": "k", "level": 1})
    owner, sa_conv, run = _kernel_seed(db)
    call, approval = _kernel_request_gated_call(
        db, run, conversation["conv"].id, prepared["confirmationId"],
        modeled["oid"], prepared["payloadHash"])

    # 1) 确认在决策前丢失：decision 必须失败，审批保持 pending
    cid = prepared["confirmationId"]
    db.query(CM.ActionConfirmation).filter_by(id=cid).delete()
    db.commit()
    r = _kernel_decide(client, auth_headers, run, approval, "approved",
                       "dec-bind1")
    assert r.status_code in (404, 409), r.text
    db.expire_all()
    approval = db.query(KernelApproval).filter_by(id=approval.id).one()
    assert approval.status == "pending"

    # 2) 载荷指纹不一致（Call 快照锚定的 payloadHash 与确认不符）：409
    prepared2 = _kernel_prepare(client, auth_headers, modeled, conversation,
                                "prop-k2", "act-3", {})
    owner2, sa_conv2, run2 = _kernel_seed(db)
    call2, approval2 = _kernel_request_gated_call(
        db, run2, conversation["conv"].id, prepared2["confirmationId"],
        modeled["oid"], "tampered-hash")
    r = _kernel_decide(client, auth_headers, run2, approval2, "approved",
                       "dec-bind2")
    assert r.status_code == 409, r.text
    db.expire_all()
    confirmation2 = _confirmation(db, prepared2["confirmationId"])
    assert confirmation2.state == "prepared", confirmation2.state
    approval2 = db.query(KernelApproval).filter_by(id=approval2.id).one()
    assert approval2.status == "pending"


def test_worker_engine_entry_revocation_blocks_execution(
        client, auth_headers, modeled, conversation, db, monkeypatch):
    """P2-C：approve 后入引擎前（worker 领取路径）撤权 → 不执行。"""
    from app.ontologies.agent_confirmation import service as confirmation_svc
    from app.models.ontology_formal import ObjectInstance

    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    # approve 后、worker 入引擎前撤权（allowlist 收紧）
    client.put(f"{_fo(modeled['oid'])}/agent/profile", headers=auth_headers,
               json={"allowedActionIds": []})
    confirmation = _confirmation(db, cid)
    db.refresh(confirmation)
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    confirmation_svc.run_operation_inline(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "failed_not_executed"
    assert confirmation.failure_code == "access_revoked_before_engine"
    inst = db.query(ObjectInstance).filter_by(id="inst-o1").one()
    db.refresh(inst)
    assert inst.properties["status"] == "pending"   # 零业务效果


# ------------------------------------- Z1：合并冲突丢失的六项行为测试恢复


def test_root_intent_rejects_assistant_only_conversation(
        client, auth_headers, modeled, db):
    """裁决 3/Z1：提案之前没有用户消息 → 拒绝建立确认（无 assistant 兜底）。"""
    from app.ontologies.agent_runtime.models import (
        AgentConversation, AgentMessage,
    )
    from app.models.user import User

    user = db.query(User).first()
    conv = AgentConversation(id=f"conv-{uuid.uuid4().hex[:8]}",
                             ontology_id=modeled["oid"], user_id=user.id)
    db.add(conv)
    db.flush()
    db.add(AgentMessage(
        id=f"ma-{uuid.uuid4().hex[:6]}", conversation_id=conv.id,
        role="assistant", content="no user intent",
        proposals=[{"kind": "action", "proposalId": "prop-no-user",
                    "actionId": "act-1",
                    "parameters": {"note": "x", "level": 1},
                    "targetInstanceId": "inst-o1",
                    "releaseId": modeled["releaseId"], "status": "success",
                    "validationErrors": [], "effects": []}]))
    db.commit()
    r = client.post(
        f"/api/v2/formal/ontologies/{modeled['oid']}/agent/proposals",
        headers=auth_headers,
        json={"sessionKind": "ontology_agent", "sessionId": conv.id,
              "clientSubmissionId": "prop-no-user", "actionId": "act-1",
              "releaseId": modeled["releaseId"],
              "targetInstanceId": "inst-o1",
              "parameters": {"note": "x", "level": 1}})
    assert r.status_code == 422
    assert "没有可信的用户意图" in r.json()["detail"]["message"]


def test_execute_wake_revoked_access_fails_not_executed(
        client, auth_headers, modeled, conversation, db):
    """裁决 1/Z1：入引擎前（唤醒路径）撤权 → failed_not_executed 零效果。"""
    from app.models.ontology_formal import ObjectInstance

    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    client.put(
        f"/api/v2/formal/ontologies/{modeled['oid']}/agent/profile",
        headers=auth_headers, json={"allowedActionIds": []})
    r = _execute(client, auth_headers, modeled, conversation, cid)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["state"] == "failed_not_executed"
    assert data["failureCode"] == "access_revoked_before_engine"
    inst = db.query(ObjectInstance).filter_by(id="inst-o1").one()
    db.refresh(inst)
    assert inst.properties["status"] == "pending"


def test_technical_checkpoint_executing_and_unknown_convergence(
        client, auth_headers, modeled, conversation, db):
    """裁决 2/Z1：技术执行 checkpoint/lease + unknown/guard 收敛。"""
    from app.ontologies.agent_confirmation import service as confirmation_svc
    from app.ontologies.formal_modeling.models import ActionExecutionLog
    from datetime import datetime, timezone

    cid = _prepare(client, auth_headers, modeled, conversation,
                   clientSubmissionId="prop-k5", actionId="act-2",
                   parameters={}).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    _execute(client, auth_headers, modeled, conversation, cid)
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()

    # 管理员已批准、技术执行 executing → checkpoint 入口
    approval_log = db.query(ActionExecutionLog).filter_by(
        id=operation.approval_log_id).one()
    approval_log.status = "executing"
    db.commit()
    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    confirmation = _confirmation(db, cid)
    assert operation.state == "executing"
    assert confirmation.state == "executing"
    assert operation.engine_entered_at is not None
    assert operation.technical_engine_key == (
        f"approval-execution:{operation.approval_log_id}")
    db.refresh(approval_log)
    assert approval_log.operation_id == operation.id

    # lease 失效且无结案证据 → unknown + blockReason + guard 保留
    operation.lease_expires_at = datetime.now(timezone.utc)
    db.commit()
    confirmation_svc.reconcile_operation(operation.id, db)
    db.expire_all()
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    confirmation = _confirmation(db, cid)
    assert operation.state == "unknown"
    assert confirmation.state == "unknown"
    assert confirmation.failure_code == "execution_result_unknown"
    _, r2 = _second_session_same_payload(
        client, auth_headers, modeled, db, "act-2", {})
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "operation_unresolved"


def test_actor_user_row_missing_blocks_engine_entry(
        client, auth_headers, modeled, conversation, db):
    """P3-1：actor 用户行删除后，worker/direct 入引擎路径拒绝执行。

    覆盖：用户删除后零业务效果；未入引擎且无外部 attempt →
    failed_not_executed；已入引擎（unknown）不因 actor 缺失释放 guard。
    """
    from app.ontologies.agent_confirmation import service as confirmation_svc
    from app.models.ontology_formal import ObjectInstance
    from app.models.user import User

    # --- 路径 1：worker 入引擎（approve 后领取）---
    cid = _prepare(client, auth_headers, modeled,
                   conversation).json()["data"]["confirmationId"]
    payload_hash = _confirmation(db, cid).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid,
            payload_hash, "approve")
    actor = db.query(User).first()
    actor_id = actor.id
    db.query(User).filter_by(id=actor_id).delete()
    db.commit()
    operation = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid).one()
    confirmation_svc.run_operation_inline(operation.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid)
    assert confirmation.state == "failed_not_executed"
    assert confirmation.failure_code == "actor_user_missing"
    inst = db.query(ObjectInstance).filter_by(id="inst-o1").one()
    db.refresh(inst)
    assert inst.properties["status"] == "pending"   # 零业务效果

    # --- 路径 2：direct 唤醒入口同样拒绝（经服务层直接调用 —
    # 用户行删除后 HTTP 认证自然 401，唤醒路径的服务层行为单独验证）---
    db.add(User(id=actor_id, username="restored",
                email="r@t.io", password_hash="x", role="admin",
                is_active=True, token_version=0))
    db.commit()
    cid2 = _prepare(client, auth_headers, modeled, conversation,
                    clientSubmissionId="prop-1").json()["data"][
                        "confirmationId"]
    payload2 = _confirmation(db, cid2).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid2,
            payload2, "approve")
    db.query(User).filter_by(id=actor_id).delete()
    db.commit()
    class _DeletedUser:
        id = actor_id
        role = "admin"

    confirmation2 = _confirmation(db, cid2)
    confirmation_svc.execute(db, _DeletedUser(), modeled["oid"], cid2,
                             "ontology_agent", conversation["conv"].id)
    db.expire_all()
    confirmation2 = _confirmation(db, cid2)
    assert confirmation2.state == "failed_not_executed"
    assert confirmation2.failure_code == "actor_user_missing"
    inst = db.query(ObjectInstance).filter_by(id="inst-o1").one()
    db.refresh(inst)
    assert inst.properties["status"] == "pending"

    # --- 路径 3：已入引擎（unknown + 未结案外部 attempt）不释放 guard ---
    db.add(User(id=actor_id, username="restored2",
                email="r2@t.io", password_hash="x", role="admin",
                is_active=True, token_version=0))
    db.commit()
    cid3 = _prepare(client, auth_headers, modeled, conversation,
                    clientSubmissionId="prop-k2",
                    actionId="act-3", parameters={}).json()["data"][
                        "confirmationId"]
    payload3 = _confirmation(db, cid3).payload_hash
    _decide(client, auth_headers, modeled, conversation, cid3,
            payload3, "approve")
    # 恢复 actor 使引擎可进入，先执行到 unknown（webhook 不可达）
    r = _execute(client, auth_headers, modeled, conversation, cid3)
    assert r.status_code == 200, r.text
    confirmation = _confirmation(db, cid3)
    db.refresh(confirmation)
    assert confirmation.state == "unknown"
    # actor 删除后 worker 对账/重验不释放 guard（不进引擎不翻案）
    db.query(User).filter_by(id=actor_id).delete()
    db.commit()
    operation3 = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid3).one()
    confirmation_svc.run_operation_inline(operation3.id, db)
    db.expire_all()
    confirmation = _confirmation(db, cid3)
    assert confirmation.state == "unknown"   # 不变：guard 保留不盲翻案
    # guard 保留证据：直接查活跃 operation（actor 已删，HTTP 会话建立不可行）
    operation_view = db.query(CM.ActionOperation).filter_by(
        confirmation_id=cid3).one()
    db.refresh(operation_view)
    assert operation_view.state in ("unknown", "reconciling")
    # 已有 external attempt 未结案（unknown 证据保留，不因 actor 缺失清账）
    attempts = db.query(CM.ActionExternalAttempt).filter_by(
        operation_id=operation_view.id).all()
    assert attempts
    assert all(a.state in ("started", "unknown") for a in attempts)
