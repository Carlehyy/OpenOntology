"""Stable virtual assets and durable single-source mutation receipts."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def now():
    return datetime.now(timezone.utc)


class IntegratedAsset(Base):
    __tablename__ = "v2_integrated_assets"
    __table_args__ = (
        UniqueConstraint("connection_id", "schema_name", "table_name", name="uq_integrated_asset_resource"),
        UniqueConstraint("dataset_id", name="uq_integrated_asset_dataset"),
        Index("ix_integrated_assets_connection", "connection_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id: Mapped[str] = mapped_column(String, ForeignKey("v2_datasets.id", ondelete="RESTRICT"), nullable=False)
    connection_id: Mapped[str] = mapped_column(String, ForeignKey("v2_connections.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    writable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class IntegratedMutation(Base):
    __tablename__ = "v2_integrated_mutations"
    __table_args__ = (
        UniqueConstraint("asset_id", "actor_id", "idempotency_key", name="uq_integrated_mutation_key"),
        Index("ix_integrated_mutations_asset_created", "asset_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    asset_id: Mapped[str] = mapped_column(String, ForeignKey("v2_integrated_assets.id", ondelete="RESTRICT"), nullable=False)
    actor_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(10), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Never persist connection configuration, source rows or supplied values.
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    affected_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
