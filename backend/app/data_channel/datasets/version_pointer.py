"""数据集当前版本的权威指针解析（leaf，无域内依赖）。

Dataset.latest_version_id 是当前版本唯一权威；候选版本（受控动作待审
变更）以更高 version_no 入库但不前移指针。指针非空精确命中（失配抛
dataset_pointer_broken，绝不 MAX 捡候选高号）；指针为空仅属迁移前历史
约定按 MAX 兜底。所有「当前数据」读取必须经本入口。
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.datasets.models import Dataset, DatasetVersion


def resolve_current_version(db: Session, dataset_id: str) -> DatasetVersion | None:
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is not None and dataset.latest_version_id:
        version = db.query(DatasetVersion).filter(
            DatasetVersion.id == dataset.latest_version_id,
            DatasetVersion.dataset_id == dataset_id).first()
        if version is None:
            raise HTTPException(500, detail={
                "code": "dataset_pointer_broken",
                "message": (
                    f"数据集 {dataset_id} 的当前版本指针指向不存在的版本"
                    f" {dataset.latest_version_id}，拒绝猜测最新版本"),
            })
        return version
    return (db.query(DatasetVersion)
            .filter(DatasetVersion.dataset_id == dataset_id)
            .order_by(DatasetVersion.version_no.desc()).first())
