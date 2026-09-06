"""远程助手服务 — 用户自配远程 agent 的声明式注册目录。

OpenOntology 远程助手 HTTP 契约 v1（单端点回合制）：

    POST {endpoint}
    Authorization: Bearer {token}        # 配置了 token 时携带
    {"message": "<task>", "session_ref": "<opaque|null>"}

    200 -> {"status": "answered"|"failed",
            "content": "<答复文本>",
            "session_ref": "<opaque|null>",   # 首回合由远端签发，之后原样回传
            "note": "<可选附注>"}

配置归 super_assistant 域（同 multica/MCP 的用户级外部能力先例）；经
assistant_hub 的动态 provider 钩子进入委派目录——新增/停用一个远程助手
只改配置行，委派引擎零改动。端点复用 MCP 同一 SSRF 校验（生产拒绝非
公网地址）；token 加密存储、永不回显。会话续用由委派表 conversation_ref
承载（ref 载荷即远端 session_ref），不经 LLM 上下文。
"""
from __future__ import annotations

import re
from typing import Any, Iterator, Optional

import httpx
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.assistant_hub import registry as assistant_registry
from app.assistant_hub.contract import (
    STATUS_ANSWERED,
    STATUS_FAILED,
    AssistantHubError,
    AssistantSpec,
    TurnResult,
    build_ref,
    parse_ref,
)
from app.shared.encryption import decrypt, encrypt
from app.super_assistant.mcp_client import McpClientError, validate_mcp_url
from app.super_assistant.models import SuperAssistantRemoteAgent
from app.super_assistant.schemas import (
    RemoteAgentCreate,
    RemoteAgentOut,
    RemoteAgentTestOut,
    RemoteAgentUpdate,
)

_KEY_RE = re.compile(r"^remote\.[a-z0-9][a-z0-9_-]{0,48}$")


class RemoteAgentServiceError(ValueError):
    """配置校验/冲突等可预期错误（HTTP 400/409 语义）。"""


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """httpx 调用收口（测试 monkeypatch 此函数）。"""
    return httpx.request(method, url, **kwargs)


# ---------------------------------------------------------------- 契约执行


