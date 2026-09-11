"""Migration coverage for the palace ontology-documents mirror table.

全新库经迁移 0003 的 create_all 已按最新模型建出
super_assistant_palace_ontology_documents（0104 建表守卫跳过）；0104 的
建表 DDL 对存量库生效，且 downgrade 必须能干净移除。这里用
「head → 降级 0102 → 再升 head」的往返验证两条路径。
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_downgrade_roundtrip(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "palace-ontology-docs-migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "super_assistant_palace_ontology_documents" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns(
        "super_assistant_palace_ontology_documents",
    )}
    assert {
        "id", "ontology_id", "version_id", "version_number", "ontology_name",
        "title", "fingerprint", "artifact_id", "size", "extracted_chars",
        "status", "error", "entity_count", "relation_count",
        "created_at", "updated_at",
    } <= columns
    # 幂等键：每本体一行
    unique = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "super_assistant_palace_ontology_documents",
        )
    }
    assert ("ontology_id",) in unique

    command.downgrade(cfg, "0104_super_assistant_remote_agent_invites")
    assert "super_assistant_palace_ontology_documents" not in inspect(engine).get_table_names()

    command.upgrade(cfg, "head")
    assert "super_assistant_palace_ontology_documents" in inspect(engine).get_table_names()
