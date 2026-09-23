"""Python 脚本流水线引擎：归一化/执行/保存/试运行/发布凭证/归档/筛选/运行链路。

JKG 边界（client.execute_script）一律 mock，测试不起真实内核。
"""
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.data_channel.pipeline_tasks.models import PipelineTask
from app.data_channel.pipelines import router as pipeline_router
from app.data_channel.pipelines.contracts import (
    PipelineUpdate,
    PublishBody,
    ScriptBody,
    ValidateDefinitionsBody,
)
from app.data_channel.pipelines.models import Pipeline, PipelineRun
from app.data_channel.pipelines.python_engine import client as python_client
from app.data_channel.pipelines.python_engine import service as python_service
from app.data_channel.pipelines.python_engine.client import (
    PythonEngineError,
    ScriptExecution,
)
from app.data_channel.steward.service import canonical_json_hash

SCRIPT = "result = [{'id': 'A-1', 'name': '示例'}]"
ROWS = [{"id": "A-1", "name": "示例"}]

COLUMNS = [{
    "source_key": "id",
    "field_key": "id",
    "field_name": "标识",
    "field_type": "string",
    "is_primary_key": True,
    "nullable": False,
}]


class _Storage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_object(self, uri: str) -> bytes:
        return self.objects[uri]

    def put_bytes(self, bucket, key, data, content_type="") -> str:
        uri = f"s3://{bucket}/{key}"
        self.objects[uri] = data
        return uri

    def delete_object(self, uri: str) -> None:
        self.objects.pop(uri, None)

    def list_prefix(self, bucket: str, prefix: str) -> list[str]:
        prefix_uri = f"s3://{bucket}/{prefix}"
        return [uri for uri in self.objects if uri.startswith(prefix_uri)]


def _python_pipeline(db, *, script=SCRIPT, status="draft", name="脚本取数") -> Pipeline:
    pipeline = Pipeline(
        id=f"py-{uuid.uuid4().hex[:12]}",
        name=name,
        status=status,
        definition={
            "engine": "python",
            "nodes": [],
            "edges": [],
            "python": {"script": script} if script is not None else {},
        },
        spec={},
        column_definitions=[],
        enabled=False,
    )
    db.add(pipeline)
    db.commit()
    return pipeline


def _execution(rows=ROWS, **overrides) -> ScriptExecution:
    payload = {
        "rows": rows,
        "stdout": "",
        "error": None,
        "traceback": "",
        "duration_ms": 12,
        "kernel_id": "kernel-1",
    }
    payload.update(overrides)
    return ScriptExecution(**payload)


def _stage(storage: _Storage, pipeline_id: str, rows: list[dict]) -> str:
    dry_run_id = str(uuid.uuid4())
    outputs = [{"rows": rows}]
    payload = {
        "pipeline_id": pipeline_id,
        "dry_run_id": dry_run_id,
        "created_at": "2026-08-08T00:00:00Z",
        "truncated": False,
        "outputs": outputs,
        "output_checksum": canonical_json_hash(outputs),
    }
    storage.objects[pipeline_router._dry_run_uri(pipeline_id, dry_run_id)] = (
        json.dumps(payload).encode()
    )
    return dry_run_id


# ── client 归一化与输出解析（纯函数） ──────────────────────────────


def test_normalize_rows_matrix():
    assert python_client.normalize_rows(None) == []
    assert python_client.normalize_rows({"a": 1}) == [{"a": 1}]
    assert python_client.normalize_rows("x") == [{"value": "x"}]
    assert python_client.normalize_rows([{"a": 1}, 2, "s"]) == [
        {"a": 1},
        {"value": 2},
        {"value": "s"},
    ]


def test_normalize_rows_rejects_over_cap():
    with pytest.raises(PythonEngineError, match="安全上限"):
        python_client.normalize_rows([{"a": i} for i in range(50_001)])


def test_extract_rows_from_marked_stdout():
    stdout = (
        "some log\n__OB_RESULT_BEGIN__\n"
        + json.dumps(ROWS, ensure_ascii=False)
        + "\n__OB_RESULT_END__\n"
    )
    assert python_client._extract_rows(stdout) == ROWS


