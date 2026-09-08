"""记忆宫殿服务：用户级文件库 + 知识图谱抽取编排。

参照 semantica 的核心管线原生实现（复用平台设施）：

- Ingest/Parse：复用 steward SessionWorkspace 的多格式文档抽取；
- Split：段落感知分块（超长段硬切带 overlap）；
- Extract：LLM 严格 JSON 输出实体/关系（与对话共用模型选型）；
- 实体消解/增量：palace_graph 的 merge_key MERGE + 文件级溯源剥离重建；
- 检索：词法锚点 + 一跳邻域（平台无向量设施，双通道注入/只读工具消费）。

抽取是分钟级长任务：上传/重建只投递消息（NATS 优先，无 NATS 部署形态
降级为守护线程内联），执行侧以 (file_id, sha256) 成功记录幂等。
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.data_channel.pipeline_tasks.dispatch import dispatch_super_assistant_palace_extract
from app.data_channel.steward.workspace import SessionWorkspace, WorkspaceError
from app.model_configs.selector import llm_call_kwargs, select_llm_model_config, usage_tags
from app.shared.config import settings
from app.shared.database import SessionLocal
from app.super_assistant import palace_graph, palace_workspace, provider, reflection_service
from app.super_assistant.models import (
    SuperAssistantPalaceBuild,
    SuperAssistantPalaceFile,
    SuperAssistantPalaceFolder,
)

logger = logging.getLogger(__name__)

# 单文件抽取的块数与单块长度上限：超长文档截断并在 build 行记录块数
_CHUNK_SIZE = 2400
_CHUNK_OVERLAP = 200
_MAX_CHUNKS = 60
# 注入 system prompt 的图谱段预算（字符）：与附件段同思路的硬边界
_SECTION_BUDGET = 6000
# running 超过该时长视为进程中断，允许重新领取
_STALE_RUNNING = timedelta(minutes=30)

_EXTRACT_PROMPT = """你是知识图谱抽取引擎。请从下面的文档片段中抽取实体和它们之间的关系。
要求：
1. 实体 name 使用原文中的规范名称；type 从这些类别中选一个：人物/组织/机构/地点/时间/概念/技术/产品/项目/事件/其他。
2. aliases 是该实体在文中的其它称谓（没有就给空数组）。
3. relation 用简短的中文短语表达（如：任职、负责、属于、位于、使用、包含）。
4. 只抽取文中明确出现的内容，不要发明；source 和 target 必须是你在 entities 里给出的实体名。
5. 每个片段最多抽取 20 个实体、15 条关系；跳过与文档主题无关的细节。

输出严格 JSON（不要任何解释、不要 markdown 围栏）：
{{"entities":[{{"name":"实体名","type":"类别","aliases":["别名"]}}],"relations":[{{"source":"实体A","target":"实体B","relation":"关系"}}]}}

