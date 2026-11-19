"""WeKnora native MCP mirror columns and per-owner integration state

超级助手 WeKnora 原生 MCP 集成（固定镜像契约）：

- ``super_assistant_mcp_servers`` 加可空列 ``weknora_endpoint_id``（镜像行
  指向的 WeKnora endpoint 标识）与 ``weknora_scope``（作用域快照 JSON），
  并建 ``(owner_id, weknora_endpoint_id)`` 具名唯一约束
  ``uq_sa_mcp_owner_weknora_endpoint``：同 owner 同 endpoint 至多一条镜像行，
  endpoint_id 为 NULL 的普通 MCP 行不受约束（SQLite/PostgreSQL 均按
  NULL 互异处理，多 NULL 共存）。
- 新表 ``super_assistant_weknora_integrations``（每用户一条的集成协调状态：
  origin 来源标记 + initialized_at 持久闩 + 默认知识库/endpoint + 最近
  错误）。不存 token，无 status 枚举，无数据回填需求。

无存量数据回填：旧 MCP 行保持 weknora_endpoint_id 为 NULL（不自动收编为
镜像行），集成状态行由服务层按需创建。

全新库经迁移 0003 create_all 以当前模型建表（列与约束已含），本迁移守卫
幂等跳过；SQLite 无 ALTER TABLE ADD CONSTRAINT，具名唯一落地为同名唯一
索引（0120 先例；SQLite 具名 UNIQUE 约束物理上即唯一索引），fresh 与存量
升级两条路径收敛到同一名称与同一唯一语义。

Revision ID: 0152_weknora_mcp
Revises: 0151_autoloop_merge
Create Date: 2026-11-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "0152_weknora_mcp"
down_revision = "0151_autoloop_merge"
branch_labels = None
depends_on = None

_SERVERS = "super_assistant_mcp_servers"
_TABLE = "super_assistant_weknora_integrations"
_UNIQUE = "uq_sa_mcp_owner_weknora_endpoint"


def _unique_names(inspector, table: str) -> set[str]:
    """具名唯一对象的方言无关视图。

    SQLite 把 create_all 写入表内的具名 UNIQUE 约束反射进
    get_unique_constraints，把本迁移创建的唯一索引反射进 get_indexes；
    取并集使 fresh（0003 create_all）与存量升级两条路径落到同一判定。
    """
    return (
        {idx["name"] for idx in inspector.get_indexes(table)}
        | {uc["name"] for uc in inspector.get_unique_constraints(table)}
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 与 0093/0100/0108 同一防御口径：部分迁移测试场景只手工建被测表并
    # stamp 到中间版本，users 表可能不存在；此时跳过 DDL。
    if "users" not in tables:
        return

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("origin", sa.String(length=1000), nullable=False),
            sa.Column("initialized_at", sa.DateTime(), nullable=True),
            sa.Column("default_knowledge_base_id", sa.String(length=64), nullable=True),
            sa.Column("default_endpoint_id", sa.String(length=64), nullable=True),
            sa.Column("last_error", sa.String(length=500), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("owner_id"),
        )

    if _SERVERS not in tables:
        return

    server_columns = {c["name"] for c in inspector.get_columns(_SERVERS)}
    if "weknora_endpoint_id" not in server_columns:
        op.add_column(
            _SERVERS,
            sa.Column("weknora_endpoint_id", sa.String(length=64), nullable=True),
        )
    if "weknora_scope" not in server_columns:
        op.add_column(_SERVERS, sa.Column("weknora_scope", sa.JSON(), nullable=True))

    if _UNIQUE not in _unique_names(inspector, _SERVERS):
        if bind.dialect.name == "sqlite":
            # SQLite 没有 ALTER TABLE ADD CONSTRAINT：具名唯一落地为同名
            # 唯一索引（0120 先例）；多 NULL 共存语义与表内约束一致。
            op.create_index(
                _UNIQUE, _SERVERS, ["owner_id", "weknora_endpoint_id"],
                unique=True,
            )
        else:
            op.create_unique_constraint(
                _UNIQUE, _SERVERS, ["owner_id", "weknora_endpoint_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if _SERVERS in tables:
        index_names = {idx["name"] for idx in inspector.get_indexes(_SERVERS)}
        constraint_names = {
            uc["name"] for uc in inspector.get_unique_constraints(_SERVERS)
        }
        columns = {c["name"] for c in inspector.get_columns(_SERVERS)}
        if bind.dialect.name == "sqlite":
            # batch 重建兼容 SQLite：独立唯一索引（存量升级路径）用
            # drop_index，表内具名约束（fresh create_all 路径）用
            # drop_constraint；与两列删除同批完成，避免被索引阻断的
            # DROP COLUMN。
            with op.batch_alter_table(_SERVERS) as batch_op:
                if _UNIQUE in index_names:
                    batch_op.drop_index(_UNIQUE)
                elif _UNIQUE in constraint_names:
                    batch_op.drop_constraint(_UNIQUE, type_="unique")
                if "weknora_endpoint_id" in columns:
                    batch_op.drop_column("weknora_endpoint_id")
                if "weknora_scope" in columns:
                    batch_op.drop_column("weknora_scope")
        else:
            # PostgreSQL also reflects a UNIQUE constraint's backing index.
            # Drop the owning constraint before considering standalone indexes.
            if _UNIQUE in constraint_names:
                op.drop_constraint(_UNIQUE, _SERVERS, type_="unique")
            elif _UNIQUE in index_names:
                op.drop_index(_UNIQUE, table_name=_SERVERS)
            if "weknora_endpoint_id" in columns:
                op.drop_column(_SERVERS, "weknora_endpoint_id")
            if "weknora_scope" in columns:
                op.drop_column(_SERVERS, "weknora_scope")

    if _TABLE in tables:
        op.drop_table(_TABLE)
