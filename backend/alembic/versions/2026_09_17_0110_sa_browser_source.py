"""super assistant conversation browser source binding

超级助手会话绑定用户级浏览器来源：`super_assistant_conversations` 新增
`browser_source_id` 可空列（FK → `v2_steward_browser_sources.id`，
ON DELETE SET NULL）+ 索引。纯增量，无数据回填。

Revision ID: 0110_sa_browser_source
Revises: 0109_user_query_keys
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0110_sa_browser_source"
down_revision = "0109_user_query_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if not inspector.has_table("super_assistant_conversations"):
        return
    columns = {
        column["name"] for column in inspector.get_columns("super_assistant_conversations")
    }
    if "browser_source_id" not in columns:
        with op.batch_alter_table("super_assistant_conversations") as batch:
            batch.add_column(sa.Column("browser_source_id", sa.String(), nullable=True))
            batch.create_foreign_key(
                "fk_sa_conversation_browser_source", "v2_steward_browser_sources",
                ["browser_source_id"], ["id"], ondelete="SET NULL")
        op.create_index(
            "ix_super_assistant_conversations_browser_source_id",
            "super_assistant_conversations", ["browser_source_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if inspector.has_table("super_assistant_conversations"):
        columns = {
            column["name"] for column in inspector.get_columns("super_assistant_conversations")
        }
        if "browser_source_id" in columns:
            # FK 实际名随建表路径不同（本迁移显式命名 vs 0003 create_all 由数据库
            # 自动命名）：按 inspector 查到的真实名删除；SQLite 经 batch 重建随列消失
            fk_name = next(
                (
                    fk["name"]
                    for fk in sa_inspect(bind).get_foreign_keys(
                        "super_assistant_conversations")
                    if fk.get("referred_table") == "v2_steward_browser_sources"
                    and fk.get("constrained_columns") == ["browser_source_id"]
                ),
                None,
            )
            indexes = {
                index["name"]
                for index in sa_inspect(bind).get_indexes("super_assistant_conversations")
            }
            if "ix_super_assistant_conversations_browser_source_id" in indexes:
                op.drop_index(
                    "ix_super_assistant_conversations_browser_source_id",
                    table_name="super_assistant_conversations")
            with op.batch_alter_table("super_assistant_conversations") as batch:
                if fk_name and bind.dialect.name != "sqlite":
                    batch.drop_constraint(fk_name, type_="foreignkey")
                batch.drop_column("browser_source_id")
