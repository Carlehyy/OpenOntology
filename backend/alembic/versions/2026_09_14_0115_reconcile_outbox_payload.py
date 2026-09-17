"""persist kernel reconciliation observations in the execution outbox

Revision ID: 0115_reconcile_outbox_payload
Revises: 0114_mcp_revisions_and_plugin_invocations
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0115_reconcile_outbox_payload"
down_revision = "0114_mcp_revisions_and_plugin_invocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "super_assistant_execution_dispatch_outbox" not in set(sa_inspect(bind).get_table_names()):
        return
    columns = {column["name"] for column in sa_inspect(bind).get_columns("super_assistant_execution_dispatch_outbox")}
    if "payload" not in columns:
        op.add_column(
            "super_assistant_execution_dispatch_outbox",
            sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "super_assistant_execution_dispatch_outbox" not in set(sa_inspect(bind).get_table_names()):
        return
    columns = {column["name"] for column in sa_inspect(bind).get_columns("super_assistant_execution_dispatch_outbox")}
    if "payload" in columns:
        op.drop_column("super_assistant_execution_dispatch_outbox", "payload")
