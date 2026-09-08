"""multica 外部集成：配置、命令目录、/multica: 斜杠解析与工具执行。

设计约定（与需求对齐）：
- 每用户单行配置；未配置/未启用时工具不进目录，斜杠命令得到确定性
  引导回复，绝不静默失败。
- 查询工具（list_agents/list_tasks）在命令无参数尾串时由 runtime 直接
  执行（不经过 LLM 决策）；create_task 及带尾串的查询走强制 LLM 轮，
  参数从尾串提取，写操作由 runtime 审批门确认。
- PAT 经 shared encryption 加密存储、永不回显；服务地址复用 MCP 同一
  SSRF 校验。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from sqlalchemy.orm import Session

from app.shared.encryption import decrypt, encrypt
from app.super_assistant import multica_client
from app.super_assistant.models import SuperAssistantMulticaConfig
from app.super_assistant.schemas import (
    MulticaCommandOut,
    MulticaConfigOut,
    MulticaConfigUpdate,
    MulticaTestOut,
    MulticaWorkspaceOut,
    MulticaWorkspacesOut,
)


class MulticaServiceError(Exception):
    """配置层错误（由路由层翻译为 HTTP 状态码）。"""


# 命令目录：前端提示与后端解析的单一事实源
MULTICA_COMMANDS: list[dict[str, Any]] = [
    {
        "command": "list_agents",
        "aliases": ["agents"],
        "tool": "multica_list_agents",
        "title": "查看智能体",
        "description": "列出 multica 工作台的全部智能体及其运行时绑定状态。",
        "usage": "/multica:list_agents",
        "write": False,
    },
    {
        "command": "list_tasks",
        "aliases": ["tasks"],
        "tool": "multica_list_tasks",
        "title": "查看任务清单",
        "description": "查看当前工作台的任务清单，可按状态或负责人过滤。",
        "usage": "/multica:list_tasks [过滤条件]",
        "write": False,
    },
    {
        "command": "create_task",
        "aliases": ["create"],
        "tool": "multica_create_task",
        "title": "下发任务",
        "description": "在 multica 创建任务并指派给指定智能体（写操作，需用户确认）。",
        "usage": "/multica:create_task 任务描述…",
        "write": True,
    },
]

_COMMAND_INDEX: dict[str, dict[str, Any]] = {
    str(alias).lower(): item
    for item in MULTICA_COMMANDS
    for alias in (item["command"], *item["aliases"])
}

_MULTICA_TOOL_NAMES = frozenset(str(item["tool"]) for item in MULTICA_COMMANDS)
_READ_TOOLS = frozenset(
    str(item["tool"]) for item in MULTICA_COMMANDS if not item["write"]
)

_SLASH_PATTERN = re.compile(r"^/multica[:：]\s*(.*)$", re.IGNORECASE | re.DOTALL)
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MAX_LIST_LIMIT = 50
_DEFAULT_LIST_LIMIT = 10


@dataclass(frozen=True)
class MulticaSlashCommand:
    """一次 /multica: 输入的解析结果。

    state：
    - ok          命中已知命令（command/tool_name 有效，tail 为自然语言尾串）
    - unknown     前缀匹配但命令名未命中（raw_command 保留用于提示）
    - unconfigured 尚未配置 multica（命令一律不可用）
    """

    state: str
    command: dict[str, Any] | None = None
    tool_name: str = ""
    raw_command: str = ""
    tail: str = ""


def parse_slash_command(message: str, *, configured: bool) -> MulticaSlashCommand | None:
    """识别 ``/multica:命令 尾串``（兼容全角冒号）；非该前缀返回 None。"""
    match = _SLASH_PATTERN.match((message or "").strip())
    if match is None:
        return None
    if not configured:
        return MulticaSlashCommand(state="unconfigured")
    body = match.group(1).strip()
    parts = body.split(None, 1)
    raw_command = (parts[0] if parts else "").strip().lower()
    tail = parts[1].strip() if len(parts) > 1 else ""
    if not raw_command:
        return MulticaSlashCommand(state="unknown")
    item = _COMMAND_INDEX.get(raw_command)
    if item is None:
        return MulticaSlashCommand(state="unknown", raw_command=raw_command)
    return MulticaSlashCommand(
        state="ok", command=item, tool_name=str(item["tool"]), tail=tail,
    )


def guidance_text(slash: MulticaSlashCommand) -> str:
    """未配置/未知命令的确定性引导文案（不经过 LLM）。"""
    if slash.state == "unconfigured":
        return (
            "multica 集成尚未配置，/multica: 命令当前不可用。"
            "请在左侧「外部集成」中填写 multica 服务地址与 API Token 并启用后重试。"
        )
    lines = [f"未知的 multica 命令“{slash.raw_command}”。可用命令："]
    lines.extend(
        f"- {item['usage']}：{item['title']}。{item['description']}"
        for item in MULTICA_COMMANDS
    )
    return "\n".join(lines)


def forced_directive_text(slash: MulticaSlashCommand) -> str:
    """强制 LLM 轮的系统指令：只允许调用命令指定的工具，参数从尾串提取。"""
    usage = str((slash.command or {}).get("usage") or "")
    return (
        f"\n\n[平台指令] 用户通过强制命令 {usage.split(' ', 1)[0]} 指定调用工具 "
        f"{slash.tool_name}：请立即调用该工具完成上述请求，参数从命令后的描述中提取"
        "（创建任务时 title 必填、assignee 用智能体名称精确匹配）；本轮不要调用其它工具，"
        "也不要重复确认（平台会向用户展示审批）。"
    )


def get_config(db: Session, owner_id: str) -> SuperAssistantMulticaConfig | None:
    return db.get(SuperAssistantMulticaConfig, owner_id)


def active_config(db: Session, owner_id: str) -> SuperAssistantMulticaConfig | None:
    """已启用且字段齐备的配置才生效；否则 multica 工具一律不可用。"""
    config = get_config(db, owner_id)
    if config is None or not config.enabled:
        return None
    if not (config.base_url.strip() and config.workspace_id.strip() and config.token_encrypted):
        return None
    return config


def decrypt_token(config: SuperAssistantMulticaConfig) -> str:
    try:
        token = decrypt(config.token_encrypted or "")
    except Exception as exc:
        raise MulticaServiceError("multica API Token 无法解密，请重新保存配置") from exc
    token = (token or "").strip()
    if not token:
        raise MulticaServiceError("multica API Token 缺失，请重新保存配置")
    return token


def save_config(db: Session, owner_id: str, body: MulticaConfigUpdate) -> SuperAssistantMulticaConfig:
    # 地址合法性提前校验（含 SSRF 策略），失败即 400，不落库
    base_url = multica_client.normalize_base_url(body.base_url)
    config = get_config(db, owner_id)
    if config is None:
        config = SuperAssistantMulticaConfig(owner_id=owner_id, base_url="", workspace_id="")
        db.add(config)
    config.base_url = base_url
    config.workspace_id = body.workspace_id.strip()
    # 显示名由前端从测试连接的工作区列表带回；缺省保留已存名称
    # （新建行属性 flush 前为 None，需兜底空串避免非空列写入 None）
    config.workspace_name = (body.workspace_name or "").strip() or (config.workspace_name or "")
    if body.token:
        config.token_encrypted = encrypt(body.token)
    if body.enabled and not config.token_encrypted:
        raise MulticaServiceError("启用 multica 集成前请先填写 API Token")
    config.enabled = body.enabled
    db.commit()
    db.refresh(config)
    return config


def config_view(config: SuperAssistantMulticaConfig | None) -> MulticaConfigOut:
    if config is None:
        return MulticaConfigOut(
            configured=False,
            enabled=False,
            base_url="",
            workspace_id="",
            token_set=False,
            commands=[],
            last_test_status=None,
            last_test_message=None,
            last_tested_at=None,
        )
    enabled = config.enabled and bool(
        config.base_url.strip() and config.workspace_id.strip() and config.token_encrypted
    )
    return MulticaConfigOut(
        configured=True,
        enabled=enabled,
        base_url=config.base_url,
        workspace_id=config.workspace_id,
        workspace_name=config.workspace_name,
        token_set=bool(config.token_encrypted),
        commands=(
            [MulticaCommandOut(
                command=str(item["command"]),
                title=str(item["title"]),
                description=str(item["description"]),
                usage=str(item["usage"]),
                write=bool(item["write"]),
            ) for item in MULTICA_COMMANDS]
            if enabled
            else []
        ),
        last_test_status=config.last_test_status,
        last_test_message=config.last_test_message,
        last_tested_at=config.last_tested_at,
    )


def test_connection(
    db: Session,
    owner_id: str,
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> MulticaTestOut:
    """用传入草稿（优先）或已保存凭据验证连通性；结果回写到配置行。"""
    config = get_config(db, owner_id)
    effective_base = (base_url or (config.base_url if config else "") or "").strip()
    effective_token = (token or "").strip()
    if not effective_token and config is not None:
        try:
            effective_token = decrypt_token(config)
        except MulticaServiceError:
            effective_token = ""
    if not effective_base:
        return MulticaTestOut(ok=False, message="请先填写服务地址与 API Token")
    # 地址校验（含 SSRF 策略）先于连通性：非法地址按请求错误上抛（HTTP 400）
    effective_base = multica_client.normalize_base_url(effective_base)
    if not effective_token:
        return MulticaTestOut(ok=False, message="请先填写服务地址与 API Token")

    def _record(ok: bool, message: str) -> None:
        if config is None:
            return
        config.last_test_status = "success" if ok else "error"
        config.last_test_message = message[:500]
        config.last_tested_at = datetime.now(timezone.utc)
        db.commit()

    try:
        me = multica_client.fetch_me(effective_base, effective_token)
        workspaces = multica_client.list_workspaces(effective_base, effective_token)
    except multica_client.MulticaClientError as exc:
        _record(False, str(exc))
        return MulticaTestOut(ok=False, message=str(exc))
    account_name = str(me.get("name") or me.get("email") or "")
    message = f"连接成功：{account_name}，可见 {len(workspaces)} 个工作区"
    # 顺带回填当前工作区的显示名（工作区改名后经测试连接自动刷新）
    if config is not None and config.workspace_id:
        matched = next(
            (
                str(item.get("name") or "")
                for item in workspaces
                if str(item.get("id") or "") == config.workspace_id
            ),
            "",
        )
        if matched and matched != config.workspace_name:
            config.workspace_name = matched
    _record(True, message)
    return MulticaTestOut(
        ok=True,
        message=message,
        account_name=account_name or None,
        workspaces=[
            {"id": str(item.get("id") or ""), "name": str(item.get("name") or ""), "slug": str(item.get("slug") or "")}
            for item in workspaces
        ],
    )


def list_config_workspaces(db: Session, owner_id: str) -> MulticaWorkspacesOut:
    """已保存配置的实时工作区列表（配置弹窗打开即拉取）。

    工作区列表不持久化：弹窗每次打开用它取全量下拉，替代"只显示已保存
    单条"的前端兜底。不写 last_test_*，未配置按请求错误上抛；上游连通性
    失败由 MulticaClientError 原样抛给路由层翻译（HTTP 502）。
    """
    config = get_config(db, owner_id)
    if config is None or not (config.base_url.strip() and config.token_encrypted):
        raise MulticaServiceError("multica 尚未配置完整，请先保存服务地址与 API Token")
    workspaces = multica_client.list_workspaces(config.base_url, decrypt_token(config))
    return MulticaWorkspacesOut(
        workspaces=[
            MulticaWorkspaceOut(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                slug=str(item.get("slug") or ""),
            )
            for item in workspaces
        ],
    )


def tool_schemas() -> list[dict[str, Any]]:
    """multica 工具的 LLM function-calling schema（仅在配置生效时进入目录）。"""
    return [
        {
            "name": "multica_list_agents",
            "description": "列出 multica 工作台的全部智能体（名称、描述、运行时绑定状态）。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "multica_list_tasks",
            "description": "查看 multica 工作台的任务清单，可按状态或负责人过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "状态过滤（如 open/in_progress/done），留空返回全部"},
                    "assignee": {"type": "string", "description": "负责人名称过滤，留空返回全部"},
                    "limit": {"type": "integer", "description": f"返回条数上限，默认 {_DEFAULT_LIST_LIMIT}，最大 {_MAX_LIST_LIMIT}"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "multica_create_task",
            "description": (
                "在 multica 创建任务并指派给指定智能体（写操作，执行前需用户确认）。"
                "assignee 尽量使用 multica_list_agents 返回的智能体名称。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题（必填，一句话概括）"},
                    "assignee": {"type": "string", "description": "指派对象：智能体名称或 ID，留空则不指派"},
                    "description": {"type": "string", "description": "任务详细描述（可选）"},
                    "allow_duplicate": {"type": "boolean", "description": "同名活跃任务默认被拒绝（409）；确需重复创建时置 true"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    ]


def _brief(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _trim_agent(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "description": _brief(agent.get("description")),
        "runtime_bound": bool(agent.get("runtime_bound")),
    }


def _trim_issue(issue: dict[str, Any]) -> dict[str, Any]:
    assignee = issue.get("assignee") or issue.get("assignee_name")
    if isinstance(assignee, dict):
        assignee = assignee.get("name")
    return {
        "identifier": issue.get("identifier") or issue.get("key"),
        "number": issue.get("number"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        "assignee": assignee,
        "updated_at": issue.get("updated_at"),
    }


def execute_tool(db: Session, owner_id: str, name: str, arguments: dict[str, Any]) -> str:
    """multica 工具统一执行入口；失败抛错（runtime 记为工具错误并回灌模型）。"""
    if name not in _MULTICA_TOOL_NAMES:
        raise MulticaServiceError(f"未知 multica 工具 {name}")
    config = active_config(db, owner_id)
    if config is None:
        raise MulticaServiceError("multica 未配置或未启用，请在「外部集成」中完成配置")
    token = decrypt_token(config)
    base_url, workspace_id = config.base_url, config.workspace_id

    if name == "multica_list_agents":
        agents = multica_client.list_agents(base_url, token, workspace_id)
        return json.dumps(
            {"count": len(agents), "agents": [_trim_agent(agent) for agent in agents]},
            ensure_ascii=False,
        )

    if name == "multica_list_tasks":
        try:
            limit = int(arguments.get("limit") or _DEFAULT_LIST_LIMIT)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIST_LIMIT
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        issues = multica_client.list_issues(
            base_url,
            token,
            workspace_id,
            status=str(arguments.get("status") or "").strip() or None,
            assignee=str(arguments.get("assignee") or "").strip() or None,
            limit=limit,
        )
        return json.dumps(
            {"count": len(issues), "issues": [_trim_issue(issue) for issue in issues]},
            ensure_ascii=False,
        )

    # multica_create_task
    title = str(arguments.get("title") or "").strip()
    if not title:
        raise MulticaServiceError("创建任务失败：title 不能为空")
    assignee = str(arguments.get("assignee") or "").strip() or None
    description = str(arguments.get("description") or "").strip() or None
    assignee_id: str | None = None
    resolved_name: str | None = None
    if assignee:
        # 服务端 create 只认 assignee_type=agent + assignee_id（真实环境实测：
        # 名称字段会被静默忽略）；名称到 ID 的解析与 multica CLI 同语义
        if _UUID_PATTERN.match(assignee):
            assignee_id = assignee
        else:
            agents = multica_client.list_agents(base_url, token, workspace_id)
            assignee_id, resolved_name = multica_client.match_agent(agents, assignee)
    issue = multica_client.create_issue(
        base_url,
        token,
        workspace_id,
        title=title,
        description=description,
        assignee_id=assignee_id,
        allow_duplicate=bool(arguments.get("allow_duplicate")),
    )
    return json.dumps(
        {
            "created": True,
            "issue": _trim_issue(issue),
            "assignee": resolved_name or assignee,
            "note": "任务已创建" + (
                f"并指派给 {resolved_name or assignee}，被指派的智能体会自动开始执行"
                if assignee_id else ""
            ),
        },
        ensure_ascii=False,
    )
