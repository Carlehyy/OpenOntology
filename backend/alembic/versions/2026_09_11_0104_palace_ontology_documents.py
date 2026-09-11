"""palace ontology documents: shared mirror of published business docs

本体发布态业务文档进入记忆宫殿：新增
super_assistant_palace_ontology_documents 平台级共享镜像表（按 ontology_id
幂等 upsert，由 ontology.documents.published 事件驱动），树中呈现为只读的
「本体文档」目录；图谱贡献写入系统作用域，与用户图谱 UNION 展示。

Revision ID: 0104_palace_ontology_documents
Revises: 0103_ontology_readpath_perf_indexes
Create Date: 2026-09-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0104_palace_ontology_documents"
down_revision = "0103_ontology_readpath_perf_indexes"
branch_labels = None
depends_on = None

_TABLE = "super_assistant_palace_ontology_documents"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    # 与 0090/0094 同一防御口径：部分迁移测试场景只手工建被测表并 stamp
    # 到中间版本；此时跳过 DDL，真实库与全量 upgrade 正常执行。
    if "users" not in set(inspector.get_table_names()):
        return
    if _TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        _TABLE,
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
    op.create_index("ix_sa_palace_ontdocs_updated", _TABLE, ["updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_index("ix_sa_palace_ontdocs_updated", table_name=_TABLE)
        op.drop_table(_TABLE)
