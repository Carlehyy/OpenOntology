"""记忆宫殿图谱的定期聚类合并（重复实体收敛，离线批处理）。

semantica DuplicateDetector/EntityMerger 的平台化版本，三级流水线：

1. 确定性预筛（find_consolidation_candidates，纯函数）：按规范名分组——
   去常见组织后缀后相同、或某实体 alias 与另一实体 name 规范名相同；
2. LLM 确认（_confirm_groups）：候选簇拼一个 prompt 让模型剔除不该合并
   的簇；LLM 失败/解析失败则本轮放弃合并（model_used=False，宁可不动图）；
3. 执行合并：每确认簇选 canonical（模型保留名，mention_count 最高兜底），
   调 palace_graph.merge_entities。

调度遵循平台任务模式（AGENTS.md：禁止新增 Celery 任务）：APScheduler 进程
内定时（每天 03:00）扫描「有 built 状态文件的全部 owner」逐个经 NATS
JetStream 派发（super_assistant.palace.consolidate），由 nats_executor
消费执行——LLM 重活不占 Web 线程。定时器启动接线在 bootstrap/lifecycle
（与 assistant_evaluation.autopilot_scheduler 同位置）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.shared.config import settings
from app.super_assistant import palace_graph, provider, reflection_service

logger = logging.getLogger(__name__)

# 单轮 LLM 确认的簇数上限：控制 prompt 长度与确认成本
_MAX_CONFIRM_GROUPS = 50

# 常见组织后缀：去掉后再比较（中文公司形态词 + 英文常见缩写）
_ORG_SUFFIX_RE = re.compile(r"(公司|科技|集团|有限|责任|inc|corp|ltd|co)\s*$")

_CONFIRM_PROMPT = """你是知识图谱实体消歧助手。下面每组实体来自同一个用户的知识图谱，组内名字可能是同一真实对象的不同写法（抽取自不同文档）。

候选组：
{groups}

请逐组判断：
1. 只保留确实指同一对象的组；组内名字明显是不同对象（如不同人物、不同地点、不同产品）时，不要输出该组。
2. 对保留的组，canonical 选择组内最正式、最完整的名称（必须来自该组）。

