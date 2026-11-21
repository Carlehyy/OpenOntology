"""流水线 NATS executor 进程的消息处理、并发与心跳测试。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

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
        self.nak_delays = []
        self.in_progress_calls = 0

    async def ack(self):
        self.acked += 1

    async def nak(self, delay=None):
        self.naked += 1
        self.nak_delays.append(delay)

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
async def test_unparseable_message_is_acked_for_discard(executor, monkeypatch):
    def forbidden_execute(*_args):  # pragma: no cover - 防御断言
        raise AssertionError("坏消息不得进入执行引擎")

    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.engine.execute_pipeline_task",
        forbidden_execute,
    )
    msg = _FakeMsg(b"not-json")

    await executor._process_message(msg, _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC)

    # 不可解析的消息无法通过重投修复：必须显式 ack 丢弃（nak 只会空烧
    # 投递次数；max_deliver 已放开，nak 会变成无限重投毒消息）。
    assert msg.acked == 1
    assert msg.naked == 0


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
async def test_handler_retry_after_controls_nak_delay(executor):
    """A live integrated-sync owner must re-arm the delivery, not be ACKed."""
    class LeaseBusy(RuntimeError):
        retry_after = 42.0

    async def busy(_payload):
        raise LeaseBusy("owner lease still live")

    msg = _msg()
    await executor._process_message(msg, busy, "integrated.sync")

    assert msg.acked == 0
    assert msg.naked == 1 and msg.nak_delays == [42.0]


@pytest.mark.asyncio
async def test_handler_invalid_retry_after_falls_back_to_bounded_nak(executor):
    class BrokenDelay(RuntimeError):
        retry_after = "not-a-number"

    async def explode(_payload):
        raise BrokenDelay("bad delay")

    msg = _msg()
    await executor._process_message(msg, explode, "integrated.sync")

    assert msg.acked == 0
    assert msg.nak_delays == [5]


@pytest.mark.asyncio
async def test_kernel_reconcile_not_applied_is_naked_for_redelivery(executor, monkeypatch):
    """A transient reconcile failure must not be acknowledged and lost."""
    async def not_applied(_payload):
        return False

    monkeypatch.setattr(
        "app.super_assistant.kernel.runtime.reconcile_execution_message",
        not_applied,
    )
    msg = _FakeMsg(json.dumps({"run_id": "run-1", "call_id": "call-1"}).encode())

    await executor._process_message(
        msg, nats_executor._run_kernel_reconcile_message, "sa.execution.reconcile"
    )

    assert msg.naked == 1
    assert msg.acked == 0


@pytest.mark.asyncio
async def test_kernel_call_not_applied_is_naked_for_redelivery(executor, monkeypatch):
    """A transient initial-call failure must remain deliverable."""
    async def not_applied(_payload):
        return False

    monkeypatch.setattr(
        "app.super_assistant.kernel.runtime.process_external_call_message",
        not_applied,
    )
    msg = _FakeMsg(json.dumps({"run_id": "run-1", "call_id": "call-1"}).encode())

    await executor._process_message(
        msg, nats_executor._run_kernel_call_message, "sa.execution.call.owner"
    )

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
        "integrated-sync-executor",
        "assistant-eval-autopilot",
        "super-assistant-reflect-micro",
        "super-assistant-reflect-full",
        "super-assistant-reflect-focused",
        "super-assistant-scheduled-run",
        "task-instances-control",
        "integrated-sync-executor",
        "ontology-assistant-run-executor",
        "sa-memory-sync",
        "super-assistant-autoloop-dispatch",
    }
    assert nats_executor._CONSUMER_DURABLE == "pipeline-executor"


def test_ontology_assistant_consumer_is_single_and_dedicated(monkeypatch):
    from app.data_channel.pipeline_tasks.dispatch import (
        ONTOLOGY_ASSISTANT_RUN_SUBJECT,
    )
    from app.tasks import ontology_assistant_tasks

    for executor_mode, exclude_control, expected in (
        ("fake", "0", True),
        ("fake", "1", True),
        ("docker", "0", False),
        ("docker", None, False),
        ("docker", "1", True),
    ):
        monkeypatch.setenv("TASK_INSTANCES_EXECUTOR", executor_mode)
        if exclude_control is None:
            monkeypatch.delenv("TASK_INSTANCES_CONTROL_EXCLUDE", raising=False)
        else:
            monkeypatch.setenv("TASK_INSTANCES_CONTROL_EXCLUDE", exclude_control)
        registry = nats_executor._handler_registry()
        matches = [
            entry for entry in registry
            if entry[0] == ONTOLOGY_ASSISTANT_RUN_SUBJECT
        ]
        assert bool(matches) is expected
        if expected:
            assert matches == [(
                ONTOLOGY_ASSISTANT_RUN_SUBJECT,
                "ontology-assistant-run-executor",
                ontology_assistant_tasks.run_ontology_assistant_run_message,
            )]
        assert len({subject for subject, _d, _h in registry}) == len(registry)
        assert len({durable for _s, durable, _h in registry}) == len(registry)


def test_memory_sync_consumer_excludes_task_worker(monkeypatch):
    from app.data_channel.pipeline_tasks.dispatch import (
        SUPER_ASSISTANT_MEMORY_SYNC_SUBJECT,
    )

    for executor_mode, exclude_control, expected in (
        ("fake", "0", True),
        ("fake", "1", True),
        ("docker", "0", False),
        ("docker", None, False),
        ("docker", "1", True),
    ):
        monkeypatch.setenv("TASK_INSTANCES_EXECUTOR", executor_mode)
        if exclude_control is None:
            monkeypatch.delenv("TASK_INSTANCES_CONTROL_EXCLUDE", raising=False)
        else:
            monkeypatch.setenv("TASK_INSTANCES_CONTROL_EXCLUDE", exclude_control)
        matches = [
            entry for entry in nats_executor._handler_registry()
            if entry[0] == SUPER_ASSISTANT_MEMORY_SYNC_SUBJECT
        ]
        assert matches == ([
            (
                SUPER_ASSISTANT_MEMORY_SYNC_SUBJECT,
                "sa-memory-sync",
                nats_executor._run_super_assistant_memory_message,
            )
        ] if expected else [])


@pytest.mark.asyncio
async def test_ontology_assistant_handler_calls_execute_run(executor, monkeypatch):
    """handler 只把 run_id 交给 run_service，重复/终态语义由服务持有。"""
    calls = []
    fake_service = SimpleNamespace(
        execute_run=lambda run_id: calls.append(run_id),
    )
    import app.ontologies.agent_runtime as agent_runtime_pkg

    monkeypatch.setitem(
        sys.modules,
        "app.ontologies.agent_runtime.run_service",
        fake_service,
    )
    monkeypatch.setattr(
        agent_runtime_pkg, "run_service", fake_service, raising=False
    )

    msg = _FakeMsg(json.dumps({"run_id": "run-1"}).encode())
    await executor._process_message(
        msg,
        next(handler for subject, _durable, handler in nats_executor._handler_registry()
             if subject == "ontology.assistant.run"),
        "ontology.assistant.run",
    )

    assert calls == ["run-1"]
    assert msg.acked == 1 and msg.naked == 0


@pytest.mark.asyncio
async def test_ontology_assistant_handler_discards_malformed_payload(executor):
    """坏消息不进入 run_service，也不触发无限重投。"""
    msg = _FakeMsg(json.dumps({"run_id": "   "}).encode())

    await executor._process_message(
        msg,
        next(handler for subject, _durable, handler in nats_executor._handler_registry()
             if subject == "ontology.assistant.run"),
        "ontology.assistant.run",
    )

    assert msg.acked == 1 and msg.naked == 0


@pytest.mark.asyncio
async def test_plugin_runner_reply_ack_path_consumes_malformed_envelope(caplog):
    """Poison replies are logged and consumed instead of redelivered forever."""
    await nats_executor._run_plugin_runner_reply_message({"protocol": "invalid"})
    assert "invalid plugin runner reply envelope" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_matches", [True, False])
@pytest.mark.parametrize(
    ("event_kind", "event_status", "expected_state"),
    [
        ("completed", "completed", "completed"),
        # The current runner descriptor has no decision return channel.  An
        # approval request therefore remains an unknown observation instead
        # of creating an Approval that could never be delivered back to the
        # remote invocation.
        ("approval_requested", "waiting_approval", "unknown"),
    ],
)
async def test_plugin_runner_reply_binds_manifest_before_enqueuing(
    monkeypatch, manifest_matches, event_kind, event_status, expected_state,
):
    from types import SimpleNamespace
    from app.super_assistant.kernel.plugin_runner import PluginRunnerEventEnvelope

    rows = iter([
        SimpleNamespace(id="run-1", owner_id="owner-1"),
        SimpleNamespace(id="call-1", capability_revision=2, target_ref="plugin-1"),
        SimpleNamespace(id="plugin-1", key="mail", revision=2, manifest_hash="a" * 64),
    ])
    committed = []
    observations = []
    db = SimpleNamespace(
        scalar=lambda _query: next(rows),
        commit=lambda: committed.append(True),
        rollback=lambda: None,
        close=lambda: None,
    )
    monkeypatch.setattr("app.shared.database.SessionLocal", lambda: db)
    monkeypatch.setattr(
        "app.super_assistant.kernel.store.enqueue_reconcile_observation",
        lambda _db, **kwargs: observations.append(kwargs),
    )
    event = PluginRunnerEventEnvelope(
        request_id="plugin-call:call-1", owner_id="owner-1", run_id="run-1", call_id="call-1",
        plugin_id="plugin-1", revision=2, manifest_hash=("a" if manifest_matches else "b") * 64,
        capability_revision=2, event_seq=3, kind=event_kind, status=event_status,
        payload={"summary": "done"} if event_kind == "completed" else {
            "target_ref": "plugin-1", "target_summary": "send mail",
            "parameter_summary": "{}", "scope_summary": "mail",
        },
    )
    await nats_executor._run_plugin_runner_reply_message(event.to_payload())
    assert bool(observations) is manifest_matches
    assert bool(committed) is manifest_matches
    if observations:
        assert observations[0]["observation"]["provider_event_id"] == "plugin-call:call-1:3"
        assert observations[0]["observation"]["remote_state"] == expected_state


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
async def test_assistant_lane_is_independent_and_bounded(executor):
    """助手回合独占 2 个槽位：可与流水线 2 个槽位同时执行，总并发有界。"""
    active = {"pipeline": 0, "assistant": 0}
    maxima = {"pipeline": 0, "assistant": 0, "total": 0}
    started = 0
    release = asyncio.Event()

    async def lane_handler(lane: str):
        nonlocal started
        active[lane] += 1
        maxima[lane] = max(maxima[lane], active[lane])
        maxima["total"] = max(maxima["total"], sum(active.values()))
        started += 1
        if started >= 4:
            release.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        finally:
            active[lane] -= 1

    async def pipeline_handler(_payload):
        await lane_handler("pipeline")

    async def assistant_handler(_payload):
        await lane_handler("assistant")

    pipeline_messages = [
        _FakeMsg(json.dumps({"task_id": f"task-{index}"}).encode())
        for index in range(2)
    ]
    assistant_messages = [
        _FakeMsg(json.dumps({"run_id": f"run-{index}"}).encode())
        for index in range(2)
    ]
    pipeline_loop = asyncio.ensure_future(executor._fetch_loop(
        _FakeSubscription([pipeline_messages]),
        pipeline_handler,
        _PIPELINE_TASK_DESC,
        semaphore=executor._semaphore,
        batch_size=executor._concurrency,
    ))
    assistant_loop = asyncio.ensure_future(executor._fetch_loop(
        _FakeSubscription([assistant_messages]),
        assistant_handler,
        "ontology.assistant.run",
        semaphore=executor._assistant_semaphore,
        batch_size=executor._assistant_concurrency,
    ))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (
        all(m.acked for m in pipeline_messages)
        and all(m.acked for m in assistant_messages)
    ):
        await asyncio.sleep(0.02)
    executor.request_shutdown()
    await asyncio.wait_for(
        asyncio.gather(pipeline_loop, assistant_loop), timeout=5
    )

    assert maxima == {"pipeline": 2, "assistant": 2, "total": 4}
    assert executor._assistant_concurrency == 2
    assert all(m.acked == 1 for m in pipeline_messages + assistant_messages)


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
    while time.monotonic() < deadline and len(subscriptions) < 21:
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
        ("super_assistant.scheduled.run", "super-assistant-scheduled-run"),
        ("task_instances.control", "task-instances-control"),
        ("integrated.sync", "integrated-sync-executor"),
        ("ontology.assistant.run", "ontology-assistant-run-executor"),
        ("super_assistant.memory.sync", "sa-memory-sync"),
        ("super_assistant.autoloop.dispatch", "super-assistant-autoloop-dispatch"),
        ("sa.execution.run.*", "sa-kernel-v1"),
        ("sa.execution.call.*", "sa-call-v1"),
        ("sa.execution.reconcile", "sa-reconciler-v1"),
        ("sa.plugin.reply.*", "sa-plugin-reply-v1"),
        ]
    assert all(stream == "PIPELINE_TASKS" for _s, _d, stream, _c in subscriptions[:17])
    assert all(stream == "SA_EXECUTION_V1" for _s, _d, stream, _c in subscriptions[17:20])
    assert subscriptions[20][2] == "SA_PLUGIN_RUNNER_V1"
    # ack_wait=60s 与 20s 续约间隔配套；max_deliver 不设上限（-1）——
    # 丢一条 kernel 派发消息等于 Run 挂死到 deadline，瞬时故障靠 nak
    # delay=5s 退避而不是丢弃兜底。
    assert all(
        config.ack_wait == 60 and config.max_deliver is None
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


_owned_nats_consumers = None


def _record_nats_resource(operation, **details):
    target = os.getenv("AUTOLOOP_TEST_NATS_LEDGER")
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "operation": operation,
                "taskId": os.getenv("AUTOLOOP_TEST_TASK_ID"),
                "attemptId": os.getenv("AUTOLOOP_TEST_ATTEMPT_ID"),
                "recordedAt": time.time(), **details,
            }) + "\n")


def _reserved_nats_url():
    raw = os.getenv("TEST_NATS_URL")
    if not raw:
        pytest.skip("TEST_NATS_URL 未设置，跳过真实 NATS 集成测试")
    url = urlsplit(raw)
    if (url.scheme != "nats" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.port in {None, 4222} or url.username or url.password):
        pytest.fail("真实 NATS 测试只接受已预留的非默认 loopback 端口")
    if os.getenv("AUTOLOOP_TEST_NATS_EXCLUSIVE") != "1":
        pytest.fail("canonical NATS 测试要求 AUTOLOOP_TEST_NATS_EXCLUSIVE=1 独占授权")
    return raw


@asynccontextmanager
async def _owned_pipeline_stream():
    """Serialize canonical stream ownership across pytest worker processes."""
    identity = hashlib.sha256(_reserved_nats_url().encode()).hexdigest()[:20]
    path = Path(tempfile.gettempdir()) / f"autoloop-nats-{identity}.lock"
    with path.open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + 180
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    pytest.fail("canonical NATS test resource lock timed out")
                await asyncio.sleep(0.05)
        try:
            async with _pipeline_stream_scope() as resources:
                yield resources
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def _pipeline_stream_scope():
    """Create canonical resources only when absent, delete exact owned names."""
    import nats
    from nats.js.errors import NotFoundError
    from app.data_channel.pipeline_tasks import dispatch

    global _owned_nats_consumers
    assert _owned_nats_consumers is None, "canonical NATS scopes cannot overlap"
    dispatch.shutdown_dispatch_loop()
    nc = await nats.connect(_reserved_nats_url(), connect_timeout=3)
    _owned_nats_consumers = set()
    js = nc.jetstream()
    owned = False
    try:
        try:
            await js.stream_info(dispatch.PIPELINE_STREAM)
        except NotFoundError:
            pass
        else:
            pytest.fail("PIPELINE_TASKS 已存在：拒绝接管未登记的 NATS 资源")
        _record_nats_resource("reserved", stream=dispatch.PIPELINE_STREAM,
                              subjects=list(dispatch.PIPELINE_STREAM_SUBJECTS))
        await dispatch.ensure_pipeline_stream(js)
        owned = True
        info = await js.stream_info(dispatch.PIPELINE_STREAM)
        _record_nats_resource("created", stream=dispatch.PIPELINE_STREAM,
                              subjects=info.config.subjects)
        yield nc, js
    finally:
        dispatch.shutdown_dispatch_loop()
        try:
            if owned:
                consumers = await js.consumers_info(dispatch.PIPELINE_STREAM)
                actual = {consumer.name for consumer in consumers}
                unexpected = actual - _owned_nats_consumers
                if unexpected:
                    _record_nats_resource("unexpected_consumers", stream=dispatch.PIPELINE_STREAM,
                                          consumers=sorted(unexpected))
                    pytest.fail("canonical stream 出现未登记 consumer：保留现场并停止清理")
                for name in sorted(actual):
                    await js.delete_consumer(dispatch.PIPELINE_STREAM, name)
                    _record_nats_resource("deleted", stream=dispatch.PIPELINE_STREAM,
                                          consumer=name)
                await js.delete_stream(dispatch.PIPELINE_STREAM)
                _record_nats_resource("deleted", stream=dispatch.PIPELINE_STREAM)
                with pytest.raises(NotFoundError):
                    await js.stream_info(dispatch.PIPELINE_STREAM)
                _record_nats_resource("residual_absent", stream=dispatch.PIPELINE_STREAM)
        finally:
            _owned_nats_consumers = None
            await nc.drain()


async def _owned_consumer(js, subject, durable, *, ack_wait=1):
    from app.data_channel.pipeline_tasks.dispatch import PIPELINE_STREAM
    from nats.js.api import ConsumerConfig

    assert _owned_nats_consumers is not None, "consumer requires an owned stream scope"
    first = durable not in _owned_nats_consumers
    if first:
        _record_nats_resource("reserved", stream=PIPELINE_STREAM, consumer=durable,
                              subject=subject)
    subscription = await js.pull_subscribe(subject, durable=durable, stream=PIPELINE_STREAM,
        config=ConsumerConfig(ack_wait=ack_wait, max_deliver=None))
    _owned_nats_consumers.add(durable)
    _record_nats_resource("created" if first else "reconnected", stream=PIPELINE_STREAM,
                          consumer=durable, subject=subject)
    return subscription


async def _consumer_drained(js, durable):
    from app.data_channel.pipeline_tasks.dispatch import PIPELINE_STREAM

    deadline = time.monotonic() + 10
    while True:
        info = await js.consumer_info(PIPELINE_STREAM, durable)
        if info.num_pending == 0 and info.num_ack_pending == 0:
            return info
        assert time.monotonic() < deadline, (
            f"consumer 未清零: pending={info.num_pending} ack_pending={info.num_ack_pending}"
        )
        await asyncio.sleep(0.05)


def _autoloop_subscription():
    from app.data_channel.pipeline_tasks.dispatch import AUTOLOOP_DISPATCH_SUBJECT

    return next(item for item in nats_executor._handler_registry()
                if item[0] == AUTOLOOP_DISPATCH_SUBJECT)


def test_autoloop_real_nats_unknown_publish_and_durable_redelivery(monkeypatch, db):
    """Real broker + persisted work: accepted timeout, duplicate, NAK and restart."""
    from nats.errors import TimeoutError as NatsTimeoutError
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from app.data_channel.pipeline_tasks import dispatch
    from app.super_assistant.autoloop import dispatching
    from app.super_assistant.autoloop.models import AutoLoopDispatch, AutoLoopEvent
    from tests.super_assistant.autoloop.test_dispatching import _admit

    sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    _, event_id, identity, version = _admit(sessions)
    monkeypatch.setattr("app.config.settings.nats_url", _reserved_nats_url())
    monkeypatch.setattr(dispatching, "SessionLocal", sessions)
    monkeypatch.setattr(dispatching, "business_router", None)
    # Exercise real delayed NAK without waiting the production 30s interval.
    monkeypatch.setattr(dispatching.RoutingDeferred, "retry_after", 0.1)
    subject, durable, handler = _autoloop_subscription()

    async def consume():
        async with _owned_pipeline_stream() as (nc, js):
            subscription = await _owned_consumer(js, subject, durable)
            publications = []

            def accepted_but_confirmation_lost(dispatch_id, dispatch_version):
                dispatch.dispatch_autoloop(dispatch_id, dispatch_version)
                publications.append((dispatch_id, dispatch_version))
                raise TimeoutError("fault: broker accepted but confirmation lost")

            assert await asyncio.to_thread(dispatching.scan_pending,
                session_factory=sessions, publish=accepted_but_confirmation_lost) == 0
            with sessions() as fresh:
                work = fresh.get(AutoLoopDispatch, identity)
                assert work.state == "pending" and work.version == version
            # A second publication retains Nats-Msg-Id and broker stream sequence.
            assert await asyncio.to_thread(dispatching.scan_pending,
                session_factory=sessions) == 1
            assert publications == [(identity, version)]
            stream = await js.stream_info(dispatch.PIPELINE_STREAM)
            assert stream.state.messages == 1 and stream.state.last_seq == 1
            [message] = await subscription.fetch(1, timeout=5)
            first_sequence = message.metadata.sequence.stream
            first_body = message.data
            assert message.headers["Nats-Msg-Id"] == f"{subject}:{identity}:{version}"
            await PipelineExecutor()._process_message(message, handler, subject)
            await nc.flush()
            info = await js.consumer_info(dispatch.PIPELINE_STREAM, durable)
            assert info.num_ack_pending == 1
            with sessions() as fresh:
                work = fresh.get(AutoLoopDispatch, identity)
                assert work.state == "pending" and work.error == "awaiting_business_router"
                assert work.event_id == event_id and work.attempts == 1
                deferred_version = work.version
                assert fresh.scalar(select(AutoLoopEvent).where(
                    AutoLoopEvent.type == "routing.deferred")).action == "pending"

            await subscription.unsubscribe()
            subscription = await _owned_consumer(js, subject, durable)
            [duplicate] = await subscription.fetch(1, timeout=5)
            assert duplicate.data == first_body
            assert duplicate.metadata.sequence.stream == first_sequence
            assert duplicate.metadata.num_delivered >= 2
            await PipelineExecutor()._process_message(duplicate, handler, subject)
            await nc.flush()
            with sessions() as fresh:
                work = fresh.get(AutoLoopDispatch, identity)
                assert work.version == deferred_version and work.attempts == 1
            # A visible terminal fact can be ACKed by a newly created executor.
            with sessions() as fresh:
                fresh.get(AutoLoopDispatch, identity).state = "done"
                fresh.commit()
            [terminal] = await subscription.fetch(1, timeout=5)
            await PipelineExecutor()._process_message(terminal, handler, subject)
            await nc.flush()
            await _consumer_drained(js, durable)
            with pytest.raises(NatsTimeoutError):
                await subscription.fetch(1, timeout=0.2)

    asyncio.run(consume())


def test_autoloop_real_nats_concurrent_duplicate_deliveries(monkeypatch, db):
    """Distinct broker deliveries of one DB identity contend under dispatch CAS."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker
    from app.super_assistant.autoloop import dispatching
    from app.super_assistant.autoloop.models import AutoLoopDispatch, AutoLoopEvent
    from tests.super_assistant.autoloop.test_dispatching import _admit

    sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    _, _, identity, version = _admit(sessions)
    monkeypatch.setattr(dispatching, "SessionLocal", sessions)
    monkeypatch.setattr(dispatching, "business_router", None)
    subject, durable, handler = _autoloop_subscription()

    async def consume():
        async with _owned_pipeline_stream() as (nc, js):
            subscription = await _owned_consumer(js, subject, durable)
            body = json.dumps({"dispatch_id": identity, "version": version}).encode()
            # Inject duplication outside the publisher's dedup window/identity;
            # DB CAS, rather than broker deduplication, must resolve it.
            for suffix in ("a", "b"):
                await js.publish(subject, body, headers={"Nats-Msg-Id": f"{identity}:{suffix}"})
            messages = await subscription.fetch(2, timeout=5)
            await asyncio.gather(*(PipelineExecutor()._process_message(message, handler, subject)
                                   for message in messages))
            await nc.flush()
            info = await js.consumer_info("PIPELINE_TASKS", durable)
            assert info.num_ack_pending == 2
            with sessions() as fresh:
                work = fresh.get(AutoLoopDispatch, identity)
                assert work.attempts == 1 and work.error == "awaiting_business_router"
                assert fresh.scalar(select(func.count()).select_from(AutoLoopEvent).where(
                    AutoLoopEvent.type == "routing.deferred")) == 1

    asyncio.run(consume())


