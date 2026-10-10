"""引擎黄金事件轨迹测试（M1 验收 §13）。

轨迹 1（条件分流 → 人工 → 审批批准）：frontend 分类进 human_review，
    人工交活 → 关口审批 → terminal success。
轨迹 2（契约纠正环）：defect_fix 分类的 auto_fix 首次交活违反端口契约，
    纠正重派 → 二次交活合规 → 关口 → 完成；以及纠正预算耗尽 → 实例失败。
轨迹 3（打回-修复环）：审批驳回（携理由）→ auto_fix 重做 → 再审批 →
    完成；打回预算耗尽 → ReworkBudgetExhausted。
"""
from __future__ import annotations

import pytest

from tests.task_instances.conftest import (
    activate,
    events_of,
    execute,
    get_instance,
    latest_run,
    make_spec,
    runs_of,
)
from app.task_instances import engine as ti_engine
from app.task_instances.models import (
    APPROVAL_PENDING,
    INSTANCE_ACTIVE,
    INSTANCE_COMPLETED,
    INSTANCE_FAILED,
    NODE_COMPLETED,
    NODE_WAITING_APPROVAL,
    NODE_WAITING_HUMAN,
    TaskApproval,
)


def _trajectory(db, instance_id):
    return [(t, (p or {}).get("node_id", ""), (p or {}).get("attempt_no", ""))
            for t, p in events_of(db, instance_id)]


class TestTrajectoryOneHumanApproval:
    def test_frontend_route_full_trajectory(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "frontend", "summary": "涉及登录页"},
            "human_review": {"summary": "人工评审通过", "verdict": "ok"},
        }}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        assert get_instance(db, instance.id).status == INSTANCE_ACTIVE
        # analyze 完成 → condition 命中 frontend → human_review 等待人工
        human = latest_run(db, instance.id, "human_review")
        assert human.status == NODE_WAITING_HUMAN
        traj = _trajectory(db, instance.id)
        assert traj[:7] == [
            ("instance.created", "", ""),
            ("node.created", "analyze", 1),
            ("node.dispatched", "analyze", 1),
            ("node.claimed", "analyze", 1),
            ("node.completed", "analyze", 1),
            ("node.created", "human_review", 1),
            ("human.assigned", "human_review", 1),
        ]
        assert traj[5][0] == "node.created"
        # 人工交活
        result2 = ti_engine.complete_node(
            db, human.id,
            {"summary": "人工评审通过", "verdict": "ok"}, actor="user:t1")
        db.commit()
        execute(db, result2)
        gate = latest_run(db, instance.id, "gate")
        assert gate.status == NODE_WAITING_APPROVAL
        approval = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate.id).first()
        assert approval.status == APPROVAL_PENDING
        import json as _json
        assert "frontend" in _json.dumps(approval.proposal, ensure_ascii=False)
        # 审批批准 → terminal
        result3 = ti_engine.decide_approval(
            db, approval.id, "approved", reason=None, decided_by=None)
        db.commit()
        execute(db, result3)
        fresh = get_instance(db, instance.id)
        assert fresh.status == INSTANCE_COMPLETED
        traj = _trajectory(db, instance.id)
        assert traj[-4:] == [
            ("approval.requested", "gate", ""),
            ("approval.decided", "gate", ""),
            ("node.completed", "gate", 1),
            ("instance.terminal", "", ""),
        ]

    def test_backend_route_goes_default_to_human(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "backend", "summary": "后端改动"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        assert latest_run(db, instance.id,
                          "human_review").status == NODE_WAITING_HUMAN
        assert latest_run(db, instance.id, "auto_fix") is None


class TestTrajectoryTwoCorrectionLoop:
    def test_contract_violation_then_correction(self, db):
        simulate = {
            "outputs": {
                "analyze": {"category": "defect_fix", "summary": "崩溃"},
            },
            "bad_outputs": {"auto_fix": [1]},
        }
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)  # analyze → auto_fix#1 违规 → 纠正 → auto_fix#2 合规
        traj = _trajectory(db, instance.id)
        types = [t[0] for t in traj]
        assert "contract.violated" in types
        assert "node.correction_requested" in types
        second = latest_run(db, instance.id, "auto_fix")
        assert second.attempt_no == 2
        assert second.status == NODE_COMPLETED  # 纠正后合规交活
        gate = latest_run(db, instance.id, "gate")
        assert gate.status == NODE_WAITING_APPROVAL
        # 纠正产出的 patch_summary 满足契约
        assert second.output["patch_summary"]

    def test_correction_budget_exhaustion_fails_instance(self, db):
        simulate = {
            "outputs": {
                "analyze": {"category": "defect_fix", "summary": "崩溃"}},
            "bad_outputs": {"auto_fix": [1, 2, 3]},
        }
        instance, result = activate(
            db, make_spec(corrections=1), simulate=simulate)
        execute(db, result)
        fresh = get_instance(db, instance.id)
        assert fresh.status == INSTANCE_FAILED
        types = [t for t, _, _ in _trajectory(db, instance.id)]
        assert types.count("contract.violated") == 2
        assert "node.failed" in types
        assert "instance.terminal" in types


