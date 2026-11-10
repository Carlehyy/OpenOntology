"""0140 会话纪要表 + 用户级生成模型配置表 + 消息 completed_at 列迁移。"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect as sa_inspect, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _table_exists(engine, name: str) -> bool:
    return sa_inspect(engine).has_table(name)


def test_upgrade_creates_tables_column_and_roundtrips_row(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "summary.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_alembic_config(backend, db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    assert _table_exists(engine, "super_assistant_conversation_summaries")
    assert _table_exists(engine, "super_assistant_summary_settings")
    assert "completed_at" in {
        column["name"] for column in sa_inspect(engine).get_columns("super_assistant_messages")
    }
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO super_assistant_conversation_summaries"
            " (conversation_id, structured, markdown, model_config_id, message_count,"
            "  generated_at, updated_at)"
            " VALUES ('c-1', '{\"title\": \"测试\"}', '### 原文', NULL, 3,"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        row = connection.execute(text(
            "SELECT structured, markdown, message_count"
            " FROM super_assistant_conversation_summaries WHERE conversation_id = 'c-1'"
        )).fetchone()
    assert row == ('{"title": "测试"}', "### 原文", 3)
    engine.dispose()


def test_downgrade_drops_tables_and_column(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "summary-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    assert _table_exists(engine, "super_assistant_conversation_summaries")
    assert _table_exists(engine, "super_assistant_summary_settings")
    engine.dispose()

    command.downgrade(cfg, "0139_curated_candidate_provenance")
    engine = create_engine(f"sqlite:///{db_path}")
    assert not _table_exists(engine, "super_assistant_conversation_summaries")
    assert not _table_exists(engine, "super_assistant_summary_settings")
    assert "completed_at" not in {
        column["name"] for column in sa_inspect(engine).get_columns("super_assistant_messages")
    }
    engine.dispose()


def test_head_is_single(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0143_hindsight_memory"]
