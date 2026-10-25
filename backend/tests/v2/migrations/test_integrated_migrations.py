"""Frozen migration DDL / baseline shape guards; PostgreSQL replay is separate E2E."""
import importlib.util
from pathlib import Path
from datetime import datetime, timezone
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[3]


def _migration(filename):
    specification = importlib.util.spec_from_file_location("frozen_" + filename, ROOT / "alembic" / "versions" / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _parents(connection):
    metadata = sa.MetaData()
    for name in ("users", "v2_connections", "v2_datasets", "v2_dataset_versions"):
        sa.Table(name, metadata, sa.Column("id", sa.String, primary_key=True))
    metadata.create_all(connection)


def test_frozen_integrated_migrations_replay_and_downgrade():
    assets = _migration("2026_10_24_0134_integrated_assets.py")
    query = _migration("2026_10_24_0135_unified_query_snapshots.py")
    recovery = _migration("2026_10_24_0136_integrated_sync_recovery.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _parents(connection)
            with Operations.context(MigrationContext.configure(connection)):
                assets.upgrade()
                query.upgrade()
                recovery.upgrade()
                # Replay is allowed only when every frozen shape check passes.
                assets.upgrade()
                query.upgrade()
                recovery.upgrade()
                tables = sa.inspect(connection).get_table_names()
                assert "v2_integrated_assets" in tables
                assert "v2_integrated_sync_recoveries" in tables
                recovery.downgrade()
                query.downgrade()
                assets.downgrade()
                assert "v2_integrated_assets" not in sa.inspect(connection).get_table_names()
                assert "v2_integrated_sync_recoveries" not in sa.inspect(connection).get_table_names()
                assets.upgrade()
                query.upgrade()
                recovery.upgrade()
    finally:
        engine.dispose()


def test_recovery_ledger_accepts_the_model_created_schema_without_drift():
    """A fresh DB reaches 0136 after 0003 create_all built the exact model table."""
    from app.data_channel.datasets.models import IntegratedSyncRecovery

    recovery = _migration("2026_10_24_0136_integrated_sync_recovery.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            IntegratedSyncRecovery.__table__.create(connection)
            indexes = {item["name"] for item in sa.inspect(connection).get_indexes(
                "v2_integrated_sync_recoveries")}
            assert "ix_integrated_sync_recoveries_recovered_at" in indexes
            with Operations.context(MigrationContext.configure(connection)):
                # Idempotent: the frozen migration only verifies the model shape.
                recovery.upgrade()
            assert "v2_integrated_sync_recoveries" in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()


def test_baseline_does_not_silently_accept_incompatible_existing_table():
    assets = _migration("2026_10_24_0134_integrated_assets.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _parents(connection)
            sa.Table("v2_integrated_assets", sa.MetaData(), sa.Column("id", sa.String, primary_key=True)).create(connection)
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="incompatible columns"):
                    assets.upgrade()
    finally:
        engine.dispose()


@pytest.mark.parametrize("drift", ["check_expression", "unique", "foreign_key", "nullability", "default", "index"])
def test_baseline_rejects_semantic_drift_not_only_column_names(drift):
    assets = _migration("2026_10_24_0134_integrated_assets.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _parents(connection)
            with Operations.context(MigrationContext.configure(connection)):
                assets.upgrade()
                inspector = sa.inspect(connection)
                real_inspect = assets.sa.inspect
                class DriftInspector:
                    def __getattr__(self, name):
                        return getattr(inspector, name)
                    def get_check_constraints(self, name):
                        values = inspector.get_check_constraints(name)
                        if drift == "check_expression" and values:
                            values[0] = {**values[0], "sqltext": "row_count < 0"}
                        return values
                    def get_unique_constraints(self, name):
                        return [] if drift == "unique" else inspector.get_unique_constraints(name)
                    def get_foreign_keys(self, name):
                        return [] if drift == "foreign_key" else inspector.get_foreign_keys(name)
                    def get_columns(self, name):
                        values = inspector.get_columns(name)
                        if drift == "nullability":
                            values[0] = {**values[0], "nullable": True}
                        if drift == "default":
                            values[0] = {**values[0], "default": "'unexpected'"}
                        return values
                    def get_indexes(self, name):
                        values = inspector.get_indexes(name)
                        if drift == "index" and values:
                            values[0] = {**values[0], "column_names": ["id"]}
                        return values
                # Only metadata introspection is doubled; frozen physical DDL is
                # real SQLite. Real PG replay/shape coverage belongs to the probe.
                assets.sa.inspect = lambda *_: DriftInspector()
                try:
                    with pytest.raises(RuntimeError, match="incompatible"):
                        assets.upgrade()
                finally:
                    assets.sa.inspect = real_inspect
    finally:
        engine.dispose()


def test_downgrade_drops_only_exact_manifest_owned_physical_relation():
    assets = _migration("2026_10_24_0134_integrated_assets.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _parents(connection)
            with Operations.context(MigrationContext.configure(connection)):
                assets.upgrade()
                owned = str(uuid.uuid4())
                unrelated = "lake_iv_" + uuid.uuid4().hex
                physical = "lake_iv_" + uuid.UUID(owned).hex
                metadata = sa.MetaData()
                sa.Table(physical, metadata, sa.Column("id", sa.Integer))
                sa.Table(unrelated, metadata, sa.Column("id", sa.Integer))
                metadata.create_all(connection)
                runs = sa.Table("v2_integrated_sync_runs", sa.MetaData(), autoload_with=connection)
                now = datetime.now(timezone.utc)
                connection.execute(runs.insert().values(id=owned, dataset_id="d", asset_id="a", status="failed",
                                   physical_table=physical, schema_json={}, snapshot_json={}, expected_rows=0,
                                   row_count=0, created_at=now, updated_at=now))
                assets.downgrade()
                assert not sa.inspect(connection).has_table(physical)
                assert sa.inspect(connection).has_table(unrelated)
    finally:
        engine.dispose()
