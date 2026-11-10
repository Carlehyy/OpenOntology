"""Focused guards for the partial CDC schema/migration (0144).

Frozen migration DDL / model shape guards on SQLite; PostgreSQL replay is
separate E2E (same convention as test_integrated_migrations.py).
"""
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[3]

CDC_COLUMNS = (
    "source_event_id", "event_group_id", "external_asset_id",
    "external_mapping_id", "event_snapshot",
)


def _migration(filename):
    specification = importlib.util.spec_from_file_location(
        "frozen_" + filename, ROOT / "alembic" / "versions" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _partial_where(indexes: dict, name: str) -> str:
    options = indexes[name].get("dialect_options", {})
    assert "sqlite_where" in options, f"{name} must be a partial index"
    return str(options["sqlite_where"]).strip()


def test_partial_cdc_schema_create_all_builds_partial_unique_indexes_on_sqlite():
    """Base create_all must succeed and materialize both partial indexes.

    The unique-index column list below pins the MODEL side exactly; the
    migration-side twin assertion lives in the minimal-parent test, so any
    model↔migration drift fails one of the two guards loudly.
    """
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            from app.model_registry import import_all_models
            from app.database import Base

            import_all_models()
            Base.metadata.create_all(connection)
            inspector = sa.inspect(connection)
            assert inspector.has_table("v2_integrated_cdc_sources")
            assert inspector.has_table("v2_integrated_cdc_subscriptions")
            indexes = {
                item["name"]: item
                for item in inspector.get_indexes("sentinel_event_log")
            }
            assert indexes["uq_sentinel_event_log_external"]["unique"]
            assert indexes["uq_sentinel_event_log_external"]["column_names"] == [
                "ontology_id", "ontology_release_id", "external_asset_id",
                "external_mapping_id", "object_type_id", "source_event_id",
                "key",
            ]
            assert _partial_where(
                indexes, "uq_sentinel_event_log_external") == \
                "source_event_id IS NOT NULL"
            group = indexes["ix_sentinel_event_log_external_group"]
            assert not group["unique"]
            assert group["column_names"] == ["event_group_id", "id"]
            assert _partial_where(
                indexes, "ix_sentinel_event_log_external_group") == \
                "event_group_id IS NOT NULL"
            checks = {
                item["name"]
                for item in inspector.get_check_constraints("sentinel_event_log")
            }
            assert "ck_sentinel_event_log_external_identity_scope" in checks
    finally:
        engine.dispose()


def test_cdc_migration_accepts_the_model_created_schema_without_drift():
    """A fresh DB reaches 0144 after 0003 create_all built the model tables.

    Migration 0003 materializes every registered model (including both CDC
    tables and the new sentinel_event_log columns/indexes) before 0144 runs
    on a fresh chain, so 0144 must verify — never re-create or reject — that
    exact shape.
    """
    cdc = _migration("2026_11_10_0144_integrated_cdc.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            from app.model_registry import import_all_models
            from app.database import Base

            import_all_models()
            Base.metadata.create_all(connection)
            with Operations.context(MigrationContext.configure(connection)):
                # Idempotent: the frozen migration only verifies the model shape.
                cdc.upgrade()
            inspector = sa.inspect(connection)
            assert inspector.has_table("v2_integrated_cdc_sources")
            assert inspector.has_table("v2_integrated_cdc_subscriptions")
            columns = {
                item["name"]
                for item in inspector.get_columns("sentinel_event_log")
            }
            assert set(CDC_COLUMNS) <= columns
    finally:
        engine.dispose()


def test_cdc_migration_downgrade_reupgrade_from_minimal_parents():
    """0144 must upgrade a pre-0144 database, then reverse and replay.

    The unique-index column list below pins the MIGRATION side exactly (it
    includes external_mapping_id); the model-side twin assertion lives in
    the create_all guard, so any model↔migration drift fails one of the two.
    """
    cdc = _migration("2026_11_10_0144_integrated_cdc.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            for name in ("users", "v2_connections", "v2_datasets",
                         "v2_integrated_assets", "ontology_projects"):
                sa.Table(name, parents, sa.Column("id", sa.String, primary_key=True))
            events = sa.Table(
                "sentinel_event_log", parents,
                sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
                sa.Column("ontology_id", sa.String, nullable=False),
                sa.Column("ontology_release_id", sa.String, nullable=True),
                sa.Column("object_type_id", sa.String, nullable=False),
                sa.Column("instance_id", sa.String, nullable=False),
                sa.Column("change_kind", sa.String(12), nullable=False),
                sa.Column("key", sa.String(255), nullable=False),
                sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            )
            parents.create_all(connection)
            connection.execute(events.insert().values(
                ontology_id="o1", ontology_release_id=None, object_type_id="t1",
                instance_id="i1", change_kind="updated", key="name",
                occurred_at=datetime(2026, 1, 1),
                created_at=datetime(2026, 1, 1)))
            with Operations.context(MigrationContext.configure(connection)):
                cdc.upgrade()
                inspector = sa.inspect(connection)
                columns = {
                    item["name"]
                    for item in inspector.get_columns("sentinel_event_log")
                }
                assert set(CDC_COLUMNS) <= columns
                indexes = {
                    item["name"]: item
                    for item in inspector.get_indexes("sentinel_event_log")
                }
                uq = indexes["uq_sentinel_event_log_external"]
                assert uq["unique"]
                assert uq["column_names"] == [
                    "ontology_id", "ontology_release_id", "external_asset_id",
                    "external_mapping_id", "object_type_id", "source_event_id",
                    "key",
                ]
                assert _partial_where(indexes, "uq_sentinel_event_log_external") \
                    == "source_event_id IS NOT NULL"
                assert _partial_where(
                    indexes, "ix_sentinel_event_log_external_group") == \
                    "event_group_id IS NOT NULL"
                # A remote-sourced row satisfies the identity-scope check.
                connection.execute(sa.text(
                    "INSERT INTO sentinel_event_log (ontology_id, ontology_release_id,"
                    " object_type_id, instance_id, source_event_id, event_group_id,"
                    " external_asset_id, external_mapping_id, change_kind, key,"
                    " occurred_at, created_at) VALUES"
                    " ('o1', 'r1', 't1', 'src1', 'se1', 'g1', 'a1', 'm1',"
                    " 'updated', 'name', '2026-01-02 00:00:00', '2026-01-02 00:00:00')"))

                cdc.downgrade()
                inspector = sa.inspect(connection)
                columns = {
                    item["name"]
                    for item in inspector.get_columns("sentinel_event_log")
                }
                assert not (set(CDC_COLUMNS) & columns)
                index_names = {
                    item["name"]
                    for item in inspector.get_indexes("sentinel_event_log")
                }
                assert "uq_sentinel_event_log_external" not in index_names
                assert "ix_sentinel_event_log_external_group" not in index_names
                # Downgrade deletes remote facts but keeps local ones.
                # The added source identity columns no longer exist after the
                # downgrade, so verify only that the original local event remains.
                remaining = connection.execute(sa.text(
                    "SELECT COUNT(*) FROM sentinel_event_log")).scalar_one()
                assert remaining == 1
                assert not inspector.has_table("v2_integrated_cdc_sources")
                assert not inspector.has_table("v2_integrated_cdc_subscriptions")

                # Re-upgrade restores the full partial-CDC shape and replays
                # cleanly (verify-only path).
                cdc.upgrade()
                cdc.upgrade()
                inspector = sa.inspect(connection)
                columns = {
                    item["name"]
                    for item in inspector.get_columns("sentinel_event_log")
                }
                assert set(CDC_COLUMNS) <= columns
                indexes = {
                    item["name"]: item
                    for item in inspector.get_indexes("sentinel_event_log")
                }
                assert indexes["uq_sentinel_event_log_external"]["unique"]
                assert _partial_where(indexes, "uq_sentinel_event_log_external") \
                    == "source_event_id IS NOT NULL"
                assert inspector.has_table("v2_integrated_cdc_sources")
                assert inspector.has_table("v2_integrated_cdc_subscriptions")
    finally:
        engine.dispose()


# ── CDC 控制表：列形状、状态 CHECK 词表与漂移拒绝 ────────────────────

SOURCE_COLUMNS = {
    "connection_id", "generation", "status", "identity_fingerprint",
    "last_position", "heartbeat_at", "cleanup_completed_at",
    "last_error_code", "last_event_at", "updated_at",
}
SUBSCRIPTION_COLUMNS = {
    "id", "connection_id", "asset_id", "ontology_id", "release_id",
    "mapping_id", "object_type_id", "field_map", "primary_key_columns",
    "contract", "schema_name", "table_name", "schema_fingerprint",
    "logical_fingerprint", "activation_position", "activated_at",
    "source_generation", "status", "last_error_code", "created_at",
    "updated_at",
}
CONTROL_CHECKS = {
    "v2_integrated_cdc_sources": {
        "ck_integrated_cdc_source_status",
        "ck_integrated_cdc_source_generation",
    },
    "v2_integrated_cdc_subscriptions": {"ck_integrated_cdc_subscription_status"},
}


def _minimal_parents(metadata):
    for name in ("users", "v2_connections", "v2_datasets",
                 "v2_integrated_assets", "ontology_projects"):
        sa.Table(name, metadata, sa.Column("id", sa.String, primary_key=True))


def _sources_table(metadata, *, fingerprint_column="identity_fingerprint",
                   status_sql=("status IN ('starting', 'active', 'paused', "
                               "'error', 'gap', 'stopped')"),
                   status_default="stopped", ondelete="RESTRICT",
                   with_status_check=True):
    elements = [
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(20), server_default=status_default, nullable=False),
        sa.Column(fingerprint_column, sa.String(64), nullable=True),
        sa.Column("last_position", sa.JSON(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["v2_connections.id"],
                                ondelete=ondelete),
        sa.PrimaryKeyConstraint("connection_id"),
        sa.CheckConstraint("generation >= 0",
                           name="ck_integrated_cdc_source_generation"),
    ]
    if with_status_check:
        elements.append(sa.CheckConstraint(
            status_sql, name="ck_integrated_cdc_source_status"))
    return sa.Table("v2_integrated_cdc_sources", metadata, *elements)


def test_cdc_control_tables_shape_and_status_vocabulary_on_create_all():
    """Model create_all must produce the frozen columns + status CHECKs, and
    the database must reject any status outside the frozen vocabulary."""
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            from app.model_registry import import_all_models
            from app.database import Base

            import_all_models()
            Base.metadata.create_all(connection)
            inspector = sa.inspect(connection)
            for table, expected in (
                ("v2_integrated_cdc_sources", SOURCE_COLUMNS),
                ("v2_integrated_cdc_subscriptions", SUBSCRIPTION_COLUMNS),
            ):
                actual = {c["name"] for c in inspector.get_columns(table)}
                assert actual == expected, table
                checks = {c["name"] for c in inspector.get_check_constraints(table)}
                assert checks == CONTROL_CHECKS[table], table
            connection.execute(sa.text(
                "INSERT INTO v2_connections (id, name, kind, config, status,"
                " created_at, updated_at) VALUES"
                " ('c1', 'n', 'postgres', '{}', 'inactive',"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
            # A frozen-vocabulary status is accepted ...
            connection.execute(sa.text(
                "INSERT INTO v2_integrated_cdc_sources (connection_id,"
                " generation, status, updated_at) VALUES"
                " ('c1', 0, 'gap', '2026-01-01 00:00:00')"))
            # ... while an invented one ('running') violates the CHECK.
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    "UPDATE v2_integrated_cdc_sources SET status = 'running'"
                    " WHERE connection_id = 'c1'"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    "UPDATE v2_integrated_cdc_sources SET generation = -1"
                    " WHERE connection_id = 'c1'"))
    finally:
        engine.dispose()


@pytest.mark.parametrize("drift", [
    "legacy_column",
    "vocabulary_drift",
    "negated_vocabulary",
    "wrong_check_column",
    "missing_check",
    "wrong_default",
    "fk_cascade",
])
def test_cdc_migration_rejects_drifted_control_table(drift):
    """0144 verifies — never silently accepts — a drifted sources table."""
    cdc = _migration("2026_11_10_0144_integrated_cdc.py")
    engine = sa.create_engine("sqlite://")
    vocabulary = "('starting', 'active', 'paused', 'error', 'gap', 'stopped')"
    kwargs = {
        "legacy_column": {"fingerprint_column": "config_fingerprint"},
        "vocabulary_drift": {"status_sql": (
            "status IN ('starting', 'active', 'running', 'error', 'gap', 'stopped')")},
        # Same literals, opposite meaning / different column: a literal-only
        # comparison would wrongly accept both.
        "negated_vocabulary": {"status_sql": f"status NOT IN {vocabulary}"},
        "wrong_check_column": {"status_sql": f"last_error_code IN {vocabulary}"},
        "missing_check": {"with_status_check": False},
        "wrong_default": {"status_default": "active"},
        "fk_cascade": {"ondelete": "CASCADE"},
    }[drift]
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _sources_table(parents, **kwargs).create(connection)
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError):
                    cdc.upgrade()
    finally:
        engine.dispose()


def test_cdc_migration_accepts_exact_control_shape_from_minimal_parents():
    """Sanity counterpart: the exact frozen shape passes verification."""
    cdc = _migration("2026_11_10_0144_integrated_cdc.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _sources_table(parents).create(connection)
            with Operations.context(MigrationContext.configure(connection)):
                # Sources table exists and matches: only subscriptions etc.
                # are created; upgrade must not raise.
                cdc.upgrade()
            inspector = sa.inspect(connection)
            assert inspector.has_table("v2_integrated_cdc_subscriptions")
    finally:
        engine.dispose()


def test_check_shape_is_structural_across_dialect_spellings():
    """The CHECK guard compares column + operator + ordered literals.

    The PostgreSQL strings below are hand-written in the form its deparser
    emits for a varchar IN-list (``= ANY (ARRAY[...])`` with per-literal
    casts); they exercise the normalizer without a live server. A real
    PostgreSQL replay of 0144 remains a separate environment check.
    """
    shape = _migration("2026_11_10_0144_integrated_cdc.py")._check_shape
    model = "status IN ('starting', 'active', 'superseded', 'stopped')"
    postgres = ("((status)::text = ANY ((ARRAY['starting'::character varying, "
                "'active'::character varying, 'superseded'::character varying, "
                "'stopped'::character varying])::text[]))")
    assert shape(postgres) == shape(model)
    # Negation, a different column, a reordered or changed vocabulary and a
    # case change inside a literal are all distinct shapes.
    assert shape(postgres.replace("= ANY", "<> ALL")) != shape(model)
    assert shape(postgres.replace("<> ALL", "= ANY").replace(
        "(status)", "(mapping_id)")) != shape(model)
    assert shape("status NOT IN ('starting', 'active', 'superseded', 'stopped')") \
        != shape(model)
    assert shape("mapping_id IN ('starting', 'active', 'superseded', 'stopped')") \
        != shape(model)
    assert shape("status IN ('active', 'starting', 'superseded', 'stopped')") \
        != shape(model)
    assert shape("status IN ('Starting', 'active', 'superseded', 'stopped')") \
        != shape(model)
    # Parenthesization/whitespace differences alone are not drift.
    assert shape("(generation >= 0)") == shape("generation >= 0")
    assert shape("generation > 0") != shape("generation >= 0")
