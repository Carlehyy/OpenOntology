"""事件日志存取层 — 采集（由 cdc 钩子调用）、查询（时间算子）、裁剪。

叶子模块：只依赖 models 与 contract，不 import cdc/engine/evaluator。
release 归属与来源标记由调用方（cdc 的 ``_before_flush``）解析后传入，
本层不重复实现 release 指针解析规则。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ontologies.sentinels.cep.contract import (
    DELETED_EVENT_KEY,
    EVENT_KIND_CREATED,
    EVENT_KIND_DELETED,
    EVENT_LOG_MAX_ROWS,
    EVENT_LOG_PRUNE_BATCH,
    EVENT_LOG_RETENTION_SECONDS,
    EVENT_SOURCE_ORGANIC,
    EVENT_SOURCE_RELEASE_ACTIVATION,
    PREVIOUS_VALUES_QUERY_CAP,
    chunk_ids,
)
from app.ontologies.sentinels.models import SentinelEventLog

logger = logging.getLogger(__name__)

# 同一事务内多 flush 的合并锚点：instance_id -> {
#     "rows": {key: SentinelEventLog 行对象},
# }
_EVENT_PENDING_KEY = "sentinel_event_log_pending"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def discard_pending(session: Session) -> None:
    """提交/回滚后丢弃合并锚点。

    复用的 Session 在下一事务中不得继续合并上一事务的行对象——回滚后的
    悬挂引用会把新值写进已回滚/已提交的行。
    """
    session.info.pop(_EVENT_PENDING_KEY, None)


def capture_instance_changes(
        session: Session, instance, *, release_id: str | None,
        change_kind: str, changes: dict,
        source: str = EVENT_SOURCE_ORGANIC,
        cascade_depth: int = 0, chain_id: str | None = None) -> None:
    """在业务事务内记录实例级变更事实（键级行）。

    由 cdc ``_before_flush`` 调用；``release_id`` 为 None 时（draft-only/
    legacy 项目）与 outbox 同样直接丢弃——没有 runtime owner 的投影变更
    不构成可评估的时间事实。同事务同实例多次 flush 会在
    ``session.info`` 中合并：每个键保留最早 old、最新 new，``created``
    优先于 ``updated``。
    """
    if release_id is None or not changes:
        return
    pending = session.info.setdefault(_EVENT_PENDING_KEY, {})
    entry = pending.get(instance.id)
    if entry is None:
        rows: dict[str, SentinelEventLog] = {}
        for key in sorted(changes):
            old, new = changes[key]
            row = SentinelEventLog(
                ontology_id=str(instance.ontology_id),
                ontology_release_id=release_id,
                object_type_id=str(instance.object_type_id),
                instance_id=str(instance.id),
                change_kind=change_kind,
                key=str(key),
                old_value=old,
                new_value=new,
                source=source,
                cascade_depth=int(cascade_depth),
                chain_id=chain_id,
                occurred_at=_now(),
            )
            rows[str(key)] = row
            session.add(row)
        pending[instance.id] = {"rows": rows}
        return
    # 同事务第二次 flush：保留最早 old / 最早 change_kind(created 优先)，
    # 覆盖最新 new。行对象已在上一轮 flush 中持久化，直接置脏。
    rows = entry["rows"]
    for key in sorted(changes):
        old, new = changes[key]
        row = rows.get(str(key))
        if row is None:
            row = SentinelEventLog(
                ontology_id=str(instance.ontology_id),
                ontology_release_id=release_id,
                object_type_id=str(instance.object_type_id),
                instance_id=str(instance.id),
                change_kind=change_kind,
                key=str(key),
                old_value=old,
                new_value=new,
                source=source,
                cascade_depth=int(cascade_depth),
                chain_id=chain_id,
                occurred_at=_now(),
            )
            rows[str(key)] = row
            session.add(row)
            continue
        row.new_value = new
        if (change_kind == EVENT_KIND_CREATED
                and row.change_kind != EVENT_KIND_CREATED):
            row.change_kind = EVENT_KIND_CREATED


def relabel_pending_as_activation(session: Session, ontology_id: str) -> None:
    """发布切换事务内回溯改标本事务已捕获的实例事件为 release_activation。

    晋级/回滚把新投影物化（实例重建的事件在早前 flush 已按 organic 落行）
    之后才切换指针——捕获时刻无法得知自己属于激活窗口。本函数在指针切换
    的 before_flush 里调用：行对象仍在 session 中，置脏后随当前 flush 一并
    UPDATE，保证激活窗口的事件不参与时间算子/模式推进（核心不变量）。
    """
    pending = session.info.get(_EVENT_PENDING_KEY)
    if not pending:
        return
    for entry in pending.values():
        for row in entry.get("rows", {}).values():
            if str(getattr(row, "ontology_id", "")) == str(ontology_id):
                row.source = EVENT_SOURCE_RELEASE_ACTIVATION


def deleted_change_marker() -> tuple:
    """删除事件的统一载荷（无值差异）。"""
    return (None, None)


def deleted_key() -> str:
    return DELETED_EVENT_KEY


# ---------------------------------------------------------------------------
# 查询：时间算子的批量预取
# ---------------------------------------------------------------------------

def instances_with_recent_change(
        db: Session, instance_ids, key: str, since: datetime) -> set[str]:
    """返回 ``key`` 在 ``since`` 之后发生过 organic 变更的实例 id 集合。"""
    matched: set[str] = set()
    for chunk in chunk_ids(instance_ids):
        rows = db.query(SentinelEventLog.instance_id).filter(
            SentinelEventLog.instance_id.in_(chunk),
            SentinelEventLog.key == key,
            SentinelEventLog.source == EVENT_SOURCE_ORGANIC,
            SentinelEventLog.occurred_at >= since,
        ).distinct().all()
        matched.update(str(row[0]) for row in rows)
    return matched


def latest_previous_values(
        db: Session, instance_ids, keys) -> dict[tuple[str, str], object]:
    """批量回捞 (instance_id, key) → 最近一次 organic 变更的 old 值。

    按 id 倒序拉取、每个 (instance, key) 取首条；总量受
    ``PREVIOUS_VALUES_QUERY_CAP`` 约束，超限时未覆盖的引用自然缺失
    （调用方按 fail-closed 语义取 None）。无历史（保留期外/新实例）
    同样缺失——``prev()`` 对无历史返回 None 而不是报错。
    """
    result: dict[tuple[str, str], object] = {}
    collected = 0
    for chunk in chunk_ids(instance_ids):
        if collected >= PREVIOUS_VALUES_QUERY_CAP:
            break
        remaining = PREVIOUS_VALUES_QUERY_CAP - collected
        rows = db.query(
            SentinelEventLog.instance_id,
            SentinelEventLog.key,
            SentinelEventLog.old_value,
        ).filter(
            SentinelEventLog.instance_id.in_(chunk),
            SentinelEventLog.key.in_(list(keys)),
            SentinelEventLog.source == EVENT_SOURCE_ORGANIC,
        ).order_by(SentinelEventLog.id.desc()).limit(remaining).all()
        collected += len(rows)
        for instance_id, key, old_value in rows:
            pair = (str(instance_id), str(key))
            if pair not in result:
                result[pair] = old_value
    return result


def events_after_cursor(
        db: Session, ontology_id: str, object_type_ids,
        after_id: int, *, limit: int = 2000):
    """水位增量拉取：某本体在指定对象类型上的 organic 事件（按 id 升序）。

    模式匹配器（M2）以此推进状态机；outbox/定时扫描只是唤醒信号。
    """
    query = db.query(SentinelEventLog).filter(
        SentinelEventLog.ontology_id == str(ontology_id),
        SentinelEventLog.source == EVENT_SOURCE_ORGANIC,
        SentinelEventLog.id > int(after_id),
    )
    type_ids = [str(item) for item in (object_type_ids or []) if item]
    if type_ids:
        query = query.filter(SentinelEventLog.object_type_id.in_(type_ids))
    return query.order_by(SentinelEventLog.id.asc()).limit(limit).all()


# ---------------------------------------------------------------------------
# 裁剪：固定保留期 + 行数硬上限，分批 keyset DELETE 防长事务锁
# ---------------------------------------------------------------------------

def prune_event_log(session_factory=None) -> int:
    """按固定保留期与行数上限裁剪事件日志；每批独立提交。"""
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal
    db = session_factory()
    deleted = 0
    try:
        cutoff = _now() - timedelta(seconds=EVENT_LOG_RETENTION_SECONDS)
        while True:
            ids = [
                row[0] for row in db.query(SentinelEventLog.id).filter(
                    SentinelEventLog.occurred_at < cutoff,
                ).order_by(SentinelEventLog.id.asc()).limit(
                    EVENT_LOG_PRUNE_BATCH).all()
            ]
            if not ids:
                break
            db.query(SentinelEventLog).filter(
                SentinelEventLog.id.in_(ids),
            ).delete(synchronize_session=False)
            db.commit()
            deleted += len(ids)
        total = db.query(func.count(SentinelEventLog.id)).scalar() or 0
        overflow = max(0, int(total) - EVENT_LOG_MAX_ROWS)
        while overflow > 0:
            batch = min(overflow, EVENT_LOG_PRUNE_BATCH)
            ids = [
                row[0] for row in db.query(SentinelEventLog.id).order_by(
                    SentinelEventLog.id.asc()).limit(batch).all()
            ]
            if not ids:
                break
            db.query(SentinelEventLog).filter(
                SentinelEventLog.id.in_(ids),
            ).delete(synchronize_session=False)
            db.commit()
            deleted += len(ids)
            overflow -= len(ids)
        return int(deleted)
    except Exception:  # noqa: BLE001 — 裁剪失败只影响保留期，不影响主流程
        db.rollback()
        logger.exception("裁剪哨兵事件日志失败")
        return int(deleted)
    finally:
        db.close()
