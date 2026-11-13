"""Batch-level snapshot receipts for native Debezium initial snapshots.

A replica run's initial snapshot is COPY-loaded per HTTP batch. Losing the
ACK of an applied batch makes the source redeliver the same rows, so each
batch leaves at most one receipt per run: a redelivered batch collides on
the composite primary key and is skipped instead of persisting per-row
receipts for multi-million-row snapshots. ``batch_id`` is the SHA256 digest
of the batch's ordered raw snapshot row content (processing timestamps
excluded), computed by the snapshot ingest path, so only byte-identical
batches deduplicate. Receipts are candidates rebuilt from scratch when a
run restarts its initial snapshot and are deleted once the run is retired
or failed. The table stores no business data — only receipt identity and
size.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def now():
    return datetime.now(timezone.utc)


class IntegratedReplicaSnapshotBatch(Base):
    __tablename__ = "v2_integrated_replica_snapshot_batches"
    __table_args__ = (
        CheckConstraint("row_count > 0",
                        name="ck_integrated_replica_snapshot_batch_rows"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_integrated_replica_runs.id", ondelete="RESTRICT"),
        primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
