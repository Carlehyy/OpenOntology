"""超级助手用户定时任务：调度计算、CRUD、到期派发、无人值守执行。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMessage,
    SuperAssistantScheduledRun,
    SuperAssistantScheduledTask,
)
from app.super_assistant.scheduled_service import (
    INBOX_HREF,
    REAP_ERROR,
    STALE_RUNNING,
    ScheduledTaskError,
    ScheduledTaskNotFoundError,
    as_utc_naive,
    compute_next_run_at,
    create_task,
    delete_task,
    dispatch_due_tasks,
    execute_run,
    get_run,
    get_task,
    list_runs,
    list_tasks,
    parse_user_run_at,
    update_task,
)
from app.super_assistant.schemas import ScheduledTaskCreate, ScheduledTaskOut, ScheduledTaskUpdate

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduled.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            SuperAssistantConversation.__table__,
            SuperAssistantMessage.__table__,
            SuperAssistantScheduledTask.__table__,
            SuperAssistantScheduledRun.__table__,
        ],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _user(db, user_id="owner-1"):
    user = User(
        id=user_id, username=user_id, email=f"{user_id}@example.com",
        password_hash="unused", role="editor",
    )
    db.add(user)
    db.commit()
    return user


def test_compute_next_run_at_once_daily_weekly():
    after = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)  # 上海 09:00
    once = datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc)
    assert compute_next_run_at(kind="once", after=after, run_at=once) == as_utc_naive(once)
    assert compute_next_run_at(kind="once", after=once, run_at=once) is None

    daily = compute_next_run_at(kind="daily", after=after, hour=9, minute=0)
    local = daily.replace(tzinfo=timezone.utc).astimezone(SHANGHAI)
    assert (local.hour, local.minute) == (9, 0)
    assert local.date() == (after.astimezone(SHANGHAI).date() + timedelta(days=1))

    # 2026-09-17 是周四(3)；weekday=0 周一 → 下周一
    weekly = compute_next_run_at(kind="weekly", after=after, hour=9, minute=30, weekday=0)
    local = weekly.replace(tzinfo=timezone.utc).astimezone(SHANGHAI)
    assert local.weekday() == 0
    assert (local.hour, local.minute) == (9, 30)


def test_crud_and_owner_isolation(tmp_path):
    Session = _session(tmp_path)
    with Session() as db:
        _user(db, "owner-1")
        _user(db, "owner-2")
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        created = create_task(db, "owner-1", ScheduledTaskCreate(
            title="晚报", instruction="整理今日进展",
            schedule_kind="once", run_at=future,
        ))
        assert created.title == "晚报"
        assert created.enabled is True
        assert created.next_run_at is not None
        ScheduledTaskOut.model_validate(created)
        assert list_tasks(db, "owner-2") == []
        assert list_tasks(db, "owner-1")[0].id == created.id

        updated = update_task(db, "owner-1", created.id, ScheduledTaskUpdate(enabled=False))
        assert updated.enabled is False
        assert updated.next_run_at is None

        with pytest.raises(ScheduledTaskNotFoundError):
            get_task(db, "owner-2", created.id)
        delete_task(db, "owner-1", created.id)
        assert list_tasks(db, "owner-1") == []


def test_daily_title_falls_back_to_instruction(tmp_path):
    Session = _session(tmp_path)
    with Session() as db:
        _user(db)
        created = create_task(db, "owner-1", ScheduledTaskCreate(
            instruction="根据昨天的会话整理站会纪要。",
            schedule_kind="daily", hour=9, minute=0,
        ))
        assert created.title.startswith("根据昨天的会话整理站会纪要")
        assert created.hour == 9


def test_dispatch_due_creates_run_and_disables_once(tmp_path, monkeypatch):
    Session = _session(tmp_path)
    sent = []
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_super_assistant_scheduled_run",
        lambda run_id: sent.append(run_id),
    )
    with Session() as db:
        _user(db)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="一次", instruction="做完这件事",
            schedule_kind="once", timezone="Asia/Shanghai",
            run_at=as_utc_naive(past), enabled=True, next_run_at=as_utc_naive(past),
        )
        db.add(task)
        db.commit()
        task_id = task.id

    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    assert dispatch_due_tasks() == 1
    assert sent

    with Session() as db:
        task = db.get(SuperAssistantScheduledTask, task_id)
        assert task.enabled is False
        assert task.next_run_at is None
        runs = db.query(SuperAssistantScheduledRun).all()
        assert len(runs) == 1
        assert runs[0].status == "queued"
        assert runs[0].id == sent[0]


def test_dispatch_skips_when_previous_run_in_flight(tmp_path, monkeypatch):
    Session = _session(tmp_path)
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_super_assistant_scheduled_run",
        lambda run_id: None,
    )
    now = datetime.now(timezone.utc)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="每日", instruction="日报",
            schedule_kind="daily", timezone="Asia/Shanghai",
            hour=9, minute=0, enabled=True, next_run_at=as_utc_naive(now),
        )
        db.add(task)
        db.flush()
        db.add(SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(now - timedelta(days=1)),
            status="running", started_at=as_utc_naive(now),
        ))
        db.commit()
        task_id = task.id

    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    assert dispatch_due_tasks() == 0
    with Session() as db:
        runs = db.query(SuperAssistantScheduledRun).filter(
            SuperAssistantScheduledRun.task_id == task_id,
        ).all()
        assert {row.status for row in runs} == {"running", "skipped"}


def test_execute_run_creates_conversation_and_records_summary(tmp_path, monkeypatch):
    Session = _session(tmp_path)

    def fake_stream_chat(**kwargs):
        assert kwargs["unattended"] is True
        assert kwargs["agent_mode"] is True
        db = Session()
        try:
            message = db.get(SuperAssistantMessage, kwargs["assistant_message_id"])
            message.content = "整理完成：三项待办。"
            message.status = "complete"
            db.commit()
        finally:
            db.close()
        if False:
            yield "unused"

    monkeypatch.setattr("app.super_assistant.scheduled_service.stream_chat", fake_stream_chat)
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)

    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="晚报", instruction="整理今日进展",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(datetime.now(timezone.utc)),
            status="queued",
        )
        db.add(run)
        db.commit()
        run_id, task_id = run.id, task.id

    execute_run(run_id)
    with Session() as db:
        run = get_run(db, "owner-1", task_id, run_id)
        assert run.status == "completed"
        assert run.conversation_id
        assert run.result_summary == "整理完成：三项待办。"
        conversation = db.get(SuperAssistantConversation, run.conversation_id)
        assert conversation.owner_id == "owner-1"
        assert "晚报" in conversation.title
        messages = list_runs(db, "owner-1", task_id)
        assert messages[0].id == run_id


def test_dispatch_subject_and_handler_registration(monkeypatch):
    from app.data_channel.pipeline_tasks.dispatch import (
        PIPELINE_STREAM_SUBJECTS,
        SUPER_ASSISTANT_SCHEDULED_RUN_SUBJECT,
        dispatch_super_assistant_scheduled_run,
    )
    from app.data_channel.pipeline_tasks import dispatch as dispatch_module
    from app.data_channel.pipeline_tasks import nats_executor
    from app.super_assistant import scheduled_tasks

    assert SUPER_ASSISTANT_SCHEDULED_RUN_SUBJECT == "super_assistant.scheduled.run"
    # 流扩容只能追加：本体文档手动重建为当前尾项，scheduled.run 让出尾位
    # 但必须仍在流内（旧 subject 与旧 durable 不变是消费兼容的硬约束）
    assert PIPELINE_STREAM_SUBJECTS[-1] == "super_assistant.palace.ontology-rebuild"
    assert SUPER_ASSISTANT_SCHEDULED_RUN_SUBJECT in PIPELINE_STREAM_SUBJECTS
    registry = {
        subject: (durable, handler)
        for subject, durable, handler in nats_executor._handler_registry()
    }
    assert registry[SUPER_ASSISTANT_SCHEDULED_RUN_SUBJECT] == (
        "super-assistant-scheduled-run",
        scheduled_tasks.run_scheduled_task_message,
    )
    sent = []
    monkeypatch.setattr(dispatch_module, "dispatch_task", lambda subject, payload: sent.append((subject, payload)))
    dispatch_super_assistant_scheduled_run("run-1")
    assert sent == [("super_assistant.scheduled.run", {"run_id": "run-1"})]


def test_inbox_href_is_hash_router_path():
    assert INBOX_HREF.startswith("/super-assistant?")
    assert "/#" not in INBOX_HREF


def test_parse_user_run_at_naive_is_shanghai():
    parsed = parse_user_run_at(datetime(2026, 9, 18, 9, 0, 0))
    assert parsed == datetime(2026, 9, 18, 1, 0, 0)
    aware = parse_user_run_at(datetime(2026, 9, 18, 9, 0, tzinfo=SHANGHAI))
    assert aware == datetime(2026, 9, 18, 1, 0, 0)


def test_create_rejects_past_once(tmp_path):
    Session = _session(tmp_path)
    with Session() as db:
        _user(db)
        with pytest.raises(ScheduledTaskError, match="触发时间已过"):
            create_task(db, "owner-1", ScheduledTaskCreate(
                title="过期", instruction="不要跑",
                schedule_kind="once",
                run_at=datetime.now(timezone.utc) - timedelta(hours=2),
            ))


def test_overdue_daily_is_skipped_not_fired(tmp_path, monkeypatch):
    Session = _session(tmp_path)
    sent = []
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_super_assistant_scheduled_run",
        lambda run_id: sent.append(run_id),
    )
    now = datetime.now(timezone.utc)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="每日", instruction="日报",
            schedule_kind="daily", timezone="Asia/Shanghai",
            hour=9, minute=0, enabled=True,
            next_run_at=as_utc_naive(now - timedelta(days=3)),
        )
        db.add(task)
        db.commit()
        task_id = task.id
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    assert dispatch_due_tasks() == 0
    with Session() as db:
        runs = db.query(SuperAssistantScheduledRun).filter(
            SuperAssistantScheduledRun.task_id == task_id,
        ).all()
        assert len(runs) == 1
        assert runs[0].status == "skipped"
        assert "错过" in (runs[0].error or "")
    assert sent == []


def test_queued_unpublished_run_is_redispatched(tmp_path, monkeypatch):
    Session = _session(tmp_path)
    sent = []
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_super_assistant_scheduled_run",
        lambda run_id: sent.append(run_id),
    )
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event", lambda *a, **k: None)
    now = datetime.now(timezone.utc)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="一次", instruction="做",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(now),
            status="queued",
            created_at=as_utc_naive(now - timedelta(minutes=2)),
        )
        db.add(run)
        db.commit()
        run_id = run.id
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    assert dispatch_due_tasks() == 1
    assert sent == [run_id]


def test_execute_run_claims_once(tmp_path, monkeypatch):
    Session = _session(tmp_path)
    calls = []

    def fake_stream_chat(**kwargs):
        calls.append(kwargs["assistant_message_id"])
        db = Session()
        try:
            message = db.get(SuperAssistantMessage, kwargs["assistant_message_id"])
            message.content = "ok"
            message.status = "complete"
            db.commit()
        finally:
            db.close()
        if False:
            yield "unused"

    monkeypatch.setattr("app.super_assistant.scheduled_service.stream_chat", fake_stream_chat)
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event", lambda *a, **k: None)
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="晚报", instruction="整理",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(datetime.now(timezone.utc)),
            status="queued",
        )
        db.add(run)
        db.commit()
        run_id = run.id
    execute_run(run_id)
    execute_run(run_id)
    assert len(calls) == 1
    with Session() as db:
        conversations = db.query(SuperAssistantConversation).all()
        assert len(conversations) == 1


def test_reap_spares_running_run_with_recent_started_at(tmp_path, monkeypatch):
    """排队很久（created_at 超阈值）但真正开跑不久的 running 不被收割。"""
    Session = _session(tmp_path)
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_super_assistant_scheduled_run",
        lambda run_id: None,
    )
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event", lambda *a, **k: None)
    now = datetime.now(timezone.utc)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="每日", instruction="日报",
            schedule_kind="daily", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(now - STALE_RUNNING - timedelta(minutes=10)),
            status="running",
            created_at=as_utc_naive(now - STALE_RUNNING - timedelta(minutes=10)),
            started_at=as_utc_naive(now - timedelta(minutes=5)),
        )
        db.add(run)
        db.commit()
        run_id = run.id

    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    assert dispatch_due_tasks() == 0
    with Session() as db:
        run = db.get(SuperAssistantScheduledRun, run_id)
        assert run.status == "running"


def test_reap_fails_running_run_with_stale_started_at(tmp_path, monkeypatch):
    """开跑超过 STALE_RUNNING 的 running 会被收割为超时失败。"""
    Session = _session(tmp_path)
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_super_assistant_scheduled_run",
        lambda run_id: None,
    )
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event", lambda *a, **k: None)
    now = datetime.now(timezone.utc)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="每日", instruction="日报",
            schedule_kind="daily", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        stale = as_utc_naive(now - STALE_RUNNING - timedelta(minutes=1))
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=stale, status="running",
            created_at=stale, started_at=stale,
        )
        db.add(run)
        db.commit()
        run_id = run.id

    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    assert dispatch_due_tasks() == 0
    with Session() as db:
        run = db.get(SuperAssistantScheduledRun, run_id)
        assert run.status == "failed"
        assert run.error == REAP_ERROR


def test_execute_run_overwrites_reaped_run_with_real_result(tmp_path, monkeypatch):
    """执行期间被收割成「执行超时未完成」的运行，跑完后按真实终态覆盖并更正通知。"""
    Session = _session(tmp_path)
    notices = []

    def fake_publish_event(db, event):
        notices.append((event.event_id, event.item.title))

    def fake_stream_chat(**kwargs):
        db = Session()
        try:
            message = db.get(SuperAssistantMessage, kwargs["assistant_message_id"])
            message.content = "整理完成：三项待办。"
            message.status = "complete"
            # 模拟收割器在执行期间介入：本行被标成超时失败（含已发出的假失败通知前提）
            run = db.query(SuperAssistantScheduledRun).filter(
                SuperAssistantScheduledRun.conversation_id == kwargs["conversation_id"],
            ).one()
            run.status = "failed"
            run.error = REAP_ERROR
            run.finished_at = as_utc_naive(datetime.now(timezone.utc))
            db.commit()
        finally:
            db.close()
        if False:
            yield "unused"

    monkeypatch.setattr("app.super_assistant.scheduled_service.stream_chat", fake_stream_chat)
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event", fake_publish_event)
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="晚报", instruction="整理今日进展",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(datetime.now(timezone.utc)),
            status="queued",
        )
        db.add(run)
        db.commit()
        run_id, task_id = run.id, task.id

    execute_run(run_id)
    with Session() as db:
        run = get_run(db, "owner-1", task_id, run_id)
        assert run.status == "completed"
        assert run.error is None
        assert run.result_summary == "整理完成：三项待办。"
    # 更正通知的 eventId 必须带 -corrected 后缀：与收割 alert 同 eventId 不同
    # payload 会被收件箱收据校验拒收（ValueError），更正通知就永远发不出去。
    assert any(
        event_id.endswith(":notice-corrected") and title.startswith("定时任务已完成")
        for event_id, title in notices
    )


def test_execute_run_overwrites_reaped_run_with_real_failure(tmp_path, monkeypatch):
    """收割误报后执行真实失败：终态与 error 用真实值覆盖，更正 alert 不撞收据。"""
    Session = _session(tmp_path)
    notices = []

    def fake_publish_event(db, event):
        notices.append((event.event_id, event.item.title))

    def fake_stream_chat(**kwargs):
        db = Session()
        try:
            run = db.query(SuperAssistantScheduledRun).filter(
                SuperAssistantScheduledRun.conversation_id == kwargs["conversation_id"],
            ).one()
            run.status = "failed"
            run.error = REAP_ERROR
            run.finished_at = as_utc_naive(datetime.now(timezone.utc))
            db.commit()
        finally:
            db.close()
        raise RuntimeError("模型通道断开")

    monkeypatch.setattr("app.super_assistant.scheduled_service.stream_chat", fake_stream_chat)
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event", fake_publish_event)
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="晚报", instruction="整理今日进展",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(datetime.now(timezone.utc)),
            status="queued",
        )
        db.add(run)
        db.commit()
        run_id, task_id = run.id, task.id

    execute_run(run_id)
    with Session() as db:
        run = get_run(db, "owner-1", task_id, run_id)
        assert run.status == "failed"
        assert "模型通道断开" in (run.error or "")
        assert run.error != REAP_ERROR
    assert any(
        event_id.endswith(":alert-corrected") and title.startswith("定时任务失败")
        for event_id, title in notices
    )


def test_reap_falls_back_to_created_at_when_started_at_missing(tmp_path, monkeypatch):
    """running 行缺 started_at（历史脏数据）时按 created_at 收割兜底。"""
    Session = _session(tmp_path)
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_super_assistant_scheduled_run",
        lambda run_id: None,
    )
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event", lambda *a, **k: None)
    now = datetime.now(timezone.utc)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="每日", instruction="日报",
            schedule_kind="daily", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        stale = as_utc_naive(now - STALE_RUNNING - timedelta(minutes=1))
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=stale, status="running",
            created_at=stale, started_at=None,
        )
        db.add(run)
        db.commit()
        run_id = run.id

    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    assert dispatch_due_tasks() == 0
    with Session() as db:
        run = db.get(SuperAssistantScheduledRun, run_id)
        assert run.status == "failed"
        assert run.error == REAP_ERROR


def test_recover_interrupted_streams_window_matches_stale_running(tmp_path, monkeypatch):
    """重启保护窗口与收割阈值同源：30 分钟~2 小时之间的 running 会话仍受保护，
    超过 STALE_RUNNING 的不再保护——锁住「两处时钟必须一致」的承诺。"""
    Session = _session(tmp_path)
    now = datetime.now(timezone.utc)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="每日", instruction="日报",
            schedule_kind="daily", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        for label, started_ago in (("recent", timedelta(minutes=45)), ("stale", STALE_RUNNING + timedelta(minutes=10))):
            conversation = SuperAssistantConversation(owner_id="owner-1", title=label)
            db.add(conversation)
            db.flush()
            db.add(SuperAssistantMessage(
                conversation_id=conversation.id, role="assistant",
                content="", status="streaming",
            ))
            db.add(SuperAssistantScheduledRun(
                task_id=task.id, owner_id="owner-1",
                scheduled_for=as_utc_naive(now - started_ago),
                status="running",
                created_at=as_utc_naive(now - started_ago),
                started_at=as_utc_naive(now - started_ago),
                conversation_id=conversation.id,
            ))
        db.commit()

    monkeypatch.setattr("app.super_assistant.conversation_service.SessionLocal", Session)
    from app.super_assistant.conversation_service import recover_interrupted_streams
    result = recover_interrupted_streams()
    assert result == {"interrupted": 1}
    with Session() as db:
        statuses = {
            conversation.title: status
            for conversation, status in db.query(
                SuperAssistantConversation, SuperAssistantMessage.status,
            ).join(
                SuperAssistantMessage, SuperAssistantMessage.conversation_id == SuperAssistantConversation.id,
            ).all()
        }
    assert statuses["recent"] == "streaming"
    assert statuses["stale"] == "error"