文档片段：
{chunk}"""


# ---------------------------------------------------------------------------
# 分块与抽取结果清洗
# ---------------------------------------------------------------------------


def _clip_str(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def split_chunks(
    text: str,
    *,
    size: int = _CHUNK_SIZE,
    overlap: int = _CHUNK_OVERLAP,
    max_chunks: int = _MAX_CHUNKS,
) -> list[str]:
    """段落感知分块：优先按空行段落聚合，超长段硬切保留 overlap 尾巴。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n{2,}", cleaned):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) + 2 <= size:
            current = f"{current}\n\n{paragraph}"
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > size:
            chunks.append(paragraph[:size])
            paragraph = paragraph[size - overlap:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks[:max_chunks]


def _sanitize_chunk_entities(owner_id: str, payload: dict) -> list[dict]:
    """单块实体清洗：去空白、限长、按 merge_key 去重。"""
    entities: list[dict] = []
    seen: set[str] = set()
    for item in payload.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = _clip_str(item.get("name"), 80)
        if not name:
            continue
        key = palace_graph.entity_key(owner_id, name)
        if key in seen:
            continue
        seen.add(key)
        aliases = [
            alias
            for alias in (_clip_str(value, 40) for value in (item.get("aliases") or []))
            if alias
        ][:6]
        entities.append({
            "key": key,
            "name": name,
            "type": _clip_str(item.get("type"), 30) or "其他",
            "aliases": aliases,
            "mentions": 1,
        })
    return entities


def _resolve_relations(
    owner_id: str,
    entity_map: dict[str, dict],
    raw_relations: list[dict],
) -> list[dict]:
    """关系端点必须落在全文件累计的实体集内（跨块可引用），按 merge_key 去重。"""
    resolved: list[dict] = []
    seen: set[str] = set()
    for item in raw_relations:
        source = _clip_str(item.get("source"), 80)
        target = _clip_str(item.get("target"), 80)
        relation = _clip_str(item.get("relation"), 40)
        if not source or not target or not relation:
            continue
        if palace_graph.normalize_name(source) == palace_graph.normalize_name(target):
            continue
        src_key = palace_graph.entity_key(owner_id, source)
        tgt_key = palace_graph.entity_key(owner_id, target)
        key = palace_graph.relation_key(owner_id, source, relation, target)
        if key in seen or src_key not in entity_map or tgt_key not in entity_map:
            continue
        seen.add(key)
        resolved.append({"key": key, "src_key": src_key, "tgt_key": tgt_key, "name": relation})
    return resolved


def extract_chunk(call_kwargs: dict, chunk: str) -> dict:
    """单块 LLM 抽取：要求严格 JSON，宽松解析（复用反思链路的解析器）。"""
    result = provider.chat(
        call_kwargs,
        [{"role": "user", "content": _EXTRACT_PROMPT.format(chunk=chunk)}],
        [],
    )
    return reflection_service._parse_json_loose(str(result.get("content") or ""))


# ---------------------------------------------------------------------------
# 别名级实体归一（抽取路径内，semantica DuplicateDetector 的最小实现）
# ---------------------------------------------------------------------------


def _alias_host(entity: dict, candidates: list[dict], alive: set[str]) -> dict | None:
    """为 entity 寻找并入目标：alias 规范名命中对方的 name 或 alias 规范名。"""
    alias_names = {
        normalized
        for alias in (entity.get("aliases") or [])
        if (normalized := palace_graph.normalize_name(alias))
    }
    if not alias_names:
        return None
    for candidate in candidates:
        if candidate is entity or candidate["key"] not in alive:
            continue
        names = {palace_graph.normalize_name(candidate.get("name") or "")}
        names.update(
            palace_graph.normalize_name(alias) for alias in (candidate.get("aliases") or [])
        )
        if alias_names & names:
            return candidate
    return None


def _absorb_entity(host: dict, absorbed: dict) -> None:
    """把 absorbed 并入 host（就地变更）：mentions 相加、name 保留更长者、
    aliases 并集去重——被并入者的 name 一并记为别名，保住其可检索性，
    也让关系端点的归一重定向有据可查。
    """
    absorbed_name = str(absorbed.get("name") or "")
    if len(absorbed_name) > len(str(host.get("name") or "")):
        host["name"] = absorbed_name
    host["mentions"] = int(host.get("mentions") or 0) + int(absorbed.get("mentions") or 0)
    seen: set[str] = set()
    final_name = palace_graph.normalize_name(host.get("name"))
    aliases: list[str] = []
    for value in [*(host.get("aliases") or []), absorbed_name, *(absorbed.get("aliases") or [])]:
        text = str(value or "").strip()
        normalized = palace_graph.normalize_name(text)
        if not text or not normalized or normalized == final_name or normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(text)
    host["aliases"] = aliases


def _merge_alias_entities(entities: list[dict]) -> list[dict]:
    """别名级实体归一（纯函数）：A 的某个 alias 规范名 == B 的 name（或 B 的
    alias）规范名时把 A 并入 B。单遍扫描存活者，迭代至收敛，至多 3 轮防环。
    """
    merged = [dict(item) for item in entities if isinstance(item, dict) and item.get("key")]
    for _ in range(3):
        alive = {item["key"] for item in merged}
        changed = False
        for entity in merged:
            if entity["key"] not in alive:
                continue
            host = _alias_host(entity, merged, alive)
            if host is None:
                continue
            _absorb_entity(host, entity)
            alive.discard(entity["key"])
            changed = True
        if not changed:
            break
        merged = [item for item in merged if item["key"] in alive]
    return merged


def _redirect_relation_endpoints(
    owner_id: str,
    raw_relations: list[dict],
    entity_map: dict[str, dict],
) -> list[dict]:
    """归一后被并入实体的旧名出现在关系端点时，改写为幸存者的规范名。"""
    lookup: dict[str, str] = {}
    for entity in entity_map.values():
        display = str(entity.get("name") or "")
        for token in [display, *(entity.get("aliases") or [])]:
            normalized = palace_graph.normalize_name(token)
            if normalized and normalized not in lookup:
                lookup[normalized] = display
    rewritten: list[dict] = []
    for item in raw_relations:
        source = _clip_str(item.get("source"), 80)
        target = _clip_str(item.get("target"), 80)
        if source and palace_graph.entity_key(owner_id, source) not in entity_map:
            source = lookup.get(palace_graph.normalize_name(source), source)
        if target and palace_graph.entity_key(owner_id, target) not in entity_map:
            target = lookup.get(palace_graph.normalize_name(target), target)
        rewritten.append({**item, "source": source, "target": target})
    return rewritten


# ---------------------------------------------------------------------------
# 幂等锚点（与 reflection_runs 同一模式）
# ---------------------------------------------------------------------------


def _latest_success_build(db: Session, file_id: str, content_hash: str) -> SuperAssistantPalaceBuild | None:
    return (
        db.query(SuperAssistantPalaceBuild)
        .filter(
            SuperAssistantPalaceBuild.file_id == file_id,
            SuperAssistantPalaceBuild.content_hash == content_hash,
            SuperAssistantPalaceBuild.status == "success",
        )
        .order_by(SuperAssistantPalaceBuild.created_at.desc())
        .first()
    )


def _active_running_build(db: Session, file_id: str) -> SuperAssistantPalaceBuild | None:
    """30 分钟内的 running 视为在途执行；更早的判定为进程中断并收口为 error。"""
    threshold = datetime.now(timezone.utc) - _STALE_RUNNING
    stale: list[SuperAssistantPalaceBuild] = []
    running: SuperAssistantPalaceBuild | None = None
    rows = (
        db.query(SuperAssistantPalaceBuild)
        .filter(SuperAssistantPalaceBuild.file_id == file_id, SuperAssistantPalaceBuild.status == "running")
        .order_by(SuperAssistantPalaceBuild.created_at.desc())
        .all()
    )
    for row in rows:
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
        if created >= threshold:
            running = running or row
        else:
            stale.append(row)
    for row in stale:
        row.status = "error"
        row.error = "执行进程中断（超时回收）"
        row.finished_at = datetime.now(timezone.utc)
    if stale:
        db.commit()
    return running


def _new_build(db: Session, owner_id: str, file_id: str, content_hash: str) -> SuperAssistantPalaceBuild:
    build = SuperAssistantPalaceBuild(
        owner_id=owner_id, file_id=file_id, content_hash=content_hash, status="running",
    )
    db.add(build)
    db.commit()
    db.refresh(build)
    return build


# ---------------------------------------------------------------------------
# 抽取执行（NATS handler / 内联降级共用）
# ---------------------------------------------------------------------------

_PALACE_MODEL_TAG = "super_assistant_palace"


def _palace_call_kwargs(db: Session) -> dict:
    """记忆宫殿抽取的模型选型：super_assistant_palace 专用 tag 优先。

    selector 的 purpose_tags 只是偏好而非硬过滤——tag 未命中时返回默认
    文本模型，仅在没有任何启用中的文本模型时返回 None；因此命中判定必须
    校验返回配置确实携带 palace tag。未命中回退 super_assistant tag（与
    反思链路共用语义），两者都无可用配置时抛 ProviderError。
    """
    palace_config = select_llm_model_config(
        db=db, purpose_tags=(_PALACE_MODEL_TAG,), allow_vlm=False,
    )
    if palace_config is not None and _PALACE_MODEL_TAG in usage_tags(palace_config):
        call_kwargs = llm_call_kwargs(palace_config)
        if call_kwargs:
            return call_kwargs
    model_config = select_llm_model_config(
        db=db, purpose_tags=("super_assistant",), allow_vlm=False,
    )
    call_kwargs = llm_call_kwargs(model_config)
    if not call_kwargs:
        raise provider.ProviderError("没有可用的文本模型，请先到“模型配置”启用一个 LLM")
    return call_kwargs


def run_build(db: Session, owner_id: str, file_id: str) -> SuperAssistantPalaceBuild:
    """执行一次图谱抽取；异常记入 build/file 行，不向上抛。"""
    file_row = db.get(SuperAssistantPalaceFile, file_id)
    if file_row is None or file_row.owner_id != owner_id:
        raise ValueError("记忆宫殿文件不存在")
    existing = _latest_success_build(db, file_id, file_row.sha256)
    if existing is not None and file_row.status == "built":
        return existing
    if (running := _active_running_build(db, file_id)) is not None:
        return running

    build = _new_build(db, owner_id, file_id, file_row.sha256)
    # 是否剥离旧图谱贡献以「存在任意 hash 的成功 build 记录」为准，而不是
    # 当前行状态：内容更新/替换路径会把状态重置为 pending，按状态判定会
    # 漏剥离、新旧实体在图中并存（生产实测发现）。
    was_built = (
        db.query(SuperAssistantPalaceBuild.id)
        .filter(
            SuperAssistantPalaceBuild.file_id == file_id,
            SuperAssistantPalaceBuild.status == "success",
        )
        .first()
        is not None
    )
    file_row.status = "building"
    file_row.error = None
    db.commit()
    try:
        text = palace_workspace.user_workspace(owner_id).extracted_text(
            palace_workspace.user_dir_id(owner_id), file_row.artifact_id, cap=200_000,
        )
        if not text.strip():
            raise ValueError("文件没有可抽取的文本（解析结果为空）")
        chunks = split_chunks(text)
        if not chunks:
            raise ValueError("文件没有可抽取的文本（解析结果为空）")
        call_kwargs = _palace_call_kwargs(db)
        entity_map: dict[str, dict] = {}
        raw_relations: list[dict] = []
        parsed_chunks = 0
        last_chunk_error: str | None = None
        for chunk in chunks:
            try:
                payload = extract_chunk(call_kwargs, chunk)
            except Exception as exc:
                last_chunk_error = str(exc)
                logger.warning("记忆宫殿抽取块失败（file=%s），跳过该块", file_id, exc_info=True)
                continue
            parsed_chunks += 1
            for entity in _sanitize_chunk_entities(owner_id, payload):
                known = entity_map.get(entity["key"])
                if known is None:
                    entity_map[entity["key"]] = entity
                else:
                    known["mentions"] += 1
            raw_relations.extend(
                item for item in (payload.get("relations") or []) if isinstance(item, dict)
            )
        if parsed_chunks == 0:
            raise ValueError(f"全部文档片段抽取失败：{last_chunk_error or '模型输出无法解析'}")
        # 别名级归一：跨块累计完成后、关系解析之前；幸存者按最终 name 重算
        # merge key（key 与展示名一致），被并入实体的旧名出现在关系端点时
        # 重定向到幸存者，避免关系被累计实体集丢弃
        merged_map: dict[str, dict] = {}
        for entity in _merge_alias_entities(list(entity_map.values())):
            entity["key"] = palace_graph.entity_key(owner_id, str(entity.get("name") or ""))
            merged_map[entity["key"]] = entity
        entity_map = merged_map
        raw_relations = _redirect_relation_endpoints(owner_id, raw_relations, entity_map)
        relations = _resolve_relations(owner_id, entity_map, raw_relations)

        # 增量重建：仅此前建过图的文件需要先剥离旧贡献（首建为无操作跳过）
        if was_built:
            palace_graph.remove_file_graph(owner_id, file_id, file_row.filename)
        entity_count, relation_count = palace_graph.merge_extraction(
            owner_id, file_id, file_row.filename, list(entity_map.values()), relations,
        )
    except Exception as exc:
        db.rollback()
        build = db.get(SuperAssistantPalaceBuild, build.id) or build
        file_row = db.get(SuperAssistantPalaceFile, file_id) or file_row
        build.status = "error"
        build.error = str(exc)[:2000]
        build.finished_at = datetime.now(timezone.utc)
        file_row.status = "failed"
        file_row.error = str(exc)[:2000]
        db.commit()
        logger.warning("记忆宫殿抽取失败（file=%s）: %s", file_id, exc)
        return build

    build.status = "success"
    build.chunk_count = len(chunks)
    build.entity_count = entity_count
    build.relation_count = relation_count
    build.finished_at = datetime.now(timezone.utc)
    file_row.status = "built"
    file_row.error = None
    file_row.entity_count = entity_count
    file_row.relation_count = relation_count
    db.commit()
    return build


def request_build(file_row: SuperAssistantPalaceFile) -> dict:
    """投递抽取任务：NATS 优先；未配置 NATS_URL 的部署形态降级为守护线程。

    上传/重建请求绝不同步执行分钟级抽取。
    """
    try:
        dispatch_super_assistant_palace_extract(file_row.owner_id, file_row.id)
        return {"dispatched": True}
    except RuntimeError as exc:
        if "NATS_URL" not in str(exc):
            raise

    def _inline() -> None:
        build_db = SessionLocal()
        try:
            run_build(build_db, file_row.owner_id, file_row.id)
        except Exception:
            logger.exception("内联记忆宫殿抽取失败（file=%s）", file_row.id)
        finally:
            build_db.close()

    threading.Thread(target=_inline, daemon=True, name="sa-palace-build").start()
    return {"dispatched": False}


# ---------------------------------------------------------------------------
# HTTP service 层
# ---------------------------------------------------------------------------

# 在线编辑只放开纯文本类（md/txt），其余类型走替换上传
_EDITABLE_EXTENSIONS = ("md", "txt")

# 图片只入库+预览（中栏原图查看），不参与图谱抽取：文件行直接定格 built，
# 不派发抽取任务。将来接入 VLM 视觉抽取时以模型配置 purpose tag 扩展。
_IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp")


class PalaceContentUpdate(BaseModel):
    content: str = Field(max_length=5_000_000)


class _PalaceBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class PalaceFolderCreate(_PalaceBody):
    path: str = Field(min_length=1, max_length=600)


class PalaceFolderRename(_PalaceBody):
    path: str = Field(min_length=1, max_length=600)


class PalaceFileMove(_PalaceBody):
    folder_path: str = Field(default="", alias="folderPath", max_length=600)


class PalaceNoteCreate(_PalaceBody):
    filename: str = Field(min_length=1, max_length=255)
    folder_path: str = Field(default="", alias="folderPath", max_length=600)


def _extension_of(filename: str | None) -> str:
    return os.path.splitext(str(filename or ""))[1].lower().lstrip(".")


def _is_editable(filename: str | None) -> bool:
    return _extension_of(filename) in _EDITABLE_EXTENSIONS


def _is_image(filename: str | None) -> bool:
    return _extension_of(filename) in _IMAGE_EXTENSIONS


def _allowed_extensions() -> set[str]:
    return {
        item.strip().lower()
        for item in settings.allowed_upload_extensions.split(",")
        if item.strip()
    }


def _palace_allowed_extensions() -> set[str]:
    """记忆宫殿白名单 = 文档白名单 ∪ 图片（图片仅存储与预览）。"""
    return _allowed_extensions() | set(_IMAGE_EXTENSIONS)


def _validate_upload_extension(filename: str | None) -> None:
    extension = _extension_of(filename)
    if extension not in _palace_allowed_extensions():
        raise HTTPException(
            400,
            (
                f"不支持的文件类型 .{extension}"
                f"（允许: {settings.allowed_upload_extensions}"
                f",{','.join(_IMAGE_EXTENSIONS)}）"
            ),
        )


def _check_quotas(
    db: Session,
    owner_id: str,
    *,
    extra_bytes: int = 0,
    extra_files: int = 0,
    reserves_build: bool = True,
) -> None:
    """上传/替换/在线编辑/重建/批量入口的统一配额闸，超限抛 429。

    extra_files/extra_bytes 是本次操作将新增的文件数与字节数（批量导入逐条
    调用，DB 计数随提交实时增长）。reserves_build 表示该文件将触发抽取，
    在途与每小时配额按本次 +1 预留；图片只入库不抽取，不占用这两项。
    PUT 内容未变的幂等路径不经过本闸。
    """
    max_files = int(settings.super_assistant_palace_max_files_per_user)
    if extra_files > 0:
        current = (
            db.query(SuperAssistantPalaceFile)
            .filter(SuperAssistantPalaceFile.owner_id == owner_id)
            .count()
        )
        if current + extra_files > max_files:
            raise HTTPException(429, f"记忆宫殿文件数已达上限（{max_files}），请删除部分文件后再试")
    if extra_bytes > 0:
        total = int(
            db.query(func.coalesce(func.sum(SuperAssistantPalaceFile.size), 0))
            .filter(SuperAssistantPalaceFile.owner_id == owner_id)
            .scalar()
            or 0
        )
        max_total_mb = int(settings.super_assistant_palace_max_total_mb)
        if total + extra_bytes > max_total_mb * 1024 * 1024:
            raise HTTPException(429, f"记忆宫殿存储已达上限（{max_total_mb} MB）")
    if not reserves_build:
        return
    max_in_flight = int(settings.super_assistant_palace_max_in_flight)
    in_flight = (
        db.query(SuperAssistantPalaceFile)
        .filter(
            SuperAssistantPalaceFile.owner_id == owner_id,
            SuperAssistantPalaceFile.status.in_(("pending", "building")),
        )
        .count()
    )
    if in_flight + max(1, extra_files) > max_in_flight:
        raise HTTPException(429, f"抽取队列已满（{max_in_flight} 个进行中），请等待完成后再提交")
    max_builds = int(settings.super_assistant_palace_max_builds_per_hour)
    recent_builds = (
        db.query(SuperAssistantPalaceBuild)
        .filter(
            SuperAssistantPalaceBuild.owner_id == owner_id,
            SuperAssistantPalaceBuild.created_at >= datetime.now(timezone.utc) - timedelta(hours=1),
        )
        .count()
    )
    if recent_builds >= max_builds:
        raise HTTPException(429, f"抽取任务过于频繁（每小时上限 {max_builds} 次），请稍后再试")


def _file_dict(row: SuperAssistantPalaceFile) -> dict:
    return {
        "id": row.id,
        "filename": row.filename,
        "path": row.folder_path,
        "mimeType": row.mime_type,
        "size": row.size,
        "sha256": row.sha256,
        "extractedChars": row.extracted_chars,
        "status": row.status,
        "error": row.error,
        "entityCount": row.entity_count,
        "relationCount": row.relation_count,
        "editable": _is_editable(row.filename),
        "isImage": _is_image(row.filename),
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }


def list_files(db: Session, owner_id: str) -> list[dict]:
    rows = (
        db.query(SuperAssistantPalaceFile)
        .filter(SuperAssistantPalaceFile.owner_id == owner_id)
        .order_by(SuperAssistantPalaceFile.created_at.desc())
        .all()
    )
    return [_file_dict(row) for row in rows]


def _owned_file(db: Session, owner_id: str, file_id: str) -> SuperAssistantPalaceFile:
    row = db.get(SuperAssistantPalaceFile, file_id)
    if row is None or row.owner_id != owner_id:
        raise HTTPException(404, "记忆宫殿文件不存在")
    return row


def _discard_artifact(workspace: SessionWorkspace, dir_id: str, artifact_id: str) -> None:
    """配额拒绝后回滚工作区文件，尽量不留孤儿 artifact。"""
    try:
        workspace.delete_file(dir_id, artifact_id)
    except WorkspaceError:
        logger.warning("记忆宫殿配额回滚删除工作区文件失败（artifact=%s）", artifact_id)


def _create_file_row(
    db: Session, owner_id: str, artifact: dict, *, fallback_name: str, folder_path: str = "",
) -> SuperAssistantPalaceFile:
    filename = str(artifact.get("filename") or fallback_name)
    image = _is_image(filename)
    row = SuperAssistantPalaceFile(
        owner_id=owner_id,
        filename=filename,
        folder_path=folder_path[:500],
        artifact_id=str(artifact.get("id")),
        mime_type=str(artifact.get("mimeType") or "application/octet-stream"),
        size=int(artifact.get("size") or 0),
        sha256=str(artifact.get("sha256") or ""),
        extracted_chars=int(artifact.get("extractedChars") or 0),
        # 图片不参与抽取：直接定格 built，不派发任务
        status="built" if image else "pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if not image:
        request_build(row)
    return row


def upload_file(db: Session, current_user: User, upload: UploadFile, raw_folder_path: str = "") -> dict:
    _validate_upload_extension(upload.filename)
    owner_id = current_user.id
    # folder_path 非空时上传归位到该目录（前端「上传到选中目录」），目录不存在按 mkdir -p 补行
    folder_path = _validate_folder_path(_normalize_folder_path(raw_folder_path), allow_root=True)
    if folder_path:
        _ensure_folder_rows(db, owner_id, folder_path, include_self=True)
    dir_id = palace_workspace.user_dir_id(owner_id)
    workspace = palace_workspace.user_workspace(owner_id)
    image = _is_image(upload.filename)
    try:
        artifact = workspace.save_stream(
            dir_id,
            upload.filename or "palace-file",
            upload.file,
            source="palace-upload",
            mime_type=upload.content_type,
            extract=not image,
        )
    except WorkspaceError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        _check_quotas(
            db, owner_id,
            extra_bytes=int(artifact.get("size") or 0), extra_files=1,
            reserves_build=not image,
        )
    except HTTPException:
        _discard_artifact(workspace, dir_id, str(artifact.get("id")))
        raise
    return _file_dict(_create_file_row(
        db, owner_id, artifact, fallback_name=upload.filename or "palace-file",
        folder_path=folder_path,
    ))


def _replace_artifact_and_rebuild(
    db: Session,
    row: SuperAssistantPalaceFile,
    data: bytes,
    *,
    filename: str,
    source: str,
    mime_type: str | None,
) -> None:
    """workspace 侧删旧存新 + DB 行重置 + 派发抽取；图片定格 built 不派发。"""
    # 先卡大小再删旧 artifact，避免落盘失败把文件行留在坏状态
    if len(data) > int(settings.max_upload_mb) * 1024 * 1024:
        raise HTTPException(422, f"文件超过大小限制 {settings.max_upload_mb}MB")
    workspace = palace_workspace.user_workspace(row.owner_id)
    dir_id = palace_workspace.user_dir_id(row.owner_id)
    image = _is_image(filename)
    try:
        workspace.delete_file(dir_id, row.artifact_id)
        artifact = workspace.save_bytes(
            dir_id, filename, data, source=source, mime_type=mime_type, extract=not image,
        )
    except WorkspaceError as exc:
        raise HTTPException(422, str(exc)) from exc
    row.filename = str(artifact.get("filename") or filename)
    row.artifact_id = str(artifact.get("id"))
    row.mime_type = str(artifact.get("mimeType") or row.mime_type or "application/octet-stream")
    row.size = int(artifact.get("size") or 0)
    row.sha256 = str(artifact.get("sha256") or "")
    row.extracted_chars = int(artifact.get("extractedChars") or 0)
    if image:
        row.status = "built"
        row.error = None
        row.entity_count = 0
        row.relation_count = 0
    else:
        row.status = "pending"
        row.error = None
    db.commit()
    db.refresh(row)
    if not image:
        request_build(row)


def update_file_content(db: Session, owner_id: str, file_id: str, content: str) -> dict:
    """在线编辑（仅 md/txt）：内容未变时幂等返回，不重建也不消耗配额。"""
    row = _owned_file(db, owner_id, file_id)
    if not _is_editable(row.filename):
        raise HTTPException(400, "该文件类型不支持在线编辑，请使用替换上传")
    data = content.encode("utf-8")
    if hashlib.sha256(data).hexdigest() == row.sha256:
        return _file_dict(row)
    _check_quotas(db, owner_id, extra_bytes=len(data) - row.size)
    _replace_artifact_and_rebuild(
        db, row, data, filename=row.filename, source="palace-edit", mime_type=row.mime_type,
    )
    return _file_dict(row)


def _read_capped(stream, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(422, f"文件超过大小限制 {settings.max_upload_mb}MB")
        chunks.append(chunk)
    return b"".join(chunks)


def replace_file(db: Session, owner_id: str, file_id: str, upload: UploadFile) -> dict:
    """替换上传（任意白名单类型）：保留文件行身份与目录，重置抽取状态。"""
    row = _owned_file(db, owner_id, file_id)
    _validate_upload_extension(upload.filename)
    data = _read_capped(upload.file, max_bytes=int(settings.max_upload_mb) * 1024 * 1024)
    _check_quotas(
        db, owner_id, extra_bytes=len(data) - row.size, reserves_build=not _is_image(upload.filename),
    )
    _replace_artifact_and_rebuild(
        db, row, data,
        filename=upload.filename or row.filename,
        source="palace-replace",
        mime_type=upload.content_type,
    )
    return _file_dict(row)


def raw_file(db: Session, owner_id: str, file_id: str) -> tuple[Path, str, str]:
    """原始字节读取（中栏图片预览）：返回（工作区路径, mime, 文件名）。"""
    row = _owned_file(db, owner_id, file_id)
    try:
        _, path = palace_workspace.user_workspace(owner_id).require_file(
            palace_workspace.user_dir_id(owner_id), row.artifact_id,
        )
    except WorkspaceError as exc:
        raise HTTPException(404, str(exc)) from exc
    return path, row.mime_type or "application/octet-stream", row.filename


def preview_file(db: Session, owner_id: str, file_id: str, max_chars: int) -> dict:
    """内容预览：截断标记 + previewable。previewable 表示「该文件可作为文本
    预览/编辑」：抽取文本非空即可预览；md/txt 即使内容为空（新建草稿笔记）
    也视为可预览——空内容可在线编辑，否则新建笔记无法写入第一行内容。"""
    row = _owned_file(db, owner_id, file_id)
    try:
        content = palace_workspace.user_workspace(owner_id).extracted_text(
            palace_workspace.user_dir_id(owner_id), row.artifact_id, max_chars,
        )
    except WorkspaceError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "file": _file_dict(row),
        "content": content,
        "truncated": len(content) >= max_chars,
        "previewable": bool(content) or _is_editable(row.filename),
    }


def _batch_candidates(names: list[str]) -> list[str]:
    """过滤批量导入条目：跳过目录、__MACOSX/、隐藏文件与重名条目。"""
    candidates: list[str] = []
    seen: set[str] = set()
    for name in names:
        cleaned = str(name or "").replace("\\", "/").strip()
        if not cleaned or cleaned.endswith("/"):
            continue
        parts = [part for part in cleaned.split("/") if part]
        if not parts or parts[0].upper() == "__MACOSX":
            continue
        if any(part.startswith(".") for part in parts):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            candidates.append(cleaned)
    return candidates


def _decode_zip_names(infos: list[zipfile.ZipInfo]) -> list[str]:
    """修正 zip 条目名编码：未打 UTF-8 标志位的条目 zipfile 按 CP437 解码，
    而实际字节可能是 GBK（中文 Windows）也可能是 UTF-8（macOS Info-ZIP 不打
    标志位）。按 CP437 还原字节后先试 UTF-8 再试 GBK——两类字节串对另一种
    编码严格解码均失败，顺序无歧义（生产实测两种平台的中文名都会乱码）。"""
    names: list[str] = []
    for info in infos:
        name = info.filename
        if not info.flag_bits & 0x800:
            try:
                raw = info.filename.encode("cp437")
            except UnicodeEncodeError:
                raw = None
            if raw is not None:
                for encoding in ("utf-8", "gbk"):
                    try:
                        name = raw.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
        names.append(name)
    return names


def batch_import_files(db: Session, current_user: User, archive: UploadFile) -> dict:
    """ZIP 批量导入：以压缩包名（去扩展名）为顶层目录，保留包内相对层级；
    逐条走与 upload 相同的落盘+DB+投递流程，受配额约束。"""
    if _extension_of(archive.filename) != "zip":
        raise HTTPException(400, "仅支持 .zip 压缩包")
    owner_id = current_user.id
    dir_id = palace_workspace.user_dir_id(owner_id)
    workspace = palace_workspace.user_workspace(owner_id)
    root_folder = (Path(archive.filename or "").stem or "").strip()[:200] or "导入文件"
    try:
        zf = zipfile.ZipFile(archive.file)
    except (zipfile.BadZipFile, OSError) as exc:
        raise HTTPException(400, "压缩包解析失败，请确认上传的是有效的 .zip 文件") from exc
    max_entry_bytes = int(settings.max_upload_mb) * 1024 * 1024
    max_batch_files = int(settings.super_assistant_palace_batch_max_files)
    allowed = _palace_allowed_extensions()
    created: list[dict] = []
    skipped: list[dict] = []
    accepted = 0
    quota_reason: str | None = None
    with zf:
        infos = zf.infolist()
        decoded = _decode_zip_names(infos)
        # 打开条目必须用原始 ZipInfo：解码恢复的名字与 zip 内实际存储键
        # （乱码形态）不同，按名字 getinfo 会 KeyError（生产实测 500）。
        info_by_name: dict[str, zipfile.ZipInfo] = {}
        for name, info in zip(decoded, infos):
            info_by_name.setdefault(name, info)
        for name in _batch_candidates(decoded):
            parts = name.split("/")
            display = parts[-1]
            # 展示用相对路径（不含压缩包根），跳过原因能定位到具体条目
            entry_display = "/".join(parts)
            folder_path = "/".join([root_folder, *parts[:-1]])
            if quota_reason is not None:  # 配额超限后剩余条目全部跳过
                skipped.append({"filename": entry_display, "reason": quota_reason})
                continue
            extension = _extension_of(display)
            if extension not in allowed:
                skipped.append({
                    "filename": entry_display,
                    "reason": f"不支持的类型 .{extension}（允许: {settings.allowed_upload_extensions}"
                              f",{','.join(_IMAGE_EXTENSIONS)}）",
                })
                continue
            if accepted >= max_batch_files:
                skipped.append({"filename": entry_display, "reason": f"超出单次导入数量上限（{max_batch_files}）"})
                continue
            if len(folder_path) > 500:
                skipped.append({"filename": entry_display, "reason": "条目路径过长"})
                continue
            info = info_by_name.get(name)
            if info is None:  # 理论不可达：candidates 全部来自 decoded
                continue
            image = extension in _IMAGE_EXTENSIONS
            try:
                _check_quotas(
                    db, owner_id, extra_bytes=int(info.file_size), extra_files=1,
                    reserves_build=not image,
                )
            except HTTPException as exc:
                quota_reason = str(exc.detail)
                skipped.append({"filename": entry_display, "reason": quota_reason})
                continue
            try:
                # 按流读取并设上限，阻断声明尺寸造假的 zip 炸弹
                data = _read_capped(zf.open(info), max_bytes=max_entry_bytes)
            except HTTPException:
                skipped.append({"filename": entry_display, "reason": f"超过大小限制（{settings.max_upload_mb}MB）"})
                continue
            except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
                skipped.append({"filename": entry_display, "reason": f"条目读取失败：{exc}"})
                continue
            try:
                artifact = workspace.save_bytes(
                    dir_id, display, data, source="palace-batch", mime_type=None,
                    extract=not image,
                )
            except WorkspaceError as exc:
                skipped.append({"filename": entry_display, "reason": str(exc)})
                continue
            created.append(_file_dict(_create_file_row(
                db, owner_id, artifact, fallback_name=display, folder_path=folder_path,
            )))
            # 目录一等公民：包内层级同步落目录行（mkdir -p），树中目录可直接拖拽/重命名
            if folder_path:
                _ensure_folder_rows(db, owner_id, folder_path, include_self=True)
            accepted += 1
    return {"created": created, "skipped": skipped}


def delete_palace_file(db: Session, owner_id: str, file_id: str) -> None:
    row = _owned_file(db, owner_id, file_id)
    try:
        palace_workspace.user_workspace(owner_id).delete_file(
            palace_workspace.user_dir_id(owner_id), row.artifact_id,
        )
    except WorkspaceError as exc:
        raise HTTPException(404, str(exc)) from exc
    # 图谱清理尽力而为：Neo4j 短暂不可用时文件仍可删除，残留溯源由重建兜底
    try:
        palace_graph.remove_file_graph(owner_id, file_id, row.filename)
    except Exception:
        logger.warning("记忆宫殿图谱清理失败（file=%s）", file_id, exc_info=True)
    db.delete(row)
    db.commit()


def rebuild_file(db: Session, owner_id: str, file_id: str) -> dict:
    row = _owned_file(db, owner_id, file_id)
    if _is_image(row.filename):
        raise HTTPException(400, "图片文件不参与图谱抽取，无需重建")
    if row.status == "draft":
        raise HTTPException(400, "笔记还没有内容，请先编辑保存后再重建")
    if row.status in {"pending", "building"}:
        raise HTTPException(409, "该文件正在抽取队列中，请稍候")
    _check_quotas(db, owner_id)
    result = request_build(row)
    return result


# ---------------------------------------------------------------------------
# 目录管理（一等公民：空目录常驻、整目录移动/重命名）
# ---------------------------------------------------------------------------


def _normalize_folder_path(value: str | None) -> str:
    """与前端 normalizePalacePath 同口径：斜杠统一、去空段与首尾空白。"""
    segments = [segment.strip() for segment in str(value or "").replace("\\", "/").split("/")]
    return "/".join(segment for segment in segments if segment)


def _validate_folder_path(path: str, *, allow_root: bool = False) -> str:
    if not path:
        if allow_root:
            return ""
        raise HTTPException(400, "目录路径不能为空")
    if len(path) > 500:
        raise HTTPException(400, "目录路径过长（上限 500 字符）")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise HTTPException(400, "目录名不能是 . 或 ..")
    return path


def _folder_dict(row: SuperAssistantPalaceFolder) -> dict:
    return {
        "id": row.id,
        "path": row.path,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }


def _owned_folder(db: Session, owner_id: str, folder_id: str) -> SuperAssistantPalaceFolder:
    row = db.get(SuperAssistantPalaceFolder, folder_id)
    if row is None or row.owner_id != owner_id:
        raise HTTPException(404, "目录不存在")
    return row


def _folder_ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:depth]) for depth in range(1, len(parts))]


