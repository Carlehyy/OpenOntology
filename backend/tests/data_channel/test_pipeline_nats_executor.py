"""流水线 NATS executor 进程的消息处理、并发与心跳测试。"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.data_channel.pipeline_tasks import nats_executor
from app.data_channel.pipeline_tasks.nats_executor import (
    PipelineExecutor,
    _touch_heartbeat,
)

# 旧 subject 的 handler：既有消息处理测试都走它
_PIPELINE_TASK_HANDLER = nats_executor._execute_pipeline_task_message
_PIPELINE_TASK_DESC = "pipeline.task.execute"


class _FakeMsg:
    def __init__(self, payload: bytes):
        self.data = payload
        self.acked = 0
        self.naked = 0
        self.in_progress_calls = 0

    async def ack(self):
        self.acked += 1

    async def nak(self):
        self.naked += 1

    async def in_progress(self):
        self.in_progress_calls += 1


def _msg(task_id: str = "task-1", trigger_type: str = "scheduled") -> _FakeMsg:
    return _FakeMsg(json.dumps({
        "task_id": task_id,
        "trigger_type": trigger_type,
        "dispatched_at": "2026-08-08T00:00:00",
    }).encode())


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.setattr(
        "app.config.settings.pipeline_executor_concurrency", 2,
    )
    return PipelineExecutor()


@pytest.mark.asyncio
async def test_successful_execution_acks(executor, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        lambda task_id, trigger_type, full_refresh=False: calls.append(
            (task_id, trigger_type, full_refresh))
        or {"status": "ok"},
    )
    msg = _msg()

    await executor._process_message(msg, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC)

    assert calls == [("task-1", "scheduled", False)]
    assert msg.acked == 1
    assert msg.naked == 0


@pytest.mark.asyncio
async def test_claim_conflict_is_acked_and_dropped(executor, monkeypatch):
    """claim 冲突（"任务正在执行中"）是送达成功：直接 ack 丢弃重复消息。"""
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        lambda *_args: {"status": "error", "error": "任务正在执行中"},
    )
    msg = _msg()

    await executor._process_message(msg, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC)

    assert msg.acked == 1
    assert msg.naked == 0


@pytest.mark.asyncio
async def test_unparseable_message_is_naked(executor, monkeypatch):
    def forbidden_execute(*_args):  # pragma: no cover - 防御断言
        raise AssertionError("坏消息不得进入执行引擎")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        forbidden_execute,
    )
    msg = _FakeMsg(b"not-json")

    await executor._process_message(msg, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC)

    assert msg.naked == 1
    assert msg.acked == 0


@pytest.mark.asyncio
async def test_thread_exception_escape_is_naked(executor, monkeypatch):
    def explode(*_args):
        raise RuntimeError("thread escaped")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        explode,
    )
    msg = _msg()

    await executor._process_message(msg, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC)

    assert msg.naked == 1
    assert msg.acked == 0


@pytest.mark.asyncio
async def test_in_progress_renews_while_executing(executor, monkeypatch):
    monkeypatch.setattr(nats_executor, "_IN_PROGRESS_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        lambda *_args: time.sleep(0.22) or {"status": "ok"},
    )
    msg = _msg()

    await executor._process_message(msg, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC)

    # ack_wait=30s、续约间隔 20s 的同比例缩小：0.22s 执行应续约 4 次左右
    assert msg.in_progress_calls >= 2
    assert msg.acked == 1


@pytest.mark.asyncio
async def test_no_renewal_after_shutdown_requested(executor, monkeypatch):
    monkeypatch.setattr(nats_executor, "_IN_PROGRESS_INTERVAL_SECONDS", 0.05)
    started = threading.Event()

    def slow_execute(*_args):
        started.set()
        time.sleep(0.2)
        return {"status": "ok"}

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        slow_execute,
    )
    msg = _msg()

    async def request_shutdown_soon():
        await asyncio.to_thread(started.wait)
        executor.request_shutdown()

    await asyncio.gather(
        executor._process_message(msg, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC),
        request_shutdown_soon(),
    )

    assert msg.in_progress_calls == 0
    assert msg.acked == 1


def _run_msg(pipeline_id: str = "pipe-1", run_id: str = "run-1") -> _FakeMsg:
    return _FakeMsg(json.dumps({
        "pipeline_id": pipeline_id,
        "run_id": run_id,
        "dispatched_at": "2026-08-08T00:00:00",
    }).encode())


def _import_msg(job_id: str = "job-1", kind: str = "inspect") -> _FakeMsg:
    return _FakeMsg(json.dumps({
        "job_id": job_id,
        "kind": kind,
        "dispatched_at": "2026-08-08T00:00:00",
    }).encode())


@pytest.mark.asyncio
async def test_pipeline_run_handler_invokes_bare_function(executor, monkeypatch):
    """task.pipeline.run：线程内调用 pipeline_run_task 裸函数（不带 write_opts）。"""
    calls = []
    # 裸函数形态（无 .run 属性）：getattr 兼容直接返回本体
    monkeypatch.setattr(
        "app.tasks.v2.pipeline_run.pipeline_run_task",
        lambda pipeline_id, run_id: calls.append((pipeline_id, run_id)),
    )
    msg = _run_msg()

    await executor._process_message(
        msg, nats_executor._run_pipeline_run_message, "task.pipeline.run"
    )

    assert calls == [("pipe-1", "run-1")]
    assert msg.acked == 1
    assert msg.naked == 0


@pytest.mark.asyncio
async def test_pipeline_run_handler_unwraps_celery_task_run(executor, monkeypatch):
    """Celery 包装形态：裸函数在 .run 上。"""
    calls = []
    celery_like = SimpleNamespace(
        run=lambda pipeline_id, run_id: calls.append((pipeline_id, run_id)),
    )
    monkeypatch.setattr(
        "app.tasks.v2.pipeline_run.pipeline_run_task", celery_like
    )
    msg = _run_msg("pipe-2", "run-2")

    await executor._process_message(
        msg, nats_executor._run_pipeline_run_message, "task.pipeline.run"
    )

    assert calls == [("pipe-2", "run-2")]
    assert msg.acked == 1


@pytest.mark.asyncio
async def test_pipeline_run_handler_thread_exception_is_naked(executor, monkeypatch):
    def explode(*_args):
        raise RuntimeError("thread escaped")

    monkeypatch.setattr("app.tasks.v2.pipeline_run.pipeline_run_task", explode)
    msg = _run_msg()

    await executor._process_message(
        msg, nats_executor._run_pipeline_run_message, "task.pipeline.run"
    )

    assert msg.naked == 1
    assert msg.acked == 0


@pytest.mark.asyncio
async def test_dataset_import_handler_routes_inspect_and_commit(executor, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.tasks.v2.dataset_import.inspect_dataset_import",
        lambda job_id: calls.append(("inspect", job_id)),
    )
    monkeypatch.setattr(
        "app.tasks.v2.dataset_import.commit_dataset_import",
        lambda job_id: calls.append(("commit", job_id)),
    )

    inspect_msg = _import_msg("job-1", "inspect")
    commit_msg = _import_msg("job-2", "commit")
    await executor._process_message(
        inspect_msg, nats_executor._run_dataset_import_message, "task.dataset.import"
    )
    await executor._process_message(
        commit_msg, nats_executor._run_dataset_import_message, "task.dataset.import"
    )

    assert calls == [("inspect", "job-1"), ("commit", "job-2")]
    assert inspect_msg.acked == 1
    assert commit_msg.acked == 1


@pytest.mark.asyncio
async def test_dataset_import_handler_unwraps_celery_task_run(executor, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.tasks.v2.dataset_import.inspect_dataset_import",
        SimpleNamespace(run=lambda job_id: calls.append(job_id)),
    )
    msg = _import_msg("job-9", "inspect")

    await executor._process_message(
        msg, nats_executor._run_dataset_import_message, "task.dataset.import"
    )

    assert calls == ["job-9"]
    assert msg.acked == 1


@pytest.mark.asyncio
async def test_dataset_import_handler_unknown_kind_is_naked(executor, monkeypatch):
    def forbidden(*_args):  # pragma: no cover - 防御断言
        raise AssertionError("未知 kind 不得触达任何任务体")

    monkeypatch.setattr(
        "app.tasks.v2.dataset_import.inspect_dataset_import", forbidden
    )
    monkeypatch.setattr(
        "app.tasks.v2.dataset_import.commit_dataset_import", forbidden
    )
    msg = _import_msg("job-1", "bogus")

    await executor._process_message(
        msg, nats_executor._run_dataset_import_message, "task.dataset.import"
    )

    assert msg.naked == 1
    assert msg.acked == 0


def test_handler_registry_covers_all_stream_subjects():
    from app.data_channel.pipeline_tasks.dispatch import PIPELINE_STREAM_SUBJECTS

    registry = nats_executor._handler_registry()
    subjects = [subject for subject, _durable, _handler in registry]
    durables = {durable for _subject, durable, _handler in registry}

    # 每个流 subject 恰好一个 durable；旧 durable 名称保持不变
    assert subjects == list(PIPELINE_STREAM_SUBJECTS)
    assert durables == {
        "pipeline-executor",
        "pipeline-run-executor",
        "dataset-import-executor",
        "mapping-apply-executor",
        "connection-sync-executor",
        "dataset-event-executor",
        "dataset-migrate-executor",
        "assistant-eval-autopilot",
        "super-assistant-reflect-micro",
        "super-assistant-reflect-full",
        "super-assistant-reflect-focused",
        "super-assistant-palace-extract",
        "super-assistant-palace-consolidate",
        "ontology-documents-published",
    }
    assert nats_executor._CONSUMER_DURABLE == "pipeline-executor"


class _FakeSubscription:
    def __init__(self, batches, on_fetch=None):
        self._batches = list(batches)
        self._on_fetch = on_fetch

    async def fetch(self, batch, timeout):
        if self._on_fetch is not None:
            self._on_fetch()
        if self._batches:
            return self._batches.pop(0)
        # 必须让出事件循环：立刻抛超时会饿死已派发的执行任务
        await asyncio.sleep(0.01)
        raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_fetch_loop_respects_concurrency_limit(executor, monkeypatch):
    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    def fake_execute(task_id, _trigger_type, full_refresh=False):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with counter_lock:
            active -= 1
        return {"status": "ok"}

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        fake_execute,
    )
    messages = [_msg(f"task-{index}") for index in range(4)]
    subscription = _FakeSubscription([messages])

    fetch_task = asyncio.ensure_future(
        executor._fetch_loop(subscription, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC)
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not all(m.acked for m in messages):
        await asyncio.sleep(0.02)
    executor.request_shutdown()
    await asyncio.wait_for(fetch_task, timeout=5)

    assert all(m.acked == 1 for m in messages)
    assert max_active == 2  # pipeline_executor_concurrency 上限


@pytest.mark.asyncio
async def test_fetch_loop_naks_unstarted_messages_on_shutdown(executor):
    messages = [_msg("task-1"), _msg("task-2")]
    subscription = _FakeSubscription(
        [messages], on_fetch=executor.request_shutdown,
    )

    await executor._fetch_loop(subscription, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC)

    # 关闭信号到来后已拉取但未开始的消息立即 nak，让其他 executor 接管
    assert [m.naked for m in messages] == [1, 1]
    assert [m.acked for m in messages] == [0, 0]


@pytest.mark.asyncio
async def test_run_subscribes_each_subject_with_own_durable(
    executor, monkeypatch, tmp_path
):
    """run() 为注册表每个 subject 建立独立 durable pull consumer。"""
    import nats as nats_module

    monkeypatch.setattr(
        nats_executor, "HEARTBEAT_PATH", str(tmp_path / "heartbeat")
    )
    monkeypatch.setattr(
        "app.config.settings.nats_url", "nats://fake-nats:4222"
    )
    subscriptions = []

    class FakeJS:
        async def add_stream(self, config):
            pass

        async def pull_subscribe(self, subject, durable, stream, config):
            subscriptions.append((subject, durable, stream, config))
            return _FakeSubscription([])

    class FakeNC:
        def jetstream(self):
            return FakeJS()

        async def drain(self):
            pass

    async def fake_connect(url, **kwargs):
        return FakeNC()

    monkeypatch.setattr(nats_module, "connect", fake_connect)

    run_task = asyncio.ensure_future(executor.run())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(subscriptions) < 14:
        await asyncio.sleep(0.02)
    executor.request_shutdown()
    await asyncio.wait_for(run_task, timeout=5)

    assert [(subject, durable) for subject, durable, _s, _c in subscriptions] == [
        ("pipeline.task.execute", "pipeline-executor"),
        ("task.pipeline.run", "pipeline-run-executor"),
        ("task.dataset.import", "dataset-import-executor"),
        ("task.mapping.apply", "mapping-apply-executor"),
        ("task.connection.sync", "connection-sync-executor"),
        ("task.dataset.event", "dataset-event-executor"),
        ("super_assistant.reflect.micro", "super-assistant-reflect-micro"),
        ("super_assistant.reflect.full", "super-assistant-reflect-full"),
        ("super_assistant.reflect.focused", "super-assistant-reflect-focused"),
        ("task.dataset.migrate", "dataset-migrate-executor"),
        ("assistant_evaluation.autopilot.cycle", "assistant-eval-autopilot"),
        ("super_assistant.palace.extract", "super-assistant-palace-extract"),
        ("super_assistant.palace.consolidate", "super-assistant-palace-consolidate"),
        ("ontology.documents.published", "ontology-documents-published"),
    ]
    assert all(stream == "PIPELINE_TASKS" for _s, _d, stream, _c in subscriptions)
    # ack_wait=30s 与 20s 续约间隔配套；max_deliver 兜底 poison 消息
    assert all(
        config.ack_wait == 30 and config.max_deliver == 5
        for _s, _d, _stream, config in subscriptions
    )


@pytest.mark.asyncio
async def test_dataset_migrate_handler_invokes_task_with_source_id(executor, monkeypatch):
    calls = []

    monkeypatch.setattr(
        "app.tasks.v2.dataset_migration.migrate_curated_to_manual",
        lambda job_id, source_dataset_id: calls.append((job_id, source_dataset_id)),
    )

    msg = _FakeMsg(json.dumps({
        "job_id": "mig-1",
        "source_dataset_id": "ds-curated-1",
    }).encode())
    await executor._process_message(
        msg, nats_executor._run_dataset_migrate_message, "task.dataset.migrate"
    )

    assert calls == [("mig-1", "ds-curated-1")]
    assert msg.acked == 1


@pytest.mark.asyncio
async def test_heartbeat_file_keeps_fresh_mtime(executor, monkeypatch, tmp_path):
    heartbeat = tmp_path / "nats_executor.heartbeat"
    monkeypatch.setattr(nats_executor, "HEARTBEAT_PATH", str(heartbeat))

    task = asyncio.ensure_future(executor._heartbeat_loop())
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
    assert heartbeat.exists()
    assert time.time() - heartbeat.stat().st_mtime < 5

    old = time.time() - 3600
    import os
    os.utime(heartbeat, (old, old))
    _touch_heartbeat()
    assert time.time() - heartbeat.stat().st_mtime < 5


@pytest.mark.skipif(
    not __import__("os").environ.get("TEST_NATS_URL"),
    reason="TEST_NATS_URL 未设置，跳过真实 NATS 集成测试",
)
def test_dispatch_to_executor_end_to_end(monkeypatch):
    """真实 NATS：派发 → executor 消费执行 → 消息 ack、consumer 无积压。"""
    import os

    from app.data_channel.pipeline_tasks import dispatch as dispatch_module
    from app.data_channel.pipeline_tasks import engine as task_engine
    from app.data_channel.pipeline_tasks.dispatch import (
        PIPELINE_EXECUTE_SUBJECT,
        PIPELINE_STREAM,
        dispatch_pipeline_task,
        ensure_pipeline_stream,
    )

    nats_url = os.environ["TEST_NATS_URL"]
    monkeypatch.setattr("app.config.settings.nats_url", nats_url)
    monkeypatch.setattr(dispatch_module, "_stream_ensured", False)

    executed = []
    monkeypatch.setattr(
        task_engine,
        "execute_pipeline_task",
        lambda task_id, trigger_type, full_refresh=False: executed.append(
            (task_id, trigger_type))
        or {"status": "ok"},
    )

    dispatch_pipeline_task("it-task-1", "scheduled")

    async def consume_once():
        import nats
        from nats.js.api import ConsumerConfig

        nc = await nats.connect(nats_url, connect_timeout=3)
        try:
            js = nc.jetstream()
            await ensure_pipeline_stream(js)
            subscription = await js.pull_subscribe(
                PIPELINE_EXECUTE_SUBJECT,
                durable=nats_executor._CONSUMER_DURABLE,
                stream=PIPELINE_STREAM,
                config=ConsumerConfig(ack_wait=30, max_deliver=5),
            )
            consumer = PipelineExecutor()
            messages = await subscription.fetch(1, timeout=10)
            assert len(messages) == 1
            await consumer._run_message(
                messages[0], _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC
            )
            # ack 是 fire-and-forget 发布，轮询等服务端记账
            import time as _time
            deadline = _time.monotonic() + 10
            while True:
                info = await js.consumer_info(
                    PIPELINE_STREAM, nats_executor._CONSUMER_DURABLE,
                )
                if info.num_pending == 0 and info.num_ack_pending == 0:
                    return info
                assert _time.monotonic() < deadline, (
                    f"consumer 未在 10 秒内清零: pending={info.num_pending} "
                    f"ack_pending={info.num_ack_pending}"
                )
                await asyncio.sleep(0.2)
        finally:
            await nc.drain()

    info = asyncio.run(consume_once())

    assert executed == [("it-task-1", "scheduled")]
    assert info.num_pending == 0
    assert info.num_ack_pending == 0


def test_main_registers_full_model_registry_before_running(monkeypatch):
    """回归：executor 进程不经过 app.main 导入链，启动必须先注册全部表映射。

    缺失时首个 INSERT 的依赖排序解析不到 FK 目标表（如
    v2_dataset_versions），运行记录初始化以 NoReferencedTableError 失败——
    真实栈 E2E 曾抓获该缺陷。
    """
    calls = []

    class _FakeExecutor:
        def __init__(self):
            calls.append("executor-init")

        def request_shutdown(self):
            pass

        async def run(self):
            calls.append("run")

    monkeypatch.setattr(nats_executor, "PipelineExecutor", _FakeExecutor)
    monkeypatch.setattr(
        "app.model_registry.import_all_models",
        lambda: calls.append("import-models"),
    )
    # 让流程越过 NATS_URL 校验，直达 executor.run()
    monkeypatch.setattr("app.config.settings.nats_url", "nats://test:4222")
    with pytest.raises(SystemExit) as exc_info:
        nats_executor.main()
    assert calls[0] == "import-models"
    assert "run" in calls
