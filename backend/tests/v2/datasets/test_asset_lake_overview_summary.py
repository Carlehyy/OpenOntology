"""资产湖总览摘要（GET /datasets/overview/summary）聚合口径测试。

五张洞察卡片原先由前端拉取「成品全量列表 + 原始数据集全量总览」在浏览器
端聚合；现在改为服务端一次聚合。本文件对拍服务端口径与旧前端公式，并验证
空湖、HTTP 可达性（viewer 只读角色）与未认证拒绝。缓存命中/失效/降级语义
另见 tests/data_channel/test_lake_redis_cache.py。
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

import pytest

from app.auth.models import User
from app.auth.service import hash_password
from app.config import settings
from app.data_channel.curated import catalog_service
from app.data_channel.datasets import query_service

# 顶层导入保证 Dataset/DatasetVersion/CuratedReview 在 conftest 的 create_all
# 执行前注册到 Base.metadata。
from app.data_channel.curated.models import CuratedReview
from app.data_channel.datasets.models import Dataset, DatasetVersion


# 与旧前端 isPendingReview 同一集合：StructuredDataPage.tsx 的
# pending_review | pending | in_review。
_PENDING_STATUSES = frozenset({"pending_review", "pending", "in_review"})


@pytest.fixture(autouse=True)
def _disable_lake_cache(monkeypatch):
    """聚合口径对拍必须直查数据库：开发机可能有真实 Redis，
    跨测试的短 TTL 缓存会让对拍不确定；缓存语义由专门的假客户端用例覆盖。"""
    monkeypatch.setattr(settings, "dataset_cache_enabled", False)


def _curated(db, name, *, quality_score=None, version_count=1,
             review_status=None, review_version_index=None):
    """建一个成品数据集：quality_score 入 schema_json，按需挂审核。

    review_version_index 指定审核绑定第几个版本（None=最新），用于构造
    「审核绑定旧版本、最新版本无审核」的历史形态。
    """
    schema = {"primary_key": "id"}
    if quality_score is not None:
        schema["quality_score"] = quality_score
    dataset = Dataset(name=name, kind="curated", schema_json=schema)
    db.add(dataset)
    db.flush()
    versions = []
    for version_no in range(1, version_count + 1):
        version = DatasetVersion(
            dataset_id=dataset.id, version_no=version_no, rowcount=version_no)
        versions.append(version)
        db.add(version)
    db.flush()
    if review_status is not None:
        bound = versions[review_version_index if review_version_index is not None else -1]
        db.add(CuratedReview(
            curated_dataset_id=dataset.id,
            dataset_version_id=bound.id,
            status=review_status,
        ))
    db.commit()
    return dataset


def _expected_from_legacy_formula(db) -> dict:
    """旧前端公式：两个全量列表拉到浏览器后自行聚合。"""
    curated = catalog_service.list_curated(
        db, pipeline="", task_id="", status="", page=1, page_size=20,
        paginated=False,
    )
    raw_items = query_service.datasets_overview(
        db, paginated=False, consumer_map_fn=lambda _db: {},
    )["items"]

    manual_items = [
        item for item in raw_items if item["source"] in ("upload", "manual")
    ]
    scores = [
        item.quality_score
        for item in curated
        if isinstance(item.quality_score, (int, float))
        and not isinstance(item.quality_score, bool)
        and math.isfinite(item.quality_score)
    ]
    return {
        "total": len(curated) + len(raw_items),
        "curated_total": len(curated),
        "raw_total": len(raw_items),
        "manual_total": len(manual_items),
        "manual_with_primary_key": sum(
            1 for item in manual_items if bool(item["primary_key"])
        ),
        "sync_total": sum(
            1 for item in raw_items if item["source"] == "sync"
        ),
        "scored_curated_total": len(scores),
        "average_quality_score": (
            sum(scores) / len(scores) if scores else None
        ),
        "pending_review_total": sum(
            1 for item in curated if item.status in _PENDING_STATUSES
        ),
    }


def _assert_summaries_equal(actual: dict, expected: dict) -> None:
    for key, value in expected.items():
        if key == "average_quality_score" and value is not None:
            assert actual[key] == pytest.approx(value)
        else:
            assert actual[key] == value, key


def test_summary_matches_legacy_client_formula(db):
    """真实形态混合数据：服务端聚合 = 硬编码口径 = 旧前端公式。"""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)

    # 成品：已通过 / 无审核 / 待定 / 审核只绑定旧版本（最新版回退待审核）
    _curated(db, "已通过成品", quality_score=0.9, review_status="approved")
    _curated(db, "无审核成品", quality_score=0.7)
    _curated(db, "待定审核成品", quality_score=None, review_status="pending")
    _curated(
        db, "旧版本审核成品", quality_score=0.5, version_count=2,
        review_status="approved", review_version_index=0,
    )

    # 原始：上传(含主键) / 上传(无主键) / 在线建表 / 历史同步 / 集成资产
    db.add_all([
        Dataset(name="上传含主键", kind="structured",
                schema_json={"primary_key": "id"},
                created_at=base, updated_at=base),
        Dataset(name="上传无主键", kind="structured", schema_json={},
                created_at=base, updated_at=base),
        Dataset(name="在线建表", kind="structured",
                schema_json={"origin": "manual", "primary_key": "id"},
                created_at=base, updated_at=base),
        Dataset(name="SYNC::历史同步", kind="structured",
                schema_json={"primary_key": "id"},
                created_at=base, updated_at=base),
        Dataset(name="集成资产", kind="integrated", schema_json={},
                created_at=base, updated_at=base),
    ])
    db.commit()

    summary = query_service._overview_summary_from_db(db)

    # (i) 硬编码字面量：成品 4（已通过/无审核/待定/旧版本回退），
    # 原始 5（上传×2 + 在线建表 + 同步 + 集成），人工 4 其中 2 个声明主键，
    # 已评分 3 个（0.9 + 0.7 + 0.5）/3，待审核 3（无审核/待定/旧版本回退）。
    _assert_summaries_equal(summary, {
        "total": 9,
        "curated_total": 4,
        "raw_total": 5,
        "manual_total": 4,
        "manual_with_primary_key": 2,
        "sync_total": 1,
        "scored_curated_total": 3,
        "average_quality_score": (0.9 + 0.7 + 0.5) / 3,
        "pending_review_total": 3,
    })

    # (ii) 旧前端公式逐项对拍
    _assert_summaries_equal(summary, _expected_from_legacy_formula(db))


def test_summary_empty_lake_is_all_zero(db):
    assert query_service._overview_summary_from_db(db) == {
        "total": 0,
        "curated_total": 0,
        "raw_total": 0,
        "manual_total": 0,
        "manual_with_primary_key": 0,
        "sync_total": 0,
        "scored_curated_total": 0,
        "average_quality_score": None,
        "pending_review_total": 0,
    }


# ── HTTP：viewer 可读、未认证拒绝、不被 /{dataset_id} 路由遮蔽 ──────────

@pytest.fixture
def api(client, db):
    """v2 datasets 路由使用模块内 get_db，需单独覆盖到测试库。"""
    from app.main import app
    from app.data_channel.datasets import router as datasets_module

    def _override():
        yield db

    app.dependency_overrides[datasets_module.get_db] = _override
    yield client
    app.dependency_overrides.pop(datasets_module.get_db, None)


def _login(client, username, password):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_overview_summary_endpoint_readable_by_viewer(api, db):
    viewer = User(
        id=str(uuid.uuid4()), username="viewer",
        email="viewer@test.com",
        password_hash=hash_password("viewer123"), role="viewer",
    )
    db.add(viewer)
    db.commit()
    _curated(db, "HTTP 成品", quality_score=0.8)

    response = api.get(
        "/api/v2/datasets/overview/summary",
        headers=_login(api, "viewer", "viewer123"),
    )

    assert response.status_code == 200, response.text
    body = response.json().get("data", response.json())
    for key in (
        "total", "curated_total", "raw_total", "manual_total",
        "manual_with_primary_key", "sync_total", "scored_curated_total",
        "average_quality_score", "pending_review_total",
    ):
        assert key in body, key
    assert body["total"] == 1
    assert body["curated_total"] == 1
    assert body["raw_total"] == 0
    assert body["scored_curated_total"] == 1
    assert body["average_quality_score"] == pytest.approx(0.8)
    # 无审核记录的成品回退 pending_review，计入待审核卡片
    assert body["pending_review_total"] == 1


def test_overview_summary_endpoint_rejects_unauthenticated(api, db):
    response = api.get("/api/v2/datasets/overview/summary")
    assert response.status_code in (401, 403)
