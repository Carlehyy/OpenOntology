"""远程助手公开端点 — 兑换邀请与回连模式任务轮询（无会话鉴权）。

挂载于 /api/public/super-assistant/remote-agents（先例：manual-dataset
公开分享）。两类门禁：
- 兑换：一次性邀请令牌（sha256 哈希查表，未知/过期/撤销同 404 防枚举）；
- 任务：回连 agent key（Bearer，sha256 哈希查表；停用/删除即吊销）。

长轮询最多挂起 30 秒（wait 参数截断），空窗返回 204——远端 agent 保持
「轮询-处理-回传」循环即可，平台全程不发起外呼。

资源口径（对抗审查修订）：长轮询为 async 端点，等待期既不占数据库
连接也不占同步线程池——认领用短会话（SessionLocal 上下文，毫秒级
借还），睡眠在事件循环上完成；请求级会话只做鉴权与心跳，提交后连接
即归还连接池（pool_size=5 + overflow=10 不被轮询者占满）。

限流口径（对抗审查修订）：任务端点在鉴权后按 agent 计数（120/分钟，
正常轮询 ≈2-4/分钟）——按 IP 计数在反向代理后是全体共享的单一桶，
会把 NAT 后的合法 agent 与攻击者一起掐死；未鉴权洪水只换来廉价的
401，不单独设 IP 桶。兑换仍按 IP 30/分钟（防邀请码爆破，代理后为
全局桶，兑换本身是低频操作，可接受）。进程内滑动窗口，多实例部署
各自计数，作为公网滥用底线而非精确配额。
"""
from __future__ import annotations

import asyncio
import threading
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.deps import get_db
from app.shared.database import SessionLocal
from app.super_assistant import remote_agent_invite_service, remote_agent_service
from app.super_assistant.models import SuperAssistantRemoteAgent
from app.super_assistant.remote_agent_service import hash_secret
from app.super_assistant.schemas import (
    RemoteAgentRedeemIn,
    RemoteAgentRedeemOut,
    RemoteAgentTaskNextOut,
    RemoteAgentTaskResultIn,
)

router = APIRouter()

_MAX_WAIT_SECONDS = 30
_POLL_INTERVAL = 0.4


class _WindowLimiter:
    """进程内每分钟计数（公开面防滥用底线）。"""

    def __init__(self, limit: int):
        self._limit = limit
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [ts for ts in self._hits.get(key, []) if now - ts < 60.0]
            if len(hits) >= self._limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 4096:  # 粗粒度防内存膨胀
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60.0}
            return True


_redeem_limiter = _WindowLimiter(limit=30)
_agent_limiter = _WindowLimiter(limit=120)


def _try_claim(agent_id: str):
    """短会话认领：连接毫秒级借还，长轮询等待期不占连接池。"""
    with SessionLocal() as poll_db:
        return remote_agent_service.claim_next_task(poll_db, agent_id)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _agent_from_bearer(
    db: Session, authorization: str | None,
) -> SuperAssistantRemoteAgent:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 agent key（Authorization: Bearer rak_…）")
    key = authorization.split(" ", 1)[1].strip()
    agent = db.query(SuperAssistantRemoteAgent).filter(
        SuperAssistantRemoteAgent.agent_key_hash == hash_secret(key),
    ).first()
    if agent is None or not agent.enabled:
        raise HTTPException(status_code=401, detail="agent key 无效或助手已停用")
    return agent


@router.post("/invite-redeem", response_model=RemoteAgentRedeemOut)
def redeem_invite(
    body: RemoteAgentRedeemIn,
    request: Request,
    db: Session = Depends(get_db),
) -> RemoteAgentRedeemOut:
    if not _redeem_limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    return remote_agent_invite_service.redeem(db, body)


@router.get("/tasks/next", response_model=RemoteAgentTaskNextOut)
async def next_task(
    wait: int = Query(default=25, ge=0, le=_MAX_WAIT_SECONDS),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> RemoteAgentTaskNextOut | Response:
    agent = await run_in_threadpool(_agent_from_bearer, db, authorization)
    if not _agent_limiter.allow(f"agent:{agent.id}"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    await run_in_threadpool(remote_agent_service.touch_agent, db, agent)
    deadline = time.monotonic() + min(wait, _MAX_WAIT_SECONDS)
    while True:
        task = await run_in_threadpool(_try_claim, agent.id)
        if task is not None:
            return RemoteAgentTaskNextOut(
                task_id=task.id,
                message=task.message,
                session_ref=task.session_ref,
                timeout_seconds=agent.timeout_seconds,
                rap_version=agent.rap_version or 1,
            )
        if time.monotonic() >= deadline:
            return Response(status_code=204)
        await asyncio.sleep(_POLL_INTERVAL)


@router.post("/tasks/{task_id}/result")
def submit_result(
    task_id: str,
    body: RemoteAgentTaskResultIn,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    agent = _agent_from_bearer(db, authorization)
    if not _agent_limiter.allow(f"agent:{agent.id}"):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    remote_agent_service.touch_agent(db, agent)
    remote_agent_service.submit_task_result(db, agent.id, task_id, body)
    return {"ok": True}
