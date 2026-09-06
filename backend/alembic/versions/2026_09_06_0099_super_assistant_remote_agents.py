"""super assistant remote agents

远程助手声明式注册表 `super_assistant_remote_agents`（每用户多行：
key 命名空间 remote.*、端点、加密 token、启用开关、超时）。配置经
assistant_hub 动态 provider 进入委派目录，新增/停用远程助手对委派
引擎零改动。无数据回填需求。

Revision ID: 0099_super_assistant_remote_agents
Revises: 0098_super_assistant_delegations
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0099_super_assistant_remote_agents"
down_revision = "0098_super_assistant_delegations"
branch_labels = None
depends_on = None

_TABLE = "super_assistant_remote_agents"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0093/0098 同一防御口径：部分迁移测试场景只手工建被测表并 stamp
    # 到中间版本，users 表可能不存在；此时跳过 DDL。
    if "users" not in tables:
        return

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("key", sa.String(length=50), nullable=False),
            sa.Column("label", sa.String(length=100), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("endpoint", sa.String(length=1000), nullable=False),
            sa.Column("token_encrypted", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("owner_id", "key", name="uq_sa_remote_agent_owner_key"),
        )
        op.create_index(
            "ix_sa_remote_agents_owner_enabled",
            _TABLE,
            ["owner_id", "enabled"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_table(_TABLE)
