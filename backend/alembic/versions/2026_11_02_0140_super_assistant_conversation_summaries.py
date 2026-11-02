"""super assistant conversation summaries

会话纪要三件套：
- `super_assistant_conversation_summaries`：每会话仅一份最新纪要
  （structured 五维卡片 + markdown 原文 + 实际使用的模型配置 + 生成时间）；
- `super_assistant_summary_settings`：用户级纪要生成模型配置（每用户一行，
  空 model_config_id 表示跟随会话当前模型）；
- `super_assistant_messages.completed_at`：助手回复落定（complete）时刻，
  作为纪要输入里的「回复时间」。无数据回填需求。

Revision ID: 0140_super_assistant_conversation_summaries
Revises: 0139_curated_candidate_provenance
Create Date: 2026-11-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0140_super_assistant_conversation_summaries"
down_revision = "0139_curated_candidate_provenance"
branch_labels = None
depends_on = None

_SUMMARY_TABLE = "super_assistant_conversation_summaries"
_SETTINGS_TABLE = "super_assistant_summary_settings"
_MESSAGES_TABLE = "super_assistant_messages"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if _MESSAGES_TABLE in tables:
        columns = {column["name"] for column in inspector.get_columns(_MESSAGES_TABLE)}
        if "completed_at" not in columns:
            op.add_column(_MESSAGES_TABLE, sa.Column("completed_at", sa.DateTime(), nullable=True))

    # 与 0127 同一防御口径：部分迁移测试场景只手工建被测表并 stamp 到中间
    # 版本，FK 依赖表可能不存在；此时跳过建表。
    if not {"users", "model_configs", "super_assistant_conversations"} <= tables:
        return

    if _SUMMARY_TABLE not in tables:
        op.create_table(
            _SUMMARY_TABLE,
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("structured", sa.JSON(), nullable=False),
            sa.Column("markdown", sa.Text(), nullable=True),
            sa.Column("model_config_id", sa.String(), nullable=True),
            sa.Column("message_count", sa.Integer(), nullable=False),
            sa.Column("generated_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["conversation_id"], ["super_assistant_conversations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("conversation_id"),
        )
    if _SETTINGS_TABLE not in tables:
        op.create_table(
            _SETTINGS_TABLE,
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("model_config_id", sa.String(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("owner_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if _SETTINGS_TABLE in tables:
        op.drop_table(_SETTINGS_TABLE)
    if _SUMMARY_TABLE in tables:
        op.drop_table(_SUMMARY_TABLE)
    if _MESSAGES_TABLE in tables:
        columns = {column["name"] for column in inspector.get_columns(_MESSAGES_TABLE)}
        if "completed_at" in columns:
            op.drop_column(_MESSAGES_TABLE, "completed_at")
