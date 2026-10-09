"""
消息通知 (Notifications) — 数据模型

以管理员为中心的消息总线：消息主体自带完整 Markdown 正文与附件，
每位用户对每条消息有独立的读/标记/归档状态（无状态行 = 全部默认否）。

三张表：
  - NotificationMessage        消息主体（标题 + Markdown 正文 + 优先级 + 来源）
  - NotificationMessageState   每用户处置状态（已读 / 已标记 / 已归档，惰性创建）
  - NotificationAttachment     附件（落盘 uploads_dir/notifications/<message_id>/，带 sha256）
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    # 与收件箱/运行库既有约定一致：持久化 naive UTC，序列化时补 Z。
    return datetime.utcnow()


# —— 词汇表（service 层校验，与收件箱优先级语义一致）——
PRIORITY_URGENT = "urgent"
PRIORITY_HIGH = "high"
PRIORITY_NORMAL = "normal"
PRIORITY_LOW = "low"

NOTIFICATION_PRIORITIES = (PRIORITY_URGENT, PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW)

# 消息来源类型：manual=管理员手动发送 / internal=平台内部事件 / ingest=外部系统投递
SOURCE_TYPE_MANUAL = "manual"
SOURCE_TYPE_INTERNAL = "internal"
SOURCE_TYPE_INGEST = "ingest"

NOTIFICATION_SOURCE_TYPES = (SOURCE_TYPE_MANUAL, SOURCE_TYPE_INTERNAL, SOURCE_TYPE_INGEST)


class NotificationMessage(Base):
    """一条通知消息。正文是事实本体，不是来源模块的投影。"""

    __tablename__ = "notification_messages"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('urgent','high','normal','low')",
            name="ck_notification_messages_priority",
        ),
        CheckConstraint(
            "source_type IN ('manual','internal','ingest')",
            name="ck_notification_messages_source_type",
        ),
        # 幂等键按来源系统作用域化：不同外部系统各自的 incident-1 互不冲突
        UniqueConstraint(
            "source_system", "event_id", name="uq_notification_messages_source_event"
        ),
        Index("ix_notification_messages_created", "created_at"),
        Index("ix_notification_messages_source", "source_system", "source_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 对外投递接口的幂等键（M2 启用）；站内手动/内部消息为 NULL
    event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    source_system: Mapped[str] = mapped_column(String(80), nullable=False, default="platform")
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default=SOURCE_TYPE_MANUAL)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default=PRIORITY_NORMAL)

    created_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 投递密钥归属（source_type=ingest 时绑定）：附件追加按此鉴权，防跨密钥注入
    ingest_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class NotificationMessageState(Base):
    """单用户对单条消息的处置状态；无行 = 未读、未标记、未归档。"""

    __tablename__ = "notification_message_states"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_notification_state_message_user"),
        Index("ix_notification_states_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notification_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    starred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class NotificationAttachment(Base):
    """消息附件（安全落盘 + sha256，与工单/事件附件同构）。"""

    __tablename__ = "notification_attachments"
    __table_args__ = (Index("ix_notification_attachments_message", "message_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notification_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class NotificationIngestKey(Base):
    """对外投递密钥。key_hash 存 sha256，明文（ob_notif_<tag>_<secret>）仅创建时返回一次。"""

    __tablename__ = "notification_ingest_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_notification_ingest_keys_hash"),
        Index("ix_notification_ingest_keys_enabled", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 密钥名 = 外部系统来源标识（投递消息未显式声明 sourceSystem 时以其兜底）
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 可选作用域：限定该密钥只能以某个 sourceSystem 名义投递
    allowed_source_system: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
