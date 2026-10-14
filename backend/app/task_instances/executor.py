"""任务实例 — 确定性假执行器（M1 执行体，M2 由 Claude Code worker 替换）。

设计目标：让引擎全链路（派发 → 认领 → 契约校验 → 纠正 → 推进）在
无 Docker / 无 LLM 的环境下可回归。产出按端口契约合成
（contracts.synthesize），并服从实例 inputs 中的仿真指令（__simulate），
使黄金轨迹与失败/纠正场景全部确定性可测：

  inputs.__simulate.outputs      = {"<node_id>": <output 覆盖>}
  inputs.__simulate.bad_outputs  = {"<node_id>": [<attempt_no>, ...]}  # 触发契约违规
  inputs.__simulate.fail_nodes   = {"<node_id>": [<attempt_no>, ...]}  # 执行器报错
  inputs.__simulate.hold_nodes   = ["<node_id>", ...]                 # 认领后停在 running

NATS handler（run_control_message）与真实 worker 共用同一 payload 契约：
{"kind": "dispatch"|"steer", "node_run_id": ..., "message_id": ...}。
execute_dispatch_on 消费方提供会话（运行时开 SessionLocal，测试传夹具库）。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.task_instances import contracts as contract_engine
from app.task_instances import engine
from app.task_instances.models import TaskInstance, TaskNodeRun
from app.task_instances.spec import DEFAULT_OUTPUT_PORT, NODE_AGENT

logger = logging.getLogger(__name__)

_SIMULATE_KEY = "__simulate"


def _simulate_directives(instance: TaskInstance) -> dict:
    inputs = instance.inputs or {}
    directives = inputs.get(_SIMULATE_KEY)
    return directives if isinstance(directives, dict) else {}


def synthesize_output(instance: TaskInstance, spec: dict, node_id: str, *,
                       seed_suffix: str = "") -> dict:
    """按端口契约合成产出；outputs 覆盖优先；无契约给确定性摘要。

    seed 含 attempt 后缀：重做/纠正后的再执行产出与上次不同——
    与真实执行体语义一致，也让下游 inputs_hash 变化以派生新尝试。
    """
    node = (spec.get("nodes") or {}).get(node_id, {})
    port = (node.get("outputs") or {}).get(DEFAULT_OUTPUT_PORT) or {}
    overrides = _simulate_directives(instance).get("outputs") or {}
    if node_id in overrides:
        return overrides[node_id]
    contract_name = port.get("contract")
    contracts = spec.get("contracts") or {}
    seed = f"{instance.id}:{node_id}:{seed_suffix}"
    if contract_name and contract_name in contracts:
        return contract_engine.synthesize(contracts[contract_name], seed=seed)
    return {"summary": f"[fake] output of {node_id} attempt {seed_suffix}",
            "instance": instance.id}


async def run_control_message(payload: dict) -> None:
    """NATS 消费入口（task_worker 服务）。

    执行模式由 TASK_INSTANCES_EXECUTOR 决定：docker（生产 task_worker，
    容器执行体）/ fake（本地开发默认与测试，确定性假执行器）。业务异常在
    handler 内消化（外抛会触发 nak 重投放大），与 reflection_tasks 同纪律。
    """
    import asyncio
    import os

    try:
        node_run_id = payload.get("node_run_id")
        if not node_run_id:
            logger.warning("task_instances 控制消息缺少 node_run_id: %s", payload)
            return
        mode = os.environ.get("TASK_INSTANCES_EXECUTOR", "fake")
        if payload.get("kind") == "steer":
            if mode == "docker":
                from app.task_instances.container_runtime import drop_steering

                await asyncio.to_thread(
                    drop_steering, node_run_id,
                    payload.get("message_id"), str(payload.get("content") or ""))
                return
            db: Session = SessionLocal()
            try:
                _mark_steering_delivered(db, node_run_id,
                                         payload.get("message_id"))
            finally:
                db.close()
            return
        if payload.get("kind") == "dispatch":
            if mode == "docker":
                from app.task_instances.container_runtime import (
                    execute_dispatch_docker,
                )

                cascaded = await asyncio.to_thread(
                    execute_dispatch_docker, node_run_id)
                if cascaded is not None:
                    for cascade_id in cascaded.dispatches:
                        _publish_dispatch(cascade_id)
                return
            db = SessionLocal()
            try:
                cascaded = execute_dispatch_on(db, node_run_id)
            finally:
                db.close()
            if cascaded is not None:
                # 级联派发再发布：节点完成触发的下游执行不等待对账重投
                for cascade_id in cascaded.dispatches:
                    _publish_dispatch(cascade_id)
    except Exception:  # noqa: BLE001 — 消化业务异常，防 nak 风暴
        logger.exception("task_instances 控制消息处理失败: %s", payload)


def _publish_dispatch(node_run_id: str) -> None:
    """级联派发的 NATS 发布（失败靠对账重投兜底）。"""
    try:
        from app.data_channel.pipeline_tasks.dispatch import dispatch_task

        dispatch_task("task_instances.control",
                      {"kind": "dispatch", "node_run_id": node_run_id})
    except Exception:  # noqa: BLE001
        logger.exception("级联派发发布失败: %s", node_run_id)


def execute_dispatch_on(db: Session, node_run_id: str):
    """在给定会话上执行一次节点派发（认领 → 产出 → 交活）。

    返回交活产生的级联派发（供测试循环推进；运行时路径丢弃）。
    """
    run = db.query(TaskNodeRun).filter(TaskNodeRun.id == node_run_id).first()
    if run is None:
        logger.warning("假执行器：节点尝试不存在 %s", node_run_id)
        return
    instance = db.query(TaskInstance).filter(
        TaskInstance.id == run.instance_id).first()
    if instance is None or instance.status != "active":
        return
    spec = instance.spec_snapshot or {}
    if (spec.get("nodes") or {}).get(run.node_id, {}).get("kind") != NODE_AGENT:
        return
    engine.claim_node(db, run.id, owner="fake-executor", lease_minutes=30)
    db.commit()
    directives = _simulate_directives(instance)
    hold = directives.get("hold_nodes") or []
    if run.node_id in hold:
        return  # 停在 running（插话/租约对账测试用）
    fail_map = directives.get("fail_nodes") or {}
    if run.attempt_no in (fail_map.get(run.node_id) or []):
        from app.task_instances.container_runtime import _fail_attempt_on

        _fail_attempt_on(db, run, "fake executor simulated failure")
        db.commit()
        return
    bad_map = directives.get("bad_outputs") or {}
    if run.attempt_no in (bad_map.get(run.node_id) or []):
        # 提交一个确定不合规的产出，触发契约纠正环
        cascaded = engine.complete_node(db, run.id,
                                         {"__deliberately_invalid__": True},
                                         actor="executor:fake")
        db.commit()
        return cascaded
    output = synthesize_output(instance, spec, run.node_id,
                                seed_suffix=str(run.attempt_no))
    cascaded = engine.complete_node(db, run.id, output, actor="executor:fake")
    db.commit()
    return cascaded


def _mark_steering_delivered(db: Session, node_run_id: str,
                             message_id: str | None) -> None:
    """M1：假执行器即时确认送达（真实插话在 M2 worker 内实现）。"""
    from app.task_instances.models import STEERING_ACKNOWLEDGED, TaskSteeringMessage

    query = db.query(TaskSteeringMessage).filter(
        TaskSteeringMessage.node_run_id == node_run_id)
    message = (query.filter(TaskSteeringMessage.id == message_id).first()
               if message_id else query.first())
    if message is not None:
        message.status = STEERING_ACKNOWLEDGED
        db.commit()
