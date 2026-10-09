"""notifications — 对外投递密钥（notification_ingest_keys）

Revision ID: 0122_notification_ingest_keys
Revises: 0121_notifications
Create Date: 2026-10-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0122_notification_ingest_keys"
down_revision = "0121_notifications"
branch_labels = None
depends_on = None

_TABLE = "notification_ingest_keys"
_MESSAGES = "notification_messages"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if "users" not in tables or _TABLE in tables:
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("allowed_source_system", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_notification_ingest_keys_hash"),
    )
    op.create_index("ix_notification_ingest_keys_key_prefix", _TABLE, ["key_prefix"])
    op.create_index("ix_notification_ingest_keys_key_hash", _TABLE, ["key_hash"])
    op.create_index("ix_notification_ingest_keys_enabled", _TABLE, ["enabled"])

    # 消息表补投递密钥归属列：附件追加按密钥鉴权（跨密钥注入防线）。
    # batch 模式（copy-and-move）：SQLite 不支持 ALTER ADD CONSTRAINT。
    messages = set(sa_inspect(bind).get_table_names())
    if _MESSAGES in messages:
        message_columns = {
            column["name"] for column in sa_inspect(bind).get_columns(_MESSAGES)
        }
        if "ingest_key_id" not in message_columns:
            with op.batch_alter_table(_MESSAGES) as batch:
                batch.add_column(sa.Column("ingest_key_id", sa.String(length=36), nullable=True))
                batch.create_foreign_key(
                    "fk_notification_messages_ingest_key",
                    _TABLE,
                    ["ingest_key_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
            op.create_index(
                "ix_notification_messages_ingest_key_id", _MESSAGES, ["ingest_key_id"]
            )


def downgrade() -> None:
    bind = op.get_bind()
    messages = set(sa_inspect(bind).get_table_names())
    if _MESSAGES in messages:
        message_columns = {
            column["name"] for column in sa_inspect(bind).get_columns(_MESSAGES)
        }
        if "ingest_key_id" in message_columns:
            op.drop_index("ix_notification_messages_ingest_key_id", table_name=_MESSAGES)
            with op.batch_alter_table(_MESSAGES) as batch:
                batch.drop_column("ingest_key_id")
    if _TABLE in set(sa_inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
