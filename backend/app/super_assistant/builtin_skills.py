"""平台内置 Skill：随代码发布的出厂技能，用户首次列出技能时幂等落库。

语义：内置 = 出厂预置。落库后与用户自建 Skill 同权治理（可停用、可编辑、
可查看文件）；删除后下次列出时自动恢复出厂版本。seed 只补缺、从不覆盖：
同名行已存在（用户自建或旧版）时保持原样，内置内容的升级由维护者运维处理。
"""
from __future__ import annotations

import logging
import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.super_assistant.models import SuperAssistantSkill
from app.super_assistant.skill_store import (
    SkillStoreError,
    build_manifest,
    create_skill_folder,
    delete_skill_folder,
    parse_skill_markdown,
    skill_directory,
    write_text_file,
)

logger = logging.getLogger(__name__)

# 包内资源目录：backend/app/super_assistant/builtin_skills/<目录名>/
BUILTIN_SKILL_DIRS = ("leader",)
_RESOURCE_ROOT = Path(__file__).parent / "builtin_skills"


@lru_cache(maxsize=1)
def _builtin_definitions() -> tuple[dict, ...]:
    """解析全部内置技能资源，进程内只读一次。

    每项含 name / description（取自 SKILL.md frontmatter）与 files
    （相对路径 → UTF-8 文本，首个必须是 SKILL.md）。
    """
    definitions: list[dict] = []
    for dir_name in BUILTIN_SKILL_DIRS:
        source = _RESOURCE_ROOT / dir_name
        skill_md = (source / "SKILL.md").read_text(encoding="utf-8")
        metadata = parse_skill_markdown(skill_md)
        files: list[tuple[str, str]] = [("SKILL.md", skill_md)]
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.name == "SKILL.md":
                continue
            files.append((
                path.relative_to(source).as_posix(),
                path.read_text(encoding="utf-8"),
            ))
        definitions.append({
            "name": metadata["name"],
            "description": metadata["description"],
            "files": files,
        })
    return tuple(definitions)


def builtin_skill_names() -> tuple[str, ...]:
    return tuple(definition["name"] for definition in _builtin_definitions())


def _materialize(db: Session, owner_id: str, definition: dict) -> None:
    """把一个内置技能写入用户的技能目录与数据库（模仿 create_skill 的回滚清理）。"""
    item = SuperAssistantSkill(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        name=definition["name"],
        display_name=definition["name"],
        description=definition["description"],
        triggers=[],
        folder_path="",
        enabled=True,
        always_active=False,
    )
    folder = skill_directory(owner_id, item.id)
    item.folder_path = str(folder)
    try:
        create_skill_folder(folder, definition["files"][0][1])
        for relative_path, content in definition["files"][1:]:
            write_text_file(folder, relative_path, content)
        item.manifest = build_manifest(folder)
        db.add(item)
        db.commit()
    except (SkillStoreError, IntegrityError, OSError) as exc:
        # 并发下同名行已被其他请求抢先落库（IntegrityError），或磁盘异常：
        # 回滚并清掉半成品目录，不让内置 seed 阻断技能列表。
        db.rollback()
        try:
            delete_skill_folder(folder)
        except SkillStoreError:
            pass
        logger.warning(
            "内置 Skill %s 落库失败（owner=%s）: %r",
            definition["name"], owner_id, exc,
        )


def seed_builtin_skills(db: Session, owner_id: str) -> None:
    """确保用户的内置技能已落库；幂等，全量已存在时只有一次索引查询。"""
    try:
        definitions = _builtin_definitions()
    except (SkillStoreError, OSError) as exc:
        # 资源损坏/缺失不该阻断技能列表；lru_cache 不缓存失败，修复后自动重试
        logger.warning("内置 Skill 资源加载失败，跳过 seed: %r", exc)
        return
    if not definitions:
        return
    existing = {
        name for (name,) in db.query(SuperAssistantSkill.name).filter(
            SuperAssistantSkill.owner_id == owner_id,
            SuperAssistantSkill.name.in_(builtin_skill_names()),
        ).all()
    }
    for definition in definitions:
        if definition["name"] in existing:
            continue
        _materialize(db, owner_id, definition)
