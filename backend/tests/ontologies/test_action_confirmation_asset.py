"""确认协议 × 资产写路由契约：冻结计划、零写、三源 outcome、CAS/幂等/unknown。

使用注入的 Fake 端口（double）驱动确认链：manual/curated/integrated 的
真实源终值断言在真实 E2E（staging + 隔离源库）完成，本文件只锚定协议
语义：确认前零业务写、确定性拒绝零源变更、unknown 不自动重放、curated
两层状态（state=succeeded + outcome=pending_review→applied/rejected）。
"""
import json
import uuid

import pytest

from app.ontologies.agent_confirmation import write_port as WP

from tests.ontologies.test_action_confirmation import _fo  # noqa: F401


class FakeAssetWritePort:
    """double：记录调用序列，按脚本返回计划/结果。"""

    def __init__(self):
        self.resolved = []
        self.applied = []
        self.inspect_calls = 0
        self.plan = None
        self.apply_result = None
        self.apply_error = None
        self.inspect_result = None

    def reset_scenario(self, *, plan=None, apply_result=None,
                       apply_error=None, inspect_result=None):
        self.resolved = []
        self.applied = []
        self.inspect_calls = 0
        self.plan = plan
        self.apply_result = apply_result
        self.apply_error = apply_error
        self.inspect_result = inspect_result

    def resolve_write(self, db, user, *, ontology_id, release_id, request):
        self.resolved.append(request)
        if self.plan is None:
            return None
        return dict(self.plan)

    def apply_write(self, db, user, plan, *, confirmation_id, actor_id):
        self.applied.append({"plan": dict(plan),
                             "confirmationId": confirmation_id,
                             "actorId": actor_id})
        if self.apply_error is not None:
            raise self.apply_error
        if self.apply_result is None:
            raise AssertionError("fake port 未配置 apply_result")
        return self.apply_result

    def inspect_write(self, db, user, plan, *, evidence=None):
        self.inspect_calls += 1
        return self.inspect_result


def _plan(asset_kind="manual", operation="update", **overrides):
    base = {
        "planVersion": 1, "assetKind": asset_kind, "ontologyId": "o",
        "releaseId": "r", "mappingId": "m1", "objectTypeId": "ot-order",
        "datasetId": "ds-asset", "datasetName": "订单源",
        "operation": operation,
        "key": {"order_no": "SO-001"}, "values": {"status": "paid"},
        "expectedToken": 3 if asset_kind == "manual" else "ver-1",
        "keyProperties": ["order_no"], "properties": ["status"],
        "valueMap": {"status": "status"},
    }
    if asset_kind == "integrated":
        base.update({
            "assetId": "asset-1", "connectionId": "conn-1",
            "schemaFingerprint": "fp-1",
        })
    base.update(overrides)
    return base


def _applied(asset_kind="manual", **overrides):
    base = WP.AssetWriteOutcome(
        asset_kind=asset_kind, status=WP.OUTCOME_APPLIED,
        operation="update", affected_rows=1, dataset_id="ds-asset",
        version_id="ver-2" if asset_kind == "manual" else None,
        receipt_id="rcp-1" if asset_kind == "integrated" else None)
    return base


@pytest.fixture
def fake_port(monkeypatch):
    port = FakeAssetWritePort()
    # monkeypatch 恢复全局 registry 原值：不把进程级 None 留给后续测试。
    monkeypatch.setattr(WP, "_port", port)
    yield port


def _apply_outcome_fields(outcome, **fields):
    return outcome.__class__(**{**vars(outcome), **fields})


