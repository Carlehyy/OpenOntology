"""计划文章与计划试跑/AI 设计的 HTTP 边界：保存/切换、预览不落库、参数与
字段拒绝、RBAC、AI 候选错误可见性、三类资产可用性矩阵。"""
from __future__ import annotations

import hashlib
import json
import uuid

from app.auth.models import User
from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.operations import query_service
from app.services.auth_service import hash_password

_BASE = "/api/v2/operations/reports"

_ROWS = [
    {"region": "east", "amount": 10, "day": "2026-01-01"},
    {"region": "east", "amount": 20, "day": "2026-01-02"},
    {"region": "west", "amount": 30, "day": "2026-01-01"},
]


def _make_user(db, username: str, role: str) -> User:
    user = User(
        id=str(uuid.uuid4()), username=username, email=f"{username}@test.com",
        password_hash=hash_password("test123"), role=role,
    )
    db.add(user)
    db.commit()
    return user


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "test123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _make_dataset(db, *, kind: str = "structured", schema: dict | None,
                  rows: list[dict] | None = _ROWS, name: str = "订单明细") -> Dataset:
    dataset = Dataset(id=str(uuid.uuid4()), name=name, kind=kind, schema_json=schema)
    db.add(dataset)
    db.flush()
    if rows is not None:
        blob = json.dumps(rows, ensure_ascii=False).encode("utf-8")
        version = DatasetVersion(
            id=str(uuid.uuid4()), dataset_id=dataset.id, version_no=1,
            rowcount=len(rows), data_blob=blob, data_size=len(blob),
            checksum=hashlib.sha256(blob).hexdigest(),
        )
        db.add(version)
        db.flush()
        dataset.latest_version_id = version.id
    db.commit()
    return dataset


def _typed_schema() -> dict:
    return {
        "columns": ["region", "amount", "day"],
        "columns_typed": [
            {"name": "region", "type": "string"},
            {"name": "amount", "type": "number"},
            {"name": "day", "type": "date"},
        ],
    }


def _plan_payload(**overrides) -> dict:
    payload: dict = {
        "version": 1,
        "filters": [
            {"name": "region", "type": "text", "field": "region"},
            {"name": "min_amount", "type": "number", "field": "amount"},
        ],
        "blocks": [
            {"type": "text", "text": "订单概览"},
            {"type": "query", "name": "q1", "columns": [
                {"field": "region"},
                {"agg": "count", "alias": "orders"},
                {"agg": "sum", "field": "amount", "alias": "total"},
             ], "sort": [{"field": "total", "dir": "desc"}]},
            {"type": "chart", "chart": "bar", "query": "q1",
             "x": "region", "y": ["total"]},
        ],
    }
    payload.update(overrides)
    return payload