def test_dispatch_to_executor_end_to_end(monkeypatch):
    """真实 NATS：派发 → executor 消费执行 → 消息 ack、consumer 无积压。"""
    from app.data_channel.pipeline_tasks import engine as task_engine
    from app.data_channel.pipeline_tasks.dispatch import (
        PIPELINE_EXECUTE_SUBJECT,
        dispatch_pipeline_task,
    )

    monkeypatch.setattr("app.config.settings.nats_url", _reserved_nats_url())

    executed = []
    monkeypatch.setattr(
        task_engine,
        "execute_pipeline_task",
        lambda task_id, trigger_type, full_refresh=False: executed.append(
            (task_id, trigger_type))
        or {"status": "ok"},
    )

    async def consume_once():
        async with _owned_pipeline_stream() as (_nc, js):
            subscription = await _owned_consumer(js, PIPELINE_EXECUTE_SUBJECT,
                                                nats_executor._CONSUMER_DURABLE)
            await asyncio.to_thread(dispatch_pipeline_task, "it-task-1", "scheduled")
            messages = await subscription.fetch(1, timeout=10)
            assert len(messages) == 1
            await PipelineExecutor()._process_message(
                messages[0], _PIPELINE_TASK_HANDLER, _PIPELINE_TASK_DESC
            )
            return await _consumer_drained(js, nats_executor._CONSUMER_DURABLE)

    info = asyncio.run(consume_once())

    assert executed == [("it-task-1", "scheduled")]
    assert info.num_pending == 0
    assert info.num_ack_pending == 0