def test_visible_stdout_removes_platform_payload_and_preserves_user_logs():
    stdout = (
        "抓取开始\n抓取完成\n\n__OB_RESULT_BEGIN__\n"
        + json.dumps(ROWS, ensure_ascii=False)
        + "\n__OB_RESULT_END__\n"
    )
    assert python_client.visible_stdout(stdout) == "抓取开始\n抓取完成\n"
    assert python_client.visible_stdout("普通日志\n") == "普通日志\n"


def test_extract_rows_rejects_missing_markers_and_bad_json():
    with pytest.raises(PythonEngineError, match="输出标记"):
        python_client._extract_rows("no markers here")
    with pytest.raises(PythonEngineError, match="JSON"):
        python_client._extract_rows(
            "__OB_RESULT_BEGIN__\n{not-json}\n__OB_RESULT_END__")


def test_clean_traceback_strips_ansi():
    cleaned = python_client._clean_traceback(["\x1b[31mBoom\x1b[0m"])
    assert cleaned == "Boom"


def test_params_prelude_is_valid_python_with_json_literals():
    # 回归：json.dumps 结果直接拼代码时，JSON 的 false/true/null 不是合法
    # Python 字面量，任务运行注入 full_refresh 布尔即抛 NameError('false')，
    # 而既有测试全部 mock 掉 execute_script，从未真实执行组装出的代码。
    code = python_client._params_prelude({
        "cursor_column": "updated_at",
        "cursor_since": "",
        "full_refresh": False,
    })
    namespace: dict = {}
    exec(code, namespace)  # 修复前此行抛 NameError: name 'false' is not defined
    assert namespace["OB_RUN_PARAMS"] == {
        "cursor_column": "updated_at",
        "cursor_since": "",
        "full_refresh": False,
    }


def test_execute_script_requires_gateway_config():
    # 测试环境默认未配置 PYTHON_KERNEL_GATEWAY_URL
    assert not python_client.settings.python_kernel_gateway_url
    with pytest.raises(PythonEngineError, match="未配置"):
        python_client.execute_script(SCRIPT)


# ── 执行端点 ─────────────────────────────────────────────────────


def test_execute_rejects_non_python_pipeline(db):
    pipeline = Pipeline(name="画布流水线", definition={"nodes": [], "edges": []})
    db.add(pipeline)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.execute_pipeline_script(
            pipeline.id, ScriptBody(script=SCRIPT), db)
    assert exc_info.value.status_code == 400


def test_execute_rejects_blank_script(db):
    pipeline = _python_pipeline(db)
    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.execute_pipeline_script(
            pipeline.id, ScriptBody(script="  "), db)
    assert exc_info.value.status_code == 400


def test_execute_maps_gateway_failure_to_502(db, monkeypatch):
    pipeline = _python_pipeline(db)

    def _boom(script, *, timeout, cancel_event=None):
        raise PythonEngineError("Python 执行网关未配置（PYTHON_KERNEL_GATEWAY_URL）。")

    monkeypatch.setattr(python_service, "execute_script", _boom)
    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.execute_pipeline_script(
            pipeline.id, ScriptBody(script=SCRIPT), db)
    assert exc_info.value.status_code == 502


def test_execute_returns_script_error_payload(db, monkeypatch):
    pipeline = _python_pipeline(db)
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(
            rows=[],
            error="脚本执行失败（NameError）：name 'result' is not defined",
            traceback="Traceback ...",
        ),
    )
    result = pipeline_router.execute_pipeline_script(
        pipeline.id, ScriptBody(script=SCRIPT), db)
    assert result["ok"] is False
    assert result["format_valid"] is False
    assert "NameError" in result["error"]
    assert result["traceback"].startswith("Traceback")
    assert result["row_count"] == 0


def test_execute_success_reports_rows_columns_and_format(db, monkeypatch):
    pipeline = _python_pipeline(db)
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(),
    )
    result = pipeline_router.execute_pipeline_script(
        pipeline.id, ScriptBody(script=SCRIPT), db)
    assert result["ok"] is True
    assert result["format_valid"] is True
    assert result["format_error"] is None
    assert result["row_count"] == 1
    assert result["columns"] == ["id", "name"]
    assert result["sample"] == ROWS
    # 平台执行时限随结果下发，供页面展示「已执行 Xs / 上限 Ys」
    assert result["timeout_seconds"] > 0


