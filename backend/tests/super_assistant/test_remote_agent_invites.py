"""远程助手邀请自助接入：生命周期、公开兑换与回连任务端点。

覆盖：邀请创建（一次性令牌 + 邀请函单源生成）、兑换落库（直连/回连
双模式、key 自动生成、agent key 一次性发放）、一次性/过期/撤销/防枚举
口径、邀请函重发、公开端点鉴权与完整回连轮询循环。
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from app.shared.encryption import decrypt
from app.super_assistant import (
    remote_agent_invite_service,
    remote_agent_public,
    remote_agent_service,
)
from app.super_assistant.models import (
    SuperAssistantRemoteAgent,
    SuperAssistantRemoteAgentInvite,
    SuperAssistantRemoteAgentTask,
)
from app.super_assistant.schemas import (
    RemoteAgentRedeemIn,
    RemoteAgentTaskResultIn,
)

PUBLIC = "/api/public/super-assistant/remote-agents"


def _create_invite(db, owner_id) -> tuple[object, str]:
    created = remote_agent_invite_service.create_invite(db, owner_id)
    row = db.query(SuperAssistantRemoteAgentInvite).filter(
        SuperAssistantRemoteAgentInvite.id == created.id,
    ).one()
    return created, decrypt(row.token_encrypted)


def _redeem_body(token: str, **overrides) -> RemoteAgentRedeemIn:
    payload = {
        "invite_token": token,
        "mode": "pull",
        "label": "回连小助手",
        "description": "测试用",
    }
    payload.update(overrides)
    return RemoteAgentRedeemIn(**payload)


# ------------------------------------------------------------------- 创建


def test_create_invite_generates_once_token_and_prompt(db, admin_user):
    created, token = _create_invite(db, admin_user.id)

    assert created.status == "pending"
    assert token.startswith("rai_")
    assert token in created.prompt_text
    # 邀请函：双模式契约 + 兑换端点 + 令牌，由后端单源生成
    assert "方式 A：直连" in created.prompt_text
    assert "方式 B：回连" in created.prompt_text
    assert "/api/public/super-assistant/remote-agents/invite-redeem" in created.prompt_text

    row = db.query(SuperAssistantRemoteAgentInvite).one()
    assert row.token_hash == remote_agent_service.hash_secret(token)
    assert row.token_hash != token  # 明文不落库


def test_list_invites_reports_status_and_redeemed_agent(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "validate_mcp_url", lambda url: url)
    _, token = _create_invite(db, admin_user.id)
    remote_agent_invite_service.redeem(db, _redeem_body(token, mode="direct",
                                                       endpoint="http://127.0.0.1:9101/turn"))

    items = remote_agent_invite_service.list_invites(db, admin_user.id)
    assert len(items) == 1
    assert items[0].status == "used"
    # 中文标签无可保留 slug 字符 → 回退 remote.agent
    assert items[0].redeemed_agent_key == "remote.agent"
    assert items[0].redeemed_agent_label == "回连小助手"


# ------------------------------------------------------------------- 兑换


def test_redeem_pull_issues_agent_key_once(db, admin_user):
    _, token = _create_invite(db, admin_user.id)
    out = remote_agent_invite_service.redeem(db, _redeem_body(token))

    assert out.mode == "pull"
    assert out.agent_key and out.agent_key.startswith("rak_")
    assert out.rap_version == 1  # 缺省按 RAP v1 注册
    row = db.query(SuperAssistantRemoteAgent).one()
    assert row.mode == "pull"
    assert row.endpoint == ""
    assert row.enabled is True
    assert row.agent_key_hash == remote_agent_service.hash_secret(out.agent_key)
    assert row.rap_version == 1
    assert row.key.startswith("remote.")  # key 未提供时按名称自动生成


def test_create_invite_caps_pending_per_owner(db, admin_user):
    for _ in range(remote_agent_invite_service.MAX_PENDING_INVITES):
        remote_agent_invite_service.create_invite(db, admin_user.id)
    with pytest.raises(remote_agent_service.RemoteAgentServiceError, match="待使用邀请"):
        remote_agent_invite_service.create_invite(db, admin_user.id)
    # 撤销一个后可再建
    first = db.query(SuperAssistantRemoteAgentInvite).order_by(
        SuperAssistantRemoteAgentInvite.created_at.asc(),
    ).first()
    remote_agent_invite_service.revoke_invite(db, admin_user.id, first.id)
    remote_agent_invite_service.create_invite(db, admin_user.id)


def test_redeem_rejects_unknown_rap_version(db, admin_user):
    _, token = _create_invite(db, admin_user.id)
    with pytest.raises(HTTPException) as exc:
        remote_agent_invite_service.redeem(db, _redeem_body(token, rap_version=2))
    assert exc.value.status_code == 400
    assert "RAP" in str(exc.value.detail)


def test_redeem_direct_requires_valid_endpoint(db, admin_user):
    _, token = _create_invite(db, admin_user.id)
    with pytest.raises(HTTPException) as exc:
        remote_agent_invite_service.redeem(db, _redeem_body(token, mode="direct"))
    assert exc.value.status_code == 400

    invite = db.query(SuperAssistantRemoteAgentInvite).one()
    assert remote_agent_invite_service.invite_status(invite) == "used"  # 消费不回滚


def test_redeem_auto_key_avoids_collision(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "validate_mcp_url", lambda url: url)
    keys = []
    for _ in range(2):
        _, token = _create_invite(db, admin_user.id)
        out = remote_agent_invite_service.redeem(db, _redeem_body(
            token, mode="direct", label="helper",
            endpoint="http://127.0.0.1:9101/turn",
        ))
        keys.append(out.key)
    assert keys == ["remote.helper", "remote.helper-2"]


def test_redeem_token_single_use_and_anti_enumeration(db, admin_user):
    _, token = _create_invite(db, admin_user.id)
    remote_agent_invite_service.redeem(db, _redeem_body(token))

    with pytest.raises(HTTPException) as exc:
        remote_agent_invite_service.redeem(db, _redeem_body(token))
    assert exc.value.status_code == 409

    # 未知/过期/撤销统一 404，与无效令牌同响应防枚举
    with pytest.raises(HTTPException) as exc:
        remote_agent_invite_service.redeem(db, _redeem_body("rai_does_not_exist_token_xx"))
    assert exc.value.status_code == 404

    _, expired_token = _create_invite(db, admin_user.id)
    invite = db.query(SuperAssistantRemoteAgentInvite).filter(
        SuperAssistantRemoteAgentInvite.token_encrypted.isnot(None),
    ).all()[-1]
    invite.expires_at = invite.expires_at.replace(year=invite.expires_at.year - 1)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        remote_agent_invite_service.redeem(db, _redeem_body(expired_token))
    assert exc.value.status_code == 404

    created, revoked_token = _create_invite(db, admin_user.id)
    remote_agent_invite_service.revoke_invite(db, admin_user.id, created.id)
    with pytest.raises(HTTPException) as exc:
        remote_agent_invite_service.redeem(db, _redeem_body(revoked_token))
    assert exc.value.status_code == 404


def test_invite_prompt_redispatch_only_while_pending(db, admin_user, monkeypatch):
    monkeypatch.setattr(remote_agent_service, "validate_mcp_url", lambda url: url)
    created, token = _create_invite(db, admin_user.id)

    prompt = remote_agent_invite_service.invite_prompt(db, admin_user.id, created.id)
    assert token in prompt

    remote_agent_invite_service.redeem(db, _redeem_body(token, mode="direct",
                                                        endpoint="http://127.0.0.1:9101/turn"))
    with pytest.raises(HTTPException) as exc:
        remote_agent_invite_service.invite_prompt(db, admin_user.id, created.id)
    assert exc.value.status_code == 404


# ------------------------------------------------------- 公开端点（回连）


def _patch_claim_sessions(db, monkeypatch):
    """长轮询认领用 SessionLocal 短会话：测试里指回 fixture 引擎。"""
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(
        remote_agent_public, "SessionLocal",
        sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False),
    )


def test_public_redeem_and_pull_cycle_via_http(db, admin_user, client, monkeypatch):
    _patch_claim_sessions(db, monkeypatch)
    _, token = _create_invite(db, admin_user.id)

    response = client.post(f"{PUBLIC}/invite-redeem", json={
        "invite_token": token, "mode": "pull",
        "label": "回连助手", "description": "HTTP 兑换",
    })
    assert response.status_code == 200
    agent_key = response.json()["agent_key"]
    assert agent_key.startswith("rak_")

    # 未带/错 key 一律 401
    assert client.get(f"{PUBLIC}/tasks/next?wait=0").status_code == 401
    assert client.get(
        f"{PUBLIC}/tasks/next?wait=0", headers={"Authorization": "Bearer rak_wrong"},
    ).status_code == 401

    agent = db.query(SuperAssistantRemoteAgent).one()
    remote_agent_service.enqueue_task(db, agent.id, "你好", None, 60)

    headers = {"Authorization": f"Bearer {agent_key}"}
    next_response = client.get(f"{PUBLIC}/tasks/next?wait=1", headers=headers)
    assert next_response.status_code == 200
    task = next_response.json()
    assert task["message"] == "你好"
    assert task["session_ref"] is None
    assert task["rap_version"] == 1  # 任务载荷携带协商版本

    result = client.post(f"{PUBLIC}/tasks/{task['task_id']}/result", headers=headers, json={
        "status": "answered", "content": "好的", "session_ref": "sess-1",
    })
    assert result.status_code == 200
    row = db.query(SuperAssistantRemoteAgentTask).one()
    assert row.status == "done"
    assert row.result_content == "好的"
    assert row.result_session_ref == "sess-1"
    assert agent.last_seen_at is not None  # 轮询即心跳

    # 任务已完成后重复回传 → 409；不存在的任务 → 404
    assert client.post(f"{PUBLIC}/tasks/{task['task_id']}/result", headers=headers, json={
        "status": "answered", "content": "again",
    }).status_code == 409
    assert client.post(f"{PUBLIC}/tasks/nope/result", headers=headers, json={
        "status": "answered", "content": "x",
    }).status_code == 404


def test_public_next_returns_204_when_idle(db, admin_user, client, monkeypatch):
    _patch_claim_sessions(db, monkeypatch)
    _, token = _create_invite(db, admin_user.id)
    agent_key = client.post(f"{PUBLIC}/invite-redeem", json={
        "invite_token": token, "mode": "pull", "label": "空闲助手",
    }).json()["agent_key"]

    started = time.monotonic()
    response = client.get(f"{PUBLIC}/tasks/next?wait=0",
                          headers={"Authorization": f"Bearer {agent_key}"})
    assert response.status_code == 204
    assert time.monotonic() - started < 5


def test_public_redeem_rejects_bad_invite_without_leaking_state(client):
    response = client.post(f"{PUBLIC}/invite-redeem", json={
        "invite_token": "rai_not_a_real_invite_token", "mode": "pull", "label": "x",
    })
    assert response.status_code == 404
    assert "无效或已过期" in response.json()["detail"]


# ------------------------------------------------------- 属主侧 HTTP

from app.super_assistant import remote_agent_invite_service


def test_owner_invite_endpoints_over_http(client, auth_headers, admin_user, db, monkeypatch):
    from app.super_assistant import remote_agent_service
    monkeypatch.setattr(remote_agent_service, "validate_mcp_url", lambda url: url)

    created = client.post("/api/v2/super-assistant/remote-agent-invites", headers=auth_headers, json={})
    assert created.status_code == 200
    body = created.json()
    assert body["prompt_text"].startswith("你是即将接入")

    listed = client.get("/api/v2/super-assistant/remote-agent-invites", headers=auth_headers)
    assert listed.status_code == 200 and listed.json()[0]["status"] == "pending"

    prompt = client.get(
        f"/api/v2/super-assistant/remote-agent-invites/{body['id']}/prompt", headers=auth_headers,
    )
    assert prompt.status_code == 200
    assert prompt.headers["content-type"].startswith("text/plain")
    assert "rai_" in prompt.text

    revoked = client.delete(
        f"/api/v2/super-assistant/remote-agent-invites/{body['id']}", headers=auth_headers,
    )
    assert revoked.status_code == 204
    assert client.get("/api/v2/super-assistant/remote-agent-invites", headers=auth_headers).json()[0]["status"] == "revoked"

    # 未登录访问属主端点被守卫拒绝
    assert client.get("/api/v2/super-assistant/remote-agent-invites").status_code in (401, 403)
