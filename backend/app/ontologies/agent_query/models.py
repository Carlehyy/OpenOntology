"""统一查询的有界结果快照 — 稳定分页的事实源。

首请求把受预算限制的完整结果物化进 ``fo_agent_query_rows``，后续页仅按
ordinal 读取；页间业务数据变化不会造成跨页漏行/重行（新查询才看到新数据）。
快照只保存获准投影列，不复制完整 properties；TTL 由清理路径物理删除。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentQuerySnapshot(Base):
    """一次查询执行的元数据与授权绑定。"""

    __tablename__ = "fo_agent_query_snapshots"
    __table_args__ = (
        Index(
            "ix_fo_agent_query_snapshots_owner_expires",
            "owner_user_id",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    session_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)

    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    releases: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    order_spec: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    columns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    citations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    page_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    observed_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AgentQueryRow(Base):
    """快照中的单行（ordinal 即稳定全序位次）。"""

    __tablename__ = "fo_agent_query_rows"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "ordinal", name="uq_fo_agent_query_row_ordinal"),
    )

    snapshot_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("fo_agent_query_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    row: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
