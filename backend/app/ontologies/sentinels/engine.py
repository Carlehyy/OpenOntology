"""
哨兵引擎 (Sentinel Engine) — 三种执行入口，统一汇入评估器

  1. run_manual    手动触发：评估本体下全部启用的哨兵
  2. run_for_change 变化驱动：某对象类型属性变化 → 挑出引用该类型的哨兵 → 逐个评估
  3. run_scheduled  定期扫描：到达各哨兵的扫描间隔 → 逐个评估，更新 last_scanned_at
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import logging
from types import SimpleNamespace
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker

from app.models.sentinel import (
    Sentinel,
    SentinelCdcOutbox,
    SentinelFiring,
    SentinelMatchState,
)
from app.models.ontology import OntologyProject
from app.models.ontology_version import OntologyVersion
from app.ontologies.sentinels.evaluator import evaluate_sentinel
from app.ontologies.agent_runtime.boundary import build_scope
from app.ontologies.release_context import current_release_context
from app.ontologies.sentinels.dynamic_service import reconcile_release

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


def _summary(firings, runtime_errors: Optional[list[dict]] = None) -> dict:
    runtime_errors = runtime_errors or []
    return {
        "evaluated": len(firings),
        "fired": sum(1 for f in firings if f.status == "fired"),
        "errors": (
            sum(1 for f in firings if f.status == "error")
            + len(runtime_errors)
        ),
        "no_change": sum(1 for f in firings if f.status == "no_change"),
        "no_match": sum(1 for f in firings if f.status == "no_match"),
        "pending": sum(1 for f in firings if f.status == "pending"),
        "muted": sum(1 for f in firings if f.status == "muted"),
        "runtimeErrors": runtime_errors,
        "firings": [{"id": getattr(f, "id", None),
                     "sentinelId": f.sentinel_id, "sentinelName": f.sentinel_name,
                     "status": f.status, "matchCount": f.match_count,
                     "entered": f.entered or [], "left": f.left or [],
                     "actionResults": f.action_results,
                     "error": f.error} for f in firings],
    }


def _superseded_summary(
        *, ontology_id: str, sentinel_id: str,
        release_id: str, reason: str) -> dict:
    return {
        **_summary([]),
        "status": "stale",
        "outcome": "superseded",
        "superseded": True,
        "skipped": reason,
        "ontologyId": ontology_id,
        "ontologyReleaseId": release_id,
        "sentinelId": sentinel_id,
    }


def _prepare_runtime(
        db: Session, ontology_id: str, *,
        prepare_dynamic: bool = True):
    """Resolve one exact immutable release and prepare its dynamic overlay."""
    try:
        context = current_release_context(db, ontology_id)
    except Exception:  # noqa: BLE001
        return None, [{
            "ontologyId": ontology_id,
            "stage": "resolve_current_release",
            "code": "current_release_unavailable",
            "error": "当前发布版本不可用，已阻断全部哨兵执行",
        }]

    has_dynamic = db.query(Sentinel.id).filter(
        Sentinel.ontology_id == ontology_id,
        Sentinel.origin == "assistant_dynamic",
        Sentinel.retired_at.is_(None),
    ).first()
    if prepare_dynamic and has_dynamic is not None:
        try:
            _, _, scope = build_scope(
                db, ontology_id, release_id=context.id)
            reconcile_release(db, context, scope)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "Sentinel 动态覆盖层准备失败: %s", ontology_id)
            return context, [{
                "ontologyId": ontology_id,
                "stage": "reconcile_dynamic_sentinels",
                "code": "dynamic_sentinel_reconcile_failed",
                "error": "动态哨兵发布绑定校验失败，已阻断执行",
            }]
    return context, []


def _released_builtin(
        ontology_id: str, release_id: str, snapshot: dict,
        raw: dict, live: Sentinel | None,
) -> SimpleNamespace:
    """Materialize one built-in definition from the release, never draft rows.

    The live ``sentinels`` row is only an operational state carrier.  Structural
    fields come exclusively from the immutable snapshot so editing the next
    draft cannot stop or rewrite the currently released automation.
    """
    operational = (
        live
        if live is not None
        and live.origin == "release_builtin"
        and live.status == "published"
        else None
    )
    return SimpleNamespace(
        id=str(raw.get("id") or ""),
        ontology_id=ontology_id,
        name=str(raw.get("name") or ""),
        display_name=str(
            raw.get("displayName") or raw.get("name") or ""),
        description=raw.get("description"),
        bindings=raw.get("bindings") or [],
        links=raw.get("links") or [],
        condition=raw.get("condition"),
        # CEP 模式定义（triggerMode='on_pattern' 时存在），随快照冻结。
        pattern=raw.get("pattern") if isinstance(
            raw.get("pattern"), dict) else None,
        condition_rows=raw.get("conditionRows") or [],
        condition_logic=raw.get("conditionLogic") or "and",
        primary_alias=raw.get("primaryAlias"),
        action_ids=raw.get("actionIds") or [],
        action_parameters=raw.get("actionParameters") or {},
        on_change=bool(raw.get("onChange", True)),
        on_schedule=bool(raw.get("onSchedule", False)),
        scan_interval_seconds=int(
            raw.get("scanIntervalSeconds") or 300),
        last_scanned_at=(
            live.last_scanned_at if live is not None else None),
        trigger_mode=raw.get("triggerMode") or "on_enter",
        muted=(
            bool(operational.muted)
            if operational is not None
            else bool(raw.get("muted", False))
        ),
        enabled=(
            bool(operational.enabled)
            if operational is not None
            else bool(raw.get("enabled", True))
        ),
        status="published",
        origin="release_builtin",
        bound_release_id=release_id,
        retired_at=None,
        _release_snapshot=snapshot,
        _live_row=live,
    )


def _runtime_sentinels(
        db: Session, context, *,
        on_change: bool | None = None,
        on_schedule: bool | None = None,
        include_dynamic: bool = True,
) -> list:
    """Return snapshot-built-ins plus exact-release assistant overlays."""
    ontology_id = context.project.id
    released = [
        item for item in context.snapshot.get("sentinels") or []
        if isinstance(item, dict) and item.get("id")
    ]
    builtin_ids = {str(item["id"]) for item in released}
    live_by_id = {
        item.id: item for item in db.query(Sentinel).filter(
            Sentinel.ontology_id == ontology_id,
            Sentinel.origin == "release_builtin",
            Sentinel.id.in_(builtin_ids),
        ).all()
    } if builtin_ids else {}
    sentinels = [
        _released_builtin(
            ontology_id,
            context.id,
            context.snapshot,
            raw,
            live_by_id.get(str(raw["id"])),
        )
        for raw in released
    ]

    if include_dynamic:
        dynamic_query = db.query(Sentinel).filter(
            Sentinel.ontology_id == ontology_id,
            Sentinel.enabled == True,  # noqa: E712
            Sentinel.status == "published",
            Sentinel.origin == "assistant_dynamic",
            Sentinel.bound_release_id == context.id,
            Sentinel.retired_at.is_(None),
        )
        dynamics = dynamic_query.all()
        for item in dynamics:
            item._release_snapshot = context.snapshot
            item._live_row = item
        sentinels.extend(dynamics)
    return [
        item for item in sentinels
        if item.enabled
        and (on_change is None or item.on_change == on_change)
        and (on_schedule is None or item.on_schedule == on_schedule)
    ]


def _evaluate(db: Session, context, sentinel, source: str):
    return evaluate_sentinel(
        db,
        context.project.id,
        sentinel,
        source,
        expected_release_id=context.id,
    )


def _control_source(prefix: str, event_id: str) -> str:
    return f"{prefix}:{str(event_id).replace('-', '')[:16]}"


def _restore_run_on_all_retry(
        db: Session, sentinel, checkpoint: dict | None) -> None:
    """Reuse the first attempt's action epoch after an outbox reclaim."""
    if str(sentinel.trigger_mode or "") != "run_on_all":
        return
    baseline = (
        ((checkpoint or {}).get("states") or {}).get(str(sentinel.id), {})
    )
    changed = False
    for state in db.query(SentinelMatchState).filter(
            SentinelMatchState.ontology_id == sentinel.ontology_id,
            SentinelMatchState.sentinel_id == sentinel.id,
    ).all():
        if state.runtime_status != "completed":
            continue
        before = baseline.get(str(state.id))
        touched = (
            before is None
            or int(state.execution_epoch or 0)
            > int(before.get("executionEpoch") or 0)
            or str(before.get("runtimeStatus") or "")
            != "completed"
        )
        if touched:
            # evaluate_sentinel only advances a run_on_all epoch from a
            # completed state. Marking the already-attempted edge recoverable
            # reuses its durable action idempotency keys on this retry.
            state.runtime_status = "failed_enter"
            changed = True
    if changed:
        db.commit()


