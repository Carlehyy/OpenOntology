"""0145 平台级运营报告册迁移。"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_creates_report_tables_and_fk(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "operations-reports.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(_config(backend, db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert {"operations_reports", "operations_report_articles"}.issubset(inspector.get_table_names())
    assert {"report_id", "dataset_id"}.issubset({column["name"] for column in inspector.get_columns("operations_report_articles")})
    assert any(fk["referred_table"] == "v2_datasets" for fk in inspector.get_foreign_keys("operations_report_articles"))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0145_operations_reports"
    engine.dispose()


def test_downgrade_drops_report_tables(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "operations-reports-downgrade.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(backend, db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0144_hindsight_memory")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "operations_reports" not in inspect(engine).get_table_names()
    engine.dispose()
