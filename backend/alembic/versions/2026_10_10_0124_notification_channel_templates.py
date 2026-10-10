"""notifications — 渠道模板化 + 平台 SMTP 设置

Revision ID: 0124_notification_channel_templates
Revises: 0123_notification_channels
Create Date: 2026-10-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0124_notification_channel_templates"
down_revision = "0123_notification_channels"
branch_labels = None
depends_on = None

_CHANNELS = "notification_channels"
_SMTP = "notification_smtp_settings"


def upgrade() -> None:
    bind = op.get_bind()

    channels_columns = {
        column["name"] for column in sa_inspect(bind).get_columns(_CHANNELS)
    }
    if "template" not in channels_columns:
        with op.batch_alter_table(_CHANNELS) as batch:
            batch.add_column(sa.Column("template", sa.String(length=32), nullable=True))
            batch.add_column(sa.Column("params_encrypted", sa.Text(), nullable=True))

    if _SMTP not in set(sa_inspect(bind).get_table_names()):
        op.create_table(
            _SMTP,
            sa.Column("id", sa.String(length=16), nullable=False),
            sa.Column("host", sa.String(length=200), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=200), nullable=False),
            sa.Column("password_encrypted", sa.Text(), nullable=False),
            sa.Column("sender", sa.String(length=200), nullable=False),
            sa.Column("use_tls", sa.Boolean(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    channels_columns = {
        column["name"] for column in sa_inspect(bind).get_columns(_CHANNELS)
    }
    if "template" in channels_columns:
        with op.batch_alter_table(_CHANNELS) as batch:
            batch.drop_column("params_encrypted")
            batch.drop_column("template")
    if _SMTP in set(sa_inspect(bind).get_table_names()):
        op.drop_table(_SMTP)
