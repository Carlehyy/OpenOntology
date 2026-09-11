"""0095 multica 配置表 workspace_name 显示名迁移。"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _columns(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        rows = connection.execute(
            text("PRAGMA table_info(super_assistant_multica_configs)")
        ).all()
    engine.dispose()
    return {row[1] for row in rows}


def _create_0093_shape_table(db_path: Path) -> None:
    """手工建 0093 形态的配置表（不含 workspace_name）并 stamp 到 0094。"""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE super_assistant_multica_configs ("
            " owner_id VARCHAR NOT NULL PRIMARY KEY,"
            " base_url VARCHAR(500) NOT NULL,"
            " workspace_id VARCHAR(100) NOT NULL,"
            " token_encrypted TEXT,"
            " enabled BOOLEAN NOT NULL,"
            " last_test_status VARCHAR(20),"
            " last_test_message VARCHAR(500),"
            " last_tested_at DATETIME,"
            " created_at DATETIME,"
            " updated_at DATETIME)"
        ))
    engine.dispose()


def test_upgrade_adds_workspace_name_with_default(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "workspace-name.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    _create_0093_shape_table(db_path)
    command.stamp(cfg, "0094_palace_folders")

    command.upgrade(cfg, "head")

    columns = _columns(db_path)
    assert "workspace_name" in columns
    # 存量行按 server_default 回填空串（前端回落显示 workspace_id）
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        connection.execute(text(
            "INSERT INTO super_assistant_multica_configs (owner_id, base_url,"
            " workspace_id, token_encrypted, enabled) VALUES"
            " ('u1', 'http://m', 'ws-1', 'x', 1)"
        ))
        row = connection.execute(text(
            "SELECT workspace_name FROM super_assistant_multica_configs WHERE owner_id = 'u1'"
        )).one()
    engine.dispose()
    assert row[0] == ""


def test_upgrade_is_idempotent_when_column_exists(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "workspace-name-idempotent.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    _create_0093_shape_table(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE super_assistant_multica_configs"
            " ADD COLUMN workspace_name VARCHAR(200) NOT NULL DEFAULT ''"
        ))
    engine.dispose()
    command.stamp(cfg, "0094_palace_folders")

    command.upgrade(cfg, "head")  # guard 应跳过而不是重复加列
    assert "workspace_name" in _columns(db_path)


def test_downgrade_drops_workspace_name(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "workspace-name-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    _create_0093_shape_table(db_path)
    command.stamp(cfg, "0094_palace_folders")

    command.upgrade(cfg, "head")
    assert "workspace_name" in _columns(db_path)
    command.downgrade(cfg, "0094_palace_folders")
    assert "workspace_name" not in _columns(db_path)


def test_head_is_single(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0100_super_assistant_tool_settings"]