def test_execute_conflict_returns_409_until_prior_run_finishes(db, monkeypatch):
    import threading

    pipeline = _python_pipeline(db)
    release = threading.Event()
    entered = threading.Event()

    def _blocking(script, *, timeout, cancel_event=None):
        entered.set()
        release.wait(5)
        return _execution()

    monkeypatch.setattr(python_service, "execute_script", _blocking)
    outcome: dict = {}

    def _first_call():
        try:
            outcome["result"] = pipeline_router.execute_pipeline_script(
                pipeline.id, ScriptBody(script=SCRIPT), db)
        except HTTPException as exc:  # noqa: BLE001
            outcome["error"] = exc

    worker = threading.Thread(target=_first_call)
    worker.start()
    assert entered.wait(5)
    try:
        with pytest.raises(HTTPException) as exc_info:
            pipeline_router.execute_pipeline_script(
                pipeline.id, ScriptBody(script=SCRIPT), db)
        assert exc_info.value.status_code == 409
    finally:
        release.set()
        worker.join(5)
    assert outcome["result"]["ok"] is True


def test_cancel_terminates_in_flight_execution(db, monkeypatch):
    import threading

    pipeline = _python_pipeline(db)
    started = threading.Event()

    def _waiting(script, *, timeout, cancel_event=None):
        started.set()
        while not cancel_event.wait(0.05):
            pass
        raise PythonEngineError("本次执行已被用户取消。")

    monkeypatch.setattr(python_service, "execute_script", _waiting)
    outcome: dict = {}

    def _execute():
        try:
            outcome["result"] = pipeline_router.execute_pipeline_script(
                pipeline.id, ScriptBody(script=SCRIPT), db)
        except HTTPException as exc:  # noqa: BLE001
            outcome["error"] = exc

    worker = threading.Thread(target=_execute)
    worker.start()
    assert started.wait(5)
    assert pipeline_router.cancel_pipeline_script(pipeline.id, db) == {"cancelled": True}
    worker.join(5)
    assert not worker.is_alive()
    # 取消经 502 落到执行请求上（前端此时已中止等待，只看到取消提示）
    assert outcome["error"].status_code == 502
    assert "取消" in str(outcome["error"].detail)
    # 没有进行中执行时取消是幂等空操作
    assert pipeline_router.cancel_pipeline_script(pipeline.id, db) == {"cancelled": False}


def test_execute_flags_lake_gate_format_violation(db, monkeypatch):
    pipeline = _python_pipeline(db)
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(rows=[{"blob": b"\x00\x01"}]),
    )
    result = pipeline_router.execute_pipeline_script(
        pipeline.id, ScriptBody(script=SCRIPT), db)
    assert result["ok"] is True
    assert result["format_valid"] is False
    assert "二进制" in result["format_error"]


# ── 保存端点（双重保障的服务端一侧） ────────────────────────────────


def test_save_rejects_non_python_and_blank(db):
    canvas = Pipeline(name="画布流水线", definition={"nodes": [], "edges": []})
    db.add(canvas)
    db.commit()
    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.save_pipeline_script(
            canvas.id, ScriptBody(script=SCRIPT), db)
    assert exc_info.value.status_code == 400

    pipeline = _python_pipeline(db)
    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.save_pipeline_script(
            pipeline.id, ScriptBody(script=""), db)
    assert exc_info.value.status_code == 400


def test_save_rejects_published_pipeline(db, monkeypatch):
    pipeline = _python_pipeline(db, status="published")
    called = []
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: called.append(script) or _execution(),
    )
    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.save_pipeline_script(
            pipeline.id, ScriptBody(script="result = []"), db)
    assert exc_info.value.status_code == 409
    assert called == []  # 已发布直接拒绝，不触发执行


def test_save_reexecutes_and_rejects_failed_or_invalid_output(db, monkeypatch):
    pipeline = _python_pipeline(db)
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(rows=[], error="脚本执行失败（ValueError）：boom"),
    )
    with pytest.raises(HTTPException, match="保存前校验执行失败"):
        pipeline_router.save_pipeline_script(
            pipeline.id, ScriptBody(script=SCRIPT), db)

    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(rows=[{"blob": b"\x00"}]),
    )
    with pytest.raises(HTTPException, match="保存前格式校验未通过"):
        pipeline_router.save_pipeline_script(
            pipeline.id, ScriptBody(script=SCRIPT), db)
    assert (pipeline.definition.get("python") or {}).get("script") == SCRIPT


