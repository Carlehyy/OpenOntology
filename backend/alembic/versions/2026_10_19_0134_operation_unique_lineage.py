"""YIYA-36 P3 对照项：fo_action_operations 条件唯一索引（有值时唯一）。

approval_log_id / technical_engine_key 需有值时唯一（NULL 不参与），
防止恢复路径把同一审批日志/技术执行键绑定到多个 operation。
"""
import sqlalchemy as sa

from alembic import op

revision = "0134_operation_unique_lineage"
down_revision = "0133_mcp_constrained_flag"
branch_labels = None
depends_on = None


def _index_names(bind, table: str) -> set:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def _drop_index_guarded(table: str, name: str) -> None:
    if name in _index_names(op.get_bind(), table):
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "fo_action_operations" not in tables:
        return
    existing = _index_names(bind, "fo_action_operations")
    if "uq_action_operation_approval_log" not in existing:
        op.create_index(
            "uq_action_operation_approval_log", "fo_action_operations",
            ["approval_log_id"], unique=True,
            sqlite_where=sa.text("approval_log_id IS NOT NULL"),
            postgresql_where=sa.text("approval_log_id IS NOT NULL"))
    if "uq_action_operation_technical_key" not in existing:
        op.create_index(
            "uq_action_operation_technical_key", "fo_action_operations",
            ["technical_engine_key"], unique=True,
            sqlite_where=sa.text("technical_engine_key IS NOT NULL"),
            postgresql_where=sa.text("technical_engine_key IS NOT NULL"))


def downgrade() -> None:
    _drop_index_guarded(
        "fo_action_operations", "uq_action_operation_technical_key")
    _drop_index_guarded(
        "fo_action_operations", "uq_action_operation_approval_log")
