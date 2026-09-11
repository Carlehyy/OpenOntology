"""0104 远程助手邀请/回连任务表迁移。"""
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


def _table_exists(db_path: Path, table: str) -> bool:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        found = connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:name"
        ), {"name": table}).first()
    engine.dispose()
    return found is not None


def _columns(db_path: Path, table: str) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        rows = connection.execute(text(f"PRAGMA table_info({table})")).fetchall()
    engine.dispose()
    return {row[1] for row in rows}


def test_upgrade_creates_invites_and_tasks_tables(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "ra-invites.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_alembic_config(backend, db_path), "head")

    assert _table_exists(db_path, "super_assistant_remote_agent_invites")
    assert _table_exists(db_path, "super_assistant_remote_agent_tasks")
    # 存量表补列：mode/agent_key_hash/last_seen_at
    agents_columns = _columns(db_path, "super_assistant_remote_agents")
    assert {"mode", "agent_key_hash", "last_seen_at", "rap_version", "last_turn_at"} <= agents_columns

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO super_assistant_remote_agent_invites"
            " (id, owner_id, token_hash, token_encrypted, expires_at, created_at)"
            " VALUES ('i1', 'u1', 'hash-1', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
    import pytest
    with pytest.raises(Exception, match="UNIQUE"):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO super_assistant_remote_agent_invites"
                " (id, owner_id, token_hash, token_encrypted, expires_at, created_at)"
                " VALUES ('i2', 'u1', 'hash-1', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
    engine.dispose()


def test_downgrade_drops_new_tables_and_columns(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "ra-invites-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    command.upgrade(cfg, "head")
    assert _table_exists(db_path, "super_assistant_remote_agent_invites")

    command.downgrade(cfg, "0103_ontology_readpath_perf_indexes")
    assert not _table_exists(db_path, "super_assistant_remote_agent_invites")
    assert not _table_exists(db_path, "super_assistant_remote_agent_tasks")
    assert not ({"mode", "agent_key_hash", "last_seen_at", "rap_version", "last_turn_at"} & _columns(
        db_path, "super_assistant_remote_agents"))


def test_head_is_single(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0105_palace_ontology_documents"]
