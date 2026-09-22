"""Read-side queries for the Event Registry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.events import models as m
from app.events import service
from app.events.models import (
    EventAttachment,
    EventAuditLog,
    EventIngestKey,
    RegisteredEvent,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def as_utc(value: datetime) -> datetime:
    """Interpret naive database values as UTC and normalize aware values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def shanghai_day_start_utc(local_day) -> datetime:
    """Convert a Shanghai calendar-day boundary to naive database UTC."""
    local_start = datetime.combine(
        local_day,
        datetime.min.time(),
        tzinfo=SHANGHAI_TZ,
    )
    return local_start.astimezone(timezone.utc).replace(tzinfo=None)


def shanghai_date(value: datetime):
    return as_utc(value).astimezone(SHANGHAI_TZ).date()


def require_event(db: Session, event_id: str) -> RegisteredEvent:
    event = (
        db.query(RegisteredEvent)
        .filter(RegisteredEvent.id == event_id)
        .first()
    )
    if not event:
        raise HTTPException(404, "事件不存在")
    return event


def filtered_event_query(
    db: Session,
    *,
    q: Optional[str],
    source_type: Optional[str],
    event_type: Optional[str],
    severity: Optional[str],
    status: Optional[str],
    ontology_id: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
):
    """列表与导出共用的筛选构造。

    status 语义：缺省仅 active；"all" 不限状态；其余按指定状态精确过滤。
    """
    query = db.query(RegisteredEvent)
    if not status:
        query = query.filter(RegisteredEvent.status == m.STATUS_ACTIVE)
    elif status != "all":
        query = query.filter(RegisteredEvent.status == status)
    if source_type:
        query = query.filter(RegisteredEvent.source_type == source_type)
    if event_type:
        query = query.filter(RegisteredEvent.event_type == event_type)
    if severity:
        query = query.filter(RegisteredEvent.severity == severity)
    if ontology_id:
        query = query.filter(RegisteredEvent.ontology_id == ontology_id)
    if start:
        query = query.filter(RegisteredEvent.recorded_at >= start)
    if end:
        query = query.filter(RegisteredEvent.recorded_at <= end)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            RegisteredEvent.title.ilike(like)
            | RegisteredEvent.description.ilike(like)
            | RegisteredEvent.event_no.ilike(like)
            | RegisteredEvent.reporter_name.ilike(like)
        )
    return query


