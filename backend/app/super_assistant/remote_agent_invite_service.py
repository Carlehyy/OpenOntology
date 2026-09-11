"""远程助手接入邀请服务 — 一次性令牌 + 远端自助注册（paperclip 式）。

流程：属主在弹窗「邀请 AI 助手接入」→ 平台生成一次性邀请令牌（24h
TTL、可撤销）与「邀请函」（人类与 agent 双可读的接入指引，双模式）→
属主把邀请函粘贴给远端 agent → agent 自判网络选直连/回连，凭令牌调
公开兑换端点完成注册 → 助手直接进入属主委派目录。

安全口径（对齐 manual-dataset 分享先例）：
- 令牌 `rai_` + token_urlsafe(32)；sha256 哈希存储供兑换端点查表；
- Fernet 加密备份：仅用于「待使用」期间重发展示邀请函；
- 未知/过期/撤销同响应（404）防枚举；已消费单独 409；
- 一次性消费走条件 UPDATE（乐观并发），竞态下只有一方能注册；
- 兑换复用 create_agent 的全部校验（key/端点 SSRF/长度），落地行归
  邀请属主——邀请即授权，不设二次审批。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.shared.config import settings
from app.shared.encryption import decrypt, encrypt
from app.super_assistant.models import (
    SuperAssistantRemoteAgent,
    SuperAssistantRemoteAgentInvite,
)
from app.super_assistant.remote_agent_service import (
    RemoteAgentServiceError,
    create_agent_row,
    hash_secret,
)
from app.super_assistant.schemas import (
    RemoteAgentCreate,
    RemoteAgentInviteCreatedOut,
    RemoteAgentInviteOut,
    RemoteAgentRedeemIn,
    RemoteAgentRedeemOut,
)

INVITE_TTL = timedelta(hours=24)


def _utcnow() -> datetime:
    # 朴素 UTC：SQLite DateTime 读回无时区，比较口径须一致
    return datetime.now(timezone.utc).replace(tzinfo=None)


def invite_status(row: SuperAssistantRemoteAgentInvite, now: datetime | None = None) -> str:
    at = now or _utcnow()
    if row.revoked_at is not None:
        return "revoked"
    if row.consumed_at is not None:
        return "used"
    if row.expires_at <= at:
        return "expired"
    return "pending"


def _invite_out(
    db: Session, row: SuperAssistantRemoteAgentInvite,
) -> RemoteAgentInviteOut:
    agent_key = agent_label = None
    if row.redeemed_agent_id:
        agent = db.query(SuperAssistantRemoteAgent).filter(
            SuperAssistantRemoteAgent.id == row.redeemed_agent_id,
        ).first()
        if agent is not None:
            agent_key, agent_label = agent.key, agent.label
    return RemoteAgentInviteOut(
        id=row.id,
        status=invite_status(row),
        created_at=row.created_at,
        expires_at=row.expires_at,
        redeemed_agent_key=agent_key,
        redeemed_agent_label=agent_label,
    )


MAX_PENDING_INVITES = 5


def create_invite(db: Session, owner_id: str) -> RemoteAgentInviteCreatedOut:
    pending = (
        db.query(SuperAssistantRemoteAgentInvite)
        .filter(
            SuperAssistantRemoteAgentInvite.owner_id == owner_id,
            SuperAssistantRemoteAgentInvite.consumed_at.is_(None),
            SuperAssistantRemoteAgentInvite.revoked_at.is_(None),
            SuperAssistantRemoteAgentInvite.expires_at > _utcnow(),
        )
        .count()
    )
    if pending >= MAX_PENDING_INVITES:
        raise RemoteAgentServiceError(
            f"待使用邀请最多 {MAX_PENDING_INVITES} 个，请先撤销或等待过期"
        )
    token = f"rai_{secrets.token_urlsafe(32)}"
    row = SuperAssistantRemoteAgentInvite(
        owner_id=owner_id,
        token_hash=hash_secret(token),
        token_encrypted=encrypt(token),
        expires_at=_utcnow() + INVITE_TTL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return RemoteAgentInviteCreatedOut(
        id=row.id,
        status="pending",
        expires_at=row.expires_at,
        prompt_text=build_invite_prompt(_public_base_url(), token),
    )


def list_invites(db: Session, owner_id: str) -> list[RemoteAgentInviteOut]:
    rows = (
        db.query(SuperAssistantRemoteAgentInvite)
        .filter(SuperAssistantRemoteAgentInvite.owner_id == owner_id)
        .order_by(SuperAssistantRemoteAgentInvite.created_at.desc())
        .limit(50)
        .all()
    )
    return [_invite_out(db, row) for row in rows]


def invite_prompt(db: Session, owner_id: str, invite_id: str) -> str:
    """待使用邀请的邀请函重发（令牌可从加密备份恢复）；其余状态 404。"""
    row = db.query(SuperAssistantRemoteAgentInvite).filter(
        SuperAssistantRemoteAgentInvite.id == invite_id,
        SuperAssistantRemoteAgentInvite.owner_id == owner_id,
    ).first()
    if row is None or not row.token_encrypted or invite_status(row) != "pending":
        raise HTTPException(status_code=404, detail="邀请不存在或已不可用")
    return build_invite_prompt(_public_base_url(), decrypt(row.token_encrypted))


def revoke_invite(db: Session, owner_id: str, invite_id: str) -> None:
    row = db.query(SuperAssistantRemoteAgentInvite).filter(
        SuperAssistantRemoteAgentInvite.id == invite_id,
        SuperAssistantRemoteAgentInvite.owner_id == owner_id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="邀请不存在")
    if row.revoked_at is None and row.consumed_at is None:
        row.revoked_at = _utcnow()
        db.commit()


def redeem(db: Session, body: RemoteAgentRedeemIn) -> RemoteAgentRedeemOut:
    """凭一次性邀请令牌自助注册（公开端点，无会话鉴权）。"""
    row = db.query(SuperAssistantRemoteAgentInvite).filter(
        SuperAssistantRemoteAgentInvite.token_hash == hash_secret(body.invite_token.strip()),
    ).first()
    # 已消费（令牌只可能泄露给持有完整串的一方）单独 409 提示勿重试；
    # 未知/过期/撤销同响应 404，防令牌枚举
    if row is None:
        raise HTTPException(status_code=404, detail="邀请码无效或已过期")
    if row.consumed_at is not None:
        raise HTTPException(status_code=409, detail="邀请码已被使用")
    if invite_status(row) != "pending":
        raise HTTPException(status_code=404, detail="邀请码无效或已过期")
    changed = (
        db.query(SuperAssistantRemoteAgentInvite)
        .filter(
            SuperAssistantRemoteAgentInvite.id == row.id,
            SuperAssistantRemoteAgentInvite.consumed_at.is_(None),
            SuperAssistantRemoteAgentInvite.revoked_at.is_(None),
        )
        .update({"consumed_at": _utcnow()}, synchronize_session=False)
    )
    db.commit()
    if not changed:
        raise HTTPException(status_code=409, detail="邀请码已被使用")
    try:
        agent, raw_agent_key = create_agent_row(
            db, row.owner_id,
            RemoteAgentCreate(
                key=body.key,
                label=body.label,
                description=body.description,
                endpoint=body.endpoint,
                token=body.token,
                timeout_seconds=body.timeout_seconds,
                mode=body.mode,
            ),
            rap_version=body.rap_version,
        )
    except RemoteAgentServiceError as exc:
        # 注册参数不合法：邀请已被消费，属主可重新发一封（不回滚消费，
        # 避免同一令牌被反复试探不同参数）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    row.redeemed_agent_id = agent.id
    db.commit()
    return RemoteAgentRedeemOut(
        mode=agent.mode,
        key=agent.key,
        label=agent.label,
        rap_version=agent.rap_version,
        agent_key=raw_agent_key,
    )


def _public_base_url() -> str:
    return (settings.super_assistant_public_api_base_url or "").rstrip("/")


# ------------------------------------------------------------- 邀请函生成


def build_invite_prompt(base_url: str, invite_token: str) -> str:
    """人类与 agent 双可读的接入指引（单一事实源：后端生成，前端只展示）。"""
    return f"""你是即将接入 OpenOntology 平台「超级助手」的外部 AI 助手。请阅读本函并自行完成接入：
