"""multica REST 客户端：外部集成工具的薄 HTTP 层。

所有 httpx 调用集中在 ``_request``（测试 monkeypatch 此函数）；服务地址
复用 MCP/web 工具同一 SSRF 校验（生产环境拒绝非公网地址）。鉴权与
multica 官方 CLI/API 约定一致：PAT Bearer + 工作区级 X-Workspace-ID。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

import httpx

from app.super_assistant.mcp_client import McpClientError, validate_mcp_url

_TIMEOUT_SECONDS = 20.0
_USER_AGENT = "OpenOntology-SuperAssistant/1.0"


class MulticaClientError(RuntimeError):
    """可直接展示给模型/用户的 multica 请求失败。"""


class MulticaRequestError(MulticaClientError):
    """A transport/server error with explicit effect certainty, never a retry hint."""

    def __init__(self, message: str, *, outcome: str, status_code: int | None = None):
        super().__init__(message)
        self.outcome = outcome
        self.status_code = status_code


@dataclass(frozen=True)
class MulticaRead:
    items: list[dict[str, Any]]
    complete: bool
    cursor: dict[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True)
class MulticaCommentResult:
    # Saving a comment never by itself proves an agent resumed.
    comment_id: str
    comment: dict[str, Any]
    execution_state: str
    execution_evidence: dict[str, Any]


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """集中的 httpx 调用点（测试 monkeypatch 此函数）。"""
    kwargs["trust_env"] = False
    return httpx.request(method, url, **kwargs)


def clean_base_url(base_url: str) -> str:
    """Parse a deployment URL without DNS or network access."""
    value = (base_url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("必须是 HTTP/HTTPS 绝对地址")
        if (parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(char.isspace() for char in value)):
            raise ValueError("地址不能包含凭据、查询串、片段或空白")
        if parsed.port == 0:
            raise ValueError("端口无效")
    except ValueError as exc:
        raise MulticaClientError(f"multica 服务地址无效：{exc}") from exc
    return value


def normalize_base_url(base_url: str) -> str:
    from app.shared.config import settings

    value = clean_base_url(base_url)
    try:
        return validate_mcp_url(
            value, require_https=True,
            internal_base_urls=settings.super_assistant_multica_internal_base_urls,
        )
    except ValueError as exc:
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
    include_headers: bool = False,
) -> Any:
    url = f"{normalize_base_url(base_url)}{path}"
    cleaned_params = {
        str(key): value
        for key, value in (params or {}).items()
        if value is not None and value != ""
    } or None
    writing = method.upper() not in {"GET", "HEAD", "OPTIONS"}
    try:
        response = _request(
            method, url, headers=_headers(token, workspace_id),
            params=cleaned_params, json=payload, timeout=_TIMEOUT_SECONDS,
            # Redirects must not forward the owner's PAT to another host.
            follow_redirects=False,
        )
    except httpx.RequestError as exc:
        # Even a connection reset can follow a successful remote commit. Never
        # include the exception text, URL, headers or PAT in the user-facing error.
        raise MulticaRequestError("multica 请求未取得可靠响应",
            outcome="unknown" if writing else "failed") from exc
    if response.status_code == 403:
        from app.super_assistant.provider import check_daily_limit
        check_daily_limit(response.status_code, response.text)
    if not 200 <= response.status_code < 300:
        # A server-side failure can occur after commit. An explicit 4xx rejection
        # is failed; none of these classifications authorize automatic retries.
        raise MulticaRequestError(
            f"multica 请求失败：HTTP {response.status_code}",
            outcome="unknown" if writing and response.status_code >= 500 else "failed",
            status_code=response.status_code,
        )
    try:
        data = response.json()
    except ValueError as exc:
        if allow_empty and not (response.text or "").strip():
            data = {}
        else:
            raise MulticaRequestError(f"multica 响应不是有效 JSON（{path}）",
                outcome="unknown" if writing else "failed") from exc
    if include_headers:
        headers = {str(k).lower(): str(v) for k, v in getattr(response, "headers", {}).items()}
        return data, headers
    return data


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


def _scan_issues(base_url: str, token: str, workspace_id: str, max_pages: int) -> MulticaRead:
    found: dict[str, dict[str, Any]] = {}
    offset, total = 0, None
    for _ in range(max_pages):
        data = _call("GET", base_url, token, "/api/issues", workspace_id=workspace_id,
            params={"limit": "100", "offset": str(offset), "sort": "created_at", "direction": "desc"})
        if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
            return MulticaRead(list(found.values()), False, reason="issue_page_contract_unknown")
        page_total = data.get("total")
        if not isinstance(page_total, int) or isinstance(page_total, bool) or page_total < 0:
            return MulticaRead(list(found.values()), False, reason="issue_total_missing")
        if total is not None and total != page_total:
            return MulticaRead(list(found.values()), False, reason="issue_snapshot_changed")
        total = page_total
        page = data["issues"]
        for item in page:
            if not isinstance(item, dict) or not item.get("id") or str(item["id"]) in found:
                return MulticaRead(list(found.values()), False, reason="issue_identity_or_boundary_changed")
            found[str(item["id"])] = item
        offset += len(page)
        if offset == total:
            return MulticaRead(list(found.values()), True, {"offset": offset})
        if not page or offset > total or len(page) < 100:
            return MulticaRead(list(found.values()), False, reason="issue_page_incomplete")
    return MulticaRead(list(found.values()), False, reason="issue_page_budget_exceeded")


def list_all_issues(
    base_url: str, token: str, workspace_id: str, *, max_pages: int = 1000,
) -> MulticaRead:
    """Verify two full reads with explicit immutable ordering.

    The pinned default is mutable position order, so request created_at DESC
    (the server adds id DESC). Offset pages have no snapshot token. A second
    full read must match identities and content, even when the total stays the
    same. Detected movement is incomplete, never inactivity evidence; the next
    sweep rereads from zero instead of advancing an updated_at watermark.
    """
    first = _scan_issues(base_url, token, workspace_id, max_pages)
    if not first.complete:
        return first
    verified = _scan_issues(base_url, token, workspace_id, max_pages)
    if not verified.complete:
        return verified
    if first.items != verified.items:
        return MulticaRead(verified.items, False, reason="issue_snapshot_changed")
    return MulticaRead(verified.items, True, {**verified.cursor, "strategy": "verified_full_reread"})


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
    priority: str | None = None,
) -> dict[str, Any]:
    """创建 issue；指派走 assignee_type=agent + assignee_id（服务端契约）。

    名称到 ID 的解析由调用方（multica_service）先经 match_agent 完成——
    直接传名称字段会被服务端静默忽略（真实环境 E2E 实测）。同名活跃任务
    默认被服务端 409 拒绝（duplicate 保护），确需重复创建时置 allow_duplicate。
    """
    payload: dict[str, Any] = {"title": title}
    if priority is not None:
        if priority not in {"none", "low", "medium", "high", "urgent"}:
            raise MulticaClientError("multica priority 无效")
        payload["priority"] = priority
    if description:
        payload["description"] = description
    if assignee_id:
        payload["assignee_type"] = "agent"
        payload["assignee_id"] = assignee_id.strip()
    if allow_duplicate:
        payload["allow_duplicate"] = True
    data = _call("POST", base_url, token, "/api/issues", workspace_id=workspace_id, payload=payload)
    if not isinstance(data, dict) or not (data.get("id") or data.get("identifier")):
        raise MulticaRequestError("multica 创建 Issue 未返回稳定 ID", outcome="unknown")
    return data


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
    """Read the Issue-authorized ``{tasks: [...]}`` response without selecting a run."""
    data = _call(
        "GET", base_url, token, f"{_issue_path(issue_ref)}/active-task",
        workspace_id=workspace_id,
    )
    if not isinstance(data, dict) or _stable_items(data, key="tasks") is None:
        raise MulticaClientError("multica 活动执行响应缺少稳定任务身份")
    return data


def list_task_runs(
    base_url: str,
    token: str,
    workspace_id: str,
    issue_ref: str,
) -> list[dict[str, Any]]:
    """Read this issue's full execution history, including completed original runs."""
    data = _call(
        "GET", base_url, token, f"{_issue_path(issue_ref)}/task-runs",
        workspace_id=workspace_id, params={"scope": "issue"},
    )
    items = _stable_items(data)
    if items is None or len({str(item["id"]) for item in items}) != len(items):
        raise MulticaClientError("multica 执行历史缺少唯一任务身份")
    return items


