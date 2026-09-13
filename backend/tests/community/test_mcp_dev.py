"""自研 MCP（开发 MCP）：服务层、执行器与守卫的行为测试。

JKG 边界一律 mock（与 world_model / python engine 测试同一纪律）：fake
execute_code 在本进程内 exec 组装好的完整代码（prelude + 用户脚本 +
epilogue），stdout 经真实标记协议回收——代码组装、注册器、schema 推导、
样例捕获、发布闸门、密钥打码都走真实路径，不起真内核。
"""
from __future__ import annotations

import asyncio
import contextlib
import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.crypto import encrypt_value
from app.auth.models import User, UserEnvVar, UserPrivacyVar
from app.shared.database import Base
from app.super_assistant import mcp_dev_executor, mcp_dev_service, mcp_server_service
from app.super_assistant.mcp_server_service import (
    McpServerServiceError,
    create_mcp_server,
    remove_mcp_server,
    update_mcp_server,
)
from app.super_assistant.models import (
    SuperAssistantMcpDevProject,
    SuperAssistantMcpDevVersion,
    SuperAssistantMcpServer,
)
from app.super_assistant.schemas import (
    McpDevExecuteIn,
    McpDevProjectCreate,
    McpDevPublishIn,
    McpDevSaveIn,
    McpServerCreate,
    McpServerUpdate,
)

_RESULT_BEGIN = "__OB_RESULT_BEGIN__"
_RESULT_END = "__OB_RESULT_END__"


def _fake_execute_code(code, *, timeout=None, cancel_event=None, full_stdout=False):
    from app.data_channel.pipelines.python_engine.client import ScriptExecution

    buffer = io.StringIO()
    namespace: dict = {}
    error = None
    traceback_text = ""
    try:
        with contextlib.redirect_stdout(buffer):
            exec(compile(code, "<mcp-dev-test>", "exec"), namespace)  # noqa: S102
    except Exception as exc:  # noqa: BLE001 — 对齐内核的 error/traceback 语义
        import traceback as traceback_module

        error = f"脚本执行失败（{type(exc).__name__}）：{exc}"
        traceback_text = traceback_module.format_exc()
    return ScriptExecution(
        rows=[],
        stdout=buffer.getvalue(),
        error=error,
        traceback=traceback_text,
        duration_ms=3,
        kernel_id="fake-kernel",
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_dev_executor, "execute_code", _fake_execute_code)
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp_dev.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            UserEnvVar.__table__,
            UserPrivacyVar.__table__,
            SuperAssistantMcpServer.__table__,
            SuperAssistantMcpDevProject.__table__,
            SuperAssistantMcpDevVersion.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add(
            User(
                id="owner-1",
                username="owner-one",
                email="owner-one@example.com",
                password_hash="unused",
                role="editor",
            )
        )
        session.add(
            User(
                id="owner-2",
                username="owner-two",
                email="owner-two@example.com",
                password_hash="unused",
                role="editor",
            )
        )
        session.commit()
        yield session
    engine.dispose()


_SCRIPT = '''
@mcp_tool(description="把两个数字相加")
def add_numbers(a: int, b: int = 1) -> dict:
    return {"sum": a + b}


@mcp_tool(description="回显环境变量值")
def read_env(name: str) -> dict:
    return {"value": OB_ENV.get(name, "<missing>")}


@mcp_tool(description="故意失败")
def always_fail() -> dict:
    raise RuntimeError("boom")
'''


def _project(db) -> SuperAssistantMcpDevProject:
    return mcp_dev_service.create_project(
        db,
        "owner-1",
        McpDevProjectCreate(name="my-tools", display_name="我的工具集", description="演示"),
    )


def _save(db, project, script: str = _SCRIPT) -> int:
    result = mcp_dev_service.save_project(
        db, "owner-1", project.id, McpDevSaveIn(script=script),
    )
    assert result.ok, result.error
    return result.version_no


def _run_tool(db, project, tool: str, arguments: dict, script: str = _SCRIPT):
    return mcp_dev_service.execute_project(
        db, "owner-1", project.id,
        McpDevExecuteIn(script=script, tool_name=tool, arguments=arguments),
    )


# ──────────────────────────── 执行与内省 ────────────────────────────


def test_execute_introspect_lists_tools_with_derived_schema(db):
    project = _project(db)
    out = mcp_dev_service.execute_project(
        db, "owner-1", project.id, McpDevExecuteIn(script=_SCRIPT),
    )
    assert out.ok
    names = {t["name"] for t in out.tools}
    assert names == {"add_numbers", "read_env", "always_fail"}
    add = next(t for t in out.tools if t["name"] == "add_numbers")
    assert add["description"] == "把两个数字相加"
    schema = add["input_schema"]
    assert schema["properties"]["a"] == {"type": "integer"}
    assert schema["properties"]["b"] == {"type": "integer", "default": 1}
    assert schema["required"] == ["a"]


