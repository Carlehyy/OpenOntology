from __future__ import annotations

import io
import uuid
from collections.abc import Callable

from fastapi import HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.super_assistant.models import SuperAssistantSkill
from app.super_assistant.builtin_skills import seed_builtin_skills
from app.super_assistant.schemas import (
    SkillCreate,
    SkillFileContent,
    SkillUpdate,
)
from app.super_assistant.skill_store import (
    SkillStoreError,
    build_manifest,
    create_skill_folder,
    delete_file,
    delete_skill_folder,
    export_skill_archive,
    import_skill_archive,
    parse_skill_markdown,
    read_text_file,
    render_skill_markdown,
    skill_directory,
    write_text_file,
)


SkillLookup = Callable[
    [Session, str, str],
    SuperAssistantSkill,
]
StorageErrorMapper = Callable[[Exception], HTTPException]


def _skill(
    db: Session,
    owner_id: str,
    skill_id: str,
) -> SuperAssistantSkill:
    item = db.query(SuperAssistantSkill).filter(
        SuperAssistantSkill.id == skill_id,
        SuperAssistantSkill.owner_id == owner_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return item


def _storage_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def list_skills(
    db: Session,
    current_user: User,
) -> list[SuperAssistantSkill]:
    # 内置出厂 Skill 随首次列出自动补齐（幂等）；前端进入超级助手页面必先
    # GET /skills，因此这里即可覆盖全部真实用户链路。
    seed_builtin_skills(db, current_user.id)
    return db.query(SuperAssistantSkill).filter(
        SuperAssistantSkill.owner_id == current_user.id,
    ).order_by(SuperAssistantSkill.updated_at.desc()).all()


def create_skill(
    body: SkillCreate,
    db: Session,
    current_user: User,
) -> SuperAssistantSkill:
    item = SuperAssistantSkill(
        id=str(uuid.uuid4()),
        owner_id=current_user.id,
        name=body.name,
        # Retained columns keep existing databases migration-compatible. Skill
        # identity and activation come exclusively from standard SKILL.md data.
        display_name=body.name,
        description=body.description,
        triggers=[],
        folder_path="",
        enabled=body.enabled,
        always_active=body.always_active,
    )
    folder = skill_directory(current_user.id, item.id)
    item.folder_path = str(folder)
    markdown = render_skill_markdown(
        name=item.name,
        description=item.description,
        content=body.content,
    )
    try:
        create_skill_folder(folder, markdown)
        item.manifest = build_manifest(folder)
        db.add(item)
        db.commit()
        db.refresh(item)
        return item
    except (SkillStoreError, IntegrityError) as exc:
        db.rollback()
        try:
            delete_skill_folder(folder)
        except SkillStoreError:
            pass
        detail = (
            "同名 Skill 已存在"
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        raise HTTPException(
            status_code=(
                409 if isinstance(exc, IntegrityError) else 400
            ),
            detail=detail,
        ) from exc


async def import_skill(
    archive: UploadFile,
    db: Session,
    current_user: User,
) -> SuperAssistantSkill:
    if not (archive.filename or "").lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="请上传 .zip 格式的 Skill 文件夹",
        )
    from app.shared.config import settings

    limit = settings.super_assistant_max_skill_archive_mb * 1024 * 1024
    data = await archive.read(limit + 1)
    item = SuperAssistantSkill(
        id=str(uuid.uuid4()),
        owner_id=current_user.id,
        name="pending",
        display_name="pending",
        folder_path="",
    )
    folder = skill_directory(current_user.id, item.id)
    item.folder_path = str(folder)
    try:
        metadata = import_skill_archive(data, folder)
        item.name = metadata["name"]
        item.display_name = metadata["name"]
        item.description = metadata["description"]
        item.triggers = []
        item.manifest = build_manifest(folder)
        db.add(item)
        db.commit()
        db.refresh(item)
        return item
    except (SkillStoreError, IntegrityError) as exc:
        db.rollback()
        try:
            delete_skill_folder(folder)
        except SkillStoreError:
            pass
        detail = (
            "同名 Skill 已存在"
            if isinstance(exc, IntegrityError)
            else str(exc)
        )
        raise HTTPException(
            status_code=(
                409 if isinstance(exc, IntegrityError) else 400
            ),
            detail=detail,
        ) from exc


def update_skill(
    skill_id: str,
    body: SkillUpdate,
    db: Session,
    current_user: User,
    *,
    skill_lookup_fn: SkillLookup = _skill,
) -> SuperAssistantSkill:
    item = skill_lookup_fn(db, current_user.id, skill_id)
    if body.enabled is not None:
        item.enabled = body.enabled
    if body.always_active is not None:
        item.always_active = body.always_active
    db.commit()
    db.refresh(item)
    return item


def remove_skill(
    skill_id: str,
    db: Session,
    current_user: User,
    *,
    skill_lookup_fn: SkillLookup = _skill,
    storage_error_fn: StorageErrorMapper = _storage_error,
) -> Response:
    item = skill_lookup_fn(db, current_user.id, skill_id)
    try:
        delete_skill_folder(
            skill_directory(current_user.id, item.id),
        )
    except SkillStoreError as exc:
        raise storage_error_fn(exc) from exc
    db.delete(item)
    db.commit()
    return Response(status_code=204)


def list_skill_files(
    skill_id: str,
    db: Session,
    current_user: User,
    *,
    skill_lookup_fn: SkillLookup = _skill,
    storage_error_fn: StorageErrorMapper = _storage_error,
) -> list[dict]:
    item = skill_lookup_fn(db, current_user.id, skill_id)
    try:
        item.manifest = build_manifest(
            skill_directory(current_user.id, item.id),
        )
        db.commit()
        return item.manifest
    except SkillStoreError as exc:
        raise storage_error_fn(exc) from exc


def get_skill_file(
    skill_id: str,
    file_path: str,
    db: Session,
    current_user: User,
    *,
    skill_lookup_fn: SkillLookup = _skill,
    storage_error_fn: StorageErrorMapper = _storage_error,
) -> dict[str, str]:
    item = skill_lookup_fn(db, current_user.id, skill_id)
    try:
        folder = skill_directory(current_user.id, item.id)
        return {
            "path": file_path,
            "content": read_text_file(folder, file_path),
        }
    except SkillStoreError as exc:
        raise storage_error_fn(exc) from exc


def put_skill_file(
    skill_id: str,
    file_path: str,
    body: SkillFileContent,
    db: Session,
    current_user: User,
    *,
    skill_lookup_fn: SkillLookup = _skill,
    storage_error_fn: StorageErrorMapper = _storage_error,
) -> dict:
    item = skill_lookup_fn(db, current_user.id, skill_id)
    folder = skill_directory(current_user.id, item.id)
    try:
        metadata = (
            parse_skill_markdown(body.content)
            if file_path == "SKILL.md"
            else None
        )
        if metadata and metadata["name"] != item.name:
            duplicate = db.query(SuperAssistantSkill).filter(
                SuperAssistantSkill.owner_id == current_user.id,
                SuperAssistantSkill.name == metadata["name"],
                SuperAssistantSkill.id != item.id,
            ).first()
            if duplicate:
                raise SkillStoreError("同名 Skill 已存在")
        write_text_file(folder, file_path, body.content)
        manifest = build_manifest(folder)
        from app.shared.config import settings

        if len(manifest) > settings.super_assistant_max_skill_files:
            if not any(
                entry["path"] == file_path
                for entry in item.manifest
            ):
                delete_file(folder, file_path)
            raise SkillStoreError("Skill 文件数量超过限制")
        if metadata:
            item.name = metadata["name"]
            item.display_name = metadata["name"]
            item.description = metadata["description"]
            item.triggers = []
        item.manifest = manifest
        item.revision += 1
        db.commit()
        return {
            "path": file_path,
            "revision": item.revision,
            "manifest": manifest,
        }
    except SkillStoreError as exc:
        db.rollback()
        raise storage_error_fn(exc) from exc


def remove_skill_file(
    skill_id: str,
    file_path: str,
    db: Session,
    current_user: User,
    *,
    skill_lookup_fn: SkillLookup = _skill,
    storage_error_fn: StorageErrorMapper = _storage_error,
) -> Response:
    item = skill_lookup_fn(db, current_user.id, skill_id)
    try:
        folder = skill_directory(current_user.id, item.id)
        delete_file(folder, file_path)
        item.manifest = build_manifest(folder)
        item.revision += 1
        db.commit()
        return Response(status_code=204)
    except SkillStoreError as exc:
        db.rollback()
        raise storage_error_fn(exc) from exc


def export_skill(
    skill_id: str,
    db: Session,
    current_user: User,
    *,
    skill_lookup_fn: SkillLookup = _skill,
    storage_error_fn: StorageErrorMapper = _storage_error,
) -> StreamingResponse:
    item = skill_lookup_fn(db, current_user.id, skill_id)
    try:
        payload = export_skill_archive(
            skill_directory(current_user.id, item.id),
        )
    except SkillStoreError as exc:
        raise storage_error_fn(exc) from exc
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{item.name}.zip"'
            ),
        },
    )