@pytest.fixture
def bound(client, auth_headers, ontology, db, fake_port):
    """带资产绑定映射（ot-order → manual 数据集）的发布本体 + 会话。

    复用 test_action_confirmation.modeled 的建模形状，但快照注入一条
    targetObjectTypeId=ot-order 的本地数据集映射，使 act-1 走资产路由。
    """
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
        "linkTypes": [
            {"id": "lt-1", "name": "related", "displayName": "关联",
             "sourceObjectTypeId": "ot-order", "targetObjectTypeId": "ot-order",
             "properties": []},
        ],
        "actions": [
            {"id": "act-1", "name": "mark_paid", "displayName": "标记已支付",
             "objectTypeId": "ot-order", "requiresApproval": False,
             "parameters": [
                 {"name": "status", "displayName": "状态", "type": "string"}],
             "rules": [
                 {"type": "update_property", "name": "set-status",
                  "enabled": True, "order": 0,
                  "config": {"targetProperty": "status",
                             "valueSource": "parameter", "value": "status"}}]},
            {"id": "act-link", "name": "link_orders", "displayName": "关联订单",
             "objectTypeId": "ot-order", "requiresApproval": False,
             "parameters": [],
             "rules": [
                 {"type": "update_property", "name": "set-status",
                  "enabled": True, "order": 0,
                  "config": {"targetProperty": "status",
                             "valueSource": "constant", "value": "\"paid\""}},
                 {"type": "create_link", "name": "rel", "enabled": True,
                  "order": 1, "config": {"linkTypeId": "lt-1",
                                         "targetSource": "created_object",
                                         "targetValue": "ot-order"}}]},
            {"id": "act-approve", "name": "approve_gate",
             "displayName": "需审批的源写", "objectTypeId": "ot-order",
             "requiresApproval": True,
             "parameters": [
                 {"name": "status", "displayName": "状态", "type": "string"}],
             "rules": [
                 {"type": "update_property", "name": "set-status",
                  "enabled": True, "order": 0,
                  "config": {"targetProperty": "status",
                             "valueSource": "parameter", "value": "status"}}]},
            {"id": "act-chain", "name": "create_and_link",
             "displayName": "新建并关联", "objectTypeId": "ot-order",
             "requiresApproval": False,
             "parameters": [
                 {"name": "order_no", "displayName": "订单号",
                  "type": "string"}],
             "rules": [
                 {"type": "create_object", "name": "new-order",
                  "enabled": True, "order": 0,
                  "config": {"targetObjectTypeId": "ot-order",
                             "propertyMappings": [
                                 {"targetProperty": "order_no",
                                  "sourceType": "parameter",
                                  "sourceValue": "order_no"},
                                 {"targetProperty": "status",
                                  "sourceType": "constant",
                                  "sourceValue": "\"new\""}]}},
                 {"type": "create_link", "name": "link-new", "enabled": True,
                  "order": 1, "config": {"linkTypeId": "lt-1",
                                         "targetSource": "created_object",
                                         "targetValue": "ot-order"}}]},
            {"id": "act-del", "name": "delete_row_action",
             "displayName": "删除行", "objectTypeId": "ot-order",
             "requiresApproval": False, "parameters": [],
             "rules": [
                 {"type": "delete_row", "name": "del", "enabled": True,
                  "order": 0, "config": {}}]},
            {"id": "act-local", "name": "local_only", "displayName": "本地动作",
             "objectTypeId": "ot-order", "requiresApproval": False,
             "parameters": [
                 {"name": "status", "displayName": "状态", "type": "string"}],
             "rules": [
                 {"type": "update_property", "name": "set-status",
                  "enabled": True, "order": 0,
                  "config": {"targetProperty": "status",
                             "valueSource": "parameter", "value": "status"}}]},
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
                    json={"allowedActionIds": ["act-1", "act-link",
                                               "act-local", "act-del",
                                               "act-approve", "act-chain"]})
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
    snapshot["mappings"] = [{
        "id": "map-asset", "curatedDatasetId": "ds-asset",
        "entityClass": "Order", "targetObjectTypeId": "ot-order",
        "fieldMapping": {"order_no": "order_no", "status": "status"},
        "bindingMode": "local", "status": "confirmed", "confidence": 1.0,
    }]
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    for inst in db.query(ObjectInstance).filter_by(ontology_id=oid).all():
        inst.ontology_release_id = release.id
    db.commit()

    from app.ontologies.agent_runtime.models import (
        AgentConversation, AgentMessage,
    )
    from app.models.user import User

    user = db.query(User).first()
    conv = AgentConversation(id=f"conv-{uuid.uuid4().hex[:8]}",
                             ontology_id=oid, user_id=user.id, title="t")
    db.add(conv)
    db.flush()
    db.add(AgentMessage(id=f"msg-u-{uuid.uuid4().hex[:8]}",
                        conversation_id=conv.id, role="user",
                        content="把 SO-001 标记为已支付"))
    proposals = []
    for proposal_id, action_id in (
            ("prop-1", "act-1"), ("prop-link", "act-link"),
            ("prop-del", "act-del"), ("prop-del2", "act-del"),
            ("prop-appr", "act-approve"), ("prop-appr2", "act-approve"),
            ("prop-ref", "act-1"), ("prop-local", "act-link"),
            ("prop-unk", "act-1"), ("prop-ok2", "act-1"),
            ("prop-chain", "act-chain"),
            ("prop-reserved", "act-1")):
        proposals.append({
            "kind": "action", "proposalId": proposal_id,
            "actionId": action_id, "actionName": action_id,
            "parameters": {"status": "paid"},
            "releaseId": release.id,
            "requiresApproval": action_id == "act-approve",
            "status": "success", "validationErrors": [], "effects": [],
            **({"targetInstanceId": "inst-o1"}
               if action_id != "act-1" or proposal_id == "prop-1" else
               {"targetRef": {"objectTypeId": "ot-order",
                              "key": {"order_no": "SO-001"}}}),
        })
    db.add(AgentMessage(
        id=f"msg-a-{uuid.uuid4().hex[:8]}", conversation_id=conv.id,
        role="assistant", content="已准备提案", proposals=proposals))
    db.commit()
    return {"oid": oid, "releaseId": str(release.id),
            "conversationId": conv.id}


def _new_intent(db, bound, *, proposal_id, action_id, target_instance=None,
                target_ref=None):
    """同一会话内追加一轮用户意图（新 root intent 解除槽位绑定）。"""
    from app.ontologies.agent_runtime.models import AgentMessage

    user_msg = AgentMessage(id=f"msg-u-{uuid.uuid4().hex[:8]}",
                            conversation_id=bound["conversationId"],
                            role="user", content="再做一次")
    db.add(user_msg)
    db.flush()
    proposal = {
        "kind": "action", "proposalId": proposal_id,
        "actionId": action_id, "actionName": action_id,
        "parameters": {"status": "paid"},
        "releaseId": bound["releaseId"],
        "requiresApproval": action_id == "act-approve",
        "status": "success", "validationErrors": [], "effects": [],
    }
    if target_ref:
        proposal["targetRef"] = target_ref
    else:
        proposal["targetInstanceId"] = target_instance or "inst-o1"
    db.add(AgentMessage(id=f"msg-a-{uuid.uuid4().hex[:8]}",
                        conversation_id=bound["conversationId"],
                        role="assistant", content="已准备提案",
                        proposals=[proposal]))
    db.commit()


