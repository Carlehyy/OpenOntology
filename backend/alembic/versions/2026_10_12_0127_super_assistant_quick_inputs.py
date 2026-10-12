"""super assistant quick inputs

输入框快捷内容表 `super_assistant_quick_inputs`（用户自管常用文本片段：
标题 + 内容详情 + 相对展示优先级 sort_order）。无数据回填需求。

Revision ID: 0127_super_assistant_quick_inputs
Revises: 0126_seed_task_templates
Create Date: 2026-10-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0127_super_assistant_quick_inputs"
down_revision = "0126_seed_task_templates"
branch_labels = None
depends_on = None

_TABLE = "super_assistant_quick_inputs"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0093/0098/0099/0100 同一防御口径：部分迁移测试场景只手工建被测表
    # 并 stamp 到中间版本，users 表可能不存在；此时跳过 DDL。
    if "users" not in tables:
        return

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(length=100), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_super_assistant_quick_inputs_owner_id",
            _TABLE,
            ["owner_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_table(_TABLE)