def test_save_persists_script_columns_and_clears_attestation(db, monkeypatch):
    pipeline = _python_pipeline(db)
    pipeline.validation_attestation = {"version": 1, "dry_run_id": "old"}
    db.commit()

    calls = []

    def _fake(script, *, timeout, cancel_event=None):
        calls.append(script)
        return _execution()

    monkeypatch.setattr(python_service, "execute_script", _fake)
    new_script = "result = [{'id': 'B-1', 'name': '新'}]"
    result = pipeline_router.save_pipeline_script(
        pipeline.id, ScriptBody(script=new_script), db)

    assert calls == [new_script]  # 保存确实重跑了当前提交的脚本
    saved = result["pipeline"]["definition"]["python"]
    assert saved["script"] == new_script
    assert saved["output_columns"] == ["id", "name"]
    assert saved["saved_at"]
    assert result["execution"]["ok"] is True
    refreshed = db.get(Pipeline, pipeline.id)
    assert refreshed.validation_attestation is None


# ── dry-run 分支 ─────────────────────────────────────────────────


def test_dry_run_python_stages_single_output(db, monkeypatch):
    pipeline = _python_pipeline(db)
    storage = _Storage()
    monkeypatch.setattr(
        "app.services.storage_service.get_storage_service", lambda: storage)
    monkeypatch.setattr(
        python_client,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(),
    )

    result = pipeline_router.dry_run_pipeline(pipeline.id, db, 100)

    assert result["engine"] == "python"
    assert result["rows_out"] == 1
    assert len(result["outputs"]) == 1
    output = result["outputs"][0]
    assert output["columns"] == ["id", "name"]
    assert output["gate_error"] is None
    staged = json.loads(
        next(iter(storage.objects.values())).decode("utf-8"))
    assert staged["pipeline_id"] == pipeline.id
    assert staged["outputs"][0]["rows"] == ROWS
    assert staged["outputs"][0]["source"]["kind"] == "python"


def test_dry_run_python_without_script_fails(db):
    pipeline = _python_pipeline(db, script=None)
    with pytest.raises(HTTPException, match="试运行失败"):
        pipeline_router.dry_run_pipeline(pipeline.id, db, 100)


def test_dry_run_python_execution_failure_fails(db, monkeypatch):
    pipeline = _python_pipeline(db)
    monkeypatch.setattr(
        python_client,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(rows=[], error="脚本执行失败（RuntimeError）：x"),
    )
    with pytest.raises(HTTPException, match="试运行失败"):
        pipeline_router.dry_run_pipeline(pipeline.id, db, 100)


# ── 结构校验与发布凭证链路 ─────────────────────────────────────────


def test_validate_pipeline_definition_python_branch(db):
    missing = _python_pipeline(db, script=None, name="无脚本")
    result = pipeline_router.validate_pipeline(missing.id, db)
    assert result.valid is False
    assert "尚未保存脚本" in result.errors[0]["message"]

    ready = _python_pipeline(db, name="有脚本")
    result = pipeline_router.validate_pipeline(ready.id, db)
    assert result.valid is True


def test_publish_requires_attestation_for_python(db):
    pipeline = _python_pipeline(db)
    pipeline.column_definitions = COLUMNS
    db.commit()
    with pytest.raises(HTTPException, match="执行预览与字段定义"):
        pipeline_router.publish_pipeline(
            pipeline.id, PublishBody(enable=False), db, SimpleNamespace(id="admin"))


def test_python_full_release_flow_with_attestation(db, monkeypatch):
    pipeline = _python_pipeline(db)
    storage = _Storage()
    dry_run_id = _stage(storage, pipeline.id, ROWS)
    monkeypatch.setattr(
        "app.services.storage_service.get_storage_service", lambda: storage)

    result = validate_column_definitions_for(pipeline, dry_run_id, db)
    assert result.valid is True
    assert pipeline.validation_attestation["engine"] == "python"
    assert pipeline.validation_attestation["dry_run_id"] == dry_run_id

    # 向导第 3 步把同一份字段契约经 update 落库（hash 一致，凭证保留）
    pipeline_router.update_pipeline(
        pipeline.id,
        PipelineUpdate(column_definitions=COLUMNS),
        db,
    )
    assert pipeline.validation_attestation is not None

    publish = pipeline_router.publish_pipeline(
        pipeline.id, PublishBody(enable=True), db, SimpleNamespace(id="admin"))
    assert publish["status"] == "published"
    assert publish["enabled"] is True


