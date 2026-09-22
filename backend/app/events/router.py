"""
事件登记 API

平台侧（JWT） router          挂 /api/v2/events
  GET    /                      列表（筛选 + 分页）
  POST   /                      平台录入
  GET    /stats/summary         概览计数
  GET    /export                按筛选条件导出 CSV（审计留存）
  GET    /ingest-keys           密钥列表（admin）
  POST   /ingest-keys           创建密钥（admin，明文仅此一次返回）
  DELETE /ingest-keys/{id}      吊销密钥（admin）
  GET    /{id}                  详情（含附件 + 审计轨迹）
  PATCH  /{id}                  编辑
  POST   /{id}/status           改状态（active/archived）
  DELETE /{id}                  软删除→归档（?hard=true 仅 admin 物理删）
  POST   /{id}/attachments      上传附件
  GET    /{id}/attachments/download-all
  GET    /{id}/attachments/{aid}/download
  DELETE /{id}/attachments/{aid}

第三方侧（X-API-Key） ingest_router   挂 /api/v2/ingest
  GET    /whoami                自测密钥
  POST   /events                单条或批量上传（幂等）
  POST   /events/{id}/attachments  给自己上传的事件补附件

治理不变式：所有变更都经 service 层并写审计；平台与第三方来源在 source_type
与 sourceLabel 上清晰区分。
"""
from __future__ import annotations

import csv
import io
import tempfile
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.deps import get_db, get_current_user, require_admin
from app.events import (
    attachment_service,
    ingest_service,
    models as m,
    query_service,
    service,
)
from app.events.deps import get_ingest_key, IngestContext
from app.events.schemas import (
    EventCreate,
    EventUpdate,
    IngestKeyCreate,
    StatusChange,
)

router = APIRouter()
ingest_router = APIRouter()
SHANGHAI_TZ = query_service.SHANGHAI_TZ


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


_as_utc = query_service.as_utc
_shanghai_day_start_utc = query_service.shanghai_day_start_utc
_shanghai_date = query_service.shanghai_date


def _ok(data):
    return {"data": data}


_require_event = query_service.require_event
_remove_temporary_archive = attachment_service.remove_temporary_archive
_archive_name = attachment_service.archive_name


# ══════════════════ 平台侧（JWT）══════════════════

# —— 静态路由须在 /{event_id} 之前声明，避免被动态段吞掉 ——

@router.get("")
def list_events(
    q: Optional[str] = Query(None, description="标题/描述/编号/上报人模糊搜索"),
    source_type: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="active|archived|all；缺省仅 active"),
    ontology_id: Optional[str] = Query(None),
    start: Optional[datetime] = Query(None, description="recorded_at 下界"),
    end: Optional[datetime] = Query(None, description="recorded_at 上界"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db), _=Depends(get_current_user),
):
    return _ok(query_service.list_events(
        db,
        q=q,
        source_type=source_type,
        event_type=event_type,
        severity=severity,
        status=status,
        ontology_id=ontology_id,
        start=start,
        end=end,
        page=page,
        page_size=page_size,
    ))


@router.post("", status_code=201)
def create_event(body: EventCreate, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    ev = service.create_event(db, body, user)
    return _ok(service.event_out(ev, attachment_count=0))


@router.get("/stats/summary")
def stats_summary(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _ok(query_service.stats_summary(db, now_utc=_now_utc()))


# —— 导出（审计留存）——

_SEVERITY_LABELS = {
    "critical": "严重", "high": "高级", "medium": "中级",
    "low": "低级", "info": "信息",
}
_STATUS_LABELS = {m.STATUS_ACTIVE: "活跃", m.STATUS_ARCHIVED: "归档"}
_CSV_HEADERS = [
    "事件编号", "标题", "事件类型", "级别", "状态",
    "来源", "上报人", "发生时间", "登记时间", "描述",
]


def _shanghai_text(value: Optional[datetime]) -> str:
    """数据库 UTC 时间 → 上海本地可读文本，空值为空串。"""
    if not value:
        return ""
    return _as_utc(value).astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _csv_safe(value) -> str:
    """防公式注入：Excel 会把以 = + - @ 开头的单元格当公式执行。"""
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@") else text


@router.get("/export")
def export_events(
    q: Optional[str] = Query(None, description="标题/描述/编号/上报人模糊搜索"),
    source_type: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="active|archived|all；缺省仅 active"),
    ontology_id: Optional[str] = Query(None),
    start: Optional[datetime] = Query(None, description="recorded_at 下界"),
    end: Optional[datetime] = Query(None, description="recorded_at 上界"),
    db: Session = Depends(get_db), _=Depends(get_current_user),
):
    """按列表同款筛选条件导出事件 CSV（UTF-8 带 BOM，Excel 可直接打开）。"""
    rows, truncated = query_service.export_rows(
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
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADERS)
    for ev in rows:
        writer.writerow([
            _csv_safe(ev.event_no),
            _csv_safe(ev.title),
            _csv_safe(ev.event_type or ""),
            _SEVERITY_LABELS.get(ev.severity, ev.severity or ""),
            _STATUS_LABELS.get(ev.status, ev.status or ""),
            _csv_safe(service.source_label(ev)),
            _csv_safe(ev.reporter_name or ""),
            _shanghai_text(ev.occurred_at),
            _shanghai_text(ev.recorded_at),
            _csv_safe(ev.description or ""),
        ])
    if truncated:
        writer.writerow([""] * (len(_CSV_HEADERS) - 1) + [
            f"注意：已达单次导出上限 {query_service.EXPORT_MAX_ROWS} 条，"
            "结果被截断，请缩小筛选范围后重试",
        ])
    filename = f"events-export-{_now_utc().astimezone(SHANGHAI_TZ):%Y%m%d-%H%M}.csv"
    # BOM 让 Excel 按 UTF-8 识别中文表头
    content = b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
    )


