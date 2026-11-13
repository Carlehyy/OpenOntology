"""Focused guards for the integrated-replica migration (0147) on SQLite.

Frozen migration DDL / model shape guards; the PostgreSQL replay is a
separate environment check (same convention as the 0146 CDC guards in
tests/v2/migrations/test_integrated_cdc_migration.py).
"""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[2]

REPLICA_TABLE = "v2_integrated_replicas"
RUN_TABLE = "v2_integrated_replica_runs"
REPLICA_COLUMNS = {
    "asset_id", "status", "generation", "active_run_id", "pending_run_id",
    "schema_fingerprint", "applied_position", "applied_at",
    "coverage_confirmed_at", "heartbeat_at", "last_error_code",
    "created_at", "updated_at",
}
RUN_COLUMNS = {
    "id", "asset_id", "generation", "status", "schema_json",
    "schema_fingerprint", "row_count", "capture_id", "capture_start",
    "snapshot_proof", "applied_position", "owner_token", "lease_expires_at",
    "last_error_code", "created_at", "started_at", "completed_at",
    "cleaned_at",
}


def _migration(filename):
    specification = importlib.util.spec_from_file_location(
        "frozen_" + filename, ROOT / "alembic" / "versions" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _minimal_parents(metadata):
    sa.Table("v2_integrated_assets", metadata,
             sa.Column("id", sa.String, primary_key=True))


def _operations(connection):
    return Operations.context(MigrationContext.configure(connection))


def _insert_asset(connection, asset_id="a1"):
    connection.execute(sa.text(
        "INSERT INTO v2_integrated_assets (id) VALUES (:id)"), {"id": asset_id})


def _insert_replica(connection, asset_id="a1"):
    connection.execute(sa.text(
        f"INSERT INTO {REPLICA_TABLE} (asset_id, created_at, updated_at)"
        " VALUES (:asset_id, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"),
        {"asset_id": asset_id})


def _insert_run(connection, run_id="r1", asset_id="a1"):
    connection.execute(sa.text(
        f"INSERT INTO {RUN_TABLE} (id, asset_id, generation, schema_json,"
        " schema_fingerprint, created_at) VALUES"
        " (:id, :asset_id, 1, '{}', 'fp', '2026-01-01 00:00:00')"),
        {"id": run_id, "asset_id": asset_id})


def test_single_head_is_0147_on_top_of_0146():
    directory = ScriptDirectory(str(ROOT / "alembic"))
    heads = list(directory.get_heads())
    # The exact head id is pinned by the newest migration's own guards.
    assert len(heads) == 1, heads
    revision = directory.get_revision("0147_integrated_replicas")
    assert revision.down_revision == "0146_integrated_cdc"


def test_migration_accepts_the_model_created_schema_without_drift():
    """A fresh DB reaches 0147 after 0003 create_all built the model tables.

    Migration 0003 materializes every registered model (including both
    replica tables) before 0147 runs on a fresh chain, so 0147 must verify —
    never re-create or reject — that exact shape.
    """
    migration = _migration("2026_11_12_0147_integrated_replicas.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            from app.model_registry import import_all_models
            from app.database import Base

            import_all_models()
            Base.metadata.create_all(connection)
            with _operations(connection):
                # Idempotent: the frozen migration only verifies the shape.
                migration.upgrade()
                migration.upgrade()
            inspector = sa.inspect(connection)
            assert inspector.has_table(REPLICA_TABLE)
            assert inspector.has_table(RUN_TABLE)
            indexes = {item["name"]: item
                       for item in inspector.get_indexes(RUN_TABLE)}
            assert indexes["ix_integrated_replica_runs_asset_status"][
                "column_names"] == ["asset_id", "status"]
    finally:
        engine.dispose()


def test_migration_creates_tables_from_minimal_parents_and_enforces_checks():
    """0147 must upgrade a pre-0147 database with the frozen shape and
    vocabulary, then protect both directions of the FK contract."""
    migration = _migration("2026_11_12_0147_integrated_replicas.py")
    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            with _operations(connection):
                migration.upgrade()
            inspector = sa.inspect(connection)
            assert ({item["name"] for item in inspector.get_columns(REPLICA_TABLE)}
                    == REPLICA_COLUMNS)
            assert ({item["name"] for item in inspector.get_columns(RUN_TABLE)}
                    == RUN_COLUMNS)
            assert inspector.get_pk_constraint(REPLICA_TABLE)[
                "constrained_columns"] == ["asset_id"]
            assert inspector.get_pk_constraint(RUN_TABLE)[
                "constrained_columns"] == ["id"]
            replica_fks = inspector.get_foreign_keys(REPLICA_TABLE)
            assert len(replica_fks) == 1
            assert replica_fks[0]["constrained_columns"] == ["asset_id"]
            assert replica_fks[0]["referred_table"] == "v2_integrated_assets"
            assert replica_fks[0]["referred_columns"] == ["id"]
            assert replica_fks[0]["options"] == {"ondelete": "RESTRICT"}
            run_fks = inspector.get_foreign_keys(RUN_TABLE)
            assert run_fks[0]["referred_table"] == REPLICA_TABLE
            assert run_fks[0]["options"] == {"ondelete": "RESTRICT"}
            for table, expected in (
                (REPLICA_TABLE, {"ck_integrated_replica_status",
                                 "ck_integrated_replica_generation"}),
                (RUN_TABLE, {"ck_integrated_replica_run_status",
                             "ck_integrated_replica_run_generation",
                             "ck_integrated_replica_run_rows"}),
            ):
                assert {item["name"]
                        for item in inspector.get_check_constraints(table)} \
                    == expected, table
            indexes = {item["name"]: item
                       for item in inspector.get_indexes(RUN_TABLE)}
            assert indexes["ix_integrated_replica_runs_asset_status"][
                "column_names"] == ["asset_id", "status"]

            _insert_asset(connection)
            _insert_replica(connection)
            _insert_run(connection)
            # Frozen vocabularies and generation/row invariants are enforced.
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {REPLICA_TABLE} SET status = 'running'"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {REPLICA_TABLE} SET generation = -1"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {RUN_TABLE} SET status = 'running'"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {RUN_TABLE} SET generation = 0"))
            # RESTRICT: an asset with a registered replica cannot be deleted.
            with pytest.raises(IntegrityError):
                connection.execute(sa.text("DELETE FROM v2_integrated_assets"))
    finally:
        engine.dispose()