@pytest.mark.asyncio
async def test_executor_uses_bounded_nats_drain_timeout(monkeypatch):
    """Pull consumers must not race the container stop grace period on drain."""
    import nats
    from app.data_channel.pipeline_tasks import dispatch
    from app.super_assistant.kernel import plugin_runner_service

    connect_calls = []
    drained = []

    class FakeJetStream:
        async def pull_subscribe(self, *_args, **_kwargs):
            return object()

    class FakeClient:
        def jetstream(self):
            return FakeJetStream()

        async def drain(self):
            drained.append(True)

    async def connect(url, **kwargs):
        connect_calls.append((url, kwargs))
        return FakeClient()

    async def noop(*_args, **_kwargs):
        return None

    async def no_fetch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(nats, "connect", connect)
    monkeypatch.setattr("app.config.settings.nats_url", "nats://test:4222")
    monkeypatch.setattr(dispatch, "ensure_pipeline_stream", noop)
    monkeypatch.setattr(dispatch, "ensure_execution_stream", noop)
    monkeypatch.setattr(plugin_runner_service, "ensure_plugin_runner_stream", noop)
    monkeypatch.setattr(nats_executor, "_handler_registry", lambda: ())
    monkeypatch.setattr(nats_executor, "_execution_handler_registry", lambda: ())

    executor = PipelineExecutor()
    monkeypatch.setattr(executor, "_fetch_loop", no_fetch)
    await executor.run()

    assert connect_calls == [("nats://test:4222", {
        "connect_timeout": 3,
        "drain_timeout": nats_executor._NATS_DRAIN_TIMEOUT_SECONDS,
    })]
    assert drained == [True]


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