def _folder_exists(db: Session, owner_id: str, path: str) -> bool:
    return (
        db.query(SuperAssistantPalaceFolder.id)
        .filter(
            SuperAssistantPalaceFolder.owner_id == owner_id,
            SuperAssistantPalaceFolder.path == path,
        )
        .first()
        is not None
    )


def _ensure_folder_rows(
    db: Session, owner_id: str, path: str, *, include_self: bool = False,
) -> None:
    """mkdir -p：按需补齐目录行（默认仅祖先，叶子行由调用方显式建/改名）。

    include_self 供文件/笔记落入选定目录使用：目标目录在树中可见即可落
    （ZIP 导入等来源的目录只体现在文件 folder_path 上，没有目录行）。
    """
    candidates = _folder_ancestors(path) + ([path] if include_self else [])
    for candidate in candidates:
        if not _folder_exists(db, owner_id, candidate):
            db.add(SuperAssistantPalaceFolder(owner_id=owner_id, path=candidate))
    db.commit()


def list_folders(db: Session, owner_id: str) -> list[dict]:
    rows = (
        db.query(SuperAssistantPalaceFolder)
        .filter(SuperAssistantPalaceFolder.owner_id == owner_id)
        .order_by(SuperAssistantPalaceFolder.path)
        .all()
    )
    return [_folder_dict(row) for row in rows]


