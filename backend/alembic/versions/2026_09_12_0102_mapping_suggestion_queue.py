"""mapping suggestion queue

映射建议人工确认队列表 `v2_mapping_suggestions`：探索 Agent 的
propose_mapping 工具提交的映射提案在此落库（status=pending），确认只发生在
映射视图的队列 UI；未确认建议不进草稿快照、不回流知识库。无数据回填需求
（全新库经 0003 create_all 以当前模型建表，表已存在时 upgrade 为幂等空操作）。

Revision ID: 0102_mapping_suggestion_queue
Revises: 0101_exploration_attachment_summary
Create Date: 2026-09-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0102_mapping_suggestion_queue"
down_revision = "0101_exploration_attachment_summary"
branch_labels = None
depends_on = None

_TABLE = "v2_mapping_suggestions"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0100 同一防御口径：被引用的父表不齐时（部分迁移测试场景只建被测表
    # 并 stamp 到中间版本）跳过 DDL。
    if not {"ontology_projects", "ontology_versions", "v2_datasets"} <= tables:
        return

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("ontology_id", sa.String(), nullable=False),
            sa.Column("version_id", sa.String(), nullable=False),
            sa.Column("dataset_id", sa.String(), nullable=False),
            sa.Column("object_type_id", sa.String(), nullable=False),
            sa.Column("entity_class", sa.String(length=200), nullable=False),
            sa.Column("field_mapping", sa.JSON(), nullable=False),
            sa.Column("primary_key_column", sa.String(length=200), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("confirmed_mapping_id", sa.String(), nullable=True),
            sa.Column("status_reason", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=20), nullable=False),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["ontology_id"], ["ontology_projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["version_id"], ["ontology_versions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["dataset_id"], ["v2_datasets.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_v2_mapping_suggestions_ontology_id", _TABLE,
                        ["ontology_id"])
        op.create_index("ix_v2_mapping_suggestions_version_id", _TABLE,
                        ["version_id"])
        op.create_index("ix_v2_mapping_suggestions_status", _TABLE, ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_table(_TABLE)
