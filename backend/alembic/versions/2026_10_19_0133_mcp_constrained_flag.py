"""YIYA-36 P2-5: MCP server 受约束 descriptor 登记位（fail-closed 默认 False）。"""
import sqlalchemy as sa

from alembic import op

revision = "0133_mcp_constrained_flag"
down_revision = "0132_join_assertions"
branch_labels = None
depends_on = None


def _table_names(bind) -> set:
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _columns(bind, table: str) -> set:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "super_assistant_mcp_servers" in _table_names(bind) and \
            "constrained" not in _columns(bind, "super_assistant_mcp_servers"):
        op.add_column(
            "super_assistant_mcp_servers",
            sa.Column("constrained", sa.Boolean(), nullable=False,
                      server_default=sa.false()))


def downgrade() -> None:
    bind = op.get_bind()
    if "super_assistant_mcp_servers" in _table_names(bind) and \
            "constrained" in _columns(bind, "super_assistant_mcp_servers"):
        op.drop_column("super_assistant_mcp_servers", "constrained")
