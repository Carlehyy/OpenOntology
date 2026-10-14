"""任务实例 — 事件层：类型冻结枚举 + payload 必填校验 + 追加器（§5.6）。

一切状态变更 = 事务内「append event(s) + 物化行」。seq 在实例行锁
保护下由 max(seq)+1 分配，(instance_id, seq) 唯一约束兜底并发。
event_budget 为防失控上限：超限时 append_event 抛 EventBudgetExceeded，
由引擎收口为实例失败（终态事件本身允许越过预算落盘）。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.task_instances.models import TaskEvent

INSTANCE_CREATED = "instance.created"
INSTANCE_TERMINAL = "instance.terminal"
NODE_CREATED = "node.created"
NODE_DISPATCHED = "node.dispatched"
NODE_CLAIMED = "node.claimed"
NODE_PROGRESS = "node.progress"
NODE_COMPLETED = "node.completed"
NODE_FAILED = "node.failed"
NODE_VOIDED = "node.voided"
NODE_REOPENED = "node.reopened"
CONTRACT_VIOLATED = "contract.violated"
NODE_CORRECTION_REQUESTED = "node.correction_requested"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"
APPROVAL_EXPIRED = "approval.expired"
HUMAN_ASSIGNED = "human.assigned"
HUMAN_SUBMITTED = "human.submitted"
NODE_REJECTED = "node.rejected"
STEERING_DELIVERED = "steering.delivered"
ARTIFACT_DECLARED = "artifact.declared"
ARTIFACT_COMPLETED = "artifact.completed"

EVENT_TYPES = frozenset({
    INSTANCE_CREATED,
    INSTANCE_TERMINAL,
    NODE_CREATED,
    NODE_DISPATCHED,
    NODE_CLAIMED,
    NODE_PROGRESS,
    NODE_COMPLETED,
    NODE_FAILED,
    NODE_VOIDED,
    NODE_REOPENED,
    CONTRACT_VIOLATED,
    NODE_CORRECTION_REQUESTED,
    APPROVAL_REQUESTED,
    APPROVAL_DECIDED,
    APPROVAL_EXPIRED,
    HUMAN_ASSIGNED,
    HUMAN_SUBMITTED,
    NODE_REJECTED,
    STEERING_DELIVERED,
    ARTIFACT_DECLARED,
    ARTIFACT_COMPLETED,
})

# 每种事件的 payload 必填字段（append 时强制，借鉴 kernel REQUIRED_PAYLOAD）
REQUIRED_PAYLOAD: dict[str, tuple[str, ...]] = {
    INSTANCE_CREATED: ("template_revision_id",),
    INSTANCE_TERMINAL: ("outcome",),
    NODE_CREATED: ("node_id", "attempt_no", "node_kind"),
    NODE_DISPATCHED: ("node_id", "attempt_no"),
    NODE_CLAIMED: ("node_id", "attempt_no", "owner"),
    NODE_PROGRESS: ("node_id", "attempt_no", "note"),
    NODE_COMPLETED: ("node_id", "attempt_no"),
    NODE_FAILED: ("node_id", "attempt_no", "error"),
    NODE_VOIDED: ("node_id", "attempt_no", "cause"),
    NODE_REOPENED: ("node_id", "attempt_no"),
    CONTRACT_VIOLATED: ("node_id", "attempt_no", "port", "violations"),
    NODE_CORRECTION_REQUESTED: ("node_id", "next_attempt_no", "remaining"),
    APPROVAL_REQUESTED: ("node_id", "approval_id"),
    APPROVAL_DECIDED: ("node_id", "approval_id", "decision"),
    APPROVAL_EXPIRED: ("node_id", "approval_id"),
    HUMAN_ASSIGNED: ("node_id", "attempt_no", "role"),
    HUMAN_SUBMITTED: ("node_id", "attempt_no"),
    NODE_REJECTED: ("node_id", "target_node_id", "reason", "rejector"),
    STEERING_DELIVERED: ("node_id", "message_id"),
    ARTIFACT_DECLARED: ("node_id", "artifact_id", "name"),
    ARTIFACT_COMPLETED: ("node_id", "artifact_id", "sha256"),
}


class EventValidationError(Exception):
    """未知事件类型或 payload 缺必填字段（编程错误，直接抛出）。"""


class EventBudgetExceeded(Exception):
    """事件数超过实例 event_budget（引擎收口为实例失败）。"""


def next_seq(db: Session, instance_id: str) -> int:
    current = db.scalar(
        select(func.max(TaskEvent.seq)).where(
            TaskEvent.instance_id == instance_id))
    return (current or 0) + 1


def append_event(
    db: Session,
    instance_id: str,
    event_type: str,
    payload: dict,
    *,
    node_run_id: str | None = None,
    actor: str = "system",
    event_budget: int | None = None,
    allow_over_budget: bool = False,
) -> TaskEvent:
    if event_type not in EVENT_TYPES:
        raise EventValidationError(f"未知事件类型: {event_type}")
    missing = [key for key in REQUIRED_PAYLOAD[event_type] if key not in payload]
    if missing:
        raise EventValidationError(
            f"事件 {event_type} 缺少必填字段: {missing}")
    seq = next_seq(db, instance_id)
    if (event_budget is not None and not allow_over_budget
            and seq > event_budget):
        raise EventBudgetExceeded(
            f"实例 {instance_id} 事件数超过预算 {event_budget}")
    event = TaskEvent(
        instance_id=instance_id,
        seq=seq,
        type=event_type,
        node_run_id=node_run_id,
        actor=actor,
        payload=payload,
    )
    db.add(event)
    db.flush()
    return event
