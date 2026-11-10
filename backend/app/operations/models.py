"""运营报告册的持久化模型。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OperationsReport(Base):
    __tablename__ = "operations_reports"
    __table_args__ = (
        Index("ix_operations_reports_updated_at", "updated_at"),
        Index("ix_operations_reports_sort_order", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class OperationsReportArticle(Base):
    __tablename__ = "operations_report_articles"
    __table_args__ = (
        UniqueConstraint("report_id", "sort_order", name="uq_operations_report_articles_order"),
        Index("ix_operations_report_articles_report_order", "report_id", "sort_order"),
        Index("ix_operations_report_articles_dataset_id", "dataset_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    report_id: Mapped[str] = mapped_column(
        String, ForeignKey("operations_reports.id", ondelete="CASCADE"), nullable=False,
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_datasets.id", ondelete="RESTRICT"), nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
