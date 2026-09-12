"""exploration attachment summary

`bx_attachments.summary`（Text, nullable）：LLM 懒生成的附件「索引卡」摘要，
注入探索上下文时代替原文全文，治理长文档上下文爆炸。无数据回填需求
（NULL=尚未生成，首个回合懒生成；全新库经 0003 create_all 以当前模型建表，
列已存在时 upgrade 为幂等空操作）。

Revision ID: 0101_exploration_attachment_summary
Revises: 0100_super_assistant_tool_settings
Create Date: 2026-09-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0101_exploration_attachment_summary"
down_revision = "0100_super_assistant_tool_settings"
branch_labels = None
depends_on = None

_TABLE = "bx_attachments"
_COLUMN = "summary"


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if inspect(op.get_bind()).has_table(_TABLE):
        cols = _columns(_TABLE)
        with op.batch_alter_table(_TABLE) as batch:
            if _COLUMN not in cols:
                batch.add_column(sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    if inspect(op.get_bind()).has_table(_TABLE):
        cols = _columns(_TABLE)
        with op.batch_alter_table(_TABLE) as batch:
            if _COLUMN in cols:
                batch.drop_column(_COLUMN)
