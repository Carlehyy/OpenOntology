"""SyncScheduler 任务注册结果与 JobLookupError 收敛的单元测试。

对应整改：任务 CRUD 后「已保存但未注册调度」必须在接口上可见，调度器
需要如实报告注册结果；remove_job 只捕 JobLookupError（其余异常是真实
调度器故障，不能再吞）。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.data_channel.sync_tasks.scheduler import SyncScheduler


def _task(task_id="t-1", *, enabled=True, schedule_type="CRON",
          cron="*/5 * * * *", interval=0, name="任务A") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id, enabled=enabled, schedule_type=schedule_type,
        cron_expression=cron, interval_seconds=interval, name=name)


def test_add_job_for_pipeline_task_reports_cron_registration_success():
    scheduler = SyncScheduler()
    try:
        assert scheduler._add_job_for_pipeline_task(
            _task(cron="*/5 * * * *")) is True
    finally:
        scheduler.shutdown()


def test_add_job_for_pipeline_task_reports_invalid_cron_as_failure():
    scheduler = SyncScheduler()
    try:
        assert scheduler._add_job_for_pipeline_task(
            _task(cron="*/5 * *")) is False  # 段数不合法必须可见
    finally:
        scheduler.shutdown()


def test_add_job_for_pipeline_task_treats_disabled_and_manual_as_intentional():
    scheduler = SyncScheduler()
    try:
        assert scheduler._add_job_for_pipeline_task(
            _task(enabled=False, cron="")) is True
        assert scheduler._add_job_for_pipeline_task(
            _task(schedule_type="MANUAL", cron="")) is True
        assert scheduler._add_job_for_pipeline_task(
            _task(schedule_type="INTERVAL", interval=60)) is True
    finally:
        scheduler.shutdown()


def test_add_job_for_pipeline_task_reports_empty_cron_as_failure():
    """CRON 任务 cron 表达式为空：不会注册任何调度，不得假报成功。"""
    scheduler = SyncScheduler()
    try:
        assert scheduler._add_job_for_pipeline_task(
            _task(cron="")) is False
    finally:
        scheduler.shutdown()


def test_add_job_for_pipeline_task_reports_non_positive_interval_as_failure():
    """INTERVAL 任务秒数非正：不会注册任何调度，不得假报成功。"""
    scheduler = SyncScheduler()
    try:
        assert scheduler._add_job_for_pipeline_task(
            _task(schedule_type="INTERVAL", interval=0, cron="")) is False
        assert scheduler._add_job_for_pipeline_task(
            _task(schedule_type="INTERVAL", interval=-30, cron="")) is False
    finally:
        scheduler.shutdown()


def test_reload_pipeline_task_returns_false_when_scheduler_not_started():
    scheduler = SyncScheduler()
    assert scheduler.started is False
    assert scheduler.reload_pipeline_task("t-1") is False


def test_refresh_scheduler_reports_not_started(monkeypatch):
    """调度器未启动时 _refresh_scheduler 必须返回 not_started（响应可见）。"""
    from app.data_channel.pipeline_tasks import lifecycle_service

    class _NotStarted:
        started = False

        def reload_pipeline_task(self, _task_id):
            raise AssertionError("未启动的调度器不应被要求 reload")

    monkeypatch.setattr(
        "app.data_channel.sync_tasks.scheduler.get_sync_scheduler",
        lambda: _NotStarted(),
    )
    assert lifecycle_service._refresh_scheduler("t-1") == {
        "status": "not_started"}


def test_refresh_scheduler_reports_failed_reload(monkeypatch):
    from app.data_channel.pipeline_tasks import lifecycle_service

    class _FailingReload:
        started = True

        def reload_pipeline_task(self, _task_id):
            return False

    monkeypatch.setattr(
        "app.data_channel.sync_tasks.scheduler.get_sync_scheduler",
        lambda: _FailingReload(),
    )
    assert lifecycle_service._refresh_scheduler("t-1") == {"status": "failed"}
