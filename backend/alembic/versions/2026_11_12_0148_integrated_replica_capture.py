"""integrated replica capture plane: generations, transactions, receipts

Revision ID: 0148_integrated_replica_capture
Revises: 0147_integrated_replicas
Create Date: 2026-11-12

Three capture-plane tables for current integrated replicas: per-connection
capture generations (immutable table sets), bounded replay transactions and
their receipts.  A fresh chain reaches this revision after migration 0003
already materialized every registered model, so each step either creates
the object or verifies the *exact* existing shape and raises on any drift.
The verification helpers are the frozen ones defined by migration 0146,
loaded from its file instead of being copied into a parallel definition.

The downgrade refuses to run while any capture, transaction or receipt row
survives: those rows are the durable record of capture ownership, pending
replay data and physical capture resources that may not be cleaned yet.
It never deletes capture data and never scans sources; only verifiably
empty metadata tables are dropped.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0148_integrated_replica_capture"
down_revision = "0147_integrated_replicas"
branch_labels = None
depends_on = None

CAPTURE_TABLE = "v2_integrated_replica_captures"
TRANSACTION_TABLE = "v2_integrated_replica_transactions"
RECEIPT_TABLE = "v2_integrated_replica_receipts"
CAPTURE_INDEX = "ix_integrated_replica_captures_connection_status"
TRANSACTION_INDEX = "ix_integrated_replica_transactions_pending"
RECEIPT_INDEX = "ix_integrated_replica_receipts_transaction"
CAPTURE_STATUS_SQL = (
    "status IN ('starting', 'streaming', 'stale', 'gap', 'blocked', "
    "'draining', 'stopped')")

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


def _capture_elements() -> tuple:
    return (
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), server_default="starting",
                  nullable=False),
        sa.Column("identity_fingerprint", sa.String(64), nullable=False),
        sa.Column("tables_json", sa.JSON(), nullable=False),
        sa.Column("received_seq", sa.BigInteger(), server_default="0",
                  nullable=False),
        sa.Column("committed_seq", sa.BigInteger(), server_default="0",
                  nullable=False),
        sa.Column("pending_bytes", sa.BigInteger(), server_default="0",
                  nullable=False),
        sa.Column("source_offset", sa.JSON(), nullable=True),
        sa.Column("committed_position", sa.JSON(), nullable=True),
        # Bounded pending/confirmed source-coverage proof. Kept separate from
        # raw source_offset/committed_position so probe state never pollutes
        # connector resume coordinates or applied cursors.
        sa.Column("coverage_probe", sa.JSON(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["v2_connections.id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "generation",
                            name="uq_integrated_replica_capture_generation"),
        sa.CheckConstraint("generation > 0",
                           name="ck_integrated_replica_capture_generation"),
        sa.CheckConstraint(CAPTURE_STATUS_SQL,
                           name="ck_integrated_replica_capture_status"),
        sa.CheckConstraint(
            "received_seq >= 0 AND committed_seq >= 0 AND pending_bytes >= 0",
            name="ck_integrated_replica_capture_progress"),
    )


def _transaction_elements() -> tuple:
    return (
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("capture_id", sa.String(36), nullable=False),
        sa.Column("source_transaction_id", sa.String(200), nullable=False),
        sa.Column("event_count", sa.BigInteger(), server_default="0",
                  nullable=False),
        sa.Column("commit_seq", sa.BigInteger(), nullable=True),
        sa.Column("source_position", sa.JSON(), nullable=True),
        sa.Column("source_committed_at", sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["capture_id"],
                                ["v2_integrated_replica_captures.id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("capture_id", "source_transaction_id",
                            name="uq_integrated_replica_source_transaction"),
        sa.UniqueConstraint("capture_id", "commit_seq",
                            name="uq_integrated_replica_transaction_commit"),
        sa.CheckConstraint("event_count >= 0",
                           name="ck_integrated_replica_transaction_rows"),
    )


def _receipt_elements() -> tuple:
    return (
        sa.Column("capture_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("transaction_id", sa.String(36), nullable=False),
        sa.Column("record_id", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("envelope_json", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["capture_id"],
                                ["v2_integrated_replica_captures.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["transaction_id"],
                                ["v2_integrated_replica_transactions.id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("capture_id", "sequence"),
        sa.UniqueConstraint("capture_id", "record_id",
                            name="uq_integrated_replica_receipt_identity"),
        sa.UniqueConstraint("transaction_id", "ordinal",
                            name="uq_integrated_replica_receipt_ordinal"),
        sa.CheckConstraint("ordinal > 0 AND size_bytes > 0",
                           name="ck_integrated_replica_receipt_size"),
    )


def upgrade() -> None:
    helpers = _frozen()
    helpers._ensure_table(CAPTURE_TABLE, *_capture_elements())
    helpers._ensure_table(TRANSACTION_TABLE, *_transaction_elements())
    helpers._ensure_table(RECEIPT_TABLE, *_receipt_elements())
    helpers._ensure_index(CAPTURE_INDEX, CAPTURE_TABLE,
                          ["connection_id", "status"])
    helpers._ensure_index(TRANSACTION_INDEX, TRANSACTION_TABLE,
                          ["capture_id", "commit_seq"])
    helpers._ensure_index(RECEIPT_INDEX, RECEIPT_TABLE,
                          ["transaction_id", "ordinal"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, subject in (
        (RECEIPT_TABLE, "receipts"),
        (TRANSACTION_TABLE, "transactions"),
        (CAPTURE_TABLE, "captures"),
    ):
        if not inspector.has_table(table):
            continue
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"Cannot downgrade 0148_integrated_replica_capture: {table} "
                f"still holds {count} {subject}. Captures with pending "
                "transactions, receipts or uncleaned physical resources must "
                "be drained and cleaned first; this downgrade never deletes "
                "capture data, never scans sources, and only drops "
                "verifiably empty metadata tables.")
    inspector = sa.inspect(bind)
    if inspector.has_table(RECEIPT_TABLE):
        index_names = {item["name"]
                       for item in inspector.get_indexes(RECEIPT_TABLE)}
        if RECEIPT_INDEX in index_names:
            op.drop_index(RECEIPT_INDEX, table_name=RECEIPT_TABLE)
        op.drop_table(RECEIPT_TABLE)
    inspector = sa.inspect(bind)
    if inspector.has_table(TRANSACTION_TABLE):
        index_names = {item["name"]
                       for item in inspector.get_indexes(TRANSACTION_TABLE)}
        if TRANSACTION_INDEX in index_names:
            op.drop_index(TRANSACTION_INDEX, table_name=TRANSACTION_TABLE)
        op.drop_table(TRANSACTION_TABLE)
    inspector = sa.inspect(bind)
    if inspector.has_table(CAPTURE_TABLE):
        index_names = {item["name"]
                       for item in inspector.get_indexes(CAPTURE_TABLE)}
        if CAPTURE_INDEX in index_names:
            op.drop_index(CAPTURE_INDEX, table_name=CAPTURE_TABLE)
        op.drop_table(CAPTURE_TABLE)
