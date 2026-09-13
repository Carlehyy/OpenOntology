"""super assistant mcp dev

自研 MCP（插件社区「开发 MCP」）：开发项目表 `super_assistant_mcp_dev_projects`
与版本冻结表 `super_assistant_mcp_dev_versions`；`super_assistant_mcp_servers`
加可空列 `dev_project_id` 关联开发项目（transport='developed' 伪传输，执行走
进程内 mcp_dev_executor）。全新库经 0003 create_all 以当前模型建表，表已存在
时 upgrade 为幂等 DDL。

Revision ID: 0108_super_assistant_mcp_dev
Revises: 0107_mapping_suggestion_queue
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0108_super_assistant_mcp_dev"
down_revision = "0107_mapping_suggestion_queue"
branch_labels = None
depends_on = None

_PROJECTS = "super_assistant_mcp_dev_projects"
_VERSIONS = "super_assistant_mcp_dev_versions"
_SERVERS = "super_assistant_mcp_servers"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0107 同一防御口径：被引用的父表不齐时（部分迁移测试场景只建被测表
    # 并 stamp 到中间版本）跳过 DDL。
    if "users" not in tables:
        return

    if _PROJECTS not in tables:
        op.create_table(
            _PROJECTS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=False),
            sa.Column("description", sa.String(length=500), nullable=False),
            sa.Column("script", sa.Text(), nullable=False),
            sa.Column("tool_samples", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("published_version_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("owner_id", "name", name="uq_sa_mcp_dev_owner_name"),
        )
        op.create_index("ix_sa_mcp_dev_projects_owner_id", _PROJECTS, ["owner_id"])
        op.create_index(
            "ix_sa_mcp_dev_owner_updated", _PROJECTS, ["owner_id", "updated_at"])

    if _VERSIONS not in tables:
        op.create_table(
            _VERSIONS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("script", sa.Text(), nullable=False),
            sa.Column("tool_manifest", sa.JSON(), nullable=False),
            sa.Column("tool_samples", sa.JSON(), nullable=False),
            sa.Column("tool_gates", sa.JSON(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id"], [f"{_PROJECTS}.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "version_no", name="uq_sa_mcp_dev_version_no"),
        )
        op.create_index("ix_sa_mcp_dev_versions_project_id", _VERSIONS, ["project_id"])

    server_columns = {
        col["name"] for col in inspector.get_columns(_SERVERS)
    } if _SERVERS in tables else set()
    if _SERVERS in tables and "dev_project_id" not in server_columns:
        op.add_column(
            _SERVERS,
            sa.Column("dev_project_id", sa.String(length=36), nullable=True))
        op.create_index(
            "ix_sa_mcp_servers_dev_project_id", _SERVERS, ["dev_project_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if _SERVERS in tables:
        index_names = {idx["name"] for idx in inspector.get_indexes(_SERVERS)}
        if "ix_sa_mcp_servers_dev_project_id" in index_names:
            op.drop_index(
                "ix_sa_mcp_servers_dev_project_id", table_name=_SERVERS)
        server_columns = {col["name"] for col in inspector.get_columns(_SERVERS)}
        if "dev_project_id" in server_columns:
            op.drop_column(_SERVERS, "dev_project_id")
    if _VERSIONS in tables:
        op.drop_table(_VERSIONS)
    if _PROJECTS in tables:
        op.drop_table(_PROJECTS)
