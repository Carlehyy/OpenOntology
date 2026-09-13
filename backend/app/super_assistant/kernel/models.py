"""kernel.v1 的持久化事实模型。

这些表只保存执行事实、租约和投影游标；旧的 SuperAssistant* 读模型继续由
legacy 路径维护。正文较大的内容通过引用字段指向对象存储，JSON 字段只放
小型合同数据和快照元数据。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionRun(Base):
    __tablename__ = "super_assistant_execution_runs"
    __table_args__ = (
        UniqueConstraint("owner_id", "conversation_id", "idempotency_key", name="uq_sa_execution_run_idempotency"),
        Index("ix_sa_execution_runs_owner_status", "owner_id", "status"),
        Index("ix_sa_execution_runs_parent", "parent_run_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_conversations.id", ondelete="CASCADE"), nullable=False)
    parent_run_id: Mapped[str | None] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="SET NULL"), nullable=True)
    execution_version: Mapped[str] = mapped_column(String(32), nullable=False, default="kernel.v1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cancel_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    wait_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    policy_snapshot_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    budget_snapshot_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    binding_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="direct_ui")
    binding_snapshot_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ontology_id: Mapped[str | None] = mapped_column(String, nullable=True)
    draft_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    permission_snapshot_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    join_policy: Mapped[str] = mapped_column(String(10), nullable=False, default="all")
    required_child_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionTurn(Base):
    __tablename__ = "super_assistant_execution_turns"
    __table_args__ = (UniqueConstraint("run_id", "turn_no", name="uq_sa_execution_turn_no"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionStep(Base):
    __tablename__ = "super_assistant_execution_steps"
    __table_args__ = (UniqueConstraint("turn_id", "step_no", name="uq_sa_execution_step_no"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    turn_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_turns.id", ondelete="CASCADE"), nullable=False, index=True)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    request_snapshot_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionCall(Base):
    __tablename__ = "super_assistant_execution_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "capability_revision", "idempotency_key", name="uq_sa_execution_call_idempotency"),
        Index("ix_sa_execution_calls_reconcile", "status", "next_reconcile_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_id: Mapped[str | None] = mapped_column(String, ForeignKey("super_assistant_execution_turns.id", ondelete="SET NULL"), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String, ForeignKey("super_assistant_execution_steps.id", ondelete="SET NULL"), nullable=True)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    capability_key: Mapped[str] = mapped_column(String(255), nullable=False)
    capability_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    input_snapshot_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    side_effect_class: Mapped[str] = mapped_column(String(32), nullable=False, default="read")
    authorization_snapshot_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="offered")
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="not_sent")
    remote_task_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reconcile_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remote_observed_state_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    manual_attention: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionAttempt(Base):
    __tablename__ = "super_assistant_execution_attempts"
    __table_args__ = (UniqueConstraint("call_id", "attempt_no", name="uq_sa_execution_attempt_no"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_calls.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    transport_request_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timeout: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    safe_to_retry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    token_usage_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    cost_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class ExecutionEvent(Base):
    __tablename__ = "super_assistant_execution_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_sa_execution_event_seq"), Index("ix_sa_execution_events_run_seq", "run_id", "seq"))

    event_id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    actor: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    causation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    command_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    redaction: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class InboxItem(Base):
    __tablename__ = "super_assistant_execution_inbox"
    __table_args__ = (UniqueConstraint("run_id", "idempotency_key", name="uq_sa_execution_inbox_idempotency"), Index("ix_sa_execution_inbox_pending", "status", "priority", "expires_at"))

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    call_id: Mapped[str | None] = mapped_column(String, ForeignKey("super_assistant_execution_calls.id", ondelete="SET NULL"), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    question_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expiry_policy: Mapped[str | None] = mapped_column(String(20), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Approval(Base):
    __tablename__ = "super_assistant_execution_approvals"
    __table_args__ = (Index("ix_sa_execution_approvals_run_status", "run_id", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="CASCADE"), nullable=False)
    call_id: Mapped[str | None] = mapped_column(String, ForeignKey("super_assistant_execution_calls.id", ondelete="SET NULL"), nullable=True)
    plugin_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_summary: Mapped[str] = mapped_column(Text, nullable=False)
    parameter_summary: Mapped[str] = mapped_column(Text, nullable=False)
    scope_summary: Mapped[str] = mapped_column(Text, nullable=False)
    capability_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    parameter_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)


class ContextSnapshot(Base):
    __tablename__ = "super_assistant_execution_context_snapshots"
    __table_args__ = (Index("ix_sa_execution_context_run_turn", "run_id", "turn_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="CASCADE"), nullable=False)
    turn_id: Mapped[str | None] = mapped_column(String, ForeignKey("super_assistant_execution_turns.id", ondelete="SET NULL"), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String, ForeignKey("super_assistant_execution_attempts.id", ondelete="SET NULL"), nullable=True)
    pack_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    source_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    budget: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    policy_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    redaction_revision: Mapped[str] = mapped_column(String(64), nullable=False)


class Artifact(Base):
    __tablename__ = "super_assistant_execution_artifacts"
    __table_args__ = (Index("ix_sa_execution_artifacts_run", "run_id", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="CASCADE"), nullable=False)
    call_id: Mapped[str | None] = mapped_column(String, ForeignKey("super_assistant_execution_calls.id", ondelete="SET NULL"), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="declared")
    integrity_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    business_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")
    retention_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    provenance_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class CapabilityRevision(Base):
    __tablename__ = "super_assistant_capability_revisions"
    __table_args__ = (UniqueConstraint("key", "revision", name="uq_sa_capability_revision"),)

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    trust_level: Mapped[str] = mapped_column(String(24), nullable=False)
    permissions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    side_effect_class: Mapped[str] = mapped_column(String(32), nullable=False)
    supports_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_cancel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_artifact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_query_status: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    workspace_scope: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    network_scope: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    secret_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProjectionCursor(Base):
    __tablename__ = "super_assistant_execution_projection_cursors"
    __table_args__ = (UniqueConstraint("projection_name", "partition_key", name="uq_sa_projection_cursor_partition"),)

    projection_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    partition_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    error_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class ExecutionDispatchOutbox(Base):
    __tablename__ = "super_assistant_execution_dispatch_outbox"
    __table_args__ = (UniqueConstraint("command_id", name="uq_sa_execution_outbox_command"), Index("ix_sa_execution_outbox_pending", "status", "next_attempt_at"))

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    command_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("super_assistant_execution_runs.id", ondelete="CASCADE"), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    message_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    claim_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
