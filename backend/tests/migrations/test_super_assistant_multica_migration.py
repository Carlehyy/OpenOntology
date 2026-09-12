"""0093 超级助手 multica 外部集成配置表迁移。"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _table_exists(db_path: Path, table: str) -> bool:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    engine.dispose()
    return table in {row[0] for row in rows}


def _create_users_table(db_path: Path) -> None:
    """手工建 users 表并 stamp 到 0092：只跑 0093 一个迁移，验证其 DDL 语义。"""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE users ("
            " id VARCHAR NOT NULL PRIMARY KEY,"
            " username VARCHAR NOT NULL)"
        ))
    engine.dispose()


def test_upgrade_creates_multica_config_table(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "multica-upgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    _create_users_table(db_path)
    command.stamp(cfg, "0092_user_token_version")

    command.upgrade(cfg, "head")

    assert _table_exists(db_path, "super_assistant_multica_configs")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(
                text("PRAGMA table_info(super_assistant_multica_configs)")
            ).all()
        }
    engine.dispose()
    assert {
        "owner_id", "base_url", "workspace_id", "token_encrypted",
        "enabled", "last_test_status", "last_tested_at",
    } <= columns


def test_upgrade_is_idempotent_when_table_exists(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "multica-idempotent.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    _create_users_table(db_path)
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
    command.stamp(cfg, "0092_user_token_version")

    command.upgrade(cfg, "head")  # guard 应跳而不是重复建表
    assert _table_exists(db_path, "super_assistant_multica_configs")


def test_downgrade_drops_multica_config_table(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "multica-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    _create_users_table(db_path)
    command.stamp(cfg, "0092_user_token_version")

    command.upgrade(cfg, "head")
    assert _table_exists(db_path, "super_assistant_multica_configs")
    command.downgrade(cfg, "0092_user_token_version")
    assert not _table_exists(db_path, "super_assistant_multica_configs")


def test_head_is_single_and_follows_token_version(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    heads = ScriptDirectory.from_config(cfg).get_heads()
    # 0094 palace folders 在本迁移之后线性追加，head 随之演进
    assert heads == ["0107_mapping_suggestion_queue"]