def create_folder(db: Session, owner_id: str, raw_path: str) -> dict:
    path = _validate_folder_path(_normalize_folder_path(raw_path))
    if _folder_exists(db, owner_id, path):
        raise HTTPException(409, "同名目录已存在")
    _ensure_folder_rows(db, owner_id, path)
    row = SuperAssistantPalaceFolder(owner_id=owner_id, path=path)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # 并发窗口：检查与提交之间另一请求已建同名目录
        db.rollback()
        raise HTTPException(409, "同名目录已存在") from None
    db.refresh(row)
    return _folder_dict(row)


def rename_folder(db: Session, owner_id: str, folder_id: str, raw_path: str) -> dict:
    """重命名/移动目录：本目录、子孙目录行与文件 folder_path 前缀批量重写。

    文件量受每用户文件数配额约束，Python 侧前缀匹配（而非 LIKE）以规避
    路径中 %/_ 通配符歧义。
    """
    row = _owned_folder(db, owner_id, folder_id)
    old = row.path
    new = _validate_folder_path(_normalize_folder_path(raw_path))
    if new == old:
        return _folder_dict(row)
    if new.startswith(f"{old}/"):
        raise HTTPException(400, "不能把目录移动到自身或其子目录下")
    if _folder_exists(db, owner_id, new):
        raise HTTPException(409, "同名目录已存在")
    _ensure_folder_rows(db, owner_id, new)

    prefix = f"{old}/"
    for other in (
        db.query(SuperAssistantPalaceFolder)
        .filter(SuperAssistantPalaceFolder.owner_id == owner_id)
        .all()
    ):
        if other.id != row.id and other.path.startswith(prefix):
            other.path = new + other.path[len(old):]
    row.path = new
    for file_row in (
        db.query(SuperAssistantPalaceFile)
        .filter(SuperAssistantPalaceFile.owner_id == owner_id)
        .all()
    ):
        if file_row.folder_path == old or file_row.folder_path.startswith(prefix):
            file_row.folder_path = new + file_row.folder_path[len(old):]
    db.commit()
    db.refresh(row)
    return _folder_dict(row)


