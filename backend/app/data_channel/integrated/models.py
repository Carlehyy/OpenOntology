"""Stable virtual assets, optional logical projections, mutation receipts."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    # Optional one-to-one logical projection; physical schema_json stays the
    # pristine identity and never carries presentation config.
    field_config_row: Mapped["IntegratedAssetFieldConfig"] = relationship(
        uselist=False, cascade="all, delete-orphan")


class IntegratedAssetFieldConfig(Base):
    """One-to-one logical field projection of a registered integrated asset.

    ``field_config`` holds the validated normalized entries
    (``[{source_key, name, display_name}]``).  Types, nullability and primary
    key membership are always physical truth on the asset; this row only
    carries addressing/display metadata so schema refreshes can replace
    ``schema_json`` wholesale without ever risking config loss.
    """
    __tablename__ = "v2_integrated_asset_field_configs"

    asset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_integrated_assets.id", ondelete="RESTRICT"), primary_key=True)
    field_config: Mapped[list] = mapped_column(JSON, nullable=False)
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


class IntegratedCdcSource(Base):
    """Desired/observed CDC runtime for one managed database connection.

    Control-plane only: reconcile writes the desired side (``starting`` /
    ``paused`` / new ``generation`` epochs), the runtime reports the observed
    side (``active`` / ``error`` / ``gap`` / ``stopped`` + ``heartbeat_at``).
    ``identity_fingerprint`` is a one-way digest of the *physical* endpoint
    identity (host/port/database, credentials excluded) — never plaintext or
    ciphertext of the connection config. ``last_position`` is the runtime's
    last applied stream coordinate, updated atomically in the same transaction
    as the capture facts/outbox (dedupe survives event-log/outbox pruning);
    retained across restarts and credential-only rotation, reset only on
    explicit generation boundaries. ``cleanup_completed_at`` is the runtime's
    observed proof that the replication slot was dropped; a stopped source
    without it must block connection decommission/repoint.
    """
    __tablename__ = "v2_integrated_cdc_sources"
    __table_args__ = (
        # Frozen lifecycle vocabulary: no writer may invent a state.
        CheckConstraint(
            "status IN ('starting', 'active', 'paused', 'error', 'gap', 'stopped')",
            name="ck_integrated_cdc_source_status"),
        CheckConstraint("generation >= 0", name="ck_integrated_cdc_source_generation"),
    )

    connection_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_connections.id", ondelete="RESTRICT"), primary_key=True)
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    # starting | active | paused | error | gap | stopped
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="stopped", server_default="stopped")
    identity_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_position: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class IntegratedCdcSubscription(Base):
    """A released remote object mapping that authorizes one table/field stream.

    ``field_map`` maps PHYSICAL source column names to ontology property names;
    ``primary_key_columns`` are PHYSICAL columns. ``contract`` freezes the
    published column/property contract the stream was authorized under.
    ``activation_position`` is the runtime-anchored coverage frontier (None =
    anchor at the next activation, i.e. explicit no-data coverage start) and
    ``activated_at`` the wall-clock moment that anchor was taken (the real
    coverage start; None while unanchored). Both are retained across restarts
    and table include-set changes, and only ever cleared together through an
    explicit new-coverage transition. Statuses:
    ``starting`` (desired, awaiting runtime activation), ``active`` (runtime
    streaming), ``superseded`` (desired removed, awaiting runtime stop ack),
    ``stopped`` (runtime acknowledged stop; safe to clean control rows).
    """
    __tablename__ = "v2_integrated_cdc_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "ontology_id", "release_id", "mapping_id",
            name="uq_integrated_cdc_subscription_release_mapping"),
        CheckConstraint(
            "status IN ('starting', 'active', 'superseded', 'stopped')",
            name="ck_integrated_cdc_subscription_status"),
        Index("ix_integrated_cdc_subscriptions_connection", "connection_id", "status"),
        Index("ix_integrated_cdc_subscriptions_asset", "asset_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_connections.id", ondelete="RESTRICT"), nullable=False)
    asset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_integrated_assets.id", ondelete="RESTRICT"), nullable=False)
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    release_id: Mapped[str] = mapped_column(String, nullable=False)
    mapping_id: Mapped[str] = mapped_column(String, nullable=False)
    object_type_id: Mapped[str] = mapped_column(String, nullable=False)
    field_map: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    primary_key_columns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    contract: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activation_position: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    # starting | active | superseded | stopped
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="starting", server_default="starting")
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
