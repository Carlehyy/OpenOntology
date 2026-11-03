"""投产与回退 — 飞轮的执行器半环。

投产（apply_proposal）：门禁通过的提案写入生产 AgentProfile，写入前
①全量快照当前 profile（版本链回退锚点）②对近期生产会话抽样评分作
看守基线（无样本时回退用实验 baseline 臂统计）。经 ontologies 域的
update_profile sanctioned 写入路径落库。

回退（rollback_version）：把快照写回 profile，前一版本恢复 active，
提案标记 rolled_back，inbox 告警管理员。看守（watch_latest）由值守
循环每轮调用：投产后的新生产会话与基线对比，劣化超噪声地板即回退。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.assistant_evaluation import service as task_service
from app.assistant_evaluation.adapters import get_adapters
from app.assistant_evaluation.dimensions import DIMENSIONS
from app.assistant_evaluation.engine import build_engine
from app.assistant_evaluation.models import (
    AssistantEvalExperiment,
    AssistantEvalProfileVersion,
    AssistantEvalProposal,
)
from app.assistant_evaluation.notify import notify_admins
from app.assistant_evaluation.service import ServiceError
from app.assistant_evaluation.timeline import (
    EVENT_PROPOSAL_APPLIED,
    EVENT_VERSION_ROLLED_BACK,
    ACTOR_ADMIN,
    ACTOR_AUTOPILOT,
    ACTOR_SYSTEM,
    record_event,
)

logger = logging.getLogger(__name__)

# 看守参数：投产前抽样上限 / 投产后判定所需最小新样本 / 劣化阈值
PRE_APPLY_SAMPLE_LIMIT = 8
MIN_WATCH_SAMPLES = 3
WATCH_OVERALL_DROP = 5.0
WATCH_DIM_DROP = 10.0
_VERSION_LEVERS = ("system_prompt_extra", "default_model_id")


def _profile_snapshot(profile) -> dict:
    from app.ontologies.agent_runtime.profile_service import PROFILE_FIELDS

    return {field: getattr(profile, field, None) for field in PROFILE_FIELDS}


def list_versions(db: Session, ontology_id: str) -> list[AssistantEvalProfileVersion]:
    return (
        db.query(AssistantEvalProfileVersion)
        .filter(AssistantEvalProfileVersion.ontology_id == ontology_id)
        .order_by(AssistantEvalProfileVersion.version.desc())
        .all()
    )


def active_version(db: Session, ontology_id: str) -> AssistantEvalProfileVersion | None:
    return (
        db.query(AssistantEvalProfileVersion)
        .filter(AssistantEvalProfileVersion.ontology_id == ontology_id,
                AssistantEvalProfileVersion.status == "active")
        .order_by(AssistantEvalProfileVersion.version.desc())
        .first()
    )


def applies_last_week(db: Session, ontology_id: str, trigger: str = "autopilot") -> int:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    rows = (
        db.query(AssistantEvalProfileVersion)
        .filter(AssistantEvalProfileVersion.ontology_id == ontology_id,
                AssistantEvalProfileVersion.created_at >= since)
        .all()
    )
    return sum(1 for r in rows if (r.source or {}).get("trigger") == trigger)


def score_recent_production(db: Session, ontology_id: str, *, since=None,
                            limit: int = PRE_APPLY_SAMPLE_LIMIT,
                            dimension_keys: list[str] | None = None,
                            engine=None) -> dict:
    """对近期真实生产会话（非沙箱）抽样评分：看守与投产基线的共用量尺。

    返回 {overall, per_dim, conversations}；无可评分会话时 conversations=0。
    """
    return asyncio.run(_score_recent_production_async(
        db, ontology_id, since=since, limit=limit,
        dimension_keys=dimension_keys, engine=engine))


async def _score_recent_production_async(db, ontology_id, *, since, limit,
                                         dimension_keys, engine) -> dict:
    from app.ontologies.agent_runtime.models import AgentConversation

    from app.assistant_evaluation.dimensions import BASE_DIMENSION_KEYS

    query = db.query(AgentConversation).filter(
        AgentConversation.ontology_id == ontology_id,
        AgentConversation.is_sandbox.is_(False),
    )
    if since is not None:
        query = query.filter(AgentConversation.created_at >= since)
    conversations = query.order_by(
        AgentConversation.created_at.desc(), AgentConversation.id.desc()
    ).limit(max(1, limit)).all()

    adapter = get_adapters()["ontology_agent"]
    base_dims = list(dimension_keys or BASE_DIMENSION_KEYS)
    dims_map = dict(DIMENSIONS)
    overalls: list[float] = []
    per_dim_values: dict[str, list[float]] = {}
    for conv in conversations:
        trace = adapter.load_trace(db, conv.id)
        if trace is None:
            continue
        dim_keys = task_service._effective_dims(base_dims, trace)
        if not dim_keys:
            continue
        try:
            raw = await task_service._evaluate_async(engine, dim_keys, trace, None)
            scores, _reasons = task_service._assemble_scores(dim_keys, dims_map, raw)
        except Exception:  # noqa: BLE001 — 单条失败跳过，不摧毁样本
            logger.exception("生产会话评分失败：%s", conv.id)
            continue
        overall = task_service._weighted_overall(dim_keys, scores, dims_map)
        if overall is not None:
            overalls.append(float(overall))
        for key, value in scores.items():
            per_dim_values.setdefault(key, []).append(float(value))

    return {
        "overall": round(sum(overalls) / len(overalls), 1) if overalls else None,
        "per_dim": {key: round(sum(v) / len(v), 1)
                    for key, v in per_dim_values.items()},
        "conversations": len(overalls),
    }


def apply_proposal(db: Session, *, proposal_id: str, trigger: str,
                   actor_user_id: str | None) -> AssistantEvalProfileVersion:
    """门禁通过的提案投产：快照 → 基线抽样 → 写入生产 → 版本链登记。"""
    from app.ontologies.agent_runtime import schemas as agent_schemas
    from app.ontologies.agent_runtime.boundary import get_or_create_profile
    from app.ontologies.agent_runtime.profile_service import update_profile

    proposal = (
        db.query(AssistantEvalProposal)
        .filter(AssistantEvalProposal.id == proposal_id)
        .first()
    )
    if proposal is None:
        raise ServiceError("提案不存在。")
    if proposal.status != "validated":
        raise ServiceError("只有门禁通过（validated）的提案才能投产。")

    profile = get_or_create_profile(db, proposal.ontology_id)
    snapshot = _profile_snapshot(profile)

    # 看守基线：优先生产会话抽样；无样本回退到实验 baseline 臂统计
    experiment = (
        db.query(AssistantEvalExperiment)
        .filter(AssistantEvalExperiment.proposal_id == proposal.id)
        .order_by(AssistantEvalExperiment.created_at.desc())
        .first()
    )
    pre_stats: dict = {}
    try:
        engine = None
        if experiment is not None and experiment.judge_model_config_id:
            from app.model_configs.models import ModelConfig

            config = db.query(ModelConfig).filter(
                ModelConfig.id == experiment.judge_model_config_id).first()
            if config is not None:
                engine = build_engine(config)
        pre_stats = score_recent_production(
            db, proposal.ontology_id,
            dimension_keys=(experiment.params or {}).get("dimension_keys")
            if experiment is not None else None,
            engine=engine)
    except Exception:  # noqa: BLE001 — 基线抽样失败不阻断投产（回退用实验基线）
        logger.exception("投产前基线抽样失败，回退使用实验 baseline 统计")
        pre_stats = {}
    if not pre_stats.get("conversations"):
        baseline = (experiment.result or {}).get("baseline") if experiment else None
        pre_stats = dict(baseline or {})
        pre_stats.setdefault("conversations", 0)
        pre_stats["baseline_source"] = "experiment" if baseline else "none"

    # 经 ontologies 域 sanctioned 路径写入（只动提案声明的杠杆字段）
    payload = proposal.payload or {}
    patch = {lever: payload[lever] for lever in _VERSION_LEVERS if lever in payload}
    if not patch:
        raise ServiceError("提案 payload 缺少可投产的杠杆字段。")
    update_profile(
        db, proposal.ontology_id,
        agent_schemas.AgentProfileUpdate(**patch),
        get_profile_fn=get_or_create_profile, source="assistant_evaluation_apply")

    # 版本链：前一 active 让位，新版本登记
    previous = active_version(db, proposal.ontology_id)
    if previous is not None:
        previous.status = "superseded"
    next_version = 1 + max(
        [v.version for v in list_versions(db, proposal.ontology_id)] or [0])
    actor = (ACTOR_AUTOPILOT if trigger == "autopilot"
             else ACTOR_SYSTEM if trigger == "system" else ACTOR_ADMIN)
    row = AssistantEvalProfileVersion(
        ontology_id=proposal.ontology_id, version=next_version,
        snapshot=snapshot,
        source={"proposal_id": proposal.id,
                "experiment_id": (experiment.id if experiment is not None else None),
                "trigger": trigger},
        pre_apply_stats=pre_stats,
        created_by=actor_user_id,
    )
    db.add(row)
    proposal.status = "applied"
    db.flush()
    record_event(db, event_type=EVENT_PROPOSAL_APPLIED,
                 assistant_key="ontology_agent", actor=actor,
                 actor_user_id=actor_user_id if actor == ACTOR_ADMIN else None,
                 ref_type="profile_version", ref_id=row.id,
                 detail={"ontology_id": proposal.ontology_id,
                         "proposal_id": proposal.id,
                         "version": next_version,
                         "trigger": trigger,
                         "levers": sorted(patch.keys()),
                         "pre_apply_stats": pre_stats})
    db.commit()
    db.refresh(row)
    return row


def rollback_version(db: Session, *, version_id: str, reason: str,
                     trigger: str, actor_user_id: str | None) -> AssistantEvalProfileVersion:
    """回退：快照写回生产 profile，前一版本恢复 active，管理员告警。"""
    from app.ontologies.agent_runtime.boundary import get_or_create_profile
    from app.ontologies.agent_runtime.profile_service import PROFILE_FIELDS, update_profile

    version = (
        db.query(AssistantEvalProfileVersion)
        .filter(AssistantEvalProfileVersion.id == version_id)
        .first()
    )
    if version is None:
        raise ServiceError("版本不存在。")
    if version.status != "active":
        raise ServiceError("只有当前生效版本才能回退。")

    snapshot = version.snapshot or {}
    profile = get_or_create_profile(db, version.ontology_id)
    restore_fields = {field: snapshot.get(field) for field in PROFILE_FIELDS
                      if field in snapshot}
    if not restore_fields:
        raise ServiceError("版本快照为空，无法回退。")
    update_profile(
        db, version.ontology_id,
        _restore_body(restore_fields),
        get_profile_fn=get_or_create_profile, source="assistant_evaluation_rollback")

    version.status = "rolled_back"
    previous = (
        db.query(AssistantEvalProfileVersion)
        .filter(AssistantEvalProfileVersion.ontology_id == version.ontology_id,
                AssistantEvalProfileVersion.version < version.version)
        .order_by(AssistantEvalProfileVersion.version.desc())
        .first()
    )
    if previous is not None:
        previous.status = "active"

    proposal_id = (version.source or {}).get("proposal_id")
    if proposal_id:
        proposal = (
            db.query(AssistantEvalProposal)
            .filter(AssistantEvalProposal.id == proposal_id)
            .first()
        )
        if proposal is not None:
            proposal.status = "rolled_back"

    actor = (ACTOR_AUTOPILOT if trigger == "autopilot"
             else ACTOR_SYSTEM if trigger == "system" else ACTOR_ADMIN)
    record_event(db, event_type=EVENT_VERSION_ROLLED_BACK,
                 assistant_key="ontology_agent", actor=actor,
                 actor_user_id=actor_user_id if actor == ACTOR_ADMIN else None,
                 ref_type="profile_version", ref_id=version.id,
                 detail={"ontology_id": version.ontology_id,
                         "version": version.version,
                         "trigger": trigger, "reason": reason[:500]})
    notify_admins(db, kind="alert",
                  title=f"助手配置已自动回退至 v{max(version.version - 1, 0)}",
                  summary=f"本体 {version.ontology_id[:8]} 的 v{version.version} 投产后"
                          f"检测到劣化，已回退。原因：{reason[:300]}",
                  correlation_key=f"rollback:{version.id}")
    db.commit()
    return version


def _restore_body(restore_fields: dict):
    """构造 ProfileUpdate body（restore 的白名单字段全量带出）。"""
    from app.ontologies.agent_runtime import schemas as agent_schemas

    return agent_schemas.AgentProfileUpdate(**restore_fields)


def latest_noise_floor(db: Session) -> float:
    from app.assistant_evaluation.models import AssistantEvalCalibration

    row = (
        db.query(AssistantEvalCalibration)
        .filter(AssistantEvalCalibration.assistant_key == "ontology_agent",
                AssistantEvalCalibration.status == "success")
        .order_by(AssistantEvalCalibration.created_at.desc())
        .first()
    )
    if row is None:
        return 0.0
    return float((row.result or {}).get("overall_noise") or 0.0)


def watch_latest(db: Session, ontology_id: str, *, dimension_keys: list[str] | None,
                 engine=None) -> str:
    """投产後看守：返回 verified | rolled_back | pending。

    新生产会话不足 MIN_WATCH_SAMPLES 条时 pending（下轮再看）；样本足够
    且未劣化 → verified；劣化超阈值 → 自动回退当前 active 版本。
    """
    version = active_version(db, ontology_id)
    if version is None or version.verified:
        return "verified" if version is not None else "none"

    post = score_recent_production(
        db, ontology_id, since=version.created_at, limit=PRE_APPLY_SAMPLE_LIMIT,
        dimension_keys=dimension_keys, engine=engine)
    if post.get("conversations", 0) < MIN_WATCH_SAMPLES:
        return "pending"

    baseline = version.pre_apply_stats or {}
    noise = latest_noise_floor(db)
    overall_drop = None
    if baseline.get("overall") is not None and post.get("overall") is not None:
        overall_drop = round(float(baseline["overall"]) - float(post["overall"]), 1)
    dim_drops = {}
    for key, base_avg in (baseline.get("per_dim") or {}).items():
        post_avg = (post.get("per_dim") or {}).get(key)
        if post_avg is not None:
            dim_drops[key] = round(float(base_avg) - float(post_avg), 1)

    degraded_keys = [k for k, drop in dim_drops.items()
                     if drop > max(WATCH_DIM_DROP, 3 * noise)]
    overall_degraded = (overall_drop is not None
                        and overall_drop > max(WATCH_OVERALL_DROP, 2 * noise))
    if not degraded_keys and not overall_degraded:
        version.verified = True
        db.commit()
        return "verified"

    reason = (f"投产后劣化：overall {overall_drop}，维度下降 "
              f"{ {k: dim_drops[k] for k in degraded_keys} }"
              if not overall_degraded
              else f"投产后综合分下降 {overall_drop}（噪声地板 {noise}）")
    rollback_version(db, version_id=version.id, reason=reason,
                     trigger="autopilot", actor_user_id=None)
    return "rolled_back"
