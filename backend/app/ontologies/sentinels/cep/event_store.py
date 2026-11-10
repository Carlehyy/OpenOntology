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
    func, select, text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    EVENT_SOURCE_EXTERNAL,
    EVENT_SOURCE_RELEASE_ACTIVATION,
    PREVIOUS_VALUES_QUERY_CAP,
    chunk_ids,
    external_outbox_dedupe_key,
)
from app.ontologies.sentinels.models import SentinelCdcOutbox, SentinelEventLog

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


def record_external_change(
        db: Session, *, ontology_id: str, release_id: str,
        object_type_id: str, instance_id: str, source_event_id: str,
        event_group_id: str, asset_id: str, mapping_id: str,
        change_kind: str, field_changes: dict[str, dict],
        snapshot: dict, occurred_at: datetime | None = None) -> bool:
    """Append one source-row event's mapped key facts without transaction merge.

    ``field_changes`` maps each property key to ``{"old": ..., "new": ...}``.
    The caller owns the SQL transaction and writes the matching Sentinel outbox
    row in the same transaction. A duplicate key fact is an idempotent replay;
    ``False`` therefore means every fact of this source event already existed.
    """
    if not field_changes:
        return False
    if not all((release_id, asset_id, mapping_id, source_event_id)):
        raise ValueError("external event identity is missing its release scope")
    occurred = occurred_at or _now()
    table = SentinelEventLog.__table__
    rows = [{
        "ontology_id": str(ontology_id),
        "ontology_release_id": str(release_id),
        "object_type_id": str(object_type_id),
        "instance_id": str(instance_id),
        "source_event_id": str(source_event_id),
        "event_group_id": str(event_group_id),
        "external_asset_id": str(asset_id),
        "external_mapping_id": str(mapping_id),
        "event_snapshot": dict(snapshot),
        "change_kind": str(change_kind),
        "key": str(key),
        "old_value": values.get("old"),
        "new_value": values.get("new"),
        "source": EVENT_SOURCE_EXTERNAL,
        "cascade_depth": 0,
        "chain_id": None,
        "occurred_at": occurred,
    } for key, values in sorted(field_changes.items())]
    dialect = db.get_bind().dialect.name
    conflict_columns = [
        table.c.ontology_id, table.c.ontology_release_id,
        table.c.external_asset_id, table.c.external_mapping_id,
        table.c.object_type_id, table.c.source_event_id, table.c.key,
    ]
    if dialect == "postgresql":
        statement = pg_insert(table).values(rows).on_conflict_do_nothing(
            index_elements=conflict_columns,
            index_where=text("source_event_id IS NOT NULL"),
        )
        return bool(db.execute(statement).rowcount)
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(rows).on_conflict_do_nothing(
            index_elements=conflict_columns,
            index_where=text("source_event_id IS NOT NULL"),
        )
        return bool(db.execute(statement).rowcount)
    # A read-then-insert fallback cannot uphold the unique index under
    # concurrency; refuse instead of silently duplicating source facts.
    raise NotImplementedError(
        f"external CDC facts require PostgreSQL or SQLite, not {dialect}")


def external_source_event_rows(
        db: Session, *, ontology_id: str, release_id: str,
        asset_id: str, mapping_id: str, object_type_id: str,
        source_event_id: str) -> list:
    """All key facts of exactly one source row event (release-scoped).

    The outbox row committed with these facts is the processing unit, so this
    identity lookup—never a shared id watermark—drives external evaluation.
    """
    return db.query(SentinelEventLog).filter(
        SentinelEventLog.ontology_id == str(ontology_id),
        SentinelEventLog.ontology_release_id == str(release_id),
        SentinelEventLog.external_asset_id == str(asset_id),
        SentinelEventLog.external_mapping_id == str(mapping_id),
        SentinelEventLog.object_type_id == str(object_type_id),
        SentinelEventLog.source_event_id == str(source_event_id),
        SentinelEventLog.source == EVENT_SOURCE_EXTERNAL,
    ).order_by(SentinelEventLog.id.asc()).all()


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
    远程（external）事实不走共享水位：事件日志 id 在提交前分配，晚提交的
    源可能落在已推进的水位之下而被永久跳过，因此它们按 outbox 事件逐个
    经 ``external_source_event_rows`` 身份查询消费。
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
# 远程（external）事实的裁剪资格由它的触发 outbox 行决定：
# - 触发行存在且未 completed（pending/processing/retry/dead）→ 一律保留：
#   宁可外部摄取因容量暂停并显式报错，也不静默删掉未处理事实；
# - 触发行已 completed → 可裁；
# - 触发行已不存在 → 可裁。外部触发行与事实同事务写入，此后唯一会删除它
#   的是 ``cdc.prune_completed_outbox``，而它只删 completed 行（含"完成行数
#   超限"时提前删除仅满 1 小时的行），所以"行缺失"只能意味着该事件早已
#   处理完成。若把缺失当作未知而永久保留，这些事实将永远不可裁，最终把
#   事件日志顶满、锁死外部摄取。
_EXTERNAL_OUTBOX_DONE = "completed"