接入成功后，平台的超级助手会把适合你的任务委派给你，你完成任务并回传结果。

【第一步：判断你的网络，二选一】
- 平台服务器能直接访问你的 HTTP 端点（你在公网，或与平台同在内网）→ 选「方式 A：直连」；
- 你在局域网 / NAT 后，平台访问不到你，但你能访问公网 → 选「方式 B：回连」（无需暴露任何端口）。

【第二步：按所选方式接入】

■ 方式 A：直连
1. 对外暴露一个 HTTP POST 端点（HTTPS 优先），实现回合契约：
   请求头：Authorization: Bearer <你自己设定的密钥>（可选）
   请求体：{{"message": "<任务文本>", "session_ref": null}}（首轮为 null）
   响应 200 JSON：{{"status": "answered", "content": "<答复文本>",
                    "session_ref": "<你签发的会话引用，首轮返回>", "note": "<可选>"}}
   任务失败时 status 用 "failed"。session_ref 由你签发、平台之后原样带回，用于多轮续聊。
2. 调用注册接口（示例）：
   curl -X POST '{base_url}/api/public/super-assistant/remote-agents/invite-redeem' \\
     -H 'Content-Type: application/json' \\
     -d '@register.json'   # 请求体见下

■ 方式 B：回连（内网/NAT 后选这个）
1. 先用 mode="pull" 调用同一个注册接口。成功响应会返回 agent_key（形如 rak_…），请永久保存。
2. 之后持续循环长轮询领任务（无任务时最多等 25 秒返回 204，稍歇再轮询）：
   curl '{base_url}/api/public/super-assistant/remote-agents/tasks/next?wait=25' \\
     -H 'Authorization: Bearer <你的 agent_key>'
   有任务返回 200：{{"task_id": "…", "message": "<任务文本>", "session_ref": null, "timeout_seconds": 120}}
3. 处理任务，完成后回传（字段含义与方式 A 的响应一致）：
   curl -X POST '{base_url}/api/public/super-assistant/remote-agents/tasks/<task_id>/result' \\
     -H 'Authorization: Bearer <你的 agent_key>' -H 'Content-Type: application/json' \\
     -d '{{"status": "answered", "content": "<答复文本>", "session_ref": "<会话引用或null>", "note": ""}}'

【注册请求体 register.json】（两种方式通用，按需删减）
{{
  "invite_token": "{invite_token}",
  "mode": "direct 或 pull",
  "label": "<你的名字，例如：客服知识库助手>",
  "description": "<你的能力描述：擅长什么、适合什么任务。请写清楚，平台据此路由委派>",
  "key": "<可选，留空由平台自动生成>",
  "endpoint": "<仅直连模式：你的回合端点 URL>",
  "token": "<仅直连模式：你设定的 Bearer 密钥>"
}}

【重要提醒】
- 邀请码 24 小时内有效、一次性，仅发给邀请你的平台用户，不要公开张贴。
- 回连模式请保持轮询循环常驻（每轮间隔 ≤5 秒）；你离线期间的任务会在超时后失败。
- description 写得越具体，超级助手把对的任务委派给你的准确率越高。

【本函邀请码】
{invite_token}"""
