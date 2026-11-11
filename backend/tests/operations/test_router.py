from __future__ import annotations

import hashlib
import json
import uuid

from app.auth.models import User
from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.services.auth_service import hash_password


def _make_user(db, username: str, role: str) -> User:
    user = User(
        id=str(uuid.uuid4()), username=username, email=f"{username}@test.com",
        password_hash=hash_password("test123"), role=role,
    )
    db.add(user)
    db.commit()
    return user


def _make_dataset(db, name: str) -> Dataset:
    dataset = Dataset(id=str(uuid.uuid4()), name=name, kind="structured")
    db.add(dataset)
    db.commit()
    return dataset


_ORDERS_ROWS = [
    {"region": "east", "amount": 10},
    {"region": "east", "amount": 20},
    {"region": "west", "amount": 30},
]

# 已保存文章正文：命名 SQL + 一个 text 筛选器 region（声明 :region）。
_ORDERS_MARKDOWN = (
    "# 订单分析\n\n"
    "```sql daily_totals\n"
    "SELECT region, COUNT(*) AS total FROM dataset GROUP BY region\n"
    "```\n\n"
    "```filter\nname: region\ntype: text\n```\n"
)


def _make_dataset_with_version(db, name: str = "订单明细") -> tuple[Dataset, DatasetVersion]:
    """真实 structured 数据集：DB 内权威版本 blob + latest 指针，走完整
    SQLite 物化执行路径（不 mock execute/source）。"""
    blob = json.dumps(_ORDERS_ROWS, ensure_ascii=False).encode("utf-8")
    dataset = Dataset(
        id=str(uuid.uuid4()), name=name, kind="structured",
        schema_json={
            "columns": ["region", "amount"],
            "columns_typed": [
                {"name": "region", "type": "string"},
                {"name": "amount", "type": "number"},
            ],
        },
    )
    db.add(dataset)
    db.flush()
    version = DatasetVersion(
        id=str(uuid.uuid4()), dataset_id=dataset.id, version_no=1,
        rowcount=len(_ORDERS_ROWS), data_blob=blob, data_size=len(blob),
        checksum=hashlib.sha256(blob).hexdigest(),
    )
    db.add(version)
    db.flush()
    dataset.latest_version_id = version.id
    db.commit()
    return dataset, version