def delete_folder(db: Session, owner_id: str, folder_id: str) -> None:
    """删除空目录。文件删除各自触发图谱剥离，静默级联删除会放大误操作
    影响面，非空目录一律拒绝（前端引导先清空）。"""
    row = _owned_folder(db, owner_id, folder_id)
    prefix = f"{row.path}/"
    has_child = any(
        other.path.startswith(prefix)
        for other in (
            db.query(SuperAssistantPalaceFolder.path)
            .filter(SuperAssistantPalaceFolder.owner_id == owner_id)
            .all()
        )
    )
    has_file = any(
        file_row.folder_path == row.path or file_row.folder_path.startswith(prefix)
        for file_row in (
            db.query(SuperAssistantPalaceFile.folder_path)
            .filter(SuperAssistantPalaceFile.owner_id == owner_id)
            .all()
        )
    )
    if has_child or has_file:
        raise HTTPException(409, "目录非空：请先删除其中的文件与子目录")
    db.delete(row)
    db.commit()


def move_file(db: Session, owner_id: str, file_id: str, raw_folder_path: str) -> dict:
    """移动文件到目标目录（不改内容、不触发抽取）；空串表示根目录。"""
    row = _owned_file(db, owner_id, file_id)
    target = _validate_folder_path(_normalize_folder_path(raw_folder_path), allow_root=True)
    if target:
        _ensure_folder_rows(db, owner_id, target, include_self=True)
    row.folder_path = target
    db.commit()
    db.refresh(row)
    return _file_dict(row)


