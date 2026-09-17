"""multica REST 客户端：外部集成工具的薄 HTTP 层。

所有 httpx 调用集中在 ``_request``（测试 monkeypatch 此函数）；服务地址
复用 MCP/web 工具同一 SSRF 校验（生产环境拒绝非公网地址）。鉴权与
multica 官方 CLI/API 约定一致：PAT Bearer + 工作区级 X-Workspace-ID。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.super_assistant.mcp_client import McpClientError, validate_mcp_url

_TIMEOUT_SECONDS = 20.0
_USER_AGENT = "OpenOntology-SuperAssistant/1.0"


class MulticaClientError(RuntimeError):
    """可直接展示给模型/用户的 multica 请求失败。"""


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """集中的 httpx 调用点（测试 monkeypatch 此函数）。"""
    kwargs["trust_env"] = False
    return httpx.request(method, url, **kwargs)


def normalize_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    try:
        return validate_mcp_url(value, require_https=True)
    except McpClientError as exc:
        raise MulticaClientError(f"multica 服务地址无效：{exc}") from exc


def _headers(token: str, workspace_id: str | None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if workspace_id:
        headers["X-Workspace-ID"] = workspace_id
    return headers


def _error_brief(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])[:160]
    return (response.text or "").strip()[:160]


def _call(
    method: str,
    base_url: str,
    token: str,
    path: str,
    *,
    workspace_id: str | None = None,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    allow_empty: bool = False,
) -> Any:
    url = f"{normalize_base_url(base_url)}{path}"
    cleaned_params = {
        str(key): value
        for key, value in (params or {}).items()
        if value is not None and value != ""
    } or None
    response = _request(
        method,
        url,
        headers=_headers(token, workspace_id),
        params=cleaned_params,
        json=payload,
        timeout=_TIMEOUT_SECONDS,
        # A redirect changes the host to which the bearer token would be sent.
        # Fail closed and let the caller surface the HTTP status instead of
        # allowing an unvalidated redirect target.
        follow_redirects=False,
    )
    if not 200 <= response.status_code < 300:
        raise MulticaClientError(
            f"multica 请求失败（{path}）：HTTP {response.status_code} {_error_brief(response)}".strip()
        )
    try:
        return response.json()
    except ValueError as exc:
        if allow_empty and not (response.text or "").strip():
            return {}
        raise MulticaClientError(f"multica 响应不是有效 JSON（{path}）") from exc


def fetch_me(base_url: str, token: str) -> dict[str, Any]:
    data = _call("GET", base_url, token, "/api/me")
    return data if isinstance(data, dict) else {}


def list_workspaces(base_url: str, token: str) -> list[dict[str, Any]]:
    data = _call("GET", base_url, token, "/api/workspaces")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("workspaces"), list):
        return [item for item in data["workspaces"] if isinstance(item, dict)]
    return []


def list_agents(base_url: str, token: str, workspace_id: str) -> list[dict[str, Any]]:
    data = _call("GET", base_url, token, "/api/agents", workspace_id=workspace_id)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("agents"), list):
        return [item for item in data["agents"] if isinstance(item, dict)]
    return []


def list_issues(
    base_url: str,
    token: str,
    workspace_id: str,
    *,
    status: str | None = None,
    assignee: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    data = _call(
        "GET",
        base_url,
        token,
        "/api/issues",
        workspace_id=workspace_id,
        params={"status": status, "assignee": assignee, "limit": str(limit)},
    )
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("issues"), list):
        items = data["issues"]
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def match_agent(agents: list[dict[str, Any]], name: str) -> tuple[str, str]:
    """按名称在智能体列表中模糊解析：精确命中优先，其次唯一子串命中。

    与 multica CLI 的 --assignee 语义对齐（名称由客户端解析成 ID；服务端
    只认 assignee_type + assignee_id）。未命中/歧义时抛出可读错误。
    """
    needle = (name or "").strip().lower()
    if not needle:
        raise MulticaClientError("智能体名称不能为空")
    named = [
        agent for agent in agents
        if str(agent.get("name") or "").strip()
    ]
    exact = [agent for agent in named if str(agent["name"]).lower() == needle]
    if exact:
        return str(exact[0]["id"]), str(exact[0]["name"])
    partial = [
        agent for agent in named
        if needle in str(agent["name"]).lower()
    ]
    if len(partial) == 1:
        return str(partial[0]["id"]), str(partial[0]["name"])
    if len(partial) > 1:
        candidates = "、".join(str(agent["name"]) for agent in partial[:5])
        raise MulticaClientError(
            f"名称“{name}”匹配到多个智能体（{candidates}），请用更精确的名称或 ID"
        )
    raise MulticaClientError(
        f'未在工作区找到名称匹配“{name}”的智能体，可先调用 multica_list_agents 查看清单'
    )


def create_issue(
    base_url: str,
    token: str,
    workspace_id: str,
    *,
    title: str,
    description: str | None = None,
    assignee_id: str | None = None,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """创建 issue；指派走 assignee_type=agent + assignee_id（服务端契约）。

    名称到 ID 的解析由调用方（multica_service）先经 match_agent 完成——
    直接传名称字段会被服务端静默忽略（真实环境 E2E 实测）。同名活跃任务
    默认被服务端 409 拒绝（duplicate 保护），确需重复创建时置 allow_duplicate。
    """
    payload: dict[str, Any] = {"title": title}
    if description:
        payload["description"] = description
    if assignee_id:
        payload["assignee_type"] = "agent"
        payload["assignee_id"] = assignee_id.strip()
    if allow_duplicate:
        payload["allow_duplicate"] = True
    data = _call("POST", base_url, token, "/api/issues", workspace_id=workspace_id, payload=payload)
    return data if isinstance(data, dict) else {}


def _issue_path(issue_ref: str) -> str:
    """Return an encoded issue identifier path segment.

    Multica accepts both its human identifier (for example ``MYW-9``) and the
    UUID returned by the API.  Encoding the complete segment prevents a remote
    task reference from changing the request path.
    """
    value = str(issue_ref or "").strip()
    if not value or len(value) > 255:
        raise MulticaClientError("multica issue 标识不能为空且长度不能超过 255")
    return f"/api/issues/{quote(value, safe='')}"


def get_issue(
    base_url: str,
    token: str,
    workspace_id: str | None,
    issue_ref: str,
) -> dict[str, Any]:
    """Fetch one issue by identifier/UUID for durable external reconciliation."""
    data = _call("GET", base_url, token, _issue_path(issue_ref), workspace_id=workspace_id)
    return data if isinstance(data, dict) else {}


def get_active_task(
    base_url: str,
    token: str,
    workspace_id: str | None,
    issue_ref: str,
) -> dict[str, Any]:
    """Return the active execution task associated with an issue.

    The endpoint returns either a task object or ``{task_id: ...}`` depending
    on the Multica server revision; callers normalize both forms.
    """
    data = _call(
        "GET", base_url, token, f"{_issue_path(issue_ref)}/active-task",
        workspace_id=workspace_id,
    )
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        return dict(data["task"])
    return data if isinstance(data, dict) else {}


def cancel_task(
    base_url: str,
    token: str,
    workspace_id: str | None,
    issue_ref: str,
    task_id: str,
) -> dict[str, Any]:
    """Request cancellation of a Multica execution task.

    Cancellation is an explicit provider operation.  A successful HTTP
    response with no body is still a valid acknowledgement and is represented
    as an empty object for the connector to normalize.
    """
    task = str(task_id or "").strip()
    if not task or len(task) > 255:
        raise MulticaClientError("multica task 标识不能为空且长度不能超过 255")
    data = _call(
        "POST", base_url, token,
        f"{_issue_path(issue_ref)}/tasks/{quote(task, safe='')}/cancel",
        workspace_id=workspace_id,
        allow_empty=True,
    )
    return data if isinstance(data, dict) else {}
