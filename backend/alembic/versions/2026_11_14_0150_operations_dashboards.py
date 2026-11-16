"""platform operations dashboards (Grafana-backed cards)

Revision ID: 0150_operations_dashboards
Revises: 0149_integrated_replica_snapshot
Create Date: 2026-11-14

监控看板卡片表：平台只维护卡片元数据、排序、revision 与预生成的 Grafana
UID（唯一）；Grafana 原生界面由使用者自行配置数据源与面板，平台不存看板
JSON。沿用 0145 的检查式建表/索引模式处理表已存在：表缺失则创建；已存在
（模型 create_all 先行或重复升级）时核对精确形态（列/类型/可空/主键/
唯一约束/索引名），漂移即失败，不悄悄丢数据也不把漂移表当正常。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "0150_operations_dashboards"
down_revision = "0149_integrated_replica_snapshot"
branch_labels = None
depends_on = None

TABLE = "operations_dashboards"
UNIQUE_GRAFANA_UID = "uq_operations_dashboards_grafana_uid"
INDEXES = (
    ("ix_operations_dashboards_updated_at", ["updated_at"]),
    ("ix_operations_dashboards_sort_order", ["sort_order"]),
)

_COLUMNS = (
    sa.Column("id", sa.String(), nullable=False),
    sa.Column("name", sa.String(length=200), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("icon", sa.String(length=64), nullable=False),
    sa.Column("grafana_uid", sa.String(length=40), nullable=False),
    sa.Column("sort_order", sa.Integer(), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if TABLE not in inspector.get_table_names():
        op.create_table(
            TABLE,
            *_COLUMNS,
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("grafana_uid", name=UNIQUE_GRAFANA_UID),
        )
        for name, columns in INDEXES:
            op.create_index(name, TABLE, columns)
        return
    # 已存在：只接受与本迁移创建形态完全一致的表；任何漂移都显式失败。
    dialect = bind.dialect
    actual = {column["name"]: column for column in inspector.get_columns(TABLE)}
    expected = {column.name: column for column in _COLUMNS}
    if set(actual) != set(expected):
        raise RuntimeError(f"Existing {TABLE} has incompatible columns")
    for name, column in expected.items():
        found = actual[name]
        if (
            found["nullable"] != column.nullable
            or str(found["type"].compile(dialect=dialect)).upper()
            != str(column.type.compile(dialect=dialect)).upper()
        ):
            raise RuntimeError(
                f"Existing {TABLE}.{name} has incompatible type/nullability")
    if inspector.get_pk_constraint(TABLE).get("constrained_columns") != ["id"]:
        raise RuntimeError(f"Existing {TABLE} has an incompatible primary key")
    uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints(TABLE)
    }
    if ("grafana_uid",) not in uniques:
        raise RuntimeError(f"Existing {TABLE} lacks the unique grafana_uid constraint")
    indexes = {item["name"]: item for item in inspector.get_indexes(TABLE)}
    for name, columns in INDEXES:
        existing = indexes.get(name)
        if existing is None or existing.get("column_names") != columns:
            raise RuntimeError(f"Existing index {name} has incompatible shape")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if TABLE in inspector.get_table_names():
        # Schema 回退只删除看板卡片表；Grafana 卷内的看板与成对恢复由运维
        # 按 UID 处理（docs/operations/backup-restore.md），不承诺找回数据。
        op.drop_table(TABLE)
