"""Skill 治理（对标 small-rust-hermes）：常驻注入、使用统计与目录降权。

沿用 test_runtime_integration 的隔离 sqlite 手法：每张用例自建引擎与会话，
provider.chat_stream 一律伪造，不触网。
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import runtime, skill_service
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantMcpServer,
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
    SuperAssistantMessage,
    SuperAssistantMulticaConfig,
    SuperAssistantReflectionCandidate,
    SuperAssistantReflectionRun,
    SuperAssistantSkill,
    SuperAssistantToolRun,
)
from app.super_assistant.schemas import SkillCreate, SkillOut, SkillUpdate
from app.super_assistant.skill_store import render_skill_markdown, skill_directory

_TABLES = [
    User.__table__, ModelConfig.__table__,
    RoleMenuPermission.__table__,  # 委派目录注入的菜单权限查询
    SuperAssistantConversation.__table__, SuperAssistantSkill.__table__,
    SuperAssistantMcpServer.__table__, SuperAssistantMessage.__table__,
    SuperAssistantToolRun.__table__, SuperAssistantMemory.__table__,
    SuperAssistantMulticaConfig.__table__,
    SuperAssistantMemoryProfile.__table__, SuperAssistantReflectionRun.__table__,
    SuperAssistantReflectionCandidate.__table__,
]


def _seed(tmp_path, monkeypatch, name, *, user_content="你好"):
    monkeypatch.setattr(settings, "super_assistant_skill_root", str(tmp_path / f"skills-{name}"))
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(runtime, "SessionLocal", TestingSession)
    with TestingSession() as db:
        db.add(User(
            id=f"user-{name}", username=name, email=f"{name}@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(ModelConfig(
            id=f"model-{name}", name="Fake", config_type="llm", provider="openai",
            models=["fake-model"], options={}, enabled=True, is_default=True,
            created_by=f"user-{name}",
        ))
        db.add(SuperAssistantConversation(
            id=f"conv-{name}", owner_id=f"user-{name}", title=name,
            model_config_id=f"model-{name}",
        ))
        db.add(SuperAssistantMessage(
            id=f"user-msg-{name}", conversation_id=f"conv-{name}",
            role="user", content=user_content, status="complete",
        ))
        db.add(SuperAssistantMessage(
            id=f"assistant-msg-{name}", conversation_id=f"conv-{name}",
            role="assistant", content="", status="streaming",
        ))
        db.commit()
    return TestingSession, {
        "conversation_id": f"conv-{name}",
        "owner_id": f"user-{name}",
        "assistant_message_id": f"assistant-msg-{name}",
        "requested_model_id": f"model-{name}",
    }


def _stream_args(ids):
    return dict(
        conversation_id=ids["conversation_id"],
        owner_id=ids["owner_id"],
        assistant_message_id=ids["assistant_message_id"],
        requested_model_id=ids["requested_model_id"],
    )


def _fake_chat_stream(responses):
    def _fake(_call_kwargs, _messages, _tools, on_delta=None):
        result = next(responses)
        content = result.get("content")
        if content and on_delta:
            on_delta(content)
        return result

    return _fake


def _text(content, **usage):
    return {"content": content, "tool_calls": [], "usage": usage or {}}


def _make_skill(db, owner_id, name, *, always_active=False, use_count=0,
                enabled=True, body=None):
    """落一行 Skill 记录并写上真实 SKILL.md（常驻注入与 use_skill 都要读盘）。"""
    skill = SuperAssistantSkill(
        id=f"skill-{name}", owner_id=owner_id, name=name, display_name=name,
        description=f"{name} 描述", triggers=[], manifest=[], enabled=enabled,
        always_active=always_active, use_count=use_count,
    )
    folder = skill_directory(owner_id, skill.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        render_skill_markdown(
            name=name,
            description=f"{name} 描述",
            content=body or f"{name} 正文",
        ),
        encoding="utf-8",
    )
    skill.folder_path = str(folder)
    db.add(skill)
    return skill


def _capture_system_prompt(monkeypatch, seen):
    def _fake(_call_kwargs, messages, _tools, on_delta=None):
        seen.append(messages[0]["content"])
        return _text("收到。")

    monkeypatch.setattr(runtime.provider, "chat_stream", _fake)


def test_always_active_skill_inlined_into_system_prompt(tmp_path, monkeypatch):
    """常驻技能 SKILL.md 全文进系统提示；普通技能只留目录条目。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "resident")
    with TestingSession() as db:
        _make_skill(
            db, ids["owner_id"], "resident-skill",
            always_active=True, body="常驻技能正文：永远直接遵守。",
        )
        _make_skill(db, ids["owner_id"], "normal-skill", body="普通技能正文：不应出现。")
        _make_skill(
            db, ids["owner_id"], "off-skill",
            always_active=True, enabled=False, body="停用技能正文：不应出现。",
        )
        db.commit()
    seen: list = []
    _capture_system_prompt(monkeypatch, seen)

    "".join(runtime.stream_chat(**_stream_args(ids)))

    prompt = seen[0]
    # 常驻技能：catalog 条目 + 全文内联（### name 标题 + SKILL.md 内容）
    assert "- resident-skill: resident-skill 描述" in prompt
    assert "### resident-skill" in prompt
    assert "常驻技能正文：永远直接遵守。" in prompt
    # 普通技能：只有 catalog 条目，正文不进系统提示
    assert "- normal-skill: normal-skill 描述" in prompt
    assert "普通技能正文：不应出现。" not in prompt
    # 停用的常驻技能既不列目录也不内联
    assert "off-skill" not in prompt


