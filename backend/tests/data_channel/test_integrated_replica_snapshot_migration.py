"""Focused guards for the integrated-replica snapshot migration (0149) on SQLite.

Frozen migration DDL / model shape guards; the PostgreSQL replay is a
separate environment check (same convention as the 0146-0148 guards).
"""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from tests.migrations.latest_head import LATEST_HEAD


ROOT = Path(__file__).resolve().parents[2]

BATCH_TABLE = "v2_integrated_replica_snapshot_batches"
RUN_PARENT_TABLE = "v2_integrated_replica_runs"
BATCH_COLUMNS = {"run_id", "batch_id", "row_count", "created_at"}
BATCH_CHECKS = {"ck_integrated_replica_snapshot_batch_rows"}


def _migration(filename):
    specification = importlib.util.spec_from_file_location(
        "frozen_" + filename, ROOT / "alembic" / "versions" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _minimal_parents(metadata):
    sa.Table(RUN_PARENT_TABLE, metadata,
             sa.Column("id", sa.String(36), primary_key=True))


def _operations(connection):
    return Operations.context(MigrationContext.configure(connection))


def _enforce_foreign_keys(engine):
    @sa.event.listens_for(engine, "connect")
    def _hook(dbapi_connection, record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _insert_run(connection, run_id="r1"):
    connection.execute(sa.text(
        f"INSERT INTO {RUN_PARENT_TABLE} (id) VALUES (:run_id)"),
        {"run_id": run_id})


def _insert_batch(connection, run_id="r1", batch_id="b1", row_count=500):
    connection.execute(sa.text(
        f"INSERT INTO {BATCH_TABLE} (run_id, batch_id, row_count, created_at)"
        " VALUES (:run_id, :batch_id, :row_count, '2026-01-01 00:00:00')"),
        {"run_id": run_id, "batch_id": batch_id, "row_count": row_count})


def test_single_head_is_0149_on_top_of_0148():
    directory = ScriptDirectory(str(ROOT / "alembic"))
    heads = list(directory.get_heads())
    assert heads == [LATEST_HEAD], heads
    revision = directory.get_revision("0149_integrated_replica_snapshot")
    assert revision.down_revision == "0148_integrated_replica_capture"


def test_migration_accepts_the_model_created_schema_without_drift():
    """A fresh DB reaches 0149 after 0003 create_all built the model tables.

    Migration 0003 materializes every registered model (including the
    snapshot batch table) before 0149 runs on a fresh chain, so 0149 must
    verify — never re-create or reject — that exact shape.  The registry
    import must also register the new module.
    """
    migration = _migration("2026_11_12_0149_integrated_replica_snapshot.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            from app.model_registry import import_all_models
            from app.database import Base

            import_all_models()
            assert BATCH_TABLE in Base.metadata.tables
            Base.metadata.create_all(connection)
            with _operations(connection):
                # Idempotent: the frozen migration only verifies the shape.
                migration.upgrade()
                migration.upgrade()
            inspector = sa.inspect(connection)
            assert inspector.has_table(BATCH_TABLE)
            # The model-built composite primary key order is frozen too.
            assert inspector.get_pk_constraint(BATCH_TABLE)[
                "constrained_columns"] == ["run_id", "batch_id"]
    finally:
        engine.dispose()


def test_migration_creates_table_from_minimal_parents_and_enforces_contract():
    """0149 must upgrade a pre-0149 database with the frozen shape, composite
    identity and positive row counts, under real foreign key enforcement."""
    migration = _migration("2026_11_12_0149_integrated_replica_snapshot.py")
    engine = sa.create_engine("sqlite://")
    _enforce_foreign_keys(engine)
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _insert_run(connection)
            with _operations(connection):
                migration.upgrade()
            inspector = sa.inspect(connection)
            assert ({item["name"] for item in inspector.get_columns(BATCH_TABLE)}
                    == BATCH_COLUMNS)
            assert inspector.get_pk_constraint(BATCH_TABLE)[
                "constrained_columns"] == ["run_id", "batch_id"]
            # The composite primary key is the whole identity contract:
            # no extra unique constraints exist.
            assert inspector.get_unique_constraints(BATCH_TABLE) == []
            assert {item["name"]
                    for item in inspector.get_check_constraints(BATCH_TABLE)} \
                == BATCH_CHECKS
            foreign_keys = inspector.get_foreign_keys(BATCH_TABLE)
            assert len(foreign_keys) == 1
            assert foreign_keys[0]["constrained_columns"] == ["run_id"]
            assert foreign_keys[0]["referred_table"] == RUN_PARENT_TABLE
            assert foreign_keys[0]["referred_columns"] == ["id"]
            assert foreign_keys[0]["options"] == {"ondelete": "RESTRICT"}

            _insert_batch(connection)
            # A run may hold many batch receipts; only exact batch identity
            # is rejected, keeping same-batch redelivery idempotent.
            _insert_batch(connection, batch_id="b2", row_count=1)
            assert connection.execute(sa.text(
                f"SELECT COUNT(*) FROM {BATCH_TABLE}")).scalar_one() == 2
            # Empty or negative batches are not receipts.
            with pytest.raises(IntegrityError):
                _insert_batch(connection, batch_id="b3", row_count=0)
            with pytest.raises(IntegrityError):
                _insert_batch(connection, batch_id="b3", row_count=-1)
            # Same-batch redelivery collides on the composite primary key.
            with pytest.raises(IntegrityError):
                _insert_batch(connection, batch_id="b1", row_count=500)
            # RESTRICT: runs with surviving receipts cannot be deleted.
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(f"DELETE FROM {RUN_PARENT_TABLE}"))
    finally:
        engine.dispose()


def _batches_table(metadata, *, created_column="created_at",
                   pk_order=("run_id", "batch_id"), with_check=True,
                   row_count_type=sa.BigInteger, batch_id_type=sa.String(64),
                   ondelete="RESTRICT", created_nullable=False):
    elements = [
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("batch_id", batch_id_type, nullable=False),
        sa.Column("row_count", row_count_type, nullable=False),
        sa.Column(created_column, sa.DateTime(timezone=True),
                  nullable=created_nullable),
        sa.ForeignKeyConstraint(["run_id"], ["v2_integrated_replica_runs.id"],
                                ondelete=ondelete),
        sa.PrimaryKeyConstraint(*pk_order),
    ]
    if with_check:
        elements.append(sa.CheckConstraint(
            "row_count > 0", name="ck_integrated_replica_snapshot_batch_rows"))
    return sa.Table(BATCH_TABLE, metadata, *elements)


@pytest.mark.parametrize("drift", [
    "legacy_column",
    "pk_order",
    "missing_check",
    "row_count_integer",
    "batch_id_length",
    "fk_cascade",
    "nullable_created_at",
])
def test_migration_rejects_drifted_batch_table(drift):
    """0149 verifies — never silently accepts — a drifted batches table."""
    migration = _migration("2026_11_12_0149_integrated_replica_snapshot.py")
    kwargs = {
        "legacy_column": {"created_column": "received_at"},
        # Same columns, reversed composite key: receipt identity changes.
        "pk_order": {"pk_order": ("batch_id", "run_id")},
        "missing_check": {"with_check": False},
        "row_count_integer": {"row_count_type": sa.Integer},
        "batch_id_length": {"batch_id_type": sa.String(128)},
        "fk_cascade": {"ondelete": "CASCADE"},
        "nullable_created_at": {"created_nullable": True},
    }[drift]
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _batches_table(parents, **kwargs).create(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError):
                    migration.upgrade()
    finally:
        engine.dispose()


def test_downgrade_refuses_existing_data_and_only_drops_empty_table():
    """Downgrade never deletes snapshot data; the empty table drops."""
    migration = _migration("2026_11_12_0149_integrated_replica_snapshot.py")

    def _upgraded_engine():
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            with _operations(connection):
                migration.upgrade()
        return engine

    # Any surviving receipt blocks the downgrade; the seeded data survives.
    engine = _upgraded_engine()
    try:
        with engine.begin() as connection:
            _insert_run(connection)
            _insert_batch(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError, match=BATCH_TABLE):
                    migration.downgrade()
            assert connection.execute(sa.text(
                f"SELECT COUNT(*) FROM {BATCH_TABLE}")).scalar_one() == 1
    finally:
        engine.dispose()

    # A verifiably empty table downgrades, then the cycle replays.
    engine = _upgraded_engine()
    try:
        with engine.begin() as connection:
            with _operations(connection):
                migration.downgrade()
            inspector = sa.inspect(connection)
            assert not inspector.has_table(BATCH_TABLE)
            assert inspector.has_table(RUN_PARENT_TABLE)
            # Re-upgrade recreates the exact shape and verifies idempotently.
            with _operations(connection):
                migration.upgrade()
                migration.upgrade()
            assert sa.inspect(connection).has_table(BATCH_TABLE)
            with _operations(connection):
                migration.downgrade()
            assert not sa.inspect(connection).has_table(BATCH_TABLE)
    finally:
        engine.dispose()
