"""Dataset-to-pipeline dependency queries.

This module is the canonical dependency boundary for dataset consumers.  HTTP
routers may expose the result, but domain code must not import router internals
to answer dependency questions.
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def dataset_consumers(db: Session, dataset_id: str) -> list[dict]:
    """Find pipelines that reference a dataset."""
    return dataset_consumer_map(db).get(dataset_id, [])


def dataset_consumer_map(db: Session) -> dict[str, list[dict]]:
    """Scan pipelines once and build a dataset-id to consumers mapping."""
    from app.data_channel.pipelines.models import Pipeline

    mapping: dict[str, list[dict]] = {}

    def add(dataset_id: str | None, pipeline: Pipeline) -> None:
        if not dataset_id:
            return
        entry = {
            "id": pipeline.id,
            "name": pipeline.name,
            "status": pipeline.status or "draft",
            "domain": pipeline.domain or "通用",
        }
        bucket = mapping.setdefault(dataset_id, [])
        if all(item["id"] != pipeline.id for item in bucket):
            bucket.append(entry)

    for pipeline in db.query(Pipeline).all():
        # canvas 画布节点（connector.files）引用扫描已随该引擎下线移除；
        # n8n/python 流水线只经 source_dataset_id 绑定源数据集。
        add(pipeline.source_dataset_id, pipeline)
    return mapping