输出严格 JSON（不要任何解释、不要 markdown 围栏）：
{{"groups":[{{"id":组编号,"canonical":"保留名","members":["并出名"]}}]}}
没有任何组应该合并时输出 {{"groups":[]}}。"""


# ---------------------------------------------------------------------------
# 确定性预筛（纯函数）
# ---------------------------------------------------------------------------


def _core_name(name: str) -> str:
    """规范名基础上去掉（可重复出现的）组织后缀：智谱AI有限责任公司/科技
    → 智谱ai；仅剩后缀本身时不 stripping，保持原规范名。"""
    core = palace_graph.normalize_name(name)
    while True:
        stripped = _ORG_SUFFIX_RE.sub("", core).strip()
        if not stripped or stripped == core:
            return core
        core = stripped


def find_consolidation_candidates(entities: list[dict]) -> list[list[dict]]:
    """确定性候选检测：组织后缀归一同组 + alias/name 规范名交叉同组。

    返回簇列表（并查集保证簇间不相交）；每簇内部按 mention_count 降序、
    name 升序，簇间按总 mention_count 降序、首名升序——全序确定，截断
    _MAX_CONFIRM_GROUPS 时结果可复现。
    """
    count = len(entities)
    parent = list(range(count))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: int, right: int) -> None:
        root_l, root_r = find(left), find(right)
        if root_l != root_r:
            parent[max(root_l, root_r)] = min(root_l, root_r)

    # 规则 a：去组织后缀后的规范名相同
    core_owner: dict[str, int] = {}
    for index, entity in enumerate(entities):
        core = _core_name(str(entity.get("name") or ""))
        if not core:
            continue
        if core in core_owner:
            union(index, core_owner[core])
        else:
            core_owner[core] = index

    # 规则 b：某实体的 alias 与另一实体的 name 规范名相同
    name_owner: dict[str, int] = {}
    for index, entity in enumerate(entities):
        norm = palace_graph.normalize_name(str(entity.get("name") or ""))
        if norm and norm not in name_owner:
            name_owner[norm] = index
    for index, entity in enumerate(entities):
        self_norm = palace_graph.normalize_name(str(entity.get("name") or ""))
        for alias in entity.get("aliases") or []:
            norm = palace_graph.normalize_name(str(alias))
            if not norm or norm == self_norm:
                continue
            other = name_owner.get(norm)
            if other is not None and other != index:
                union(index, other)

    clusters: dict[int, list[dict]] = {}
    for index in range(count):
        clusters.setdefault(find(index), []).append(entities[index])

    def _sort_key(entity: dict) -> tuple[int, str]:
        return (-int(entity.get("mention_count") or 0), str(entity.get("name") or ""))

    groups = [sorted(members, key=_sort_key) for members in clusters.values()]
    groups = [group for group in groups if len(group) >= 2]
    groups.sort(key=lambda group: (
        -sum(int(entity.get("mention_count") or 0) for entity in group),
        str(group[0].get("name") or ""),
    ))
    return groups


# ---------------------------------------------------------------------------
# LLM 确认与合并执行
# ---------------------------------------------------------------------------


def _pick_canonical(group: list[dict], canonical_name: str) -> dict:
    """canonical 实体：模型保留名（规范名口径匹配）优先，mention_count
    最高者兜底（候选簇已按 mention_count 降序，group[0] 即兜底）。"""
    target = palace_graph.normalize_name(canonical_name)
    for entity in group:
        if palace_graph.normalize_name(str(entity.get("name") or "")) == target:
            return entity
    return group[0]


def _confirm_groups(
    call_kwargs: dict, groups: list[list[dict]],
) -> list[tuple[list[dict], str]] | None:
    """LLM 确认候选簇，返回 [(簇, 模型保留名)]；调用或解析失败返回 None
    （调用方本轮放弃合并，宁可不动图）。"""
    capped = groups[:_MAX_CONFIRM_GROUPS]
    listing = "\n".join(
        f"{index}. " + " / ".join(
            str(entity.get("name") or "") for entity in group
        )
        for index, group in enumerate(capped, start=1)
    )
    try:
        result = provider.chat(
            call_kwargs,
            [{"role": "user", "content": _CONFIRM_PROMPT.format(groups=listing)}],
            [],
        )
        parsed = reflection_service._parse_json_loose(str(result.get("content") or ""))
    except Exception:
        logger.warning("记忆宫殿聚类合并的 LLM 确认失败，本轮放弃合并", exc_info=True)
        return None

    confirmed: list[tuple[list[dict], str]] = []
    seen_ids: set[int] = set()
    raw_groups = parsed.get("groups")
    for item in raw_groups if isinstance(raw_groups, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            group_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if not 1 <= group_id <= len(capped) or group_id in seen_ids:
            continue
        canonical_name = str(item.get("canonical") or "").strip()
        if not canonical_name:
            continue
        seen_ids.add(group_id)
        confirmed.append((capped[group_id - 1], canonical_name))
    return confirmed


def run_consolidation(db: Session, owner_id: str) -> dict[str, Any]:
    """对单个用户执行一轮聚类合并（service 入口，NATS handler / 手动触发共用）。

    返回 {"candidates", "merged_groups", "merged_entities", "model_used"}；
    保守原则：模型不可用或 LLM 确认失败时不动图（model_used=False）。
    """
    entities = palace_graph.owner_entities(owner_id)
    candidates = find_consolidation_candidates(entities)
    if not candidates:
        return {"candidates": 0, "merged_groups": [], "merged_entities": 0, "model_used": False}

    # lazy import：palace_service._palace_call_kwargs 由并行批次提供同名契约；
    # 函数缺失（ImportError）与无可用模型（ProviderError）都按本轮放弃处理
    try:
        from app.super_assistant.palace_service import _palace_call_kwargs

        call_kwargs = _palace_call_kwargs(db)
    except Exception:
        logger.warning(
            "记忆宫殿聚类合并无可用模型调用参数，本轮放弃（owner=%s）", owner_id,
            exc_info=True,
        )
        return {
            "candidates": len(candidates), "merged_groups": [],
            "merged_entities": 0, "model_used": False,
        }

    confirmed = _confirm_groups(call_kwargs, candidates)
    if confirmed is None:
        return {
            "candidates": len(candidates), "merged_groups": [],
            "merged_entities": 0, "model_used": False,
        }

    merged_groups: list[list[str]] = []
    merged_entities = 0
    consumed: set[str] = set()
    for group, canonical_name in confirmed:
        # 并查集保证簇间不相交，consumed 只是对模型输出的防御性兜底
        available = [
            entity for entity in group
            if entity.get("merge_key") and entity["merge_key"] not in consumed
        ]
        if len(available) < 2:
            continue
        canonical = _pick_canonical(available, canonical_name)
        absorbed = [
            entity for entity in available
            if entity["merge_key"] != canonical["merge_key"]
        ]
        if not absorbed:
            continue
        merged_entities += palace_graph.merge_entities(
            owner_id,
            canonical["merge_key"],
            [entity["merge_key"] for entity in absorbed],
        )
        consumed.update(entity["merge_key"] for entity in absorbed)
        merged_groups.append([str(entity.get("name") or "") for entity in available])
    if merged_entities:
        # 图谱内容已变化：bump 图谱视图缓存版本（executor 与 Web 进程共享）
        from app.super_assistant import palace_cache

        palace_cache.invalidate_graph()
    return {
        "candidates": len(candidates),
        "merged_groups": merged_groups,
        "merged_entities": merged_entities,
        "model_used": True,
    }


# ---------------------------------------------------------------------------
# 调度（APScheduler 进程内定时 + NATS JetStream 派发，autopilot_scheduler 同模式）
# ---------------------------------------------------------------------------


_scheduler = None
_JOB_ID = "super-assistant-palace-consolidate-dispatch"


def _dispatch_daily_consolidation() -> None:
    """为「有 built 状态文件的全部 owner」逐个派发聚类合并消息。"""
    from app.super_assistant.models import SuperAssistantPalaceFile
    from app.shared.database import SessionLocal

    from app.data_channel.pipeline_tasks.dispatch import (
        dispatch_super_assistant_palace_consolidate,
    )

    db = SessionLocal()
    try:
        owner_ids = [
            str(row[0])
            for row in (
                db.query(SuperAssistantPalaceFile.owner_id)
                .filter(SuperAssistantPalaceFile.status == "built")
                .distinct()
                .all()
            )
        ]
    finally:
        db.close()
    if not owner_ids:
        return
    for owner_id in owner_ids:
        try:
            dispatch_super_assistant_palace_consolidate(owner_id)
            logger.info("记忆宫殿聚类合并已派发（owner=%s）", owner_id)
        except Exception:  # noqa: BLE001 — 单个 owner 派发失败不影响其余
            logger.exception("记忆宫殿聚类合并派发失败（owner=%s）", owner_id)


def start() -> None:
    """启动每日 03:00 的聚类合并定时器；settings 开关关闭时为 no-op。"""
    global _scheduler
    if not getattr(settings, "super_assistant_palace_consolidate_enabled", True):
        return
    if _scheduler is not None and _scheduler.running:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        _dispatch_daily_consolidation, "cron", hour=3, minute=0,
        id=_JOB_ID, max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("记忆宫殿聚类合并定时器已启动（每天 03:00，本地时区）")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
