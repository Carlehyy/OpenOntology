"""notifications — 渠道转发（notification_channels / notification_deliveries）

Revision ID: 0123_notification_channels
Revises: 0122_notification_ingest_keys
Create Date: 2026-10-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0123_notification_channels"
down_revision = "0122_notification_ingest_keys"
branch_labels = None
depends_on = None

_CHANNELS = "notification_channels"
_DELIVERIES = "notification_deliveries"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if "users" not in tables:
        return

    if _CHANNELS not in tables:
        op.create_table(
            _CHANNELS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("apprise_url_encrypted", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("note", sa.String(length=500), nullable=True),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("last_status", sa.String(length=20), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False),
            sa.Column("last_sent_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "last_status IN ('sent','failed')",
                name="ck_notification_channels_last_status",
            ),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_notification_channels_name"),
        )
        op.create_index("ix_notification_channels_enabled", _CHANNELS, ["enabled"])

    tables = set(sa_inspect(bind).get_table_names())

    if _DELIVERIES not in tables and _CHANNELS in tables and "notification_messages" in tables:
        op.create_table(
            _DELIVERIES,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("message_id", sa.String(length=36), nullable=False),
            sa.Column("channel_id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending','sent','failed','skipped')",
                name="ck_notification_deliveries_status",
            ),
            sa.ForeignKeyConstraint(
                ["message_id"], ["notification_messages.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["channel_id"], [f"{_CHANNELS}.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "message_id", "channel_id", name="uq_notification_delivery_message_channel"
            ),
        )
        op.create_index("ix_notification_deliveries_pending", _DELIVERIES, ["status", "created_at"])
        op.create_index("ix_notification_deliveries_channel", _DELIVERIES, ["channel_id"])


def downgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    if _DELIVERIES in tables:
        op.drop_table(_DELIVERIES)
    if _CHANNELS in set(sa_inspect(op.get_bind()).get_table_names()):
        op.drop_table(_CHANNELS)
