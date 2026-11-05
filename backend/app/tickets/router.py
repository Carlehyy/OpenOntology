"""
工单 API — /api/v2/tickets（全部 JWT 鉴权，与收件箱同级的个人反馈入口）

  GET    /                       列表（管理员见全部；其他用户仅见自己提交的）
  POST   /                       提交工单（自动进入「待处理」）
  GET    /stats/summary          按状态计数（与列表同 scope）
  GET    /{id}                   详情（附件 + 处理轨迹）
  POST   /{id}/progress          处理工单（仅管理员；状态 + 必填评论）
  POST   /{id}/attachments       上传附件（提交人或管理员）
  GET    /{id}/attachments/{aid}/download  下载附件（提交人或管理员）

工单是全角色可用的反馈通道，不挂菜单权限（menu_guard），仅要求登录。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_admin
from app.tickets import service
from app.tickets.schemas import TicketCreate, ProgressUpdate

router = APIRouter()


def _ok(data):
    return {"data": data}


# —— 静态路由须在 /{ticket_id} 之前声明，避免被动态段吞掉 ——

@router.get("")
def list_tickets(
    q: Optional[str] = Query(None, description="标题/内容/编号/提交人模糊搜索"),
    status: Optional[str] = Query(None, description="pending|analyzing|completed|cancelled|all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    return _ok(service.list_tickets(
        db, user=user, q=q, status=status, page=page, page_size=page_size,
    ))


@router.post("", status_code=201)
def create_ticket(body: TicketCreate, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    ticket = service.create_ticket(db, body, user)
    return _ok(service.ticket_out(ticket, attachment_count=0))


@router.get("/stats/summary")
def stats_summary(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _ok(service.stats_summary(db, user=user))


# —— 单条工单 ——

@router.get("/{ticket_id}")
def get_ticket(ticket_id: str, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    ticket = service.require_visible_ticket(db, ticket_id, user)
    return _ok(service.ticket_detail(db, ticket))


@router.post("/{ticket_id}/progress")
def update_progress(ticket_id: str, body: ProgressUpdate,
                    db: Session = Depends(get_db), user=Depends(require_admin)):
    ticket = service.require_ticket(db, ticket_id)
    ticket = service.apply_progress(db, ticket, body.status, body.comment, user)
    return _ok(service.ticket_detail(db, ticket))


# —— 附件 ——

@router.post("/{ticket_id}/attachments", status_code=201)
async def upload_attachment(ticket_id: str, file: UploadFile = File(...),
                            db: Session = Depends(get_db),
                            user=Depends(get_current_user)):
    ticket = service.require_visible_ticket(db, ticket_id, user)
    att = await service.add_attachment(db, ticket, upload=file, user=user)
    return _ok(service.attachment_out(att))


@router.get("/{ticket_id}/attachments/{att_id}/download")
def download_attachment(ticket_id: str, att_id: str, db: Session = Depends(get_db),
                        user=Depends(get_current_user)):
    ticket = service.require_visible_ticket(db, ticket_id, user)
    att = service.attachment_for_download(db, ticket, att_id)
    return FileResponse(att.file_path, filename=att.filename,
                        media_type=att.mime_type or "application/octet-stream")