def _make_report_with_article(client, headers, dataset_id: str,
                              markdown: str = _ORDERS_MARKDOWN) -> tuple[str, str]:
    base = "/api/v2/operations/reports"
    report_id = client.post(base, json={"name": "运营周报"}, headers=headers).json()["data"]["id"]
    created = client.post(
        f"{base}/{report_id}/articles",
        json={"title": "订单分析", "dataset_id": dataset_id, "markdown": markdown},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return report_id, created.json()["data"]["id"]


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "test123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_report_book_crud_and_revision_conflict(client, db):
    _make_user(db, "report_editor", "editor")
    headers = _login(client, "report_editor")
    base = "/api/v2/operations/reports"

    created = client.post(base, json={"name": "运营周报", "description": "订单趋势"}, headers=headers)
    assert created.status_code == 201, created.text
    report = created.json()["data"]
    assert report["article_count"] == 0
    assert report["revision"] == 1

    updated = client.patch(
        f"{base}/{report['id']}",
        json={"description": "订单趋势 v2", "expected_revision": 1},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["revision"] == 2

    conflict = client.patch(
        f"{base}/{report['id']}",
        json={"description": "旧修改", "expected_revision": 1},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "report_revision_conflict"


def test_viewer_can_read_but_cannot_write_report(client, db):
    _make_user(db, "report_viewer", "viewer")
    headers = _login(client, "report_viewer")
    base = "/api/v2/operations/reports"
    assert client.get(base, headers=headers).status_code == 200
    response = client.post(base, json={"name": "不应创建"}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "REPORT_WRITE_DENIED"


def test_custom_role_without_menu_cannot_read_report(client, db):
    _make_user(db, "report_custom", "custom")
    headers = _login(client, "report_custom")
    response = client.get("/api/v2/operations/reports", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_second_report_gets_distinct_sort_order(client, db):
    _make_user(db, "report_order_editor", "editor")
    headers = _login(client, "report_order_editor")
    base = "/api/v2/operations/reports"

    first = client.post(base, json={"name": "周报"}, headers=headers).json()["data"]
    second = client.post(base, json={"name": "月报"}, headers=headers).json()["data"]

    items = client.get(base, headers=headers).json()["data"]["items"]
    assert [item["id"] for item in items] == [first["id"], second["id"]]


def test_article_save_reorder_delete_and_revision_conflict(client, db):
    _make_user(db, "report_article_editor", "editor")
    headers = _login(client, "report_article_editor")
    base = "/api/v2/operations/reports"
    dataset = _make_dataset(db, "订单明细")

    report_id = client.post(base, json={"name": "运营周报"}, headers=headers).json()["data"]["id"]
    articles = f"{base}/{report_id}/articles"

    first = client.post(articles, json={"title": "第一篇", "dataset_id": dataset.id}, headers=headers)
    assert first.status_code == 201, first.text
    first_data = first.json()["data"]
    assert first_data["position"] == 0
    assert first_data["revision"] == 1

    # 回归：首篇 sort_order=0 是假值，`0 or -1` 曾让第二篇再次取 0，
    # 触发 (report_id, sort_order) 唯一约束冲突返回 500。
    second = client.post(articles, json={"title": "第二篇", "dataset_id": dataset.id}, headers=headers)
    assert second.status_code == 201, second.text
    second_data = second.json()["data"]
    assert second_data["position"] == 1

    detail = client.get(f"{base}/{report_id}", headers=headers).json()["data"]
    assert detail["article_count"] == 2
    assert [item["position"] for item in detail["articles"]] == [0, 1]

    conflict = client.patch(
        f"{articles}/{second_data['id']}",
        json={"title": "过期修改", "expected_revision": 99},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "article_revision_conflict"

    saved = client.patch(
        f"{articles}/{second_data['id']}",
        json={"title": "第二篇修订", "expected_revision": 1},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["title"] == "第二篇修订"
    assert saved.json()["data"]["revision"] == 2

    reordered = client.post(
        f"{articles}/reorder",
        json={"article_ids": [second_data["id"], first_data["id"]]},
        headers=headers,
    )
    assert reordered.status_code == 200, reordered.text
    items = reordered.json()["data"]["items"]
    assert [item["id"] for item in items] == [second_data["id"], first_data["id"]]
    assert [item["position"] for item in items] == [0, 1]

    detail = client.get(f"{base}/{report_id}", headers=headers).json()["data"]
    assert [item["title"] for item in detail["articles"]] == ["第二篇修订", "第一篇"]

    invalid = client.post(
        f"{articles}/reorder",
        json={"article_ids": [first_data["id"]]},
        headers=headers,
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_article_order"

    deleted = client.delete(f"{articles}/{second_data['id']}", headers=headers)
    assert deleted.status_code == 200
    detail = client.get(f"{base}/{report_id}", headers=headers).json()["data"]
    assert [item["id"] for item in detail["articles"]] == [first_data["id"]]
    assert detail["articles"][0]["position"] == 0

    blank = client.post(articles, json={"title": "   ", "dataset_id": dataset.id}, headers=headers)
    assert blank.status_code == 422
    assert blank.json()["detail"]["code"] == "article_title_required"


def test_execute_article_sql_aggregates_and_named_param_literals(client, db):
    _make_user(db, "report_exec_editor", "editor")
    headers = _login(client, "report_exec_editor")
    dataset, version = _make_dataset_with_version(db)
    report_id, article_id = _make_report_with_article(client, headers, dataset.id)
    url = f"/api/v2/operations/reports/{report_id}/articles/{article_id}/execute"

    scoped = client.post(url, headers=headers, json={
        "sql": "SELECT region, COUNT(*) AS orders, SUM(amount) AS total "
               "FROM dataset WHERE (:region IS NULL OR region = :region) "
               "GROUP BY region ORDER BY region",
        "params": {"region": "east"},
        "limit": 10,
    })
    assert scoped.status_code == 200, scoped.text
    data = scoped.json()["data"]
    assert data["columns"] == ["region", "orders", "total"]
    assert data["rows"] == [{"region": "east", "orders": 2, "total": 30.0}]
    assert data["rowcount"] == 1
    assert data["source"] == "materialized_temp"
    assert data["dataset"]["version_id"] == version.id
    assert data["dataset"]["version_no"] == 1
    assert data["dataset"]["materialized_rows"] == 3

    unscoped = client.post(url, headers=headers, json={
        "sql": "SELECT region, COUNT(*) AS orders, SUM(amount) AS total "
               "FROM dataset WHERE (:region IS NULL OR region = :region) "
               "GROUP BY region ORDER BY region",
        "params": {"region": None},
    })
    assert unscoped.status_code == 200, unscoped.text
    assert unscoped.json()["data"]["rows"] == [
        {"region": "east", "orders": 2, "total": 30.0},
        {"region": "west", "orders": 1, "total": 30.0},
    ]


def test_execute_article_draft_filter_trials_without_saving(client, db):
    _make_user(db, "report_draft_editor", "editor")
    headers = _login(client, "report_draft_editor")
    dataset, _version = _make_dataset_with_version(db)
    report_id, article_id = _make_report_with_article(client, headers, dataset.id)
    url = f"/api/v2/operations/reports/{report_id}/articles/{article_id}/execute"
    sql = "SELECT COUNT(*) AS qualified FROM dataset WHERE amount >= :min_amount"

    # 草稿只声明新筛选器 min_amount（未保存），试跑必须按草稿校验通过。
    trialed = client.post(url, headers=headers, json={
        "sql": sql,
        "markdown": "# 草稿\n\n```filter\nname: min_amount\ntype: number\n```\n",
        "params": {"min_amount": 15},
    })
    assert trialed.status_code == 200, trialed.text
    assert trialed.json()["data"]["rows"] == [{"qualified": 2}]

    # 同一条 SQL 不带草稿（已保存正文只声明 region）→ 未声明筛选器拒绝。
    rejected = client.post(url, headers=headers, json={
        "sql": sql, "params": {"min_amount": 15},
    })
    assert rejected.status_code == 422
    detail = rejected.json()["detail"]
    assert detail["code"] == "report_sql_filter_undeclared"
    assert detail["parameters"] == ["min_amount"]

    # 草稿试跑不落库：文章正文与 revision 保持不变。
    after = client.get(
        f"/api/v2/operations/reports/{report_id}/articles/{article_id}",
        headers=headers,
    ).json()["data"]
    assert after["markdown"] == _ORDERS_MARKDOWN
    assert after["revision"] == 1


def test_execute_article_batch_queries_share_one_version(client, db):
    _make_user(db, "report_batch_editor", "editor")
    headers = _login(client, "report_batch_editor")
    dataset, version = _make_dataset_with_version(db)
    report_id, article_id = _make_report_with_article(client, headers, dataset.id)
    url = f"/api/v2/operations/reports/{report_id}/articles/{article_id}/execute"

    response = client.post(url, headers=headers, json={
        "queries": {
            "by_region": "SELECT region, COUNT(*) AS orders, SUM(amount) AS total "
                         "FROM dataset GROUP BY region ORDER BY region",
            "grand_total": "SELECT COUNT(*) AS rows_total, SUM(amount) AS grand FROM dataset",
        },
        "limit": 10,
    })
    assert response.status_code == 200, response.text
    results = response.json()["data"]["results"]
    assert set(results) == {"by_region", "grand_total"}
    assert results["by_region"]["rows"] == [
        {"region": "east", "orders": 2, "total": 30.0},
        {"region": "west", "orders": 1, "total": 30.0},
    ]
    assert results["grand_total"]["rows"] == [{"rows_total": 3, "grand": 60.0}]
    # 一次物化、同一快照：所有命名查询命中同一版本。
    assert {item["dataset"]["version_id"] for item in results.values()} == {version.id}


def test_execute_article_request_contract_rejections(client, db):
    _make_user(db, "report_contract_editor", "editor")
    headers = _login(client, "report_contract_editor")
    dataset, _version = _make_dataset_with_version(db)
    report_id, article_id = _make_report_with_article(client, headers, dataset.id)
    url = f"/api/v2/operations/reports/{report_id}/articles/{article_id}/execute"
    select = "SELECT COUNT(*) AS n FROM dataset"

    # sql 与 queries 必须二选一。
    both = client.post(url, headers=headers,
                       json={"sql": select, "queries": {"q": select}})
    assert both.status_code == 422
    neither = client.post(url, headers=headers, json={"params": {}})
    assert neither.status_code == 422
    # queries 至少 1 条、至多 20 条。
    empty = client.post(url, headers=headers, json={"queries": {}})
    assert empty.status_code == 422
    too_many = client.post(url, headers=headers, json={
        "queries": {f"q{index}": select for index in range(21)}})
    assert too_many.status_code == 422
    # 单条 SQL 不超过 20000 字符。
    oversized = client.post(url, headers=headers,
                            json={"sql": select + " -- " + "x" * 20_000})
    assert oversized.status_code == 422


def test_execute_article_denies_unauthenticated_and_menu_less_role(client, db):
    _make_user(db, "report_exec_custom", "custom")
    headers = _login(client, "report_exec_custom")
    url = ("/api/v2/operations/reports/00000000-0000-0000-0000-000000000000"
           "/articles/00000000-0000-0000-0000-000000000000/execute")
    body = {"sql": "SELECT COUNT(*) AS n FROM dataset"}

    # 未携带凭据：授权门在路由层先于正文与目标查找生效。
    assert client.post(url, json=body).status_code == 403
    denied = client.post(url, json=body, headers=headers)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_execute_article_rejects_out_of_bounds_targets(client, db):
    _make_user(db, "report_bounds_editor", "editor")
    headers = _login(client, "report_bounds_editor")
    dataset, _version = _make_dataset_with_version(db)
    report_id, article_id = _make_report_with_article(client, headers, dataset.id)
    other_report_id = client.post(
        "/api/v2/operations/reports", json={"name": "另一本"}, headers=headers,
    ).json()["data"]["id"]
    base = "/api/v2/operations/reports"
    body = {"sql": "SELECT COUNT(*) AS n FROM dataset"}

    cross_report = client.post(
        f"{base}/{other_report_id}/articles/{article_id}/execute",
        json=body, headers=headers)
    assert cross_report.status_code == 404
    assert cross_report.json()["detail"]["code"] == "article_not_found"

    # execute 以文章作用域解析目标（article 同时匹配 report_id），因此
    # 不存在的报告册与文章一样统一落在 article_not_found。
    missing_report = client.post(
        f"{base}/{uuid.uuid4()}/articles/{article_id}/execute",
        json=body, headers=headers)
    assert missing_report.status_code == 404
    assert missing_report.json()["detail"]["code"] == "article_not_found"

    missing_article = client.post(
        f"{base}/{report_id}/articles/{uuid.uuid4()}/execute",
        json=body, headers=headers)
    assert missing_article.status_code == 404
    assert missing_article.json()["detail"]["code"] == "article_not_found"