def _prepare(client, auth_headers, bound, *, action="act-1",
             target_instance="inst-o1", target_ref=None,
             parameters=None, submission="prop-1"):
    payload = {
        "sessionKind": "agent_runtime", "sessionId": bound["conversationId"],
        "clientSubmissionId": submission, "actionId": action,
        "releaseId": bound["releaseId"],
        "parameters": {"status": "paid"} if parameters is None else parameters,
    }
    if target_instance:
        payload["targetInstanceId"] = target_instance
    if target_ref:
        payload["targetRef"] = target_ref
    return client.post(f"{_fo(bound['oid'])}/agent/proposals",
                       headers=auth_headers, json=payload)


def _decide_and_execute(client, auth_headers, bound, data):
    cid = data["confirmationId"]
    r = client.post(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"],
              "decisionId": f"dec-{cid}", "payloadHash": data["payloadHash"]})
    assert r.status_code == 200, r.text
    r = client.post(
        f"{_fo(bound['oid'])}/agent/execute-proposal", headers=auth_headers,
        json={"confirmationId": cid, "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"]})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_prepare_freezes_plan_without_cas_exposure(
        client, auth_headers, bound, fake_port, db):
    fake_port.reset_scenario(plan=_plan())
    r = _prepare(client, auth_headers, bound)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    asset = data["display"]["target"]["asset"]
    assert asset["kind"] == "manual"
    assert asset["datasetId"] == "ds-asset"
    assert asset["fields"] == ["status"]
    assert asset["key"] == {"order_no": "SO-001"}
    # CAS 私有材料绝不进入任何公开响应
    dumped = json.dumps(data)
    for secret in ("expectedToken", "before", "schemaFingerprint"):
        assert secret not in dumped
    assert data["outcome"] is None
    # 确认前零业务写：端口未收到任何 apply
    assert fake_port.applied == []
    # 冻结计划进入 parameter_sources 保留键（服务端结构）
    from app.ontologies.agent_confirmation import models as CM

    row = db.query(CM.ActionConfirmation).filter_by(
        id=data["confirmationId"]).one()
    plan = row.parameter_sources[WP.WRITE_PLAN_RESERVED_KEY]
    assert plan["expectedToken"] == 3
    # 本地实例不被写：实例属性保持 pending
    from app.models.ontology_formal import ObjectInstance

    inst = db.query(ObjectInstance).filter_by(id="inst-o1").one()
    assert inst.properties["status"] == "pending"


def test_approve_execute_dispatches_frozen_plan_and_outcome(
        client, auth_headers, bound, fake_port):
    fake_port.reset_scenario(
        plan=_plan(),
        apply_result=WP.AssetWriteOutcome(
            asset_kind="manual", status=WP.OUTCOME_APPLIED,
            operation="update", affected_rows=1, dataset_id="ds-asset",
            version_id="ver-2"))
    data = _prepare(client, auth_headers, bound).json()["data"]
    result = _decide_and_execute(client, auth_headers, bound, data)
    assert result["state"] == "succeeded"
    assert result["outcome"] == {
        "assetKind": "manual", "status": "applied", "operation": "update",
        "affectedRows": 1, "datasetId": "ds-asset", "versionId": "ver-2",
    }
    assert len(fake_port.applied) == 1
    assert fake_port.applied[0]["plan"]["expectedToken"] == 3
    assert fake_port.applied[0]["plan"]["values"] == {"status": "paid"}
    # 引擎未运行：executionLogId 为空（资产写不经本地引擎）
    assert result["executionLogId"] is None


def test_retry_after_success_never_reapplies(
        client, auth_headers, bound, fake_port):
    fake_port.reset_scenario(plan=_plan(), apply_result=_applied())
    data = _prepare(client, auth_headers, bound).json()["data"]
    first = _decide_and_execute(client, auth_headers, bound, data)
    cid = data["confirmationId"]
    for _ in range(2):
        r = client.post(
            f"{_fo(bound['oid'])}/agent/execute-proposal",
            headers=auth_headers,
            json={"confirmationId": cid, "sessionKind": "agent_runtime",
                  "sessionId": bound["conversationId"]})
        assert r.status_code == 200
        assert r.json()["data"]["state"] == "succeeded"
    assert len(fake_port.applied) == 1  # 终态回执幂等，不重复派发


def test_deterministic_rejection_zero_source_change(
        client, auth_headers, bound, fake_port):
    fake_port.reset_scenario(
        plan=_plan(),
        apply_error=WP.WritePortError(
            "version_conflict", "数据已更新到 v9，请刷新后重新修改",
            status=409))
    data = _prepare(client, auth_headers, bound).json()["data"]
    result = _decide_and_execute(client, auth_headers, bound, data)
    assert result["state"] == "failed_not_executed"
    assert result["failureCode"] == "version_conflict"
    assert result["outcome"]["status"] == "failed"
    assert result["outcome"]["errorCode"] == "version_conflict"
    assert result["outcome"]["affectedRows"] == 0


def test_unknown_outcome_blocks_replay(
        client, auth_headers, bound, fake_port):
    fake_port.reset_scenario(
        plan=_plan(asset_kind="integrated", expectedToken="tok-1"),
        apply_result=WP.AssetWriteOutcome(
            asset_kind="integrated", status=WP.OUTCOME_UNKNOWN,
            operation="update", affected_rows=0, dataset_id="ds-asset",
            receipt_id="rcp-1", error_code="execution_result_unknown"))
    data = _prepare(client, auth_headers, bound).json()["data"]
    result = _decide_and_execute(client, auth_headers, bound, data)
    assert result["state"] == "unknown"
    assert result["blockReason"] == "execution_result_unknown"
    assert result["outcome"]["status"] == "unknown"
    assert result["outcome"]["receiptId"] == "rcp-1"
    cid = data["confirmationId"]
    r = client.post(
        f"{_fo(bound['oid'])}/agent/execute-proposal", headers=auth_headers,
        json={"confirmationId": cid, "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"]})
    assert r.status_code == 200
    assert r.json()["data"]["state"] == "unknown"
    assert len(fake_port.applied) == 1  # unknown 不自动重放


