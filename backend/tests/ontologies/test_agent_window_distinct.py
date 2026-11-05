"""固定 30 分钟窗去重主体统计（源端多阶段派生聚合）。

阶段请求由真实 QueryRequest 装配；执行体用假统一查询端口钉定，验证：
内层 group by module+half_hour + count_distinct，外层阈值过滤后按模块
count/max，再一层 scalar 求总量；绝不回传全部 module-window 组。
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.query_spec import spec_hash, verify_drilldown
from app.ontologies.agent_runtime.query_window import bucket_bounds
from app.ontologies.agent_runtime.toolkit import ToolRunner
from tests.ontologies.test_agent_query_view import events_ontology  # noqa: F401

SEPTEMBER_WINDOW = {
    "property": "occurred_at", "timezone": "Asia/Shanghai",
    "start": "2026-09-01T00:00:00", "end": "2026-10-01T00:00:00",
}


def _result(rows, *, total=None, has_next=False, exact=True,
            completeness="complete", partial=False, persistence=True):
    payload = {
        "rows": rows,
        "totalRowCount": len(rows) if total is None else total,
        "hasNext": has_next,
        "truncated": False,
        "partial": partial,
        "exact": exact,
        "completeness": completeness,
        "observedAt": "2026-11-02T00:00:00+00:00",
        "citations": [],
    }
    if persistence:
        payload["persistence"] = {"mode": "ephemeral"}
    return payload


def _runner(db, events, user, session_id="conv-window"):
    _, _, scope = build_scope(
        db, events["ontologyId"], release_id=events["releaseId"])
    return ToolRunner(db, scope, user=user, query_session_id=session_id)


def _patch_sequence(monkeypatch, results):
    from app.ontologies.agent_query import service as query_service

    calls = []

    def fake_execute(db, user, request, *, session_kind, session_id,
                     include_page_size=False):
        calls.append({"request": request, "user": user,
                      "session_kind": session_kind, "session_id": session_id})
        return results[min(len(calls) - 1, len(results) - 1)]

    from app.ontologies.agent_query import remote_query
    monkeypatch.setattr(query_service, "execute_unified_query", fake_execute)
    monkeypatch.setattr(remote_query, "cancel_remote_query",
                        lambda *args, **kwargs: None)
    return calls


def _args(**overrides):
    payload = {
        "object_type": "Event",
        "mode": "window_distinct",
        "subject_property": "actor",
        "time_property": "occurred_at",
        "threshold": 5,
        "window": SEPTEMBER_WINDOW,
        "window_minutes": 30,
        "group_by": [{"property": "module"}],
    }
    payload.update(overrides)
    return payload


def test_window_distinct_runs_three_source_side_aggregate_stages(
        db, admin_user, events_ontology, monkeypatch):
    calls = _patch_sequence(monkeypatch, [
        # 阶段 2：每个模块的达标窗口数与最大去重主体数
        _result([{"g0": "m1", "qualifyingWindows": 2,
                  "maxDistinctSubjects": 7},
                 {"g0": "m2", "qualifyingWindows": 1,
                  "maxDistinctSubjects": 5}], total=2),
        # 阶段 3：总量标量
        _result([{"qualifyingWindows": 3, "qualifyingModules": 2}], total=1),
        # 样本窗口（只取前 20，绝不回传全部 module-window 组）
        _result([{"g0": "m1", "window": "2026-09-10T09:30:00",
                  "users": 7, "rows": 9},
                 {"g0": "m2", "window": "2026-09-20T03:00:00",
                  "users": 5, "rows": 6}], total=3, has_next=True),
    ])
    runner = _runner(db, events_ontology, admin_user)

    out = runner.run("analyze_sequences", _args())

    assert len(calls) == 3
    module_request = calls[0]["request"]
    derived = module_request.sources[0]
    assert derived.alias == "agg" and derived.subquery is not None
    inner = derived.subquery
    assert [item.property for item in inner.group_by] == [
        "module", "occurred_at"]
    assert [item.bucket for item in inner.group_by] == [None, "half_hour"]
    assert [(item.fn, item.property) for item in inner.aggregates] == [
        ("count_distinct", "actor"), ("count", None)]
    assert [item.as_ for item in inner.select] == ["g0", "g1"]
    assert module_request.bounded_stats is True
    assert [(item.fn, item.property) for item in module_request.aggregates] == [
        ("count", None), ("max", "users")]
    assert [(item.property, item.op, item.value)
            for item in module_request.where] == [("users", "gte", 5)]

    totals_request = calls[1]["request"]
    assert totals_request.sources[0].alias == "per_module"
    assert totals_request.sources[0].subquery is not None
    assert [(item.fn, item.property) for item in totals_request.aggregates] == [
        ("sum", "qualifyingWindows"), ("count", None)]

    sample_request = calls[2]["request"]
    assert sample_request.sources[0].subquery is not None
    assert [item.property for item in sample_request.select] == [
        "g0", "g1", "users", "rows"]
    assert [(item.output, item.direction)
            for item in sample_request.order_by] == [
        ("users", "desc"), ("window", "asc"), ("g0", "asc")]
    assert sample_request.page_size == 20

    # 真实计划编制（授权/派生源/半小时间桶编译）必须在执行体之前通过。
    from app.ontologies.agent_query import remote_query
    plan = remote_query.prepare_remote_plan(db, admin_user, module_request)
    nested = plan["sources"][0]["subquery"]
    assert nested["sources"][0]["binding"]["kind"] == "ontology"
    assert nested["groupBy"][-1]["bucket"] == "half_hour"
    assert nested["groupBy"][-1]["property"] == "occurred_at"
    assert plan["outputs"] == ["g0", "qualifyingWindows",
                               "maxDistinctSubjects"]
    # 模块预览计划只在最外层带 statsLimit；这正是 totals 内嵌 middle 时
    # 必须确认不会把 per-module 预览 LIMIT 带进总量计算的证据。
    assert plan.get("statsLimit") == module_request.page_size

    totals_plan = remote_query.prepare_remote_plan(
        db, admin_user, totals_request)
    assert totals_plan["outputs"] == ["qualifyingWindows",
                                      "qualifyingModules"]
    assert totals_plan["sources"][0]["subquery"]["groupBy"] == [
        {"source": "agg", "property": "g0"}]
    # totals 顶层没有 boundedStats；内嵌的 module 预览计划不得携带
    # statsLimit（即不得 LIMIT 200 后再 count/sum）。
    assert "statsLimit" not in totals_plan
    assert "statsLimit" not in totals_plan["sources"][0]["subquery"]
    sample_plan = remote_query.prepare_remote_plan(
        db, admin_user, sample_request)
    assert sample_plan["outputs"] == ["g0", "window", "users", "rows"]

    assert out["mode"] == "window_distinct"
    assert out["qualifyingWindows"] == 3
    assert out["qualifyingModules"] == 2
    assert out["qualifyingModulesScope"] == "source_aggregate"
    assert out["windowMinutes"] == 30
    assert out["windowBucket"] == "half_hour"
    assert [item["module"] for item in out["modules"]] == ["m1", "m2"]
    assert [item["qualifyingWindows"] for item in out["modules"]] == [2, 1]
    assert [item["maxDistinctSubjects"] for item in out["modules"]] == [7, 5]
    assert out["modulesTruncated"] is False
    assert out["evaluatedWindows"] is None
    assert out["evaluatedWindowsAvailable"] is False
    assert len(out["sampleWindows"]) == 2
    assert out["sampleWindowsTruncated"] is True
    first = out["sampleWindows"][0]
    assert first["windowStart"] == "2026-09-10T17:30:00+08:00"
    assert first["windowEnd"] == "2026-09-10T18:00:00+08:00"
    assert first["windowStartUtc"] == "2026-09-10T09:30:00+00:00"
    assert first["windowEndUtc"] == "2026-09-10T10:00:00+00:00"
    assert first["distinctSubjects"] == 7
    assert first["drilldown"]["token"]
    assert out["querySpec"]["description"]["semantics"] == "window_distinct"
    assert out["querySpec"]["description"]["view"] == "totals"
    assert [item["fn"] for item in
            out["querySpec"]["query"]["aggregates"]] == ["sum", "count"]
    assert out["querySpec"]["labels"]["qualifyingModules"] == "达标模块数"
    assert out["querySpec"]["drilldownBase"]["select"]
    assert out["moduleQuerySpec"]["description"]["view"] == "modules"
    assert [item["fn"] for item in
            out["moduleQuerySpec"]["query"]["aggregates"]] == ["count", "max"]
    assert out["moduleQuerySpecHash"] == spec_hash(
        out["moduleQuerySpec"])
    assert out["exact"] is True and out["partial"] is False


def test_window_distinct_rejects_other_window_minutes_before_query(
        db, admin_user, events_ontology, monkeypatch):
    calls = _patch_sequence(monkeypatch, [_result([])])

    with pytest.raises(ToolError, match="sequence_window_minutes_unsupported"):
        _runner(db, events_ontology, admin_user).run(
            "analyze_sequences", _args(window_minutes=15))
    assert calls == []


def test_window_distinct_requires_explicit_window(
        db, admin_user, events_ontology, monkeypatch):
    calls = _patch_sequence(monkeypatch, [_result([])])

    with pytest.raises(ToolError, match="sequence_window_required"):
        _runner(db, events_ontology, admin_user).run(
            "analyze_sequences", _args(window=None))
    assert calls == []


def test_window_distinct_fails_closed_on_incomplete_aggregate(
        db, admin_user, events_ontology, monkeypatch):
    _patch_sequence(monkeypatch, [
        _result([{"g0": "m1", "qualifyingWindows": 2,
                  "maxDistinctSubjects": 7}], total=2,
                exact=False, completeness="partial"),
    ])

    with pytest.raises(ToolError, match="window_distinct_incomplete"):
        _runner(db, events_ontology, admin_user).run(
            "analyze_sequences", _args())


def test_half_hour_bucket_bounds_are_timestamps():
    start, end = bucket_bounds("2026-09-10T09:47:31", "half_hour")
    assert start.isoformat() == "2026-09-10T09:30:00"
    assert end.isoformat() == "2026-09-10T10:00:00"
    assert bucket_bounds("2026-09-10", "half_hour") is None


def _remote_snapshot(timezone_flag):
    column = {"name": "occurred_at", "type": "timestamp"}
    if timezone_flag is not None:
        column["timezone"] = timezone_flag
    return {
        "objectTypes": [{"id": "ot-event", "name": "Event",
                         "displayName": "事件"}],
        "mappings": [{
            "id": "map-remote-window", "targetObjectTypeId": "ot-event",
            "entityClass": "Event", "curatedDatasetId": "ds-window",
            "bindingMode": "remote",
            "remote": {
                "assetId": "asset-window", "datasetId": "ds-window",
                "connectionId": "conn-window", "schemaName": "public",
                "tableName": "events", "schemaFingerprint": "f" * 64,
                "columns": [column], "primaryKey": ["occurred_at"],
            },
            "fieldMapping": {"occurred_at": "occurred_at"},
        }],
    }


def test_remote_time_column_timezone_contract_is_authoritative():
    from app.ontologies.agent_runtime.sequence_window_distinct import (
        _remote_time_column_timezone,
    )

    assert _remote_time_column_timezone(
        _remote_snapshot(True), "ot-event", "occurred_at") is True
    assert _remote_time_column_timezone(
        _remote_snapshot(False), "ot-event", "occurred_at") is False
    assert _remote_time_column_timezone(
        _remote_snapshot(None), "ot-event", "occurred_at") is None


def test_window_condition_value_preserves_naive_datetime_vs_aware_timestamp():
    from app.ontologies.agent_runtime.sequence_window_distinct import (
        _window_condition_value,
    )

    naive = datetime(2026, 9, 10, 9, 30)
    aware = datetime(2026, 9, 10, 9, 30, tzinfo=timezone.utc)
    # aware TIMESTAMP 列：naive 桶值补 UTC 偏移；aware 值转 UTC。
    assert _window_condition_value(naive, True) == \
        "2026-09-10T09:30:00+00:00"
    assert _window_condition_value(aware, True) == \
        "2026-09-10T09:30:00+00:00"
    # naive DATETIME 列：保持无偏移；aware 值按 UTC 墙钟去偏移。
    assert _window_condition_value(naive, False) == "2026-09-10T09:30:00"
    assert _window_condition_value(aware, False) == "2026-09-10T09:30:00"
    # 无权威契约：原样保留，不猜测。
    assert _window_condition_value(naive, None) == "2026-09-10T09:30:00"
    assert _window_condition_value(aware, None) == \
        "2026-09-10T09:30:00+00:00"


def test_sample_drilldown_conditions_follow_timestamp_contract(
        db, admin_user, events_ontology, monkeypatch):
    from app.ontologies.agent_runtime import sequence_window_distinct as window

    _patch_sequence(monkeypatch, [
        _result([{"g0": "m1", "qualifyingWindows": 1,
                  "maxDistinctSubjects": 6}], total=1),
        _result([{"qualifyingWindows": 1, "qualifyingModules": 1}], total=1),
        _result([{"g0": "m1", "window": "2026-09-10T09:30:00",
                  "users": 6, "rows": 6}], total=1),
    ])
    monkeypatch.setattr(
        window, "_time_column_timezone", lambda scope, ot, prop: True)
    out = _runner(db, events_ontology, admin_user).run(
        "analyze_sequences", _args())

    payload = verify_drilldown(
        out["sampleWindows"][0]["drilldown"]["token"],
        owner=str(admin_user.id),
        ontology_id=events_ontology["ontologyId"],
        conversation_id="conv-window",
    )
    time_conditions = [
        (item["op"], item["value"]) for item in payload["conditions"]
        if item["property"] == "occurred_at"
    ]
    assert time_conditions == [
        ("gte", "2026-09-10T09:30:00+00:00"),
        ("lt", "2026-09-10T10:00:00+00:00"),
    ]


def test_window_distinct_without_module_documents_derived_module_count(
        db, admin_user, events_ontology, monkeypatch):
    calls = _patch_sequence(monkeypatch, [
        _result([{"qualifyingWindows": 3,
                  "maxDistinctSubjects": 7}], total=1),
        _result([], total=3),
    ])
    out = _runner(db, events_ontology, admin_user).run(
        "analyze_sequences", _args(group_by=[]))

    assert len(calls) == 2  # 无模块维度：总量 + 样本，没有模块预览查询
    assert out["qualifyingWindows"] == 3
    assert out["qualifyingModules"] == 1
    assert out["qualifyingModulesScope"] == (
        "derived_from_qualifying_windows_no_module_dimension")
    assert out["modules"] == [{
        "module": "全部", "moduleValues": [],
        "qualifyingWindows": 3, "maxDistinctSubjects": 7,
    }]
    assert out["moduleQuerySpec"] is None
    assert out["querySpec"]["description"]["qualifyingModulesScope"] == (
        "derived_from_qualifying_windows_no_module_dimension")
    assert "qualifyingModules" not in out["querySpec"]["labels"]
    assert out["querySpec"]["description"]["qualifyingModulesSemantics"]
    assert "qualifyingWindows" in out["querySpec"]["labels"]


def test_modules_are_ordered_by_windows_then_max_distinct_then_key(
        db, admin_user, events_ontology, monkeypatch):
    calls = _patch_sequence(monkeypatch, [
        # 故意乱序返回：输出必须按 qualifyingWindows desc、maxDistinct desc、
        # module key asc 重新稳定排序。
        _result([{"g0": "m9", "qualifyingWindows": 1,
                  "maxDistinctSubjects": 9},
                 {"g0": "m1", "qualifyingWindows": 3,
                  "maxDistinctSubjects": 5},
                 {"g0": "m0", "qualifyingWindows": 3,
                  "maxDistinctSubjects": 5}], total=3),
        _result([{"qualifyingWindows": 7, "qualifyingModules": 3}], total=1),
        _result([], total=7),
    ])
    out = _runner(db, events_ontology, admin_user).run(
        "analyze_sequences", _args())

    assert [(item.output, item.direction)
            for item in calls[0]["request"].order_by] == [
        ("qualifyingWindows", "desc"),
        ("maxDistinctSubjects", "desc"),
        ("g0", "asc"),
    ]
    assert [item["module"] for item in out["modules"]] == ["m0", "m1", "m9"]


class _BudgetScope:
    ontology = SimpleNamespace(id="ontology-budget")
    release_id = "release-budget"

    def resolve_visible_property(self, ot, ref):
        return ref


class _BudgetObjectType:
    id = "ot-budget"
    display_name = "预算对象"


def _budget_args(**overrides):
    payload = {
        "window_minutes": 30,
        "group_by": [],
    }
    payload.update(overrides)
    return payload


def test_window_group_budget_is_actionable_before_source(monkeypatch):
    from app.ontologies.agent_runtime import sequence_window_distinct as window
    from app.ontologies.agent_query import service as query_service

    def _explode(*args, **kwargs):
        raise AssertionError("source query must not run")

    monkeypatch.setattr(query_service, "execute_unified_query", _explode)

    with pytest.raises(ToolError, match="sequence_window_group_budget_exceeded"):
        window.analyze_window_distinct(
            db=None, scope=_BudgetScope(), user=None, session_id="",
            ot=_BudgetObjectType(), args=_budget_args(group_by=[
                {"property": f"p{index}"} for index in range(8)]),
            subject_prop="actor", time_prop="occurred_at",
            time_property_type="datetime", threshold=5, where=[],
            resolved_window={}, cap=50, cancel_handle=lambda *args: None)


def test_window_filter_budget_is_actionable_before_source(monkeypatch):
    from app.ontologies.agent_runtime import sequence_window_distinct as window
    from app.ontologies.agent_query import service as query_service

    def _explode(*args, **kwargs):
        raise AssertionError("source query must not run")

    monkeypatch.setattr(query_service, "execute_unified_query", _explode)

    with pytest.raises(ToolError, match="sequence_window_filter_budget_exceeded"):
        window.analyze_window_distinct(
            db=None, scope=_BudgetScope(), user=None, session_id="",
            ot=_BudgetObjectType(), args=_budget_args(),
            subject_prop="actor", time_prop="occurred_at",
            time_property_type="datetime", threshold=5,
            where=[{"source": "s0", "property": "action", "op": "eq",
                    "value": "x"}] * 17,
            resolved_window={}, cap=50, cancel_handle=lambda *args: None)
