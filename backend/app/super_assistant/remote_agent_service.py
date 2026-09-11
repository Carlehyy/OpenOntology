"""远程助手服务 — 远程 agent 的注册目录与双传输回合执行。

OpenOntology 远程助手 HTTP 契约 v1（单端点回合制，直连模式）：

    POST {endpoint}
    Authorization: Bearer {token}        # 配置了 token 时携带
    {"message": "<task>", "session_ref": "<opaque|null>"}

    200 -> {"status": "answered"|"failed",
            "content": "<答复文本>",
            "session_ref": "<opaque|null>",   # 首回合由远端签发，之后原样回传
            "note": "<可选附注>"}

回连模式（pull，NAT/防火墙后远端）：平台不发起外呼。远端凭 agent key
（sha256 哈希落库）长轮询 `GET /tasks/next` 领任务（契约载荷同上），
完成后 `POST /tasks/{id}/result` 回传（响应字段同上）。

配置归 super_assistant 域（同 multica/MCP 的用户级外部能力先例）；经
assistant_hub 的动态 provider 钩子进入委派目录——新增/停用一个远程助手
只改配置行，委派引擎零改动（两种模式对委派引擎同构，仅本模块内分支）。
直连端点复用 MCP 同一 SSRF 校验（生产拒绝非公网地址）；token 加密存储、
永不回显。会话续用由委派表 conversation_ref 承载（ref 载荷即远端
session_ref），不经 LLM 上下文。

RAP 版本化规则（本文件即协议规范单一事实源）：
- minor 演进只加可选字段，不改动既有字段语义；major 演进改变语义时
  以新的 rap_version 并列，旧版本行为保持不变；
- 平台永远兼容 rap_version=1；
- 版本在兑换注册时由远端声明、平台确认并冻结在助手行上，之后每个
  任务载荷同值携带，远端按该版本解释契约；
- 架构不变量：委派引擎与传输解耦、协议只增不改、凭证哈希落库且吊销
  即停用/删除一行、远端内容按不可信数据处理（封顶+来源标记）、直连
  外呼必过 SSRF 校验、任务认领条件 UPDATE 幂等、公开端点必有
  门禁+限流+防枚举口径。
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
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
from app.super_assistant.models import (
    SuperAssistantRemoteAgent,
    SuperAssistantRemoteAgentTask,
)
from app.super_assistant.schemas import (
    RemoteAgentCreate,
    RemoteAgentOut,
    RemoteAgentTaskResultIn,
    RemoteAgentTestOut,
    RemoteAgentUpdate,
)

# 总长上限 49（含 remote. 前缀），与 key 列 String(50) 对齐
_KEY_RE = re.compile(r"^remote\.[a-z0-9][a-z0-9_-]{0,41}$")
# 平台当前支持的 RAP 协议版本（兑换协商时校验；1 = 本文件文档的契约）
SUPPORTED_RAP_VERSIONS = (1,)
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# 回连等待轮询节奏与结果宽限（测试可 monkeypatch 缩短）
_TASK_POLL_INTERVAL = 0.4
_TASK_RESULT_GRACE_SECONDS = 5.0
# 「测试」按钮的回合超时上限：离线回连助手快速失败而非挂满 timeout_seconds
_TEST_TURN_TIMEOUT_SECONDS = 20


def _utcnow() -> datetime:
    # 朴素 UTC：SQLite DateTime 读回无时区，比较口径须一致
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_secret(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RemoteAgentServiceError(ValueError):
    """配置校验/冲突等可预期错误（HTTP 400/409 语义）。"""


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """httpx 调用收口（测试 monkeypatch 此函数）。"""
    return httpx.request(method, url, **kwargs)


# ---------------------------------------------------------------- 契约执行


class RemoteAgentAdapter:
    """把一条远程助手配置适配为 PlatformAssistant（直连/回连同构）。"""

    def __init__(self, row: SuperAssistantRemoteAgent, *, timeout_seconds: int | None = None):
        self._endpoint = row.endpoint
        # 密钥轮换/坏密文时退化为无凭据调用（远端将拒绝），不毒化整个委派目录
        # （先例：mcp_client 对 decrypt 的兜底口径）
        try:
            self._token = decrypt(row.token_encrypted or "") if row.token_encrypted else ""
        except Exception:  # noqa: BLE001 — InvalidToken 等一律降级，不阻断目录构建
            self._token = ""
        if timeout_seconds is not None:
            # 显式覆写（如「测试」按钮的快速失败上限）允许低于常规下限
            self._timeout = max(1, int(timeout_seconds or 120))
        else:
            self._timeout = max(10, int(row.timeout_seconds or 120))
        self._key = row.key
        self._label = row.label
        self._description = row.description
        self._mode = row.mode or "direct"
        self._agent_id = row.id

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
        touch_last_turn(db, self._agent_id)
        payload = parse_ref(self._key, conversation_ref)
        remote_session = payload.get("remote_session")
        if self._mode == "pull":
            yield from self._run_turn_pull(
                db, conversation_ref, message, remote_session, cancel_event=cancel_event,
            )
            return
        yield from self._run_turn_direct(conversation_ref, message, remote_session)

    # ------------------------------------------------------------- 直连模式

    def _run_turn_direct(
        self, conversation_ref: str, message: str, remote_session: str | None,
    ) -> Iterator[TurnResult]:
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
        if isinstance(new_session, str) and len(new_session) > 255:
            new_session = new_session[:255]  # 与任务表列宽对齐，防 conversation_ref 溢出
        final_ref = build_ref(self._key, {"remote_session": new_session}) if new_session else conversation_ref
        status = STATUS_ANSWERED if body.get("status") == "answered" else STATUS_FAILED
        yield TurnResult(
            status=status,
            content=str(body.get("content") or "（远程助手未返回内容）"),
            conversation_ref=final_ref,
            created_new_conversation=remote_session is None and bool(new_session),
            note=str(body.get("note") or "")[:2000],
        )

    # ------------------------------------------------------------- 回连模式

    def _run_turn_pull(
        self, db: Session, conversation_ref: str, message: str, remote_session: str | None,
        *, cancel_event=None,
    ) -> Iterator[TurnResult]:
        task = enqueue_task(db, self._agent_id, message, remote_session, self._timeout)
        deadline = time.monotonic() + self._timeout + _TASK_RESULT_GRACE_SECONDS
        while time.monotonic() < deadline:
            time.sleep(_TASK_POLL_INTERVAL)
            if cancel_event is not None and cancel_event.is_set():
                expire_task(db, task.id)
                yield TurnResult(
                    status=STATUS_FAILED,
                    content="远程助手任务已被取消",
                    conversation_ref=conversation_ref,
                )
                return
            # 结束当前事务再读：确保能看到远端经其它会话提交的结果
            db.commit()
            row = (
                db.query(SuperAssistantRemoteAgentTask)
                .filter(SuperAssistantRemoteAgentTask.id == task.id)
                .first()
            )
            if row is None:
                continue
            if row.status == "done":
                new_session = row.result_session_ref
                final_ref = (
                    build_ref(self._key, {"remote_session": new_session})
                    if new_session else conversation_ref
                )
                status = STATUS_ANSWERED if row.result_status == "answered" else STATUS_FAILED
                yield TurnResult(
                    status=status,
                    content=str(row.result_content or "（远程助手未返回内容）"),
                    conversation_ref=final_ref,
                    created_new_conversation=remote_session is None and bool(new_session),
                    note=str(row.result_note or ""),
                )
                return
            if row.status == "expired":
                break
        expire_task(db, task.id)
        yield TurnResult(
            status=STATUS_FAILED,
            content="远程助手未在超时窗口内领走/回传任务（可能离线）",
            conversation_ref=conversation_ref,
        )


# ----------------------------------------------------------- 回连任务队列


def enqueue_task(
    db: Session, agent_id: str, message: str, session_ref: str | None, timeout_seconds: int,
) -> SuperAssistantRemoteAgentTask:
    row = SuperAssistantRemoteAgentTask(
        agent_id=agent_id,
        status="pending",
        message=message,
        session_ref=session_ref,
        expires_at=_utcnow() + timedelta(seconds=timeout_seconds),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def claim_next_task(db: Session, agent_id: str) -> Optional[SuperAssistantRemoteAgentTask]:
    """认领该 agent 最早一条未过期任务；条件 UPDATE 保证并发下唯一认领者。"""
    now = _utcnow()
    candidates = (
        db.query(SuperAssistantRemoteAgentTask)
        .filter(
            SuperAssistantRemoteAgentTask.agent_id == agent_id,
            SuperAssistantRemoteAgentTask.status == "pending",
            SuperAssistantRemoteAgentTask.expires_at > now,
        )
        .order_by(SuperAssistantRemoteAgentTask.created_at.asc())
        .limit(5)
        .all()
    )
    for candidate in candidates:
        changed = (
            db.query(SuperAssistantRemoteAgentTask)
            .filter(
                SuperAssistantRemoteAgentTask.id == candidate.id,
                SuperAssistantRemoteAgentTask.status == "pending",
            )
            .update({"status": "claimed", "claimed_at": now}, synchronize_session=False)
        )
        db.commit()
        if changed:
            # 重查代替 refresh：认领与响应之间行被级联删除时返回 None 而非 500
            return (
                db.query(SuperAssistantRemoteAgentTask)
                .filter(SuperAssistantRemoteAgentTask.id == candidate.id)
                .first()
            )
    return None


def submit_task_result(
    db: Session, agent_id: str, task_id: str, body: RemoteAgentTaskResultIn,
) -> SuperAssistantRemoteAgentTask:
    row = db.query(SuperAssistantRemoteAgentTask).filter(
        SuperAssistantRemoteAgentTask.id == task_id,
        SuperAssistantRemoteAgentTask.agent_id == agent_id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row.status == "done":
        raise HTTPException(status_code=409, detail="任务已完成")
    if row.status == "expired":
        raise HTTPException(status_code=409, detail="任务已超时失效")
    row.status = "done"
    row.result_status = "answered" if body.status == "answered" else "failed"
    row.result_content = body.content[:20000]
    row.result_session_ref = (body.session_ref or None) if len(body.session_ref or "") <= 255 else None
    row.result_note = body.note[:2000]
    row.completed_at = _utcnow()
    db.commit()
    db.refresh(row)
    return row


def expire_task(db: Session, task_id: str) -> None:
    changed = (
        db.query(SuperAssistantRemoteAgentTask)
        .filter(
            SuperAssistantRemoteAgentTask.id == task_id,
            SuperAssistantRemoteAgentTask.status.in_(("pending", "claimed")),
        )
        .update({"status": "expired"}, synchronize_session=False)
    )
    if changed:
        db.commit()


def touch_agent(db: Session, agent: SuperAssistantRemoteAgent) -> None:
    """回连端点心跳：刷新在线状态展示（失败不阻断轮询主流程）。"""
    try:
        agent.last_seen_at = _utcnow()
        db.commit()
    except OperationalError:
        db.rollback()


def touch_last_turn(db: Session, agent_id: str) -> None:
    """委派回合开始时刷新 last_turn_at（直连模式的活动信号；失败不阻断）。"""
    try:
        db.query(SuperAssistantRemoteAgent).filter(
            SuperAssistantRemoteAgent.id == agent_id,
        ).update({"last_turn_at": _utcnow()}, synchronize_session=False)
        db.commit()
    except OperationalError:
        db.rollback()


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
        mode=row.mode or "direct",
        last_seen_at=row.last_seen_at,
        last_turn_at=row.last_turn_at,
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
            "key 必须以 remote. 开头，后接小写字母/数字/-/_（总长 ≤49 字符）"
        )
    return value


def _slugify(label: str) -> str:
    slug = _SLUG_RE.sub("-", (label or "").strip().lower()).strip("-")
    return (slug or "agent")[:32].strip("-")


def _generate_key(db: Session, owner_id: str, label: str) -> str:
    """按名称生成 remote.<slug> key，冲突时追加 -2/-3… 序号。"""
    slug = _slugify(label)
    for index in range(0, 98):
        suffix = "" if index == 0 else f"-{index + 1}"
        candidate = f"remote.{slug}{suffix}"[:50].rstrip("-")
        exists = db.query(SuperAssistantRemoteAgent).filter(
            SuperAssistantRemoteAgent.owner_id == owner_id,
            SuperAssistantRemoteAgent.key == candidate,
        ).first()
        if exists is None:
            return candidate
    raise RemoteAgentServiceError("无法生成唯一 key，请手动指定")


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


def create_agent_row(
    db: Session, owner_id: str, body: RemoteAgentCreate, *, rap_version: int = 1,
) -> tuple[SuperAssistantRemoteAgent, str | None]:
    """落一行远程助手配置；回连模式额外返回一次性发放的 agent key 原文。"""
    if rap_version not in SUPPORTED_RAP_VERSIONS:
        raise RemoteAgentServiceError(
            f"暂不支持 RAP 协议版本 {rap_version}（当前支持 {list(SUPPORTED_RAP_VERSIONS)}）"
        )
    mode = (body.mode or "direct").strip().lower()
    if mode not in ("direct", "pull"):
        raise RemoteAgentServiceError("mode 只支持 direct（直连）或 pull（回连）")
    label = (body.label or "").strip()
    if not label:
        raise RemoteAgentServiceError("名称不能为空")
    raw_key = (body.key or "").strip()
    key = _validate_key(raw_key) if raw_key and raw_key != "remote." else _generate_key(db, owner_id, label)
    if mode == "direct":
        endpoint = _validate_endpoint(body.endpoint or "")
    else:
        endpoint = ""
    duplicate = db.query(SuperAssistantRemoteAgent).filter(
        SuperAssistantRemoteAgent.owner_id == owner_id,
        SuperAssistantRemoteAgent.key == key,
    ).first()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"key {key} 已存在")
    raw_agent_key: str | None = None
    agent_key_hash: str | None = None
    if mode == "pull":
        raw_agent_key = f"rak_{secrets.token_urlsafe(32)}"
        agent_key_hash = hash_secret(raw_agent_key)
    row = SuperAssistantRemoteAgent(
        owner_id=owner_id,
        key=key,
        label=label[:100],
        description=(body.description or "").strip()[:2000],
        endpoint=endpoint,
        token_encrypted=encrypt(body.token) if (mode == "direct" and body.token) else None,
        enabled=body.enabled,
        timeout_seconds=min(600, max(10, int(body.timeout_seconds or 120))),
        mode=mode,
        agent_key_hash=agent_key_hash,
        rap_version=rap_version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, raw_agent_key


def create_agent(db: Session, owner_id: str, body: RemoteAgentCreate) -> RemoteAgentOut:
    row, _ = create_agent_row(db, owner_id, body)
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
    adapter = RemoteAgentAdapter(
        row, timeout_seconds=min(int(row.timeout_seconds or 120), _TEST_TURN_TIMEOUT_SECONDS),
    )
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
