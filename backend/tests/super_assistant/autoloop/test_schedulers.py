"""Real APScheduler ownership, failure cleanup and in-flight scan shutdown."""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Thread

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app.super_assistant import scheduled_scheduler
from app.super_assistant.autoloop import scheduler as autoloop_scheduler
from app.task_instances import reconcile


@pytest.fixture(params=[autoloop_scheduler, scheduled_scheduler, reconcile], ids=["autoloop", "schedules", "task-reconcile"])
def scheduler_module(request):
    module = request.param
    module.shutdown()
    yield module
    module.shutdown()


def test_repeated_start_retains_one_job_and_shutdown_joins_thread(scheduler_module):
    module = scheduler_module
    module.start()
    owned = module._scheduler
    thread = owned._thread
    jobs = owned.get_jobs()
    assert len(jobs) == 1 and jobs[0].max_instances == 1 and jobs[0].coalesce
    module.start()
    assert module._scheduler is owned and len(owned.get_jobs()) == 1
    module.shutdown()
    assert module._scheduler is None and not owned.running and not thread.is_alive()
    module.shutdown()
    module.start()
    restarted = module._scheduler
    restarted_thread = restarted._thread
    assert restarted is not owned and restarted_thread is not thread
    module.shutdown()
    assert not restarted_thread.is_alive()


@pytest.mark.parametrize("after_start", [False, True], ids=["before-thread", "after-thread"])
def test_partial_startup_failure_cleans_owned_thread_and_allows_retry(
    scheduler_module, monkeypatch, after_start,
):
    module = scheduler_module
    owned = []
    threads = []

    class InterruptedScheduler(BackgroundScheduler):
        def start(self, *args, **kwargs):
            owned.append(self)
            if after_start:
                super().start(*args, **kwargs)
                threads.append(self._thread)
            raise RuntimeError("injected scheduler start interruption")

    monkeypatch.setattr(module, "BackgroundScheduler", InterruptedScheduler)
    with pytest.raises(RuntimeError, match="start interruption"):
        module.start()
    assert module._scheduler is None and not owned[0].running
    assert all(not thread.is_alive() for thread in threads)
    monkeypatch.setattr(module, "BackgroundScheduler", BackgroundScheduler)
    module.start()
    assert module._scheduler.running and len(module._scheduler.get_jobs()) == 1


def test_shutdown_of_externally_stopped_instance_clears_stale_global(scheduler_module):
    module = scheduler_module
    module.start()
    owned = module._scheduler
    thread = owned._thread
    owned.shutdown(wait=True)
    module.shutdown()
    assert module._scheduler is None and not thread.is_alive()


def test_shutdown_waits_for_business_scan_and_joins_scheduler_and_worker(
    scheduler_module, monkeypatch,
):
    module = scheduler_module
    entered, release, completed, stopped = Event(), Event(), Event(), Event()
    workers = []
    failures = []

    def controlled_scan():
        from threading import current_thread
        workers.append(current_thread())
        entered.set()
        assert release.wait(timeout=10), "test did not release the owned in-flight scan"
        completed.set()

    def shutdown():
        try:
            module.shutdown()
        except BaseException as exc:
            failures.append(exc)
        finally:
            stopped.set()

    monkeypatch.setattr(module, "_scan", controlled_scan)
    module.start()
    owned = module._scheduler
    thread = owned._thread
    owned.modify_job(owned.get_jobs()[0].id, next_run_time=datetime.now(timezone.utc))
    assert entered.wait(timeout=5)
    closer = Thread(target=shutdown, name=f"test-{module.__name__}-shutdown")
    closer.start()
    try:
        assert not stopped.wait(timeout=0.1), "shutdown returned while business scan was still active"
    finally:
        release.set()
        closer.join(timeout=10)
    assert not closer.is_alive() and not failures and stopped.is_set() and completed.is_set()
    assert module._scheduler is None and not owned.running and not thread.is_alive()
    assert workers and all(not worker.is_alive() for worker in workers)


def test_scan_recovers_committed_work_without_running_model_or_business_router(
    monkeypatch,
):
    from app.super_assistant.autoloop import dispatching, watchdog, worker
    from app.super_assistant import scheduled_service

    calls = []
    monkeypatch.setattr(autoloop_scheduler, "_next_watchdog", 0)
    monkeypatch.setattr(dispatching, "scan_pending", lambda: calls.append("autoloop") or 1)
    monkeypatch.setattr(worker, "scan_actions", lambda: calls.append("actions") or 1)
    monkeypatch.setattr(watchdog, "scan_watchdog", lambda: calls.append("watchdog") or 1)
    monkeypatch.setattr(scheduled_service, "dispatch_due_tasks", lambda: calls.append("schedules") or 1)
    autoloop_scheduler._scan()
    scheduled_scheduler._scan()
    assert calls == ["autoloop", "actions", "watchdog", "schedules"]
