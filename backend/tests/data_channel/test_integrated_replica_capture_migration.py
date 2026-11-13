"""Focused guards for the integrated-replica capture migration (0148) on SQLite.

Frozen migration DDL / model shape guards; the PostgreSQL replay is a
separate environment check (same convention as the 0146/0147 guards).
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

CAPTURE_TABLE = "v2_integrated_replica_captures"
TRANSACTION_TABLE = "v2_integrated_replica_transactions"
RECEIPT_TABLE = "v2_integrated_replica_receipts"
CAPTURE_COLUMNS = {
    "id", "connection_id", "generation", "status", "identity_fingerprint",
    "tables_json", "received_seq", "committed_seq", "pending_bytes",
    "source_offset", "committed_position", "coverage_probe", "heartbeat_at",
    "last_error_code", "cleaned_at", "created_at", "updated_at",
}
TRANSACTION_COLUMNS = {
    "id", "capture_id", "source_transaction_id", "event_count", "commit_seq",
    "source_position", "source_committed_at", "created_at",
}
RECEIPT_COLUMNS = {
    "capture_id", "sequence", "transaction_id", "record_id", "ordinal",
    "envelope_json", "size_bytes", "received_at",
}
CAPTURE_CHECKS = {
    "ck_integrated_replica_capture_generation",
    "ck_integrated_replica_capture_status",
    "ck_integrated_replica_capture_progress",
}
TRANSACTION_CHECKS = {"ck_integrated_replica_transaction_rows"}
RECEIPT_CHECKS = {"ck_integrated_replica_receipt_size"}


def _migration(filename):
    specification = importlib.util.spec_from_file_location(
        "frozen_" + filename, ROOT / "alembic" / "versions" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _minimal_parents(metadata):
    sa.Table("v2_connections", metadata,
             sa.Column("id", sa.String, primary_key=True))


def _operations(connection):
    return Operations.context(MigrationContext.configure(connection))


def _enforce_foreign_keys(engine):
    @sa.event.listens_for(engine, "connect")
    def _hook(dbapi_connection, record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _insert_capture(connection, capture_id="cp1", connection_id="c1",
                    generation=1):
    connection.execute(sa.text(
        f"INSERT INTO {CAPTURE_TABLE} (id, connection_id, generation,"
        " identity_fingerprint, tables_json, created_at, updated_at)"
        " VALUES (:id, :connection_id, :generation, 'fp', '[]',"
        " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"),
        {"id": capture_id, "connection_id": connection_id,
         "generation": generation})


def _insert_transaction(connection, transaction_id="t1", capture_id="cp1",
                        source_transaction_id="stx1", commit_seq=None):
    connection.execute(sa.text(
        f"INSERT INTO {TRANSACTION_TABLE} (id, capture_id,"
        " source_transaction_id, commit_seq, created_at) VALUES"
        " (:id, :capture_id, :source_transaction_id, :commit_seq,"
        " '2026-01-01 00:00:00')"),
        {"id": transaction_id, "capture_id": capture_id,
         "source_transaction_id": source_transaction_id,
         "commit_seq": commit_seq})


def _insert_receipt(connection, capture_id="cp1", sequence=1,
                    transaction_id="t1", record_id="rec1", ordinal=1):
    connection.execute(sa.text(
        f"INSERT INTO {RECEIPT_TABLE} (capture_id, sequence, transaction_id,"
        " record_id, ordinal, envelope_json, size_bytes, received_at)"
        " VALUES (:capture_id, :sequence, :transaction_id, :record_id,"
        " :ordinal, '{}', 2, '2026-01-01 00:00:00')"),
        {"capture_id": capture_id, "sequence": sequence,
         "transaction_id": transaction_id, "record_id": record_id,
         "ordinal": ordinal})


def test_single_head_preserves_0148_on_top_of_0147():
    directory = ScriptDirectory(str(ROOT / "alembic"))
    heads = list(directory.get_heads())
    assert heads == [LATEST_HEAD], heads
    revision = directory.get_revision("0148_integrated_replica_capture")
    assert revision.down_revision == "0147_integrated_replicas"


def test_migration_accepts_the_model_created_schema_without_drift():
    """A fresh DB reaches 0148 after 0003 create_all built the model tables.

    Migration 0003 materializes every registered model (including all three
    capture tables) before 0148 runs on a fresh chain, so 0148 must verify —
    never re-create or reject — that exact shape.
    """
    migration = _migration("2026_11_12_0148_integrated_replica_capture.py")
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
            for table in (CAPTURE_TABLE, TRANSACTION_TABLE, RECEIPT_TABLE):
                assert inspector.has_table(table), table
            for table, name, columns in (
                (CAPTURE_TABLE, "ix_integrated_replica_captures_connection_status",
                 ["connection_id", "status"]),
                (TRANSACTION_TABLE, "ix_integrated_replica_transactions_pending",
                 ["capture_id", "commit_seq"]),
                (RECEIPT_TABLE, "ix_integrated_replica_receipts_transaction",
                 ["transaction_id", "ordinal"]),
            ):
                indexes = {item["name"]: item
                           for item in inspector.get_indexes(table)}
                assert indexes[name]["column_names"] == columns, name
    finally:
        engine.dispose()


def test_migration_creates_tables_from_minimal_parents_and_enforces_contract():
    """0148 must upgrade a pre-0148 database with the frozen shape, unique
    identities and vocabulary, under real foreign key enforcement."""
    migration = _migration("2026_11_12_0148_integrated_replica_capture.py")
    engine = sa.create_engine("sqlite://")
    _enforce_foreign_keys(engine)
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            connection.execute(sa.text(
                "INSERT INTO v2_connections (id) VALUES ('c1')"))
            with _operations(connection):
                migration.upgrade()
            inspector = sa.inspect(connection)
            assert ({item["name"] for item in inspector.get_columns(CAPTURE_TABLE)}
                    == CAPTURE_COLUMNS)
            assert ({item["name"]
                     for item in inspector.get_columns(TRANSACTION_TABLE)}
                    == TRANSACTION_COLUMNS)
            assert ({item["name"] for item in inspector.get_columns(RECEIPT_TABLE)}
                    == RECEIPT_COLUMNS)
            assert inspector.get_pk_constraint(CAPTURE_TABLE)[
                "constrained_columns"] == ["id"]
            assert inspector.get_pk_constraint(TRANSACTION_TABLE)[
                "constrained_columns"] == ["id"]
            # Receipts carry the composite primary key.
            assert inspector.get_pk_constraint(RECEIPT_TABLE)[
                "constrained_columns"] == ["capture_id", "sequence"]
            for table, expected in (
                (CAPTURE_TABLE,
                 {("connection_id", "generation")}),
                (TRANSACTION_TABLE,
                 {("capture_id", "source_transaction_id"),
                  ("capture_id", "commit_seq")}),
                (RECEIPT_TABLE,
                 {("capture_id", "record_id"),
                  ("transaction_id", "ordinal")}),
            ):
                actual = {tuple(item["column_names"])
                          for item in inspector.get_unique_constraints(table)}
                assert actual == expected, table
            for table, expected in (
                (CAPTURE_TABLE, CAPTURE_CHECKS),
                (TRANSACTION_TABLE, TRANSACTION_CHECKS),
                (RECEIPT_TABLE, RECEIPT_CHECKS),
            ):
                assert {item["name"]
                        for item in inspector.get_check_constraints(table)} \
                    == expected, table
            capture_fks = inspector.get_foreign_keys(CAPTURE_TABLE)
            assert capture_fks[0]["constrained_columns"] == ["connection_id"]
            assert capture_fks[0]["referred_table"] == "v2_connections"
            assert capture_fks[0]["options"] == {"ondelete": "RESTRICT"}
            receipt_fks = {
                item["constrained_columns"][0]: item
                for item in inspector.get_foreign_keys(RECEIPT_TABLE)
            }
            assert set(receipt_fks) == {"capture_id", "transaction_id"}
            assert receipt_fks["transaction_id"]["referred_table"] == \
                TRANSACTION_TABLE
            assert all(fk["options"] == {"ondelete": "RESTRICT"}
                       for fk in receipt_fks.values())

            _insert_capture(connection)
            _insert_transaction(connection)
            _insert_receipt(connection)
            # Frozen vocabulary and progress/size invariants are enforced.
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {CAPTURE_TABLE} SET status = 'running'"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {CAPTURE_TABLE} SET generation = 0"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {CAPTURE_TABLE} SET pending_bytes = -1"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {TRANSACTION_TABLE} SET event_count = -1"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {RECEIPT_TABLE} SET ordinal = 0"))
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(
                    f"UPDATE {RECEIPT_TABLE} SET size_bytes = 0"))
            # Unique identities: same source transaction, same record id and
            # same (transaction, ordinal) slot are all rejected.
            with pytest.raises(IntegrityError):
                _insert_transaction(connection, transaction_id="t2",
                                    source_transaction_id="stx1")
            with pytest.raises(IntegrityError):
                _insert_receipt(connection, sequence=2, record_id="rec1",
                                ordinal=2)
            with pytest.raises(IntegrityError):
                _insert_receipt(connection, sequence=3, record_id="rec3",
                                ordinal=1)
            # RESTRICT: captures with dependent data cannot be deleted.
            with pytest.raises(IntegrityError):
                connection.execute(sa.text(f"DELETE FROM {CAPTURE_TABLE}"))
    finally:
        engine.dispose()


def _captures_table(metadata, *, fingerprint_column="identity_fingerprint",
                    status_sql=("status IN ('starting', 'streaming', 'stale', "
                                "'gap', 'blocked', 'draining', 'stopped')"),
                    status_default="starting", ondelete="RESTRICT",
                    with_progress_check=True):
    elements = [
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), server_default=status_default,
                  nullable=False),
        sa.Column(fingerprint_column, sa.String(64), nullable=False),
        sa.Column("tables_json", sa.JSON(), nullable=False),
        sa.Column("received_seq", sa.BigInteger(), server_default="0",
                  nullable=False),
        sa.Column("committed_seq", sa.BigInteger(), server_default="0",
                  nullable=False),
        sa.Column("pending_bytes", sa.BigInteger(), server_default="0",
                  nullable=False),
        sa.Column("source_offset", sa.JSON(), nullable=True),
        sa.Column("committed_position", sa.JSON(), nullable=True),
        sa.Column("coverage_probe", sa.JSON(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["v2_connections.id"],
                                ondelete=ondelete),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "generation",
                            name="uq_integrated_replica_capture_generation"),
        sa.CheckConstraint("generation > 0",
                           name="ck_integrated_replica_capture_generation"),
        sa.CheckConstraint(status_sql,
                           name="ck_integrated_replica_capture_status"),
    ]
    if with_progress_check:
        elements.append(sa.CheckConstraint(
            "received_seq >= 0 AND committed_seq >= 0 AND pending_bytes >= 0",
            name="ck_integrated_replica_capture_progress"))
    return sa.Table(CAPTURE_TABLE, metadata, *elements)


@pytest.mark.parametrize("drift", [
    "legacy_column",
    "vocabulary_drift",
    "negated_vocabulary",
    "missing_check",
    "wrong_default",
    "fk_cascade",
])
def test_migration_rejects_drifted_capture_table(drift):
    """0148 verifies — never silently accepts — a drifted captures table."""
    migration = _migration("2026_11_12_0148_integrated_replica_capture.py")
    vocabulary = ("('starting', 'streaming', 'stale', 'gap', 'blocked', "
                  "'draining', 'stopped')")
    kwargs = {
        "legacy_column": {"fingerprint_column": "config_fingerprint"},
        "vocabulary_drift": {"status_sql": (
            "status IN ('starting', 'streaming', 'running', 'gap', "
            "'blocked', 'draining', 'stopped')")},
        # Same literals, opposite meaning: a literal-only comparison would
        # wrongly accept it.
        "negated_vocabulary": {"status_sql": f"status NOT IN {vocabulary}"},
        "missing_check": {"with_progress_check": False},
        "wrong_default": {"status_default": "streaming"},
        "fk_cascade": {"ondelete": "CASCADE"},
    }[drift]
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _captures_table(parents, **kwargs).create(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError):
                    migration.upgrade()
    finally:
        engine.dispose()


def test_migration_rejects_missing_unique_and_drifted_receipt_index():
    """A dropped transaction unique and a wrong-column receipt index drift."""
    migration = _migration("2026_11_12_0148_integrated_replica_capture.py")
    # Missing (capture_id, commit_seq) unique on transactions is drift.
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _captures_table(parents).create(connection)
            elements = [
                element for element in migration._transaction_elements()
                if getattr(element, "name", None)
                != "uq_integrated_replica_transaction_commit"]
            sa.Table(TRANSACTION_TABLE, parents, *elements).create(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError, match="unique"):
                    migration.upgrade()
    finally:
        engine.dispose()

    # Same-name receipt index on different columns is drift too.
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            _captures_table(parents).create(connection)
            sa.Table(TRANSACTION_TABLE, parents,
                     *migration._transaction_elements()).create(connection)
            receipts = sa.Table(RECEIPT_TABLE, parents,
                                *migration._receipt_elements())
            receipts.create(connection)
            sa.Index("ix_integrated_replica_receipts_transaction",
                     receipts.c.record_id).create(connection)
            with _operations(connection):
                with pytest.raises(RuntimeError, match="ix_integrated"):
                    migration.upgrade()
    finally:
        engine.dispose()


def test_downgrade_refuses_existing_data_and_only_drops_empty_tables():
    """Downgrade never deletes capture data; empty metadata tables drop."""
    migration = _migration("2026_11_12_0148_integrated_replica_capture.py")

    def _upgraded_engine():
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            parents = sa.MetaData()
            _minimal_parents(parents)
            parents.create_all(connection)
            with _operations(connection):
                migration.upgrade()
        return engine

    def _seed(connection, *, receipts=False, transactions=False,
              captures=False):
        if receipts or transactions or captures:
            connection.execute(sa.text(
                "INSERT INTO v2_connections (id) VALUES ('c1')"))
        if receipts or transactions or captures:
            _insert_capture(connection)
        if transactions or receipts:
            _insert_transaction(connection)
        if receipts:
            _insert_receipt(connection)

    # Any receipt / transaction / capture row blocks the downgrade; the
    # seeded data survives every refusal.
    for flag in ("receipts", "transactions", "captures"):
        engine = _upgraded_engine()
        try:
            with engine.begin() as connection:
                _seed(connection, **{flag: True})
                blocked = (RECEIPT_TABLE if flag == "receipts"
                           else TRANSACTION_TABLE if flag == "transactions"
                           else CAPTURE_TABLE)
                with _operations(connection):
                    with pytest.raises(RuntimeError, match=blocked):
                        migration.downgrade()
                assert connection.execute(sa.text(
                    f"SELECT COUNT(*) FROM {blocked}")).scalar_one() == 1
        finally:
            engine.dispose()

    # Verifiably empty tables downgrade, then the cycle replays.
    engine = _upgraded_engine()
    try:
        with engine.begin() as connection:
            with _operations(connection):
                migration.downgrade()
            inspector = sa.inspect(connection)
            for table in (RECEIPT_TABLE, TRANSACTION_TABLE, CAPTURE_TABLE):
                assert not inspector.has_table(table), table
            assert inspector.has_table("v2_connections")
            # Re-upgrade recreates the exact shape and verifies idempotently.
            with _operations(connection):
                migration.upgrade()
                migration.upgrade()
            inspector = sa.inspect(connection)
            for table in (RECEIPT_TABLE, TRANSACTION_TABLE, CAPTURE_TABLE):
                assert inspector.has_table(table), table
            with _operations(connection):
                migration.downgrade()
            assert not sa.inspect(connection).has_table(CAPTURE_TABLE)
    finally:
        engine.dispose()