def test_execute_call_tool_success_captures_sample(db):
    project = _project(db)
    out = _run_tool(db, project, "add_numbers", {"a": 2, "b": 3})
    assert out.ok
    assert out.payload["ok"] is True
    assert out.payload["payload"] == {"sum": 5}
    fresh = db.get(SuperAssistantMcpDevProject, project.id)
    assert fresh.tool_samples["add_numbers"] == {"a": 2, "b": 3}


def test_execute_call_tool_error_is_captured_not_raised(db):
    project = _project(db)
    out = _run_tool(db, project, "always_fail", {})
    assert out.ok  # 脚本与 epilogue 本身执行成功
    assert out.payload["ok"] is False
    assert "RuntimeError" in out.payload["error"]
    assert "boom" in out.payload["traceback"]
    # 失败的调用不捕获为样例参数
    fresh = db.get(SuperAssistantMcpDevProject, project.id)
    assert "always_fail" not in (fresh.tool_samples or {})


def test_execute_unregistered_tool_reports_error(db):
    project = _project(db)
    out = _run_tool(db, project, "nope", {})
    assert out.ok
    assert out.payload["ok"] is False
    assert "未注册" in out.payload["error"]


def test_personal_env_vars_injected_into_kernel_namespace(db):
    db.add(UserEnvVar(user_id="owner-1", key="MY_KEY", value_encrypted=encrypt_value("s3cret-value")))
    db.commit()
    project = _project(db)
    out = _run_tool(db, project, "read_env", {"name": "MY_KEY"})
    assert out.payload["ok"] is True
    assert out.payload["payload"] == {"value": "s3cret-value"}


def test_mask_secret_values_replaces_known_plaintext():
    text = "token=s3cret-value and short=1"
    masked = mcp_dev_executor.mask_secret_values(text, ["s3cret-value", "1"])
    assert "s3cret-value" not in masked
    assert "short=1" in masked  # 过短（<6）的值不参与打码


# ──────────────────────────── 保存与版本 ────────────────────────────


def test_save_rejects_script_without_tools(db):
    project = _project(db)
    result = mcp_dev_service.save_project(
        db, "owner-1", project.id, McpDevSaveIn(script="x = 1\n"),
    )
    assert not result.ok
    assert "未解析到任何工具" in result.error


def test_save_freezes_version_and_prunes_to_keep_20(db):
    project = _project(db)
    for _ in range(23):
        _save(db, project)
    versions = mcp_dev_service.list_versions(db, "owner-1", project.id)
    assert len(versions) == mcp_dev_service.SCRIPT_VERSION_KEEP
    assert versions[0].version_no == 23


def test_save_prune_never_removes_published_binding(db):
    script = _passify(_SCRIPT)
    project = _project(db)
    _run_tool(db, project, "add_numbers", {"a": 1, "b": 1}, script=script)
    _run_tool(db, project, "read_env", {"name": "X"}, script=script)
    _run_tool(db, project, "always_fail", {}, script=script)
    first = _save(db, project, script)
    mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    for _ in range(25):
        _save(db, project, script)
    detail = mcp_dev_service.get_version(db, "owner-1", project.id, first)
    assert detail.version_no == first
    fresh = db.get(SuperAssistantMcpDevProject, project.id)
    assert fresh.published_version_id is not None


# ──────────────────────────── 发布闸门 ────────────────────────────


def _passify(script: str) -> str:
    """把 always_fail 替换为成功返回：发布闸门要求每个工具都有成功样例。"""
    return script.replace('raise RuntimeError("boom")', 'return {"recovered": True}')


def _prepare_publishable(db, script: str | None = None) -> SuperAssistantMcpDevProject:
    if script is None:
        script = _SCRIPT
    script = _passify(script)
    project = _project(db)
    _run_tool(db, project, "add_numbers", {"a": 1, "b": 2}, script=script)
    _run_tool(db, project, "read_env", {"name": "X"}, script=script)
    _run_tool(db, project, "always_fail", {}, script=script)
    _save(db, project, script=script)
    return db.get(SuperAssistantMcpDevProject, project.id)


def test_publish_rejects_missing_description(db):
    script = _SCRIPT.replace('description="把两个数字相加"', "description=''")
    project = _prepare_publishable(db, script)
    with pytest.raises(mcp_dev_service.McpDevValidationError, match="缺少描述"):
        mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())