class TestTrajectoryThreeReworkLoop:
    def _drive_to_gate(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "defect_fix", "summary": "崩溃"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        return instance

    def test_reject_rework_then_approve(self, db):
        instance = self._drive_to_gate(db)
        gate = latest_run(db, instance.id, "gate")
        approval = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate.id).first()
        # 第一次驳回：auto_fix 重做
        result = ti_engine.decide_approval(
            db, approval.id, "rejected", reason="补丁不完整", decided_by=None)
        db.commit()
        execute(db, result)
        traj = _trajectory(db, instance.id)
        types = [t[0] for t in traj]
        assert "node.rejected" in types
        reworked = latest_run(db, instance.id, "auto_fix")
        assert reworked.attempt_no == 2
        assert reworked.rework_count == 1
        assert reworked.status == NODE_COMPLETED
        # 重做完成后再次等待审批
        gate2 = latest_run(db, instance.id, "gate")
        assert gate2.status == NODE_WAITING_APPROVAL
        approval2 = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate2.id,
            TaskApproval.status == APPROVAL_PENDING).first()
        assert approval2 is not None
        # 驳回理由进入重做上下文
        inputs = {i.get("kind"): i for i in reworked.inputs}
        assert inputs["rejection"]["reason"] == "补丁不完整"
        # 批准 → 完成
        result2 = ti_engine.decide_approval(
            db, approval2.id, "approved", reason=None, decided_by=None)
        db.commit()
        execute(db, result2)
        assert get_instance(db, instance.id).status == INSTANCE_COMPLETED

    def test_reject_reason_required(self, db):
        instance = self._drive_to_gate(db)
        gate = latest_run(db, instance.id, "gate")
        approval = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate.id).first()
        with pytest.raises(Exception):
            ti_engine.decide_approval(
                db, approval.id, "rejected", reason="", decided_by=None)
        db.rollback()

    def test_rework_budget_exhausted(self, db):
        instance = self._drive_to_gate(db)
        # rework_per_edge 默认 3 → 前三次驳回合法，第四次触发熔断
        for round_no in range(3):
            gate = latest_run(db, instance.id, "gate")
            approval = db.query(TaskApproval).filter(
                TaskApproval.node_run_id == gate.id,
                TaskApproval.status == APPROVAL_PENDING).first()
            result = ti_engine.decide_approval(
                db, approval.id, "rejected", reason=f"第{round_no}次驳回",
                decided_by=None)
            db.commit()
            execute(db, result)
        gate = latest_run(db, instance.id, "gate")
        approval = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate.id,
            TaskApproval.status == APPROVAL_PENDING).first()
        with pytest.raises(ti_engine.ReworkBudgetExhausted):
            ti_engine.decide_approval(
                db, approval.id, "rejected", reason="超出预算",
                decided_by=None)
        db.rollback()
        # 预算拒绝不得破坏现场：实例仍 active、第三次重做已完成
        assert get_instance(db, instance.id).status == INSTANCE_ACTIVE
        assert latest_run(db, instance.id,
                          "auto_fix").attempt_no == 4


class TestHumanReject:
    def test_human_rejects_direct_upstream(self, db):
        # frontend 路径：human_review 的直接上游是 analyze
        simulate = {"outputs": {
            "analyze": {"category": "frontend", "summary": "初次摘要"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        human = latest_run(db, instance.id, "human_review")
        result2 = ti_engine.human_reject(
            db, human.id, reason="摘要遗漏兼容性影响", rejected_by=None)
        db.commit()
        execute(db, result2)
        traj = _trajectory(db, instance.id)
        types = [t[0] for t in traj]
        assert "node.rejected" in types
        assert types.count("node.voided") >= 2  # 打回方 + 被打回上游
        reworked = latest_run(db, instance.id, "analyze")
        assert reworked.attempt_no == 2
        assert reworked.status == NODE_COMPLETED
        # analyze 重做后（simulate 产出不变）→ condition 再路由 → 人工再等待
        assert latest_run(db, instance.id,
                          "human_review").status == NODE_WAITING_HUMAN

    def test_human_reject_requires_reason(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "frontend", "summary": "s"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        human = latest_run(db, instance.id, "human_review")
        with pytest.raises(ti_engine.EngineGuardError):
            ti_engine.human_reject(db, human.id, reason="  ", rejected_by=None)
        db.rollback()


class TestJoinSemantics:
    JOIN_SPEC = """
api_version: openontology.task/v1
kind: Workflow
metadata: { name: 汇合演示流程 }
nodes:
  a: { kind: agent, system: a }
  b: { kind: agent, system: b }
  merge:
    kind: join
    mode: all
  out: { kind: agent, system: 汇合后处理 }
  end_ok: { kind: terminal, outcome: success }
edges:
  - { from: a.done, to: merge }
  - { from: b.done, to: merge }
  - { from: merge.done, to: out }
  - { from: out.done, to: end_ok }
"""

    def test_all_join_waits_for_both(self, db):
        simulate = {"hold_nodes": ["b"]}
        instance, result = activate(db, self.JOIN_SPEC, simulate=simulate)
        execute(db, result)  # a 完成，b 认领后被 hold
        assert latest_run(db, instance.id, "merge") is None
        # 释放 b：手动交活
        held = latest_run(db, instance.id, "b")
        result2 = ti_engine.complete_node(
            db, held.id, {"summary": "b done"}, actor="executor:fake")
        db.commit()
        execute(db, result2)
        merge = latest_run(db, instance.id, "merge")
        assert merge is not None
        assert merge.status == NODE_COMPLETED
        assert set((merge.output or {}).get("inputs", {})) == {"a", "b"}
        assert get_instance(db, instance.id).status == INSTANCE_COMPLETED
