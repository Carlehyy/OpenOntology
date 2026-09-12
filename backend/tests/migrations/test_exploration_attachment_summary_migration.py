"""0106 探索附件索引卡摘要列迁移。"""
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


def _has_summary_column(db_path: Path) -> bool:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        found = connection.execute(text("PRAGMA table_info('bx_attachments')")).all()
    engine.dispose()
    return any(row[1] == "summary" for row in found)


def test_upgrade_downgrade_roundtrip(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "attachment-summary.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    # 钉在 0106 验证其往返(head 已推进到 0107,downgrade -1 口径随之变化)
    command.upgrade(cfg, "0106_exploration_attachment_summary")
    # 全新库经 0003 create_all 以当前模型建表（列随之创建），0106 幂等空操作。
    assert _has_summary_column(db_path)

    command.downgrade(cfg, "-1")
    assert not _has_summary_column(db_path)

    command.upgrade(cfg, "head")
    assert _has_summary_column(db_path)

    # summary 列可空（PRAGMA table_info 的 notnull 位为 0）
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        rows = connection.execute(text("PRAGMA table_info('bx_attachments')")).all()
    engine.dispose()
    col = next(row for row in rows if row[1] == "summary")
    assert col[3] == 0


def test_head_is_single(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0107_mapping_suggestion_queue"]
