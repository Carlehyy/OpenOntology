"""freeze MCP manifests and persist process-plugin invocation leases

Revision ID: 0113_mcp_revisions_and_plugin_invocations
Revises: 0112_super_assistant_process_plugins
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from app.super_assistant import models as super_assistant_models

revision = "0113_mcp_revisions_and_plugin_invocations"
down_revision = "0112_super_assistant_process_plugins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if "super_assistant_mcp_servers" in tables:
        columns = {c["name"] for c in sa_inspect(bind).get_columns("super_assistant_mcp_servers")}
        if "manifest_revision" not in columns:
            # Keep migration DDL independent from ORM Column.copy(), which is
            # deprecated and can inherit table metadata unexpectedly.
            op.add_column(
                "super_assistant_mcp_servers",
                sa.Column("manifest_revision", sa.Integer(), nullable=False, server_default="1"),
            )
        if "manifest_hash" not in columns:
            op.add_column(
                "super_assistant_mcp_servers",
                sa.Column("manifest_hash", sa.String(length=128), nullable=True),
            )
    if "users" in tables:
        super_assistant_models.SuperAssistantProcessPluginInvocation.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    super_assistant_models.SuperAssistantProcessPluginInvocation.__table__.drop(bind=bind, checkfirst=True)
    columns = {c["name"] for c in sa_inspect(bind).get_columns("super_assistant_mcp_servers")} if "super_assistant_mcp_servers" in set(sa_inspect(bind).get_table_names()) else set()
    if "manifest_hash" in columns:
        op.drop_column("super_assistant_mcp_servers", "manifest_hash")
    if "manifest_revision" in columns:
        op.drop_column("super_assistant_mcp_servers", "manifest_revision")
