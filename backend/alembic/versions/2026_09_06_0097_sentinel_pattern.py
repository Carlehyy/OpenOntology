"""add Sentinel CEP pattern sentinels

模式哨兵（trigger_mode='on_pattern'）：sentinels.pattern 定义列 +
sentinel_pattern_state 在途状态机表 + sentinel_pattern_cursor 每哨兵
事件水位表。downgrade 先把 on_pattern 行复位为 on_enter（老代码的枚举
校验会在下一次快照校验时拒绝该值），再删列删表——沿用 0052 的
"先改值再收结构"次序。

Revision ID: 0097_sentinel_pattern
Revises: 0096_sentinel_event_log
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0097_sentinel_pattern"
down_revision = "0096_sentinel_event_log"
branch_labels = None
depends_on = None


SENTINELS_TABLE = "sentinels"
STATE_TABLE = "sentinel_pattern_state"
CURSOR_TABLE = "sentinel_pattern_cursor"


def upgrade() -> None:
    inspector = inspect(op.get_bind())

    sentinels_columns = {
        column["name"] for column in inspector.get_columns(SENTINELS_TABLE)
    }
    if "pattern" not in sentinels_columns:
        with op.batch_alter_table(SENTINELS_TABLE) as batch:
            batch.add_column(sa.Column("pattern", sa.JSON(), nullable=True))

    if not inspector.has_table(STATE_TABLE):
        op.create_table(
            STATE_TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("ontology_id", sa.String(), nullable=False),
            sa.Column("sentinel_id", sa.String(), nullable=False),
            sa.Column("ontology_release_id", sa.String(), nullable=True),
            sa.Column(
                "definition_revision", sa.Integer(), nullable=False,
                server_default="1"),
            sa.Column(
                "enable_generation", sa.Integer(), nullable=False,
                server_default="1"),
            sa.Column("correlation_key", sa.String(), nullable=False),
            sa.Column(
                "stage_index", sa.Integer(), nullable=False,
                server_default="0"),
            sa.Column(
                "started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "stage_entered_at", sa.DateTime(timezone=True),
                nullable=False),
            sa.Column(
                "deadline", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "snapshots", sa.JSON(), nullable=False),
            sa.Column(
                "status", sa.String(length=16), nullable=False,
                server_default="active"),
            sa.Column(
                "completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_sentinel_pattern_state_active",
            STATE_TABLE, ["sentinel_id", "status", "deadline"])
        op.create_index(
            "ix_sentinel_pattern_state_ontology_id",
            STATE_TABLE, ["ontology_id"])
        op.create_index(
            "ix_sentinel_pattern_state_sentinel_id",
            STATE_TABLE, ["sentinel_id"])

    if not inspector.has_table(CURSOR_TABLE):
        op.create_table(
            CURSOR_TABLE,
            sa.Column("sentinel_id", sa.String(), nullable=False),
            sa.Column("ontology_id", sa.String(), nullable=False),
            sa.Column(
                "event_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=False, server_default="0"),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("sentinel_id"),
        )
        op.create_index(
            "ix_sentinel_pattern_cursor_ontology_id",
            CURSOR_TABLE, ["ontology_id"])


def downgrade() -> None:
    inspector = inspect(op.get_bind())

    # 先复位数据：老代码枚举校验不接受 on_pattern，残留值会让后续快照
    # 校验直接失败（0052 的"先改值再收结构"次序）。
    sentinels_columns = {
        column["name"] for column in inspector.get_columns(SENTINELS_TABLE)
    }
    if "trigger_mode" in sentinels_columns and "pattern" in sentinels_columns:
        op.execute(
            "UPDATE sentinels SET trigger_mode = 'on_enter', "
            "pattern = NULL WHERE trigger_mode = 'on_pattern'")

    if inspector.has_table(STATE_TABLE):
        op.drop_table(STATE_TABLE)
    if inspector.has_table(CURSOR_TABLE):
        op.drop_table(CURSOR_TABLE)

    if "pattern" in sentinels_columns:
        with op.batch_alter_table(SENTINELS_TABLE) as batch:
            batch.drop_column("pattern")
