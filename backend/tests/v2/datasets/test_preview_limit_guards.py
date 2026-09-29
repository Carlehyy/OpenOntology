"""preview_data 服务层上限闸口测试。

preview_data 的 limit 由调用方直送（路由签名没有 Query(le=)），不夹上限
会被大值直送进 SQL LIMIT 拖垮湖表读取——服务层是唯一闸口，与
preview_dataset 的 min(limit, 1000) 保持同一口径。
"""
from __future__ import annotations

import uuid

from app.data_channel.datasets.models import Dataset, DatasetVersion


def _dataset_with_version(db) -> Dataset:
    ds = Dataset(
        id=str(uuid.uuid4()),
        name="预览上限测试",
        kind="structured",
        schema_json={"origin": "manual", "columns": ["a", "b"]},
    )
    db.add(ds)
    db.add(DatasetVersion(
        id=str(uuid.uuid4()), dataset_id=ds.id, version_no=1, rowcount=2,
        data_blob="a,b\n1,x\n2,y\n".encode("utf-8"), data_size=13))
    db.commit()
    return ds


def test_preview_data_clamps_limit_to_1000(db, monkeypatch):
    from app.data_channel.datasets.query_service import (
        preview_data,
        require_curated_preview_approved,
    )
    from app.services.v2.dataset_service import DatasetService

    ds = _dataset_with_version(db)
    seen: dict[str, int] = {}
    real_preview = DatasetService.preview

    def spy_preview(self, dataset_id, version_no=None, limit=100, offset=0):
        seen["limit"] = limit
        return real_preview(self, dataset_id, version_no,
                            limit=limit, offset=offset)

    monkeypatch.setattr(DatasetService, "preview", spy_preview)

    rows = preview_data(ds.id, 1, 500_000, db,
                        require_curated_preview_approved_fn=require_curated_preview_approved)

    assert seen["limit"] == 1000, "超大 limit 必须被夹到 1000"
    assert len(rows) == 2  # 数据本身只有 2 行，夹限不影响结果正确性


def test_preview_data_clamps_non_positive_limit_to_1(db, monkeypatch):
    from app.data_channel.datasets.query_service import (
        preview_data,
        require_curated_preview_approved,
    )
    from app.services.v2.dataset_service import DatasetService

    ds = _dataset_with_version(db)
    seen: dict[str, int] = {}
    real_preview = DatasetService.preview

    def spy_preview(self, dataset_id, version_no=None, limit=100, offset=0):
        seen["limit"] = limit
        return real_preview(self, dataset_id, version_no,
                            limit=limit, offset=offset)

    monkeypatch.setattr(DatasetService, "preview", spy_preview)

    preview_data(ds.id, 1, -5, db,
                 require_curated_preview_approved_fn=require_curated_preview_approved)

    assert seen["limit"] == 1, "非正数 limit 必须被夹到 1，不能直送 SQL"
