"""Migration coverage for self-developed MCP (0108)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_adds_mcp_dev_tables_and_column(tmp_path, monkeypatch):
    """0108 建两张开发表并给 MCP 行表加 dev_project_id，可干净降级。"""
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "mcp-dev-migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0107_mapping_suggestion_queue")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert "super_assistant_mcp_dev_projects" not in tables
    assert "super_assistant_mcp_dev_versions" not in tables
    columns = {
        c["name"]
        for c in inspect(engine).get_columns("super_assistant_mcp_servers")
    }
    assert "dev_project_id" not in columns

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO super_assistant_mcp_servers "
                "(id, owner_id, name, display_name, description, builtin_key, "
                "transport, url, header_names, args, env_names, enabled, "
                "require_confirmation, tool_manifest, created_at, updated_at) "
                "VALUES ('mcp-1', 'owner-1', 'dmp-mcp-server', 'dmp-mcp-server', "
                "'', NULL, 'streamable_http', 'https://example.com/mcp', '[]', "
                "'[]', '[]', 1, 1, '[]', '2026-09-13', '2026-09-13')"
            )
        )
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert "super_assistant_mcp_dev_projects" in tables
    assert "super_assistant_mcp_dev_versions" in tables
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT transport, dev_project_id "
                "FROM super_assistant_mcp_servers WHERE id = 'mcp-1'"
            )
        ).fetchone()
    assert row.transport == "streamable_http"
    assert row.dev_project_id is None
    engine.dispose()
