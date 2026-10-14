"""notifications — 消息通知域（消息主体 / 每用户处置状态 / 附件）

新增 `notification_messages`、`notification_message_states`、
`notification_attachments`。纯增量，无数据回填。

Revision ID: 0121_notifications
Revises: 0120_user_report_token_hash
Create Date: 2026-10-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0121_notifications"
down_revision = "0120_user_report_token_hash"
branch_labels = None
depends_on = None

_MESSAGES = "notification_messages"
_STATES = "notification_message_states"
_ATTACHMENTS = "notification_attachments"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        return

    if _MESSAGES not in tables:
        op.create_table(
            _MESSAGES,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("event_id", sa.String(length=255), nullable=True),
            sa.Column("source_system", sa.String(length=80), nullable=False),
            sa.Column("source_type", sa.String(length=16), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("body_md", sa.Text(), nullable=False),
            sa.Column("priority", sa.String(length=20), nullable=False),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "priority IN ('urgent','high','normal','low')",
                name="ck_notification_messages_priority",
            ),
            sa.CheckConstraint(
                "source_type IN ('manual','internal','ingest')",
                name="ck_notification_messages_source_type",
            ),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            # 幂等键按来源系统作用域化：不同外部系统各自的 incident-1 互不冲突
            sa.UniqueConstraint(
                "source_system", "event_id", name="uq_notification_messages_source_event"
            ),
        )
        op.create_index("ix_notification_messages_created", _MESSAGES, ["created_at"])
        op.create_index(
            "ix_notification_messages_source", _MESSAGES, ["source_system", "source_type"]
        )

    tables = set(sa_inspect(bind).get_table_names())

    if _STATES not in tables and _MESSAGES in tables:
        op.create_table(
            _STATES,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("message_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("is_read", sa.Boolean(), nullable=False),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.Column("is_starred", sa.Boolean(), nullable=False),
            sa.Column("starred_at", sa.DateTime(), nullable=True),
            sa.Column("is_archived", sa.Boolean(), nullable=False),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["message_id"], [f"{_MESSAGES}.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "message_id", "user_id", name="uq_notification_state_message_user"
            ),
        )
        op.create_index("ix_notification_states_user", _STATES, ["user_id"])
        op.create_index(
            "ix_notification_message_states_message_id", _STATES, ["message_id"]
        )

    tables = set(sa_inspect(bind).get_table_names())

    if _ATTACHMENTS not in tables and _MESSAGES in tables:
        op.create_table(
            _ATTACHMENTS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("message_id", sa.String(length=36), nullable=False),
            sa.Column("filename", sa.String(length=500), nullable=False),
            sa.Column("file_path", sa.String(length=1000), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("mime_type", sa.String(length=200), nullable=True),
            sa.Column("sha256", sa.String(length=64), nullable=True),
            sa.Column("uploaded_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["message_id"], [f"{_MESSAGES}.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notification_attachments_message", _ATTACHMENTS, ["message_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _ATTACHMENTS in tables:
        op.drop_table(_ATTACHMENTS)
    if _STATES in set(sa_inspect(bind).get_table_names()):
        op.drop_table(_STATES)
    if _MESSAGES in set(sa_inspect(bind).get_table_names()):
        op.drop_table(_MESSAGES)
