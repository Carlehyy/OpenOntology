"""preserve structured RAP artifacts for pull tasks

Revision ID: 0113_remote_agent_result_artifacts
Revises: 0112_reconcile_outbox_payload
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "0113_remote_agent_result_artifacts"
down_revision = "0112_reconcile_outbox_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if "super_assistant_remote_agent_tasks" not in tables:
        return
    columns = {c["name"] for c in sa_inspect(bind).get_columns("super_assistant_remote_agent_tasks")}
    if "result_artifacts" not in columns:
        op.add_column(
            "super_assistant_remote_agent_tasks",
            sa.Column("result_artifacts", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )
        # SQLite cannot ALTER COLUMN to remove a default.  The default is only
        # needed to backfill pre-existing rows during this expand migration;
        # leaving it in place is harmless on SQLite and keeps the migration
        # reversible in the lightweight migration test database.  PostgreSQL
        # can drop it after the backfill so future inserts use the ORM default.
        if bind.dialect.name != "sqlite":
            op.alter_column("super_assistant_remote_agent_tasks", "result_artifacts", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    if "super_assistant_remote_agent_tasks" in set(sa_inspect(bind).get_table_names()):
        columns = {c["name"] for c in sa_inspect(bind).get_columns("super_assistant_remote_agent_tasks")}
        if "result_artifacts" in columns:
            op.drop_column("super_assistant_remote_agent_tasks", "result_artifacts")
