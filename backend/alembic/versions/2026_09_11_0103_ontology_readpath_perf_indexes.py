"""ontology read-path performance indexes

读路径热点接口的复合索引（仅性能优化，无行为/契约变化）：
- ontology_versions (ontology_id, created_at)：版本树按本体全量读取、
  版本列表分页排序；行内多个 JSON 快照列使堆元组很宽，顺序扫描成本
  随版本历史线性增长。
- sentinel_firings (ontology_id, ontology_release_id, created_at)：总览/
  运行汇总按发布血缘 + 时间窗聚合统计；无复合索引时按发布过滤需要对
  全部评估行回表。

Revision ID: 0103_ontology_readpath_perf_indexes
Revises: 0102_super_assistant_palace_sync_token
Create Date: 2026-09-11
"""

from alembic import op
from sqlalchemy import inspect as sa_inspect


revision = "0103_ontology_readpath_perf_indexes"
down_revision = "0102_super_assistant_palace_sync_token"
branch_labels = None
depends_on = None

_INDEXES = [
    (
        "ix_ontology_versions_ontology_created",
        "ontology_versions",
        ["ontology_id", "created_at"],
    ),
    (
        "ix_sentinel_firings_release_time",
        "sentinel_firings",
        ["ontology_id", "ontology_release_id", "created_at"],
    ),
]


def _existing_columns(table: str, tables: set[str]) -> set[str]:
    if table not in tables:
        return set()
    inspector = sa_inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    # 与 0030/0031 同一防御口径：部分迁移测试场景只手工建被测表并 stamp
    # 到中间版本；表或列可能不存在，此时跳过 DDL。索引已存在（如模型
    # create_all 先行建过）时跳过；但同名索引列组合不一致说明环境有异常
    # 索引，硬报错拒绝静默失去该性能索引。
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    for name, table, columns in _INDEXES:
        if not set(columns) <= _existing_columns(table, tables):
            continue
        existing = {
            index["name"]: list(index.get("column_names") or [])
            for index in inspector.get_indexes(table)
        }
        if name in existing:
            if existing[name] != columns:
                raise RuntimeError(
                    f"index {name} already exists on {table} with columns "
                    f"{existing[name]} instead of {columns}; refusing to "
                    "silently skip the performance index — drop or rename "
                    "the conflicting index first"
                )
            continue
        op.create_index(name, table, columns)


def downgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    for name, table, _columns in _INDEXES:
        inspector = sa_inspect(op.get_bind())
        if table not in tables:
            continue
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name not in existing:
            continue
        op.drop_index(name, table_name=table)
