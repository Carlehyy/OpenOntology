"""0100 内置工具启停设置表迁移。"""
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


def _table_exists(db_path: Path) -> bool:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        found = connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='super_assistant_tool_settings'"
        )).first()
    engine.dispose()
    return found is not None


def test_upgrade_creates_table_with_owner_primary_key(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "tool-settings.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_alembic_config(backend, db_path), "head")

    assert _table_exists(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        # 全新库经 0003 create_all 以当前模型建表（缺行=全启用语义由应用层
        # 保证，列只存禁用名单）；存量库走 0100 静态 DDL
        connection.execute(text(
            "INSERT INTO super_assistant_tool_settings (owner_id, disabled_tools, updated_at)"
            " VALUES ('u1', '[\"web_search\"]', CURRENT_TIMESTAMP)"
        ))
        row = connection.execute(text(
            "SELECT disabled_tools FROM super_assistant_tool_settings WHERE owner_id = 'u1'"
        )).scalar()
    assert row == '["web_search"]'
    # owner 是主键：同用户第二行被拒（每用户一行名单）
    import pytest
    with pytest.raises(Exception, match="UNIQUE|PRIMARY"):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO super_assistant_tool_settings (owner_id, disabled_tools, updated_at)"
                " VALUES ('u1', '[]', CURRENT_TIMESTAMP)"
            ))
    engine.dispose()


def test_downgrade_drops_table(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "tool-settings-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    command.upgrade(cfg, "head")
    assert _table_exists(db_path)
    command.downgrade(cfg, "0099_super_assistant_remote_agents")
    assert not _table_exists(db_path)


def test_head_is_single(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(backend, tmp_path / "heads-check.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0104_super_assistant_remote_agent_invites"]
