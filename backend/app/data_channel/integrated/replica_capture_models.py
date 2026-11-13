"""Durable, bounded replay receipts for current integrated replicas.

Capture generations have immutable table sets. A replacement can overlap its
predecessor while existing replicas keep applying; it never resets a live
stream's offsets to initialize a newly registered table.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def now():
    return datetime.now(timezone.utc)


class IntegratedReplicaCapture(Base):
    __tablename__ = "v2_integrated_replica_captures"
    __table_args__ = (
        UniqueConstraint("connection_id", "generation", name="uq_integrated_replica_capture_generation"),
        CheckConstraint("generation > 0", name="ck_integrated_replica_capture_generation"),
        CheckConstraint(
            "status IN ('starting', 'streaming', 'stale', 'gap', 'blocked', 'draining', 'stopped')",
            name="ck_integrated_replica_capture_status"),
        CheckConstraint("received_seq >= 0 AND committed_seq >= 0 AND pending_bytes >= 0",
                        name="ck_integrated_replica_capture_progress"),
        Index("ix_integrated_replica_captures_connection_status", "connection_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_connections.id", ondelete="RESTRICT"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="starting", server_default="starting")
    identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    tables_json: Mapped[list] = mapped_column(JSON, nullable=False)
    received_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    committed_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    pending_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    source_offset: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    committed_position: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    coverage_probe: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class IntegratedReplicaTransaction(Base):
    __tablename__ = "v2_integrated_replica_transactions"
    __table_args__ = (
        UniqueConstraint("capture_id", "source_transaction_id", name="uq_integrated_replica_source_transaction"),
        UniqueConstraint("capture_id", "commit_seq", name="uq_integrated_replica_transaction_commit"),
        CheckConstraint("event_count >= 0", name="ck_integrated_replica_transaction_rows"),
        Index("ix_integrated_replica_transactions_pending", "capture_id", "commit_seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    capture_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_integrated_replica_captures.id", ondelete="RESTRICT"), nullable=False)
    source_transaction_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    commit_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_position: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IntegratedReplicaReceipt(Base):
    __tablename__ = "v2_integrated_replica_receipts"
    __table_args__ = (
        UniqueConstraint("capture_id", "record_id", name="uq_integrated_replica_receipt_identity"),
        UniqueConstraint("transaction_id", "ordinal", name="uq_integrated_replica_receipt_ordinal"),
        CheckConstraint("ordinal > 0 AND size_bytes > 0", name="ck_integrated_replica_receipt_size"),
        Index("ix_integrated_replica_receipts_transaction", "transaction_id", "ordinal"),
    )

    capture_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_integrated_replica_captures.id", ondelete="RESTRICT"), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_integrated_replica_transactions.id", ondelete="RESTRICT"), nullable=False)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Exact JSON text: the platform JSON serializer must not round source
    # decimals. This is operational replay data, never sent to ordinary logs.
    envelope_json: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
