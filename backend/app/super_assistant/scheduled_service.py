"""用户日程 CRUD 与事件接收：所有存量目标均进入 AutoLoop。

计划投影、唯一槽位 run、接收事实和待派发同事务提交。NATS 只负责唤醒，
发布失败由 AutoLoop 持久派发扫描恢复，不再直接创建会话或任务实例。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.inbox.schemas import (
    InboxAction,
    InboxAudience,
    InboxContent,
    InboxEventIn,
    InboxResource,
    InboxSource,
)
from app.inbox.service import publish_event
from app.shared.database import SessionLocal
from app.super_assistant.models import (
    SuperAssistantScheduledRun,
    SuperAssistantScheduledTask,
)
from app.super_assistant.autoloop.ingress import accept_scheduled_run
from app.super_assistant.autoloop.models import AutoLoopDispatch, AutoLoopEvent, AutoLoopReceipt
from app.super_assistant.schemas import ScheduledTaskCreate, ScheduledTaskUpdate
from app.super_assistant.scheduled_constants import STALE_RUNNING
from app.task_instances.models import TaskInstance, TaskTemplate

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
REAP_ERROR = "执行超时未完成"
QUEUED_REDISPATCH_AFTER = timedelta(seconds=20)
OVERDUE_GRACE = timedelta(hours=2)
ONCE_PAST_GRACE = timedelta(minutes=2)
# 模板目标的任务内容即实例 goal（InstanceActivate.goal 上限），创建/更新时校验
TEMPLATE_GOAL_MAX_CHARS = 20_000
IN_FLIGHT = ("queued", "running")
INBOX_HREF = "/super-assistant?schedule={task_id}&scheduleRun={run_id}"


class ScheduledTaskError(Exception):
    pass


class ScheduledTaskNotFoundError(ScheduledTaskError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc_naive(value: datetime) -> datetime:
    """库内 UTC naive，或已带时区的瞬间。naive 输入不当成上海墙钟。"""
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_user_run_at(value: datetime) -> datetime:
    """用户触发时间：无时区按 Asia/Shanghai 墙钟，再存 UTC naive。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return as_utc_naive(value)


def compute_next_run_at(
    *,
    kind: str,
    after: datetime,
    run_at: datetime | None = None,
    hour: int | None = None,
    minute: int | None = None,
    weekday: int | None = None,
) -> datetime | None:
    """计算 after 之后的下一次触发时刻（UTC naive，秒归零）。"""
    after_utc = as_utc_naive(after)
    after_local = after_utc.replace(tzinfo=timezone.utc).astimezone(SHANGHAI)

    if kind == "once":
        if run_at is None:
            raise ScheduledTaskError("一次性任务需要触发时间")
        when = as_utc_naive(run_at)
        return when if when > after_utc else None

    if hour is None or minute is None:
        raise ScheduledTaskError("每日/每周任务需要时刻")
    if kind == "daily":
        candidate = after_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after_local:
            candidate = candidate + timedelta(days=1)
        return as_utc_naive(candidate)

    if kind == "weekly":
        if weekday is None or weekday < 0 or weekday > 6:
            raise ScheduledTaskError("每周任务需要星期（0=周一 … 6=周日）")
        candidate = after_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (weekday - candidate.weekday()) % 7
        if days_ahead == 0 and candidate <= after_local:
            days_ahead = 7
        candidate = candidate + timedelta(days=days_ahead)
        return as_utc_naive(candidate)

    raise ScheduledTaskError("未知的调度类型")


def _validate_schedule(kind: str, run_at: datetime | None, hour: int | None,
                       minute: int | None, weekday: int | None) -> None:
    compute_next_run_at(
        kind=kind, after=_now() - timedelta(seconds=1),
        run_at=run_at, hour=hour, minute=minute, weekday=weekday,
    )


def _title_from(instruction: str, title: str) -> str:
    cleaned = title.strip()
    if cleaned:
        return cleaned[:200]
    return instruction.strip().replace("\n", " ")[:40] or "定时任务"


