"""Migration coverage for notifications tables (0121)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_creates_notification_tables(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "notifications-migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert {
        "notification_messages",
        "notification_message_states",
        "notification_attachments",
    } <= tables

    columns = {c["name"] for c in inspect(engine).get_columns("notification_messages")}
    assert {
        "id", "event_id", "source_system", "source_type", "title", "body_md",
        "priority", "created_by", "created_at", "updated_at",
    } <= columns

    state_columns = {
        c["name"] for c in inspect(engine).get_columns("notification_message_states")
    }
    assert {
        "id", "message_id", "user_id", "is_read", "read_at",
        "is_starred", "starred_at", "is_archived", "archived_at",
    } <= state_columns
    engine.dispose()


def test_downgrade_0121_removes_tables(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "notifications-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0120_user_report_token_hash")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert "notification_messages" not in tables
    assert "notification_message_states" not in tables
    assert "notification_attachments" not in tables
    engine.dispose()
