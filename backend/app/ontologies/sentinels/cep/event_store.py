"""事件日志存取层 — 采集（由 cdc 钩子调用）、查询（时间算子）、裁剪。

叶子模块：只依赖 models 与 contract，不 import cdc/engine/evaluator。
release 归属与来源标记由调用方（cdc 的 ``_before_flush``）解析后传入，
本层不重复实现 release 指针解析规则。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger, Column, MetaData, String, Table, bindparam, event,
    func, select,
)
from sqlalchemy.schema import CreateTable
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

# Only unflushed rows may be held in Python. Persisted merge anchors live in a
# connection-local TEMP table, never in Base.metadata or the persistent schema.
_EVENT_PENDING_KEY = "sentinel_event_log_pending"
_ANCHOR_BATCH_SIZE = 500
_ANCHORS = Table(
    "sentinel_event_log_tx_anchors", MetaData(),
    Column("anchor_key", String(64), primary_key=True),
    Column("event_id", BigInteger, nullable=False),
    prefixes=["TEMPORARY"],
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connection(session: Session):
    return session.connection(bind_arguments={"mapper": SentinelEventLog})


def _anchor_key(instance_id, key) -> str:
    # Length-prefixing makes the tuple unambiguous; hashing keeps MySQL TEMP
    # primary keys bounded even for long instance identifiers/property names.
    instance_id = str(instance_id)
    return hashlib.sha256(
        f"{len(instance_id)}:{instance_id}{key}".encode("utf-8")).hexdigest()


def _pending(session: Session) -> dict:
    transaction = session.get_transaction()
    pending = session.info.get(_EVENT_PENDING_KEY)
    if pending is None or pending["transaction"] is not transaction:
        connection = _connection(session)
        connection.execute(CreateTable(_ANCHORS, if_not_exists=True))
        # TEMP tables survive pooled-connection reuse. Reset their DML at the
        # first capture in each root transaction; never commit/rollback here.
        connection.execute(_ANCHORS.delete())
        pending = {"transaction": session.get_transaction(), "rows": {}}
        session.info[_EVENT_PENDING_KEY] = pending
    return pending


def discard_pending(session: Session) -> None:
    """Forget flush-local references after commit/rollback (safe to repeat).

    TEMP anchor DML shares the business transaction. The next transaction resets
    any anchors left by a commit before consulting them, including on a reused
    pooled connection. No SQL is issued from CDC's after_commit callback.
    """
    session.info.pop(_EVENT_PENDING_KEY, None)


def _anchor_flushed_rows(session: Session, _flush_context) -> None:
    pending = session.info.get(_EVENT_PENDING_KEY)
    if pending is None or not pending["rows"]:
        return
    connection = _connection(session)
    batch = []
    for key, row in pending["rows"].items():
        batch.append({"anchor_key": key, "event_id": row.id})
        if len(batch) == _ANCHOR_BATCH_SIZE:
            connection.execute(_ANCHORS.insert(), batch)
            batch.clear()
    if batch:
        connection.execute(_ANCHORS.insert(), batch)
    # ORM inserted these rows normally, so every other flush listener can still
    # observe them. Once anchored, neither session.info nor this module retains
    # any persisted event objects or values until the business transaction ends.
    pending["rows"].clear()


event.listen(Session, "after_flush_postexec", _anchor_flushed_rows)


def capture_instance_changes(
        session: Session, instance, *, release_id: str | None,
        change_kind: str, changes: dict,
        source: str = EVENT_SOURCE_ORGANIC,
        cascade_depth: int = 0, chain_id: str | None = None) -> None:
    """Capture atomic key-level facts with flush-bounded Python retention.

    Keep earliest old/release/source metadata, latest new, and created priority
    across all flushes. As before, the merge identity is (instance_id, key), not
    release_id; activation retrospectively relabels this transaction's facts.
    """
    if release_id is None or not changes:
        return
    pending = _pending(session)
    rows = pending["rows"]
    changes_by_anchor = {
        _anchor_key(instance.id, str(key)): (str(key), *changes[key])
        for key in sorted(changes)
    }
    connection = _connection(session)
    persisted = {}
    for chunk in chunk_ids(changes_by_anchor):
        persisted.update(connection.execute(select(
            _ANCHORS.c.anchor_key, _ANCHORS.c.event_id,
        ).where(_ANCHORS.c.anchor_key.in_(chunk))).all())
    updates = []
    for anchor, (key, old, new) in changes_by_anchor.items():
        row = rows.get(anchor)
        if row is not None:
            row.new_value = new
            if change_kind == EVENT_KIND_CREATED:
                row.change_kind = EVENT_KIND_CREATED
        elif anchor in persisted:
            updates.append({"event_id": persisted[anchor], "latest_value": new})
        else:
            row = SentinelEventLog(
                ontology_id=str(instance.ontology_id),
                ontology_release_id=release_id,
                object_type_id=str(instance.object_type_id),
                instance_id=str(instance.id), change_kind=change_kind, key=key,
                old_value=old, new_value=new, source=source,
                cascade_depth=int(cascade_depth), chain_id=chain_id,
                occurred_at=_now(),
            )
            rows[anchor] = row
            session.add(row)
    if updates:
        table = SentinelEventLog.__table__
        statement = table.update().where(table.c.id == bindparam("event_id")).values(
            new_value=bindparam("latest_value"),
            change_kind=(
                EVENT_KIND_CREATED if change_kind == EVENT_KIND_CREATED
                else table.c.change_kind
            ),
        )
        for offset in range(0, len(updates), _ANCHOR_BATCH_SIZE):
            connection.execute(statement, updates[offset:offset + _ANCHOR_BATCH_SIZE])
        # Core DML must not leave a previously queried ORM event stale in this
        # Session. Look up only touched identities, without loading extra rows.
        for item in updates:
            row = session.identity_map.get(session.identity_key(
                SentinelEventLog, item["event_id"]))
            if row is not None:
                session.expire(row, ["new_value", "change_kind"])


def relabel_pending_as_activation(session: Session, ontology_id: str) -> None:
    """Relabel only this transaction's facts, including previously flushed pages."""
    pending = session.info.get(_EVENT_PENDING_KEY)
    if pending is None:
        return
    for row in pending["rows"].values():
        if str(row.ontology_id) == str(ontology_id):
            row.source = EVENT_SOURCE_RELEASE_ACTIVATION
    table = SentinelEventLog.__table__
    _connection(session).execute(table.update().where(
        table.c.ontology_id == str(ontology_id),
        table.c.id.in_(select(_ANCHORS.c.event_id)),
    ).values(source=EVENT_SOURCE_RELEASE_ACTIVATION))
    # Invalidate only already-loaded event objects; do not fetch or retain the
    # transaction's entire event universe merely to synchronize ORM state.
    for row in session.identity_map.values():
        if isinstance(row, SentinelEventLog):
            session.expire(row, ["source"])


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