def _latest_run(db: Session, task_id: str) -> SuperAssistantScheduledRun | None:
    return (
        db.query(SuperAssistantScheduledRun)
        .filter(SuperAssistantScheduledRun.task_id == task_id)
        .order_by(SuperAssistantScheduledRun.scheduled_for.desc())
        .first()
    )


def _validate_target(
    db: Session, target_kind: str, template_id: str | None, instruction: str,
) -> TaskTemplate | None:
    """触发对象校验：模板目标要求模板存在且未软删，任务内容受实例 goal 上限约束。"""
    if target_kind == "super_assistant":
        if template_id:
            raise ScheduledTaskError("超级助手目标不需要选择流程模板")
        return None
    if not template_id:
        raise ScheduledTaskError("请选择流程模板")
    template = (
        db.query(TaskTemplate)
        .filter(TaskTemplate.id == template_id, TaskTemplate.deleted_at.is_(None))
        .first()
    )
    if template is None:
        # 引用实体缺失属请求校验错误（400），与任务归属缺失的 404 区分语义
        raise ScheduledTaskError("流程模板不存在或已删除")
    if len(instruction) > TEMPLATE_GOAL_MAX_CHARS:
        raise ScheduledTaskError(f"流程模板目标的任务内容不能超过 {TEMPLATE_GOAL_MAX_CHARS} 字")
    return template


def _attach_target_name(db: Session, task: SuperAssistantScheduledTask) -> None:
    """Out.target_template_name 读取侧回填：跟随模板最新名，软删/丢失为 None。"""
    task.target_template_name = None
    if task.target_kind == "task_template" and task.target_template_id:
        template = (
            db.query(TaskTemplate)
            .filter(
                TaskTemplate.id == task.target_template_id,
                TaskTemplate.deleted_at.is_(None),
            )
            .first()
        )
        if template is not None:
            task.target_template_name = template.name


def _with_last_run(db: Session, task: SuperAssistantScheduledTask) -> SuperAssistantScheduledTask:
    run = _latest_run(db, task.id)
    task.last_run_id = run.id if run else None
    task.last_run_status = run.status if run else None
    task.last_run_at = run.finished_at or run.started_at or run.created_at if run else None
    _attach_target_name(db, task)
    return task


def _get_owned(db: Session, owner_id: str, task_id: str) -> SuperAssistantScheduledTask:
    task = (
        db.query(SuperAssistantScheduledTask)
        .filter(
            SuperAssistantScheduledTask.id == task_id,
            SuperAssistantScheduledTask.owner_id == owner_id,
        )
        .first()
    )
    if task is None:
        raise ScheduledTaskNotFoundError("定时任务不存在")
    return task


def list_tasks(db: Session, owner_id: str) -> list[SuperAssistantScheduledTask]:
    rows = (
        db.query(SuperAssistantScheduledTask)
        .filter(SuperAssistantScheduledTask.owner_id == owner_id)
        .order_by(SuperAssistantScheduledTask.updated_at.desc())
        .all()
    )
    return [_with_last_run(db, row) for row in rows]


