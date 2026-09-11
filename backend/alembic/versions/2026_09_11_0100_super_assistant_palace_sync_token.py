"""super assistant palace sync token

记忆宫殿文件夹同步令牌表 `super_assistant_palace_sync_tokens`（每用户一行：
sha256 查找列 + Fernet 复嵌列 + last_used_at）。纯新增，无数据回填。

Revision ID: 0100_super_assistant_palace_sync_token
Revises: 0099_super_assistant_remote_agents
Create Date: 2026-09-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0100_super_assistant_palace_sync_token"
down_revision = "0099_super_assistant_remote_agents"
branch_labels = None
depends_on = None

_TABLE = "super_assistant_palace_sync_tokens"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0094/0099 同一防御口径：部分迁移测试场景只手工建被测表并 stamp
    # 到中间版本，users 表可能不存在；此时跳过 DDL。
    if "users" not in tables:
        return

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("token_encrypted", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("owner_id"),
            sa.UniqueConstraint("token_hash", name="uq_sa_palace_sync_token_hash"),
        )
        op.create_index(
            "ix_sa_palace_sync_tokens_token_hash",
            _TABLE,
            ["token_hash"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_table(_TABLE)
