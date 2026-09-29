"""v2 增量更新触发 API——HTTP 回调入口已退役。

IncrementalOrchestrator 的 on_dataset_version_published / on_review_approved
仍由 datasets/version_events.py 的 durable 事件链内部消费；这里只把
HTTP 回调面（on_connection_sync / on_pipeline_success / on_review_approved）
退役为 410。观察一个发布周期确认无外部系统回调后，再删路由与
OpenAPI 条目；编排器方法体在确认前保持原样。
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])

_RETIRED_DETAIL = (
    "增量回调 HTTP 入口已停用；成品审核与版本发布走 durable 事件链自动"
    "触发，任务调度请使用数据任务池（/api/v2/pipeline-tasks）。"
)


def _reject_retired_callback() -> None:
    raise HTTPException(status_code=410, detail=_RETIRED_DETAIL)


@router.post("/connections/{connection_id}/sync-complete")
def notify_sync_complete(connection_id: str, dataset_id: str):
    _reject_retired_callback()


@router.post("/pipeline-runs/{run_id}/complete")
def notify_pipeline_complete(run_id: str):
    _reject_retired_callback()


@router.post("/reviews/{review_id}/approve-trigger")
def trigger_on_approve(review_id: str):
    _reject_retired_callback()
