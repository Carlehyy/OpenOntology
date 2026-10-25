"""数据资产湖 Redis 读缓存测试：命中/回源/降级/版本失效与键空间正确性。

所有用例用内存版 fake 客户端，不依赖真实 Redis；缓存层的对外契约是
「Redis 出现任何问题都必须等价于没启用缓存」，用故障注入显式验证。
"""
import json
import uuid

import pytest

from app.config import settings
from app.data_channel.datasets import cache as lake_cache
from app.shared import redis_cache as shared_redis

# 顶层导入保证 Dataset/DatasetVersion/Connection/Pipeline 模型在 conftest
# 的 create_all 执行前注册到 Base.metadata。
from app.models.v2.dataset import Dataset, DatasetVersion  # noqa: F401
from app.models.v2.connection import Connection  # noqa: F401
from app.models.v2.pipeline import Pipeline  # noqa: F401


class FakeRedis:
    """内存版 redis 客户端：只实现共享缓存模块用到的接口，可注入故障。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail_on: set[str] = set()

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise RuntimeError(f"fake redis {op} failure")

    def get(self, key):
        self._maybe_fail("get")
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self._maybe_fail("set")
        self.store[key] = value
        return True

    def incr(self, key, amount=1):
        self._maybe_fail("incr")
        current = int(self.store.get(key, "0"))
        self.store[key] = str(current + int(amount))
        return current + int(amount)

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(shared_redis, "get_client", lambda: client)
    monkeypatch.setattr(shared_redis, "_client", None)
    monkeypatch.setattr(shared_redis, "_client_failed_at", 0.0)
    return client


# ---------------------------------------------------------------------------
# 缓存胶水层：命中 / 回源 / 降级 / 体积上限 / 版本失效
# ---------------------------------------------------------------------------

def test_cached_call_miss_loads_and_hit_skips_builder(fake):
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return {"rows": [1, 2]}

    key = "ob:lake:k1"
    first = lake_cache.cached_call(key, 60, builder)
    second = lake_cache.cached_call(key, 60, builder)
    assert first == {"rows": [1, 2]}
    assert second == first
    assert calls["n"] == 1
    assert json.loads(fake.store[key]) == {"rows": [1, 2]}


def test_cached_call_caches_empty_payload(fake):
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return []

    key = "ob:lake:k2"
    assert lake_cache.cached_call(key, 60, builder) == []
    assert lake_cache.cached_call(key, 60, builder) == []
    assert calls["n"] == 1


def test_redis_get_failure_falls_back_to_builder_without_raising(fake):
    fake.fail_on = {"get"}
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return {"ok": True}

    key = "ob:lake:k3"
    first = lake_cache.cached_call(key, 60, builder)
    second = lake_cache.cached_call(key, 60, builder)
    assert first == second == {"ok": True}
    assert calls["n"] == 2  # 每次失败都回源，绝不吞结果


def test_redis_set_failure_still_returns_builder_value(fake):
    fake.fail_on = {"set"}
    value = lake_cache.cached_call("ob:lake:k4", 60, lambda: {"a": 1})
    assert value == {"a": 1}
    assert not fake.store


def test_oversize_payload_is_not_cached(fake):
    value = {"blob": "x" * (lake_cache.MAX_PAYLOAD_BYTES + 10)}
    result = lake_cache.cached_call("ob:lake:big", 60, lambda: value)
    assert result == value
    assert not fake.store


def test_disabled_flag_bypasses_cache_entirely(monkeypatch):
    monkeypatch.setattr(settings, "dataset_cache_enabled", False)
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return {"ok": True}

    first = lake_cache.cached_call("ob:lake:off", 60, builder)
    second = lake_cache.cached_call("ob:lake:off", 60, builder)
    assert first == second == {"ok": True}
    assert calls["n"] == 2


def test_overview_key_rotates_after_invalidate(fake):
    params = {"source": "manual", "page": 1}
    before = lake_cache.overview_key(params)
    assert before == lake_cache.overview_key(params)
    lake_cache.invalidate_overview()
    after = lake_cache.overview_key(params)
    assert after != before
    assert before.startswith("ob:lake:overview:v0:")
    assert after.startswith("ob:lake:overview:v1:")


# ---------------------------------------------------------------------------
# 查询路径集成：键空间、短 TTL 语义与写路径失效
# ---------------------------------------------------------------------------

def _dataset(dataset_id: str, name: str) -> Dataset:
    return Dataset(
        id=dataset_id, name=name, kind="structured",
        schema_json={"origin": "manual"},
    )


def test_overview_cache_hit_and_invalidate(db, monkeypatch, fake):
    from app.data_channel.datasets.consumers import dataset_consumer_map
    from app.data_channel.datasets.query_service import datasets_overview

    db.add(_dataset(str(uuid.uuid4()), "销售订单"))
    db.commit()

    first = datasets_overview(db, consumer_map_fn=dataset_consumer_map)
    second = datasets_overview(db, consumer_map_fn=dataset_consumer_map)
    assert first == second
    assert first["total"] == 1
    assert first["items"][0]["name"] == "销售订单"
    assert fake.store  # 首次回源后写入了缓存

    # 短 TTL 语义：新增数据集后命中旧缓存（≤10 秒滞后）
    db.add(_dataset(str(uuid.uuid4()), "第二个数据集"))
    db.commit()
    third = datasets_overview(db, consumer_map_fn=dataset_consumer_map)
    assert third["total"] == 1

    # 写路径 bump 版本后立即回源
    lake_cache.invalidate_overview()
    fourth = datasets_overview(db, consumer_map_fn=dataset_consumer_map)
    assert fourth["total"] == 2


def test_overview_redis_failure_never_affects_result(db, monkeypatch, fake):
    from app.data_channel.datasets.consumers import dataset_consumer_map
    from app.data_channel.datasets.query_service import datasets_overview

    db.add(_dataset(str(uuid.uuid4()), "销售订单"))
    db.commit()
    fake.fail_on = {"get", "set"}

    result = datasets_overview(db, consumer_map_fn=dataset_consumer_map)
    assert result["total"] == 1
    assert result["items"][0]["name"] == "销售订单"


def test_preview_cache_keyed_by_version_id(db, monkeypatch, fake):
    from app.data_channel.datasets.query_service import (
        preview_dataset,
        require_curated_preview_approved,
    )
    from app.services.v2.dataset_service import DatasetService

    ds = _dataset(str(uuid.uuid4()), "预览集")
    ds.schema_json = {"origin": "manual", "columns": ["a", "b"]}
    db.add(ds)
    db.add(DatasetVersion(
        id=str(uuid.uuid4()), dataset_id=ds.id, version_no=1, rowcount=2,
        data_blob="a,b\n1,x\n2,y\n".encode("utf-8"), data_size=13))
    db.commit()

    real_preview = DatasetService.preview
    calls = {"n": 0}

    def counting_preview(self, dataset_id, version_no=None, limit=100, offset=0):
        calls["n"] += 1
        return real_preview(self, dataset_id, version_no, limit=limit, offset=offset)

    monkeypatch.setattr(DatasetService, "preview", counting_preview)

    gate = require_curated_preview_approved
    first = preview_dataset(ds.id, limit=20, offset=0, db=db,
                            require_curated_preview_approved_fn=gate)
    second = preview_dataset(ds.id, limit=20, offset=0, db=db,
                             require_curated_preview_approved_fn=gate)
    assert first["rows"] == [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]
    assert first == second
    assert calls["n"] == 1

    # 新版本 → 新键 → 自动回源，无需显式失效
    db.add(DatasetVersion(
        id=str(uuid.uuid4()), dataset_id=ds.id, version_no=2, rowcount=1,
        data_blob="a,b\n9,z\n".encode("utf-8"), data_size=10))
    db.commit()
    third = preview_dataset(ds.id, limit=20, offset=0, db=db,
                            require_curated_preview_approved_fn=gate)
    assert third["version_no"] == 2
    assert third["rows"] == [{"a": "9", "b": "z"}]
    assert calls["n"] == 2


def test_schema_cache_uses_contract_fingerprint(db, monkeypatch, fake):
    from app.data_channel.datasets.query_service import get_schema
    from app.services.v2.dataset_service import DatasetService

    ds = _dataset(str(uuid.uuid4()), "契约集")
    ds.schema_json = {
        "origin": "manual",
        "types_source": "declared",
        "columns": ["a"],
        "columns_typed": [{"name": "a", "type": "string"}],
    }
    db.add(ds)
    db.commit()

    real_preview = DatasetService.preview
    calls = {"n": 0}

    def counting_preview(self, dataset_id, version_no=None, limit=100, offset=0):
        calls["n"] += 1
        return real_preview(self, dataset_id, version_no, limit=limit, offset=offset)

    monkeypatch.setattr(DatasetService, "preview", counting_preview)

    first = get_schema(ds.id, db)
    second = get_schema(ds.id, db)
    assert first == second
    assert first["columns"][0]["name"] == "a"
    assert first["columns"][0]["type"] == "string"
    assert calls["n"] == 1


def test_version_publish_invalidates_overview(db, monkeypatch, fake):
    """DatasetService.create_version 成功后 bump 总览版本键。"""
    from app.data_channel.datasets.service import DatasetService
    from app.data_channel.datasets.consumers import dataset_consumer_map
    from app.data_channel.datasets.query_service import datasets_overview

    ds = _dataset(str(uuid.uuid4()), "发布集")
    db.add(ds)
    db.commit()

    first = datasets_overview(db, consumer_map_fn=dataset_consumer_map)
    assert first["items"][0]["latest_version_no"] == 0

    svc = DatasetService(db)
    svc.create_version(
        ds.id,
        "a,b\n1,x\n".encode("utf-8"),
        rowcount=1,
        schema_json={"origin": "manual", "columns": ["a", "b"]},
    )
    second = datasets_overview(db, consumer_map_fn=dataset_consumer_map)
    assert second["items"][0]["latest_version_no"] == 1
    assert second["items"][0]["rowcount"] == 1


# ---------------------------------------------------------------------------
# 总览摘要（五张洞察卡片）：键空间、缓存命中与写路径失效
# ---------------------------------------------------------------------------

def test_summary_cached_under_versioned_key_and_invalidates(db, fake):
    """摘要键 = ob:lake:summary:v{总览版本}：未 bump 时命中旧值，bump 后换键回源。"""
    from app.data_channel.datasets.query_service import overview_summary

    db.add(_dataset(str(uuid.uuid4()), "摘要集"))
    db.commit()

    first = overview_summary(db)
    assert first["total"] == 1
    assert first["manual_total"] == 1
    assert fake.store["ob:lake:summary:v0"], fake.store.keys()

    # 短 TTL 语义：直写数据库（不 bump）后命中旧缓存，数字保持旧值
    db.add(_dataset(str(uuid.uuid4()), "第二个摘要集"))
    db.commit()
    second = overview_summary(db)
    assert second["total"] == 1

    # 写路径 bump 总览版本：摘要键轮转到 v1，立即回源出新数字
    lake_cache.invalidate_overview()
    assert fake.store["ob:lake:overview:ver"] == "1"
    third = overview_summary(db)
    assert third["total"] == 2
    assert fake.store["ob:lake:summary:v1"]
    # 旧键不主动清扫，靠 TTL 自然过期
    assert "ob:lake:summary:v0" in fake.store


def test_summary_disabled_flag_bypasses_cache(db, monkeypatch, fake):
    monkeypatch.setattr(settings, "dataset_cache_enabled", False)
    from app.data_channel.datasets.query_service import overview_summary

    db.add(_dataset(str(uuid.uuid4()), "关闭缓存集"))
    db.commit()

    first = overview_summary(db)
    second = overview_summary(db)
    assert first == second
    assert first["total"] == 1
    assert not fake.store  # 开关关闭时既不读也不写 Redis


@pytest.mark.parametrize("failure", [{"get"}, {"set"}])
def test_summary_redis_failure_never_affects_result(db, fake, failure):
    fake.fail_on = failure
    from app.data_channel.datasets.query_service import overview_summary

    db.add(_dataset(str(uuid.uuid4()), "故障集"))
    db.commit()

    result = overview_summary(db)
    assert result["total"] == 1
    assert result["manual_total"] == 1


def _curated_with_version(db, dataset_id: str) -> Dataset:
    """最小成品资产：curated Dataset + 一个 blob-less 版本，供审核流使用。"""
    dataset = Dataset(
        id=dataset_id, name=f"成品-{dataset_id[:8]}", kind="curated",
        schema_json={"primary_key": "id"})
    db.add(dataset)
    db.flush()
    db.add(DatasetVersion(
        id=str(uuid.uuid4()), dataset_id=dataset.id, version_no=1, rowcount=1))
    db.commit()
    return dataset


def test_curated_write_paths_bump_overview_version(db, fake):
    """start_review/approve/reject/delete_curated 提交后各 bump 一次版本键。"""
    from fastapi import HTTPException
    from app.data_channel.curated.review_service import ReviewService
    from app.data_channel.curated.lifecycle_service import delete_curated

    def version() -> int:
        return int(fake.store.get("ob:lake:overview:ver", "0"))

    ds_one = _curated_with_version(db, str(uuid.uuid4()))
    ds_two = _curated_with_version(db, str(uuid.uuid4()))

    reviews = ReviewService(db)
    review = reviews.start_review(ds_one.id, reviewer_id="u-reviewer")
    assert review.status == "pending"
    assert version() == 1

    reviews.approve(review.id, reviewer_id="u-approver")
    assert version() == 2

    # 失败的决定（审核已定）不 bump：版本键原地不动
    with pytest.raises(HTTPException) as exc_info:
        reviews.approve(review.id)
    assert exc_info.value.status_code == 409
    assert version() == 2

    review_two = reviews.start_review(ds_two.id)
    assert version() == 3
    reviews.reject(review_two.id, notes="质量不达标")
    assert version() == 4

    delete_curated(db, ds_one.id, force=False, admin_id="u-admin")
    assert version() == 5
