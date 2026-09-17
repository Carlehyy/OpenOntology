"""0109 kernel context source tombstone migration contract."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _cfg(tmp_path: Path) -> tuple[Config, Path]:
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "context-tombstones.db"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg, db_path


def test_upgrade_and_downgrade_context_source_tombstones(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg, db_path = _cfg(tmp_path)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "super_assistant_context_source_tombstones" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("super_assistant_context_source_tombstones")}
    assert {"owner_id", "kind", "source_id", "revision", "locator", "recipe_revision", "extraction_id", "reason", "tombstone_at"} <= columns
    indexes = {index["name"] for index in inspector.get_indexes("super_assistant_context_source_tombstones")}
    assert "ix_sa_context_tombstones_owner_kind" in indexes
    engine.dispose()
    command.downgrade(cfg, "0111_super_assistant_kernel")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "super_assistant_context_source_tombstones" not in inspect(engine).get_table_names()
    engine.dispose()
