"""Migration and RBAC coverage for the 智能运营 (operations) menu keys."""

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.auth.permissions import normalize_menu_keys


_OPERATIONS_MENU_KEYS = {
    "operations",
    "operations.dashboard",
    "operations.reports",
}


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _menu_keys_by_role(db_path: Path) -> dict[str, list[str]]:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT role,menu_keys FROM role_menu_permissions ORDER BY role"
        )).mappings().all()
    engine.dispose()
    return {row["role"]: json.loads(row["menu_keys"]) for row in rows}


def test_upgrade_backfills_operations_menu_keys_for_default_full_roles(
    tmp_path, monkeypatch,
):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "operations-migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "0142_agent_run_ledger")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        # editor 已持有分组 key：回填只补缺失项，不重复写入
        connection.execute(text(
            "UPDATE role_menu_permissions SET menu_keys = :menu_keys "
            "WHERE role = 'editor'"
        ), {"menu_keys": json.dumps(["overview", "operations"])})
        connection.execute(text(
            "UPDATE role_menu_permissions SET menu_keys = :menu_keys "
            "WHERE role = 'viewer'"
        ), {"menu_keys": json.dumps(["overview"])})
    engine.dispose()

    command.upgrade(cfg, "head")

    by_role = _menu_keys_by_role(db_path)
    # editor/viewer 是「非 admin 默认全量」角色：新一级菜单自动可见
    assert _OPERATIONS_MENU_KEYS <= set(by_role["editor"])
    assert by_role["editor"].count("operations") == 1
    assert _OPERATIONS_MENU_KEYS <= set(by_role["viewer"])
    # 回填的 key 必须在 RBAC 白名单内，归一化后仍然生效
    assert _OPERATIONS_MENU_KEYS <= set(normalize_menu_keys(by_role["viewer"]))
    # custom 坚持显式授予，不受回填影响
    assert _OPERATIONS_MENU_KEYS.isdisjoint(by_role.get("custom", []))


def test_downgrade_removes_operations_menu_keys(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "operations-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")
    assert _OPERATIONS_MENU_KEYS <= set(_menu_keys_by_role(db_path)["editor"])

    command.downgrade(cfg, "0142_agent_run_ledger")

    for keys in _menu_keys_by_role(db_path).values():
        assert _OPERATIONS_MENU_KEYS.isdisjoint(keys)


def test_operations_group_requires_child_after_normalize():
    # 分组 key 需至少一个子 key 才保留；只有子 key 时自动补父 key
    assert "operations" not in normalize_menu_keys(["operations"])
    assert normalize_menu_keys(["operations.reports"]) == [
        "operations",
        "operations.reports",
    ]
