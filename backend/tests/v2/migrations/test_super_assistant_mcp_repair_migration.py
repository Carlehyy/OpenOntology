"""Regression coverage for repairing an incomplete Super Assistant schema."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_repairs_missing_super_assistant_mcp_table(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[3]
    db_path = tmp_path / "missing-super-assistant-mcp.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "0033_model_call_log_query_index")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE super_assistant_mcp_servers"))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "super_assistant_mcp_servers" in inspector.get_table_names()
    assert {
        "id", "owner_id", "name", "display_name", "description",
        "builtin_key", "transport", "url", "dev_project_id",
        "headers_encrypted", "header_names", "command", "args",
        "env_encrypted", "env_names", "enabled", "require_confirmation",
        "tool_manifest", "last_test_status", "last_test_message",
        "last_tested_at", "created_at", "updated_at",
    } == {column["name"] for column in inspector.get_columns("super_assistant_mcp_servers")}
    assert {
        "ix_super_assistant_mcp_servers_owner_id", "ix_sa_mcp_owner_updated",
        "ix_sa_mcp_servers_dev_project_id",
    } == {index["name"] for index in inspector.get_indexes("super_assistant_mcp_servers")}
    assert any(
        constraint["name"] == "uq_sa_mcp_owner_name"
        for constraint in inspector.get_unique_constraints("super_assistant_mcp_servers")
    )
    engine.dispose()


def test_0032_upgrade_repairs_legacy_partial_super_assistant_schema(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[3]
    db_path = tmp_path / "partial-super-assistant-schema.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    # Reproduce a database whose application-created schema has every Super
    # Assistant table except MCP while its migration revision is still 0031.
    command.upgrade(cfg, "0032_super_assistant")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE super_assistant_mcp_servers"))
    engine.dispose()
    command.stamp(cfg, "0031_governance_release_identity")

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "super_assistant_mcp_servers" in inspector.get_table_names()
    assert {"command", "args", "env_encrypted", "env_names", "builtin_key"} <= {
        column["name"] for column in inspector.get_columns("super_assistant_mcp_servers")
    }
    engine.dispose()