def _stable_items(data: Any, *, key: str | None = None) -> list[dict[str, Any]] | None:
    items = data.get(key) if isinstance(data, dict) and key else data
    if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("id") for item in items):
        return None
    return items


def list_comments(
    base_url: str, token: str, workspace_id: str, issue_ref: str, *, max_pages: int = 1000,
) -> MulticaRead:
    """Walk complete threads using both opaque RFC3339Nano cursor components.

    Full rereads deliberately cover late replies and comments with old timestamps;
    the independent evidence ledger deduplicates their stable IDs. No folding or
    summary projection is requested. A defensive server cap remains incomplete.
    """
    found: dict[str, dict[str, Any]] = {}
    params = {"recent": "100"}
    cursors: set[tuple[str, str]] = set()
    for _ in range(max_pages):
        data, headers = _call("GET", base_url, token, f"{_issue_path(issue_ref)}/comments",
            workspace_id=workspace_id, params=params, include_headers=True)
        page = _stable_items(data)
        if page is None:
            return MulticaRead(list(found.values()), False, reason="comment_identity_missing")
        found.update((str(item["id"]), item) for item in page)
        if headers.get("x-comments-truncated", "").lower() == "true":
            return MulticaRead(list(found.values()), False, reason="comment_server_truncated")
        before, before_id = headers.get("x-multica-next-before"), headers.get("x-multica-next-before-id")
        if not before and not before_id:
            return MulticaRead(list(found.values()), True,
                {"strategy": "full_thread_reread", "seenIds": sorted(found)})
        if not before or not before_id or (before, before_id) in cursors:
            return MulticaRead(list(found.values()), False, reason="comment_cursor_invalid")
        cursors.add((before, before_id))
        params = {"recent": "100", "before": before, "before_id": before_id}
    return MulticaRead(list(found.values()), False, reason="comment_page_budget_exceeded")