def _run_control_sentinel(
        db: Session, context, sentinel, *, source: str,
        checkpoint: dict | None, retry: bool):
    existing = db.query(SentinelFiring).filter(
        SentinelFiring.ontology_id == context.project.id,
        SentinelFiring.ontology_release_id == context.id,
        SentinelFiring.sentinel_id == sentinel.id,
        SentinelFiring.trigger_source == source,
        SentinelFiring.status != "error",
    ).order_by(
        SentinelFiring.created_at.desc(),
        SentinelFiring.id.asc(),
    ).first()
    if existing is not None:
        return existing
    if retry:
        _restore_run_on_all_retry(db, sentinel, checkpoint)
    return _evaluate(db, context, sentinel, source)


def run_release_initialization(
        db: Session, ontology_id: str, *, event_id: str,
        checkpoint: dict | None = None, retry: bool = False) -> dict:
    """Evaluate every on-change built-in exactly for one activated release."""
    context, errors = _prepare_runtime(
        db, ontology_id, prepare_dynamic=False)
    if context is None or errors:
        return _summary([], errors)
    sentinels = _runtime_sentinels(
        db,
        context,
        on_change=True,
        include_dynamic=False,
    )
    source = _control_source("rel", event_id)
    firings = [
        _run_control_sentinel(
            db,
            context,
            sentinel,
            source=source,
            checkpoint=checkpoint,
            retry=retry,
        )
        for sentinel in sentinels
    ]
    return _summary(firings)


