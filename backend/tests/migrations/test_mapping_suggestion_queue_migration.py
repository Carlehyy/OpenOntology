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

    # head 即 0107;全新库经 0003 create_all 以当前模型建表(表随之创建),
    # 0107 upgrade 为幂等空操作,downgrade 负责验证可逆。
    command.upgrade(cfg, "head")
    assert _has_queue_table(db_path)

    command.downgrade(cfg, "-1")
    assert not _has_queue_table(db_path)

    command.upgrade(cfg, "head")
    assert _has_queue_table(db_path)