class RemoteAgentAdapter:
    """把一条远程助手配置适配为 PlatformAssistant。"""

    def __init__(self, row: SuperAssistantRemoteAgent):
        self._endpoint = row.endpoint
        self._token = decrypt(row.token_encrypted or "") if row.token_encrypted else ""
        self._timeout = max(10, int(row.timeout_seconds or 120))
        self._key = row.key
        self._label = row.label
        self._description = row.description

    def spec(self) -> AssistantSpec:
        return AssistantSpec(
            key=self._key,
            label=self._label,
            description=f"{self._description}（远程助手，由远端服务执行）",
            menu_keys=(),
        )

    def start(self, db, user, *, context: dict[str, Any] | None = None) -> str:
        return build_ref(self._key, {"remote_session": None})

    def run_turn(
        self, db, user, conversation_ref: str, message: str, *,
        cancel_event=None,
    ) -> Iterator[TurnResult]:
        payload = parse_ref(self._key, conversation_ref)
        remote_session = payload.get("remote_session")
        try:
            validate_mcp_url(self._endpoint)
        except McpClientError as exc:
            raise AssistantHubError(f"远程助手端点被拒绝：{exc}") from exc

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = _request(
                "POST",
                self._endpoint,
                headers=headers,
                json={"message": message, "session_ref": remote_session},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            yield TurnResult(
                status=STATUS_FAILED,
                content=f"远程助手连接失败：{exc.__class__.__name__}",
                conversation_ref=conversation_ref,
            )
            return
        if response.status_code != 200:
            yield TurnResult(
                status=STATUS_FAILED,
                content=f"远程助手返回 HTTP {response.status_code}",
                conversation_ref=conversation_ref,
            )
            return
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("响应不是 JSON 对象")
        except ValueError as exc:
            yield TurnResult(
                status=STATUS_FAILED,
                content=f"远程助手响应无法解析：{exc}",
                conversation_ref=conversation_ref,
            )
            return

        new_session = body.get("session_ref")
        final_ref = build_ref(self._key, {"remote_session": new_session}) if new_session else conversation_ref
        status = STATUS_ANSWERED if body.get("status") == "answered" else STATUS_FAILED
        yield TurnResult(
            status=status,
            content=str(body.get("content") or "（远程助手未返回内容）"),
            conversation_ref=final_ref,
            created_new_conversation=remote_session is None and bool(new_session),
            note=str(body.get("note") or ""),
        )


# ------------------------------------------------------------ 动态目录接线


def dynamic_assistants(db: Session, user) -> list[Any]:
    """assistant_hub 动态目录 provider：返回该用户已启用的远程助手适配器。

    配置表缺失（隔离测试库只建部分表）时目录退化为空——动态目录是可选
    增强，生产库迁移 0099 后必有表。
    """
    try:
        rows = (
            db.query(SuperAssistantRemoteAgent)
            .filter(
                SuperAssistantRemoteAgent.owner_id == user.id,
                SuperAssistantRemoteAgent.enabled.is_(True),
            )
            .order_by(SuperAssistantRemoteAgent.created_at.asc())
            .all()
        )
    except OperationalError:
        return []
    return [RemoteAgentAdapter(row) for row in rows]


assistant_registry.register_dynamic_provider(dynamic_assistants)


# ------------------------------------------------------------------- CRUD


def _agent_out(row: SuperAssistantRemoteAgent) -> RemoteAgentOut:
    return RemoteAgentOut(
        id=row.id,
        key=row.key,
        label=row.label,
        description=row.description,
        endpoint=row.endpoint,
        token_set=bool(row.token_encrypted),
        enabled=row.enabled,
        timeout_seconds=row.timeout_seconds,
    )


def list_agents(db: Session, owner_id: str) -> list[RemoteAgentOut]:
    rows = (
        db.query(SuperAssistantRemoteAgent)
        .filter(SuperAssistantRemoteAgent.owner_id == owner_id)
        .order_by(SuperAssistantRemoteAgent.created_at.asc())
        .all()
    )
    return [_agent_out(row) for row in rows]


def _validate_key(key: str) -> str:
    value = (key or "").strip()
    if not _KEY_RE.match(value):
        raise RemoteAgentServiceError(
            "key 必须以 remote. 开头，后接小写字母/数字/-/_（≤49 字符）"
        )
    return value


def _validate_endpoint(endpoint: str) -> str:
    value = (endpoint or "").strip()
    try:
        validate_mcp_url(value)
    except McpClientError as exc:
        raise RemoteAgentServiceError(str(exc)) from exc
    return value


def _require_row(db: Session, owner_id: str, agent_id: str) -> SuperAssistantRemoteAgent:
    row = db.query(SuperAssistantRemoteAgent).filter(
        SuperAssistantRemoteAgent.id == agent_id,
        SuperAssistantRemoteAgent.owner_id == owner_id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="远程助手不存在")
    return row


def create_agent(db: Session, owner_id: str, body: RemoteAgentCreate) -> RemoteAgentOut:
    key = _validate_key(body.key)
    endpoint = _validate_endpoint(body.endpoint)
    label = (body.label or "").strip()
    if not label:
        raise RemoteAgentServiceError("名称不能为空")
    duplicate = db.query(SuperAssistantRemoteAgent).filter(
        SuperAssistantRemoteAgent.owner_id == owner_id,
        SuperAssistantRemoteAgent.key == key,
    ).first()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"key {key} 已存在")
    row = SuperAssistantRemoteAgent(
        owner_id=owner_id,
        key=key,
        label=label[:100],
        description=(body.description or "").strip()[:2000],
        endpoint=endpoint,
        token_encrypted=encrypt(body.token) if body.token else None,
        enabled=body.enabled,
        timeout_seconds=min(600, max(10, int(body.timeout_seconds or 120))),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _agent_out(row)


def update_agent(
    db: Session, owner_id: str, agent_id: str, body: RemoteAgentUpdate,
) -> RemoteAgentOut:
    row = _require_row(db, owner_id, agent_id)
    if body.label is not None:
        label = body.label.strip()
        if not label:
            raise RemoteAgentServiceError("名称不能为空")
        row.label = label[:100]
    if body.description is not None:
        row.description = body.description.strip()[:2000]
    if body.endpoint is not None:
        row.endpoint = _validate_endpoint(body.endpoint)
    if body.token is not None and body.token.strip():
        # token 留空/缺省 = 保留已保存凭据（同 multica 惯例，不回显不覆盖）
        row.token_encrypted = encrypt(body.token.strip())
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.timeout_seconds is not None:
        row.timeout_seconds = min(600, max(10, int(body.timeout_seconds)))
    db.commit()
    db.refresh(row)
    return _agent_out(row)


def delete_agent(db: Session, owner_id: str, agent_id: str) -> None:
    row = _require_row(db, owner_id, agent_id)
    db.delete(row)
    db.commit()


def test_agent(db: Session, owner_id: str, agent_id: str) -> RemoteAgentTestOut:
    row = _require_row(db, owner_id, agent_id)
    adapter = RemoteAgentAdapter(row)
    result: Optional[TurnResult] = None
    for item in adapter.run_turn(
        db, None, adapter.start(db, None), "ping：请回复 pong 以确认连通",
    ):
        if isinstance(item, TurnResult):
            result = item
    assert result is not None
    return RemoteAgentTestOut(
        ok=result.status == STATUS_ANSWERED,
        message=result.content[:500],
    )
