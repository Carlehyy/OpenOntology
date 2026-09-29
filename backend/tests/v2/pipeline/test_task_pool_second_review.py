"""数据任务池二次审查修复项的回归测试。

覆盖：
- 编辑保存「停用 → 启用」必须走与启停开关相同的流水线状态校验（409），
  已启用任务的改名/改调度不受影响（停用期间允许编辑）；
- 调度刷新失败时保存/启停响应携带 scheduler_refresh 状态，保存本身仍成功；
  SyncScheduler.reload_pipeline_task 以布尔返回注册结果；
- 旧 DataSyncTask 读接口、增量回调 HTTP 入口按退役纪律返回 410。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.data_channel.pipeline_tasks import lifecycle_service
from app.data_channel.pipeline_tasks.models import PipelineTask
from app.data_channel.pipeline_tasks.router import (
    PipelineTaskCreate,
    PipelineTaskUpdate,
    create_task as router_create_task,
    toggle_task as router_toggle_task,
    update_task as router_update_task,
)
from app.data_channel.sync_tasks import incremental_router
from app.data_channel.sync_tasks import router as sync_router
from app.data_channel.sync_tasks.scheduler import SyncScheduler
from app.models.v2.pipeline import Pipeline

_DEFINITIONS = [
    {"field_key": "id", "field_name": "ID", "field_type": "string",
     "is_primary_key": True, "nullable": False},
]


def _pipeline(db, pipe_id="pipe-a", *, status="published", enabled=True):
    pipe = Pipeline(
        id=pipe_id, name=f"流水线{pipe_id}", spec={}, status=status,
        enabled=enabled, column_definitions=_DEFINITIONS,
    )
    db.add(pipe)
    db.commit()
    return pipe


def _task(db, pipeline_id="pipe-a", task_id="task-1", *, enabled=False):
    task = PipelineTask(
        id=task_id, name="调度任务", description="d",
        pipeline_id=pipeline_id, write_mode="overwrite", primary_key="id",
        schedule_type="MANUAL", enabled=enabled, status="idle",
    )
    db.add(task)
    db.commit()
    return task


def _body(pipe_id="pipe-a", **overrides) -> PipelineTaskUpdate:
    data = {"name": "调度任务", "description": "d", "pipeline_id": pipe_id,
            "write_mode": "overwrite", "schedule_type": "MANUAL"}
    data.update(overrides)
    return PipelineTaskUpdate(**data)


def _no_scheduler(monkeypatch):
    monkeypatch.setattr(
        "app.data_channel.pipeline_tasks.router._refresh_scheduler",
        lambda _task_id: None,
    )


# ── 停用 → 启用走与启停开关相同的校验 ────────────────────────


def test_update_enabling_disabled_task_rejects_stopped_pipeline(
        db, monkeypatch):
    # 已发布但已停用的流水线：validation 不拦（非换绑不看 enabled），
    # 此前向导保存 enabled=true 可绕过开关接口的 409——现在同样 409
    _no_scheduler(monkeypatch)
    _pipeline(db, "pipe-a", status="published", enabled=False)
    _task(db, "pipe-a", enabled=False)

    with pytest.raises(HTTPException) as ei:
        router_update_task("task-1", _body(enabled=True), db)
    assert ei.value.status_code == 409
    assert "不能启用" in ei.value.detail


def test_toggle_enabling_rejects_stopped_pipeline(db, monkeypatch):
    # 既有开关行为不变：409 与向导保存同一文案
    _no_scheduler(monkeypatch)
    _pipeline(db, "pipe-a", status="published", enabled=False)
    _task(db, "pipe-a", enabled=False)

    with pytest.raises(HTTPException) as ei:
        router_toggle_task("task-1", True, db)
    assert ei.value.status_code == 409


def test_update_enabled_task_rename_not_blocked_by_stopped_pipeline(
        db, monkeypatch):
    # 已启用任务的改名/改调度不受影响：停用期间允许编辑
    _no_scheduler(monkeypatch)
    _pipeline(db, "pipe-a", status="published", enabled=False)
    _task(db, "pipe-a", enabled=True)

    result = router_update_task("task-1", _body(name="新名字"), db)
    assert result["name"] == "新名字"
    assert result["enabled"] is True


def test_update_enabling_with_rebind_to_healthy_pipeline_allowed(
        db, monkeypatch):
    # 换绑场景校验作用于保存后生效的流水线：新流水线健康即允许启用
    _no_scheduler(monkeypatch)
    _pipeline(db, "pipe-old", status="published", enabled=False)
    _pipeline(db, "pipe-new", status="published", enabled=True)
    _task(db, "pipe-old", enabled=False)

    result = router_update_task(
        "task-1", _body(pipe_id="pipe-new", enabled=True), db)
    assert result["enabled"] is True
    assert result["pipeline_id"] == "pipe-new"


def test_update_enabling_disabled_task_with_healthy_pipeline_allowed(
        db, monkeypatch):
    _no_scheduler(monkeypatch)
    _pipeline(db, "pipe-a", status="published", enabled=True)
    _task(db, "pipe-a", enabled=False)

    result = router_update_task("task-1", _body(enabled=True), db)
    assert result["enabled"] is True


def test_update_disabled_task_without_enabled_field_not_blocked(
        db, monkeypatch):
    # 停用期编辑（body 不带 enabled，exclude_unset 回落当前停用态）：
    # 不构成「停用 → 启用」转换，不走 409 校验
    _no_scheduler(monkeypatch)
    _pipeline(db, "pipe-a", status="published", enabled=False)
    _task(db, "pipe-a", enabled=False)

    result = router_update_task("task-1", _body(name="仅改名"), db)
    assert result["name"] == "仅改名"
    assert result["enabled"] is False


def test_update_disabled_task_with_explicit_disable_not_blocked(
        db, monkeypatch):
    # 显式 enabled=false：falsy，同样不触发启用校验
    _no_scheduler(monkeypatch)
    _pipeline(db, "pipe-a", status="published", enabled=False)
    _task(db, "pipe-a", enabled=False)

    result = router_update_task("task-1", _body(enabled=False), db)
    assert result["enabled"] is False


# ── 调度刷新降级提示：保存成功 + 响应带 scheduler_refresh ────


def test_refresh_scheduler_reports_not_started_when_scheduler_down():
    # 测试进程从不 start 调度器：刷新函数必须以结构化状态回报，而非静默
    result = lifecycle_service._refresh_scheduler("task-1")
    assert result == {"status": "not_started"}


def test_refresh_scheduler_swallows_unexpected_import_failure(monkeypatch):
    # 导入/单例层面的窄失败：不抛错（保存已 commit 不能回滚），回报 failed
    def _boom():
        raise RuntimeError("scheduler module broken")

    monkeypatch.setattr(
        "app.data_channel.sync_tasks.scheduler.get_sync_scheduler", _boom)
    result = lifecycle_service._refresh_scheduler("task-1")
    assert result["status"] == "failed"
    assert "scheduler module broken" in result["error"]


def test_create_task_response_carries_scheduler_refresh(db, monkeypatch):
    # 调度器未启动时创建仍成功，但响应必须带调度注册状态
    _pipeline(db, "pipe-a", status="published", enabled=True)

    class _User:
        id = "user-1"

    result = router_create_task(
        PipelineTaskCreate(
            name="新任务", description="d", pipeline_id="pipe-a",
            write_mode="overwrite", schedule_type="MANUAL", enabled=True,
        ),
        db,
        current_user=_User(),
    )
    assert result["name"] == "新任务"
    assert result["scheduler_refresh"]["status"] == "not_started"


def test_toggle_response_carries_scheduler_refresh(db, monkeypatch):
    _pipeline(db, "pipe-a", status="published", enabled=True)
    _task(db, "pipe-a", enabled=False)

    result = router_toggle_task("task-1", True, db)
    assert result["enabled"] is True
    assert result["scheduler_refresh"]["status"] == "not_started"


# ── 调度器 reload 的布尔契约 ────────────────────────────────


def test_reload_pipeline_task_false_when_not_started():
    assert SyncScheduler().reload_pipeline_task("t") is False


def test_add_job_for_pipeline_task_bool_semantics():
    scheduler = SyncScheduler()
    scheduler._scheduler = MagicMock()

    # 停用 → 摘除 Job 属预期结果
    assert scheduler._add_job_for_pipeline_task(
        SimpleNamespace_task(enabled=False, schedule_type="CRON",
                             cron_expression="0 2 * * *")) is True
    # MANUAL → 按设计不注册
    assert scheduler._add_job_for_pipeline_task(
        SimpleNamespace_task(enabled=True, schedule_type="MANUAL",
                             cron_expression="")) is True
    # 非法 cron → 注册失败
    assert scheduler._add_job_for_pipeline_task(
        SimpleNamespace_task(enabled=True, schedule_type="CRON",
                             cron_expression="0 2 * *")) is False
    # 合法 cron → 注册成功
    assert scheduler._add_job_for_pipeline_task(
        SimpleNamespace_task(enabled=True, schedule_type="CRON",
                             cron_expression="0 2 * * *")) is True


def SimpleNamespace_task(**kwargs):
    from types import SimpleNamespace

    defaults = {"id": "t-1", "name": "n", "interval_seconds": 0}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── 旧读接口与增量回调退役为 410 ────────────────────────────


@pytest.mark.parametrize("call", [
    lambda: sync_router.stats_overview(),
    lambda: sync_router.scheduler_status(),
    lambda: sync_router.list_tasks(),
    lambda: sync_router.get_task("t-1"),
    lambda: sync_router.list_histories("t-1"),
    lambda: sync_router.list_source_tables("c-1"),
    lambda: sync_router.preview_source_table("c-1", "tbl"),
])
def test_sync_read_endpoints_retired_to_410(call):
    with pytest.raises(HTTPException) as ei:
        call()
    assert ei.value.status_code == 410
    assert "数据任务池" in ei.value.detail


@pytest.mark.parametrize("call", [
    lambda: incremental_router.notify_sync_complete("c-1", "ds-1"),
    lambda: incremental_router.notify_pipeline_complete("run-1"),
    lambda: incremental_router.trigger_on_approve("rev-1"),
])
def test_incremental_callbacks_retired_to_410(call):
    with pytest.raises(HTTPException) as ei:
        call()
    assert ei.value.status_code == 410
