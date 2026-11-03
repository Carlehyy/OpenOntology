"""0141 旧记忆宫殿文件图谱五表退役迁移。

head 上五表必须不存在；降级到 0140 只回建原结构的空表（数据不可恢复），
再次升级到 head 后五表再次移除。
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

_TABLES = (
    "super_assistant_palace_files",
    "super_assistant_palace_builds",
    "super_assistant_palace_folders",
    "super_assistant_palace_ontology_documents",
    "super_assistant_palace_sync_tokens",
)


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _table_names(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_head_drops_and_downgrade_recreates_empty_palace_tables(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "retire-palace.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    command.upgrade(cfg, "head")
    tables = _table_names(db_path)
    assert not set(_TABLES) & tables

    command.downgrade(cfg, "0140_super_assistant_conversation_summaries")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert set(_TABLES) <= tables
        assert {
            "id", "owner_id", "filename", "folder_path", "artifact_id",
            "mime_type", "size", "sha256", "extracted_chars", "status",
            "error", "entity_count", "relation_count", "created_at",
            "updated_at",
        } <= {column["name"] for column in inspector.get_columns(_TABLES[0])}
        assert {
            "owner_id", "path", "created_at", "updated_at",
        } <= {column["name"] for column in inspector.get_columns(_TABLES[2])}
        assert {
            "owner_id", "token_hash", "token_encrypted", "last_used_at",
        } <= {column["name"] for column in inspector.get_columns(_TABLES[4])}
        folder_uniques = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(_TABLES[2])
        }
        assert ("owner_id", "path") in folder_uniques
        ontology_uniques = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(_TABLES[3])
        }
        assert ("ontology_id",) in ontology_uniques
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    assert not set(_TABLES) & _table_names(db_path)
