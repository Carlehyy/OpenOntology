"""Fixed, expiring result snapshots; migrations are owned by the composition team."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.database import Base


class UnifiedQuerySnapshot(Base):
    __tablename__ = "v2_unified_query_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), index=True)
    query_hash: Mapped[str] = mapped_column(String(64))
    auth_fingerprint: Mapped[str] = mapped_column(String(64))
    schema_fingerprint: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSON)
    columns: Mapped[list] = mapped_column(JSON)
    source_states: Mapped[list] = mapped_column(JSON)
    consistency: Mapped[str] = mapped_column(String(40))
    plan: Mapped[dict] = mapped_column(JSON)
    result_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    total_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    result_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    page_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class UnifiedQueryRow(Base):
    __tablename__ = "v2_unified_query_rows"

    snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_unified_query_snapshots.id", ondelete="CASCADE"),
        primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    row: Mapped[dict] = mapped_column(JSON)