def test_use_skill_success_increments_usage_stats(tmp_path, monkeypatch):
    """use_skill 成功（结果 JSON 无 error 键）后 use_count +1、记录 last_used_at。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "counter")
    with TestingSession() as db:
        _make_skill(db, ids["owner_id"], "counter-skill", body="计数技能正文。")
        db.commit()
    responses = iter([
        {
            "content": None,
            "tool_calls": [{"id": "c1", "name": "use_skill", "arguments": {"name": "counter-skill"}}],
            "usage": {},
        },
        _text("已读取。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))

    events = "".join(runtime.stream_chat(**_stream_args(ids)))

    assert "计数技能正文。" in events
    with TestingSession() as db:
        skill = db.query(SuperAssistantSkill).filter_by(name="counter-skill").one()
        assert skill.use_count == 1
        assert skill.last_used_at is not None


def test_use_skill_error_does_not_count(tmp_path, monkeypatch):
    """use_skill 命中不存在的 Skill（结果含 error 键）时不计数。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "miss")
    with TestingSession() as db:
        _make_skill(db, ids["owner_id"], "bystander-skill")
        db.commit()
    responses = iter([
        {
            "content": None,
            "tool_calls": [{"id": "c1", "name": "use_skill", "arguments": {"name": "ghost-skill"}}],
            "usage": {},
        },
        _text("没有这个技能。"),
    ])
    monkeypatch.setattr(runtime.provider, "chat_stream", _fake_chat_stream(responses))

    events = "".join(runtime.stream_chat(**_stream_args(ids)))

    assert "不存在或未启用" in events
    with TestingSession() as db:
        skill = db.query(SuperAssistantSkill).filter_by(name="bystander-skill").one()
        assert skill.use_count == 0
        assert skill.last_used_at is None


def test_catalog_orders_by_use_count_desc_then_name(tmp_path, monkeypatch):
    """目录降权：use_count 高者排前；同为零使用按 name 升序沉底。"""
    TestingSession, ids = _seed(tmp_path, monkeypatch, "order")
    with TestingSession() as db:
        _make_skill(db, ids["owner_id"], "aaa-fresh", use_count=0)
        _make_skill(db, ids["owner_id"], "zzz-fresh", use_count=0)
        _make_skill(db, ids["owner_id"], "mid-used", use_count=9)
        db.commit()
    seen: list = []
    _capture_system_prompt(monkeypatch, seen)

    "".join(runtime.stream_chat(**_stream_args(ids)))

    prompt = seen[0]
    assert prompt.index("- mid-used:") < prompt.index("- aaa-fresh:")
    assert prompt.index("- aaa-fresh:") < prompt.index("- zzz-fresh:")


def test_skill_service_always_active_roundtrip_and_out_contract(tmp_path, monkeypatch):
    """创建/更新接受 always_active；SkillOut 契约暴露三个治理字段。"""
    monkeypatch.setattr(settings, "super_assistant_skill_root", str(tmp_path / "svc-skills"))
    engine = create_engine(f"sqlite:///{tmp_path / 'svc.db'}")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, SuperAssistantSkill.__table__])
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        user = User(
            id="user-svc", username="svc", email="svc@example.com",
            password_hash="unused", role="editor",
        )
        db.add(user)
        db.commit()

        skill = skill_service.create_skill(
            SkillCreate(
                name="governed-skill",
                description="治理技能",
                content="按规矩办事。",
                always_active=True,
            ),
            db,
            user,
        )
        assert skill.always_active is True
        assert skill.use_count == 0
        assert skill.last_used_at is None

        updated = skill_service.update_skill(
            skill.id, SkillUpdate(always_active=False), db, user,
        )
        assert updated.always_active is False

        out = SkillOut.model_validate(updated).model_dump()
        assert out["always_active"] is False
        assert out["use_count"] == 0
        assert out["last_used_at"] is None

        # ZIP 导入路径不接受 always_active，默认 False
        assert SkillCreate(
            name="plain-skill", description="普通", content="内容",
        ).always_active is False
    engine.dispose()
