"""super assistant remote agent invites

远程助手邀请自助接入：`super_assistant_remote_agents` 增加双传输模式
（mode/agent_key_hash/last_seen_at），新建一次性邀请表
`super_assistant_remote_agent_invites` 与回连任务队列表
`super_assistant_remote_agent_tasks`。存量行 mode 回填 direct、endpoint
语义不变；无数据回填需求。

Revision ID: 0100_super_assistant_remote_agent_invites
Revises: 0099_super_assistant_remote_agents
Create Date: 2026-09-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0100_super_assistant_remote_agent_invites"
down_revision = "0099_super_assistant_remote_agents"
branch_labels = None
depends_on = None

_AGENTS_TABLE = "super_assistant_remote_agents"
_INVITES_TABLE = "super_assistant_remote_agent_invites"
_TASKS_TABLE = "super_assistant_remote_agent_tasks"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0093/0098/0099 同一防御口径：部分迁移测试场景只手工建被测表并
    # stamp 到中间版本，users 表可能不存在；此时跳过 DDL。
    if "users" not in tables:
        return

    if _AGENTS_TABLE in tables:
        columns = {column["name"] for column in inspector.get_columns(_AGENTS_TABLE)}
        with op.batch_alter_table(_AGENTS_TABLE) as batch:
            if "mode" not in columns:
                batch.add_column(sa.Column("mode", sa.String(length=10), nullable=False,
                                           server_default="direct"))
            if "agent_key_hash" not in columns:
                batch.add_column(sa.Column("agent_key_hash", sa.String(length=64), nullable=True))
            if "last_seen_at" not in columns:
                batch.add_column(sa.Column("last_seen_at", sa.DateTime(), nullable=True))
            if "rap_version" not in columns:
                batch.add_column(sa.Column("rap_version", sa.Integer(), nullable=False,
                                           server_default="1"))
            if "last_turn_at" not in columns:
                batch.add_column(sa.Column("last_turn_at", sa.DateTime(), nullable=True))
        agents_indexes = {index["name"] for index in inspector.get_indexes(_AGENTS_TABLE)}
        if "ix_sa_remote_agents_key_hash" not in agents_indexes:
            op.create_index(
                "ix_sa_remote_agents_key_hash", _AGENTS_TABLE,
                ["agent_key_hash"], unique=True,
            )

    if _INVITES_TABLE not in tables:
        op.create_table(
            _INVITES_TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("token_encrypted", sa.Text(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("redeemed_agent_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash"),
        )
        op.create_index(
            "ix_sa_remote_agent_invites_owner", _INVITES_TABLE,
            ["owner_id", "created_at"], unique=False,
        )

    if _TASKS_TABLE not in tables:
        op.create_table(
            _TASKS_TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("agent_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("session_ref", sa.String(length=255), nullable=True),
            sa.Column("result_status", sa.String(length=16), nullable=True),
            sa.Column("result_content", sa.Text(), nullable=True),
            sa.Column("result_session_ref", sa.String(length=255), nullable=True),
            sa.Column("result_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("claimed_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["agent_id"], ["super_assistant_remote_agents.id"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_sa_remote_agent_tasks_queue", _TASKS_TABLE,
            ["agent_id", "status", "created_at"], unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if _TASKS_TABLE in tables:
        op.drop_table(_TASKS_TABLE)
    if _INVITES_TABLE in tables:
        op.drop_table(_INVITES_TABLE)
    if _AGENTS_TABLE in tables:
        columns = {column["name"] for column in inspector.get_columns(_AGENTS_TABLE)}
        agents_indexes = {index["name"] for index in inspector.get_indexes(_AGENTS_TABLE)}
        if "ix_sa_remote_agents_key_hash" in agents_indexes:
            op.drop_index("ix_sa_remote_agents_key_hash", table_name=_AGENTS_TABLE)
        with op.batch_alter_table(_AGENTS_TABLE) as batch:
            if "last_turn_at" in columns:
                batch.drop_column("last_turn_at")
            if "rap_version" in columns:
                batch.drop_column("rap_version")
            if "last_seen_at" in columns:
                batch.drop_column("last_seen_at")
            if "agent_key_hash" in columns:
                batch.drop_column("agent_key_hash")
            if "mode" in columns:
                batch.drop_column("mode")
