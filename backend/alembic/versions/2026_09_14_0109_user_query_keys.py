"""user query keys

用户变量查询密钥表 `user_query_keys`（PAT 式：跟用户、分类别 env/privacy、
多把并存；sha256 key_hash 唯一查表 + 可见前缀 key_prefix + expires_at 空=
永久 + 软吊销 revoked_at + last_used_at）。纯新增，无数据回填。

Revision ID: 0109_user_query_keys
Revises: 0108_super_assistant_mcp_dev
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0109_user_query_keys"
down_revision = "0108_super_assistant_mcp_dev"
branch_labels = None
depends_on = None

_TABLE = "user_query_keys"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0102/0108 同一防御口径：部分迁移测试场景只手工建被测表并 stamp
    # 到中间版本，users 表可能不存在；此时跳过 DDL。
    if "users" not in tables:
        return

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("category", sa.String(length=16), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("key_prefix", sa.String(length=32), nullable=False),
            sa.Column("key_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("key_hash", name="uq_user_query_keys_key_hash"),
        )
        op.create_index(
            "ix_user_query_keys_user_id",
            _TABLE,
            ["user_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_index("ix_user_query_keys_user_id", table_name=_TABLE)
        op.drop_table(_TABLE)
