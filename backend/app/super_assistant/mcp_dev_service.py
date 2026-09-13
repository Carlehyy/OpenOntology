"""自研 MCP（插件社区「开发 MCP」）— HTTP 层业务逻辑。

生命周期与推演服务同一纪律：
  「执行」= 内核试跑（解析工具清单或调用单个工具），成功调用自动把入参
  记为该工具的样例参数；「保存」= 服务端复核后冻结版本（保留 20 版，
  永不修剪发布绑定版）；「发布」= 逐工具样例真实执行 + 描述完备性闸门，
  通过后把冻结版本固化为 SuperAssistantMcpServer 行（transport='developed'，
  默认停用 + 逐次确认，与导入 MCP 同一启用路径）。

已发布的 MCP 始终执行绑定版本的冻结脚本；草稿编辑不影响线上，直到重新
发布。agent 运行时经 call_published_tool 进程内调用（对标 builtin minio）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.data_channel.pipelines.python_engine.client import PythonEngineError
from app.super_assistant import mcp_dev_executor as executor
from app.super_assistant.models import (
    SuperAssistantMcpDevProject,
    SuperAssistantMcpDevVersion,
    SuperAssistantMcpServer,
)
from app.super_assistant.schemas import (
    McpDevExecuteIn,
    McpDevExecuteOut,
    McpDevProjectCreate,
    McpDevProjectDetailOut,
    McpDevProjectOut,
    McpDevProjectUpdate,
    McpDevPublishIn,
    McpDevPublishOut,
    McpDevSaveIn,
    McpDevSaveOut,
    McpDevVersionDetailOut,
    McpDevVersionOut,
    McpTestOut,
)

# 每个项目保留的脚本历史版数上限（对齐推演服务 / Python 脚本流水线）
SCRIPT_VERSION_KEEP = 20

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"

_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 新建项目的脚本模板：声明平台统一的工具注册契约
SCRIPT_TEMPLATE = '''# @mcp_tool 注册器由平台注入，可直接使用；OB_ENV / OB_SECRET 为个人
# 设置中环境/隐私变量的只读字典（例如 OB_ENV.get("MY_API_KEY")）。


@mcp_tool(description="示例工具：把两个数字相加。请替换为你的接口封装逻辑")
def add_numbers(a: int, b: int = 1) -> dict:
    """工具入参从类型注解推导为 JSON Schema；返回值必须可 JSON 序列化。"""
    return {"sum": a + b}
'''


class McpDevServiceError(Exception):
    """Base error translated to an HTTP response by the community router."""


class McpDevNotFoundError(McpDevServiceError):
    pass


class McpDevValidationError(McpDevServiceError):
    pass


class McpDevConflictError(McpDevServiceError):
    pass


class McpDevUnavailableError(McpDevServiceError):
    """执行网关不可用等基础设施失败（映射 502）。"""


def _load_project(db: Session, owner_id: str, project_id: str) -> SuperAssistantMcpDevProject:
    project = db.query(SuperAssistantMcpDevProject).filter(
        SuperAssistantMcpDevProject.id == project_id,
        SuperAssistantMcpDevProject.owner_id == owner_id,
    ).first()
    if project is None:
        raise McpDevNotFoundError("开发项目不存在")
    return project


def _version_count(db: Session, project_id: str) -> int:
    return (
        db.query(func.count(SuperAssistantMcpDevVersion.id))
        .filter(SuperAssistantMcpDevVersion.project_id == project_id)
        .scalar()
        or 0
    )


def _published_version_no(db: Session, project: SuperAssistantMcpDevProject) -> int | None:
    if not project.published_version_id:
        return None
    row = db.get(SuperAssistantMcpDevVersion, project.published_version_id)
    return row.version_no if row is not None else None


def _latest_version(db: Session, project_id: str) -> SuperAssistantMcpDevVersion | None:
    return (
        db.query(SuperAssistantMcpDevVersion)
        .filter(SuperAssistantMcpDevVersion.project_id == project_id)
        .order_by(SuperAssistantMcpDevVersion.version_no.desc())
        .first()
    )


def _project_out(
    db: Session,
    project: SuperAssistantMcpDevProject,
    *,
    detail: bool,
) -> McpDevProjectOut | McpDevProjectDetailOut:
    latest = _latest_version(db, project.id)
    common = dict(
        id=project.id,
        name=project.name,
        display_name=project.display_name,
        description=project.description,
        status=project.status,
        tool_count=len(latest.tool_manifest or []) if latest is not None else 0,
        version_count=_version_count(db, project.id),
        published_version_no=_published_version_no(db, project),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
    if detail:
        return McpDevProjectDetailOut(script=project.script, tool_samples=project.tool_samples or {}, **common)
    return McpDevProjectOut(**common)


def list_projects(db: Session, owner_id: str) -> list[McpDevProjectOut]:
    rows = (
        db.query(SuperAssistantMcpDevProject)
        .filter(SuperAssistantMcpDevProject.owner_id == owner_id)
        .order_by(SuperAssistantMcpDevProject.updated_at.desc())
        .all()
    )
    return [_project_out(db, row, detail=False) for row in rows]


def create_project(db: Session, owner_id: str, body: McpDevProjectCreate) -> McpDevProjectDetailOut:
    duplicate = db.query(SuperAssistantMcpDevProject).filter(
        SuperAssistantMcpDevProject.owner_id == owner_id,
        SuperAssistantMcpDevProject.name == body.name,
    ).first()
    if duplicate:
        raise McpDevConflictError("同名开发项目已存在")
    project = SuperAssistantMcpDevProject(
        owner_id=owner_id,
        name=body.name,
        display_name=body.display_name or body.name,
        description=body.description,
        script=SCRIPT_TEMPLATE,
        tool_samples={},
    )
    db.add(project)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise McpDevConflictError("同名开发项目已存在") from exc
    db.refresh(project)
    return _project_out(db, project, detail=True)  # type: ignore[return-value]


def get_project(db: Session, owner_id: str, project_id: str) -> McpDevProjectDetailOut:
    return _project_out(db, _load_project(db, owner_id, project_id), detail=True)  # type: ignore[return-value]


def update_project(
    db: Session,
    owner_id: str,
    project_id: str,
    body: McpDevProjectUpdate,
) -> McpDevProjectDetailOut:
    project = _load_project(db, owner_id, project_id)
    if body.display_name is not None:
        project.display_name = body.display_name
    if body.description is not None:
        project.description = body.description
    db.commit()
    db.refresh(project)
    return _project_out(db, project, detail=True)  # type: ignore[return-value]


def remove_project(db: Session, owner_id: str, project_id: str) -> None:
    project = _load_project(db, owner_id, project_id)
    delete_project_row(db, project)
    db.commit()


def delete_project_row(db: Session, project: SuperAssistantMcpDevProject) -> None:
    """删除项目及其版本与关联的已发布 MCP 行（社区删除入口共用）。"""
    for version in db.query(SuperAssistantMcpDevVersion).filter(
        SuperAssistantMcpDevVersion.project_id == project.id,
    ).all():
        db.delete(version)
    server = db.query(SuperAssistantMcpServer).filter(
        SuperAssistantMcpServer.dev_project_id == project.id,
    ).first()
    if server is not None:
        db.delete(server)
    db.delete(project)


# ──────────────────────────── 执行与保存 ────────────────────────────


def execute_project(
    db: Session,
    owner_id: str,
    project_id: str,
    body: McpDevExecuteIn,
) -> McpDevExecuteOut:
    """试跑：tool_name 为空时解析工具清单，否则调用该工具。

    工具调用成功后自动把本次入参记为样例参数（发布闸门的数据源），
    因此「最后一次成功试跑的参数」即发布校验使用的样例。
    """
    project = _load_project(db, owner_id, project_id)
    personal = executor.load_personal_vars(db, owner_id)
    try:
        if body.tool_name:
            result = executor.call_tool(body.script, personal, body.tool_name, body.arguments)
            if result.ok and isinstance(result.payload, dict) and result.payload.get("ok"):
                samples = dict(project.tool_samples or {})
                samples[body.tool_name] = body.arguments or {}
                project.tool_samples = samples
                db.commit()
            return McpDevExecuteOut(
                ok=result.ok,
                payload=result.payload,
                stdout=result.stdout,
                error=result.error,
                traceback=result.traceback,
                duration_ms=result.duration_ms,
            )
        result = executor.introspect(body.script, personal)
        return McpDevExecuteOut(
            ok=result.ok,
            tools=result.payload if isinstance(result.payload, list) else [],
            payload=None,
            stdout=result.stdout,
            error=result.error,
            traceback=result.traceback,
            duration_ms=result.duration_ms,
        )
    except executor.McpDevExecutorError as exc:
        raise McpDevValidationError(str(exc)) from exc
    except PythonEngineError as exc:
        raise McpDevUnavailableError(str(exc)) from exc


def save_project(
    db: Session,
    owner_id: str,
    project_id: str,
    body: McpDevSaveIn,
) -> McpDevSaveOut:
    """保存脚本：服务端重新内省复核，通过才落库并冻结版本。"""
    project = _load_project(db, owner_id, project_id)
    personal = executor.load_personal_vars(db, owner_id)
    try:
        result = executor.introspect(body.script, personal)
    except executor.McpDevExecutorError as exc:
        raise McpDevValidationError(str(exc)) from exc
    except PythonEngineError as exc:
        raise McpDevUnavailableError(str(exc)) from exc
    if not result.ok:
        return McpDevSaveOut(
            ok=False, error=result.error, traceback=result.traceback,
            duration_ms=result.duration_ms,
        )
    manifest = result.payload if isinstance(result.payload, list) else []
    if not manifest:
        return McpDevSaveOut(
            ok=False,
            error="未解析到任何工具：请至少用一个 @mcp_tool 装饰器声明工具函数",
            traceback="", duration_ms=result.duration_ms,
        )
    for tool in manifest:
        name = str(tool.get("name") or "")
        if not _TOOL_NAME_RE.match(name) or len(name) > 64:
            return McpDevSaveOut(
                ok=False,
                error=f"工具名 {name!r} 无效：必须是 1-64 位的 Python 标识符",
                traceback="", duration_ms=result.duration_ms,
            )

    project.script = body.script
    next_version_no = (
        db.query(func.max(SuperAssistantMcpDevVersion.version_no))
        .filter(SuperAssistantMcpDevVersion.project_id == project.id)
        .scalar()
        or 0
    ) + 1
    known_names = {str(tool.get("name")) for tool in manifest}
    samples = {
        name: args
        for name, args in (project.tool_samples or {}).items()
        if name in known_names
    }
    version = SuperAssistantMcpDevVersion(
        project_id=project.id,
        version_no=next_version_no,
        script=body.script,
        tool_manifest=manifest,
        tool_samples=samples,
        duration_ms=result.duration_ms,
    )
    db.add(version)
    db.flush()

    # 修剪历史版本：只保留最近 SCRIPT_VERSION_KEEP 版；发布绑定版永不修剪
    stale = (
        db.query(SuperAssistantMcpDevVersion)
        .filter(SuperAssistantMcpDevVersion.project_id == project.id)
        .order_by(SuperAssistantMcpDevVersion.version_no.desc())
        .offset(SCRIPT_VERSION_KEEP)
        .all()
    )
    for row in stale:
        if row.id == project.published_version_id:
            continue
        db.delete(row)

    db.commit()
    return McpDevSaveOut(
        ok=True, version_no=next_version_no, tools=manifest,
        duration_ms=result.duration_ms,
    )


def list_versions(db: Session, owner_id: str, project_id: str) -> list[McpDevVersionOut]:
    project = _load_project(db, owner_id, project_id)
    rows = (
        db.query(SuperAssistantMcpDevVersion)
        .filter(SuperAssistantMcpDevVersion.project_id == project.id)
        .order_by(SuperAssistantMcpDevVersion.version_no.desc())
        .all()
    )
    return [
        McpDevVersionOut(
            id=row.id,
            version_no=row.version_no,
            tool_count=len(row.tool_manifest or []),
            duration_ms=row.duration_ms,
            created_at=row.created_at,
        )
        for row in rows
    ]


def get_version(
    db: Session,
    owner_id: str,
    project_id: str,
    version_no: int,
) -> McpDevVersionDetailOut:
    project = _load_project(db, owner_id, project_id)
    row = (
        db.query(SuperAssistantMcpDevVersion)
        .filter(
            SuperAssistantMcpDevVersion.project_id == project.id,
            SuperAssistantMcpDevVersion.version_no == version_no,
        )
        .first()
    )
    if row is None:
        raise McpDevNotFoundError("脚本版本不存在")
    return McpDevVersionDetailOut(
        id=row.id,
        version_no=row.version_no,
        tool_count=len(row.tool_manifest or []),
        duration_ms=row.duration_ms,
        created_at=row.created_at,
        script=row.script,
        tool_manifest=row.tool_manifest or [],
        tool_samples=row.tool_samples or {},
        tool_gates=row.tool_gates or None,
    )


# ──────────────────────────── 发布（闸门 + 固化） ────────────────────────────


def _validate_manifest_gates(manifest: list, samples: dict) -> None:
    """描述完备性与样例完备性：不满足时直接拒绝，不进入真实执行。"""
    if not manifest:
        raise McpDevValidationError("该版本未解析出任何工具，无法发布")
    missing_desc = [
        str(tool.get("name"))
        for tool in manifest
        if not str(tool.get("description") or "").strip()
    ]
    if missing_desc:
        raise McpDevValidationError(
            "以下工具缺少描述（MCP 工具必须带描述才能被 agent 正确选用）："
            + ", ".join(missing_desc)
        )
    missing_samples = [
        str(tool.get("name")) for tool in manifest if str(tool.get("name")) not in samples
    ]
    if missing_samples:
        raise McpDevValidationError(
            "以下工具还没有样例参数（请在开发页用真实入参成功试跑一次，"
            "发布校验将按样例参数执行）："
            + ", ".join(missing_samples)
        )


def _run_gates(
    db: Session,
    owner_id: str,
    version: SuperAssistantMcpDevVersion,
    *,
    project: SuperAssistantMcpDevProject | None = None,
) -> list[dict]:
    """逐工具按样例参数真实执行，返回逐工具结果清单。

    样例来源：版本冻结快照优先；快照里缺的工具回退到项目当前样例
    （试跑成功即记在项目上，晚于保存冻结也仍可用于校验），并把合并结果
    回写版本快照——否则用户按提示重新试跑也无法修复"缺样例"的旧版本。
    """
    manifest = version.tool_manifest or []
    samples = {**(project.tool_samples or {}), **(version.tool_samples or {})} if project else dict(version.tool_samples or {})
    _validate_manifest_gates(manifest, samples)
    version.tool_samples = {
        str(tool.get("name")): samples[str(tool.get("name"))]
        for tool in manifest
    }
    personal = executor.load_personal_vars(db, owner_id)
    picked = {
        str(tool.get("name")): samples[str(tool.get("name"))]
        for tool in manifest
    }
    try:
        result = executor.run_gates(version.script, personal, picked)
    except executor.McpDevExecutorError as exc:
        raise McpDevValidationError(str(exc)) from exc
    except PythonEngineError as exc:
        raise McpDevUnavailableError(str(exc)) from exc
    if not result.ok:
        raise McpDevUnavailableError(f"发布校验执行失败：{result.error}")
    gates = result.payload if isinstance(result.payload, list) else []
    failed = [g for g in gates if not g.get("ok")]
    if failed:
        summary = "；".join(
            f"{g.get('name')}：{g.get('error') or '执行失败'}" for g in failed
        )
        raise McpDevValidationError(f"发布校验未通过（样例参数真实执行失败）：{summary[:500]}")
    return gates


def publish_project(
    db: Session,
    owner_id: str,
    project_id: str,
    body: McpDevPublishIn,
) -> McpDevPublishOut:
    """发布为 MCP：闸门全过 → 固化/更新 super_assistant_mcp_servers 行。

    重新发布 = 绑定新冻结版本；enabled / require_confirmation 尊重用户既有
    设置（新发布默认停用 + 逐次确认，与导入 MCP 同一启用路径）。
    """
    project = _load_project(db, owner_id, project_id)
    if body.version_no is not None:
        version = (
            db.query(SuperAssistantMcpDevVersion)
            .filter(
                SuperAssistantMcpDevVersion.project_id == project.id,
                SuperAssistantMcpDevVersion.version_no == body.version_no,
            )
            .first()
        )
        if version is None:
            raise McpDevNotFoundError("指定的脚本版本不存在")
    else:
        version = (
            db.query(SuperAssistantMcpDevVersion)
            .filter(SuperAssistantMcpDevVersion.project_id == project.id)
            .order_by(SuperAssistantMcpDevVersion.version_no.desc())
            .first()
        )
        if version is None:
            raise McpDevValidationError("尚未保存任何版本：请先在开发页执行通过并保存，再发布。")

    if body.display_name is not None:
        project.display_name = body.display_name
    if body.description is not None:
        project.description = body.description

    manifest = version.tool_manifest or []
    gates = _run_gates(db, owner_id, version, project=project)
    version.tool_gates = gates

    server = db.query(SuperAssistantMcpServer).filter(
        SuperAssistantMcpServer.owner_id == owner_id,
        SuperAssistantMcpServer.dev_project_id == project.id,
    ).first()
    if server is None:
        clash = db.query(SuperAssistantMcpServer).filter(
            SuperAssistantMcpServer.owner_id == owner_id,
            SuperAssistantMcpServer.name == project.name,
        ).first()
        if clash is not None:
            raise McpDevConflictError(
                f"已存在名为 {project.name} 的 MCP Server：请修改项目标识后重新发布"
            )
        server = SuperAssistantMcpServer(
            owner_id=owner_id,
            name=project.name,
            builtin_key=None,
            transport="developed",
            url=f"developed://{project.id}",
            dev_project_id=project.id,
            headers_encrypted=None,
            header_names=[],
            command=None,
            args=[],
            env_encrypted=None,
            env_names=[],
            enabled=False,
            require_confirmation=True,
        )
        db.add(server)

    server.display_name = project.display_name or project.name
    server.description = project.description
    server.tool_manifest = manifest
    server.last_test_status = "success"
    server.last_test_message = f"发布校验通过：{len(manifest)} 个工具全部执行成功"
    server.last_tested_at = datetime.now(timezone.utc)

    project.status = STATUS_PUBLISHED
    project.published_version_id = version.id

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise McpDevConflictError(
            f"已存在名为 {project.name} 的 MCP Server：请修改项目标识后重新发布"
        ) from exc
    db.refresh(server)
    return McpDevPublishOut(
        server_id=server.id,
        version_no=version.version_no,
        tools=manifest,
        gates=gates,
    )


def verify_published_server(
    db: Session,
    owner_id: str,
    server: SuperAssistantMcpServer,
) -> McpTestOut:
    """社区页「测试」动作对自研 MCP 的分支：重跑发布闸门并刷新状态。"""
    server.last_tested_at = datetime.now(timezone.utc)
    project = db.query(SuperAssistantMcpDevProject).filter(
        SuperAssistantMcpDevProject.id == server.dev_project_id,
        SuperAssistantMcpDevProject.owner_id == owner_id,
    ).first()
    version = (
        db.get(SuperAssistantMcpDevVersion, project.published_version_id)
        if project is not None and project.published_version_id
        else None
    )
    if project is None or version is None:
        server.tool_manifest = []
        server.last_test_status = "error"
        server.last_test_message = "开发项目或绑定版本不存在：请重新发布或删除该 MCP"
        db.commit()
        return McpTestOut(ok=False, message=server.last_test_message, tools=[])
    try:
        gates = _run_gates(db, owner_id, version, project=project)
    except McpDevServiceError as exc:
        server.tool_manifest = []
        server.last_test_status = "error"
        server.last_test_message = str(exc)[:500]
        db.commit()
        return McpTestOut(ok=False, message=str(exc), tools=[])
    version.tool_gates = gates
    server.tool_manifest = version.tool_manifest or []
    server.last_test_status = "success"
    server.last_test_message = f"校验通过：{len(server.tool_manifest)} 个工具全部执行成功"
    db.commit()
    return McpTestOut(ok=True, message=server.last_test_message, tools=server.tool_manifest)


# ──────────────────────────── agent 运行时入口 ────────────────────────────


def call_published_tool(
    db: Session,
    server: SuperAssistantMcpServer,
    tool_name: str,
    arguments: dict,
) -> str:
    """超级助手运行时的自研 MCP 工具调用：执行发布绑定的冻结脚本。

    永不抛异常：任何失败都收敛为 JSON 错误串回灌模型上下文（与外部 MCP
    工具失败的语义一致），由 runtime 统一截断与落库。
    """
    try:
        project = db.query(SuperAssistantMcpDevProject).filter(
            SuperAssistantMcpDevProject.id == server.dev_project_id,
            SuperAssistantMcpDevProject.owner_id == server.owner_id,
        ).first()
        version = (
            db.get(SuperAssistantMcpDevVersion, project.published_version_id)
            if project is not None and project.published_version_id
            else None
        )
        if project is None or version is None:
            return json.dumps(
                {"error": "自研 MCP 的开发项目或绑定版本不存在：请到插件社区重新发布或删除"},
                ensure_ascii=False,
            )
        personal = executor.load_personal_vars(db, server.owner_id)
        result = executor.call_tool(
            version.script,
            personal,
            tool_name,
            arguments,
            timeout=settings.mcp_dev_tool_timeout_seconds,
        )
        return executor.serialize_for_runtime(result, personal)
    except Exception as exc:  # noqa: BLE001 — 工具失败进模型上下文，不抛穿会话
        return json.dumps({"error": f"自研 MCP 工具执行失败：{exc}"}, ensure_ascii=False)
