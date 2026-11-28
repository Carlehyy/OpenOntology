"""0156 routing-policy versions: chain, parity, immutability and rollback.

The versions are the audit record of what a rule miss was assessed under, so
the table must be identical whether a fresh chain pre-created it from the
model registry or this revision created it, and it must stay append-only.
"""
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
from sqlalchemy.orm import Session

from app.auth.models import User
from app.model_registry import import_all_models
from app.shared.database import Base
from app.super_assistant.autoloop.models import (
    APPEND_ONLY_MODELS, AUTOLOOP_TABLES, FACT_TABLES, AutoLoopRoutingPolicy,
)
from tests.migrations.latest_head import LATEST_HEAD

BACKEND = Path(__file__).resolve().parents[3]
REVISION = "0156_autoloop_routing_policy"
PARENT = "0155_multica_profile_web_url"
TABLE = AutoLoopRoutingPolicy.__table__
NAME = TABLE.name
PATH = BACKEND / "alembic/versions/2026_11_25_0156_autoloop_routing_policy.py"
FACT_REVISION = BACKEND / "alembic/versions/2026_11_14_0147_autoloop.py"


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_db(request, tmp_path):
    if request.param == "postgresql":
        engine, url, _ = request.getfixturevalue("pg_schema")
        yield engine, config(url.render_as_string(hide_password=False))
    else:
        url = f"sqlite:///{tmp_path / 'routing-policy.db'}"
        engine = create_engine(url)
        try:
            yield engine, config(url)
        finally:
            engine.dispose()


def config(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    return cfg


def version(engine):
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def add_owner(engine, identity=None):
    owner = identity or str(uuid4())
    with engine.begin() as connection:
        connection.execute(User.__table__.insert().values(id=owner, username=owner,
            email=f"{owner}@test.invalid", password_hash="fixture", role="admin"))
    return owner


def add_version(engine, owner, number=1, prompt="prefer a decision card"):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO sa_autoloop_routing_policies (owner_id, version, prompt_text, created_by, created_at) "
            "VALUES (:owner, :version, :prompt, :owner, CURRENT_TIMESTAMP)"),
            {"owner": owner, "version": number, "prompt": prompt})


def prompts(engine, owner):
    with engine.connect() as connection:
        return list(connection.execute(text(
            "SELECT version, prompt_text FROM sa_autoloop_routing_policies "
            "WHERE owner_id = :owner ORDER BY version"), {"owner": owner}).all())


