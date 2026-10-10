"""任务实例 — 容器执行运行时（M2，设计方案 §6）。

task_worker 服务（backend 同镜像、独立入口消费 task_instances.control）
在每个 agent 节点上起一个 Docker 容器：
  - 镜像 openontology-task-agent（node22 + Claude Agent SDK runner）；
  - 工作区 bind mount 到 uploads/task-instances/<instance>/<node_run>/，
    容器内 /workspace 唯一可写根（只读镜像层由 --read-only 保证）；
  - 模型注入：spawn 时解析 model_configs（节点级覆盖 → purpose 默认），
    以 ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL 环境变量注入——密钥只在
    容器环境，不落库不进事件（§6.3）；
  - 插话：宿主直接向 bind-mount 的 .steering/ 落 JSON，runner 侧
    fs.watch 拉入运行中的开放输入流（R1 spike 已实证 mid-turn 折叠）；
  - 产物：runner 落 /workspace/output/，回收时逐件 sha256 + MinIO
    上传后才交活（容器销毁不丢证据，§6.2）。

docker CLI 走 _docker 单点（测试 seam）；执行语义与假执行器一致：
认领 → 执行 → 交活（契约校验/纠正环由引擎统一收口）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.task_instances import engine as ti_engine
from app.task_instances import events as ev

logger = logging.getLogger(__name__)

_STEERING_DIR = ".steering"
_OUTPUT_DIR = ".ti-output"
_ARTIFACT_DIR = "output"
_TASK_FILE = ".ti-task.json"
_OUTPUT_FILE = f"{_OUTPUT_DIR}/output.json"
_POLL_INTERVAL_SECONDS = 3.0


class ContainerRuntimeError(Exception):
    """容器执行基础设施错误（节点按执行失败收口）。"""


class ModelProtocolError(ContainerRuntimeError):
    """所选模型配置非 Anthropic 协议（v1 约束，设计 §6.3）。"""


def _settings():
    from app.config import settings

    return settings


def workspace_root() -> Path:
    return Path(_settings().uploads_dir) / "task-instances"


def workspace_of(instance_id: str, node_run_id: str) -> Path:
    return workspace_root() / instance_id / node_run_id


def _docker(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """docker CLI 单点 seam（测试 monkeypatch 目标）。"""
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout)


def _container_name(node_run_id: str) -> str:
    return f"taskinst-{node_run_id[:12]}"


# ---------------------------------------------------------------------------
# 模型注入
# ---------------------------------------------------------------------------


def resolve_model_env(db: Session, model_config_id: str | None) -> dict[str, str]:
    """节点级覆盖 → task_instances 用途默认 → 平台默认；仅认 Anthropic 协议。"""
    from app.model_configs.models import ModelConfig
    from app.model_configs.selector import llm_call_kwargs, select_llm_model_config

    config = None
    if model_config_id:
        config = db.query(ModelConfig).filter(ModelConfig.id == model_config_id).first()
    if config is None:
        config = select_llm_model_config(db, purpose_tags=("task_instances",))
    if config is None:
        config = select_llm_model_config(db, purpose_tags=("super_assistant",))
    if config is None:
        raise ModelProtocolError("无可用模型配置：请先在系统设置配置模型")
    kwargs = llm_call_kwargs(config)
    if not kwargs or not kwargs.get("model"):
        raise ModelProtocolError(f"模型配置「{config.name}」缺少可用模型名")
    api_base = kwargs.get("api_base") or ""
    anthropic_like = (kwargs.get("provider") == "anthropic"
                      or "anthropic" in api_base)
    if not anthropic_like:
        raise ModelProtocolError(
            f"模型配置「{config.name}」provider={kwargs.get('provider')} 不支持："
            "agent 节点执行体（Claude Code）仅认 Anthropic 协议配置")
    env = {
        "ANTHROPIC_AUTH_TOKEN": kwargs.get("api_key") or "",
        "ANTHROPIC_MODEL": kwargs["model"],
    }
    if api_base:
        env["ANTHROPIC_BASE_URL"] = api_base
    return env


# ---------------------------------------------------------------------------
# 任务渲染
# ---------------------------------------------------------------------------


def render_task(spec_node: dict, instance, inputs_list: list[dict]) -> dict:
    """把节点说明 + 输入上下文 + 输出协议渲染为 runner 任务 JSON。"""
    outputs_desc = spec_node.get("outputs") or {}
    lines: list[str] = [
        f"# 任务目标\n{instance.goal}",
        "",
        "# 输入上下文",
    ]
    for item in inputs_list or []:
        kind = item.get("kind")
        if kind == "taskContext":
            lines.append(f"- 实例入参：{json.dumps(item.get('inputs') or {}, ensure_ascii=False)}")
        elif kind == "output":
            lines.append(f"- 上游节点 {item.get('ref')} 的产出："
                         f"{json.dumps(item.get('output') or {}, ensure_ascii=False)}")
        elif kind == "correction":
            lines.append(f"- 【纠正】上次产出违反契约：{item.get('violations')}；"
                         "请修正后重新交付")
        elif kind == "rejection":
            lines.append(f"- 【驳回】下游 {item.get('rejector')} 驳回了你的产出，"
                         f"理由：{item.get('reason')}；请按理由重做")
    lines += [
        "",
        "# 交付要求",
        "- 完成后，把最终结果作为一个 JSON 对象写入 `/workspace/.ti-output/output.json`，"
        "并在最后一条回复中原样输出该 JSON。",
        "- 需要交付文件时写入 `/workspace/output/` 目录，并在 output.json 的 "
        "`artifacts` 数组中列出：[{\"name\": \"文件名\", \"path\": \"output/文件名\", "
        "\"mime_type\": \"text/plain\"}]。",
    ]
    if outputs_desc:
        lines.append(f"- 输出端口定义（如声明 contract 必须严格遵守）："
                     f"{json.dumps(outputs_desc, ensure_ascii=False)}")
    contracts = (instance.spec_snapshot or {}).get("contracts") or {}
    for port in outputs_desc.values():
        name = (port or {}).get("contract")
        if name and name in contracts:
            lines.append(f"- 端口契约 {name}："
                         f"{json.dumps(contracts[name], ensure_ascii=False)}")
    return {
        "system": spec_node.get("system") or "你是严谨的任务执行者。",
        "prompt": "\n".join(lines),
        "max_turns": 40,
    }


# ---------------------------------------------------------------------------
# 执行入口（task_worker 消费侧调用）
# ---------------------------------------------------------------------------


def execute_dispatch_docker(node_run_id: str):
    """完整执行一个 agent 节点：容器生命周期 + 产物回收 + 引擎交活。"""
    db: Session = SessionLocal()
    try:
        return _execute(db, node_run_id)
    finally:
        db.close()


def _execute(db: Session, node_run_id: str):
    from app.task_instances.models import (
        NODE_DISPATCHED,
        NODE_RUNNING,
        TaskInstance,
        TaskNodeRun,
    )
    from app.task_instances.spec import NODE_AGENT

    run = db.query(TaskNodeRun).filter(TaskNodeRun.id == node_run_id).first()
    if run is None:
        logger.warning("容器执行：节点尝试不存在 %s", node_run_id)
        return None
    instance = db.query(TaskInstance).filter(
        TaskInstance.id == run.instance_id).first()
    if instance is None or instance.status != "active":
        return None
    spec = instance.spec_snapshot or {}
    node = (spec.get("nodes") or {}).get(run.node_id, {})
    if node.get("kind") != NODE_AGENT or run.status not in (
            NODE_DISPATCHED, NODE_RUNNING):
        return None
    timeout_minutes = int(node.get("timeout_minutes") or 30)
    try:
        ti_engine.claim_node(db, run.id, owner=_container_name(run.id),
                             lease_minutes=timeout_minutes)
        db.commit()
    except ti_engine.EngineGuardError:
        db.rollback()
        return None

    try:
        env = resolve_model_env(db, run.model_config_id)
        task = render_task(node, instance, run.inputs or [])
        result = _run_container_and_collect(db, run, instance, task, env,
                                            timeout_minutes)
        db.commit()
        return result
    except ContainerRuntimeError as exc:
        db.rollback()
        _fail(db, run, f"容器执行失败: {exc}")
        return None


def _run_container_and_collect(db: Session, run, instance, task: dict,
                               env: dict[str, str],
                               timeout_minutes: int):
    ws = workspace_of(instance.id, run.id)
    (ws / _OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    (ws / _STEERING_DIR).mkdir(parents=True, exist_ok=True)
    (ws / _ARTIFACT_DIR).mkdir(parents=True, exist_ok=True)
    (ws / _TASK_FILE).write_text(json.dumps(task, ensure_ascii=False),
                                 encoding="utf-8")
    settings = _settings()
    image = getattr(settings, "task_agent_image", None) or "openontology-task-agent:local"
    network = getattr(settings, "task_agent_network", None) or "bridge"
    name = _container_name(run.id)
    args = [
        "run", "-d", "--rm", "--name", name,
        "--network", network, "--read-only",
        "--user", "1000:1000",
        "--tmpfs", "/tmp:rw,nosuid,size=64m",
        "-v", f"{ws}:/workspace", "-w", "/workspace",
    ]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += [image]
    started = _docker(args)
    if started.returncode != 0:
        raise ContainerRuntimeError(
            f"docker run 失败: {started.stderr.strip()[:500]}")
    deadline = time.monotonic() + timeout_minutes * 60
    exit_code: int | None = None
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        status = _docker(["inspect", "-f",
                          "{{.State.Status}}:{{.State.ExitCode}}", name],
                         timeout=15)
        if status.returncode != 0:
            break  # 容器已消失（--rm）：以产物为准
        state = status.stdout.strip()
        if state.startswith("exited:"):
            try:
                exit_code = int(state.split(":", 1)[1] or 0)
            except ValueError:
                exit_code = -1
            break
    else:
        _docker(["stop", name], timeout=30)
        raise ContainerRuntimeError(f"节点执行超时（{timeout_minutes} 分钟）")
    output_path = ws / _OUTPUT_FILE
    if exit_code not in (0, None) or not output_path.exists():
        detail = ""
        if output_path.exists():
            detail = output_path.read_text(encoding="utf-8")[:300]
        raise ContainerRuntimeError(
            f"runner 异常退出 code={exit_code} output={detail!r}")
    try:
        output = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(output, dict):
            raise ValueError("output.json 必须是 JSON 对象")
    except (ValueError, OSError) as exc:
        raise ContainerRuntimeError(f"产出解析失败: {exc}") from exc
    artifacts = []
    for entry in output.get("artifacts") or []:
        path = ws / str(entry.get("path") or "")
        if not path.is_file():
            raise ContainerRuntimeError(
                f"产物缺失: {entry.get('name')} -> {entry.get('path')}")
        content = path.read_bytes()
        artifacts.append({
            "name": str(entry.get("name") or path.name),
            "mime_type": str(entry.get("mime_type")
                             or "application/octet-stream"),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "storage_uri": _upload_artifact(instance.id, run.id,
                                            str(entry.get("name") or path.name),
                                            content,
                                            str(entry.get("mime_type")
                                                or "application/octet-stream")),
        })
    return ti_engine.complete_node(
        db, run.id, output, artifacts=artifacts, actor="executor:docker")


def _upload_artifact(instance_id: str, node_run_id: str, name: str,
                     content: bytes, mime_type: str) -> str:
    """产物上传 seam（测试可替换；默认 MinIO）。"""
    from app.task_instances.service import _write_artifact_object

    return _write_artifact_object(
        f"{instance_id}/{node_run_id}/{name}", content, mime_type)


def _fail(db: Session, run, error: str) -> None:
    from app.task_instances.executor import _fail_attempt_on

    try:
        _fail_attempt_on(db, run, error)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("容器执行失败收口失败: %s", run.id)


# ---------------------------------------------------------------------------
# 插话（宿主侧落盘，runner fs.watch 拉取）
# ---------------------------------------------------------------------------


def drop_steering(node_run_id: str, message_id: str | None,
                  content: str) -> None:
    """把插话写入节点工作区 .steering/（bind mount 即时可见）。"""
    from app.task_instances.models import TaskNodeRun

    db: Session = SessionLocal()
    try:
        run = db.query(TaskNodeRun).filter(TaskNodeRun.id == node_run_id).first()
        if run is None:
            return
        ws = workspace_of(run.instance_id, run.id)
        steering_dir = ws / _STEERING_DIR
        steering_dir.mkdir(parents=True, exist_ok=True)
        payload = {"id": message_id or uuid.uuid4().hex, "content": content}
        (steering_dir / f"{uuid.uuid4().hex}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        ev.append_event(db, run.instance_id, ev.STEERING_DELIVERED,
                        {"node_id": run.node_id, "message_id": payload["id"]},
                        node_run_id=run.id, actor="executor:docker",
                        event_budget=None)
        db.commit()
    except Exception:  # noqa: BLE001 — 插话是旁路能力
        db.rollback()
        logger.exception("插话落盘失败: %s", node_run_id)
    finally:
        db.close()
