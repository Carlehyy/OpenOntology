"""Homerail 模板移植验收（M4）：pr-review / release-notes / topic-outline
适配为平台 WorkflowSpec，结构性校验全绿 + pr-review 走一条假执行器轨迹。

移植对照（Homerail 概念 → 平台语言）：
- pr-review 的多模型 2/3 仲裁评审 → 三个 reviewer agent + join(all) 汇合
  + Verdict 契约 + approval 关口（人对合并拍板）+ 驳回打回 reviewer 链；
- release-notes / topic-outline 的 run_input + 输出契约 → 实例 inputs +
  端口契约（notes/outline schema 收紧）+ terminal。
"""
from __future__ import annotations

from tests.task_instances.conftest import (
    activate,
    events_of,
    execute,
    get_instance,
    latest_run,
)
from app.task_instances.spec import compile_workflow_yaml
from app.task_instances.validator import validate_workflow

PR_REVIEW_TEMPLATE = """
api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 变更评审三仲裁
  description: Homerail pr-review 移植：三评审仲裁 + 人把关合并
contracts:
  Verdict:
    type: object
    required: [verdict, confidence, rationale]
    properties:
      verdict: { enum: [approve, request_changes] }
      confidence: { type: number, minimum: 0, maximum: 1 }
      rationale: { type: string, minLength: 10 }
  ReviewSummary:
    type: object
    required: [consensus, combined_rationale]
    properties:
      consensus: { enum: [approve, request_changes] }
      combined_rationale: { type: string, minLength: 10 }
nodes:
  reviewer_a:
    kind: agent
    system: 资深后端评审视角，按 Verdict 契约给出裁决
    outputs: { done: { contract: Verdict } }
  reviewer_b:
    kind: agent
    system: 安全与依赖视角评审，按 Verdict 契约给出裁决
    outputs: { done: { contract: Verdict } }
  reviewer_c:
    kind: agent
    system: 测试覆盖视角评审，按 Verdict 契约给出裁决
    outputs: { done: { contract: Verdict } }
  merge_review:
    kind: join
    mode: all
  summarize:
    kind: agent
    system: 汇总三方裁决为 ReviewSummary（consensus 按 2/3 多数）
    outputs: { done: { contract: ReviewSummary } }
  human_gate:
    kind: approval
    approvers: [admin]
  merge_ok: { kind: terminal, outcome: success }
edges:
  - { from: reviewer_a.done, to: merge_review }
  - { from: reviewer_b.done, to: merge_review }
  - { from: reviewer_c.done, to: merge_review }
  - { from: merge_review.done, to: summarize }
  - { from: summarize.done, to: human_gate }
  - { from: human_gate.approved, to: merge_ok }
  - { from: human_gate.rejected, to: reviewer_a, rework: true }
"""

RELEASE_NOTES_TEMPLATE = """
api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 发布说明生成
  description: Homerail release-notes 移植：从变更清单产出发布说明
contracts:
  ReleaseNotes:
    type: object
    required: [title, highlights, breaking_changes]
    properties:
      title: { type: string, minLength: 5 }
      highlights: { type: array, minItems: 1, items: { type: string } }
      breaking_changes: { type: array, items: { type: string } }
nodes:
  draft:
    kind: agent
    system: 依据实例入参 changes 产出用户视角的发布说明（ReleaseNotes 契约）
    outputs: { done: { contract: ReleaseNotes } }
  gate: { kind: approval, approvers: [admin] }
  done: { kind: terminal, outcome: success }
edges:
  - { from: draft.done, to: gate }
  - { from: gate.approved, to: done }
  - { from: gate.rejected, to: draft, rework: true }
"""

TOPIC_OUTLINE_TEMPLATE = """
api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 主题大纲生成
  description: Homerail topic-outline 移植：从主题素材产出结构化大纲
contracts:
  Outline:
    type: object
    required: [sections]
    properties:
      sections:
        type: array
        minItems: 2
        items:
          type: object
          required: [heading, points]
          properties:
            heading: { type: string, minLength: 2 }
            points: { type: array, minItems: 1, items: { type: string } }
nodes:
  outline:
    kind: agent
    system: 依据实例入参 topic 与 materials 产出分层大纲（Outline 契约）
    outputs: { done: { contract: Outline } }
  done: { kind: terminal, outcome: success }
edges:
  - { from: outline.done, to: done }
"""

