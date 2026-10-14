"""任务实例域测试共享设施。

- SPEC_TEMPLate：设计文档 §4.6 条件分流场景（前端→人工 / 缺陷→agent）
  + auto_fix 端口契约（供纠正环测试）；
- activate/execute：夹具库上的引擎直驱助手（绕过 NATS）；
- events_of：事件流投影（type + 关键字段）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.task_instances import engine as ti_engine
from app.task_instances import executor as ti_executor
from app.task_instances.models import (
    TaskInstance,
    TaskNodeRun,
    TaskTemplate,
    TaskTemplateRevision,
)
from app.task_instances.spec import compile_workflow_yaml

SPEC_TEMPLATE = """
api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 代码变更评审
contracts:
  ChangeSummary:
    type: object
    required: [category, summary]
    properties:
      category: { enum: [frontend, backend, defect_fix] }
      summary: { type: string, minLength: 1 }
  PatchResult:
    type: object
    required: [patch_summary]
    properties:
      patch_summary: { type: string, minLength: 1 }
nodes:
  analyze:
    kind: agent
    system: 分析变更并产出结构化摘要
    outputs: { done: { contract: ChangeSummary } }
  route:
    kind: condition
    on: analyze.done
    branches:
      - { when: { field: category, equals: frontend }, to: [human_review] }
      - { when: { field: category, equals: defect_fix }, to: [auto_fix] }
    default: [human_review]
  human_review:
    kind: human
    role: 前端变更评审（可修改产出再交活）
  auto_fix:
    kind: agent
    system: 按上游摘要执行缺陷修复
    outputs: { done: { contract: PatchResult } }
  merge:
    kind: join
    mode: any
  gate:
    kind: approval
    approvers: [admin]
    expires_hours: 72
  end_ok: { kind: terminal, outcome: success }
edges:
  - { from: human_review.done, to: merge }
  - { from: auto_fix.done, to: merge }
  - { from: merge.done, to: gate }
  - { from: gate.approved, to: end_ok }
  - { from: gate.rejected, to: auto_fix, rework: true }
policies:
  max_parallelism: __MAX_PARALLELISM__
  corrections_per_node: __CORRECTIONS__
  rework_per_edge: __REWORK__
"""


def make_spec(*, max_parallelism: int = 8, corrections: int = 2,
              rework: int = 3) -> str:
    return (SPEC_TEMPLATE
            .replace("__MAX_PARALLELISM__", str(max_parallelism))
            .replace("__CORRECTIONS__", str(corrections))
            .replace("__REWORK__", str(rework)))


def make_revision(db: Session, spec_yaml: str) -> TaskTemplateRevision:
    template = TaskTemplate(name="tpl")
    db.add(template)
    db.flush()
    compiled = compile_workflow_yaml(spec_yaml)
    revision = TaskTemplateRevision(
        template_id=template.id, revision_no=1,
        spec_yaml=spec_yaml, spec_compiled=compiled.canonical,
        canonical_hash=compiled.canonical_hash)
    db.add(revision)
    db.flush()
    template.latest_revision_id = revision.id
    db.commit()
    return revision


def activate(db: Session, spec_yaml: str, *, simulate: dict | None = None,
             goal: str = "测试目标", name: str = "测试实例"):
    revision = make_revision(db, spec_yaml)
    instance, result = ti_engine.activate(
        db, revision=revision, name=name, goal=goal,
        inputs={"__simulate": simulate} if simulate else {}, created_by=None)
    db.commit()
    return instance, result


def execute(db: Session, result: ti_engine.EngineResult) -> None:
    """内联执行派发结果并循环推进级联（绕过 NATS，复用假执行器逻辑）。"""
    pending = list(result.dispatches)
    guard = 0
    while pending:
        guard += 1
        assert guard <= 25, "级联派发超过安全上限，疑似循环"
        cascades: list[str] = []
        for node_run_id in pending:
            inner = ti_executor.execute_dispatch_on(db, node_run_id)
            if inner is not None:
                cascades.extend(inner.dispatches)
        db.commit()
        pending = cascades


def events_of(db: Session, instance_id: str) -> list[tuple[str, dict]]:
    from app.task_instances.models import TaskEvent

    rows = db.query(TaskEvent).filter(
        TaskEvent.instance_id == instance_id,
    ).order_by(TaskEvent.seq).all()
    return [(row.type, row.payload or {}) for row in rows]


def runs_of(db: Session, instance_id: str) -> list[TaskNodeRun]:
    return db.query(TaskNodeRun).filter(
        TaskNodeRun.instance_id == instance_id,
    ).order_by(TaskNodeRun.created_at, TaskNodeRun.attempt_no).all()


def latest_run(db: Session, instance_id: str, node_id: str) -> TaskNodeRun:
    return (
        db.query(TaskNodeRun)
        .filter(TaskNodeRun.instance_id == instance_id,
                TaskNodeRun.node_id == node_id)
        .order_by(TaskNodeRun.attempt_no.desc())
        .first()
    )


def get_instance(db: Session, instance_id: str) -> TaskInstance:
    db.expire_all()
    return db.query(TaskInstance).filter(
        TaskInstance.id == instance_id).first()


@pytest.fixture
def inline_dispatch(db, monkeypatch):
    """API 层测试用：把 service 的 NATS 派发替换为夹具库内联执行。"""
    from app.task_instances import service as ti_service

    monkeypatch.setattr(
        ti_service, "_dispatch_node",
        lambda node_run_id: ti_executor.execute_dispatch_on(db, node_run_id))
    monkeypatch.setattr(ti_service, "_dispatch_steering",
                        lambda *args, **kwargs: None)
    return ti_service