def test_curated_pending_review_two_layer_state(
        client, auth_headers, bound, fake_port):
    fake_port.reset_scenario(
        plan=_plan(asset_kind="curated", expectedToken="ver-1"),
        apply_result=WP.AssetWriteOutcome(
            asset_kind="curated", status=WP.OUTCOME_PENDING_REVIEW,
            operation="update", affected_rows=0, dataset_id="ds-asset",
            review_id="rev-1"))
    data = _prepare(client, auth_headers, bound).json()["data"]
    result = _decide_and_execute(client, auth_headers, bound, data)
    # 两层状态：动作确认成功 ≠ 数据已生效
    assert result["state"] == "succeeded"
    assert result["outcome"]["status"] == "pending_review"
    assert result["outcome"]["affectedRows"] == 0
    assert result["outcome"]["reviewId"] == "rev-1"
    # 重复 execute 不产生第二条待审
    cid = data["confirmationId"]
    client.post(
        f"{_fo(bound['oid'])}/agent/execute-proposal", headers=auth_headers,
        json={"confirmationId": cid, "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"]})
    assert len(fake_port.applied) == 1

    # 审核批准 → 状态读取只刷新 outcome，不重新执行（每次读取只读刷新）
    fake_port.inspect_result = WP.AssetWriteOutcome(
        asset_kind="curated", status=WP.OUTCOME_APPLIED,
        operation="update", affected_rows=1, dataset_id="ds-asset",
        version_id="ver-1", review_id="rev-1")
    r = client.get(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}",
        headers=auth_headers,
        params={"session_kind": "agent_runtime",
                "session_id": bound["conversationId"]})
    body = r.json()["data"]
    assert body["state"] == "succeeded"
    assert body["outcome"]["status"] == "applied"
    assert body["outcome"]["versionId"] == "ver-1"
    assert fake_port.inspect_calls >= 1
    assert len(fake_port.applied) == 1

    # 审核拒绝 → 值永不生效
    fake_port.inspect_result = WP.AssetWriteOutcome(
        asset_kind="curated", status=WP.OUTCOME_FAILED,
        operation="update", affected_rows=0, dataset_id="ds-asset",
        review_id="rev-1", error_code="curated_review_rejected")
    r = client.get(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}",
        headers=auth_headers,
        params={"session_kind": "agent_runtime",
                "session_id": bound["conversationId"]})
    body = r.json()["data"]
    assert body["outcome"]["status"] == "failed"
    assert body["outcome"]["errorCode"] == "curated_review_rejected"


def test_unsupported_link_combination_rejected_before_write(
        client, auth_headers, bound, fake_port, db):
    fake_port.reset_scenario(plan=_plan())
    r = _prepare(client, auth_headers, bound, action="act-link",
                 parameters={}, submission="prop-link")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "unsupported_combination", r.text
    assert fake_port.applied == []
    # 本地 fo 实例/链接零变更
    from app.models.ontology_formal import LinkInstance

    assert db.query(LinkInstance).count() == 0


def test_delete_row_routes_and_unbound_rejected(
        client, auth_headers, bound, fake_port, db):
    fake_port.reset_scenario(
        plan=_plan(operation="delete", values={}))
    r = _prepare(client, auth_headers, bound, action="act-del",
                 parameters={}, submission="prop-del")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["display"]["target"]["asset"]["operation"] == "delete"
    assert data["display"]["target"]["asset"]["fields"] == []

    # 未绑定类型的 delete_row：prepare 即拒绝（本地引擎无对象删除语义）
    from app.models.ontology import OntologyProject
    from app.models.ontology_version import OntologyVersion

    project = db.query(OntologyProject).filter_by(id=bound["oid"]).one()
    release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    release.snapshot_formal = {**release.snapshot_formal, "mappings": []}
    db.commit()
    _new_intent(db, bound, proposal_id="prop-del2", action_id="act-del")
    r = _prepare(client, auth_headers, bound, action="act-del",
                 parameters={}, submission="prop-del2")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "delete_row_requires_asset"