def run_dynamic_initialization(
        db: Session, ontology_id: str, sentinel_id: str, *,
        event_id: str, checkpoint: dict | None = None,
        retry: bool = False) -> dict:
    """Initialize only the exact enabled assistant overlay from its event."""
    context, errors = _prepare_runtime(db, ontology_id)
    if context is None or errors:
        return _summary([], errors)
    selected = [
        item for item in _runtime_sentinels(
            db, context, on_change=True, include_dynamic=True)
        if item.origin == "assistant_dynamic" and item.id == sentinel_id
    ]
    if not selected:
        return _superseded_summary(
            ontology_id=ontology_id,
            sentinel_id=sentinel_id,
            release_id=context.id,
            reason="dynamic_sentinel_inactive",
        )
    firing = _run_control_sentinel(
        db,
        context,
        selected[0],
        source=_control_source("dyn", event_id),
        checkpoint=checkpoint,
        retry=retry,
    )
    return _summary([firing])


def run_builtin_initialization(
        db: Session, ontology_id: str, sentinel_id: str, *,
        event_id: str, checkpoint: dict | None = None,
        retry: bool = False) -> dict:
    """Initialize only one exact published built-in from the release snapshot."""
    context, errors = _prepare_runtime(
        db, ontology_id, prepare_dynamic=False)
    if context is None or errors:
        return _summary([], errors)
    selected = [
        item for item in _runtime_sentinels(
            db,
            context,
            on_change=True,
            include_dynamic=False,
        )
        if item.origin == "release_builtin"
        and item.id == sentinel_id
        and not bool(item.muted)
    ]
    if not selected:
        return _superseded_summary(
            ontology_id=ontology_id,
            sentinel_id=sentinel_id,
            release_id=context.id,
            reason="builtin_sentinel_inactive",
        )
    firing = _run_control_sentinel(
        db,
        context,
        selected[0],
        source=_control_source("bin", event_id),
        checkpoint=checkpoint,
        retry=retry,
    )
    return _summary([firing])