PORTED = {
    "pr_review": PR_REVIEW_TEMPLATE,
    "release_notes": RELEASE_NOTES_TEMPLATE,
    "topic_outline": TOPIC_OUTLINE_TEMPLATE,
}


class TestPortedTemplatesValidate:
    def test_all_three_compile_and_validate_clean(self):
        for name, text in PORTED.items():
            compiled = compile_workflow_yaml(text)
            errors = validate_workflow(compiled.spec)
            assert errors == [], f"{name}: {errors}"


class TestPrReviewTrajectory:
    def test_three_arbiters_join_then_approval(self, db):
        simulate = {"outputs": {
            "reviewer_a": {"verdict": "approve", "confidence": 0.9,
                           "rationale": "实现清晰，符合规范要求"},
            "reviewer_b": {"verdict": "approve", "confidence": 0.8,
                           "rationone": None, "rationale": "无新增依赖与安全面变化"},
            "reviewer_c": {"verdict": "request_changes", "confidence": 0.7,
                           "rationale": "缺少回归测试覆盖新分支逻辑"},
            "summarize": {"consensus": "approve",
                          "combined_rationale": "2/3 通过：实现与安全面无异议，测试缺口已记录"},
        }}
        # 修正 reviewer_b 的多余键（模板常量书写防御）
        simulate["outputs"]["reviewer_b"].pop("rationone", None)
        instance, result = activate(db, PR_REVIEW_TEMPLATE, simulate=simulate,
                                    goal="评审 #1024 变更")
        execute(db, result)
        # join(all)：三评审全部完成后 summarize 才执行
        summarize = latest_run(db, instance.id, "summarize")
        assert summarize is not None and summarize.status == "completed"
        assert summarize.output["consensus"] == "approve"
        gate = latest_run(db, instance.id, "human_gate")
        assert gate.status == "waiting_approval"
        # 关口批准 → 合并
        from app.task_instances import engine as ti_engine
        from app.task_instances.models import TaskApproval
        approval = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate.id).first()
        outcome = ti_engine.decide_approval(
            db, approval.id, "approved", reason=None, decided_by=None)
        db.commit()
        execute(db, outcome)
        assert get_instance(db, instance.id).status == "completed"
        types = [t for t, _ in events_of(db, instance.id)]
        assert "node.rejected" not in types  # 未触发打回
        assert types[-1] == "instance.terminal"

    def test_approval_reject_reworks_reviewer_chain(self, db):
        simulate = {"outputs": {
            "reviewer_a": {"verdict": "approve", "confidence": 0.9,
                           "rationale": "第一轮实现评审通过，等待复议"},
            "reviewer_b": {"verdict": "approve", "confidence": 0.8,
                           "rationale": "第二轮安全与依赖视角无异议"},
            "reviewer_c": {"verdict": "approve", "confidence": 0.8,
                           "rationale": "第三轮测试覆盖视角无异议"},
            "summarize": {"consensus": "approve",
                          "combined_rationale": "三方一致通过，等待人工把关"},
        }}
        instance, result = activate(db, PR_REVIEW_TEMPLATE, simulate=simulate,
                                    goal="评审 #1025 变更")
        execute(db, result)
        from app.task_instances import engine as ti_engine
        from app.task_instances.models import TaskApproval
        gate = latest_run(db, instance.id, "human_gate")
        approval = db.query(TaskApproval).filter(
            TaskApproval.node_run_id == gate.id).first()
        outcome = ti_engine.decide_approval(
            db, approval.id, "rejected", reason="边缘场景未覆盖，请补充评审",
            decided_by=None)
        db.commit()
        execute(db, outcome)
        # 打回边指向 reviewer_a：重做链启动
        reworked = latest_run(db, instance.id, "reviewer_a")
        assert reworked.attempt_no == 2
        types = [t for t, _ in events_of(db, instance.id)]
        assert "node.rejected" in types