def test_requires_approval_hitl_gate_then_dispatch_and_reject(
        client, auth_headers, bound, fake_port, db, monkeypatch):
    """资产写 × requiresApproval：确认后进 HITL 审批闸门，批准派发/拒绝结案。"""
    from app.ontologies.agent_confirmation import service as CS

    fake_port.reset_scenario(plan=_plan(), apply_result=_applied())
    data = _prepare(client, auth_headers, bound, action="act-approve",
                    submission="prop-appr").json()["data"]
    cid = data["confirmationId"]
    r = client.post(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"],
              "decisionId": f"dec-{cid}", "payloadHash": data["payloadHash"]})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    # 用户确认 ≠ 派发：进入审批闸门，源写零调用
    assert body["state"] == "pending_approval"
    assert fake_port.applied == []
    from app.models.ontology_formal import ActionExecutionLog

    log = db.query(ActionExecutionLog).filter_by(
        operation_id=body["operationId"]).one()
    assert log.status == "pending"

    # 管理员拒绝 → approval_rejected，源写从未派发
    decision_url = f"{_fo(bound['oid'])}/action-logs/{log.id}/decide"
    rr = client.post(decision_url, headers=auth_headers,
                     json={"decision": "rejected", "reason": "不要改"})
    assert rr.status_code == 200, rr.text
    status = client.get(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}", headers=auth_headers,
        params={"session_kind": "agent_runtime",
                "session_id": bound["conversationId"]}).json()["data"]
    assert status["state"] == "approval_rejected"
    assert status["outcome"]["errorCode"] == "approval_rejected"
    assert fake_port.applied == []

    # 另一轮用户意图走批准路径：管理员批准 → 资产写真实派发
    _new_intent(db, bound, proposal_id="prop-appr2", action_id="act-approve")
    data2 = _prepare(client, auth_headers, bound, action="act-approve",
                     submission="prop-appr2").json()["data"]
    cid2 = data2["confirmationId"]
    r = client.post(
        f"{_fo(bound['oid'])}/agent/proposals/{cid2}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"],
              "decisionId": f"dec-{cid2}",
              "payloadHash": data2["payloadHash"]})
    log2 = db.query(ActionExecutionLog).filter_by(
        operation_id=r.json()["data"]["operationId"]).one()
    ra = client.post(
        f"{_fo(bound['oid'])}/action-logs/{log2.id}/decide",
        headers=auth_headers,
        json={"decision": "approved", "reason": "同意",
              "releaseId": bound["releaseId"]})
    assert ra.status_code == 200, ra.text
    assert len(fake_port.applied) == 1
    status2 = client.get(
        f"{_fo(bound['oid'])}/agent/proposals/{cid2}", headers=auth_headers,
        params={"session_kind": "agent_runtime",
                "session_id": bound["conversationId"]}).json()["data"]
    assert status2["state"] == "succeeded"
    assert status2["outcome"]["status"] == "applied"


def test_crash_between_write_and_finalize_recovers_unknown(
        client, auth_headers, bound, fake_port, db, monkeypatch):
    """源写已执行、结案前崩溃：恢复按未结案 attempt 裁决 unknown 不重放。"""
    from app.ontologies.agent_confirmation import service as CS

    fake_port.reset_scenario(plan=_plan(), apply_result=_applied())
    data = _prepare(client, auth_headers, bound).json()["data"]
    cid = data["confirmationId"]
    r = client.post(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"],
              "decisionId": f"dec-{cid}", "payloadHash": data["payloadHash"]})
    assert r.status_code == 200, r.text
    operation_id = r.json()["data"]["operationId"]

    calls = {"n": 0}
    original = CS._apply_asset_write_result

    def crashing(operation_id_arg, outcome, session=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # 模拟结案前进程退出：源写已回、attempt started 未结案
            raise RuntimeError("simulated crash before finalize")
        return original(operation_id_arg, outcome, session, **kwargs)

    monkeypatch.setattr(CS, "_apply_asset_write_result", crashing)
    with pytest.raises(RuntimeError):
        CS.run_operation_inline(operation_id, db)
    assert len(fake_port.applied) == 1  # 源写确实发生过

    # 模拟 lease 到期后恢复扫描（执行后崩溃、未结案）
    from datetime import datetime, timedelta, timezone as _tz

    db.expire_all()
    from app.ontologies.agent_confirmation import models as CM

    op_row = db.query(CM.ActionOperation).filter_by(id=operation_id).one()
    op_row.lease_expires_at = datetime.now(_tz.utc) - timedelta(seconds=1)
    db.commit()
    CS.reconcile_operation(operation_id, db)
    db.expire_all()
    status = client.get(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}", headers=auth_headers,
        params={"session_kind": "agent_runtime",
                "session_id": bound["conversationId"]}).json()["data"]
    assert status["state"] == "unknown"
    assert status["blockReason"] == "execution_result_unknown"
    # unknown 不自动重放：apply 仍只发生一次
    assert len(fake_port.applied) == 1


def test_unbound_type_keeps_full_local_semantics(
        client, auth_headers, bound, fake_port, db):
    """未绑定类型的多 effect 动作完整保留本地路径（无端口交互）。"""
    from app.models.ontology_version import OntologyVersion
    from app.models.ontology import OntologyProject

    project = db.query(OntologyProject).filter_by(id=bound["oid"]).one()
    release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    release.snapshot_formal = {**release.snapshot_formal, "mappings": []}
    db.commit()

    fake_port.reset_scenario(plan=None)
    r = _prepare(client, auth_headers, bound, action="act-chain",
                 parameters={"order_no": "SO-9"}, submission="prop-chain")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    result = _decide_and_execute(client, auth_headers, bound, data)
    assert result["state"] == "succeeded"
    assert result["outcome"] is None  # 本地动作无资产 outcome
    assert fake_port.resolved == []   # 未绑定不触发端口解析
    # 本地引擎确实执行完整合法链：新建实例 + created_object 链接
    from app.models.ontology_formal import LinkInstance, ObjectInstance

    created = [i for i in db.query(ObjectInstance)
               .filter_by(ontology_id=bound["oid"]).all()
               if i.properties.get("order_no") == "SO-9"]
    assert len(created) == 1
    assert created[0].properties["status"] == "new"
    assert db.query(LinkInstance).filter_by(
        ontology_id=bound["oid"]).count() == 1


def test_target_ref_path_for_remote_rows(
        client, auth_headers, bound, fake_port):
    fake_port.reset_scenario(
        plan=_plan(asset_kind="integrated", expectedToken="tok-9"),
        apply_result=_applied("integrated"))
    r = _prepare(client, auth_headers, bound, target_instance=None,
                 target_ref={"objectTypeId": "ot-order",
                             "key": {"order_no": "SO-001"}},
                 submission="prop-ref")
    assert r.status_code == 201, r.text
    request = fake_port.resolved[0]
    assert request.target_ref is not None
    assert request.target_ref.key == {"order_no": "SO-001"}
    assert request.key_values == {"order_no": "SO-001"}
    data = r.json()["data"]
    result = _decide_and_execute(client, auth_headers, bound, data)
    assert result["outcome"]["assetKind"] == "integrated"


