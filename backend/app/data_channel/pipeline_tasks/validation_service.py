"""Published-pipeline contract validation for Pipeline Task writes."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.pipeline_tasks.models import PipelineTask
from app.data_channel.pipelines.models import Pipeline


def ensure_pipeline_ready_for_enable(db: Session, pipeline_id: str) -> None:
    """启用任务前校验关联流水线仍可调度（409，与启停开关同一规则）。

    停用期间允许编辑改名/改调度；只有「停用 → 启用」这一转换要求
    流水线已发布且未停用，避免向导保存绕过开关接口的 409。
    """
    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if (
        not pipeline
        or (pipeline.status or "draft") != "published"
        or pipeline.enabled is False
    ):
        raise HTTPException(
            409,
            "关联流水线未发布或已停用，不能启用该调度任务",
        )


def _validate(
    db: Session,
    body,
    existing: PipelineTask | None = None,
) -> tuple[Pipeline, str]:
    from app.data_channel.datasets.lake_gate import (
        contract_pk,
        normalize_definitions,
        split_pk,
    )

    def get_value(key):
        value = getattr(body, key, None)
        if value is None and existing is not None:
            return getattr(existing, key, None)
        return value

    pipeline_id = get_value("pipeline_id")
    if not pipeline_id:
        raise HTTPException(400, "必须选择要调度的流水线")
    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pipeline:
        raise HTTPException(400, "所选流水线不存在")
    if (pipeline.status or "draft") != "published":
        raise HTTPException(
            400,
            f"流水线「{pipeline.name}」尚未发布，任务只能调度已发布的流水线。"
            "请先在流水线编辑向导中完成发布。",
        )
    # 启用校验只在新建/换绑流水线时做：已有任务的流水线被临时停用时，
    # 改名等编辑不应被拦（执行时引擎自会跳过停用的流水线）
    picking_pipeline = (
        existing is None or pipeline_id != existing.pipeline_id
    )
    if picking_pipeline and pipeline.enabled is False:
        raise HTTPException(
            400,
            f"流水线「{pipeline.name}」未启用，任务只能挂接已启用的流水线。"
            "请先在流水线列表打开启用开关。",
        )

    # 主键只有一个权威源：流水线发布契约。任务请求里的 primary_key 仅用于
    # 兼容旧客户端，允许留空或传入同值，绝不允许自行补充/改写。
    pipeline_primary_key = contract_pk(pipeline.column_definitions)
    requested_primary_key = (
        getattr(body, "primary_key", None) or ""
    ).strip()
    if (
        requested_primary_key
        and split_pk(requested_primary_key)
        != split_pk(pipeline_primary_key)
    ):
        raise HTTPException(
            400,
            "数据任务不再定义主键；主键必须来自已发布流水线的数据契约"
            + (
                f"（当前契约：{pipeline_primary_key}）"
                if pipeline_primary_key
                else "（当前流水线未声明主键）"
            ),
        )
    if (
        get_value("write_mode") == "upsert"
        and not pipeline_primary_key
    ):
        raise HTTPException(
            400,
            "「主键合并」要求流水线在发布契约中声明主键；"
            "请返回流水线补齐契约后重新发布",
        )

    soft_delete = (get_value("soft_delete_column") or "").strip()
    contract_columns = {
        definition["field_key"]
        for definition in normalize_definitions(
            pipeline.column_definitions
        )
    }
    if soft_delete and soft_delete not in contract_columns:
        raise HTTPException(
            400,
            f"软删除列「{soft_delete}」不在流水线发布契约中",
        )

    cursor_column = (get_value("cursor_column") or "").strip()
    if cursor_column and cursor_column not in contract_columns:
        raise HTTPException(
            400,
            f"增量游标列「{cursor_column}」不在流水线发布契约中",
        )

    schedule_type = get_value("schedule_type")
    if schedule_type == "CRON":
        expression = (get_value("cron_expression") or "").strip()
        if not expression:
            raise HTTPException(400, "CRON 调度必须填写 cron 表达式")
        if len(expression.split()) != 5:
            raise HTTPException(
                400,
                "cron 表达式须为 5 段格式：分 时 日 月 周",
            )
        try:
            from apscheduler.triggers.cron import CronTrigger

            CronTrigger.from_crontab(expression)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                400,
                f"cron 表达式无效：{exc}",
            ) from exc
    elif schedule_type == "INTERVAL":
        interval = get_value("interval_seconds")
        if not interval or interval < 10:
            raise HTTPException(
                400,
                "固定间隔调度的间隔必须 ≥ 10 秒",
            )
    return pipeline, pipeline_primary_key
