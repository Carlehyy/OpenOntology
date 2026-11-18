"""operations report article plan_json

Revision ID: 0152_operations_report_plan
Revises: 0151_autoloop_merge
Create Date: 2026-11-18

运营报告文章新增可空 plan_json（结构化计划，app/operations/plan.py 的规范化
形态）：纯增量列、无回填、无约束变化，存量 markdown 文章保持 NULL 且读写
路径不变。沿用 0102/0120 的检查式 DDL 口径：表/列缺失才动手，重复升级与
部分建表场景可安全重入。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "0152_operations_report_plan"
down_revision = "0151_autoloop_merge"
branch_labels = None
depends_on = None

_TABLE = "operations_report_articles"
_COLUMN = "plan_json"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN not in {column["name"] for column in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}:
        # batch 模式兼容 SQLite 的列删除（重建表语义）。
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