def run_scheduled_event(
        db: Session, ontology_id: str, sentinel_id: str, *,
        event_id: str, checkpoint: dict | None = None,
        retry: bool = False) -> dict:
    """Execute one durable due cycle; outbox finalization owns its watermark."""
    context, errors = _prepare_runtime(db, ontology_id)
    if context is None or errors:
        return _summary([], errors)
    selected = [
        item for item in _runtime_sentinels(
            db, context, on_schedule=True)
        if item.id == sentinel_id
    ]
    if not selected:
        return _superseded_summary(
            ontology_id=ontology_id,
            sentinel_id=sentinel_id,
            release_id=context.id,
            reason="scheduled_sentinel_inactive",
        )
    firing = _run_control_sentinel(
        db,
        context,
        selected[0],
        source=_control_source("sch", event_id),
        checkpoint=checkpoint,
        retry=retry,
    )
    return _summary([firing])


def run_manual(db: Session, ontology_id: str) -> dict:
    """手动触发：全量评估本体所有启用哨兵。"""
    context, errors = _prepare_runtime(db, ontology_id)
    if context is None or errors:
        return _summary([], errors)
    sentinels = _runtime_sentinels(db, context)
    firings = [_evaluate(db, context, s, "manual") for s in sentinels]
    return _summary(firings)


def run_for_save(db: Session, ontology_id: str) -> dict:
    """整体保存后触发：评估该本体所有启用且开启 on_change 的哨兵。

    图谱编辑器的实例变更只有保存时才落库——保存即"变化到达"时刻。
    边沿触发语义(SentinelMatchState)保证重复评估幂等，不会重复放炮。
    """
    context, errors = _prepare_runtime(db, ontology_id)
    if context is None or errors:
        return _summary([], errors)
    sentinels = _runtime_sentinels(db, context, on_change=True)
    firings = [_evaluate(db, context, s, "change") for s in sentinels]
    return _summary(firings)


def run_for_change(db: Session, ontology_id: str, object_type_id: str,
                   changed_keys: Optional[list] = None) -> dict:
    """变化驱动：挑出 bindings 引用了该对象类型、且开启 on_change 的哨兵并评估。"""
    context, errors = _prepare_runtime(db, ontology_id)
    if context is None or errors:
        return _summary([], errors)
    sentinels = _runtime_sentinels(db, context, on_change=True)
    # 仅挑选监听了该对象类型的哨兵(属性级可在此进一步收窄)
    affected = [s for s in sentinels
                if any(b.get("objectTypeId") == object_type_id for b in (s.bindings or []))]
    firings = [_evaluate(db, context, s, "change") for s in affected]
    return _summary(firings)


def run_for_link_change(db: Session, ontology_id: str) -> dict:
    """链接拓扑变化驱动：链接实例增删对声明了 links 约束的跨对象哨兵是"变化"。
    只评估 on_change 且带链接约束的哨兵（无链接约束的由属性 CDC 覆盖）。"""
    context, errors = _prepare_runtime(db, ontology_id)
    if context is None or errors:
        return _summary([], errors)
    sentinels = _runtime_sentinels(db, context, on_change=True)
    affected = [s for s in sentinels if (s.links or [])]
    firings = [_evaluate(db, context, s, "change") for s in affected]
    return _summary(firings)


