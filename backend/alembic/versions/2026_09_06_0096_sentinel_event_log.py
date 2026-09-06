"""add Sentinel CEP event log

实例级变更事件日志：哨兵时间能力（changed_within/prev、模式匹配）的
唯一事实源。键级行（实例 × 属性键），携带 old→new 值，单调大整数主键，
固定 7 天保留由后台周期裁剪（见 sentinels/cep/event_store.py）。

Revision ID: 0096_sentinel_event_log
Revises: 0095_super_assistant_multica_workspace_name
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0096_sentinel_event_log"
down_revision = "0095_super_assistant_multica_workspace_name"
branch_labels = None
depends_on = None


TABLE = "sentinel_event_log"


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column(
                "id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                autoincrement=True, nullable=False),
            sa.Column("ontology_id", sa.String(), nullable=False),
            sa.Column("ontology_release_id", sa.String(), nullable=True),
            sa.Column("object_type_id", sa.String(), nullable=False),
            sa.Column("instance_id", sa.String(), nullable=False),
            sa.Column(
                "change_kind", sa.String(length=12), nullable=False,
                server_default="updated"),
            sa.Column("key", sa.String(length=255), nullable=False),
            sa.Column("old_value", sa.JSON(), nullable=True),
            sa.Column("new_value", sa.JSON(), nullable=True),
            sa.Column(
                "source", sa.String(length=24), nullable=False,
                server_default="organic"),
            sa.Column(
                "cascade_depth", sa.Integer(), nullable=False,
                server_default="0"),
            sa.Column("chain_id", sa.String(length=64), nullable=True),
            sa.Column(
                "occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["ontology_id"], ["ontology_projects.id"],
                ondelete="CASCADE"),
        )
        op.create_index(
            "ix_sentinel_event_log_timeline",
            TABLE,
            ["ontology_id", "object_type_id", "instance_id", "id"],
        )
        op.create_index(
            "ix_sentinel_event_log_key", TABLE, ["instance_id", "key", "id"])
        op.create_index(
            "ix_sentinel_event_log_occurred", TABLE, ["occurred_at", "id"])


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if inspector.has_table(TABLE):
        op.drop_table(TABLE)
