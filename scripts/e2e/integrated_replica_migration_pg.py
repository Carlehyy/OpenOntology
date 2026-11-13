#!/usr/bin/env python3
r"""Seed/verify helper for the real-PG Alembic acceptance of 0147/0148/0149.

Runs inside the canary image against the isolated fixture network
(oo-replica-spike-20261112, target-pg:5432). Owns nothing but two probe
databases: replica_migration_fresh / replica_migration_existing. It never
touches replica_acceptance, replica_store_probe, or any production URL —
DATABASE_URL is asserted to be one of the two probe databases on target-pg
before any app import.

The Alembic invocations themselves are driven from the host as real
`python3 -m alembic` commands (see .artifacts/replica-migrations-pg/); this
helper only seeds sanitized fixture rows and verifies post-migration state.

Subcommands:
  verify-fresh      alembic_version == 0149 head; 0147/0148/0149 tables exist
  seed-existing     user/connection/dataset/asset rows (sanitized fixtures)
  verify-existing   seeded rows intact after upgrade head; version == 0149
  seed-batch        replica + run + snapshot batch receipt (raw SQL, the
                    migration's own tables) to arm the 0149 downgrade guard
  verify-reject     after refused downgrade: version still 0149, table intact
  drain-batch       delete receipt + run + replica rows (cleanup between legs)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

_ALLOWED_DBS = {"replica_migration_fresh", "replica_migration_existing"}
_url = os.environ.get("DATABASE_URL", "")
_ok = (_url.startswith("postgresql://")
       and "@target-pg:5432/" in _url
       and _url.rsplit("/", 1)[-1] in _ALLOWED_DBS)
if not _ok:
    sys.exit(f"refusing to run: DATABASE_URL must target target-pg:5432/"
             f"replica_migration_fresh|replica_migration_existing (got {_url!r})")

import sqlalchemy as sa

HEAD = "0149_integrated_replica_snapshot"
NEW_TABLES = [
    "v2_integrated_replicas",              # 0147
    "v2_integrated_replica_runs",          # 0147
    "v2_integrated_replica_captures",      # 0148
    "v2_integrated_replica_transactions",  # 0148
    "v2_integrated_replica_receipts",      # 0148
    "v2_integrated_replica_snapshot_batches",  # 0149
]
BATCH_TABLE = "v2_integrated_replica_snapshot_batches"
RUN_TABLE = "v2_integrated_replica_runs"
REPLICA_TABLE = "v2_integrated_replicas"

# Sanitized, deterministic fixture identities (no secrets, no real names).
USER_ID = "0a111111-1111-4111-8111-111111111111"
CONN_ID = "0b222222-2222-4222-8222-222222222222"
DATASET_ID = "0c333333-3333-4333-8333-333333333333"
ASSET_ID = "0d444444-4444-4444-8444-444444444444"
RUN_ID = "0e555555-5555-4555-8555-555555555555"
BATCH_ID = "batch-0001"

engine = sa.create_engine(_url)


def version(conn) -> str:
    return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


def table_exists(conn, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


def seed_business_rows() -> dict:
    """ORM-seed the minimal FK chain user → connection → dataset → asset."""
    from app.auth.models import User
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.integrated.models import IntegratedAsset

    # NOTE: deliberately no Base.metadata.create_all — the schema must come
    # from the Alembic chain only, otherwise the create branch of 0147..0149
    # would be silently replaced by their verify branch.
    with sa.orm.Session(engine) as session:
        user = User(id=USER_ID, username="migration_probe", email="probe@fixture.invalid",
                    password_hash="fixture-not-a-real-hash")
        conn = Connection(id=CONN_ID, name="migration-probe-source", kind="postgres",
                          config={"host": "target-pg", "database": "fixture"})
        dataset = Dataset(id=DATASET_ID, name="migration_probe_dataset", kind="structured")
        asset = IntegratedAsset(
            id=ASSET_ID, dataset_id=DATASET_ID, connection_id=CONN_ID,
            name="migration_probe_asset", schema_name="public", table_name="probe_src",
            schema_json={"columns": [{"name": "id", "type": "integer"}],
                         "primary_key": ["id"]},
            schema_fingerprint="ab" * 32, created_by=USER_ID)
        for row in (user, conn, dataset, asset):
            session.merge(row)
        session.commit()
    return {"user": USER_ID, "connection": CONN_ID, "dataset": DATASET_ID,
            "asset": ASSET_ID}


def main() -> int:
    phase = sys.argv[1]
    out: dict = {"phase": phase, "database": _url.rsplit("/", 1)[-1]}

    if phase == "verify-fresh":
        with engine.connect() as conn:
            out["version"] = version(conn)
            out["new_tables"] = {t: table_exists(conn, t) for t in NEW_TABLES}
            cols = [r.column_name for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = :t ORDER BY ordinal_position"), {"t": BATCH_TABLE})]
            out["batch_columns"] = cols
            out["ok"] = (out["version"] == HEAD and all(out["new_tables"].values())
                         and cols == ["run_id", "batch_id", "row_count", "created_at"])
    elif phase == "seed-existing":
        out.update(seed_business_rows())
        with engine.connect() as conn:
            out["counts"] = {t: conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one()
                             for t in ("users", "v2_connections", "v2_datasets",
                                       "v2_integrated_assets")}
        out["ok"] = all(c == 1 for c in out["counts"].values())
    elif phase == "verify-existing":
        with engine.connect() as conn:
            out["version"] = version(conn)
            out["seed_rows_intact"] = {
                "user": conn.execute(sa.text(
                    "SELECT username FROM users WHERE id = :i"), {"i": USER_ID}).scalar_one_or_none(),
                "connection": conn.execute(sa.text(
                    "SELECT name FROM v2_connections WHERE id = :i"), {"i": CONN_ID}).scalar_one_or_none(),
                "dataset": conn.execute(sa.text(
                    "SELECT name FROM v2_datasets WHERE id = :i"), {"i": DATASET_ID}).scalar_one_or_none(),
                "asset": conn.execute(sa.text(
                    "SELECT schema_fingerprint FROM v2_integrated_assets WHERE id = :i"),
                    {"i": ASSET_ID}).scalar_one_or_none(),
            }
            out["replica_tables_present"] = all(table_exists(conn, t) for t in NEW_TABLES)
            out["ok"] = (out["version"] == HEAD and out["replica_tables_present"]
                         and out["seed_rows_intact"] == {
                             "user": "migration_probe",
                             "connection": "migration-probe-source",
                             "dataset": "migration_probe_dataset",
                             "asset": "ab" * 32})
    elif phase == "seed-batch":
        now = datetime.now(timezone.utc).isoformat()
        with engine.begin() as conn:
            conn.execute(sa.text(f"DELETE FROM {BATCH_TABLE} WHERE run_id = :r"), {"r": RUN_ID})
            conn.execute(sa.text(f"DELETE FROM {RUN_TABLE} WHERE id = :r"), {"r": RUN_ID})
            conn.execute(sa.text(f"DELETE FROM {REPLICA_TABLE} WHERE asset_id = :a"), {"a": ASSET_ID})
            conn.execute(sa.text(
                f"INSERT INTO {REPLICA_TABLE} (asset_id, status, generation, created_at, updated_at)"
                " VALUES (:a, 'ready', 1, :t, :t)"), {"a": ASSET_ID, "t": now})
            conn.execute(sa.text(
                f"INSERT INTO {RUN_TABLE} (id, asset_id, generation, status, schema_json,"
                " schema_fingerprint, created_at)"
                " VALUES (:r, :a, 1, 'active', '{}', :fp, :t)"),
                {"r": RUN_ID, "a": ASSET_ID, "fp": "cd" * 32, "t": now})
            conn.execute(sa.text(
                f"INSERT INTO {BATCH_TABLE} (run_id, batch_id, row_count, created_at)"
                " VALUES (:r, :b, 1000, :t)"), {"r": RUN_ID, "b": BATCH_ID, "t": now})
        with engine.connect() as conn:
            out["receipts"] = conn.execute(sa.text(
                f"SELECT count(*) FROM {BATCH_TABLE}")).scalar_one()
        out["ok"] = out["receipts"] == 1
    elif phase == "verify-reject":
        with engine.connect() as conn:
            out["version"] = version(conn)
            out["batch_table_present"] = table_exists(conn, BATCH_TABLE)
            out["receipts"] = conn.execute(sa.text(
                f"SELECT count(*) FROM {BATCH_TABLE}")).scalar_one()
        out["ok"] = (out["version"] == HEAD and out["batch_table_present"]
                     and out["receipts"] == 1)
    elif phase == "drain-batch":
        with engine.begin() as conn:
            conn.execute(sa.text(f"DELETE FROM {BATCH_TABLE} WHERE run_id = :r"), {"r": RUN_ID})
            conn.execute(sa.text(f"DELETE FROM {RUN_TABLE} WHERE id = :r"), {"r": RUN_ID})
            conn.execute(sa.text(f"DELETE FROM {REPLICA_TABLE} WHERE asset_id = :a"), {"a": ASSET_ID})
        with engine.connect() as conn:
            out["replicas_left"] = conn.execute(sa.text(
                f"SELECT count(*) FROM {REPLICA_TABLE}")).scalar_one()
        out["ok"] = out["replicas_left"] == 0
    else:
        sys.exit(f"unknown phase {phase!r}")

    engine.dispose()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
