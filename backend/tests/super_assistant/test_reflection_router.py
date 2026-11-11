"""超级助手反思 HTTP 端点：候选列表/审批/full 触发/设置测试。

行为基线（2026-11 Hindsight 迁移后）：候选列表可查询历史（含旧记忆
候选）；技能候选 accept 全链路保留；旧记忆/冲突候选 accept/new_supersedes
经现有 ValueError 映射返回 400，reject/keep_old/skip 可清理历史。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import reflection_service, router
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMessage,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantSkill,
)

_PREFIX = "/api/v2/super-assistant"
_BASE_TIME = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "super_assistant_skill_root", str(tmp_path / "skills")
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reflection-router.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            SuperAssistantConversation.__table__,
            SuperAssistantMessage.__table__,
            SuperAssistantMemory.__table__,
            SuperAssistantMemoryProfile.__table__,
            SuperAssistantReflectionRun.__table__,
            SuperAssistantReflectionCandidate.__table__,
            SuperAssistantSkill.__table__,
        ],
    )
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestingSession() as session:
        session.add_all([
            User(
                id="user-1", username="owner",
                email="owner@example.com", password_hash="unused",
                role="editor",
            ),
            User(
                id="user-2", username="other",
                email="other@example.com", password_hash="unused",
                role="editor",
            ),
        ])
        session.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(router.router, prefix=_PREFIX)
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: User(
        id="user-1", username="owner",
        email="owner@example.com", password_hash="unused", role="editor",
    )
    return engine, TestingSession, TestClient(app)


def _seed_candidate(
    db,
    *,
    candidate_id,
    owner_id="user-1",
    kind="memory",
    status="pending",
    payload=None,
    decision=None,
    decided_at=None,
    created_at=None,
):
    run = SuperAssistantReflectionRun(
        owner_id=owner_id,
        conversation_id="conv-1",
        message_id=None,
        kind="micro",
        status="success",
    )
    db.add(run)
    db.flush()
    candidate = SuperAssistantReflectionCandidate(
        id=candidate_id,
        run_id=run.id,
        owner_id=owner_id,
        conversation_id="conv-1",
        kind=kind,
        status=status,
        confidence="medium",
        payload=payload or {},
        decision=decision,
        decided_at=decided_at,
    )
    if created_at is not None:
        candidate.created_at = created_at
    db.add(candidate)
    db.commit()
    return candidate


def _seed_conversation(db, conversation_id="conv-1", owner_id="user-1"):
    conversation = SuperAssistantConversation(
        id=conversation_id, owner_id=owner_id, title="会话",
    )
    db.add(conversation)
    db.commit()
    return conversation


def _skill_payload(name="coffee-guide"):
    return {
        "name": name,
        "display_name": "咖啡指南",
        "description": "手冲咖啡步骤",
        "triggers": ["咖啡"],
        "skill_md": (
            f"---\nname: {name}\ndescription: 手冲咖啡步骤\n---\n\n"
            "按 1:15 粉水比冲泡。"
        ),
        "files": [],
    }


def test_candidates_list_filters_status_and_owner(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        _seed_candidate(
            db, candidate_id="c-pending",
            payload={"content": "用户喜欢黑咖啡"},
            created_at=_BASE_TIME,
        )
        _seed_candidate(
            db, candidate_id="c-accepted", status="accepted",
            payload={"content": "用户偏好中文"}, decision="accept",
            decided_at=_BASE_TIME,
            created_at=_BASE_TIME + timedelta(minutes=1),
        )
        _seed_candidate(
            db, candidate_id="c-other-owner", owner_id="user-2",
            payload={"content": "别人的候选"},
            created_at=_BASE_TIME + timedelta(minutes=2),
        )
    try:
        response = client.get(f"{_PREFIX}/reflection/candidates")
        assert response.status_code == 200, response.text
        items = response.json()
        # 默认只列本人 pending，按创建时间升序，payload 一并返回
        assert [item["id"] for item in items] == ["c-pending"]
        assert items[0]["payload"] == {"content": "用户喜欢黑咖啡"}
        assert items[0]["kind"] == "memory"

        accepted = client.get(
            f"{_PREFIX}/reflection/candidates", params={"status": "accepted"}
        )
        assert [item["id"] for item in accepted.json()] == ["c-accepted"]

        all_items = client.get(
            f"{_PREFIX}/reflection/candidates", params={"status": "all"}
        )
        assert [item["id"] for item in all_items.json()] == [
            "c-pending", "c-accepted",
        ]

        bogus = client.get(
            f"{_PREFIX}/reflection/candidates", params={"status": "bogus"}
        )
        assert bogus.status_code == 422
    finally:
        engine.dispose()


def test_decision_memory_accept_rejected_legacy_and_reject_cleans(tmp_path, monkeypatch):
    """旧记忆候选：accept 经现有 ValueError 映射返回 400 且不写旧库；
    reject 可清理历史候选（409 幂等语义保持）。"""
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        _seed_candidate(
            db, candidate_id="c-1",
            payload={
                "content": "用户喜欢黑咖啡",
                "zone": "core",
                "tags": ["咖啡"],
                "pinned": False,
                "confidence": "medium",
                "supersedes": [],
            },
        )
    try:
        accept = client.post(
            f"{_PREFIX}/reflection/candidates/c-1/decision",
            json={"decision": "accept"},
        )
        assert accept.status_code == 400, accept.text
        assert "旧记忆链路已停用" in accept.json()["detail"]

        with TestingSession() as db:
            assert db.query(SuperAssistantMemory).count() == 0
            candidate = db.get(SuperAssistantReflectionCandidate, "c-1")
            assert candidate.status == "pending"

        rejected = client.post(
            f"{_PREFIX}/reflection/candidates/c-1/decision",
            json={"decision": "reject"},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"

        again = client.post(
            f"{_PREFIX}/reflection/candidates/c-1/decision",
            json={"decision": "reject"},
        )
        assert again.status_code == 409
        assert again.json()["detail"] == "候选已处理"

        with TestingSession() as db:
            assert db.query(SuperAssistantMemory).count() == 0
    finally:
        engine.dispose()


def test_decision_conflict_new_supersedes_rejected_legacy(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        _seed_candidate(
            db, candidate_id="c-conflict", kind="conflict",
            payload={
                "memory_id": "mem-old",
                "conflict_kind": "contradiction",
                "explain": "偏好变化",
                "options": ["new_supersedes", "keep_old"],
                "candidate_content": "用户偏好中文报告",
            },
        )
    try:
        supersede = client.post(
            f"{_PREFIX}/reflection/candidates/c-conflict/decision",
            json={"decision": "new_supersedes"},
        )
        assert supersede.status_code == 400, supersede.text
        assert "旧记忆链路已停用" in supersede.json()["detail"]

        keep = client.post(
            f"{_PREFIX}/reflection/candidates/c-conflict/decision",
            json={"decision": "keep_old"},
        )
        assert keep.status_code == 200, keep.text
        assert keep.json()["status"] == "rejected"

        with TestingSession() as db:
            assert db.query(SuperAssistantMemory).count() == 0
    finally:
        engine.dispose()


def test_decision_owner_isolation_and_bad_decision(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        _seed_candidate(
            db, candidate_id="c-other", owner_id="user-2",
            payload={"content": "别人的候选"},
        )
        _seed_candidate(
            db, candidate_id="c-mine",
            payload={"content": "用户喜欢黑咖啡", "zone": "general"},
        )
        _seed_candidate(
            db, candidate_id="c-skill", kind="skill", payload=_skill_payload(),
        )
    try:
        missing = client.post(
            f"{_PREFIX}/reflection/candidates/c-other/decision",
            json={"decision": "accept"},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "候选不存在"

        # 旧记忆候选的未知操作同样落入停用拒绝（400）
        legacy = client.post(
            f"{_PREFIX}/reflection/candidates/c-mine/decision",
            json={"decision": "bogus"},
        )
        assert legacy.status_code == 400
        assert "旧记忆链路已停用" in legacy.json()["detail"]

        # 技能候选保留独立的未知操作校验
        bad = client.post(
            f"{_PREFIX}/reflection/candidates/c-skill/decision",
            json={"decision": "bogus"},
        )
        assert bad.status_code == 400
        assert "不支持的技能候选操作" in bad.json()["detail"]
    finally:
        engine.dispose()


def test_decision_accept_skill_creates_skill(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        _seed_candidate(
            db, candidate_id="c-skill", kind="skill",
            payload=_skill_payload(),
        )
    try:
        response = client.post(
            f"{_PREFIX}/reflection/candidates/c-skill/decision",
            json={"decision": "accept"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "accepted"
        with TestingSession() as db:
            skill = db.query(SuperAssistantSkill).one()
            assert skill.name == "coffee-guide"
            assert skill.enabled is True
            assert (Path(skill.folder_path) / "SKILL.md").exists()
    finally:
        engine.dispose()


def test_decision_accept_skill_duplicate_name_conflicts(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        db.add(SuperAssistantSkill(
            owner_id="user-1", name="coffee-guide", display_name="旧技能",
            description="", folder_path="/tmp/dup-skill",
        ))
        _seed_candidate(
            db, candidate_id="c-skill-dup", kind="skill",
            payload=_skill_payload(),
        )
        db.commit()
    try:
        response = client.post(
            f"{_PREFIX}/reflection/candidates/c-skill-dup/decision",
            json={"decision": "accept"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "同名 Skill 已存在"
        # 候选仍未决，可稍后重试
        with TestingSession() as db:
            candidate = db.get(SuperAssistantReflectionCandidate, "c-skill-dup")
            assert candidate.status == "pending"
    finally:
        engine.dispose()


def test_full_reflection_dispatch_path(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        _seed_conversation(db)
    sent = []
    monkeypatch.setattr(
        reflection_service,
        "dispatch_super_assistant_reflection",
        lambda kind, payload: sent.append((kind, payload)),
    )
    try:
        response = client.post(
            f"{_PREFIX}/reflection/full",
            json={"conversation_id": "conv-1"},
        )
        assert response.status_code == 202, response.text
        assert response.json() == {"dispatched": True, "runId": None}
        assert sent == [(
            "full",
            {"owner_id": "user-1", "conversation_id": "conv-1"},
        )]
    finally:
        engine.dispose()


def test_full_reflection_inline_fallback_without_nats(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        _seed_conversation(db)

    def no_nats(_kind, _payload):
        raise RuntimeError(
            "后台任务派发失败：未配置 NATS_URL（JetStream 消息通道），"
            "请在环境配置中显式设置后重试"
        )

    monkeypatch.setattr(
        reflection_service, "dispatch_super_assistant_reflection", no_nats
    )
    monkeypatch.setattr(
        reflection_service,
        "run_full_reflection",
        lambda db, owner_id, conversation_id: SimpleNamespace(
            id="run-inline"
        ),
    )
    try:
        response = client.post(
            f"{_PREFIX}/reflection/full",
            json={"conversation_id": "conv-1"},
        )
        assert response.status_code == 202, response.text
        assert response.json() == {"dispatched": False, "runId": "run-inline"}
    finally:
        engine.dispose()


def test_full_reflection_conversation_and_dispatch_errors(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        _seed_conversation(db, conversation_id="conv-other", owner_id="user-2")
        _seed_conversation(db, conversation_id="conv-1", owner_id="user-1")

    def broken_dispatch(_kind, _payload):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        reflection_service,
        "dispatch_super_assistant_reflection",
        broken_dispatch,
    )
    try:
        missing = client.post(
            f"{_PREFIX}/reflection/full",
            json={"conversation_id": "conv-other"},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "会话不存在"

        unavailable = client.post(
            f"{_PREFIX}/reflection/full",
            json={"conversation_id": "conv-1"},
        )
        assert unavailable.status_code == 503
    finally:
        engine.dispose()


def test_settings_get_defaults_and_put_upserts(tmp_path, monkeypatch):
    engine, TestingSession, client = _make_app(tmp_path, monkeypatch)
    with TestingSession() as db:
        db.add(SuperAssistantMemory(
            id="mem-1", owner_id="user-1", content="用户喜欢黑咖啡",
        ))
        _seed_candidate(db, candidate_id="c-pending")
    try:
        response = client.get(f"{_PREFIX}/reflection/settings")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "auto_accept_enabled": True,
            "palace_index": None,
            "profile": None,
            "memory_count": 1,
            "pending_count": 1,
        }

        updated = client.put(
            f"{_PREFIX}/reflection/settings",
            json={"auto_accept_enabled": False},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["auto_accept_enabled"] is False

        again = client.get(f"{_PREFIX}/reflection/settings")
        assert again.json()["auto_accept_enabled"] is False
        with TestingSession() as db:
            profile = db.get(SuperAssistantMemoryProfile, "user-1")
            assert profile is not None
            assert profile.auto_accept_enabled is False
    finally:
        engine.dispose()