def run_scheduled(db: Session) -> dict:
    """Durably enqueue and drain every due Sentinel schedule cycle.

    ``last_scanned_at`` is a success watermark, not a pre-execution claim. The
    outbox's unique due-cycle key arbitrates concurrent scanners and its stale
    claim recovery survives process termination.
    """
    from app.ontologies.sentinels.cdc import (
        drain_cdc_outbox,
        ensure_scheduled_scan_event,
    )

    now = _now()
    released_ids = [row[0] for row in db.query(OntologyProject.id).join(
        OntologyVersion,
        OntologyVersion.id == OntologyProject.current_release_id,
    ).filter(
        OntologyVersion.node_kind == "release",
        OntologyVersion.lifecycle_status == "released",
    ).distinct().all()]
    runtime_errors: list[dict] = []
    scheduled: list[tuple[object, object]] = []
    for ontology_id in released_ids:
        context, errors = _prepare_runtime(db, ontology_id)
        if context is None or errors:
            runtime_errors.extend(errors)
            continue
        scheduled.extend(
            (context, sentinel)
            for sentinel in _runtime_sentinels(
                db, context, on_schedule=True)
        )
    due = []
    for context, s in scheduled:
        interval = timedelta(seconds=max(s.scan_interval_seconds or 300, 5))
        last = s.last_scanned_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or (now - last) >= interval:
            due.append((context, s))
    event_ids: set[str] = set()
    for context, s in due:
        live = getattr(s, "_live_row", None)
        if live is None:
            runtime_errors.append({
                "ontologyId": s.ontology_id,
                "sentinelId": s.id,
                "stage": "claim_schedule",
                "code": "schedule_state_unavailable",
                "error": "发布哨兵缺少调度状态行，已阻断定时执行",
            })
            continue
        event_id = ensure_scheduled_scan_event(
            db,
            ontology_id=context.project.id,
            ontology_release_id=context.id,
            sentinel_id=s.id,
            previous_last_scanned_at=live.last_scanned_at,
            scheduled_at=now,
            sentinel_origin=str(s.origin or ""),
            definition_revision=(
                int(live.definition_revision or 0)
                if s.origin == "assistant_dynamic" else None
            ),
            enable_generation=int(live.enable_generation or 0),
        )
        if event_id is None:
            runtime_errors.append({
                "ontologyId": s.ontology_id,
                "sentinelId": s.id,
                "stage": "claim_schedule",
                "code": "schedule_outbox_unavailable",
                "error": "定时扫描无法写入持久任务，已阻断执行",
            })
            continue
        event_ids.add(event_id)

    if not event_ids:
        return _summary([], runtime_errors)

    factory = sessionmaker(
        bind=db.get_bind(), expire_on_commit=False)
    drained = drain_cdc_outbox(
        event_ids=event_ids,
        limit=max(len(event_ids), 1),
        session_factory=factory,
    )
    if drained.get("errors"):
        runtime_errors.extend([{
            "stage": "scheduled_outbox",
            "code": "sentinel_evaluation_failed",
            "eventId": item.get("eventId"),
            "error": "定时哨兵执行失败，持久任务将按策略重试",
        } for item in drained["errors"]])

    db.expire_all()
    event_rows = db.query(SentinelCdcOutbox).filter(
        SentinelCdcOutbox.id.in_(event_ids),
    ).all()
    for row in event_rows:
        if row.status in ("dead", "cdc_dead"):
            runtime_errors.append({
                "ontologyId": row.ontology_id,
                "sentinelId": row.sentinel_id,
                "eventId": row.id,
                "stage": "scheduled_outbox",
                "code": "scheduled_outbox_dead",
                "status": "dead",
                "error": "定时哨兵持久任务已进入死信，成功水位未推进",
            })
    completed = [
        row for row in event_rows if row.status == "completed"
    ]
    payloads = [
        row.result_json for row in completed
        if isinstance(row.result_json, dict)
    ]
    return {
        "evaluated": sum(int(item.get("evaluated", 0)) for item in payloads),
        "fired": sum(int(item.get("fired", 0)) for item in payloads),
        "errors": (
            sum(int(item.get("errors", 0)) for item in payloads)
            + len(runtime_errors)
        ),
        "no_change": sum(
            int(item.get("no_change", 0)) for item in payloads),
        "no_match": sum(
            int(item.get("no_match", 0)) for item in payloads),
        "pending": sum(int(item.get("pending", 0)) for item in payloads),
        "muted": sum(int(item.get("muted", 0)) for item in payloads),
        "runtimeErrors": runtime_errors,
        "firings": [
            firing
            for item in payloads
            for firing in (item.get("firings") or [])
        ],
    }