def test_publish_rejects_missing_sample(db):
    project = _project(db)
    _run_tool(db, project, "add_numbers", {"a": 1, "b": 2})
    _run_tool(db, project, "read_env", {"name": "X"})
    _save(db, project)  # always_fail 从未成功试跑 → 无样例
    with pytest.raises(mcp_dev_service.McpDevValidationError, match="样例参数"):
        mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())


def test_publish_rejects_when_sample_run_fails(db):
    # 样例齐备（全部试跑成功），但保存后改脚本让 always_fail 在闸门里失败
    project = _prepare_publishable(db)
    _save(db, project, _SCRIPT)  # 回到会失败的原脚本并冻结
    with pytest.raises(mcp_dev_service.McpDevValidationError, match="发布校验未通过"):
        mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())


def test_publish_creates_developed_server_row(db):
    project = _prepare_publishable(db)
    out = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    server = db.get(SuperAssistantMcpServer, out.server_id)
    assert server.transport == "developed"
    assert server.url == f"developed://{project.id}"
    assert server.dev_project_id == project.id
    assert server.enabled is False
    assert server.require_confirmation is True
    assert {t["name"] for t in server.tool_manifest} == {
        "add_numbers", "read_env", "always_fail"}
    assert server.last_test_status == "success"
    fresh = db.get(SuperAssistantMcpDevProject, project.id)
    assert fresh.status == "published"
    assert all(g["ok"] for g in out.gates)


def test_publish_conflicts_with_imported_server_name(db):
    # 直接落库避免 create_mcp_server 的 DNS 校验（测试环境离线）
    db.add(SuperAssistantMcpServer(
        owner_id="owner-1", name="my-tools", transport="streamable_http",
        url="https://example.com/mcp",
    ))
    db.commit()
    project = _prepare_publishable(db)
    with pytest.raises(mcp_dev_service.McpDevConflictError, match="已存在名"):
        mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())


def test_republish_rebinds_to_new_frozen_version(db):
    project = _prepare_publishable(db)
    first = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    script = _passify(_SCRIPT).replace(
        'return {"sum": a + b}', 'return {"sum": a + b + 100}')
    _run_tool(db, project, "add_numbers", {"a": 1, "b": 1}, script=script)
    _run_tool(db, project, "read_env", {"name": "X"}, script=script)
    _run_tool(db, project, "always_fail", {}, script=script)
    second_no = _save(db, project, script)
    second = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    assert second.server_id == first.server_id
    assert second.version_no == second_no > first.version_no


def test_publish_without_versions_rejected(db):
    project = _project(db)
    with pytest.raises(mcp_dev_service.McpDevValidationError, match="尚未保存任何版本"):
        mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())


# ──────────────────────────── 通用 MCP 入口的守卫 ────────────────────────────


def _developed_server(db) -> SuperAssistantMcpServer:
    project = _prepare_publishable(db)
    out = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    return db.get(SuperAssistantMcpServer, out.server_id)


def test_update_developed_server_restricted_to_toggles(db):
    server = _developed_server(db)
    with pytest.raises(McpServerServiceError, match="开发页管理"):
        update_mcp_server(
            db, "owner-1", server.id,
            McpServerUpdate(url="https://evil.example.com/mcp"),
            include_builtins=False,
        )
    updated = update_mcp_server(
        db, "owner-1", server.id, McpServerUpdate(enabled=True),
        include_builtins=False,
    )
    assert updated.enabled is True


def test_remove_server_cascades_project(db):
    server = _developed_server(db)
    project_id = server.dev_project_id
    remove_mcp_server(db, "owner-1", server.id, include_builtins=False)
    assert db.get(SuperAssistantMcpServer, server.id) is None
    assert db.get(SuperAssistantMcpDevProject, project_id) is None
    assert (
        db.query(SuperAssistantMcpDevVersion)
        .filter(SuperAssistantMcpDevVersion.project_id == project_id)
        .count()
        == 0
    )


def test_remove_project_cascades_server(db):
    project = _prepare_publishable(db)
    out = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    mcp_dev_service.remove_project(db, "owner-1", project.id)
    assert db.get(SuperAssistantMcpServer, out.server_id) is None


def test_test_endpoint_reruns_gates(db):
    server = _developed_server(db)
    out = asyncio.run(
        mcp_server_service.test_mcp_server(
            db, "owner-1", server.id, include_builtins=False),
    )
    assert out.ok
    assert "3 个工具" in out.message


# ──────────────────────────── agent 运行时入口 ────────────────────────────