def validate_column_definitions_for(pipeline, dry_run_id, db):
    return pipeline_router.validate_column_definitions(
        pipeline.id,
        ValidateDefinitionsBody(column_definitions=COLUMNS),
        dry_run_id,
        db,
    )


# ── 通用 update 拦截 definition ────────────────────────────────────


def test_update_pipeline_blocks_definition_for_python(db):
    pipeline = _python_pipeline(db)
    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.update_pipeline(
            pipeline.id,
            PipelineUpdate(definition={"engine": "python", "python": {"script": "x"}}),
            db,
        )
    assert exc_info.value.status_code == 400
    assert "脚本编辑页" in exc_info.value.detail

    result = pipeline_router.update_pipeline(
        pipeline.id,
        PipelineUpdate(name="改名后的脚本流水线", description="新描述"),
        db,
    )
    assert result["name"] == "改名后的脚本流水线"


# ── 归档（删除按钮）与列表筛选 ──────────────────────────────────────


def test_delete_archives_python_pipeline_and_keeps_runs(db):
    pipeline = _python_pipeline(db)
    run = PipelineRun(pipeline_id=pipeline.id, status="success")
    db.add(run)
    db.commit()

    result = pipeline_router.delete_pipeline(pipeline.id, db)

    assert result["status"] == "archived"
    archived = db.get(Pipeline, pipeline.id)
    assert archived.status == "archived"
    assert archived.enabled is False
    assert db.get(PipelineRun, run.id) is not None  # 审计链保留

    listed = pipeline_router.list_pipelines(
        search="", domain="", status="", engine="", enabled=None,
        page=1, page_size=20, paginated=True, db=db)
    assert all(item["id"] != pipeline.id for item in listed["items"])


def test_delete_python_pipeline_rejected_when_task_references(db):
    pipeline = _python_pipeline(db)
    db.add(PipelineTask(
        name="每日入湖",
        pipeline_id=pipeline.id,
        write_mode="overwrite",
        schedule_type="MANUAL",
        enabled=False,
        status="idle",
    ))
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.delete_pipeline(pipeline.id, db)
    assert exc_info.value.status_code == 400
    assert "调度任务引用" in exc_info.value.detail
    assert db.get(Pipeline, pipeline.id).status == "draft"


def test_list_filter_separates_python_from_n8n(db):
    _python_pipeline(db, name="脚本A")
    db.add(Pipeline(
        name="n8nA",
        definition={"engine": "n8n", "nodes": [], "edges": []},
    ))
    db.commit()

    def _ids(engine):
        page = pipeline_router.list_pipelines(
            search="", domain="", status="", engine=engine, enabled=None,
            page=1, page_size=20, paginated=True, db=db)
        return {item["name"] for item in page["items"]}

    assert _ids("python") == {"脚本A"}
    assert _ids("n8n") == {"n8nA"}
    # canvas 已下线：engine=canvas 不再是受支持的过滤值，按不过滤处理
    assert _ids("canvas") == {"脚本A", "n8nA"}


# ── 脚本保存历史 ─────────────────────────────────────────────────


def test_save_records_history_and_lists_newest_first(db, monkeypatch):
    pipeline = _python_pipeline(db)
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(),
    )
    pipeline_router.save_pipeline_script(
        pipeline.id, ScriptBody(script="result = [{'id': 'A-1'}]"), db)
    pipeline_router.save_pipeline_script(
        pipeline.id, ScriptBody(script="result = [{'id': 'A-2'}]"), db)

    result = pipeline_router.list_script_versions(pipeline.id, db)

    assert [item["version_no"] for item in result["items"]] == [2, 1]
    latest = result["items"][0]
    assert latest["script"] == "result = [{'id': 'A-2'}]"
    assert latest["output_columns"] == ["id", "name"]
    assert latest["row_count"] == 1
    assert latest["created_at"]


def test_failed_or_rejected_save_records_no_history(db, monkeypatch):
    pipeline = _python_pipeline(db)
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(rows=[], error="脚本执行失败（ValueError）：x"),
    )
    with pytest.raises(HTTPException):
        pipeline_router.save_pipeline_script(
            pipeline.id, ScriptBody(script=SCRIPT), db)

    published = _python_pipeline(db, status="published", name="已发布")
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(),
    )
    with pytest.raises(HTTPException):
        pipeline_router.save_pipeline_script(
            published.id, ScriptBody(script=SCRIPT), db)

    assert pipeline_router.list_script_versions(pipeline.id, db)["items"] == []
    assert pipeline_router.list_script_versions(published.id, db)["items"] == []


