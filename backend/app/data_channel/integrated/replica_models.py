"""Current integrated replicas and their private, fenced initialization runs.

A run is operational progress, not a DatasetVersion. Only an atomically
published active_run_id exposes a relation to readers. Failed/replaced private
relations are cleaned by their run identity; existing lake_iv versions are not
part of this lifecycle.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def now():
    return datetime.now(timezone.utc)


class IntegratedReplica(Base):
    __tablename__ = "v2_integrated_replicas"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'initializing', 'catching_up', 'ready', "
            "'stale', 'rebuild_required', 'blocked', 'stopped')",
            name="ck_integrated_replica_status"),
        CheckConstraint("generation >= 0", name="ck_integrated_replica_generation"),
    )

    asset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_integrated_assets.id", ondelete="RESTRICT"), primary_key=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending")
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    # Run identities are fenced under this row's lock. Deliberately not FKs:
    # initialization runs refer back to this row and publication must not create
    # a circular insert/delete dependency.
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    pending_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A coverage observation is meaningful only after all preceding captured
    # transactions were applied. Receipt/heartbeat time is stored separately.
    applied_position: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coverage_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class IntegratedReplicaRun(Base):
    __tablename__ = "v2_integrated_replica_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'capturing', 'loading', 'catching_up', "
            "'active', 'retired', 'failed', 'cancelled')",
            name="ck_integrated_replica_run_status"),
        CheckConstraint("generation > 0", name="ck_integrated_replica_run_generation"),
        CheckConstraint("row_count >= 0", name="ck_integrated_replica_run_rows"),
        Index("ix_integrated_replica_runs_asset_status", "asset_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    asset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_integrated_replicas.asset_id", ondelete="RESTRICT"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued")
    schema_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0")
    capture_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    capture_start: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot_proof: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    applied_position: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Initialization ownership is separate from connection capture ownership.
    # Every write/transition rechecks this token and its lease under a row lock.
    owner_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
