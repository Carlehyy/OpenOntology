"""Migration coverage for super assistant conversation browser source (0110)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_adds_browser_source_binding(tmp_path, monkeypatch):
    """0110 给 super_assistant_conversations 加 browser_source_id（SET NULL FK + 索引），可干净降级。"""
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "sa-browser-source-migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("super_assistant_conversations")}
    assert "browser_source_id" in columns
    fks = [
        fk for fk in inspector.get_foreign_keys("super_assistant_conversations")
        if fk["referred_table"] == "v2_steward_browser_sources"
        and fk["constrained_columns"] == ["browser_source_id"]
    ]
    assert len(fks) == 1
    assert fks[0].get("options", {}).get("ondelete", "").lower() == "set null"
    indexes = {i["name"] for i in inspector.get_indexes("super_assistant_conversations")}
    assert "ix_super_assistant_conversations_browser_source_id" in indexes
    engine.dispose()

    command.downgrade(cfg, "0109_user_query_keys")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("super_assistant_conversations")}
    assert "browser_source_id" not in columns
    fks = [
        fk for fk in inspector.get_foreign_keys("super_assistant_conversations")
        if fk["referred_table"] == "v2_steward_browser_sources"
    ]
    assert not fks
    indexes = {i["name"] for i in inspector.get_indexes("super_assistant_conversations")}
    assert "ix_super_assistant_conversations_browser_source_id" not in indexes
    engine.dispose()