def create_note(db: Session, current_user: User, body: PalaceNoteCreate) -> dict:
    """新建 .md 空笔记并落入选中目录（笔记创建仅 md；txt 仍可上传与编辑）。

    空文本建图必然失败（run_build 对空内容置 failed），因此 status=draft
    不派发抽取；首次保存内容走 update_file_content 的既有重建链路。
    """
    owner_id = current_user.id
    filename = str(body.filename or "").strip()
    if not filename:
        raise HTTPException(400, "文件名不能为空")
    if "/" in filename or "\\" in filename:
        raise HTTPException(400, "文件名不能包含路径分隔符")
    if _extension_of(filename) != "md":
        raise HTTPException(400, "仅支持新建 .md 笔记（文件名无需带后缀时由前端自动补 .md）")
    folder_path = _validate_folder_path(_normalize_folder_path(body.folder_path), allow_root=True)
    if folder_path:
        _ensure_folder_rows(db, owner_id, folder_path, include_self=True)
    _check_quotas(db, owner_id, extra_files=1, reserves_build=False)
    try:
        artifact = palace_workspace.user_workspace(owner_id).save_stream(
            palace_workspace.user_dir_id(owner_id),
            filename,
            io.BytesIO(b""),
            source="palace-note",
            mime_type="text/markdown",
            extract=True,
        )
    except WorkspaceError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = SuperAssistantPalaceFile(
        owner_id=owner_id,
        filename=filename,
        folder_path=folder_path,
        artifact_id=str(artifact.get("id")),
        mime_type=str(artifact.get("mimeType") or "text/plain"),
        size=int(artifact.get("size") or 0),
        sha256=str(artifact.get("sha256") or ""),
        extracted_chars=0,
        # draft=新建未写入内容：不参与抽取队列与图谱统计
        status="draft",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _file_dict(row)


def search_graph(db: Session, owner_id: str, query: str) -> dict:
    """图谱检索：无已建图文件或无关键词时返回空集；Neo4j 不可用降级
    available=False（同 graph_overview 语义，不 5xx）。"""
    built = (
        db.query(SuperAssistantPalaceFile)
        .filter(
            SuperAssistantPalaceFile.owner_id == owner_id,
            SuperAssistantPalaceFile.status == "built",
        )
        .count()
    )
    empty = {"entities": [], "relations": []}
    if not built:
        return {"available": True, **empty}
    from app.data_channel.steward.workspace import _context_terms

    terms = _context_terms(query)
    if not terms:
        return {"available": True, **empty}
    try:
        payload = palace_graph.search(owner_id, terms)
    except palace_graph.PalaceGraphUnavailable:
        logger.warning("记忆宫殿图谱检索失败：Neo4j 不可用（owner=%s）", owner_id)
        return {"available": False, **empty}
    return {
        "available": True,
        "entities": payload.get("entities") or [],
        "relations": payload.get("relations") or [],
    }


def graph_overview(db: Session, owner_id: str) -> dict:
    """图谱可视化数据；Neo4j 不可用时返回 available=False 而不是 5xx。

    附带画布下统计条的消费字段：builtFiles/totalFiles（文档口径）与
    updatedAt（最近一次成功建图完成时间，无则 None）。
    """
    built = (
        db.query(SuperAssistantPalaceFile)
        .filter(
            SuperAssistantPalaceFile.owner_id == owner_id,
            SuperAssistantPalaceFile.status == "built",
        )
        .count()
    )
    total_files = (
        db.query(SuperAssistantPalaceFile.id)
        .filter(SuperAssistantPalaceFile.owner_id == owner_id)
        .count()
    )
    last_built_at = (
        db.query(func.max(SuperAssistantPalaceBuild.finished_at))
        .filter(
            SuperAssistantPalaceBuild.owner_id == owner_id,
            SuperAssistantPalaceBuild.status == "success",
        )
        .scalar()
    )
    stats = {
        "builtFiles": built,
        "totalFiles": total_files,
        "updatedAt": last_built_at.isoformat() if last_built_at else None,
    }
    if not built:
        return {"available": True, "nodes": [], "edges": [], "totals": {"entities": 0, "relations": 0}, "truncated": False, **stats}
    try:
        payload = palace_graph.owner_graph(owner_id)
    except palace_graph.PalaceGraphUnavailable:
        logger.warning("记忆宫殿图谱读取失败：Neo4j 不可用（owner=%s）", owner_id)
        return {"available": False, "nodes": [], "edges": [], "totals": {"entities": 0, "relations": 0}, "truncated": False, **stats}
    return {"available": True, **payload, **stats}


# ---------------------------------------------------------------------------
# 助手消费：system prompt 注入 + 只读工具
# ---------------------------------------------------------------------------


def _format_source_files(source_files: list[str]) -> str:
    names = [str(name) for name in source_files if str(name)][:4]
    return "、".join(names)


def build_prompt_section(db: Session, owner_id: str, query: str = "") -> str:
    """组装注入 system prompt 的图谱段；无已建图文件时返回 ""。

    先做一次轻量 DB 计数探测，避免每轮对话都触碰 Neo4j。
    """
    built = (
        db.query(SuperAssistantPalaceFile)
        .filter(
            SuperAssistantPalaceFile.owner_id == owner_id,
            SuperAssistantPalaceFile.status == "built",
        )
        .count()
    )
    if not built:
        return ""
    from app.data_channel.steward.workspace import _context_terms

    terms = _context_terms(query)
    if not terms:
        return ""
    result = palace_graph.search(owner_id, terms)
    entities = result.get("entities") or []
    relations = result.get("relations") or []
    if not entities and not relations:
        return ""
    lines = ["# 记忆宫殿知识图谱（用户上传文档沉淀的长期知识，跨会话可用）"]
    if entities:
        lines.append("## 相关实体")
        for entity in entities[:20]:
            source = _format_source_files(entity.get("source_files") or [])
            suffix = f"；来源：{source}" if source else ""
            lines.append(f"- {entity.get('name')}（{entity.get('type') or '其他'}{suffix}）")
    if relations:
        lines.append("## 相关关系")
        for relation in relations[:30]:
            source = relation.get("source_name") or relation.get("source")
            target = relation.get("target_name") or relation.get("target")
            lines.append(f"- {source} —{relation.get('name')}→ {target}")
    lines.append("（图谱其余内容可用 palace_graph_search 检索，文件清单可用 palace_graph_files 查看。）")
    return "\n".join(lines)[:_SECTION_BUDGET]


def search_for_tool(owner_id: str, query: str) -> dict:
    from app.data_channel.steward.workspace import _context_terms

    return palace_graph.search(owner_id, _context_terms(query))


def list_files_for_tool(db: Session, owner_id: str) -> list[dict]:
    return [
        {
            "id": row.id,
            "filename": row.filename,
            "status": row.status,
            "entityCount": row.entity_count,
            "relationCount": row.relation_count,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        }
        for row in (
            db.query(SuperAssistantPalaceFile)
            .filter(SuperAssistantPalaceFile.owner_id == owner_id)
            .order_by(SuperAssistantPalaceFile.created_at.desc())
            .all()
        )
    ]
