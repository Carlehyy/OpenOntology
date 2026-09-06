"""super assistant delegations

超级助手委派映射表 `super_assistant_delegations`：超级会话 → 平台助手
子会话的追加式历史（resume 取最近一条；session=new 轮转新线）。对
status='running' 建部分唯一索引防并发残留竞态。无数据回填需求。

Revision ID: 0096_super_assistant_delegations
Revises: 0095_super_assistant_multica_workspace_name
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0098_super_assistant_delegations"
down_revision = "0097_sentinel_pattern"
branch_labels = None
depends_on = None

_TABLE = "super_assistant_delegations"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0093 同一防御口径：部分迁移测试场景只手工建被测表并 stamp 到中间
    # 版本，users 表可能不存在；此时跳过 DDL，真实库与全量 upgrade 正常执行。
    if "users" not in tables:
        return

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("super_conversation_id", sa.String(), nullable=False),
            sa.Column("assistant_key", sa.String(length=50), nullable=False),
            sa.Column("conversation_ref", sa.String(length=1000), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("last_turn_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["super_conversation_id"],
                ["super_assistant_conversations.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_sa_delegations_owner", _TABLE, ["owner_id"], unique=False,
        )
        op.create_index(
            "ix_sa_delegations_conv_key_last",
            _TABLE,
            ["super_conversation_id", "assistant_key", "last_turn_at"],
            unique=False,
        )
        op.create_index(
            "uq_sa_delegation_running",
            _TABLE,
            ["super_conversation_id", "assistant_key"],
            unique=True,
            sqlite_where=sa.text("status = 'running'"),
            postgresql_where=sa.text("status = 'running'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_table(_TABLE)
