"""integrated replica registration and fenced initialization runs

Revision ID: 0147_integrated_replicas
Revises: 0146_integrated_cdc
Create Date: 2026-11-12

Two replica lifecycle tables (per-asset current replica, per-generation
private initialization run).  A fresh chain reaches this revision after
migration 0003 already materialized every registered model, so each step
either creates the object or verifies the *exact* existing shape and raises
on any drift.  The verification helpers are the frozen ones defined by
migration 0146, loaded from its file instead of being copied into a second
parallel definition.

The downgrade refuses to run while any replica registration or run row
survives: those rows are the only durable record of active replicas and of
initialization runs whose physical relations may not be cleaned yet.  It
never deletes replica data and never scans sources; only verifiably empty
metadata tables are dropped.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0147_integrated_replicas"
down_revision = "0146_integrated_cdc"
branch_labels = None
depends_on = None

REPLICA_TABLE = "v2_integrated_replicas"
RUN_TABLE = "v2_integrated_replica_runs"
RUN_INDEX = "ix_integrated_replica_runs_asset_status"
REPLICA_STATUS_SQL = (
    "status IN ('pending', 'initializing', 'catching_up', 'ready', "
    "'stale', 'rebuild_required', 'blocked', 'stopped')")
RUN_STATUS_SQL = (
    "status IN ('queued', 'capturing', 'loading', 'catching_up', "
    "'active', 'retired', 'failed', 'cancelled')")

# Migration files are frozen history: reuse 0146's shape-verification
# helpers from its own file rather than duplicating them here.
_FROZEN_HELPERS = "2026_11_10_0146_integrated_cdc.py"


def _frozen():
    path = Path(__file__).resolve().parent / _FROZEN_HELPERS
    specification = importlib.util.spec_from_file_location(
        "frozen_0146_shape_helpers", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _replica_elements() -> tuple:
    return (
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(24), server_default="pending", nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        # Run identities are deliberately not FKs (see the model): the
        # migration mirrors that contract — no constraint is expected.
        sa.Column("active_run_id", sa.String(36), nullable=True),
        sa.Column("pending_run_id", sa.String(36), nullable=True),
        sa.Column("schema_fingerprint", sa.String(64), nullable=True),
        sa.Column("applied_position", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["v2_integrated_assets.id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.CheckConstraint(REPLICA_STATUS_SQL,
                           name="ck_integrated_replica_status"),
        sa.CheckConstraint("generation >= 0",
                           name="ck_integrated_replica_generation"),
    )


def _run_elements() -> tuple:
    return (
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), server_default="queued", nullable=False),
        sa.Column("schema_json", sa.JSON(), nullable=False),
        sa.Column("schema_fingerprint", sa.String(64), nullable=False),
        sa.Column("row_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("capture_id", sa.String(36), nullable=True),
        sa.Column("capture_start", sa.JSON(), nullable=True),
        sa.Column("snapshot_proof", sa.JSON(), nullable=True),
        sa.Column("applied_position", sa.JSON(), nullable=True),
        sa.Column("owner_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["asset_id"], ["v2_integrated_replicas.asset_id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(RUN_STATUS_SQL,
                           name="ck_integrated_replica_run_status"),
        sa.CheckConstraint("generation > 0",
                           name="ck_integrated_replica_run_generation"),
        sa.CheckConstraint("row_count >= 0",
                           name="ck_integrated_replica_run_rows"),
    )


def upgrade() -> None:
    helpers = _frozen()
    helpers._ensure_table(REPLICA_TABLE, *_replica_elements())
    helpers._ensure_table(RUN_TABLE, *_run_elements())
    helpers._ensure_index(RUN_INDEX, RUN_TABLE, ["asset_id", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, subject in ((RUN_TABLE, "initialization runs"),
                           (REPLICA_TABLE, "replica registrations")):
        if not inspector.has_table(table):
            continue
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"Cannot downgrade 0147_integrated_replicas: {table} still "
                f"holds {count} {subject}. Active replicas and runs with "
                "uncleaned physical resources must be retired and cleaned "
                "first; this downgrade never deletes replica data, never "
                "scans sources, and only drops verifiably empty metadata "
                "tables.")
    inspector = sa.inspect(bind)
    if inspector.has_table(RUN_TABLE):
        index_names = {item["name"] for item in inspector.get_indexes(RUN_TABLE)}
        if RUN_INDEX in index_names:
            op.drop_index(RUN_INDEX, table_name=RUN_TABLE)
        op.drop_table(RUN_TABLE)
    inspector = sa.inspect(bind)
    if inspector.has_table(REPLICA_TABLE):
        op.drop_table(REPLICA_TABLE)
