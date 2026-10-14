"""
消息通知对外投递 API — /api/v2/notifications/ingest（X-API-Key 鉴权，无 JWT）

外部系统凭密钥把消息投进平台消息总线：

  POST /                                JSON：{title, body, priority?, sourceSystem?, eventId?}
  POST /{message_id}/attachments        multipart 文件附件（仅 ingest 消息可追加）

密钥的签发与吊销由管理员在 /api/v2/notifications/ingest-keys（JWT）完成。
与 events 域的 ingest 同构：sha256 查表鉴权、明文仅创建时返回一次。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.deps import get_db
from app.notifications import service
from app.notifications.models import NotificationIngestKey

# auto_error=False：缺头时返回 None，由依赖统一 401 "Invalid API key"
# （不用 "Not authenticated"，避免前端 axios 拦截器误判跳登录）
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass
class NotificationIngestContext:
    key: NotificationIngestKey
    client_ip: Optional[str]


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def get_notification_ingest_key(
    request: Request,
    api_key: Optional[str] = Depends(api_key_header),
    db: Session = Depends(get_db),
) -> NotificationIngestContext:
    key = service.verify_ingest_key(db, (api_key or "").strip())
    if key is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    key.last_used_at = service._now()
    db.commit()
    return NotificationIngestContext(key=key, client_ip=_client_ip(request))


ingest_router = APIRouter()


def _ok(data):
    return {"data": data}


@ingest_router.post("")
async def ingest_notification(
    request: Request,
    ctx: NotificationIngestContext = Depends(get_notification_ingest_key),
    db: Session = Depends(get_db),
):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(422, "请求体必须是合法 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(422, "请求体必须是 JSON 对象")
    return _ok(service.ingest_message(db, body, ctx.key))


@ingest_router.post("/{message_id}/attachments", status_code=201)
async def ingest_notification_attachment(
    message_id: str,
    file: UploadFile = File(...),
    ctx: NotificationIngestContext = Depends(get_notification_ingest_key),
    db: Session = Depends(get_db),
):
    message = service.require_ingest_message(db, message_id, ctx.key)
    # 附件来源以密钥前缀留痕（无平台用户身份），随附件行同事务一次提交
    att = await service.add_attachment(
        db, message, upload=file, user=None, uploaded_by=ctx.key.key_prefix
    )
    return _ok(
        {
            **service.attachment_out(att),
            "url": f"/api/v2/notifications/{message.id}/attachments/{att.id}/download",
        }
    )
