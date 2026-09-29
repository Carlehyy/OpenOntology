"""Durable DatasetVersion publication events and automation dispatch."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, or_
from sqlalchemy.orm import Session

from app.data_channel.datasets.models import (
    Dataset, DatasetVersion, DatasetVersionEvent,
)
# 该导入同时是 tests/v2/datasets/test_version_automation_events.py 的引用缝，
# 保留再导出（函数本体在 automation_policy，发布门禁等多处使用）。
from app.data_channel.datasets.automation_policy import (
    manual_dataset_automation_eligibility,
)
from app.data_channel.datasets.version_event_outbox import (
    CURATED_REVIEW_APPROVED_EVENT,
    CURATED_REVIEW_PENDING_STATUS,
    CURATED_REVIEW_PROCESSING_STATUS,
    CURATED_REVIEW_RETRY_STATUS,
    VERSION_PUBLISHED_EVENT,
)

logger = logging.getLogger(__name__)

_LEGACY_PENDING_STATUS = "pending"
_LEGACY_PROCESSING_STATUS = "processing"
_LEGACY_RETRY_STATUS = "retry"


@dataclass(frozen=True)
class _DispatchOutcome:
    """Claim-bound proof of which event handler produced a result.

    ``result_json`` is deliberately not trusted as an untyped success value:
    version publication and curated approval have different governance
    semantics.  Keeping the route identity beside the result lets the final
    compare-and-set fail closed if a stale Session identity or a future routing
    regression ever hands one event the other handler's result.
    """

    event_id: str
    claim_token: str
    event_type: str
    dataset_id: str
    dataset_version_id: str
    handler: str
    result: dict
    review_id: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _processing_status(event_type: str) -> str:
    if event_type == CURATED_REVIEW_APPROVED_EVENT:
        return CURATED_REVIEW_PROCESSING_STATUS
    return _LEGACY_PROCESSING_STATUS


def _retry_status(event_type: str) -> str:
    if event_type == CURATED_REVIEW_APPROVED_EVENT:
        return CURATED_REVIEW_RETRY_STATUS
    return _LEGACY_RETRY_STATUS


def _ready_event_filter(now: datetime, stale_before: datetime):
    """Return the namespaced ready/stale claim predicate.

    Legacy approval rows are intentionally accepted so an already queued
    ``pending``/``retry`` event can be upgraded in place without a migration.
    The successful CAS immediately moves it to ``review_processing``.  New
    approval rows never enter the legacy namespace and are therefore safe from
    old workers during a rolling deployment.  A pre-upgrade row still using a
    generic state can inherently race an old worker until that worker is
    stopped; no code-only predicate can revoke a claim protocol both binaries
    intentionally share.
    """
    legacy_ready = or_(
        and_(
            DatasetVersionEvent.status.in_(
                (_LEGACY_PENDING_STATUS, _LEGACY_RETRY_STATUS)),
            DatasetVersionEvent.available_at <= now,
        ),
        and_(
            DatasetVersionEvent.status == _LEGACY_PROCESSING_STATUS,
            DatasetVersionEvent.claimed_at < stale_before,
        ),
    )
    curated_ready = or_(
        and_(
            DatasetVersionEvent.status.in_((
                CURATED_REVIEW_PENDING_STATUS,
                CURATED_REVIEW_RETRY_STATUS,
                # Migration-free compatibility for events created before this
                # status namespace was introduced.
                _LEGACY_PENDING_STATUS,
                _LEGACY_RETRY_STATUS,
            )),
            DatasetVersionEvent.available_at <= now,
        ),
        and_(
            DatasetVersionEvent.status.in_((
                CURATED_REVIEW_PROCESSING_STATUS,
                # A legacy worker may have died while owning an approval row.
                _LEGACY_PROCESSING_STATUS,
            )),
            DatasetVersionEvent.claimed_at < stale_before,
        ),
    )
    return or_(
        and_(
            DatasetVersionEvent.event_type == CURATED_REVIEW_APPROVED_EVENT,
            curated_ready,
        ),
        and_(
            DatasetVersionEvent.event_type != CURATED_REVIEW_APPROVED_EVENT,
            legacy_ready,
        ),
    )


def _claim_one(db: Session, event_id: str, now: datetime, stale_before: datetime) -> str | None:
    token = str(uuid.uuid4())
    changed = db.query(DatasetVersionEvent).filter(
        DatasetVersionEvent.id == event_id,
        _ready_event_filter(now, stale_before),
    ).update({
        DatasetVersionEvent.status: case(
            (
                DatasetVersionEvent.event_type
                == CURATED_REVIEW_APPROVED_EVENT,
                CURATED_REVIEW_PROCESSING_STATUS,
            ),
            else_=_LEGACY_PROCESSING_STATUS,
        ),
        DatasetVersionEvent.claimed_at: now,
        DatasetVersionEvent.claim_token: token,
        DatasetVersionEvent.attempts: DatasetVersionEvent.attempts + 1,
        DatasetVersionEvent.updated_at: now,
    }, synchronize_session=False)
    db.commit()
    return token if changed == 1 else None


def _outcome(
    event: DatasetVersionEvent,
    token: str,
    *,
    handler: str,
    result: dict,
    review_id: str | None = None,
) -> _DispatchOutcome:
    return _DispatchOutcome(
        event_id=event.id,
        claim_token=token,
        event_type=event.event_type,
        dataset_id=event.dataset_id,
        dataset_version_id=event.dataset_version_id,
        handler=handler,
        result=result,
        review_id=review_id,
    )


def _process_claimed_event(
    db: Session, event_id: str, token: str,
) -> _DispatchOutcome:
    # ``drain_dataset_version_events`` also accepts a caller-owned Session.
    # Refresh an already-cached identity explicitly so routing never relies on
    # a pre-claim snapshot of this durable row.
    event = db.query(DatasetVersionEvent).execution_options(
        populate_existing=True,
    ).filter(
        DatasetVersionEvent.id == event_id,
        DatasetVersionEvent.status.in_((
            _LEGACY_PROCESSING_STATUS,
            CURATED_REVIEW_PROCESSING_STATUS,
        )),
        DatasetVersionEvent.claim_token == token,
    ).first()
    if event is None or event.status != _processing_status(event.event_type):
        raise RuntimeError(f"DatasetVersion event claim lost: {event_id}")

    dataset = db.query(Dataset).filter(Dataset.id == event.dataset_id).first()
    version = db.query(DatasetVersion).filter(
        DatasetVersion.id == event.dataset_version_id,
        DatasetVersion.dataset_id == event.dataset_id,
    ).first()
    if dataset is None or version is None:
        return _outcome(
            event,
            token,
            handler="dataset_or_version_deleted",
            result={
                "status": "skipped",
                "reason": "dataset_or_version_deleted",
                "dataset_id": event.dataset_id,
                "dataset_version_id": event.dataset_version_id,
            },
        )
    if dataset.latest_version_id != version.id:
        # Full-reconciliation consumers only need the newest snapshot.  Marking
        # older queued versions complete prevents a restart backlog from
        # repeatedly rebuilding states that have already been superseded.
        return _outcome(
            event,
            token,
            handler="superseded",
            result={
                "status": "skipped",
                "reason": "superseded",
                "dataset_id": dataset.id,
                "dataset_version_id": version.id,
                "latest_version_id": dataset.latest_version_id,
            },
        )

    from app.data_channel.sync_tasks.incremental_orchestrator import (
        IncrementalOrchestrator,
    )
    orchestrator = IncrementalOrchestrator(db)
    if event.event_type == VERSION_PUBLISHED_EVENT:
        return _outcome(
            event,
            token,
            handler="on_dataset_version_published",
            result=orchestrator.on_dataset_version_published(
                dataset.id, version.id),
        )
    if event.event_type == CURATED_REVIEW_APPROVED_EVENT:
        from app.data_channel.curated.models import CuratedReview

        review = (db.query(CuratedReview).filter(
            CuratedReview.curated_dataset_id == dataset.id,
            CuratedReview.dataset_version_id == version.id,
            CuratedReview.status == "approved",
        ).order_by(
            CuratedReview.decided_at.desc(),
            CuratedReview.created_at.desc(),
        ).first())
        if review is None:
            # An approval event without its exact approved review is an
            # integrity failure, not a successful no-op.  Keep it retryable so
            # users cannot see "completed" while governance proof is missing.
            raise RuntimeError(
                "curated_review_approved event has no approved "
                f"version-bound review: event={event.id}, "
                f"dataset={dataset.id}, version={version.id}")
        # This call runs Mapping synchronously inside the durable event claim.
        # Only its complete Formal projection + Sentinel barrier lets the
        # caller acknowledge the outbox row.
        return _outcome(
            event,
            token,
            handler="on_review_approved",
            result=orchestrator.on_review_approved(
                review.id, synchronous=True),
            review_id=review.id,
        )
    raise RuntimeError(
        f"Unsupported DatasetVersion event type: {event.event_type}")


def _validate_dispatch_outcome(
    event_id: str,
    token: str,
    outcome: _DispatchOutcome,
) -> None:
    """Reject cross-routed or identity-mismatched results before acknowledgement."""
    if not isinstance(outcome, _DispatchOutcome):
        raise RuntimeError(
            "DatasetVersion event handler returned an unbound result")
    if outcome.event_id != event_id or outcome.claim_token != token:
        raise RuntimeError(
            "DatasetVersion event handler result does not belong to "
            "the active claim")
    if not isinstance(outcome.result, dict):
        raise RuntimeError(
            "DatasetVersion event handler returned a non-object result")

    common_handlers = {"dataset_or_version_deleted", "superseded"}
    expected_handler = {
        VERSION_PUBLISHED_EVENT: "on_dataset_version_published",
        CURATED_REVIEW_APPROVED_EVENT: "on_review_approved",
    }.get(outcome.event_type)
    if expected_handler is None:
        raise RuntimeError(
            f"Unsupported DatasetVersion event type: {outcome.event_type}")
    if outcome.handler not in common_handlers | {expected_handler}:
        raise RuntimeError(
            "DatasetVersion event dispatch identity mismatch: "
            f"event_type={outcome.event_type}, handler={outcome.handler}")

    result = outcome.result
    if result.get("dataset_id") != outcome.dataset_id:
        raise RuntimeError(
            "DatasetVersion event result dataset identity mismatch")
    if outcome.handler in common_handlers:
        if result.get("dataset_version_id") != outcome.dataset_version_id:
            raise RuntimeError(
                "DatasetVersion event result version identity mismatch")
        return

    if outcome.event_type == VERSION_PUBLISHED_EVENT:
        if (
            result.get("dataset_version_id") != outcome.dataset_version_id
            or not isinstance(result.get("manual_mapping"), dict)
        ):
            raise RuntimeError(
                "version_published handler returned an incompatible result")
        return

    if (
        not outcome.review_id
        or result.get("review_id") != outcome.review_id
        or not isinstance(result.get("triggered_mappings"), list)
    ):
        raise RuntimeError(
            "curated_review_approved handler returned an incompatible result")


def _finalize_success(
    db: Session,
    event_id: str,
    token: str,
    dispatch_outcome: _DispatchOutcome,
) -> bool:
    """仅由当前 claim owner 确认事件成功。"""
    from fastapi.encoders import jsonable_encoder

    _validate_dispatch_outcome(event_id, token, dispatch_outcome)
    completed_at = _now()
    changed = db.query(DatasetVersionEvent).filter(
        DatasetVersionEvent.id == event_id,
        DatasetVersionEvent.status == _processing_status(
            dispatch_outcome.event_type),
        DatasetVersionEvent.claim_token == token,
        DatasetVersionEvent.event_type == dispatch_outcome.event_type,
        DatasetVersionEvent.dataset_id == dispatch_outcome.dataset_id,
        DatasetVersionEvent.dataset_version_id
        == dispatch_outcome.dataset_version_id,
    ).update({
        DatasetVersionEvent.status: "completed",
        DatasetVersionEvent.result_json: jsonable_encoder(
            dispatch_outcome.result),
        DatasetVersionEvent.last_error: None,
        DatasetVersionEvent.processed_at: completed_at,
        DatasetVersionEvent.claimed_at: None,
        DatasetVersionEvent.claim_token: None,
        DatasetVersionEvent.updated_at: completed_at,
    }, synchronize_session=False)
    db.commit()
    return changed == 1


def _finalize_failure(
    db: Session,
    event_id: str,
    token: str,
    error: BaseException,
) -> str:
    """将当前 claim 放回重试队列；过期 owner 不得覆盖接管者。"""
    db.rollback()
    event = db.query(DatasetVersionEvent).filter(
        DatasetVersionEvent.id == event_id,
        DatasetVersionEvent.status.in_((
            _LEGACY_PROCESSING_STATUS,
            CURATED_REVIEW_PROCESSING_STATUS,
        )),
        DatasetVersionEvent.claim_token == token,
    ).first()
    if event is None or event.status != _processing_status(event.event_type):
        # 上面的 miss 查询已经开启一个新读事务。调用方可能复用 Session 继续
        # drain，必须显式结束它，避免悬挂快照或长期占用连接。
        db.rollback()
        return "lost_claim"

    now = _now()
    delay = min(300, 2 ** min(max(int(event.attempts or 1), 1), 8))
    changed = db.query(DatasetVersionEvent).filter(
        DatasetVersionEvent.id == event_id,
        DatasetVersionEvent.status == _processing_status(event.event_type),
        DatasetVersionEvent.claim_token == token,
    ).update({
        DatasetVersionEvent.status: _retry_status(event.event_type),
        DatasetVersionEvent.available_at: now + timedelta(seconds=delay),
        DatasetVersionEvent.claimed_at: None,
        DatasetVersionEvent.claim_token: None,
        DatasetVersionEvent.last_error: str(error)[:4000],
        DatasetVersionEvent.updated_at: now,
    }, synchronize_session=False)
    db.commit()
    return "retry" if changed == 1 else "lost_claim"


def drain_dataset_version_events(
    db: Session | None = None, *, limit: int | None = None,
    strict_schema: bool = False,
) -> dict:
    """Claim and dispatch a bounded event batch.

    Mapping is a full idempotent reconciliation.  The event is acknowledged
    only after it completes; a crash between projection commit and ack can
    replay safely, while sentinel edge state prevents duplicate notifications.

    dispatch_mode=async 时本函数只负责 claim + 派发 Celery 任务：重活在
    独立 worker 进程执行，worker 完成后确认事件；派发失败降级为内联执行
    （fail-open）。sync 模式保留旧行为（内联执行后确认）。
    """
    from app.config import settings
    from app.database import SessionLocal

    own_session = db is None
    session = db or SessionLocal()
    batch_size = max(1, int(limit or settings.dataset_event_batch_size or 20))
    dispatch_mode = str(
        settings.dataset_event_dispatch_mode or "async").strip().lower()
    result = {
        "processed": 0, "dispatched": 0, "retried": 0, "lost_claims": 0,
    }
    try:
        now = _now()
        stale_before = now - timedelta(
            seconds=max(30, int(settings.dataset_event_claim_timeout_seconds or 300)))
        candidates = session.query(DatasetVersionEvent.id).filter(
            _ready_event_filter(now, stale_before),
        ).order_by(
            DatasetVersionEvent.created_at, DatasetVersionEvent.id,
        ).limit(batch_size).all()
    except Exception:
        session.rollback()
        if own_session:
            session.close()
        if strict_schema:
            raise
        logger.exception("读取 DatasetVersion 事件 outbox 失败")
        return {**result, "schema_error": True}

    for (event_id,) in candidates:
        claim_now = _now()
        token: str | None = None
        try:
            token = _claim_one(session, event_id, claim_now, stale_before)
            if token is None:
                result["lost_claims"] += 1
                continue
            if dispatch_mode != "sync":
                # 经 NATS 派发到 executor；不 import 任务模块——避免
                # version_events ↔ 任务模块的静态依赖环（架构守卫约束）。
                from app.data_channel.pipeline_tasks.dispatch import (
                    DATASET_EVENT_SUBJECT, dispatch_task,
                )

                try:
                    dispatch_task(DATASET_EVENT_SUBJECT, {
                        "event_id": event_id, "claim_token": token})
                    # 事件保持 claimed：executor 完成后经 claim CAS 确认；
                    # executor 崩溃则超时后由下一轮 drain 重新派发（对账幂等）。
                    result["dispatched"] += 1
                    logger.info(
                        "DatasetVersion event %s dispatched to executor",
                        event_id,
                    )
                    continue
                except Exception as dispatch_error:  # noqa: BLE001
                    # NATS 不可用时 fail-open：降级为内联执行，
                    # 保证自动化链路不断；executor 恢复后自动回到异步。
                    logger.warning(
                        "DatasetVersion event %s NATS 派发失败，降级内联: %s",
                        event_id, dispatch_error,
                    )
            dispatch_outcome = _process_claimed_event(
                session, event_id, token)
            if not _finalize_success(
                    session, event_id, token, dispatch_outcome):
                result["lost_claims"] += 1
                continue
            result["processed"] += 1
        except Exception as exc:  # noqa: BLE001 - durable retry boundary
            if token is None:
                # claim 本身失败时同样要清理 Session 的失败事务，后续候选项
                # 才不会继承 PendingRollbackError。
                session.rollback()
                status = "lost_claim"
            else:
                status = _finalize_failure(
                    session, event_id, token, exc)
            if status == "retry":
                result["retried"] += 1
                logger.exception(
                    "DatasetVersion 自动化派发失败，已进入重试: %s",
                    event_id,
                )
            else:
                result["lost_claims"] += 1
                logger.warning(
                    "DatasetVersion 自动化派发结束时 claim 已被接管: %s",
                    event_id,
                    exc_info=True,
                )
    if own_session:
        session.close()
    return result


def run_claimed_event(
    event_id: str, claim_token: str, *, db: Session | None = None,
    strict_schema: bool = False,
) -> dict:
    """Worker 进程入口：处理一个已被 claim 的事件并确认其终态。

    等价于 sync 模式 drain 对单个事件的处理，但运行在 Celery worker 进程
    （API 进程只派发）。成功经 _finalize_success 确认；失败经
    _finalize_failure 回退重试队列；claim 已被接管时抛错（幂等重跑安全）。
    测试可传入自有 Session；worker 进程默认使用 SessionLocal。
    """
    from app.database import SessionLocal

    own_session = db is None
    session = db or SessionLocal()
    try:
        outcome = _process_claimed_event(session, event_id, claim_token)
        if not _finalize_success(session, event_id, claim_token, outcome):
            raise RuntimeError(
                f"DatasetVersion event claim lost before finalize: {event_id}")
        return outcome.result
    except Exception as exc:  # noqa: BLE001 - durable retry boundary
        status = _finalize_failure(session, event_id, claim_token, exc)
        if status != "retry":
            logger.warning(
                "DatasetVersion event %s 失败且 claim 已被接管: %s",
                event_id, status,
            )
        raise
    finally:
        if own_session:
            session.close()
