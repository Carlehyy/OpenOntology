"""super assistant tool settings

内置工具启停设置表 `super_assistant_tool_settings`（每用户一行：
disabled_tools 禁用名单 JSON，缺行=全部启用）。无数据回填需求。

Revision ID: 0100_super_assistant_tool_settings
Revises: 0099_super_assistant_remote_agents
Create Date: 2026-09-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0100_super_assistant_tool_settings"
down_revision = "0099_super_assistant_remote_agents"
branch_labels = None
depends_on = None

_TABLE = "super_assistant_tool_settings"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0093/0098/0099 同一防御口径：部分迁移测试场景只手工建被测表并 stamp
    # 到中间版本，users 表可能不存在；此时跳过 DDL。
    if "users" not in tables:
        return

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("disabled_tools", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("owner_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_table(_TABLE)
