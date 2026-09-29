"""Lifecycle operations for curated datasets."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.datasets.consumers import dataset_consumers
from app.data_channel.datasets.service import (
    drain_storage_deletion_outbox,
    enqueue_dataset_storage_deletions,
)
from app.data_channel.file_assets.models import PipelineFileAsset
from app.data_channel.curated.models import CuratedReview, CuratedRowEdit
from app.data_channel.datasets.models import Dataset, DatasetVersion, MediaItem
from app.ontologies.mappings.consumers import dataset_mapping_bindings

logger = logging.getLogger(__name__)


def delete_curated(
    db: Session,
    dataset_id: str,
    *,
    force: bool,
    admin_id: str | None = None,
) -> None:
    """Delete one unreferenced curated dataset and all owned evidence/data."""
    dataset = (
        db.query(Dataset)
        .filter(Dataset.id == dataset_id, Dataset.kind == "curated")
        .first()
    )
    if not dataset:
        raise HTTPException(404, "Curated dataset not found")

    if force:
        raise HTTPException(
            400,
            "force 强制删除已禁用；请先解除流水线和本体映射依赖",
        )

    pipelines = dataset_consumers(db, dataset_id)
    mappings = dataset_mapping_bindings(db, dataset_id)
    if pipelines or mappings:
        raise HTTPException(
            409,
            detail={
                "code": "in_use",
                "message": (
                    f"该数据集被 {len(pipelines)} 条流水线、"
                    f"{len(mappings)} 个本体映射引用，请先解除依赖后再删除。"
                ),
                "pipelines": pipelines,
                "mappings": mappings,
            },
        )

    # Storage deletion intents are committed in the same transaction as the
    # database deletion.  Physical deletion then drains the durable outbox.
    enqueue_dataset_storage_deletions(db, dataset_id)
    review_ids = [
        review.id
        for review in (
            db.query(CuratedReview.id)
            .filter(CuratedReview.curated_dataset_id == dataset_id)
            .all()
        )
    ]
    if review_ids:
        (
            db.query(CuratedRowEdit)
            .filter(CuratedRowEdit.review_id.in_(review_ids))
            .delete(synchronize_session=False)
        )
        (
            db.query(CuratedReview)
            .filter(CuratedReview.curated_dataset_id == dataset_id)
            .delete(synchronize_session=False)
        )

    version_ids = [
        version.id
        for version in (
            db.query(DatasetVersion)
            .filter(DatasetVersion.dataset_id == dataset_id)
            .all()
        )
    ]
    if version_ids:
        # 摘除运行记录对版本的血缘指针，否则版本删除撞 FK（NO ACTION）
        from app.data_channel.datasets.service import detach_run_version_lineage
        detach_run_version_lineage(db, version_ids)
        (
            db.query(MediaItem)
            .filter(MediaItem.dataset_version_id.in_(version_ids))
            .delete(synchronize_session=False)
        )
        (
            db.query(PipelineFileAsset)
            .filter(PipelineFileAsset.dataset_version_id.in_(version_ids))
            .delete(synchronize_session=False)
        )
    # 变更集随数据集显式清理（不依赖 SQLite FK 强制开关）；物理湖表幂等 DROP
    from app.data_channel.datasets import lake_store
    from app.data_channel.datasets.models import (
        DatasetChangeset, DatasetChangesetRow)
    changeset_ids = [
        row[0]
        for row in db.query(DatasetChangeset.id)
        .filter(DatasetChangeset.dataset_id == dataset_id)
        .all()
    ]
    if changeset_ids:
        (
            db.query(DatasetChangesetRow)
            .filter(DatasetChangesetRow.changeset_id.in_(changeset_ids))
            .delete(synchronize_session=False)
        )
        (
            db.query(DatasetChangeset)
            .filter(DatasetChangeset.dataset_id == dataset_id)
            .delete(synchronize_session=False)
        )
    lake_store.drop_lake_table(db, dataset_id)
    (
        db.query(DatasetVersion)
        .filter(DatasetVersion.dataset_id == dataset_id)
        .delete(synchronize_session=False)
    )
    db.delete(dataset)
    # 删除是行将消失的最危险操作：提交前留下唯一的服务端痕迹（连带删除的
    # 审核数/版本数与操作管理员），事后只能靠这条日志追溯。
    logger.warning(
        "删除成品数据集 %s：连带删除 %d 条审核与 %d 个版本并 DROP 物理湖表，"
        "操作管理员=%s",
        dataset_id, len(review_ids), len(version_ids), admin_id or "unknown",
    )
    db.commit()
    drain_storage_deletion_outbox(db)
