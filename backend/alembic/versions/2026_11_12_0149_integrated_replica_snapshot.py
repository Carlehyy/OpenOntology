"""integrated replica snapshot batch receipts

Revision ID: 0149_integrated_replica_snapshot
Revises: 0148_integrated_replica_capture
Create Date: 2026-11-12

One batch-receipt table for native Debezium initial snapshots: each HTTP
snapshot batch of a replica initialization run leaves at most one receipt,
so a lost ACK and the resulting same-batch redelivery resolve idempotently
on the composite primary key instead of persisting per-row receipts for
multi-million-row snapshots.  The table stores no business data, only the
receipt identity and size.  A fresh chain reaches this revision after
migration 0003 already materialized every registered model, so this step
either creates the object or verifies the *exact* existing shape and raises
on any drift.  The verification helpers are the frozen ones defined by
migration 0146, loaded from its file instead of being copied into a
parallel definition.

The downgrade refuses to run while any snapshot batch receipt survives:
receipts of runs that are not yet retired/failed may still be needed to
deduplicate a redelivered batch.  It never deletes snapshot data and never
scans sources; only a verifiably empty metadata table is dropped.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0149_integrated_replica_snapshot"
down_revision = "0148_integrated_replica_capture"
branch_labels = None
depends_on = None

BATCH_TABLE = "v2_integrated_replica_snapshot_batches"

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


def _batch_elements() -> tuple:
    return (
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["v2_integrated_replica_runs.id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("run_id", "batch_id"),
        sa.CheckConstraint("row_count > 0",
                           name="ck_integrated_replica_snapshot_batch_rows"),
    )


def upgrade() -> None:
    helpers = _frozen()
    helpers._ensure_table(BATCH_TABLE, *_batch_elements())


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(BATCH_TABLE):
        count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {BATCH_TABLE}")).scalar_one()
        if count:
            raise RuntimeError(
                f"Cannot downgrade 0149_integrated_replica_snapshot: "
                f"{BATCH_TABLE} still holds {count} snapshot batch receipts. "
                "Runs whose receipts were not yet cleaned by retirement or "
                "failure must be drained and cleaned first; this downgrade "
                "never deletes snapshot data, never scans sources, and only "
                "drops a verifiably empty metadata table.")
    inspector = sa.inspect(bind)
    if inspector.has_table(BATCH_TABLE):
        op.drop_table(BATCH_TABLE)