def _make_report(client, headers) -> str:
    created = client.post(_BASE, json={"name": "运营周报"}, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


# ---------------------------------------------------------------- 保存与切换


def test_create_article_with_plan_stores_plan_and_generated_markdown(client, db):
    _make_user(db, "plan_editor", "editor")
    headers = _login(client, "plan_editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    report_id = _make_report(client, headers)

    created = client.post(f"{_BASE}/{report_id}/articles", headers=headers, json={
        "title": "计划文章", "dataset_id": dataset.id, "plan": _plan_payload(),
    })
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["plan_json"]["version"] == 1
    assert "```sql q1" in data["markdown"]
    assert "LIMIT 200" in data["markdown"]
    from app.operations.markdown import validate_markdown
    assert validate_markdown(data["markdown"]).ok
    detail = client.get(f"{_BASE}/{report_id}/articles/{data['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["plan_json"] == data["plan_json"]


def test_update_article_markdown_detaches_plan_and_plan_regenerates(client, db):
    _make_user(db, "plan_switch_editor", "editor")
    headers = _login(client, "plan_switch_editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    report_id = _make_report(client, headers)
    created = client.post(f"{_BASE}/{report_id}/articles", headers=headers, json={
        "title": "计划文章", "dataset_id": dataset.id, "plan": _plan_payload(),
    }).json()["data"]
    articles = f"{_BASE}/{report_id}/articles/{created['id']}"

    # 手写 markdown 保存后与计划脱钩：plan_json 清空。
    handwritten = "# 手写\n\n```sql q\nSELECT COUNT(*) AS n FROM dataset\n```\n"
    switched = client.patch(articles, headers=headers, json={"markdown": handwritten})
    assert switched.status_code == 200, switched.text
    assert switched.json()["data"]["plan_json"] is None
    assert switched.json()["data"]["markdown"] == handwritten
    assert switched.json()["data"]["revision"] == 2

    # 再以计划形态保存：plan_json 与 markdown 同时重建。
    again = client.patch(articles, headers=headers, json={"plan": _plan_payload()})
    assert again.status_code == 200, again.text
    assert again.json()["data"]["plan_json"]["version"] == 1
    assert "```sql q1" in again.json()["data"]["markdown"]
    assert again.json()["data"]["revision"] == 3


def test_update_article_dataset_change_revalidates_plan_whitelist(client, db):
    _make_user(db, "plan_dataset_editor", "editor")
    headers = _login(client, "plan_dataset_editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    other = _make_dataset(db, schema={
        "columns": ["city"], "columns_typed": [{"name": "city", "type": "string"}],
    }, name="城市")
    compatible = _make_dataset(db, schema=_typed_schema(), name="同构数据集")
    report_id = _make_report(client, headers)
    created = client.post(f"{_BASE}/{report_id}/articles", headers=headers, json={
        "title": "计划文章", "dataset_id": dataset.id, "plan": _plan_payload(),
    }).json()["data"]
    articles = f"{_BASE}/{report_id}/articles/{created['id']}"

    rejected = client.patch(articles, headers=headers, json={"dataset_id": other.id})
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "invalid_report_plan"
    assert any("白名单" in error for error in rejected.json()["detail"]["errors"])

    moved = client.patch(articles, headers=headers, json={"dataset_id": compatible.id})
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["dataset_id"] == compatible.id


# ---------------------------------------------------------------- 计划试跑


def test_plan_preview_executes_queries_with_filters_and_returns_markdown(client, db):
    _make_user(db, "plan_preview_editor", "editor")
    headers = _login(client, "plan_preview_editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    report_id = _make_report(client, headers)

    response = client.post(f"{_BASE}/{report_id}/plan-preview", headers=headers, json={
        "dataset_id": dataset.id,
        "plan": _plan_payload(),
        "params": {"region": "east", "min_amount": None},
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["plan_version"] == 1
    assert [block["type"] for block in data["blocks"]] == ["text", "query", "chart"]
    rows = data["blocks"][1]["result"]["rows"]
    assert rows == [{"region": "east", "orders": 2, "total": 30.0}]
    assert data["version"]["version_no"] == 1
    assert data["source"] == "materialized_temp"
    # 服务端确定性生成的正文，前端高级 Markdown 切换直接复用。
    assert "```sql q1" in data["markdown"] and "LIMIT 200" in data["markdown"]

    # 试跑不落库：报告册文章数保持为 0。
    detail = client.get(f"{_BASE}/{report_id}", headers=headers).json()["data"]
    assert detail["article_count"] == 0


def test_plan_preview_number_and_date_filters_execute(client, db):
    _make_user(db, "plan_filter_editor", "editor")
    headers = _login(client, "plan_filter_editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    report_id = _make_report(client, headers)
    plan = _plan_payload(
        filters=[{"name": "min_amount", "type": "number", "field": "amount"},
                 {"name": "d", "type": "date", "field": "day"}],
        blocks=[{"type": "query", "name": "q", "columns": [
            {"field": "region"}, {"agg": "count", "alias": "orders"}]}],
    )

    by_amount = client.post(f"{_BASE}/{report_id}/plan-preview", headers=headers, json={
        "dataset_id": dataset.id, "plan": plan, "params": {"min_amount": 20, "d": None},
    })
    assert by_amount.status_code == 200, by_amount.text
    assert by_amount.json()["data"]["blocks"][0]["result"]["rows"] == [
        {"region": "east", "orders": 1}]

    by_day = client.post(f"{_BASE}/{report_id}/plan-preview", headers=headers, json={
        "dataset_id": dataset.id, "plan": plan, "params": {"min_amount": None, "d": "2026-01-01"},
    })
    assert by_day.status_code == 200, by_day.text
    assert by_day.json()["data"]["blocks"][0]["result"]["rows"] == [
        {"region": "east", "orders": 1}, {"region": "west", "orders": 1}]


def test_plan_preview_count_distinct_executes(client, db):
    _make_user(db, "plan_cd_editor", "editor")
    headers = _login(client, "plan_cd_editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    report_id = _make_report(client, headers)
    plan = _plan_payload(filters=[], blocks=[{"type": "query", "name": "q", "columns": [
        {"agg": "count_distinct", "field": "region"}]}])

    response = client.post(f"{_BASE}/{report_id}/plan-preview", headers=headers, json={
        "dataset_id": dataset.id, "plan": plan})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["blocks"][0]["result"]["rows"] == [
        {"count_distinct_region": 2}]


def test_plan_preview_schemaless_dataset_uses_inferred_fields(client, db):
    """schema 未声明列的历史 CSV 数据集：字段口径与 article_schema 的推导一致。"""
    _make_user(db, "plan_csv_editor", "editor")
    headers = _login(client, "plan_csv_editor")
    dataset = _make_dataset(db, schema=None, rows=[
        {"region": "east"}, {"region": "east"}, {"region": "west"}], name="无schema")
    report_id = _make_report(client, headers)

    schema = client.get(f"{_BASE}/datasets/{dataset.id}/schema", headers=headers)
    assert schema.status_code == 200, schema.text
    assert schema.json()["data"] == {
        "dataset_id": dataset.id,
        "dataset_name": dataset.name,
        "columns": [{"name": "region", "type": "string"}],
    }
    missing = client.get(f"{_BASE}/datasets/missing/schema", headers=headers)
    assert missing.status_code == 404

    response = client.post(f"{_BASE}/{report_id}/plan-preview", headers=headers, json={
        "dataset_id": dataset.id,
        "plan": {"version": 1, "blocks": [
            {"type": "query", "name": "q", "columns": [
                {"field": "region"}, {"agg": "count", "alias": "orders"}]}]},
    })
    assert response.status_code == 200, response.text
    rows = response.json()["data"]["blocks"][0]["result"]["rows"]
    assert {"region": "east", "orders": 2} in rows


def test_plan_preview_text_only_plan_skips_execution(client, db):
    _make_user(db, "plan_text_editor", "editor")
    headers = _login(client, "plan_text_editor")
    dataset = _make_dataset(db, schema=None, rows=[])  # 无版本无 schema 也不影响纯文本
    report_id = _make_report(client, headers)

    response = client.post(f"{_BASE}/{report_id}/plan-preview", headers=headers, json={
        "dataset_id": dataset.id,
        "plan": {"version": 1, "blocks": [{"type": "text", "text": "纯文本结论"}]},
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["blocks"] == [{"type": "text", "text": "纯文本结论"}]
    assert data["version"] is None and data["source"] is None
    assert data["markdown"] == "纯文本结论\n"


def test_plan_preview_rejects_invalid_fields_params_and_targets(client, db):
    _make_user(db, "plan_reject_editor", "editor")
    headers = _login(client, "plan_reject_editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    unstructured = _make_dataset(db, kind="unstructured", schema=None, rows=None)
    report_id = _make_report(client, headers)
    url = f"{_BASE}/{report_id}/plan-preview"

    unknown_field = client.post(url, headers=headers, json={
        "dataset_id": dataset.id,
        "plan": _plan_payload(blocks=[{"type": "query", "name": "q", "columns": [
            {"field": "nope"}]}])})
    assert unknown_field.status_code == 422
    detail = unknown_field.json()["detail"]
    assert detail["code"] == "invalid_report_plan"
    assert any("白名单" in error for error in detail["errors"])

    undeclared = client.post(url, headers=headers, json={
        "dataset_id": dataset.id, "plan": _plan_payload(),
        "params": {"unknown": 1}})
    assert undeclared.status_code == 422
    assert undeclared.json()["detail"]["code"] == "report_plan_params_undeclared"
    assert undeclared.json()["detail"]["parameters"] == ["unknown"]

    mistyped = client.post(url, headers=headers, json={
        "dataset_id": dataset.id, "plan": _plan_payload(),
        "params": {"region": 5, "min_amount": "abc"}})
    assert mistyped.status_code == 422
    detail = mistyped.json()["detail"]
    assert detail["code"] == "report_plan_params_invalid"
    assert len(detail["errors"]) == 2

    missing_report = client.post(f"{_BASE}/{uuid.uuid4()}/plan-preview", headers=headers, json={
        "dataset_id": dataset.id, "plan": _plan_payload()})
    assert missing_report.status_code == 404
    assert missing_report.json()["detail"]["code"] == "report_not_found"

    missing_dataset = client.post(url, headers=headers, json={
        "dataset_id": str(uuid.uuid4()), "plan": _plan_payload()})
    assert missing_dataset.status_code == 404
    assert missing_dataset.json()["detail"]["code"] == "dataset_not_found"

    unsupported = client.post(url, headers=headers, json={
        "dataset_id": unstructured.id, "plan": _plan_payload()})
    assert unsupported.status_code == 409
    assert unsupported.json()["detail"]["code"] == "report_dataset_kind_unsupported"


def test_plan_preview_rbac_viewer_reads_custom_role_denied(client, db):
    _make_user(db, "plan_owner_editor", "editor")
    _make_user(db, "plan_viewer", "viewer")
    _make_user(db, "plan_custom", "custom")
    dataset = _make_dataset(db, schema=_typed_schema())
    editor_headers = _login(client, "plan_owner_editor")
    report_id = _make_report(client, editor_headers)

    viewer_headers = _login(client, "plan_viewer")
    readable = client.post(f"{_BASE}/{report_id}/plan-preview", headers=viewer_headers, json={
        "dataset_id": dataset.id,
        "plan": {"version": 1, "blocks": [{"type": "text", "text": "只读"}]}})
    assert readable.status_code == 200, readable.text

    custom_headers = _login(client, "plan_custom")
    denied = client.post(f"{_BASE}/{report_id}/plan-preview", headers=custom_headers, json={
        "dataset_id": dataset.id,
        "plan": {"version": 1, "blocks": [{"type": "text", "text": "无菜单"}]}})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


# ---------------------------------------------------------------- AI 设计


def _design_body(dataset_id: str, **overrides) -> dict:
    body = {"dataset_id": dataset_id, "brief": "按地区看订单量"}
    body.update(overrides)
    return body


def test_plan_design_requires_editor_role(client, db):
    _make_user(db, "design_owner_editor", "editor")
    _make_user(db, "design_viewer", "viewer")
    dataset = _make_dataset(db, schema=_typed_schema())
    editor_headers = _login(client, "design_owner_editor")
    report_id = _make_report(client, editor_headers)

    viewer_headers = _login(client, "design_viewer")
    denied = client.post(f"{_BASE}/{report_id}/plan-design", headers=viewer_headers,
                         json=_design_body(dataset.id))
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "REPORT_WRITE_DENIED"


def test_plan_design_without_llm_returns_unavailable(client, db, monkeypatch):
    from app.operations import ai
    _make_user(db, "design_editor", "editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    headers = _login(client, "design_editor")
    report_id = _make_report(client, headers)

    monkeypatch.setattr(ai, "select_llm_model_config", lambda db, model_id=None: object())
    monkeypatch.setattr(ai, "llm_call_kwargs", lambda config: None)
    response = client.post(f"{_BASE}/{report_id}/plan-design", headers=headers,
                           json=_design_body(dataset.id))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "report_ai_unavailable"


def _mock_chat(monkeypatch, content: str):
    from app.operations import ai
    monkeypatch.setattr(ai, "select_llm_model_config", lambda db, model_id=None: object())
    monkeypatch.setattr(ai, "llm_call_kwargs", lambda config: {"model": "mock"})
    monkeypatch.setattr(ai, "chat", lambda kwargs, messages, tools: {"content": content})


def test_plan_design_invalid_candidate_keeps_visible_errors(client, db, monkeypatch):
    _make_user(db, "design_bad_editor", "editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    headers = _login(client, "design_bad_editor")
    report_id = _make_report(client, headers)

    _mock_chat(monkeypatch, "这不是 JSON")
    garbage = client.post(f"{_BASE}/{report_id}/plan-design", headers=headers,
                          json=_design_body(dataset.id))
    assert garbage.status_code == 422
    assert garbage.json()["detail"]["code"] == "report_ai_invalid_candidate"
    assert garbage.json()["detail"]["errors"]

    _mock_chat(monkeypatch, json.dumps({
        "version": 1,
        "blocks": [{"type": "query", "name": "q", "columns": [{"field": "nope"}]}],
    }))
    unknown_field = client.post(f"{_BASE}/{report_id}/plan-design", headers=headers,
                                json=_design_body(dataset.id))
    assert unknown_field.status_code == 422
    detail = unknown_field.json()["detail"]
    assert detail["code"] == "report_ai_invalid_candidate"
    assert any("白名单" in error for error in detail["errors"])


def test_plan_design_returns_candidate_without_persisting(client, db, monkeypatch):
    _make_user(db, "design_ok_editor", "editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    headers = _login(client, "design_ok_editor")
    report_id = _make_report(client, headers)

    candidate = {
        "version": 1,
        "filters": [],
        "blocks": [
            {"type": "text", "text": "AI 概览"},
            {"type": "query", "name": "q_ai", "columns": [
                {"field": "region"}, {"agg": "count", "alias": "orders"}],
             "sort": [{"field": "orders", "dir": "desc"}]},
            {"type": "chart", "chart": "pie", "query": "q_ai",
             "x": "region", "y": ["orders"]},
        ],
    }
    _mock_chat(monkeypatch, f"```json\n{json.dumps(candidate)}\n```")
    response = client.post(f"{_BASE}/{report_id}/plan-design", headers=headers,
                           json=_design_body(dataset.id, current_plan=None))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["generation_mode"] == "ai"
    assert data["plan"]["blocks"][1]["name"] == "q_ai"
    assert data["explanation"]
    assert {"name": "region", "type": "string"} in data["dataset"]["fields"]

    # 设计不落库：文章数保持为 0。
    detail = client.get(f"{_BASE}/{report_id}", headers=headers).json()["data"]
    assert detail["article_count"] == 0


def test_plan_design_provider_failure_returns_503(client, db, monkeypatch):
    from app.model_configs.llm_gateway import LLMError
    from app.operations import ai
    _make_user(db, "design_err_editor", "editor")
    dataset = _make_dataset(db, schema=_typed_schema())
    headers = _login(client, "design_err_editor")
    report_id = _make_report(client, headers)

    monkeypatch.setattr(ai, "select_llm_model_config", lambda db, model_id=None: object())
    monkeypatch.setattr(ai, "llm_call_kwargs", lambda config: {"model": "mock"})
    def _boom(kwargs, messages, tools):
        raise LLMError("provider down")
    monkeypatch.setattr(ai, "chat", _boom)
    response = client.post(f"{_BASE}/{report_id}/plan-design", headers=headers,
                           json=_design_body(dataset.id))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "report_ai_failed"


# ------------------------------------------------------- 三类资产可用性矩阵


def _version_for(db, dataset: Dataset, *, rowcount: int) -> DatasetVersion:
    blob = json.dumps([] if rowcount == 0 else _ROWS[:rowcount]).encode("utf-8")
    version = DatasetVersion(
        id=str(uuid.uuid4()), dataset_id=dataset.id, version_no=1,
        rowcount=rowcount, data_blob=blob, data_size=len(blob),
        checksum=hashlib.sha256(blob).hexdigest(),
    )
    db.add(version)
    db.flush()
    dataset.latest_version_id = version.id
    db.commit()
    return version


def test_list_report_datasets_availability_matrix(client, db):
    _make_user(db, "avail_editor", "editor")
    headers = _login(client, "avail_editor")

    typed = _make_dataset(db, schema=_typed_schema())
    _make_dataset(db, schema=None, name="无schema有行")
    empty_no_schema = _make_dataset(db, schema=None, rows=None, name="空")
    _version_for(db, empty_no_schema, rowcount=0)
    no_version = _make_dataset(db, schema=_typed_schema(), rows=None, name="无版本")
    _make_dataset(db, kind="unstructured", schema=None, rows=None, name="附件")

    response = client.get(f"{_BASE}/datasets", headers=headers)
    assert response.status_code == 200, response.text
    items = {item["name"]: item for item in response.json()["data"]["items"]}
    assert set(items) == {"订单明细", "无schema有行", "空", "无版本"}  # unstructured 不进列表

    assert items["订单明细"]["status"] == "ready"
    assert items["订单明细"]["reason"] is None
    assert items["订单明细"]["version"]["rowcount"] == 3
    # schema 未声明列但有数据行：执行器可按数据行推导列，列表保持 ready。
    assert items["无schema有行"]["status"] == "ready"
    assert items["空"]["status"] == "unavailable"
    assert "没有可查询的列" in items["空"]["reason"]
    assert items["无版本"]["status"] == "unavailable"
    assert items["无版本"]["reason"] == "数据集尚无可用数据版本"
    assert items["无版本"]["version"] is None


def test_availability_integrated_requires_non_empty_column_map(db, monkeypatch):
    from app.data_channel.datasets import integrated_store

    dataset = _make_dataset(db, kind="integrated", schema=None, rows=None, name="集成")
    version = _version_for(db, dataset, rowcount=5)

    class _Relation:
        column_map: dict = {}

    monkeypatch.setattr(integrated_store, "get_version_relation",
                        lambda db_, dataset_id, version_id: _Relation())
    reportable, reason = query_service._report_availability(db, dataset, version)
    assert reportable is False
    assert "没有可查询的列" in reason

    _Relation.column_map = {"logical": "physical"}
    reportable, reason = query_service._report_availability(db, dataset, version)
    assert reportable is True and reason is None


def test_availability_curated_lake_requires_mapping_and_table(db, monkeypatch):
    from app.data_channel.curated import approved_version_reader
    from app.data_channel.datasets import lake_store

    dataset = _make_dataset(db, kind="curated", schema=None, rows=None, name="成品")
    version = _version_for(db, dataset, rowcount=5)

    monkeypatch.setattr(approved_version_reader, "require_version_approved",
                        lambda db_, dataset_id, ver: None)
    monkeypatch.setattr(approved_version_reader, "has_approved_edits",
                        lambda db_, dataset_id, ver: False)
    monkeypatch.setattr(lake_store, "version_uses_lake", lambda ds, ver: True)
    monkeypatch.setattr(lake_store, "lake_table_exists", lambda db_, dataset_id: True)

    reportable, reason = query_service._report_availability(db, dataset, version)
    assert reportable is False
    assert "物理湖表" in reason

    dataset.schema_json = {"lake_columns": {"region": "col_region"}}
    db.commit()
    reportable, reason = query_service._report_availability(db, dataset, version)
    assert reportable is True and reason is None
