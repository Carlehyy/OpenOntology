"""本体助手后台回合任务入口与对账调度器测试。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tasks import ontology_assistant_tasks


def _install_fake_run_service(monkeypatch, **attributes) -> SimpleNamespace:
    service = SimpleNamespace(**attributes)
    import app.ontologies.agent_runtime as agent_runtime_pkg

    monkeypatch.setitem(
        sys.modules,
        "app.ontologies.agent_runtime.run_service",
        service,
    )
    monkeypatch.setattr(
        agent_runtime_pkg, "run_service", service, raising=False
    )
    return service


def test_reconcile_once_returns_run_service_counts(monkeypatch):
    calls = []

    def reconcile_runs():
        calls.append(True)
        return {"interrupted": 1, "redispatched": 3}

    _install_fake_run_service(monkeypatch, reconcile_runs=reconcile_runs)

    assert ontology_assistant_tasks.reconcile_once() == {
        "interrupted": 1,
        "redispatched": 3,
    }
    assert calls == [True]


def test_reconcile_once_rejects_invalid_service_result(monkeypatch):
    _install_fake_run_service(monkeypatch, reconcile_runs=lambda: None)

    with pytest.raises(TypeError, match="计数 dict"):
        ontology_assistant_tasks.reconcile_once()


def test_scheduler_start_is_idempotent_and_uses_bounded_interval(monkeypatch):
    instances = []

    class FakeScheduler:
        def __init__(self, *, timezone):
            self.timezone = timezone
            self.running = False
            self.jobs = []
            self.shutdown_calls = 0
            instances.append(self)

        def add_job(self, func, trigger, **kwargs):
            self.jobs.append((func, trigger, kwargs))

        def start(self):
            self.running = True

        def shutdown(self, wait=False):
            self.running = False
            self.shutdown_calls += 1

    monkeypatch.setattr(
        ontology_assistant_tasks, "BackgroundScheduler", FakeScheduler
    )
    monkeypatch.setattr(ontology_assistant_tasks, "_scheduler", None)

    ontology_assistant_tasks.start()
    ontology_assistant_tasks.start()

    assert len(instances) == 1
    scheduler = instances[0]
    assert scheduler.running is True
    assert len(scheduler.jobs) == 1
    _func, trigger, options = scheduler.jobs[0]
    assert trigger == "interval"
    assert options["id"] == "ontology-assistant-run-reconcile"
    assert options["max_instances"] == 1
    assert options["coalesce"] is True
    assert 30 <= ontology_assistant_tasks.RECONCILE_INTERVAL_SECONDS <= 60
    assert options["seconds"] == ontology_assistant_tasks.RECONCILE_INTERVAL_SECONDS
    assert isinstance(options["next_run_time"], datetime)

    ontology_assistant_tasks.shutdown()
    ontology_assistant_tasks.shutdown()
    assert scheduler.running is False
    assert scheduler.shutdown_calls == 1
    assert ontology_assistant_tasks._scheduler is None


def test_scan_swallows_reconcile_failure(monkeypatch, caplog):
    def explode():
        raise RuntimeError("db unavailable")

    _install_fake_run_service(monkeypatch, reconcile_runs=explode)

    ontology_assistant_tasks._scan()

    assert "本体助手回合对账扫描失败" in caplog.text


def test_lifecycle_owns_reconcile_scheduler():
    """调度器只由 Web lifespan 启停；executor worker 不重复注册。"""
    lifecycle = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "bootstrap"
        / "lifecycle.py"
    ).read_text(encoding="utf-8")

    assert "ontology_assistant_tasks.start()" in lifecycle
    assert "ontology_assistant_tasks.shutdown()" in lifecycle
    assert "本体助手回合对账器启动失败" in lifecycle
