"""query_objects / get_data_time_range 工具。

本地物化对象走真实本地快照执行（orders_ontology 端到端）；远程绑定对象
用 execute_unified_query 假端口钉定请求装配（别名展开/默认投影/页帽/分页
句柄生命周期/时间范围双聚合）。
"""
import pytest

from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.toolkit import ToolRunner
from tests.ontologies.test_agent_runtime import modeled_ontology  # noqa: F401
from tests.ontologies.test_agent_unified_query import orders_ontology  # noqa: F401

from tests.ontologies.test_agent_remote_aggregation import (  # noqa: F401
    _freeze_with_rich_remote_supplier, _freeze_with_wide_remote_supplier,
    _patch_execute_unified, _remote_runner,
)


def _local_runner(db, orders, user) -> ToolRunner:
    _, _, scope = build_scope(db, orders["ontologyId"],
                              release_id=orders["releaseId"])
    return ToolRunner(db, scope, user=user, query_session_id="s-qo")


# ------------------------------------------------ 本地物化：明细/按ID/排序/关联/统计


def test_local_detail_filter_sort_and_pagination(db, admin_user, orders_ontology):
    runner = _local_runner(db, orders_ontology, admin_user)
    out = runner.run("query_objects", {
        "object_type": "Order",
        "filters": [{"property": "status", "op": "eq", "value": "paid"}],
        "select": ["order_no", "amount"],
        "order_by": [{"output": "order_no", "direction": "asc"}],
        "page_size": 10})
    assert out["dataMode"] == "materialized"
    assert out["returned"] == 2
    assert [row["order_no"] for row in out["rows"]] == ["SO-002", "SO-003"]
    assert out["exact"] is True and out["hasNext"] is False


def test_local_query_by_primary_key(db, admin_user, orders_ontology):
    runner = _local_runner(db, orders_ontology, admin_user)
    out = runner.run("query_objects", {
        "object_type": "Order",
        "filters": [{"property": "order_no", "op": "eq", "value": "SO-002"}],
        "select": ["order_no", "status"]})
    assert out["returned"] == 1
    assert out["rows"][0]["order_no"] == "SO-002"
    assert out["rows"][0]["status"] == "paid"


def test_local_declared_relation_join(db, admin_user, orders_ontology):
    runner = _local_runner(db, orders_ontology, admin_user)
    out = runner.run("query_objects", {
        "object_type": "Order",
        "joins": [{"object_type": "Supplier", "link_type": "order_supplier",
                   "type": "left"}],
        "select": ["order_no", {"object_type": "Supplier", "property": "sname"}],
        "order_by": [{"output": "order_no", "direction": "asc"}]})
    assert out["joinedObjectTypes"] == ["供应商"]
    by_no = {row["order_no"]: row.get("sname") for row in out["rows"]}
    assert by_no == {"SO-001": "华南电子", "SO-002": None, "SO-003": None}


def test_local_group_stats(db, admin_user, orders_ontology):
    runner = _local_runner(db, orders_ontology, admin_user)
    out = runner.run("query_objects", {
        "object_type": "Order",
        "group_by": [{"property": "status"}],
        "aggregates": [{"fn": "count"}, {"fn": "sum", "property": "amount",
                                         "as": "total_amount"}],
        "order_by": [{"output": "count", "direction": "desc"}]})
    counts = {row["status"]: row["count"] for row in out["rows"]}
    assert counts == {"paid": 2, "pending": 1}
    sums = {row["status"]: row["total_amount"] for row in out["rows"]}
    assert sums["pending"] == 100


def test_local_bucket_group_is_rejected_with_guidance(db, admin_user, orders_ontology):
    runner = _local_runner(db, orders_ontology, admin_user)
    with pytest.raises(ToolError, match="bucket"):
        runner.run("query_objects", {
            "object_type": "Order",
            "group_by": [{"property": "status", "bucket": "month"}],
            "aggregates": [{"fn": "count"}]})


# ------------------------------------------------ 远程绑定：装配与句柄生命周期


