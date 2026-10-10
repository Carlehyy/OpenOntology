"""取消 vs 推进竞态测试（M1 验收：50+ 次重复，终态一致性）。

以两种交错顺序（先取消后交活 / 先交活后取消）+ 确定性轮转各跑 25 次，
断言 qwenpaw T11 同型不变量：
  - 事件 seq 恰好 1..N 无空洞（(instance, seq) 唯一约束兜底）
  - 至多一个 instance.terminal
  - terminal 之后不再出现任何节点事件
  - 终态后节点行全部落在终态集合
生产级互斥由 PG 实例行锁（_lock_instance + 锁后 refresh）保证，
staging 真实环境 E2E（M2）再做数据库级并发验证。
"""
from __future__ import annotations

import random

from sqlalchemy.orm import Session

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
    INSTANCE_ACTIVE,
    INSTANCE_CANCELLED,
    INSTANCE_COMPLETED,
    TaskEvent,
    TaskNodeRun,
)

_TERMINAL_NODE_STATES = {
    "completed", "failed", "voided", "skipped", "cancelled",
}
_TERMINAL_INSTANCE_STATES = {
    INSTANCE_COMPLETED, INSTANCE_CANCELLED, "failed",
}


def _assert_invariants(db: Session, instance_id: str) -> None:
    events = db.query(TaskEvent).filter(
        TaskEvent.instance_id == instance_id).order_by(TaskEvent.seq).all()
    seqs = [event.seq for event in events]
    assert seqs == list(range(1, len(seqs) + 1)), f"事件 seq 有空洞: {seqs}"

    terminal_idx = [i for i, event in enumerate(events)
                    if event.type == "instance.terminal"]
    assert len(terminal_idx) <= 1
    if terminal_idx:
        cut = terminal_idx[0]
        later = [event.type for event in events[cut + 1:]]
        assert later == [], f"terminal 之后仍有事件: {later}"

    instance = get_instance(db, instance_id)
    if instance.status in _TERMINAL_INSTANCE_STATES:
        for run in runs_of(db, instance_id):
            assert run.status in _TERMINAL_NODE_STATES, (
                f"终态实例存在非终态节点 {run.node_id}:{run.status}")


class TestCancelVersusAdvance:
    def test_50_interleavings_terminal_consistency(self, db):
        rng = random.Random(20261011)
        outcomes = {"cancelled": 0, "advanced": 0}
        for iteration in range(50):
            simulate = {"outputs": {
                "analyze": {"category": "frontend",
                            "summary": f"迭代{iteration}"}}}
            instance, result = activate(db, make_spec(), simulate=simulate)
            execute(db, result)
            human = latest_run(db, instance.id, "human_review")
            assert human is not None and human.status == "waiting_human"
            cancel_first = (iteration % 2 == 0) or (rng.random() < 0.3)
            if cancel_first:
                ti_engine.cancel_instance(
                    db, instance.id, reason="竞态测试", actor=None)
                db.commit()
                try:
                    ti_engine.complete_node(
                        db, human.id, {"summary": "late"}, actor="user:t")
                    db.commit()
                except ti_engine.EngineGuardError:
                    db.rollback()
                outcomes["cancelled"] += 1
            else:
                result2 = ti_engine.complete_node(
                    db, human.id, {"summary": "early"}, actor="user:t")
                db.commit()
                ti_engine.cancel_instance(
                    db, instance.id, reason="竞态测试", actor=None)
                db.commit()
                outcomes["advanced"] += 1
            fresh = get_instance(db, instance.id)
            assert fresh.status in (INSTANCE_CANCELLED, INSTANCE_ACTIVE,
                                    INSTANCE_COMPLETED, "failed")
            _assert_invariants(db, instance.id)
            if fresh.status == INSTANCE_ACTIVE:
                # 交活先行的路径应已推进到关口等待
                assert latest_run(db, instance.id,
                                  "gate") is not None
        assert outcomes["cancelled"] + outcomes["advanced"] == 50

    def test_cancel_freezes_pending_nodes(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "frontend", "summary": "s"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        ti_engine.cancel_instance(db, instance.id, reason="freeze", actor=None)
        db.commit()
        fresh = get_instance(db, instance.id)
        assert fresh.status == INSTANCE_CANCELLED
        human = latest_run(db, instance.id, "human_review")
        assert human.status == "cancelled"
        types = [t for t, _ in events_of(db, instance.id)]
        assert types[-1] == "instance.terminal"

    def test_cancel_terminal_instance_is_noop(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "defect_fix", "summary": "s"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        gate = latest_run(db, instance.id, "gate")
        from app.task_instances.models import TaskApproval
        approval = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate.id).first()
        ti_engine.decide_approval(
            db, approval.id, "approved", reason=None, decided_by=None)
        db.commit()
        assert get_instance(db, instance.id).status == INSTANCE_COMPLETED
        again = ti_engine.cancel_instance(
            db, instance.id, reason="late", actor=None)
        db.commit()
        assert again.status == INSTANCE_COMPLETED  # 已终态不再变更
