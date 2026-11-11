"""超级助手跨会话持久记忆（Hindsight 集成）的数据模型。

与旧链路 ``SuperAssistantMemory``（``memory_service.py`` 的 TF-IDF 表）完全
隔离：旧数据不迁移，本链路不读写旧表。设计要点：

- ``SuperAssistantMemorySource`` 是独立的安全快照 + 同步任务一体行：
  - 快照侧保存 role/content/context/occurred_at/explicit/correction_of，
    对 conversation/message 无外键（删会话保留来源与已沉淀记忆），
    仅 owner 外键级联（删用户连记忆一起清理）；
  - 任务侧保存 status/attempt/backoff/租约/operation_id/document_id，
    天然与消息写入同事务（enqueue 只 flush 不 commit）。
- ``SuperAssistantMemorySetting`` 是每用户开关与记忆模型选择。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SuperAssistantMemorySource(Base):
    """记忆来源快照与上游同步状态机。

    状态机（status）：

    - ``pending``：已入队等待派发/认领；
    - ``processing``：worker 已认领（租约 lease_expires_at 防多 worker 竞争）；
    - ``synced``：上游 retain 完成（document_id/operation_id 留档）；
    - ``failed``：本次尝试失败，next_attempt_at 到点自动重试（指数退避）；
    - ``dead``：attempt 耗尽，等待人工 POST /memory/sources/{id}/retry；
    - ``forgetting``：用户删除（tombstone 先落），等上游异步 operation
      完成后删除 document 并核实；
    - ``forgotten``：上游删除核实完成。终态 tombstone，行保留防复活。

    唯一约束 (owner_id, source_message_id, version)：消息源用 message_id 作
    source_message_id（重复入队幂等）；人工源/更正用新生成 uuid；更正共享被
    更正来源的 source_message_id 且 version+1（历史追加不覆盖）。
    """

    __tablename__ = "super_assistant_memory_sources"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "source_message_id", "version",
            name="uq_sa_memory_sources_owner_msg_version",
        ),
        Index("ix_sa_memory_sources_owner_status", "owner_id", "status"),
        Index("ix_sa_memory_sources_owner_created", "owner_id", "created_at"),
        Index("ix_sa_memory_sources_status_lease", "status", "lease_expires_at"),
        Index("ix_sa_memory_sources_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    # 消息源 = message_id；人工源 = 生成 uuid；更正 = 沿用被更正来源的值
    source_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # user | assistant（上游无 role 字段，retain 时写入 metadata/context 标注）
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    explicit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    correction_of: Mapped[str | None] = mapped_column(String, nullable=True)
    # 快照引用，无外键：删会话/消息不级联删除来源
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- 同步任务侧字段 ---
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 认领令牌：worker 侧一切状态转移都以 (status, lease_token) 做 CAS
    # 栅栏，防 stale worker 把并发 forgetting 覆写回 pending/synced
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 本次已提交 operation 实际使用的 explicit 快照（提交前与 op id 一同
    # 持久化）：finalize 时与行当前 explicit 比对，不一致说明在途期间用户
    # 翻转了显式标记 → 旧 op 完成后转 pending 以同 document 新 generation
    # 重提取，不遗漏 processing 窗口的显式变更
    operation_explicit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 当前派发代次令牌：NATS Msg-Id 幂等键（retry/forget/对账补发时换新代）
    dispatch_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=lambda: uuid.uuid4().hex,
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class SuperAssistantMemorySetting(Base):
    """每用户的跨会话记忆设置。

    enabled 默认 True：自动沉淀照常入队，即使尚未选择记忆模型（未选模型
    时状态页显示 unconfigured，不盲猜现有模型）。
    """

    __tablename__ = "super_assistant_memory_settings"

    owner_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    model_config_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)