def test_remote_detail_defaults_and_readable_rows(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_unified(monkeypatch, result={
        "queryId": "q-qo-1", "rows": [{"c0": "华南电子", "c1": "华南",
                                       "c2": "12.5", "c3": "2026-01-03"}],
        "totalRowCount": 1, "rowCounting": "complete", "hasNext": False,
        "cursor": None, "truncated": False, "partial": False, "exact": True,
        "completeness": "complete", "pageSize": 50, "observedAt": "t",
        "countsScope": "complete_snapshot", "citations": [],
        "releases": {"objects": {"dataMode": "remote"}},
        "persistence": {"mode": "ephemeral"}})

    out = runner.run("query_objects", {"object_type": "Supplier"})

    request = calls[0]["request"]
    assert [source.alias for source in request.sources] == ["s0"]
    # 默认投影 = 授权属性 ∩ 远程列映射
    assert [item.property for item in request.select] == [
        "sname", "region", "rating", "since_day"]
    assert out["dataMode"] == "remote"
    assert out["rows"][0]["sname"] == "华南电子"
    assert out["rows"][0]["since_day"] == "2026-01-03"
    assert out["hasNext"] is False and out["cursor"] is None


def test_remote_pagination_keeps_cursor_then_releases_terminal_page(
        modeled_ontology, db, admin_user, monkeypatch):
    from app.ontologies.agent_query import service as query_service

    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    cancelled = []
    from app.ontologies.agent_query import remote_query
    monkeypatch.setattr(query_service, "execute_unified_query", lambda *a, **k: {
        "queryId": "q-qo-2", "rows": [{"c0": "a"}, {"c0": "b"}],
        "totalRowCount": 3, "rowCounting": "complete", "hasNext": True,
        "cursor": "cur-1", "truncated": True, "partial": False, "exact": True,
        "completeness": "complete", "pageSize": 2, "observedAt": "t",
        "countsScope": "complete_snapshot", "citations": [],
        "releases": {"objects": {"dataMode": "remote"}},
        "persistence": {"mode": "ephemeral"}})
    monkeypatch.setattr(remote_query, "cancel_remote_query",
                        lambda _db, _user, qid: cancelled.append(qid))

    out = runner.run("query_objects", {"object_type": "Supplier", "page_size": 2})
    assert out["hasNext"] is True and out["cursor"] == "cur-1"
    assert out["truncated"] is True
    assert cancelled == []  # 中间页不释放句柄

    fetched = []
    monkeypatch.setattr(query_service, "fetch_unified_query_page",
                        lambda *a, **k: fetched.append(k) or {
                            "queryId": "q-qo-2", "rows": [{"c0": "c"}],
                            "totalRowCount": 3, "rowCounting": "complete",
                            "hasNext": False, "cursor": None, "truncated": False,
                            "partial": False, "exact": True, "completeness":
                            "complete", "pageSize": 2, "observedAt": "t",
                            "countsScope": "complete_snapshot", "citations": [],
                            "releases": {"objects": {"dataMode": "remote"}},
                            "persistence": {"mode": "ephemeral"}})
    final = runner.run("query_objects", {
        "object_type": "Supplier", "cursor": "cur-1"})
    assert final["hasNext"] is False and final["returned"] == 1
    assert fetched and fetched[0]["session_kind"] == "ontology_agent"
    assert cancelled == ["q-qo-2"]  # 终页释放


def test_wide_default_projection_is_bounded_and_reports_omitted_fields(
        modeled_ontology, db, admin_user, monkeypatch):
    """47 属性远程对象默认投影不得越界；主键/过滤/窗口/排序字段优先。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_wide_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_unified(monkeypatch, result={
        "queryId": "q-wide-1", "rows": [{"c0": "华南电子"}],
        "totalRowCount": 1, "rowCounting": "complete", "hasNext": False,
        "cursor": None, "truncated": False, "partial": False, "exact": True,
        "completeness": "complete", "pageSize": 50, "observedAt": "t",
        "countsScope": "complete_snapshot", "citations": [],
        "releases": {"objects": {"dataMode": "remote"}},
        "persistence": {"mode": "ephemeral"}})

    out = runner.run("query_objects", {
        "object_type": "Supplier",
        "filters": [{"property": "field_40", "op": "eq", "value": "x"}],
        "window": {"property": "since_day", "timezone": "Asia/Shanghai",
                   "start": "2026-01-01", "end": "2026-02-01"},
        "order_by": [{"output": "field_41", "direction": "asc"}],
    })

    request = calls[0]["request"]
    selected = [item.property for item in request.select]
    assert len(selected) == 32
    # 可查询主键 + 当前过滤/窗口/排序字段最先进入投影。
    assert selected[:4] == ["sname", "field_40", "since_day", "field_41"]
    projection = out["projection"]
    assert projection["cap"] == 32
    assert len(projection["selectedFields"]) == 32
    assert len(projection["omittedFields"]) == 15
    assert "field_42" in projection["omittedFields"]
    assert "field_42" not in out["querySpec"]["description"]["select"]
    assert out["querySpec"]["description"]["omittedFields"] == \
        projection["omittedFields"]
    # 省略字段只是元数据，不能出现在观测列/行里。
    assert all(column.get("name") != "field_42" for column in out["columns"])


def test_wide_default_projection_metadata_survives_cursor_pages(
        modeled_ontology, db, admin_user, monkeypatch):
    from app.ontologies.agent_query import service as query_service

    oid = modeled_ontology["id"]
    release_id = _freeze_with_wide_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    monkeypatch.setattr(query_service, "execute_unified_query", lambda *a, **k: {
        "queryId": "q-wide-3", "rows": [{"c0": "华南电子"}],
        "totalRowCount": 2, "rowCounting": "complete", "hasNext": True,
        "cursor": "cur-wide", "truncated": True, "partial": False, "exact": True,
        "completeness": "complete", "pageSize": 1, "observedAt": "t",
        "countsScope": "complete_snapshot", "citations": [],
        "releases": {"objects": {"dataMode": "remote"}},
        "persistence": {"mode": "ephemeral"}})
    from app.ontologies.agent_query import remote_query
    monkeypatch.setattr(remote_query, "cancel_remote_query", lambda *a: None)
    monkeypatch.setattr(query_service, "fetch_unified_query_page",
                        lambda *a, **k: {
                            "queryId": "q-wide-3", "rows": [{"c0": "华东电子"}],
                            "totalRowCount": 2, "rowCounting": "complete",
                            "hasNext": False, "cursor": None, "truncated": False,
                            "partial": False, "exact": True,
                            "completeness": "complete", "pageSize": 1,
                            "observedAt": "t", "countsScope": "complete_snapshot",
                            "citations": [],
                            "releases": {"objects": {"dataMode": "remote"}},
                            "persistence": {"mode": "ephemeral"}})

    first = runner.run("query_objects", {
        "object_type": "Supplier", "page_size": 1})
    omitted = first["projection"]["omittedFields"]
    assert omitted and "field_42" in omitted

    final = runner.run("query_objects", {
        "object_type": "Supplier", "cursor": "cur-wide"})
    assert final["hasNext"] is False
    assert final["projection"]["omittedFields"] == omitted
    assert final["querySpec"]["description"]["omittedFields"] == omitted


def test_explicit_select_over_32_is_rejected_before_execution(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_wide_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_unified(monkeypatch, result={"rows": []})

    with pytest.raises(ToolError, match="query_objects_select_too_many"):
        runner.run("query_objects", {
            "object_type": "Supplier",
            "select": ["sname"] + [f"field_{index:02d}" for index in range(32)]})
    assert calls == []


def test_explicit_unmapped_remote_property_is_rejected_before_execution(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(
        db, oid, unmapped_properties=["internal_note"])
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_unified(monkeypatch, result={"rows": []})

    with pytest.raises(ToolError, match="query_objects_property_unqueryable"):
        runner.run("query_objects", {
            "object_type": "Supplier", "select": ["internal_note"]})
    assert calls == []


def test_stats_drilldown_projection_is_bounded_and_keeps_query_spec(
        modeled_ontology, db, admin_user, monkeypatch):
    """聚合下钻明细投影与主投影共享 32 项上限，querySpec 必须保留。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_wide_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    _patch_execute_unified(monkeypatch, result={
        "queryId": "q-wide-2", "rows": [{"g0": "华南", "a0": 4}],
        "totalRowCount": 1, "rowCounting": "complete", "hasNext": False,
        "cursor": None, "truncated": False, "partial": False, "exact": True,
        "completeness": "complete", "pageSize": 50, "observedAt": "t",
        "countsScope": "complete_snapshot", "citations": [],
        "releases": {"objects": {"dataMode": "remote"}},
        "persistence": {"mode": "ephemeral"}})

    out = runner.run("query_objects", {
        "object_type": "Supplier",
        "group_by": [{"property": "region"}],
        "aggregates": [{"fn": "count"}],
    })

    detail_select = out["querySpec"]["drilldownBase"]["select"]
    assert len(detail_select) == 32
    assert [item["property"] for item in detail_select][:2] == ["sname", "region"]
    omitted = out["querySpec"]["description"]["drilldownOmittedFields"]
    assert omitted and "field_42" in omitted
    assert out["drilldownProjection"]["omittedFields"] == omitted
    assert out["querySpecHash"]


# ------------------------------------------------ 数据时间范围


def test_data_time_range_remote_min_max(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_unified(monkeypatch, result={
        "queryId": "q-tr-1", "rows": [{"minValue": "2026-01-03",
                                       "maxValue": "2026-10-04"}],
        "partial": False, "exact": True, "completeness": "complete",
        "observedAt": "t", "countsScope": "complete_snapshot", "citations": []})

    out = runner.run("get_data_time_range", {"object_type": "Supplier"})

    request = calls[0]["request"]
    assert [(agg.fn, agg.property) for agg in request.aggregates] == [
        ("min", "since_day"), ("max", "since_day")]
    assert request.page_size == 1
    assert out["property"] == "since_day"
    assert out["min"] == "2026-01-03" and out["max"] == "2026-10-04"
    assert out["dataCoverage"]["reason"] == "covered"


def test_data_time_range_empty_source(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    _patch_execute_unified(monkeypatch, result={
        "queryId": "q-tr-2", "rows": [{"minValue": None, "maxValue": None}],
        "partial": False, "exact": True, "completeness": "complete",
        "observedAt": "t", "countsScope": "complete_snapshot", "citations": []})

    out = runner.run("get_data_time_range", {"object_type": "Supplier"})
    assert out["min"] is None and out["max"] is None
    assert out["dataCoverage"]["reason"] == "source_no_data"


def test_data_time_range_local_type_is_rejected(db, admin_user, orders_ontology):
    runner = _local_runner(db, orders_ontology, admin_user)
    with pytest.raises(ToolError, match="data_time_range_local_unsupported"):
        runner.run("get_data_time_range", {"object_type": "Order"})