def test_script_history_pruned_beyond_keep_limit(db, monkeypatch):
    pipeline = _python_pipeline(db)
    monkeypatch.setattr(python_service, "_SCRIPT_VERSION_KEEP", 2)
    monkeypatch.setattr(
        python_service,
        "execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(),
    )
    for index in range(3):
        pipeline_router.save_pipeline_script(
            pipeline.id, ScriptBody(script=f"result = [{{'n': {index}}}]"), db)

    result = pipeline_router.list_script_versions(pipeline.id, db)

    assert [item["version_no"] for item in result["items"]] == [3, 2]


def test_list_script_versions_rejects_non_python(db):
    canvas = Pipeline(name="画布流水线", definition={"nodes": [], "edges": []})
    db.add(canvas)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        pipeline_router.list_script_versions(canvas.id, db)
    assert exc_info.value.status_code == 400


# ── run-sync 全链路（证明下游零改动可用） ───────────────────────────


def test_run_sync_dispatches_python_engine_into_lake(db, monkeypatch):
    pipeline = _python_pipeline(db)
    runtime_sessions = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.database.SessionLocal", runtime_sessions)
    monkeypatch.setattr(
        "app.data_channel.pipelines.python_engine.runner.execute_script",
        lambda script, *, timeout, cancel_event=None, params=None: _execution(),
    )

    result = pipeline_router.run_pipeline_sync(pipeline.id, db)

    assert result["status"] == "success", result.get("error")
    stats = result["stats"]
    assert stats["engine"] == "python"
    assert stats["rows_in"] == 1
    assert stats["lake_rows"] == 1
    assert stats["curated_dataset_ids"]

    from app.models.v2.dataset import Dataset, DatasetVersion

    curated = db.get(Dataset, stats["curated_dataset_ids"][0])
    assert curated is not None
    version = (
        db.query(DatasetVersion)
        .filter(DatasetVersion.dataset_id == curated.id)
        .one()
    )
    assert version.rowcount == 1


def test_run_sync_missing_script_marks_run_failed(db, monkeypatch):
    pipeline = _python_pipeline(db, script=None)
    runtime_sessions = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.database.SessionLocal", runtime_sessions)

    result = pipeline_router.run_pipeline_sync(pipeline.id, db)

    assert result["status"] == "failed"
    assert "尚未保存脚本" in result["error"]


# ── 引擎注册表 ────────────────────────────────────────────────────


def test_engine_registry_resolves_python_and_rejects_unknown():
    from app.data_channel.pipelines.engine_registry import (
        get_engine_runner,
        known_engines,
    )

    assert callable(get_engine_runner("python"))
    assert get_engine_runner("no-such-engine") is None
    assert "python" in known_engines()


def test_execute_script_extracts_rows_beyond_stdout_tail(monkeypatch):
    """结果块超过 stdout 尾部保留上限（200K）时仍能提取行数据。

    回归：曾改为在截断后的 stdout 上提取，BEGIN 标记被截掉导致大结果集
    从「成功」变「未检测到平台输出标记」。修复后提取在完整 stdout 上进行，
    回传的 stdout 仍截断到尾部上限。
    """
    big_rows = [{"id": i, "text": "x" * 60} for i in range(4000)]
    payload = json.dumps(big_rows, ensure_ascii=False)
    assert len(payload) > 200_000  # 确保结果块本身超过尾部保留上限
    full_stdout = (
        "prep log\n"
        "__OB_RESULT_BEGIN__\n" + payload + "\n__OB_RESULT_END__\n"
    )

    class _KernelResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "kernel-big"}

    monkeypatch.setattr(
        python_client.settings, "python_kernel_gateway_url", "http://jkg.test")
    monkeypatch.setattr(
        python_client.httpx, "post", lambda *a, **k: _KernelResponse())
    monkeypatch.setattr(
        python_client, "_run_on_kernel",
        lambda *a, **k: (full_stdout, None, None, ""))
    monkeypatch.setattr(
        python_client, "_delete_kernel", lambda *a, **k: None)

    execution = python_client.execute_script("result = []")

    assert execution.error is None
    assert len(execution.rows) == 4000
    assert execution.rows[0] == {"id": 0, "text": "x" * 60}
    assert execution.stdout == "prep log\n"
    assert "__OB_RESULT_" not in execution.stdout
