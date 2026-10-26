"""0127 输入框快捷内容表迁移。"""
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
            " AND name='super_assistant_quick_inputs'"
        )).first()
    engine.dispose()
    return found is not None


def test_upgrade_creates_table_and_roundtrips_row(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "quick-inputs.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_alembic_config(backend, db_path), "head")

    assert _table_exists(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO super_assistant_quick_inputs"
            " (id, owner_id, title, content, sort_order, created_at, updated_at)"
            " VALUES ('qi-1', 'u1', '自我介绍', '我是产品经理', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        row = connection.execute(text(
            "SELECT title, content, sort_order FROM super_assistant_quick_inputs WHERE id = 'qi-1'"
        )).fetchone()
    assert tuple(row) == ("自我介绍", "我是产品经理", 0)
    engine.dispose()


def test_downgrade_drops_table(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "quick-inputs-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    command.upgrade(cfg, "head")
    assert _table_exists(db_path)
    command.downgrade(cfg, "0126_seed_task_templates")
    assert not _table_exists(db_path)


def test_head_is_single(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0137_integrated_field_config"]
