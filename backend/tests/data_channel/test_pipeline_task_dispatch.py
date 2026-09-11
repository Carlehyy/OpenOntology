"""流水线任务 NATS 派发与调用点切换的契约测试。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.data_channel.pipeline_tasks import dispatch as dispatch_module
from app.data_channel.pipeline_tasks.dispatch import (
    DATASET_IMPORT_SUBJECT,
    PIPELINE_EXECUTE_SUBJECT,
    PIPELINE_RUN_SUBJECT,
    PIPELINE_STREAM,
    PIPELINE_STREAM_SUBJECTS,
    dispatch_pipeline_task,
    dispatch_task,
    ensure_pipeline_stream,
)
from app.data_channel.pipeline_tasks.execution_service import trigger_task
from app.data_channel.pipeline_tasks.models import PipelineTask
from app.data_channel.sync_tasks import scheduler as scheduler_module
from app.data_channel.sync_tasks.scheduler import SyncScheduler
from app.models.v2.pipeline import Pipeline


class _FakeJS:
    def __init__(
        self,
        calls,
        *,
        add_stream_error: Exception | None = None,
        existing_subjects: list[str] | None = None,
    ):
        self._calls = calls
        self._add_stream_error = add_stream_error
        self._existing_subjects = existing_subjects

    async def add_stream(self, config):
        self._calls.setdefault("add_stream", []).append(config)
        if self._add_stream_error is not None:
            raise self._add_stream_error

    async def stream_info(self, name):
        from types import SimpleNamespace

        self._calls.setdefault("stream_info", []).append(name)
        return SimpleNamespace(
            config=SimpleNamespace(subjects=list(self._existing_subjects or [])),
        )

    async def update_stream(self, config):
        self._calls.setdefault("update_stream", []).append(config)


class _FakeNC:
    def __init__(self, calls, **js_kwargs):
        self._js = _FakeJS(calls, **js_kwargs)
        self._calls = calls

    def jetstream(self):
        return self._js

    async def drain(self):
        self._calls["drained"] = True


@pytest.fixture
def fake_nats(monkeypatch):
    """替换 nats.connect 为记录型假客户端，返回记录字典。"""
    import nats

    calls: dict = {"published": []}

    class PublishingJS(_FakeJS):
        async def publish(self, subject, payload, headers=None):
            calls["published"].append((subject, payload, headers))

    class PublishingNC(_FakeNC):
        def __init__(self, calls_):
            super().__init__(calls_)
            self._js = PublishingJS(calls_)

    async def fake_connect(url, **kwargs):
        calls["connect"] = (url, kwargs)
        return PublishingNC(calls)

    monkeypatch.setattr(nats, "connect", fake_connect)
    monkeypatch.setattr(dispatch_module, "_stream_ensured", False)
    monkeypatch.setattr(
        "app.config.settings.nats_url", "nats://fake-nats:4222",
    )
    return calls


def test_dispatch_publishes_payload_subject_and_msg_id(fake_nats):
    dispatch_pipeline_task("task-1", "scheduled")

    url, kwargs = fake_nats["connect"]
    assert url == "nats://fake-nats:4222"
    assert kwargs["connect_timeout"] == 3

    (subject, payload, headers), = fake_nats["published"]
    assert subject == PIPELINE_EXECUTE_SUBJECT == "pipeline.task.execute"
    body = json.loads(payload.decode())
    assert body["task_id"] == "task-1"
    assert body["trigger_type"] == "scheduled"
    assert datetime.fromisoformat(body["dispatched_at"])
    assert re.fullmatch(r"task-1:scheduled:\d+", headers["Nats-Msg-Id"])
    assert fake_nats["drained"] is True


def test_dispatch_ensures_work_queue_stream_once(fake_nats, monkeypatch):
    from nats.js.api import RetentionPolicy

    dispatch_pipeline_task("task-1", "scheduled")
    dispatch_pipeline_task("task-2", "manual")

    (config,), = [fake_nats["add_stream"]]
    assert config.name == PIPELINE_STREAM == "PIPELINE_TASKS"
    # 流已扩容：旧 subject 保持不变，新增 UI 手动运行、数据集导入、
    # 超级助手三种反思任务、成品→人工迁移任务、记忆宫殿图谱抽取与
    # 定期聚类合并、本体发布文档事件（扩容只能追加，subject 顺序须与
    # PIPELINE_STREAM_SUBJECTS 一致）
    assert config.subjects == [
        "pipeline.task.execute",
        "task.pipeline.run",
        "task.dataset.import",
        "task.mapping.apply",
        "task.connection.sync",
        "task.dataset.event",
        "super_assistant.reflect.micro",
        "super_assistant.reflect.full",
        "super_assistant.reflect.focused",
        "task.dataset.migrate",
        "assistant_evaluation.autopilot.cycle",
        "super_assistant.palace.extract",
        "super_assistant.palace.consolidate",
        "ontology.documents.published",
    ]
    assert config.subjects == list(PIPELINE_STREAM_SUBJECTS)
    assert config.retention == RetentionPolicy.WORK_QUEUE
    assert config.max_age == 7 * 24 * 3600
    assert config.duplicate_window == 10 * 60
    # 进程内缓存：同一进程只确保一次 Stream
    assert len(fake_nats["published"]) == 2


def test_dispatch_task_publishes_generic_subject_payload_and_msg_id(fake_nats):
    dispatch_task(
        PIPELINE_RUN_SUBJECT,
        {"pipeline_id": "pipe-1", "run_id": "run-1"},
    )
    (subject, payload, headers), = fake_nats["published"]
    assert subject == PIPELINE_RUN_SUBJECT == "task.pipeline.run"
    body = json.loads(payload.decode())
    assert body["pipeline_id"] == "pipe-1"
    assert body["run_id"] == "run-1"
    assert datetime.fromisoformat(body["dispatched_at"])
    # Msg-Id 惯例：subject + 排序参数 + 纳秒时间戳
    assert re.fullmatch(
        r"task\.pipeline\.run:pipeline_id=pipe-1:run_id=run-1:\d+",
        headers["Nats-Msg-Id"],
    )
    assert fake_nats["drained"] is True


def test_dispatch_task_dataset_import_subject(fake_nats):
    dispatch_task(
        DATASET_IMPORT_SUBJECT,
        {"job_id": "job-1", "kind": "inspect"},
    )

    (subject, payload, headers), = fake_nats["published"]
    assert subject == DATASET_IMPORT_SUBJECT == "task.dataset.import"
    body = json.loads(payload.decode())
    assert body["job_id"] == "job-1"
    assert body["kind"] == "inspect"
    assert re.fullmatch(
        r"task\.dataset\.import:job_id=job-1:kind=inspect:\d+",
        headers["Nats-Msg-Id"],
    )


def test_dispatch_ontology_document_published_keeps_msg_id_compact(fake_nats):
    """本体文档事件自包含携带正文，但 Msg-Id 只含本体标识+指纹：
    dispatch_task 的全量值拼接惯例会把 document_md 复制进 NATS 消息头，
    大文档直接超 max_payload（对抗式审查回归）。"""
    from app.data_channel.pipeline_tasks.dispatch import (
        ONTOLOGY_DOCUMENT_PUBLISHED_SUBJECT,
        dispatch_ontology_document_published,
    )

    document_md = "# 大文档\n" + "内容" * 50_000  # 约 300KB，远超 Msg-Id 合理长度
    dispatch_ontology_document_published({
        "ontology_id": "ont-1",
        "ontology_name": "供应链本体",
        "version_id": "rel-2",
        "version_number": "v2",
        "title": "供应链业务文档",
        "document_md": document_md,
        "fingerprint": "fp-1",
        "published_at": None,
    })

    (subject, payload, headers), = fake_nats["published"]
    assert subject == ONTOLOGY_DOCUMENT_PUBLISHED_SUBJECT == "ontology.documents.published"
    body = json.loads(payload.decode())
    assert body["document_md"] == document_md  # 正文只在消息体
    msg_id = headers["Nats-Msg-Id"]
    assert len(msg_id) < 200
    assert "内容" not in msg_id
    assert re.fullmatch(r"ontology\.documents\.published:ont-1:fp-1:\d+", msg_id)


def test_dispatch_super_assistant_palace_consolidate(fake_nats):
    from app.data_channel.pipeline_tasks.dispatch import (
        SUPER_ASSISTANT_PALACE_CONSOLIDATE_SUBJECT,
        dispatch_super_assistant_palace_consolidate,
    )

    dispatch_super_assistant_palace_consolidate("user-1")

    (subject, payload, headers), = fake_nats["published"]
    assert subject == SUPER_ASSISTANT_PALACE_CONSOLIDATE_SUBJECT
    assert SUPER_ASSISTANT_PALACE_CONSOLIDATE_SUBJECT == "super_assistant.palace.consolidate"
    body = json.loads(payload.decode())
    assert body["owner_id"] == "user-1"
    assert datetime.fromisoformat(body["dispatched_at"])
    assert re.fullmatch(
        r"super_assistant\.palace\.consolidate:owner_id=user-1:\d+",
        headers["Nats-Msg-Id"],
    )
    assert fake_nats["drained"] is True


def test_dispatch_task_without_nats_url_fails_with_chinese_message(monkeypatch):
    monkeypatch.setattr("app.config.settings.nats_url", "")

    with pytest.raises(RuntimeError, match="未配置 NATS_URL"):
        dispatch_task(PIPELINE_RUN_SUBJECT, {"pipeline_id": "p", "run_id": "r"})


def test_dispatch_without_nats_url_fails_with_chinese_message(monkeypatch):
    monkeypatch.setattr("app.config.settings.nats_url", "")

    with pytest.raises(RuntimeError, match="未配置 NATS_URL"):
        dispatch_pipeline_task("task-1", "manual")


@pytest.mark.asyncio
async def test_ensure_stream_tolerates_already_exists():
    """流已存在且 subject 已是最新：不更新、不抛异常。"""
    calls: dict = {"add_stream": []}
    js = _FakeJS(
        calls,
        add_stream_error=Exception("stream name already in use"),
        existing_subjects=list(PIPELINE_STREAM_SUBJECTS),
    )

    await ensure_pipeline_stream(js)  # 不抛异常即为通过

    assert "update_stream" not in calls


@pytest.mark.asyncio
async def test_ensure_stream_evolves_legacy_stream_subjects():
    """生产旧流只有 pipeline.task.execute：合并 subjects 后原地更新。"""
    calls: dict = {"add_stream": []}
    js = _FakeJS(
        calls,
        add_stream_error=Exception("stream name already in use"),
        existing_subjects=["pipeline.task.execute"],
    )

    await ensure_pipeline_stream(js)

    (config,), = [calls["update_stream"]]
    # 旧 subject 保留在前，新增 subject 合并入流，旧 durable 不受影响
    assert config.subjects == sorted(set(PIPELINE_STREAM_SUBJECTS) | {"pipeline.task.execute"})
    assert set(config.subjects) == set(PIPELINE_STREAM_SUBJECTS)


@pytest.mark.asyncio
async def test_ensure_stream_update_preserves_unknown_subjects():
    """流上存在本版本未知的 subject 时保留不丢（前向兼容）。"""
    calls: dict = {"add_stream": []}
    js = _FakeJS(
        calls,
        add_stream_error=Exception("stream name already in use"),
        existing_subjects=["pipeline.task.execute", "task.future"],
    )

    await ensure_pipeline_stream(js)

    (config,), = [calls["update_stream"]]
    assert set(config.subjects) == set(PIPELINE_STREAM_SUBJECTS) | {"task.future"}


@pytest.mark.asyncio
async def test_ensure_stream_propagates_real_errors():
    calls: dict = {"add_stream": []}
    js = _FakeJS(calls, add_stream_error=Exception("permission denied"))

    with pytest.raises(Exception, match="permission denied"):
        await ensure_pipeline_stream(js)


def _published_pipeline(db) -> Pipeline:
    pipe = Pipeline(
        id="pipe-dispatch",
        name="派发流水线",
        spec={},
        status="published",
        enabled=True,
        column_definitions=[{"field_key": "id"}],
    )
    db.add(pipe)
    db.commit()
    return pipe


def _idle_task(db) -> PipelineTask:
    _published_pipeline(db)
    task = PipelineTask(
        id="task-dispatch",
        name="派发任务",
        description="",
        pipeline_id="pipe-dispatch",
        status="idle",
    )
    db.add(task)
    db.commit()
    return task


def test_pipeline_job_runner_dispatches_instead_of_executing(monkeypatch):
    dispatched = []

    def fake_dispatch(task_id, trigger_type):
        dispatched.append((task_id, trigger_type))

    def forbidden_execute(*_args, **_kwargs):  # pragma: no cover - 防御断言
        raise AssertionError("调度回调不得再内联执行 execute_pipeline_task")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_pipeline_task",
        fake_dispatch,
    )
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        forbidden_execute,
    )

    scheduler_module._pipeline_job_runner("task-1")

    assert dispatched == [("task-1", "scheduled")]


def test_pipeline_job_runner_survives_dispatch_failure(monkeypatch):
    def failing_dispatch(_task_id, _trigger_type):
        raise RuntimeError("channel down")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_pipeline_task",
        failing_dispatch,
    )

    # 派发失败只记日志等下一周期重试，不能炸掉调度线程
    scheduler_module._pipeline_job_runner("task-1")

    # 锁已释放：下一周期可以再次派发
    dispatched = []
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_pipeline_task",
        lambda task_id, trigger_type: dispatched.append((task_id, trigger_type)),
    )
    scheduler_module._pipeline_job_runner("task-1")
    assert dispatched == [("task-1", "scheduled")]


def test_trigger_task_async_dispatches(monkeypatch, db):
    task = _idle_task(db)
    dispatched = []

    def forbidden_execute(*_args, **_kwargs):  # pragma: no cover - 防御断言
        raise AssertionError("sync=false 不得再内联执行 execute_pipeline_task")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_pipeline_task",
        lambda task_id, trigger_type, **_kwargs: dispatched.append(
            (task_id, trigger_type)),
    )
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        forbidden_execute,
    )

    result = trigger_task(task.id, BackgroundTasks(), False, db)

    assert result == {"status": "triggered", "task_id": task.id}
    assert dispatched == [(task.id, "manual")]


def test_trigger_task_async_dispatch_failure_returns_503(monkeypatch, db):
    task = _idle_task(db)

    def failing_dispatch(_task_id, _trigger_type, **_kwargs):
        raise RuntimeError("未配置 NATS_URL")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_pipeline_task",
        failing_dispatch,
    )

    with pytest.raises(HTTPException) as exc_info:
        trigger_task(task.id, BackgroundTasks(), False, db)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "任务派发失败：消息通道不可用，请稍后重试"


def test_trigger_task_sync_still_executes_inline(monkeypatch, db):
    task = _idle_task(db)
    executed = []

    def fake_execute(task_id, trigger_type, full_refresh=False):
        executed.append((task_id, trigger_type, full_refresh))
        return {"status": "ok", "task_id": task_id}

    def forbidden_dispatch(*_args, **_kwargs):  # pragma: no cover - 防御断言
        raise AssertionError("sync=true 保持内联执行，不得派发")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        fake_execute,
    )
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.dispatch.dispatch_pipeline_task",
        forbidden_dispatch,
    )

    result = trigger_task(task.id, BackgroundTasks(), True, db)

    assert result["status"] == "ok"
    assert executed == [(task.id, "manual", False)]


def test_scheduler_start_registers_reconcile_job(monkeypatch):
    monkeypatch.setattr(SyncScheduler, "reload_all", lambda self: None)
    monkeypatch.setattr(
        "app.data_channel.datasets.version_events.drain_dataset_version_events",
        lambda **_kwargs: {},
    )
    instance = SyncScheduler()

    instance.start()
    try:
        job = instance.scheduler.get_job("pipeline_executions:reconcile")
        assert job is not None
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == 60
        assert job.trigger.interval.total_seconds() == 300
    finally:
        instance.shutdown()


def test_reconcile_job_runner_opens_session_and_swallows_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.reconciler.reconcile_pipeline_executions",
        lambda db: calls.append(db) or {"tasks_failed": 0, "runs_failed": 0},
    )
    monkeypatch.setattr("app.database.SessionLocal", lambda: object())

    scheduler_module._pipeline_reconcile_job_runner()

    assert len(calls) == 1

    def failing_reconcile(_db):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.reconciler.reconcile_pipeline_executions",
        failing_reconcile,
    )
    # 对账器异常不能炸掉调度线程
    scheduler_module._pipeline_reconcile_job_runner()


@pytest.mark.asyncio
async def test_dispatch_works_inside_running_event_loop(fake_nats):
    """回归：async def 端点（如数据集导入）在事件循环上运行，asyncio.run
    会直接炸——派发必须经专用循环线程，任意调用上下文都安全。"""
    dispatch_task(
        DATASET_IMPORT_SUBJECT,
        {"job_id": "job-async-ctx", "kind": "inspect"},
    )
    (subject, payload, headers), = fake_nats["published"]
    assert subject == "task.dataset.import"
    assert json.loads(payload.decode())["job_id"] == "job-async-ctx"
    assert headers["Nats-Msg-Id"]
