"""
消息通知 API — /api/v2/notifications（整个域仅管理员）

  GET    /                                列表（tab=all|unread|starred|archived，游标分页）
  GET    /summary                         未读/标记/归档计数（侧栏徽章数据源）
  POST   /                                手动发送消息（正式消息：入站内并触发渠道转发）
  POST   /read-all                        全部未读标为已读
  GET    /{message_id}                    详情（Markdown 正文 + 附件；查看即自动已读）
  PATCH  /{message_id}                    处置状态更新（isRead/isStarred/isArchived 部分更新）
  DELETE /{message_id}                    单条删除（连带附件，用于误投敏感消息处置）
  POST   /{message_id}/attachments        上传附件
  GET    /{message_id}/attachments/{att_id}/download  下载附件
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin
from app.notifications import channel_service, service
from app.notifications.schemas import (
    NotificationChannelCreateV2,
    NotificationChannelUpdateV2,
    NotificationCreate,
    NotificationIngestKeyCreate,
    NotificationSmtpTest,
    NotificationSmtpUpdate,
    NotificationStateUpdate,
)

router = APIRouter()


def _ok(data):
    return {"data": data}


# —— 静态路由须在 /{message_id} 之前声明，避免被动态段吞掉 ——


@router.get("")
def list_notifications(
    tab: str = "all",
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    return _ok(
        service.list_messages(
            db, user_id=admin.id, tab=tab, cursor=cursor, limit=limit
        )
    )


@router.get("/summary")
def notifications_summary(db: Session = Depends(get_db), admin=Depends(require_admin)):
    return _ok(service.notification_summary(db, user_id=admin.id))


@router.post("", status_code=201)
def create_notification(
    body: NotificationCreate,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    message = service.create_message(
        db,
        title=body.title,
        body_md=body.body,
        priority=body.priority,
        source_system="platform",
        source_type="manual",
        user=admin,
    )
    return _ok(
        service.get_message_detail(db, user_id=admin.id, message_id=message.id, mark_read=True)
    )


@router.post("/read-all")
def read_all_notifications(db: Session = Depends(get_db), admin=Depends(require_admin)):
    return _ok({"updated": service.mark_all_read(db, user_id=admin.id)})


# —— 对外投递密钥管理（admin；投递端点在 ingest_router，X-API-Key 鉴权）——


@router.get("/ingest-keys")
def list_ingest_keys(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    rows = (
        db.query(service.NotificationIngestKey)
        .order_by(service.NotificationIngestKey.created_at.desc())
        .all()
    )
    return _ok([service.ingest_key_out(row) for row in rows])


@router.post("/ingest-keys", status_code=201)
def create_ingest_key(
    body: NotificationIngestKeyCreate,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    row, plaintext = service.mint_ingest_key(db, body.name, body.allowedSourceSystem, admin)
    return _ok(service.ingest_key_out(row, plaintext=plaintext))


@router.delete("/ingest-keys/{key_id}")
def revoke_ingest_key(
    key_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    row = (
        db.query(service.NotificationIngestKey)
        .filter(service.NotificationIngestKey.id == key_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "密钥不存在")
    service.revoke_ingest_key(db, row)
    return _ok(service.ingest_key_out(row))


# —— 单条消息 ——


# —— 转发渠道管理（admin；apprise URL 加密存储、界面只回脱敏掩码）——


@router.get("/channel-templates")
def list_channel_templates(_admin=Depends(require_admin)):
    return _ok(channel_service.list_template_definitions())


@router.get("/smtp")
def get_smtp(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    return _ok(channel_service.smtp_out(db))


@router.put("/smtp")
def put_smtp(
    body: NotificationSmtpUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    return _ok(channel_service.update_smtp(
        db,
        host=body.host,
        port=body.port,
        username=body.username,
        password=body.password,
        sender=body.sender,
        use_tls=body.useTls,
    ))


@router.post("/smtp/test")
def test_smtp(
    body: NotificationSmtpTest,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    return _ok(channel_service.test_smtp(db, body.to))


@router.get("/channels")
def list_channels(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    rows = (
        db.query(channel_service.NotificationChannel)
        .order_by(channel_service.NotificationChannel.created_at.desc())
        .all()
    )
    return _ok([channel_service.templated_channel_out(db, row) for row in rows])


@router.post("/channels", status_code=201)
def create_channel(
    body: NotificationChannelCreateV2,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    row = channel_service.create_templated_channel(
        db,
        name=body.name,
        note=body.note,
        template_id=body.template,
        params=body.params,
        user=admin,
    )
    return _ok(channel_service.templated_channel_out(db, row))


@router.patch("/channels/{channel_id}")
def update_channel(
    channel_id: str,
    body: NotificationChannelUpdateV2,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    row = channel_service.require_channel(db, channel_id)
    if body.params is not None:
        row = channel_service.update_channel_params(db, row, params=body.params)
    row = channel_service.update_channel(
        db,
        row,
        name=body.name,
        apprise_url=None,
        note=body.note,
        enabled=body.enabled,
    )
    return _ok(channel_service.templated_channel_out(db, row))


@router.delete("/channels/{channel_id}")
def delete_channel(
    channel_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    row = channel_service.require_channel(db, channel_id)
    channel_service.delete_channel(db, row)
    return _ok({"deleted": channel_id})


@router.post("/channels/{channel_id}/test")
def test_channel(
    channel_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    row = channel_service.require_channel(db, channel_id)
    return _ok(channel_service.test_channel(db, row))


@router.get("/{message_id}")
def get_notification(
    message_id: str,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    return _ok(
        service.get_message_detail(db, user_id=admin.id, message_id=message_id)
    )


@router.patch("/{message_id}")
def update_notification_state(
    message_id: str,
    body: NotificationStateUpdate,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    fields = body.model_dump()
    return _ok(
        service.update_state(db, user_id=admin.id, message_id=message_id, fields=fields)
    )


@router.delete("/{message_id}")
def delete_notification(
    message_id: str,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    service.delete_message(db, message_id)
    return _ok({"deleted": message_id})


# —— 附件 ——


@router.post("/{message_id}/attachments", status_code=201)
async def upload_notification_attachment(
    message_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    message = service.require_message(db, message_id)
    att = await service.add_attachment(db, message, upload=file, user=admin)
    return _ok(
        {
            **service.attachment_out(att),
            "url": f"/api/v2/notifications/{message.id}/attachments/{att.id}/download",
        }
    )


@router.get("/{message_id}/attachments/{att_id}/download")
def download_notification_attachment(
    message_id: str,
    att_id: str,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    message = service.require_message(db, message_id)
    att = service.attachment_for_download(db, message, att_id)
    return FileResponse(
        att.file_path,
        filename=att.filename,
        media_type=att.mime_type or "application/octet-stream",
    )
