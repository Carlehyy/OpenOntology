"""Migration coverage for user query keys (0109)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_adds_user_query_keys_table(tmp_path, monkeypatch):
    """0109 建 user_query_keys（sha256 唯一约束 + user_id 索引），可干净降级。"""
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "user-query-keys-migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "user_query_keys" in tables

    columns = {c["name"] for c in inspector.get_columns("user_query_keys")}
    assert columns == {
        "id", "user_id", "category", "name", "key_prefix", "key_hash",
        "expires_at", "revoked_at", "last_used_at", "created_at",
    }
    unique_constraints = {
        tuple(c["column_names"]) for c in inspector.get_unique_constraints("user_query_keys")
    }
    assert ("key_hash",) in unique_constraints
    indexes = {i["name"] for i in inspector.get_indexes("user_query_keys")}
    assert "ix_user_query_keys_user_id" in indexes
    engine.dispose()

    command.downgrade(cfg, "0108_super_assistant_mcp_dev")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "user_query_keys" not in set(inspect(engine).get_table_names())
    engine.dispose()