def test_release_drift_blocks_asset_write(
        client, auth_headers, bound, fake_port, db):
    """decide（批准）后发布漂移：execute 的 release 栅栏在源写前拒绝。"""
    from app.models.ontology_version import OntologyVersion
    from app.models.ontology import OntologyProject

    fake_port.reset_scenario(plan=_plan(), apply_result=_applied())
    data = _prepare(client, auth_headers, bound).json()["data"]
    cid = data["confirmationId"]
    r = client.post(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"],
              "decisionId": f"dec-{cid}", "payloadHash": data["payloadHash"]})
    assert r.status_code == 200, r.text
    # 批准后、执行前发布新版本 → 冻结计划跨发布
    project = db.query(OntologyProject).filter_by(id=bound["oid"]).one()
    new_release = OntologyVersion(
        id=f"rel-{uuid.uuid4().hex[:8]}", ontology_id=bound["oid"],
        node_kind="release", lifecycle_status="released",
        version_number=2, snapshot_formal={}, created_by="tester")
    db.add(new_release)
    db.flush()
    project.current_release_id = new_release.id
    db.commit()
    result = client.post(
        f"{_fo(bound['oid'])}/agent/execute-proposal", headers=auth_headers,
        json={"confirmationId": cid, "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"]}).json()["data"]
    assert result["state"] == "failed_not_executed"
    assert result["failureCode"] == "release_context_changed"
    assert fake_port.applied == []  # 漂移在源写之前拒绝


def test_reserved_parameter_key_rejected(
        client, auth_headers, bound, fake_port):
    fake_port.reset_scenario(plan=_plan())
    r = _prepare(client, auth_headers, bound,
                 parameters={"status": "paid", "__write_plan__": {}},
                 submission="prop-reserved")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "invalid_parameter"


def _actor_id(db, bound):
    from app.models.user import User as U

    return db.query(U).filter_by(role="admin").first().id


def test_execute_time_revocation_blocks_source_write(
        client, auth_headers, bound, fake_port, db):
    """A21/A23：prepare 后撤权（菜单/角色/profile/属性级）→ 执行时真实
    拒绝，源写零调用、failed_not_executed。"""
    from app.auth.models import RoleMenuPermission
    from app.ontologies.agent_runtime.models import AgentProfile

    fake_port.reset_scenario(plan=_plan(), apply_result=_applied())
    data = _prepare(client, auth_headers, bound).json()["data"]
    cid = data["confirmationId"]
    r = client.post(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"],
              "decisionId": f"dec-{cid}",
              "payloadHash": data["payloadHash"]})
    assert r.status_code == 200, r.text

    # 1) 批准后、执行前：发起人角色降级 viewer（菜单+本体写+资产湖写
    #    同时失效）→ 执行门禁拒绝，源写零调用
    from app.models.user import User as UserModel

    actor = db.query(UserModel).filter_by(role="admin").first()
    original_role = actor.role
    actor.role = "viewer"
    db.commit()
    # 经服务层执行（worker/kernel 消费路径——路由守卫不在此层，执行门禁
    # 必须自证拦截）
    from app.ontologies.agent_confirmation import service as CS

    confirmation = CS.execute(
        db, actor, bound["oid"], cid, "agent_runtime",
        bound["conversationId"])
    result = CS.confirmation_out(db, confirmation)
    assert result["state"] == "failed_not_executed"
    assert result["failureCode"] == "source_write_denied"
    assert result["outcome"]["status"] == "failed"
    assert fake_port.applied == []
    actor.role = original_role
    db.commit()

    def _approved_cid(proposal_id):
        _new_intent(db, bound, proposal_id=proposal_id, action_id="act-1")
        payload = _prepare(client, auth_headers, bound,
                           submission=proposal_id).json()["data"]
        rr = client.post(
            f"{_fo(bound['oid'])}/agent/proposals/{payload['confirmationId']}"
            f"/decision",
            headers=auth_headers,
            json={"decision": "approve", "sessionKind": "agent_runtime",
                  "sessionId": bound["conversationId"],
                  "decisionId": f"dec-{payload['confirmationId']}",
                  "payloadHash": payload["payloadHash"]})
        assert rr.status_code == 200, rr.text
        return payload["confirmationId"]

    # 2) 批准后撤 profile 提案开关 → 执行门禁拒绝（decide 已被 403 拦截
    #    的路径不复现；此处验证批准后撤权的执行层防线）
    cid2 = _approved_cid("prop-rev2")
    profile = db.query(AgentProfile).filter_by(
        ontology_id=bound["oid"]).one()
    profile.allow_action_proposals = False
    db.commit()
    confirmation2 = CS.execute(
        db, actor, bound["oid"], cid2, "agent_runtime",
        bound["conversationId"])
    result2 = CS.confirmation_out(db, confirmation2)
    assert result2["state"] == "failed_not_executed"
    assert fake_port.applied == []
    profile.allow_action_proposals = True
    db.commit()

    # 3) 批准后属性级收紧（allowed_properties 排除 status）→ 执行拒绝
    cid3 = _approved_cid("prop-rev3")
    profile.allowed_properties = {"ot-order": ["order_no"]}
    db.commit()
    confirmation3 = CS.execute(
        db, actor, bound["oid"], cid3, "agent_runtime",
        bound["conversationId"])
    result3 = CS.confirmation_out(db, confirmation3)
    assert result3["state"] == "failed_not_executed"
    assert fake_port.applied == []


