"""容器执行运行时测试（M2）：docker CLI monkeypatch，无真实 Docker。

覆盖：容器执行 → 产物回收 → 引擎交活；非 Anthropic 协议拒绝；
容器失败收口；超时收口；插话落盘；注册表独占开关。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.task_instances.conftest import (
    activate,
    execute,
    get_instance,
    latest_run,
    make_spec,
)
from app.task_instances import container_runtime as cr
from app.task_instances.models import (
    INSTANCE_FAILED,
    NODE_COMPLETED,
    NODE_RUNNING,
    NODE_WAITING_HUMAN,
    TaskArtifact,
)

_TEST_WS = {"path": None}


def _make_model_config(db, *, provider="anthropic",
                       api_base="https://api.minimaxi.com/anthropic"):
    from app.model_configs.models import ModelConfig
    from tests.task_instances.conftest import make_revision

    config = ModelConfig(
        id="mc-anthropic-1", name="测试模型", config_type="llm",
        provider=provider, api_base=api_base, models=["MiniMax-M3"],
        api_key_encrypted=None, enabled=True, created_by="u-test")
    db.add(config)
    db.commit()
    return config


def _spec_with_model(spec_yaml: str, model_id: str) -> str:
    return spec_yaml.replace(
        "  analyze:\n    kind: agent",
        f"  analyze:\n    kind: agent\n    model: {model_id}")


class _FakeDocker:
    """docker CLI 假实现：run 时在工作区写入产出，inspect 报 exited。"""

    def __init__(self, *, mode="ok"):
        self.mode = mode
        self.calls: list[list[str]] = []

    def __call__(self, args, *, timeout=60.0):
        self.calls.append(list(args))
        command = args[0] if args else ""
        if command == "run":
            if self.mode == "run-fail":
                return SimpleNamespace(returncode=1, stdout="", stderr="no image")
            ws = args[args.index("-v") + 1].split(":", 1)[0]
            _TEST_WS["path"] = ws
            from pathlib import Path
            base = Path(ws)
            (base / ".ti-output").mkdir(parents=True, exist_ok=True)
            (base / "output").mkdir(parents=True, exist_ok=True)
            (base / "output" / "report.md").write_text("# 报告\n内容", "utf-8")
            (base / ".ti-output" / "output.json").write_text(json.dumps({
                "summary": "容器执行完成",
                "category": "frontend",
                "artifacts": [{"name": "report.md", "path": "output/report.md",
                               "mime_type": "text/markdown"}],
            }, ensure_ascii=False), "utf-8")
            return SimpleNamespace(returncode=0, stdout="cid\n", stderr="")
        if command == "inspect":
            if self.mode == "timeout":
                return SimpleNamespace(returncode=0, stdout="running:",
                                       stderr="")
            return SimpleNamespace(returncode=0, stdout="exited:0", stderr="")
        if command == "stop":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def runtime_env(db, monkeypatch, tmp_path):
    """容器运行时测试环境：SessionLocal/存储/时钟/设置全部换绑。"""
    from tests.conftest import TestSession
    from app.task_instances import service as ti_service

    monkeypatch.setattr(cr, "SessionLocal", TestSession)
    from app.task_instances import artifacts as ti_artifacts

    monkeypatch.setattr(ti_artifacts, "write_artifact_object",
                        lambda key, content, mime: f"s3://fake/{key}")
    monkeypatch.setattr(
        cr, "_settings",
        lambda: SimpleNamespace(uploads_dir=str(tmp_path),
                                task_agent_image="agent:test",
                                task_agent_network="none"))
    return monkeypatch


class TestDockerExecution:
    def test_dispatch_completes_with_artifacts(self, db, runtime_env, monkeypatch):
        model = _make_model_config(db)
        fake = _FakeDocker()
        monkeypatch.setattr(cr, "_docker", fake)
        instance, result = activate(
            db, _spec_with_model(make_spec(), model.id),
            simulate={"outputs": {"analyze": {
                "category": "frontend", "summary": "s"}}})
        outcome = cr.execute_dispatch_docker(result.dispatches[0])
        assert outcome is not None
        # 执行走独立会话（TestSession）：先结束本连接的读事务快照再断言
        db.rollback()
        db.expire_all()
        analyze = latest_run(db, instance.id, "analyze")
        assert analyze.status == NODE_COMPLETED
        assert analyze.output["summary"] == "容器执行完成"
        artifact = db.query(TaskArtifact).filter(
            TaskArtifact.instance_id == instance.id).first()
        assert artifact is not None
        assert artifact.name == "report.md"
        assert artifact.storage_uri.startswith("s3://fake/")
        assert artifact.sha256 and len(artifact.sha256) == 64
        # 任务文件已渲染进工作区
        task_file = _TEST_WS["path"] + "/.ti-task.json"
        task = json.loads(open(task_file, encoding="utf-8").read())
        assert "任务目标" in task["prompt"]
        assert "交付要求" in task["prompt"]
        # 推进：frontend 分支 → 人工等待
        assert get_instance(db, instance.id).status == "active"
        assert latest_run(db, instance.id,
                          "human_review").status == NODE_WAITING_HUMAN

    def test_non_anthropic_model_rejected(self, db, runtime_env, monkeypatch):
        model = _make_model_config(db, provider="openai",
                                   api_base="https://api.example.com/v1")
        fake = _FakeDocker()
        monkeypatch.setattr(cr, "_docker", fake)
        instance, result = activate(
            db, _spec_with_model(make_spec(), model.id),
            simulate={"outputs": {"analyze": {
                "category": "frontend", "summary": "s"}}})
        outcome = cr.execute_dispatch_docker(result.dispatches[0])
        assert outcome is None
        assert get_instance(db, instance.id).status == INSTANCE_FAILED
        assert "Anthropic" in (get_instance(db, instance.id).fail_reason or "")
        assert fake.calls == []  # 未触达 docker

    def test_docker_run_failure_fails_node(self, db, runtime_env, monkeypatch):
        model = _make_model_config(db)
        monkeypatch.setattr(cr, "_docker", _FakeDocker(mode="run-fail"))
        instance, result = activate(
            db, _spec_with_model(make_spec(), model.id),
            simulate={"outputs": {"analyze": {
                "category": "frontend", "summary": "s"}}})
        assert cr.execute_dispatch_docker(result.dispatches[0]) is None
        fresh = get_instance(db, instance.id)
        assert fresh.status == INSTANCE_FAILED
        assert "docker run 失败" in (fresh.fail_reason or "")

    def test_timeout_stops_container_and_fails(self, db, runtime_env, monkeypatch):
        model = _make_model_config(db)
        fake = _FakeDocker(mode="timeout")
        monkeypatch.setattr(cr, "_docker", fake)
        monkeypatch.setattr(cr, "_POLL_INTERVAL_SECONDS", 0.01)
        # 时钟快进：每次 sleep 后 monotonic 前进 120 秒（超 1 分钟超时）
        real_monotonic = cr.time.monotonic

        class _FastClock:
            offset = 0.0

            @classmethod
            def monotonic(cls):
                return real_monotonic() + cls.offset

            @classmethod
            def sleep(cls, _seconds):
                cls.offset += 120.0

        monkeypatch.setattr(cr, "time", _FastClock)
        instance, result = activate(
            db, _spec_with_model(make_spec(), model.id),
            simulate={"outputs": {"analyze": {
                "category": "frontend", "summary": "s"}}})
        assert cr.execute_dispatch_docker(result.dispatches[0]) is None
        fresh = get_instance(db, instance.id)
        assert fresh.status == INSTANCE_FAILED
        assert "超时" in (fresh.fail_reason or "")
        assert any(call[0] == "stop" for call in fake.calls)


class TestSteeringDrop:
    def test_drop_writes_file_and_event(self, db, runtime_env, monkeypatch, tmp_path):
        model = _make_model_config(db)
        monkeypatch.setattr(cr, "_docker", _FakeDocker(mode="timeout"))
        monkeypatch.setattr(cr, "_POLL_INTERVAL_SECONDS", 0.05)
        import threading
        instance, result = activate(
            db, _spec_with_model(make_spec(), model.id),
            simulate={"outputs": {"analyze": {
                "category": "frontend", "summary": "s"}}})
        run_id = result.dispatches[0]
        # 后台启动容器执行（timeout 模式会保持 running），主线程测插话
        worker = threading.Thread(
            target=cr.execute_dispatch_docker, args=(run_id,), daemon=True)
        worker.start()
        from tests.task_instances.conftest import events_of
        deadline_drop = 0
        while latest_run(db, instance.id, "analyze").status != NODE_RUNNING \
                and deadline_drop < 100:
            import time as _t
            _t.sleep(0.02)
            deadline_drop += 1
            db.expire_all()
        cr.drop_steering(run_id, "msg-1", "补充要求：关注兼容性")
        worker.join(timeout=10)
        ws = cr.workspace_of(instance.id, run_id)
        steering_files = list((ws / ".steering").glob("*.json"))
        assert steering_files, "插话文件未落盘"
        payload = json.loads(steering_files[0].read_text(encoding="utf-8"))
        assert payload == {"id": "msg-1", "content": "补充要求：关注兼容性"}
        types = [t for t, _ in events_of(db, instance.id)]
        assert "steering.delivered" in types


class TestRegistryOwnership:
    def test_exclude_env_removes_subject(self, db, monkeypatch):
        from app.data_channel.pipeline_tasks import nats_executor

        monkeypatch.delenv("TASK_INSTANCES_CONTROL_EXCLUDE", raising=False)
        subjects = {s for s, _, _ in nats_executor._handler_registry()}
        assert "task_instances.control" in subjects
        monkeypatch.setenv("TASK_INSTANCES_CONTROL_EXCLUDE", "1")
        subjects = {s for s, _, _ in nats_executor._handler_registry()}
        assert "task_instances.control" not in subjects
