"""v2 Curated Dataset API — reads from v2_datasets kind=curated"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.data_channel.curated import (
    catalog_service,
    lifecycle_service,
    read_service,
    review_query_service,
)
from app.data_channel.curated.contracts import CuratedDatasetResponse
from app.deps import get_current_user, require_admin
from app.data_channel.curated.models import CuratedDataset, CuratedReview

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


def _dispatch_approved_review(db: Session, review_id: str) -> dict:
    """返回与审核同事务落库的可靠自动化交接状态。

    实际 Mapping 由 DatasetVersion outbox 调度器执行；不能在审批事务提交后
    再依赖一次无持久凭据的 ``Celery.delay``，否则用户可能只看到“已审核”，
    却没有任何可恢复的下游任务。
    """
    try:
        from app.data_channel.datasets.models import DatasetVersionEvent
        review = db.query(CuratedReview).filter(
            CuratedReview.id == review_id).first()
        if review is None or not review.dataset_version_id:
            raise RuntimeError("审核未绑定可自动化的数据版本")
        event = db.query(DatasetVersionEvent).filter(
            DatasetVersionEvent.dataset_version_id == review.dataset_version_id,
            DatasetVersionEvent.event_type == "curated_review_approved",
        ).first()
        if event is None:
            raise RuntimeError("审核自动化 outbox 事件未创建")
        return {
            "status": "success" if event.status == "completed" else "queued",
            "event_id": event.id,
            "event_status": event.status,
            "durable": True,
        }
    except Exception as exc:  # 批准已落库，自动灌入失败必须可见、不可静默伪成功
        logger.exception(
            "Durable Mapping hand-off failed after review approve %s",
            review_id,
        )
        return {
            "status": "failed",
            "error": f"审核已批准，但自动灌入本体失败：{str(exc)[:500]}",
        }


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("")
def list_curated(
    db: Session = Depends(get_db),
    pipeline: str = "",
    task_id: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    paginated: bool = False,
):
    """列出成品数据集；分页模式固定按最近更新时间倒序。"""
    return catalog_service.list_curated(
        db,
        pipeline=pipeline,
        task_id=task_id,
        status=status,
        page=page,
        page_size=page_size,
        paginated=paginated,
    )


@router.delete("/{dataset_id}", status_code=204)
def delete_curated(dataset_id: str, force: bool = False,
                   db: Session = Depends(get_db), admin=Depends(require_admin)):
    """完整删除 Curated Dataset 及其审核、版本数据（仅管理员）。

    安全约束：
    - 删除数据集时同步删除其审核记录和行级修改；
    - 被流水线 / 本体映射引用时始终拦截；force 已禁用，避免外键层实际失败却
      向用户承诺“强删成功”。
    """
    return lifecycle_service.delete_curated(
        db,
        dataset_id,
        force=force,
        admin_id=str(admin.id),
    )


@router.get("/{dataset_id}", response_model=CuratedDatasetResponse)
def get_curated(dataset_id: str, db: Session = Depends(get_db)):
    return catalog_service.get_curated(db, dataset_id)


@router.get("/{dataset_id}/preview")
def preview_curated(
    dataset_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """生产预览 — 仅读取当前已批准版本，并支持 offset/limit 分页。"""
    return read_service.preview_curated(
        db,
        dataset_id,
        limit=limit,
        offset=offset,
    )


@router.get("/{dataset_id}/export")
def export_curated(
    dataset_id: str,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    db: Session = Depends(get_db),
):
    """导出当前已批准成品版本全量数据，并叠加该版本已批准的行级修改。"""
    return read_service.export_curated(
        db,
        dataset_id,
        output_format=format,
    )


@router.get("/{dataset_id}/review-diff")
def review_diff(
    dataset_id: str,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    review_id: str | None = None,
                db: Session = Depends(get_db)):
    """审批三视角：① 变化量（相对上一版）② 上一版全量 ③ 本次全量（叠加审核编辑）。

    变化量 = compute_lake_impact(上一版全量, 本次全量)，按主键识别新增/更新/删除；
    无主键则退化为整行比对（只有新增/删除）。无上一版（v1 首次）→ ① 全为新增、② 空。
    审核者据此聚焦本次改动，而非盲审全量。
    """
    return review_query_service.build_review_diff(
        db,
        dataset_id,
        limit=limit,
        offset=offset,
        review_id=review_id,
    )


@router.get("/{dataset_id}/quality")
def get_quality_report(dataset_id: str, db: Session = Depends(get_db)):
    """获取质量报告（支持旧 curated 表和新 v2_datasets curated）"""
    from app.data_channel.curated.quality_service import QualityService
    from app.data_channel.datasets.service import DatasetService

    from app.data_channel.datasets.models import Dataset as Ds2
    ds = db.query(Ds2).filter(Ds2.id == dataset_id, Ds2.kind == "curated").first()
    sample_data = []
    if ds:
        # 读 canonical 最新版本（叠加已批准编辑），质量分不能被 legacy mirror
        # 中陈旧的 sample_rows 覆盖。
        try:
            from app.data_channel.curated.approved_version_reader import (
                load_rows_with_edits,
            )
            sample_data = load_rows_with_edits(db, dataset_id, limit=200)
        except Exception as e:
            logger.exception("质量评估读取数据失败 dataset=%s", dataset_id)
            raise HTTPException(502, f"质量评估读取数据失败：{e}")
    else:
        legacy = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
        if not legacy:
            raise HTTPException(404, "Curated dataset not found")
        if isinstance(legacy.schema_json, dict):
            sample_data = legacy.schema_json.get("sample_rows", [])

    svc = QualityService(db)
    report = svc.compute_report(dataset_id, sample_data)
    return report.to_dict()


@router.post("/{dataset_id}/review")
def submit_review(
    dataset_id: str,
    action: str,  # "approve" | "reject"
    notes: str = "",
    db: Session = Depends(get_db),
    admin=Depends(require_admin),  # PRD Security Logic: only admin can approve curated rows
):
    """提交审核结果（approve/reject）"""
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action 仅支持 approve 或 reject")
    # 兼容旧的一步审批端点，但内部仍必须先创建“绑定当前版本”的审核，随后
    # 再走统一的版本漂移检查，不能另造一条无 dataset_version_id 的旁路。
    # 若用户刚在详情页保存了行编辑，必须决定那一条 pending 审核；另开一条
    # 空审核会造成按钮显示“已批准”但人工修改永远没有被批准。
    from app.data_channel.curated.review_service import ReviewService
    svc = ReviewService(db)
    review = svc.start_review(dataset_id, reviewer_id=str(admin.id))
    if action == "approve":
        review = svc.approve(review.id, notes, reviewer_id=str(admin.id))
    else:
        review = svc.reject(review.id, notes, reviewer_id=str(admin.id))

    dispatch = _dispatch_approved_review(db, review.id) if action == "approve" else None
    return {"review_id": review.id, "status": review.status,
            "mapping_dispatch": dispatch}


# ── 审核工作流端点 ─────────────────────────────────────────────────────

class BatchEditRequest(BaseModel):
    edits: list[dict]  # [{row_pk, field_name, old_value, new_value}]


@router.post("/{dataset_id}/reviews")
def start_review(dataset_id: str, db: Session = Depends(get_db)):
    """为数据集启动审核流程"""
    from app.data_channel.curated.review_service import ReviewService
    svc = ReviewService(db)
    review = svc.start_review(dataset_id)
    return {"review_id": review.id, "status": review.status,
            "dataset_version_id": review.dataset_version_id}


@router.get("/reviews/{review_id}")
def get_review(review_id: str, db: Session = Depends(get_db)):
    """获取审核记录详情"""
    from app.data_channel.curated.models import CuratedReview
    review = db.query(CuratedReview).filter(CuratedReview.id == review_id).first()
    if not review:
        raise HTTPException(404, "Review not found")
    return {
        "id": review.id,
        "curated_dataset_id": review.curated_dataset_id,
        "dataset_version_id": review.dataset_version_id,
        "status": review.status,
        "reviewer_id": review.reviewer_id,
        "notes": review.notes,
        "decided_at": review.decided_at,
    }


@router.post("/reviews/{review_id}/edits")
def add_edit(review_id: str, body: BatchEditRequest, db: Session = Depends(get_db)):
    """批量提交行编辑"""
    from app.data_channel.curated.review_service import ReviewService
    svc = ReviewService(db)
    edits = svc.batch_edit_rows(review_id, body.edits)
    return {"saved": len(edits)}


@router.post("/reviews/{review_id}/approve")
def approve_review(review_id: str, notes: str = "", db: Session = Depends(get_db),
                   admin=Depends(require_admin)):
    """审核通过"""
    from app.data_channel.curated.review_service import ReviewService
    svc = ReviewService(db)
    review = svc.approve(review_id, notes, reviewer_id=str(admin.id))
    return {
        "review_id": review.id,
        "status": review.status,
        "mapping_dispatch": _dispatch_approved_review(db, review.id),
    }


@router.post("/reviews/{review_id}/reject")
def reject_review(review_id: str, notes: str = "", db: Session = Depends(get_db),
                  admin=Depends(require_admin)):
    """审核拒绝"""
    from app.data_channel.curated.review_service import ReviewService
    svc = ReviewService(db)
    review = svc.reject(review_id, notes, reviewer_id=str(admin.id))
    return {"review_id": review.id, "status": review.status}