def assert_immutable(engine, owner):
    # Exercise the database trigger itself, independent of the ORM.
    for statement in (
        "UPDATE sa_autoloop_routing_policies SET prompt_text = 'rewritten' WHERE owner_id = :owner",
        "DELETE FROM sa_autoloop_routing_policies WHERE owner_id = :owner",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            with engine.begin() as connection:
                connection.execute(text(statement), {"owner": owner})


def assert_matches_model(engine):
    """The migrated object is exactly the registered model, on any dialect."""
    inspector = inspect(engine)
    columns = {item["name"]: item for item in inspector.get_columns(NAME)}
    assert set(columns) == set(TABLE.c.keys())
    for column in TABLE.c:
        actual = columns[column.name]
        expected = str(column.type.compile(dialect=engine.dialect)).upper()
        assert actual["nullable"] == column.nullable
        assert str(actual["type"].compile(dialect=engine.dialect)).upper() == expected
    assert inspector.get_pk_constraint(NAME)["constrained_columns"] == ["owner_id", "version"]
    assert [(item["referred_table"], item["referred_columns"], item["constrained_columns"])
            for item in inspector.get_foreign_keys(NAME)] == [("users", ["id"], ["owner_id"])]
    if engine.dialect.name == "postgresql":
        assert [item["name"] for item in inspector.get_check_constraints(NAME)] == [
            "ck_sa_al_routing_policy_version"]


def test_latest_head_is_0156_and_the_revision_is_self_contained():
    cfg = config("sqlite://")
    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_heads() == [LATEST_HEAD]
    assert LATEST_HEAD == REVISION
    assert scripts.get_revision(REVISION).down_revision == PARENT
    # The frozen definition never imports application models.
    source = PATH.read_text(encoding="utf-8")
    assert "app." not in source
    spec = importlib.util.spec_from_file_location("routing_policy_frozen_revision", PATH)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == REVISION and revision.down_revision == PARENT
    assert set(revision.sa_autoloop_routing_policies.c.keys()) == set(TABLE.c.keys())


def test_round_trip_creates_validates_and_removes_versions(migration_db):
    engine, cfg = migration_db
    assert ScriptDirectory.from_config(cfg).get_heads() == [LATEST_HEAD]
    command.upgrade(cfg, PARENT)
    owner = add_owner(engine)
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    assert_matches_model(engine)
    add_version(engine, owner, 1)
    add_version(engine, owner, 2, None)
    assert_immutable(engine, owner)
    # Downgrade drops the history with the table and nothing else.
    command.downgrade(cfg, PARENT)
    assert version(engine) == PARENT
    assert NAME not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar() == 1
    # Re-created by this revision alone: no registry create_all has run since.
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    assert_matches_model(engine)
    owner = add_owner(engine)
    add_version(engine, owner)
    assert_immutable(engine, owner)
    assert prompts(engine, owner) == [(1, "prefer a decision card")]


def test_registry_precreated_table_and_rows_are_preserved(migration_db):
    engine, cfg = migration_db
    import_all_models()
    # Historical 0003 also imports the registry: on a fresh chain the table
    # exists before this revision runs and must be validated, not recreated.
    Base.metadata.create_all(engine)
    owner = add_owner(engine, "policy-owner")
    add_version(engine, owner, 3, "kept")
    command.stamp(cfg, PARENT)
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    assert_matches_model(engine)
    assert prompts(engine, owner) == [(3, "kept")]
    assert_immutable(engine, owner)
    # Re-running the revision validates the existing object and resets nothing.
    command.stamp(cfg, PARENT)
    command.upgrade(cfg, "head")
    assert version(engine) == REVISION
    assert prompts(engine, owner) == [(3, "kept")]
    assert_immutable(engine, owner)


@pytest.mark.parametrize("fault", ["columns", "trigger"])
def test_existing_incompatible_object_is_rejected_before_any_change(migration_db, fault):
    engine, cfg = migration_db
    command.upgrade(cfg, REVISION)
    owner = add_owner(engine)
    add_version(engine, owner)
    command.stamp(cfg, PARENT)
    with engine.begin() as connection:
        if fault == "columns":
            connection.execute(text(f"ALTER TABLE {NAME} ADD COLUMN unreviewed_column TEXT"))
        elif engine.dialect.name == "sqlite":
            connection.execute(text(f"DROP TRIGGER {NAME}_update_immutable"))
            connection.execute(text(f"CREATE TRIGGER {NAME}_update_immutable "
                                    f"BEFORE UPDATE ON {NAME} BEGIN SELECT 1; END"))
        else:
            connection.execute(text(f"ALTER TABLE {NAME} DISABLE TRIGGER {NAME}_immutable"))
    with pytest.raises(RuntimeError, match="incompatible"):
        command.upgrade(cfg, REVISION)
    assert version(engine) == PARENT
    assert prompts(engine, owner) == [(1, "prefer a decision card")]


def test_missing_immutable_trigger_is_restored_without_changing_rows(migration_db):
    engine, cfg = migration_db
    command.upgrade(cfg, REVISION)
    owner = add_owner(engine)
    add_version(engine, owner)
    command.stamp(cfg, PARENT)
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            connection.execute(text(f"DROP TRIGGER {NAME}_delete_immutable"))
        else:
            connection.execute(text(f"DROP TRIGGER {NAME}_immutable ON {NAME}"))
    command.upgrade(cfg, REVISION)
    assert version(engine) == REVISION
    assert prompts(engine, owner) == [(1, "prefer a decision card")]
    assert_immutable(engine, owner)


def test_registry_uses_the_canonical_identity_and_0147_stays_untouched():
    import_all_models()
    assert Base.metadata.tables[NAME] is TABLE
    assert TABLE in AUTOLOOP_TABLES
    # 0147 owns the facts; the versions are a separate, later append-only table.
    assert TABLE not in FACT_TABLES
    assert AutoLoopRoutingPolicy in APPEND_ONLY_MODELS
    assert "routing_policies" not in FACT_REVISION.read_text(encoding="utf-8")


def test_orm_refuses_to_rewrite_or_delete_a_version(migration_db):
    engine, cfg = migration_db
    command.upgrade(cfg, "head")
    owner = add_owner(engine)
    add_version(engine, owner)
    with Session(engine) as db:
        row = db.get(AutoLoopRoutingPolicy, (owner, 1))
        row.prompt_text = "rewritten"
        with pytest.raises(ValueError, match="append-only"):
            db.flush()
        db.rollback()
        db.delete(db.get(AutoLoopRoutingPolicy, (owner, 1)))
        with pytest.raises(ValueError, match="append-only"):
            db.flush()
        db.rollback()
    assert prompts(engine, owner) == [(1, "prefer a decision card")]
