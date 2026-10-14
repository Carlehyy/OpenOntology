"""preset template metadata (is_preset + real descriptions + revision cleanup)

1) task_templates 增加 is_preset 布尔列（官方预置模板标记，
   模板列表「预置」徽章依据；默认 false）。
2) 三个种子预置模板（0126 种入）的 description 被 0126 统一写成了
   内部迁移术语「预置模板（Homerail 移植）」——外部用户无从理解，
   且未描述流程用途。此处改写为真实用途描述（取自种子 YAML
   metadata.description，去掉内部代号括注），并标记 is_preset=true。
   匹配守卫：name 相等 AND created_by IS NULL（种子行无创建者）——
   用户自建的同名模板（必有 created_by）不受影响。
3) 同步清洗对应 revision 的 spec_yaml：去掉 metadata.description 的
   内部代号括注，并回填 spec_compiled 与 canonical_hash。否则用户在
   编排页打开预置模板原样保存时，update_template 会用存量 YAML 的
   旧描述覆盖列值（0128 第 2 步白改），且 spec_compiled=None 导致
   模板卡「N 节点」徽章对预置模板永远不显示。

Revision ID: 0128_preset_template_metadata
Revises: 0127_super_assistant_quick_inputs
Create Date: 2026-10-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0128_preset_template_metadata"
down_revision = "0127_super_assistant_quick_inputs"
branch_labels = None
depends_on = None

_TABLE = "task_templates"
_REVISIONS = "task_template_revisions"

# 预置模板官方描述（与种子 YAML metadata.description 同源，去内部代号）
_PRESET_DESCRIPTIONS = {
    "变更评审三仲裁": "三个 Agent 评审仲裁 + 人工把关合并",
    "发布说明生成": "从变更清单产出用户视角的发布说明",
    "主题大纲生成": "从主题素材产出结构化大纲",
}

# revision.spec_yaml 内部代号后缀 → 清洗为官方描述
_YAML_DESCRIPTION_SUFFIXES = {
    "（Homerail pr-review 移植）",
    "（Homerail release-notes 移植）",
    "（Homerail topic-outline 移植）",
}


def _clean_yaml_description(spec_yaml: str) -> str | None:
    """去掉 metadata.description 的内部代号括注；无匹配返回 None。"""
    for suffix in _YAML_DESCRIPTION_SUFFIXES:
        if suffix in spec_yaml:
            # 种子 YAML 的描述行形如 “  description: 三个 Agent 评审仲裁 + 人工把关合并（Homerail pr-review 移植）”
            return spec_yaml.replace(suffix, "").rstrip()
    return None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "is_preset" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("is_preset", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
        )

    templates = sa.table(
        _TABLE,
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("is_preset", sa.Boolean),
        sa.column("latest_revision_id", sa.String),
        sa.column("created_by", sa.String),
        sa.column("deleted_at", sa.DateTime),
    )
    revisions = sa.table(
        _REVISIONS,
        sa.column("id", sa.String),
        sa.column("spec_yaml", sa.Text),
        sa.column("spec_compiled", sa.JSON),
        sa.column("canonical_hash", sa.String(64)),
    )

    for name, description in _PRESET_DESCRIPTIONS.items():
        rows = bind.execute(
            sa.select(templates.c.id, templates.c.latest_revision_id).where(
                templates.c.name == name,
                templates.c.deleted_at.is_(None),
                templates.c.created_by.is_(None),
            )).fetchall()
        for template_id, latest_revision_id in rows:
            bind.execute(
                templates.update().where(templates.c.id == template_id)
                .values(description=description, is_preset=True))
            if not latest_revision_id:
                continue
            revision = bind.execute(
                sa.select(revisions.c.spec_yaml).where(
                    revisions.c.id == latest_revision_id)).first()
            if revision is None or not revision.spec_yaml:
                continue
            cleaned = _clean_yaml_description(revision.spec_yaml)
            if cleaned is None:
                continue
            values = {"spec_yaml": cleaned}
            try:
                from app.task_instances.spec import compile_workflow_yaml
                compiled = compile_workflow_yaml(cleaned)
                values["spec_compiled"] = compiled.canonical
                values["canonical_hash"] = compiled.canonical_hash
            except Exception:  # noqa: BLE001 — 编译失败保留原文，读取侧惰性补算兜底
                pass
            bind.execute(
                revisions.update().where(revisions.c.id == latest_revision_id)
                .values(**values))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "is_preset" in columns:
        op.drop_column(_TABLE, "is_preset")
    # 描述与 revision 清洗不回滚：改写为官方文案后回填内部术语没有意义
