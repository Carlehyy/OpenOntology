"""对账层测试：审批过期 / 租约回收 / 派发重投 / 并行补位。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.task_instances.conftest import (
    activate,
    execute,
    get_instance,
    latest_run,
    make_spec,
)
from app.task_instances import reconcile as ti_reconcile
from app.task_instances.models import (
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    INSTANCE_ACTIVE,
    NODE_PENDING,
    NODE_RUNNING,
    TaskApproval,
)

PARALLEL_SPEC = """
api_version: openontology.task/v1
kind: Workflow
metadata: { name: 并行预算演示流程 }
nodes:
  a: { kind: agent, system: a }
  b: { kind: agent, system: b }
  merge: { kind: join, mode: all }
  end_ok: { kind: terminal, outcome: success }
edges:
  - { from: a.done, to: merge }
  - { from: b.done, to: merge }
  - { from: merge.done, to: end_ok }
policies:
  max_parallelism: 1
"""


def _past(hours: float = 1.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


class TestApprovalExpiry:
    def test_expired_approval_auto_rejected(
            self, db, monkeypatch):
        dispatched: list[str] = []
        from app.task_instances import executor as ti_executor
        monkeypatch.setattr(
            "app.task_instances.service._dispatch_node",
            lambda node_run_id: (
                dispatched.append(node_run_id),
                ti_executor.execute_dispatch_on(db, node_run_id)))
        simulate = {"outputs": {
            "analyze": {"category": "defect_fix", "summary": "s"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        gate = latest_run(db, instance.id, "gate")
        approval = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate.id).first()
        approval.expires_at = _past()
        db.commit()
        expired = ti_reconcile.expire_approvals_once(db)
        assert expired == 1
        db.expire_all()
        fresh = db.query(TaskApproval).filter(
            TaskApproval.id == approval.id).first()
        assert fresh.status == APPROVAL_REJECTED
        assert fresh.decided_by == "system:expiry"
        # 驳回路由生效：auto_fix 被打回重做
        assert latest_run(db, instance.id, "auto_fix").attempt_no == 2


class TestLeaseReclaim:
    def test_expired_running_reclaimed_with_correction(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "defect_fix", "summary": "s"}},
            "hold_nodes": ["auto_fix"]}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)  # analyze 完成 → auto_fix 认领后被 hold 在 running
        held = latest_run(db, instance.id, "auto_fix")
        assert held.status == NODE_RUNNING
        held.lease_expires_at = _past()
        db.commit()
        counts = ti_reconcile.reclaim_and_redispatch_once(db)
        assert counts["reclaimed"] >= 1
        fresh = latest_run(db, instance.id, "auto_fix")
        assert fresh.attempt_no == 2  # 纠正重派
        assert get_instance(db, instance.id).status == INSTANCE_ACTIVE


class TestParallelPromotion:
    def test_pending_promoted_when_capacity_frees(self, db, monkeypatch):
        from app.task_instances import executor as ti_executor

        monkeypatch.setattr(
            "app.task_instances.service._dispatch_node",
            lambda node_run_id: ti_executor.execute_dispatch_on(
                db, node_run_id))
        instance, result = activate(
            db, PARALLEL_SPEC,
            simulate={"outputs": {"a": {"s": 1}, "b": {"s": 2}}})
        # 并行预算 1：a 派发、b 落 pending
        run_a = latest_run(db, instance.id, "a")
        run_b = latest_run(db, instance.id, "b")
        assert run_b.status == NODE_PENDING
        execute(db, result)  # a 执行完成
        assert latest_run(db, instance.id, "a").status == "completed"
        # 容量释放 → 对账补位 b（内联派发使其直接完成 → join 汇合 → 收束）
        counts = ti_reconcile.reclaim_and_redispatch_once(db)
        assert counts["promoted"] >= 1
        assert latest_run(db, instance.id, "b").status == "completed"
        assert get_instance(db, instance.id).status == "completed"
