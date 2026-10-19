"""YIYA-36 P3: fo_action_logs.operation_id 血缘列（可空，既有调用 NULL）。

确认协议的恢复路径按 operation_id 与旧幂等键双向定位引擎日志；列可空，
不回填历史，不改变既有哨兵/直连调用行为。
"""
import sqlalchemy as sa

from alembic import op

revision = "0131_action_log_operation_lineage"
down_revision = "0130_agent_confirmation_query"
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
    if "fo_action_logs" in _table_names(bind) and \
            "operation_id" not in _columns(bind, "fo_action_logs"):
        op.add_column(
            "fo_action_logs",
            sa.Column("operation_id", sa.String(), nullable=True))
        op.create_index(
            "ix_fo_action_logs_operation_id",
            "fo_action_logs", ["operation_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "fo_action_logs" in _table_names(bind) and \
            "operation_id" in _columns(bind, "fo_action_logs"):
        op.drop_index("ix_fo_action_logs_operation_id",
                      table_name="fo_action_logs")
        op.drop_column("fo_action_logs", "operation_id")
