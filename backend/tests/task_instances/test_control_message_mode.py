"""控制消息执行模式分支测试：TASK_INSTANCES_EXECUTOR=fake|docker 的
dispatch/steer 路由正确性（M2 栈级 E2E 暴露的回归缺口——mode 分支
曾因补丁未命中而缺失，task_worker 一直跑 fake）。"""
from __future__ import annotations

import pytest

from app.task_instances import executor as ti_executor


@pytest.mark.asyncio
async def test_docker_mode_dispatch_routes_to_container_runtime(monkeypatch):
    monkeypatch.setenv("TASK_INSTANCES_EXECUTOR", "docker")
    calls: list[tuple[str, ...]] = []

    def fake_dispatch_docker(node_run_id):  # to_thread 目标须为同步函数
        calls.append(("dispatch", node_run_id))
        return None

    async def fail_fake(*args, **kwargs):
        raise AssertionError("docker 模式不得进入假执行器")

    import app.task_instances.container_runtime as cr

    monkeypatch.setattr(cr, "execute_dispatch_docker", fake_dispatch_docker)
    monkeypatch.setattr(
        ti_executor, "execute_dispatch_on", fail_fake)
    await ti_executor.run_control_message(
        {"kind": "dispatch", "node_run_id": "nr-1"})
    assert calls == [("dispatch", "nr-1")]


@pytest.mark.asyncio
async def test_docker_mode_steer_routes_to_drop_steering(monkeypatch):
    monkeypatch.setenv("TASK_INSTANCES_EXECUTOR", "docker")
    import app.task_instances.container_runtime as cr

    steers: list[tuple[str, str | None, str]] = []

    def fake_drop(node_run_id, message_id, content):
        steers.append((node_run_id, message_id, content))

    monkeypatch.setattr(cr, "drop_steering", fake_drop)
    await ti_executor.run_control_message(
        {"kind": "steer", "node_run_id": "nr-2",
         "message_id": "m-1", "content": "中途补充"})
    assert steers == [("nr-2", "m-1", "中途补充")]


@pytest.mark.asyncio
async def test_fake_mode_dispatch_uses_fake_executor(monkeypatch):
    monkeypatch.setenv("TASK_INSTANCES_EXECUTOR", "fake")
    used: list[str] = []

    class _Result:
        dispatches: list[str] = []

    def fake_execute(db, node_run_id):
        used.append(node_run_id)
        return _Result()

    monkeypatch.setattr(ti_executor, "execute_dispatch_on", fake_execute)
    published: list[str] = []
    monkeypatch.setattr(ti_executor, "_publish_dispatch", published.append)
    await ti_executor.run_control_message(
        {"kind": "dispatch", "node_run_id": "nr-3"})
    assert used == ["nr-3"]
