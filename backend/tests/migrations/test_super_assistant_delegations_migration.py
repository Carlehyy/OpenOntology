"""0098 超级助手委派映射表迁移。"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _index_rows(db_path: Path) -> dict[str, tuple[int, int]]:
    """表索引清单：name → (unique, partial)。"""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        rows = connection.execute(
            text("PRAGMA index_list(super_assistant_delegations)")
        ).all()
    engine.dispose()
    return {row[1]: (row[2], row[4]) for row in rows}


def _table_exists(db_path: Path) -> bool:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        found = connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='super_assistant_delegations'"
        )).first()
    engine.dispose()
    return found is not None


def test_upgrade_creates_table_with_partial_unique_running_index(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "delegations.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_alembic_config(backend, db_path), "head")

    assert _table_exists(db_path)
    indexes = _index_rows(db_path)
    assert indexes["uq_sa_delegation_running"] == (1, 1)  # unique + partial
    assert "ix_sa_delegations_conv_key_last" in indexes

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        # 全新库经 0003 的 create_all 以当前模型建表（时间戳 NOT NULL、无
        # server 默认，与 0093-0095 的既有行为一致）；存量库走 0098 静态 DDL
        base_insert = (
            "INSERT INTO super_assistant_delegations"
            " (id, owner_id, super_conversation_id, assistant_key, status,"
            "  summary, created_at, updated_at, last_turn_at)"
            " VALUES ('{id}', 'u1', 'c1', 'ontology_agent', '{status}', '',"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        # 追加式历史：同一 (会话, 助手) 多条终态行合法
        connection.execute(text(base_insert.format(id="d1", status="answered")))
        connection.execute(text(base_insert.format(id="d2", status="answered")))
        connection.execute(text(base_insert.format(id="d3", status="running")))
    # 并发残留竞态：同一 (会话, 助手) 第二条 running 行必须被部分唯一索引拒绝
    with pytest.raises(Exception, match="UNIQUE"):
        with engine.begin() as connection:
            base_insert = (
                "INSERT INTO super_assistant_delegations"
                " (id, owner_id, super_conversation_id, assistant_key, status,"
                "  summary, created_at, updated_at, last_turn_at)"
                " VALUES ('d4', 'u1', 'c1', 'ontology_agent', 'running', '',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(text(base_insert))
    engine.dispose()


def test_downgrade_drops_table(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "delegations-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    command.upgrade(cfg, "head")
    assert _table_exists(db_path)

    command.downgrade(cfg, "0097_sentinel_pattern")
    assert not _table_exists(db_path)


def test_head_is_single(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    # 0098 委派映射表在本迁移之后线性追加，head 随之演进
    assert heads == ["0102_super_assistant_palace_sync_token"]
