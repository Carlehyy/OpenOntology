import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, Text, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class PostCommitVerificationError(RuntimeError):
    """受控动作候选/编辑事务已提交但回执读回失败：按 unknown 人工对账。"""

    pass


class CuratedDataset(Base):
    __tablename__ = "v2_curated_datasets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_pipelines.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latest_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending_review")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class CuratedReview(Base):
    __tablename__ = "v2_curated_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    curated_dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_datasets.id", ondelete="RESTRICT"), nullable=False)
    # 审批背书的是不可变数据版本，而不是会持续追加版本的数据集逻辑 ID。
    # nullable=True 仅为兼容迁移前的历史审批；新审批一律绑定最新版本。
    dataset_version_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_dataset_versions.id", ondelete="RESTRICT"),
        nullable=True, index=True)
    reviewer_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    # 受控动作候选：base_version_id=已批准不可变基线（approve 时 CAS：
    # 当前指针必须仍指向 base）；proposal_key=proposal-write:{confirmation_id}
    # 持久唯一关联（幂等与只读恢复锚点）。流水线审核两列恒 NULL。
    base_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    proposal_key: Mapped[str | None] = mapped_column(String(255), nullable=True,
                                                     index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|partial
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class CuratedRowEdit(Base):
    __tablename__ = "v2_curated_row_edits"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    review_id: Mapped[str] = mapped_column(String, ForeignKey("v2_curated_reviews.id", ondelete="CASCADE"), nullable=False)
    # Composite identities use a canonical JSON payload and may legitimately
    # exceed 200 characters (multiple UUID/business-key components).  Truncating
    # here would make review edits address a different row.
    row_pk: Mapped[str] = mapped_column(Text, nullable=False)
    field_name: Mapped[str] = mapped_column(String(200), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
