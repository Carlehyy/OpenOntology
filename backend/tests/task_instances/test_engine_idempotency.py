"""幂等与版本化测试（M1 验收 §13）。

- inputs_hash 派发幂等：重复交活被守卫拦截、同输入不建重复尝试；
- 激活幂等：同 Idempotency-Key 复用实例；
- canonical_hash 版本幂等：内容不变不建新 revision。
"""
from __future__ import annotations

import pytest

from tests.task_instances.conftest import (
    activate,
    events_of,
    execute,
    get_instance,
    latest_run,
    make_revision,
    make_spec,
    runs_of,
)
from app.task_instances import engine as ti_engine
from app.task_instances import service as ti_service
from app.task_instances.models import TaskInstance, TaskNodeRun


class TestDispatchIdempotency:
    def test_double_completion_rejected(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "frontend", "summary": "s"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        human = latest_run(db, instance.id, "human_review")
        ti_engine.complete_node(db, human.id, {"summary": "x"}, actor="u")
        db.commit()
        with pytest.raises(ti_engine.EngineGuardError):
            ti_engine.complete_node(db, human.id, {"summary": "y"}, actor="u")
        db.rollback()

    def test_same_inputs_no_duplicate_attempt(self, db):
        # 两次独立交活（第二次先作废重开）不产生同输入重复行：
        # 交活 → 打回 → 上游同产出 → 人工重开（attempt_no 不变）
        simulate = {"outputs": {
            "analyze": {"category": "frontend", "summary": "固定"}}}
        instance, result = activate(db, make_spec(), simulate=simulate)
        execute(db, result)
        before = latest_run(db, instance.id, "human_review")
        reject_result = ti_engine.human_reject(
            db, before.id, reason="重做", rejected_by=None)
        db.commit()
        execute(db, reject_result)  # analyze 重做（同产出）→ 人工重开
        after = latest_run(db, instance.id, "human_review")
        assert after.id == before.id  # 同输入原地重开
        assert after.attempt_no == before.attempt_no
        types = [t for t, _ in events_of(db, instance.id)]
        assert types.count("node.reopened") == 1

    def test_unique_constraint_guards_duplicate_hash(self, db):
        simulate = {"outputs": {
            "analyze": {"category": "frontend", "summary": "s"}}}
        instance, _ = activate(db, make_spec(), simulate=simulate)
        clone = TaskNodeRun(
            instance_id=instance.id, node_id="clone_node",
            attempt_no=1, status="pending", inputs=[],
            inputs_hash="deadbeef" * 8)
        db.add(clone)
        db.commit()
        db.add(TaskNodeRun(
            instance_id=instance.id, node_id="clone_node",
            attempt_no=2, status="pending", inputs=[],
            inputs_hash="deadbeef" * 8))
        with pytest.raises(Exception):
            db.commit()
        db.rollback()


class TestActivationIdempotency:
    def test_same_key_reuses_instance(self, db):
        revision = make_revision(db, make_spec())
        one, r1 = ti_engine.activate(
            db, revision=revision, name="i1", goal="g",
            inputs={}, created_by=None, idempotency_key="key-1")
        db.commit()
        two, r2 = ti_engine.activate(
            db, revision=revision, name="i2", goal="g",
            inputs={}, created_by=None, idempotency_key="key-1")
        db.commit()
        assert one.id == two.id
        assert db.query(TaskInstance).filter(
            TaskInstance.idempotency_key == "key-1").count() == 1


class TestRevisionCanonicity:
    def test_same_content_no_new_revision(self, db):
        from tests.task_instances.conftest import SPEC_TEMPLATE

        revision = make_revision(db, make_spec())
        template_id = revision.template_id
        from app.task_instances.models import TaskTemplate, TaskTemplateRevision
        # 内容不变 → 复用既有 revision
        spec2, hash2, canonical2 = ti_service.compile_and_validate(
            make_spec())
        existing = db.query(TaskTemplateRevision).filter(
            TaskTemplateRevision.template_id == template_id,
            TaskTemplateRevision.canonical_hash == hash2).first()
        assert existing is not None
        assert existing.id == revision.id
        # 内容变化 → 新 revision（service 层语义在 router 测试覆盖）
        spec3, hash3, _ = ti_service.compile_and_validate(
            make_spec(corrections=5))
        assert hash3 != hash2