def _replicas_table(metadata, *, run_column="active_run_id",
                    status_sql=("status IN ('pending', 'initializing', "
                                "'catching_up', 'ready', 'stale', "
                                "'rebuild_required', 'blocked', 'stopped')"),
                    status_default="pending", ondelete="RESTRICT",
                    with_generation_check=True):
    elements = [
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(24), server_default=status_default,
                  nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column(run_column, sa.String(36), nullable=True),
        sa.Column("pending_run_id", sa.String(36), nullable=True),
        sa.Column("schema_fingerprint", sa.String(64), nullable=True),
        sa.Column("applied_position", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_confirmed_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["v2_integrated_assets.id"],
                                ondelete=ondelete),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.CheckConstraint(status_sql, name="ck_integrated_replica_status"),
    ]
    if with_generation_check:
        elements.append(sa.CheckConstraint(
            "generation >= 0", name="ck_integrated_replica_generation"))
    return sa.Table(REPLICA_TABLE, metadata, *elements)


@pytest.mark.parametrize("drift", [
    "legacy_column",
    "vocabulary_drift",
    "negated_vocabulary",
    "missing_check",
    "wrong_default",
    "fk_cascade",
])
def test_migration_rejects_drifted_replica_table(drift):
    """0147 verifies — never silently accepts — a drifted replicas table."""
    migration = _migration("2026_11_12_0147_integrated_replicas.py")
    vocabulary = ("('pending', 'initializing', 'catching_up', 'ready', "
                  "'stale', 'rebuild_required', 'blocked', 'stopped')")
    kwargs = {
        "legacy_column": {"run_column": "current_run_id"},
        "vocabulary_drift": {"status_sql": (
            "status IN ('pending', 'initializing', 'catching_up', 'running', "
            "'stale', 'rebuild_required', 'blocked', 'stopped')")},
        # Same literals, opposite meaning: a literal-only comparison would
        # wrongly accept it.
        "negated_vocabulary": {"status_sql": f"status NOT IN {vocabulary}"},
        "missing_check": {"with_generation_check": False},
        "wrong_default": {"status_default": "ready"},
        "fk_cascade": {"ondelete": "CASCADE"},
    }[drift]
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _replicas_table(parents, **kwargs).create(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError):
                    migration.upgrade()
    finally:
        engine.dispose()