def _external_fact_ids_released_by_outbox(db: Session, rows) -> list[int]:
    """候选行中可裁的 id：本地行原样放行；远程事实仅当触发行未在途。"""
    safe: list[int] = []
    pending_review: dict[str, list[int]] = {}
    for row in rows:
        if not row.source_event_id:
            safe.append(int(row.id))
            continue
        dedupe = external_outbox_dedupe_key(
            ontology_id=row.ontology_id,
            release_id=row.ontology_release_id or "",
            asset_id=row.external_asset_id or "",
            mapping_id=row.external_mapping_id or "",
            object_type_id=row.object_type_id,
            source_event_id=row.source_event_id,
        )
        pending_review.setdefault(dedupe, []).append(int(row.id))
    unfinished: set[str] = set()
    for chunk in chunk_ids(list(pending_review)):
        unfinished.update(
            str(item[0]) for item in db.query(SentinelCdcOutbox.dedupe_key)
            .filter(
                SentinelCdcOutbox.dedupe_key.in_(chunk),
                SentinelCdcOutbox.status != _EXTERNAL_OUTBOX_DONE,
            ).all()
        )
    for dedupe, ids in pending_review.items():
        if dedupe not in unfinished:
            safe.extend(ids)
    return safe


def prune_event_log(session_factory=None) -> int:
    """按固定保留期与行数上限裁剪事件日志；每批独立提交。

    本地（organic/release_activation）行为不变：``occurred_at`` 过保留期
    即裁。远程（external）事实以平台入库时间 ``created_at`` 计龄（积压的
    旧 source 时间戳不应刚入库就被裁），且绝不裁触发行仍在途的部分——
    超出硬上限时同样如此，改为显式报错，由外部摄取的容量暂停兜底。
    触发行已 completed 或已被 outbox 裁剪删除的事实按同一规则正常可裁。
    """
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal
    db = session_factory()
    deleted = 0

    def _candidate_columns():
        return (
            SentinelEventLog.id, SentinelEventLog.source_event_id,
            SentinelEventLog.ontology_id, SentinelEventLog.ontology_release_id,
            SentinelEventLog.external_asset_id, SentinelEventLog.external_mapping_id,
            SentinelEventLog.object_type_id,
        )

    try:
        cutoff = _now() - timedelta(seconds=EVENT_LOG_RETENTION_SECONDS)
        # 1) 本地事实：保留期裁剪（行为与既有实现一致）。
        while True:
            ids = [
                row[0] for row in db.query(SentinelEventLog.id).filter(
                    SentinelEventLog.source_event_id.is_(None),
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
        # 2) 远程事实：created_at 过保留期且触发行不在途（已 completed 或已
        #    被 outbox 裁剪删除）。keyset 游标推进，被保护行不会卡住循环。
        external_cursor = 0
        while True:
            rows = db.query(*_candidate_columns()).filter(
                SentinelEventLog.source_event_id.isnot(None),
                SentinelEventLog.created_at < cutoff,
                SentinelEventLog.id > external_cursor,
            ).order_by(SentinelEventLog.id.asc()).limit(
                EVENT_LOG_PRUNE_BATCH).all()
            if not rows:
                break
            external_cursor = int(rows[-1].id)
            ids = _external_fact_ids_released_by_outbox(db, rows)
            if not ids:
                continue
            db.query(SentinelEventLog).filter(
                SentinelEventLog.id.in_(ids),
            ).delete(synchronize_session=False)
            db.commit()
            deleted += len(ids)
        # 3) 行数硬上限：同一资格规则，最旧优先，绝不裁受保护行。
        total = db.query(func.count(SentinelEventLog.id)).scalar() or 0
        overflow = max(0, int(total) - EVENT_LOG_MAX_ROWS)
        scan_cursor = 0
        while overflow > 0:
            rows = db.query(*_candidate_columns()).filter(
                SentinelEventLog.id > scan_cursor,
            ).order_by(SentinelEventLog.id.asc()).limit(
                EVENT_LOG_PRUNE_BATCH).all()
            if not rows:
                logger.error(
                    "哨兵事件日志超上限 %d 行且剩余 %d 行全部是未完成的"
                    "远程事实：拒绝继续裁剪，外部摄取将因容量暂停",
                    int(total), overflow)
                break
            scan_cursor = int(rows[-1].id)
            ids = sorted(_external_fact_ids_released_by_outbox(db, rows))[
                :overflow]
            if not ids:
                continue
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
