"""超级助手反思服务：触发判定、三种反思执行、JSON 修复与审批决策测试。

行为基线（2026-11 Hindsight 迁移后）：
- micro/full/focused 反思只产出技能候选；模型按旧契约返回
  memory_candidates/conflicts 时一律忽略，不触碰旧记忆库；
- 自动反思链不做记忆 auto-accept、不触发画像/宫殿重编译；
- 技能候选的 accept（skill_service 落地 + 同名 409 + 伴随文件）与
  reject 完整保留；
- 历史记忆/冲突候选可 reject 清理，accept/new_supersedes 以
  ValueError 拒绝（router 映射 400），不写入旧记忆库；
- stage_memory_candidate（旧 manual memory_save 入口）fail-closed。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import memory_service, reflection_service
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMessage,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantSkill,
)

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reflection.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            ModelConfig.__table__,
            SuperAssistantConversation.__table__,
            SuperAssistantMessage.__table__,
            SuperAssistantMemory.__table__,
            SuperAssistantMemoryProfile.__table__,
            SuperAssistantReflectionRun.__table__,
            SuperAssistantReflectionCandidate.__table__,
            SuperAssistantSkill.__table__,
        ],
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "super_assistant_skill_root", str(tmp_path / "skills")
    )
    engine, SessionFactory = _make_session(tmp_path)
    with SessionFactory() as session:
        session.add_all([
            User(
                id="owner-1", username="owner1",
                email="owner1@example.com", password_hash="unused",
                role="editor",
            ),
            User(
                id="owner-2", username="owner2",
                email="owner2@example.com", password_hash="unused",
                role="editor",
            ),
        ])
        session.add(ModelConfig(
            id="model-1", name="测试模型", config_type="llm",
            provider="openai", models=["test-model"], enabled=True,
            is_default=True, created_by="owner-1",
        ))
        session.commit()
        yield session
    engine.dispose()


def _conversation(db, conversation_id="conv-1", owner_id="owner-1"):
    conversation = SuperAssistantConversation(
        id=conversation_id, owner_id=owner_id, title="测试会话",
    )
    db.add(conversation)
    db.commit()
    return conversation


def _message(
    db,
    conversation_id,
    role,
    content,
    *,
    message_id=None,
    status="complete",
    steps=None,
    created_at=None,
):
    message = SuperAssistantMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        status=status,
        steps=list(steps or []),
    )
    if message_id is not None:
        message.id = message_id
    if created_at is not None:
        message.created_at = created_at
    db.add(message)
    db.commit()
    return message


def _micro_turn(db):
    """种一轮完整对话：用户消息 + 带工具 steps 的助手回复（锚点）。"""
    _conversation(db)
    _message(
        db, "conv-1", "user", "我喜欢喝黑咖啡，以后推荐时优先考虑",
        message_id="msg-user",
        created_at=_BASE_TIME,
    )
    _message(
        db, "conv-1", "assistant", "好的，我记住了。",
        message_id="msg-anchor",
        created_at=_BASE_TIME + timedelta(seconds=1),
        steps=[{
            "toolName": "web_search",
            "status": "success",
            "arguments": {"q": "黑咖啡"},
            "preview": "搜索结果：黑咖啡推荐清单……",
        }],
    )


def _chat_recording(payload_text: str, calls: list):
    def fake_chat(call_kwargs, messages, tools):
        calls.append(messages)
        return {"content": payload_text, "tool_calls": [], "usage": {}}
    return fake_chat


def _spy_recompile(monkeypatch) -> list:
    """监听画像/宫殿编译调用：旧记忆链路停用后自动反思不应触发任何重编译。"""
    recompiled = []
    monkeypatch.setattr(
        memory_service,
        "compile_profile_and_palace",
        lambda db_, owner_id, llm_fn: recompiled.append(owner_id),
    )
    return recompiled


def _forbid_create_memory(monkeypatch):
    """旧记忆库写入哨兵：任何调用都以 AssertionError 失败。"""
    def forbidden_create(*_args, **_kwargs):
        raise AssertionError("自动反思链不得调用旧记忆库 create_memory")
    monkeypatch.setattr(memory_service, "create_memory", forbidden_create)


def _seed_run(
    db,
    *,
    kind="micro",
    message_id="msg-anchor",
    status="success",
    conversation_id="conv-1",
    owner_id="owner-1",
):
    run = SuperAssistantReflectionRun(
        owner_id=owner_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=kind,
        status=status,
    )
    db.add(run)
    db.commit()
    return run


def _seed_candidate(
    db,
    *,
    kind,
    payload,
    confidence="medium",
    status="pending",
    run=None,
    owner_id="owner-1",
    decision=None,
    decided_at=None,
):
    if run is None:
        run = _seed_run(db, owner_id=owner_id)
    candidate = SuperAssistantReflectionCandidate(
        run_id=run.id,
        owner_id=owner_id,
        conversation_id=run.conversation_id,
        kind=kind,
        status=status,
        confidence=confidence,
        payload=payload,
        decision=decision,
        decided_at=decided_at,
    )
    db.add(candidate)
    db.commit()
    return candidate


def _memory_payload(content="用户喜欢喝黑咖啡", **overrides):
    payload = {
        "content": content,
        "zone": "core",
        "tags": ["咖啡"],
        "pinned": False,
        "confidence": "medium",
        "supersedes": [],
    }
    payload.update(overrides)
    return payload


def _skill_payload(name="coffee-guide", **overrides):
    payload = {
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
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# should_micro_reflect
# ---------------------------------------------------------------------------


def test_should_micro_reflect_teaching_keywords_bypass_cooldown(db):
    _conversation(db)
    assert reflection_service.should_micro_reflect(
        db, "conv-1", "请记住我喜欢黑咖啡"
    ) is True
    assert reflection_service.should_micro_reflect(
        db, "conv-1", "Remember my timezone"
    ) is True
    assert reflection_service.should_micro_reflect(
        db, "conv-1", "你刚才说的不对"
    ) is True
    assert reflection_service.should_micro_reflect(
        db, "conv-1", "I PREFER tea, ACTUALLY"
    ) is True


def test_should_micro_reflect_interval_boundary(db):
    _conversation(db)
    for index in range(2):
        _message(
            db, "conv-1", "user", f"问题 {index}",
            created_at=_BASE_TIME + timedelta(minutes=index),
        )
    # 进行中的流式消息不计入
    _message(
        db, "conv-1", "user", "尚未完成", status="pending",
        created_at=_BASE_TIME + timedelta(minutes=2),
    )
    assert reflection_service.should_micro_reflect(db, "conv-1", "普通问题") is False
    _message(
        db, "conv-1", "user", "第三个问题",
        created_at=_BASE_TIME + timedelta(minutes=3),
    )
    assert reflection_service.should_micro_reflect(db, "conv-1", "普通问题") is True


def test_should_micro_reflect_counts_since_last_micro_run(db):
    _conversation(db)
    for index in range(3):
        _message(
            db, "conv-1", "user", f"早期问题 {index}",
            created_at=_BASE_TIME + timedelta(minutes=index),
        )
    # 最近一次 micro run（即使失败）也是冷却锚点
    anchor = _seed_run(db, kind="micro", status="error")
    anchor.created_at = _BASE_TIME + timedelta(minutes=10)
    db.commit()
    assert reflection_service.should_micro_reflect(db, "conv-1", "普通问题") is False
    for index in range(3):
        _message(
            db, "conv-1", "user", f"后续问题 {index}",
            created_at=_BASE_TIME + timedelta(minutes=20 + index),
        )
    assert reflection_service.should_micro_reflect(db, "conv-1", "普通问题") is True


def test_should_micro_reflect_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_reflect_enabled", False)
    _conversation(db)
    assert reflection_service.should_micro_reflect(
        db, "conv-1", "请记住我喜欢黑咖啡"
    ) is False


# ---------------------------------------------------------------------------
# _parse_json_loose
# ---------------------------------------------------------------------------


def test_parse_json_loose_handles_markdown_fence():
    text = '```json\n{"memory_candidates": [], "skill_candidates": []}\n```'
    assert reflection_service._parse_json_loose(text) == {
        "memory_candidates": [],
        "skill_candidates": [],
    }


def test_parse_json_loose_repairs_missing_tail():
    text = (
        '{"memory_candidates": [{"content": "用户喜欢手冲咖啡", '
        '"zone": "general", "tags": ["咖啡"], "confidence": "low", '
        '"supersedes": []}], "skill_candidates": ['
    )
    parsed = reflection_service._parse_json_loose(text)
    assert parsed["memory_candidates"][0]["content"] == "用户喜欢手冲咖啡"
    assert parsed["skill_candidates"] == []


def test_parse_json_loose_drops_unterminated_trailing_pair():
    text = '{"memory_candidates": [{"content": "用户喜欢手冲咖啡", "zone": "gen'
    parsed = reflection_service._parse_json_loose(text)
    # zone 键值对不完整被丢弃，完整的 content 保留
    assert parsed == {
        "memory_candidates": [{"content": "用户喜欢手冲咖啡"}],
    }


def test_parse_json_loose_rejects_garbage():
    with pytest.raises(ValueError, match="JSON"):
        reflection_service._parse_json_loose("抱歉，我无法输出 JSON。")


# ---------------------------------------------------------------------------
# run_micro_reflection
# ---------------------------------------------------------------------------


def test_micro_run_ignores_memory_candidates_and_skips_old_store(
    db, monkeypatch,
):
    """模型按旧契约返回记忆候选时：不写旧记忆库、不落候选、不重编译。

    记忆学习已移交 Hindsight 逐轮自动学习；即使 auto-accept 开关保持
    默认开启，micro 反思也不得再产出任何旧记忆。
    """
    _micro_turn(db)
    calls = []
    monkeypatch.setattr(
        reflection_service.provider,
        "chat",
        _chat_recording(json.dumps({
            "memory_candidates": [_memory_payload()],
            "skill_candidates": [],
        }, ensure_ascii=False), calls),
    )
    recompiled = _spy_recompile(monkeypatch)
    _forbid_create_memory(monkeypatch)

    run = reflection_service.run_micro_reflection(
        db, "owner-1", "conv-1", "msg-anchor"
    )

    assert run.status == "success"
    assert run.candidate_count == 0
    assert db.query(SuperAssistantMemory).count() == 0
    assert db.query(SuperAssistantReflectionCandidate).count() == 0
    assert recompiled == []
    # prompt 不再注入旧记忆小节，但仍含本轮对话与工具 preview
    prompt = calls[0][0]["content"]
    assert "已有记忆" not in prompt
    assert "我喜欢喝黑咖啡" in prompt
    assert "web_search" in prompt
    assert "搜索结果" in prompt


def test_micro_run_idempotent_for_successful_message(db, monkeypatch):
    _micro_turn(db)
    calls = []
    monkeypatch.setattr(
        reflection_service.provider,
        "chat",
        _chat_recording('{"skill_candidates": []}', calls),
    )

    first = reflection_service.run_micro_reflection(
        db, "owner-1", "conv-1", "msg-anchor"
    )
    second = reflection_service.run_micro_reflection(
        db, "owner-1", "conv-1", "msg-anchor"
    )

    assert second.id == first.id
    assert len(calls) == 1
    assert db.query(SuperAssistantReflectionRun).count() == 1


def test_micro_run_auto_accept_disabled_also_ignores_memory(db, monkeypatch):
    """auto-accept 关闭同样不落记忆候选：旧记忆链路整体停用，与开关无关。"""
    _micro_turn(db)
    db.add(SuperAssistantMemoryProfile(
        owner_id="owner-1", auto_accept_enabled=False,
    ))
    db.commit()
    monkeypatch.setattr(
        reflection_service.provider,
        "chat",
        _chat_recording(json.dumps({
            "memory_candidates": [_memory_payload()],
            "skill_candidates": [],
        }, ensure_ascii=False), []),
    )

    run = reflection_service.run_micro_reflection(
        db, "owner-1", "conv-1", "msg-anchor"
    )

    assert run.status == "success"
    assert run.candidate_count == 0
    assert db.query(SuperAssistantReflectionCandidate).count() == 0
    assert db.query(SuperAssistantMemory).count() == 0


def test_micro_run_skill_candidate_always_pending(db, monkeypatch):
    _micro_turn(db)
    monkeypatch.setattr(
        reflection_service.provider,
        "chat",
        _chat_recording(json.dumps({
            # 同轮混入旧记忆候选：只有技能候选落库
            "memory_candidates": [_memory_payload("不应落库的记忆")],
            "skill_candidates": [_skill_payload()],
        }, ensure_ascii=False), []),
    )
    recompiled = _spy_recompile(monkeypatch)

    run = reflection_service.run_micro_reflection(
        db, "owner-1", "conv-1", "msg-anchor"
    )

    assert run.candidate_count == 1
    candidate = db.query(SuperAssistantReflectionCandidate).one()
    assert candidate.kind == "skill"
    assert candidate.status == "pending"
    assert candidate.payload["name"] == "coffee-guide"
    assert candidate.payload["skill_md"].startswith("---")
    assert db.query(SuperAssistantSkill).count() == 0
    assert db.query(SuperAssistantMemory).count() == 0
    assert recompiled == []


def test_micro_run_llm_error_marks_run_error_without_raising(db, monkeypatch):
    _micro_turn(db)

    def failing_chat(*_args):
        raise reflection_service.provider.ProviderError("模型不可用")

    monkeypatch.setattr(reflection_service.provider, "chat", failing_chat)

    run = reflection_service.run_micro_reflection(
        db, "owner-1", "conv-1", "msg-anchor"
    )

    assert run.status == "error"
    assert "模型不可用" in run.error
    assert run.finished_at is not None
    assert db.query(SuperAssistantReflectionCandidate).count() == 0


def test_micro_run_parses_truncated_json(db, monkeypatch):
    """截断修复仍工作；修复出的 memory_candidates 属旧契约，被忽略。"""
    _micro_turn(db)
    monkeypatch.setattr(
        reflection_service.provider,
        "chat",
        _chat_recording(
            '{"memory_candidates": [{"content": "用户喜欢手冲咖啡", '
            '"zone": "general", "tags": ["咖啡"], "confidence": "low", '
            '"supersedes": []}], "skill_candidates": [',
            [],
        ),
    )

    run = reflection_service.run_micro_reflection(
        db, "owner-1", "conv-1", "msg-anchor"
    )

    assert run.status == "success"
    assert run.candidate_count == 0
    assert db.query(SuperAssistantReflectionCandidate).count() == 0
    assert db.query(SuperAssistantMemory).count() == 0


# ---------------------------------------------------------------------------
# run_focused_reflection
# ---------------------------------------------------------------------------


def test_focused_run_produces_single_pending_skill_candidate(db, monkeypatch):
    _micro_turn(db)
    calls = []
    monkeypatch.setattr(
        reflection_service.provider,
        "chat",
        _chat_recording(json.dumps({
            "skill_candidates": [_skill_payload("report-writer")],
            # focused 忽略 memory 候选，即使模型给了
            "memory_candidates": [_memory_payload("不应落库的记忆")],
        }, ensure_ascii=False), calls),
    )

    run = reflection_service.run_focused_reflection(
        db, "owner-1", "conv-1", "msg-anchor", hint="把写报告的流程沉淀下来"
    )

    assert run.status == "success"
    assert run.candidate_count == 1
    candidate = db.query(SuperAssistantReflectionCandidate).one()
    assert candidate.kind == "skill"
    assert candidate.status == "pending"
    assert candidate.payload["name"] == "report-writer"
    assert db.query(SuperAssistantMemory).count() == 0
    assert "把写报告的流程沉淀下来" in calls[0][0]["content"]


# ---------------------------------------------------------------------------
# run_full_reflection
# ---------------------------------------------------------------------------


def test_full_run_running_record_prevents_duplicate(db, monkeypatch):
    _conversation(db)
    running = _seed_run(db, kind="full", message_id=None, status="running")

    def forbidden_chat(*_args):
        raise AssertionError("已有 running 的 full run 时不得调用 LLM")

    monkeypatch.setattr(reflection_service.provider, "chat", forbidden_chat)

    run = reflection_service.run_full_reflection(db, "owner-1", "conv-1")
    assert run.id == running.id
    assert db.query(SuperAssistantReflectionRun).count() == 1


def test_full_run_lands_only_skill_candidates(db, monkeypatch):
    _micro_turn(db)
    old = memory_service.create_memory(
        db, "owner-1", "用户偏好英文报告", conflict_check=False,
    )
    # 审批历史：一条被 reject 的记忆候选
    _seed_candidate(
        db,
        kind="memory",
        payload=_memory_payload("用户不喜欢咖啡"),
        confidence="low",
        status="rejected",
        decision="reject",
        decided_at=datetime.now(timezone.utc),
    )
    calls = []
    monkeypatch.setattr(
        reflection_service.provider,
        "chat",
        _chat_recording(json.dumps({
            "summary": "用户明确了报告语言偏好",
            # 模型仍按旧契约返回记忆/冲突候选：一律忽略，只落技能
            "memory_candidates": [_memory_payload(
                "用户偏好中文报告",
                zone="work",
                confidence="high",
                supersedes=[old.id],
            )],
            "skill_candidates": [_skill_payload("weekly-report")],
            "conflicts": [{
                "memory_id": old.id,
                "conflict_kind": "contradiction",
                "explain": "报告语言偏好发生变化",
                "options": ["new_supersedes", "keep_old"],
                "candidate_content": "用户偏好中文报告",
            }],
        }, ensure_ascii=False), calls),
    )
    recompiled = _spy_recompile(monkeypatch)
    _forbid_create_memory(monkeypatch)

    run = reflection_service.run_full_reflection(db, "owner-1", "conv-1")

    assert run.status == "success"
    assert run.candidate_count == 1
    candidates = (
        db.query(SuperAssistantReflectionCandidate)
        .filter(SuperAssistantReflectionCandidate.run_id == run.id)
        .all()
    )
    assert [candidate.kind for candidate in candidates] == ["skill"]
    assert candidates[0].status == "pending"
    assert candidates[0].payload["name"] == "weekly-report"
    # 旧记忆不被新增/取代，也不触发画像重编译
    db.refresh(old)
    assert old.superseded is False
    assert db.query(SuperAssistantMemory).count() == 1
    assert recompiled == []
    # 审批历史进入 prompt（meta-reflection），旧记忆小节不再进入
    prompt = calls[0][0]["content"]
    assert "reject" in prompt
    assert "用户不喜欢咖啡" in prompt
    assert "现有记忆" not in prompt


# ---------------------------------------------------------------------------
# decide_candidate
# ---------------------------------------------------------------------------


def test_decide_memory_accept_is_rejected_legacy(db, monkeypatch):
    """历史记忆候选 accept 被显式拒绝（ValueError → router 映射 400），
    候选保持 pending，不写旧记忆库、不重编译。"""
    candidate = _seed_candidate(db, kind="memory", payload=_memory_payload())
    recompiled = _spy_recompile(monkeypatch)
    _forbid_create_memory(monkeypatch)

    with pytest.raises(ValueError, match="旧记忆链路已停用"):
        reflection_service.decide_candidate(
            db, "owner-1", candidate.id, "accept"
        )

    assert candidate.status == "pending"
    assert db.query(SuperAssistantMemory).count() == 0
    assert recompiled == []


def test_decide_memory_accept_edited_payload_still_rejected(db, monkeypatch):
    """edited_payload 不能绕过停用拒绝：任何 accept 语义都不生效。"""
    candidate = _seed_candidate(db, kind="memory", payload=_memory_payload())
    _spy_recompile(monkeypatch)

    with pytest.raises(ValueError, match="旧记忆链路已停用"):
        reflection_service.decide_candidate(
            db, "owner-1", candidate.id, "accept",
            edited_payload={"content": "人工修订后的内容", "zone": "work"},
        )

    assert candidate.status == "pending"
    assert db.query(SuperAssistantMemory).count() == 0


def test_decide_memory_reject_only_marks(db, monkeypatch):
    candidate = _seed_candidate(db, kind="memory", payload=_memory_payload())
    recompiled = _spy_recompile(monkeypatch)

    decided = reflection_service.decide_candidate(
        db, "owner-1", candidate.id, "reject"
    )

    assert decided.status == "rejected"
    assert decided.decision == "reject"
    assert db.query(SuperAssistantMemory).count() == 0
    assert recompiled == []


def test_stage_memory_candidate_fail_closed(db):
    """旧 manual memory_save 入口 fail-closed：不建 run、不落候选。"""
    _conversation(db)
    with pytest.raises(ValueError, match="旧记忆链路已停用"):
        reflection_service.stage_memory_candidate(
            db, "owner-1", "conv-1", _memory_payload(),
        )

    assert db.query(SuperAssistantReflectionRun).count() == 0
    assert db.query(SuperAssistantReflectionCandidate).count() == 0


def test_decide_skill_accept_creates_skill_via_skill_service(db, monkeypatch):
    candidate = _seed_candidate(
        db,
        kind="skill",
        payload=_skill_payload(
            files=[{"path": "references/ratios.md", "content": "粉水比表"}],
        ),
    )
    recompiled = _spy_recompile(monkeypatch)

    decided = reflection_service.decide_candidate(
        db, "owner-1", candidate.id, "accept"
    )

    assert decided.status == "accepted"
    skill = db.query(SuperAssistantSkill).one()
    assert skill.name == "coffee-guide"
    assert skill.enabled is True
    folder = Path(skill.folder_path)
    assert (folder / "SKILL.md").exists()
    assert (folder / "references" / "ratios.md").read_text(
        encoding="utf-8"
    ) == "粉水比表"
    # 技能落地不再触发旧画像/宫殿重编译（旧记忆链路停用）
    assert recompiled == []


def test_decide_skill_accept_duplicate_name_conflicts(db, monkeypatch):
    db.add(SuperAssistantSkill(
        owner_id="owner-1", name="coffee-guide", display_name="旧技能",
        description="", folder_path="/tmp/dup-skill",
    ))
    db.commit()
    candidate = _seed_candidate(db, kind="skill", payload=_skill_payload())
    _spy_recompile(monkeypatch)

    with pytest.raises(ValueError, match="同名 Skill 已存在"):
        reflection_service.decide_candidate(
            db, "owner-1", candidate.id, "accept"
        )

    assert candidate.status == "pending"
    assert db.query(SuperAssistantSkill).count() == 1


def test_decide_conflict_new_supersedes_is_rejected_legacy(db, monkeypatch):
    """new_supersedes 会向旧记忆库写取代记忆，随旧链路一并停用。"""
    old = memory_service.create_memory(
        db, "owner-1", "用户偏好英文报告", conflict_check=False,
    )
    candidate = _seed_candidate(
        db,
        kind="conflict",
        payload={
            "memory_id": old.id,
            "conflict_kind": "contradiction",
            "explain": "报告语言偏好发生变化",
            "options": ["new_supersedes", "keep_old"],
            "candidate_content": "用户偏好中文报告",
        },
    )
    recompiled = _spy_recompile(monkeypatch)
    _forbid_create_memory(monkeypatch)

    with pytest.raises(ValueError, match="旧记忆链路已停用"):
        reflection_service.decide_candidate(
            db, "owner-1", candidate.id, "new_supersedes"
        )

    assert candidate.status == "pending"
    db.refresh(old)
    assert old.superseded is False
    assert db.query(SuperAssistantMemory).count() == 1
    assert recompiled == []


def test_decide_conflict_keep_old_and_skip_only_mark(db, monkeypatch):
    for action in ("keep_old", "skip"):
        old = memory_service.create_memory(
            db, "owner-1", f"旧记忆-{action}", conflict_check=False,
        )
        candidate = _seed_candidate(
            db,
            kind="conflict",
            payload={
                "memory_id": old.id,
                "conflict_kind": "outdated",
                "explain": "可能过时",
                "options": [],
                "candidate_content": "新内容",
            },
        )
        decided = reflection_service.decide_candidate(
            db, "owner-1", candidate.id, action
        )
        assert decided.status == "rejected"
        assert decided.decision == action
        db.refresh(old)
        assert old.superseded is False
    assert db.query(SuperAssistantMemory).count() == 2


def test_decide_candidate_unknown_and_already_decided_errors(db):
    candidate = _seed_candidate(db, kind="memory", payload=_memory_payload())

    with pytest.raises(ValueError, match="候选不存在"):
        reflection_service.decide_candidate(db, "owner-2", candidate.id, "accept")

    # 旧记忆候选的任何非 reject 操作都落入停用拒绝（含未知操作）
    with pytest.raises(ValueError, match="旧记忆链路已停用"):
        reflection_service.decide_candidate(db, "owner-1", candidate.id, "bogus")

    reflection_service.decide_candidate(db, "owner-1", candidate.id, "reject")

    with pytest.raises(ValueError, match="候选已处理"):
        reflection_service.decide_candidate(db, "owner-1", candidate.id, "accept")


# ---------------------------------------------------------------------------
# recent_decisions
# ---------------------------------------------------------------------------


def test_recent_decisions_returns_latest_first(db):
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    for index, (decision, content) in enumerate([
        ("reject", "用户不喜欢咖啡"),
        ("accept", "用户偏好中文报告"),
        ("skip", "时区可能变化"),
    ]):
        _seed_candidate(
            db,
            kind="memory",
            payload=_memory_payload(content),
            status="accepted" if decision == "accept" else "rejected",
            decision=decision,
            decided_at=base + timedelta(minutes=index),
        )
    _seed_candidate(db, kind="memory", payload=_memory_payload("未决候选"))

    items = reflection_service.recent_decisions(db, "owner-1", cap=10)

    assert [item["decision"] for item in items] == ["skip", "accept", "reject"]
    assert items[0]["payload_summary"] == "时区可能变化"
    assert items[0]["decided_at"] is not None

    capped = reflection_service.recent_decisions(db, "owner-1", cap=2)
    assert [item["decision"] for item in capped] == ["skip", "accept"]