def _unknown_confirmation(client, auth_headers, bound, fake_port, db):
    """构造一个 unknown 态资产确认（fake 端口返回 unknown）。"""
    fake_port.reset_scenario(
        plan=_plan(asset_kind="integrated"),
        apply_result=WP.AssetWriteOutcome(
            asset_kind="integrated", status=WP.OUTCOME_UNKNOWN,
            operation="update", affected_rows=0, dataset_id="ds-asset",
            receipt_id="rcp-u", error_code="execution_result_unknown"))
    data = _prepare(client, auth_headers, bound,
                    submission="prop-unk").json()["data"]
    result = _decide_and_execute(client, auth_headers, bound, data)
    assert result["state"] == "unknown"
    from app.ontologies.agent_confirmation import models as CM0

    op0 = db.query(CM0.ActionOperation).filter_by(
        id=result["operationId"]).one()
    data["_triple"] = {
        "expectedFenceToken": op0.fence_token,
        "expectedGuardHash": op0.guard_hash,
        "expectedPayloadHash": data["payloadHash"],
    }
    return data, result


def test_manual_adjudication_not_executed_authority_path(
        client, auth_headers, bound, fake_port, db):
    """D2 正门：仅 unknown、归属/载荷匹配、必填 rationale+evidence、审计
    追加不可覆写、不重放、终态后 guard 自然释放。"""
    data, result = _unknown_confirmation(client, auth_headers, bound,
                                         fake_port, db)
    cid = data["confirmationId"]
    op_id = result["operationId"]
    url = f"{_fo(bound['oid'])}/agent/proposals/{cid}/adjudicate-not-executed"

    # 非 admin → 403（editor_token 无 admin 依赖放行权）
    from app.auth.models import User as U
    from app.services.auth_service import hash_password

    editor = U(id="usr-editor-x", username="ed", email="e@e.invalid",
               password_hash=hash_password("editorpw"),
               role="editor", is_active=True)
    db.add(editor)
    db.commit()
    login = client.post("/api/v1/auth/login",
                        json={"username": "ed", "password": "editorpw"})
    assert login.status_code == 200, login.text
    if login.status_code == 200:
        editor_headers = {"Authorization":
                          f"Bearer {login.json()['data']['access_token']}"}
        r403 = client.post(url, headers=editor_headers, json={
            "operationId": op_id, "rationale": "x", "evidenceRefs": ["x"],
            **data["_triple"]})
        assert r403.status_code == 403

    # 缺 rationale / evidence → 422
    r = client.post(url, headers=auth_headers, json={
        "operationId": op_id, "rationale": "", "evidenceRefs": ["trace-1"],
        **data["_triple"]})
    assert r.status_code == 422
    r = client.post(url, headers=auth_headers, json={
        "operationId": op_id, "rationale": "对账未应用",
        "evidenceRefs": [], **data["_triple"]})
    assert r.status_code == 422

    # 三 expected 任一失配 → 409，原证据/状态不变
    from app.ontologies.agent_confirmation import models as CMx

    op_row = db.query(CMx.ActionOperation).filter_by(id=op_id).one()
    for key, wrong in (
            ("expectedFenceToken", op_row.fence_token + 7),
            ("expectedGuardHash", "0" * 64),
            ("expectedPayloadHash", "1" * 64)):
        body = {"operationId": op_id, "rationale": "x",
                "evidenceRefs": ["x"], **data["_triple"], key: wrong}
        rm = client.post(url, headers=auth_headers, json=body)
        assert rm.status_code == 409, (key, rm.text)
        assert rm.json()["detail"]["code"] == f"adjudication_{'fence' if 'Fence' in key else 'guard' if 'Guard' in key else 'payload'}_mismatch"
    db.expire_all()
    assert db.query(CMx.ActionOperation).filter_by(
        id=op_id).one().state == "unknown"  # 状态零变化
    assert db.query(CMx.ActionExternalAttempt).filter_by(
        operation_id=op_id).one().state == "unknown"  # 证据零变化

    # 正门裁决成功 → failed_not_executed + outcome 如实 failed
    r = client.post(url, headers=auth_headers, json={
        "operationId": op_id, "rationale": "源端回执与 trace 证明未应用",
        "evidenceRefs": ["artifact:df5-curated-d1-exception.json"],
        **data["_triple"]})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["state"] == "failed_not_executed"
    assert body["failureCode"] == "manual_adjudicated_not_executed"
    assert body["outcome"]["status"] == "failed"
    assert body["outcome"]["errorCode"] == "manual_adjudicated_not_executed"
    # 审计追加且原 unknown 证据保留
    from app.ontologies.agent_confirmation import models as CM

    op = db.query(CM.ActionOperation).filter_by(id=op_id).one()
    adjudications = op.reconcile_evidence["manualAdjudications"]
    assert adjudications[0]["rationale"] == "源端回执与 trace 证明未应用"
    assert adjudications[0]["adjudicatedFenceToken"] == \
        data["_triple"]["expectedFenceToken"]
    assert adjudications[0]["adjudicatedGuardHash"] == \
        data["_triple"]["expectedGuardHash"]
    assert adjudications[0]["adjudicatedPayloadHash"] == \
        data["_triple"]["expectedPayloadHash"]
    assert "reason" in op.reconcile_evidence  # 原 unknown 证据未覆写
    attempt = db.query(CM.ActionExternalAttempt).filter_by(
        operation_id=op_id).one()
    assert attempt.state == "unknown"  # 原 attempt 证据原样保留
    # 不重放：apply 仍只发生过一次
    assert len(fake_port.applied) == 1

    # 重复裁决 → 409（已非 unknown）
    r2 = client.post(url, headers=auth_headers, json={
        "operationId": op_id, "rationale": "again",
        "evidenceRefs": ["x"], **data["_triple"]})
    assert r2.status_code == 409


