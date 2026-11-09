"""operations menu keys backfill

智能运营是全新一级菜单分组（operations），下设监控看板
（operations.dashboard）与运营报告（operations.reports），当前均为前端
待开发占位页，没有后端表。平台的非 admin 默认授权语义是「常规角色保留
既有全部产品区可见性」（见 app/auth/permissions.py），因此为已初始化的
editor/viewer 角色行补授这三个菜单 key；custom 角色坚持显式授予原则不动。

Revision ID: 0143_operations_menu_keys
Revises: 0142_agent_run_ledger
Create Date: 2026-11-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision = "0143_operations_menu_keys"
down_revision = "0142_agent_run_ledger"
branch_labels = None
depends_on = None

_OPERATIONS_MENU_KEYS = (
    "operations",
    "operations.dashboard",
    "operations.reports",
)
# 非 admin 默认全量的两个常规角色；admin 硬编码全量无需回填，
# custom 坚持管理员显式授予
_DEFAULT_FULL_ROLES = ("editor", "viewer")

_role_menu = sa.table(
    "role_menu_permissions",
    sa.column("role", sa.String),
    sa.column("menu_keys", sa.JSON),
)


def _role_menu_rows(bind):
    # 治理迁移测试等场景会从最小库形态升级到 head：表不存在时跳过
    if "role_menu_permissions" not in sa_inspect(bind).get_table_names():
        return []
    return bind.execute(
        sa.select(_role_menu.c.role, _role_menu.c.menu_keys)
    ).fetchall()


def _write_menu_keys(bind, role: str, keys: list[str]) -> None:
    bind.execute(
        _role_menu.update()
        .where(_role_menu.c.role == role)
        .values(menu_keys=keys)
    )


def upgrade() -> None:
    bind = op.get_bind()
    for role, menu_keys in _role_menu_rows(bind):
        if role not in _DEFAULT_FULL_ROLES:
            continue
        keys = list(menu_keys or [])
        missing = [key for key in _OPERATIONS_MENU_KEYS if key not in keys]
        if missing:
            _write_menu_keys(bind, role, keys + missing)


def downgrade() -> None:
    bind = op.get_bind()
    for role, menu_keys in _role_menu_rows(bind):
        keys = list(menu_keys or [])
        kept = [key for key in keys if key not in _OPERATIONS_MENU_KEYS]
        if len(kept) != len(keys):
            _write_menu_keys(bind, role, kept)
