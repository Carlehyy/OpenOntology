"""retire super assistant memory palace file graph tables

超级助手旧「记忆宫殿文件知识图谱」退役：删除五张持久化表——

- ``super_assistant_palace_files``：用户文件库登记行；
- ``super_assistant_palace_builds``：图谱抽取执行记录；
- ``super_assistant_palace_folders``：用户级目录行；
- ``super_assistant_palace_ontology_documents``：本体发布文档共享镜像；
- ``super_assistant_palace_sync_tokens``：文件夹同步脚本长效令牌。

旧图谱由 WeKnora 知识库能力替换，文件/笔记/镜像与 Neo4j ``PalaceEntity``
数据不做迁移。历史 revision 全部保留，便于审计与旧库升级路径复现。

downgrade 仅按原结构回建**空表**（含索引/唯一约束），不会恢复任何已删除
的数据；旧文件行、抽取记录、目录、镜像状态与同步令牌均不可恢复。

Revision ID: 0141_retire_super_assistant_palace
Revises: 0140_super_assistant_conversation_summaries
Create Date: 2026-11-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0141_retire_super_assistant_palace"
down_revision = "0140_super_assistant_conversation_summaries"
branch_labels = None
depends_on = None

_FILES = "super_assistant_palace_files"
_BUILDS = "super_assistant_palace_builds"
_FOLDERS = "super_assistant_palace_folders"
_ONTOLOGY_DOCUMENTS = "super_assistant_palace_ontology_documents"
_SYNC_TOKENS = "super_assistant_palace_sync_tokens"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    # 子表先删，避免外键依赖导致 DROP 失败。
    for table in (
        _BUILDS,
        _FOLDERS,
        _ONTOLOGY_DOCUMENTS,
        _SYNC_TOKENS,
        _FILES,
    ):
        if table in tables:
            op.drop_table(table)


def downgrade() -> None:
    """回建空表；被删除的旧图谱数据不可恢复。"""
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if "users" not in tables:
        # 与 0089/0094/0102/0105 同一防御口径：部分迁移测试场景只手工建
        # 被测表并 stamp 到中间版本，users 表可能不存在。
        return

    if _FILES not in tables:
        op.create_table(
            _FILES,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("folder_path", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("artifact_id", sa.String(length=64), nullable=False),
            sa.Column("mime_type", sa.String(length=120), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("extracted_chars", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("entity_count", sa.Integer(), nullable=False),
            sa.Column("relation_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_super_assistant_palace_files_owner_id", _FILES, ["owner_id"])
        op.create_index("ix_sa_palace_files_owner_updated", _FILES, ["owner_id", "updated_at"])

    if _BUILDS not in tables:
        op.create_table(
            _BUILDS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("file_id", sa.String(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("chunk_count", sa.Integer(), nullable=False),
            sa.Column("entity_count", sa.Integer(), nullable=False),
            sa.Column("relation_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["file_id"], [_FILES + ".id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_super_assistant_palace_builds_owner_id", _BUILDS, ["owner_id"])
        op.create_index("ix_super_assistant_palace_builds_file_id", _BUILDS, ["file_id"])
        op.create_index("ix_sa_palace_builds_file_created", _BUILDS, ["file_id", "created_at"])
        op.create_index("ix_sa_palace_builds_owner_created", _BUILDS, ["owner_id", "created_at"])

    if _FOLDERS not in tables:
        op.create_table(
            _FOLDERS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("path", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("owner_id", "path", name="uq_sa_palace_folders_owner_path"),
        )
        op.create_index("ix_super_assistant_palace_folders_owner_id", _FOLDERS, ["owner_id"])

    if _ONTOLOGY_DOCUMENTS not in tables:
        op.create_table(
            _ONTOLOGY_DOCUMENTS,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("ontology_id", sa.String(length=64), nullable=False),
            sa.Column("version_id", sa.String(length=64), nullable=False),
            sa.Column("version_number", sa.String(length=50), nullable=False),
            sa.Column("ontology_name", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("artifact_id", sa.String(length=64), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("extracted_chars", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("entity_count", sa.Integer(), nullable=False),
            sa.Column("relation_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ontology_id"),
        )
        op.create_index("ix_sa_palace_ontdocs_updated", _ONTOLOGY_DOCUMENTS, ["updated_at"])

    if _SYNC_TOKENS not in tables:
        op.create_table(
            _SYNC_TOKENS,
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("token_encrypted", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("owner_id"),
            sa.UniqueConstraint("token_hash", name="uq_sa_palace_sync_token_hash"),
        )
        op.create_index("ix_sa_palace_sync_tokens_token_hash", _SYNC_TOKENS, ["token_hash"])