def list_events(
    db: Session,
    *,
    q: Optional[str],
    source_type: Optional[str],
    event_type: Optional[str],
    severity: Optional[str],
    status: Optional[str],
    ontology_id: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
    page: int,
    page_size: int,
) -> dict:
    query = filtered_event_query(
        db,
        q=q,
        source_type=source_type,
        event_type=event_type,
        severity=severity,
        status=status,
        ontology_id=ontology_id,
        start=start,
        end=end,
    )

    total = query.count()
    rows = (
        query.order_by(
            RegisteredEvent.recorded_at.desc(),
            RegisteredEvent.created_at.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    event_ids = [row.id for row in rows]
    attachment_counts: dict[str, int] = {}
    if event_ids:
        count_rows = (
            db.query(EventAttachment.event_id, func.count(EventAttachment.id))
            .filter(EventAttachment.event_id.in_(event_ids))
            .group_by(EventAttachment.event_id)
            .all()
        )
        attachment_counts.update(count_rows)
    return {
        "items": [
            service.event_out(
                row,
                attachment_count=attachment_counts.get(row.id, 0),
            )
            for row in rows
        ],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


# 单次导出上限：审计导出不支持无限拉全表，超限时明确截断并提示缩小范围。
EXPORT_MAX_ROWS = 10_000


def export_rows(
    db: Session,
    *,
    q: Optional[str],
    source_type: Optional[str],
    event_type: Optional[str],
    severity: Optional[str],
    status: Optional[str],
    ontology_id: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
) -> tuple[list, bool]:
    """按列表同款筛选取导出行，返回 (rows, 是否被截断)。"""
    query = filtered_event_query(
        db,
        q=q,
        source_type=source_type,
        event_type=event_type,
        severity=severity,
        status=status,
        ontology_id=ontology_id,
        start=start,
        end=end,
    ).order_by(
        RegisteredEvent.recorded_at.desc(),
        RegisteredEvent.created_at.desc(),
    )
    rows = query.limit(EXPORT_MAX_ROWS + 1).all()
    truncated = len(rows) > EXPORT_MAX_ROWS
    return rows[:EXPORT_MAX_ROWS], truncated


def stats_summary(db: Session, *, now_utc: datetime) -> dict:
    def count(*filters):
        query = db.query(func.count(RegisteredEvent.id))
        for expression in filters:
            query = query.filter(expression)
        return query.scalar() or 0

    local_today = now_utc.astimezone(SHANGHAI_TZ).date()
    today_start = shanghai_day_start_utc(local_today)
    tomorrow_start = shanghai_day_start_utc(local_today + timedelta(days=1))
    first_day = local_today - timedelta(days=6)
    trend = {
        (first_day + timedelta(days=index)).isoformat(): {
            severity: 0 for severity in m.SEVERITIES
        }
        for index in range(7)
    }
    recent = (
        db.query(RegisteredEvent.recorded_at, RegisteredEvent.severity)
        .filter(
            RegisteredEvent.recorded_at >= shanghai_day_start_utc(first_day),
            RegisteredEvent.recorded_at < tomorrow_start,
        )
        .all()
    )
    for recorded_at, severity in recent:
        if not recorded_at:
            continue
        day = shanghai_date(recorded_at).isoformat()
        if day not in trend:
            continue
        normalized_severity = (
            severity if severity in m.SEVERITIES else "info"
        )
        trend[day][normalized_severity] += 1

    return {
        "total": count(),
        "active": count(RegisteredEvent.status == m.STATUS_ACTIVE),
        "archived": count(RegisteredEvent.status == m.STATUS_ARCHIVED),
        "platform": count(RegisteredEvent.source_type == m.SOURCE_PLATFORM),
        "api": count(RegisteredEvent.source_type == m.SOURCE_API),
        "today": count(
            RegisteredEvent.recorded_at >= today_start,
            RegisteredEvent.recorded_at < tomorrow_start,
        ),
        # 口径统一：级别分布与 7 日趋势均统计全部事件（含归档），
        # 与 total/active/archived 卡片同口径，避免同屏数字互相矛盾（MYW-42）。
        "bySeverity": {
            severity: count(RegisteredEvent.severity == severity)
            for severity in m.SEVERITIES
        },
        "trend7d": [
            {
                "date": day,
                "total": sum(counts.values()),
                "bySeverity": counts,
            }
            for day, counts in trend.items()
        ],
    }


def list_ingest_keys(
    db: Session,
    *,
    q: Optional[str],
    status: str,
    source_system: Optional[str],
    page: int,
    page_size: int,
) -> dict:
    query = db.query(EventIngestKey)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            EventIngestKey.name.ilike(like),
            EventIngestKey.key_prefix.ilike(like),
            EventIngestKey.allowed_source_system.ilike(like),
        ))
    if source_system and source_system.strip():
        query = query.filter(
            EventIngestKey.allowed_source_system.ilike(
                f"%{source_system.strip()}%",
            )
        )
    if status == "active":
        query = query.filter(
            EventIngestKey.enabled.is_(True),
            EventIngestKey.revoked_at.is_(None),
        )
    elif status == "revoked":
        query = query.filter(or_(
            EventIngestKey.enabled.is_(False),
            EventIngestKey.revoked_at.is_not(None),
        ))

    total = query.count()
    rows = (
        query.order_by(
            EventIngestKey.created_at.desc(),
            EventIngestKey.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [service.key_out(row) for row in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def revoke_ingest_key(db: Session, key_id: str) -> dict:
    row = (
        db.query(EventIngestKey)
        .filter(EventIngestKey.id == key_id)
        .first()
    )
    if not row:
        raise HTTPException(404, "密钥不存在")
    service.revoke_ingest_key(db, row)
    return service.key_out(row)


def event_detail(db: Session, event_id: str) -> dict:
    event = require_event(db, event_id)
    attachments = (
        db.query(EventAttachment)
        .filter(EventAttachment.event_id == event.id)
        .order_by(EventAttachment.created_at.asc())
        .all()
    )
    audit = (
        db.query(EventAuditLog)
        .filter(EventAuditLog.event_id == event.id)
        .order_by(EventAuditLog.seq.asc())
        .all()
    )
    return service.event_out(event, attachments=attachments, audit=audit)
