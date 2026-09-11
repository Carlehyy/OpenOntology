"""0101 本体读路径性能索引迁移。"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _index_columns(db_path: Path, table: str, index_name: str) -> list[str] | None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        for index in inspect(engine).get_indexes(table):
            if index["name"] == index_name:
                return list(index.get("column_names") or [])
        return None
    finally:
        engine.dispose()


def _create_minimal_tables(db_path: Path) -> None:
    """模拟只手工建被测表的部分迁移测试场景（0093-0100 同一防御口径）。"""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE ontology_versions (id TEXT PRIMARY KEY,"
            " ontology_id TEXT NOT NULL, created_at TIMESTAMP)"))
        connection.execute(text(
            "CREATE TABLE sentinel_firings (id TEXT PRIMARY KEY,"
            " ontology_id TEXT NOT NULL, ontology_release_id TEXT,"
            " created_at TIMESTAMP)"))
    engine.dispose()


def test_upgrade_creates_readpath_indexes(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "readpath-indexes.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_alembic_config(backend, db_path), "head")

    assert _index_columns(
        db_path, "ontology_versions", "ix_ontology_versions_ontology_created"
    ) == ["ontology_id", "created_at"]
    assert _index_columns(
        db_path, "sentinel_firings", "ix_sentinel_firings_release_time"
    ) == ["ontology_id", "ontology_release_id", "created_at"]


def test_downgrade_drops_readpath_indexes(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "readpath-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0102_super_assistant_palace_sync_token")

    assert _index_columns(
        db_path, "ontology_versions", "ix_ontology_versions_ontology_created"
    ) is None
    assert _index_columns(
        db_path, "sentinel_firings", "ix_sentinel_firings_release_time"
    ) is None


def test_upgrade_skips_when_indexes_already_exist(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "readpath-precreated.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _create_minimal_tables(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX ix_ontology_versions_ontology_created"
            " ON ontology_versions (ontology_id, created_at)"))
        connection.execute(text(
            "CREATE INDEX ix_sentinel_firings_release_time"
            " ON sentinel_firings (ontology_id, ontology_release_id, created_at)"))
    engine.dispose()
    cfg = _alembic_config(backend, db_path)
    command.stamp(cfg, "0102_super_assistant_palace_sync_token")
    # 索引已存在（如模型 create_all 先行建过）：升级必须幂等跳过而非报错。
    command.upgrade(cfg, "head")

    assert _index_columns(
        db_path, "ontology_versions", "ix_ontology_versions_ontology_created"
    ) == ["ontology_id", "created_at"]


def test_upgrade_refuses_same_name_different_columns(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "readpath-conflicting.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _create_minimal_tables(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        # 同名索引但列组合不一致：静默跳过会让性能索引永久缺失，必须硬报错。
        connection.execute(text(
            "CREATE INDEX ix_ontology_versions_ontology_created"
            " ON ontology_versions (id)"))
    engine.dispose()
    cfg = _alembic_config(backend, db_path)
    command.stamp(cfg, "0102_super_assistant_palace_sync_token")

    with pytest.raises(RuntimeError, match="ix_ontology_versions_ontology_created"):
        command.upgrade(cfg, "head")
