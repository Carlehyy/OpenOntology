"""Migration coverage for the palace folder-sync token table.

全新库经迁移 0003 的 create_all 已按最新模型建出
super_assistant_palace_sync_tokens（0100 建表守卫跳过）；0100 的建表 DDL
对存量库生效，且 downgrade 必须能干净移除。这里用「head → 降级 0099 →
再升 head」的往返验证两条路径。
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_downgrade_roundtrip(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "palace-sync-token-migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "super_assistant_palace_sync_tokens" in inspector.get_table_names()
    columns = {
        column["name"]
        for column in inspector.get_columns("super_assistant_palace_sync_tokens")
    }
    assert {"owner_id", "token_hash", "token_encrypted", "last_used_at"} <= columns
    engine.dispose()

    # 降级到 0099：表被移除（此时库不再有 0100 的痕迹）
    command.downgrade(cfg, "0099_super_assistant_remote_agents")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "super_assistant_palace_sync_tokens" not in inspect(engine).get_table_names()
    engine.dispose()

    # 再升回 head：这次由 0100 自己的 create_table 建表（存量库路径）
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "super_assistant_palace_sync_tokens" in inspect(engine).get_table_names()
    engine.dispose()
