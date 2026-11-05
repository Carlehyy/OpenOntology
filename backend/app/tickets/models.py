"""
工单 (Tickets) — 数据模型

面向平台使用反馈的轻量工单：任何登录用户都可提交工单（反馈 Bug 或
不好用的体验），管理员在工单流水线上处理并留下必填评论。

三张表：
  - Ticket               工单主体（标题 + 反馈内容 + 提交人快照 + 状态）
  - TicketAttachment     附件（落盘 uploads_dir/tickets/<ticket_id>/，带 sha256）
  - TicketProgressLog    处理轨迹（管理员每次处理一行：状态迁移 + 必填评论）

状态机（业务语义，后端不做迁移限制，仅校验取值合法）：
  pending 待处理 → analyzing 分析中 → completed 已完成
  任意状态可转 cancelled 已取消
（历史存量行可能仍保留 verifying/accepted 取值，不做数据迁移）
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    String, Text, DateTime, Integer, ForeignKey, Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# —— 状态词汇（集中定义，service 层校验）——
STATUS_PENDING = "pending"        # 待处理：用户提交后的初始状态
STATUS_ANALYZING = "analyzing"    # 分析中：管理员已接手分析
STATUS_COMPLETED = "completed"    # 已完成：处理完毕
STATUS_CANCELLED = "cancelled"    # 已取消

TICKET_STATUSES = (
    STATUS_PENDING,
    STATUS_ANALYZING,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
)

# —— 分类词汇（用户反馈的来源类型；other 兼容历史数据与自由反馈）——
CATEGORY_SYSTEM_FAULT = "system_fault"  # 系统故障
CATEGORY_EXPERIENCE = "experience"      # 体验优化
CATEGORY_FEATURE = "feature"            # 新增功能
CATEGORY_OTHER = "other"                # 其他

TICKET_CATEGORIES = (
    CATEGORY_SYSTEM_FAULT,
    CATEGORY_EXPERIENCE,
    CATEGORY_FEATURE,
    CATEGORY_OTHER,
)


class Ticket(Base):
    """一条用户反馈工单。"""
    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_submitter_id", "submitter_id"),
        Index("ix_tickets_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    # —— 内容 ——
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(50), default=CATEGORY_OTHER)
    # 提交工单时用户所在页面的完整地址（含 hash 路由），供后续审查定位
    page_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # —— 提交人（快照提交时的用户名，提交人账号删除后仍可读）——
    submitter_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    submitter_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # —— 生命周期 ——
    status: Mapped[str] = mapped_column(String(20), default=STATUS_PENDING)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"
    __table_args__ = (Index("ix_ticket_attachments_ticket_id", "ticket_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        String, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 完整性校验
    uploaded_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TicketProgressLog(Base):
    """管理员处理轨迹。每次处理一行：状态迁移（可相同，即仅补充评论）+ 必填评论。"""
    __tablename__ = "ticket_progress_logs"
    __table_args__ = (Index("ix_ticket_progress_ticket_seq", "ticket_id", "seq"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        String, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=1)  # 每工单内单调递增

    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)  # 管理员必填评论
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
