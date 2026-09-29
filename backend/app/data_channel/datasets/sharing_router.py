"""Manual dataset anonymous share links and approval workflow."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.data_channel.datasets.edit_service import build_edited_snapshot
from app.data_channel.datasets.manual_contract import (
    RowEditOp,
    RowEditsRequest,
    require_manual_dataset as _require_manual_dataset,
)
from app.data_channel.datasets.sharing_models import ManualDatasetChange, ManualDatasetShare
from app.deps import get_current_user, get_db
from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.data_channel.datasets.service import DatasetService, rows_to_csv_bytes
from app.shared.encryption import decrypt, encrypt


management_router = APIRouter(dependencies=[Depends(get_current_user)])
public_router = APIRouter()


class CreateShareRequest(BaseModel):
    permission: str = "view"
    label: str = ""
    expires_in_days: int | None = 30


class PublicChangeRequest(BaseModel):
    base_version_no: int
    updates: list[RowEditOp] = Field(default_factory=list)
    inserts: list[RowEditOp] = Field(default_factory=list)
    deletes: list[RowEditOp] = Field(default_factory=list)


class ReviewChangeRequest(BaseModel):
    decision: str
    comment: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _recover_token(share: ManualDatasetShare) -> str | None:
    """Recover a managed share token without weakening public token lookup."""
    if not share.token_encrypted:
        return None
    try:
        token = decrypt(share.token_encrypted)
    except Exception:
        # Legacy rows have no ciphertext; a changed key or damaged ciphertext
        # must not make the entire share-management dialog unavailable.
        return None
    return token if secrets.compare_digest(_hash_token(token), share.token_hash) else None


def _can_manage_share(share: ManualDatasetShare, user) -> bool:
    """分享管理权：admin 或分享创建者（对齐 file_assets 的属主语义）；
    created_by 为空的遗留分享仅管理员可管理。"""
    if str(getattr(user, "role", "") or "") == "admin":
        return True
    return share.created_by is not None and str(user.id) == str(share.created_by)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _active_share(db: Session, token: str) -> ManualDatasetShare:
    share = db.query(ManualDatasetShare).filter(
        ManualDatasetShare.token_hash == _hash_token(token)).first()
    if not share or share.revoked_at or (
        share.expires_at and _as_aware(share.expires_at) <= _now()
    ):
        # Deliberately use the same response for unknown, revoked and expired tokens.
        raise HTTPException(404, "分享链接不存在或已失效")
    return share


def _dataset(db: Session, dataset_id: str) -> Dataset:
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not dataset:
        raise HTTPException(404, "Dataset not found")
    _require_manual_dataset(dataset, "分享维护")
    return dataset


def _latest_no(db: Session, dataset_id: str) -> int:
    latest = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset_id,
    ).order_by(DatasetVersion.version_no.desc()).first()
    return latest.version_no if latest else 0


def _change_dict(change: ManualDatasetChange) -> dict:
    return {
        "id": change.id,
        "dataset_id": change.dataset_id,
        "base_version_no": change.base_version_no,
        "status": change.status,
        "summary": change.summary or {},
        "review_comment": change.review_comment or "",
        "reviewed_by": change.reviewed_by,
        "submitted_at": change.submitted_at.isoformat() if change.submitted_at else None,
        "reviewed_at": change.reviewed_at.isoformat() if change.reviewed_at else None,
        "applied_version_no": change.applied_version_no,
    }


@management_router.post("/{dataset_id}/shares", status_code=201)
def create_share(dataset_id: str, body: CreateShareRequest, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    dataset = _dataset(db, dataset_id)
    permission = body.permission.strip().lower()
    if permission not in {"view", "edit"}:
        raise HTTPException(400, "permission 仅支持 view 或 edit")
    if body.expires_in_days is not None and not 1 <= body.expires_in_days <= 365:
        raise HTTPException(400, "有效期须为 1 至 365 天，或留空表示长期有效")
    token = secrets.token_urlsafe(32)
    share = ManualDatasetShare(
        dataset_id=dataset.id,
        token_hash=_hash_token(token),
        token_encrypted=encrypt(token),
        permission=permission,
        label=body.label.strip()[:200],
        created_by=user.id,
        expires_at=_now() + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None else None,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return {
        "id": share.id,
        "token": token,
        "permission": share.permission,
        "dataset_name": dataset.name,
        "expires_at": share.expires_at.isoformat() if share.expires_at else None,
    }


@management_router.get("/{dataset_id}/shares")
def list_shares(dataset_id: str, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    _dataset(db, dataset_id)
    query = db.query(ManualDatasetShare).filter(
        ManualDatasetShare.dataset_id == dataset_id,
    )
    if str(getattr(user, "role", "") or "") != "admin":
        # 列表按属主过滤：普通用户只看到自己创建的分享，解不出他人 token
        query = query.filter(ManualDatasetShare.created_by == user.id)
    rows = query.order_by(ManualDatasetShare.created_at.desc()).all()
    return [{
        "id": row.id,
        "token": _recover_token(row),
        "permission": row.permission,
        "label": row.label,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]


@management_router.delete("/shares/{share_id}")
def revoke_share(share_id: str, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    share = db.query(ManualDatasetShare).filter(ManualDatasetShare.id == share_id).first()
    if not share:
        raise HTTPException(404, "分享记录不存在")
    if not _can_manage_share(share, user):
        raise HTTPException(403, "仅分享创建者或管理员可以吊销分享")
    if not share.revoked_at:
        share.revoked_at = _now()
        db.commit()
    return {"status": "revoked", "id": share.id}


@management_router.get("/changes")
def list_changes(
    dataset_id: str | None = None,
    status: str | None = None,
    search: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    if status and status not in {"pending", "approved", "rejected"}:
        raise HTTPException(400, "status 仅支持 pending、approved 或 rejected")
    query = db.query(ManualDatasetChange, Dataset, ManualDatasetShare).join(
        Dataset, Dataset.id == ManualDatasetChange.dataset_id,
    ).join(ManualDatasetShare, ManualDatasetShare.id == ManualDatasetChange.share_id)
    if dataset_id:
        query = query.filter(ManualDatasetChange.dataset_id == dataset_id)
    if status:
        query = query.filter(ManualDatasetChange.status == status)
    keyword = search.strip()
    if keyword:
        pattern = f"%{keyword}%"
        query = query.filter(or_(
            Dataset.name.ilike(pattern),
            ManualDatasetShare.label.ilike(pattern),
        ))
    total = query.count()
    rows = query.order_by(
        ManualDatasetChange.submitted_at.desc(),
        ManualDatasetChange.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size).all()
    items = [{
        **_change_dict(change),
        "dataset_name": dataset.name,
        "share_label": share.label,
        "permission": share.permission,
        "edits": change.edits,
    } for change, dataset, share in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@management_router.post("/changes/{change_id}/review")
def review_change(change_id: str, body: ReviewChangeRequest, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    decision = body.decision.strip().lower()
    if decision not in {"approve", "reject"}:
        raise HTTPException(400, "decision 仅支持 approve 或 reject")
    comment = body.comment.strip()
    if decision == "reject" and not comment:
        raise HTTPException(400, "驳回时必须填写具体原因")

    change = db.query(ManualDatasetChange).filter(
        ManualDatasetChange.id == change_id).first()
    if not change:
        raise HTTPException(404, "审批任务不存在")
    if change.status != "pending":
        raise HTTPException(409, f"该任务已是 {change.status} 状态，不能重复审批")

    if decision == "reject":
        change.status = "rejected"
        change.review_comment = comment
        change.reviewed_by = user.id
        change.reviewed_at = _now()
        db.commit()
        return _change_dict(change)

    from app.data_channel.datasets.lock import DatasetLockTimeout, dataset_write_lock
    dataset = _dataset(db, change.dataset_id)
    edits = RowEditsRequest(**(change.edits or {}))
    svc = DatasetService(db)
    try:
        with dataset_write_lock(f"dataset::{dataset.id}", bind=db.get_bind(), wait_timeout=30):
            current_no = _latest_no(db, dataset.id)
            if current_no != change.base_version_no:
                raise HTTPException(409, detail={
                    "code": "manual_share_base_version_conflict",
                    "message": (
                        f"该数据集已更新到 v{current_no}，外部修改基于 v{change.base_version_no}。"
                        "为避免覆盖新数据，请驳回并让维护者刷新后重新提交。"
                    ),
                    "current_version_no": current_no,
                })
            new_rows, columns, schema = build_edited_snapshot(db, svc, dataset, edits)
            version = svc.create_version(
                dataset.id, rows_to_csv_bytes(new_rows, columns), rowcount=len(new_rows),
                schema_json=schema if new_rows else None,
                _lock_held=True, _defer_commit=True)
            # 版本发布与审批落账必须同一次提交：create_version 只 flush 不
            # commit。若分两次提交，第一次成功后失败会留下「新版本已发布、
            # 变更仍 pending」，且重试必撞 base_version 409，审批永久卡死。
            change.status = "approved"
            change.review_comment = comment
            change.reviewed_by = user.id
            change.reviewed_at = _now()
            change.applied_version_no = version.version_no
            db.commit()
            # 旧版本清理与总览缓存失效放在提交成功之后；失败不影响审批结果。
            svc.finalize_version_publish(dataset.id)
    except DatasetLockTimeout as exc:
        raise HTTPException(423, str(exc))
    return _change_dict(change)


@public_router.get("/{token}")
def public_dataset(token: str, limit: int = 50, offset: int = 0,
                   db: Session = Depends(get_db)):
    share = _active_share(db, token)
    dataset = _dataset(db, share.dataset_id)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    svc = DatasetService(db)
    schema = dict(dataset.schema_json or {})
    latest_version = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset.id,
    ).order_by(DatasetVersion.version_no.desc()).first()
    version_no = int(latest_version.version_no) if latest_version else 0

    # UI paging should only materialize the requested window. Historical versions
    # without rowcount must not fall back to a full-table read either: the public
    # endpoint caps limit at 500 but that cap cannot bound load_all_rows. Window
    # the stored payload directly; the total becomes a conservative "at least"
    # count (exact on the last short page), which is enough for paging. Manual
    # datasets are blob-backed, so no lake-table replay is involved here.
    if latest_version is None:
        total_rows = 0
        rows = []
    elif latest_version.rowcount is not None:
        total_rows = max(0, int(latest_version.rowcount))
        rows = svc.preview(dataset.id, latest_version.version_no, limit, offset)
    else:
        from app.data_channel.datasets.service import _parse_stored_rows
        try:
            raw = svc.load_version_bytes(dataset.id, latest_version.version_no)
            rows = _parse_stored_rows(raw or b"", limit=limit, offset=offset)
        except HTTPException:
            raise
        except Exception:
            # 读失败按 502 拒绝并说明：公开分享页不能把「读不出来」伪装成空表，
            # 也不能借全量读兜底绕过 limit 上限。
            raise HTTPException(
                502, "该分享的数据版本缺少行数元数据且内容读取失败，"
                     "请联系分享发起方重新发布版本")
        total_rows = offset + len(rows) + (1 if len(rows) >= limit else 0)

    typed_columns = [
        item for item in (schema.get("columns_typed") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    columns = [str(item) for item in (schema.get("columns") or [])]
    if not columns and rows:
        columns = list(rows[0].keys())
    if not columns and total_rows:
        first_row = svc.preview(dataset.id, latest_version.version_no, 1, 0) if latest_version else []
        columns = list(first_row[0].keys()) if first_row else []
    field_names = schema.get("field_names") if isinstance(schema.get("field_names"), dict) else {}
    primary_key = str(schema.get("primary_key") or "")
    pk_columns = {name.strip() for name in primary_key.split(",") if name.strip()}
    types = {
        str(item.get("name")): str(item.get("type") or "string")
        for item in typed_columns
    }
    column_meta = {
        str(item.get("name")): {
            "display_name": str(
                item.get("display_name")
                or field_names.get(str(item.get("name")))
                or item.get("name")
            ),
            # A declared primary key is non-null by contract even when legacy
            # columns_typed metadata still carries nullable=true.
            "nullable": (
                False if str(item.get("name")) in pk_columns
                else bool(item.get("nullable", True))
            ),
        }
        for item in typed_columns
    }

    # Review history is part of edit capability and is not exposed to view-only links.
    changes = []
    if share.permission == "edit":
        changes = db.query(ManualDatasetChange).filter(
            ManualDatasetChange.share_id == share.id,
        ).order_by(ManualDatasetChange.submitted_at.desc()).limit(20).all()
    return {
        "dataset": {
            "id": dataset.id,
            "name": dataset.name,
            "version_no": version_no,
            "total_rows": total_rows,
            "columns": columns,
            "column_types": types,
            "column_meta": column_meta,
            "primary_key": primary_key,
            "rows": rows,
        },
        "share": {
            "permission": share.permission,
            "label": share.label,
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        },
        "changes": [_change_dict(change) for change in changes],
    }


@public_router.post("/{token}/changes", status_code=201)
def submit_public_change(token: str, body: PublicChangeRequest,
                         db: Session = Depends(get_db)):
    share = _active_share(db, token)
    if share.permission != "edit":
        raise HTTPException(403, "该分享链接仅支持查看，不能提交修改")
    dataset = _dataset(db, share.dataset_id)
    if not (body.updates or body.inserts or body.deletes):
        raise HTTPException(400, "没有任何修改")
    operation_count = len(body.updates) + len(body.inserts) + len(body.deletes)
    if operation_count > 1000:
        raise HTTPException(400, "单次最多提交 1000 行修改，请分批维护")
    current_no = _latest_no(db, dataset.id)
    if body.base_version_no != current_no:
        raise HTTPException(409, detail={
            "message": f"数据已更新到 v{current_no}，请刷新页面后重新修改",
            "current_version_no": current_no,
        })
    pending = db.query(ManualDatasetChange).filter(
        ManualDatasetChange.share_id == share.id,
        ManualDatasetChange.status == "pending",
    ).first()
    if pending:
        raise HTTPException(409, detail={
            "message": "已有一份修改正在审批，请等待审批完成后再提交",
            "change_id": pending.id,
        })

    edits = RowEditsRequest(**body.model_dump())
    svc = DatasetService(db)
    new_rows, _, _ = build_edited_snapshot(db, svc, dataset, edits)
    summary = {
        "updated": len(body.updates),
        "inserted": len(body.inserts),
        "deleted": len(body.deletes),
        "result_rows": len(new_rows),
    }
    change = ManualDatasetChange(
        share_id=share.id,
        dataset_id=dataset.id,
        base_version_no=body.base_version_no,
        edits=body.model_dump(),
        summary=summary,
        status="pending",
    )
    db.add(change)
    db.commit()
    db.refresh(change)
    return _change_dict(change)