# —— 密钥管理（admin）——

@router.get("/ingest-keys")
def list_keys(
    q: Optional[str] = Query(None, description="名称、密钥前缀或来源系统模糊搜索"),
    status: str = Query("all", pattern="^(all|active|revoked)$"),
    source_system: Optional[str] = Query(None, description="限定来源系统模糊筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=100),
    db: Session = Depends(get_db), _=Depends(require_admin),
):
    return _ok(query_service.list_ingest_keys(
        db,
        q=q,
        status=status,
        source_system=source_system,
        page=page,
        page_size=page_size,
    ))


@router.post("/ingest-keys", status_code=201)
def create_key(body: IngestKeyCreate, db: Session = Depends(get_db),
               user=Depends(require_admin)):
    row, plaintext = service.mint_ingest_key(db, body.name, body.allowed_source_system, user)
    return _ok(service.key_out(row, plaintext=plaintext))


@router.delete("/ingest-keys/{key_id}")
def revoke_key(key_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    return _ok(query_service.revoke_ingest_key(db, key_id))


# —— 单条事件 ——

@router.get("/{event_id}")
def get_event(event_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _ok(query_service.event_detail(db, event_id))


@router.patch("/{event_id}")
def update_event(event_id: str, body: EventUpdate, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    ev = _require_event(db, event_id)
    ev = service.update_event(db, ev, body, user)
    return _ok(service.event_out(ev))


@router.post("/{event_id}/status")
def change_status(event_id: str, body: StatusChange, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    ev = _require_event(db, event_id)
    ev = service.change_status(db, ev, body.status, body.note, user)
    return _ok(service.event_out(ev))


@router.delete("/{event_id}")
def delete_event(event_id: str, hard: bool = Query(False),
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    ev = _require_event(db, event_id)
    if hard:
        if getattr(user, "role", "") != "admin":
            raise HTTPException(403, "物理删除仅管理员可用")
        service.hard_delete_event(db, ev)
        return _ok({"status": "deleted", "id": event_id})
    ev = service.change_status(db, ev, m.STATUS_ARCHIVED, "归档", user)
    return _ok(service.event_out(ev))


# —— 附件 ——

@router.post("/{event_id}/attachments", status_code=201)
async def upload_attachment(event_id: str, file: UploadFile = File(...),
                            db: Session = Depends(get_db), user=Depends(get_current_user)):
    ev = _require_event(db, event_id)
    att = await service.add_attachment(db, ev, upload=file, user=user)
    return _ok(service.attachment_out(att))


@router.get("/{event_id}/attachments/download-all")
def download_all_attachments(event_id: str, db: Session = Depends(get_db),
                             _=Depends(get_current_user)):
    archive = attachment_service.build_archive(
        db,
        event_id,
        named_temporary_file=tempfile.NamedTemporaryFile,
    )
    return FileResponse(
        archive.path,
        filename=archive.filename,
        media_type="application/zip",
        background=BackgroundTask(
            _remove_temporary_archive,
            archive.path,
        ),
    )


@router.get("/{event_id}/attachments/{att_id}/download")
def download_attachment(event_id: str, att_id: str, db: Session = Depends(get_db),
                        _=Depends(get_current_user)):
    att = attachment_service.attachment_for_download(db, event_id, att_id)
    return FileResponse(att.file_path, filename=att.filename,
                        media_type=att.mime_type or "application/octet-stream")


@router.delete("/{event_id}/attachments/{att_id}")
def delete_attachment(event_id: str, att_id: str, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    return _ok(attachment_service.remove_attachment(
        db,
        event_id,
        att_id,
        user,
    ))


# ══════════════════ 第三方侧（X-API-Key）══════════════════

@ingest_router.get("/whoami")
def whoami(ctx: IngestContext = Depends(get_ingest_key)):
    k = ctx.key
    return _ok({"name": k.name, "keyPrefix": k.key_prefix,
                "allowedSourceSystem": k.allowed_source_system,
                "clientIp": ctx.client_ip})


@ingest_router.post("/events")
async def ingest_events(request: Request, ctx: IngestContext = Depends(get_ingest_key),
                        db: Session = Depends(get_db)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "请求体必须是合法 JSON")
    return _ok(ingest_service.ingest_events(db, body, ctx))


@ingest_router.post("/events/{event_id}/attachments", status_code=201)
async def ingest_attachment(event_id: str, file: UploadFile = File(...),
                            ctx: IngestContext = Depends(get_ingest_key),
                            db: Session = Depends(get_db)):
    return _ok(await attachment_service.add_ingest_attachment(
        db,
        event_id,
        file,
        ctx,
    ))