def create_task(db: Session, owner_id: str, body: ScheduledTaskCreate) -> SuperAssistantScheduledTask:
    _validate_schedule(body.schedule_kind, body.run_at, body.hour, body.minute, body.weekday)
    template = _validate_target(db, body.target_kind, body.target_template_id, body.instruction)
    now = _now()
    run_at = parse_user_run_at(body.run_at) if body.run_at is not None else None
    if body.schedule_kind == "once":
        if run_at is None:
            raise ScheduledTaskError("一次性任务需要触发时间")
        if run_at < now - ONCE_PAST_GRACE:
            raise ScheduledTaskError("触发时间已过，请选择一个未来的时间")
    next_run = compute_next_run_at(
        kind=body.schedule_kind, after=now - timedelta(seconds=1),
        run_at=run_at, hour=body.hour, minute=body.minute, weekday=body.weekday,
    )
    if body.schedule_kind == "once" and next_run is None:
        next_run = now
    item = SuperAssistantScheduledTask(
        owner_id=owner_id,
        title=_title_from(body.instruction, body.title),
        instruction=body.instruction,
        target_kind=body.target_kind,
        target_template_id=template.id if template is not None else None,
        schedule_kind=body.schedule_kind,
        timezone="Asia/Shanghai",
        run_at=run_at,
        hour=body.hour,
        minute=body.minute,
        weekday=body.weekday,
        enabled=body.enabled,
        next_run_at=next_run if body.enabled else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _with_last_run(db, item)


def get_task(db: Session, owner_id: str, task_id: str) -> SuperAssistantScheduledTask:
    return _with_last_run(db, _get_owned(db, owner_id, task_id))


def update_task(db: Session, owner_id: str, task_id: str, body: ScheduledTaskUpdate) -> SuperAssistantScheduledTask:
    task = _get_owned(db, owner_id, task_id)
    if body.title is not None:
        task.title = body.title
    if body.instruction is not None:
        task.instruction = body.instruction
    if (body.target_kind is not None or "target_template_id" in body.model_fields_set
            or body.instruction is not None):
        # 触发对象或任务内容任一变化都按合并后的最终值校验（模板目标的内容受
        # 实例 goal 上限约束）；非模板目标一律清空模板指针（切换回超级助手时
        # 携带的存量指针不算矛盾）
        target_kind = body.target_kind if body.target_kind is not None else task.target_kind
        target_template_id = (
            body.target_template_id
            if "target_template_id" in body.model_fields_set
            else task.target_template_id
        )
        if target_kind != "task_template":
            target_template_id = None
        instruction = body.instruction if body.instruction is not None else task.instruction
        _validate_target(db, target_kind, target_template_id, instruction)
        task.target_kind = target_kind
        task.target_template_id = target_template_id
    if body.schedule_kind is not None:
        task.schedule_kind = body.schedule_kind
    if "run_at" in body.model_fields_set:
        task.run_at = parse_user_run_at(body.run_at) if body.run_at else None
    if "hour" in body.model_fields_set:
        task.hour = body.hour
    if "minute" in body.model_fields_set:
        task.minute = body.minute
    if "weekday" in body.model_fields_set:
        task.weekday = body.weekday
    if body.enabled is not None:
        task.enabled = body.enabled

    _validate_schedule(task.schedule_kind, task.run_at, task.hour, task.minute, task.weekday)
    if task.enabled:
        task.next_run_at = compute_next_run_at(
            kind=task.schedule_kind, after=_now() - timedelta(seconds=1),
            run_at=task.run_at, hour=task.hour, minute=task.minute, weekday=task.weekday,
        )
        if task.schedule_kind == "once" and task.next_run_at is None:
            raise ScheduledTaskError("触发时间已过，请选择一个未来的时间")
    else:
        task.next_run_at = None
    task.updated_at = _now()
    db.commit()
    db.refresh(task)
    return _with_last_run(db, task)


def delete_task(db: Session, owner_id: str, task_id: str) -> None:
    task = _get_owned(db, owner_id, task_id)
    db.delete(task)
    db.commit()


def _with_event_ids(db: Session, runs: list[SuperAssistantScheduledRun]) -> list[SuperAssistantScheduledRun]:
    by_id = {run.id: run for run in runs}
    for run in runs:
        run.autoloopEventIds = []
    if by_id:
        links = db.execute(select(AutoLoopReceipt.scheduled_run_id, AutoLoopEvent.id).join(
            AutoLoopEvent, AutoLoopEvent.receipt_id == AutoLoopReceipt.id,
        ).where(AutoLoopReceipt.scheduled_run_id.in_(by_id)).order_by(AutoLoopEvent.seq))
        for run_id, event_id in links:
            by_id[run_id].autoloopEventIds.append(event_id)
    return runs


def list_runs(db: Session, owner_id: str, task_id: str, *, limit: int = 50) -> list[SuperAssistantScheduledRun]:
    _get_owned(db, owner_id, task_id)
    runs = (
        db.query(SuperAssistantScheduledRun)
        .filter(
            SuperAssistantScheduledRun.task_id == task_id,
            SuperAssistantScheduledRun.owner_id == owner_id,
        )
        .order_by(SuperAssistantScheduledRun.scheduled_for.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    return _with_event_ids(db, runs)


def get_run(db: Session, owner_id: str, task_id: str, run_id: str) -> SuperAssistantScheduledRun:
    _get_owned(db, owner_id, task_id)
    run = (
        db.query(SuperAssistantScheduledRun)
        .filter(
            SuperAssistantScheduledRun.id == run_id,
            SuperAssistantScheduledRun.task_id == task_id,
            SuperAssistantScheduledRun.owner_id == owner_id,
        )
        .first()
    )
    if run is None:
        raise ScheduledTaskNotFoundError("执行记录不存在")
    return _with_event_ids(db, [run])[0]


def _fail_run_row(db: Session, run: SuperAssistantScheduledRun, now: datetime, error: str) -> None:
    if run.status in {"completed", "failed", "skipped"}:
        return
    run.status = "failed"
    run.error = error
    run.finished_at = now
    task = db.get(SuperAssistantScheduledTask, run.task_id)
    if task is not None:
        _notify(
            db, run=run, task=task, kind="alert",
            title=f"定时任务失败：{task.title}",
            summary=error,
        )


def _reap_stale_runs(db: Session, now: datetime) -> None:
    cutoff = now - STALE_RUNNING
    # 排队超时看创建时间；运行超时看开跑时间（started_at 为空的理论行退回
    # created_at），与 recover_interrupted_streams 的重启保护看同一列——
    # 排队很久但真正开跑不久的运行不该被收割。
    stale = (
        db.query(SuperAssistantScheduledRun)
        .filter(
            or_(
                and_(
                    SuperAssistantScheduledRun.status == "queued",
                    or_(SuperAssistantScheduledRun.conversation_id.isnot(None),
                        SuperAssistantScheduledRun.instance_id.isnot(None)),
                    SuperAssistantScheduledRun.created_at < cutoff,
                ),
                and_(
                    SuperAssistantScheduledRun.status == "running",
                    func.coalesce(
                        SuperAssistantScheduledRun.started_at,
                        SuperAssistantScheduledRun.created_at,
                    ) < cutoff,
                ),
            ),
        )
        .all()
    )
    for run in stale:
        logger.warning(
            "收割超时运行 run=%s task=%s status=%s started_at=%s created_at=%s",
            run.id, run.task_id, run.status, run.started_at, run.created_at,
        )
        # 模板目标收割前先对账：activate 已成功的实例要把 run 补记为完成
        if _reconcile_template_run(db, run, db.get(SuperAssistantScheduledTask, run.task_id)):
            continue
        _fail_run_row(db, run, now, REAP_ERROR)


def _admit_queued_run(db: Session, run: SuperAssistantScheduledRun, now: datetime):
    """CAS an unexecuted legacy run; admission and completion share one commit."""
    changed = db.execute(update(SuperAssistantScheduledRun).where(
        SuperAssistantScheduledRun.id == run.id,
        SuperAssistantScheduledRun.status == "queued",
        SuperAssistantScheduledRun.conversation_id.is_(None),
        SuperAssistantScheduledRun.instance_id.is_(None),
    ).values(status="running", started_at=now))
    if changed.rowcount != 1:
        return None
    task = db.get(SuperAssistantScheduledTask, run.task_id)
    if task is None:
        raise ScheduledTaskNotFoundError("计划已删除")
    _, event, _ = accept_scheduled_run(db, task, run)
    run.status = "completed"
    run.error = None
    run.result_summary = "已生成AutoLoop事件，等待规则路由"
    run.finished_at = now
    db.flush()
    dispatch = db.scalar(select(AutoLoopDispatch).where(AutoLoopDispatch.event_id == event.id))
    return dispatch.id, dispatch.version, dispatch.state


def _publish_admitted(work, dispatch_fn) -> None:
    if work is None or work[2] != "pending":
        return
    try:
        dispatch_fn(work[0], work[1])
    except Exception:
        # The broker may have accepted the message. The durable dispatch keeps
        # its identity/version and the scanner retries after restart.
        logger.exception("日程事件唤醒未确认 dispatch=%s", work[0])


def _redispatch_queued(db: Session, now: datetime, dispatch_fn) -> int:
    queued = db.scalars(select(SuperAssistantScheduledRun).where(
        SuperAssistantScheduledRun.status == "queued",
        SuperAssistantScheduledRun.conversation_id.is_(None),
        SuperAssistantScheduledRun.instance_id.is_(None),
        SuperAssistantScheduledRun.created_at <= now - QUEUED_REDISPATCH_AFTER,
    ).order_by(SuperAssistantScheduledRun.created_at).limit(50)).all()
    admitted = 0
    for run in queued:
        try:
            work = _admit_queued_run(db, run, now)
            db.commit()
            admitted += int(work is not None)
            _publish_admitted(work, dispatch_fn)
        except Exception:
            db.rollback()
            logger.exception("日程排队记录接收失败 run=%s", run.id)
    return admitted


def _in_flight(db: Session, task_id: str) -> SuperAssistantScheduledRun | None:
    return (
        db.query(SuperAssistantScheduledRun)
        .filter(
            SuperAssistantScheduledRun.task_id == task_id,
            SuperAssistantScheduledRun.status.in_(IN_FLIGHT),
        )
        .first()
    )


def _advance_schedule(task: SuperAssistantScheduledTask, now: datetime) -> None:
    if task.schedule_kind == "once":
        task.enabled = False
        task.next_run_at = None
        return
    nxt = compute_next_run_at(
        kind=task.schedule_kind, after=now,
        run_at=task.run_at, hour=task.hour, minute=task.minute, weekday=task.weekday,
    )
    task.next_run_at = nxt
    if nxt is None:
        task.enabled = False


def _notify(db: Session, *, run: SuperAssistantScheduledRun, task: SuperAssistantScheduledTask,
            kind: str, title: str, summary: str, event_suffix: str = "") -> None:
    href = INBOX_HREF.format(task_id=task.id, run_id=run.id)
    publish_event(db, InboxEventIn(
        eventId=f"super-assistant-scheduled:{run.id}:{kind}{event_suffix}",
        occurredAt=datetime.now(timezone.utc),
        operation="append",
        source=InboxSource(
            system="super_assistant",
            type="scheduled_run",
            id=task.id,
            occurrenceId=run.id,
            correlationKey=f"sa-scheduled:{run.id}",
        ),
        item=InboxContent(
            kind=kind,
            priority="high" if kind == "alert" else "normal",
            title=title[:300],
            summary=(summary or "")[:4000],
            safeContext={"taskId": task.id, "runId": run.id, "status": run.status},
        ),
        resource=InboxResource(
            type="super_assistant_scheduled_run",
            id=run.id,
            label=task.title,
            href=href,
        ),
        actions=[InboxAction(key="open", label="查看执行", mode="navigate", href=href)],
        audience=InboxAudience(type="user", user_id=task.owner_id),
    ))


def dispatch_due_tasks(now: datetime | None = None) -> int:
    """Admit due schedule slots; return the number durably admitted."""
    from app.data_channel.pipeline_tasks.dispatch import dispatch_autoloop

    now = as_utc_naive(now or datetime.now(timezone.utc))
    with SessionLocal() as db:
        _reap_stale_runs(db, now)
        db.commit()
        admitted = _redispatch_queued(db, now, dispatch_autoloop)
        due = db.scalars(select(SuperAssistantScheduledTask).where(
            SuperAssistantScheduledTask.enabled.is_(True),
            SuperAssistantScheduledTask.next_run_at.isnot(None),
            SuperAssistantScheduledTask.next_run_at <= now,
        ).order_by(SuperAssistantScheduledTask.next_run_at).limit(50)).all()
        for task in due:
            task_id = task.id
            try:
                admitted += _dispatch_one(db, task, now, dispatch_autoloop)
            except Exception:
                db.rollback()
                logger.exception("日程事件接收失败 task=%s", task_id)
        return admitted


def _dispatch_one(db: Session, task: SuperAssistantScheduledTask, now: datetime, dispatch_fn) -> int:
    slot = task.next_run_at
    if slot is None:
        return 0
    next_run = (None if task.schedule_kind == "once" else compute_next_run_at(
        kind=task.schedule_kind, after=now, run_at=task.run_at,
        hour=task.hour, minute=task.minute, weekday=task.weekday,
    ))
    claimed = db.execute(update(SuperAssistantScheduledTask).where(
        SuperAssistantScheduledTask.id == task.id,
        SuperAssistantScheduledTask.enabled.is_(True),
        SuperAssistantScheduledTask.next_run_at == slot,
    ).values(next_run_at=next_run, enabled=next_run is not None,
        last_dispatched_at=now, updated_at=now))
    if claimed.rowcount != 1:
        db.rollback()
        return 0
    overdue = (now - slot) > OVERDUE_GRACE and task.schedule_kind != "once"
    if _in_flight(db, task.id):
        status, error = "skipped", "上次执行尚未结束，本次已跳过"
    elif overdue:
        status, error = "skipped", "错过触发窗口，已跳过补跑"
    else:
        status, error = "queued", None
    run = SuperAssistantScheduledRun(
        task_id=task.id, owner_id=task.owner_id, scheduled_for=slot,
        status=status, error=error, finished_at=now if status == "skipped" else None,
    )
    db.add(run)
    try:
        db.flush()
        work = _admit_queued_run(db, run, now) if status == "queued" else None
        db.commit()
    except IntegrityError:
        db.rollback()
        # The slot's unique constraint is the final concurrent admission guard.
        existing = db.scalar(select(SuperAssistantScheduledRun).where(
            SuperAssistantScheduledRun.task_id == task.id,
            SuperAssistantScheduledRun.scheduled_for == slot,
        ))
        if existing is None:
            raise
        return 0
    _publish_admitted(work, dispatch_fn)
    return int(work is not None)


def execute_run(run_id: str) -> None:
    """Preserve the old NATS seam, admitting only never-executed queued runs.

    Historical running/terminal rows retain their existing remote identities.
    A timeout never permits replaying the old conversation/template action.
    """
    from app.data_channel.pipeline_tasks.dispatch import dispatch_autoloop

    with SessionLocal() as db:
        run = db.get(SuperAssistantScheduledRun, run_id)
        if run is None:
            return  # A deleted schedule/run has no work to admit.
        work = _admit_queued_run(db, run, _now())
        db.commit()
    _publish_admitted(work, dispatch_autoloop)


def _reconcile_template_run(
    db: Session, run: SuperAssistantScheduledRun, task: SuperAssistantScheduledTask | None,
) -> bool:
    """模板目标 run 的对账兜底：activate_instance 内部自 commit，其后的
    终态写回异常、进程崩溃或收割误报都不代表实例未创建。按幂等键反查
    实例，查到则照实补记完成并回链实例，避免「实例在跑、run 却记失败」。"""
    if task is None or task.target_kind != "task_template":
        return False
    instance = (
        db.query(TaskInstance)
        .filter(TaskInstance.idempotency_key == f"sa-scheduled:{run.id}")
        .first()
    )
    if instance is None:
        return False
    reaped = run.status == "failed" and run.error == REAP_ERROR
    if run.status != "running" and not reaped:
        return False
    run.status = "completed"
    run.error = None
    run.instance_id = instance.id
    run.result_summary = (
        f"已创建实例「{instance.name}」，进度与结果请在任务实例中查看"
    )
    run.finished_at = _now()
    db.commit()
    _notify(
        db, run=run, task=task, kind="notice",
        title=f"定时任务已完成：{task.title}",
        summary=run.result_summary,
        event_suffix="-corrected" if reaped else "",
    )
    db.commit()
    return True
