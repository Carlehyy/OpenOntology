"""super assistant hindsight memory

Cross-session persistent memory for the super assistant (Hindsight-backed):

- ``super_assistant_memory_sources`` stores independent source snapshots
  (role/content/occurred_at/explicit/correction_of, no FK to conversations or
  messages) doubling as the reliable sync task row (status/lease/backoff/
  operation_id/document_id) with a unique (owner_id, source_message_id,
  version) key;
- ``super_assistant_memory_settings`` stores the per-user enable flag and the
  memory model config choice.

Revision ID: 0144_hindsight_memory
Revises: 0143_operations_menu_keys
Create Date: 2026-11-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0144_hindsight_memory"
down_revision = "0143_operations_menu_keys"
branch_labels = None
depends_on = None

_SOURCES_TABLE = "super_assistant_memory_sources"
_SETTINGS_TABLE = "super_assistant_memory_settings"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _SOURCES_TABLE not in tables:
        op.create_table(
            _SOURCES_TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("source_message_id", sa.String(length=64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("context", sa.Text(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("explicit", sa.Boolean(), nullable=False),
            sa.Column("correction_of", sa.String(), nullable=True),
            sa.Column("conversation_id", sa.String(), nullable=True),
            sa.Column("message_id", sa.String(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("lease_token", sa.String(length=64), nullable=True),
            sa.Column("operation_id", sa.String(length=64), nullable=True),
            sa.Column("operation_explicit", sa.Boolean(), nullable=True),
            sa.Column("document_id", sa.String(length=200), nullable=True),
            sa.Column("dispatch_id", sa.String(length=64), nullable=False),
            sa.Column("last_synced_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["owner_id"], ["users.id"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "owner_id", "source_message_id", "version",
                name="uq_sa_memory_sources_owner_msg_version",
            ),
        )
        op.create_index(
            "ix_sa_memory_sources_owner_status", _SOURCES_TABLE,
            ["owner_id", "status"],
        )
        op.create_index(
            "ix_sa_memory_sources_owner_created", _SOURCES_TABLE,
            ["owner_id", "created_at"],
        )
        op.create_index(
            "ix_sa_memory_sources_status_lease", _SOURCES_TABLE,
            ["status", "lease_expires_at"],
        )
        op.create_index(
            "ix_sa_memory_sources_status_next_attempt", _SOURCES_TABLE,
            ["status", "next_attempt_at"],
        )

    if _SETTINGS_TABLE not in tables:
        op.create_table(
            _SETTINGS_TABLE,
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("model_config_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["owner_id"], ["users.id"], ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["model_config_id"], ["model_configs.id"], ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("owner_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _SETTINGS_TABLE in tables:
        op.drop_table(_SETTINGS_TABLE)
    if _SOURCES_TABLE in tables:
        op.drop_table(_SOURCES_TABLE)
