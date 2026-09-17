"""超级助手定时任务：计划 CRUD、到期派发、无人值守执行。

扫描半边在 API 进程（APScheduler），执行半边经 NATS 交给 nats_executor。
每次触发新建一条会话，结果摘要写回执行记录；需要确认的写工具不自动批准。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
    SuperAssistantConversation,
    SuperAssistantMessage,
    SuperAssistantScheduledRun,
    SuperAssistantScheduledTask,
)
from app.super_assistant.runtime import stream_chat
from app.super_assistant.schemas import ScheduledTaskCreate, ScheduledTaskUpdate

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
STALE_RUNNING = timedelta(minutes=30)
QUEUED_REDISPATCH_AFTER = timedelta(seconds=20)
OVERDUE_GRACE = timedelta(hours=2)
ONCE_PAST_GRACE = timedelta(minutes=2)
RESULT_SUMMARY_CHARS = 4000
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


def _with_last_run(db: Session, task: SuperAssistantScheduledTask) -> SuperAssistantScheduledTask:
    run = _latest_run(db, task.id)
    task.last_run_id = run.id if run else None
    task.last_run_status = run.status if run else None
    task.last_run_at = run.finished_at or run.started_at or run.created_at if run else None
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


def list_runs(db: Session, owner_id: str, task_id: str, *, limit: int = 50) -> list[SuperAssistantScheduledRun]:
    _get_owned(db, owner_id, task_id)
    return (
        db.query(SuperAssistantScheduledRun)
        .filter(
            SuperAssistantScheduledRun.task_id == task_id,
            SuperAssistantScheduledRun.owner_id == owner_id,
        )
        .order_by(SuperAssistantScheduledRun.scheduled_for.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )


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
    return run


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
    stale = (
        db.query(SuperAssistantScheduledRun)
        .filter(
            SuperAssistantScheduledRun.status.in_(IN_FLIGHT),
            SuperAssistantScheduledRun.created_at < cutoff,
        )
        .all()
    )
    for run in stale:
        _fail_run_row(db, run, now, "执行超时未完成")


def _redispatch_queued(db: Session, now: datetime, dispatch_fn) -> int:
    cutoff = now - QUEUED_REDISPATCH_AFTER
    queued = (
        db.query(SuperAssistantScheduledRun)
        .filter(
            SuperAssistantScheduledRun.status == "queued",
            SuperAssistantScheduledRun.started_at.is_(None),
            SuperAssistantScheduledRun.created_at <= cutoff,
        )
        .limit(50)
        .all()
    )
    sent = 0
    for run in queued:
        try:
            dispatch_fn(run.id)
            sent += 1
        except Exception as exc:
            _fail_run_row(db, run, now, f"派发失败：{exc}")
            logger.exception("定时任务排队重派失败 run=%s", run.id)
    return sent


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


def _notify(db: Session, *, run: SuperAssistantScheduledRun, task: SuperAssistantScheduledTask, kind: str, title: str, summary: str) -> None:
    href = INBOX_HREF.format(task_id=task.id, run_id=run.id)
    publish_event(db, InboxEventIn(
        eventId=f"super-assistant-scheduled:{run.id}:{kind}",
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
    """扫描到期计划并派发。返回本次尝试派发的条数。"""
    from app.data_channel.pipeline_tasks.dispatch import dispatch_super_assistant_scheduled_run

    now = as_utc_naive(now or datetime.now(timezone.utc))
    db = SessionLocal()
    dispatched = 0
    try:
        _reap_stale_runs(db, now)
        dispatched += _redispatch_queued(db, now, dispatch_super_assistant_scheduled_run)
        db.commit()
        due = (
            db.query(SuperAssistantScheduledTask)
            .filter(
                SuperAssistantScheduledTask.enabled.is_(True),
                SuperAssistantScheduledTask.next_run_at.isnot(None),
                SuperAssistantScheduledTask.next_run_at <= now,
            )
            .order_by(SuperAssistantScheduledTask.next_run_at.asc())
            .limit(50)
            .all()
        )
        for task in due:
            try:
                dispatched += _dispatch_one(db, task, now, dispatch_super_assistant_scheduled_run)
            except Exception:  # noqa: BLE001 — 单条失败不影响其余
                db.rollback()
                logger.exception("定时任务派发失败 task=%s", task.id)
        return dispatched
    finally:
        db.close()


def _dispatch_one(db: Session, task: SuperAssistantScheduledTask, now: datetime, dispatch_fn) -> int:
    slot = task.next_run_at
    if slot is None:
        return 0
    overdue = (now - slot) > OVERDUE_GRACE and task.schedule_kind != "once"
    overlapping = _in_flight(db, task.id)
    if overlapping:
        status, error = "skipped", "上次执行尚未结束，本次已跳过"
    elif overdue:
        status, error = "skipped", "错过触发窗口，已跳过补跑"
    else:
        status, error = "queued", None
    run = SuperAssistantScheduledRun(
        task_id=task.id,
        owner_id=task.owner_id,
        scheduled_for=slot,
        status=status,
        error=error,
        finished_at=now if status == "skipped" else None,
    )
    db.add(run)
    task.last_dispatched_at = now
    task.updated_at = now
    _advance_schedule(task, now)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(SuperAssistantScheduledRun)
            .filter(
                SuperAssistantScheduledRun.task_id == task.id,
                SuperAssistantScheduledRun.scheduled_for == slot,
            )
            .first()
        )
        if existing is not None and existing.status == "queued":
            try:
                dispatch_fn(existing.id)
                return 1
            except Exception:
                logger.exception("定时任务冲突槽位重派失败 run=%s", existing.id)
        logger.info("定时任务时段已派发，跳过重复 task=%s slot=%s", task.id, slot)
        return 0
    db.refresh(run)
    if status != "queued":
        return 0
    try:
        dispatch_fn(run.id)
    except Exception as exc:
        _fail_run_row(db, run, _now(), f"派发失败：{exc}")
        db.commit()
        logger.exception("定时任务 NATS 派发失败 run=%s", run.id)
        return 0
    return 1


def execute_run(run_id: str) -> None:
    """nats_executor 消费入口：认领 queued 行并无人值守跑完对话。"""
    db = SessionLocal()
    assistant_id = None
    owner_id = None
    conversation_id = None
    task_title = None
    task_id = None
    try:
        claimed = (
            db.query(SuperAssistantScheduledRun)
            .filter(
                SuperAssistantScheduledRun.id == run_id,
                SuperAssistantScheduledRun.status == "queued",
            )
            .update(
                {"status": "running", "started_at": _now()},
                synchronize_session=False,
            )
        )
        db.commit()
        if claimed != 1:
            logger.info("定时任务执行未认领 run=%s", run_id)
            return
        run = db.get(SuperAssistantScheduledRun, run_id)
        if run is None:
            return
        task = db.get(SuperAssistantScheduledTask, run.task_id)
        if task is None:
            _fail_run_row(db, run, _now(), "计划已删除")
            db.commit()
            return
        conversation = SuperAssistantConversation(
            owner_id=task.owner_id,
            title=_conversation_title(task, run.scheduled_for),
        )
        db.add(conversation)
        db.flush()
        user_message = SuperAssistantMessage(
            conversation_id=conversation.id,
            role="user",
            content=task.instruction,
            status="complete",
        )
        assistant_message = SuperAssistantMessage(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="streaming",
        )
        db.add_all([user_message, assistant_message])
        run.conversation_id = conversation.id
        db.commit()
        assistant_id = assistant_message.id
        owner_id = task.owner_id
        conversation_id = conversation.id
        task_title = task.title
        task_id = task.id
    finally:
        db.close()

    if not assistant_id or not owner_id or not conversation_id:
        return

    error_text = None
    try:
        for _ in stream_chat(
            conversation_id=conversation_id,
            owner_id=owner_id,
            assistant_message_id=assistant_id,
            requested_model_id=None,
            agent_mode=True,
            unattended=True,
        ):
            pass
    except Exception as exc:  # noqa: BLE001 — 执行失败写回 run，不向上抛以免 NATS 空烧
        error_text = str(exc)
        logger.exception("定时任务执行失败 run=%s", run_id)

    db = SessionLocal()
    try:
        run = (
            db.query(SuperAssistantScheduledRun)
            .filter(
                SuperAssistantScheduledRun.id == run_id,
                SuperAssistantScheduledRun.status == "running",
            )
            .first()
        )
        if run is None:
            return
        task = db.get(SuperAssistantScheduledTask, task_id) if task_id else None
        assistant = db.get(SuperAssistantMessage, assistant_id)
        finished = _now()
        run.finished_at = finished
        if error_text:
            run.status = "failed"
            run.error = error_text[:2000]
            if assistant and assistant.status == "streaming":
                assistant.status = "error"
                assistant.content = assistant.content or error_text
        elif assistant is None or assistant.status != "complete":
            run.status = "failed"
            run.error = (assistant.content if assistant else None) or "执行未正常结束"
            run.result_summary = (assistant.content or "")[:RESULT_SUMMARY_CHARS] if assistant else None
        else:
            run.status = "completed"
            run.error = None
            run.result_summary = (assistant.content or "")[:RESULT_SUMMARY_CHARS]
        db.commit()
        if task is not None:
            kind = "notice" if run.status == "completed" else "alert"
            title = (
                f"定时任务已完成：{task_title}"
                if run.status == "completed"
                else f"定时任务失败：{task_title}"
            )
            summary = run.result_summary or run.error or ""
            _notify(db, run=run, task=task, kind=kind, title=title, summary=summary)
            db.commit()
    finally:
        db.close()


def _conversation_title(task: SuperAssistantScheduledTask, scheduled_for: datetime) -> str:
    local = as_utc_naive(scheduled_for).replace(tzinfo=timezone.utc).astimezone(SHANGHAI)
    stamp = f"{local.month:02d}-{local.day:02d} {local.hour:02d}:{local.minute:02d}"
    base = task.title.strip() or "定时任务"
    title = f"{base} · {stamp}"
    return title[:200]
