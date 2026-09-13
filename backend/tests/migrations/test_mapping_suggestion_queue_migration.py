"""0107 映射建议人工确认队列表迁移。"""
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


def _has_queue_table(db_path: Path) -> bool:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    engine.dispose()
    return any(row[0] == "v2_mapping_suggestions" for row in found)


def test_upgrade_downgrade_roundtrip(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "mapping-suggestion-queue.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    # kernel.v1 迁移位于 0107 之后；全新库经历史链创建父表，
    # -2 回退到 0106 以验证 0107 队列表仍可逆。
    command.upgrade(cfg, "head")
    assert _has_queue_table(db_path)

    command.downgrade(cfg, "-2")
    assert not _has_queue_table(db_path)

    command.upgrade(cfg, "head")
    assert _has_queue_table(db_path)
