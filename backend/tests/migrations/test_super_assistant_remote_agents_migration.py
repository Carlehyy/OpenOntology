"""0099 远程助手声明式注册表迁移。"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _table_exists(db_path: Path) -> bool:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        found = connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='super_assistant_remote_agents'"
        )).first()
    engine.dispose()
    return found is not None


def test_upgrade_creates_table_with_owner_key_unique(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "remote-agents.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_alembic_config(backend, db_path), "head")

    assert _table_exists(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        # 全新库经 0003 create_all 以当前模型建表（时间戳 NOT NULL、无 server
        # 默认，与 0093-0098 的既有行为一致）；存量库走 0099 静态 DDL
        insert = (
            "INSERT INTO super_assistant_remote_agents"
            " (id, owner_id, key, label, description, endpoint, enabled, timeout_seconds,"
            "  created_at, updated_at)"
            " VALUES ('{id}', 'u1', '{key}', 'n', 'd', 'http://127.0.0.1:9/turn', 1, 30,"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(text(insert.format(id="a1", key="remote.one")))
        connection.execute(text(insert.format(id="a2", key="remote.two")))
    # 同 owner 同 key 唯一约束
    import pytest
    with pytest.raises(Exception, match="UNIQUE"):
        with engine.begin() as connection:
            connection.execute(text(insert.format(id="a3", key="remote.one")))
    engine.dispose()


def test_downgrade_drops_table(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "remote-agents-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    command.upgrade(cfg, "head")
    assert _table_exists(db_path)
    command.downgrade(cfg, "0098_super_assistant_delegations")
    assert not _table_exists(db_path)


def test_head_is_single(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0100_palace_ontology_documents"]
