"""用户日程：保留调度契约，全部目标事务化生成 AutoLoop 事件。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant.memory_models import (
    SuperAssistantMemorySetting,
    SuperAssistantMemorySource,
)
from app.super_assistant.autoloop.models import (
    AUTOLOOP_TABLES,
    AutoLoopDispatch,
    AutoLoopEvent,
    AutoLoopReceipt,
)
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMessage,
    SuperAssistantScheduledRun,
    SuperAssistantScheduledTask,
)
from app.task_instances.models import (
    TaskApproval,
    TaskArtifact,
    TaskEvent,
    TaskInstance,
    TaskNodeRun,
    TaskTemplate,
    TaskTemplateRevision,
)
from app.task_instances.spec import compile_workflow_yaml
from tests.task_instances.conftest import make_spec
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
from app.super_assistant.schemas import (
    ScheduledRunOut, ScheduledTaskCreate, ScheduledTaskOut, ScheduledTaskUpdate,
)

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
            # 模板目标（触发对象=流程模板）链路需要任务实例域的表
            TaskTemplate.__table__,
            TaskTemplateRevision.__table__,
            TaskInstance.__table__,
            TaskNodeRun.__table__,
            TaskApproval.__table__,
            TaskArtifact.__table__,
            TaskEvent.__table__,
            # 证明事件接收不创建会话、实例或用户记忆来源。
            SuperAssistantMemorySource.__table__,
            SuperAssistantMemorySetting.__table__,
            *AUTOLOOP_TABLES,
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
        "app.data_channel.pipeline_tasks.dispatch.dispatch_autoloop",
        lambda dispatch_id, version: sent.append((dispatch_id, version)),
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
        assert runs[0].status == "completed"
        assert runs[0].result_summary == "已生成AutoLoop事件，等待规则路由"
        receipt = db.query(AutoLoopReceipt).one()
        event = db.query(AutoLoopEvent).filter_by(receipt_id=receipt.id).one()
        dispatch = db.query(AutoLoopDispatch).one()
        assert receipt.scheduled_task_id == task_id
        assert receipt.scheduled_run_id == runs[0].id
        assert dispatch.event_id == event.id
        assert sent == [(dispatch.id, dispatch.version)]
        assert db.query(SuperAssistantConversation).count() == 0
        assert db.query(TaskInstance).count() == 0
    assert dispatch_due_tasks() == 0
    assert len(sent) == 1


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


@pytest.mark.parametrize("target_kind", ["super_assistant", "task_template"])
@pytest.mark.parametrize("memory_enabled", [True, False])
def test_execute_run_admits_event_only_and_replays_original_identity(
    tmp_path, monkeypatch, target_kind, memory_enabled,
):
    Session = _session(tmp_path)
    sent = []
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)

    def publish(dispatch_id, version):
        # Publishing can only observe a fully committed run and admission.
        with Session() as db:
            dispatch = db.get(AutoLoopDispatch, dispatch_id)
            event = db.query(AutoLoopEvent).filter_by(id=dispatch.event_id).one()
            receipt = db.get(AutoLoopReceipt, event.receipt_id)
            assert db.get(SuperAssistantScheduledRun, receipt.scheduled_run_id).status == "completed"
        sent.append((dispatch_id, version))

    monkeypatch.setattr("app.data_channel.pipeline_tasks.dispatch.dispatch_autoloop", publish)
    with Session() as db:
        _user(db)
        db.add(SuperAssistantMemorySetting(owner_id="owner-1", enabled=memory_enabled))
        template_id = _template(db).id if target_kind == "task_template" else None
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="晚报", instruction="整理今日进展",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=False,
            target_kind=target_kind, target_template_id=template_id,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(datetime.now(timezone.utc)), status="queued",
        )
        db.add(run)
        db.commit()
        run_id, task_id = run.id, task.id

    execute_run(run_id)
    execute_run(run_id)
    with Session() as db:
        run = get_run(db, "owner-1", task_id, run_id)
        assert run.status == "completed"
        assert run.result_summary == "已生成AutoLoop事件，等待规则路由"
        assert run.conversation_id is run.instance_id is None
        receipt = db.query(AutoLoopReceipt).one()
        event = db.query(AutoLoopEvent).filter_by(receipt_id=receipt.id).one()
        dispatch = db.query(AutoLoopDispatch).one()
        assert receipt.owner_id == event.owner_id == dispatch.owner_id == "owner-1"
        assert event.data["instruction"] == "整理今日进展"
        assert event.data["targetKind"] == target_kind
        assert event.data["targetTemplateId"] == template_id
        assert event.data["timezone"] == "Asia/Shanghai"
        assert event.data["scheduledFor"].endswith("+00:00")
        assert sent == [(dispatch.id, dispatch.version)]
        dto = ScheduledRunOut.model_validate(run)
        assert dto.autoloopEventIds == [event.id]
        assert list_runs(db, "owner-1", task_id)[0].autoloopEventIds == [event.id]
        assert db.query(SuperAssistantConversation).count() == 0
        assert db.query(SuperAssistantMessage).count() == 0
        assert db.query(TaskInstance).count() == 0
        assert db.query(SuperAssistantMemorySource).count() == 0


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
    # 仅追加 AutoLoop；既有 subject 的顺序和 durable 保持不变。
    assert PIPELINE_STREAM_SUBJECTS[-5:] == (
        "task_instances.control", "integrated.sync", "ontology.assistant.run",
        "super_assistant.memory.sync", "super_assistant.autoloop.dispatch",
    )
    assert SUPER_ASSISTANT_SCHEDULED_RUN_SUBJECT in PIPELINE_STREAM_SUBJECTS
    registry = {
        subject: (durable, handler)
        for subject, durable, handler in nats_executor._handler_registry()
    }
    assert registry[SUPER_ASSISTANT_SCHEDULED_RUN_SUBJECT] == (
        "super-assistant-scheduled-run",
        scheduled_tasks.run_scheduled_task_message,
    )
    # 记忆同步 durable：pending 来源 → Hindsight 上游 retain 的派发通道
    memory_entry = registry["super_assistant.memory.sync"]
    assert memory_entry[0] == "sa-memory-sync"
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
        "app.data_channel.pipeline_tasks.dispatch.dispatch_autoloop",
        lambda dispatch_id, version: sent.append((dispatch_id, version)),
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
        "app.data_channel.pipeline_tasks.dispatch.dispatch_autoloop",
        lambda dispatch_id, version: sent.append((dispatch_id, version)),
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
    with Session() as db:
        work = db.query(AutoLoopDispatch).one()
        assert sent == [(work.id, work.version)]
        assert db.get(SuperAssistantScheduledRun, run_id).status == "completed"
    assert dispatch_due_tasks() == 0
    assert len(sent) == 1


def test_admission_failure_rolls_back_run_slot_and_facts(tmp_path, monkeypatch):
    from app.super_assistant import scheduled_service
    from app.super_assistant.autoloop.ingress import accept_scheduled_run

    Session = _session(tmp_path)
    now = as_utc_naive(datetime.now(timezone.utc))
    sent = []
    monkeypatch.setattr(scheduled_service, "SessionLocal", Session)
    monkeypatch.setattr("app.data_channel.pipeline_tasks.dispatch.dispatch_autoloop",
                        lambda *args: sent.append(args))
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="事务", instruction="整理",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=True, next_run_at=now,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    def fail_after_admission(db, task, run):
        accept_scheduled_run(db, task, run)
        raise RuntimeError("admission write interrupted")

    monkeypatch.setattr(scheduled_service, "accept_scheduled_run",
                        fail_after_admission)
    assert dispatch_due_tasks(now) == 0
    with Session() as db:
        task = db.get(SuperAssistantScheduledTask, task_id)
        assert task.enabled is True
        assert task.next_run_at == now
        assert db.query(SuperAssistantScheduledRun).count() == 0
        assert db.query(AutoLoopReceipt).count() == 0
        assert db.query(AutoLoopEvent).count() == 0
        assert db.query(AutoLoopDispatch).count() == 0
    assert sent == []
    monkeypatch.setattr(scheduled_service, "accept_scheduled_run",
                        accept_scheduled_run)
    assert dispatch_due_tasks(now) == 1
    with Session() as db:
        assert db.query(SuperAssistantScheduledRun).one().status == "completed"
        assert db.query(AutoLoopDispatch).count() == 1


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


def test_committed_schedule_recovers_same_dispatch_after_publish_unknown(tmp_path, monkeypatch):
    from app.super_assistant.autoloop.dispatching import scan_pending

    Session = _session(tmp_path)
    attempts = []

    def accepted_then_timeout(dispatch_id, version):
        attempts.append((dispatch_id, version))
        raise TimeoutError("broker acceptance unknown")

    monkeypatch.setattr("app.data_channel.pipeline_tasks.dispatch.dispatch_autoloop",
                        accepted_then_timeout)
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="恢复", instruction="整理今日进展",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(datetime.now(timezone.utc)), status="queued",
        )
        db.add(run)
        db.commit()
        run_id, task_id = run.id, task.id

    execute_run(run_id)
    with Session() as db:
        run = get_run(db, "owner-1", task_id, run_id)
        assert run.status == "completed"
        work = db.query(AutoLoopDispatch).one()
        original = (work.id, work.version)
        assert work.state == "pending"
    assert attempts == [original]
    recovered = []
    assert scan_pending(session_factory=Session,
                        publish=lambda *args: recovered.append(args)) == 1
    assert recovered == [original]
    execute_run(run_id)
    with Session() as db:
        assert db.query(AutoLoopReceipt).count() == 1
        assert db.query(AutoLoopDispatch).count() == 1
        assert db.query(SuperAssistantConversation).count() == 0
        assert db.query(TaskInstance).count() == 0


@pytest.mark.parametrize("status", ["running", "completed", "failed", "skipped"])
def test_legacy_delivery_never_reexecutes_running_or_terminal_run(tmp_path, monkeypatch, status):
    """旧投递不能把超时、已有执行或终态当作从未执行而重做。"""
    Session = _session(tmp_path)
    sent = []
    monkeypatch.setattr("app.data_channel.pipeline_tasks.dispatch.dispatch_autoloop",
                        lambda *args: sent.append(args))
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    with Session() as db:
        _user(db)
        task = SuperAssistantScheduledTask(
            owner_id="owner-1", title="历史运行", instruction="整理今日进展",
            schedule_kind="once", timezone="Asia/Shanghai", enabled=False,
        )
        db.add(task)
        db.flush()
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1", status=status,
            scheduled_for=as_utc_naive(datetime.now(timezone.utc)),
            error=REAP_ERROR if status == "failed" else None,
            result_summary="historical execution result",
        )
        db.add(run)
        db.commit()
        run_id = run.id
    execute_run(run_id)
    with Session() as db:
        row = db.get(SuperAssistantScheduledRun, run_id)
        assert row.status == status
        assert row.result_summary == "historical execution result"
        assert row.error == (REAP_ERROR if status == "failed" else None)
        assert db.query(AutoLoopReceipt).count() == 0
    assert sent == []


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


# ---------------------------------------------------------------------------
# 触发对象=流程模板（target_kind=task_template）
# ---------------------------------------------------------------------------

def _template(db, name="例行汇总"):
    """在定时任务测试库造一个已编译流程模板（复用任务实例域夹具同款 YAML）。"""
    spec_yaml = make_spec()
    template = TaskTemplate(name=name)
    db.add(template)
    db.flush()
    compiled = compile_workflow_yaml(spec_yaml)
    revision = TaskTemplateRevision(
        template_id=template.id, revision_no=1,
        spec_yaml=spec_yaml, spec_compiled=compiled.canonical,
        canonical_hash=compiled.canonical_hash)
    db.add(revision)
    db.flush()
    template.latest_revision_id = revision.id
    db.commit()
    return template


def _future_body(**overrides):
    body = ScheduledTaskCreate(
        instruction="汇总昨天的待办", schedule_kind="once",
        run_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    for key, value in overrides.items():
        setattr(body, key, value)
    return body


def test_create_template_target_validation_and_name_backfill(tmp_path):
    Session = _session(tmp_path)
    with Session() as db:
        _user(db)
        template = _template(db)

        with pytest.raises(ScheduledTaskError, match="请选择流程模板"):
            create_task(db, "owner-1", _future_body(target_kind="task_template"))
        # 引用实体缺失 → 400 语义（ScheduledTaskError），与任务归属缺失的 404 区分
        with pytest.raises(ScheduledTaskError, match="流程模板不存在或已删除"):
            create_task(db, "owner-1", _future_body(
                target_kind="task_template", target_template_id="tpl-missing"))
        with pytest.raises(ScheduledTaskError, match="不需要选择流程模板"):
            create_task(db, "owner-1", _future_body(
                target_kind="super_assistant", target_template_id=template.id))

        created = create_task(db, "owner-1", _future_body(
            target_kind="task_template", target_template_id=template.id))
        assert created.target_kind == "task_template"
        assert created.target_template_id == template.id
        ScheduledTaskOut.model_validate(created)
        fetched = get_task(db, "owner-1", created.id)
        assert fetched.target_template_name == "例行汇总"

        # 模板软删后：读取侧名字回填为 None（前端显示「已删除」），创建侧拒绝
        template.deleted_at = datetime.now(timezone.utc)
        db.commit()
        assert get_task(db, "owner-1", created.id).target_template_name is None
        with pytest.raises(ScheduledTaskError, match="流程模板不存在或已删除"):
            create_task(db, "owner-1", _future_body(
                target_kind="task_template", target_template_id=template.id))


def test_create_template_target_rejects_instruction_over_goal_limit(tmp_path):
    Session = _session(tmp_path)
    with Session() as db:
        _user(db)
        template = _template(db)
        with pytest.raises(ScheduledTaskError, match="20000"):
            create_task(db, "owner-1", _future_body(
                instruction="长" * 20_001,
                target_kind="task_template", target_template_id=template.id))


def test_update_task_target_change_validates_and_merges(tmp_path):
    Session = _session(tmp_path)
    with Session() as db:
        _user(db)
        template = _template(db)
        created = create_task(db, "owner-1", _future_body())
        assert created.target_kind == "super_assistant"
        assert created.target_template_id is None

        switched = update_task(db, "owner-1", created.id, ScheduledTaskUpdate(
            target_kind="task_template", target_template_id=template.id))
        assert switched.target_kind == "task_template"
        assert switched.target_template_id == template.id
        assert switched.target_template_name == "例行汇总"

        # 切回超级助手时清空模板指针；合并后的任务内容超限同样拦截
        reverted = update_task(db, "owner-1", created.id, ScheduledTaskUpdate(
            target_kind="super_assistant"))
        assert reverted.target_template_id is None
        update_task(db, "owner-1", created.id, ScheduledTaskUpdate(
            target_kind="task_template", target_template_id=template.id))
        with pytest.raises(ScheduledTaskError, match="20000"):
            update_task(db, "owner-1", created.id, ScheduledTaskUpdate(
                instruction="长" * 20_001))


@pytest.mark.parametrize("template_deleted", [False, True])
def test_template_schedule_records_target_without_activating_instance(
    tmp_path, monkeypatch, template_deleted,
):
    Session = _session(tmp_path)
    monkeypatch.setattr("app.super_assistant.scheduled_service.SessionLocal", Session)
    monkeypatch.setattr("app.data_channel.pipeline_tasks.dispatch.dispatch_autoloop",
                        lambda *args: None)

    with Session() as db:
        _user(db)
        template = _template(db)
        task = create_task(db, "owner-1", _future_body(
            target_kind="task_template", target_template_id=template.id))
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=as_utc_naive(datetime.now(timezone.utc)), status="queued",
        )
        db.add(run)
        if template_deleted:
            template.deleted_at = datetime.now(timezone.utc)
        db.commit()
        run_id, task_id, template_id = run.id, task.id, template.id

    execute_run(run_id)
    execute_run(run_id)
    with Session() as db:
        run = get_run(db, "owner-1", task_id, run_id)
        assert run.status == "completed"
        assert run.result_summary == "已生成AutoLoop事件，等待规则路由"
        assert run.instance_id is run.conversation_id is None
        receipt = db.query(AutoLoopReceipt).one()
        event = db.query(AutoLoopEvent).filter_by(receipt_id=receipt.id).one()
        assert event.data["targetKind"] == "task_template"
        assert event.data["targetTemplateId"] == template_id
        assert event.data["instruction"] == "汇总昨天的待办"
        assert db.query(TaskInstance).count() == 0
        assert db.query(SuperAssistantConversation).count() == 0

def test_reap_reconciles_template_run_with_existing_instance(tmp_path, monkeypatch):
    """收割对账（对抗审查 F1）：activate 已成功、终态写回因进程崩溃丢失——
    run 卡 running 超 2h，收割器须按幂等键反查实例并补记完成，不记假失败。"""
    from app.super_assistant.scheduled_service import _reap_stale_runs
    from app.task_instances import engine as ti_engine

    Session = _session(tmp_path)
    monkeypatch.setattr("app.super_assistant.scheduled_service.publish_event",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr("app.task_instances.service._dispatch_node",
                        lambda node_run_id: None)

    with Session() as db:
        _user(db)
        template = _template(db)
        task = create_task(db, "owner-1", _future_body(
            target_kind="task_template", target_template_id=template.id))
        stale_since = as_utc_naive(datetime.now(timezone.utc) - timedelta(hours=3))
        run = SuperAssistantScheduledRun(
            task_id=task.id, owner_id="owner-1",
            scheduled_for=stale_since, status="running", started_at=stale_since,
        )
        db.add(run)
        db.commit()
        # 模拟崩溃前 activate 已成功：实例带同一幂等键落库
        revision = db.query(TaskTemplateRevision).filter_by(
            template_id=template.id).first()
        instance, _ = ti_engine.activate(
            db, revision=revision, name="崩溃前已建实例", goal=task.instruction,
            inputs=None, created_by=None, idempotency_key=f"sa-scheduled:{run.id}")
        db.commit()
        run_id, instance_id = run.id, instance.id

    with Session() as db:
        _reap_stale_runs(db, as_utc_naive(datetime.now(timezone.utc)))
        db.commit()
        row = db.get(SuperAssistantScheduledRun, run_id)
        assert row.status == "completed"
        assert row.instance_id == instance_id
        assert row.error is None
