"""
工单 — service 层（创建、附件、处理轨迹与读侧查询集中于此）

权限契约：
  - 任何登录用户可提交工单、查看自己的工单（含附件下载）；
  - 管理员（role=admin）可查看与处理所有工单；进度调整必须携带非空评论。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiofiles
from fastapi import HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.tickets import models as m
from app.tickets.models import Ticket, TicketAttachment, TicketProgressLog
from app.tickets.schemas import TicketCreate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_admin(user) -> bool:
    return getattr(user, "role", "") == "admin"


# ── 工单编号（人读，防撞，与事件 EVT- 前缀同构）──────────────────

def generate_ticket_no(db: Session) -> str:
    day = _now().strftime("%Y%m%d")
    for _ in range(12):
        code = f"TK-{day}-{secrets.token_hex(3)}"  # 6 hex
        exists = db.query(Ticket.id).filter(Ticket.ticket_no == code).first()
        if not exists:
            return code
    return f"TK-{day}-{uuid.uuid4().hex[:8]}"  # 极端兜底


# ── 查找与可见性 ────────────────────────────────────────────────

def require_ticket(db: Session, ticket_id: str) -> Ticket:
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(404, "工单不存在")
    return ticket


def require_visible_ticket(db: Session, ticket_id: str, user) -> Ticket:
    """非管理员只能访问自己提交的工单；管理员可见全部。"""
    ticket = require_ticket(db, ticket_id)
    if not _is_admin(user) and ticket.submitter_id != getattr(user, "id", None):
        raise HTTPException(403, "无权访问他人工单")
    return ticket


def _scoped_query(db: Session, user):
    query = db.query(Ticket)
    if not _is_admin(user):
        query = query.filter(Ticket.submitter_id == getattr(user, "id", None))
    return query


# ── 写侧：创建 / 处理 ──────────────────────────────────────────

def create_ticket(db: Session, data: TicketCreate, user) -> Ticket:
    title = (data.title or "").strip()
    content = (data.content or "").strip()
    if not title:
        raise HTTPException(422, "title 不能为空")
    if not content:
        raise HTTPException(422, "content 不能为空")
    page_url = (data.page_url or "").strip() or None
    if page_url:
        page_url = page_url[:1000]  # 列宽兜底，正常页面地址远短于此
    ticket = Ticket(
        ticket_no=generate_ticket_no(db),
        title=title,
        content=content,
        category=_norm_category(data.category),
        page_url=page_url,
        submitter_id=getattr(user, "id", None),
        submitter_name=getattr(user, "username", None),
        status=m.STATUS_PENDING,  # 用户提交后自动进入「待处理」
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def _norm_category(value: Optional[str]) -> str:
    """缺省 other；显式给出时必须命中分类词汇，防脏数据入库。"""
    category = (value or "").strip()
    if not category:
        return m.CATEGORY_OTHER
    if category not in m.TICKET_CATEGORIES:
        raise HTTPException(422, f"非法分类: {category}")
    return category


def apply_progress(db: Session, ticket: Ticket, status: str,
                   comment: str, user) -> Ticket:
    """管理员处理工单：状态迁移 + 必填评论，落一行处理轨迹。"""
    status = (status or "").strip()
    if status not in m.TICKET_STATUSES:
        raise HTTPException(422, f"非法状态: {status}")
    comment = (comment or "").strip()
    if not comment:
        raise HTTPException(422, "处理评论不能为空")
    from_status = ticket.status
    ticket.status = status
    ticket.updated_at = _now()
    max_seq = (db.query(func.max(TicketProgressLog.seq))
               .filter(TicketProgressLog.ticket_id == ticket.id).scalar()) or 0
    db.add(TicketProgressLog(
        ticket_id=ticket.id, seq=max_seq + 1,
        from_status=from_status, to_status=status, comment=comment,
        actor_id=getattr(user, "id", None),
        actor_name=getattr(user, "username", None),
    ))
    db.commit()
    db.refresh(ticket)
    return ticket


# ── 附件（安全落盘 + sha256，与事件附件同构）─────────────────────

async def add_attachment(db: Session, ticket: Ticket, *, upload: UploadFile,
                         user) -> TicketAttachment:
    filename = upload.filename or ""
    mime = upload.content_type
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    configured_extensions = settings.ticket_attachment_extensions
    allowed = {e.strip().lower() for e in configured_extensions.split(",") if e.strip()}
    if ext and allowed and "*" not in allowed and ext not in allowed:
        raise HTTPException(400, f"不支持的工单附件类型: .{ext}（允许: {configured_extensions}）")

    att_id = str(uuid.uuid4())
    upload_dir = os.path.join(settings.uploads_dir, "tickets", ticket.id)
    os.makedirs(upload_dir, exist_ok=True)
    ext_suffix = os.path.splitext(filename or "")[1]
    save_path = os.path.join(upload_dir, f"{att_id}{ext_suffix}")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    file_size = 0
    digest = hashlib.sha256()
    try:
        async with aiofiles.open(save_path, "wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                file_size += len(chunk)
                if file_size > max_bytes:
                    raise HTTPException(413, f"文件超过大小限制 {settings.max_upload_mb}MB")
                digest.update(chunk)
                await destination.write(chunk)
    except Exception:
        _remove_file(save_path)
        raise

    att = TicketAttachment(
        id=att_id, ticket_id=ticket.id, filename=filename or att_id,
        file_path=save_path, file_size=file_size, mime_type=mime,
        sha256=digest.hexdigest(),
        uploaded_by=getattr(user, "id", None),
    )
    try:
        db.add(att)
        db.commit()
        db.refresh(att)
    except Exception:
        db.rollback()
        _remove_file(save_path)
        raise
    return att


def attachment_for_download(db: Session, ticket: Ticket, att_id: str) -> TicketAttachment:
    att = (db.query(TicketAttachment)
           .filter(TicketAttachment.id == att_id,
                   TicketAttachment.ticket_id == ticket.id)
           .first())
    if not att:
        raise HTTPException(404, "附件不存在")
    return att


def _remove_file(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ── 读侧：列表 / 详情 / 统计 ────────────────────────────────────

def list_tickets(
    db: Session, *, user, q: Optional[str], status: Optional[str],
    page: int, page_size: int,
) -> dict:
    query = _scoped_query(db, user)
    if status and status != "all":
        # 支持逗号分隔多状态（顶栏弹窗用「处理中 = pending,analyzing」）
        statuses = [part.strip() for part in status.split(",") if part.strip()]
        invalid = [part for part in statuses if part not in m.TICKET_STATUSES]
        if invalid:
            raise HTTPException(422, f"非法状态筛选: {','.join(invalid)}")
        if statuses:
            query = query.filter(Ticket.status.in_(statuses))
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            Ticket.title.ilike(like)
            | Ticket.content.ilike(like)
            | Ticket.ticket_no.ilike(like)
            | Ticket.submitter_name.ilike(like)
        )

    total = query.count()
    rows = (
        query.order_by(Ticket.created_at.desc(), Ticket.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    ticket_ids = [row.id for row in rows]
    attachment_counts: dict[str, int] = {}
    if ticket_ids:
        count_rows = (
            db.query(TicketAttachment.ticket_id, func.count(TicketAttachment.id))
            .filter(TicketAttachment.ticket_id.in_(ticket_ids))
            .group_by(TicketAttachment.ticket_id)
            .all()
        )
        attachment_counts.update(count_rows)
    return {
        "items": [
            ticket_out(row, attachment_count=attachment_counts.get(row.id, 0))
            for row in rows
        ],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def ticket_detail(db: Session, ticket: Ticket) -> dict:
    attachments = (
        db.query(TicketAttachment)
        .filter(TicketAttachment.ticket_id == ticket.id)
        .order_by(TicketAttachment.created_at.asc())
        .all()
    )
    logs = (
        db.query(TicketProgressLog)
        .filter(TicketProgressLog.ticket_id == ticket.id)
        .order_by(TicketProgressLog.seq.asc())
        .all()
    )
    return ticket_out(ticket, attachments=attachments, progress_logs=logs)


def stats_summary(db: Session, *, user) -> dict:
    """按状态计数；非管理员只统计自己的工单。"""
    query = db.query(
        Ticket.status, func.count(Ticket.id)
    )
    if not _is_admin(user):
        query = query.filter(Ticket.submitter_id == getattr(user, "id", None))
    rows = query.group_by(Ticket.status).all()
    by_status = {status: 0 for status in m.TICKET_STATUSES}
    for status, count in rows:
        by_status[status] = count
    return {
        "total": sum(by_status.values()),
        "byStatus": by_status,
    }


# ── 序列化助手（对外 camelCase）────────────────────────────────

def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def ticket_out(t: Ticket, *, attachment_count: Optional[int] = None,
               attachments: Optional[list] = None,
               progress_logs: Optional[list] = None) -> dict:
    out = {
        "id": t.id, "ticketNo": t.ticket_no, "title": t.title,
        "content": t.content, "status": t.status,
        "category": t.category, "pageUrl": t.page_url,
        "submitterId": t.submitter_id, "submitterName": t.submitter_name,
        "createdAt": _iso(t.created_at), "updatedAt": _iso(t.updated_at),
    }
    if attachment_count is not None:
        out["attachmentCount"] = attachment_count
    if attachments is not None:
        out["attachments"] = [attachment_out(a) for a in attachments]
    if progress_logs is not None:
        out["progressLogs"] = [progress_log_out(entry) for entry in progress_logs]
    return out


def attachment_out(a: TicketAttachment) -> dict:
    return {
        "id": a.id, "ticketId": a.ticket_id, "filename": a.filename,
        "fileSize": a.file_size, "mimeType": a.mime_type, "sha256": a.sha256,
        "uploadedBy": a.uploaded_by, "createdAt": _iso(a.created_at),
    }


def progress_log_out(entry: TicketProgressLog) -> dict:
    return {
        "id": entry.id, "seq": entry.seq,
        "fromStatus": entry.from_status, "toStatus": entry.to_status,
        "comment": entry.comment,
        "actorId": entry.actor_id, "actorName": entry.actor_name,
        "createdAt": _iso(entry.created_at),
    }