def test_manual_adjudication_rejects_non_unknown_and_mismatch(
        client, auth_headers, bound, fake_port, db):
    data, _ = _unknown_confirmation(client, auth_headers, bound,
                                    fake_port, db)
    cid = data["confirmationId"]
    url = f"{_fo(bound['oid'])}/agent/proposals/{cid}/adjudicate-not-executed"

    # operation 不匹配 → 409
    r = client.post(url, headers=auth_headers, json={
        "operationId": "not-an-op", "rationale": "x", "evidenceRefs": ["x"],
        "expectedFenceToken": 1, "expectedGuardHash": "0" * 64,
        "expectedPayloadHash": "0" * 64})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] in ("operation_mismatch",
                                          "confirmation_not_found")

    # 非 unknown（succeeded）→ 409（新一轮用户意图避免同槽位冲突）
    fake_port.reset_scenario(plan=_plan(), apply_result=_applied())
    _new_intent(db, bound, proposal_id="prop-ok2b", action_id="act-1")
    data2 = _prepare(client, auth_headers, bound,
                     submission="prop-ok2b").json()["data"]
    result2 = _decide_and_execute(client, auth_headers, bound, data2)
    url2 = (f"{_fo(bound['oid'])}/agent/proposals/"
            f"{data2['confirmationId']}/adjudicate-not-executed")
    r2 = client.post(url2, headers=auth_headers, json={
        "operationId": result2["operationId"], "rationale": "x",
        "evidenceRefs": ["x"],
        "expectedFenceToken": 1, "expectedGuardHash": "0" * 64,
        "expectedPayloadHash": "0" * 64})
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "operation_not_unknown"
    # 成功态未被改动
    status = client.get(
        f"{_fo(bound['oid'])}/agent/proposals/{data2['confirmationId']}",
        headers=auth_headers,
        params={"session_kind": "agent_runtime",
                "session_id": bound["conversationId"]}).json()["data"]
    assert status["state"] == "succeeded"


def test_decide_field_revocation_rejected_at_confirm(
        client, auth_headers, bound, fake_port, db):
    """A21/A23：prepare 后撤 allowed_properties → decide（确认）即拒绝
    403 property_write_forbidden，不等 execute 兜底。"""
    from app.ontologies.agent_runtime.models import AgentProfile

    fake_port.reset_scenario(plan=_plan(), apply_result=_applied())
    data = _prepare(client, auth_headers, bound).json()["data"]
    cid = data["confirmationId"]

    # 撤字段权限：allowed_properties 排除写入属性 status
    profile = db.query(AgentProfile).filter_by(
        ontology_id=bound["oid"]).one()
    profile.allowed_properties = {"ot-order": ["order_no"]}
    db.commit()

    # decide 应 403（不 200 dispatch_queued）
    r = client.post(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}/decision",
        headers=auth_headers,
        json={"decision": "approve", "sessionKind": "agent_runtime",
              "sessionId": bound["conversationId"],
              "decisionId": f"dec-{cid}",
              "payloadHash": data["payloadHash"]})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "property_write_forbidden"
    assert fake_port.applied == []

    # 状态 expired（需重新 prepare）
    status = client.get(
        f"{_fo(bound['oid'])}/agent/proposals/{cid}", headers=auth_headers,
        params={"session_kind": "agent_runtime",
                "session_id": bound["conversationId"]}).json()["data"]
    assert status["state"] == "expired"
    assert status["failureCode"] == "property_write_forbidden"

    # 恢复后新 prepare 可通过（正门重验）
    profile.allowed_properties = None
    db.commit()
    _new_intent(db, bound, proposal_id="prop-field-restore",
                action_id="act-1")
    r2 = _prepare(client, auth_headers, bound,
                  submission="prop-field-restore")
    assert r2.status_code == 201, r2.text


def test_guard_hash_logical_identity_across_cas_snapshots():
    """同业务意图不同 CAS 快照的 guard 一致（并发互斥）；值不同则互斥。"""
    from app.ontologies.agent_confirmation import service as CS

    def _confirmation(plan):
        sources = {}
        if plan is not None:
            sources[WP.WRITE_PLAN_RESERVED_KEY] = plan
        return CS.M.ActionConfirmation(
            actor_user_id="u1", ontology_id="o1", action_id="act-1",
            target_instance_id="inst-o1",
            final_parameters={"status": "paid"}, parameter_sources=sources)

    plan_a = _plan(expectedToken=3)
    plan_b = _plan(expectedToken=4)  # 源快照不同（并发重新准备）
    assert CS._guard_hash(_confirmation(plan_a)) == \
        CS._guard_hash(_confirmation(plan_b))
    plan_c = _plan(values={"status": "cancelled"})
    assert CS._guard_hash(_confirmation(plan_a)) != \
        CS._guard_hash(_confirmation(plan_c))
    # 旧确认（无计划）guard 语义保持不变：无 writePlan 键参与
    legacy = CS._guard_hash(_confirmation(None))
    manual = CS._sha256(CS._canonical_json({
        "actor": "u1", "ontology": "o1", "action": "act-1",
        "target": "inst-o1", "parameters": {"status": "paid"},
    }))
    assert legacy == manual
