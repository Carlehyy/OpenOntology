"""内置出厂 Skill（builtin_skills）：首次列出幂等落库、可恢复、可被 use_skill 读取。

沿用 test_skill_governance 的隔离 sqlite 手法：每张用例自建引擎与会话，
技能根目录 monkeypatch 到 tmp_path，不触网。
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import builtin_skills, skill_service
from app.super_assistant.models import SuperAssistantSkill
from app.super_assistant.skill_tools import execute_skill_tool

_TABLES = [User.__table__, SuperAssistantSkill.__table__]
_LEADER_FILES = {"SKILL.md", "references/anatomy.md", "references/style.md"}


def _setup(tmp_path, monkeypatch, name):
    monkeypatch.setattr(
        settings, "super_assistant_skill_root", str(tmp_path / f"skills-{name}"),
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / name}.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(User(
            id=f"user-{name}", username=name, email=f"{name}@example.com",
            password_hash="unused", role="editor",
        ))
        db.commit()
    return TestingSession, f"user-{name}"


def test_list_skills_seeds_builtin_leader(tmp_path, monkeypatch):
    """首次列出：leader 落库、默认启用、manifest 覆盖全部资源文件。"""
    TestingSession, owner_id = _setup(tmp_path, monkeypatch, "seed")
    with TestingSession() as db:
        skills = skill_service.list_skills(db, _user(db, owner_id))
        by_name = {skill.name: skill for skill in skills}
        assert "leader" in by_name
        leader = by_name["leader"]
        assert leader.enabled is True
        assert leader.always_active is False
        assert leader.description
        assert {entry["path"] for entry in leader.manifest} == _LEADER_FILES


def _user(db, owner_id):
    return db.query(User).filter(User.id == owner_id).one()


def test_seed_is_idempotent(tmp_path, monkeypatch):
    """重复列出：不新建行、不改动既有行的 id 与描述。"""
    TestingSession, owner_id = _setup(tmp_path, monkeypatch, "idem")
    with TestingSession() as db:
        first = [s for s in skill_service.list_skills(db, _user(db, owner_id))
                 if s.name == "leader"]
        second = [s for s in skill_service.list_skills(db, _user(db, owner_id))
                  if s.name == "leader"]
        assert len(first) == 1 and len(second) == 1
        assert first[0].id == second[0].id
        assert first[0].description == second[0].description


def test_seed_never_overwrites_existing_row(tmp_path, monkeypatch):
    """同名行已存在（用户旧版）：seed 跳过，内容与描述保持用户版本。"""
    TestingSession, owner_id = _setup(tmp_path, monkeypatch, "keep")
    with TestingSession() as db:
        db.add(SuperAssistantSkill(
            id="skill-old", owner_id=owner_id, name="leader",
            display_name="leader", description="用户旧版描述", triggers=[],
            folder_path="/unused/legacy", manifest=[], enabled=False,
        ))
        db.commit()
        skills = skill_service.list_skills(db, _user(db, owner_id))
        leader = next(skill for skill in skills if skill.name == "leader")
        assert leader.id == "skill-old"
        assert leader.description == "用户旧版描述"
        assert leader.enabled is False


def test_deleted_builtin_skill_is_restored_on_next_list(tmp_path, monkeypatch):
    """删除内置 Skill 后再次列出：按出厂版本自动恢复（出厂预置语义）。"""
    TestingSession, owner_id = _setup(tmp_path, monkeypatch, "restore")
    with TestingSession() as db:
        seeded = next(
            skill for skill in skill_service.list_skills(db, _user(db, owner_id))
            if skill.name == "leader"
        )
        skill_service.remove_skill(seeded.id, db, _user(db, owner_id))
        assert db.query(SuperAssistantSkill).filter_by(
            owner_id=owner_id, name="leader",
        ).count() == 0

        restored = next(
            skill for skill in skill_service.list_skills(db, _user(db, owner_id))
            if skill.name == "leader"
        )
        assert restored.id != seeded.id
        assert {entry["path"] for entry in restored.manifest} == _LEADER_FILES


def test_builtin_skill_readable_via_skill_tools(tmp_path, monkeypatch):
    """seed 后 use_skill 返回 SKILL.md 全文与文件清单，read_skill_file 可读 references。"""
    TestingSession, owner_id = _setup(tmp_path, monkeypatch, "tools")
    with TestingSession() as db:
        skill_service.list_skills(db, _user(db, owner_id))
        payload = json.loads(
            execute_skill_tool(db, owner_id, "use_skill", {"name": "leader"}),
        )
        assert payload["skill"] == "leader"
        assert payload["skill_md"].startswith("---")
        assert "name: leader" in payload["skill_md"]
        assert {entry["path"] for entry in payload["files"]} == _LEADER_FILES

        reference = json.loads(execute_skill_tool(
            db, owner_id, "read_skill_file",
            {"name": "leader", "path": "references/anatomy.md"},
        ))
        assert "任务书" in reference["content"]


def test_builtin_skill_enters_runtime_catalog(tmp_path, monkeypatch):
    """seed 出的 leader 会出现在运行时 Skill 目录（system prompt 首段）。"""
    from app.super_assistant import runtime

    TestingSession, owner_id = _setup(tmp_path, monkeypatch, "catalog")
    with TestingSession() as db:
        skill_service.list_skills(db, _user(db, owner_id))
        skills = db.query(SuperAssistantSkill).filter(
            SuperAssistantSkill.owner_id == owner_id,
            SuperAssistantSkill.enabled.is_(True),
        ).all()
        prompt = runtime._system_prompt(skills)
        assert "- leader: " in prompt


def test_list_skills_survives_resource_failure(tmp_path, monkeypatch):
    """资源目录异常时 seed 只记日志，不阻断技能列表。"""
    builtin_skills._builtin_definitions.cache_clear()
    monkeypatch.setattr(
        builtin_skills, "_RESOURCE_ROOT", tmp_path / "missing-resources",
    )
    try:
        TestingSession, owner_id = _setup(tmp_path, monkeypatch, "broken")
        with TestingSession() as db:
            db.add(SuperAssistantSkill(
                id="skill-own", owner_id=owner_id, name="own-skill",
                display_name="own-skill", description="自建", triggers=[],
                folder_path="/unused", manifest=[], enabled=True,
            ))
            db.commit()
            skills = skill_service.list_skills(db, _user(db, owner_id))
            assert [skill.name for skill in skills] == ["own-skill"]
    finally:
        builtin_skills._builtin_definitions.cache_clear()


def test_builtin_definitions_match_store_contract():
    """资源经 parse_skill_markdown 校验，files 以 SKILL.md 打头、路径唯一。"""
    definitions = builtin_skills._builtin_definitions()
    assert builtin_skills.builtin_skill_names() == ("leader",)
    (definition,) = definitions
    assert definition["files"][0][0] == "SKILL.md"
    assert {path for path, _ in definition["files"]} == _LEADER_FILES
