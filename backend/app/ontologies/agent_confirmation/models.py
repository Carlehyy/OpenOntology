"""确认请求 / 操作账本 / 外部效果 attempt 的持久数据模型（YIYA-39 v2 §4.5）。

- ``fo_action_confirmations``：不可变确认载荷，绑定用户/会话/意图/来源/本体/
  release/动作/目标/最终参数；UNIQUE(owner,session,prepare_request_id) 提供
  持久幂等，UNIQUE(owner,session,root_intent,action_request_slot) 提供同人类
  意图下的稳定槽。
- ``fo_action_operations``：每个确认至多一个操作行；guard_hash 在活跃状态下
  唯一，防止同用户多会话对同一在途载荷重复执行。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


_ACTIVE_GUARD_WHERE = (
    "state IN ('confirmed','dispatch_queued','executing','recovering',"
    "'pending_approval','unknown','compensation_required')"
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# 状态机（与引擎/HITL 状态分开）：
#   prepared → confirmed → dispatch_queued → executing → succeeded
#                                    ↘ pending_approval → approval_rejected
# prepared → denied / cancelled / expired
# executing → recovering / unknown / compensation_required / failed_not_executed
CONFIRMATION_STATES = (
    "prepared", "confirmed", "dispatch_queued", "executing", "pending_approval",
    "succeeded", "failed_not_executed", "approval_rejected", "denied",
    "cancelled", "expired", "recovering", "unknown", "compensation_required",
)

# guard_hash 唯一占用的状态（部分唯一索引）
ACTIVE_OPERATION_STATES = (
    "confirmed", "dispatch_queued", "executing", "recovering",
    "pending_approval", "unknown", "compensation_required",
)

OPERATION_STATES = CONFIRMATION_STATES


class ActionConfirmation(Base):
    """一次不可变的动作确认请求 — 写后载荷不可更新。"""

    __tablename__ = "fo_action_confirmations"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id", "session_kind", "session_id", "prepare_request_id",
            name="uq_action_confirmation_prepare_request"),
        UniqueConstraint(
            "actor_user_id", "session_kind", "session_id", "root_intent_id",
            "action_request_slot",
            name="uq_action_confirmation_request_slot"),
        CheckConstraint(
            "state IN (" + ",".join(f"'{s}'" for s in CONFIRMATION_STATES) + ")",
            name="ck_action_confirmation_state"),
        Index("ix_fo_action_confirmations_owner_created",
              "actor_user_id", "created_at"),
        Index("ix_fo_action_confirmations_ontology_release",
              "ontology_id", "release_id"),
    )


    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    actor_user_id: Mapped[str] = mapped_column(String, nullable=False)
    session_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    root_intent_id: Mapped[str] = mapped_column(String, nullable=False)
    action_request_slot: Mapped[str] = mapped_column(String, nullable=False)
    prepare_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source_call_id: Mapped[str] = mapped_column(String, nullable=False)

    ontology_id: Mapped[str] = mapped_column(String, nullable=False)
    release_id: Mapped[str] = mapped_column(String, nullable=False)
    release_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    action_id: Mapped[str] = mapped_column(String, nullable=False)
    target_instance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    final_parameters: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                   default=dict)
    parameter_sources: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                    default=dict)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                    default=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    display_payload: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                  default=dict)

    state: Mapped[str] = mapped_column(String(32), nullable=False,
                                       default="prepared")
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ActionOperation(Base):
    """确认后的技术执行账本 — 状态决定 guard 占用。"""

    __tablename__ = "fo_action_operations"
    __table_args__ = (
        UniqueConstraint("confirmation_id", name="uq_action_operation_confirmation"),
        UniqueConstraint("initial_engine_key", name="uq_action_operation_initial_key"),
        CheckConstraint(
            "state IN (" + ",".join(f"'{s}'" for s in OPERATION_STATES) + ")",
            name="ck_action_operation_state"),
        Index(
            "uq_action_operation_guard_active", "guard_hash", unique=True,
            sqlite_where=text(_ACTIVE_GUARD_WHERE),
            postgresql_where=text(_ACTIVE_GUARD_WHERE),
        ),
        # P3 对照项：有值时唯一（NULL 不参与 — 部分唯一索引）
        Index(
            "uq_action_operation_approval_log", "approval_log_id", unique=True,
            sqlite_where=text("approval_log_id IS NOT NULL"),
            postgresql_where=text("approval_log_id IS NOT NULL"),
        ),
        Index(
            "uq_action_operation_technical_key", "technical_engine_key",
            unique=True,
            sqlite_where=text("technical_engine_key IS NOT NULL"),
            postgresql_where=text("technical_engine_key IS NOT NULL"),
        ),
        Index("ix_fo_action_operations_state_claimed",
              "state", "claimed_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    confirmation_id: Mapped[str] = mapped_column(String, nullable=False)
    initial_engine_key: Mapped[str] = mapped_column(String(255), nullable=False)
    approval_log_id: Mapped[str | None] = mapped_column(String, nullable=True)
    technical_engine_key: Mapped[str | None] = mapped_column(String(255),
                                                             nullable=True)
    guard_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[str] = mapped_column(String(32), nullable=False,
                                       default="dispatch_queued")
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    engine_entered_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                               nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                              nullable=True)
    fence_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_log_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconcile_evidence: Mapped[dict] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now,
                                                 onupdate=_now)


class ActionExternalAttempt(Base):
    """外部效果（webhook）发送前后的持久证据 — 不含正文秘密。"""

    __tablename__ = "fo_action_external_attempts"
    __table_args__ = (
        UniqueConstraint("operation_id", "effect_ordinal",
                         name="uq_action_external_attempt_effect"),
        UniqueConstraint("stable_delivery_key",
                         name="uq_action_external_attempt_delivery_key"),
        CheckConstraint(
            "state IN ('started','acknowledged','unknown','proven_not_applied')",
            name="ck_action_external_attempt_state"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    operation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    effect_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    stable_delivery_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False,
                                       default="started")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receipt_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