def list_task_messages(
    base_url: str, token: str, workspace_id: str, task_id: str,
) -> MulticaRead:
    """Owner-authorized full transcript; (task_id, seq) is the pinned stable ID.

    The pinned owner endpoint is unbounded, unlike daemon ingestion. Always
    reread so a late persisted row below a previous seq watermark is retained.
    """
    task = str(task_id or "").strip()
    if not task or len(task) > 255:
        raise MulticaClientError("multica task 标识无效")
    data = _call("GET", base_url, token, f"/api/tasks/{quote(task, safe='')}/messages",
        workspace_id=workspace_id)
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    if not isinstance(data, list):
        return MulticaRead([], False, reason="message_contract_unknown")
    for item in data:
        seq = item.get("seq") if isinstance(item, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0 or seq in seen or item.get("task_id") != task:
            return MulticaRead(items, False, reason="message_identity_invalid")
        seen.add(seq)
        items.append({**item, "id": f"{task}:{seq}"})
    return MulticaRead(items, True, {"taskId": task, "seq": max(seen, default=0), "strategy": "full_reread"})


def list_timeline(base_url: str, token: str, workspace_id: str, issue_ref: str) -> MulticaRead:
    """Pinned timeline has a defensive cap and no resumable cursor.

    A capped response cannot prove absence of activity and is never treated as
    a complete history. Reading comments separately cannot repair activity loss.
    """
    data, headers = _call("GET", base_url, token, f"{_issue_path(issue_ref)}/timeline",
        workspace_id=workspace_id, include_headers=True)
    page = _stable_items(data, key="entries" if isinstance(data, dict) else None)
    if page is None:
        return MulticaRead([], False, reason="timeline_identity_missing")
    # The pinned header contains "activity", "comment" or "activity,comment".
    truncated = headers.get("x-timeline-truncated", "").lower() not in ("", "false") or (
        isinstance(data, dict) and (data.get("has_more_before") is True or data.get("has_more_after") is True))
    return MulticaRead(page, not bool(truncated), {"strategy": "full_reread"},
        "timeline_server_truncated_no_cursor" if truncated else None)


def create_comment(
    base_url: str, token: str, workspace_id: str, issue_ref: str, *, content: str,
    steer_task_id: str | None = None,
) -> MulticaCommentResult:
    """Ordinary comment, with optional explicit steering of the currently active run.

    The source contract is pinned, deployment equivalence remains an external
    acceptance item. No suppress/type/daemon/Issue-state fields are exposed.
    """
    if not content.strip():
        raise MulticaClientError("multica 评论不能为空")
    payload: dict[str, Any] = {"content": content}
    if steer_task_id:
        try:
            UUID(steer_task_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise MulticaClientError("multica steer task 必须是 UUID") from exc
        active = get_active_task(base_url, token, workspace_id, issue_ref)
        runs = active["tasks"]
        if not any(str(run["id"]) == steer_task_id
                   and run.get("status") == "running" for run in runs):
            raise MulticaClientError("multica steer 目标已非当前有效运行")
        payload["steer_task_ids"] = [steer_task_id]
    data = _call("POST", base_url, token, f"{_issue_path(issue_ref)}/comments",
        workspace_id=workspace_id, payload=payload)
    if not isinstance(data, dict) or not data.get("id"):
        raise MulticaRequestError("multica 评论未返回稳定 ID", outcome="unknown")
    evidence = {key: data[key] for key in ("trigger_outcomes", "supplements", "supplement_task_id",
        "supplement_status", "supplement_failure_reason", "supplement_delivered_at") if key in data}
    # A queued trigger or accepted supplement proves admission, not execution.
    supplements = data.get("supplements")
    delivered = any(isinstance(item, dict) and item.get("status") == "delivered" and item.get("delivered_at")
        and (not steer_task_id or item.get("task_id") == steer_task_id)
        for item in (supplements if isinstance(supplements, list) else []))
    delivered = delivered or (data.get("supplement_status") == "delivered"
        and bool(data.get("supplement_delivered_at"))
        and (not steer_task_id or data.get("supplement_task_id") == steer_task_id))
    return MulticaCommentResult(str(data["id"]), data, "confirmed" if delivered else "unknown", evidence)


def verified_issue_url(base_url: str, issue: dict[str, Any]) -> str | None:
    """Only return a same-origin Issue URL explicitly supplied by the API.

    No deployment-verified synthesis pattern is configured, so an identifier
    alone deliberately produces no URL. Untrusted bodies are not a URL source.
    """
    base = urlsplit(normalize_base_url(base_url))
    identities = {str(issue[key]) for key in ("id", "identifier") if issue.get(key)}
    for key in ("url", "web_url", "html_url"):
        candidate = issue.get(key)
        if not isinstance(candidate, str) or any(ord(char) < 33 for char in candidate):
            continue
        try:
            value = urlsplit(candidate)
            if (value.scheme, value.hostname, value.port) != (base.scheme, base.hostname, base.port):
                continue
            path = unquote(value.path, errors="strict")
        except (ValueError, UnicodeDecodeError):
            continue
        if value.username or value.password or value.fragment or value.query or "\\" in path:
            continue
        prefix = base.path.rstrip("/") + "/"
        if not path.startswith(prefix):
            continue
        parts = path[len(prefix):].split("/")
        # Pinned web route: /[workspaceSlug]/issues/[id]. A same-origin
        # settings/API path, another Issue or an encoded traversal is not proof.
        if (len(parts) != 3 or parts[1] != "issues" or parts[2] not in identities
                or any(part in ("", ".", "..") or any(ord(char) < 33 for char in part) for part in parts)):
            continue
        return candidate
    return None


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
