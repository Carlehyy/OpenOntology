"""0145 平台级运营报告册迁移。"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from tests.migrations.latest_head import LATEST_HEAD


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
    assert {"report_id", "dataset_id", "plan_json"}.issubset({column["name"] for column in inspector.get_columns("operations_report_articles")})
    assert any(fk["referred_table"] == "v2_datasets" for fk in inspector.get_foreign_keys("operations_report_articles"))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == LATEST_HEAD
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


def test_plan_json_upgrade_preserves_legacy_articles(tmp_path, monkeypatch):
    """0151 → 0152：纯增量 plan_json 列，存量 markdown 文章原样保留且保持
    NULL；降级只删列不删行。"""
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "operations-reports-legacy.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _config(backend, db_path)
    command.upgrade(cfg, "0151_autoloop_merge")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO operations_reports "
            "(id, name, description, sort_order, revision, created_at, updated_at) "
            "VALUES ('r1', '旧周报', 'legacy', 0, 1, '2026-11-01 00:00:00', '2026-11-01 00:00:00')"))
        connection.execute(text(
            "INSERT INTO operations_report_articles "
            "(id, report_id, title, markdown, dataset_id, sort_order, revision, created_at, updated_at) "
            "VALUES ('a1', 'r1', '旧文章', '# legacy\n', 'd1', 0, 1, "
            "'2026-11-01 00:00:00', '2026-11-01 00:00:00')"))
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    columns = {column["name"] for column in
               inspect(engine).get_columns("operations_report_articles")}
    assert "plan_json" in columns
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT markdown, plan_json FROM operations_report_articles WHERE id = 'a1'")).one()
    assert row.markdown == "# legacy\n"
    assert row.plan_json is None
    engine.dispose()

    command.downgrade(cfg, "0151_autoloop_merge")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "plan_json" not in {column["name"] for column in
                               inspect(engine).get_columns("operations_report_articles")}
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT markdown FROM operations_report_articles WHERE id = 'a1'"
        )).scalar_one() == "# legacy\n"
    engine.dispose()
