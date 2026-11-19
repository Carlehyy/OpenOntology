"""0147 migration parity, fail-closed drift handling and rollback contracts."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from app.auth.models import User
from app.model_registry import import_all_models
from app.shared.database import Base
from app.super_assistant.autoloop.models import AUTOLOOP_TABLES, FACT_TABLES
from tests.migrations.latest_head import LATEST_HEAD

BACKEND = Path(__file__).resolve().parents[3]
REVISION = "0147_autoloop"
HEAD = LATEST_HEAD
PARENT = "0146_integrated_cdc"
NAMES = {table.name for table in AUTOLOOP_TABLES}


def config(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    return cfg


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_db(request, tmp_path):
    if request.param == "postgresql":
        engine, url, _ = request.getfixturevalue("pg_schema")
        yield engine, config(url.render_as_string(hide_password=False))
    else:
        url = f"sqlite:///{tmp_path / 'migration.db'}"
        engine = create_engine(url)
        try:
            yield engine, config(url)
        finally:
            engine.dispose()


def version(engine):
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def assert_immutable(engine):
    # Exercise the database trigger on an existing fact, independent of the ORM.
    with engine.begin() as connection:
        owner = str(uuid4())
        connection.execute(User.__table__.insert().values(id=owner, username="migration-owner",
            email="migration@test.invalid", password_hash="fixture", role="admin"))
        connection.execute(text("INSERT INTO sa_autoloop_receipts "
            "(id,source_kind,source_system,source_event_id,dedup_key,owner_id,payload,accepted_at) "
            "VALUES (:id,'notification','fixture','event','dedup',:owner,'{}',CURRENT_TIMESTAMP)"),
            {"id": str(uuid4()), "owner": owner})
    for operation in ("UPDATE sa_autoloop_receipts SET dedup_key=dedup_key", "DELETE FROM sa_autoloop_receipts"):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.execute(text(operation))


@pytest.mark.parametrize("start_revision", ["0150_operations_dashboards", REVISION])
def test_published_branches_upgrade_to_single_head(migration_db, start_revision):
    engine, cfg = migration_db
    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_heads() == [HEAD]
    merge_revision = scripts.get_revision("0151_autoloop_merge")
    assert set(merge_revision.down_revision) == {"0150_operations_dashboards", REVISION}
    assert merge_revision.revision in {
        revision.revision for revision in scripts.iterate_revisions(HEAD, PARENT)
    }
    command.upgrade(cfg, start_revision)
    assert version(engine) == start_revision
    with engine.begin() as connection:
        connection.execute(User.__table__.insert().values(id="merge-owner", username="merge-owner",
            email="merge@test.invalid", password_hash="preserved", role="editor"))
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD
    assert NAMES | {"operations_dashboards", "v2_integrated_replicas"} <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert tuple(connection.execute(text("SELECT username,password_hash,role FROM users WHERE id='merge-owner'"))
                     .one()) == ("merge-owner", "preserved", "editor")
    assert_immutable(engine)


def test_fresh_head_upgrade_downgrade_reupgrade(migration_db):
    engine, cfg = migration_db
    assert ScriptDirectory.from_config(cfg).get_heads() == [HEAD]
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD and NAMES <= set(inspect(engine).get_table_names())
    assert_immutable(engine)
    command.downgrade(cfg, PARENT)
    assert version(engine) == PARENT and NAMES.isdisjoint(inspect(engine).get_table_names())
    command.upgrade(cfg, "head")
    assert version(engine) == HEAD and NAMES <= set(inspect(engine).get_table_names())


def test_registry_precreated_tables_and_existing_data_are_preserved(migration_db):
    engine, cfg = migration_db
    import_all_models()
    # Historical revision 0003 also imports the registry. Test the actual
    # pre-created current metadata shape rather than an alternate table model.
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(User.__table__.insert().values(id="existing-owner", username="existing",
            email="existing@test.invalid", password_hash="fixture", role="admin"))
    command.stamp(cfg, PARENT)
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION
    with engine.connect() as connection:
        assert connection.execute(text("SELECT username FROM users WHERE id='existing-owner'")).scalar_one() == "existing"
    # Re-running this revision validates existing metadata and does not reset it.
    command.stamp(cfg, PARENT)
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION


@pytest.mark.parametrize("fault", ["columns", "index", "trigger"])
def test_existing_incompatible_schema_is_rejected(migration_db, fault):
    engine, cfg = migration_db
    command.upgrade(cfg, REVISION)
    command.stamp(cfg, PARENT)
    with engine.begin() as connection:
        if fault == "columns":
            connection.execute(text("ALTER TABLE sa_autoloop_decisions ADD COLUMN unreviewed_column TEXT"))
        elif fault == "index":
            connection.execute(text("DROP INDEX uq_sa_al_unfinished_issue"))
            connection.execute(text("CREATE UNIQUE INDEX uq_sa_al_unfinished_issue ON "
                "sa_autoloop_decisions (owner_id,workspace_id,issue_key) WHERE status='open'"))
        elif engine.dialect.name == "sqlite":
            connection.execute(text("DROP TRIGGER sa_autoloop_receipts_update_immutable"))
            connection.execute(text("CREATE TRIGGER sa_autoloop_receipts_update_immutable "
                "BEFORE UPDATE ON sa_autoloop_receipts BEGIN SELECT 1; END"))
        else:
            connection.execute(text("ALTER TABLE sa_autoloop_receipts DISABLE TRIGGER sa_autoloop_receipts_immutable"))
    with pytest.raises(RuntimeError, match="incompatible"):
        command.upgrade(cfg, REVISION)
    assert version(engine) == PARENT


def test_missing_index_is_restored_without_changing_data(migration_db):
    engine, cfg = migration_db
    command.upgrade(cfg, REVISION)
    command.stamp(cfg, PARENT)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_sa_al_unfinished_issue"))
    command.upgrade(cfg, REVISION)
    index = next(item for item in inspect(engine).get_indexes("sa_autoloop_decisions")
                 if item["name"] == "uq_sa_al_unfinished_issue")
    assert index["unique"]
    predicate = str(index["dialect_options"][f"{engine.dialect.name}_where"])
    assert all(state in predicate for state in ("open", "pending_dispatch", "sending", "writeback_unknown", "writeback_failed"))


def test_revision_is_self_contained_and_registry_uses_canonical_identity():
    path = BACKEND / "alembic/versions/2026_11_14_0147_autoloop.py"
    source = path.read_text(encoding="utf-8")
    assert "app.super_assistant" not in source and "app.model" not in source
    spec = importlib.util.spec_from_file_location("autoloop_frozen_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == REVISION and revision.down_revision == PARENT
    import_all_models()
    for table in AUTOLOOP_TABLES:
        assert Base.metadata.tables[table.name] is table
    assert {table.name for table in revision.TABLES} == NAMES
    assert set(revision.FACT_NAMES) == {table.name for table in FACT_TABLES}
