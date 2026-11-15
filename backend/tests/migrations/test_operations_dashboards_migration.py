"""0147 监控看板卡片迁移。"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)

BACKEND = Path(__file__).resolve().parents[2]

EXPECTED_COLUMNS = {
    "id", "name", "description", "icon", "grafana_uid", "sort_order",
    "revision", "created_at", "updated_at",
}


def _config(db_path: Path) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _engine(cfg: Config):
    return create_engine(cfg.get_main_option("sqlalchemy.url"))


def test_upgrade_creates_dashboard_table_with_contract_shape(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(tmp_path / "dashboards-upgrade.db")
    command.upgrade(cfg, "head")
    engine = _engine(cfg)
    inspector = inspect(engine)
    assert "operations_dashboards" in inspector.get_table_names()
    columns = {
        column["name"]
        for column in inspector.get_columns("operations_dashboards")
    }
    assert columns == EXPECTED_COLUMNS
    assert (
        inspector.get_pk_constraint("operations_dashboards")["constrained_columns"]
        == ["id"]
    )
    uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("operations_dashboards")
    }
    assert ("grafana_uid",) in uniques
    indexes = {
        item["name"]: item["column_names"]
        for item in inspector.get_indexes("operations_dashboards")
    }
    assert indexes["ix_operations_dashboards_updated_at"] == ["updated_at"]
    assert indexes["ix_operations_dashboards_sort_order"] == ["sort_order"]
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0147_operations_dashboards"
    engine.dispose()


def test_downgrade_drops_dashboard_table_and_upgrade_restores(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(tmp_path / "dashboards-downgrade.db")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0146_integrated_cdc")
    engine = _engine(cfg)
    assert "operations_dashboards" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(cfg, "head")
    engine = _engine(cfg)
    assert "operations_dashboards" in inspect(engine).get_table_names()
    engine.dispose()


def test_upgrade_accepts_model_created_table_without_duplicates(tmp_path, monkeypatch):
    """模型 create_all 先行（运行时已建表）后升级 0147：幂等接受，不重复建。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "dashboards-precreated.db"
    from app.database import Base
    from app.model_registry import import_all_models

    import_all_models()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    cfg = _config(db_path)
    command.stamp(cfg, "0146_integrated_cdc")
    command.upgrade(cfg, "head")
    engine = _engine(cfg)
    inspector = inspect(engine)
    columns = {
        column["name"]
        for column in inspector.get_columns("operations_dashboards")
    }
    assert columns == EXPECTED_COLUMNS
    # 表/索引无重复（幂等，不因已存在而失败或重建）。
    names = [
        index["name"] for index in inspector.get_indexes("operations_dashboards")
    ]
    assert sorted(names) == [
        "ix_operations_dashboards_sort_order",
        "ix_operations_dashboards_updated_at",
    ]
    engine.dispose()


def test_upgrade_rejects_drifted_existing_table(tmp_path, monkeypatch):
    """已存在但形态漂移（如 name 长度不同）的表必须显式失败，不当正常。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "dashboards-drifted.db"
    drifted = Table(
        "operations_dashboards",
        MetaData(),
        Column("id", String(), primary_key=True),
        Column("name", String(10), nullable=False),
        Column("description", Text(), nullable=False),
        Column("icon", String(64), nullable=False),
        Column("grafana_uid", String(40), nullable=False),
        Column("sort_order", Integer(), nullable=False),
        Column("revision", Integer(), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    engine = create_engine(f"sqlite:///{db_path}")
    drifted.create(bind=engine)
    engine.dispose()
    cfg = _config(db_path)
    command.stamp(cfg, "0146_integrated_cdc")
    with pytest.raises(RuntimeError, match="incompatible"):
        command.upgrade(cfg, "head")
    engine = _engine(cfg)
    inspector = inspect(engine)
    name_column = [
        column
        for column in inspector.get_columns("operations_dashboards")
        if column["name"] == "name"
    ][0]
    # 漂移表未被悄悄改写。
    assert "VARCHAR(10)" in str(name_column["type"])
    engine.dispose()
