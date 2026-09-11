"""Read models for formal ontology dashboards, facts, autonomy, and logs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ontology_formal import (
    ActionExecutionLog,
    ActionType,
    LinkInstance,
    ObjectInstance,
    ObjectType,
    PropertyFact,
)
from app.ontologies.formal_modeling.facts import fact_order_clause
from app.ontologies.formal_modeling.runtime_support import (
    _current_release_view,
    _fact_to_dict,
    _naive_utc,
    _ok,
    _release_fact_query,
    _require_ontology,
)
from app.ontologies.release_context import current_release_context
from app.schemas import ontology_formal as S


logger = logging.getLogger("app.ontologies.formal_modeling.router")


def ontology_overview(ontology_id: str, db: Session):
    """Latest-release dashboard backed by the immutable release snapshot.

    Schema/configuration counts come from ``current_release_id`` rather than
    mutable runtime tables.  Runtime projections and telemetry are then
    constrained to identifiers/version lineage owned by that release.
    """
    # 详情页默认落点：重统计轮询热点，多客户端共享一次计算（fail-open）。
    # 写路径 bump 版本立即失效，进程外数据推进由短 TTL 兜底。
    from app.config import settings
    from app.ontologies import cache as ontology_cache
    return ontology_cache.cached_call(
        ontology_cache.overview_cache_key(ontology_id),
        settings.ontology_overview_cache_ttl_seconds,
        lambda: _ontology_overview_uncached(ontology_id, db),
    )


def runtime_daily_summary(ontology_id: str, start_iso: str, end_iso: str, db: Session):
    """运行汇总按日聚合（显式时间窗），供总览「运行汇总」时间维度下拉使用。

    口径与 ``ontology_overview`` 完全一致：只统计当前发布血缘
    （``ontology_release_id``）且不早于发布时间的哨兵评估与动作执行，
    请求窗口中早于发布时间的桶如实记 0。窗口跨度与参数合法性由路由层校验。
    """
    from app.config import settings
    from app.ontologies import cache as ontology_cache
    return ontology_cache.cached_call(
        ontology_cache.runtime_summary_cache_key(ontology_id, start_iso, end_iso),
        settings.ontology_overview_cache_ttl_seconds,
        lambda: _runtime_daily_summary_uncached(ontology_id, start_iso, end_iso, db),
    )


def _runtime_daily_summary_uncached(ontology_id: str, start_iso: str, end_iso: str, db: Session):
    from datetime import timedelta
    project = _require_ontology(db, ontology_id)
    release, snapshot = _current_release_view(db, project)
    start_day = datetime.fromisoformat(start_iso)
    end_day = datetime.fromisoformat(end_iso)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window_start = start_day
    window_end = end_day + timedelta(days=1)  # 右开区间：末日的全天都计入
    release_started_at = _naive_utc(release.published_at or release.created_at)
    effective_start = max(
        [value for value in (window_start, release_started_at) if value is not None]
    )
    effective_end = min(window_end, now)
    span_days = (end_day - start_day).days + 1
    days = [
        {
            "date": (start_day + timedelta(days=offset)).date().isoformat(),
            "firings": {"fired": 0, "error": 0},
            "actionRuns": {"success": 0, "failed": 0},
        }
        for offset in range(span_days)
    ]
    runtime_by_date = {item["date"]: item for item in days}

    # 哨兵评估：与 overview 相同——只认当前发布快照内仍存在的哨兵 + 发布血缘
    sentinel_ids = {
        str(item.get("id")) for item in snapshot["sentinels"] if item.get("id")
    }
    if sentinel_ids and effective_start < effective_end:
        try:
            from app.models.sentinel import SentinelFiring
            # 只取统计需要的两个标量列：整行 hydration 会连带解压
            # matches/entered/left/action_results 四个 JSON 大列。
            firings = db.query(
                SentinelFiring.status,
                SentinelFiring.created_at,
            ).filter(
                SentinelFiring.ontology_id == ontology_id,
                SentinelFiring.ontology_release_id == release.id,
                SentinelFiring.sentinel_id.in_(sentinel_ids),
                SentinelFiring.created_at >= effective_start,
                SentinelFiring.created_at < effective_end,
            ).all()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.warning("运行汇总哨兵统计失败,已降级为空统计(ontology=%s)", ontology_id, exc_info=True)
            firings = []
        for firing_status, firing_created_at in firings:
            day = runtime_by_date.get(firing_created_at.date().isoformat())
            if day and firing_status in ("fired", "error"):
                day["firings"][firing_status] += 1

    # 动作执行：显式发布版本血缘 + 非 dry-run + 已出结果
    action_ids = {
        str(item.get("id")) for item in snapshot["actions"] if item.get("id")
    }
    if action_ids and effective_start < effective_end:
        runs = db.query(
            ActionExecutionLog.status,
            ActionExecutionLog.executed_at,
        ).filter(
            ActionExecutionLog.ontology_id == ontology_id,
            ActionExecutionLog.ontology_release_id == release.id,
            ActionExecutionLog.action_id.in_(action_ids),
            ActionExecutionLog.dry_run == False,  # noqa: E712
            ActionExecutionLog.executed_at >= effective_start,
            ActionExecutionLog.executed_at < effective_end,
            ActionExecutionLog.status.in_(("success", "failed")),
        ).all()
        for run_status, run_executed_at in runs:
            day = runtime_by_date.get(run_executed_at.date().isoformat())
            if day:
                day["actionRuns"][run_status] += 1

    return _ok({
        "start": start_iso,
        "end": end_iso,
        "release": {
            "id": release.id,
            "version": release.version_number,
            "publishedAt": release.published_at.isoformat() if release.published_at else None,
        },
        "days": days,
    })


def _ontology_overview_uncached(ontology_id: str, db: Session):
    from datetime import timedelta
    project = _require_ontology(db, ontology_id)
    release, snapshot = _current_release_view(db, project)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window_start = (now - timedelta(days=6)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    release_started_at = _naive_utc(release.published_at or release.created_at)
    release_window_start = max(
        [value for value in (window_start, release_started_at) if value is not None]
    )
    runtime_days = [
        {
            "date": (window_start + timedelta(days=offset)).date().isoformat(),
            "firings": {"fired": 0, "error": 0},
            "actionRuns": {"success": 0, "failed": 0},
        }
        for offset in range(7)
    ]
    runtime_by_date = {item["date"]: item for item in runtime_days}

    # —— 发布模型：不可变 current release 快照是唯一口径 ——
    object_types = snapshot["objectTypes"]
    link_types = snapshot["linkTypes"]
    actions = snapshot["actions"]
    functions = snapshot["functions"]
    snapshot_sentinels = snapshot["sentinels"]
    object_type_ids = {
        str(item.get("id")) for item in object_types if item.get("id")
    }
    link_type_ids = {
        str(item.get("id")) for item in link_types if item.get("id")
    }
    sentinel_ids = {
        str(item.get("id")) for item in snapshot_sentinels if item.get("id")
    }
    action_ids = {
        str(item.get("id")) for item in actions if item.get("id")
    }
    try:
        from app.models.sentinel import Sentinel, SentinelFiring
        live_sentinels = db.query(Sentinel).filter(
            Sentinel.ontology_id == ontology_id,
            Sentinel.id.in_(sentinel_ids),
        ).all() if sentinel_ids else []
        live_sentinel_by_id = {item.id: item for item in live_sentinels}
        # 只取统计需要的两个标量列：整行 hydration 会连带解压
        # matches/entered/left/action_results 四个 JSON 大列。
        firings_7d = (db.query(
            SentinelFiring.status,
            SentinelFiring.created_at,
        ).filter(
            SentinelFiring.ontology_id == ontology_id,
            SentinelFiring.ontology_release_id == release.id,
            SentinelFiring.sentinel_id.in_(sentinel_ids),
            SentinelFiring.created_at >= release_window_start,
            SentinelFiring.created_at <= now,
        ).all()) if sentinel_ids else []
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("Overview 哨兵统计失败,已降级为空统计(ontology=%s)", ontology_id, exc_info=True)
        live_sentinel_by_id, firings_7d = {}, []

    def sentinel_flag(item: dict, field: str, default: bool) -> bool:
        live = live_sentinel_by_id.get(str(item.get("id")))
        return bool(getattr(live, field)) if live is not None else bool(item.get(field, default))

    # —— 当前运行投影：只接收发布结构中仍然存在的类型 ——
    # 计数在 SQL 端 GROUP BY 聚合：整行 hydration 会连带解压
    # properties/computed 两个 JSON 大列。
    instance_rows = (db.query(
        ObjectInstance.object_type_id,
        ObjectInstance.source,
        func.count(),
    ).filter(
        ObjectInstance.ontology_id == ontology_id,
        ObjectInstance.object_type_id.in_(object_type_ids),
    ).group_by(
        ObjectInstance.object_type_id,
        ObjectInstance.source,
    ).all()) if object_type_ids else []
    instances_n = 0
    by_source: dict[str, int] = {}
    inst_by_type: dict[str, int] = {}
    for object_type_id, source, count in instance_rows:
        instances_n += count
        by_source[source or "manual"] = by_source.get(source or "manual", 0) + count
        inst_by_type[object_type_id] = inst_by_type.get(object_type_id, 0) + count
    link_instances_n = (db.query(LinkInstance).filter(
        LinkInstance.ontology_id == ontology_id,
        LinkInstance.link_type_id.in_(link_type_ids),
    ).count()) if link_type_ids else 0

    # Mapping 定义属于发布结构；运行表中的 applied 版本戳不改变定义口径。
    mappings_stat = {"total": 0, "bound": 0, "nameMatch": 0, "autoCreate": 0, "autoApply": 0}
    ot_names = {
        str(value) for item in object_types
        for value in (item.get("name"), item.get("displayName")) if value
    }
    for mapping in snapshot["mappings"]:
        mappings_stat["total"] += 1
        if mapping.get("targetObjectTypeId"):
            mappings_stat["bound"] += 1
        elif mapping.get("entityClass") in ot_names:
            mappings_stat["nameMatch"] += 1
        else:
            mappings_stat["autoCreate"] += 1
        if (mapping.get("fieldMapping") or {}).get("__auto_apply_on_review__"):
            mappings_stat["autoApply"] += 1

    # —— 运行：动作日志显式带发布版本血缘 ——
    # 只取审批统计需要的三个标量列：整行 hydration 会连带解压
    # parameters/target_snapshot/effects 等 JSON 大列。
    log_rows = (db.query(
        ActionExecutionLog.status,
        ActionExecutionLog.decided_at,
        ActionExecutionLog.executed_at,
    ).filter(
        ActionExecutionLog.ontology_id == ontology_id,
        ActionExecutionLog.ontology_release_id == release.id,
        ActionExecutionLog.action_id.in_(action_ids),
        ActionExecutionLog.dry_run == False).all()) if action_ids else []  # noqa: E712
    pending_n = sum(1 for status, _, _ in log_rows
                    if status in ("pending", "executing"))
    decided = sorted(
        [(status, decided_at, executed_at)
         for status, decided_at, executed_at in log_rows
         if status in ("approved", "rejected")],
        key=lambda row: (row[1] or row[2]), reverse=True)
    approved_n = sum(1 for status, _, _ in decided if status == "approved")
    recent = decided[:20]
    recent_rate = (sum(1 for status, _, _ in recent if status == "approved")
                   / len(recent)) if recent else None
    runs_7d = [row for row in log_rows if row[2]
               and release_window_start <= row[2] <= now
               and row[0] in ("success", "failed")]
    for firing_status, firing_created_at in firings_7d:
        day = runtime_by_date.get(firing_created_at.date().isoformat())
        if day and firing_status in ("fired", "error"):
            day["firings"][firing_status] += 1
    for run_status, _, run_executed_at in runs_7d:
        day = runtime_by_date.get(run_executed_at.date().isoformat())
        if day:
            day["actionRuns"][run_status] += 1

    # —— 事实流：仅统计当前发布产生/发布后追加且仍属于该结构的事实 ——
    facts_query = _release_fact_query(db, ontology_id, release, snapshot)
    facts_total = 0
    by_kind: dict[str, int] = {}
    for kind, count in facts_query.with_entities(
            PropertyFact.kind, func.count()).group_by(PropertyFact.kind).all():
        facts_total += count
        by_kind[kind or "property"] = by_kind.get(kind or "property", 0) + count

    # —— 健康检查（可操作的下一步建议）——
    # target 指向本体详情页 Tab key（GROUPS），前端据此一键跳转处理。
    health: list[dict] = []
    if not object_types:
        health.append({"level": "info", "message": "还没有对象实体", "target": "design",
                       "hint": "打开图谱编辑器开始建模，或在「数据映射」由数据生成类型"})
    no_pk = [
        item.get("displayName") or item.get("name") or str(item.get("id") or "")
        for item in object_types if not item.get("primaryKey")
    ]
    if no_pk:
        health.append({"level": "warn", "message": f"{len(no_pk)} 个对象实体未设主键：{', '.join(no_pk[:3])}{'…' if len(no_pk) > 3 else ''}", "target": "design",
                       "hint": "无主键会影响数据灌入去重与动作的实例定位"})
    if object_types and instances_n == 0:
        health.append({"level": "info", "message": "模型已就绪但还没有实例数据", "target": "data-mapping",
                       "hint": "到「数据映射」把 curated 数据灌进来"})
    if mappings_stat["autoCreate"] > 0:
        health.append({"level": "warn", "message": f"{mappings_stat['autoCreate']} 条映射未绑定对象实体（将由数据自建类型）", "target": "data-mapping",
                       "hint": "建议在映射维护里显式绑定，防止产生平行类型"})
    if snapshot_sentinels and all(
            sentinel_flag(item, "muted", False) for item in snapshot_sentinels):
        health.append({"level": "warn", "message": "所有哨兵都处于影子（静默）状态", "target": "governance",
                       "hint": "确认规律无误后，在哨兵面板解除静默让治理真正生效"})
    if not snapshot_sentinels and instances_n > 0:
        health.append({"level": "info", "message": "已有数据但还没有哨兵", "target": "design",
                       "hint": "在图谱编辑器建哨兵，让平台替你盯住状态变化"})
    err_firings = sum(1 for status, _ in firings_7d if status == "error")
    if err_firings:
        health.append({"level": "warn", "message": f"近 7 天有 {err_firings} 次哨兵评估出错", "target": "governance",
                       "hint": "查看运行历史的哨兵触发记录，多为条件表达式写错"})
    # 待审批动作刻意不进 health：总览不展示审批待办，审批统一在「治理推演」处理。

    return _ok({
        "release": {
            "id": release.id,
            "version": release.version_number,
            "publishedAt": release.published_at.isoformat() if release.published_at else None,
        },
        "model": {
            "objectTypes": len(object_types), "linkTypes": len(link_types),
            "actions": len(actions),
            "actionsRequiringApproval": sum(
                1 for item in actions if item.get("requiresApproval")),
            "functions": len(functions),
            "sentinels": {"total": len(snapshot_sentinels),
                          "enabled": sum(
                              1 for item in snapshot_sentinels
                              if sentinel_flag(item, "enabled", True)),
                          "muted": sum(
                              1 for item in snapshot_sentinels
                              if sentinel_flag(item, "muted", False))},
        },
        "data": {
            "instances": instances_n, "instancesBySource": by_source,
            "linkInstances": link_instances_n, "mappings": mappings_stat,
            "topTypes": sorted(
                [{"id": str(item.get("id") or ""),
                  "name": item.get("displayName") or item.get("name") or str(item.get("id") or ""),
                  "count": inst_by_type.get(str(item.get("id") or ""), 0)}
                 for item in object_types],
                key=lambda x: -x["count"])[:6],
        },
        "runtime": {
            "pendingApprovals": pending_n,
            "decisions": {"total": len(decided), "approved": approved_n,
                          "rejected": len(decided) - approved_n,
                          "recentApprovalRate": recent_rate},
            "firings7d": {"total": len(firings_7d),
                          "fired": sum(1 for status, _ in firings_7d if status == "fired"),
                          "error": err_firings},
            "actionRuns7d": {"total": len(runs_7d),
                             "success": sum(1 for row in runs_7d if row[0] == "success"),
                             "failed": sum(1 for row in runs_7d if row[0] == "failed")},
            "daily7d": runtime_days,
        },
        "facts": {"total": facts_total, "byKind": by_kind},
        "health": health,
    })


def _instance_fact_label(
        inst: ObjectInstance,
        type_info: Optional[SimpleNamespace],
        instance_id: str,
) -> str:
    """事实流实例标签：候选顺序与审批待办标签（_approval_instance_label）对齐，
    避免同一实例在「待审批」与「事实流」两处标识不一致。"""
    props = inst.properties if isinstance(inst.properties, dict) else {}
    candidates: list[str] = []
    if type_info is not None:
        primary_key = getattr(type_info, "primary_key", None)
        if primary_key:
            candidates.append(primary_key)
            for prop in getattr(type_info, "properties", None) or []:
                if not isinstance(prop, dict):
                    continue
                prop_id, prop_name = prop.get("id"), prop.get("name")
                if prop_id == primary_key and prop_name:
                    candidates.append(prop_name)
                elif prop_name == primary_key and prop_id:
                    candidates.append(prop_id)
    candidates.extend(("name", "flight_no", "id"))
    candidates.extend(("title", "label", "code", "month", "report_month"))

    value = None
    for key in dict.fromkeys(candidates):
        candidate = props.get(key)
        if (
            candidate is not None
            and not isinstance(candidate, (dict, list))
            and str(candidate).strip()
        ):
            value = str(candidate).strip()
            break
    if value is None and inst.external_id:
        value = str(inst.external_id)

    type_name = getattr(type_info, "name", "") if type_info is not None else ""
    if value:
        return f"{type_name}·{value}" if type_name else value
    return type_name or instance_id[:8]


def recent_facts(
        ontology_id: str,
        limit: int,
        kind: Optional[str],
        release_id: Optional[str],
        current_release_only: bool,
        db: Session,
):
    """本体级事实流（跨实例，时间倒序），带可读主体标签——治理页的时间线。"""
    project = _require_ontology(db, ontology_id)
    release_snapshot = None
    q = db.query(PropertyFact).filter(PropertyFact.ontology_id == ontology_id)
    if release_id:
        release = current_release_context(
            db, ontology_id, expected_release_id=release_id)
        q = q.filter(PropertyFact.ontology_release_id == release.id)
        release_snapshot = release.snapshot
    elif current_release_only:
        release_row, release_snapshot = _current_release_view(db, project)
        q = _release_fact_query(db, ontology_id, release_row, release_snapshot)
    if kind:
        q = q.filter(PropertyFact.kind == kind)
    items = q.order_by(*fact_order_clause()).limit(min(limit, 200)).all()

    # 主体标签解析：实例 → name/主键值；哨兵(absence) → 哨兵名；决策 → 动作名
    inst_ids = {f.instance_id for f in items}
    inst_map = {i.id: i for i in db.query(ObjectInstance).filter(
        ObjectInstance.id.in_(inst_ids)).all()} if inst_ids else {}
    if release_snapshot is not None:
        sent_map = {
            str(item["id"]): SimpleNamespace(
                display_name=item.get("displayName"), name=item.get("name"))
            for item in release_snapshot["sentinels"] if item.get("id")
        }
    else:
        try:
            from app.models.sentinel import Sentinel
            sent_map = {s.id: s for s in db.query(Sentinel).filter(
                Sentinel.id.in_(inst_ids)).all()} if inst_ids else {}
        except Exception:  # noqa: BLE001
            sent_map = {}
    log_map = {l.id: l for l in db.query(ActionExecutionLog).filter(
        ActionExecutionLog.id.in_(inst_ids)).all()} if inst_ids else {}
    if release_snapshot is not None:
        type_info = {
            str(item["id"]): SimpleNamespace(
                name=item.get("displayName") or item.get("name") or "",
                primary_key=item.get("primaryKey"),
                properties=item.get("properties") or [],
            )
            for item in release_snapshot["objectTypes"] if item.get("id")
        }
        action_names = {
            str(item["id"]): item.get("displayName") or item.get("name") or ""
            for item in release_snapshot["actions"] if item.get("id")
        }
    else:
        type_info = {
            o.id: SimpleNamespace(
                name=o.display_name or o.name,
                primary_key=getattr(o, "primary_key", None),
                properties=getattr(o, "properties", None) or [],
            )
            for o in db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        }
        action_names = {}

    def _label(f: PropertyFact) -> str:
        inst = inst_map.get(f.instance_id)
        if inst is not None:
            return _instance_fact_label(inst, type_info.get(inst.object_type_id), f.instance_id)
        s = sent_map.get(f.instance_id)
        if s is not None:
            return f"哨兵·{s.display_name or s.name}"
        l = log_map.get(f.instance_id)
        if l is not None:
            return f"动作·{action_names.get(l.action_id) or l.action_name or l.action_id[:8]}"
        return f.instance_id[:8]

    out = []
    for f in items:
        d = _fact_to_dict(f)
        d["subjectLabel"] = _label(f)
        out.append(d)
    return _ok(out)


# ============================================================
#  自治等级（Autonomy）— 哨兵×动作的人工批准率统计与晋升建议
# ============================================================


AUTONOMY_PROMOTE_MIN = 10      # 晋升所需最少近期决策数
AUTONOMY_PROMOTE_RATE = 0.95   # 晋升所需近期批准率
AUTONOMY_DEMOTE_FAILRATE = 0.2  # 自动执行失败率超过则建议降级


def autonomy_stats(
        ontology_id: str,
        release_id: Optional[str],
        db: Session,
):
    """按动作统计 HITL 决策历史，给出自治等级与晋升/降级建议。

    等级模型（自治是挣来的，不是配出来的）：
      L0 影子   绑定的哨兵全部静默——只观察记录，不产生副作用
      L1 人审   requires_approval=True，每次真实执行等人拍板
      L2 自动   requires_approval=False，命中即执行
    晋升依据 = 近期人工批准率（人几乎总是点"批准"，说明规则已可信）。
    """
    _require_ontology(db, ontology_id)
    release = (
        current_release_context(
            db, ontology_id, expected_release_id=release_id)
        if release_id else None
    )
    logs_query = db.query(ActionExecutionLog).filter(
        ActionExecutionLog.ontology_id == ontology_id,
        ActionExecutionLog.dry_run == False,  # noqa: E712
    )
    if release is not None:
        actions = [SimpleNamespace(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            display_name=item.get("displayName") or item.get("display_name"),
            requires_approval=bool(
                item.get("requiresApproval", item.get("requires_approval", False))),
        ) for item in release.snapshot["actions"] if item.get("id")]
        logs_query = logs_query.filter(
            ActionExecutionLog.ontology_release_id == release.id)
        try:
            sentinels = []
            for item in release.snapshot["sentinels"]:
                sentinel_id = str(item.get("id") or "")
                if not sentinel_id:
                    continue
                sentinels.append(SimpleNamespace(
                    id=sentinel_id,
                    name=str(item.get("name") or ""),
                    display_name=item.get("displayName") or item.get("name") or "",
                    action_ids=item.get("actionIds") or item.get("action_ids") or [],
                    muted=bool(item.get("muted", False)),
                    enabled=bool(item.get("enabled", True)),
                ))
        except Exception:  # noqa: BLE001
            sentinels = []
    else:
        actions = db.query(ActionType).filter(
            ActionType.ontology_id == ontology_id).all()
        try:
            from app.models.sentinel import Sentinel
            sentinels = db.query(Sentinel).filter(
                Sentinel.ontology_id == ontology_id).all()
        except Exception:  # noqa: BLE001
            sentinels = []
    logs = logs_query.all()

    by_action: dict[str, list] = {}
    for l in logs:
        by_action.setdefault(l.action_id, []).append(l)

    out = []
    for a in actions:
        ls = by_action.get(a.id, [])
        decided = [l for l in ls if l.status in ("approved", "rejected")]
        decided.sort(key=lambda l: (l.decided_at or l.executed_at), reverse=True)
        approved_n = sum(1 for l in decided if l.status == "approved")
        rejected_n = len(decided) - approved_n
        recent = decided[:20]
        recent_approved = sum(1 for l in recent if l.status == "approved")
        recent_rate = (recent_approved / len(recent)) if recent else None

        auto = [l for l in ls if l.decided_by is None and l.status in ("success", "failed")]
        auto_failed = sum(1 for l in auto if l.status == "failed")
        pending_n = sum(1 for l in ls if l.status in ("pending", "executing"))

        bound = [s for s in sentinels if a.id in (s.action_ids or [])]
        shadow = bool(bound) and all(bool(s.muted) for s in bound)

        if shadow:
            level = "L0"
        elif a.requires_approval:
            level = "L1"
        else:
            level = "L2"

        recommendation = None
        reason = None
        if level == "L1" and recent_rate is not None and len(recent) >= AUTONOMY_PROMOTE_MIN \
                and recent_rate >= AUTONOMY_PROMOTE_RATE:
            recommendation = "promote"
            reason = (f"近 {len(recent)} 次决策批准率 {recent_rate:.0%} ≥ "
                      f"{AUTONOMY_PROMOTE_RATE:.0%}，人几乎总在点批准——可晋升为自动执行")
        elif level == "L1" and recent_rate is not None and len(recent) >= 5 and recent_rate < 0.5:
            recommendation = "observe"
            reason = f"近 {len(recent)} 次决策批准率仅 {recent_rate:.0%}，建议回哨兵面板静默观察、修条件后再放行"
        elif level == "L2" and len(auto) >= AUTONOMY_PROMOTE_MIN \
                and (auto_failed / len(auto)) > AUTONOMY_DEMOTE_FAILRATE:
            recommendation = "demote"
            reason = (f"自动执行 {len(auto)} 次中失败 {auto_failed} 次"
                      f"（{auto_failed / len(auto):.0%}），建议加回人工审批闸门")

        out.append({
            "actionId": a.id,
            "actionName": a.display_name or a.name,
            "requiresApproval": bool(a.requires_approval),
            "level": level,
            "shadow": shadow,
            "sentinels": [{"id": s.id, "name": s.display_name or s.name,
                           "muted": bool(s.muted), "enabled": bool(s.enabled)} for s in bound],
            "decisions": {
                "approved": approved_n, "rejected": rejected_n, "total": len(decided),
                "approvalRate": (approved_n / len(decided)) if decided else None,
                "recentCount": len(recent), "recentApprovalRate": recent_rate,
            },
            "autoRuns": {"total": len(auto), "failed": auto_failed},
            "pending": pending_n,
            "recommendation": recommendation,
            "recommendationReason": reason,
            "thresholds": {"promoteMinDecisions": AUTONOMY_PROMOTE_MIN,
                           "promoteRate": AUTONOMY_PROMOTE_RATE},
        })
    return _ok(out)


# ============================================================
#  Execution Logs
# ============================================================


def list_logs(ontology_id: str, db: Session):
    _require_ontology(db, ontology_id)
    items = (db.query(ActionExecutionLog)
             .filter(ActionExecutionLog.ontology_id == ontology_id)
             .order_by(ActionExecutionLog.executed_at.desc()).limit(200).all())
    return _ok([S.ActionLogOut.model_validate(x).model_dump(by_alias=True) for x in items])