def test_migration_rejects_drifted_run_table_default_and_index_shape():
    """Drifted run-table default / index shape must fail verification."""
    migration = _migration("2026_11_12_0147_integrated_replicas.py")

    def _drifted_default_engine():
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _replicas_table(parents).create(connection)
            sa.Table(
            RUN_TABLE, parents,
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("asset_id", sa.String(), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            # Drifted server default ("active" instead of "queued").
            sa.Column("status", sa.String(24), server_default="active",
                      nullable=False),
            sa.Column("schema_json", sa.JSON(), nullable=False),
            sa.Column("schema_fingerprint", sa.String(64), nullable=False),
            sa.Column("row_count", sa.BigInteger(), server_default="0",
                      nullable=False),
            sa.Column("capture_id", sa.String(36), nullable=True),
            sa.Column("capture_start", sa.JSON(), nullable=True),
            sa.Column("snapshot_proof", sa.JSON(), nullable=True),
            sa.Column("applied_position", sa.JSON(), nullable=True),
            sa.Column("owner_token", sa.String(36), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("last_error_code", sa.String(80), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["asset_id"], ["v2_integrated_replicas.asset_id"],
                ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint(migration.RUN_STATUS_SQL,
                               name="ck_integrated_replica_run_status"),
            sa.CheckConstraint("generation > 0",
                               name="ck_integrated_replica_run_generation"),
            sa.CheckConstraint("row_count >= 0",
                               name="ck_integrated_replica_run_rows"),
        ).create(connection)
        return engine

    # Drifted server default on runs.status must fail verification.
    engine = _drifted_default_engine()
    try:
        with engine.begin() as connection:
            with _operations(connection):
                with pytest.raises(RuntimeError, match="server default"):
                    migration.upgrade()
    finally:
        engine.dispose()

    # Same-name index on different columns is drift too.
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _replicas_table(parents).create(connection)
            runs = sa.Table(RUN_TABLE, parents, *migration._run_elements())
            runs.create(connection)
            sa.Index("ix_integrated_replica_runs_asset_status",
                     runs.c.status).create(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError, match="ix_integrated"):
                    migration.upgrade()
    finally:
        engine.dispose()


def test_downgrade_refuses_existing_data_and_only_drops_empty_tables():
    """Downgrade never deletes replica data; empty metadata tables drop."""
    migration = _migration("2026_11_12_0147_integrated_replicas.py")

    def _upgraded_engine():
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            with _operations(connection):
                migration.upgrade()
        return engine

    # A registered replica (runs empty) blocks the downgrade.
    engine = _upgraded_engine()
    try:
        with engine.begin() as connection:
            _insert_asset(connection)
            _insert_replica(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError, match=REPLICA_TABLE):
                    migration.downgrade()
            assert connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {REPLICA_TABLE}")).scalar_one() == 1
    finally:
        engine.dispose()

    # A surviving run row (even without active/pending pointers) blocks
    # the downgrade; both tables keep their data.
    engine = _upgraded_engine()
    try:
        with engine.begin() as connection:
            _insert_asset(connection)
            _insert_replica(connection)
            _insert_run(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError, match=RUN_TABLE):
                    migration.downgrade()
            assert connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {RUN_TABLE}")).scalar_one() == 1
            assert connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {REPLICA_TABLE}")).scalar_one() == 1
    finally:
        engine.dispose()

    # Verifiably empty tables downgrade, then the cycle replays.
    engine = _upgraded_engine()
    try:
        with engine.begin() as connection:
            with _operations(connection):
                migration.downgrade()
            inspector = sa.inspect(connection)
            assert not inspector.has_table(RUN_TABLE)
            assert not inspector.has_table(REPLICA_TABLE)
            assert inspector.has_table("v2_integrated_assets")
            # Re-upgrade recreates the exact shape and verifies idempotently.
            with _operations(connection):
                migration.upgrade()
                migration.upgrade()
            inspector = sa.inspect(connection)
            assert inspector.has_table(RUN_TABLE)
            assert inspector.has_table(REPLICA_TABLE)
            with _operations(connection):
                migration.downgrade()
            assert not sa.inspect(connection).has_table(REPLICA_TABLE)
    finally:
        engine.dispose()
