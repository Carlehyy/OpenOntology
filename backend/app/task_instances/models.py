"""任务实例 — 数据模型（八表，设计方案 §7）。

  - TaskTemplate / TaskTemplateRevision  模板与不可变版本（canonical_hash 幂等）
  - TaskInstance                          一次激活执行（spec 快照钉死 revision）
  - TaskNodeRun                           节点 × 尝试（attempt 承载纠正与打回重做）
  - TaskEvent                             append-only 事实源（(instance, seq) gap-free）
  - TaskApproval / TaskSteeringMessage    审批关口 / 运行中插话
  - TaskArtifact                          产物（sha256 + MinIO storage_uri）

状态机（设计方案 §5.1）：
  实例：active → cancelling → completed | failed | cancelled
  节点：pending → dispatched → running → waiting_human | waiting_approval
        → completed | failed | voided | skipped | cancelled
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
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
    return datetime.now(timezone.utc)


# —— 实例状态 ——
INSTANCE_ACTIVE = "active"
INSTANCE_CANCELLING = "cancelling"
INSTANCE_COMPLETED = "completed"
INSTANCE_FAILED = "failed"
INSTANCE_CANCELLED = "cancelled"
INSTANCE_STATUSES = (
    INSTANCE_ACTIVE,
    INSTANCE_CANCELLING,
    INSTANCE_COMPLETED,
    INSTANCE_FAILED,
    INSTANCE_CANCELLED,
)

# —— 节点运行状态 ——
NODE_PENDING = "pending"             # 已建未就绪（等待上游）
NODE_DISPATCHED = "dispatched"       # 已派发执行器（agent）
NODE_RUNNING = "running"             # 执行器已认领
NODE_WAITING_HUMAN = "waiting_human"  # 人工生产节点等待交活
NODE_WAITING_APPROVAL = "waiting_approval"
NODE_COMPLETED = "completed"
NODE_FAILED = "failed"
NODE_VOIDED = "voided"               # 被驳回/纠正作废（历史保留）
NODE_SKIPPED = "skipped"             # 实例终态时未触达
NODE_CANCELLED = "cancelled"
NODE_STATUSES = (
    NODE_PENDING,
    NODE_DISPATCHED,
    NODE_RUNNING,
    NODE_WAITING_HUMAN,
    NODE_WAITING_APPROVAL,
    NODE_COMPLETED,
    NODE_FAILED,
    NODE_VOIDED,
    NODE_SKIPPED,
    NODE_CANCELLED,
)

# —— 审批状态 ——
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_EXPIRED = "expired"
APPROVAL_CANCELLED = "cancelled"

# —— 插话状态 ——
STEERING_QUEUED = "queued"
STEERING_DELIVERED = "delivered"
STEERING_ACKNOWLEDGED = "acknowledged"
STEERING_FAILED = "failed"


class TaskTemplate(Base):
    """一个声明式流程模板： revisions 只增不改，latest 指针随保存前移。"""

    __tablename__ = "task_templates"
    __table_args__ = (
        Index("ix_task_templates_updated_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    # 指向本表 revisions 的最新版本（应用层维护；不设 FK 避免
    # templates ↔ revisions 循环外键，SQLite 测试库无法 ALTER 补挂）
    latest_revision_id: Mapped[str | None] = mapped_column(
        String, nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class TaskTemplateRevision(Base):
    """模板的不可变版本：spec_yaml 原文 + 编译归一化产物 + canonical_hash。"""

    __tablename__ = "task_template_revisions"
    __table_args__ = (
        UniqueConstraint("template_id", "revision_no",
                         name="uq_task_template_revisions_no"),
        UniqueConstraint("template_id", "canonical_hash",
                         name="uq_task_template_revisions_hash"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_templates.id", ondelete="CASCADE"),
        nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    # 编译归一化产物（节点/边/契约/预算，引擎消费的结构）
    spec_compiled: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)


class TaskInstance(Base):
    """模板某 revision 的一次激活：spec_snapshot 深拷贝，演进不影响在途实例。"""

    __tablename__ = "task_instances"
    __table_args__ = (
        Index("ix_task_instances_status_created", "status", "created_at"),
        Index("ix_task_instances_created_by", "created_by"),
        UniqueConstraint("idempotency_key", name="uq_task_instances_idem"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    template_revision_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_template_revisions.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    inputs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    spec_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default=INSTANCE_ACTIVE)
    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class TaskNodeRun(Base):
    """节点的一次执行尝试：纠正与打回重做均递增 attempt（inputs 附上下文）。"""

    __tablename__ = "task_node_runs"
    __table_args__ = (
        UniqueConstraint("instance_id", "node_id", "attempt_no",
                         name="uq_task_node_runs_attempt"),
        UniqueConstraint("instance_id", "node_id", "inputs_hash",
                         name="uq_task_node_runs_inputs_hash"),
        Index("ix_task_node_runs_instance_status", "instance_id", "status"),
        Index("ix_task_node_runs_lease", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_instances.id", ondelete="CASCADE"),
        nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False,
                                       default=NODE_PENDING)
    inputs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False,
                                             default="")
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_count: Mapped[int] = mapped_column(Integer, nullable=False,
                                                  default=0)
    rework_count: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=0)
    model_config_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("model_configs.id", ondelete="SET NULL"),
        nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)


class TaskEvent(Base):
    """append-only 事件流：一切状态的来源，(instance, seq) 唯一且 gap-free。"""

    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("instance_id", "seq", name="uq_task_events_seq"),
        Index("ix_task_events_instance_id", "instance_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_instances.id", ondelete="CASCADE"),
        nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    node_run_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("task_node_runs.id", ondelete="SET NULL"),
        nullable=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False, default="system")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)


class TaskApproval(Base):
    """审批关口的等待-决策记录（拒绝时 reason 即驳回理由）。"""

    __tablename__ = "task_approvals"
    __table_args__ = (
        Index("ix_task_approvals_instance_status", "instance_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_instances.id", ondelete="CASCADE"),
        nullable=False)
    node_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_node_runs.id", ondelete="CASCADE"),
        nullable=False, index=True)
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False,
                                               default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=APPROVAL_PENDING)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)


class TaskSteeringMessage(Base):
    """发送给运行中节点的中途消息（插话）。"""

    __tablename__ = "task_steering_messages"
    __table_args__ = (
        Index("ix_task_steering_instance", "instance_id"),
        UniqueConstraint("idempotency_key", name="uq_task_steering_idem"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_instances.id", ondelete="CASCADE"),
        nullable=False)
    node_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_node_runs.id", ondelete="CASCADE"),
        nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=STEERING_QUEUED)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)


class TaskArtifact(Base):
    """节点交付的可校验产物（先落 MinIO 再登记，sha256 防篡改）。"""

    __tablename__ = "task_artifacts"
    __table_args__ = (
        Index("ix_task_artifacts_instance", "instance_id"),
        Index("ix_task_artifacts_node_run", "node_run_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    instance_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_instances.id", ondelete="CASCADE"),
        nullable=False)
    node_run_id: Mapped[str] = mapped_column(
        String, ForeignKey("task_node_runs.id", ondelete="CASCADE"),
        nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False,
                                           default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
