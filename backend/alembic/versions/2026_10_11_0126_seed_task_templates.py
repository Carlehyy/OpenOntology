"""seed task instance preset templates

种入三个 Homerail 移植预置模板（pr-review 三仲裁/release-notes/topic-outline），
用户打开任务实例弹窗即见可用模板，不再面对空列表。

Revision ID: 0126_seed_task_templates
Revises: 0125_task_instances
Create Date: 2026-10-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
import json
import hashlib


revision = "0126_seed_task_templates"
down_revision = "0125_task_instances"
branch_labels = None
depends_on = None

# ─── 预置模板 YAML（与 tests/task_instances/test_ported_templates.py 同源）───

PR_REVIEW = """api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 变更评审三仲裁
  description: 三个 Agent 评审仲裁 + 人工把关合并（Homerail pr-review 移植）
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

RELEASE_NOTES = """api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 发布说明生成
  description: 从变更清单产出用户视角的发布说明（Homerail release-notes 移植）
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

TOPIC_OUTLINE = """api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 主题大纲生成
  description: 从主题素材产出结构化大纲（Homerail topic-outline 移植）
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

SEEDS = [
    ("变更评审三仲裁", PR_REVIEW),
    ("发布说明生成", RELEASE_NOTES),
    ("主题大纲生成", TOPIC_OUTLINE),
]

_TEMPLATES = "task_templates"
_REVISIONS = "task_template_revisions"


def _canonical_hash(spec_yaml: str) -> str:
    """与前端 spec.py canonical_hash 同构（简化版：sha256 of sorted YAML）。"""
    import yaml as _yaml
    parsed = _yaml.safe_load(spec_yaml)
    payload = json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if _TEMPLATES not in tables or _REVISIONS not in tables:
        return

    templates = sa.table(_TEMPLATES, sa.column("id", sa.String),
                         sa.column("name", sa.String), sa.column("description", sa.Text),
                         sa.column("latest_revision_id", sa.String),
                         sa.column("created_by", sa.String),
                         sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime))
    revisions = sa.table(_REVISIONS, sa.column("id", sa.String),
                         sa.column("template_id", sa.String), sa.column("revision_no", sa.Integer),
                         sa.column("spec_yaml", sa.Text), sa.column("spec_compiled", sa.JSON),
                         sa.column("canonical_hash", sa.String(64)), sa.column("note", sa.String(500)),
                         sa.column("created_by", sa.String), sa.column("created_at", sa.DateTime))

    from datetime import datetime, timezone
    import uuid
    now = datetime.now(timezone.utc)

    for name, spec_yaml in SEEDS:
        # 幂等：同名模板已存在则跳过
        existing = bind.execute(
            sa.select(templates.c.id).where(templates.c.name == name)
        ).first()
        if existing:
            continue
        template_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        chash = _canonical_hash(spec_yaml)
        bind.execute(templates.insert().values(
            id=template_id, name=name, description=f"预置模板（Homerail 移植）",
            latest_revision_id=revision_id, created_by=None,
            created_at=now, updated_at=now))
        bind.execute(revisions.insert().values(
            id=revision_id, template_id=template_id, revision_no=1,
            spec_yaml=spec_yaml, spec_compiled=None, canonical_hash=chash,
            note="种子数据", created_by=None, created_at=now))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if _TEMPLATES not in tables:
        return
    templates = sa.table(_TEMPLATES, sa.column("id", sa.String),
                         sa.column("name", sa.String))
    for name, _ in SEEDS:
        bind.execute(templates.delete().where(templates.c.name == name))