def test_call_published_tool_returns_masked_json(db):
    db.add(UserEnvVar(user_id="owner-1", key="MY_KEY", value_encrypted=encrypt_value("s3cret-value")))
    db.commit()
    server = _developed_server(db)
    output = mcp_dev_service.call_published_tool(db, server, "read_env", {"name": "MY_KEY"})
    assert "s3cret-value" not in output
    assert '"***"' in output
    import json as json_module

    assert json_module.loads(output)["payload"] == {"value": "***"}


def test_call_published_tool_unknown_tool_returns_error_json(db):
    server = _developed_server(db)
    output = mcp_dev_service.call_published_tool(db, server, "nope", {})
    assert "未注册" in output


def test_call_published_tool_missing_binding_never_raises(db):
    project = _prepare_publishable(db)
    out = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    server = db.get(SuperAssistantMcpServer, out.server_id)
    fresh = db.get(SuperAssistantMcpDevProject, project.id)
    fresh.published_version_id = None  # 模拟绑定丢失
    db.commit()
    output = mcp_dev_service.call_published_tool(db, server, "add_numbers", {"a": 1})
    assert "重新发布" in output


def test_call_published_tool_truncates_huge_output(db):
    script = _SCRIPT.replace(
        'return {"sum": a + b}',
        'return {"blob": "x" * 200000}',
    )
    project = _prepare_publishable(db, script)
    out = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    server = db.get(SuperAssistantMcpServer, out.server_id)
    output = mcp_dev_service.call_published_tool(db, server, "add_numbers", {"a": 1})
    assert len(output) <= mcp_dev_executor._RUNTIME_OUTPUT_CHARS + 100
    assert "结果已截断" in output


# ──────────────────────────── 隔离与元数据 ────────────────────────────


def test_cross_user_isolation(db):
    project = _project(db)
    with pytest.raises(mcp_dev_service.McpDevNotFoundError):
        mcp_dev_service.get_project(db, "owner-2", project.id)
    with pytest.raises(mcp_dev_service.McpDevNotFoundError):
        mcp_dev_service.save_project(
            db, "owner-2", project.id, McpDevSaveIn(script=_SCRIPT))


def test_duplicate_project_name_conflicts(db):
    _project(db)
    with pytest.raises(mcp_dev_service.McpDevConflictError):
        mcp_dev_service.create_project(
            db, "owner-1", McpDevProjectCreate(name="my-tools"),
        )


# ──────────────────────────── 对抗式审查回归 ────────────────────────────


def test_publish_out_serializes_dev_project_id(db):
    """McpServerOut 必须回传 dev_project_id：前端编辑按钮据此进入开发页。"""
    from app.super_assistant.schemas import McpServerOut

    project = _prepare_publishable(db)
    out = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())
    server = db.get(SuperAssistantMcpServer, out.server_id)
    payload = McpServerOut.model_validate(server)
    assert payload.dev_project_id == project.id
    assert payload.transport == "developed"


def test_export_interfaces_rejects_developed_server(db):
    """转接口 API 必须拒绝自研行：放行只会生成运行时必然失败的桥接接口。"""
    from app.community import mcp_export

    server = _developed_server(db)
    with pytest.raises(mcp_server_service.McpServerServiceError, match="自研 MCP 暂不支持转接口"):
        mcp_export.export_server_tools(db, "owner-1", server.id, ["add_numbers"])


def test_publish_rejects_version_with_empty_manifest(db):
    """空清单版本的独立防线（不依赖 save 兜底）。"""
    project = _prepare_publishable(db)
    version = (
        db.query(SuperAssistantMcpDevVersion)
        .filter(SuperAssistantMcpDevVersion.project_id == project.id)
        .order_by(SuperAssistantMcpDevVersion.version_no.desc())
        .first()
    )
    version.tool_manifest = []
    db.commit()
    with pytest.raises(mcp_dev_service.McpDevValidationError, match="未解析出任何工具"):
        mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn())


def test_publish_falls_back_to_live_project_samples(db):
    """样例死角：版本冻结早于成功试跑时，闸门回退项目当前样例并回写快照。"""
    script = _passify(_SCRIPT)
    project = _project(db)
    _save(db, project, script)  # v1 冻结时项目还没有任何样例
    for tool, args in (("add_numbers", {"a": 1, "b": 2}), ("read_env", {"name": "X"}), ("always_fail", {})):
        _run_tool(db, project, tool, args, script=script)
    out = mcp_dev_service.publish_project(db, "owner-1", project.id, McpDevPublishIn(version_no=1))
    assert all(g["ok"] for g in out.gates)
    detail = mcp_dev_service.get_version(db, "owner-1", project.id, 1)
    assert set(detail.tool_samples) == {"add_numbers", "read_env", "always_fail"}
