"""persist last plugin-runner event for publish replay

Revision ID: 0117_plugin_runner_event_replay
Revises: 0116_plugin_runner_journal
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0117_plugin_runner_event_replay"
down_revision = "0116_plugin_runner_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if "super_assistant_plugin_runner_journal" not in tables:
        return
    columns = {column["name"] for column in sa_inspect(bind).get_columns("super_assistant_plugin_runner_journal")}
    if "last_event" not in columns:
        op.add_column(
            "super_assistant_plugin_runner_journal",
            sa.Column("last_event", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )
        if bind.dialect.name != "sqlite":
            op.alter_column("super_assistant_plugin_runner_journal", "last_event", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    if "super_assistant_plugin_runner_journal" in set(sa_inspect(bind).get_table_names()):
        columns = {column["name"] for column in sa_inspect(bind).get_columns("super_assistant_plugin_runner_journal")}
        if "last_event" in columns:
            op.drop_column("super_assistant_plugin_runner_journal", "last_event")
